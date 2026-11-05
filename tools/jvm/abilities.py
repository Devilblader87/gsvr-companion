"""W6 abilities, buffs and auras for the reference engine (docs/NOTES_abilities.md; clean-room: behaviour only).

The engine (engine.py) calls in at fixed points:
  swing start      on_swing(u, t)                 crit / bash proc rolls (§5.1), Pulverize stream 0x28 (§5.25)
  damage point     on_damage_point(u, t, i, amt)  -> Hit: crit multiplier, trueshot, never-miss, the hit claimer
                                                  (lowest priority 18…25 that claims, §3.1)
  launch           on_launch(u, t, i, hit)        melee / instant: stun + bonus instance + claimer buff first (§5.1,
                                                  §5.3); missiles carry the Hit to the impact
  miss check       missed(src, t, hit)            evasion (largest chance rolls, stream 2) (§3.2)
  impact           on_impact(m)                   buffs carried by a missile
  take damage      damage_hooks(t, src, amt, ...) priority 5: hardened skin, magic immunity reduction (§3.3)
  timers           step()                         buff expiry, DoT ticks (0.01 s then 1 s), auras (0.5 s / 2 s),
                                                  wander (§5.6, §5.10, §6)
Per-unit state lives on the Unit (natives.Unit): prd {code: n}, procs set, buffs [Buff], move/attack factor deltas.
Buffs are ability objects for the JASS side: GetUnitAbilityLevel(u, buff) = 1, UnitRemoveAbility removes them.

Assumptions tagged A-AB* are listed in docs/engine_w5.md (W6 section).
"""
import heapq
import os

from .vm import r32
from . import unitdata as UD


def _id(s):
    return (ord(s[0]) << 24) | (ord(s[1]) << 16) | (ord(s[2]) << 8) | ord(s[3])


def _id4(n):
    return ''.join(chr((n >> k) & 255) for k in (24, 16, 8, 0))


SPS = 200
# PRD table C[k] for 0 %, 5 %, … 100 % (§4.5) [obs]
PRD_C = (0.0, 0.0038, 0.0147, 0.0320, 0.0547, 0.0819, 0.1126, 0.1461, 0.1815, 0.2184, 0.2562, 0.2953, 0.3333,
         0.3781, 0.4203, 0.4607, 0.5000, 0.5799, 0.6666, 0.7706, 1.0)

CRIT = {'AOcr', 'AHbh'}                 # shared code (§5.1)
EVADE = {'AEev'}
POISON = {'Aspo': ('Bspo', 23), 'Aven': ('Bpoi', 11),   # buff, damage type (SLOW_POISON 23 / POISON 11) (§5.3)
          'Apoi': ('Bpoi', 11)}                          # §5.16 poison attack: the Aspo code with Bpoi / POISON
THORNS = {'Aspi'}                                        # §5.15 spiked barricades
FROST = {'Afrb', 'Afra'}                # Bfro on hit (§5.4)
LIGHTNING = {'Alit'}                    # claims priority 25, no effect (§5.5)
INCINERATE = {'ANic'}                   # §5.49: BNic before the hit, + A × count, the death explosion (A-AB19)
FEEDBACK = {'Afbk'}                     # §5.43: melee burn + bonus, missile burn at the impact (A-AB17)
LIQUID = {'Aliq'}                       # §5.42: missile / msplash launch hook 40, 'Bliq' after the impact (A-AB16)
BARRAGE = {'Aroc'}                      # §5.35: hit priority 24 without targs; missile launch hook 40 (A-SP66)
BARRAGE_DMG = 4                         # the rockets copy the attack record: DAMAGE_TYPE_NORMAL (engine.DMG_NORMAL)
AURAS = {'AUau', 'AHad', 'AOae', 'AEar', 'AHab', 'AUav', 'Aoar', 'Aarm', 'Aapl', 'Aasl', 'Aabr', 'AOac'}  # AOac: §5.51
TWANDER = {'Atwa'}
IMMOLATION = {'ANpi', 'Apig'}           # W8 permanent immolation (NOTES_spells §2.3): a tick every Dur s                      # W8 tornado wander: a move every Dur s within Area (A-SP20)
HARDSKIN = {'Assk'}
ARMOR_BONUS = {'AIde'}                  # §5.38 item armor bonus: + DataA of the current level (A-AB12)
DEF_BUFF = (ord('B') << 24) | (ord('d') << 16) | (ord('e') << 8) | ord('f')    # 'Bdef': defend is active (§5.59)
AVUL_ID = (ord('A') << 24) | (ord('v') << 16) | (ord('u') << 8) | ord('l')
NEGATIVE_KINDS = ('poison', 'frost', 'stun', 'roots', 'sleep', 'quake', 'arrow')    # A-SP22: what divine shield strips
MAGIC_IMMUNE = {'Amim'}
GHOST = {'Agho', 'Apiv'}                # Apiv permanent invisibility: the same counter (NOTES_spells §2)
DETECT = {'Adts', 'Atru', 'Adt1'}       # CAbilityDetector: Rng of level 1 + DataA kind mask (CMaskDetect)
WANDER = {'Awan'}
STUN_BUFF = 'BPSE'
HIT_PRIO = {'Aspo': 24, 'Aven': 24, 'Apoi': 24, 'Afrb': 25, 'Afra': 25, 'Alit': 25,     # §3.1 (item orbs 18+slot: items not yet)
            'AHca': 24,                     # W8 frost arrows (NOTES_heroes §4.1; 11 for a manual shot)
            'AHfa': 24,                     # searing arrows: the same class base (NOTES_heroes §4.11)
            'AIcb': 18,                     # §5.14 orb of corruption: 18 + item slot; on the unit itself slot 0 (A-AB11)
            'AIfb': 18, 'AIob': 18, 'AIpb': 18, 'AIsb': 18,   # W9R2-3 the other orbs (NOTES_items §11); item orbs 18 + slot
            'Aroc': 24,                     # §5.35 barrage: claims without a targs test, no effect of its own
            'ANic': 11,                     # §5.49 incinerate: armed every swing (no roll), claims at 11
            'Afbk': 24,                     # §5.43 feedback: claims when the target has mana (or a summon + DataE)
            'AIva': 12}                     # §5.41 life steal: 12 + item slot (on the unit 0); illusions do not claim
LIFE_STEAL = {'AIva'}                   # §5.41: the next damage record of the attacker heals it (A-AB15)
CORRUPTION = {'AIcb'}                   # §5.14: armor −DataB (BIcb), melee before the hit, missiles after the damage
ORBS = ('AIfb', 'AIob', 'AIpb', 'AIsb')  # W9R2-3 (NOTES_items §11): fire / frost / poison / spell-effect orbs, the AIcb skeleton
ORB_ALL = CORRUPTION | set(ORBS)         # + trunc(DataA) attack bonus, hit priority 18 + slot, launch 34 + slot
DUR_SNAP = set(POISON) | FROST | CORRUPTION | {'AHca'}   # PORT8: buff duration captured by the launcher
SB_TAG = (0x80, 0x8000, 0x800000)       # AIsb: the PRD counter of the target class (unit / hero / summon) is the code xor a non-ASCII bit
SB_STREAM = 0x24                         # AIsb swing roll: game stream 36 [obs 0x6f578775]
SB_ROW = {'Aslo'}                        # effect ability bases the orb can cast (NOTES_items §11.5; others Open)
ARROW_LIFE, ARROW_MANA, ARROW_LEFT = 100.0, 200.0, 1.0     # autocast filter constants (NOTES_heroes §5) [obs]
ARROWS = ('AHca', 'AHfa')               # orb-type arrow autocasts (NOTES_heroes §4.1, §4.11)
AURA_LIFE = 4.0                         # §5.6 [obs]
AURA_SCAN, AURA_PERIOD = 0.5, 2.0
DOT_FIRST, DOT_PERIOD = 0.01, 1.0       # §6.2 [obs]
SHIELD_FIRST, SHIELD_TICK = 0.01, 0.5   # §5.22 lightning shield [obs]
SSTRIKE_SCALE = 1.0                     # NOTES_spells §5.3: the slow is re-scaled every 1.0 s [obs]
DMG_SLOW_POISON, DMG_SHADOW_STRIKE = 23, 25
DMG_UNIVERSAL = 26                      # 0x4000000: the orbs' splash (NOTES_items §11.3)
CLEAVE = {'ANca'}                       # §5.39: armed every swing (no roll), normal / instant launch only (A-AB13)
PULVERIZE = {'Awar'}                    # §5.25: roll at the swing start (stream 0x28), splash after the melee hit
PURGE_KINDS = ('bloodlust', 'faerie', 'lshield', 'slow', 'roots', 'sleep', 'roar',
               'poly')                  # §5.23 magic buffs (A-SP48; polymorph removed [obs], A-SP62)
# PORT13: CBuffPoisonAttackStackInfo §5 / CBuffSlowPoisonStackInfo §2 keep the stack mode word and re-apply, on every
# refresh, only the stored values whose mode bit is set (a clear bit writes 0). This changes every fun map that sets a
# stacking poison word (Apoi's DataD default is 1), so it stays behind a default-OFF switch; Udon twin abPoisonStackMode.
POISON_STACK_MODE = os.environ.get('W3_POISON_STACK_MODE') == '1'


# PORT5-NativesB round 2 (Unclassed_Native_0032 §9 .. §11, 0033 §3): the buff category bits behind UnitHasBuffsEx /
# UnitCountBuffsEx / UnitRemoveBuffs(Ex). The engine answers them through seven per-buff virtuals (positive, negative,
# magic, physical, timed, aura, auto-dispel); the virtuals' bodies are not decoded, so the port assigns the bits by buff
# kind [inf]: magic = PURGE_KINDS (the purge strips them [obs], NOTES_abilities §5.23), negative = NEGATIVE_KINDS plus the
# other harmful spells, positive = the beneficial ones, an unlisted kind answers neither polarity and the queries skip it
# (a buff that is neither positive nor negative is never touched [obs 0x6f48b6c0]). No kind is physical or timed [inf].
# The same table is W3Abilities.cs AbBuffClass (bit for bit).
BC_POS, BC_NEG, BC_MAGIC, BC_PHYS, BC_TIMED, BC_AURA, BC_AUTO = 1, 2, 4, 8, 16, 32, 64
BUFF_POSITIVE_KINDS = ('bloodlust', 'lshield', 'roar', 'mshield', 'shield', 'rej', 'rpl', 'frenzy', 'invis', 'phase',
                       'voodoo', 'avatar', 'efcaster', 'immo_on', 'phx', 'defend', 'aura')
BUFF_NEGATIVE_KINDS = ('faerie', 'slow', 'roots', 'sleep', 'poly', 'ensnare', 'curse', 'silence', 'purge', 'sstrike',
                       'flame', 'incin', 'frost', 'stun', 'poison', 'arrow', 'corruption', 'acid', 'quake', 'impale',
                       'mdrain')
BUFF_CODE_POSITIVE = ('Bvul', 'Bdef')           # buffs the port adds without a kind of their own (invulnerable, defend)
BUFF_CODE_NEGATIVE = ('Bcrs',)                  # curse: the port adds it with kind 'curse' (C#: by code)
_BUFF_POS_CODES = frozenset(_id(c) for c in BUFF_CODE_POSITIVE)
_BUFF_NEG_CODES = frozenset(_id(c) for c in BUFF_CODE_NEGATIVE)


def buff_class(bf):
    """the category bits of a buff (see BC_*); 0 = neither polarity."""
    c = 0
    if bf.kind in BUFF_POSITIVE_KINDS or bf.code in _BUFF_POS_CODES:
        c = BC_POS
    elif bf.kind in BUFF_NEGATIVE_KINDS or bf.code in _BUFF_NEG_CODES:
        c = BC_NEG
    if c == 0:
        return 0
    if bf.kind in PURGE_KINDS:
        c |= BC_MAGIC | BC_AUTO
    if bf.kind == 'aura':
        c |= BC_AURA
    return c


