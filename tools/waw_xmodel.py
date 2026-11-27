#!/usr/bin/env python3
"""World at War XModel extraction from the sequential zone walker (tools/waw_zone.py). Python 3 stdlib only.

BYOG: reads the player's own fastfiles; output goes to scratch, never into the repo.

model_from_zone(walker, name) -> neutral dict:
  bones:  [{name, parent (-1 = root), local_q (x,y,z,w), local_t, base_q, base_t}]
          local_* = parent relative bind pose (root bones: identity), base_* = model space bind pose (baseMat)
  lods:   [{dist, surfs: [{material, images {semantic: image name}, pos, nrm, uv, col, tris, weights}]}]
          pos/nrm in model space (bind pose), uv (u, v) top-left origin, weights per vertex [(bone, w), ...]
  materials: {name: {images: {semantic: name}, techset}}
See docs/xmodel.md for the format notes.

Usage:
  waw_xmodel.py info <file.ff> <xmodel>               bones, LODs, surfaces, materials, bind-pose check
  waw_xmodel.py obj  <file.ff> <xmodel> <out.obj> [--lod 0]
"""
import argparse
import math
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import waw_zone  # noqa: E402
import t4_schema as S  # noqa: E402

SKEL_MAT = 64            # sizeof(DObjSkelMat): blend / vertList bone values are byte offsets into a skel mat array
SEMANTICS = {v: k for k, v in S.ENUMS.items() if k.startswith("TS_")}


# ---- quaternion helpers (x, y, z, w) -------------------------------------------------------------------------------
def qmul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz)


def qnorm(q):
    n = math.sqrt(sum(c * c for c in q)) or 1.0
    return tuple(c / n for c in q)


def qrot(q, v):
    x, y, z, w = q
    vx, vy, vz = v
    # t = 2 * cross(q.xyz, v); v' = v + w * t + cross(q.xyz, t)
    tx, ty, tz = 2 * (y * vz - z * vy), 2 * (z * vx - x * vz), 2 * (x * vy - y * vx)
    return (vx + w * tx + (y * tz - z * ty), vy + w * ty + (z * tx - x * tz), vz + w * tz + (x * ty - y * tx))


def qconj(q):
    return (-q[0], -q[1], -q[2], q[3])


def compose(parent, local):
    """(q, t) of parent * local"""
    pq, pt = parent
    lq, lt = local
    r = qrot(pq, lt)
    return qnorm(qmul(pq, lq)), (pt[0] + r[0], pt[1] + r[1], pt[2] + r[2])


def half(h):
    return struct.unpack("<e", struct.pack("<H", h))[0]


def unit_vec(p):
    s = ((p >> 24 & 0xFF) + 192.0) / 32385.0
    return (((p & 0xFF) - 127.0) * s, ((p >> 8 & 0xFF) - 127.0) * s, ((p >> 16 & 0xFF) - 127.0) * s)


# ---- material ------------------------------------------------------------------------------------------------------
def material_info(mat):
    if not isinstance(mat, dict):
        return {"name": None, "images": {}, "techset": None}
    name = mat.get("info", {}).get("name")
    images = {}
    tt = mat.get("textureTable")
    if isinstance(tt, list):
        for td in tt:
            img = (td.get("u") or {}).get("image") if isinstance(td.get("u"), dict) else td.get("image")
            if img is None:
                img = td.get("image")
            nm = img.get("name") if isinstance(img, dict) else None
            sem = SEMANTICS.get(td.get("semantic"), f"semantic{td.get('semantic')}")
            if nm:
                images.setdefault(sem, nm.lstrip(","))
    ts = mat.get("techniqueSet")
    return {"name": name.lstrip(",") if isinstance(name, str) else name, "images": images,
            "techset": ts.get("name") if isinstance(ts, dict) else None}


# ---- model ---------------------------------------------------------------------------------------------------------
def _vals(x):
    if isinstance(x, waw_zone.Blob):
        return x.values()
    if isinstance(x, list):
        return x
    if isinstance(x, dict):
        return [x]
    return []


