#!/usr/bin/env python3
"""CoD: World at War weapon pack for GSVR (VRChat): GSM1 + GSX1 chunks, built from the player's OWN WaW install.
Python 3 stdlib only (pypy3 is faster, CPython 3.9+ works: the frozen companion has no pypy). BYOG: nothing from the
game is in this repo; the pack is written where --out says (the companion cache), never into git.

  codwaw_pack.py build --game-root <WaW install> [--lang auto] [--zone-dir DIR] --out pack.gsm
                       [--weapons thompson_mp,kar98k_mp,m1garand_mp] [--arms viewmodel_rus_guard_arms,...]
                       [--max-px 512] [--max-clip-s 6] [--impacts small,large|none]
  codwaw_pack.py check pack.gsm          validate + decode every table, print a summary (exit 1 on any error)
  codwaw_pack.py info  pack.gsm --json   the decoded tables as JSON (without the big arrays)
  codwaw_pack.py --version               cache version of the converter output ("<VERSION>-r<PACK_REV>")

Progress: '@plan <stage>=<weight> ...' and '@progress <stage> <done> <total>' lines on stdout (same protocol as the
half-life companion's tools/bprog.py / companion/buildprog.py).

Why GSM1: the half-life companion already serves GSM1 packs at /pack/models.bin and the world's GsvrModelLoader decodes
them (header arithmetic, mesh build, atlas, AudioClip build, GSX1 chunk scan). A CoD pack in the same container loads
in the existing loader as it is: weapon records, P/W/V meshes (V = arms + gun, 4 key poses), sounds, sound events.
Everything CoD specific is in new GSX1 chunks that old loaders skip: skinned 30 fps view rigs (CSK1), the weapon
definition table of all MP weapons (WDF1 + WDJ1), the sound alias table (SAL1) and the hit marker image (CHUD).

── GSM1 part (exact layout: half-life tools/modelpack.py header; this writer uses header version 2, no player models) ──
  weapon [W*24] / weaponF [W*8] / mesh [M*8] / sound [S*6] / event [E*4] / idx / uv / pos / nrm / samples / atlas / names
  (GSX1 block at the end of names) / v2 tail: player [0] panim [0] panimF [0] hand [0] grip [W*4] (identity quaternions)
  weapon id = 1 + record index (GsvrModelLoader.MaxWeaponId is 63), weapon int 20 = the weapon's WDF1 row (all MP weapon
  files sorted by name; the full numbers live there), name = WaW internal name ("thompson_mp"). Mesh field 7 (wid) = the
  weapon id as well (-1 for the shared arms meshes).
  sound kinds (weapon int 4..13): 0 fire = fireSoundPlayer, 1 fire2 = fireLastSoundPlayer, 2 empty = emptyFireSoundPlayer,
    3 draw = raiseSoundPlayer, 4..6 reload1..3 = the first three notetrack sounds of the reload clip, 7 special1 = first
    notetrack sound of the rechamber clip (bolt), 8 special2 = meleeSwipeSoundPlayer, 9 special3 = meleeHitSound; the
    index is the FIRST variant of that alias (SAL1 has all variants + volume / pitch / distance). -1 = none.
  events: anim 0 reload, 1 draw (raise clip), 2 attack (fire clip): notetrack sounds, time ms, frame at 30 fps.
  weaponF: muzzleP (tag_flash in the P frame), muzzleV (tag_flash at idle frame 0, view frame), reload / raise seconds.
  mesh kinds: 0 P held (gun only, bind pose, gun frame: origin j_gun, barrel +Z), 1 W world model (LOD0, model space),
    2 V view (arms 0 + gun, 4 poses: 0 idle, 1 fire frame 1, 2 reload first sound note, 3 reload last sound note),
    7 skinned gun part (bind pose, view-rig model space, 1 pose), 8 skinned arms part (bind pose, 1 pose),
    9 target dummy (MP soldier body + head, posed, model space: feet at y 0, facing +Z; CDUM has its hit boxes).
    hideTags are applied: every surface whose bones all sit under a hidden tag is left out (P, W, V, 7).
  atlas: RGBA32, 4 mips, colour maps only (TS_COLOR_MAP), each texture <= --max-px (a lower stored IWI mip, no resampling),
    8 px edge-extended gutter. samples: mono float32 (stereo files are averaged), the sound's own (odd) rate.

FRAMES (same as the GoldSrc packs): Unity +X right, +Y up, +Z forward, metres. WaW (x fwd, y left, z up, inches) ->
  Unity (-y, z, x) * 0.0254. This is a reflection, so rotations convert as C R C^T: quaternion (x,y,z,w) -> (y,-z,-x,w),
  and the triangle winding is chosen per surface by a vote against the vertex normals (Unity front face = cross(b-a, c-a)
  along the normal). View frame origin = the eye (rig root tag_view), +Z = view direction.

── GSX1 chunks (payload layouts; all int32 / float32 little endian, names = (offset, length) into the GSM1 names region) ──
"CSK1" skinned view rigs, one per (weapon, arms model):
  int32 hdr[12]  version 1, rigs R, bones B, clips C, notes N, skin rows K, weight verts V, texels T, texW 1024,
                 slots S (= NUM_WEAP_ANIMS 35, the WEAP_ANIM_* enum), 0, 0
  int32 rig[R*12]   weapon record, arms index, boneFirst, boneCount, clipFirst, clipCount, armsNameOff, armsNameLen,
                    tag_flash, j_gun, tag_brass, tag_view (rig-local bone or -1)
  int32 slot[R*S]   per WEAP_ANIM slot: global clip index or -1 (e.g. slot 9 RELOAD, 13 RAISE, 15 DROP, 33 ADS_UP)
  int32 bone[B*4]   parent (rig-local, -1 = root), nameOff, nameLen, flags (bit0 arms model, bit1 gun model, bit2 hidden tag)
  float boneF[B*8]  bind pose in rig model space: quat xyzw, pos xyz (m, Unity axes), 0   (the space the meshes are in)
  int32 clip[C*8]   rig, frameCount F, texelFirst, noteFirst, noteCount, nameOff, nameLen (xanim name), flags (bit0 loop)
  float clipF[C*4]  fps (30), seconds, source fps (24 or 30), 0
  int32 note[N*4]   clip, SAL1 alias row (-1 = no sound), nameOff, nameLen (notetrack name, "end" included)
  float noteF[N*2]  seconds from the clip start, frame (fractional, 30 fps)
  int32 skin[K*4]   GSM1 mesh (kind 7 / 8), rig, vertFirst, vertCount (= the mesh's vertex count, same order)
  int32 vbone[V]    4 rig-local bone indices, one byte each (b0 | b1 << 8 | b2 << 16 | b3 << 24)
  float vweight[V*4] weights (sum 1, unused = 0)
  float tex[T*4]    bone texture (RGBAFloat, texW wide): frame f of a clip, bone b: texel texelFirst + f * 2 * boneCount + 2 b
                    = world quaternion xyzw, + 1 = world position xyz, 0 (rig model space = view frame, Unity axes, m)
  skinning: v' = sum_i w_i * (Wq_i * (Bq_i^-1 * (v - Bt_i)) + Wt_i)  (W = tex pose of the frame, B = boneF bind)
"WDF1" weapon definition table (every weapons/mp/* WEAPONFILE of the install's iwds, later iwds override earlier ones):
  int32 hdr[4]   rows R, columns C, enum strings E, 0
  int32 col[C*4] nameOff, nameLen (WEAPONFILE key), enumFirst, enumCount (0 = numeric column)
  int32 enum[E*2] nameOff, nameLen            int32 row[R*2] nameOff, nameLen (weapon file name, "thompson_mp")
  float val[R*C] the value (enum columns: index into the column's enum strings); NaN = not set / not a number
  Units as in the files: seconds, game units (inches), degrees; docs/weapondef.md has the key meanings.
"WDJ1" int32 byteLen + UTF-8 JSON (padded to 4): {"version": 1, "weapons": {name: waw_weapon.summary(...)}} (strings too)
"SAL1" sound aliases (weapon sounds, notetrack sounds, layers, MP_hit_alert, bullet impacts):
  int32 hdr[4]     aliases A, variants V, 0, 0
  int32 alias[A*6] nameOff, nameLen, variantFirst, variantCount, secondary alias row (-1), chain alias row (-1)
  int32 var[V*8]   GSM1 sound (-1 = streamed / unsupported codec / no file), flags, sequence, volumeFalloffCurve, limitType,
                   entityLimitCount, fileNameOff, fileNameLen (original LoadedSound name)
  float varF[V*12] volMin, volMax, pitchMin, pitchMax, distMin, distMax, distReverbMax (units), probability, slavePercentage,
                   reverbSend, occlusionLevel, startDelay (ms)
"CDUM" target dummies (original MP soldier models of the localized map zone, body + head, posed at frame 0 of the MP stand anim):
  int32 hdr[4]       dummies D, boxes B, 0, 0
  int32 dum[D*8]     GSM1 mesh (kind 9), boxFirst, boxCount, bodyNameOff, bodyNameLen, headNameOff, headNameLen, team (0 allies, 1 axis)
  float dumF[D*8]    mins xyz, maxs xyz (Unity m, model space, feet at y 0, facing +Z), eye height (tag_eye y, m), 0
  int32 box[B*4]     hit location (HITLOC_*: 1 helmet, 2 head, 3 neck, 4 torso upper, 5 torso lower, 6/7 right/left arm upper,
                     8/9 arm lower, 10/11 hand, 12/13 right/left leg upper, 14/15 leg lower, 16/17 foot), boneNameOff, boneNameLen, dummy
  float boxF[B*12]   centre xyz, half extents xyz (box local axes), rotation quat xyzw (Unity, model space), 0, 0
  The boxes are the model's own XBoneInfo bounds (bone-local hit boxes the game traces bullets against, DObj trace) of every
  bone with partClassification != 0, at the dummy pose; partClassification is the hit location. A bullet takes the box it
  enters first.
"CHUD" HUD images: int32 count, 0; per image int32[8] nameOff, nameLen, w, h, byteOffset (into the pixel area after the
  table), 0, 0, 0 + float[8] drawW, drawH, offsetX, offsetY (virtual 640x480 screen px), fadeSeconds, 0, 0, 0; then the
  RGBA32 pixels (top row first). damage_feedback = the MP hit marker (maps/mp/gametypes/_damagefeedback.gsc: 24x48 at
  (-12, -12) from the centre, alpha 1 -> 0 over 1 s), damage_feedback_j = the flak jacket variant.
"""
import argparse
import glob
import json
import math
import os
import struct
import sys
import time
import zipfile
from array import array

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import waw_ff  # noqa: E402
import waw_zone  # noqa: E402
import waw_xmodel as X  # noqa: E402
import waw_xanim  # noqa: E402
import waw_pose  # noqa: E402
import waw_export  # noqa: E402
import waw_weapon  # noqa: E402
import waw_tex  # noqa: E402
import waw_iwd  # noqa: E402
import t4_schema as S  # noqa: E402

