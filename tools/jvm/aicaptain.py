#!/usr/bin/env python3
"""AI captains: the attack and defense groups of a computer player (docs/ai_plan.md S5a, groups G4 / G5 and the part of
G6 the attack thread needs).  Natives CreateCaptains / SetCaptainHome / ResetCaptainLocs / ShiftTownSpot /
ClearCaptainTargets / SetCaptainChanges / GroupTimedLife / SetSmartArtillery / SetStagePoint / CaptainAttack / CaptainVsUnits /
CaptainVsPlayer / CaptainGoHome / CaptainIsHome / CaptainIsFull / CaptainIsEmpty / CaptainGroupSize / CaptainReadiness /
CaptainReadinessHP / CaptainReadinessMa / CaptainRetreating / CaptainInCombat / CaptainAtGoal / AttackMoveXY / AttackMoveKill,
InitAssault / AddAssault / AddDefenders, and the intel the scripts ask before a wave: GetCreepCamp / StartGetEnemyBase /
WaitGetEnemyBase / GetEnemyBase / TownThreatened / GetMegaTarget / SetAllianceTarget / GetAllianceTarget / CreepsOnMap, plus the
engine-side captain pass.  Everything goes through the existing paths: a decision is a player input (engine.post_input kind 1
O_ATTACK = attack-move, O_MOVE, kind 2 O_ATTACK on a target), validated and executed by the order, pathing and combat code that
exist; there is no new movement.  Udon twin: unity/Assets/W3VR/Scripts/W3AiCaptain.cs.  Clean room: behaviour only,
[obs] = seen in a decompile chapter, [inf] = inferred (formation geometry, the engage rule and every number below are open).

Model (chapters: Unclassed_Native_0002 s2, Unclassed_Native_0003 s1, Unclassed_AI_0002 s4 / s7, Unclassed_AI_0009 s1,
CCaptainAI_p1 s0.1 / s3.8 / s4.2 / s4.8 / s5.9, CCaptainAI_p2 s7.3):

  * Two captains per AI player, created by CreateCaptains [obs 0002 s4: type 2 then type 1]: 0 = attack (type 1), 1 = defense
    (type 2).  The melee scripts only act on the attack captain; natives without a captain argument address it.  A captain is a
    set of up to CP_MEM unit serials (members), a table of wanted types, a home point and a goal.
  * InitAssault [obs 0002 s2.9: a one-way "assault mode" switch that CaptainIsFull reads] starts a new request: the wanted types
    are cleared (the members stay) and assault mode is on.  AddAssault(qty, type) [obs 0002 s2.10, 0002 AI s7: hands up to n idle
    units of a type to the captain] records the want and recruits idle own units of that type (creation order) until qty are
    members; members of the type above qty are released again [inf]; answers whether qty members exist.  AddDefenders does the same
    for the defense captain.  While a wave is under way (attacking or retreating) the composition is frozen: both calls only
    answer [inf: CCaptainAI_p1 s3.8, a unit meeting a running wave goes to the second-group slot].
  * CaptainIsFull [obs 0002 s2.13: flag set by InitAssault; CCaptainAI_p1 s3.3 compares the unit counts of the wave to decide
    "full"]: assault mode on, every wanted type has its quantity, and the members stand within GATHER_R of the gather point (stage
    point, else home) [inf]; members that have not arrived after GATHER_WAIT steps count as arrived [inf, so a stuck unit cannot
    hold the script's gather loop for ever].  Idle members farther than GATHER_NEAR from it are sent there by the tick.
  * CaptainReadiness* [obs 0002 s2.12 + CCaptainAI_p2 s7.3: a figure over the army from life and mana]: HP = 100 * sum of life /
    sum of max life, Ma = the same over the members that have mana (100 if none), both = their mean; integers, 0 for an empty
    captain [inf: scale and weights are open].
  * AttackMoveKill(unit) / AttackMoveXY(x, y) / CaptainAttack(x, y) [obs 0003 s1.1: store the target, state 2, clear the
    attacking bit; 0002 s2.2]: set the goal, launch the wave (members attack-move to the goal; the wave size is recorded) and set
    state 1 ("in combat" for CaptainInCombat: the goal is set and members exist).  A call with the same target again changes
    nothing (the scripts re-issue it every 3 s).  The tick (every CP_EVERY steps) re-sends idle members every CP_REORDER steps, a
    member that is idle within ENGAGE_R of the target unit attacks it directly, a point goal is reached when at least half of the
    members are within GOAL_R of it, a unit goal is done when the unit is dead [inf]; a finished goal clears the state (the
    members walk back to the gather point).
  * Progress [obs CCaptainAI_p1 s4.8 step 5: a counter of checks in which the units do not move; at 3 the captain goes home]: every
    STUCK_WIN steps the wave centroid is sampled (the launch position is the first reference); STUCK_N samples in a row that moved
    less than 50 while nobody is fighting end the wave with a retreat, and the position of its goal is remembered as unreachable
    (GetCreepCamp skips a camp there for BAD_STEPS).
  * Retreat [obs CCaptainAI_p1 s4.2 / s5.9: a running wave aborts when half its launch size exceeds the members left]: the wave
    retreats when its size / 2 > the members left, when HP readiness <= RET_HP, or after CP_MAX_ATTACK steps [inf for the last two].
    State 2: members move home
    (CaptainRetreating true), the state ends when every member is within HOME_R of home or after CP_MAX_RET steps.
  * CaptainGoHome = a retreat; ClearCaptainTargets drops the goal (state 0); CaptainIsHome = no attack under way and all members
    within HOME_R of the point they return to: home while retreating, the gather point (stage point if set, else home) while idle,
    so a script waiting on it cannot hang when a stage point is far from home [inf] (true for an empty captain);
    CaptainAtGoal = latch set by a finished goal, cleared by the next order.
  * Stored but not consumed yet [inf]: the SetCaptainChanges / SetSmartArtillery / GroupTimedLife bits (cp_flags), the
    CaptainVsUnits / CaptainVsPlayer mark (cp_vs), the wants of the defense captain (AddDefenders records them, nothing reads
    them), and the alliance target (al_tgt).
  * Defense captain [inf, no melee script uses it]: when a hostile (enemy, not creep) non-building unit is within TH_R of a town
    anchor (TownThreatened, scanned every pass) the idle members attack-move to it; when it is gone they go home.
  * GetCreepCamp(min, max, third) [obs Unclassed_AI_0009 s1 / Unclassed_Native_0003 s1.2: the nearest camp, preferred = level >=
    min [obs], rejected = level > max [obs]; a rejected camp pulls the band in to its distance + 200 [obs]].  Not decompile-backed
    [inf]: the clustering (live neutral hostile non-building units, CAMP_R around the first member's guard point, creation order;
    the original scans an engine camp table), the level as the sum of the type levels, the straight distance from the gather
    point (the original uses a path distance), and the flyer rule "a flyer in the camp is rejected unless the third argument is
    set".  OPEN QUESTION: the chapters disagree on the third argument (AI_0009 s1: allowEmpty, Native_0003 s1.2: onlyEmpty), so
    the flyer reading is a guess kept only because the scripts pass a constant; the answer is the camp unit nearest to the
    gather point.  StartGetEnemyBase picks the nearest enemy town hall (else any enemy structure) at once [inf]; WaitGetEnemyBase is
    false [inf]; GetMegaTarget is null [inf: the weak-player trigger is open].
"""
import math

