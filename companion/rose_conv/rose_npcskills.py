"""ROSE w20 DUNGEON (companion-owned, like rose_skilltips.py: the vendored converter files stay byte-identical to rose-vrchat): the monsters' SKILL TABLE behind the AIPS records.

The AI scripts' "use skill" action (AIPS op 25: target, skill id, motion) only names a LIST_SKILL row. Monster skills are no player skills (no page / icon), so the SKDB section has
none of them, and the runtime used to turn every monster skill into a normal attack. This module appends the columns the runtime's skill damage needs (GsvrRoseAi.UseSkill /
SkillArea, docs/design/rose_w20_dungeon_audit.md) for every skill id the zone's programs use, to the AIPS section itself:

  AIPS header word 9 = skillCount ; word 10 = skillOff (4-byte aligned, after the records; both were reserved 0: a pack without the table keeps the old meaning)
  skill record 32 at skillOff + i * 32, sorted by id: u16 id ; u16 type (cell 5) ; u16 damageType (15) ; u16 power (9) ; u16 castRange cm (6) ; u16 scope cm (8) ;
      u16 hitCount (70) ; u16 successPct (13) ; u16 status[2] (11, 12) ; u16 durationS (14) ; u16 cooldown (20) ; u16 0 x 4
  (column numbers = rosefmt.stb cell indices, rose-offline rose-data-irose skill_database.rs; the same cells rose_skills.skill_record reads for the player skills)

`install()` wraps rose_aips.build_section (the vendored rose_pack calls it through the module attribute). No number or text of the game is committed: everything is read from the
player's own client at conversion time.
"""
import struct

import rose_aips

HDR_SK_COUNT, HDR_SK_OFF = 9, 10            # header word indices of rose_aips.HDR (reserved 0 before w20)
SK = struct.Struct('<16H')
SK_BYTES = 32
MAX_SKILLS = 1024                           # GsvrRoseAi.SkMax
OP_USE_SKILL = 25
assert SK.size == SK_BYTES


def _int(c, i, default=0):
    try:
        v = str(c[i]).strip()
        return int(v) if v else default
    except (IndexError, ValueError):
        return default


def _u16(v):
    return max(0, min(65535, int(v)))


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
    """LIST_SKILL row cells -> the 16 u16 of the record (None for a row without a skill type)"""
    typ = _int(c, 5)
    if typ <= 0:
        return None
    return (sid, typ, _int(c, 15), _int(c, 9), _int(c, 6), _int(c, 8), _int(c, 70), _int(c, 13), _int(c, 11), _int(c, 12), _int(c, 14), _int(c, 20), 0, 0, 0, 0)


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
        out += SK.pack(*[_u16(v) for v in r])
    h[HDR_SK_COUNT], h[HDR_SK_OFF] = len(rows), off
    rose_aips.HDR.pack_into(out, 0, *h)
    return bytes(out)


def dec_table(d):
    """-> [16-tuples] of the skill table; [] without one or with a bad one (same bounds as GsvrRoseAi.SkillCount)"""
    if len(d) < rose_aips.HDR.size:
        return []
    h = rose_aips.HDR.unpack_from(d, 0)
    n, o = h[HDR_SK_COUNT], h[HDR_SK_OFF]
    if n <= 0 or n > MAX_SKILLS or o < rose_aips.HDR.size or o + n * SK_BYTES > len(d):
        return []
    return [SK.unpack_from(d, o + i * SK_BYTES) for i in range(n)]


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


def install():
    """wraps rose_aips.build_section once (idempotent); the vendored rose_pack calls it as rose_aips.build_section(vfs, side['actors'], F.stb)"""
    global _orig
    if _orig is not None:
        return
    _orig = rose_aips.build_section
    orig = _orig

    def build_section(vfs, side_actors, stb_reader):
        return build_with_skills(orig, vfs, side_actors, stb_reader)
    rose_aips.build_section = build_section

