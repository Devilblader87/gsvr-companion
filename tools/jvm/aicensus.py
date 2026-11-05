#!/usr/bin/env python3
"""AI census natives (docs/ai_plan.md S2, group G1): the read-only queries of the engine state the race AI scripts call
most (unit counts by type, in progress vs done, towns = halls with a mine nearby, mines owned, research levels).
Clean room: behaviour only; every rule is tagged [obs] (seen in a decompile chapter) or [inf] (inferred).

Chapters: Unclassed_Native_0001 section 6 (counts, upgrade level and prices; open items 2 and 3 of its section 12),
Unclassed_Native_0003 section 5 (towns, mines), CCompAI_p1 section 7.3 / 7.4 (live counts), section 6.4 / 7.1 (census).
Udon twin: unity/Assets/W3VR/Scripts/W3AiCensus.cs.  Both VMs run the same rules, loops and tie breaks.

Model (all [inf] where the chapters leave the bucket meaning open):

  * Counter buckets.  The original keeps four per-type buckets (+0x1c .. +0x28); GetUnitCountDone reads the two of
    mask 0x30, GetUnitCount / GetTownUnitCount / GetPlayerUnitTypeCount read all four (mask 0x36).  Here:
      done(T)     = the player's living, finished units of type T that are not being upgraded
      progress(T) = structures of type T under construction, units of type T in a train queue slot of any of the
                    player's buildings, and buildings being upgraded to T (an upgrading building is counted at
                    the type it becomes, never at the type it is, so the scripts' sum over an upgrade chain does not
                    count it twice)
      GetUnitCountDone = done, GetUnitCount = done + progress.
    The "busy units" term of GetUnitCount (CCompAI_p1 7.4: units with an assigned target in state 2) is the pending
    build order of a worker (S3): a worker that was told to build type T and has not placed the structure yet counts as
    one T in progress (at the order's site), and so does an order the AI posted that the engine has not applied yet (the
    aibuild reservations, only for the calls of the AI itself).  Other busy units are already in done [inf].
  * No AI context (outside an AI thread): every count is 0, TownWithMine -1, booleans false [inf, 0001 section 12].
  * Towns.  The AI keeps four town slots (0003 section 5.1: +0x1b8 .. +0x1c4); a slot holds a hall reference and a mine
    reference.  Here a slot is bound to a hall of the player (a unit of HALL_IDS, living, under construction
    included) in creation order; the nearest mine (ngol / egol / ugol, living) within TOWN_RADIUS of the hall that
    no other slot of the player holds becomes the slot's mine.  A slot stays when only one of the two still lives
    (a mine without hall = the expansion the scripts build a hall for); it frees when both are gone.  A new hall
    first goes to a slot whose hall is gone and whose mine is within TOWN_RADIUS, else to the lowest free slot; with
    no slot free it is not a town.  The slots are refreshed at most once per step per player, on the first town
    query of the step (census state of the AI, not game state).
  * GetTownUnitCount(T, town, doneOnly): the same counts, restricted to the units whose nearest town anchor (hall,
    else mine of the slot) is the slot; a unit in a train queue belongs to the town of its building.  PORT12: with
    `W3_AI_TOWNORDER=1` / `aiTownOrder` the restriction is the chapter's relation instead: the unit's current order
    target is the slot's hall or mine (`Unclassed_AI_0003` s4 `[obs]`); train-queue / build-progress entries and the
    AI's own not-yet-applied orders have no current order target and do not count.  Default OFF because which of the
    slot's two unit references the original compares is not decoded `[inf]`.
  * GetUpgradeLevel(R) = researched level + 1 while a slot of the player's queues holds that research (0001 6: "plus
    one when the type's mask-2 bucket holds at least 1").  GetUpgradeGoldCost / WoodCost quote the price of the next
    level to buy: researched + (1 if in progress) + 1, clamped to [1, max level] [inf]; 0 for an unknown id.
"""

import os

from .ai import RV_N

