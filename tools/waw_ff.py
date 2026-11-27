#!/usr/bin/env python3
"""Call of Duty: World at War (IW 3.0, PC) fastfile reader. Python 3 stdlib only.

BYOG: reads the player's own zone/<lang>/*.ff. Never write game data into the repo.

Format (verified on the German retail PC files, see docs/inventory.md):
  file  = "IWffu100" + u32 version (0x183 = 387) + zlib stream (one deflate stream to EOF)
  zone  = XFile header (36 bytes: size, externalSize, blockSize[7])
          + XAssetList { i32 scriptStringCount; ptr; i32 assetCount; ptr }   at 0x24
          + scriptString pointer array (count * u32, 0 = null entry, -1 = inline string follows)
          + the inline strings (only for -1 entries, NUL terminated, no padding)
          + XAsset[assetCount] { i32 type; ptr header }  (8 bytes each)
          + asset data, streamed in asset order. The stream has NO alignment padding.
  pointers inside data: 0xFFFFFFFF = "data follows inline", 0 = null, else (block << 29 | offset) + 1
  reference to data already loaded (the linker dedups strings and shared sub-assets).

Assets referenced by other assets (materials, images, loaded sounds) are mostly written inline inside the
referencing asset, not as their own XAsset entry, so the asset table under-counts them.

Name recovery is heuristic: each asset's fixed-size header precedes its name string, so we look for
"0xFFFFFFFF ... <fixed size> ... name\\0" patterns with type specific header sizes found by hand
(xmodel 0xE4, xanim 0x58, weapon 0x9AC; techset 0xF8 is known but not used for counting). See docs/inventory.md for the hit rates.

Usage:
  waw_ff.py info   <file.ff> [--json out.json]     header, block sizes, asset type counts, recovered names
  waw_ff.py xmodel <file.ff> <xmodel name> [--obj out.obj] [--lod 0]
  waw_ff.py sound  <file.ff> <path substring> [--out dir] [--pcm]  dump inline RIFF loaded sounds (+ PCM16)
  waw_ff.py weapon <file.ff> <weapon name>        WeaponDef location + int fields that match a value list
The decompressed zone is cached next to --cache (default /tmp/codwaw_scratch/ff).
"""
import argparse
import json
import os
import re
import struct
import sys
import zlib

MAGIC = b"IWffu100"
VERSION_WAW_PC = 0x183
DEFAULT_CACHE = "/tmp/codwaw_scratch/ff"

# WaW PC XAssetType (confirmed against content: 4 = xanim, 5 = xmodel, 6 = material, 7 = techset,
# 9 = sound alias list, 12 = clipmap_pvs, 13 = comworld, 15 = gameworld_mp, 17 = gfxworld, 24 = weapon,
# 26 = fx, 32 = rawfile, 33 = stringtable). The rest follows the known IW3/IW3.0 order.
ASSET_TYPES = [
    "xmodelpieces", "physpreset", "physconstraints", "destructibledef", "xanim", "xmodel", "material",
    "techset", "image", "sound", "loaded_sound", "clipmap", "clipmap_pvs", "comworld", "gameworld_sp",
    "gameworld_mp", "map_ents", "gfxworld", "lightdef", "ui_map", "font", "menulist", "menu",
    "localize", "weapon", "snddriverglobals", "fx", "impactfx", "aitype", "mptype", "character",
    "xmodelalias", "rawfile", "stringtable", "packindex",
]

HDR_XMODEL = 0xE4
HDR_XANIM = 0x58
HDR_WEAPON = 0x9AC
HDR_TECHSET = 0xF8
FF = b"\xff\xff\xff\xff"


def tname(t):
    return ASSET_TYPES[t] if 0 <= t < len(ASSET_TYPES) else f"type{t}"


def load_zone(path, cache=DEFAULT_CACHE):
    """Return (version, zone bytes). Decompressed zone cached as <cache>/<base>.zone."""
    with open(path, "rb") as f:
        head = f.read(12)
        if head[:8] != MAGIC:
            raise ValueError(f"{path}: bad magic {head[:8]!r}")
        ver = struct.unpack_from("<I", head, 8)[0]
        base = os.path.splitext(os.path.basename(path))[0]
        cp = os.path.join(cache, base + ".zone") if cache else None
        if cp and os.path.exists(cp) and os.path.getmtime(cp) >= os.path.getmtime(path):
            with open(cp, "rb") as c:
                return ver, c.read()
        z = zlib.decompressobj()
        data = z.decompress(f.read()) + z.flush()
    if cp:
        os.makedirs(cache, exist_ok=True)
        with open(cp, "wb") as c:
            c.write(data)
    return ver, data


