#!/usr/bin/env python3
"""Viewmodel rig + xanim pose + skinning + software render for World at War. Python 3 stdlib only (pypy3 is ~10x
faster). BYOG: reads the player's own fastfiles / iwds; PNGs go to scratch, never into the repo.

A first-person viewmodel in WaW is one skeleton made of the arms model (viewmodel_*_arms, 70 bones, root tag_view)
plus the gun model (viewmodel_mp_*, root j_gun) attached under the arms' tag_weapon. Weapon anims
(viewmodel_<gun>_<action>) carry tracks for both (j_gun, j_bolt, j_clip ... and j_shoulder_le ...).

  rig = build_rig([(arms_model, None), (gun_model, "tag_weapon")])
  world = pose(rig, waw_xanim.sample(anim, frame))      # model-space (q, t) per rig bone
  verts = skin(rig, world)                               # per surface skinned positions

Usage:
  waw_pose.py render --ff common_mp.ff --arms-ff localized_mp_dome.ff --arms viewmodel_rus_guard_arms
                     --gun viewmodel_mp_thompson --anim viewmodel_thompson_reload --frames 0,20,40
                     --out shot.png [--view fp|side|top] [--size 420] [--textures]
"""
import argparse
import json
import math
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import waw_zone  # noqa: E402
import waw_xmodel as X  # noqa: E402
import waw_xanim  # noqa: E402
import waw_iwd  # noqa: E402

SCRATCH = os.environ.get("CODWAW_SCRATCH", "/tmp/codwaw_scratch")
GAME_MAIN = os.environ.get("CODWAW_MAIN", os.path.join(os.path.expanduser("~"), "MacBackup", "GameInstalls", "CoDWaW", "main"))


# ---- rig -----------------------------------------------------------------------------------------------------------
def build_rig(parts):
    """parts: [(model dict, attach bone name or None)] -> {bones: [...], models: [(model, bone offset)]}"""
    bones, models = [], []
    byname = {}
    for model, attach in parts:
        off = len(bones)
        for i, b in enumerate(model["bones"]):
            p = b["parent"]
            if p >= 0:
                parent = off + p
            elif attach is not None and attach in byname:
                parent = byname[attach]
            else:
                parent = -1
            nb = dict(b)
            nb["parent"] = parent
            nb["model"] = model["name"]
            # a root bone attached to a parent: bind local = identity relative to the tag (matches the game, which
            # places the attached model's root exactly on the tag)
            if p < 0 and parent >= 0:
                nb["local_q"], nb["local_t"], nb["model_t"] = (0.0, 0.0, 0.0, 1.0), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
            bones.append(nb)
            byname.setdefault(b["name"], off + i)
        models.append((model, off))
    return {"bones": bones, "models": models, "byname": byname}


def pose(rig, local=None):
    """model-space (q, t) per bone; local = {bone name: (q, t)} from an anim sample (others keep bind locals)"""
    out = []
    for b in rig["bones"]:
        lt = local.get(b["name"]) if local else None
        if lt:
            # anim rotation replaces the bind rotation; anim translation is added to the model's stored translation
            mt = b.get("model_t", (0.0, 0.0, 0.0))
            lq, ltr = lt[0], (mt[0] + lt[1][0], mt[1] + lt[1][1], mt[2] + lt[1][2])
        else:
            lq, ltr = b["local_q"], b["local_t"]
        if b["parent"] < 0:
            out.append((X.qnorm(lq), ltr))
        else:
            out.append(X.compose(out[b["parent"]], (lq, ltr)))
    return out


def bind_world(rig):
    """model-space bind matrices the vertices were authored in: each model's own baseMat"""
    return [(X.qnorm(b["base_q"]), b["base_t"]) for b in rig["bones"]]


def skin(rig, world, lod=0, hidden=None):
    """skinned positions per (model, surface): vertex' = sum w * world_b * inverse(bind_b) * vertex"""
    bind = bind_world(rig)
    skinmats = []
    for (wq, wt), (bq, bt) in zip(world, bind):
        iq = X.qconj(bq)
        q = X.qnorm(X.qmul(wq, iq))
        r = X.qrot(q, bt)
        skinmats.append((q, (wt[0] - r[0], wt[1] - r[1], wt[2] - r[2])))
    out = []
    for model, off in rig["models"]:
        for si, s in enumerate(model["lods"][lod]["surfs"]):
            if hidden and (model["name"], si) in hidden:
                continue
            P = []
            for p, wl in zip(s["pos"], s["weights"]):
                x = y = z = 0.0
                for bi, wgt in wl:
                    q, t = skinmats[off + bi]
                    v = X.qrot(q, p)
                    x += wgt * (v[0] + t[0]); y += wgt * (v[1] + t[1]); z += wgt * (v[2] + t[2])
                P.append((x, y, z))
            out.append((model, s, P))
    return out


