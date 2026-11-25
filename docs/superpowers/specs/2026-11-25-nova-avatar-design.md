# Nova 3D Avatar — Design

Date: 2026-11-25
Status: draft, awaiting approval

## Intent

Give Nova a face: a 3D chibi character in the style of a soft vinyl figure that
lives in the web dashboard and visibly reacts while Nova works. It should feel
like the assistant is "there": it listens when you type, thinks while the core
routes the command, talks when it answers and looks confused on errors.

Visual reference (supplied by the user): a chibi boy figure with grey-blue
messy hair with an ahoge, big dark glossy eyes, blush, a small earring, a light
grey and blue zip hoodie over a tee, grey cargo joggers and white-blue sneakers.
Those colors match the dashboard's existing palette (`--bg-dark #090b0e`,
`--accent #2e8fff`).

## Goals

- An original Nova character built from the user's rigged chibi base mesh
  (`BaseMesh_Chibi.fbx`, ~1k faces, 31-bone humanoid rig, no textures, no
  shape keys) and edited towards the reference's look.
- Expressions as morph targets (shape keys): blink, mouth open (talk), smile,
  confused. These carry the "alive" feeling.
- A single `nova_avatar.glb` asset that any client can load.
- An avatar panel in the dashboard Overview tab, rendered with three.js,
  driven by the existing console command flow.

## Non-goals

- Reproducing the reference image detail-for-detail (sleeve wave pattern, logo
  print, zipper teeth, individual hair strands).
- Text-to-speech or real audio lip-sync. Nova has no TTS today; "talking" means
  the mouth animates while the reply text is shown.
- Android integration in this round. The `.glb` is reusable there later.
- Full VRM/VTuber tooling (face tracking, VSeeFace export).

## Anti-goals

- A generic grey mannequin with a hat: if it doesn't read as "chibi boy in a
  blue hoodie" at dashboard size, it has failed even if it loads.
- An avatar that slows the dashboard down or breaks it when WebGL is
  unavailable or the model fails to load.
- Copying a recognisable existing character.

## Constraints

- No GUI Blender. Editing is done with Blender's Python module (`bpy` 5.3)
  from scripts, rendering previews with CPU Cycles (no EGL in the container).
- Hair is built from a handful of smooth sculpted clumps (deformed and
  subdivided primitives), not strand cards. Expect a stylised "toy" read.
- The dashboard is plain HTML/CSS/JS served by FastAPI at `/static`, no build
  step. Nova supports offline use (Ollama fallback), so three.js is vendored
  into `nova_core/web/vendor/` rather than loaded from a CDN.
- Target asset size: under ~2 MB `.glb`, colors via materials/vertex colors
  (no large texture maps).
- The uploaded zip has no license file. The user is responsible for confirming
  the base mesh's terms; the design does not depend on redistributing it
  publicly.

## Approaches considered

| | Approach | Trade-off |
|---|---|---|
| **A (chosen)** | Edit the user's base mesh in Blender via scripts: proportions, hair clumps, anime eyes, clothes, colors, shape keys; export GLB; three.js panel in dashboard | Fully doable here, lively expressions, light file. Less detailed than the reference. |
| B | Image-to-3D with Meshy from the reference | Closest look, but needs a Meshy account/network access and gives a single mesh with no facial expressions. |
| C | Meshy body + Blender-made eyes/mouth | Best of both, most work, still needs Meshy. |

The user picked **A**. C remains a possible later upgrade.

## Design

### 1. Asset pipeline (`tools/avatar/`)

A reproducible script, `tools/avatar/build_avatar.py`, run with the `bpy`
module, that:

1. Imports `assets/avatar/source/BaseMesh_Chibi.fbx`.
2. Reshapes the body with proportional vertex edits per bone region: slightly
   larger head, rounder cheeks, smaller hands, chunkier feet.
3. Replaces the slit eyes with anime eyes: dark iris disc, blue-grey ring,
   two white highlight dots, a thin eyebrow above each eye. They are parented
   to the Head bone.
