"""Gameplay tables for the combat loop of the Unity runtime (RPG1 1.3, docs/RPG1_PACK.md section 9): DROP and SKIL.

Both sections are optional and additive; the runtime falls back to built-in placeholders when they are missing. Layouts (all little endian), verified against the
retail SearchMMO tables on 2026-11-13 (docs/ROSE_SPEC.md section 14):

  DROP (count = monster types of the window that have a drop row)
      header 32: u32 count ; u32 stride (208) ; u32 recOff (32) ; u32 itemCount ; u32 itemStride (64) ; u32 itemOff ; u32 0 ; u32 0
      record (208): u16 npcId ; u16 0 ; u32 dropRow ; u32 slot[30] ; u32 pool[4][5]
          slot[i] = ITEM_DROP.STB row `dropRow` cells 1..30 (the 30 roll slots):  0 = empty (no drop),  >= 1000 = base1000 item code (item_type * 1000 + item_number),
                    1..4 = redirect v: roll again inside pool[v-1] (uniform over its 5 cells; an empty pool cell = no drop)
          pool[v-1][k] = row cells 26 + 5 * v + k  (31..35, 36..40, 41..45, 46..50); item code or 0.
          A roll: with probability ACTR itemDropPct pick slot uniformly in 0..29, resolve a redirect, 0 = nothing.
      item record (64), one per distinct item code used in the section (sorted by code):
          u32 code ; u16 icon (item STB col 9, sprite number in the UI item atlas) ; u16 itemType (code / 1000) ; u32 basePrice (col 5) ; u16 weight (col 7) ; u16 quality (col 8) ;
          char[48] name (English, UTF-8, NUL padded; the table's Korean name when no STL entry)

  SKIL (count = skill rows)
      header 16: u32 count ; u32 stride (64) ; u32 recOff (16) ; u32 0
      record (64): u16 id ; u16 type ; u16 level ; u16 targetFilter ; u16 castRange (cm, 0 = weapon range) ; u16 scope (cm radius) ; u16 hitCount ; u16 0 ;
                   u32 power ; u32 cooldown (units of 200 ms) ; u16 abilType[2] ; i32 abilValue[2] (cost: abilType 17 = mana, 16 = hp, 40 = money) ;
                   u16 icon ; u16 castMotion ; u16 castMotionSpeed ; u16 actionMotion ; u16 actionMotionSpeed ; u16 0 ;
                   u16 repeatMotion ; u16 repeatCount ; u16 jobReq ; u16 successPct ; u16 durationS ; u16 status1 ; u16 calcType ; u16 progressType
      The last 16 bytes are new in the first retail-verified writer (older readers saw zero padding there).
      Rows: the default set (below) with every level of each skill (rows share col 1 = level 1 skill id and run consecutively), or the ids of the environment variable
      ROSE_SKILL_IDS="901,931" (each id expands to all levels of its skill). Source columns: ROSE_SPEC section 14.

Read only use of the client files, like the rest of the converter. No text of the game is copied into the repository (names are read at run time).
"""
import os
import struct

import rosefmt as F

TAG_DROP = b'DROP'
TAG_SKIL = b'SKIL'
TAGS = (TAG_DROP, TAG_SKIL)
DROP_HDR = struct.Struct('<8I')
DROP_REC = struct.Struct('<HHI30I20I')
DROP_BYTES = DROP_REC.size            # 208
DROP_ITEM = struct.Struct('<IHHIHH48s')
DROP_ITEM_BYTES = DROP_ITEM.size      # 64
DROP_SLOTS = 30
DROP_POOLS = 4
DROP_POOL_SIZE = 5
SKIL_HDR = struct.Struct('<4I')
SKIL_REC = struct.Struct('<8H II 2H 2i 6H 8H')
SKIL_BYTES = 64
assert SKIL_REC.size == SKIL_BYTES
# Level-1 skill ids of the default set (retail 129_129en LIST_SKILL.STB): 16 Normal Attack (basic action, row 16 has level 0),
# 1501 Power Attack and 1541 Double Slash (Hawker melee), 901 Mana Bolt (Muse ranged bolt), 931 Cure (Muse single target heal).
MAX_LEVELS = 10      # a skill has up to 10 levels; rows after that (Double Slash -> Triple Slash, ids 1551..) are the next skill of the chain
DEFAULT_SKILLS = (16, 1501, 1541, 901, 931)


def _int(c, i, default=0):
    try:
        v = c[i]
    except IndexError:
        return default
    v = str(v).strip()
    if v.lstrip('-').isdigit():
        return int(v)
    return default


