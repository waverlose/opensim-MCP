"""OpenSim simulation logic for the MCP server.

Importable headlessly (no MCP dependency), and usable as a CLI so the MCP
server can run each structured operation in its own subprocess:

    python -m opensim_runner --op run_inverse_kinematics --args-json '{...}'

Every ``op_*`` function takes a dict of arguments and returns a JSON-serializable
dict. Nothing raises: failures come back as ``{"ok": False, "error": ...}`` so a
native OpenSim crash or a bad model path cannot take down the caller.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
MODELS_DIR = HERE / "models"
REGISTRY_PATH = HERE / "models_registry.json"

# The official wheel finds its own native libraries. A manual SDK install does
# not: its `sdk\Python` directory has to be on sys.path and its `bin` directory
# on PATH. These two overrides cover that case.
_SDK_PATH_ADDED = False


def _osim():
    """Import opensim lazily so env checks still work when it is missing."""
    global _SDK_PATH_ADDED
    import os

    if not _SDK_PATH_ADDED:
        sdk_python = os.environ.get("OPENSIM_SDK_PYTHON")
        if sdk_python and sdk_python not in sys.path:
            sys.path.insert(0, sdk_python)
        bin_hint = os.environ.get("OPENSIM_BIN_HINT")
        if bin_hint and bin_hint not in os.environ.get("PATH", ""):
            os.environ["PATH"] = bin_hint + os.pathsep + os.environ.get("PATH", "")
        _SDK_PATH_ADDED = True

    import opensim

    return opensim


def _quiet() -> None:
    """Suppress OpenSim's geometry/mesh chatter in headless runs."""
    try:
        _osim().Logger.setLevelString("Error")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# result envelope
# --------------------------------------------------------------------------- #


@dataclass
class RunResult:
    """Uniform result envelope. ``ok`` is the only field callers must check."""

    ok: bool = True
    op: str = ""
    error: str | None = None
    traceback: str | None = None
    warnings: list[str] = field(default_factory=list)
    outputs: dict[str, Any] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        if d["error"] is None:
            d.pop("error")
        if d["traceback"] is None:
            d.pop("traceback")
        if not d["warnings"]:
            d.pop("warnings")
        return d


def _safe_call(op: str, fn: Callable[[dict], dict], args: dict) -> dict:
    """Run one operation, converting any exception into a structured error."""
    try:
        data = fn(args or {}) or {}
        return RunResult(ok=True, op=op, data=data).to_dict()
    except Exception as exc:  # noqa: BLE001 - deliberately broad: native code too
        return RunResult(
            ok=False,
            op=op,
            error=f"{type(exc).__name__}: {exc}",
            traceback=traceback.format_exc(limit=8),
        ).to_dict()


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #


def _set_first(obj: Any, names: list[str], value: Any) -> str | None:
    """Call the first setter that exists.

    OpenSim renames accessors between versions (``setModelFileName`` vs
    ``setModelFilename`` vs ``set_model_file``), so never assume one name.
    """
    for n in names:
        fn = getattr(obj, n, None)
        if callable(fn):
            fn(value)
            return n
    return None


def _arr(x: Any) -> list:
    """Convert a SWIG ArrayDouble/ArrayStr/Vec3 into a plain list."""
    if x is None:
        return []
    for attr in ("getSize", "size"):
        if hasattr(x, attr):
            try:
                n = getattr(x, attr)()
            except Exception:
                continue
            try:
                return [x.get(i) for i in range(n)]
            except Exception:
                break
    try:
        return list(x)
    except Exception:
        return [x]


def _names(container: Any) -> list[str]:
    """Names of every object in a SWIG Set/List (CoordinateSet, MuscleList...)."""
    out: list[str] = []
    n = 0
    for attr in ("getSize", "getNumObjects"):
        if hasattr(container, attr):
            n = getattr(container, attr)()
            break
    for i in range(n):
        try:
            out.append(container.get(i).getName())
        except Exception:
            break
    return out


def _load_registry() -> dict:
    if not REGISTRY_PATH.exists():
        return {}
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8")).get("models", {})


def resolve_model(spec: str) -> Path:
    """Accept an absolute/relative .osim path, or a name from the registry."""
    p = Path(spec)
    if p.exists():
        return p.resolve()

    reg = _load_registry()
    if spec in reg:
        local = MODELS_DIR / reg[spec]["filename"]
        if local.exists():
            return local
        raise FileNotFoundError(
            f"Model '{spec}' is registered but not downloaded yet. "
            f"Call download_model(model_name='{spec}') first (expected at {local})."
        )

    known = ", ".join(sorted(reg)) or "(registry empty)"
    raise FileNotFoundError(
        f"No such model file or registered model: {spec!r}. Known names: {known}"
    )


