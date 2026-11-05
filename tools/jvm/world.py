#!/usr/bin/env python3
"""Unit type data for the reference world: the core SLKs / profile / Misc files from the game MPQs merged with the
map's war3map.w3u (original-modified + custom), see tools/jvm/unitdata.py. Read at run time from input/war3 (never
committed); the base table is cached in work/core/ (git-ignored)."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
import jass   # noqa: E402

from . import unitdata as UD   # noqa: E402

ROOT = os.environ.get('W3_ROOT') or os.path.abspath(os.path.join(HERE, '..', '..'))
CACHE = os.path.join(ROOT, 'work', 'core', 'jvm_unitdata_w5.json')

FIELDS = {          # the 12 W3/W4 keys (JUTY order): our key: (slk table, column, w3u mod id)
    'spd': ('bal', 'spd', 'umvs'),
    'hp': ('bal', 'HP', 'uhpm'),
    'mana': ('bal', 'manaN', 'umpm'),
    'mana0': ('bal', 'mana0', 'umpi'),
    'bldg': ('bal', 'isbldg', 'ubdg'),
    'fused': ('bal', 'fused', 'ufoo'),
    'fmade': ('bal', 'fmade', 'ufma'),
    'level': ('bal', 'level', 'ulev'),
    'gold': ('bal', 'goldcost', 'ugol'),
    'lumber': ('bal', 'lumbercost', 'ulum'),
    'points': ('data', 'points', 'upoi'),
    'collision': ('bal', 'collision', 'ucol'),
}

_BASE = {}


def _num(v, default=0):
    return UD.p_num(v, default)


def map_overlay(map_dir):
    """BUGS FF-SLK: the map archive's own Units\\*.slk / *.txt (old-style maps ship their custom unit, item and ability
    rows as SLKs; the game reads the map's file instead of the install's). Extracted flat to <map_dir>/Units/.
    -> (tag, {lower file name: path}) or (None, {})."""
    d = os.path.join(map_dir, 'Units') if map_dir else None
    if not d or not os.path.isdir(d):
        return None, {}
    files = {fn.lower(): os.path.join(d, fn) for fn in sorted(os.listdir(d))}
    if not files:
        return None, {}
    import hashlib
    h = hashlib.sha1()
    for k, p in sorted(files.items()):
        h.update(k.encode() + b'\0' + str(os.path.getsize(p)).encode() + b'\0')
    return h.hexdigest()[:10], files


def core_reader(ds=None, map_dir=None):
    """name -> bytes from input/war3; ds = the map's balance set (war3fs.data_set, BUGS DS-1): its file of the same
    name first; map_dir: the map's own Units files before both (map_overlay)."""
    import w3mpq as mpq
    chain = mpq.war3_chain(os.path.join(ROOT, 'input', 'war3'))
    _tag, ov = map_overlay(map_dir)
    if not ds and not ov:
        return chain.read

    def read(name, missing_ok=False):
        p = ov.get(name.replace('/', '\\').lower().split('\\')[-1]) if name.lower().startswith('units\\') else None
        if p:
            with open(p, 'rb') as fh:
                return fh.read()
        if ds:
            d = chain.read(ds + '\\' + name, missing_ok=True)
            if d is not None:
                return d
        return chain.read(name, missing_ok=missing_ok)
    return read


def map_data_set(map_dir):
    """BUGS DS-1: the balance set of an extracted map (its war3map.w3i), None = base (melee TFT)."""
    p = os.path.join(map_dir, 'war3map.w3i') if map_dir else None
    if not p or not os.path.exists(p):
        return None
    import war3fs
    with open(p, 'rb') as fh:
        return war3fs.data_set(fh.read())


def slot_order_from(w3i):
    """BUGS FF-SLOTS: the slots the humans of a lobby take, in order. A map with several real teams (w3i forces that are
    disjoint and hold two or more user slots each: Footman Frenzy 3v3v3v3, DotA 5v5, Capture the Flag) puts the first
    players on different teams (round-robin over the forces, a force's slots in slot order), as picking different team
    slots in the lobby does, so that two players are opponents. Everything else (every TD: one team of players plus a
    one-slot creep force) -> None = the slots 0, 1, 2 ... as before [inf]."""
    import w3map
    try:
        info = w3map.parse_w3i(w3i)
    except Exception:                          # noqa: BLE001
        return None
    users = sorted(p['id'] for p in info.get('players', []) if p.get('type') == 'human' and 0 <= p['id'] < 12)
    forces = []
    for f in info.get('forces', []):
        mem = [i for i in users if (f['mask'] >> i) & 1]
        if len(mem) >= 2:
            forces.append(mem)
    if len(forces) < 2 or sum(len(f) for f in forces) != len({i for f in forces for i in f}):
        return None
    out = []
    for k in range(max(len(f) for f in forces)):
        out += [f[k] for f in forces if k < len(f)]
    return out


def slot_order(map_dir):
    p = os.path.join(map_dir, 'war3map.w3i') if map_dir else None
    if not p or not os.path.exists(p):
        return None
    with open(p, 'rb') as fh:
        return slot_order_from(fh.read())


