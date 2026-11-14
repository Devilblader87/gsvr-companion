"""Skill database for the Unity runtime (GoldSrcVR GsvrRoseSkill, lane SKILLS): section SKDB, optional and additive (a pack without it keeps the old 4-slot starter bar).

The runtime runs the original skill system from this one section: the skill window (pages basic / active / passive), learning and levelling with skill points, the quickbar,
use checks (cooldown and cooldown groups, MP / HP / money cost, required weapon, target filter, range), skill damage by damage type, heals, and the LIST_STATUS buffs / debuffs
with their durations. The older SKIL section (rose_gameplay.py) stays as it is; SKDB carries every player skill row with all the columns the rules need.

Source tables (read only, from the player's own client; column numbers = rosefmt.stb cell indices, the numbers of ROSE_SPEC section 14.1 and of rose-offline skill_database.rs):
  LIST_SKILL.STB + LIST_SKILL_S.STL (names, language 1), LIST_STATUS.STB, LIST_CLASS.STB (job class -> job ids, as in rose_items.py).

Layout (little endian, 4-byte aligned):
  header 48: u32 count ; u32 stride (136) ; u32 recOff (48) ; u32 namesOff ; u32 namesBytes ; u32 statusCount ; u32 statusStride (32) ; u32 statusOff ;
             u32 jobCount ; u32 jobOff (job name table, 0 / 0 = none: packs before the job change lane) ; u32 0 x 2
  skill record 136 at recOff + i * stride, sorted by id (binary search):
      0 u16 id ; 2 u16 baseId (cell 1) ; 4 u16 level (2) ; 6 u16 learnPoints (3) ; 8 u16 page (4: 0 basic 1 active 2 passive 3 clan) ; 10 u16 type (5) ;
     12 u16 castRange (6, cm; 0 = weapon range; basic actions: the basic command number) ; 14 u16 targetFilter (7) ; 16 u16 scope (8, cm radius) ; 18 u16 power (9) ;
     20 u16 harm (10) ; 22 u16 successPct (13) ; 24 u16 status[2] (11, 12: LIST_STATUS rows) ; 28 u16 durationS (14, seconds) ; 30 u16 damageType (15) ;
     32 u16 useType[2] (16, 18) ; 36 i32 useValue[2] (17, 19)      (the cost: ability 17 mana, 16 hp, 40 money ...)
     44 u16 cooldown (20, units of 200 ms) ; 46 u16 cooldownGroup (27) ; 48 u16 addType[2] (21, 24) ; 52 i32 addValue[2] (22, 25) ; 60 i16 addRate[2] (23, 26)
     64 u32 jobMask (35 resolved through LIST_CLASS to bits of rose_items.JOBS; 0 = no job requirement) ; 68 u16 jobClass (35 raw) ; 70 u16 actionMode (29)
     72 u16 reqSkill[3] (39, 41, 43) ; 78 u16 reqSkillLevel[3] (40, 42, 44) ; 84 u16 reqAbilityType[2] (45, 47) ; 88 i32 reqAbilityValue[2] (46, 48)
     96 u16 icon (51, SKILLICON sprite) ; 98 u16 castMotion (52) ; 100 u16 castMotionSpeed (53) ; 102 u16 repeatMotion (54) ; 104 u16 repeatCount (55) ;
    106 u16 actionMotion (68) ; 108 u16 actionMotionSpeed (69) ; 110 u16 hitCount (70) ; 112 u16 reqWeaponClass[5] (30..34) ; 122 u16 flags (bit 0 = start skill) ;
    124 u32 nameOff ; 128 u16 nameLen (UTF-8 in the names blob) ; 130 u16 0 ; 132 u32 0
  status record 32 at statusOff + i * statusStride, sorted by id:
      0 u16 id ; 2 u16 type (cell 1) ; 4 u16 reapply (2) ; 6 u16 clearedBy (3) ; 8 u16 icon (9, STATEICON sprite) ; 10 u16 applyId[2] (5, 7) ; 14 u16 0 ;
     16 i32 applyValue[2] (6, 8: value 0 = the per second value of hp / mp / poison effects) ; 24 u16 stepEffect (10) ; 26 u16 endEffect (15) ; 28 u32 0
  job name record 8 at jobOff + i * 8 (lane JOB CHANGE, GsvrRoseJob.JobName): u16 job id (the STR_JOB.STL key) ; u16 nameLen ; u32 nameOff (UTF-8 in the names blob)
      source: 3DDATA/STB/STR_JOB.STL, language 1 (English), keys that are job ids (rose-offline string_database get_job_name); at most 64 rows, names cut at 64 bytes

Rows: every LIST_SKILL row with a skill type, an icon and a page (cell 4) = the player skills (monster skills have no page). A start skill (flag bit 0) is a row of the basic
page without job class and without learn points at level 0 that has a name: the basic actions, kisses and emotes every new character knows (the reference server gives a fixed
list of them; this is the data rule for that list). No text or number of the game is committed; everything is read at conversion time on the player's machine.
"""
import struct

