"""Image decoding for RO textures (BMP 8/24/32 bit, TGA 24/32 bit raw or RLE) + resize helpers. Stdlib only.

Pixel buffers are RGBA8, row 0 = TOP row (the order rosedds / the RPG1 pack use). RO marks transparent texels with pure magenta
(r >= 0xF8, g <= 0x07, b >= 0xF8 in practice): `key_magenta` turns them into alpha 0 and fills their colour from opaque neighbours
so bilinear filtering shows no pink fringe.
"""
import struct


class ImageError(ValueError):
    pass


def decode(data, name=''):
    """-> (w, h, rgba bytes). Raises ImageError for unsupported files (JPEG etc.)."""
    if data[:2] == b'BM':
        return _bmp(data)
    low = name.lower()
    if low.endswith('.tga') or (len(data) > 18 and data[2] in (2, 10) and data[1] == 0):
        return _tga(data)
    raise ImageError('unsupported image format')


def _bmp(d):
    if len(d) < 54:
        raise ImageError('BMP too short')
    off = struct.unpack_from('<I', d, 10)[0]
    hsz = struct.unpack_from('<I', d, 14)[0]
    if hsz < 40:
        raise ImageError('BMP core header not supported')
    w, h, _planes, bpp, comp = struct.unpack_from('<iiHHI', d, 18)
    ncol = struct.unpack_from('<I', d, 46)[0]
    if comp not in (0, 3) or w <= 0 or h == 0 or w > 4096 or abs(h) > 4096:
        raise ImageError('BMP compression %d / size %dx%d not supported' % (comp, w, h))
    top_down = h < 0
    h = abs(h)
    out = bytearray(w * h * 4)
    if bpp == 8:
        ncol = ncol or 256
        pal = []
        po = 14 + hsz
        for i in range(ncol):
            b, g, r, _ = d[po + i * 4:po + i * 4 + 4] if po + i * 4 + 4 <= len(d) else (0, 0, 0, 0)
            pal.append((r, g, b))
        pal += [(0, 0, 0)] * (256 - len(pal))
        stride = (w + 3) & ~3
        for y in range(h):
            src = off + (y if top_down else h - 1 - y) * stride
            row = d[src:src + w]
            o = y * w * 4
            for x, ci in enumerate(row):
                r, g, b = pal[ci]
                out[o + x * 4] = r; out[o + x * 4 + 1] = g; out[o + x * 4 + 2] = b; out[o + x * 4 + 3] = 255
    elif bpp in (24, 32):
        bs = bpp // 8
        stride = (w * bs + 3) & ~3
        for y in range(h):
            src = off + (y if top_down else h - 1 - y) * stride
            o = y * w * 4
            for x in range(w):
                p = src + x * bs
                if p + 3 > len(d):
                    break
                out[o + x * 4] = d[p + 2]; out[o + x * 4 + 1] = d[p + 1]; out[o + x * 4 + 2] = d[p]; out[o + x * 4 + 3] = 255
    else:
        raise ImageError('BMP %d bit not supported' % bpp)
    return w, h, bytes(out)


def _tga(d):
    idl, cmt, typ = d[0], d[1], d[2]
    w, h, bpp, desc = struct.unpack_from('<HHBB', d, 12)
    if cmt != 0 or typ not in (2, 10) or bpp not in (24, 32) or w == 0 or h == 0:
        raise ImageError('TGA type %d / %d bit not supported' % (typ, bpp))
    bs = bpp // 8
    p = 18 + idl
    n = w * h
    px = bytearray(n * 4)
    i = 0
    if typ == 2:
        for i in range(n):
            q = p + i * bs
            if q + bs > len(d):
                break
            px[i * 4:i * 4 + 4] = bytes((d[q + 2], d[q + 1], d[q], d[q + 3] if bs == 4 else 255))
    else:
        while i < n and p < len(d):
            c = d[p]; p += 1
            cnt = (c & 0x7f) + 1
            if c & 0x80:
                v = bytes((d[p + 2], d[p + 1], d[p], d[p + 3] if bs == 4 else 255)); p += bs
                for _ in range(min(cnt, n - i)):
                    px[i * 4:i * 4 + 4] = v; i += 1
            else:
                for _ in range(min(cnt, n - i)):
                    px[i * 4:i * 4 + 4] = bytes((d[p + 2], d[p + 1], d[p], d[p + 3] if bs == 4 else 255)); p += bs; i += 1
    if not desc & 0x20:                  # bottom-up -> flip to top row first
        row = w * 4
        px = b''.join(bytes(px[(h - 1 - y) * row:(h - y) * row]) for y in range(h))
    return w, h, bytes(px)