def buff_match(c, pos, neg, magic, phys, timed, aura, auto):
    """one buff against the seven script flags (0032 §9 / §11, 0033 §3). Polarity [obs 0x6f48b990]: a positive buff
    passes when the first flag is set or the second is clear, a negative one when the second is set or the first is
    clear. Class flags [inf]: neither magic nor physical set = no class filter; any set = the buff passes when it is in
    at least one set class (OR). This is what Blizzard.j's callers need: UnitRemoveBuffsExBJ passes (magic, physical) =
    (true, true) for BOTH, (true, false) / (false, true) for MAGIC / PHYSICAL and (false, false) for EITHER, and
    UnitRemoveBuffs / the NONTLIFE dispel pass (false, false). 0032 §9 reads the flags as filters tested against the
    buff virtuals, 0033 §3 as "when zero a buff of that class is skipped" and its own text contradicts its table; neither
    reading is usable with BOTH (an AND would strip nothing), so the OR reading is used. No port buff kind is physical
    [no evidence], so a physical-only filter matches nothing. A clear timed / aura flag leaves those buffs out, the
    auto-dispel flag demands the auto class."""
    if c & (BC_POS | BC_NEG) == 0:
        return False
    if c & BC_POS:
        if not (pos or not neg):
            return False
    elif not (neg or not pos):
        return False
    if (magic or phys) and not ((magic and c & BC_MAGIC) or (phys and c & BC_PHYS)):
        return False
    if auto and not c & BC_AUTO:
        return False
    return not ((c & BC_TIMED and not timed) or (c & BC_AURA and not aura))


def steps_of(t):
    import math
    n = int(math.ceil(t * SPS - 1e-6))
    return n if n > 0 else 1


class Buff:
    __slots__ = ('code', 'kind', 'unit', 'src', 'end', 'vals', 'serial', 'move', 'atk', 'armor', 'regen', 'dot',
                 'dtype', 'aura', 'stun', 'alive', 'lv', 'orig')

    def __init__(self, code, kind, unit, src, end):
        self.code, self.kind, self.unit, self.src, self.end = code, kind, unit, src, end
        self.vals = {}
        self.serial = 0
        self.move = self.atk = self.armor = self.regen = self.dot = 0.0
        self.dtype = 0
        self.aura = None                    # the aura ability's owner (unit) for aura buffs
        self.stun = False
        self.alive = True
        self.lv = None                      # the level record of a buff that reads it later (shadow strike)
        self.orig = 0                       # polymorph: the unit's type before the morph (NOTES_abilities §5.31)


class Hit:
    """the attack record between damage point and delivery (§3.1)."""
    __slots__ = ('amount', 'claim', 'crit', 'never_miss', 'stun', 'bonus', 'pulv', 'cleave', 'liq', 'fb', 'mspeed', 'aux',
                 'bdur')

    def __init__(self, amount):
        self.amount = amount
        self.claim = None                   # (base code, level record) of the claiming on-hit ability
        self.bdur = None                    # PORT8: the buff duration snapshotted at launch (None = read it live)
        self.crit = None                    # (code, level record) of the proc'd crit / bash
        self.never_miss = False
        self.stun = 0.0                     # stun duration to apply
        self.bonus = 0.0                    # DataC bonus damage of a proc'd crit / bash
        self.pulv = None                    # level record of an armed pulverize (§5.25)
        self.cleave = None                  # level record of the armed cleaving attack (§5.39)
        self.liq = None                     # (level record, zero values) of a liquid fire launch (§5.42)
        self.fb = None                      # (max burn, ratio) of a feedback missile (§5.43)
        self.mspeed = 0.0                   # an ability missile's speed instead of the weapon's (§5.49)
        self.aux = None                     # (ability id, level record) an orb applies besides its own effect: AIsb's armed
                                            # effect ability, AIpb's Aspo sibling (NOTES_items §11)


