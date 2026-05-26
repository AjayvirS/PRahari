"""Integration tests for backends.runner.run_with_heartbeat."""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from backends.base import RunContext, RunResult
from backends.runner import run_with_heartbeat

FIXTURES = Path(__file__).parent / "fixtures"
FAKE_BACKEND = FIXTURES / "fake_backend.sh"
FAKE_LOG = FIXTURES / "fake_log_formatter.py"


class _StubBackend:
    """Minimal Backend-like object for runner tests (no DB, no real parser)."""
    name = "stub"
    log_formatter_script = str(FAKE_LOG)
    requires_delta_for_success = False

    def __init__(self, cmd_factory):
        self._cmd_factory = cmd_factory

    def build_command(self, ctx: RunContext):
        return self._cmd_factory(ctx)


@pytest.fixture(autouse=True)
def _no_db_heartbeat(monkeypatch):
    """Replace HeartbeatThread with a no-op so tests don't need PG."""
    import backends.runner as runner

    class _NoopHB:
        def __init__(self, job_id, interval=60): pass
        def start(self): pass
        def stop(self): pass
        def join(self, timeout=None): pass

    monkeypatch.setattr(runner, "HeartbeatThread", _NoopHB)


def test_runner_returns_runresult_with_ctx(tmp_path):
    out = tmp_path / "out.jsonl"
    backend = _StubBackend(lambda ctx: [str(FAKE_BACKEND)])
    os.environ.update({"FAKE_OUTPUT": '{"type":"noop"}', "EXIT_CODE": "0"})
    try:
        result = run_with_heartbeat(
            backend,
            prompt="hello",
            cwd=str(tmp_path),
            model="m",
            effort_or_turns=None,
            job_id=-1,
            timeout_seconds=10,
            output_file=str(out),
        )
    finally:
        os.environ.pop("FAKE_OUTPUT", None)
        os.environ.pop("EXIT_CODE", None)

    assert isinstance(result, RunResult)
    assert result.exit_code == 0
    assert result.ctx.cwd == str(tmp_path)
    assert result.ctx.model == "m"
    # Prompt path: runner creates it, unlinks in finally.
    assert not Path(result.ctx.prompt_path).exists()
    # Output captured verbatim by the identity log formatter.
    assert '"type":"noop"' in out.read_text()


def test_runner_timeout_kills_children(tmp_path):
    out = tmp_path / "out.jsonl"
    backend = _StubBackend(lambda ctx: [str(FAKE_BACKEND)])
    os.environ.update({"FAKE_OUTPUT": '{"type":"done"}', "SLEEP_BEFORE": "5"})
    try:
        t0 = time.monotonic()
        result = run_with_heartbeat(
            backend,
            prompt="hello",
            cwd=str(tmp_path),
            model="m",
            effort_or_turns=None,
            job_id=-1,
            timeout_seconds=1,
            output_file=str(out),
        )
        elapsed = time.monotonic() - t0
    finally:
        os.environ.pop("FAKE_OUTPUT", None)
        os.environ.pop("SLEEP_BEFORE", None)

    assert result.exit_code == -1
    assert elapsed < 4  # kill, not natural completion at 5s


def test_runner_log_formatter_missing_returns_minus_two(tmp_path, monkeypatch):
    out = tmp_path / "out.jsonl"
    backend = _StubBackend(lambda ctx: [str(FAKE_BACKEND)])
    backend.log_formatter_script = str(tmp_path / "does-not-exist.py")
    result = run_with_heartbeat(
        backend,
        prompt="hello",
        cwd=str(tmp_path),
        model="m",
        effort_or_turns=None,
        job_id=-1,
        timeout_seconds=5,
        output_file=str(out),
    )
    assert result.exit_code == -2
    # LLM was never spawned → output_file is empty (open-for-write only).
    assert out.read_text() == ""


def test_runner_log_formatter_crash_mid_stream(tmp_path):
    out = tmp_path / "out.jsonl"
    backend = _StubBackend(lambda ctx: [str(FAKE_BACKEND)])
    os.environ.update({
        "FAKE_OUTPUT": '{"type":"first"}',
        "EXIT_CODE": "0",
        "FAKE_LOG_MODE": "crash_after_one",
    })
    try:
        result = run_with_heartbeat(
            backend,
            prompt="hello",
            cwd=str(tmp_path),
            model="m",
            effort_or_turns=None,
            job_id=-1,
            timeout_seconds=10,
            output_file=str(out),
        )
    finally:
        os.environ.pop("FAKE_OUTPUT", None)
        os.environ.pop("EXIT_CODE", None)
        os.environ.pop("FAKE_LOG_MODE", None)

    assert result.exit_code == -2


def test_runner_popen_failure_returns_minus_two(tmp_path):
    out = tmp_path / "out.jsonl"
    backend = _StubBackend(lambda ctx: [str(tmp_path / "missing-binary")])
    result = run_with_heartbeat(
        backend,
        prompt="hello",
        cwd=str(tmp_path),
        model="m",
        effort_or_turns=None,
        job_id=-1,
        timeout_seconds=5,
        output_file=str(out),
    )
    assert result.exit_code == -2
