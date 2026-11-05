"""W8 items in the world and in inventories for the reference engine (docs/NOTES_items.md §3–§5; behaviour only).

Item objects (natives.Item) carry tid, x, y, life, charges, the record flags, the carrier unit and slot. The
inventory of a unit is its first ability of the `AInv` class (DataA slots, DataB drop on death, DataC can use items,
DataD can get items). The engine calls:
  on_death(u)          carrier death: items dropped (slots last → first), before EVENT_UNIT_DEATH (A-IT2)
  pickup orders        smart / getitem on an item: walk within PickupItemRange, then add (engine.issue / step)
Assumptions A-IT* are listed in docs/engine_w5.md (W8 section).
"""
from .vm import r32
from . import itemdata as ID

PICKUP_RANGE_DEFAULT = 150.0
DROP_RANGE_DEFAULT = 100.0
GIVE_RANGE_DEFAULT = 150.0
PAWN_RANGE_DEFAULT = 300.0
ORDER_DROPITEM = 852001                        # orders.py: dropitem (the move-slot orders are 852002 + slot)
DEATH_LIFE = r32(0.4)                          # NOTES_items §3.2: the item life threshold (the same 0.4 as a unit)
ITEM_SEARCH_R = 12                             # cells searched for the nearest valid item point [inf: the unit-spawn square]
OP_MOVE, OP_DROP, OP_GIVE, OP_PAWN = 1, 2, 3, 4
POWERUP_GOLD = 'AIgo'
POWERUP_LUMBER = 'AIlu'                        # CAbilityItemGiveLumber §2: the owner gains DataA lumber [inf: DataA]
POWERUP_FX = ('AIhe', 'AIha', 'AIma', 'AIre', 'AIra', 'AImr')   # the restore family runs its effect on the carrier (NOTES_items §4.5.1)
ORB_CODES = ('AIfb', 'AIob', 'AIpb', 'AIsb')   # NOTES_items §11.3 item orbs (hit priority 18 + slot)
TOMES = {'AIam', 'AIim', 'AIsm', 'AIxm'}   # permanent +DataA agi / +DataB int / +DataC str (NOTES_items §5)


def _rc(s):
    return (ord(s[0]) << 24) | (ord(s[1]) << 16) | (ord(s[2]) << 8) | ord(s[3])