class Zone:
    def __init__(self, path, cache=DEFAULT_CACHE):
        self.path = path
        self.version, self.u = load_zone(path, cache)
        u = self.u
        self.size, self.external = struct.unpack_from("<II", u, 0)
        self.blocks = list(struct.unpack_from("<7I", u, 8))
        sc, _, ac, _ = struct.unpack_from("<iIiI", u, 36)
        ptrs = struct.unpack_from(f"<{sc}I", u, 52)
        o = 52 + 4 * sc
        self.script_strings = []
        for p in ptrs:
            if p == 0xFFFFFFFF:
                e = u.index(b"\0", o)
                self.script_strings.append(u[o:e].decode("latin1"))
                o = e + 1
            else:
                self.script_strings.append(None)
        self.assets = [struct.unpack_from("<iI", u, o + 8 * i) for i in range(ac)]
        self.data_start = o + 8 * ac

    def type_counts(self):
        c = {}
        for t, _ in self.assets:
            c[tname(t)] = c.get(tname(t), 0) + 1
        return dict(sorted(c.items(), key=lambda kv: -kv[1]))

    # ---- heuristic name recovery -------------------------------------------------------------------
    def _names(self):
        """All NUL-delimited identifier strings in the data area: (offset, str)."""
        if not hasattr(self, "_nm"):
            self._nm = [(m.start() + 1, m.group(1).decode()) for m in
                        re.finditer(rb"\x00([A-Za-z0-9_~$\-/.,@&#]{2,90})(?=\x00)", self.u, )
                        if m.start() >= self.data_start - 1]
        return self._nm

    def find_xmodels(self):
        out, u = [], self.u
        for p, s in self._names():
            h = p - HDR_XMODEL
            if h < 0 or u[h:h + 4] != FF:
                continue
            nb, nrb, ns = u[h + 4], u[h + 5], u[h + 6]
            if not (1 <= nb and nrb <= nb and 1 <= ns <= 64):
                continue
            dist = struct.unpack_from("<f", u, h + 0x28)[0]
            nlods = struct.unpack_from("<H", u, h + 0xC4)[0]
            if not (0.0 < dist <= 1.1e6) or not (1 <= nlods <= 4) or not re.fullmatch(r"[A-Za-z0-9_@\-#]+", s):
                continue
            out.append((h, s))
        return out

    def find_xanims(self):
        out, u = [], self.u
        for p, s in self._names():
            h = p - HDR_XANIM
            if h < 0 or u[h:h + 4] != FF:
                continue
            fr = struct.unpack_from("<f", u, h + 0x28)[0]
            if 1.0 <= fr <= 240.0:
                out.append((h, s))
        return out

    def find_weapons(self):
        out, u = [], self.u
        for p, s in self._names():
            h = p - HDR_WEAPON
            if h < 0 or u[h:h + 4] != FF or not re.fullmatch(r"[a-z0-9_]+", s):
                continue
            nxt = u[p + len(s) + 1:p + len(s) + 9]
            if re.search(r"[a-z]", s) and (s.endswith("_mp") or nxt.startswith(b"WEAPON_")):
                out.append((h, s))
        return out

    def find_techsets(self):
        out, u = [], self.u
        for p, s in self._names():
            for h in (p - HDR_TECHSET, p - HDR_TECHSET - 1):
                if h >= 0 and u[h:h + 4] == FF:
                    out.append((h, s))
                    break
        return out

    def find_rawfiles(self):
        out, u = [], self.u
        for m in re.finditer(rb"\xff\xff\xff\xff(....)\xff\xff\xff\xff([\x21-\x7e]{3,120})\x00", u):
            ln = struct.unpack("<i", m.group(1))[0]
            if 0 <= ln < 8 << 20 and (b"/" in m.group(2) or b"." in m.group(2)):
                out.append((m.start(), m.group(2).decode(), ln))
        return out

    def find_sound_aliases(self):
        """snd_alias_list_t {name*, head*, count} = -1, -1, count then the name."""
        out, u = [], self.u
        for m in re.finditer(rb"\xff\xff\xff\xff\xff\xff\xff\xff([\x01-\x40])\x00\x00\x00([a-z0-9_]{3,80})\x00", u):
            out.append((m.start(), m.group(2).decode(), m.group(1)[0]))
        return out

    def find_loaded_sounds(self):
        out, u = [], self.u
        for m in re.finditer(rb"([A-Za-z0-9_/\-. ]{3,160}\.wav)\x00RIFF(....)WAVE", u):
            ln = struct.unpack("<I", m.group(2))[0] + 8
            fmt_tag, ch, rate = struct.unpack_from("<HHI", u, m.end() + 8)
            out.append({"name": m.group(1).decode(), "riff_off": m.start(2) - 4, "bytes": ln,
                        "fmt": fmt_tag, "channels": ch, "rate": rate})
        return out


