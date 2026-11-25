# Nova 3D Avatar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the user's rigged chibi base mesh into an expressive Nova character (`nova_avatar.glb`) and show it in the web dashboard, reacting to the console command flow.

**Architecture:** A reproducible `bpy` script edits the FBX base mesh (reshape, eyes, face, hair, clothes, shape keys) and exports a skinned GLB with morph targets. A standalone ES module (`avatar.js`) renders it with vendored three.js and listens for `nova:state` events that `dashboard.js` dispatches around `/command` calls.

**Tech Stack:** Blender `bpy` 5.3 (Python 3.13 venv, CPU Cycles previews), three.js r170 (vendored), FastAPI static files, pytest, Playwright (verification only).

**Spec:** `docs/superpowers/specs/2026-11-25-nova-avatar-design.md`

## Global Constraints

- No GUI Blender; build runs as `python tools/avatar/build_avatar.py` inside a venv with `bpy==5.3.*`. Never name a script after a stdlib module (`inspect.py` crashed bpy).
- Previews render with `CYCLES` on CPU (no EGL in container).
- GLB under ~2 MB, colors via materials (no large textures).
- Morph target names exactly: `blink`, `mouth_open`, `smile`, `confused`.
- three.js vendored under `nova_core/web/vendor/three/`, one pinned version (0.170.0); no CDN.
- Dashboard must keep working if WebGL or the GLB fails: fall back to the "▲" card.
- `dashboard.js` stays a classic script; it talks to the avatar only via `window.dispatchEvent(new CustomEvent('nova:state', {detail: {state, text}}))`.
- States exactly: `idle`, `listening`, `thinking`, `talking`, `error`.
- Palette: hair `#9aa4b4`-ish grey-blue, hoodie light grey + blue, joggers grey, sneakers white/blue; dashboard accent `#2e8fff` for lighting.

## Review Focus

1. GLB missing / 404 or WebGL unavailable → dashboard still usable, fallback card shown, no uncaught errors (Task 4 test).
2. Rapid repeated submits while a `/command` is pending → state ends in the latest result, no stuck `thinking` (Task 5: state is overwritten by each event; talking timer cleared on new state).
3. Very long reply text → `talking` duration capped (max 6 s) so the mouth doesn't flap for a minute (Task 4 unit in `avatarTiming`).
4. Hidden tab → render loop pauses (Task 4: `document.hidden` check).
5. Morph targets lost on export (common with modifiers applied after shape keys) → asset test asserts all four names present in the GLB (Task 1).

---

### Task 1: Asset tests (red)

**Files:**
- Test: `tests/nova_core/test_avatar_asset.py`

**Interfaces:**
- Produces: expectation that `nova_core/web/avatar/nova_avatar.glb` exists and is served at `/static/avatar/nova_avatar.glb`.

- [ ] **Step 1: Write tests**
  - `test_glb_header_and_size`: file starts with `b"glTF"`, version 2, size < 2_500_000.
  - `test_glb_has_skin_and_morphs`: parse JSON chunk (bytes 20..20+chunkLen); `len(gltf["skins"]) >= 1`; collect `mesh.extras.targetNames` across meshes; assert `{"blink","mouth_open","smile","confused"} <= names`.
  - `test_glb_served_statically`: `TestClient(app).get("/static/avatar/nova_avatar.glb")` → 200, `content-type` contains `gltf` or `octet-stream`.
- [ ] **Step 2:** `pytest tests/nova_core/test_avatar_asset.py -v` → FAIL (file missing).
- [ ] **Step 3: Commit** `test: add avatar asset checks`.

### Task 2: Build script — body, face, expressions, export (green for Task 1)

**Files:**
- Create: `assets/avatar/source/BaseMesh_Chibi.fbx` (copied from the upload)
- Create: `tools/avatar/build_avatar.py`, `tools/avatar/README.md` (how to set up the venv and run)
- Create (output): `nova_core/web/avatar/nova_avatar.glb`, `assets/avatar/previews/*.png`

**Interfaces:**
- `build_avatar.py` reads env `NOVA_AVATAR_OUT` (default `nova_core/web/avatar/nova_avatar.glb`) and `NOVA_AVATAR_PREVIEWS` (default `assets/avatar/previews`); a `PARAMS` dict at the top holds colors, head scale, clump list.
- Functions (each operates on the current scene): `import_base(path) -> (armature, body, eye)`, `reshape_body(body, armature)`, `build_eyes(armature) -> obj`, `build_face(armature) -> face_obj`, `add_shape_keys(face_obj, eyes_obj)`, `assign_materials(objs)`, `export_glb(path)`, `render_previews(dir)`.

- [ ] **Step 1:** Reshape: scale head-weighted verts ×1.08 around Head bone, hands ×0.85, feet ×1.15 using vertex-group weights as falloff.
- [ ] **Step 2:** Eyes: delete slit eye mesh; per side add dark iris disc + blue-grey ring + 2 white highlight discs + thin brow; parent with Head weight 1.0.
- [ ] **Step 3:** Face: nose bump, inset mouth, blush discs, silver ring earring on the right ear.
- [ ] **Step 4:** Shape keys: `blink` (eyelid skin-color caps scale down over iris / iris squash to line), `mouth_open`, `smile` (mouth corners up + eye bottom lift), `confused` (asymmetric brows + small 'o' mouth). Faces needing morphs are joined into one `Nova_Face` mesh before keys are added; no modifiers applied after keys.
- [ ] **Step 5:** Export GLB with `export_morph=True`, `export_skins=True`, `export_apply=False` for skinned meshes; render front + three-quarter previews.
- [ ] **Step 6:** `pytest tests/nova_core/test_avatar_asset.py -v` → PASS.
- [ ] **Step 7:** Look at previews; iterate until face reads as the reference (big glossy eyes, blush, small smile).
- [ ] **Step 8: Commit** `feat(avatar): base face, eyes and expressions`.

