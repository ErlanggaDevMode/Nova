"""Build the Nova avatar GLB from the chibi base mesh.

Run inside a venv that has the `bpy` module (Blender 5.3):

    python tools/avatar/build_avatar.py

Env vars:
    NOVA_AVATAR_OUT       output .glb (default nova_core/web/avatar/nova_avatar.glb)
    NOVA_AVATAR_PREVIEWS  preview PNG directory (default assets/avatar/previews)
    NOVA_AVATAR_NO_RENDER set to 1 to skip preview renders
"""
import sys

sys.argv = sys.argv[:1]  # bpy parses argv on import

import math  # noqa: E402
import os  # noqa: E402
from pathlib import Path  # noqa: E402

import bpy  # noqa: E402  (must come before bmesh/mathutils)
import bmesh  # noqa: E402
from mathutils import Matrix, Quaternion, Vector  # noqa: E402
from mathutils.bvhtree import BVHTree  # noqa: E402
from mathutils.kdtree import KDTree  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SOURCE_FBX = ROOT / "assets" / "avatar" / "source" / "BaseMesh_Chibi.fbx"
OUT_GLB = Path(os.environ.get("NOVA_AVATAR_OUT", ROOT / "nova_core" / "web" / "avatar" / "nova_avatar.glb"))
PREVIEW_DIR = Path(os.environ.get("NOVA_AVATAR_PREVIEWS", ROOT / "assets" / "avatar" / "previews"))


def hexc(h, a=1.0):
    h = h.lstrip("#")
    srgb = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in srgb]
    return (*lin, a)


# ---------------------------------------------------------------------------
# Tunable parameters
# ---------------------------------------------------------------------------
PARAMS = {
    "colors": {
        "skin": "#f6d9c8",
        "blush": "#f3a9a0",
        "hair": "#8f9bb0",
        "hair_dark": "#7f8a9c",
        "brow": "#5a5f6b",
        "eye_dark": "#151925",
        "eye_ring": "#5f7fae",
        "eye_light": "#ffffff",
        "lash": "#1d2029",
        "mouth": "#8a4a48",
        "hoodie": "#d9dee6",
        "hoodie_blue": "#6f96cf",
        "hem": "#f2f4f7",
        "tee": "#c9d0da",
        "pants": "#7e8898",
        "shoe": "#6f96cf",
        "sole": "#f4f5f7",
        "metal": "#d8dce3",
        "string": "#f4f5f7",
    },
    "head_scale": 1.08,
    "hand_scale": 0.8,
    "foot_scale": 1.12,
    # eye: center x (abs), center z, width, height
    "eye": {"x": 0.052, "z": 0.293, "w": 0.050, "h": 0.058},
    "mouth": {"z": 0.247, "w": 0.020},
}

HEAD_CENTER = Vector((0.0, -0.004, 0.34))
HEAD_R = 0.135


# ---------------------------------------------------------------------------
# Scene helpers
# ---------------------------------------------------------------------------
def material(name, color, rough=0.55, metal=0.0):
    m = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    m.use_nodes = True
    bsdf = m.node_tree.nodes.get("Principled BSDF")
    bsdf.inputs["Base Color"].default_value = hexc(color)
    bsdf.inputs["Roughness"].default_value = rough
    bsdf.inputs["Metallic"].default_value = metal
    m.diffuse_color = hexc(color)
    return m


def mats():
    c = PARAMS["colors"]
    return {
        "skin": material("Nova_Skin", c["skin"], 0.6),
        "blush": material("Nova_Blush", c["blush"], 0.7),
        "hair": material("Nova_Hair", c["hair"], 0.5),
        "brow": material("Nova_Brow", c["brow"], 0.6),
        "eye_dark": material("Nova_EyeDark", c["eye_dark"], 0.15),
        "eye_ring": material("Nova_EyeRing", c["eye_ring"], 0.2),
        "eye_light": material("Nova_EyeLight", c["eye_light"], 0.1),
        "lash": material("Nova_Lash", c["lash"], 0.5),
        "mouth": material("Nova_Mouth", c["mouth"], 0.5),
        "hoodie": material("Nova_Hoodie", c["hoodie"], 0.75),
        "hoodie_blue": material("Nova_HoodieBlue", c["hoodie_blue"], 0.75),
        "hem": material("Nova_Hem", c["hem"], 0.8),
        "tee": material("Nova_Tee", c["tee"], 0.8),
        "pants": material("Nova_Pants", c["pants"], 0.8),
        "shoe": material("Nova_Shoe", c["shoe"], 0.5),
        "sole": material("Nova_Sole", c["sole"], 0.5),
        "metal": material("Nova_Metal", c["metal"], 0.25, 1.0),
        "string": material("Nova_String", c["string"], 0.6),
    }


