"""BUGS FF-UPG: research (upgrade) effects on units (docs/NOTES_production.md section 3a; behaviour only, clean-room).

A research has up to 4 effect slots (UpgradeData effect1..4 with base / mod, the w3q fields gef1.. gba1.. gmo1..). The value of
a slot at researched level L is V(L) = base + mod * (L - 1) for L > 0 and 0 for L = 0 [obs 0x6f19bad0]; the game applies the
DIFFERENCE V(new) - V(old) to every unit of the player whose type lists the research in its "Upgrades Used" list
(UnitBalance upgrades, w3u upr) when a level is gained, and applies the full V to a unit that is created later [obs
0x6f1e7c10 -> the effect's slot 31 per unit; inf for the late arrival]. This module keeps the totals per unit and computes them
from the player's tech levels (the sum of the single level steps), so research, creation, a type change and an owner change all
go through `sync`.

Effects implemented (the codes of the 1.21 data that change a unit's numbers or state):
  ratd  attack dice += trunc(delta)            [obs 0x6f19be40 -> 0x6f5572b0 adds to the weapon's dice count, read by 0x6f555c40]
  ratx  attack damage bonus += trunc(delta)    [obs 0x6f19bd60 -> 0x6f557300 adds to the weapon's bonus, base + bonus + dice roll]
  rarm  armor += defUp * level step            [obs 0x6f19bbe0: the type's defUp times the step; inf: the slot's base / mod are ignored]
  rhpx  max life += delta, life fraction kept  [obs 0x6f19c1e0 -> 0x6f1e1bf0 mode 1]
  rhpo  max life += delta * type hit points    [obs 0x6f19c270 scales by the unit's base life, mode 1]
  rmnx  max mana += delta, fraction kept       [obs 0x6f19c450 -> 0x6f1bba10 mode 1]
  rman  max mana += delta * type base mana     [PORT9: CUpgradeMana 0x6f19c3f0 / 0x6f25d210; inf for the +0x88 factor]
  rmnr  mana regeneration += delta / s         [obs 0x6f19c4d0]
  rhpr  life regeneration += delta / s         [obs 0x6f19c300]
  rmvx  move speed += delta                    [inf: flat, the data range is +-400]
  rmov  move factor += delta (percent)         [PORT9: CUpgradeMove 0x6f19c590 / 0x6f254d80; inf for set-vs-add]
  rats  attack speed += delta (a fraction)     [obs 0x6f19bf10 -> 0x6f557620: the attack speed factor sum]
  ratr  attack range += delta                  [obs 0x6f19beb0 -> 0x6f557070, all weapons]
  rsig  unit sight += delta                    [obs 0x6f19c730: the delta goes to the unit's reveal state; fog.py reads it]
  renw  per-unit weapon mask = trunc(V(level)) [obs 0x6f19c120: the low two bits replace the enabled-weapon bits]
  rspi  Aspi tier and its cached DataA amount  [obs 0x6f19c860 / 0x6f532a10: counter +-1 per step, V added to the
                                               holder; PORT14: thorns = level-1 DataA + V, CAbilitySpiked §5.15]
  rlum  lumber carry capacity += trunc(delta)  [obs 0x6f19c350: the harvest ability's lumber capacity (slot 207)]
  rmin  gold carry capacity += trunc(delta)    [obs 0x6f19c520: the harvest ability's gold capacity (slot 206)]
  rent  Aegm gold per cycle += trunc(delta)    [obs 0x6f19c170: the entangled gold mine's per-cycle amount]
  ratc  attack target count += trunc(delta)    [PORT4-Upgrade: CUpgradeAttackTargetCount 0x6f19c030 / 0x6f254960; inf for the consumer]
  rlev  spell level steps (slot code = ability) [PORT4-Upgrade: CUpgradeSpell 0x6f19c780 / 0x6f254f80]
  radl  bounce damage loss += V delta (float)   [PORT8-Upgrade, PORT12: CUpgradeAttackDamageLoss 0x6f19bdd0 / 0x6f254700]
  rmim  magic buffs are stripped when applied  [PORT8-Upgrade: CUpgradeMagicImmunity 0x6f19c3c0 / 0x6f254c10]
  rtma  the player's tech-max record of the slot code (player-wide, on the level transition)
                                               [PORT8-Upgrade: CUpgradeAddTechMaxAllowed 0x6f19bb50 / 0x6f254600]
  rart  armor type: the clamped step counter drives the Arbr ability's armor [PORT11-Upgrade:
                                               CUpgradeArmorType 0x6f19bc50 / 0x6f25d030; inf for the DataA reading]
  raud  the unit's aura amount (cached DataA) += delta [PORT13-Upgrade: CUpgradeAuraData 0x6f19c090 / 0x6f25d140
                                               pushes V(new) - V(old) into the aura's holder #1 (+0x88 = DataA,
                                               CAbilityAura §6.2); inf for the additive sign and the first-aura pick]
Not implemented (listed in BUGS FF-UPG): rasd rasr rauv rrai rroo ruro
(the effect id of the slot is read, the number is ignored).
"""
from .vm import r32