def _ensure_dir(p: str | Path) -> Path:
    d = Path(p)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _abs_input(value: Any) -> str | None:
    """Resolve a caller-supplied *input* path to an absolute one.

    OpenSim resolves relative paths against different bases depending on the
    tool - and the analyses resolve them from inside the setup XML, where the
    working directory is not the caller's. Making inputs absolute once, here,
    removes a whole class of "file exists but cannot be opened" failures.
    """
    if value in (None, ""):
        return None
    p = Path(str(value))
    return str(p if p.is_absolute() else (Path.cwd() / p))


def _model_summary(model: Any) -> dict:
    """A compact, physically meaningful description of a loaded model."""
    state = model.initSystem()
    summary: dict[str, Any] = {
        "name": model.getName(),
        "bodies": model.getNumBodies(),
        "joints": model.getNumJoints(),
        "coordinates": model.getNumCoordinates(),
        "markers": model.getNumMarkers(),
        "state_variables": model.getNumStateVariables(),
    }
    for key, fn in (
        ("muscles", lambda: model.getMuscles().getSize()),
        ("actuators", lambda: model.getActuators().getSize()),
        ("forces", lambda: model.getForceSet().getSize()),
        ("mass_kg", lambda: model.getTotalMass(state)),
    ):
        try:
            summary[key] = fn()
        except Exception:
            pass
    try:
        summary["gravity"] = _arr(model.getGravity())
    except Exception:
        pass
    for key, fn in (
        ("coordinate_names", lambda: _names(model.getCoordinateSet())),
        ("body_names", lambda: _names(model.getBodySet())),
        ("joint_names", lambda: _names(model.getJointSet())),
        ("marker_names", lambda: _names(model.getMarkerSet())),
        ("muscle_names", lambda: _names(model.getMuscles())),
    ):
        try:
            summary[key] = fn()
        except Exception:
            pass
    return summary


def _storage_columns(storage: Any) -> list[str]:
    labs = storage.getColumnLabels()
    return [labs.get(i) for i in range(labs.getSize())]


def _storage_data_columns(storage: Any) -> list[str]:
    """Result columns, i.e. everything except the leading time column."""
    cols = _storage_columns(storage)
    return cols[1:] if cols and cols[0].lower() == "time" else cols


def _time_array(storage: Any) -> list[float]:
    """Storage's time column.

    ``getTimeColumn()`` is not a plain getter here - SWIG exposes it with an
    out-parameter, so the array has to be passed in and filled.
    """
    osim = _osim()
    arr = osim.ArrayDouble()
    storage.getTimeColumn(arr)
    return [arr.get(i) for i in range(arr.getSize())]


def _col_array(storage: Any, key: Any) -> list[float]:
    """One data column by name or index, using the same out-parameter form."""
    osim = _osim()
    arr = osim.ArrayDouble()
    if isinstance(key, int):
        storage.getDataColumn(key, arr)
    else:
        storage.getDataColumn(str(key), arr)
    return [arr.get(i) for i in range(arr.getSize())]


def _read_storage_any(path: str | Path) -> Any:
    """Read .sto/.mot via Storage (it handles both)."""
    osim = _osim()
    return osim.Storage(str(path))


def _stat(values: list[float]) -> dict:
    if not values:
        return {}
    s = sorted(values)
    return {
        "min": s[0],
        "max": s[-1],
        "mean": sum(values) / len(values),
        "n": len(values),
    }


# --------------------------------------------------------------------------- #
# environment / models
# --------------------------------------------------------------------------- #


def op_get_installation_info(args: dict) -> dict:
    osim = _osim()
    info: dict[str, Any] = {
        "python_executable": sys.executable,
        "python_version": sys.version.split()[0],
        "cwd": str(Path.cwd()),
        "models_dir": str(MODELS_DIR),
        "opensim_version": osim.GetVersion(),
        "opensim_module": getattr(osim, "__file__", None),
    }
    for key, attr in (
        ("moco_available", "MocoStudy"),
        ("moco_casadi_available", "MocoCasADiSolver"),
        ("moco_tropter_available", "MocoTropterSolver"),
        ("opensense_available", "IMUInverseKinematicsTool"),
    ):
        info[key] = hasattr(osim, attr)

    reg = _load_registry()
    info["registered_models"] = sorted(reg)
    info["downloaded_models"] = sorted(
        p.stem for p in MODELS_DIR.glob("*.osim")
    ) if MODELS_DIR.exists() else []
    return info