# ---- textures ------------------------------------------------------------------------------------------------------
_iwd_index = None


def iwi_bytes(name, main=GAME_MAIN, cache_dir=os.path.join(SCRATCH, "iwd")):
    """images/<name>.iwi from the player's iwds; copied once into scratch"""
    global _iwd_index
    fn = os.path.join(cache_dir, name + ".iwi")
    if os.path.exists(fn):
        return open(fn, "rb").read()
    if _iwd_index is None:
        ip = os.path.join(SCRATCH, "out", "iwd_image_index.json")
        if os.path.exists(ip):
            _iwd_index = json.load(open(ip))
        else:
            _iwd_index = {}
            for f in waw_iwd.iwds(main):
                for i in zipfile.ZipFile(f).infolist():
                    if i.filename.lower().endswith(".iwi"):
                        _iwd_index.setdefault(os.path.basename(i.filename)[:-4].lower(), [os.path.basename(f), i.filename])
            os.makedirs(os.path.dirname(ip), exist_ok=True)
            json.dump(_iwd_index, open(ip, "w"))
    hit = _iwd_index.get(name.lower())
    if not hit:
        return None
    b = zipfile.ZipFile(os.path.join(main, hit[0])).read(hit[1])
    os.makedirs(cache_dir, exist_ok=True)
    open(fn, "wb").write(b)
    return b


def load_texture(name, maxsize=256):
    b = iwi_bytes(name) if name else None
    if not b:
        return None
    try:
        _, w, h, rgba = waw_iwd.decode_iwi(b)
    except Exception:
        return None
    step = max(1, max(w, h) // maxsize)
    if step > 1:
        nw, nh = w // step, h // step
        small = bytearray(nw * nh * 4)
        for y in range(nh):
            for x in range(nw):
                s = 4 * ((y * step) * w + x * step)
                small[4 * (y * nw + x):4 * (y * nw + x) + 4] = rgba[s:s + 4]
        return nw, nh, small
    return w, h, rgba


# ---- render --------------------------------------------------------------------------------------------------------
def project(view, p, fov=1.0):
    x, y, z = p
    if view == "side":       # look along +Y: screen right = +X, up = +Z
        return x, z, y
    if view == "quarter":    # from front-left-above: yaw 140 deg, pitch 25 deg
        cy, sy, cp, sp = math.cos(2.44), math.sin(2.44), math.cos(0.44), math.sin(0.44)
        x, y = x * cy - y * sy, x * sy + y * cy
        y, z = y * cp - z * sp, y * sp + z * cp
        return x, z, y
    if view == "top":        # look down -Z: right = +X, up = +Y
        return x, y, -z
    if view == "front":      # look along -X from in front: right = -Y... (we look back at the gun)
        return y, z, -x
    # first person: camera at the origin looking +X, right = -Y, up = +Z, perspective
    d = max(x, 0.5)
    return -y / d / fov, z / d / fov, d


def raster(tris, W, H, view, frame_box, textures, light=(0.3, -0.5, 0.8)):
    """tris: [(P0, P1, P2, uv0, uv1, uv2, tex key, base colour)] -> RGBA bytearray"""
    img = bytearray(b"\x20\x22\x28\xff" * (W * H))
    zb = [1e30] * (W * H)
    (x0b, x1b, y0b, y1b) = frame_box
    sc = 0.92 * min(W / (x1b - x0b), H / (y1b - y0b))
    cx, cy = (x0b + x1b) / 2, (y0b + y1b) / 2
    ln = math.sqrt(sum(c * c for c in light))
    light = tuple(c / ln for c in light)
    for A, B, C, ua, ub, uc, tk, col in tris:
        e1 = (B[0] - A[0], B[1] - A[1], B[2] - A[2])
        e2 = (C[0] - A[0], C[1] - A[1], C[2] - A[2])
        n = (e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0])
        nl = math.sqrt(n[0] * n[0] + n[1] * n[1] + n[2] * n[2]) or 1.0
        shade = 0.55 + 0.6 * abs((n[0] * light[0] + n[1] * light[1] + n[2] * light[2]) / nl)
        P = []
        for p in (A, B, C):
            sx, sy, d = project(view, p)
            P.append(((sx - cx) * sc + W / 2, H / 2 - (sy - cy) * sc, d))
        (px0, py0, z0), (px1, py1, z1), (px2, py2, z2) = P
        den = (py1 - py2) * (px0 - px2) + (px2 - px1) * (py0 - py2)
        if abs(den) < 1e-9:
            continue
        minx, maxx = max(0, int(min(px0, px1, px2))), min(W - 1, int(max(px0, px1, px2)) + 1)
        miny, maxy = max(0, int(min(py0, py1, py2))), min(H - 1, int(max(py0, py1, py2)) + 1)
        tex = textures.get(tk)
        for py in range(miny, maxy + 1):
            fy = py + 0.5
            for px in range(minx, maxx + 1):
                fx = px + 0.5
                w0 = ((py1 - py2) * (fx - px2) + (px2 - px1) * (fy - py2)) / den
                w1 = ((py2 - py0) * (fx - px2) + (px0 - px2) * (fy - py2)) / den
                w2 = 1 - w0 - w1
                if w0 < 0 or w1 < 0 or w2 < 0:
                    continue
                d = w0 * z0 + w1 * z1 + w2 * z2
                k = py * W + px
                if d >= zb[k]:
                    continue
                zb[k] = d
                if tex:
                    tw, th, tr = tex
                    u = w0 * ua[0] + w1 * ub[0] + w2 * uc[0]
                    v = w0 * ua[1] + w1 * ub[1] + w2 * uc[1]
                    q = 4 * ((int(v * th) % th) * tw + (int(u * tw) % tw))
                    r, g, b = tr[q], tr[q + 1], tr[q + 2]
                else:
                    r, g, b = col
                img[4 * k:4 * k + 4] = bytes((min(255, int(r * shade)), min(255, int(g * shade)),
                                              min(255, int(b * shade)), 255))
    return img