def _cache_path(suffix, ds, tag=None):
    return CACHE.replace('.json', suffix + ('_' + ds.lower() if ds else '') + ('_ov' + tag if tag else '') + '.json')


def _src_digest(mod):
    """sha1 of the module that builds a cache (BUGS W8-6: a new column must rebuild the cache)."""
    import hashlib
    with open(mod.__file__, 'rb') as fh:
        return hashlib.sha1(fh.read()).hexdigest()[:16]


def _cached(path, mod, build):
    """the JSON cache at path, rebuilt when the building module changed since it was written."""
    dig = _src_digest(mod)
    if os.path.exists(path):
        with open(path) as fh:
            b = json.load(fh)
        if isinstance(b, dict) and b.get('_src') == dig:
            return b
    b = build()
    b['_src'] = dig
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as fh:
        json.dump(b, fh, sort_keys=True)
    return b


def sound_labels():
    """w3sound.sound_labels() of the core install (cached next to the unit cache): {label: [file, volume]}."""
    import sys as _s
    _s.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
    import w3sound as WS
    p = CACHE.replace('.json', '_sndlabels.json')
    rd = core_reader()

    def read(n):
        try:
            return rd(n)
        except Exception:                      # noqa: BLE001
            return None
    d = _cached(p, WS, lambda: {'labels': WS.sound_labels(read)})
    return {k: tuple(v) for k, v in d['labels'].items()}


def engine_base(ds=None, map_dir=None):
    """unitdata.base_records() of the install for a balance set (cached per set; per map when it ships Units files)."""
    tag, _ov = map_overlay(map_dir)
    k = 'b' + (ds or '') + (tag or '')
    if k in _BASE:
        return _BASE[k]
    b = _cached(_cache_path('', ds, tag), UD, lambda: UD.base_records(core_reader(ds, map_dir)))
    _BASE[k] = b
    return b


def ability_base(ds=None, map_dir=None):
    """abildata.base_abilities() of the install (cached next to the unit cache, per balance set)."""
    tag, _ov = map_overlay(map_dir)
    k = 'a' + (ds or '') + (tag or '')
    if k in _BASE:
        return _BASE[k]
    from . import abildata as AD
    a = _cached(_cache_path('_abil', ds, tag), AD, lambda: AD.base_abilities(core_reader(ds, map_dir)))
    a.pop('_src', None)                              # keyed by ability id: the digest is no ability
    _BASE[k] = a
    return a


def load_abilities(map_dir):
    """-> {ability id int: abildata record} (base + the map's war3map.w3a)."""
    from . import abildata as AD
    w3a = None
    p = os.path.join(map_dir, 'war3map.w3a') if map_dir else None
    if p and os.path.exists(p):
        with open(p, 'rb') as fh:
            w3a = fh.read()
    return {jass.rawcode(k): v for k, v in AD.merge(ability_base(map_data_set(map_dir), map_dir), w3a).items()}


def item_base(ds=None, map_dir=None):
    """itemdata.base_items() of the install (cached next to the unit cache, per balance set)."""
    tag, _ov = map_overlay(map_dir)
    k = 'i' + (ds or '') + (tag or '')
    if k in _BASE:
        return _BASE[k]
    from . import itemdata as ID
    b = _cached(_cache_path('_item', ds, tag), ID, lambda: ID.base_items(core_reader(ds, map_dir)))
    _BASE[k] = b
    return b


def load_items(map_dir, wts=None):
    """-> [item record] in the game's list order (ItemData.slk + the map's war3map.w3t, NOTES_items §1.1)."""
    from . import itemdata as ID
    w3t = None
    p = os.path.join(map_dir, 'war3map.w3t') if map_dir else None
    if p and os.path.exists(p):
        with open(p, 'rb') as fh:
            w3t = fh.read()
    return ID.merge(item_base(map_data_set(map_dir), map_dir), w3t, wts)


def load_upgrades(map_dir, wts=None):
    """-> {research id int: record} (UpgradeData.slk + the map's war3map.w3q)."""
    w3q = None
    p = os.path.join(map_dir, 'war3map.w3q') if map_dir else None
    if p and os.path.exists(p):
        with open(p, 'rb') as fh:
            w3q = fh.read()
    return {jass.rawcode(k): v for k, v in UD.merge_upgrades(engine_base(map_data_set(map_dir), map_dir)['upgrades'], w3q, wts).items()}


def base_table():
    return engine_base()['units']


def load_unit_types(map_dir, wts=None):
    """-> {type id int: record}; record keys = unitdata.KEYS + 'name', 'id4'."""
    base = engine_base(map_data_set(map_dir), map_dir)
    w3u = None
    p = os.path.join(map_dir, 'war3map.w3u') if map_dir else None
    if p and os.path.exists(p):
        with open(p, 'rb') as fh:
            w3u = fh.read()
    types = UD.merge(base, w3u, wts)
    out = {}
    for k, v in types.items():
        v['id4'] = k
        out[jass.rawcode(k)] = v
    return out