from . import aibuild as AB
from . import aicensus as AC
from .ai import CP_MEM, CP_WANT, CP_BAD_N, FLAG_IGNORE_INJURED, is_fleeing, switch
from .engine import O_ATTACK, O_MOVE

CP_EVERY = 40                  # steps between two captain passes (0.2 s) [inf]
CP_REORDER = 200               # steps before a member gets another order (1 s) [inf]
CP_MAX_ATTACK = 120000         # a wave older than this retreats (600 s) [inf]
CP_MAX_RET = 60000             # a retreat older than this ends (300 s) [inf]
GATHER_R2 = 900.0 * 900.0
GATHER_NEAR2 = 300.0 * 300.0
GATHER_WAIT = 2400             # steps (12 s)
HOME_R2 = 700.0 * 700.0
GOAL_R2 = 600.0 * 600.0
ENGAGE_R2 = 900.0 * 900.0
FIGHT_R2 = 700.0 * 700.0       # a hostile this close to a member keeps a finished unit goal open [inf]
TH_R2 = 1000.0 * 1000.0
RET_HP = 30
STUCK_WIN = 1000               # steps between two progress samples of a wave (5 s) [inf]
STUCK_N = 3                    # samples in a row without progress that send the wave home [obs CCaptainAI_p1 s4.8: counter >= 3]
STUCK_D2 = 50.0 * 50.0         # the wave centroid moved less than this between two samples [inf]
BAD_STEPS = 120000             # a goal the wave could not reach is skipped by GetCreepCamp for 600 s [inf]
CAMP_R2 = 600.0 * 600.0
CAMP_MAX = 48
CAMP_BAND = 200.0
ACQ_R2 = 5000.0 * 5000.0       # PORT8: CCompAI_p1 s6.5 "best distance 5000" for the AI target scan [obs]
NEUTRAL_HOSTILE = 12
AI_DEFEND = switch('W3_AI_DEFEND')   # AI-DEFEND (docs/ai_plan.md): a running wave turns back on a strong threat at home; default OFF
THW_R2 = 700.0 * 700.0         # hostiles within this of the threat count for its weight [inf; the chapter's enumeration radius is a constant it does not name]
THW_MIN = 8                    # a weight above this alerts the attack captain [obs Unclassed_AI_0003 s10: accumulator > 8]
THW_ALLY = 20                  # ... and the captains of the allied AI players above this [obs s10 step 8: accumulator > 20]
DEF_MIN = 3                    # ... if it has more than 2 members [obs s8 / s10: wave size > 2]
DEF_REORDER = 600              # a defending member is re-ordered at least every 3 s [inf]
INJ_PCT = 50.0                 # S6 (aiextra.py): a unit below this percent of its life is injured [inf]

# per-call camp scratch (module level in the reference; fixed arrays in the Udon twin)
_camp_ax = [0.0] * CAMP_MAX
_camp_ay = [0.0] * CAMP_MAX
_camp_lv = [0] * CAMP_MAX
_camp_fl = [False] * CAMP_MAX
_camp_u = [None] * CAMP_MAX
_camp_d = [0.0] * CAMP_MAX
_tax = [0.0] * AC.TOWN_N                     # TownThreatened scratch: the live town anchors
_tay = [0.0] * AC.TOWN_N


def _live(u):
    # PORT8: a consumed night-elf builder (inside an Ancient) is out of the AI's population, as in aicensus._live.
    return u is not None and u.alive and not u.dead and not getattr(u, 'consumed', False)


def _d2(ax, ay, bx, by):
    dx = ax - bx
    dy = ay - by
    return dx * dx + dy * dy


# ------------------------------------------------------------------------------------------------ members
def _prune(N, ai, a):
    """forget members that are gone (dead, removed, no longer the player's)."""
    by = N.eng.by_serial
    pl = N.players[ai.slot]
    for i in range(CP_MEM):
        s = ai.cp_mem[a * CP_MEM + i]
        if s == 0:
            continue
        u = by.get(s)
        if not _live(u) or u.owner is not pl:
            ai.cp_mem[a * CP_MEM + i] = 0


def _count(N, ai, a):
    n = 0
    for i in range(CP_MEM):
        if ai.cp_mem[a * CP_MEM + i] != 0:
            n += 1
    return n


def _is_member(ai, serial):
    for k in range(2 * CP_MEM):
        if ai.cp_mem[k] == serial:
            return True
    return False


def _count_type(N, ai, a, tid):
    n = 0
    by = N.eng.by_serial
    for i in range(CP_MEM):
        s = ai.cp_mem[a * CP_MEM + i]
        if s != 0:
            u = by.get(s)
            if u is not None and u.tid == tid:
                n += 1
    return n


def injured(u):
    return u.maxlife > 0.0 and u.life * 100.0 < u.maxlife * INJ_PCT


