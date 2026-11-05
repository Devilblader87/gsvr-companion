#!/usr/bin/env python3
"""Merged unit type data for the W5 engine (docs/engine_w5.md §1).

Sources (bring your own game: read from the player's install / the map at run time, never stored in the repo):
  Units\\UnitBalance.slk, UnitData.slk, UnitWeapons.slk, UnitAbilities.slk   (base values per type)
  Units\\*UnitFunc.txt profile sections                                     (Builds, Trains, Missilespeed/arc/homing)
  Units\\UnitMetaData.slk                                                    (w3u field id -> column, for the map diffs)
  Units\\MiscData.txt + MiscGame.txt [Misc], the map's war3mapMisc.txt       (game constants, NOTES_combat §0)
  PathTextures\\*.tga                                                         (building footprints, NOTES_units §4)
  war3map.w3u                                                                (original-modified + custom types)

A record is a flat dict: the 12 W3/W4 keys (spd hp mana mana0 bldg fused fmade level gold lumber points collision,
unchanged meaning, JUTY) + engine keys named after their SLK column (`def`, `defType`, `cool1` ...; profile fields
with weapon index as `Missilespeed1` ...). Enumerations are converted to ints here (NOTES_units §3 / NOTES_combat §5
parsers), target lists to bit masks (TARG below), lists (abilList, Builds, Trains) stay comma strings.
"""
import re

# ---------------------------------------------------------------- enumerations (NOTES_units §3, NOTES_combat §5)

ATK_TYPES = {'normal': 1, 'pierce': 2, 'siege': 3, 'magic': 4, 'chaos': 5, 'hero': 6}           # else 0 (spells)
DEF_TYPES = {'small': 0, 'medium': 1, 'large': 2, 'fort': 3, 'normal': 4, 'hero': 5, 'divine': 6, 'none': 7}  # unknown 4
WEAP_TYPES = {'normal': 1, 'missile': 2, 'artillery': 3, 'instant': 4, 'msplash': 5, 'mbounce': 6, 'mline': 7,
              'aline': 8}                                                                          # else 0 (no attack)
MOVE_TYPES = {'foot': 0x1, 'fly': 0x2, 'horse': 0x4, 'hover': 0x8, 'float': 0x10, 'amph': 0x20, 'unbuild': 0x40}
REGEN_TYPES = {'always': 1, 'blight': 2, 'day': 3, 'night': 4}                                     # else 0

# target list flags (our own bit layout; the game keeps an internal mask, NOTES_units §3 "targType keeps 0x1e")
T_GROUND, T_AIR, T_STRUCTURE, T_WARD, T_ITEM, T_TREE, T_DEBRIS, T_DECOR, T_BRIDGE, T_WALL = (1 << i for i in range(10))
T_SELF, T_PLAYER, T_ALLY, T_NEUTRAL, T_ENEMY = (1 << i for i in range(10, 15))
T_VULN, T_INVULN, T_HERO, T_NONHERO, T_ALIVE, T_DEAD, T_ORGANIC, T_MECH = (1 << i for i in range(15, 23))
T_NONSAPPER, T_SAPPER, T_NONANCIENT, T_ANCIENT, T_NOTSELF = (1 << i for i in range(23, 28))
T_CLASS = T_GROUND | T_AIR | T_STRUCTURE | T_WARD
T_ALLIANCE = T_SELF | T_PLAYER | T_ALLY | T_NEUTRAL | T_ENEMY
TARG = {'ground': T_GROUND, 'air': T_AIR, 'structure': T_STRUCTURE, 'ward': T_WARD, 'item': T_ITEM,
        'tree': T_TREE, 'debris': T_DEBRIS, 'decoration': T_DECOR, 'bridge': T_BRIDGE, 'wall': T_WALL,
        'self': T_SELF, 'player': T_PLAYER, 'allies': T_ALLY, 'friend': T_ALLY, 'neutral': T_NEUTRAL,
        'enemies': T_ENEMY, 'enemy': T_ENEMY, 'vulnerable': T_VULN, 'invulnerable': T_INVULN, 'hero': T_HERO,
        'nonhero': T_NONHERO, 'alive': T_ALIVE, 'dead': T_DEAD, 'organic': T_ORGANIC, 'mechanical': T_MECH,
        'nonsapper': T_NONSAPPER, 'sapper': T_SAPPER, 'nonancient': T_NONANCIENT, 'ancient': T_ANCIENT,
        'notself': T_NOTSELF}


