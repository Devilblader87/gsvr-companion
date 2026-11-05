#!/usr/bin/env python3
"""Hub textures of the glue pack (docs/w3_hub_plan.md section 3, milestone H2): tileable wood / stone / ground tiles and a banner crest
sheet, cut from the player's own install at pack time and written as two sections of war3/__glue:

  HTEX   i32 n, then n entries of 7 i32 {nameOff, w, h, mips + 256, dataOff, dataBytes, flags}, the NUL-terminated names, then per entry
         the DXT5 mip chain (largest level first, rows bottom-up like TATL / MATX, down to 4 x 4 texels; mips + 256 = DXT5 like MATX).
         All offsets are relative to the section start, data blocks are 4-aligned. flags bit 0: the tile is tileable (Repeat wrap).
  HINF   text lines, one per fact:
           hubver=1
           tile <name> <metres per repeat> <tint r> <tint g> <tint b> <install path>
           crest <race> <u0> <v0> <u1> <v1>          rect in the crest sheet "crests" (v up)
           ok <name> / miss <name>                   what the converter got
  (HSND, the tavern ambience, comes with H3.)

The world (W3HubArt) reads both by tag and falls back to the fallback colours of W3HubLayout when they are missing; an old world ignores
them. Nothing here is committed: the tiles exist only in packs built on the player's PC (work/ in tests).

  python3 tools/w3hub.py [--install input/war3_131] [--out work/hub]    -> PNG previews of every tile + a size report
"""
import argparse
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

HUB_VERSION = 1
BLOCK_CEILING = 6 * 1024 * 1024          # plan 3.5: hub block <= 6 MB
SEAM_LIMIT = 1.6                         # seam score (see seam_score) below this = seamless enough, else blended

# name, size, kind, sources (first one the install has wins), metres per repeat, tint
#   terrain: the largest square of opaque 64-px cells (terrain files hold blend cells with holes; 512-wide ones an opaque half)
#   strip:   a plank strip repeated as rows (row k shifted by a fixed offset so the boards do not line up), param = rows
#   whole:   the entire picture
TILES = (
    ('stoneWall', 512, 'terrain', ('TerrainArt\\Ruins\\Ruins_LargeBricks.blp', 'TerrainArt\\CityScape\\City_SquareTiles.blp',
                                   'TerrainArt\\CityScape\\City_BrickTiles.blp'), 3.0, (1.0, 1.0, 1.0), 0),
    ('floor', 512, 'strip', ('UI\\Glues\\SinglePlayer\\HumanCampaign3D\\HumanCampaignWoodMetalTrim.blp',), 3.0, (0.9, 0.9, 0.9), 8),
    ('wood', 256, 'strip', ('UI\\Glues\\SinglePlayer\\HumanCampaign3D\\HumanCampaignWoodMetalTrim.blp',), 1.0, (0.9, 0.9, 0.9), 4),
    ('woodDark', 256, 'whole', ('UI\\Glues\\MainMenu\\MainMenu3D\\TreeBark.blp',
                                'UI\\Glues\\MainMenu\\MainMenu3D\\ShieldWood.blp'), 1.2, (0.8, 0.8, 0.8), 0),
    ('flag', 256, 'terrain', ('TerrainArt\\Village\\Village_StonePath.blp', 'TerrainArt\\BlackCitadel\\Citadel_FlatStones.blp',
                              'TerrainArt\\Village\\Village_CobblePath.blp', 'TerrainArt\\LordaeronSummer\\Lords_Rock.blp'), 1.5, (1.0, 1.0, 1.0), 0),
    ('ground', 256, 'terrain', ('TerrainArt\\LordaeronSummer\\Lords_Dirt.blp', 'TerrainArt\\LordaeronSummer\\Lords_Rock.blp'),
     2.0, (1.0, 1.0, 1.0), 0),
    ('hearth', 256, 'terrain', ('TerrainArt\\Dungeon\\Cave_Brick.blp', 'TerrainArt\\LordaeronSummer\\Lords_Rock.blp'),
     1.5, (0.8, 0.8, 0.8), 0),
)
CREST_W, CREST_H, CREST_CELL_W = 1024, 512, 256
# race -> candidate sources: a banner picture (cell filled), or an icon (64 x 64, centred in the cell's upper part)
CRESTS = (
    ('human', ('UI\\Glues\\SinglePlayer\\HumanCampaign3D\\HumanCampaignBanner.blp',), 'banner'),
    ('orc', ('UI\\Glues\\MainMenu\\MainMenu3D\\Banner_Orc2.blp',), 'banner'),
    ('undead', ('UI\\Widgets\\BattleNet\\ChatIcons\\Tier1-Undead.blp',), 'icon'),
    ('nightelf', ('UI\\Widgets\\BattleNet\\ChatIcons\\Tier1-NightElf.blp',), 'icon'),
)