4. Adds the face: small nose bump, mouth (a dark inset whose shape is driven by
   shape keys), pink blush patches, ears kept, small silver ring earring on one
   ear.
5. Builds the hair from ~10–14 clumps (scaled, bent, subdivided spheres/cones)
   arranged as messy fringe + side locks + back + one ahoge, joined and
   weighted to the Head bone. Grey-blue (`#9aa4b4`-ish).
6. Builds the clothes as offset shells over the body with weights copied
   from the body: hoodie (light grey torso, blue lower panel and cuffs, hood
   behind neck, two drawstrings, zip line), grey joggers with cargo pockets,
   white-blue sneakers.
7. Adds shape keys on the face mesh: `blink`, `mouth_open`, `smile`,
   `confused` (asymmetric brows + small mouth).
8. Assigns simple PBR materials with a soft vinyl look (high roughness, light
   clearcoat-like sheen where supported by glTF).
9. Poses: rest pose stays the T-pose for the rig. The dashboard applies a
   relaxed arms-down idle pose at runtime.
10. Exports `nova_core/web/avatar/nova_avatar.glb` (skinned, morph targets on)
    and renders front/three-quarter preview PNGs for review.

The source FBX and build script are committed so the avatar can be rebuilt
and tweaked by editing parameters at the top of the script (colors, head
scale, hair clump list).

### 2. Dashboard integration

- `nova_core/web/vendor/three/`: vendored `three.module.js` plus
  `GLTFLoader.js` (and its utils), pinned to one version.
- `nova_core/web/avatar.js`: an ES module exposing a tiny API:
  `NovaAvatar.mount(el)`, `NovaAvatar.setState(state, opts)`.
  - Scene: transparent background, soft key/rim light in the accent blue,
    camera framing head to knees, slight auto-sway.
  - Idle loop: breathing (spine scale/rotation), random blink every 2–6 s,
    occasional head tilt and glance.
  - States: `idle`, `listening` (head tilts toward viewer, eyes wider),
    `thinking` (looks up, brows up, small sway), `talking` (mouth_open
    oscillates for a duration from reply length, smile blended), `error`
    (`confused` for ~2 s then back to idle).
  - Pauses rendering when the tab is hidden; caps the pixel ratio at 2.
  - Falls back to the existing "▲" logo card if WebGL or the GLB fails to load.
- `index.html`: an "Nova" avatar card in the Overview grid next to the
  Interactive Console; `<script type="module" src="/static/avatar.js">`.
- `dashboard.js` hooks into the existing console flow:
  - typing in `#console-input` → `listening`
  - on submit, while `/command` is pending → `thinking`
  - on success → `talking` for the reply, then `idle`
  - on error → `error`
  Communication is via a small `window.dispatchEvent(new CustomEvent('nova:state', …))`
  so `dashboard.js` stays a classic script and the avatar module stays optional.

### 3. Testing and verification

- Python test that the committed `.glb` exists, is a valid glTF binary, has a
  skin, and contains the four morph target names (parse the JSON chunk; no
  `bpy` needed in CI).
- Python test that `/static/avatar/nova_avatar.glb` and `/static/avatar.js`
  are served by the FastAPI app.
- Manual/automated visual check: Playwright screenshots of the dashboard in
  each state, reviewed before handing over.

## Decided by the user

- Style: anime/VTuber-like chibi, following the supplied reference image.
- Base: their uploaded rigged chibi base mesh.
- Approach A (script-edited mesh in Blender, three.js in the dashboard).

## Claude's calls (please check)

- Web dashboard only for now; Android later reuses the `.glb`.
- Four expressions (blink, talk, smile, confused) and five states mapped to
  the console flow, no TTS.
- three.js vendored locally (offline-friendly) instead of a CDN.
- Character details simplified: no sleeve pattern/logo, hair as clumps.
- Character colors taken from the reference, tinted toward the dashboard's
  accent blue.

## Left to the builder

- Exact vertex-edit falloffs and clump placements (iterate on preview renders).
- Light rig and camera framing values.
- Timings and easing of the idle and state animations.