def import_base(path):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.fbx(filepath=str(path))
    arm = bpy.data.objects["BaseMesh_Chibi"]
    body = bpy.data.objects["SM_Chibi_Body"]
    eye = bpy.data.objects["SM_Chibi_Eye"]
    arm.name = "Nova_Rig"
    body.name = "Nova_Body"
    return arm, body, eye


def bone_world(arm, name):
    b = arm.data.bones[name]
    return arm.matrix_world @ b.head_local


def to_local(obj, p):
    return obj.matrix_world.inverted() @ p


def link_skinned(obj, arm, weights=None):
    """Parent obj to the rig with an Armature modifier. weights: callable(world_co)->{group: w}."""
    obj.parent = arm
    obj.matrix_parent_inverse = arm.matrix_world.inverted()
    mod = obj.modifiers.new("Armature", "ARMATURE")
    mod.object = arm
    if weights is None:
        weights = lambda co: {"Head": 1.0}  # noqa: E731
    groups = {}
    for v in obj.data.vertices:
        for g, w in weights(obj.matrix_world @ v.co).items():
            if g not in groups:
                groups[g] = obj.vertex_groups.new(name=g)
            groups[g].add([v.index], w, "REPLACE")


def new_object(name, bm, mat_list):
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    for m in mat_list:
        me.materials.append(m)
    for p in me.polygons:
        p.use_smooth = True
    ob = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(ob)
    return ob


# ---------------------------------------------------------------------------
# Body
# ---------------------------------------------------------------------------
def subdivide_body(body):
    mod = body.modifiers.new("Subd", "SUBSURF")
    mod.levels = 1
    mod.render_levels = 1
    with bpy.context.temp_override(object=body, active_object=body):
        bpy.ops.object.modifier_move_to_index(modifier="Subd", index=0)
        bpy.ops.object.modifier_apply(modifier="Subd")


def reshape_body(body, arm):
    mw, mwi = body.matrix_world, body.matrix_world.inverted()
    gi = {g.name: g.index for g in body.vertex_groups}
    regions = [
        (["Head"], HEAD_CENTER, PARAMS["head_scale"]),
        (["RightHand", "RightHandIndex1", "RightHandIndex3"], bone_world(arm, "RightHand"), PARAMS["hand_scale"]),
        (["Left_Hand", "Left_HandIndex1", "Left_HandIndex3"], bone_world(arm, "Left_Hand"), PARAMS["hand_scale"]),
        (["RightFoot"], bone_world(arm, "RightFoot") * Vector((1, 1, 0)) + Vector((0, -0.01, 0)), PARAMS["foot_scale"]),
        (["Left_Foot"], bone_world(arm, "Left_Foot") * Vector((1, 1, 0)) + Vector((0, -0.01, 0)), PARAMS["foot_scale"]),
    ]
    for v in body.data.vertices:
        co = mw @ v.co
        for groups, center, s in regions:
            w = sum(g.weight for g in v.groups if g.group in [gi[n] for n in groups if n in gi])
            if w > 0:
                f = 1 + (s - 1) * min(w, 1.0)
                co = center + (co - center) * f
        v.co = mwi @ co


def dominant_group(body, v):
    names = [g.name for g in body.vertex_groups]
    if not v.groups:
        return None
    g = max(v.groups, key=lambda g: g.weight)
    return names[g.group]


