#!/usr/bin/env python3
"""AI build planner and harvest assigner (docs/ai_plan.md S3 + S4, groups G2 / G3 and the expansion part of G6): the natives
SetProduce / SetUpgrade / SetExpansion / GetNextExpansion / GetExpansionPeon / GetExpansionFoe / GetExpansionX / Y and
HarvestGold / HarvestWood / ClearHarvestAI / StopGathering, and the engine-side tick that turns the harvest marks into
orders.  Everything goes through the ported engine paths: a decision becomes a player input (engine.post_input, the same
event the HUD sends: train / research / building upgrade = kind 0, build = kind 1, harvest = kind 2 on a mine or tree), so
the order validation, production queue, construction and harvest state machines are the existing ones; there is no new
movement or pathing.  Udon twin: unity/Assets/W3VR/Scripts/W3AiBuild.cs.  Clean room: behaviour only, [obs] / [inf] tags.

Rules (chapter = docs/decomp/classes/...):

SetProduce(qty, id, town)   Unclassed_Native_0001 s10.1, Unclassed_0063 s1 / s2.
  * qty exactly 0 -> true; each round asks the production helper for one unit; a refusal ends the loop with false; a
    negative qty keeps asking until the helper refuses [obs]; here bounded to PRODUCE_MAX rounds (UdonSharp) [inf].
  * the helper tries, in this order [inf: the three legs of 0063 s2]:  (1) a building of the player that trains the type
    (a hero id whose hero already lives or is queued answers true at once [obs 0063 s1 step 1]);  (2) a building whose
    Upgrade list names the type (a structure that is an upgrade: keep, castle, ...);  (3) a worker that builds the type.
  * "may I issue an order now" [obs 0063 s1 step 3 / CCompAI_p1 7.5]: the producer must be complete, not hidden and have
    nothing in progress (empty train queue, no upgrade running); one already given an order that the engine has not
    applied yet is reserved for RV_STEPS steps [inf: the original applies the order at once].
  * town slot [obs 0063: -1 or the slot is free; inf]: -1 = any producer, else only a producer whose nearest town anchor
    (aicensus._town_of) is that slot.
  * the answer: false only when no unit of the player can ever take the type (no building trains or upgrades to it, no
    worker builds it) [obs 0063 s1: no unit of the player can take it -> 0]; an order given, and a request that a
    producer exists for but cannot take now (busy, no gold / food, tech limit, no spot) answer true [inf: the caller keeps
    trying other types when a producer cannot act yet, while the build loop stops at a false answer, so a false for a busy
    producer would freeze the whole build array; see docs/ai_plan.md S3].
  * committed resources [inf]: gold / lumber / food of the orders the engine has not applied yet (live reservations) and of
    the builders walking to a site are subtracted in every train / build / upgrade / research check, so several calls in
    one script round cannot all pass on the same gold.
  * a structure is built by the best worker (idle first, then lumber, then gold; nearest to the town anchor) at the first
    placeable spot of the lattice around the hall (MeleeBot rule [inf]: the ~420 points with 22 <= i*i + j*j <= 156 in
    steps of 64, nearest first; a hall type: around the town's mine); a spot near a pending build site of the player is
    skipped (the sites are collected once per search); a failed search is not repeated for FAIL_STEPS steps (per type).
SetUpgrade(id)               Unclassed_Native_0001 s10.2: tries the research (a building that lists it, idle, validated by
  research_check); ALWAYS answers false, the success is discarded [obs both builds].
GetNextExpansion / GetExpansionPeon / SetExpansion / GetExpansionFoe / GetExpansionX / Y   Unclassed_Native_0001 s8, s8.1.
  The three pickers are not read in the chapters [inf]: the next expansion is the nearest living gold mine (to the first
  town anchor) that no town slot holds and that has no hall of any player within TOWN_RADIUS and gold left; its answer is the
  lowest free town slot (-1: no slot or no mine).  The peon is the best idle / lumber / gold worker (nearest to that mine).
  SetExpansion(peon, id) = produce one id at this building (s8.1): the peon builds the hall at the nearest placeable spot
  to the mine.  GetExpansionFoe = the nearest neutral hostile creature within FOE_R of that mine, else null.
ClearHarvestAI / HarvestGold / HarvestWood / StopGathering   Unclassed_Native_0002 s1, Unclassed_0064 s3 - s5.
  * every worker has two request bits (this round: gold / lumber) and two active bits (the previous round):
    ClearHarvestAI moves request -> active and clears request [obs 1.21 loop of s1.1].
  * HarvestX(slot, n): n <= 0 or slot outside 0..3 or an unused slot -> nothing [obs]; gold needs a living mine in the slot.
    n is a counter of workers still to mark [obs 0064 s3]:  (special, lumber slot 0 only) idle unmarked workers are marked;
    (gather) unmarked working workers whose active bit equals the resource are marked;  (return) unmarked workers that are
    idle or working, not hidden, are marked.  Marked = request bit set; the marking of a worker that already works on the
    requested resource changes nothing else.
  * the tick (every HV_EVERY steps) orders each marked worker that is not already on the requested resource: gold ->
    smart order on the slot's mine, lumber -> smart order on the tree nearest to the slot's town anchor; the same worker is
    not ordered again within HV_REORDER steps.  StopGathering clears all marks and stops the workers that harvest [inf].
"""
from . import aicensus as AC
from .ai import HV_N, RV_N, FAIL_N
from .bot import MELEE_OFFS
from .engine import O_SMART, O_STOP
from .harvest import M_NONE

