"""W8 shops: buying items and units (docs/NOTES_items.md §7, §7.6; behaviour only, clean-room).

A unit type's sale lists become hidden shop abilities at unit creation [obs 0x6f1a5190]: `Makeitems` → 'Amai'
(CAbilityMakeItem: race shops, buyer through the shop's `Aall`), `Sellitems` → 'Asei' (CAbilitySellItem: buyer
through `Aneu`), `Sellunits` → 'Asel' (CAbilitySellUnit). Each has up to 12 slots (list positions; ids that are no
item / unit type leave the slot empty) with a stock. The explicit `Asid` / `Asud` (the *Dynamic* classes) read the
same lists; a type with both gets one slot set (A-SH6).

The Dynamic classes add a slot limit (11 after init) and 12 age counters; the stock natives (AddUnitToStock …,
NOTES_items §7.7, A-SH9) work only on a unit that carries `Asud` / `Asid` (dyn_* below); their slots live in the
K_ASEL / K_ASEI row.

Stock is evaluated lazily from step stamps (same result as the game's agent timers, A-SH3): a slot keeps `cur`, the
end of its start delay and the start of its regen timer; ticks at start + k·period count once vm.steps reached them.

A purchase is player input kind 5 (docs/engine_w5.md §6): player p, shop serial, order id = the item / unit type id,
optional buyer serial (p's own unit: becomes p's manually chosen active unit at this shop). The engine calls
  init_unit(u)                 the shop slots of a new unit (engine.init_unit)
  buy(p, shop, tid, buyer)     validation (§7.1 codes) + execution, synchronous; -> (code, sold object)
  listing(shop, p)             [(kind, id, gold, lumber, stock, max, code)] for the HUD (W3Vm.EngShopList)
  pawn(u, it, shop)            Apit (§7.4): the carrier sells an item to the shop
Assumptions A-SH* are listed in docs/engine_w5.md (W8 shops).
"""
from .vm import r32
from . import itemdata as ID

SPS = 200
SLOTS = 12
K_AMAI, K_ASEI, K_ASEL = 0, 1, 2                 # slot classes (list order = creation order [obs 0x6f1a5190])
LISTS = (('Makeitems', K_AMAI), ('Sellitems', K_ASEI), ('Sellunits', K_ASEL))
ALLIANCE_SHARED_SPELLS = 4                       # [obs 0x6f2a2f70: mask +0x78 = 0x38 + 16·4; ext common.j order]
ALLIANCE_SHARED_CONTROL = 6                      # [obs 0x6f2a2dc0: mask +0x98 = 0x38 + 16·6]
ALLIANCE_SHARED_ADVANCED_CONTROL = 7             # [obs 0x6f2a2e60: mask +0xa8 = 0x38 + 16·7]
PAWN_RANGE_DEFAULT = 300.0
PAWN_RATE_DEFAULT = 0.5
OK = 0
E_NOT_SOLD, E_DELAY, E_STOCK, E_NO_ACTIVE, E_GOLD, E_LUMBER, E_FOOD = 0xdd, 0x59, 0x58, 0x40, 8, 9, 7
E_HERO_REQ = 0xb7                                 # Asel +0x340: a hero type's requirements (NOTES_items §7.6)
E_LIMIT = 3                                       # Asel +0x310: the food CAP, below the ceiling (code 7)
DYN = {'Asud': K_ASEL, 'Asid': K_ASEI}          # the Dynamic sell classes (NOTES_items §7.7) → their slot row
DYN_LIMIT = 11                                   # the init's slot limit [obs 0x6f46d79f / 0x6f46e6cf]


def _rc(s):
    return (ord(s[0]) << 24) | (ord(s[1]) << 16) | (ord(s[2]) << 8) | ord(s[3])


def _steps(sec):
    """A2: durations → steps rounded up, at least 1 (engine.steps_of)."""
    from .engine import steps_of
    return steps_of(float(sec))


def _misc_f(m, key, default):
    try:
        return float(str(m.get(key, default)).split(',')[0])
    except ValueError:
        return default