def _s(v):
    return v.strip().strip('"').lower() if isinstance(v, str) else ''


def p_atk(v):
    return ATK_TYPES.get(_s(v), 0)


def p_def(v):
    return DEF_TYPES.get(_s(v), 4)


def p_weap(v):
    return WEAP_TYPES.get(_s(v), 0)


def p_flags(table):
    def f(v):
        if isinstance(v, (int, float)):
            return int(v)
        m = 0
        for part in _s(v).split(','):
            m |= table.get(part.strip(), 0)
        return m
    return f


p_move = p_flags(MOVE_TYPES)
# NOTES_unittype: UnitBalance `type` classification -> the unit's class bits (+0x244)
UNIT_CLASS_BITS = {'giant': 0x1, 'undead': 0x2, 'summoned': 0x4, 'mechanical': 0x8, 'peon': 0x10, 'sapper': 0x20,
                   'townhall': 0x40, 'ancient': 0x80, 'tauren': 0x800}
p_ucls = p_flags(UNIT_CLASS_BITS)
p_targ = p_flags(TARG)
# UnitData `race` -> the game's race value [obs name / value table 0x6f807720]
RACES = {'human': 1, 'orc': 2, 'undead': 3, 'nightelf': 4, 'demon': 5, 'other': 7, 'creeps': 8, 'commoner': 9,
         'critters': 10, 'naga': 11, 'unknown': 0}


def p_race(v):
    if isinstance(v, (int, float)):
        return int(v)
    return RACES.get(_s(v), 0)


def p_regen(v):
    return REGEN_TYPES.get(_s(v), 0)


def p_num(v, default=0):
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return v
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def p_str(v):
    if v is None:
        return ''
    if isinstance(v, (int, float)):
        return ''
    s = v.strip().strip('"')
    return '' if s in ('_', '-') else s


# ---------------------------------------------------------------- columns

# record key -> (source, column, parser). sources: bal data weap abil prof
_W = ('targs', 'atkType', 'weapTp', 'rangeN', 'RngBuff', 'cool', 'dice', 'sides', 'dmgplus', 'dmgpt', 'backSw',
      'Farea', 'Harea', 'Qarea', 'Hfact', 'Qfact', 'splashTargs', 'targCount', 'damageLoss')


def p_hfact(v):
    """BUGS SPL-DEF [obs runtime oracle dmgt1]: "-" (no value in UnitWeapons) is x0.5 for the medium ring."""
    return p_num(v, 0.5)


def p_qfact(v):
    """BUGS SPL-DEF [obs runtime oracle dmgt1]: "-" is x0.25 for the outer ring."""
    return p_num(v, 0.25)


_WP = {'targs': p_targ, 'splashTargs': p_targ, 'atkType': p_atk, 'weapTp': p_weap, 'Hfact': p_hfact, 'Qfact': p_qfact}

