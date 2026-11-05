#!/usr/bin/env python3
"""Parsers for Warcraft III map data files (clean-room from public format notes).

  w3i  map info            parse_w3i(bytes, wts) -> dict
  w3e  terrain             parse_w3e(bytes) -> Terrain
  w3r  regions             parse_w3r(bytes) -> [dict]
  doo  doodads / units     parse_doo(bytes), parse_units_doo(bytes)
  w3u/w3t/w3b/w3d/w3a/w3q/w3h object data  parse_objects(bytes, ext)
  wts  trigger strings     parse_wts(bytes)
"""
import re
import struct
import sys


class Reader:
    def __init__(self, data, pos=0):
        self.d = data
        self.p = pos

    def u8(self):
        v = self.d[self.p]
        self.p += 1
        return v

    def i32(self):
        v = struct.unpack_from('<i', self.d, self.p)[0]
        self.p += 4
        return v

    def u32(self):
        v = struct.unpack_from('<I', self.d, self.p)[0]
        self.p += 4
        return v

    def i16(self):
        v = struct.unpack_from('<h', self.d, self.p)[0]
        self.p += 2
        return v

    def f32(self):
        v = struct.unpack_from('<f', self.d, self.p)[0]
        self.p += 4
        return v

    def raw(self, n):
        v = self.d[self.p:self.p + n]
        self.p += n
        return v

    def id4(self):
        return self.raw(4).decode('latin-1')

    def cstr(self):
        e = self.d.index(b'\0', self.p)
        v = self.d[self.p:e].decode('utf-8', 'replace')
        self.p = e + 1
        return v

    def left(self):
        return len(self.d) - self.p


# ---------------------------------------------------------------- wts

def parse_wts(data):
    txt = data.decode('utf-8', 'replace').lstrip('﻿')
    out = {}
    for m in re.finditer(r'STRING\s+(\d+)[^{]*\{\r?\n(.*?)\r?\n\}', txt, re.S):
        out[int(m.group(1))] = m.group(2)
    return out


def resolve(s, wts):
    if wts and s.startswith('TRIGSTR_'):
        try:
            return wts.get(int(s[8:].split()[0]), s)
        except ValueError:
            return s
    return s


# ---------------------------------------------------------------- w3i

TILESETS = {'A': 'Ashenvale', 'B': 'Barrens', 'C': 'Felwood', 'D': 'Dungeon', 'F': 'Lordaeron Fall',
            'G': 'Underground', 'L': 'Lordaeron Summer', 'N': 'Northrend', 'Q': 'Village Fall',
            'V': 'Village', 'W': 'Lordaeron Winter', 'X': 'Dalaran', 'Y': 'Cityscape',
            'Z': 'Sunken Ruins', 'I': 'Icecrown', 'J': 'Dalaran Ruins', 'O': 'Outland', 'K': 'Black Citadel'}
PLAYER_TYPES = {1: 'human', 2: 'computer', 3: 'neutral', 4: 'rescuable'}
RACES = {0: 'selectable', 1: 'human', 2: 'orc', 3: 'undead', 4: 'nightelf'}