def op_list_models(args: dict) -> dict:
    search = args.get("search_dir")
    reg = _load_registry()
    available: list[dict] = []
    for name, meta in sorted(reg.items()):
        local = MODELS_DIR / meta["filename"]
        available.append(
            {
                "name": name,
                "downloaded": local.exists(),
                "path": str(local) if local.exists() else None,
                "description": meta.get("description", ""),
            }
        )
    extra: list[str] = []
    if search:
        extra = [str(p) for p in Path(search).rglob("*.osim")]
    return {
        "registered": available,
        "found_in_search_dir": extra,
        "models_dir": str(MODELS_DIR),
    }


def op_download_model(args: dict) -> dict:
    name = args["model_name"]
    reg = _load_registry()
    if name not in reg:
        raise KeyError(
            f"Unknown model {name!r}. Known: {', '.join(sorted(reg)) or '(none)'}"
        )
    meta = reg[name]
    _ensure_dir(MODELS_DIR)
    dest = MODELS_DIR / meta["filename"]
    if dest.exists() and not args.get("overwrite"):
        return {"path": str(dest), "downloaded": False, "reason": "already present"}

    url = meta["url"]
    req = urllib.request.Request(url, headers={"User-Agent": "opensim-mcp"})
    with urllib.request.urlopen(req, timeout=args.get("timeout", 60)) as resp:
        payload = resp.read()
    dest.write_bytes(payload)
    return {"path": str(dest), "downloaded": True, "bytes": len(payload), "url": url}


# --------------------------------------------------------------------------- #
# model inspection / editing
# --------------------------------------------------------------------------- #


def op_load_model(args: dict) -> dict:
    _quiet()
    osim = _osim()
    path = resolve_model(args["model"])
    osim.Model(str(path))  # fail fast on a malformed file
    model = osim.Model(str(path))
    summary = _model_summary(model)
    summary["path"] = str(path)
    return summary


# Component collection names do not follow one pattern in the OpenSim API:
# coordinates live in getCoordinateSet, muscles in getMuscles, markers in
# getMarkerSet. Map the words a caller is likely to use onto the real accessors.
_COLLECTIONS: dict[str, list[str]] = {
    "coordinate": ["getCoordinateSet", "getCoordinates"],
    "coordinates": ["getCoordinateSet", "getCoordinates"],
    "body": ["getBodySet", "getBodies"],
    "bodies": ["getBodySet", "getBodies"],
    "joint": ["getJointSet", "getJoints"],
    "joints": ["getJointSet", "getJoints"],
    "muscle": ["getMuscles"],
    "muscles": ["getMuscles"],
    "marker": ["getMarkerSet", "getMarkers"],
    "markers": ["getMarkerSet", "getMarkers"],
    "force": ["getForceSet", "getForces"],
    "forces": ["getForceSet", "getForces"],
    "actuator": ["getActuators"],
    "actuators": ["getActuators"],
    "contactgeometry": ["getContactGeometrySet"],
    "constraint": ["getConstraintSet", "getConstraints"],
    "controller": ["getControllerSet", "getControllers"],
    "probe": ["getProbeSet", "getProbes"],
}


def _resolve_component(model: Any, component: str) -> Any:
    """Walk a slash path such as "BodySet/femur_r" to an OpenSim object."""
    obj = model
    for part in str(component).split("/"):
        if not part:
            continue
        resolved = None
        for candidate in ("get_" + part, "get" + part[0].upper() + part[1:]):
            fn = getattr(obj, candidate, None)
            if callable(fn):
                try:
                    resolved = fn()
                except Exception:
                    resolved = None
                if resolved is not None:
                    break
        if resolved is None:
            # Not an accessor name, so it may be a member of a set: Set.get("name").
            getter = getattr(obj, "get", None)
            if callable(getter):
                try:
                    resolved = getter(part)
                except Exception:
                    resolved = None
        if resolved is None:
            raise KeyError(
                f"Cannot resolve {part!r} on {type(obj).__name__}. Use an "
                f"accessor (getCoordinateSet) or a member name inside a set "
                f"(CoordinateSet/knee_angle_r)."
            )
        obj = resolved
    return obj


