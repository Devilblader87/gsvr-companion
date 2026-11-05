"""W8 heroes for the reference engine (docs/NOTES_heroes.md §1–§3; clean-room: behaviour only).

Hero = unit type id with first character 'A'…'Z' (NOTES_heroes §1.1: the AHer state). Per hero on the Unit:
hlevel, xp (int, cumulative), skill (points), hslots (the heroAbilList ids, max 5), xp_suspended, and the attribute
effects last applied (h_str / h_agi / h_int / h_prim, the applied deltas). The engine calls:
  init_unit     init(u)              level 1, XP 0, 1 skill point (capped), attributes applied (spawns full)
  on_death      on_kill(victim, k)   after the bounty, before EVENT_UNIT_DEATH (§1.2)
  issue         learn(u, oid)        order id = a hero slot's ability id (§3)
Assumptions A-HE* are listed in docs/engine_w5.md (W8 section).
"""
import math

from .vm import r32
from . import randdata as RD

ALLIANCE_PASSIVE = 0
ALLIANCE_SHARED_XP = 3


def _f(misc, key, default):
    try:
        return float(str(misc.get(key, default)).split(',')[0])
    except (TypeError, ValueError):
        return float(default)


def _list(misc, key):
    out = []
    for p in str(misc.get(key, '') or '').split(','):
        p = p.strip()
        if p:
            try:
                out.append(float(p))
            except ValueError:
                pass
    return out


def _table(misc, key, n, x0):
    """list values first, then f(x) = A f(x-1) + B x + C with the running value as float, entries truncated (§1.1);
    x of entry i = i + x0."""
    vals = _list(misc, key)
    A, B, C = (_f(misc, key + 'Formula' + k, 0) for k in 'ABC')
    out = []
    f = 0.0
    for i in range(n):
        if i < len(vals):
            f = vals[i]
        else:
            f = A * f + B * (i + x0) + C
        out.append(int(f))
    return out


def _rc(s):
    return (ord(s[0]) << 24) | (ord(s[1]) << 16) | (ord(s[2]) << 8) | ord(s[3])


AUFA = _rc('AUfa')      # the order id that learns a slot holding AUfu (§4.3 [obs 0x6f4d9070])
AUFU = _rc('AUfu')


