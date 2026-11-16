# GoldSrc light styles for the map pack ("LSTY" section, GSV3, optional; older loaders skip it).
#
# A BSP face has up to 4 light-style slots (dface_t.styles[4], 255 = unused); the lighting lump holds one w*h RGB luxel
# block per used slot, back to back from lightofs. The base pack bakes slot 0 only (main lightmap atlas, treated as style
# 0). LSTY adds, for every vertex of a "styled" face (slot 0 style != 0, or any slot 1..3 used), the 4 style ids, and a
# second atlas ("style atlas") with the blocks of slots 1..3. Slot 0 of a styled face stays in the main atlas and is
# scaled by the runtime intensity of its style id (so a face lit only by a flicker style, e.g. style 10, flickers).
#
#   section blob (little endian, everything 4-byte aligned):
#     int32 SW, int32 SH      style atlas size (RGBA32, A = 255, 128 = 1.0 like the main lightmap), 0 x 0 = no extra blocks
#     int32 V                 vertex count (= the pack's vertex count; world + brush models, same order as pos)
#     int32 reserved 0
#     uint8 atlas [SW*SH*4]
#     per vertex V x 16 bytes: float32 u, float32 v, float32 stride, uint8 id[4]
#       u, v   style atlas UV of the vertex for slot 1 (the face's first extra block)
#       stride u offset to the next extra slot's block (slot k uses u + (k-1) * stride); 0 for a face without extras
#       id[k]  light style of slot k, 255 = slot unused. A plain vertex is (0, 0, 0, 0, 255, 255, 255)
#   The runtime evaluates 64 style intensities (0..11 patterns of CWorld, 12 "mmnnmmnnnmmnn", 32..62 switchable lights,
#   63 "a") and blends  light = main(uv1) * I[id0] + style(uv) * I[id1] + style(uv + stride) * I[id2] + ...
#
# The loader (GsvrPackLoader) packs u, v, stride + the 4 ids (4 x 6 bits of id + 1, ids >= 63 = unused) into ONE float4 mesh channel.
# Intensity of a pattern letter c at frame f = int(time * 10): (c - 'a') / 12, i.e. 'm' = 1.0 (the engine's (c - 'a') * 22
# / 256 gives 'm' = 1.03; style 0 must stay exactly 1.0 so a map without styles renders as before).
import struct

NONE = 255
STYLE_COUNT = 64
# CWorld::Precache pfnLightStyle calls (decomp/valve/evidence/hl_so_cond/CWorld.c): style -> pattern
PATTERNS = {
    0: "m",
    1: "mmnmmommommnonmmonqnmmo",
    2: "abcdefghijklmnopqrstuvwxyzyxwvutsrqponmlkjihgfedcba",
    3: "mmmmmaaaaammmmmaaaaaabcdefgabcdefg",
    4: "mamamamamama",
    5: "jklmnopqrstuvwxyzyxwvutsrqponmlkj",
    6: "nmonqnmomnmomomno",
    7: "mmmaaaabcdefgmmmmaaaammmaamm",
    8: "mmmaaammmaaammmabcdefaaaammmmabcdefmmmaaaa",
    9: "aaaaaaaazzzzzzzz",
    10: "mmamammmmammamamaaamammma",
    11: "abcdefghijklmnopqrrqponmlkjihgfedcba",
    12: "mmnnmmnnnmmnn",
    63: "a",
}
LM_MAX = 2048


def face_ids(face):
    """dface_t styles[4] → tuple of 4 ints (255 = unused)."""
    return tuple(face[5:9])


def is_plain(ids):
    return ids[0] in (0, NONE) and all(i == NONE for i in ids[1:])


def extra_blocks(b, face, w, h):
    """RGB blocks (bytes) of slots 1..3 of a face whose slot-0 block is w x h luxels at lightofs."""
    ids, ofs = face_ids(face), face[9]
    out = []
    if ofs < 0 or ids[0] == NONE:
        return out
    n = w * h * 3
    for k in (1, 2, 3):
        if ids[k] == NONE:
            break
        o = ofs + k * n
        if o + n > len(b.lighting):
            break
        out.append(bytes(b.lighting[o:o + n]))
    return out


