# WAD3 / miptex reader (stdlib only). Decodes palette-indexed GoldSrc textures to RGBA.
# usage: python3 -I wad3.py <file.wad>          — lists the miptex lumps
#
# miptex (in a WAD lump or embedded in the BSP texture lump), offsets relative to the miptex start:
#   char name[16]; uint32 width, height; uint32 offsets[4]   (mip 0..3, 0 = not embedded)
#   mip data w*h, w/2*h/2, w/4*h/4, w/8*h/8 bytes; then int16 palette count (256) + 256*3 RGB palette
# WAD3: "WAD3", int32 numlumps, int32 infotableofs; lump entry 32 bytes:
#   int32 filepos, disksize, size; int8 type (0x43 = miptex); int8 compression; int16 pad; char name[16]
import os, struct, sys

SKIP_EXACT = {"sky", "clip", "trigger", "origin", "aaatrigger", "null", "hint", "skip", "bevel"}
SKIP_PREFIX = ("clip", "aaatrigger")


def is_skipped(name):
    n = name.lower()
    return n in SKIP_EXACT or n.startswith(SKIP_PREFIX)


def decode_miptex(buf, base=0):
    """Returns (name, w, h, rgba bytes) of mip 0, or None if the pixel data is not present."""
    name, w, h, o0, o1, o2, o3 = struct.unpack_from("<16sII4I", buf, base)
    name = name.split(b"\0")[0].decode("latin-1")
    if o0 == 0:
        return None
    npix = w * h
    pal_off = base + o3 + (w // 8) * (h // 8)
    count, = struct.unpack_from("<h", buf, pal_off)
    pal = buf[pal_off + 2:pal_off + 2 + 768]
    if len(pal) < 768:
        pal = pal + bytes(768 - len(pal))
    idx = buf[base + o0:base + o0 + npix]
    masked = name.startswith("{")
    lut = []
    for i in range(256):
        r, g, b = pal[i * 3], pal[i * 3 + 1], pal[i * 3 + 2]
        lut.append(bytes((r, g, b, 255)))
    if masked:
        lut[255] = b"\0\0\0\0"                    # palette index 255 = transparent
    rgba = b"".join(lut[i] for i in idx)
    return name, w, h, rgba


class Wad:
    def __init__(self, path):
        self.path = path
        self.data = open(path, "rb").read()
        magic, n, ofs = struct.unpack_from("<4sii", self.data, 0)
        if magic not in (b"WAD3", b"WAD2"):
            raise ValueError(f"{path}: not a WAD3 ({magic!r})")
        self.lumps = {}
        for i in range(n):
            fp, disk, size, typ, comp, _pad, name = struct.unpack_from("<iiibbh16s", self.data, ofs + 32 * i)
            if typ != 0x43 or comp != 0:
                continue
            self.lumps[name.split(b"\0")[0].decode("latin-1").lower()] = fp

    def get(self, name):
        fp = self.lumps.get(name.lower())
        return None if fp is None else decode_miptex(self.data, fp)


def find_wads(wad_key, game_root, mod):
    """Resolve the worldspawn "wad" key (paths like \\sierra\\half-life\\valve\\halflife.wad) against the mod's
    content folders (gamedirs.content_dirs: <mod>_addon, <mod>, <mod>_downloads, valve; the first folder holding a
    name wins). Returns (found paths, missing names). Then every other WAD in those dirs is appended as a fallback
    (some maps reference WADs under the wrong dir)."""
    from gamedirs import content_dirs
    dirs = content_dirs(game_root, mod)
    index = {}
    for d in dirs:
        for f in sorted(os.listdir(d)):
            if f.lower().endswith(".wad"):
                index.setdefault(f.lower(), os.path.join(d, f))
    found, missing = [], []
    for entry in wad_key.replace("\\", "/").split(";"):
        base = entry.strip().split("/")[-1].lower()
        if not base:
            continue
        if base in index:
            if index[base] not in found:
                found.append(index[base])
        else:
            missing.append(base)
    fallback = [p for p in index.values() if p not in found and not os.path.basename(p).lower()
                in ("decals.wad", "fonts.wad", "gfx.wad", "spraypaint.wad", "cached.wad")]
    return found, missing, fallback


if __name__ == "__main__":
    w = Wad(sys.argv[1])
    for n in sorted(w.lumps):
        t = w.get(n)
        print(f"{n:16s} {t[1]}x{t[2]}" if t else n)
    print(len(w.lumps), "miptex lumps")
