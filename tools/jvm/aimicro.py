#!/usr/bin/env python3
"""AI worker repair and hero flight (docs/ai_plan.md AI-MICRO): two engine-side behaviours the race scripts only switch on.

StandardAI sets SetPeonsRepair and SetHeroesFlee (not on easy); the chapters store the two bits (Unclassed_Native_0003 s3) and carry
no consumer, so before this pass the flags did nothing.  Everything below is [inf]; the pass sits behind the switch AI_MICRO (env
W3_AI_MICRO=1 or W3_AI_EXTRAS=1, default OFF; Udon twin: W3Vm.aiMicro / aiExtras) and each half also needs its script flag.  Decisions are player inputs.

  * Repair (flag FLAG_PEONS_REPAIR = 0x800), every MI_EVERY steps (0.5 s): up to REPAIR_MAX new orders; the most damaged finished own
    structure (life below REPAIR_PCT of its maximum, no mine) that has fewer than REPAIR_PER live assignments gets the nearest free
    worker within REPAIR_R of it that answers the repair order (idle or on a lumber trip, not hidden, no build order) as a smart order on the structure
    (the engine turns it into the repair cast); the order is only given when the engine accepts it (repair_smart + the amount / 10
    affordability test).  An assignment lives while its worker repairs (REPAIR_HOLD steps at most; 40 steps before the order is applied).
    Repairing workers are left alone by the harvest assigner.
  * Hero flight (flag FLAG_HEROES_FLEE = 0x8): a hero below FLEE_PCT of its life with a visible enemy (no structure) within FLEE_FOE_R
    runs to the nearest own town hall, leaves the captains, is not recruited again, and stays out until it is back above FLEE_HEAL_PCT
    or FLEE_MAX steps passed.  An idle fleeing hero far from the hall is sent again.
  * Hero items (flag FLAG_HEROES_TAKE_ITEMS = 0x400): an idle hero (no cast, not fleeing, no enemy within 700) walks to the nearest item on the ground
    within TAKE_R that its inventory takes (smart order on the item; the same item is not ordered again within TAKE_GAP); a hero in a fight
    (enemy within 800) below USE_LIFE of its life uses the first usable heal item (item classes AIhe / AIha / AIre / AIra), below USE_MANA of its
    mana a mana item (AIma / AImr / AIre / AIra), one use per pass, the same slot at most every USE_GAP steps; the engine's own useslot check
    (cooldown, charges, mana, full-life refusal) decides.  Buying at shops (SetHeroesBuyItems) is not done.
Udon twin: unity/Assets/W3VR/Scripts/W3AiMicro.cs.  Clean room: behaviour only, [inf] throughout.
"""
from . import aibuild as AB
from . import aicensus as AC
from . import itemdata as ID
from .ai import CP_MEM, RP_N, FL_N, IT_N, FLAG_PEONS_REPAIR, FLAG_HEROES_FLEE, FLAG_HEROES_TAKE_ITEMS, is_fleeing, switch
from .engine import O_SMART, O_MOVE

