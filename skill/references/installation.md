# How this server reaches OpenSim

```
MCP client (Claude Code)
   │  stdio, JSON-RPC
   ▼
run_mcp.bat  ->  <opensim-env>\python.exe -m opensim_mcp_server
   │
   ├── structured tools ──> one subprocess per call:  python -m opensim_runner --op ... --args-json ...
   └── run_python      ──> long-lived worker:         python -m opensim_worker   (JSON lines on stdin/stdout)
```

## Why two paths

* **Structured tools** map onto OpenSim's Tool API, which is file-in/file-out, so
  one fresh process per call is the natural fit. If OpenSim crashes on a bad model,
  only that subprocess dies.
* **`run_python`** needs a *session*: variables, loaded models and Moco study
  objects persist between calls. The worker is also its own process, so a native
  crash there cannot take down the MCP server — the server notices the dead worker
  and starts a new one.

## Interpreter

`run_mcp.bat` probes candidates in order and uses the first one where
`import opensim` succeeds:

1. `%OPENSIM_MCP_PYTHON%`
2. `.venv\Scripts\python.exe` (a venv made by `setup_env.ps1 -Venv`)
3. the active conda environment (`%CONDA_PREFIX%`)
4. a conda env named `opensim-mcp` under the usual install roots
5. any `python` on `PATH`

Probing rather than assuming is deliberate: a python on `PATH` that cannot import
OpenSim would otherwise fail much later with a confusing error. If nothing
qualifies, the launcher explains what to do and exits non-zero, and
`get_installation_info` reports the problem instead of hanging.

## Installing / repairing

```powershell
powershell -ExecutionPolicy Bypass -File setup_env.ps1             # conda env
powershell -ExecutionPolicy Bypass -File setup_env.ps1 -Venv       # project .venv, no conda needed
```

Behind a slow mirror, add
`-PipIndexUrl https://pypi.tuna.tsinghua.edu.cn/simple` (or your local mirror).

Verify:

```
python -c "import opensim; print(opensim.GetVersion())"
```

## Two gotchas worth remembering

* **Do not `pip install casadi` separately.** OpenSim 4.6 bundles its own CasADi
  for Moco; two copies on the path break the solver (`opensim-org/opensim-fitter`
  documents the same conflict).
* **Prefer the pip wheel over a manual SDK.** A GUI/SDK install (e.g. OpenSim
  4.2) ships Python bindings built for one specific interpreter — usually Python
  3.7 — and needs that SDK's `bin` directory on `PATH`. The pip wheel has none of
  that. If you must use a manual SDK, set `OPENSIM_SDK_PYTHON` to its
  `sdk\Python` directory and `OPENSIM_BIN_HINT` to its `bin` directory.

## Registering with Claude Code

Add to the `mcpServers` block of `~/.claude.json`:

```json
"opensim": {
  "type": "stdio",
  "command": "C:\\path\\to\\opensim-mcp-server\\run_mcp.bat",
  "args": [],
  "env": { "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8" }
}
```

Restart the client, then call `get_installation_info` to confirm.