def parse_w3i(data, wts=None):
    r = Reader(data)
    info = {'version': r.i32()}
    v = info['version']
    info['saves'] = r.i32()
    info['editor_version'] = r.i32()
    if v >= 28:       # [obs Legion_TD_3.46d, w3i 31]: four u32 game version (1, 32, 10, 18067)
        info['game_version'] = [r.i32() for _ in range(4)]
    info['name'] = resolve(r.cstr(), wts)
    info['author'] = resolve(r.cstr(), wts)
    info['description'] = resolve(r.cstr(), wts)
    info['players_recommended'] = resolve(r.cstr(), wts)
    info['camera_bounds'] = [r.f32() for _ in range(8)]
    info['camera_complements'] = [r.i32() for _ in range(4)]
    info['playable_w'] = r.i32()
    info['playable_h'] = r.i32()
    info['flags'] = r.u32()
    t = chr(r.u8())
    info['tileset'] = '%s (%s)' % (t, TILESETS.get(t, '?'))
    if v >= 25:
        info['loading_bg'] = r.i32()
        info['loading_model'] = r.cstr()
        info['loading_text'] = resolve(r.cstr(), wts)
        info['loading_title'] = resolve(r.cstr(), wts)
        info['loading_subtitle'] = resolve(r.cstr(), wts)
        info['game_data_set'] = r.i32()
        info['prologue_path'] = r.cstr()
        for k in ('prologue_text', 'prologue_title', 'prologue_subtitle'):
            info[k] = resolve(r.cstr(), wts)
        info['fog'] = {'type': r.i32(), 'start': r.f32(), 'end': r.f32(), 'density': r.f32(),
                       'rgba': list(r.raw(4))}
        info['weather'] = r.id4() if data[r.p:r.p + 4] != b'\0\0\0\0' else (r.raw(4) and None)
        info['sound_env'] = r.cstr()
        info['light_tileset'] = chr(r.u8())
        info['water_rgba'] = list(r.raw(4))
    else:
        info['loading_screen'] = r.i32()
        for k in ('loading_text', 'loading_title', 'loading_subtitle'):
            info[k] = resolve(r.cstr(), wts)
        info['prologue_screen'] = r.i32()
        for k in ('prologue_text', 'prologue_title', 'prologue_subtitle'):
            info[k] = resolve(r.cstr(), wts)
    # Reforged format 31 [obs Legion_TD_3.46d.w3i, 811 bytes; the four game-version u32 after the editor version above]: after the water tint three u32 (0, 3, 1: script
    # language, supported modes, game data version [ext]), and every player record ends with two more u32
    # (enemy low / high masks, after ally low / high).
    info['missing'] = []
    if v >= 31:
        try:
            info['script_language'], info['supported_modes'], info['game_data_version'] = r.i32(), r.i32(), r.i32()
        except struct.error:
            info['missing'].append('v31 header')
    players = []
    try:
        for _ in range(r.i32()):
            p = {'id': r.i32(), 'type': PLAYER_TYPES.get(r.i32(), '?'), 'race': RACES.get(r.i32(), '?'),
                 'fixed_start': r.i32(), 'name': resolve(r.cstr(), wts), 'start': (r.f32(), r.f32()),
                 'ally_low': r.u32(), 'ally_high': r.u32()}
            if v >= 31:
                p['enemy_low'], p['enemy_high'] = r.u32(), r.u32()
            players.append(p)
    except (struct.error, ValueError):
        info['missing'].append('players (%d read)' % len(players))
    info['players'] = players
    forces = []
    try:
        for _ in range(r.i32()):
            forces.append({'flags': r.u32(), 'mask': r.u32(), 'name': resolve(r.cstr(), wts)})
    except (struct.error, ValueError):
        info['missing'].append('forces (%d read)' % len(forces))
    info['forces'] = forces
    if info['missing']:
        print('w3i: truncated (version %d, %d bytes), defaults for: %s' % (v, len(data), ', '.join(info['missing'])),
              file=sys.stderr)
    return info


# ---------------------------------------------------------------- w3e

class Terrain:
    pass


def parse_w3e(data):
    r = Reader(data)
    if r.raw(4) != b'W3E!':
        raise ValueError('not W3E')
    t = Terrain()
    t.version = r.i32()
    t.tileset = chr(r.u8())
    t.custom_tilesets = r.i32()
    t.ground = [r.id4() for _ in range(r.i32())]
    t.cliffs = [r.id4() for _ in range(r.i32())]
    t.w = r.i32()   # tile points (= tiles + 1)
    t.h = r.i32()
    t.ox = r.f32()
    t.oy = r.f32()
    n = t.w * t.h
    raw = data[r.p:r.p + 7 * n]
    if len(raw) < 7 * n:
        raise ValueError('w3e truncated')
    t.height = [0.0] * n        # world z of ground incl. cliff layer
    t.water = [0.0] * n
    t.tex = [0] * n
    t.flags = [0] * n
    t.layer = [0] * n
    t.cliff_tex = [0] * n
    t.variation = [0] * n
    for i in range(n):
        gh, wl, ft, var, cl = struct.unpack_from('<hhBBB', raw, i * 7)
        t.layer[i] = cl & 0x0F
        t.cliff_tex[i] = cl >> 4
        t.height[i] = (gh - 0x2000 + (t.layer[i] - 2) * 0x200) / 4.0
        t.water[i] = ((wl & 0x3FFF) - 0x2000) / 4.0 - 89.6
        t.flags[i] = (ft & 0xF0) | ((wl & 0x4000) >> 14)  # 0x10 ramp 0x20 blight 0x40 water 0x80 boundary, bit0=boundary(wl)
        t.tex[i] = ft & 0x0F
        t.variation[i] = var
    return t