class Heroes:
    def __init__(self, eng):
        self.eng = eng
        self.N = eng.N
        m = eng.misc
        self.max_level = int(_f(m, 'MaxHeroLevel', 10))
        self.max_unit_level = int(_f(m, 'MaxUnitLevel', 20))
        need = _table(m, 'NeedHeroXP', self.max_level, 2)
        if len(need) >= 2:
            need[-1] = need[-2]                         # max-level sentinel [obs]
        self.need = need                                # need[i] = XP to reach level i + 2
        self.grant_hero = _table(m, 'GrantHeroXP', self.max_level, 1)
        self.grant_normal = _table(m, 'GrantNormalXP', self.max_unit_level, 1)
        fac = [v / 100.0 for v in _list(m, 'HeroFactorXP')]
        self.factor = [(fac[i] if i < len(fac) else (fac[-1] if fac else 0.0)) for i in range(self.max_level)]
        self.summoned_factor = _f(m, 'SummonedKillFactor', 0.5)
        self.range = _f(m, 'HeroExpRange', 1200.0)
        self.global_xp = _f(m, 'GlobalExperience', 1) != 0
        self.drain = _f(m, 'MaxLevelHeroesDrainExp', 1) != 0
        self.building_xp = _f(m, 'BuildingKillsGiveExp', 0) != 0
        self.str_dmg = _f(m, 'StrAttackBonus', 1.0)
        self.str_hp = _f(m, 'StrHitPointBonus', 25.0)
        self.str_regen = _f(m, 'StrRegenBonus', 0.05)
        self.agi_def = _f(m, 'AgiDefenseBonus', 0.3)
        self.agi_base = _f(m, 'AgiDefenseBase', -2.0)
        self.agi_speed = _f(m, 'AgiAttackSpeedBonus', 0.02)
        self.int_mana = _f(m, 'IntManaBonus', 15.0)
        self.int_regen = _f(m, 'IntRegenBonus', 0.05)
        self.skip = int(_f(m, 'HeroAbilityLevelSkip', 2))
        self.heroes = []                                # living hero units, creation order (candidate walk)
        self.name_ctr = {}                              # W9R2-1: (owner id, type id) -> heroes created (Propernames entry)

    # ------------------------------------------------------------ state
    @staticmethod
    def is_hero(u):
        """type id 'A'…'Z' (the game's hero test [obs]) with hero data: A-HE5 also requires a Primary attribute
        (every hero base type has one; synthetic test types 'X…' do not)."""
        c = (u.tid >> 24) & 0xff
        return 65 <= c <= 90 and int(u.data.get('Primary', 0) or 0) != 0

    def init(self, u):
        u.hero = self.is_hero(u)
        if not u.hero:
            return
        d = u.data
        u.pname = RD.assign_name(self.name_ctr, getattr(u.owner, 'id', -1), u.tid, d)    # W9R2-1
        u.hlevel = 1
        u.xp = 0
        u.xp_suspended = False
        u.hslots = [_rc(a.strip()) for a in str(d.get('heroAbilList', '') or '').split(',') if len(a.strip()) == 4][:5]
        # §4.2 0x6f4d6a40: levels / reqLevel are copied per slot at creation; learn / strip / capacity read the copies
        u.hslot_lv = []
        u.hslot_req = []
        for aid in u.hslots:
            r = self.eng.ab.rec(aid)
            u.hslot_lv.append(int(r['levels']) if r is not None else 0)
            u.hslot_req.append(int(r.get('reqLevel', 1) or 0) if r is not None else 0)
        u.skill = 1
        u.h_bonus = {}                  # temporary item / ability attribute bonus
        u.h_bonus_perm = {}             # permanent tome / permanent-source attribute bonus
        u.h_str = u.h_agi = u.h_int = 0
        u.h_dmg = 0
        u.h_hp = 0
        u.h_mana = 0
        u.h_regen = 0.0
        u.h_mregen = 0.0
        u.h_armor = 0.0
        u.h_speed = 0.0
        self.cap_skill(u)
        self.apply_attributes(u)
        self.heroes.append(u)

    def attr(self, u, k):
        """shown value: base + trunc((level - 1) × plus) (§2.2) + item / tome bonuses (W8, NOTES_items §5)."""
        d = u.data
        return (int(float(d.get(k, 0) or 0)) + int((u.hlevel - 1) * float(d.get(k + 'plus', 0) or 0)) +
                u.h_bonus.get(k, 0) + u.h_bonus_perm.get(k, 0))

    def attr_no_bonus(self, u, k):
        """the value GetHeroStr(h, false) answers: level growth + permanent sources, without items / abilities."""
        d = u.data
        return (int(float(d.get(k, 0) or 0)) + int((u.hlevel - 1) * float(d.get(k + 'plus', 0) or 0)) +
                u.h_bonus_perm.get(k, 0))

    def add_bonus(self, u, s, a, i, permanent=False):
        """tomes (permanent) and attribute items (while carried) add to the attributes (A-HE4 lifted)."""
        bonus = u.h_bonus_perm if permanent else u.h_bonus
        for k, v in (('STR', s), ('AGI', a), ('INT', i)):
            if v:
                bonus[k] = bonus.get(k, 0) + v
        self.apply_attributes(u)

    def primary(self, u):
        p = int(u.data.get('Primary', 0) or 0)
        return self.attr(u, 'STR' if p == 1 else ('INT' if p == 3 else 'AGI'))     # A-HE1: no Primary → AGI

    def apply_attributes(self, u):
        """§2.3: every effect re-applied as the delta to the last applied value."""
        eng = self.eng
        s, a, i = self.attr(u, 'STR'), self.attr(u, 'AGI'), self.attr(u, 'INT')
        dmg = int(self.primary(u) * self.str_dmg)
        u.h_dmg = dmg                                    # integer bonus on all weapons (engine roll)
        hp = int(math.floor(s * self.str_hp * u.owner.handicap + 0.5))
        dhp = hp - u.h_hp
        if dhp:
            u.h_hp = hp
            life = u.life
            u.maxlife = r32(u.maxlife + dhp)
            nl = r32(life + dhp)                         # mode 2: max and current (A-HE2 also for a loss)
            if dhp < 0:
                # [obs runtime 1.26a] docs/oracle.md "hero STR loss at low life": the life floors at 1; a max life driven to
                # (or below) the unit death threshold (STR -5: max life -25) takes the life with it and the hero dies
                if nl < 1.0:
                    nl = 1.0
                if nl > u.maxlife:
                    nl = u.maxlife
            u.life = nl
            if dhp < 0 and u.alive and not u.dead:
                from .engine import UNIT_DEATH_LIFE            # (engine imports this module)
                if nl <= UNIT_DEATH_LIFE:
                    eng.N.kill_unit(u, None)
        mana = int(math.floor(i * self.int_mana + 0.5))
        dmn = mana - u.h_mana
        if dmn:
            u.h_mana = mana
            m = u.mana
            u.maxmana = r32(u.maxmana + dmn)
            u.mana = r32(m + dmn)
        arm = a * self.agi_def + self.agi_base
        u.armor = u.armor - u.h_armor + arm
        u.h_armor = arm
        sp = a * self.agi_speed
        if sp != u.h_speed:
            d = sp - u.h_speed
            u.h_speed = sp
            eng.set_attack_factor(u, u.atk_delta + d)
        rg, mrg = s * self.str_regen, i * self.int_regen
        if rg != u.h_regen or mrg != u.h_mregen:
            u.h_regen, u.h_mregen = rg, mrg
            eng.set_regen(u)
        u.h_str, u.h_agi, u.h_int = s, a, i

    def learnable(self, u):
        n = 0
        for i, aid in enumerate(u.hslots):
            n += max(0, u.hslot_lv[i] - u.abilities.get(aid, 0))
        return n

    def cap_skill(self, u):
        n = self.learnable(u)
        if u.skill > n:
            u.skill = n

    # ------------------------------------------------------------ XP (§1.2, §1.3)
    def need_for(self, level):
        """cumulative XP to reach `level` (2 … max)."""
        if level <= 1:
            return 0
        return self.need[min(level - 2, len(self.need) - 1)]

    def base_xp(self, v):
        if getattr(v, 'hero', False):
            L = v.hlevel
            if L <= 0:
                return 0
            L = min(L, self.max_level)
            x = float(self.grant_hero[L - 1])
        else:
            L = int(v.data.get('level', 0) or 0)
            if L <= 0:
                return 0
            L = min(L, self.max_unit_level)
            x = float(self.grant_normal[L - 1])
        if getattr(v, 'summoned', False):
            x *= self.summoned_factor
        return int(x)

    def on_kill(self, v, killer):
        if killer is None or not self.heroes:
            return
        eng = self.eng
        kp, vp = killer.owner, v.owner
        if eng.ally(kp, vp):
            return                                       # PASSIVE toward the victim's owner (step 1)
        if killer.is_bldg and not self.building_xp:
            return                                       # step 5
        rng2 = self.range * self.range
        near, every = [], []
        for h in self.heroes:
            eng.wk += 1
            if not h.alive or h.dead:
                continue
            if h.hlevel >= self.max_level and not self.drain:
                continue
            if not (h.owner is kp or kp.alliance.get((h.owner.hid, ALLIANCE_SHARED_XP), False)):
                continue
            if self.global_xp:
                every.append(h)
            dx, dy = h.x - v.x, h.y - v.y
            if dx * dx + dy * dy <= rng2:
                near.append(h)
        lst = near or every
        if not lst:
            return
        base = self.base_xp(v)
        share = float(base) / float(len(lst))
        if share <= 0:
            return
        creep = vp.id >= 12
        for h in lst:
            self.add_xp(h, share, handicap=True, creep=creep)

    def add_xp(self, h, amount, handicap=False, creep=False):
        if amount == 0 or h.xp_suspended:
            return
        if handicap:
            amount *= h.owner.handicap_xp
        lv0 = h.hlevel
        if creep:
            E = float(h.xp)
            lvl = h.hlevel - 1
            while lvl < self.max_level - 1:
                f = self.factor[lvl]
                if f == 0:
                    break
                E2 = E + amount * f
                nd = self.need_for(lvl + 2)
                if E2 > nd:
                    amount = (E2 - nd) / f
                    E = float(nd)
                    lvl += 1
                    continue
                E = E2
                break
            h.xp = int(math.floor(E + 0.5))
        else:
            h.xp += int(math.floor(amount + 0.5))
        while h.hlevel < self.max_level and h.xp >= self.need_for(h.hlevel + 1):
            h.hlevel += 1
            h.skill += 1
        if h.hlevel >= self.max_level:
            mx = self.need_for(self.max_level)
            if h.xp > mx:
                h.xp = mx
        if h.hlevel > lv0:
            self.apply_attributes(h)
            self.cap_skill(h)
            self.N.hero_event(h, 'LEVEL', {'leveling': self.N.H(h)})

    def set_level(self, h, L):
        if L >= 1 and h.hlevel <= L:
            want = self.need_for(max(L, 2))                         # A-HE3: L = 1 → level-2 threshold (Open)
            if want > h.xp:
                self.add_xp(h, float(want - h.xp))

    # ------------------------------------------------------------ learning (§3)
    @staticmethod
    def slot_index(u, aid):
        """§4.3: the slot index for an order id — the order id 'AUfa' addresses a slot holding 'AUfu'."""
        for i, s in enumerate(u.hslots):
            if s == aid:
                return i
            if aid == AUFA and s == AUFU:
                return i
        return None

    @staticmethod
    def slot_id(u, aid):
        """§4.3: the slot's own id for an order id — the order id 'AUfa' addresses a slot holding 'AUfu' [obs 0x6f4d9070]."""
        i = Heroes.slot_index(u, aid)
        return None if i is None else u.hslots[i]

    @staticmethod
    def slot_ok(u, aid):
        return Heroes.slot_id(u, aid) is not None

    def learn(self, u, aid, free=False):
        """-> True if learned. order id = ability id of a hero slot."""
        i = self.slot_index(u, aid) if getattr(u, 'hero', False) else None
        if i is None:
            return False
        aid = u.hslots[i]
        if not free and u.skill <= 0:
            return False
        r = self.eng.ab.rec(aid)
        if r is None:
            return False
        c = u.abilities.get(aid, 0)
        if c >= u.hslot_lv[i]:
            return False
        req = u.hslot_req[i]
        sk = int(r.get('levelSkip', 0) or 0) or self.skip
        if u.hlevel < req + sk * c:
            return False
        if c == 0:
            u.abilities[aid] = 1
            self.eng.ab.on_add(u, aid)
        else:
            from .abilities import ORB_ALL
            ab = self.eng.ab
            b = ab.base(aid)
            old = ab.lv(u, aid)
            u.abilities[aid] = c + 1
            new = ab.lv(u, aid)
            if b == 'Aamk':
                ab.aamk_apply(u, aid)                        # NOTES_items §11.2: the new level's attributes
            elif b == 'Arel' and not u.illusion:
                self.eng.item_lreg_calc(u)                   # the new level's DataA (W9R2-3 review)
            elif b in ORB_ALL and not u.illusion and old is not None and new is not None:
                u.dmg_int += int(new['A']) - int(old['A'])   # an orb skill's attack bonus follows its level
        self.N.hero_event(u, 'SKILL', {'learning': self.N.H(u), 'skill': aid, 'skilllevel': c + 1})
        if not free:
            u.skill -= 1
        return True

    # ------------------------------------------------------------ strip levels (§4.3, UnitStripHeroLevel)
    def _lower_abil(self, u, aid, lvl):
        """one ability to `lvl` (0 = removed): the port stores the level in u.abilities; the per-level side effects
        follow learn() in reverse."""
        from .abilities import ORB_ALL
        ab = self.eng.ab
        oldl = u.abilities.get(aid, 0)
        if lvl <= 0:
            u.abilities.pop(aid, None)
            u.perm_abil.pop(aid, None)
            ab.on_remove_abil(u, aid, oldl)
            return
        b = ab.base(aid)
        old = ab.lv(u, aid)
        u.abilities[aid] = lvl
        new = ab.lv(u, aid)
        if b == 'Aamk':
            ab.aamk_apply(u, aid)
        elif b == 'Arel' and not u.illusion:
            self.eng.item_lreg_calc(u)
        elif b in ORB_ALL and not u.illusion and old is not None and new is not None:
            u.dmg_int += int(new['A']) - int(old['A'])

    def strip_one(self, h):
        """§4.3 0x6f4d82b0: take one hero level back -> True when a level was lost. Abilities whose
        reqLevel + levelSkip × (level − 1) no longer fit lose levels (level 1 no longer fitting = removed); nothing
        adjusted -> one skill point back, else the highest ability loses one level; XP = bottom of the new level."""
        if h.hlevel <= 1:
            h.xp = 0
            return False
        h.hlevel -= 1
        nl = h.hlevel
        ab = self.eng.ab
        adjusted = False
        for i, aid in enumerate(h.hslots):
            cl = h.abilities.get(aid, 0) if aid else 0
            if cl <= 0:
                continue
            r = ab.rec(aid)
            req = h.hslot_req[i]                            # the cached creation copy (§4.2, open question 6)
            sk = (int(r.get('levelSkip', 0) or 0) if r is not None else 0) or self.skip
            j = cl - 1
            while j > 0 and nl < req + sk * j:
                j -= 1
            if j == cl - 1:
                continue                                    # the current level still fits
            if nl < req + sk * j:                            # not even level 1 fits -> remove
                self._lower_abil(h, aid, 0)
            else:
                self._lower_abil(h, aid, j + 1)
            adjusted = True
        if not adjusted:
            if h.skill > 0:
                h.skill -= 1
            else:
                best, bi = 0, None
                for aid in h.hslots:
                    cl = h.abilities.get(aid, 0) if aid else 0
                    if cl > best:                            # ties keep the earlier slot
                        best, bi = cl, aid
                if bi is not None:
                    self._lower_abil(h, bi, best - 1)
        h.xp = self.need_for(h.hlevel)                       # XP = the threshold of the new level (0 at level 1)
        self.apply_attributes(h)
        return True

    def strip_levels(self, h, n):
        """§4.3 0x6f4d84a0: strip up to n levels (never below 1); True when something was stripped."""
        if h.hlevel <= 1 and h.skill <= 0:
            return False
        n = min(int(n), h.hlevel)
        ok = False
        while n > 0:
            if not self.strip_one(h):
                break
            ok = True
            n -= 1
        return ok

    def on_death(self, u):
        pass                                            # heroes stay in the candidate list (dead ones are skipped)

    def on_remove(self, u):
        if u in self.heroes:
            self.heroes.remove(u)