class Abilities:
    DUR_SNAP = DUR_SNAP                    # PORT8: the missile launcher snapshots these buff durations

    def __init__(self, eng, defs):
        self.eng = eng
        self.N = eng.N
        self.defs = defs or {}              # {ability id int: abildata record}
        self.timers = []                    # (step, seq, kind, a, b)
        self.reveals = []                   # §5.44 point detectors [owner, x, y, radius, end step]
        self.seq = 0
        self.buff_serial = 0
        self.auras = []                     # units with an aura ability
        self.detectors = []                 # units with a detection ability
        misc = eng.misc
        self.frost_move = _misc(misc, 'FrostMoveSpeedDecrease', 0.5)
        self.frost_atk = _misc(misc, 'FrostAttackSpeedDecrease', 0.25)
        self.miss_reduction = _misc(misc, 'MissDamageReduction', 0.5)
        self.phase_pending = None           # (unit, ability) a phase shift to cast after the damage hooks

    # ------------------------------------------------------------ data
    def rec(self, code):
        return self.defs.get(code)

    def base(self, code):
        r = self.defs.get(code)
        return r['code'] if r else _id4(code)

    def lv(self, u, code):
        r = self.defs.get(code)
        if not r or not r['lv']:
            return None
        n = u.abilities.get(code, 1)
        n = max(1, min(n, len(r['lv'])))
        return r['lv'][n - 1]

    def abil_list(self, u):
        """(code, base, level record) in list order (= order of addition, §2)."""
        out = []
        for code in u.abilities:
            r = self.defs.get(code)
            if r is None:
                continue
            out.append((code, r['code'], self.lv(u, code)))
        return out

    def enabled(self, u):
        return not u.dead and u.alive

    def is_hero(self, u):
        c = (u.tid >> 24) & 255
        return 65 <= c <= 90

    def dur(self, lv, t):
        """§2 duration rule [obs]; Resistant Skin (Arsk) counts as a hero here (NOTES_spells §2.1)."""
        return lv['HeroDur'] if self.is_hero(t) or self.resistant(t) else lv['Dur']

    def resistant(self, t):
        for code in t.abilities:
            r = self.defs.get(code)
            if r is not None and r['code'] == 'Arsk':
                return True
        return False

    def targ_ok(self, owner_u, t, mask):
        """ability targs filter (unitdata TARG bits); no class flag = any class; no alliance flag = any [inf A-AB4]."""
        if mask == 0:
            return True
        eng = self.eng
        cls = mask & UD.T_CLASS
        if cls and not (cls & eng.target_class(t)):
            return False
        if t.invulnerable and not (mask & UD.T_INVULN) and (mask & UD.T_VULN):
            return False
        al = mask & UD.T_ALLIANCE
        if al:
            if t is owner_u:
                return bool(al & (UD.T_SELF | UD.T_PLAYER | UD.T_ALLY))
            enemy = not eng.ally(owner_u.owner, t.owner)
            if enemy and not (al & UD.T_ENEMY):
                return False
            if not enemy and not (al & (UD.T_ALLY | UD.T_PLAYER)):
                return False
        if (mask & UD.T_NONHERO) and not (mask & UD.T_HERO) and self.is_hero(t):
            return False
        if (mask & UD.T_HERO) and not (mask & UD.T_NONHERO) and not self.is_hero(t):
            return False
        return True

    # ------------------------------------------------------------ unit lifecycle
    def on_add(self, u, code):
        """an ability was added (type list at creation, UnitAddAbility)."""
        b = self.base(code)
        hv = getattr(self.eng, 'hv', None)
        if hv is not None:
            hv.on_add(u, b)                               # PORT-Harvest: a mine ability added later seeds the pool
        if b in AURAS and u not in self.auras:
            self.auras.append(u)
            self.at(self.eng.now() + steps_of(AURA_SCAN), 'aura_scan', u, None)
            self.at(self.eng.now() + 1, 'aura_full', u, None)
        if b in DETECT and u not in self.detectors:
            self.detectors.append(u)
        if b == 'Awrp':
            self.eng.wg_register(u, self.lv(u, code))                # NOTES_pathing 1.1: the gate area is written once, at ability add
        if b in MAGIC_IMMUNE:
            u.magic_immune += 1
        if b in GHOST:
            u.invisible = getattr(u, 'invisible', 0) + 1
        if b in ORB_ALL and not u.illusion:
            lv = self.lv(u, code)
            if lv is not None:
                u.dmg_int += int(lv['A'])                           # §5.14: +trunc(DataA) like AIat (every orb, §11.3)
        if b == 'Aamk':
            self.aamk_apply(u, code)
        if b == 'Arel' and not u.illusion:
            self.eng.item_lreg_calc(u)                              # NOTES_items §11.1: the unit's own Arel
        if b == 'AIml':
            self.max_life_bonus(u, code, 1)
        if b == 'Aroo':
            self.eng.root_init(u)                                   # PORT4-Build: CAbilityRoot, a rooted unit with the state's weapon mask
        if b == 'Arbr':
            self.eng.ue.sync(u)                                     # PORT11-Upgrade rart: a late Arbr gets its researched level
        if b == 'Aloc':
            self.locust(u, 1)
        if b in ARMOR_BONUS:
            self.armor_bonus(u, code, 1)
        if b in WANDER:
            self.schedule_wander(u)
        if b == 'Acpf':
            lv = self.lv(u, code)
            na = (lv.get('S') or {}).get('A', '') if lv else ''
            if len(na) == 4 and _id(na) == u.tid:
                u.ethereal += 1                                     # A-SP31: the normal form is ethereal
        if b in ('Arpl', 'Arpm') and _id4(code) in str(u.data.get('auto') or '').split(','):
            self.eng.sp.rpl_auto(u, code, True)                     # NOTES_abilities §5.55: the type's default autocast
        if b == 'Apsh' and u.owner.controller == 1:
            u.auto_on.add(code)                                     # A-SP26: computer players' units autocast
        if b in IMMOLATION:
            lv = self.lv(u, code)
            if lv is not None:
                self.at(self.eng.now() + steps_of(lv['Dur'] if lv['Dur'] > 0 else 1.0), 'immo', u, code)
        if b in ('Asud', 'Asid'):
            from .shops import DYN
            self.eng.shops.dyn_ensure(u, DYN[b])                    # NOTES_items §7.7: an added Dynamic shop class
        if b in TWANDER:
            lv = self.lv(u, code)
            if lv is not None:
                self.at(self.eng.now() + steps_of(lv['Dur'] if lv['Dur'] > 0 else 2.0), 'twander', u, code)

    def lv_at(self, code, n):
        """the level record of level n (clamped like lv) - for an ability that already left the unit's list."""
        r = self.defs.get(code)
        if not r or not r['lv']:
            return None
        return r['lv'][max(1, min(n, len(r['lv']))) - 1]

    def on_remove_abil(self, u, code, lvl=1):
        """an ability left the list (UnitRemoveAbility, type swap); lvl = the level it had (BUGS ETD-LIFE)."""
        b = self.base(code)
        if b == 'Aroo' and u.root_st:
            self.eng.root_drop(u)                                   # PORT4-Build: the root state goes with the ability
        if b == 'Arbr' and u.upg_art_arm:                           # PORT11-Upgrade rart: the ability takes its armor with it
            u.armor_bonus -= u.upg_art_arm
            u.upg_art_arm = 0.0
        if b == 'AHav':
            for bf in list(u.buffs):
                if bf.kind == 'avatar':
                    self.remove_buff(bf)                            # A-SP24: removing avatar ends its buff
        if b == 'Adef':
            self.remove_buff_code(u, DEF_BUFF)                      # §5.59: the ability goes, the toggle with it [obs 0x6f4dd2c0]
        if b == 'Awrp':
            self.eng.wg_unregister(u)                               # the area is cleared at the unit's position now, the slot freed
        if b in MAGIC_IMMUNE and u.magic_immune > 0:
            u.magic_immune -= 1
        if b in GHOST and getattr(u, 'invisible', 0) > 0:
            u.invisible -= 1
        if b in ORB_ALL and not u.illusion:
            lv = self.lv_at(code, lvl)
            if lv is not None:
                u.dmg_int -= int(lv['A'])
        if b == 'Aamk':
            self.aamk_remove(u, code)
        if b == 'Arel' and not u.illusion:
            self.eng.item_lreg_calc(u)
        if b == 'AIml':
            self.max_life_bonus(u, code, -1, self.lv_at(code, lvl))
        if b == 'Aloc':
            self.locust(u, -1)
        if b == 'AInv':
            self.eng.itm.detach(u, self.lv_at(code, lvl))      # §5.2: detach drops (or destroys) every carried item

    def armor_bonus(self, u, code, d):
        """§5.38 AIde (A-AB12): armor ± DataA of the current level (float); illusions get nothing [obs 0x6f57c520]."""
        lv = self.lv(u, code)
        if lv is not None and not u.illusion:
            u.armor_bonus += lv['A'] * d

    def before_remove(self, u, code):
        """UnitRemoveAbility, before the ability leaves the list (the level is still known)."""
        if self.base(code) in ARMOR_BONUS:
            self.armor_bonus(u, code, -1)

    def level_change(self, u, code, lvl):
        """SetUnitAbilityLevel: AIde unapplies, takes the new level's DataA, applies [obs 0x6f57c3d0 / 0x6f57c3f0].
        AIml only re-caches the new level's DataA, max life stays (§5.18 [obs 0x6f57b9d0 / 0x6f57ba10]); its removal
        takes that cached value off = the level the ability has then (on_remove_abil's lvl, BUGS ETD-LIFE)."""
        b = self.base(code)
        if b in ARMOR_BONUS:
            self.armor_bonus(u, code, -1)
        u.abilities[code] = lvl
        if b in ARMOR_BONUS:
            self.armor_bonus(u, code, 1)
        if b == 'Aamk':
            self.aamk_apply(u, code)
        if b == 'Arel' and not u.illusion:
            self.eng.item_lreg_calc(u)
        if b == 'AInv':
            self.eng.itm.reclamp(u)                             # §5.5: a shrinking DataA drops the vanished slots

    def aamk_apply(self, u, code):
        """Aamk [obs 0x6f57cfc0 / 0x6f57d070 / 0x6f57cf60]: a hero takes back what the ability gave, reads trunc(DataA) agility,
        trunc(DataB) intelligence, trunc(DataC) strength of its current level and adds them; a unit without hero state
        (+0x1ec) keeps 0 (NOTES_items §11.2)."""
        old = u.aamk.get(code, (0, 0, 0))
        lv = self.lv(u, code)
        new = (0, 0, 0)
        if getattr(u, 'hero', False) and lv is not None:
            new = (int(lv['C']), int(lv['A']), int(lv['B']))
        elif not getattr(u, 'hero', False):
            return
        if new != old:
            self.eng.hero.add_bonus(u, new[0] - old[0], new[1] - old[1], new[2] - old[2])
        u.aamk[code] = new

    def aamk_remove(self, u, code):
        old = u.aamk.pop(code, (0, 0, 0))
        if old != (0, 0, 0) and getattr(u, 'hero', False):
            self.eng.hero.add_bonus(u, -old[0], -old[1], -old[2])

    def locust(self, u, d):
        """NOTES_abilities §5.34 (A-SP65): add = out of the spatial index (range / rect enums, acquisition, area
        effects), no footprint, invulnerable + magic-immune counters + 1; removal gives back only the two counters."""
        u.invuln_n += d
        u.invulnerable = AVUL_ID in u.abilities or u.invuln_n > 0
        u.magic_immune += d
        if d > 0 and not u.locust:
            u.locust = True
            self.eng.relocate(u)
            if u.footprint:
                self.eng.remove_footprint(u)

    def max_life_bonus(self, u, code, d, lv=None):
        """§5.18 AIml: max life ± DataA keeping the life fraction, at least 1 while alive [obs; mode 1 partly inf].
        lv: the level record (removal: the level the ability had, BUGS ETD-LIFE); None = the current level."""
        if lv is None:
            lv = self.lv(u, code)
        if lv is None or lv['A'] == 0:
            return
        old = u.maxlife
        frac = u.life / old if old > 0 else 1.0
        u.maxlife = r32(max(1.0, old + lv['A'] * d))
        nl = r32(frac * u.maxlife)
        if u.alive and not u.dead and nl < 1.0:
            nl = 1.0
        u.life = nl

    def on_death(self, u):
        self.incinerate_death(u)                                    # §5.49: before the buffs go
        for bf in list(u.buffs):
            self.remove_buff(bf)
        if u in self.auras:
            self.auras.remove(u)
        if u in self.detectors:
            self.detectors.remove(u)

    # ------------------------------------------------------------ timers
    def at(self, step, kind, a, b):
        self.seq += 1
        heapq.heappush(self.timers, (step, self.seq, kind, a, b))

    def step(self):
        now = self.eng.now()
        q = self.timers
        while q and q[0][0] <= now:
            _s, _q, kind, a, b = heapq.heappop(q)
            self.eng.wk += 1
            if kind == 'buff_end':
                if a.alive and a.end == _s:
                    self.remove_buff(a)
                    if a.kind in ('roots', 'sleep') and a.unit.alive and not a.unit.dead:
                        src = a.src if a.src is not None and a.src.alive and not a.src.dead else None
                        self.eng.damaged_event(a.unit, src, 0.0)   # 0-damage notice at the end (NOTES_heroes §4.4)
            elif kind == 'dot':
                self.dot_tick(a, _s)
            elif kind == 'aura_scan':
                self.aura_pass(a, False)
                if a in self.auras:
                    self.at(now + steps_of(AURA_SCAN), 'aura_scan', a, None)
            elif kind == 'aura_full':
                self.aura_pass(a, True)
                if a in self.auras:
                    self.at(now + steps_of(AURA_PERIOD), 'aura_full', a, None)
            elif kind == 'wander':
                self.wander(a)
            elif kind == 'twander':
                self.twander(a, b)
            elif kind == 'immo':
                self.immolation(a, b)
            elif kind == 'immo_a':
                self.immolation_active(a, b)
            elif kind == 'lsh':
                self.shield_tick(a, b)
            elif kind == 'prg':
                self.purge_tick(a, b)
            elif kind == 'mim':
                self.strip_magic(a)                      # PORT8-Upgrade rmim (CUpgradeMagicImmunity §4)
            elif kind == 'cswarm':
                self.eng.sp.swarm_sweep(a)                # NOTES_spells §5.2 (A-SP57)
            elif kind == 'wvvis':
                self.eng.sp.wave_vis(a)                   # NOTES_spells §5.20 (A-SP79)
            elif kind == 'wvdmg':
                self.eng.sp.wave_dmg(a)
            elif kind in ('lcspawn', 'lcend', 'lcloop', 'lcseek', 'lcarr', 'lcdie'):
                self.eng.sp.lc_timer(kind, a, b)          # NOTES_spells §5.16 locust swarm (A-SP83)
            elif kind == 'vcwave':
                self.eng.sp.volc_wave(a)                  # NOTES_spells §5.15 (A-SP82)
            elif kind == 'vcend':
                self.eng.sp.volc_end(a)
            elif kind == 'vcdrop':
                self.eng.sp.volc_drop(a, b)
            elif kind == 'vcrock':
                self.eng.sp.volc_rock(a, b)
            elif kind == 'stspawn':
                self.eng.sp.stamp_tick(a, b)              # NOTES_spells §5.18 (A-SP77)
            elif kind == 'stbeast':
                self.eng.sp.stamp_beast(a)
            elif kind == 'stdmg':
                self.eng.sp.stamp_damage(a)
            elif kind == 'kaboom':
                self.eng.sp.kaboom(a)                     # NOTES_abilities §5.52 (A-AB21)
            elif kind == 'mdrain':
                self.eng.sp.monsoon_drain(a, b)           # NOTES_spells §3.5 / §5.6 (A-SP74)
            elif kind == 'esh':
                self.sstrike_scale(a)
            elif kind == 'inferno':
                self.eng.sp.inferno_impact(a, b)          # NOTES_spells §3.4 / §5.8 (A-SP52)
            elif kind == 'impale_w':
                self.eng.sp.impale_step(a)                # NOTES_spells §5.13 (A-SP61)
            elif kind == 'impale':
                self.impale_land(a, b)
            elif kind == 'phx':
                self.eng.sp.phx_back(a)                             # NOTES_abilities §5.50 (A-SP72)
            elif kind == 'incin':
                self.incinerate_explode(a)                          # §5.49 (A-AB19)
            elif kind == 'rpl':
                self.eng.sp.rpl_check(a, b)                         # NOTES_abilities §5.55 (A-SP81)
            elif kind == 'reveal':
                if a in self.reveals:
                    self.reveals.remove(a)                          # §5.44: the Xbdt lifetime ends
            elif kind == 'root_end':
                self.eng.root_end(a)                                # PORT4-Build: the root / unroot transition is over

    # ------------------------------------------------------------ buffs (§6)
    def find_buff(self, u, code):
        for bf in u.buffs:
            if bf.code == code and bf.alive:
                return bf
        return None

    def find_buff_src(self, u, code, src):
        """the stacking buff of this caster on u (one info object per caster / target, CMissileSlowPoison §3.4)."""
        for bf in u.buffs:
            if bf.code == code and bf.alive and bf.src is src:
                return bf
        return None

    def refresh_extend(self, t, code, kind, src, dur):
        """CMissileFrost §5.1 (0x6f48eb70): a found buff keeps its caster and only ever gets a LATER end (a shorter
        re-hit never shortens it; a non-positive duration makes it permanent); the caller rewrites the factors.
        Not found: a new buff as add_buff. -> (buff, new)."""
        bf = self.find_buff(t, code)
        if bf is None:
            return self.add_buff(t, code, kind, src, dur)
        self.unapply(bf)
        if bf.end >= 0:
            end = self.eng.now() + steps_of(dur) if dur > 0 else -1
            if end < 0 or end > bf.end:
                bf.end = end
                if end >= 0:
                    self.at(end, 'buff_end', bf, None)
        return bf, False

    def add_stack_buff(self, t, code, kind, src, dur, dot, move, atk, dtype, mode=None, slow=False):
        """stacking poison (DataD & 7 != 0): one DoT per caster and target; a re-hit only extends the duration, never
        shortens it. PORT13 / CBuffPoisonAttackStackInfo §5, CBuffSlowPoisonStackInfo §2: when the switch carries the
        whole mode word, a re-hit also re-applies the stored values selected by its low bits (attack family bits
        0/1/2 = amount/move/attack, the slow-poison family bits 1/2/4); a clear bit writes 0."""
        now = self.eng.now()
        bf = self.find_buff_src(t, code, src)
        if bf is not None:
            if mode is not None:
                dm, mm, am = (2, 4, 8) if slow else (1, 2, 4)
                self.unapply(bf)
                bf.dot = dot if mode & dm else 0.0
                bf.move = move if mode & mm else 0.0
                bf.atk = atk if mode & am else 0.0
                self.apply(bf)
            if bf.end < 0:
                return bf                                       # already permanent
            end = now + steps_of(dur) if dur > 0 else -1
            if end < 0 or end > bf.end:
                bf.end = end
                if end >= 0:
                    self.at(end, 'buff_end', bf, None)
            return bf
        bf, new = self.add_buff(t, code, kind, src, dur, refresh=False)
        bf.dot, bf.move, bf.atk, bf.dtype = dot, move, atk, dtype
        self.apply(bf)
        if new:
            self.at(now + steps_of(DOT_FIRST), 'dot', bf, None)
        return bf

    def add_buff(self, t, code, kind, src, dur, refresh=True):
        """one instance per code (refresh) unless refresh is False (stacking DoTs) (§6.1)."""
        now = self.eng.now()
        bf = self.find_buff(t, code) if refresh else None
        end = now + steps_of(dur) if dur > 0 else -1
        if bf is not None:
            self.unapply(bf)
            bf.src = src
            bf.end = end
            new = False
        else:
            bf = Buff(code, kind, t, src, end)
            self.buff_serial += 1
            bf.serial = self.buff_serial
            t.buffs.append(bf)
            new = True
        if code == _id('Bvul'):
            bf.vals['inv'] = 1                 # CBaseBuffInvulnerable §8/§9: apply raises +0xe8, remove lowers it
        if end >= 0:
            self.at(end, 'buff_end', bf, None)
        return bf, new

    def apply(self, bf):
        """add the buff's deltas to its unit (after the values are set)."""
        u = bf.unit
        if bf.move:
            u.move_delta += bf.move
            self.eng.speed_changed(u)
        if bf.atk:
            self.eng.set_attack_factor(u, u.atk_delta + bf.atk)
        if bf.armor:
            u.armor_bonus += bf.armor
        if bf.regen:
            u.regen_bonus += bf.regen
            self.eng.set_regen(u)
        if bf.stun:
            u.stunned += 1
            self.eng.stun_started(u)
        if bf.vals.get('dmg'):
            u.dmg_bonus_f += bf.vals['dmg']
        self.apply_state(bf, 1)

    def apply_state(self, bf, d):
        """W8 unit-state counters of spell buffs (NOTES_spells §0 item 1)."""
        u, v = bf.unit, bf.vals
        if v.get('inv'):
            u.invuln_n += d
            u.invulnerable = AVUL_ID in u.abilities or u.invuln_n > 0
        if v.get('mimm'):
            u.magic_immune += d
        if v.get('invis'):
            u.invisible = getattr(u, 'invisible', 0) + d
        if v.get('life'):
            L = v['life'] * d
            life = u.life
            u.maxlife = r32(u.maxlife + L)
            u.life = r32(min(max(life + L, 0.0), u.maxlife))          # A-SP23
        if v.get('dint'):
            u.dmg_int += v['dint'] * d
        if v.get('sil'):
            u.silenced += d                                             # NOTES_spells §5.4 bit 8
        if v.get('atkoff'):
            u.atk_off += d                                              # A-SP55: like a stun for the attack only
            if d > 0:
                self.eng.stun_started(u)
            elif u.atk_off == 0:
                self.eng.wake(u, self.eng.now())

    def unapply(self, bf):
        u = bf.unit
        if bf.move:
            u.move_delta -= bf.move
            self.eng.speed_changed(u)
        if bf.atk:
            self.eng.set_attack_factor(u, u.atk_delta - bf.atk)
        if bf.armor:
            u.armor_bonus -= bf.armor
        if bf.regen:
            u.regen_bonus -= bf.regen
            self.eng.set_regen(u)
        if bf.stun:
            u.stunned -= 1
            if u.stunned == 0:
                self.eng.wake(u, self.eng.now())
        if bf.vals.get('dmg'):
            u.dmg_bonus_f -= bf.vals['dmg']
        self.apply_state(bf, -1)
        bf.move = bf.atk = bf.armor = bf.regen = 0.0
        bf.vals = {}
        bf.stun = False

    def remove_buff(self, bf):
        if not bf.alive:
            return
        self.unapply(bf)
        bf.alive = False
        if bf in bf.unit.buffs:
            bf.unit.buffs.remove(bf)
        u = bf.unit
        if bf.kind == 'poly' and bf.orig and u.alive and not u.dead and bf.orig in self.N.unit_types:
            self.eng.sp.poly_morph(u, bf.orig)                      # §5.31: the morph back (a dead unit stays)

    def remove_buff_code(self, u, code):
        found = False
        for bf in list(u.buffs):
            if bf.code == code:
                self.remove_buff(bf)
                found = True
        return found

    def strip_magic(self, u):
        """CUpgradeMagicImmunity §4 [obs 0x6f19c3c0 / 0x6f254c10]: every magic-class buff goes, positive or negative
        and any alliance (the Purge step-1 selection, PURGE_KINDS); the aura class is not in the set."""
        for bf in list(u.buffs):
            if bf.alive and bf.kind in PURGE_KINDS:
                self.remove_buff(bf)

    def has_buff(self, u, code):
        return self.find_buff(u, code) is not None

    def stun(self, t, src, dur):
        """BPSE stun (§5.1): an existing one is refreshed. Only exactly 0.0 skips the buff step; a negative
        duration is permanent (CMissileCriticalStrike §5 step 3). CBuffPause §2 step 3: the buff of an illusion
        source is created with the reject flag, so it never pauses; §4: a refresh updates the source and the
        values and only ever extends the remaining duration, the pause state is not re-decided."""
        if dur == 0:
            return
        old = self.find_buff(t, _id(STUN_BUFF))
        was = old is not None and old.stun
        old_end = old.end if old is not None else None
        bf, new = self.add_buff(t, _id(STUN_BUFF), 'stun', src, dur)
        if new:
            bf.stun = not (src is not None and getattr(src, 'illusion', False))
        else:
            bf.stun = was
            if old_end is not None and (old_end < 0 or (bf.end >= 0 and bf.end < old_end)):
                bf.end = old_end
        self.apply(bf)

    def impale_on(self, t, src, lv):
        """NOTES_spells §5.13: BUim holds the unit for DataD (in the air) + Dur′ (stunned after the landing); the
        landing after DataD deals DataC FORCE from the caster (attack type 0) [obs 0x6f598980, 0x6f598d40]. The landing
        timer is armed before the buff's end (same step when Dur′ is 0: the landing ends the buff, then the damage)."""
        air = lv['D']
        bf, _new = self.add_buff(t, _id('BUim'), 'impale', src, 0.0)
        bf.stun = True
        self.apply(bf)
        now = self.eng.now()
        self.at(now + steps_of(air), 'impale', bf, lv)
        bf.end = now + steps_of(air + self.dur(lv, t))
        self.at(bf.end, 'buff_end', bf, None)

    def impale_land(self, bf, lv):
        t = bf.unit
        if not bf.alive:
            return                                                          # removed early: its timer goes with it
        if bf.end == self.eng.now():
            self.remove_buff(bf)                                            # Dur′ 0: the buff ends at the landing
        if not t.alive or t.dead:
            return
        src = bf.src if bf.src is not None and bf.src.alive and not bf.src.dead else None
        self.eng.damage(t, src, r32(lv['C']), 0, 17, False, False)          # FORCE 0x20000

    def dot_tick(self, bf, s):
        if not bf.alive:
            return
        if bf.end >= 0 and s >= bf.end:
            return
        t = bf.unit
        src = bf.src if bf.src is not None and bf.src.alive and not bf.src.dead else None
        if bf.kind == 'flame':
            bf.dot = bf.vals['a1'] if s < bf.vals['ph'] else bf.vals['a2']     # NOTES_spells §5.14: the phase switch
        if bf.kind == 'rej':                                              # NOTES_abilities §5.28 (A-SP80)
            if t.alive and not t.dead:
                t.life = r32(min(t.maxlife, t.life + bf.vals['hl']))
                if t.maxmana > 0:
                    t.mana = r32(min(t.maxmana, t.mana + bf.vals['hm']))
        if bf.dot > 0:
            self.eng.damage(t, src, r32(bf.dot), 0, bf.dtype, False, False, dot=True)
        if bf.alive and t.alive and not t.dead:
            self.at(s + steps_of(self.dot_period(bf, s)), 'dot', bf, None)

    @staticmethod
    def dot_period(bf, s=0):
        """§6.2 1 s; shadow strike: the ability's Cast field (NOTES_spells §5.3; 0 → 1 s, A-SP53)."""
        if bf.kind == 'flame':
            p = bf.vals['p1'] if s < bf.vals['ph'] else bf.vals['p2']
            return p if p > 0 else DOT_PERIOD                       # NOTES_spells §5.14 (≤ 0: 1 s [inf])
        if bf.kind == 'sstrike' and bf.lv is not None and bf.lv['Cast'] > 0:
            return bf.lv['Cast']
        if bf.kind == 'acid' and bf.lv is not None and bf.lv['F'] > 0:
            return bf.lv['F']                                       # NOTES_abilities §5.47: every DataF s
        return DOT_PERIOD

    # ------------------------------------------------------------ shadow strike (NOTES_spells §5.3)
    def sstrike_on(self, t, src, lv):
        """BEsh for Dur′ (refresh: new values, the running timers go on): DataA SHADOW_STRIKE every Cast s from
        Cast s after the impact; move −DataB, attack −DataC, × (remaining / T)^DataD every 1 s."""
        bf, new = self.add_buff(t, _id('BEsh'), 'sstrike', src, self.dur(lv, t))
        bf.lv = lv
        bf.dot, bf.dtype = lv['A'], DMG_SHADOW_STRIKE
        bf.move, bf.atk = -lv['B'], -lv['C']
        self.apply(bf)
        if new:
            now = self.eng.now()
            self.at(now + steps_of(self.dot_period(bf)), 'dot', bf, None)
            self.at(now + steps_of(SSTRIKE_SCALE), 'esh', bf, None)

    def sstrike_scale(self, bf):
        """f = (remaining / T)^DataD (T = Dur′; f < 0.001 → 0), the deltas replaced by −DataB·f / −DataC·f [obs
        0x6f5288c0; remaining inf]."""
        if not bf.alive or bf.end < 0:
            return
        u, lv, eng = bf.unit, bf.lv, self.eng
        now = eng.now()
        T = self.dur(lv, u)
        f = 0.0
        if T > 0 and bf.end > now:
            f = ((bf.end - now) / SPS / T) ** lv['D']
            if f < 0.001:
                f = 0.0
        mv, at = -lv['B'] * f, -lv['C'] * f
        if bf.move or mv:
            u.move_delta = u.move_delta - bf.move + mv
            bf.move = mv
            eng.speed_changed(u)
        if bf.atk or at:
            eng.set_attack_factor(u, u.atk_delta - bf.atk + at)
            bf.atk = at
        if f > 0 and u.alive and not u.dead:
            self.at(now + steps_of(SSTRIKE_SCALE), 'esh', bf, None)

    # ------------------------------------------------------------ attack hooks (§3.1, §5.1)
    def prd(self, u, code, chance, stream, strict=False):
        """pseudo-random proc (§4.5): proc if U <= C[k] · n (strict: U < C[k] · n, pulverize §5.25)."""
        k = int(chance * 0.2 + 0.5)
        k = 0 if k < 0 else (20 if k > 20 else k)
        n = u.prd.get(code, 1)
        roll = self.eng.streams.game[stream].unit()
        if self.eng.trace is not None:
            from .engine import fmt_g
            self.eng.log('prd u%d %d n%d s%d %s' % (u.serial, code, n, stream, fmt_g(roll)))
        if (roll < PRD_C[k] * n) if strict else (roll <= PRD_C[k] * n):
            u.prd[code] = 1
            return True
        u.prd[code] = n + 1
        return False

    def sb_roll(self, u, t, key, aid, lv):
        """AIsb swing start [obs 0x6f5786f0]: a target that passes targs gets the PRD roll of its class (hero DataC, summon
        DataD, else DataB; one counter per class) on game stream 0x24; a proc arms this orb (flag 0x400) until its
        launch, which casts the effect ability (NOTES_items §11.5)."""
        if not self.targ_ok(u, t, lv['targs']):
            return
        cls = 1 if self.is_hero(t) else (2 if getattr(t, 'summoned', False) else 0)
        chance = lv['C'] if cls == 1 else (lv['D'] if cls == 2 else lv['B'])
        if self.prd(u, aid ^ SB_TAG[cls], chance, SB_STREAM):
            u.sb_arm.append(key)

    def on_swing(self, u, t):
        u.procs = []
        u.pulv = None
        u.cleave = None
        u.sb_arm = []
        if not u.abilities:
            return
        for code, b, lv in self.abil_list(u):
            if b == 'AIsb':
                if lv is not None:
                    self.sb_roll(u, t, (code, -1), code, lv)           # on the unit itself: slot 0 (key slot -1)
                continue
            if b in CLEAVE:
                if lv is not None and u.cleave is None and self.targ_ok(u, t, lv['targs']):
                    u.cleave = lv                                   # §5.39: armed every swing, no roll (the first, A-AB13)
                continue
            if b in CRIT and lv is not None and self.targ_ok(u, t, lv['targs']) and not self.has_buff(u, _id('BOwk')):
                if self.prd(u, code, lv['A'], 3):
                    u.procs.append((code, lv))
            elif b in PULVERIZE and lv is not None and self.targ_ok(u, t, lv['targs']):
                if self.prd(u, code, lv['A'], 0x28, True) and u.pulv is None:
                    u.pulv = lv                                     # §5.25: armed (flag 0x200)
        for slot, aid, _sib in u.orbs:                              # item orbs: the abilities joined the list at the pickup
            r = self.defs.get(aid)
            if r['code'] == 'AIsb':
                self.sb_roll(u, t, (aid, slot), aid, r['lv'][0])

    def on_damage_point(self, u, t, i, amount):
        hit = Hit(amount)
        for code, lv in u.procs:
            if hit.crit is None:
                hit.crit = (code, lv)
                if lv['B'] != 0:                                  # AddBonus refuses a second positive bonus
                    hit.amount = r32(amount * lv['B'])
                if lv['E'] != 0:
                    hit.never_miss = True
                hit.stun = self.dur(lv, t)
                hit.bonus = lv['C']
        u.procs = []
        hit.pulv, u.pulv = u.pulv, None                             # §5.25: the attack's end clears the flag
        hit.cleave, u.cleave = getattr(u, 'cleave', None), None
        if u.illusion:
            hit.amount = r32(hit.amount * u.img_dealt)              # mirror image: priority 26 (A-SP33)
        for code, b, lv in self.abil_list(u):
            if b == 'AIdd' and lv is not None and lv['B'] != 0:
                hit.amount = r32(hit.amount * lv['B'])              # §5.27: the holder's own attacks × DataB
                break
            if b == 'Adef' and lv is not None and lv['B'] != 0 and self.has_buff(u, DEF_BUFF):
                hit.amount = r32(hit.amount * lv['B'])              # §5.59: the same, while defending (0x6f4dd690)
                break
        # trueshot-type aura bonus (§5.6: damage + DataA × base damage) [ext; A-AB6 base = dmgplus]
        if u.dmg_bonus_f:
            hit.amount = r32(hit.amount + u.dmg_bonus_f * float(self.eng.wv(u, i, 'dmgplus') or 0))
        best = None
        own = None
        steal = None
        for code, b, lv in self.abil_list(u):
            p = HIT_PRIO.get(b)
            if p is None or lv is None:
                continue
            if b in LIFE_STEAL:
                if steal is None and self.targ_ok(u, t, lv['targs']):
                    steal = (p, code, b, lv)
                continue
            if b in ARROWS:
                manual = u.arrow_t is t and t is not None
                if not self.arrow_ok(u, code, b, lv, t, manual):
                    continue
                if manual:
                    p = 11
            if b in FEEDBACK and not self.feedback_claims(t, lv):
                continue
            if b in LIGHTNING or b in BARRAGE or self.targ_ok(u, t, lv['targs']):
                if best is None or p < best[0]:
                    best = (p, code, b, lv)
                    own = (code, -1)
        for slot, aid, sib in u.orbs:                               # item orbs claim at 18 + slot (NOTES_items §11.3)
            r = self.defs.get(aid)
            lv = r['lv'][0]
            if self.targ_ok(u, t, lv['targs']) and (best is None or 18 + slot < best[0]):
                best = (18 + slot, aid, r['code'], lv)
                own = (aid, slot)
        if steal is not None and (best is None or best[0] > steal[0]):
            u.lsteal = steal[3]                                     # §5.41: armed; an illusion arms without claiming
            if not u.illusion:
                best = steal
        if best is not None:
            hit.claim = (best[1], best[2], best[3])
            if best[2] in ORBS and own is not None and not u.illusion:
                hit.aux = self.orb_aux(u, t, best[2], own)
            if best[2] in ARROWS:
                u.arrow_t = None
                hit.amount = r32(hit.amount + best[3]['A'])          # A-SP17 / A-SP59: the COLD / FIRE bonus joins
                self.eng.sp.arrow_effect(u, best[1], best[3], t)     # SPELL_EFFECT, mana, cooldown per shot
        u.sb_arm = []                                               # the attack's end clears the AIsb flags (0x400)
        return hit

    def orb_aux(self, u, t, b, own):
        """what a claiming orb carries to its launch (NOTES_items §11): AIsb - the effect ability when this orb rolled a
        proc at the swing start; AIpb - the Apo2-class ability of the same item (slot) [obs 0x6f575a90 callback]."""
        aid, slot = own
        if b == 'AIsb':
            if own not in u.sb_arm:
                return None
            lv = self.lv(u, aid) if slot < 0 else self.defs[aid]['lv'][0]     # a unit ability: its current level
            cid = _id(lv['UnitID']) if len(str(lv.get('UnitID') or '')) == 4 else 0
            r = self.defs.get(cid)
            if r is None or not r['lv'] or r['code'] not in SB_ROW:
                return None                                         # effect classes beyond Aslo: Open
            if not self.targ_ok(u, t, r['lv'][0]['targs']):
                return None
            return (cid, r['lv'][0])
        if b == 'AIpb':
            sib = 0
            if slot < 0:
                for code, bb, _lv in self.abil_list(u):
                    if bb == 'Apo2':
                        sib = code
                        break
            else:
                for o in u.orbs:
                    if o[0] == slot and o[1] == aid:
                        sib = o[2]
                        break
            if sib and sib in self.defs:
                return (sib, self.defs[sib]['lv'][0])
        return None

    @staticmethod
    def summoned_or_illusion(t):
        return bool(getattr(t, 'summoned', False) or (t.ucls & 4) or t.illusion)

    def incinerate_on(self, t, src, lv):
        """§5.49 (0x6f4f0320): BNic (the fixed code) for Dur′ — new: count 1 (an illusion's 0) and the values; refresh:
        count + 1 (not for an illusion), the duration restarts, A…E the larger, F the smaller; the applier's player."""
        illu = bool(getattr(src, 'illusion', False))
        old = self.find_buff(t, _id('BNic'))
        keep = dict(old.vals) if old is not None else None
        bf, new = self.add_buff(t, _id('BNic'), 'incin', src, self.dur(lv, t))
        if new or keep is None:
            bf.vals = {k: lv[k] for k in 'ABCDEF'}
            bf.vals['n'] = 0 if illu else 1
        else:
            v = keep
            for k in 'ABCDE':
                if lv[k] > v[k]:
                    v[k] = lv[k]
            if lv['F'] < v['F']:
                v['F'] = lv['F']
            if not illu:
                v['n'] += 1
            bf.vals = v
        bf.vals['p'] = src.owner if src is not None else t.owner
        self.apply(bf)

    def incinerate_bonus(self, t, src, amount):
        """§5.49 damage hook priority 8 (0x6f4f1390): a NORMAL / ENHANCED record from a source with ANic gets
        + A × count of the target's BNic."""
        bf = self.find_buff(t, _id('BNic'))
        if bf is None or not any(b in INCINERATE for _c, b, _lv in self.abil_list(src)):
            return amount
        return r32(amount + r32(bf.vals['A'] * bf.vals['n']))

    def incinerate_death(self, u):
        """§5.49 death hook (0x6f4f14b0): a non-hero leaves no corpse; count ≥ 1 → after DataF the two rings (C, B) and
        (E, D) around the death point, FIRE, enemies of the applier's player on the dying unit's layer (ground / air)."""
        bf = self.find_buff(u, _id('BNic'))
        if bf is None:
            return
        if not self.is_hero(u):
            u.exploded = True
        v = bf.vals
        if v.get('n', 0) < 1:
            return
        ex = (u.x, u.y, v['p'], v['C'], v['B'], v['E'], v['D'], self.eng.flying(u))
        self.at(self.eng.now() + steps_of(v['F']), 'incin', ex, None)

    def incinerate_explode(self, ex):
        x, y, p, c, b, e, d, air = ex
        eng = self.eng
        rings = sorted([r for r in ((c, b), (e, d)) if r[0] > 0], key=lambda r: r[0])
        if not rings:
            return
        rmax = rings[-1][0]
        hits = []
        for v in sorted((v for v in eng.query(x, y, rmax + eng.max_coll) if v.alive and not v.dead),
                        key=lambda v: v.serial):
            dx, dy = v.x - x, v.y - y
            d2 = dx * dx + dy * dy
            if d2 > rmax * rmax or eng.ally(p, v.owner) or eng.flying(v) != air:
                continue
            for r, dmg in rings:
                if d2 <= r * r:
                    hits.append((v, dmg))
                    break
        for v, dmg in hits:
            if v.alive and not v.dead:
                eng.damage(v, None, r32(dmg), 0, 8, False, False)

    def feedback_claims(self, t, lv):
        """§5.43 hit claim (0x6f4ac9d0, after targs): the target has max mana > 0, or DataE > 0 and it is summoned or an
        illusion."""
        return t is not None and (t.maxmana > 0 or (lv['E'] > 0 and self.summoned_or_illusion(t)))

    def feedback_melee(self, u, t, hit, lv):
        """§5.43 normal / instant launch (0x6f4acb00): burn = min(A, or C on a hero, mana); the purge step-1 routine
        with summon damage E and mana loss burn (no buff strip): a magic-immune target → an event-only 0 record (enemy),
        no mana loss; else E MAGIC to an enemy summon / illusion without Bams (other enemies: a 0 record), mana − burn
        without Bams; then the hit + burn × (B, or D on a hero) in any case [obs: the local is not rewritten]."""
        eng = self.eng
        hero = self.is_hero(t)
        burn = lv['C'] if hero else lv['A']
        if burn > t.mana:
            burn = t.mana
        enemy = not eng.ally(u.owner, t.owner)
        if not t.illusion and t.magic_immune > 0:
            if enemy:
                eng.damaged_event(t, u, 0.0)
        else:
            bams = self.has_buff(t, _id('Bams'))
            if enemy:
                if self.summoned_or_illusion(t) and not bams:
                    eng.damage(t, u, r32(lv['E']), 0, 14, False, False)
                else:
                    eng.damaged_event(t, u, 0.0)
            if not bams and burn != 0:
                t.mana = r32(max(0.0, t.mana - burn))
        hit.amount = r32(hit.amount + r32(burn * (lv['D'] if hero else lv['B'])))

    def feedback_impact(self, m, t):
        """§5.43 feedback missile (0x6f4ace90), before the miss check: burned = min(max burn, mana) (no immunity,
        alliance or Bams test) → the carried damage + burned × ratio."""
        mx, ratio = m.hit_rec.fb
        burned = mx if mx < t.mana else t.mana
        if burned > 0:
            t.mana = r32(t.mana - burned)
        m.amount = r32(m.amount + r32(burned * ratio))

    def life_steal(self, src, t, amount):
        """§5.41 damage-dealt hook (0x6f57f660): the armed AIva of src is used up by its next damage record; amount =
        min(dealt, the target's life before) > 0, t passes targs, neither an illusion → src + amount × DataA."""
        lv = getattr(src, 'lsteal', None)
        if lv is None:
            return
        src.lsteal = None
        if amount <= 0 or src.illusion or t.illusion or not src.alive or src.dead or not self.targ_ok(src, t, lv['targs']):
            return
        src.life = r32(min(src.maxlife, src.life + r32(amount * lv['A'])))

    def arrow_ok(self, u, code, b, lv, t, manual):
        """frost / searing arrows used on this attack: enabled (autocast on or a manual shot), mana, cooldown, the
        autocast target filter (NOTES_heroes §5; searing arrows: every target, A-SP59)."""
        if not (manual or code in u.arrow_on):
            return False
        sp = self.eng.sp
        if u.mana < lv['Cost'] or sp.cooling(u, code):
            return False
        if manual or b == 'AHfa' or self.is_hero(t) or t.life < ARROW_LIFE:
            return True
        if u.mana < ARROW_MANA:
            return False
        bcode = _id(lv['BuffID'][0]) if lv['BuffID'] else _id('BHca')
        bf = self.find_buff(t, bcode)
        return bf is None or bf.end < 0 or (bf.end - self.eng.now()) <= steps_of(ARROW_LEFT)

    def on_hit_effects(self, u, t, hit, after=True, mk=0):
        """the claimer's buff on the target (melee: before the hit; missiles: at the impact) (§5.3, §5.4); after =
        False at a missile's impact before its damage. mk = the missile kind (0 = a melee / instant hit): the buff
        missiles of the CBuffMissile family (frost, corruption, incinerate) apply before the carried damage and an
        illusion's shot still carries a zero-valued buff (CMissileFrost §5.1, CMissileCorruption §2.1 / §4.3,
        CMissileIncinerate §4.3); the corruption orb of a melee hit and the other orbs keep their own timing."""
        if hit.claim is None or t is None or not t.alive or t.dead:
            return
        code, b, lv = hit.claim
        # PORT8: the launcher snapshots the buff duration (CMissilePoisonAttackSplash §7, CMissileColdArrow §2.1,
        # CMissileCorruption §4.3, CMissileFrost §4): a target that gains a hero state / Resistant Skin in flight
        # does not change the buff already launched. A melee hit has no snapshot (bdur None) and reads it live.
        d = hit.bdur if getattr(hit, 'bdur', None) is not None else self.dur(lv, t)
        ill = u is not None and getattr(u, 'illusion', False)
        if ill and b not in POISON and not (mk and (b in FROST or b in CORRUPTION or b in INCINERATE)):
            return                                              # NOTES_abilities §5.3: an illusion's poison applies with A/B/C = 0
        if b in ORBS:
            if after:
                self.orb_effect(u, t, hit, b, lv)               # NOTES_items §11: melee before the hit, missiles after the damage
            return
        if b in POISON:
            if t.invulnerable:
                return                                              # CMissileSlowPoison §3.1 / PoisonAttackSplash §7: gate before any effect
            bcode, dtype = POISON[b]
            bufs = lv['BuffID'] or [bcode]
            stack = int(lv['D']) & 7
            a, mv, at = (0.0, 0.0, 0.0) if ill else (lv['A'], -lv['B'], -lv['C'])
            if stack:
                self.add_stack_buff(t, _id(bufs[0]), 'poison', u, d, a, mv, at, dtype,
                                    mode=int(lv['D']) if POISON_STACK_MODE else None, slow=(b == 'Aspo'))
            else:
                # CBuffPoisonAttack §6 / 0x6f490f40: the lookup uses the fixed family code, a custom BuffID only
                # feeds creation; a weaker amount refreshes nothing at all (not even the timer), a stronger one
                # replaces the values and only ever extends the remaining duration.
                bf = self.find_buff(t, _id(bcode))
                if bf is not None:
                    if d > 0 and a < bf.dot:
                        return
                    self.unapply(bf)
                    bf.src = u
                    end = self.eng.now() + steps_of(d) if d > 0 else -1
                    if end < 0 or end > bf.end:
                        bf.end = end
                        if end >= 0:
                            self.at(end, 'buff_end', bf, None)
                    bf.dot, bf.move, bf.atk, bf.dtype = a, mv, at, dtype
                    self.apply(bf)
                else:
                    bf, _new = self.add_buff(t, _id(bufs[0]), 'poison', u, d)
                    bf.dot, bf.move, bf.atk, bf.dtype = a, mv, at, dtype
                    self.apply(bf)
                    if _new:
                        self.at(self.eng.now() + steps_of(DOT_FIRST), 'dot', bf, None)
        elif b in FROST:
            if t.invulnerable:
                return                                              # CMissileFrost §5 step 2: dead / invulnerable gate
            # CMissileFrost §5.1: the factors come from Misc (zero for an illusion's shot); a found Bfro is refreshed
            # in place (caster kept, timer only ever extended) and both factors are rewritten
            fm, fa = (0.0, 0.0) if ill else (-self.frost_move, -self.frost_atk)
            bf, _new = self.refresh_extend(t, _id('Bfro'), 'frost', u, d)
            bf.move, bf.atk = fm, fa
            self.apply(bf)
        elif b in CORRUPTION:
            if mk:
                if mk not in (2, 5):
                    return                                          # only the missile / msplash corruption shot (§5.14)
                bcode = _id('BIcb')                                 # CMissileCorruption §4.3: the missile path has no BuffID override
            elif not after:
                return
            else:
                bcode = _id(lv['BuffID'][0]) if lv['BuffID'] else _id('BIcb')
            bf, _new = self.add_buff(t, bcode, 'corruption', u, d)   # refresh: the latest hit wins
            bf.armor = 0.0 if ill else -lv['B']                     # an illusion's shot carries value 0 (§4.3)
            self.apply(bf)
        elif b in INCINERATE:
            if getattr(t, 'is_bldg', False):
                return                                              # CMissileIncinerate §4.1 step 1: structure -> no effect
            self.incinerate_on(t, u, lv)                            # §5.49: before the hit's damage
        elif b == 'AHca':
            if t.invulnerable:
                return                                              # CMissileColdArrow §2.1: dead / invulnerable gate
            bcode = _id(lv['BuffID'][0]) if lv['BuffID'] else _id('BHca')
            bf, _new = self.add_buff(t, bcode, 'arrow', u, d)
            bf.move, bf.atk = -lv['B'], -lv['C']
            self.apply(bf)

    def orb_effect(self, u, t, hit, b, lv):
        """the launch hook of an item / unit orb that claimed the hit (NOTES_items §11.3-§11.5)."""
        if b == 'AIob':
            bf, _new = self.add_buff(t, _id('Bfro'), 'frost', u, self.dur(lv, t))      # Bfro for Dur / HeroDur
            bf.move, bf.atk = -self.frost_move, -self.frost_atk
            self.apply(bf)
        elif b == 'AIfb':
            self.orb_fire(u, t, lv)
        elif b == 'AIpb' and hit.aux is not None:
            sib, slv = hit.aux                                  # the Apo2-class ability of the item: its poison buff
            bufs = slv['BuffID'] or ['Bspo']
            stack = int(slv['D']) & 7
            if stack:
                self.add_stack_buff(t, _id(bufs[0]), 'poison', u, self.dur(slv, t), slv['A'], -slv['B'],
                                    -slv['C'], DMG_SLOW_POISON)
            else:
                nb, _new = self.add_buff(t, _id(bufs[0]), 'poison', u, self.dur(slv, t))
                nb.dot, nb.move, nb.atk, nb.dtype = slv['A'], -slv['B'], -slv['C'], DMG_SLOW_POISON
                self.apply(nb)
                if _new:
                    self.at(self.eng.now() + steps_of(DOT_FIRST), 'dot', nb, None)
        elif b == 'AIsb' and hit.aux is not None:
            cid, clv = hit.aux                                  # Aslo: Bslo, move -DataA, attack -DataB (spells.fx_Aslo)
            bcode = _id(clv['BuffID'][0]) if clv['BuffID'] else _id('Bslo')
            bf, _new = self.add_buff(t, bcode, 'slow', u, self.dur(clv, t))
            bf.move, bf.atk = -clv['A'], -clv['B']
            self.apply(bf)

    def orb_fire(self, u, t, lv):
        """AIfb launch [obs 0x6f573850 -> 0x6f1fb180]: one ring of radius Area around the target, DataA (the cached float)
        of damage type UNIVERSAL (mask 0x4000000, attack type 0) to the targs units except the attacked one; serial order
        (NOTES_items §11.3)."""
        eng = self.eng
        r = lv['Area']
        amt = r32(lv['A'])
        victims = []
        if amt == 0.0:
            return                                              # a ring factor of 0 is skipped (NOTES_combat §3.2)
        for v in sorted((v for v in eng.query(t.x, t.y, r + eng.max_coll) if v.alive and not v.dead),
                        key=lambda v: v.serial):
            dx, dy = v.x - t.x, v.y - t.y
            if v is t or dx * dx + dy * dy > r * r or not self.targ_ok(u, v, lv['targs']):
                continue
            victims.append(v)
        for v in victims:
            if v.alive and not v.dead:
                eng.damage(v, u, amt, 0, DMG_UNIVERSAL, False, False)

    def melee_launch(self, u, t, hit, atk=0):
        """normal / instant attack: crit/bash stun + DataC instance, then the claimer's buff (§5.1, §5.3), then the
        cleaving attacks (launch priority 27, after the claimers' 24 / 25 [inf: a crit at 27 first]) (§5.39)."""
        if hit.crit is not None:
            self.stun(t, u, hit.stun)
            if hit.bonus != 0 and not getattr(u, 'illusion', False):
                self.eng.damage(t, u, r32(hit.bonus), 0, 4, True, False)
        self.on_hit_effects(u, t, hit)
        if hit.claim is not None and hit.claim[1] in FEEDBACK and t.alive and not t.dead:
            self.feedback_melee(u, t, hit, hit.claim[2])           # launch 40 [inf: before a cleave, never together]
        if hit.cleave is not None:
            self.cleave(u, t, hit.amount, atk, hit.cleave)

    def cleave(self, u, t, amount, atk, lv):
        """§5.39 (A-AB13): one ring of radius Area around the point Area ahead of u (its facing), DataA × the amount
        (before armor), ENHANCED with the weapon's attack type, the ability's targs, serial order; not the attacked
        unit nor the attacker; an illusion deals nothing."""
        import math
        if u.illusion or lv['A'] == 0:
            return
        eng = self.eng
        r = lv['Area']
        f = u.facing * math.pi / 180.0
        cx, cy = u.x + r * math.cos(f), u.y + r * math.sin(f)
        amt = r32(amount * lv['A'])
        hits = []
        for v in sorted((v for v in eng.query(cx, cy, r + eng.max_coll) if v.alive and not v.dead),
                        key=lambda v: v.serial):
            dx, dy = v.x - cx, v.y - cy
            if v is t or v is u or dx * dx + dy * dy > r * r or not self.targ_ok(u, v, lv['targs']):
                continue
            hits.append(v)
        for v in hits:
            if v.alive and not v.dead:
                eng.damage(v, u, amt, atk, 5, False, False)

    def missile_launch(self, u, t, hit, tp=0, atk=0):
        """missile: a proc'd crit/bash claims the launch (priority 27) → no orb / poison / frost on it (§3.1).
        Otherwise a weapon of type missile (2) runs every barrage's launch hook (priority 40, returns 0: the default
        missile still flies) (§5.35)."""
        if hit.crit is not None:
            hit.amount = r32(hit.amount + hit.bonus)
            hit.claim = None
            return hit
        if hit.claim is not None and hit.claim[1] in INCINERATE:
            if tp in (3, 8):
                hit.claim = None                                    # §5.49: artillery / aline: no incinerate
            else:
                r = self.defs.get(hit.claim[0]) or {}
                hit.mspeed = float(int(r.get('mspeed', 0) or 0))    # its own missile (JABL packs an integer)
            return hit
        if tp == 2 and hit.claim is not None and hit.claim[1] in FEEDBACK:
            lv = hit.claim[2]                                       # §5.43: a feedback missile replaces the default
            hero = self.is_hero(t)
            if u.illusion:
                hit.fb = (0.0, 0.0)
            else:
                hit.fb = (lv['C'] if hero else lv['A'], lv['D'] if hero else lv['B'])
                if lv['E'] > 0 and self.summoned_or_illusion(t):
                    hit.amount = r32(hit.amount + lv['E'])
            return hit
        if tp in (2, 5):
            orb = hit.claim is not None and hit.claim[1] in ORB_ALL   # an item orb's launch (34) took over
            for code, b, lv in self.abil_list(u):
                if lv is None:
                    continue
                if b in BARRAGE and tp == 2:
                    self.barrage(u, t, hit.amount, atk, lv)
                elif b in LIQUID and not orb and self.targ_ok(u, t, lv['targs']):
                    hit.liq = (lv, u.illusion)                      # §5.42: returns 1 — the later 40s do not run
                    break
        return hit

    def barrage(self, u, t, amount, atk, lv):
        """§5.35 (A-SP66): the targs units within Area of the shooter except the target, by distance (then serial);
        each gets the attack's amount (instant, attack type of weapon 1, NORMAL, ranged) while i ≤ trunc(DataC) (DataC 0
        = no cap) and the DataB budget is > 0 (DataB 0 = no budget); budget' = budget − amount, the hit = budget −
        budget' (float32 both)."""
        eng = self.eng
        r = lv['Area']
        cand = []
        for v in eng.query(u.x, u.y, r + eng.max_coll):
            if v is t or v is u or not v.alive or v.dead:
                continue
            dx, dy = v.x - u.x, v.y - u.y
            d2 = dx * dx + dy * dy
            if d2 <= r * r and self.targ_ok(u, v, lv['targs']):
                cand.append((d2, v.serial, v))
        cand.sort(key=lambda e: (e[0], e[1]))
        cap = int(lv['C'])
        unlimited = lv['B'] == 0
        bud = 1.0 if unlimited else r32(lv['B'])
        for i, (_d2, _s, v) in enumerate(cand):
            if bud <= 0 or (cap != 0 and i > cap):
                break
            a = r32(amount)
            if not unlimited:
                nb = r32(bud - a)
                a = r32(bud - nb)
                bud = nb
            eng.damage(v, u, a, atk, BARRAGE_DMG, True, True)

    def on_impact(self, m, t):
        hit = m.hit_rec
        if hit is None:
            return
        if hit.crit is not None:
            self.stun(t, m.src, hit.stun)
        self.on_hit_effects(m.src, t, hit, after=False, mk=m.kind)

    def corruption_impact(self, m):
        """§5.14: missile / msplash — the item orbs after the damage, the primary target only, if it is still alive.
        The corruption orb itself is applied before the damage in on_impact (CMissileCorruption §2.1)."""
        hit = getattr(m, 'hit_rec', None)
        t = m.target
        if hit is None or hit.claim is None or hit.claim[1] not in ORB_ALL or t is None or m.kind not in (2, 5):
            return
        if hit.claim[1] in CORRUPTION:
            return
        if getattr(m, 'evaded', False):
            return                                                  # an evaded orb missile: Open (§5.14) → no buff
        self.on_hit_effects(m.src, t, hit)

    def liquid_impact(self, m):
        """§5.42 (A-AB16): after the impact's damage the primary target, if alive, gets / refreshes 'Bliq' (the fixed
        class code): the slow-poison body — DataA FIRE per second (0.01 s, then 1 s), move −DataB, attack −DataC, for
        Dur; an illusion's missile carries 0 values; an evaded missile applies nothing (as corruption, §5.14)."""
        hit = getattr(m, 'hit_rec', None)
        t = m.target
        if hit is None or hit.liq is None or t is None or not t.alive or t.dead or getattr(m, 'evaded', False):
            return
        lv, zero = hit.liq
        bf, new = self.add_buff(t, _id('Bliq'), 'poison', m.src, lv['Dur'])
        bf.dot, bf.move, bf.atk = (0.0, 0.0, 0.0) if zero else (lv['A'], -lv['B'], -lv['C'])
        bf.dtype = 8                                                # FIRE (mask 0x100) [obs 0x6f4c7710]
        self.apply(bf)
        if new:
            self.at(self.eng.now() + steps_of(DOT_FIRST), 'dot', bf, None)

    def missed(self, src, t, hit):
        """the miss check (§3.2, 0x6f1f7620): the target's evasion first (the largest chance rolls once on stream 2, a miss
        ends the check), then the attacker's curse (§5.57: the largest chance of its Bcrs buffs rolls once, a miss at
        roll <= chance)."""
        if hit is not None and hit.never_miss:
            return False
        if t is not None and t.abilities:
            best = 0.0
            for code, b, lv in self.abil_list(t):
                if lv is None:
                    continue
                if b in EVADE:
                    c = lv['A']
                elif b in CRIT:
                    c = lv['D']
                else:
                    continue
                if c > best:
                    best = c
            if best > 0 and self.eng.rng.unit() < best:
                return True
        return self.curse_miss(src)

    def curse_miss(self, src):
        """§5.57 (0x6f48cd40 / 0x6f4ec450): the buff with the largest chance (> 0) is asked; it rolls on stream 2 whatever the
        chance and misses at roll <= chance when |chance| >= 0.001 [inf: the constant 0x6f84ca90]."""
        if src is None or not src.buffs:
            return False
        best = 0.0
        for bf in src.buffs:
            c = bf.vals.get('curse', 0.0)
            if bf.alive and c > best:
                best = c
        if best <= 0:
            return False
        roll = self.eng.rng.unit()
        return abs(best) >= 0.001 and roll <= best

    # ------------------------------------------------------------ thorns (§5.15)
    def thorns(self, t, src, mask, ranged):
        """priority 1 notification, before the invulnerability / amount tests: every Aspi of t returns its current
        tier's DataA to a non-ranged NORMAL / DEMOLITION attacker that passes its targs [obs; CUpgradeSpikes §5]."""
        if src is None or ranged or mask not in (0x10, 0x400000):
            return
        for code, b, lv in self.abil_list(t):
            if b not in THORNS or lv is None:
                continue
            base_lv = self.lv_at(code, 1) or lv
            a = float(base_lv['A']) + float(getattr(t, 'upg_spike_amt', 0.0))
            # CAbilitySpiked §5.15 [obs]: the class caches the level-1 DataA; an rspi level adds V to that holder
            # (CUpgradeSpikes §5), so the current ability tier does not change the amount by itself (PORT14).
            if -2e-7 < a < 2e-7 or not self.targ_ok(t, src, lv['targs']):
                continue
            self.eng.damage(src, t, r32(a), 0, 21, False, False)        # DEFENSIVE (0x200000), spells, flags 0
            if not src.alive or src.dead:
                return

    # ------------------------------------------------------------ target-side damage hooks (§3.3)
    def damage_hooks(self, t, src, amount, atk, mask, attack, ranged, dot=False):
        if t.illusion and t.img_taken != 1.0:
            amount = r32(amount * t.img_taken)                      # mirror image: priority 3 (NOTES_spells §1.6)
        for code, b, lv in self.abil_list(t):
            if lv is None:
                continue
            if b in HARDSKIN and mask == 0x10 and attack and not getattr(t, 'illusion', False):
                if (ranged and lv['D'] == 0) or (not ranged and lv['E'] == 0):
                    continue
                if self.prd(t, code, lv['A'], 0x1d):
                    red, mn = lv['C'], lv['B']
                    if red == 0 or amount <= red:
                        amount = r32(mn)
                    else:
                        amount = r32(amount - red)
                    return amount                                   # first claimer ends priority 5
            elif b == 'Apsh':
                # phase shift (NOTES_spells §1.2): enemy damage that is not a DoT tick, autocast on, no cooldown,
                # mana → the unit casts (after the hook loop, A-SP30); the amount is never changed
                if dot or code not in t.auto_on or src is None or self.eng.ally(t.owner, src.owner):
                    continue
                if getattr(t, 'illusion', False) or t.mana < lv['Cost'] or self.eng.sp.cooling(t, code):
                    continue
                if self.phase_pending is None:
                    self.phase_pending = (t, code)
                continue
            elif b == 'ANms':
                bf = self.find_buff(t, _id('BNms'))
                if bf is None:
                    continue
                A, B = bf.vals.get('A', 0.0), bf.vals.get('B', 0.0)
                absorbable = amount * B
                if absorbable < 0.001:
                    continue
                m = t.mana
                cap = m * A if A != 0 else 3.4028234663852886e38
                absorbed = absorbable if absorbable < cap else cap
                amount = r32(max(0.0, amount - absorbed))
                t.mana = r32(m - absorbed / (A if A != 0 else 1.0))
                if t.mana <= 0 or absorbed <= 0:
                    self.remove_buff(bf)                            # the shield switches off (NOTES_spells §1.5)
                return amount
            elif b == 'AIdd':
                # §5.27 defend (item): priority 6 — pierce attacks × DataA, spells / magic attack types × DataE
                if atk == 2 and attack:
                    amount = r32(amount * lv['A'])
                elif atk in (0, 4):
                    amount = r32(amount * lv['E'])
            elif b == 'Adef' and self.has_buff(t, DEF_BUFF):
                # §5.59 defend (active): the AIdd body - pierce attacks × DataA, spells / magic × DataE (priority 6)
                if atk == 2 and attack:
                    amount = r32(amount * lv['A'])
                elif atk in (0, 4):
                    amount = r32(amount * lv['E'])
            elif b in MAGIC_IMMUNE and atk == 4 and src is not None and lv['A'] != 0:
                amount = r32(max(0.0, amount - lv['A']))
                return amount
        return amount

    # ------------------------------------------------------------ auras (§5.6)
    def aura_pass(self, owner, full):
        if not owner.alive or owner.dead:
            return
        eng = self.eng
        first = None
        for code, b, _lv in self.abil_list(owner):
            if b in AURAS:
                first = code
                break
        for code, b, lv in self.abil_list(owner):
            if b not in AURAS or lv is None:
                continue
            r = lv['Area'] if lv['Area'] > 0 else 900.0
            bcode = _id(lv['BuffID'][0]) if lv['BuffID'] else code
            extra = getattr(owner, 'upg_aura', 0.0) if code == first else 0.0   # PORT13 raud: the first aura's cached DataA
            for t in eng.query(owner.x, owner.y, r):
                eng.wk += 1
                if not t.alive or t.dead:
                    continue
                dx, dy = t.x - owner.x, t.y - owner.y
                if dx * dx + dy * dy > r * r:
                    continue
                if t is not owner and not self.targ_ok(owner, t, lv['targs']):
                    continue
                if b == 'Aabr' and (not (t.ucls & 2) or t.life >= t.maxlife):
                    continue                                        # §5.40: damaged undead only (0x7b / 0x1c)
                has = self.find_buff(t, bcode)
                if has is not None and not full:
                    continue
                bf, _new = self.add_buff(t, bcode, 'aura', owner, AURA_LIFE)
                self.aura_values(bf, b, lv, t, extra)
                self.apply(bf)

    def aura_values(self, bf, b, lv, t, extra=0.0):
        A, B, C = lv['A'] + extra, lv['B'], lv['C']
        if b == 'AUau':
            bf.move = A
            bf.regen = B * t.maxlife if C != 0 else B
        elif b == 'AHad':
            bf.armor = A
        elif b == 'AOae':
            bf.move, bf.atk = A, B
        elif b == 'Aoar':
            bf.regen = A * t.maxlife if B != 0 else A
        elif b == 'Aabr':
            bf.regen = 0.0 if getattr(bf.src, 'illusion', False) else (A * t.maxlife if B != 0 else A)   # §5.40
        elif b == 'AEar' or b == 'AOac':                           # AOac = the AEar class (§5.51, A-AB20)
            bf.vals['dmg'] = A                                      # used via unit dmg_bonus_f (recomputed)
        elif b == 'Aasl':
            bf.move = A                                             # tornado slow aura: move factor DataA (< 0)
        bf.aura = True

    # ------------------------------------------------------------ invisibility / detection (§5.9)
    def reveal(self, owner, x, y, r, kind, dur):
        """§5.44: a point detector (owner, x, y, radius, end step); its end is a timer (U#: the visibility cache)."""
        if not (kind & 1):
            return                                                  # burrowed only: nothing in the engine
        rv = [owner, x, y, r, self.eng.now() + steps_of(dur)]
        self.reveals.append(rv)
        self.at(rv[4], 'reveal', rv, None)

    def visible_to(self, viewer, t):
        if not getattr(t, 'invisible', 0):
            return True
        eng = self.eng
        for o, x, y, r, end in self.reveals:
            if o is viewer.owner:                                   # per-player counters: the owner only (A-AB18)
                dx, dy = t.x - x, t.y - y
                if dx * dx + dy * dy <= r * r:
                    return True
        for d in self.detectors:
            if not d.alive or d.dead or not eng.ally(viewer.owner, d.owner) and d.owner is not viewer.owner:
                continue
            for code, b, lv in self.abil_list(d):
                if b in DETECT and lv is not None:
                    rec = self.rec(code)
                    # CAbilityDetector §3.1/§3.6: the radius is Rng of level 1 (captured when the detector is
                    # armed), never Area or the current level; DataA of the current level is the kind mask.
                    r = rec['lv'][0]['Rng'] if rec and rec['lv'] else 0.0
                    kind = int(lv['A'])
                    if kind != 0 and not (kind & 1):
                        continue                                    # not an invisible-kind detector
                    dx, dy = t.x - d.x, t.y - d.y
                    if dx * dx + dy * dy <= r * r:
                        return True
        return False

    def revealed_to(self, pl, t):
        """PORT5-NativesB: the detection half of visible_to for a player (IsUnitDetected / IsUnitInvisible): a point reveal
        of the player or a detector of the player's team covers t [inf: the engine reads two per-player counter arrays,
        0031 IsUnitDetected, whose writers are the detectors]. Same loops as visible_to."""
        eng = self.eng
        for o, x, y, r, end in self.reveals:
            if o is pl:
                dx, dy = t.x - x, t.y - y
                if dx * dx + dy * dy <= r * r:
                    return True
        for d in self.detectors:
            if not d.alive or d.dead or not eng.ally(pl, d.owner) and d.owner is not pl:
                continue
            for code, b, lv in self.abil_list(d):
                if b in DETECT and lv is not None:
                    rec = self.rec(code)
                    r = rec['lv'][0]['Rng'] if rec and rec['lv'] else 0.0
                    kind = int(lv['A'])
                    if kind != 0 and not (kind & 1):
                        continue
                    dx, dy = t.x - d.x, t.y - d.y
                    if dx * dx + dy * dy <= r * r:
                        return True
        return False

    def buffs_match(self, u, pos, neg, magic, phys, timed, aura, auto):
        """the unit's buffs that pass the seven include flags, in list order (UnitHasBuffsEx / UnitCountBuffsEx)."""
        return [bf for bf in u.buffs if bf.alive and buff_match(buff_class(bf), pos, neg, magic, phys, timed, aura, auto)]

    def remove_buffs_ex(self, u, pos, neg, magic, phys, timed, aura, auto):
        """UnitRemoveBuffs(Ex) (0033 §3 [obs 0x6f48b990]): every matching buff leaves through the plain buff removal;
        -> the count (the native throws it away)."""
        n = 0
        for bf in list(u.buffs):
            if bf.alive and buff_match(buff_class(bf), pos, neg, magic, phys, timed, aura, auto):
                self.remove_buff(bf)
                n += 1
        return n

    # ------------------------------------------------------------ wander (§5.10)
    def wander_pending(self, u):
        """p8 §9.4 step 1 (0x6f4b9080): the wander ability is busy when a wander timer is still queued."""
        for _step, _seq, kind, a, _b in self.timers:
            if kind == 'wander' and a is u:
                return True
        return False

    def schedule_wander(self, u):
        if self.wander_pending(u):
            return                                          # p8 §9.4: one wander timer per unit (busy test)
        p = self.N.streams.jass.unit() * 5.0 + 5.0
        self.at(self.eng.now() + steps_of(p), 'wander', u, None)

    def immolation(self, u, code):
        """§2.3: DataA FIRE to the targs units within Area (serial order, A-SP36); no tick while dead or cycloned."""
        if not u.alive or code not in u.abilities:
            return
        lv = self.lv(u, code)
        eng = self.eng
        self.immolation_damage(u, lv)
        if u.alive and not u.dead:
            self.at(eng.now() + steps_of(lv['Dur'] if lv['Dur'] > 0 else 1.0), 'immo', u, code)

    # ------------------------------------------------------------ lightning shield (§5.22)
    def shield_on(self, t, src, lv):
        """Blsh on t: new → first tick 0.01 s; a refresh replaces the values and re-arms the tick 0.5 s later (the
        older timer is void: its token no longer matches) [obs]."""
        bcode = _id(lv['BuffID'][0]) if lv['BuffID'] else _id('Blsh')
        bf, new = self.add_buff(t, bcode, 'lshield', src, self.dur(lv, t))
        bf.vals['A'], bf.vals['area'] = lv['A'], lv['Area']
        bf.vals['tok'] = bf.vals.get('tok', 0) + 1
        self.apply(bf)
        self.at(self.eng.now() + steps_of(SHIELD_FIRST if new else SHIELD_TICK), 'lsh', bf, bf.vals['tok'])

    def shield_tick(self, bf, tok):
        """the next tick is armed first, then: every ground unit within Area of the shielded unit, but not the unit
        itself and not one with Blsa (0.5 s marker, set before its damage), takes DataA/2 LIGHTNING from the caster
        (attack type 0) [obs; the marker / next-tick order: A-SP46]."""
        if not bf.alive or bf.vals.get('tok') != tok:
            return
        t, eng = bf.unit, self.eng
        if bf.end >= 0 and eng.now() >= bf.end:
            return
        self.at(eng.now() + steps_of(SHIELD_TICK), 'lsh', bf, tok)
        if not t.alive or t.dead or self.has_buff(t, _id('Bcyc')):
            return
        r = bf.vals['area']
        src = bf.src if bf.src is not None and bf.src.alive else bf.src
        for v in sorted((v for v in eng.query(t.x, t.y, r + eng.max_coll) if v.alive and not v.dead),
                        key=lambda v: v.serial):
            dx, dy = v.x - t.x, v.y - t.y
            if v is t or dx * dx + dy * dy > r * r or eng.flying(v) or self.has_buff(v, _id('Blsa')):
                continue
            mk, _n = self.add_buff(v, _id('Blsa'), 'marker', src, SHIELD_TICK)
            if v.alive and not v.dead:
                eng.damage(v, src, r32(bf.vals['A'] / 2.0), 0, 10, False, False)

    # ------------------------------------------------------------ pulverize (§5.25)
    def pulverize(self, u, lv):
        """after the armed unit's melee hit dealt its damage: one splash around the attacker, DataB within DataC, DataB/2
        within DataD (smallest ring wins), ENHANCED, attack type 0, the ability's targs, serial order; an illusion
        deals nothing; the attacker itself is not hit (A-SP49)."""
        if u.illusion or not u.alive or u.dead:
            return
        eng = self.eng
        rings = sorted([r for r in ((lv['C'], 1.0), (lv['D'], 0.5)) if r[0] > 0], key=lambda r: r[0])
        if not rings:
            return
        rmax = rings[-1][0]
        x, y = u.x, u.y
        hits = []
        for v in sorted((v for v in eng.query(x, y, rmax + eng.max_coll) if v.alive and not v.dead),
                        key=lambda v: v.serial):
            dx, dy = v.x - x, v.y - y
            d2 = dx * dx + dy * dy
            if v is u or d2 > rmax * rmax or not self.targ_ok(u, v, lv['targs']):
                continue
            for r, f in rings:
                if d2 <= r * r:
                    hits.append((v, f))
                    break
        for v, f in hits:
            if v.alive and not v.dead:
                eng.damage(v, u, r32(lv['B'] * f), 0, 5, False, False)

    # ------------------------------------------------------------ purge (§5.23)
    def purge(self, u, t, lv):
        """step 1: affected (an illusion, or not magic immune) → DataC MAGIC to an enemy summoned unit / illusion
        without Bams, else an event-only record for an enemy; then every magic buff goes (PURGE_KINDS, any alliance),
        mana − DataF. Step 2 (alive enemies): a new Bprg (an old one removed), duration + 0.01 s, move factor −1.0
        if DataA (attack −1.0 if DataB), recovering by 1/DataA (1/DataB) every duration/DataA s (A-SP48)."""
        eng = self.eng
        enemy = not eng.ally(u.owner, t.owner)
        illu = bool(getattr(t, 'illusion', False))
        if not illu and t.magic_immune > 0:
            if enemy:
                eng.damaged_event(t, u, 0.0)
            return
        bams = self.has_buff(t, _id('Bams'))
        if enemy:
            if (t.summoned or (t.ucls & 4) or illu) and not bams:
                eng.damage(t, u, r32(lv['C']), 0, 14, False, False)
            else:
                eng.damaged_event(t, u, 0.0)
        for bf in list(t.buffs):
            if bf.kind in PURGE_KINDS:
                self.remove_buff(bf)
        if not bams and lv['F'] != 0:
            t.mana = r32(max(0.0, t.mana - lv['F']))
        if not enemy or not t.alive or t.dead:
            return
        bcode = _id(lv['BuffID'][0]) if lv['BuffID'] else _id('Bprg')
        self.remove_buff_code(t, bcode)
        d = self.dur(lv, t)
        bf, _new = self.add_buff(t, bcode, 'purge', u, d + 0.01)
        if lv['A'] != 0:
            bf.move = -1.0
        if lv['B'] != 0:
            bf.atk = -1.0
        self.apply(bf)
        if lv['A'] != 0:
            bf.vals['ma'], bf.vals['mp'] = 1.0 / lv['A'], d / lv['A']
            self.at(eng.now() + steps_of(bf.vals['mp']), 'prg', bf, 0)
        if lv['B'] != 0:
            bf.vals['ab'], bf.vals['ap'] = 1.0 / lv['B'], d / lv['B']
            self.at(eng.now() + steps_of(bf.vals['ap']), 'prg', bf, 1)

    def purge_tick(self, bf, which):
        """Bprg recovery step (0 move, 1 attack): while the remaining slow is ≥ 0.001 the factor rises by its step and
        the timer repeats [obs 0xd01c1 / 0xd01c3]."""
        if not bf.alive:
            return
        u, eng = bf.unit, self.eng
        if which == 0:
            if -bf.move < 0.001:
                return
            st = bf.vals['ma']
            bf.move += st
            u.move_delta += st
            eng.speed_changed(u)
            per = bf.vals['mp']
        else:
            if -bf.atk < 0.001:
                return
            st = bf.vals['ab']
            bf.atk += st
            eng.set_attack_factor(u, u.atk_delta + st)
            per = bf.vals['ap']
        self.at(eng.now() + steps_of(per), 'prg', bf, which)

    def immolation_on(self, u, code):
        """AEim switched on (NOTES_spells §2.3): its caster buff (BuffID[0], else BEim) until switched off."""
        lv = self.lv(u, code)
        if lv is None:
            return
        bcode = _id(lv['BuffID'][0]) if lv['BuffID'] else _id('BEim')
        if self.find_buff(u, bcode) is not None:
            return
        bf, _new = self.add_buff(u, bcode, 'immo_on', u, 0.0)
        self.apply(bf)
        self.at(self.eng.now() + steps_of(lv['Dur'] if lv['Dur'] > 0 else 1.0), 'immo_a', u, code)

    def immolation_off(self, u):
        """unimmolation: every AEim caster buff goes (instant, no cast sequence, as manashieldoff A-SP25)."""
        for bf in list(u.buffs):
            if bf.kind == 'immo_on':
                self.remove_buff(bf)

    def immolation_active(self, u, code):
        """AEim tick while on: DataB × Dur mana drained (A-SP44, regen modifier [obs] as a per-tick charge [inf]),
        then the area damage as the permanent ones; stops when switched off or the ability is gone."""
        if not u.alive or code not in u.abilities:
            return
        lv = self.lv(u, code)
        bcode = _id(lv['BuffID'][0]) if lv['BuffID'] else _id('BEim')
        bf = self.find_buff(u, bcode)
        if bf is None or bf.kind != 'immo_on':
            return
        per = lv['Dur'] if lv['Dur'] > 0 else 1.0
        if lv['B'] != 0:
            u.mana = r32(max(0.0, u.mana - lv['B'] * per))
        self.immolation_damage(u, lv)
        if u.alive and not u.dead:
            self.at(self.eng.now() + steps_of(per), 'immo_a', u, code)

    def immolation_damage(self, u, lv):
        eng = self.eng
        if not u.dead and not self.has_buff(u, _id('Bcyc')):
            r = lv['Area']
            for v in sorted((v for v in eng.query(u.x, u.y, r + eng.max_coll) if v.alive and not v.dead),
                            key=lambda v: v.serial):
                dx, dy = v.x - u.x, v.y - u.y
                if v is u or dx * dx + dy * dy > r * r or not self.targ_ok(u, v, lv['targs']):
                    continue
                if v.alive and not v.dead:
                    eng.damage(v, u, r32(lv['A']), 0, 8, False, False)

    def twander(self, u, code):
        """tornado wander (A-SP20): every Dur s a move to a random point within Area (game stream 8, as wander)."""
        if not u.alive or u.dead or code not in u.abilities:
            return
        lv = self.lv(u, code)
        eng = self.eng
        if u.atk_target is None and not u.paused:
            g = eng.streams.game[8]
            R = lv['Area'] if lv['Area'] > 0 else 250.0
            x = r32(u.x + (g.unit() * 2 * R - R))
            y = r32(u.y + (g.unit() * 2 * R - R))
            eng.issue(u, 0xd0012, x, y, None)
        self.at(eng.now() + steps_of(lv['Dur'] if lv['Dur'] > 0 else 2.0), 'twander', u, code)

    def wander(self, u):
        if not u.alive or u.dead or not any(self.base(c) in WANDER for c in u.abilities):
            return
        eng = self.eng
        if u.order == 0 and u.atk_target is None and not u.paused:
            g = eng.streams.game[8]
            R = 250.0
            x = r32(u.x + (g.unit() * 2 * R - R))
            y = r32(u.y + (g.unit() * 2 * R - R))
            eng.issue(u, 0xd0012, x, y, None)                      # move, no order events (A-AB7)
        self.schedule_wander(u)


def _misc(misc, key, default):
    try:
        return float(misc.get(key, default))
    except (TypeError, ValueError):
        return default
