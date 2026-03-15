"""Deterministic PR review summary generation."""
from __future__ import annotations

from collections import Counter
from typing import Any

from app.logging_config import get_logger

logger = get_logger(__name__)


def build_review_comment(
    pull_request: dict[str, Any],
    changed_files: list[dict[str, Any]],
    *,
    head_sha: str,
) -> str:
    """Build a structured top-level PR comment, with placeholder fallback."""
    try:
        return _build_structured_review_comment(pull_request, changed_files)
    except Exception:
        logger.exception(
            "reviewer.build_review_comment.failed",
            pr_number=pull_request.get("number"),
            head_sha=head_sha,
        )
        return build_placeholder_review_comment(head_sha)


def build_placeholder_review_comment(head_sha: str) -> str:
    """Return the existing placeholder comment body."""
    return f"Review pipeline connected successfully for this PR head SHA {head_sha}"


def _build_structured_review_comment(
    pull_request: dict[str, Any], changed_files: list[dict[str, Any]]
) -> str:
    title = str(pull_request.get("title") or "Untitled PR")
    file_count = len(changed_files)
    additions = int(pull_request.get("additions") or 0)
    deletions = int(pull_request.get("deletions") or 0)
    areas = _summarize_areas(changed_files)
    findings = _derive_findings(changed_files, additions, file_count)
    questions = _derive_questions(changed_files)

    lines = [
        "PRahari review summary",
        "",
        "Summary",
        (
            f"- {title}. This PR touches {file_count} file(s) "
            f"with {additions} addition(s) and {deletions} deletion(s)."
        ),
    ]

    if areas:
        lines.append(f"- Primary areas: {areas}.")

    lines.extend(_format_section("Potential findings", findings))
    lines.extend(_format_section("Open questions", questions))
    return "\n".join(lines)


def _format_section(title: str, items: list[str]) -> list[str]:
    lines = ["", title]
    if items:
        lines.extend(f"- {item}" for item in items[:3])
    else:
        lines.append("- None.")
    return lines


def _summarize_areas(changed_files: list[dict[str, Any]]) -> str:
    areas: list[str] = []
    for file_info in changed_files:
        filename = file_info["filename"]
        parts = filename.split("/", maxsplit=1)
        areas.append(parts[0] if len(parts) > 1 else filename)

    most_common = [name for name, _ in Counter(areas).most_common(3)]
    return ", ".join(most_common)


def _derive_findings(
    changed_files: list[dict[str, Any]], additions: int, file_count: int
) -> list[str]:
    filenames = [file_info["filename"] for file_info in changed_files]
    findings: list[str] = []

    if filenames and not any("test" in filename.lower() for filename in filenames):
        findings.append(
            "No test file changes were detected; verify the affected paths with focused checks."
        )

    if any(
        token in filename.lower()
        for filename in filenames
        for token in ("migration", ".sql", "config", ".env", "docker")
    ):
        findings.append(
            "Schema or configuration changes are present; confirm rollout and rollback expectations."
        )

    if file_count > 10 or additions > 400:
        findings.append(
            "The change spans a broad surface area; verify integration boundaries carefully."
        )

    return findings[:3]


def _derive_questions(changed_files: list[dict[str, Any]]) -> list[str]:
    filenames = [file_info["filename"] for file_info in changed_files]
    questions: list[str] = []

    if any(token in filename.lower() for filename in filenames for token in ("worker", "queue", "review")):
        questions.append("Should retry behavior or stale-job handling change for this path?")

    if any(token in filename.lower() for filename in filenames for token in ("migration", ".sql", "database")):
        questions.append("Does this schema change require deployment ordering or backfill steps?")

    if any(token in filename.lower() for filename in filenames for token in ("config", ".env", "docker")):
        questions.append("Are there operator-facing configuration updates that should be documented?")

    return questions[:3]

