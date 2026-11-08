# Studio-model skeletons for the GSM1 pack (GSX1 chunk "SKL1", tools/modelpack.py) — bone-texture skinning of the monster models.
# Clean-room from the public MDL v10 layout (tools/mdl10.py) and the observed renderer behaviour (docs/decomp/hl_client/CStudioModelRenderer_p*.md);
# no SDK / decompiler code is copied.
#
# Why: the MON1 monsters used to be baked pose meshes (every key = every vertex again, 24 bytes per vertex per key: 42 MB of the
# valve pack for 2076 keys). GoldSrc binds every vertex to ONE bone, so a key only needs the bone transforms: 32 bytes per bone per
# key (scientist: 22 bones, 760 vertices -> ~0.7 KB instead of ~29 KB per key). The runtime (GoldSrcVR/Model shader, _SkOn = 1)
# reads the transforms from one float texture and skins the vertex itself, so the visible behaviour of the original renderer is a
# matter of picking texel rows per frame: frame interpolation (slerp between two keys), the 2-pose sequence blend (aim pitch:
# numblends 2, blend byte / 255), the 0.2 s latched-sequence crossfade and the bone controllers (head / mouth / turret yaw +
# pitch) as model-space rotations about the controller bone's pivot.
#
# Player bodies (kind-1 model rows, HL-family packs): the gait, upper-body (per weapon class, 2 pitch blends) and death sequences of
# the player model as their own key sets; the runtime plays legs (LEGS_BIT bones) from the gait set and the rest from the upper set,
# turns the model to the gait yaw and gives the 4 spine controllers a quarter of the yaw difference each (StudioProcessGait). The upper
# sequences are stored relative to the attach bone (the pelvis: the gait bone the upper body hangs from, player row field 6); the shader
# places them on the gait layer's attach bone, which equals the original's local-space merge of the two sequences.
#
# Bone texture (RGBAFloat, SKL_TEX_W texels wide, row-major, linear texel index i -> (i % W, i / W)), per skinned model and key:
#   texel base + 2 b     quaternion of bone b (x, y, z, w) in Unity axes (model space of the W frame: GoldSrc (x, y, z) ->
#                        Unity (-y, z, x) · 0.0254, a reflection, so the rotation is C·R·Cᵀ)
#   texel base + 2 b + 1 bone origin (x, y, z, 0), metres
#   texel base + 2 NB + s  controller slot s: rotation axis (x, y, z, 0) in model space, already negated for the reflection
#                        (a rotation by +a about it = the original's +a on the bone's local angle)
#   stride = 2 NB + NS texels per key. Key k of a model = texel base + k · stride.
# A vertex = (bone, position and normal in that bone's frame, Unity axes): model position = q · local + origin.
#
# Controllers (mstudiobonecontroller: bone, type, start, end, index 0..3 / 4 = mouth): only rotations (XR / YR / ZR) become slots.
# Adding d to a bone's local Euler angle (R = Rz·Ry·Rx) equals a model-space rotation by d about an axis through the bone's
# origin: XR -> the bone's own x axis, YR -> parent · Rz(z) · y, ZR -> the parent's z axis, all taken from the pose without the
# controller. Descendants follow, and nested controllers compose when the deeper bone (and on one bone X before Y before Z)
# is applied first; slots are sorted that way, the runtime applies them in slot order. tools/test_studioskel.py checks the
# whole construction against mdl10.pose(ctl=...).
import array, math, struct, sys

INCH = 0.0254
SKL_TEX_W = 1024
SKL_VERSION = 1
MAX_SLOTS = 6                       # shader: _SkCA (slots 0..3) + _SkCB (4..5)
LEGS_BIT = 1 << 8                   # bone mask bit: driven by the gait sequence (player models)
T_X, T_Y, T_Z, T_XR, T_YR, T_ZR, T_RLOOP = 1, 2, 4, 8, 16, 32, 0x8000
ROT_TYPES = T_XR | T_YR | T_ZR
SKEL_TOL = 0.05                     # GoldSrc units: largest bone point error a skipped frame may leave (lerp between keys)
SKEL_KEYS_MAX = 64                  # keys per sequence (then the adaptive pick stops)
SKEL_TIP = 8.0                      # units: the error points are the bone origin + its x / y axis tips (rotations count too)


def to_u(p):
    return (-p[1] * INCH, p[2] * INCH, p[0] * INCH)