def op_inspect_model(args: dict) -> dict:
    _quiet()
    osim = _osim()
    path = resolve_model(args["model"])
    model = osim.Model(str(path))
    component = args.get("component")
    kind = args.get("kind")
    properties = args.get("properties") or []

    if not component and not kind and not properties:
        return _model_summary(model)

    obj = _resolve_component(model, component) if component else model

    if kind:
        for accessor in _COLLECTIONS.get(str(kind).lower(), []):
            getter = getattr(obj, accessor, None)
            if not callable(getter):
                continue
            try:
                members = _names(getter())
            except Exception:
                continue
            if members:
                return {
                    "component": component,
                    "kind": kind,
                    "accessor": accessor,
                    "members": members,
                    "count": len(members),
                }
        raise KeyError(
            f"No collection {kind!r} on {component or 'the model'}. Try one of: "
            f"{', '.join(sorted(set(_COLLECTIONS)))}"
        )

    props: dict[str, Any] = {}
    for prop in properties:
        g = getattr(obj, prop, None)
        if callable(g):
            try:
                props[prop] = g()
            except Exception as exc:
                props[prop] = f"<{type(exc).__name__}: {exc}>"
        else:
            props[prop] = f"<no such accessor on {type(obj).__name__}>"
    return {
        "component": component,
        "class": type(obj).__name__,
        "properties": props,
    }


def op_set_coordinate_values(args: dict) -> dict:
    _quiet()
    osim = _osim()
    path = resolve_model(args["model"])
    model = osim.Model(str(path))
    state = model.initSystem()
    coords = model.getCoordinateSet()

    applied: dict[str, float] = {}
    for name, value in (args.get("values") or {}).items():
        idx = coords.getIndex(name)
        if idx < 0:
            raise KeyError(f"No coordinate named {name!r}")
        coords.get(idx).setValue(state, float(value))
        applied[name] = float(value)
    model.realizePosition(state)

    out_path = args.get("output_model")
    if out_path:
        model.printToXML(str(out_path))
    return {
        "applied": applied,
        "output_model": str(out_path) if out_path else None,
    }


def op_save_model(args: dict) -> dict:
    _quiet()
    osim = _osim()
    model = osim.Model(str(resolve_model(args["model"])))
    out = Path(args["output_model"])
    _ensure_dir(out.parent)
    model.printToXML(str(out))
    return {"output_model": str(out), "bytes": out.stat().st_size}


# --------------------------------------------------------------------------- #
# standard pipeline
# --------------------------------------------------------------------------- #


def _results_dir(args: dict) -> Path:
    # Absolute: a relative results dir would otherwise get re-prefixed onto the
    # already-qualified default output path and land in a doubled directory.
    raw = args.get("results_dir") or (Path.cwd() / "opensim_results")
    return _ensure_dir(Path(raw).resolve())


def _place_output(args: dict, key: str, default: Path) -> tuple[Path, str, Path]:
    """Resolve an output file argument.

    Returns ``(results_dir, basename, full_path)``.

    OpenSim's tools join ``resultsDir`` with the output *filename*, so passing an
    absolute path makes it concatenate the two into nonsense. Give the tool the
    basename only, and set resultsDir to wherever the caller actually wants it.
    """
    raw = args.get(key)
    full = Path(raw) if raw else default
    if not full.is_absolute():
        full = default.parent / full
    return full.parent, full.name, full


def _require_output(path: Path, what: str) -> None:
    """Fail loudly when a tool reported success but wrote nothing.

    OpenSim logs some I/O failures and still returns normally, so trusting the
    absence of an exception here would hand the caller a silent wrong answer.
    """
    if not path.exists() or path.stat().st_size == 0:
        raise RuntimeError(
            f"{what} did not produce {path}. OpenSim often logs the real cause "
            f"and still returns - check the 'logs' field for an [error] line."
        )


def _require_any_output(out_dir: Path, pattern: str, what: str) -> list[str]:
    """As above, for the analyses, which write a family of files."""
    written = sorted(p.name for p in out_dir.glob(pattern))
    if not written:
        raise RuntimeError(
            f"{what} produced no {pattern} in {out_dir}. OpenSim often logs the "
            f"real cause (bad coordinates file, missing external loads) and "
            f"still returns - check the 'logs' field."
        )
    return written


