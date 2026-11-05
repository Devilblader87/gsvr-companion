# GoldSrc sprite (.spr, "IDSP" version 2) reader → RGBA frames. Stdlib only (runs inside the companion).
# usage: python3 -I tools/spr.py check <sprites dir or .spr files...>   decode every sprite, print sizes, exit 1 on errors
#        python3 -I tools/spr.py info <file.spr>                         header + per-frame table
#        python3 -I tools/spr.py png <file.spr> <out.png> [--bg R,G,B] [--scale N]   all frames side by side
#        python3 -I tools/spr.py pack <models.bin> [--game-root DIR] [--mod valve|cstrike|svencoop] [--png atlas.png]   verify the GSX1/SPR1 sprite
#                chunk of a GSM1 pack (tools/modelpack.py): layout, every frame == the .spr decode, optional atlas PNG
#
# ── SPR v2 layout (little endian) ──
#    0  char[4] "IDSP"      4  int32 version (2 = GoldSrc; 1 = Quake, no palette — not supported)
#    8  int32 type          orientation: 0 VP_PARALLEL_UPRIGHT 1 FACING_UPRIGHT 2 VP_PARALLEL 3 ORIENTED
#                           4 VP_PARALLEL_ORIENTED
#   12  int32 texFormat     0 SPR_NORMAL 1 SPR_ADDITIVE 2 SPR_INDEXALPHA 3 SPR_ALPHTEST
#   16  float boundingRadius  20 int32 width  24 int32 height (max frame size)  28 int32 numFrames
#   32  float beamLength    36 int32 syncType (0 sync, 1 random)
#   40  int16 paletteColors (256) + paletteColors*3 bytes RGB
#   frames: numFrames × { int32 group; group 0 → one frame; else int32 n, float32 intervals[n], n frames }
#   frame: int32 originX, originY (offset of the top-left pixel from the sprite origin: x right, y UP, so a
#          centred 64×64 frame has origin (-32, 32)), int32 w, h, then w*h palette indices, rows top → bottom.
#
# RGBA conversion (what the GoldSrc GL renderer uploads per texFormat [inf: engine, not in the game DLLs]):
#   NORMAL     rgb = palette[i], a = 255
#   ADDITIVE   rgb = palette[i], a = 255 (drawn with additive blending; black = no contribution)
#   INDEXALPHA rgb = palette[255] (the sprite's single colour), a = i (index = coverage, 0 transparent … 255 opaque)
#   ALPHTEST   index 255 → (0, 0, 0, 0), else rgb = palette[i], a = 255
# Frame groups are flattened (their intervals are kept in Frame.interval, 0 for single frames).
import os, struct, sys, zlib

TYPES = ("vp_parallel_upright", "facing_upright", "vp_parallel", "oriented", "vp_parallel_oriented")
FORMATS = ("normal", "additive", "indexalpha", "alphatest")
SPR_NORMAL, SPR_ADDITIVE, SPR_INDEXALPHA, SPR_ALPHTEST = range(4)


class Frame:
    def __init__(self, ox, oy, w, h, rgba, interval=0.0):
        self.ox, self.oy, self.w, self.h, self.rgba, self.interval = ox, oy, w, h, rgba, interval