def paint_body(body, M):
    """Assign clothing materials per face and push clothed areas outward."""
    me = body.data
    me.materials.clear()
    order = ["skin", "hoodie", "hoodie_blue", "hem", "tee", "pants", "shoe", "sole"]
    for k in order:
        me.materials.append(M[k])
    idx = {k: i for i, k in enumerate(order)}
    mw = body.matrix_world
    vdom = [dominant_group(body, v) for v in me.vertices]
    vworld = [mw @ v.co for v in me.vertices]

    def region(poly):
        c = sum((vworld[i] for i in poly.vertices), Vector()) / len(poly.vertices)
        doms = [vdom[i] for i in poly.vertices]
        dom = max(set(doms), key=doms.count)
        ax = abs(c.x)
        # Regions are cut by rest-pose (T-pose) geometry so the colour seams are clean rings.
        if ax > 0.074 and 0.14 < c.z < 0.215 and dom != "Head":  # arms stick out sideways in the T-pose
            return "skin" if ax > 0.146 else "hoodie_blue"
        if dom == "Head" or c.z > 0.2:
            return "skin"
        if c.z < 0.011:
            return "sole"
        if c.z < 0.03:
            return "shoe"
        # torso / legs
        if c.z < 0.072:
            return "pants"
        if c.z < 0.084:
            return "hem"
        if ax < 0.016 and c.y < -0.02 and c.z < 0.19:
            return "tee"
        if c.z < 0.128:
            return "hoodie_blue"
        return "hoodie"

    vert_cloth = [0.0] * len(me.vertices)
    offs = {"skin": 0.0, "hoodie": 0.0045, "hoodie_blue": 0.0045, "hem": 0.0055, "tee": 0.003,
            "pants": 0.003, "shoe": 0.003, "sole": 0.004}
    counts = [0] * len(me.vertices)
    for p in me.polygons:
        r = region(p)
        p.material_index = idx[r]
        for i in p.vertices:
            vert_cloth[i] += offs[r]
            counts[i] += 1
    me.update()
    normals = [v.normal.copy() for v in me.vertices]
    for v in me.vertices:
        if counts[v.index]:
            v.co += normals[v.index] * (vert_cloth[v.index] / counts[v.index]) / body.matrix_world.to_scale().x


def body_bvh(body):
    dg = bpy.context.evaluated_depsgraph_get()
    ev = body.evaluated_get(dg)
    me = ev.to_mesh()
    verts = [ev.matrix_world @ v.co for v in me.vertices]
    polys = [tuple(p.vertices) for p in me.polygons]
    ev.to_mesh_clear()
    return BVHTree.FromPolygons(verts, polys)


# ---------------------------------------------------------------------------
# Face decals (projected onto the head from the front) with shape keys
# ---------------------------------------------------------------------------
class Decal:
    """Collects flat 2D (x, z) shapes, then projects them onto the face."""

    def __init__(self):
        self.verts = []  # list of dict state->(x,z), offset
        self.faces = []  # (indices, material key)

    def add_vert(self, states, offset):
        self.verts.append((states, offset))
        return len(self.verts) - 1

    def add_grid(self, fn, nu, nv, offset, mat, keys=("basis",)):
        """fn(key, u, v) -> (x, z) for u,v in [0,1]."""
        ids = [[self.add_vert({k: fn(k, i / nu, j / nv) for k in keys}, offset) for j in range(nv + 1)]
               for i in range(nu + 1)]
        for i in range(nu):
            for j in range(nv):
                self.faces.append(((ids[i][j], ids[i + 1][j], ids[i + 1][j + 1], ids[i][j + 1]), mat))

    def add_ellipse(self, fn, offset, mat, keys=("basis",), rings=4, segs=24):
        """fn(key, r, t) -> (x, z) with r in [0,1] radius fraction, t angle."""
        center = self.add_vert({k: fn(k, 0.0, 0.0) for k in keys}, offset)
        prev = None
        rows = []
        for ri in range(1, rings + 1):
            r = ri / rings
            rows.append([self.add_vert({k: fn(k, r, 2 * math.pi * s / segs) for k in keys}, offset)
                         for s in range(segs)])
        for s in range(segs):
            self.faces.append(((center, rows[0][s], rows[0][(s + 1) % segs]), mat))
        for a, b in zip(rows, rows[1:]):
            for s in range(segs):
                self.faces.append(((a[s], b[s], b[(s + 1) % segs], a[(s + 1) % segs]), mat))
        del prev

    def build(self, name, bvh, M, mat_keys, keys):
        bm = bmesh.new()
        bverts = []
        for states, off in self.verts:
            bverts.append(bm.verts.new(project(bvh, states["basis"], off)))
        mat_index = {k: i for i, k in enumerate(mat_keys)}
        for ids, mk in self.faces:
            f = bm.faces.new([bverts[i] for i in ids])
            f.material_index = mat_index[mk]
        # All shapes are wound counter-clockwise in (x, z), so faces point to -y (out of the face)
        ob = new_object(name, bm, [M[k] for k in mat_keys])
        if keys:
            ob.shape_key_add(name="Basis")
            for key in keys:
                sk = ob.shape_key_add(name=key, from_mix=False)
                sk.value = 0.0  # new keys default to 1.0 in Blender 5.x
                for i, (states, off) in enumerate(self.verts):
                    sk.data[i].co = project(bvh, states.get(key, states["basis"]), off)
        return ob


