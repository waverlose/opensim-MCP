# opensim-mcp-server

An [MCP](https://modelcontextprotocol.io) server that lets an AI agent drive
**[OpenSim](https://opensim.stanford.edu/)** musculoskeletal simulation: scale a
model to a subject, run inverse kinematics and inverse dynamics, static
optimization, muscle analysis and joint-reaction analysis, read the results — and
drop into a persistent Python session for anything else, including OpenSim Moco.

## Why this exists

There was no OpenSim MCP server before this one. The repositories that surface
under "opensim mcp" are all **[OpenSimulator](http://opensimulator.org/)** — the
Second Life–style virtual-world server — which shares the name and nothing else.
Everything here builds on the official `opensim-org` toolchain rather than
reimplementing it.

| Built on | For |
|---|---|
| [`opensim-org/opensim-core`](https://github.com/opensim-org/opensim-core) | the engine and its Python bindings |
| [`opensim-org/opensim-models`](https://github.com/opensim-org/opensim-models) | the model registry this server downloads from |
| the official `opensim` PyPI wheel (4.6) | the runtime |

The design follows the shape MathWorks uses for their official MATLAB MCP server:
introspection + structured tools + a code escape hatch + documentation exposed as
resources.

## Requirements

* Python 3.11–3.13 (the `opensim` wheels cover exactly those)
* Windows, macOS or Linux — `setup_env.ps1` and `run_mcp.bat` are the Windows
  conveniences; on macOS/Linux create the environment however you like and see
  "Register" below.

## Install

Windows, with conda:

```powershell
powershell -ExecutionPolicy Bypass -File setup_env.ps1
```

Windows or any OS, without conda (creates a project-local `.venv`):

```powershell
powershell -ExecutionPolicy Bypass -File setup_env.ps1 -Venv
```

Either way this installs `opensim==4.6`, `mcp`, `numpy`, `scipy` and
`matplotlib`, then verifies the import.

Verify by hand:

```
python -c "import opensim; print(opensim.GetVersion())"   # -> 4.6-...
```

To use an OpenSim you already have instead, point `OPENSIM_MCP_PYTHON` at its
interpreter (and `OPENSIM_BIN_HINT` at its `bin` directory if it is a manual SDK
rather than a pip wheel).

## Register with an MCP client

`run_mcp.bat` locates a Python that can `import opensim` by probing candidates in
order, so it works whether you installed with conda or a venv. Point your client
at it (Claude Code shown; other clients use the same fields):

```json
"opensim": {
  "type": "stdio",
  "command": "C:\\path\\to\\opensim-mcp-server\\run_mcp.bat",
  "args": [],
  "env": { "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8" }
}
```

On macOS or Linux, use an equivalent script — the server itself is just a stdio
program:

```
OPENSIM_MCP_PYTHON=/path/to/python  python -m opensim_mcp_server
```

with the repository's `src/` directory on `PYTHONPATH`. Then call
`get_installation_info` to confirm it is up.

### Optional: the agent-facing skill

`skill/` holds instructions that teach an agent the workflows and the API traps.
Copy it into your client's skills directory to have it picked up automatically:

```powershell
Copy-Item -Recurse skill "$env:USERPROFILE\.claude\skills\opensim-mcp"
```

The same content is served over MCP as `opensim://` resources, so the skill is a
convenience, not a requirement.

## Tools

**Environment** — `get_installation_info`, `list_models`, `download_model`

**Model** — `load_model`, `inspect_model`, `set_coordinate_values`, `save_model`

**Pipeline** — `scale_model`, `run_inverse_kinematics`, `run_inverse_dynamics`,
`run_static_optimization`, `run_muscle_analysis`, `compute_joint_reactions`,
`run_forward_dynamics`

**Results** — `read_storage`, `get_result_column`, `plot_results`

**Escape hatch** — `run_python`, `run_script`, `session_info`, `reset_session`

**Resources** — `opensim://workflows`, `opensim://api-cheatsheet`,
`opensim://models`, `opensim://moco-recipe`, `opensim://installation`

## Architecture

```
run_mcp.bat -> python -m opensim_mcp_server
   ├── structured tools -> one subprocess per call (python -m opensim_runner --op ...)
   └── run_python       -> long-lived worker (python -m opensim_worker)
```

Two execution paths, each matched to its job:

* OpenSim's Tool API is **file-in / file-out**, so the structured pipeline tools
  each get a fresh process — the natural fit, and a native crash kills only that
  subprocess instead of the server.
* `run_python` needs a **session**: variables, loaded models and Moco study
  objects persist between calls. The worker is also its own process, so a
  segfault inside OpenSim cannot take down the MCP server — the server notices
  the dead worker and starts a new one.

OpenSim prints to the C++ streams, so the worker redirects file descriptors
during user code rather than relying on Python's `redirect_stdout`; that is what
keeps model-loading output out of the JSON-RPC channel.

## Models

`src/models_registry.json` lists models fetched on demand from
`opensim-org/opensim-models`. Only `.osim` files are downloaded — headless
simulation needs no mesh geometry, and the full repository is ~298 MB. Add your
own entries freely; any tool also accepts an absolute path to a `.osim`.

```python
list_models()
download_model("gait2392")
load_model("gait2392")
load_model("D:/data/my_model.osim")
```

## Tests

```
python -m pytest tests -q
```

The suite needs nothing beyond the pip package: models are downloaded on first
use and the inputs are generated, including a marker file synthesised from a
known pose so inverse kinematics is checked for *correctness* (does it recover
the pose?) rather than merely for producing a file.

## Notes

* Do **not** additionally `pip install casadi` — OpenSim ships its own for Moco,
  and two copies on the path conflict.
* Missing `.vtp` geometry warnings in headless runs are cosmetic.
* `pyproject.toml` deliberately omits `opensim` from `dependencies`: it is a
  ~78 MB wheel whose build must match the interpreter, so `setup_env.ps1`
  installs it explicitly.
* Documented OpenSim API traps (output-path rules that differ per tool, why the
  analyses must run from a setup file, accessor names that changed between
  versions) are in [`skill/references/api-cheatsheet.md`](skill/references/api-cheatsheet.md).

## License

MIT — see [LICENSE](LICENSE).
