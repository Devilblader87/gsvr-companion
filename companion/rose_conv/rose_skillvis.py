"""ROSE w25 SKILL VISUALS (companion-owned, like rose_skilltips.py / rose_music.py: the vendored converter files stay byte-identical to rose-vrchat).
Owner MC 2026-11-22 "skills look different"; docs/design/rose_w25_skillvis.md.

1. SKILL MOTIONS (section SKMO, every pack with the avatar = the zones + the login pack, WRAPPER_REV 8):
   every player skill's casting / casting-repeat / action motion (LIST_SKILL cells 52 / 54 / 68 = TYPE_MOTION rows) is baked as an avatar clip into CANM, per gender and per
   weapon motion type (TYPE_MOTION column, LIST_WEAPON cell 34), exactly the file the original picks: rose-offline CharacterMotionDatabase.find_first_character_motion =
   TYPE_MOTION[motion][weapon type], 0 -> the no-weapon column, FILE_MOTION gender column, empty -> the male column (rose_chars.CharBuilder._motion_path does the same).
   Skills that ask for weapon classes (cells 30..34) get the columns of those classes only; skills without get every weapon motion type the avatar can hold.
   A weapon-type row whose file is the same as the no-weapon row is NOT written: the runtime falls back to weapon type 0 like the original.
   Casting-repeat motions are baked looping, cast / action motions once (the same file can be both: two clips).
   The runtime (GsvrRpg1Actors.SkillClip / PlaySkillMotion) plays them and takes the cast time from the cast clip's real length (GsvrRoseSkill.CastMsClip): no fixed number.

   SKMO layout (little endian): header 16 = u32 count ; u32 stride (8) ; u32 recOff (16) ; u32 0
       record 8, sorted by key = motion << 17 | loop << 16 | gender << 8 | weaponType:  u16 motion ; u8 gender ; u8 weaponType ; u16 clip (CANM) ; u8 loop ; u8 0

   Budget: the skill clips add about 1 MB CANM to every pack; their skin frames (about 5 MB of BONT) are pre-baked only while the pack stays at most SKILL_BONT_MAX bytes,
   else they are written un-baked (BONT flag 0) and the runtime bakes them on demand (they are "lazy": GsvrRpg1Chars.MarkSkillLazy = mark_skill_lazy below). Zone 62
   (Shady Jungle, ~93 MiB) takes the second path, so it stays far from the 100 MB limit and keeps its BONT.

2. AMMO PROJECTILES (EFCT, the avatar pack only, PACK_REV avatar 6): the original flies the bullet effect of the equipped AMMO (LIST_NATURAL cell 17 = LIST_EFFECT row) before
   the weapon's own (LIST_WEAPON cell 38) - rose-offline-client animation_effect_system EFFECT_WEAPON_FIRE_BULLET: arrows for bows / crossbows, bullets for guns, shells for
   launchers. The EFCT section gets an ammo table in the reserved header words 43 / 44 (old runtimes ignore them):
       header word 43 = ammoCount ; 44 = ammoOff ; ammo record 8 (sorted): u16 itemNumber (LIST_NATURAL row) ; u16 hit (file id) ; u16 bullet (file id) ; u16 bulletSpeed (cm / s)
   The ammo bullets go through the same effect builder as every other effect: one shared atlas / mesh / animation table (no extra atlas).

No number or text of the game is committed: everything is read from the player's own client at conversion time.
"""
import struct

import rosefmt as F
import rose_chars as RC
import rose_skills as SK
import rose_bont
import rose_effects as E

TAG_SKMO = b'SKMO'
HDR = struct.Struct('<4I')
REC = struct.Struct('<HBBHBB')
REC_BYTES = 8
MAX_ROWS = 8192
SKILL_BONT_MAX = 88 * 1024 * 1024        # the skill clips' frames go into BONT only while the pack with them stays at most this big (zone 62 does not: they stay lazy)
assert REC.size == REC_BYTES

