"""ClaudeBackend — wraps the existing `claude -p --output-format stream-json` shape.

Semantically preserves today's worker.py + utils.py behavior so the migration
is invisible to the audit trail (token math identical, cost from
total_cost_usd, model_used = first key in modelUsage).
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from pathlib import Path

from backends.base import ParsedRun, RunContext
from backends.frontmatter import PromptBuildError, parse_frontmatter, pick

log = logging.getLogger("claudia.backends.claude")

SCRIPT_DIR = Path(__file__).resolve().parent.parent
_DELTA_RE = re.compile(r"```state_delta\s*\n(.*?)\n```", re.DOTALL)


class ClaudeBackend:
    name = "claude"
    log_formatter_script = str(SCRIPT_DIR / "stream-log.py")
    requires_delta_for_success = False

    def build_command(self, ctx: RunContext) -> list[str]:
        cmd = [
            "claude",
            "--dangerously-skip-permissions",
            "--output-format", "stream-json",
            "--verbose",
        ]
        if ctx.model:
            cmd.extend(["--model", ctx.model])
        if isinstance(ctx.effort_or_turns, int) and ctx.effort_or_turns:
            cmd.extend(["--max-turns", str(ctx.effort_or_turns)])
        cmd.extend(["-p", f"Your prompt is in file {ctx.prompt_path}. Read it and follow it accurately."])
        return cmd

    def parse_output(self, ctx: RunContext, output_file: str) -> ParsedRun:
        state_deltas: list[dict] = []
        malformed: list[str] = []
        has_tool_use = False
        result_obj: dict | None = None
        try:
            with open(output_file) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") == "result" or "total_cost_usd" in obj:
                        result_obj = obj
                    if obj.get("type") == "assistant":
                        for block in obj.get("message", {}).get("content", []):
                            btype = block.get("type")
                            if btype == "tool_use":
                                has_tool_use = True
                            elif btype == "text":
                                text = block.get("text", "")
                                for match in _DELTA_RE.findall(text):
                                    snippet = match.strip()
                                    try:
                                        state_deltas.append(json.loads(snippet))
                                    except json.JSONDecodeError:
                                        malformed.append(snippet[:200])
        except OSError as exc:
            log.warning("ClaudeBackend.parse_output: could not read %s: %s", output_file, exc)

        if result_obj is None:
            return ParsedRun(
                tokens_in=None, tokens_out=None, cached_in=None,
                model_used=None, cost_usd=None,
                state_deltas=state_deltas, malformed_state_deltas=malformed,
                has_tool_use=has_tool_use, unexpected_events=[],
            )

        model_usage = result_obj.get("modelUsage") or {}
        tokens_in = sum(
            (d.get("inputTokens") or 0) + (d.get("cacheCreationInputTokens") or 0)
            for d in model_usage.values()
        ) if model_usage else None
        tokens_out = sum(
            (d.get("outputTokens") or 0) for d in model_usage.values()
        ) if model_usage else None
        cached_in = sum(
            (d.get("cacheReadInputTokens") or 0) for d in model_usage.values()
        ) if model_usage else None
        model_used = next(iter(model_usage)) if model_usage else None
        cost_usd = result_obj.get("total_cost_usd")

        return ParsedRun(
            tokens_in=tokens_in, tokens_out=tokens_out, cached_in=cached_in,
            model_used=model_used, cost_usd=cost_usd,
            state_deltas=state_deltas, malformed_state_deltas=malformed,
            has_tool_use=has_tool_use, unexpected_events=[],
        )

    def query_quota(self, timeout: float = 15.0) -> dict | None:
        usage_script = SCRIPT_DIR / "claude-usage.py"
        try:
            result = subprocess.run(
                [sys.executable, str(usage_script), "quota"],
                capture_output=True, text=True, timeout=timeout,
            )
            if result.returncode != 0:
                log.warning("claude-usage.py quota failed (rc=%d): %s",
                            result.returncode, result.stderr.strip())
                return None
            return json.loads(result.stdout)
        except subprocess.TimeoutExpired:
            log.warning("claude-usage.py quota timed out")
        except json.JSONDecodeError as exc:
            log.warning("claude-usage.py quota returned invalid JSON: %s", exc)
        except Exception as exc:
            log.warning("claude-usage.py quota error: %s", exc)
        return None

    def preflight(self) -> None:
        # No-op. Missing `claude` binary surfaces at first job via FileNotFoundError.
        return None

    def validate_agents(self, agents_dir: Path) -> None:
        for agent_file in sorted(agents_dir.glob("*.md")):
            fm, _ = parse_frontmatter(agent_file.read_text())
            try:
                pick(self.name, fm, agent_name=agent_file.stem, agent_file=str(agent_file))
            except PromptBuildError as exc:
                log.error("Agent validation failed: %s", exc)
                raise