def op_scale_model(args: dict) -> dict:
    """Scale a generic model to a subject (by mass/height, or by markers)."""
    _quiet()
    osim = _osim()
    model_path = resolve_model(args["model"])
    out_dir = _results_dir(args)
    scaled = Path(args.get("output_model") or (out_dir / f"{model_path.stem}_scaled.osim"))

    tool = osim.ScaleTool()
    # ScaleTool has no top-level setModel; the model goes through the GenericModelMaker.
    gm = tool.getGenericModelMaker()
    _set_first(gm, ["setModelFileName", "setModelFilename", "set_model_file"],
               str(model_path))

    if args.get("subject_mass") is not None:
        tool.setSubjectMass(float(args["subject_mass"]))
    if args.get("subject_height") is not None:
        tool.setSubjectHeight(float(args["subject_height"]))
    if args.get("subject_age") is not None:
        tool.setSubjectAge(float(args["subject_age"]))
    if args.get("path_to_subject"):
        tool.setPathToSubject(str(args["path_to_subject"]))

    scaler = tool.getModelScaler()
    scaler.setApply(bool(args.get("apply_scaling", True)))
    if args.get("scale_set_file"):
        scaler.setScaleSetFile(_abs_input(args["scale_set_file"]))
    if args.get("markers_file"):
        markers_file = _abs_input(args["markers_file"])
        scaler.setMarkerFileName(markers_file)
        placer = tool.getMarkerPlacer()
        placer.setApply(bool(args.get("apply_marker_placer", True)))
        placer.setMarkerFileName(markers_file)
        placer.setOutputMotionFileName(str(out_dir / "scaled_markers.mot"))
        if args.get("time_range"):
            tr = args["time_range"]
            placer.setTimeRange(float(tr[0]), float(tr[1]))
    scaler.setOutputModelFileName(str(scaled))
    tool.setPrintResultFiles(True)

    tool.run()
    if not scaled.exists():
        raise RuntimeError(
            f"ScaleTool finished but {scaled} was not written; check the "
            f"ScaleSet/measurement files for this model."
        )
    return {"output_model": str(scaled), "results_dir": str(out_dir)}


def op_run_inverse_kinematics(args: dict) -> dict:
    """Marker data (.trc/.mot) -> joint angles (.mot)."""
    _quiet()
    osim = _osim()
    model_path = resolve_model(args["model"])
    markers = Path(_abs_input(args["markers_file"]))
    if not markers.exists():
        raise FileNotFoundError(f"Marker file not found: {markers}")

    # A model with no markers makes IK segfault inside OpenSim rather than
    # raising, taking the whole process with it. Fail here instead, while we can
    # still say something useful.
    probe = osim.Model(str(model_path))
    if probe.getNumMarkers() == 0:
        raise ValueError(
            f"{model_path.name} defines no markers, so inverse kinematics has "
            "nothing to track. Use a model that carries a marker set (e.g. "
            "rajagopal2016), or add markers to this one."
        )

    out_dir, _, out_mot = _place_output(
        args, "output_motion_file", _results_dir(args) / f"{markers.stem}_ik.mot"
    )
    out_dir = _ensure_dir(out_dir)

    tool = osim.InverseKinematicsTool()
    _set_first(tool, ["setModelFileName", "setModelFilename", "set_model_file"],
               str(model_path))
    tool.setMarkerDataFileName(str(markers))
    # IK takes an ABSOLUTE output path. Given a bare filename it writes to the
    # current working directory and ignores resultsDir, which is the opposite of
    # what InverseDynamicsTool does with the same property. Verified on 4.6.
    tool.setOutputMotionFileName(str(out_mot))
    tool.setResultsDir(str(out_dir))
    tool.set_report_errors(True)
    if args.get("time_range"):
        tr = args["time_range"]
        tool.setStartTime(float(tr[0]))
        tool.setEndTime(float(tr[1]))
    if args.get("accuracy") is not None:
        _set_first(tool, ["setAccuracy", "set_accuracy"], float(args["accuracy"]))

    tool.run()
    _require_output(out_mot, "inverse kinematics")
    return {
        "output_motion_file": str(out_mot),
        "results_dir": str(out_dir),
        "size_bytes": out_mot.stat().st_size,
    }


