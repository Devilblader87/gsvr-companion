"""W2 model sections of a W3P1 pack (contract: docs/w3p_models.md; called by tools/w3pack.py).

collect(files, ...) finds the model per object type (merged object data: core SLKs + the map's w3u/w3d/w3b diffs),
bake() skins every model with tools/mdx.py (bind pose + vertex-animation frames at 15 fps for unit models, frame 0
for doodads / destructables / buildings), build_atlas() packs the textures into one atlas (MATX + TEXS) and
sections() lays everything out as MLST MNAM GEOS MVTX MIDX SEQS SNAM MVAT MATX TEXS DTYM UTYM.

Object-data fields used (ids from Units\\UnitMetaData.slk, Doodads\\DoodadMetaData.slk,
Units\\DestructableMetaData.slk): umdl -> UnitUI file, usca -> UnitUI modelScale, ubdg -> UnitBalance isbldg,
dfil / dvar -> Doodads file / numVar, bfil / bvar / btxi / btxf -> DestructableData file / numVar / texID / texFile.
"""
import array, math, os, re, struct

import mdx, w3slk as slk, w3map

FPS = 15.0
# BUGS P-WALK: walk cycles are baked at 30 fps (the shader plays every sequence at frames / duration, W3ModelLoader.SeqFps):
# the legs of a walk swing 60-75 degrees inside two frames of the 15 fps grid (Grunt: 33 ms), and the shader's linear
# lerp between baked frames cut the chord - limbs shrank and flickered ("wild bad" walking; Footman error 5.8 -> 1.5 model
# units, Grunt 13.1 -> 3.6, Ghoul 15.8 -> 7.5). Other sequences keep 15 fps.
WALK_FPS = 30.0
MAX_SEQS, MAX_SEQ_FRAMES, MAX_MODEL_FRAMES = 12, 48, 360
# BUGS FX-ANIM: effect, spell art and missile models (Request.emit / MISSILE_SEQS) are small and short-lived, but their
# parts move fast (a missile's burst, a spell's rings): their Stand / Birth / Death are baked at EFFECT_FPS with a higher
# cap, so a 2-3 s Death keeps its frame rate instead of being resampled to 48 frames and played at 15 fps.
EFFECT_FPS = 30.0
EFFECT_SEQ_FRAMES, EFFECT_MODEL_FRAMES = 60, 480
EFFECT_MAX_VERTS = 600            # bigger models (a hero used as spell art, a unit as missile art) keep the unit rules
EFFECT_SEQS = ('stand', 'birth', 'death')      # the sequences the world plays on an effect (W3Effects, impacts, flight)
# BUGS UNIT-ANIM: 'stand work' (a builder's hammering, a wisp harvesting) is its own category - before it, 'Stand Work' was
# one of the extra Stand variants the world never plays and the budget dropped first. The order is the bake order of a
# model's first sequence of each category (and the order the 12-sequence / frame caps are filled in).
SEQ_PREFIXES = ('stand', 'walk', 'attack', 'death', 'stand work', 'spell', 'birth', 'morph')
# BUGS UNIT-ANIM: the world plays Attack / Spell / Stand Work on units (W3Present poses): 30 fps like the walk - a swing moves
# a limb 60+ degrees inside two frames of the 15 fps grid (the lerp error in BUGS FX-BLEND: Footman 34 model units at 15 fps,
# 11 at 30). Effect models (fx) keep their own rules.
ACTION_FPS = 30.0
ACTION_PREFIXES = ('attack', 'spell', 'stand work')
ATLAS_W, ATLAS_MAX_H, ATLAS_MIPS, BORDER, MAX_TEX = 2048, 4096, 4, 4, 256
# MATX as DXT5 (BC3, 1 byte / texel instead of 4): Azeroth Grand Prix was 108 MB > VRChat's 100 MB download limit
# with an RGBA atlas of 34.5 MB (the maintainer's first live test, BUGS L-8). The mips field carries the format: + MATX_DXT5.
ATLAS_DXT5 = True
MATX_DXT5 = 256


def use_dxt5():
    """DXT5 atlases when wanted and this Pillow can write them (else RGBA: bigger, but correct; BUGS L-10)."""
    return ATLAS_DXT5 and dxt5_supported()


_DXT5_OK = []


def dxt5_supported():
    """Pillow writes DXT5 DDS (pixel_format='DXT5') only from 11.2 on; older ones silently write an uncompressed DDS
    and dxt5() would cut garbage out of it (BUGS L-10: the maintainer's Windows companion with Pillow 11.1 -> noise textures).
    A probe once: the header's FourCC (offset 84) must be DXT5."""
    if not _DXT5_OK:
        import io
        from PIL import Image
        b = io.BytesIO()
        try:
            Image.new('RGBA', (4, 4), (255, 0, 0, 255)).save(b, 'DDS', pixel_format='DXT5')
            _DXT5_OK.append(b.getvalue()[84:88] == b'DXT5')
        except Exception:                                      # noqa: BLE001 — no DDS writer at all
            _DXT5_OK.append(False)
    return _DXT5_OK[0]