def is_post_unit(ai, serial):
    """a unit standing on a guard post (aiextra.py) is not recruited."""
    for k in range(ai.gp_n):
        if ai.gp_ser[k] == serial:
            return True
    return False


def _recruitable(N, u, pl):
    """an idle own unit that is no structure, worker on a trip, builder or hidden unit [obs 0002 AI s7: idle states only]."""
    return (u.owner is pl and u.alive and not u.dead and not getattr(u, 'consumed', False) and not u.is_bldg
            and not u.constructing and u.build is None and not getattr(u, 'hidden', False) and u.order == 0
            and AB.hv_res(N, u) < 0)


def _release_extra(N, ai, a, tid, keep):
    """release the members of a type above `keep` (highest entries first)."""
    by = N.eng.by_serial
    have = _count_type(N, ai, a, tid)
    i = CP_MEM - 1
    while have > keep and i >= 0:
        s = ai.cp_mem[a * CP_MEM + i]
        if s != 0:
            u = by.get(s)
            if u is not None and u.tid == tid:
                ai.cp_mem[a * CP_MEM + i] = 0
                have -= 1
        i -= 1


def _recruit(N, ai, a, tid, qty):
    pl = N.players[ai.slot]
    have = _count_type(N, ai, a, tid)
    if have >= qty:
        return
    for u in N.units:
        if have >= qty:
            break
        if u.tid != tid or not _recruitable(N, u, pl) or _is_member(ai, u.serial) or is_post_unit(ai, u.serial) or is_fleeing(ai, u.serial):
            continue                                        # AI-MICRO: a hero running home is not recruited
        if (ai.flags & FLAG_IGNORE_INJURED) and injured(u):
            continue                                        # S6: SetIgnoreInjured - injured units are not recruited
        for i in range(CP_MEM):
            if ai.cp_mem[a * CP_MEM + i] == 0:
                ai.cp_mem[a * CP_MEM + i] = u.serial
                ai.cp_mord[a * CP_MEM + i] = -1000000
                have += 1
                break
        else:
            return                                          # the captain is full


# ------------------------------------------------------------------------------------------------ home / gather
def reset_locs(N, ai):
    """ResetCaptainLocs: both home points = the first town anchor, else the first own unit [inf]."""
    AC.refresh_towns(N, ai)
    hx = hy = 0.0
    have = False
    for i in range(AC.TOWN_N):
        an = AC._anchor(N, ai, i)
        if an is not None and not have:
            hx, hy = an
            have = True
    if not have:
        pl = N.players[ai.slot]
        for u in N.units:
            if not have and u.owner is pl and _live(u):
                hx, hy = u.x, u.y
                have = True
    for a in range(2):
        ai.cp_hx[a] = hx
        ai.cp_hy[a] = hy
    ai.cp_hset = True


def _home(N, ai):
    if not ai.cp_hset:
        reset_locs(N, ai)


def _gather(ai, a):
    if ai.cp_sset[a]:
        return ai.cp_sx[a], ai.cp_sy[a]
    return ai.cp_hx[a], ai.cp_hy[a]


def create(N, ai):
    """CreateCaptains."""
    if ai is None or ai.cp_on:
        return
    ai.cp_on = True
    reset_locs(N, ai)


def set_home(N, ai, which, x, y):
    if ai is None or not ai.cp_on:
        return
    for a in range(2):
        if which == 3 or which == a + 1:
            ai.cp_hx[a] = x
            ai.cp_hy[a] = y
    ai.cp_hset = True


def set_flag(ai, bit, on):
    if ai is None:
        return
    if on:
        ai.cp_flags |= bit
    else:
        ai.cp_flags &= ~bit


def set_vs(ai, v):
    if ai is not None:
        ai.cp_vs[0] = v


def set_stage(N, ai, x, y):
    if ai is None or not ai.cp_on:
        return
    ai.cp_sx[0] = x
    ai.cp_sy[0] = y
    ai.cp_sset[0] = True


# ------------------------------------------------------------------------------------------------ assault
def init_assault(N, ai):
    if ai is None or not ai.cp_on:
        return
    ai.cp_asl[0] = True
    if ai.cp_state[0] == 0:
        ai.cp_wn[0] = 0


def add_assault(N, ai, a, qty, tid):
    """AddAssault (a = 0) / AddDefenders (a = 1): record the want, recruit idle units; True when qty members of the type exist."""
    if ai is None or not ai.cp_on:
        return False
    _home(N, ai)
    ai.cp_asl[a] = True
    _prune(N, ai, a)
    if ai.cp_state[a] != 0 or qty <= 0:
        return _count_type(N, ai, a, tid) >= qty
    n = ai.cp_wn[a]
    k = -1
    for i in range(n):
        if ai.cp_wt[a * CP_WANT + i] == tid:
            k = i
    if k < 0 and n < CP_WANT:
        k = n
        ai.cp_wn[a] = n + 1
        ai.cp_wt[a * CP_WANT + k] = tid
    if k >= 0:
        ai.cp_wq[a * CP_WANT + k] = qty
    _release_extra(N, ai, a, tid, qty)
    _recruit(N, ai, a, tid, qty)
    return _count_type(N, ai, a, tid) >= qty


def _gathered(N, ai, a):
    gx, gy = _gather(ai, a)
    by = N.eng.by_serial
    for i in range(CP_MEM):
        s = ai.cp_mem[a * CP_MEM + i]
        if s != 0:
            u = by.get(s)
            if u is not None and _d2(u.x, u.y, gx, gy) > GATHER_R2:
                return False
    return True


def is_full(N, ai):
    if ai is None or not ai.cp_on or not ai.cp_asl[0]:
        return False
    _prune(N, ai, 0)
    for i in range(ai.cp_wn[0]):
        if _count_type(N, ai, 0, ai.cp_wt[i]) < ai.cp_wq[i]:
            ai.cp_wait[0] = -1
            return False
    if _gathered(N, ai, 0):
        ai.cp_wait[0] = -1
        return True
    steps = N.vm.steps
    if ai.cp_wait[0] < 0:
        ai.cp_wait[0] = steps
    return steps - ai.cp_wait[0] >= GATHER_WAIT