NO_MOTION_TYPES = (1, 2, 15, 16)          # basic action, make window, passive, emote: the runtime never casts them (GsvrRoseSkill.FireKind FkBasic / window / passive / FkEmote)
KIND_CAST, KIND_REPEAT, KIND_ACTION = 0, 1, 2
KIND_LOOP = {KIND_CAST: False, KIND_REPEAT: True, KIND_ACTION: False}

AMMO_STRIDE = 8
AMMO = struct.Struct('<4H')
HDR_AMMO_COUNT, HDR_AMMO_OFF = 43, 44
NATURAL_BULLET_CELL = 17                  # LIST_NATURAL: bullet effect id (rose-offline item_database get_material_bullet_effect_id)


def _int(c, i, default=0):
    try:
        v = str(c[i]).strip()
    except IndexError:
        return default
    return int(v) if v.lstrip('-').isdigit() else default


def key(motion, loop, gender, wt):
    return (int(motion) << 17) | ((1 if loop else 0) << 16) | (int(gender) << 8) | int(wt)


# ------------------------------------------------------------------ which motions
def motion_needs(skill_stb, item_ids=(), names=None):
    """LIST_SKILL -> {(motion, loop): set(weapon motion types)} for every player skill row; the start skills (basic actions, emotes),
    the magic item skills (flag ITEM: the dance / emote scrolls, about 2,900 frames of long dance motions; they keep the old cast) and the types the runtime never casts (NO_MOTION_TYPES) are skipped (the runtime plays its own motions for them); a casting-repeat motion only when the row repeats it
    (cell 55 > 0: no row of the reference client does; names given (LIST_SKILL_S key -> name): rows without a name are skipped too; the cast fires at the end of the casting motion, so a repeat motion would never show). Weapon types: the motion types of the weapon classes the row asks for (cells 30..34), else all of rose_chars.WEAPON_MOTION_TYPES."""
    out = {}
    if skill_stb is None:
        return out
    for sid in range(1, min(skill_stb['rows'], 65536)):
        c = skill_stb['cells'][sid]
        if names is not None and not names.get(str(c[-1]).strip() if c else '', ''):
            continue          # a row without a name (LIST_SKILL_S.STL) is in no skill window: nobody can learn it (the unnamed dance rows 3171..)
        r = SK.skill_record(sid, c, 'x', None, sid in item_ids)
        if not r or (r['flags'] & (SK.FLAG_START | SK.FLAG_ITEM)) or r['type'] in NO_MOTION_TYPES:
            continue
        asked = [w for w in r['req_weapon'] if w]
        req = [RC.WEAPON_CLASS_MOTION[w] for w in asked if w in RC.WEAPON_CLASS_MOTION]
        if asked and not req:
            continue          # only weapon classes the avatar cannot hold (castle gear 512 ...): the use check never passes, no clip
        wts = set(req) if req else set(RC.WEAPON_MOTION_TYPES)
        wts.add(0)
        for m, loop in ((r['cast_motion'], False), (r['action_motion'], False)) + (((r['repeat_motion'], True),) if r['repeat_count'] > 0 else ()):
            if 0 < m <= 65535:
                out.setdefault((m, loop), set()).update(wts)
    return out


def _same(a, b):
    return a is not None and b is not None and RC._norm(a) == RC._norm(b)


def add_skill_clips(cb, genders, tm, fm, needs):
    """bakes the skill clips into the CharBuilder (after add_avatar: the avatar skeletons exist) -> SKMO rows [(motion, gender, wt, clip, loop)]"""
    rows = []
    if not cb.avatar:
        return rows
    for gi, g in enumerate(genders):
        if gi >= len(cb.avatar['genders']):
            break
        skel = cb.avatar['genders'][gi]['skeleton']
        for (m, loop), wts in sorted(needs.items()):
            base = cb._motion_path(tm, fm, m, 0, g)
            if base:
                c0 = cb.clip(base, skel, loop)
                if 0 <= c0 <= 65535:
                    rows.append((m, g, 0, c0, loop))
            for wt in sorted(wts):
                if wt == 0:
                    continue
                p = cb._motion_path(tm, fm, m, wt, g)
                if not p or _same(p, base):
                    continue
                c = cb.clip(p, skel, loop)
                if 0 <= c <= 65535:
                    rows.append((m, g, wt, c, loop))
    return rows


