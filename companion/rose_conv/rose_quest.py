"""Quests and NPC dialogs for the Unity runtime (docs/RPG1_PACK.md section 15): section QUST, optional and additive. Read-only, stdlib only.

What the runtime (GoldSrcVR GsvrRoseQuest) needs to run the original quests of a zone without a Lua interpreter:
* the QSD triggers reachable from the zone (conditions + rewards, normalised into fixed 16 byte records, chains resolved to indices);
* per NPC of the zone its conversation (CON file via LIST_EVENT): the menus and messages, and every Lua function they name, translated by symbolic execution of the Lua 4.0
  bytecode into a small decision graph (test / effect / return nodes; the calls of the QF_* / GF_* functions resolved: trigger names become trigger indices);
* the texts the player reads: dialog lines (3DDATA/EVENT/ULNGTB_CON.LTB, column 2 = English like rose-offline-client) and quest name / description / start / end messages
  (LIST_QUEST_S.STL, language 1), stored as UTF-16 code units.

Sources (all read from the player's own client at conversion time; nothing of the game is committed):
  IFO block 2 (NPC id + conversation key) -> LIST_EVENT.STB (key -> CON path, col 3) -> CON (rose-offline con_.rs layout, XOR obfuscated menus, Lua 4 blob)
  LIST_QUESTDATA.STB -> QSD files (rose-offline qsd.rs layout); LIST_QUEST.STB (col 1 time limit, last col string key); LIST_NPC.STB col 41 (death trigger of a monster);
  QSD triggers named levelup_<n> (rose-offline experience_points_system: run on reaching level n); IFO block 12 event objects (trigger name).

Trigger names are matched without case (the original hashes them upper case, rose-offline QuestTriggerHash).

Layout (little endian, 4-byte aligned; offsets from the section start):
  header 128: u32 version (1) ; then 14 pairs (count, offset): trig, cond, rew, npc, menu, msg, fn, node, quest, levelup, death, event, qname, qitem ; u32 strOff ; u32 strBytes ; u32 0
  trigger 20: u32 condFirst ; u16 condCount ; u16 rewCount ; u32 rewFirst ; i32 next (chain on failure, -1) ; i32 nameStr
  cond / reward 16: u8 op ; u8 sub ; u16 a ; i32 b ; i32 c ; i32 d          (field meaning per op: COND_DOC / REW_DOC below)
  npc 24: u32 npcId ; i32 openFn (-1 = always opens) ; u32 menuFirst ; u32 menuCount ; i32 nameStr ; u32 0
  menu 8: u32 msgFirst ; u32 msgCount
  msg 20: u32 type (0 close 1 next 2 show 3 player select 4 jump select) ; i32 value (menu index inside the NPC, -1 = none) ; i32 condFn ; i32 actFn ; i32 textStr
  fn 8: i32 rootNode ; i32 nameStr
  node 24: u8 kind (0 return const, 1 return call, 2 effect, 3 test) ; u8 call ; u8 cmp ; u8 0 ; i32 arg0 ; i32 arg1 ; i32 k ; i32 next ; i32 alt
           test: CMP(call(arg0, arg1), k) ? next : alt ; effect: call, then next ; return const: k ; return call: value of the call
  quest 24: u32 questId ; i32 nameStr ; i32 descStr ; i32 startStr ; i32 endStr ; u32 timeLimit (LIST_QUEST col 1, world ticks)
  levelup 8: u32 level ; u32 trigger      death 8: u32 npcId ; u32 trigger      event 8: u32 eventIndex ; u32 trigger
  qname 8: u32 questId ; i32 nameStr      (names of every quest of LIST_QUEST, so a quest taken in another zone still has its name in the log)
  qitem 12: u32 code (13000 + number) ; u16 icon (LIST_QUESTITEM cell 9, ITEM1 sprite) ; u16 0 ; i32 nameStr     (quest items the zone's triggers name, for the quest log slots)
  strings: u16 length + length x u16 UTF-16 code units, 4-byte aligned; a string reference is its byte offset inside the blob, -1 = none.
"""
import struct, collections

import rosevfs, rosefmt as F
import rose_text as TX

TAG_QUST = b'QUST'
VERSION = 1
HDR_BYTES = 128
TRIG = struct.Struct('<IHHIii')
REC = struct.Struct('<BBHiii')
NPC = struct.Struct('<IiIIiI')
MENU = struct.Struct('<II')
MSG = struct.Struct('<Iiiii')
FN = struct.Struct('<ii')
NODE = struct.Struct('<BBBBiiiii')
QUEST = struct.Struct('<IiiiiI')
QITEM = struct.Struct('<IHHi')
PAIR = struct.Struct('<II')
assert TRIG.size == 20 and REC.size == 16 and NPC.size == 24 and MSG.size == 20 and NODE.size == 24 and QUEST.size == 24
TABLES = ('trig', 'cond', 'rew', 'npc', 'menu', 'msg', 'fn', 'node', 'quest', 'levelup', 'death', 'event', 'qname', 'qitem')
MAX_NODES_PER_FN = 512

LTB_CON = '3DDATA/EVENT/ULNGTB_CON.LTB'
LTB_COLUMN = 2                # rose-offline-client conversation_dialog_system: ltb_event.get_string(string_id, 2)
STL_LANG = 1                  # English, like rose_pack.ItemNames

# ---- normalised condition ops (op = original QSD opcode unless noted)
COND_DOC = {
    0: 'select quest: b = quest id', 1: 'quest variable (QSD ops 1, 2, 8): sub = variable type (0 var 1 switch 2 timer 3 episode 4 job 5 planet 6 union), a = id, b = value, c = cmp',
    3: 'ability value: a = ability type, b = value, c = cmp', 4: 'quest item: a = raw equipment index (1..11 = that equipment slot must hold the item, else a count check), b = item sn, c = count, d = cmp',
    6: 'position: a = zone, b = x cm, c = y cm, d = distance cm', 10: 'random percent: b = low, c = high', 13: 'select npc: b = npc id',
    11: 'object variable (ZONES MORE): sub = object (0 selected npc, 1 selected event), a = variable 0..19, b = value, c = cmp',
    5: 'party (W18 DUNGEONS): sub = must be the leader (1), b = party level, c = cmp (2 >=, 3 <)',
    9: 'has skill (QUEST CHECK): sub = has (1) / has not (0), b = first id, c = last id',
    14: 'user switch: a = id, b = value 0/1', 21: 'object distance: a = object (0 npc 1 event), b = distance cm', 22: 'server channel: b = low, c = high',
    23: 'clan: has a clan: b = 1 / 0 (CLAN QUESTS)', 24: 'clan rank (0 penalty .. 6 master): b = value, c = cmp', 25: 'clan contribution: b = value, c = cmp', 26: 'clan level: b = value, c = cmp',
    27: 'clan points: b = value, c = cmp', 28: 'clan money: b = value, c = cmp', 29: 'clan member count: b = value, c = cmp', 30: 'clan skill in range: sub = has (1 / 0), b = first id, c = last id',
    15: 'party member count (W20): b .. c = the allowed member count (false without a party)', 17: 'compare npc variables (W20): sub = cmp, a = variable of npc b, c = npc 2, d = variable of npc 2',
    18: 'month day time (W20): sub = day of the month (0 any), b .. c = minutes of the day (UTC)', 19: 'week day time (W20): sub = day of the week (0 Sunday), b .. c = minutes of the day (UTC)',
    255: 'unsupported (sub = original op): false'}