def to_u_dir(n):
    v = (-n[1], n[2], n[0])
    l = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
    return (v[0] / l, v[1] / l, v[2] / l) if l > 1e-12 else (0.0, 1.0, 0.0)


_C = ((0, -1, 0), (0, 0, 1), (1, 0, 0))


def rot_u(m):
    r = ((m[0], m[1], m[2]), (m[4], m[5], m[6]), (m[8], m[9], m[10]))
    return tuple(tuple(sum(_C[i][a] * r[a][b] * _C[j][b] for a in range(3) for b in range(3)) for j in range(3)) for i in range(3))


def quat_rows(r):
    """3x3 rotation rows -> unit quaternion (x, y, z, w), w >= 0 (Unity: q * v = r · v)."""
    t = r[0][0] + r[1][1] + r[2][2]
    if t > 0:
        s = math.sqrt(t + 1.0) * 2
        q = ((r[2][1] - r[1][2]) / s, (r[0][2] - r[2][0]) / s, (r[1][0] - r[0][1]) / s, 0.25 * s)
    elif r[0][0] > r[1][1] and r[0][0] > r[2][2]:
        s = math.sqrt(1.0 + r[0][0] - r[1][1] - r[2][2]) * 2
        q = (0.25 * s, (r[0][1] + r[1][0]) / s, (r[0][2] + r[2][0]) / s, (r[2][1] - r[1][2]) / s)
    elif r[1][1] > r[2][2]:
        s = math.sqrt(1.0 + r[1][1] - r[0][0] - r[2][2]) * 2
        q = ((r[0][1] + r[1][0]) / s, 0.25 * s, (r[1][2] + r[2][1]) / s, (r[0][2] - r[2][0]) / s)
    else:
        s = math.sqrt(1.0 + r[2][2] - r[0][0] - r[1][1]) * 2
        q = ((r[0][2] + r[2][0]) / s, (r[1][2] + r[2][1]) / s, 0.25 * s, (r[1][0] - r[0][1]) / s)
    n = math.sqrt(sum(c * c for c in q)) or 1.0
    q = tuple(c / n for c in q)
    return q if q[3] >= 0 else tuple(-c for c in q)


def qrot(q, v):
    """q * v (Unity convention), the shader's formula: v + 2 w (u x v) + 2 u x (u x v)."""
    ux, uy, uz, w = q
    cx, cy, cz = uy * v[2] - uz * v[1], uz * v[0] - ux * v[2], ux * v[1] - uy * v[0]
    cx, cy, cz = cx + w * v[0], cy + w * v[1], cz + w * v[2]
    return (v[0] + 2 * (uy * cz - uz * cy), v[1] + 2 * (uz * cx - ux * cz), v[2] + 2 * (ux * cy - uy * cx))


def nlerp(a, b, t):
    """The shader's key interpolation: shortest-way normalised lerp of two quaternions."""
    if sum(x * y for x, y in zip(a, b)) < 0:
        b = tuple(-x for x in b)
    q = tuple(x + (y - x) * t for x, y in zip(a, b))
    n = math.sqrt(sum(c * c for c in q)) or 1.0
    return tuple(c / n for c in q)


def axis_rotate(v, pivot, axis, ang):
    """Rodrigues rotation of point v about the line (pivot, unit axis) by ang radians (the shader's controller step)."""
    d = (v[0] - pivot[0], v[1] - pivot[1], v[2] - pivot[2])
    c, s = math.cos(ang), math.sin(ang)
    kd = axis[0] * d[0] + axis[1] * d[1] + axis[2] * d[2]
    kx = (axis[1] * d[2] - axis[2] * d[1], axis[2] * d[0] - axis[0] * d[2], axis[0] * d[1] - axis[1] * d[0])
    return tuple(pivot[i] + d[i] * c + kx[i] * s + axis[i] * kd * (1 - c) for i in range(3))


# ───────────── controllers ─────────────

def depth(m, b):
    n = 0
    while b >= 0 and n < 256:
        b = m.bones[b]["parent"]
        n += 1
    return n