def group_size(N, ai):
    if ai is None or not ai.cp_on:
        return 0
    _prune(N, ai, 0)
    return _count(N, ai, 0)


def readiness(N, ai, hp, ma):
    """CaptainReadiness (1, 1) / HP (1, 0) / Ma (0, 1): integer percent over the attack captain."""
    if ai is None or not ai.cp_on:
        return 0
    _prune(N, ai, 0)
    by = N.eng.by_serial
    lf = lm = mf = mm = 0.0
    n = 0
    for i in range(CP_MEM):
        s = ai.cp_mem[i]
        if s == 0:
            continue
        u = by.get(s)
        n += 1
        lf += u.life
        lm += u.maxlife
        if u.maxmana > 0.0:
            mf += u.mana
            mm += u.maxmana
    if n == 0:
        return 0
    php = int(100.0 * lf / lm) if lm > 0.0 else 0
    pma = int(100.0 * mf / mm) if mm > 0.0 else 100
    if hp and ma:
        return (php + pma) // 2
    return php if hp else pma


def in_combat(N, ai, attack):
    if ai is None or not ai.cp_on:
        return False
    a = 0 if attack else 1
    return ai.cp_state[a] == 1 and _count(N, ai, a) > 0


def retreating(ai):
    return ai is not None and ai.cp_on and ai.cp_state[0] == 2


def is_home(N, ai):
    if ai is None or not ai.cp_on:
        return False
    if ai.cp_state[0] == 1:
        return False
    _prune(N, ai, 0)
    if ai.cp_state[0] == 2:                      # retreating: the members walk to home
        px, py = ai.cp_hx[0], ai.cp_hy[0]
    else:                                        # idle: they stand at the gather point (stage point, else home)
        px, py = _gather(ai, 0)
    by = N.eng.by_serial
    for i in range(CP_MEM):
        s = ai.cp_mem[i]
        if s != 0:
            u = by.get(s)
            if _d2(u.x, u.y, px, py) > HOME_R2:
                return False
    return True


# ------------------------------------------------------------------------------------------------ orders
def _send_all(N, ai, a, steps, oid, x, y):
    """order every member (kind 1 point order) and stamp it."""
    by = N.eng.by_serial
    for i in range(CP_MEM):
        s = ai.cp_mem[a * CP_MEM + i]
        if s != 0 and by.get(s) is not None:
            N.eng.post_input(ai.slot, 1, s, oid, x, y)
            ai.cp_mord[a * CP_MEM + i] = steps


def _goal(N, ai, steps, tgt_serial, x, y):
    """AttackMoveKill / AttackMoveXY / CaptainAttack: launch or re-aim the attack wave."""
    if ai is None or not ai.cp_on:
        return
    _home(N, ai)
    _prune(N, ai, 0)
    n = _count(N, ai, 0)
    st = ai.cp_state[0]
    if st == 2 or n == 0:
        return
    if st == 1 and ai.cp_tgt[0] == tgt_serial and (tgt_serial != 0 or (ai.cp_gx[0] == x and ai.cp_gy[0] == y)):
        return
    if st == 0:
        ai.cp_size[0] = n
        ai.cp_waves += 1
        ai.cp_sent += n
        if ai.cp_first < 0:
            ai.cp_first = steps
        ai.cp_gstep[0] = steps
    ai.cp_state[0] = 1
    ai.cp_tgt[0] = tgt_serial
    ai.cp_gx[0] = x
    ai.cp_gy[0] = y
    ai.cp_atg[0] = False
    ai.cp_wait[0] = -1
    ai.cp_pstep = steps
    ai.cp_stuck = 0
    sx = sy = 0.0                               # the launch centroid is the reference of the first progress sample
    for i in range(CP_MEM):
        s = ai.cp_mem[i]
        if s != 0:
            u = N.eng.by_serial.get(s)
            sx += u.x
            sy += u.y
    ai.cp_px = sx / n
    ai.cp_py = sy / n
    ai.cp_fstep = -1
    N.eng.log('ai wave p%d %d units -> %d,%d tgt %d' % (ai.slot, n, int(x), int(y), tgt_serial))
    _send_all(N, ai, 0, steps, O_ATTACK, x, y)


def attack_move_kill(N, ai, target):
    if ai is None or not ai.cp_on or not _live(target):
        return
    _goal(N, ai, N.vm.steps, target.serial, target.x, target.y)


def attack_move_xy(N, ai, x, y):
    _goal(N, ai, N.vm.steps, 0, float(x), float(y))


def go_home(N, ai):
    """CaptainGoHome: a retreat of the attack captain (nothing to retreat from when it is idle and empty)."""
    if ai is None or not ai.cp_on:
        return
    _home(N, ai)
    _prune(N, ai, 0)
    if ai.cp_state[0] == 2 or _count(N, ai, 0) == 0:
        return
    _retreat(N, ai, N.vm.steps)


def clear_targets(N, ai):
    if ai is None or not ai.cp_on:
        return
    if ai.cp_state[0] == 1:
        ai.cp_state[0] = 0
        ai.cp_size[0] = 0
    ai.cp_tgt[0] = 0


def _retreat(N, ai, steps):
    ai.cp_state[0] = 2
    ai.cp_tgt[0] = 0
    ai.cp_gstep[0] = steps
    ai.cp_atg[0] = False
    N.eng.log('ai retreat p%d' % ai.slot)
    _send_all(N, ai, 0, steps, O_MOVE, ai.cp_hx[0], ai.cp_hy[0])


def _finish(N, ai, steps):
    """the goal is done: idle state, atgoal latch, members walk back to the gather point."""
    ai.cp_state[0] = 0
    ai.cp_size[0] = 0
    ai.cp_tgt[0] = 0
    ai.cp_atg[0] = True
    gx, gy = _gather(ai, 0)
    _send_all(N, ai, 0, steps, O_ATTACK, gx, gy)


