"""Single source of truth for agent frontmatter parsing and per-backend field pick.

`PromptBuildError` lives here exactly once. Any other module that needs to
catch it imports from here — two class objects would break `except
PromptBuildError` checks (Python compares classes by identity, not name).
"""
from __future__ import annotations


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Parse YAML-like frontmatter; return (metadata, body).

    Mirrors worker.py:262 _parse_agent_frontmatter and
    inline_agents.py:68 _parse_frontmatter exactly.
    """
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    fm_raw = text[3:end].strip()
    body = text[end + 4:].lstrip("\n")
    fm: dict[str, str] = {}
    for line in fm_raw.split("\n"):
        line = line.strip()
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip()
    return fm, body


class PromptBuildError(Exception):
    """Raised when an agent file is missing a required backend field."""

    def __init__(self, *, backend: str, agent_name: str, agent_file: str, missing: str):
        self.backend = backend
        self.agent_name = agent_name
        self.agent_file = agent_file
        self.missing = missing
        super().__init__(
            f"agent '{agent_name}' ({agent_file}) missing required field "
            f"'{missing}' for backend '{backend}'"
        )


def pick(
    backend_name: str,
    fm: dict[str, str],
    *,
    agent_name: str,
    agent_file: str,
) -> tuple[str, str | int | None]:
    """Return (model, effort_or_turns) for the active backend.

    codex → (codex_model, codex_effort) — both required.
    claude → (model, int(max_turns) | None) — max_turns optional.
    """
    if backend_name == "codex":
        for required in ("codex_model", "codex_effort"):
            if required not in fm:
                raise PromptBuildError(
                    backend=backend_name,
                    agent_name=agent_name,
                    agent_file=agent_file,
                    missing=required,
                )
        return fm["codex_model"], fm["codex_effort"]

    if "model" not in fm:
        raise PromptBuildError(
            backend=backend_name,
            agent_name=agent_name,
            agent_file=agent_file,
            missing="model",
        )
    max_turns = int(fm["max_turns"]) if "max_turns" in fm else None
    return fm["model"], max_turns
