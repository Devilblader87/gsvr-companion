"""AIP (monster AI script) summariser: reads the behaviour numbers a co-op runtime needs (aggro radius, wandering, leash, flee, ally calls)
without interpreting the whole script language. Stdlib only, read-only. Layout from docs/ROSE_SPEC.md 3.14 / 13 (condition and action records are
length prefixed, so unknown opcodes are skipped safely).

  summarize(aip_bytes) -> dict(idle_interval_s, new_target_pct, aggressive, aggro_m, wander_m, wander_run, leash_m, flee_hp_pct, call_allies_m,
                               uses_skills, drops, triggers={name: events})
"""
import struct

COND_BASE = 0x04000000
ACT_BASE = 0x0B000000

# condition opcodes (low 24 bits) that matter here
C_DAMAGE, C_NEARBY, C_FAR_FROM_SPAWN, C_TARGET_DIST, C_HP, C_RANDOM, C_NEARBY2, C_NEARBY3 = 2, 3, 4, 5, 7, 8, 9, 28
# action opcodes
A_STOP, A_MOVE_CUR, A_MOVE_SPAWN, A_ATK_STAT, A_MOVE_AWAY, A_ALLIES, A_ATK_NEAR, A_ATK_FIND, A_ALLIES_SAME, A_ATK_ATTACKER, A_MOVE_SPAWN_RUN = 1, 4, 5, 7, 9, 12, 13, 14, 15, 16, 17
A_ALLIES_NPC, A_ATK_NEAR2, A_USE_SKILL, A_KILL_SELF = 19, 20, 25, 24
A_DROP = 18


class AipError(ValueError):
    pass


def parse(d):
    """-> (idle_interval_s, new_target_pct, [(trigger name, [(event name, [(opcode, payload)], [(opcode, payload)])])])"""
    if len(d) < 16:
        raise AipError('short AIP')
    ntrig, idle, chance = struct.unpack_from('<3I', d, 0)
    p = 12
    tl, = struct.unpack_from('<I', d, p); p += 4 + tl
    if ntrig > 64:
        raise AipError('implausible trigger count %d' % ntrig)

    def rd_str32():
        nonlocal p
        s = d[p:p + 32].split(b'\0', 1)[0]; p += 32
        return s.decode('cp949', 'replace')

    def rd_records(base):
        nonlocal p
        n, = struct.unpack_from('<I', d, p); p += 4
        if n > 4096:
            raise AipError('implausible record count')
        out = []
        for _ in range(n):
            size, op = struct.unpack_from('<2I', d, p)
            if size < 8 or p + size > len(d):
                raise AipError('bad record size')
            out.append((op ^ base if (op & 0xff000000) == base else op, d[p + 8:p + size]))
            p += size
        return out

    trigs = []
    for _ in range(ntrig):
        name = rd_str32()
        nev, = struct.unpack_from('<I', d, p); p += 4
        if nev > 4096:
            raise AipError('implausible event count')
        evs = []
        for _ in range(nev):
            en = rd_str32()
            conds = rd_records(COND_BASE)
            acts = rd_records(ACT_BASE)
            evs.append((en, conds, acts))
        trigs.append((name, evs))
    return idle, chance, trigs


TRIG_CREATED, TRIG_IDLE, TRIG_ATTACK_MOVE, TRIG_DAMAGED, TRIG_KILL, TRIG_DEAD = range(6)


def summarize(d):
    """Behaviour numbers (metres / seconds / percent) from the literal arguments of the script; 0 / False = not present.
    Triggers are positional (created, idle, attack-move, damaged, kill, dead; the names are Korean).
      aggressive / aggro_m   : the idle trigger searches a non-allied entity within aggro_m and then attacks it (a passive monster only fights back)
      approach_m             : idle trigger walks towards an entity found within that radius without attacking (jelly beans 'follow' players)
      wander_m / wander_run  : idle trigger random-walks up to wander_m metres from its current position
      leash_m                : farther than this from the spawn point the monster returns to it
      giveup_m               : attack-move trigger: target farther than this away -> give up and return
      flee_hp_pct            : HP percent at or below which a trigger runs away / returns (0 = never)
      call_allies_m          : calls allies (same npc / nearby) within that radius when fighting
    """
    idle, chance, trigs = parse(d)
    out = dict(idle_interval_s=idle, new_target_pct=chance, aggressive=False, aggro_m=0, approach_m=0, wander_m=0, wander_run=False, leash_m=0,
               giveup_m=0, flee_hp_pct=0, call_allies_m=0, uses_skills=False, events=0, triggers=[t[0] for t in trigs])
    for ti, (tname, evs) in enumerate(trigs):
        for en, conds, acts in evs:
            out['events'] += 1
            aops = [a[0] for a in acts]
            attacks = any(o in (A_ATK_NEAR, A_ATK_FIND, A_ATK_NEAR2, A_ATK_STAT) for o in aops)
            for op, pl in conds:
                if op in (C_NEARBY, C_NEARBY3, C_NEARBY2) and len(pl) >= 5:
                    dist = struct.unpack_from('<i', pl, 0)[0]
                    allied = (pl[8] if op == C_NEARBY2 and len(pl) >= 9 else pl[4]) != 0
                    if not (0 < dist < 200):
                        continue
                    if not allied and ti == TRIG_IDLE and attacks:
                        out['aggressive'] = True; out['aggro_m'] = max(out['aggro_m'], dist)
                    elif not allied and ti == TRIG_IDLE and 6 in aops:
                        out['approach_m'] = max(out['approach_m'], dist)
                    elif allied and any(o in (A_ALLIES, A_ALLIES_SAME, A_ALLIES_NPC) for o in aops):
                        out['call_allies_m'] = max(out['call_allies_m'], dist)
                elif op == C_FAR_FROM_SPAWN and len(pl) >= 4:
                    out['leash_m'] = max(out['leash_m'], struct.unpack_from('<i', pl, 0)[0])
                elif op == C_TARGET_DIST and len(pl) >= 5 and ti == TRIG_ATTACK_MOVE and not pl[4]:
                    out['giveup_m'] = max(out['giveup_m'], struct.unpack_from('<i', pl, 0)[0])
                elif op == C_HP and len(pl) >= 5 and pl[4] and any(o in (A_MOVE_AWAY, A_MOVE_SPAWN, A_MOVE_SPAWN_RUN) for o in aops):
                    out['flee_hp_pct'] = max(out['flee_hp_pct'], struct.unpack_from('<i', pl, 0)[0])
            for op, pl in acts:
                if op == A_MOVE_CUR and len(pl) >= 5 and ti == TRIG_IDLE:
                    dist = struct.unpack_from('<i', pl, 0)[0]
                    if 0 < dist < 200:
                        out['wander_m'] = max(out['wander_m'], dist); out['wander_run'] = out['wander_run'] or pl[4] != 0
                elif op == A_USE_SKILL:
                    out['uses_skills'] = True
    return out