import rosefmt as F
import rose_items

TAG_SKDB = b'SKDB'
HDR = struct.Struct('<12I')
REC = struct.Struct('<HHHHHH HHHH HH HH HH HH ii HHHH ii hh IHH HHH HHH HH ii HHHHHHHH 5H H IHHI')
REC_BYTES = 136
ST = struct.Struct('<HHHHH HHH ii HHI')
ST_BYTES = 32
MAX_SKILLS = 8192
MAX_STATUS = 1024
JOB = struct.Struct('<HHI')
MAX_JOBS = 64
MAX_JOB_NAME = 64
assert REC.size == REC_BYTES, REC.size
assert ST.size == ST_BYTES, ST.size
START_TYPES = (1, 11, 16)       # basic action, target bound (the kisses), emote


def _int(c, i, default=0):
    try:
        v = c[i]
    except IndexError:
        return default
    v = str(v).strip()
    if v.lstrip('-').isdigit():
        return int(v)
    return default


def _u16(v):
    return max(0, min(65535, v))


def _i16(v):
    return max(-32768, min(32767, v))


def _i32(v):
    return max(-2 ** 31, min(2 ** 31 - 1, v))


def skill_record(sid, c, name, class_stb=None):
    """one LIST_SKILL row -> record dict (None for rows that are no player skill: no type, no icon or no page)"""
    typ, icon = _int(c, 5), _int(c, 51)
    page_cell = str(c[4]).strip() if len(c) > 4 else ''
    if typ <= 0 or icon <= 0 or not page_cell.isdigit():
        return None
    job_class = _int(c, 35)
    r = dict(id=sid, base=_int(c, 1, sid) or sid, level=_int(c, 2), learn_pts=_int(c, 3), page=int(page_cell), type=typ, cast_range=_int(c, 6), target_filter=_int(c, 7),
             scope=_int(c, 8), power=_int(c, 9), harm=_int(c, 10), success_pct=_int(c, 13), status=[_int(c, 11), _int(c, 12)], duration_s=_int(c, 14), damage_type=_int(c, 15),
             use_type=[_int(c, 16), _int(c, 18)], use_value=[_int(c, 17), _int(c, 19)], cooldown=_int(c, 20), cooldown_group=_int(c, 27),
             add_type=[_int(c, 21), _int(c, 24)], add_value=[_int(c, 22), _int(c, 25)], add_rate=[_int(c, 23), _int(c, 26)],
             job_class=job_class, job_mask=rose_items.job_mask(class_stb, job_class), action_mode=_int(c, 29),
             req_skill=[_int(c, 39), _int(c, 41), _int(c, 43)], req_skill_level=[_int(c, 40), _int(c, 42), _int(c, 44)],
             req_type=[_int(c, 45), _int(c, 47)], req_value=[_int(c, 46), _int(c, 48)],
             icon=icon, cast_motion=_int(c, 52), cast_speed=_int(c, 53), repeat_motion=_int(c, 54), repeat_count=_int(c, 55),
             action_motion=_int(c, 68), action_speed=_int(c, 69), hit_count=_int(c, 70), req_weapon=[_int(c, 30 + k) for k in range(5)], name=name or '')
    r['flags'] = 1 if (r['page'] == 0 and job_class == 0 and r['learn_pts'] == 0 and r['level'] == 0 and typ in START_TYPES and r['name']) else 0
    return r


def status_record(sid, c):
    """one LIST_STATUS row -> record dict (None without a type)"""
    typ = _int(c, 1)
    if typ <= 0:
        return None
    return dict(id=sid, type=typ, reapply=1 if _int(c, 2) else 0, cleared_by=_int(c, 3), icon=_int(c, 9), apply_id=[_int(c, 5), _int(c, 7)],
                apply_value=[_int(c, 6), _int(c, 8)], step_effect=_int(c, 10), end_effect=_int(c, 15))