AI_MICRO = switch('W3_AI_MICRO')     # default OFF: unmeasured heuristics that move workers and heroes around
MI_EVERY = 100                  # steps between two passes (0.5 s) [inf]
ORDER_REPAIR = 852024           # repair 0xd0038 (CAbilityRepair)
REPAIR_PCT = 0.8                # a structure below this life fraction is repaired [inf]
REPAIR_PER = 2                  # workers per structure [inf]
REPAIR_MAX = 3                  # new repair orders per pass [inf]
REPAIR_TRIES = 6                # structures looked at per pass
REPAIR_R2 = 1500.0 * 1500.0     # only workers this close to the structure are pulled [inf]
REPAIR_HOLD = 1600              # an assignment counts while the worker repairs, 8 s at most [inf]
REPAIR_NEW = 40                 # ... and for 0.2 s while its order is not applied yet
HERO_MAX = 4
FIGHT_R2 = 800.0 * 800.0       # a fight = an enemy this close to the hero (as the spell pass)
FLEE_PCT = 0.3                  # a hero below this life fraction with an enemy near flees [inf]
FLEE_HEAL_PCT = 0.6             # ... and comes back above this [inf]
FLEE_FOE_R2 = 700.0 * 700.0
FLEE_HOME_R2 = 300.0 * 300.0    # at the hall = no further order
FLEE_MAX = 12000                # steps (60 s) [inf]
TAKE_R2 = 900.0 * 900.0         # an item this close to an idle hero is fetched [inf]
TAKE_GAP = 2000                 # steps (10 s) before the same item is ordered again (an unreachable one) [inf]
USE_GAP = 400                   # steps (2 s) between two uses of the same slot [inf]
USE_LIFE = 0.4                  # a hero in a fight below this life fraction drinks a healing item [inf]
USE_MANA = 0.3                  # ... below this mana fraction a mana item [inf]
USE_SLOT0 = 852008              # useslot 852008 + slot (orders.py)
HEAL_ITEMS = ('AIhe', 'AIha', 'AIre', 'AIra')
MANA_ITEMS = ('AIma', 'AImr', 'AIre', 'AIra')
NEUTRAL_PASSIVE = 15


def _live(u):
    return u is not None and u.alive and not u.dead and not u.consumed


def _rp_live(N, ai, i, steps):
    s = ai.rp_wk[i]
    if s == 0:
        return False
    w = N.eng.by_serial.get(s)
    if not _live(w):
        return False
    age = steps - ai.rp_at[i]
    return age < REPAIR_NEW or (w.order == ORDER_REPAIR and age < REPAIR_HOLD)


def _rp_count(N, ai, serial, steps):
    n = 0
    for i in range(RP_N):
        if ai.rp_str[i] == serial and _rp_live(N, ai, i, steps):
            n += 1
    return n


def _rp_note(N, ai, structure, worker, steps):
    k = -1
    for i in range(RP_N):
        if k < 0 and not _rp_live(N, ai, i, steps):
            k = i
    if k < 0:
        k = 0
        for i in range(RP_N):
            if ai.rp_at[i] < ai.rp_at[k]:
                k = i
    ai.rp_str[k] = structure
    ai.rp_wk[k] = worker
    ai.rp_at[k] = steps


def _free_worker(N, ai, w, steps):
    """a worker that may be pulled to a repair: idle or on a lumber trip, visible, no build order, not repairing, not reserved."""
    if w.hidden or w.constructing or w.build is not None or w.is_bldg or AB.reserved(ai, w.serial, steps):
        return False
    rs = AB.hv_res(N, w)
    if rs == 0:
        return False                                   # on a gold trip: left to the harvest assigner
    if rs < 0 and w.order != 0:
        return False
    for i in range(RP_N):
        if ai.rp_wk[i] == w.serial and _rp_live(N, ai, i, steps):
            return False
    return True


def repair_pass(N, ai, steps):
    if not (ai.flags & FLAG_PEONS_REPAIR):
        return
    eng = N.eng
    sp = eng.sp
    pl = N.players[ai.slot]
    given = 0
    tried = 0
    last = None                                        # (fraction, serial) of the structure chosen last: the next one is "after" it
    while given < REPAIR_MAX and tried < REPAIR_TRIES:
        tried += 1
        best = None
        bk = None
        for s in N.units:
            if s.owner is not pl or not _live(s) or not s.is_bldg or s.constructing or s.tid in AC.MINE_IDS or s.maxlife <= 0.0:
                continue
            fr = s.life / s.maxlife
            if fr >= REPAIR_PCT:
                continue
            key = (fr, s.serial)
            if last is not None and key <= last:
                continue
            if bk is None or key < bk:
                best, bk = s, key
        if best is None:
            return
        last = bk
        if _rp_count(N, ai, best.serial, steps) >= REPAIR_PER:
            continue
        pick = None
        pk = None
        for w in N.units:
            if w.owner is not pl or not _live(w) or w is best or not _free_worker(N, ai, w, steps):
                continue
            key = ((w.x - best.x) ** 2 + (w.y - best.y) ** 2, w.serial)
            if key[0] <= REPAIR_R2 and (pk is None or key < pk) and sp.repair_smart(w, best) and sp.repair_order_ok(w, best):
                pick, pk = w, key
        if pick is None:
            continue
        eng.post_input(ai.slot, 2, pick.serial, O_SMART, 0.0, 0.0, best.serial)
        AB.reserve(ai, pick.serial, steps, AB.RK_NONE, 0, 0.0, 0.0)      # the harvest assigner must not override the order before the engine applied it
        _rp_note(N, ai, best.serial, pick.serial, steps)
        ai.mi_rep += 1
        given += 1


