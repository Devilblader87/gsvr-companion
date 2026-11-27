#!/usr/bin/env python3
"""WaW textures for packs: xmodel -> materials -> images, IWI decode at a mip level (<= 512 px), PNG or raw DXT.
Python 3 stdlib only. BYOG: reads the player's own main/*.iwd, writes only to a scratch dir.

Findings (German retail PC, see docs/pack_format.md):
  * after an xmodel's surface data the zone stream holds the materials it uses, inline the first time a material is
    used in a fastfile: 'mc/mtl_weapon_mp_thompson' followed by the image names of its techset samplers
    (colorMap 'w_tommy_c', combined spec/gloss '~w_tommy_s-rgb&w_tommy_g-l-11'). Later users of the same material only
    carry a back reference, so the list of the 1st user is the complete set (checked for the 3 prototype weapons).
  * image file = images/<name>.iwi in main/iw_*.iwd. '_c' colour, '_n' normal map (RGB), '_s' specular. A name
    '~A-rgb&B-l-NN' is a combined image the linker made: RGB = image A, ALPHA = luminance of image B (spec colour + gloss).
  * IWI v6: 28 byte header, mip chain stored SMALLEST FIRST, so mip L (size w>>L) is found counting from the end.

Usage:
  waw_tex.py model <file.ff> <xmodel> <main dir> --out <dir> [--max-px 512] [--dxt]
  waw_tex.py image <main dir> <image name> --out <dir> [--max-px 512]
  waw_tex.py index <main dir> [--grep substr]
"""
import argparse
import glob
import json
import os
import re
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import waw_ff  # noqa: E402
import waw_iwd  # noqa: E402


def iwi_index(main_dir):
    """{lowercase image name: (iwd path, member name)} from the central directories only."""
    idx = {}
    for f in sorted(glob.glob(os.path.join(main_dir, "*.iwd"))):
        for n in zipfile.ZipFile(f).namelist():
            if n.startswith("images/") and n.lower().endswith(".iwi"):
                idx[n[7:-4].lower()] = (f, n)
    return idx


def read_iwi(index, name):
    f, n = index[name.lower()]
    return zipfile.ZipFile(f).read(n)