RC = {}


def _rc(s):
    return (ord(s[0]) << 24) | (ord(s[1]) << 16) | (ord(s[2]) << 8) | ord(s[3])


for _n in ('ratd', 'ratx', 'rarm', 'rhpx', 'rhpo', 'rmnx', 'rman', 'rmnr', 'rhpr', 'rmvx', 'rmov', 'rats', 'ratr',
           'rsig', 'renw', 'rspi', 'rlum', 'rmin', 'rent', 'ratc', 'rlev', 'radl', 'rmim', 'rart', 'raud'):
    RC[_n] = _n
IMPLEMENTED = tuple(RC)
FIELDS = ('dice', 'dmg', 'arm', 'hp', 'mp', 'hreg', 'mreg', 'spd', 'mspd', 'as', 'rng', 'sig',
          'lcap', 'gcap', 'mine', 'tgt', 'dloss', 'art', 'aura', 'spike')
ART_CLAMP = 8                                  # Arbr has eight levels; the armor-type counter stops there [obs 0x6f19bc50 / 0x6f25d030]
ASPI = (ord('A') << 24) | (ord('s') << 16) | (ord('p') << 8) | ord('i')


def trunc(x):
    return int(x)


def value(rec, i, lvl):
    """V(lvl) of effect slot i (1..4) of a research record."""
    if lvl <= 0:
        return 0.0
    return float(rec.get('base%d' % i, 0) or 0) + float(rec.get('mod%d' % i, 0) or 0) * (lvl - 1)


def int_steps(rec, i, lvl):
    """the sum of the single level steps truncated one by one (ratd / ratx / capacity / mine effects)."""
    n = 0
    for lv in range(1, lvl + 1):
        n += trunc(value(rec, i, lv) - value(rec, i, lv - 1))
    return n


