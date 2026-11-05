#!/usr/bin/env python3
"""In-game HUD layout from the player's install (docs/ui_plan.md U3): FDF frames (tools/fdf.py) with INHERITS,
skin keys resolved per race (UI\\war3skins.txt), anchors resolved to absolute rectangles of the 0.8 x 0.6 screen
(origin bottom left). The positions of the top-level frames that the game sets in code come from NOTES_ui.md
(TOP_LEVEL below).

  python3 tools/w3ui.py [--race Human] [--install input/war3]   -> work/ui/hud_<race>.json + work/ui/preview_<race>.png

Nothing of the install goes into the repo (work/ is git-ignored).
"""
import argparse
import json
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..'))
sys.path.insert(0, HERE)
import w3fdf as fdf                     # noqa: E402

SCREEN_W, SCREEN_H = 0.8, 0.6
POINTS = ('TOPLEFT', 'TOP', 'TOPRIGHT', 'LEFT', 'CENTER', 'RIGHT', 'BOTTOMLEFT', 'BOTTOM', 'BOTTOMRIGHT')
# top-level frames placed by the game in code: name -> (point, relative point of the screen, x, y) [NOTES_ui.md]
TOP_LEVEL = {
    'ConsoleUI': None,                                   # SimpleConsole fills the screen (NOTES_ui root frames)
    'ResourceBarFrame': ('TOPRIGHT', 0.0, 0.0),          # NOTES_ui [obs 0x6f0eb510]
    'UpperButtonBarFrame': ('TOPLEFT', 0.0, 0.0),        # NOTES_ui [obs 0x6f0eb535]
}


def slots():
    """hard-coded HUD slots (NOTES_ui.md): kind, index, rect (x0, y0, x1, y1) in screen units"""
    out = [('minimap', 0, (0.009375, 0.006875, 0.14875, 0.145)),
           ('portrait', 0, (0.2075, 0.030625, 0.2975, 0.116225))]
    for row in range(3):                                 # command card: 3 x 4 buttons of 0.038, pitch 0.043625 x 0.043
        for col in range(4):
            x0 = 0.618125 + col * 0.043625
            y1 = 0.131 - row * 0.043
            out.append(('command', row * 4 + col, (x0, y1 - 0.038, x0 + 0.038, y1)))
    for row in range(3):                                 # inventory: 2 x 3 slots of 0.03125, pitch 0.04 x 0.038125
        for col in range(2):
            x0 = 0.51625 + col * 0.04
            y1 = 0.111875 - row * 0.038125
            out.append(('inventory', row * 2 + col, (x0, y1 - 0.03125, x0 + 0.03125, y1)))
    return out
HUD_FILES = ('ConsoleUI.fdf', 'ResourceBar.fdf', 'UpperButtonBar.fdf', 'SimpleInfoPanel.fdf')


def point_xy(rect, p):
    x0, y0, x1, y1 = rect
    px = x0 if 'LEFT' in p else (x1 if 'RIGHT' in p else (x0 + x1) / 2)
    py = y1 if 'TOP' in p else (y0 if 'BOTTOM' in p else (y0 + y1) / 2)
    return px, py


