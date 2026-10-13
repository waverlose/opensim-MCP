# OpenSim Python API cheat sheet

Verified against the OpenSim 4.2 bindings by introspection (`dir()` + live calls).
4.6 keeps the same shape; where an accessor name has moved between versions the
server tries several spellings, and this file marks the risky ones.

## Modules

```python
import opensim as osim
```

In the `run_python` session these are already bound, so a snippet can start
using them directly:

| Name | Is |
|---|---|
| `osim` / `opensim` | the OpenSim module |
| `np` | numpy |
| `resolve_model("gait2392")` | registry name or path -> an existing `.osim` `Path` |
| `models_dir` | the model cache directory, as a string |
| `runner` | the `opensim_runner` module, for its helpers |
| `Path`, `os`, `sys`, `json` | the usual |

`resolve_model` is the convenient one: it accepts a registered name *or* a path,
and raises an actionable error if the model is registered but not yet downloaded.

| Module | What lives there |
|---|---|
| `osim` (top level) | `Model`, `Body`, `Joint`, `Muscle`, `Marker`, `Coordinate`, solvers |
| `osim.tools` | `ScaleTool`, `InverseKinematicsTool`, `InverseDynamicsTool`, `AnalyzeTool`, `ForwardTool`, `CMCTool`, `RRATool`, `IMUInverseKinematicsTool` |
| `osim.analyses` | `StaticOptimization`, `MuscleAnalysis`, `JointReaction`, `BodyKinematics`, `PointKinematics` |
| `osim.moco` | `MocoStudy`, `MocoTrack`, `MocoInverse`, `MocoSolution`, `MocoCasADiSolver` |
| `osim.common` | `TimeSeriesTable`, `Storage`, `STOFileAdapter`, `TRCFileAdapter`, `C3DFileAdapter` |

`osim.GetVersion()` returns e.g. `4.6`.


## Model

```python
model = osim.Model("gait2392.osim")
state = model.initSystem()          # REQUIRED before most queries

model.getName()                     # 'gait2392_thelen2003muscle'
model.getNumBodies()                # 23
model.getNumJoints()                # 23
model.getNumCoordinates()           # 23
model.getNumMarkers()               # 43
model.getNumStateVariables()
model.getMuscles().getSize()        # 92   <-- NOT getNumMuscles(), which does not exist
model.getActuators().getSize()      #      <-- NOT getNumActuators()
model.getForceSet().getSize()       #      <-- NOT getNumForces()
model.getTotalMass(state)           # kg   <-- takes a STATE; getMass() does not exist
```

### Traversing

```python
model.getCoordinateSet().getSize()
model.getCoordinateSet().get(0).getName()
model.getCoordinateSet().getIndex("knee_angle_r")   # -1 if absent
model.getBodySet() / getJointSet() / getMarkerSet()
model.getMuscles().get(0).getMaxIsometricForce()
```

SWIG sets are indexed with `.get(i)` and measured with `.getSize()`. They are
**not** Python iterables, and `list(set)` does not work.

### Reading and setting coordinates

```python
c = model.getCoordinateSet().get("knee_angle_r")
c.getValue(state)          # radians (unless the model is flagged in degrees)
c.setValue(state, 0.5)
c.getRangeMin(), c.getRangeMax()
c.getDefaultValue()
model.realizePosition(state)      # after setting values
model.printToXML("edited.osim")   # persist
```

## Tools

Two families of accessor exist and they disagree on spelling. Prefer the one the
server already handles, or use `set_<snake_case_property>`.

| Class | Model accessor | Notes |
|---|---|---|
| `InverseKinematicsTool` | `set_model_file` (or `setModel(model)`) | no `setModelFileName` in 4.2 |
| `InverseDynamicsTool` | `setModelFileName` | capital N |
| `AnalyzeTool` | `setModelFilename` | **lowercase n** |
| `ForwardTool` | `setModelFilename` | lowercase n |
| `ScaleTool` | *(none)* | set the model via `getGenericModelMaker()` |

### InverseKinematicsTool

```python
ik = osim.InverseKinematicsTool()
ik.setModel(osim.Model("scaled.osim"))     # or ik.set_model_file(path)
ik.setMarkerDataFileName("walk.trc")
ik.setOutputMotionFileName("ik.mot")
ik.setResultsDir("results")
ik.setStartTime(0.0); ik.setEndTime(1.0)
ik.set_accuracy(1e-5)                      # snake_case, not setAccuracy()
ik.set_report_errors(True)
ik.run()
```

> **Output paths differ between tools.** `InverseKinematicsTool` takes an
> **absolute** path for `output_motion_file`; given a bare filename it writes to
> the *current working directory* and ignores `resultsDir`. `InverseDynamicsTool`
> does the opposite: it joins `resultsDir` with the name you give, so it wants a
> *basename*. Both verified on 4.6. If a tool "succeeds" but you cannot find the
> file, look in the working directory before assuming it failed.

### InverseDynamicsTool

```python
id_tool = osim.InverseDynamicsTool()
id_tool.setModelFileName("scaled.osim")
id_tool.setCoordinatesFileName("ik.mot")
id_tool.setOutputGenForceFileName("id.sto")
id_tool.setExternalLoadsFileName("grf.xml")   # omit only if the trial is free
id_tool.setLowpassCutoffFrequency(6.0)
id_tool.setStartTime(0.0); id_tool.setEndTime(1.0)
id_tool.run()
```

### Analyses go through an AnalyzeTool *setup file*

