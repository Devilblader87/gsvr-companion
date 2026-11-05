#!/usr/bin/env python3
"""AI housekeeping, enemy intel and heroes (docs/ai_plan.md S5b + S6, groups G6 / G7 and the hero part of G1): the natives
RemoveInjuries / RemoveSiege / IgnoredUnits / GetEnemyExpansion / IsTowered / TeleportCaptain / AddGuardPost / FillGuardPosts /
ReturnGuardPosts / DisablePathing / SetAmphibious / GetBuilding / Unsummon / MergeUnits / ConvertUnits / PurchaseZeppelin /
LoadZepWave / GetHeroId / GetHeroLevelAI / Suicide* and the debug natives (DisplayText* / Debug*), plus the hero skill pass
(SetHeroLevels' callback).  Everything goes through the existing paths: a decision is a player input (engine.post_input).
Udon twin: unity/Assets/W3VR/Scripts/W3AiExtra.cs.  Clean room: behaviour only, [obs] = seen in a decompile chapter, [inf] =
inferred (the chapters pin the native surface, not the numbers), open = needs an engine subsystem that does not exist.

Rules (chapters: Unclassed_Native_0001 s5 / s8 / s9, Unclassed_Native_0002 s2 / s3, Unclassed_Native_0003 s1 / s2 / s4 / s6,
Unclassed_AI_0001 s7, CCompAI_p1):

  * DisplayText / DisplayTextI / II / III, DebugS / DebugFI / DebugUnitID: no effect [obs 0001 s2: retail bodies are a bare
    return; DisplayText is the Trace helper of common.ai].
  * RemoveInjuries [obs 0003 s4.1: walks the attack group and takes the injured out, "re-heals" is a guess]: while no wave is
    under way the attack captain releases its members below INJ_PCT percent life; with the "ignore injured" flag
    (SetIgnoreInjured, set by StandardAI) AddAssault does not recruit such units either [inf: the flag's reading].
    RemoveSiege [obs 0003 s4.2: detaches the siege units from the group]: the members with a siege attack (attack type 3) are
    released the same way.  Both do nothing while a wave runs (the composition is frozen, S5a).
  * IgnoredUnits(type) [obs 0001 s5.3: answers an opaque list-node value, common.ai adds it to the wanted quantity, so it is
    used as a count here (deviation, [inf])]: the number of the player's units of the type that the group cannot use right
    now: injured (below INJ_PCT) units that no captain holds, 0 when the ignore-injured flag is off.
  * GetEnemyExpansion [obs 0001 s8: picks a unit, null when nothing]: the enemy town hall (constructing included) that is not the
    first hall of its owner, nearest to the gather point of the attack captain [inf: "expansion" = any hall but the owner's
    oldest]; null without captains.  IsTowered(unit) [obs 0002 s3.4: true only when the enumeration counted at least two]: at
    least TOWER_N of the owner's finished armed structures stand within TOWER_R of the unit [inf: radius, class].
  * TeleportCaptain(x, y) [0002 s2.2, inf: instant move]: the attack captain's members stop and are set down at the point (the
    engine's SetUnitPosition path: relocation and region events of the next step).
  * AddGuardPost(type, x, y) [obs 0001 s5.2: appends a node; the list holds GP_N posts here]; FillGuardPosts [obs s5.4: a post
    whose unit is empty or dead starts production of the type with the produce helper, default town]: a free idle unit of the
    type that no group holds is posted there at once (attack-move), else the helper is asked for one; ReturnGuardPosts [obs s5.5:
    re-asserts the guard order of units standing on a post]: an idle post unit farther than GUARD_R is sent back (attack-move).
    Post units are not recruited into the captains.
  * DisablePathing / SetAmphibious [obs 0002 s2.8: one-way flag bits 0x4000 / 0x8000]: stored (cp_flags bits 8 / 16), not
    consumed (the engine has no switch for either).
  * GetBuilding(player) [obs 0003 s1.9]: the player's first (creation order) finished structure that carries the Unsummon
    ability (Auns) [inf: the only user, UnsummonAll, loops until null]; Unsummon(unit) does nothing [open: Auns].
  * MergeUnits(qty, a, b, make) [obs 0001 s9.2]: true when qty <= units of `make` (all buckets) + the player's units of `a` in
    progress; false when a or b has no finished unit; else the merge helper, which has no engine path here (archer + hippogryph
    rider mount is not in the engine) -> false [open].  ConvertUnits(qty, type) [obs 0001 s9.1]: the source type's `Aave`
    record names the unit type in its first level (`S.A` = the source fourcc, `UnitID` = the target), so each finished own
    source up to `qty` target units is morphed in place through `Engine.morph_type`.  PORT12 [obs runtime 1.26a,
    oracle convert_units_cost]: the direct `avengerform` order is refused without the target type's `Requires` research
    (`Rusp`), costs its gold / lumber (100 / 50) and needs its food difference free (2 -> 4), so the same gates are charged
    here; applying them to the AI native (not observed) and the in-place morph are [inf].  True when at least one morphed.
  * PurchaseZeppelin [obs 0001 s8]: buys the zeppelin ('nzep') at the nearest neutral shop that sells it and accepts the
    player now (the shop's own validation, input kind 5); nothing otherwise.  LoadZepWave(x, y) [obs 0002 s2.2]: stored, not
    consumed [open: cargo / transport loading is not in the engine].
  * SuicidePlayer / SuicidePlayerUnits / SuicideUnit / SuicideUnitEx [obs 0003 s2, 0001 s1: campaign suicide waves]: the
    documented refusal (no order, false) [open: campaign suicide mode].
  * Heroes [obs 0003 s6]: SetHeroLevels stores a code; the engine calls it when a hero has an unspent skill point.  The hero pass
    (every HERO_EVERY steps, the player's heroes in creation order, at most HERO_MAX) runs the code as a short thread of the
    player's own program with the hero as "current hero": GetHeroId = its type, GetHeroLevelAI = learned skill levels + 1 (the
    1-based index of the next skill [inf: base value and offset of 0003 s6.2 unresolved]); the integer the code returns is the
    ability to learn -> a player input (kind 0, the HUD's learn click).  Outside the callback both answer 0.
"""
import os