def enc_skmo(rows):
    rows = sorted(set((int(m), int(g), int(w), int(c), bool(lp)) for m, g, w, c, lp in rows), key=lambda r: key(r[0], r[4], r[1], r[2]))
    if len(rows) > MAX_ROWS:
        raise ValueError('SKMO: too many rows')
    keys = [key(r[0], r[4], r[1], r[2]) for r in rows]
    if len(set(keys)) != len(keys):
        raise ValueError('SKMO: two clips for one (motion, loop, gender, weapon type)')
    body = b''.join(REC.pack(m, g, w, c, 1 if lp else 0, 0) for m, g, w, c, lp in rows)
    return HDR.pack(len(rows), REC_BYTES, HDR.size, 0) + body


def dec_skmo(d, clip_count=None):
    """-> [(motion, gender, wt, clip, loop)]; ValueError on a bad layout (same rules as GsvrRpg1Chars.SkmoValid)"""
    if len(d) < HDR.size:
        raise ValueError('SKMO shorter than its header')
    n, stride, ro, _z = HDR.unpack_from(d, 0)
    if n > MAX_ROWS or stride < REC_BYTES or ro < HDR.size or ro + n * stride > len(d):
        raise ValueError('bad SKMO header')
    out = []
    prev = -1
    for i in range(n):
        m, g, w, c, lp, _ = REC.unpack_from(d, ro + i * stride)
        k = key(m, lp, g, w)
        if k <= prev or lp > 1:
            raise ValueError('SKMO rows not sorted / bad loop flag')
        if clip_count is not None and c >= clip_count:
            raise ValueError('SKMO clip outside CANM')
        prev = k
        out.append((m, g, w, c, bool(lp)))
    return out


def skmo_clip(rows, motion, loop, gender, wt):
    """runtime lookup (GsvrRpg1Chars.SkmoClip): the weapon type's row, else the no-weapon row (find_first_character_motion), else -1"""
    idx = {key(m, lp, g, w): c for m, g, w, c, lp in rows}
    c = idx.get(key(motion, loop, gender, wt), -1)
    if c < 0 and wt != 0:
        c = idx.get(key(motion, loop, gender, 0), -1)
    return c


def skill_only_clips(skmo_rows, avat, actr, clip_count):
    """clips only SKMO uses (no AVAT clip map row of any weapon type, no ACTR actor): GsvrRpg1Chars.MarkSkillLazy marks exactly these lazy"""
    used = set()
    if avat is not None and len(avat) >= 40:
        n, mo = rose_bont._i32(avat, 12), rose_bont._i32(avat, 28)
        for i in range(n):
            used.add(rose_bont._u16(avat, mo + i * 8 + 4))
    if actr is not None and len(actr) >= 12:
        na, stride, ro = rose_bont._i32(actr, 0), rose_bont._i32(actr, 4), rose_bont._i32(actr, 8)
        for a in range(na):
            for m in range(rose_bont.MOT_COUNT):
                c = rose_bont._i16(actr, ro + a * stride + rose_bont.AR_CLIP + m * 2)
                if c >= 0:
                    used.add(c)
    return sorted(set(c for _m, _g, _w, c, _l in skmo_rows if 0 <= c < clip_count) - used)


def mark_skill_lazy(lazy, skmo_rows, avat, actr):
    """lazy (rose_bont.mark_lazy result) + the skill-only clips -> the same list GsvrRpg1Chars.MarkLazyClips + MarkSkillLazy give"""
    out = list(lazy)
    for c in skill_only_clips(skmo_rows, avat, actr, len(out)):
        out[c] = True
    return out


def bont_bytes_estimate(skel, canm, baked):
    """bytes of the BONT section rose_bont.bont_section writes for the baked flags (without baking)"""
    skels = rose_bont._skeletons(skel)
    clips = rose_bont._clips(canm)
    n = len(clips)
    tex = 2 * sum(len(s) for s in skels)
    for c, cl in enumerate(clips):
        if baked[c]:
            tex += cl[1] * 2 * len(skels[cl[0]])
    return rose_bont.HEADER.size + 4 * n + ((n + 3) & ~3) + 16 * tex