# ------------------------------------------------------------------------------------------------ the captain pass
def _threat_scan(N, ai):
    """TownThreatened: the nearest hostile non-creep, non-structure unit within TH_R of a town anchor."""
    pl = N.players[ai.slot]
    AC.refresh_towns(N, ai)
    eng = N.eng
    found = False
    bd = 0.0
    bt = 0
    bx = by_ = 0.0
    nt = 0
    for t in range(AC.TOWN_N):                   # the live town anchors, in town order
        an = AC._anchor(N, ai, t)
        if an is not None:
            _tax[nt] = an[0]
            _tay[nt] = an[1]
            nt += 1
    if nt > 0:
        for u in N.units:                        # unit-major: one pass, the nearest anchor of each hostile
            if u.owner.id >= 12 or u.owner is pl or u.is_bldg or not _live(u) or getattr(u, 'hidden', False):
                continue
            if eng.ally(pl, u.owner):
                continue
            ud = 0.0
            ut = -1
            for t in range(nt):
                d = _d2(u.x, u.y, _tax[t], _tay[t])
                if d <= TH_R2 and (ut < 0 or d < ud):
                    ud = d
                    ut = t
            # the nearest (distance, town, creation order) over all hostiles: the result of the town-major scan
            if ut >= 0 and (not found or ud < bd or (ud == bd and ut < bt)):
                found = True
                bd = ud
                bt = ut
                bx = u.x
                by_ = u.y
    ai.cp_thr = found
    if found:
        ai.cp_tx = bx
        ai.cp_ty = by_
    ai.cp_thw = _threat_weight(N, ai, bx, by_) if (found and AI_DEFEND) else 0


def _threat_weight(N, ai, x, y):
    """Unclassed_AI_0003 s11 [obs]: the threat weight of the hostile units around the spotted target: a hero counts (level + 1) / 2 + 4,
    every other unit its level.  The enumeration of s10 (hostile units of other players near the target) is the hostile non-structure
    units of the scan within THW_R of the threat point [inf]."""
    pl = N.players[ai.slot]
    eng = N.eng
    w = 0
    for u in N.units:
        if u.owner.id >= 12 or u.owner is pl or u.is_bldg or not _live(u) or getattr(u, 'hidden', False) or eng.ally(pl, u.owner):
            continue
        if _d2(u.x, u.y, x, y) > THW_R2:
            continue
        if u.hero:
            w += (u.hlevel + 1) // 2 + 4
        else:
            w += int(u.data.get('level', 0) or 0)
    return w


def _defend_point(N, ai):
    """AI-DEFEND: where the attack wave is wanted at home: the own threat above THW_MIN, else the threat of the first allied AI player
    (slots ascending) whose weight is above THW_ALLY [obs Unclassed_AI_0003 s10 steps 8-9]; None for none."""
    if ai.cp_thr and ai.cp_thw > THW_MIN:
        return ai.cp_tx, ai.cp_ty
    pl = N.players[ai.slot]
    for s2 in sorted(N.ais):
        a2 = N.ais[s2]
        if s2 == ai.slot or a2 is None or not a2.cp_on or not a2.cp_thr or a2.cp_thw <= THW_ALLY:
            continue
        if N.eng.ally(pl, N.players[s2]):
            return a2.cp_tx, a2.cp_ty
    return None


def _enemy_near(N, ai, x, y):
    """a live hostile non-structure (creep or enemy player's unit) within FIGHT_R of (x, y)."""
    pl = N.players[ai.slot]
    for v in N.units:
        if v.is_bldg or not _live(v) or v.owner is pl or getattr(v, 'hidden', False):
            continue
        if v.owner.id == NEUTRAL_HOSTILE or (v.owner.id < 12 and not N.eng.ally(pl, v.owner)):
            if _d2(v.x, v.y, x, y) <= FIGHT_R2:
                return True
    return False


def _fighting(N, ai, steps):
    """some member has a hostile within FIGHT_R; computed at most once per captain pass (the unit positions do not change
    inside a pass, so the answer is the same for every caller of that pass)."""
    if ai.cp_fstep == steps:
        return ai.cp_fval
    by = N.eng.by_serial
    r = False
    for i in range(CP_MEM):
        s = ai.cp_mem[i]
        if s != 0 and not r:
            u = by.get(s)
            if _enemy_near(N, ai, u.x, u.y):
                r = True
    ai.cp_fstep = steps
    ai.cp_fval = r
    return r