def op_run_inverse_dynamics(args: dict) -> dict:
    """Joint angles (.mot) [+ external loads] -> joint moments (.sto)."""
    _quiet()
    osim = _osim()
    model_path = resolve_model(args["model"])
    coords = Path(_abs_input(args["coordinates_file"]))
    if not coords.exists():
        raise FileNotFoundError(f"Coordinates file not found: {coords}")
    out_dir, out_name, out_sto = _place_output(
        args, "output_gen_force_file", _results_dir(args) / f"{coords.stem}_id.sto"
    )
    out_dir = _ensure_dir(out_dir)

    tool = osim.InverseDynamicsTool()
    _set_first(tool, ["setModelFileName", "setModelFilename", "set_model_file"],
               str(model_path))
    tool.setCoordinatesFileName(str(coords))
    tool.setOutputGenForceFileName(out_name)  # basename: resultsDir supplies the folder
    tool.setResultsDir(str(out_dir))
    if args.get("external_loads_file"):
        tool.setExternalLoadsFileName(_abs_input(args["external_loads_file"]))
    if args.get("lowpass_cutoff") is not None:
        tool.setLowpassCutoffFrequency(float(args["lowpass_cutoff"]))
    if args.get("time_range"):
        tr = args["time_range"]
        tool.setStartTime(float(tr[0]))
        tool.setEndTime(float(tr[1]))
    if args.get("excluded_forces"):
        tool.setExcludedForces(osim.ArrayStr())
        tool.updExcludedForces().append(str(args["excluded_forces"]))

    tool.run()
    _require_output(out_sto, "inverse dynamics")
    return {
        "output_gen_force_file": str(out_sto),
        "results_dir": str(out_dir),
        "size_bytes": out_sto.stat().st_size,
    }


def _xml_escape(value: Any) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _run_analyze_tool(
    args: dict, analysis_name: str, analysis_body: str
) -> tuple[Path, list[str]]:
    """Run one analysis through ``AnalyzeTool`` driven by a setup file.

    The analyses are *not* runnable by attaching them to an in-memory
    ``AnalyzeTool``: ``run()`` rebuilds the tool from its properties and reports
    "no analyses have been set", with the analysis set silently ignored. This was
    verified on both OpenSim 4.2 and 4.6. Going through the setup XML is
    OpenSim's own documented workflow and is the only path that works.
    """
    osim = _osim()
    model_path = resolve_model(args["model"])
    coords = Path(_abs_input(args["coordinates_file"]))
    if not coords.exists():
        raise FileNotFoundError(f"Coordinates file not found: {coords}")
    out_dir = _results_dir(args)

    timing = ""
    if args.get("time_range"):
        t0, t1 = args["time_range"]
        timing = (
            f"    <initial_time>{float(t0)}</initial_time>\n"
            f"    <final_time>{float(t1)}</final_time>\n"
        )
    lowpass = ""
    if args.get("lowpass_cutoff") is not None:
        lowpass = (
            f"    <lowpass_cutoff_frequency>{float(args['lowpass_cutoff'])}"
            f"</lowpass_cutoff_frequency>\n"
        )

    setup = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<OpenSimDocument Version="40000">\n'
        f'  <AnalyzeTool name="{_xml_escape(analysis_name)}">\n'
        f"    <model_file>{_xml_escape(model_path)}</model_file>\n"
        f"    <coordinates_file>{_xml_escape(coords)}</coordinates_file>\n"
        "    <replace_force_set>false</replace_force_set>\n"
        f"    <results_directory>{_xml_escape(out_dir)}</results_directory>\n"
        f"{timing}{lowpass}"
        "    <solve_for_equilibrium>true</solve_for_equilibrium>\n"
        '    <AnalysisSet name="Analyses">\n'
        "      <objects>\n"
        f"{analysis_body}"
        "      </objects>\n"
        "    </AnalysisSet>\n"
        "  </AnalyzeTool>\n"
        "</OpenSimDocument>\n"
    )

    setup_path = out_dir / f"setup_{analysis_name}.xml"
    setup_path.write_text(setup, encoding="utf-8")

    osim.AnalyzeTool(str(setup_path)).run()
    return out_dir, []


def _analysis_time_block(args: dict, indent: str = "          ") -> str:
    block = f"{indent}<on>true</on>\n"
    if args.get("time_range"):
        t0, t1 = args["time_range"]
        block += f"{indent}<start_time>{float(t0)}</start_time>\n"
        block += f"{indent}<end_time>{float(t1)}</end_time>\n"
    return block


