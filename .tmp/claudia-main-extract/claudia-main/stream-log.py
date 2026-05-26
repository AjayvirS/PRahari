#!/usr/bin/env python3
"""Parse Claude Code --output-format stream-json into human-readable logs.

Reads JSONL from stdin, writes human-readable logs to stderr, and passes
through raw JSONL to stdout (for capture to a file).

Usage:
    claude -p "..." --output-format stream-json | python3 stream-log.py > output.jsonl
"""

import json
import sys
import time

# ANSI colors
DIM = "\033[2m"
BOLD = "\033[1m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
RED = "\033[31m"
MAGENTA = "\033[35m"
RESET = "\033[0m"


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def main() -> None:
    start = time.monotonic()
    current_tool = None
    current_tool_input = ""
    assistant_text = ""
    turn_count = 0

    for raw_line in sys.stdin:
        # Pass through raw JSONL to stdout
        sys.stdout.write(raw_line)
        sys.stdout.flush()

        line = raw_line.strip()
        if not line:
            continue

        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg_type = obj.get("type", "")

        if msg_type == "system":
            # System message (init)
            subtype = obj.get("subtype", "")
            if subtype == "init":
                session_id = obj.get("session_id", "?")
                tools = obj.get("tools", [])
                log(f"{DIM}── Session {session_id} | {len(tools)} tools ──{RESET}")

        elif msg_type == "assistant":
            turn_count += 1
            elapsed = time.monotonic() - start
            content = obj.get("message", {}).get("content", [])

            for block in content:
                btype = block.get("type", "")

                if btype == "text":
                    text = block.get("text", "")
                    if text.strip():
                        # Truncate long text output
                        preview = text.strip()
                        if len(preview) > 200:
                            preview = preview[:200] + "..."
                        log(f"{DIM}[{elapsed:5.0f}s]{RESET} {CYAN}💬{RESET} {preview}")

                elif btype == "tool_use":
                    tool_name = block.get("name", "?")
                    tool_input = block.get("input", {})

                    if tool_name == "Bash":
                        cmd = tool_input.get("command", "")
                        desc = tool_input.get("description", "")
                        label = desc if desc else (cmd[:80] + "..." if len(cmd) > 80 else cmd)
                        log(f"{DIM}[{elapsed:5.0f}s]{RESET} {YELLOW}⚡ bash:{RESET} {label}")

                    elif tool_name == "Read":
                        path = tool_input.get("file_path", "?")
                        # Shorten path
                        short = path.split("/")[-1] if "/" in path else path
                        log(f"{DIM}[{elapsed:5.0f}s]{RESET} {GREEN}📖 read:{RESET} {short}")

                    elif tool_name == "Edit":
                        path = tool_input.get("file_path", "?")
                        short = path.split("/")[-1] if "/" in path else path
                        log(f"{DIM}[{elapsed:5.0f}s]{RESET} {MAGENTA}✏️  edit:{RESET} {short}")

                    elif tool_name == "Write":
                        path = tool_input.get("file_path", "?")
                        short = path.split("/")[-1] if "/" in path else path
                        log(f"{DIM}[{elapsed:5.0f}s]{RESET} {MAGENTA}📝 write:{RESET} {short}")

                    elif tool_name == "Glob":
                        pattern = tool_input.get("pattern", "?")
                        log(f"{DIM}[{elapsed:5.0f}s]{RESET} {GREEN}🔍 glob:{RESET} {pattern}")

                    elif tool_name == "Grep":
                        pattern = tool_input.get("pattern", "?")
                        log(f"{DIM}[{elapsed:5.0f}s]{RESET} {GREEN}🔎 grep:{RESET} {pattern}")

                    elif tool_name == "Task":
                        desc = tool_input.get("description", "")
                        agent = tool_input.get("subagent_type", "?")
                        log(f"{DIM}[{elapsed:5.0f}s]{RESET} {RED}🤖 agent:{RESET} {agent} — {desc}")

                    elif tool_name == "Skill":
                        skill = tool_input.get("skill", "?")
                        log(f"{DIM}[{elapsed:5.0f}s]{RESET} {CYAN}🎯 skill:{RESET} {skill}")

                    else:
                        log(f"{DIM}[{elapsed:5.0f}s]{RESET} {YELLOW}🔧 {tool_name}{RESET}")

        elif msg_type == "tool_result":
            # Tool results — just note errors
            content = obj.get("content", "")
            is_error = obj.get("is_error", False)
            if is_error:
                preview = str(content)[:150]
                elapsed = time.monotonic() - start
                log(f"{DIM}[{elapsed:5.0f}s]{RESET} {RED}❌ error:{RESET} {preview}")

        elif msg_type == "result":
            # Final result
            elapsed = time.monotonic() - start
            cost = obj.get("total_cost_usd", 0)
            turns = obj.get("num_turns", 0)
            duration_ms = obj.get("duration_ms", 0)

            model_usage = obj.get("modelUsage", {})
            total_in = sum(
                d.get("inputTokens", 0) + d.get("cacheCreationInputTokens", 0)
                for d in model_usage.values()
            )
            total_out = sum(d.get("outputTokens", 0) for d in model_usage.values())

            log(f"\n{BOLD}{'═' * 60}{RESET}")
            log(f"{BOLD}Run complete{RESET} — {turns} turns, {elapsed:.0f}s wall, {duration_ms/1000:.0f}s API")
            log(f"  Cost: ${cost:.2f} | Tokens: ↑{fmt_tokens(total_in)} ↓{fmt_tokens(total_out)}")
            for model, usage in model_usage.items():
                model_short = model.split("/")[-1] if "/" in model else model
                m_in = usage.get("inputTokens", 0) + usage.get("cacheCreationInputTokens", 0)
                m_out = usage.get("outputTokens", 0)
                m_cost = usage.get("costUSD", 0)
                log(f"  {DIM}{model_short}: ↑{fmt_tokens(m_in)} ↓{fmt_tokens(m_out)} ${m_cost:.2f}{RESET}")
            log(f"{BOLD}{'═' * 60}{RESET}")


if __name__ == "__main__":
    main()