TOWN_ORDER = os.environ.get('W3_AI_TOWNORDER') == '1'   # PORT12: GetTownUnitCount by the current order target (Unclassed_AI_0003 s4); default OFF, deliberately not part of W3_AI_EXTRAS
RV_STEPS = 10                        # life of an aibuild reservation (steps)
TOWN_N = 4
TOWN_RADIUS = 900.0                  # [inf] hall to mine distance of one town (not measured)
TOWN_RADIUS2 = TOWN_RADIUS * TOWN_RADIUS
MINE_RETRY = 200                     # steps between mine searches of a slot that has a hall and no mine

# town halls of the four races (the harvest drop-off set without the mines): htow hkee hcas ogre ostr ofrt unpl unp1 unp2
# etol etoa etoe
HALL_IDS = frozenset((0x68746F77, 0x686B6565, 0x68636173, 0x6F677265, 0x6F737472, 0x6F667274, 0x756E706C,
                      0x756E7031, 0x756E7032, 0x65746F6C, 0x65746F61, 0x65746F65))
# gold mines: ngol egol ugol
MINE_IDS = frozenset((0x6E676F6C, 0x65676F6C, 0x75676F6C))
MINE_UGOL = 0x75676F6C
MINE_NGOL = 0x6E676F6C


def _live(u):
    return u is not None and u.alive and not u.dead and not u.consumed      # PORT4-Build: a consumed wisp is out of the AI's population (CAbilityNightElfBuild §3.3)


def _d2(ax, ay, bx, by):
    dx = ax - bx
    dy = ay - by
    return dx * dx + dy * dy


def _mine_near(N, pl, ai, hx, hy):
    """the nearest living mine within TOWN_RADIUS of (hx, hy) that no slot of this player holds; first in creation order
    wins a tie."""
    best, bd = None, 0.0
    for m in N.units:
        if m.tid not in MINE_IDS or not m.alive or m.dead:
            continue
        s = m.serial
        held = False
        for i in range(TOWN_N):
            if ai.town_m[i] == s:
                held = True
        if held:
            continue
        d = _d2(m.x, m.y, hx, hy)
        if d <= TOWN_RADIUS2 and (best is None or d < bd):
            best, bd = m, d
    return best


MINE_AT_R2 = 64.0 * 64.0             # [inf] a replaced mine unit stands within this distance of the old one


def _mine_at(N, ai, x, y):
    """AI-MINEBUILD: serial of the living mine (any type of MINE_IDS) at (x, y) that no slot holds, else 0.  A Haunted Gold Mine
    replaces the neutral mine unit in place (engine.start_construction: new serial), a cancel / destruction brings a neutral
    mine back the same way, so the slot follows the place, not the serial."""
    if x == 0.0 and y == 0.0:
        return 0
    for m in N.units:
        if m.tid not in MINE_IDS or not _live(m) or _d2(m.x, m.y, x, y) > MINE_AT_R2:
            continue
        held = False
        for i in range(TOWN_N):
            if ai.town_m[i] == m.serial:
                held = True
        if not held:
            return m.serial
    return 0