HV_EVERY = 40                  # steps between two harvest passes (0.2 s) [inf: the tick periods are open]
HV_REORDER = 200               # steps before the same worker gets another harvest order (1 s)
RV_STEPS = AC.RV_STEPS         # life of a reservation (a turn has 3..5 steps)
FAIL_STEPS = 1000              # no new spot search of a type that found no spot (5 s, as MELEE_SPOT_FAIL)
PRODUCE_MAX = 64               # rounds of a negative SetProduce
PEND_N = 64                    # pending build sites kept per placement search (builders with a build order + live reservations)
SPOT_MINE2 = 250000.0          # building spots keep 500 away from the mine (MELEE_SPOT_MINE2)
SPOT_HALL2 = 600.0 * 600.0     # S7 [inf]: an expansion hall stands at least this far from its mine (the start halls are 583 .. 820 away; a hall
                               # at 512 let a peasant stand inside both reach radii and shuttle 10 gold every second step: AI-HALLMINE)
SPOT_PEND2 = 192.0 * 192.0     # ... and this far from a pending build site
FOE_R2 = 800.0 * 800.0         # a creature this close to the expansion mine guards it [inf]
NEUTRAL_HOSTILE = 12


def _live(u):
    return u is not None and u.alive and not u.dead and not u.consumed      # PORT4-Build: a consumed wisp is out of the AI's population (CAbilityNightElfBuild §3.3)


def _is_hero_id(tid):
    return 0x41 <= (tid >> 24) <= 0x5a


# ------------------------------------------------------------------------------------------------ reservations
def rv_live(ai, i, steps):
    return ai.rv_ser[i] != 0 and steps - ai.rv_step[i] <= RV_STEPS


def reserved(ai, serial, steps):
    for i in range(RV_N):
        if ai.rv_ser[i] == serial and rv_live(ai, i, steps):
            return True
    return False


RK_NONE, RK_BUILD, RK_TRAIN, RK_UPGRADE = 0, 1, 2, 3     # reservation kinds (the census counts the last three; kind 0 = a research, tid = its id)


def reserve(ai, serial, steps, kind, tid, x, y):
    k = -1
    for i in range(RV_N):                       # a free or expired entry, lowest first
        if k < 0 and not rv_live(ai, i, steps):
            k = i
    if k < 0:
        k = 0
        for i in range(RV_N):                   # all busy: overwrite the oldest (lowest index on a tie)
            if ai.rv_step[i] < ai.rv_step[k]:
                k = i
    ai.rv_ser[k] = serial
    ai.rv_step[k] = steps
    ai.rv_kind[k] = kind
    ai.rv_tid[k] = tid
    ai.rv_x[k] = x
    ai.rv_y[k] = y


def _cost(N, tid):
    d = N.unit_types[tid]
    return int(d.get('gold', 0) or 0), int(d.get('lumber', 0) or 0), int(d.get('fused', 0) or 0)


def committed(N, ai, steps):
    """(gold, lumber, food) the player's own orders will still charge: orders posted but not applied yet (live reservations
    whose producer shows no queue / upgrade, builder no build order) and builders walking to their site (the cost is paid
    when the construction starts) [inf].  The check of the next order subtracts it, so several SetProduce calls of one
    script round cannot all pass on the same gold."""
    eng = N.eng
    pl = N.players[ai.slot]
    by = eng.by_serial
    g = l = f = 0
    for i in range(RV_N):
        if not rv_live(ai, i, steps):
            continue
        w = by.get(ai.rv_ser[i])
        if not _live(w):
            continue
        k = ai.rv_kind[i]
        tid = ai.rv_tid[i]
        if tid == 0:
            continue
        if k == RK_BUILD:
            if w.build is not None:
                continue                                # counted with the builders below
            cg, cl, cf = _cost(N, tid)
        elif w.tq or w.upg is not None:
            continue                                    # the engine applied it and charged it
        elif k == RK_TRAIN:
            cg, cl, cf = _cost(N, tid)
        elif k == RK_UPGRADE:
            _ok, cg, cl = eng.upgrade_check(w, tid)
            cf = max(0, _cost(N, tid)[2] - int(w.data.get('fused', 0) or 0))
        else:                                           # a research (kind 0, the research id in rv_tid)
            cg, cl, _t = eng.research_cost(tid, pl.tech.get(tid, 0) + 1)
            cf = 0
        g += cg
        l += cl
        f += cf
    for v in N.units:
        if v.owner is pl and _live(v) and v.build is not None:
            cg, cl, cf = _cost(N, v.build[0])
            g += cg
            l += cl
            f += cf
    return g, l, f


