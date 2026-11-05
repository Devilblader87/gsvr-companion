#!/usr/bin/env python3
"""Merged ability type data for the W6 engine (docs/NOTES_abilities.md §2).

Sources (bring your own game: read from the player's install / the map at run time, never stored in the repo):
  Units\\AbilityData.slk      base record per ability id: base code (class), levels, per level targs, Cast, Dur,
                              HeroDur, Cool, Cost, Area, Rng, DataA…DataI, UnitID, BuffID
  Units\\AbilityMetaData.slk  w3a field id -> (field, Data letter)
  war3map.w3a                 original-modified + custom abilities (custom ones keep the base code of their original)

  Units\\*AbilityFunc.txt   profile per ability id (W8): Order / Unorder / Orderon / Orderoff strings, Missilespeed,
                              Missilearc, MissileHoming (first definition wins, as unitdata.parse_profile)

A record: {'code': base code (id4), 'levels': n, 'lv': [level dict] (index 0 = level 1)} + the REC_KEYS (hero, item,
reqLevel, levelSkip, priority, checkDep, order, unorder, orderon, orderoff, mspeed, marc, mhoming); level dict keys
targs (unitdata TARG mask), Cast Dur HeroDur Cool Cost Area Rng, A…I (all reals, the game reads them as reals [obs]),
S (the raw text of the Data fields that are not numbers, e.g. AAns DataE = an order string), UnitID (str), BuffID
(list of id4). Levels beyond the SLK's (w3a 'alev') start as zeros (A-AB1 [inf]).
"""
from . import unitdata as UD

LEVEL_NUM = ('Cast', 'Dur', 'HeroDur', 'Cool', 'Cost', 'Area', 'Rng')
REC_NUM = ('hero', 'item', 'reqLevel', 'levelSkip', 'priority', 'checkDep')            # AbilityData.slk, per id
PROFILE = {'Order': 'order', 'Unorder': 'unorder', 'Orderon': 'orderon', 'Orderoff': 'orderoff',
           'Missilespeed': 'mspeed', 'Missilearc': 'marc', 'MissileHoming': 'mhoming', 'Animnames': 'anim'}
PROFILE_NUM = ('mspeed', 'marc', 'mhoming')
FUNC_FILES = ('Human', 'Orc', 'Undead', 'NightElf', 'Neutral', 'Campaign', 'Common', 'Item')
DATA = 'ABCDEFGHI'
SLK_LEVELS = 4


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _targs(v):
    m = 0
    for t in str(v or '').lower().replace('"', '').split(','):
        m |= UD.TARG.get(t.strip(), 0)
    return m


def _codes(v):
    return [c.strip() for c in str(v or '').replace('"', '').split(',') if len(c.strip()) == 4]


def _isnum(v):
    if isinstance(v, (int, float)):
        return True
    try:
        float(str(v).strip('"'))
        return True
    except ValueError:
        return False


ANIM_MAX_ENTRIES, ANIM_MAX_TAGS, ANIM_MAX_LEN = 4, 4, 48


def clean_anim(raw):
    """BUGS SPELL-ANIM: the ability's Animnames (a profile line or a map's 'aani' value; untrusted, parse only) -> the
    entries joined by '|', each entry 1..4 lowercase tags [a-z0-9] joined by ','; at most 4 entries. '' = none (the unit plays
    its plain Spell). Anything else in the text is dropped."""
    out = []
    for ent in str(raw or '').replace('"', '').split('|')[:ANIM_MAX_ENTRIES]:
        tags = []
        for t in ent.split(',')[:ANIM_MAX_TAGS]:
            t = ''.join(c for c in t.strip().lower() if c.isalnum() and c.isascii())[:16]
            if t and t not in tags:
                tags.append(t)
        e = ','.join(tags)
        if e and len(e) <= ANIM_MAX_LEN:
            out.append(e)
    return '|'.join(out)


def profile_anim(p):
    """the Animnames, Animnames1 .. entries of a profile section (Animnamescount says how many) -> clean_anim text."""
    try:
        n = int(float(str(p.get('Animnamescount', '') or '1').strip('"')))
    except ValueError:
        n = 1
    ents = [str(p.get('Animnames' if i == 0 else 'Animnames%d' % i, '')).strip('"') for i in range(max(1, min(n, ANIM_MAX_ENTRIES)))]
    return clean_anim('|'.join(e for e in ents if e))


def _blank_level():
    lv = {k: 0.0 for k in LEVEL_NUM}
    for d in DATA:
        lv[d] = 0.0
    lv['S'] = {}
    lv['targs'] = 0
    lv['UnitID'] = ''
    lv['BuffID'] = []
    return lv


