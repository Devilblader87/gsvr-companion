#!/usr/bin/env python3
"""WaW MP map -> GSV3 map pack (the GoldSrc VR map pack format, read by GsvrPackLoader.cs unchanged).

The pack is a regular GSV3 file (layout: half-life tools/pack.py header + docs/goldsrc_base_layer.md; our
summary + the CoD additions: docs/map_pack.md). One world model, one mesh, one RGBA32 diffuse atlas with 4 mips,
one RGBA32 lightmap, the entity text, and these sections:

  MODL  1 record (the world; no brush entities yet), PLAN/HNOD empty (no GoldSrc hull: every headnode = -1 empty)
  ENTS / EKVS  keyvalue table over the entity text (all map ents, worldspawn first, + info_player_deathmatch aliases)
  CLIP  collision mesh: clipMap brushes (solid | playerclip) + collision triangles + static model collision LODs,
        welded, internal brush faces removed. Unity metres. The loader appends it to the map collider and chunks it.
  SKY6  6 RGBA faces rendered from the map's skybox xmodel (GoldSrc face order rt bk lf ft up dn)
  CSRF  (CoD extension, older loaders skip it) uint8 per CLIP triangle = WaW SURF_TYPE_* index (footsteps/impacts)
  CODI  (CoD extension) JSON: source map, converter version, stats, spawn class counts

Render geometry: the world surfaces (lit range, no sky / tool / decal surfaces) + every static model instance
baked into the world mesh (LOD chosen by --smodel-max-tris). S = 0: no render triangle collides; collision is CLIP.
Textures: layer-0 colour map (nameHash colorMap) of each material, <= --max-tex px, alpha kept for alpha-test (t) and
blend (b) techsets (cut out at 0.5 by the shader), opaque otherwise. Lightmap: baked from the map's own lightmap
set (secondary A/B average + primary sun mask x sun colour x N.L, rasterised in lightmap space), --lm-size px.

Stdlib only; run with pypy3 (about 1 min) or CPython. BYOG: the output goes to scratch, never into the repo.

Usage: waw_mappack.py <mp_x.ff> <out.bin> [--iwd <main dir>] [--max-tex 256] [--lm-size 1024] [--sky 256]
       [--smodel-max-tris 6500] [--json stats.json]
"""
import argparse
import array
import json
import math
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import waw_iwd  # noqa: E402
import waw_map as wm  # noqa: E402

VERSION = 1
INCH = 0.0254
GUTTER = 8
MIPS = 4
HEADER = 64
MAX_PACK = 100 * 1024 * 1024
COLORMAP = 0xA0AB1041          # MaterialTextureDef.nameHash of "colorMap" (layer 0); colorMap1/2 = 0xB60D185x
SKIP_TECHSETS = ("wc_sky", "wc_shadowcaster", "wc_default", "wc_unlit_distfalloff", "shadowcaster", "default")
SKIP_MATERIAL_PARTS = ("hdrportal", "$default", "shadowcaster", "caulk", "clip", "portal_nodraw", "nodraw")
SURF_TYPES = ("default", "bark", "brick", "carpet", "cloth", "concrete", "dirt", "flesh", "foliage", "glass", "grass",
              "gravel", "ice", "metal", "mud", "paper", "plaster", "rock", "sand", "snow", "water", "wood", "asphalt",
              "ceramic", "plastic", "rubber", "cushion", "fruit", "paintedmetal", "player", "tallgrass")


def to_unity(p):
    return (p[0] * INCH, p[2] * INCH, p[1] * INCH)


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _norm(a):
    ln = math.sqrt(_dot(a, a)) or 1.0
    return (a[0] / ln, a[1] / ln, a[2] / ln)


# ───────────────────────── materials / textures ─────────────────────────

def material_color(mi):
    """(image name, alpha mode 'opaque' | 'test' | 'blend') of the layer-0 colour map, or (None, None)."""
    if mi is None:
        return None, None
    cols = [t for t in mi["textures"] if t["semantic"] == 2 and t["image"]]
    if not cols:
        return None, None
    c0 = next((t for t in cols if t["nameHash"] == COLORMAP), cols[0])
    ts = (mi["techset"] or "").lstrip(",")
    mode = "opaque"
    for part in ts.split("_"):
        if len(part) >= 2 and part[0] in "rtb" and part[1] == "0":
            mode = {"r": "opaque", "t": "test", "b": "blend"}[part[0]]
            break
    if "foliage" in ts or "_t0" in ts:
        mode = "test"
    return c0["image"], mode


def drawable(mi):
    if mi is None or not mi.get("name"):
        return False
    n = mi["name"].lower()
    ts = (mi["techset"] or "").lstrip(",").lower()
    if ts in SKIP_TECHSETS or any(p in n for p in SKIP_MATERIAL_PARTS):
        return False
    return material_color(mi)[0] is not None