PALETTE = [(200, 170, 120), (120, 160, 210), (190, 110, 110), (130, 190, 130), (210, 200, 120), (170, 130, 200)]


def frame_tris(skinned):
    tris = []
    for si, (model, s, P) in enumerate(skinned):
        col = PALETTE[si % len(PALETTE)]
        tk = s["images"].get("TS_COLOR_MAP")
        uv = s["uv"]
        for t in s["tris"]:
            a, b, c = t
            tris.append((P[a], P[c], P[b], uv[a], uv[c], uv[b], tk, col))
    return tris


def bounds(view, tris, pad=0.04):
    xs, ys = [], []
    for t in tris:
        for p in t[:3]:
            sx, sy, _ = project(view, p)
            xs.append(sx); ys.append(sy)
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    dx, dy = (x1 - x0) * pad + 1e-6, (y1 - y0) * pad + 1e-6
    return x0 - dx, x1 + dx, y0 - dy, y1 + dy


def draw_label(img, W, x, y, text):
    """3x5 pixel font for frame numbers / short labels"""
    font = {"0": "75557", "1": "26227", "2": "71747", "3": "71717", "4": "55711", "5": "74717", "6": "74757",
            "7": "71111", "8": "75757", "9": "75717", "f": "74644", " ": "00000", "/": "11244", "s": "74717",
            "r": "06544", "e": "25742", "l": "44447", "o": "02552", "a": "02575", "d": "11575", "i": "20222",
            "m": "05775", "p": "06564", "t": "47442", "y": "55211", "-": "00700", ".": "00002", "_": "00007",
            "n": "06555", "c": "03443", "u": "05553", "h": "44755", "g": "03531", "k": "45654", "b": "44755",
            "w": "05577", "x": "05255", "v": "05552", "z": "07247", "j": "10116", "q": "03531"}
    for ci, ch in enumerate(text.lower()):
        rows = font.get(ch, "00000")
        for ry, rc in enumerate(rows):
            bits = int(rc, 16) if rc in "0123456789abcdef" else 0
            for rx in range(3):
                if bits & (4 >> rx):
                    for sy in range(2):
                        for sx in range(2):
                            px, py = x + ci * 8 + rx * 2 + sx, y + ry * 2 + sy
                            k = py * W + px
                            if 0 <= px < W and 0 <= k < len(img) // 4:
                                img[4 * k:4 * k + 4] = b"\xff\xff\x60\xff"


