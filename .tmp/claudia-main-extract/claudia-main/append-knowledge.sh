#!/usr/bin/env bash
# Append a validated knowledge entry to a JSONL file.
# Usage: MEMORIES_DIR=<root> bash append-knowledge.sh <file> <date> <source> <pattern>
#
# Requires MEMORIES_DIR env var to be set (the trusted root for knowledge files).
#
# Validates:
#   - MEMORIES_DIR is set and is an existing directory
#   - Target directory exists (created by run.py's setup_directories)
#   - Resolved file path is anchored under MEMORIES_DIR
#   - File is an allowlisted JSONL filename
#   - Directory structure is .../knowledge/<repo-slug>/<file>
#   - Date matches YYYY-MM-DD format
#   - Source matches "PR #N" or "issue #N" pattern
#   - Pattern: strips markdown/URLs/HTML, truncates to 200 chars
#
# Silently exits 1 on validation failure (never corrupts files).

set -euo pipefail

if [[ $# -ne 4 ]]; then
  echo "Usage: MEMORIES_DIR=<root> $0 <file> <date> <source> <pattern>" >&2
  exit 1
fi

FILE="$1"
DATE="$2"
SOURCE="$3"
PATTERN="$4"

# Validate MEMORIES_DIR is set and exists
if [[ -z "${MEMORIES_DIR:-}" ]]; then
  echo "Rejected: MEMORIES_DIR env var is not set" >&2
  exit 1
fi
if [[ ! -d "$MEMORIES_DIR" ]]; then
  echo "Rejected: MEMORIES_DIR does not exist: $MEMORIES_DIR" >&2
  exit 1
fi
REAL_MEMORIES_DIR=$(realpath "$MEMORIES_DIR")

# Allowlisted filenames
ALLOWED_FILES=("coding-patterns.jsonl" "review-lessons.jsonl" "common-mistakes.jsonl" "tooling-notes.jsonl")
BASENAME=$(basename "$FILE")
ALLOWED=false
for af in "${ALLOWED_FILES[@]}"; do
  if [[ "$BASENAME" == "$af" ]]; then
    ALLOWED=true
    break
  fi
done
if [[ "$ALLOWED" != "true" ]]; then
  echo "Rejected: filename '$BASENAME' not in allowlist" >&2
  exit 1
fi

# Resolve the target directory (must exist — created by run.py setup_directories)
FILE_DIR=$(dirname "$FILE")
if [[ ! -d "$FILE_DIR" ]]; then
  echo "Rejected: target directory does not exist: $FILE_DIR" >&2
  exit 1
fi

# Canonicalize the existing directory (no -m needed since dir exists)
REAL_DIR=$(realpath "$FILE_DIR")

# Anchor check: resolved path must be under MEMORIES_DIR
if [[ "$REAL_DIR" != "$REAL_MEMORIES_DIR" && "$REAL_DIR" != "$REAL_MEMORIES_DIR/"* ]]; then
  echo "Rejected: path $REAL_DIR is outside MEMORIES_DIR $REAL_MEMORIES_DIR" >&2
  exit 1
fi

# Enforce strict directory structure: .../knowledge/<repo-slug>/<allowed-file>
KNOWLEDGE_DIR=$(dirname "$REAL_DIR")
KNOWLEDGE_BASENAME=$(basename "$KNOWLEDGE_DIR")
if [[ "$KNOWLEDGE_BASENAME" != "knowledge" ]]; then
  echo "Rejected: file not in a */knowledge/<repo-slug>/ directory (got: $REAL_DIR)" >&2
  exit 1
fi

# Validate date format (YYYY-MM-DD)
if ! [[ "$DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
  echo "Rejected: invalid date format '$DATE'" >&2
  exit 1
fi

# Validate source format (PR #N or issue #N)
if ! [[ "$SOURCE" =~ ^(PR|issue)\ #[0-9]+$ ]]; then
  echo "Rejected: invalid source format '$SOURCE'" >&2
  exit 1
fi

# Sanitize pattern: strip markdown, URLs, HTML
PATTERN=$(echo "$PATTERN" | sed 's/[#*`\[\]()]//g; s|https\?://[^ ]*||g; s/<[^>]*>//g')

# Truncate to 200 chars
PATTERN="${PATTERN:0:200}"

# Validate non-empty after sanitization
if [[ -z "$PATTERN" ]]; then
  echo "Rejected: pattern is empty after sanitization" >&2
  exit 1
fi

# Build JSON entry using jq (safe serialization) and append
ENTRY=$(jq -nc --arg d "$DATE" --arg s "$SOURCE" --arg p "$PATTERN" '{date:$d,source:$s,pattern:$p}')
echo "$ENTRY" >> "$REAL_DIR/$BASENAME"
