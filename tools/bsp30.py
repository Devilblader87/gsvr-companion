# GoldSrc BSP v30 reader + OBJ export (world model only).
# Also: entity parsing, embedded miptex access, per-face lightmap extents/luxels (lighting lump = RGB, 1 luxel / 16 units),
# and the collision trees: planes, nodes + leaves (hull 0, point contents incl. water/lava), clipnodes (hulls 1-3).
# usage: python3 -I bsp30.py <map.bsp> <out.obj>   — prints stats; texture names go to usemtl
import struct, sys

LUMPS = ["entities", "planes", "textures", "vertices", "visibility", "nodes", "texinfo", "faces",
         "lighting", "clipnodes", "leaves", "marksurfaces", "edges", "surfedges", "models"]


def _looks_like_entities(data, off, ln):
    """True when the lump is entity text: inside the file, first non-blank byte '{', first 8 bytes printable."""
    if ln <= 0 or off < 0 or off + ln > len(data):
        return False
    head = data[off:off + min(ln, 8)]
    return head.lstrip()[:1] == b"{" and all(c in (9, 10, 13, 0) or 32 <= c < 127 for c in head)


def lump_dir(data):
    """The 15 (offset, length) lump entries of a BSP v30 in canonical order (entities, planes, ...).
    Blue Shift's shipped maps are v30 but store LUMP_ENTITIES and LUMP_PLANES swapped; the engine detects this and swaps
    them back. Same rule here: entry 0 is not entity text but entry 1 is -> swap. Normal maps are returned untouched."""
    d = [struct.unpack_from("<ii", data, 4 + i * 8) for i in range(15)]
    if not _looks_like_entities(data, *d[0]) and _looks_like_entities(data, *d[1]):
        d[0], d[1] = d[1], d[0]
    return d


class Bsp:
    def __init__(self, path):
        self.data = open(path, "rb").read()
        version, = struct.unpack_from("<i", self.data, 0)
        if version != 30:
            raise ValueError(f"not BSP v30: {version}")
        self.lumps = dict(zip(LUMPS, lump_dir(self.data)))     # Blue Shift: entities/planes swapped, see lump_dir
        self.vertices = list(struct.iter_unpack("<3f", self.lump("vertices")))
        self.edges = list(struct.iter_unpack("<2H", self.lump("edges")))
        self.surfedges = [s[0] for s in struct.iter_unpack("<i", self.lump("surfedges"))]
        # dface_t: planenum, side, firstedge, numedges, texinfo, styles[4], lightofs
        self.faces = list(struct.iter_unpack("<hhihh4Bi", self.lump("faces")))
        # texinfo: vecs[2][4], miptex, flags
        self.texinfo = [(t[0:4], t[4:8], t[8], t[9]) for t in struct.iter_unpack("<8fii", self.lump("texinfo"))]
        # dmodel_t: mins[3], maxs[3], origin[3], headnode[4], visleafs, firstface, numfaces
        self.models = list(struct.iter_unpack("<9f4iiii", self.lump("models")))
        self.textures = self._read_miptex()
        self.entities = self.lump("entities").split(b"\0")[0].decode("latin-1")
        self.lighting = self.lump("lighting")
        # dplane_t: normal[3], dist, type
        self.planes = list(struct.iter_unpack("<4fi", self.lump("planes")))
        # dnode_t: planenum, children[2] (short; <0 = -(leaf+1)), mins[3], maxs[3] (short), firstface, numfaces
        self.nodes = [(n[0], n[1], n[2]) for n in struct.iter_unpack("<i2h6h2H", self.lump("nodes"))]
        # dleaf_t: contents, visofs, mins[3], maxs[3], firstmarksurface, nummarksurfaces, ambient[4]
        self.leaf_contents = [l[0] for l in struct.iter_unpack("<ii6h2H4B", self.lump("leaves"))]
        # dclipnode_t: planenum, children[2] (short; <0 = contents)
        self.clipnodes = list(struct.iter_unpack("<i2h", self.lump("clipnodes")))

    def entity_list(self):
        """Entity lump → list of dicts (keys lower-case is NOT applied; GoldSrc keys are lower-case anyway)."""
        out, cur = [], None
        import re
        for m in re.finditer(r'\{|\}|"([^"]*)"\s*"([^"]*)"', self.entities):
            tok = m.group(0)
            if tok == "{":
                cur = {}
            elif tok == "}":
                if cur is not None:
                    out.append(cur)
                cur = None
            elif cur is not None:
                cur[m.group(1)] = m.group(2)
        return out

    def miptex_offset(self, i):
        """Absolute file offset of embedded miptex i (or None)."""
        off, _ = self.lumps["textures"]
        mo, = struct.unpack_from("<i", self.data, off + 4 + 4 * i)
        return None if mo < 0 else off + mo

    def face_lightmap(self, face):
        """→ (mins_s, mins_t, w, h, rgb bytes or None) for lightstyle 0. Coordinates in luxels (16 texels)."""
        import math
        s, t, _m, _f = self.texinfo[face[4]]
        ss, ts = [], []
        for x, y, z in self.face_polygon(face):
            ss.append(x * s[0] + y * s[1] + z * s[2] + s[3])
            ts.append(x * t[0] + y * t[1] + z * t[2] + t[3])
        bmin_s, bmin_t = math.floor(min(ss) / 16), math.floor(min(ts) / 16)
        bmax_s, bmax_t = math.ceil(max(ss) / 16), math.ceil(max(ts) / 16)
        w, h = bmax_s - bmin_s + 1, bmax_t - bmin_t + 1
        ofs, style0 = face[9], face[5]
        rgb = None
        if ofs >= 0 and style0 != 255 and ofs + w * h * 3 <= len(self.lighting):
            rgb = self.lighting[ofs:ofs + w * h * 3]
        return bmin_s, bmin_t, w, h, rgb

    def lump(self, name):
        off, length = self.lumps[name]
        return self.data[off:off + length]

    def _read_miptex(self):
        off, length = self.lumps["textures"]
        count, = struct.unpack_from("<i", self.data, off)
        out = []
        for i in range(count):
            mo, = struct.unpack_from("<i", self.data, off + 4 + 4 * i)
            if mo < 0:
                out.append(("<missing>", 64, 64, False)); continue
            name, w, h, o0 = struct.unpack_from("<16sIII", self.data, off + mo)
            out.append((name.split(b"\0")[0].decode("latin-1"), w, h, o0 != 0))  # embedded if offset != 0
        return out

    def face_polygon(self, face):
        first, num = face[2], face[3]
        pts = []
        for se in self.surfedges[first:first + num]:
            e = self.edges[abs(se)]
            pts.append(self.vertices[e[0] if se >= 0 else e[1]])
        return pts


