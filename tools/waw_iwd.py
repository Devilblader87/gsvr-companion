#!/usr/bin/env python3
"""World at War .iwd (zip) lister + .iwi image decoder. Python 3 stdlib only (PNG written with zlib).

BYOG: reads the player's own main/*.iwd. Listing reads only the zip central directory; members are
extracted to a scratch dir, never into the repo.

IWI (version 6, CoD4 / WaW): 28-byte header
  char magic[3] = "IWi"; u8 version (6); u8 format; u8 flags; u16 width, height, depth;
  u32 fileSizeForPicmip[4]   ([0] = whole file size, [1..3] = size with the top 1..3 mips dropped)
  then the mip chain SMALLEST FIRST; the full-size mip is the last <mip0 bytes> of the file.
  format: 1 ARGB32 (stored B,G,R,A), 2 RGB24 (B,G,R), 3 L8A8, 4 A8, 0x0B DXT1, 0x0C DXT3, 0x0D DXT5
  flags: 0x01 nopicmip, 0x02 cube, 0x04 volume, 0x08 streaming? (we only need 2D)

Usage:
  waw_iwd.py list   <dir with iwd files> [--json out.json]     counts per iwd: images, sounds, other
  waw_iwd.py find   <dir> <substring>                           which iwd holds which member
  waw_iwd.py extract <dir> <member> --out <scratch dir>
  waw_iwd.py iwi2png <file.iwi> <out.png>                        header + decode the top mip
"""
import argparse
import glob
import json
import os
import struct
import sys
import zipfile
import zlib

FORMATS = {1: "ARGB32", 2: "RGB24", 3: "L8A8", 4: "A8", 0x0B: "DXT1", 0x0C: "DXT3", 0x0D: "DXT5"}


def iwds(d):
    return sorted(glob.glob(os.path.join(d, "*.iwd")))


def cmd_list(a):
    out, tot = {}, {"iwi": 0, "wav": 0, "mp3": 0, "other": 0, "files": 0, "bytes_iwi": 0, "bytes_wav": 0}
    for f in iwds(a.dir):
        z = zipfile.ZipFile(f)
        c = {"iwi": 0, "wav": 0, "mp3": 0, "other": 0, "files": 0, "bytes_iwi": 0, "bytes_wav": 0, "dirs": {}}
        for i in z.infolist():
            if i.is_dir():
                continue
            n = i.filename.lower()
            ext = n.rsplit(".", 1)[-1] if "." in n.split("/")[-1] else ""
            k = ext if ext in ("iwi", "wav", "mp3") else "other"
            c[k] += 1
            c["files"] += 1
            if k in ("iwi", "wav"):
                c["bytes_" + k] += i.file_size
            top = n.split("/")[0] if "/" in n else "(root)"
            c["dirs"][top] = c["dirs"].get(top, 0) + 1
        out[os.path.basename(f)] = c
        for k in tot:
            tot[k] += c[k]
        print(f"{os.path.basename(f):28s} files {c['files']:5d}  iwi {c['iwi']:5d}  wav {c['wav']:5d}  "
              f"other {c['other']:4d}  {c['dirs']}")
    print("TOTAL", tot)
    if a.json:
        with open(a.json, "w") as f:
            json.dump({"per_iwd": out, "total": tot}, f, indent=1)


def cmd_find(a):
    s = a.sub.lower()
    for f in iwds(a.dir):
        for i in zipfile.ZipFile(f).infolist():
            if s in i.filename.lower():
                print(os.path.basename(f), i.filename, i.file_size)


def find_member(d, member):
    m = member.lower()
    for f in iwds(d):
        z = zipfile.ZipFile(f)
        for i in z.infolist():
            if i.filename.lower() == m:
                return z, i
    return None, None


def cmd_extract(a):
    z, i = find_member(a.dir, a.member)
    if not z:
        sys.exit(f"{a.member} not found")
    os.makedirs(a.out, exist_ok=True)
    p = os.path.join(a.out, os.path.basename(i.filename))
    with open(p, "wb") as f:
        f.write(z.read(i))
    print(f"{os.path.basename(z.filename)}:{i.filename} -> {p} ({i.file_size} B)")


# ---- IWI ------------------------------------------------------------------------------------------
def iwi_header(b):
    if b[:3] != b"IWi":
        raise ValueError(f"not an IWI file (magic {b[:3]!r})")
    ver, fmt, flags = b[3], b[4], b[5]
    w, h, dpt = struct.unpack_from("<3H", b, 6)
    sizes = struct.unpack_from("<4I", b, 12)
    return {"version": ver, "format": fmt, "format_name": FORMATS.get(fmt, f"0x{fmt:x}"), "flags": flags,
            "width": w, "height": h, "depth": dpt, "picmip_sizes": sizes}