def refresh_towns(N, ai):
    """bind the four town slots of the AI's player to its halls and mines (once per step)."""
    steps = N.vm.steps
    if ai.town_step == steps:
        return
    ai.town_step = steps
    pl = N.players[ai.slot]
    by = N.eng.by_serial
    for i in range(TOWN_N):
        if ai.town_h[i] != 0 and not _live(by.get(ai.town_h[i])):
            ai.town_h[i] = 0
        if ai.town_m[i] != 0 and not _live(by.get(ai.town_m[i])):
            ai.town_m[i] = _mine_at(N, ai, ai.town_mx[i], ai.town_my[i])     # AI-MINEBUILD: the mine unit was replaced in place (haunted mine / cancel)
    for u in N.units:                                   # halls in creation order
        if u.owner is not pl or u.tid not in HALL_IDS or not u.alive or u.dead:
            continue
        s = u.serial
        known = False
        for i in range(TOWN_N):
            if ai.town_h[i] == s:
                known = True
        if known:
            continue
        slot = -1
        for i in range(TOWN_N):                         # a mine without hall within reach first
            if slot < 0 and ai.town_h[i] == 0 and ai.town_m[i] != 0:
                m = by.get(ai.town_m[i])
                if m is not None and _d2(m.x, m.y, u.x, u.y) <= TOWN_RADIUS2:
                    slot = i
        for i in range(TOWN_N):
            if slot < 0 and ai.town_h[i] == 0 and ai.town_m[i] == 0:
                slot = i
        if slot >= 0:
            ai.town_h[slot] = s
            ai.town_try[slot] = -MINE_RETRY             # search a mine below
    for i in range(TOWN_N):
        if ai.town_h[i] != 0 and ai.town_m[i] == 0 and steps - ai.town_try[i] >= MINE_RETRY:
            ai.town_try[i] = steps
            h = by.get(ai.town_h[i])
            m = _mine_near(N, pl, ai, h.x, h.y)
            if m is not None:
                ai.town_m[i] = m.serial
    for m in N.units:                                   # AI-MINEBUILD: an own mine no slot holds (a haunted mine built on a free mine) is an expansion
        if m.tid not in MINE_IDS or m.owner is not pl or not _live(m):
            continue
        held = False
        for i in range(TOWN_N):
            if ai.town_m[i] == m.serial:
                held = True
        if held:
            continue
        for i in range(TOWN_N):
            if ai.town_h[i] == 0 and ai.town_m[i] == 0:
                ai.town_m[i] = m.serial
                break
    for i in range(TOWN_N):
        if ai.town_m[i] != 0:
            m = by.get(ai.town_m[i])
            if m is not None:
                ai.town_mx[i] = m.x
                ai.town_my[i] = m.y


def _anchor(N, ai, i):
    """(x, y) of town slot i: the hall, else the mine; None for an empty slot."""
    by = N.eng.by_serial
    u = by.get(ai.town_h[i]) if ai.town_h[i] != 0 else None
    if u is None and ai.town_m[i] != 0:
        u = by.get(ai.town_m[i])
    return None if u is None else (u.x, u.y)


def _ordered_to(ai, town, u):
    """PORT12 Unclassed_AI_0003 s4: the unit's current order target is the town slot's unit.  The port's slot has the
    hall and mine serials where the original has one slot unit; either matches `[inf]`."""
    t = getattr(u, 'target', None)
    if t is None or not getattr(t, 'alive', False):
        return False                                  # the slot/order target must still resolve (C# !oAlive[t])
    s = getattr(t, 'serial', 0)
    if s == 0:
        return False
    return (ai.town_h[town] != 0 and s == ai.town_h[town]) or (ai.town_m[town] != 0 and s == ai.town_m[town])


def _town_of(N, ai, x, y):
    """index of the town slot whose anchor is nearest to (x, y) (lowest index wins a tie), -1 without a town."""
    best, bd = -1, 0.0
    for i in range(TOWN_N):
        a = _anchor(N, ai, i)
        if a is None:
            continue
        d = _d2(a[0], a[1], x, y)
        if best < 0 or d < bd:
            best, bd = i, d
    return best