REW_DOC = {
    0: 'quest op: sub 0 remove selected 1 add 2 change selected keep data 3 change selected new 4 select, b = quest id', 1: 'item: sub 1 add 0 remove, b = item sn, c = quantity',
    2: 'quest variable (QSD ops 2, 4): sub = variable type, a = id, b = value, c = reward op (5 set 6 add 7 sub 8 zero 9 one)', 3: 'ability: a = type, b = value, c = reward op',
    5: 'calculated: sub 0 xp 1 money 2 item, a = equation, b = value, c = item sn, d = gem', 6: 'hp / mp percent: b = hp, c = mp', 7: 'teleport: a = zone, b = x cm, c = y cm',
    9: 'trigger chain: b = trigger index (-1 unknown name)', 10: 'reset basic stats', 12: 'npc message: sub = type, b = string id', 14: 'skill: sub 1 add 0 remove, b = skill id',
    11: 'object variable (ZONES MORE): sub = object (0 selected npc, 1 selected event), a = variable 0..19, b = value, c = reward op',
    23: 'clan level increase (QUEST CHECK; the runtime fails it: no clan growth yet)', 24: 'clan money: b = value, c = reward op', 25: 'clan points: b = value, c = reward op',
    26: 'clan skill: sub 1 add 0 remove, b = skill id', 27: 'clan contribution: b = value, c = reward op',
    8: 'spawn monster (W18 DUNGEONS): sub = location, a = count, b = npc id, c / d = x / y cm; the runtime accepts it and counts it (not modelled)',
    20: 'set team number (W18): sub = source; accepted, one instance per zone', 21: 'set revive position (W18): b = x cm, c = y cm',
    15: 'set user switch: a = id, b = value', 16: 'clear switch group: a = group', 17: 'clear all switches', 29: 'call lua (client side): no-op', 30: 'reset skills',
    28: 'teleport me and the nearby clan members (CLAN QUESTS): a = zone, b = x cm, c = y cm, d = distance cm',
    255: 'unsupported (sub = original op): fails like the reference server'}

# ---- dialog call ids (node.call)
CALL_IDS = {'QF_checkQuestCondition': 1, 'QF_doQuestTrigger': 2, 'QF_findQuest': 3, 'QF_getUserSwitch': 4, 'QF_getEpisodeVAR': 5, 'QF_getJobVAR': 6,
            'QF_getPlanetVAR': 7, 'QF_getQuestItemQuantity': 8, 'QF_getQuestCount': 9, 'QF_getQuestVar': 10, 'QF_getQuestSwitch': 11, 'QF_getQuestID': 12,
            'GF_getVariable': 13, 'GF_openStore': 20, 'GF_repair': 21, 'GF_appraisal': 22, 'GF_openBank': 23,
            # ZONES MORE: services of the town NPCs (not modelled, the runtime says so) and the NPC object variable 0
            'GF_openSeparate': 24, 'GF_openUpgrade': 25, 'GF_openDeliveryStore': 26, 'QF_getNpcQuestZeroVal': 16,
            # JUNON POLIS: the clan founding / disband NPC (effects; the runtime says the clan service is not available until the guild lane wires them)
            'GF_organizeClan': 27, 'GF_disorganizeClan': 28}
CALL_QVAR_ID, CALL_QSW_ID = 14, 15          # ZONES MORE: QF_getQuestVar / QF_getQuestSwitch whose slot argument is QF_findQuest(questId): fused, a0 = quest id
EFFECT_CALLS = (2, 20, 21, 22, 23, 24, 25, 26, 27, 28)
TRIGGER_ARG_CALLS = (1, 2)                  # first argument is a trigger name
IGNORED_CALLS = ('QF_getEventOwner', 'GF_setRevivePosition', 'GF_GetMotionUseFile', 'GF_SetMotion', 'GF_playSound', 'GF_playEffect', 'QF_MotionCallNpc', 'QF_EffectCallNpc')
LUA_CONSTANTS = {'SV_SEX': 0, 'SV_BIRTH': 1, 'SV_CLASS': 2, 'SV_UNION': 3, 'SV_RANK': 4, 'SV_FAME': 5, 'SV_STR': 6, 'SV_DEX': 7, 'SV_INT': 8, 'SV_CON': 9,
                 'SV_CHA': 10, 'SV_SEN': 11, 'SV_EXP': 12, 'SV_LEVEL': 13, 'SV_POINT': 14}
# node kinds and compare ops (Lua jump op -> runtime cmp)
NK_RET, NK_RETCALL, NK_EFFECT, NK_TEST = 0, 1, 2, 3
CMP_EQ, CMP_NE, CMP_LT, CMP_LE, CMP_GT, CMP_GE = 0, 1, 2, 3, 4, 5


def _u16str(d, p):
    n, = struct.unpack_from('<H', d, p)
    return d[p + 2:p + 2 + n].split(b'\0')[0].decode('cp949', 'replace'), p + 2 + n


def tkey(name):
    return (name or '').strip().upper()


# =============================================================== QSD
MAX_COUNT = 4096          # audit fix: cap of any count read from a QSD / CON file (menus, messages, records, triggers)


def _n(d, p, rec, size=None):
    """Audit fix: a count at d[p] whose n x rec bytes must fit in the record (size) / the buffer; ValueError otherwise."""
    n, = struct.unpack_from('<I', d, p)
    room = (size - 8 - 4) if size is not None else (len(d) - p - 4)
    if n > MAX_COUNT or n * rec > max(room, 0):
        raise ValueError('count %d does not fit' % n)
    return n


def _cond_records(op, d, p, size, warn):
    """One QSD condition record (payload at p) -> list of normalised (op, sub, a, b, c, d) tuples."""
    out = []
    if op == 0:
        out.append((0, 0, 0, struct.unpack_from('<I', d, p)[0], 0, 0))
    elif op in (1, 2):
        n = _n(d, p, 8, size)
        for k in range(n):
            vid, vt, val, cmp_ = struct.unpack_from('<HHhB', d, p + 4 + k * 8)
            out.append((1, (vt >> 8) & 0xFF, vid, val, cmp_, 0))
    elif op == 3:
        n = _n(d, p, 12, size)
        for k in range(n):
            ab, val, cmp_ = struct.unpack_from('<IiB', d, p + 4 + k * 12)
            out.append((3, 0, ab & 0xFFFF, val, cmp_, 0))
    elif op == 4:
        n = _n(d, p, 16, size)
        for k in range(n):
            sn, eq, cnt, cmp_ = struct.unpack_from('<IIIB', d, p + 4 + k * 16)
            out.append((4, 0, eq if 0 <= eq < 0xFFFF else 0, sn, cnt, cmp_))
    elif op == 6:
        z, x, y, _z, dist = struct.unpack_from('<IIIII', d, p)
        out.append((6, 0, z & 0xFFFF, x, y, dist))
    elif op == 8:
        val, cmp_ = struct.unpack_from('<iB', d, p)
        out.append((1, 2, 0, val, cmp_, 0))
    elif op == 9:           # QUEST CHECK: HasSkill / HasSkillInRange (rose-offline qsd.rs): u32 first id, u32 last id, u8 has: sub = has (1 / 0), b = first id, c = last id
        lo, hi, has = struct.unpack_from('<IIB', d, p)
        out.append((9, 1 if has else 0, 0, lo, hi, 0))
    elif op == 10:
        lo, hi = struct.unpack_from('<BB', d, p)
        out.append((10, 0, 0, lo, hi, 0))
    elif op == 11:          # ZONES MORE: object variable of the selected NPC (0) / event object (1): sub = object, a = variable, b = value, c = cmp
        obj, vid, val, cmp_ = struct.unpack_from('<BxHiB', d, p)
        out.append((11, obj, vid, val, cmp_, 0))
    elif op == 13:
        out.append((13, 0, 0, struct.unpack_from('<I', d, p)[0], 0, 0))
    elif op == 14:
        sid, val = struct.unpack_from('<HB', d, p)
        out.append((14, 0, sid, 1 if val else 0, 0, 0))
    elif op == 21:
        obj, dist = struct.unpack_from('<B3xI', d, p)
        out.append((21, 0, obj, dist, 0, 0))
    elif op == 22:
        lo, hi = struct.unpack_from('<HH', d, p)
        out.append((22, 0, 0, lo, hi, 0))
    elif op == 23:          # CLAN QUESTS: has clan: b = 1 / 0
        out.append((23, 0, 0, 1 if d[p] else 0, 0, 0))
    elif op in (24, 25, 26, 27, 29):          # CLAN QUESTS: clan rank (24) / contribution (25) / level (26) / points (27) / member count (29): b = value (u16), c = cmp
        val, cmp_ = struct.unpack_from('<HB', d, p)
        out.append((op, 0, 0, val, cmp_, 0))
    elif op == 5:          # W18 DUNGEONS: Party (rose-offline QsdCondition::Party): sub = must be the leader, b = party level, c = cmp (2 >= / 3 <, the file byte 1 = less than)
        lead, lv, lt = struct.unpack_from('<B3xiB', d, p)
        out.append((5, 1 if lead else 0, 0, lv, 3 if lt else 2, 0))
    elif op == 28:          # CLAN QUESTS: clan money: b = value, c = cmp
        val, cmp_ = struct.unpack_from('<iB', d, p)
        out.append((28, 0, 0, val, cmp_, 0))
    elif op == 15:          # W20 QUEST: PartyMemberCount (rose-offline QsdCondition::PartyMemberCount): b = first count, c = last count
        lo, hi = struct.unpack_from('<HH', d, p)
        out.append((15, 0, 0, lo, hi, 0))
    elif op == 17:          # W20 QUEST: CompareNpcVariables: sub = cmp, a = variable of npc 1, b = npc 1, c = npc 2, d = variable of npc 2
        n1, v1, n2, v2, cmp_ = struct.unpack_from('<IHxxIHxxB', d, p)
        out.append((17, cmp_, v1, n1, n2, v2))
    elif op in (18, 19):    # W20 QUEST: MonthDayTime (18: sub = day of the month, 0 = any) / WeekDayTime (19: sub = day of the week, 0 = Sunday): b .. c = minutes of the day (hour x 60 + minute)
        day, h0, m0, h1, m1 = struct.unpack_from('<BBBBB', d, p)
        out.append((op, day, 0, h0 * 60 + m0, h1 * 60 + m1, 0))
    elif op == 30:          # CLAN QUESTS: clan skill in range: sub = has (1 / 0), b = first id, c = last id
        lo, hi, has = struct.unpack_from('<HHB', d, p)
        out.append((30, 1 if has else 0, 0, lo, hi, 0))
    else:
        warn['condition op %d: not supported (false)' % op] += 1
        out.append((255, op & 0xFF, 0, 0, 0, 0))
    return out


