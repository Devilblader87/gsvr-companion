#!/usr/bin/env python3
"""The lobby's map info (MapInfoPane: name, suggested players, size, tileset, description; list icon melee / UMS)
from a map's war3map.w3i (+ war3map.wts for TRIGSTR) without converting the map. The companion puts it into
index.json (docs/ui_plan.md L5).

  python3 tools/w3info.py <map.w3x | key> [--install input/war3]
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import w3mpq as mpq                       # noqa: E402
import w3map                     # noqa: E402

DESC_MAX = 600                   # characters kept of the description (the pane shows ~0.10625 of height)
FLAG_MELEE = 0x0004              # w3i flag "melee map" [ext: w3i format docs]


def plain(s):
    """WC3 colour markup off (|cAARRGGBB, |r), |n -> newline"""
    s = re.sub(r'\|[cC][0-9a-fA-F]{8}', '', s or '')
    return s.replace('|r', '').replace('|R', '').replace('|n', '\n').replace('|N', '\n').replace('\r', '').strip()


class Names:
    """The game's words for size and tileset, from the player's install (NOTES_glue.md "MapInfoPane content"):
    [MapSizes] Size%02d = "max area,WESTRING key" (first entry with playable w*h <= max wins [obs 0x6f3e4afc]) and
    [TileSets] letter = "WESTRING key,..." in UI\\WorldEditData.txt, the keys resolved in UI\\WorldEditStrings.txt."""

    def __init__(self, core):
        def sect(txt, name):
            m = re.search(r'^\[' + name + r'\]\s*$(.*?)(?=^\[|\Z)', txt, re.S | re.M)
            out = {}
            for ln in (m.group(1).splitlines() if m else []):
                if '=' in ln and not ln.startswith('//'):
                    k, v = ln.split('=', 1)
                    out[k.strip()] = v.strip()
            return out
        data = (core.read('UI\\WorldEditData.txt', missing_ok=True) or b'').decode('utf-8', 'replace')
        self.words = {}
        for f in ('UI\\WorldEditStrings.txt', 'UI\\WorldEditGameStrings.txt'):   # the size / locale keys live in the latter
            txt = (core.read(f, missing_ok=True) or b'').decode('utf-8', 'replace')
            for ln in txt.splitlines():
                if '=' in ln and not ln.startswith(('//', '[')):
                    k, v = ln.split('=', 1)
                    self.words[k.strip()] = v.strip()
        ms = sect(data, 'MapSizes')
        self.sizes = []
        for i in range(int(ms.get('NumSizes', '0') or 0)):
            v = ms.get('Size%02d' % i, '').split(',')
            if len(v) >= 2 and v[0].strip().isdigit():
                self.sizes.append((int(v[0]), v[1].strip()))
        self.tilesets = {k: v.split(',')[0].strip() for k, v in sect(data, 'TileSets').items() if len(k) == 1}

    def word(self, key):
        return self.words.get(key, '').strip('"') or key

    def size(self, w, h):
        a = w * h
        for mx, key in self.sizes:
            if a <= mx:
                return self.word(key)
        return self.word('WESTRING_UNKNOWN') if self.sizes else ''

    def tileset(self, letter):
        k = self.tilesets.get(letter)
        return self.word(k) if k else ''


def info_from_archive(arc, names=None):
    raw = arc.read('war3map.w3i', missing_ok=True)
    if not raw:
        return None
    wts_raw = arc.read('war3map.wts', missing_ok=True)
    wts = w3map.parse_wts(wts_raw) if wts_raw else None
    i = w3map.parse_w3i(raw, wts)
    humans = sum(1 for p in i.get('players', []) if p.get('type') == 'human')
    t = i.get('tileset', '')
    m = re.match(r'. \((.*)\)$', t)
    letter = t[:1]
    out = {'title': plain(i.get('name', '')), 'author': plain(i.get('author', '')),
            'players': humans or len(i.get('players', [])), 'suggested': plain(i.get('players_recommended', '')),
            'w': i.get('playable_w', 0), 'h': i.get('playable_h', 0),
            'tileset': m.group(1) if m else t, 'melee': bool(i.get('flags', 0) & FLAG_MELEE),
            'desc': plain(i.get('description', ''))[:DESC_MAX],
            'ltitle': plain(i.get('loading_title', '')), 'lsub': plain(i.get('loading_subtitle', '')),
            'ltext': plain(i.get('loading_text', ''))[:DESC_MAX]}
    if names is not None:
        out['size'] = names.size(out['w'], out['h'])
        out['tileset'] = names.tileset(letter) or out['tileset']
    return out


# ---------------------------------------------------------------- preview (MinimapImage)

MMP_ICONS = ('MinimapIconGold', 'MinimapIconNeutralBuilding', 'MinimapIconStartLoc', 'MinimapIconCreepCampSmall',
             'MinimapIconCreepCampLarge')   # mmp icon types 0..4 [obs 0x6f3de405] (keys of [WorldEditArt])
PREVIEW_PX = 128                            # atlas cell; the game sizes it to the MinimapImage width


class Art:
    """the marker icons (16 x 16) and the default image from the player's install"""

    def __init__(self, core):
        import w3pack
        self.core, self.blp = core, w3pack.blp_rgba
        data = (core.read('UI\\WorldEditData.txt', missing_ok=True) or b'').decode('utf-8', 'replace')
        m = re.search(r'^\[WorldEditArt\]\s*$(.*?)(?=^\[|\Z)', data, re.S | re.M)
        paths = {}
        for ln in (m.group(1).splitlines() if m else []):
            if '=' in ln and not ln.startswith('//'):
                k, v = ln.split('=', 1)
                paths[k.strip()] = v.strip()
        self.icons = []
        for k in MMP_ICONS:
            im = self.image(paths.get(k))
            self.icons.append(im.resize((16, 16)) if im is not None else None)
        self.unknown = self.image('UI\\Widgets\\Glues\\Minimap-Unknown.blp')

    def image(self, path, arc=None):
        if not path:
            return None
        raw = (arc.read(path, missing_ok=True) if arc is not None else None) or (
            self.core.read(path, missing_ok=True) if arc is None else None)
        if not raw:
            return None
        try:
            if path.lower().endswith('.tga'):
                import io
                from PIL import Image
                return Image.open(io.BytesIO(raw)).convert('RGBA')
            return self.blp(raw).convert('RGBA')
        except Exception:        # noqa: BLE001
            return None


