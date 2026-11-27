#!/usr/bin/env python3
"""WaW MP map decoder: GfxWorld (render geometry, materials, static models), clipMap_t (brushes, collision
triangles, static model collision list, dyn ents) and map_ents, straight from the player's own mp_*.ff.
Python 3 stdlib only; built on waw_zone.Walker (full sequential zone walk). Run with pypy3 for speed.

BYOG: everything written by this tool goes to scratch (never into the repo).

Usage:
  waw_map.py info    <mp_x.ff> [--json out.json] [--iwd <main dir>]     counts + texture/pack budget
  waw_map.py export  <mp_x.ff> --out <dir> [--iwd <main dir>]           OBJs (render, collision, walkable), ents
  waw_map.py preview <mp_x.ff> --out <dir> [--iwd <main dir>] [--size 1400]   top-down + perspective PNGs

Coordinates: game units (inches), Z up (as in the game). Unity: x' = x, y' = z, z' = y, scale 0.0254.
"""
import argparse
import json
import math
import os
import re
import struct
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import waw_zone as wz  # noqa: E402

CACHE = "/tmp/codwaw_scratch/map"
IWD_DIR = os.environ.get("CODWAW_MAIN", os.path.join(os.path.expanduser("~"), "MacBackup", "GameInstalls", "CoDWaW", "main"))   # default for the CLI; --iwd overrides

# IW3-family contents bits (bg_public.h conventions; the ones that matter for player movement)
CONTENTS = {0x1: "solid", 0x2: "foliage", 0x4: "noncolliding", 0x10: "glass", 0x20: "water", 0x40: "canshootclip",
            0x80: "missileclip", 0x200: "vehicleclip", 0x400: "itemclip", 0x800: "sky", 0x1000: "ai_nosight",
            0x2000: "clipshot", 0x4000: "mover", 0x10000: "playerclip", 0x20000: "monsterclip",
            0x8000000: "detail", 0x10000000: "structural", 0x20000000: "transparent"}
PLAYER_SOLID = 0x1 | 0x10000
SEMANTIC = {0: "2d", 1: "function", 2: "color", 5: "normal", 8: "specular", 11: "water"}


def deref(x):
    """Loaded value or None (unresolved back-references count as None)."""
    return None if isinstance(x, wz.Unresolved) else x


def items(x):
    """Elements of a loaded pointer target: list items, [dict] for a single struct, Blob values."""
    x = deref(x)
    if x is None:
        return []
    if isinstance(x, dict):
        return [x]
    if isinstance(x, wz.Blob):
        return x.values()
    return x


def _inline_bytes(im):
    """Pixel bytes stored in the fastfile itself (0 = loaded from images/<name>.iwi in the iwds)."""
    if not isinstance(im, dict) or not isinstance(im.get("texture"), dict):
        return 0
    ld = deref(im["texture"].get("loadDef"))
    return ld["resourceSize"] if isinstance(ld, dict) else 0


def kstr(x):
    return x if isinstance(x, str) else None


def unpack_unit_vec(v):
    x, y, z, w = v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF, (v >> 24) & 0xFF
    s = (w + 192.0) / 32385.0
    return ((x - 127.0) * s, (y - 127.0) * s, (z - 127.0) * s)


def half(h):
    return struct.unpack("<e", struct.pack("<H", h))[0]


# ---- map ents ----------------------------------------------------------------------------------------
def parse_ents(text):
    ents = []
    for m in re.finditer(r"\{([^{}]*)\}", text):
        e = {}
        for k, v in re.findall(r'"([^"]*)"\s+"([^"]*)"', m.group(1)):
            e[k] = v
        ents.append(e)
    return ents


def vec(s):
    try:
        return tuple(float(x) for x in s.split())
    except (ValueError, AttributeError):
        return None


# ---- brush hulls ------------------------------------------------------------------------------------
def clip_poly(poly, n, d, eps=0.01):
    """Keep the part of poly with n.p <= d."""
    out = []
    L = len(poly)
    for i in range(L):
        a, b = poly[i], poly[(i + 1) % L]
        da = a[0] * n[0] + a[1] * n[1] + a[2] * n[2] - d
        db = b[0] * n[0] + b[1] * n[1] + b[2] * n[2] - d
        if da <= eps:
            out.append(a)
        if (da > eps) != (db > eps):
            t = da / (da - db)
            out.append((a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]), a[2] + t * (b[2] - a[2])))
    return out


def brush_polys(planes, size=65536.0):
    """planes: [(normal, dist)] with inside = n.p <= d. Returns [(plane index, [points])] convex faces."""
    faces = []
    for i, (n, d) in enumerate(planes):
        ax = max(range(3), key=lambda k: abs(n[k]))
        up = (0.0, 0.0, 1.0) if ax != 2 else (1.0, 0.0, 0.0)
        # u = up x n, v = n x u
        u = (up[1] * n[2] - up[2] * n[1], up[2] * n[0] - up[0] * n[2], up[0] * n[1] - up[1] * n[0])
        ul = math.sqrt(sum(c * c for c in u)) or 1.0
        u = tuple(c / ul for c in u)
        v = (n[1] * u[2] - n[2] * u[1], n[2] * u[0] - n[0] * u[2], n[0] * u[1] - n[1] * u[0])
        o = tuple(n[k] * d for k in range(3))
        poly = [tuple(o[k] + size * (su * u[k] + sv * v[k]) for k in range(3))
                for su, sv in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
        for j, (n2, d2) in enumerate(planes):
            if j != i:
                poly = clip_poly(poly, n2, d2)
                if len(poly) < 3:
                    break
        if len(poly) >= 3:
            # wind so that the face normal points outwards (along n)
            a, b, c = poly[0], poly[1], poly[2]
            e1 = [b[k] - a[k] for k in range(3)]
            e2 = [c[k] - a[k] for k in range(3)]
            cr = (e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0])
            if cr[0] * n[0] + cr[1] * n[1] + cr[2] * n[2] < 0:
                poly = poly[::-1]
            faces.append((i, poly))
    return faces