# ---- XModel / XSurface ---------------------------------------------------------------------------
XSURF = 0x40      # sizeof(XSurface) in the WaW PC zone stream
VERT = 32         # sizeof(GfxPackedVertex)


def unpack_unit_vec(v):
    # PackedUnitVec (IW3): 3 bytes x/y/z + scale byte; decode = (b - 127.0) / 127 * ((s + 192) / 32385 *...)
    x, y, z, w = v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF, (v >> 24) & 0xFF
    s = (w + 192.0) / 32385.0
    return ((x - 127.0) * s, (y - 127.0) * s, (z - 127.0) * s)


def half(h):
    return struct.unpack("<e", struct.pack("<H", h))[0]


def parse_xmodel(zone, name):
    u = zone.u
    hits = [h for h, s in zone.find_xmodels() if s == name]
    if not hits:
        raise KeyError(f"xmodel {name!r} not found")
    h = hits[0]
    nb, nrb, ns, lodramp = u[h + 4], u[h + 5], u[h + 6], u[h + 7]
    lods = []
    for i in range(4):
        b = h + 0x28 + 28 * i
        dist, lns, lsi = struct.unpack_from("<fHH", u, b)
        lods.append({"dist": dist, "numsurfs": lns, "surfIndex": lsi})
    radius = struct.unpack_from("<f", u, h + 0xA8)[0]
    mins = struct.unpack_from("<3f", u, h + 0xAC)
    maxs = struct.unpack_from("<3f", u, h + 0xB8)
    numlods = struct.unpack_from("<H", u, h + 0xC4)[0]
    o = h + HDR_XMODEL + len(name) + 1
    bone_ids = struct.unpack_from(f"<{nb}H", u, o)
    bones = [zone.script_strings[i] if i < len(zone.script_strings) else f"#{i}" for i in bone_ids]
    o += 2 * nb
    nn = nb - nrb
    parents = list(u[o:o + nn]) if nn > 0 else []      # parentList: parent distance (bone i - parent) per non-root bone
    o += nn                           # parentList
    quats = [struct.unpack_from("<4h", u, o + 8 * i) for i in range(nn)]
    o += 8 * nn                       # quats (4 shorts, /32768 = unit quaternion x y z w)
    trans = [struct.unpack_from("<3f", u, o + 12 * i) for i in range(nn)]
    o += 12 * nn                      # trans
    o += 4 * (nb - nrb)               # WaW extra per non-root bone dword (not in CoD4; found by alignment)
    o += nb                           # partClassification
    o += 32 * nb                      # baseMat (DObjAnimMat)
    sbase = o
    surfs = []
    for i in range(ns):
        b = sbase + XSURF * i
        tile, deform, vc, tc, btri, bvert = struct.unpack_from("<BBHHHH", u, b)
        tri_p = struct.unpack_from("<I", u, b + 0x0C)[0]
        vinfo = struct.unpack_from("<4h", u, b + 0x10)
        blend_p, verts_p, _, vlc, vl_p = struct.unpack_from("<IIIII", u, b + 0x18)
        surfs.append({"vertCount": vc, "triCount": tc, "baseTri": btri, "baseVert": bvert,
                      "tri_p": tri_p, "vertInfo": vinfo, "blend_p": blend_p, "verts_p": verts_p,
                      "vertListCount": vlc, "vertList_p": vl_p})
    o = sbase + XSURF * ns
    # streamed sub data per surface (order found on viewmodel_mp_thompson): vertsBlend, verts0, vertList
    # (+ collision trees), triIndices
    for s in surfs:
        if s["blend_p"] == 0xFFFFFFFF:
            vi = s["vertInfo"]
            n = vi[0] + 3 * vi[1] + 5 * vi[2] + 7 * vi[3]
            o += 2 * n
        if s["verts_p"] == 0xFFFFFFFF:
            vs = []
            for k in range(s["vertCount"]):
                x, y, z, bs, col, uv, nrm, tan = struct.unpack_from("<4f4I", u, o + VERT * k)
                vs.append(((x, y, z), (half(uv >> 16), half(uv & 0xFFFF)), unpack_unit_vec(nrm)))   # u = high half
            s["verts"] = vs
            o += VERT * s["vertCount"]
        if s["vertList_p"] == 0xFFFFFFFF:
            lists = []
            for k in range(s["vertListCount"]):
                lists.append(struct.unpack_from("<4HI", u, o + 12 * k))
            o += 12 * s["vertListCount"]
            for bo, vcnt, toff, tcnt, ct in lists:
                if ct == 0xFFFFFFFF:     # XSurfaceCollisionTree: trans[3] scale[3] nodeCount nodes* leafCount leafs*
                    nn, np_, nl, lp = struct.unpack_from("<IIII", u, o + 24)
                    o += 40
                    if np_ == 0xFFFFFFFF:
                        o += 16 * nn
                    if lp == 0xFFFFFFFF:
                        o += 2 * nl
            s["vertLists"] = lists
        if s["tri_p"] == 0xFFFFFFFF:
            s["tris"] = struct.unpack_from(f"<{3 * s['triCount']}H", u, o)
            o += 6 * s["triCount"]
    end = o
    return {"offset": h, "name": name, "numBones": nb, "numRootBones": nrb, "numSurfs": ns,
            "lodRampType": lodramp, "lods": lods, "numLods": numlods, "radius": radius, "mins": mins,
            "maxs": maxs, "bones": bones, "surfs": surfs, "end": end,
            "parentDist": parents, "quat": [tuple(c / 32768.0 for c in q) for q in quats], "trans": trans}