def iwi_level(b, cap):
    """Decode the largest stored mip of an iwi whose longer side is <= cap -> (w, h, rgba bytearray)."""
    hd = waw_iwd.iwi_header(b)
    fmt, w, h = hd["format"], hd["width"], hd["height"]
    if hd["flags"] & 0x04 or hd["depth"] > 1:
        raise ValueError("cube/volume image")
    sizes = []
    mw, mh = w, h
    while True:
        sizes.append((mw, mh))
        if mw == 1 and mh == 1:
            break
        mw, mh = max(1, mw // 2), max(1, mh // 2)
    end = len(b)
    for lw, lh in sizes:                      # mips are stored smallest first: walk down from the end of the file
        n = waw_iwd.mip_bytes(fmt, lw, lh)
        start = end - n
        if start < 28:
            raise ValueError("mip chain shorter than expected")
        if max(lw, lh) <= cap:
            return lw, lh, decode_pixels(b[start:end], lw, lh, fmt)
        end = start
    raise ValueError("no mip <= cap")


def decode_pixels(data, w, h, fmt):
    if fmt in (0x0B, 0x0C, 0x0D):
        if w < 4 or h < 4:
            full = waw_iwd.decode_dxt(data, max(4, w), max(4, h), fmt)
            bw = max(4, w)
            out = bytearray(w * h * 4)
            for y in range(h):
                out[4 * w * y:4 * w * (y + 1)] = full[4 * bw * y:4 * bw * y + 4 * w]
            return out
        return waw_iwd.decode_dxt(data, w, h, fmt)
    out = bytearray(w * h * 4)
    if fmt == 1:
        out[0::4], out[1::4], out[2::4], out[3::4] = data[2::4], data[1::4], data[0::4], data[3::4]
    elif fmt == 2:
        out[0::4], out[1::4], out[2::4] = data[2::3], data[1::3], data[0::3]
        out[3::4] = b"\xff" * (w * h)
    elif fmt == 3:
        out[0::4] = out[1::4] = out[2::4] = data[0::2]
        out[3::4] = data[1::2]
    elif fmt == 4:
        out[0::4] = out[1::4] = out[2::4] = b"\xff" * (w * h)
        out[3::4] = data
    else:
        raise ValueError(f"iwi format {fmt} not supported")
    return out


def resize_nearest(w, h, px, nw, nh):
    out = bytearray(nw * nh * 4)
    for y in range(nh):
        sy = y * h // nh
        for x in range(nw):
            sx = x * w // nw
            k = 4 * (sy * w + sx)
            out[4 * (y * nw + x):4 * (y * nw + x) + 4] = px[k:k + 4]
    return out


def load_textures(md, names, cap, log):
    """{image name: (w, h, rgba)}; w, h multiples of 8 (resampled when needed). Missing images -> grey checker."""
    out = {}
    missing = []
    for name, mode in sorted(names.items()):
        try:
            b = md.read_image(name)
            if b is None:
                raise ValueError("not in the iwds")
            w, h, px = iwi_level(b, cap)
        except Exception as e:                # noqa: BLE001 - a missing / odd image must not stop the pack
            missing.append((name, str(e)))
            w = h = 8
            px = bytearray(b"\x80\x80\x80\xff\x60\x60\x60\xff" * 32)
        nw, nh = max(8, (w + 7) // 8 * 8), max(8, (h + 7) // 8 * 8)
        if (nw, nh) != (w, h):
            px = resize_nearest(w, h, px, nw, nh)
            w, h = nw, nh
        if mode == "opaque":
            px[3::4] = b"\xff" * (w * h)
        else:
            px[3::4] = bytes(255 if a >= 128 else 0 for a in px[3::4])
        out[name] = (w, h, px)
    if missing:
        log(f"  textures missing/undecodable: {len(missing)}: " + ", ".join(n for n, _ in missing[:8]))
    return out, missing


def pack_atlas(texs):
    """Shelf packing with an 8 px wrapped gutter. -> (AW, AH, {name: (x, y, w, h)}) (x, y = texture corner)."""
    items = sorted(texs.items(), key=lambda kv: (-kv[1][1], -kv[1][0], kv[0]))
    area = sum((w + 2 * GUTTER) * (h + 2 * GUTTER) for _, (w, h, _) in items)
    best = None
    for AW in (1024, 2048, 4096, 8192):
        if AW * AW * 2 < area:
            continue
        pos = {}
        x = y = shelf = 0
        ok = True
        for name, (w, h, _) in items:
            cw, ch = w + 2 * GUTTER, h + 2 * GUTTER
            if cw > AW:
                ok = False
                break
            if x + cw > AW:
                x, y, shelf = 0, y + shelf, 0
            pos[name] = (x + GUTTER, y + GUTTER, w, h)
            x += cw
            shelf = max(shelf, ch)
        if not ok:
            continue
        AH = (y + shelf + 7) // 8 * 8
        if AH > 8192:
            continue
        cand = (AW * AH, max(AW, AH), AW, AH, pos)
        if best is None or cand[:2] < best[:2]:
            best = cand
    if best is None:
        raise RuntimeError("textures do not fit an 8192 atlas; lower --max-tex")
    return best[2], best[3], best[4]


def build_atlas(texs, AW, AH, pos):
    lvl0 = bytearray(AW * AH * 4)
    for name, (x0, y0, w, h) in pos.items():
        _, _, px = texs[name]
        row = 4 * w
        for yy in range(-GUTTER, h + GUTTER):
            sy = yy % h
            src = px[row * sy:row * (sy + 1)]
            # wrapped row: [last GUTTER px] + row + [first GUTTER px]
            line = src[row - 4 * GUTTER:] + src + src[:4 * GUTTER]
            o = 4 * ((y0 + yy) * AW + x0 - GUTTER)
            lvl0[o:o + len(line)] = line
    levels = [lvl0]
    w, h = AW, AH
    for _ in range(1, MIPS):
        src = levels[-1]
        nw, nh = w // 2, h // 2
        dst = bytearray(nw * nh * 4)
        for y in range(nh):
            r0 = 4 * w * (2 * y)
            r1 = r0 + 4 * w
            o = 4 * nw * y
            for x in range(nw):
                a = r0 + 8 * x
                b = r1 + 8 * x
                for c in range(4):
                    dst[o + c] = (src[a + c] + src[a + 4 + c] + src[b + c] + src[b + 4 + c] + 2) >> 2
                o += 4
        levels.append(dst)
        w, h = nw, nh
    return levels


# ───────────────────────── lightmap ─────────────────────────

def lightmap_planes(md):
    """(primary L8 bytes, pw, ph, secondary BGRA bytes, sw, sh) of lightmap set 0."""
    x = wm.items(md.g["lightmaps"])[0]
    out = []
    for k in ("primary", "secondary"):
        im = wm.deref(x[k])
        ld = wm.deref(im["texture"]["loadDef"])
        blob = ld["data"]
        out.append((bytes(md.u[blob.pos:blob.pos + blob.count]), im["width"], im["height"], ld["format"]))
    return out


def raster_lightmap_normals(tris, S):
    """Rasterise (uv0, uv1, uv2, n0, n1, n2) triangles in lightmap space -> per-texel normal (3 floats) + coverage."""
    nrm = array.array("f", bytes(4 * 3 * S * S))
    cov = bytearray(S * S)
    for (ua, ub, uc, na, nb, nc) in tris:
        x0, y0 = ua[0] * S - 0.5, ua[1] * S - 0.5
        x1, y1 = ub[0] * S - 0.5, ub[1] * S - 0.5
        x2, y2 = uc[0] * S - 0.5, uc[1] * S - 0.5
        den = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
        minx, maxx = max(0, int(math.floor(min(x0, x1, x2)))), min(S - 1, int(math.ceil(max(x0, x1, x2))))
        miny, maxy = max(0, int(math.floor(min(y0, y1, y2)))), min(S - 1, int(math.ceil(max(y0, y1, y2))))
        if abs(den) < 1e-9:
            # degenerate in lightmap space: mark the covered texels with the average normal
            n = _norm((na[0] + nb[0] + nc[0], na[1] + nb[1] + nc[1], na[2] + nb[2] + nc[2]))
            for py in range(miny, maxy + 1):
                for px in range(minx, maxx + 1):
                    k = py * S + px
                    if not cov[k]:
                        cov[k] = 1
                        nrm[3 * k], nrm[3 * k + 1], nrm[3 * k + 2] = n
            continue
        inv = 1.0 / den
        for py in range(miny, maxy + 1):
            for px in range(minx, maxx + 1):
                w0 = ((y1 - y2) * (px - x2) + (x2 - x1) * (py - y2)) * inv
                w1 = ((y2 - y0) * (px - x2) + (x0 - x2) * (py - y2)) * inv
                w2 = 1.0 - w0 - w1
                if w0 < -0.02 or w1 < -0.02 or w2 < -0.02:
                    continue
                k = py * S + px
                n = (w0 * na[0] + w1 * nb[0] + w2 * nc[0], w0 * na[1] + w1 * nb[1] + w2 * nc[1],
                     w0 * na[2] + w1 * nb[2] + w2 * nc[2])
                n = _norm(n)
                cov[k] = 2
                nrm[3 * k], nrm[3 * k + 1], nrm[3 * k + 2] = n
    return nrm, cov


def bake_lightmap(md, surfaces, S, sec_gain=1.6, sun_gain=0.7):
    """S x S RGBA lightmap (128 = 1.0 after the shader's x2): secondary (A + B) / 2 x sec_gain + primary sun mask x
    sun colour x max(0, N.L) x sun_gain, N rasterised in lightmap space from the vertex normals."""
    (pri, pw, ph, _), (sec, sw, sh, _) = lightmap_planes(md)
    half = sh // 2
    sun = md.g["sunLight"]
    scol = sun["color"]
    sdir = _norm(sun["dir"])
    tris = []
    V = md.verts
    for s in surfaces:
        for a, b, c in md.surface_tris(s):
            tris.append((V[a][2], V[b][2], V[c][2], V[a][4], V[b][4], V[c][4]))
    nrm, cov = raster_lightmap_normals(tris, S)
    out = bytearray(S * S * 4)
    for y in range(S):
        sy_a = y * half // S
        sy_b = half + sy_a
        py = y * ph // S
        for x in range(S):
            sx = x * sw // S
            ka = 4 * (sy_a * sw + sx)
            kb = 4 * (sy_b * sw + sx)
            # average the primary (higher res) over the texel footprint
            px0 = x * pw // S
            fx, fy = max(1, pw // S), max(1, ph // S)
            ps = 0
            for yy in range(py, py + fy):
                o = yy * pw + px0
                ps += sum(pri[o:o + fx])
            pv = ps / (255.0 * fx * fy)
            k = y * S + x
            ndl = 1.0
            if cov[k]:
                ndl = max(0.0, nrm[3 * k] * sdir[0] + nrm[3 * k + 1] * sdir[1] + nrm[3 * k + 2] * sdir[2])
            o = 4 * k
            for c, (bi) in enumerate((2, 1, 0)):          # BGRA -> RGB
                v = (sec[ka + bi] + sec[kb + bi]) * 0.5 / 255.0 * sec_gain + pv * scol[c] * ndl * sun_gain
                out[o + c] = min(255, int(v * 128.0 + 0.5))
            out[o + 3] = 255
    return out, cov


# ───────────────────────── static models ─────────────────────────

def xmodel_lod_geom(md, xm, lod):
    """[(material info, verts[(xyz, uv, n)], tris)] of one LOD of a loaded XModel."""
    li = xm["lodInfo"][lod]
    surfs = wm.items(xm["surfs"])
    mats = wm.items(xm["materialHandles"])
    out = []
    for si in range(li["surfIndex"], li["surfIndex"] + li["numsurfs"]):
        if si >= len(surfs):
            break
        s = surfs[si]
        vr, tr = wm.deref(s["verts0"]), wm.deref(s["triIndices"])
        if not isinstance(vr, wm.wz.Blob) or not isinstance(tr, wm.wz.Blob) or vr.pos is None or tr.pos is None:
            continue
        vs = []
        for r in struct.iter_unpack("<3ffI3I", md.u[vr.pos:vr.pos + 32 * vr.count]):
            vs.append(((r[0], r[1], r[2]), (wm.half(r[5] >> 16), wm.half(r[5] & 0xFFFF)), wm.unpack_unit_vec(r[6])))
        ts = list(struct.iter_unpack("<3H", md.u[tr.pos:tr.pos + 6 * tr.count]))
        mi = md.material_info(wm.deref(mats[si])) if si < len(mats) else None
        out.append((mi, vs, ts))
    return out


def lod_tris(md, xm, lod):
    li = xm["lodInfo"][lod]
    surfs = wm.items(xm["surfs"])
    return sum(surfs[i]["triCount"] for i in range(li["surfIndex"], li["surfIndex"] + li["numsurfs"]) if i < len(surfs))


def pick_lod(md, xm, max_tris):
    for lod in range(max(1, xm["numLods"])):
        if lod_tris(md, xm, lod) <= max_tris:
            return lod
    return max(0, xm["numLods"] - 1)


class FloorFinder:
    """Highest up-facing world render triangle below a point -> its interpolated lightmap coordinate."""
    CELL = 128.0

    def __init__(self, md, surfaces):
        self.md = md
        self.grid = {}
        V = md.verts
        for s in surfaces:
            for t in md.surface_tris(s):
                A, B, C = V[t[0]][0], V[t[1]][0], V[t[2]][0]
                n = _cross(_sub(B, A), _sub(C, A))
                ln = math.sqrt(_dot(n, n))
                if ln < 1e-9 or abs(n[2]) / ln < 0.5:
                    continue
                x0, x1 = min(A[0], B[0], C[0]), max(A[0], B[0], C[0])
                y0, y1 = min(A[1], B[1], C[1]), max(A[1], B[1], C[1])
                for gx in range(int(math.floor(x0 / self.CELL)), int(math.floor(x1 / self.CELL)) + 1):
                    for gy in range(int(math.floor(y0 / self.CELL)), int(math.floor(y1 / self.CELL)) + 1):
                        self.grid.setdefault((gx, gy), []).append(t)

    def lmap_below(self, p, up=24.0):
        V = self.md.verts
        best = None
        for t in self.grid.get((int(math.floor(p[0] / self.CELL)), int(math.floor(p[1] / self.CELL))), ()):
            A, B, C = V[t[0]][0], V[t[1]][0], V[t[2]][0]
            den = (B[1] - C[1]) * (A[0] - C[0]) + (C[0] - B[0]) * (A[1] - C[1])
            if abs(den) < 1e-9:
                continue
            w0 = ((B[1] - C[1]) * (p[0] - C[0]) + (C[0] - B[0]) * (p[1] - C[1])) / den
            w1 = ((C[1] - A[1]) * (p[0] - C[0]) + (A[0] - C[0]) * (p[1] - C[1])) / den
            w2 = 1.0 - w0 - w1
            if w0 < -1e-4 or w1 < -1e-4 or w2 < -1e-4:
                continue
            z = w0 * A[2] + w1 * B[2] + w2 * C[2]
            if z > p[2] + up or (best is not None and z <= best[0]):
                continue
            la, lb, lc = V[t[0]][2], V[t[1]][2], V[t[2]][2]
            best = (z, (w0 * la[0] + w1 * lb[0] + w2 * lc[0], w0 * la[1] + w1 * lb[1] + w2 * lc[1]))
        return best[1] if best else None


def place(o, ax, sc, p):
    return tuple(o[k] + sc * (p[0] * ax[0][k] + p[1] * ax[1][k] + p[2] * ax[2][k]) for k in range(3))


def rot(ax, n):
    return _norm(tuple(n[0] * ax[0][k] + n[1] * ax[1][k] + n[2] * ax[2][k] for k in range(3)))


# ───────────────────────── collision ─────────────────────────

def inv3(m):
    a, b, c = m[0]
    d, e, f = m[1]
    g, h, i = m[2]
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if abs(det) < 1e-12:
        return None
    k = 1.0 / det
    return [[(e * i - f * h) * k, (c * h - b * i) * k, (b * f - c * e) * k],
            [(f * g - d * i) * k, (a * i - c * g) * k, (c * d - a * f) * k],
            [(d * h - e * g) * k, (b * g - a * h) * k, (a * e - b * d) * k]]


def surf_type_of_material(mi):
    bits = mi.get("surfaceTypeBits", 0) if mi else 0
    for k in range(32):
        if bits >> k & 1:
            return k
    return 0


def collision(md, log):
    """-> (verts in game units, tris [(a, b, c, surftype)], stats). Wound like the render mesh (outward normals)."""
    stats = {}
    polys = []           # (points, outward normal, surftype, source)
    nb = 0
    for b in md.brushes:
        if not b["contents"] & wm.PLAYER_SOLID:
            continue
        nb += 1
        for pi, poly in wm.brush_polys(b["planes"]):
            mn = b["mats"][pi] if pi < len(b["mats"]) else None
            st = (md.cmaterials[mn][1] >> 20) & 0x1F if mn is not None and 0 <= mn < len(md.cmaterials) else 0
            polys.append((poly, b["planes"][pi][0], st, "brush"))
    stats["brushes_player_solid"] = nb
    stats["brush_faces"] = len(polys)
    # internal faces: two brush faces with the same (rounded) outline and opposite normals touch -> drop both
    q = 1.0 / 8.0
    keyed = {}
    for i, (poly, n, st, src) in enumerate(polys):
        key = tuple(sorted((round(p[0] / q), round(p[1] / q), round(p[2] / q)) for p in poly))
        keyed.setdefault(key, []).append(i)
    drop = set()
    for key, ids in keyed.items():
        if len(ids) < 2:
            continue
        for x in range(len(ids)):
            for y in range(x + 1, len(ids)):
                i, j = ids[x], ids[y]
                if i in drop or j in drop:
                    continue
                if _dot(polys[i][1], polys[j][1]) < -0.999:
                    drop.add(i)
                    drop.add(j)
                elif _dot(polys[i][1], polys[j][1]) > 0.999:
                    drop.add(j)                 # duplicate face of an overlapping brush
    stats["brush_faces_internal_dropped"] = len(drop)
    tris = []
    verts = []
    vmap = {}

    def vid(p):
        key = (round(p[0] * 16), round(p[1] * 16), round(p[2] * 16))
        i = vmap.get(key)
        if i is None:
            i = vmap[key] = len(verts)
            verts.append((key[0] / 16.0, key[1] / 16.0, key[2] / 16.0))
        return i

    def add_tri(A, B, C, n_out, st):
        a, b, c = vid(A), vid(B), vid(C)
        if a == b or b == c or a == c:
            return
        g = _cross(_sub(verts[b], verts[a]), _sub(verts[c], verts[a]))
        if _dot(g, g) < 1e-8:
            return
        if n_out is not None and _dot(g, n_out) < 0:
            b, c = c, b
        tris.append((a, b, c, st))

    for i, (poly, n, st, src) in enumerate(polys):
        if i in drop:
            continue
        for k in range(1, len(poly) - 1):
            add_tri(poly[0], poly[k], poly[k + 1], n, st)
    stats["brush_tris"] = len(tris)
    # collision triangles (terrain / patches): surface type through the aabb trees (leaf -> partition -> material)
    part_mat = {}
    for t in wm.items(md.c["aabbTrees"]):
        if t["childCount"] == 0:
            u = t["u"]
            pidx = u.get("partitionIndex") if isinstance(u, dict) else None
            if pidx is not None:
                part_mat.setdefault(pidx, t["materialIndex"])
    tri_mat = {}
    for pi, (tc, _bc, ft) in enumerate(md.partitions):
        m = part_mat.get(pi)
        for k in range(ft, ft + tc):
            tri_mat.setdefault(k, m)
    n0 = len(tris)
    skipped_nonsolid = 0
    V = md.cverts
    # winding of the game's collision triangles: terrain is mostly floor, so the orientation whose up-facing
    # (|n.z| > 0.7) triangles point up in the majority is the outward one
    up = down = 0
    for a, b, c in md.ctris:
        g = _norm(_cross(_sub(V[b], V[a]), _sub(V[c], V[a])))
        if g[2] > 0.7:
            up += 1
        elif g[2] < -0.7:
            down += 1
    ccw_out = up >= down
    stats["collision_tris_winding"] = {"bxc_up": up, "bxc_down": down, "outward": "(b-a)x(c-a)" if ccw_out else
                                       "(c-a)x(b-a)"}
    for k, (a, b, c) in enumerate(md.ctris):
        m = tri_mat.get(k)
        st = 0
        if m is not None and 0 <= m < len(md.cmaterials):
            if not md.cmaterials[m][2] & wm.PLAYER_SOLID:
                skipped_nonsolid += 1
                continue
            st = (md.cmaterials[m][1] >> 20) & 0x1F
        A, B, C = V[a], V[b], V[c]
        n = _cross(_sub(B, A), _sub(C, A)) if ccw_out else _cross(_sub(C, A), _sub(B, A))
        add_tri(A, B, C, n, st)
    stats["collision_tris"] = len(tris) - n0
    stats["collision_tris_nonsolid_skipped"] = skipped_nonsolid
    stats["collision_tris_with_material"] = sum(1 for k in range(len(md.ctris)) if tri_mat.get(k) is not None)
    # static models: their collision LOD (XModel.collLod), placed by inverse(invScaledAxis)
    n0 = len(tris)
    nsm = 0
    for x in wm.items(md.c["staticModelList"]):
        xm = wm.deref(x["xmodel"])
        if not isinstance(xm, dict) or not (xm["contents"] & wm.PLAYER_SOLID) or xm["collLod"] < 0:
            continue
        m = inv3(x["invScaledAxis"])
        if m is None:
            continue
        nsm += 1
        o = x["origin"]
        for mi, vs, ts in xmodel_lod_geom(md, xm, min(xm["collLod"], xm["numLods"] - 1)):
            st = surf_type_of_material(mi)
            P = [place(o, m, 1.0, p) for p, _, _ in vs]
            N = [rot(m, nn) for _, _, nn in vs]
            for a, b, c in ts:
                nn = (N[a][0] + N[b][0] + N[c][0], N[a][1] + N[b][1] + N[c][1], N[a][2] + N[b][2] + N[c][2])
                add_tri(P[a], P[b], P[c], nn, st)
    stats["static_models_with_collision"] = nsm
    stats["static_model_tris"] = len(tris) - n0
    stats["total_tris"] = len(tris)
    stats["verts"] = len(verts)
    return verts, tris, stats


# ───────────────────────── sky ─────────────────────────

SKY_FACES = (  # GoldSrc order rt bk lf ft up dn: (forward, up) in game axes (rt = +X, ft = +Y as the loader maps them)
    ((1, 0, 0), (0, 0, 1)), ((0, -1, 0), (0, 0, 1)), ((-1, 0, 0), (0, 0, 1)), ((0, 1, 0), (0, 0, 1)),
    ((0, 0, 1), (0, -1, 0)), ((0, 0, -1), (0, 1, 0)))


def sky_faces(md, side, log):
    """Render the skybox xmodel (all LOD0 surfaces, centred on its origin) into 6 faces -> [rgba top row first]."""
    import waw_raster as wr
    name = md.g["skyBoxModel"]
    xm = None
    for tn, obj, _ in md.assets:
        if tn == "XModel" and isinstance(obj, dict) and obj.get("name") == name:
            xm = obj
            break
    if xm is None:
        log(f"  sky: skybox model {name!r} not found")
        return None
    geo = xmodel_lod_geom(md, xm, 0)
    tex = {}
    layers = []
    for mi, vs, ts in geo:
        img, _mode = material_color(mi)
        if img is None:
            continue
        if img not in tex:
            try:
                tex[img] = iwi_level(md.read_image(img), 1024)
            except Exception as e:      # noqa: BLE001
                log(f"  sky: {img}: {e}")
                continue
        tris = [((vs[a][0], vs[b][0], vs[c][0]), (vs[a][1], vs[b][1], vs[c][1])) for a, b, c in ts]
        dist = sum(math.sqrt(_dot(p, p)) for p, _, _ in vs) / max(1, len(vs))
        opaque = min(tex[img][2][3::4]) >= 250
        layers.append((not opaque, -dist, img, tris))
    # opaque backdrop first, then the blended shells far to near (the game draws them as layered shells)
    layers.sort(key=lambda x: (x[0], x[1]))
    faces = []
    for fwd, up in SKY_FACES:
        faces.append(wr.render_sky_face([(img, tris) for _, _, img, tris in layers], tex, fwd, up, side))
    return faces


def sky_section(faces, side):
    row = side * 4
    flipped = [b"".join(bytes(f[(side - 1 - y) * row:(side - y) * row]) for y in range(side)) for f in faces]
    return struct.pack("<2i", side, 0) + b"".join(flipped)


# ───────────────────────── entities ─────────────────────────

def entity_text(md):
    """GoldSrc-style entity text (worldspawn first) + [(classname, [(koff, klen, voff, vlen)])]."""
    ents = [e for e in md.ents if e.get("classname") == "worldspawn"] + \
           [e for e in md.ents if e.get("classname") != "worldspawn"]
    extra = []
    for e in md.ents:
        if e.get("classname") == "mp_dm_spawn" and e.get("origin"):
            a = {"classname": "info_player_deathmatch", "origin": e["origin"], "gsvr_alias": "mp_dm_spawn"}
            if e.get("angles"):
                a["angles"] = e["angles"]
            extra.append(a)
    parts = []
    table = []
    pos = 0
    for e in ents + extra:
        kvs = []
        chunk = "{\n"
        for k, v in e.items():
            k = k.replace('"', "'")
            v = str(v).replace('"', "'")
            ko = pos + len(chunk.encode("latin-1", "replace")) + 1
            vo = ko + len(k.encode("latin-1", "replace")) + 3
            kvs.append((ko, len(k.encode("latin-1", "replace")), vo, len(v.encode("latin-1", "replace"))))
            chunk += f'"{k}" "{v}"\n'
        chunk += "}\n"
        enc = chunk.encode("latin-1", "replace")
        parts.append(enc)
        pos += len(enc)
        table.append((e.get("classname", ""), kvs))
    return b"".join(parts), table


# ───────────────────────── build ─────────────────────────

def world_surfaces(md):
    out = []
    lo, hi = md.lit
    skipped = {"sky": 0, "no_lightmap": 0, "not_drawable": 0, "decal": 0, "brushmodel": 0}
    for s in md.surfaces:
        i = s["index"]
        if i in md.sky_surfs:
            skipped["sky"] += 1
            continue
        if i >= md.static_surface_count:
            skipped["brushmodel"] += 1
            continue
        if md.decal[0] <= i < md.decal[1]:
            skipped["decal"] += 1
            continue
        if not lo <= i < hi:
            skipped["not_drawable"] += 1
            continue
        if s["lightmap"] != 0:
            skipped["no_lightmap"] += 1
            continue
        if not drawable(md.materials.get(s["mat"])):
            skipped["not_drawable"] += 1
            continue
        out.append(s)
    return out, skipped


def build(ff, out_path, iwd=wm.IWD_DIR, max_tex=256, lm_size=1024, sky_side=256, smodel_max_tris=6500,
          sec_gain=1.6, sun_gain=0.7, log=print):
    t0 = time.time()
    md = wm.MapData(ff, iwd_dir=iwd if iwd and os.path.isdir(iwd) else None, log=log)
    log(f"map decoded ({time.time() - t0:.1f} s)")
    stats = {"map": os.path.splitext(os.path.basename(ff))[0], "converter": VERSION}
    surfs, skipped = world_surfaces(md)
    stats["world_surfaces"] = len(surfs)
    stats["world_surfaces_skipped"] = skipped

    # static model instances + their lod / material list
    inst = []
    smodel_skipped = 0
    for sm in md.smodels:
        xm = sm["xm"]
        if not isinstance(xm, dict):
            smodel_skipped += 1
            continue
        lod = pick_lod(md, xm, smodel_max_tris)
        geo = [(mi, vs, ts) for mi, vs, ts in xmodel_lod_geom(md, xm, lod) if drawable(mi)]
        inst.append((sm, lod, geo))

    # textures
    names = {}
    for s in surfs:
        img, mode = material_color(md.materials[s["mat"]])
        names.setdefault(img, mode)
    for sm, lod, geo in inst:
        for mi, _, _ in geo:
            img, mode = material_color(mi)
            names.setdefault(img, mode)
    texs, missing = load_textures(md, names, max_tex, log)
    AW, AH, apos = pack_atlas(texs)
    levels = build_atlas(texs, AW, AH, apos)
    stats["textures"] = {"count": len(texs), "missing": [n for n, _ in missing], "atlas": [AW, AH, MIPS],
                         "atlas_bytes": sum(len(x) for x in levels), "max_tex": max_tex,
                         "alpha_test": sum(1 for m in names.values() if m != "opaque")}
    log(f"atlas {AW}x{AH} from {len(texs)} textures ({time.time() - t0:.1f} s)")

    def rect_of(img):
        x, y, w, h = apos[img]
        return (x / AW, y / AH, w / AW, h / AH)

    # render mesh (game units -> Unity on write)
    pos, uv0, uv1, rect, idx = array.array("f"), array.array("f"), array.array("f"), array.array("f"), array.array("i")
    flips = 0
    vmap = {}                            # (world vertex, image) -> pack vertex (surfaces share vertex ranges)

    def wvert(vi, img, r):
        k = (vi, img)
        j = vmap.get(k)
        if j is None:
            j = vmap[k] = len(pos) // 3
            p, uv, lmuv, _col, _n = md.verts[vi]
            pos.extend(to_unity(p))
            uv0.extend(uv)
            uv1.extend(lmuv)
            rect.extend(r)
        return j

    for s in surfs:
        img, _ = material_color(md.materials[s["mat"]])
        r = rect_of(img)
        for a, b, c in md.surface_tris(s):
            A, B, C = md.verts[a][0], md.verts[b][0], md.verts[c][0]
            g = _cross(_sub(to_unity(B), to_unity(A)), _sub(to_unity(C), to_unity(A)))
            nn = to_unity(md.verts[a][4])
            if _dot(g, nn) < 0:          # Unity front face: (b - a) x (c - a) along the normal
                b, c = c, b
                flips += 1
            idx.extend((wvert(a, img, r), wvert(b, img, r), wvert(c, img, r)))
    world_tris = len(idx) // 3
    stats["world_tris"] = world_tris
    stats["world_tris_flipped_to_unity_winding"] = flips
    log(f"world mesh {len(pos) // 3} verts {world_tris} tris ({time.time() - t0:.1f} s)")

    # lightmap
    lm, cov = bake_lightmap(md, surfs, lm_size, sec_gain, sun_gain)
    # neutral-ish block for static models without a floor below: the unused bottom-right corner
    S = lm_size
    fb = 4
    used = any(cov[y * S + x] for y in range(S - fb, S) for x in range(S - fb, S))
    fallback_uv = ((S - fb / 2) / S, (S - fb / 2) / S)
    if not used:
        for y in range(S - fb, S):
            for x in range(S - fb, S):
                lm[4 * (y * S + x):4 * (y * S + x) + 4] = bytes((110, 110, 115, 255))
    stats["lightmap"] = [S, S]
    log(f"lightmap baked ({time.time() - t0:.1f} s)")

    # static models into the world mesh
    ff_ = FloorFinder(md, surfs)
    sm_tris = 0
    no_floor = 0
    lod_hist = {}
    for sm, lod, geo in inst:
        o, ax, sc = sm["origin"], sm["axis"], sm["scale"]
        lmuv = ff_.lmap_below(o)
        if lmuv is None:
            lmuv = fallback_uv
            no_floor += 1
        lod_hist[sm["model"]] = lod
        for mi, vs, ts in geo:
            img, _ = material_color(mi)
            r = rect_of(img)
            base = len(pos) // 3
            P = [to_unity(place(o, ax, sc, p)) for p, _, _ in vs]
            N = [to_unity(rot(ax, n)) for _, _, n in vs]
            for k, (p, uv, n) in enumerate(vs):
                pos.extend(P[k])
                uv0.extend(uv)
                uv1.extend(lmuv)
                rect.extend(r)
            for a, b, c in ts:
                g = _cross(_sub(P[b], P[a]), _sub(P[c], P[a]))
                nn = (N[a][0] + N[b][0] + N[c][0], N[a][1] + N[b][1] + N[c][1], N[a][2] + N[b][2] + N[c][2])
                if _dot(g, nn) < 0:
                    b, c = c, b
                idx.extend((a + base, b + base, c + base))
                sm_tris += 1
    stats["static_models"] = {"instances": len(inst), "skipped": smodel_skipped, "tris": sm_tris,
                              "no_floor_light": no_floor, "lod_used": lod_hist}
    V, I = len(pos) // 3, len(idx)
    log(f"static models {len(inst)} instances {sm_tris} tris ({time.time() - t0:.1f} s)")

    # collision
    cverts, ctris, cstats = collision(md, log)
    stats["collision"] = cstats
    log(f"collision {cstats['total_tris']} tris {cstats['verts']} verts ({time.time() - t0:.1f} s)")

    # sky
    faces = sky_faces(md, sky_side, log) if sky_side else None
    stats["sky"] = {"model": md.g["skyBoxModel"], "side": sky_side if faces else 0}

    # entities
    etext, etable = entity_text(md)
    classes = {}
    for cn, _ in etable:
        classes[cn] = classes.get(cn, 0) + 1
    stats["entities"] = {"count": len(etable), "bytes": len(etext),
                         "spawns": {k: v for k, v in classes.items() if "spawn" in k or k.startswith("info_player")}}

    # ── write ──
    E = len(etext)
    core = [pos.tobytes(), uv0.tobytes(), uv1.tobytes(), rect.tobytes(), idx.tobytes()] + [bytes(x) for x in levels] + \
           [bytes(lm), etext + b"\0" * (-E % 4)]
    dir_off = HEADER + sum(len(x) for x in core)
    secs = []
    # MODL: world record (24 x 4 bytes)
    mins = [min(pos[k::3]) / INCH for k in range(3)]
    maxs = [max(pos[k::3]) / INCH for k in range(3)]
    gmins = (mins[0], mins[2], mins[1])
    gmaxs = (maxs[0], maxs[2], maxs[1])
    modl = struct.pack("<14i9fi", 0, 0, 0, V, 0, 0, I, 0, 0, 1, -1, -1, -1, -1, 0.0, 0.0, 0.0, *gmins, *gmaxs, 0)
    secs.append((b"MODL", modl, 1))
    secs.append((b"PLAN", b"", 0))
    secs.append((b"HNOD", b"", 0))
    ents = b"".join(struct.pack("<4i", sum(len(t[1]) for t in etable[:k]), len(kv), 0 if k == 0 else -1, 0)
                    for k, (_, kv) in enumerate(etable))
    secs.append((b"ENTS", ents, len(etable)))
    ekvs = b"".join(struct.pack("<4i", *x) for _, kv in etable for x in kv)
    secs.append((b"EKVS", ekvs, sum(len(kv) for _, kv in etable)))
    cp = array.array("f")
    for p in cverts:
        cp.extend(to_unity(p))
    ci = array.array("i")
    for a, b, c, _st in ctris:
        ci.extend((a, c, b))          # outward in game axes -> (a, c, b) is outward after the mirroring y/z swap
    secs.append((b"CLIP", cp.tobytes() + ci.tobytes(), len(ctris)))
    cs = bytes(t[3] for t in ctris)
    secs.append((b"CSRF", cs + b"\0" * (-len(cs) % 4), len(ctris)))
    if faces:
        secs.append((b"SKY6", sky_section(faces, sky_side), 6))
    stats["counts"] = {"V": V, "I": I, "tris": I // 3, "world_tris": world_tris, "static_model_tris": sm_tris,
                       "clip_tris": len(ctris), "clip_verts": len(cverts)}
    info = json.dumps({k: v for k, v in stats.items() if k != "static_models"} |
                      {"static_models": {k: v for k, v in stats["static_models"].items() if k != "lod_used"}},
                      sort_keys=True).encode()
    secs.append((b"CODI", info + b" " * (-len(info) % 4), 1))
    off = dir_off + 4 + 16 * len(secs)
    dirb = struct.pack("<i", len(secs))
    for tag, data, cnt in secs:
        assert len(data) % 4 == 0, tag
        dirb += struct.pack("<4s3i", tag, off, len(data), cnt)
        off += len(data)
    header = struct.pack("<4s15i", b"GSV3", HEADER, V, I, 0, AW, AH, MIPS, S, S, E, 1, len(texs), V, I, dir_off)
    assert len(header) == HEADER
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(header)
        for x in core:
            f.write(x)
        f.write(dirb)
        for _, data, _ in secs:
            f.write(data)
    os.replace(tmp, out_path)
    size = os.path.getsize(out_path)
    stats["size"] = size
    stats["size_parts"] = {"vertices": V * 44, "indices": I * 4, "atlas": sum(len(x) for x in levels),
                           "lightmap": len(lm), "entities": E, **{t.decode(): len(d) for t, d, _ in secs}}
    stats["runtime_ram_estimate"] = runtime_ram(stats)
    stats["seconds"] = round(time.time() - t0, 1)
    if size > MAX_PACK:
        log(f"WARNING pack {size} B > VRChat limit {MAX_PACK}")
    log(f"wrote {out_path}: {size / 1e6:.1f} MB ({stats['seconds']} s)")
    return stats


def runtime_ram(st):
    """Rough Unity-side bytes while/after loading (pack byte[] + decoded arrays + mesh + textures + colliders)."""
    c = st["counts"]
    V, I = c["V"], c["I"]
    atlas = st["size_parts"]["atlas"]
    lm = st["size_parts"]["lightmap"]
    arrays = V * (12 + 8 + 8 + 16) + I * 4                     # Vector3/2/2/4 + int[] copies of the pack data
    mesh = V * (12 + 8 + 8 + 16) + I * 4                       # Unity mesh CPU copy (GPU similar)
    col = c["clip_verts"] * 12 + c["clip_tris"] * 12
    col_cooked = c["clip_tris"] * 3 * 12 * 2                   # PhysX cooked meshes, rough
    peak = st["size"] + arrays + mesh + atlas + lm + col + col_cooked
    steady = mesh + atlas + lm + col_cooked
    return {"peak_load_mb": round(peak / 1e6, 1), "steady_mb": round(steady / 1e6, 1),
            "note": "peak = pack byte[] + decoded arrays + mesh + textures + colliders; steady = after the pack byte[] "
                    "and decode arrays are released"}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ff")
    ap.add_argument("out")
    ap.add_argument("--iwd", default=wm.IWD_DIR)
    ap.add_argument("--max-tex", type=int, default=256)
    ap.add_argument("--lm-size", type=int, default=1024)
    ap.add_argument("--sky", type=int, default=256)
    ap.add_argument("--smodel-max-tris", type=int, default=6500)
    ap.add_argument("--sec-gain", type=float, default=1.6)
    ap.add_argument("--sun-gain", type=float, default=0.7)
    ap.add_argument("--json")
    a = ap.parse_args()
    st = build(a.ff, a.out, a.iwd, a.max_tex, a.lm_size, a.sky, a.smodel_max_tris, a.sec_gain, a.sun_gain)
    js = a.json or os.path.splitext(a.out)[0] + ".json"
    with open(js, "w") as f:
        json.dump(st, f, indent=1, sort_keys=True)
    print(json.dumps({k: st[k] for k in ("size", "counts", "textures", "collision", "runtime_ram_estimate", "sky")},
                     indent=1))


if __name__ == "__main__":
    main()