class Layout:
    def __init__(self, install, race):
        self.inst = install
        self.race = race
        self.skins = fdf.skins(install)
        self.templates = {}
        self.files = {}

    def load(self, path):
        if path in self.files:
            return self.files[path]
        raw = self.inst.read(path, missing_ok=True)
        tree = fdf.parse(raw.decode('latin-1')) if raw else {'includes': [], 'nodes': [], 'strings': {}}
        self.files[path] = tree
        for inc in tree['includes']:
            self.load(inc)
        for n in tree['nodes']:
            self._register(n)
        return tree

    def _register(self, n):
        if n.get('name'):
            self.templates.setdefault(n['name'], n)
        for c in n['children']:
            self._register(c)

    def skin(self, key):
        for sec in (self.race, 'Default', 'Main'):
            v = self.skins.get(sec, {}).get(key)
            if v:
                return v if os.path.splitext(v)[1] else v + '.blp'      # skins name the texture without extension
        return key if key.lower().endswith(('.blp', '.tga')) else None

    def resolved(self, n):
        """props / children with INHERITS applied (the template first, the node's own entries override)."""
        props = list(n['props'])
        children = list(n['children'])
        if n.get('inherits') and n['inherits'] in self.templates and self.templates[n['inherits']] is not n:
            tp, tc = self.resolved(self.templates[n['inherits']])
            own = set(k for k, _v in props)
            props = [kv for kv in tp if kv[0] not in own or kv[0] in ('Anchor', 'SetPoint')] + props
            if n.get('withchildren'):
                children = tc + children
        return props, children

    def rect(self, props, parent, named):
        w = h = None
        pts = []
        for k, v in props:
            if k == 'Width' and v:
                w = float(v[0])
            elif k == 'Height' and v:
                h = float(v[0])
            elif k == 'Anchor' and v:
                p = str(v[0])
                pts.append((p, parent, p, float(v[1]) if len(v) > 1 else 0.0, float(v[2]) if len(v) > 2 else 0.0))
            elif k == 'SetPoint' and len(v) >= 3:
                rel = named.get(str(v[1]), parent)
                pts.append((str(v[0]), rel, str(v[2]), float(v[3]) if len(v) > 3 else 0.0,
                            float(v[4]) if len(v) > 4 else 0.0))
            elif k == 'SetAllPoints':
                return parent
        if not pts:
            if w is None and h is None:
                return parent
            x, y = point_xy(parent, 'TOPLEFT')
            return (x, y - (h or 0), x + (w or 0), y)
        xs, ys = {}, {}
        for p, rel, rp, ox, oy in pts:
            ax, ay = point_xy(rel, rp)
            ax += ox
            ay += oy
            xs['L' if 'LEFT' in p else ('R' if 'RIGHT' in p else 'C')] = ax
            ys['T' if 'TOP' in p else ('B' if 'BOTTOM' in p else 'C')] = ay
        if 'L' in xs and 'R' in xs:
            x0, x1 = xs['L'], xs['R']
        elif 'L' in xs:
            x0 = xs['L']; x1 = x0 + (w or 0)
        elif 'R' in xs:
            x1 = xs['R']; x0 = x1 - (w or 0)
        else:
            c = xs.get('C', (parent[0] + parent[2]) / 2); x0 = c - (w or 0) / 2; x1 = c + (w or 0) / 2
        if 'T' in ys and 'B' in ys:
            y0, y1 = ys['B'], ys['T']
        elif 'T' in ys:
            y1 = ys['T']; y0 = y1 - (h or 0)
        elif 'B' in ys:
            y0 = ys['B']; y1 = y0 + (h or 0)
        else:
            c = ys.get('C', (parent[1] + parent[3]) / 2); y0 = c - (h or 0) / 2; y1 = c + (h or 0) / 2
        return (x0, y0, x1, y1)

    def walk(self, n, parent, out, named, frame_name):
        props, children = self.resolved(n)
        r = self.rect(props, parent, named)
        if n.get('name'):
            named[n['name']] = r
        pd = dict((k, v) for k, v in props)
        kind = n['kind']
        if kind == 'Texture':
            f = pd.get('File', [None])[0]
            tc = [float(x) for x in pd.get('TexCoord', [0, 1, 0, 1])][:4]
            out.append({'el': 'texture', 'frame': frame_name, 'name': n.get('name'), 'key': f,
                        'file': self.skin(f) if f else None, 'rect': r, 'uv': tc,
                        'alpha': pd.get('AlphaMode', ['BLEND'])[0]})
        elif kind == 'String':
            font = pd.get('Font', [None, 0.01])
            out.append({'el': 'string', 'frame': frame_name, 'name': n.get('name'), 'rect': r,
                        'font': self.skin(font[0]) if font[0] else None, 'size': float(font[1]) if len(font) > 1 else 0.01,
                        'justh': pd.get('FontJustificationH', ['JUSTIFYLEFT'])[0]})
        for c in children:
            self.walk(c, r, out, named, n.get('name') or frame_name)

    def frame(self, name, rect):
        out = []
        self.walk(self.templates[name], rect, out, {}, name)
        return out


PROFILE_FILES = ('Command', 'Human', 'Orc', 'Undead', 'NightElf', 'Neutral', 'Campaign', 'Common', 'Item')
# map overrides (object editor field ids): icon, button x, button y; abilities also the hero-learn ("research") art
OBJ_FIELDS = {'.w3u': ('uico', 'ubpx', 'ubpy'), '.w3a': ('aart', 'abpx', 'abpy'), '.w3q': ('gar1', 'gbpx', 'gbpy'),
              '.w3t': ('iico', None, None)}


# BUGS MELEE-SEEDS: a melee map's script only calls MeleeStartingUnits(); the town halls and workers it creates are named in
# Blizzard.j, not in war3map.j, so the rawcode scan never saw them: no model (invisible peasants), no command icons (empty
# Build sub-card), no voices. Their Builds / Trains / Upgrade lists are followed by the callers' closures.
MELEE_START = ('htow', 'hpea', 'ogre', 'opeo', 'unpl', 'uaco', 'etol', 'ewsp')