def slots_of(m, log=None):
    """Rotation controller records as slots, deepest bone first (on one bone XR, YR, ZR). -> list of dicts with the record fields."""
    nb = len(m.bones)
    recs = []
    for i, c in enumerate(getattr(m, "controllers", ())):
        if not (0 <= c["bone"] < nb):
            continue
        if not (c["type"] & ROT_TYPES):
            if log:
                log(f"  {m.name}: translation controller on bone {m.bones[c['bone']]['name']} (type {c['type']:#x}) not packed")
            continue
        recs.append(dict(c, rec=i))
    order = {T_XR: 0, T_YR: 1, T_ZR: 2}
    recs.sort(key=lambda c: (-depth(m, c["bone"]), order.get(c["type"] & ROT_TYPES, 3), c["rec"]))
    if len(recs) > MAX_SLOTS and log:
        log(f"  {m.name}: {len(recs)} rotation controllers, only {MAX_SLOTS} packed")
    return recs[:MAX_SLOTS]


def bone_masks(m, slots, legs=()):
    """Per bone: bit s when slot s rotates it (the controller bone itself or a descendant), LEGS_BIT for gait bones."""
    nb = len(m.bones)
    out = [0] * nb
    for s, c in enumerate(slots):
        for b in range(nb):
            x = b
            while x >= 0:
                if x == c["bone"]:
                    out[b] |= 1 << s
                    break
                x = m.bones[x]["parent"]
    for b in legs:
        out[b] |= LEGS_BIT
    return out


def slot_axis_gs(m, vals, world, c):
    """Model-space (GoldSrc) axis of a rotation controller in this pose (see the header)."""
    b = c["bone"]
    p = m.bones[b]["parent"]
    t = c["type"] & ROT_TYPES
    if t == T_XR:
        w = world[b]
        a = (w[0], w[4], w[8])
    else:
        wp = world[p] if p >= 0 else (1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0)
        if t == T_ZR:
            a = (wp[2], wp[6], wp[10])
        else:
            z = vals[b][5]
            ly = (-math.sin(z), math.cos(z), 0.0)          # Rz(z) · y
            a = (wp[0] * ly[0] + wp[1] * ly[1], wp[4] * ly[0] + wp[5] * ly[1], wp[8] * ly[0] + wp[9] * ly[1])
    return a


def slot_axis_u(a_gs):
    """The reflection to Unity axes flips the rotation sense: store the negated axis so +angle stays +angle."""
    d = to_u_dir(a_gs)
    return (-d[0], -d[1], -d[2])


def ctl_value(c, byte):
    """Controller byte 0..255 -> degrees, the renderer's mapping: RLOOP = start + byte · 360/256, else lerp(start, end, byte / 255)."""
    if c["type"] & T_RLOOP:
        return byte * (360.0 / 256.0) + c["start"]
    v = max(0.0, min(1.0, byte / 255.0))
    return (1.0 - v) * c["start"] + v * c["end"]


def ctl_byte(c, angle):
    """Server SetBoneController: degrees -> byte (mirrors GsvrModelLoader.SkelControllerByte, the original's wrap / clamp rules)."""
    v = angle
    if c["type"] & ROT_TYPES:
        if c["end"] < c["start"]:
            v = -v
        if c["start"] + 359.0 >= c["end"]:
            mid = (c["start"] + c["end"]) / 2.0
            if v > mid + 180:
                v -= 360
            if v < mid - 180:
                v += 360
        elif v > 360:
            v -= int(v / 360.0) * 360.0
        elif v < 0:
            v += int((v / -360.0) + 1) * 360.0
    if c["end"] == c["start"]:
        return 0
    b = int(255 * (v - c["start"]) / (c["end"] - c["start"]))
    return max(0, min(255, b))


def mouth_value(c, mouthopen):
    v = max(0.0, min(1.0, mouthopen / 64.0))
    return (1.0 - v) * c["start"] + v * c["end"]


# ───────────── per-key bone rows ─────────────

def key_rows(m, vals, world, slots, lift=0.0):
    """-> list of float rows (4 floats each) for one key: 2 per bone (quat, origin) + 1 per slot (axis). lift = GoldSrc units added
    to z (player bodies: the feet at the origin, FEET_STAND / FEET_CROUCH like the baked player poses)."""
    rows = []
    for w in world:
        rows.append(quat_rows(rot_u(w)))
        p = to_u((w[3], w[7], w[11] + lift))
        rows.append((p[0], p[1], p[2], 0.0))
    for c in slots:
        a = slot_axis_u(slot_axis_gs(m, vals, world, c))
        rows.append((a[0], a[1], a[2], 0.0))
    return rows


