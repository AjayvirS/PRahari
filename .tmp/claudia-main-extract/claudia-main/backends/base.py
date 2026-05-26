"""Shared dataclasses for backend strategies and runner.

These types are imported by both the runner (backends/runner.py) and
each backend (backends/claude.py, backends/codex.py). Keeping them in
their own module avoids circular imports.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RunContext:
    """Inputs the runner passes to backend.build_command / backend.parse_output."""

    prompt_path: str
    cwd: str
    model: str
    effort_or_turns: str | int | None  # str for codex ("xhigh"), int|None for claude (max_turns)


@dataclass
class ParsedRun:
    """Output of backend.parse_output — backend-agnostic shape for the worker."""

    tokens_in: int | None
    tokens_out: int | None
    cached_in: int | None
    model_used: str | None
    cost_usd: float | None
    state_deltas: list[dict] = field(default_factory=list)
    malformed_state_deltas: list[str] = field(default_factory=list)
    has_tool_use: bool = False
    unexpected_events: list[str] = field(default_factory=list)

    @property
    def state_delta(self) -> dict | None:
        return self.state_deltas[-1] if self.state_deltas else None


@dataclass
class RunResult:
    """Output of backends.run_with_heartbeat.

    `ctx` is returned so the caller can feed it back into
    backend.parse_output without re-constructing it.
    """

    exit_code: int
    ctx: RunContext
