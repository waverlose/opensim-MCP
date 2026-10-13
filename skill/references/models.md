# The built-in model library

Models come from `opensim-org/opensim-models` (branch `master`) and are cached in
`src/models/`. Only the `.osim` is downloaded: headless simulation needs no mesh
geometry, and the full repository is ~298 MB.

| Name | DOF / muscles | Use it for |
|---|---|---|
| `arm26` | 2 / 6 | the smallest sanity check — and the only registered model that carries a marker set (3 markers), so it is the one to use for inverse kinematics |
| `gait10dof18musc` | 10 / 18 | full IK→ID→SO pipeline that still solves in seconds |
| `gait2354` | 23 / 54 | classic inverse dynamics |
| `gait2392` | 23 / 92 | the most widely used gait model |
| `gait2392_millard` | 23 / 92 | as above, Millard2012 equilibrium muscles |
| `rajagopal2016` | 37 / 80 | full body; the recommended base for Moco |
| `wrist` | — | upper limb / hand studies |

> **The gait models here have no markers.** `opensim-models` ships them without a
> marker set — markers live in separate scale-set files — so they cannot drive
> `run_inverse_kinematics` at all. Point IK and the analyses at `arm26`, or bring
> a model of your own that includes markers. `load_model` reports the count, so
> check `markers` before reaching for IK.

```python
list_models()                    # what is registered and what is downloaded
download_model("gait2392")       # fetch into src/models/
load_model("gait2392")           # by name ...
load_model("D:/data/my_model.osim")   # ... or by path
```

## Using your own models

Any tool that takes a `model` argument accepts an absolute path to a `.osim`.
Nothing in this server requires the built-in library.

A `.osim` may reference external files:

* **Geometry (`.vtp`)** — only needed for visualization. Headless runs emit
  "Couldn't find file 'x.vtp'" warnings and continue correctly. Ignore them.
* **Scale sets, marker sets, external loads** — needed for the corresponding
  steps, and must be passed explicitly (e.g. `scale_set_file`, `external_loads_file`).

## Adding a model to the registry

Append to `src/models_registry.json`:

```json
"my_model": {
  "filename": "my_model.osim",
  "url": "https://raw.githubusercontent.com/<owner>/<repo>/<branch>/<path>.osim",
  "description": "what it is and what it is good for"
}
```