def terrain_summary(t):
    n = t.w * t.h
    water = sum(1 for f in t.flags if f & 0x40)
    boundary = sum(1 for f in t.flags if f & 0x81)
    ramps = sum(1 for f in t.flags if f & 0x10)
    layers = sorted(set(t.layer))
    return {'tilepoints': [t.w, t.h], 'tiles': [t.w - 1, t.h - 1], 'origin': [t.ox, t.oy],
            'world_size': [(t.w - 1) * 128, (t.h - 1) * 128],
            'tileset': t.tileset, 'ground_tiles': t.ground, 'cliff_types': t.cliffs,
            'height_min': min(t.height), 'height_max': max(t.height),
            'cliff_levels': layers, 'water_points': water, 'boundary_points': boundary,
            'ramp_points': ramps, 'tex_hist': {t.ground[k] if k < len(t.ground) else str(k): t.tex.count(k)
                                               for k in sorted(set(t.tex))}}


# ---------------------------------------------------------------- w3r

def parse_w3r(data):
    """Regions. A protected map can carry a junk record count (Legion TD 3.46d: "FUCK" = 0x4b435546 after the version,
    BUGS W3R-JUNK): the records are read while they fit and the rest is dropped (logged), never an exception."""
    r = Reader(data)
    ver = r.i32()
    out = []
    n = r.i32()
    try:
        for _ in range(n):
            reg = {'left': r.f32(), 'bottom': r.f32(), 'right': r.f32(), 'top': r.f32(), 'name': r.cstr(),
                   'index': r.i32()}
            w = r.raw(4)
            reg['weather'] = w.decode('latin-1') if w != b'\0\0\0\0' else None
            reg['sound'] = r.cstr()
            b, g, rr, _ = r.raw(4)
            reg['color'] = (rr, g, b)
            out.append(reg)
    except (struct.error, ValueError):
        print('w3r: truncated (version %d, count %d, %d bytes), %d regions read' % (ver, n, len(data), len(out)),
              file=sys.stderr)
    return out


# ---------------------------------------------------------------- doo

def _doodads(data, ver, sub, n, skin):
    """the doodad records of a war3map.doo from offset 16; skin = an id4 after the scale (Reforged). ->
    (items, reader at the end of the records, error or None)"""
    r = Reader(data)
    r.p = 16
    items = []
    try:
        for _ in range(n):
            if r.left() < 4:
                raise ValueError('end')
            d = {'id': r.id4(), 'var': r.i32(), 'x': r.f32(), 'y': r.f32(), 'z': r.f32(), 'angle': r.f32(),
                 'scale': (r.f32(), r.f32(), r.f32())}
            if skin:
                d['skin'] = r.id4()
            d['flags'] = r.u8()
            d['life'] = r.u8()
            if ver >= 8:
                d['item_table'] = r.i32()
                ns = r.i32()
                if not 0 <= ns <= r.left():          # raw() never raises past the end: a junk count would spin
                    raise ValueError('item sets')
                for _s in range(ns):
                    ni = r.i32()
                    if not 0 <= ni <= r.left():
                        raise ValueError('item set')
                    r.raw(8 * ni)
            d['editor_id'] = r.i32()
            items.append(d)
    except (struct.error, ValueError, MemoryError, OverflowError):
        return items, r, True
    return items, r, False


def parse_doo(data):
    """Doodads. Version 8 / sub 11 comes in two layouts [obs]: with an id4 after the scale equal to the type id
    (Reforged skin id; Legion_TD_3.46d, 890 records ending exactly at the special-doodad block) and without it (a
    1.31-era map, 50 bytes per record). The layout whose records end where a special-doodad block (version, count,
    16 bytes each) ends the file is taken; a junk count (protected maps) keeps the records that parse, logged."""
    r = Reader(data)
    if r.raw(4) != b'W3do':
        raise ValueError('not W3do')
    ver = r.i32()
    sub = r.i32()
    n = r.i32()
    tries = (True, False) if (ver >= 8 and sub >= 11) else (False,)
    best = None
    for skin in tries:
        items, rr, bad = _doodads(data, ver, sub, n, skin)
        left = rr.left()
        cnt = struct.unpack_from('<i', data, rr.p + 4)[0] if left >= 8 else 0
        fits = not bad and (left == 0 or (left >= 8 and 0 <= cnt <= left and left == 8 + 16 * cnt))
        if fits:
            best = (items, rr, False)
            break
        if best is None or len(items) > len(best[0]):
            best = (items, rr, bad or True)
    items, rr, bad = best
    if bad:
        print('doo: truncated / odd (version %d.%d, count %d, %d bytes), %d doodads read' %
              (ver, sub, n, len(data), len(items)), file=sys.stderr)
        return {'version': ver, 'subversion': sub, 'doodads': items, 'special': []}
    special = []
    r = rr
    if r.left() >= 8:
        r.i32()
        try:
            for _ in range(r.i32()):
                special.append({'id': r.id4(), 'z': r.i32(), 'x': r.i32(), 'y': r.i32()})
        except (struct.error, ValueError):
            print('doo: special doodads truncated, %d read' % len(special), file=sys.stderr)
    return {'version': ver, 'subversion': sub, 'doodads': items, 'special': special}


