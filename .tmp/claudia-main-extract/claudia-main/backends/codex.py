"""CodexBackend — wraps `codex exec --json` for Claudia.

Flags pinned for fail-loud + minimal tool surface:
  --ignore-user-config         skip ~/.codex/config.toml
  --ignore-rules               skip .rules execpolicy files
  --ephemeral                  don't persist session under CODEX_HOME
  -c web_search="disabled"     turn off web search (correct form since 0.130.0)
  --dangerously-bypass-approvals-and-sandbox
  --skip-git-repo-check

Pinned CLI version: 0.133.0. preflight() exits 1 on mismatch.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from backends.base import ParsedRun, RunContext
from backends.frontmatter import PromptBuildError, parse_frontmatter, pick

log = logging.getLogger("claudia.backends.codex")

SCRIPT_DIR = Path(__file__).resolve().parent.parent
EXPECTED_CODEX_VERSION = "codex-cli 0.133.0"
_DELTA_RE = re.compile(r"```state_delta\s*\n(.*?)\n```", re.DOTALL)
_EXPECTED_ITEM_TYPES = {"command_execution", "agent_message", "file_change"}
# Codex internal item types that are NOT tool use and NOT capability drift —
# safe to ignore for `unexpected_events`. The drift gate is there to catch new
# tool surfaces (mcp_tool_call, web_search_request, etc.), not to flag every
# internal codex event.
#   - `todo_list`: the model's internal task tracker; appears routinely on
#     complex multi-step agents (smoke confirmed for pr-feedback-handler and
#     pr-hygiene-checker on 0.133.0).
#   - `error`: a sub-command (command_execution) reported a non-zero exit or
#     similar failure. The model handles these natively — observed in first
#     production review where 11 errors fired in one second during exploration
#     but the review still completed cleanly with a valid state_delta.
_IGNORED_ITEM_TYPES = {"todo_list", "error"}


def _default_prices_path() -> Path:
    override = os.getenv("CLAUDIA_CODEX_PRICES")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent / "codex_prices.json"


class CodexBackend:
    name = "codex"
    log_formatter_script = str(SCRIPT_DIR / "backends" / "codex_stream_log.py")
    requires_delta_for_success = True

    def __init__(self, prices_path: Path | None = None):
        self._prices_path = Path(prices_path) if prices_path else _default_prices_path()
        self._prices = self._load_prices()
        self._unknown_model_warned: set[str] = set()
        self._first_run_logged = False
        self._cli_version: str | None = None

    def _load_prices(self) -> dict:
        try:
            with open(self._prices_path) as f:
                data = json.load(f)
            if not isinstance(data, dict):
                log.warning("Codex prices file %s is not a JSON object; ignoring", self._prices_path)
                return {}
            return data
        except FileNotFoundError:
            log.warning("Codex prices file %s missing; cost_usd will be None", self._prices_path)
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Could not load codex prices %s: %s", self._prices_path, exc)
            return {}

    def build_command(self, ctx: RunContext) -> list[str]:
        return [
            os.getenv("CODEX_BIN") or "codex",
            "exec",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "--ignore-user-config",
            "--ignore-rules",
            "--ephemeral",
            "-c", 'web_search="disabled"',
            "-C", ctx.cwd,
            "-m", ctx.model,
            "-c", f'model_reasoning_effort="{ctx.effort_or_turns}"',
            "--json",
            f"Your prompt is in file {ctx.prompt_path}. Read it and follow it accurately.",
        ]

    def parse_output(self, ctx: RunContext, output_file: str) -> ParsedRun:
        input_tokens = 0
        cached_input_tokens = 0
        output_tokens = 0
        reasoning_output_tokens = 0
        state_deltas: list[dict] = []
        malformed: list[str] = []
        has_tool_use = False
        unexpected: list[str] = []

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
                    t = obj.get("type")
                    if t == "turn.completed":
                        usage = obj.get("usage") or {}
                        input_tokens += int(usage.get("input_tokens") or 0)
                        cached_input_tokens += int(usage.get("cached_input_tokens") or 0)
                        output_tokens += int(usage.get("output_tokens") or 0)
                        reasoning_output_tokens += int(usage.get("reasoning_output_tokens") or 0)
                    elif t == "item.completed":
                        item = obj.get("item") or {}
                        itype = item.get("type")
                        if itype == "agent_message":
                            text = item.get("text") or ""
                            for match in _DELTA_RE.findall(text):
                                snippet = match.strip()
                                try:
                                    state_deltas.append(json.loads(snippet))
                                except json.JSONDecodeError:
                                    malformed.append(snippet[:200])
                        elif itype in ("command_execution", "file_change"):
                            has_tool_use = True
                        elif itype in _IGNORED_ITEM_TYPES:
                            pass
                        elif itype is not None:
                            unexpected.append(itype)
        except OSError as exc:
            log.warning("CodexBackend.parse_output: could not read %s: %s", output_file, exc)

        # Invariant: cached_input_tokens MUST NOT exceed input_tokens.
        if cached_input_tokens > input_tokens:
            log.warning(
                "Codex token invariant violated (cached=%d > input=%d). "
                "Token counts and cost discarded; possible codex schema change.",
                cached_input_tokens, input_tokens,
            )
            tokens_in: int | None = None
            cost_usd: float | None = None
        elif input_tokens == 0 and output_tokens == 0:
            # No turn.completed seen — treat as missing usage.
            tokens_in = None
            cost_usd = None
        else:
            tokens_in = input_tokens - cached_input_tokens
            cost_usd = self._compute_cost(ctx.model, tokens_in, output_tokens, cached_input_tokens)

        if not self._first_run_logged and tokens_in is not None:
            log.info(
                "Codex first-run usage: input=%d cached=%d output=%d reasoning_out=%d cli_version=%s",
                input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens,
                self._cli_version or "unknown",
            )
            self._first_run_logged = True

        return ParsedRun(
            tokens_in=tokens_in,
            tokens_out=output_tokens if (input_tokens or output_tokens) else None,
            cached_in=cached_input_tokens if (input_tokens or output_tokens) else None,
            model_used=ctx.model,
            cost_usd=cost_usd,
            state_deltas=state_deltas,
            malformed_state_deltas=malformed,
            has_tool_use=has_tool_use,
            unexpected_events=unexpected,
        )

    def _compute_cost(self, model: str, tokens_in: int | None,
                      tokens_out: int, cached_in: int) -> float | None:
        rates = self._prices.get(model)
        if rates is None:
            if model not in self._unknown_model_warned:
                log.warning("No codex price entry for model %r; cost_usd will be None", model)
                self._unknown_model_warned.add(model)
            return None
        if tokens_in is None:
            return None
        return (
            tokens_in        * rates.get("input_per_mtok", 0) +
            tokens_out       * rates.get("output_per_mtok", 0) +
            (cached_in or 0) * rates.get("cache_read_per_mtok", 0)
        ) / 1_000_000

    def query_quota(self, timeout: float = 15.0):
        import signal
        import subprocess
        import time as _time

        cmd = [
            os.getenv("CODEX_BIN") or "codex",
            "-s", "read-only",
            "-a", "untrusted",
            "app-server",
        ]
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                start_new_session=True,
            )
        except (FileNotFoundError, OSError) as exc:
            log.warning("codex app-server spawn failed: %s", exc)
            return None

        deadline = _time.monotonic() + timeout

        def _kill_child():
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

        def _send(obj):
            try:
                proc.stdin.write(json.dumps(obj) + "\n")
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise RuntimeError(f"app-server stdin closed: {exc}")

        def _read_response(req_id: int) -> dict | None:
            """Read until we see a response matching req_id; skip notifications."""
            while True:
                remaining = deadline - _time.monotonic()
                if remaining <= 0:
                    return None
                import select
                ready, _, _ = select.select([proc.stdout], [], [], remaining)
                if not ready:
                    return None
                line = proc.stdout.readline()
                if not line:
                    return None
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("codex app-server returned non-JSON line: %s", line[:200])
                    return None
                # Skip notifications (no `id` field).
                if "id" not in obj:
                    continue
                if obj.get("id") != req_id:
                    continue
                return obj

        try:
            _send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"capabilities": {},
                              "clientInfo": {"name": "claudia", "version": "1.0.0"}}})
            init_resp = _read_response(1)
            if init_resp is None or "error" in init_resp:
                log.warning("codex initialize failed: %s", init_resp)
                return None
            _send({"jsonrpc": "2.0", "method": "initialized", "params": {}})

            _send({"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read", "params": {}})
            resp = _read_response(2)
            if resp is None or "error" in resp:
                log.warning("codex account/rateLimits/read failed: %s", resp)
                return None
            # codex-cli 0.133.0 nests windows under `rateLimits` and exposes
            # per-limit-id breakdowns under `rateLimitsByLimitId`. Older codex
            # had `primary`/`secondary` at the top level — fall back to that
            # shape so a downgrade doesn't silently null out the quota.
            result = resp.get("result") or {}
            rate_limits = result.get("rateLimits") or {}
            primary = rate_limits.get("primary") or result.get("primary") or {}
            secondary = rate_limits.get("secondary") or result.get("secondary") or {}
            return {
                "session": self._format_window(primary),
                "weekly": self._format_window(secondary),
            }
        except RuntimeError as exc:
            log.warning("codex quota error: %s", exc)
            return None
        finally:
            _kill_child()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

    @staticmethod
    def _format_window(window: dict) -> dict:
        from datetime import datetime, timezone
        used = float(window.get("usedPercent") or 0.0)
        resets_at_epoch = window.get("resetsAt")
        if resets_at_epoch:
            dt = datetime.fromtimestamp(int(resets_at_epoch), tz=timezone.utc)
            resets_at = dt.isoformat()
            secs = int(resets_at_epoch) - int(datetime.now(tz=timezone.utc).timestamp())
            if secs < 0:
                resets_in = "now"
            elif secs < 3600:
                resets_in = f"{secs // 60}m"
            elif secs < 86400:
                resets_in = f"{secs // 3600}h"
            else:
                resets_in = f"{secs // 86400}d"
        else:
            resets_at = None
            resets_in = "?"
        return {
            "used_pct": used,
            "remaining_pct": round(100.0 - used, 1),
            "resets_at": resets_at,
            "resets_in": resets_in,
        }

    def preflight(self) -> None:
        import subprocess
        import time as _time

        DEADLINE = 15.0
        start = _time.monotonic()

        def remaining() -> float:
            return DEADLINE - (_time.monotonic() - start)

        codex_bin = os.getenv("CODEX_BIN") or "codex"

        rem = remaining()
        if rem <= 0:
            log.error("codex preflight exceeded 15s budget at version step")
            raise SystemExit(1)
        try:
            ver_out = subprocess.run(
                [codex_bin, "--version"],
                capture_output=True, text=True,
                timeout=min(3.0, rem), check=True,
            )
            self._cli_version = ver_out.stdout.strip()
        except Exception as exc:
            log.error("codex --version failed: %s", exc)
            raise SystemExit(1)

        if self._cli_version != EXPECTED_CODEX_VERSION:
            log.error(
                "Codex version mismatch: expected %r, got %r. "
                "Pin via `npm install -g @openai/codex@0.133.0`.",
                EXPECTED_CODEX_VERSION, self._cli_version,
            )
            raise SystemExit(1)

        rem = remaining()
        if rem <= 0:
            log.error("codex preflight exceeded 15s budget before features list")
            raise SystemExit(1)
        try:
            feat_out = subprocess.run(
                [codex_bin, "features", "list"],
                capture_output=True, text=True,
                timeout=min(3.0, rem), check=True,
            )
        except Exception as exc:
            log.error("codex features list failed: %s", exc)
            raise SystemExit(1)
        log.info("codex version: %s", self._cli_version)
        log.info("codex features:\n%s", feat_out.stdout.strip())

        rem = remaining()
        if rem <= 0:
            log.error("codex preflight exceeded 15s budget before quota check")
            raise SystemExit(1)

        quota = self.query_quota(timeout=rem)
        if quota is None:
            log.error("codex preflight failed: query_quota returned None within budget")
            raise SystemExit(1)
        sess = quota.get("session", {})
        weekly = quota.get("weekly", {})
        log.info(
            "Codex preflight ok: version=%s, session=%s%% used (resets %s), weekly=%s%% used (resets %s)",
            self._cli_version,
            sess.get("used_pct", "?"), sess.get("resets_in", "?"),
            weekly.get("used_pct", "?"), weekly.get("resets_in", "?"),
        )

    def validate_agents(self, agents_dir: Path) -> None:
        for agent_file in sorted(agents_dir.glob("*.md")):
            fm, _ = parse_frontmatter(agent_file.read_text())
            try:
                pick(self.name, fm, agent_name=agent_file.stem, agent_file=str(agent_file))
            except PromptBuildError as exc:
                log.error("Agent validation failed: %s", exc)
                raise
