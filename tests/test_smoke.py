"""End-to-end tests for the OpenSim runner.

Run with the OpenSim interpreter:

    python -m pytest tests -q

No local OpenSim installation or sample data is required: models are downloaded
on demand and the inputs are generated (see conftest.py).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import opensim_runner as R  # noqa: E402


def test_installation_info():
    out = R.run_op("get_installation_info", {})
    assert out["ok"], out
    assert out["data"]["opensim_version"]
    assert out["data"]["python_executable"]


def test_load_model_reports_physics(model):
    out = R.run_op("load_model", {"model": model})
    assert out["ok"], out
    d = out["data"]
    assert d["bodies"] > 0
    assert d["muscles"] > 0
    assert d["coordinates"] > 0
    assert d["mass_kg"] > 0
    assert len(d["muscle_names"]) == d["muscles"]
    assert len(d["coordinate_names"]) == d["coordinates"]


def test_inspect_model_lists_a_collection(model):
    out = R.run_op("inspect_model", {"model": model, "kind": "coordinate"})
    assert out["ok"], out
    assert out["data"]["members"], "expected a non-empty coordinate list"
    assert out["data"]["count"] == len(out["data"]["members"])


def test_inspect_model_rejects_an_unknown_collection(model):
    out = R.run_op("inspect_model", {"model": model, "kind": "not_a_thing"})
    assert not out["ok"]
    assert "Try one of" in out["error"]


def test_inspect_model_reads_a_named_component(model):
    loaded = R.run_op("load_model", {"model": model})
    coord = loaded["data"]["coordinate_names"][0]

    out = R.run_op(
        "inspect_model",
        {
            "model": model,
            "component": f"CoordinateSet/{coord}",
            "properties": ["getName", "getRangeMin", "getRangeMax"],
        },
    )
    assert out["ok"], out
    assert out["data"]["properties"]["getName"] == coord
    assert out["data"]["properties"]["getRangeMax"] > out["data"]["properties"]["getRangeMin"]


def test_missing_model_errors_cleanly():
    out = R.run_op("load_model", {"model": "definitely_not_a_model"})
    assert not out["ok"]
    assert "No such model" in out["error"]


def test_registered_but_undownloaded_model_says_how_to_fix_it(tmp_path, monkeypatch):
    """The message must name the tool to call, not just fail."""
    import opensim_runner

    monkeypatch.setattr(opensim_runner, "MODELS_DIR", tmp_path / "empty")
    out = R.run_op("load_model", {"model": "arm26"})
    if out["ok"]:  # arm26 resolves through another path on this machine
        pytest.skip("arm26 resolvable without the cache")
    assert "download_model" in out["error"]


def test_set_coordinate_values_roundtrip(model, tmp_path):
    loaded = R.run_op("load_model", {"model": model})
    coord = loaded["data"]["coordinate_names"][0]

    out = R.run_op(
        "set_coordinate_values",
        {
            "model": model,
            "values": {coord: 0.1},
            "output_model": str(tmp_path / "edited.osim"),
        },
    )
    assert out["ok"], out
    written = Path(out["data"]["output_model"])
    assert written.exists() and written.stat().st_size > 0

    # The edit must survive a reload.
    reread = R.run_op("inspect_model", {"model": str(written)})
    assert reread["ok"], reread


def test_list_models_reports_registry():
    out = R.run_op("list_models", {})
    assert out["ok"], out
    names = {m["name"] for m in out["data"]["registered"]}
    assert {"arm26", "gait2392", "rajagopal2016"} <= names


def test_read_storage_summarizes_a_real_result(ik_motion):
    out = R.run_op("read_storage", {"file_path": str(ik_motion)})
    assert out["ok"], out
    d = out["data"]
    assert d["rows"] > 0
    assert d["columns"], "an IK result should have coordinate columns"
    assert "time" not in d["columns"], "time is reported separately"
    assert d["time_range"][1] >= d["time_range"][0]
    assert d["statistics"], "expected per-column statistics"


def test_get_result_column_and_plot(ik_motion, tmp_path):
    cols = R.run_op("read_storage", {"file_path": str(ik_motion)})["data"]["columns"]
    column = cols[0]

    out = R.run_op(
        "get_result_column", {"file_path": str(ik_motion), "column": column}
    )
    assert out["ok"], out
    assert out["data"]["statistics"]["n"] > 0

    full = R.run_op(
        "get_result_column",
        {"file_path": str(ik_motion), "column": column, "full": True},
    )
    assert full["ok"], full
    assert len(full["data"]["values"]) == len(full["data"]["time"])

    png = R.run_op(
        "plot_results",
        {
            "file_path": str(ik_motion),
            "columns": cols[:2],
            "output_image": str(tmp_path / "plot.png"),
        },
    )
    assert png["ok"], png
    assert Path(png["data"]["output_image"]).stat().st_size > 0


def test_unknown_column_is_rejected_with_the_valid_list(ik_motion):
    out = R.run_op(
        "get_result_column", {"file_path": str(ik_motion), "column": "not_a_column"}
    )
    assert not out["ok"]
    assert "Available" in out["error"]


def test_analyses_produce_output_and_sane_activations(model, ik_motion, tmp_path):
    """Static optimization over a real motion must yield activations in [0, 1]."""
    out = R.run_op(
        "run_static_optimization",
        {
            "model": model,
            "coordinates_file": str(ik_motion),
            "results_dir": str(tmp_path / "so"),
        },
    )
    assert out["ok"], out.get("error")
    assert out["data"]["wrote"]

    files = list((tmp_path / "so").glob("*activation*.sto"))
    assert files, out["data"]["wrote"]

    read = R.run_op("read_storage", {"file_path": str(files[0]), "stat_columns": 200})
    assert read["ok"], read
    for column, stats in read["data"]["statistics"].items():
        assert -1e-6 <= stats["min"], f"{column} activation below 0: {stats['min']}"
        assert stats["max"] <= 1.0 + 1e-6, f"{column} activation above 1: {stats['max']}"


def test_muscle_analysis_writes_its_file_family(model, ik_motion, tmp_path):
    out = R.run_op(
        "run_muscle_analysis",
        {
            "model": model,
            "coordinates_file": str(ik_motion),
            "results_dir": str(tmp_path / "ma"),
        },
    )
    assert out["ok"], out.get("error")
    written = out["data"]["wrote"]
    assert any("Length" in w for w in written), written
    assert any("MomentArm" in w for w in written), written


def test_analysis_without_writing_anything_fails_loudly(model, tmp_path):
    """A coordinates file the model does not know must not look like success."""
    bogus = tmp_path / "bogus.mot"
    bogus.write_text("Coordinates\nversion=1\nnRows=1\nnColumns=2\n"
                     "inDegrees=no\nendheader\ntime\tnot_a_coordinate\n"
                     "0.0\t0.0\n", encoding="utf-8")
    out = R.run_op(
        "run_muscle_analysis",
        {
            "model": model,
            "coordinates_file": str(bogus),
            "results_dir": str(tmp_path / "bad"),
        },
    )
    assert not out["ok"], "an analysis that wrote nothing must report failure"


def test_unknown_op_is_reported():
    out = R.run_op("not_an_op", {})
    assert not out["ok"]
    assert "Unknown op" in out["error"]


def test_cli_entrypoint_prints_json():
    """The subprocess path the MCP server actually uses."""
    src = str(Path(__file__).resolve().parents[1] / "src")
    proc = subprocess.run(
        [sys.executable, "-m", "opensim_runner",
         "--op", "get_installation_info", "--args-json", "{}"],
        capture_output=True, text=True, encoding="utf-8", cwd=src, timeout=300,
    )
    lines = [l for l in proc.stdout.splitlines() if l.strip().startswith("{")]
    assert lines, f"no JSON on stdout: {proc.stdout[-500:]}"
    assert json.loads(lines[-1])["ok"]
