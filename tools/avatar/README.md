# Nova avatar build

`build_avatar.py` turns the rigged chibi base mesh in
`assets/avatar/source/BaseMesh_Chibi.fbx` into the Nova character and exports
`nova_core/web/avatar/nova_avatar.glb` (skinned, with the morph targets
`blink`, `mouth_open`, `smile`, `confused`). It also renders preview PNGs to
`assets/avatar/previews/`.

## Setup (once)

Blender is used as a Python module, no GUI needed. It needs Python 3.13:

```bash
python3.13 -m venv .venv-avatar
.venv-avatar/bin/pip install bpy
```

## Build

```bash
.venv-avatar/bin/python tools/avatar/build_avatar.py
```

About a minute with previews. Set `NOVA_AVATAR_NO_RENDER=1` to export only.
Then check the result with `pytest tests/nova_core/test_avatar_asset.py`.

## Tweaking

Colors, head/hand/foot scale, eye placement and mouth size live in `PARAMS`
at the top of the script. Hair clumps are the `clumps` list in `build_hair`
(root point, tip point, radius, bend direction, bend amount; points are given
on the head sphere with `sph(azimuth, elevation)`, azimuth 0 = front).

Note: never name a helper script after a stdlib module (e.g. `inspect.py`);
`bpy` imports it by accident and crashes.
