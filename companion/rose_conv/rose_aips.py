"""AIPS: the monsters' AI scripts (3DDATA/AI/*.aip, FILE_AI.STB row = LIST_NPC ai column) decoded and normalised for the Unity runtime (GsvrRoseAi in the GoldSrcVR
repo, docs/RPG1_PACK.md section 16). Additive and optional: a pack without AIPS keeps the NBEH summary behaviour. Stdlib only, read-only.

The record layouts follow rose-offline rose-file-readers/src/aip.rs (read for understanding, nothing copied); every record is length prefixed, so an unknown opcode is
kept as a record the runtime ignores (condition = false, action = nothing). Padding bytes in the files are uninitialised (0xcd ...), so only the documented bytes are read.

Normalisation (what the runtime gets, all ints):
  * distances in centimetres (the files store metres; the "allies attack" (12 / 15 / 19) and "run home" (17) distances are metres too (small single / double digit values in the data), although
    rose-offline uses them as cm)
  * comparison operators as 0 =, 1 >, 2 >=, 3 <, 4 <=, 10 !=, 255 none (the boolean "less or equal" flags become 4 / 2, "less than" 3 / 1)
  * condition 9 and 28 become condition 3 (find nearby: allied, count operator, count, distance, level difference range); actions 5 / 6 / 17 become action 4 (random move with an origin
    0 spawn point / 1 current position / 2 found entity), 13 / 20 become 13, 15 / 19 become 12 (allies attack with a filter), 21 / 37 / 38 become 11 (spawn npc)
"""
import struct

import rose_ai

TAG = b'AIPS'
VERSION = 1
HDR = struct.Struct('<12I')            # npcCount, npcOff, progCount, progOff, evCount, evOff, recCount, recOff, version, 0, 0, 0
NPC = struct.Struct('<HH')              # npcId, program (0xFFFF = none)
PROG = struct.Struct('<HBB12H4x')       # idleIntervalS, newTargetPct, 0, (firstEvent, eventCount) x 6 triggers
EVT = struct.Struct('<HBB')             # firstRecord, conditionCount, actionCount (the actions follow the conditions)
REC = struct.Struct('<5B3x3i')          # op, kind (0 condition, 1 action), a, b, c, v0, v1, v2
NO_PROG = 0xFFFF
TRIGGERS = 6
OP_EQ, OP_GT, OP_GE, OP_LT, OP_LE, OP_NE, OP_NONE = 0, 1, 2, 3, 4, 10, 255
KIND_COND, KIND_ACT = 0, 1


class AipsError(ValueError):
    pass


def _i32(pl, o):
    return struct.unpack_from('<i', pl, o)[0] if len(pl) >= o + 4 else 0


def _u16(pl, o):
    return struct.unpack_from('<H', pl, o)[0] if len(pl) >= o + 2 else 0


def _i16(pl, o):
    return struct.unpack_from('<h', pl, o)[0] if len(pl) >= o + 2 else 0


def _u8(pl, o):
    return pl[o] if len(pl) > o else 0


def _op(v):
    return v if v in (OP_EQ, OP_GT, OP_GE, OP_LT, OP_LE, OP_NE) else OP_NONE


def _ab(v):
    return v if 0 <= v <= 5 else 255


def _c32(v):
    """Clamp to the int32 range (the record fields v0..v2 are int32; a metres value x 100 or a stray payload number can leave it)."""
    return max(-2147483648, min(2147483647, v))


def _clamp_u8(v):
    return max(0, min(255, v))


