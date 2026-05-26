"""Run drafting agents outside the job queue with strict success criteria.

This bypasses AGENT_MAP, build_agent_prompt, and repo overlays entirely.
Used for review-announcer and review-digest which have no associated
job, no repo worktree, and no repo-specific context.
"""
import os
import re
import tempfile
from pathlib import Path

import backends
from backends.frontmatter import PromptBuildError, parse_frontmatter, pick

SCRIPT_DIR = Path(__file__).resolve().parent


def _get_backend():
    # Lazy lookup so tests can monkeypatch backends.get_backend / CLAUDIA_BACKEND
    # without importing inline_agents at a different point in the env lifecycle.
    return backends.get_backend(os.getenv("CLAUDIA_BACKEND", "codex"))


def run_inline_agent(
    agent_name: str,
    placeholders: dict[str, str],
    *,
    expected_type: str,
    timeout_seconds: int = 180,
) -> dict:
    """Run an agent file out-of-queue with strict success criteria.

    Returns one of:
        {"result": "ok", "delta": {...}}
        {"result": "agent_failure", "reason": "<tag>"}
    """
    agent_file = SCRIPT_DIR / "agents" / f"{agent_name}.md"
    if not agent_file.is_file():
        return {"result": "agent_failure", "reason": "agent_file_missing"}

    output_file = None
    try:
        text = agent_file.read_text()
        fm, body = parse_frontmatter(text)
        backend = _get_backend()

        try:
            model, effort_or_turns = pick(
                backend.name, fm, agent_name=agent_name, agent_file=str(agent_file),
            )
        except PromptBuildError as exc:
            return {"result": "agent_failure",
                    "reason": f"prompt_build_failed:{exc.missing}"}

        prompt = body
        for key, value in placeholders.items():
            prompt = prompt.replace("{{" + key + "}}", value)

        unresolved = re.findall(r"\{\{[A-Z0-9_]+\}\}", prompt)
        if unresolved:
            unresolved_str = ",".join(sorted(set(unresolved)))[:80]
            return {"result": "agent_failure",
                    "reason": f"unresolved:{unresolved_str}"}

        claudia_dir = placeholders.get("CLAUDIA_DIR", str(SCRIPT_DIR))
        output_file = tempfile.mktemp(
            suffix=".jsonl", prefix=f"inline-{agent_name}-",
        )

        try:
            run_result = backends.run_with_heartbeat(
                backend,
                prompt=prompt,
                cwd=claudia_dir,
                model=model,
                effort_or_turns=effort_or_turns,
                job_id=-1,
                timeout_seconds=timeout_seconds,
                output_file=output_file,
            )
        except Exception as exc:
            return {"result": "agent_failure",
                    "reason": f"exception:{type(exc).__name__}"}

        if run_result.exit_code == -1:
            return {"result": "agent_failure", "reason": "timeout"}
        if run_result.exit_code != 0:
            return {"result": "agent_failure",
                    "reason": f"exit_{run_result.exit_code}"}

        try:
            parsed = backend.parse_output(run_result.ctx, output_file)
        except Exception as exc:
            return {"result": "agent_failure",
                    "reason": f"parse_exception:{type(exc).__name__}"}

        if parsed.unexpected_events:
            return {"result": "agent_failure",
                    "reason": f"unexpected_events:{','.join(parsed.unexpected_events)}"}

        deltas = parsed.state_deltas
        if not deltas:
            return {"result": "agent_failure", "reason": "no_delta"}
        if len(deltas) > 1:
            return {"result": "agent_failure", "reason": "multiple_deltas"}
        if parsed.malformed_state_deltas:
            return {"result": "agent_failure", "reason": "malformed_json"}

        delta = deltas[0]
        if delta.get("type") != expected_type:
            return {"result": "agent_failure",
                    "reason": f"type_mismatch:{delta.get('type')}"}
        if not (isinstance(delta.get("message"), str) and delta["message"].strip()):
            return {"result": "agent_failure", "reason": "empty_message"}

        return {"result": "ok", "delta": delta}
    except Exception as exc:
        # Hard contract: NEVER raise. Anything outside the per-step catches
        # (read_text, parse_frontmatter, backend lookup, .replace, etc.)
        # lands here so callers can rely on a dict result.
        return {"result": "agent_failure",
                "reason": f"unhandled:{type(exc).__name__}"}
    finally:
        if output_file:
            try:
                os.unlink(output_file)
            except OSError:
                pass
