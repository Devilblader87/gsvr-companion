"""Item database for the Unity runtime (docs/RPG1_PACK.md section 13): section ITEM, optional and additive.

The runtime (GoldSrcVR GsvrRoseItems) computes the player's attack / defence / hit / avoid / crit / speeds from basic stats + level + equipped items with the original
formulas, checks equip requirements and runs potions; everything it needs per item is in one fixed record. A pack without ITEM keeps the old placeholder stats.

Source tables (read only, from the player's own client; column numbers = rosefmt.stb cell indices, the same numbers rose-offline's item_database.rs uses):
  LIST_FACEITEM / CAP / BODY / ARMS / FOOT / BACK / JEWEL / WEAPON / SUBWPN (types 1..9, equipment) and LIST_USEITEM (type 10, consumables), LIST_JEMITEM / LIST_NATURAL / LIST_QUESTITEM (types 11 / 12 / 13, gems, materials, quest items), names via LIST_*_S.STL (language 1);
  LIST_CLASS (job class -> job ids), LIST_STATUS (potion over-time effect), INIT_AVATAR (start stats / equipment / inventory per gender).

Layout (little endian, 4-byte aligned):
  header 48: u32 count ; u32 stride (80) ; u32 recOff (48) ; u32 namesOff ; u32 namesBytes ; u32 startCount (0..8) ; u32 startOff ; u32 startStride (256) ;
             u32 feetDefaultSpeed (LIST_FOOT row 0 cell 33: the bare-feet speed used when no shoes are worn) ;
             u32 unarmedAttackSpeed (LIST_WEAPON row 0 cell 36) ; u32 unarmedRange (LIST_WEAPON row 0 cell 33, cm; rose-offline: weapon row 0 = the unarmed attack data) ; u32 0
  record 80 at recOff + i * stride, sorted by code (binary search):
     0 u32 code (type * 1000 + number) ; 4 u16 class (cell 4) ; 6 u16 icon (cell 9, ITEM1 sprite number) ; 8 u32 basePrice (5) ; 12 u16 weight (7) ; 14 u16 quality (8)
    16 u32 jobMask (cell 16 job class resolved through LIST_CLASS to bits of JOBS; 0 = anyone may equip)
    20 u16 reqType0 ; 22 i16 reqVal0 ; 24 u16 reqType1 ; 26 i16 reqVal1      equipment: cells 19/20, 21/22 (ability type, minimum: 31 level, 10..15 STR DEX INT CON CHA SEN)
                                                                            consumable: cells 17/18 (the use requirement) in slot 0
    28 u16 addType0 ; 30 i16 addVal0 ; 32 u16 addType1 ; 34 i16 addVal1      equipment: cells 24/25, 27/28 (ability added while worn; 18 attack, 19 defence, 20 hit, 22 avoid, 26 crit ...)
                                                                            consumable: cells 19/20 (ability changed on use: 16 hp, 17 mp ...) in slot 0
    36 u16 durability (29) ; 38 u16 defence (31) ; 40 u16 resistance (32) ; 42 i16 cell33 (weapon attack range cm / shoes + back move speed)
    44 u16 motion (weapon 34) ; 46 i16 attack (weapon 35) ; 48 i16 attackSpeed (weapon 36) ; 50 u8 magic (weapon 37) ; 51 u8 flags (bit0 two-handed weapon class)
    52 i32 useValue (consumable cell 20 full width: potion total, skill id ...)
    56 u16 statusId (consumable cell 24, LIST_STATUS row) ; 58 u16 statusType (LIST_STATUS cell 1 of the applied effect: 1 hp over time, 2 mp over time) ;
    60 u16 perSecond (LIST_STATUS cell 6 of the item's status row: value per second) ; 62 u16 reapply (LIST_STATUS cell 2 of the applied effect: 1 = may replace a running one)
    64 u32 nameOff ; 68 u16 nameLen (UTF-8, names blob) ; 70 u16 effectId (LIST_STATUS row of the applied effect, cell 5) ; 72 u32 jobClass (raw cell 16) ;
    76 u16 fieldModel (cell 10: the ZSC object of LIST_FIELDITEM.ZSC the dropped item is drawn with, 0 = the money model) ; 78 u8 rareType (cell 30, equipment) ; 79 u8 hairType (cell 33 of caps)
  start record 256 per INIT_AVATAR row 0 (male) and 1 (female):
     0 u16 stats[6] (STR DEX INT CON CHA SEN) ; 12 u32 0 ; 16 u32 equip[8] (cells 6..13, item codes, EquipmentIndex order) ; 48 u32 invEquip[10] (14..23) ;
    88 u32 consumables[5][2] (24..33 code, qty) ; 128 u32 materials[5][2] (34..43) ; 168 u32 money (44) ; 172 u32 zone (45) ; rest 0
    Codes that do not fit their place (wrong type for the slot, number >= 1000, quantity <= 0) are written as 0 with a warning.

Rows: every row of the tables that has a string id plus every code INIT_AVATAR names. Item numbers >= 1000 cannot be written as a base1000 code (they would collide with the next type);
such rows are skipped with a counted warning.
No text or table of the game is committed; everything is read at conversion time on the player's machine.
"""
import struct