def norm_cond(op, pl):
    """(op, payload) of an AIP condition -> (op, a, b, c, v0, v1, v2) normalised."""
    if op == 2:
        return (2, 1 if _u8(pl, 4) else 0, OP_GE, 0, _i32(pl, 0), 0, 0)
    if op == 3:
        return (3, 1 if _u8(pl, 4) else 0, OP_NONE, _clamp_u8(_u16(pl, 10)), _i32(pl, 0) * 100, _i16(pl, 6), _i16(pl, 8))
    if op == 9:
        return (3, 1 if _u8(pl, 8) else 0, OP_NONE, 1, _i32(pl, 0) * 100, _i16(pl, 4), _i16(pl, 6))
    if op == 28:
        return (3, 1 if _u8(pl, 4) else 0, _op(_u8(pl, 12)), _clamp_u8(_u16(pl, 10)), _i32(pl, 0) * 100, _i16(pl, 6), _i16(pl, 8))
    if op == 4:
        return (4, 0, OP_GE, 0, _i32(pl, 0) * 100, 0, 0)
    if op == 5:
        return (5, 0, OP_LE if _u8(pl, 4) else OP_GE, 0, _i32(pl, 0) * 100, 0, 0)
    if op == 6:
        return (6, _ab(_u8(pl, 0)), OP_LE if _u8(pl, 8) else OP_GE, 0, _i32(pl, 4), 0, 0)
    if op == 7:
        return (7, 0, OP_LE if _u8(pl, 4) else OP_GE, 0, _i32(pl, 0), 0, 0)
    if op == 8:
        return (8, 0, OP_LT, 0, _u8(pl, 0), 0, 0)
    if op == 10:
        return (10, 0, 0, 0, 0, 0, 0)
    if op == 11:
        return (11, _ab(_u8(pl, 0)), OP_LT if _u8(pl, 1) else OP_GT, 0, 0, 0, 0)
    if op == 12:
        return (12, _ab(_u8(pl, 0)), OP_LE if _u8(pl, 8) else OP_GE, 0, _i32(pl, 4), 0, 0)
    if op == 13:
        return (13, 0 if _u8(pl, 0) else 1, 0, 0, 0, 0, 0)               # a = 1: day wanted
    if op == 14:
        t = _u8(pl, 1)
        return (14, 1 if _u8(pl, 0) else 0, t if t <= 1 else 2, 1 if _u8(pl, 2) else 0, 0, 0, 0)
    if op in (15, 16, 17, 29):
        return (op, {15: 0, 16: 1, 17: 2, 29: 3}[op], _op(_u8(pl, 8)), 0, _u16(pl, 0), _i32(pl, 4), 0)
    if op == 18:
        return (18, 0, 0, 0, _i32(pl, 0), 0, 0)
    if op == 19:
        return (19, 0, _op(_u8(pl, 4)), 0, _i32(pl, 0) * 100, 0, 0)
    if op in (20, 24):
        return (op, 0, 0, 0, _i32(pl, 0), _i32(pl, 4), 0)
    if op in (25, 26):
        return (op, _u8(pl, 0), 0, 0, _u8(pl, 1) * 60 + _u8(pl, 2), _u8(pl, 3) * 60 + _u8(pl, 4), 0)
    if op == 21:
        return (21, _ab(_u8(pl, 0)), _op(_u8(pl, 8)), 0, _i32(pl, 4), 0, 0)
    if op in (22, 23):
        return (op, 0, 0, 0, 0, 0, 0)
    if op == 27:
        return (27, 0, 0, 0, _u16(pl, 0), _u16(pl, 2), 0)
    if op == 30:
        return (30, _u8(pl, 0), 0, 0, 0, 0, 0)
    return (op & 0xFF, 0, 0, 0, 0, 0, 0)