from . import aibuild as AB
from . import aicaptain as CP
from . import aicensus as AC
from . import aispell as SPL
from . import aimicro as MIC
from .ai import CP_MEM, GP_N, FLAG_IGNORE_INJURED
from .engine import O_ATTACK, PLAYER_GOLD, PLAYER_LUMBER

TOWER_R2 = 800.0 * 800.0       # armed structures within this of a unit make it towered [inf]
TOWER_N = 2                    # ... at least this many [obs 0002 s3.4: counter > 1]
GUARD_R2 = 150.0 * 150.0       # a post unit this far from its post is sent back [inf]
HERO_EVERY = 200               # steps between two hero passes (1 s) [inf]
HERO_MAX = 4                   # heroes served per pass
HERO_BUDGET = 20000            # ops of one skill callback
SIEGE_ATK = 3                  # attack type 'siege' (unitdata.ATK_TYPES)
NZEP = 0x6E7A6570              # 'nzep'
AI_CONVERT = os.environ.get('W3_AI_CONVERT') == '1'   # PORT12: costs / requirements / food of the Aave morph modelled; default OFF = the old 'false' (fun maps that call the native keep their answer)
AUNS = 0x41756E73              # 'Auns'
AAVE = 'Aave'                  # the morph data record of ConvertUnits (0001 s9.1)
CF_NOPATH = 8                  # cp_flags bits (aicaptain uses 1, 2, 4)
CF_AMPHIB = 16

_first = [0] * 12              # GetEnemyExpansion scratch: the oldest hall serial of each owner


def _live(u):
    # PORT12-ReviewFix: a consumed night-elf builder is out of the AI's population, like aicensus._live.
    return u is not None and u.alive and not u.dead and not getattr(u, 'consumed', False)


def _siege(eng, u):
    for i in range(2):
        if eng.has_weapon(u, i) and int(eng.wv(u, i, 'atkType') or 0) == SIEGE_ATK:
            return True
    return False


def _armed(eng, u):
    return eng.has_weapon(u, 0) or eng.has_weapon(u, 1)