def count(N, pl, tid, done_only, ai=None, town=-1):
    """done (+ progress) of unit type `tid` for player `pl`; with `town` >= 0 only the units of that town slot of `ai`.
    S3: progress also holds the build orders of the player's workers that have not placed the structure yet (the "busy
    units" term of 0001 s6 / CCompAI_p1 7.4: a unit with an assigned job of this type), counted at the order's site, and
    the orders the AI posted that the engine has not applied yet (aibuild reservations, only when `ai` is given)."""
    n = 0
    for v in N.units:
        if v.owner is not pl or not v.alive or v.dead or v.consumed:
            continue
        c = 0
        if v.tid == tid and not v.constructing and v.upg is None:
            c = 1
        if not done_only:
            if v.tid == tid and v.constructing:
                c += 1
            if v.upg is not None and v.upg[0] == tid:
                c += 1
            for e in v.tq:
                if e[1] == 0 and e[0] == tid:
                    c += 1
        if c and town >= 0:
            if TOWN_ORDER:
                if not _ordered_to(ai, town, v):
                    c = 0
            elif _town_of(N, ai, v.x, v.y) != town:
                c = 0
        if not done_only and v.build is not None and v.build[0] == tid:
            if town < 0 or (_ordered_to(ai, town, v) if TOWN_ORDER
                            else _town_of(N, ai, v.build[1], v.build[2]) == town):
                c += 1
        n += c
    if not done_only and ai is not None:
        steps = N.vm.steps
        by = N.eng.by_serial
        for i in range(RV_N):
            if ai.rv_ser[i] == 0 or ai.rv_kind[i] == 0 or ai.rv_tid[i] != tid or steps - ai.rv_step[i] > RV_STEPS:
                continue
            w = by.get(ai.rv_ser[i])
            if w is None or not w.alive or w.dead:
                continue
            k = ai.rv_kind[i]
            if k == 1 and w.build is not None:
                continue                                # a build order the engine applied: counted above
            if k != 1 and (w.tq or w.upg is not None):
                continue                                # a train / upgrade order the engine applied: counted above
            if town >= 0 and TOWN_ORDER:
                continue                                # no current order target yet (the order was not applied)
            x, y = (ai.rv_x[i], ai.rv_y[i]) if k == 1 else (w.x, w.y)
            if town < 0 or _town_of(N, ai, x, y) == town:
                n += 1
    return n


def unit_count(N, ai, tid, done_only):
    if ai is None:
        return 0
    return count(N, N.players[ai.slot], tid, done_only, ai)


def town_unit_count(N, ai, tid, town, done_only):
    if ai is None or town < 0 or town >= TOWN_N:
        return 0
    refresh_towns(N, ai)
    if ai.town_h[town] == 0 and ai.town_m[town] == 0:
        return 0
    return count(N, N.players[ai.slot], tid, done_only, ai, town)


def upgrade_level(N, pl, rid):
    return pl.tech.get(rid, 0) + (1 if N.eng.research_busy(pl, rid) else 0)


def upgrade_cost(N, ai, rid, k):
    """gold (k 0) / lumber (k 1) of the next level of research rid."""
    if ai is None:
        return 0
    r = N.eng.upgrades.get(rid)
    if r is None:
        return 0
    lvl = upgrade_level(N, N.players[ai.slot], rid) + 1
    mx = int(r.get('maxlevel', 0))
    if lvl > mx:
        lvl = mx
    if lvl < 1:
        lvl = 1
    return N.eng.research_cost(rid, lvl)[k]


def mines_owned(N, ai):
    if ai is None:
        return 0
    refresh_towns(N, ai)
    return sum(1 for i in range(TOWN_N) if ai.town_m[i] != 0)


def gold_owned(N, ai):
    if ai is None:
        return 0
    refresh_towns(N, ai)
    by = N.eng.by_serial
    g = 0
    for i in range(TOWN_N):
        m = by.get(ai.town_m[i]) if ai.town_m[i] != 0 else None
        if m is not None:                                    # a mine removed since this step's refresh counts 0 (C# m >= 0)
            g += int(getattr(m, 'res_amt', 0))
    return g


def town_with_mine(N, ai):
    if ai is None:
        return -1
    refresh_towns(N, ai)
    for i in range(TOWN_N):
        if ai.town_m[i] != 0:
            return i
    return -1


def town_has_mine(N, ai, i):
    if ai is None or i < 0 or i >= TOWN_N:
        return False
    refresh_towns(N, ai)
    return ai.town_m[i] != 0


def town_has_hall(N, ai, i):
    if ai is None or i < 0 or i >= TOWN_N:
        return False
    refresh_towns(N, ai)
    return ai.town_h[i] != 0
