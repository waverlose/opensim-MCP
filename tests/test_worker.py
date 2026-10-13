"""Tests for the persistent Python session behind ``run_python``.

These need the ``mcp`` package (the server module imports it), so they skip
cleanly when only the runner is installed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

pytest.importorskip("mcp", reason="the persistent-session tests need the mcp package")

import opensim_mcp_server as S  # noqa: E402


@pytest.fixture
def worker():
    """A worker bound to this test, stopped afterwards."""
    client = S.WorkerClient()
    client.request({"op": "reset"}, timeout=60)
    yield client
    client.stop()


def test_state_persists_across_calls(worker):
    assert worker.request({"op": "exec", "source": "x = 41"})["ok"]
    result = worker.request({"op": "exec", "source": "x += 1\nprint('x is', x)"})
    assert result["ok"], result
    assert "x is 42" in result["stdout"]


def test_openSim_stdout_does_not_corrupt_the_protocol(worker):
    """Loading a model prints a summary table; it must land in stdout, not the pipe."""
    result = worker.request(
        {
            "op": "exec",
            "source": (
                "import opensim as osim\n"
                "osim.Logger.setLevelString('Warn')\n"
                "m = osim.Model()\n"
                "print('BODIES', m.getNumBodies())\n"
            ),
        }
    )
    assert result["ok"], result
    assert "BODIES" in result["stdout"]


def test_errors_come_back_structured(worker):
    result = worker.request({"op": "exec", "source": "1/0"})
    assert not result["ok"]
    assert "ZeroDivisionError" in result["error"]


def test_namespace_reports_user_variables(worker):
    worker.request({"op": "exec", "source": "answer = 42"})
    result = worker.request({"op": "namespace"})
    assert result["ok"], result
    assert "answer" in result["namespace"]


def test_reset_clears_variables(worker):
    worker.request({"op": "exec", "source": "keep = 1"})
    assert worker.request({"op": "reset"})["ok"]
    result = worker.request({"op": "exec", "source": "print('keep' in dir())"})
    assert result["stdout"].strip() == "False"


def test_a_crashing_call_does_not_kill_the_session(worker):
    """OpenSim is native code: a hard exit must be survivable."""
    crashed = worker.request({"op": "exec", "source": "import os; os._exit(3)"})
    assert not crashed["ok"]

    revived = worker.request({"op": "exec", "source": "print('alive')"})
    assert revived["ok"], revived
    assert "alive" in revived["stdout"]


def test_missing_script_is_reported(worker, tmp_path):
    result = worker.request(
        {"op": "script", "script_path": str(tmp_path / "absent.py")}
    )
    assert not result["ok"]
    assert "not found" in result["error"].lower()