def mip_count(w, h):
    n = 1
    while w > 1 or h > 1:
        w, h = max(1, w // 2), max(1, h // 2)
        n += 1
    return n


def decode_mip(b, level):
    """Decode mip 'level' (0 = full size) of an IWI. Returns (header, w, h, rgba bytearray)."""
    hd = waw_iwd.iwi_header(b)
    fmt, w0, h0 = hd["format"], hd["width"], hd["height"]
    level = max(0, min(level, mip_count(w0, h0) - 1))
    # bytes of mips smaller than 'level' sit before; mip 'level' ends where mip level-1 begins
    end = len(b)
    for k in range(level):
        end -= waw_iwd.mip_bytes(fmt, max(1, w0 >> k), max(1, h0 >> k))
    w, h = max(1, w0 >> level), max(1, h0 >> level)
    n = waw_iwd.mip_bytes(fmt, w, h)
    top = b[end - n:end]
    if level == 0:
        return waw_iwd.decode_iwi(b)
    if fmt in (0x0B, 0x0C, 0x0D):
        rgba = waw_iwd.decode_dxt(top, w, h, fmt)
    else:
        # uncompressed formats: build a tiny IWI-less decode through the same code path
        fake = b[:6] + w.to_bytes(2, "little") + h.to_bytes(2, "little") + b[10:28] + top
        _, _, _, rgba = waw_iwd.decode_iwi(fake)
    return hd, w, h, rgba


def level_for(hd, max_px):
    lv = 0
    w, h = hd["width"], hd["height"]
    while max(w, h) > max_px:
        w, h, lv = max(1, w // 2), max(1, h // 2), lv + 1
    return lv


def dxt_chain(b, max_px=512):
    """Raw DXT blocks from the first mip <= max_px down to 1x1 (Unity can upload these as is).
    Returns (fmt name, w, h, mip count, bytes) or None if the image is not DXT."""
    hd = waw_iwd.iwi_header(b)
    if hd["format"] not in (0x0B, 0x0C, 0x0D):
        return None
    lv = level_for(hd, max_px)
    fmt, w0, h0 = hd["format"], hd["width"], hd["height"]
    total = sum(waw_iwd.mip_bytes(fmt, max(1, w0 >> k), max(1, h0 >> k)) for k in range(lv, mip_count(w0, h0)))
    data = b[len(b) - total:]
    # stored smallest first; Unity wants largest first -> reorder
    sizes = [waw_iwd.mip_bytes(fmt, max(1, w0 >> k), max(1, h0 >> k)) for k in range(lv, mip_count(w0, h0))]
    parts, o = [], 0
    for sz in reversed(sizes):              # file order: smallest mip first
        parts.append(data[o:o + sz])
        o += sz
    return hd["format_name"], max(1, w0 >> lv), max(1, h0 >> lv), len(sizes), b"".join(reversed(parts))


def model_materials(zone, model, index, span=0x6000):
    """[(material name, [image names that exist in the iwd index])] in stream order after the model's surface data."""
    u = zone.u
    end = model["end"]
    out, cur = [], None
    for m in re.finditer(rb"[\x21-\x7e]{3,120}\x00", u[end:end + span]):
        s = m.group().decode()[:-1]
        if s.startswith("mc/"):
            cur = (s, [])
            out.append(cur)
        elif cur is not None and s.lower() in index:
            if s not in cur[1]:
                cur[1].append(s)
        elif cur is not None and re.match(r"^(\w+_)?(weapon|viewmodel)\w*$", s) and not s.startswith("mc/"):
            if s not in index and len(cur[1]) >= 1:
                pass
    # stop at the first non-material block: keep materials until a repeated name
    seen, res = set(), []
    for n, imgs in out:
        if n in seen:
            break
        seen.add(n)
        res.append((n, imgs))
    return res


def classify(name):
    n = name.lower()
    if n.startswith("~"):
        return "spec_gloss"
    for suf, k in (("_c", "color"), ("_n", "normal"), ("_s", "spec"), ("_d", "detail")):
        if n.endswith(suf):
            return k
    return "other"


def downscale(w, h, rgba, tw, th):
    """Box filter to (tw, th) (both must divide w, h)."""
    if (w, h) == (tw, th):
        return rgba
    fx, fy = w // tw, h // th
    out = bytearray(tw * th * 4)
    for y in range(th):
        for x in range(tw):
            acc = [0, 0, 0, 0]
            for dy in range(fy):
                o = 4 * ((y * fy + dy) * w + x * fx)
                for dx in range(fx):
                    for c in range(4):
                        acc[c] += rgba[o + 4 * dx + c]
            n = fx * fy
            out[4 * (y * tw + x):4 * (y * tw + x) + 4] = bytes(a // n for a in acc)
    return out


def export_image(index, name, out_dir, max_px=512, dxt=False):
    b = read_iwi(index, name)
    hd = waw_iwd.iwi_header(b)
    lv = level_for(hd, max_px)
    hd2, w, h, rgba = decode_mip(b, lv)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    path = os.path.join(out_dir, safe + ".png")
    waw_iwd.write_png(path, w, h, rgba)
    ent = {"image": name, "kind": classify(name), "format": hd["format_name"], "src": [hd["width"], hd["height"]],
           "out": [w, h], "mip": lv, "png": os.path.basename(path)}
    if dxt:
        ch = dxt_chain(b, max_px)
        if ch:
            ent["dxt"] = {"format": ch[0], "mips": ch[3], "bytes": len(ch[4])}
            with open(os.path.join(out_dir, safe + "." + ch[0].lower()), "wb") as f:
                f.write(ch[4])
    return ent


def cmd_model(a):
    z = waw_ff.Zone(a.file)
    idx = iwi_index(a.main)
    m = waw_ff.parse_xmodel(z, a.xmodel)
    mats = model_materials(z, m, idx)
    os.makedirs(a.out, exist_ok=True)
    res = {"model": a.xmodel, "materials": []}
    for mat, imgs in mats:
        e = {"material": mat, "images": [export_image(idx, i, a.out, a.max_px, a.dxt) for i in imgs]}
        res["materials"].append(e)
        print(mat, [(x["image"], x["kind"], tuple(x["out"])) for x in e["images"]])
    with open(os.path.join(a.out, a.xmodel + ".materials.json"), "w") as f:
        json.dump(res, f, indent=1)
    if not mats:
        print("no materials found")


def cmd_image(a):
    idx = iwi_index(a.main)
    os.makedirs(a.out, exist_ok=True)
    print(json.dumps(export_image(idx, a.name, a.out, a.max_px)))


def cmd_index(a):
    idx = iwi_index(a.main)
    print(len(idx), "images")
    for k, (f, n) in idx.items():
        if not a.grep or a.grep in k:
            print(k, os.path.basename(f))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("model"); p.add_argument("file"); p.add_argument("xmodel"); p.add_argument("main")
    p.add_argument("--out", required=True); p.add_argument("--max-px", type=int, default=512)
    p.add_argument("--dxt", action="store_true")
    p = sp.add_parser("image"); p.add_argument("main"); p.add_argument("name"); p.add_argument("--out", required=True)
    p.add_argument("--max-px", type=int, default=512)
    p = sp.add_parser("index"); p.add_argument("main"); p.add_argument("--grep")
    a = ap.parse_args()
    {"model": cmd_model, "image": cmd_image, "index": cmd_index}[a.cmd](a)


if __name__ == "__main__":
    main()