def melee_seeds(script):
    """the four races' start types when the map script runs the standard melee init (bytes or str), else ()."""
    if script is None:
        return ()
    text = script.decode('latin-1') if isinstance(script, (bytes, bytearray)) else script
    return MELEE_START if 'MeleeStartingUnits' in text else ()


def command_icons(install, read_map, objs):
    """the command-card icons a map can show (docs/ui_plan.md U5): ids = the map's unit types (objs['units'],
    from w3pack_jvm), their Builds / Trains / Researches / Upgrade lists, abilities (abilList, heroAbilList),
    and the generic commands of CommandFunc.txt. -> {id: (art path, bpx, bpy, research art, rbpx, rbpy)}"""
    sys.path.insert(0, os.path.join(HERE, 'jvm'))
    import unitdata as UD
    import w3map
    prof = {}
    for pre in PROFILE_FILES:
        for kind in ('UnitFunc', 'AbilityFunc', 'UpgradeFunc', 'Func'):
            name = 'Units\\%s%s.txt' % (pre, kind) if pre != 'Command' else 'Units\\CommandFunc.txt'
            if pre == 'Item' and kind != 'Func' and kind != 'AbilityFunc':
                continue
            raw = install.read(name, missing_ok=True)
            if raw:
                UD.parse_profile(raw.decode('latin-1'), prof)
    over = {}
    for ext, (fi, fx, fy) in OBJ_FIELDS.items():
        raw = read_map('war3map' + ext)
        if not raw:
            continue
        try:
            d = w3map.parse_objects(raw, ext)
        except Exception:                                # noqa: BLE001 — a broken object file: no overrides
            continue
        std = {k: dict(v) for k, v in over.items()}     # BUGS OD-1: custom objects start from the standard values
        for o in d['original_modified'] + d['custom']:
            oid = o.get('new') or o.get('orig')
            base = o.get('orig')
            cur = dict(std.get(base, {})) if o.get('new') else over.setdefault(oid, {})
            for mid, _lvl, val in o['mods']:
                if mid == fi:
                    cur['Art'] = val
                elif mid == fx:
                    cur['bx'] = val
                elif mid == fy:
                    cur['by'] = val
                elif mid == 'arar':
                    cur['Researchart'] = val
            over[oid] = cur
            if o.get('new'):
                over.setdefault('_base', {})[oid] = base
    want = set()
    allu = objs.get('units', {}) if objs else {}
    # the map's unit types: object data + rawcodes in the script, closed over Builds / Trains / Upgrade
    import re as _re
    seeds = set(k for k in over if k != '_base' and k in allu)
    js = read_map('war3map.j') or read_map('Scripts\\war3map.j') or b''
    seeds |= set(x for x in _re.findall(r"'([A-Za-z0-9]{4})'", js.decode('latin-1')) if x in allu)
    seeds |= set(x for x in melee_seeds(js) if x in allu)                  # BUGS MELEE-SEEDS
    used, todo = set(), list(seeds)
    while todo:
        u = todo.pop()
        if u in used:
            continue
        used.add(u)
        for k in ('Builds', 'Trains', 'Upgrade'):
            for x in str(allu[u].get(k) or '').split(','):
                x = x.strip().strip('"')
                if x in allu and x not in used:
                    todo.append(x)
    units = dict((u, allu[u]) for u in used)
    for uid, d in units.items():
        want.add(uid)
        for k in ('Builds', 'Trains', 'Researches', 'Upgrade', 'abilList', 'heroAbilList'):
            for x in str(d.get(k) or '').split(','):
                x = x.strip().strip('"')
                if len(x) == 4:
                    want.add(x)
    for k, v in prof.items():
        if k.startswith('Cmd'):
            want.add(k)
    # td_plan T6 inventory: the items a map can carry — its custom / modified ones (w3t), the item rawcodes its
    # script names and its shops' Sellitems / Makeitems (when the unit data has them)
    items_known = set(k for k in prof if len(k) == 4)
    for oid in over:
        if oid != '_base' and len(oid) == 4 and oid not in allu:
            want.add(oid)
    for x in _re.findall(r"'([A-Za-z0-9]{4})'", js.decode('latin-1')):
        if x in items_known and x not in allu:
            want.add(x)
    for uid, d in units.items():
        for k in ('Sellitems', 'Makeitems', 'Sellunits'):
            for x in str(d.get(k) or '').split(','):
                x = x.strip().strip('"')
                if len(x) == 4:
                    want.add(x)
    out = {}
    bases = over.get('_base', {})
    sk = fdf.skins(install)

    def skin_art(a):                                     # CommandFunc names skin keys (CommandMove …), not paths
        if not a or '\\' in a or '/' in a:
            return a
        for sec in ('Default', 'Human', 'Main'):
            v = sk.get(sec, {}).get(a)
            if v:
                return v
        return a
    for i in sorted(want):
        p = prof.get(i) or prof.get(bases.get(i, ''), {}) or {}
        p = dict((k.lower(), v) for k, v in p.items())           # profile keys are case-insensitive (ButtonPos)
        ov = over.get(i, {})
        art = str(ov.get('Art') or p.get('art') or '').split(',')[0].strip('"')
        if not art:
            continue
        bp = str(p.get('buttonpos') or '0,0').split(',')
        bx = int(ov['bx']) if 'bx' in ov else int(float(bp[0] or 0))
        by = int(ov['by']) if 'by' in ov else int(float(bp[1] if len(bp) > 1 else 0))
        rart = str(ov.get('Researchart') or p.get('researchart') or '').split(',')[0].strip('"')
        art, rart = skin_art(art), skin_art(rart)
        rbp = str(p.get('researchbuttonpos') or '0,0').split(',')
        out[i] = (art, bx, by, rart, int(float(rbp[0] or 0)), int(float(rbp[1] if len(rbp) > 1 else 0)))
    return out