# ------------------------------------------------------------------ image helpers

def _blp(core, path):
    import w3pack
    raw = core.read(path, missing_ok=True)
    if not raw:
        return None
    try:
        return w3pack.blp_rgba(raw)
    except Exception:                                   # noqa: BLE001 — an undecodable texture is a miss, never fatal
        return None


def opaque_block(im, cell=64):
    """largest square block of fully opaque cells of an RGBA image -> (x0, y0, size) in pixels, or None."""
    a = im.getchannel('A')
    gw, gh = im.size[0] // cell, im.size[1] // cell
    ok = [[a.crop((x * cell, y * cell, x * cell + cell, y * cell + cell)).getextrema()[0] >= 250 for x in range(gw)] for y in range(gh)]
    for k in range(min(gw, gh), 0, -1):
        for y in range(gh - k + 1):
            for x in range(gw - k + 1):
                if all(ok[y + j][x + i] for j in range(k) for i in range(k)):
                    return x * cell, y * cell, k * cell
    return None


def seam_score(im):
    """how visible the wrap-around seam is: mean |difference| between the last and first column / row, divided by the mean
    |difference| of ordinary neighbouring columns / rows. ~1 = invisible, a hard seam is 2 .. 10."""
    from PIL import ImageChops, ImageStat
    g = im.convert('L')
    w, h = g.size

    def mad(a, b):
        return ImageStat.Stat(ImageChops.difference(a, b)).mean[0]
    inner_x = mad(g.crop((0, 0, w - 1, h)), g.crop((1, 0, w, h)))
    inner_y = mad(g.crop((0, 0, w, h - 1)), g.crop((0, 1, w, h)))
    seam_x = mad(g.crop((w - 1, 0, w, h)), g.crop((0, 0, 1, h)))
    seam_y = mad(g.crop((0, h - 1, w, h)), g.crop((0, 0, w, 1)))
    inner = max(0.5, (inner_x + inner_y) / 2.0)
    return ((seam_x + seam_y) / 2.0) / inner