class Items:
    def __init__(self, eng):
        self.eng = eng
        self.N = eng.N
        self.ab = eng.ab
        m = eng.misc
        try:
            self.pick_range = float(str(m.get('PickupItemRange', PICKUP_RANGE_DEFAULT)).split(',')[0])
        except ValueError:
            self.pick_range = PICKUP_RANGE_DEFAULT
        self.drop_range = self._range(m, 'DropItemRange', DROP_RANGE_DEFAULT)
        self.give_range = self._range(m, 'GiveItemRange', GIVE_RANGE_DEFAULT)
        self.pawn_range = self._range(m, 'PawnItemRange', PAWN_RANGE_DEFAULT)
        self.death_time = self._range(m, 'EffectDeathTime', 5.0)   # CItem §5.4: the dead object lives 5 s
        self.serial = 0
        self.by_serial = {}              # input serial -> item (A-IT3)
        self.ground = []                 # items on the ground (creation order) — presentation / pickup queries
        self.dying = []                  # CItem §5.4: dead items whose 0xd01c1 timer has not fired

    @staticmethod
    def _range(m, key, default):
        try:
            return float(str(m.get(key, default)).split(',')[0])
        except ValueError:
            return default

    # ------------------------------------------------------------ data
    def inventory(self, u):
        """-> the level record of u's inventory ability (first of class AInv) or None."""
        for aid, code, lv in self.ab.abil_list(u):
            if code == 'AInv' and lv is not None:
                return lv
        return None

    def size(self, u):
        lv = self.inventory(u)
        return min(6, int(lv['A'])) if lv is not None else 0

    def abilities(self, it):
        out = []
        for a in str(it.data.get('abilList', '') or '').split(','):
            a = a.strip()
            if len(a) == 4 and _rc(a) in self.ab.defs:
                out.append(_rc(a))
                if len(out) == 4:
                    break                                           # at most 4 [obs]
        return out

    # ------------------------------------------------------------ objects
    def created(self, it):
        self.serial += 1
        it.serial = 1000000 + self.serial                           # input serials of items (A-IT3)
        it.dead_at = -1                                             # the 5 s dead-object timer (CItem §5.4)
        self.by_serial[it.serial] = it
        self.ground.append(it)

    def valid_point(self, x, y):
        """A-IT1 / NOTES_items §3.1 / §3.2: the item placement search 0x6f08d8f0 (radius 0) keeps a point whose
        own cell is valid and otherwise walks the measured square spiral of the unit-spawn query 0x6f66ee10
        (oracle ssp1 / ssp_oC11: the point itself, then cells south, east, north, west with arm lengths
        1, 1, 2, 2, ...), placing at the first walkable cell's centre. The acceptance test is [inf]: the item's
        own pathing flag word ([obs 0x6f1ddeb0], the item's slot-89 word) is not decoded here, so the port uses
        the ground walkability the item shares with a collision-0 unit; without a pathing grid the point is
        unchanged."""
        g = self.eng.grid
        if g is None:
            return r32(x), r32(y)
        cx, cy = g.cell_of(x, y)
        if g.walkable(cx, cy):
            return r32(x), r32(y)                                   # the query returns the point itself when valid
        r = ITEM_SEARCH_R
        sx, sy = cx, cy
        dx, dy = 0, -1
        arm = 1
        while arm <= 2 * r + 1:
            for _rep in range(2):
                for _i in range(arm):
                    sx += dx
                    sy += dy
                    if abs(sx - cx) <= r and abs(sy - cy) <= r and g.walkable(sx, sy):
                        return r32(g.ox + (sx + 0.5) * 32.0), r32(g.oy + (sy + 0.5) * 32.0)
                dx, dy = -dy, dx
            arm += 1
        return r32(x), r32(y)                                       # no valid cell: the search leaves the point

    def place(self, it, x, y):
        it.carrier = None
        it.slot = -1
        it.x, it.y = self.valid_point(x, y)                         # A-IT1: nearest valid point
        it.hidden = False
        if it.live and it not in self.ground:
            self.ground.append(it)

    # ------------------------------------------------------------ add / remove (§4.3, §4.4)
    def can_receive(self, u, it, by_order):
        lv = self.inventory(u)
        if lv is None or u.dead or getattr(u, 'illusion', False):
            return False
        if by_order and lv['D'] == 0:
            return False
        if it.fl & ID.FLAG_POWERUP:
            return lv['C'] != 0
        return any(s is None for s in u.items[:self.size(u)])

    def add(self, u, it, by_order=False, slot=None):
        """-> True if u took the item (slot or powerup)."""
        if it is None or not it.live or it.carrier is not None or not self.can_receive(u, it, by_order):
            return False
        lv = self.inventory(u)
        if it.fl & ID.FLAG_POWERUP:
            return self.powerup(u, it)
        n = self.size(u)
        if slot is None:
            slot = next((k for k in range(n) if u.items[k] is None), -1)
        if slot < 0 or slot >= n or u.items[slot] is not None:
            return False
        if it in self.ground:
            self.ground.remove(it)
        it.carrier, it.slot, it.hidden = u, slot, True
        u.items[slot] = it
        it.owner = u.owner
        if lv['C'] != 0:
            self.carry_item(u, it, 1)
        self.N.unit_event(u, 'PICKUP_ITEM', {'item': self.N.H(it), 'manipulator': self.N.H(u)})
        return True

    def remove(self, u, it, x=None, y=None, events=True):
        """from the slot: DROP first (item still there, bonuses still on), then abilities off, slot cleared, placed."""
        if it is None or it.carrier is not u:
            return False
        if events:
            self.N.unit_event(u, 'DROP_ITEM', {'item': self.N.H(it), 'manipulator': self.N.H(u)})
        if it.carrier is not u:
            return True                                             # removed again inside the trigger
        lv = self.inventory(u)
        if lv is not None and lv['C'] != 0:
            self.carry_item(u, it, -1)
        if 0 <= it.slot < 6 and u.items[it.slot] is it:
            u.items[it.slot] = None
        self.place(it, u.x if x is None else x, u.y if y is None else y)
        return True

    def set_life(self, it, v):
        """CItem §8 / NOTES_items §3.2 (the +0x124 absolute life setter, reachable through SetWidgetLife):
        the value is clamped at 0 (there is no upper clamp) and stored as float32; a living item (old life above
        0.4) whose new life reaches the 0.4 threshold (+ the 0.001 epsilon) takes the death path. A non-hidden
        death keeps the object for EffectDeathTime; a hidden one deletes it at once (_die_later). The 5 s
        dead-object lifetime is modelled since PORT13-Inventory (A-IT7 fixed; `_die_later` / `step`)."""
        if it is None:
            return
        old = it.life
        v = r32(float(v))
        if not v >= 0.0:                                          # NaN / negative clamp (as the C# twin)
            v = 0.0
        it.life = v
        if old > DEATH_LIFE and it.life - DEATH_LIFE <= 0.001:
            self.kill(it)

    def kill(self, it, remove=False):
        """item death (RemoveItem, life ≤ 0.4): a carried item is dropped first (DROP fires) [obs §3.2].

        `remove` is the RemoveItem path: the native hides the item before the death, and the drop through
        remove() -> place() clears the hidden flag, so the hidden decision is taken here (NOTES_items §3.2
        [obs 0x6f2c0ab0]). A carried item is hidden in the port's presentation model, so its own `hidden` is
        not a death flag; only a ground item's hidden / FLAG_HIDDEN means "delete at once"."""
        if not it.live:
            return
        hidden = bool(remove) or (it.carrier is None and
                                  (getattr(it, 'hidden', False) or bool(it.fl & ID.FLAG_HIDDEN)))
        if it.carrier is not None:
            self.remove(it.carrier, it)
        it.life = r32(0.0)                                          # the death path writes life 0 [obs]
        it.live = False
        if it in self.ground:
            self.ground.remove(it)
        self._die_later(it, hidden)

    def _die_later(self, it, hidden=None):
        """CItem §5.3/§5.4 death path: a hidden item is deleted at once, otherwise the object keeps its handle for
        `EffectDeathTime` (5 s) and is removed by the 0xd01c1 timer. The world list already lost it in `kill`.
        A triggering DROP / PICKUP handler may already have deleted the object: never destroy it twice."""
        if not it.alive:
            return
        if hidden is None:
            hidden = getattr(it, 'hidden', False) or bool(it.fl & ID.FLAG_HIDDEN)
        if hidden:
            self.eng.N.vm.H.destroyed(it)                           # hidden: the death path deletes it at once
            return
        if getattr(it, 'dead_at', -1) >= 0:
            return
        from .engine import steps_of
        it.dead_at = self.eng.now() + steps_of(r32(self.death_time))
        self.dying.append(it)

    def step(self):
        """CItem §5.4: fire the 0xd01c1 timers of dead items whose EffectDeathTime has elapsed."""
        if not self.dying:
            return
        now = self.eng.now()
        keep = []
        for it in self.dying:
            if it.dead_at >= 0 and now >= it.dead_at:
                it.dead_at = -1
                if it.alive:                                # a handler may have deleted it during an outer death
                    self.eng.N.vm.H.destroyed(it)
            else:
                keep.append(it)
        self.dying = keep

    def on_death(self, u):
        """§4.4 carrier death: slots last → first; DataB or the item's drop flag → slot cleared first, then DROP."""
        if getattr(u, 'illusion', False):
            return
        lv = self.inventory(u)
        if lv is None:
            return
        for k in range(5, -1, -1):
            it = u.items[k]
            if it is None or not (lv['B'] != 0 or (it.fl & ID.FLAG_DROP)):
                continue
            u.items[k] = None
            self.N.unit_event(u, 'DROP_ITEM', {'item': self.N.H(it), 'manipulator': self.N.H(u)})
            if it.carrier is not u:
                continue
            if lv['C'] != 0:
                self.carry_item(u, it, -1)
            self.place(it, u.x, u.y)

    def reclamp(self, u):
        """§5.5: a shrinking DataA drops every slot at or above the new size. The pair is cleared first and the item
        leaves without a DROP event (the raw removal), placed at the carrier. No effect when the size did not shrink."""
        lv = self.inventory(u)
        if lv is None:
            return
        n = max(0, min(6, int(lv['A'])))
        for k in range(5, n - 1, -1):
            it = u.items[k]
            if it is None:
                continue
            u.items[k] = None
            if lv['C'] != 0:
                self.carry_item(u, it, -1)
            self.place(it, u.x, u.y)

    def detach(self, u, lv=None):
        """CAbilityInventory §5.2 detach (UnitRemoveAbility / a morph that drops AInv): every slot last → first is
        cleared first, the item abilities leave, and the item is either placed at the carrier (normal) or killed
        (an illusion carrier) — no DROP_ITEM event either way. The +0x20 bit 0x100 destroy flag and the transport
        point are not modelled (no producer in the port). `lv` is the level record the ability had."""
        apply_ab = lv is not None and lv.get('C', 0) != 0
        for k in range(5, -1, -1):
            it = u.items[k]
            if it is None:
                continue
            u.items[k] = None
            if apply_ab:
                self.carry_item(u, it, -1)
            it.carrier = None
            it.slot = -1
            if getattr(u, 'illusion', False):
                self.kill(it)                       # §5.2 step 3: an illusion's items are destroyed, not dropped
            else:
                self.place(it, u.x, u.y)            # §5.2 step 4: the raw removal (place through the pathing search)

    # ------------------------------------------------------------ effects (§5)
    def carry_item(self, u, it, d):
        """the abilities of a carried item join (d = 1) / leave (-1) the unit; the slot of the item is the orbs' hit priority
        offset and the sibling of AIpb is the item's own Apo2-class ability (NOTES_items §11.3, §11.7)."""
        abs_ = self.abilities(it)
        sib = 0
        for a in abs_:
            if self.ab.defs[a]['code'] == 'Apo2':
                sib = a
                break
        for a in abs_:
            self.carry_effect(u, a, d, it.slot, sib)

    def carry_effect(self, u, aid, d, slot=0, sib=0):
        r = self.ab.defs.get(aid)
        if r is None or not r['lv']:
            return
        code, lv = r['code'], r['lv'][0]
        if code == 'AIat':
            u.dmg_int += int(lv['A']) * d                           # A-IT4: an integer attack bonus
        elif code == 'AIab' and getattr(u, 'hero', False):
            self.eng.hero.add_bonus(u, int(lv['C']) * d, int(lv['A']) * d, int(lv['B']) * d)
        elif code == 'AIas':
            self.eng.set_attack_factor(u, u.atk_delta + lv['A'] * d)
        elif code == 'AIde' and not u.illusion:
            u.armor_bonus += lv['A'] * d                            # NOTES_abilities §5.38 (A-AB12)
        elif code == 'AIms':
            self.eng.item_bonus(u, u.item_spd, aid, d)              # NOTES_items §10: flat move speed (W9-4)
            self.eng.item_speed_calc(u)
        elif code == 'AImm':
            self.eng.add_max_mana(u, lv['A'] * d)                   # NOTES_items §10: max mana, fraction kept
        elif code == 'AIrm' and getattr(u, 'hero', False):
            self.eng.item_bonus(u, u.item_mrg, aid, d)              # NOTES_items §10: share of the base mana regen
            self.eng.item_mreg_calc(u)
        elif code == 'Arel' and not u.illusion:
            self.eng.item_bonus(u, u.item_lrg, aid, d)              # NOTES_items §11.1: + DataA hit points per second
            self.eng.item_lreg_calc(u)
        elif code == 'Aamk' and getattr(u, 'hero', False):
            self.eng.hero.add_bonus(u, int(lv['C']) * d, int(lv['A']) * d, int(lv['B']) * d)   # NOTES_items §11.2
        elif code in ORB_CODES and not u.illusion:
            u.dmg_int += int(lv['A']) * d                           # NOTES_items §11.3: + trunc(DataA) like AIat (every orb)
            if d > 0:
                u.orbs.append([slot, aid, sib])
            else:
                for k, o in enumerate(u.orbs):
                    if o[0] == slot and o[1] == aid:
                        del u.orbs[k]
                        break

    def powerup(self, u, it):
        """§4.3: carrier set, the abilities cast at once, PICKUP, then the item dies (→ DROP). A powerup whose
        every spell-class ability refuses the cast (AIhe at full life 0x19, AIma at full mana 0x1a, AIre with both
        pools full 0x1b) stays on the ground with no event [obs NOTES_items §4.3]. No abilities at all still
        consumes the item."""
        abs_ = self.abilities(it)
        ok_any = not abs_
        for a in abs_:
            r = self.ab.defs.get(a)
            if r is None or not r['lv']:
                ok_any = True                       # an unmodelled ability is assumed to cast (never silently kept)
            elif r['code'] in POWERUP_FX:
                if self.eng.sp.powerup_ok(u, r['code']):
                    ok_any = True
            else:
                ok_any = True                       # gold / lumber / tomes / AIem / an unmodelled class
        if not ok_any:
            return False                            # all casts failed: the item stays on the ground, no event
        if it in self.ground:
            self.ground.remove(it)
        it.carrier, it.slot, it.hidden = u, -1, True
        for a in abs_:
            r = self.ab.defs.get(a)
            code, lv = r['code'], r['lv'][0]
            if code == POWERUP_GOLD:
                pl = u.owner
                self.N.set_player_state(pl, 1, pl.state.get(1, 0) + int(lv['A']))      # A-IT5: to the owner
            elif code == POWERUP_LUMBER:
                pl = u.owner
                self.N.set_player_state(pl, 2, pl.state.get(2, 0) + int(lv['A']))      # CAbilityItemGiveLumber §2: no tax / share
            elif code in POWERUP_FX:
                self.eng.sp.powerup_fx(u, code, lv)                                    # NOTES_items §4.5.1: effect at once, no gate
            elif code in TOMES and getattr(u, 'hero', False):
                self.eng.hero.add_bonus(u, int(lv['C']), int(lv['A']), int(lv['B']), permanent=True)
            elif code == 'AIem' and getattr(u, 'hero', False):
                self.eng.hero.add_xp(u, float(lv['A']))
        self.N.unit_event(u, 'PICKUP_ITEM', {'item': self.N.H(it), 'manipulator': self.N.H(u)})
        if it.carrier is u:
            self.N.unit_event(u, 'DROP_ITEM', {'item': self.N.H(it), 'manipulator': self.N.H(u)})
        it.carrier = None
        it.life = r32(0.0)                                          # the death path writes life 0 [obs]
        it.live = False
        self._die_later(it)                                         # hidden powerup death: deleted at once
        return True

    # ------------------------------------------------------------ pickup order (§4.3)
    def order_pickup(self, u, it, smart=False):
        """the general order check of CAbilityInventory §8.1/§8.2 at issue time: a smart order uses the can-receive
        test without the DataD test, a getitem order the §4.2 test with it (0xd4). A refusal takes no order."""
        if it is None or not it.live or it.carrier is not None or self.inventory(u) is None:
            return False
        if not self.can_receive(u, it, not smart):
            return False
        eng = self.eng
        eng.clear_order(u)
        u.order = 851971 if smart else 851981                   # smart / getitem (orders.py)
        u.pick = it
        u.tx, u.ty = it.x, it.y
        if self.in_reach(u, it):
            u.pick = None
            u.order = 0
            return self.add(u, it, by_order=not smart) or True
        if eng.can_move(u) and eng.eff_speed(u, True) > 0:
            self.N.moving.add(u)
            return True
        u.pick = None
        u.order = 0
        return False

    def in_reach(self, u, it):
        dx, dy = it.x - u.x, it.y - u.y
        return dx * dx + dy * dy <= self.pick_range * self.pick_range     # A-IT6: centre distance

    def arrived(self, u):
        """movement: the walking unit reached the item → pick it up (if still there)."""
        it = u.pick
        if it is not None and u.iop is not None:
            return self._iop_done(u) if self._iop_reach(u, u.iop) else False
        if it is None or not self.in_reach(u, it):
            return False
        by_order = u.order == 851981
        u.pick = None
        u.order = 0
        self.N.moving.discard(u)
        u.path = None
        self.eng.log('pickup u%d i%d' % (u.serial, it.serial))
        self.add(u, it, by_order=by_order)
        u.next_scan = self.eng.now()
        self.eng.wake(u, self.eng.now())
        return True


    # ------------------------------------------------------------ inventory orders (HERO-UI, NOTES_items §4.4)
    def can_drop(self, u, it):
        """§4.4 can-drop check (0x6f56de70): the carrier is no illusion, the item is alive in this inventory, the inventory
        allows dropping (DataE) and the item type is droppable (flag 0x400)."""
        lv = self.inventory(u)
        if lv is None or u.dead or u.illusion or it is None or not it.live or it.carrier is not u:
            return False
        if lv.get('E', 1) == 0:
            return False
        return bool(it.fl & ID.FLAG_DROPPABLE)

    def _reslot(self, u, it, old, new):
        """an item changes slot: only the orbs' hit priority (18 + slot) follows the slot (carry_item)."""
        mine = self.abilities(it)
        for o in u.orbs:
            if o[0] == old and o[1] in mine:
                o[0] = new
        it.slot = new

    def move_slot(self, u, slot, dest):
        """itemdrag (move-slot orders 852002 + dest, CAbilityInventory §8.1): the item in `slot` goes to the slot `dest`
        below the inventory size; an occupied slot swaps. No walk, no event [inf]. -> True when moved."""
        n = self.size(u)
        if not (0 <= slot < 6 and 0 <= dest < n) or slot == dest or u.dead or u.illusion:
            return False
        a = u.items[slot]
        if a is None or a.carrier is not u:
            return False
        b = u.items[dest]
        self._reslot(u, a, slot, -7)
        if b is not None:
            self._reslot(u, b, dest, slot)
        self._reslot(u, a, -7, dest)
        u.items[dest], u.items[slot] = a, b
        return True

    def input_op(self, u, slot, op, x, y, ts):
        """player input kind 12 (the item argument of dropitem / itemdrag does not fit the order wire): the carried item in
        `slot`; op 1 move to the slot ts, 2 drop at the point (x, y), 3 give to the unit with the serial ts."""
        if not 0 <= slot < 6:
            return False
        it = u.items[slot]
        if op == OP_MOVE:
            return self.move_slot(u, slot, ts)
        if op == OP_DROP:
            return self.order_drop(u, it, x, y)
        if op == OP_GIVE:
            t = self.eng.by_serial.get(ts)
            return t is not None and self.order_give(u, it, t)
        return False

    def _walk_iop(self, u, it, kind, x, y, target):
        """the carried item walks with the unit: drop / give happens once the unit is within DropItemRange / GiveItemRange.
        A unit that cannot walk (rooted / stunned / speed 0) and is out of range refuses BEFORE anything is cleared: its
        current order stays (HERO-UI review 4)."""
        eng = self.eng
        op = (kind, x, y, target)
        reach = self._iop_reach(u, op)
        if not reach and not (eng.can_move(u) and eng.eff_speed(u, True) > 0):
            return False
        eng.clear_order(u)
        u.order = ORDER_DROPITEM
        u.pick = it
        u.iop = op
        u.tx, u.ty = x, y
        if reach:
            return self._iop_done(u)
        self.N.moving.add(u)
        return True

    def follow_give(self, u):
        """movement goal of a pending give / pawn: the target's LIVE position (HERO-UI review 5). A drop (point) and a
        dead / removed target keep the stored point. -> True when the walk follows a target (repath on cell change)."""
        op = u.iop
        if op is None or op[0] not in (OP_GIVE, OP_PAWN) or op[3] is None:
            return False
        t = op[3]
        if not t.alive or t.dead:
            return False
        u.tx, u.ty = t.x, t.y
        return True

    def pawn_shop(self, t):
        """the give target test 0x6f56dfd0: a target with an enabled Apit is a pawn target (NOTES_items §4.4).
        Like ShNeutral, the ability needs its level record (an Apit without one is not enabled)."""
        return t is not None and any(c == 'Apit' and _lv is not None for _a, c, _lv in self.ab.abil_list(t))

    def _iop_reach(self, u, op):
        kind, x, y, t = op
        if t is not None:
            x, y = t.x, t.y
        dx, dy = x - u.x, y - u.y
        r = self.pawn_range if kind == OP_PAWN else self.give_range if kind == OP_GIVE else self.drop_range
        return dx * dx + dy * dy <= r * r

    def _iop_done(self, u):
        """the unit is in range: perform the pending drop / give / pawn -> True."""
        it, (kind, x, y, t) = u.pick, u.iop
        u.pick = None
        u.iop = None
        u.order = 0
        self.N.moving.discard(u)
        u.path = None
        if not self.can_drop(u, it):
            return True                                             # §4.4: the drop / give / pawn is refused, item stays
        if kind == OP_PAWN:
            if t is not None and t.alive and not t.dead:
                self.eng.shops.pawn(u, it, t)                   # PAWN_ITEM → DROP → price → item dies
            u.next_scan = self.eng.now()
            self.eng.wake(u, self.eng.now())
            return True
        if kind == OP_GIVE and (t is None or not t.alive or t.dead or not self.can_receive(t, it, False)):
            return True                                             # §4.4: the item stays with the giver (full / gone)
        self.remove(u, it, x if kind == OP_DROP else None, y if kind == OP_DROP else None)   # DROP_ITEM fires
        if kind == OP_GIVE and it.carrier is None and it.live:
            self.add(t, it)                                         # PICKUP_ITEM fires (slot or powerup path)
        u.next_scan = self.eng.now()
        self.eng.wake(u, self.eng.now())
        return True

    def order_drop(self, u, it, x, y):
        """dropitem (852001) with a point: walk within DropItemRange, then the item is placed at the point through
        the place routine (CItem_p1 §5.5), so a blocked point lands on the nearest valid cell (A-IT1 / PORT11)."""
        if not self.can_drop(u, it):
            return False
        return self._walk_iop(u, it, OP_DROP, r32(x), r32(y), None)

    def order_give(self, u, it, t):
        """dropitem (852001) with a unit: a target with an enabled Apit is a pawn target (PawnItemRange, item must be
        pawnable 0x57); a target that can receive the item gets a give within GiveItemRange; a target that cannot (no
        inventory, full, other shop) gets the item dropped next to it (§4.4). Self / illusion / dead refuse. NO
        owner / alliance test: the 1.26a target test 0x6f56dfd0 has none [obs, CAbilityInventory_p1 §8.5] -- enemy and
        neutral units take the item too (the world UI only offers own / allied targets, W3Command). A hidden target
        (0xaa) or a hidden item (0xa9) is refused (NOTES_items §4.4)."""
        if t is u or not t.alive or t.dead or t.illusion or t.hidden or not self.can_drop(u, it):
            return False
        if it.fl & ID.FLAG_HIDDEN:
            return False
        if self.pawn_shop(t):
            if not (it.fl & ID.FLAG_PAWNABLE):
                return False                                        # 0x57: a shop target needs a pawnable item
            return self._walk_iop(u, it, OP_PAWN, t.x, t.y, t)
        if self.can_receive(t, it, False):
            return self._walk_iop(u, it, OP_GIVE, t.x, t.y, t)
        return self._walk_iop(u, it, OP_DROP, t.x, t.y, None)