def bones_from_model(w, m):
    nb, nr = m["numBones"], m["numRootBones"]
    ids = _vals(m["boneNames"])
    names = [w.script_strings[i] if i < len(w.script_strings) else f"bone{i}" for i in ids]
    plist = _vals(m["parentList"]) if m.get("parentList") is not None else []
    quats = _vals(m["quats"]) if m.get("quats") is not None else []
    trans = _vals(m["trans"]) if m.get("trans") is not None else []
    base = _vals(m["baseMat"]) if m.get("baseMat") is not None else []
    stride = 3 if TRANS_STRIDE == 3 else 4
    bones = []
    for b in range(nb):
        if b < nr:
            parent, lq, lt = -1, (0.0, 0.0, 0.0, 1.0), (0.0, 0.0, 0.0)
        else:
            k = b - nr
            parent = b - plist[k]
            q = quats[k]["v"]
            lq = qnorm(tuple(c / 32767.0 for c in q))
            lt = tuple(trans[stride * k:stride * k + 3])
        if b < len(base):
            bm = base[b]
            bq = (bm["quat"]["x"], bm["quat"]["y"], bm["quat"]["z"], bm["quat"]["w"])
            bt = (bm["trans"]["x"], bm["trans"]["y"], bm["trans"]["z"])
        else:
            bq, bt = (0.0, 0.0, 0.0, 1.0), (0.0, 0.0, 0.0)
        # model_t = the stored parent-relative translation: xanim translations are ADDED to it (see docs/xanim.md)
        bones.append({"name": names[b], "parent": parent, "local_q": lq, "local_t": lt, "base_q": bq, "base_t": bt,
                      "model_t": lt})
    ep, eq = bind_pose_error(bones)
    if ep > 0.01 or eq > 1e-4:
        # viewmodel arms (70 bones) ship all-zero local trans; baseMat (model space) is authoritative -> derive locals
        for bn in bones:
            if bn["parent"] >= 0:
                p = bones[bn["parent"]]
                pq = qnorm(p["base_q"])
                ic = qconj(pq)
                d = tuple(bn["base_t"][i] - p["base_t"][i] for i in range(3))
                bn["local_t"] = qrot(ic, d)
                bn["local_q"] = qnorm(qmul(ic, qnorm(bn["base_q"])))
                bn["local_from_base"] = True
    return bones


TRANS_STRIDE = 3   # WaW stores (numBones - numRootBones) * 4 floats but uses 3 per bone (+ n unused); see docs


def bind_pose_error(bones):
    """max position / rotation error between composing local bind transforms and the stored baseMat"""
    world = []
    ep, eq = 0.0, 0.0
    for b in bones:
        if b["parent"] < 0:
            g = (b["local_q"], b["local_t"])
        else:
            g = compose(world[b["parent"]], (b["local_q"], b["local_t"]))
        world.append(g)
        bq = qnorm(b["base_q"])
        ep = max(ep, max(abs(g[1][i] - b["base_t"][i]) for i in range(3)))
        dot = abs(sum(g[0][i] * bq[i] for i in range(4)))
        eq = max(eq, 1.0 - min(1.0, dot))
    return ep, eq