# ------------------------------------------------------------------------------------------------ group housekeeping
def _release(N, ai, injured_too, siege_too):
    """release the idle attack captain's members that are injured / siege; -> count."""
    if ai is None or not ai.cp_on or ai.cp_state[0] != 0:
        return 0
    CP._prune(N, ai, 0)
    by = N.eng.by_serial
    n = 0
    for i in range(CP_MEM):
        s = ai.cp_mem[i]
        if s == 0:
            continue
        u = by.get(s)
        if (injured_too and CP.injured(u)) or (siege_too and _siege(N.eng, u)):
            ai.cp_mem[i] = 0
            n += 1
    return n


def remove_injuries(N, ai):
    if ai is not None:
        ai.ex_inj += _release(N, ai, True, False)


def remove_siege(N, ai):
    if ai is not None:
        ai.ex_sie += _release(N, ai, False, True)


def ignored_units(N, ai, tid):
    if ai is None or not (ai.flags & FLAG_IGNORE_INJURED):
        return 0
    pl = N.players[ai.slot]
    n = 0
    for u in N.units:
        if u.tid == tid and u.owner is pl and _live(u) and not u.is_bldg and CP.injured(u) and not CP._is_member(ai, u.serial):
            n += 1
    return n


# ------------------------------------------------------------------------------------------------ enemy intel
def enemy_expansion(N, ai):
    """the enemy hall nearest to the gather point that is not its owner's oldest hall, else None."""
    if ai is None or not ai.cp_on:
        return None
    CP._home(N, ai)
    gx, gy = CP._gather(ai, 0)
    pl = N.players[ai.slot]
    eng = N.eng
    for i in range(12):
        _first[i] = 0
    for u in N.units:
        if not u.is_bldg or u.tid not in AC.HALL_IDS or not _live(u) or u.owner.id >= 12 or u.owner is pl:
            continue
        if eng.ally(pl, u.owner):
            continue
        if _first[u.owner.id] == 0:
            _first[u.owner.id] = u.serial
    best = None
    bd = 0.0
    for u in N.units:
        if not u.is_bldg or u.tid not in AC.HALL_IDS or not _live(u) or u.owner.id >= 12 or u.owner is pl:
            continue
        if eng.ally(pl, u.owner) or _first[u.owner.id] == u.serial:
            continue
        d = CP._d2(u.x, u.y, gx, gy)
        if best is None or d < bd:
            best = u
            bd = d
    return best


def is_towered(N, ai, t):
    if ai is None or t is None or not _live(t):
        return False
    eng = N.eng
    n = 0
    for u in N.units:
        if u.owner is t.owner and u.is_bldg and not u.constructing and _live(u) and _armed(eng, u) \
                and CP._d2(u.x, u.y, t.x, t.y) <= TOWER_R2:
            n += 1
    return n >= TOWER_N


# ------------------------------------------------------------------------------------------------ captains
def teleport(N, ai, x, y, place):
    """TeleportCaptain: the attack captain's members are set down at (x, y) (orders stopped); place(unit, x, y) is the
    engine's position setter of SetUnitPosition (natives.py)."""
    if ai is None or not ai.cp_on:
        return 0
    CP._prune(N, ai, 0)
    by = N.eng.by_serial
    n = 0
    for i in range(CP_MEM):
        s = ai.cp_mem[i]
        if s == 0:
            continue
        u = by.get(s)
        N.eng.clear_order(u)
        place(u, x, y)
        n += 1
    ai.ex_tele += n
    return n


def set_cp_flag(ai, bit):
    if ai is not None:
        ai.cp_flags |= bit


# ------------------------------------------------------------------------------------------------ guard posts
def add_guard_post(ai, tid, x, y):
    if ai is None or ai.gp_n >= GP_N:
        return
    k = ai.gp_n
    ai.gp_n = k + 1
    ai.gp_tid[k] = tid
    ai.gp_x[k] = x
    ai.gp_y[k] = y
    ai.gp_ser[k] = 0