def project(bvh, xz, offset):
    x, z = xz
    origin = Vector((x, -1.0, z))
    hit, normal, _, _ = bvh.ray_cast(origin, Vector((0, 1, 0)))
    if hit is None:
        return Vector((x, -0.13, z))
    return hit + normal * offset


def build_face(bvh, M):
    E = PARAMS["eye"]
    hw, hh = E["w"] / 2, E["h"] / 2
    d = Decal()
    keys = ("basis", "blink", "mouth_open", "smile", "confused")

    for side in (-1, 1):
        cx, cz = side * E["x"], E["z"]

        def closed_z(x, cx=cx, cz=cz):
            """Height of the closed-eye curve (a soft 'u') at x."""
            dx = max(-1.0, min(1.0, (x - cx) / (hw * 1.08)))
            return cz - hh * 0.05 - hh * 0.22 * (1 - dx * dx)

        def ell(cxx, czz, rx, rz):
            """Eye layer: an ellipse that collapses onto the closed-eye curve on blink."""
            def fn(k, r, t):
                x, z = cxx + rx * r * math.cos(t), czz + rz * r * math.sin(t)
                if k == "blink":
                    return (x, closed_z(x) + 0.0021 + (z - czz) * 0.02)
                return (x, z)
            return fn

        # Eye layers; on blink they flatten behind the lash line, so no eyelid mesh is needed
        d.add_ellipse(ell(cx, cz, hw, hh), 0.0012, "eye_dark", keys=keys)
        d.add_ellipse(ell(cx, cz - hh * 0.28, hw * 0.78, hh * 0.58), 0.0018, "eye_ring", keys=keys)
        d.add_ellipse(ell(cx, cz + hh * 0.05, hw * 0.5, hh * 0.5), 0.0024, "eye_dark", keys=keys)
        hx = cx - side * hw * 0.32
        d.add_ellipse(ell(hx, cz + hh * 0.38, hw * 0.3, hw * 0.3), 0.0030, "eye_light", keys=keys,
                      segs=16, rings=2)
        d.add_ellipse(ell(cx + side * hw * 0.35, cz - hh * 0.38, hw * 0.13, hw * 0.13), 0.0030, "eye_light",
                      keys=keys, segs=12, rings=1)

        # Lash line along the upper arc; on blink it becomes a soft "u" closed-eye line
        def lash(k, u, v, cx=cx, cz=cz):
            ang = math.pi * (0.92 - 0.84 * u)
            dx = math.cos(ang)
            thick = 0.0042 * (0.45 + 0.55 * math.sin(ang))
            arc_z = cz + hh * 1.02 * math.sin(ang)
            x = cx + hw * 1.08 * dx
            if k == "blink":
                return (x, closed_z(x) + thick * v)
            return (x, arc_z - thick * 0.25 + thick * v)

        d.add_grid(lash, 16, 1, 0.0042, "lash", keys=keys)

        # Brow: rounded thick dash
        bz = cz + hh * 1.55

        def brow(k, u, v, side=side, cx=cx, bz=bz):
            x = cx + (u - 0.5) * hw * 1.3
            arch = 0.004 * math.sin(math.pi * u)
            th = 0.0055 * (0.6 + 0.4 * math.sin(math.pi * u))
            z = bz + arch + (v - 0.5) * th
            inner = (1 - u) if side > 0 else u  # 1 at the end nearest the nose
            if k == "confused":
                tilt = 0.012 if side > 0 else -0.006
                z += tilt * inner - 0.002
            elif k == "smile":
                z += 0.004
            return (x, z)

        d.add_grid(brow, 10, 2, 0.0015, "brow", keys=keys)

        # Blush
        d.add_ellipse(ell(side * (E["x"] + 0.018), cz - hh * 1.0, 0.016, 0.0085), 0.0008, "blush",
                      segs=16, rings=2)

    # Mouth
    mz, mw = PARAMS["mouth"]["z"], PARAMS["mouth"]["w"]

    def mouth(k, u, v):
        s = u * 2 - 1
        if k == "mouth_open":
            w = mw * 0.75
            top = mz + 0.0025 + 0.0015 * s * s
            bot = mz - 0.011 * (1 - s * s) ** 0.7 + 0.0015 * s * s
        elif k == "smile":
            w = mw * 1.25
            top = mz + 0.0055 * s * s
            bot = top - 0.0028 - 0.004 * (1 - s * s)
        elif k == "confused":
            w = mw * 0.45
            c = math.sqrt(max(0.0, 1 - s * s))
            top = mz + 0.0035 * c
            bot = mz - 0.0035 * c
        else:
            w = mw
            top = mz + 0.003 * s * s
            bot = top - 0.0024 * (0.35 + 0.65 * (1 - s * s))
        return (s * w / 2, bot + (top - bot) * v)

    d.add_grid(mouth, 16, 3, 0.0012, "mouth", keys=keys)

    mat_keys = ["skin", "eye_dark", "eye_ring", "eye_light", "lash", "brow", "blush", "mouth"]
    return d.build("Nova_Face", bvh, M, mat_keys, keys[1:])


