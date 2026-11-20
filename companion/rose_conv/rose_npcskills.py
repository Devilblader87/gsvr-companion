"""ROSE w20 DUNGEON (companion-owned, like rose_skilltips.py: the vendored converter files stay byte-identical to rose-vrchat): the monsters' SKILL TABLE behind the AIPS records.

The AI scripts' "use skill" action (AIPS op 25: target, skill id, motion) only names a LIST_SKILL row. Monster skills are no player skills (no page / icon), so the SKDB section has
none of them, and the runtime used to turn every monster skill into a normal attack. This module appends the columns the runtime's skill damage needs (GsvrRoseAi.UseSkill /
SkillArea, docs/design/rose_w20_dungeon_audit.md) for every skill id the zone's programs use, to the AIPS section itself:

  AIPS header word 9 = skillCount ; word 10 = skillOff (4-byte aligned, after the records; both were reserved 0: a pack without the table keeps the old meaning)
  W21 DUNGEON2: header word 11 = record bytes (0 = the w20 record of 32 B; 64 = v2, written now)
  skill record 64 at skillOff + i * 64, sorted by id: u16 id ; u16 type (cell 5) ; u16 damageType (15) ; u16 power (9) ; u16 castRange cm (6) ; u16 scope cm (8) ;
      u16 hitCount (70) ; u16 successPct (13) ; u16 status[2] (11, 12) ; u16 durationS (14) ; u16 cooldown (20) ; u16 0 x 4 ;
      v2: u16 targetFilter (7) ; u16 castMotion (52) ; u16 castSpeed % (53) ; then the add-ability pairs u16 type (21 / 24), i16 value (22 / 25), i16 rate (23 / 26) ;
      u16 harm (10) ; u16 actionMotion (68) ; u16 actionSpeed % (69) ; u16 0 x 4
  (GsvrRoseAi.LinkSkills reads the v2 fields: monster buffs / debuffs through LIST_STATUS, the target filter, the cast time; docs/design/rose_w21_dungeon2.md)
  (column numbers = rosefmt.stb cell indices, rose-offline rose-data-irose skill_database.rs; the same cells rose_skills.skill_record reads for the player skills)

`install()` wraps rose_aips.build_section (the vendored rose_pack calls it through the module attribute). No number or text of the game is committed: everything is read from the
player's own client at conversion time.
"""
import struct

import rose_aips
import rose_chars

HDR_SK_COUNT, HDR_SK_OFF, HDR_SK_STRIDE = 9, 10, 11   # header word indices of rose_aips.HDR (reserved 0 before w20; word 11 = record bytes since w21)
SK = struct.Struct('<16H')
SK_BYTES = 32
SK2 = struct.Struct('<16H3H' + 'Hhh' * 2 + '3H4H')     # W21 v2 record: the w20 part + filter, cast motion / speed, add abilities, harm, action motion / speed
SK2_BYTES = 64
MAX_SKILLS = 1024                           # GsvrRoseAi.SkMax
OP_USE_SKILL = 25
assert SK.size == SK_BYTES
assert SK2.size == SK2_BYTES


def _int(c, i, default=0):
    try:
        v = str(c[i]).strip()
        return int(v) if v else default
    except (IndexError, ValueError):
        return default


def _u16(v):
    return max(0, min(65535, int(v)))


def _i16(v):
    return max(-32768, min(32767, int(v)))


SIGNED = (20, 21, 23, 24)                   # v2 field indices that are i16 (the add-ability values / rates)


def used_skill_ids(programs):
    """programs (rose_aips.decode form) -> sorted ids of the skills their "use skill" actions name"""
    out = set()
    for pr in programs:
        for evlist in pr['triggers']:
            for _conds, acts in evlist:
                for a in acts:
                    if a[0] == OP_USE_SKILL and 0 < int(a[4]) <= 65535:
                        out.add(int(a[4]))
    return sorted(out)


def skill_row(sid, c):
    """LIST_SKILL row cells -> the 32 fields of the v2 record (None for a row without a skill type)"""
    typ = _int(c, 5)
    if typ <= 0:
        return None
    return (sid, typ, _int(c, 15), _int(c, 9), _int(c, 6), _int(c, 8), _int(c, 70), _int(c, 13), _int(c, 11), _int(c, 12), _int(c, 14), _int(c, 20), 0, 0, 0, 0,
            _int(c, 7), _int(c, 52), _int(c, 53), _int(c, 21), _int(c, 22), _int(c, 23), _int(c, 24), _int(c, 25), _int(c, 26), _int(c, 10), _int(c, 68), _int(c, 69), 0, 0, 0, 0)


def _pack_row(r):
    r = tuple(r) + (0,) * (32 - len(r))
    return SK2.pack(*[(_i16(v) if i in SIGNED else _u16(v)) for i, v in enumerate(r[:32])])