def op_run_static_optimization(args: dict) -> dict:
    """Static optimization -> muscle activations and forces."""
    _quiet()
    extra = ""
    if args.get("activation_exponent") is not None:
        extra += (
            f"          <activation_exponent>{float(args['activation_exponent'])}"
            f"</activation_exponent>\n"
        )
    if args.get("use_muscle_physiology") is not None:
        val = "true" if args["use_muscle_physiology"] else "false"
        extra += f"          <use_muscle_physiology>{val}</use_muscle_physiology>\n"

    body = (
        '        <StaticOptimization name="StaticOptimization">\n'
        + _analysis_time_block(args)
        + extra
        + "        </StaticOptimization>\n"
    )
    out_dir, _ = _run_analyze_tool(args, "static_optimization", body)
    return {
        "results_dir": str(out_dir),
        "wrote": _require_any_output(
            out_dir, "*StaticOptimization_*.sto*", "static optimization"
        ),
    }


def op_run_muscle_analysis(args: dict) -> dict:
    """Muscle lengths, moment arms, velocities, activations, forces."""
    _quiet()
    moments = "true" if args.get("compute_moments", True) else "false"
    body = (
        '        <MuscleAnalysis name="MuscleAnalysis">\n'
        + _analysis_time_block(args)
        + f"          <compute_moments>{moments}</compute_moments>\n"
        + "        </MuscleAnalysis>\n"
    )
    out_dir, _ = _run_analyze_tool(args, "muscle_analysis", body)
    return {
        "results_dir": str(out_dir),
        "wrote": _require_any_output(
            out_dir, "*MuscleAnalysis_*.sto*", "muscle analysis"
        ),
    }


def op_compute_joint_reactions(args: dict) -> dict:
    """Joint reaction (contact) forces from a motion + external loads."""
    _quiet()
    extra = ""
    if args.get("forces_file"):
        extra += (
            f"          <forces_file>{_xml_escape(_abs_input(args['forces_file']))}"
            f"</forces_file>\n"
        )
    if args.get("in_frame"):
        extra += f"          <in_frame>{_xml_escape(args['in_frame'])}</in_frame>\n"
    if args.get("joint_names"):
        extra += (
            f"          <joint_names>{_xml_escape(' '.join(args['joint_names']))}"
            f"</joint_names>\n"
        )

    body = (
        '        <JointReaction name="JointReaction">\n'
        + _analysis_time_block(args)
        + extra
        + "        </JointReaction>\n"
    )
    out_dir, _ = _run_analyze_tool(args, "joint_reaction", body)
    return {
        "results_dir": str(out_dir),
        "wrote": _require_any_output(
            out_dir, "*JointReaction_*.sto*", "joint reaction analysis"
        ),
    }


def op_run_forward_dynamics(args: dict) -> dict:
    """Forward integration of the model under its controls."""
    _quiet()
    osim = _osim()
    model_path = resolve_model(args["model"])
    out_dir = _results_dir(args)

    tool = osim.ForwardTool()
    _set_first(tool, ["setModelFilename", "setModelFileName", "set_model_file"],
               str(model_path))
    tool.setResultsDir(str(out_dir))
    if args.get("controls_file"):
        tool.setControlsFileName(_abs_input(args["controls_file"]))
    if args.get("states_file"):
        tool.setStatesFileName(_abs_input(args["states_file"]))
    if args.get("time_range"):
        tr = args["time_range"]
        tool.setInitialTime(float(tr[0]))
        tool.setFinalTime(float(tr[1]))
    if args.get("max_steps") is not None:
        tool.setMaximumNumberOfSteps(int(args["max_steps"]))

    tool.run()
    return {
        "results_dir": str(out_dir),
        "wrote": sorted(p.name for p in out_dir.glob("*.sto")),
    }


# --------------------------------------------------------------------------- #
# results
# --------------------------------------------------------------------------- #


def op_read_storage(args: dict) -> dict:
    """Summarize a .sto/.mot result file."""
    _quiet()
    path = Path(args["file_path"])
    if not path.exists():
        raise FileNotFoundError(f"Result file not found: {path}")
    storage = _read_storage_any(path)
    columns = _storage_data_columns(storage)
    preview_rows = int(args.get("preview_rows", 5))

    times = _time_array(storage)
    wanted = columns[: int(args.get("stat_columns", 30))]
    series = {c: _col_array(storage, c) for c in wanted}

    preview = []
    for i in range(min(preview_rows, storage.getSize())):
        row = {"time": times[i] if i < len(times) else None}
        for c in columns[: int(args.get("preview_columns", 8))]:
            vals = series.get(c) or _col_array(storage, c)
            if i < len(vals):
                row[c] = vals[i]
        preview.append(row)

    return {
        "file_path": str(path),
        "rows": storage.getSize(),
        "columns": columns,
        "num_columns": len(columns),
        "in_degrees": storage.isInDegrees(),
        "time_range": [storage.getFirstTime(), storage.getLastTime()],
        "statistics": {c: _stat(v) for c, v in series.items()},
        "preview": preview,
    }