def build_nose(bvh, M):
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=12, v_segments=8, radius=1.0)
    p = project(bvh, (0.0, 0.262), 0.0)
    for v in bm.verts:
        v.co = p + Vector((v.co.x * 0.006, v.co.y * 0.004, v.co.z * 0.0045))
    return new_object("Nova_Nose", bm, [M["skin"]])


# ---------------------------------------------------------------------------
# Hair
# ---------------------------------------------------------------------------
def sph(theta_deg, phi_deg, r=HEAD_R):
    """Point on head sphere. theta: azimuth from front (-y) towards +x; phi: elevation."""
    t, p = math.radians(theta_deg), math.radians(phi_deg)
    return HEAD_CENTER + Vector((math.sin(t) * math.cos(p), -math.cos(t) * math.cos(p), math.sin(p))) * r


def clump(bm, root, tip, radius, bend_dir, bend=0.3, flat=0.6, segs=12, rings=14):
    axis = tip - root
    L = axis.length
    a = axis.normalized()
    bend_dir = (bend_dir - a * bend_dir.dot(a)).normalized() if bend_dir.length > 1e-6 else Vector((0, 0, 1))
    side = a.cross(bend_dir).normalized()
    ret = bmesh.ops.create_uvsphere(bm, u_segments=segs, v_segments=rings, radius=1.0)
    for v in ret["verts"]:
        x, y, z = v.co
        t = (z + 1) / 2  # 0 root .. 1 tip
        prof = radius * (math.sin(math.pi * min(1.0, 0.15 + t * 0.85)) ** 0.75) * (1.15 - 0.75 * t)
        rr = math.sqrt(max(0.0, 1 - z * z))
        k = prof / max(rr, 1e-6) if rr > 1e-6 else 0
        pos = root + a * (t * L) + bend_dir * (bend * L * t * t)
        v.co = pos + side * (x * k) + bend_dir * (y * k * flat)