def surface(w, s, bones):
    verts = s["verts0"].values() if isinstance(s.get("verts0"), waw_zone.Blob) else []
    pos, nrm, uv, col = [], [], [], []
    for v in verts:
        pos.append((v["xyz"]["x"], v["xyz"]["y"], v["xyz"]["z"]))
        nrm.append(unit_vec(v["normal"]["packed"]))
        t = v["texCoord"]["packed"]
        uv.append((half(t >> 16), half(t & 0xFFFF)))
        c = v["color"]["packed"]
        col.append((c & 0xFF, c >> 8 & 0xFF, c >> 16 & 0xFF, c >> 24 & 0xFF))
    tri = s.get("triIndices")
    tris = []
    if isinstance(tri, waw_zone.Blob):
        raw = struct.unpack_from(f"<{3 * tri.count}H", tri.u, tri.pos)
        tris = [raw[i:i + 3] for i in range(0, len(raw), 3)]
    weights = []
    vl = s.get("vertList")
    if isinstance(vl, list) and vl:
        for r in vl:
            weights += [[(r["boneOffset"] // SKEL_MAT, 1.0)]] * r["vertCount"]
    vi = s.get("vertInfo") or {}
    vb = vi.get("vertsBlend")
    if isinstance(vb, waw_zone.Blob):
        bl = vb.values()
        cnt = vi["vertCount"]
        o = 0
        for _ in range(cnt[0]):
            weights.append([(bl[o] // SKEL_MAT, 1.0)]); o += 1
        for _ in range(cnt[1]):
            w1 = bl[o + 2] / 65535.0
            weights.append([(bl[o] // SKEL_MAT, 1 - w1), (bl[o + 1] // SKEL_MAT, w1)]); o += 3
        for _ in range(cnt[2]):
            w1, w2 = bl[o + 2] / 65535.0, bl[o + 4] / 65535.0
            weights.append([(bl[o] // SKEL_MAT, 1 - w1 - w2), (bl[o + 1] // SKEL_MAT, w1), (bl[o + 3] // SKEL_MAT, w2)])
            o += 5
        for _ in range(cnt[3]):
            w1, w2, w3 = bl[o + 2] / 65535.0, bl[o + 4] / 65535.0, bl[o + 6] / 65535.0
            weights.append([(bl[o] // SKEL_MAT, 1 - w1 - w2 - w3), (bl[o + 1] // SKEL_MAT, w1),
                            (bl[o + 3] // SKEL_MAT, w2), (bl[o + 5] // SKEL_MAT, w3)])
            o += 7
    if len(weights) < len(pos):          # should not happen; keep the remainder on the root bone
        weights += [[(0, 1.0)]] * (len(pos) - len(weights))
    return {"pos": pos, "nrm": nrm, "uv": uv, "col": col, "tris": tris, "weights": weights[:len(pos)],
            "deformed": bool(s.get("deformed"))}


def model_from_zone(w, name):
    m = w.by_name.get(("XModel", name)) or w.by_name.get(("XModel", "," + name))
    if m is None:
        raise KeyError(f"xmodel {name} not in zone")
    bones = bones_from_model(w, m)
    mats = m.get("materialHandles") or []
    minfo = [material_info(x) for x in mats]
    lods = []
    surfs = m.get("surfs") or []
    for li in range(m["numLods"]):
        L = m["lodInfo"][li]
        out = []
        for si in range(L["surfIndex"], L["surfIndex"] + L["numsurfs"]):
            s = surface(w, surfs[si], bones)
            mi = minfo[si] if si < len(minfo) else {"name": None, "images": {}}
            s["material"] = mi["name"]
            s["images"] = mi["images"]
            out.append(s)
        lods.append({"dist": L["dist"], "surfs": out})
    materials = {mi["name"]: mi for mi in minfo if mi["name"]}
    return {"name": m["name"].lstrip(","), "bones": bones, "lods": lods, "materials": materials,
            "radius": m["radius"], "mins": tuple(m["mins"][k] for k in "xyz"),
            "maxs": tuple(m["maxs"][k] for k in "xyz"), "numRootBones": m["numRootBones"]}


def write_obj(model, path, lod=0, positions=None):
    base = 1
    with open(path, "w") as f:
        f.write(f"# {model['name']} LOD{lod} (from player's own fastfile; do not distribute)\n")
        for i, s in enumerate(model["lods"][lod]["surfs"]):
            P = positions[i] if positions else s["pos"]
            f.write(f"o surf{i}_{s['material']}\n")
            for p in P:
                f.write("v %.5f %.5f %.5f\n" % p)
            for u in s["uv"]:
                f.write("vt %.5f %.5f\n" % (u[0], 1.0 - u[1]))
            for t in s["tris"]:
                a, b, c = t[0] + base, t[1] + base, t[2] + base
                f.write(f"f {a}/{a} {c}/{c} {b}/{b}\n")
            base += len(P)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("info"); p.add_argument("file"); p.add_argument("name")
    p = sp.add_parser("obj"); p.add_argument("file"); p.add_argument("name"); p.add_argument("out")
    p.add_argument("--lod", type=int, default=0)
    a = ap.parse_args()
    w = waw_zone.zone(a.file)
    m = model_from_zone(w, a.name)
    if a.cmd == "info":
        print(f"{m['name']}: {len(m['bones'])} bones, {len(m['lods'])} LODs, radius {m['radius']:.2f}")
        for i, b in enumerate(m["bones"]):
            print(f"  {i:3d} {b['name']:24s} parent {b['parent']:3d}  local_t {tuple(round(x, 3) for x in b['local_t'])}")
        ep, eq = bind_pose_error(m["bones"])
        print(f"  bind pose check (local chain vs baseMat): max pos err {ep:.5f}, max rot err {eq:.6f}")
        for li, L in enumerate(m["lods"]):
            nv = sum(len(s["pos"]) for s in L["surfs"])
            nt = sum(len(s["tris"]) for s in L["surfs"])
            print(f"  lod{li} dist {L['dist']:.0f}: {len(L['surfs'])} surfs, {nv} verts, {nt} tris")
        for s in m["lods"][0]["surfs"]:
            bset = sorted({b for wl in s["weights"] for b, _ in wl})
            print(f"    {s['material']}: {len(s['pos'])} v {len(s['tris'])} t deformed={s['deformed']} bones "
                  f"{[m['bones'][b]['name'] for b in bset][:6]} images {s['images']}")
    else:
        write_obj(m, a.out, a.lod)
        print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