class Sprite:
    """Parsed sprite: header fields + frames (RGBA bytes, rows top → bottom)."""

    def __init__(self, data, name="?"):
        self.name = name
        if len(data) < 42 or data[:4] != b"IDSP":
            raise ValueError(f"{name}: not an IDSP sprite")
        (self.version, self.type, self.tex_format, self.radius, self.width, self.height, self.num_frames,
         self.beam_length, self.sync_type) = struct.unpack_from("<iiifiiifi", data, 4)
        if self.version != 2:
            raise ValueError(f"{name}: sprite version {self.version} (only GoldSrc v2 is supported)")
        if not 0 <= self.tex_format < 4 or not 0 <= self.type < 5:
            raise ValueError(f"{name}: bad type {self.type} / texFormat {self.tex_format}")
        if not 0 < self.num_frames <= 4096:
            raise ValueError(f"{name}: bad frame count {self.num_frames}")
        ncol = struct.unpack_from("<h", data, 40)[0]
        if not 0 < ncol <= 256:
            raise ValueError(f"{name}: bad palette size {ncol}")
        pal = data[42:42 + ncol * 3]
        if len(pal) != ncol * 3:
            raise ValueError(f"{name}: truncated palette")
        pal = pal + bytes(3 * (256 - ncol))
        self.palette = pal
        self.frames = []
        off = 42 + ncol * 3
        for _ in range(self.num_frames):
            group = struct.unpack_from("<i", data, off)[0]
            off += 4
            if group == 0:
                f, off = self._frame(data, off, 0.0)
                self.frames.append(f)
            else:
                n = struct.unpack_from("<i", data, off)[0]
                if not 0 < n <= 1024:
                    raise ValueError(f"{name}: bad group size {n}")
                iv = struct.unpack_from(f"<{n}f", data, off + 4)
                off += 4 + 4 * n
                for k in range(n):
                    f, off = self._frame(data, off, iv[k])
                    self.frames.append(f)
        self.trailing = len(data) - off

    def _frame(self, data, off, interval):
        if off + 16 > len(data):
            raise ValueError(f"{self.name}: truncated frame header")
        ox, oy, w, h = struct.unpack_from("<iiii", data, off)
        off += 16
        if not (0 < w <= 4096 and 0 < h <= 4096) or off + w * h > len(data):
            raise ValueError(f"{self.name}: bad frame {w}x{h}")
        idx = data[off:off + w * h]
        return Frame(ox, oy, w, h, to_rgba(idx, self.palette, self.tex_format), interval), off + w * h


def to_rgba(idx, pal, fmt):
    """Palette indices → RGBA bytes per GoldSrc texFormat (see header)."""
    n = len(idx)
    out = bytearray(n * 4)
    if fmt == SPR_INDEXALPHA:
        r, g, b = pal[765], pal[766], pal[767]
        out[0::4] = bytes([r]) * n
        out[1::4] = bytes([g]) * n
        out[2::4] = bytes([b]) * n
        out[3::4] = idx
        return bytes(out)
    lut = [bytes((pal[3 * i], pal[3 * i + 1], pal[3 * i + 2], 255)) for i in range(256)]
    if fmt == SPR_ALPHTEST:
        lut[255] = b"\0\0\0\0"
    return b"".join(lut[i] for i in idx)


def load(path):
    with open(path, "rb") as fh:
        return Sprite(fh.read(), os.path.basename(path))


# ───────────────────────── GSM1 extension: sprites (used by tools/modelpack.py) ─────────────────────────
# GSX1 extension block = chunks appended at the END of the GSM1 names region (header nameBytes counts them, so
# loaders that predate the block still validate the pack size and ignore it):
#   chunk  = char[4] tag, int32 payloadBytes (multiple of 4), payload
#   trailer (last 8 bytes of the names region) = int32 blockBytes (all chunks + these 8 bytes), char[4] "GSX1"
# "SPR1" payload (all int32 unless noted):
#   [8]  spriteCount, cellCount, atlasW, atlasH, mips, cellPx, cols, 0
#   [spriteCount*8]  nameOff, nameLen (into the GSM1 names table, e.g. "sprites/gas_puff_01.spr"), type, texFormat,
#                    frameCount, cellFirst (frame k = cell cellFirst + k), width, height (header max size)
#   [cellCount*4]    originX, originY, w, h of each frame (pixels; origin as in the .spr file)
#   uint8 atlas RGBA32, mip levels 0..mips-1 concatenated (level l = (atlasW>>l) x (atlasH>>l)), rows top → bottom
#         (Unity raw layout: v = row / atlasH). Cell c sits at column c % cols, row c // cols, cellPx square; the
#         frame is centred in it at ((cellPx - w) // 2, (cellPx - h) // 2), the rest is its edge-extended gutter.
SPR1_ATLAS_W = 1024