VERSION = "cwp-1.0"
PACK_REV = 2                # 2: CDUM target dummies (kind 9 meshes + hit boxes)
MAGIC = b"GSM1"
HEADER = 80
GSM_VERSION = 2
MIPS = 4
GUTTER = 8
FPS = 30.0
TEX_W = 1024
INCH = 0.0254
MAX_PACK = 100 * 1024 * 1024
NUM_SLOTS = S.ENUMS["NUM_WEAP_ANIMS"]
SLOT_NAMES = {v: k[len("WEAP_ANIM_"):].lower() for k, v in S.ENUMS.items()
              if k.startswith("WEAP_ANIM_") and v < NUM_SLOTS}
SLOT_OF = {n: v for v, n in SLOT_NAMES.items()}
KINDS = ("fire", "fire2", "empty", "draw", "reload1", "reload2", "reload3", "special1", "special2", "special3")
K_P, K_W, K_V, K_SKIN_GUN, K_SKIN_ARMS, K_DUMMY = 0, 1, 2, 7, 8, 9
# target dummies: (body xmodel, head xmodel, team 0 allies / 1 axis) from the localized MP map zones, posed at frame 0 of DUMMY_ANIM
DEFAULT_DUMMIES = (("char_rus_guard_player_body_rifle", "char_rus_guard_player_head_rifle", 0),
                   ("char_ger_hnrgd_player_body_rifle", "char_ger_hnrgd_player_head_rifle", 1))
DUMMY_ANIM = "pb_stand_alert"
DUMMY_MAX_PX = 256             # dummy textures: seen at 10-100 m on the range, 512 px would double the atlas
DEFAULT_WEAPONS = ("thompson_mp", "kar98k_mp", "m1garand_mp")
DEFAULT_ARMS = ("viewmodel_rus_guard_arms", "viewmodel_ger_waffen_rolledup_arms")
HUD_IMAGES = (("damage_feedback", 24.0, 48.0, -12.0, -12.0, 1.0), ("damage_feedback_j", 24.0, 48.0, -12.0, -12.0, 1.0))
EXTRA_ALIASES = ("MP_hit_alert", "bullet_impact_headshot", "bullet_impact_headshot_2", "bullet_impact_headshot_helmet",
                 "melee_hit", "melee_knife_hit_body", "melee_knife_hit_other")
ENUM_KEYS = ("weaponType", "weaponClass", "fireType", "penetrateType", "impactType", "inventoryType", "clipType",
             "offhandClass", "stance", "activeReticleType", "playerAnimType", "overlayReticle", "overlayInterface",
             "guidedMissileType", "projExplosionType")
STAGES = {"zone": 15, "weapons": 30, "textures": 25, "sounds": 25, "write": 5}


def log(*a):
    print(*a, flush=True)


def plan(**w):
    print("@plan " + " ".join(f"{k}={v}" for k, v in w.items()), flush=True)


_plast = {}


def progress(stage, done, total):
    total = max(1, int(total))
    done = max(0, min(int(done), total))
    now = time.monotonic()
    if done < total and now - _plast.get(stage, -1.0) < 0.25:
        return
    _plast[stage] = now
    print(f"@progress {stage} {done} {total}", flush=True)


# ───────────────────────────── frames ─────────────────────────────
def u_pos(p):
    return (-p[1] * INCH, p[2] * INCH, p[0] * INCH)


def u_dir(n):
    v = (-n[1], n[2], n[0])
    ln = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
    return (v[0] / ln, v[1] / ln, v[2] / ln) if ln > 1e-12 else (0.0, 1.0, 0.0)


def u_quat(q):
    """WaW rotation -> Unity rotation for the reflection (x,y,z)->(-y,z,x): C R C^T = quaternion (y, -z, -x, w)."""
    x, y, z, w = X.qnorm(q)
    if w < 0:
        x, y, z, w = -x, -y, -z, -w
    return (y, -z, -x, w)


def skin_mats(world, bind):
    out = []
    for (wq, wt), (bq, bt) in zip(world, bind):
        q = X.qnorm(X.qmul(wq, X.qconj(X.qnorm(bq))))
        r = X.qrot(q, bt)
        out.append((q, (wt[0] - r[0], wt[1] - r[1], wt[2] - r[2])))
    return out


def skin_surface(s, mats, off):
    P, N = [], []
    for p, n, wl in zip(s["pos"], s["nrm"], s["weights"]):
        x = y = z = nx = ny = nz = 0.0
        for bi, wgt in wl:
            q, t = mats[off + bi]
            v = X.qrot(q, p)
            m = X.qrot(q, n)
            x += wgt * (v[0] + t[0]); y += wgt * (v[1] + t[1]); z += wgt * (v[2] + t[2])
            nx += wgt * m[0]; ny += wgt * m[1]; nz += wgt * m[2]
        P.append((x, y, z)); N.append((nx, ny, nz))
    return P, N


def winding(tris, P, N):
    """triangles in Unity front-face order: keep (a,b,c) when cross(b-a, c-a) agrees with the vertex normals for the
    majority of the surface's triangles (P, N already in Unity axes), else (a,c,b) for all."""
    agree = 0
    for a, b, c in tris:
        pa, pb, pc = P[a], P[b], P[c]
        e1 = (pb[0] - pa[0], pb[1] - pa[1], pb[2] - pa[2])
        e2 = (pc[0] - pa[0], pc[1] - pa[1], pc[2] - pa[2])
        cr = (e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0])
        nn = (N[a][0] + N[b][0] + N[c][0], N[a][1] + N[b][1] + N[c][1], N[a][2] + N[b][2] + N[c][2])
        d = cr[0] * nn[0] + cr[1] * nn[1] + cr[2] * nn[2]
        agree += 1 if d > 0 else (-1 if d < 0 else 0)
    flip = agree < 0
    return [(a, c, b) if flip else (a, b, c) for a, b, c in tris], flip


# ───────────────────────────── binary: GSM1 + GSX1 ─────────────────────────────
def _i(a):
    return array("i", a)


def _f(a):
    return array("f", a)


def _b(a):
    if sys.byteorder != "little":
        a = array(a.typecode, a)
        a.byteswap()
    return a.tobytes()


def _arr(code, data, off, n):
    a = array(code)
    a.frombytes(data[off:off + n * a.itemsize])
    if len(a) != n:
        raise ValueError(f"truncated table ({len(a)} of {n})")
    if sys.byteorder != "little":
        a.byteswap()
    return a, off + n * a.itemsize


def _pad4(b):
    return b + b"\0" * (-len(b) % 4)


