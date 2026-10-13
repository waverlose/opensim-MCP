# OpenSim Moco recipes (via `run_python`)

Moco is OpenSim's optimal-control toolbox and is driven through `run_python`,
because a study object has to persist across calls. Everything below runs in the
persistent session; `opensim`/`osim` and `np` are already imported.

> **Accessor names in Moco do change between releases.** Before trusting a
> signature, list what your build actually has — this is one `run_python` call
> and it prevents a page of guesses:

```python
for name in ["MocoInverse", "MocoTrack", "MocoStudy"]:
    cls = getattr(osim, name)
    print(name, sorted(m for m in dir(cls) if m.startswith(("set", "get", "solve", "initialize"))))
```

Moco needs the **CasADi** solver, which OpenSim bundles. Check it first:

```python
print(osim.MocoCasADiSolver().isAvailable())   # must be True
```
If it is `False`, the install is missing the bundled CasADi — reinstall OpenSim,
and do not `pip install casadi` as a workaround.

## MocoInverse — muscle activations from measured kinematics

Given a model and an inverse-kinematics motion, MocoInverse finds the muscle
activity that best reproduces it. This is the practical replacement for static
optimization when you want smooth, dynamically consistent activations.

```python
import opensim as osim
osim.Logger.setLevelString("Error")

model = osim.Model(str(resolve_model_path("rajagopal2016")))   # or an absolute path
model.initSystem()

# Kinematics: a TableProcessor over the IK .mot. Add the degrees->radians
# conversion when the .mot is in degrees (check with read_storage: in_degrees).
kinematics = osim.TableProcessor("ik.mot")
kinematics.append(osim.TabOpConvertDegreesToRadians())

inverse = osim.MocoInverse()
inverse.setModel(model)
inverse.setKinematics(kinematics)
inverse.set_initial_time(0.0)
inverse.set_final_time(1.0)
inverse.set_mesh_interval(0.02)
inverse.set_convergence_tolerance(1e-2)

# MocoInverse needs the ground reaction forces to balance the body.
inverse.set_external_loads(osim.ExternalLoads("grf.xml", True))

solution = inverse.solve()
sol = solution.getMocoSolution()
sol.write("moco_inverse_solution.sto")

print("success:", sol.success())
print("objective:", sol.getObjective())
```

Then read the result with `read_storage`, or pull a single muscle:

```python
import opensim as osim
t = osim.TimeSeriesTable("moco_inverse_solution.sto")
names = [l for l in t.getColumnLabels() if "activation" in l][:5]
for n in names:
    col = t.getDependentColumn(n)
    print(n, max(col[i] for i in range(col.size())))
```

## MocoTrack — simulate from markers

When you have marker trajectories but no reliable IK, MocoTrack solves for the
motion and the muscle behaviour together.

```python
import opensim as osim
osim.Logger.setLevelString("Error")

track = osim.MocoTrack()
track.setName("gait_tracking")
model = track.initialize()          # returns the model to add forces to

# Ground reaction forces must be added to the model, not set on the study.
# (Build an ExternalLoads from the .mot/.xml and append it to the force set.)
model.addComponent(osim.ExternalLoads("grf.xml", True))

track.setMarkersReferenceFromTRC("walk.trc")
track.set_allow_unused_references(True)
track.set_markers_global_tracking_weight(10.0)

study = track.initialize()
problem = study.updProblem()

effort = osim.MocoControlGoal.safeDownCast(problem.updGoal("control_effort"))
effort.setWeight(0.1)

# Keep the periodicity of a cycle, if this is gait.
periodicity = osim.MocoPeriodicityGoal("periodicity")
for i in range(problem.getModel().getNumCoordinates()):
    coord = problem.getModel().getCoordinateSet().get(i)
    periodicity.addStatePair(osim.MocoPeriodicityGoalPair(coord.getName()))
problem.addGoal(periodicity)

solver = study.initCasADiSolver()
solver.set_num_mesh_intervals(50)
solver.set_optim_convergence_tolerance(1e-2)

solution = study.solve()
solution.write("moco_track_solution.sto")
```

## Reading a Moco solution

A `MocoSolution` is a trajectory, not a `Storage` file, but it writes both ways:

```python
sol = study.solve()
sol.write("solution.sto")                       # Storage-compatible
states = sol.getStatesTrajectory()              # direct access
print(sol.getObjective(), sol.success())
```

## Troubleshooting

| Symptom | Cause |
|---|---|
| Solver reports failure with large constraint violation | Mesh too coarse, or missing external loads |
| Activations pegged at 1.0 | Model cannot produce the motion — check IK quality first |
| `MocoCasADiSolver().isAvailable()` is False | Broken install; do not add a separate `casadi` |
| Solves forever | Tighten `set_num_mesh_intervals` and loosen tolerances while iterating; tighten later |
| Muscle names in the goal do not match | `load_model` → `muscle_names` for the real list |