### Task 3: Build script — hair and clothes

**Files:**
- Modify: `tools/avatar/build_avatar.py` (add `build_hair(armature) -> obj`, `build_clothes(body, armature) -> list[obj]`)
- Regenerate: GLB + previews

- [ ] **Step 1:** Hair: 10–14 clumps from `PARAMS["hair_clumps"]` (each: position, scale, rotation, bend), smooth-subdivided, joined, weighted 100% Head; one ahoge on top; fringe covering upper forehead, side locks, back volume.
- [ ] **Step 2:** Clothes as offset shells (Solidify-free: duplicate body region by vertex-group, push along normals 4–8 mm), weights via Data Transfer from body then applied: hoodie (grey torso, blue lower panel + cuffs, hood behind neck, two drawstrings, zip line), joggers with cargo pockets, sneakers (white sole, blue upper). Hide body skin under clothes (delete covered faces) to avoid clipping.
- [ ] **Step 3:** Verify a test pose (arms down 60°, slight head turn) in a preview render: no major clipping.
- [ ] **Step 4:** `pytest tests/nova_core/test_avatar_asset.py -v` → PASS (size still < 2.5 MB).
- [ ] **Step 5:** Show previews to the user for a look check before wiring the dashboard.
- [ ] **Step 6: Commit** `feat(avatar): hair and outfit`.

### Task 4: Avatar module in the dashboard

**Files:**
- Create: `nova_core/web/vendor/three/three.module.js`, `nova_core/web/vendor/three/addons/loaders/GLTFLoader.js`, `nova_core/web/vendor/three/addons/utils/BufferGeometryUtils.js` (from three@0.170.0), `nova_core/web/vendor/three/LICENSE`
- Create: `nova_core/web/avatar.js`
- Modify: `nova_core/web/index.html` (importmap `{"three": "/static/vendor/three/three.module.js", "three/addons/": "/static/vendor/three/addons/"}`, avatar card `#nova-avatar` as first card in `.dashboard-grid`, module script tag), `nova_core/web/dashboard.css` (`.avatar-card` styling, fallback `.avatar-fallback`)
- Test: `tests/nova_core/test_avatar_asset.py` (add static tests), `tests/web/avatar.spec.mjs` (Playwright, run manually)

**Interfaces:**
- Produces: `avatar.js` exports `mount(el: HTMLElement): Promise<void>`, `setState(state: string, opts?: {text?: string}): void`, `talkDuration(text: string): number` (ms = clamp(400 + 45*len, 1200, 6000)). On load it auto-mounts on `#nova-avatar` and listens to `window` `nova:state`.

- [ ] **Step 1:** Add pytest: `/static/avatar.js` 200 and contains `nova:state`; `/static/vendor/three/three.module.js` 200. Run → FAIL.
- [ ] **Step 2:** Implement `avatar.js`: transparent renderer (pixelRatio ≤ 2), camera framing head-to-knees, key light + accent-blue rim light, relaxed arms-down pose applied to bones at load, idle loop (breath, blink every 2–6 s, occasional tilt), state blends per spec, rAF skipped when `document.hidden`. Any error in WebGL creation or GLB load → replace contents with `.avatar-fallback` "▲ Nova" and `console.warn` once.
- [ ] **Step 3:** pytest → PASS.
- [ ] **Step 4:** Playwright script: serve app, log in (set token in localStorage as dashboard does), screenshot avatar card; then route `**/nova_avatar.glb` to 404 and assert `.avatar-fallback` visible and no page errors.
- [ ] **Step 5: Commit** `feat(dashboard): 3D Nova avatar card`.

### Task 5: Wire console flow to avatar states

**Files:**
- Modify: `nova_core/web/dashboard.js:242-271` (console submit) and console input listener

**Interfaces:**
- Consumes: `nova:state` contract from Task 4.

- [ ] **Step 1:** Add `function setAvatarState(state, text)` dispatching the event. `input` on `#console-input` → `listening` (debounced back to `idle` after 4 s of no typing); submit → `thinking`; ok → `talking` with `data.response_text`; non-ok / catch → `error`.
- [ ] **Step 2:** Playwright: mock `/command` with a 1 s delay then JSON; assert avatar state attribute `data-state` on `#nova-avatar` goes `thinking` → `talking` → `idle`; mock 500 → `error`. (`avatar.js` mirrors its state to `el.dataset.state` for this.)
- [ ] **Step 3:** Full `pytest` → all existing tests still pass.
- [ ] **Step 4: Commit** `feat(dashboard): drive avatar from console commands`, push.

## Self-review notes

- Spec coverage: pipeline §1 → Tasks 2–3; dashboard §2 → Tasks 4–5; testing §3 → Tasks 1, 4, 5. Android/TTS are non-goals.
- Names consistent: `nova:state`, `data-state`, morph names, state names match Global Constraints.
