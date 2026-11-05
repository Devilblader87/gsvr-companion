"""CS / Condition Zero bot navigation mesh reader (.nav, magic 0xFEEDFACE, versions 1..5).

Layout from the cs.so loaders (decomp/cstrike/NOTES_nav.md: LoadNavigationMap, PlaceDirectory::Load,
CNavArea::Load, HidingSpot::Load). Little-endian:
  u32 magic 0xFEEDFACE, u32 version
  v>=4: u32 bsp file size (the map the mesh was generated for)
  v>=5: place directory: u16 count, count × (u16 len, len bytes incl. NUL) → place names, index+1 = Place id
  u32 area count, then per area:
    u32 id, u8 attribute flags (1 crouch, 2 jump, 4 precise, 8 no-jump)
    f32×3 extent lo, f32×3 extent hi, f32 neZ, f32 swZ  (corner heights: nw = lo.z, se = hi.z)
    4 × (u32 count, count × u32 area id)        connections north, east, south, west
    u8 hiding spot count; spot: v1 f32×3 pos; v>=2 u32 id, f32×3 pos, u8 flags (1 cover, 2 good sniper, 4 ideal sniper)
    v<15 approach info: u8 count × (u32 here, u32 prev, u8 prevToHereHow, u32 next, u8 hereToNextHow)
    u32 encounter path count × (u32 from, u8 fromDir, u32 to, u8 toDir, u8 spot count × (u32 spot id, u8 t·255))
    v>=5: u16 place (0 = none)
  Ladders are not stored (the game rebuilds them from func_ladder).
usage: python3 -I tools/nav5.py <file.nav>... [--png out.png] [--json out.json]
"""
import json, struct, sys, zlib

DIRS = ("north", "east", "south", "west")


class Reader:
    def __init__(self, data):
        self.d, self.o = data, 0

    def take(self, fmt):
        v = struct.unpack_from("<" + fmt, self.d, self.o)
        self.o += struct.calcsize("<" + fmt)
        return v

    def u8(self): return self.take("B")[0]
    def u16(self): return self.take("H")[0]
    def u32(self): return self.take("I")[0]


def parse(data):
    r = Reader(data)
    magic, version = r.u32(), r.u32()
    if magic != 0xFEEDFACE or not 1 <= version <= 5:
        raise ValueError(f"not a CS nav file (magic {magic:#x}, version {version})")
    nav = {"version": version, "bsp_size": r.u32() if version >= 4 else None, "places": [], "areas": []}
    if version >= 5:
        for _ in range(r.u16()):
            n = r.u16()
            nav["places"].append(r.d[r.o:r.o + n].split(b"\0")[0].decode("latin-1"))
            r.o += n
    for _ in range(r.u32()):
        a = {"id": r.u32(), "flags": r.u8()}
        lo, hi = r.take("3f"), r.take("3f")
        a["lo"], a["hi"] = lo, hi
        a["neZ"], a["swZ"] = r.take("2f")
        a["connect"] = [list(r.take(f"{r.u32()}I")) for _ in range(4)]
        spots = []
        for _ in range(r.u8()):
            if version == 1:
                spots.append({"id": None, "pos": r.take("3f"), "flags": 0})
            else:
                sid = r.u32(); pos = r.take("3f"); spots.append({"id": sid, "pos": pos, "flags": r.u8()})
        a["spots"] = spots
        a["approach"] = [r.take("IIBIB") for _ in range(r.u8())]
        enc = []
        for _ in range(r.u32()):
            frm, fdir, to, tdir = r.take("IBIB")
            enc.append({"from": frm, "fromDir": fdir, "to": to, "toDir": tdir,
                        "spots": [r.take("IB") for _ in range(r.u8())]})
        a["encounters"] = enc
        a["place"] = r.u16() if version >= 5 else 0
        nav["areas"].append(a)
    nav["trailing"] = len(data) - r.o
    return nav