def op_get_result_column(args: dict) -> dict:
    _quiet()
    path = Path(args["file_path"])
    if not path.exists():
        raise FileNotFoundError(f"Result file not found: {path}")
    storage = _read_storage_any(path)
    column = args["column"]

    available = _storage_columns(storage)
    if column not in available:
        raise KeyError(f"No column {column!r}. Available: {', '.join(available)}")
    values = _col_array(storage, column)
    out = {"column": column, "statistics": _stat(values)}
    if args.get("full"):
        out["time"] = _time_array(storage)
        out["values"] = values
    return out


def op_plot_results(args: dict) -> dict:
    """Plot one or more columns of a result file to a PNG."""
    _quiet()
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    path = Path(args["file_path"])
    if not path.exists():
        raise FileNotFoundError(f"Result file not found: {path}")
    storage = _read_storage_any(path)
    available = _storage_columns(storage)
    wanted = args.get("columns") or _storage_data_columns(storage)[:3]
    missing = [c for c in wanted if c not in available]
    if missing:
        raise KeyError(f"No such columns: {missing}. Available: {', '.join(available)}")

    times = _time_array(storage)
    fig, ax = plt.subplots(figsize=(10, 4.5))
    for c in wanted:
        ax.plot(times, _col_array(storage, c), label=c)
    ax.set_xlabel("time (s)")
    ax.set_ylabel(args.get("ylabel") or "value")
    ax.set_title(args.get("title") or path.name)
    ax.legend(fontsize=8)
    ax.grid(alpha=0.3)
    fig.tight_layout()

    out = Path(
        args.get("output_image")
        or path.with_suffix("").with_name(path.stem + "_plot.png")
    )
    _ensure_dir(out.parent)
    fig.savefig(out, dpi=int(args.get("dpi", 120)))
    plt.close(fig)
    return {"output_image": str(out), "columns": wanted, "points": len(times)}


# --------------------------------------------------------------------------- #
# registry / dispatch
# --------------------------------------------------------------------------- #

OPS: dict[str, Callable[[dict], dict]] = {
    "get_installation_info": op_get_installation_info,
    "list_models": op_list_models,
    "download_model": op_download_model,
    "load_model": op_load_model,
    "inspect_model": op_inspect_model,
    "set_coordinate_values": op_set_coordinate_values,
    "save_model": op_save_model,
    "scale_model": op_scale_model,
    "run_inverse_kinematics": op_run_inverse_kinematics,
    "run_inverse_dynamics": op_run_inverse_dynamics,
    "run_static_optimization": op_run_static_optimization,
    "run_muscle_analysis": op_run_muscle_analysis,
    "compute_joint_reactions": op_compute_joint_reactions,
    "run_forward_dynamics": op_run_forward_dynamics,
    "read_storage": op_read_storage,
    "get_result_column": op_get_result_column,
    "plot_results": op_plot_results,
}


def run_op(op: str, args: dict) -> dict:
    fn = OPS.get(op)
    if fn is None:
        return RunResult(
            ok=False, op=op, error=f"Unknown op {op!r}. Known: {', '.join(sorted(OPS))}"
        ).to_dict()
    return _safe_call(op, fn, args)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run one OpenSim operation, print JSON.")
    ap.add_argument("--op", required=True, help="operation name")
    ap.add_argument("--args-json", default="{}", help="JSON object of arguments")
    ap.add_argument("--list-ops", action="store_true", help="print available ops and exit")
    ns = ap.parse_args(argv)

    if ns.list_ops:
        print(json.dumps(sorted(OPS), indent=2))
        return 0

    try:
        args = json.loads(ns.args_json) if ns.args_json else {}
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "op": ns.op, "error": f"bad --args-json: {exc}"}))
        return 2

    out = run_op(ns.op, args)
    # ensure_ascii keeps stdout safe under a GBK console.
    print(json.dumps(out, ensure_ascii=False, default=str))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