# ------------------------------------------------------------------------------------------------ hero flight
def _foe_near(N, h):
    eng = N.eng
    ab = eng.ab
    for v in N.units:
        if v is h or not _live(v) or v.hidden or v.is_bldg or v.locust or v.invulnerable or v.owner.id == NEUTRAL_PASSIVE:
            continue
        if eng.ally(h.owner, v.owner) or (v.invisible and not ab.visible_to(h, v)):
            continue
        if (v.x - h.x) ** 2 + (v.y - h.y) ** 2 <= FLEE_FOE_R2:
            return True
    return False


def _hall_for(N, pl, h):
    best = None
    bk = None
    for u in N.units:
        if u.owner is not pl or u.tid not in AC.HALL_IDS or not _live(u) or u.constructing:
            continue
        key = ((u.x - h.x) ** 2 + (u.y - h.y) ** 2, u.serial)
        if bk is None or key < bk:
            best, bk = u, key
    return best


def _fl_find(ai, serial):
    for i in range(FL_N):
        if ai.fl_ser[i] == serial:
            return i
    return -1


def _fl_add(ai, serial, steps):
    for i in range(FL_N):
        if ai.fl_ser[i] == 0:
            ai.fl_ser[i] = serial
            ai.fl_at[i] = steps
            return i
    return -1


def _leave_captains(ai, serial):
    for k in range(2 * CP_MEM):
        if ai.cp_mem[k] == serial:
            ai.cp_mem[k] = 0


def flee_pass(N, ai, steps):
    if not (ai.flags & FLAG_HEROES_FLEE):
        return
    eng = N.eng
    pl = N.players[ai.slot]
    n = 0
    for h in N.units:
        if n >= HERO_MAX:
            break
        if h.owner is not pl or not _live(h) or not h.hero or h.illusion or h.hidden or h.constructing:
            continue
        n += 1
        k = _fl_find(ai, h.serial)
        fr = h.life / h.maxlife if h.maxlife > 0.0 else 1.0
        fresh = False
        if k < 0:
            if fr >= FLEE_PCT or not _foe_near(N, h):
                continue
            k = _fl_add(ai, h.serial, steps)
            if k < 0:
                continue
            _leave_captains(ai, h.serial)
            ai.mi_flee += 1
            fresh = True
        elif fr >= FLEE_HEAL_PCT or steps - ai.fl_at[k] > FLEE_MAX:
            ai.fl_ser[k] = 0
            continue
        hall = _hall_for(N, pl, h)
        if hall is None:
            continue
        far = (hall.x - h.x) ** 2 + (hall.y - h.y) ** 2 > FLEE_HOME_R2
        if far and (fresh or h.order == 0):
            eng.post_input(ai.slot, 1, h.serial, O_MOVE, hall.x, hall.y)


# ------------------------------------------------------------------------------------------------ hero items
def _it_fresh(ai, a, b, steps, gap):
    for i in range(IT_N):
        if ai.it_a[i] == a and ai.it_b[i] == b:
            return steps - ai.it_at[i] >= gap
    return True