def validate_surfs(m):
    bad = []
    for i, s in enumerate(m["surfs"]):
        if "tris" in s and s["tris"] and max(s["tris"]) >= s["vertCount"]:
            bad.append(f"surf {i}: index {max(s['tris'])} >= vertCount {s['vertCount']}")
        for (p, _, _) in s.get("verts", [])[:64]:
            if any(abs(c) > 1e4 or c != c for c in p):
                bad.append(f"surf {i}: vertex out of range {p}")
                break
    return bad


def write_obj(m, path, lod=0):
    L = m["lods"][lod]
    surfs = m["surfs"][L["surfIndex"]:L["surfIndex"] + L["numsurfs"]]
    base = 1
    with open(path, "w") as f:
        f.write(f"# {m['name']} LOD{lod} (from player's own fastfile; do not distribute)\n")
        for i, s in enumerate(surfs):
            f.write(f"o surf{i}\n")
            for p, uv, n in s["verts"]:
                f.write("v %.5f %.5f %.5f\n" % p)
            for p, uv, n in s["verts"]:
                f.write("vt %.5f %.5f\n" % (uv[0], 1.0 - uv[1]))
            for p, uv, n in s["verts"]:
                f.write("vn %.4f %.4f %.4f\n" % n)
            t = s["tris"]
            for k in range(0, len(t), 3):
                a, b, c = t[k] + base, t[k + 1] + base, t[k + 2] + base
                f.write(f"f {a}/{a}/{a} {c}/{c}/{c} {b}/{b}/{b}\n")
            base += len(s["verts"])
    return sum(len(s["verts"]) for s in surfs), sum(s["triCount"] for s in surfs)


ADPCM_ADAPT = (230, 230, 230, 230, 307, 409, 512, 614, 768, 614, 512, 409, 307, 230, 230, 230)


def riff_chunks(b):
    o, out = 12, {}
    while o + 8 <= len(b):
        cid, ln = b[o:o + 4], struct.unpack_from("<I", b, o + 4)[0]
        out[cid] = b[o + 8:o + 8 + ln]
        o += 8 + ln + (ln & 1)
    return out


def msadpcm_riff_to_pcm(riff, out_path):
    """Decode a Microsoft ADPCM (wFormatTag 2) RIFF to 16-bit PCM WAV. Returns frames written.
    Loaded sounds in WaW fastfiles are MS ADPCM (Unity would not import them as is)."""
    nch, rate, pcm, frames = msadpcm_decode(riff)
    hdr = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " + struct.pack(
        "<IHHIIHH", 16, 1, nch, rate, rate * 2 * nch, 2 * nch, 16) + b"data" + struct.pack("<I", len(pcm))
    with open(out_path, "wb") as f:
        f.write(hdr + pcm)
    return frames