COLUMNS = {
    # the 12 keys of W3/W4 (tools/jvm/world.FIELDS, JUTY)
    'spd': ('bal', 'spd', p_num), 'hp': ('bal', 'HP', p_num), 'mana': ('bal', 'manaN', p_num),
    'mana0': ('bal', 'mana0', p_num), 'bldg': ('bal', 'isbldg', p_num), 'fused': ('bal', 'fused', p_num),
    'fmade': ('bal', 'fmade', p_num), 'level': ('bal', 'level', p_num), 'gold': ('bal', 'goldcost', p_num),
    'lumber': ('bal', 'lumbercost', p_num), 'points': ('data', 'points', p_num),
    'collision': ('bal', 'collision', p_num),
    # W5 engine
    'regenHP': ('bal', 'regenHP', p_num), 'regenMana': ('bal', 'regenMana', p_num),
    'regenType': ('bal', 'regenType', p_regen), 'def': ('bal', 'def', p_num), 'defType': ('bal', 'defType', p_def),
    'minSpd': ('bal', 'minSpd', p_num), 'maxSpd': ('bal', 'maxSpd', p_num), 'bldtm': ('bal', 'bldtm', p_num),
    'bountydice': ('bal', 'bountydice', p_num), 'bountysides': ('bal', 'bountysides', p_num),
    'bountyplus': ('bal', 'bountyplus', p_num), 'lumberbountydice': ('bal', 'lumberbountydice', p_num),
    'lumberbountysides': ('bal', 'lumberbountysides', p_num),
    'lumberbountyplus': ('bal', 'lumberbountyplus', p_num),
    'preventPlace': ('bal', 'preventPlace', p_str), 'requirePlace': ('bal', 'requirePlace', p_str),
    'turnRate': ('data', 'turnRate', p_num), 'movetp': ('data', 'movetp', p_move),
    'targType': ('data', 'targType', p_targ), 'pathTex': ('data', 'pathTex', p_str),
    'weapsOn': ('weap', 'weapsOn', p_num), 'acquire': ('weap', 'acquire', p_num),
    'minRange': ('weap', 'minRange', p_num),
    'abilList': ('abil', 'abilList', p_str),
    'Builds': ('prof', 'Builds', p_str), 'Trains': ('prof', 'Trains', p_str),
    'Researches': ('prof', 'Researches', p_str), 'Upgrade': ('prof', 'Upgrade', p_str),
}
for _i in (1, 2):
    for _c in _W:
        COLUMNS['%s%d' % (_c, _i)] = ('weap', '%s%d' % (_c, _i), _WP.get(_c, p_num))
    COLUMNS['Missilespeed%d' % _i] = ('prof', 'Missilespeed%d' % _i, p_num)
    COLUMNS['Missilearc%d' % _i] = ('prof', 'Missilearc%d' % _i, p_num)
    COLUMNS['MissileHoming%d' % _i] = ('prof', 'MissileHoming%d' % _i, p_num)

# W8 heroes + spell timing (appended after the weapon columns: JUT5 index 64 on, NOTES_heroes / NOTES_production §5)
PRIMARY = {'STR': 1, 'AGI': 2, 'INT': 3}


def p_primary(v):
    return PRIMARY.get(_s(v).upper(), 0)