def dxt5(img):
    """PIL RGBA (w, h multiples of 4) -> raw DXT5 blocks (Pillow's DDS writer, header dropped). Raises when this
    Pillow cannot write DXT5 (the header is checked, never a silent garbage cut)."""
    import io
    b = io.BytesIO()
    img.save(b, 'DDS', pixel_format='DXT5')
    raw = b.getvalue()
    if raw[84:88] != b'DXT5':
        raise RuntimeError('Pillow %s wrote no DXT5 DDS' % __import__('PIL').__version__)
    need = (img.size[0] // 4) * (img.size[1] // 4) * 16
    return raw[len(raw) - need:]
BUDGET = 60 * 1024 * 1024
PARTICLE_TEX = 128                       # particle-only textures at most this size (an 8 x 8 flipbook: 16 px cells)
PARTICLE_TEX_BUDGET = 8 * 1024 * 1024    # BUGS P-PART: extra budget for the particle-only textures (RGBA accounting)
# MDX layer shading flags -> contract GEOS flags
SHADE_MAP = ((0x1, 1), (0x10, 2), (0x20, 16), (0x40, 32), (0x80, 64))
GEO_BILLBOARD = 256              # faces the camera (VAT = pivot, MVTX normal = offset)
GEO_TEAM_UNDERLAY = 128          # CHANGED by converter: see docs/w3p_models.md
REPL_TEAM, REPL_GLOW = 1, 2
MAX_EMITTERS = 16               # PRE2 emitters packed per model (MPEM, w3p1-45), file order
EMIT_SAMPLE_MS = 25.0            # visibility / emission-rate sampling step inside a sequence
# MPEM flags (docs/w3p_models.md): head / tail quads, PRE2 node flags [ext MDX node flags], squirt
PF_HEAD, PF_TAIL, PF_UNSHADED, PF_LINE, PF_MODELSPACE, PF_XYQUAD, PF_SQUIRT, PF_UNFOGGED = 1, 2, 4, 8, 16, 32, 64, 128
NODE_PF = ((0x8000, PF_UNSHADED), (0x20000, PF_LINE), (0x40000, PF_UNFOGGED), (0x80000, PF_MODELSPACE),
           (0x100000, PF_XYQUAD))
MLST_EMITTERS = 2                # MLST flag: the model has packed particle emitters
MISSILE_SEQS = 'missile'         # Request.animated for missile art: the bind Stand (flight, BUGS P-MISSILE) + the first
                                 # Death (the impact burst at the target, BUGS P-IMPACT); nothing else is baked


# ---------------------------------------------------------------- object data

def _model_path(f):
    if not isinstance(f, str) or not f or f in ('_', '-'):
        return None
    f = f.strip()
    low = f.lower()
    if low.endswith(('.mdl', '.mdx')):
        f = f[:-4]
    return f if f not in ('', '_', '-') else None      # "_.mdl": a dummy unit without a model on purpose


def _apply_mods(table, objs, fieldmap, key_field):
    """merge an object-data diff (parse_objects result) into `table` (id -> row dict). -> set of touched ids."""
    touched = set()
    std = {k: dict(v) for k, v in table.items()}       # BUGS OD-1: custom objects start from the standard row
    for kind in ('original_modified', 'custom'):
        for o in objs.get(kind, []):
            orig, new = o['orig'], o['new']
            if kind == 'custom' and new and new != '\0\0\0\0':
                row = dict(std.get(orig, {}))
                row[key_field] = new
                table[new] = row
                tid = new
            else:
                tid = orig
                table.setdefault(tid, {key_field: tid})
            touched.add(tid)
            for mid, _lvl, val in o['mods']:
                f = fieldmap.get(mid)
                if f:
                    table[tid][f] = val
    return touched


def object_tables(files, log):
    def tab(name, key):
        raw = files.read(name, missing_ok=True)
        return slk.table(raw.decode('latin1'), key) if raw else {}

    units = tab('Units\\UnitUI.slk', 'unitUIID')
    bal = tab('Units\\UnitBalance.slk', 'unitBalanceID')
    for k, r in units.items():
        r['isbldg'] = bal.get(k, {}).get('isbldg', 0)
    move = tab('Units\\unitdata.slk', 'unitID')           # BUGS UNIT-HEIGHT: movetp + moveHeight (UnitData)
    dood = tab('Doodads\\Doodads.slk', 'doodID')
    dest = tab('Units\\DestructableData.slk', 'DestructableID')
    touched_units = set()
    for ext, table, fmap, key in (('w3u', units, {'umdl': 'file', 'usca': 'modelScale', 'ubdg': 'isbldg', 'ussc': 'scale',
                                                  'uslz': 'selZ', 'uubs': 'uberSplat', 'uclr': 'red', 'uclg': 'green',
                                                  'uclb': 'blue', 'utco': 'teamColor', 'utcc': 'customTeamColor',
                                                  'uver': 'fileVerFlags'},
                                   'unitUIID'),
                                  ('w3d', dood, {'dfil': 'file', 'dvar': 'numVar'}, 'doodID'),
                                  ('w3b', dest, {'bfil': 'file', 'bvar': 'numVar', 'btxi': 'texID', 'btxf': 'texFile'},
                                   'DestructableID')):
        raw = files.map.read('war3map.' + ext, missing_ok=True)
        if not raw:
            continue
        try:
            objs = w3map.parse_objects(raw, '.' + ext)
            t = _apply_mods(table, objs, fmap, key)
            if ext == 'w3u':                             # BUGS UNIT-HEIGHT: the move type + height of the map's own types
                _apply_mods(move, objs, {'umvt': 'movetp', 'umvh': 'moveHeight'}, 'unitID')
        except Exception as e:
            log('  models: war3map.%s unreadable (%s), ignored' % (ext, e))
            continue
        if ext == 'w3u':
            touched_units = t
    return units, dood, dest, touched_units, move


MOVE_KINDS = {'fly': 1, 'hover': 2, 'float': 2, 'amph': 3}      # UHGT kind: 1 flies, 2 sits on the water surface, 3 amphibious
HEIGHT_MAX = 4096.0


def move_height(row):
    """BUGS UNIT-HEIGHT: (kind, height) of a unit type's UnitData row (+ the map's umvt / umvh): kind 0 walks (foot, horse,
    unbuild, buildings), 1 flies, 2 hover / float (drawn on the water surface where there is water), 3 amphibious; the
    height is "Movement - Height" in world units, kept for the three non-walking kinds only. Map data is untrusted: only
    finite numbers are used, clamped to 0..HEIGHT_MAX."""
    if not row:
        return 0, 0.0
    tp = row.get('movetp')
    kind = MOVE_KINDS.get(tp.strip().lower(), 0) if isinstance(tp, str) else 0
    h = row.get('moveHeight')
    if kind == 0 or isinstance(h, bool) or not isinstance(h, (int, float)) or not math.isfinite(h):
        return kind, 0.0
    return kind, float(min(HEIGHT_MAX, max(0.0, h)))


def pack_uhgt(rows):
    """UHGT section bytes (w3p1-74, BUGS UNIT-HEIGHT): per row (unit id, kind, height) char[4] id, i32 kind, f32 height."""
    out = bytearray()
    for uid, kind, h in rows:
        out += struct.pack('<4sif', uid.encode('latin1')[:4].ljust(4, b'\0'), kind, h)
    return bytes(out)


def look_flags(row):
    """UTYM flags word (BUGS ETD-LOOK): bit 0 = building (as before); bits 1..5 = forced team colour + 1 (0 = the
    owner's: UnitUI customTeamColor set and teamColor >= 0); bits 8..31 = the UnitUI tint red / green / blue as
    255 - value, one byte each (0 = white, so an old pack or an untinted type reads as no tint). Element TD alone gives
    82 unit types a tint or a team colour (dual towers on one shared model, the pure elements on one wisp)."""
    def num(k, d):
        v = row.get(k, d)
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return d
    f = 1 if row.get('isbldg') in (1, '1', True) else 0
    tc = num('teamColor', -1)
    if num('customTeamColor', 0) and 0 <= tc < 31:
        f |= (tc + 1) << 1
    for sh, k in ((8, 'red'), (16, 'green'), (24, 'blue')):
        c = min(255, max(0, num(k, 255)))
        f |= (255 - c) << sh
    return f - (1 << 32) if f >= (1 << 31) else f


def unit_model_path(row, has):
    """the model path (no extension) a unit type's UnitUI row shows in the expansion. BUGS ETD-VER [inf]: UnitUI
    fileVerFlags 2 (19 base types: Priest, Sorceress, Gyrocopter, Siege Engine, Demolisher, Headhunter, Ballista, the
    Liches, the Murlocs ...) means the game loads "<file>_V1" (the expansion's version of the model) — the install ships
    only the _V1 file next to the old one, and Element TD's n00S names catapult_V1 itself."""
    base = _model_path(row.get('file'))
    if base and not base.lower().endswith(('_v1', '_v0')):
        try:
            fl = int(float(row.get('fileVerFlags', 0) or 0))
        except (TypeError, ValueError):
            fl = 0
        if fl & 2 and has(base + '_V1.mdx'):
            return base + '_V1'
    return base


def unit_anim_entries(rec, animmap):
    """BUGS SPELL-ANIM: the Animnames entries (see abildata.clean_anim) of a unit type's abilities (abilList + heroAbilList)."""
    out = set()
    for fld in ('abilList', 'heroAbilList'):
        for a in str((rec or {}).get(fld, '') or '').split(','):
            for e in animmap.get(a.strip(), '').split('|'):
                if e:
                    out.add(e)
    return out


class Request:
    """one baked model: (path, replaceable-texture override)."""

    def __init__(self, path, tex_override):
        self.path = path
        self.tex_override = tex_override      # (replaceable id, texture path) or None
        self.animated = False
        self.emit = False                     # pack its PRE2 particle emitters (effects, spell art, missiles)
        self.anim = set()                     # BUGS SPELL-ANIM: Animnames entries of the abilities of the unit types on this model
        self.index = -1


def collect(files, doo, units_placed, log, effects=True, extra_effects=()):
    """-> (requests in order, dtym rows [(id, var, request)], utym rows [(id, request, scale, flags)], number of
    effect models, extra = {'missiles': w3missileart.missile_art rows, 'sel': [(id, selection scale, selZ)]})."""
    utab, dtab, btab, touched, mtab = object_tables(files, log)
    reqs = {}
    exists = {}

    def has(path):
        if path not in exists:
            exists[path] = files.read(path, missing_ok=True) is not None
        return exists[path]

    def request(path, override=None):
        key = (path.lower(), override and (override[0], override[1].lower()))
        r = reqs.get(key)
        if r is None:
            r = reqs[key] = Request(path, override)
        return r

    dtym = []
    missing = set()
    for did, var in sorted(set((d['id'], d['var']) for d in doo)):
        row = dtab.get(did)
        override = None
        if row is None:
            row = btab.get(did)
            if row is not None:
                tid = row.get('texID')
                tf = row.get('texFile')
                if isinstance(tid, int) and tid > 0 and isinstance(tf, str) and tf not in ('', '_', '-'):
                    tf = tf if tf.lower().endswith('.blp') else tf + '.blp'
                    override = (tid, tf)
        base = _model_path(row.get('file')) if row else None
        if not base:
            missing.add(did)
            continue
        nvar = row.get('numVar', 1)
        nvar = nvar if isinstance(nvar, int) and nvar > 0 else 1
        cands = ([base + str(var % nvar) + '.mdx', base + '.mdx'] if nvar > 1 else [base + '.mdx', base + '0.mdx'])
        path = next((c for c in cands if has(c)), None)
        if path is None:
            missing.add('%s:%s' % (did, cands[0]))
            continue
        dtym.append((did, var, request(path, override)))

    # unit types: preplaced + defined / modified by the map + rawcodes named in the script
    uids = set(u['id'] for u in units_placed) | set(touched)
    script = files.map.read('war3map.j', missing_ok=True) or files.map.read('scripts\\war3map.j', missing_ok=True)
    if script:
        for m in re.finditer(rb"'([\x21-\x7e]{4})'", script):
            k = m.group(1).decode('latin1')
            if k in utab:
                uids.add(k)
        import w3ui                                      # BUGS MELEE-SEEDS: the start units of MeleeStartingUnits()
        uids |= set(k for k in w3ui.melee_seeds(script) if k in utab)
    # BUGS P-UPGM: every type the map can reach from those (Builds, Trains, Upgrade, sold units), transitively —
    # Wintermaul's Guard / Cannon / Arcane tower upgrades are base types nobody places or names: no model, the
    # upgraded tower vanished (maintainer 2026-10-24)
    objs = (getattr(files, 'ui_objs', None) or {}).get('units') or {}
    animmap = (getattr(files, 'ui_objs', None) or {}).get('abil_anim') or {}
    todo = list(uids)
    while todo:
        rec = objs.get(todo.pop())
        if not rec:
            continue
        for fld in ('Builds', 'Trains', 'Upgrade', 'Sellunits'):
            for k in str(rec.get(fld, '') or '').split(','):
                k = k.strip()
                if len(k) == 4 and k in utab and k not in uids:
                    uids.add(k)
                    todo.append(k)
    utym = []
    for uid in sorted(uids):
        row = utab.get(uid)
        base = unit_model_path(row, has) if row else None
        if row and base is None:
            # no model on purpose (dummy casters, "_.mdl"): UTYM model -1 — the world draws nothing, no fallback box
            sc = row.get('modelScale', 1.0)
            utym.append((uid, None, float(sc) if isinstance(sc, (int, float)) else 1.0, look_flags(row)))
            continue
        if not base or not has(base + '.mdx'):
            missing.add('%s:%s' % (uid, base))
            continue
        r = request(base + '.mdx')
        bld = 1 if row.get('isbldg') in (1, '1', True) else 0
        if not bld:
            r.animated = True
            r.anim |= unit_anim_entries(objs.get(uid), animmap)
        sc = row.get('modelScale', 1.0)
        utym.append((uid, r, float(sc) if isinstance(sc, (int, float)) else 1.0, look_flags(row)))
    # BUGS P-SELRING: the selection circle size + height of every unit type the map uses (with or without a model)
    sel = []
    for uid in sorted(uids):
        row = utab.get(uid)
        if row is None:
            continue
        sc, sz = row.get('scale', 1.0), row.get('selZ', 0.0)
        sel.append((uid, float(sc) if isinstance(sc, (int, float)) else 1.0, float(sz) if isinstance(sz, (int, float)) else 0.0))
    # UHGT (BUGS UNIT-HEIGHT): the move kind + height of every non-walking unit type the map uses (a walker is at ground level)
    fly = []
    for uid in sorted(uids):
        kind, h = move_height(mtab.get(uid))
        if kind:
            fly.append((uid, kind, h))
    # USPL (NOTES_units §4.2): the ground decal (ubersplat) of every structure type the map uses
    splat = []
    for uid in sorted(uids):
        row = utab.get(uid)
        if row is None or row.get('isbldg') not in (1, '1', True):
            continue
        nm = row.get('uberSplat')
        if isinstance(nm, str) and nm and nm != '_':
            splat.append((uid, nm))
    # BUGS P-MISSILE: the attack missile art of those unit types (towers included) — animated like the effects
    marts = {}
    try:
        import w3missileart
        marts = w3missileart.missile_art(files, [u for u, _r, _s, _f in utym] + [u for u, _s, _z in sel], log)
    except Exception as e:              # noqa: BLE001
        log('  models: missile art skipped (%s: %s)' % (type(e).__name__, e))
    nmis = 0
    for rows in marts.values():
        for _i, p, _a, _t, _lz, _iz in rows:
            if p and p != '-' and has(p):
                r = request(p)
                r.emit = True
                if not r.animated:
                    nmis += 1
                    r.animated = MISSILE_SEQS    # a flying missile loops its Stand, its Death plays at the impact
    if marts:
        log('  models: missile art of %d unit types, %d models' % (len(marts), nmis))
    # td_plan T4: special-effect models the script names (AddSpecialEffect* paths: string literals ending in
    # .mdl / .mdx with JASS's doubled backslashes; the game loads the .mdx of a .mdl name [ext]) — animated
    neff = 0
    if script and effects:
        seen = set()
        for m in re.finditer(rb'"((?:[^"\\]|\\.)*\.md[lx])"', script, re.I):
            p = m.group(1).decode('latin1').replace('\\\\', '\\')
            p = p[:-4] + '.mdx'
            if p.lower() in seen:
                continue
            seen.add(p.lower())
            if has(p):
                r = request(p)
                r.animated = True
                r.emit = True
                neff += 1
        if neff:
            log('  models: %d special-effect models named by the script' % neff)
    if effects:                         # td_score: the spell art (tools/w3spellart.py) — animated like the effects
        for p in extra_effects:
            if has(p):
                r = request(p)
                r.animated = True
                r.emit = True
    if missing:
        log('  models: %d types without a readable model: %s' % (len(missing), ' '.join(sorted(missing)[:30])
                                                                + (' ...' if len(missing) > 30 else '')))
    return list(reqs.values()), dtym, utym, neff, {'missiles': marts, 'sel': sel, 'splat': splat, 'fly': fly}


# ---------------------------------------------------------------- baking (worker; pure function of the MDX bytes)

_PRIO_LONGEST_FIRST = sorted(range(len(SEQ_PREFIXES)), key=lambda k: -len(SEQ_PREFIXES[k]))


def _seq_priority(name):
    """category index (SEQ_PREFIXES) of a sequence name, the longest matching prefix first ('Stand Work' is not a 'Stand')."""
    low = name.lower()
    for k in _PRIO_LONGEST_FIRST:
        if low.startswith(SEQ_PREFIXES[k]):
            return k
    return None


_EXTRA_ATTACK = re.compile(r'attack\s*-?\s*2\b')


def extra_attack(seq):
    """the second attack animation a unit alternates with ('Attack - 2', 'Attack -2 ') - not the carried-load / stance variants."""
    return bool(_EXTRA_ATTACK.match(seq['name'].lower()))


def _choose_layer(m, mat):
    """-> (texture key, filter, flags, alpha, team underlay)."""
    layers = mat['layers'] if mat else []
    if not layers:
        return None, 0, 0, 1.0, False

    def repl(L):
        t = L['texture']
        return m.textures[t][0] if 0 <= t < len(m.textures) else 0
    chosen = None
    under = False
    for L in layers:
        if repl(L) == REPL_TEAM:
            under = True
            continue
        chosen = L
        break
    if chosen is None:
        chosen, under = layers[0], False
    t = chosen['texture']
    key = None
    if 0 <= t < len(m.textures):
        rid, path, tfl = m.textures[t]
        if rid:
            key = ('repl', rid, tfl & 3)
        elif path:
            key = ('file', path, tfl & 3)
    flags = 0
    for src, dst in SHADE_MAP:
        if chosen['shading'] & src:
            flags |= dst
    filt = chosen['filter'] if chosen['filter'] <= 6 else 0
    if under:
        flags |= GEO_TEAM_UNDERLAY
        if filt in (1, 2):
            filt = 0
    return key, filt, flags, chosen['alpha'], under


def fx_seq(seq, fx):
    """the sequence is one an effect model plays (Stand / Birth / Death) and baked at EFFECT_FPS."""
    return bool(fx) and seq['name'].lower().startswith(EFFECT_SEQS)


def seq_fps(seq, fx=False, legacy=False):
    """baked frame rate of a sequence: EFFECT_FPS for the Stand / Birth / Death of a small effect / spell art / missile
    model (fx), WALK_FPS for a walk cycle (any case: 25 unit models name it 'walk'), else FPS."""
    if fx_seq(seq, fx):
        return EFFECT_FPS
    low = seq['name'].lower()
    if not fx and not legacy and low.startswith(ACTION_PREFIXES):
        return ACTION_FPS
    return WALK_FPS if low.startswith('walk') else FPS


def _frame_times(seq, fx=False, legacy=False):
    start, end = seq['start'], seq['end']
    dur = max(0, end - start)
    nonloop = bool(seq['flags'] & 1)
    n = int(round(dur * seq_fps(seq, fx, legacy) / 1000.0))
    if nonloop:
        n += 1
    n = max(1, min(EFFECT_SEQ_FRAMES if fx_seq(seq, fx) else MAX_SEQ_FRAMES, n))
    if nonloop:
        return [start + (dur * k / (n - 1) if n > 1 else 0) for k in range(n)]
    return [start + dur * k / n for k in range(n)]


def _first_key(tr, default):
    """[inf] an animated PRE2 parameter (speed, latitude ...) is reduced to its first key."""
    return tr.vals[0][0] if tr is not None and tr.frames else default


def _emit_tex_key(m, ti):
    if not (0 <= ti < len(m.textures)):
        return None
    rid, path, tfl = m.textures[ti]
    if rid in (REPL_TEAM, REPL_GLOW):
        return ('repl', rid, 0)
    if rid:
        return ('repl', rid, tfl & 3)
    return ('file', path, tfl & 3) if path else None


def emitter_rows(m, ev, em, start, end, nonloop):
    """one sequence [start, end] (ms) of emitter em -> (rate, f0, f1, px, py, pz, dx, dy, dz): docs/w3p_models.md
    MPSQ. Sampled every EMIT_SAMPLE_MS: visible = KP2V > 0 (no key in the sequence: the static 1), rate = KP2E (no
    key: the static rate). f0 / f1 = the first / last visible-and-emitting sample as a fraction of the duration (+ one
    step), rate = the mean rate over [f0, f1] [inf: a per-sequence constant]. Squirt emitters burst: rate = -(the
    biggest KP2E key value inside the sequence while visible), f0 = f1 = that key's fraction [ext: squirt emits the
    key's value at once]. Position / direction = the emitter node's world matrix x pivot / x (0, 0, 1) at the first
    emitting sample (else the start), WC3 model axes."""
    gs = m.global_seqs
    tr = em['tracks']
    dur = max(0.0, float(end - start))
    n = max(1, min(256, int(dur / EMIT_SAMPLE_MS) + 1))
    step = dur / n if n else 0.0
    ts = [start + step * k for k in range(n)] if dur > 0 else [start]

    def vis(t):
        return eval_vis(tr.get('KP2V'), t, start, end, gs) > 0.0

    rate = f0 = f1 = 0.0
    t_at = start
    if em['squirt']:
        ke = tr.get('KP2E')
        best = None
        if ke is not None:
            for fr, v in zip(ke.frames, ke.vals):
                if start <= fr <= end and v[0] > 0 and vis(fr) and (best is None or v[0] > best[1]):
                    best = (fr, v[0])
        if best is None and ke is None and em['rate'] > 0 and vis(start):
            best = (start, em['rate'])
        if best is not None:
            rate = -float(best[1])
            f0 = f1 = (best[0] - start) / dur if dur > 0 else 0.0
            t_at = best[0]
    else:
        on = []
        for k, t in enumerate(ts):
            r = mdx.eval_track(tr.get('KP2E'), t, start, end, gs, (em['rate'],))[0]
            on.append(r if (r > 0 and vis(t)) else 0.0)
        idx = [k for k, r in enumerate(on) if r > 0]
        if idx:
            a, b = idx[0], idx[-1]
            rate = sum(on[a:b + 1]) / float(b - a + 1)
            if dur > 0:
                f0 = (ts[a] - start) / dur
                f1 = min(1.0, (ts[b] - start + step) / dur)
            else:
                f0, f1 = 0.0, 1.0
            t_at = ts[a]
    M, piv = ev.emitter_matrix(em, t_at, start, end)
    x, y, z = piv
    px = M[0] * x + M[1] * y + M[2] * z + M[3]
    py = M[4] * x + M[5] * y + M[6] * z + M[7]
    pz = M[8] * x + M[9] * y + M[10] * z + M[11]
    dx, dy, dz = M[2], M[6], M[10]
    ln = math.sqrt(dx * dx + dy * dy + dz * dz) or 1.0
    return (rate, f0, f1, px, py, pz, dx / ln, dy / ln, dz / ln)


def eval_vis(tr, t, start, end, gs):
    return mdx.eval_track(tr, t, start, end, gs, (1.0,))[0]


def emitter_static(m, em):
    """the static MPEM fields of a PRE2 emitter (animated parameters: their first key [inf])."""
    tr = em['tracks']
    ht = em['head_tail']
    fl = (PF_HEAD if ht in (0, 2) else 0) | (PF_TAIL if ht in (1, 2) else 0) | (PF_SQUIRT if em['squirt'] else 0)
    for src, dst in NODE_PF:
        if em['flags'] & src:
            fl |= dst
    return {'name': em['name'], 'tex': _emit_tex_key(m, em['texture']), 'filter': em['filter'] if em['filter'] <= 4 else 0,
            'rows': max(1, em['rows']), 'columns': max(1, em['columns']), 'flags': fl,
            'speed': _first_key(tr.get('KP2S'), em['speed']), 'variation': _first_key(tr.get('KP2R'), em['variation']),
            'latitude': _first_key(tr.get('KP2L'), em['latitude']), 'gravity': _first_key(tr.get('KP2G'), em['gravity']),
            'lifespan': em['lifespan'], 'width': _first_key(tr.get('KP2W'), em['width']),
            'length': _first_key(tr.get('KP2N'), em['length']), 'tail_length': em['tail_length'], 'time': em['time'],
            'colors': em['colors'], 'alphas': [a / 255.0 for a in em['alphas']], 'scaling': em['scaling'],
            'intervals': em['intervals']}



# ---------------------------------------------------------------- BUGS SPELL-ANIM: animation names of abilities

ANIM_FAMILIES = ('spell', 'attack', 'stand')
_ANIM_WORD = re.compile(r'[a-z0-9]+')


def anim_words(name):
    """the words of a sequence name (lowercase [a-z0-9] runs): 'Spell Slam - 2' -> spell slam 2. W3ModelLoader.PoseTagSeq
    splits the same way."""
    return _ANIM_WORD.findall(name.lower())


def anim_required(entry, family):
    """the words a sequence needs for an Animnames entry ('spell,slam'): its tags, plus the family word (spell / attack)
    when the entry names none of spell / attack / stand ('slam' alone, Pulverize)."""
    tags = [t for t in entry.lower().split(',') if t]
    if not any(t in ANIM_FAMILIES for t in tags):
        tags.insert(0, family)
    return tags


def anim_pick(names, entry, family='spell'):
    """index into `names` (sequence names) of the sequence the original picks for an Animnames entry: the one whose name
    has all the tags (the fewest other words wins, then the first); with no such sequence the last tags are dropped one by
    one down to two (the family word and one tag). None: the unit plays its plain Spell / Attack."""
    req = anim_required(entry, family)
    words = [anim_words(n) for n in names]
    for n in range(len(req), 1, -1):
        sub = req[:n]
        best = None
        for i, w in enumerate(words):
            if all(t in w for t in sub) and (best is None or len(w) < len(words[best])):
                best = i
        if best is not None:
            return best
    return None


def anim_families(entry):
    """the pose families an entry is looked up for: Spell always, Attack too when the entry names no family (a tagged
    attack: Pulverize's 'slam')."""
    tags = [t for t in entry.lower().split(',') if t]
    return ('spell',) if any(t in ANIM_FAMILIES for t in tags) else ('spell', 'attack')


def anim_entries(entries):
    """entries worth a lookup: a plain 'spell' / 'attack' / 'stand' or an entry for a work loop is the default pose."""
    out = []
    for e in sorted(set(entries)):
        tags = [t for t in e.split(',') if t]
        if len(tags) >= 2 or (tags and tags[0] not in ANIM_FAMILIES):
            if 'work' not in tags:
                out.append(e)
    return out


def bake(data, animated, emit=False, anim=()):
    """MDX bytes -> dict (geosets, vertices, indices, sequences with their VAT frames, bounds; with emit the PRE2
    emitters: 'emit' static fields, per baked sequence s['emit'] rows, 'emit_bind' rows of a model without baked
    sequences). Raises on bad data."""
    m = mdx.parse(data)
    ev = mdx.Evaluator(m)
    seqs = m.sequences
    # the bind is the first plain Stand (BUGS UNIT-ANIM review: 'Stand Work' listed before 'Stand' must not become the bind)
    stand = next((i for i, s in enumerate(seqs) if _seq_priority(s['name']) == SEQ_PREFIXES.index('stand')), None)
    bind_si = stand if stand is not None else (0 if seqs else None)
    bind_t = seqs[bind_si]['start'] if bind_si is not None else 0
    chosen = []
    anim_idx = set()
    old_kept = []
    fx = False
    if animated and seqs:
        cand = [(i, s) for i, s in enumerate(seqs) if _seq_priority(s['name']) is not None]
        # bind Stand first, then one of each category in prefix order, then the rest in file order
        firsts, rest, seen = [], [], set()
        # (BUGS SPELL-ANIM: a plain 'Spell' / 'Attack' is the category's first even when 'Spell Slam' is listed before it)
        def _plain(c):
            nm = ' '.join(anim_words(c[1]['name']))
            return 0 if nm in ('attack', 'spell') else 1
        for i, s in sorted(cand, key=lambda c: (_seq_priority(c[1]['name']), _plain(c), c[0])):
            p = _seq_priority(s['name'])
            (rest if p in seen else firsts).append(i)
            seen.add(p)
        if bind_si in firsts:
            firsts.remove(bind_si)
            firsts.insert(0, bind_si)
        order = firsts + sorted(rest)
        if animated is True and not emit:
            # what the bake kept before UNIT-ANIM (visibility only): the legacy categories, 12 sequences in file order
            leg = ('stand', 'walk', 'attack', 'death', 'birth', 'spell', 'morph')
            lp = [next((k for k, pf in enumerate(leg) if sq['name'].lower().startswith(pf)), None) for sq in seqs]
            lf, lr, lseen = [], [], set()
            for i in sorted((i for i in range(len(seqs)) if lp[i] is not None), key=lambda i: (lp[i], i)):
                (lr if lp[i] in lseen else lf).append(i)
                lseen.add(lp[i])
            if bind_si in lf:
                lf.remove(bind_si)
                lf.insert(0, bind_si)
            tot0 = 0
            for i in lf + sorted(lr):
                ts0 = _frame_times(seqs[i], False, True)
                if len(old_kept) >= MAX_SEQS or tot0 + len(ts0) > MAX_MODEL_FRAMES:
                    continue
                tot0 += len(ts0)
                old_kept.append((i, ts0))
            # BUGS UNIT-ANIM: a unit / building plays its first Stand, Walk, Attack (+ the 'Attack - 2' variant), Death, Stand Work
            # and Spell only; the other variants (Stand - 2, Walk Gold, Attack Lumber ...) were baked for nothing and ate the
            # frame cap and the pack budget the action sequences need
            order = firsts + [i for i in sorted(rest) if _seq_priority(seqs[i]['name']) == SEQ_PREFIXES.index('attack')
                              and extra_attack(seqs[i])][:1]
            # BUGS SPELL-ANIM: the variants the unit's abilities name (Spell Slam, Spell Throw, Stand Channel, Attack Slam ...),
            # after everything above so the frame cap and the budget drop them first
            names = [sq['name'] for sq in seqs]
            for ent in anim_entries(anim):
                for fam in anim_families(ent):
                    k = anim_pick(names, ent, fam)
                    if k is not None and k not in order and k != bind_si:
                        order.append(k)
                        anim_idx.add(k)
        if animated == MISSILE_SEQS:
            dth = [i for i in firsts if _seq_priority(seqs[i]['name']) == SEQ_PREFIXES.index('death') and i != order[0]]
            order = order[:1] + dth[:1]
        total = 0
        fx = (bool(emit) or animated == MISSILE_SEQS) and sum(len(g['verts']) for g in m.geosets) <= EFFECT_MAX_VERTS
        cap = EFFECT_MODEL_FRAMES if fx else MAX_MODEL_FRAMES
        for i in order:
            ts = _frame_times(seqs[i], fx)
            if len(chosen) >= MAX_SEQS or total + len(ts) > cap:
                continue
            chosen.append((i, ts))
            total += len(ts)
    # visibility over the baked frames -> geosets kept
    frames = [(si, t) for si, ts in chosen for t in ts]
    bind = ev.pose(bind_si, bind_t, normals=True)
    poses = [ev.pose(si, t, normals=False, billboard=animated) for si, t in frames]
    vis_extra = []
    kept_ids = set(si for si, _ in chosen)
    for i, ts0 in old_kept:
        if i not in kept_ids:
            vis_extra += [(i, t) for t in ts0]
    if vis_extra:
        # BUGS UNIT-ANIM review: a geoset that only a sequence we no longer bake shows (the wisp's glow planes live in its
        # Stand variants) must stay in the model: the visibility of the sequences the old bake kept counts too
        vis_poses = poses + [ev.pose(si, t, normals=False, billboard=animated) for si, t in vis_extra]
    else:
        vis_poses = poses
    keep = []
    for gi, g in enumerate(m.geosets):
        if not g['faces'] or not g['verts']:
            continue
        vis = bind[gi][2] > 0.01 or any(p[gi][2] > 0.01 for p in vis_poses)
        if vis:
            keep.append(gi)

    def collapse(pos, alpha):
        if alpha > 0.01:
            return pos
        n = len(pos)
        c = (sum(p[0] for p in pos) / n, sum(p[1] for p in pos) / n, sum(p[2] for p in pos) / n)
        return [c] * n
    out_geos, verts, idx = [], [], []
    rad = 0.0
    zmin, zmax = float('inf'), float('-inf')

    def bounds(pos, alpha):
        nonlocal rad, zmin, zmax
        if alpha <= 0.01:
            return
        for x, y, z in pos:
            d = x * x + y * y + z * z
            if d > rad:
                rad = d
            if z < zmin:
                zmin = z
            if z > zmax:
                zmax = z
    team = False
    for gi in keep:
        g = m.geosets[gi]
        pos, nrm, alpha = bind[gi]
        bounds(pos, alpha)
        pos = collapse(pos, alpha)
        mat = m.materials[g['material']] if 0 <= g['material'] < len(m.materials) else None
        key, filt, flags, lalpha, under = _choose_layer(m, mat)
        team |= under or (key is not None and key[0] == 'repl' and key[1] == REPL_TEAM)
        if animated and ev.bb_geo[gi]:
            # billboard (docs/w3p_models.md GEO_BILLBOARD): VAT = the nodes' animated pivot, the MVTX normal slot = the
            # vertex offset from it; MVTX position stays the bind pose (static use, e.g. a building)
            flags |= GEO_BILLBOARD
            nrm = ev.bb_offsets(gi)
        out_geos.append({'first_vertex': len(verts), 'vertex_count': len(pos), 'first_index': len(idx),
                         'index_count': len(g['faces']) * 3, 'tex': key, 'filter': filt, 'flags': flags,
                         'alpha': lalpha})
        uv = g['uvs']
        for v in range(len(pos)):
            verts.append(pos[v] + nrm[v] + uv[v])
        for f in g['faces']:
            idx.extend(f)
    nverts = len(verts)
    out_seqs = []
    fi = 0
    for si, ts in chosen:
        s = seqs[si]
        vat = array.array('f')
        for _ in ts:
            pose = poses[fi]
            fi += 1
            for gi in keep:
                pos, _n, alpha = pose[gi]
                bounds(pos, alpha)
                for p in collapse(pos, alpha):
                    vat.extend(p)
        out_seqs.append({'name': s['name'], 'frames': len(ts), 'duration': float(s['end'] - s['start']),
                         'flags': s['flags'] & 1, 'rarity': s['rarity'], 'speed': s['speed'], 'vat': vat.tobytes(),
                         'prio': _seq_priority(s['name']), 'bind': si == bind_si,
                         'fx30': fx_seq(s, fx), 'anim': si in anim_idx})
    if zmin == float('inf'):
        zmin = zmax = 0.0
    emits, emit_bind = [], []
    if emit and m.emitters:
        ems = [e for e in m.emitters if e['lifespan'] > 0][:MAX_EMITTERS]
        emits = [emitter_static(m, e) for e in ems]
        for (si, _ts), osq in zip(chosen, out_seqs):
            sq = seqs[si]
            osq['emit'] = [emitter_rows(m, ev, e, sq['start'], sq['end'], bool(sq['flags'] & 1)) for e in ems]
        # the rows of a model left without sequences (none baked, or the budget dropped them all): its bind sequence
        st, en = ev.seq_range(bind_si)
        emit_bind = [emitter_rows(m, ev, e, st, en, False) for e in ems]
    return {'geos': out_geos, 'verts': verts, 'idx': idx, 'nverts': nverts, 'seqs': out_seqs,
            'radius': math.sqrt(rad), 'zmin': zmin, 'zmax': zmax, 'team': team, 'emit': emits, 'emit_bind': emit_bind,
            'summary': mdx.summary(m)}


def _bake_job(args):
    path, data, animated, emit, anim = args
    try:
        return path, (bake(data, animated, emit) if emit else (bake(data, animated, False, anim) if anim else bake(data, animated))), None
    except Exception as e:                  # broken / unsupported model -> skipped, logged
        return path, None, '%s: %s' % (type(e).__name__, e)


def _bake_key(r):
    return (r.path.lower(), bool(r.animated), bool(r.emit), tuple(sorted(r.anim)))


def _mp_context():
    """-> multiprocessing context for the bake pool, or None (bake serially).
    fork on macOS/Linux source runs; spawn on Windows and in the frozen companion build (BUGS L-SPAWN: the frozen
    gsvr-cli baked every model serially, 4-8x slower; its entry calls multiprocessing.freeze_support(), so a spawned
    worker re-enters gsvr-cli and unpickles _bake_job from the bundled tools dir). W3PACK_MP=serial|fork|spawn
    overrides (support / tests)."""
    import sys
    want = os.environ.get('W3PACK_MP', '').strip().lower()
    if want == 'serial':
        return None
    import multiprocessing as mp
    if want in ('fork', 'spawn'):
        return mp.get_context(want)
    if hasattr(os, 'fork') and not getattr(sys, 'frozen', False):
        return mp.get_context('fork')
    return mp.get_context('spawn')


def _bake_parallel(work, n, ctx, log):
    """-> list of _bake_job results in `work` order, or None when the pool broke (caller bakes serially).
    ProcessPoolExecutor, not mp.Pool: a worker that dies on start-up raises BrokenProcessPool instead of being
    respawned forever (a hung converter would block the companion's queue)."""
    from concurrent.futures import ProcessPoolExecutor
    order = sorted(range(len(work)), key=lambda k: -len(work[k][1]) * (8 if work[k][2] else 1))   # biggest first
    try:
        with ProcessPoolExecutor(max_workers=n, mp_context=ctx) as ex:
            res = list(ex.map(_bake_job, [work[k] for k in order], chunksize=1))
    except Exception as e:                   # BrokenProcessPool, OSError, pickling ...: never fail the pack for it
        log('  models: %s pool failed (%s: %s) - baking serially' % (ctx.get_start_method(), type(e).__name__, e))
        return None
    out = [None] * len(work)
    for k, rr in zip(order, res):
        out[k] = rr
    return out


BAKE_STATS = {'baked': 0, 'reused': 0, 'mode': 'serial', 'secs': 0.0}   # last bake_all (logs + tests)


def bake_jobs(cpus):
    """bake processes for `cpus` logical CPUs: a quarter, 1..4 (maintainer 2026-10-23: the PC still runs VRChat and
    SteamVR while the companion converts; the companion also starts the converter below normal priority).
    W3PACK_JOBS overrides."""
    try:
        want = int(os.environ.get('W3PACK_JOBS', '') or 0)
    except ValueError:
        want = 0
    if want > 0:
        return want
    return max(1, min(4, cpus // 4))


def bake_all(files, reqs, log, jobs=None, cache=None):
    """bake every request's model; `cache` (dict, per pack) keeps finished bakes so build()'s budget retry
    (effects=False) reuses them instead of baking every model a second time (BUGS L-REBAKE). Each caller gets its
    own copy (build() annotates geosets and fit_budget drops sequences in place)."""
    import pickle, time
    t0 = time.time()
    jobs_in = []
    for r in reqs:
        data = files.read(r.path, missing_ok=True)
        if data is not None:
            jobs_in.append((r, data))
    results = {}
    todo = []
    for k, (r, d) in enumerate(jobs_in):
        if cache is not None and _bake_key(r) in cache:
            results[k] = pickle.loads(cache[_bake_key(r)])
        else:
            todo.append(k)
    work = [(jobs_in[k][0].path, jobs_in[k][1], jobs_in[k][0].animated, jobs_in[k][0].emit,
             tuple(sorted(jobs_in[k][0].anim))) for k in todo]
    n = jobs or bake_jobs(os.cpu_count() or 1)
    ctx = _mp_context() if n > 1 and len(work) > 4 else None
    res = _bake_parallel(work, n, ctx, log) if ctx is not None else None
    mode = '%s x%d' % (ctx.get_start_method(), n) if res is not None else 'serial'
    if res is None:
        res = [_bake_job(w) for w in work]
    for k, rr in zip(todo, res):
        results[k] = rr
        if cache is not None:
            cache[_bake_key(jobs_in[k][0])] = pickle.dumps(rr, pickle.HIGHEST_PROTOCOL)
    BAKE_STATS.update(baked=len(work), reused=len(jobs_in) - len(work), secs=time.time() - t0, mode=mode)
    log('  models: baked %d, reused %d (%s) in %.1f s' % (BAKE_STATS['baked'], BAKE_STATS['reused'],
                                                         BAKE_STATS['mode'], BAKE_STATS['secs']))
    out = []
    for k, (r, _d) in enumerate(jobs_in):
        _p, baked, err = results[k]
        if err:
            log('  models: %s skipped (%s)' % (r.path, err))
            continue
        if not baked['geos'] and not baked.get('emit'):
            log('  models: %s skipped (no visible geometry, no particle emitters)' % r.path)
            continue
        out.append((r, baked))
    return out


# ---------------------------------------------------------------- budget

def model_bytes(baked):
    b = 0
    for _r, m in baked:
        b += m['nverts'] * 32 + len(m['idx']) * 4 + len(m['geos']) * 32 + len(m['seqs']) * 32
        b += sum(len(s['vat']) for s in m['seqs'])
    return b


DROP_ORDER = ('morph', 'birth', 'anim', 'spell', 'stand work', 'attack', 'death', 'walk')   # TD creeps need Walk + Death most; UNIT-ANIM: Attack / Death / Work / Spell outlast Birth


def fit_budget(baked, budget, log, extras_only=False):
    """drop sequences (rarest first; never a model's bind Stand) until the geometry + VAT fit `budget`."""
    total = model_bytes(baked)
    if total <= budget:
        return []
    # order: first the extra variants of a category ('Stand - 2', 'Attack - 2' ...), then whole categories in
    # DROP_ORDER (Morph, Spell, Birth, Attack, Death, Walk; the bind Stand never); rarest first in each group,
    # then the biggest
    cands = []
    for k, (r, m) in enumerate(baked):
        seen = set()
        for s in m['seqs']:
            if s.get('anim'):                          # BUGS SPELL-ANIM: an ability's variant goes after Morph / Birth, before the plain Spell
                if not extras_only:
                    cands.append((1 + DROP_ORDER.index('anim'), -s['rarity'], -len(s['vat']), k, s))
                continue
            first = s['prio'] not in seen
            seen.add(s['prio'])
            if (not s['bind'] and not s.get('fx30') and not (first and SEQ_PREFIXES[s['prio']] == 'walk')
                    and (not first or SEQ_PREFIXES[s['prio']] in DROP_ORDER)):   # BUGS FX-ANIM: an effect's Stand / Birth / Death is what the world plays; a first category outside DROP_ORDER (Stand) never drops
                tier = 1 + DROP_ORDER.index(SEQ_PREFIXES[s['prio']]) if first else 0
                if extras_only and tier:
                    continue
                cands.append((tier, -s['rarity'], -len(s['vat']), k, s))
    cands.sort(key=lambda c: c[:3])
    dropped = []
    for _t, _ra, _nb, k, s in cands:
        nb = len(s['vat'])
        if total <= budget:
            break
        baked[k][1]['seqs'].remove(s)
        total -= nb + 32
        dropped.append('%s:%s' % (os.path.basename(baked[k][0].path.replace('\\', '/')), s['name']))
    if dropped:
        log('  models: budget %.1f MB -> dropped %d sequences: %s%s' % (budget / 1048576.0, len(dropped),
                                                                      ', '.join(dropped[:20]),
                                                                      ' ...' if len(dropped) > 20 else ''))
    return dropped


def thin_frames(baked, budget, log):
    """last resort when only the bind sequences are left and the pack is still over budget (BUGS LTD-SIZE: Legion TD,
    516 models, 59 MB of VAT with nothing but each model's Stand): halve the frame count of the biggest sequence
    (every 2nd frame; a non-looping one keeps its last frame) until the geometry + VAT fit, down to one frame. The
    vertex animation is interpolated over `duration`, so fewer frames only coarsen the motion."""
    total = model_bytes(baked)
    if total <= budget:
        return 0
    n = 0
    while total > budget:
        best = None
        for _r, m in baked:
            stride = m['nverts'] * 12
            for sq in m['seqs']:
                if sq['frames'] > 1 and stride > 0 and (best is None or len(sq['vat']) > len(best[1]['vat'])):
                    best = (stride, sq)
        if best is None:
            break
        stride, sq = best
        fr = list(range(0, sq['frames'], 2))
        if sq['flags'] & 1 and fr[-1] != sq['frames'] - 1:
            fr.append(sq['frames'] - 1)
        if len(fr) >= sq['frames']:                       # two frames of a non-looping sequence: down to one
            fr = [0]
        old = len(sq['vat'])
        sq['vat'] = b''.join(sq['vat'][k * stride:(k + 1) * stride] for k in fr)
        sq['frames'] = len(fr)
        total -= old - len(sq['vat'])
        n += 1
    log('  models: still over budget with the bind sequences only -> %d sequences thinned to fewer frames' % n)
    return n


# ---------------------------------------------------------------- textures -> one atlas

def _pow2_floor(n):
    p = 1
    while p * 2 <= n:
        p *= 2
    return p


def load_textures(files, keys, overrides, log, blp_rgba, small=()):
    """keys: ordered list of texture keys -> list of (PIL RGBA image, replaceable id, wrap flags). Keys in `small`
    (textures only particle emitters use, BUGS P-PART) are scaled to <= PARTICLE_TEX px."""
    from PIL import Image
    out = []
    cache = {}
    bad = []

    def blp(path):
        if path not in cache:
            data = files.read(path, missing_ok=True)
            im = None
            if data is not None:
                try:
                    im = blp_rgba(data)
                except Exception as e:
                    bad.append('%s (%s)' % (path, e))
            else:
                bad.append(path)
            cache[path] = im
        return cache[path]
    for key in keys:
        kind, ref, wrap = key[0], key[1], key[2]
        im, rid = None, 0
        if kind == 'file':
            im = blp(ref)
            if im is None:
                im = Image.new('RGBA', (8, 8), (255, 0, 255, 255))
        else:
            rid = ref
            if rid == REPL_TEAM:
                im = Image.new('RGBA', (8, 8), (255, 255, 255, 255))
            elif rid == REPL_GLOW:
                g = blp('ReplaceableTextures\\TeamGlow\\TeamGlow00.blp')
                if g is None:
                    im = Image.new('RGBA', (8, 8), (255, 255, 255, 255))
                else:
                    lum, _g, _b, a = g.split()              # TeamGlow00 is red: intensity = red channel
                    mx = max(1, lum.getextrema()[1])
                    lum = Image.eval(lum, lambda v: min(255, v * 255 // mx))
                    im = Image.merge('RGBA', (lum, lum, lum, a))
            else:
                path = overrides.get(key[3]) if len(key) > 3 else None
                im = blp(path) if path else None
                if im is None:
                    im = Image.new('RGBA', (8, 8), (128, 128, 128, 255))
        w, h = im.size
        s = min(1.0, float(PARTICLE_TEX if key in small else MAX_TEX) / max(w, h))
        nw, nh = _pow2_floor(max(1, int(w * s))), _pow2_floor(max(1, int(h * s)))
        if (nw, nh) != (w, h):
            im = im.resize((nw, nh), Image.BOX if nw <= w and nh <= h else Image.BILINEAR)
        out.append((im, rid, wrap))
    if bad:
        log('  models: %d textures unreadable: %s' % (len(bad), ', '.join(bad[:10])))
    return out


def _padded(im, wrap):
    """image with a BORDER px edge-extended (or wrapped, per axis) frame."""
    from PIL import Image
    w, h = im.size
    B = BORDER
    out = Image.new('RGBA', (w + 2 * B, h + 2 * B))
    tiled = Image.new('RGBA', (3 * w, 3 * h))
    for i in range(3):
        for j in range(3):
            tiled.paste(im, (i * w, j * h))
    big = tiled.crop((w - B, h - B, 2 * w + B, 2 * h + B)) if B <= min(w, h) else None
    # clamp: replicate edges
    out.paste(im, (B, B))
    left = im.crop((0, 0, 1, h)).resize((B, h))
    right = im.crop((w - 1, 0, w, h)).resize((B, h))
    out.paste(left, (0, B))
    out.paste(right, (w + B, B))
    top = out.crop((0, B, w + 2 * B, B + 1)).resize((w + 2 * B, B))
    bot = out.crop((0, h + B - 1, w + 2 * B, h + B)).resize((w + 2 * B, B))
    out.paste(top, (0, 0))
    out.paste(bot, (0, h + B))
    if big is not None and wrap:
        if wrap & 1:            # wrap u: left / right borders from the opposite edge
            out.paste(big.crop((0, B, B, h + B)), (0, B))
            out.paste(big.crop((w + B, B, w + 2 * B, h + B)), (w + B, B))
        if wrap & 2:            # wrap v
            out.paste(big.crop((B, 0, w + B, B)), (B, 0))
            out.paste(big.crop((B, h + B, w + B, h + 2 * B)), (B, h + B))
        if wrap & 3 == 3:
            for cx, cy in ((0, 0), (w + B, 0), (0, h + B), (w + B, h + B)):
                out.paste(big.crop((cx, cy, cx + B, cy + B)), (cx, cy))
    return out


def _shelf(sizes):
    """sizes [(w, h)] incl. border, multiples of 8 -> (positions top-left, height) or None if > ATLAS_MAX_H."""
    order = sorted(range(len(sizes)), key=lambda k: (-sizes[k][1], -sizes[k][0]))
    pos = [None] * len(sizes)
    x = y = rowh = 0
    for k in order:
        w, h = sizes[k]
        if x + w > ATLAS_W:
            x, y, rowh = 0, y + rowh, 0
        pos[k] = (x, y)
        x += w
        rowh = max(rowh, h)
    height = y + rowh
    return pos, height


def build_atlas(texs, log):
    """texs [(im, rid, wrap)] -> (MATX bytes, TEXS rows [(x, y, w, h, rid, wrap)], w, h)."""
    from PIL import Image
    imgs = [t[0] for t in texs]
    shrunk = 0
    while True:
        sizes = [(-(-(im.size[0] + 2 * BORDER) // 8) * 8, -(-(im.size[1] + 2 * BORDER) // 8) * 8) for im in imgs]
        pos, height = _shelf(sizes)
        if height <= ATLAS_MAX_H:
            break
        big = max(max(im.size) for im in imgs)
        if big <= 16:
            raise ValueError('atlas overflow')
        # halve the biggest textures in batches of 8 (largest area first) until the shelf fits
        cand = sorted((k for k, im in enumerate(imgs) if max(im.size) >= big),
                      key=lambda k: -imgs[k].size[0] * imgs[k].size[1])[:8]
        for k in cand:
            im = imgs[k]
            imgs[k] = im.resize((max(1, im.size[0] // 2), max(1, im.size[1] // 2)), Image.BOX)
        shrunk += len(cand)
    if shrunk:
        log('  models: atlas overflow -> %d texture halvings to fit %d px' % (shrunk, ATLAS_MAX_H))
    H = max(256, -(-height // 256) * 256)
    page = Image.new('RGBA', (ATLAS_W, H), (0, 0, 0, 0))
    rows = []
    for im, p, t in zip(imgs, pos, texs):
        x, y = p
        page.paste(_padded(im, t[2]), (x, y))
        w, h = im.size
        rows.append((x + BORDER, H - (y + BORDER + h), w, h, t[1], t[2]))
    levels = []
    for lvl in range(ATLAS_MIPS):
        lv = page if lvl == 0 else page.resize((ATLAS_W >> lvl, H >> lvl), Image.BOX)
        lv = lv.transpose(Image.FLIP_TOP_BOTTOM)                # rows bottom-up like the RGBA path (v = 0 first)
        levels.append(dxt5(lv) if use_dxt5() else lv.tobytes())
    fmt = ATLAS_MIPS + (MATX_DXT5 if use_dxt5() else 0)
    return struct.pack('<3i', ATLAS_W, H, fmt) + b''.join(levels), rows, ATLAS_W, H


# ---------------------------------------------------------------- sections

HALVED = [False]                    # the last build() halved the textures (build's effects decision)


def build(files, doo, units_placed, log, blp_rgba, budget=BUDGET, jobs=None, effects=True, extra_effects=(),
          bakes=None):
    from PIL import Image
    if bakes is None:
        bakes = {}                  # finished bakes of this pack, shared with the budget retry (BUGS L-REBAKE)
    """-> (list of (tag, bytes, count), summary string)."""
    reqs, dtym, utym, neff, extra = collect(files, doo, units_placed, log, effects, extra_effects)
    emit_keys, geo_keys = [], set()
    baked = bake_all(files, reqs, log, jobs, bakes)
    # texture keys (tree / replaceable overrides resolved per request)
    keys, kix = [], {}
    overrides = {}
    for r, m in baked:
        for g in m['geos']:
            key = g['tex']
            if key is None:
                g['texi'] = -1
                continue
            if key[0] == 'repl' and key[1] not in (REPL_TEAM, REPL_GLOW):
                ov = r.tex_override[1] if r.tex_override and r.tex_override[0] == key[1] else None
                key = key + (ov.lower() if ov else None,)
                if ov:
                    overrides[ov.lower()] = ov
            elif key[0] == 'repl':
                key = ('repl', key[1], 0)          # one white cell per replaceable id
            geo_keys.add(key)
            if key not in kix:
                kix[key] = len(keys)
                keys.append(key)
            g['texi'] = kix[key]
        for e in m.get('emit', ()):            # MPEM: the emitter textures go into the atlas too
            key = e['tex']
            emit_keys.append(key)
            if key is None:
                e['texi'] = -1
                continue
            if key[0] == 'repl' and key[1] not in (REPL_TEAM, REPL_GLOW):
                key = key + (None,)
            emit_keys[-1] = key
            if key not in kix:
                kix[key] = len(keys)
                keys.append(key)
            e['texi'] = kix[key]
    # BUGS P-SELRING: the selection circle textures (UI\\MiscData.txt [SelectionCircle]) go into the model atlas
    import w3missileart
    selinfo = w3missileart.selection_info(files)
    seltex = []
    for k in range(3):
        tp = selinfo.get('Texture%02d' % k, '')
        key = ('file', tp + '.blp', 0) if tp else None
        if key is None or files.read(key[1], missing_ok=True) is None:
            seltex.append(-1)
            continue
        if key not in kix:
            kix[key] = len(keys)
            keys.append(key)
        seltex.append(kix[key])
    # USPL: the structures' ubersplat textures go into the atlas too (NOTES_units §4.2)
    spinfo = w3missileart.ubersplat_info(files) if extra.get('splat') else {}
    splat_rows = []
    for uid, nm in extra.get('splat', ()):
        si = spinfo.get(nm)
        if si is None:
            continue
        key = ('file', si[0], 0)
        if key not in kix:
            kix[key] = len(keys)
            keys.append(key)
        splat_rows.append((uid, kix[key], si[1], si[2], si[3]))
    # BUGS P-PART: the textures only particle emitters use (<= PARTICLE_TEX px) do not cost the units their animations:
    # the budget grows by their atlas share (padded area, RGBA, mips; at most PARTICLE_TEX_BUDGET) [inf: the pack grows
    # by ~1/4 of it, DXT5]
    only = set(k for k in emit_keys if k is not None) - geo_keys
    other = set(seltex) | set(r[1] for r in splat_rows)            # selection circles / ubersplats keep their size
    only = set(k for k in only if kix.get(k) not in other)
    texs = load_textures(files, keys, overrides, log, blp_rgba, only)
    matx, trows, aw, ah = build_atlas(texs, log) if texs else (struct.pack('<3i', 0, 0, 0), [], 0, 0)
    ptex = 0
    for key in only:
        if key in kix:
            w, h = texs[kix[key]][0].size
            ptex += (w + 2 * BORDER) * (h + 2 * BORDER) * 4 * 4 // 3
    budget0 = budget
    budget += min(ptex, PARTICLE_TEX_BUDGET)
    # BUGS FX-ANIM: the effect / missile models' 30 fps bake (about twice the 15 fps frames) must not cost the units
    # their Walk and must not be dropped by the budget pass: the budget grows by the extra half
    fxextra = sum(len(sq['vat']) for _r, m in baked for sq in m['seqs'] if sq.get('fx30')) // 2
    budget += fxextra
    log('  models: effect models baked at %d fps: extra VAT %.2f MB (in the budget)' % (EFFECT_FPS, fxextra / 1048576.0))
    # budget = geometry + VAT + atlas: drop extra sequence variants first; if still too big, textures > 128 px
    # are halved before whole categories (Attack, Death, Walk ...) go. The atlas counts at its RGBA size even as
    # DXT5: the 4x saving must shrink the pack (VRChat's 100 MB limit), not be refilled with more VAT frames.
    acct = lambda m: len(m) * (4 if use_dxt5() else 1)                       # noqa: E731
    fit_budget(baked, max(0, budget - acct(matx)), log, extras_only=True)
    if texs and model_bytes(baked) + acct(matx) > budget and neff:
        # the unit / doodad textures come first: without the effect models if that keeps them at full size
        # (td_plan T4); if the map halves them anyway, the effects stay
        res = build(files, doo, units_placed, log, blp_rgba, budget0, jobs, effects=False, bakes=bakes)
        if not HALVED[0]:
            log('  models: %d special-effect models left out (they would halve the textures)' % neff)
            return res
        log('  models: textures are halved anyway -> the %d special-effect models stay' % neff)
    HALVED[0] = False
    if texs and model_bytes(baked) + acct(matx) > budget:
        HALVED[0] = True
        big = [(im.resize((max(1, im.size[0] // 2), max(1, im.size[1] // 2)), Image.BOX) if max(im.size) > 128
                else im, rid, wrap) for im, rid, wrap in texs]
        matx2, trows2, aw2, ah2 = build_atlas(big, log)
        if len(matx2) < len(matx):
            log('  models: over budget -> textures > 128 px halved (atlas %dx%d -> %dx%d)' % (aw, ah, aw2, ah2))
            matx, trows, aw, ah = matx2, trows2, aw2, ah2
    fit_budget(baked, max(0, budget - acct(matx)), log)
    thin_frames(baked, max(0, budget - acct(matx)), log)

    mlst, mnam, geos, mvtx, midx, seqs, snam, mvat, mpem, mpsq = (bytearray() for _ in range(10))
    nemit = nsq_rows = 0
    nverts = nidx = ngeo = nseq = 0
    vat_floats = 0
    total_frames = 0
    names_seen = {}
    for k, (r, m) in enumerate(baked):
        r.index = k
        first_geo, first_seq = ngeo, nseq
        for g in m['geos']:
            geos += struct.pack('<7if', nverts + g['first_vertex'], g['vertex_count'], nidx + g['first_index'],
                                g['index_count'], g['texi'], g['filter'], g['flags'], g['alpha'])
            ngeo += 1
        for v in m['verts']:
            mvtx += struct.pack('<8f', *v)
        midx += struct.pack('<%di' % len(m['idx']), *m['idx'])
        frames = 0
        vat_off = -1
        if m['seqs']:
            vat_off = vat_floats
            for s in m['seqs']:
                seqs += struct.pack('<iiififfi', len(snam), frames, s['frames'], s['duration'], s['flags'],
                                    s['rarity'], s['speed'], 0)
                snam += s['name'].encode('latin1', 'replace') + b'\0'
                mvat += s['vat']
                frames += s['frames']
                nseq += 1
            vat_floats += frames * m['nverts'] * 3
        total_frames += frames
        # MPEM / MPSQ (w3p1-45): the model's particle emitters, one MPSQ row per emitter and packed sequence (SEQS
        # order of the model; a model without sequences: one row)
        for ei, e in enumerate(m.get('emit', ())):
            rows = [sq['emit'][ei] for sq in m['seqs']] if m['seqs'] else [m['emit_bind'][ei]]
            c = e['colors']
            mpem += struct.pack('<6i9f9f3f3f12i2i', k, e['texi'], e['filter'], e['rows'], e['columns'], e['flags'],
                                e['speed'], e['variation'], e['latitude'], e['gravity'], e['lifespan'], e['width'],
                                e['length'], e['tail_length'], e['time'], *(c[0] + c[1] + c[2]), *e['alphas'],
                                *e['scaling'], *e['intervals'], nsq_rows, len(rows))
            for row in rows:
                mpsq += struct.pack('<10f', *(tuple(row) + (0.0,)))
            nsq_rows += len(rows)
            nemit += 1
        mlst += struct.pack('<8i3fi', len(mnam), first_geo, len(m['geos']), first_seq, len(m['seqs']), m['nverts'],
                            vat_off, frames, m['radius'], m['zmin'], m['zmax'],
                            (1 if m['team'] else 0) | (MLST_EMITTERS if m.get('emit') else 0))
        mnam += r.path.encode('latin1', 'replace') + b'\0'
        nverts += m['nverts']
        nidx += len(m['idx'])
    assert len(mvat) == vat_floats * 4
    texs_b = bytearray()
    for row in trows:
        texs_b += struct.pack('<6i', *row)
    dt = bytearray()
    nd = 0
    for did, var, r in dtym:
        if r.index >= 0:
            dt += struct.pack('<4siif', did.encode('latin1')[:4].ljust(4, b'\0'), var, r.index, 1.0)
            nd += 1
    ut = bytearray()
    nu = 0
    for uid, r, sc, fl in utym:
        if r is None or r.index >= 0:
            ut += struct.pack('<4sifi', uid.encode('latin1')[:4].ljust(4, b'\0'), -1 if r is None else r.index, sc, fl)
            nu += 1
    # UMIS (BUGS P-MISSILE): per unit type + weapon the missile model (MLST index, -1 = none / no visible geometry:
    # glow quad, -2 = the map blanked the art: not drawn)
    mix = {}
    for r, _m in baked:
        mix.setdefault(r.path.lower(), r.index)
    um = bytearray()
    nm = 0
    cols = {}
    for uid in sorted(extra['missiles']):
        for wi, p, arc, tp, lz, iz in extra['missiles'][uid]:
            if p == '-':                       # the map blanked the art: the game draws no missile
                um += struct.pack('<4siifIffi', uid.encode('latin1')[:4].ljust(4, b'\0'), wi, -2, arc, 0, lz, iz, tp)
                nm += 1
                continue
            if p and p.lower() not in cols:
                cols[p.lower()] = w3missileart.art_colour(files, p, blp_rgba)
            um += struct.pack('<4siifIffi', uid.encode('latin1')[:4].ljust(4, b'\0'), wi, mix.get(p.lower(), -1) if p else -1,
                              arc, cols.get(p.lower(), 0) if p else 0, lz, iz, tp)
            nm += 1
    # USEL (BUGS P-SELRING): header (16 words) + per unit type id, selection scale, selection z
    us = struct.pack('<3f3i3If6i', float(selinfo.get('ScaleFactor') or 72), float(selinfo.get('Size00') or 100),
                     float(selinfo.get('Size01') or 300), seltex[0], seltex[1], seltex[2],
                     w3missileart.argb_to_rgba8(selinfo.get('ColorFriend', '')),
                     w3missileart.argb_to_rgba8(selinfo.get('ColorNeutral', '')),
                     w3missileart.argb_to_rgba8(selinfo.get('ColorEnemy', '')),
                     float(selinfo.get('AllyOffset') or 0), 0, 0, 0, 0, 0, 0)
    for uid, sc, sz in extra['sel']:
        us += struct.pack('<4sff', uid.encode('latin1')[:4].ljust(4, b'\0'), sc, sz)
    # USPL (w3p1-40, NOTES_units §4.2): per structure type the ubersplat: TEXS index, Scale, colour, blend mode
    sp = bytearray()
    for uid, ti, sc, col, bm in splat_rows:
        sp += struct.pack('<4sifIi', uid.encode('latin1')[:4].ljust(4, b'\0'), ti, sc, col, bm)
    # UHGT (w3p1-74, BUGS UNIT-HEIGHT): per non-walking unit type: char[4] id, i32 kind, f32 height
    uh = pack_uhgt(extra.get('fly', ()))
    secs = [(b'MLST', bytes(mlst), len(baked)), (b'MNAM', bytes(mnam), len(baked)), (b'GEOS', bytes(geos), ngeo),
            (b'MVTX', bytes(mvtx), nverts), (b'MIDX', bytes(midx), nidx), (b'SEQS', bytes(seqs), nseq),
            (b'SNAM', bytes(snam), nseq), (b'MVAT', bytes(mvat), vat_floats), (b'MATX', matx, ATLAS_MIPS if texs else 0),
            (b'TEXS', bytes(texs_b), len(trows)), (b'DTYM', bytes(dt), nd), (b'UTYM', bytes(ut), nu),
            (b'UMIS', bytes(um), nm), (b'USEL', us, len(extra['sel'])), (b'USPL', bytes(sp), len(splat_rows)),
            (b'MPEM', bytes(mpem), nemit), (b'MPSQ', bytes(mpsq), nsq_rows),
            (b'UHGT', bytes(uh), len(extra.get('fly', ())))]
    nbytes = sum(len(s[1]) for s in secs)
    anim = sum(1 for _r, m in baked if m['seqs'])
    summ = ('models %d (%d animated), geosets %d, verts %d, tris %d, seqs %d, frames %d, textures %d (atlas %dx%d), '
            'dtym %d, utym %d, umis %d, usel %d, emitters %d (%d models), %.1f MB (MVAT %.1f MB, MATX %.1f MB)'
            % (len(baked), anim, ngeo, nverts, nidx // 3, nseq, total_frames, len(trows), aw, ah, nd, nu, nm, len(extra['sel']),
               nemit, sum(1 for _r, m in baked if m.get('emit')),
               nbytes / 1048576.0, len(mvat) / 1048576.0, len(matx) / 1048576.0))
    return secs, summ