import rosefmt as F

TAG_ITEM = b'ITEM'
HDR = struct.Struct('<12I')
REC = struct.Struct('<IHHIHHI HhHh HhHh HHHh HhhBB i HHHH IHHIHBB')
REC_BYTES = 80
START = struct.Struct('<6HI8I10I10I10III')
START_BYTES = 256
MAX_ITEMS = 65536      # same bounds as GsvrRoseItems.Valid
MAX_STARTS = 8
assert REC.size == REC_BYTES, REC.size
assert START.size <= START_BYTES

TABLES = {1: 'LIST_FACEITEM', 2: 'LIST_CAP', 3: 'LIST_BODY', 4: 'LIST_ARMS', 5: 'LIST_FOOT', 6: 'LIST_BACK', 7: 'LIST_JEWEL', 8: 'LIST_WEAPON',
          9: 'LIST_SUBWPN', 10: 'LIST_USEITEM', 11: 'LIST_JEMITEM', 12: 'LIST_NATURAL', 13: 'LIST_QUESTITEM', 14: 'LIST_PAT'}
# types 11 / 12 / 13 (gems, materials = monster parts and ores, quest items; ITEMTYPES): plain stackable items. Only the common cells are carried (cell 4 class, 5 price, 7 weight, 8 quality, 9 icon, 10 field
# model, last cell = string id); the equipment cells 16..37 mean something else there (a gem's ability pairs sit in cells 16..19, they live in ITEX) and are left 0.
PLAIN_TYPES = (11, 12, 13)
# type 14 = vehicle part (rose-data ItemType::Vehicle, LIST_PAT; carts and castle gear, docs/design/rose_carts.md). Its record fields, same bytes, different meaning:
#   cls = cell 4 (511 cart body / 512 gear body / 521, 522 engine / 531 wheels, 532 leg / 551, 552 arms), req[0] = ability requirement cells 21 / 22 (skill requirement cells 19 / 20 are empty
#   in every row of the 1.31 client and not carried), durability = cell 29, defence = cell 31 (max fuel, unused by the server), resistance = cell 32 (fuel use rate per 10 s of driving),
#   c33 = cell 33 (move speed), motion = cell 40 (base motion index), attack = cell 35 (attack range), attack_speed = cell 36 (attack power), magic = cell 37 (attack speed), add = cells 24 / 25, 27 / 28
# job ids in mask bit order: visitor, the four first jobs, the eight second jobs, the eight third jobs
JOBS = (0, 111, 211, 311, 411, 121, 122, 221, 222, 321, 322, 421, 422, 131, 132, 231, 232, 331, 332, 431, 432)
# two-handed weapon classes (rose-data ItemClass::is_two_handed_weapon)
TWO_HANDED = (221, 222, 223, 231, 232, 233, 241, 251, 252, 253)
# item types allowed per start equipment slot (EquipmentIndex face head body back hands feet weapon sub)
START_SLOT_TYPES = (1, 2, 3, 6, 4, 5, 8, 9)