def render_sheet(rig, anims_frames, out, view="fp", size=420, textures=True, hidden=None):
    """anims_frames: [(anim dict or None, [frames])] -> one row per anim, one column per frame"""
    texs = {}
    if textures:
        for model, _ in rig["models"]:
            for s in model["lods"][0]["surfs"]:
                k = s["images"].get("TS_COLOR_MAP")
                if k and k not in texs:
                    texs[k] = load_texture(k)
    rows = []
    for an, frames in anims_frames:
        rowt = []
        for f in frames:
            world = pose(rig, waw_xanim.sample(an, f) if an else None)
            rowt.append((f, frame_tris(skin(rig, world, hidden=hidden))))
        rows.append((an, rowt))
    allt = [t for _, r in rows for _, ts in r for t in ts]
    box = bounds(view, allt)
    W = size
    H = int(size * (box[3] - box[2]) / (box[1] - box[0])) if view != "fp" else int(size * 0.75)
    H = max(120, min(H, 2 * size))
    if view == "fp":
        box = (-0.75, 0.75, -0.5625, 0.5625)   # fixed 1.5 x 1.125 tan-space window (about 74 degrees hfov)
    ncol = max(len(r) for _, r in rows)
    sheet = bytearray(W * ncol * H * len(rows) * 4)
    for ri, (an, r) in enumerate(rows):
        for ci, (f, ts) in enumerate(r):
            img = raster(ts, W, H, view, box, texs)
            lab = f"{(an['name'].replace('viewmodel_', '') if an else 'bind')} f{f}"
            draw_label(img, W, 4, 4, lab[:W // 8 - 1])
            for y in range(H):
                dst = 4 * ((ri * H + y) * W * ncol + ci * W)
                sheet[dst:dst + 4 * W] = img[4 * W * y:4 * W * (y + 1)]
    waw_iwd.write_png(out, W * ncol, H * len(rows), sheet)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("render")
    p.add_argument("--ff", required=True); p.add_argument("--arms-ff"); p.add_argument("--arms")
    p.add_argument("--gun"); p.add_argument("--anim", action="append", default=[])
    p.add_argument("--weapon", help="WeaponDef name: gun model + hideTags from the weapon (e.g. thompson_mp)")
    p.add_argument("--frames", default="0"); p.add_argument("--out", required=True)
    p.add_argument("--view", default="fp"); p.add_argument("--size", type=int, default=420)
    p.add_argument("--no-textures", action="store_true")
    a = ap.parse_args()
    w = waw_zone.zone(a.ff)
    parts = []
    if a.arms:
        wa = waw_zone.zone(a.arms_ff or a.ff)
        parts.append((X.model_from_zone(wa, a.arms), None))
    hide = set()
    if a.weapon:
        import waw_export
        info = waw_export.weapon_info(w, w.by_name[("WeaponDef", a.weapon)])
        a.gun = a.gun or info["viewModel"]
        hide = set(info["hideTags"])
    gun = X.model_from_zone(w, a.gun)
    parts.append((gun, "tag_weapon" if a.arms else None))
    rig = build_rig(parts)
    hidden = None
    if hide:
        import waw_export
        hidden = {(gun["name"], i) for i in waw_export.hidden_surfaces(gun, hide)}
    rows = []
    for an in a.anim or [None]:
        if an and an != "bind" and a.weapon and an in ("fire", "reload", "reload_empty", "idle", "rechamber",
                                                           "raise", "drop", "ads_up", "ads_fire", "lastshot"):
            an = info["anims"].get(an, an)
        anim = waw_xanim.decode(w, an) if an and an != "bind" else None
        fr = [float(x) if x not in ("end", "mid") else None for x in a.frames.split(",")]
        if anim:
            fr = [anim["numframes"] if x is None and s == "end" else (anim["numframes"] / 2 if x is None else x)
                  for x, s in zip(fr, a.frames.split(","))]
        rows.append((anim, [x if x is not None else 0 for x in fr]))
    render_sheet(rig, rows, a.out, a.view, a.size, not a.no_textures, hidden)
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
