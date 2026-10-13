"""OpenSim MCP server: thin MCP layer over ``opensim_runner``.

Two execution paths, each matched to its job:

* **Structured pipeline tools** (scale / IK / ID / analyses) run one operation
  per subprocess via ``opensim_runner``. OpenSim's Tool API is file-in,
  file-out, so this is a natural fit, and a native crash kills only that
  subprocess.
* **``run_python`` / ``run_script``** talk to a long-lived worker process
  (``opensim_worker``) so variables and loaded models persist between calls.

Register with Claude Code as a stdio server; see README.md.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

try:  # mcp >= 2.0 renamed FastMCP to MCPServer
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
REFERENCES = PROJECT_ROOT / "skill" / "references"
DEFAULT_TIMEOUT = 300

# The server itself runs under the OpenSim environment, so sys.executable is
# normally the right interpreter for the workers too.
PYTHON = os.environ.get("OPENSIM_MCP_PYTHON") or sys.executable


def _child_env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(HERE) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


mcp = _Server("opensim")


# --------------------------------------------------------------------------- #
# structured operations: one subprocess each
# --------------------------------------------------------------------------- #


def _run_op(op: str, args: dict, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Run one ``opensim_runner`` op in a fresh subprocess and return its JSON."""
    cmd = [
        PYTHON,
        "-m",
        "opensim_runner",
        "--op",
        op,
        "--args-json",
        json.dumps(args, default=str),
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_child_env(),
            cwd=str(PROJECT_ROOT),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "op": op,
            "error": f"timed out after {timeout}s",
            "hint": "Increase the timeout argument, or reduce the time_range.",
        }
    except FileNotFoundError as exc:
        return {
            "ok": False,
            "op": op,
            "error": f"cannot start the OpenSim interpreter ({PYTHON}): {exc}",
            "hint": "Set OPENSIM_MCP_PYTHON, or run setup_env.ps1.",
        }

    payload = None
    for line in reversed((proc.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
                break
            except json.JSONDecodeError:
                continue

    if payload is None:
        return {
            "ok": False,
            "op": op,
            "error": f"runner produced no JSON (exit {proc.returncode})",
            "stdout_tail": (proc.stdout or "")[-2000:],
            "stderr_tail": (proc.stderr or "")[-2000:],
        }

    if proc.returncode != 0 and payload.get("ok"):
        payload["ok"] = False
        payload["error"] = f"runner exited {proc.returncode}"
    # Keep a tail of the native log; it often explains a failure.
    stderr_tail = (proc.stderr or "").strip()[-1500:]
    if stderr_tail:
        payload["logs"] = stderr_tail
    return payload


# --------------------------------------------------------------------------- #
# persistent worker: for run_python / run_script
# --------------------------------------------------------------------------- #


class WorkerClient:
    """Manages the long-lived OpenSim worker, restarting it when it dies."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._queue: queue.Queue = queue.Queue()
        self._reader: threading.Thread | None = None
        self._lock = threading.Lock()
        self._seq = 0

    def _spawn(self) -> None:
        self._proc = subprocess.Popen(
            [PYTHON, "-u", "-m", "opensim_worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_child_env(),
            cwd=str(PROJECT_ROOT),
            bufsize=1,
        )
        self._reader = threading.Thread(
            target=self._read_loop, args=(self._proc,), daemon=True
        )
        self._reader.start()

    def _read_loop(self, proc: subprocess.Popen) -> None:
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                self._queue.put(line)
        except Exception:
            pass
        finally:
            self._queue.put(None)  # signal EOF

    def _ensure(self) -> None:
        if self._proc is None or self._proc.poll() is not None:
            self._spawn()

    def request(self, req: dict, timeout: int = DEFAULT_TIMEOUT) -> dict:
        with self._lock:
            self._ensure()
            self._seq += 1
            req = dict(req, id=self._seq)
            try:
                self._proc.stdin.write(json.dumps(req, default=str) + "\n")  # type: ignore[union-attr]
                self._proc.stdin.flush()  # type: ignore[union-attr]
            except (BrokenPipeError, OSError) as exc:
                self._proc = None
                return {
                    "ok": False,
                    "error": f"worker pipe broke ({exc}); it will be restarted",
                }

            try:
                line = self._queue.get(timeout=timeout)
            except queue.Empty:
                self._kill()
                return {
                    "ok": False,
                    "error": f"worker did not respond within {timeout}s",
                    "hint": "The worker was restarted; its previous state is lost.",
                }

            if line is None:
                # EOF: the worker died mid-request. Give the OS a moment to
                # reap it so the exit code is real rather than None.
                code = None
                if self._proc is not None:
                    try:
                        code = self._proc.wait(timeout=5)
                    except Exception:
                        code = self._proc.poll()
                self._proc = None
                return {
                    "ok": False,
                    "error": (
                        f"worker process exited (code {code}) - most likely a "
                        "native crash inside OpenSim (a bad model or a diverging "
                        "solver). A fresh worker starts on the next call, but the "
                        "session state is gone."
                    ),
                }

            try:
                return json.loads(line)
            except json.JSONDecodeError as exc:
                return {"ok": False, "error": f"worker sent invalid JSON: {exc}"}

    def _kill(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.kill()
            except Exception:
                pass
        self._proc = None

    def stop(self) -> None:
        self._kill()


_worker = WorkerClient()


# --------------------------------------------------------------------------- #
# tools: environment and models
# --------------------------------------------------------------------------- #


@mcp.tool()
def get_installation_info(timeout: int = 60) -> dict:
    """Report the OpenSim version, interpreter, Moco availability and model dirs.

    Call this first to confirm the server can reach OpenSim.
    """
    return _run_op("get_installation_info", {}, timeout)


@mcp.tool()
def list_models(search_dir: str | None = None, timeout: int = 60) -> dict:
    """List the built-in model library and any .osim files found under search_dir."""
    return _run_op("list_models", {"search_dir": search_dir}, timeout)


@mcp.tool()
def download_model(model_name: str, overwrite: bool = False, timeout: int = 120) -> dict:
    """Fetch a registered model from opensim-org/opensim-models into the local cache.

    Only the .osim is fetched (no mesh geometry), which keeps each model small.
    """
    return _run_op(
        "download_model", {"model_name": model_name, "overwrite": overwrite}, timeout
    )


@mcp.tool()
def load_model(model: str, timeout: int = 120) -> dict:
    """Load a model and summarize it: bodies, joints, muscles, coordinates, mass.

    ``model`` is a registered name (e.g. "gait2392") or a path to a .osim file.
    """
    return _run_op("load_model", {"model": model}, timeout)


@mcp.tool()
def inspect_model(
    model: str,
    component: str | None = None,
    kind: str | None = None,
    properties: list[str] | None = None,
    timeout: int = 120,
) -> dict:
    """Inspect a model, or one component inside it.

    With no arguments, summarizes the whole model. ``component`` is a slash path
    such as "BodySet/femur_r" or "CoordinateSet/knee_angle_r", and ``properties``
    names accessors to read on it, e.g. ["getName", "getRangeMin"].

    ``kind`` lists a member collection instead. Accepted words include:
    coordinate, body, joint, muscle, marker, force, actuator, constraint,
    controller, probe, contactgeometry.
    """
    return _run_op(
        "inspect_model",
        {
            "model": model,
            "component": component,
            "kind": kind,
            "properties": properties or [],
        },
        timeout,
    )


@mcp.tool()
def set_coordinate_values(
    model: str, values: dict, output_model: str | None = None, timeout: int = 120
) -> dict:
    """Set coordinate values on a model, optionally writing the result to a .osim.

    ``values`` maps coordinate name -> value (radians, or degrees for models
    flagged in degrees).
    """
    return _run_op(
        "set_coordinate_values",
        {"model": model, "values": values, "output_model": output_model},
        timeout,
    )


@mcp.tool()
def save_model(model: str, output_model: str, timeout: int = 120) -> dict:
    """Write a loaded model out as .osim (useful after edits)."""
    return _run_op("save_model", {"model": model, "output_model": output_model}, timeout)


# --------------------------------------------------------------------------- #
# tools: standard pipeline
# --------------------------------------------------------------------------- #


@mcp.tool()
def scale_model(
    model: str,
    output_model: str | None = None,
    subject_mass: float | None = None,
    subject_height: float | None = None,
    subject_age: float | None = None,
    markers_file: str | None = None,
    scale_set_file: str | None = None,
    path_to_subject: str | None = None,
    time_range: list[float] | None = None,
    results_dir: str | None = None,
    timeout: int = 600,
) -> dict:
    """Scale a generic model to a subject, by mass/height and/or by markers.

    Supply a ``scale_set_file`` (or the marker-based workflow) for real scaling;
    mass/height alone will not change segment lengths without one.
    """
    return _run_op(
        "scale_model",
        {
            "model": model,
            "output_model": output_model,
            "subject_mass": subject_mass,
            "subject_height": subject_height,
            "subject_age": subject_age,
            "markers_file": markers_file,
            "scale_set_file": scale_set_file,
            "path_to_subject": path_to_subject,
            "time_range": time_range,
            "results_dir": results_dir,
        },
        timeout,
    )


@mcp.tool()
def run_inverse_kinematics(
    model: str,
    markers_file: str,
    output_motion_file: str | None = None,
    time_range: list[float] | None = None,
    accuracy: float | None = None,
    results_dir: str | None = None,
    timeout: int = 900,
) -> dict:
    """Inverse kinematics: marker trajectories (.trc/.mot) -> joint angles (.mot)."""
    return _run_op(
        "run_inverse_kinematics",
        {
            "model": model,
            "markers_file": markers_file,
            "output_motion_file": output_motion_file,
            "time_range": time_range,
            "accuracy": accuracy,
            "results_dir": results_dir,
        },
        timeout,
    )


@mcp.tool()
def run_inverse_dynamics(
    model: str,
    coordinates_file: str,
    output_gen_force_file: str | None = None,
    external_loads_file: str | None = None,
    lowpass_cutoff: float | None = None,
    time_range: list[float] | None = None,
    results_dir: str | None = None,
    timeout: int = 900,
) -> dict:
    """Inverse dynamics: joint angles + external loads -> joint moments (.sto).

    Pass the .mot from inverse kinematics as ``coordinates_file``, and a GRF
    .mot/.xml as ``external_loads_file`` when ground contact matters.
    """
    return _run_op(
        "run_inverse_dynamics",
        {
            "model": model,
            "coordinates_file": coordinates_file,
            "output_gen_force_file": output_gen_force_file,
            "external_loads_file": external_loads_file,
            "lowpass_cutoff": lowpass_cutoff,
            "time_range": time_range,
            "results_dir": results_dir,
        },
        timeout,
    )


@mcp.tool()
def run_static_optimization(
    model: str,
    coordinates_file: str,
    activation_exponent: float | None = None,
    use_muscle_physiology: bool | None = None,
    lowpass_cutoff: float | None = None,
    time_range: list[float] | None = None,
    results_dir: str | None = None,
    timeout: int = 900,
) -> dict:
    """Static optimization -> per-muscle activations and forces.

    Needs an inverse-kinematics .mot plus the external loads for the trial.
    """
    return _run_op(
        "run_static_optimization",
        {
            "model": model,
            "coordinates_file": coordinates_file,
            "activation_exponent": activation_exponent,
            "use_muscle_physiology": use_muscle_physiology,
            "lowpass_cutoff": lowpass_cutoff,
            "time_range": time_range,
            "results_dir": results_dir,
        },
        timeout,
    )


@mcp.tool()
def run_muscle_analysis(
    model: str,
    coordinates_file: str,
    compute_moments: bool = True,
    lowpass_cutoff: float | None = None,
    time_range: list[float] | None = None,
    results_dir: str | None = None,
    timeout: int = 900,
) -> dict:
    """Muscle analysis: fiber lengths, moment arms, velocities, activations, forces."""
    return _run_op(
        "run_muscle_analysis",
        {
            "model": model,
            "coordinates_file": coordinates_file,
            "compute_moments": compute_moments,
            "lowpass_cutoff": lowpass_cutoff,
            "time_range": time_range,
            "results_dir": results_dir,
        },
        timeout,
    )


@mcp.tool()
def compute_joint_reactions(
    model: str,
    coordinates_file: str,
    forces_file: str | None = None,
    joint_names: list[str] | None = None,
    in_frame: str | None = None,
    lowpass_cutoff: float | None = None,
    time_range: list[float] | None = None,
    results_dir: str | None = None,
    timeout: int = 900,
) -> dict:
    """Joint reaction (contact) forces for a trial.

    ``forces_file`` is normally the inverse-dynamics .sto of the trial;
    ``in_frame`` names the reference frame to express forces in (e.g. "child").
    """
    return _run_op(
        "compute_joint_reactions",
        {
            "model": model,
            "coordinates_file": coordinates_file,
            "forces_file": forces_file,
            "joint_names": joint_names,
            "in_frame": in_frame,
            "lowpass_cutoff": lowpass_cutoff,
            "time_range": time_range,
            "results_dir": results_dir,
        },
        timeout,
    )


@mcp.tool()
def run_forward_dynamics(
    model: str,
    controls_file: str | None = None,
    states_file: str | None = None,
    time_range: list[float] | None = None,
    max_steps: int | None = None,
    results_dir: str | None = None,
    timeout: int = 900,
) -> dict:
    """Forward integration of a model under its controls/states."""
    return _run_op(
        "run_forward_dynamics",
        {
            "model": model,
            "controls_file": controls_file,
            "states_file": states_file,
            "time_range": time_range,
            "max_steps": max_steps,
            "results_dir": results_dir,
        },
        timeout,
    )


# --------------------------------------------------------------------------- #
# tools: results
# --------------------------------------------------------------------------- #


@mcp.tool()
def read_storage(
    file_path: str,
    preview_rows: int = 5,
    stat_columns: int = 30,
    timeout: int = 120,
) -> dict:
    """Summarize a .sto/.mot result: columns, time range, statistics, preview rows."""
    return _run_op(
        "read_storage",
        {"file_path": file_path, "preview_rows": preview_rows, "stat_columns": stat_columns},
        timeout,
    )


@mcp.tool()
def get_result_column(
    file_path: str, column: str, full: bool = False, timeout: int = 120
) -> dict:
    """Statistics for one result column, or the full time series with full=True."""
    return _run_op(
        "get_result_column",
        {"file_path": file_path, "column": column, "full": full},
        timeout,
    )


@mcp.tool()
def plot_results(
    file_path: str,
    columns: list[str] | None = None,
    output_image: str | None = None,
    title: str | None = None,
    ylabel: str | None = None,
    timeout: int = 180,
) -> dict:
    """Plot result columns to a PNG and return its path."""
    return _run_op(
        "plot_results",
        {
            "file_path": file_path,
            "columns": columns,
            "output_image": output_image,
            "title": title,
            "ylabel": ylabel,
        },
        timeout,
    )


# --------------------------------------------------------------------------- #
# tools: escape hatch
# --------------------------------------------------------------------------- #


@mcp.tool()
def run_python(source: str, cwd: str | None = None, timeout: int = 600) -> dict:
    """Execute Python in a persistent OpenSim session and return its output.

    ``opensim`` (aliased ``osim``) and ``numpy`` (``np``) are pre-imported, and
    variables persist across calls. Use this for anything the structured tools
    do not cover -- notably OpenSim Moco (MocoInverse, MocoTrack, MocoStudy).
    """
    return _worker.request(
        {"op": "exec", "source": source, "cwd": cwd}, timeout
    )


@mcp.tool()
def run_script(script_path: str, timeout: int = 600) -> dict:
    """Execute a .py file in the persistent session (its folder becomes cwd)."""
    return _worker.request({"op": "script", "script_path": script_path}, timeout)


@mcp.tool()
def session_info() -> dict:
    """Show the variables currently defined in the persistent session."""
    return _worker.request({"op": "namespace"}, timeout=30)


@mcp.tool()
def reset_session() -> dict:
    """Clear the persistent session (drops every variable and loaded model)."""
    return _worker.request({"op": "reset"}, timeout=60)


# --------------------------------------------------------------------------- #
# resources: documentation the agent can pull into context
# --------------------------------------------------------------------------- #


def _reference(name: str) -> str:
    p = REFERENCES / name
    if not p.exists():
        return f"(reference {name} not installed)"
    return p.read_text(encoding="utf-8")


@mcp.resource("opensim://installation")
def res_installation() -> str:
    """How this server is wired to OpenSim, and how to fix a broken install."""
    return _reference("installation.md")


@mcp.resource("opensim://api-cheatsheet")
def res_cheatsheet() -> str:
    """Verified OpenSim Python API surface: classes, accessors, gotchas."""
    return _reference("api-cheatsheet.md")


@mcp.resource("opensim://workflows")
def res_workflows() -> str:
    """Recipe for the standard pipeline: scale -> IK -> ID -> SO -> muscle analysis."""
    return _reference("workflows.md")


@mcp.resource("opensim://models")
def res_models() -> str:
    """The built-in model library and how to fetch more."""
    return _reference("models.md")


@mcp.resource("opensim://moco-recipe")
def res_moco() -> str:
    """Runnable OpenSim Moco recipes (MocoInverse, MocoTrack) for run_python."""
    return _reference("moco-recipe.md")


# --------------------------------------------------------------------------- #
# prompts
# --------------------------------------------------------------------------- #


@mcp.prompt()
def run_pipeline(model: str = "gait10dof18musc", markers_file: str = "") -> str:
    """Plan a scale -> IK -> ID -> static-optimization run for a trial."""
    return (
        f"Run the standard OpenSim pipeline for model '{model}'\n"
        f"and marker file '{markers_file}'.\n\n"
        "1. Read the resource opensim://workflows for the exact recipe.\n"
        "2. get_installation_info, then load_model to confirm the model.\n"
        "3. download_model if the model is not cached yet.\n"
        "4. run_inverse_kinematics -> run_inverse_dynamics -> "
        "run_static_optimization.\n"
        "5. read_storage each output and report the peak activations and joint "
        "moments, with units.\n"
        "State any assumption you had to make (filter cutoffs, time range, "
        "missing external loads) rather than silently choosing one."
    )


@mcp.prompt()
def moco_inverse(model: str = "rajagopal2016", motion_file: str = "") -> str:
    """Plan an OpenSim Moco inverse solve via run_python."""
    return (
        f"Set up a MocoInverse problem for model '{model}' using motion "
        f"'{motion_file}'.\n\n"
        "Read opensim://moco-recipe first and follow it. Use run_python so the "
        "study object persists between calls, and check gates/effort after "
        "solving. Report the solver's convergence and the objective value."
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