def _int(c, i, default=0):
    try:
        v = c[i]
    except IndexError:
        return default
    v = str(v).strip()
    if v.lstrip('-').isdigit():
        return int(v)
    return default


def _i16(v):
    return max(-32768, min(32767, v))


def _u16(v):
    return max(0, min(65535, v))


def job_mask(class_stb, job_class):
    """LIST_CLASS row `job_class` -> bit mask over JOBS; 0 = no requirement (no class, a class row without jobs, or no table)"""
    if not job_class or class_stb is None or not (0 <= job_class < class_stb['rows']):
        return 0
    c = class_stb['cells'][job_class]
    m = 0
    for i in range(1, 9):
        v = c[i].strip() if i < len(c) else ''
        if v.isdigit() and int(v) in JOBS:
            m |= 1 << JOBS.index(int(v))
    return m


def status_info(status_stb, sid):
    """potion status row -> (statusType of the applied effect, value per second, reapply flag of the applied effect, applied effect id); zeros when unknown.
    The item's status row lists (effect id, value per second) in cells 5 / 6; the effect row's cell 1 is its type (1 hp, 2 mp), cell 2 'can be reapplied' - rose-offline use_item_system / can_apply."""
    if status_stb is None or not (0 < sid < status_stb['rows']):
        return 0, 0, 0, 0
    c = status_stb['cells'][sid]
    eff, vps = _int(c, 5), _int(c, 6)
    if not (0 < eff < status_stb['rows']) or vps <= 0:
        return 0, 0, 0, 0
    e = status_stb['cells'][eff]
    return _int(e, 1), vps, 1 if _int(e, 2) else 0, eff


def item_record(code, cells, name, class_stb=None, status_stb=None):
    """one STB row -> record dict"""
    t = code // 1000
    c = cells
    r = dict(code=code, cls=_int(c, 4), icon=_int(c, 9), price=max(0, _int(c, 5)), weight=_int(c, 7), quality=_int(c, 8), job_class=_int(c, 16),
             req=[(0, 0), (0, 0)], add=[(0, 0), (0, 0)], durability=0, defence=0, resistance=0, c33=0, motion=0, attack=0, attack_speed=0, magic=0, flags=0,
             use_value=0, status_id=0, status_type=0, per_second=0, reapply=0, effect_id=0, field=max(0, _int(c, 10)), rare=0, hair=0, name=name or '')
    r['job_mask'] = job_mask(class_stb, r['job_class'])
    if t in PLAIN_TYPES:
        r['job_class'] = r['job_mask'] = 0
    elif t == 10:
        r['job_class'] = r['job_mask'] = 0      # consumables: cell 16 is no job class
        r['req'][0] = (_int(c, 17), _int(c, 18))
        r['add'][0] = (_int(c, 19), _int(c, 20))
        r['use_value'] = _int(c, 20)
        r['status_id'] = _int(c, 24)
        r['status_type'], r['per_second'], r['reapply'], r['effect_id'] = status_info(status_stb, r['status_id'])
    elif t == 14:
        r['job_class'] = r['job_mask'] = 0      # cell 16 is the vehicle type (21 cart, 31 castle gear), no job class; cell 68 (job requirement) does not exist in the 1.31 table
        r['req'] = [(_int(c, 21), _int(c, 22)), (0, 0)]
        r['add'] = [(_int(c, 24), _int(c, 25)), (_int(c, 27), _int(c, 28))]
        r['durability'] = _int(c, 29)
        r['rare'] = max(0, min(255, _int(c, 30)))
        r['defence'] = _int(c, 31)
        r['resistance'] = _int(c, 32)
        r['c33'] = _int(c, 33)
        r['motion'] = _int(c, 40)
        r['attack'] = _int(c, 35)
        r['attack_speed'] = _int(c, 36)
        r['magic'] = max(0, min(255, _int(c, 37)))
    else:
        r['req'] = [(_int(c, 19), _int(c, 20)), (_int(c, 21), _int(c, 22))]
        r['add'] = [(_int(c, 24), _int(c, 25)), (_int(c, 27), _int(c, 28))]
        r['durability'] = _int(c, 29)
        r['rare'] = max(0, min(255, _int(c, 30)))
        if t == 2:
            r['hair'] = max(0, min(255, _int(c, 33)))
        r['defence'] = _int(c, 31)
        r['resistance'] = _int(c, 32)
        r['c33'] = _int(c, 33)
        if t == 8:
            r['motion'] = _int(c, 34)
            r['attack'] = _int(c, 35)
            r['attack_speed'] = _int(c, 36)
            r['magic'] = 1 if _int(c, 37) else 0
            r['flags'] = 1 if r['cls'] in TWO_HANDED else 0
    return r


