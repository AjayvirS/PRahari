"""PR review orchestration and deterministic fallback generation."""
from __future__ import annotations

from collections import Counter
from typing import Any

from app.logging_config import get_logger
from app.review_service import (
    ReviewGenerator,
    ReviewInput,
    ReviewSections,
    build_review_generator,
)

logger = get_logger(__name__)


async def build_review_comment(
    pull_request: dict[str, Any],
    changed_files: list[dict[str, Any]],
    *,
    head_sha: str,
    generator: ReviewGenerator | None = None,
) -> str:
    """Build a structured top-level PR comment with LLM and fallback support."""
    review_input = _build_review_input(pull_request, changed_files, head_sha=head_sha)
    review_generator = generator if generator is not None else build_review_generator()

    if review_generator is not None:
        try:
            generated = await review_generator.generate(review_input)
            return _format_review_comment(generated)
        except Exception:
            logger.exception(
                "reviewer.build_review_comment.generator_failed",
                pr_number=pull_request.get("number"),
                head_sha=head_sha,
            )

    try:
        generated = _build_structured_review_sections(review_input)
        return _format_review_comment(generated)
    except Exception:
        logger.exception(
            "reviewer.build_review_comment.fallback_failed",
            pr_number=pull_request.get("number"),
            head_sha=head_sha,
        )
        return build_placeholder_review_comment(head_sha)


def build_placeholder_review_comment(head_sha: str) -> str:
    """Return the placeholder comment body."""
    return f"Review pipeline connected successfully for this PR head SHA {head_sha}"


def _build_review_input(
    pull_request: dict[str, Any],
    changed_files: list[dict[str, Any]],
    *,
    head_sha: str,
) -> ReviewInput:
    return ReviewInput(
        pr_number=pull_request.get("number"),
        title=str(pull_request.get("title") or "Untitled PR"),
        body=str(pull_request.get("body") or ""),
        additions=int(pull_request.get("additions") or 0),
        deletions=int(pull_request.get("deletions") or 0),
        changed_files=[str(file_info["filename"]) for file_info in changed_files],
        head_sha=head_sha,
    )


def _build_structured_review_sections(review_input: ReviewInput) -> ReviewSections:
    areas = _summarize_areas(review_input.changed_files)
    findings = _derive_findings(
        review_input.changed_files,
        review_input.additions,
        len(review_input.changed_files),
    )
    questions = _derive_questions(review_input.changed_files)

    summary = (
        f"{review_input.title}. This PR touches {len(review_input.changed_files)} file(s) "
        f"with {review_input.additions} addition(s) and {review_input.deletions} deletion(s)."
    )
    if areas:
        summary = f"{summary} Primary areas: {areas}."

    return ReviewSections(
        summary=summary,
        findings=findings,
        open_questions=questions,
    )


def _format_review_comment(sections: ReviewSections) -> str:
    lines = [
        "PRahari review summary",
        "",
        "Summary",
        f"- {sections.summary}",
    ]
    lines.extend(_format_section("Potential findings", sections.findings))
    lines.extend(_format_section("Open questions", sections.open_questions))
    return "\n".join(lines)


def _format_section(title: str, items: list[str]) -> list[str]:
    lines = ["", title]
    if items:
        lines.extend(f"- {item}" for item in items[:3])
    else:
        lines.append("- None.")
    return lines


def _summarize_areas(changed_files: list[str]) -> str:
    areas: list[str] = []
    for filename in changed_files:
        parts = filename.split("/", maxsplit=1)
        areas.append(parts[0] if len(parts) > 1 else filename)

    most_common = [name for name, _ in Counter(areas).most_common(3)]
    return ", ".join(most_common)


def _derive_findings(
    changed_files: list[str], additions: int, file_count: int
) -> list[str]:
    findings: list[str] = []

    if changed_files and not any("test" in filename.lower() for filename in changed_files):
        findings.append(
            "No test file changes were detected; verify the affected paths with focused checks."
        )

    if any(
        token in filename.lower()
        for filename in changed_files
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


def _derive_questions(changed_files: list[str]) -> list[str]:
    questions: list[str] = []

    if any(
        token in filename.lower()
        for filename in changed_files
        for token in ("worker", "queue", "review")
    ):
        questions.append("Should retry behavior or stale-job handling change for this path?")

    if any(
        token in filename.lower()
        for filename in changed_files
        for token in ("migration", ".sql", "database")
    ):
        questions.append("Does this schema change require deployment ordering or backfill steps?")

    if any(
        token in filename.lower()
        for filename in changed_files
        for token in ("config", ".env", "docker")
    ):
        questions.append("Are there operator-facing configuration updates that should be documented?")

    return questions[:3]
