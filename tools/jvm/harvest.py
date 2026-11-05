#!/usr/bin/env python3
"""Harvest state machine (PORT-Harvest, docs/decomp/port/Harvest.md).

Rules ported from the decompiled chapters CAbilityHarvest / HarvestBase / HarvestLumber /
HarvestReturn / AcolyteHarvest / WispHarvest / GoldMine / GoldMineBase / BlightedGoldMine /
EatTree.  Values only:

* carry capacities: ``DataB`` = lumber, ``DataC`` = gold of ``Ahar`` (NOTES_harvest §0);
  per lumber hit = ``DataA``; search radius = ``Area`` (900 stock).
* single carried slot, never both resources (NOTES_harvest §2).
* a gold trip takes ``min(gold capacity, gold left in the mine)`` and depletes the mine
  (CAbilityHarvest §5.2).
* the deposit credits the carried amount through ``Natives.credit_resource`` (tax flag on,
  CAbilityHarvestReturn §17 / NOTES_harvest §2).
* a gold mine's pool = ``DataA`` (CAbilityGoldMine §4.2), miner capacity = ``DataC`` clamped
  to 32 (Agld) / 8 (Abgm, stock 5); the Acologist ring is the "5-worker queue".
* wisp: ``DataA`` lumber every ``Dur`` seconds while standing at a tree, no carry
  (CAbilityWispHarvest §7.3 / §7.6); gold is paid by the mine side.
* Entangle (``Aent``, orders 852147/852148): a neutral ``ngol`` becomes the caster's ``egol``
  with the mine's ``Aegm`` data (DataA = gold per cycle, DataB = cycle) and ``Aenc`` DataA
  (wisp slots); the remaining gold in ``res_amt`` is kept (CAbilityEntangle §5.2/§6.3).
  The per-wisp pay ``Aegm DataA / Aenc slots`` is ``[inf]``: CAbilityEntangledGoldMine §5
  (mining cycle) is Open.
* Eat Tree kills the targeted destructable and marks the caster busy until the order ends
  (CAbilityEatTree §7.1; the Beat heal buff itself is Open and not modelled).
"""
import math

from . import unitdata as UD
from .vm import r32
from .abilities import steps_of

O_SMART = 851971
O_MOVE = 851986
O_HARVEST = 852018
O_RESUME = 852017
O_RETURN = 852020
O_AUTOGOLD = 852021
O_AUTOLUMBER = 852022
O_ACOLYTE = 852185
O_WISP = 852214
O_EATTREE = 852146
O_ENTANGLE = 852147
O_ENTANGLE_INSTANT = 852148
REM_N = 8                                  # CUnit p7 §2.6 / NOTES_harvest §0: the 8 remembered resource targets

AHAR = 'Ahar'
AHRL = 'Ahrl'
AWHA = 'Awha'
AAHA = 'Aaha'
AEAT = 'Aeat'
AGLD = 'Agld'
AGMB = 'Agmb'
ABGM = 'Abgm'
AENT = 'Aent'
AEGM = 'Aegm'
AENC = 'Aenc'

MINE_IDS = frozenset(('ngol', 'egol', 'ugol'))
# resource drop-off buildings (town halls / halls / mines); a type with a Trains list is a
# town hall as well, so a custom map's hall works without a hard-coded id.
GOLD_DROP = frozenset(('htow', 'hkee', 'hcas', 'ogre', 'ostr', 'ofrt', 'unpl', 'unp1', 'unp2',
                       'etol', 'etoa', 'etoe', 'egol', 'ugol', 'ngol', 'ntav'))
LUMBER_DROP = frozenset(('htow', 'hkee', 'hcas', 'ogre', 'ostr', 'ofrt', 'unpl', 'unp1', 'unp2',
                         'etol', 'etoa', 'etoe', 'hlum', 'olum', 'ulum'))

REACH_TREE = 128.0                   # trees keep the old reach: the ORACLE-ECON probe measured gold trips only
REACH = 144.0                        # ORACLE-ECON 2026-10-30: interaction range (see _reach).  The old
                                     # 128 stopped a miner just outside the hall's interaction radius and
                                     # made it walk around the building; 144 reproduces the measured
                                     # melee rate (3 peasants, +60 gold / 10 s = 6 gold/s).
SPS = 200
SKIP_STEP = 4.0                       # PERF-MELEE: a walker covers less than this per step (522 / 200 = 2.6): the reach test sleeps need / SKIP_STEP steps
SKIP_MAX = 16                         # PERF-MELEE: ... at most this many steps (a teleport of the worker is noticed at the latest then)

M_NONE, M_GO, M_WORK, M_DROP, M_WAIT_MINE, M_IN_MINE = 0, 1, 2, 3, 4, 5

RETRY_RES = 0.5                      # NOTES_harvest §1.2 [obs 0x6f451600]: a failed arrival at the resource re-walks after 500 ms
RETRY_DROP = 5.0                     # NOTES_harvest §1.2 [obs 0x6f4501b0]: a failed arrival at the drop-off re-walks after 5 s
RETRY_RES_N = 3                      # [inf] the first counter's limit (the chapter gives only the mine-entry value 3)
RETRY_DROP_N = 30                    # NOTES_harvest §1.2 [obs]: the second counter gives up after 30 failed drop-off arrivals

TRIP_MIN = 4.5                       # HALL-MINE guard [inf]: where one spot lies in the reach discs of both the mine and its drop-off
                                     # (_lens: a hall pre-placed by a map script) two instant takes of a worker are at least this far
                                     # apart; otherwise take / deposit would cycle with no walking (10 gold per step). The floor sits
                                     # under the measured melee trip (5.0 s, NOTES_harvest section 5), regular layouts are not touched;
                                     # the placement rule (engine.mine_near) keeps a player from building such a hall.


def _id4(u):
    return (u.data or {}).get('id4', '')