def spr1_chunk(sprites, name, mips, half):
    """sprites: [(rel name, Sprite)], name(str) → (off, len) in the GSM1 names table, half = 2x2 box filter."""
    frames = [f for _n, s in sprites for f in s.frames]
    cell = -(-max(max(f.w, f.h) for f in frames) // 8) * 8 + 8
    cols = max(1, SPR1_ATLAS_W // cell)
    aw = cols * cell
    ah = -(-len(frames) // cols) * cell
    img = bytearray(aw * ah * 4)
    for c, f in enumerate(frames):
        x0, y0 = (c % cols) * cell, (c // cols) * cell
        px, py = (cell - f.w) // 2, (cell - f.h) // 2
        for y in range(cell):
            sy = min(f.h - 1, max(0, y - py))
            row = f.rgba[sy * f.w * 4:(sy + 1) * f.w * 4]
            left, right = row[:4] * px, row[-4:] * (cell - px - f.w)
            o = ((y0 + y) * aw + x0) * 4
            img[o:o + cell * 4] = left + row + right
    levels, lw, lh, cur = [bytes(img)], aw, ah, bytes(img)
    for _ in range(1, mips):
        cur = half(cur, lw, lh)
        lw, lh = lw // 2, lh // 2
        levels.append(cur)
    hdr = struct.pack("<8i", len(sprites), len(frames), aw, ah, mips, cell, cols, 0)
    stab, ftab, first = b"", b"", 0
    for rel, s in sprites:
        no, nl = name(rel)
        stab += struct.pack("<8i", no, nl, s.type, s.tex_format, len(s.frames), first, s.width, s.height)
        first += len(s.frames)
    for f in frames:
        ftab += struct.pack("<4i", f.ox, f.oy, f.w, f.h)
    payload = hdr + stab + ftab + b"".join(levels)
    payload += b"\0" * (-len(payload) % 4)
    return b"SPR1" + struct.pack("<i", len(payload)) + payload, (aw, ah, cell, len(frames))


def read_gsm1_sprites(data):
    """GSM1 pack bytes → (names, [sprite dicts], atlas (w, h, mips, cellPx, cols, level-0 RGBA)) or None without
    SPR1. Mirrors GsvrModelLoader's GSX1 scan: names region from the header sizes, trailer at its end."""
    if data[:4] != b"GSM1":
        raise ValueError("not a GSM1 pack")
    h = struct.unpack_from("<19i", data, 4)
    hdr, ver, W, M, S, E, U, I, P, N, AW, AH, mips, nb = h[:14]
    atlas = sum((AW >> l) * (AH >> l) * 4 for l in range(mips))
    off = hdr + 4 * (W * 32 + M * 8 + S * 6 + E * 4 + I + U * 2 + P * 6 + N) + atlas
    names = data[off:off + nb]
    need = off + nb + (4 * (h[15] * 8 + h[16] * 8 + h[17] * 8 + W * 4) if ver == 2 else 0)   # v2: Q, A, H
    if need != len(data):
        raise ValueError(f"GSM1 size mismatch: header needs {need}, file {len(data)}")
    if nb < 8 or names[-4:] != b"GSX1":
        return names, None, None
    blk = struct.unpack_from("<i", names, nb - 8)[0]
    if blk < 8 or blk > nb or blk % 4:
        raise ValueError(f"bad GSX1 block size {blk}")
    p, end = nb - blk, nb - 8
    while p + 8 <= end:
        tag, size = names[p:p + 4], struct.unpack_from("<i", names, p + 4)[0]
        if size < 0 or size % 4 or p + 8 + size > end:
            raise ValueError(f"bad chunk {tag!r} size {size}")
        if tag == b"SPR1":
            q = p + 8
            n, cells, aw, ah, smips, cell, cols, _r = struct.unpack_from("<8i", names, q)
            q += 32
            sprites = []
            for k in range(n):
                no, nl, typ, fmt, nf, first, w, hh = struct.unpack_from("<8i", names, q + 32 * k)
                sprites.append(dict(name=names[no:no + nl].decode("ascii"), type=typ, fmt=fmt, frames=nf,
                                    first=first, w=w, h=hh))
            q += 32 * n
            fr = [struct.unpack_from("<4i", names, q + 16 * c) for c in range(cells)]
            q += 16 * cells
            lvl0 = names[q:q + aw * ah * 4]
            total = sum((aw >> l) * (ah >> l) * 4 for l in range(smips))
            if q + total > p + 8 + size:
                raise ValueError("SPR1 atlas truncated")
            for s in sprites:
                s["rects"] = fr[s["first"]:s["first"] + s["frames"]]
            return names, sprites, (aw, ah, smips, cell, cols, lvl0)
        p += 8 + size
    return names, None, None


# ── "SPR2" chunk: effect sprites (explosions, muzzle flashes, blood, beams, glows, map sprites) ──
# The SPR1 grid wastes a lot with mixed frame sizes (16x16 blood next to 96x96 explosions), so SPR2 shelf-packs the
# frames. Own atlas texture (SPR1 keeps the smoke grenade sprites in the grid the GsvrSprite shader indexes).
#   [8]  spriteCount, frameCount, atlasW, atlasH, mips, 0, 0, 0
#   [spriteCount*8]  nameOff, nameLen, type, texFormat, frameCount, firstFrame, width, height   (as SPR1; frame k of a
#                    sprite is frame record firstFrame + k)
#   [frameCount*8]   originX, originY, w, h, atlasX, atlasY, 0, 0   (atlasX/Y = top-left pixel of the frame, rows top ->
#                    bottom; both are multiples of 4, so mips <= 3 never mix two frames; the 2+ px around the frame is its
#                    edge-extended gutter)
#   uint8 atlas RGBA32, mip levels 0..mips-1 concatenated (level l = (atlasW>>l) x (atlasH>>l)), rows top -> bottom
SPR2_ATLAS_W = 1024
SPR2_MIPS = 3


def spr2_slot(f):
    """Slot size (w, h) of a frame: >= 2 px gutter per side, rounded up to a multiple of 4."""
    return -(-(f.w + 4) // 4) * 4, -(-(f.h + 4) // 4) * 4


def spr2_layout(frames, atlas_w=SPR2_ATLAS_W):
    """Shelf packing, tallest frames first. frames: [Frame] -> ([(slot x, slot y)] in input order, atlas height).
    The atlas height is a multiple of 8 (mip chain)."""
    order = sorted(range(len(frames)), key=lambda i: (-spr2_slot(frames[i])[1], -spr2_slot(frames[i])[0], i))
    pos = [None] * len(frames)
    x = y = shelf = 0
    for i in order:
        sw, sh = spr2_slot(frames[i])
        if sw > atlas_w:
            raise ValueError("frame wider than the atlas")
        if x + sw > atlas_w:
            x, y, shelf = 0, y + shelf, 0
        if shelf == 0:
            shelf = sh
        pos[i] = (x, y)
        x += sw
    ah = -(-(y + shelf) // 8) * 8
    return pos, ah


def spr2_select(candidates, max_texels):
    """candidates: [(rel name, Sprite)] in priority order -> (kept, dropped names). Strict priority: the kept sprites are the
    longest prefix whose shelf-packed atlas fits max_texels; the first sprite that does not fit and everything after it is
    dropped (lowest priority first). Sprites with screen-sized frames are skipped without ending the prefix."""
    kept, dropped, frames = [], [], []
    full = False
    for rel, s in candidates:
        if any(spr2_slot(f)[0] > SPR2_ATLAS_W or spr2_slot(f)[1] > 512 for f in s.frames):
            dropped.append(rel)          # screen-sized sprites (logos, HUD art) do not belong in the effect atlas
            continue
        if full:
            dropped.append(rel)
            continue
        trial = frames + s.frames
        _pos, ah = spr2_layout(trial)
        if SPR2_ATLAS_W * ah <= max_texels:
            kept.append((rel, s))
            frames = trial
        else:
            dropped.append(rel)
            full = True                  # strict priority: nothing after the first sprite that does not fit
    return kept, dropped


def spr2_chunk(sprites, name, mips, half):
    """sprites: [(rel name, Sprite)], name(str) -> (off, len) in the GSM1 names table, half = 2x2 box filter.
    Returns (chunk bytes, (atlasW, atlasH, frames))."""
    frames = [f for _n, s in sprites for f in s.frames]
    pos, ah = spr2_layout(frames)
    aw = SPR2_ATLAS_W
    img = bytearray(aw * ah * 4)
    ftab = b""
    for f, (sx, sy) in zip(frames, pos):
        sw, sh = spr2_slot(f)
        px, py = (sw - f.w) // 2, (sh - f.h) // 2
        for y in range(sh):
            srow = min(f.h - 1, max(0, y - py))
            row = f.rgba[srow * f.w * 4:(srow + 1) * f.w * 4]
            line = row[:4] * px + row + row[-4:] * (sw - px - f.w)
            o = ((sy + y) * aw + sx) * 4
            img[o:o + sw * 4] = line
        ftab += struct.pack("<8i", f.ox, f.oy, f.w, f.h, sx + px, sy + py, 0, 0)
    levels, lw, lh, cur = [bytes(img)], aw, ah, bytes(img)
    for _ in range(1, mips):
        cur = half(cur, lw, lh)
        lw, lh = lw // 2, lh // 2
        levels.append(cur)
    hdr = struct.pack("<8i", len(sprites), len(frames), aw, ah, mips, 0, 0, 0)
    stab, first = b"", 0
    for rel, s in sprites:
        no, nl = name(rel)
        stab += struct.pack("<8i", no, nl, s.type, s.tex_format, len(s.frames), first, s.width, s.height)
        first += len(s.frames)
    payload = hdr + stab + ftab + b"".join(levels)
    payload += b"\0" * (-len(payload) % 4)
    return b"SPR2" + struct.pack("<i", len(payload)) + payload, (aw, ah, len(frames))


def gsx1_chunks(data):
    """GSM1 pack bytes -> (names region bytes, {tag: (payload offset in names, size)}); {} without a GSX1 block.
    Same scan as GsvrModelLoader.DecodeExt."""
    if data[:4] != b"GSM1":
        raise ValueError("not a GSM1 pack")
    h = struct.unpack_from("<19i", data, 4)
    hdr, ver, W, M, S, E, U, I, P, N, AW, AH, mips, nb = h[:14]
    atlas = sum((AW >> l) * (AH >> l) * 4 for l in range(mips))
    off = hdr + 4 * (W * 32 + M * 8 + S * 6 + E * 4 + I + U * 2 + P * 6 + N) + atlas
    names = data[off:off + nb]
    if nb < 8 or names[-4:] != b"GSX1":
        return names, {}
    blk = struct.unpack_from("<i", names, nb - 8)[0]
    if blk < 8 or blk > nb or blk % 4:
        raise ValueError(f"bad GSX1 block size {blk}")
    p, end, out = nb - blk, nb - 8, {}
    while p + 8 <= end:
        tag, size = names[p:p + 4], struct.unpack_from("<i", names, p + 4)[0]
        if size < 0 or size % 4 or p + 8 + size > end:
            raise ValueError(f"bad chunk {tag!r} size {size}")
        out[tag.decode("latin-1")] = (p + 8, size)
        p += 8 + size
    return names, out


def parse_spr2(names, q, size):
    """SPR2 payload at names[q:q+size] -> ([sprite dicts with 'rects' = [(ox, oy, w, h, ax, ay)]], (aw, ah, mips, level-0 RGBA))."""
    n, cells, aw, ah, smips, _a, _b, _c = struct.unpack_from("<8i", names, q)
    if n <= 0 or cells <= 0 or aw < 8 or ah < 8 or not 1 <= smips <= 4:
        raise ValueError("SPR2 header invalid")
    total = sum((aw >> l) * (ah >> l) * 4 for l in range(smips))
    if 32 + n * 32 + cells * 32 + total > size:
        raise ValueError("SPR2 truncated")
    sprites = []
    for k in range(n):
        no, nl, typ, fmt, nf, first, w, hh = struct.unpack_from("<8i", names, q + 32 + 32 * k)
        if nf <= 0 or first < 0 or first + nf > cells:
            raise ValueError("SPR2 sprite out of range")
        sprites.append(dict(name=names[no:no + nl].decode("ascii"), type=typ, fmt=fmt, frames=nf, first=first, w=w, h=hh))
    q2 = q + 32 + 32 * n
    fr = [struct.unpack_from("<6i", names, q2 + 32 * c) for c in range(cells)]
    for f in fr:
        if f[2] <= 0 or f[3] <= 0 or f[4] < 2 or f[5] < 2 or f[4] + f[2] > aw or f[5] + f[3] > ah:
            raise ValueError("SPR2 frame outside the atlas")
    q2 += 32 * cells
    for s in sprites:
        s["rects"] = fr[s["first"]:s["first"] + s["frames"]]
    return sprites, (aw, ah, smips, names[q2:q2 + aw * ah * 4])


def gsx1_block(chunks):
    """Extension chunks → the GSX1 block for the end of the names region (empty input → b"")."""
    if not chunks:
        return b""
    body = b"".join(chunks)
    return body + struct.pack("<i", len(body) + 8) + b"GSX1"


# ───────────────────────── PNG (stdlib) ─────────────────────────

def write_png(path, w, h, rgba):
    """8-bit RGBA PNG, rows top → bottom."""
    raw = b"".join(b"\0" + rgba[y * w * 4:(y + 1) * w * 4] for y in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
                 + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def sheet(spr, bg=(48, 48, 56), scale=1, pad=4):
    """All frames side by side, alpha-composited over bg (premultiplied preview) → (w, h, rgba)."""
    cw, ch = max(f.w for f in spr.frames), max(f.h for f in spr.frames)
    n = len(spr.frames)
    cols = min(n, 10)
    rows = (n + cols - 1) // cols
    W, H = (cols * (cw + pad) + pad) * scale, (rows * (ch + pad) + pad) * scale
    img = bytearray(bytes((bg[0], bg[1], bg[2], 255)) * (W * H))
    for k, f in enumerate(spr.frames):
        x0 = (pad + (k % cols) * (cw + pad) + (cw - f.w) // 2) * scale
        y0 = (pad + (k // cols) * (ch + pad) + (ch - f.h) // 2) * scale
        for y in range(f.h * scale):
            for x in range(f.w * scale):
                s = ((y // scale) * f.w + x // scale) * 4
                r, g, b, a = f.rgba[s:s + 4]
                if spr.tex_format == SPR_ADDITIVE:
                    a = 255
                o = ((y0 + y) * W + x0 + x) * 4
                for c, v in enumerate((r, g, b)):
                    if spr.tex_format == SPR_ADDITIVE:
                        img[o + c] = min(255, img[o + c] + v)
                    else:
                        img[o + c] = (v * a + img[o + c] * (255 - a) + 127) // 255
    return W, H, bytes(img)


# ───────────────────────── CLI ─────────────────────────

def describe(spr):
    sizes = sorted({(f.w, f.h) for f in spr.frames})
    return (f"{spr.name}: {TYPES[spr.type]}, {FORMATS[spr.tex_format]}, {len(spr.frames)} frame(s) "
            f"(header {spr.num_frames}), max {spr.width}x{spr.height}, sizes {', '.join(f'{w}x{h}' for w, h in sizes)}"
            f"{', palette[255] #%02x%02x%02x' % tuple(spr.palette[765:768]) if spr.tex_format == SPR_INDEXALPHA else ''}")


def check(paths):
    files = []
    for p in paths:
        if os.path.isdir(p):
            for d, _sub, names in sorted(os.walk(p)):
                files += sorted(os.path.join(d, n) for n in names if n.lower().endswith(".spr"))
        else:
            files.append(p)
    bad = 0
    stats = {}
    for f in files:
        try:
            s = load(f)
            assert all(len(fr.rgba) == fr.w * fr.h * 4 for fr in s.frames)
            assert all(fr.w <= s.width and fr.h <= s.height for fr in s.frames), "frame larger than the header size"
            assert s.trailing == 0, f"{s.trailing} trailing bytes"
            stats[FORMATS[s.tex_format]] = stats.get(FORMATS[s.tex_format], 0) + 1
            print("  ok   " + describe(s))
        except (ValueError, AssertionError, struct.error) as e:
            bad += 1
            print(f"  FAIL {os.path.basename(f)}: {e}")
    print(f"{len(files) - bad}/{len(files)} sprites decoded ({', '.join(f'{k} {v}' for k, v in sorted(stats.items()))})")
    return 1 if bad or not files else 0


MODS = ("cstrike", "valve")        # reference search order of `pack --game-root` (--mod X puts X first, then valve)


def _find_ref(game_root, name):
    if not game_root:
        return None
    for mod in MODS:
        d = os.path.join(game_root, mod)
        f = os.path.join(d, name)
        if os.path.isfile(f):
            return load(f)
        cur = d       # case-insensitive fallback (names of SPR2 are lower-case)
        for part in name.split("/"):
            try:
                hit = [e for e in os.listdir(cur) if e.lower() == part.lower()]
            except OSError:
                hit = []
            if not hit:
                cur = None
                break
            cur = os.path.join(cur, hit[0])
        if cur and os.path.isfile(cur):
            return load(cur)
    return None


def check_spr2(data, game_root, png):
    """Verify the SPR2 chunk: layout rules and, with the game install, every frame == the .spr decode. Returns the
    number of problems, or None without SPR2."""
    names, chunks = gsx1_chunks(data)
    if "SPR2" not in chunks:
        return None
    q, size = chunks["SPR2"]
    sprites, (aw, ah, mips, lvl0) = parse_spr2(names, q, size)
    print(f"SPR2 {len(sprites)} sprites, {sum(s['frames'] for s in sprites)} frames, atlas {aw}x{ah}x{mips}")
    bad = 0
    used = bytearray(aw * ah)           # slot occupancy: no two frames may overlap
    for s in sprites:
        ref = _find_ref(game_root, s["name"])
        if ref is not None and (len(ref.frames) != s["frames"] or ref.type != s["type"] or ref.tex_format != s["fmt"]):
            print(f"  FAIL {s['name']}: header differs from the .spr"); bad += 1; continue
        for k, (ox, oy, w, h, ax, ay) in enumerate(s["rects"]):
            if ax < 2 or ay < 2 or ax + w + 2 > aw or ay + h + 2 > ah:
                print(f"  FAIL {s['name']} frame {k}: gutter outside the atlas"); bad += 1; continue
            for y in range(ay - 2, ay + h + 2):
                row = used[y * aw + ax - 2:y * aw + ax + w + 2]
                if any(row):
                    print(f"  FAIL {s['name']} frame {k}: overlaps another frame"); bad += 1; break
                used[y * aw + ax - 2:y * aw + ax + w + 2] = b"\1" * (w + 4)
            if ref is not None:
                fr = ref.frames[k]
                got = b"".join(lvl0[((ay + y) * aw + ax) * 4:((ay + y) * aw + ax + w) * 4] for y in range(h))
                if (fr.ox, fr.oy, fr.w, fr.h) != (ox, oy, w, h) or got != fr.rgba:
                    print(f"  FAIL {s['name']} frame {k}: differs from the .spr decode"); bad += 1
    if png:
        write_png(png, aw, ah, lvl0)
        print(f"  SPR2 atlas level 0 -> {png}")
    print("SPR2 OK" if not bad else f"SPR2: {bad} problem(s)")
    return bad


def check_pack(path, game_root, png):
    with open(path, "rb") as fh:
        data = fh.read()
    _names, sprites, atlas = read_gsm1_sprites(data)
    bad2 = check_spr2(data, game_root, png + ".spr2.png" if png else None)
    if sprites is None:
        if bad2 is None:
            print(f"{path}: no GSX1/SPR1 or SPR2 sprite chunk")
            return 1
        return 1 if bad2 else 0
    aw, ah, mips, cell, cols, lvl0 = atlas
    print(f"{path}: SPR1 {len(sprites)} sprites, atlas {aw}x{ah}x{mips}, {cell} px cells, {cols} columns")
    bad = bad2 or 0
    for s in sprites:
        print(f"  {s['name']}: {TYPES[s['type']]} {FORMATS[s['fmt']]} {s['frames']} frames from cell {s['first']}, "
              f"{s['w']}x{s['h']}")
        ref = _find_ref(game_root, s["name"])
        for k, (ox, oy, w, h) in enumerate(s["rects"]):
            c = s["first"] + k
            x0 = (c % cols) * cell + (cell - w) // 2
            y0 = (c // cols) * cell + (cell - h) // 2
            if x0 + w > aw or y0 + h > ah:
                print(f"    FAIL frame {k} outside the atlas"); bad += 1; continue
            if ref is not None:
                fr = ref.frames[k]
                got = b"".join(lvl0[((y0 + y) * aw + x0) * 4:((y0 + y) * aw + x0 + w) * 4] for y in range(h))
                if (fr.ox, fr.oy, fr.w, fr.h) != (ox, oy, w, h) or got != fr.rgba:
                    print(f"    FAIL frame {k} differs from the .spr decode"); bad += 1
        if ref is not None and len(ref.frames) != s["frames"]:
            print("    FAIL frame count differs"); bad += 1
    if png:
        write_png(png, aw, ah, lvl0)
        print(f"  atlas level 0 -> {png}")
    print("SPR1 OK" if not bad else f"SPR1/SPR2: {bad} problem(s)")
    return 1 if bad else 0


def main(argv=None):
    global MODS
    a = list(sys.argv[1:] if argv is None else argv)
    if not a or a[0] not in ("check", "info", "png", "pack"):
        print(__doc__ or "usage: spr.py check|info|png ...")
        print("usage: spr.py check <dir|files...> | info <file.spr> | png <file.spr> <out.png> [--bg R,G,B] [--scale N]")
        return 2
    if a[0] == "check":
        return check(a[1:])
    if a[0] == "pack":
        if "--mod" in a:
            m = a[a.index("--mod") + 1]
            MODS = (m, "valve") if m != "valve" else ("valve",)
        return check_pack(a[1], a[a.index("--game-root") + 1] if "--game-root" in a else None,
                          a[a.index("--png") + 1] if "--png" in a else None)
    s = load(a[1])
    if a[0] == "info":
        print(describe(s))
        for k, f in enumerate(s.frames):
            cov = sum(f.rgba[3::4]) / (255.0 * f.w * f.h)
            print(f"  frame {k}: origin ({f.ox}, {f.oy}) {f.w}x{f.h} interval {f.interval:g} mean alpha {cov:.3f}")
        return 0
    bg, scale = (48, 48, 56), 1
    if "--bg" in a:
        bg = tuple(int(v) for v in a[a.index("--bg") + 1].split(","))
    if "--scale" in a:
        scale = int(a[a.index("--scale") + 1])
    w, h, img = sheet(s, bg, scale)
    write_png(a[2], w, h, img)
    print(f"{a[2]}: {w}x{h}, {len(s.frames)} frame(s) of {describe(s)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