COLUMNS.update({
    'castpt': ('weap', 'castpt', p_num), 'castbsw': ('weap', 'castbsw', p_num),
    'STR': ('bal', 'STR', p_num), 'AGI': ('bal', 'AGI', p_num), 'INT': ('bal', 'INT', p_num),
    'STRplus': ('bal', 'STRplus', p_num), 'AGIplus': ('bal', 'AGIplus', p_num), 'INTplus': ('bal', 'INTplus', p_num),
    'Primary': ('bal', 'Primary', p_primary),
    'heroAbilList': ('abil', 'heroAbilList', p_str), 'auto': ('abil', 'auto', p_str),
    # Skibi TD tags unit types by it (GetUnitDefaultPropWindow)
    'propWin': ('data', 'propWin', p_num),
    # W8 requirements (NOTES_skibi §3.5): type / research ids and their levels
    'Requires': ('prof', 'Requires', p_str), 'Requiresamount': ('prof', 'Requiresamount', p_str),
    # W8 IsUnitType (NOTES_unittype): classification bits
    'ucls': ('bal', 'type', p_ucls),
    # Wintermaul polymorph level cap (NOTES_abilities §5.28: race 8 = creeps only)
    'race': ('data', 'race', p_race),
    # W8 shops (NOTES_items §7.6): the type's sale lists + a sold unit's stock (UnitBalance) — pack section JSHP, not JUT5
    'Sellitems': ('prof', 'Sellitems', p_str), 'Sellunits': ('prof', 'Sellunits', p_str),
    'Makeitems': ('prof', 'Makeitems', p_str),
    'stockMax': ('bal', 'stockMax', p_num), 'stockRegen': ('bal', 'stockRegen', p_num),
    'stockStart': ('bal', 'stockStart', p_num),
    # BUGS FF-RALLY: the profile's Revive flag (Footman Frenzy's barracks revive heroes: the third production list
    # gives the rally ability); appended last: JUT5 index 76
    'Revive': ('prof', 'Revive', p_num),
})
# FOG: sight radius by day / night (UnitBalance sight, nsight; w3u usi1 / usi2 via the meta table); appended last: JUT5 index 77, 78
COLUMNS.update({'sight': ('bal', 'sight', p_num), 'nsight': ('bal', 'nsight', p_num)})
# BUGS FF-UPG: the type's "Upgrades Used" list (UnitBalance upgrades, w3u upgr: the researches whose effects reach the type's
# units) and the armor gained per level of an armor upgrade (UnitBalance defUp, w3u udu1): kept out of JUT5 (w3pack_jvm), the
# pack carries them in JUUP
COLUMNS.update({'upgrades': ('bal', 'upgrades', p_str), 'defUp': ('bal', 'defUp', p_num)})
UPG_KEYS = ('upgrades', 'defUp')
SHOP_LISTS = ('Sellitems', 'Sellunits', 'Makeitems')
SHOP_NUM = ('stockMax', 'stockRegen', 'stockStart')     # kept out of JUT5 (w3pack_jvm.UNIT_FIELDS_W5)

KEYS = tuple(COLUMNS)
_BY_COL = {(src, col): k for k, (src, col, _p) in COLUMNS.items()}
_META_SLK = {'UnitBalance': 'bal', 'UnitData': 'data', 'UnitWeapons': 'weap', 'UnitAbilities': 'abil',
             'Profile': 'prof'}
PROFILE_FILES = ('Human', 'Orc', 'Undead', 'NightElf', 'Neutral', 'Campaign')


def parse_profile(text, out):
    """[id] sections of a *UnitFunc.txt; first definition wins. -> {id: {key: raw value string}}."""
    cur = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('//'):
            continue
        if line.startswith('['):
            k = line[1:line.find(']')] if ']' in line else line[1:]
            cur = out.setdefault(k, {}) if k not in out else None
            continue
        if cur is None or '=' not in line:
            continue
        key, val = line.split('=', 1)
        cur.setdefault(key.strip(), val.strip())
    return out


def _profile_cols(raw):
    """profile values -> our columns (comma split per weapon index for the missile keys)."""
    out = {}
    for key in ('Builds', 'Trains', 'Researches', 'Upgrade', 'Revive') + SHOP_LISTS:
        if key in raw:
            out[key] = raw[key].strip('"')
    for key in ('Missilespeed', 'Missilearc', 'MissileHoming'):
        if key in raw:
            parts = raw[key].strip('"').split(',')
            for i in (1, 2):
                v = parts[i - 1] if len(parts) >= i else parts[-1]
                out['%s%d' % (key, i)] = v
    return out


def parse_misc(text, out):
    """[Misc] key=value lines (comments after // dropped); later sources override (map Misc last)."""
    sec = None
    for line in text.splitlines():
        line = line.split('//', 1)[0].strip()
        if not line:
            continue
        if line.startswith('['):
            sec = line.strip('[]').lower()
            continue
        if sec == 'misc' and '=' in line:
            k, v = line.split('=', 1)
            out[k.strip()] = v.strip()
    return out