def _afford(N, pl, cm, gold, lumber, fused):
    """resources, minus what is committed, cover the cost (food: the same cap the engine checks)."""
    if pl.state.get(1, 0) - cm[0] < gold or pl.state.get(2, 0) - cm[1] < lumber:
        return False
    return fused <= 0 or N.eng.food_ok(pl, fused + cm[2])


# ------------------------------------------------------------------------------------------------ workers
def worker_kind(N, u):
    """harvest kind of the unit's ability (0 Ahar, 1 Ahrl, 2 wisp, 3 acolyte), -1 for a non worker."""
    return N.eng.hv.info(u)[0]


def can_gold(kind):
    return kind == 0 or kind == 2 or kind == 3


def can_lumber(kind):
    return kind == 0 or kind == 1 or kind == 2


def hv_res(N, u):
    """resource of the worker's running harvest trip: 0 gold, 1 lumber, -1 not harvesting."""
    s = N.eng.hv.st.get(u)
    if s is None or s.get('mode', M_NONE) == M_NONE:
        return -1
    return s.get('res', -1)


def is_idle(N, u):
    return u.order == 0 and hv_res(N, u) < 0 and not getattr(u, 'hidden', False)


def hv_find(ai, serial):
    for i in range(HV_N):
        if ai.hv_ser[i] == serial:
            return i
    return -1


def hv_slot(ai, serial):
    """table entry of a worker; a new entry if it has none; -1 when the table is full."""
    i = hv_find(ai, serial)
    if i >= 0:
        return i
    for i in range(HV_N):
        if ai.hv_ser[i] == 0:
            ai.hv_ser[i] = serial
            ai.hv_req[i] = ai.hv_act[i] = 0
            ai.hv_rtn[i] = ai.hv_atn[i] = 0
            ai.hv_ord[i] = -1000000
            return i
    for i in range(HV_N):                       # full: reclaim the lowest entry that carries no mark (request and active bit 0)
        if ai.hv_req[i] == 0 and ai.hv_act[i] == 0:
            ai.hv_ser[i] = serial
            ai.hv_rtn[i] = ai.hv_atn[i] = 0
            ai.hv_ord[i] = -1000000
            return i
    return -1


# ------------------------------------------------------------------------------------------------ harvest natives
def clear_harvest(N, ai):
    """ClearHarvestAI: the request bits move to the active bits [obs 1.21 loop of Unclassed_Native_0002 s1.1]."""
    if ai is None:
        return
    for i in range(HV_N):
        if ai.hv_ser[i] != 0:
            ai.hv_act[i] = ai.hv_req[i]
            ai.hv_atn[i] = ai.hv_rtn[i]
            ai.hv_req[i] = 0


def stop_gathering(N, ai):
    """StopGathering: forget every mark and stop the workers that are harvesting [inf: 0001 s5.1 helper]."""
    if ai is None:
        return
    pl = N.players[ai.slot]
    for u in N.units:
        if u.owner is pl and _live(u) and hv_res(N, u) >= 0:
            N.eng.post_input(ai.slot, 0, u.serial, O_STOP)
    for i in range(HV_N):
        ai.hv_ser[i] = 0
        ai.hv_req[i] = ai.hv_act[i] = 0