def _units_doo(data, ver, sub, skin):
    """the unit records of a war3mapUnits.doo; skin = an id4 after the scale (Reforged-era editors). -> (units, end offset)."""
    r = Reader(data)
    r.raw(12)
    units = []
    for _ in range(r.i32()):
        u = {'id': r.id4(), 'var': r.i32(), 'x': r.f32(), 'y': r.f32(), 'z': r.f32(), 'angle': r.f32(),
             'scale': (r.f32(), r.f32(), r.f32())}
        if skin:
            u['skin'] = r.id4()
        u['flags'] = r.u8()
        u['player'] = r.i32()
        r.u8(); r.u8()
        u['hp'] = r.i32()
        u['mp'] = r.i32()
        if sub >= 11:
            r.i32()                     # item table pointer
        for _s in range(r.i32()):
            for _i in range(r.i32()):
                r.raw(8)
        u['gold'] = r.i32()
        u['acquire'] = r.f32()
        u['hero_level'] = r.i32()
        if sub >= 11:
            r.raw(12)                   # str/agi/int
        for _ in range(r.i32()):
            r.raw(8)                    # inventory slot + item id
        for _ in range(r.i32()):
            r.raw(12)                   # modified abilities
        rf = r.i32()
        if rf == 0:
            r.raw(4)
        elif rf == 1:
            r.raw(8)
        elif rf == 2:
            for _ in range(r.i32()):
                r.raw(8)
        u['color'] = r.i32()
        u['waygate'] = r.i32()
        u['creation'] = r.i32()
        units.append(u)
    return units, r.p


def parse_units_doo(data):
    """war3mapUnits.doo. Version 8 / sub 11 files come with and without a skin id4 after the scale (x hero siege 3.53,
    saved by a 1.32+ editor: 12 records end exactly at the end of the file only with it; BUGS MAP-XHS) - the layout
    that consumes the file exactly wins, the old one (no skin) first."""
    r = Reader(data)
    if r.raw(4) != b'W3do':
        raise ValueError('not W3do')
    ver = r.i32()
    sub = r.i32()
    first = None
    err = None
    for skin in ((False, True) if ver >= 8 and sub >= 11 else (False,)):
        try:
            units, end = _units_doo(data, ver, sub, skin)
        except (struct.error, IndexError, ValueError) as e:
            err = err or e
            continue
        if end == len(data):
            return {'version': ver, 'subversion': sub, 'units': units}
        first = first or (units, end)
    if first is None:
        raise err
    return {'version': ver, 'subversion': sub, 'units': first[0]}


# ---------------------------------------------------------------- object data

OPT_INTS = ('.w3d', '.w3a', '.w3q')


def parse_objects(data, ext):
    r = Reader(data)
    ver = r.i32()
    opt = ext.lower() in OPT_INTS
    tables = []
    for _t in range(2):
        objs = []
        if r.left() < 4:
            tables.append(objs)
            continue
        for _ in range(r.i32()):
            o = {'orig': r.id4(), 'new': r.id4(), 'mods': [], 'ptrs': []}
            nm = r.i32()
            for _m in range(nm):
                mid = r.id4()
                vt = r.i32()
                lvl = None
                ptr = 0
                if opt:
                    lvl = r.i32()
                    ptr = r.i32()           # data pointer (1..9 = DataA..DataI column)
                if vt == 0:
                    val = r.i32()
                elif vt in (1, 2):
                    val = r.f32()
                elif vt == 3:
                    val = r.cstr()
                else:
                    raise ValueError('bad var type %d' % vt)
                r.raw(4)
                o['mods'].append((mid, lvl, val))
                o['ptrs'].append(ptr)
            objs.append(o)
        tables.append(objs)
    return {'version': ver, 'original_modified': tables[0], 'custom': tables[1]}


# ---------------------------------------------------------------- wpm (pathing map, 4x4 cells per tile)

def parse_wpm(data):
    r = Reader(data)
    if r.raw(4) != b'MP3W':
        raise ValueError('not MP3W')
    ver = r.i32()
    w = r.i32()
    h = r.i32()
    cells = data[r.p:r.p + w * h]
    # bits: 0x02 not walkable, 0x04 not flyable, 0x08 not buildable, 0x20 blight, 0x40 not water(=land), 0x80 ?
    return {'version': ver, 'w': w, 'h': h, 'cells': cells,
            'walkable': sum(1 for c in cells if not c & 0x02), 'buildable': sum(1 for c in cells if not c & 0x08)}
