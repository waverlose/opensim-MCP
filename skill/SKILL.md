---
name: opensim-mcp
description: Drive OpenSim musculoskeletal simulation through the `opensim` MCP server. Use when the user wants to run biomechanics simulations — inverse kinematics, inverse dynamics, static optimization, muscle analysis, joint reaction forces, scaling a model to a subject, muscle activations or forces, or OpenSim Moco optimal control. Triggers on: OpenSim, .osim, musculoskeletal model, 肌骨仿真, 逆运动学, 逆动力学, 静态优化, 肌肉激活, 关节反力, gait simulation, 步态仿真, IK/ID, Moco, muscle force.
---

# Driving OpenSim

The `opensim` MCP server exposes OpenSim as tools. Load `opensim://api-cheatsheet`
and `opensim://workflows` before writing simulation code — the API has real
traps (accessor names that changed between versions, SWIG arrays that are not
Python lists, an `AnalyzeTool` flag that silently deletes your muscles).

## Read the resources first

| Resource | When |
|---|---|
| `opensim://workflows` | any IK / ID / optimization work — the pipeline and its units |
| `opensim://api-cheatsheet` | before writing any `run_python` code |
| `opensim://models` | choosing a model |
| `opensim://moco-recipe` | Moco optimal control |
| `opensim://installation` | server not reachable, or a weird import error |

## Two ways to work

**Structured tools** — preferred. One call per pipeline step, each isolated in
its own process:

```
get_installation_info → load_model → run_inverse_kinematics
                     → run_inverse_dynamics → run_static_optimization
                     → read_storage
```

**`run_python`** — a persistent session with `opensim`/`osim` and `np` already
imported. Use it for anything the structured tools do not cover: model surgery,
solvers, custom analyses, and all of Moco.

## Habits that prevent wasted runs

* **Call `get_installation_info` first.** If OpenSim is unreachable, every other
  call fails the same way, and you will have burned several tool calls learning it.
* **`load_model` before simulating, and read the counts.** A model reporting 0
  muscles is the wrong file. Check `marker_names` against the `.trc` before IK —
  silently mismatched markers still produce a plausible-looking, wrong answer.
* **Inverse dynamics needs the external loads.** If the trial had ground contact,
  omitting `external_loads_file` makes the solver load the muscles with what the
  ground actually carried.
* **Give every long solve a real timeout.** Default is 300 s; IK on a full-body
  model can take much longer. A silent timeout is the worst failure mode —
  pass `timeout` explicitly and prefer large values over retries.
* **Check activations are in [0, 1] and joint forces are a sensible multiple of
  body weight** before reporting numbers. Outside that, something upstream is wrong.
* **Say what you assumed.** Filter cutoff, time range, scaling — these are
  methodological choices. State them rather than picking silently.

## Do not

* Do not `pip install casadi` to fix Moco. OpenSim 4.6 ships its own; two copies
  conflict.
* Do not treat missing `.vtp` geometry warnings as errors — they are cosmetic in
  headless runs.
* Do not assume an accessor name. `Model` has `getMuscles().getSize()`, not
  `getNumMuscles()`; `Storage` has no `getNumColumns()`; `AnalyzeTool` uses
  `setModelFilename` (lowercase n) while `InverseDynamicsTool` uses
  `setModelFileName`. The cheat sheet lists the verified names.