def harvest(N, ai, res, town, amount):
    """HarvestGold (res 1) / HarvestWood (res 2): mark up to `amount` workers for the resource at the town slot."""
    if ai is None or amount <= 0 or town < 0 or town >= AC.TOWN_N:
        return
    AC.refresh_towns(N, ai)
    if ai.town_h[town] == 0 and ai.town_m[town] == 0:
        return                                          # unused slot [obs 0064 s3]
    by = N.eng.by_serial
    if res == 1:
        m = by.get(ai.town_m[town]) if ai.town_m[town] != 0 else None
        if not _live(m):
            return                                      # no usable gold source [obs 0064 s3 steps 1-3]
    pl = N.players[ai.slot]
    left = amount
    steps = N.vm.steps
    if res == 2 and town == 0:                          # special pass: idle workers go to lumber [obs 0064 s4 step 3]
        for u in N.units:
            if left <= 0:
                break
            if u.owner is not pl or not _live(u) or not can_lumber(worker_kind(N, u)):
                continue
            if not is_idle(N, u) or u.build is not None or u.constructing or reserved(ai, u.serial, steps):
                continue
            k = hv_slot(ai, u.serial)
            if k < 0 or ai.hv_req[k] != 0:
                continue
            ai.hv_req[k] = res
            ai.hv_rtn[k] = town
            left -= 1
    for pas in (0, 1):                                  # 0 gather (keep the workers of the last round), 1 return (any)
        for u in N.units:
            if left <= 0:
                break
            if u.owner is not pl or not _live(u):
                continue
            kind = worker_kind(N, u)
            if kind < 0 or (res == 1 and not can_gold(kind)) or (res == 2 and not can_lumber(kind)):
                continue
            if kind == 3 and res == 1 and not N.eng.hv.accepts_acolyte(m):
                continue                                # an acolyte only mines a mine with Abgm (CUnit p7 §2.3 / CAbilityAcolyteHarvest §5.3)
            if u.build is not None or u.constructing or reserved(ai, u.serial, steps):
                continue
            rs = hv_res(N, u)
            hidden = getattr(u, 'hidden', False)
            if rs < 0 and (u.order != 0 or hidden):
                continue                                # neither idle nor working
            k = hv_find(ai, u.serial)
            if k >= 0 and ai.hv_req[k] != 0:
                continue                                # already marked this round
            if pas == 0:
                if k < 0 or ai.hv_act[k] != res:
                    continue                            # gather pass: the worker was on this resource last round
            elif hidden:
                continue                                # return pass skips the hidden (inside a mine)
            if k < 0:
                k = hv_slot(ai, u.serial)
                if k < 0:
                    continue
            ai.hv_req[k] = res
            ai.hv_rtn[k] = town
            left -= 1


def _anchor_xy(N, ai, town):
    a = AC._anchor(N, ai, town)
    return a


def harvest_tick(N, ai, steps):
    """engine tick: order every marked worker that is not on the requested resource (every HV_EVERY steps)."""
    if steps - ai.hv_step < HV_EVERY:
        return
    ai.hv_step = steps
    AC.refresh_towns(N, ai)
    by = N.eng.by_serial
    hv = N.eng.hv
    for i in range(HV_N):
        s = ai.hv_ser[i]
        if s == 0:
            continue
        u = by.get(s)
        if not _live(u) or u.owner is not N.players[ai.slot]:
            ai.hv_ser[i] = 0                            # the worker is gone: free the entry
            continue
        r = ai.hv_req[i]
        if r == 0:
            continue
        if u.build is not None or u.constructing or getattr(u, 'hidden', False) or reserved(ai, s, steps):
            continue
        if u.order == 852024:
            continue                                    # AI-MICRO: a repairing worker (aimicro.py) is left alone
        want = 0 if r == 1 else 1
        if hv_res(N, u) == want:
            continue
        if steps - ai.hv_ord[i] < HV_REORDER:
            continue
        t = ai.hv_rtn[i]
        if want == 0:
            m = by.get(ai.town_m[t]) if ai.town_m[t] != 0 else None
            if not _live(m):
                continue
            N.eng.post_input(ai.slot, 2, s, O_SMART, 0.0, 0.0, m.serial)
            N.eng.log('ai harvest u%d gold town %d mine u%d' % (s, t, m.serial))
        else:
            a = _anchor_xy(N, ai, t)
            if a is None:
                continue
            tree = hv._nearest_tree(u, a[0], a[1], 0.0)
            if tree is None:
                ai.hv_ord[i] = steps                    # throttle the rescan of a map without a reachable tree
                continue
            N.eng.post_input(ai.slot, 2, s, O_SMART, 0.0, 0.0, tree.dser)
            N.eng.log('ai harvest u%d lumber town %d tree %d' % (s, t, tree.dser))
        ai.hv_ord[i] = steps


# ------------------------------------------------------------------------------------------------ production
def _town_ok(N, ai, u, town):
    if town < 0:
        return True
    return AC._town_of(N, ai, u.x, u.y) == town


def _producer_idle(u):
    return not u.tq and u.upg is None and not u.constructing and not getattr(u, 'hidden', False)


def _hero_present(N, pl, tid):
    for v in N.units:
        if v.owner is pl and v.alive and not v.dead and v.tid == tid:
            return True
        if v.owner is pl:
            for e in v.tq:
                if e[0] == tid and e[1] == 0:
                    return True
    return False


P_NONE, P_PENDING, P_ISSUED = 0, 1, 2     # _produce_one: no unit of the player can ever take it / accepted, retried / order given