def _it_note(ai, a, b, steps):
    k = -1
    for i in range(IT_N):
        if ai.it_a[i] == a and ai.it_b[i] == b:
            k = i
    if k < 0:
        for i in range(IT_N):
            if k < 0 and ai.it_a[i] == 0:
                k = i
    if k < 0:
        k = 0
        for i in range(IT_N):
            if ai.it_at[i] < ai.it_at[k]:
                k = i
    ai.it_a[k] = a
    ai.it_b[k] = b
    ai.it_at[k] = steps


def _fight_near(N, h):
    """an enemy (visible, no structure) within the fight radius of the spell pass (800)."""
    eng = N.eng
    ab = eng.ab
    for v in N.units:
        if v is h or not _live(v) or v.hidden or v.is_bldg or v.locust or v.invulnerable or v.owner.id == NEUTRAL_PASSIVE:
            continue
        if eng.ally(h.owner, v.owner) or (v.invisible and not ab.visible_to(h, v)):
            continue
        if (v.x - h.x) ** 2 + (v.y - h.y) ** 2 <= FIGHT_R2:
            return True
    return False


def _use_item(N, ai, h, steps):
    """a healing / mana item for the hero in a fight -> True when a use order was posted."""
    eng = N.eng
    sp = eng.sp
    life = h.life < USE_LIFE * h.maxlife
    mana = h.maxmana > 0.0 and h.mana < USE_MANA * h.maxmana
    if not life and not mana:
        return False
    for slot in range(eng.itm.size(h)):
        r = sp.item_spell(h, slot)
        if r is None:
            continue
        code = r[1]
        if not ((life and code in HEAL_ITEMS) or (mana and code in MANA_ITEMS)):
            continue
        if sp.cooling(h, sp.cool_key(r[0], r[3])) or not _it_fresh(ai, h.serial, slot + 1, steps, USE_GAP):
            continue
        eng.post_input(ai.slot, 0, h.serial, USE_SLOT0 + slot)
        _it_note(ai, h.serial, slot + 1, steps)
        ai.mi_use += 1
        return True
    return False


def _take_item(N, ai, h, steps):
    """the nearest ground item an idle hero can take -> True when a pickup order was posted."""
    itm = N.eng.itm
    best = None
    bk = None
    for it in itm.ground:
        if not it.live or it.carrier is not None or it.hidden:
            continue
        d2 = (it.x - h.x) ** 2 + (it.y - h.y) ** 2
        if d2 > TAKE_R2 or not itm.can_receive(h, it, True) or not _it_fresh(ai, it.serial, 0, steps, TAKE_GAP):
            continue
        key = (d2, it.serial)
        if bk is None or key < bk:
            best, bk = it, key
    if best is None:
        return False
    N.eng.post_input(ai.slot, 2, h.serial, O_SMART, 0.0, 0.0, best.serial)
    _it_note(ai, best.serial, 0, steps)
    ai.mi_take += 1
    return True


def item_pass(N, ai, steps):
    if not (ai.flags & FLAG_HEROES_TAKE_ITEMS):
        return
    pl = N.players[ai.slot]
    n = 0
    for h in N.units:
        if n >= HERO_MAX:
            break
        if h.owner is not pl or not _live(h) or not h.hero or h.illusion or h.hidden or h.constructing or h.cast is not None:
            continue
        n += 1
        if N.eng.itm.inventory(h) is None:
            continue
        if _fight_near(N, h):
            _use_item(N, ai, h, steps)
        elif h.order == 0 and not is_fleeing(ai, h.serial) and not _foe_near(N, h):
            _take_item(N, ai, h, steps)


def engine_tick(N, ai, steps):
    """the AI player's repair / flight pass (called by the host after the spell pass, slots ascending)."""
    if not AI_MICRO or ai is None or steps - ai.mi_step < MI_EVERY:
        return
    ai.mi_step = steps
    repair_pass(N, ai, steps)
    flee_pass(N, ai, steps)
    item_pass(N, ai, steps)