def attach_bone(m, legs):
    """The gait bone the upper body hangs from (the parent of the first non-gait bone, Bip01 Pelvis); None if the non-gait bones do
    not all descend from it."""
    first = next((b for b in range(len(m.bones)) if b not in legs), None)
    if first is None:
        return None
    att = m.bones[first]["parent"]
    if att < 0 or att not in legs:
        return None
    for b in range(len(m.bones)):
        if b in legs:
            continue
        x = b
        while x >= 0 and x != att:
            x = m.bones[x]["parent"]
        if x != att:
            return None
    return att


def key_rows_rel(m, vals, world, slots, att):
    """key_rows for an upper-body sequence of a player model: every bone relative to the attach bone of the same pose
    (W_att^-1 · W_b), the controller axes likewise. The runtime puts them on the gait layer's attach bone, which reproduces the
    original's local-space merge (gait bones' locals from the gait sequence, the rest from the upper one, then one concat)."""
    wa = world[att]
    inv = (wa[0], wa[4], wa[8], -(wa[0] * wa[3] + wa[4] * wa[7] + wa[8] * wa[11]),
           wa[1], wa[5], wa[9], -(wa[1] * wa[3] + wa[5] * wa[7] + wa[9] * wa[11]),
           wa[2], wa[6], wa[10], -(wa[2] * wa[3] + wa[6] * wa[7] + wa[10] * wa[11]))
    rel = []
    for w in world:
        out = [0.0] * 12
        for r in range(3):
            for c in range(4):
                v = inv[r * 4] * w[c] + inv[r * 4 + 1] * w[4 + c] + inv[r * 4 + 2] * w[8 + c]
                if c == 3:
                    v += inv[r * 4 + 3]
                out[r * 4 + c] = v
        rel.append(out)
    rows = []
    for w in rel:
        rows.append(quat_rows(rot_u(w)))
        p = to_u((w[3], w[7], w[11]))
        rows.append((p[0], p[1], p[2], 0.0))
    for c in slots:
        a = slot_axis_gs(m, vals, world, c)
        a = (inv[0] * a[0] + inv[1] * a[1] + inv[2] * a[2], inv[4] * a[0] + inv[5] * a[1] + inv[6] * a[2], inv[8] * a[0] + inv[9] * a[1] + inv[10] * a[2])
        u = slot_axis_u(a)
        rows.append((u[0], u[1], u[2], 0.0))
    return rows


def error_points(world):
    out = []
    for w in world:
        out.append((w[3], w[7], w[11]))
        out.append((w[3] + w[0] * SKEL_TIP, w[7] + w[4] * SKEL_TIP, w[11] + w[8] * SKEL_TIP))
        out.append((w[3] + w[1] * SKEL_TIP, w[7] + w[5] * SKEL_TIP, w[11] + w[9] * SKEL_TIP))
    return out


def pick_keys(P, n, kmax, tol):
    """Adaptive keys (first + last frame, then the worst in-between until kmax or every in-between is within tol).
    P[f] = error points of frame f (all blends concatenated)."""
    if n < 2:
        return [0]

    def err(a, b, f):
        t = (f - a) / (b - a)
        worst = 0.0
        for pa, pb, pf in zip(P[a], P[b], P[f]):
            e = (pf[0] - (pa[0] + (pb[0] - pa[0]) * t)) ** 2 + (pf[1] - (pa[1] + (pb[1] - pa[1]) * t)) ** 2 \
                + (pf[2] - (pa[2] + (pb[2] - pa[2]) * t)) ** 2
            if e > worst:
                worst = e
        return worst
    keys = [0, n - 1]
    tol2 = tol * tol
    while len(keys) < kmax:
        we, wf = 0.0, None
        for a, b in zip(keys, keys[1:]):
            for f in range(a + 1, b):
                e = err(a, b, f)
                if e > we:
                    we, wf = e, f
        if wf is None or we < tol2:
            break
        keys.append(wf)
        keys.sort()
    return keys


def sequence_keys(m, si, kmax=SKEL_KEYS_MAX, tol=SKEL_TOL):
    """-> (key frames, {blend: [(vals, world) per key]}) of one sequence; numblends >= 2 also bakes blend 1 (the 2-pose blend)."""
    sq = m.sequences[si]
    n = max(1, sq["numframes"])
    nbl = 2 if sq["numblends"] >= 2 else 1
    frames = {}
    for bl in range(nbl):
        for f in range(n):
            vals = m.pose_locals(si, f, bl)
            frames[(bl, f)] = (vals, m.world_of(vals))
    P = {f: [p for bl in range(nbl) for p in error_points(frames[(bl, f)][1])] for f in range(n)}
    keys = pick_keys(P, n, kmax, tol)
    return keys, {bl: [frames[(bl, f)] for f in keys] for bl in range(nbl)}