def _produce_one(N, ai, tid, town):
    eng = N.eng
    d = N.unit_types.get(tid)
    if d is None:
        return P_NONE
    pl = N.players[ai.slot]
    steps = N.vm.steps
    AC.refresh_towns(N, ai)
    if _is_hero_id(tid) and _hero_present(N, pl, tid):
        return P_ISSUED                                 # already present or queued [obs 0063 s1 step 1]
    capable = False
    cm = committed(N, ai, steps)
    # leg 1: a building that trains the type
    for u in N.units:
        if u.owner is not pl or not _live(u) or not u.is_bldg or not eng.trains(u, tid):
            continue
        capable = True
        if not _producer_idle(u) or reserved(ai, u.serial, steps) or not _town_ok(N, ai, u, town):
            continue
        ok, _g, _l, _free = eng.train_check(u, tid)
        if ok and _afford(N, pl, cm, _g, _l, _cost(N, tid)[2]):
            eng.post_input(ai.slot, 0, u.serial, tid)
            reserve(ai, u.serial, steps, RK_TRAIN, tid, 0.0, 0.0)
            return P_ISSUED
        return P_PENDING                                # cannot pay / no food / tech limit now: the script asks again
    if capable:
        return P_PENDING                                # every producer is busy
    # leg 2: a building that becomes the type (an Upgrade list entry)
    for u in N.units:
        if u.owner is not pl or not _live(u) or not u.is_bldg or tid not in eng.upgrade_list(u.tid):
            continue
        capable = True
        if not _producer_idle(u) or reserved(ai, u.serial, steps) or not _town_ok(N, ai, u, town):
            continue
        ok, _g, _l = eng.upgrade_check(u, tid)
        if ok and _afford(N, pl, cm, _g, _l, max(0, _cost(N, tid)[2] - int(u.data.get('fused', 0) or 0))):
            eng.post_input(ai.slot, 0, u.serial, tid)
            reserve(ai, u.serial, steps, RK_UPGRADE, tid, 0.0, 0.0)
            return P_ISSUED
        return P_PENDING
    if capable:
        return P_PENDING
    # leg 3: a worker that builds the type
    if not d.get('bldg'):
        return P_NONE
    for u in N.units:
        if u.owner is pl and _live(u) and eng.builds(u, tid):
            capable = True
            break
    if not capable:
        return P_NONE
    if eng.is_mine_type(d):
        return P_ISSUED if _build_mine(N, ai, tid, town, None) else P_PENDING     # AI-MINEBUILD: a mine structure goes ON a neutral mine
    return P_ISSUED if _build_one(N, ai, tid, town, None) else P_PENDING


def _pick_worker(N, ai, tid, ax, ay, steps, only=None):
    """best worker for a build: idle first, then lumber, then gold; nearest to (ax, ay); lowest serial last."""
    eng = N.eng
    pl = N.players[ai.slot]
    best, bk = None, None
    for u in N.units:
        if u.owner is not pl or not _live(u) or not eng.builds(u, tid):
            continue
        if only is not None and u is not only:
            continue
        rs = hv_res(N, u)
        if (getattr(u, 'hidden', False) and rs != 0) or u.constructing or u.build is not None or reserved(ai, u.serial, steps):
            continue                                    # a worker inside a mine (gold trip) may still be sent: it leaves the mine [inf: S6]
        if rs < 0 and u.order != 0:
            continue
        cls = 0 if rs < 0 else (1 if rs == 1 else 2)
        d2 = (u.x - ax) ** 2 + (u.y - ay) ** 2
        key = (cls, d2, u.serial)
        if bk is None or key < bk:
            best, bk = u, key
    return best


def _pending_sites(N, ai, pl, steps):
    """the player's pending build sites (builders with a build order, live build reservations), at most PEND_N, collected
    once per placement search."""
    out = []
    for v in N.units:
        if v.owner is pl and _live(v) and v.build is not None and len(out) < PEND_N:
            out.append((v.build[1], v.build[2]))
    for i in range(RV_N):
        if rv_live(ai, i, steps) and ai.rv_kind[i] == RK_BUILD and len(out) < PEND_N:
            out.append((ai.rv_x[i], ai.rv_y[i]))
    return out


def _build_check(N, ai, pl, tid, steps):
    eng = N.eng
    if not eng.tech_ok(pl, tid) or not eng.req_ok(pl, tid):
        return False
    gold, lumber, fused = _cost(N, tid)
    cm = committed(N, ai, steps)
    if fused > 0 and pl.state.get(5, 0) + cm[2] + fused > eng.food_ceiling:
        return False
    return pl.state.get(1, 0) - cm[0] >= gold and pl.state.get(2, 0) - cm[1] >= lumber