def norm_act(op, pl):
    """(op, payload) of an AIP action -> (op, a, b, c, v0, v1, v2) normalised."""
    if op == 4:
        return (4, 1, 1 if _u8(pl, 4) else 0, 0, _i32(pl, 0) * 100, 0, 0)
    if op == 5:
        return (4, 0, 1 if _u8(pl, 4) else 0, 0, _i32(pl, 0) * 100, 0, 0)
    if op == 6:
        return (4, 2, 1 if _u8(pl, 0) else 0, 0, 200, 0, 0)
    if op == 17:
        return (4, 0, 1, 0, _i32(pl, 0) * 100, 0, 0)
    if op == 7:
        return (7, _ab(_u8(pl, 4)), 1 if _u8(pl, 5) else 0, 0, _i32(pl, 0) * 100, 0, 0)
    if op == 9:
        return (9, 0, 1 if _u8(pl, 4) else 0, 0, _i32(pl, 0) * 100, 0, 0)
    if op == 10:
        return (10, 0, 0, 0, _u16(pl, 0), 0, 0)
    if op == 11:
        return (11, 0, 0, 0, _u16(pl, 0), 150, 0)
    if op == 21:
        return (11, min(_u8(pl, 2), 2), 0, 0, _u16(pl, 0), _i32(pl, 4), 0)
    if op == 37:
        return (11, 0, 1 if _u8(pl, 2) else 0, 0, _u16(pl, 0), 150, 0)
    if op == 38:
        return (11, min(_u8(pl, 2), 2), 1 if _u8(pl, 8) else 0, 0, _u16(pl, 0), _i32(pl, 4), 0)
    if op == 12:
        return (12, 0, 0, 0, _i32(pl, 0) * 100, 0, max(0, _i32(pl, 4)))
    if op == 15:
        return (12, 2, 0, 0, _i32(pl, 0) * 100, 0, 0)
    if op == 19:
        return (12, 1, 0, 0, _i32(pl, 4) * 100, _u16(pl, 0), _u16(pl, 2))
    if op in (13, 20):
        return (13, 0, 0, 0, 0, 0, 0)
    if op == 2:
        return (2, 0, 0, 0, _u8(pl, 0), 0, 0)
    if op == 3:
        return (3, 0, 0, 0, _i32(pl, 0), 0, 0)
    if op == 25:
        return (25, min(_u8(pl, 0), 3), 0, 0, _u16(pl, 2), _u16(pl, 4), 0)
    if op in (26, 27, 28, 36):
        return (op, {26: 0, 27: 1, 28: 2, 36: 3}[op], _u8(pl, 8) if _u8(pl, 8) in (5, 6, 7) else 0, 0, _u16(pl, 0), _i32(pl, 4), 0)
    if op == 29:
        return (29, _u8(pl, 0), 0, 0, _i32(pl, 4), 0, 0)
    return (op & 0xFF, 0, 0, 0, 0, 0, 0)


def program(aip_bytes):
    """AIP file -> dict(idle_s, new_target_pct, triggers=[[(conds, acts)], ... 6 lists]) with normalised records."""
    try:
        idle, chance, trigs = rose_ai.parse(aip_bytes)
        out = []
        for t in range(TRIGGERS):
            evs = trigs[t][1] if t < len(trigs) else []
            out.append([([norm_cond(o, p) for o, p in conds], [norm_act(o, p) for o, p in acts]) for _en, conds, acts in evs])
    except (IndexError, ValueError) as e:          # a malformed file loses only its own program (build_section warns, its npcs keep the old behaviour)
        raise AipsError('malformed AIP: %s: %s' % (type(e).__name__, e))
    return dict(idle_s=min(65535, idle), new_target_pct=min(100, chance), triggers=out)


def encode(npc_progs, programs):
    """npc_progs: [(npc id, program index or None)], programs: [program dict] -> AIPS bytes."""
    progs = b''; evs = b''; recs = b''
    nev = nrec = 0
    for pr in programs:
        trig = []
        for evlist in pr['triggers']:
            trig += [nev, len(evlist)]
            for conds, acts in evlist:
                if len(conds) > 255 or len(acts) > 255:
                    raise AipsError('event with more than 255 conditions / actions')
                evs += EVT.pack(nrec, len(conds), len(acts)); nev += 1
                for kind, rows in ((KIND_COND, conds), (KIND_ACT, acts)):
                    for r in rows:
                        recs += REC.pack(r[0] & 0xFF, kind, r[1] & 0xFF, r[2] & 0xFF, r[3] & 0xFF, _c32(r[4]), _c32(r[5]), _c32(r[6])); nrec += 1
        progs += PROG.pack(pr['idle_s'], pr['new_target_pct'], 0, *trig)
    if nev > 65535 or nrec > 65535:
        raise AipsError('too many events / records (%d / %d)' % (nev, nrec))
    npcs = b''.join(NPC.pack(n, NO_PROG if p is None else p) for n, p in npc_progs)
    off_npc = HDR.size; off_prog = off_npc + len(npcs); off_ev = off_prog + len(progs); off_rec = off_ev + len(evs)
    off_rec += -off_rec % 4
    pad = b'\0' * (off_rec - off_ev - len(evs))
    return HDR.pack(len(npc_progs), off_npc, len(programs), off_prog, nev, off_ev, nrec, off_rec, VERSION, 0, 0, 0) + npcs + progs + evs + pad + recs