TIP_FIELDS = {'.w3u': ('utip', 'utub', 'uhot'), '.w3a': ('atp1', 'aub1', 'ahky'), '.w3q': ('gtp1', 'gub1', 'ghk1'),
              '.w3t': ('utip', 'utub', 'uhot')}     # map overrides: tip, ubertip, hotkey (level 1 for abilities)


def tip_overrides(read_map):
    """the map's object-editor tooltip overrides -> {id: {'tip', 'ubertip', '_base'}} (TRIGSTR resolved). Unit / item
    modifications carry no level (None); abilities / upgrades level 1 (or 0) — BUGS UI-TIP: units were skipped."""
    sys.path.insert(0, os.path.join(HERE, 'jvm'))
    import w3map
    wraw = read_map('war3map.wts') if read_map else None
    wts = w3map.parse_wts(wraw) if wraw else None
    over = {}
    for ext, (ft, fu, _fh) in TIP_FIELDS.items():
        raw = read_map('war3map' + ext) if read_map else None
        if not raw:
            continue
        try:
            d = w3map.parse_objects(raw, ext)
        except Exception:                                # noqa: BLE001
            continue
        for o in d['original_modified'] + d['custom']:
            oid = o.get('new') or o.get('orig')
            cur = over.setdefault(oid, {})
            if o.get('new'):
                cur.setdefault('_base', o.get('orig'))
            for mid, lvl, val in o['mods']:
                if ext == '.w3q' and lvl and mid == 'gnam':
                    cur.setdefault('levels', {}).setdefault(lvl, {})['name'] = w3map.resolve(str(val), wts)
                    continue
                if ext == '.w3q' and lvl and lvl > 1 and mid in (ft, fu):
                    # BUGS ETD-TIP: an upgrade has a tooltip per level (Element TD: "Upgrade to Water Level 2",
                    # "Interest Rate +1% (New Interest Rate 4%)"); level 1 stays in tip / ubertip
                    cur.setdefault('levels', {}).setdefault(lvl, {})['tip' if mid == ft else 'ubertip'] = \
                        w3map.resolve(str(val), wts)
                    continue
                if lvl not in (None, 0, 1):
                    continue
                if mid == ft:
                    cur['tip'] = w3map.resolve(str(val), wts)
                elif mid == fu:
                    cur['ubertip'] = w3map.resolve(str(val), wts)
    return over


def _split_levels(val, n):
    """a profile value with one entry per level ("a,b,c" or '"a","b","c"') -> n strings (the last entry repeats; a
    value that does not split into n parts stays whole for every level)."""
    v = str(val or '').strip()
    if not v:
        return [''] * n
    if v.startswith('"'):
        parts = [x for x in v.strip('"').split('","')]
    else:
        parts = [x.strip() for x in v.split(',')]
    if len(parts) != n:
        parts = [v.strip('"')] * n
    return parts