def _spot(N, ai, tid, w, hx, hy, mine, near_mine, pl, steps):
    """first placeable lattice point: around the hall (the mine kept clear) or, for a hall, around the mine."""
    eng = N.eng
    if near_mine and mine is not None:
        cx0, cy0 = mine.x, mine.y
    else:
        cx0, cy0 = hx, hy
    pend = _pending_sites(N, ai, pl, steps)
    for _key, j, i in MELEE_OFFS:
        x, y = cx0 + 64.0 * i, cy0 + 64.0 * j
        if near_mine and mine is not None and (x - mine.x) ** 2 + (y - mine.y) ** 2 < SPOT_HALL2:
            continue
        if not near_mine and mine is not None and (x - mine.x) ** 2 + (y - mine.y) ** 2 <= SPOT_MINE2:
            continue
        near = False
        for px, py in pend:
            if (px - x) ** 2 + (py - y) ** 2 < SPOT_PEND2:
                near = True
                break
        if near:
            continue
        ok, cx, cy = eng.can_place(pl, tid, x, y, w)
        if ok:
            return cx, cy
    return None


def _fail_idx(ai, tid):
    for i in range(FAIL_N):
        if ai.fail_tid[i] == tid:
            return i
    return -1


def _build_one(N, ai, tid, town, peon):
    """order one worker to build tid (town slot or -1); peon forces the worker (SetExpansion)."""
    eng = N.eng
    pl = N.players[ai.slot]
    steps = N.vm.steps
    if town < -1 or town >= AC.TOWN_N:
        town = -1
    fi = _fail_idx(ai, tid)
    if fi >= 0 and steps < ai.fail_until[fi]:
        return False
    if not _build_check(N, ai, pl, tid, steps):
        return False
    is_hall = tid in AC.HALL_IDS
    mine = None
    ax = ay = None
    if is_hall:
        if town >= 0 and ai.town_m[town] != 0:
            mine = N.eng.by_serial.get(ai.town_m[town])
        if not _live(mine):
            mine = _expansion_mine(N, ai)                # no mine in the slot: the next expansion's mine
        if mine is None:
            return False
        ax, ay = mine.x, mine.y
    else:
        t = town if town >= 0 else 0
        a = AC._anchor(N, ai, t)
        if a is None:
            a = AC._anchor(N, ai, 0)
        if a is None:
            for i in range(AC.TOWN_N):
                a = AC._anchor(N, ai, i)
                if a is not None:
                    break
        if a is None:
            return False
        ax, ay = a
        mine = eng.hv._nearest_mine(None, ax, ay, 0.0)
    w = _pick_worker(N, ai, tid, ax, ay, steps, peon)
    if w is None:
        return False
    sp = _spot(N, ai, tid, w, ax, ay, mine, is_hall, pl, steps)
    if sp is None:
        k = fi if fi >= 0 else -1
        if k < 0:
            for i in range(FAIL_N):
                if ai.fail_tid[i] == 0 or steps >= ai.fail_until[i]:
                    k = i
                    break
        if k < 0:
            k = 0
        ai.fail_tid[k] = tid
        ai.fail_until[k] = steps + FAIL_STEPS
        return False
    eng.post_input(ai.slot, 1, w.serial, tid, sp[0], sp[1])
    reserve(ai, w.serial, steps, RK_BUILD, tid, sp[0], sp[1])
    return True


def _neutral_mine(m):
    return m.owner is not None and m.owner.id >= NEUTRAL_HOSTILE and m.tid == AC.MINE_NGOL