def _tick_attack(N, ai, steps):
    a = 0
    _prune(N, ai, a)
    n = _count(N, ai, a)
    by = N.eng.by_serial
    st = ai.cp_state[a]
    gx, gy = _gather(ai, a)
    if st == 0:
        for i in range(CP_MEM):                     # gather: idle members far from the point walk there
            s = ai.cp_mem[i]
            if s == 0:
                continue
            u = by.get(s)
            if u.order == 0 and steps - ai.cp_mord[i] >= CP_REORDER and _d2(u.x, u.y, gx, gy) > GATHER_NEAR2:
                N.eng.post_input(ai.slot, 1, s, O_ATTACK, gx, gy)
                ai.cp_mord[i] = steps
        return
    if st == 1:
        if n == 0:
            ai.cp_state[a] = 0
            ai.cp_size[a] = 0
            ai.cp_tgt[a] = 0
            return
        if ai.cp_size[a] // 2 > n or readiness(N, ai, True, False) <= RET_HP or steps - ai.cp_gstep[a] > CP_MAX_ATTACK:
            _retreat(N, ai, steps)
            return
        if steps - ai.cp_pstep >= STUCK_WIN:
            sx = sy = 0.0
            for i in range(CP_MEM):
                s = ai.cp_mem[i]
                if s != 0:
                    u = by.get(s)
                    sx += u.x
                    sy += u.y
            cx = sx / n
            cy = sy / n
            if _d2(cx, cy, ai.cp_px, ai.cp_py) < STUCK_D2 and not _fighting(N, ai, steps):
                ai.cp_stuck += 1
            else:
                ai.cp_stuck = 0
            ai.cp_px = cx
            ai.cp_py = cy
            ai.cp_pstep = steps
            if ai.cp_stuck >= STUCK_N:
                ai.cp_stuck = 0
                k = ai.cp_bn % CP_BAD_N
                ai.cp_bn += 1
                ai.cp_bx[k] = ai.cp_gx[a]
                ai.cp_by[k] = ai.cp_gy[a]
                ai.cp_bu[k] = steps + BAD_STEPS
                N.eng.log('ai stuck p%d goal %d,%d' % (ai.slot, int(ai.cp_gx[a]), int(ai.cp_gy[a])))
                _retreat(N, ai, steps)
                return
        dp = _defend_point(N, ai) if (AI_DEFEND and n >= DEF_MIN) else None
        if dp is not None:
            first = not ai.cp_dfd                       # AI-DEFEND: the base (or an ally's, weight above 20) is under strong attack, the wave turns back
            if first:
                ai.cp_dfd = True
                ai.cp_dn += 1
            for i in range(CP_MEM):
                s = ai.cp_mem[i]
                if s == 0:
                    continue
                u = by.get(s)
                if first or steps - ai.cp_mord[i] >= DEF_REORDER or (u.order == 0 and steps - ai.cp_mord[i] >= CP_REORDER):
                    N.eng.post_input(ai.slot, 1, s, O_ATTACK, dp[0], dp[1])
                    ai.cp_mord[i] = steps
            return
        if ai.cp_dfd:
            ai.cp_dfd = False                           # the threat is gone: the wave goes on to its goal
            for i in range(CP_MEM):
                s = ai.cp_mem[i]
                if s != 0:
                    N.eng.post_input(ai.slot, 1, s, O_ATTACK, ai.cp_gx[a], ai.cp_gy[a])
                    ai.cp_mord[i] = steps
        tgt = by.get(ai.cp_tgt[a]) if ai.cp_tgt[a] != 0 else None
        if ai.cp_tgt[a] != 0:
            if not _live(tgt):
                if not _fighting(N, ai, steps):
                    _finish(N, ai, steps)
                    return
                tgt = None                          # the fight goes on at the last position of the target
            else:
                ai.cp_gx[a] = tgt.x
                ai.cp_gy[a] = tgt.y
        else:
            near = 0
            for i in range(CP_MEM):
                s = ai.cp_mem[i]
                if s != 0:
                    u = by.get(s)
                    if _d2(u.x, u.y, ai.cp_gx[a], ai.cp_gy[a]) <= GOAL_R2:
                        near += 1
            if 2 * near >= n:
                _finish(N, ai, steps)
                return
        for i in range(CP_MEM):
            s = ai.cp_mem[i]
            if s == 0:
                continue
            u = by.get(s)
            if u.order != 0 or steps - ai.cp_mord[i] < CP_REORDER:
                continue
            if tgt is not None and _d2(u.x, u.y, tgt.x, tgt.y) <= ENGAGE_R2:
                N.eng.post_input(ai.slot, 2, s, O_ATTACK, 0.0, 0.0, tgt.serial)
            else:
                N.eng.post_input(ai.slot, 1, s, O_ATTACK, ai.cp_gx[a], ai.cp_gy[a])
            ai.cp_mord[i] = steps
        return
    # retreating
    away = 0
    for i in range(CP_MEM):
        s = ai.cp_mem[i]
        if s == 0:
            continue
        u = by.get(s)
        if _d2(u.x, u.y, ai.cp_hx[a], ai.cp_hy[a]) > HOME_R2:
            away += 1
            if steps - ai.cp_mord[i] >= CP_REORDER and u.order == 0:
                N.eng.post_input(ai.slot, 1, s, O_MOVE, ai.cp_hx[a], ai.cp_hy[a])
                ai.cp_mord[i] = steps
    if away == 0 or steps - ai.cp_gstep[a] > CP_MAX_RET:
        ai.cp_state[a] = 0
        ai.cp_size[a] = 0


def _tick_defense(N, ai, steps):
    a = 1
    _prune(N, ai, a)
    n = _count(N, ai, a)
    by = N.eng.by_serial
    if n == 0:
        ai.cp_state[a] = 0
        return
    if ai.cp_thr:
        ai.cp_state[a] = 1
        tx, ty = ai.cp_tx, ai.cp_ty
    else:
        if ai.cp_state[a] == 1:
            ai.cp_state[a] = 0
            for i in range(CP_MEM):
                ai.cp_mord[CP_MEM + i] = -1000000
        tx, ty = ai.cp_hx[a], ai.cp_hy[a]
    for i in range(CP_MEM):
        s = ai.cp_mem[CP_MEM + i]
        if s == 0:
            continue
        u = by.get(s)
        if u.order != 0 or steps - ai.cp_mord[CP_MEM + i] < CP_REORDER:
            continue
        if ai.cp_state[a] == 0 and _d2(u.x, u.y, tx, ty) <= GATHER_NEAR2:
            continue
        N.eng.post_input(ai.slot, 1, s, O_ATTACK, tx, ty)
        ai.cp_mord[CP_MEM + i] = steps


# ------------------------------------------------------------------------------------------------ AI target acquisition
def home_unit(N, ai):
    """CCompAI_p1 s7.9: the AI's home unit - the first own hall, else the current acquire target, else None.

    The chapter reads the two AI references +0x2f0/+0x2f4 (home) and +0x2fc/+0x300 (acquire target); the port has
    no such reference pair, so the home unit is the first finished hall of the player [inf]."""
    if ai is None or not ai.cp_on:
        return None
    pl = N.players[ai.slot]
    for u in N.units:
        if u.owner is pl and _live(u) and u.is_bldg and u.tid in AC.HALL_IDS:
            return u
    t = N.eng.by_serial.get(ai.acq_tgt) if ai.acq_tgt else None
    return t if _live(t) else None


def _idle_with_target(N, ai):
    """CCompAI_p1 s7.2: an idle own unit already has an attack target (the original's state-2 test).

    The original also checks the target's visibility flag and owner state; the port's units keep `atk_target` and
    the order `target`, so an idle unit with either is the answer [inf]."""
    pl = N.players[ai.slot]
    for u in N.units:
        if u.owner is pl and _live(u) and u.order == 0 and (u.atk_target is not None or u.target is not None):
            return True
    return False


def _hero_like(u):
    """Unclassed_AI_0003 s6: the type letter is A..Z and the illusion bit is clear (the same gate as AI_0001 s7)."""
    return u is not None and AB._is_hero_id(u.tid) and not getattr(u, 'illusion', False)


