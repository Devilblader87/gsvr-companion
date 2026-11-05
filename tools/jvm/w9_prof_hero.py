#!/usr/bin/env python3
"""W9R2-5: hero-pick / hero-control play profile for the hero arenas and sieges (maps 9330 Boss Battle, 2637 X Hero
Siege 3.12, 10145 DBZ Hero Arena 4.4, 2043 DOTA 3.7, 7073 Custom Hero Arena 1.5c; SDBZ 9486 and Hero Plan Wars 6161 are
listed with xfail ids). Loaded by glob from tools/jvm/w9_play.py (interface there); w9_play.py is not edited.

Controller HeroPlay(w9_play.Play), one human (p0), every input through eng.post_input (recorded by w9_play for the Udon
replay). Acquisition, in this order, stopping at the first that works (docs/map_audit.md "Hero maps" lists per map which
one the JASS uses and the trigger):
  (a) p0 already owns a hero (Boss Battle),
  (b) every dialog shown to p0 is answered with the button whose label names a hero type, else button 0,
  (e) [added: the real mechanism of X Hero Siege 3.12] a hero of a pick owner stands in the selection area: p0 selects it
      twice within the map's 0.3 s window (input kind 8: select, clear, select; the map's selection trigger creates p0's copy),
  (c) buy a hero type from a neutral shop / tavern that lists hero types in Sellunits (input kind 5 = shops.buy),
  (d) chat command the map documents (-random / -pick ...) as input kind 7.
Control, every 2 s game time per owned hero: learn a point (SoakPlayer.hero_learn), retreat to the start when life < 30 %,
use one item per 20 s, cast the first ready spell at the nearest enemy (SoakPlayer.hero_cast, reach 900), else attack-move to
the nearest enemy. Clean room: values come from the map's own data at run time; nothing of the map's code is copied.
Report fields (rep['hero'], printed in the assert details): hero_count, hero_levels, spell_effect_events_by_hero,
kills_by_player[0], deaths of own heroes, revives, first_hero_s, events raised for p0 heroes.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
import jass                                          # noqa: E402
from jvm import w9_play as WP                        # noqa: E402
from jvm.engine import SPS                           # noqa: E402
from jvm.orders import ORDER_IDS                     # noqa: E402

TICK = 2 * SPS                  # control period per hero (2 s game time, steps)
ITEM_EVERY = 20 * SPS           # one item use per 20 s per hero
RETREAT_LIFE = 0.30             # walk back to the start below 30 % life
RESUME_LIFE = 0.70              # ... and fight again above 70 %
CAST_REACH = 900.0
NOTARGET_GAP = 10 * SPS         # a no-target spell (buff / nova) is repeated at most every 10 s
PICK_TRIES = 6                  # candidates tried by the selection / walk stages before the next stage takes over
SHOP_REACH = 300.0              # the buyer is walked until it stands this close to the shop
BLOCK_FOR = 60 * SPS            # an enemy position the hero could not approach is skipped this long
COMMIT_FOR = 40 * SPS           # a committed attack-move is not re-targeted for this long (commit=True)
WALK_WAIT = 15 * SPS           # a chooser walk gets this long
WATCH = ('SPELL_EFFECT', 'SPELL_CAST', 'USE_ITEM', 'DEATH', 'PICKUP_ITEM', 'LEVEL', 'SKILL')
CC = re.compile(r'\|c[0-9a-fA-F]{8}|\|r')


def install_counters(nat, owner=0):
    """count the unit / hero events whose unit is a hero (or the item user) of `player`: nat.hero_cnt[(suffix, hero serial)].
    Pure observation (the original methods run unchanged), idempotent; installed by HeroPlay and by the tests' hook."""
    if getattr(nat, 'hero_cnt', None) is not None:
        return nat.hero_cnt
    cnt = nat.hero_cnt = {}
    o_unit, o_hero = nat.unit_event, nat.hero_event

    def unit_event(u, suffix, data, player=None):
        if suffix in WATCH and u.owner.id == owner and getattr(u, 'hero', False) and not getattr(u, 'illusion', False):
            cnt[(suffix, u.serial)] = cnt.get((suffix, u.serial), 0) + 1
        return o_unit(u, suffix, data, player)

    def hero_event(u, suffix, data, slots=None):          # slots: W9R2-2 CTX_REVIVABLE (merge of W9R2-2 + W9R2-5)
        if u.owner.id == owner and getattr(u, 'hero', False) and not getattr(u, 'illusion', False):
            k = ('HERO_' + suffix, u.serial)
            cnt[k] = cnt.get(k, 0) + 1
        return o_hero(u, suffix, data, slots)
    nat.unit_event, nat.hero_event = unit_event, hero_event
    # per-second look at the owner's heroes (independent of any controller): the first step one lives, revives
    obs = nat.hero_obs = {'first': None, 'revives': 0, 'alive': {}, 'peak': 0, 'last_live': None}

    def watch(turn):
        vm = nat.eng.vm
        if vm.steps % SPS:
            return
        live = 0
        for u in nat.units:
            if u.owner.id != owner or not getattr(u, 'hero', False) or getattr(u, 'illusion', False) or not nat.eng.can_move(u):
                continue                                  # an immobile 'hero' (Dark Deeds H00Q score-code avatar) is not a controllable hero
            a = bool(u.alive and not u.dead)
            live += a
            if obs['alive'].get(u.serial) is False and a:
                obs['revives'] += 1
            obs['alive'][u.serial] = a
        obs['peak'] = max(obs['peak'], live)
        if live and obs['first'] is None:
            obs['first'] = vm.steps
        if live:
            obs['last_live'] = vm.steps               # W9R3-4 review: the last second an own hero was alive
    nat.eng.vm.on_turn.append(watch)
    return cnt


