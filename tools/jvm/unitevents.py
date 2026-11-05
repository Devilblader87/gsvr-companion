"""W9-5 unit-scoped events (docs/NOTES_triggers.md section 10, docs/BUGS.md W9-5). Python reference; the Udon twin is
unity/Assets/W3VR/Scripts/W3NatEvents.cs (impl ids 1320-1339) plus the hooks in W3World.cs / W3Engine.cs / W3Dest.cs.

Clean room: behaviour only, tags [obs] observed in the game, [ext] common.j / Blizzard.j meaning, [inf] our choice.

  widget death   TriggerRegisterDeathEvent(trigger, widget) = EVENT_WIDGET_DEATH (89) [ext]
                 unit:         raised once from Natives.kill_unit right after EVENT_UNIT_DEATH [inf order]; GetTriggerUnit /
                               GetTriggerWidget / GetDyingUnit = the unit, GetKillingUnit = the killer (0 without) [ext]
                 destructable: raised when dest_set_alive(o, False) changes a living one (KillDestructable,
                               SetDestructableLife(<= 0), DestructableRestoreLife(<= 0)); GetTriggerDestructable = it
                               (the widget context slot, answered only when it is a destructable) [ext]; once per death
                               (a restore + a second death raises again) [inf]; RemoveDestructable / RemoveUnit of a living
                               widget raise nothing [inf]. Items are not modelled.
  acquired target EVENT_UNIT_ACQUIRED_TARGET (60) [ext]: raised from Engine.scan when the scan picked a new target, at the
                 end of the scan (after next_scan); GetTriggerUnit = the acquirer, GetEventTargetUnit = the target [ext].
                 No RNG draw, no engine log line of its own; checked only when the unit has a registration of the event.
  in range       TriggerRegisterUnitInRange(trigger, unit, range, filter) [ext]: edge-triggered per registration [inf]:
                 fires when a unit other than the watched one (alive, not dead, not hidden, no locust) gets centre distance
                 <= range (no collision term [inf]); the registration then remembers every unit inside (filtered or not:
                 the filter runs once per entry, GetFilterUnit = the entering unit) and re-arms when it has left.
                 Evaluated every UR_EVERY-th engine step for the units of the buckets around the watched unit
                 (Engine.query, no scan of all units) [inf: the reference's own cadence; units move every 16th step anyway].
                 GetTriggerUnit = the unit that came into range, GetEventTargetUnit = the watched unit [inf].
                 Order: registrations by (watched unit serial, registration order), new units by serial.
                 A registration whose trigger was destroyed, whose watched unit is gone, is dropped (freed) when met.
                 Units already in range when the registration is made count as inside: no event for them until they have
                 left and come back (like `was` of the state events) [inf].
  unit state     TriggerRegisterUnitStateEvent(trigger, unit, state, op, limit) [ext]: edge-triggered like the player state
                 event (natives.set_player_state): reg.extra = [state, op, limit, was]; fires when the compare turns true.
                 The compare is the variable event's float32 limit compare (natives.limit_true) [inf]; `was` is taken at
                 registration. Evaluated in the same pass as the in-range registrations (every UR_EVERY-th step, after
                 them, registration order): discrete changes (SetUnitState, damage, spell costs, heals) are seen at the next
                 evaluation, at most UR_EVERY - 1 steps late [inf: not inside the setter]. A regeneration crossing is exact:
                 an evaluation that finds the compare still false looks ahead step by step to the next evaluation with the
                 lazily computed value (Unit.life_at / mana_at, the same r32 arithmetic in both VMs) and schedules an exact
                 check at the first step it holds (sched_pass, before the pass of that step).
                 GetTriggerUnit = the unit, GetEventUnitState = the registration's unitstate [ext: common.j declares
                 `GetEventUnitState returns unitstate`, like GetEventPlayerState; the value is GetUnitState(unit, state)],
                 GetTriggerEventId = EVENT_UNIT_STATE_LIMIT.
"""
import bisect

from . import vm as V

UR_EVERY = 4                       # in-range evaluation cadence in engine steps (power of two)
UR_CAP = 1024                      # live in-range registrations; the next ones are accepted but inert [inf]
US_CAP = 4096                      # live unit state registrations (same rule)


def ev_wdeath(N):
    return N.ev_id('EVENT_WIDGET_DEATH')


# ------------------------------------------------------------------ widget death