def enc_item(recs, starts=(), feet_default=0, unarmed_speed=0, unarmed_range=0):
    """recs: record dicts (any order, sorted here); starts: dicts(stats[6], equip[8], inv_equip[10], cons[5][2], mats[5][2], money, zone)"""
    recs = sorted(recs, key=lambda r: r['code'])
    if len(recs) > MAX_ITEMS or len(starts) > MAX_STARTS:
        raise ValueError('ITEM: too many records')
    names = bytearray()
    body = bytearray()
    for r in recs:
        nb = r['name'].encode('utf-8')[:255]
        off = len(names)
        names += nb
        (q0, v0), (q1, v1) = r['req']
        (a0, w0), (a1, w1) = r['add']
        body += REC.pack(r['code'], _u16(r['cls']), _u16(r['icon']), r['price'] & 0xFFFFFFFF, _u16(r['weight']), _u16(r['quality']), r['job_mask'] & 0xFFFFFFFF,
                         _u16(q0), _i16(v0), _u16(q1), _i16(v1), _u16(a0), _i16(w0), _u16(a1), _i16(w1),
                         _u16(r['durability']), _u16(r['defence']), _u16(r['resistance']), _i16(r['c33']),
                         _u16(r['motion']), _i16(r['attack']), _i16(r['attack_speed']), r['magic'] & 0xFF, r['flags'] & 0xFF,
                         max(-2 ** 31, min(2 ** 31 - 1, r['use_value'])),
                         _u16(r['status_id']), _u16(r['status_type']), _u16(r['per_second']), _u16(r.get('reapply', 0)),
                         off, len(nb), _u16(r.get('effect_id', 0)), r['job_class'] & 0xFFFFFFFF,
                         _u16(r.get('field', 0)), r.get('rare', 0) & 0xFF, r.get('hair', 0) & 0xFF)
    rec_off = HDR.size
    names_off = rec_off + len(body)
    names += b'\0' * (-len(names) % 4)
    start_off = names_off + len(names)
    sb = bytearray()
    for s in starts:
        flat_c = [x for p in s['cons'] for x in p]
        flat_m = [x for p in s['mats'] for x in p]
        one = START.pack(*[_u16(x) for x in s['stats']], 0, *s['equip'], *s['inv_equip'], *flat_c, *flat_m, s['money'] & 0xFFFFFFFF, s['zone'] & 0xFFFFFFFF)
        sb += one + b'\0' * (START_BYTES - len(one))
    hdr = HDR.pack(len(recs), REC_BYTES, rec_off, names_off, len(names), len(starts), start_off if starts else 0, START_BYTES, feet_default & 0xFFFFFFFF,
                   unarmed_speed & 0xFFFFFFFF, unarmed_range & 0xFFFFFFFF, 0)
    return hdr + bytes(body) + bytes(names) + bytes(sb)