def msadpcm_decode(riff):
    """MS ADPCM RIFF -> (channels, rate, interleaved PCM16 little endian bytes, frames). In memory, no file."""
    ch = riff_chunks(riff)
    fmt, data = ch[b"fmt "], ch[b"data"]
    tag, nch, rate, _, align, bits, cb = struct.unpack_from("<HHIIHHH", fmt, 0)
    spb, ncoef = struct.unpack_from("<HH", fmt, 18)
    coefs = [struct.unpack_from("<hh", fmt, 22 + 4 * i) for i in range(ncoef)]
    pcm = bytearray()
    frames = 0
    for bo in range(0, len(data), align):
        blk = data[bo:bo + align]
        if len(blk) < 7 * nch:
            break
        pred = list(blk[0:nch])
        o = nch
        delta = list(struct.unpack_from(f"<{nch}h", blk, o)); o += 2 * nch
        s1 = list(struct.unpack_from(f"<{nch}h", blk, o)); o += 2 * nch
        s2 = list(struct.unpack_from(f"<{nch}h", blk, o)); o += 2 * nch
        c = [coefs[p] if p < ncoef else (256, 0) for p in pred]
        out = [s2[:], s1[:]]
        nib = []
        for byte in blk[o:]:
            nib.append(byte >> 4)
            nib.append(byte & 15)
        cur = []
        for i, n in enumerate(nib):
            k = i % nch
            sn = n - 16 if n & 8 else n
            p = (s1[k] * c[k][0] + s2[k] * c[k][1]) >> 8
            v = max(-32768, min(32767, p + sn * delta[k]))
            s2[k], s1[k] = s1[k], v
            delta[k] = max(16, (ADPCM_ADAPT[n] * delta[k]) >> 8)
            cur.append(v)
            if len(cur) == nch:
                out.append(cur)
                cur = []
        for fr in out:
            pcm += struct.pack(f"<{nch}h", *fr)
        frames += len(out)
    return nch, rate, bytes(pcm), frames


def strings_between(u, a, b, minlen=4):
    return [(m.start() + a, m.group().decode()) for m in re.finditer(rb"[\x20-\x7e]{%d,}" % minlen, u[a:b])]


# ---- CLI -------------------------------------------------------------------------------------------
def cmd_info(a):
    z = Zone(a.file, a.cache)
    xm, xa, wp = z.find_xmodels(), z.find_xanims(), z.find_weapons()
    rf, sa, ls = z.find_rawfiles(), z.find_sound_aliases(), z.find_loaded_sounds()
    tc = z.type_counts()
    info = {
        "file": os.path.basename(a.file), "ff_bytes": os.path.getsize(a.file), "version": hex(z.version),
        "zone_bytes": len(z.u), "header_size": z.size, "external_size": z.external, "blocks": z.blocks,
        "script_strings": len(z.script_strings), "assets": len(z.assets), "type_counts": tc,
        "recovered": {"xmodel": len(xm), "xanim": len(xa), "weapon": len(wp),
                      "rawfile": len(rf), "sound_alias": len(sa), "loaded_sound_riff": len(ls)},
        "weapons": sorted(set(s for _, s in wp)),
        "xmodels": sorted(set(s for _, s in xm)),
        "rawfiles": sorted(s for _, s, _ in rf),
        "loaded_sound_bytes": sum(x["bytes"] for x in ls),
        "loaded_sound_formats": {},
        "xanims_sample": sorted(s for _, s in xa)[:40],
    }
    for x in ls:
        k = f"fmt{x['fmt']}"
        info["loaded_sound_formats"][k] = info["loaded_sound_formats"].get(k, 0) + 1
    if a.json:
        with open(a.json, "w") as f:
            json.dump(info, f, indent=1)
    print(f"{info['file']}: ff {info['ff_bytes']:,} B -> zone {info['zone_bytes']:,} B, version {info['version']}")
    print(f"  blocks {z.blocks}  script strings {len(z.script_strings)}  assets {len(z.assets)}")
    print("  asset table:", ", ".join(f"{k} {v}" for k, v in tc.items()))
    print("  recovered names:", info["recovered"])
    print(f"  loaded sounds: {len(ls)} RIFF, {info['loaded_sound_bytes']:,} B, {info['loaded_sound_formats']}")
    if wp:
        print("  weapons:", " ".join(info["weapons"]))