def append_table(aips, rows):
    """aips = AIPS section bytes, rows = [16-tuples] -> AIPS bytes with the skill table (unchanged without rows); header words 9 / 10 are rewritten"""
    if len(aips) < rose_aips.HDR.size:
        return aips
    rows = sorted(rows, key=lambda r: r[0])[:MAX_SKILLS]
    if not rows:
        return aips
    h = list(rose_aips.HDR.unpack_from(aips, 0))
    out = bytearray(aips)
    out += b'\0' * (-len(out) % 4)
    off = len(out)
    for r in rows:
        out += _pack_row(r)
    h[HDR_SK_COUNT], h[HDR_SK_OFF], h[HDR_SK_STRIDE] = len(rows), off, SK2_BYTES
    rose_aips.HDR.pack_into(out, 0, *h)
    return bytes(out)


def table_stride(d):
    """record bytes of the table (32 for a w20 table, word 11 for v2), None for a bad value (same rule as GsvrRoseAi.SkStride)"""
    st = rose_aips.HDR.unpack_from(d, 0)[HDR_SK_STRIDE]
    if st == 0:
        return SK_BYTES
    return st if SK2_BYTES <= st <= 256 else None


def dec_table(d):
    """-> [16-tuples] (the w20 part) of the skill table; [] without one or with a bad one (same bounds as GsvrRoseAi.SkillCount)"""
    return [r[:16] for r in dec_table_v2(d)]


def dec_table_v2(d):
    """-> [32-tuples] of the skill table (the v2 fields 0 for a w20 table); [] without one or with a bad one"""
    if len(d) < rose_aips.HDR.size:
        return []
    h = rose_aips.HDR.unpack_from(d, 0)
    n, o, st = h[HDR_SK_COUNT], h[HDR_SK_OFF], table_stride(d)
    if st is None or n <= 0 or n > MAX_SKILLS or o < rose_aips.HDR.size or o + n * st > len(d):
        return []
    if st == SK_BYTES:
        return [SK.unpack_from(d, o + i * st) + (0,) * 16 for i in range(n)]
    return [SK2.unpack_from(d, o + i * st) for i in range(n)]


def build_with_skills(orig, vfs, side_actors, stb_reader):
    """the wrapper body: orig(...) -> (section or None, warnings, info); the AIPS section gets the skill table of the skills its programs use"""
    sec, warn, info = orig(vfs, side_actors, stb_reader)
    if sec is None:
        return sec, warn, info
    tag, ver, count, data = sec
    try:
        _npcs, programs = rose_aips.decode(data)
        sk = stb_reader(vfs.read('3DDATA/STB/LIST_SKILL.STB'))
    except (KeyError, rose_aips.AipsError, ValueError, struct.error, IndexError) as e:
        return sec, list(warn) + ['AIPS skills: %s' % e], info
    ids = used_skill_ids(programs)
    rows = []
    missing = 0
    for sid in ids:
        r = skill_row(sid, sk['cells'][sid]) if sid < sk['rows'] else None
        if r is None:
            missing += 1
        else:
            rows.append(r)
    if missing:
        warn = list(warn) + ['AIPS skills: %d skill ids of the scripts have no LIST_SKILL row (kept as normal attacks)' % missing]
    info = dict(info, skills=len(rows))
    return (tag, ver, count, append_table(data, rows)), warn, info


_orig = None

# W21 DUNGEON2 repair: every CHR motion slot of a monster goes into ACTR (the 12 slots = GsvrRpg1Chars.MotCount). The vendored rose_chars exports only 0..5
# (stop / move / attack / hit / die / run), so the cast motions the AI "use skill" actions name (6 / 8 cast, 7 / 9 skill action: ai_action_use_skill motion /
# motion + 1) had no clip, GsvrRoseAi.MotionMs read 0 and no monster ever had a cast time (rosedungeon r95: Goblin Cave B2's six skill programs all use motion 6 or 8,
# their actors' ACTR slots 6..11 were -1). rose_chars.add_actors reads the module global at call time. Upstream (rose-vrchat tools/rose_chars.py) should take the same value.
NPC_CAST_MOTION_IDS = tuple(range(12))


def install_cast_motions():
    """sets rose_chars.NPC_MOTION_IDS to all 12 CHR motion slots (idempotent; the loop set stays 0 / 1 / 5)"""
    rose_chars.NPC_MOTION_IDS = NPC_CAST_MOTION_IDS


def install():
    """wraps rose_aips.build_section once (idempotent); the vendored rose_pack calls it as rose_aips.build_section(vfs, side['actors'], F.stb)"""
    global _orig
    if _orig is not None:
        return
    install_cast_motions()
    _orig = rose_aips.build_section
    orig = _orig

    def build_section(vfs, side_actors, stb_reader):
        return build_with_skills(orig, vfs, side_actors, stb_reader)
    rose_aips.build_section = build_section