def base_abilities(read):
    """read(name) -> bytes. -> {'abil': {id4: record}, 'meta': {w3a field id: [field, data index]}} (JSON-able)."""
    import w3slk as slk
    _h, rows = slk.parse(read('Units\\AbilityData.slk').decode('latin-1'))
    abil = {}
    for r in rows:
        aid = r.get('alias')
        code = r.get('code')
        if not isinstance(aid, str) or len(aid) != 4:
            continue
        n = int(_num(r.get('levels'))) or 1
        lvs = []
        for i in range(1, max(n, 1) + 1):
            lv = _blank_level()
            if i <= SLK_LEVELS:
                for k in LEVEL_NUM:
                    lv[k] = _num(r.get('%s%d' % (k, i)))
                for d in DATA:
                    v = r.get('Data%s%d' % (d, i))
                    lv[d] = _num(v)
                    if v not in (None, '', '-', '_') and not _isnum(v):
                        lv['S'][d] = str(v).strip('"')
                lv['targs'] = _targs(r.get('targs%d' % i))
                lv['UnitID'] = str(r.get('UnitID%d' % i) or '').strip('"')
                lv['BuffID'] = _codes(r.get('BuffID%d' % i))
            lvs.append(lv)
        rec = {'code': code if isinstance(code, str) and len(code) == 4 else aid, 'levels': n, 'lv': lvs}
        for k in REC_NUM:
            rec[k] = _num(r.get(k))
        abil[aid] = rec
    prof = {}
    for race in FUNC_FILES:
        try:
            raw = read('Units\\%sAbilityFunc.txt' % race)
        except KeyError:
            raw = None
        if raw:
            UD.parse_profile(raw.decode('latin-1'), prof)
    for aid, rec in abil.items():
        p = prof.get(aid, {})
        for col, k in PROFILE.items():
            v = str(p.get(col, '')).strip('"')
            rec[k] = _num(v.split(',')[0]) if k in PROFILE_NUM else v
        rec['anim'] = profile_anim(p)
    inherit_orders(abil)
    meta = {}
    _h, mrows = slk.parse(read('Units\\AbilityMetaData.slk').decode('latin-1'))
    for r in mrows:
        if not isinstance(r.get('ID'), str):
            continue
        if r.get('slk') == 'AbilityData' or (r.get('slk') == 'Profile' and r.get('field') in PROFILE):
            meta[r['ID']] = [r.get('field'), int(_num(r.get('data')))]
    return {'abil': abil, 'meta': meta}


ORDER_KEYS = ('order', 'unorder', 'orderon', 'orderoff')


def inherit_orders(abil):
    """docs/td_plan.md T1: a variant whose own profile section has no Order= (creep healing wave AChv) answers the
    order of its base code's section (AOhw: healingwave) — the order belongs to the class [inf: the game's ids per
    class; the variants' sections only carry art]. In place."""
    for aid, rec in abil.items():
        b = abil.get(rec.get('code'))
        if b is None or b is rec:
            continue
        for k in ORDER_KEYS:
            if not rec.get(k) and b.get(k):
                rec[k] = b[k]


def _set(rec, field, data, lvl, val):
    if field == 'levels':
        n = max(1, int(_num(val)))
        while len(rec['lv']) < n:
            rec['lv'].append(_blank_level())
        rec['levels'] = n
        return
    if field in REC_NUM:
        rec[field] = _num(val)
        return
    if field in PROFILE:
        k = PROFILE[field]
        rec[k] = _num(val) if k in PROFILE_NUM else (clean_anim(val) if k == 'anim' else str(val or ''))
        return
    if not lvl or lvl < 1:
        return
    while len(rec['lv']) < lvl:
        rec['lv'].append(_blank_level())
    lv = rec['lv'][lvl - 1]
    if field == 'Data' and 1 <= data <= 9:
        lv[DATA[data - 1]] = _num(val)
        if isinstance(val, str) and val and not _isnum(val):
            lv['S'][DATA[data - 1]] = val
        else:
            lv['S'].pop(DATA[data - 1], None)
    elif field in LEVEL_NUM:
        lv[field] = _num(val)
    elif field == 'targs':
        lv['targs'] = _targs(val)
    elif field == 'UnitID':
        lv['UnitID'] = str(val or '')
    elif field == 'BuffID':
        lv['BuffID'] = _codes(val)


def _copy(v):
    r = dict(v)
    r['lv'] = [dict(lv, BuffID=list(lv['BuffID']), S=dict(lv.get('S') or {})) for lv in v['lv']]
    return r


def merge(base, w3a):
    """base_abilities() + w3a bytes -> {id4: record}: base ids in SLK order, then custom ones in w3a order."""
    meta = base['meta']
    out = {}
    for k, v in base['abil'].items():
        out[k] = _copy(v)
    if w3a:
        import w3map
        d = w3map.parse_objects(w3a, '.w3a')
        for table, is_custom in ((d['original_modified'], False), (d['custom'], True)):
            for o in table:
                src = (base['abil'] if is_custom else out).get(o['orig'])   # BUGS OD-1: custom from the standard
                if src is None:
                    continue
                aid = o['new'] if is_custom else o['orig']
                rec = _copy(src)
                for (mid, lvl, val), ptr in zip(o['mods'], o['ptrs']):
                    m = meta.get(mid)
                    if m is None:
                        continue
                    field, data = m
                    _set(rec, field, ptr if field == 'Data' and ptr else data, lvl, val)
                del rec['lv'][rec['levels']:]
                out[aid] = rec
    return out