def cmd_xmodel(a):
    z = Zone(a.file, a.cache)
    m = parse_xmodel(z, a.name)
    print(f"{m['name']} @0x{m['offset']:x}: bones {m['numBones']} (root {m['numRootBones']}), surfs {m['numSurfs']}, "
          f"numLods {m['numLods']}, radius {m['radius']:.2f}")
    print("  mins", tuple(round(x, 2) for x in m["mins"]), "maxs", tuple(round(x, 2) for x in m["maxs"]))
    print("  bones:", ", ".join(map(str, m["bones"])))
    for i, L in enumerate(m["lods"][:max(1, m["numLods"])]):
        print(f"  lod{i}: dist {L['dist']:.0f} surfs {L['numsurfs']} from {L['surfIndex']}")
    for i, s in enumerate(m["surfs"]):
        print(f"  surf{i}: verts {s['vertCount']} tris {s['triCount']} vertLists {s['vertListCount']} "
              f"blend {s['vertInfo']}")
    bad = validate_surfs(m)
    print("  validation:", "OK" if not bad else bad[:5])
    print(f"  surface data ends @0x{m['end']:x}; strings after (materials/images):")
    for off, s in strings_between(z.u, m["end"], m["end"] + 0x4000, 5)[:40]:
        print(f"    0x{off:x} {s}")
    if a.obj:
        nv, nt = write_obj(m, a.obj, a.lod)
        print(f"  wrote {a.obj}: {nv} verts, {nt} tris (LOD{a.lod})")
    if a.json:
        slim = {k: v for k, v in m.items() if k != "surfs"}
        slim["surfs"] = [{k: v for k, v in s.items() if k not in ("tris", "verts")} for s in m["surfs"]]
        with open(a.json, "w") as f:
            json.dump(slim, f, indent=1)


def cmd_sound(a):
    z = Zone(a.file, a.cache)
    os.makedirs(a.out, exist_ok=True)
    n = 0
    for x in z.find_loaded_sounds():
        if a.match.lower() in x["name"].lower():
            fn = os.path.join(a.out, x["name"].replace("/", "__"))
            riff = z.u[x["riff_off"]:x["riff_off"] + x["bytes"]]
            with open(fn, "wb") as f:
                f.write(riff)
            extra = ""
            if a.pcm and x["fmt"] == 2:
                pfn = fn[:-4] + "_pcm16.wav"
                ns = msadpcm_riff_to_pcm(riff, pfn)
                extra = f", PCM16 {ns} frames -> {pfn}"
            print(f"{x['name']}: {x['bytes']:,} B fmt {x['fmt']} ch {x['channels']} {x['rate']} Hz -> {fn}{extra}")
            n += 1
    print(f"{n} sounds")


def cmd_weapon(a):
    z = Zone(a.file, a.cache)
    hits = [h for h, s in z.find_weapons() if s == a.name]
    if not hits:
        sys.exit(f"weapon {a.name} not found")
    h = hits[0]
    u = z.u
    print(f"WeaponDef {a.name} @0x{h:x} .. 0x{h + HDR_WEAPON:x} ({HDR_WEAPON} bytes)")
    want = [int(v) for v in a.values.split(",")] if a.values else []
    for off in range(0, HDR_WEAPON - 3, 4):
        iv = struct.unpack_from("<i", u, h + off)[0]
        fv = struct.unpack_from("<f", u, h + off)[0]
        if iv in want or (a.floats and any(abs(fv - w) < 1e-4 for w in map(float, a.floats.split(",")))):
            print(f"  +0x{off:03x}: int {iv}  float {fv:g}")
    for off, s in strings_between(u, h + HDR_WEAPON, h + HDR_WEAPON + 0x800, 4)[:30]:
        print(f"  str 0x{off:x} {s}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", default=DEFAULT_CACHE)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("info"); p.add_argument("file"); p.add_argument("--json")
    p = sp.add_parser("xmodel"); p.add_argument("file"); p.add_argument("name"); p.add_argument("--obj")
    p.add_argument("--lod", type=int, default=0); p.add_argument("--json")
    p = sp.add_parser("sound"); p.add_argument("file"); p.add_argument("match"); p.add_argument("--out", default=".")
    p.add_argument("--pcm", action="store_true", help="also decode MS ADPCM to 16-bit PCM wav")
    p = sp.add_parser("weapon"); p.add_argument("file"); p.add_argument("name"); p.add_argument("--values")
    p.add_argument("--floats")
    a = ap.parse_args()
    {"info": cmd_info, "xmodel": cmd_xmodel, "sound": cmd_sound, "weapon": cmd_weapon}[a.cmd](a)


if __name__ == "__main__":
    main()