def layout(infos, pick_atlas, halve_block, max_side=LM_MAX):
    """infos: per face None or (w, h, [rgb...]) -> (SW, SH, rgba bytes, [(x, y, bw) or None per face], step).
    Each face's n extra blocks sit side by side, every block with a 1 texel clamp border (bw = block width + 2).
    Full resolution first; when that does not fit one max_side page the blocks are halved (step 2)."""
    for step in (1, 2):
        sizes, idx = [], []
        for i, inf in enumerate(infos):
            if inf is None or not inf[2]:
                continue
            w, h, blocks = inf
            bw = (w + step - 1) // step + 2
            sizes.append((bw * len(blocks), (h + step - 1) // step + 2))
            idx.append(i)
        if not sizes:
            return 0, 0, b"", [None] * len(infos), 1
        res = pick_atlas(sizes, max_side, 4)
        if res is not None:
            break
    else:
        raise ValueError(f"light style atlas does not fit {max_side}^2 even at half resolution")      # the caller skips LSTY, never aborts the map
    SW, SH, pos = res
    img = bytearray(bytes((128, 128, 128, 255)) * (SW * SH))
    where = [None] * len(infos)
    for (x, y), i, (bwn, bh) in zip(pos, idx, sizes):
        w, h, blocks = infos[i]
        ww, hh = w, h
        bw = bwn // len(blocks)
        for e, rgb in enumerate(blocks):
            if step == 2:
                ww, hh, rgb = halve_block(w, h, rgb)
            rows = []
            for r in range(hh):
                line = bytearray()
                for c in range(ww):
                    line += rgb[(r * ww + c) * 3:(r * ww + c) * 3 + 3] + b"\xff"
                rows.append(line[:4] + line + line[-4:])
            rows = [rows[0]] + rows + [rows[-1]]
            for r, line in enumerate(rows):
                d = ((y + r) * SW + x + e * bw) * 4
                img[d:d + len(line)] = line
        where[i] = (x, y, bw)
    return SW, SH, bytes(img), where, step


def vertex_uv(where, step, ss, tt, ms, mt, SW, SH):
    """Style atlas (u, v, stride) of texture-space point (ss, tt); same luxel centre rule as pack.lightmap_uv."""
    x, y, bw = where
    return ((x + 1 + (ss / 16.0 - ms + 0.5) / step) / SW, (y + 1 + (tt / 16.0 - mt + 0.5) / step) / SH, bw / SW)


def section(SW, SH, atlas, verts):
    """verts: list of (u, v, stride, (id0, id1, id2, id3)) per pack vertex -> blob."""
    assert len(atlas) == SW * SH * 4
    out = bytearray(struct.pack("<4i", SW, SH, len(verts), 0))
    out += atlas
    for u, v, st, ids in verts:
        # the loader packs the 4 ids as 4 x 6 bits of (id + 1) into one float: ids 0..62; 63 (engine "a", unused by the HL / OF maps) counts as unused
        out += struct.pack("<3f4B", u, v, st, *(i if i < 63 else NONE for i in ids))
    return bytes(out)


def parse(blob):
    """Inverse of section -> (SW, SH, atlas bytes, verts)."""
    SW, SH, V, _r = struct.unpack_from("<4i", blob, 0)
    o = 16
    atlas = blob[o:o + SW * SH * 4]
    o += SW * SH * 4
    verts = []
    for k in range(V):
        u, v, st, a, b, c, d = struct.unpack_from("<3f4B", blob, o + 16 * k)
        verts.append((u, v, st, (a, b, c, d)))
    return SW, SH, atlas, verts


def intensity(style_pattern, frame):
    """Brightness of a pattern at a 10 Hz frame (reference for the Udon evaluator): (letter - 'a') / 12."""
    if not style_pattern:
        return 1.0
    return (ord(style_pattern[frame % len(style_pattern)]) - 97) / 12.0