# ---- decoder -------------------------------------------------------------------------------------------
class MapData:
    def __init__(self, ff, cache=CACHE, iwd_dir=None, log=print):
        self.ff = ff
        self.log = log
        got = {}

        def on(tname, obj, at):
            if tname in ("GfxWorld", "clipMap_t", "ComWorld", "GameWorldMp"):
                got.setdefault(tname, obj)
        import waw_ff
        _, self.u = waw_ff.load_zone(ff, cache)
        w = self.w = wz.Walker(self.u)
        w.walk(on_asset=on)
        self.assets = w.assets
        self.zone_exact = w.pos == len(self.u)
        self.g, self.c, self.com = got["GfxWorld"], got["clipMap_t"], got.get("ComWorld")
        self.iwd_dir = iwd_dir
        self._iwd_index = None
        self._decode_world()
        self._decode_materials()
        self._decode_smodels()
        self._decode_clip()

    # -- helpers
    def arr(self, ref, fmt):
        """Unpack a leaf Blob (flat array in the zone) with struct format fmt per element."""
        ref = deref(ref)
        if not isinstance(ref, wz.Blob) or ref.pos is None:
            return []
        sz = struct.calcsize("<" + fmt)
        return list(struct.iter_unpack("<" + fmt, self.u[ref.pos:ref.pos + sz * ref.count]))

    def index_in(self, array, esize, x):
        """Element index of x (a back-referenced element, or a raw zone pointer) inside the loaded array."""
        base = self.w.where(array)
        fp = self.w.ptr_filepos(x) if isinstance(x, int) else self.w.where(x)
        if base is None or fp is None:
            return None
        d = fp - base
        n = len(array) if isinstance(array, list) else array.count
        return d // esize if d % esize == 0 and 0 <= d // esize < n else None

    # -- GfxWorld
    def _decode_world(self):
        g = self.g
        self.vertex_count = g["vertexCount"]
        raw = self.arr(g["vd"]["vertices"], "3ff4B2f2f2I")
        self.verts = [((r[0], r[1], r[2]), (r[8], r[9]), (r[10], r[11]), (r[4], r[5], r[6], r[7]), unpack_unit_vec(r[12]))
                      for r in raw]
        self.indices = [x[0] for x in self.arr(g["indices"], "H")]
        d = g["dpvs"]
        self.static_surface_count = d["staticSurfaceCount"]
        self.lit = (d["litSurfsBegin"], d["litSurfsEnd"])
        self.decal = (d["decalSurfsBegin"], d["decalSurfsEnd"])
        self.emissive = (d["emissiveSurfsBegin"], d["emissiveSurfsEnd"])
        # skyStartSurfs index the draw-sorted list (dpvs.sortedSurfIndex), not the surface array
        sorted_idx = [x[0] for x in self.arr(d["sortedSurfIndex"], "H")]
        self.sky_start = [x[0] for x in self.arr(g["skyStartSurfs"], "i")]
        self.sky_surfs = set(sorted_idx[i] for i in self.sky_start if i < len(sorted_idx))
        self.surfaces = []
        for i, s in enumerate(items(d["surfaces"])):
            tr = s["tris"]
            self.surfaces.append({
                "index": i, "firstVertex": tr["firstVertex"], "vertexCount": tr["vertexCount"],
                "triCount": tr["triCount"], "baseIndex": tr["baseIndex"], "lightmap": s["lightmapIndex"],
                "flags": s["flags"], "material": deref(s["material"]), "bounds": s["bounds"]})
        self.brushmodels = [{"surfaceCount": b["surfaceCount"], "startSurfIndex": b["startSurfIndex"],
                             "bounds": b["bounds"]} for b in items(g["models"])]
        self.lightmaps = []
        for x in items(g["lightmaps"]):
            for k in ("primary", "secondary"):
                im = deref(x[k])
                if isinstance(im, dict):
                    ld = deref(im["texture"]["loadDef"])
                    self.lightmaps.append({"slot": k, "name": kstr(im["name"]), "w": im["width"],
                                           "h": im["height"],
                                           "bytes": ld["resourceSize"] if ld is not None else 0,
                                           "format": ld["format"] if ld is not None else None})
        self.bounds = (g["mins"], g["maxs"])
        self.reflection_probes = g["reflectionProbeCount"]
        self.primary_lights = g["primaryLightCount"]
        self.cells = g["dpvsPlanes"]["cellCount"]

    def surface_tris(self, s):
        b, n, f = s["baseIndex"], s["triCount"], s["firstVertex"]
        ind = self.indices
        return [(ind[b + 3 * k] + f, ind[b + 3 * k + 1] + f, ind[b + 3 * k + 2] + f) for k in range(n)]

    # -- materials
    def material_info(self, m):
        if not isinstance(m, dict):
            return None
        key = id(m)
        if key in self._mat_cache:
            return self._mat_cache[key]
        info = m["info"]
        ts = deref(m["techniqueSet"])
        texs = []
        for it in items(m["textureTable"]):
            u = it["u"]
            im = deref(u["image"]) if isinstance(u, dict) else None
            im = im if isinstance(im, dict) else None
            texs.append({"semantic": it["semantic"], "sem": SEMANTIC.get(it["semantic"], str(it["semantic"])),
                         "nameHash": it["nameHash"], "image": kstr(im["name"]) if im is not None else None,
                         "w": im["width"] if im is not None else None, "h": im["height"] if im is not None else None,
                         "inline_bytes": _inline_bytes(im)})
        r = {"name": kstr(info["name"]), "techset": kstr(ts["name"]) if isinstance(ts, dict) else None,
             "surfaceTypeBits": info["surfaceTypeBits"], "gameFlags": info["gameFlags"], "sortKey": info["sortKey"],
             "textures": texs}
        self._mat_cache[key] = r
        return r

    def _decode_materials(self):
        self._mat_cache = {}
        self.materials = {}
        self.unresolved_material_refs = 0
        for s in self.surfaces:
            mi = self.material_info(s["material"])
            if mi is None:
                self.unresolved_material_refs += 1
                s["mat"] = None
                continue
            s["mat"] = mi["name"]
            self.materials.setdefault(mi["name"], mi)
            if (mi["techset"] or "").endswith("_sky") or (mi["techset"] or "").startswith("sky"):
                self.sky_surfs.add(s["index"])

    # -- static models
    def xmodel_geom(self, xm):
        """LOD0 geometry of a loaded XModel instance: [(material name, verts[(xyz, uv, n)], tris)]."""
        xm = deref(xm)
        if not isinstance(xm, dict):
            return None
        key = id(xm)
        if key in self._xm_cache:
            return self._xm_cache[key]
        lod = xm["lodInfo"][0]
        surfs = items(xm["surfs"])
        mats = items(xm["materialHandles"])
        out = []
        if surfs:
            for si in range(lod["surfIndex"], lod["surfIndex"] + lod["numsurfs"]):
                s = surfs[si]
                vr, tr = deref(s["verts0"]), deref(s["triIndices"])
                if not isinstance(vr, wz.Blob) or not isinstance(tr, wz.Blob) or vr.pos is None or tr.pos is None:
                    continue
                vs = []
                for r in struct.iter_unpack("<3ffI3I", self.u[vr.pos:vr.pos + 32 * vr.count]):
                    vs.append(((r[0], r[1], r[2]), (half(r[5] >> 16), half(r[5] & 0xFFFF)), unpack_unit_vec(r[6])))
                ts = list(struct.iter_unpack("<3H", self.u[tr.pos:tr.pos + 6 * tr.count]))
                mname = None
                if si < len(mats):
                    mi = self.material_info(deref(mats[si]))
                    mname = mi["name"] if mi else None
                    if mi:
                        self.smodel_materials.setdefault(mname, mi)
                out.append((mname, vs, ts))
        self._xm_cache[key] = out
        return out

    def _decode_smodels(self):
        self._xm_cache = {}
        self.smodel_materials = {}
        d = self.g["dpvs"]
        self.smodels = []
        for x in items(d["smodelDrawInsts"]):
            p = x["placement"]
            xm = deref(x["model"])
            self.smodels.append({"model": kstr(xm["name"]) if isinstance(xm, dict) else None, "xm": xm,
                                 "origin": p["origin"], "axis": p["axis"], "scale": p["scale"],
                                 "cullDist": x["cullDist"], "flags": x["flags"]})

    def smodel_tris(self, sm):
        geo = self.xmodel_geom(sm["xm"]) or []
        o, ax, sc = sm["origin"], sm["axis"], sm["scale"]
        out = []
        for mname, vs, ts in geo:
            wv = [tuple(o[k] + sc * (p[0] * ax[0][k] + p[1] * ax[1][k] + p[2] * ax[2][k]) for k in range(3))
                  for p, _, _ in vs]
            out.append((mname, wv, [v[1] for v in vs], ts))
        return out

    # -- clipMap
    def _decode_clip(self):
        c = self.c
        planes_ref = deref(c["planes"])
        self.planes = [((r[0], r[1], r[2]), r[3]) for r in self.arr(planes_ref, "4f4x")]
        self.cmaterials = [(bytes(r[0]).split(b"\0")[0].decode("latin1"), r[1], r[2])
                           for r in self.arr(c["materials"], "64sii")]
        sides_ref = deref(c["brushsides"])
        side_size = wz.T["cbrushside_t"]["size"]
        sides = []
        for it in items(sides_ref):
            sides.append((self.index_in(planes_ref, 20, it["plane"]), it["materialNum"]))
        self.sides = sides
        self.brushes = []
        bad_side_refs = 0
        for b in items(c["brushes"]):
            mins, maxs = b["mins"], b["maxs"]
            planes = [((-1.0, 0.0, 0.0), -mins[0]), ((1.0, 0.0, 0.0), maxs[0]),
                      ((0.0, -1.0, 0.0), -mins[1]), ((0.0, 1.0, 0.0), maxs[1]),
                      ((0.0, 0.0, -1.0), -mins[2]), ((0.0, 0.0, 1.0), maxs[2])]
            ns = b["numsides"]
            sref = b["sides"]
            first = None
            if sref is sides_ref:
                first = 0
            elif isinstance(sref, dict):
                first = self.index_in(sides_ref, side_size, sref)
            if ns and first is None:
                bad_side_refs += 1
            mats = [m for row in b["axialMaterialNum"] for m in row]
            if first is not None:
                for k in range(ns):
                    pi, mn = sides[first + k]
                    if pi is not None:
                        planes.append(self.planes[pi])
                        mats.append(mn)
            self.brushes.append({"contents": b["contents"], "mins": mins, "maxs": maxs, "planes": planes,
                                 "mats": mats, "numsides": ns})
        self.bad_side_refs = bad_side_refs
        self.cverts = [tuple(r) for r in self.arr(c["verts"], "3f")]
        ti = [x[0] for x in self.arr(c["triIndices"], "H")]
        self.ctris = [(ti[3 * k], ti[3 * k + 1], ti[3 * k + 2]) for k in range(len(ti) // 3)]
        wb = deref(c["triEdgeIsWalkable"])
        self.walk_bits = self.u[wb.pos:wb.pos + wb.count] if isinstance(wb, wz.Blob) and wb.pos is not None else b""
        self.partitions = [(p["triCount"], p["borderCount"], p["firstTri"]) for p in items(c["partitions"])]
        self.static_models_clip = []
        for x in items(c["staticModelList"]):
            xm = deref(x["xmodel"])
            ok = isinstance(xm, dict)
            self.static_models_clip.append({"model": kstr(xm["name"]) if ok else None,
                                            "contents": xm["contents"] if ok else None,
                                            "origin": x["origin"]})
        self.dynents = []
        for lst in c["dynEntDefList"] or []:
            for x in items(lst):
                xm = deref(x["xModel"])
                self.dynents.append({"type": x["type"], "model": kstr(xm["name"]) if isinstance(xm, dict) else None,
                                     "origin": x["pose"]["origin"], "health": x["health"],
                                     "contents": x["contents"]})
        es = deref(c["mapEnts"]["entityString"])
        self.ent_text = self.u[es.pos:es.pos + es.count].split(b"\0")[0].decode("latin1")
        self.ents = parse_ents(self.ent_text)
        self.cmodels = deref(c["cmodels"]).count

    # -- textures / iwd
    def iwd_index(self):
        if self._iwd_index is None:
            idx = {}
            if self.iwd_dir and os.path.isdir(self.iwd_dir):
                cp = os.path.join(CACHE, "iwd_index.json")
                if os.path.exists(cp):
                    with open(cp) as f:
                        idx = json.load(f)
                else:
                    for fn in sorted(os.listdir(self.iwd_dir)):
                        if not fn.endswith(".iwd"):
                            continue
                        for n in zipfile.ZipFile(os.path.join(self.iwd_dir, fn)).namelist():
                            if n.lower().endswith(".iwi"):
                                idx[n.lower()] = fn          # later (higher) iwd wins
                    os.makedirs(CACHE, exist_ok=True)
                    with open(cp, "w") as f:
                        json.dump(idx, f)
            self._iwd_index = idx
        return self._iwd_index

    def image_header(self, name):
        """(iwd, header dict, member size) of images/<name>.iwi from the player's iwds (reads 28 bytes)."""
        idx = self.iwd_index()
        mem = f"images/{name.lstrip(',')}.iwi".lower()     # ',name' = reference to an asset of another zone
        fn = idx.get(mem)
        if fn is None:
            return None
        z = self._zips.get(fn) if hasattr(self, "_zips") else None
        if z is None:
            self._zips = getattr(self, "_zips", {})
            z = self._zips[fn] = zipfile.ZipFile(os.path.join(self.iwd_dir, fn))
        info = z.getinfo(next(n for n in z.namelist() if n.lower() == mem)) if mem not in z.NameToInfo else z.NameToInfo[mem]
        with z.open(info) as f:
            hb = f.read(28)
        import waw_iwd
        return fn, waw_iwd.iwi_header(hb), info.file_size

    def read_image(self, name):
        idx = self.iwd_index()
        mem = f"images/{name.lstrip(',')}.iwi".lower()     # ',name' = reference to an asset of another zone
        fn = idx.get(mem)
        if fn is None:
            return None
        self._zips = getattr(self, "_zips", {})
        z = self._zips.get(fn) or self._zips.setdefault(fn, zipfile.ZipFile(os.path.join(self.iwd_dir, fn)))
        info = z.NameToInfo.get(mem) or z.getinfo(next(n for n in z.namelist() if n.lower() == mem))
        return z.read(info)


def all_materials(md):
    m = dict(md.materials)
    for k, v in md.smodel_materials.items():
        m.setdefault(k, v)
    return m


def texture_budget(md, cap=512):
    """Unique images used by world + static model materials, their real sizes, and the pack size at <= cap px."""
    imgs = {}
    for mname, mi in all_materials(md).items():
        for t in mi["textures"]:
            if t["image"] and t["semantic"] in (2, 5, 8):
                imgs.setdefault(t["image"], {"sem": t["sem"], "w": t["w"], "h": t["h"], "materials": 0})
                imgs[t["image"]]["materials"] += 1
    found = 0
    full_bytes = cap_bytes = 0
    by_sem = {}
    for name, x in imgs.items():
        hd = md.image_header(name) if md.iwd_dir else None
        if hd:
            found += 1
            x["iwd"], x["fmt"], x["w"], x["h"], x["file"] = hd[0], hd[1]["format_name"], hd[1]["width"], hd[1]["height"], hd[2]
        fmt = x.get("fmt", "DXT1")
        bpp = {"DXT1": 0.5, "DXT3": 1.0, "DXT5": 1.0, "ARGB32": 4.0, "RGB24": 3.0, "L8A8": 2.0, "A8": 1.0}.get(fmt, 1.0)
        tbpp = 0.5 if fmt == "DXT1" else 1.0          # pack keeps DXT1 / DXT5 blocks (Unity uploads raw)
        w, h = x["w"] or 512, x["h"] or 512
        full_bytes += x.get("file", w * h * bpp * 4 / 3)
        s = max(1.0, max(w, h) / cap)
        cw, ch = max(4, int(w / s)), max(4, int(h / s))
        cb = cw * ch * tbpp * 4 / 3
        x["pack_bytes"] = int(cb)
        cap_bytes += cb
        e = by_sem.setdefault(x["sem"], [0, 0])
        e[0] += 1
        e[1] += cb
    return {"images": imgs, "unique": len(imgs), "found_in_iwd": found, "full_bytes": int(full_bytes),
            "cap": cap, "cap_bytes": int(cap_bytes), "by_semantic": {k: [v[0], int(v[1])] for k, v in by_sem.items()}}


def summary(md, tex=None):
    lit_tris = sum(s["triCount"] for s in md.surfaces[md.lit[0]:md.lit[1]] if s["index"] not in md.sky_surfs)
    all_tris = sum(s["triCount"] for s in md.surfaces)
    sky_tris = sum(md.surfaces[i]["triCount"] for i in md.sky_surfs if i < len(md.surfaces))
    decal_tris = sum(s["triCount"] for s in md.surfaces[md.decal[0]:md.decal[1]])
    bm_tris = sum(s["triCount"] for s in md.surfaces[md.static_surface_count:])
    xm_names = sorted(set(s["model"] for s in md.smodels if s["model"]))
    sm_tris = 0
    sm_unique_tris = 0
    seen = set()
    for s in md.smodels:
        geo = md.xmodel_geom(s["xm"]) or []
        t = sum(len(g[2]) for g in geo)
        sm_tris += t
        if s["model"] not in seen:
            seen.add(s["model"])
            sm_unique_tris += t
    cont = {}
    for b in md.brushes:
        cont[b["contents"]] = cont.get(b["contents"], 0) + 1
    player_brushes = sum(1 for b in md.brushes if b["contents"] & PLAYER_SOLID)
    walk = sum(1 for k in range(len(md.ctris)) if md.walk_bits and (md.walk_bits[(3 * k) >> 3] >> ((3 * k) & 7)) & 1)
    classes = {}
    for e in md.ents:
        cn = e.get("classname", "?")
        classes[cn] = classes.get(cn, 0) + 1
    out = {
        "file": os.path.basename(md.ff), "zone_bytes": len(md.u), "zone_walk_exact": md.zone_exact,
        "assets": len(md.assets),
        "gfxworld": {
            "vertices": md.vertex_count, "indices": len(md.indices), "triangles_all": all_tris,
            "surfaces": len(md.surfaces), "static_surfaces": md.static_surface_count,
            "lit_surfs": md.lit, "decal_surfs": md.decal, "emissive_surfs": md.emissive,
            "sky_surfs": sorted(md.sky_surfs), "sky_tris": sky_tris, "lit_tris_no_sky": lit_tris,
            "decal_tris": decal_tris, "brushmodel_surface_tris": bm_tris, "brushmodels": len(md.brushmodels),
            "materials": len(md.materials), "unresolved_material_refs": md.unresolved_material_refs,
            "lightmaps": md.lightmaps, "cells": md.cells, "reflection_probes": md.reflection_probes,
            "primary_lights": md.primary_lights, "bounds": md.bounds,
        },
        "static_models": {"instances": len(md.smodels), "unique_xmodels": len(xm_names),
                          "instance_tris_lod0": sm_tris, "unique_tris_lod0": sm_unique_tris,
                          "materials": len(md.smodel_materials), "names": xm_names},
        "clipmap": {
            "planes": len(md.planes), "brushes": len(md.brushes), "brush_sides": len(md.sides),
            "player_solid_brushes": player_brushes, "brush_side_refs_unresolved": md.bad_side_refs,
            "brush_contents": {hex(k): v for k, v in sorted(cont.items(), key=lambda kv: -kv[1])},
            "collision_verts": len(md.cverts), "collision_tris": len(md.ctris), "partitions": len(md.partitions),
            "walkable_edge_bits_set": walk, "clip_materials": len(md.cmaterials),
            "static_models": len(md.static_models_clip), "submodels": md.cmodels,
            "dynents": len(md.dynents),
        },
        "map_ents": {"count": len(md.ents), "chars": len(md.ent_text),
                     "classes": dict(sorted(classes.items(), key=lambda kv: -kv[1]))},
    }
    if tex:
        out["textures"] = {k: v for k, v in tex.items() if k != "images"}
    return out


def pack_estimate(md, sm, tex):
    """Bytes for a GSV-style map pack: render mesh (pos 12, uv 8, packed normal 4, colour 4 = 28 B/vert,
    u16/u32 indices), unique static xmodels + instance transforms, collision mesh (12 B/vert + 4 B index),
    DXT textures at the cap, entity text. No lightmap (optional + listed separately)."""
    g = sm["gfxworld"]
    render = md.vertex_count * 28 + g["triangles_all"] * 3 * 4
    st = sm["static_models"]
    smod = st["unique_tris_lod0"] * 3 * (28 + 4) // 2 + st["instances"] * 64
    coll_tris = sm["clipmap"]["collision_tris"] + sum(len(b["planes"]) * 2 for b in md.brushes)
    coll = coll_tris * (12 * 3 // 2 + 12)
    lm = sum(min(x["bytes"], 1024 * 1024 * 2) for x in md.lightmaps)
    total = render + smod + coll + tex["cap_bytes"] + len(md.ent_text)
    return {"render_mesh": render, "static_models": smod, "collision": coll, "textures": tex["cap_bytes"],
            "entities": len(md.ent_text), "total_without_lightmap": total,
            "lightmap_1024_optional": lm, "total_with_lightmap": total + lm}


# ---- OBJ export ---------------------------------------------------------------------------------------
def export(md, out):
    os.makedirs(out, exist_ok=True)
    mats = all_materials(md)
    with open(os.path.join(out, "world.mtl"), "w") as f:
        for n, mi in sorted(mats.items(), key=lambda kv: kv[0] or ""):
            f.write(f"newmtl {n}\n")
            col = next((t["image"] for t in mi["textures"] if t["semantic"] == 2 and t["image"]), None)
            if col:
                f.write(f"map_Kd textures/{col}.png\n")
    # render mesh: world surfaces (no sky), then static models
    nv = nt = 0
    with open(os.path.join(out, "world_render.obj"), "w") as f:
        f.write("# WaW map render mesh (from the player's own fastfile; do not distribute)\nmtllib world.mtl\n")
        for v in md.verts:
            f.write("v %.3f %.3f %.3f\n" % v[0])
        for v in md.verts:
            f.write("vt %.5f %.5f\n" % (v[1][0], 1.0 - v[1][1]))
        nv = len(md.verts)
        cur = None
        f.write("o world\n")
        for s in md.surfaces:
            if s["index"] in md.sky_surfs:
                continue
            if s["mat"] != cur:
                cur = s["mat"]
                f.write(f"usemtl {cur}\n")
            for a, b, c in md.surface_tris(s):
                f.write(f"f {a + 1}/{a + 1} {c + 1}/{c + 1} {b + 1}/{b + 1}\n")
                nt += 1
        base = nv
        for i, sm in enumerate(md.smodels):
            f.write(f"o smodel{i}_{sm['model']}\n")
            for mname, wv, uvs, ts in md.smodel_tris(sm):
                for p in wv:
                    f.write("v %.3f %.3f %.3f\n" % p)
                for uv in uvs:
                    f.write("vt %.5f %.5f\n" % (uv[0], 1.0 - uv[1]))
                f.write(f"usemtl {mname}\n")
                for a, b, c in ts:
                    f.write(f"f {a + base + 1}/{a + base + 1} {c + base + 1}/{c + base + 1} {b + base + 1}/{b + base + 1}\n")
                    nt += 1
                base += len(wv)
                nv += len(wv)
    # collision: player-solid brushes + collision triangles (partitions)
    ncv = nct = 0
    walk = []
    with open(os.path.join(out, "world_collision.obj"), "w") as f:
        f.write("# WaW map collision: clipMap brushes (contents & (solid|playerclip)) + collision tris\n")
        f.write("o collision_tris\n")
        for p in md.cverts:
            f.write("v %.3f %.3f %.3f\n" % p)
        for k, (a, b, c) in enumerate(md.ctris):
            f.write(f"f {a + 1} {c + 1} {b + 1}\n")
            nct += 1
            A, B, C = md.cverts[a], md.cverts[b], md.cverts[c]
            n = _normal(A, B, C)
            if n[2] > 0.7:
                walk.append((A, B, C))
        ncv = len(md.cverts)
        f.write("o brushes\n")
        for bi, b in enumerate(md.brushes):
            if not b["contents"] & PLAYER_SOLID:
                continue
            for pi, poly in brush_polys(b["planes"]):
                for p in poly:
                    f.write("v %.3f %.3f %.3f\n" % p)
                for k in range(1, len(poly) - 1):
                    f.write(f"f {ncv + 1} {ncv + k + 2} {ncv + k + 1}\n")
                    nct += 1
                if b["planes"][pi][0][2] > 0.7:
                    for k in range(1, len(poly) - 1):
                        walk.append((poly[0], poly[k], poly[k + 1]))
                ncv += len(poly)
    with open(os.path.join(out, "walkable.obj"), "w") as f:
        f.write("# up-facing (n.z > 0.7) collision faces: collision tris + player-solid brush tops\n")
        for k, (A, B, C) in enumerate(walk):
            f.write("v %.3f %.3f %.3f\nv %.3f %.3f %.3f\nv %.3f %.3f %.3f\n" % (A + B + C))
            f.write(f"f {3 * k + 1} {3 * k + 3} {3 * k + 2}\n")
    ents_out = os.path.join(out, "map_ents.json")
    with open(ents_out, "w") as f:
        json.dump(md.ents, f, indent=0)
    with open(os.path.join(out, "materials.json"), "w") as f:
        json.dump(mats, f, indent=1)
    return {"render_obj_verts": nv, "render_obj_tris": nt, "collision_obj_verts": ncv, "collision_obj_tris": nct,
            "walkable_tris": len(walk)}


def _normal(A, B, C):
    e1 = (B[0] - A[0], B[1] - A[1], B[2] - A[2])
    e2 = (C[0] - A[0], C[1] - A[1], C[2] - A[2])
    n = (e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0])
    ln = math.sqrt(n[0] * n[0] + n[1] * n[1] + n[2] * n[2]) or 1.0
    return (n[0] / ln, n[1] / ln, n[2] / ln)


# ---- preview (software rasterizer) ----------------------------------------------------------------------
def material_colors(md):
    """Average colour of each material's colour map, from the smallest mip >= 4x4 of the iwi (cheap)."""
    import waw_iwd
    cols = {}
    cache = {}
    for n, mi in all_materials(md).items():
        img = next((t["image"] for t in mi["textures"] if t["semantic"] == 2 and t["image"]), None)
        c = (150, 150, 150)
        if img and md.iwd_dir:
            if img not in cache:
                cache[img] = None
                try:
                    b = md.read_image(img)
                    if b:
                        hd = waw_iwd.iwi_header(b)
                        fmt, w, h = hd["format"], hd["width"], hd["height"]
                        # mips are stored smallest first: walk up to an 8x8 (or smaller) level
                        sizes = []
                        mw, mh = w, h
                        while True:
                            sizes.append((mw, mh))
                            if mw == 1 and mh == 1:
                                break
                            mw, mh = max(1, mw // 2), max(1, mh // 2)
                        o = 28
                        for mw, mh in reversed(sizes):
                            nb = waw_iwd.mip_bytes(fmt, mw, mh)
                            if mw >= 8 or mh >= 8 or (mw, mh) == (w, h):
                                if fmt in (0x0B, 0x0C, 0x0D):
                                    rgba = waw_iwd.decode_dxt(b[o:o + nb], max(4, mw), max(4, mh), fmt)
                                else:
                                    rgba = None
                                if rgba:
                                    px = len(rgba) // 4
                                    cache[img] = tuple(sum(rgba[k::4][:px]) // px for k in range(3))
                                break
                            o += nb
                except Exception:
                    cache[img] = None
            if cache.get(img):
                c = cache[img]
        cols[n] = c
    return cols


def preview(md, out, size=1400):
    import waw_iwd
    os.makedirs(out, exist_ok=True)
    cols = material_colors(md)
    tris = []      # (A, B, C, colour)
    for s in md.surfaces:
        if s["index"] in md.sky_surfs or md.decal[0] <= s["index"] < md.decal[1]:
            continue
        col = cols.get(s["mat"], (150, 150, 150))
        V = md.verts
        for a, b, c in md.surface_tris(s):
            tris.append((V[a][0], V[b][0], V[c][0], col))
    for sm in md.smodels:
        for mname, wv, uvs, ts in md.smodel_tris(sm):
            col = cols.get(mname, (170, 160, 140))
            for a, b, c in ts:
                tris.append((wv[a], wv[b], wv[c], col))
    # frame on the playable area (spawn points + 15 % margin)
    sp = [vec(e.get("origin")) for e in md.ents if e.get("classname", "").startswith("mp_") and "spawn" in e.get("classname", "")]
    sp = [p for p in sp if p]
    if sp:
        mn = [min(p[k] for p in sp) for k in range(3)]
        mx = [max(p[k] for p in sp) for k in range(3)]
        pad = 0.25 * max(mx[0] - mn[0], mx[1] - mn[1])
        box = (mn[0] - pad, mn[1] - pad, mx[0] + pad, mx[1] + pad)
    else:
        box = (md.bounds[0][0], md.bounds[0][1], md.bounds[1][0], md.bounds[1][1])
    res = {}
    W = size
    H = int(size * (box[3] - box[1]) / (box[2] - box[0]))
    top_cut = (max(p[2] for p in sp) + 200.0) if sp else 1e9      # floor plan: drop roofs above the spawns
    img = raster_top([t for t in tris if min(t[0][2], t[1][2], t[2][2]) < top_cut], box, W, H)
    for p in sp:       # spawn markers
        x = int((p[0] - box[0]) / (box[2] - box[0]) * W)
        y = int((box[3] - p[1]) / (box[3] - box[1]) * H)
        for dy in range(-3, 4):
            for dx in range(-3, 4):
                if 0 <= x + dx < W and 0 <= y + dy < H and abs(dx) + abs(dy) <= 4:
                    k = 4 * ((y + dy) * W + x + dx)
                    img[k:k + 4] = b"\xff\x30\x30\xff"
    p = os.path.join(out, "preview_top.png")
    waw_iwd.write_png(p, W, H, img)
    res["top"] = (p, W, H)
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    zs = [p[2] for p in sp] or [0.0]
    ground = sorted(zs)[len(zs) // 2]
    r = 0.55 * max(box[2] - box[0], box[3] - box[1])
    cut = [t for t in tris if min(t[0][2], t[1][2], t[2][2]) < top_cut]
    for name, yaw, pitch, dist, tl in (("preview_persp.png", 225.0, 35.0, 1.15, tris),
                                       ("preview_cutaway.png", 60.0, 42.0, 1.1, cut)):
        eye = (cx + dist * r * math.cos(math.radians(yaw)), cy + dist * r * math.sin(math.radians(yaw)),
               ground + dist * r * math.tan(math.radians(pitch)))
        PW, PH = size, int(size * 0.62)
        im2 = raster_persp(tl, eye, (cx, cy, ground), PW, PH, 60.0)
        p = os.path.join(out, name)
        waw_iwd.write_png(p, PW, PH, im2)
        res[name] = (p, PW, PH)
    return res, len(tris)


def collision_tris(md):
    """(A, B, C, colour) for player-solid brush faces (blue, walkable tops green) and collision tris (orange)."""
    out = []
    for b in md.brushes:
        if not b["contents"] & PLAYER_SOLID:
            continue
        clipish = not b["contents"] & 0x1        # playerclip-only brushes: invisible walls
        for pi, poly in brush_polys(b["planes"]):
            up = b["planes"][pi][0][2] > 0.7
            col = (90, 200, 110) if up else ((200, 80, 200) if clipish else (90, 130, 210))
            for k in range(1, len(poly) - 1):
                out.append((poly[0], poly[k], poly[k + 1], col))
    V = md.cverts
    for a, b_, c in md.ctris:
        out.append((V[a], V[b_], V[c], (225, 150, 60)))
    return out


def preview_collision(md, out, size=1400):
    import waw_iwd
    tris = collision_tris(md)
    sp = [vec(e.get("origin")) for e in md.ents if e.get("classname", "").startswith("mp_") and "spawn" in e.get("classname", "")]
    sp = [p for p in sp if p]
    mn = [min(p[k] for p in sp) for k in range(3)]
    mx = [max(p[k] for p in sp) for k in range(3)]
    pad = 0.25 * max(mx[0] - mn[0], mx[1] - mn[1])
    box = (mn[0] - pad, mn[1] - pad, mx[0] + pad, mx[1] + pad)
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    ground = sorted(p[2] for p in sp)[len(sp) // 2]
    top_cut = max(p[2] for p in sp) + 200.0
    cut = [t for t in tris if min(t[0][2], t[1][2], t[2][2]) < top_cut]
    r = 0.55 * max(box[2] - box[0], box[3] - box[1])
    yaw, pitch, dist = 60.0, 42.0, 1.1
    eye = (cx + dist * r * math.cos(math.radians(yaw)), cy + dist * r * math.sin(math.radians(yaw)),
           ground + dist * r * math.tan(math.radians(pitch)))
    PW, PH = size, int(size * 0.62)
    img = raster_persp(cut, eye, (cx, cy, ground), PW, PH, 60.0)
    p = os.path.join(out, "preview_collision.png")
    waw_iwd.write_png(p, PW, PH, img)
    return p, len(tris)


LIGHT = (0.35, -0.5, 0.79)


def _shade(A, B, C, col):
    n = _normal(A, B, C)
    l = abs(n[0] * LIGHT[0] + n[1] * LIGHT[1] + n[2] * LIGHT[2])
    s = 0.35 + 0.75 * l
    return (min(255, int(col[0] * s)), min(255, int(col[1] * s)), min(255, int(col[2] * s)))


def raster_top(tris, box, W, H):
    img = bytearray(b"\x16\x18\x1c\xff" * (W * H))
    zb = [-1e30] * (W * H)
    sx = W / (box[2] - box[0])
    sy = H / (box[3] - box[1])
    for A, B, C, col in tris:
        P = [((p[0] - box[0]) * sx, (box[3] - p[1]) * sy, p[2]) for p in (A, B, C)]
        _fill(img, zb, W, H, P, _shade(A, B, C, col), top=True)
    return img


def raster_persp(tris, eye, target, W, H, fov):
    fx = (target[0] - eye[0], target[1] - eye[1], target[2] - eye[2])
    fl = math.sqrt(sum(c * c for c in fx))
    f = tuple(c / fl for c in fx)
    r = (f[1], -f[0], 0.0)
    rl = math.sqrt(r[0] ** 2 + r[1] ** 2) or 1.0
    r = (r[0] / rl, r[1] / rl, 0.0)
    up = (r[1] * f[2] - r[2] * f[1], r[2] * f[0] - r[0] * f[2], r[0] * f[1] - r[1] * f[0])
    foc = (W / 2) / math.tan(math.radians(fov / 2))
    img = bytearray(b"\x16\x18\x1c\xff" * (W * H))
    zb = [-1e30] * (W * H)
    for A, B, C, col in tris:
        P = []
        for p in (A, B, C):
            d = (p[0] - eye[0], p[1] - eye[1], p[2] - eye[2])
            z = d[0] * f[0] + d[1] * f[1] + d[2] * f[2]
            if z < 16.0:
                break
            x = d[0] * r[0] + d[1] * r[1] + d[2] * r[2]
            y = d[0] * up[0] + d[1] * up[1] + d[2] * up[2]
            P.append((W / 2 - foc * x / z, H / 2 - foc * y / z, -z))
        if len(P) < 3:
            continue
        _fill(img, zb, W, H, P, _shade(A, B, C, col), top=False)
    return img


def _fill(img, zb, W, H, P, col, top):
    (x0, y0, z0), (x1, y1, z1), (x2, y2, z2) = P
    den = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
    if not abs(den) > 1e-6 or not abs(den) < 1e30:
        return
    minx, maxx = max(0, int(min(x0, x1, x2))), min(W - 1, int(max(x0, x1, x2)))
    miny, maxy = max(0, int(min(y0, y1, y2))), min(H - 1, int(max(y0, y1, y2)))
    if minx > maxx or miny > maxy:
        return
    px_col = bytes((col[0], col[1], col[2], 255))
    inv = 1.0 / den
    for py in range(miny, maxy + 1):
        fy = py + 0.5
        for px in range(minx, maxx + 1):
            fx = px + 0.5
            w0 = ((y1 - y2) * (fx - x2) + (x2 - x1) * (fy - y2)) * inv
            w1 = ((y2 - y0) * (fx - x2) + (x0 - x2) * (fy - y2)) * inv
            w2 = 1.0 - w0 - w1
            if w0 < 0 or w1 < 0 or w2 < 0:
                continue
            d = w0 * z0 + w1 * z1 + w2 * z2
            k = py * W + px
            if d <= zb[k]:
                continue
            zb[k] = d
            img[4 * k:4 * k + 4] = px_col


# ---- CLI -----------------------------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["info", "export", "preview", "all"])
    ap.add_argument("ff")
    ap.add_argument("--out", default=os.path.join(CACHE, "out"))
    ap.add_argument("--json")
    ap.add_argument("--iwd", default=IWD_DIR)
    ap.add_argument("--cache", default=CACHE)
    ap.add_argument("--size", type=int, default=1400)
    ap.add_argument("--cap", type=int, default=512)
    a = ap.parse_args()
    iwd = a.iwd if a.iwd and os.path.isdir(a.iwd) else None
    md = MapData(a.ff, a.cache, iwd)
    tex = texture_budget(md, a.cap)
    sm = summary(md, tex)
    sm["pack_estimate"] = pack_estimate(md, sm, tex)
    if a.cmd in ("export", "all"):
        sm["export"] = export(md, a.out)
    if a.cmd in ("preview", "all"):
        res, n = preview(md, a.out, a.size)
        cp, cn = preview_collision(md, a.out, a.size)
        res["collision"] = cp
        sm["preview"] = {"tris": n, "collision_tris": cn, "files": res}
    print(json.dumps({k: v for k, v in sm.items() if k != "static_models"}, indent=1, default=str))
    print("static models:", sm["static_models"]["instances"], "instances,", sm["static_models"]["unique_xmodels"],
          "unique")
    js = a.json or os.path.join(a.out, "summary.json")
    os.makedirs(os.path.dirname(js), exist_ok=True)
    with open(js, "w") as f:
        sm["textures_detail"] = tex["images"]
        json.dump(sm, f, indent=1, default=str)


if __name__ == "__main__":
    main()