def dec_item(d):
    """-> (records, starts, header dict); raises ValueError on a bad layout. Same bounds as the Unity reader (GsvrRoseItems.Valid)."""
    if len(d) < HDR.size:
        raise ValueError('ITEM shorter than its header')
    n, stride, ro, no, nbytes, ns, so, sstride, feet, uspd, urng, _ = HDR.unpack_from(d, 0)
    if n > MAX_ITEMS or stride < REC_BYTES or ro < HDR.size or ro + n * stride > len(d) or no + nbytes > len(d) or ns > MAX_STARTS:
        raise ValueError('bad ITEM header')
    if ns and (sstride < START.size or so < HDR.size or so + ns * sstride > len(d)):
        raise ValueError('bad ITEM start table')
    recs = []
    prev = -1
    for i in range(n):
        v = REC.unpack_from(d, ro + i * stride)
        if v[0] <= prev:
            raise ValueError('ITEM records not sorted by code')
        prev = v[0]
        if v[29] + v[30] > nbytes:
            raise ValueError('ITEM name outside the blob')
        nm = bytes(d[no + v[29]: no + v[29] + v[30]]).decode('utf-8', 'replace')
        recs.append(dict(code=v[0], cls=v[1], icon=v[2], price=v[3], weight=v[4], quality=v[5], job_mask=v[6], req=[(v[7], v[8]), (v[9], v[10])], add=[(v[11], v[12]), (v[13], v[14])],
                         durability=v[15], defence=v[16], resistance=v[17], c33=v[18], motion=v[19], attack=v[20], attack_speed=v[21], magic=v[22], flags=v[23],
                         use_value=v[24], status_id=v[25], status_type=v[26], per_second=v[27], reapply=v[28], name=nm, effect_id=v[31], job_class=v[32], field=v[33], rare=v[34], hair=v[35]))
    starts = []
    for i in range(ns):
        v = START.unpack_from(d, so + i * sstride)
        starts.append(dict(stats=list(v[0:6]), equip=list(v[7:15]), inv_equip=list(v[15:25]), cons=[list(v[25 + 2 * k:27 + 2 * k]) for k in range(5)],
                           mats=[list(v[35 + 2 * k:37 + 2 * k]) for k in range(5)], money=v[45], zone=v[46]))
    return recs, starts, dict(feet_default=feet, unarmed_speed=uspd, unarmed_range=urng)


def _code_ok(code, types):
    return code >= 1000 and code // 1000 in types and code % 1000 > 0


def start_rows(stb, warn=None):
    """INIT_AVATAR rows 0 (male) and 1 (female) -> start dicts (rose-offline character_creator columns). Codes that do not fit their place become 0 (warning)."""
    out = []
    bad = 0
    for row in range(min(2, stb['rows'])):
        c = stb['cells'][row]
        s = dict(stats=[max(0, min(65535, _int(c, i))) for i in range(6)], equip=[_int(c, 6 + i) for i in range(8)], inv_equip=[_int(c, 14 + i) for i in range(10)],
                 cons=[[_int(c, 24 + 2 * k), _int(c, 25 + 2 * k)] for k in range(5)], mats=[[_int(c, 34 + 2 * k), _int(c, 35 + 2 * k)] for k in range(5)],
                 money=max(0, _int(c, 44)), zone=max(0, _int(c, 45)))
        for e in range(8):
            if s['equip'][e] and not _code_ok(s['equip'][e], (START_SLOT_TYPES[e],)):
                s['equip'][e] = 0; bad += 1
        for k in range(10):
            if s['inv_equip'][k] and not _code_ok(s['inv_equip'][k], range(1, 10)):
                s['inv_equip'][k] = 0; bad += 1
        for lst, types in ((s['cons'], (10,)), (s['mats'], (11, 12, 13))):
            for p in lst:
                if (p[0] or p[1]) and (not _code_ok(p[0], types) or p[1] <= 0):
                    p[0] = p[1] = 0; bad += 1
        out.append(s)
    if bad and warn is not None:
        warn.append('ITEM: %d INIT_AVATAR entries did not fit their place and were dropped' % bad)
    return out


def _stb(vfs, name):
    try:
        return F.stb(vfs.read('3DDATA/STB/%s.STB' % name))
    except KeyError:
        return None