def _rew_records(op, d, p, size, warn):
    """One QSD reward record -> (normalised tuples, chained trigger name or None)."""
    out = []
    chain = None
    if op == 0:
        qid, sub = struct.unpack_from('<IB', d, p)
        out.append((0, sub, 0, qid, 0, 0))
    elif op == 1:
        sn, add, qty = struct.unpack_from('<IBxH', d, p)
        out.append((1, 1 if add else 0, 0, sn, qty, 0))
    elif op in (2, 4):
        n = _n(d, p, 8, size)
        for k in range(n):
            vid, vt, val, rop = struct.unpack_from('<HHhB', d, p + 4 + k * 8)
            out.append((2, (vt >> 8) & 0xFF, vid, val, rop, 0))
    elif op == 3:
        n = _n(d, p, 12, size)
        for k in range(n):
            ab, val, rop = struct.unpack_from('<IiB', d, p + 4 + k * 12)
            out.append((3, 0, ab & 0xFFFF, val, rop, 0))
    elif op == 5:
        typ, eqn, val, sn, _party, gem = struct.unpack_from('<BB2xiIBxH', d, p)
        out.append((5, typ, eqn, val, sn, gem))
    elif op == 6:
        hp, mp = struct.unpack_from('<ii', d, p)
        out.append((6, 0, 0, hp & 0xFF, mp & 0xFF, 0))
    elif op == 7:
        z, x, y = struct.unpack_from('<III', d, p)
        out.append((7, 0, z & 0xFFFF, x, y, 0))
    elif op == 9:
        chain, _ = _u16str(d, p)
        out.append((9, 0, 0, -1, 0, 0))
    elif op == 11:          # ZONES MORE: object variable reward: sub = object (0 npc 1 event), a = variable, b = value, c = reward op
        obj, vid, val, rop = struct.unpack_from('<BxHiB', d, p)
        out.append((11, obj, vid, val, rop, 0))
    elif op in (10, 17, 29, 30):
        out.append((op, 0, 0, 0, 0, 0))
    elif op == 23:          # QUEST CHECK: ClanLevelIncrease (no data): the runtime has no clan growth yet and fails it (docs/design/rose_quests_runtime.md)
        out.append((23, 0, 0, 0, 0, 0))
    elif op in (24, 25, 27):          # QUEST CHECK: clan money (24) / points (25) / contribution (27): b = value, c = reward op (5 set 6 add 7 sub 8 zero 9 one)
        val, rop = struct.unpack_from('<iB', d, p)
        out.append((op, 0, 0, val, rop, 0))
    elif op == 26:          # QUEST CHECK: AddClanSkill / RemoveClanSkill: sub = add (1) / remove (0), b = skill id
        sk, add = struct.unpack_from('<HB', d, p)
        out.append((26, 1 if add else 0, 0, sk, 0, 0))
    elif op == 12:
        typ, sid = struct.unpack_from('<B3xI', d, p)
        out.append((12, typ, 0, sid, 0, 0))
    elif op == 14:
        add, sk = struct.unpack_from('<B3xI', d, p)
        out.append((14, 1 if add else 0, 0, sk, 0, 0))
    elif op == 15:
        sid, val = struct.unpack_from('<HB', d, p)
        out.append((15, 0, sid, 1 if val else 0, 0, 0))
    elif op == 16:
        out.append((16, 0, struct.unpack_from('<H', d, p)[0], 0, 0, 0))
    elif op == 8:          # W18 DUNGEONS: SpawnMonster: sub = location (0 owner 1 selected npc 2 selected event 3 position), a = count, b = npc id, c / d = x / y cm (position), zone dropped
        npc, cnt, loc, _z, x, y = struct.unpack_from('<IIB3xIii', d, p)
        out.append((8, loc, min(cnt, 65535), npc, x, y))
    elif op == 20:          # W18 DUNGEONS: SetTeamNumber (sub = source 0 unique 1 clan 2 party)
        out.append((20, d[p], 0, 0, 0, 0))
    elif op == 21:          # W18 DUNGEONS: SetRevivePosition: b = x cm, c = y cm
        x, y = struct.unpack_from('<ii', d, p)
        out.append((21, 0, 0, x, y, 0))
    elif op == 28:          # CLAN QUESTS: teleport nearby clan members (and me): a = zone, b = x cm, c = y cm, d = distance (cm)
        dist, z, x, y = struct.unpack_from('<IH2xII', d, p)
        out.append((28, 0, z, x, y, dist))
    else:
        warn['reward op %d: not supported (fails)' % op] += 1
        out.append((255, op & 0xFF, 0, 0, 0, 0))
    return out, chain