def research_tips(install, read_map, objs):
    """BUGS ETD-TIP: the tooltip of every research LEVEL (the card button shows the NEXT level's text and cost): ->
    {id: [(level, tip, ubertip, gold, lumber, name), ...]} for the upgrades the map's unit types research. Text: the map's
    per-level object-editor strings (gtp1 / gub1, TRIGSTR resolved), else the install's per-level profile lists;
    cost = goldbase + goldmod * (level - 1) (NOTES_production §3)."""
    upg = (objs or {}).get('upgrades') or {}
    units = (objs or {}).get('units') or {}
    ids = set()
    for d in units.values():
        for x in str(d.get('Researches') or '').split(','):
            x = x.strip()
            if x in upg:
                ids.add(x)
    if not ids:
        return {}
    sys.path.insert(0, os.path.join(HERE, 'jvm'))
    import unitdata as UD
    prof = {}
    for pre in PROFILE_FILES:
        raw = install.read('Units\\%sUpgradeStrings.txt' % pre, missing_ok=True)
        if raw:
            UD.parse_profile(raw.decode('latin-1'), prof)
    over = tip_overrides(read_map)
    out = {}
    for i in sorted(ids):
        r = upg[i]
        n = max(1, int(r.get('maxlevel', 1) or 1))
        ov = over.get(i, {})
        p = dict((k.lower(), v) for k, v in (prof.get(i) or prof.get(ov.get('_base', ''), {}) or {}).items())
        tips = _split_levels(p.get('tip'), n)
        ubs = _split_levels(p.get('ubertip'), n)
        names = _split_levels(p.get('name'), n)
        rows = []
        for lv in range(1, n + 1):
            lo = ov.get('levels', {}).get(lv, {}) if lv > 1 else ov      # level 1 sits in tip / ubertip
            tip = lo['tip'] if 'tip' in lo else tips[lv - 1]                  # an unset level keeps the base text
            ub = lo['ubertip'] if 'ubertip' in lo else ubs[lv - 1]
            nm = ov.get('levels', {}).get(lv, {}).get('name', names[lv - 1])
            g = int(r.get('goldbase', 0) + r.get('goldmod', 0) * (lv - 1))
            l = int(r.get('lumberbase', 0) + r.get('lumbermod', 0) * (lv - 1))
            rows.append((lv, tip, ub, g, l, nm))
        out[i] = rows
    return out


def command_tips(install, read_map, objs, ids):
    """the command-card tooltip data (docs/ui_plan.md U5 "tooltips"): per id the profile Tip / Ubertip (the
    install's *Strings.txt: the hotkey letter is already coloured there with |c markup), the hero-learn
    Researchtip / Researchubertip, the map's object-editor overrides (TRIGSTR resolved through war3map.wts) and the
    cost of units / buildings (gold, lumber, food). -> {id: (tip, ubertip, gold, lumber, food, rtip, rubertip)}"""
    sys.path.insert(0, os.path.join(HERE, 'jvm'))
    import unitdata as UD
    import w3map
    prof = {}
    for pre in PROFILE_FILES:
        for kind in ('UnitStrings', 'AbilityStrings', 'UpgradeStrings', 'Strings'):
            name = 'Units\\%s%s.txt' % (pre, kind) if pre != 'Command' else 'Units\\CommandStrings.txt'
            if pre == 'Item' and kind not in ('Strings', 'AbilityStrings'):
                continue
            raw = install.read(name, missing_ok=True)
            if raw:
                UD.parse_profile(raw.decode('latin-1'), prof)
    over = tip_overrides(read_map)
    allu = objs.get('units', {}) if objs else {}
    try:                                                 # <id,DataA1[,%]> placeholders: the ability's level values
        from jvm import abildata as AD
        abil = AD.merge(AD.base_abilities(lambda n: install.read(n)), read_map('war3map.w3a') if read_map else None)
    except Exception:                                    # noqa: BLE001
        abil = {}
    import re as _re

    def fill(t):
        def one(m):
            a = abil.get(m.group(1))
            f, lvl, pct = m.group(2), int(m.group(3) or 1), bool(m.group(4))
            if not a or not a.get('lv'):
                return m.group(0)
            lv = a['lv'][min(max(lvl, 1), len(a['lv'])) - 1]
            key = f[4:] if f.startswith('Data') and len(f) == 5 else f
            v = lv.get(key)
            if not isinstance(v, (int, float)):
                return m.group(0)
            v = v * 100.0 if pct else v
            return ('%d' % round(v)) if abs(v - round(v)) < 1e-6 else ('%.2f' % v).rstrip('0').rstrip('.')
        # [inf] the field grammar from the install's strings: <id,Field<level>> with an optional ",%" (x 100)
        return _re.sub(r'<([A-Za-z0-9]{4}),([A-Za-z]+?)(\d+)?(,%)?>', one, t or '')
    out = {}
    for i in ids:
        ov = over.get(i, {})
        p = prof.get(i) or prof.get(ov.get('_base', ''), {}) or {}
        p = dict((k.lower(), v) for k, v in p.items())
        strip = lambda v: str(v or '').split(',')[0].strip('"') if v is not None else ''      # noqa: E731
        tip = ov.get('tip') or strip(p.get('tip'))
        ub = ov.get('ubertip') or str(p.get('ubertip') or '').strip('"')
        rt, rub = strip(p.get('researchtip')), str(p.get('researchubertip') or '').strip('"')
        u = allu.get(i) or {}
        out[i] = (fill(tip), fill(ub), int(u.get('gold') or 0), int(u.get('lumber') or 0), int(u.get('fused') or 0),
                  fill(rt), fill(rub))
    return out