def _build_mine(N, ai, tid, town, peon):
    """AI-MINEBUILD (docs/ai_plan.md): order a worker to build a mine structure (the undead Haunted Gold Mine, ability Abgm) ON a free
    neutral gold mine [obs Build.md / BUGS PORT4-Build: the engine replaces the mine at the construction start].  Which mine
    [inf]: town >= 0 = the mine its slot holds when that is still neutral; town -1 = the first slot (ascending) that holds a
    neutral mine; town -2 (SetExpansion) = the expansion mine chosen by GetNextExpansion."""
    eng = N.eng
    pl = N.players[ai.slot]
    steps = N.vm.steps
    if town < -2 or town >= AC.TOWN_N:
        town = -1
    AC.refresh_towns(N, ai)
    mine = None
    if town == -2:
        mine = eng.by_serial.get(ai.exp_mine) if ai.exp_mine != 0 else None
        if not _live(mine) or not _neutral_mine(mine):
            mine = _expansion_mine(N, ai)
    else:
        for i in range(AC.TOWN_N):
            if town >= 0 and i != town:
                continue
            m = eng.by_serial.get(ai.town_m[i]) if ai.town_m[i] != 0 else None
            if _live(m) and _neutral_mine(m) and mine is None:
                mine = m
    if not _live(mine) or not _neutral_mine(mine):
        return False
    fi = _fail_idx(ai, tid)
    if fi >= 0 and steps < ai.fail_until[fi]:
        return False
    if not _build_check(N, ai, pl, tid, steps):
        return False
    for px, py in _pending_sites(N, ai, pl, steps):
        if (px - mine.x) ** 2 + (py - mine.y) ** 2 < SPOT_PEND2:
            return False                                # a worker is already on its way to this mine
    w = _pick_worker(N, ai, tid, mine.x, mine.y, steps, peon)
    if w is None:
        return False
    ok, cx, cy = eng.can_place_over(pl, tid, mine.x, mine.y, w, mine)
    if not ok:
        k = fi if fi >= 0 else -1
        if k < 0:
            for i in range(FAIL_N):
                if ai.fail_tid[i] == 0 or steps >= ai.fail_until[i]:
                    k = i
                    break
        if k < 0:
            k = 0
        ai.fail_tid[k] = tid
        ai.fail_until[k] = steps + FAIL_STEPS
        return False
    eng.post_input(ai.slot, 1, w.serial, tid, mine.x, mine.y)
    reserve(ai, w.serial, steps, RK_BUILD, tid, mine.x, mine.y)
    return True


def set_produce(N, ai, qty, tid, town):
    if ai is None:
        return False
    if town < -1 or town >= AC.TOWN_N:
        town = -1                                       # a slot outside the table is "any" [inf]
    n = int(qty)
    rounds = 0
    while n != 0:
        if rounds >= PRODUCE_MAX:
            return False
        rounds += 1
        r = _produce_one(N, ai, tid, town)
        if r == P_NONE:
            return False
        if r == P_PENDING:                              # a full round that issued nothing: the next ones cannot either
            return n > 0
        n -= 1
    return True


def set_upgrade(N, ai, rid):
    """SetUpgrade: start the research in the first idle building that lists it; the native always answers false."""
    if ai is None:
        return False
    eng = N.eng
    pl = N.players[ai.slot]
    steps = N.vm.steps
    if rid not in eng.upgrades:
        return False
    for u in N.units:
        if u.owner is not pl or not _live(u) or not u.is_bldg or not eng.listed(u, 'Researches', rid):
            continue
        if not _producer_idle(u) or reserved(ai, u.serial, steps):
            continue
        ok, _g, _l, _lv = eng.research_check(u, rid)
        if ok and _afford(N, pl, committed(N, ai, steps), _g, _l, 0):
            eng.post_input(ai.slot, 0, u.serial, rid)
            reserve(ai, u.serial, steps, RK_NONE, rid, 0.0, 0.0)       # kind 0 with the research id: the census skips it
        return False
    return False


# ------------------------------------------------------------------------------------------------ expansion
def _expansion_mine(N, ai):
    """the mine of the next expansion (also stored in ai.exp_mine) or None."""
    AC.refresh_towns(N, ai)
    by = N.eng.by_serial
    pl = N.players[ai.slot]
    a = None
    for i in range(AC.TOWN_N):
        a = AC._anchor(N, ai, i)
        if a is not None:
            break
    if a is None:
        a = (0.0, 0.0)
    best, bd, bg = None, 0.0, 0
    for m in N.units:
        if m.tid not in AC.MINE_IDS or not _live(m):
            continue
        if float(getattr(m, 'res_amt', 0)) <= 0.0:
            continue
        if m.owner is not None and m.owner.id < NEUTRAL_HOSTILE and m.owner is not pl:
            continue                                     # AI-MINEBUILD: another player's (haunted / entangled) mine is not free
        held = False
        for i in range(AC.TOWN_N):
            if ai.town_m[i] == m.serial:
                held = True
        if held:
            continue
        guarded = False
        for h in N.units:
            if h.tid in AC.HALL_IDS and _live(h) and AC._d2(h.x, h.y, m.x, m.y) <= AC.TOWN_RADIUS2:
                guarded = True                           # some player's hall already stands at this mine
                break
        if guarded:
            continue
        g = 1 if _guard(N, m) is not None else 0         # an unguarded mine first, then the nearest [inf]
        d = AC._d2(m.x, m.y, a[0], a[1])
        if best is None or g < bg or (g == bg and d < bd):
            best, bd, bg = m, d, g
    ai.exp_mine = best.serial if best is not None else 0
    return best


def next_expansion(N, ai):
    if ai is None:
        return -1
    AC.refresh_towns(N, ai)
    slot = -1
    for i in range(AC.TOWN_N):
        if slot < 0 and ai.town_h[i] == 0 and ai.town_m[i] == 0:
            slot = i
    m = _expansion_mine(N, ai)
    if slot < 0 or m is None:
        ai.exp_town = -1
        return -1
    ai.exp_town = slot
    return slot