def parse_qsd(data, warn):
    """QSD file bytes -> list of triggers (rose-offline qsd.rs read_irose / read_trigger): dict(name, conds, rews, chains, next_name). Records are walked by their size."""
    trigs = []
    _ver, ng = struct.unpack_from('<II', data, 0)
    if ng > MAX_COUNT:
        raise ValueError('QSD group count %d' % ng)
    _fn, p = _u16str(data, 8)
    for _g in range(ng):
        nt, = struct.unpack_from('<I', data, p)
        if nt > MAX_COUNT or nt * 9 > len(data) - p:
            raise ValueError('QSD trigger count %d' % nt)
        _gn, p = _u16str(data, p + 4)
        prev = None
        for _t in range(nt):
            check_next = data[p] != 0
            nc, nr = struct.unpack_from('<II', data, p + 1)
            if nc > MAX_COUNT or nr > MAX_COUNT or (nc + nr) * 8 > len(data) - p:
                raise ValueError('QSD record counts %d / %d' % (nc, nr))
            name, p = _u16str(data, p + 9)
            t = dict(name=name, conds=[], rews=[], chains=[], next_name=None)
            for _ in range(nc):
                size, op = struct.unpack_from('<II', data, p)
                if size < 8 or size > len(data) - p:
                    raise ValueError('QSD condition size %d' % size)
                t['conds'] += _cond_records(op & 0xFFFF, data, p + 8, size, warn)
                p += size
            for _ in range(nr):
                size, op = struct.unpack_from('<II', data, p)
                if size < 8 or size > len(data) - p:
                    raise ValueError('QSD reward size %d' % size)
                recs, chain = _rew_records(op & 0xFFFF, data, p + 8, size, warn)
                if chain is not None:
                    t['chains'].append((len(t['rews']), chain))
                t['rews'] += recs
                p += size
            if prev is not None:
                prev['next_name'] = name
            prev = t if check_next else None
            trigs.append(t)
    return trigs


# =============================================================== CON
def _xor_key(a, b):
    return a if a & 1 else b


def parse_con(data):
    """CON bytes (rose-offline con_.rs) -> dict(initial=[msg], menus=[[msg]], script=bytes). msg = dict(type, value, cond, act, sid)."""
    conv_off, script_off, n_msgs, msgs_rel, n_menus, menus_rel = struct.unpack_from('<6I', data, 516)
    if n_msgs > MAX_COUNT or n_menus > MAX_COUNT or n_msgs * 4 > len(data) or n_menus * 4 > len(data):
        raise ValueError('CON counts %d / %d' % (n_msgs, n_menus))
    out = dict(initial=[], menus=[], script=b'')
    mb = conv_off + msgs_rel
    for i in range(n_msgs):
        m = mb + struct.unpack_from('<I', data, mb + 4 * i)[0]
        _id, typ, val = struct.unpack_from('<IIi', data, m)
        cond = data[m + 12:m + 44].split(b'\0')[0].decode('cp949', 'replace')
        act = data[m + 44:m + 76].split(b'\0')[0].decode('cp949', 'replace')
        sid, = struct.unpack_from('<I', data, m + 76)
        out['initial'].append(dict(type=typ, value=val, cond=cond, act=act, sid=sid))
    nb = conv_off + menus_rel
    for i in range(n_menus):
        mo = nb + struct.unpack_from('<I', data, nb + 4 * i)[0]
        size, count = struct.unpack_from('<II', data, mo)
        if count > MAX_COUNT or count * 4 > len(data) - mo:
            raise ValueError('CON menu count %d' % count)
        k8 = _xor_key(count & 0xFF, size & 0xFF)
        k32 = k8 * 0x01010101
        msgs = []
        for j in range(count):
            m = mo + (struct.unpack_from('<I', data, mo + 8 + 4 * j)[0] ^ k32)
            _id, typ, val = [v ^ k32 for v in struct.unpack_from('<III', data, m)]
            if val >= 0x80000000:
                val -= 1 << 32
            cond = bytes(b ^ k8 for b in data[m + 12:m + 44]).split(b'\0')[0].decode('cp949', 'replace')
            act = bytes(b ^ k8 for b in data[m + 44:m + 76]).split(b'\0')[0].decode('cp949', 'replace')
            sid = struct.unpack_from('<I', data, m + 76)[0] ^ k32
            msgs.append(dict(type=typ, value=val, cond=cond, act=act, sid=sid))
        out['menus'].append(msgs)
    ssz, = struct.unpack_from('<I', data, script_off)
    k = _xor_key(ssz & 0xFF, len(data) & 0xFF)
    out['script'] = bytes(b ^ k for b in data[script_off + 4:script_off + 4 + ssz])
    return out


# =============================================================== Lua 4.0 bytecode (lundump.c 4.0 layout, lopcodes.h 4.0 opcode order)
LUA_OPS = ('END RETURN CALL TAILCALL PUSHNIL POP PUSHINT PUSHSTRING PUSHNUM PUSHNEGNUM PUSHUPVALUE GETLOCAL GETGLOBAL GETTABLE GETDOTTED GETINDEXED '
           'PUSHSELF CREATETABLE SETLOCAL SETGLOBAL SETTABLE SETLIST SETMAP ADD ADDI SUB MULT DIV POW CONCAT MINUS NOT JMPNE JMPEQ JMPLT JMPLE JMPGT JMPGE '
           'JMPT JMPF JMPONT JMPONF JMP PUSHNILJMP FORPREP FORLOOP LFORPREP LFORLOOP CLOSURE').split()
OP = {n: i for i, n in enumerate(LUA_OPS)}
MAXARG_S = (1 << 25) - 1
MULT_RET = 255


class _LR:
    def __init__(s, d, p):
        s.d, s.p = d, p
        s.isz = 4
        s.ssz = 4

    def int_(s):
        v, = struct.unpack_from('<i', s.d, s.p); s.p += 4; return v

    def size(s):
        v, = struct.unpack_from('<I', s.d, s.p); s.p += 4; return v

    def byte(s):
        v = s.d[s.p]; s.p += 1; return v

    def num(s):
        v, = struct.unpack_from('<d', s.d, s.p); s.p += 8; return v

    def str_(s):
        n = s.size()
        if n == 0:
            return None
        v = s.d[s.p:s.p + n - 1]; s.p += n
        return v.decode('cp949', 'replace')


def _lua_fn(r, depth=0):
    if depth > 8:
        raise ValueError('Lua: functions nested too deep')
    f = dict(src=r.str_(), line=r.int_(), nparams=r.int_(), vararg=r.byte(), maxstack=r.int_())
    f['locals'] = [(r.str_(), r.int_(), r.int_()) for _ in range(r.int_())]
    for _ in range(r.int_()):
        r.int_()
    f['ks'] = [r.str_() for _ in range(r.int_())]
    f['kn'] = [r.num() for _ in range(r.int_())]
    f['kp'] = [_lua_fn(r, depth + 1) for _ in range(r.int_())]
    n = r.int_()
    f['code'] = list(struct.unpack_from('<%dI' % n, r.d, r.p)); r.p += 4 * n
    return f


def lua_load(data):
    """Lua 4.0 precompiled chunk -> main function dict. Header: ESC 'Lua' 0x40, endian 1, int/size_t/Instruction 4, 32/6/9 instruction bits, Number 8, test number."""
    if data[:5] != b'\x1bLua\x40':
        raise ValueError('not a Lua 4.0 chunk')
    if tuple(data[5:13]) != (1, 4, 4, 4, 32, 6, 9, 8):
        raise ValueError('Lua 4.0 chunk with an unsupported layout')
    r = _LR(data, 13 + 8)
    return _lua_fn(r)


def lua_globals(main):
    """Top-level `function NAME() ... end` definitions: CLOSURE k + SETGLOBAL name -> {name: proto}."""
    out = {}
    code = main['code']
    for pc in range(len(code) - 1):
        if code[pc] & 63 == OP['CLOSURE'] and code[pc + 1] & 63 == OP['SETGLOBAL']:
            k = code[pc] >> 15
            if k < len(main['kp']):
                out[main['ks'][code[pc + 1] >> 6]] = main['kp'][k]
    return out


JUMP_CMP = {'JMPNE': CMP_NE, 'JMPEQ': CMP_EQ, 'JMPLT': CMP_LT, 'JMPLE': CMP_LE, 'JMPGT': CMP_GT, 'JMPGE': CMP_GE}
NEG_CMP = {CMP_EQ: CMP_NE, CMP_NE: CMP_EQ, CMP_LT: CMP_GE, CMP_GE: CMP_LT, CMP_LE: CMP_GT, CMP_GT: CMP_LE}
SWAP_CMP = {CMP_EQ: CMP_EQ, CMP_NE: CMP_NE, CMP_LT: CMP_GT, CMP_GT: CMP_LT, CMP_LE: CMP_GE, CMP_GE: CMP_LE}