def preview(arc, art):
    """The MinimapImage of the lobby (NOTES_glue.md "MapInfoPane content"): war3mapPreview.tga as is, else
    war3mapMap.tga / .blp with the war3map.mmp markers, else the default image; w3i flag 0x1 (hide the minimap in
    preview screens) -> the default. -> PIL RGBA PREVIEW_PX square or None."""
    import struct
    raw = arc.read('war3map.w3i', missing_ok=True)
    hide = False
    if raw:
        try:
            hide = bool(w3map.parse_w3i(raw).get('flags', 0) & 0x1)
        except Exception:        # noqa: BLE001
            pass
    im = None if hide else art.image('war3mapPreview.tga', arc)
    if im is None and not hide:
        im = art.image('war3mapMap.tga', arc) or art.image('war3mapMap.blp', arc)
        mmp = arc.read('war3map.mmp', missing_ok=True) if im is not None else None
        if mmp and len(mmp) >= 8:
            im = im.copy()
            n = struct.unpack_from('<i', mmp, 4)[0]
            for i in range(max(0, min(n, (len(mmp) - 8) // 16))):
                typ, x, y = struct.unpack_from('<3i', mmp, 8 + 16 * i)
                b, g, r, a = mmp[20 + 16 * i:24 + 16 * i]     # [inf BGRA like the game's colours]
                ic = art.icons[typ] if 0 <= typ < len(art.icons) else None
                if ic is None:
                    continue
                if (r, g, b) != (255, 255, 255):
                    from PIL import ImageChops, Image
                    ic = ImageChops.multiply(ic, Image.new('RGBA', ic.size, (r, g, b, 255)))
                px = min(max(0, x - 8), im.size[0] - 16)
                py = min(max(0, y - 8), im.size[1] - 16)
                im.alpha_composite(ic, (px, py))
    if im is None:
        im = art.unknown
    return im.resize((PREVIEW_PX, PREVIEW_PX)) if im is not None else None


def previews_pack(items, art, log=print):
    """[(key, archive)] -> W3P1 bytes with PVIX ("key col row" lines, cell px) + PVAT (w, h, 3 = RGB24, rows
    bottom-up) for the world's MapInfoPane (one download for every map)."""
    import struct
    from PIL import Image
    cells = []
    for key, arc in items:
        try:
            im = preview(arc, art)
        except Exception:        # noqa: BLE001
            im = None
        if im is not None:
            cells.append((key, im))
    per = 2048 // PREVIEW_PX
    rows = max(1, -(-len(cells) // per))
    W, H = per * PREVIEW_PX, rows * PREVIEW_PX
    page = Image.new('RGB', (W, H), (0, 0, 0))
    lines = ['C %d' % PREVIEW_PX]
    for i, (key, im) in enumerate(cells):
        c, r = i % per, i // per
        bg = Image.new('RGBA', im.size, (0, 0, 0, 255))
        bg.alpha_composite(im)
        page.paste(bg.convert('RGB'), (c * PREVIEW_PX, r * PREVIEW_PX))
        lines.append('P %d %d %s' % (c, rows - 1 - r, key))       # row counted from the bottom (texture v)
    ix = ('\n'.join(lines) + '\n').encode('utf-8')
    at = struct.pack('<3i', W, H, 3) + page.transpose(Image.FLIP_TOP_BOTTOM).tobytes()
    secs = [(b'PVIX', ix), (b'PVAT', at)]
    head = 16 + 16 * len(secs)
    d, body, off = b'', b'', head
    for tag, data in secs:
        d += struct.pack('<4s3i', tag, off, len(data), 1)
        body += data
        off += len(data)
    log('previews: %d maps, atlas %dx%d (%.1f MB)' % (len(cells), W, H, len(at) / 1048576.0))
    return struct.pack('<4s3i', b'W3P1', 1, len(secs), 0) + d + body


def map_info(path=None, data=None, names=None):
    """-> dict or None (not a readable map). names: Names(core install) for the game's size / tileset words."""
    try:
        return info_from_archive(mpq.MPQ(data if data is not None else path), names)
    except Exception:            # noqa: BLE001 — a broken map must not stop the index
        return None


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('map')
    ap.add_argument('--install', default=os.path.join(HERE, '..', 'input', 'war3'))
    a = ap.parse_args()
    import war3fs
    fs = war3fs.open_install(a.install)
    names = Names(fs)
    if os.path.isfile(a.map):
        print(json.dumps(map_info(a.map, names=names), indent=1, ensure_ascii=False))
        return
    hit = fs.maps().get(a.map.lower())
    print(json.dumps(map_info(data=fs.read_map(hit[1]), names=names) if hit else None, indent=1, ensure_ascii=False))


if __name__ == '__main__':
    main()