# ------------------------------------------------------------------ hooks
_state = dict(skmo=None, skip=False)
_orig = {}


def _patched_mark_lazy(avat, actr, clip_count):
    lazy = _orig['mark_lazy'](avat, actr, clip_count)
    rows = _state['skmo']
    if not rows:
        return lazy
    skill = skill_only_clips(rows, avat, actr, clip_count)
    if _state['skip']:
        out = [False] * clip_count          # lazy_too=False then bakes every weapon-class set and leaves only the skill clips out
        for c in skill:
            out[c] = True
        return out
    out = list(lazy)
    for c in skill:
        out[c] = True
    return out


def _patched_from_sections(sections, max_pack=rose_bont.MAX_PACK_BYTES, lazy_too=True):
    by = {t: d for t, _v, _c, d in sections}
    sk = by.get(TAG_SKMO)
    if not sk:
        return _orig['from_sections'](sections, max_pack, lazy_too)
    rows = dec_skmo(sk)
    _state['skmo'] = rows
    try:
        skel, canm, avat, actr = by.get(b'SKEL'), by.get(b'CANM'), by.get(b'AVAT'), by.get(b'ACTR')
        if not skel or not canm:
            return _orig['from_sections'](sections, max_pack, lazy_too)
        n = len(rose_bont._clips(canm))
        rest = 32 + sum((len(d) + 3) & ~3 for _t, _v, _c, d in sections) + 24 * (len(sections) + 1)
        full = rest + bont_bytes_estimate(skel, canm, [True] * n)
        _state['skip'] = full > min(max_pack, SKILL_BONT_MAX)
        if _state['skip']:
            return _orig['from_sections'](sections, max_pack, False)
        return _orig['from_sections'](sections, max_pack, lazy_too)
    finally:
        _state['skmo'] = None
        _state['skip'] = False


def _tm_fm(vfs):
    try:
        return F.stb(vfs.read('3DDATA/STB/TYPE_MOTION.STB')), F.stb(vfs.read('3DDATA/STB/FILE_MOTION.STB'))
    except KeyError:
        return None, None


def _skill_tables(vfs):
    try:
        sk = F.stb(vfs.read('3DDATA/STB/LIST_SKILL.STB'))
    except KeyError:
        return None, set()
    try:
        items = SK.item_skill_ids(F.stb(vfs.read('3DDATA/STB/LIST_USEITEM.STB')))
    except KeyError:
        items = set()
    return sk, items


def _wrap_add_avatar(orig):
    def add_avatar(self, genders=(0, 1), *a, **kw):
        res = orig(self, genders, *a, **kw)
        self.skill_motions = []
        try:
            tm, fm = _tm_fm(self.vfs)
            sk, items = _skill_tables(self.vfs)
            if tm is not None and sk is not None:
                self.skill_motions = add_skill_clips(self, genders, tm, fm, motion_needs(sk, items, SK._names(self.vfs) or None))
        except (KeyError, ValueError, IndexError, struct.error) as e:          # never fails the conversion: a pack without SKMO plays the old fixed cast
            self.warn['skill motions: %s' % e.__class__.__name__] += 1
            self.skill_motions = []
        return res
    add_avatar.__doc__ = orig.__doc__
    return add_avatar


def _wrap_sections(orig):
    def sections(self):
        out = orig(self)
        rows = getattr(self, 'skill_motions', None)
        if rows and self.avatar:
            out.append((TAG_SKMO, 1, len(rows), enc_skmo(rows)))
        return out
    sections.__doc__ = orig.__doc__
    return sections


# ---- ammo projectiles (EFCT header words 43 / 44)
def ammo_rows(b, fe, list_stb, natural_stb):
    """LIST_NATURAL rows with a bullet effect -> [dict(id, hit, bullet, speed)] (the files are added to the effect builder b: the shared atlas / mesh tables)"""
    out = []
    if natural_stb is None:
        return out
    for n in range(1, min(natural_stb['rows'], 65536)):
        eid = _int(natural_stb['cells'][n], NATURAL_BULLET_CELL)
        if eid <= 0:
            continue
        hn, _hc, bul, speed = E.effect_parts(list_stb, eid)
        bullet = E.resolve_file(b, fe, bul)
        hit = E.resolve_file(b, fe, hn)
        if bullet or hit:
            out.append(dict(id=n, hit=hit, bullet=bullet, speed=E._u16(speed)))
    return out


