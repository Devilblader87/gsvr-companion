#!/usr/bin/env python3
"""Spell art -> pack section WSPA (td_score Darstellung "Zauber-Effekte"): per ability the map's units use, the models
the game shows when the spell takes effect — CasterArt on the caster, TargetArt on the target unit, EffectArt at the
target point, SpecialArt [obs: Units\\*AbilityFunc.txt profile columns; the map's overrides are the w3a fields
acat / atat / aeat / asat, Units\\AbilityMetaData.slk]. Presentation only (W3Effects plays them once); the models
are packed with the script's effect models (w3pack_models.collect extra_effects).

Abilities: the abilList of the unit types the map uses (preplaced, named in the script) and of what they build /
upgrade into (towers), plus ability rawcodes named in the script; custom abilities take their base's profile, then
their own overrides. At most MAX_MODELS distinct models [inf: pack budget].

  WSPA  "ability\\tcaster\\ttarget\\teffect\\tspecial\\tmissile\\tmissile speed\\n" lines (paths as .mdx, '' = none;
        a map that blanks an art field shows nothing there, as the game [obs Skibi: ACtb amat = '']); the model budget
        goes to the abilities most unit types carry
"""
import re

ART_COLS = (('casterart', 'acat'), ('targetart', 'atat'), ('effectart', 'aeat'), ('specialart', 'asat'),
            ('missileart', 'amat'))                # Missileart flies caster -> target at Missilespeed (field amsp)
MAX_MODELS = 40


def _profiles(read):
    from jvm import abildata, unitdata as UD
    prof = {}
    for race in abildata.FUNC_FILES:
        try:
            raw = read('Units\\%sAbilityFunc.txt' % race)
        except Exception:                      # noqa: BLE001
            raw = None
        if raw:
            UD.parse_profile(raw.decode('latin-1'), prof)
    return prof


def _first_model(v):
    v = str(v or '').strip().strip('"').split(',')[0].strip()
    if not v.lower().endswith(('.mdl', '.mdx')):
        return ''
    return v[:-4] + '.mdx'


def spell_art(files, units_placed, script, log=print):
    """files.read / files.map.read; script = war3map.j bytes. -> (model paths, (b'WSPA', bytes, count) or None)."""
    import w3map
    from jvm import unitdata

    def rd(n):
        return files.read(n, missing_ok=True)

    def need(n):
        b = rd(n)
        if b is None:
            raise KeyError(n)
        return b
    w3u = files.map.read('war3map.w3u', missing_ok=True)
    types = unitdata.merge(unitdata.base_records(need), w3u)
    lits = set(m.group(1).decode('latin1') for m in re.finditer(rb"'([\x21-\x7e]{4})'", script or b''))
    uids = set(u['id'] for u in units_placed) | (lits & set(types))
    todo = list(uids)
    while todo:                                 # builds / upgrades / trains: the towers a builder makes
        t = todo.pop()
        rec = types.get(t) or {}
        for key in ('Builds', 'Upgrade', 'Trains'):
            for a in str(rec.get(key) or '').split(','):
                a = a.strip()
                if len(a) == 4 and a in types and a not in uids:
                    uids.add(a)
                    todo.append(a)
    abils = set(lits)
    uses = {}                                   # ability -> unit types carrying it (the model budget goes by use)
    for t in uids:
        for a in str((types.get(t) or {}).get('abilList') or '').split(','):
            a = a.strip()
            if len(a) == 4:
                abils.add(a)
                uses[a] = uses.get(a, 0) + 1
    prof = {k: {c.lower(): v for c, v in p.items()} for k, p in _profiles(rd).items()}
    art = {}                                    # ability id -> [caster, target, effect, special]
    w3a = files.map.read('war3map.w3a', missing_ok=True)
    custom = {}
    if w3a:
        try:
            d = w3map.parse_objects(w3a, '.w3a')
            for table, is_custom in ((d.get('original_modified', []), False), (d.get('custom', []), True)):
                for o in table:
                    custom[o['new'] if is_custom else o['orig']] = o
        except Exception as e:                 # noqa: BLE001
            log('  spell art: war3map.w3a unreadable (%s)' % e)
    for a in sorted(abils):
        o = custom.get(a)
        base = o['orig'] if o else a
        p = prof.get(base)
        if p is None and o is None:
            continue
        row = [_first_model((p or {}).get(col)) for col, _f in ART_COLS]
        try:
            spd = float(str((p or {}).get('missilespeed', '') or '0').split(',')[0] or 0)
        except ValueError:
            spd = 0.0
        if o:
            for mid, _lvl, val in o['mods']:
                for k, (_col, fid) in enumerate(ART_COLS):
                    if mid == fid and isinstance(val, str):
                        row[k] = _first_model(val)
                if mid == 'amsp' and isinstance(val, (int, float)):
                    spd = float(val)
        if any(row):
            art[a] = (row, spd)
    models, seen = [], set()
    lines = []
    for a in sorted(art, key=lambda k: (-uses.get(k, 0), k)):
        row, spd = art[a]
        keep = []
        for p in row:
            if p and p.lower() not in seen and len(models) < MAX_MODELS and rd(p) is not None:
                seen.add(p.lower())
                models.append(p)
            keep.append(p if p and p.lower() in seen else '')
        if any(keep):
            lines.append('\t'.join([a] + keep + ['%g' % (spd if spd > 0 else 900.0)]))
    if not lines:
        return [], None
    lines.sort()
    data = ('\n'.join(lines) + '\n').encode('latin-1', 'replace')
    log('  spell art: %d abilities, %d models' % (len(lines), len(models)))
    return models, (b'WSPA', data + b'\0' * ((-len(data)) % 4), len(lines))