def enc_skdb(skills, status=(), jobs=()):
    """jobs: [(job id, name)] for the job name table (lane JOB CHANGE); empty = no table (header words 8 / 9 stay 0)."""
    skills = sorted(skills, key=lambda r: r['id'])
    jobs = sorted((int(j), n) for j, n in jobs if 0 <= int(j) <= 65535)[:MAX_JOBS]
    status = sorted(status, key=lambda r: r['id'])
    if len(skills) > MAX_SKILLS or len(status) > MAX_STATUS:
        raise ValueError('SKDB: too many records')
    names = bytearray()
    body = bytearray()
    for r in skills:
        nb = r['name'].encode('utf-8')[:255]
        off = len(names)
        names += nb
        body += REC.pack(_u16(r['id']), _u16(r['base']), _u16(r['level']), _u16(r['learn_pts']), _u16(r['page']), _u16(r['type']),
                         _u16(r['cast_range']), _u16(r['target_filter']), _u16(r['scope']), _u16(r['power']), _u16(r['harm']), _u16(r['success_pct']),
                         _u16(r['status'][0]), _u16(r['status'][1]), _u16(r['duration_s']), _u16(r['damage_type']),
                         _u16(r['use_type'][0]), _u16(r['use_type'][1]), _i32(r['use_value'][0]), _i32(r['use_value'][1]),
                         _u16(r['cooldown']), _u16(r['cooldown_group']), _u16(r['add_type'][0]), _u16(r['add_type'][1]),
                         _i32(r['add_value'][0]), _i32(r['add_value'][1]), _i16(r['add_rate'][0]), _i16(r['add_rate'][1]),
                         r['job_mask'] & 0xFFFFFFFF, _u16(r['job_class']), _u16(r['action_mode']),
                         *[_u16(x) for x in r['req_skill']], *[_u16(x) for x in r['req_skill_level']], _u16(r['req_type'][0]), _u16(r['req_type'][1]),
                         _i32(r['req_value'][0]), _i32(r['req_value'][1]),
                         _u16(r['icon']), _u16(r['cast_motion']), _u16(r['cast_speed']), _u16(r['repeat_motion']), _u16(r['repeat_count']),
                         _u16(r['action_motion']), _u16(r['action_speed']), _u16(r['hit_count']), *[_u16(x) for x in r['req_weapon']], _u16(r.get('flags', 0)),
                         off, len(nb), 0, 0)
    jt = bytearray()
    for j, nm in jobs:
        nb = nm.encode('utf-8')[:MAX_JOB_NAME]
        jt += JOB.pack(j, len(nb), len(names))
        names += nb
    names += b'\0' * (-len(names) % 4)
    rec_off = HDR.size
    names_off = rec_off + len(body)
    st_off = names_off + len(names)
    sb = bytearray()
    for s in status:
        sb += ST.pack(_u16(s['id']), _u16(s['type']), _u16(s['reapply']), _u16(s['cleared_by']), _u16(s['icon']), _u16(s['apply_id'][0]), _u16(s['apply_id'][1]), 0,
                      _i32(s['apply_value'][0]), _i32(s['apply_value'][1]), _u16(s['step_effect']), _u16(s['end_effect']), 0)
    job_off = st_off + len(sb)
    hdr = HDR.pack(len(skills), REC_BYTES, rec_off, names_off, len(names), len(status), ST_BYTES, st_off if status else 0, len(jobs), job_off if jobs else 0, 0, 0)
    return hdr + bytes(body) + bytes(names) + bytes(sb) + bytes(jt)


def dec_jobs(d):
    """-> [(job id, name)] of the job name table; [] when the section has none. Same bounds as GsvrRoseJob.JobCount / JobName (a bad table reads as none)."""
    if len(d) < HDR.size:
        return []
    h = HDR.unpack_from(d, 0)
    no, nbytes, n, o = h[3], h[4], h[8], h[9]
    if n <= 0 or n > MAX_JOBS or o < HDR.size or o + n * JOB.size > len(d):
        return []
    out = []
    for i in range(n):
        j, ln, off = JOB.unpack_from(d, o + i * JOB.size)
        if ln > MAX_JOB_NAME or off + ln > nbytes:
            out.append((j, ''))
            continue
        out.append((j, bytes(d[no + off: no + off + ln]).decode('utf-8', 'replace')))
    return out