class HeroPlay(WP.Play):
    """see the module docstring. Hints (per map, constructor keywords): pick_owners = owners of the heroes that stand to be
    picked (path e), chat = chat strings to try in order (path d)."""

    def __init__(self, N, player=0, pick_owners=(), chat=(), roam=None, pre_chat=(), pick_mode='select', support=None, item_chain=(), commit=False, pick_gap=2, **kw):
        WP.Play.__init__(self, N, player=player, **kw)
        self.cnt = install_counters(N, player)
        self.pick_owners = tuple(pick_owners)
        self.chat = tuple(chat)
        self.pre_chat = tuple(pre_chat)   # map mode commands sent first (CHA)
        self.pick_gap = pick_gap          # turns between the clear and the second select (the map's double-select window, see PROFILES)
        self.pick_mode = pick_mode        # 'select' (path e) or 'walk' (path f)
        self.support = support            # None: any enemy; else only enemies with an allied fighter within `support` (or within 600 of the hero)
        self.commit = commit              # keep an attack-move order running up to COMMIT_FOR (A1)
        self.item_chain = tuple(item_chain)   # A1 (CHA): item classes (cls) the live hero buys one after the other
        self.chain_next, self.chain_tries, self.chain_walk_until = 0, 0, 0
        self.roam = roam               # None: attack-move to the nearest enemy anywhere; else only enemies within roam of the start
        self.acq_log = []                 # (ms, path, text): which acquisition path acted
        self.next_acq = 0
        self.stage, self.stage_n, self.walk_until = 0, 0, 0
        self.pick_seq = None              # path (e): (candidate serial, turn of the first select)
        self.shop_tries = {}
        self.shop_walks = 0
        self.chat_next = 0
        self.first_hero = None            # game step the first own hero was seen
        self.hnext = {}                   # hero serial -> next control step
        self.item_next = {}
        self.home = {}
        self.retreat = set()
        self.blocked = {}                 # (x // 256, y // 256) of an enemy the hero could not get closer to -> step until which it is skipped
        self.lastgo = {}                  # hero serial -> (step, x, y, target x, target y, block key) of the last attack-move

    # ---------------------------------------------------------------------------------------------------- helpers
    def own_heroes(self):
        return [u for u in self.N.units if u.owner.id == self.player and getattr(u, 'hero', False)
                and not getattr(u, 'illusion', False)]

    def live_heroes(self):
        return [u for u in self.own_heroes() if u.alive and not u.dead]

    def hero_names(self):
        if getattr(self, '_hn', None) is None:
            self._hn = {}
            for tid, d in self.N.unit_types.items():
                if 65 <= ((tid >> 24) & 0xff) <= 90 and int(d.get('Primary', 0) or 0) != 0:
                    nm = CC.sub('', str(d.get('name') or '')).strip().lower()
                    if nm:
                        self._hn[nm] = tid
        return self._hn

    def click_dialogs(self, now):
        """(b): the button whose label names a hero type, else button 0 (SoakPlayer.click_dialogs picks 0 only)."""
        N = self.N
        hp = N.H(N.players[self.player])
        seen = getattr(self, 'dlg_seen', 0)
        texts = N.last_text
        self.dlg_seen = len(texts)
        for t in texts[seen:]:
            if t[1] == 'dialog' and t[2] == hp and t[4]:
                o = N.o(t[3])
                btns = getattr(o, 'buttons', None)
                if not btns:
                    continue
                idx, how = 0, 'button 0'
                names = self.hero_names()
                for i, b in enumerate(btns):
                    lab = CC.sub('', str(getattr(b, 'text', '') or '')).strip().lower()
                    if lab and lab in names:
                        idx, how = i, 'hero button %d' % i
                        break
                self.eng.post_input(self.player, 4, t[3], idx)
                self.log.append((now * 5, 'dialog ' + how))
                self.acq_log.append((now * 5, 'b', 'dialog %s' % how))
                return True
        return False

    # ---------------------------------------------------------------------------------------------------- acquisition
    def stages(self):
        """the acquisition stages of this map in the order (a) already owned, (b) dialogs (always on), then the first of
        pre-chat, (e) selection pick, (f) chooser walk, (c) shop purchase, (d) chat commands that works."""
        out = []
        if self.pre_chat:
            out.append(self.stage_prechat)
        if self.pick_owners and self.pick_mode == 'select':
            out.append(self.stage_select)
        if self.pick_owners and self.pick_mode == 'walk':
            out.append(self.stage_walk)
        out += [self.stage_shop, self.stage_chat]
        return out

    def acquire(self, now):
        """one acquisition action per call; -> True if an input was posted. A stage that has nothing (more) to try returns
        False and the next one takes over."""
        if self.click_dialogs(now):
            return True
        if now < self.next_acq:
            return False
        st = self.stages()
        while self.stage < len(st):
            if st[self.stage](now):
                self.next_acq = now + TICK
                return True
            self.stage += 1
            self.stage_n = 0
        return False

    def stage_prechat(self, now):
        """map mode commands that must come first (CHA: -kc / -kr); each is sent once."""
        cmds = self.N.chat_cmds
        while self.stage_n < len(self.pre_chat):
            s = self.pre_chat[self.stage_n]
            self.stage_n += 1
            if s in cmds:
                self.eng.post_input(self.player, 7, 0, cmds.index(s))
                self.acq_log.append((now * 5, 'd', 'pre-chat %s' % s))
                return True
        return False

    def stage_select(self, now):
        """(e): select a hero of a pick owner twice within the map's 0.3 s window (select, clear, select 3 turns later)."""
        cands = [u for u in self.N.units if u.alive and not u.dead and getattr(u, 'hero', False)
                 and u.owner.id in self.pick_owners]
        if not cands or self.stage_n >= PICK_TRIES:
            return False
        u = cands[self.stage_n % len(cands)]
        self.stage_n += 1
        self.pick_seq = [u.serial, 0]
        self.eng.post_input(self.player, 8, u.serial, 1)
        self.acq_log.append((now * 5, 'e', 'select u%d %s' % (u.serial, u.data.get('id4'))))
        return True

    def stage_walk(self, now):
        """(f) DBZ Hero Arena: the player owns only a mobile chooser unit (Spirit); the map creates the hero when it enters a
        pedestal: walk it onto the pick-owner heroes one after the other (WALK_WAIT each)."""
        ch = [u for u in self.own() if not u.is_bldg and not getattr(u, 'hero', False) and self.eng.can_move(u)]
        cands = [u for u in self.N.units if u.alive and not u.dead and getattr(u, 'hero', False)
                 and u.owner.id in self.pick_owners]
        if not ch or not cands or self.stage_n >= PICK_TRIES:
            return False
        if now < self.walk_until:
            return False
        c = ch[0]
        u = sorted(cands, key=lambda v: ((v.x - c.x) ** 2 + (v.y - c.y) ** 2, v.serial))[self.stage_n % len(cands)]
        self.stage_n += 1
        self.walk_until = now + WALK_WAIT
        self.eng.post_input(self.player, 1, c.serial, ORDER_IDS['move'], u.x, u.y)
        self.acq_log.append((now * 5, 'f', 'walk u%d to hero u%d %s' % (c.serial, u.serial, u.data.get('id4'))))
        return True

    def stage_shop(self, now):
        return self.buy_hero(now)

    def stage_chat(self, now):
        return self.chat_pick(now)

    def pick_step(self, now, turn):
        """the follow-up inputs of a pick sequence (clear after 1 turn, select again after 3)."""
        if self.pick_seq is None:
            return False
        sr, k = self.pick_seq
        self.pick_seq[1] = k + 1
        if k == 0:
            self.eng.post_input(self.player, 8, 0, 0)
            return True
        if k == self.pick_gap:
            self.eng.post_input(self.player, 8, sr, 1)
            self.pick_seq = None
            return True
        return False

    def buy_hero(self, now):
        """(c): a hero type in some shop's Sellunits, bought through input kind 5 (shops.buy: gold, stock, and an own unit in
        the Aneu reach of the shop - the buyer is walked next to the shop first)."""
        names = self.hero_names()
        tids = set(names.values())
        own = [u for u in self.own() if not getattr(u, 'hero', False) and not u.is_bldg]
        pl = self.N.players[self.player]
        gold = pl.state.get(1, 0)
        for u in self.N.units:
            if not u.alive or u.dead or not u.data.get('Sellunits'):
                continue
            for s in str(u.data.get('Sellunits')).split(','):
                s = s.strip()
                if len(s) != 4 or jass.rawcode(s) not in tids:
                    continue
                tid = jass.rawcode(s)
                d = self.N.unit_types[tid]
                key = (u.serial, tid)
                if self.shop_tries.get(key, 0) >= 3 or int(d.get('gold', 0) or 0) > gold:
                    continue
                near = sorted(own, key=lambda b: ((b.x - u.x) ** 2 + (b.y - u.y) ** 2, b.serial))
                buyer = near[0] if near else None
                if buyer is not None and (buyer.x - u.x) ** 2 + (buyer.y - u.y) ** 2 > SHOP_REACH ** 2 and \
                        self.shop_walks < 12 and self.eng.can_move(buyer):
                    self.shop_walks += 1
                    self.eng.post_input(self.player, 1, buyer.serial, ORDER_IDS['move'], u.x, u.y)
                    self.acq_log.append((now * 5, 'c', 'walk buyer u%d to shop u%d' % (buyer.serial, u.serial)))
                    return True
                self.shop_tries[key] = self.shop_tries.get(key, 0) + 1
                self.eng.post_input(self.player, 5, u.serial, tid, 0.0, 0.0, buyer.serial if buyer is not None else 0)
                self.acq_log.append((now * 5, 'c', 'buy %s at shop u%d' % (s, u.serial)))
                return True
        return False

    def chain_buy(self, h, now):
        """A1 (CHA, NOTES_items 7.8.1): the map teleports the hero on every pickup; the next shop can be farther than the
        shop reach. Per live hero: the next class of item_chain is sought in the shops' item lists; the hero walks to a
        point SHOP_REACH from that shop (a unit that is still moving is not accepted while it crosses the ring), stops
        there, then the buy is posted (input kind 5 with the hero as buyer). The class is done when the hero picked up
        one more item. -> True if an input was posted."""
        from .shops import K_ASEI
        n = min(self.cnt.get(('PICKUP_ITEM', h.serial), 0), len(self.item_chain))
        if n >= len(self.item_chain) or h.cast is not None:
            return False
        if n != self.chain_next:
            self.chain_next, self.chain_tries = n, 0
        want = self.item_chain[n]
        gold = self.N.players[self.player].state.get(1, 0)
        eng = self.eng
        best = None
        for sh in self.N.units:
            if not sh.alive or sh.dead or not getattr(sh, 'shop', None):
                continue
            for sl in sh.shop.get(K_ASEI) or ():
                if sl is None or sl.id == 0:
                    continue
                sl.sync(eng.vm.steps)
                d = eng.item_types.get(sl.id) or {}
                if int(d.get('cls', -1)) != want or sl.cur < 1 or int(d.get('goldcost', 0) or 0) > gold:
                    continue
                dist2 = (sh.x - h.x) ** 2 + (sh.y - h.y) ** 2
                if best is None or (dist2, sh.serial, sl.id) < best[0]:
                    best = ((dist2, sh.serial, sl.id), sh, sl.id)
        if best is None:
            return False
        _k, sh, tid = best
        dist = ((sh.x - h.x) ** 2 + (sh.y - h.y) ** 2) ** 0.5
        if dist > SHOP_REACH:
            if not eng.can_move(h) or (h.order != 0 and now < self.chain_walk_until):
                return False
            f = (dist - SHOP_REACH + 40.0) / dist          # a point just inside SHOP_REACH on the way to the shop
            self.chain_walk_until = now + 4 * SPS
            eng.post_input(self.player, 1, h.serial, ORDER_IDS['move'], h.x + (sh.x - h.x) * f, h.y + (sh.y - h.y) * f)
            self.acq_log.append((now * 5, 'chain', 'walk hero u%d to shop u%d (%.0f)' % (h.serial, sh.serial, dist)))
            return True
        if h.order != 0 or self.chain_tries >= 8:          # stand still first
            return False
        self.chain_tries += 1
        eng.post_input(self.player, 5, sh.serial, tid, 0.0, 0.0, h.serial)
        self.acq_log.append((now * 5, 'chain', 'buy %s at shop u%d (%.0f)' % (eng.item_types[tid].get('id4'), sh.serial, dist)))
        return True

    def chat_pick(self, now):
        """(d): the documented chat commands, one per try, in order."""
        cmds = self.N.chat_cmds
        while self.chat_next < len(self.chat):
            s = self.chat[self.chat_next]
            self.chat_next += 1
            if s in cmds:
                self.eng.post_input(self.player, 7, 0, cmds.index(s))
                self.acq_log.append((now * 5, 'd', 'chat %s' % s))
                return True
        return False

    # ---------------------------------------------------------------------------------------------------- control
    def enemies_of(self, h):
        eng = self.eng
        for v in self.N.units:
            if not v.alive or v.dead or v.owner.id == self.player or v.owner.id == 15 or getattr(v, 'hidden', False):
                continue
            if v.invulnerable or getattr(v, 'locust', False) or eng.ally(h.owner, v.owner):
                continue
            yield v

    def supported(self, h, v):
        """the enemy is close to the hero, or an allied non-building unit stands within self.support of it."""
        if (v.x - h.x) ** 2 + (v.y - h.y) ** 2 <= 600.0 ** 2:
            return True
        r2 = self.support ** 2
        for a in self.N.units:
            if a is h or not a.alive or a.dead or a.is_bldg or getattr(a, 'locust', False) or a.owner.id == 15:
                continue
            if (a.x - v.x) ** 2 + (a.y - v.y) ** 2 <= r2 and (a.owner.id == self.player or self.eng.ally(h.owner, a.owner)):
                return True
        return False

    def nearest_enemy(self, h):
        best = None
        hm = self.home.get(h.serial)
        for v in self.enemies_of(h):
            if self.blocked.get((int(v.x) // 256, int(v.y) // 256), 0) > self.eng.vm.steps:
                continue
            if self.roam is not None and hm is not None and (v.x - hm[0]) ** 2 + (v.y - hm[1]) ** 2 > self.roam ** 2:
                continue
            if self.eng.pick_weapon(h, v, explicit=True) < 0:
                continue
            if self.support is not None and not self.supported(h, v):
                continue
            d2 = (v.x - h.x) ** 2 + (v.y - h.y) ** 2
            if best is None or (d2, v.serial) < best[0]:
                best = ((d2, v.serial), v)
        return best[1] if best else None

    def use_item(self, h, now):
        """one item use per ITEM_EVERY: a usable item with a spell ability, targeted like hero_cast (nearest enemy)."""
        from .spells import target_kind, T_UNIT, T_POINT, USE_SLOT0
        sp = self.eng.sp
        if now < self.item_next.get(h.serial, 0):
            return False
        for slot in range(6):
            r = sp.item_spell(h, slot)
            if r is None:
                continue
            aid, code, lv, it = r
            if h.mana < lv['Cost'] or sp.cooling(h, sp.cool_key(aid, it)) or (not it.uncharged and it.charges == 0):
                continue
            if (code == 'AIhe' and h.life >= h.maxlife) or (code == 'AIma' and h.mana >= h.maxmana) or \
                    (code == 'AIre' and h.life >= h.maxlife and h.mana >= h.maxmana):
                continue                   # the engine refuses a heal / mana potion at full life / mana (0x19 / 0x1a)
            kind = target_kind(code, lv)
            oid = USE_SLOT0 + slot
            if kind == T_UNIT:
                v = self.nearest_enemy(h)
                if v is None or (v.x - h.x) ** 2 + (v.y - h.y) ** 2 > CAST_REACH ** 2 or \
                        not self.eng.ab.targ_ok(h, v, lv['targs']):
                    continue
                self.eng.post_input(self.player, 2, h.serial, oid, 0.0, 0.0, v.serial)
            elif kind == T_POINT:
                v = self.nearest_enemy(h)
                if v is None or (v.x - h.x) ** 2 + (v.y - h.y) ** 2 > CAST_REACH ** 2:
                    continue
                self.eng.post_input(self.player, 1, h.serial, oid, v.x, v.y)
            else:
                self.eng.post_input(self.player, 0, h.serial, oid)
            self.item_next[h.serial] = now + ITEM_EVERY
            self.log.append((now * 5, 'item slot %d %s' % (slot, code)))
            return True
        return False

    def control(self, h, now):
        if h.cast is not None:
            return False
        sr = h.serial
        self.home.setdefault(sr, (h.x, h.y))
        frac = h.life / h.maxlife if h.maxlife > 0 else 1.0
        if sr in self.retreat and frac >= RESUME_LIFE:
            self.retreat.discard(sr)
        if frac < RETREAT_LIFE:
            self.retreat.add(sr)
        if sr in self.retreat:
            hx, hy = self.home[sr]
            if (h.x - hx) ** 2 + (h.y - hy) ** 2 > 200.0 ** 2 and self.eng.can_move(h):
                self.eng.post_input(self.player, 1, sr, ORDER_IDS['move'], hx, hy)
                self.log.append((now * 5, 'retreat'))
                return True
            return False
        if self.hero_learn(h, now):
            return True
        if self.use_item(h, now):
            return True
        if self.hero_cast(h, now, reach=CAST_REACH, instant=True, gap=NOTARGET_GAP, friendly=True):
            return True
        if self.eng.can_move(h):
            if self.commit and h.order != 0:           # A1 (CHA): keep walking to the chosen enemy (the nearest one flips behind walls)
                lg = self.lastgo.get(sr)
                if lg is not None and now - lg[0] <= COMMIT_FOR:
                    return False
            lg = self.lastgo.pop(sr, None)
            if lg is not None and now - lg[0] <= 2 * TICK and (h.x - lg[1]) ** 2 + (h.y - lg[2]) ** 2 < 32.0 ** 2 and \
                    (lg[3] - h.x) ** 2 + (lg[4] - h.y) ** 2 > 400.0 ** 2:
                self.blocked = {k: t for k, t in self.blocked.items() if t > now}     # prune expired entries
                self.blocked[lg[5]] = now + BLOCK_FOR       # [inf] no path (the order ended where it began): try another enemy
            v = self.nearest_enemy(h)
            if v is not None:
                self.lastgo[sr] = (now, h.x, h.y, v.x, v.y, (int(v.x) // 256, int(v.y) // 256))
                self.eng.post_input(self.player, 1, sr, ORDER_IDS['attack'], v.x, v.y)
                self.log.append((now * 5, 'attack-move'))
                return True
            hx, hy = self.home[sr]
            if self.roam is not None and (h.x - hx) ** 2 + (h.y - hy) ** 2 > 300.0 ** 2 and h.order == 0:
                self.eng.post_input(self.player, 1, sr, ORDER_IDS['attack'], hx, hy)     # back to the guard post
                self.log.append((now * 5, 'back to post'))
                return True
        return False

    def on_turn(self, turn):
        now = self.eng.vm.steps
        if self.pick_step(now, turn):
            return
        hs = self.live_heroes()
        if hs and self.first_hero is None:
            self.first_hero = now
            self.acq_log.append((now * 5, 'a' if now <= 1 else 'first', 'p0 owns %d hero(es): %s' % (
                len(hs), ','.join(str(u.data.get('id4')) for u in hs[:4]))))
        if not hs:
            if not self.own_heroes():              # a dead hero is revived by the map (or not): no new pick for it
                self.acquire(now)
            return
        if self.click_dialogs(now):
            return
        for h in hs:
            if now >= self.hnext.get(h.serial, 0):
                self.hnext[h.serial] = now + TICK
                if self.item_chain and self.chain_buy(h, now):
                    return
                if self.control(h, now):
                    return                                     # one input per turn (stable, readable order)


# ============================================================================================ report + asserts
def hero_report(rep):
    """-> dict of the W9R2-5 report fields for the finished play() result (also stored in rep['hero'])."""
    nat = rep['nat']
    ap = nat.autoplayer
    cnt = getattr(nat, 'hero_cnt', None) or {}
    obs = getattr(nat, 'hero_obs', None) or {'first': None, 'revives': 0}
    pid = getattr(ap, 'player', 0)
    own = [u for u in nat.units if u.owner.id == pid and getattr(u, 'hero', False) and not getattr(u, 'illusion', False)
           and nat.eng.can_move(u)]
    live = [u for u in own if u.alive and not u.dead]
    byh = {}
    for (suf, sr), n in cnt.items():
        if suf == 'SPELL_EFFECT':
            byh[sr] = byh.get(sr, 0) + n
    rep['hero'] = h = {
        'hero_count': len(live),
        'hero_owned': len(own),                       # info only (dead heroes included); the assert uses last_live_s
        'last_live_s': (obs['last_live'] * 5 / 1000.0) if obs.get('last_live') is not None else None,
        'hero_levels': sorted((u.hlevel for u in own), reverse=True),
        'spell_effect_events_by_hero': byh,
        'spell_effects': sum(byh.values()),
        'kills_by_player0': rep['kills'].get(pid, 0),
        'deaths_own_heroes': sum(n for (s_, _), n in cnt.items() if s_ == 'DEATH'),
        'revives': obs['revives'],
        'first_hero_s': (obs['first'] * 5 / 1000.0) if obs['first'] is not None else None,
        'hero_level_events': sum(n for (s, _), n in cnt.items() if s in ('HERO_LEVEL', 'LEVEL')),
        'hero_skill_events': sum(n for (s, _), n in cnt.items() if s in ('HERO_SKILL', 'SKILL')),
        'use_item_events': sum(n for (s, _), n in cnt.items() if s == 'USE_ITEM'),
        'acq': list(getattr(ap, 'acq_log', []))[:6],
    }
    return h


def asserts_factory(floors):
    """asserts(rep) with the floors of the map (taken from the first accepted run, never lowered)."""
    def asserts(rep):
        h = hero_report(rep)
        det = 'heroes %d (owned %d, last alive %s s) levels %s first %s s acq %s' % (h['hero_count'], h['hero_owned'], h['last_live_s'], h['hero_levels'][:4], h['first_hero_s'], h['acq'][:2])
        rej = rep['rejected'] / max(1, rep['inputs'])
        return [
            ('hero owned by 60 s', h['first_hero_s'] is not None and h['first_hero_s'] <= 60.0 and h['last_live_s'] is not None and h['last_live_s'] >= 60.0, det),
            ('inputs rejected <= 20 %', rej <= 0.20, '%d of %d (%.0f %%)' % (rep['rejected'], rep['inputs'], 100 * rej)),
            ('spell effects >= %d' % floors['spells'], h['spell_effects'] >= floors['spells'],
             '%d by hero %s' % (h['spell_effects'], h['spell_effect_events_by_hero'])),
            ('hero level >= 2', bool(h['hero_levels']) and h['hero_levels'][0] >= 2 and h['hero_level_events'] >= 1,
             'levels %s, HERO_LEVEL events %d, skills %d, item uses %d' % (h['hero_levels'][:4], h['hero_level_events'],
                                                                           h['hero_skill_events'], h['use_item_events'])),
            ('kills_by_player[0] >= %d' % floors['kills'], h['kills_by_player0'] >= floors['kills'],
             '%d (deaths of own heroes %d, revives %d)' % (h['kills_by_player0'], h['deaths_own_heroes'], h['revives'])),
        ]
    return asserts


def make(**kw):
    return lambda N: HeroPlay(N, **kw)


PROFILES = {}
for _key, _kw, _floors, _extra in (
        ('fan_9330BossBattle', {}, {'spells': 5, 'kills': 5}, {}),
        # pick_gap 12: the map's double-select window (O58688: TriggerSleepAction(0.03), enable, TriggerSleepAction(0.30), disable) opens
        # d + 0.1 .. 0.2 s later on the real 1.26a (docs/oracle.md TriggerSleepAction: 0.03 -> ~0.2 s, 0.3 -> ~0.5 s), so a second select
        # 75 ms after the first (the old 3-turn gap) comes before the window; 12 turns (0.3 s) is inside it under the grid rule (opens <= 0.23 s, closes >= 0.5 s) and the old one (0.055 .. 0.38 s)
        ('fan_2637XHeroSiege312', {'pick_owners': (8,), 'chat': ('-random',), 'roam': 2000.0, 'pick_gap': 12}, {'spells': 5, 'kills': 5}, {}),
        ('fan_10145DbzHeroArena44', {'pick_owners': (15,), 'pick_mode': 'walk'}, {'spells': 5, 'kills': 5}, {}),
        ('fan_2043DOTA37', {'player': 1, 'pick_owners': (15,), 'pick_mode': 'walk', 'chat': ('-random',), 'support': 1500.0}, {'spells': 5, 'kills': 5}, {}),
        ('fan_7073CustomHeroArena15cprotected', {'pre_chat': ('-kc',), 'item_chain': (4, 1, 5, 3), 'commit': True}, {'spells': 5, 'kills': 5},
         {'xfail': {'spell effects >= 5': 'W9R3-4-CHA', 'hero level >= 2': 'W9R3-4-CHA', 'kills_by_player[0] >= 5': 'W9R3-4-CHA'}}),
        ('fan_9486SuperDBZAllSagasV282', {}, {'spells': 5, 'kills': 5},
         {'xfail': {'kills_by_player[0] >= 5': 'W9R2-5-SDBZ'}}),
        ('fan_6161HeroPlanWars123b', {'pick_owners': (15,)}, {'spells': 5, 'kills': 5},
         {'xfail': {'hero owned by 60 s': 'W9R2-5-HPW', 'spell effects >= 5': 'W9R2-5-HPW', 'hero level >= 2': 'W9R2-5-HPW',
                    'kills_by_player[0] >= 5': 'W9R2-5-HPW'}})):
    PROFILES[_key] = dict({'key': _key, 'humans': 2 if _kw.get('player') else 1, 'seconds': 300, 'make': make(**_kw),
                           'hero_kw': _kw, 'asserts': asserts_factory(_floors), 'allowed_errors': '', 'xfail': {},
                           'floors': _floors}, **_extra)
