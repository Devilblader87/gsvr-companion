"""W8 spell casting for the reference engine (docs/NOTES_production.md §5; clean-room: behaviour only).

A spell order (an order id that one of the unit's abilities answers to) runs the CAbilitySpell sequence [obs §5.2]:

  order      can-cast check (§5.1: mana >= Cost, cooldown not running, the class's own check) else rejected;
             unit target / point must be valid; the caster walks into range Rng(level) first (A-SP3)
  CHANNEL    SPELL_CHANNEL (player-unit registrations first, then the unit's); wait ability Cast seconds
  CAST       SPELL_CAST; wait the caster's castpt (not for instant abilities)
  EFFECT     SPELL_EFFECT fires first; then mana -= min(Cost, mana), the class effect (caster alive and still on
             this order), the cooldown Cool starts — a SPELL_EFFECT trigger sees the mana before payment, no cooldown
  backswing  castbsw seconds (not instant), then
  END        SPELL_FINISH, SPELL_ENDCAST
  interrupt  a new order / stop between CHANNEL and the end: SPELL_ENDCAST only (no FINISH) [obs]; it fires after the
             new order is set up (flush(), A-SP7), then the new cast's CHANNEL, then the order event (natives)

Classes the engine implements live in CLASSES (base code -> target kind + flags); other abilities do not answer
orders (the order is rejected, as before W8). Effects of a class are methods `fx_<code>`.
Assumptions A-SP* are listed in docs/engine_w5.md (W8 section).
"""
import math
import types

from .orders import ORDER_IDS
from .abilities import NEGATIVE_KINDS, AVUL_ID, steps_of as ab_steps
from .vm import r32
from . import itemdata as ID

SPS = 200

T_NONE, T_UNIT, T_POINT = 0, 1, 2
PH_MOVE, PH_CHANNEL, PH_CAST, PH_BACKSWING, PH_EFFECT, PH_CHANNELING = 0, 1, 2, 3, 4, 5

# base code -> (target kind, instant). AAns: the neutral spell (Blizzard TD sell / buy buttons), instant, no target
# [obs 0x6f45b210: SPELL_EFFECT then the charge]; its order is DataE (an order string) [ext data].
CLASSES = {
    'AAns': (T_NONE, True),
    'AHtb': (T_UNIT, False),        # thunder bolt: homing missile, DataA + stun (NOTES_heroes §4.2)
    'AUfn': (T_UNIT, False),        # frost nova: DataA area + DataB target, COLD, Bfro (§4.3)
    'AEer': (T_UNIT, False),        # entangling roots: BEer, DataA / s, held (§4.4)
    'AUsl': (T_UNIT, False),        # sleep: BUsl, held (§4.7)
    'AEbl': (T_POINT, False),       # blink: the caster moves to the point, DataA max / DataB min range (§4.8)
    'AOeq': (T_POINT, False),       # earthquake: channel; units slowed (BOeq), structures DataB / s (§4.5)
    'AUdd': (T_POINT, False),       # death and decay: channel; max life × DataA per second (§4.6)
    'ANto': (T_POINT, False),       # tornado: summons UnitID at the point for Dur (§4.9)
    'AHds': (T_NONE, True),         # divine shield: BHds invulnerable + magic immune (NOTES_spells §1.1)
    'AHav': (T_NONE, False),        # avatar: BHav armor + A, life + B, damage + trunc(C), magic immune (§1.4)
    'ANms': (T_NONE, True),         # mana shield on (Orderon) / off (Orderoff): damage absorption (§1.5)
    'Apsh': (T_NONE, True),         # phase shift: Bpsh hidden + untargetable for Dur, cast ends at Dur + 0.1 (§1.2)
    'AOmi': (T_NONE, False),        # mirror image: trunc(DataA) images, deal × DataB, take × DataC (§1.6)
    'AOvd': (T_NONE, False),        # big bad voodoo: channel; allies in Area invulnerable (§1.7)
    'ANef': (T_NONE, False),        # elemental fury: the caster is taken out, DataA's units summoned (§1.8)
    'ANfb': (T_UNIT, False),        # fire bolt: the thunder bolt effect with FIRE (§3.2)
    'AOcl': (T_UNIT, False),        # chain lightning: DataB targets, × (1 − DataC) per hop, closest next (§3.3)
    'AEfk': (T_NONE, False),        # fan of knives: DataA per unit in Area, budget DataB, cap DataC (§3.1)
    'AOws': (T_NONE, False),        # war stomp: DataA SONIC + stun to the units in Area (§3.6)
    'ANbr': (T_NONE, False),        # battle roar: BNbr +DataA damage to the units in Area (§3.7)
    'ANsw': (T_NONE, False),        # summon war eagle: trunc(DataA) UnitID, timed life Dur (§3.8)
    'AOsf': (T_NONE, False),        # spirit wolf: trunc(DataB) UnitID, timed life Dur / HeroDur (§3.9)
    # W8 item abilities (NOTES_items §4.5.1, §8, §9)
    'AIbl': (T_POINT, False),       # item build: a structure under construction for Dur (min 0.1 s) at the point
    'ANfd': (T_UNIT, False),        # finger of death: DataA MAGIC at the effect
    'Absk': (T_NONE, True),         # berserk: events only, the buff is not modelled (A-IT9)
    # docs/td_plan.md T1: trigger buttons (the map script reacts to their spell events); the own effect of these
    # classes is not modelled yet (A-SP41) — cost, cooldown and the event sequence are
    'ANcl': (T_NONE, False),        # channel: order DataF [obs], target kind DataB, follow-through DataA (A-SP42)
    'AOwk': (T_NONE, True),         # wind walk: BOwk invisibility + move DataB for Dur′ (NOTES_spells §5.19, A-SP78)
    'Aroa': (T_NONE, False),        # roar: §3.7 body, Broa +DataA × avg damage, armor B, regen C (NOTES_spells §5.1)
    'AEtq': (T_NONE, False),        # tranquility (channel Dur)
    'AUcs': (T_POINT, False),       # carrion swarm: wave over DataC, 0.1 s sweeps, min(A, budget/n) SONIC (§5.2, A-SP57)
    'Aivs': (T_UNIT, False),        # invisibility: Binv, the invisibility counter for Dur′ (§5.10, A-SP54)
    'AUin': (T_POINT, False),       # inferno: the §3.4 drop at the point, DataA FORCE + BPSE after DataC (§5.8, A-SP52)
    'AOwd': (T_POINT, False),       # serpent ward: trunc(DataA) UnitID at the point, timed life Dur (NOTES_spells §5.9)
    'ACtc': (T_NONE, False),        # creep thunder clap: DataA SONIC in Area + BCtc slow −C / −D (NOTES_spells §5.5)
    'AHtc': (T_NONE, False),        # hero thunder clap = the ACtc body [inf, BUGS W9-3-GEM1: Gem TD's "Keep" A009]; packed as ACtc
    'AEsf': (T_NONE, False),        # starfall: the monsoon drains at the caster for the channel Dur (NOTES_spells §5.6, A-SP74)
    'AEsh': (T_UNIT, False),        # shadow strike: missile DataE, BEsh DoT DataA every Cast s + decaying slow (§5.3)
    'ANsi': (T_POINT, False),       # silence: BNsi in Area, DataA bits 1/2/4 attacks, 8 spells (§5.4, A-SP55)
    'Aslo': (T_UNIT, False),        # slow: Bslo move −DataA, attack −DataB (NOTES_abilities §5.12; autocast A-SP43)
    'AEim': (T_NONE, True),         # immolation on (0xd00d1) / off (0xd00d2), NOTES_spells §2.3 (A-SP44)
    # NOTES_abilities §5.19-5.24 (td_plan T2); autocast of Ablo / Afae / Afzy not modelled (A-SP45)
    'AHhb': (T_UNIT, False),        # holy light: heal a friendly living unit DataA / DataA/2 DIVINE to enemy undead
    'Ablo': (T_UNIT, False),        # bloodlust: attack speed +DataA, move +DataB
    'Afzy': (T_NONE, False),        # frenzy: the same buff on the caster
    'Afae': (T_UNIT, False),        # faerie fire: armor −DataA (§5.18)
    'Alsh': (T_UNIT, False),        # lightning shield: DataA/2 LIGHTNING every 0.5 s around the target (§5.22)
    'Aens': (T_UNIT, False),        # ensnare: homing missile, Bena (air) / Beng held for Dur (§5.17, A-SP47)
    'Aprg': (T_UNIT, False),        # purge: magic buffs off, DataC MAGIC to enemy summons, Bprg slow (§5.23, A-SP48)
    'AIhe': (T_NONE, False),        # item heal: the carrier + DataA, refused at full life (§5.27, A-SP50)
    'AIha': (T_NONE, False),        # item heal aoe: + DataA to every targs unit within Area (§5.27, A-SP50)
    # Wintermaul (td_score): creep storm bolt = the thunder bolt class [obs vtable diff], cripple (§5.28, A-SP58)
    'ACtb': (T_UNIT, False),        # creep storm bolt: the AHtb missile, DataA SONIC + stun Dur′ (NOTES_spells §5.12)
    'Acri': (T_UNIT, False),        # cripple: Bcri move −DataA, attack −DataB, damage −trunc(C·avg + 0.5)
    'Arai': (T_UNIT, False),        # raise dead: a corpse target, trunc(A) DataC + trunc(B) DataD summons (A-SP60)
    'AUim': (T_POINT, False),       # impale: Area/2 circles along the line, BUim air DataD → C FORCE + stun (A-SP61)
    'Aply': (T_UNIT, False),        # polymorph: Bply, the DataB…E type by movement type for Dur′ (§5.31, A-SP62)
    'AIrg': (T_UNIT, False),        # item regeneration potion: BIrg / BIrl / BIrm for Dur′ + 0.01, A / Dur′ life + B / Dur′ mana per s (NOTES_items §10, W9-4); T_NONE when DataD (no target required) is set
    'AIma': (T_NONE, False),        # item mana restore: the caster + DataA mana, refused at full mana (§5.32, A-SP63)
    'AIre': (T_NONE, False),        # item restore: the carrier + DataA life and DataB mana, refused at full life AND mana (CAbilityItemRestore §2-§3)
    'AIra': (T_NONE, False),        # item restore aoe: DataA life + DataB mana to every targs unit within Area (CAbilityItemRestoreAoe §5)
    'AImr': (T_NONE, False),        # item mana restore aoe: + DataA mana to every targs unit within Area (CAbilityItemManaRestoreAoe, NOTES_items §9 [inf amount])
    'AIwb': (T_UNIT, False),        # item web: the Aweb launch (CAbilityItemWeb §3); packed as Aweb (AB_ALIAS)
    'AIrv': (T_POINT, False),       # item reveal map: the AIta point detector, Dur + 0.1 s (CAbilityItemRevealMap §2) [inf: not instant]
    'AOhw': (T_UNIT, False),        # healing wave: the AOcl chain healing, next = the most damaged (§5.33, A-SP64)
    'Arst': (T_UNIT, False),        # restoration = the repair class: life rate + resource ticks (§5.36, A-SP67)
    'Arep': (T_UNIT, False),        # repair (Ahrp): the same class, other order ids [obs vtable diff] (§5.36)
    'AIta': (T_POINT, True),        # reveal (AHta): a point detector of the owner for Dur + 0.1 s (§5.44, A-AB18)
    'Ainf': (T_UNIT, False),        # inner fire: Binf = the roar stat buff, damage %, armor B, regen D (§5.45, A-SP68)
    'Auhf': (T_UNIT, False),        # unholy frenzy: Buhf attack rate + A, B DEATH per second (§5.46, A-SP69)
    'ANab': (T_UNIT, False),        # acid bomb: missile, BNab armor −C, DoT D / E every F s in Area (§5.47, A-SP70)
    'ANfl': (T_UNIT, False),        # forked lightning: a widening line of circles, the trunc(B) closest (§5.48, A-SP71)
    'Aphx': (T_NONE, False),        # phoenix: the death veto casts the morph to UnitID and back after HeroDur (§5.50, A-SP72)
    'AHfs': (T_POINT, False),       # flame strike: Cast s after EFFECT an area for Dur, burning members (NOTES_spells §5.14)
    'ANmo': (T_POINT, False),       # monsoon: per-member drains strike DataA UNIVERSAL every ≈ DataB + 0.1 s (§3.5, A-SP74)
    'AOsh': (T_POINT, False),       # shockwave: the AUcs wave, own order (NOTES_spells §5.17, A-SP75)
    'ANbf': (T_POINT, False),       # breath of fire: the AUcs wave; the BNbf DoT only under drunken haze (§5.17, A-SP75)
    'AHbz': (T_POINT, False),       # blizzard: trunc(A) waves of B COLD in Area every Cast, first 0.9 s (§5.20, A-SP79)
    'ANrf': (T_POINT, False),       # rain of fire: the blizzard waves, FIRE, + the burn E / s for Dur (§5.20, A-SP79)
    'ANvc': (T_POINT, False),       # volcano: trunc(B) waves of ≤ 32 rocks in trunc(A) rings, E / E·F, × D, stun (§5.15, A-SP82)
    'ANst': (T_POINT, False),       # stampede: beasts on 5 lanes through the caster, DataC UNIVERSAL in D (§5.18, A-SP77)
    'Aweb': (T_UNIT, False),        # web: the ensnare missile, Bwea (air) / Bweb held for Dur (NOTES_abilities §5.53, A-SP76)
    'Arpl': (T_NONE, False),        # replenish life: + DataA life to ≤ trunc(Cast) units in Area, Cost × n (NOTES_abilities §5.55, A-SP81)
    'Arpm': (T_NONE, False),        # replenish mana: the same class, + DataB mana (§5.55, A-SP81)
    'Arej': (T_UNIT, False),        # rejuvenation: Brej for Dur′ + 0.01, every 1 s + A / Dur′ life, + B / Dur′ mana (§5.28, A-SP80)
    'AUls': (T_NONE, False),        # locust swarm: ≤ 40 UnitID locusts bite, return heal D × damage, Dur (NOTES_spells §5.16, A-SP83)
    'Asds': (T_POINT, False),       # kaboom: rings (A, B) / (C, D) NORMAL at the caster after Dur, the caster dies (§5.52, A-AB21)
    # W9R3-2 (NOTES_abilities §5.57 .. §5.60)
    'Acrs': (T_UNIT, False),        # curse: Bcrs for Dur′, the cursed attacker misses at a roll <= DataA; curseon / curseoff (§5.57)
    'ANht': (T_NONE, False),        # howl of terror: the roar body with BNht, damage - DataA × avg, armor - DataB (§5.58)
    'Adef': (T_NONE, True),         # defend: a toggle (defend / undefend, no cast sequence), Bdef = the active flag (§5.59)
    'Acan': (T_UNIT, False),        # cannibalize: a corpse target, + DataA life per second for Dur′ while the order lasts (§5.60)
    # PORT7-RestGameplay: CAbilityFigurine_p1 (+ the six figurine subclasses); AbilityData gives every figurine item the code AIfs
    'AIfs': (T_NONE, False),        # figurine: trunc(DataA) DataC + trunc(DataB) DataD summons around the carrier, timed life Dur′
}
# §5.19 [obs]: the order id is the class's (vtable +0x308) — the answer when the profile has no Order= line
CLASS_ORDER = {'AHhb': 0xd007c, 'Ablo': 0xd0085, 'Afzy': 0xd0251, 'Alsh': 0xd008e, 'Aprg': 0xd008f, 'Afae': 0xd00b5,
               'Acri': 0xd00dd, 'Arej': 0xd00c0, 'ANvc': 0xd02bd, 'AUls': 0xd024c, 'Aens': 0xd008a, 'Aply': 0xd006a, 'Arst': 0xd00ea,
               'Arep': 0xd0038,     # NOTES_abilities §5.36 [obs 0x6f480600]
               'Arpl': 0xd0241, 'Arpm': 0xd0244,   # §5.55: replenishlife [obs 0x6f539e00]; replenishmana [inf: the order table]
               'AIta': 0xd012e,     # NOTES_abilities §5.44 [obs 0x6f44cf40]
               'Ainf': 0xd0062, 'Auhf': 0xd00f1,   # §5.45 / §5.46 [obs 0x6f5ba820, 0x6f4c6490]
               'ANab': 0xd02b6,     # §5.47 [obs 0x6f607380]
               'ANfl': 0xd026b,     # §5.48 [obs 0x6f511ba0]
               'Aphx': 0xd0202,     # §5.50 phoenixmorph [obs]
               'AHfs': 0xd0208,     # NOTES_spells §5.14 flamestrike
               'ANmo': 0xd026f,     # NOTES_spells §3.5 monsoon [obs]
               'AOsh': 0xd009d, 'ANbf': 0xd0264,   # NOTES_spells §5.17 [obs 0x6f540cc0, 0x6f617140]
               'AOwk': 0xd00a1,     # NOTES_spells §5.19 windwalk [obs]
               'Asds': 0xd0049,     # NOTES_abilities §5.52 selfdestruct [obs 0x6f53bf60]
               'Acrs': 0xd00de,     # NOTES_abilities §5.57 curse [obs 0x6f50af10]; curseon 0xd00df / curseoff 0xd00e0 [obs 0x6f50aef0 / 0x6f50af00]
               'ANht': 0xd026c,     # §5.58 howlofterror [obs 0x6f515da0]
               'Adef': 0xd0057,     # §5.59 defend [obs 0x6f500310]; undefend 0xd0058 [obs 0x6f500320]
               'Acan': 0xd00dc,     # §5.60 cannibalize [obs 0x6f6183e0]
               'AIma': 0xd0134,     # NOTES_abilities §5.32 [obs 0x6f5c0ad0]
               # CAbilityItem* class order ids [obs, getters in the chapters]: heal 0xd0130, heal aoe 0xd0131, mana restore aoe 0xd0135,
               # restore 0xd0139, restore aoe 0xd013a, reveal map 0xd0284
               'AIhe': 0xd0130, 'AIha': 0xd0131, 'AImr': 0xd0135, 'AIre': 0xd0139, 'AIra': 0xd013a, 'AIrv': 0xd0284,
               'AOhw': 0xd0215,     # NOTES_abilities §5.33 [obs 0x6f514090]
               # NOTES_spells §5 [obs]
               'Aroa': 0xd00c4, 'AUcs': 0xd00fa, 'AEsh': 0xd022f, 'ANsi': 0xd0270, 'ACtc': 0xd011d, 'AEsf': 0xd00d7,
               'AEtq': 0xd00d8, 'AUin': 0xd0100, 'AOwd': 0xd0218, 'Aivs': 0xd0065}
USE_SLOT0 = 852008                  # useslot 852008 + slot (orders.py)
CURSE_ON, CURSE_OFF = ORDER_IDS['curseon'], ORDER_IDS['curseoff']       # §5.57: 0xd00df / 0xd00e0
DEFEND_ON, DEFEND_OFF = ORDER_IDS['defend'], ORDER_IDS['undefend']       # §5.59: 0xd0057 / 0xd0058
CORPSE_TARGET = ('Arai', 'Acan')    # classes whose unit target is a corpse
RPL_TOGGLE = {ORDER_IDS['replenishlifeon']: ('Arpl', True, ORDER_IDS['replenishlife']),
              ORDER_IDS['replenishlifeoff']: ('Arpl', False, ORDER_IDS['replenishlife']),
              ORDER_IDS['replenishmanaon']: ('Arpm', True, ORDER_IDS['replenishmana']),
              ORDER_IDS['replenishmanaoff']: ('Arpm', False, ORDER_IDS['replenishmana'])}   # §5.55 on / off
ARROW_ORDERS = (ORDER_IDS['coldarrowstarg'], ORDER_IDS['coldarrows'], ORDER_IDS['uncoldarrows'],
                ORDER_IDS['flamingarrowstarg'], ORDER_IDS['flamingarrows'], ORDER_IDS['unflamingarrows'])
ARROW_BASE = {ORDER_IDS['coldarrowstarg']: 'AHca', ORDER_IDS['coldarrows']: 'AHca', ORDER_IDS['uncoldarrows']: 'AHca',
              ORDER_IDS['flamingarrowstarg']: 'AHfa', ORDER_IDS['flamingarrows']: 'AHfa',
              ORDER_IDS['unflamingarrows']: 'AHfa'}     # searing arrows 0xd00cd…0xd00cf (NOTES_heroes §4.11)