def dec_skdb(d):
    """-> (skills, status); raises ValueError on a bad layout. Same bounds as the Unity reader (GsvrRoseSkill.Valid)."""
    if len(d) < HDR.size:
        raise ValueError('SKDB shorter than its header')
    n, stride, ro, no, nbytes, ns, sstride, so = HDR.unpack_from(d, 0)[:8]
    if n > MAX_SKILLS or stride < REC_BYTES or ro < HDR.size or ro + n * stride > len(d) or no < HDR.size or no + nbytes > len(d) or ns > MAX_STATUS:
        raise ValueError('bad SKDB header')
    if ns and (sstride < ST_BYTES or so < HDR.size or so + ns * sstride > len(d)):
        raise ValueError('bad SKDB status table')
    skills = []
    prev = -1
    for i in range(n):
        v = REC.unpack_from(d, ro + i * stride)
        if v[0] <= prev:
            raise ValueError('SKDB records not sorted by id')
        prev = v[0]
        if v[55] + v[56] > nbytes:
            raise ValueError('SKDB name outside the blob')
        skills.append(dict(id=v[0], base=v[1], level=v[2], learn_pts=v[3], page=v[4], type=v[5], cast_range=v[6], target_filter=v[7], scope=v[8], power=v[9],
                           harm=v[10], success_pct=v[11], status=[v[12], v[13]], duration_s=v[14], damage_type=v[15], use_type=[v[16], v[17]], use_value=[v[18], v[19]],
                           cooldown=v[20], cooldown_group=v[21], add_type=[v[22], v[23]], add_value=[v[24], v[25]], add_rate=[v[26], v[27]], job_mask=v[28],
                           job_class=v[29], action_mode=v[30], req_skill=list(v[31:34]), req_skill_level=list(v[34:37]), req_type=[v[37], v[38]], req_value=[v[39], v[40]],
                           icon=v[41], cast_motion=v[42], cast_speed=v[43], repeat_motion=v[44], repeat_count=v[45], action_motion=v[46], action_speed=v[47],
                           hit_count=v[48], req_weapon=list(v[49:54]), flags=v[54], name=bytes(d[no + v[55]: no + v[55] + v[56]]).decode('utf-8', 'replace')))
    status = []
    for i in range(ns):
        v = ST.unpack_from(d, so + i * sstride)
        status.append(dict(id=v[0], type=v[1], reapply=v[2], cleared_by=v[3], icon=v[4], apply_id=[v[5], v[6]], apply_value=[v[8], v[9]], step_effect=v[10], end_effect=v[11]))
    return skills, status


def _stb(vfs, name):
    try:
        return F.stb(vfs.read('3DDATA/STB/%s.STB' % name))
    except KeyError:
        return None


def _names(vfs):
    """string id -> English name of LIST_SKILL_S.STL (language 1, else the first language); {} without the file"""
    try:
        typ, keys, langs = F.stl(vfs.read('3DDATA/STB/LIST_SKILL_S.STL'))
    except (KeyError, ValueError):
        return {}
    lang = langs[1] if len(langs) > 1 else (langs[0] if langs else [])
    out = {}
    for k, (i, _h) in keys.items():
        if i < len(lang) and lang[i] and lang[i][0]:
            out[k] = lang[i][0].strip()
    return out


def _job_names(vfs):
    """STR_JOB.STL (language 1, else the first) -> [(job id, name)] for the keys that are job ids; [] without the file (no warning: the runtime then shows the id)."""
    try:
        typ, keys, langs = F.stl(vfs.read('3DDATA/STB/STR_JOB.STL'))
    except (KeyError, ValueError, struct.error, IndexError):
        return []
    lang = langs[1] if len(langs) > 1 else (langs[0] if langs else [])
    out = []
    for k, (i, _h) in keys.items():
        k = str(k).strip()
        if not k.isdigit() or int(k) > 65535:
            continue
        if i < len(lang) and lang[i] and lang[i][0] and lang[i][0].strip():
            out.append((int(k), lang[i][0].strip()))
    return sorted(out)


def build_section(vfs):
    """-> (sections, warnings). Never raises on a missing table: no LIST_SKILL = no section."""
    warn = []
    sk = _stb(vfs, 'LIST_SKILL')
    if sk is None:
        return [], ['SKDB: LIST_SKILL.STB missing']
    class_stb = _stb(vfs, 'LIST_CLASS')
    if class_stb is None:
        warn.append('SKDB: LIST_CLASS.STB missing (no job requirement resolved)')
    st = _stb(vfs, 'LIST_STATUS')
    if st is None:
        warn.append('SKDB: LIST_STATUS.STB missing (no buffs / debuffs)')
    names = _names(vfs)
    if not names:
        warn.append('SKDB: LIST_SKILL_S.STL missing (no names, no start skills)')
    recs = []
    big = 0
    for sid in range(1, sk['rows']):
        if sid > 65535:
            big += 1
            continue
        c = sk['cells'][sid]
        key = str(c[-1]).strip() if c else ''
        r = skill_record(sid, c, names.get(key, ''), class_stb)
        if r:
            recs.append(r)
    if big:
        warn.append('SKDB: %d rows above id 65535 skipped' % big)
    status = []
    if st is not None:
        for sid in range(1, min(st['rows'], 65536)):
            s = status_record(sid, st['cells'][sid])
            if s:
                status.append(s)
    if not recs:
        return [], warn + ['SKDB: no player skill rows']
    return [(TAG_SKDB, 1, len(recs), enc_skdb(recs, status, _job_names(vfs)))], warn