def _ability_class3(N, u):
    """Unclassed_AI_0003 s6: the unit's attack ability answers class 3.  The chapter's +0xf4/+0xf8 words are not
    decoded; the port reads the attack type of a weapon (3 = siege, the same value aiextra.SIEGE_ATK uses) [inf]."""
    eng = N.eng
    for i in (0, 1):
        if eng.has_weapon(u, i) and int(eng.wv(u, i, 'atkType') or 0) == 3:
            return True
    return False


def target_better(N, ai, cur, cand, home):
    """Unclassed_AI_0003 s6 target preference: hero-like first, then the ability class, then the distance to the
    captain/agent.  `home` is the port's captain/agent stand-in (home_unit); with no agent a candidate always wins."""
    if cur is None:
        return True
    if cand is None or cand is cur or cand.serial == cur.serial:
        return False
    ch = _hero_like(cur)
    dh = _hero_like(cand)
    if dh != ch:
        return dh
    if not dh:
        ca = _ability_class3(N, cur)
        da = _ability_class3(N, cand)
        if da != ca:
            return da
    if home is None:
        return True
    return _d2(cand.x, cand.y, home.x, home.y) < _d2(cur.x, cur.y, home.x, home.y)


def _target_pass(N, ai, steps):
    """CCompAI_p1 s6.3 target acquisition (model): pick the AI's current target and give the nearest idle fighter a
    direct attack order on it.

    [obs] the scan cap is 5000 (s6.5), the target must be visible to the AI (the chapter's "can see it" test, read
    through the engine's `order_visible` on the home unit) and the order is an attack on the chosen unit; the
    chapter's target reference and builder/state filters need fields the port does not keep [inf]. The target is the
    best visible hostile player unit to the AI's home unit under the Unclassed_AI_0003 s6 preference (hero-like,
    then ability class 3, then distance). The fighter sent is an idle own fighter (no current order) [inf]. Only runs
    while no attack wave is under way and no town is threatened (the captain owns the army then)."""
    if ai is None or not ai.cp_on:
        return
    if ai.cp_state[0] != 0 or ai.cp_thr:
        return
    if _idle_with_target(N, ai):
        return
    home = home_unit(N, ai)
    if home is None:
        return
    eng = N.eng
    by = eng.by_serial
    pl = N.players[ai.slot]
    tgt = by.get(ai.acq_tgt) if ai.acq_tgt else None
    if tgt is not None and not _live(tgt):
        ai.acq_tgt = 0
        tgt = None
    if tgt is None:
        # CCompAI_p1 s7.7: share a target with an allied AI player (the chapter copies it into the ally's
        # +0x2d4/+0x2d8 references); the port reads the ally's acquire target [inf for the 5000 range].
        for slot2 in sorted(N.ais):
            if slot2 == ai.slot:
                continue
            ai2 = N.ais[slot2]
            if ai2 is None or not ai2.cp_on or not eng.ally(pl, N.players[slot2]):
                continue
            t2 = by.get(ai2.acq_tgt) if ai2.acq_tgt else None
            if _live(t2) and _d2(t2.x, t2.y, home.x, home.y) <= ACQ_R2 and eng.order_visible(home, t2):
                tgt = t2
                ai.acq_tgt = t2.serial
                break
    if tgt is None:
        for u in N.units:
            if not _live(u) or u.is_bldg or getattr(u, 'hidden', False):
                continue
            if u.owner is pl or u.owner.id >= 12 or eng.ally(pl, u.owner):
                continue
            if not eng.order_visible(home, u):
                continue
            if _d2(u.x, u.y, home.x, home.y) >= ACQ_R2:      # CCompAI_p1 s6.5: best distance 5000
                continue
            if target_better(N, ai, tgt, u, home):
                tgt = u
        if tgt is None:
            return
        ai.acq_tgt = tgt.serial
    best = None
    bd = 0.0
    for u in N.units:
        if u.owner is not pl or not u.fighter or AB.worker_kind(N, u) >= 0 or not _recruitable(N, u, pl):
            continue
        if _is_member(ai, u.serial) or is_post_unit(ai, u.serial):
            continue
        if u.order != 0:                                    # only an idle unit gets the direct attack order
            continue
        if u.atk_target is not None or u.target is not None or getattr(u, 'gpt', None) is not None:
            continue
        d = _d2(u.x, u.y, tgt.x, tgt.y)
        if best is None or d < bd:
            best = u
            bd = d
    if best is None:
        return
    eng.post_input(ai.slot, 2, best.serial, O_ATTACK, 0.0, 0.0, tgt.serial)
    eng.log('ai acquire p%d u%d -> u%d' % (ai.slot, best.serial, tgt.serial))


def engine_tick(N, ai, steps):
    """the captain pass of one AI player (run by the host after the player's threads, slots ascending)."""
    if not ai.cp_on or steps - ai.cp_step < CP_EVERY:
        return
    ai.cp_step = steps
    _threat_scan(N, ai)
    _tick_attack(N, ai, steps)
    _tick_defense(N, ai, steps)
    _target_pass(N, ai, steps)


# ------------------------------------------------------------------------------------------------ intel
def town_threatened(ai):
    return ai is not None and ai.cp_on and ai.cp_thr


def creeps_on_map(N):
    for u in N.units:
        if u.owner.id == NEUTRAL_HOSTILE and _live(u) and not u.is_bldg:
            return True
    return False


def unit_invis(N, ai, u):
    """UnitInvis(unit) [obs 0002 s3.2]: the unit is invisible to the AI's player, i.e. it carries invisibility, is not
    the player's / an ally's unit and no point reveal or detector of the player covers it (the same test as
    IsUnitInvisible).  Outside an AI context the port keeps the plain invisibility counter [inf: the chapter's "local
    player" has no map-script meaning here]."""
    if u is None or u.dead or not u.alive or getattr(u, 'invisible', 0) <= 0:
        return False
    if ai is None:
        return True
    pl = N.players[ai.slot]
    if u.owner is pl or N.eng.ally(pl, u.owner):
        return False
    return not N.eng.ab.revealed_to(pl, u)


