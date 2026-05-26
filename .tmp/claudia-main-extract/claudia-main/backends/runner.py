"""Subprocess plumbing shared by all backends.

Owns process lifecycle: heartbeat thread, stdin=DEVNULL, start_new_session,
timeout-kill of process group, log child reaping, KeyboardInterrupt cleanup.

Import direction: backends.runner MUST NOT import worker. worker imports
from backends.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import db
from backends.base import RunContext, RunResult

log = logging.getLogger("claudia.backends.runner")


def _kill_tree(proc: subprocess.Popen | None) -> None:
    """Send SIGKILL to the entire process group of `proc` (started with
    start_new_session=True)."""
    if proc is None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError):
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


class HeartbeatThread(threading.Thread):
    """Background thread that extends job lease while the LLM is running.

    Owns its own DB connection (opened in run(), closed on stop) so we don't
    share a psycopg2 connection across threads.

    job_id == -1 means "no job in DB" (inline agents, smoke tests); the
    thread runs but skips the DB heartbeat call.
    """

    def __init__(self, job_id: int, interval: int = 60):
        super().__init__(daemon=True)
        self._job_id = job_id
        self._interval = interval
        self._stop_event = threading.Event()
        self._conn = None

    def run(self):
        if self._job_id == -1:
            # No DB row to heartbeat; just sleep until stop().
            self._stop_event.wait()
            return
        try:
            self._conn = db.connect()
        except Exception as exc:
            log.error("Heartbeat could not connect for job %d: %s", self._job_id, exc)
            return
        try:
            while not self._stop_event.wait(self._interval):
                try:
                    db.update_heartbeat(self._conn, self._job_id)
                except Exception as exc:
                    log.warning("Heartbeat failed for job %d: %s — reconnecting",
                                self._job_id, exc)
                    try:
                        self._conn.close()
                    except Exception:
                        pass
                    try:
                        self._conn = db.connect()
                    except Exception as reconnect_exc:
                        log.error("Heartbeat reconnect failed for job %d: %s",
                                  self._job_id, reconnect_exc)
        finally:
            if self._conn:
                try:
                    self._conn.close()
                except Exception:
                    pass

    def stop(self):
        self._stop_event.set()


def run_with_heartbeat(
    backend,
    *,
    prompt: str,
    cwd: str,
    model: str,
    effort_or_turns,
    job_id: int,
    timeout_seconds: int,
    output_file: str,
) -> RunResult:
    """Owns process lifecycle; backend supplies command + log formatter + parser.

    Exit-code contract:
      0..N — LLM process natural exit.
      -1   — Timeout; both children killed.
      -2   — Spawn / setup failure (missing binary, missing log formatter,
             log formatter crashed mid-stream). Output_file may be partial.
    """
    heartbeat = HeartbeatThread(job_id)
    heartbeat.start()
    fd, prompt_path = tempfile.mkstemp(prefix="claudia-prompt-", suffix=".md")
    os.close(fd)
    ctx = RunContext(
        prompt_path=prompt_path, cwd=cwd, model=model, effort_or_turns=effort_or_turns,
    )
    llm_proc = None
    log_proc = None
    try:
        Path(prompt_path).write_text(prompt)
        cmd = backend.build_command(ctx)
        log_path = Path(backend.log_formatter_script)

        try:
            with open(output_file, "w") as fh:
                if not log_path.is_file():
                    log.error("run_with_heartbeat: log formatter missing: %s", log_path)
                    return RunResult(exit_code=-2, ctx=ctx)
                try:
                    llm_proc = subprocess.Popen(
                        cmd, cwd=cwd,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        start_new_session=True,
                    )
                except (FileNotFoundError, OSError) as exc:
                    log.error("run_with_heartbeat: Popen failed (%s): %s",
                              type(exc).__name__, exc)
                    return RunResult(exit_code=-2, ctx=ctx)

                try:
                    log_proc = subprocess.Popen(
                        [sys.executable, str(log_path)],
                        stdin=llm_proc.stdout, stdout=fh,
                        start_new_session=True,
                    )
                except (FileNotFoundError, OSError) as exc:
                    log.error("run_with_heartbeat: log Popen failed (%s): %s",
                              type(exc).__name__, exc)
                    if llm_proc.stdout is not None:
                        llm_proc.stdout.close()
                    _kill_tree(llm_proc)
                    return RunResult(exit_code=-2, ctx=ctx)

                llm_proc.stdout.close()
                try:
                    rc = llm_proc.wait(timeout=timeout_seconds)
                    log_rc = log_proc.wait(timeout=30)
                    if log_rc != 0:
                        log.error("run_with_heartbeat: log formatter exited %d", log_rc)
                        return RunResult(exit_code=-2, ctx=ctx)
                    return RunResult(exit_code=rc, ctx=ctx)
                except subprocess.TimeoutExpired:
                    _kill_tree(llm_proc)
                    _kill_tree(log_proc)
                    return RunResult(exit_code=-1, ctx=ctx)
        except KeyboardInterrupt:
            log.info("run_with_heartbeat: interrupted, killing children (job=%d)", job_id)
            if llm_proc is not None:
                _kill_tree(llm_proc)
            if log_proc is not None:
                _kill_tree(log_proc)
            raise
    finally:
        heartbeat.stop()
        heartbeat.join(timeout=10)
        try:
            os.unlink(prompt_path)
        except OSError:
            pass