def dest_types_from(read, w3b, pathtex):
    """BUGS DS-2: destructable types {id4: {'pathTex', 'pathTexDeath', 'hp'}} (DestructableData.slk of the map's balance
    set + the map's war3map.w3b: bptx / bptd / bhps), their path textures loaded into pathtex. read: the install
    (with the map's balance set)."""
    import w3slk as slk
    import w3map
    _h, rows = slk.parse(read('Units\\DestructableData.slk').decode('latin-1'))
    out = {}
    for r in rows:
        if isinstance(r, dict) and r.get('DestructableID'):
            out[r['DestructableID']] = {'pathTex': r.get('pathTex') or '', 'pathTexDeath': r.get('pathTexDeath') or '',
                                        'hp': _num(r.get('HP'), 0),
                                        'targ': UD.TARG.get(str(r.get('targType') or '').strip().lower(), 0)}
    if w3b:
        d = w3map.parse_objects(w3b, '.w3b')
        std = dict(out)                                  # BUGS OD-1: custom types start from the standard record
        for table, custom in ((d['original_modified'], False), (d['custom'], True)):
            for o in table:
                src = (std if custom else out).get(o['orig'])
                if src is None:
                    continue
                rec = dict(src)
                for mid, _lvl, val in o['mods']:
                    if mid == 'bptx':
                        rec['pathTex'] = val or ''
                    elif mid == 'bptd':
                        rec['pathTexDeath'] = val or ''
                    elif mid == 'bhps':
                        rec['hp'] = _num(val, 0)
                    elif mid == 'btar':
                        rec['targ'] = UD.TARG.get(str(val or '').strip().lower(), 0)
                out[o['new'] if custom else o['orig']] = rec
    for rec in out.values():
        for k in ('pathTex', 'pathTexDeath'):
            n = rec[k]
            if n in ('', '_'):
                rec[k] = ''
            else:
                UD.load_pathtex(read, n, pathtex)
    return out


def placed_dests_from(doo, types):
    """the destructables the game places from war3map.doo: solid ones (flags bit 2; flags 0 = created by the map
    script, CreateAllDestructables [obs Blizzard TD gates]) -> [(id4, x, y, angle rad, life %)]."""
    import w3map
    if not doo:
        return []
    d = w3map.parse_doo(doo)
    return [(x['id'], x['x'], x['y'], x['angle'], x['life']) for x in d['doodads']
            if x['id'] in types and x['flags'] & 2]


def _map_file(map_dir, name):
    p = os.path.join(map_dir, name) if map_dir else None
    if p and os.path.exists(p):
        with open(p, 'rb') as fh:
            return fh.read()
    return None


def dest_types(map_dir, pathtex):
    return dest_types_from(core_reader(map_data_set(map_dir), map_dir), _map_file(map_dir, 'war3map.w3b'), pathtex)


def placed_dests(map_dir, types):
    return placed_dests_from(_map_file(map_dir, 'war3map.doo'), types)


def load_map_env(map_dir):
    """the engine's map inputs: Misc constants (core + war3mapMisc.txt), wpm pathing grid, map origin, path textures.
    -> dict or None without a map."""
    base = engine_base(map_data_set(map_dir), map_dir)
    env = {'misc': dict(base['misc']), 'pathtex': base['pathtex'], 'wpm': None, 'origin': (0.0, 0.0),
           'reader': None, 'abilities': load_abilities(None), 'upgrades': load_upgrades(None),
           'items': load_items(None), 'dest_types': {}, 'dests': []}
    if not map_dir or not os.path.isdir(map_dir):
        return env
    env['abilities'] = load_abilities(map_dir)
    env['pathtex'] = dict(env['pathtex'])          # destructable textures join the units' (cache untouched)
    env['dest_types'] = dest_types(map_dir, env['pathtex'])
    env['dests'] = placed_dests(map_dir, env['dest_types'])
    env['upgrades'] = load_upgrades(map_dir)
    env['items'] = load_items(map_dir)
    mp = os.path.join(map_dir, 'war3mapMisc.txt')
    if os.path.exists(mp):
        with open(mp, 'rb') as fh:
            UD.parse_misc(fh.read().decode('latin-1'), env['misc'])
    wp = os.path.join(map_dir, 'war3map.wpm')
    we = os.path.join(map_dir, 'war3map.w3e')
    if os.path.exists(wp) and os.path.exists(we):
        import struct
        import w3map
        with open(wp, 'rb') as fh:
            env['wpm'] = w3map.parse_wpm(fh.read())
        with open(we, 'rb') as fh:
            head = fh.read(4096)
        # w3e header: magic, version, tileset, custom flag, ground tiles, cliff tiles, w, h, origin x, y
        p = 4 + 4 + 1 + 4
        ng = struct.unpack_from('<i', head, p)[0]
        p += 4 + 4 * ng
        nc = struct.unpack_from('<i', head, p)[0]
        p += 4 + 4 * nc + 8
        env['origin'] = struct.unpack_from('<2f', head, p)
    return env