def build_hair(body, M):
    bm = bmesh.new()

    # Scalp cap: head sphere shell above the hairline
    cap = bmesh.ops.create_uvsphere(bm, u_segments=40, v_segments=24, radius=1.0)
    for v in cap["verts"]:
        v.co = HEAD_CENTER + Vector((v.co.x * 1.07, v.co.y * 1.05, v.co.z * 1.03)) * (HEAD_R + 0.012)
    kill = []
    for v in cap["verts"]:
        rel = v.co - HEAD_CENTER
        front = -rel.y / (HEAD_R + 0.012)
        h = rel.z / (HEAD_R + 0.012)
        side = abs(rel.x) / (HEAD_R + 0.012)
        hairline = 0.42 - 1.0 * max(0.0, -front) - 0.5 * side
        if h < hairline - 0.05 * max(0, front):
            kill.append(v)
    bmesh.ops.delete(bm, geom=kill, context="VERTS")

    clumps = [
        # fringe (sweeping to the character's right / viewer's left)
        (sph(-35, 48), sph(-48, 4, HEAD_R + 0.02), 0.032, Vector((0, -1, 0)), 0.25),
        (sph(-12, 52), sph(-30, 6, HEAD_R + 0.024), 0.034, Vector((0, -1, 0)), 0.25),
        (sph(8, 55), sph(-8, 10, HEAD_R + 0.026), 0.034, Vector((0, -1, 0)), 0.25),
        (sph(28, 52), sph(14, 9, HEAD_R + 0.024), 0.032, Vector((0, -1, 0)), 0.22),
        (sph(46, 46), sph(38, 7, HEAD_R + 0.022), 0.030, Vector((0, -1, 0)), 0.22),
        # side locks in front of the ears
        (sph(-74, 42), sph(-80, -24, HEAD_R + 0.014), 0.036, Vector((-1, -0.2, 0)), 0.1),
        (sph(74, 42), sph(80, -24, HEAD_R + 0.014), 0.036, Vector((1, -0.2, 0)), 0.1),
        # top / crown, flicking outward
        (sph(-60, 70), sph(-96, 26, HEAD_R + 0.024), 0.036, Vector((-1, 0, 0.3)), 0.15),
        (sph(60, 70), sph(100, 26, HEAD_R + 0.024), 0.036, Vector((1, 0, 0.3)), 0.15),
        # nape
        (sph(-130, 20), sph(-138, -38, HEAD_R + 0.012), 0.04, Vector((0, 1, 0)), 0.1),
        (sph(130, 20), sph(138, -38, HEAD_R + 0.012), 0.04, Vector((0, 1, 0)), 0.1),
        (sph(180, 20), sph(180, -42, HEAD_R + 0.012), 0.045, Vector((0, 1, 0)), 0.1),
        (sph(-140, 60), sph(-130, 10, HEAD_R + 0.03), 0.04, Vector((-0.5, 1, 0)), 0.2),
        (sph(140, 60), sph(130, 10, HEAD_R + 0.03), 0.04, Vector((0.5, 1, 0)), 0.2),
        (sph(180, 55), sph(180, -5, HEAD_R + 0.025), 0.045, Vector((0, 1, 0)), 0.2),
        (sph(-160, 30), sph(-155, -25, HEAD_R + 0.02), 0.035, Vector((0, 1, 0)), 0.15),
        (sph(160, 30), sph(155, -25, HEAD_R + 0.02), 0.035, Vector((0, 1, 0)), 0.15),
        (sph(-105, 40), sph(-112, -12, HEAD_R + 0.03), 0.032, Vector((-1, 0.4, 0)), 0.18),
        (sph(105, 40), sph(112, -12, HEAD_R + 0.03), 0.032, Vector((1, 0.4, 0)), 0.18),
    ]
    for root, tip, r, bd, bend in clumps:
        clump(bm, root, tip, r, bd, bend)

    # Ahoge
    top = sph(-5, 85, HEAD_R + 0.01)
    clump(bm, top, top + Vector((0.012, 0.03, 0.05)), 0.008, Vector((0, 1, 0.2)), 0.6, flat=0.5)

    ob = new_object("Nova_Hair", bm, [M["hair"]])
    sub = ob.modifiers.new("Subd", "SUBSURF")
    sub.levels = 1
    with bpy.context.temp_override(object=ob, active_object=ob):
        bpy.ops.object.modifier_apply(modifier="Subd")
    return ob


# ---------------------------------------------------------------------------
# Outfit details
# ---------------------------------------------------------------------------
def tube(bm, pts, radius, segs=8):
    """Sweep a circle along a polyline."""
    rings = []
    for i, p in enumerate(pts):
        nxt = pts[min(i + 1, len(pts) - 1)]
        prv = pts[max(i - 1, 0)]
        t = (nxt - prv).normalized()
        up = Vector((0, 0, 1)) if abs(t.z) < 0.9 else Vector((1, 0, 0))
        n1 = t.cross(up).normalized()
        n2 = t.cross(n1).normalized()
        rings.append([bm.verts.new(p + (n1 * math.cos(a) + n2 * math.sin(a)) * radius)
                      for a in [2 * math.pi * s / segs for s in range(segs)]])
    for a, b in zip(rings, rings[1:]):
        for s in range(segs):
            bm.faces.new((a[s], a[(s + 1) % segs], b[(s + 1) % segs], b[s]))
    bm.faces.new(list(reversed(rings[0])))
    bm.faces.new(rings[-1])


def surface_front(bvh, x, z, off):
    return project(bvh, (x, z), off)


