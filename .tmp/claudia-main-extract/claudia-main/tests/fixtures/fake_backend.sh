#!/usr/bin/env bash
# Fake LLM backend for run_with_heartbeat integration tests.
#
# Behavior controlled by env vars:
#   FAKE_OUTPUT  — printf format string emitted to stdout, then newline.
#   EXIT_CODE    — integer exit code (default 0).
#   SLEEP_BEFORE — seconds to sleep before emitting output (default 0).
#
# Also asserts stdin is closed (DEVNULL) — if stdin has data or stays open it
# exits 99 to fail the test.

set -u

# Stdin should be DEVNULL → select() on fd 0 returns immediately (EOF),
# and sysread gives 0 bytes. Use Python (always present) for portability
# across macOS (no GNU timeout) and Linux.
python3 - <<'PYEOF'
import sys, select
try:
    r, _, _ = select.select([sys.stdin], [], [], 2.0)
    if r:
        data = sys.stdin.buffer.read1(4096)
        if data:
            print(f"fake_backend: stdin had data ({len(data)} bytes)", file=sys.stderr)
            sys.exit(99)
        # EOF with 0 bytes — DEVNULL confirmed.
    else:
        # Timeout: stdin was open but no EOF arrived.
        print("fake_backend: stdin NOT closed (timeout)", file=sys.stderr)
        sys.exit(99)
except Exception as exc:
    print(f"fake_backend: stdin check error: {exc}", file=sys.stderr)
    sys.exit(99)
PYEOF

if [ $? -ne 0 ]; then
    exit 99
fi

sleep "${SLEEP_BEFORE:-0}"
printf '%s\n' "${FAKE_OUTPUT:-{\"type\":\"noop\"\}}"
exit "${EXIT_CODE:-0}"