def parse_tga(data):
    """uncompressed (2) or RLE (10) true-colour TGA -> (w, h, rows bottom-up of (r, g, b) tuples)."""
    idlen, _cmt, itype = data[0], data[1], data[2]
    w = data[12] | data[13] << 8
    h = data[14] | data[15] << 8
    bpp = data[16] // 8
    desc = data[17]
    p = 18 + idlen
    px = []
    n = w * h
    if itype == 2:
        for i in range(n):
            px.append(data[p + i * bpp:p + i * bpp + 3])
    elif itype == 10:
        while len(px) < n:
            c = data[p]
            p += 1
            cnt = (c & 0x7f) + 1
            if c & 0x80:
                v = data[p:p + 3]
                p += bpp
                px += [v] * cnt
            else:
                for _ in range(cnt):
                    px.append(data[p:p + 3])
                    p += bpp
    else:
        raise ValueError('tga type %d' % itype)
    rows = [[(q[2], q[1], q[0]) for q in px[y * w:(y + 1) * w]] for y in range(h)]   # BGR -> RGB
    if desc & 0x20:            # top-left origin -> make bottom-up
        rows.reverse()
    return w, h, rows


def footprint(data):
    """pathing texture -> [w, h, mask list bottom-up row-major]; mask bit 1 = blocks walking (red), 2 = blocks
    flying (green), 4 = blocks building (blue) [inf: channel meaning, any non-zero value blocks]."""
    w, h, rows = parse_tga(data)
    m = []
    for row in rows:
        for r, g, b in row:
            m.append((1 if r else 0) | (2 if g else 0) | (4 if b else 0))
    return [w, h, m]


def base_records(read):
    """read(name) -> bytes (KeyError/None if missing). -> {'units': {id4: record}, 'meta': {w3u id: [key, parser]},
    'misc': {key: str}, 'pathtex': {name: footprint}} (JSON-able)."""
    import w3slk as slk
    tabs = {
        'bal': slk.table(read('Units\\UnitBalance.slk').decode('latin-1'), 'unitBalanceID'),
        'data': slk.table(read('Units\\UnitData.slk').decode('latin-1'), 'unitID'),
        'weap': slk.table(read('Units\\UnitWeapons.slk').decode('latin-1'), 'unitWeapID'),
        'abil': slk.table(read('Units\\UnitAbilities.slk').decode('latin-1'), 'unitAbilID'),
    }
    prof = {}
    for race in PROFILE_FILES:
        try:
            raw = read('Units\\%sUnitFunc.txt' % race)
        except KeyError:
            raw = None
        if raw:
            parse_profile(raw.decode('latin-1'), prof)
    tabs['prof'] = {k: _profile_cols(v) for k, v in prof.items()}
    strs = {}
    for race in PROFILE_FILES:                     # display names (command UI; GetUnitName) [ext data]
        try:
            raw = read('Units\\%sUnitStrings.txt' % race)
        except KeyError:
            raw = None
        if raw:
            parse_profile(raw.decode('latin-1'), strs)
    units = {}
    for uid in sorted(set(tabs['bal']) | set(tabs['data']), key=str):
        if not isinstance(uid, str) or len(uid) != 4:
            continue
        rec = {}
        for k, (src, col, parse) in COLUMNS.items():
            row = tabs[src].get(uid, {})
            rec[k] = parse(row.get(col, 0 if parse is p_num else ''))
        nm = strs.get(uid, {}).get('Name')
        if nm:
            rec['name'] = nm.strip('"')
        # W9R2-1 (randdata.py): the random-building flag and the hero name list; kept out of COLUMNS (and so out of the
        # JUT5 record): the pack carries them in JRND
        if p_num(tabs['bal'].get(uid, {}).get('nbrandom', 0)) == 1:
            rec['nbrandom'] = 1
        pn = strs.get(uid, {}).get('Propernames')
        if pn:
            rec['propernames'] = pn.strip('"')
        units[uid] = rec
    # w3u field id -> record key (UnitMetaData: ID, field, slk, index)
    meta = {}
    _h, rows = slk.parse(read('Units\\UnitMetaData.slk').decode('latin-1'))
    for r in rows:
        src = _META_SLK.get(r.get('slk'))
        f = r.get('field')
        if src is None or not isinstance(f, str):
            continue
        idx = r.get('index')
        col = f
        if src == 'prof' and isinstance(idx, int) and idx >= 0 and f in ('Missilespeed', 'Missilearc',
                                                                          'MissileHoming'):
            col = '%s%d' % (f, idx + 1)
        key = _BY_COL.get((src, col))
        if key is not None:
            meta[r['ID']] = key
    misc = {}
    for n in ('Units\\MiscData.txt', 'Units\\MiscGame.txt'):
        parse_misc(read(n).decode('latin-1'), misc)
    pathtex = {}
    for rec in units.values():
        load_pathtex(read, rec['pathTex'], pathtex)
    return {'units': units, 'meta': meta, 'misc': misc, 'pathtex': pathtex, 'upgrades': base_upgrades(read)}