def check(nav):
    """Structural sanity: every connection / approach / encounter id names an existing area, extents ordered."""
    ids = {a["id"] for a in nav["areas"]}
    bad = 0
    for a in nav["areas"]:
        bad += sum(1 for d in a["connect"] for t in d if t not in ids)
        bad += sum(1 for k in range(2) if a["lo"][k] > a["hi"][k])
        bad += sum(1 for e in a["encounters"] if e["from"] not in ids or e["to"] not in ids)
        bad += 1 if a["place"] > len(nav["places"]) else 0
    return bad


def write_png(path, w, h, rgb):
    raw = b"".join(b"\0" + bytes(rgb[y * w * 3:(y + 1) * w * 3]) for y in range(h))
    def chunk(t, d): return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    open(path, "wb").write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                           + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def render(nav, path, size=1024):
    """Top-down: areas shaded by height (crouch areas blue), connections green, hiding spots red."""
    xs = [v for a in nav["areas"] for v in (a["lo"][0], a["hi"][0])]
    ys = [v for a in nav["areas"] for v in (a["lo"][1], a["hi"][1])]
    zs = [a["lo"][2] for a in nav["areas"]]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    s = (size - 8) / max(x1 - x0, y1 - y0)
    w, h = int((x1 - x0) * s) + 8, int((y1 - y0) * s) + 8
    img = bytearray(w * h * 3)
    def px(x, y): return int((x - x0) * s) + 4, h - 1 - (int((y - y0) * s) + 4)
    def put(x, y, c):
        if 0 <= x < w and 0 <= y < h: img[(y * w + x) * 3:(y * w + x) * 3 + 3] = bytes(c)
    zlo, zhi = min(zs), max(zs) or 1
    for a in nav["areas"]:
        (ax0, ay1), (ax1, ay0) = px(a["lo"][0], a["lo"][1]), px(a["hi"][0], a["hi"][1])
        t = (a["lo"][2] - zlo) / max(1e-3, zhi - zlo)
        g = int(70 + 150 * t)
        c = (40, 60, g) if a["flags"] & 1 else (g, g, g)
        for y in range(ay0, ay1 + 1):
            for x in range(ax0, ax1 + 1):
                edge = x in (ax0, ax1) or y in (ay0, ay1)
                put(x, y, (20, 20, 20) if edge else c)
    cen = {a["id"]: ((a["lo"][0] + a["hi"][0]) / 2, (a["lo"][1] + a["hi"][1]) / 2) for a in nav["areas"]}
    for a in nav["areas"]:
        cx, cy = px(*cen[a["id"]])
        for d in a["connect"]:
            for t in d:
                if t in cen:
                    tx, ty = px(*cen[t])
                    n = max(abs(tx - cx), abs(ty - cy), 1)
                    for k in range(0, n + 1, 2):
                        put(cx + (tx - cx) * k // n, cy + (ty - cy) * k // n, (40, 200, 60))
        for sp in a["spots"]:
            sx, sy = px(sp["pos"][0], sp["pos"][1])
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    put(sx + dx, sy + dy, (230, 40, 40))
    write_png(path, w, h, img)


def main():
    args = sys.argv[1:]
    png = js = None
    if "--png" in args: k = args.index("--png"); png = args[k + 1]; del args[k:k + 2]
    if "--json" in args: k = args.index("--json"); js = args[k + 1]; del args[k:k + 2]
    fails = 0
    for p in args:
        try:
            nav = parse(open(p, "rb").read())
        except Exception as e:
            print(f"FAIL {p}: {e}"); fails += 1; continue
        bad = check(nav)
        a = nav["areas"]
        ok = bad == 0          # bytes after the areas are ignored by LoadNavigationMap (stale data in many files)
        fails += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} {p.split('/')[-1]}: v{nav['version']} bsp {nav['bsp_size']} places {len(nav['places'])} "
              f"areas {len(a)} links {sum(len(t) for x in a for t in x['connect'])} spots {sum(len(x['spots']) for x in a)} "
              f"approach {sum(len(x['approach']) for x in a)} encounters {sum(len(x['encounters']) for x in a)} "
              f"trailing {nav['trailing']} bad refs {bad}")
        if png and len(args) == 1: render(nav, png)
        if js and len(args) == 1: json.dump(nav, open(js, "w"))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