def fill_guard_posts(N, ai):
    if ai is None:
        return
    pl = N.players[ai.slot]
    by = N.eng.by_serial
    for k in range(ai.gp_n):
        s = ai.gp_ser[k]
        if s != 0:
            u = by.get(s)
            if not _live(u) or u.owner is not pl:
                ai.gp_ser[k] = 0
                s = 0
        if s != 0:
            continue
        tid = ai.gp_tid[k]
        c = None
        for v in N.units:
            if c is None and v.tid == tid and CP._recruitable(N, v, pl) and not CP._is_member(ai, v.serial) \
                    and not CP.is_post_unit(ai, v.serial):
                c = v
        if c is not None:
            ai.gp_ser[k] = c.serial
            N.eng.post_input(ai.slot, 1, c.serial, O_ATTACK, ai.gp_x[k], ai.gp_y[k])
        else:
            AB._produce_one(N, ai, tid, -1)


def return_guard_posts(N, ai):
    if ai is None:
        return
    by = N.eng.by_serial
    for k in range(ai.gp_n):
        s = ai.gp_ser[k]
        if s == 0:
            continue
        u = by.get(s)
        if _live(u) and u.order == 0 and CP._d2(u.x, u.y, ai.gp_x[k], ai.gp_y[k]) > GUARD_R2:
            N.eng.post_input(ai.slot, 1, s, O_ATTACK, ai.gp_x[k], ai.gp_y[k])


# ------------------------------------------------------------------------------------------------ buildings, merges, zeppelin
def get_building(N, ai, pl):
    if ai is None or pl is None:
        return None
    for u in N.units:
        if u.owner is pl and u.is_bldg and not u.constructing and _live(u) and AUNS in u.abilities:
            return u
    return None


def merge_units(N, ai, qty, a, b, make):
    if ai is None:
        return False
    busy = AC.unit_count(N, ai, a, False) - AC.unit_count(N, ai, a, True)
    if qty <= AC.unit_count(N, ai, make, False) + busy:
        return True
    if AC.unit_count(N, ai, a, True) < 1 or AC.unit_count(N, ai, b, True) < 1:
        return False
    return False                                    # open: the merge itself (mount / morph) has no engine path


def _fourcc(text):
    if not isinstance(text, str) or len(text) != 4:
        return 0
    return (ord(text[0]) << 24) | (ord(text[1]) << 16) | (ord(text[2]) << 8) | ord(text[3])


def _morph_target(N, u):
    """the target of a unit's `Aave` ability: `UnitID`, with `S.A` naming the source type (0001 s9.1 [inf])."""
    eng = N.eng
    own = u.data.get('id4', '')
    for code in u.abilities:
        r = eng.ab.rec(code)
        if r is None or r['code'] != AAVE or not r['lv']:
            continue
        lv = r['lv'][0]
        src = lv.get('S', {}).get('A', '')
        if src and isinstance(own, str) and len(own) == 4 and src != own:
            continue
        tid = _fourcc(lv.get('UnitID', ''))
        if tid != 0 and tid in N.unit_types:
            return tid
    return 0


