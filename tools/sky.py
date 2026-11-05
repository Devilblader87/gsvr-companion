"""GoldSrc skybox → RGBA faces for the GSV3 "SKY6" section (tools/pack.py).

worldspawn "skyname" (engine default "desert" when the key is missing) → <mod>/gfx/env/<name>{rt,bk,lf,ft,up,dn}.tga
(or .bmp; then valve/gfx/env as fallback). Stdlib only: TGA types 2 (true colour) and 10 (RLE), 24/32 bit, both
origins; BMP 8-bit palettized and 24/32-bit, bottom-up and top-down. Faces are box-filtered down to ≤ max_side and
returned top row first, RGBA, in GoldSrc order rt bk lf ft up dn.
SKY6 layout: int32 size (square edge), int32 0, then 6 × size·size·4 bytes RGBA, rows bottom → top (Unity
Texture2D raw order).
"""
import os, re, struct

SUFFIX = ("rt", "bk", "lf", "ft", "up", "dn")


def read_tga(d):
    idlen, cmtype, itype = d[0], d[1], d[2]
    w, h, bpp, desc = struct.unpack_from("<HHBB", d, 12)
    if cmtype != 0 or itype not in (2, 10) or bpp not in (24, 32):
        raise ValueError(f"unsupported TGA (type {itype}, colour map {cmtype}, {bpp} bpp)")
    px = bpp // 8
    o = 18 + idlen
    n = w * h
    raw = bytearray()
    if itype == 2:
        raw = d[o:o + n * px]
    else:
        while len(raw) < n * px:
            c = d[o]; o += 1
            k = (c & 0x7F) + 1
            if c & 0x80:
                raw += d[o:o + px] * k; o += px
            else:
                raw += d[o:o + k * px]; o += k * px
    rows = []
    for y in range(h):
        row = bytearray()
        for x in range(w):
            i = (y * w + x) * px
            b, g, r = raw[i], raw[i + 1], raw[i + 2]
            row += bytes((r, g, b, 255))
        rows.append(bytes(row))
    if not desc & 0x20:            # origin bottom-left → flip to top row first
        rows.reverse()
    return w, h, b"".join(rows)


def read_bmp(d):
    if d[:2] != b"BM":
        raise ValueError("not a BMP")
    off = struct.unpack_from("<I", d, 10)[0]
    hs, w, h, _pl, bpp = struct.unpack_from("<IiiHH", d, 14)
    top_down = h < 0
    h = abs(h)
    pal = []
    if bpp == 8:
        ncol = struct.unpack_from("<I", d, 46)[0] or 256
        p = 14 + hs
        pal = [(d[p + 4 * i + 2], d[p + 4 * i + 1], d[p + 4 * i]) for i in range(ncol)]
    elif bpp not in (24, 32):
        raise ValueError(f"unsupported BMP {bpp} bpp")
    stride = ((w * bpp + 31) // 32) * 4
    rows = []
    for y in range(h):
        o = off + y * stride
        row = bytearray()
        for x in range(w):
            if bpp == 8:
                r, g, b = pal[d[o + x]]
            else:
                i = o + x * (bpp // 8)
                b, g, r = d[i], d[i + 1], d[i + 2]
            row += bytes((r, g, b, 255))
        rows.append(bytes(row))
    if not top_down:
        rows.reverse()
    return w, h, b"".join(rows)


def resize_box(w, h, rgba, side):
    """Nearest / box sample to side × side (sky faces are square, usually 256)."""
    if w == side and h == side:
        return rgba
    out = bytearray(side * side * 4)
    for y in range(side):
        sy = min(h - 1, y * h // side)
        for x in range(side):
            sx = min(w - 1, x * w // side)
            i = (sy * w + sx) * 4
            o = (y * side + x) * 4
            out[o:o + 4] = rgba[i:i + 4]
    return bytes(out)


def sky_name(entity_text):
    m = re.search(r'"skyname"\s+"([^"]*)"', entity_text or "")
    return (m.group(1).strip() if m else "") or "desert"


def load(entity_text, game_root, mod, max_side=256, log=print):
    """→ (name, side, [6 × rgba bytes]) or None when a face is missing."""
    name = sky_name(entity_text)
    from gamedirs import content_dirs
    dirs = [os.path.join(root, "gfx", "env") for root in content_dirs(game_root, mod)]
    faces = []
    side = None
    for suf in SUFFIX:
        img = None
        for dd in dirs:
            for ext, reader in ((".tga", read_tga), (".bmp", read_bmp)):
                p = os.path.join(dd, name + suf + ext)
                if os.path.isfile(p):
                    try:
                        img = reader(open(p, "rb").read())
                    except (ValueError, IndexError, struct.error) as e:
                        log(f"sky {p}: {e}")
                        img = None
                    if img:
                        break
            if img:
                break
        if not img:
            log(f"sky '{name}': face {suf} missing - no skybox")
            return None
        w, h, rgba = img
        s = min(max_side, max(w, h))
        if side is None:
            side = s
        faces.append(resize_box(w, h, rgba, side))
    log(f"sky '{name}': 6 faces {side}x{side}")
    return name, side, faces


def section(sky):
    """SKY6 blob (see module doc)."""
    _name, side, faces = sky
    row = side * 4
    flipped = [b"".join(f[(side - 1 - y) * row:(side - y) * row] for y in range(side)) for f in faces]
    return struct.pack("<2i", side, 0) + b"".join(flipped)