# ───────────── vertices ─────────────

def local_vertex(m, key, ref_world):
    """Vertex key of mdl10.bake_parts (bodypart, model, vertex, normal, s, t, texture) -> (bone, local pos, local normal), Unity
    axes: position straight from the file (GoldSrc vertices are stored in their bone's frame), the normal moved from its own
    bone's frame into the vertex bone's frame at the reference pose (GoldSrc may bind a normal to another bone)."""
    bi, mi, vi, ni = key[0], key[1], key[2], key[3]
    mdl = m.bodyparts[bi]["models"][mi]
    vb = mdl["vbone"][vi]
    lp = to_u(mdl["verts"][vi])
    if ni < len(mdl["norms"]):
        nb = mdl["nbone"][ni]
        n = mdl["norms"][ni]
        if nb != vb:
            wn, wv = ref_world[nb], ref_world[vb]
            nm = (wn[0] * n[0] + wn[1] * n[1] + wn[2] * n[2], wn[4] * n[0] + wn[5] * n[1] + wn[6] * n[2],
                  wn[8] * n[0] + wn[9] * n[1] + wn[10] * n[2])
            n = (wv[0] * nm[0] + wv[4] * nm[1] + wv[8] * nm[2], wv[1] * nm[0] + wv[5] * nm[1] + wv[9] * nm[2],
                 wv[2] * nm[0] + wv[6] * nm[1] + wv[10] * nm[2])
        ln = to_u_dir(n)
    else:
        ln = (0.0, 1.0, 0.0)
    return vb, lp, ln


def skin_point(rows, bone, lp):
    q = rows[2 * bone]
    o = rows[2 * bone + 1]
    v = qrot(q, lp)
    return (v[0] + o[0], v[1] + o[1], v[2] + o[2])


# ───────────── chunk parse (checks / tests; mirrors GsvrModelLoader.DecodeSkel) ─────────────

def parse_skl1(b):
    """SKL1 payload bytes -> dict of the tables (lists) + the texture as a float array. Raises ValueError on a size mismatch."""
    h = struct.unpack_from("<16i", b, 0)
    ver, ns, tw, th, nmr, nc, na, nr, nbm, nv, nk, nu, npl, nps, npt = h[:15]
    need = 64 + 4 * (24 * ns + 4 * nmr + 8 * nc + 6 * na + 8 * nr + nbm + 7 * nv + 4 * nk + nu + 8 * npl + 16 * nps + npt) + 16 * tw * th
    if ver != SKL_VERSION or need != len(b):
        raise ValueError(f"SKL1 version {ver} / size {len(b)} != {need}")
    q = [64]

    def take(code, n):
        a = array.array(code)
        a.frombytes(b[q[0]:q[0] + 4 * n])
        if sys.byteorder != "little":
            a.byteswap()
        q[0] += 4 * n
        return a
    out = dict(w=tw, h=th)
    out["model"] = take("i", 16 * ns)
    out["modelf"] = take("f", 8 * ns)
    out["mesh"] = take("i", 4 * nmr)
    out["ctl"] = take("i", 4 * nc)
    out["ctlf"] = take("f", 4 * nc)
    out["att"] = take("i", 2 * na)
    out["attf"] = take("f", 4 * na)
    out["anim"] = take("i", 4 * nr)
    out["animf"] = take("f", 4 * nr)
    out["mask"] = take("i", nbm)
    out["verti"] = take("i", nv)
    out["vertf"] = take("f", 6 * nv)
    out["skin"] = take("i", 4 * nk)
    out["skinuv"] = take("f", nu)
    out["prow"] = take("i", 8 * npl)
    out["pseq"] = take("i", 8 * nps)
    out["pseqf"] = take("f", 8 * nps)
    out["ptimes"] = take("f", npt)
    out["tex"] = take("f", 4 * tw * th)
    out["models"] = [dict(zip(("kind", "owner", "nb", "stride", "key0", "keys", "ctl0", "nctl", "att0", "natt", "mask0", "mesh0", "nmesh",
                                "anim0", "nanim", "nslots"), out["model"][r * 16:(r + 1) * 16])) for r in range(ns)]
    out["mesh_of"] = {out["mesh"][e * 4]: e for e in range(nmr)}
    return out


def tex_row(skl, texel):
    t = skl["tex"]
    return tuple(t[texel * 4:texel * 4 + 4])


