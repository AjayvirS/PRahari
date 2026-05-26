#!/usr/bin/env python3
"""Render codex --json events into human-readable progress lines on stdout.

Reads JSONL from stdin (codex exec --json emits one JSON object per line);
copies them through to stdout verbatim (so the worker still has the full
event trace for parse_output), and ALSO writes a short summary line per
event to stderr for journalctl visibility.
"""
from __future__ import annotations

import json
import sys


def _summary(obj: dict) -> str | None:
    t = obj.get("type")
    if t == "thread.started":
        return f"codex: thread started ({obj.get('thread_id', '?')})"
    if t == "turn.started":
        return f"codex: turn {obj.get('turn_id', '?')} started"
    if t == "turn.completed":
        usage = obj.get("usage") or {}
        return (
            f"codex: turn completed — "
            f"input={usage.get('input_tokens', 0)} "
            f"cached={usage.get('cached_input_tokens', 0)} "
            f"output={usage.get('output_tokens', 0)} "
            f"reasoning_out={usage.get('reasoning_output_tokens', 0)}"
        )
    if t == "item.started":
        item = obj.get("item") or {}
        return f"codex: item started ({item.get('type', '?')})"
    if t == "item.completed":
        item = obj.get("item") or {}
        itype = item.get("type", "?")
        if itype == "command_execution":
            cmd = (item.get("command") or "")[:80]
            return f"codex: command exec — {cmd}"
        if itype == "file_change":
            path = item.get("path", "?")
            return f"codex: file change — {path}"
        if itype == "agent_message":
            text = (item.get("text") or "")[:80].replace("\n", " ")
            return f"codex: agent message — {text}"
        return f"codex: item completed ({itype})"
    return None


def main() -> int:
    for line in sys.stdin:
        sys.stdout.write(line)
        sys.stdout.flush()
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        s = _summary(obj)
        if s:
            print(s, file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