def mip_bytes(fmt, w, h):
    if fmt in (0x0B, 0x0C, 0x0D):
        bw, bh = max(1, (w + 3) // 4), max(1, (h + 3) // 4)
        return bw * bh * (8 if fmt == 0x0B else 16)
    return w * h * {1: 4, 2: 3, 3: 2, 4: 1}[fmt]


def _c565(c):
    r, g, b = (c >> 11) & 31, (c >> 5) & 63, c & 31
    return ((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2))


def _color_block(blk, o, dxt1):
    c0, c1, bits = struct.unpack_from("<HHI", blk, o)
    p0, p1 = _c565(c0), _c565(c1)
    if c0 > c1 or not dxt1:
        p2 = tuple((2 * x + y) // 3 for x, y in zip(p0, p1))
        p3 = tuple((x + 2 * y) // 3 for x, y in zip(p0, p1))
        pal = [p0 + (255,), p1 + (255,), p2 + (255,), p3 + (255,)]
    else:
        p2 = tuple((x + y) // 2 for x, y in zip(p0, p1))
        pal = [p0 + (255,), p1 + (255,), p2 + (255,), (0, 0, 0, 0)]
    return [pal[(bits >> (2 * i)) & 3] for i in range(16)]


def decode_dxt(data, w, h, fmt):
    out = bytearray(w * h * 4)
    bw, bh = (w + 3) // 4, (h + 3) // 4
    bs = 8 if fmt == 0x0B else 16
    o = 0
    for by in range(bh):
        for bx in range(bw):
            alpha = None
            if fmt == 0x0C:
                a64 = int.from_bytes(data[o:o + 8], "little")
                alpha = [((a64 >> (4 * i)) & 15) * 17 for i in range(16)]
                cols = _color_block(data, o + 8, False)
            elif fmt == 0x0D:
                a0, a1 = data[o], data[o + 1]
                ab = int.from_bytes(data[o + 2:o + 8], "little")
                if a0 > a1:
                    pal = [a0, a1] + [((7 - i) * a0 + i * a1) // 7 for i in range(1, 7)]
                else:
                    pal = [a0, a1] + [((5 - i) * a0 + i * a1) // 5 for i in range(1, 5)] + [0, 255]
                alpha = [pal[(ab >> (3 * i)) & 7] for i in range(16)]
                cols = _color_block(data, o + 8, False)
            else:
                cols = _color_block(data, o, True)
            o += bs
            for i in range(16):
                x, y = bx * 4 + (i & 3), by * 4 + (i >> 2)
                if x < w and y < h:
                    r, g, b, aa = cols[i]
                    if alpha is not None:
                        aa = alpha[i]
                    p = 4 * (y * w + x)
                    out[p:p + 4] = bytes((r, g, b, aa))
    return out


def decode_iwi(b):
    hd = iwi_header(b)
    fmt, w, h = hd["format"], hd["width"], hd["height"]
    n = mip_bytes(fmt, w, h)
    top = b[len(b) - n:]
    if fmt in (0x0B, 0x0C, 0x0D):
        rgba = decode_dxt(top, w, h, fmt)
    elif fmt == 1:
        rgba = bytearray(w * h * 4)
        for i in range(w * h):
            bb, g, r, aa = top[4 * i:4 * i + 4]
            rgba[4 * i:4 * i + 4] = bytes((r, g, bb, aa))
    elif fmt == 2:
        rgba = bytearray(w * h * 4)
        for i in range(w * h):
            bb, g, r = top[3 * i:3 * i + 3]
            rgba[4 * i:4 * i + 4] = bytes((r, g, bb, 255))
    elif fmt == 3:
        rgba = bytearray(w * h * 4)
        for i in range(w * h):
            l, aa = top[2 * i:2 * i + 2]
            rgba[4 * i:4 * i + 4] = bytes((l, l, l, aa))
    elif fmt == 4:
        rgba = bytearray(w * h * 4)
        for i in range(w * h):
            rgba[4 * i:4 * i + 4] = bytes((255, 255, 255, top[i]))
    else:
        raise ValueError(f"format {hd['format_name']} not supported")
    return hd, w, h, rgba


def write_png(path, w, h, rgba):
    raw = b"".join(b"\0" + bytes(rgba[4 * w * y:4 * w * (y + 1)]) for y in range(h))

    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)


def cmd_iwi2png(a):
    b = open(a.iwi, "rb").read()
    hd, w, h, rgba = decode_iwi(b)
    chain = []
    mw, mh, tot = w, h, 0
    while True:
        chain.append(mip_bytes(hd["format"], mw, mh))
        if mw == 1 and mh == 1:
            break
        mw, mh = max(1, mw // 2), max(1, mh // 2)
    print(json.dumps(hd), f"file {len(b)} B, mip chain {sum(chain) + 28} B ({len(chain)} mips)")
    write_png(a.png, w, h, rgba)
    print(f"wrote {a.png} {w}x{h}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("list"); p.add_argument("dir"); p.add_argument("--json")
    p = sp.add_parser("find"); p.add_argument("dir"); p.add_argument("sub")
    p = sp.add_parser("extract"); p.add_argument("dir"); p.add_argument("member"); p.add_argument("--out", required=True)
    p = sp.add_parser("iwi2png"); p.add_argument("iwi"); p.add_argument("png")
    a = ap.parse_args()
    {"list": cmd_list, "find": cmd_find, "extract": cmd_extract, "iwi2png": cmd_iwi2png}[a.cmd](a)


if __name__ == "__main__":
    main()