RACES = ('Human', 'Orc', 'NightElf', 'Undead')
ATLAS_W = 2048
FONT_PX = 32
FONT_CHARS = [c for c in range(32, 256) if c != 127 and not (128 <= c < 160)]


UI_DXT5 = True                  # the HUD atlas UIAT as DXT5 (mips field + 256), see ui_sections


def _shelf(sizes, width):
    x = y = rowh = 0
    pos = []
    for w, h in sizes:
        if x + w > width:
            x, y, rowh = 0, y + rowh, 0
        pos.append((x, y))
        x += w
        rowh = max(rowh, h)
    return pos, y + rowh


def ui_sections(install, log=print, read_map=None, objs=None):
    """-> [(b'JUI1', text bytes, n lines), (b'UIAT', atlas bytes, 1)] for the four races' HUD: every texture element
    cropped to its TexCoord rectangle and packed into one RGBA atlas (2 px gutter), plus the install's font rendered
    as a glyph atlas (FONT_PX). Text lines (all coordinates in screen units, 0.8 x 0.6, origin bottom left):
      T <race> <frame> <x0> <y0> <x1> <y1> <ax0> <ay0> <ax1> <ay1> <alpha>   (atlas rect normalised, v up)
      S <race> <frame> <name> <x0> <y0> <x1> <y1> <size> <justify>
      F <px> <ascent> <descent>  /  G <code> <ax0> <ay0> <ax1> <ay1> <w> <h> <xoff> <yoff> <advance>  (glyph px)"""
    from PIL import Image, ImageFont, ImageDraw
    import io
    import w3pack
    entries, lines, crops = {}, [], []
    for race in RACES:
        lay = Layout(install, race)
        for f in HUD_FILES:
            lay.load('UI\\FrameDef\\UI\\' + f)
        els = lay.frame('ConsoleUI', (0.0, 0.0, SCREEN_W, SCREEN_H))
        for name in ('ResourceBarFrame', 'UpperButtonBarFrame'):
            if name not in lay.templates:
                continue
            pr, _c = lay.resolved(lay.templates[name])
            pd = dict(pr)
            w, h = float(pd.get('Width', [0])[0]), float(pd.get('Height', [0])[0])
            pt, ox, oy = TOP_LEVEL[name]
            x1 = SCREEN_W + ox if 'RIGHT' in pt else ox + w
            y1 = SCREEN_H + oy
            sub = lay.frame(name, (x1 - w, y1 - h, x1, y1))
            for e in sub:
                if e['el'] == 'string':
                    lines.append(('S', race, e))
            els += sub
        for e in els:
            if e['el'] != 'texture' or not e['file']:
                continue
            key = (e['file'], tuple(round(x, 6) for x in e['uv']))
            if key not in entries:
                raw = install.read(e['file'].replace('/', '\\'), missing_ok=True)
                if not raw:
                    log('  ui: missing %s' % e['file'])
                    continue
                tex = w3pack.blp_rgba(raw)
                tw, th = tex.size
                u0, u1, v0, v1 = e['uv']
                box = (int(round(u0 * tw)), int(round(v0 * th)), max(int(round(u0 * tw)) + 1, int(round(u1 * tw))),
                       max(int(round(v0 * th)) + 1, int(round(v1 * th))))
                entries[key] = len(crops)
                crops.append(tex.crop(box))
            e['atlas'] = entries[key]
            lines.append(('T', race, e))
    icons = command_icons(install, read_map, objs) if read_map else {}
    icon_at = {}
    for iid, (art, bx, by, rart, rbx, rby) in sorted(icons.items()):
        for path in (art, rart):
            if not path or path in icon_at:
                continue
            stem = os.path.splitext(path)[0] if path.lower().endswith(('.blp', '.tga')) else path
            im = None
            for fn in (stem + '.blp', path if path.lower().endswith('.tga') else None):   # .tga names load the .blp
                if not fn:
                    continue
                fn = fn.replace('/', '\\')
                raw = (read_map(fn) if read_map else None) or install.read(fn, missing_ok=True)
                if not raw:                              # map imports first (custom icons), then the install
                    continue
                try:
                    if fn.lower().endswith('.blp'):
                        im = w3pack.blp_rgba(raw)
                    else:
                        from PIL import Image
                        import io
                        im = Image.open(io.BytesIO(raw)).convert('RGBA')
                    break
                except Exception:                        # noqa: BLE001
                    im = None
            if im is None:
                continue
            if im.size != (64, 64):
                im = im.resize((64, 64))
            icon_at[path] = len(crops)
            crops.append(im)
    # tooltip cost icons (NOTES_ui.md "Command-card tooltip": skin keys ToolTipGold/Lumber/Supply/ManaIcon)
    tip_icons = {}
    sk = fdf.skins(install)
    for key in ('ToolTipGoldIcon', 'ToolTipLumberIcon', 'ToolTipSupplyIcon', 'ToolTipManaIcon'):
        v = None
        for sec in ('Human', 'Default', 'Main'):
            v = v or sk.get(sec, {}).get(key)
        if not v:
            continue
        fn = (v if os.path.splitext(v)[1] else v + '.blp').replace('/', '\\')
        raw = install.read(fn, missing_ok=True)
        if not raw:
            continue
        try:
            im = w3pack.blp_rgba(raw)
        except Exception:                                # noqa: BLE001
            continue
        tip_icons[key] = len(crops)
        crops.append(im.resize((32, 32)))
    white = len(crops)                                   # solid fills (empty command slots, portrait background)
    crops.append(Image.new('RGBA', (4, 4), (255, 255, 255, 255)))
    ttf = install.read('Fonts\\FRIZQT__.TTF', missing_ok=True)
    glyphs = []
    if ttf:
        font = ImageFont.truetype(io.BytesIO(ttf), FONT_PX)
        asc, desc = font.getmetrics()
        for c in FONT_CHARS:
            ch = chr(c)
            l, t, r, b = font.getbbox(ch)
            w, h = max(1, r - l), max(1, b - t)
            im = Image.new('RGBA', (w, h), (255, 255, 255, 0))
            ImageDraw.Draw(im).text((-l, -t), ch, font=font, fill=(255, 255, 255, 255))
            glyphs.append((c, len(crops), w, h, l, t, font.getlength(ch)))
            crops.append(im)
    order = sorted(range(len(crops)), key=lambda k: (-crops[k].size[1], -crops[k].size[0]))   # tallest first
    # DXT5 (UI_DXT5): 4-aligned cells, the crop 4 px in, so no 4 x 4 block mixes two crops; RGBA: 2 px gutter
    g = 4 if UI_DXT5 else 2
    cell = lambda n: (-(-(n + 2 * g) // 4) * 4) if UI_DXT5 else n + 4                       # noqa: E731
    spos, height = _shelf([(cell(crops[k].size[0]), cell(crops[k].size[1])) for k in order], ATLAS_W)
    pos = [None] * len(crops)
    for k, p in zip(order, spos):
        pos[k] = p
    H = -(-height // 256) * 256
    page = Image.new('RGBA', (ATLAS_W, H), (0, 0, 0, 0))
    rects = []
    for im, (x, y) in zip(crops, pos):
        page.paste(im, (x + g, y + g))
        w, h = im.size
        rects.append(((x + g) / ATLAS_W, (H - (y + g + h)) / H, (x + g + w) / ATLAS_W, (H - (y + g)) / H))
    out = []
    for t, race, e in lines:
        if t == 'S':
            out.append('S %s %s %s %.6f %.6f %.6f %.6f %.6f %s' % ((race, e['frame'], e['name'] or '-') + tuple(e['rect'])
                                                                  + (e['size'], e['justh'])))
            continue
        a = rects[e['atlas']]
        out.append('T %s %s %.6f %.6f %.6f %.6f %.6f %.6f %.6f %.6f %s' % ((race, e['frame']) + tuple(e['rect']) + a + (e['alpha'],)))
    for iid, (art, bx, by, rart, rbx, rby) in sorted(icons.items()):
        if art in icon_at:
            out.append('I %s %d %d %.6f %.6f %.6f %.6f' % ((iid, bx, by) + rects[icon_at[art]]))
        if rart in icon_at:
            out.append('R %s %d %d %.6f %.6f %.6f %.6f' % ((iid, rbx, rby) + rects[icon_at[rart]]))
    # tooltips (P lines): id gold lumber food, then tip / ubertip / research tip / research ubertip, tab-separated
    # (texts keep the |c colour markup; newlines escaped as \n like the X lines)
    try:
        tips = command_tips(install, read_map, objs, list(icons)) if read_map else {}
    except Exception as e:                               # noqa: BLE001 — no tooltips is not fatal
        log('  ui: tooltips skipped: %s: %s' % (type(e).__name__, e))
        tips = {}
    esc = lambda t: str(t or '').replace('\t', ' ').replace('\r', '').replace('\n', '\\n')   # noqa: E731
    for iid, (tip, ub, g, l, f, rt, rub) in sorted(tips.items()):
        if tip or ub or rt:
            out.append('P %s %d %d %d %s' % (iid, g, l, f, '\t'.join(esc(x) for x in (tip, ub, rt, rub))))
    try:                                                 # BUGS ETD-TIP: Q lines = id level gold lumber, tip \t ubertip
        qt = research_tips(install, read_map, objs) if read_map else {}
    except Exception as e:                               # noqa: BLE001
        log('  ui: research tooltips skipped: %s: %s' % (type(e).__name__, e))
        qt = {}
    for iid, rows in sorted(qt.items()):
        for lv, tip, ub, g, l, nm in rows:
            if tip or ub or nm:
                out.append('Q %s %d %d %d %s' % (iid, lv, g, l, '\t'.join(esc(x) for x in (tip, ub, nm))))
    for key, k in sorted(tip_icons.items()):
        out.append('K %s %.6f %.6f %.6f %.6f' % ((key,) + rects[k]))
    wr = rects[white]
    out.append('W %.6f %.6f' % ((wr[0] + wr[2]) / 2, (wr[1] + wr[3]) / 2))
    for kind, idx, r in slots():
        out.append('B %s %d %.6f %.6f %.6f %.6f' % ((kind, idx) + r))
    if ttf:
        out.append('F %d %d %d' % (FONT_PX, asc, desc))
        for c, k, w, h, l, t, adv in glyphs:
            a = rects[k]
            out.append('G %d %.6f %.6f %.6f %.6f %d %d %d %d %.2f' % ((c,) + a + (w, h, l, t, adv)))
    text = ('\n'.join(out) + '\n').encode('utf-8')
    flipped = page.transpose(Image.FLIP_TOP_BOTTOM)
    import w3pack_models
    if UI_DXT5 and w3pack_models.dxt5_supported():   # 26 MB RGBA -> 6.5 MB (1.31 packs were 92 MB of VRChat's 100, BUGS L-9)
        atlas = struct.pack('<3i', ATLAS_W, H, 1 + 256) + w3pack_models.dxt5(flipped)
    else:
        atlas = struct.pack('<3i', ATLAS_W, H, 1) + flipped.tobytes()
    log('  ui: %d command icons (%d ids)' % (len(icon_at), len(icons)))
    log('  ui: %d texture crops + %d glyphs, atlas %dx%d (%.1f MB), %d lines' % (
        len(entries), len(glyphs), ATLAS_W, H, len(atlas) / 1048576.0, len(out)))
    return [(b'JUI1', text, len(out)), (b'UIAT', atlas, 1)]


def preview(layout, els, path, scale=2000):
    from PIL import Image
    import w3pack
    W, H = int(SCREEN_W * scale), int(SCREEN_H * scale)
    img = Image.new('RGBA', (W, H), (40, 60, 40, 255))
    cache = {}
    for e in els:
        if e['el'] != 'texture' or not e['file']:
            continue
        f = e['file']
        if f not in cache:
            raw = layout.inst.read(f.replace('/', '\\'), missing_ok=True)
            cache[f] = w3pack.blp_rgba(raw) if raw and f.lower().endswith('.blp') else None
        tex = cache[f]
        if tex is None:
            continue
        x0, y0, x1, y1 = e['rect']
        u0, u1, v0, v1 = e['uv']
        tw, th = tex.size
        crop = tex.crop((int(u0 * tw), int(v0 * th), max(int(u0 * tw) + 1, int(u1 * tw)), max(int(v0 * th) + 1, int(v1 * th))))
        pw, ph = max(1, int((x1 - x0) * scale)), max(1, int((y1 - y0) * scale))
        crop = crop.resize((pw, ph))
        img.alpha_composite(crop, (int(x0 * scale), int(H - y1 * scale)))
    img.save(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--race', default='Human')
    ap.add_argument('--install', default=os.path.join(ROOT, 'input', 'war3'))
    a = ap.parse_args()
    import war3fs
    lay = Layout(war3fs.open_install(a.install), a.race)
    for f in HUD_FILES:
        lay.load('UI\\FrameDef\\UI\\' + f)
    screen = (0.0, 0.0, SCREEN_W, SCREEN_H)
    els = lay.frame('ConsoleUI', screen)
    out = os.path.join(ROOT, 'work', 'ui')
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, 'hud_%s.json' % a.race), 'w') as fh:
        json.dump(els, fh, indent=1)
    preview(lay, els, os.path.join(out, 'preview_%s.png' % a.race))
    print('%d elements; textures: %s' % (len(els), sorted(set(e['file'] for e in els if e['el'] == 'texture'))))


if __name__ == '__main__':
    main()