def _cmp(c, a, b):
    return {CMP_EQ: a == b, CMP_NE: a != b, CMP_LT: a < b, CMP_LE: a <= b, CMP_GT: a > b, CMP_GE: a >= b}[c]


class FnCompiler:
    """Symbolic execution of one Lua 4 function into decision nodes.

    Values on the symbolic stack: ('int', n) ('str', s) ('nil',) ('call', id, args) (a pure query result) ('opaque',) (anything we do not model, e.g. the event owner).
    Conditional jumps that compare a call result with a constant become test nodes (both branches are followed); constant comparisons are folded. A backward jump
    (a loop) or an unknown opcode makes the function unsupported (ValueError). Effect calls (QF_doQuestTrigger, GF_openStore ...) become effect nodes in program order."""

    def __init__(s, fn, trig_index, warn, name, assigned=None):
        s.fn, s.tix, s.warn, s.name = fn, trig_index, warn, name
        s.assigned = assigned if assigned is not None else None          # QUEST CHECK: every global the script assigns anywhere (None = unknown: nothing is folded to nil)
        s.nodes = []
        s.memo = {}
        s.refs = set()            # trigger names this function names

    def compile(s):
        stack = [('opaque',)] * s.fn['nparams']
        return s._run(0, tuple(stack), 0, ())

    def _nilg(s, v):
        return v[0] == 'global' and v[1] not in LUA_CONSTANTS and v[1] not in s.assigned

    def _node(s, kind, call=0, cmp_=0, a0=0, a1=0, k=0, nxt=-1, alt=-1):
        if len(s.nodes) >= MAX_NODES_PER_FN:
            raise ValueError('too many dialog nodes')
        s.nodes.append([kind, call, cmp_, a0, a1, k, nxt, alt])
        return len(s.nodes) - 1

    def _callval(s, fval, args):
        """Resolves a call: -> ('call', id, (a0, a1)) or ('opaque',) for ignored functions. Raises ValueError for unknown functions or arguments."""
        if fval[0] != 'global':
            raise ValueError('call of a non-global')
        nm = fval[1]
        if nm in IGNORED_CALLS:
            return ('opaque',)
        cid = CALL_IDS.get(nm)
        if cid is None:
            s.warn['dialog: unknown Lua function %s (call ignored)' % nm] += 1
            return ('opaque',)
        a = []
        if cid in (10, 11) and args and args[0][0] == 'call' and args[0][1] == 3:          # ZONES MORE: QF_getQuestVar(QF_findQuest(q), v): by quest id
            cid = CALL_QVAR_ID if cid == 10 else CALL_QSW_ID
            args = [('int', args[0][2][0])] + list(args[1:])
        for i, v in enumerate(args[:2]):
            if v[0] == 'int':
                a.append(v[1])
            elif v[0] == 'str' and cid in TRIGGER_ARG_CALLS and i == 0:
                s.refs.add(v[1])
                a.append(s.tix.get(tkey(v[1]), -1))
                if a[-1] < 0:
                    s.warn['dialog: trigger %s not in any QSD' % v[1]] += 1
            elif v[0] == 'global' and v[1] in LUA_CONSTANTS:
                a.append(LUA_CONSTANTS[v[1]])
            elif v[0] == 'opaque':
                a.append(0)
            else:
                raise ValueError('unsupported argument %r to %s' % (v, nm))
        while len(a) < 2:
            a.append(0)
        return ('call', cid, tuple(a))

    def _run(s, pc, stack, depth, gl=()):
        """gl: the Lua globals this function assigned so far, ((name, value), ...) sorted (ZONES MORE: `npcVar0 = QF_getNpcQuestZeroVal(...)`; part of the memo key)."""
        key = (pc, stack, gl)
        if key in s.memo:
            return s.memo[key]
        if depth > 400:
            raise ValueError('function too deep')
        code = s.fn['code']
        ks = s.fn['ks']
        st = list(stack)
        start_node = None
        prev_effect = None

        def link(nid):
            nonlocal start_node, prev_effect
            if start_node is None:
                start_node = nid
            if prev_effect is not None:
                s.nodes[prev_effect][6] = nid
            prev_effect = None

        while True:
            if pc >= len(code):
                nid = s._node(NK_RET, k=0); link(nid); break
            ins = code[pc]
            o = LUA_OPS[ins & 63] if (ins & 63) < len(LUA_OPS) else '?'
            u = ins >> 6
            sarg = u - MAXARG_S
            a = ins >> 15
            b = (ins >> 6) & 0x1FF
            pc += 1
            if o == 'END':
                nid = s._node(NK_RET, k=0); link(nid); break
            if o == 'RETURN':
                vals = st[u:]
                v = vals[0] if vals else ('nil',)
                if v[0] == 'int':
                    nid = s._node(NK_RET, k=v[1])
                elif v[0] == 'call':
                    nid = s._node(NK_RETCALL, call=v[1], a0=v[2][0], a1=v[2][1])
                elif v[0] in ('nil', 'opaque'):
                    nid = s._node(NK_RET, k=0)
                else:
                    raise ValueError('return of %r' % (v,))
                link(nid); break
            if o == 'PUSHNIL':
                st += [('nil',)] * u
            elif o == 'POP':
                del st[len(st) - u:]
            elif o == 'PUSHINT':
                st.append(('int', sarg))
            elif o == 'PUSHSTRING':
                st.append(('str', ks[u]))
            elif o == 'PUSHNUM':
                v = s.fn['kn'][u]
                st.append(('int', int(v)) if _int32(v) else ('opaque',))          # audit fix: only numbers that fit an int32 (the runtime field)
            elif o == 'PUSHNEGNUM':
                v = -s.fn['kn'][u]
                st.append(('int', int(v)) if _int32(v) else ('opaque',))
            elif o == 'GETLOCAL':
                st.append(st[u])
            elif o == 'SETLOCAL':
                st[u] = st.pop()
            elif o == 'GETGLOBAL':
                gv = dict(gl).get(ks[u])
                st.append(gv if gv is not None else ('global', ks[u]))
            elif o == 'SETGLOBAL':
                gd = dict(gl)
                gd[ks[u]] = st.pop()
                gl = tuple(sorted(gd.items(), key=lambda kv: kv[0]))
            elif o == 'CALL':
                fval, args = st[a], st[a + 1:]
                del st[a:]
                cv = s._callval(fval, args)
                if cv[0] == 'call' and cv[1] in EFFECT_CALLS:
                    nid = s._node(NK_EFFECT, call=cv[1], a0=cv[2][0], a1=cv[2][1])
                    link(nid)
                    prev_effect = nid
                    cv = ('opaque',)        # an effect's result is not modelled (the original only shows it in the chat)
                if b == MULT_RET:
                    st.append(cv)
                else:
                    st += [cv] + [('nil',)] * (b - 1) if b > 0 else []
            elif o in JUMP_CMP:
                rhs, lhs = st.pop(), st.pop()
                tgt = pc + sarg
                if sarg < 0:
                    raise ValueError('backward jump (loop)')
                c = JUMP_CMP[o]
                # QUEST CHECK: a global the whole script never assigns (and no SV_* constant) is nil in Lua: nil == n is false, nil ~= n is true (an ordered compare is a Lua error: unsupported)
                if s.assigned is not None and ((s._nilg(lhs) and rhs[0] == 'int') or (s._nilg(rhs) and lhs[0] == 'int')):
                    if c not in (CMP_EQ, CMP_NE):
                        raise ValueError('ordered compare of an unassigned global')
                    if c == CMP_NE:
                        pc = tgt
                    continue
                if lhs[0] == 'int' and rhs[0] == 'int':
                    if _cmp(c, lhs[1], rhs[1]):
                        pc = tgt
                    continue
                if lhs[0] == 'int' and rhs[0] == 'call':
                    lhs, rhs, c = rhs, lhs, SWAP_CMP[c]
                if lhs[0] != 'call' or rhs[0] != 'int':
                    raise ValueError('compare of %r with %r' % (lhs, rhs))
                nid = s._node(NK_TEST, call=lhs[1], cmp_=c, a0=lhs[2][0], a1=lhs[2][1], k=rhs[1])
                link(nid)
                yes = s._run(tgt, tuple(st), depth + 1, gl)
                no = s._run(pc, tuple(st), depth + 1, gl)
                s.nodes[nid][6], s.nodes[nid][7] = yes, no
                break
            elif o in ('JMPT', 'JMPF', 'JMPONT', 'JMPONF'):
                v = st.pop()
                truth = v[0] not in ('nil',)          # Lua 4: only nil (and no false) is false; numbers, strings and call results are true
                jump = truth if o in ('JMPT', 'JMPONT') else not truth
                if sarg < 0:
                    raise ValueError('backward jump (loop)')
                if jump:
                    if o in ('JMPONT', 'JMPONF'):
                        st.append(v)
                    pc = pc + sarg
            elif o == 'JMP':
                if sarg < 0:
                    raise ValueError('backward jump (loop)')
                pc = pc + sarg
            elif o == 'NOT':
                v = st.pop()
                st.append(('int', 1) if v[0] == 'nil' else ('nil',))
            else:
                raise ValueError('Lua op %s not supported' % o)
        s.memo[key] = start_node
        return start_node