def _quiet_cut(g, n, horizontal):
    """position c in the middle half of 0..n where columns (horizontal) / rows c-1 and c differ least (a cut that crosses no board
    edge or mortar line)."""
    from PIL import Image, ImageChops
    w, h = g.size
    if horizontal:
        d = ImageChops.difference(g.crop((0, 0, w - 1, h)), g.crop((1, 0, w, h))).resize((w - 1, 1), Image.BOX)
    else:
        d = ImageChops.difference(g.crop((0, 0, w, h - 1)), g.crop((0, 1, w, h))).resize((1, h - 1), Image.BOX)
    v = list(d.tobytes())                                # v[c - 1] = difference between c - 1 and c
    best = min(range(n // 4, 3 * n // 4 + 1), key=lambda c: (v[c - 1], abs(c - n // 2)))
    return best


def make_tileable(im):
    """offset cross-fade: out = w * img + (1 - w) * shifted img, w = 0 on every border and ~1 in the middle. The image is shifted so that
    its own cut (the quietest column / row near the middle, see _quiet_cut) lands on the border: the border pixels are then two
    neighbouring pixels of the original (no seam) and the shifted copy's own wrap seam falls where w = 1 (invisible)."""
    from PIL import Image, ImageChops
    import math
    w, h = im.size
    g = im.convert('L')
    cx, cy = _quiet_cut(g, w, True), _quiet_cut(g, h, False)
    shifted = ImageChops.offset(im, (w - cx) % w, (h - cy) % h)

    def ramp(n):
        return [min(1.0, math.sin(math.pi * (i + 0.5) / n) * 1.6) for i in range(n)]
    rx, ry = ramp(w), ramp(h)
    mask = Image.new('L', (w, h))
    mask.putdata([int(round(255 * min(rx[x], ry[y]))) for y in range(h) for x in range(w)])
    return Image.composite(im, shifted, mask)


def _fit(im, size):
    from PIL import Image
    return im.resize((size, size), Image.LANCZOS if size >= im.size[0] else Image.BOX)


def build_tile(core, spec):
    """-> (RGBA image size x size, tileable, source path) or None (miss)."""
    from PIL import Image
    name, size, kind, sources, _mpr, _tint, param = spec
    for path in sources:
        im = _blp(core, path)
        if im is None:
            continue
        if kind == 'terrain':
            blk = opaque_block(im, max(8, im.size[1] // 4))
            if blk is None:
                continue
            x0, y0, s = blk
            if s < 128:         # a single 64-px cell is too small to blow up
                continue
            tile = _fit(im.crop((x0, y0, x0 + s, y0 + s)), size)
        elif kind == 'strip':
            rows = param
            rh = size // rows
            strip = im.resize((size, rh), Image.LANCZOS)
            tile = Image.new('RGBA', (size, size))
            for k in range(rows):
                from PIL import ImageChops
                tile.paste(ImageChops.offset(strip, (k * 97) % size, 0), (0, k * rh))
        else:
            tile = _fit(im, size)
        tile = tile.convert('RGB')
        score = seam_score(tile)
        if score > SEAM_LIMIT:
            tile = make_tileable(tile)
        return tile.convert('RGBA'), path
    return None


def build_crests(core):
    """-> (RGBA sheet CREST_W x CREST_H, {race: path}) ; cells of CREST_CELL_W x CREST_H side by side in CRESTS order."""
    from PIL import Image
    sheet = Image.new('RGBA', (CREST_W, CREST_H), (0, 0, 0, 0))
    got = {}
    for i, (race, sources, kind) in enumerate(CRESTS):
        for path in sources:
            im = _blp(core, path)
            if im is None:
                continue
            if kind == 'banner':
                cell = im.resize((CREST_CELL_W, CREST_H), Image.LANCZOS)
            else:
                cell = Image.new('RGBA', (CREST_CELL_W, CREST_H), (0, 0, 0, 0))
                cell.paste(im.resize((CREST_CELL_W - 32, CREST_CELL_W - 32), Image.LANCZOS), (16, 48))
            sheet.paste(cell, (i * CREST_CELL_W, 0))
            got[race] = path
            break
    return sheet, got


# ------------------------------------------------------------------ DXT5

def mip_levels(img):
    """RGBA image (power-of-two sides >= 4) -> [level images] from the full size down to 4 texels on the short side."""
    from PIL import Image
    out = [img]
    while min(out[-1].size) > 4:
        w, h = out[-1].size
        out.append(out[-1].resize((max(4, w // 2), max(4, h // 2)), Image.BOX))
    return out


def encode_chain(img):
    """-> (mip count, bytes): DXT5 blocks of every level, rows bottom-up (the image is flipped before encoding like UIAT / MATX)."""
    from PIL import Image
    import w3pack_models
    levels = mip_levels(img)
    data = b''.join(w3pack_models.dxt5(l.transpose(Image.FLIP_TOP_BOTTOM)) for l in levels)
    return len(levels), data


def decode_chain(w, h, mips, data):
    """DXT5 chain -> [RGBA images top-down per level] (test helper: uses Pillow's DDS reader)."""
    import io
    from PIL import Image
    out, off = [], 0
    for l in range(mips):
        lw, lh = max(4, w >> l), max(4, h >> l)
        n = (lw // 4) * (lh // 4) * 16
        dds = bytearray(128)
        dds[0:4] = b'DDS '
        struct.pack_into('<7I', dds, 4, 124, 0x1007 | 0x80000, lh, lw, n, 0, 0)
        struct.pack_into('<I', dds, 76, 32)
        struct.pack_into('<I4s', dds, 80, 4, b'DXT5')
        struct.pack_into('<I', dds, 108, 0x1000)
        im = Image.open(io.BytesIO(bytes(dds) + data[off:off + n])).convert('RGBA')
        out.append(im.transpose(Image.FLIP_TOP_BOTTOM))
        off += n
    return out


# ------------------------------------------------------------------ sections

def pack_htex(entries):
    """entries [(name, w, h, mips, flags, data)] -> HTEX bytes."""
    n = len(entries)
    head = 4 + 28 * n
    names = bytearray()
    name_off = []
    for e in entries:
        name_off.append(head + len(names))
        names += e[0].encode('ascii') + b'\0'
    off = head + len(names)
    off += (-off) % 4
    table, body = bytearray(), bytearray()
    for (name, w, h, mips, flags, data), no in zip(entries, name_off):
        table += struct.pack('<7i', no, w, h, mips + 256, off + len(body), len(data), flags)
        body += data + b'\0' * ((-len(data)) % 4)
    blob = bytearray(struct.pack('<i', n)) + table + names
    blob += b'\0' * (off - len(blob))
    return bytes(blob + body)


def read_htex(blob):
    """the reader the world implements (W3HubArt.LoadPack): HTEX bytes -> [(name, w, h, mips, dxt, flags, data)]."""
    n = struct.unpack_from('<i', blob, 0)[0]
    if n < 0 or 4 + 28 * n > len(blob):
        raise ValueError('HTEX header')
    out = []
    for i in range(n):
        no, w, h, m, do, db, fl = struct.unpack_from('<7i', blob, 4 + 28 * i)
        end = blob.index(b'\0', no)
        dxt = m >= 256
        if dxt:
            m -= 256
        need = sum((max(4, w >> l) // 4) * (max(4, h >> l) // 4) * 16 for l in range(m)) if dxt else 0
        if w <= 0 or h <= 0 or m < 1 or m > 13 or do % 4 or do + db > len(blob) or db != need:
            raise ValueError('HTEX entry %d' % i)
        out.append((blob[no:end].decode('ascii'), w, h, m, dxt, fl, blob[do:do + db]))
    return out


def hub_sections(core, log=print):
    """-> [(b'HTEX', data, n), (b'HINF', text, lines)] or [] when no tile could be made. Never raises for a missing / broken texture."""
    from w3pack_models import dxt5_supported
    if not dxt5_supported():
        log('  hub: this Pillow writes no DXT5 (needs >= 11.2): no hub textures')
        return []
    entries, info = [], ['hubver=%d' % HUB_VERSION]
    for spec in TILES:
        name, size, _k, _s, mpr, tint, _p = spec
        try:
            r = build_tile(core, spec)
        except Exception as e:                           # noqa: BLE001
            log('  hub: %s failed: %s: %s' % (name, type(e).__name__, e))
            r = None
        if r is None:
            info.append('miss ' + name)
            continue
        img, path = r
        mips, data = encode_chain(img)
        entries.append((name, size, size, mips, 1, data))
        info.append('tile %s %g %g %g %g %s' % ((name, mpr) + tint + (path,)))
        info.append('ok ' + name)
    try:
        sheet, got = build_crests(core)
    except Exception as e:                               # noqa: BLE001
        log('  hub: crests failed: %s: %s' % (type(e).__name__, e))
        sheet, got = None, {}
    if got:
        mips, data = encode_chain(sheet)
        entries.append(('crests', CREST_W, CREST_H, mips, 0, data))
        info.append('ok crests')
        for i, (race, _s, _k) in enumerate(CRESTS):
            if race in got:
                info.append('crest %s %.6f 0 %.6f 1' % (race, i * CREST_CELL_W / float(CREST_W), (i + 1) * CREST_CELL_W / float(CREST_W)))
            else:
                info.append('miss crest-' + race)
    elif sheet is not None:
        info.append('miss crests')
    if not entries:
        log('  hub: no tile found in this install, hub block left out')
        return []
    blob = pack_htex(entries)
    log('  hub: %d textures (%d tiles + crests), HTEX %.2f MB' % (len(entries), sum(1 for e in entries if e[0] != 'crests'), len(blob) / 1048576.0))
    return [(b'HTEX', blob, len(entries)), (b'HINF', ('\n'.join(info) + '\n').encode('utf-8'), len(info))]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--install', default=os.path.join(HERE, '..', 'input', 'war3_131'))
    ap.add_argument('--out', default=os.path.join(HERE, '..', 'work', 'hub'))
    a = ap.parse_args()
    import war3fs
    core = war3fs.open_install(a.install)
    os.makedirs(a.out, exist_ok=True)
    secs = hub_sections(core)
    for tag, data, _n in secs:
        print(tag.decode(), len(data), 'bytes')
        if tag == b'HINF':
            print(data.decode())
        else:
            for name, w, h, m, _d, _f, raw in read_htex(data):
                decode_chain(w, h, m, raw)[0].save(os.path.join(a.out, 'tile_%s.png' % name))
    return 0


if __name__ == '__main__':
    sys.exit(main())