class Slot:
    __slots__ = ('kind', 'id', 'max', 'cur', 'delay_end', 'regen_t0', 'period')

    def __init__(self, kind, tid, mx, start, regen, now):
        self.kind = kind
        self.id = tid
        self.max = mx
        self.period = _steps(regen)
        self.regen_t0 = None                      # regen timer idle
        if kind != K_AMAI and start > 0:          # start delay: 0, then 1 when it ends [obs 0x6f46c920]
            self.cur = 0
            self.delay_end = now + _steps(start)
        else:                                     # full at once (Amai has no start delay [obs 0x6f46ed10])
            self.cur = mx
            self.delay_end = None

    def sync(self, now):
        if self.delay_end is not None:
            if now < self.delay_end:
                return
            self.cur = 1
            t = self.delay_end
            self.delay_end = None
            if self.cur != self.max:
                self.regen_t0 = t
        if self.regen_t0 is not None:
            k = (now - self.regen_t0) // self.period
            if k > 0:
                if self.cur + k >= self.max:
                    self.cur = self.max          # timer stopped at max [obs 0x6f46c800]
                    self.regen_t0 = None
                else:
                    self.cur += k
                    self.regen_t0 += k * self.period


class Shops:
    def __init__(self, eng):
        self.eng = eng
        self.N = eng.N
        self.ab = eng.ab
        m = eng.misc
        self.pawn_range = _misc_f(m, 'PawnItemRange', PAWN_RANGE_DEFAULT)
        self.pawn_rate = _misc_f(m, 'PawnItemRate', PAWN_RATE_DEFAULT)

    # ------------------------------------------------------------ setup
    def init_unit(self, u):
        d = u.data
        slots = None
        now = self.eng.vm.steps
        for key, kind in LISTS:
            ids = [a.strip() for a in str(d.get(key) or '').split(',')][:SLOTS]
            row = [None] * SLOTS
            got = False
            for i, a in enumerate(ids):
                if len(a) != 4:
                    continue
                tid = _rc(a)
                if kind == K_ASEL:
                    rec = self.N.unit_types.get(tid)
                    if rec is None:
                        continue
                    row[i] = Slot(kind, tid, int(rec.get('stockMax', 0) or 0), float(rec.get('stockStart', 0) or 0),
                                  float(rec.get('stockRegen', 0) or 0), now)
                else:
                    rec = self.eng.item_types.get(tid)
                    if rec is None:
                        continue
                    row[i] = Slot(kind, tid, int(rec.get('stockMax', 0) or 0), float(rec.get('stockStart', 0) or 0),
                                  float(rec.get('stockRegen', 0) or 0), now)
                got = True
            if got:
                if slots is None:
                    slots = {}
                slots[kind] = row
        u.shop = slots
        if slots is not None:
            u.shop_manual = {}
        u.shop_dyn = None
        u.shop_ready = True
        for _aid, c, _lv in self.ab.abil_list(u):
            if c in DYN:
                self.dyn_ensure(u, DYN[c])

    # ------------------------------------------------------------ the Dynamic classes (NOTES_items §7.7, A-SH9)
    def dyn_ensure(self, u, kind):
        """a unit with Asud / Asid: its row (the type list's, else empty) with limit 11 and 12 ages of 0; also for
        UnitAddAbility at run time (the added ability runs its init as at creation [inf])."""
        if not getattr(u, 'shop_ready', False):
            return                                    # init_unit does it once the abilities are in
        if u.shop is None:
            u.shop = {}
            u.shop_manual = {}
        if kind not in u.shop:
            u.shop[kind] = [None] * SLOTS
        if u.shop_dyn is None:
            u.shop_dyn = {}
        if kind not in u.shop_dyn:
            u.shop_dyn[kind] = [DYN_LIMIT, [0] * SLOTS]

    def dyn_of(self, u, kind):
        """the unit's Dynamic state when it carries the class (the natives look the ability up [obs 0x6f48a4e0])."""
        if u is None or not getattr(u, 'shop_dyn', None) or kind not in u.shop_dyn:
            return None
        code = 'Asud' if kind == K_ASEL else 'Asid'
        if not any(c == code for _a, c, _lv in self.ab.abil_list(u)):
            return None
        return u.shop_dyn[kind]

    def type_stock(self, kind, tid):
        rec = (self.N.unit_types.get(tid) if kind == K_ASEL else self.eng.item_types.get(tid)) or {}
        return int(rec.get('stockMax', 0) or 0), float(rec.get('stockRegen', 0) or 0)

    def dyn_add(self, u, kind, tid, cur, mx):
        """0x6f46dad0 / 0x6f46e910: the slot of tid below the limit, else the one with the largest age (first on
        ties); cur clamped by the slot's old max, max (0 / −1 → the type's stockMax), cur clamped again; the regen timer
        restarts when cur ≠ max (period = the type's stockRegen); the start delay is left; all ages + 1, this one 0."""
        d = self.dyn_of(u, kind)
        if d is None:
            return
        limit, ages = d
        row = u.shop[kind]
        now = self.eng.vm.steps
        hit, best = None, 0
        for i in range(limit):
            sl = row[i]
            if sl is not None and sl.id == tid:
                hit = i
                break
            if ages[i] > ages[best]:
                best = i
        i = best if hit is None else hit
        sl = row[i]
        tmax, regen = self.type_stock(kind, tid)
        nmax = tmax if mx in (0, -1) else mx
        if sl is None:
            c = cur if cur < nmax else nmax
            sl = Slot(kind, tid, nmax, 0, regen, now)
            row[i] = sl
        else:
            sl.sync(now)
            c = cur if cur < sl.max else sl.max
            c = c if c < nmax else nmax
            sl.id, sl.max, sl.period = tid, nmax, _steps(regen)
        sl.cur = c
        sl.regen_t0 = now if c != nmax else None
        for j in range(SLOTS):
            ages[j] += 1
        ages[i] = 0
        self.eng.log('stock u%d add %d slot %d %d/%d' % (u.serial, tid, i, c, nmax))

    def dyn_remove(self, u, kind, tid):
        """0x6f46dca0 / 0x6f46eae0: the first of the 12 slots with tid → id 0, regen stopped (stock, max, age kept)."""
        if self.dyn_of(u, kind) is None:
            return
        now = self.eng.vm.steps
        for sl in u.shop[kind]:
            if sl is not None and sl.id == tid:
                sl.sync(now)
                sl.id = 0
                sl.regen_t0 = None
                return

    def dyn_slots(self, u, kind, n):
        """0x6f46dd10 / 0x6f46eb50: n unsigned, > 10 → 11; slots n…11 emptied with age = the largest + 1."""
        d = self.dyn_of(u, kind)
        if d is None:
            return
        if n < 0 or n > 10:
            n = 11
        ages = d[1]
        m = max(ages)
        now = self.eng.vm.steps
        for i in range(n, SLOTS):
            sl = u.shop[kind][i]
            if sl is not None:
                sl.sync(now)
                sl.id = 0
                sl.regen_t0 = None
            ages[i] = m + 1
        d[0] = n

    def dyn_all(self, kind, fn, *args):
        """the All-variants: every living unit with the class, creation order [inf: the registry order is Open]."""
        for u in list(self.N.units):
            if u.alive and self.dyn_of(u, kind) is not None:
                fn(u, kind, *args)

    # ------------------------------------------------------------ the shop's Aneu / Aall (§7.2, §7.5)
    def neutral(self, shop, code):
        """the level record of the shop's first ability of class `code` ('Aneu' / 'Aall'), or None."""
        for aid, c, lv in self.ab.abil_list(shop):
            if c == code and lv is not None:
                return lv
        return None

    def qualifies(self, shop, lv, u, item=False):
        """0x6f459f10 (A-SH1; the 0x200 flag and the entry gate are not modelled). A1 (NOTES_items 7.8.1, oracle 1.26a):
        the reach is DataA + the buyer's collision size (centre distance to the shop), and an ITEM purchase needs a buyer
        with an inventory (a Wisp is refused even with DataB 16); unit / mercenary purchases (item=False) need none."""
        if u is None or not u.alive or u.dead or u.hidden or u.illusion or u.owner.id > 11:
            return False
        dx, dy = u.x - shop.x, u.y - shop.y
        r = float(lv['A']) + float(u.coll)
        if dx * dx + dy * dy > r * r:
            return False
        if item and self.eng.itm.inventory(u) is None:
            return False
        b = int(lv['B'])
        if b == 0 and not self.ab.is_hero(u):
            return False
        if b & 1 and self.eng.itm.inventory(u) is None:
            return False
        if b & 2 and u.is_bldg:
            return False
        return True

    def active(self, shop, lv, pid, skip=None, item=False):
        """A-SH1: p's active unit at the shop: the manually chosen one while it qualifies, else p's qualifying
        units — a hero first, then the nearest (ties: lower serial)."""
        if lv is None:
            return None
        man = shop.shop_manual.get(pid) if getattr(shop, 'shop_manual', None) is not None else None
        if man is not None and man.owner.id == pid and self.qualifies(shop, lv, man, item):
            return man
        best, bh, bd = None, False, 0.0
        for u in self.N.units:
            if u is skip or u.owner.id != pid or not self.qualifies(shop, lv, u, item):
                continue
            h = self.ab.is_hero(u)
            dx, dy = u.x - shop.x, u.y - shop.y
            d2 = dx * dx + dy * dy
            if best is None or (h and not bh) or (h == bh and d2 < bd):
                best, bh, bd = u, h, d2
        return best

    def flag(self, q, p, t):
        """GetPlayerAlliance(Player(q), Player(p), t) without allocating handles; the same player counts."""
        if q == p:
            return True
        if p < 0 or p >= 16 or q < 0 or q >= 16:
            return False                          # an out-of-range player index grants no control
        pq, pp = self.N.players[q], self.N.players[p]
        return bool(pp.hid and pq.alliance.get((pp.hid, t), False))

    def control(self, q, p):
        """0x6f2a2e60: player p may control q's units via advanced control (7) or shared control (6).
        The reference's extra player flag for the shared-control branch is not decoded, so the port keeps its
        existing shared-control test; advanced control is unconditional in both versions."""
        return self.flag(q, p, ALLIANCE_SHARED_ADVANCED_CONTROL) or self.flag(q, p, ALLIANCE_SHARED_CONTROL)

    def lookup(self, shop, lv, p, allied, check, powerup):
        """0x6f45a0b0: p's own active unit; else (allied) the active units of players q that share control with p,
        in player order — with `check` the first that can receive, otherwise the last found."""
        own = self.active(shop, lv, p, item=True)
        if own is not None or not allied:
            return own
        last = None
        for q in range(12):
            if not self.control(q, p):
                continue
            a = self.active(shop, lv, q, item=True)
            if a is None:
                continue
            if not check or self.receive_code(a, powerup) == OK:
                return a
            last = a
        return last

    def receive_code(self, u, powerup):
        """0x6f1b3460 without the full-allowance: §4.2 (0x12 no inventory, 0x11 full, 0xd6 powerup into DataC 0)."""
        itm = self.eng.itm
        lv = itm.inventory(u)
        if lv is None or u.dead or u.illusion:
            return 0x12
        if powerup:
            return OK if lv['C'] != 0 else 0xd6
        return OK if any(s is None for s in u.items[:itm.size(u)]) else 0x11

    # ------------------------------------------------------------ validation (issue time, §7.1 / §7.6)
    def find(self, shop, tid, last):
        """-> (kind, slot index, slot) of tid; the validation scans slots from the last, the execution from the
        first [obs 0x6f46e200 / 0x6f46e410]; classes in creation order."""
        if not getattr(shop, 'shop', None):
            return None
        for kind in (K_AMAI, K_ASEI, K_ASEL):
            row = shop.shop.get(kind)
            if row is None:
                continue
            rng = range(SLOTS - 1, -1, -1) if last else range(SLOTS)
            for i in rng:
                s = row[i]
                if s is not None and s.id == tid:
                    return kind, i, s
        return None

    def last_slot(self, shop, kind, tid):
        """0x6f46c6d0 (stock −1) scans the twelve ids from the last; with the same id twice the last slot loses the
        stock (the validation §8.1 and the execution filter scan differently, chapter note)."""
        row = shop.shop.get(kind) or ()
        for i in range(SLOTS - 1, -1, -1):
            s = row[i]
            if s is not None and s.id == tid:
                return s
        return None

    def any_powerup(self, shop, kind):
        for s in shop.shop.get(kind) or ():
            if s is not None and (self.eng.item_types.get(s.id) or {}).get('powerup'):
                return True
        return False

    def cost(self, kind, tid):
        if kind == K_ASEL:
            d = self.N.unit_types.get(tid) or {}
            return int(d.get('gold', 0) or 0), int(d.get('lumber', 0) or 0)
        d = self.eng.item_types.get(tid) or {}
        return int(d.get('goldcost', 0) or 0), int(d.get('lumbercost', 0) or 0)

    def price(self, p, kind, tid):
        """0x6f46cd60: (gold, lumber, free). A hero type (first character A…Z) costs 0 while the player's hero-token
        counter (state 3) is positive; otherwise the type's gold / lumber (CAbilitySellUnit_p1 §6)."""
        g, lu = self.cost(kind, tid)
        if kind == K_ASEL and 0x41 <= (tid >> 24) <= 0x5a and self.N.players[p].state.get(3, 0) > 0:
            return 0, 0, True
        return g, lu, False

    def cost_code(self, p, kind, tid):
        g, lu, _free = self.price(p, kind, tid)
        st = self.N.players[p].state
        if st.get(1, 0) < g:
            return E_GOLD
        if st.get(2, 0) < lu:
            return E_LUMBER
        return OK

    def code(self, shop, p, tid):
        """the refusal code of buying tid from the shop for player p (0 = ok), first failure wins."""
        if shop is None or not shop.alive or shop.dead:
            return E_NOT_SOLD
        f = self.find(shop, tid, True)
        if f is None:
            return E_NOT_SOLD
        kind, _i, s = f
        if kind == K_ASEL and 0x41 <= (tid >> 24) <= 0x5a:
            # 0x6f46ce60 / 0x6f46cee0 (Asel +0x344 / +0x340, NOTES_items §7.6): a hero type needs the type's and
            # the 'HERO' tech allowance (0xdd), then its requirements (0xb7); non-heroes have no test here.
            pl = self.N.players[p]
            if not self.eng.tech_ok(pl, tid):
                return E_NOT_SOLD
            if not self.eng.req_ok(pl, tid):
                return E_HERO_REQ
        if kind == K_AMAI:                       # 0x6f46f3e0: the shop owner shares spells with p (allied)
            if shop.owner.id > 15 or not self.flag(shop.owner.id, p, ALLIANCE_SHARED_SPELLS):
                return E_NOT_SOLD
            if self.lookup(shop, self.neutral(shop, 'Aall'), p, True, True, self.any_powerup(shop, kind)) is None:
                return E_NO_ACTIVE
        s.sync(self.eng.vm.steps)
        if s.delay_end is not None:
            return E_DELAY
        if s.cur < 1:
            return E_STOCK
        aneu = self.neutral(shop, 'Aneu')
        if aneu is not None and p < 12:          # 0x6f46c280 → 0x6f459a80
            if not any(self.control(q, p) and self.active(shop, aneu, q, item=kind != K_ASEL) is not None
                       for q in range(12)):
                return E_NO_ACTIVE
        c = self.cost_code(p, kind, tid)
        if c:
            return c
        if kind == K_ASEL:
            fused = int((self.N.unit_types.get(tid) or {}).get('fused', 0) or 0)
            pl = self.N.players[p]
            # Asel +0x300 food: code 7 only when the new unit passes the hard ceiling; a lower food cap (the
            # +0x310 headroom test) is code 3 (NOTES_items §7.6, CAbilityTrain_p1 §7 / CAbilityBaseBuild_p1 §8).
            # fused = 0 is not stopped here [inf: the LTW trace buys 0-food units while over cap].
            if fused > 0:
                if pl.state.get(5, 0) + fused > self.eng.food_ceiling:
                    return E_FOOD
                if not self.eng.food_ok(pl, fused):
                    return E_LIMIT
            return OK
        pw = bool((self.eng.item_types.get(tid) or {}).get('powerup'))
        b = self.lookup(shop, aneu, p, True, True, pw) if aneu is not None else None
        return self.receive_code(b, pw) if b is not None else OK

    # ------------------------------------------------------------ execution (§7.1 steps 1–7; §7.6)
    def buy(self, p, shop, tid, buyer=None):
        """-> (code, sold item / unit or None). Synchronous; events fire inside."""
        if buyer is not None and getattr(shop, 'shop', None) is not None:
            shop.shop_manual[p] = buyer           # the "select user" choice (A-SH2)
        c = self.code(shop, p, tid)
        if c:
            return c, None
        kind, i, s = self.find(shop, tid, False)
        s = self.last_slot(shop, kind, tid) or s
        if self.cost_code(p, kind, tid):
            return E_GOLD, None
        g, lu, free = self.price(p, kind, tid)
        N = self.N
        pl = N.players[p]
        if free:
            N.set_player_state(pl, 3, pl.state.get(3, 0) - 1)     # one hero token consumed (0x6f46d140)
        N.set_player_state(pl, 1, pl.state.get(1, 0) - g)
        N.set_player_state(pl, 2, pl.state.get(2, 0) - lu)
        if kind == K_ASEL:
            return OK, self.sell_unit(p, shop, s)
        code = 'Aall' if kind == K_AMAI else 'Aneu'
        lv = self.neutral(shop, code)
        b = self.lookup(shop, lv, p, True, True, self.any_powerup(shop, kind)) if lv is not None else None
        from . import natives as NN
        at = b if b is not None else shop
        it = NN.new_item(N, tid, at.x, at.y)
        self.eng.log('shop u%d sells %s to p%d buyer %s' % (shop.serial, it.data.get('id4'), p,
                                                             'u%d' % b.serial if b is not None else '-'))
        if b is not None and self.receive_code(b, bool(it.fl & ID.FLAG_POWERUP)) == OK:
            N.unit_event(shop, 'SELL_ITEM', {'seller': N.H(shop), 'buyer': N.H(b), 'solditem': N.H(it)})
            self.eng.itm.add(b, it)
        self.take(s)
        return OK, it

    def sell_unit(self, p, shop, s):
        """0x6f46d140 after the payment: the unit for p at the shop (A-SH5), stock −1, SELL with p's own active
        unit (no allied lookup) as the buying unit."""
        N = self.N
        nu = N.create_unit(N.players[p], s.id, shop.x, shop.y, 0.0)
        self.eng.log('shop u%d sells unit %s to p%d' % (shop.serial, nu.data.get('id4') if nu else '-', p))
        if nu is None:
            return None
        self.take(s)
        b = self.active(shop, self.neutral(shop, 'Aneu'), p, skip=nu)
        N.unit_event(shop, 'SELL', {'seller': N.H(shop), 'soldunit': N.H(nu), 'buyer': N.H(b)})
        return nu

    def take(self, s):
        """0x6f46c6d0: current −1; the regen timer starts if idle."""
        now = self.eng.vm.steps
        s.sync(now)
        s.cur -= 1
        if s.regen_t0 is None and s.cur != s.max:
            s.regen_t0 = now

    # ------------------------------------------------------------ HUD
    def listing(self, shop, p):
        out = []
        if shop is None or not getattr(shop, 'shop', None):
            return out
        now = self.eng.vm.steps
        for kind in (K_AMAI, K_ASEI, K_ASEL):
            for s in shop.shop.get(kind) or ():
                if s is None or s.id == 0:
                    continue
                s.sync(now)
                g, lu, _free = self.price(p, kind, s.id)
                out.append((1 if kind == K_ASEL else 0, s.id, g, lu, s.cur, s.max, self.code(shop, p, s.id)))
        return out

    # ------------------------------------------------------------ pawn (Apit, §7.4)
    def pawn(self, u, it, shop):
        """the carrier u sells the item it to the shop: -> True when sold (A-SH7: u must already be within
        PawnItemRange; no approach walk)."""
        if shop is None or not shop.alive or shop.dead or u is None or it is None or it.carrier is not u:
            return False
        if not any(c == 'Apit' and _lv is not None for _a, c, _lv in self.ab.abil_list(shop)):
            return False
        if not (it.fl & ID.FLAG_PAWNABLE) or not (it.fl & ID.FLAG_DROPPABLE) or u.illusion:
            return False
        dx, dy = shop.x - u.x, shop.y - u.y
        if dx * dx + dy * dy > self.pawn_range * self.pawn_range:
            return False
        N = self.N
        N.unit_event(u, 'PAWN_ITEM', {'seller': N.H(u), 'buyer': N.H(shop), 'solditem': N.H(it)})
        if it.carrier is not u:
            return True
        self.eng.itm.remove(u, it)                # DROP_ITEM fires
        uses = int(it.data.get('uses', 0) or 0)
        f = self.pawn_rate * (float(it.charges) / uses if uses else 1.0)
        g, lu = self.cost(K_ASEI, it.tid)
        pl = u.owner
        N.set_player_state(pl, 1, pl.state.get(1, 0) + int(g * f))
        N.set_player_state(pl, 2, pl.state.get(2, 0) + int(lu * f))
        self.eng.itm.kill(it)
        return True


def shop_lines(N):
    """harness check of the HUD listing (W3Vm.ShopLines): per living shop (creation order), players 0 and 1."""
    out = []
    sh = N.eng.shops
    for u in N.units:
        if not getattr(u, 'shop', None) or not u.alive:
            continue
        for p in (0, 1):
            for k, tid, g, lu, cur, mx, c in sh.listing(u, p):
                out.append('u%d p%d %d %d %d %d %d %d %d' % (u.serial, p, k, tid, g, lu, cur, mx, c))
    return out