def build_section(vfs, names):
    """-> (sections, warnings). `names`: rose_pack.ItemNames(vfs) (English names via the STLs). Never raises on a missing table: no tables = no section."""
    warn = []
    class_stb = _stb(vfs, 'LIST_CLASS')
    status_stb = _stb(vfs, 'LIST_STATUS')
    init = _stb(vfs, 'INIT_AVATAR')
    starts = start_rows(init, warn) if init else []
    if init is None:
        warn.append('ITEM: INIT_AVATAR.STB missing (no start record)')
    want = set()
    for s in starts:
        want.update(x for x in s['equip'] + s['inv_equip'] if x >= 1000)
        want.update(p[0] for p in s['cons'] + s['mats'] if p[0] >= 1000)
    recs = []
    feet_default = unarmed_speed = unarmed_range = 0
    skipped = 0
    for t, tb in sorted(TABLES.items()):
        stb = _stb(vfs, tb)
        if stb is None:
            warn.append('ITEM: %s.STB missing' % tb)
            continue
        if t == 5 and stb['rows'] > 0:
            feet_default = max(0, _int(stb['cells'][0], 33))
        if t == 8 and stb['rows'] > 0:
            unarmed_speed = max(0, _int(stb['cells'][0], 36))
            unarmed_range = max(0, _int(stb['cells'][0], 33))
        for n in range(1, stb['rows']):
            c = stb['cells'][n]
            sid = c[-1].strip() if c else ''
            if n >= 1000:
                if sid:
                    skipped += 1        # a base1000 code would collide with type t + 1
                continue
            code = t * 1000 + n
            if not sid and code not in want:
                continue
            nm = names.name(code) if names else None
            recs.append(item_record(code, c, (nm or '').strip(), class_stb, status_stb))
    if skipped:
        warn.append('ITEM: %d named rows with an item number >= 1000 skipped (no base1000 code)' % skipped)
    if not recs:
        return [], warn + ['ITEM: no item tables']
    return [(TAG_ITEM, 1, len(recs), enc_item(recs, starts, feet_default, unarmed_speed, unarmed_range))], warn


# ------------------------------------------------------------------ coverage: every item a player can meet resolves to a name + icon
def reachable_codes(secs):
    """secs: {tag bytes: section data} of ONE zone pack -> {source: set of item codes} for DROP (drop item table), SHOP (tab slots),
    QUST (quest item table, item conditions and rewards), PRODUCT (craft recipe materials, ITEX craft block). Missing / undecodable sections are skipped (old packs)."""
    import rose_gameplay
    import rose_quest
    import rose_shop
    import rose_itemx
    out = {'DROP': set(), 'SHOP': set(), 'QUST': set(), 'PRODUCT': set()}
    try:
        _rows, items = rose_gameplay.dec_drop(secs[b'DROP'])
        out['DROP'].update(i['code'] for i in items if i['code'] >= 1000)
    except (KeyError, ValueError, struct.error):
        pass
    try:
        out['SHOP'].update(c for c in rose_shop.dec_shop(secs[b'SHOP'])['slots'] if c >= 1000)
    except (KeyError, ValueError, struct.error):
        pass
    try:
        q = rose_quest.dec_qust(secs[b'QUST'])['tables']
        out['QUST'].update(r[0] for r in q['qitem'] if r[0] >= 1000)
        for r in q['cond']:
            if r[0] == 4 and r[3] >= 1000:                  # (op, sub, a, b, c, d): quest item condition, b = item code
                out['QUST'].add(r[3])
        for r in q['rew']:
            if r[0] == 1 and r[3] >= 1000:                  # item reward / removal
                out['QUST'].add(r[3])
            elif r[0] == 5 and r[1] == 2 and r[4] >= 1000:  # calculated item reward, c = item code
                out['QUST'].add(r[4])
    except (KeyError, ValueError, struct.error, IndexError):
        pass
    try:
        cr = rose_itemx.dec_itemx(secs[b'ITEX'])['craft']
        if cr:
            for mats in cr['recipes'].values():
                out['PRODUCT'].update(m[0] for m in mats if m[0] >= 1000)
    except (KeyError, ValueError, struct.error):
        pass
    return out


def missing_codes(reach, item_recs):
    """reach: reachable_codes() result ; item_recs: dec_item() records -> {source: sorted codes without a name or without an icon in ITEM}"""
    ok = set(r['code'] for r in item_recs if r['name'] and r['icon'] > 0)
    return {src: sorted(c for c in codes if c not in ok) for src, codes in reach.items() if any(c not in ok for c in codes)}