# ------------------------------------------------------------------ DROP
def drop_layout(stb, row):
    """ITEM_DROP.STB row -> (slots[30], pools[4][5]) of ints (see the module docstring); ([0]*30, zeros) when the row is outside the table."""
    slots = [0] * DROP_SLOTS
    pools = [[0] * DROP_POOL_SIZE for _ in range(DROP_POOLS)]
    if not (0 <= row < stb['rows']):
        return slots, pools
    c = stb['cells'][row]
    for i in range(DROP_SLOTS):
        v = _int(c, 1 + i)
        slots[i] = v if (v >= 1000 or 1 <= v <= DROP_POOLS) else 0
    for p in range(DROP_POOLS):
        for k in range(DROP_POOL_SIZE):
            v = _int(c, 31 + p * DROP_POOL_SIZE + k)
            pools[p][k] = v if v >= 1000 else 0
    return slots, pools


def drop_codes(slots, pools):
    """every item code a row can yield (slots and the pools it redirects to)"""
    out = set(v for v in slots if v >= 1000)
    for v in slots:
        if 1 <= v <= DROP_POOLS:
            out.update(x for x in pools[v - 1] if x)
    return out


def enc_drop(rows, items=()):
    """rows: dict(npc, drop_row, slots[30], pools[4][5]); items: dict(code, icon, type, price, weight, quality, name)"""
    recs = bytearray()
    for r in rows:
        flat = [x for p in r['pools'] for x in p]
        recs += DROP_REC.pack(r['npc'], 0, r['drop_row'], *r['slots'], *flat)
    recoff = DROP_HDR.size
    itemoff = recoff + len(recs)
    ib = bytearray()
    for it in items:
        ib += DROP_ITEM.pack(it['code'], it['icon'] & 0xFFFF, it['type'] & 0xFFFF, it['price'] & 0xFFFFFFFF, it['weight'] & 0xFFFF, it['quality'] & 0xFFFF,
                             it['name'].encode('utf-8')[:47])
    return DROP_HDR.pack(len(rows), DROP_BYTES, recoff, len(items), DROP_ITEM_BYTES, itemoff if items else 0, 0, 0) + bytes(recs) + bytes(ib)


def dec_drop(d):
    n, stride, ro, ni, istride, io, _, _ = DROP_HDR.unpack_from(d, 0)
    if stride < DROP_BYTES or ro < DROP_HDR.size or ro + n * stride > len(d):
        raise ValueError('bad DROP header')
    rows = []
    for i in range(n):
        v = DROP_REC.unpack_from(d, ro + i * stride)
        rows.append(dict(npc=v[0], drop_row=v[2], slots=list(v[3:33]), pools=[list(v[33 + p * 5:38 + p * 5]) for p in range(DROP_POOLS)]))
    items = []
    if ni:
        if istride < DROP_ITEM_BYTES or io < ro + n * stride or io + ni * istride > len(d):
            raise ValueError('bad DROP item table')
        for i in range(ni):
            v = DROP_ITEM.unpack_from(d, io + i * istride)
            items.append(dict(code=v[0], icon=v[1], type=v[2], price=v[3], weight=v[4], quality=v[5], name=v[6].split(b'\0', 1)[0].decode('utf-8', 'replace')))
    return rows, items


def drop_rows(vfs, side_actors, monsters=None):
    """DROP rows for the monster types among `side_actors` (rose_pack.npc_sidecar()['actors']); `monsters` = set of ids to restrict to (NPCs reuse col 18 as a minimap icon).
    -> (rows, stb) ; ([], None) when ITEM_DROP.STB is missing"""
    try:
        stb = F.stb(vfs.read('3DDATA/STB/ITEM_DROP.STB'))
    except KeyError:
        return [], None
    rows = []
    for key, a in sorted(side_actors.items(), key=lambda kv: int(kv[0])):
        nid = int(key)
        if monsters is not None and nid not in monsters:
            continue
        dr = a.get('drop_row')
        if not isinstance(dr, int) or dr <= 0:
            continue
        slots, pools = drop_layout(stb, dr)
        if drop_codes(slots, pools):
            rows.append(dict(npc=nid, drop_row=dr, slots=slots, pools=pools))
    return rows, stb