class UpgradeEffects:
    def __init__(self, eng):
        self.eng = eng
        self.lists = {}                       # unit type id -> [research ids of its Upgrades Used list that have implemented effects]

    def used(self, u):
        """the researches that reach the unit: its type's Upgrades Used list, ids known with at least one implemented effect."""
        k = None if u.own_data else u.tid
        lst = self.lists.get(k) if k is not None else None
        if lst is None:
            lst = []
            for t in str(u.data.get('upgrades', '') or '').split(','):
                t = t.strip()
                if len(t) != 4:
                    continue
                rid = _rc(t)
                rec = self.eng.upgrades.get(rid)
                if rec is None or rid in lst:
                    continue
                if any((rec.get('effect%d' % i) or '') in RC for i in range(1, 5)):
                    lst.append(rid)
            if k is not None:
                self.lists[k] = lst
        return lst

    def totals(self, u):
        """-> {field: total} of every effect of the unit's researches at the owner's levels."""
        t = dict.fromkeys(FIELDS, 0.0)
        t['dice'] = t['dmg'] = t['lcap'] = t['gcap'] = t['mine'] = t['tgt'] = 0
        t['art'] = 0
        t['weap'] = -1
        t['spi'] = -1
        t['mim'] = -1
        t['lev'] = (0, 0)                                     # PORT4: (ability id, steps) of the first rlev slot
        pl = u.owner
        if pl is None:
            return t
        base_hp = float(u.data.get('hp', 0) or 0)
        base_mana = float(u.data.get('mana', 0) or 0)
        defup = float(u.data.get('defUp', 0) or 0)
        for rid in self.used(u):
            lvl = int(pl.tech.get(rid, 0) or 0)
            rec = self.eng.upgrades[rid]
            for i in range(1, 5):
                e = rec.get('effect%d' % i) or ''
                if e not in RC:
                    continue
                if e == 'renw':
                    if lvl > 0:                             # the low two bits are the full weapon mask
                        t['weap'] = trunc(value(rec, i, lvl)) & 3
                    continue
                if e == 'rspi':
                    if lvl > t['spi']:                        # the Aspi counter is the research level
                        t['spi'] = lvl
                    t['spike'] += value(rec, i, lvl)          # PORT14: the cached DataA amount (the thorns damage)
                    continue
                if e == 'rmim':
                    if lvl > t['mim']:                        # the applied level (a strip per newly gained level)
                        t['mim'] = lvl
                    continue
                if e == 'rart':                               # PORT11: the armor-type step counter, clamped per gained level
                    for lv in range(1, lvl + 1):
                        t['art'] += trunc(value(rec, i, lv) - value(rec, i, lv - 1))
                        if t['art'] < 0:
                            t['art'] = 0
                        elif t['art'] > ART_CLAMP:
                            t['art'] = ART_CLAMP - 1          # the game's quirk: an exact 8 stays, 9+ becomes 7
                    continue
                if lvl <= 0:
                    continue
                if e == 'rlev':                               # CUpgradeSpell: the ability named by the slot's code steps up
                    cd = str(rec.get('code%d' % i) or '')
                    if len(cd) == 4:
                        k = _rc(cd)
                        if t['lev'][0] in (0, k):             # one leveled ability per unit
                            t['lev'] = (k, t['lev'][1] + trunc(value(rec, i, lvl)))
                    continue
                if e in ('ratd', 'ratx'):
                    n = int_steps(rec, i, lvl)              # the game truncates every single level step
                    t['dice' if e == 'ratd' else 'dmg'] += n
                elif e == 'rarm':
                    t['arm'] += defup * lvl                    # the slot's base / mod are not read (Footman Frenzy XV writes 1 / 1)
                elif e == 'rhpx':
                    t['hp'] += value(rec, i, lvl)
                elif e == 'rhpo':
                    t['hp'] += value(rec, i, lvl) * base_hp
                elif e == 'rmnx':
                    t['mp'] += value(rec, i, lvl)
                elif e == 'rman':
                    t['mp'] += value(rec, i, lvl) * base_mana   # PORT9: percent of the type's base mana [inf]
                elif e == 'rmnr':
                    t['mreg'] += value(rec, i, lvl)
                elif e == 'rhpr':
                    t['hreg'] += value(rec, i, lvl)
                elif e == 'rmvx':
                    t['spd'] += value(rec, i, lvl)
                elif e == 'rmov':
                    t['mspd'] += value(rec, i, lvl)            # PORT9: the percent move variant [inf set-vs-add]
                elif e == 'rats':
                    t['as'] += value(rec, i, lvl)
                elif e == 'ratr':
                    t['rng'] += value(rec, i, lvl)
                elif e == 'rsig':
                    t['sig'] += value(rec, i, lvl)
                elif e == 'rlum':
                    t['lcap'] += int_steps(rec, i, lvl)
                elif e == 'rmin':
                    t['gcap'] += int_steps(rec, i, lvl)
                elif e == 'rent':
                    t['mine'] += int_steps(rec, i, lvl)
                elif e == 'ratc':
                    t['tgt'] += int_steps(rec, i, lvl)
                elif e == 'radl':
                    t['dloss'] += value(rec, i, lvl)        # PORT8: the per-weapon attack damage-loss values (float, no truncation)
                elif e == 'raud':
                    t['aura'] += value(rec, i, lvl)         # PORT13: the aura's cached DataA amount [inf additive]
        return t

    def sync(self, u):
        """bring the unit's upgrade numbers to the totals of its owner's levels (deltas for life / mana keep the fraction)."""
        eng = self.eng
        if not u.upg_any and not self.used(u):
            return
        t = self.totals(u)
        u.upg_any = True
        u.upg_dice = int(t['dice'])
        u.upg_dmg = int(t['dmg'])
        u.upg_rng = t['rng']
        u.upg_spd = t['spd']
        d = t['mspd'] - u.upg_mspd                    # PORT9 rmov: the percent move variant joins the move factor
        if d:
            u.move_delta += d
            u.upg_mspd = t['mspd']
        d = t['hp'] - u.upg_hp
        if d:
            old = u.maxlife
            frac = u.life / old if old > 0 else 1.0
            u.maxlife = r32(max(1.0, old + d))
            nl = r32(frac * u.maxlife)
            if u.alive and not u.dead and nl < 1.0:
                nl = 1.0
            u.life = nl
            u.upg_hp = t['hp']
        d = t['mp'] - u.upg_mp
        if d:
            eng.add_max_mana(u, d)
            u.upg_mp = t['mp']
        if t['arm'] != u.upg_arm:
            u.armor_bonus += t['arm'] - u.upg_arm
            u.upg_arm = t['arm']
        if t['as'] != u.upg_as:
            old = eng.attack_factor(u)
            u.upg_as = t['as']
            eng.refactor_attack(u, old)
        if t['hreg'] != u.upg_hreg or t['mreg'] != u.upg_mreg:
            u.upg_hreg = t['hreg']
            u.upg_mreg = t['mreg']
            eng.set_regen(u)
        u.upg_sight = t['sig']
        u.upg_aura = t['aura']                              # PORT13 raud: the aura pass adds it to the cached DataA
        u.upg_lcap = int(t['lcap'])
        u.upg_gcap = int(t['gcap'])
        u.upg_mine = int(t['mine'])
        u.upg_tgt = int(t['tgt'])
        u.upg_dloss = float(t['dloss'])                     # PORT8 radl (CUpgradeAttackDamageLoss, a float delta)
        if int(t['mim']) > u.upg_mim and int(t['mim']) > 0:  # PORT8 rmim: a newly gained level strips the magic buffs
            u.upg_mim = int(t['mim'])
            self.eng.ab.at(self.eng.now(), 'mim', u, None)   # deferred: the strip may morph a polymorphed unit back
        elif int(t['mim']) < u.upg_mim:
            u.upg_mim = int(t['mim'])
        w = int(t['weap'])
        if w >= 0:
            if not u.upg_weap_on or u.upg_weap != w:
                u.upg_weap = w
                u.upg_weap_on = True
                eng.refresh_fighter(u)
        elif u.upg_weap_on:                                 # the effect left: its remove branch enables the complement
            u.upg_weap = 3
            u.upg_weap_on = False
            eng.refresh_fighter(u)
        self.sync_art(u, int(t['art']))                     # PORT11 rart: the clamped counter drives Arbr
        if int(t['spi']) >= 0:
            self.sync_spikes(u, int(t['spi']), float(t['spike']))
        elif getattr(u, 'upg_spike', 0) or getattr(u, 'upg_spike_amt', 0.0):
            self.sync_spikes(u, 0, 0.0)                     # PORT14: the effect left (owner / type change)
        if t['lev'][0]:
            self.sync_levels(u, t['lev'][0], t['lev'][1])

    def sync_levels(self, u, code, steps):
        """rlev [PORT4-Upgrade, CUpgradeSpell 0x6f19c780 / 0x6f254f80]: the applicator reaches the requested level by single
        up / down steps of the ability named by the slot's code; a request equal to the neutral value (research level 0,
        V = 0) applies nothing. A unit without the ability does nothing at all. The steps applied are kept, so a change of
        the research level moves the CURRENT ability level by the difference (a script that set the level itself is respected).
        One leveled ability per unit (the first rlev slot of its researches; the Udon twin keeps one pair per unit)."""
        if code == 0 or code not in u.abilities:
            return
        prev = u.upg_lev_steps if u.upg_lev_code == code else 0
        if steps == prev:
            return
        r = self.eng.ab.defs.get(code)
        top = len(r['lv']) if r and r['lv'] else 1
        new = max(1, min(top, u.abilities[code] + steps - prev))
        u.upg_lev_code = code
        u.upg_lev_steps = steps
        if new != u.abilities[code]:
            self.eng.ab.level_change(u, code, new)

    def sync_art(self, u, art):
        """rart [PORT11-Upgrade, CUpgradeArmorType 0x6f19bc50 / 0x6f25d030]: the unit's armor-type step counter (already
        clamped 0..8 per gained level) is pushed into its Arbr ability. The ability's current level follows the
        counter (a counter of 0 applies nothing, as in the game's receiver) and its DataA is the armor amount
        [inf: the chapter calls Arbr the armor-bonus ability and reuses the AIde DataA machinery]. No Arbr, no
        armor; the counter still exists on the unit."""
        code = None
        for c, b, _lv in self.eng.ab.abil_list(u):
            if b == 'Arbr':
                code = c
                break
        arm = 0.0
        if code is not None and art > 0:
            lv = self.eng.ab.lv_at(code, art)
            if lv is not None:
                arm = float(lv.get('A', 0) or 0)
            d = self.eng.ab.defs.get(code)
            top = len(d['lv']) if d and d.get('lv') else 1
            nw = max(1, min(top, art))
            if int(u.abilities.get(code, 1) or 1) != nw:
                self.eng.ab.level_change(u, code, nw)
        diff = arm - u.upg_art_arm
        if diff:
            u.armor_bonus += diff
        u.upg_art_arm = arm
        u.upg_art = art

    def sync_spikes(self, u, lvl, amount):
        """rspi: one step per research level moves the Aspi counter +-1; only a counter > 0 re-applies the level
        [obs 0x6f19c860 / 0x6f532a10]. The same call also folds the level's value into the ability's cached DataA
        amount, which is the thorns damage (CAbilitySpiked §5.15 reads the level-1 DataA; PORT14). Without the
        ability the hook is a no-op (not even the counter moves).
        PORT2-FIX: the ability is the first one whose BASE is Aspi (a map's custom spike ability counts), as in C#."""
        code = None
        for c, b, _lv in self.eng.ab.abil_list(u):
            if b == 'Aspi':
                code = c
                break
        if code is None:
            return
        cur = int(getattr(u, 'upg_spike', 0))
        while cur < lvl:
            cur += 1
            u.upg_spike = cur
            if cur > 0:
                self.eng.ab.level_change(u, code, cur)
        while cur > lvl:
            cur -= 1
            u.upg_spike = cur
            if cur > 0:
                self.eng.ab.level_change(u, code, cur)
        u.upg_spike_amt = float(amount)                       # PORT14: the Aspi holder's cached DataA amount

    def reset_type(self, u):
        """a type change resets life / mana to the new type's numbers: forget the applied totals, then sync."""
        u.upg_hp = 0.0
        u.upg_mp = 0.0
        u.upg_lev_code = u.upg_lev_steps = 0                # PORT4: the new type's abilities start at level 1 again
        u.upg_mim = 0                                       # PORT8: the new type's magic-buff strip applies again
        self.sync(u)

    def apply_rtma(self, pl, rid, old, new):
        """rtma [PORT8-Upgrade, CUpgradeAddTechMaxAllowed 0x6f19bb50 / 0x6f254600]: on a level transition the slot's
        code names a tech record of the player; the truncated V(new) - V(old) is applied to it only in the two sentinel
        cases: an unlimited record (the getter's 0x7fffffff, here the port's negative "no limit") becomes step - 1, and a
        record at 0 with step -1 becomes unlimited. Every other current value is left alone. The target is the owner's
        player-wide tech_max, not a unit number, so this runs once per gained level (never through sync)."""
        rec = self.eng.upgrades.get(rid)
        if rec is None or new <= old:
            return
        for i in range(1, 5):
            if (rec.get('effect%d' % i) or '') != 'rtma':
                continue
            cd = str(rec.get('code%d' % i) or '')
            if len(cd) != 4:
                continue                                    # packs before w3p1-75 have no code column: no target [inf]
            tid = _rc(cd)
            step = trunc(value(rec, i, new) - value(rec, i, old))
            cur = int(pl.tech_max.get(tid, -1))
            if cur < 0:                                     # 0x7fffffff: "no record / unlimited"
                cur = step - 1
            elif cur == 0 and step == -1:
                cur = -1                                    # 0x7fffffff
            pl.tech_max[tid] = cur

    def sync_player(self, pl):
        """a research level was gained / set: every unit of the player."""
        for u in list(self.eng.N.units):
            if u.owner is pl and u.alive:
                self.sync(u)
