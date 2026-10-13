"""Inverse kinematics: a round-trip that checks the answer, not just the file.

A model is posed to a known set of joint angles, its markers are recorded into a
synthetic .trc, and IK is asked to recover the pose. If the recovered angles
match what went in, the whole marker -> motion path is correct; checking that an
output file appeared would not tell us that.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import opensim_runner as R  # noqa: E402
from conftest import POSE  # noqa: E402


def test_inverse_kinematics_recovers_a_known_pose(model, posed_trc, tmp_path):
    assert posed_trc.stat().st_size > 0

    out = R.run_op(
        "run_inverse_kinematics",
        {
            "model": model,
            "markers_file": str(posed_trc),
            "output_motion_file": str(tmp_path / "ik.mot"),
            "results_dir": str(tmp_path),
        },
    )
    assert out["ok"], out.get("error")

    mot = Path(out["data"]["output_motion_file"])
    assert mot.exists(), "IK reported success but wrote no motion file"

    read = R.run_op("read_storage", {"file_path": str(mot)})
    assert read["ok"], read
    assert read["data"]["rows"] >= 1
    in_degrees = read["data"]["in_degrees"]

    # Every modelled coordinate should come back close to what was posed.
    for name, expected in POSE.items():
        column = R.run_op("get_result_column", {"file_path": str(mot), "column": name})
        assert column["ok"], column
        got = column["data"]["statistics"]["mean"]
        if in_degrees:
            expected = expected * 180.0 / 3.141592653589793
        assert got == pytest.approx(expected, abs=0.05), (
            f"{name}: posed {expected}, IK recovered {got}"
        )


def test_ik_rejects_a_missing_marker_file(model, tmp_path):
    out = R.run_op(
        "run_inverse_kinematics",
        {
            "model": model,
            "markers_file": str(tmp_path / "nope.trc"),
            "results_dir": str(tmp_path),
        },
    )
    assert not out["ok"]
    assert "not found" in out["error"].lower()