def build_outfit(body, bvh, M):
    bm_cloth = bmesh.new()
    bm_metal = bmesh.new()
    bm_string = bmesh.new()

    # Hood: a fat torus lying behind/around the neck
    hood = bmesh.ops.create_uvsphere(bm_cloth, u_segments=24, v_segments=12, radius=1.0)
    for v in hood["verts"]:
        x, y, z = v.co
        v.co = Vector((x * 0.075, 0.035 + y * 0.05, 0.197 + z * 0.022))
    # Collar ring around neck front
    collar = bmesh.ops.create_circle(bm_cloth, cap_ends=False, segments=24, radius=0.045)
    ring2 = []
    for v in collar["verts"]:
        v.co = Vector((v.co.x * 1.0, v.co.y * 0.9 - 0.004, 0.196))
    bmesh.ops.extrude_edge_only(bm_cloth, edges=[e for e in bm_cloth.edges if all(v in collar["verts"] for v in e.verts)])
    for v in bm_cloth.verts:
        if v not in collar["verts"] and v not in hood["verts"]:
            v.co = Vector((v.co.x * 1.15, v.co.y * 1.15 - 0.002, 0.188))
            ring2.append(v)

    # Zipper lines on both edges of the open jacket
    for side in (-1, 1):
        pts = [surface_front(bvh, side * 0.018, z, 0.0055) for z in [0.088 + i * 0.0085 for i in range(13)]]
        tube(bm_metal, pts, 0.0016, segs=6)
        # zipper pull
    pull = bmesh.ops.create_cube(bm_metal, size=1.0)
    p = surface_front(bvh, 0.018, 0.092, 0.008)
    for v in pull["verts"]:
        v.co = p + Vector((v.co.x * 0.004, v.co.y * 0.002, v.co.z * 0.009 - 0.004))

    # Drawstrings
    for side in (-1, 1):
        base = surface_front(bvh, side * 0.03, 0.19, 0.008)
        pts = [base + Vector((side * 0.002 * i, -0.002 * min(i, 3), -0.008 * i)) for i in range(6)]
        tube(bm_string, pts, 0.0024, segs=8)

    outfit = new_object("Nova_Outfit", bm_cloth, [M["hoodie"]])
    metal = new_object("Nova_Zip", bm_metal, [M["metal"]])
    strings = new_object("Nova_Strings", bm_string, [M["string"]])
    # Cuffs/hood colors: give cuff and pocket faces their own materials
    outfit.data.materials.append(M["hoodie_blue"])
    outfit.data.materials.append(M["pants"])
    for poly in outfit.data.polygons:
        c = poly.center
        if c.z < 0.08:
            poly.material_index = 2
        elif abs(c.x) > 0.1:
            poly.material_index = 1
    return [outfit, metal, strings]


def build_earring(body, M):
    head_vs = [body.matrix_world @ v.co for v in body.data.vertices]
    ear = max((p for p in head_vs if 0.24 < p.z < 0.34), key=lambda p: p.x)
    bm = bmesh.new()
    bmesh.ops.create_circle(bm, segments=12, radius=0.006)
    tor = bmesh.new()
    # simple torus via tube along a circle
    pts = [ear + Vector((0.002, -0.004, -0.014)) + Vector((0, math.cos(a), math.sin(a))) * 0.0065
           for a in [2 * math.pi * i / 16 for i in range(17)]]
    tube(tor, pts, 0.0012, segs=6)
    bm.free()
    return new_object("Nova_Earring", tor, [M["metal"]])


def weights_from_body(body):
    kd = KDTree(len(body.data.vertices))
    mw = body.matrix_world
    for v in body.data.vertices:
        kd.insert(mw @ v.co, v.index)
    kd.balance()
    names = [g.name for g in body.vertex_groups]

    def fn(co):
        _, idx, _ = kd.find(co)
        return {names[g.group]: g.weight for g in body.data.vertices[idx].groups if g.weight > 0.001}

    return fn


