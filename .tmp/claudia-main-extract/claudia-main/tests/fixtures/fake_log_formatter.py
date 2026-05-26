#!/usr/bin/env python3
"""Identity log formatter for run_with_heartbeat tests.

Behavior controlled by FAKE_LOG_MODE env var:
  "identity" (default) — copy stdin to stdout line by line, exit 0.
  "crash_after_one"    — read one line, write it, exit 1.
"""
import os
import sys


def main() -> int:
    mode = os.environ.get("FAKE_LOG_MODE", "identity")
    if mode == "crash_after_one":
        line = sys.stdin.readline()
        sys.stdout.write(line)
        sys.stdout.flush()
        return 1
    for line in sys.stdin:
        sys.stdout.write(line)
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