def item_records(rows, items):
    """distinct item codes of `rows` -> item records via items.info(code) (rose_pack.ItemNames); unknown codes get an empty record"""
    codes = set()
    for r in rows:
        codes |= drop_codes(r['slots'], r['pools'])
    out = []
    for code in sorted(codes):
        i = items.info(code) if items else None
        out.append(dict(code=code, type=code // 1000, **(i or dict(icon=0, price=0, weight=0, quality=0, name=''))))
    return out


# ------------------------------------------------------------------ SKIL
def skill_row(stb, sid):
    """LIST_SKILL.STB row -> SKIL record dict, None when the id is outside the table"""
    if not (0 <= sid < stb['rows']):
        return None
    c = stb['cells'][sid]
    return dict(id=sid, type=_int(c, 5), level=_int(c, 2), target_filter=_int(c, 7), cast_range=_int(c, 6) & 0xFFFF, scope=_int(c, 8) & 0xFFFF, hit_count=_int(c, 70) & 0xFFFF,
                power=max(0, _int(c, 9)), cooldown=max(0, _int(c, 20)),
                abil_type=[_int(c, 16) & 0xFFFF, _int(c, 18) & 0xFFFF], abil_value=[_int(c, 17), _int(c, 19)],
                icon=_int(c, 51) & 0xFFFF, cast_motion=_int(c, 52) & 0xFFFF, cast_motion_speed=_int(c, 53) & 0xFFFF,
                action_motion=_int(c, 68) & 0xFFFF, action_motion_speed=_int(c, 69) & 0xFFFF,
                repeat_motion=_int(c, 54) & 0xFFFF, repeat_count=_int(c, 55) & 0xFFFF, job_req=_int(c, 35) & 0xFFFF, success_pct=_int(c, 13) & 0xFFFF,
                duration_s=_int(c, 14) & 0xFFFF, status1=_int(c, 11) & 0xFFFF, calc_type=_int(c, 15) & 0xFFFF, progress_type=_int(c, 29) & 0xFFFF)


def skill_levels(stb, sid):
    """ids of every level of the skill that row `sid` belongs to (col 1 = level 1 id; consecutive rows); [sid] for rows without the column"""
    if not (0 <= sid < stb['rows']):
        return []
    base = _int(stb['cells'][sid], 1, sid) or sid
    out = []
    r = base
    while r < stb['rows'] and len(out) < MAX_LEVELS and (r == base or _int(stb['cells'][r], 1) == base):
        out.append(r)
        r += 1
    return out if sid in out else [sid]


def select_skills(stb, ids=None):
    """`ids` (any level of a skill) or the default set -> rows for every level of each skill, in id order without duplicates"""
    seen = []
    for sid in (ids or DEFAULT_SKILLS):
        for r in skill_levels(stb, sid):
            if r not in seen:
                seen.append(r)
    return [skill_row(stb, r) for r in seen]


def enc_skil(rows):
    recs = bytearray()
    for r in rows:
        rec = SKIL_REC.pack(r['id'], r['type'], r['level'], r['target_filter'], r['cast_range'], r['scope'], r['hit_count'], 0,
                            r['power'], r['cooldown'], r['abil_type'][0], r['abil_type'][1], r['abil_value'][0], r['abil_value'][1],
                            r['icon'], r['cast_motion'], r['cast_motion_speed'], r['action_motion'], r['action_motion_speed'], 0,
                            r.get('repeat_motion', 0), r.get('repeat_count', 0), r.get('job_req', 0), r.get('success_pct', 0), r.get('duration_s', 0),
                            r.get('status1', 0), r.get('calc_type', 0), r.get('progress_type', 0))
        recs += rec
    return SKIL_HDR.pack(len(rows), SKIL_BYTES, SKIL_HDR.size, 0) + bytes(recs)


def dec_skil(d):
    n, stride, ro, _ = SKIL_HDR.unpack_from(d, 0)
    if stride < SKIL_BYTES or ro < SKIL_HDR.size or ro + n * stride > len(d):
        raise ValueError('bad SKIL header')
    out = []
    for i in range(n):
        v = SKIL_REC.unpack_from(d, ro + i * stride)
        out.append(dict(id=v[0], type=v[1], level=v[2], target_filter=v[3], cast_range=v[4], scope=v[5], hit_count=v[6], power=v[8], cooldown=v[9],
                        abil_type=[v[10], v[11]], abil_value=[v[12], v[13]], icon=v[14], cast_motion=v[15], cast_motion_speed=v[16], action_motion=v[17],
                        action_motion_speed=v[18], repeat_motion=v[20], repeat_count=v[21], job_req=v[22], success_pct=v[23], duration_s=v[24],
                        status1=v[25], calc_type=v[26], progress_type=v[27]))
    return out


def build_sections(vfs, side_actors, skill_ids=None, items=None, monsters=None):
    """-> (sections, warnings): [(tag, section_version, count, bytes)] for DROP / SKIL (those with content), plus text warnings. Never raises on a missing table.
    `items`: object with .info(code) (rose_pack.ItemNames) for the DROP item table; `monsters`: ids of the monster types (None = all actors)."""
    sections = []
    warn = []
    try:
        rows, _ = drop_rows(vfs, side_actors, monsters)
        if rows:
            its = item_records(rows, items)
            sections.append((TAG_DROP, 1, len(rows), enc_drop(rows, its)))
        else:
            warn.append('DROP: no ITEM_DROP.STB rows for the monster types of the window')
    except Exception as e:
        warn.append('DROP: %s' % e)
    try:
        env = os.environ.get('ROSE_SKILL_IDS', '').strip()
        ids = skill_ids if skill_ids else ([int(x) for x in env.split(',') if x.strip().isdigit()] if env else None)
        stb = F.stb(vfs.read('3DDATA/STB/LIST_SKILL.STB'))
        sk = [r for r in select_skills(stb, ids) if r]
        if sk:
            sections.append((TAG_SKIL, 1, len(sk), enc_skil(sk)))
        else:
            warn.append('SKIL: no skill rows selected')
    except KeyError:
        warn.append('SKIL: LIST_SKILL.STB missing')
    except Exception as e:
        warn.append('SKIL: %s' % e)
    return sections, warn
