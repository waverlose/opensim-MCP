# The standard OpenSim pipeline

The classic inverse-dynamics workflow runs in one direction and each step's
output is the next step's input:

```
generic .osim
   │  scale_model          (mass/height + markers -> subject-specific geometry)
   ▼
scaled .osim
   │  run_inverse_kinematics   .trc markers -> .mot joint angles
   ▼
ik.mot
   │  run_inverse_dynamics     ik.mot + GRF -> .sto joint moments
   ▼
id.sto ──┬── run_static_optimization   -> muscle activations / forces
         ├── run_muscle_analysis       -> lengths, moment arms, velocities
         └── compute_joint_reactions   -> joint contact forces
```

## Order of operations

1. **`get_installation_info`** — confirm OpenSim is reachable before anything else.
2. **`list_models`** / **`download_model`** — make sure the model is on disk.
3. **`load_model`** — sanity check: are the muscle and coordinate counts what you
   expect? A model that loads with 0 muscles is the wrong file.
4. **`scale_model`** — only needed when the subject differs from the generic model.
   Without a scale set this step does little; if you have no scale set, say so
   instead of silently skipping scaling.
5. **`run_inverse_kinematics`** — needs a `.trc` (experimental markers) whose
   marker names match the model. Mismatches are the most common failure; the tool
   will still run and produce a plausible-looking but wrong result, so check the
   marker count in the `.trc` against `marker_names` from `load_model`.
6. **`run_inverse_dynamics`** — pass `external_loads_file` whenever the trial had
   ground contact. Omitting it makes the solver attribute the ground reaction to
   muscle moments and inflates every number.
7. **Analyses** — static optimization and friends need the *coordinates* file
   (the IK output) and the external loads too.

## Units and conventions

| Quantity | Unit |
|---|---|
| Coordinate values, joint angles | radians (unless the model is flagged in degrees) |
| Moments | N·m |
| Forces | N |
| Marker positions | metres |
| Time | seconds |

`read_storage` reports `in_degrees` per file — check it before quoting angles.

## Filtering

`InverseDynamicsTool` and the analyses expose `lowpass_cutoff` (Hz). The
conventional choice is **6 Hz for kinematics and ground reaction forces** in
walking, and higher (10–20 Hz) for running. Whatever you pick, state it: a
filter cutoff is a methodological choice, not a default.

## Verification checks

After a run, before reporting numbers:

* Joint angles should stay within the coordinate ranges reported by
  `inspect_model`.
* Static-optimization activations must lie in **[0, 1]**. Values above 1 mean the
  model cannot produce the required moment — usually a bad IK result or a missing
  external load.
* Joint reaction forces are typically several times body weight at the hip and
  knee in gait. Orders of magnitude off indicates a unit or filtering error.

## Files this pipeline produces

| Step | Typical file |
|---|---|
| IK | `*_ik.mot` |
| ID | `*_id.sto` |
| Static optimization | `StaticOptimization_activation.sto`, `StaticOptimization_force.sto` |
| Muscle analysis | `MuscleAnalysis_Length.sto`, `_MomentArm.sto`, `_Activation.sto`, `_Force.sto` |
| Joint reaction | `*_JointReaction_ReactionLoads.sto` |

Read them with `read_storage`, inspect a series with `get_result_column`, and
draw them with `plot_results`.