`StaticOptimization`, `MuscleAnalysis` and `JointReaction` are *analyses*: they
are attached to an `AnalyzeTool`, not run directly. Two things about this will
cost you an afternoon if you do not know them.

**1. Attaching an analysis in memory does not work.** This looks right and fails:

```python
tool = osim.AnalyzeTool()
tool.setModel(model)
tool.updAnalysisSet().cloneAndAppend(so)   # getAnalysisSet().getSize() == 1
tool.run()
# RuntimeError: AnalysisTool.run: ERROR- no analyses have been set.
```

`AnalyzeTool.run()` rebuilds itself from its properties, and the analysis set is
not one of them — the object you appended is discarded. Confirmed on both 4.2 and
4.6; it is not a version quirk. Go through a setup file:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<OpenSimDocument Version="40000">
  <AnalyzeTool name="so">
    <model_file>scaled.osim</model_file>
    <coordinates_file>ik.mot</coordinates_file>
    <replace_force_set>false</replace_force_set>
    <results_directory>results</results_directory>
    <initial_time>0.4</initial_time>
    <final_time>0.6</final_time>
    <AnalysisSet name="Analyses">
      <objects>
        <StaticOptimization name="StaticOptimization">
          <on>true</on>
          <start_time>0.4</start_time>
          <end_time>0.6</end_time>
        </StaticOptimization>
      </objects>
    </AnalysisSet>
  </AnalyzeTool>
</OpenSimDocument>
```

```python
osim.AnalyzeTool("setup.xml").run()    # this is the path that works
```

The analyses' outputs land in `results_directory` prefixed with the tool name,
e.g. `static_optimization_StaticOptimization_activation.sto`.

**2. `setReplaceForceSet(False)` is not optional.** `AnalyzeTool` defaults to
replacing the model's force set, which deletes every muscle. Static optimization
would then "succeed" by optimizing over nothing and report zero activation
everywhere. Set it in the setup file as shown above.

`AnalyzeTool` uses `initial_time` / `final_time`. It has no `end_time`.

| Analysis | XML element / key setters | Outputs |
|---|---|---|
| `StaticOptimization` | `<StaticOptimization>`, `activation_exponent`, `use_muscle_physiology` | `*_StaticOptimization_activation.sto`, `*_StaticOptimization_force.sto` |
| `MuscleAnalysis` | `<MuscleAnalysis>`, `compute_moments` | `*_MuscleAnalysis_Length.sto`, `_MomentArm_<coord>.sto`, `_Activation.sto`, `_FiberForce.sto`, … |
| `JointReaction` | `<JointReaction>`, `forces_file`, `in_frame`, `joint_names` | `*_JointReaction_ReactionLoads.sto` |

### ScaleTool

`ScaleTool` does not derive from `Tool`, so there is no top-level model setter.

```python
tool = osim.ScaleTool()
tool.getGenericModelMaker().setModelFileName("generic.osim")
tool.setSubjectMass(72.0); tool.setSubjectHeight(1.75)

scaler = tool.getModelScaler()
scaler.setApply(True)
scaler.setScaleSetFile("gait_scale_setup.xml")
scaler.setMarkerFileName("static.trc")
scaler.setOutputModelFileName("scaled.osim")

placer = tool.getMarkerPlacer()
placer.setMarkerFileName("static.trc")
placer.setOutputModelFileName("scaled.osim")
placer.setOutputMotionFileName("static_output.mot")

tool.run()
```

Scaling by mass/height alone does **not** change segment lengths; you need a
scale-set or measurement file for that.

## Reading results

```python
st = osim.Storage("results.sto")           # handles .sto and .mot
st.getSize()                               # rows
labels = st.getColumnLabels()              # ArrayStr - NOT iterable
[labels.get(i) for i in range(labels.getSize())]
st.getTimeColumn()                         # ArrayDouble
st.getDataColumn("knee_angle_r")           # ArrayDouble
st.getFirstTime(), st.getLastTime()
st.isInDegrees()
st.exportToTable()                         # -> TimeSeriesTable
```

`Storage` has **no** `getNumColumns()`; use `getColumnLabels().getSize()`.
`ArrayStr`/`ArrayDouble` are SWIG arrays: index with `.get(i)`, size with
`.getSize()`, and they are not iterable.

### Marker and force-plate files

```python
# TRCFileAdapter.read() returns a MAP of tables, not a table
tables = osim.TRCFileAdapter().read("walk.trc")
markers = tables["markers"]                # TimeSeriesTableVec3
markers.getNumRows(), markers.getNumColumns()

# C3D gives marker and force data together
tables = osim.C3DFileAdapter().read("walking.c3d")
tables["markers"], tables["forces"]
```

## Logging

OpenSim writes to the C++ streams, so `contextlib.redirect_stdout` will **not**
capture it. To quiet it:

```python
osim.Logger.setLevelString("Error")   # or "Warn", "Info", "Debug"
```

**On 4.6, loading a model prints a summary table to stdout** (MODEL / bodies /
joints / muscles / …). If you are writing a script whose stdout is parsed as
JSON, either set the level to `Error` first, redirect file descriptors, or make
the parser ignore non-JSON lines. The MCP worker does the last two — see
`opensim_worker.py`.

Missing `.vtp` geometry files produce a wall of warnings in headless runs; they
are cosmetic and do not affect IK/ID/optimization.

## Moco

```python
study = osim.MocoStudy()
problem = study.updProblem()
problem.setModel(osim.Model("rajagopal2016.osim"))
...
solution = study.solve()
```

See `opensim://moco-recipe` for full, runnable recipes.