def _steps(t):
    """seconds -> steps through the engine's one conversion (ceil at 200/s), data as float32."""
    return steps_of(r32(float(t)))


def _dur(lv):
    """mine cycle = DataB seconds, a non-positive row means 1 s (as HvMineDur)."""
    b = float(lv['B']) if lv is not None else 0.0
    return b if b > 0.0 else 1.0


def _rawcode(s):
    return (ord(s[0]) << 24) | (ord(s[1]) << 16) | (ord(s[2]) << 8) | ord(s[3])


class Harvest:
    def __init__(self, eng):
        self.eng = eng
        self.N = eng.N
        self.take_at = {}                   # HALL-MINE guard: worker -> step of its last instant mine take
        self.st = {}                 # unit -> state dict
        self.load = {}               # unit -> (amount, kind) of a partly filled load kept across orders
        self.mines = {}              # mine -> {'slots': [...], 'next': step}
        self.seeded = set()          # mines whose pool was seeded from DataA
        self.rem = {}                # worker -> [8 remembered resource targets, ring cursor] (CUnit p7 §2.5/§2.6)
        self.drop_rem = {}           # worker -> remembered drop-off (HarvestReturn §3: +0x1b8/+0x1bc)

    # ---------------------------------------------------------------- data
    def _abil(self, u, bases):
        for code, base, lv in self.eng.ab.abil_list(u):
            if base in bases:
                return base, lv
        return None, None

    def info(self, u):
        """(kind, data) of the unit's worker harvest ability: kind 0 Ahar, 1 Ahrl, 2 wisp, 3 acolyte."""
        for code, base, lv in self.eng.ab.abil_list(u):
            if lv is None:
                continue
            if base == AHAR:
                cg, cl = max(0, int(lv['C'])), max(0, int(lv['B']))
                if cg > 0:
                    cg += int(getattr(u, 'upg_gcap', 0))     # rmin: only a capacity that is already non-zero
                if cl > 0:
                    cl += int(getattr(u, 'upg_lcap', 0))     # rlum: same rule (slot 207)
                return 0, dict(gold=True, lumber=True, cg=cg, cl=cl,
                               per=max(0, int(lv['A'])), rad=float(lv['Area']) or 900.0)
            if base == AHRL:
                cl = max(0, int(lv['B']))
                if cl > 0:
                    cl += int(getattr(u, 'upg_lcap', 0))
                return 1, dict(gold=False, lumber=True, cg=0, cl=cl,
                               per=max(0, int(lv['A'])), rad=float(lv['Area']) or 900.0)
            if base == AWHA:
                return 2, dict(gold=True, lumber=max(0, int(lv['A'])) > 0, cg=0, cl=0,
                               per=max(0, int(lv['A'])), rad=float(lv['Rng']) or float(lv['Area']) or 900.0,
                               dur=float(lv['Dur']) or 8.0)
            if base == AAHA:
                return 3, dict(gold=True, lumber=False, cg=0, cl=0, per=10, rad=float(lv['Area']) or 900.0,
                               dur=float(lv['Dur']) or 1.1)
        return -1, None

    def mine_info(self, mine):
        if mine is None or not getattr(mine, 'alive', False) or getattr(mine, 'dead', False):
            return None, None
        got = {}
        for code, base, lv in self.eng.ab.abil_list(mine):
            if lv is not None and base in (AGLD, AGMB, ABGM, AEGM, AENC):
                got[base] = lv
        if AEGM in got:
            lv = got[AEGM]
            aenc = got.get(AENC)
            cap = int(aenc['A']) if aenc is not None else int(lv['C'])
            if cap <= 0:
                cap = 5
            cap = min(cap, 32)
            return AEGM, dict(lv=lv, cap=cap, pool=0, interval=_dur(lv),
                              amount=max(0, int(lv['A']) + int(getattr(mine, 'upg_mine', 0))))   # rent
        for base in (AGLD, ABGM, AGMB):
            if base in got:
                lv = got[base]
                cap = int(lv['C'])
                if cap <= 0:
                    cap = 1
                cap = min(cap, 8 if base == ABGM else 32)
                return base, dict(lv=lv, cap=cap, pool=int(lv['A']), interval=_dur(lv))
        if _id4(mine) in MINE_IDS:
            return AGLD, dict(lv=None, cap=1, pool=0, interval=1.0)
        return None, None

    def is_mine(self, u):
        return self.mine_info(u)[0] is not None

    def accepts_acolyte(self, mine):
        """CAbilityAcolyteHarvest §5.3 step 4 (`0x6f440b80`): after the mine's resource object answers, the
        validator looks for the **Abgm** slot ability on the same unit; absent -> 0x28 and the acolyte order is
        refused. A normal gold mine (`Agld`) and an entangled mine (`Aegm`) have no Abgm, so only a haunted mine
        (`ugol`, `Abgm`) accepts an acolyte."""
        if mine is None:
            return False
        for _code, base, lv in self.eng.ab.abil_list(mine):
            if lv is not None and base == ABGM:
                return True
        return False

    # ---------------------------------------------------------------- init / ability changes
    def init_unit(self, u):
        self._seed_mine(u)

    def on_add(self, u, base):
        if base in (AGLD, ABGM, AGMB):
            self._seed_mine(u)

    def _seed_mine(self, u):
        if u in self.seeded:
            return
        base, mi = self.mine_info(u)
        if base != AGLD:
            return
        self.seeded.add(u)
        if not hasattr(u, 'res_amt'):
            u.res_amt = mi['pool']

    def on_remove(self, u):
        """release a miner's slot / unhide it, and drop the mine's queue."""
        self.stop(u)
        self.load.pop(u, None)
        self.take_at.pop(u, None)
        self.rem.pop(u, None)
        self.drop_rem.pop(u, None)
        if u in self.mines:
            for w in list(self.mines[u]['slots']):
                self._eject(u, w)
            del self.mines[u]

    # ---------------------------------------------------------------- state helpers
    def _remember(self, u, t):
        """CUnit p7 §2.5/§2.6 [obs 0x6f26cf90, 0x6f26cd30] / NOTES_harvest §1.2 [obs 0x6f44f710]: a resource
        target going into the 8-slot remembered ring. Insertion order is not decoded: the target takes the slot
        after the cursor (oldest entry is overwritten on wrap) and a repeated target only moves the cursor [inf]."""
        if t is None:
            return
        r = self.rem.get(u)
        if r is None:
            r = [[None] * REM_N, -1]
            self.rem[u] = r
        slots = r[0]
        for i in range(REM_N):
            if slots[i] is t:
                r[1] = i
                return
        c = r[1] + 1
        if c >= REM_N:
            c = 0
        slots[c] = t
        r[1] = c

    def _remembered(self, u, res):
        """CUnit p7 §2.6 [obs 0x6f26cd30]: advance the cursor and answer the first live remembered target of the
        resource kind (up to one full wrap); the target itself is checked by the caller."""
        r = self.rem.get(u)
        if r is None:
            return None
        acol = self.info(u)[0] == 3
        slots = r[0]
        c = r[1]
        for _k in range(REM_N):
            c += 1
            if c >= REM_N:
                c = 0
            t = slots[c]
            if t is None or not self._valid(t):
                continue
            if res == 0:
                if not self.is_mine(t):
                    continue
                if acol and not self.accepts_acolyte(t):
                    continue                     # CAbilityAcolyteHarvest §5.3: a non-Abgm mine is not a candidate
            elif getattr(t, 'tname', '') != 'destructable':
                continue
            r[1] = c
            return t
        return None

    def active(self, u):
        return self.st.get(u, {}).get('mode', M_NONE) != M_NONE

    def stop(self, u):
        s = self.st.get(u)
        if not s:
            return
        if s['mode'] == M_IN_MINE:
            self._leave_slot(u)
            self._unhide(u)
        if s.get('mine') is not None:
            self._leave_slot(u)
        if s.get('carry', 0) > 0:
            self.load[u] = (s['carry'], s.get('kind', 0))
        self.st.pop(u, None)

    def _slot_list(self, mine):
        return self.mines.setdefault(mine, {'slots': [], 'next': -1})

    def _leave_slot(self, u):
        s = self.st.get(u)
        if s is None:
            return
        mine = s.get('mine')
        if mine is None or mine not in self.mines:
            return
        slots = self.mines[mine]['slots']
        if u in slots:
            slots.remove(u)

    def _unhide(self, u):
        if not getattr(u, 'hidden', False):
            return
        u.hidden = False
        s = self.st.get(u) or {}
        mine = s.get('mine')
        if mine is not None and not s.get('trip'):
            u.x = r32(mine.x + 48.0)
            u.y = r32(mine.y)
        u.path = None
        u.seg = None
        self.eng.N.dirty.add(u)
        self.eng.relocate(u)

    def _eject(self, mine, w):
        s = self.st.get(w)
        if s is not None:
            s['mode'] = M_NONE
            s['mine'] = None
            s['trip'] = False
        self._unhide(w)
        w.order = 0

    # ---------------------------------------------------------------- orders
    def order(self, u, oid, x=None, y=None, target=None):
        """-> True/False when this module answers the order, None when the engine keeps it."""
        kind, inf = self.info(u)
        has_eat = any(b == AEAT for _c, b, _l in self.eng.ab.abil_list(u))
        if oid in (O_ENTANGLE, O_ENTANGLE_INSTANT) and target is not None \
                and getattr(target, 'tname', '') == 'unit':
            if any(b == AENT for _c, b, _l in self.eng.ab.abil_list(u)):
                return self.entangle(u, target)
        if oid == O_EATTREE and has_eat and target is not None and getattr(target, 'tname', '') == 'destructable':
            if getattr(target, 'dalive', False) and not getattr(target, 'removed', False):
                self.stop(u)
                target.life = 0.0
                self.eng.dest_set_alive(target, False)
                self.eng.clear_order(u)
                u.order = O_EATTREE
                u.eating = 1
                return True
            return False
        if kind < 0:
            return None
        if oid not in (O_HARVEST, O_RESUME, O_RETURN, O_AUTOGOLD, O_AUTOLUMBER, O_ACOLYTE, O_WISP, O_SMART):
            return None
        if target is not None and getattr(target, 'tname', '') == 'destructable':
            dt = self.eng.dest_types.get(getattr(target, 'id4', ''))
            is_tree = bool(dt and int(dt.get('targ', 0)) & UD.T_TREE)
            if kind in (0, 1, 2) and inf['lumber'] and is_tree and getattr(target, 'dalive', False):
                self.stop(u)
                self._start_tree(u, kind, inf, target, oid == O_RETURN)
                return True
            if oid == O_SMART:
                return None
            return False
        if target is not None and getattr(target, 'tname', '') == 'unit':
            if self.is_mine(target) and inf['gold']:
                if kind == 3:
                    if not self.accepts_acolyte(target):
                        return False                 # CAbilityAcolyteHarvest §5.3: no Abgm on the mine -> 0x28
                    st = self.st.get(u)
                    if st is not None and st.get('mode') == M_IN_MINE and st.get('mine') is target:
                        return False                 # CAbilityAcolyteHarvest §5.3 step 6: already inside -> 0xb0
                self.stop(u)
                self._start_mine(u, kind, inf, target, oid == O_RETURN)
                return True
            if kind == 3 and oid in (O_HARVEST, O_ACOLYTE):
                return False                         # CAbilityAcolyteHarvest §5.1: harvest on a non-mine -> 0x26
            if oid in (O_SMART, O_RETURN, O_RESUME) and self._dropoff(target, u.owner, self._want_kind(u, inf)):
                carry, _ckind = self._carried(u)
                if carry <= 0:
                    return True if oid == O_RETURN else None
                self.stop(u)
                self._start_drop(u, kind, inf, target)
                return True
        if oid == O_SMART and target is not None:
            return None
        # point / automatic orders
        if oid in (O_AUTOGOLD, O_AUTOLUMBER, O_HARVEST, O_RESUME, O_RETURN, O_ACOLYTE, O_WISP):
            return self._order_auto(u, kind, inf, oid, x, y)
        return None

    def entangle(self, u, mine):
        """Aent order 0xd00b3/0xd00b4 on a neutral mine: it becomes this player's egol, the
        existing res_amt stays, and Aegm/Aenc supply the cycle and the wisp slots
        (CAbilityEntangle §5.2/§5.3/§6.3; CAbilityEntangledGoldMine §3.3; Aenc §3.3)."""
        base, mi = self.mine_info(mine)
        if base != AGLD or _id4(mine) == 'egol':
            return False
        keep = getattr(mine, 'res_amt', None)
        if keep is None:
            lv = mi.get('lv') if mi else None
            keep = max(0, int(lv['A'])) if lv is not None else 0
        tid = _rawcode('egol')
        if tid in self.N.unit_types:
            self.eng.morph_type(mine, tid, food=False)
        self._ensure_mine_ability(mine, 'Aegm')
        self._ensure_mine_ability(mine, 'Aenc')
        if mine.owner is not u.owner:
            mine.owner = u.owner
            self.eng.ue.sync(mine)                                # BUGS FF-UPG: the new owner's levels
            self.eng.owner_changed(mine)
        mine.res_amt = max(0, int(keep))
        return True

    def _ensure_mine_ability(self, mine, id4):
        code = _rawcode(id4)
        if code in mine.abilities or code not in self.eng.ab.defs:
            return
        mine.abilities[code] = 1
        self.eng.ab.on_add(mine, code)

    def _carried(self, u):
        """(amount, kind) of the load: the live state first, then a load kept across an order (stop)."""
        s = self.st.get(u)
        if s is not None and s.get('carry', 0) > 0:
            return s['carry'], s.get('kind', 0)
        return self.load.get(u, (0, 0))

    def _want_kind(self, u, inf):
        carry, ckind = self._carried(u)
        if carry:
            return ckind
        return 0 if inf['gold'] else 1

    def _order_auto(self, u, kind, inf, oid, x, y):
        carry, ckind = self._carried(u)
        if oid == O_RETURN:
            if not carry:
                return True
            self.stop(u)
            d = self._drop_for(u, ckind)
            if d is None:
                return False
            self._start_drop(u, kind, inf, d)
            return True
        if oid in (O_RESUME, O_HARVEST) and carry:
            self.stop(u)
            d = self._drop_for(u, ckind)
            if d is None:
                return False
            self._start_drop(u, kind, inf, d)
            return True
        want_gold = oid in (O_AUTOGOLD, O_ACOLYTE) or (inf['gold'] and not inf['lumber'])
        want_lumber = oid in (O_AUTOLUMBER, O_WISP) or not want_gold
        if oid in (O_AUTOGOLD, O_ACOLYTE):
            want_gold, want_lumber = True, False
        elif oid in (O_AUTOLUMBER, O_WISP):
            want_gold, want_lumber = False, True
        px = x if x is not None else u.x
        py = y if y is not None else u.y
        best = None
        if want_gold and inf['gold']:
            best = self._nearest_mine(u, px, py, inf['rad'])
        if best is None and want_lumber and inf['lumber']:
            best = self._nearest_tree(u, px, py, inf['rad'])
        if best is None:
            return False
        self.stop(u)
        if getattr(best, 'tname', '') == 'destructable':
            self._start_tree(u, kind, inf, best, False)
        else:
            if kind == 2 or kind == 3:
                self._start_mine(u, kind, inf, best, False)
            else:
                self._start_mine(u, kind, inf, best, False)
        return True

    # ---------------------------------------------------------------- start a trip
    def _state(self, u):
        carry, kind = self.load.get(u, (0, 0))
        return self.st.setdefault(u, dict(mode=M_NONE, kind=kind, target=None, carry=carry, next=0, mine=None,
                                          drop=None, back=None, retry_at=0, r1=0, r2=0))

    def _start_tree(self, u, kind, inf, tree, stop_after):
        s = self._state(u)
        s.update(mode=M_GO, worker=kind, res=1, target=tree, mine=None, drop=None,
                 back=None if stop_after else tree, next=0, retry_at=0, r1=0, r2=0)
        if kind == 2:
            s['per'] = inf['per']
            s['dur'] = inf['dur']
        else:
            s['per'] = inf['per']
        s['cg'] = inf['cg']
        s['cl'] = inf['cl']
        self._remember(u, tree)
        self._walk(u, tree.x, tree.y)

    def _start_mine(self, u, kind, inf, mine, stop_after):
        s = self._state(u)
        s.update(mode=M_GO, res=0, target=mine, mine=None, drop=None,
                 back=None if stop_after else mine, next=0, retry_at=0, r1=0, r2=0)
        s['cg'] = inf['cg']
        s['cl'] = inf['cl']
        s['per'] = inf['per']
        s['dur'] = inf.get('dur', 1.0)
        self._remember(u, mine)
        self._walk(u, mine.x, mine.y)

    def _start_drop(self, u, kind, inf, drop):
        s = self._state(u)
        s.update(mode=M_DROP, drop=drop, target=drop, next=0, retry_at=0, r1=0, r2=0)
        self.drop_rem[u] = drop
        self._walk(u, drop.x, drop.y)

    def _walk(self, u, x, y):
        u.ab_arr = False
        sw = self.st.get(u)
        if sw is not None:
            sw['skt'] = None                  # PERF-MELEE: a new leg wakes the reach test
        self.eng.refund_build(u)              # PORT10-Build: a new harvest order aborts an armed build (CAbilityBuild §5.3)
        u.order = O_HARVEST
        u.target = None
        u.pick = None
        u.wg_via = None
        u.atk_target = None
        u.gpt = None
        u.explicit = False
        u.chasing = False
        u.build = None
        u.tx, u.ty = r32(x), r32(y)
        u.path = None
        u.seg = None
        u.next_scan = self.eng.now()
        self.eng.wake(u, self.eng.now())
        if self.eng.eff_speed(u, True) > 0:
            self.N.moving.add(u)
        else:
            self.N.moving.discard(u)

    def _reach(self, u, t):
        if getattr(t, 'tname', '') == 'destructable':
            bx0, by0, bx1, by1 = self.eng.dest_box(t)
            dx = bx0 - u.x if u.x < bx0 else (u.x - bx1 if u.x > bx1 else 0.0)
            dy = by0 - u.y if u.y < by0 else (u.y - by1 if u.y > by1 else 0.0)
            return math.hypot(dx, dy)
        # NOTES_combat §in-range (0x6f08ce80): dist <= range + coll_worker + coll_target.  The
        # caller adds the worker's collision; the target's full radius comes off here, so a hall
        # (coll 176) is reached from its near edge instead of forcing the worker around it.
        return math.hypot(t.x - u.x, t.y - u.y) - float(getattr(t, 'coll', 0.0) or 0.0)

    def _arrived(self, u, s, t, now):
        """PERF-MELEE (invisible): the reach test of a walking worker. A worker cannot cover more than SKIP_STEP per step, so after a
        failed test it cannot be there for (distance - reach) / SKIP_STEP steps (at most SKIP_MAX) and the test sleeps for the
        same target; with an active waygate (a crossing jumps) it always runs. Same answer as testing every step."""
        if self.eng.pab:
            if u.ab_arr:
                u.ab_arr = False
                return True
            return False
        if s.get('skt') is t and now < s.get('sku', 0):
            return False
        r = self._reach(u, t)
        lim = (REACH_TREE if getattr(t, 'tname', '') == 'destructable' else REACH) + float(getattr(u, 'coll', 0.0) or 0.0)
        if r <= lim:
            return True
        g = self.eng.grid
        if g is None or not g.gact:
            k = int((r - lim) / SKIP_STEP)
            if k > 1:
                s['skt'] = t
                s['sku'] = now + (k if k < SKIP_MAX else SKIP_MAX)
        return False

    # ---------------------------------------------------------------- search
    def _nearest_tree(self, u, x, y, rad):
        best, bd = None, None
        for o in self.eng.dests:
            if not getattr(o, 'dalive', False) or getattr(o, 'removed', False):
                continue
            t = self.eng.dest_types.get(o.id4)
            if not t or not (int(t.get('targ', 0)) & UD.T_TREE):
                continue
            d = (o.x - x) ** 2 + (o.y - y) ** 2
            if rad > 0 and d > rad * rad:
                continue
            if bd is None or (d, o.dser) < (bd, best.dser):
                best, bd = o, d
        return best

    def _nearest_mine(self, u, x, y, rad):
        best, bd = None, None
        acol = u is not None and self.info(u)[0] == 3
        for o in self.N.units:
            if not o.alive or o.dead or o is u:
                continue
            if not self.is_mine(o):
                continue
            if acol and not self.accepts_acolyte(o):
                continue                             # CAbilityAcolyteHarvest §5.3: 0x28 for a non-Abgm mine
            d = (o.x - x) ** 2 + (o.y - y) ** 2
            if rad > 0 and d > rad * rad:
                continue
            if bd is None or (d, o.serial) < (bd, best.serial):
                best, bd = o, d
        return best

    def _dropoff(self, b, owner, kind):
        if b is None or not b.alive or b.dead or not b.is_bldg or b.constructing:
            return False
        if b.owner is not owner:
            return False
        # GOLD-RATE: the drop-off test is the type's receive ability (the engine's accept-gold / accept-lumber flags): Argl
        # (hall: both), Abgl (necropolis / haunted mine: gold), Arlm (mill: lumber).  A barracks / altar / shop is NOT a drop-off.
        # Only a type without any abilList (synthetic test types) keeps the old stand-in (hall ids, a Trains list, a mine).
        al = str(b.data.get('abilList', '') or '')
        if al:
            codes = al.split(',')
            if kind == 0:
                return 'Argl' in codes or 'Abgl' in codes
            return 'Argl' in codes or 'Arlm' in codes
        id4 = _id4(b)
        trains = str(b.data.get('Trains', '') or '')
        if kind == 0:
            return id4 in GOLD_DROP or bool(trains) or self.is_mine(b)
        return id4 in LUMBER_DROP or bool(trains)

    def _nearest_drop(self, u, kind):
        best, bd = None, None
        for b in self.N.units:
            if not self._dropoff(b, u.owner, kind):
                continue
            d = (b.x - u.x) ** 2 + (b.y - u.y) ** 2
            if bd is None or (d, b.serial) < (bd, best.serial):
                best, bd = b, d
        return best

    def _drop_for(self, u, kind):
        """HarvestReturn §3 [obs 0x6f450ad0]: reuse the remembered drop-off while the drop-off test still passes;
        otherwise search and remember the first valid building."""
        b = self.drop_rem.get(u)
        if b is not None:
            if self._dropoff(b, u.owner, kind):
                return b
            self.drop_rem.pop(u, None)
        b = self._nearest_drop(u, kind)
        if b is not None:
            self.drop_rem[u] = b
        return b

    # ---------------------------------------------------------------- carry (NOTES_harvest §2 Add)
    def _credit_carry(self, u, s):
        """deposit the carried amount now (the old-type deposit of Add); no walk."""
        amount = max(0, int(s.get('carry', 0)))
        if amount > 0:
            self.N.credit_resource(u.owner, 1 if s.get('kind', 0) == 0 else 2, amount * 10)
        s['carry'] = 0
        self.load.pop(u, None)

    def _add_carry(self, u, s, amount, lumber):
        """NOTES_harvest §2 Add: a different old type is deposited first; clamp to the load cap."""
        amount = max(0, int(amount))
        if amount <= 0:
            return False
        ck = 1 if lumber else 0
        if s.get('carry', 0) > 0 and s.get('kind', 0) != ck:
            self._credit_carry(u, s)
        cap = max(0, int(s.get('cl', 0) if lumber else s.get('cg', 0)))
        s['carry'] = min(cap, max(0, int(s.get('carry', 0))) + amount)
        s['kind'] = ck
        return s['carry'] + amount >= cap

    # ---------------------------------------------------------------- step
    def step(self):
        now = self.eng.now()
        for u in list(self.st):
            s = self.st.get(u)
            if s is None or s.get('mode', M_NONE) == M_NONE:
                if s is not None:
                    self.stop(u)                 # keeps a partial load in self.load (C# keeps hCarry)
                continue
            self.eng.wk += 1
            if not u.alive or u.dead or u.hidden and s.get('mode') != M_IN_MINE:
                self.stop(u)
                continue
            m = s['mode']
            if m == M_GO:
                t = s.get('target')
                if not self._valid(t):
                    self._target_gone(u, s)
                    continue
                if self._arrived(u, s, t, now):
                    if getattr(t, 'tname', '') == 'destructable':
                        self._enter_tree(u, s)
                    else:
                        self._enter_mine(u, s, t)
                else:
                    self._retry_wait(u, s, t, now, False)
            elif m == M_WORK:
                self._work_tree(u, s, now)
            elif m == M_DROP:
                d = s.get('drop')
                if not self._dropoff(d, u.owner, s.get('kind', 0)):
                    self._target_gone(u, s)
                    continue
                if self._arrived(u, s, d, now):
                    self._deposit(u, s)
                else:
                    self._retry_wait(u, s, d, now, True)
            elif m == M_WAIT_MINE:
                mine = s.get('target')
                if not self._valid(mine):
                    self._target_gone(u, s)
                    continue
                if self._slot_free(mine):
                    self._enter_mine(u, s, mine)
            elif m == M_IN_MINE:
                if s.get('trip') and now >= s['next']:
                    self._leave_trip(u, s)
        self._step_mines(now)

    def _retry_wait(self, u, s, t, now, drop):
        """NOTES_harvest §1.2 [obs 0x6f451600 / 0x6f4501b0]: a route that ended out of reach waits 500 ms at the
        resource / 5 s at the drop-off and re-posts the walk.  The resource counter falls back to another target at
        its limit; the drop-off counter gives up after RETRY_DROP_N failed arrivals (the limit is [obs], 'give up' [inf])."""
        ra = s.get('retry_at', 0)
        if ra:
            if now < ra:
                return
            s['retry_at'] = 0
            self._walk(u, t.x, t.y)
            return
        if u in self.N.moving or u.order == O_HARVEST:
            return
        if drop:
            s['r1'] = s.get('r1', 0) + 1
            if s['r1'] >= 2:
                s['r1'] = 0
                s['r2'] = s.get('r2', 0) + 1
            if s.get('r2', 0) >= RETRY_DROP_N:
                self._give_up(u, s)
                return
            s['retry_at'] = now + _steps(RETRY_DROP)
        else:
            s['r1'] = s.get('r1', 0) + 1
            if s['r1'] >= RETRY_RES_N:
                s['back'] = None                 # at the limit fall back to a target search, not the same unreachable one
                self._target_gone(u, s)
                return
            s['retry_at'] = now + _steps(RETRY_RES)
        self.N.moving.discard(u)

    def _give_up(self, u, s):
        """[inf] the drop-off retry counter ran out: the worker stops with its load (the chapter only says 'gives up')."""
        s['mode'] = M_NONE
        s['retry_at'] = 0
        u.order = 0
        self.N.moving.discard(u)
        self._leave_slot(u)

    def _valid(self, t):
        if t is None:
            return False
        if getattr(t, 'tname', '') == 'destructable':
            return getattr(t, 'dalive', False) and not getattr(t, 'removed', False) and t.life > 0.0
        return getattr(t, 'alive', False) and not getattr(t, 'dead', False)

    def _enter_tree(self, u, s):
        s['mode'] = M_WORK
        if s.get('worker') == 2:
            s['next'] = self.eng.now() + _steps(float(s.get('dur', 1.0)))
        else:
            s['next'] = self.eng.now() + _steps(self._dmgpt(u))
        u.order = O_HARVEST
        u.path = None
        self.N.moving.discard(u)

    def _dmgpt(self, u):
        if self.eng.has_weapon(u, 0):
            return float(self.eng.wv(u, 0, 'dmgpt') or 0.0)
        return 0.0

    def _cool(self, u, s):
        if self.eng.has_weapon(u, 0):
            c = float(self.eng.wv(u, 0, 'cool') or 0.0)
            if c > 0.0:
                return c
        return float(s.get('dur', 0.0) or 1.35)

    def _work_tree(self, u, s, now):
        if now < s['next']:
            return
        t = s.get('target')
        if not self._valid(t):
            self._target_gone(u, s)
            return
        if s.get('worker') == 2:
            if s.get('per', 0) > 0:
                self.N.credit_resource(u.owner, 2, int(s['per']) * 10)
            s['next'] = now + _steps(float(s.get('dur', 1.0)))
            return
        if s.get('res', s.get('kind', 0)) == 1:
            if s.get('per', 0) <= 0:
                self._target_gone(u, s)
                return
            self._add_carry(u, s, s['per'], True)
            from .engine import DestPt
            self.eng.dest_strike(u, DestPt(t, u, self.eng.dest_box(t)), 0, 1.0)
        if s.get('kind', 0) == 1 and s['carry'] >= s['cl'] and s['cl'] > 0:
            self._go_home(u, s)
        s['next'] = now + _steps(self._cool(u, s))

    def _enter_mine(self, u, s, mine):
        base, mi = self.mine_info(mine)
        if base is None:
            self._target_gone(u, s)
            return
        kindn = self.info(u)[0]
        if kindn in (2, 3):
            if not self._slot_free(mine):
                s['mode'] = M_WAIT_MINE
                s['target'] = mine
                u.order = O_HARVEST
                self.N.moving.discard(u)
                return
            self._slot_list(mine)['slots'].append(u)
            s['mode'] = M_IN_MINE
            s['mine'] = mine
            u.order = O_HARVEST
            u.hidden = True
            u.path = None
            self.N.moving.discard(u)
            if self.mines[mine]['next'] < 0:
                self.mines[mine]['next'] = self.eng.now() + _steps(mi['interval'])
            return
        if self.eng.pab and base == AGLD:
            # STEER-4b [inf]: with the two-level finder the walk is the real one (to the footprint of the mine) and the worker spends the mine's mining duration
            # (Agld DataB, 1 s) inside, one worker per mining slot (Agld DataC); the gold comes out when it leaves (the mine-leave handler hands it over)
            if int(getattr(mine, 'res_amt', 0) or 0) <= 0:
                self._end_empty(u, s)               # an exhausted mine (the port keeps it alive): the harvest ends, no walk / hide churn
                return
            if not self._slot_free(mine):
                s['mode'] = M_WAIT_MINE
                s['target'] = mine
                u.order = O_HARVEST
                self.N.moving.discard(u)
                return
            self._slot_list(mine)['slots'].append(u)
            s['mode'] = M_IN_MINE
            s['mine'] = mine
            s['trip'] = True
            s['next'] = self.eng.now() + _steps(mi['interval'])
            u.order = O_HARVEST
            u.hidden = True
            u.path = None
            self.N.moving.discard(u)
            return
        # Ahar / Ahrl: one instant trip from the mine pool
        last = self.take_at.get(u)
        if last is not None and self.eng.now() < last + _steps(TRIP_MIN) and self._lens(u, mine):
            return                                      # HALL-MINE guard: the worker waits at the mine
        take = 0
        pool = getattr(mine, 'res_amt', None)
        if base in (AGLD, AEGM):
            take = min(max(0, int(s.get('cg', 0))), max(0, int(pool or 0)))
            if take > 0:
                mine.res_amt = max(0, int(pool) - take)
        else:
            take = max(0, int(s.get('cg', 0)))
        if take <= 0:
            self._target_gone(u, s)
            return
        self.take_at[u] = self.eng.now()
        self._add_carry(u, s, take, False)
        self._go_home(u, s)

    def _leave_trip(self, u, s):
        """STEER-4b: the mining duration of a peasant / peon trip is over: the gold of the trip comes out with the worker, which walks to the drop-off."""
        mine = s.get('mine')
        self._leave_slot(u)
        s['trip'] = False
        s['mine'] = None
        self._unhide(u)
        base, mi = self.mine_info(mine)
        pool = getattr(mine, 'res_amt', None)
        take = 0
        if base is not None:
            take = min(max(0, int(s.get('cg', 0))), max(0, int(pool or 0)))
        if take <= 0:
            self._end_empty(u, s)
            return
        mine.res_amt = max(0, int(pool) - take)
        self._add_carry(u, s, take, False)
        self._go_home(u, s)

    def _end_empty(self, u, s):
        """STEER-4b [inf]: the mine is exhausted (the decompile does not say what becomes of an empty mine; the port keeps it): a worker without a load ends its harvest, with a load it takes it home first."""
        if s.get('carry', 0) > 0:
            self._go_home(u, s)
            return
        s['mode'] = M_NONE
        u.order = 0
        self.N.moving.discard(u)

    def _lens(self, u, mine):
        """HALL-MINE guard: the nearest gold drop-off and the mine are so close that one spot lies in both reach discs
        (centre distance <= the two limits of _arrived added up): the worker could take and deposit without walking.
        The melee start layouts (hall to mine 583 .. 820) stay outside: 210 + 336 = 546 for a peasant, a Town Hall, a mine."""
        d = self._nearest_drop(u, 0)
        if d is None:
            return False
        uc = float(getattr(u, 'coll', 0.0) or 0.0)
        lim = 2.0 * (REACH + uc) + float(getattr(mine, 'coll', 0.0) or 0.0) + float(getattr(d, 'coll', 0.0) or 0.0)
        dx = d.x - mine.x
        dy = d.y - mine.y
        return math.sqrt(dx * dx + dy * dy) <= lim

    def _slot_free(self, mine):
        base, mi = self.mine_info(mine)
        if base is None:
            return False
        return len(self._slot_list(mine)['slots']) < mi['cap']

    def _step_mines(self, now):
        for mine in list(self.mines):
            md = self.mines[mine]
            base, mi = self.mine_info(mine)
            if base is None or not self._valid(mine):
                for w in list(md['slots']):
                    self._eject(mine, w)
                continue
            if not md['slots']:
                md['next'] = -1
                continue
            if md['next'] < 0:
                md['next'] = now + _steps(mi['interval'])
                continue
            if now < md['next']:
                continue
            md['next'] = now + _steps(mi['interval'])
            pool = getattr(mine, 'res_amt', None)
            empty = base in (AGLD, AEGM) and int(pool or 0) <= 0
            if base == ABGM:
                # ORACLE-ECON 2026-10-30: Abgm pays DataA ONCE per DataB cycle for the whole mine,
                # not once per acolyte.  Three acolytes therefore earn 10 gold/s (10/1 s), the real
                # 1.26a steady rate; the old per-slot loop paid 3 x 10 = 30 gold/s.
                amount = self._mine_pay(mine, base, mi, None, s_pool=pool)
                if amount > 0 and md['slots']:
                    self.N.credit_resource(md['slots'][0].owner, 1, amount * 10)
            else:
                for w in list(md['slots']):
                    if (self.st.get(w) or {}).get('trip'):
                        continue                  # STEER-4b: a peasant's trip is paid when it leaves, not by the cycle
                    amount = self._mine_pay(mine, base, mi, w, s_pool=pool)
                    if amount > 0:
                        self.N.credit_resource(w.owner, 1, amount * 10)      # PLAYER_GOLD = 1, tenths
                    if empty:
                        self._leave_slot(w)
                        self._eject(mine, w)
            if empty:
                md['next'] = -1

    def _mine_pay(self, mine, base, mi, w, s_pool=None):
        if base == ABGM:
            return max(0, int(mi['lv']['A']))
        pool = getattr(mine, 'res_amt', None)
        if base == AEGM:
            # [inf] Aegm DataA is the mine's gold per cycle when all Aenc slots work: per wisp = DataA / slots.
            # CAbilityEntangledGoldMine §5 (mining cycle) is Open.
            n = max(1, int(mi.get('cap', 1) or 1))
            amount = max(0, int(mi.get('amount', 0)))
            take = amount // n if amount > 0 else 0
            if amount > 0 and take <= 0:
                take = 1
            take = min(take, max(0, int(pool or 0)))
            if take > 0:
                mine.res_amt = max(0, int(pool) - take)
            return take
        if pool is None:
            return 0
        take = min(max(0, int(mi['lv']['C']) if mi['lv'] is not None else 10), max(0, int(pool)))
        if take > 0:
            mine.res_amt = max(0, int(pool) - take)
        return take

    def _go_home(self, u, s):
        d = self._drop_for(u, s['kind']) if s.get('carry', 0) else None
        if d is None:
            s['mode'] = M_NONE
            u.order = 0
            self.N.moving.discard(u)
            return
        s['mode'] = M_DROP
        s['drop'] = d
        s['target'] = d
        s['retry_at'] = 0
        s['r1'] = 0
        s['r2'] = 0
        self._walk(u, d.x, d.y)

    def _deposit(self, u, s):
        self._credit_carry(u, s)
        back = s.get('back')
        if back is not None and self._valid(back):
            s['target'] = back
            s['mode'] = M_GO
            s['retry_at'] = 0
            s['r1'] = 0
            s['r2'] = 0
            self._walk(u, back.x, back.y)
            return
        # BUGS MELEE-LUMBER: the tree / mine of the trip is gone (felled by this or another worker): the worker goes on with the
        # nearest one of the same resource around the old spot, as in WC3, instead of standing idle at the hall
        kind, inf = self.info(u)
        t = None
        if back is not None and kind >= 0 and inf is not None:
            if s.get('res', s.get('kind', 0)) == 0 and inf['gold']:
                t = self._nearest_mine(u, back.x, back.y, inf['rad'])
            elif inf['lumber']:
                t = self._nearest_tree(u, back.x, back.y, inf['rad'])
        if t is not None:
            if getattr(t, 'tname', '') == 'destructable':
                self._start_tree(u, kind, inf, t, False)
            else:
                self._start_mine(u, kind, inf, t, False)
            return
        r = self._remembered(u, s.get('res', s.get('kind', 0))) if (back is not None and kind >= 0 and inf is not None) else None
        if r is not None and r is not back:
            if getattr(r, 'tname', '') == 'destructable':
                self._start_tree(u, kind, inf, r, False)
            else:
                self._start_mine(u, kind, inf, r, False)
            return
        s['mode'] = M_NONE
        u.order = 0
        self.N.moving.discard(u)

    def _target_gone(self, u, s):
        back = s.get('back')
        if s.get('carry', 0) > 0:
            self._go_home(u, s)
            return
        if s.get('res', s.get('kind', 0)) == 1 and s.get('per', 0) <= 0:
            # nothing can be chopped: end the trip instead of re-entering the same tree forever
            s['mode'] = M_NONE
            u.order = 0
            self.N.moving.discard(u)
            self._leave_slot(u)
            return
        if back is not None and self._valid(back):
            s['target'] = back
            s['mode'] = M_GO
            s['retry_at'] = 0
            s['r1'] = 0
            s['r2'] = 0
            self._walk(u, back.x, back.y)
            return
        # look for another resource of the same kind
        kind, inf = self.info(u)
        t = None
        if kind >= 0 and inf is not None:
            if s.get('res', s.get('kind', 0)) == 0 and inf['gold']:
                t = self._nearest_mine(u, u.x, u.y, inf['rad'])
            elif inf['lumber']:
                t = self._nearest_tree(u, u.x, u.y, inf['rad'])
        if t is not None and t is not s.get('target'):
            if getattr(t, 'tname', '') == 'destructable':
                self._start_tree(u, kind, inf, t, False)
            else:
                self._start_mine(u, kind, inf, t, False)
            return
        r = self._remembered(u, s.get('res', s.get('kind', 0))) if (kind >= 0 and inf is not None) else None
        if r is not None and r is not s.get('target') and r is not back:
            if getattr(r, 'tname', '') == 'destructable':
                self._start_tree(u, kind, inf, r, False)
            else:
                self._start_mine(u, kind, inf, r, False)
            return
        s['mode'] = M_NONE
        u.order = 0
        self.N.moving.discard(u)
        self._leave_slot(u)