def _int32(v):
    try:
        return v == v and v not in (float('inf'), float('-inf')) and v == int(v) and -2 ** 31 <= int(v) < 2 ** 31
    except (OverflowError, ValueError):
        return False


def compile_dialog_functions(script, names, trig_index, warn):
    """CON Lua blob + the function names its messages use -> ({name: (root, nodes)}, referenced trigger names). Unsupported functions are left out (warning)."""
    main = lua_load(script)
    glob = lua_globals(main)
    assigned = set()          # QUEST CHECK: every SETGLOBAL name of the script (functions defined with `function f` included)

    def _walk(fn):
        for ins in fn['code']:
            if ins & 63 == OP['SETGLOBAL']:
                assigned.add(fn['ks'][ins >> 6])
        for q in fn['kp']:
            _walk(q)
    _walk(main)
    out = {}
    refs = set()
    for nm in sorted(names):
        if not nm:
            continue
        proto = glob.get(nm)
        if proto is None:
            warn['dialog: function %s not defined in the script' % nm] += 1
            continue
        c = FnCompiler(proto, trig_index, warn, nm, assigned)
        try:
            root = c.compile()
        except Exception as e:          # audit fix: any failure of one function leaves only that function out
            warn['dialog: function %s unsupported (%s: %s)' % (nm, e.__class__.__name__, str(e)[:70])] += 1
            continue
        out[nm] = (root, c.nodes)
        refs |= c.refs
    return out, refs


# =============================================================== text tables
def ltb_strings(data, column):
    """LTB (rose-offline ltb.rs): u32 columns, u32 rows, rows x columns x (u32 offset, u16 chars), UTF-16 data. -> {row: text} for one column."""
    cols, rows = struct.unpack_from('<II', data, 0)
    base = 8 + rows * cols * 6
    out = {}
    if column >= cols:
        return out
    for r in range(rows):
        off, n = struct.unpack_from('<IH', data, 8 + (r * cols + column) * 6)
        if n == 0 or off < base or off + 2 * n > len(data):
            continue
        out[r] = data[off:off + 2 * n].decode('utf-16-le', 'replace')
    return out