def reg_death(N, t, w):
    """TriggerRegisterDeathEvent(trigger, widget)."""
    from .natives import Unit, _reg
    ev = ev_wdeath(N)
    reg = _reg(N, t, 'wdeath', ev, None, None)
    if reg is None:
        return None
    o = N.o(w)
    if isinstance(o, Unit):
        reg.src = o
        o.unit_regs.setdefault(ev, []).append(reg)
    elif o is not None and hasattr(o, 'dalive'):
        reg.src = o
        o.wd_regs = getattr(o, 'wd_regs', None) or []
        o.wd_regs.append(reg)
    return reg


def unit_died(N, u, killer):
    """right after EVENT_UNIT_DEATH (Natives.kill_unit): the unit-specific EVENT_WIDGET_DEATH registrations."""
    ev = ev_wdeath(N)
    if u.unit_regs.get(ev):
        N.raise_unit_event(u, ev, -1, {'dying': N.H(u), 'killing': N.H(killer)})


def dest_died(N, o):
    """Engine.dest_set_alive(o, False) changed a living destructable."""
    regs = getattr(o, 'wd_regs', None)
    if not regs:
        return
    vm = N.vm
    ev = ev_wdeath(N)
    hid = N.H(o)
    for reg in list(regs):
        if not reg.alive or not reg.trig.alive:
            continue
        vm.fire(reg.trig, {V.CTX_TRIGGER_WIDGET: hid, V.CTX_EVENT_ID: ev, V.CTX_SOURCE: hid}, {'event': ev})


def trigger_destructable(N):
    """GetTriggerDestructable: the widget slot when it holds a destructable."""
    h = N.vm.ctx_top(V.CTX_TRIGGER_WIDGET)
    o = N.o(h) if h else None
    return h if o is not None and hasattr(o, 'dalive') else 0


# ------------------------------------------------------------------ acquired target

def acquired(N, u, best):
    """Engine.scan end: u picked `best` as a new target. O(1) when the unit has no registration."""
    if u.unit_regs.get(N.ev_id('EVENT_UNIT_ACQUIRED_TARGET')):
        N.unit_event(u, 'ACQUIRED_TARGET', {'etarget': N.H(best)})


# ------------------------------------------------------------------ in range

def reg_inrange(N, t, u, rng, filt):
    """TriggerRegisterUnitInRange(trigger, unit, range, filter): the registry is ordered by (watched serial, order)."""
    from .natives import _reg
    reg = _reg(N, t, 'inrange', 0, filt, None)
    uo = N.o(u)
    if reg is None or uo is None or not hasattr(uo, 'serial') or len(N.ur_list) >= UR_CAP:
        return reg
    reg.src = uo
    # like `was` of the state events: the units already in range when the registration is made are "inside" (no event)
    reg.extra = {'r': rng, 'inside': set() if uo.dead or uo.hidden else set(_collect(N, uo, rng))}
    N.ur_seq += 1
    bisect.insort(N.ur_list, (uo.serial, N.ur_seq, reg))
    return reg


def _collect(N, w, r):
    """the units in range of watched unit w now: serial -> unit (centre distance <= r, bucket query)."""
    eng = N.eng
    r2 = r * r
    cur = {}
    for c in eng.query(w.x, w.y, r):
        eng.wk += 1
        if c is w or not c.alive or c.dead or c.hidden or c.locust:
            continue
        dx, dy = c.x - w.x, c.y - w.y
        if dx * dx + dy * dy <= r2:
            cur[c.serial] = c
    return cur


def inrange_pass(N):
    """every UR_EVERY-th step after the region phase / corpse decay (Natives.step)."""
    eng = N.eng
    vm = N.vm
    lst = N.ur_list
    for ent in list(lst):
        reg = ent[2]
        w = reg.src
        if not reg.alive or not reg.trig.alive or not w.alive:
            lst.remove(ent)                       # DestroyTrigger / RemoveUnit: freed
            continue
        eng.wk += 1
        ex = reg.extra
        if w.dead or w.hidden:
            ex['inside'] = set()                  # a dead / hidden watcher sees nobody; units inside fire again later
            continue
        cur = _collect(N, w, ex['r'])
        fresh = sorted(s for s in cur if s not in ex['inside'])
        ex['inside'] = set(cur)
        for s in fresh:
            c = cur[s]
            if not c.alive or not reg.alive or not reg.trig.alive or not w.alive:
                continue
            hid = N.H(c)
            wid = N.H(w)
            pid = N.H(c.owner)
            if reg.filter is not None and not vm.eval_filter(reg.filter, V.CTX_FILTER_UNIT, hid):
                continue
            # Unclassed_Native_0016 §3: GetEnteringUnit accepts 0x8023d and answers the unit that came into range.
            vm.fire(reg.trig, {V.CTX_TRIGGER_UNIT: hid, V.CTX_TRIGGER_WIDGET: hid, V.CTX_TRIGGER_PLAYER: pid,
                               V.CTX_EVENT_ID: reg.event_id, V.CTX_SOURCE: wid},
                    {'etarget': wid, 'entering': hid, 'event': reg.event_id})