def decode(d):
    """AIPS bytes -> (npc_progs, programs) in the encode() form; raises AipsError on a malformed section (same rules as GsvrRoseAi.Valid)."""
    if len(d) < HDR.size:
        raise AipsError('short AIPS')
    nn, on, pn, op_, en, oe, rn, orr, ver = HDR.unpack_from(d, 0)[:9]
    if ver != VERSION:
        raise AipsError('AIPS version %d' % ver)
    for off, cnt, sz in ((on, nn, NPC.size), (op_, pn, PROG.size), (oe, en, EVT.size), (orr, rn, REC.size)):
        if off < HDR.size or off + cnt * sz > len(d):
            raise AipsError('AIPS table out of bounds')
    npcs = []
    for i in range(nn):
        n, p = NPC.unpack_from(d, on + i * NPC.size)
        if p != NO_PROG and p >= pn:
            raise AipsError('npc %d: program %d out of range' % (n, p))
        npcs.append((n, None if p == NO_PROG else p))
    evtab = [EVT.unpack_from(d, oe + i * EVT.size) for i in range(en)]
    rtab = [REC.unpack_from(d, orr + i * REC.size) for i in range(rn)]
    progs = []
    for i in range(pn):
        v = PROG.unpack_from(d, op_ + i * PROG.size)
        trig = []
        for t in range(TRIGGERS):
            e0, ne = v[3 + 2 * t], v[4 + 2 * t]
            if e0 + ne > en:
                raise AipsError('program %d trigger %d: events out of range' % (i, t))
            lst = []
            for e in range(e0, e0 + ne):
                r0, nc, na = evtab[e]
                if r0 + nc + na > rn:
                    raise AipsError('event %d: records out of range' % e)
                conds = [tuple([r[0]] + list(r[2:])) for r in rtab[r0:r0 + nc]]
                acts = [tuple([r[0]] + list(r[2:])) for r in rtab[r0 + nc:r0 + nc + na]]
                if any(r[1] != KIND_COND for r in rtab[r0:r0 + nc]) or any(r[1] != KIND_ACT for r in rtab[r0 + nc:r0 + nc + na]):
                    raise AipsError('event %d: record kinds out of order' % e)
                lst.append((conds, acts))
            trig.append(lst)
        progs.append(dict(idle_s=v[0], new_target_pct=v[1], triggers=trig))
    return npcs, progs


def build_section(vfs, side_actors, stb_reader):
    """-> (section tuple or None, warnings, info). side_actors = npc_sidecar 'actors' ({npc id: {..., 'ai': row}}); stb_reader(bytes) parses an STB (rosefmt.stb).
    Programs are shared by every npc that uses the same AIP file; a file that does not parse leaves its npcs without a program (old behaviour for them)."""
    warn = []
    try:
        ai = stb_reader(vfs.read('3DDATA/STB/FILE_AI.STB'))
    except KeyError:
        return None, ['AIPS: FILE_AI.STB missing'], {}
    by_path = {}; programs = []; npc_progs = []
    for key, a in sorted(side_actors.items(), key=lambda kv: int(kv[0])):
        aid = a.get('ai') or 0
        path = ai['cells'][aid][0] if isinstance(aid, int) and 0 < aid < ai['rows'] else ''
        p = None
        if path:
            if path not in by_path:
                try:
                    programs.append(program(vfs.read(path))); by_path[path] = len(programs) - 1
                except (KeyError, rose_ai.AipError, AipsError, struct.error) as e:
                    by_path[path] = None; warn.append('AIPS %s: %s' % (path, e))
            p = by_path[path]
        npc_progs.append((int(key), p))
    if not programs:
        return None, warn, {}
    data = encode(npc_progs, programs)
    info = dict(programs=len(programs), npcs=len(npc_progs), scripted=sum(1 for _n, p in npc_progs if p is not None), bytes=len(data))
    return (TAG, 1, len(programs), data), warn, info
