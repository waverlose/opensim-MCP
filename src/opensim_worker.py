"""Persistent OpenSim session for ``run_python`` / ``run_script``.

Protocol: one JSON request per line on stdin, one JSON response per line on
stdout.

    -> {"id": 1, "op": "exec", "source": "import opensim", "cwd": "C:/x"}
    <- {"id": 1, "ok": true, "stdout": "...", "stderr": "...", "result": ...}

The worker runs in its own process for two reasons:

* **Crash isolation.** OpenSim is native code and a malformed model or a
  diverging solver can take the process down. The server notices the dead
  worker and starts a fresh one instead of dying with it.
* **Session state.** Variables, loaded models and solver objects persist
  between calls, like a MATLAB or IPython session.

OpenSim prints to the C++ streams directly, so capturing Python's ``sys.stdout``
is not enough: user code runs with file descriptors 1 and 2 redirected to
temporary files, which catches native and Python output alike.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

# Extra globals the user's code may rely on between calls.
_session: dict = {}


def _base_namespace() -> dict:
    import numpy as np

    ns: dict = {
        "__name__": "__main__",
        "__builtins__": __builtins__,
        "np": np,
        "os": os,
        "sys": sys,
        "Path": Path,
        "json": json,
    }
    try:
        import opensim

        ns["opensim"] = opensim
        ns["osim"] = opensim
    except Exception as exc:  # surfaced to the agent on first exec
        ns["_opensim_import_error"] = f"{type(exc).__name__}: {exc}"

    # Model lookup, so code in the session can say resolve_model('gait2392')
    # instead of assembling a cache path by hand.
    try:
        import opensim_runner as runner

        ns["runner"] = runner
        ns["resolve_model"] = runner.resolve_model
        ns["models_dir"] = str(runner.MODELS_DIR)
    except Exception:
        pass
    return ns


def _run_captured(source: str, ns: dict, cwd: str | None) -> dict:
    """Exec source with fds 1/2 redirected, returning captured text."""
    prev_cwd = os.getcwd()
    out_f = tempfile.TemporaryFile(mode="w+b")
    err_f = tempfile.TemporaryFile(mode="w+b")
    saved1, saved2 = os.dup(1), os.dup(2)
    ok, error = True, None

    try:
        if cwd:
            os.makedirs(cwd, exist_ok=True)
            os.chdir(cwd)
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(out_f.fileno(), 1)
        os.dup2(err_f.fileno(), 2)
        try:
            code = compile(source, "<opensim-mcp>", "exec")
            exec(code, ns)  # noqa: S102 - this tool's whole purpose
        except BaseException as exc:  # noqa: BLE001
            ok = False
            error = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            os.dup2(saved1, 1)
            os.dup2(saved2, 2)
    finally:
        os.chdir(prev_cwd)

    out_f.seek(0)
    err_f.seek(0)
    stdout = out_f.read().decode("utf-8", "replace")
    stderr = err_f.read().decode("utf-8", "replace")
    out_f.close()
    err_f.close()
    return {"ok": ok, "error": error, "stdout": stdout, "stderr": stderr}


def _handle(req: dict) -> dict:
    op = req.get("op", "exec")
    cwd = req.get("cwd") or None

    if op == "ping":
        return {"ok": True, "pong": True, "pid": os.getpid()}

    if op == "reset":
        _session.clear()
        _session.update(_base_namespace())
        return {"ok": True, "reset": True}

    if op == "namespace":
        safe = {}
        for k, v in sorted(_session.items()):
            if k.startswith("__"):
                continue
            try:
                safe[k] = f"{type(v).__name__}: {repr(v)[:120]}"
            except Exception:
                safe[k] = f"{type(v).__name__}"
        return {"ok": True, "namespace": safe}

    if op in ("exec", "script"):
        if not _session:
            _session.update(_base_namespace())
        source = req.get("source")
        if op == "script":
            path = Path(req["script_path"])
            if not path.exists():
                return {"ok": False, "error": f"script not found: {path}"}
            source = path.read_text(encoding="utf-8")
            cwd = cwd or str(path.parent)
        if not source:
            return {"ok": False, "error": "no source provided"}

        result = _run_captured(source, _session, cwd)
        if "_opensim_import_error" in _session and "import opensim" not in source:
            result.setdefault("notes", [])
            result["notes"] = [
                "opensim failed to import in this worker: "
                + _session["_opensim_import_error"]
            ]
        result["pid"] = os.getpid()
        return result

    return {"ok": False, "error": f"unknown worker op: {op!r}"}


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            resp = {"id": None, "ok": False, "error": f"bad json: {exc}"}
        else:
            try:
                resp = _handle(req)
            except BaseException as exc:  # noqa: BLE001
                resp = {
                    "id": req.get("id"),
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "stderr": traceback.format_exc(limit=8),
                }
            resp["id"] = req.get("id")

        sys.stdout.write(json.dumps(resp, ensure_ascii=False, default=str) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