def creep_camp(N, ai, lo, hi, flyers_ok):
    """GetCreepCamp: the unit nearest to the gather point of the best camp (see the module header), else None."""
    if ai is None or not ai.cp_on:
        return None
    _home(N, ai)
    gx, gy = _gather(ai, 0)
    eng = N.eng
    nc = 0
    for u in N.units:
        if u.owner.id != NEUTRAL_HOSTILE or not _live(u) or u.is_bldg or getattr(u, 'hidden', False):
            continue
        px = u.guard_x if u.guard_x is not None else u.x
        py = u.guard_y if u.guard_y is not None else u.y
        k = -1
        for c in range(nc):
            if k < 0 and _d2(px, py, _camp_ax[c], _camp_ay[c]) <= CAMP_R2:
                k = c
        if k < 0:
            if nc >= CAMP_MAX:
                continue
            k = nc
            nc += 1
            _camp_ax[k] = px
            _camp_ay[k] = py
            _camp_lv[k] = 0
            _camp_fl[k] = False
            _camp_u[k] = None
            _camp_d[k] = 0.0
        _camp_lv[k] += int(u.data.get('level', 0) or 0)
        if eng.flying(u):
            _camp_fl[k] = True
        d = _d2(u.x, u.y, gx, gy)
        if _camp_u[k] is None or d < _camp_d[k]:
            _camp_u[k] = u
            _camp_d[k] = d
    steps = N.vm.steps
    for c in range(nc):                          # a camp where a wave could not get through is skipped for a while
        for k in range(CP_BAD_N):
            if ai.cp_bu[k] > steps and _d2(_camp_ax[c], _camp_ay[c], ai.cp_bx[k], ai.cp_by[k]) <= CAMP_R2:
                _camp_u[c] = None
    band = 999999.0
    bp = bs = None
    dp = ds = 999999.0
    for c in range(nc):
        if _camp_u[c] is None:
            continue
        dist = math.sqrt(_d2(_camp_ax[c], _camp_ay[c], gx, gy))
        if dist > band:
            continue
        if _camp_lv[c] > hi or (not flyers_ok and _camp_fl[c]):
            if dist < band - CAMP_BAND:
                band = dist + CAMP_BAND
                if band < dp:
                    bp = None
                    dp = 999999.0
                if band < ds:
                    bs = None
                    ds = 999999.0
        elif _camp_lv[c] >= lo:
            if dist < dp:
                bp = _camp_u[c]
                dp = dist
        else:
            if dist < ds:
                bs = _camp_u[c]
                ds = dist
    return bp if bp is not None else bs


EB_ORDER = (5, 2, 4, 1, 3, 0, 11, 8, 10, 7, 9, 6)      # CCaptainAI_p1 4.9 half B: the fixed enemy-slot priority
EB_UGOL = 0x75676F6C                                   # 'ugol': the haunted mine, not a "special" target
_eb_best = [0.0] * 12
_eb_ser = [0] * 12


def start_enemy_base(N, ai):
    """StartGetEnemyBase: the enemy objective at once.  CCaptainAI_p1 4.9: every known enemy is scored into one of 12
    slots (the captain's current target 2, another non-mine 8, a mine 6; +3 would be an unreachable one, which the port
    does not probe), the closest per slot to the gather point is kept, and the first non-empty slot in the fixed order
    5,2,4,1,3,0,11,8,10,7,9,6 wins [obs 0x6f27f290 / 0x6f2efdd0].  The candidates are the enemy structures [inf]."""
    if ai is None or not ai.cp_on:
        return
    _home(N, ai)
    gx, gy = _gather(ai, 0)
    pl = N.players[ai.slot]
    eng = N.eng
    for i in range(12):
        _eb_ser[i] = 0
        _eb_best[i] = 0.0
    cur = ai.cp_tgt[0]
    for u in N.units:
        if not u.is_bldg or not _live(u) or u.owner.id >= 12 or u.owner is pl or eng.ally(pl, u.owner):
            continue
        sl = 2 if (cur != 0 and u.serial == cur) else (6 if u.tid == EB_UGOL else 8)
        d = _d2(u.x, u.y, gx, gy)
        if _eb_ser[sl] == 0 or d < _eb_best[sl]:
            _eb_ser[sl] = u.serial
            _eb_best[sl] = d
    ai.eb_ser = 0
    for i in range(12):
        if _eb_ser[EB_ORDER[i]] != 0:
            ai.eb_ser = _eb_ser[EB_ORDER[i]]
            break


def enemy_base(N, ai):
    if ai is None or ai.eb_ser == 0:
        return None
    u = N.eng.by_serial.get(ai.eb_ser)
    return u if _live(u) else None


def set_alliance_target(ai, u):
    if ai is not None:
        ai.al_tgt = u.serial if u is not None else 0


def alliance_target(N, ai):
    if ai is None or ai.al_tgt == 0:
        return None
    u = N.eng.by_serial.get(ai.al_tgt)
    return u if _live(u) else None


# ------------------------------------------------------------------------------------------------ parity digest
def digest_lines(N, slot):
    ai = N.ais.get(slot)
    if ai is None or not ai.cp_on:
        return []
    s0 = s1 = 0
    for i in range(CP_MEM):
        s0 += ai.cp_mem[i]
        s1 += ai.cp_mem[CP_MEM + i]
    out = ['ai cap %d mem %d %d sum %d %d state %d %d size %d goal %d %d tgt %d atg %d full %d waves %d first %d sent %d thr %d bad %d acq %d' % (
        slot, _count(N, ai, 0), _count(N, ai, 1), s0, s1, ai.cp_state[0], ai.cp_state[1], ai.cp_size[0], int(ai.cp_gx[0]),
        int(ai.cp_gy[0]), ai.cp_tgt[0], 1 if ai.cp_atg[0] else 0, 1 if ai.cp_asl[0] else 0, ai.cp_waves, ai.cp_first,
        ai.cp_sent, 1 if ai.cp_thr else 0, ai.cp_bn, ai.acq_tgt)]
    if AI_DEFEND:
        out.append('ai def %d n %d' % (slot, ai.cp_dn))                 # AI-DEFEND: only with the switch on (the default digest keeps its shape)
    return out