def expansion_peon(N, ai):
    """a worker for the expansion (None when there is no mine or no free worker)."""
    if ai is None:
        return None
    steps = N.vm.steps
    m = N.eng.by_serial.get(ai.exp_mine) if ai.exp_mine != 0 else None
    if not _live(m):
        m = _expansion_mine(N, ai)
    if m is None:
        return None
    pl = N.players[ai.slot]
    best, bk = None, None
    for u in N.units:
        if u.owner is not pl or not _live(u) or not u.data.get('Builds'):
            continue
        rs = hv_res(N, u)
        if (getattr(u, 'hidden', False) and rs != 0) or u.constructing or u.build is not None or reserved(ai, u.serial, steps):
            continue                                    # a worker inside a mine (gold trip) may still be sent: it leaves the mine [inf: S6]
        if rs < 0 and u.order != 0:
            continue
        cls = 0 if rs < 0 else (1 if rs == 1 else 2)
        key = (cls, AC._d2(u.x, u.y, m.x, m.y), u.serial)
        if bk is None or key < bk:
            best, bk = u, key
    return best


def set_expansion(N, ai, peon, tid):
    """SetExpansion(peon, id): the peon builds the hall near the expansion mine; answers whether the order was taken."""
    if ai is None or peon is None or not _live(peon) or peon.owner is not N.players[ai.slot]:
        return False
    if N.unit_types.get(tid) is None or not N.eng.builds(peon, tid):
        return False
    town = ai.exp_town
    if town < -1 or town >= AC.TOWN_N:
        town = -1
    if N.eng.is_mine_type(N.unit_types[tid]):
        return _build_mine(N, ai, tid, -2, peon)        # AI-MINEBUILD: the haunted mine on the expansion's neutral mine; the hall follows (MeleeTownHall)
    return _build_one(N, ai, tid, town, peon)


def _guard(N, m):
    """the nearest neutral hostile creature within FOE_R of the mine, else None."""
    best, bd = None, 0.0
    for u in N.units:
        if u.owner.id != NEUTRAL_HOSTILE or not _live(u) or u.is_bldg:
            continue
        d = AC._d2(u.x, u.y, m.x, m.y)
        if d <= FOE_R2 and (best is None or d < bd):
            best, bd = u, d
    return best


def expansion_foe(N, ai):
    if ai is None:
        return None
    m = N.eng.by_serial.get(ai.exp_mine) if ai.exp_mine != 0 else None
    if not _live(m):
        return None
    return _guard(N, m)


def expansion_xy(N, ai, k):
    if ai is None or ai.exp_mine == 0:
        return 0
    m = N.eng.by_serial.get(ai.exp_mine)
    if not _live(m):
        return 0
    return int(m.x if k == 0 else m.y)


# ------------------------------------------------------------------------------------------------ the engine tick
def engine_tick(N, ai, steps):
    """CCompAI-style periodic work of one AI player, run by the host after the player's threads (slots ascending)."""
    harvest_tick(N, ai, steps)


# ------------------------------------------------------------------------------------------------ parity digest
def digest_lines(N, slot):
    """the player's state for the host digest (the Udon host prints the same lines): units by type, resources, harvest."""
    pl = N.players[slot]
    cnt = {}
    total = bldg = constr = 0
    for u in N.units:
        if u.owner is not pl or not _live(u):
            continue
        total += 1
        if u.is_bldg:
            bldg += 1
        if u.constructing:
            constr += 1
        cnt[u.tid] = cnt.get(u.tid, 0) + 1
    ln = 'ai units %d total %d bldg %d constr %d' % (slot, total, bldg, constr)
    for tid in sorted(cnt):
        ln += ' %08x=%d' % (tid, cnt[tid])
    out = [ln, 'ai res %d gold %d lumber %d food %d/%d' % (slot, pl.state.get(1, 0), pl.state.get(2, 0), pl.state.get(5, 0),
                                                           pl.state.get(4, 0))]
    ai = N.ais.get(slot)
    if ai is not None:
        mg = ml = wg = wl = 0
        for i in range(HV_N):
            if ai.hv_ser[i] != 0:
                if ai.hv_req[i] == 1:
                    mg += 1
                elif ai.hv_req[i] == 2:
                    ml += 1
        for u in N.units:
            if u.owner is pl and _live(u):
                r = hv_res(N, u)
                if r == 0:
                    wg += 1
                elif r == 1:
                    wl += 1
        out.append('ai hv %d marks %d %d working %d %d' % (slot, mg, ml, wg, wl))
    return out