def convert_units(N, ai, qty, tid):
    """ConvertUnits(qty, type) [obs 0001 s9.1]: morph the player's finished units of `type` into the target named by
    their `Aave` record until `qty` target units exist; true when at least one unit morphed.  Unknown type / no AI / an
    already met count -> false (nothing was issued) [inf].

    PORT12 [obs runtime 1.26a, oracle convert_units_cost]: the direct `avengerform` Aave order is refused while the
    target type's requirement (Rusp) is missing, costs the target type's gold / lumber (100 / 50) and needs its food
    difference free (2 -> 4), then the type changes in the same thread.  The same gates are applied here before each
    morph; the gold / lumber and the food used / made difference are charged.  Applying them to the AI native itself is
    [inf] (the oracle ran the direct order, not the AI script)."""
    if not AI_CONVERT or ai is None or qty <= 0:
        return False
    pl = N.players[ai.slot]
    target = 0
    for u in N.units:
        if u.owner is pl and u.tid == tid and _live(u) and not u.constructing:
            target = _morph_target(N, u)
            if target != 0:
                break
    if target == 0:
        return False
    if target not in N.unit_types:
        return False                                      # PORT12-ReviewFix: an unknown target type issues nothing (C# false)
    if not N.eng.req_ok(pl, target):
        return False                                      # [obs] refused without the target type's research
    sd = N.unit_types.get(tid) or {}
    td = N.unit_types[target]
    gold = int(td.get('gold', 0) or 0)
    lumber = int(td.get('lumber', 0) or 0)
    dfused = int(td.get('fused', 0) or 0) - int(sd.get('fused', 0) or 0)
    dfmade = int(td.get('fmade', 0) or 0) - int(sd.get('fmade', 0) or 0)
    have = AC.unit_count(N, ai, target, False)
    limit = qty - have
    if gold > 0:
        limit = min(limit, pl.state.get(PLAYER_GOLD, 0) // gold)        # [obs]: 8
    if lumber > 0:
        limit = min(limit, pl.state.get(PLAYER_LUMBER, 0) // lumber)    # [obs]: 9
    if dfused > 0:
        cap = min(pl.state.get(4, 0), pl.state.get(6, 0), N.eng.food_ceiling)
        limit = min(limit, max(0, cap - pl.state.get(5, 0)) // dfused)  # [obs]: needs the food difference free
    if limit <= 0:
        return False
    morphed = 0
    for u in N.units:
        if morphed >= limit:
            break
        if u.owner is not pl or u.tid != tid or not _live(u) or u.constructing:
            continue
        if _morph_target(N, u) != target:
            continue
        N.eng.morph_type(u, target, food=False)           # the food difference is charged below (both VMs alike)
        morphed += 1
    if morphed == 0:
        return False
    if gold:
        N.set_player_state(pl, PLAYER_GOLD, pl.state.get(PLAYER_GOLD, 0) - gold * morphed)
    if lumber:
        N.set_player_state(pl, PLAYER_LUMBER, pl.state.get(PLAYER_LUMBER, 0) - lumber * morphed)
    if dfused or dfmade:
        N.food_add(pl, dfused * morphed, dfmade * morphed)
    return True


def purchase_zeppelin(N, ai):
    if ai is None or not ai.cp_on:
        return
    CP._home(N, ai)
    gx, gy = CP._gather(ai, 0)
    eng = N.eng
    best = None
    bd = 0.0
    for u in N.units:
        if not _live(u) or getattr(u, 'shop', None) is None or eng.shops.find(u, NZEP, True) is None:
            continue
        if eng.shops.code(u, ai.slot, NZEP) != 0:
            continue
        d = CP._d2(u.x, u.y, gx, gy)
        if best is None or d < bd:
            best = u
            bd = d
    if best is not None:
        eng.post_input(ai.slot, 5, best.serial, NZEP)
        ai.ex_zep += 1


# ------------------------------------------------------------------------------------------------ heroes
def hero_learned(u):
    n = 0
    for a in u.hslots:
        n += u.abilities.get(a, 0)
    return n


def hero_id(N, ai):
    if ai is None or ai.hero_cur == 0:
        return 0
    u = N.eng.by_serial.get(ai.hero_cur)
    return u.tid if _live(u) else 0


def hero_level(N, ai):
    if ai is None or ai.hero_cur == 0:
        return 0
    u = N.eng.by_serial.get(ai.hero_cur)
    return hero_learned(u) + 1 if _live(u) else 0


def hero_targets(N, ai):
    """the player's heroes with an unspent skill point, creation order, at most HERO_MAX serials (a list; reference only)."""
    pl = N.players[ai.slot]
    out = []
    for u in N.units:
        if u.owner is pl and _live(u) and getattr(u, 'hero', False) and not getattr(u, 'illusion', False) and u.skill > 0:
            out.append(u)
            if len(out) >= HERO_MAX:
                break
    return out


# ------------------------------------------------------------------------------------------------ parity digest
def digest_lines(N, slot):
    ai = N.ais.get(slot)
    if ai is None:
        return []
    s = 0
    for k in range(ai.gp_n):
        s += ai.gp_ser[k]
    out = ['ai ext %d gp %d %d learn %d zep %d inj %d sie %d tele %d' % (
        slot, ai.gp_n, s, ai.ex_learn, ai.ex_zep, ai.ex_inj, ai.ex_sie, ai.ex_tele)]
    if SPL.AI_CAST:
        out.append('ai cast %d n %d' % (slot, ai.cs_n))                 # AI-CAST: only with the switch on (the default digest keeps its shape)
    if MIC.AI_MICRO:
        out.append('ai micro %d rep %d flee %d take %d use %d' % (slot, ai.mi_rep, ai.mi_flee, ai.mi_take, ai.mi_use))
    return out