BUTTONS = {'ANcl', 'AEtq'}  # T1, no effect yet
CHANNEL = {'AOeq', 'AUdd', 'AOvd', 'ANto', 'AEtq', 'AEsf', 'Aphx', 'ANmo', 'ANst'}   # Aphx: the morph time Dur (§5.50)  # the caster channels for Dur after the EFFECT (A-SP14)
AREA_TICK = 1.0                     # DnD [obs 1.0 s]; earthquake tick interval Open → 1.0 s (A-SP15)
VOODOO_TICK = 0.5                   # big bad voodoo scan [obs 0.5 s]
_D = 0.7071067811865476
MIRROR_DIRS = ((1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0), (_D, _D), (-_D, _D), (-_D, -_D), (_D, -_D))
DMG_COLD, DMG_SONIC, DMG_UNIVERSAL, DMG_MAGIC = 9, 15, 26, 14         # damage type index (mask = 1 << index)
DMG_FIRE, DMG_LIGHTNING, DMG_ENHANCED, DMG_DIVINE = 8, 10, 5, 13
DMG_FORCE = 17                                                       # 0x20000 (NOTES_spells §3.4)
DMG_NORMAL = 4                                                       # 0x10 (kaboom, NOTES_abilities §5.52)
T_STRUCT = 1 << 2                                                    # unitdata.T_STRUCTURE
DMG_SLOW_POISON = 23                                                 # 0x800000 shadow strike's missile (§5.3)
SWARM_TICK, SWARM_NEAR, SWARM_MAX = 0.1, 20.0, 64   # NOTES_spells §5.2 [obs]
MONSOON_ENTER, STARFALL_ENTER, MONSOON_CYCLE = 0x1c, 0x12, 0x12     # game streams of §3.5 / §5.6 [obs edx]
STAMPEDE_STREAM, STAMPEDE_LANES, STAMPEDE_MIN_CHECK = 0x27, 5, 0.02  # NOTES_spells §5.18 [obs]
WAVES = {'AHbz': DMG_COLD, 'ANrf': DMG_FIRE}       # NOTES_spells §5.20: damage masks 0x200 / 0x100 [obs 0x6f615f10, 0x6f616910]
WAVE_FIRST, WAVE_SHARDS, WAVE_STREAM = 0.9, 32, 4   # first damage wave 0.9 s [obs 0x6f84ca48], shards ≤ 32, jitter stream 4
LC_STREAM, LC_MAX, LC_LEASH, LC_EPS, LC_DIE, LC_LAST = 0x1b, 40, 3.0, 0.001, 7.0, 0.01   # NOTES_spells §5.16 [obs]
E_MOVE, E_ATTACKONCE = ORDER_IDS['move'], ORDER_IDS['attackonce']
LC_NEAR, LC_RANK = 50.0, 1024.0     # §5.16: the return move's 50.0 as the arrival distance [inf]; the rank scale 2^10 [obs]
VOLC_STREAM, VOLC_ROCKS, VOLC_FIRST, VOLC_FLIGHT = 0x2a, 32, 1.0, 0.5   # NOTES_spells §5.15 [obs edx 0x2a, cap 0x20, 1.0 = 0x6f84c944]; flight [inf]
CAST_IS_PERIOD = {'AEsh', 'AHfs', 'AHbz', 'ANrf', 'Arpl', 'Arpm'}   # NOTES_spells §5.3: Cast is the DoT period, no channel wait (A-SP53); AHfs: §5.14; Arpl / Arpm: Cast = the unit cap (§5.55)
REPLENISH = ('Arpl', 'Arpm')                  # NOTES_abilities §5.55: one class (vtable diff: code / name / orders / +0x478 / +0x47c)
RPL_FIRST, RPL_PERIOD, RPL_AFTER = 0.02, 3.0, 1.0   # §5.55: autocast check after turn-on, its period, the minimum after a cast [obs]
FROST_NOVA_BUFF = 'Bfro'
ROOTS_BUFF, SLEEP_BUFF = 'BEer', 'BUsl'
ROOT_DOT_FIRST, ROOT_DOT_PERIOD = 0.01, 1.0                          # CBuffLifeDoT timing (NOTES_abilities §6.2)
IMPALE_NEAR2 = 0.0001                                                # §5.13: a point on the caster → facing (A-SP61)
REPAIR_MIN_T, REPAIR_TICK = 5.0, 1.0         # NOTES_abilities §5.36: shortest repair time, the check tick [obs 0x6f4638e0]
REPAIR = ('Arst', 'Arep')                    # §5.36: one class (CAbilityRepair / CAbilityRestoration) [obs vtable diff]
REPAIR_GOLD, REPAIR_LUMBER = 1, 2            # player state ids (engine.PLAYER_GOLD / PLAYER_LUMBER)


SMART_ORDER = 851971                         # order id "smart" (orders.py)


class AreaEffect:
    __slots__ = ('u', 'code', 'lv', 'x', 'y', 'next', 'end', 'cast', 'period')


class Swarm:
    __slots__ = ('u', 'lv', 'x0', 'y0', 'dx', 'dy', 't0', 'speed', 'rem', 'hit')


class Impale:
    __slots__ = ('u', 'lv', 'x', 'y', 'sx', 'sy', 'n', 'period')


class SpellMissile:
    __slots__ = ('src', 'target', 'x', 'y', 'tx', 'ty', 'speed', 'aid', 'code', 'lv')


def ability_order(r):
    """the order id an ability record answers to (0 = none / class not implemented). A-SP1: the profile Order; the
    neutral spell AAns uses DataE of level 1 (an order string) [ext data]. Also used by the pack (JABL)."""
    if r is None or r['code'] not in CLASSES:
        return 0
    s = r.get('order') or ''
    if r['code'] == 'ANms':
        s = r.get('orderon') or 'manashieldon'
    if r['code'] == 'AAns':
        lv = r['lv'][0] if r['lv'] else None
        s = (lv.get('S') or {}).get('E', '') if lv else ''
    if r['code'] == 'ANcl':
        lv = r['lv'][0] if r['lv'] else None
        s = (lv.get('S') or {}).get('F', '') if lv else ''      # NOTES_spells §3.10: the base order string [obs]
    o = ORDER_IDS.get(s, 0)
    return o if o or r['code'] == 'ANcl' else CLASS_ORDER.get(r['code'], 0)


def target_kind(code, lv):
    """CLASSES' target kind; channel: DataB 0 none, 1 unit, 2 point, 3 unit or point (A-SP42 [ext WE names])."""
    if code == 'ANcl':
        b = int(lv['B']) if lv else 0
        return T_UNIT if b == 1 else (T_POINT if b in (2, 3) else T_NONE)
    if code == 'AIrg' and lv is not None and lv.get('D', 0) != 0:
        return T_NONE                                             # NOTES_items §10: DataD "no target required" [obs 0x6f56ca30]
    return CLASSES[code][0]


def _rc(s):
    return (ord(s[0]) << 24) | (ord(s[1]) << 16) | (ord(s[2]) << 8) | ord(s[3])


def _id4(n):
    return ''.join(chr((n >> k) & 255) for k in (24, 16, 8, 0))


def steps_of(t):
    """seconds -> whole 5 ms steps, at least 1 for a positive time (as abilities.steps_of)."""
    if t <= 0:
        return 0
    return max(1, int(t * SPS + 0.5))


class Cast:
    __slots__ = ('u', 'aid', 'code', 'lv', 'oid', 'target', 'x', 'y', 'phase', 'end', 'instant', 'kind', 'free',
                 'dead', 'item', 'pend', 'summon', 'repair', 'phx', 'can')


class Repair:
    __slots__ = ('t', 'rate', 'due', 'per')


class Cannibal:
    __slots__ = ('corpse', 'rate', 'cap')