def _wrap_build_tables(orig):
    def build_tables(read, file_stb, list_stb, skill_stb, *a, **kw):
        skills, npcs, weapons, b = orig(read, file_stb, list_stb, skill_stb, *a, **kw)
        b.ammo = []
        try:
            nat = F.stb(read('3DDATA/STB/LIST_NATURAL.STB'))
        except KeyError:
            nat = None
        if nat is not None:
            fe = {}
            for r in range(file_stb['rows']):
                c = file_stb['cells'][r]
                if len(c) > 1 and str(c[1]).strip():
                    fe[r] = E._norm(str(c[1]))
            b.ammo = ammo_rows(b, fe, list_stb, nat)
        return skills, npcs, weapons, b
    build_tables.__doc__ = orig.__doc__
    return build_tables


def append_ammo(data, ammo):
    """an encoded EFCT section + the ammo table at its end (4-byte aligned), header words 43 / 44 set"""
    if not ammo:
        return data
    rows = sorted(ammo, key=lambda r: r['id'])
    if [r['id'] for r in rows] != sorted(set(r['id'] for r in rows)):
        raise ValueError('EFCT ammo ids not unique')
    pad = bytes((-len(data)) % 4)
    off = len(data) + len(pad)
    body = b''.join(AMMO.pack(E._u16(r['id']), E._u16(r['hit']), E._u16(r['bullet']), E._u16(r['speed'])) for r in rows)
    out = bytearray(data + pad + body)
    struct.pack_into('<I', out, 4 * HDR_AMMO_COUNT, len(rows))
    struct.pack_into('<I', out, 4 * HDR_AMMO_OFF, off)
    return bytes(out)


def dec_ammo(d):
    """-> [(id, hit, bullet, speed)] of an EFCT section ([] without the table); ValueError on a bad table (GsvrRoseFx.AmmoValid)"""
    n, o = struct.unpack_from('<I', d, 4 * HDR_AMMO_COUNT)[0], struct.unpack_from('<I', d, 4 * HDR_AMMO_OFF)[0]
    if n == 0:
        return []
    if n > 65535 or o < E.HDR.size or o % 4 or o + n * AMMO_STRIDE > len(d):
        raise ValueError('bad EFCT ammo table')
    rows = [AMMO.unpack_from(d, o + i * AMMO_STRIDE) for i in range(n)]
    ids = [r[0] for r in rows]
    if ids != sorted(set(ids)):
        raise ValueError('EFCT ammo table not sorted')
    return rows


def _wrap_enc_efct(orig):
    def enc_efct(b, skills, npcs=(), weapons=()):
        return append_ammo(orig(b, skills, npcs, weapons), getattr(b, 'ammo', None))
    enc_efct.__doc__ = orig.__doc__
    return enc_efct


def install():
    """wraps CharBuilder.add_avatar / sections, rose_bont.mark_lazy / from_sections and rose_effects.build_tables / enc_efct once (idempotent)"""
    if _orig:
        return
    _orig['add_avatar'] = RC.CharBuilder.add_avatar
    _orig['sections'] = RC.CharBuilder.sections
    _orig['mark_lazy'] = rose_bont.mark_lazy
    _orig['from_sections'] = rose_bont.from_sections
    _orig['build_tables'] = E.build_tables
    _orig['enc_efct'] = E.enc_efct
    RC.CharBuilder.add_avatar = _wrap_add_avatar(_orig['add_avatar'])
    RC.CharBuilder.sections = _wrap_sections(_orig['sections'])
    rose_bont.mark_lazy = _patched_mark_lazy
    rose_bont.from_sections = _patched_from_sections
    E.build_tables = _wrap_build_tables(_orig['build_tables'])
    E.enc_efct = _wrap_enc_efct(_orig['enc_efct'])
