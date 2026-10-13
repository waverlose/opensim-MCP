"""Shared fixtures.

Nothing here needs a pre-existing OpenSim installation beyond the pip package:
models are downloaded on demand and every input file is generated. The only
network access is fetching a model, and that skips with a clear message when it
is unavailable.

The model used throughout is ``arm26`` — 2 coordinates, 6 muscles, 3 markers.
That last number matters: the gait models in the registry ship **without** a
marker set (their markers live in separate scale-set files), so they cannot drive
inverse kinematics at all. arm26 is the classic model for exactly this test.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import opensim_runner as R  # noqa: E402

MODEL_NAME = "arm26"

# Non-trivial, and comfortably inside the coordinate ranges
# (r_shoulder_elev [-1.571, 3.142], r_elbow_flex [0, 2.269]).
POSE = {
    "r_shoulder_elev": 0.5,
    "r_elbow_flex": 1.0,
}


@pytest.fixture(scope="session")
def model() -> str:
    """The registered test model, downloaded on first use."""
    if R.run_op("load_model", {"model": MODEL_NAME})["ok"]:
        return MODEL_NAME

    downloaded = R.run_op("download_model", {"model_name": MODEL_NAME})
    if not downloaded["ok"]:
        pytest.skip(
            f"{MODEL_NAME} is not cached and could not be downloaded "
            f"(offline?): {downloaded.get('error')}"
        )
    return MODEL_NAME


def write_marker_trc(
    model: str, trc_path: Path, pose: dict, frames: int = 51, dt: float = 0.01
) -> list[str]:
    """Pose a model, record its marker positions, and write a .trc for IK.

    The frame count is generous on purpose: inverse dynamics differentiates the
    motion, and a handful of samples fails in the spline fit rather than
    producing a useful answer.
    """
    import opensim as osim

    osim.Logger.setLevelString("Error")
    loaded = osim.Model(str(R.resolve_model(model)))
    state = loaded.initSystem()

    coords = loaded.getCoordinateSet()
    for name, value in pose.items():
        coords.get(name).setValue(state, float(value))
    loaded.realizePosition(state)

    markers = loaded.getMarkerSet()
    names = [markers.get(i).getName() for i in range(markers.getSize())]
    if not names:
        raise RuntimeError(
            f"{model} defines no markers, so no marker data can be generated "
            "from it. Pick a model that carries a marker set."
        )

    table = osim.TimeSeriesTableVec3()
    labels = osim.StdVectorString()
    for n in names:
        labels.append(n)
    table.setColumnLabels(labels)

    for f in range(frames):
        row = osim.RowVectorVec3(len(names))
        for i, _ in enumerate(names):
            row.__setitem__(i, markers.get(i).getLocationInGround(state))
        table.appendRow(float(f) * dt, row)

    # The TRC writer refuses to run without the header keys a real capture file
    # would carry. Positions are in metres, so say so rather than the usual mm.
    # (This SWIG build exposes metadata only as key/value strings.)
    for key, value in (
        ("DataRate", "100"),
        ("CameraRate", "100"),
        ("NumFrames", str(frames)),
        ("NumMarkers", str(len(names))),
        ("Units", "m"),
        ("OrigDataRate", "100"),
        ("OrigDataStartFrame", "1"),
        ("OrigNumFrames", str(frames)),
    ):
        table.addTableMetaDataString(key, value)

    osim.TRCFileAdapter().write(table, str(trc_path))
    return names


@pytest.fixture(scope="session")
def posed_trc(model, tmp_path_factory) -> Path:
    """Marker data generated from a known pose."""
    out = tmp_path_factory.mktemp("markers")
    trc = out / "posed.trc"
    write_marker_trc(model, trc, POSE)
    return trc


@pytest.fixture(scope="session")
def ik_motion(model, posed_trc, tmp_path_factory) -> Path:
    """A real IK result, which doubles as the coordinates input for analyses."""
    out = tmp_path_factory.mktemp("ik")
    result = R.run_op(
        "run_inverse_kinematics",
        {
            "model": model,
            "markers_file": str(posed_trc),
            "output_motion_file": str(out / "ik.mot"),
            "results_dir": str(out),
        },
    )
    if not result["ok"]:
        pytest.skip(f"inverse kinematics unavailable: {result.get('error')}")
    return Path(result["data"]["output_motion_file"])
