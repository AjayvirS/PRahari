"""Live smoke against real codex CLI (skipped when codex not on PATH)."""
import shutil
import subprocess

import pytest

from backends.codex import EXPECTED_CODEX_VERSION, CodexBackend


@pytest.fixture(autouse=True)
def _require_codex():
    if shutil.which("codex") is None:
        pytest.skip("codex not on PATH")


def test_codex_version_matches_pin():
    out = subprocess.run(["codex", "--version"], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == EXPECTED_CODEX_VERSION


def test_codex_features_lists_something():
    out = subprocess.run(["codex", "features", "list"], capture_output=True, text=True, check=True)
    assert out.returncode == 0
    # Don't assert specific feature names — codex updates may change them.
    # Just verify the command exits cleanly and prints non-empty output.
    assert out.stdout.strip() != ""
