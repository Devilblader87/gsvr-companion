"""DDS reader + DXT1/3/5 decode/encode helpers (stdlib only). Written from the public DDS container layout.

Used by rose_pack.py (to convert the player's textures into the formats Unity can load raw: DXT1, DXT5, RGBA32)
and by rose_pack_check.py (to turn packed textures back into PNG previews).

All pixel buffers are RGBA8, row 0 = TOP row (DDS order), bytes r,g,b,a.
"""
import struct

FMT_DXT1, FMT_DXT5, FMT_RGBA32 = 1, 2, 3          # pack texture format codes (see docs/RPG1_PACK.md)


def _rgb565(c):
    r = (c >> 11) & 31; g = (c >> 5) & 63; b = c & 31
    return ((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2))


def parse_dds(d):
    """-> dict(w, h, fmt in 'DXT1'|'DXT3'|'DXT5'|'RGBA', levels=[bytes...]); RGBA levels are converted to RGBA8 (top row first)."""
    if d[:4] != b'DDS ':
        raise ValueError('not a DDS file')
    hsize, flags, h, w, pitch, depth, mips = struct.unpack_from('<7I', d, 4)
    mips = max(1, mips) if flags & 0x20000 else 1
    pf = struct.unpack_from('<8I', d, 76)         # size, flags, fourcc, bits, rmask, gmask, bmask, amask
    pos = 4 + hsize
    levels = []
    if pf[1] & 4:                                 # DDPF_FOURCC
        cc = d[84:88]
        if cc not in (b'DXT1', b'DXT3', b'DXT5'):
            raise ValueError('unsupported fourcc %r' % cc)
        fmt = cc.decode(); bs = 8 if cc == b'DXT1' else 16
        lw, lh = w, h
        for _ in range(mips):
            n = max(1, (lw + 3) // 4) * max(1, (lh + 3) // 4) * bs
            if pos + n > len(d):
                break
            levels.append(bytes(d[pos:pos + n])); pos += n
            lw = max(1, lw // 2); lh = max(1, lh // 2)
    else:
        fmt = 'RGBA'
        bits, rm, gm, bm, am = pf[3], pf[4], pf[5], pf[6], pf[7]
        bpp = bits // 8
        if bits not in (16, 24, 32):
            raise ValueError('unsupported raw dds bits %d' % bits)
        lw, lh = w, h
        for _ in range(mips):
            n = lw * lh * bpp
            if pos + n > len(d):
                break
            out = bytearray(lw * lh * 4)
            src = d[pos:pos + n]
            for i in range(lw * lh):
                px = int.from_bytes(src[i * bpp:(i + 1) * bpp], 'little')
                o = i * 4
                for k, m in enumerate((rm, gm, bm)):
                    out[o + k] = _chan(px, m)
                out[o + 3] = _chan(px, am) if am else 255
            levels.append(bytes(out)); pos += n
            lw = max(1, lw // 2); lh = max(1, lh // 2)
    if not levels:
        raise ValueError('dds without data')
    return dict(w=w, h=h, fmt=fmt, levels=levels)


def _chan(px, mask):
    if not mask:
        return 0
    sh = (mask & -mask).bit_length() - 1
    v = (px & mask) >> sh
    bits = (mask >> sh).bit_length()
    if bits >= 8:
        return v >> (bits - 8)
    return (v * 255) // ((1 << bits) - 1)


def decode_dxt(fmt, data, w, h):
    """DXT1/3/5 -> RGBA8 (w*h*4). w,h need not be multiples of 4 (cropped)."""
    bw, bh = max(1, (w + 3) // 4), max(1, (h + 3) // 4)
    out = bytearray(w * h * 4)
    bs = 8 if fmt == 'DXT1' else 16
    for by in range(bh):
        for bx in range(bw):
            off = (by * bw + bx) * bs
            blk = data[off:off + bs]
            if fmt == 'DXT1':
                cb = blk
            else:
                cb = blk[8:16]
            c0, c1, bits = struct.unpack_from('<HHI', cb, 0)
            p0, p1 = _rgb565(c0), _rgb565(c1)
            if c0 > c1 or fmt != 'DXT1':
                pal = [p0 + (255,), p1 + (255,),
                       tuple((2 * a + b) // 3 for a, b in zip(p0, p1)) + (255,),
                       tuple((a + 2 * b) // 3 for a, b in zip(p0, p1)) + (255,)]
            else:
                pal = [p0 + (255,), p1 + (255,), tuple((a + b) // 2 for a, b in zip(p0, p1)) + (255,), (0, 0, 0, 0)]
            alpha = None
            if fmt == 'DXT3':
                alpha = [((blk[i >> 1] >> ((i & 1) * 4)) & 15) * 17 for i in range(16)]
            elif fmt == 'DXT5':
                alpha = _dxt5_alpha(blk[:8])
            for py in range(4):
                y = by * 4 + py
                if y >= h:
                    break
                for px in range(4):
                    x = bx * 4 + px
                    if x >= w:
                        break
                    i = py * 4 + px
                    r, g, b, a = pal[(bits >> (2 * i)) & 3]
                    if alpha is not None:
                        a = alpha[i]
                    o = (y * w + x) * 4
                    out[o] = r; out[o + 1] = g; out[o + 2] = b; out[o + 3] = a
    return bytes(out)


def _dxt5_alpha(b8):
    a0, a1 = b8[0], b8[1]
    if a0 > a1:
        tab = [a0, a1] + [((7 - k) * a0 + k * a1) // 7 for k in range(1, 7)]
    else:
        tab = [a0, a1] + [((5 - k) * a0 + k * a1) // 5 for k in range(1, 5)] + [0, 255]
    bits = int.from_bytes(b8[2:8], 'little')
    return [tab[(bits >> (3 * i)) & 7] for i in range(16)]


def _enc_alpha(al):
    """16 alpha values -> 8-byte DXT5 alpha block (8-value interpolation mode)."""
    hi, lo = max(al), min(al)
    if hi == lo:
        return bytes([hi, lo, 0, 0, 0, 0, 0, 0])
    tab = [hi, lo] + [((7 - k) * hi + k * lo) // 7 for k in range(1, 7)]
    bits = 0
    for i, a in enumerate(al):
        best = min(range(8), key=lambda j: abs(tab[j] - a))
        bits |= best << (3 * i)
    return bytes([hi, lo]) + bits.to_bytes(6, 'little')


def _to565(r, g, b):
    return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)


def _enc_color(px):
    """16 (r,g,b) -> 8-byte colour block in 4-colour mode (c0 > c1)."""
    rs = [p[0] for p in px]; gs = [p[1] for p in px]; bs_ = [p[2] for p in px]
    lo = (min(rs), min(gs), min(bs_)); hi = (max(rs), max(gs), max(bs_))
    c0 = _to565(*hi); c1 = _to565(*lo)
    if c0 == c1:
        return struct.pack('<HHI', c0, c1, 0)
    if c0 < c1:
        c0, c1 = c1, c0
    p0, p1 = _rgb565(c0), _rgb565(c1)
    pal = [p0, p1, tuple((2 * a + b) // 3 for a, b in zip(p0, p1)), tuple((a + 2 * b) // 3 for a, b in zip(p0, p1))]
    bits = 0
    for i, p in enumerate(px):
        best = min(range(4), key=lambda j: (pal[j][0] - p[0]) ** 2 + (pal[j][1] - p[1]) ** 2 + (pal[j][2] - p[2]) ** 2)
        bits |= best << (2 * i)
    return struct.pack('<HHI', c0, c1, bits)


def encode_dxt5(rgba, w, h):
    """RGBA8 (top row first) -> DXT5 blocks. Simple bounding-box endpoint encoder (good enough for atlas fallbacks)."""
    bw, bh = max(1, (w + 3) // 4), max(1, (h + 3) // 4)
    out = bytearray()
    for by in range(bh):
        for bx in range(bw):
            px = []; al = []
            for py in range(4):
                y = min(by * 4 + py, h - 1)
                for pxx in range(4):
                    x = min(bx * 4 + pxx, w - 1)
                    o = (y * w + x) * 4
                    px.append((rgba[o], rgba[o + 1], rgba[o + 2])); al.append(rgba[o + 3])
            out += _enc_alpha(al) + _enc_color(px)
    return bytes(out)


def encode_dxt1(rgba, w, h):
    """RGBA8 -> DXT1 blocks (alpha ignored, 4-colour mode only)."""
    bw, bh = max(1, (w + 3) // 4), max(1, (h + 3) // 4)
    out = bytearray()
    for by in range(bh):
        for bx in range(bw):
            px = []
            for py in range(4):
                y = min(by * 4 + py, h - 1)
                for pxx in range(4):
                    x = min(bx * 4 + pxx, w - 1)
                    o = (y * w + x) * 4
                    px.append((rgba[o], rgba[o + 1], rgba[o + 2]))
            out += _enc_color(px)
    return bytes(out)


def downsample2(rgba, w, h):
    """Box filter to (w//2, h//2) (min 1)."""
    nw, nh = max(1, w // 2), max(1, h // 2)
    out = bytearray(nw * nh * 4)
    for y in range(nh):
        for x in range(nw):
            acc = [0, 0, 0, 0]
            for dy in (0, 1):
                for dx in (0, 1):
                    xx = min(w - 1, x * 2 + dx); yy = min(h - 1, y * 2 + dy)
                    o = (yy * w + xx) * 4
                    for k in range(4):
                        acc[k] += rgba[o + k]
            o = (y * nw + x) * 4
            for k in range(4):
                out[o + k] = acc[k] >> 2
    return bytes(out), nw, nh


def dxt3_to_dxt5(data):
    """DXT3 blocks -> DXT5 blocks (colour block copied, 4-bit alpha re-encoded as 8-bit interpolated alpha)."""
    n = len(data) // 16
    out = bytearray(n * 16)
    for i in range(n):
        blk = data[i * 16:i * 16 + 16]
        al = [((blk[j >> 1] >> ((j & 1) * 4)) & 15) * 17 for j in range(16)]
        out[i * 16:i * 16 + 8] = _enc_alpha(al)
        out[i * 16 + 8:i * 16 + 16] = blk[8:16]
    return bytes(out)


def dxt1_to_dxt5(data, w, h):
    """DXT1 -> DXT5 (opaque alpha). 3-colour-mode blocks (c0 <= c1) are re-encoded because DXT5 has no such mode."""
    n = len(data) // 8
    out = bytearray(n * 16)
    for i in range(n):
        blk = data[i * 8:i * 8 + 8]
        c0, c1 = struct.unpack_from('<HH', blk, 0)
        out[i * 16:i * 16 + 8] = bytes([255, 255, 0, 0, 0, 0, 0, 0])
        if c0 > c1:
            out[i * 16 + 8:i * 16 + 16] = blk
        else:
            dec = decode_dxt('DXT1', blk, 4, 4)
            px = [(dec[k * 4], dec[k * 4 + 1], dec[k * 4 + 2]) for k in range(16)]
            out[i * 16 + 8:i * 16 + 16] = _enc_color(px)
    return bytes(out)


def to_pack_texture(dds):
    """parse_dds result -> (fmt_code, w, h, [level bytes]) in a Unity-loadable format.
    DXT1 stays DXT1, DXT3/DXT5 become DXT5 (Unity has no DXT3), raw becomes RGBA32."""
    w, h = dds['w'], dds['h']
    f = dds['fmt']; lv = dds['levels']
    if f == 'DXT1':
        return FMT_DXT1, w, h, lv
    if f == 'DXT5':
        return FMT_DXT5, w, h, lv
    if f == 'DXT3':
        return FMT_DXT5, w, h, [dxt3_to_dxt5(x) for x in lv]
    return FMT_RGBA32, w, h, lv


def compress_raw(dds, min_dim=8):
    """parse_dds result with raw RGBA levels -> (fmt_code, w, h, [DXT level bytes]).
    DXT1 when the picture is opaque, else DXT5. The mip chain is extended by box filtering down to `min_dim` px
    (the retail avatar textures only have 2 levels). Already compressed input is passed through to_pack_texture."""
    if dds['fmt'] != 'RGBA':
        return to_pack_texture(dds)
    w, h = dds['w'], dds['h']
    lv = list(dds['levels'])
    lw, lh = w, h
    for _ in range(len(lv) - 1):
        lw = max(1, lw // 2); lh = max(1, lh // 2)
    while min(lw, lh) > min_dim:
        nxt, nw, nh = downsample2(lv[-1], lw, lh)
        lv.append(nxt); lw, lh = nw, nh
    opaque = all(lv[0][i] == 255 for i in range(3, len(lv[0]), 4))
    out = []; lw, lh = w, h
    for l in lv:
        out.append(encode_dxt1(l, lw, lh) if opaque else encode_dxt5(l, lw, lh))
        lw = max(1, lw // 2); lh = max(1, lh // 2)
    return (FMT_DXT1 if opaque else FMT_DXT5), w, h, out


def decode_pack_texture(fmt, w, h, data):
    """Inverse for previews: pack format code + level-0 bytes -> RGBA8."""
    if fmt == FMT_DXT1:
        return decode_dxt('DXT1', data, w, h)
    if fmt == FMT_DXT5:
        return decode_dxt('DXT5', data, w, h)
    return bytes(data[:w * h * 4])


def level_size(fmt, w, h):
    if fmt == FMT_RGBA32:
        return w * h * 4
    return max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * (8 if fmt == FMT_DXT1 else 16)


def make_dds(fmt, w, h, levels):
    """Wrap pack texture data into a .dds (for external viewers)."""
    hdr = bytearray(128)
    hdr[0:4] = b'DDS '
    struct.pack_into('<7I', hdr, 4, 124, 0x1007 | 0x20000 | 0x80000, h, w, len(levels[0]), 0, len(levels))
    if fmt == FMT_RGBA32:
        struct.pack_into('<8I', hdr, 76, 32, 0x41, 0, 32, 0xff, 0xff00, 0xff0000, 0xff000000)
    else:
        struct.pack_into('<2I4s', hdr, 76, 32, 4, b'DXT1' if fmt == FMT_DXT1 else b'DXT5')
    struct.pack_into('<I', hdr, 108, 0x1000 | 0x400000)
    return bytes(hdr) + b''.join(levels)