UPG_NUM = ('maxlevel', 'goldbase', 'goldmod', 'lumberbase', 'lumbermod', 'timebase', 'timemod')
# BUGS FF-UPG: the effect columns (NOTES_production section 3a): effect id (4 chars), base, mod, code (an ability / unit id) per
# effect slot 1..4, the global flag; carried in the pack section JUPE
UPG_EFF_STR = tuple('%s%d' % (c, i) for i in range(1, 5) for c in ('effect', 'code'))
UPG_EFF_NUM = tuple('%s%d' % (c, i) for i in range(1, 5) for c in ('base', 'mod')) + ('global',)


def base_upgrades(read):
    """UpgradeData.slk -> {'upg': {id4: {maxlevel, goldbase, goldmod, lumberbase, lumbermod, timebase, timemod}},
    'meta': {w3q field id: column}} (NOTES_production §3; effects not modelled yet)."""
    import w3slk as slk
    _h, rows = slk.parse(read('Units\\UpgradeData.slk').decode('latin-1'))
    upg = {}
    for r in rows:
        uid = r.get('upgradeid')
        if isinstance(uid, str) and len(uid) == 4:
            upg[uid] = {k: p_num(r.get(k, 0)) for k in UPG_NUM}
            for k in UPG_EFF_NUM:
                upg[uid][k] = p_num(r.get(k, 0))
            for k in UPG_EFF_STR:
                v = r.get(k, '')
                v = v.strip().strip('"') if isinstance(v, str) else ''
                upg[uid][k] = '' if v in ('_', '-') else v
    strs = {}
    for race in PROFILE_FILES:
        try:
            raw = read('Units\\%sUpgradeStrings.txt' % race)
        except KeyError:
            raw = None
        if raw:
            parse_profile(raw.decode('latin-1'), strs)
    for uid, r in upg.items():
        nm = strs.get(uid, {}).get('Name')
        if nm:
            r['name'] = nm.strip('"').split(',')[0]
    funcs = {}
    for race in PROFILE_FILES:                       # BUGS ETD-ORA3: per-level requirements (Requires, Requires1, ...)
        try:
            raw = read('Units\\%sUpgradeFunc.txt' % race)
        except KeyError:
            raw = None
        if raw:
            parse_profile(raw.decode('latin-1'), funcs)
    for uid, r in upg.items():
        f = funcs.get(uid, {})
        req = []
        for lv in range(1, max(1, int(r.get('maxlevel', 1) or 1)) + 1):
            sfx = '' if lv == 1 else str(lv - 1)
            req.append([f.get('Requires' + sfx, '').strip('"'), f.get('Requiresamount' + sfx, '').strip('"')])
        if any(x[0] for x in req):
            r['req'] = req                           # [[ids, amounts] per level]; absent = no requirement
    meta = {}
    try:
        _h, mrows = slk.parse(read('Units\\UpgradeMetaData.slk').decode('latin-1'))
    except KeyError:
        mrows = []
    for r in mrows:
        if r.get('slk') == 'UpgradeData' and r.get('field') in UPG_NUM + UPG_EFF_NUM + UPG_EFF_STR and isinstance(r.get('ID'), str):
            meta[r['ID']] = r['field']
        elif r.get('ID') in ('greq', 'grqc'):
            meta[r['ID']] = 'req' if r['ID'] == 'greq' else 'reqamt'     # BUGS ETD-ORA3: per-level lists (mod level = upgrade level)
        elif r.get('ID') == 'gnam':
            meta['gnam'] = 'name'
    return {'upg': upg, 'meta': meta}