def export_obj(bsp, out):
    model = bsp.models[0]
    firstface, numfaces = model[14], model[15]
    lines, vt_lines, by_tex = [], [], {}
    vi = 1
    for f in bsp.faces[firstface:firstface + numfaces]:
        s, t, miptex, _flags = bsp.texinfo[f[4]]
        name, w, h, _ = bsp.textures[miptex]
        if name.lower() in ("sky", "aaatrigger") or name.lower().startswith(("clip", "origin")):
            continue
        poly = bsp.face_polygon(f)
        idx = []
        for x, y, z in poly:
            u = (x * s[0] + y * s[1] + z * s[2] + s[3]) / w
            v = (x * t[0] + y * t[1] + z * t[2] + t[3]) / h
            lines.append(f"v {x:.3f} {z:.3f} {-y:.3f}")   # GoldSrc Z-up -> Y-up
            vt_lines.append(f"vt {u:.5f} {-v:.5f}")
            idx.append(vi); vi += 1
        tris = [(idx[0], idx[i], idx[i + 1]) for i in range(1, len(idx) - 1)]  # faces are convex
        by_tex.setdefault(name, []).extend(tris)
    with open(out, "w") as fh:
        fh.write("\n".join(lines) + "\n" + "\n".join(vt_lines) + "\n")
        for name, tris in by_tex.items():
            fh.write(f"usemtl {name}\n")
            fh.write("\n".join(f"f {a}/{a} {b}/{b} {c}/{c}" for a, b, c in tris) + "\n")
    return vi - 1, sum(len(t) for t in by_tex.values()), len(by_tex)


if __name__ == "__main__":
    b = Bsp(sys.argv[1])
    nverts, ntris, ntex = export_obj(b, sys.argv[2])
    emb = sum(1 for t in b.textures if t[3])
    ents = b.entities.count("{")
    classes = {}
    for line in b.entities.splitlines():
        if '"classname"' in line:
            c = line.split('"')[3]; classes[c] = classes.get(c, 0) + 1
    print(f"models {len(b.models)}, world faces {b.models[0][15]}, exported verts {nverts}, tris {ntris}, textures {ntex} "
          f"({emb}/{len(b.textures)} embedded, rest from WAD), lightmap bytes {b.lumps['lighting'][1]}, entities {ents}")
    print("entity classes:", ", ".join(f"{k}×{v}" for k, v in sorted(classes.items(), key=lambda kv: -kv[1])[:25]))