def key_magenta(rgba, w, h):
    """-> (rgba, has_alpha). Magenta texels -> alpha 0, colour = mean of opaque 8-neighbours (else black)."""
    b = bytearray(rgba)
    keyed = []
    for i in range(w * h):
        o = i * 4
        if b[o] >= 0xF8 and b[o + 1] <= 0x07 and b[o + 2] >= 0xF8:
            b[o + 3] = 0; keyed.append(i)
        elif b[o + 3] < 255:
            keyed.append(-1)            # real alpha in the file (TGA)
    if not keyed:
        return bytes(b), False
    for i in keyed:
        if i < 0:
            continue
        x, y = i % w, i // w
        acc = [0, 0, 0]; c = 0
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                xx, yy = x + dx, y + dy
                if 0 <= xx < w and 0 <= yy < h:
                    o = (yy * w + xx) * 4
                    if b[o + 3] == 255:
                        acc[0] += b[o]; acc[1] += b[o + 1]; acc[2] += b[o + 2]; c += 1
        o = i * 4
        if c:
            b[o] = acc[0] // c; b[o + 1] = acc[1] // c; b[o + 2] = acc[2] // c
        else:
            b[o] = b[o + 1] = b[o + 2] = 0
    return bytes(b), True


def resize(rgba, w, h, nw, nh):
    """Box / nearest resample to nw x nh (box average when shrinking by an integer-ish factor, nearest otherwise)."""
    if (w, h) == (nw, nh):
        return rgba
    out = bytearray(nw * nh * 4)
    for y in range(nh):
        y0 = y * h // nh; y1 = max(y0 + 1, (y + 1) * h // nh)
        for x in range(nw):
            x0 = x * w // nw; x1 = max(x0 + 1, (x + 1) * w // nw)
            acc = [0, 0, 0, 0]; c = 0
            for yy in range(y0, y1):
                for xx in range(x0, x1):
                    o = (yy * w + xx) * 4
                    acc[0] += rgba[o]; acc[1] += rgba[o + 1]; acc[2] += rgba[o + 2]; acc[3] += rgba[o + 3]; c += 1
            o = (y * nw + x) * 4
            out[o] = acc[0] // c; out[o + 1] = acc[1] // c; out[o + 2] = acc[2] // c; out[o + 3] = acc[3] // c
    return bytes(out)


def pow2_at_most(v, cap):
    p = 4
    while p * 2 <= min(v, cap):
        p *= 2
    return p


def placeholder(w=8, h=8, rgb=(128, 128, 128)):
    """Neutral grey texture for missing / unsupported files (our own pixels, nothing from the game)."""
    return w, h, bytes(list(rgb) + [255]) * (w * h)


def write_bmp24(w, h, rgba):
    """Test helper: RGBA (top row first) -> 24-bit bottom-up BMP bytes."""
    stride = (w * 3 + 3) & ~3
    body = bytearray()
    for y in range(h - 1, -1, -1):
        row = bytearray()
        for x in range(w):
            o = (y * w + x) * 4
            row += bytes((rgba[o + 2], rgba[o + 1], rgba[o]))
        body += row + b'\0' * (stride - len(row))
    hdr = b'BM' + struct.pack('<IHHI', 54 + len(body), 0, 0, 54) + struct.pack('<IiiHHIIiiII', 40, w, h, 1, 24, 0, len(body), 2835, 2835, 0, 0)
    return hdr + bytes(body)


def write_bmp8(w, h, idx, pal):
    """Test helper: palette BMP (idx top row first, pal = [(r,g,b)])."""
    stride = (w + 3) & ~3
    body = bytearray()
    for y in range(h - 1, -1, -1):
        row = bytes(idx[y * w:(y + 1) * w])
        body += row + b'\0' * (stride - len(row))
    pb = b''.join(bytes((b, g, r, 0)) for r, g, b in pal)
    off = 54 + len(pb)
    hdr = b'BM' + struct.pack('<IHHI', off + len(body), 0, 0, off) + struct.pack('<IiiHHIIiiII', 40, w, h, 1, 8, 0, len(body), 2835, 2835, len(pal), 0)
    return hdr + pb + bytes(body)
