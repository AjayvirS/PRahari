"""Backend strategy package — pluggable LLM driver.

Public API (re-exported here for convenience):
  - Backend (Protocol)
  - get_backend(name) -> Backend
  - RunContext, ParsedRun, RunResult dataclasses
  - run_with_heartbeat
  - PromptBuildError
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

from backends.base import ParsedRun, RunContext, RunResult
from backends.frontmatter import PromptBuildError, parse_frontmatter, pick
from backends.runner import HeartbeatThread, run_with_heartbeat


class Backend(Protocol):
    name: str
    log_formatter_script: str
    requires_delta_for_success: bool

    def build_command(self, ctx: RunContext) -> list[str]: ...

    def parse_output(self, ctx: RunContext, output_file: str) -> ParsedRun:
        """MUST be total. Never raises on missing file / malformed JSON / missing field."""

    def query_quota(self, timeout: float = 15.0) -> dict | None:
        """Return {'session': {...}, 'weekly': {...}} or None."""

    def preflight(self) -> None:
        """Called once at worker startup. Raises SystemExit(1) on fatal config."""

    def validate_agents(self, agents_dir: Path) -> None:
        """Validate every agents/*.md frontmatter. Raises PromptBuildError on bad agent."""


def get_backend(name: str) -> Backend:
    if name == "claude":
        from backends.claude import ClaudeBackend
        return ClaudeBackend()
    if name == "codex":
        from backends.codex import CodexBackend
        return CodexBackend()
    raise ValueError(f"Unknown backend: {name!r}. Expected 'claude' or 'codex'.")


__all__ = [
    "Backend",
    "get_backend",
    "HeartbeatThread",
    "ParsedRun",
    "PromptBuildError",
    "RunContext",
    "RunResult",
    "parse_frontmatter",
    "pick",
    "run_with_heartbeat",
]