def merge_upgrades(base, w3q, wts=None):
    """base_upgrades() + war3map.w3q -> {id4: record}: base ids, then custom ones."""
    out = {k: dict(v) for k, v in base['upg'].items()}
    if w3q:
        import w3map
        d = w3map.parse_objects(w3q, '.w3q')
        for table, is_custom in ((d['original_modified'], False), (d['custom'], True)):
            for o in table:
                src = (base['upg'] if is_custom else out).get(o['orig'])   # BUGS OD-1: custom from the standard
                if src is None:
                    continue
                rec = dict(src)
                for mid, _lvl, val in o['mods']:
                    k = base['meta'].get(mid)
                    if k == 'name':
                        import w3map
                        if isinstance(val, str):
                            rec['name'] = w3map.resolve(val, wts).split(',')[0]
                    elif k in ('req', 'reqamt'):
                        # BUGS ETD-ORA3: the w3q's mod level is the upgrade level (1-based; Element TD blanks levels 2 / 3)
                        rq = [list(x) for x in rec.get('req', [])]
                        while len(rq) < max(int(_lvl or 1), 1):
                            rq.append(['', ''])
                        rq[max(int(_lvl or 1), 1) - 1][0 if k == 'req' else 1] = str(val or '').strip('"')
                        rec['req'] = rq
                    elif k in UPG_EFF_STR:
                        v = str(val or '').strip().strip('"') if isinstance(val, str) else ''
                        rec[k] = '' if v in ('_', '-') else v
                    elif k is not None:
                        rec[k] = p_num(val)
                out[o['new'] if is_custom else o['orig']] = rec
    return out


def tex_key(name):
    """pathTex value -> MPQ name of the TGA (maps may write .blp; 1.21 ships .tga) [inf]."""
    n = name.replace('/', '\\')
    return re.sub(r'\.(blp|tga)$', '', n, flags=re.I) + '.tga'


def load_pathtex(read, name, pathtex):
    if not name:
        return None
    k = tex_key(name)
    if k not in pathtex:
        try:
            raw = read(k)
        except KeyError:
            raw = None
        pathtex[k] = footprint(raw) if raw else None
    return pathtex[k]


def merge(base, w3u, wts=None):
    """base_records() + w3u bytes -> {id4: record} in base order, custom types appended (= W4 JUTY order)."""
    units = base['units']
    meta = base['meta']
    types = {k: dict(v) for k, v in units.items()}
    blank = {k: (0 if p is p_num else ('' if p is p_str else p(''))) for k, (_s_, _c, p) in COLUMNS.items()}
    if w3u:
        import w3map
        d = w3map.parse_objects(w3u, '.w3u')
        for table, is_custom in ((d['original_modified'], False), (d['custom'], True)):
            for o in table:
                tid = o['new'] if is_custom else o['orig']
                # CCustomData §17 [obs 0x6f3c7f30]: a modification whose base id is unknown is purged, not kept;
                # duplicate field rows of one object collapse (the sequential apply keeps the last value).
                if is_custom and o['orig'] not in units:
                    continue
                if not is_custom and o['orig'] not in types:
                    continue
                rec = dict((units if is_custom else types).get(o['orig'], blank))
                for mid, _lvl, val in o['mods']:
                    k = meta.get(mid)
                    if k is not None:
                        rec[k] = COLUMNS[k][2](val)
                    elif mid == 'unam':
                        import w3map
                        rec['name'] = w3map.resolve(val, wts) if isinstance(val, str) else val
                    elif mid == 'upro':                      # W9R2-1: the map's hero name list wins
                        import w3map
                        rec['propernames'] = (w3map.resolve(val, wts) if isinstance(val, str) else '') or ''
                types[tid] = rec
    return types


def misc_constants(base_misc, map_misc_text=None):
    m = dict(base_misc)
    if map_misc_text:
        parse_misc(map_misc_text, m)
    return m