class Spells:
    def __init__(self, eng):
        self.eng = eng
        self.N = eng.N
        self.ab = eng.ab
        self.casts = []                 # active casts (phase >= CHANNEL), start order
        self.pend_end = []              # interrupted casts whose ENDCAST is due (flush)
        self.pend_begin = None          # a cast accepted by order() that begins at flush()
        self.cool = {}                  # (unit serial, ability id) -> step the cooldown ends
        self.order_of = {}              # ability id -> order id (0 = none), cached
        self.missiles = []              # spell missiles (thunder bolt), launch order
        self.areas = []                 # channelled area effects (earthquake, death and decay), start order
        self.flames = []                # NOTES_spells §5.14 flame strikes (creation order)
        self.monsoons = []              # NOTES_spells §3.5 / §5.6 monsoon / starfall effects (creation order)
        self.stamps = []                # §5.18 running stampede channels
        self.volcs = []                 # §5.15 running volcano effects (A-SP82)
        self.swarms = {}                # §5.16 (caster serial, ability id) -> the running locust swarm (A-SP83)
        self.lc_cnt = {}                # §5.16 (target serial, locust owner id) -> locusts registered on it
        self.waves = []                 # §5.20 blizzard / rain of fire effects (creation order)
        self.wv_mark = {}               # §5.20 (unit serial, class code) -> marker end step (A-SP79)
        self.st_lanes = {}              # §5.18 (unit serial, ability id) -> used-lane bits (kept across casts)

    # ------------------------------------------------------------ data
    def order_id(self, aid):
        o = self.order_of.get(aid)
        if o is None:
            o = ability_order(self.ab.rec(aid))
            self.order_of[aid] = o
        return o

    def spell_of(self, u, oid):
        """first ability in list order that answers order id oid (A-SP1), -> (aid, base code, level rec) or None."""
        if oid == 0:
            return None
        for aid, code, lv in self.ab.abil_list(u):
            if lv is not None and self.order_id(aid) == oid:
                return aid, code, lv
        return None

    def cooling(self, u, aid):
        e = self.cool.get((u.serial, aid))
        return e is not None and self.eng.now() < e

    def item_spell(self, u, slot):
        """§4.5.1: useslot answers only for a usable item in an inventory that can use items (DataC ≠ 0): its first
        spell-class ability -> (aid, code, level rec, item) or None."""
        itm = self.eng.itm
        if slot >= itm.size(u):
            return None
        it = u.items[slot]
        inv = itm.inventory(u)
        if it is None or inv is None or inv['C'] == 0 or not (it.fl & ID.FLAG_USABLE):
            return None
        for a in itm.abilities(it):
            r = self.ab.defs.get(a)
            if r is not None and r['code'] in CLASSES and r['lv']:
                return a, r['code'], r['lv'][0], it
        return None

    def cool_key(self, aid, it):
        """item abilities cool down per item cooldown group (the item's cooldownID, else the ability) [obs]."""
        if it is None:
            return aid
        g = it.data.get('cooldownID') or ''
        return ('grp', _rc(g) if len(g) == 4 else aid)

    # ------------------------------------------------------------ order
    def order(self, u, oid, x=None, y=None, target=None):
        """-> True if the spell order was taken (engine.issue); None if no ability of u answers oid."""
        if oid == SMART_ORDER and target is not None:
            # CAbilityRepair §7.3 [obs 0x6f4649d0]: the smart order becomes the class's own order id; a damaged
            # friendly target is therefore repaired, not followed/attacked. A full-life target stays a plain smart.
            if self.repair_smart(u, target):
                oid = ORDER_IDS['repair']
        if oid in ARROW_ORDERS:
            return self.arrow_order(u, oid, target)
        if oid in (ORDER_IDS['corporealform'], ORDER_IDS['uncorporealform']):
            return self.morph(u, oid)
        if oid in (ORDER_IDS['phaseshifton'], ORDER_IDS['phaseshiftoff']):
            for aid, code, lv in self.ab.abil_list(u):
                if code == 'Apsh':
                    if oid == ORDER_IDS['phaseshifton']:
                        u.auto_on.add(aid)
                    else:
                        u.auto_on.discard(aid)
                    return True
            return None
        if oid in RPL_TOGGLE:
            for aid, code, lv in self.ab.abil_list(u):
                if code == RPL_TOGGLE[oid][0] and self.order_id(aid) == RPL_TOGGLE[oid][2]:
                    self.rpl_auto(u, aid, RPL_TOGGLE[oid][1])
                    return True
            return None
        if oid in (CURSE_ON, CURSE_OFF):
            return self.curse_auto(u, oid == CURSE_ON)
        if oid in (DEFEND_ON, DEFEND_OFF):
            return self.defend_order(u, oid == DEFEND_ON)
        if oid == ORDER_IDS['unimmolation']:
            for aid, code, lv in self.ab.abil_list(u):
                if code == 'AEim':
                    self.ab.immolation_off(u)
                    return True
            return None
        if oid == ORDER_IDS['manashieldoff']:
            for aid, code, lv in self.ab.abil_list(u):
                if code == 'ANms':
                    self.ab.remove_buff_code(u, _rc('BNms'))      # instant, no cast sequence (A-SP25)
                    return True
        it = None
        if USE_SLOT0 <= oid < USE_SLOT0 + 6:
            sp = self.item_spell(u, oid - USE_SLOT0)
            if sp is None:
                return None
            aid, code, lv, it = sp
            if not it.uncharged and it.charges == 0:
                return False                                      # 0x54: a charged item without charges
        else:
            sp = self.spell_of(u, oid)
            if sp is None:
                return None
            aid, code, lv = sp
            if aid in u.owner.abil_off:
                return False                                      # NAT-3: SetPlayerAbilityAvailable(false): the order fails
        kind, instant = target_kind(code, lv), CLASSES[code][1]
        if u.dead or not u.alive or u.constructing:
            return False
        if it is None and u.silenced > 0:
            return False                                          # NOTES_spells §5.4: BNsi bit 8 (items: A-SP55)
        if u.mana < lv['Cost']:
            return False                                          # 0xd1 not enough mana
        if self.cooling(u, self.cool_key(aid, it)):
            return False                                          # 0xd2 cooldown
        if kind == T_UNIT and code in CORPSE_TARGET:
            if not self.raisable(target):
                return False                                      # NOTES_abilities §5.30: a corpse only (A-SP60)
            if code == 'Acan' and (u.life >= u.maxlife or getattr(target, 'eaten', False)):
                return False                                      # §5.60: 0x1c at full life; a corpse being eaten (flag 0x1000)
        elif kind == T_UNIT:
            if target is None or not target.alive or target.dead:
                return False
            if getattr(target, 'invisible', 0) and not self.ab.visible_to(u, target):
                return False                                      # BUGS W8-8: an unseen invisible unit [ext]
            if not self.ab.targ_ok(u, target, lv['targs']):
                return False
        elif kind == T_POINT:
            if target is not None:
                x, y = target.x, target.y
            if x is None:
                return False
        if not self.affordable(u, code, lv):
            return False
        if code == 'AHhb' and target is not None:
            if getattr(target, 'illusion', False):
                return False                                      # §5.20: 0xa2
            if self.eng.ally(u.owner, target.owner) and not (target.ucls & 2) and target.life >= target.maxlife:
                return False                                      # §5.20: 0x1c full life
        if code in REPAIR and target is not None and not self.repair_ok(u, target):
            return False                                          # §5.36: 0x98 / 0x31 / 0x1e / 0xa2 / 8 / 9
        if code in REPAIR and target is not None and not self.repair_order_ok(u, target):
            return False                                          # §7.5 [obs]: the pre-order threshold amount/10 < 1 (8 / 9)
        if code == 'AOhw' and target is not None and not self.wave_ok(target):
            return False                                          # §5.33: 0x1c full life / 0xa2 illusion
        if code == 'Arej' and target is not None and not self.rej_ok(target, lv):
            return False                                          # §5.28: 0xdd / 0xa2 / 0x1c / 0x1d / 0x7d
        if code == 'AIrg' and lv['Area'] <= 0.0 and not self.rej_ok(u if kind == T_NONE else target, lv):
            return False                                          # NOTES_items §10: the Arej can-target test 0x6f56bea0
        if code == 'Alsh' and target is not None and self.eng.flying(target):
            return False                                          # §5.22: 0x5e, not on flying units
        if code == 'Aply' and target is not None:
            mx = int(lv['A'])
            if mx > 0 and int(target.data.get('race', 0) or 0) == 8 and int(target.data.get('level', 0) or 0) > mx:
                return False                                      # §5.28: 0x33 creep level above DataA
            if target.ucls & 0x4:
                return False                                      # §5.28: 0x9f summoned
        if code == 'AUim' and target is not None and self.eng.flying(target):
            return False                                          # NOTES_spells §5.13: 0x5e, not on flying units
        if code == 'AIhe' and u.life >= u.maxlife:
            return False                                          # §5.27: 0x19 full life
        if code == 'AIma' and u.mana >= u.maxmana:
            return False                                          # §5.32: 0x1a full mana
        if code == 'AIre' and u.mana >= u.maxmana and u.life >= u.maxlife:
            return False                                          # CAbilityItemRestore §2: 0x1b nothing to restore (both pools full)
        if code == 'AIbl':
            tid = self.build_type(u, lv)
            if not tid or not self.eng.can_place(u.owner, tid, x, y, u)[0]:
                return False                                      # §8: placement fails → message only, no cast
        if u.fcast is not None or (instant and u.cast is not None):
            return False                                          # A-SP28: one cast at a time
        eng = self.eng
        c = Cast()
        c.u, c.aid, c.code, c.lv, c.oid = u, aid, code, lv, oid
        c.free, c.dead = instant, False
        c.item, c.pend, c.summon = it, False, None
        c.repair = None
        c.phx = None
        c.can = None
        if instant:
            # A-SP29: an instant ability queues no unit task (NOTES_production §5.2 step 1) → the current order goes on
            c.target = target if kind == T_UNIT else None
            c.x = r32(x) if x is not None else u.x
            c.y = r32(y) if y is not None else u.y
            c.instant, c.kind = True, kind
            c.phase, c.end = PH_MOVE, -1
            u.fcast = c
            self.pend_begin = c
            return True
        eng.clear_order(u)
        c.target = target if kind == T_UNIT else None
        c.x = r32(x) if x is not None else u.x
        c.y = r32(y) if y is not None else u.y
        c.instant, c.kind = instant, kind
        c.phase, c.end = PH_MOVE, -1
        u.order = oid
        u.cast = c
        if not instant and kind != T_NONE and not self.in_range(c):
            u.tx, u.ty = (c.target.x, c.target.y) if c.target is not None else (c.x, c.y)
            u.target = c.target
            if eng.can_move(u) and eng.eff_speed(u, True) > 0:
                self.N.moving.add(u)                              # walk into range (A-SP3); begin() on arrival
                return True
            u.cast = None
            u.order = 0
            return False
        self.pend_begin = c                                       # begins in flush() (A-SP7)
        return True

    def flush(self):
        """after an accepted order (engine.issue, SetUnitPosition): ENDCAST of interrupted casts, then the new cast's
        CHANNEL … (A-SP7)."""
        while self.pend_end:
            c = self.pend_end.pop(0)
            self.event(c, 'SPELL_ENDCAST')
            self.item_used(c)
        c = self.pend_begin
        if c is not None:
            self.pend_begin = None
            if not c.dead and c.phase == PH_MOVE:
                self.begin(c)

    def in_range(self, c):
        u = c.u
        tx, ty = (c.target.x, c.target.y) if c.target is not None else (c.x, c.y)
        r = c.lv['Rng']
        if c.code in REPAIR and c.target is not None:
            return self.repair_reach(u, c.target, r)              # §5.36: edge to edge (0x6f08ce80)
        dx, dy = tx - u.x, ty - u.y
        return dx * dx + dy * dy <= r * r                         # A-SP3: centre distance

    def affordable(self, u, code, lv):
        if code == 'AAns':
            pl = self.caster_payer(u, lv)
            return pl.state.get(1, 0) >= int(lv['A']) and pl.state.get(2, 0) >= int(lv['B'])   # A-SP2
        return True

    @staticmethod
    def caster_payer(u, lv):
        return u.owner                                            # DataF != 0: the caster's owner (§5 [obs])

    # ------------------------------------------------------------ sequence
    def begin(self, c):
        u = c.u
        if not c.free:
            self.N.moving.discard(u)
            u.path = None
        c.phase = PH_CHANNEL
        self.casts.append(c)
        self.event(c, 'SPELL_CHANNEL')
        if c.dead:
            return
        if c.lv['Cast'] > 0 and c.code not in CAST_IS_PERIOD:
            c.end = self.eng.now() + steps_of(c.lv['Cast'])
            return
        self.cast_phase(c)

    def cast_phase(self, c):
        u = c.u
        c.phase = PH_CAST
        self.event(c, 'SPELL_CAST')
        if c.dead:
            return
        pt = float(u.data.get('castpt', 0) or 0)
        if pt > 0 and not c.instant:
            c.end = self.eng.now() + steps_of(pt)
            return
        self.effect_phase(c)

    def effect_phase(self, c):
        u = c.u
        c.phase = PH_EFFECT
        self.event(c, 'SPELL_EFFECT')
        cost = c.lv['Cost'] if c.code not in REPLENISH else 0.0      # §5.55: Cost × n after the enumeration
        if cost > 0:
            u.mana = r32(u.mana - min(cost, u.mana))
        if u.alive and not u.dead and (c.instant or not c.dead):
            if c.item is not None:
                c.pend = True                                     # §4.5.1: flag 0x4000, the charge goes at ENDCAST
            fx = getattr(self, 'fx_' + c.code, None)
            if fx is not None:
                fx(c)
        cool = c.lv['Cool'] if c.item is None or not (c.item.fl & ID.FLAG_IGNORECD) else 0.0
        if cool > 0:
            self.cool[(u.serial, self.cool_key(c.aid, c.item))] = self.eng.now() + steps_of(cool)
        if c.dead:
            return
        if c.code == 'Acan' and c.can is not None:
            c.phase = PH_CHANNELING                               # §5.60: the channel lasts Dur′ (or until it is interrupted)
            c.end = self.eng.now() + steps_of(self.ab.dur(c.lv, u))
            return
        if c.code in REPAIR and c.repair is not None:
            c.phase = PH_CHANNELING                               # §5.36: repairs until a tick stops it
            c.end = -1
            return
        if c.code == 'Apsh':
            c.phase = PH_CHANNELING                               # the shift's timer: Dur + 0.1 s [obs]
            c.end = self.eng.now() + steps_of(self.ab.dur(c.lv, u) + 0.1)
            return
        if c.code == 'ANvc' and float(int(c.lv['B'])) * c.lv['C'] > 0:
            c.phase = PH_CHANNELING                               # §5.15: the backswing holder float(trunc(B)) × C
            c.end = self.eng.now() + steps_of(float(int(c.lv['B'])) * c.lv['C'])
            return
        if c.code in WAVES and c.lv['Cast'] * int(c.lv['A']) > 0:
            c.phase = PH_CHANNELING                               # §5.20: the backswing holder float(trunc(A)) × Cast
            c.end = self.eng.now() + steps_of(float(int(c.lv['A'])) * c.lv['Cast'])
            return
        if c.code in CHANNEL:
            c.phase = PH_CHANNELING
            c.end = self.eng.now() + steps_of(c.lv['Dur'])           # A-SP14: follow-through = Dur
            return
        if c.code == 'ANcl' and c.lv['A'] > 0:
            c.phase = PH_CHANNELING
            c.end = self.eng.now() + steps_of(c.lv['A'])             # A-SP42: follow-through DataA, no backswing
            return
        bsw = float(u.data.get('castbsw', 0) or 0)
        if c.code == 'AUim':
            bsw = c.lv['B'] + 0.1                                 # NOTES_spells §5.13: backswing holder DataB + 0.1 [obs]
        if c.code == 'AHfs':
            bsw = c.lv['Cast']                                    # NOTES_spells §5.14: the strike Cast s after EFFECT
        if bsw > 0 and not c.instant:
            c.phase = PH_BACKSWING
            c.end = self.eng.now() + steps_of(bsw)
            return
        self.finish(c)

    def finish(self, c):
        u = c.u
        if c.code == 'Acan' and c.can is not None:
            self.can_done(c)                                      # §5.60: the corpse is used up at the natural end
        if c.code == 'Aphx' and getattr(c, 'phx', None):
            self.phx_swap(c)                                      # §5.50: the type swap at the end of the morph time
        self.drop(c)
        if c.code == 'AHfs':
            self.flame_start(c)                                   # NOTES_spells §5.14: the end slot creates the strike
        if c.code == 'Apsh':
            self.ab.remove_buff_code(u, _rc('Bpsh'))
        self.event(c, 'SPELL_FINISH')
        self.event(c, 'SPELL_ENDCAST')
        self.item_used(c)
        if not c.free and u.alive and not u.dead and u.order == c.oid and u.cast is None:
            u.order = 0
            u.next_scan = self.eng.now()
            self.eng.wake(u, self.eng.now())

    def item_used(self, c):
        """§4.5.1 after the ENDCAST triggers: charges −1 (charged items), USE_ITEM while carried, a perishable item
        at 0 charges dies (DROP_ITEM)."""
        it = c.item
        if it is None or not c.pend:
            return
        c.pend = False
        u = c.u
        if not it.uncharged and it.charges > 0:
            it.charges -= 1
        if it.carrier is u:
            self.N.unit_event(u, 'USE_ITEM', {'item': self.N.H(it), 'manipulator': self.N.H(u)})
        if (it.fl & ID.FLAG_PERISHABLE) and not it.uncharged and it.charges == 0 and it.live:
            self.eng.itm.kill(it)

    def drop(self, c):
        if getattr(c, 'phx', None):
            self.phx_counters(c.u, c.phx, -1)                     # §5.50: an interrupted morph gives the counters back
            c.phx = None
        if c.repair is not None:
            self.repair_off(c)                                    # §5.36: the rate leaves the target
        if c.can is not None:
            self.can_off(c)                                       # §5.60: the rate leaves the caster, the corpse is free again
        t = getattr(c, 'summon', None)
        if t is not None and t.alive and not t.dead:
            c.summon = None
            self.eng.timed_life(t, 0)                            # A-SP40: the tornado dies at the next step
        for w in [w for w in self.waves if w['cast'] is c]:
            self.wave_stop(w)                                     # §5.20: the visual waves stop, d − v damage waves left
        for v in [v for v in self.volcs if v['cast'] is c]:
            v['alive'] = False                                    # §5.15: the interrupt destroys the volcano (rocks in flight land)
            self.volcs.remove(v)
        for st in [st for st in self.stamps if st['cast'] is c]:
            st['alive'] = False                                   # §5.18: no more beasts (running ones go on)
            self.stamps.remove(st)
        for m in [m for m in self.monsoons if m['cast'] is c]:
            self.monsoon_end(m)                                   # §3.5: the channel end destroys the effect
        for a in [a for a in self.areas if a.cast is c]:
            self.areas.remove(a)                                  # the channel ends: its area effect too
            if a.code == 'AOvd':
                for v in list(self.N.units):
                    for bf in list(v.buffs):
                        if bf.kind == 'voodoo' and bf.src is c.u:
                            self.ab.remove_buff(bf)
        if c in self.casts:
            self.casts.remove(c)
        c.dead = True
        if c.u.cast is c:
            c.u.cast = None
        if c.u.fcast is c:
            c.u.fcast = None

    def interrupt(self, u):
        """a new order / stop while casting: SPELL_ENDCAST only [obs], due at flush(); walking to range: no event."""
        c = getattr(u, 'cast', None)
        if c is None:
            return
        self.drop(c)
        if c.phase >= PH_CHANNEL:
            self.pend_end.append(c)

    def on_death(self, u):
        for c in (u.cast, u.fcast):
            if c is not None:
                self.drop(c)                                      # A-SP4: no events for a dying caster
        self.lc_unit_gone(u)                                      # §5.16 locust / swarm caster (A-SP83)

    def arrived(self, u):
        """engine movement: the walking caster reached its range."""
        c = u.cast
        if c is not None and c.phase == PH_MOVE and self.in_range(c):
            self.begin(c)
            return True
        return False

    def reached_point(self, u):
        """movement ended (the walk reached its point) with the cast not begun: begin if in range, else the order
        ends without events (A-SP3)."""
        c = u.cast
        self.N.moving.discard(u)
        u.path = None
        if c.phase == PH_MOVE and self.in_range(c):
            self.begin(c)
            return
        self.drop(c)
        u.order = 0
        u.next_scan = self.eng.now()
        self.eng.wake(u, self.eng.now())

    def step(self, dt=0.005):
        now = self.eng.now()
        if self.missiles:
            self.step_missiles(dt)
        if self.areas:
            self.step_areas()
        if self.flames:
            self.step_flames()
        if self.monsoons:
            self.step_monsoons()
        for c in list(self.casts):
            self.eng.wk += 1
            if c.repair is not None and c in self.casts:
                self.repair_tick(c, now)
            if c.can is not None and c in self.casts:
                self.can_tick(c, now)
            if c not in self.casts or c.end < 0 or now < c.end:
                continue
            c.end = -1
            if c.phase == PH_CHANNEL:
                self.cast_phase(c)
            elif c.phase == PH_CAST:
                c.phase = PH_EFFECT
                self.effect_phase(c)
            elif c.phase in (PH_BACKSWING, PH_CHANNELING):
                self.finish(c)

    # ------------------------------------------------------------ events
    def event(self, c, suffix):
        N = self.N
        d = {'ability': c.aid, 'caster': N.H(c.u)}
        if c.target is not None:
            d['spelltarget'] = N.H(c.target)
        if c.kind == T_POINT:
            d['sx'], d['sy'] = c.x, c.y
        N.unit_event(c.u, suffix, d)

    # ------------------------------------------------------------ spell missiles (A-SP8: moved like attack missiles,
    # homing, before the cast phases)
    def step_missiles(self, dt):
        for m in list(self.missiles):
            self.eng.wk += 1
            t = m.target
            if t is not None and t.alive and not t.dead:
                m.tx, m.ty = t.x, t.y
            dx, dy = m.tx - m.x, m.ty - m.y
            dist = math.sqrt(dx * dx + dy * dy)
            d = m.speed * dt
            if dist <= d:
                m.x, m.y = m.tx, m.ty
                self.missiles.remove(m)
                self.missile_hit(m)
            else:
                m.x = m.x + dx / dist * d
                m.y = m.y + dy / dist * d

    def missile_hit(self, m):
        t = m.target
        if t is None or not t.alive or t.dead:
            return                                                # A-SP9: a dead target: the bolt fizzles
        src = m.src if m.src.alive and not m.src.dead else None
        if m.code in ('Aens', 'Aweb'):
            # §5.17: an existing Bena / Beng is refreshed, else Bena on a flyer, Beng otherwise; held (A-SP47);
            # web (§5.53): the same with Bwea / Bweb
            if m.code == 'Aweb':
                code = 'Bwea' if self.eng.flying(t) else 'Bweb'
            else:
                code = 'Bena' if self.eng.flying(t) else 'Beng'
            bf, _n = self.ab.add_buff(t, _rc(code), 'ensnare', src, self.ab.dur(m.lv, t))
            bf.stun = True
            self.ab.apply(bf)
            return
        if m.code == 'ANab':
            # §5.47 (A-SP70): the target (DataD), then the targs units within Area of it (DataE); magic immune: none
            if t.magic_immune <= 0:
                self.acid_on(t, src, m.lv, m.lv['D'])
            for v in self.in_area(t.x, t.y, m.lv['Area'], m.src, m.lv['targs']):
                if v is not t and v.magic_immune <= 0:
                    self.acid_on(v, src, m.lv, m.lv['E'])
            return
        if m.code == 'AEsh':
            # §5.3: DataE SLOW_POISON (physical class), then BEsh on a survivor
            self.eng.damage(t, src, r32(m.lv['E']), 0, DMG_SLOW_POISON, False, False)
            if t.alive and not t.dead:
                self.ab.sstrike_on(t, src, m.lv)
            return
        if m.code in ('AHtb', 'ANfb', 'ACtb'):
            self.eng.damage(t, src, r32(m.lv['A']), 0, DMG_FIRE if m.code == 'ANfb' else DMG_SONIC, False, False)
            if t.alive and not t.dead:
                self.ab.stun(t, src, self.ab.dur(m.lv, t))

    # ------------------------------------------------------------ area effects (A-SP15: ticks before the cast phases)
    def area_start(self, c, delay):
        a = AreaEffect()
        a.u, a.code, a.lv, a.x, a.y, a.cast = c.u, c.code, c.lv, c.x, c.y, c
        a.period = VOODOO_TICK if c.code == 'AOvd' else AREA_TICK
        a.next = self.eng.now() + steps_of(delay if delay > 0 else a.period)
        a.end = -1
        self.areas.append(a)

    def step_areas(self):
        now = self.eng.now()
        for a in list(self.areas):
            self.eng.wk += 1
            if a not in self.areas or now < a.next:
                continue
            a.next = now + steps_of(a.period)
            self.area_tick(a)

    def area_tick(self, a):
        eng, ab, lv, u = self.eng, self.ab, a.lv, a.u
        r = lv['Area']
        cand = sorted((v for v in eng.query(a.x, a.y, r + eng.max_coll) if v.alive and not v.dead),
                      key=lambda v: v.serial)
        for v in cand:
            dx, dy = v.x - a.x, v.y - a.y
            if dx * dx + dy * dy > r * r or not ab.targ_ok(u, v, lv['targs']):
                continue
            if not v.alive or v.dead:
                continue
            if a.code == 'AUdd':
                f = lv['A'] * (lv['B'] if v.is_bldg and lv['B'] > 0 else 1.0)          # A-SP16
                eng.damage(v, u, r32(v.maxlife * f), 0, DMG_UNIVERSAL, False, False)
            elif a.code == 'AOvd':
                if v is u:
                    continue                                      # the caster stays vulnerable [obs]
                bf, new = ab.add_buff(v, _rc('BOvd'), 'voodoo', u, VOODOO_TICK * 2)    # A-SP35
                if new:
                    for ob in list(v.buffs):
                        if ob.kind in NEGATIVE_KINDS:
                            ab.remove_buff(ob)
                bf.vals['inv'] = 1
                bf.vals['mimm'] = 1
                ab.apply(bf)
            elif a.code == 'AOeq':
                if v.is_bldg:
                    eng.damage(v, u, r32(lv['B']), 0, DMG_UNIVERSAL, False, False)
                else:
                    bf, _new = ab.add_buff(v, _rc('BOeq'), 'quake', u, AREA_TICK)
                    bf.move = -lv['C']
                    ab.apply(bf)

    # ------------------------------------------------------------ frost arrows (NOTES_heroes §4.1, §5)
    def arrow_ability(self, u, base='AHca'):
        for aid, code, lv in self.ab.abil_list(u):
            if code == base and lv is not None:
                return aid
        return None

    def arrow_order(self, u, oid, target):
        """coldarrows / uncoldarrows (flamingarrows / unflamingarrows): autocast on / off (instant, no cast
        sequence); coldarrowstarg (flamingarrowstarg): one manual shot = an attack order on the target whose next
        shot carries the arrow (A-SP18, A-SP59)."""
        aid = self.arrow_ability(u, ARROW_BASE[oid])
        if aid is None:
            return None
        if oid in (ORDER_IDS['coldarrows'], ORDER_IDS['flamingarrows']):
            u.arrow_on.add(aid)
            return True
        if oid in (ORDER_IDS['uncoldarrows'], ORDER_IDS['unflamingarrows']):
            u.arrow_on.discard(aid)
            return True
        if target is None:
            return False
        r = self.eng.issue_order(u, ORDER_IDS['attack'], None, None, target)
        if r:
            u.arrow_t = target
        return r

    def morph(self, u, oid):
        """corporeal form (NOTES_spells §1.3, A-SP31): corporealform turns the normal form (DataA, ethereal) into the
        alternate one (UnitID, corporeal), uncorporealform back; the type swaps at once (no cast sequence, the order
        stays), cooldown Cool."""
        for aid, code, lv in self.ab.abil_list(u):
            if code != 'Acpf' or lv is None:
                continue
            na = (lv.get('S') or {}).get('A', '')
            alt = lv.get('UnitID') or ''
            if len(na) != 4 or len(alt) != 4 or u.dead or self.cooling(u, aid):
                return False
            to_alt = oid == ORDER_IDS['corporealform']
            frm, to = (_rc(na), _rc(alt)) if to_alt else (_rc(alt), _rc(na))
            d = self.N.unit_types.get(to)
            if u.tid != frm or d is None:
                return False
            u.tid, u.data, u.own_data = to, d, False       # NAT-1: the unit field overrides go with the old type
            u.ethereal += -1 if to_alt else 1
            if u.ethereal < 0:
                u.ethereal = 0
            if lv['Cool'] > 0:
                self.cool[(u.serial, aid)] = self.eng.now() + steps_of(lv['Cool'])
            return True
        return None

    def arrow_effect(self, u, aid, lv, t):
        """the spell-effect step per shot: SPELL_EFFECT, then mana, then the cooldown [obs]."""
        c = Cast()
        c.u, c.aid, c.code, c.lv, c.oid = u, aid, 'AHca', lv, 0
        c.free, c.dead = True, True
        c.target, c.x, c.y, c.kind, c.instant = t, u.x, u.y, T_UNIT, True
        self.event(c, 'SPELL_EFFECT')
        cost = lv['Cost']
        if cost > 0:
            u.mana = r32(u.mana - min(cost, u.mana))
        if lv['Cool'] > 0:
            self.cool[(u.serial, aid)] = self.eng.now() + steps_of(lv['Cool'])

    # ------------------------------------------------------------ effects
    def fx_ANto(self, c):
        """§4.9: UnitID (ntor) for the caster's owner at the point, facing the caster's, timed life Dur, summoned;
        then EVENT_PLAYER_UNIT_SUMMON on the summoned unit (A-SP21)."""
        u, lv, N = c.u, c.lv, self.N
        tid = lv.get('UnitID') or 'ntor'
        if len(tid) != 4:
            return
        nu = N.create_unit(u.owner, _rc(tid), c.x, c.y, self.eng.building_angle)   # facing BuildingAngle [obs]
        if nu is None:
            return
        nu.summoned = True
        nu.invuln_n += 1                                          # BNto: invulnerable + magic immune [obs]
        nu.invulnerable = True
        nu.magic_immune += 1
        self.eng.timed_life(nu, lv['Dur'] + 0.02)
        c.summon = nu                                             # the channel's end kills it (A-SP40)
        N.unit_event(nu, 'SUMMON', {'summoner': N.H(u), 'summoned': N.H(nu)})

    def fx_AOvd(self, c):
        u = c.u
        for bf in list(u.buffs):
            if bf.code in (_rc('Bvul'), _rc('BHds'), _rc('BOvd')):
                self.ab.remove_buff(bf)                           # the caster loses its protection [obs]
        c.x, c.y = u.x, u.y
        self.area_start(c, 0.0)
        a = self.areas[-1]
        self.area_tick(a)                                         # A-SP35: the first scan at once
        a.next = self.eng.now() + steps_of(VOODOO_TICK)

    def fx_AOmi(self, c):
        """§1.6: kill the previous images, n = trunc(DataA) (≤ 9) images of the caster's type around it (A-SP33),
        the caster takes a random one of the n + 1 spots; images: life / mana copied, illusion, timed life Dur /
        HeroDur, deal × DataB, take × DataC, EVENT_UNIT_SUMMON (summoner = caster)."""
        u, lv, N, eng = c.u, c.lv, self.N, self.eng
        for im in list(u.images):
            if im.alive and not im.dead:
                N.kill_unit(im, None)
        u.images = []
        n = min(9, int(lv['A']))
        if n <= 0:
            return
        g = eng.streams.game[2]
        pick = g.below(n + 1)                                     # A-SP33: game stream 2
        r = lv['Rng'] if lv['Rng'] > 0 else 128.0
        spots = []
        for k in range(n + 1):
            dx, dy = MIRROR_DIRS[k % 8]
            f = r * (1 + k // 8)                                  # A-SP33: fixed directions (no trig: exact in
            spots.append((r32(u.x + dx * f), r32(u.y + dy * f)))  # every runtime)
        life, mana = u.life, u.mana
        k2 = 0
        for k in range(n + 1):
            if k == pick:
                continue
            im = N.create_unit(u.owner, u.tid, spots[k][0], spots[k][1], u.facing, food=False)
            if im is None:
                continue
            k2 += 1
            im.illusion = True
            im.img_dealt, im.img_taken = lv['B'], lv['C']
            im.life = life
            im.mana = mana
            eng.timed_life(im, self.ab.dur(lv, u))
            im.ucls &= ~0x4                                       # images are not SUMMONED [obs NOTES_unittype]
            u.images.append(im)
            N.unit_event(im, 'SUMMON', {'summoner': N.H(u), 'summoned': N.H(im)})
        N.set_unit_pos(u, spots[pick][0], spots[pick][1])

    def fx_ANef(self, c):
        """§1.8: DataA's unit types (comma list) at random points within Area (A-SP34), timed life Dur (+0.01), SUMMON;
        the caster is hidden, held, invulnerable and magic immune until its summons are gone."""
        u, lv, N, eng, ab = c.u, c.lv, self.N, self.eng, self.ab
        ids = [s.strip() for s in str((lv.get('S') or {}).get('A', '')).split(',')]
        ids = [s for s in ids if len(s) == 4 and _rc(s) in N.unit_types]
        if not ids:
            return
        bf, _new = ab.add_buff(u, _rc('BNef'), 'efcaster', u, 0.0)
        bf.vals['inv'] = 1
        bf.vals['mimm'] = 1
        bf.stun = True
        ab.apply(bf)
        u.hidden = True
        g = eng.streams.game[2]
        R = lv['Area']
        dur = lv['Dur'] + 0.01 if lv['Dur'] > 0 else 0.0
        for s in ids:
            x = r32(u.x + (g.unit() * 2 * R - R))
            y = r32(u.y + (g.unit() * 2 * R - R))
            nu = N.create_unit(u.owner, _rc(s), x, y, u.facing)
            if nu is None:
                continue
            nu.ef_caster = u
            u.ef_summons.append(nu)
            if dur > 0:
                eng.timed_life(nu, dur)
            N.unit_event(nu, 'SUMMON', {'summoner': N.H(u), 'summoned': N.H(nu)})

    def ef_summon_gone(self, s):
        u = s.ef_caster
        s.ef_caster = None
        if u is None or s not in u.ef_summons:
            return
        u.ef_summons.remove(s)
        if not u.ef_summons and u.alive and not u.dead:
            self.ab.remove_buff_code(u, _rc('BNef'))              # the caster comes back (A-SP34)
            u.hidden = False

    def fx_Apsh(self, c):
        u, lv, ab = c.u, c.lv, self.ab
        bf, _new = ab.add_buff(u, _rc('Bpsh'), 'phase', u, ab.dur(lv, u))
        bf.vals['invis'] = 1
        bf.vals['inv'] = 1                                        # A-SP27: untargetable / not hittable
        ab.apply(bf)

    def fx_AHds(self, c):
        u, lv, ab = c.u, c.lv, self.ab
        for bf in list(u.buffs):
            if bf.kind in NEGATIVE_KINDS:
                ab.remove_buff(bf)                                # A-SP22: the dispel on apply
        bf, _new = ab.add_buff(u, _rc('BHds'), 'shield', u, ab.dur(lv, u))
        bf.vals['inv'] = 1
        bf.vals['mimm'] = 1
        ab.apply(bf)

    def fx_AHav(self, c):
        u, lv, ab = c.u, c.lv, self.ab
        code = _rc(lv['BuffID'][0]) if lv['BuffID'] else _rc('BHav')
        bf, _new = ab.add_buff(u, code, 'avatar', u, ab.dur(lv, u))
        bf.armor = lv['A']
        bf.vals['life'] = lv['B']
        bf.vals['dint'] = int(lv['C'])
        bf.vals['mimm'] = 1
        ab.apply(bf)

    def fx_ANms(self, c):
        u, lv, ab = c.u, c.lv, self.ab
        bf, _new = ab.add_buff(u, _rc('BNms'), 'mshield', u, ab.dur(lv, u))
        bf.vals['A'] = lv['A']
        bf.vals['B'] = lv['B']
        ab.apply(bf)

    def fx_AOeq(self, c):
        self.area_start(c, c.lv['A'])                             # the quake starts after DataA s [obs]

    def fx_AUdd(self, c):
        self.area_start(c, 0.0)

    def fx_AHtb(self, c):
        u, lv = c.u, c.lv
        r = self.ab.rec(c.aid) or {}
        m = SpellMissile()
        m.src, m.target, m.aid, m.code, m.lv = u, c.target, c.aid, c.code, lv
        m.x, m.y = u.x, u.y
        m.tx, m.ty = c.target.x, c.target.y
        m.speed = float(int(r.get('mspeed', 0) or 0)) or 1000.0        # packed as an integer (JABL)
        self.missiles.append(m)

    def fx_ANfb(self, c):
        self.fx_AHtb(c)

    def fx_ACtb(self, c):
        self.fx_AHtb(c)                                           # NOTES_spells §5.12: the AHtb class body [obs]

    def fx_Asds(self, c):
        """NOTES_abilities §5.52 (A-AB21): at the caster's position the explosion after Dur (+0x9c = 0; ≤ 0: in the
        next timer phase [inf]), then the caster dies (no FINISH / ENDCAST)."""
        u = c.u
        ex = (u.x, u.y, u, c.lv)
        self.ab.at(self.eng.now() + steps_of(c.lv['Dur']), 'kaboom', ex, None)
        self.N.kill_unit(u, None)

    def kaboom(self, ex):
        """0x6f1fb180: the units (no structures) passing the caster's targs within max(A, C) take the damage of the
        innermost ring (A, B) / (C, D); then, if targs has structures, the structures × DataE; NORMAL, attack type 0,
        no source (the caster is dead); serial order."""
        x, y, u, lv = ex
        eng, ab = self.eng, self.ab
        rings = sorted([r for r in ((lv['A'], lv['B']), (lv['C'], lv['D'])) if r[0] > 0], key=lambda r: r[0])
        if not rings:
            return
        rmax = rings[-1][0]
        cand = sorted((v for v in eng.query(x, y, rmax + eng.max_coll) if v.alive and not v.dead), key=lambda v: v.serial)
        hits = []
        for bldg in (False, True):
            if bldg and not (lv['targs'] & T_STRUCT):
                break
            k = lv['E'] if bldg else 1.0
            for v in cand:
                if v.is_bldg != bldg or v is u:
                    continue
                dx, dy = v.x - x, v.y - y
                d2 = dx * dx + dy * dy
                if d2 > rmax * rmax or not ab.targ_ok(u, v, lv['targs']):
                    continue
                for r, dmg in rings:
                    if d2 <= r * r:
                        hits.append((v, r32(dmg * k)))
                        break
        for v, dmg in hits:
            if v.alive and not v.dead:
                eng.damage(v, None, dmg, 0, DMG_NORMAL, False, False)

    def fx_AOsh(self, c):
        self.fx_AUcs(c)                                           # NOTES_spells §5.17: the carrion swarm wave [obs]

    def fx_ANbf(self, c):
        self.fx_AUcs(c)                                           # §5.17: the BNbf DoT needs BNdh (A-SP75)

    def fx_AEsh(self, c):
        self.fx_AHtb(c)                                           # homing missile at the profile speed (§5.3)

    def fl_units(self, u, tx, ty, lv):
        """§5.48 (0x6f4f8820): s = Area + D, L = max(C − s, s); circles at distance Area + t along caster → (tx, ty),
        t = 0, then t + the previous radius, the last at t = L, radius Area + (D − Area)·t / L; each unit once (serial
        order inside a circle), then sorted by the distance to (tx, ty), ties serial descending [inf: field +0xc]."""
        A, D, C = lv['Area'], lv['D'], lv['C']
        vx, vy = tx - u.x, ty - u.y
        ln = math.sqrt(vx * vx + vy * vy)
        if ln < 1e-6:
            f = u.facing * math.pi / 180.0
            vx, vy, ln = math.cos(f), math.sin(f), 1.0
        s = r32(A + D)
        L = r32(C - s) if C - s > s else s
        seen, out = set(), []
        t, rp = 0.0, 0.0
        for _i in range(256):
            t = r32(t + rp)
            last = t >= L
            if last:
                t = L
            k = (A + t) / ln
            cx, cy = u.x + vx * k, u.y + vy * k
            r = r32(A + (D - A) * t / L) if L > 0 else r32(A)
            for v in self.in_area(cx, cy, r, u, lv['targs']):
                if v.serial not in seen:
                    seen.add(v.serial)
                    out.append(v)
            rp = r
            if last or L <= 0 or r <= 0:
                break
        out.sort(key=lambda v: ((v.x - tx) * (v.x - tx) + (v.y - ty) * (v.y - ty), -v.serial))
        return out[:max(0, int(lv['B']))]

    def fx_ANfl(self, c):
        """NOTES_abilities §5.48 (A-SP71): DataA LIGHTNING to each of the first trunc(DataB) units (instant, as AOcl)."""
        u, t, lv = c.u, c.target, c.lv
        if t is None:
            return
        for v in self.fl_units(u, t.x, t.y, lv):
            if v.alive and not v.dead:
                self.eng.damage(v, u, r32(lv['A']), 0, DMG_LIGHTNING, False, False)

    # ------------------------------------------------------------ NOTES_spells §5.14 flame strike (A-SP73)
    def flame_start(self, c):
        """XHfs at the point (DataF ≠ 0 = XHf2: the same model [inf]): lifetime Dur (+0x9c = 0), the full-damage
        phase HeroDur, members = the caster's targs units within Area (centre distance) [obs 0x6f4f6fc0, 0x6f4f5b40]."""
        lv, now = c.lv, self.eng.now()
        bc = lv['BuffID'][0] if lv['BuffID'] else 'BHfs'
        self.flames.append({'u': c.u, 'lv': lv, 'x': c.x, 'y': c.y, 'end': now + steps_of(lv['Dur']),
                            'ph': now + steps_of(lv['HeroDur']), 'code': _rc(bc), 'mem': []})

    def step_flames(self):
        """per step: an ended strike lets its members go; else members that left (or died / fail targs) go first, then
        new members enter in serial order [inf: the game's enter / leave events, A-SP73]."""
        now, eng, ab = self.eng.now(), self.eng, self.ab
        for f in list(self.flames):
            eng.wk += 1
            if now >= f['end']:
                for v in f['mem']:
                    self.flame_leave(f, v)
                self.flames.remove(f)
                continue
            r = f['lv']['Area']
            inside = []
            for v in sorted((v for v in eng.query(f['x'], f['y'], r + eng.max_coll) if v.alive and not v.dead),
                            key=lambda v: v.serial):
                dx, dy = v.x - f['x'], v.y - f['y']
                if dx * dx + dy * dy <= r * r and ab.targ_ok(f['u'], v, f['lv']['targs']):
                    inside.append(v)
            for v in list(f['mem']):
                if v not in inside:
                    f['mem'].remove(v)
                    self.flame_leave(f, v)
            for v in inside:
                if v not in f['mem']:
                    f['mem'].append(v)
                    self.flame_enter(f, v)

    def flame_enter(self, f, v):
        """0x6f4f5c20: BHfs — amounts DataA / DataC (× DataE on a structure), periods DataB / DataD, the strike's phase
        end; a unit that has it gets the new values (tick phase kept); reference count + 1."""
        lv, ab = f['lv'], self.ab
        k = lv['E'] if v.is_bldg else 1.0
        vals = {'a1': r32(lv['A'] * k), 'a2': r32(lv['C'] * k), 'p1': lv['B'], 'p2': lv['D'], 'ph': f['ph']}
        src = f['u'] if f['u'].alive and not f['u'].dead else None
        bf = ab.find_buff(v, f['code'])
        if bf is not None and bf.kind == 'flame':
            bf.vals.update(vals)
            bf.vals['ref'] += 1
            bf.src = src
            return
        bf, _new = ab.add_buff(v, f['code'], 'flame', src, 0)
        bf.vals = vals
        bf.vals['ref'] = 1
        bf.dtype = 8                                              # FIRE (0x100) [obs]
        ab.at(self.eng.now() + ab_steps(0.01), 'dot', bf, None)

    def flame_leave(self, f, v):
        bf = self.ab.find_buff(v, f['code'])
        if bf is None or bf.kind != 'flame':
            return
        bf.vals['ref'] -= 1
        if bf.vals['ref'] <= 0:
            self.ab.remove_buff(bf)

    # ------------------------------------------------------------ NOTES_spells §5.20 blizzard / rain of fire (A-SP79)
    def fx_AHbz(self, c):
        self.wave_start(c)

    def fx_ANrf(self, c):
        self.wave_start(c)

    def wave_start(self, c):
        """0x6f5f5a30 → 0x6f5f5840 / 0x6f5f4230: the effect at the point; v = d = max(trunc(A), 1) waves; the first visual
        wave at once, the first damage wave after 0.9 s, both then every Cast."""
        lv = c.lv
        n = max(int(lv['A']), 1)
        w = {'u': c.u, 'lv': lv, 'x': c.x, 'y': c.y, 'vis': n, 'dmg': n, 'cast': c, 'code': c.code}
        self.waves.append(w)
        self.wave_vis(w)
        self.ab.at(self.eng.now() + steps_of(WAVE_FIRST), 'wvdmg', w, None)

    def wave_vis(self, w):
        """0xd01b0 (0x6f5f4b10 / 0x6f5f4ba0): v − 1, re-armed after Cast while v > 0; the shards: n = clamp(trunc(C), 1, 32)
        slots shuffled on the JASS stream (i = 1 … n − 1: below(i + 1)), then game stream 4: 2 draws + n − 1 when n > 5,
        1 draw, 2 per remaining shard (art only)."""
        if w['vis'] <= 0:
            return
        w['vis'] -= 1
        if w['vis'] > 0:
            self.ab.at(self.eng.now() + steps_of(w['lv']['Cast']), 'wvvis', w, None)
        st = self.eng.streams
        n = min(max(int(w['lv']['C']), 1), WAVE_SHARDS)
        for i in range(1, n):
            st.jass.below(i + 1)
        g = st.game[WAVE_STREAM]
        if n > 5:
            g.unit()
            g.unit()
            n -= 1
        g.unit()
        for _ in range(2 * n):
            g.unit()

    def wave_stop(self, w):
        """the cast's end / interrupt (0x6f5f5b10 → 0x6f5f49f0): d −= v, v = 0 (d = 0 cancels the damage timer)."""
        w['dmg'] -= w['vis']
        w['vis'] = 0
        w['cast'] = None
        if w['dmg'] <= 0 and w in self.waves:
            self.waves.remove(w)

    def wave_dmg(self, w):
        """0xd01b1 (0x6f5f4f50): the splash 0x6f1fb180 — one ring (Area, B), the class's damage type, attack type 0, source
        the caster while alive, the targs units in serial order; per target (0x6f5f5050): the burn (not magic immune,
        HeroDur / Dur ≠ 0: BuffID[0] life DoT, E per s for dur + 0.01), amount B (F ≠ 0 and F < B · n: F / n), × D on a
        structure, skipped while the target's wave marker of this class runs (else marked for Cast); then d − 1."""
        if w['dmg'] <= 0:
            return
        eng, ab, lv, u = self.eng, self.ab, w['lv'], w['u']
        now = eng.now()
        src = u if u.alive and not u.dead else None
        hits = self.in_area(w['x'], w['y'], lv['Area'], u, lv['targs'])
        n = len(hits)
        for v in hits:
            if not v.alive or v.dead:
                continue
            d = ab.dur(lv, v)
            if v.magic_immune <= 0 and d != 0 and lv['BuffID']:
                bf, new = ab.add_buff(v, _rc(lv['BuffID'][0]), 'poison', src, d + 0.01)
                bf.dot, bf.move, bf.atk, bf.dtype = lv['E'], 0.0, 0.0, DMG_FIRE
                ab.apply(bf)
                if new:
                    ab.at(now + steps_of(0.01), 'dot', bf, None)
            amt = lv['B']
            if lv['F'] != 0 and lv['F'] < amt * n:
                amt = lv['F'] / n
            if v.is_bldg:
                amt = amt * lv['D']
            key = (v.serial, w['code'])
            if self.wv_mark.get(key, -1) > now:
                continue
            self.wv_mark[key] = now + steps_of(lv['Cast'])
            eng.damage(v, src, r32(amt), 0, WAVES[w['code']], False, False)
        w['dmg'] -= 1
        if w['dmg'] > 0:
            ab.at(now + steps_of(lv['Cast']), 'wvdmg', w, None)
        elif w in self.waves:
            self.waves.remove(w)

    # ------------------------------------------------------------ NOTES_spells §5.15 volcano (A-SP82)
    def fx_ANvc(self, c):
        """0x6f4f30e0: the caster loses Bvul / BHds / BOvd; XNvc at the point (0x6f4f3560): spacing = Area / (trunc(A) + 0.5),
        total T = max(1, float(trunc(B)) × C); the first wave after 1.0 s, then every C; the effect dies (0xd019f) at
        T − 0.5; the channel float(trunc(B)) × C ends it too."""
        u, lv = c.u, c.lv
        for b in ('Bvul', 'BHds', 'BOvd'):
            self.ab.remove_buff_code(u, _rc(b))
        sp = lv['Area'] / (float(int(lv['A'])) + 0.5)
        tot = max(1.0, float(int(lv['B'])) * lv['C'])
        v = {'u': u, 'lv': lv, 'x': c.x, 'y': c.y, 'sp': sp, 'cast': c, 'alive': True, 'gen': 0,
             'xs': [], 'ys': [], 'ord': [], 'cnt': 0, 'gap': 0.0}
        self.volcs.append(v)
        now = self.eng.now()
        self.ab.at(now + steps_of(VOLC_FIRST), 'vcwave', v, None)
        self.ab.at(now + steps_of(tot - 0.5), 'vcend', v, None)

    def volc_end(self, v):
        if v['alive']:
            v['alive'] = False
            if v in self.volcs:
                self.volcs.remove(v)

    def volc_wave(self, v):
        """0xd01c3 (0x6f4f3cd0): n = min(1 + Σ_{i<R} (5 + 2i), 32) rocks (R = trunc(A)); the slots 0 … n − 1 shuffled on
        the JASS stream (i = 1 … n − 1: below(i + 1), swap); stream 0x2a: the centre rock (slot n − 1) at the point +
        0.15·sp·(U − 0.5) per axis (x first); θ0 = 2π·below(100) / 100; ring i: θ0 (+ π on even i), 5 + 2i rocks at
        φ = θi + j·2π / (5 + 2i), radius sp·(i + 0.5) + 0.5·sp·(0.75 + (below(10) + 20) / 100), + 0.15·sp·U per axis,
        into the next lower slot; the next wave after C; the drop gap 0.5·C / n − 0.01; the first rock at once."""
        if not v['alive']:
            return
        lv = v['lv']
        st = self.eng.streams
        g = st.game[VOLC_STREAM]
        R = int(lv['A'])
        n = 1
        for i in range(R):
            n += 5 + 2 * i
        n = min(n, VOLC_ROCKS)
        order = list(range(n))
        for i in range(1, n):
            j = st.jass.below(i + 1)
            order[i], order[j] = order[j], order[i]
        x, y, sp = v['x'], v['y'], v['sp']
        h = 0.3 * (sp * 0.5)
        xs, ys = [0.0] * n, [0.0] * n
        rem = n - 1
        xs[rem] = x + h * (g.unit() - 0.5)
        ys[rem] = y + h * (g.unit() - 0.5)
        th0 = 2.0 * math.pi * g.below(100) / 100.0
        for i in range(R):
            if rem == 0:
                break
            th = th0 + (math.pi if i % 2 == 0 else 0.0)
            cnt = 5 + 2 * i
            stp = 2.0 * math.pi / cnt
            for j in range(cnt):
                if rem == 0:
                    break
                s = 0.75 + (g.below(10) + 20) / 100.0
                rr = 0.5 * sp + sp * i + 0.5 * sp * s
                phi = th + j * stp
                jx = h * g.unit()
                jy = h * g.unit()
                rem -= 1
                xs[rem] = rr * math.cos(phi) + jx + x
                ys[rem] = rr * math.sin(phi) + jy + y
        now = self.eng.now()
        self.ab.at(now + steps_of(lv['C']), 'vcwave', v, None)
        v['xs'], v['ys'], v['ord'], v['cnt'] = xs, ys, order, n
        v['gap'] = 0.5 * lv['C'] / n - 0.01
        v['gen'] += 1
        self.volc_drop(v, v['gen'])

    def volc_drop(self, v, gen):
        """0xd01b0 (0x6f4f4190): cnt − 1 → the rock of slot ord[cnt] (an RNvc artillery from the point, landing after
        VOLC_FLIGHT [inf]); while cnt > 0 the next drop after gap × (50 + below(50)) / 100 (stream 0x2a)."""
        if not v['alive'] or gen != v['gen'] or v['cnt'] <= 0:
            return
        v['cnt'] -= 1
        k = v['cnt']
        sl = v['ord'][k]
        now = self.eng.now()
        self.ab.at(now + steps_of(VOLC_FLIGHT), 'vcrock', v, (v['xs'][sl], v['ys'][sl]))
        if k > 0:
            m = self.eng.streams.game[VOLC_STREAM].below(50)
            self.ab.at(now + steps_of(v['gap'] * (m + 50) / 100.0), 'vcdrop', v, gen)

    def volc_rock(self, v, p):
        """CArtilleryVolcano impact (0x6f4f4f40 / 0x6f4f4e80 / 0x6f4f53f0): the targs units (serial order) within sp of the
        landing point: within 0.5·sp → E, else (F ≥ 0.001) E·F; × D on a structure; magic immune → event only [inf, as
        the inferno]; the stun (BPSE for Dur / HeroDur) unless a structure or invulnerable; the burn BuffID[1] is not
        modelled; source the caster while alive."""
        x, y = p
        u, lv, eng = v['u'], v['lv'], self.eng
        sp = v['sp']
        full = 0.5 * sp
        src = u if u.alive and not u.dead else None
        for t in self.in_area(x, y, sp, u, lv['targs']):
            if not t.alive or t.dead:
                continue
            dx, dy = t.x - x, t.y - y
            if dx * dx + dy * dy <= full * full:
                f = 1.0
            elif lv['F'] >= 0.001:
                f = lv['F']
            else:
                continue
            if t.magic_immune > 0:
                eng.damaged_event(t, src, 0.0)
                continue
            amt = lv['E'] * f
            if t.is_bldg:
                amt = amt * lv['D']
            eng.damage(t, src, r32(amt), 0, DMG_FIRE, False, False)
            if t.alive and not t.dead and not t.is_bldg and not t.invulnerable:
                self.ab.stun(t, src, self.ab.dur(lv, t))

    # ------------------------------------------------------------ NOTES_spells §5.16 locust swarm (A-SP83)
    def fx_AUls(self, c):
        """0x6f59f500: the previous swarm of this ability ends first (0x6f59f8d0(0)); n = clamp(trunc(A), 1, 40); the
        first locust at once (its spawn re-arms after B); the end event after Dur (+ the +0x9c holder, 0)."""
        u, lv = c.u, c.lv
        key = (u.serial, c.aid)
        old = self.swarms.get(key)
        if old is not None:
            self.lc_end(old, False)
        sw = {'u': u, 'lv': lv, 'n': max(1, min(LC_MAX, int(lv['A']))), 'spawned': 0, 'gone': 0, 'slots': [],
              'alive': True, 'spawning': True, 'key': key}
        self.swarms[key] = sw
        self.lc_spawn(sw)
        self.ab.at(self.eng.now() + steps_of(lv['Dur']), 'lcend', sw, None)

    def lc_spawn(self, sw):
        """0x6f59f740 (also event 0xd01c1): one UnitID unit for the caster's owner at the caster, facing 0.0, no SUMMON;
        while spawned < n the next after B; its Aloc gets (caster, Area, trunc(C), D, E) and the loop order 0xd024d."""
        u, lv, N = sw['u'], sw['lv'], self.N
        if not sw['alive'] or not sw['spawning'] or not u.alive or u.dead:
            return
        tid = lv.get('UnitID') or ''
        if len(tid) != 4 or _rc(tid) not in N.unit_types:
            return
        nu = N.create_unit(u.owner, _rc(tid), u.x, u.y, 0.0)
        if nu is None:
            return
        sw['slots'].append(nu)
        sw['spawned'] += 1
        if sw['spawned'] < sw['n']:
            self.ab.at(self.eng.now() + steps_of(lv['B']), 'lcspawn', sw, None)
        if any(b == 'Aloc' for _a, b, _l in self.ab.abil_list(nu)):
            nu.lc = {'c': u, 'sw': sw, 'area': lv['Area'], 'cap': int(lv['C']), 'D': lv['D'], 'E': lv['E'], 'acc': 0.0,
                     'tgt': None, 'mode': '', 'gen': 0, 'dgen': 0, 'ending': False}
            self.lc_loop(nu)

    def lc_end(self, sw, final):
        """0x6f59f8d0: the spawns stop; every live slot with an Aloc returns (0x6f5a0200); not final (a new cast): the
        slots are cleared (those locusts no longer count) and the swarm is over."""
        sw['spawning'] = False
        for lu in list(sw['slots']):
            if lu is not None and lu.alive and not lu.dead and lu.lc is not None:
                self.lc_return_end(lu)
        if not final:
            for lu in sw['slots']:
                if lu is not None and lu.lc is not None:
                    lu.lc['sw'] = None
            sw['slots'] = []
            self.lc_over(sw)

    def lc_over(self, sw):
        sw['alive'] = False
        sw['spawning'] = False
        if self.swarms.get(sw['key']) is sw:
            del self.swarms[sw['key']]

    def lc_leash(self, lu):
        """0x6f5a07a0: the caster alive and dist² ≤ (3 · Area)²."""
        lc = lu.lc
        c = lc['c']
        if c is None or not c.alive or c.dead:
            return False
        dx, dy = c.x - lu.x, c.y - lu.y
        r = LC_LEASH * lc['area']
        return dx * dx + dy * dy <= r * r

    def lc_unreg(self, lu):
        """0x6f5a0d10: the registered target leaves the (target, owner) count."""
        t = lu.lc['tgt']
        if t is not None:
            k = (t.serial, lu.owner.id)
            self.lc_cnt[k] = self.lc_cnt.get(k, 1) - 1
            lu.lc['tgt'] = None

    def lc_kill(self, lu):
        lu.lc['gen'] += 1
        self.N.kill_unit(lu, None)

    def lc_return_end(self, lu):
        """0x6f5a0200 at the swarm end: leash broken → dies (one step later, both VMs [inf]); else the 7.0 s death timer and order 0xd024e
        (0x6f5a08f0): unregister, leash check, flag bit 7 (ending), return (0x6f5a09b0)."""
        lc = lu.lc
        lc['dgen'] += 1
        if not self.lc_leash(lu):
            lc['gen'] += 1
            lc['mode'] = 'end'
            self.ab.at(self.eng.now() + 1, 'lcdie', lu, lc['dgen'])      # dies at the next step [inf]
            return
        self.ab.at(self.eng.now() + steps_of(LC_DIE), 'lcdie', lu, lc['dgen'])
        self.lc_unreg(lu)
        lc['ending'] = True
        self.lc_go_return(lu)

    def lc_go_return(self, lu):
        """0x6f5a09b0: move to the caster (0xd0174 with 50.0), then the arrival event 0xd029b."""
        lc = lu.lc
        lc['gen'] += 1
        lc['mode'] = 'ret'
        self.eng.issue_order(lu, E_MOVE, None, None, lc['c'])

    def lc_loop(self, lu):
        """loop order 0xd024d (0x6f5a0430): unregister; leash broken → dies; acc > E or within 0.001 of it → return;
        else wander: θ = 2π·U, r = Area·U' (game stream 0x1b, θ first), the point = caster + r·(cos θ, sin θ)
        (math cos / sin, no map clamp [inf]); move there, then the target search 0xd029a."""
        lc = lu.lc
        if lc is None or not lu.alive or lu.dead:
            return
        lc['gen'] += 1
        self.lc_unreg(lu)
        if not self.lc_leash(lu):
            self.lc_kill(lu)
            return
        if lc['acc'] > lc['E'] or abs(lc['acc'] - lc['E']) <= LC_EPS:
            self.lc_go_return(lu)
            return
        g = self.eng.streams.game[LC_STREAM]
        th = 2.0 * math.pi * g.unit()
        r = lc['area'] * g.unit()
        c = lc['c']
        lc['mode'] = 'wan'
        if not self.eng.issue_order(lu, E_MOVE, c.x + r * math.cos(th), c.y + r * math.sin(th), None):
            lc['mode'] = ''

    def lc_moved(self, lu, done):
        """engine movement step of a locust: the wander point reached → the search (next step); returning: within
        50.0 of the caster (or the point once the caster is gone) → the arrival (next step). -> False (the engine's
        own arrival is not used)."""
        lc = lu.lc
        m = lc['mode']
        if m == 'ret':
            c = lc['c']
            if not done and c is not None and c.alive and not c.dead:
                dx, dy = c.x - lu.x, c.y - lu.y
                done = dx * dx + dy * dy <= LC_NEAR * LC_NEAR
            if done:
                self.lc_halt(lu, 'arr')
                self.ab.at(self.eng.now() + 1, 'lcarr', lu, lc['gen'])
        elif m == 'wan' and done:
            self.lc_halt(lu, 'seek')
            self.ab.at(self.eng.now() + 1, 'lcseek', lu, lc['gen'])
        return False

    def lc_halt(self, lu, mode):
        lu.lc['mode'] = mode
        lu.target = None
        lu.path = None
        self.N.moving.discard(lu)

    def lc_seek(self, lu):
        """0xd029a (0x6f5a06e0): the acquisition (0x6f5542f0) with Aloc's filter 0x6f5a0b90 — a target with trunc(C)
        or more locusts of this owner registered is refused; rank = max(1, trunc(1024·(1 − life / max life))) [obs];
        the higher rank wins, then the strictly nearer, then the lower serial [inf]; found: registered (0x6f5a0c40)
        and attacked once (0xd0011), then the loop; none: the loop."""
        lc = lu.lc
        eng = self.eng
        best, bk = None, None
        if eng.has_weapon(lu, 0) or eng.has_weapon(lu, 1):
            acq = float(lu.data.get('acquire', 0) or 0)
            rmax = max(eng.rng_of(lu, i) for i in (0, 1) if eng.has_weapon(lu, i))
            qr = (acq if acq > 0 else rmax) + lu.coll + eng.max_coll
            own = lu.owner.id
            for t in eng.query(lu.x, lu.y, qr):
                eng.wk += 1
                if t is lu or not t.alive or t.dead or t.hidden or eng.ally(lu.owner, t.owner):
                    continue
                wi = eng.pick_weapon(lu, t)
                if wi < 0 or (t.invisible and not self.ab.visible_to(lu, t)):
                    continue
                rad = acq if acq > 0 else eng.rng_of(lu, wi)
                dx, dy = t.x - lu.x, t.y - lu.y
                d2 = dx * dx + dy * dy
                rr = rad + lu.coll + t.coll
                if d2 > rr * rr + 0.001:
                    continue
                if self.lc_cnt.get((t.serial, own), 0) >= lc['cap']:
                    continue
                ratio = 1.0 if t.life == t.maxlife else r32(t.life / t.maxlife)
                rank = max(1, int(r32(r32(1.0 - ratio) * LC_RANK)))
                key = (rank, -d2, -t.serial)
                if bk is None or key > bk:
                    best, bk = t, key
        if best is None:
            self.lc_loop(lu)
            return
        k = (best.serial, lu.owner.id)
        self.lc_cnt[k] = self.lc_cnt.get(k, 0) + 1
        lc['tgt'] = best
        lc['gen'] += 1
        lc['mode'] = 'atk'
        if not eng.issue_order(lu, E_ATTACKONCE, None, None, best):
            self.lc_loop(lu)

    def lc_hit(self, lu, fired):
        """the attack-once order is done after its damage point (and backswing when it fired) [inf] → the loop."""
        lc = lu.lc
        lc['mode'] = 'hit'
        lc['gen'] += 1
        now = self.eng.now()
        at = lu.bs_end if fired and lu.bs_end > now else now + 1
        self.ab.at(at, 'lcloop', lu, lc['gen'])

    def lc_dropped(self, lu):
        """the attack-once target became invalid before the hit → the loop at the next step."""
        lu.order = E_ATTACKONCE                                   # still on its order: no own acquisition
        self.lc_hit(lu, False)

    def lc_dealt(self, lu, amount):
        """Aloc slot +0x170 (0x6f5a0b30): acc += D × the damage dealt (min(final, life before))."""
        lc = lu.lc
        lc['acc'] = r32(lc['acc'] + r32(lc['D'] * amount))

    def lc_arrive(self, lu):
        """0xd029b (0x6f5a0a30): leash valid → the caster gains acc life (held at max life [inf]); acc := 0; ending →
        dies 0.01 s later (the death timer re-armed); else the loop."""
        lc = lu.lc
        ok = self.lc_leash(lu)
        if ok:
            c = lc['c']
            c.life = r32(min(c.life + lc['acc'], c.maxlife))
        lc['acc'] = 0.0
        if lc['ending']:
            lc['mode'] = 'end'
            lc['dgen'] += 1
            self.ab.at(self.eng.now() + steps_of(LC_LAST), 'lcdie', lu, lc['dgen'])
            return
        self.lc_loop(lu)

    def lc_timer(self, kind, a, b):
        """abilities.step dispatch of the §5.16 timers."""
        if kind == 'lcspawn':
            self.lc_spawn(a)
        elif kind == 'lcend':
            if a['alive']:
                self.lc_end(a, True)
        else:
            lc = a.lc
            if lc is None or not a.alive or a.dead:
                return
            if kind == 'lcdie':
                if b == lc['dgen']:
                    self.lc_kill(a)
            elif b == lc['gen']:
                if kind == 'lcloop':
                    self.lc_loop(a)
                elif kind == 'lcseek':
                    self.lc_seek(a)
                elif kind == 'lcarr':
                    self.lc_arrive(a)

    def lc_unit_gone(self, u):
        """death / removal (0xd01a0 / 0xd01a4 → 0x6f59fa70): a listed locust leaves its slot, gone + 1, all n gone →
        the swarm is over; a swarm caster (0xd01a2 → 0x6f59f9d0): every listed locust dies (at the next step [inf])."""
        lc = u.lc
        if lc is not None and lc['mode'] != 'gone':
            self.lc_unreg(u)
            lc['mode'] = 'gone'
            lc['gen'] += 1
            lc['dgen'] += 1
            sw = lc['sw']
            if sw is not None and u in sw['slots']:
                sw['slots'][sw['slots'].index(u)] = None
                sw['gone'] += 1
                if sw['gone'] >= sw['n']:
                    self.lc_over(sw)
        if self.swarms:
            for key in [k for k in self.swarms if k[0] == u.serial]:
                sw = self.swarms[key]
                for lu in sw['slots']:
                    if lu is not None and lu.alive and not lu.dead and lu.lc is not None:
                        lu.lc['dgen'] += 1
                        self.ab.at(self.eng.now() + 1, 'lcdie', lu, lu.lc['dgen'])
                self.lc_over(sw)

    # ------------------------------------------------------------ NOTES_spells §5.18 stampede (A-SP77)
    def fx_ANst(self, c):
        """0x6f535a50: θ = caster → point (shorter than 1e-5: +x); the caster loses Bvul / BHds / BOvd; the first beast
        at once, then one every 1 / DataA s while the channel (Dur) lasts."""
        u, lv = c.u, c.lv
        dx, dy = c.x - u.x, c.y - u.y
        d = math.sqrt(dx * dx + dy * dy)
        cs, sn = (dx / d, dy / d) if d >= 1e-5 else (1.0, 0.0)
        for b in ('Bvul', 'BHds', 'BOvd'):
            self.ab.remove_buff_code(u, _rc(b))
        st = {'u': u, 'aid': c.aid, 'lv': lv, 'cs': cs, 'sn': sn, 'cast': c, 'alive': True}
        self.stamps.append(st)
        self.stamp_spawn(st, True)
        p = steps_of(1.0 / lv['A']) if lv['A'] > 0 else 0
        if p > 0:
            self.ab.at(self.eng.now() + p, 'stspawn', st, p)

    def stamp_tick(self, st, p):
        if not st['alive']:
            return
        self.stamp_spawn(st, False)
        self.ab.at(self.eng.now() + p, 'stspawn', st, p)

    def stamp_spawn(self, st, first):
        """0x6f535d10 / 0x6f536340: the lane (the cast's first beast: 2; else a free one, randint(free) on stream 0x27;
        all used: cleared, randint(5)), then u (stream 0x27); w = 2·Area / 5, y′ = −Area + lane·w + u·w; the beast runs
        from caster + R(θ)(−Area, y′) to caster + R(θ)(Area, y′) at the ability's missile speed."""
        u, lv, eng = st['u'], st['lv'], self.eng
        g = eng.streams.game[STAMPEDE_STREAM]
        key = (u.serial, st['aid'])
        bits = self.st_lanes.get(key, 0)
        full = (1 << STAMPEDE_LANES) - 1
        if first:
            lane = 2
        else:
            free = [i for i in range(STAMPEDE_LANES) if not bits & (1 << i)]
            if free:
                lane = free[g.below(len(free))]
            else:
                bits = 0
                lane = g.below(STAMPEDE_LANES)
        if bits == full:
            bits = 0
        self.st_lanes[key] = bits | (1 << lane)
        uu = g.unit()
        A = lv['Area']
        w = A * 2.0 / STAMPEDE_LANES
        yp = -A + lane * w + uu * w
        cs, sn = st['cs'], st['sn']
        r = self.ab.rec(st['aid']) or {}
        sp = float(int(r.get('mspeed', 0) or 0)) or 1000.0
        b = {'st': st, 'x0': u.x - cs * A - sn * yp, 'y0': u.y - sn * A + cs * yp, 'dx': cs, 'dy': sn,
             't0': eng.now(), 'sp': sp, 'life': steps_of(2.0 * A / sp), 'per': steps_of(max(lv['B'] / sp,
                                                                                            STAMPEDE_MIN_CHECK))}
        self.ab.at(eng.now() + b['per'], 'stbeast', b, None)

    def stamp_beast(self, b):
        """0x6f535740: every max(DataB / speed, 0.02) s while alive (2·Area / speed): the first unit (serial) within
        DataB that passes the caster's targs (not the caster) stops it; DataE s later DataC UNIVERSAL to the targs units
        within DataD of that point (0x6f1fb900), source the caster."""
        eng, st = self.eng, b['st']
        now = eng.now()
        el = now - b['t0']
        if el > b['life']:
            return
        lv, u = st['lv'], st['u']
        trav = b['sp'] * el / SPS
        x, y = b['x0'] + b['dx'] * trav, b['y0'] + b['dy'] * trav
        hit = [v for v in self.in_area(x, y, lv['B'], u, lv['targs']) if v is not u]
        if hit:
            self.ab.at(now + steps_of(lv['E']), 'stdmg', (x, y, st), None)
            return
        if el + b['per'] <= b['life']:
            self.ab.at(now + b['per'], 'stbeast', b, None)

    def stamp_damage(self, ex):
        x, y, st = ex
        u, lv = st['u'], st['lv']
        src = u if u.alive and not u.dead else None
        for v in self.in_area(x, y, lv['D'], u, lv['targs']):
            if v.alive and not v.dead:
                self.eng.damage(v, src, r32(lv['C']), 0, DMG_UNIVERSAL, False, False)

    # ------------------------------------------------------------ NOTES_spells §3.5 monsoon / §5.6 starfall (A-SP74)
    def fx_ANmo(self, c):
        self.monsoon_start(c, c.x, c.y, MONSOON_ENTER)

    def fx_AEsf(self, c):
        self.monsoon_start(c, c.u.x, c.u.y, STARFALL_ENTER)          # §5.6: centred on the caster

    def monsoon_start(self, c, x, y, stream):
        """§3.5 effect (0x6f4a3890): the caster loses Bvul / BHds / BOvd; the effect lives Dur (+0x9c = 0) with Area and
        the level's values; the drain code = BuffID[0] (default ANmd / AEsd)."""
        u, lv = c.u, c.lv
        for b in ('Bvul', 'BHds', 'BOvd'):
            self.ab.remove_buff_code(u, _rc(b))
        dc = lv['BuffID'][0] if lv['BuffID'] else ('AEsd' if c.code == 'AEsf' else 'ANmd')
        self.monsoons.append({'u': u, 'lv': lv, 'x': x, 'y': y, 'end': self.eng.now() + steps_of(lv['Dur']),
                              'cast': c, 'stream': stream, 'code': _rc(dc), 'mem': []})

    def monsoon_end(self, m):
        if m in self.monsoons:
            self.monsoons.remove(m)
        for v in m['mem']:
            self.monsoon_leave(m, v)
        m['mem'] = []

    def step_monsoons(self):
        """per step (as the flame strike, A-SP73): an ended effect lets its members go; else the leavers first (entry
        order), then the new members (serial order) [inf: the game's region enter / leave events]."""
        now, eng, ab = self.eng.now(), self.eng, self.ab
        for m in list(self.monsoons):
            eng.wk += 1
            if now >= m['end']:
                self.monsoon_end(m)
                continue
            inside = self.in_area(m['x'], m['y'], m['lv']['Area'], m['u'], m['lv']['targs'])
            for v in list(m['mem']):
                if v not in inside:
                    m['mem'].remove(v)
                    self.monsoon_leave(m, v)
            for v in inside:
                if v not in m['mem']:
                    m['mem'].append(v)
                    self.monsoon_enter(m, v)

    def monsoon_enter(self, m, v):
        """0x6f4a3690: one draw U0 first (monsoon stream 0x1c, starfall 0x12), also for a unit that has the drain (then
        only its count rises); a new drain: DataA (× DataC on a structure), period DataB, the first event after U0."""
        ab, lv = self.ab, m['lv']
        u0 = self.eng.streams.game[m['stream']].unit()
        bf = ab.find_buff(v, m['code'])
        if bf is not None and bf.kind == 'mdrain':
            bf.vals['ref'] += 1
            return
        bf, _new = ab.add_buff(v, m['code'], 'mdrain', m['u'], 0)
        bf.vals = {'ref': 1, 'amt': r32(lv['A'] * lv['C']) if v.is_bldg else r32(lv['A']), 'per': lv['B']}
        ab.at(self.eng.now() + steps_of(u0), 'mdrain', bf, 0)

    def monsoon_leave(self, m, v):
        bf = self.ab.find_buff(v, m['code'])
        if bf is None or bf.kind != 'mdrain':
            return
        bf.vals['ref'] -= 1
        if bf.vals['ref'] <= 0:
            self.ab.remove_buff(bf)

    def monsoon_drain(self, bf, ev):
        """0x6f4a2f70: every event draws u (stream 0x12); start → strike check after B − u; check → (the caster is
        there) the strike after u + 0.1; strike → the next check after B − u, then DataA UNIVERSAL, attack type 0."""
        if not bf.alive:
            return
        now, ab = self.eng.now(), self.ab
        u = self.eng.streams.game[MONSOON_CYCLE].unit()
        src = bf.src if bf.src is not None and bf.src.alive and not bf.src.dead else None
        per = bf.vals['per']
        if ev == 1 and src is not None:
            ab.at(now + steps_of(u + 0.1), 'mdrain', bf, 2)
            return
        ab.at(now + steps_of(per - u), 'mdrain', bf, 1)
        if ev == 2 and src is not None:
            t = bf.unit
            if t.alive and not t.dead:
                self.eng.damage(t, src, bf.vals['amt'], 0, DMG_UNIVERSAL, False, False)

    # ------------------------------------------------------------ §5.50 phoenix (A-SP72)
    def phx_types(self, u, lv):
        """(normal, alternate): alternate = UnitID; normal = DataA only when the unit's type is DataA or UnitID, else
        its type [obs 0x6f56fed0 / 0x6f56ff40]."""
        alt = str(lv.get('UnitID') or '')
        alt = _rc(alt) if len(alt) == 4 and _rc(alt) in self.N.unit_types else None
        da = str((lv.get('S') or {}).get('A', '') or '')
        da = _rc(da) if len(da) == 4 and _rc(da) in self.N.unit_types else None
        normal = da if da is not None and u.tid in (da, alt) else u.tid
        return normal, alt

    def phx_counters(self, u, way, d):
        if way == 'alt':
            u.invuln_n += d
            u.invulnerable = AVUL_ID in u.abilities or u.invuln_n > 0
        u.magic_immune += d

    def phx_veto(self, u):
        """the death claim (priority 3, 0x6f5af650): an Aphx unit not in its alternate form does not die; a running
        morph cast claims as it is, else the unit orders itself 852482 (no order taken → it dies) [obs]."""
        for _code, b, lv in self.ab.abil_list(u):
            if b != 'Aphx' or lv is None:
                continue
            _n, alt = self.phx_types(u, lv)
            if alt is None or u.tid == alt:
                return False
            c = u.cast
            if (c is not None and c.code == 'Aphx') or self.eng.issue(u, CLASS_ORDER['Aphx']):
                u.last_src = None                                 # the killer reference is reset [obs]
                return True
            return False
        return False

    def fx_Aphx(self, c):
        """§5.50 morph start: to the alternate (invulnerable + magic immune for the morph time) or back to the normal
        form (magic immune); the swap at the end of the channel (Dur)."""
        u, lv = c.u, c.lv
        normal, alt = self.phx_types(u, lv)
        if alt is None:
            return
        if u.tid != alt:
            u.phx_norm, u.phx_alt = normal, alt
            c.phx = 'alt'
        else:
            if getattr(u, 'phx_alt', None) != alt:
                u.phx_norm, u.phx_alt = normal, alt
            c.phx = 'norm'
        self.phx_counters(u, c.phx, 1)

    def phx_swap(self, c):
        """the type swap in place (the same handle; life percentage kept, at least 1 [obs 0x6f1e1bf0 mode 1]); to the
        alternate: Bphx for HeroDur and the timer that orders the morph back [obs 0x6f5af390]."""
        u, way = c.u, c.phx
        c.phx = None
        if not u.alive or u.dead:
            return
        tid = u.phx_alt if way == 'alt' else u.phx_norm
        self.eng.morph_type(u, tid, food=False)                   # A-SP72: no food step (fmade 0 on the creeps)
        if u.life < 1.0:
            u.life = 1.0
        self.phx_counters(u, way, -1)
        if way == 'alt' and c.lv['HeroDur'] > 0:
            self.ab.add_buff(u, _rc('Bphx'), 'phx', u, c.lv['HeroDur'])
            self.ab.at(self.eng.now() + steps_of(c.lv['HeroDur']), 'phx', u, None)

    def phx_back(self, u):
        """0xd01db: Bphx off, the unit orders the morph back (A-SP72: also when stunned / paused)."""
        if not u.alive or u.dead or u.tid != getattr(u, 'phx_alt', None):
            return
        self.ab.remove_buff_code(u, _rc('Bphx'))
        self.eng.issue(u, CLASS_ORDER['Aphx'])

    def fx_ANab(self, c):
        self.fx_AHtb(c)                                           # §5.47: the homing missile MNab (profile speed)

    def acid_on(self, v, src, lv, dot):
        """§5.47: BNab (BuffID first) for Dur′, refreshed (tick phase kept): move + DataA, attack + DataB (the slow
        poison is passed −A / −B), armor −DataC, DoT `dot` SHADOW_STRIKE (25), 0.01 s then every DataF s."""
        ab = self.ab
        bcode = _rc(lv['BuffID'][0]) if lv['BuffID'] else _rc('BNab')
        bf, new = ab.add_buff(v, bcode, 'acid', src, ab.dur(lv, v))
        bf.move, bf.atk, bf.armor = lv['A'], lv['B'], -lv['C']
        bf.dot, bf.dtype, bf.lv = dot, 25, lv
        ab.apply(bf)
        if new:
            ab.at(self.eng.now() + steps_of(0.01), 'dot', bf, None)

    def fx_Aens(self, c):
        self.fx_AHtb(c)                                           # the same homing missile (profile speed, §5.24)

    def fx_AIwb(self, c):
        self.fx_Aweb(c)
        self.missiles[-1].code = 'Aweb'                           # CAbilityItemWeb §3: the same Mweb missile and buffs as Aweb

    def fx_Aweb(self, c):
        self.fx_AHtb(c)                                           # NOTES_abilities §5.53: the ensnare missile (Mweb)

    def in_area(self, x, y, r, u, mask, key=None):
        """the living units within r of (x, y) that pass the caster's targs, in serial order (or sorted by key)."""
        eng, ab = self.eng, self.ab
        out = []
        for v in sorted((v for v in eng.query(x, y, r + eng.max_coll) if v.alive and not v.dead),
                        key=lambda v: v.serial):
            dx, dy = v.x - x, v.y - y
            if dx * dx + dy * dy <= r * r and ab.targ_ok(u, v, mask):
                out.append(v)
        if key is not None:
            out.sort(key=key)
        return out

    def fx_AOcl(self, c):
        """§3.3 (A-SP37: all hops at once): hop n hits for DataA × (1 − DataC)^(n−1), then the closest valid unhit unit
        within Area of the current target (distance, then serial)."""
        u, lv, eng = c.u, c.lv, self.eng
        t, f, hit = c.target, 1.0, []
        n = int(lv['B'])
        while t is not None and len(hit) < max(1, n):
            hit.append(t)
            eng.damage(t, u, r32(lv['A'] * f), 0, DMG_LIGHTNING, False, False)
            f = f * (1.0 - lv['C'])
            if len(hit) >= n:
                break
            nxt = None
            for v in self.in_area(t.x, t.y, lv['Area'], u, lv['targs']):
                if v in hit or v.magic_immune > 0:
                    continue
                d2 = (v.x - t.x) * (v.x - t.x) + (v.y - t.y) * (v.y - t.y)
                if nxt is None or d2 < nxt[0]:
                    nxt = (d2, v)
            t = nxt[1] if nxt is not None else None

    # ------------------------------------------------------------ §5.36 restoration / repair (A-SP67)
    def repair_smart(self, u, t):
        """CAbilityRepair §7.3 / §7.5: a smart order on a damaged friendly unit becomes the repair order when the
        caster really has an ability that answers 0xd0038 (`Arep`, profile Order=repair). A full-life target keeps
        the plain smart path (the +0x12c test returns 0); a target failing the ability's targs mask (structure /
        mechanical) too -> plain smart (follow / move) [inf]."""
        if getattr(t, 'tname', '') != 'unit':                     # only a unit (a destructable stays a point target)
            return False
        if u.dead or not u.alive or t.dead or not t.alive:
            return False
        if not self.eng.ally(u.owner, t.owner):
            return False
        if getattr(t, 'constructing', False) or t.maxlife - t.life < 1.0:
            return False
        sp = self.spell_of(u, ORDER_IDS['repair'])                # the unit's own ability list, first answering 0xd0038
        if sp is None:
            return False
        return self.ab.targ_ok(u, t, sp[2]['targs'])              # the target must pass the repair ability's mask [inf]

    def repair_order_ok(self, u, t):
        """§7.5 [obs 0x6f464d70]: the owner unit must not be hidden (+0x20 bit 0 -> 0xaa), then the order-time
        affordability threshold `amount / 10 < 1` (the cast path keeps the `< 1` test of §7.2)."""
        if getattr(u, 'hidden', False):
            return False                             # PORT14-Build: the hidden-owner part of step 5
        st = u.owner.state
        if int(t.data.get('gold', 0) or 0) != 0 and st.get(REPAIR_GOLD, 0) // 10 < 1:
            return False
        if int(t.data.get('lumber', 0) or 0) != 0 and st.get(REPAIR_LUMBER, 0) // 10 < 1:
            return False
        return True

    def repair_ok(self, u, t):
        """can-target 0x6f464af0 (no construction help): dead 0x98, under construction 0x31 (power build not modelled),
        missing life < 1.0 → 0x1e, illusion 0xa2, the target type costs gold / lumber and the owner has < 1 → 8 / 9."""
        if t.dead or not t.alive:
            return False
        if getattr(t, 'constructing', False):
            return False
        if t.maxlife - t.life < 1.0:
            return False
        if getattr(t, 'illusion', False):
            return False
        st = u.owner.state
        if int(t.data.get('gold', 0) or 0) != 0 and st.get(REPAIR_GOLD, 0) < 1:
            return False
        if int(t.data.get('lumber', 0) or 0) != 0 and st.get(REPAIR_LUMBER, 0) < 1:
            return False
        return True

    @staticmethod
    def repair_reach(u, t, r):
        """0x6f08ce80: dist² ≤ (r + coll_a + coll_t)² + 0.001 (edge to edge, as the attack range)."""
        rr = r + u.coll + t.coll
        dx, dy = t.x - u.x, t.y - u.y
        return dx * dx + dy * dy <= rr * rr + 0.001

    def fx_Ainf(self, c):
        """NOTES_abilities §5.45 (A-SP68): Binf (BuffID first) for Dur′, refreshed: the roar damage part trunc(DataA ×
        avg + 0.5), armor + DataB, life regen + DataD (≥ 0.001); DataC (autocast range) and DataE (mana regen) not read
        / not modelled."""
        t, lv, ab = c.target, c.lv, self.ab
        if t is None or not t.alive or t.dead:
            return
        bcode = _rc(lv['BuffID'][0]) if lv['BuffID'] else _rc('Binf')
        bf, _new = ab.add_buff(t, bcode, 'roar', c.u, ab.dur(lv, t))
        bf.vals['dint'] = self.roar_damage(t, lv['A'])
        bf.armor = lv['B']
        bf.regen = lv['D'] if lv['D'] >= 0.001 else 0.0
        ab.apply(bf)

    def fx_Auhf(self, c):
        """NOTES_abilities §5.46 (A-SP69): Buhf (BuffID first) for Dur′ from the caster: attack rate + DataA (none on
        an illusion), a life DoT of DataB DEATH (18) per tick, 0.01 s then every 1 s; a refresh keeps the tick phase."""
        t, lv, ab = c.target, c.lv, self.ab
        if t is None or not t.alive or t.dead:
            return
        bcode = _rc(lv['BuffID'][0]) if lv['BuffID'] else _rc('Buhf')
        bf, new = ab.add_buff(t, bcode, 'frenzy', c.u, ab.dur(lv, t))
        bf.atk = 0.0 if t.illusion else lv['A']
        bf.dot, bf.dtype = lv['B'], 18
        ab.apply(bf)
        if new:
            ab.at(self.eng.now() + steps_of(0.01), 'dot', bf, None)

    @staticmethod
    def rej_ok(t, lv):
        """NOTES_abilities §5.28 can-target 0x6f56bea0 (A-SP80): DataA = DataB = 0 → 0xdd; an illusion → 0xa2; a
        mana-only row (A 0) on a unit without a mana pool → 0x7d; full life (not allowed by trunc(DataC) bit 0) refuses
        unless the mana part still has work (B > 0, a pool, not full, or full allowed by bit 1) [inf]."""
        a, b, cf = lv['A'], lv['B'], int(lv['C'])
        if a == 0.0 and b == 0.0:
            return False
        if getattr(t, 'illusion', False):
            return False
        if a == 0.0 and t.maxmana <= 0.0:
            return False
        if t.life >= t.maxlife and not (cf & 1):
            if b <= 0.0 or t.maxmana <= 0.0 or (t.mana >= t.maxmana and not (cf & 2)):
                return False
        return True

    def fx_Arej(self, c):
        """NOTES_abilities §5.28 (A-SP80): Brej (BuffID first) for Dur′ + 0.01 s, refreshed (new amounts, the tick
        phase goes on); every 1.0 s from the effect + DataA / Dur′ life and + DataB / Dur′ mana, held at the maxima."""
        t, lv, ab = c.target, c.lv, self.ab
        if t is None or not t.alive or t.dead:
            return
        d = ab.dur(lv, t)
        bcode = _rc(lv['BuffID'][0]) if lv['BuffID'] else _rc('Brej')
        bf, new = ab.add_buff(t, bcode, 'rej', c.u, d + 0.01)
        bf.vals['hl'] = lv['A'] / d if d > 0 else 0.0
        bf.vals['hm'] = lv['B'] / d if d > 0 else 0.0
        ab.apply(bf)
        if new:
            ab.at(self.eng.now() + steps_of(1.0), 'dot', bf, None)

    def fx_AIrg(self, c):
        """NOTES_items §10 (W9-4, effect 0x6f56ca30 / 0x6f56c730 -> 0x6f56c660 -> 0x6f56c380): Area > 0: every targs unit
        within Area of the carrier (of the target); else the carrier (DataD set) or the target. Each passing the Arej
        can-target test gets the buff chosen by the amounts (DataA 0: BIrm mana only; DataB 0: BIrl life only;
        else BIrg) for Dur′ + 0.01 s, ticking like Brej: A / Dur′ life, B / Dur′ mana every 1 s. DataE (dispel on
        damage) is not modelled (Open)."""
        u, lv, ab = c.u, c.lv, self.ab
        a, b = lv['A'], lv['B']
        if lv['Area'] > 0.0:
            cx, cy = (c.target.x, c.target.y) if c.target is not None else (u.x, u.y)
            ts = self.in_area(cx, cy, lv['Area'], u, lv['targs'])
        elif lv['D'] != 0:
            ts = [u]
        else:
            ts = [c.target]
        bcode = _rc('BIrm' if a == 0.0 else ('BIrg' if b != 0.0 else 'BIrl'))   # fixed codes (the pack keeps the first BuffID only)
        for t in ts:
            if t is None or not t.alive or t.dead or not self.rej_ok(t, lv):
                continue
            d = ab.dur(lv, t)
            bf, new = ab.add_buff(t, bcode, 'rej', u, d + 0.01)
            bf.vals['hl'] = a / d if d > 0 else 0.0
            bf.vals['hm'] = b / d if d > 0 else 0.0
            ab.apply(bf)
            if new:
                ab.at(self.eng.now() + steps_of(1.0), 'dot', bf, None)

    def rpl_auto(self, u, aid, on):
        """NOTES_abilities §5.55 (A-SP81): autocast on posts the first check after 0.02 s, off cancels the pending check
        (a generation per (unit, ability))."""
        g = u.__dict__.setdefault('rpl_gen', {})
        if on and aid not in u.auto_on:
            u.auto_on.add(aid)
            g[aid] = g.get(aid, 0) + 1
            self.ab.at(self.eng.now() + steps_of(RPL_FIRST), 'rpl', u, (aid, g[aid]))
        elif not on and aid in u.auto_on:
            u.auto_on.discard(aid)
            g[aid] = g.get(aid, 0) + 1

    @staticmethod
    def rpl_amounts(code, lv):
        """§5.55: +0x478 / +0x47c = (DataA, 0) for Arpl, (0, DataB) for Arpm [obs 0x6f539960 / 0x6f539e10 /
        0x6f539f50 / 0x6f539980]."""
        return (lv['A'], 0.0) if code == 'Arpl' else (0.0, lv['B'])

    def rpl_ok(self, u, v, code, lv):
        """§5.55 can-target 0x6f520c40: nothing to give → 0xdd; life only at full life → 0x1c; mana only without a pool
        → 0x7d, at full mana → 0x1d; a running buff of this class from another caster → 0xdd."""
        hl, hm = self.rpl_amounts(code, lv)
        if hl == 0.0 and hm == 0.0:
            return False
        if hl != 0.0 and v.life == v.maxlife:
            return False
        if hm != 0.0 and (v.maxmana == 0.0 or v.mana == v.maxmana):
            return False
        bf = self.ab.find_buff(v, self.rpl_buff(code, lv))
        return bf is None or bf.src is u

    @staticmethod
    def rpl_buff(code, lv):
        return _rc(lv['BuffID'][0]) if lv['BuffID'] else _rc('Brpl' if code == 'Arpl' else 'Brpm')

    def rpl_check(self, u, key):
        """§5.55 the autocast check 0x6f445880: with autocast on, a living caster that can pay and is not cooling casts
        when a unit in Area passes the can-target test; the next check after max(the cooldown left, 1.0 s) after a
        cast, else after 3.0 s."""
        aid, gen = key
        if not u.alive or u.dead or u.__dict__.get('rpl_gen', {}).get(aid) != gen or aid not in u.auto_on:
            return
        r = self.ab.rec(aid)
        lv = self.ab.lv(u, aid)
        if r is None or lv is None:
            return
        nxt = RPL_PERIOD
        if (u.mana >= lv['Cost'] and not self.cooling(u, aid) and u.cast is None and u.fcast is None and
                not u.constructing and any(self.rpl_ok(u, v, r['code'], lv)
                                           for v in self.in_area(u.x, u.y, lv['Area'], u, lv['targs']))):
            if self.eng.issue(u, self.order_id(aid)):
                e = self.cool.get((u.serial, aid))
                left = (e - self.eng.now()) * 0.005 if e is not None else 0.0
                nxt = max(left, lv['Cool'], RPL_AFTER)            # +0x3e0: max(the cooldown left, Cool) [obs 0x6f473f60]
        self.ab.at(self.eng.now() + steps_of(nxt), 'rpl', u, key)

    def fx_Arpl(self, c):
        """NOTES_abilities §5.55 (A-SP81, effect 0x6f521580 / 0x6f521100): the units within Area of the caster passing
        targs (serial order) that miss life (Arpl) / mana (Arpm), at most trunc(Cast) of them (0 = all): BuffID for
        Dur′ (refreshed), + DataA life / + DataB mana (held at the maxima; an illusion gets the buff only); then the
        caster pays Cost × max(1, min(n, trunc(DataE)))."""
        u, lv, ab = c.u, c.lv, self.ab
        hl, hm = self.rpl_amounts(c.code, lv)
        cap, n = int(lv['Cast']), 0
        bc = self.rpl_buff(c.code, lv)
        for v in self.in_area(u.x, u.y, lv['Area'], u, lv['targs']):
            if cap and n == cap:
                break
            nl = hl != 0.0 and abs(v.maxlife - v.life) > 0.0
            nm = hm != 0.0 and abs(v.maxmana - v.mana) > 0.0
            if not nl and not nm:
                continue
            n += 1
            bf, _new = ab.add_buff(v, bc, 'rpl', u, ab.dur(lv, v))
            ab.apply(bf)
            if getattr(v, 'illusion', False):
                continue
            if nl:
                v.life = r32(min(v.maxlife, v.life + hl))
            if nm:
                v.mana = r32(min(v.maxmana, v.mana + hm))
        pay = lv['Cost'] * max(1, min(n, int(lv['E'])))
        if pay > 0:
            u.mana = r32(u.mana - min(pay, u.mana))

    fx_Arpm = fx_Arpl

    def fx_AIta(self, c):
        """NOTES_abilities §5.44 (A-AB18): Xbdt at the point — the caster's owner detects units of kind trunc(DataA)
        (bit 1 invisible) within Area for Dur + 0.1 s (+0x9c = 0)."""
        self.ab.reveal(c.u.owner, c.x, c.y, c.lv['Area'], int(c.lv['A']), c.lv['Dur'] + 0.1)

    def fx_AIrv(self, c):
        """CAbilityItemRevealMap §1-§2: the same Xbdt point detector as AIta (kind trunc(DataA), Dur + 0.1 s; the +0x9c bonus is 0)."""
        self.fx_AIta(c)

    def fx_Arep(self, c):
        self.fx_Arst(c)                                           # §5.36: the same effect slot body

    def fx_Arst(self, c):
        """0x6f464f70: gold g = cost × DataA, lumber l = cost × DataA, time T = max(5, trunc(bldtm) × DataB); none
        affordable (< 1) → nothing; else 1 gold / 1 lumber at once (when g / l > 0), ticks every T / g (gold) and T / l
        (lumber), a 1 s check tick only when both periods are ≥ 1 s; the target's life rate + max life / T."""
        u, t, lv, N = c.u, c.target, c.lv, self.N
        if t is None or not self.repair_ok(u, t):
            return
        pl = u.owner
        g = r32(int(t.data.get('gold', 0) or 0) * lv['A'])
        l = r32(int(t.data.get('lumber', 0) or 0) * lv['A'])
        T = r32(int(float(t.data.get('bldtm', 0) or 0)) * lv['B'])
        if T < REPAIR_MIN_T:
            T = REPAIR_MIN_T
        if (g > 0 and pl.state.get(REPAIR_GOLD, 0) < 1) or (l > 0 and pl.state.get(REPAIR_LUMBER, 0) < 1):
            return
        pg = r32(T / g) if g > 0 else REPAIR_TICK
        pw = r32(T / l) if l > 0 else REPAIR_TICK
        now = self.eng.now()
        rp = Repair()
        rp.t = t
        rp.rate = r32(t.maxlife / T)
        rp.per = {'g': steps_of(pg), 'l': steps_of(pw), 's': steps_of(REPAIR_TICK)}
        rp.due = {'g': now + rp.per['g'] if g > 0 else -1, 'l': now + rp.per['l'] if l > 0 else -1,
                  's': now + rp.per['s'] if pg >= REPAIR_TICK and pw >= REPAIR_TICK else -1}
        c.repair = rp
        if g > 0:
            N.set_player_state(pl, REPAIR_GOLD, pl.state.get(REPAIR_GOLD, 0) - 1)
        if l > 0:
            N.set_player_state(pl, REPAIR_LUMBER, pl.state.get(REPAIR_LUMBER, 0) - 1)
        if c.repair is rp and t.alive and not t.dead:
            t.regen_bonus += rp.rate
            self.eng.set_regen(t)
        else:
            c.repair = None

    def repair_valid(self, c):
        """0x6f4657b0 + the range part of 0x6f465890: alive, targs, can-target (§5.36), in edge range."""
        t, u = c.repair.t, c.u
        if t is None or not t.alive or t.dead or not self.ab.targ_ok(u, t, c.lv['targs']):
            return False
        if getattr(t, 'constructing', False) or t.maxlife - t.life < 1.0 or getattr(t, 'illusion', False):
            return False
        return self.repair_reach(u, t, c.lv['Rng'])

    def repair_tick(self, c, now):
        """the due ticks of this step, gold first, then lumber, then the check tick [inf]: invalid → stop; a gold /
        lumber tick pays 1 (the owner has < 1 → stop [inf])."""
        rp = c.repair
        pl = c.u.owner
        while c.repair is rp:
            kind = None
            for k in ('g', 'l', 's'):
                if 0 <= rp.due[k] <= now:
                    kind = k
                    break
            if kind is None:
                return
            rp.due[kind] += rp.per[kind]
            if not self.repair_valid(c):
                self.repair_stop(c)
                return
            if kind == 's':
                continue
            sid = REPAIR_GOLD if kind == 'g' else REPAIR_LUMBER
            have = pl.state.get(sid, 0)
            if have < 1:
                self.repair_stop(c)
                return
            self.N.set_player_state(pl, sid, have - 1)

    def repair_off(self, c):
        rp = c.repair
        c.repair = None
        t = rp.t
        t.regen_bonus -= rp.rate
        self.eng.set_regen(t)

    def repair_stop(self, c):
        """0x6f465da0: the rate is removed; the cast ends this step (FINISH, ENDCAST)."""
        self.repair_off(c)
        c.end = self.eng.now()

    def wave_ok(self, t):
        """NOTES_abilities §5.33 (+0x3f0 of the heal form): not at full life (|max − life| < 2e-7) and not an
        illusion (the Misc flag taken as off, as AHhb)."""
        return abs(t.maxlife - t.life) >= 2e-7 and not getattr(t, 'illusion', False)

    def fx_AOhw(self, c):
        """NOTES_abilities §5.33 (A-SP64, all hops at once as A-SP37): the AOcl chain with the amount negated (a heal
        through the damage pipeline, LIGHTNING, attack type 0); the next target is the valid unhit unit within Area of
        the current one with the largest missing life (strictly more than the best so far, serial order on ties);
        magic-immune units are candidates (the pipeline gives them 0); EtherealHealBonus is not modelled."""
        u, lv, eng = c.u, c.lv, self.eng
        t, f, hit = c.target, 1.0, []
        n = int(lv['B'])
        while t is not None and len(hit) < max(1, n):
            hit.append(t)
            eng.damage(t, u, r32(-(lv['A'] * f)), 0, DMG_LIGHTNING, False, False)
            f = f * (1.0 - lv['C'])
            if len(hit) >= n:
                break
            nxt, best = None, 0.0
            for v in self.in_area(t.x, t.y, lv['Area'], u, lv['targs']):
                if v in hit or not self.wave_ok(v):
                    continue
                miss = v.maxlife - v.life
                if miss > best:
                    nxt, best = v, miss
            t = nxt

    def fx_AEfk(self, c):
        """§3.1: units in Area by distance (then serial, A-SP37); DataA each; stop at the DataB budget / DataC cap."""
        u, lv, eng = c.u, c.lv, self.eng
        cand = self.in_area(u.x, u.y, lv['Area'], u, lv['targs'],
                            key=lambda v: ((v.x - u.x) * (v.x - u.x) + (v.y - u.y) * (v.y - u.y), v.serial))
        budget, cap = lv['B'], int(lv['C'])
        for i, v in enumerate(cand):
            if v is u:
                continue
            if cap > 0 and i >= cap:
                break
            if v.alive and not v.dead:
                eng.damage(v, u, r32(lv['A']), 0, DMG_ENHANCED, False, False)
            budget -= lv['A']
            if lv['B'] > 0 and budget <= 0:
                break

    def fx_AOws(self, c):
        """§3.6: the frost nova area routine around the caster: DataA SONIC + BPSE Dur / HeroDur (immune: event only)."""
        u, lv, eng, ab = c.u, c.lv, self.eng, self.ab
        for v in self.in_area(u.x, u.y, lv['Area'], u, lv['targs']):
            if v is u or not v.alive or v.dead:
                continue
            if v.magic_immune > 0:
                eng.damaged_event(v, u, 0.0)
                continue
            eng.damage(v, u, r32(lv['A']), 0, DMG_SONIC, False, False)
            if v.alive and not v.dead:
                ab.stun(v, u, ab.dur(lv, v))

    def fx_ANbr(self, c):
        """§3.7: BNbr for Dur / HeroDur, attack damage + trunc(DataA) (A-SP38) to the targs units in Area."""
        u, lv, ab = c.u, c.lv, self.ab
        for v in self.in_area(u.x, u.y, lv['Area'], u, lv['targs']):
            bf, _new = ab.add_buff(v, _rc('BNbr'), 'roar', u, ab.dur(lv, v))
            bf.vals['dint'] = int(lv['A'])
            ab.apply(bf)

    def fx_Aroa(self, c):
        """NOTES_spells §5.1: the §3.7 body with Broa; percent damage trunc(DataA × avg + 0.5) (0 → sign DataA, at
        least −avg), armor + DataB, life regen + DataC; trunc(DataG) ≠ 0: the nearest DataG only (A-SP51)."""
        u, lv, ab = c.u, c.lv, self.ab
        cand = self.in_area(u.x, u.y, lv['Area'], u, lv['targs'])
        g = int(lv['G'])
        if g > 0:
            cand.sort(key=lambda v: (v.x - u.x) * (v.x - u.x) + (v.y - u.y) * (v.y - u.y))   # stable: serial ties
            cand = cand[:g]
        for v in cand:
            bf, _new = ab.add_buff(v, _rc('Broa'), 'roar', u, ab.dur(lv, v))
            bf.vals['dint'] = self.roar_damage(v, lv['A'])
            bf.armor = lv['B']
            bf.regen = lv['C'] if lv['C'] >= 0.001 else 0.0
            ab.apply(bf)

    def roar_damage(self, v, a):
        """§5.1 percent mode: the first weapon's avg base damage dmgplus + dice · (sides · 0.5 + 0.5) [inf]; one bonus
        for the unit (A-SP51); illusions none."""
        eng = self.eng
        if a == 0 or getattr(v, 'illusion', False):
            return 0
        i = 0 if eng.has_weapon(v, 0) else (1 if eng.has_weapon(v, 1) else -1)
        if i < 0:
            return 0
        dice = float(int(eng.wv(v, i, 'dice') or 0))
        avg = float(int(eng.wv(v, i, 'dmgplus') or 0)) + dice * (float(int(eng.wv(v, i, 'sides') or 0)) * 0.5 + 0.5)
        b = int(a * avg + 0.5)
        if b == 0:
            b = 1 if a > 0 else -1
        if b < -int(avg):
            b = -int(avg)
        return b

    def fx_ACtc(self, c):
        """NOTES_spells §5.5: the war stomp body; DataA SONIC to the targs units in Area, then (not immune, not
        invulnerable) BCtc for Dur′ (none at 0): move −DataC, attack −DataD, refreshed; immune: event only."""
        u, lv, eng, ab = c.u, c.lv, self.eng, self.ab
        for v in self.in_area(u.x, u.y, lv['Area'], u, lv['targs']):
            if v is u or not v.alive or v.dead:
                continue
            if v.magic_immune > 0:
                eng.damaged_event(v, u, 0.0)
                continue
            eng.damage(v, u, r32(lv['A']), 0, DMG_SONIC, False, False)
            d = ab.dur(lv, v)
            if v.alive and not v.dead and not v.invulnerable and d > 0:
                bf, _new = ab.add_buff(v, _rc('BCtc'), 'slow', u, d)
                bf.move, bf.atk = -lv['C'], -lv['D']
                ab.apply(bf)

    fx_AHtc = fx_ACtc                  # BUGS W9-3-GEM1

    def summon_units(self, c, tid, count, dur):
        u, N, eng = c.u, self.N, self.eng
        g = eng.streams.game[2]
        R = c.lv['Area'] if c.lv['Area'] > 0 else 200.0
        for _k in range(count):
            x = r32(u.x + (g.unit() * 2 * R - R))
            y = r32(u.y + (g.unit() * 2 * R - R))
            nu = N.create_unit(u.owner, tid, x, y, u.facing)
            if nu is None:
                continue
            nu.summoned = True
            if dur > 0:
                eng.timed_life(nu, dur)
            N.unit_event(nu, 'SUMMON', {'summoner': N.H(u), 'summoned': N.H(nu)})

    def fx_AOwd(self, c):
        """NOTES_spells §5.9: trunc(DataA) UnitID wards for the caster's owner exactly at the point, facing
        BuildingAngle, timed life Dur (+ f9c = 0; no HeroDur, 0 = permanent), SUMMON each."""
        u, lv, N, eng = c.u, c.lv, self.N, self.eng
        tid = lv.get('UnitID') or ''
        if len(tid) != 4 or _rc(tid) not in N.unit_types:
            return
        for _k in range(int(lv['A'])):
            nu = N.create_unit(u.owner, _rc(tid), c.x, c.y, eng.building_angle)
            if nu is None:
                continue
            nu.summoned = True
            if lv['Dur'] > 0:
                eng.timed_life(nu, lv['Dur'])
            N.unit_event(nu, 'SUMMON', {'summoner': N.H(u), 'summoned': N.H(nu)})

    def fx_AUin(self, c):
        """NOTES_spells §5.8 / §3.4 "each drop": the infernal UnitID (default ninf) for the caster's owner at the
        point, facing the caster's, timed life DataB (0 permanent), BNin holds it until the impact after DataC (the
        timer is armed before the SUMMON event)."""
        u, lv, N, eng, ab = c.u, c.lv, self.N, self.eng, self.ab
        tid = lv.get('UnitID') or 'ninf'
        if len(tid) != 4 or _rc(tid) not in N.unit_types:
            return
        nu = N.create_unit(u.owner, _rc(tid), c.x, c.y, u.facing)
        if nu is None:
            return
        nu.summoned = True
        if lv['B'] > 0:
            eng.timed_life(nu, lv['B'])
        bf, _new = ab.add_buff(nu, _rc('BNin'), 'infernal', u, 0.0)
        bf.stun = True                                            # inactive until the impact (A-SP52)
        ab.apply(bf)
        ab.at(eng.now() + ab_steps(lv['C']), 'inferno', bf, (c.x, c.y, lv))
        N.unit_event(nu, 'SUMMON', {'summoner': N.H(u), 'summoned': N.H(nu)})

    def inferno_impact(self, bf, data):
        """§3.4 impact: the infernal active again; destructables are not in our world (A-SP52); every targs unit
        (relative to the infernal) within Area of the point: immune → event only, else DataA FORCE (attack type 0,
        source = the infernal), then BPSE Dur′ unless invulnerable."""
        x, y, lv = data
        t, eng, ab = bf.unit, self.eng, self.ab
        if not bf.alive or not t.alive or t.dead:
            return
        ab.unapply(bf)
        for v in self.in_area(x, y, lv['Area'], t, lv['targs']):
            if v is t or not v.alive or v.dead:
                continue
            if v.magic_immune > 0:
                eng.damaged_event(v, t, 0.0)
                continue
            eng.damage(v, t, r32(lv['A']), 0, DMG_FORCE, False, False)
            if v.alive and not v.dead and not v.invulnerable:
                ab.stun(v, t, ab.dur(lv, v))

    def fx_ANsw(self, c):
        tid = c.lv.get('UnitID') or ''
        if len(tid) == 4 and _rc(tid) in self.N.unit_types:
            self.summon_units(c, _rc(tid), int(c.lv['A']), c.lv['Dur'])           # A-SP39

    def fx_AOsf(self, c):
        tid = c.lv.get('UnitID') or ''
        if len(tid) == 4 and _rc(tid) in self.N.unit_types:
            self.summon_units(c, _rc(tid), int(c.lv['B']), self.ab.dur(c.lv, c.u))

    def fx_AUfn(self, c):
        """§4.3: units within Area of the target (ability targs, sorted by serial, A-SP10) take DataA COLD + Bfro,
        then the target takes DataB."""
        u, lv, t = c.u, c.lv, c.target
        eng, ab = self.eng, self.ab
        x, y, r = t.x, t.y, lv['Area']
        cand = sorted((v for v in eng.query(x, y, r + eng.max_coll) if v.alive and not v.dead),
                      key=lambda v: v.serial)
        for v in cand:
            dx, dy = v.x - x, v.y - y
            if dx * dx + dy * dy > r * r or not ab.targ_ok(u, v, lv['targs']):
                continue
            if v.magic_immune > 0:
                eng.damaged_event(v, u, 0.0)                      # "event only", no slow [obs]
                continue
            eng.damage(v, u, r32(lv['A']), 0, DMG_COLD, False, False)
            if v.alive and not v.dead:
                bf, _new = ab.add_buff(v, _rc('Bfro'), 'frost', u, ab.dur(lv, v))
                bf.move, bf.atk = -ab.frost_move, -ab.frost_atk
                ab.apply(bf)
        if t.alive and not t.dead:
            eng.damage(t, u, r32(lv['B']), 0, DMG_COLD, False, False)

    def fx_AEer(self, c):
        """§4.4: BEer for Dur / HeroDur, DataA per second (first tick 0.01 s), the target is held (A-SP11)."""
        t, lv, ab = c.target, c.lv, self.ab
        bf, new = ab.add_buff(t, _rc(ROOTS_BUFF), 'roots', c.u, ab.dur(lv, t))
        bf.dot, bf.dtype, bf.stun = lv['A'], DMG_MAGIC, True
        ab.apply(bf)
        if new:
            ab.at(self.eng.now() + steps_of(ROOT_DOT_FIRST), 'dot', bf, None)

    def fx_AEbl(self, c):
        """§4.8: the caster is moved to the point (position setter, no stop); farther than DataA → DataA along the
        line, closer than DataB → DataB (A-SP13); the Dur delay is not modelled (A-SP13)."""
        u, lv = c.u, c.lv
        dx, dy = c.x - u.x, c.y - u.y
        d = math.sqrt(dx * dx + dy * dy)
        x, y = c.x, c.y
        if d > 0:
            lim = lv['A'] if lv['A'] > 0 and d > lv['A'] else (lv['B'] if d < lv['B'] else 0.0)
            if lim > 0:
                x, y = u.x + dx / d * lim, u.y + dy / d * lim
        self.N.set_unit_pos(u, x, y)

    def fx_AUsl(self, c):
        """§4.7: BUsl, the target paused for Dur / HeroDur (A-SP12: damage does not wake it yet)."""
        t, lv, ab = c.target, c.lv, self.ab
        bf, _new = ab.add_buff(t, _rc(SLEEP_BUFF), 'sleep', c.u, ab.dur(lv, t))
        bf.stun = True
        ab.apply(bf)

    def fx_AHhb(self, c):
        """§5.20: a friendly living target is healed DataA; an enemy undead one takes DataA/2 DIVINE (attack type
        0); anything else nothing."""
        t, lv, u = c.target, c.lv, c.u
        if t is None or not t.alive or t.dead:
            return
        undead = bool(t.ucls & 2)
        if self.eng.ally(u.owner, t.owner) and not undead:
            t.life = r32(min(t.maxlife, t.life + lv['A']))       # EtherealHealBonus: Open (A-SP45)
        elif not self.eng.ally(u.owner, t.owner) and undead:
            self.eng.damage(t, u, r32(lv['A'] / 2.0), 0, DMG_DIVINE, False, False)

    def bloodlust(self, c, t, default):
        lv, ab = c.lv, self.ab
        bcode = _rc(lv['BuffID'][0]) if lv['BuffID'] else _rc(default)
        bf, _new = ab.add_buff(t, bcode, 'bloodlust', c.u, ab.dur(lv, t))     # refresh: the new values (§5.21)
        bf.atk, bf.move = lv['A'], lv['B']
        ab.apply(bf)

    def fx_Ablo(self, c):
        if c.target is not None and c.target.alive and not c.target.dead:
            self.bloodlust(c, c.target, 'Bblo')

    def fx_Afzy(self, c):
        self.bloodlust(c, c.u, 'Bfzy')

    def fx_Acri(self, c):
        """NOTES_abilities §5.28 (A-SP58): Bcri for Dur′ (refresh = new values): move −DataA, attack −DataB, damage
        −trunc(DataC × avg + 0.5) of the first weapon's avg base damage (illusions: no damage part); a `slow` kind
        (Bcri is magic → purge strips it [obs])."""
        t, lv, ab = c.target, c.lv, self.ab
        if t is None or not t.alive or t.dead:
            return
        bcode = _rc(lv['BuffID'][0]) if lv['BuffID'] else _rc('Bcri')
        bf, _new = ab.add_buff(t, bcode, 'slow', c.u, ab.dur(lv, t))
        bf.move, bf.atk = -lv['A'], -lv['B']
        bf.vals['dint'] = self.cripple_damage(t, lv['C'])
        ab.apply(bf)

    def cripple_damage(self, v, f):
        """§5.28: −trunc(f × avg + 0.5) [obs 0x6f603cd0: avg = 0x6f557470 as roar]; a negative trunc (f < 0) is held at
        −trunc(avg) [obs: the cap at −base]; one value for both weapons from the first weapon (A-SP58)."""
        eng = self.eng
        if f == 0 or getattr(v, 'illusion', False):
            return 0
        i = 0 if eng.has_weapon(v, 0) else (1 if eng.has_weapon(v, 1) else -1)
        if i < 0:
            return 0
        dice = float(int(eng.wv(v, i, 'dice') or 0))
        avg = float(int(eng.wv(v, i, 'dmgplus') or 0)) + dice * (float(int(eng.wv(v, i, 'sides') or 0)) * 0.5 + 0.5)
        b = int(f * avg + 0.5)
        if b < 0 and b < -int(avg):
            b = -int(avg)
        return -b

    def raisable(self, t):
        """§5.30 (A-SP60): a corpse that has not decayed, not a structure's, not a hero's."""
        return (t is not None and t.alive and t.dead and t in self.N.corpses and not t.is_bldg
                and not self.ab.is_hero(t) and not getattr(t, 'exploded', False))   # §5.49: incinerated: no corpse

    def raise_types(self, lv):
        """§5.30: (count, type) for DataA × DataC (default UnitID) and DataB × DataD [obs 0x6f5810a0 / 0x6f581140]."""
        S = lv.get('S') or {}
        out = []
        for n, tid in ((int(lv['A']), S.get('C') or lv.get('UnitID') or ''), (int(lv['B']), S.get('D') or '')):
            tid = str(tid).strip()
            out.append((n, _rc(tid) if len(tid) == 4 and _rc(tid) in self.N.unit_types else 0))
        return out

    # ------------------------------------------------------------ W9R3-2 (NOTES_abilities §5.57 .. §5.60)
    def curse_auto(self, u, on):
        """§5.57 curseon / curseoff (0xd00df / 0xd00e0): the autocast switch of the first Acrs; instant, nothing is paid."""
        for aid, code, lv in self.ab.abil_list(u):
            if code == 'Acrs' and code in CLASSES:
                if on:
                    u.auto_on.add(aid)
                else:
                    u.auto_on.discard(aid)
                return True
        return None

    def fx_Acrs(self, c):
        """§5.57 (0x6f4ec8a0): Bcrs (a fixed class constant, BuffID only picks art) for Dur′ with the miss chance DataA;
        an existing instance is refreshed (new value, timer restarted)."""
        t, lv, ab = c.target, c.lv, self.ab
        if t is None or not t.alive or t.dead:
            return
        bf, _new = ab.add_buff(t, _rc('Bcrs'), 'curse', c.u, ab.dur(lv, t))
        bf.vals['curse'] = lv['A']
        ab.apply(bf)

    def fx_ANht(self, c):
        """§5.58: the Aroa body (fx_Aroa) with BNht and the values (−DataA, −DataB, DataC): percent damage
        trunc(−A × avg + 0.5) (0 → −1, at least −avg), armor − DataB, life regen + DataC; trunc(DataG) ≠ 0 caps the units."""
        u, lv, ab = c.u, c.lv, self.ab
        cand = self.in_area(u.x, u.y, lv['Area'], u, lv['targs'])
        g = int(lv['G'])
        if g > 0:
            cand.sort(key=lambda v: (v.x - u.x) * (v.x - u.x) + (v.y - u.y) * (v.y - u.y))
            cand = cand[:g]
        for v in cand:
            bf, _new = ab.add_buff(v, _rc('BNht'), 'roar', u, ab.dur(lv, v))
            bf.vals['dint'] = self.roar_damage(v, -lv['A'])
            bf.armor = -lv['B']
            bf.regen = lv['C'] if lv['C'] >= 0.001 else 0.0
            ab.apply(bf)

    def defend_order(self, u, on):
        """§5.59 defend / undefend (0xd0057 / 0xd0058): instant toggle of the first Adef. On: Bdef (the active flag) with
        move − DataC and attack − DataD [obs 0x6f4dd370]; off: both given back [obs 0x6f4dd440]."""
        for aid, code, lv in self.ab.abil_list(u):
            if code != 'Adef' or lv is None or code not in CLASSES:
                continue
            if aid in u.owner.abil_off or u.dead or not u.alive:
                return False
            ab = self.ab
            if on:
                if not ab.has_buff(u, _rc('Bdef')):
                    bf, _new = ab.add_buff(u, _rc('Bdef'), 'defend', u, 0.0)
                    bf.move = -lv['C']
                    bf.atk = -lv['D']
                    ab.apply(bf)
            else:
                ab.remove_buff_code(u, _rc('Bdef'))
            return True
        return None

    def fx_Acan(self, c):
        """§5.60 (0x6f5f9ed0): the corpse is flagged (0x1000), the caster gets a life regeneration of DataA per second for
        the channel; DataB is the life total at which the channel ends [inf]."""
        u, lv, t = c.u, c.lv, c.target
        if not self.raisable(t) or getattr(t, 'eaten', False):
            return
        t.eaten = True
        k = Cannibal()
        k.corpse, k.rate, k.cap = t, lv['A'], lv['B']
        c.can = k
        u.regen_bonus += k.rate
        self.eng.set_regen(u)

    def can_tick(self, c, now):
        """the channel ends when the caster's life reaches DataB [inf]."""
        if c.u.life >= c.can.cap and c.end != now:
            c.end = now

    def can_off(self, c):
        k = c.can
        c.can = None
        c.u.regen_bonus -= k.rate
        self.eng.set_regen(c.u)
        k.corpse.eaten = False

    def can_done(self, c):
        corpse = c.can.corpse
        self.can_off(c)
        if corpse.alive and corpse.dead and corpse in self.N.corpses:
            self.N.remove_unit(corpse)

    def fx_Arai(self, c):
        """NOTES_abilities §5.30 (A-SP60): the summons for the caster's owner at the corpse (its facing), summoned,
        timed life Dur′, SUMMON each; then the corpse is removed."""
        u, lv, N, eng, t = c.u, c.lv, self.N, self.eng, c.target
        if not self.raisable(t):
            return
        dur = self.ab.dur(lv, t)
        for n, tid in self.raise_types(lv):
            for _k in range(n if tid else 0):
                nu = N.create_unit(u.owner, tid, t.x, t.y, t.facing)
                if nu is None:
                    continue
                nu.summoned = True
                if dur > 0:
                    eng.timed_life(nu, dur)
                N.unit_event(nu, 'SUMMON', {'summoner': N.H(u), 'summoned': N.H(nu)})
        N.remove_unit(t)

    def fx_AIfs(self, c):
        """CAbilityFigurine_p1 §4/§5 (PORT7-RestGameplay): the level's two (type, count) pairs - DataC x trunc(DataA),
        DataD x trunc(DataB) - are summoned [obs]; an unset or unknown type spawns nothing (the 0x5f5f5f5f test).
        Each unit is a summon with a SUMMON event [obs 0x80254]. Placement and life are [inf]: the ring of Area
        around the carrier of ANsw (summon_units) and timed life Dur (the summoned unit is no hero; the data's HeroDur 0 would make a hero carrier's summons permanent); the
        0x500 type flag / 0x2000 unit flag are unmodelled (open)."""
        u, lv = c.u, c.lv
        S = lv.get('S') or {}
        dur = lv['Dur']
        for n, key in ((int(lv['A']), 'C'), (int(lv['B']), 'D')):
            tid = str(S.get(key) or '').strip()
            if n > 0 and len(tid) == 4 and _rc(tid) in self.N.unit_types:
                self.summon_units(c, _rc(tid), n, dur)

    def fx_AUim(self, c):
        """NOTES_spells §5.13 (A-SP61): n = max(2, trunc((DataA + Area) / (Area/2))) circles of radius Area/2, the first
        at caster + dir · Area/2, then every DataA / (n − 1) along dir (towards the point; closer than 0.01: the
        facing), DataB / (n − 1) s apart; the first at once."""
        u, lv = c.u, c.lv
        a, area = lv['A'], lv['Area']
        h = area * 0.5
        n = int((a + area) / h) if h > 0 else 2
        if n < 3:
            n = 2
        stp = a / (n - 1)
        dx, dy = c.x - u.x, c.y - u.y
        d2 = dx * dx + dy * dy
        if d2 < IMPALE_NEAR2:
            f = u.facing * math.pi / 180.0
            ux, uy = math.cos(f), math.sin(f)
        else:
            d = math.sqrt(d2)
            ux, uy = dx / d, dy / d
        w = Impale()
        w.u, w.lv, w.n = u, lv, n
        w.x, w.y = u.x + ux * h, u.y + uy * h
        w.sx, w.sy = ux * stp, uy * stp
        w.period = lv['B'] / (n - 1)
        self.impale_step(w)

    def impale_step(self, w):
        """one circle: the targs units (relative to the caster) within Area/2 of the point, in serial order, that are
        not flying, not magic immune and have no BUim get BUim (0x6f599410 / 0x6f598b30 veto 199 [obs]); then the
        next circle after the period while circles remain."""
        ab, eng, lv, u = self.ab, self.eng, w.lv, w.u
        for v in self.in_area(w.x, w.y, lv['Area'] * 0.5, u, lv['targs']):
            if eng.flying(v) or v.magic_immune > 0 or ab.has_buff(v, _rc('BUim')):
                continue
            src = u if u.alive and not u.dead else None
            ab.impale_on(v, src, lv)
        w.n -= 1
        if w.n > 0:
            w.x, w.y = w.x + w.sx, w.y + w.sy
            ab.at(eng.now() + ab_steps(w.period), 'impale_w', w, None)

    def poly_type(self, t, lv):
        """§5.31: the DataB (ground) / DataC (movetp fly) / DataD (amph) / DataE (float) list, one entry by a die of
        game stream 0x18 (drawn also for one entry) [obs 0x6f5b4e60 / 0x6f5b4db0]; 0 = none."""
        mt = int(t.data.get('movetp', 0) or 0)
        key = 'C' if mt == 2 else ('E' if mt == 0x10 else ('D' if mt == 0x20 else 'B'))
        ids = [i.strip() for i in str((lv.get('S') or {}).get(key, '') or '').split(',')]
        ids = [_rc(i) for i in ids if len(i) == 4 and _rc(i) in self.N.unit_types]
        if not ids:
            return 0
        return ids[self.eng.streams.game[0x18].below(len(ids))]

    def fx_Aply(self, c):
        """NOTES_abilities §5.31 (A-SP62): an illusion dies; else the new type is drawn, an existing Bply only gets the
        new duration [obs +0x350], a new one morphs the unit (life and max life kept) until it ends."""
        t, lv, ab = c.target, c.lv, self.ab
        if t is None or not t.alive or t.dead:
            return
        if getattr(t, 'illusion', False):
            self.N.kill_unit(t, None)                             # [obs slot +0x124]
            return
        tid = self.poly_type(t, lv)
        if not tid:
            return
        bcode = _rc(lv['BuffID'][0]) if lv['BuffID'] else _rc('Bply')
        old = ab.find_buff(t, bcode)
        if old is not None:
            ab.add_buff(t, bcode, 'poly', c.u, ab.dur(lv, t))   # refresh: the duration only
            return
        orig = t.tid
        self.poly_morph(t, tid)
        bf, _new = ab.add_buff(t, bcode, 'poly', c.u, ab.dur(lv, t))
        bf.orig = orig

    def poly_morph(self, t, tid):
        """the engine's upgrade morph without food; life and max life stay (A-SP62 [ext])."""
        life, ml = t.life, t.maxlife
        self.eng.morph_type(t, tid, food=False)
        t.maxlife, t.life = ml, life

    def fx_Afae(self, c):
        t, lv, ab = c.target, c.lv, self.ab
        if t is None or not t.alive or t.dead:
            return
        bcode = _rc(lv['BuffID'][0]) if lv['BuffID'] else _rc('Bfae')
        bf, _new = ab.add_buff(t, bcode, 'faerie', c.u, ab.dur(lv, t))
        bf.armor = -lv['A']
        ab.apply(bf)

    def fx_Alsh(self, c):
        t = c.target
        if t is not None and t.alive and not t.dead:
            self.ab.shield_on(t, c.u, c.lv)

    def fx_Aprg(self, c):
        t = c.target
        if t is not None and t.alive and not t.dead:
            self.ab.purge(c.u, t, c.lv)

    def fx_AIhe(self, c):
        """§5.27: the carrier gains DataA life (no EtherealHealBonus)."""
        u = c.u
        u.life = r32(min(u.maxlife, u.life + c.lv['A']))

    def fx_AIma(self, c):
        """NOTES_abilities §5.32 (A-SP63): the caster gains DataA mana, held at the maximum."""
        u = c.u
        u.mana = r32(min(u.maxmana, u.mana + c.lv['A']))

    def fx_AIha(self, c):
        """§5.27: every unit within Area of the carrier that passes targs gains DataA; illusions only with a Misc flag
        (not modelled: never, A-SP50)."""
        u, lv = c.u, c.lv
        for v in self.in_area(u.x, u.y, lv['Area'], u, lv['targs']):
            if not getattr(v, 'illusion', False):
                v.life = r32(min(v.maxlife, v.life + lv['A']))

    def powerup_fx(self, u, code, lv):
        """a powerup item's usable ability: the effect body on the carrier without the cast sequence (NOTES_items §4.5.1)."""
        getattr(self, 'fx_' + code)(types.SimpleNamespace(u=u, lv=lv))

    @staticmethod
    def powerup_ok(u, code):
        """NOTES_items §4.3: a powerup whose ability refuses the cast leaves the item on the ground. The restore
        family's refusals are the same as its normal cast (AIhe 0x19 full life, AIma 0x1a full mana, AIre 0x1b both
        pools full); the area classes have no full-pool refusal."""
        if code == 'AIhe':
            return u.life < u.maxlife
        if code == 'AIma':
            return u.mana < u.maxmana
        if code == 'AIre':
            return u.life < u.maxlife or u.mana < u.maxmana
        return True

    @staticmethod
    def restore_pools(v, life, mana):
        """CAbilityItemRestore §3 / RestoreAoe §5.2: DataA life, then DataB mana, each held at the maximum
        (EtherealHealBonus is not modelled, as for fx_AIha)."""
        v.life = r32(min(v.maxlife, v.life + life))
        v.mana = r32(min(v.maxmana, v.mana + mana))

    def fx_AIre(self, c):
        """CAbilityItemRestore §3: the carrier gains DataA life and DataB mana."""
        self.restore_pools(c.u, c.lv['A'], c.lv['B'])

    def fx_AIra(self, c):
        """CAbilityItemRestoreAoe §5: every unit within Area of the carrier that passes targs gains DataA life and DataB
        mana (illusions never, as AIha [inf])."""
        u, lv = c.u, c.lv
        for v in self.in_area(u.x, u.y, lv['Area'], u, lv['targs']):
            if not getattr(v, 'illusion', False):
                self.restore_pools(v, lv['A'], lv['B'])

    def fx_AImr(self, c):
        """CAbilityItemManaRestoreAoe + NOTES_items §9: every targs unit within Area of the carrier gains DataA mana [inf amount]."""
        u, lv = c.u, c.lv
        for v in self.in_area(u.x, u.y, lv['Area'], u, lv['targs']):
            if not getattr(v, 'illusion', False):
                v.mana = r32(min(v.maxmana, v.mana + lv['A']))

    def fx_AOwk(self, c):
        """NOTES_spells §5.19 (A-SP78): BOwk (BuffID[0]) on the caster for Dur′, refreshed: the invisibility counter (the
        fade DataA and the backstab DataC are not modelled) and, with a move ability, move + DataB."""
        u, lv, ab = c.u, c.lv, self.ab
        bc = _rc(lv['BuffID'][0]) if lv['BuffID'] else _rc('BOwk')
        bf, _new = ab.add_buff(u, bc, 'invis', u, ab.dur(lv, u))
        bf.vals['invis'] = 1
        bf.move = lv['B'] if self.eng.can_move(u) else 0.0
        ab.apply(bf)

    def fx_Aivs(self, c):
        """NOTES_spells §5.10: Binv (fixed code) for Dur′, refreshed; the invisibility counter at once (the fade DataA
        and the break by attacking / casting are not modelled, A-SP54)."""
        t, lv, ab = c.target, c.lv, self.ab
        if t is None or not t.alive or t.dead:
            return
        bf, _new = ab.add_buff(t, _rc('Binv'), 'invis', c.u, ab.dur(lv, t))
        bf.vals['invis'] = 1
        ab.apply(bf)

    def fx_AUcs(self, c):
        """NOTES_spells §5.2: the wave runs from the caster towards the point (closer than 20: along the caster's
        facing) over DataC at the profile missile speed (default 1100); the first sweep at launch (the next timer
        phase), then every 0.1 s (A-SP57)."""
        u, lv = c.u, c.lv
        w = Swarm()
        w.u, w.lv, w.x0, w.y0 = u, lv, u.x, u.y
        dx, dy = c.x - u.x, c.y - u.y
        d = math.sqrt(dx * dx + dy * dy)
        if d < SWARM_NEAR:
            f = u.facing * math.pi / 180.0
            w.dx, w.dy = math.cos(f), math.sin(f)
        else:
            w.dx, w.dy = dx / d, dy / d
        r = self.ab.rec(c.aid) or {}
        w.speed = float(int(r.get('mspeed', 0) or 0)) or 1100.0
        w.t0 = self.eng.now()
        w.rem = lv['B']
        w.hit = set()
        self.ab.at(w.t0, 'cswarm', w, None)

    def swarm_sweep(self, w):
        """one sweep: head = start + dir · min(DataC, speed · elapsed), radius Area + (DataD − Area) · travelled /
        DataC; the unmarked targs units in it that are not allied to the caster (serial order, at most 64) each take
        min(DataA, budget / n) SONIC from the caster (immune: event only, not marked); the budget drops by the life
        lost; the next sweep 0.1 s later while budget > 0.01 and the head has not reached DataC."""
        eng, ab, lv, u = self.eng, self.ab, w.lv, w.u
        now = eng.now()
        C = lv['C']
        trav = w.speed * (now - w.t0) / SPS
        if trav > C:
            trav = C
        fr = trav / C if C > 0 else 1.0
        r = lv['Area'] + (lv['D'] - lv['Area']) * fr
        hx, hy = w.x0 + w.dx * trav, w.y0 + w.dy * trav
        vic = [v for v in self.in_area(hx, hy, r, u, lv['targs'])
               if v.serial not in w.hit and not eng.ally(u.owner, v.owner)][:SWARM_MAX]
        if vic:
            amt = min(lv['A'], w.rem / len(vic))
            src = u if u.alive and not u.dead else None
            for v in vic:
                if not v.alive or v.dead:
                    continue
                if v.magic_immune > 0:
                    eng.damaged_event(v, src, 0.0)
                    continue
                w.hit.add(v.serial)
                before = v.life
                eng.damage(v, src, r32(amt), 0, DMG_SONIC, False, False)
                w.rem -= before - v.life
        if w.rem > 0.01 and trav < C:
            ab.at(now + ab_steps(SWARM_TICK), 'cswarm', w, None)

    def fx_ANsi(self, c):
        """NOTES_spells §5.4: BNsi for Dur′ (refreshed) on every targs unit within Area of the point that has
        something trunc(DataA) blocks; bit 8 → no spell casts, bits 1 / 2 / 4 → attacks (A-SP55), DataC / DataD move /
        attack factor deltas."""
        u, lv, ab = c.u, c.lv, self.ab
        m = int(lv['A'])
        for v in self.in_area(c.x, c.y, lv['Area'], u, lv['targs']):
            sil = bool(m & 8) and (ab.is_hero(v) or any(code in CLASSES for _a, code, _l in ab.abil_list(v)))
            off = self.attacks_blocked(v, m)
            if not sil and not off:
                continue
            bf, _new = ab.add_buff(v, _rc('BNsi'), 'silence', u, ab.dur(lv, v))
            bf.move, bf.atk = lv['C'], lv['D']
            if sil:
                bf.vals['sil'] = 1
            if off:
                bf.vals['atkoff'] = 1
            ab.apply(bf)

    def attacks_blocked(self, v, m):
        """A-SP55: every weapon of v blocked — bit 4 all, bit 1 weapon type normal, bit 2 the other types."""
        eng = self.eng
        n = 0
        for i in (0, 1):
            if not eng.has_weapon(v, i):
                continue
            n += 1
            tp = int(eng.wv(v, i, 'weapTp') or 0)
            if not (m & 4 or (m & 1 and tp == 1) or (m & 2 and tp != 1)):
                return False
        return n > 0

    def fx_AEim(self, c):
        self.ab.immolation_on(c.u, c.aid)

    def fx_Aslo(self, c):
        """NOTES_abilities §5.12: Bslo, move factor −DataA, attack factor −DataB for Dur / HeroDur (refresh)."""
        t, lv, ab = c.target, c.lv, self.ab
        bcode = _rc(lv['BuffID'][0]) if lv['BuffID'] else _rc('Bslo')
        bf, _new = ab.add_buff(t, bcode, 'slow', c.u, ab.dur(lv, t))
        bf.move, bf.atk = -lv['A'], -lv['B']
        ab.apply(bf)

    def build_type(self, u, lv):
        """§8: UnitID list: one entry for every race, else entry [race − 1] of the caster's owner."""
        ids = [t.strip() for t in str(lv.get('UnitID') or '').split(',') if t.strip()][:5]
        if len(ids) > 1:
            race = {1: 1, 2: 2, 8: 3, 4: 4}.get(u.owner.race_pref, 1)
            ids = ids[race - 1:race]
        if not ids or len(ids[0]) != 4 or _rc(ids[0]) not in self.N.unit_types:
            return 0
        return _rc(ids[0])

    def fx_AIbl(self, c):
        """§8: structure for the caster's owner at the point, CONSTRUCT_START, construction time Dur (min 0.1 s),
        no cost."""
        u, eng, N = c.u, self.eng, self.N
        tid = self.build_type(u, c.lv)
        if not tid:
            return
        ok, cx, cy = eng.can_place(u.owner, tid, c.x, c.y, u)
        if not ok:
            return
        b = N.create_unit(u.owner, tid, cx, cy, eng.building_angle, constructing=True)
        if b is None:
            return
        eng.construction_time(b, max(c.lv['Dur'], 0.1))
        eng.log('item build u%d %s' % (b.serial, b.data.get('id4')))
        N.unit_event(b, 'CONSTRUCT_START', {'constructing': N.H(b)})

    def fx_ANfd(self, c):
        """§9: DataA MAGIC damage to the target at the effect."""
        if c.target is not None and c.target.alive and not c.target.dead:
            self.eng.damage(c.target, c.u, r32(c.lv['A']), 0, DMG_MAGIC, False, False)

    def fx_AAns(self, c):
        """neutral spell: charge DataA gold / DataB lumber after the EFFECT triggers [obs 0x6f45b210]."""
        pl = self.caster_payer(c.u, c.lv)
        g, l = int(c.lv['A']), int(c.lv['B'])
        if g:
            self.N.set_player_state(pl, 1, pl.state.get(1, 0) - g)
        if l:
            self.N.set_player_state(pl, 2, pl.state.get(2, 0) - l)