class StrBlob:
    """UTF-16 string blob. add(text, ja): ja = the Japanese text of the same line (language overlay TXJA, domain D_QSTR keyed by the offset of the English string);
    two lines with the same English text and different Japanese text get two blob entries, so every line keeps its own translation."""

    def __init__(s):
        s.buf = bytearray(); s.index = {}

    def add(s, text, ja=None):
        if text is None:
            return -1
        text = text[:4000]
        ja = ja[:4000] if ja and ja != text else None
        key = (text, ja)
        if key in s.index:
            return s.index[key]
        off = len(s.buf)
        u = text.encode('utf-16-le')
        s.buf += struct.pack('<H', len(u) // 2) + u
        s.buf += b'\0' * (-len(s.buf) % 4)
        s.index[key] = off
        if ja:
            TX.note(TX.D_QSTR, off, 0, text, ja)
        return off


# =============================================================== zone build
def _stb(vfs, path):
    try:
        return F.stb(vfs.read(path))
    except (KeyError, AssertionError, struct.error, IndexError):
        return None


def load_triggers(vfs, warn):
    """All QSD triggers of LIST_QUESTDATA -> (list, {upper name: index}). Unsupported ops are counted later, for the triggers a zone keeps."""
    scratch = collections.Counter()
    qlist = _stb(vfs, '3DDATA/STB/LIST_QUESTDATA.STB')
    trigs = []
    if qlist is None:
        warn['QUST: LIST_QUESTDATA.STB missing'] += 1
        return trigs, {}
    for row in qlist['cells']:
        path = row[0].strip() if row else ''
        if not path:
            continue
        try:
            trigs += parse_qsd(vfs.read(path), scratch)
        except (KeyError, ValueError, struct.error, IndexError):
            warn['QUST: QSD unreadable'] += 1
    index = {}
    for i, t in enumerate(trigs):
        k = tkey(t['name'])
        if k in index:
            warn['QUST: duplicate trigger name (first kept)'] += 1
            continue
        index[k] = i
    return trigs, index


def quest_table(vfs, warn, lang=TX.EN):
    """LIST_QUEST.STB + LIST_QUEST_S.STL -> {quest id: (name, desc, start, end, time limit)} in the STL language `lang` (1 English, 2 Japanese; rose_text.pick fallbacks)."""
    qs = _stb(vfs, '3DDATA/STB/LIST_QUEST.STB')
    out = {}
    if qs is None:
        if lang == TX.EN:
            warn['QUST: LIST_QUEST.STB missing'] += 1
        return out
    keys = langs = None
    try:
        _typ, keys, langs = F.stl(vfs.read('3DDATA/STB/LIST_QUEST_S.STL'))
    except (KeyError, ValueError, struct.error, IndexError):
        if lang == TX.EN:
            warn['QUST: LIST_QUEST_S.STL missing (no quest texts)'] += 1
    for qid, c in enumerate(qs['cells']):
        if not c:
            continue
        sid = c[-1].strip()
        if not sid:
            continue
        try:
            tl = int(c[1].strip() or '0') if len(c) > 1 else 0
        except ValueError:
            tl = 0
        txt = (None, None, None, None)
        if keys and sid in keys and len(langs) > STL_LANG:
            stl = (_typ, keys, langs)
            txt = tuple(TX.stl_text(stl, sid, k, lang) if TX.stl_cells(stl, sid, k) else None for k in range(4))
        out[qid] = txt + (max(0, tl),)
    return out


def _zone_npcs(ifos):
    """IFO block 2 NPCs and block 12 event objects -> ({npc id: conversation key}, [event trigger names]). ifos: {block: rosefmt.ifo dict} (rose_pack's window)."""
    npcs = {}
    events = []
    for _b, o in sorted(ifos.items()):
        for n in o.get(2, []) or []:
            if n.get('quest'):
                npcs.setdefault(n['id'], n['quest'])
        for e in o.get(12, []) or []:
            if e.get('trig'):
                events.append(e['trig'])
    return npcs, events


def build_section(vfs, ifos, monster_ids, warn=None):
    """-> (sections, info). ifos: {block: parsed IFO} of the converted window (rose_pack), monster_ids: monster NPC ids of the zone's spawns.
    No data (no QSD / no NPC conversation) -> no section. Never raises (audit fix): any failure = no section + a warning."""
    warn = warn if warn is not None else collections.Counter()
    try:
        return _build_section(vfs, ifos, monster_ids, warn)
    except Exception as e:
        warn['QUST: not built (%s: %s)' % (e.__class__.__name__, e)] += 1
        return [], dict(warnings=dict(warn))


def _build_section(vfs, ifos, monster_ids, warn):
    trigs, tix = load_triggers(vfs, warn)
    if not trigs:
        return [], dict(warnings=dict(warn))
    npcs, events = _zone_npcs(ifos)
    ev = _stb(vfs, '3DDATA/STB/LIST_EVENT.STB')
    ev_index = {}
    if ev is not None:
        for i, k in enumerate(ev['keys']):
            c = ev['cells'][i] if i < len(ev['cells']) else []
            if len(c) > 3 and c[3].strip():
                ev_index[k.strip().upper()] = c[3].strip()
    ltb = {}
    ltb_ja = {}
    try:
        ltb = ltb_strings(vfs.read(LTB_CON), LTB_COLUMN)
        ltb_ja = ltb_strings(vfs.read(LTB_CON), TX.LTB_JA)
    except (KeyError, struct.error):
        warn['QUST: %s missing (no dialog texts)' % LTB_CON] += 1
    # --- NPC conversations
    conv = []          # (npc id, con dict, {fn name: (root, nodes)})
    roots = set()
    for nid in sorted(npcs):
        path = ev_index.get(npcs[nid].strip().upper())
        if path is None:
            warn['QUST: conversation key of an NPC not in LIST_EVENT'] += 1
            continue
        try:
            con = parse_con(vfs.read(path))
        except (KeyError, struct.error, IndexError, ValueError):
            warn['QUST: CON unreadable'] += 1
            continue
        names = set()
        if con['initial']:
            names.add(con['initial'][0]['cond'])
        for m in con['menus']:
            for x in m:
                names.add(x['cond']); names.add(x['act'])
        try:
            fns, refs = compile_dialog_functions(con['script'], names, tix, warn)
        except (ValueError, struct.error, IndexError) as e:
            warn['QUST: CON script unreadable (%s)' % e.__class__.__name__] += 1
            fns, refs = {}, set()
        roots |= {tix[tkey(r)] for r in refs if tkey(r) in tix}
        conv.append((nid, con, fns))
    # --- other entry points: monster death, level up, event objects
    npc_stb = _stb(vfs, '3DDATA/STB/LIST_NPC.STB')
    death = []
    for mid in sorted(set(monster_ids)):
        if npc_stb is None or mid >= npc_stb['rows']:
            continue
        c = npc_stb['cells'][mid]
        tn = c[41].strip() if len(c) > 41 else ''
        if tn and tkey(tn) in tix:
            death.append((mid, tix[tkey(tn)])); roots.add(tix[tkey(tn)])
        elif tn:
            warn['QUST: monster death trigger not in any QSD'] += 1
    levelup = []
    for k, i in tix.items():
        if k.startswith('LEVELUP_'):
            try:
                lv = int(k[8:])
            except ValueError:
                continue
            levelup.append((lv, i)); roots.add(i)
    evt = []
    for ei, tn in enumerate(events):
        if tkey(tn) in tix:
            evt.append((ei, tix[tkey(tn)])); roots.add(tix[tkey(tn)])
    # --- closure over chains (next on failure, Trigger rewards)
    seen = set()
    stack = list(roots)
    while stack:
        i = stack.pop()
        if i in seen:
            continue
        seen.add(i)
        t = trigs[i]
        if t['next_name'] and tkey(t['next_name']) in tix:
            stack.append(tix[tkey(t['next_name'])])
        for _ri, nm in t['chains']:
            if tkey(nm) in tix:
                stack.append(tix[tkey(nm)])
            else:
                warn['QUST: chained trigger name not in any QSD'] += 1
    order = sorted(seen)
    remap = {g: n for n, g in enumerate(order)}
    for g in order:
        for c in trigs[g]['conds']:
            if c[0] == 255:
                warn['QUST: reachable condition op %d not supported (false)' % c[1]] += 1
        for r in trigs[g]['rews']:
            if r[0] == 255:
                warn['QUST: reachable reward op %d not supported (fails)' % r[1]] += 1
    if not conv and not order:
        return [], dict(warnings=dict(warn))
    sb = StrBlob()
    T, C, R = [], [], []
    quest_ids = set()
    for g in order:
        t = trigs[g]
        rews = [list(r) for r in t['rews']]
        for ri, nm in t['chains']:
            rews[ri][3] = remap.get(tix.get(tkey(nm), -1), -1)
        for r in t['rews']:
            if r[0] == 0:
                quest_ids.add(r[3])
        for c in t['conds']:
            if c[0] == 0:
                quest_ids.add(c[3])
        nxt = remap.get(tix.get(tkey(t['next_name']), -1), -1) if t['next_name'] else -1
        T.append((len(C), len(t['conds']), len(rews), len(R), nxt, sb.add(t['name'])))
        C += t['conds']
        R += [tuple(r) for r in rews]
    # --- dialogs
    NP, ME, MS, FNS, NO = [], [], [], [], []
    for nid, con, fns in conv:
        fidx = {}
        for nm, (root, nodes) in sorted(fns.items()):
            base = len(NO)
            for n in nodes:
                kind, call, cmp_, a0, a1, k, nx, al = n
                if call in TRIGGER_ARG_CALLS:
                    a0 = remap.get(a0, -1)
                NO.append((kind, call, cmp_, 0, a0, a1, k, nx + base if nx >= 0 else -1, al + base if al >= 0 else -1))
            fidx[nm] = len(FNS)
            FNS.append((root + base if root is not None else -1, sb.add(nm)))
        open_fn = fidx.get(con['initial'][0]['cond'], -1) if con['initial'] and con['initial'][0]['cond'] else -1
        if con['initial'] and con['initial'][0]['cond'] and open_fn < 0:
            open_fn = -2           # named but unsupported: the dialog never opens (like a failing check)
        first = len(ME)
        for m in con['menus']:
            ME.append((len(MS), len(m)))
            for x in m:
                cf = fidx.get(x['cond'], -2) if x['cond'] else -1
                af = fidx.get(x['act'], -2) if x['act'] else -1
                MS.append((x['type'], x['value'], cf, af, sb.add(ltb.get(x['sid']), ltb_ja.get(x['sid']))))
        NP.append((nid, open_fn, first, len(con['menus']), -1, 0))
    qt = quest_table(vfs, warn)
    qj = quest_table(vfs, collections.Counter(), TX.JA)
    Q = []
    for qid in sorted(quest_ids):
        if qid in qt:
            nm, de, st_, en, tl = qt[qid]
            j = qj.get(qid, (None,) * 5)
            Q.append((qid, sb.add(nm, j[0]), sb.add(de, j[1]), sb.add(st_, j[2]), sb.add(en, j[3]), tl))
    QN = [(qid, sb.add(v[0], qj.get(qid, (None,))[0])) for qid, v in sorted(qt.items()) if v[0]]
    QI = quest_items(vfs, quest_item_codes(trigs, order), sb, warn)
    tables = dict(trig=[TRIG.pack(*x) for x in T], cond=[REC.pack(*x) for x in C], rew=[REC.pack(*x) for x in R], npc=[NPC.pack(*x) for x in NP],
                  menu=[MENU.pack(*x) for x in ME], msg=[MSG.pack(*x) for x in MS], fn=[FN.pack(*x) for x in FNS], node=[NODE.pack(*x) for x in NO],
                  quest=[QUEST.pack(*x) for x in Q], levelup=[PAIR.pack(lv, remap[i]) for lv, i in sorted(levelup)],
                  death=[PAIR.pack(m, remap[i]) for m, i in death], event=[PAIR.pack(e, remap[i]) for e, i in evt], qname=[PAIR.pack(q, s_) for q, s_ in QN],
                  qitem=[QITEM.pack(c, ic, 0, s_) for c, ic, s_ in QI])
    data = enc_qust(tables, bytes(sb.buf))
    info = dict(triggers=len(T), conditions=len(C), rewards=len(R), npcs=len(NP), menus=len(ME), messages=len(MS), functions=len(FNS), nodes=len(NO),
                quests=len(Q), quest_names=len(QN), quest_items=len(QI), levelup=len(levelup), death=len(death), events=len(evt), string_bytes=len(sb.buf), bytes=len(data),
                warnings=dict(warn))
    return [(TAG_QUST, 1, len(T), data)], info


def sn_code(sn):
    """QSD item serial number -> item code type * 1000 + number (rose-offline QsdItem::from_sn), 0 = invalid / not representable."""
    if sn <= 0:
        return 0
    num, typ = (sn % 1000, sn // 1000) if sn < 1000000 else (sn % 1000000, sn // 1000000)
    return typ * 1000 + num if 0 < num < 1000 and 0 < typ <= 14 else 0


def quest_item_codes(trigs, order):
    """Quest item codes (type 13) the kept triggers name (item conditions, item and calculated-item rewards)."""
    out = set()
    for g in order:
        for c in trigs[g]['conds']:
            if c[0] == 4:
                out.add(sn_code(c[3]))
        for r in trigs[g]['rews']:
            if r[0] == 1:
                out.add(sn_code(r[3]))
            elif r[0] == 5 and r[1] == 2:
                out.add(sn_code(r[4]))
    return sorted(c for c in out if c // 1000 == 13)


def quest_items(vfs, codes, sb, warn):
    """LIST_QUESTITEM.STB (cell 9 icon, last cell string key) + LIST_QUESTITEM_S.STL (language 1) -> [(code, icon, name string)]."""
    stb = _stb(vfs, '3DDATA/STB/LIST_QUESTITEM.STB')
    if stb is None:
        if codes:
            warn['QUST: LIST_QUESTITEM.STB missing (quest items without icon / name)'] += 1
        return []
    keys = langs = None
    try:
        _t, keys, langs = F.stl(vfs.read('3DDATA/STB/LIST_QUESTITEM_S.STL'))
    except (KeyError, ValueError, struct.error, IndexError):
        pass
    out = []
    for code in codes:
        n = code % 1000
        if n >= stb['rows']:
            continue
        c = stb['cells'][n]
        try:
            icon = int(c[9].strip() or '0') if len(c) > 9 else 0
        except ValueError:
            icon = 0
        sid = c[-1].strip() if c else ''
        nm = nj = None
        if keys and sid in keys and len(langs) > STL_LANG:
            cells = TX.stl_cells((_t, keys, langs), sid, 0)
            nm, nj = TX.pick(cells, TX.EN), TX.pick(cells, TX.JA)
            TX.note(TX.D_ITEM, code, 0, nm, nj)
        out.append((code, max(0, min(icon, 0xFFFF)), sb.add(nm, nj)))
    return out


def check(dec):
    """Index check of a decoded section (every reference inside its table, strings inside the blob) -> list of errors (rose_pack_check)."""
    T, S = dec['tables'], dec['strings']
    err = []
    n = {k: len(v) for k, v in T.items()}

    def sref(o):
        return o == -1 or (0 <= o and o + 2 <= len(S) and o + 2 + 2 * struct.unpack_from('<H', S, o)[0] <= len(S))
    for i, t in enumerate(T['trig']):
        if t[0] + t[1] > n['cond'] or t[3] + t[2] > n['rew'] or not -1 <= t[4] < n['trig'] or not sref(t[5]):
            err.append('QUST trigger %d: bad index' % i)
            break
    for r in T['rew']:
        if r[0] == 9 and not -1 <= r[3] < n['trig']:
            err.append('QUST chain reward: bad trigger index')
            break
    for i, p in enumerate(T['npc']):
        if p[2] + p[3] > n['menu'] or not -2 <= p[1] < n['fn']:
            err.append('QUST npc %d: bad menu / open function' % i)
            break
    for m in T['menu']:
        if m[0] + m[1] > n['msg']:
            err.append('QUST menu: bad message range')
            break
    for m in T['msg']:
        if not -2 <= m[2] < n['fn'] or not -2 <= m[3] < n['fn'] or not sref(m[4]):
            err.append('QUST message: bad function / text')
            break
    for f in T['fn']:
        if not -1 <= f[0] < n['node']:
            err.append('QUST function: bad root node')
            break
    for x in T['node']:
        if not -1 <= x[7] < n['node'] or not -1 <= x[8] < n['node'] or x[0] > NK_TEST:
            err.append('QUST node: bad link')
            break
    for t in ('levelup', 'death', 'event'):
        for p in T[t]:
            if not 0 <= p[1] < n['trig']:
                err.append('QUST %s: bad trigger index' % t)
                break
    return err


def enc_qust(tables, strings):
    """tables: {name: [packed records]} for TABLES; strings: blob. -> section bytes."""
    hdr = bytearray(HDR_BYTES)
    body = bytearray()
    pairs = []
    for t in TABLES:
        recs = tables.get(t, [])
        off = HDR_BYTES + len(body)
        body += b''.join(recs)
        body += b'\0' * (-len(body) % 4)
        pairs.append((len(recs), off))
    str_off = HDR_BYTES + len(body)
    body += strings
    body += b'\0' * (-len(body) % 4)
    struct.pack_into('<I', hdr, 0, VERSION)
    for i, (n, o) in enumerate(pairs):
        struct.pack_into('<II', hdr, 4 + 8 * i, n, o)
    struct.pack_into('<II', hdr, 4 + 8 * 14, str_off, len(strings))
    return bytes(hdr + body)


REC_SIZES = dict(trig=TRIG.size, cond=REC.size, rew=REC.size, npc=NPC.size, menu=MENU.size, msg=MSG.size, fn=FN.size, node=NODE.size, quest=QUEST.size,
                 levelup=PAIR.size, death=PAIR.size, event=PAIR.size, qname=PAIR.size, qitem=QITEM.size)
STRUCTS = dict(trig=TRIG, cond=REC, rew=REC, npc=NPC, menu=MENU, msg=MSG, fn=FN, node=NODE, quest=QUEST, levelup=PAIR, death=PAIR, event=PAIR, qname=PAIR, qitem=QITEM)


def dec_qust(d):
    """section bytes -> dict(version, tables={name: [tuples]}, strings=bytes). Raises ValueError on bad bounds (same rules as GsvrRoseQuest.Valid)."""
    if len(d) < HDR_BYTES:
        raise ValueError('QUST: too short')
    ver, = struct.unpack_from('<I', d, 0)
    if ver != VERSION:
        raise ValueError('QUST: version %d' % ver)
    out = dict(version=ver, tables={})
    for i, t in enumerate(TABLES):
        n, o = struct.unpack_from('<II', d, 4 + 8 * i)
        sz = REC_SIZES[t]
        if o < HDR_BYTES or o + n * sz > len(d) or n > 1 << 20:
            raise ValueError('QUST: table %s out of bounds' % t)
        out['tables'][t] = [STRUCTS[t].unpack_from(d, o + k * sz) for k in range(n)]
    so, sl = struct.unpack_from('<II', d, 4 + 8 * 14)
    if so < HDR_BYTES or so + sl > len(d):
        raise ValueError('QUST: strings out of bounds')
    out['strings'] = d[so:so + sl]
    return out


def string_at(strings, off):
    """UTF-16 string of the blob at byte offset off ('' for -1)."""
    if off < 0 or off + 2 > len(strings):
        return ''
    n, = struct.unpack_from('<H', strings, off)
    return strings[off + 2:off + 2 + 2 * n].decode('utf-16-le', 'replace')