def mesh_pose_points(skl, mesh, pose):
    """Skinned model-space points (Unity axes, m) of a part mesh at key row `pose` of its model (no blend / controller)."""
    e = skl["mesh_of"][mesh]
    _m, row, v0, nv = skl["mesh"][e * 4:e * 4 + 4]
    mo = skl["models"][row]
    base = mo["key0"] + pose * mo["stride"]
    out = []
    for i in range(v0, v0 + nv):
        b = skl["verti"][i]
        lp = tuple(skl["vertf"][i * 6:i * 6 + 3])
        q = tex_row(skl, base + 2 * b)
        o = tex_row(skl, base + 2 * b + 1)
        v = qrot(q, lp)
        out.append((v[0] + o[0], v[1] + o[1], v[2] + o[2]))
    return out


# ───────────── chunk ─────────────

class SkelPack:
    """Collects the skinned models; chunk() -> the SKL1 bytes (layout in tools/modelpack.py's header)."""

    def __init__(self):
        self.models = []        # dict(kind, owner, nb, nslots, key0 (texel), keys, ctl0, nctl, att0, natt, mask0, mesh0, nmesh, anim0, nanim, bounds)
        self.texels = []        # flat float rows (4 floats each)
        self.meshes = []        # (mesh index, model row, vert0, nverts)
        self.verts_i = array.array("i")
        self.verts_f = array.array("f")
        self.ctl_i, self.ctl_f = array.array("i"), array.array("f")
        self.att_i, self.att_f = array.array("i"), array.array("f")
        self.anim_i, self.anim_f = array.array("i"), array.array("f")
        self.masks = array.array("i")
        self.skins = []         # (mesh index, family, [uv floats])
        # skinned player bodies (kind-1 model rows): per player (player index, model row, seqFirst, seqCount, hand bone, mesh, 0, 0),
        # per sequence int (model row, role 0 gait / 1 upper / 2 death, kind, class, keyFirst, keys, timeFirst, blend-1 first)
        # + float (fps, seconds, linear distance, loop, blend start, blend end, numframes, 0), key times (s)
        self.prow_i = array.array("i")
        self.pseq_i, self.pseq_f, self.ptimes = array.array("i"), array.array("f"), array.array("f")

    def add_texels(self, rows):
        base = len(self.texels)
        self.texels.extend(rows)
        return base

    def tex_size(self):
        n = max(1, len(self.texels))
        return SKL_TEX_W, -(-n // SKL_TEX_W)

    def chunk(self):
        w, h = self.tex_size()
        tex = array.array("f")
        for r in self.texels:
            tex.extend(r)
        tex.extend([0.0] * (w * h * 4 - len(tex)))
        mi, mf = array.array("i"), array.array("f")
        for mo in self.models:
            mi.extend([mo["kind"], mo["owner"], mo["nb"], 2 * mo["nb"] + mo["nslots"], mo["key0"], mo["keys"], mo["ctl0"], mo["nctl"],
                       mo["att0"], mo["natt"], mo["mask0"], mo["mesh0"], mo["nmesh"], mo["anim0"], mo["nanim"], mo["nslots"]])
            mf.extend(list(mo["bounds"]) + [0.0, 0.0])
        me = array.array("i")
        for row in self.meshes:
            me.extend(row)
        sk_i, sk_f = array.array("i"), array.array("f")
        for mesh, fam, uvs in self.skins:
            sk_i.extend([mesh, fam, len(sk_f), len(uvs) // 2])
            sk_f.extend(uvs)
        tabs = [mi, mf, me, self.ctl_i, self.ctl_f, self.att_i, self.att_f, self.anim_i, self.anim_f, self.masks,
                self.verts_i, self.verts_f, sk_i, sk_f, self.prow_i, self.pseq_i, self.pseq_f, self.ptimes, tex]
        if sys.byteorder != "little":
            for t in tabs:
                t.byteswap()
        hdr = struct.pack("<16i", SKL_VERSION, len(self.models), w, h, len(self.meshes), len(self.ctl_i) // 4, len(self.att_i) // 2,
                          len(self.anim_i) // 4, len(self.masks), len(self.verts_i), len(self.skins), len(sk_f),
                          len(self.prow_i) // 8, len(self.pseq_i) // 8, len(self.ptimes), 0)
        body = hdr + b"".join(t.tobytes() for t in tabs)
        return b"SKL1" + struct.pack("<i", len(body)) + body