def enc_csk1(o, name):
    h = _i([1, len(o["rigs"]), len(o["bones"]), len(o["clips"]), len(o["notes"]), len(o["skins"]), len(o["vbone"]),
            len(o["tex"]) // 4, o["texW"], NUM_SLOTS, 0, 0])
    rig, slot, bone, boneF, clip, clipF, note, noteF, skin = (_i([]), _i([]), _i([]), _f([]), _i([]), _f([]), _i([]),
                                                              _f([]), _i([]))
    for r in o["rigs"]:
        no, nl = name(r["armsName"])
        rig.extend([r["weapon"], r["arms"], r["boneFirst"], r["boneCount"], r["clipFirst"], r["clipCount"], no, nl]
                   + list(r["tags"]))
        if len(r["slots"]) != NUM_SLOTS:
            raise ValueError("rig slot map must have NUM_SLOTS entries")
        slot.extend(r["slots"])
    for b in o["bones"]:
        no, nl = name(b["name"])
        bone.extend([b["parent"], no, nl, b["flags"]])
        boneF.extend(list(b["q"]) + list(b["t"]) + [0.0])
    for c in o["clips"]:
        no, nl = name(c["name"])
        clip.extend([c["rig"], c["frames"], c["texelFirst"], c["noteFirst"], c["noteCount"], no, nl, c["flags"]])
        clipF.extend([c["fps"], c["seconds"], c["srcFps"], 0.0])
    for n in o["notes"]:
        no, nl = name(n["name"])
        note.extend([n["clip"], n["alias"], no, nl])
        noteF.extend([n["seconds"], n["frame"]])
    for k in o["skins"]:
        skin.extend([k["mesh"], k["rig"], k["vertFirst"], k["vertCount"]])
    return b"".join(_b(a) for a in (h, rig, slot, bone, boneF, clip, clipF, note, noteF, skin, o["vbone"], o["vweight"],
                                     o["tex"]))


def dec_csk1(p, s):
    h, off = _arr("i", p, 0, 12)
    ver, R, B, C, N, K, V, T, tw, ns = h[:10]
    if ver != 1 or ns != NUM_SLOTS or tw <= 0:
        raise ValueError("CSK1 header")
    rig, off = _arr("i", p, off, R * 12)
    slot, off = _arr("i", p, off, R * ns)
    bone, off = _arr("i", p, off, B * 4)
    boneF, off = _arr("f", p, off, B * 8)
    clip, off = _arr("i", p, off, C * 8)
    clipF, off = _arr("f", p, off, C * 4)
    note, off = _arr("i", p, off, N * 4)
    noteF, off = _arr("f", p, off, N * 2)
    skin, off = _arr("i", p, off, K * 4)
    vbone, off = _arr("i", p, off, V)
    vweight, off = _arr("f", p, off, V * 4)
    tex, off = _arr("f", p, off, T * 4)
    if off != len(p):
        raise ValueError(f"CSK1 size {len(p)} != {off}")
    o = {"texW": tw, "vbone": vbone, "vweight": vweight, "tex": tex, "rigs": [], "bones": [], "clips": [], "notes": [],
         "skins": []}
    for i in range(R):
        r = rig[i * 12:i * 12 + 12]
        o["rigs"].append({"weapon": r[0], "arms": r[1], "boneFirst": r[2], "boneCount": r[3], "clipFirst": r[4],
                          "clipCount": r[5], "armsName": s(r[6], r[7]), "tags": list(r[8:12]),
                          "slots": list(slot[i * ns:i * ns + ns])})
    for i in range(B):
        o["bones"].append({"parent": bone[i * 4], "name": s(bone[i * 4 + 1], bone[i * 4 + 2]), "flags": bone[i * 4 + 3],
                           "q": tuple(boneF[i * 8:i * 8 + 4]), "t": tuple(boneF[i * 8 + 4:i * 8 + 7])})
    for i in range(C):
        c = clip[i * 8:i * 8 + 8]
        o["clips"].append({"rig": c[0], "frames": c[1], "texelFirst": c[2], "noteFirst": c[3], "noteCount": c[4],
                           "name": s(c[5], c[6]), "flags": c[7], "fps": clipF[i * 4], "seconds": clipF[i * 4 + 1],
                           "srcFps": clipF[i * 4 + 2]})
    for i in range(N):
        o["notes"].append({"clip": note[i * 4], "alias": note[i * 4 + 1], "name": s(note[i * 4 + 2], note[i * 4 + 3]),
                           "seconds": noteF[i * 2], "frame": noteF[i * 2 + 1]})
    for i in range(K):
        o["skins"].append({"mesh": skin[i * 4], "rig": skin[i * 4 + 1], "vertFirst": skin[i * 4 + 2],
                           "vertCount": skin[i * 4 + 3]})
    return o


def enc_wdf1(o, name):
    col, enum, row = _i([]), _i([]), _i([])
    for c in o["cols"]:
        no, nl = name(c["name"])
        col.extend([no, nl, len(enum) // 2, len(c["enum"])])
        for e in c["enum"]:
            enum.extend(name(e))
    for r in o["rows"]:
        row.extend(name(r))
    if len(o["val"]) != len(o["rows"]) * len(o["cols"]):
        raise ValueError("WDF1 value count")
    return b"".join(_b(a) for a in (_i([len(o["rows"]), len(o["cols"]), len(enum) // 2, 0]), col, enum, row, o["val"]))


def dec_wdf1(p, s):
    h, off = _arr("i", p, 0, 4)
    R, C, E = h[0], h[1], h[2]
    col, off = _arr("i", p, off, C * 4)
    enum, off = _arr("i", p, off, E * 2)
    row, off = _arr("i", p, off, R * 2)
    val, off = _arr("f", p, off, R * C)
    if off != len(p):
        raise ValueError("WDF1 size")
    ens = [s(enum[2 * i], enum[2 * i + 1]) for i in range(E)]
    cols = []
    for i in range(C):
        no, nl, ef, ec = col[i * 4:i * 4 + 4]
        if ef < 0 or ef + ec > E:
            raise ValueError("WDF1 enum range")
        cols.append({"name": s(no, nl), "enum": ens[ef:ef + ec]})
    return {"cols": cols, "rows": [s(row[2 * i], row[2 * i + 1]) for i in range(R)], "val": val}


def enc_wdj1(o, name):
    b = o["json"].encode("utf-8")
    return struct.pack("<i", len(b)) + _pad4(b)


def dec_wdj1(p, s):
    n = struct.unpack_from("<i", p, 0)[0]
    if n < 0 or 4 + n > len(p) or len(p) != 4 + n + (-n % 4):
        raise ValueError("WDJ1 size")
    return {"json": p[4:4 + n].decode("utf-8")}


def enc_sal1(o, name):
    al, va, vf = _i([]), _i([]), _f([])
    for a in o["aliases"]:
        no, nl = name(a["name"])
        al.extend([no, nl, a["varFirst"], a["varCount"], a["secondary"], a["chain"]])
    for v in o["variants"]:
        fo, fl = name(v["file"])
        va.extend([v["sound"], v["flags"], v["sequence"], v["falloff"], v["limitType"], v["limitCount"], fo, fl])
        vf.extend(v["f"])
    return b"".join(_b(a) for a in (_i([len(o["aliases"]), len(o["variants"]), 0, 0]), al, va, vf))


SAL_F = ("volMin", "volMax", "pitchMin", "pitchMax", "distMin", "distMax", "distReverbMax", "probability",
         "slavePercentage", "reverbSend", "occlusionLevel", "startDelay")


def dec_sal1(p, s):
    h, off = _arr("i", p, 0, 4)
    A, V = h[0], h[1]
    al, off = _arr("i", p, off, A * 6)
    va, off = _arr("i", p, off, V * 8)
    vf, off = _arr("f", p, off, V * 12)
    if off != len(p):
        raise ValueError("SAL1 size")
    o = {"aliases": [], "variants": []}
    for i in range(A):
        r = al[i * 6:i * 6 + 6]
        o["aliases"].append({"name": s(r[0], r[1]), "varFirst": r[2], "varCount": r[3], "secondary": r[4], "chain": r[5]})
    for i in range(V):
        r = va[i * 8:i * 8 + 8]
        o["variants"].append({"sound": r[0], "flags": r[1], "sequence": r[2], "falloff": r[3], "limitType": r[4],
                              "limitCount": r[5], "file": s(r[6], r[7]), "f": list(vf[i * 12:i * 12 + 12])})
    return o


def enc_cdum(o, name):
    dum, dumF, box, boxF = _i([]), _f([]), _i([]), _f([])
    for d in o["dummies"]:
        bo, bl = name(d["body"])
        ho, hl = name(d["head"])
        dum.extend([d["mesh"], d["boxFirst"], d["boxCount"], bo, bl, ho, hl, d["team"]])
        dumF.extend(list(d["mins"]) + list(d["maxs"]) + [d["eye"], 0.0])
    for b in o["boxes"]:
        no, nl = name(b["bone"])
        box.extend([b["loc"], no, nl, b["dummy"]])
        boxF.extend(list(b["c"]) + list(b["h"]) + list(b["q"]) + [0.0, 0.0])
    return b"".join(_b(a) for a in (_i([len(o["dummies"]), len(o["boxes"]), 0, 0]), dum, dumF, box, boxF))


def dec_cdum(p, s):
    h, off = _arr("i", p, 0, 4)
    D, B = h[0], h[1]
    dum, off = _arr("i", p, off, D * 8)
    dumF, off = _arr("f", p, off, D * 8)
    box, off = _arr("i", p, off, B * 4)
    boxF, off = _arr("f", p, off, B * 12)
    if off != len(p):
        raise ValueError("CDUM size")
    o = {"dummies": [], "boxes": []}
    for i in range(D):
        r, f = dum[i * 8:i * 8 + 8], dumF[i * 8:i * 8 + 8]
        if r[1] < 0 or r[1] + r[2] > B:
            raise ValueError("CDUM box range")
        o["dummies"].append({"mesh": r[0], "boxFirst": r[1], "boxCount": r[2], "body": s(r[3], r[4]), "head": s(r[5], r[6]),
                             "team": r[7], "mins": tuple(f[0:3]), "maxs": tuple(f[3:6]), "eye": f[6]})
    for i in range(B):
        r, f = box[i * 4:i * 4 + 4], boxF[i * 12:i * 12 + 12]
        if not 0 < r[0] < 19 or not 0 <= r[3] < D:
            raise ValueError("CDUM box values")
        o["boxes"].append({"loc": r[0], "bone": s(r[1], r[2]), "dummy": r[3], "c": tuple(f[0:3]), "h": tuple(f[3:6]),
                           "q": tuple(f[6:10])})
    return o


def enc_chud(o, name):
    tab, pix = bytearray(struct.pack("<2i", len(o["images"]), 0)), bytearray()
    for im in o["images"]:
        no, nl = name(im["name"])
        tab += struct.pack("<8i8f", no, nl, im["w"], im["h"], len(pix), 0, 0, 0, *im["draw"], 0.0, 0.0, 0.0)
        if len(im["rgba"]) != im["w"] * im["h"] * 4:
            raise ValueError("CHUD pixel size")
        pix += _pad4(bytes(im["rgba"]))
    return bytes(tab + pix)


def dec_chud(p, s):
    n = struct.unpack_from("<i", p, 0)[0]
    if not 0 <= n <= 64:
        raise ValueError("CHUD count")
    base = 8 + 64 * n
    o = {"images": []}
    for i in range(n):
        r = struct.unpack_from("<8i8f", p, 8 + 64 * i)
        no, nl, w, h, bo = r[:5]
        a = base + bo
        if w <= 0 or h <= 0 or a + w * h * 4 > len(p):
            raise ValueError("CHUD image range")
        o["images"].append({"name": s(no, nl), "w": w, "h": h, "draw": list(r[8:13]), "rgba": p[a:a + w * h * 4]})
    return o


CHUNKS = {"CSK1": (enc_csk1, dec_csk1), "WDF1": (enc_wdf1, dec_wdf1), "WDJ1": (enc_wdj1, dec_wdj1),
          "SAL1": (enc_sal1, dec_sal1), "CHUD": (enc_chud, dec_chud), "CDUM": (enc_cdum, dec_cdum)}


def encode(pk):
    """pack dict (see decode) -> GSM1 bytes. Deterministic: names are appended in table order, never deduplicated, so
    encode(decode(b)) == b."""
    names = bytearray()

    def name(s):
        b = (s or "").encode("ascii", "replace")
        off = len(names)
        names.extend(b)
        return off, len(b)

    wint, wflt = _i([]), _f([])
    for w in pk["weapons"]:
        no, nl = name(w["name"])
        if len(w["sounds"]) != 10:
            raise ValueError("weapon sounds must have 10 kinds")
        wint.extend([w["id"], w["meshP"], w["meshW"], w["meshV"]] + list(w["sounds"]) +
                    [w["ev0"], w["nev"], no, nl, w["flags"], w["animClass"], w.get("wdfRow", -1), 0, 0, 0])
        wflt.extend(list(w["muzzleP"]) + list(w["muzzleV"]) + [w["reload_s"], w["draw_s"]])
    mtab, idx, uv, pos, nrm = _i([]), _i([]), _f([]), _f([]), _f([])
    for m in pk["meshes"]:
        v = len(m["uv"]) // 2
        mtab.extend([v, len(m["idx"]), len(m["poses"]), len(uv) // 2, len(idx), len(pos) // 3, m["kind"], m["wid"]])
        idx.extend(m["idx"])
        uv.extend(m["uv"])
        for p, n in m["poses"]:
            if len(p) != 3 * v or len(n) != 3 * v:
                raise ValueError("pose size != vertex count")
            pos.extend(p)
            nrm.extend(n)
    stab, samples = _i([]), _f([])
    for s in pk["sounds"]:
        no, nl = name(s["name"])
        stab.extend([s["rate"], len(s["samples"]), len(samples), 1, no, nl])
        samples.extend(s["samples"])
    etab = _i([])
    for e in pk["events"]:
        etab.extend(e)
    chunks = []
    for tag, obj in pk["chunks"]:
        payload = CHUNKS[tag][0](obj, name)
        if len(payload) % 4:
            raise ValueError(f"{tag} payload not 4-aligned")
        chunks.append(tag.encode() + struct.pack("<i", len(payload)) + payload)
    names.extend(b"\0" * (-len(names) % 4))
    if chunks:
        body = b"".join(chunks)
        names.extend(body + struct.pack("<i", len(body) + 8) + b"GSX1")
    grip = _f([])
    for w in pk["weapons"]:
        grip.extend(w["grip"])
    at = pk["atlas"]
    secs = [wint, wflt, mtab, stab, etab, idx, uv, pos, nrm, samples]
    hdr = MAGIC + struct.pack("<19i", HEADER, GSM_VERSION, len(pk["weapons"]), len(pk["meshes"]), len(pk["sounds"]),
                              len(pk["events"]), len(uv) // 2, len(idx), len(pos) // 3, len(samples), at["w"], at["h"],
                              at["mips"], len(names), pk.get("flags", 1), 0, 0, 0, 0)
    return b"".join([hdr] + [_b(a) for a in secs] + [bytes(at["data"]), bytes(names), _b(grip)])


def validate(data):
    """GSM1 header arithmetic (same as the half-life companion's validate_models / GsvrModelLoader) -> header tuple."""
    size = len(data)
    if size < HEADER or data[:4] != MAGIC:
        raise ValueError("not a GSM1 pack")
    if size > MAX_PACK:
        raise ValueError(f"pack too large for VRChat ({size} > {MAX_PACK})")
    hdr, ver, w, m, s, e, u, i, p, n, aw, ah, mips, nb, _fl = struct.unpack_from("<15i", data, 4)
    q, a, h, _r = struct.unpack_from("<4i", data, 64)
    if ((ver, hdr) not in ((1, 64), (2, 80)) or not 0 <= q <= 64 or not 0 <= a <= 65536 or not 0 <= h <= 1 << 20
            or not 0 < w <= 256 or not 0 < m <= 4096 or not 0 <= s <= 4096 or not 0 <= e <= 65536
            or u <= 0 or i <= 0 or i % 3 or p < u or n < 0 or not 8 <= aw <= 8192 or not 8 <= ah <= 8192
            or aw % 8 or ah % 8 or not 1 <= mips <= 4 or nb < 0 or nb % 4):
        raise ValueError("GSM1 header values out of range")
    atlas = sum((aw >> l) * (ah >> l) * 4 for l in range(mips))
    need = hdr + 4 * (w * 32 + m * 8 + s * 6 + e * 4 + i + u * 2 + p * 6 + n) + atlas + nb
    if ver == 2:
        need += 4 * (q * 8 + a * 8 + h * 8 + w * 4)
    if need != size:
        raise ValueError(f"GSM1 size mismatch: header needs {need} bytes, file has {size}")
    return hdr, ver, w, m, s, e, u, i, p, n, aw, ah, mips, nb, _fl, q, a, h


def gsx1_chunks(names):
    """names region -> [(tag, payload bytes)] of its GSX1 block ([] without one)."""
    if len(names) < 8 or names[-4:] != b"GSX1":
        return [], len(names)
    blk = struct.unpack_from("<i", names, len(names) - 8)[0]
    if blk < 8 or blk > len(names) or blk % 4:
        raise ValueError(f"bad GSX1 block size {blk}")
    p, stop, out = len(names) - blk, len(names) - 8, []
    start = p
    while p < stop:
        tag = names[p:p + 4].decode("ascii", "replace")
        sz = struct.unpack_from("<i", names, p + 4)[0]
        if sz < 0 or sz % 4 or p + 8 + sz > stop:
            raise ValueError(f"GSX1 chunk {tag} size invalid")
        out.append((tag, names[p + 8:p + 8 + sz]))
        p += 8 + sz
    return out, start


def decode(data):
    """GSM1 bytes -> pack dict {weapons, meshes, sounds, events, atlas, chunks [(tag, obj)], flags}. Unknown chunks are kept
    as raw bytes. Every table is range-checked; raises ValueError on anything malformed."""
    (hdr, ver, W, M, Sn, E, U, I, P, N, aw, ah, mips, nb, fl, Q, A, H) = validate(data)
    off = hdr
    wint, off = _arr("i", data, off, W * 24)
    wflt, off = _arr("f", data, off, W * 8)
    mtab, off = _arr("i", data, off, M * 8)
    stab, off = _arr("i", data, off, Sn * 6)
    etab, off = _arr("i", data, off, E * 4)
    idx, off = _arr("i", data, off, I)
    uv, off = _arr("f", data, off, U * 2)
    pos, off = _arr("f", data, off, P * 3)
    nrm, off = _arr("f", data, off, P * 3)
    samples, off = _arr("f", data, off, N)
    asz = sum((aw >> l) * (ah >> l) * 4 for l in range(mips))
    atlas = data[off:off + asz]
    off += asz
    names = data[off:off + nb]
    off += nb
    if Q or A or H:
        raise ValueError("player tables are not used in CoD packs")
    grip, off = _arr("f", data, off, W * 4)

    def s(o, n):
        if o < 0 or n < 0 or o + n > nb:
            raise ValueError("name out of range")
        return names[o:o + n].decode("ascii")

    pk = {"flags": fl, "atlas": {"w": aw, "h": ah, "mips": mips, "data": atlas}, "weapons": [], "meshes": [],
          "sounds": [], "events": [], "chunks": []}
    for k in range(W):
        r = wint[k * 24:k * 24 + 24]
        f = wflt[k * 8:k * 8 + 8]
        pk["weapons"].append({"id": r[0], "meshP": r[1], "meshW": r[2], "meshV": r[3], "sounds": list(r[4:14]),
                              "ev0": r[14], "nev": r[15], "name": s(r[16], r[17]), "flags": r[18], "animClass": r[19],
                              "wdfRow": r[20],
                              "muzzleP": tuple(f[0:3]), "muzzleV": tuple(f[3:6]), "reload_s": f[6], "draw_s": f[7],
                              "grip": tuple(grip[k * 4:k * 4 + 4])})
        for mi in r[1:4]:
            if not -1 <= mi < M:
                raise ValueError("weapon mesh index out of range")
    for k in range(M):
        v, ic, npo, uvb, ib, pb, kind, wid = mtab[k * 8:k * 8 + 8]
        if v <= 0 or v > 65000 or ic % 3 or npo <= 0 or uvb + v > U or ib + ic > I or pb + v * npo > P:
            raise ValueError(f"mesh {k} ranges")
        mi = idx[ib:ib + ic]
        if mi and (min(mi) < 0 or max(mi) >= v):
            raise ValueError(f"mesh {k} index out of range")
        poses = [(pos[3 * (pb + j * v):3 * (pb + (j + 1) * v)], nrm[3 * (pb + j * v):3 * (pb + (j + 1) * v)])
                 for j in range(npo)]
        pk["meshes"].append({"kind": kind, "wid": wid, "idx": mi, "uv": uv[2 * uvb:2 * (uvb + v)], "poses": poses})
    for k in range(Sn):
        rate, cnt, base, ch, no, nl = stab[k * 6:k * 6 + 6]
        if rate <= 0 or cnt < 0 or base < 0 or base + cnt > N or ch != 1:
            raise ValueError(f"sound {k} ranges")
        pk["sounds"].append({"name": s(no, nl), "rate": rate, "samples": samples[base:base + cnt]})
    for k in range(E):
        e = tuple(etab[k * 4:k * 4 + 4])
        if not -1 <= e[2] < Sn:
            raise ValueError("event sound out of range")
        pk["events"].append(e)
    chunks, _start = gsx1_chunks(names)
    for tag, payload in chunks:
        if tag in CHUNKS:
            pk["chunks"].append((tag, CHUNKS[tag][1](payload, s)))
        else:
            pk["chunks"].append((tag, payload))
    return pk


def chunk(pk, tag):
    for t, o in pk["chunks"]:
        if t == tag:
            return o
    return None


# ───────────────────────────── atlas ─────────────────────────────
def half_rgba(w, h, data):
    """2x2 box filter of an RGBA32 image (w, h even) with big-int bit tricks (fast in CPython): avg(a, b) per byte =
    (a & b) + (((a ^ b) >> 1) & 0x7f..)."""
    nw, nh = w // 2, h // 2
    mask = int.from_bytes(b"\x7f" * (nw * 4), "little")
    out = bytearray()
    mv = memoryview(bytes(data)).cast("I")
    row = w
    for y in range(nh):
        r0 = mv[2 * y * row:(2 * y + 1) * row]
        r1 = mv[(2 * y + 1) * row:(2 * y + 2) * row]
        vals = []
        for r in (r0, r1):
            e = int.from_bytes(r[0::2].tobytes(), "little")
            o = int.from_bytes(r[1::2].tobytes(), "little")
            vals.append((e & o) + (((e ^ o) >> 1) & mask))
        a, b = vals
        out += ((a & b) + (((a ^ b) >> 1) & mask)).to_bytes(nw * 4, "little")
    return out


def pack_atlas(textures, gutter=GUTTER, mips=MIPS):
    """textures {key: (w, h, rgba)} -> (aw, ah, data with mips, rects {key: (x, y, w, h)}); shelf packing, tallest first,
    edge-extended gutter, sizes multiples of 8 (so 4 mip levels divide exactly)."""
    if not textures:
        return 8, 8, bytes(sum((8 >> l) * (8 >> l) * 4 for l in range(mips))), {}
    keys = sorted(textures, key=lambda k: (-textures[k][1], -textures[k][0], k))
    area = sum((textures[k][0] + 2 * gutter) * (textures[k][1] + 2 * gutter) for k in keys)
    widest = max(textures[k][0] + 2 * gutter for k in keys)
    aw = 256
    while aw < widest or aw * aw < area * 1.15:
        aw *= 2
    aw = min(aw, 8192)
    rects, x, y, shelf = {}, 0, 0, 0
    for k in keys:
        w, h, _ = textures[k]
        cw, ch = w + 2 * gutter, h + 2 * gutter
        if x + cw > aw:
            x, y, shelf = 0, y + shelf, 0
        rects[k] = (x + gutter, y + gutter, w, h)
        x += cw
        shelf = max(shelf, ch)
    ah = y + shelf
    ah += -ah % 8
    if ah > 8192:
        raise ValueError("atlas too large")
    img = bytearray(aw * ah * 4)
    for k in keys:
        w, h, rgba = textures[k]
        rx, ry, _, _ = rects[k]
        for yy in range(-gutter, h + gutter):
            sy = min(h - 1, max(0, yy))
            src = rgba[4 * w * sy:4 * w * (sy + 1)]
            line = bytes(src[0:4]) * gutter + bytes(src) + bytes(src[4 * (w - 1):4 * w]) * gutter
            o = 4 * ((ry + yy) * aw + rx - gutter)
            img[o:o + len(line)] = line
    levels, cur, cw, chh = [bytes(img)], img, aw, ah
    for _ in range(1, mips):
        cur = half_rgba(cw, chh, cur)
        cw, chh = cw // 2, chh // 2
        levels.append(bytes(cur))
    return aw, ah, b"".join(levels), rects


# ───────────────────────────── install ─────────────────────────────
def find_lang(game_root, want="auto"):
    zd = os.path.join(game_root, "zone")
    try:
        langs = sorted(d for d in os.listdir(zd) if os.path.isfile(os.path.join(zd, d, "common_mp.ff")))
    except OSError:
        langs = []
    if not langs:
        raise FileNotFoundError(f"no zone/<language>/common_mp.ff under {game_root}")
    if want and want != "auto":
        for d in langs:
            if d.lower() == want.lower():
                return d
        raise FileNotFoundError(f"language {want} not installed (have: {', '.join(langs)})")
    for pref in ("english", "german", "french", "italian", "spanish", "russian", "polish", "japanese"):
        for d in langs:
            if d.lower() == pref:
                return d
    return langs[0]


def open_walker(path):
    _, u = waw_ff.load_zone(path, cache="")
    w = waw_zone.Walker(u)
    w.walk()
    return w


def load_weapon_files(main_dir):
    """weapons/mp/<name> WEAPONFILEs of every iwd; a later iwd (sorted name order) overrides an earlier one."""
    out = {}
    for f in sorted(glob.glob(os.path.join(main_dir, "*.iwd"))):
        try:
            z = zipfile.ZipFile(f)
        except (OSError, zipfile.BadZipFile):
            continue
        for n in z.namelist():
            if n.lower().startswith(waw_weapon.MP_PREFIX) and not n.endswith("/"):
                try:
                    out[n[len(waw_weapon.MP_PREFIX):].lower()] = waw_weapon.parse_weaponfile(z.read(n))
                except ValueError:
                    pass
    return out


def weapon_table(files):
    rows = sorted(files)
    keys, seen = [], set()
    for r in rows:
        for k, v in files[r].items():
            if k in seen:
                continue
            if k in ENUM_KEYS or isinstance(v, (int, float)) and not isinstance(v, bool):
                seen.add(k)
                keys.append(k)
    # a column is numeric only if every set value of it is a number (else it is an enum when listed, or dropped)
    cols = []
    for k in keys:
        vals = [files[r].get(k) for r in rows]
        strs = sorted({v for v in vals if isinstance(v, str) and v != ""})
        if k in ENUM_KEYS:
            cols.append({"name": k, "enum": strs})
        elif not strs:
            cols.append({"name": k, "enum": []})
    val = _f([])
    nan = float("nan")
    for r in rows:
        for c in cols:
            v = files[r].get(c["name"])
            if c["enum"]:
                val.append(float(c["enum"].index(v)) if isinstance(v, str) and v in c["enum"] else nan)
            else:
                val.append(float(v) if isinstance(v, (int, float)) and v != "" else nan)
    return {"cols": cols, "rows": rows, "val": val}


# ───────────────────────────── builder ─────────────────────────────
class Builder:
    def __init__(self, game_root, lang="auto", zone_dir=None, weapons=DEFAULT_WEAPONS, arms=DEFAULT_ARMS, max_px=512,
                 max_clip_s=6.0, impacts=("small", "large"), dummies=DEFAULT_DUMMIES):
        self.game_root = game_root
        self.main_dir = os.path.join(game_root, "main")
        self.lang = find_lang(game_root, lang) if not zone_dir else (lang if lang != "auto" else "custom")
        self.zone_dir = zone_dir or os.path.join(game_root, "zone", self.lang)
        self.weapon_names = list(weapons)
        self.arms_names = list(arms)
        self.max_px = max_px
        self.max_clip_s = max_clip_s
        self.impacts = tuple(impacts)
        self.dummy_names = list(dummies)
        self.meshes = []            # pending: {kind, wid, texkeys, st, poses [(P, N)], tris}
        self.textures = {}          # key -> (w, h, rgba)
        self.tex_missing = set()
        self.sounds = []            # {name, rate, samples}
        self.sound_of = {}          # LoadedSound name -> index (or -1)
        self.unsupported = {}       # LoadedSound name -> reason
        self.alias_rows = {}        # alias name (lower) -> row
        self.aliases, self.variants = [], []
        self.stats = {}

    # ---- textures
    def tex(self, name, max_px=None):
        if not name:
            return None
        k = name.lower()
        if k in self.textures:
            return k
        if k in self.tex_missing:
            return None
        if k not in self.iwi:
            self.tex_missing.add(k)
            return None
        try:
            b = waw_tex.read_iwi(self.iwi, k)
            hd = waw_iwd.iwi_header(b)
            lv = waw_tex.level_for(hd, max_px or self.max_px)
            _, w, h, rgba = waw_tex.decode_mip(b, lv)
        except Exception as ex:          # noqa: BLE001 - a broken / unsupported image only costs its texture
            log(f"texture {name}: {ex}")
            self.tex_missing.add(k)
            return None
        self.textures[k] = (w, h, bytes(rgba))
        progress("textures", len(self.textures), len(self.textures) + 2)
        return k

    # ---- meshes
    def add_mesh(self, kind, wid, surfs, max_px=None):
        """surfs: [(surface dict, P (WaW), N (WaW))] -> GSM1 mesh index (Unity axes, per-surface winding vote).
        max_px: texture size cap of this mesh (default --max-px)."""
        texkeys, st, Pu, Nu, idx = [], [], [], [], []
        flips = 0
        for s, P, N in surfs:
            base = len(Pu)
            pu = [u_pos(p) for p in P]
            nu = [u_dir(n) for n in N]
            tk = self.tex(s["images"].get("TS_COLOR_MAP"), max_px)
            tris, fl = winding(s["tris"], pu, nu)
            flips += fl
            for a, b, c in tris:
                idx.extend((base + a, base + b, base + c))
            Pu += pu
            Nu += nu
            for uu, vv in s["uv"]:
                texkeys.append(tk)
                st.append((min(1.0, max(0.0, uu)), min(1.0, max(0.0, vv))))
        if not Pu:
            return -1
        self.meshes.append({"kind": kind, "wid": wid, "texkeys": texkeys, "st": st, "idx": idx,
                            "poses": [(Pu, Nu)]})
        return len(self.meshes) - 1

    def add_pose(self, mi, surfs):
        Pu, Nu = [], []
        for _s, P, N in surfs:
            Pu += [u_pos(p) for p in P]
            Nu += [u_dir(n) for n in N]
        if len(Pu) != len(self.meshes[mi]["texkeys"]):
            raise ValueError("pose vertex count")
        self.meshes[mi]["poses"].append((Pu, Nu))

    # ---- sounds / aliases
    def loaded_sound(self, ls):
        nm = ls.get("name") if isinstance(ls, dict) else None
        if not isinstance(nm, str):
            return -1, ""
        nm = nm.lstrip(",")
        if nm in self.sound_of:
            return self.sound_of[nm], nm
        si = -1
        d = (ls.get("sound") or {}).get("data")
        if isinstance(d, waw_zone.Blob):
            b = d.bytes()
            try:
                si = self.decode_sound(nm, b)
            except Exception as ex:      # noqa: BLE001
                self.unsupported[nm] = str(ex)[:80]
        else:
            self.unsupported[nm] = "no data in the zone (streamed)"
        self.sound_of[nm] = si
        progress("sounds", len(self.sound_of), len(self.sound_of) + 4)
        return si, nm

    def decode_sound(self, nm, riff):
        if riff[:4] != b"RIFF" or riff[8:12] != b"WAVE":
            raise ValueError(f"not a WAVE RIFF ({riff[8:12]!r}, e.g. xWMA)")
        ch = waw_ff.riff_chunks(riff)
        tag, nch, rate = struct.unpack_from("<HHI", ch[b"fmt "], 0)
        if tag == 2:
            nch, rate, pcm, _ = waw_ff.msadpcm_decode(riff)
        elif tag == 1 and struct.unpack_from("<H", ch[b"fmt "], 14)[0] == 16:
            pcm = ch[b"data"]
        else:
            raise ValueError(f"codec 0x{tag:x} not supported")
        a = array("h")
        a.frombytes(pcm[:len(pcm) // 2 * 2])
        if sys.byteorder != "little":
            a.byteswap()
        cap = int(self.max_clip_s * rate) * nch
        if len(a) > cap:
            a = a[:cap]
            fade = min(len(a) // nch, int(0.05 * rate))
            for i in range(fade):
                g = i / fade
                for c in range(nch):
                    k = len(a) - (i + 1) * nch + c
                    a[k] = int(a[k] * g)
        if nch == 1:
            sm = array("f", [x / 32768.0 for x in a])
        else:
            sc = 1.0 / (32768.0 * nch)
            sm = array("f", [sum(a[i:i + nch]) * sc for i in range(0, len(a) - nch + 1, nch)])
        self.sounds.append({"name": nm, "rate": rate, "samples": sm})
        return len(self.sounds) - 1

    def alias(self, name):
        """alias name -> SAL1 row (adds it, its variants, sounds and its secondary / chain layers), -1 if unknown."""
        if not name:
            return -1
        k = name.lower()
        if k in self.alias_rows:
            return self.alias_rows[k]
        al = self.alias_index.get(k)
        if al is None:
            self.alias_rows[k] = -1
            return -1
        row = len(self.aliases)
        self.alias_rows[k] = row
        ent = {"name": al["aliasName"], "varFirst": len(self.variants), "varCount": 0, "secondary": -1, "chain": -1}
        self.aliases.append(ent)
        sec, chn = [], []
        for v in (al.get("head") or []):
            if not isinstance(v, dict):
                continue
            sf = v.get("soundFile") or {}
            si, fn = -1, ""
            if isinstance(sf, dict) and sf.get("type") == 1:
                si, fn = self.loaded_sound(((sf.get("u") or {}).get("loadSnd")))
            elif isinstance(sf, dict) and sf.get("type") == 2:
                fn = "stream"
            self.variants.append({"sound": si, "flags": int(v.get("flags") or 0), "sequence": int(v.get("sequence") or 0),
                                  "falloff": int(v.get("volumeFalloffCurve") or 0),
                                  "limitType": int(v.get("limitType") or 0),
                                  "limitCount": int(v.get("entityLimitCount") or 0), "file": fn,
                                  "f": [float(v.get(f) or 0.0) for f in SAL_F[:-1]] + [float(v.get("startDelay") or 0)]})
            ent["varCount"] += 1
            if v.get("secondaryAliasName"):
                sec.append(v["secondaryAliasName"])
            if v.get("chainAliasName"):
                chn.append(v["chainAliasName"])
        if sec:
            ent["secondary"] = self.alias(sec[0])
        if chn:
            ent["chain"] = self.alias(chn[0])
        return row

    def first_sound(self, row):
        if row < 0:
            return -1
        a = self.aliases[row]
        for v in self.variants[a["varFirst"]:a["varFirst"] + a["varCount"]]:
            if v["sound"] >= 0:
                return v["sound"]
        return -1

    # ---- the run
    def run(self, out):
        t0 = time.time()
        plan(**STAGES)
        progress("zone", 0, 3)
        common = os.path.join(self.zone_dir, "common_mp.ff")
        self.zw = open_walker(common)
        progress("zone", 1, 3)
        self.alias_index = {}
        for (tn, nm), o in self.zw.by_name.items():
            if tn == "snd_alias_list_t":
                self.alias_index.setdefault(nm.lower(), o)
        self.iwi = waw_tex.iwi_index(self.main_dir)
        files = load_weapon_files(self.main_dir)
        if not files:
            raise FileNotFoundError(f"no weapons/mp/* weapon files in {self.main_dir}/*.iwd")
        wdf = weapon_table(files)
        progress("zone", 2, 3)
        arms = self.find_arms()
        progress("zone", 3, 3)
        log(f"install {self.game_root} lang {self.lang}: {len(files)} MP weapon files, {len(self.iwi)} images, "
            f"arms {', '.join(m['name'] for m in arms)} ({time.time() - t0:.1f} s)")
        weapons, events, csk = [], [], {"texW": TEX_W, "rigs": [], "bones": [], "clips": [], "notes": [], "skins": [],
                                        "vbone": _i([]), "vweight": _f([]), "tex": _f([])}
        arms_mesh = [None] * len(arms)
        for wi, wn in enumerate(self.weapon_names):
            progress("weapons", wi, len(self.weapon_names))
            weapons.append(self.weapon(wn, wi, wdf, arms, arms_mesh, events, csk))
        progress("weapons", len(self.weapon_names), len(self.weapon_names))
        for nm in EXTRA_ALIASES:
            self.alias(nm)
        for kind in self.impacts:
            for k in sorted(self.alias_index):
                if k.startswith(f"bullet_{kind}_") and "_exit_" not in k:
                    self.alias(k)
        progress("sounds", 1, 1)
        cdum = self.dummies() if self.dummy_names else {"dummies": [], "boxes": []}
        hud = {"images": []}
        for nm, dw, dh, ox, oy, fade in HUD_IMAGES:
            if nm in self.iwi:
                _, w, h, rgba = waw_iwd.decode_iwi(waw_tex.read_iwi(self.iwi, nm))
                hud["images"].append({"name": nm, "w": w, "h": h, "draw": [dw, dh, ox, oy, fade], "rgba": bytes(rgba)})
        summ = {}
        for r in wdf["rows"]:
            try:
                summ[r] = waw_weapon.summary(files[r])
            except Exception as ex:      # noqa: BLE001 - a malformed file keeps its WDF1 numbers
                summ[r] = {"error": str(ex)[:120]}
        wdj = {"json": json.dumps({"version": 1, "weapons": summ}, separators=(",", ":"), sort_keys=True,
                                  ensure_ascii=True)}
        progress("textures", 1, 2)
        aw, ah, atlas, rects = pack_atlas(self.textures)
        progress("textures", 2, 2)
        meshes = []
        for m in self.meshes:
            uv = []
            for tk, (s_, t_) in zip(m["texkeys"], m["st"]):
                if tk is None:
                    uv += [0.0, 0.0]
                    continue
                x, y, w, h = rects[tk]
                uv += [(x + s_ * w) / aw, (y + t_ * h) / ah]
            meshes.append({"kind": m["kind"], "wid": m["wid"], "idx": m["idx"], "uv": uv,
                           "poses": [([c for p in P for c in p], [c for n in N for c in n]) for P, N in m["poses"]]})
        pk = {"flags": 1, "atlas": {"w": aw, "h": ah, "mips": MIPS, "data": atlas}, "weapons": weapons,
              "meshes": meshes, "sounds": self.sounds, "events": events,
              "chunks": [("CSK1", csk), ("WDF1", wdf), ("WDJ1", wdj),
                         ("SAL1", {"aliases": self.aliases, "variants": self.variants}), ("CHUD", hud)]
              + ([("CDUM", cdum)] if cdum["dummies"] else [])}
        progress("write", 0, 1)
        data = encode(pk)
        validate(data)
        tmp = out + ".part"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, out)
        progress("write", 1, 1)
        nsamp = sum(len(s["samples"]) for s in self.sounds)
        self.stats = {"bytes": len(data), "weapons": len(weapons), "meshes": len(meshes),
                      "verts": sum(len(m["uv"]) // 2 for m in meshes),
                      "poseVerts": sum(len(m["uv"]) // 2 * len(m["poses"]) for m in meshes),
                      "atlas": [aw, ah], "atlasBytes": len(atlas), "textures": len(self.textures),
                      "texMissing": sorted(self.tex_missing), "sounds": len(self.sounds), "samples": nsamp,
                      "soundBytes": nsamp * 4, "soundSeconds": round(sum(len(s["samples"]) / s["rate"] for s in self.sounds), 1),
                      "unsupportedSounds": len(self.unsupported), "aliases": len(self.aliases),
                      "variants": len(self.variants), "rigs": len(csk["rigs"]), "clips": len(csk["clips"]),
                      "boneTexels": len(csk["tex"]) // 4, "csk1Bytes": len(enc_csk1(csk, lambda s: (0, 0))),
                      "wdf1": [len(wdf["rows"]), len(wdf["cols"])], "wdj1Bytes": len(wdj["json"]),
                      "hud": [im["name"] for im in hud["images"]], "dummies": len(cdum["dummies"]),
                      "dummyBoxes": len(cdum["boxes"]), "seconds": round(time.time() - t0, 1)}
        log(f"{out}: {len(weapons)} weapons, {len(meshes)} meshes ({self.stats['verts']} verts, "
            f"{self.stats['poseVerts']} pose verts), atlas {aw}x{ah}x{MIPS} ({len(self.textures)} textures, "
            f"{len(atlas) / 1e6:.2f} MB), {len(self.sounds)} sounds ({self.stats['soundSeconds']} s, {nsamp * 4 / 1e6:.2f} MB), "
            f"{len(self.aliases)} aliases / {len(self.variants)} variants, {len(csk['rigs'])} rigs / {len(csk['clips'])} clips "
            f"({self.stats['csk1Bytes'] / 1e6:.2f} MB CSK1), WDF1 {len(wdf['rows'])}x{len(wdf['cols'])}, "
            f"{len(data)} bytes ({len(data) / 1e6:.2f} MB) in {self.stats['seconds']} s")
        if self.unsupported:
            log(f"sounds left out ({len(self.unsupported)}): " +
                ", ".join(f"{k} ({v})" for k, v in sorted(self.unsupported.items())[:8]))
        if self.tex_missing:
            log(f"textures not found: {', '.join(sorted(self.tex_missing))}")
        return self.stats

    def find_models(self, want):
        """xmodels by name from the localized MP zones of the install (localized_mp_dome first), then common_mp ->
        {name: (model dict, raw zone XModel)}; names not found are missing from the result."""
        found = {}
        cands = [os.path.join(self.zone_dir, "localized_mp_dome.ff")]
        cands += sorted(p for p in glob.glob(os.path.join(self.zone_dir, "localized_mp_*.ff")) if p not in cands)
        cands.append(os.path.join(self.zone_dir, "common_mp.ff"))
        for p in cands:
            if all(a in found for a in want):
                break
            if not os.path.isfile(p):
                continue
            w = self.zw if p.endswith(os.sep + "common_mp.ff") else open_walker(p)
            for a in want:
                raw = w.by_name.get(("XModel", a)) or w.by_name.get(("XModel", "," + a))
                if a not in found and raw is not None:
                    found[a] = (X.model_from_zone(w, a), raw)
        return found

    def find_arms(self):
        """arms models (default rus guard + ger waffen) from the localized MP zones of the install (localized_mp_dome first)."""
        want = list(self.arms_names)
        found = self.find_models(want)
        miss = [a for a in want if a not in found]
        if miss:
            raise FileNotFoundError(f"arms model(s) {', '.join(miss)} not found in {self.zone_dir}")
        return [found[a][0] for a in want]

    def dummies(self):
        """CDUM: the MP soldier models (body + head) posed at frame 0 of DUMMY_ANIM as kind-9 meshes + their hit boxes
        (XBoneInfo bounds of every bone with a partClassification). Missing models are skipped (logged)."""
        want = sorted({n for b, h, _t in self.dummy_names for n in (b, h)})
        found = self.find_models(want)
        anim = None
        if ("XAnimParts", DUMMY_ANIM) in self.zw.by_name:
            anim = waw_xanim.decode(self.zw, DUMMY_ANIM)
        out = {"dummies": [], "boxes": []}
        for body_n, head_n, team in self.dummy_names:
            if body_n not in found or head_n not in found:
                log(f"dummy {body_n} + {head_n}: not in the zones, skipped")
                continue
            (body, braw), (head, hraw) = found[body_n], found[head_n]
            rig = waw_pose.build_rig([(body, None), (head, head["bones"][0]["name"])])
            world = waw_pose.pose(rig, waw_xanim.sample(anim, 0) if anim else None)
            mats = skin_mats(world, waw_pose.bind_world(rig))
            surfs = []
            for model, off in rig["models"]:
                for sf in model["lods"][0]["surfs"]:
                    surfs.append((sf,) + skin_surface(sf, mats, off))
            mi = self.add_mesh(K_DUMMY, -1, surfs, DUMMY_MAX_PX)
            P = self.meshes[mi]["poses"][0][0]
            lo = tuple(min(p[k] for p in P) for k in range(3))
            hi = tuple(max(p[k] for p in P) for k in range(3))
            eb = rig["byname"].get("tag_eye")
            eye = u_pos(world[eb][1])[1] if eb is not None else hi[1] * 0.93
            d = {"mesh": mi, "boxFirst": len(out["boxes"]), "boxCount": 0, "body": body_n, "head": head_n, "team": team,
                 "mins": lo, "maxs": hi, "eye": eye}
            for (model, off), raw in zip(rig["models"], (braw, hraw)):
                pc = raw.get("partClassification")
                bi = raw.get("boneInfo")
                pcv = pc.values() if isinstance(pc, waw_zone.Blob) else (pc or [])
                biv = bi.values() if isinstance(bi, waw_zone.Blob) else (bi or [])
                for b, (cls, inf) in enumerate(zip(pcv, biv)):
                    if not cls or not inf or float(inf.get("radiusSquared") or 0.0) <= 0.0:
                        continue
                    blo, bhi = inf["bounds"][0]["v"], inf["bounds"][1]["v"]
                    cl = tuple((blo[k] + bhi[k]) * 0.5 for k in range(3))
                    hw = tuple((bhi[k] - blo[k]) * 0.5 for k in range(3))
                    q, t = world[off + b]
                    r = X.qrot(X.qnorm(q), cl)
                    out["boxes"].append({"loc": int(cls), "bone": model["bones"][b]["name"], "dummy": len(out["dummies"]),
                                         "c": u_pos((t[0] + r[0], t[1] + r[1], t[2] + r[2])),
                                         # box local axes under the reflection: WaW local (x, y, z) -> Unity local (-y, z, x)
                                         "h": (abs(hw[1]) * INCH, abs(hw[2]) * INCH, abs(hw[0]) * INCH), "q": u_quat(q)})
                    d["boxCount"] += 1
            log(f"dummy {body_n} + {head_n}: {len(P)} verts, {d['boxCount']} hit boxes, height {hi[1] - lo[1]:.2f} m, "
                f"eye {eye:.2f} m{'' if anim else ' (bind pose: ' + DUMMY_ANIM + ' not in common_mp)'}")
            out["dummies"].append(d)
        return out

    def weapon(self, wn, wi, wdf, arms, arms_mesh, events, csk):
        zw = self.zw
        wd = zw.by_name.get(("WeaponDef", wn))
        if wd is None:
            raise KeyError(f"weapon {wn} not in common_mp")
        info = waw_export.weapon_info(zw, wd)
        hide = set(info["hideTags"])
        gun = X.model_from_zone(zw, info["viewModel"])
        world = X.model_from_zone(zw, info["worldModel"]) if info["worldModel"] else None
        hid_g = set(waw_export.hidden_surfaces(gun, hide))
        wdf_row = wdf["rows"].index(wn) if wn in wdf["rows"] else -1
        wid = wi + 1
        gsurfs = [s for i, s in enumerate(gun["lods"][0]["surfs"]) if i not in hid_g]
        meshW = -1
        if world:
            hid_w = set(waw_export.hidden_surfaces(world, hide))
            meshW = self.add_mesh(K_W, wid, [(s, s["pos"], s["nrm"]) for i, s in enumerate(world["lods"][0]["surfs"])
                                             if i not in hid_w])
        gbn = {b["name"]: i for i, b in enumerate(gun["bones"])}
        # anims (decoded once, shared by the rigs of every arms model)
        anims, slot_anim = {}, {}
        for slot, an in info["anims"].items():
            if slot not in SLOT_OF or ("XAnimParts", an) not in zw.by_name:
                continue
            if an not in anims:
                anims[an] = waw_xanim.decode(zw, an)
            slot_anim[SLOT_OF[slot]] = an
        ntmap = {k.lower(): v for k, v in info["notetrackSoundMap"].items()}

        def note_alias(n):
            a = ntmap.get(n.lower(), n)
            return self.alias(a) if a else -1

        sounds_by_field = {k: self.alias(v) for k, v in info["sounds"].items()}
        clip_notes = {}
        for an, a in anims.items():
            notes = []
            for n, t in a["notes"]:
                sec = t * a["duration"]
                notes.append((n, sec, note_alias(n) if n != "end" else -1))
            clip_notes[an] = notes
        # rigs: one per arms model; the first one also gives the GSM1 V mesh
        meshV, muzzleV = -1, (0.0, 0.0, 0.0)
        gun_skin_mesh = None
        for ai, am in enumerate(arms):
            rig = waw_pose.build_rig([(am, None), (gun, "tag_weapon")])
            bind = waw_pose.bind_world(rig)
            na = len(am["bones"])
            r = {"weapon": wi, "arms": ai, "boneFirst": len(csk["bones"]), "boneCount": len(rig["bones"]),
                 "clipFirst": len(csk["clips"]), "clipCount": 0, "armsName": am["name"],
                 "tags": [rig["byname"].get(t, -1) for t in ("tag_flash", "j_gun", "tag_brass", "tag_view")],
                 "slots": [-1] * NUM_SLOTS}
            if len(rig["bones"]) > 255:
                raise ValueError("rig has more than 255 bones")
            hidden_bones = set()
            for bi, b in enumerate(rig["bones"]):
                p, chain = bi, []
                while p >= 0:
                    chain.append(rig["bones"][p]["name"])
                    p = rig["bones"][p]["parent"]
                if bi >= na and any(c in hide for c in chain):
                    hidden_bones.add(bi)
                bq, bt = bind[bi]
                csk["bones"].append({"parent": b["parent"], "name": b["name"],
                                     "flags": (1 if bi < na else 2) | (4 if bi in hidden_bones else 0),
                                     "q": u_quat(bq), "t": u_pos(bt)})
            clip_of = {}
            for an, a in anims.items():
                frames = waw_xanim.bake(a, FPS) if a["framerate"] else [waw_xanim.sample(a, 0)]
                c = {"rig": len(csk["rigs"]), "frames": len(frames), "texelFirst": len(csk["tex"]) // 4,
                     "noteFirst": len(csk["notes"]), "noteCount": 0, "name": a["name"], "flags": 1 if a["loop"] else 0,
                     "fps": FPS, "seconds": a["duration"], "srcFps": a["framerate"]}
                for fr in frames:
                    for q, t in waw_pose.pose(rig, fr):
                        uq, ut = u_quat(q), u_pos(t)
                        csk["tex"].extend(uq)
                        csk["tex"].extend((ut[0], ut[1], ut[2], 0.0))
                for n, sec, arow in clip_notes[an]:
                    csk["notes"].append({"clip": len(csk["clips"]), "alias": arow, "name": n, "seconds": sec,
                                         "frame": sec * FPS})
                    c["noteCount"] += 1
                clip_of[an] = len(csk["clips"])
                csk["clips"].append(c)
                r["clipCount"] += 1
            for sl, an in slot_anim.items():
                r["slots"][sl] = clip_of[an]
            # skinned meshes: arms part once per arms model, the gun part once per weapon (both bind pose)
            if arms_mesh[ai] is None:
                arms_mesh[ai] = self.add_mesh(K_SKIN_ARMS, -1, [(s, s["pos"], s["nrm"]) for s in am["lods"][0]["surfs"]])
            if gun_skin_mesh is None:
                gun_skin_mesh = self.add_mesh(K_SKIN_GUN, wid, [(s, s["pos"], s["nrm"]) for s in gsurfs])
            for mesh, surfs, off in ((arms_mesh[ai], am["lods"][0]["surfs"], 0), (gun_skin_mesh, gsurfs, na)):
                vf = len(csk["vbone"])
                for s in surfs:
                    for wl in s["weights"]:
                        wl = sorted(wl, key=lambda x: -x[1])[:4]
                        tot = sum(x[1] for x in wl) or 1.0
                        bs = [off + b for b, _ in wl] + [0] * (4 - len(wl))
                        ws = [x[1] / tot for x in wl] + [0.0] * (4 - len(wl))
                        csk["vbone"].append(bs[0] | bs[1] << 8 | bs[2] << 16 | bs[3] << 24)
                        csk["vweight"].extend(ws)
                csk["skins"].append({"mesh": mesh, "rig": len(csk["rigs"]), "vertFirst": vf,
                                     "vertCount": len(csk["vbone"]) - vf})
            csk["rigs"].append(r)
            if ai == 0:
                meshV, muzzleV, idle = self.view_mesh(rig, am, gun, gsurfs, wid, anims, slot_anim, clip_notes)
                meshP, muzzleP = self.held_mesh(rig, na, gun, gsurfs, wid, idle, wn)
        # weapon record
        reload_an, raise_an, fire_an = (slot_anim.get(SLOT_OF[k]) for k in ("reload", "raise", "fire"))
        reload_sounds = [self.first_sound(r) for _n, _s, r in clip_notes.get(reload_an, []) if r >= 0]
        reload_sounds = [s for s in reload_sounds if s >= 0][:3]
        rech = [self.first_sound(r) for _n, _s, r in clip_notes.get(slot_anim.get(SLOT_OF["rechamber"]), []) if r >= 0]
        fs = lambda k: self.first_sound(sounds_by_field.get(k, -1))     # noqa: E731
        kinds = [fs("fireSoundPlayer"), fs("fireLastSoundPlayer"), fs("emptyFireSoundPlayer"), fs("raiseSoundPlayer")]
        kinds += (reload_sounds + [-1, -1, -1])[:3]
        kinds += [next((s for s in rech if s >= 0), -1), fs("meleeSwipeSoundPlayer"), fs("meleeHitSound")]
        ev0 = len(events)
        for code, an in ((0, reload_an), (1, raise_an), (2, fire_an)):
            for _n, sec, r in clip_notes.get(an, []):
                snd = self.first_sound(r)
                if snd >= 0:
                    events.append((code, int(round(sec * 1000)), snd, int(round(sec * FPS))))
        sec_of = lambda an: anims[an]["duration"] if an in anims else 0.0      # noqa: E731
        log(f"{wn}: P {len(self.meshes[meshP]['texkeys']) if meshP >= 0 else 0} v, V {len(self.meshes[meshV]['texkeys'])} v "
            f"x {len(self.meshes[meshV]['poses'])} poses, {len(anims)} clips x {len(arms)} rigs, hidden surfaces {sorted(hid_g)}")
        return {"id": wid, "wdfRow": wdf_row, "meshP": meshP, "meshW": meshW, "meshV": meshV, "sounds": kinds, "ev0": ev0,
                "nev": len(events) - ev0, "name": wn, "flags": (1 if "tag_flash" in gbn else 0) | 2,
                "animClass": -1, "muzzleP": muzzleP, "muzzleV": muzzleV, "reload_s": sec_of(reload_an),
                "draw_s": sec_of(raise_an), "grip": (0.0, 0.0, 0.0, 1.0)}

    def view_mesh(self, rig, am, gun, gsurfs, wid, anims, slot_anim, clip_notes):
        """GSM1 kind-2 V mesh: arms + gun, 4 poses (idle, fire frame 1, reload first / last sound note)."""
        bind = waw_pose.bind_world(rig)
        na = len(am["bones"])
        parts = [(s, 0) for s in am["lods"][0]["surfs"]] + [(s, na) for s in gsurfs]

        def at(an, sec):
            if an is None or an not in anims:
                return waw_pose.pose(rig, None)
            a = anims[an]
            f = min(a["numframes"], sec * a["framerate"]) if a["framerate"] else 0
            return waw_pose.pose(rig, waw_xanim.sample(a, f))

        rel = slot_anim.get(SLOT_OF["reload"])
        snd = [sec for _n, sec, r in clip_notes.get(rel, []) if r >= 0]
        rdur = anims[rel]["duration"] if rel in anims else 0.0
        poses = [at(slot_anim.get(SLOT_OF["idle"]), 0.0), at(slot_anim.get(SLOT_OF["fire"]), 1.0 / FPS),
                 at(rel, snd[0] if snd else rdur * 0.35), at(rel, snd[-1] if snd else rdur * 0.7)]
        mi = -1
        for k, world in enumerate(poses):
            mats = skin_mats(world, bind)
            surfs = [(s,) + skin_surface(s, mats, off) for s, off in parts]
            if k == 0:
                mi = self.add_mesh(K_V, wid, surfs)
            else:
                self.add_pose(mi, surfs)
        fl = rig["byname"].get("tag_flash")
        muzzle = u_pos(poses[0][fl][1]) if fl is not None else (0.0, 0.0, 0.0)
        return mi, muzzle, poses[0]

    def held_mesh(self, rig, na, gun, gsurfs, wid, idle, wn, park=8.0):
        """GSM1 kind-0 P mesh (VR held gun): gun only, posed like the idle frame, in the j_gun frame (origin j_gun, barrel
        +Z). Parts the idle pose parks away from the gun (the Kar98k stripper clip waits 37 in to the left, outside the view)
        are left out: a surface whose centre is more than `park` inches outside the box of the j_gun-only surfaces."""
        bind = waw_pose.bind_world(rig)
        jg = rig["byname"]["j_gun"] if "j_gun" in rig["byname"] else na
        iq = X.qconj(X.qnorm(idle[jg][0]))
        it = idle[jg][1]
        rel = []
        for q, t in idle:
            d = (t[0] - it[0], t[1] - it[1], t[2] - it[2])
            rel.append((X.qnorm(X.qmul(iq, q)), X.qrot(iq, d)))
        mats = skin_mats(rel, bind)
        posed = [(s,) + skin_surface(s, mats, na) for s in gsurfs]
        core = [p for s, P, _N in posed if {b for wl in s["weights"] for b, _ in wl} <= {0} for p in P]
        lo = [min(p[k] for p in core) for k in range(3)] if core else None
        hi = [max(p[k] for p in core) for k in range(3)] if core else None
        keep, dropped = [], []
        for s, P, N in posed:
            c = [sum(p[k] for p in P) / len(P) for k in range(3)]
            out = max(max(lo[k] - c[k], c[k] - hi[k], 0.0) for k in range(3)) if core else 0.0
            (dropped if out > park else keep).append((s, P, N) if out <= park else s["images"].get("TS_COLOR_MAP"))
        if dropped:
            log(f"{wn}: P mesh leaves out {len(dropped)} parked surface(s) ({', '.join(sorted(set(map(str, dropped))))})")
        fl = rig["byname"].get("tag_flash")
        muzzle = u_pos(rel[fl][1]) if fl is not None else (0.0, 0.0, 0.0)
        return self.add_mesh(K_P, wid, keep), muzzle


# ───────────────────────────── CLI ─────────────────────────────
def summarize(pk):
    csk, sal, wdf, hud = (chunk(pk, t) for t in ("CSK1", "SAL1", "WDF1", "CHUD"))
    out = {"weapons": [{k: w[k] for k in ("id", "name", "meshP", "meshW", "meshV", "sounds", "nev", "reload_s", "draw_s")}
                       for w in pk["weapons"]],
           "meshes": [{"kind": m["kind"], "wid": m["wid"], "verts": len(m["uv"]) // 2, "tris": len(m["idx"]) // 3,
                       "poses": len(m["poses"])} for m in pk["meshes"]],
           "sounds": len(pk["sounds"]), "samples": sum(len(s["samples"]) for s in pk["sounds"]),
           "events": len(pk["events"]), "atlas": [pk["atlas"]["w"], pk["atlas"]["h"], pk["atlas"]["mips"]],
           "chunks": [t for t, _ in pk["chunks"]]}
    if csk:
        out["rigs"] = [{"weapon": r["weapon"], "arms": r["armsName"], "bones": r["boneCount"],
                        "clips": {csk["clips"][c]["name"]: csk["clips"][c]["frames"]
                                  for c in range(r["clipFirst"], r["clipFirst"] + r["clipCount"])}}
                       for r in csk["rigs"]]
    if sal:
        out["aliases"] = len(sal["aliases"])
        out["variants"] = len(sal["variants"])
    if wdf:
        out["wdf1"] = [len(wdf["rows"]), len(wdf["cols"])]
    if hud:
        out["hud"] = [(im["name"], im["w"], im["h"]) for im in hud["images"]]
    cdum = chunk(pk, "CDUM")
    if cdum:
        out["dummies"] = [{"body": d["body"], "head": d["head"], "team": d["team"], "boxes": d["boxCount"],
                           "height": round(d["maxs"][1] - d["mins"][1], 3), "eye": round(d["eye"], 3)} for d in cdum["dummies"]]
    return out


def check(path):
    with open(path, "rb") as fh:
        data = fh.read()
    pk = decode(data)
    csk = chunk(pk, "CSK1")
    if csk is None or chunk(pk, "WDF1") is None or chunk(pk, "SAL1") is None:
        raise ValueError("CoD chunks CSK1 / WDF1 / SAL1 missing")
    M = len(pk["meshes"])
    for k in csk["skins"]:
        if not 0 <= k["mesh"] < M or len(pk["meshes"][k["mesh"]]["uv"]) // 2 != k["vertCount"]:
            raise ValueError("CSK1 skin row does not match its mesh")
        if k["vertFirst"] + k["vertCount"] > len(csk["vbone"]):
            raise ValueError("CSK1 skin weights out of range")
        nb = csk["rigs"][k["rig"]]["boneCount"]
        for v in csk["vbone"][k["vertFirst"]:k["vertFirst"] + k["vertCount"]]:
            if max(v & 255, v >> 8 & 255, v >> 16 & 255, v >> 24 & 255) >= nb:
                raise ValueError("CSK1 bone index out of range")
    T = len(csk["tex"]) // 4
    for c in csk["clips"]:
        nb = csk["rigs"][c["rig"]]["boneCount"]
        if c["texelFirst"] + c["frames"] * 2 * nb > T or c["noteFirst"] + c["noteCount"] > len(csk["notes"]):
            raise ValueError(f"CSK1 clip {c['name']} out of range")
    if encode(pk) != data:
        raise ValueError("re-encode differs from the file (round trip)")
    return pk


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if argv[:1] == ["--version"]:
        print(f"{VERSION}-r{PACK_REV}")
        return 0
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    b = sp.add_parser("build")
    b.add_argument("--game-root", required=True)
    b.add_argument("--lang", default="auto")
    b.add_argument("--zone-dir", help="read the .ff files from here instead of <game-root>/zone/<lang>")
    b.add_argument("--weapons", default=",".join(DEFAULT_WEAPONS))
    b.add_argument("--arms", default=",".join(DEFAULT_ARMS))
    b.add_argument("--max-px", type=int, default=512)
    b.add_argument("--max-clip-s", type=float, default=6.0)
    b.add_argument("--impacts", default="small,large", help="bullet impact alias sets: small,large or none")
    b.add_argument("--dummies", default="default", help="target dummies: default (rus guard + ger honour guard rifle soldiers) or none")
    b.add_argument("--stats", help="write the build statistics as JSON here")
    b.add_argument("out")
    c = sp.add_parser("check")
    c.add_argument("pack")
    i = sp.add_parser("info")
    i.add_argument("pack")
    a = ap.parse_args(argv)
    if a.cmd == "build":
        imp = () if a.impacts in ("", "none") else tuple(x for x in a.impacts.split(",") if x)
        bd = Builder(a.game_root, a.lang, a.zone_dir, [x for x in a.weapons.split(",") if x],
                     [x for x in a.arms.split(",") if x], a.max_px, a.max_clip_s, imp,
                     () if a.dummies == "none" else DEFAULT_DUMMIES)
        st = bd.run(a.out)
        if a.stats:
            with open(a.stats, "w") as fh:
                json.dump(st, fh, indent=1)
        return 0
    if a.cmd == "check":
        try:
            pk = check(a.pack)
        except (ValueError, OSError) as ex:
            print(f"FAIL {a.pack}: {ex}")
            return 1
        print(json.dumps(summarize(pk), indent=1))
        print(f"OK {a.pack}: {os.path.getsize(a.pack)} bytes, round trip exact")
        return 0
    with open(a.pack, "rb") as fh:
        print(json.dumps(summarize(decode(fh.read())), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