# ------------------------------------------------------------------ unit state

def state_value(u, st):
    """UNIT_STATE_LIFE / MAX_LIFE / MANA / MAX_MANA (GetUnitState)."""
    if st == 0:
        return u.life
    if st == 1:
        return u.maxlife
    if st == 2:
        return u.mana
    if st == 3:
        return u.maxmana
    return 0.0


def reg_state(N, t, u, st, op, lim):
    """TriggerRegisterUnitStateEvent(trigger, unit, state, op, limit).

    CUnitStateEventReg §10 [obs 0x6f337c40]: the setup creates a listener only for UNIT_STATE_LIFE (0) and
    UNIT_STATE_MANA (2); 1 (max life) and 3 (max mana) return before anything is installed, and operation 5
    (NOT_EQUAL, sub-id 0x80106) is past the 0x80105 cap. Such a registration never fires (PORT9)."""
    from .natives import _reg, limit_true, US_EQ_EPS
    if st not in (0, 2) or op == 5:
        return None
    reg = _reg(N, t, 'ustate', N.ev_id('EVENT_UNIT_STATE_LIMIT'), None, None)
    uo = N.o(u)
    if reg is None or uo is None or not hasattr(uo, 'serial') or len(N.us_list) >= US_CAP:
        return reg
    reg.src = uo
    N.us_seq += 1
    reg.extra = [st, op, lim, limit_true(op, state_value(uo, st), lim, eq_eps=US_EQ_EPS), N.us_seq]
    N.us_list.append(reg)
    return reg


def _state_check(N, reg, look):
    """evaluate one registration now: fire when the compare turns true. look: also look ahead (pass) to the steps
    before the next pass for a regeneration crossing and schedule an exact evaluation there."""
    from .natives import limit_true, US_EQ_EPS
    u = reg.src
    ex = reg.extra
    st = ex[0]
    v = state_value(u, st)
    now = limit_true(ex[1], v, ex[2], eq_eps=US_EQ_EPS)
    was = ex[3]
    ex[3] = now
    if look and not now:
        rate = u.life_rate if st == 0 else (u.mana_rate if st == 2 else 0.0)
        if rate != 0.0:
            step0 = N.vm.steps
            for k in range(1, UR_EVERY):
                v2 = u.life_at(step0 + k) if st == 0 else u.mana_at(step0 + k)
                if limit_true(ex[1], v2, ex[2], eq_eps=US_EQ_EPS):
                    bisect.insort(N.us_sched, (step0 + k, ex[4], reg))
                    break
    if now and not was:
        hid = N.H(u)
        pid = N.H(u.owner)
        N.vm.fire(reg.trig, {V.CTX_TRIGGER_UNIT: hid, V.CTX_TRIGGER_WIDGET: hid, V.CTX_TRIGGER_PLAYER: pid,
                             V.CTX_EVENT_ID: reg.event_id, V.CTX_SOURCE: hid}, {'ustate': st, 'event': reg.event_id})


def state_pass(N):
    """the unit state registrations in registration order (every UR_EVERY-th step)."""
    lst = N.us_list
    for reg in list(lst):
        u = reg.src
        if not reg.alive or not reg.trig.alive or not u.alive:
            lst.remove(reg)                           # DestroyTrigger / RemoveUnit: freed
            continue
        N.eng.wk += 1
        _state_check(N, reg, True)


def sched_pass(N):
    """the exact steps a regeneration crosses its limit (found by the look-ahead of the pass), registration order."""
    sch = N.us_sched
    steps = N.vm.steps
    while sch and sch[0][0] <= steps:
        reg = sch.pop(0)[2]
        if not reg.alive or not reg.trig.alive or not reg.src.alive:
            continue
        N.eng.wk += 1
        _state_check(N, reg, False)


def pass_(N):
    """Natives.step, every UR_EVERY-th step after the corpse decay: in-range, then unit state."""
    if N.ur_list:
        inrange_pass(N)
    if N.us_list:
        state_pass(N)