# ---------------------------------------------------------------------------
# Export & previews
# ---------------------------------------------------------------------------
def export_glb(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.export_scene.gltf(
        filepath=str(path),
        export_format="GLB",
        export_skins=True,
        export_morph=True,
        export_morph_normal=False,
        export_animations=False,
        export_apply=False,
        export_yup=True,
        export_extras=False,
        export_cameras=False,
        export_lights=False,
    )


def pose_relaxed(arm, amount=1.0):
    """Arms down ~65 degrees from the T-pose (used for previews; the web app does the same)."""
    for name, sign in (("RightArm", 1), ("Left_Arm", -1)):
        pb = arm.pose.bones[name]
        pb.rotation_mode = "QUATERNION"
        rest = (arm.matrix_world @ pb.bone.matrix_local).to_quaternion()
        world_rot = Quaternion(Vector((0, 1, 0)), math.radians(-65 * sign * amount))
        pb.rotation_quaternion = rest.inverted() @ world_rot @ rest


def render_previews(out_dir, arm):
    out_dir.mkdir(parents=True, exist_ok=True)
    sc = bpy.context.scene
    pose_relaxed(arm)
    bpy.context.view_layer.update()
    sc.render.engine = "CYCLES"
    sc.cycles.device = "CPU"
    sc.cycles.samples = 48
    sc.cycles.use_denoising = True
    sc.render.resolution_x = 720
    sc.render.resolution_y = 900
    sc.render.film_transparent = False
    sc.view_settings.view_transform = "Standard"
    w = bpy.data.worlds.new("w")
    sc.world = w
    w.use_nodes = True
    w.node_tree.nodes["Background"].inputs[0].default_value = hexc("#3a4656")
    w.node_tree.nodes["Background"].inputs[1].default_value = 0.6

    def light(name, loc, energy, size, color="#ffffff"):
        ld = bpy.data.lights.new(name, "AREA")
        ld.energy = energy
        ld.size = size
        ld.color = hexc(color)[:3]
        lo = bpy.data.objects.new(name, ld)
        sc.collection.objects.link(lo)
        lo.location = loc
        lo.rotation_euler = (Vector((0, 0, 0.25)) - Vector(loc)).to_track_quat("-Z", "Y").to_euler()

    light("key", (-0.6, -0.9, 0.9), 22, 0.8)
    light("fill", (0.8, -0.6, 0.4), 8, 1.0, "#cfe0ff")
    light("rim", (0.3, 0.9, 0.8), 18, 0.6, "#8fb8ff")

    cam = bpy.data.objects.new("cam", bpy.data.cameras.new("cam"))
    sc.collection.objects.link(cam)
    sc.camera = cam
    cam.data.lens = 85
    target = Vector((0, 0, 0.245))
    for name, d in (("front", Vector((0, -1, 0.12))), ("three_quarter", Vector((0.65, -0.8, 0.18))),
                    ("side", Vector((1, 0, 0.08))), ("face", Vector((0.15, -1, 0.08)))):
        dist = 1.35 if name != "face" else 0.6
        tgt = target if name != "face" else Vector((0, 0, 0.32))
        cam.location = tgt + d.normalized() * dist
        cam.rotation_euler = (tgt - cam.location).to_track_quat("-Z", "Y").to_euler()
        sc.render.filepath = str(out_dir / f"{name}.png")
        bpy.ops.render.render(write_still=True)
    # Expression close-ups
    face = bpy.data.objects["Nova_Face"]
    cam.location = Vector((0, 0, 0.32)) + Vector((0.15, -1, 0.08)).normalized() * 0.6
    cam.rotation_euler = (Vector((0, 0, 0.32)) - cam.location).to_track_quat("-Z", "Y").to_euler()
    for kb in face.data.shape_keys.key_blocks[1:]:
        kb.value = 1.0
        sc.render.filepath = str(out_dir / f"expr_{kb.name}.png")
        bpy.ops.render.render(write_still=True)
        kb.value = 0.0
    pose_relaxed(arm, 0.0)


def main():
    arm, body, eye = import_base(SOURCE_FBX)
    M = mats()
    bpy.data.objects.remove(eye, do_unlink=True)
    subdivide_body(body)
    reshape_body(body, arm)
    paint_body(body, M)
    bpy.context.view_layer.update()
    bvh = body_bvh(body)

    face = build_face(bvh, M)
    link_skinned(face, arm)
    nose = build_nose(bvh, M)
    link_skinned(nose, arm)
    hair = build_hair(body, M)
    link_skinned(hair, arm)
    ear = build_earring(body, M)
    link_skinned(ear, arm)
    wfn = weights_from_body(body)
    for ob in build_outfit(body, bvh, M):
        link_skinned(ob, arm, wfn)

    export_glb(OUT_GLB)
    print("exported", OUT_GLB, OUT_GLB.stat().st_size)
    if os.environ.get("NOVA_AVATAR_NO_RENDER") != "1":
        render_previews(PREVIEW_DIR, arm)


if __name__ == "__main__":
    main()
