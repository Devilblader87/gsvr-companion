#!/usr/bin/env python3
"""Natives over a MINI WORLD MODEL (players/forces, rects/regions/locations, units with straight-line
movement + enter/leave events, groups, timers, triggers + event registrations, leaderboards/multiboards/
dialogs/text recorded but not drawn, game cache, strings, math, seeded RNG).

Every native call is traced as one line  "t=<game ms> thread=<id> <Native>(<args>) -> <result>"  (handles as
h<hex id>). Natives not implemented here are traced with " ?unknown", return the zero value of their type and
are counted (vm.unknown_counts). Pure presentation natives (camera, sound, effects, cinematics, fog, UI) are
'stubs': explicit no-ops that create placeholder objects where a handle is returned (vm.stub_counts).

Rules from NOTES_triggers: §2 event fire/conditions, §3 registration order dispatch, §4 context slots,
§5 ForGroup over a copy / group order / enum filters, §6 timers. Assumptions are marked (Open) and listed in
docs/jvm_bytecode.md.
"""
import math
import os
import struct
import re

from . import vm as V
from . import unitdata as UD
from .vm import Agent, Trigger, TriggerCondition, TriggerAction, BoolExpr, Timer, r32, r2i, s32
from .engine import AVUL, DEATH_LIFE, UNIT_DEATH_LIFE, rally_dest_alive, O_SMART, O_MOVE, O_AIMOVE, O_PATROL
from . import unitevents as UE
from . import randdata as RD
from . import itemdata as ID
from . import ai as AI
from . import aicensus as AC
from . import aibuild as AB
from . import aicaptain as CP
from . import aiextra as EX

CELL = 32.0                 # region cell size (world units)
RB_SHIFT = 4                # region index bucket: 16 x 16 cells (512 world units)

# order ids: the game's order-name table (tools/jvm/orders.py, [obs 0x6f5d29c0])
from .orders import ORDER_IDS, ORDER_NAMES  # noqa: E402
MOVE_ORDERS = {851971, 851983, 851986, 851988, 851990, 851985}
STOP_ORDERS = {851972, 851993}

GROUP_CAP12 = os.environ.get('W3_GROUP_CAP12') == '1'   # PORT9-RestGameplay: CGroup §0.2 caps one group order at 12
                                                        # members. The constant 12 is [obs] (every caller passes 0xc);
                                                        # that members past the twelfth are ignored is [inf] (chapter
                                                        # open question 3) and would change fun-map group orders, so
                                                        # the switch is OFF unless W3_GROUP_CAP12=1 (C# GROUP_CAP12).
GROUP_ORDER_MAX = 12

STUB_RE = re.compile(r'(Camera|Cine|Cinematic|Sound|Music|Volume|Ambient|Effect|Lightning|Fog|Weather|'
                     r'Image|Ubersplat|Blight|Terrain|Water|DayNight|Doodad|Minimap|Interface|Portrait|'
                     r'UserControl|UserUI|Occlusion|Selection|SelectUnit|ClearSelection|SetSkyModel|'
                     r'VertexColor|SetUnitScale|SetUnitTimeScale|Animation|SetUnitColor|SetUnitLookAt|'
                     r'ResetUnitLookAt|BlendTime|Indicator|Quest|Defeat|Victory|Letterbox|Thematic|'
                     r'SetAltMinimap|EnableWorldFogBoundary|SuspendTimeOfDay|SetTimeOfDayScale|'
                     r'PingMinimap|SetDayNight|NewSoundEnvironment|Cheat|Preload|Display.*Dialog|'
                     r'SetUnitFlyHeight|SetUnitExploded|SetUnitPropWindow|SetUnitTurnSpeed|Trackable|'
                     r'FlashQuest|ForceUI|EnableSelect|EnableDragSelect|EnablePreSelect|SetUnitVertexColor|'
                     r'SetUnitRescuable|SetUnitRescueRange|'
                     r'StartCampaignAI|StartMeleeAI|SetMeleeAI|PauseCompAI|CommandAI|'
                     r'RecycleGuardPosition|SetUnitCreepGuard|'
                     r'SetUnitUseFood|UnitSetUsesAltIcon|'
                     r'SetDefaultDifficulty|SetMapMusic|ClearMapMusic|PlayMusic|StopMusic|ResumeMusic|'
                     r'SetReservedLocalHeroButtons|'
                     r'SetDoodadAnimation|SetDestructableAnimation|QueueDestructableAnimation|'
                     r'SetDestructableAnimationSpeed|ShowDestructable|'
                     r'Waygate|DisplayLoadDialog|LockGameSpeed|SetTutorialCleared|'
                     r'SetMissionAvailable|SetCampaign|SetOpCinematic|SetCustomCampaign|SetUnitAnimation|'
                     r'AddUnitAnimationProperties|QueueUnitAnimation|SetUnitBlendTime|SetSpeechVolume|'
                     r'VolumeGroup|AddWeatherEffect|EnableWeatherEffect|SetImage|ShowImage|CreateImage|'
                     # BUGS MAP-NAT: no pointer / keyboard / custom-frame UI in the port - the 1.31 BlzFrame API and
                     # key events are presentation + input without a source; nobody is ever selecting a unit
                     r'Blz\w*Frame|BlzLoadTOCFile|BlzTriggerRegisterPlayerKeyEvent|BlzGetTriggerPlayerKey|'
                     r'BlzGetTriggerPlayerMetaKey|BlzGetTriggerPlayerIsKeyDown|BlzEnableCursor|BlzHideOriginFrames|'
                     r'BlzSetUnitName|GetUnitFlyHeight|GetUnitDefaultFlyHeight|'
                     # BUGS NAT-11: no pointer in VR - the mouse getters of a mouse event (never raised) read 0 / null
                     r'BlzGetTriggerPlayerMouse\w*)')


def stub_creates_handle(name, ret):
    """a stubbed native that must return a (placeholder) handle, not null: Create… / Add… / Start… and the frame
    getters (a map tests the frame it asked for against null)."""
    if ret in ('integer', 'real', 'string', 'boolean', 'code', 'nothing'):
        return False
    return name.startswith(('Create', 'Add', 'Start')) or 'Create' in name or (
        name.startswith('BlzGet') and 'Frame' in name and 'Trigger' not in name)


# ---------------------------------------------------------------- world objects

class Player(Agent):
    tname = 'player'

    def __init__(self, i):
        self.id = i
        self.alive = True            # players are never destroyed
        self.controller = 0 if i < 12 else 3     # MAP_CONTROL_USER / NEUTRAL
        self.slot_state = 0
        self.race_pref = 0
        self.race_selectable = True
        self.team = i
        self.color = i
        self.start_loc = i
        self.name = 'Player %d' % (i + 1)
        self.state = {6: 100}        # FOOD_CAP_CEILING starts at 100 (NOTES_production §2: initial value Open, [ext])
        self.frac = {}               # UPKEEP: resource state (1 gold / 2 lumber) -> stored tenths below the whole unit (0..9)
        self.alliance = {}
        self.tech = {}
        self.tech_max = {}
        self.abil_off = set()        # NAT-3: abilities SetPlayerAbilityAvailable(false) took away
        self.tax = {}                # NAT-6: (other player id, resource state) -> tax rate (stored only)
        self.handicap = 1.0
        self.handicap_xp = 1.0
        self.unit_regs = {}          # player-unit event id -> [EventReg]
        self.sel = []                # BUGS SEL-1: the selected units in selection order (Engine.selected filters)
        self.groups = [[] for _ in range(10)]   # PORT10-Net: the ten control-group banks (CNetCommandUnitDefineControlGroup)
        self.player_regs = {}        # player event id -> [EventReg]
        self.leaderboard = None
        self.on_score = True
        self.score = [0] * 25         # NativesB round 3: the GetPlayerScore counters (only the kill ones are fed)


class Force(Agent):
    tname = 'force'

    def __init__(self):
        self.players = set()


class Location(Agent):
    tname = 'location'

    def __init__(self, x, y):
        self.x = x
        self.y = y


class Rect(Agent):
    tname = 'rect'

    def __init__(self, a, b, c, d):
        self.set_bounds(a, b, c, d)

    def set_bounds(self, a, b, c, d):
        """Unclassed_Native_0010 §Rect/§SetRect: each axis is normalised (lower/higher of the pair). The
        map-bounds clamp of the binary is not applied (the port keeps no playable-map rectangle)."""
        self.minx, self.maxx = (a, c) if a <= c else (c, a)
        self.miny, self.maxy = (b, d) if b <= d else (d, b)


class Region(Agent):
    tname = 'region'

    def __init__(self):
        self.enter_regs = []
        self.leave_regs = []


class Unit(Agent):
    tname = 'unit'

    def __init__(self, serial, tid, owner, x, y, facing, data, vm=None):
        self.vm = vm                 # clock for the time-linear life / mana (NOTES_units §5)
        self.serial = serial
        self.tid = tid
        self.owner = owner
        # A-GP1: a guard position at creation; PORT2-FIX: creep owners only (slot >= 12, NOTES_combat §7 [obs]) -
        # a computer player's own units (TD creeps, fun-map actors) have none and are never sent back
        self.guard = owner is not None and getattr(owner, 'id', 0) >= 12
        # CGuardPost [obs NOTES_combat §7]: the guard point is copied at creation; a unit without one is never
        # sent back. guard_away / guard_due are the 0xd01ad / 0xd01ae state (2 s check, GuardReturnTime return).
        self.guard_x = x if self.guard else None
        self.guard_y = y if self.guard else None
        self.guard_away = False
        self.guard_due = -1
        self.flee_oid = 0            # [obs runtime 1.26a] reported order id of a creep-owned worker's flee (engine.flee_check)
        self.x = x
        self.y = y
        self.facing = facing
        self.data = data
        self.own_data = False        # NAT-1: data became a private copy (unit field overrides)
        self.maxlife = r32(float(data.get('hp', 0) or 1))
        self.life_base = self.maxlife
        self.life_t = vm.steps if vm else 0
        self.life_rate = 0.0
        self.maxmana = r32(float(data.get('mana', 0)))
        self.mana_base = r32(float(data.get('mana0', 0)))
        self.mana_t = self.life_t
        self.mana_rate = 0.0
        self.speed = r32(float(data.get('spd', 0)))
        self.default_speed = self.speed
        self.expire_at = -1              # W8 timed life (Engine.timed_life): death step, -1 none
        self.pick = None                 # W8 the item a getitem / smart order walks to
        self.iop = None                  # HERO-UI the pending drop / give of the carried item u.pick: (kind, x, y, target)
        self.ucls = 0                    # W8 class bits (IsUnitType, NOTES_unittype)
        self.invuln_n = 0                # W8 invulnerable counter (divine shield …, NOTES_spells §0 item 1)
        self.fcast = None                # W8 a running instant ("free") cast (spells.py A-SP29)
        self.auto_on = set()             # W8 autocast on (phase shift; frost arrows use arrow_on)
        self.illusion = False            # W8 mirror image: an image (img_dealt / img_taken factors)
        self.img_dealt = self.img_taken = 1.0
        self.images = []                 # W8 the images of this caster (a recast kills them)
        self.ef_summons = []             # W8 elemental fury: the caster's summons; ef_caster on a summon
        self.ef_caster = None
        self.dmg_int = 0                 # W8 integer attack bonus (avatar)
        self.summoned = False
        self.arrow_on = set()            # W8 frost arrows: ability ids with autocast on; arrow_t = manual shot
        self.arrow_t = None
        self.hero = False                # W8 heroes (heroes.py): set by Engine.init_unit
        self.h_dmg = 0
        self.h_regen = self.h_mregen = 0.0
        self.dead = False
        self.order = 0
        self.tx = self.ty = 0.0
        self.target = None
        self.userdata = 0
        self.hidden = False
        self.usr = False                 # PERF-AUTOFIELD: the unit's current order came from a player's input (not a trigger / the engine)
        self.consumed = False            # PORT4-Build: a night elf builder consumed by an Ancient under construction
        self.cbuilder = None             # PORT4-Build: the structure under construction -> its consumed builder
        self.root_st = 0                 # PORT4-Build CAbilityRoot: 0 no Aroo, 1 rooted, 2 uprooted
        self.root_mask = -1              # ... the enabled weapon slots of the state (DataA rooted / DataB uprooted), -1 none
        self.root_hold = False           # ... held (paused) for the root / unroot transition (Dur / HeroDur)
        self.mine_prev = None            # PORT4-Build: a mine structure built on a neutral gold mine: (owner, tid, x, y, facing, pool)
        self.site_builder = None         # PORT4-Build: ... and the builder to send into it when it stands
        self.paused = False
        self.root_pz = False             # ... and the pause is the transition's own (a script's PauseUnit takes it over)
        self.invulnerable = False
        self.pathing = True
        self.abilities = {}
        self.perm_abil = {}              # PORT8-NativesB: UnitMakeAbilityPermanent counter per ability id (0032 §6)
        self.items = [None] * 6
        self.regions = set()
        self.cell = None
        self.born = None                 # (cell, regions) at creation until the first region update (C-SHOP)
        self.unit_regs = {}
        self.xp = 0
        self.level = int(data.get('level', 0) or 0)
        self.death_time = None
        self.share_vis = 0               # PORT5-NativesB round 2: UnitShareVision slot mask (bit = player id)
        self.ignore_alarm = False        # PORT5-NativesB: bit 0x80 of the Aalr state (UnitIgnoreAlarm)
        self.decay_off = False           # PORT5-NativesB: UnitSuspendDecay(u, true) on a corpse (cleared at the next death)
        # W5 engine state (tools/jvm/engine.py; one Udon parallel array each)
        self.is_bldg = False
        self.base_speed = 0.0
        self.coll = 0.0
        self.armor = 0.0
        self.def_type = 4
        self.magic_immune = 0
        self.locust = False              # NOTES_abilities §5.34 Aloc: out of the spatial index (kept after removal)
        self.coll_wait = 0               # A-COL1: movement steps waited behind an occupied cell
        self.lc = None                   # NOTES_spells §5.16: the swarm locust's Aloc state (spells.lc_*)
        self.ethereal = 0
        # W6 train queue (NOTES_production §1): type ids, slot 0 end step (-1 = not running), waiting for food
        self.tq = []
        self.tq_tok = []                 # PORT10-Queue §4.30: per-slot "paid with a hero token" bit
        self.upg = None                  # W8 building upgrade in progress: [new type, end step, Δgold, Δlumber]
        self.upg_ion = False             # max-life interpolation during the upgrade (CAbilityUpgradeInProgress §5)
        self.upg_i0 = self.upg_i1 = self.upg_is = self.upg_ic = 0.0
        self.tq_end = -1
        self.tq_wait = False
        # W6 abilities (tools/jvm/abilities.py)
        self.prd = {}
        self.procs = []
        self.pulv = None                 # an armed pulverize (abilities.on_swing, NOTES_abilities §5.25)
        self.cleave = None               # an armed cleaving attack (§5.39)
        self.lsteal = None               # an armed life steal (§5.41)
        self.buffs = []
        self.move_delta = 0.0
        self.atk_delta = 0.0
        self.armor_bonus = 0.0
        self.regen_bonus = 0.0
        self.item_spd = []               # W9-4 carried AIms / AIrm ability ids (NOTES_items §10), insertion order
        self.item_mrg = []
        self.item_speed = 0.0            # the flat move speed bonus the list gives (largest, or the sum)
        self.item_mreg = 0.0             # sum of the AIrm DataA (a share of the base mana regeneration)
        self.item_lrg = []               # W9R2-3 carried Arel ability ids (NOTES_items §11.1), insertion order
        self.item_lreg = 0.0             # sum of the Arel DataA (hit points per second)
        self.orbs = []                   # W9R2-3 item-carried orbs [slot, ability id, sibling Apo2 id or 0] (NOTES_items §11.3)
        self.sb_arm = []                 # AIsb keys rolled at this swing (NOTES_items §11.5)
        self.aamk = {}                   # W9R2-3 Aamk on the unit: ability id -> (str, agi, int) applied (NOTES_items §11.2)
        self.dmg_bonus_f = 0.0
        self.upg_any = False             # BUGS FF-UPG (upgeff.py): the research effects applied to this unit
        self.upg_dice = self.upg_dmg = 0
        self.upg_rng = self.upg_spd = self.upg_hp = self.upg_mp = self.upg_arm = 0.0
        self.upg_mspd = 0.0              # PORT9-Upgrade rmov: the percent move factor already applied
        self.upg_as = self.upg_hreg = self.upg_mreg = self.upg_sight = 0.0
        self.upg_aura = 0.0              # PORT13-Upgrade raud: the aura amount delta (cached DataA)
        self.upg_lcap = self.upg_gcap = self.upg_mine = self.upg_spike = 0
        self.upg_spike_amt = 0.0         # PORT14-Upgrade rspi: the Aspi holder's cached DataA amount (thorns damage)
        self.upg_weap = 0
        self.upg_weap_on = False
        self.upg_tgt = 0                 # PORT4-Upgrade ratc: extra bounce targets
        self.upg_dloss = 0.0             # PORT8-Upgrade radl: bounce damage-loss delta (float, no truncation)
        self.upg_mim = 0                 # PORT8-Upgrade rmim: the magic-buff strip level already applied
        self.upg_lev_code = self.upg_lev_steps = 0   # PORT4-Upgrade rlev: the leveled ability id and the steps applied to it
        self.upg_art = 0                 # PORT11-Upgrade rart: the clamped +0xe4 armor-type step counter
        self.upg_art_arm = 0.0           # ... the Arbr DataA armor already applied
        self.stunned = 0
        self.silenced = 0                # W8 BNsi bit 8: no spell casts (NOTES_spells §5.4)
        self.atk_off = 0                 # W8 BNsi attack bits: no attacks (A-SP55)
        self.invisible = 0
        self.fighter = False
        self.atk_target = None
        self.gpt = None                  # BUGS ATK-GND: engine.Pt of an attackground order
        self.atk_w = 0
        self.explicit = False
        self.chasing = False
        self.hold = False
        self.attack_move = False
        self.pat = None                  # MOVE-PORT patrol: the other end (x, y) of the two-point patrol 0xd0017
        self.next_scan = 0
        self.react_at = 0
        self.cd_end = 0
        self.dp_at = -1
        self.dp_target = None
        self.dp_w = 0
        self.bs_end = 0
        self.last_attacker = None
        self.recent_atk = []         # ATTACK-PORT: [(attacker, step)] newest last, 8 slots x 10 s (CAbilityAttack)
        self.last_src = None
        self.path = None
        self.wg_via = None           # NOTES_pathing 1.6: the waygate a smart (right-click) order walks to (it carries the unit to its destination)
        self.wg = None               # a waygate's [active, dest cell x, dest cell y] (None = never set)
        self.wg_slot = 0             # WAYGATE-PORT: its slot in the pathing map's gate table (0 = none: registered at ability add)
        self.wg_ab = (0.0, 0.0)      # the area half extents it was registered with
        self.wg_hold = -1            # follower: step until which a crossing waits for a free landing spot (20 mover updates)
        self.path_i = 0
        self.path_ver = -1
        self.path_goal = None
        self.repath_at = 0
        self.preq = False                # A-PB1: queued for a path in its owner's budget
        self.creq = False                # A-PB2: queued for a collision repath
        self.side = False                # A-PB2: served: steps around the blocker at its next blocked step
        self.det = None                  # A-PB2: the detour point walked to first
        self.pexp = 0                    # A-PB2: the A* expansions of its last path (a collision repath's cost)
        self.creq_at = 0                 # PATH-AB: step the collision request was raised
        self.fo = None                   # STEER-3 (switch on): the formation slot offset (x, y) of the cluster this unit was ordered in
        self.bx = 0                      # ... boxed-in search cycles in a row (engine.ab_boxed)
        self.vmv = False                 # STEER-2 (switch on): the unit has a velocity (it stepped at its last movement tick) - the original's footprint bit 0x20000000; a unit without one STANDS (obstacle of search B, the sight, the blocker check)
        self.wait_until = 0              # ... the step until which a blocked mover stands (4 / 20 ticks behind a moving blocker)
        self.occk = None                 # ... the cell under which the unit is counted in engine.occs (None: not counted)
        self.pt_a = -(1 << 30)           # PATH-AB step 3: step of its last expensive search A / B (the per-mover throttle, NOTES_pathing 2.5); cleared by a cheap one
        self.pt_b = -(1 << 30)
        self.rt = None                   # PATH-AB step 4: its route object (pathab.AbRoute): the A route kept in the mover
        self.rt_cont = False             # ... the next request continues that route (the engine dropped the leg itself) instead of a new order
        self.retry = 0                   # ... the retry counter of the blocked handler (NOTES_pathing 2.7)
        self.ab_safe = False             # ... the next aim is the next raw route point (a smoothed aim was blocked)
        self.ab_arr = False              # ... a harvest walk's route is walked to its end (the target footprint touched): the range check is due
        self.ab_end = False              # ... the retry counter ran out in the path queue: the order ends at the unit's next movement step
        self.build = None
        self.build_paid = None           # PORT10-Build: (gold, lumber) already paid for the armed build (CAbilityBuild §5.3)
        self.footprint = None
        self.constructing = False
        self.construct_done = 0
        self.construct_rate = 0.0
        self.construct_dmg = 0.0         # damage taken while constructing: the refund penalty term +0x10c
        self.wake_at = -1
        self.bkt = None
        self.path_partial = False
        self.seg = None                  # A19b: (target x, y, at x, y, ux, uy, remaining, heading deg) of the current segment
        self.hx = 1.0                    # STEER (switch on): the commanded heading (unit vector), the facing the movement last wrote (-1: none: the heading follows the facing), cos/sin of the turn rate, cos of the propulsion window
        self.hy = 0.0
        self.hf = -1.0
        self.tc = None

    def _now(self):
        return self.vm.steps if self.vm else 0

    @property
    def life(self):
        """time-linear value: clamp(base + rate * (now - stamp), 0, max) (NOTES_units §5)."""
        return self.life_at(self._now())

    def life_at(self, step):
        """the same value at an engine step (W9-5: the state events look ahead to the step a regeneration crosses)."""
        if self.life_rate == 0.0:
            return self.life_base
        v = self.life_base + self.life_rate * ((step - self.life_t) * 0.005)
        if v > self.maxlife:
            v = self.maxlife
        if v < 0.0:
            v = 0.0
        return r32(v)

    @life.setter
    def life(self, v):
        self.life_base = r32(v)
        self.life_t = self._now()

    def set_life_rate(self, r):
        self.life_base = self.life
        self.life_t = self._now()
        self.life_rate = r

    @property
    def mana(self):
        return self.mana_at(self._now())

    def mana_at(self, step):
        if self.mana_rate == 0.0:
            return self.mana_base
        v = self.mana_base + self.mana_rate * ((step - self.mana_t) * 0.005)
        if v > self.maxmana:
            v = self.maxmana
        if v < 0.0:
            v = 0.0
        return r32(v)

    @mana.setter
    def mana(self, v):
        self.mana_base = r32(v)
        self.mana_t = self._now()

    def set_mana_rate(self, r):
        self.mana_base = self.mana
        self.mana_t = self._now()
        self.mana_rate = r

    def is_hero(self):
        c = (self.tid >> 24) & 0xff
        return 65 <= c <= 90         # T§5: type id's first character 'A'..'Z'


class Group(Agent):
    tname = 'group'

    def __init__(self):
        self.units = []


class EventReg(Agent):
    tname = 'event'

    def __init__(self, trig, kind, event_id, filt=None, src=None):
        self.trig = trig
        self.kind = kind
        self.event_id = event_id
        self.filter = filt
        self.src = src
        self.extra = None


class Item(Agent):
    """W8 item (items.py): record data, carrier unit + slot (None / −1 on the ground), life, charges."""
    tname = 'item'

    def __init__(self, tid, data, x, y):
        self.tid = tid
        self.data = data
        self.x, self.y = x, y
        hp = r32(float(data.get('HP', 0) or 0))
        self.life = self.maxlife = hp
        self.charges = int(data.get('uses', 0) or 0)
        self.uncharged = self.charges == 0       # flag 0x1000: uses 0 or SetItemCharges(0) → never runs out
        self.fl = ID.record_flags(data)          # the item's own flag word (CItem_p1 §4); SetItem* natives write it
        self.carrier = None
        self.slot = -1
        self.hidden = False
        self.owner = None
        self.serial = 0
        self.live = True                 # the item's own state (dies as a powerup / at life 0); alive = the handle


class Simple(Agent):
    """placeholder for everything that only needs an identity (effects, sounds, quests, dialogs ...)."""

    def __init__(self, tname, **kw):
        self.tname = tname
        self.__dict__.update(kw)


class Leaderboard(Agent):
    tname = 'leaderboard'

    def __init__(self):
        self.items = []              # [label, value, player]
        self.label = ''
        self.displayed = False
        self.size = 0


class GameCache(Agent):
    tname = 'gamecache'

    def __init__(self, name):
        self.name = name
        self.data = {}


# ---------------------------------------------------------------- registry

REG = {}


DUPLICATES = []              # names registered twice (a later @nat would silently replace the first; BUGS W8-4)


def nat(*names):
    def deco(fn):
        for n in names:
            if n in REG:
                DUPLICATES.append(n)
            REG[n] = fn
        return fn
    return deco


RNG_T = (                    # NOTES_abilities §4.1: the generator's 61-dword constant table [obs]
    0x9927148e, 0x08c7aafd, 0x1f3ee6d5, 0xda55bbf6, 0x6a4aa075, 0xff97bde8, 0x9fbc9bde, 0x46a18a81,
    0x63e30b6e, 0x5d6c7a76, 0xca69d388, 0x25b947c3, 0x3fa2ab83, 0xba7c41a6, 0x0195ace5, 0xc109cf7e,
    0x717062d9, 0x0205db8d, 0x54ef8724, 0x3037d4c6, 0x7bcb1bd0, 0xecd8e4b8, 0xdcadce49, 0xc494a913,
    0x0dae398f, 0x0edd5218, 0x85f5fa78, 0x6dafd258, 0x3b53b2a4, 0xbe50a551, 0x11f42dfc, 0xf1169848,
    0x663ddf86, 0x2f2e445e, 0x176b0736, 0xb64c298b, 0xe75f89e2, 0xe121a7cd, 0xed65c94d, 0x239ceefe,
    0x04b77d33, 0x402a9a9e, 0xf35b10b3, 0x921c7782, 0x571e4e20, 0x8c067222, 0xfb732c67, 0xbf0ac259,
    0x0cf95c79, 0x68121a28, 0x42193474, 0xf884c0b1, 0x9d15f038, 0x6f3af260, 0x91eb90b4, 0x61357f1d,
    0x5603325a, 0x932bc5a3, 0x434b0f80, 0x3ce0a8f7, 0x2664d196)
GAME_STREAMS = 45


def _rotl(x, n):
    return ((x << n) | (x >> (32 - n))) & 0xFFFFFFFF


class RNG:
    """The game's generator (NOTES_abilities §4.1): additive lagged table generator, state = sum + four table
    indices (dword indices into RNG_T, stepping down by 1 / 3 / 6 / 7 modulo 47 / 53 / 59 / 61)."""

    def __init__(self, seed=1):
        self.seed(seed)

    def seed(self, s):
        s &= 0xFFFFFFFF
        self.sum = s
        self.i3, self.i2, self.i1, self.i0 = s % 47, s % 53, s % 59, s % 61

    def state(self):
        """(sum, idx) as the game stores it: idx = byte offsets i3 i2 i1 i0 packed high to low."""
        return self.sum, (self.i3 << 26 | self.i2 << 18 | self.i1 << 10 | self.i0 << 2) & 0xFFFFFFFF

    def next(self):
        self.i3 = self.i3 - 1 if self.i3 >= 1 else self.i3 + 46
        self.i2 = self.i2 - 3 if self.i2 >= 3 else self.i2 + 50
        self.i1 = self.i1 - 6 if self.i1 >= 6 else self.i1 + 53
        self.i0 = self.i0 - 7 if self.i0 >= 7 else self.i0 + 54
        T = RNG_T
        self.sum = (self.sum + (_rotl(T[self.i1], 3) ^ _rotl(T[self.i2], 2) ^ T[self.i0] ^ _rotl(T[self.i3], 1))) \
            & 0xFFFFFFFF
        return self.sum

    def below(self, n):
        """integer in [0, n): high 32 bits of sum * n."""
        return (self.next() * (n & 0xFFFFFFFF)) >> 32

    def unit(self):
        """real in [0, 1): 23 mantissa bits of the sum as 1.m − 1 (exact in float32)."""
        return (self.next() & 0x7FFFFF) / 8388608.0

    def int(self, a, b):
        """GetRandomInt (NOTES_abilities §4.2): a + below(|b − a| + 1); the first argument is the base even when
        a > b (game quirk [obs])."""
        if a == b:
            return a
        return s32(a + self.below(abs(b - a) + 1))

    def real(self, a, b):
        """GetRandomReal: a + U · |b − a| in float32 (rounding of the intermediate product: A-RNG1 [inf]). The first argument is
        the base, like the integer form: GetRandomReal(10, 5) = 10 + U*5 in [10, 15] [obs runtime 1.26a docs/oracle.md
        "GetRandomReal(10, 5)", ext_determinism]. The stream is untouched (a span of 0 draws nothing)."""
        span = r32(abs(b - a))
        if span == 0.0:
            return a
        return r32(a + r32(self.unit() * span))


class Streams:
    """The JASS/global stream plus the 45 synced game streams (NOTES_abilities §4.2), all from one game seed."""

    def __init__(self, seed=1):
        self.jass = RNG(seed)
        self.game = [RNG(0) for _ in range(GAME_STREAMS)]
        self.seed_game(seed)

    def seed_game(self, s):
        master = RNG(s)
        for g in self.game:
            g.seed(master.next())

    def set_random_seed(self, s):
        """SetRandomSeed: JASS = Seed(s), one JASS step, all game streams re-seeded from that step's sum [obs]."""
        self.jass.seed(s)
        self.seed_game(self.jass.next())


def fmt_real(v):
    if v != v:
        return 'nan'
    if v in (float('inf'), float('-inf')):
        return 'inf' if v > 0 else '-inf'
    for p in range(1, 10):
        s = '%.*g' % (p, v)
        if r32(float(s)) == v:
            return repr(float(s))          # shortest decimal that round-trips the float32, no exponent
    return repr(v)


def fmt_str(s):
    if s is None:
        return 'null'
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r') + '"'


class Natives:
    def __init__(self, vm, unit_types=None, humans=1, seed=1, wts=None, trace_lines=None, env=None):
        self.vm = vm
        self.p = vm.p
        self.unit_types = unit_types or {}
        self.humans = humans
        self.slot_order = None       # BUGS FF-SLOTS: the user slots in the order humans take them (None = 0, 1, 2 ...)
        self.ai_levels = None        # S7: lobby computer level per slot (0 easy / 1 normal / 2 insane = GetAIDifficulty), None / -1 = the game difficulty
        self.race_choice = None      # BUGS MELEE-RACE: lobby race pick per slot (0 map's own, 1 human, 2 orc, 3 undead, 4 night elf, 5 random); None = no pick
        self.race_seed = seed
        self.streams = Streams(seed)
        self.rng = self.streams.jass
        self.wts = wts or {}
        self.players = [Player(i) for i in range(16)]
        self.units = []              # creation order (= spatial query order, Open)
        self.unit_serial = 0
        self.moving = set()
        self.dirty = set()
        self.ur_list = []            # W9-5 TriggerRegisterUnitInRange registry: (watched serial, order, reg), sorted
        self.ur_seq = 0
        self.us_list = []            # W9-5 TriggerRegisterUnitStateEvent registry (registration order)
        self.us_seq = 0
        self.us_sched = []           # W9-5 regeneration crossings seen by the look-ahead: sorted (step, order, reg)
        self.region_pend = []        # ETD-IDX: region events whose filter already ran (CreateUnit / SetUnitPosition)
        self.region_index = {}       # bucket (bx, by) -> [[region, x0, y0, x1, y1, add]] (region_op)
        self.regions = []
        self.camera_bounds = (-4096.0, -4096.0, 4096.0, 4096.0)
        self.start_locs = {}
        self.map_name = ''
        self.game = {'players': 0, 'teams': 0, 'placement': 0, 'difficulty': 1,
                     'speed': {3: 0, 4: 1}.get(vm.steps_per_turn, 2), 'flags': {}}   # T§1.2a: lobby speed
        self.float_state = {}
        self.int_state = {}
        self.gstate_regs = []            # PORT-GameState: TriggerRegisterGameStateEvent registrations (edge-triggered)
        self.caches = {}
        self.lines = trace_lines     # list for the trace or None
        self.stub_counts = {}
        self.last_text = []
        self.hud_dlg = {}                # DBZ-DIALOG: player id -> the dialog handle the player's UI shows (display state; DialogDisplay / a click)
        self.sounds = []                 # (game ms, file, x, y, 3d, volume) per StartSound (docs/sound_plan.md S2)
        self.sndlog = []                 # A-SN2: start / stop events as the U# queue holds them (rule tests: sound_lines)
        self.texttags = []               # live TextTag objects, creation order (td_plan T4; display only)
        self.multiboards = []            # live multiboards, creation order (td_plan T6; display only)
        self.mm_ally = 0                 # PORT5-NativesB round 2: minimap filter states (0041 §4; no UI, script-visible round trip)
        self.mm_creep = False
        self.dp_pend = []            # NativesB round 3: delayed UnitDamagePoint (due clock, src handle, x, y, radius, amount, atk, dmg, flags)
        self.mb_current = None           # PORT5-NativesB: the board MultiboardDisplay registered last; mb_suppress: MultiboardSuppressDisplay
        self.mb_suppress = False
        self.effects = []                # special effects, creation order (td_plan T4; display only)
        self.quests = []                 # quests, creation order (td_plan T4: the quest log)
        self.weather = []                # weather effects, creation order (td_plan T4; display only)
        self.fog = [True, True, True]    # FogEnable, FogMaskEnable, EnableWorldFogBoundary (td_score: display state)
        self.fogmods = []                # fog modifiers, creation order
        self.fogg = None                 # FOG: the fog grid (tools/jvm/fog.py), created on first use
        self.images = []                 # td_score display state: images (CreateImage), creation order (A-UI3)
        # td_score display state (A-UI1..3): time of day (lazy: base hour at t0, scale, suspended), DNC models, user UI /
        # control, the host's selection requests, minimap pings
        self.tod = [12.0, 0, 1.0, False]
        self.ui = {'dnc': None, 'ui': True, 'ctl': True, 'sel': [], 'pings': [], 'sky': None, 'colors': {},
                   'iface': True, 'subs': False, 'scene': None}
        # S5 music state for W3Music (A-SN1): map music (name, random, index), the music played now, stopped,
        # thematic track, volume 0..127, sound environment, volume groups; seq bumps on every music change
        self.music = {'map': None, 'play': None, 'stopped': False, 'thematic': None, 'vol': 127, 'env': None,
                      'groups': {}, 'seq': 0}
        self.cine = {'tex': '', 'blend': 0, 'flags': 0, 'suv': (0.0, 0.0, 1.0, 1.0), 'euv': (0.0, 0.0, 1.0, 1.0),
                     'sc': -1, 'ec': -1, 'dur': 0.0, 'shown': False, 't0': 0}   # td_plan T4 cinematic filter
        self.ev_names = {}
        self.chat_cmds = []              # BUGS CHAT-1: every chat string the map registers (static scan, then runtime), append-only
        self.chat_regs = []              # the chat registrations, registration order
        from . import gamecache
        self.gc_disk = gamecache.GcDisk()    # SAVE-2: the game cache file (the player's PlayerData in the world); tests / the launcher may swap it
        self.chat_dyn = []               # SAVE-1: typed messages that are no table entry ("-load CODE"), a ring of CHAT_DYN; index CHAT_MAX + k
        self.chat_dyn_n = 0
        self.chat_dyn_owner = []         # SAVE-1: the player each ring entry was typed by (a kind-7 input of another player is refused)
        self.save_capture = None         # SAVE-1: a savecode.SaveCapture (the texts shown after a "-save" are scanned) or None
        self.ais = {}                    # PORT3-AI: slot -> ai.AIPlayer (the computer-player object)
        self.ai_current = None           # the AI script thread's AI (None outside one)
        self.ai_host = None              # aivm.AiHost when the real AI path is on (run.build real_ai=True); None = LtwBot only
        self.bots = []                   # LtwBot/MeleeBot controllers started by StartMeleeAI or bot.attach
        self.bot_slots = set()           # slots that already own a bot controller
        self.unit_to_player_ev = {}
        self.vm.on_step.append(self.step)
        self.corpses = []
        self.timer_internal = []
        from .engine import Engine
        self.eng = Engine(self, env or {}, seed)       # W5 engine core (tools/jvm/engine.py)

    # ---------------------------------------------------------- binding
    def bind(self):
        p = self.p
        self.chat_cmds = chat_scan(p)
        fns = []
        for idx, info in enumerate(p.natives):
            fns.append(self._make(idx, info))
        self.vm.natives = fns
        self.vm.world = self
        self.vm.stub_counts = self.stub_counts

    def _fmt_for(self, t):
        if t == 'integer':
            return str
        if t == 'real':
            return fmt_real
        if t == 'string':
            return fmt_str
        if t == 'boolean':
            return lambda v: 'true' if v else 'false'
        if t == 'code':
            fl = self.p.functions
            return lambda v: ('fn:' + fl[v - 1].name) if 0 < v <= len(fl) else ('null' if v == 0 else 'fn?%d' % v)
        if t == 'nothing':
            return None
        if t in self.p.enum_types:
            return str                     # Convert* enums are raw integers
        return lambda v: ('h%x' % v) if v else 'null'

    def _make(self, idx, info):
        name = info['name']
        fn = REG.get(name)
        ret = info['ret']
        kind = 'impl'
        if fn is None:
            kind = 'stub' if STUB_RE.search(name) else 'unknown'
        argf = [self._fmt_for(t) for t in info['params']]
        retf = self._fmt_for(ret)
        vm = self.vm
        is_handle_ret = stub_creates_handle(name, ret)
        zero = None if ret == 'nothing' else (0.0 if ret == 'real' else (None if ret == 'string' else 0))
        counts = vm.native_counts
        N = self
        tname = ret

        def call(args):
            counts[name] = counts.get(name, 0) + 1
            lines = N.lines
            if lines is not None:
                th = vm.thread_stack[-1] if vm.thread_stack else None
                li = len(lines)
                lines.append(None)
                head = 't=%d thread=%d %s(%s)' % (vm.game_ms(), th.id if th else 0, name,
                                                   ', '.join(f(a) for f, a in zip(argf, args)))
            if kind == 'impl':
                res = fn(N, *args)
            elif kind == 'stub':
                N.stub_counts[name] = N.stub_counts.get(name, 0) + 1
                res = zero
                if is_handle_ret:
                    res = vm.h(Simple(tname))
            else:
                vm.unknown_counts[name] = vm.unknown_counts.get(name, 0) + 1
                res = zero
            if ret == 'nothing':
                res = None
            elif res is None:
                res = zero
            elif ret == 'boolean':
                res = 1 if res else 0
            elif ret == 'real':
                res = r32(float(res))
            elif isinstance(res, Agent):
                res = vm.h(res)
            if lines is not None:
                tail = '' if retf is None else ' -> ' + retf(res)
                lines[li] = head + tail + (' ?unknown' if kind == 'unknown' else '')
            return res
        return call

    # ---------------------------------------------------------- lookups
    def o(self, hid, cls=None):
        return self.vm.obj(hid, cls)

    def o_read(self, hid, cls=None):
        """BUGS RM-READ [obs runtime oracle rmget1 / rmget2]: a unit removed in this step still answers the unit
        getters (the real game keeps it until the next tick: a 0 s timer reads 0); everything else sees it gone."""
        o = self.vm.obj(hid, cls)
        if o is None and hid:
            g = self.vm.H.get(hid)
            if g is not None and getattr(g, 'removed_step', -1) == self.vm.steps and (cls is None or isinstance(g, cls)):
                return g
        return o

    def H(self, obj):
        return self.vm.h(obj) if obj is not None else 0

    def ev_id(self, name):
        if name not in self.ev_names:
            gi = self.p.global_by_name.get(name)
            v = self.vm.G[gi] if gi is not None else None
            self.ev_names[name] = v if isinstance(v, int) else -1
        return self.ev_names[name]

    def reset_display_state(self):
        """GAMEOVER-DIALOG: per-game reset of everything display-only (dialogs / texts, boards, quests, effects,
        weather, images, fog modifiers, music and the user-UI state). The U# side does the same in
        W3Vm.ResetDisplayState / HudReset; a new Natives object starts with these values too."""
        self.last_text = []
        self.hud_dlg = {}
        self.sounds = []
        self.sndlog = []
        self.texttags = []
        self.multiboards = []
        self.mb_current = None
        self.mb_suppress = False
        self.mm_ally = 0
        self.mm_creep = False
        self.effects = []
        self.quests = []
        self.weather = []
        self.images = []
        self.fogmods = []
        self.fog = [True, True, True]
        self.ui = {'dnc': None, 'ui': True, 'ctl': True, 'sel': [], 'pings': [], 'sky': None, 'colors': {},
                   'iface': True, 'subs': False, 'scene': None}
        self.cine = {'tex': '', 'blend': 0, 'flags': 0, 'suv': (0.0, 0.0, 1.0, 1.0),
                     'euv': (0.0, 0.0, 1.0, 1.0), 'sc': -1, 'ec': -1, 'dur': 0.0, 'shown': False, 't0': 0}
        self.music = {'map': None, 'play': None, 'stopped': False, 'thematic': None, 'vol': 127, 'env': None,
                      'groups': {}, 'seq': 0}

    def after_globals(self):
        """map unit events <-> player unit events by name (EVENT_UNIT_X <-> EVENT_PLAYER_UNIT_X)."""
        self.reset_display_state()
        for g in self.p.globals:
            n = g['name']
            if n.startswith('EVENT_PLAYER_UNIT_'):
                u = 'EVENT_UNIT_' + n[len('EVENT_PLAYER_UNIT_'):]
                pe, ue = self.ev_id(n), self.ev_id(u)
                if pe >= 0 and ue >= 0:
                    self.unit_to_player_ev[pe] = ue

    def player(self, i):
        return self.players[i] if 0 <= i < 16 else None

    # BUGS MELEE-RACE: pick (1 human, 2 orc, 3 undead, 4 night elf) -> the race_pref bit GetPlayerRace maps back to it
    RACE_PICK_PREF = {1: 1, 2: 2, 3: 8, 4: 4}   # RACE_PREF_UNDEAD 8, RACE_PREF_NIGHTELF 4 (common.j)

    @staticmethod
    def random_race(seed, slot):
        """Random in the lobby: one of the four races from the game seed and the slot (same on every client)."""
        h = ((seed & 0xFFFFFFFF) * 1103515245 + 12345 + slot * 2654435) & 0x7FFFFFFF
        h = (h * 1103515245 + 12345) & 0x7FFFFFFF
        return (h >> 8) % 4 + 1

    def apply_race_choice(self):
        """BUGS MELEE-RACE: after the map's config() (which sets the slots' preferences and 'selectable') and before
        main() (MeleeStartingUnits): a slot the lobby gave a race takes it as its race preference; a slot the map fixed
        (not selectable) keeps the map's race. No picks = nothing changes."""
        if not self.race_choice:
            return
        for i, c in enumerate(self.race_choice[:12]):
            pl = self.players[i]
            if c == 5:
                c = self.random_race(self.race_seed, i)
            if c in self.RACE_PICK_PREF and pl.race_selectable:
                pl.race_pref = self.RACE_PICK_PREF[c]

    def setup_lobby(self):
        """slot states after the lobby: user slots < humans and computer slots are playing (Open)."""
        self.apply_race_choice()
        human_slots = set((self.slot_order or range(12))[:self.humans])
        for pl in self.players[:12]:
            if pl.controller == 0:
                pl.slot_state = 1 if pl.id in human_slots else 0
            elif pl.controller == 1:
                pl.slot_state = 1
            else:
                pl.slot_state = 0

    # ---------------------------------------------------------- units / world
    def create_unit(self, owner, tid, x, y, facing, constructing=False, food=True, made=True):
        data = self.unit_types.get(tid)
        if data is None:                  # invalid type id -> no unit, null (Open: checked only by WC3 lore)
            self.vm.errors.append('t=%d CreateUnit: unknown unit type %r' % (self.vm.game_ms(), _id4(tid)))
            return None
        self.unit_serial += 1
        u = Unit(self.unit_serial, tid, owner, r32(x), r32(y), r32(facing), data, self.vm)
        self.units.append(u)
        self.dirty.add(u)
        # BUGS C-SHOP: the regions the unit is born into are fixed at creation [inf]: a region made (or grown) later
        # gives no enter event for a unit that already stands there (LTW7's shops stand in gg_rct_Spawn before
        # "Buy Towers" registers it; the map only works when they stay). The event itself still fires at the next
        # step (dirty units), as before.
        c = self.cell_of(u.x, u.y)
        u.born = (c, self.regions_at(c))
        if constructing:
            self.eng.begin_construction(u)
        self.eng.init_unit(u)
        if u.guard:
            self.eng.guard_arm(u)
        if not constructing:              # BUGS PB-3: a stacked creation moves to a free cell; its birth regions follow
            ox, oy = u.x, u.y
            self.eng.spread_spawn(u)
            if u.x != ox or u.y != oy:
                # BUGS DBZ-SPAWN [inf]: the regions of the REQUESTED point stay in the birth set (the map's "enter region" order was meant for the units it created
                # there; a 25 unit clump pushed over the rect's edge would stand at the spawn for ever), the cell it stands on now is added
                c = self.cell_of(u.x, u.y)
                u.born = (c, u.born[1] | self.regions_at(c))
        if self.fogg is not None:
            self.fogg.unit_added(u)       # EXT-ow3 SPEC-0032: a created unit reveals at once (not at the next periodic update)
        self.food_add(owner, int(data.get('fused', 0) or 0) if food else 0,
                       int(data.get('fmade', 0) or 0) if made else 0)
        # BUGS ETD-IDX [obs runtime oracle enter1]: the enter-region filters run inside CreateUnit (unit indexers
        # read GetUnitUserData right after it); the actions follow at the next step (after the thread yields)
        if any(rg.enter_regs for rg in u.born[1]):
            hid = self.H(u)
            for rg in sorted(u.born[1], key=lambda r: r.hid):
                self._region_queue(rg, rg.enter_regs, u, hid, 'EVENT_GAME_ENTER_REGION', 'entering')
        u.born = (c, set())               # entered now; the next step only joins the cell (C-SHOP: later regions silent)
        return u

    def remove_unit(self, u):
        if u is None or not u.alive:
            return
        if u in self.units:
            self.units.remove(u)
        self.moving.discard(u)
        self.dirty.discard(u)
        if u in self.corpses:
            self.corpses.remove(u)
        u.regions = set()
        self.eng.on_remove(u)
        self.vm.H.destroyed(u)
        if not u.dead:                    # a living unit leaves: its food goes (A-PR4 [inf])
            self.eng.queue_abort(u)
            if not u.consumed:            # PORT4-Build: a consumed builder's food went at the consumption
                self.food_add(u.owner, -int(u.data.get('fused', 0) or 0), -int(u.data.get('fmade', 0) or 0))
        self.eng.revive_removed(u)        # PORT10-Queue §4.38: a removed queued hero ends its slot silently

    def kill_unit(self, u, killer=None):
        """NOTES_combat §4: dead flag, life 0, orders / movement stopped, bounty, food, EVENT_UNIT_DEATH (sync)."""
        if u is None or not u.alive or u.dead:
            return
        u.dead = True
        u.life = 0.0
        u.order = 0
        u.target = None
        self.moving.discard(u)
        u.death_time = self.vm.qgame.clock
        u.decay_off = False
        self.corpses.append(u)
        reinc = self.eng.reinc_begin(u)               # BUGS BOSS-ANKH: the Ankh is used up before the carrier's items drop
        self.eng.note_revivable(u)                    # CAbilityRevive §3: the dead hero enters the owner's revivable list
        self.eng.on_death(u, killer)
        _score_kill(self, u, killer)
        self.food_add(u.owner, -int(u.data.get('fused', 0) or 0), -int(u.data.get('fmade', 0) or 0))
        self.unit_event(u, 'DEATH', {'dying': self.H(u), 'killing': self.H(killer)})
        UE.unit_died(self, u, killer)                 # W9-5: EVENT_WIDGET_DEATH after EVENT_UNIT_DEATH
        if not reinc:
            IP.hero_died(self, u, killer)             # W9R2-2: EVENT_*_HERO_REVIVABLE after the death events (not for an Ankh revive [obs])

    def unit_event(self, u, suffix, data, player=None):
        """player-unit registrations of the owner first, then the unit-specific ones (NOTES_production §1.4 [obs])."""
        self.raise_unit_event(u, self.ev_id('EVENT_UNIT_' + suffix), self.ev_id('EVENT_PLAYER_UNIT_' + suffix), data, player)

    def raise_unit_event(self, u, uev, pev, data, player=None):
        """unit_event for explicit event ids (W9-5: EVENT_WIDGET_DEATH has no player-unit twin: pev -1)."""
        vm = self.vm
        hid = self.H(u)
        evd = dict(data)
        pl = player if player is not None else u.owner          # BUGS SEL-1: a selection event is the selecting player's
        for ev, regs in ((pev, pl.unit_regs.get(pev, ())), (uev, u.unit_regs.get(uev, ()))):
            for reg in list(regs):
                if not reg.alive or not reg.trig.alive:
                    continue
                if reg.filter is not None and not vm.eval_filter(reg.filter, V.CTX_FILTER_UNIT, hid):
                    continue
                evd['event'] = ev
                if 'dmgbox' in evd:
                    evd['damage'] = evd['dmgbox'][0]              # NAT-2: a trigger before this one may have changed it
                vm.fire(reg.trig, {V.CTX_TRIGGER_UNIT: hid, V.CTX_TRIGGER_WIDGET: hid,
                                   V.CTX_TRIGGER_PLAYER: self.H(pl), V.CTX_EVENT_ID: ev,
                                   V.CTX_SOURCE: hid}, dict(evd))

    def hero_event(self, u, suffix, data, slots=None):
        """EVENT_PLAYER_HERO_<suffix> registrations of the owner, then EVENT_UNIT_HERO_<suffix> of the unit
        (NOTES_heroes §1.3 step 6, §3). `slots`: extra context slots pushed for the run (W9R2-2 CTX_REVIVABLE)."""
        vm = self.vm
        uev = self.ev_id('EVENT_UNIT_HERO_' + suffix)
        pev = self.ev_id('EVENT_PLAYER_HERO_' + suffix)
        hid = self.H(u)
        evd = dict(data)
        for ev, regs in ((pev, u.owner.unit_regs.get(pev, ())), (uev, u.unit_regs.get(uev, ()))):
            for reg in list(regs):
                if not reg.alive or not reg.trig.alive:
                    continue
                if reg.filter is not None and not vm.eval_filter(reg.filter, V.CTX_FILTER_UNIT, hid):
                    continue
                evd['event'] = ev
                sl = {V.CTX_TRIGGER_UNIT: hid, V.CTX_TRIGGER_WIDGET: hid,
                      V.CTX_TRIGGER_PLAYER: self.H(u.owner), V.CTX_EVENT_ID: ev, V.CTX_SOURCE: hid}
                if slots:
                    sl.update(slots)
                vm.fire(reg.trig, sl, dict(evd))

    def revive_hero(self, uo, x, y, life=None, mana=None, food=True):
        return _revive_hero(self, uo, x, y, life, mana, food)

    def reinc_revive(self, uo, life):
        """Ankh of Reincarnation: in place, DataB life (not above max life), mana as it was, no event of any kind [obs]."""
        _revive_hero(self, uo, uo.x, uo.y, min(life, uo.maxlife), uo.mana)

    def reinc_unit_revive(self, uo):
        """BUGS ETD-REINC (ACrn): a dead non-hero unit comes back where it lay with full life and mana, no event of its own (the
        unit state events see the life rise at their next pass); its food counts again."""
        if uo is None or not uo.alive or not uo.dead:
            return False
        uo.dead = False
        uo.death_time = None
        uo.rc_wait = False
        if uo in self.corpses:
            self.corpses.remove(uo)
        uo.life = uo.maxlife
        uo.mana = uo.maxmana
        self.eng.set_regen(uo)
        _refresh_fighter(self, uo)
        _set_pos_native(self, uo, uo.x, uo.y)
        self.food_add(uo.owner, int(uo.data.get('fused', 0) or 0), int(uo.data.get('fmade', 0) or 0))
        return True

    def set_unit_pos(self, uo, x, y):
        _set_pos(self, uo, x, y)

    # ---- player state + food (EVENT_PLAYER_STATE_LIMIT, edge-triggered: Open)
    @staticmethod
    def _limit_ok(v, op, lim):
        return {0: v < lim, 1: v <= lim, 2: v == lim, 3: v >= lim, 4: v > lim, 5: v != lim}.get(op, False)

    def set_player_state(self, pl, state, v):
        old = pl.state.get(state, 0)
        pl.state[state] = v
        if old == v:
            return
        if state == 5:
            self.upkeep_update(pl)       # UPKEEP [obs runtime 1.21]: the rates follow FOOD_USED in the same tick
        hid = self.H(pl)
        for reg in list(pl.player_regs.get('state', ())):
            st, op, lim, was = reg.extra
            if st != state or not reg.alive:
                continue
            now = self._limit_ok(v, op, lim)
            reg.extra[3] = now
            if now and not was and reg.trig.alive:
                self.vm.fire(reg.trig, {V.CTX_TRIGGER_PLAYER: hid, V.CTX_EVENT_ID: reg.event_id,
                                        V.CTX_SOURCE: hid}, {'pstate': state, 'event': reg.event_id})

    # ---- UPKEEP (NOTES_production §2.3, probe upkeep_tiers [obs runtime 1.21])
    def upkeep_level(self, food):
        """number of thresholds strictly below FOOD_USED; the walk stops at the first entry that is 0 or not below it."""
        lvl = 0
        for t in self.eng.upkeep_usage:
            if t <= 0 or not t < food:
                break
            lvl += 1
        return lvl

    def upkeep_update(self, pl):
        """states 13 / 14 = tax fraction x 100 of the level of the raw FOOD_USED (tax tables capped at index 9)."""
        e = self.eng
        lvl = min(self.upkeep_level(pl.state.get(5, 0)), 9)
        pl.state[13] = int(e.upkeep_gold[lvl] * 100.0 + 0.5)
        pl.state[14] = int(e.upkeep_lumber[lvl] * 100.0 + 0.5)

    def credit_resource(self, pl, state, tenths, tax=True):
        """the engine's resource credit (0x6f318d30): the amount in tenths loses rate x amount / 100 (truncated, rate = state 12 + id),
        the net is added to the stored tenths. Used by the bounty award (tax flag set) and by a harvest drop (not ported: no mining)."""
        if pl is None or not tenths:
            return 0
        if tax and tenths > 0:
            t = pl.state.get(12 + state, 0) * tenths // 100
            tenths -= max(0, min(t, tenths))
        tot = pl.state.get(state, 0) * 10 + pl.frac.get(state, 0) + tenths
        whole = tot // 10
        pl.frac[state] = tot - whole * 10
        self.set_player_state(pl, state, whole)
        return tenths

    def food_add(self, pl, used, made):
        """NOTES_production §2: FOOD_USED (5) and FOOD_CAP (4) change incrementally on top of the stored value
        (creation, death, removal, owner change, train start); a waiting train queue retries on any change."""
        if pl is None or (not used and not made):
            return
        if used:
            self.set_player_state(pl, 5, pl.state.get(5, 0) + used)
        if made:
            self.set_player_state(pl, 4, pl.state.get(4, 0) + made)
        self.eng.food_retry(pl)

    def issue_order(self, u, oid, x=None, y=None, target=None):
        """JASS natives never queue (NOTES_units §1): the engine replaces the current order; issued-order events
        fire after a taken order (sync, Open O5)."""
        if u is None or u.dead or not u.alive:
            return False
        if x is not None:
            x, y = r32(x), r32(y)
        if not self.eng.issue(u, oid, x, y, target):
            return False
        if target is not None:
            self.unit_event(u, 'ISSUED_TARGET_ORDER', {'ordered': self.H(u), 'order': oid,
                                                       'otarget': self.H(target), 'ox': target.x, 'oy': target.y})
        elif x is not None:
            self.unit_event(u, 'ISSUED_POINT_ORDER', {'ordered': self.H(u), 'order': oid, 'ox': x, 'oy': y})
        else:
            self.unit_event(u, 'ISSUED_ORDER', {'ordered': self.H(u), 'order': oid})
        return not (oid == 851988 and x is not None)   # MOVE-PORT [obs runtime 1.26a R2]: a point-order AImove runs but
                                                       # answers false; the target form keeps the validator answer (slot 191
                                                       # section 2.3 step 11 accepts AImove)

    def step(self, dt):
        """one 5 ms game step: the queued region events of CreateUnit / SetUnitPosition (their filters already ran),
        the engine (construction, missiles, combat, movement), then enter/leave region checks of the units that
        moved, then corpse decay."""
        _gstate_tick(self)                                   # PORT-GameState: time-of-day limit events of this step
        if self.dp_pend:
            _dp_tick(self)                                   # NativesB round 3: UnitDamagePoint with a delay that is due
        if self.eng.reinc and self.eng.reinc_due():
            self.eng.reinc_run()                                 # BUGS BOSS-ANKH: Ankh reincarnations that are due
        if self.region_pend:
            pend, self.region_pend = self.region_pend, []
            for rg, reg, u, hid, evname, key in pend:
                if reg.alive and reg.trig.alive and u.alive:
                    self.vm.fire(reg.trig, {V.CTX_TRIGGER_UNIT: hid, V.CTX_TRIGGER_WIDGET: hid,
                                            V.CTX_EVENT_ID: self.ev_id(evname), V.CTX_SOURCE: self.H(rg)},
                                 {key: hid, 'region': self.H(rg), 'event': self.ev_id(evname)})
        self.eng.step(dt)
        if self.dirty:
            for u in sorted(self.dirty, key=lambda u: u.serial):
                self.eng.wk += 1
                if u.alive:
                    self.update_regions(u)
            self.dirty = set()
        if self.corpses:
            now = self.vm.qgame.clock
            for u in list(self.corpses):
                self.eng.wk += 1
                if u.death_time is not None and now - u.death_time >= self.eng.decay_time(u) and not getattr(u, 'rc_wait', False) and not u.decay_off:
                    self.corpses.remove(u)
                    if not getattr(u, 'hero', False):        # BUGS NAT-13: a dead hero stays (ReviveHero)
                        # PORT2-UnitB: the reference posts PLAYER_UNIT_DECAY (21) then UNIT_DECAY (54) with the
                        # decaying unit as payload before the corpse leaves; GetDecayingUnit reads it.
                        # PORT2-FIX: no event without a registration (TD maps with many corpses; no handle made either)
                        pev, uev = self.ev_id('EVENT_PLAYER_UNIT_DECAY'), self.ev_id('EVENT_UNIT_DECAY')
                        if u.owner.unit_regs.get(pev) or u.unit_regs.get(uev):
                            self.unit_event(u, 'DECAY', {'decaying': self.H(u)})
                        self.remove_unit(u)
        if self.us_sched and self.us_sched[0][0] <= self.vm.steps:
            UE.sched_pass(self)                                  # W9-5: a regeneration crossing found by the look-ahead
        if (self.ur_list or self.us_list) and self.vm.steps % UE.UR_EVERY == 0:
            UE.pass_(self)                                       # W9-5: TriggerRegisterUnitInRange / UnitStateEvent (edge-triggered)

    def cell_of(self, x, y):
        return (int(math.floor(x / CELL)), int(math.floor(y / CELL)))

    def update_regions(self, u, fire=True, sync=False):
        c = self.cell_of(u.x, u.y)
        if c == u.cell:
            return
        u.cell = c
        born, u.born = u.born, None
        now = set() if u.dead else self.regions_at(c)
        old = u.regions
        if now == old:
            return
        u.regions = now
        if not fire:
            return
        hid = self.H(u)
        ev = self._region_queue if sync else self._region_event
        for rg in sorted(old - now, key=lambda r: r.hid):
            ev(rg, rg.leave_regs, u, hid, 'EVENT_GAME_LEAVE_REGION', 'leaving')
        enters = now - old
        if born is not None and born[0] == c:
            enters &= born[1]            # C-SHOP: regions newer than the unit are joined silently
        for rg in sorted(enters, key=lambda r: r.hid):
            ev(rg, rg.enter_regs, u, hid, 'EVENT_GAME_ENTER_REGION', 'entering')

    def _region_queue(self, rg, regs, u, hid, evname, key):
        """ETD-IDX: evaluate the registration filters now, queue the passing ones for the next step."""
        vm = self.vm
        for reg in list(regs):
            if not reg.alive or not reg.trig.alive or not u.alive:
                continue
            if reg.filter is not None and not vm.eval_filter(reg.filter, V.CTX_FILTER_UNIT, hid):
                continue
            self.region_pend.append((rg, reg, u, hid, evname, key))

    def _region_event(self, rg, regs, u, hid, evname, key):
        vm = self.vm
        ev = self.ev_id(evname)
        for reg in list(regs):
            if not reg.alive or not reg.trig.alive or not u.alive:
                continue
            if reg.filter is not None and not vm.eval_filter(reg.filter, V.CTX_FILTER_UNIT, hid):
                continue
            vm.fire(reg.trig, {V.CTX_TRIGGER_UNIT: hid, V.CTX_TRIGGER_WIDGET: hid, V.CTX_EVENT_ID: ev,
                               V.CTX_SOURCE: self.H(rg)}, {key: hid, 'region': self.H(rg), 'event': ev})

    # A region is the ordered list of its cell-range operations: a cell is in the region iff the last operation
    # covering it was an add (= a cell set with add / discard). Index: buckets of 16 x 16 cells -> the operations
    # touching the bucket, clipped to it, in operation order ([region, x0, y0, x1, y1, add], half-open ranges).
    # A new operation drops the older entries of its region that it fully covers; a clear is only recorded where it
    # overlaps an add entry of the region; entries of removed regions are dropped (a removed region is inert).
    def region_op(self, rg, x0, y0, x1, y1, add):
        """add / clear the cells [x0, x1) x [y0, y1)."""
        sz = 1 << RB_SHIFT
        for bx in range(x0 >> RB_SHIFT, ((x1 - 1) >> RB_SHIFT) + 1):
            cx = bx << RB_SHIFT
            a0, a1 = max(x0, cx), min(x1, cx + sz)
            for by in range(y0 >> RB_SHIFT, ((y1 - 1) >> RB_SHIFT) + 1):
                cy = by << RB_SHIFT
                b0, b1 = max(y0, cy), min(y1, cy + sz)
                lst = self.region_index.get((bx, by))
                if lst is None:
                    if not add:
                        continue
                    lst = self.region_index[(bx, by)] = []
                keep = []
                overlap = False
                for e in lst:
                    r = e[0]
                    if not r.alive:
                        continue
                    if r is rg:
                        if e[1] >= a0 and e[2] >= b0 and e[3] <= a1 and e[4] <= b1:
                            continue
                        if e[5] and e[1] < a1 and a0 < e[3] and e[2] < b1 and b0 < e[4]:
                            overlap = True
                    keep.append(e)
                if add or overlap:
                    keep.append([rg, a0, b0, a1, b1, add])
                lst[:] = keep

    def region_has(self, rg, c):
        cx, cy = c
        for e in reversed(self.region_index.get((cx >> RB_SHIFT, cy >> RB_SHIFT), ())):
            if e[0] is rg and e[1] <= cx < e[3] and e[2] <= cy < e[4]:
                return e[5]
        return False

    def regions_at(self, c):
        """the live regions containing cell c."""
        cx, cy = c
        seen = set()
        out = set()
        for e in reversed(self.region_index.get((cx >> RB_SHIFT, cy >> RB_SHIFT), ())):
            r = e[0]
            if not r.alive or r in seen or not (e[1] <= cx < e[3] and e[2] <= cy < e[4]):
                continue
            seen.add(r)
            if e[5]:
                out.add(r)
        return out

    def region_cell(self, rg, c, add):
        self.region_op(rg, c[0], c[1], c[0] + 1, c[1] + 1, add)

    def region_rect(self, rg, r, add):
        """cells covered by a rect: [floor(min/32), ceil(max/32)) in both axes (Open: exact snapping)."""
        x0 = int(math.floor(r.minx / CELL))
        y0 = int(math.floor(r.miny / CELL))
        x1 = max(x0 + 1, int(math.ceil(r.maxx / CELL)))
        y1 = max(y0 + 1, int(math.ceil(r.maxy / CELL)))
        self.region_op(rg, x0, y0, x1, y1, add)

    def group_add(self, g, u):
        """T§5 sorted insert: before the first member of the same type with a higher agent id; else a
        hero goes to the head, any other unit to the tail; duplicates ignored."""
        if u in g.units:
            return
        uid = self.H(u)
        for i, m in enumerate(g.units):
            if m.tid == u.tid and m.alive and self.H(m) > uid:
                g.units.insert(i, u)
                return
        if u.is_hero():
            g.units.insert(0, u)
        else:
            g.units.append(u)

    def enum_units(self, g, units, filt, limit=None):
        """clear, then per candidate: push GetFilterUnit, evaluate the filter in a new thread, add if true.
        Unclassed_Native_0018 §1: a limit of 0 means "no limit" (the frame gate is a non-zero test). `u.alive`
        is object liveness (a game-dead unit keeps it), so the of-type enumeration already keeps corpses."""
        vm = self.vm
        if g is None:
            return
        g.units = []
        n = 0
        for u in list(units):
            if not u.alive:
                continue
            if limit is not None and limit > 0 and n >= limit:
                break
            hid = self.H(u)
            if filt is None or vm.eval_filter(filt, V.CTX_FILTER_UNIT, hid):
                self.group_add(g, u)
                n += 1

    def text(self, kind, *args):
        self.last_text.append((self.vm.game_ms(), kind) + args)
        if kind == 'text' and self.save_capture is not None and len(args) >= 2 and isinstance(args[1], str):
            pl = args[0] if not isinstance(args[0], int) else self.o(args[0], Player)
            if pl is not None and hasattr(pl, 'id'):
                self.save_capture.on_text(pl.id, args[1], self.vm.game_ms() / 1000.0)


def _id4(v):
    v &= 0xFFFFFFFF
    return ''.join(chr((v >> s) & 0xff) for s in (24, 16, 8, 0))


def _bx(N, hid):
    return N.o(hid, BoolExpr)


# ================================================================ natives
# ---- conversions / math / strings

@nat('ConvertRace', 'ConvertAllianceType', 'ConvertRacePref', 'ConvertIGameState', 'ConvertFGameState',
     'ConvertPlayerState', 'ConvertPlayerScore', 'ConvertPlayerGameResult', 'ConvertUnitState',
     'ConvertAIDifficulty', 'ConvertGameEvent', 'ConvertPlayerEvent', 'ConvertPlayerUnitEvent',
     'ConvertWidgetEvent', 'ConvertDialogEvent', 'ConvertUnitEvent', 'ConvertLimitOp', 'ConvertUnitType',
     'ConvertGameSpeed', 'ConvertPlacement', 'ConvertStartLocPrio', 'ConvertGameDifficulty', 'ConvertGameType',
     'ConvertMapFlag', 'ConvertMapVisibility', 'ConvertMapSetting', 'ConvertMapDensity', 'ConvertMapControl',
     'ConvertPlayerColor', 'ConvertPlayerSlotState', 'ConvertVolumeGroup', 'ConvertCameraField',
     'ConvertBlendMode', 'ConvertRarityControl', 'ConvertTexMapFlags', 'ConvertFogState', 'ConvertEffectType',
     'ConvertVersion', 'ConvertItemType', 'ConvertAttackType', 'ConvertDamageType', 'ConvertWeaponType',
     'ConvertSoundType', 'ConvertPathingType')
def _convert(N, i):
    return i                      # enum handles are the raw integer (no handle table entry)


# W9 (1.24+ / 1.31 common.j): the new enum conversions, same rule
REG.update({_n: _convert for _n in (
     'ConvertAbilityBooleanField', 'ConvertAbilityBooleanLevelArrayField', 'ConvertAbilityBooleanLevelField', 'ConvertAbilityIntegerField',
     'ConvertAbilityIntegerLevelArrayField', 'ConvertAbilityIntegerLevelField', 'ConvertAbilityRealField', 'ConvertAbilityRealLevelArrayField',
     'ConvertAbilityRealLevelField', 'ConvertAbilityStringField', 'ConvertAbilityStringLevelArrayField', 'ConvertAbilityStringLevelField',
     'ConvertAnimType', 'ConvertArmorType', 'ConvertDefenseType', 'ConvertFrameEventType',
     'ConvertFramePointType', 'ConvertHeroAttribute', 'ConvertItemBooleanField', 'ConvertItemIntegerField',
     'ConvertItemRealField', 'ConvertItemStringField', 'ConvertMouseButtonType', 'ConvertMoveType',
     'ConvertOriginFrameType', 'ConvertOsKeyType', 'ConvertPathingFlag', 'ConvertRegenType',
     'ConvertSubAnimType', 'ConvertTargetFlag', 'ConvertTextAlignType', 'ConvertUnitBooleanField',
     'ConvertUnitCategory', 'ConvertUnitIntegerField', 'ConvertUnitRealField', 'ConvertUnitStringField',
     'ConvertUnitWeaponBooleanField', 'ConvertUnitWeaponIntegerField', 'ConvertUnitWeaponRealField', 'ConvertUnitWeaponStringField')})


@nat('OrderId')
def _orderid(N, s):
    return ORDER_IDS.get(s, 0)


@nat('OrderId2String')
def _orderid2s(N, i):
    return ORDER_NAMES.get(i)


@nat('UnitId')
def _unitid(N, s):
    """a unit-type name (case-sensitive here, the by-name natives retry lower-case) or a four-character id
    string such as 'hfoo': the latter is the form GroupEnumUnitsOfType / IssueBuildOrder / typed counts pass."""
    if not s:
        return 0
    for tid, d in N.unit_types.items():
        if d.get('name') == s:
            return tid
    if len(s) == 4:
        tid = (ord(s[0]) << 24) | (ord(s[1]) << 16) | (ord(s[2]) << 8) | ord(s[3])
        if tid in N.unit_types:
            return tid
    return 0


@nat('UnitId2String')
def _unitid2s(N, i):
    return None


@nat('AbilityId')
def _abilid(N, s):
    return 0


@nat('AbilityId2String')
def _abilid2s(N, i):
    return None


@nat('I2R')
def _i2r(N, i):
    return float(i)


@nat('R2I')
def _r2i(N, r):
    return r2i(r)


@nat('I2S')
def _i2s(N, i):
    return str(i)


@nat('R2S')
def _r2s(N, r):
    return '%.3f' % r


@nat('R2SW')
def _r2sw(N, r, w, p):
    try:
        return '%*.*f' % (w, max(min(p, 64), 0), r)       # precision <= 64 (BUGS UDON-THROW: RoundDigits' table)
    except (ValueError, OverflowError):
        return '%.3f' % r


def _atoi(s):
    m = re.match(r'\s*([+-]?\d+)', s or '')
    return s32(int(m.group(1))) if m else 0


@nat('S2I')
def _s2i(N, s):
    return _atoi(s)


@nat('S2R')
def _s2r(N, s):
    m = re.match(r'\s*([+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?)', s or '')
    return float(m.group(1)) if m else 0.0


@nat('SubString')
def _substring(N, s, a, b):
    if s is None:
        return None
    n = len(s)
    a = max(0, min(a, n))
    b = max(a, min(b, n))
    return s[a:b]


@nat('StringLength')
def _strlen(N, s):
    return len(s.encode('utf-8')) if s else 0


@nat('StringCase')
def _strcase(N, s, upper):
    if s is None:
        return None
    return s.upper() if upper else s.lower()


_LOC = []


def _loc_table():
    """BUGS GAMEOVER-DIALOG / ETD-REINC parity: the install's FDF string tables (tools/locstrings.py, the converter's JLOC
    source), read once from input/war3; an install without them answers {} (every key stays the key, as before)."""
    if not _LOC:
        tab = {}
        try:
            import locstrings
            from . import world as JW

            class _Core(object):
                read = staticmethod(JW.core_reader())
            tab = locstrings.collect_strings(_Core())
        except Exception:                                    # noqa: BLE001 - no install: the old fallback
            tab = {}
        _LOC.append(tab)
    return _LOC[0]


@nat('GetLocalizedString')
def _locstr(N, s):
    if not s:
        return s
    return _loc_table().get(s, s)


@nat('GetLocalizedHotkey')
def _lochot(N, s):
    return 0


@nat('Deg2Rad')
def _d2r(N, d):
    return d * math.pi / 180.0


@nat('Rad2Deg')
def _r2d(N, r):
    return r * 180.0 / math.pi


@nat('Sin')
def _sin(N, r):
    return math.sin(r)


@nat('Cos')
def _cos(N, r):
    return math.cos(r)


@nat('Tan')
def _tan(N, r):
    return math.tan(r)


@nat('Asin')
def _asin(N, r):
    """Unclassed_Native_0005 §3.1: 0.0 outside [-1, 1] (NaN included); the bounds are inclusive."""
    return math.asin(r) if -1.0 <= r <= 1.0 else 0.0


@nat('Acos')
def _acos(N, r):
    return math.acos(r) if -1.0 <= r <= 1.0 else 0.0


@nat('Atan')
def _atan(N, r):
    return math.atan(r)


@nat('Atan2')
def _atan2(N, y, x):
    return math.atan2(y, x)


@nat('SquareRoot')
def _sqrt(N, r):
    """Unclassed_Native_0005 §4.1: only r >= 0.001 is rooted; a negative or tiny non-negative argument is 0.0."""
    return math.sqrt(r) if r >= 0.001 else 0.0


@nat('Pow')
def _pow(N, a, b):
    try:
        return math.pow(a, b)
    except (ValueError, OverflowError):
        return 0.0


@nat('GetRandomInt')
def _rint(N, lo, hi):
    return N.rng.int(lo, hi)


@nat('GetRandomReal')
def _rreal(N, lo, hi):
    return N.rng.real(lo, hi)


@nat('SetRandomSeed')
def _rseed(N, s):
    N.streams.set_random_seed(s)


@nat('DebugS', 'DebugFI', 'DebugUnitID', 'DisplayText', 'DisplayTextI', 'DisplayTextII', 'DisplayTextIII',
     'DoNotSaveReplay', 'Cheat', 'ExecuteFuncNoop')
def _noop(N, *a):
    return None


# ---- game / map setup

@nat('SetMapName')
def _setmapname(N, s):
    N.map_name = s


@nat('SetMapDescription', 'SetGamePlacement', 'SetStartLocPrioCount',
     'SetStartLocPrio', 'SetEnemyStartLocPrioCount', 'SetEnemyStartLocPrio')
def _setgame(N, *a):
    return None


@nat('SetPlayers')
def _setplayers(N, n):
    N.game['players'] = n


@nat('SetTeams')
def _setteams(N, n):
    N.game['teams'] = n


@nat('GetPlayers')
def _getplayers(N):
    return N.game['players']


@nat('GetTeams')
def _getteams(N):
    return N.game['teams']


@nat('DefineStartLocation')
def _defstart(N, i, x, y):
    N.start_locs[i] = (x, y)


@nat('DefineStartLocationLoc')
def _defstartloc(N, i, l):
    lo = N.o(l, Location)
    if lo:
        N.start_locs[i] = (lo.x, lo.y)


@nat('GetStartLocationX')
def _startx(N, i):
    return N.start_locs.get(i, (0.0, 0.0))[0]


@nat('GetStartLocationY')
def _starty(N, i):
    return N.start_locs.get(i, (0.0, 0.0))[1]


@nat('GetStartLocationLoc')
def _startloc(N, i):
    x, y = N.start_locs.get(i, (0.0, 0.0))
    return Location(x, y)


@nat('GetPlayerStartLocationX')
def _pstartx(N, p):
    """Unclassed_Native_0006 §4: the player's assigned +0x274 indexes the start-location table."""
    pl = N.o(p, Player)
    if pl is None:
        return 0.0                                     # [inf] a null player reads nothing (the C# twin returns 0.0)
    return N.start_locs.get(pl.start_loc, (0.0, 0.0))[0]


@nat('GetPlayerStartLocationY')
def _pstarty(N, p):
    pl = N.o(p, Player)
    if pl is None:
        return 0.0
    return N.start_locs.get(pl.start_loc, (0.0, 0.0))[1]


@nat('GetGameTypeSelected')
def _gametype(N):
    return 1 << 3              # GAME_TYPE_USE_MAP_SETTINGS (Open)


@nat('IsGameTypeSupported')
def _gtsupp(N, t):
    return True


@nat('SetMapFlag')
def _setmapflag(N, f, v):
    N.game['flags'][f] = bool(v)


@nat('IsMapFlagSet')
def _ismapflag(N, f):
    return N.game['flags'].get(f, False)


@nat('GetGamePlacement')
def _getplacement(N):
    return N.game['placement']


MAP_LOCK_SPEED = 0x4000
GAME_SPEED_STEPS = (3, 4, 5)       # T§1.2: 5 ms steps per 25 ms turn for the stored speed 0 / 1 / 2 [obs 0x6f813644]


@nat('GetGameSpeed')
def _getspeed(N):
    return N.game['speed']             # the stored byte [obs 0x6f2b13d0]; lobby default fast = 2


@nat('SetGameSpeed')
def _setspeed(N, v):
    # T§1.2a [obs 0x6f2b13e0 -> 0x6f2f29a0 -> 0x6f292f90]: ignored while MAP_LOCK_SPEED is set; the low byte of the
    # value, everything above 1 (NORMAL, FAST, FASTEST, also a negative one) is stored as 2 = fast; steps per turn
    # from the table, read by the simulation loop at the start of a turn (the running turn keeps its count)
    if N.game['flags'].get(MAP_LOCK_SPEED, False):
        return
    b = v & 0xff
    sp = 2 if b > 1 else b
    N.game['speed'] = sp
    N.vm.steps_per_turn = GAME_SPEED_STEPS[sp]


@nat('SetGameDifficulty')
def _setdiff(N, d):
    N.game['difficulty'] = d


@nat('GetGameDifficulty', 'GetDefaultDifficulty')
def _getdiff(N):
    return N.game['difficulty']


@nat('GetResourceDensity', 'GetCreatureDensity')
def _density(N):
    return 1


@nat('VersionGet')
def _version(N):
    return 1                   # VERSION_FROZEN_THRONE


@nat('VersionCompatible', 'VersionSupported')
def _versionc(N, v):
    return True


@nat('SetFloatGameState')
def _setfgs(N, s, v):
    if s != 2:                 # Unclassed_Native_0038 §2: only the time-of-day state is a float state
        return
    old = _gstate_value(N, s)
    N.float_state[s] = v
    if s == 2:                 # a set time of day restarts the lazy cycle there
        N.tod[0] = v
        N.tod[1] = N.vm.game_ms()
    if r32(v) != old:
        _gstate_fire(N, s, r32(v))


TOD_DAY_S = 480.0              # A-UI1: game seconds per day [obs Units\\MiscData.txt DayLength=480, DayHours=24]


def tod_now(N):
    base, t0, scale, susp = N.tod
    if susp:
        return base
    return (base + (N.vm.game_ms() - t0) / 1000.0 * 24.0 / TOD_DAY_S * scale) % 24.0


def _tod_rebase(N):
    N.tod[0] = r32(tod_now(N))
    N.tod[1] = N.vm.game_ms()


def _gstate_value(N, s):
    """PORT-GameState: the value a game-state event compares against [obs CGameState_p1: the Float object +0x5c
    (time of day) and the Integer object +0x80; the port stores the integer states in int_state]."""
    if s == 2:
        return r32(tod_now(N))
    if s in N.int_state:
        return float(N.int_state[s])
    if s in N.float_state:
        return r32(N.float_state[s])
    return 0.0


def _gstate_limit_ok(op, v, lim):
    """CFloatGameStateEventReg §Port relevance: the base listener posts on a crossing with the 0.001 epsilon
    (the same constant CFloatEventReg posts an EQUAL event with)."""
    if op == 2:
        return abs(r32(v - lim)) < VAR_NE_EPS
    return limit_true(op, v, lim)


def _gstate_fire(N, s, v):
    """PORT-GameState: one game-state change, edge-triggered like EVENT_PLAYER_STATE_LIMIT (a registration fires
    when its compare turns false -> true) [inf from the shared notify pattern; CGameState_p1 0x80262/0x80268]."""
    for reg in list(N.gstate_regs):
        if not reg.alive or not reg.trig.alive:
            continue
        st, op, lim, was = reg.extra
        if st != s:
            continue
        now = _gstate_limit_ok(op, v, lim)            # CFloatGameStateEventReg: the base listener's 0.001 compare
        reg.extra[3] = now
        if now and not was:
            N.vm.fire(reg.trig, {V.CTX_EVENT_ID: reg.event_id},
                      {'event': reg.event_id, 'gstate': s})


def _gstate_tick(N):
    """PORT-GameState: time of day changes every step; compare it with the registrations once per step so an
    EQUAL / GREATER_THAN limit is seen at the step it is crossed (Blizzard.j dawn/dusk, Dark Deeds 4/6/16/18)."""
    if not N.gstate_regs:
        return
    for reg in N.gstate_regs:
        if reg.alive and reg.extra[0] == 2:
            _gstate_fire(N, 2, r32(tod_now(N)))
            return


@nat('GetFloatGameState')
def _getfgs(N, s):
    if s == 2:                 # GAME_STATE_TIME_OF_DAY: the lazy day cycle (A-UI1; start 12:00 [inf])
        return r32(tod_now(N))
    return 0.0                 # Unclassed_Native_0038 §2: every other state reads the default


@nat('SetTimeOfDayScale')
def _todscale(N, r):
    _tod_rebase(N)
    N.tod[2] = r


@nat('GetTimeOfDayScale')
def _gettodscale(N):
    return N.tod[2]


@nat('SuspendTimeOfDay')
def _todsuspend(N, b):
    _tod_rebase(N)
    N.tod[3] = bool(b)


@nat('SetDayNightModels')
def _dnc(N, terrain, unit):
    N.ui['dnc'] = (terrain or '', unit or '')


@nat('EnableUserUI')
def _userui(N, b):
    N.ui['ui'] = bool(b)


@nat('ShowInterface')
def _showiface(N, b, fade):
    N.ui['iface'] = bool(b)                     # A-UI4: cinematic mode hides the interface (the fade is presentation)


@nat('ForceCinematicSubtitles')
def _cinesubs(N, b):
    N.ui['subs'] = bool(b)


@nat('SetCinematicScene')
def _cinescene(N, uid, color, speaker, text, dur, vodur):
    # A-UI4: the transmission panel (portrait of the unit type, speaker, text) for the scene duration
    N.ui['scene'] = (_id4(uid), speaker or '', text or '', dur, vodur)


@nat('EndCinematicScene')
def _cineend(N):
    N.ui['scene'] = None


@nat('EnableUserControl')
def _userctl(N, b):
    N.ui['ctl'] = bool(b)


def _uisel(N, item):
    N.ui['sel'] = (N.ui['sel'] + [item])[-16:]


@nat('SelectUnit')
def _selectunit(N, u, flag):
    uo = N.o(u, Unit)
    if uo is not None:
        _uisel(N, ('%d u%d' % (1 if flag else 0, uo.serial)))
        # BUGS SEL-1 [inf]: the script's selection is the local player's (slot 0) and raises the selection events
        N.eng.select_op(N.players[0], 2 if flag else 3, uo)


@nat('ClearSelection')
def _clearsel(N):
    _uisel(N, '0')
    N.eng.select_op(N.players[0], 0, None)


@nat('PingMinimap')
def _ping(N, x, y, d):
    N.ui['pings'] = (N.ui['pings'] + [(x, y, d, 255, 255, 255)])[-8:]


@nat('PingMinimapEx')
def _pingex(N, x, y, d, r, g, b, extra):
    N.ui['pings'] = (N.ui['pings'] + [(x, y, d, r, g, b)])[-8:]


@nat('SetSkyModel')
def _skymodel(N, path):
    N.ui['sky'] = path or ''


def _ucolor(N, u):
    c = N.ui['colors']
    if u.serial not in c:
        c[u.serial] = [-1, (255, 255, 255, 255)]
    return c[u.serial]


def _sat255(v):
    return 0 if v < 0 else (255 if v > 255 else v)


@nat('SetUnitVertexColor')
def _uvertex(N, u, r, g, b, a):
    uo = N.o(u, Unit)
    if uo is not None:
        _ucolor(N, uo)[1] = (_sat255(r), _sat255(g), _sat255(b), _sat255(a))   # 0026 §6 [obs 0x6f2c5920]: saturate, not mask


@nat('SetUnitColor')
def _ucolorset(N, u, pc):
    uo = N.o(u, Unit)
    if uo is not None:
        _ucolor(N, uo)[0] = pc


def _mus(N, **kw):
    N.music.update(kw)
    N.music['seq'] += 1


@nat('SetMapMusic')
def _setmapmusic(N, name, rnd, idx):
    _mus(N, map=(name or '', bool(rnd), idx))


@nat('ClearMapMusic')
def _clearmapmusic(N):
    _mus(N, map=None)


@nat('PlayMusic')
def _playmusic(N, name):
    _mus(N, play=name or '', stopped=False)


@nat('PlayMusicEx')
def _playmusicex(N, name, frm, fade):
    _mus(N, play=name or '', stopped=False)


@nat('StopMusic')
def _stopmusic(N, fade):
    _mus(N, stopped=True)


@nat('ResumeMusic')
def _resumemusic(N):
    _mus(N, stopped=False)


@nat('PlayThematicMusic')
def _thematic(N, f):
    _mus(N, thematic=f or '')


@nat('PlayThematicMusicEx')
def _thematicex(N, f, frm):
    _mus(N, thematic=f or '')


@nat('EndThematicMusic')
def _endthematic(N):
    _mus(N, thematic=None)


@nat('SetMusicVolume')
def _musicvol(N, v):
    N.music['vol'] = v


@nat('NewSoundEnvironment')
def _soundenv(N, name):
    N.music['env'] = name or ''


@nat('VolumeGroupSetVolume')
def _vgset(N, g, scale):
    N.music['groups'][g] = scale


@nat('VolumeGroupReset')
def _vgreset(N):
    N.music['groups'] = {}


def music_lines(N):
    m = N.music
    mp = m['map']
    return ['map ' + ('%s random %d index %d' % (mp[0], 1 if mp[1] else 0, mp[2]) if mp else '-'),
            'play %s stopped %d' % (m['play'] if m['play'] is not None else '-', 1 if m['stopped'] else 0),
            'thematic %s volume %d' % (m['thematic'] if m['thematic'] is not None else '-', m['vol']),
            'env %s groups %s' % (m['env'] if m['env'] is not None else '-',
                                  ' '.join('%d:%s' % (g, fmt_real(v)) for g, v in sorted(m['groups'].items())) or '-'),
            'seq %d' % m['seq']]


@nat('CreateImage')
def _createimage(N, f, sx, sy, sz, x, y, z, ox, oy, oz, t):
    im = Simple('image', file=f or '', sx=sx, sy=sy, x=x, y=y, z=z, ox=ox, oy=oy, type=t, shown=False,
                color=(255, 255, 255, 255), ch=None, always=False, water=(False, False))
    N.images.append(im)
    return im


def _img(N, h):
    o = N.o(h)
    return o if isinstance(o, Simple) and o.tname == 'image' else None


@nat('ShowImage')
def _showimage(N, h, b):
    im = _img(N, h)
    if im:
        im.shown = bool(b)


@nat('SetImageColor')
def _imagecolor(N, h, r, g, b, a):
    im = _img(N, h)
    if im:
        im.color = (r & 255, g & 255, b & 255, a & 255)


@nat('SetImagePosition')
def _imagepos(N, h, x, y, z):
    im = _img(N, h)
    if im:
        im.x, im.y, im.z = x, y, z


@nat('SetImageConstantHeight')
def _imageheight(N, h, b, height):
    im = _img(N, h)
    if im:
        im.ch = height if b else None


@nat('SetImageRenderAlways')
def _imagealways(N, h, b):
    im = _img(N, h)
    if im:
        im.always = bool(b)


@nat('SetImageRender')
def _imagerender(N, h, b):
    im = _img(N, h)
    if im:
        im.always = bool(b)


@nat('SetImageAboveWater')
def _imagewater(N, h, b, wa):
    im = _img(N, h)
    if im:
        im.water = (bool(b), bool(wa))


@nat('SetImageType')
def _imagetype(N, h, t):
    im = _img(N, h)
    if im:
        im.type = t


def image_lines(N):
    out = []
    for im in N.images:
        out.append('%s|%s|%s|%s|%s|%s|%s|%s|%d|shown %d|color %d,%d,%d,%d|h %s|always %d|water %d,%d' % (
            im.file, fmt_real(im.sx), fmt_real(im.sy), fmt_real(im.x), fmt_real(im.y), fmt_real(im.z), fmt_real(im.ox),
            fmt_real(im.oy), im.type, 1 if im.shown else 0, im.color[0], im.color[1], im.color[2], im.color[3],
            fmt_real(im.ch) if im.ch is not None else '-', 1 if im.always else 0, 1 if im.water[0] else 0,
            1 if im.water[1] else 0))
    return out


def ui_lines(N):
    """td_score display state for the harness: DNC models, user UI / control, selection requests, pings."""
    u = N.ui
    out = ['dnc ' + ('|'.join(u['dnc']) if u['dnc'] else '-'), 'ui %d control %d' % (1 if u['ui'] else 0, 1 if u['ctl'] else 0),
           'sel ' + ' / '.join(u['sel'])]
    if u['sky'] is not None:
        out.append('sky ' + (u['sky'] or '-'))
    for ser in sorted(u['colors']):
        pc, c = u['colors'][ser]
        out.append('color u%d %d %d,%d,%d,%d' % (ser, pc, c[0], c[1], c[2], c[3]))
    out += ['ping %s|%s|%s|%d,%d,%d' % (fmt_real(p[0]), fmt_real(p[1]), fmt_real(p[2]), p[3], p[4], p[5]) for p in u['pings']]
    if not u['iface'] or u['subs']:
        out.append('iface %d subs %d' % (1 if u['iface'] else 0, 1 if u['subs'] else 0))
    if u['scene'] is not None:
        sc = u['scene']
        out.append('scene %s %s|%s %.3f %.3f' % (sc[0], sc[1], sc[2], sc[3], sc[4]))
    return out


@nat('SetIntegerGameState')
def _setigs(N, s, v):
    if s not in (0, 1):        # Unclassed_Native_0038 §2: only the two integer states exist
        return
    old = N.int_state.get(s, 0)
    N.int_state[s] = v
    if v != old:
        _gstate_fire(N, s, float(v))


@nat('GetIntegerGameState')
def _getigs(N, s):
    return N.int_state.get(s, 0) if s in (0, 1) else 0


@nat('IsNoVictoryCheat', 'IsNoDefeatCheat')
def _false1(N, *a):
    return False


@nat('EnableWorldFogBoundary')
def _fogbound(N, b):
    N.fog[2] = bool(b)


def fog_lines(N):
    """td_score display state: the switches, then the modifiers (R rect / C circle | player | state | coords | on)."""
    out = ['fog %d mask %d boundary %d' % tuple(1 if f else 0 for f in N.fog)]
    for m in N.fogmods:
        co = [m.a, m.b, m.c] + ([m.d] if m.kind == 'R' else [])
        out.append('%s|%d|%d|%s|%d' % (m.kind, m.pid, m.state, '|'.join(fmt_real(v) for v in co), 1 if m.on else 0))
    g = N.fogg
    if g is not None:                    # FOG: the grid (size, rebuilds), the counts of slots 0..3 and the layer sums
        out.append('grid %d %d %d' % (g.w, g.h, g.rebuilds))
        from . import fog as FG
        for s in range(4):
            out.append('slot %d %d %d %d' % ((s,) + FG.fog_counts(N, s)))
        out.append('sum %d %d' % (sum(g.mask), sum(g.vis)))
    return out


@nat('PauseGame')
def _pause(N, b):
    N.vm.paused = bool(b)


@nat('EndGame', 'RestartGame', 'ChangeLevel', 'RemoveSaveDirectory',
     'SetFloatGameState_')
def _endgame(N, *a):
    N.text('endgame', a)


@nat('SetCameraBounds')
def _setcambounds(N, x1, y1, x2, y2, x3, y3, x4, y4):
    N.camera_bounds = (min(x1, x2, x3, x4), min(y1, y2, y3, y4), max(x1, x2, x3, x4), max(y1, y2, y3, y4))


@nat('GetCameraBoundMinX')
def _cbminx(N):
    return N.camera_bounds[0]


@nat('GetCameraBoundMinY')
def _cbminy(N):
    return N.camera_bounds[1]


@nat('GetCameraBoundMaxX')
def _cbmaxx(N):
    return N.camera_bounds[2]


@nat('GetCameraBoundMaxY')
def _cbmaxy(N):
    return N.camera_bounds[3]


@nat('GetCameraMargin')
def _cammargin(N, m):
    # left / right / top / bottom (Open: values are engine constants, taken as 512/512/256/256)
    return {0: 512.0, 1: 512.0, 2: 256.0, 3: 256.0}.get(m, 0.0)


@nat('GetCameraTargetPositionX', 'GetCameraTargetPositionY', 'GetCameraEyePositionX',
     'GetCameraEyePositionY', 'GetCameraTargetPositionZ', 'GetCameraEyePositionZ', 'GetCameraField')
def _camzero(N, *a):
    return 0.0


@nat('GetCameraTargetPositionLoc', 'GetCameraEyePositionLoc')
def _camloc(N):
    return Location(0.0, 0.0)


# ---- players / forces

@nat('Player')
def _player(N, i):
    j = jass_to_slot(N, i)
    return N.player(j) if j is not None else None


@nat('GetLocalPlayer')
def _localplayer(N):
    return N.players[0]


@nat('GetPlayerId')
def _getpid(N, p):
    pl = N.o(p, Player)
    # BUGS LTW-ANTIWALL [obs oracle rmget1 / rmget2]: GetPlayerId(GetOwningPlayer(removed unit)) reads 0 (a null player
    # answers 0, not -1): LTW7's anti-wall trigger reads the owner of a tower the Security trigger removed
    return slot_to_jass(N, pl.id) if pl else 0


@nat('GetPlayerName')
def _getpname(N, p):
    pl = N.o(p, Player)
    return pl.name if pl else None


@nat('SetPlayerName')
def _setpname(N, p, s):
    pl = N.o(p, Player)
    if pl:
        pl.name = s


@nat('GetPlayerController')
def _getctrl(N, p):
    pl = N.o(p, Player)
    return pl.controller if pl else 5           # Unclassed_Native_0007 §4: the null answer is 5 (MAP_CONTROL_NONE)


@nat('SetPlayerController')
def _setctrl(N, p, c):
    pl = N.o(p, Player)
    if pl:
        pl.controller = c


@nat('GetPlayerSlotState')
def _slotstate(N, p):
    pl = N.o(p, Player)
    return pl.slot_state if pl else 0


@nat('GetPlayerRace')
def _getrace(N, p):
    pl = N.o(p, Player)
    return {1: 1, 2: 2, 8: 3, 4: 4}.get(pl.race_pref if pl else 0, 1)


@nat('SetPlayerRacePreference')
def _setracepref(N, p, r):
    pl = N.o(p, Player)
    if pl:
        pl.race_pref = r


@nat('IsPlayerRacePrefSet')
def _racepref(N, p, r):
    pl = N.o(p, Player)
    return bool(pl and pl.race_pref & r)


@nat('SetPlayerRaceSelectable')
def _setracesel(N, p, b):
    pl = N.o(p, Player)
    if pl:
        pl.race_selectable = bool(b)


@nat('GetPlayerSelectable')
def _getracesel(N, p):
    pl = N.o(p, Player)
    return bool(pl and pl.race_selectable)


@nat('SetPlayerTeam')
def _setteam(N, p, t):
    pl = N.o(p, Player)
    if pl:
        pl.team = t


@nat('GetPlayerTeam')
def _getteam(N, p):
    pl = N.o(p, Player)
    return pl.team if pl else 0


@nat('SetPlayerColor')
def _setcolor(N, p, c):
    pl = N.o(p, Player)
    if pl:
        pl.color = c


@nat('GetPlayerColor')
def _getcolor(N, p):
    pl = N.o(p, Player)
    return pl.color if pl else 0


@nat('SetPlayerStartLocation', 'ForcePlayerStartLocation')
def _setpstart(N, p, i):
    pl = N.o(p, Player)
    if pl:
        pl.start_loc = i


@nat('GetPlayerStartLocation')
def _getpstart(N, p):
    pl = N.o(p, Player)
    return pl.start_loc if pl else 0


@nat('SetPlayerAlliance')
def _setally(N, a, b, t, v):
    pa = N.o(a, Player)
    if pa:
        pa.alliance[(b, t)] = bool(v)


@nat('GetPlayerAlliance')
def _getally(N, a, b, t):
    pa = N.o(a, Player)
    return bool(pa and pa.alliance.get((b, t), False))


@nat('IsPlayerAlly')
def _isally(N, a, b):
    pa, pb = N.o(a, Player), N.o(b, Player)
    if not pa or not pb:
        return False
    return pa is pb or pa.alliance.get((b, 0), False)     # ALLIANCE_PASSIVE (Open: team rules)


@nat('IsPlayerEnemy')
def _isenemy(N, a, b):
    """Unclassed_Native_0036 §2 [obs 0x6f2d0170]: a bad handle exits false before the mask test, so a null player is
    never an enemy; otherwise the negation of IsPlayerAlly."""
    pa, pb = N.o(a, Player), N.o(b, Player)
    return bool(pa and pb and not _isally(N, a, b))


@nat('IsPlayerObserver')
def _isobs(N, p):
    return False


@nat('SetPlayerState')
def _setpstate(N, p, s, v):
    pl = N.o(p, Player)
    if pl:
        pl.frac.pop(s, None)                  # UPKEEP: a raw write replaces the stored tenths
        N.set_player_state(pl, s, v)
        if s in (4, 5):
            N.eng.food_retry(pl)              # a train queue waiting for food retries (NOTES_production §1.4)


@nat('GetPlayerState')
def _getpstate(N, p, s):
    pl = N.o(p, Player)
    return pl.state.get(s, 0) if pl else 0


@nat('GetPlayerScore')
def _getscore(N, p, s):
    """Unclassed_Native_0037 §2 [obs]: an index of 25 or more (also negative, the compare is unsigned) or a non-player answers 0;
    index 10 is the sum of the getters of 11 and 12. Only the kill counters have a source in the port (see _score_kill); the
    rest read 0."""
    pl = N.o(p, Player)
    if pl is None or s < 0 or s >= 25:
        return 0
    if s == 10:
        return pl.score[11] + pl.score[12]
    return pl.score[s]


def _tech_raise(N, pl, t, n):
    """NOTES_production §3: single levels while the level can rise (maxlevel of a known research; others uncapped,
    A-PR6 [inf])."""
    r = N.eng.upgrades.get(t)
    raised = False
    for _ in range(max(0, n)):
        cur = pl.tech.get(t, 0)
        if r is not None and cur >= int(r.get('maxlevel', 0)):
            break
        pl.tech[t] = cur + 1
        N.eng.ue.apply_rtma(pl, t, cur, cur + 1)           # PORT8-Upgrade rtma: the level transition's tech-max step
        raised = True
    if raised:
        N.eng.ue.sync_player(pl)                           # BUGS FF-UPG: the new levels reach the player's units


@nat('SetPlayerTechResearched')
def _settech(N, p, t, lvl):
    pl = N.o(p, Player)
    if pl:
        _tech_raise(N, pl, t, lvl - pl.tech.get(t, 0))     # never lowers [obs]
        N.eng.tech_raised(pl, t)                           # BUGS ETD-ORA1: a running research up to that level ends now


@nat('GetPlayerTechResearched')
def _gettech(N, p, t, spec):
    pl = N.o(p, Player)
    return bool(pl and pl.tech.get(t, 0) > 0)


@nat('GetPlayerTechCount')
def _gettechc(N, p, t, spec):
    pl = N.o(p, Player)
    if not pl:
        return 0
    if t not in N.eng.upgrades and t in N.unit_types:
        return N.eng.tech_count(pl, t, bool(spec))      # BUGS ETD-ORA7: unit types count the player's units
    return pl.tech.get(t, 0)


@nat('AddPlayerTechResearched')
def _addtech(N, p, t, lvl):
    pl = N.o(p, Player)
    if pl:
        _tech_raise(N, pl, t, lvl)
        N.eng.tech_raised(pl, t)                           # [inf: the same path as Set]


@nat('SetPlayerTechMaxAllowed')
def _settechmax(N, p, t, m):
    pl = N.o(p, Player)
    if pl:
        pl.tech_max[t] = m


@nat('GetPlayerTechMaxAllowed')
def _gettechmax(N, p, t):
    """BUGS ETD-ORA2 [obs oracle td_techmax_research]: an upgrade reads back at most its data levels (unset / -1 -> the
    levels, 9 -> 3); a unit type with no maximum reads 2147483647 [obs; a negative set value the same, inf]."""
    pl = N.o(p, Player)
    if not pl:
        return 0
    m = pl.tech_max.get(t, -1)
    r = N.eng.upgrades.get(t)
    if r is not None:
        lv = int(r.get('maxlevel', 0))
        return lv if m < 0 or m > lv else m
    return 2147483647 if m < 0 else m


@nat('SetPlayerOnScoreScreen')
def _onscore(N, p, b):
    # [obs 0x6f2d0df0]: a per-player flag (player +0x31c); read only by the end-game score screen (rows of the
    # players with the flag set, 0x6f24efd0); no getter native -> no effect on the game (our port has no score screen)
    pl = N.o(p, Player)
    if pl:
        pl.on_score = bool(b)


@nat('SetUnitCreepGuard')
def _setguard(N, u, b):
    # [obs 0x6f2c2b80: sets/clears bit 2 of unit +0x60, non-zero = true]. A newly armed guard starts at the
    # unit's current point; clearing it removes the point so the unit is never sent back.
    uo = N.o(u, Unit)
    if uo:
        uo.guard = bool(b)
        if uo.guard:
            uo.guard_x, uo.guard_y = uo.x, uo.y
            N.eng.guard_arm(uo)
        else:
            uo.guard_x = uo.guard_y = None
        uo.guard_away = False
        uo.guard_due = -1


@nat('RemoveGuardPosition')
def _rmguard(N, u):
    # [obs 0x6f2d6550 -> 0x6f26fd40]: takes the unit out of its owner's AI guard list (owner player +0x2d4); a unit
    # without a guard position is never sent back to it by the AI idle handler (0x6f26fa30 -> 0x6f26d320).
    # Our engine has no AI guard return (engine_w5 A-GP1) -> state only
    uo = N.o(u, Unit)
    if uo:
        uo.guard = False
        uo.guard_x = uo.guard_y = None
        uo.guard_away = False
        uo.guard_due = -1


@nat('RemoveAllGuardPositions')
def _rmallguard(N, p):
    # [obs 0x6f2d6630 -> 0x6f26fd00 -> 0x6f26d880]: empties the player's AI guard list (every unit's guard link
    # cleared); units created later get a new one again (0x6f26fbe0 at creation) — A-GP1
    pl = N.o(p, Player)
    if pl:
        for uo in N.units:
            if uo.alive and uo.owner is pl:
                uo.guard = False
                uo.guard_x = uo.guard_y = None
                uo.guard_away = False
                uo.guard_due = -1


@nat('SetPlayerHandicap')
def _sethc(N, p, v):
    """Unclassed_Native_0037 §8 [obs 0x6f2d2790]: |v| < 0.001 is ignored completely; the stored value is clamped to
    [lower, 100]. The port's units read the player record, so the raw unit push has no separate copy."""
    pl = N.o(p, Player)
    if pl and abs(r32(float(v))) >= 0.001:
        pl.handicap = min(100.0, max(0.0, r32(float(v))))


@nat('GetPlayerHandicap')
def _gethc(N, p):
    pl = N.o(p, Player)
    return pl.handicap if pl else 1.0


@nat('SetPlayerHandicapXP')
def _sethcx(N, p, v):
    """Unclassed_Native_0038 §1 [obs 0x6f2d2990]: clamped to [0, 100] (100 = normal XP gain), no epsilon test."""
    pl = N.o(p, Player)
    if pl:
        pl.handicap_xp = min(100.0, max(0.0, r32(float(v))))


@nat('GetPlayerHandicapXP')
def _gethcx(N, p):
    pl = N.o(p, Player)
    return pl.handicap_xp if pl else 1.0


@nat('RemovePlayer')
def _removeplayer(N, p, r):
    pl = N.o(p, Player)
    if pl:
        pl.slot_state = 2


@nat('CachePlayerHeroData', 'SetPlayerUnitsOwner')
def _pnoop(N, *a):
    return None


@nat('GetPlayerUnitCount')
def _punitcount(N, p, incomplete):
    """Unclassed_Native_0036 §8 [obs 0x6f2d0ec0]: live units of the player; includeIncomplete also counts the
    structures still under construction (the engine's 0x16 filter vs 0x10)."""
    pl = N.o(p, Player)
    if not pl:
        return 0
    return sum(1 for u in N.units
               if u.owner is pl and u.alive and not u.dead and not u.consumed and (incomplete or not u.constructing))


@nat('GetPlayerTypedUnitCount')
def _ptypedcount(N, p, name, incomplete, upgraded):
    """Unclassed_Native_0036 §8 [obs 0x6f2d0fb0]: the player's living units of one type; includeIncomplete also
    counts the ones under construction, includeUpgrades the types this type upgrades to (engine.tech_count)."""
    pl = N.o(p, Player)
    if not pl:
        return 0
    tid = _unitid(N, name)
    if not tid:
        return 0
    return N.eng.tech_count(pl, tid, not upgraded, bool(incomplete))


@nat('GetPlayerStructureCount')
def _pstructcount(N, p, incomplete):
    """Unclassed_Native_0037 §1 [obs 0x6f2d10e0]: finished buildings, plus the ones under construction when
    includeIncomplete is true (the +0x54 / +0x58 pair)."""
    pl = N.o(p, Player)
    if not pl:
        return 0
    return sum(1 for u in N.units
               if u.owner is pl and u.alive and not u.dead and u.data.get('bldg')
               and (incomplete or not u.constructing))


@nat('CreateForce')
def _createforce(N):
    return Force()


@nat('DestroyForce')
def _destroyforce(N, f):
    fo = N.o(f, Force)
    if fo:
        N.vm.H.destroyed(fo)


@nat('ForceAddPlayer')
def _forceadd(N, f, p):
    fo, pl = N.o(f, Force), N.o(p, Player)
    if fo and pl:
        fo.players.add(pl.id)


@nat('ForceRemovePlayer')
def _forcerem(N, f, p):
    fo, pl = N.o(f, Force), N.o(p, Player)
    if fo and pl:
        fo.players.discard(pl.id)


@nat('ForceClear')
def _forceclear(N, f):
    fo = N.o(f, Force)
    if fo:
        fo.players.clear()


@nat('IsPlayerInForce')
def _inforce(N, p, f):
    fo, pl = N.o(f, Force), N.o(p, Player)
    return bool(fo and pl and pl.id in fo.players)


def _force_enum(N, fo, cands, filt, limit=None):
    vm = N.vm
    fo.players.clear()
    n = 0
    for pl in cands:
        if limit is not None and n >= limit:
            break
        if filt is None or vm.eval_filter(filt, V.CTX_FILTER_PLAYER, N.H(pl)):
            fo.players.add(pl.id)
            n += 1


@nat('ForceEnumPlayers')
def _forceenum(N, f, filt):
    fo = N.o(f, Force)
    if fo:
        # BUGS FF-ALL [obs 1.21 0x6f2e9350]: the loop covers the 12 player slots and takes those whose slot state is
        # not empty (+0x270 = GetPlayerSlotState); the neutral four (our 12..15) are never in the force. Footman
        # Frenzy relies on it: empty slots are not "players in the game", the last team standing wins
        _force_enum(N, fo, [pl for pl in N.players[:12] if pl.slot_state != 0], _bx(N, filt))


@nat('ForceEnumPlayersCounted')
def _forceenumc(N, f, filt, n):
    fo = N.o(f, Force)
    if fo:
        _force_enum(N, fo, [pl for pl in N.players[:12] if pl.slot_state != 0], _bx(N, filt), n)


@nat('ForceEnumAllies')
def _forceenumallies(N, f, p, filt):
    fo = N.o(f, Force)
    if fo:
        _force_enum(N, fo, [pl for pl in N.players if _isally(N, p, N.H(pl))], _bx(N, filt))


@nat('ForceEnumEnemies')
def _forceenumenemies(N, f, p, filt):
    fo = N.o(f, Force)
    if fo:
        _force_enum(N, fo, [pl for pl in N.players if not _isally(N, p, N.H(pl))], _bx(N, filt))


@nat('ForForce')
def _forforce(N, f, code):
    """T§5: ForForce like ForGroup (slot 11), players in id order (Open), each callback own thread."""
    fo = N.o(f, Force)
    if not fo or not code:
        return
    for pid in sorted(fo.players):
        N.vm.run_callback(code, 'enum', V.CTX_ENUM_PLAYER, N.H(N.players[pid]))


# ---- locations / rects / regions

@nat('Location')
def _location(N, x, y):
    return Location(x, y)


@nat('RemoveLocation', 'RemoveRect', 'RemoveRegion', 'DestroyTextTag', 'DestroyQuest',
     'DestroyFogModifier', 'DestroyLightning', 'DestroyImage', 'DestroyUbersplat', 'DestroyTimerDialog',
     'DestroyMultiboard', 'DialogDestroy', 'DestroyUnitPool', 'DestroyItemPool', 'DestroyDefeatCondition',
     'DestroyLeaderboard', 'RemoveWeatherEffect', 'KillSoundWhenDone')
def _destroy(N, h):
    o = N.o(h)
    if isinstance(o, TextTag) and o in N.texttags:
        N.texttags.remove(o)
    if isinstance(o, Simple) and o.tname == 'multiboard' and o in N.multiboards:
        N.multiboards.remove(o)
    if isinstance(o, Quest) and o in N.quests:
        N.quests.remove(o)
    if isinstance(o, Simple) and o.tname == 'weathereffect' and o in N.weather:
        N.weather.remove(o)
    if getattr(o, 'tname', None) == 'fogmodifier' and o in N.fogmods:
        N.fogmods.remove(o)
    if isinstance(o, Simple) and o.tname == 'image' and o in N.images:
        N.images.remove(o)
    if isinstance(o, Effect) and o.dead < 0:
        o.dead = N.vm.game_ms()                  # its death animation plays (presentation)
    if o is not None and not isinstance(o, (Player, Unit, Trigger, Timer)):
        N.vm.H.destroyed(o)          # a removed region drops out of the region index


@nat('DestroyEffect')
def _destroyeffect(N, h):
    """0048 §4.1 [obs 0x6f2dea10]: the object must resolve and carry the effect class; a null, wrong-type or destroyed
    handle is a silent no-op (the generic Destroy* handler would also release a rect or location handed to it)."""
    o = N.o(h)
    if isinstance(o, Effect):
        if o.dead < 0:
            o.dead = N.vm.game_ms()              # its death animation plays (presentation)
        N.vm.H.destroyed(o)


@nat('MoveLocation')
def _moveloc(N, l, x, y):
    lo = N.o(l, Location)
    if lo:
        lo.x, lo.y = x, y


@nat('GetLocationX')
def _locx(N, l):
    lo = N.o(l, Location)
    return lo.x if lo else 0.0


@nat('GetLocationY')
def _locy(N, l):
    lo = N.o(l, Location)
    return lo.y if lo else 0.0


@nat('GetLocationZ')
def _locz(N, l):
    return 0.0


@nat('Rect')
def _rect(N, a, b, c, d):
    return Rect(a, b, c, d)


@nat('RectFromLoc')
def _rectloc(N, l1, l2):
    a, b = N.o(l1, Location), N.o(l2, Location)
    if not a or not b:
        return None                            # Unclassed_Native_0010 §RectFromLoc: null handle
    return Rect(a.x, a.y, b.x, b.y)


@nat('SetRect')
def _setrect(N, r, a, b, c, d):
    ro = N.o(r, Rect)
    if ro:
        ro.set_bounds(a, b, c, d)


@nat('SetRectFromLoc')
def _setrectloc(N, r, l1, l2):
    ro, a, b = N.o(r, Rect), N.o(l1, Location), N.o(l2, Location)
    if ro and a and b:
        ro.set_bounds(a.x, a.y, b.x, b.y)


def _move_rect(ro, x, y):
    hw, hh = (ro.maxx - ro.minx) / 2, (ro.maxy - ro.miny) / 2
    ro.minx, ro.maxx = r32(x - hw), r32(x + hw)
    ro.miny, ro.maxy = r32(y - hh), r32(y + hh)


@nat('MoveRectTo')
def _moverect(N, r, x, y):
    ro = N.o(r, Rect)
    if ro:
        _move_rect(ro, x, y)


@nat('MoveRectToLoc')
def _moverectloc(N, r, l):
    ro, lo = N.o(r, Rect), N.o(l, Location)
    if ro and lo:
        _move_rect(ro, lo.x, lo.y)


@nat('GetRectCenterX')
def _rcx(N, r):
    ro = N.o(r, Rect)
    return (ro.minx + ro.maxx) / 2 if ro else 0.0


@nat('GetRectCenterY')
def _rcy(N, r):
    ro = N.o(r, Rect)
    return (ro.miny + ro.maxy) / 2 if ro else 0.0


@nat('GetRectMinX')
def _rminx(N, r):
    ro = N.o(r, Rect)
    return ro.minx if ro else 0.0


@nat('GetRectMinY')
def _rminy(N, r):
    ro = N.o(r, Rect)
    return ro.miny if ro else 0.0


@nat('GetRectMaxX')
def _rmaxx(N, r):
    ro = N.o(r, Rect)
    return ro.maxx if ro else 0.0


@nat('GetRectMaxY')
def _rmaxy(N, r):
    ro = N.o(r, Rect)
    return ro.maxy if ro else 0.0


@nat('GetWorldBounds')
def _worldbounds(N):
    a, b, c, d = N.camera_bounds
    return Rect(a - 512.0, b - 256.0, c + 512.0, d + 256.0)     # Open: real map extent


@nat('CreateRegion')
def _createregion(N):
    rg = Region()
    N.regions.append(rg)
    return rg


@nat('RegionAddRect')
def _regaddrect(N, rg, r):
    rgo, ro = N.o(rg, Region), N.o(r, Rect)
    if rgo and ro:
        N.region_rect(rgo, ro, True)


@nat('RegionClearRect')
def _regclrrect(N, rg, r):
    rgo, ro = N.o(rg, Region), N.o(r, Rect)
    if rgo and ro:
        N.region_rect(rgo, ro, False)


@nat('RegionAddCell')
def _regaddcell(N, rg, x, y):
    rgo = N.o(rg, Region)
    if rgo:
        N.region_cell(rgo, N.cell_of(x, y), True)


@nat('RegionAddCellAtLoc')
def _regaddcellloc(N, rg, l):
    rgo, lo = N.o(rg, Region), N.o(l, Location)
    if rgo and lo:
        N.region_cell(rgo, N.cell_of(lo.x, lo.y), True)


@nat('RegionClearCell')
def _regclrcell(N, rg, x, y):
    rgo = N.o(rg, Region)
    if rgo:
        N.region_cell(rgo, N.cell_of(x, y), False)


@nat('IsUnitInRegion')
def _unitinreg(N, rg, u):
    rgo, uo = N.o(rg, Region), N.o(u, Unit)
    return bool(rgo and uo and N.region_has(rgo, N.cell_of(uo.x, uo.y)))


@nat('IsPointInRegion')
def _ptinreg(N, rg, x, y):
    rgo = N.o(rg, Region)
    return bool(rgo and N.region_has(rgo, N.cell_of(x, y)))


@nat('IsLocationInRegion')
def _locinreg(N, rg, l):
    rgo, lo = N.o(rg, Region), N.o(l, Location)
    return bool(rgo and lo and N.region_has(rgo, N.cell_of(lo.x, lo.y)))


@nat('GetTriggeringRegion')
def _trigregion(N):
    return N.vm.ev('region')


# ---- units

@nat('CreateUnit')
def _createunit(N, p, tid, x, y, face):
    pl = N.o(p, Player)
    if not pl:
        return None
    return N.create_unit(pl, tid, x, y, face)


@nat('CreateUnitByName')
def _createunitbyname(N, p, name, x, y, face):
    return _createunit(N, p, _unitid(N, name), x, y, face)


@nat('CreateUnitAtLoc')
def _createunitloc(N, p, tid, l, face):
    lo = N.o(l, Location)
    if not lo:
        return None
    return _createunit(N, p, tid, lo.x, lo.y, face)


@nat('CreateUnitAtLocByName')
def _createunitlocname(N, p, name, l, face):
    return _createunitloc(N, p, _unitid(N, name), l, face)


@nat('CreateCorpse')
def _createcorpse(N, p, tid, x, y, face):
    pl = N.o(p, Player)
    if not pl:
        return None
    # a corpse never uses / makes food (the corpse flag of the constructor); the wrapper otherwise creates like CreateUnit
    u = N.create_unit(pl, tid, x, y, face, food=False, made=False)
    if u:
        u.dead = True
        N.eng.drop_lists(u)
        u.set_life_rate(0.0)
        u.life = 0.0
    return u


@nat('RemoveUnit')
def _removeunit(N, u):
    uo = N.o(u, Unit)
    if uo is not None:
        uo.removed_step = N.vm.steps                # BUGS RM-READ: its getters still answer in this step
    N.remove_unit(uo)


@nat('KillUnit')
def _killunit(N, u):
    N.kill_unit(N.o(u, Unit))


@nat('GetUnitX')
def _unitx(N, u):
    uo = N.o_read(u, Unit)
    return uo.x if uo else 0.0


@nat('GetUnitY')
def _unity(N, u):
    uo = N.o_read(u, Unit)
    return uo.y if uo else 0.0


@nat('GetUnitLoc')
def _unitloc(N, u):
    """CUnit_p2 0x6f2c2ed0: null handle / removed unit -> null (not a (0,0) location)."""
    uo = N.o(u, Unit)
    return Location(uo.x, uo.y) if uo else None


@nat('GetUnitRallyPoint')
def _rallypoint(N, u):
    """a NEW location each call [obs 0x6f2c2fe0]; null without the rally ability, for the default rally (the building
    itself) and for a dead / removed target; a unit target gives its current position (the rally follows it)."""
    uo = N.o(u, Unit)
    if uo is None or not N.eng.has_rally(uo):
        return None
    r = getattr(uo, 'rally', None)
    if not r:
        return None
    if r[0] == 'unit':
        t = r[1]
        if t is uo or not getattr(t, 'alive', False):
            return None
        return Location(t.x, t.y)
    if r[0] == 'dest':
        t = r[1]
        if not rally_dest_alive(t):
            return None
        return Location(t.x, t.y)
    return Location(r[1], r[2])


@nat('GetUnitRallyUnit')
def _rallyunit(N, u):
    uo = N.o(u, Unit)
    if uo is None or not N.eng.has_rally(uo):
        return None
    r = getattr(uo, 'rally', None)
    if not r:
        return uo                                  # the default rally is the building itself [obs]
    return r[1] if r[0] == 'unit' else None


@nat('GetUnitRallyDestructable')
def _rallydestr(N, u):
    uo = N.o(u, Unit)
    if uo is None or not N.eng.has_rally(uo):
        return None
    r = getattr(uo, 'rally', None)
    return r[1] if r and r[0] == 'dest' and rally_dest_alive(r[1]) else None


@nat('SetDestructableInvulnerable')
def _destrinvul(N, d, flag):
    o = N.o(d)
    if o is not None:
        o.invul = bool(flag)                       # widget flag bit 0x8: damage 0 [obs 0x6f2bf960]


@nat('IsDestructableInvulnerable')
def _isdestrinvul(N, d):
    o = N.o(d)
    return bool(getattr(o, 'invul', False)) if o is not None else False


@nat('GetUnitFacing')
def _unitface(N, u):
    uo = N.o(u, Unit)
    return uo.facing if uo else 0.0


@nat('SetUnitFacing', 'SetUnitFacingTimed')
def _setunitface(N, u, f, *t):
    uo = N.o(u, Unit)
    if uo:
        uo.facing = f


def _set_pos(N, uo, x, y):
    uo.x, uo.y = r32(x), r32(y)
    uo.path = None                   # movers repath from the new position
    N.eng.relocate(uo)
    N.dirty.add(uo)
    if uo.footprint:                 # CWidget_p2 §5.13: a moved widget re-registers its footprint (slots 0x150 /
        N.eng.remove_footprint(uo)   # 0x154), so a building / rooted Ancient blocks its new cells and frees the old
        N.eng.add_footprint(uo)


def _set_pos_native(N, uo, x, y):
    """SetUnitX / SetUnitY / SetUnitPosition: BUGS ETD-IDX [obs runtime oracle enter1] — the region filters run inside
    the native, the actions at the next step."""
    _set_pos(N, uo, x, y)
    N.update_regions(uo, sync=True)


@nat('SetUnitX')
def _setunitx(N, u, x):
    uo = N.o(u, Unit)
    if uo:
        _set_pos_native(N, uo, x, uo.y)


@nat('SetUnitY')
def _setunity(N, u, y):
    uo = N.o(u, Unit)
    if uo:
        _set_pos_native(N, uo, uo.x, y)


@nat('SetUnitPosition')
def _setunitpos(N, u, x, y):
    uo = N.o(u, Unit)
    if uo:
        if not uo.dead:
            N.eng.clear_order(uo)    # NOTES_units §1: SetUnitPosition issues stop first (A23: no order event)
        _set_pos_native(N, uo, x, y)
        if N.eng.sp.pend_end:
            N.eng.sp.flush()         # W8: an interrupted cast's ENDCAST (spells.py A-SP7)


@nat('SetUnitPositionLoc')
def _setunitposloc(N, u, l):
    uo, lo = N.o(u, Unit), N.o(l, Location)
    if uo and lo:
        _setunitpos(N, u, lo.x, lo.y)


# ---- waygates (NOTES_abilities §5.54, A-AB22): the natives act only on a unit with an Awrp ability
#      [obs 0x6f2cf470 / 0x6f2cf570 / 0x6f2cf270 / 0x6f2cf370 / 0x6f2cf670 -> 0x6f458ee0 / 0x6f458f10 / 0x6f458ec0 / 0x6f458f60]
@nat('WaygateActivate')
def _wgactivate(N, u, b):
    uo = N.o(u, Unit)
    if uo and N.eng.wg_is_gate(uo):
        N.eng.wg_set_active(uo, bool(b))


@nat('WaygateIsActive')
def _wgisactive(N, u):
    uo = N.o(u, Unit)
    return bool(uo and N.eng.wg_is_gate(uo) and N.eng.wg_state(uo)[0])


@nat('WaygateSetDestination')
def _wgsetdest(N, u, x, y):
    uo = N.o(u, Unit)
    if uo and N.eng.wg_is_gate(uo):
        N.eng.wg_set_dest(uo, x, y)


@nat('WaygateGetDestinationX')
def _wgdestx(N, u):
    uo = N.o(u, Unit)
    return N.eng.wg_dest(uo)[0] if uo and N.eng.wg_is_gate(uo) else 0.0


@nat('WaygateGetDestinationY')
def _wgdesty(N, u):
    uo = N.o(u, Unit)
    return N.eng.wg_dest(uo)[1] if uo and N.eng.wg_is_gate(uo) else 0.0


@nat('GetUnitTypeId')
def _unittype(N, u):
    uo = N.o_read(u, Unit)
    return uo.tid if uo else 0


# ---- Reforged skin natives (BUGS LTD-BLZ): the skin id is presentation only, game logic is the plain native's.
# Declared by jvm/compat.py when the install's common.j lacks them. [inf: skin ids only select the model]

@nat('BlzCreateUnitWithSkin')
def _createunitskin(N, p, tid, x, y, face, skin):
    return _createunit(N, p, tid, x, y, face)


@nat('BlzCreateDestructableWithSkin')
def _createdestrskin(N, tid, x, y, face, scale, var, skin):
    return _mkdestr(N, tid, x, y, face, False)


@nat('BlzGetUnitSkin')
def _unitskin(N, u):
    return _unittype(N, u)


@nat('BlzSetUnitSkin', 'BlzSetDestructableSkin')
def _setskin(N, o, skin):
    return None


@nat('GetUnitName')
def _unitname(N, u):
    uo = N.o(u, Unit)
    return uo.data.get('name', _id4(uo.tid)) if uo else None


@nat('GetOwningPlayer')
def _owner(N, u):
    uo = N.o_read(u, Unit)
    return uo.owner if uo else None


@nat('SetUnitOwner')
def _setowner(N, u, p, color):
    uo, pl = N.o(u, Unit), N.o(p, Player)
    if uo and pl and uo.owner is not pl:
        old = uo.owner
        N.eng.queue_abort(uo, refund_to=old)              # NOTES_production §1.6: owner change cancels, refund old
        uo.owner = pl
        N.eng.ue.sync(uo)                                 # BUGS FF-UPG: the new owner's researched levels
        N.eng.owner_changed(uo)                           # A2: drop target + cancel swing in progress (NOTES_combat 6.4)
        fu, fm = int(uo.data.get('fused', 0) or 0), int(uo.data.get('fmade', 0) or 0)
        if not uo.dead:
            N.food_add(old, -fu, -fm)
            N.food_add(pl, fu, fm)
        # NAT-4: EVENT_PLAYER_UNIT_CHANGE_OWNER (new owner's registrations) then EVENT_UNIT_CHANGE_OWNER, after the change;
        # GetChangingUnit = the unit, GetChangingUnitPrevOwner = the old owner [ext: common.j event data]
        N.unit_event(uo, 'CHANGE_OWNER', {'changing': N.H(uo), 'prevowner': N.H(old)})


@nat('GetUnitState')
def _unitstate(N, u, s):
    uo = N.o_read(u, Unit)                          # BUGS ETD-ORA9 [obs oracle td_construct]: like GetWidgetLife (RM-READ)
    if not uo:
        return 0.0
    return {0: uo.life, 1: uo.maxlife, 2: uo.mana, 3: uo.maxmana}.get(s, 0.0)


@nat('SetUnitState')
def _setunitstate(N, u, s, v):
    """NOTES_units §5: only LIFE (SetLife: <= 0 -> 0, then the death check, can kill) and MANA are written;
    MAX_LIFE / MAX_MANA are silently ignored in 1.21."""
    uo = N.o(u, Unit)
    if not uo:
        return
    if s == 0:
        if uo.dead:
            return
        uo.life = r32(max(0.0, min(v, uo.maxlife)))
        if uo.life <= UNIT_DEATH_LIFE:              # NOTES_combat §4 (A1); [obs runtime 1.26a] the unit threshold is 0.405, inclusive
            N.kill_unit(uo, None)
    elif s == 2:
        uo.mana = r32(max(0.0, min(v, uo.maxmana)))


@nat('GetWidgetLife')
def _widgetlife(N, w):
    o = N.o_read(w)
    return getattr(o, 'life', 0.0) if o else 0.0


@nat('SetWidgetLife')
def _setwidgetlife(N, w, v):
    o = N.o(w)
    if isinstance(o, Unit):
        _setunitstate(N, w, 0, v)
    elif o is not None and hasattr(o, 'dalive'):
        _destr_life(N, o, v)                       # the same +0x124 setter as SetDestructableLife (CDestructable §10)
    elif isinstance(o, Item):
        N.eng.itm.set_life(o, v)                   # CItem §8 / NOTES_items §3.2: clamp at 0 + the 0.4 death path
    elif o is not None and hasattr(o, 'life'):
        o.life = v


@nat('GetUnitMoveSpeed')
def _movespeed(N, u):
    uo = N.o(u, Unit)
    return N.eng.eff_speed(uo, True) if uo else 0.0      # NOTES_units §2: clamped, 0 without a move ability


@nat('GetUnitDefaultMoveSpeed')
def _defmovespeed(N, u):
    uo = N.o(u, Unit)
    return uo.default_speed if uo else 0.0


@nat('GetUnitTurnSpeed')
@nat('GetUnitDefaultTurnSpeed')
def _turnspeed(N, u):
    uo = N.o(u, Unit)                               # the type's turnRate; SetUnitTurnSpeed is not modelled (A-UN1)
    return r32(float(uo.data.get('turnRate', 0) or 0)) if uo else 0.0


@nat('GetUnitDefaultPropWindow')
def _defpropwin(N, u):
    uo = N.o(u, Unit)                               # the type's propWin as stored (degrees) [obs 0x6f2c48e0, inf]
    return r32(float(uo.data.get('propWin', 0) or 0)) if uo else 0.0


@nat('SetUnitMoveSpeed')
def _setmovespeed(N, u, v):
    uo = N.o(u, Unit)
    if uo and uo.base_speed > 0:                    # NOTES_units §2: no effect without a move ability
        uo.speed = r32(v)
        if uo.order in MOVE_ORDERS and N.eng.eff_speed(uo, True) > 0:
            N.moving.add(uo)


@nat('GetUnitUserData')
def _userdata(N, u):
    uo = N.o_read(u, Unit)
    return uo.userdata if uo else 0


@nat('SetUnitUserData')
def _setuserdata(N, u, v):
    uo = N.o(u, Unit)
    if uo:
        uo.userdata = v


# NOTES_unittype (BUGS W8-9): the UNIT_TYPE constants. Results are normalised to true / false (the game returns
# the raw bit; only a trigger condition that returns it directly differs — A-UT1)
UT_CLASS = {9: 0x1, 14: 0x2, 10: 0x4, 15: 0x8, 16: 0x10, 17: 0x20, 18: 0x40, 19: 0x80, 20: 0x800}
_UT_NOATTACK = {0, UD.T_TREE, UD.T_DEBRIS, UD.T_DECOR}


def _ut_weapons(d, melee):
    """a usable weapon of type data d: normal (melee) / missile … aline (ranged), not a tree / debris weapon."""
    for i in (0, 1):
        if not (int(d.get('weapsOn', 0) or 0) >> i) & 1:
            continue
        wt = int(d.get('weapTp%d' % (i + 1), 0) or 0)
        if wt == 0 or int(d.get('targs%d' % (i + 1), 0) or 0) in _UT_NOATTACK:
            continue
        if (wt == 1) == melee:
            return True
    return False


def _ut_hero_id(tid):
    return 0x41 <= ((tid >> 24) & 0xff) <= 0x5a                # first letter A–Z


@nat('IsUnitType')
def _isunittype(N, u, t):
    uo = N.o_read(u, Unit)
    if not uo:
        return False
    d = uo.data
    mt = int(d.get('movetp', 0) or 0)
    if t == 0:
        return _ut_hero_id(uo.tid) and not uo.illusion
    if t == 1:
        return uo.dead
    if t == 2:
        return bool(d.get('bldg'))
    if t == 3:
        return mt == UD.MOVE_TYPES['fly']
    if t == 4:
        return mt in (UD.MOVE_TYPES['foot'], UD.MOVE_TYPES['horse'], UD.MOVE_TYPES['hover'])
    if t in (5, 6):
        return False                                # broken in the game: tests a 'none' target mask [obs]
    if t in (7, 8):
        return _ut_weapons(d, t == 7)
    if t in UT_CLASS:
        return bool(uo.ucls & UT_CLASS[t])
    kinds = set(b.kind for b in uo.buffs)
    if t == 11:
        return 'stun' in kinds
    if t == 13:
        return 'roots' in kinds
    if t == 21:
        return 'poison' in kinds
    if t == 12:
        return any(b.code == _PLAGUE_BUFF for b in uo.buffs)
    if t == 23:
        return 'sleep' in kinds
    if t == 24:
        return any(code == 'Arsk' for _a, code, lv in N.eng.ab.abil_list(uo) if lv is not None)
    if t == 25:
        return uo.ethereal > 0
    if t == 26:
        return uo.magic_immune > 0
    return False                                    # 22 polymorphed (no polymorph spell) / unknown constant


_PLAGUE_BUFF = (ord('B') << 24) | (ord('a') << 16) | (ord('p') << 8) | ord('l')


@nat('IsUnitIdType')
def _isunitidtype(N, tid, t):
    if t == 0:
        return _ut_hero_id(tid)                     # the letter only, even for an unknown id [obs]
    d = N.unit_types.get(tid)
    if d is None:
        return False
    if t == 2:
        return bool(d.get('bldg'))
    if t == 3:
        return int(d.get('movetp', 0) or 0) == UD.MOVE_TYPES['fly']
    if t in (7, 8):
        return _ut_weapons(d, t == 7)
    if t in UT_CLASS:
        return bool(int(d.get('ucls', 0) or 0) & UT_CLASS[t])
    return False                                    # ground (never matches [obs]), dead, states: type data only


@nat('UnitAddType')
def _unitaddtype(N, u, t):
    uo = N.o(u, Unit)
    if uo is None or t not in UT_CLASS:
        return False                                # only the class constants [obs]
    uo.ucls |= UT_CLASS[t]
    return True


@nat('UnitRemoveType')
def _unitremovetype(N, u, t):
    uo = N.o(u, Unit)
    if uo is None or t not in UT_CLASS:
        return False
    uo.ucls &= ~UT_CLASS[t]
    return True


@nat('IsUnitHidden')
def _ishidden(N, u):
    uo = N.o(u, Unit)
    return bool(uo and uo.hidden)


@nat('IsUnitInTransport')
def _unitintransport(N, u, t):
    """Unclassed_Native_0030 §10 [obs 0x6f2c9af0]: the passenger currently loaded in the transport. The port's only
    cargo/transport object is the gold mine a worker stands in (harvest's M_IN_MINE state)."""
    uo, to = N.o(u, Unit), N.o(t, Unit)
    if uo is None or to is None:
        return False
    from .harvest import M_IN_MINE
    s = N.eng.hv.st.get(uo)
    return bool(s is not None and s.get('mode') == M_IN_MINE and s.get('mine') is to)


@nat('IsUnitLoaded')
def _unitloaded(N, u):
    """Unclassed_Native_0031 §1 [obs 0x6f2c9c90]: bit 4 of the unit state word, i.e. loaded inside a transport."""
    uo = N.o(u, Unit)
    if uo is None:
        return False
    from .harvest import M_IN_MINE
    s = N.eng.hv.st.get(uo)
    return bool(s is not None and s.get('mode') == M_IN_MINE)


@nat('ShowUnit')
def _showunit(N, u, show):
    uo = N.o(u, Unit)
    if uo:
        was = uo.hidden
        uo.hidden = not show
        if was and show and N.eng is not None:
            N.eng.wake(uo, N.vm.steps)                    # W9R3-6-HIDDEN-FIGHT: its fight step was dropped while hidden


@nat('SetTerrainPathable')
def _setterrainpathable(N, x, y, t, flag):
    """NOTES_units §2 [obs 0x6f2dfd90]: flag false sets the cell's "not-t" bit (mask 1 << t, ANY 0xff), true clears it;
    movers repath (the grid version changes)."""
    g = N.eng.grid
    if g is None:
        return
    cx, cy = g.cell_of(x, y)
    if not g.inside(cx, cy):
        return
    m = 0xff if t == 0 else (1 << t) & 0xff
    i = cy * g.w + cx
    old = g.flags[i]
    g.flags[i] = (old & ~m) if flag else (old | m)
    if g.flags[i] != old:
        g.version += 1
        if flag:
            g.fld_free = g.version                  # PATH-FIELD: a cell may have become walkable (the fields older than this are not shortest any more)
        g.delta(cx, cy, cx, cy)


@nat('IsTerrainPathable')
def _isterrainpathable(N, x, y, t):
    """NOTES_units §2 [obs 0x6f080470]: inverted — true when the "not-t" bit is set; building footprints count."""
    g = N.eng.grid
    if g is None:
        return False
    cx, cy = g.cell_of(x, y)
    if not g.inside(cx, cy):
        return True
    i = cy * g.w + cx
    m = 0xff if t == 0 else (1 << t) & 0xff
    if g.flags[i] & m:
        return True
    if m & 0x02 and g.occ_walk[i]:
        return True
    return bool(m & 0x08 and (g.occ_walk[i] or g.occ_build[i]))


@nat('IsUnitIllusion')
def _isillusion(N, u):
    uo = N.o_read(u, Unit)
    return bool(uo and uo.illusion)


@nat('IsUnitInGroup')
def _inunitgroup(N, u, g):
    uo, go = N.o(u, Unit), N.o(g, Group)
    return bool(uo and go and uo in go.units)


@nat('IsUnitInForce')
def _unitinforce(N, u, f):
    uo, fo = N.o(u, Unit), N.o(f, Force)
    return bool(uo and fo and uo.owner.id in fo.players)


@nat('IsUnitOwnedByPlayer')
def _ownedby(N, u, p):
    uo, pl = N.o(u, Unit), N.o(p, Player)
    return bool(uo and pl and uo.owner is pl)


@nat('IsUnitAlly')
def _unitally(N, u, p):
    uo = N.o(u, Unit)
    return bool(uo and _isally(N, N.H(uo.owner), p))


@nat('IsUnitEnemy')
def _unitenemy(N, u, p):
    """Unclassed_Native_0030 §8 [obs 0x6f2c97a0]: the unconditional negation of IsUnitAlly, so a null/removed unit
    or an invalid player answers true."""
    uo = N.o(u, Unit)
    return not (uo and _isally(N, N.H(uo.owner), p))


@nat('IsUnit')
def _isunit(N, a, b):
    """Unclassed_Native_0031 §7: the comparison is of the resolved objects, so removed handles collapse to null
    (two different removed units are equal, as is a removed unit and the null handle)."""
    return N.o(a, Unit) is N.o(b, Unit)


@nat('IsUnitInRange')
def _unitinrange(N, a, b, d):
    """Unclassed_Native_0031 §10 [obs 0x6f2cb2d0 / 0x6f3c8090]: both handles must resolve to live units, then
    the shared distance predicate 0x6f08ce80 / 0x6f4766e0 is the engine's in-range test (NOTES_combat §2):
    `dist² <= (range + collA + collB)²` with the 0.001 tolerance, edge to edge, not centre to centre.
    A negative range is clamped at the predicate's stored minimum (0 here) [inf]."""
    ua, ub = N.o(a, Unit), N.o(b, Unit)
    if ua is None or ub is None:
        return False
    r = max(0.0, float(d)) + ua.coll + ub.coll
    dx, dy = ua.x - ub.x, ua.y - ub.y
    return dx * dx + dy * dy <= r * r + 0.001


@nat('IsUnitInRangeXY')
def _unitinrangexy(N, u, x, y, d):
    uo = N.o(u, Unit)
    return bool(uo and math.hypot(uo.x - x, uo.y - y) <= d)


@nat('IsUnitInRangeLoc')
def _unitinrangeloc(N, u, l, d):
    uo, lo = N.o(u, Unit), N.o(l, Location)
    return bool(uo and lo and math.hypot(uo.x - lo.x, uo.y - lo.y) <= d)


@nat('PauseUnit')
def _pauseunit(N, u, b):
    uo = N.o(u, Unit)
    if uo:
        uo.paused = bool(b)
        uo.root_pz = False                       # the script owns the pause now (CAbilityRoot's transition must not clear it)
        if not b:
            N.eng.wake(uo, N.vm.steps)


@nat('IsUnitPaused')
def _ispaused(N, u):
    uo = N.o(u, Unit)
    return bool(uo and uo.paused)


@nat('SetUnitInvulnerable')
def _setinvuln(N, u, b):
    """NOTES_units §5: true adds 'Avul' if absent, false removes it."""
    uo = N.o(u, Unit)
    if uo:
        if b:
            if AVUL not in uo.abilities:
                uo.perm_abil.pop(AVUL, None)
            uo.abilities.setdefault(AVUL, 1)
        else:
            uo.abilities.pop(AVUL, None)
            uo.perm_abil.pop(AVUL, None)
        uo.invulnerable = AVUL in uo.abilities or uo.invuln_n > 0


@nat('SetUnitPathing')
def _setpathing(N, u, b):
    uo = N.o(u, Unit)
    if uo:
        uo.pathing = bool(b)


@nat('SetUnitExploded')
def _setexploded(N, u, flag):
    """Unclassed_Native_0028 §13 / 0029 §5 [obs 0x6f2c7ca0]: bit 0x2000 of the live unit's state word; the port's
    corpse-suppression flag (abilities.incinerate_death reads it), cleared by a false argument."""
    uo = N.o(u, Unit)
    if uo is not None and uo.alive and not uo.dead:
        uo.exploded = bool(flag)


@nat('UnitAddAbility')
def _addabil(N, u, a):
    uo = N.o(u, Unit)
    if not uo or a in uo.abilities:
        return False
    uo.perm_abil.pop(a, None)          # a new ability instance starts with a zero permanence counter
    uo.abilities[a] = 1
    uo.invulnerable = AVUL in uo.abilities or uo.invuln_n > 0
    N.eng.ab.on_add(uo, a)
    return True


@nat('UnitRemoveAbility')
def _remabil(N, u, a):
    uo = N.o(u, Unit)
    if not uo:
        return False
    if a not in uo.abilities:
        return N.eng.ab.remove_buff_code(uo, a)            # buffs are ability objects (NOTES_abilities §2)
    N.eng.ab.before_remove(uo, a)
    uo.perm_abil.pop(a, None)
    lvl = uo.abilities.pop(a)
    uo.invulnerable = AVUL in uo.abilities or uo.invuln_n > 0
    N.eng.ab.on_remove_abil(uo, a, lvl)
    return True


@nat('GetUnitAbilityLevel')
def _abillevel(N, u, a):
    uo = N.o(u, Unit)
    if not uo:
        return 0
    if a in uo.abilities:
        return uo.abilities[a]
    return 1 if N.eng.ab.has_buff(uo, a) else 0


@nat('SetUnitAbilityLevel')
def _setabillevel(N, u, a, lvl):
    """Unclassed_Native_0029 §3 [obs 0x6f2c7a20]: clamp to [1, level count], then walk one level at a time so every
    intermediate level is entered (the |delta| apply / unapply calls)."""
    uo = N.o(u, Unit)
    if uo and a in uo.abilities:
        r = N.eng.ab.rec(a)
        if r and r.get('lv'):
            mx = len(r['lv'])
            lvl = 1 if lvl < 1 else (mx if lvl > mx else lvl)
        else:
            lvl = max(1, lvl)
        cur = uo.abilities[a]
        if lvl > cur:
            for x in range(cur + 1, lvl + 1):
                N.eng.ab.level_change(uo, a, x)
        elif lvl < cur:
            for x in range(cur - 1, lvl - 1, -1):
                N.eng.ab.level_change(uo, a, x)
        return lvl
    return 0


@nat('UnitApplyTimedLife')
def _timedlife(N, u, buff, dur):
    uo = N.o(u, Unit)
    if uo and not uo.dead:
        N.eng.timed_life(uo, dur)


@nat('UnitPauseTimedLife')
def _pausetimedlife(N, u, flag):
    """Unclassed_Native_0034 §1: freeze / restart the countdown of the unit's timed-life buff. The port keeps one
    expiry step per unit, so a paused timer stores its remaining steps (<= -2); the engine ignores it while paused."""
    uo = N.o(u, Unit)
    if uo is None:
        return None
    now = N.eng.now()
    if flag:
        if uo.expire_at >= 0:
            uo.expire_at = -2 - (uo.expire_at - now)
    elif uo.expire_at <= -2:
        rem = -2 - uo.expire_at
        uo.expire_at = now + (rem if rem > 0 else 0)
    return None


@nat('UnitSetUpgradeProgress')
def _unitnoop(N, *a):
    return None


# ---- PORT5-NativesB round 2: buff category queries / strips, detection (docs/decomp/port/NativesB.md)

@nat('SetAllyColorFilterState')
def _setallyfilter(N, v):
    """0041 §4 [obs 0x6f2d6fd0]: the integer is stored unmodified (no 0/1 normalisation). Default 0 [inf]."""
    N.mm_ally = v


@nat('GetAllyColorFilterState')
def _getallyfilter(N):
    return N.mm_ally


@nat('SetCreepCampFilterState')
def _setcreepfilter(N, v):
    """0041 §4 [obs 0x6f2d7030]: stores state != 0."""
    N.mm_creep = v != 0


@nat('GetCreepCampFilterState')
def _getcreepfilter(N):
    return N.mm_creep


@nat('EnableMinimapFilterButtons')
def _enablefilterbuttons(N, ally, creep):
    """0041 §4 [obs 0x6f2d7060]: only the user's two filter buttons are enabled / disabled; the filter states stay."""
    return None

@nat('UnitShareVision')
def _sharevision(N, u, p, share):
    """0032 §4 [obs 0x6f2cb760]: a bad unit or player does nothing; else one permission flag per (unit, player slot) [inf: accumulate vs replace is open in 0032 §4], set
    or cleared (idempotent). The unit's sight is then also written for that slot [inf: the engine routine 0x6f296910 is not
    decoded]; the fog update picks the new slot mask up at its next turn (fog.py, W3Fog.cs FgUnitState)."""
    uo, pl = N.o(u, Unit), N.o(p, Player)
    if uo is None or pl is None:
        return
    if share:
        uo.share_vis |= 1 << pl.id
    else:
        uo.share_vis &= ~(1 << pl.id)

@nat('UnitHasBuffsEx')
def _hasbuffsex(N, u, pos, neg, magic, phys, timed, aura, auto):
    """0032 §9 [obs 0x6f2cbcf0]: true at the first buff that passes the seven flags; a bad unit answers false."""
    uo = N.o(u, Unit)
    return bool(uo is not None and N.eng.ab.buffs_match(uo, pos, neg, magic, phys, timed, aura, auto))


@nat('UnitCountBuffsEx')
def _countbuffsex(N, u, pos, neg, magic, phys, timed, aura, auto):
    """0032 §10 [obs 0x6f2cbdf0]: the number of buffs that pass the seven flags [inf: the counting twin of the query]."""
    uo = N.o(u, Unit)
    return len(N.eng.ab.buffs_match(uo, pos, neg, magic, phys, timed, aura, auto)) if uo is not None else 0


@nat('UnitRemoveBuffsEx')
def _removebuffsex(N, u, pos, neg, magic, phys, timed, aura, auto):
    """0033 §3 [obs 0x6f2cbfe0]: every matching buff is detached (the count is thrown away)."""
    uo = N.o(u, Unit)
    if uo is not None:
        N.eng.ab.remove_buffs_ex(uo, pos, neg, magic, phys, timed, aura, auto)


@nat('UnitRemoveBuffs')
def _removebuffs(N, u, pos, neg):
    """0032 §11 [obs 0x6f2cbef0]: the strip with the fixed selector prefix (magic / physical / auto-dispel open, timed and
    aura buffs included); (false, false) and (true, true) strip every polarity."""
    uo = N.o(u, Unit)
    if uo is not None:
        N.eng.ab.remove_buffs_ex(uo, pos, neg, False, False, True, True, False)


@nat('IsUnitDetected')
def _isdetected(N, u, p):
    """0031 [obs 0x6f2ca230]: a dead / bad unit or a bad player answers false; else whether a detector or point reveal of
    the player's team covers the unit [inf: the engine reads two per-player counter arrays]."""
    uo, pl = N.o(u, Unit), N.o(p, Player)
    if uo is None or pl is None or uo.dead or not uo.alive:
        return False
    return bool(N.eng.ab.revealed_to(pl, uo))


@nat('IsUnitInvisible')
def _isinvisible(N, u, p):
    """0031 [obs 0x6f2ca3a0]: false for a bad / dead unit or player; else the unit carries invisibility, is not on the
    player's team and no detector of the player covers it [inf: the engine asks the unit's visibility virtual]."""
    uo, pl = N.o(u, Unit), N.o(p, Player)
    if uo is None or pl is None or uo.dead or not uo.alive:
        return False
    if not getattr(uo, 'invisible', 0) or uo.owner is pl or N.eng.ally(pl, uo.owner):
        return False
    return not N.eng.ab.revealed_to(pl, uo)


# ---- PORT5-NativesB: corpse decay, sleep states, item drop orders (docs/decomp/port/NativesB.md)

@nat('UnitSuspendDecay')
def _suspenddecay(N, u, suspend):
    """Unclassed_Native_0032 §5 [obs 0x6f2cb8c0]: only a corpse that is eligible to decay reacts (a living unit, a
    CreateCorpse unit and a unit already gone are no-ops). true detaches it from the decay pipeline (it stays);
    false detaches and re-attaches it, so the decay countdown starts again from now [inf: restart]. The two decay
    events of the re-attach are the ones the decay step already posts when the corpse finally goes."""
    uo = N.o(u, Unit)
    if uo is None or not uo.dead or uo.death_time is None or uo not in N.corpses:
        return None
    if suspend:
        uo.decay_off = True
    else:
        uo.decay_off = False
        uo.death_time = N.vm.qgame.clock
    return None


SLEEP_NORMAL = struct.unpack('>i', b'ACsp')[0]
SLEEP_PERM = struct.unpack('>i', b'Asla')[0]


def _sleep_set(N, u, code, flag):
    """0033 §4/§6 [obs 0x6f2cc0e0]: flag false detaches the state ability when the unit has it, true attaches it only
    when absent (a second add is a no-op)."""
    if not N.o(u, Unit):
        return None
    if flag:
        _addabil(N, u, code)
    elif code in N.o(u, Unit).abilities:
        _remabil(N, u, code)
    return None


@nat('UnitAddSleep')
def _addsleep(N, u, flag):
    return _sleep_set(N, u, SLEEP_NORMAL, flag)


@nat('UnitAddSleepPerm')
def _addsleepperm(N, u, flag):
    return _sleep_set(N, u, SLEEP_PERM, flag)


@nat('UnitCanSleep')
def _cansleep(N, u):
    uo = N.o(u, Unit)
    return bool(uo and SLEEP_NORMAL in uo.abilities)


@nat('UnitCanSleepPerm')
def _cansleepperm(N, u):
    uo = N.o(u, Unit)
    return bool(uo and SLEEP_PERM in uo.abilities)


ALARM_STATE = struct.unpack('>i', b'Aalr')[0]


@nat('UnitIgnoreAlarm')
def _ignorealarm(N, u, flag):
    """0033 §7 [obs 0x6f2cc7d0]: answers whether the live unit has the Aalr state at all (true on every call then, the
    bit is set / cleared as asked); a dead unit or one without the state answers false and changes nothing."""
    uo = N.o(u, Unit)
    if uo is None or uo.dead or ALARM_STATE not in uo.abilities:
        return False
    uo.ignore_alarm = bool(flag)
    return True


@nat('UnitIgnoreAlarmToggled')
def _alarmtoggled(N, u):
    uo = N.o(u, Unit)
    return bool(uo is not None and not uo.dead and ALARM_STATE in uo.abilities and uo.ignore_alarm)


@nat('UnitIsSleeping')
def _issleeping(N, u):
    """0033 §6: bit 0x80 of the Asla state object; the port has no sleeping state (creeps do not fall asleep), so a
    unit never reports sleeping."""
    return False


@nat('UnitWakeUp')
def _wakeup(N, u):
    return None


def _drop_unit_item(N, u, i):
    """0035 §6/§7 [obs 0x6f2ce910]: the unit must hold items and the item resolve; (unit, item) or (None, None)."""
    uo, it = N.o(u, Unit), _item(N, i)
    if uo is None or it is None or N.eng.itm.inventory(uo) is None:
        return None, None
    return uo, it


@nat('UnitDropItemPoint')
def _dropitempoint(N, u, i, x, y):
    """NOTES_items §4.5 [obs 0x6f2ce740]: the dropitem order (852001) with a point: walk within DropItemRange, then drop."""
    uo, it = _drop_unit_item(N, u, i)
    return bool(uo is not None and N.eng.itm.order_drop(uo, it, x, y))


@nat('UnitDropItemTarget')
def _dropitemtarget(N, u, i, w):
    """0035 §6: the instant target order 0xd0021 with the widget as target. A unit target takes the item (give within
    GiveItemRange, else dropped next to it); an item target is a drop at its position [inf]."""
    uo, it = _drop_unit_item(N, u, i)
    t = N.o(w)
    if uo is None or t is None:
        return False
    if isinstance(t, Unit):
        return bool(t.alive and N.eng.itm.order_give(uo, it, t))
    if isinstance(t, Item):
        return bool(N.eng.itm.order_drop(uo, it, t.x, t.y))
    return False                                              # a destructable target has no port state [inf]


@nat('UnitDropItemSlot')
def _dropitemslot(N, u, i, slot):
    """0035 §7 [obs 0x6f2ceb00]: the slot must be below the container size; the order 0xd0022 + slot moves the item
    there (an occupied slot swaps). The item must be one the unit carries (the order system's test)."""
    uo, it = _drop_unit_item(N, u, i)
    if uo is None or not 0 <= slot < N.eng.itm.size(uo) or it.carrier is not uo or it not in uo.items:
        return False
    return bool(N.eng.itm.move_slot(uo, uo.items.index(it), slot))


@nat('UnitResetCooldown')
def _resetcooldown(N, u):
    """Unclassed_Native_0033 §8 [obs 0x6f2cc9e0]: clear the remaining cooldown of every member the unit has on
    cooldown (abilities and item cooldowns; the port keys them by unit serial, so all its entries go)."""
    uo = N.o(u, Unit)
    if uo is None:
        return
    cool = N.eng.sp.cool
    for k in list(cool):
        if k[0] == uo.serial:
            del cool[k]


@nat('UnitSetConstructionProgress')
def _conprogress(N, u, pct):
    uo = N.o(u, Unit)
    if uo is not None:
        N.eng.set_construction_progress(uo, pct)


@nat('IssueImmediateOrder')
def _issueimm(N, u, order):
    return N.issue_order(N.o(u, Unit), ORDER_IDS.get(order, 0))


@nat('IssueImmediateOrderById')
def _issueimmid(N, u, oid):
    uo = N.o(u, Unit)
    if oid == 851986 and uo is not None and uo.alive and not uo.dead:
        return True         # [obs runtime 1.26a] docs/oracle.md "order natives" (ext_combat): IssueImmediateOrderById(move) -> true, order stays 0
    return N.issue_order(uo, oid)


def _point_order_ret(oid, r):
    """MOVE-PORT [obs runtime 1.26a] docs/oracle.md "order natives" (ext_combat): IssuePointOrder smart -> false while
    the order runs (order 851971); slot 191 §2.3 step 1 requires a target widget, so the point form answers 0xdd.
    AImove 851988 answers false too (R2) while its order runs; it is accepted in the target form (slot 191 step 11),
    so only the point forms are overridden. The other point orders answer true (move / attack / patrol). Only the JASS
    Boolean is affected; the engine order runs."""
    if oid == 851971 or oid == 851988:
        return False
    return r


@nat('IssuePointOrder')
def _issuept(N, u, order, x, y):
    oid = ORDER_IDS.get(order, 0)
    return _point_order_ret(oid, N.issue_order(N.o(u, Unit), oid, x, y))


@nat('IssuePointOrderById')
def _issueptid(N, u, oid, x, y):
    return _point_order_ret(oid, N.issue_order(N.o(u, Unit), oid, x, y))


@nat('IssuePointOrderLoc')
def _issueptloc(N, u, order, l):
    lo = N.o(l, Location)
    if not lo:
        return False
    oid = ORDER_IDS.get(order, 0)
    return _point_order_ret(oid, N.issue_order(N.o(u, Unit), oid, lo.x, lo.y))


@nat('IssuePointOrderByIdLoc')
def _issueptidloc(N, u, oid, l):
    lo = N.o(l, Location)
    if not lo:
        return False
    return _point_order_ret(oid, N.issue_order(N.o(u, Unit), oid, lo.x, lo.y))


def _destr_target(N, t):
    o = N.o(t)
    return o if o is not None and hasattr(o, 'dalive') and getattr(o, 'tname', '') == 'destructable' else None


# the unit control orders plus the target-only orders (no point form): getitem, harvest / resumeharvesting /
# returnresources, repair, board / forceboard / load / loadcorpse [inf]; attackonce is already in the control set
_TARGET_FALLBACK_SKIP = {851971, 851972, 851983, 851985, 851986, 851988, 851990, 851993,
                         851981, 852017, 852018, 852020, 852024, 852043, 852044, 852046, 852050}


def _target_point_order(N, uo, oid, tu):
    """Unclassed_Native_0034 §4 [obs 0x6f2cd580]: when the order's classification has a fallback, a refused target
    form is retried as a point order at the target's (issue-time) position. The port has no mask table; the unit
    control orders (smart / attack / move / patrol / hold / stop) are the target-only classes."""
    if tu is None:
        return False
    r = N.issue_order(uo, oid, target=tu)
    if not r and oid not in _TARGET_FALLBACK_SKIP:
        x, y = getattr(tu, 'x', None), getattr(tu, 'y', None)
        if x is not None and y is not None:
            r = N.issue_order(uo, oid, x, y)
    return r


@nat('IssueTargetOrder')
def _issuetgt(N, u, order, t):
    tu = N.o(t, Unit) or _item(N, t) or _destr_target(N, t)       # W8: item targets (pickup); W9-PYR-DEST: destructables
    return _target_point_order(N, N.o(u, Unit), ORDER_IDS.get(order, 0), tu)


@nat('IssueTargetOrderById')
def _issuetgtid(N, u, oid, t):
    tu = N.o(t, Unit) or _item(N, t) or _destr_target(N, t)
    return _target_point_order(N, N.o(u, Unit), oid, tu)


def _instant_target(N, uo, oid, t):
    """Unclassed_Native_0034 §6 / 0035 §3: the target form first, then the point form at the target's position; a
    missing target makes the point the issuer's own position. The optional instant widget only decorates the order
    record in the engine and has no port state."""
    if uo is None:
        return False
    tu = N.o(t, Unit) or _item(N, t) or _destr_target(N, t)
    if tu is None:
        return N.issue_order(uo, oid, uo.x, uo.y)
    return _target_point_order(N, uo, oid, tu)


def _instant_target_str(N, uo, oid, t, instant):
    """1.26a string-form shim (Unclassed_Native_0035 §3 [obs 0x6f3c8e60]): the unit, the target widget and the
    instant widget must all resolve, otherwise false."""
    if uo is None or not t or not instant:
        return False
    if not (N.o(t, Unit) or _item(N, t) or _destr_target(N, t)):
        return False
    if not (N.o(instant, Unit) or _item(N, instant) or _destr_target(N, instant)):
        return False
    return _instant_target(N, uo, oid, t)


@nat('IssueInstantPointOrderById')
def _instantptid(N, u, oid, x, y, instant):
    return N.issue_order(N.o(u, Unit), oid, x, y)


@nat('IssueInstantPointOrder')
def _instantpt(N, u, order, x, y, instant):
    uo = N.o(u, Unit)
    if uo is None or not instant or not (N.o(instant, Unit) or _item(N, instant) or _destr_target(N, instant)):
        return False                                       # 1.26a shim needs the instant widget [obs 0x6f3c8e40]
    return N.issue_order(N.o(u, Unit), ORDER_IDS.get(order, 0), x, y)


@nat('IssueInstantTargetOrderById')
def _instanttgtid(N, u, oid, t, instant):
    return _instant_target(N, N.o(u, Unit), oid, t)


@nat('IssueInstantTargetOrder')
def _instanttgt(N, u, order, t, instant):
    return _instant_target_str(N, N.o(u, Unit), ORDER_IDS.get(order, 0), t, instant)


@nat('IssueBuildOrderById')
def _issuebuildid(N, u, tid, x, y):
    return N.issue_order(N.o(u, Unit), tid, x, y)      # NOTES_units §4 (placement test inside the engine)


@nat('IssueBuildOrder')
def _issuebuild(N, u, name, x, y):
    return N.issue_order(N.o(u, Unit), _unitid(N, name), x, y)


@nat('UnitDamageTarget')
def _unitdamage(N, u, w, amount, attack, ranged, atk, dmg, weapon):
    """NOTES_combat §1.1: atk >= 8, weapon >= 25 or damage type > 31 -> false; else the pipeline, always true.
    [obs runtime 1.26a] docs/oracle.md "UnitDamageTarget null source" (ext_combat): a null source unit -> false, no damage."""
    if atk >= 8 or weapon >= 25 or dmg > 31 or atk < 0 or dmg < 0:
        return False
    if N.o(u, Unit) is None:
        return False
    tu = N.o(w, Unit)
    if tu is not None:
        N.eng.damage(tu, N.o(u, Unit), amount, atk if atk < 7 else 0, dmg, bool(attack), bool(ranged))
    return True


@nat('GetUnitCurrentOrder')
def _curorder(N, u):
    uo = N.o(u, Unit)
    if uo and uo.upg is not None and not uo.order:
        return uo.upg[0]            # BUGS ETD-ORA6 [obs oracle td_upgrade_events]: the upgrade order's id = the target type
    if uo and uo.order == 851986 and uo.flee_oid:
        return uo.flee_oid          # [obs runtime 1.26a] docs/oracle.md flee: a creep-owned worker's flee reports 852018
    if uo and uo.order == 851990 and uo.pat is not None:
        return 851991               # MOVE-PORT [obs runtime 1.26a R3]: a patrolling unit reports the two-point order 0xd0017
    return uo.order if uo else 0


@nat('GetUnitFoodUsed')
def _foodused(N, u):
    uo = N.o_read(u, Unit)
    return int(uo.data.get('fused', 0)) if uo else 0


@nat('GetUnitFoodMade')
def _foodmade(N, u):
    uo = N.o_read(u, Unit)
    return int(uo.data.get('fmade', 0)) if uo else 0


@nat('GetFoodUsed')
def _foodusedid(N, tid):
    return int(N.unit_types.get(tid, {}).get('fused', 0))


@nat('GetFoodMade')
def _foodmadeid(N, tid):
    return int(N.unit_types.get(tid, {}).get('fmade', 0))


@nat('GetUnitPointValue')
def _pointvalue(N, u):
    uo = N.o_read(u, Unit)
    return int(uo.data.get('points', 0)) if uo else 0


@nat('GetUnitPointValueByType')
def _pointvaluet(N, tid):
    return int(N.unit_types.get(tid, {}).get('points', 0))


@nat('GetUnitLevel')
def _unitlevel(N, u):
    """Unclassed_Native_0028 §7: heroes answer their level; a non-hero below 1 answers 0 and above the
    global maximum is clamped to it in the binary; that maximum is not confirmed as MiscGame MaxUnitLevel [open], so
    the port answers the raw level."""
    uo = N.o_read(u, Unit)
    if not uo:
        return 0
    if getattr(uo, 'hero', False):
        return uo.hlevel
    lv = int(uo.level)
    if lv < 1:
        return 0
    return lv                                              # the clamp's constant is open (NativesB.md): raw level


def _hero(N, u):
    uo = N.o(u, Unit)
    return uo if uo is not None and getattr(uo, 'hero', False) else None


@nat('GetHeroLevel')
def _herolevel(N, u):
    h = _hero(N, u)
    return h.hlevel if h else 0


@nat('GetHeroXP')
def _herox(N, u):
    h = _hero(N, u)
    return h.xp if h else 0


@nat('AddHeroXP')
def _addherox(N, u, x, show):
    """NOTES_heroes §1.4: AddXP without handicap / creep factor / tier bonus."""
    h = _hero(N, u)
    if h:
        N.eng.hero.add_xp(h, float(x))


@nat('SetHeroXP')
def _setherox(N, u, x, show):
    h = _hero(N, u)
    if h and x > h.xp:
        N.eng.hero.add_xp(h, float(x - h.xp))      # cannot lower [obs]


@nat('SetHeroLevel')
def _setherolevel(N, u, lvl, show):
    h = _hero(N, u)
    if h:
        N.eng.hero.set_level(h, lvl)


@nat('SuspendHeroXP')
def _suspendxp(N, u, flag):
    h = _hero(N, u)
    if h:
        h.xp_suspended = bool(flag)


@nat('IsSuspendedXP')
def _issuspended(N, u):
    """Unclassed_Native_0028 §13 [obs 0x6f2c7210]: every rejected argument (null / non-hero / removed) answers true."""
    h = _hero(N, u)
    return True if h is None else bool(h.xp_suspended)


def _heroattr(N, u, k, bonus=True):
    """Unclassed_Native_0027 §3: include-bonuses false subtracts the item / ability layer; permanent sources
    stay in the cell."""
    h = _hero(N, u)
    if not h:
        return 0
    hero = N.eng.hero
    return hero.attr(h, k) if bonus else hero.attr_no_bonus(h, k)


@nat('GetHeroStr')
def _herostr(N, u, bonus):
    return _heroattr(N, u, 'STR', bonus)


@nat('GetHeroAgi')
def _heroagi(N, u, bonus):
    return _heroattr(N, u, 'AGI', bonus)


@nat('GetHeroInt')
def _heroint(N, u, bonus):
    return _heroattr(N, u, 'INT', bonus)


@nat('GetHeroSkillPoints')
def _heroskill(N, u):
    h = _hero(N, u)
    return h.skill if h else 0


@nat('UnitModifySkillPoints')
def _modskill(N, u, d):
    """§4.3 0x6f4d8510: the clamped delta is applied; false when nothing changes (no point to take back / no free
    slot to add), true otherwise (even when the capacity clamped the delta to 0). The shared hero gate of
    Unclassed_AI_0001 §7 refuses an illusion (capital type letter and flag 0x40000000 clear)."""
    h = _hero(N, u)
    if not h or getattr(h, 'illusion', False):
        return False
    raw = d
    p = h.skill
    if p <= -d and p != -d:
        d = -p                                             # never take back more than the hero has
    if d == 0:
        return False                                       # CAbilityHero_p1 0x6f4d8510 step 1
    cap = N.eng.hero.learnable(h)                          # Σ max(0, levels − current level)
    if cap < d:
        d = cap
    h.skill += d                                          # §4.3 0x6f4d8510 step 4: only the delta is applied
    return raw != 0                                       # §4: the pre-clamp delta decides the answer


@nat('SelectHeroSkill')
def _selectskill(N, u, aid):
    h = _hero(N, u)
    if h:
        N.eng.hero.learn(h, aid)


@nat('UnitStripHeroLevel')
def _striphero(N, u, n):
    """§4.3 0x6f2c65e0 → 0x6f4d84a0: the integer is a number of levels (not the target level); true when a level went."""
    h = _hero(N, u)
    return N.eng.hero.strip_levels(h, int(n)) if h and not getattr(h, 'illusion', False) else False


def _item(N, i):
    it = N.o(i)
    return it if isinstance(it, Item) else None


def new_item(N, tid, x, y):
    """NOTES_items §3: unknown id / 0 -> null; life = HP, charges = uses (0 = none). A-IT1: the position is the
    nearest valid point of the placement search (a valid point stays exact)."""
    d = N.eng.item_types.get(tid) if N.eng else None
    if d is None:
        return None
    if N.eng is not None:
        x, y = N.eng.itm.valid_point(x, y)
    it = Item(tid, d, r32(x), r32(y))
    N.eng.itm.created(it)
    return it


@nat('UnitAddItem')
def _unitadditem(N, u, i):
    uo, it = N.o(u, Unit), _item(N, i)
    if not uo or it is None:
        return False
    return N.eng.itm.add(uo, it)


@nat('UnitAddItemById')
def _unitadditemid(N, u, tid):
    """§4.3: the item is created at the unit, then added (failure leaves it on the ground there)."""
    uo = N.o(u, Unit)
    if not uo:
        return None
    it = new_item(N, tid, uo.x, uo.y)
    if it is not None:
        N.eng.itm.add(uo, it)
    return it


@nat('UnitAddItemToSlotById')
def _unitadditemslot(N, u, tid, k):
    uo = N.o(u, Unit)
    if not uo:
        return False
    it = new_item(N, tid, uo.x, uo.y)
    return bool(it is not None and N.eng.itm.add(uo, it, slot=k))


@nat('UnitItemInSlot')
def _iteminslot(N, u, k):
    uo = N.o(u, Unit)
    if uo and 0 <= k < 6:
        return uo.items[k]
    return None


@nat('UnitRemoveItem')
def _unitremitem(N, u, i):
    uo, it = N.o(u, Unit), _item(N, i)
    if uo and it is not None and it in uo.items:
        N.eng.itm.remove(uo, it)


@nat('UnitUseItem', 'UnitUseItemPoint', 'UnitUseItemTarget')
def _unituseitem(N, u, i, *a):
    """NOTES_items §4.5.1: useslot (852008 + slot) as an immediate / point / target order; false when u does not
    carry the item or the item has no useslot."""
    uo, it = N.o(u, Unit), _item(N, i)
    if uo is None or it is None or it.carrier is not uo or not 0 <= it.slot < 6:
        return False
    oid = 852008 + it.slot
    if len(a) == 2:
        return N.issue_order(uo, oid, a[0], a[1])
    if len(a) == 1:
        t = N.o(a[0])
        return N.issue_order(uo, oid, target=t) if t is not None and getattr(t, 'alive', False) else False
    return N.issue_order(uo, oid)


@nat('UnitRemoveItemFromSlot')
def _unitremslot(N, u, k):
    uo = N.o(u, Unit)
    if uo and 0 <= k < 6 and uo.items[k] is not None:
        it = uo.items[k]
        N.eng.itm.remove(uo, it)
        return it
    return None


@nat('UnitHasItem')
def _unithasitem(N, u, i):
    uo, it = N.o(u, Unit), _item(N, i)
    return bool(uo and it is not None and it in uo.items)


@nat('UnitInventorySize')
def _invsize(N, u):
    uo = N.o(u, Unit)
    return N.eng.itm.size(uo) if uo else 0


@nat('CreateItem')
def _createitem(N, tid, x, y):
    return new_item(N, tid, x, y)


@nat('RemoveItem')
def _removeitem(N, i):
    it = _item(N, i)
    if it is not None and it.live:              # items.kill: a dead / dying item is a pure no-op
        N.eng.itm.kill(it, remove=True)        # §3.2: hide, carried item fires DROP first, hidden deletes at once


@nat('IsItemOwned')
def _itemowned(N, i):
    it = _item(N, i)
    return bool(it and it.carrier is not None)


@nat('GetItemCharges')
def _itemcharges(N, i):
    it = _item(N, i)
    return it.charges if it else 0


@nat('SetItemCharges')
def _setitemcharges(N, i, n):
    it = _item(N, i)
    if it:
        it.charges = n if n > 0 else 0
        it.uncharged = n <= 0                    # NOTES_items §4.5.1 [obs]


@nat('IsItemPowerup')
def _itempowerupi(N, i):
    it = _item(N, i)
    return bool(it and (it.fl & ID.FLAG_POWERUP))


@nat('IsItemPawnable')
def _itempawnablei(N, i):
    """CItem_p1 §4 bit 0x2000 of the item's own flag word (Unclassed_Native_0023 §1.2): the getter of the word
    SetItemPawnable writes, not the type record's field."""
    it = _item(N, i)
    return bool(it and (it.fl & ID.FLAG_PAWNABLE))


@nat('IsItemSellable')
def _itemsellablei(N, i):
    """Unclassed_Native_0023 §1.1: the item TYPE record's sellable field (all items of a type answer alike),
    unlike IsItemPawnable which reads the instance's own flag word."""
    it = _item(N, i)
    return bool(it and it.data.get('sellable'))


@nat('GetItemLevel')
def _itemlevel(N, i):
    it = _item(N, i)
    return int(it.data.get('Level', 0)) if it else 0


@nat('GetItemType')
def _itemtypei(N, i):
    it = _item(N, i)
    return int(it.data.get('cls', 7)) if it else 0


@nat('GetItemTypeId')
def _itemtype(N, i):
    it = N.o(i)
    return getattr(it, 'tid', 0) if it is not None else 0


@nat('GetItemX')
def _itemx(N, i):
    it = N.o(i)
    return getattr(it, 'x', 0.0) if it is not None else 0.0


@nat('GetItemY')
def _itemy(N, i):
    it = N.o(i)
    return getattr(it, 'y', 0.0) if it is not None else 0.0


@nat('IsItemIdSellable')
def _itemsellable(N, i):
    d = N.eng.item_types.get(i) if N.eng else None
    return bool(d and d.get('sellable'))


@nat('IsItemIdPawnable')
def _itempawnable(N, i):
    d = N.eng.item_types.get(i) if N.eng else None
    return bool(d and d.get('pawnable'))


@nat('IsItemIdPowerup')
def _itempowerup(N, i):
    d = N.eng.item_types.get(i) if N.eng else None
    return bool(d and d.get('powerup'))


def _stock_natives(name, kind):
    """NOTES_items §7.7 (A-SH9): the per-unit stock natives work on the unit's Asid (kind 1) / Asud (kind 2) only
    (no-op without one); the All-variants need an item / unit type id, then every living Dynamic shop."""
    def unit(N, u):
        uo = N.o(u, Unit)
        return uo if uo is not None and N.eng is not None else None

    def known(N, i):
        return (N.unit_types if kind == 2 else N.eng.item_types).get(i) is not None

    def add(N, u, i, cur, mx):
        uo = unit(N, u)
        if uo is not None:
            N.eng.shops.dyn_add(uo, kind, i, cur, mx)

    def rem(N, u, i):
        uo = unit(N, u)
        if uo is not None:
            N.eng.shops.dyn_remove(uo, kind, i)

    def slots(N, u, n):
        uo = unit(N, u)
        if uo is not None:
            N.eng.shops.dyn_slots(uo, kind, n)

    def add_all(N, i, cur, mx):
        if N.eng is not None and known(N, i):
            N.eng.shops.dyn_all(kind, N.eng.shops.dyn_add, i, cur, mx)

    def rem_all(N, i):
        if N.eng is not None and known(N, i):
            N.eng.shops.dyn_all(kind, N.eng.shops.dyn_remove, i)

    def slots_all(N, n):
        if N.eng is not None:
            N.eng.shops.dyn_all(kind, N.eng.shops.dyn_slots, n)

    nat('Add%sToStock' % name)(add)
    nat('Remove%sFromStock' % name)(rem)
    nat('Set%sTypeSlots' % name)(slots)
    nat('Add%sToAllStock' % name)(add_all)
    nat('Remove%sFromAllStock' % name)(rem_all)
    nat('SetAll%sTypeSlots' % name)(slots_all)


_stock_natives('Item', 1)
_stock_natives('Unit', 2)


def _setitemflag(N, i, b, bit):
    """SetItemDropOnDeath / SetItemDroppable / SetItemPawnable: read-modify-write of the item's own flag word,
    no side effects (CItem_p1 §4 [obs 0x6f1dbce0–0x6f1dbd10])."""
    it = _item(N, i)
    if it is not None:
        if b:
            it.fl |= bit
        else:
            it.fl &= ~bit


@nat('SetItemDropOnDeath')
def _setitemdrop(N, i, b):
    _setitemflag(N, i, b, ID.FLAG_DROP)


@nat('SetItemDroppable')
def _setitemdroppable(N, i, b):
    _setitemflag(N, i, b, ID.FLAG_DROPPABLE)


@nat('SetItemPawnable')
def _setitempawnable(N, i, b):
    _setitemflag(N, i, b, ID.FLAG_PAWNABLE)


@nat('SetItemDropID')
def _itemnoop(N, *a):
    """Unclassed_Native_0023 §1.3: the id is stored at +0xcc; no consumer in the port (the 5 s dead-item drop
    timer is the only reader; it is modelled since PORT13-Inventory, A-IT7 fixed, see docs/BUGS.md)."""
    return None


@nat('SetItemPlayer')
def _setitemplayer(N, i, p, colour):
    """Unclassed_Native_0022 §5: write the item's owner player (+0x74); a null item or a null player is a silent
    no-op (the item keeps its owner). The colour argument only recolours the model (presentation). No event, no
    flag change, no move."""
    it = _item(N, i)
    pl = N.o(p, Player)
    if it is None or pl is None:
        return None
    it.owner = pl
    return None


@nat('GetItemPlayer')
def _itemplayer(N, i):
    """Unclassed_Native_0022 §4: the item's current owner (the last owner for a ground item); 0 for a null handle."""
    it = _item(N, i)
    return N.H(it.owner) if it is not None and it.owner is not None else 0


@nat('SetItemVisible')
def _setitemvisible(N, i, flag):
    """NOTES_items §3.2 / Uncl. 0022 §7: true shows, false hides; only acts while the item is not carried (a
    carried item has no ground model). The state is the port's hidden bit of the item's own word."""
    it = _item(N, i)
    if it is None or it.carrier is not None:
        return None
    if flag:
        it.fl &= ~ID.FLAG_HIDDEN
    else:
        it.fl |= ID.FLAG_HIDDEN
    return None


@nat('IsItemVisible')
def _itemvisiblei(N, i):
    """Uncl. 0022 §7: the inverse of the hidden bit; false for a null / stale handle."""
    it = _item(N, i)
    return bool(it is not None and not (it.fl & ID.FLAG_HIDDEN))


@nat('SetItemPosition')
def _setitempos(N, i, x, y):
    """NOTES_items §3.2: a carried item is first removed from its carrier as UnitRemoveItem (the DROP event fires),
    then the item is placed at the point and shown. The placement routine runs the nearest-valid-point search
    (A-IT1, `Items.valid_point`; a valid point stays exact), so the show and the search halves are both reproduced."""
    it = _item(N, i)
    if it is None:
        return None
    if it.carrier is not None:
        N.eng.itm.remove(it.carrier, it)                # §4.4: DROP first, then the item is on the ground
    N.eng.itm.place(it, x, y)                          # the placement routine ends with "show"
    it.fl &= ~ID.FLAG_HIDDEN
    return None


def _random_item(N, itype, level):
    """NOTES_items §2: pool in list order, one below(n) on game stream 0x23; empty pool -> 0 without a draw."""
    from .itemdata import random_pool
    pool = random_pool(N.eng.items, itype, level) if N.eng else []
    if not pool:
        return 0
    i = N.streams.game[0x23].below(len(pool))
    s = pool[i]
    return (ord(s[0]) << 24) | (ord(s[1]) << 16) | (ord(s[2]) << 8) | ord(s[3])


@nat('ChooseRandomItem')
def _chooseranditem(N, level):
    return _random_item(N, 7, level)


@nat('ChooseRandomItemEx')
def _chooseranditemex(N, itype, level):
    return _random_item(N, itype, level)


def _mkdestr(N, tid, x, y, face, dead):
    """BUGS DS-2: the destructable also blocks with its path texture (engine.dest_add); life = the type's HP."""
    id4 = ''.join(chr((tid >> s) & 255) for s in (24, 16, 8, 0))
    eng = N.eng
    t = eng.dest_types.get(id4) if eng is not None else None
    hp = float(t['hp']) if t and t['hp'] > 0 else 100.0
    o = Simple('destructable', tid=tid, x=x, y=y, life=0.0 if dead else hp, maxlife=hp, id4=id4, facing=face,
               dalive=not dead, fpalive=not dead, fp=None)  # dalive: not 'alive' (= the object exists, vm.obj)
    if eng is not None:
        eng.dest_add(o)
    return o


@nat('CreateDestructable')
def _createdestr(N, tid, x, y, face, scale, var):
    return _mkdestr(N, tid, x, y, face, False)


@nat('CreateDeadDestructable')
def _createdeaddestr(N, tid, x, y, face, scale, var):
    return _mkdestr(N, tid, x, y, face, True)


@nat('CreateDestructableZ')
def _createdestrz(N, tid, x, y, z, face, scale, var):
    return _mkdestr(N, tid, x, y, face, False)


@nat('CreateDeadDestructableZ')
def _createdeaddestrz(N, tid, x, y, z, face, scale, var):
    return _mkdestr(N, tid, x, y, face, True)


def _destr_alive(N, o, alive):
    if N.eng is not None and hasattr(o, 'dalive'):
        N.eng.dest_set_alive(o, alive)


def _destr_life(N, o, v, restore=False):
    """CDestructable_p1 §10 (both builds): the absolute life setter / DestructableRestoreLife through the engine."""
    if N.eng is not None and hasattr(o, 'dalive'):
        N.eng.dest_set_life(o, v, restore)
    else:
        o.life = v


@nat('KillDestructable')
def _killdestr(N, d):
    o = N.o(d)
    if o is not None:
        _destr_life(N, o, 0.0)


@nat('RemoveDestructable')
def _remdestr(N, d):
    o = N.o(d)
    if o is not None:
        if N.eng is not None and hasattr(o, 'dalive'):
            N.eng.dest_remove(o)
        N.vm.H.destroyed(o)


@nat('GetDestructableLife')
def _destrlife(N, d):
    o = N.o(d)
    return getattr(o, 'life', 0.0) if o is not None else 0.0


@nat('SetDestructableLife')
def _setdestrlife(N, d, v):
    o = N.o(d)
    if o is not None:
        _destr_life(N, o, v)                       # §10: clamp + the 0.4 death crossing


@nat('GetDestructableMaxLife')
def _destrmaxlife(N, d):
    o = N.o(d)
    return getattr(o, 'maxlife', 0.0) if o is not None else 0.0


@nat('SetDestructableMaxLife')
def _setdestrmaxlife(N, d, v):
    """Unclassed_Native_0021 §7 / CDestructable_p1 §10 (+0x144): write the ceiling and re-clamp the current life;
    no death-crossing test and no event (unlike SetDestructableLife)."""
    o = N.o(d)
    if o is not None and hasattr(o, 'maxlife') and N.eng is not None:
        N.eng.dest_set_max_life(o, v)


@nat('DestructableRestoreLife')
def _destrrestore(N, d, v, birth):
    o = N.o(d)
    if o is not None:
        _destr_life(N, o, v, restore=True)         # §10: floor 0.5 / NaN -> max life


@nat('GetDestructableX')
def _destrx(N, d):
    o = N.o(d)
    return getattr(o, 'x', 0.0) if o is not None else 0.0


@nat('GetDestructableY')
def _destry(N, d):
    o = N.o(d)
    return getattr(o, 'y', 0.0) if o is not None else 0.0


@nat('GetDestructableTypeId')
def _destrtype(N, d):
    o = N.o(d)
    return getattr(o, 'tid', 0) if o is not None else 0


@nat('EnumItemsInRect')
def _enumitems(N, r, filt, code):
    """Unclassed_Native_0023 §2 (0x6f2c2310 / 0x6f3c5110): a null rect or a null action is a silent no-op; the
    world list is visited in creation order, a null filter accepts all, otherwise the filter sees
    GetFilterItem and the action sees GetEnumItem, interleaved per candidate. Carried items are not on the ground
    list (the engine's visibility gate skips them), and a candidate removed by an earlier action is skipped."""
    ro = N.o(r, Rect)
    eng = N.eng
    if ro is None or eng is None or not code:
        return
    vm = N.vm
    cand = [it for it in eng.itm.ground
            if ro.minx <= it.x <= ro.maxx and ro.miny <= it.y <= ro.maxy]
    fx = _bx(N, filt)
    for it in cand:
        if not it.live or it.carrier is not None:
            continue
        hid = N.H(it)
        if fx is not None and not vm.eval_filter(fx, V.CTX_FILTER_ITEM, hid):
            continue
        if not it.live or it.carrier is not None:
            continue
        vm.run_callback(code, 'enum', V.CTX_ENUM_ITEM, hid)


@nat('EnumDestructablesInRect')
def _enumdestr(N, r, filt, code):
    """BUGS W9R3-4-CHA (EnumDestructablesInRectAll is Blizzard.j: the same with a null filter). The destructables inside the rect
    (min <= x <= max, y alike, dead ones too [inf]) are collected when the native is called: first the ones placed by the map
    (war3map.doo order), then the script-made ones in creation order = Engine.dests order [inf: no NOTES entry for the game's
    enumeration order]. Then, per candidate that still exists, in that order: its handle (a placed one gets it here, the first
    time a script sees it), the filter with GetFilterDestructable, and when it passes the callback with GetEnumDestructable
    (own thread and budget, like ForGroup) [inf: filter and callback interleaved per candidate]."""
    ro = N.o(r, Rect)
    eng = N.eng
    if ro is None or eng is None or not code:
        return
    vm = N.vm
    cand = [o for o in eng.dests if ro.minx <= o.x <= ro.maxx and ro.miny <= o.y <= ro.maxy]
    fx = _bx(N, filt)
    for o in cand:
        if not o.alive:
            continue
        hid = N.H(o)
        if fx is not None and not vm.eval_filter(fx, V.CTX_FILTER_DESTR, hid):
            continue
        if not o.alive:
            continue
        vm.run_callback(code, 'enum', V.CTX_ENUM_DESTR, hid)


@nat('CreateBlightedGoldmine')
def _goldmine(N, p, x, y, face):
    return _createunit(N, p, 0x75676f6c, x, y, face)          # 'ugol' (Haunted Gold Mine, Abgm; ORACLE-ECON: real native makes a haunted mine)


# ---- groups

@nat('CreateGroup')
def _creategroup(N):
    return Group()


@nat('DestroyGroup')
def _destroygroup(N, g):
    go = N.o(g, Group)
    if go:
        go.units = []
        N.vm.H.destroyed(go)


@nat('GroupAddUnit')
def _groupadd(N, g, u):
    go, uo = N.o(g, Group), N.o(u, Unit)
    if go and uo:
        N.group_add(go, uo)


@nat('GroupRemoveUnit')
def _grouprem(N, g, u):
    go, uo = N.o(g, Group), N.o(u, Unit)
    if go and uo and uo in go.units:
        go.units.remove(uo)


@nat('GroupClear')
def _groupclear(N, g):
    go = N.o(g, Group)
    if go:
        go.units = []


@nat('GroupEnumUnitsOfType')
def _genumtype(N, g, name, filt):
    tid = _unitid(N, name)
    N.enum_units(N.o(g, Group), [u for u in N.units if u.tid == tid], _bx(N, filt))


@nat('GroupEnumUnitsOfTypeCounted')
def _genumtypec(N, g, name, filt, n):
    tid = _unitid(N, name)
    N.enum_units(N.o(g, Group), [u for u in N.units if u.tid == tid], _bx(N, filt), n)


@nat('GroupEnumUnitsOfPlayer')
def _genumplayer(N, g, p, filt):
    pl = N.o(p, Player)
    N.enum_units(N.o(g, Group), [u for u in N.units if u.owner is pl], _bx(N, filt))


def _in_rect(u, ro):
    return ro.minx <= u.x <= ro.maxx and ro.miny <= u.y <= ro.maxy


@nat('GroupEnumUnitsInRect')
def _genumrect(N, g, r, filt):
    ro = N.o(r, Rect)
    N.enum_units(N.o(g, Group), [u for u in N.units if ro and _in_rect(u, ro) and not u.locust], _bx(N, filt))


@nat('GroupEnumUnitsInRectCounted')
def _genumrectc(N, g, r, filt, n):
    ro = N.o(r, Rect)
    N.enum_units(N.o(g, Group), [u for u in N.units if ro and _in_rect(u, ro) and not u.locust], _bx(N, filt), n)


@nat('GroupEnumUnitsInRange')
def _genumrange(N, g, x, y, d, filt):
    N.enum_units(N.o(g, Group), [u for u in N.units if math.hypot(u.x - x, u.y - y) <= d and not u.locust],
                 _bx(N, filt))                   # §5.34: the spatial query's class mask 0x14fd0000 skips locusts


@nat('GroupEnumUnitsInRangeOfLoc')
def _genumrangeloc(N, g, l, d, filt):
    lo = N.o(l, Location)
    if lo:
        _genumrange(N, g, lo.x, lo.y, d, filt)


@nat('GroupEnumUnitsInRangeCounted')
def _genumrangec(N, g, x, y, d, filt, n):
    N.enum_units(N.o(g, Group), [u for u in N.units if math.hypot(u.x - x, u.y - y) <= d and not u.locust],
                 _bx(N, filt), n)


@nat('GroupEnumUnitsInRangeOfLocCounted')
def _genumrangelocc(N, g, l, d, filt, n):
    lo = N.o(l, Location)
    if lo:
        _genumrangec(N, g, lo.x, lo.y, d, filt, n)


@nat('GroupEnumUnitsSelected')
def _genumsel(N, g, p, filt):
    pl = N.o(p, Player)                           # BUGS SEL-1: the player's selection, in selection order
    N.enum_units(N.o(g, Group), N.eng.selected(pl) if pl is not None else [], _bx(N, filt))


def _group_order(N, g, oid, x=None, y=None, target=None):
    go = N.o(g, Group)
    ok = False
    taken = []
    if go:
        units = list(go.units)
        if GROUP_CAP12 and len(units) > GROUP_ORDER_MAX:
            units = units[:GROUP_ORDER_MAX]                 # CGroup §0.2: the dispatcher processes min(count, 12)
        for u in units:
            if u.alive:
                r = N.issue_order(u, oid, x, y, target)
                ok = r or ok
                if r:
                    taken.append(u)
                elif target is not None:
                    # CGroup §6 [obs 0x6f2f0cd0 / 0x6f3e7b00]: a failed *target* validation makes the per-unit
                    # callback return 0, which aborts the pass — the remaining members are not ordered. The
                    # validation of the issuing path is order_unit_target_ok (a unit target); an order the unit
                    # itself cannot take (no weapon / no move ability) does not abort, and the point callbacks
                    # always return 1 (§2 step 5), so a point order never aborts either.
                    if (getattr(target, 'tname', '') == 'unit'
                            and oid in (O_SMART, O_MOVE, O_AIMOVE, O_PATROL, 851970)
                            and not N.eng.order_unit_target_ok(u, target, oid)):
                        break
        if x is not None and target is None and len(taken) > 1 and N.eng.pab:
            N.eng.cluster_form(taken, oid, r32(x), r32(y))        # STEER-3: one group order = a cluster (formation rows)
    return ok


@nat('GroupImmediateOrder')
def _gimm(N, g, order):
    return _group_order(N, g, ORDER_IDS.get(order, 0))


@nat('GroupImmediateOrderById')
def _gimmid(N, g, oid):
    return _group_order(N, g, oid)


@nat('GroupPointOrder')
def _gpt(N, g, order, x, y):
    return _group_order(N, g, ORDER_IDS.get(order, 0), x, y)


@nat('GroupPointOrderById')
def _gptid(N, g, oid, x, y):
    return _group_order(N, g, oid, x, y)


@nat('GroupPointOrderLoc')
def _gptloc(N, g, order, l):
    lo = N.o(l, Location)
    return _group_order(N, g, ORDER_IDS.get(order, 0), lo.x, lo.y) if lo else False


@nat('GroupPointOrderByIdLoc')
def _gptidloc(N, g, oid, l):
    lo = N.o(l, Location)
    return _group_order(N, g, oid, lo.x, lo.y) if lo else False


@nat('GroupTargetOrder')
def _gtgt(N, g, order, t):
    tu = N.o(t, Unit)
    return _group_order(N, g, ORDER_IDS.get(order, 0), target=tu) if tu else False


@nat('GroupTargetOrderById')
def _gtgtid(N, g, oid, t):
    tu = N.o(t, Unit)
    return _group_order(N, g, oid, target=tu) if tu else False


@nat('ForGroup')
def _forgroup(N, g, code):
    """T§5: copy first; per unit that still exists: push GetEnumUnit, new thread + own budget, pop."""
    go = N.o(g, Group)
    if not go or not code:
        return
    copy = list(go.units)
    hids = [N.H(u) if u.alive else 0 for u in copy]
    for v in hids:                # the copy add-refs each member
        N.vm.H.addref(v)
    try:
        for u, hid in zip(copy, hids):
            if not u.alive:
                continue
            N.vm.run_callback(code, 'enum', V.CTX_ENUM_UNIT, hid)
    finally:
        for v in hids:
            N.vm.H.release(v)


@nat('FirstOfGroup')
def _firstofgroup(N, g):
    go = N.o(g, Group)
    if go and go.units:
        u = go.units[0]
        return u if u.alive else None          # alive = object liveness; a game-dead unit is returned
    return None


# ---- context getters (T§4)

def _ctx(slot):
    def f(N):
        return N.vm.ctx_top(slot)
    return f


def _filter_unit(N):
    """CFilterFunc §0x6f2aa030 / GetFilterUnit: the current candidate inside a filter, else the unit of the last
    filter evaluation (the engine's one filter object per code keeps it readable between two enumerations)."""
    v = N.vm.ctx_top(V.CTX_FILTER_UNIT)
    return v if v else N.vm.last_filter_unit


REG['GetTriggerUnit'] = _ctx(V.CTX_TRIGGER_UNIT)
REG['GetTriggerWidget'] = _ctx(V.CTX_TRIGGER_WIDGET)
REG['GetTriggeringTrigger'] = _ctx(V.CTX_TRIGGER)
REG['GetTriggerPlayer'] = _ctx(V.CTX_TRIGGER_PLAYER)
REG['GetExpiredTimer'] = _ctx(V.CTX_EXPIRED_TIMER)
REG['GetTriggerEventId'] = _ctx(V.CTX_EVENT_ID)
REG['GetFilterUnit'] = _filter_unit
REG['GetFilterPlayer'] = _ctx(V.CTX_FILTER_PLAYER)
REG['GetFilterDestructable'] = _ctx(V.CTX_FILTER_DESTR)
REG['GetEnumUnit'] = _ctx(V.CTX_ENUM_UNIT)
REG['GetEnumPlayer'] = _ctx(V.CTX_ENUM_PLAYER)
REG['GetEnumDestructable'] = _ctx(V.CTX_ENUM_DESTR)
REG['GetEnumItem'] = _ctx(V.CTX_ENUM_ITEM)
REG['GetFilterItem'] = _ctx(V.CTX_FILTER_ITEM)


def _evd(key, default=0):
    def f(N):
        return N.vm.ev(key, default)
    return f


for _n, _k in (('GetEnteringUnit', 'entering'), ('GetLeavingUnit', 'leaving'), ('GetDyingUnit', 'dying'),
               ('GetKillingUnit', 'killing'), ('GetOrderedUnit', 'ordered'), ('GetIssuedOrderId', 'order'),
               ('GetOrderTarget', 'otarget'), ('GetOrderTargetUnit', 'otarget'), ('GetAttacker', 'attacker'),
               ('GetTrainedUnit', 'trained'), ('GetTrainedUnitType', 'trainedtype'),
               ('GetConstructedStructure', 'constructed'), ('GetSummonedUnit', 'summoned'),
               ('GetSummoningUnit', 'summoner'), ('GetSpellAbilityUnit', 'caster'),
               ('GetSpellAbilityId', 'ability'), ('GetSpellTargetUnit', 'spelltarget'),
               ('GetResearched', 'research'), ('GetResearchingUnit', 'researcher'),
               ('GetManipulatedItem', 'item'), ('GetManipulatingUnit', 'manipulator'),
               ('GetSoldItem', 'solditem'), ('GetSellingUnit', 'seller'), ('GetBuyingUnit', 'buyer'),
               ('GetSoldUnit', 'soldunit'), ('GetRescuer', 'rescuer'), ('GetDecayingUnit', 'decaying'),
               ('GetCancelledStructure', 'cancelled'), ('GetConstructingStructure', 'constructing'),
               ('GetEventDamage', 'damage'), ('GetEventDamageSource', 'damagesource'),
               ('GetClickedButton', 'button'), ('GetClickedDialog', 'dialog'),
               ('GetEventPlayerState', 'pstate'), ('GetEventGameState', 'gstate'),
               ('GetEventDetectingPlayer', 'detecting'), ('GetEventTargetUnit', 'etarget'),
               ('GetLevelingUnit', 'leveling'), ('GetLearningUnit', 'learning'),
               ('GetLearnedSkill', 'skill'), ('GetRevivingUnit', 'reviving'), ('GetTransportUnit', 'transport'),
               ('GetLoadedUnit', 'loaded'), ('GetChangingUnit', 'changing'), ('GetSpellTargetItem', 'sitem'),
               ('GetSpellTargetDestructable', 'sdestr'), ('GetEventUnitState', 'ustate'),
               ('GetTournamentFinishSoonTimeRemaining', 'tfs')):
    REG[_n] = _evd(_k)
REG['GetOrderPointX'] = _evd('ox', 0.0)
REG['GetOrderPointY'] = _evd('oy', 0.0)
REG['GetEventPlayerChatString'] = _evd('chat', None)
REG['GetEventPlayerChatStringMatched'] = _evd('chatmatch', None)
REG['GetSpellTargetX'] = _evd('sx', 0.0)
REG['GetLearnedSkillLevel'] = _evd('skilllevel', 0)
REG['GetSpellTargetY'] = _evd('sy', 0.0)


@nat('GetOrderPointLoc')
def _orderloc(N):
    return Location(N.vm.ev('ox', 0.0), N.vm.ev('oy', 0.0))


@nat('GetSpellTargetLoc')
def _spellloc(N):
    return Location(N.vm.ev('sx', 0.0), N.vm.ev('sy', 0.0))


# ---- triggers (T§2)

@nat('CreateTrigger')
def _createtrig(N):
    return Trigger()


@nat('DestroyTrigger')
def _destroytrig(N, t):
    tr = N.o(t, Trigger)
    if tr:
        for reg in tr.events:
            reg.alive = False
        tr.conds = []
        tr.actions = []
        N.vm.H.destroyed(tr)


@nat('ResetTrigger')
def _resettrig(N, t):
    tr = N.o(t, Trigger)
    if tr:
        tr.evalc = tr.execc = 0


@nat('EnableTrigger')
def _enabletrig(N, t):
    tr = N.o(t, Trigger)
    if tr:
        tr.enabled = True


@nat('DisableTrigger')
def _disabletrig(N, t):
    tr = N.o(t, Trigger)
    if tr:
        tr.enabled = False


@nat('IsTriggerEnabled')
def _istrigenabled(N, t):
    tr = N.o(t, Trigger)
    return bool(tr and tr.enabled)


@nat('TriggerWaitOnSleeps')
def _waitonsleeps(N, t, b):
    tr = N.o(t, Trigger)
    if tr:
        tr.wait_on_sleeps = bool(b)


@nat('IsTriggerWaitOnSleeps')
def _iswaitonsleeps(N, t):
    tr = N.o(t, Trigger)
    return bool(tr and tr.wait_on_sleeps)


@nat('GetTriggerEvalCount')
def _evalcount(N, t):
    tr = N.o(t, Trigger)
    return tr.evalc if tr else 0


@nat('GetTriggerExecCount')
def _execcount(N, t):
    tr = N.o(t, Trigger)
    return tr.execc if tr else 0


@nat('Condition', 'Filter')
def _condition(N, code):
    """BUGS W9-1 [obs 0x6f2b3410 = 0x6f2b3580 -> 0x6f32b940]: one object per function (a cache by code id) while it
    lives, so Condition and Filter of the same function give the same handle; after a Destroy* a new one."""
    if not code:
        return None
    cache = N.__dict__.setdefault('cond_cache', {})
    c = cache.get(code)
    if c is None or not c.alive:
        c = BoolExpr('cond', code)
        cache[code] = c
    return c


@nat('And')
def _and(N, a, b):
    return BoolExpr('and', 0, _bx(N, a), _bx(N, b))


@nat('Or')
def _or(N, a, b):
    return BoolExpr('or', 0, _bx(N, a), _bx(N, b))


@nat('Not')
def _not(N, a):
    return BoolExpr('not', 0, _bx(N, a))


@nat('DestroyBoolExpr', 'DestroyCondition', 'DestroyFilter')
def _destroybx(N, b):
    bx = N.o(b, BoolExpr)
    if bx:
        N.vm.H.destroyed(bx)


@nat('TriggerAddCondition')
def _addcond(N, t, b):
    tr = N.o(t, Trigger)
    if not tr:
        return None
    c = TriggerCondition(_bx(N, b))
    tr.conds.append(c)
    return c


@nat('TriggerRemoveCondition')
def _remcond(N, t, c):
    tr, co = N.o(t, Trigger), N.o(c, TriggerCondition)
    if tr and co and co in tr.conds:
        tr.conds.remove(co)
        N.vm.H.destroyed(co)


@nat('TriggerClearConditions')
def _clrcond(N, t):
    tr = N.o(t, Trigger)
    if tr:
        # CTriggerWar3 §19 [obs]: clearing drains the list and frees each node; a stored condition handle is
        # detached (RemoveCondition after a clear is a no-op).
        for c in tr.conds:
            N.vm.H.destroyed(c)
        tr.conds = []


@nat('TriggerAddAction')
def _addaction(N, t, code):
    tr = N.o(t, Trigger)
    if not tr:
        return None
    a = TriggerAction(code)
    tr.actions.append(a)              # list mode 2 = tail
    return a


@nat('TriggerRemoveAction')
def _remaction(N, t, a):
    tr, ao = N.o(t, Trigger), N.o(a, TriggerAction)
    if tr and ao:
        if ao in tr.actions:
            tr.actions.remove(ao)
        N.vm.H.destroyed(ao)          # running snapshots skip a destroyed action (T§2)


@nat('TriggerClearActions')
def _clraction(N, t):
    tr = N.o(t, Trigger)
    if tr:
        # CTriggerWar3 §19 [obs]: the list and its nodes are freed, so a stored action handle is detached.
        for a in tr.actions:
            N.vm.H.destroyed(a)
        tr.actions = []               # running executions keep their snapshot


@nat('TriggerSleepAction')
def _sleep(N, t):
    th = N.vm.cur_thread()
    if th is not None:
        th.wait_req = t


@nat('TriggerWaitForSound')
def _waitsound(N, s, offset):
    so = N.o(s)
    th = N.vm.cur_thread()
    dur = getattr(so, 'duration', 0) / 1000.0 if so is not None else 0.0
    if th is not None:
        th.wait_req = max(0.0, dur - offset)


@nat('TriggerSyncStart', 'TriggerSyncReady')
def _sync(N):
    return None                       # single machine: no sync wait (Open)


@nat('TriggerEvaluate')
def _trigeval(N, t):
    tr = N.o(t, Trigger)
    return N.vm.trigger_evaluate(tr) if tr else False


@nat('TriggerExecute')
def _trigexec(N, t):
    tr = N.o(t, Trigger)
    if tr:
        N.vm.trigger_execute(tr, False)


@nat('TriggerExecuteWait')
def _trigexecwait(N, t):
    tr = N.o(t, Trigger)
    if tr:
        N.vm.trigger_execute(tr, True)


@nat('ExecuteFunc')
def _execfunc(N, name):
    N.vm.execute_func(name)


def _reg(N, t, kind, ev, filt=None, src=None):
    tr = N.o(t, Trigger)
    if not tr:
        return None
    reg = EventReg(tr, kind, ev, filt, src)
    tr.events.append(reg)
    return reg


@nat('TriggerRegisterTimerEvent')
def _regtimer(N, t, timeout, periodic):
    reg = _reg(N, t, 'timer', N.ev_id('EVENT_GAME_TIMER_EXPIRED'))
    if reg is None:
        return None
    tm = Timer()
    tm.internal = True                # the reg's own timer, never seen by JASS (Open: GetExpiredTimer)
    tm.regs.append(reg)
    N.vm.timer_start(tm, timeout, bool(periodic), 0)
    reg.extra = tm
    return reg


# W9 variable events (NOTES_triggers §8): the limit compare [obs 0x6f2aad50] on float32; both EQUAL and NOT_EQUAL
# use |v − limit| against the 0.001 constant (CFloatEventReg §5 [obs 0x6f334f50 / 0x6f436760, 0x6f84ca90])
VAR_EQ_EPS = r32(0.0010000000474974513)
VAR_NE_EPS = r32(0.0010000000474974513)
US_EQ_EPS = r32(2.0e-7)                 # PORT9-RestGameplay: a unit-state EQUAL listener uses the 2e-7 epsilon
                                        # (CUnitStateEventReg §10 [obs 0x6f06ee20 / 0x6f84c980]), not the float
                                        # variable event's 0.001 (CFloatEventReg §5 [obs 0x6f84ca90])


def limit_true(op, v, lim, is_int=False, eq_eps=VAR_EQ_EPS):
    if not isinstance(v, float):                  # the store passes the raw 32 bits: an integer reads as a float
        v = struct.unpack('<f', struct.pack('<i', ((int(v or 0) + 0x80000000) & 0xFFFFFFFF) - 0x80000000))[0]
    if op == 0:
        return v < lim
    if op == 1:
        return v <= lim
    if op == 3:
        return v >= lim
    if op == 4:
        return v > lim
    d = abs(r32(v - lim))
    if op == 2:
        return d < eq_eps
    if op == 5:
        # CIntegerEventReg §5 [obs 0x6f3358c0]: NOT_EQUAL on an integer channel short-circuits to always true.
        return True if is_int else not (d < VAR_NE_EPS)
    return False


@nat('TriggerRegisterVariableEvent')
def _regvarevent(N, t, name, op, value):
    """[obs 0x6f2b7740 -> 0x6f3381f0] a registration on the variable NAME (matched case-insensitively by the
    store callback, Storm SStrCmpI [obs 0x6f32a5c0]) with limitop and limit; A-W9-4: globals only (a store to a
    local of that name would also match in the game), no check that the variable exists."""
    reg = _reg(N, t, 'variable', N.ev_id('EVENT_GAME_VARIABLE_LIMIT'))
    if reg is None:
        return None
    reg.extra = (int(op or 0), r32(value))
    key = (name or '').lower()
    for gi, g in enumerate(N.vm.p.globals):
        if g['name'].lower() == key and not g.get('array'):
            N.vm.var_watch.setdefault(gi, []).append(reg)
    return reg


@nat('TriggerRegisterTimerExpireEvent')
def _regtimerexpire(N, t, tmh):
    tm = N.o(tmh, Timer)
    reg = _reg(N, t, 'timerexpire', N.ev_id('EVENT_GAME_TIMER_EXPIRED'), src=tm)
    if reg is not None and tm is not None:
        tm.regs.append(reg)
    return reg


@nat('TriggerRegisterEnterRegion')
def _regenter(N, t, rg, filt):
    rgo = N.o(rg, Region)
    reg = _reg(N, t, 'enter', N.ev_id('EVENT_GAME_ENTER_REGION'), _bx(N, filt), rgo)
    if reg is not None and rgo is not None:
        rgo.enter_regs.append(reg)
    return reg


@nat('TriggerRegisterLeaveRegion')
def _regleave(N, t, rg, filt):
    rgo = N.o(rg, Region)
    reg = _reg(N, t, 'leave', N.ev_id('EVENT_GAME_LEAVE_REGION'), _bx(N, filt), rgo)
    if reg is not None and rgo is not None:
        rgo.leave_regs.append(reg)
    return reg


@nat('TriggerRegisterPlayerUnitEvent')
def _regpunit(N, t, p, ev, filt):
    pl = N.o(p, Player)
    reg = _reg(N, t, 'playerunit', ev, _bx(N, filt), pl)
    if reg is not None and pl is not None:
        pl.unit_regs.setdefault(ev, []).append(reg)
    return reg


@nat('TriggerRegisterUnitEvent')
def _regunit(N, t, u, ev):
    uo = N.o(u, Unit)
    reg = _reg(N, t, 'unit', ev, None, uo)
    if reg is not None and uo is not None:
        uo.unit_regs.setdefault(ev, []).append(reg)
    return reg


@nat('TriggerRegisterFilterUnitEvent')
def _regfunit(N, t, u, ev, filt):
    uo = N.o(u, Unit)
    reg = _reg(N, t, 'unit', ev, _bx(N, filt), uo)
    if reg is not None and uo is not None:
        uo.unit_regs.setdefault(ev, []).append(reg)
    return reg


@nat('TriggerRegisterPlayerStateEvent')
def _regpstate(N, t, p, state, op, value):
    pl = N.o(p, Player)
    reg = _reg(N, t, 'pstate', N.ev_id('EVENT_PLAYER_STATE_LIMIT'), None, pl)
    if reg is not None and pl is not None:
        reg.extra = [state, op, value, N._limit_ok(pl.state.get(state, 0), op, value)]
        pl.player_regs.setdefault('state', []).append(reg)
    return reg


@nat('TriggerRegisterPlayerEvent',
     'TriggerRegisterPlayerAllianceChange', 'TriggerRegisterGameEvent',
     'TriggerRegisterTrackableHitEvent',
     'TriggerRegisterTrackableTrackEvent', 'TriggerRegisterStateEvent')
def _regother(N, t, *a):
    # registered but never raised by the mini world (no player input, no combat) (Open)
    return _reg(N, t, 'other', 0, None, None)


@nat('TriggerRegisterGameStateEvent')
def _reggstate(N, t, s, op, lim):
    """PORT-GameState (CGameState_p1 0x80262 time of day / 0x80268 integer+float variables): the limit compare is
    edge-triggered, GetEventGameState = the state id. The event is EVENT_GAME_STATE_LIMIT (common.j id 3); the
    binary dispatches it per changed state (0x6f32a8a0) [obs; the JASS id mapping is ext common.j]."""
    reg = _reg(N, t, 'gstate', N.ev_id('EVENT_GAME_STATE_LIMIT'), None, None)
    if reg is not None:
        reg.extra = [s, op, lim, _gstate_limit_ok(op, _gstate_value(N, s), lim)]
        N.gstate_regs.append(reg)
    return reg


# ---- timers (T§6)

@nat('CreateTimer')
def _createtimer(N):
    return Timer()


@nat('DestroyTimer')
def _destroytimer(N, t):
    tm = N.o(t, Timer)
    if tm:
        if tm.node is not None:
            tm.node[3] = False
        tm.running = False
        N.vm.H.destroyed(tm)


@nat('TimerStart')
def _timerstart(N, t, timeout, periodic, code):
    tm = N.o(t, Timer)
    if tm:
        N.vm.timer_start(tm, timeout, bool(periodic), code)


@nat('TimerGetRemaining')
def _timerrem(N, t):
    tm = N.o(t, Timer)
    return N.vm.timer_remaining(tm) if tm else 0.0


@nat('TimerGetElapsed')
def _timerelapsed(N, t):
    tm = N.o(t, Timer)
    if not tm:
        return 0.0
    if tm.running:
        # The record's elapsed is measured from its clamped start, so a stored timeout below the 0.0001 s
        # clamp reads 0, not the negative timeout − remaining (CTimerWar3 §10, PORT10-Game).
        e = tm.timeout - N.vm.timer_remaining(tm)
        return e if e > 0 else 0.0
    return tm.timeout - tm.saved_remaining       # Open: not running


@nat('TimerGetTimeout')
def _timertimeout(N, t):
    tm = N.o(t, Timer)
    return tm.timeout if tm else 0.0


@nat('PauseTimer')
def _pausetimer(N, t):
    tm = N.o(t, Timer)
    if tm:
        N.vm.timer_pause(tm)


@nat('ResumeTimer')
def _resumetimer(N, t):
    tm = N.o(t, Timer)
    if tm:
        N.vm.timer_resume(tm)


@nat('CreateTimerDialog')
def _createtd(N, t):
    return Simple('timerdialog', timer=t, title='', shown=False)


@nat('TimerDialogSetTitle')
def _tdtitle(N, d, s):
    o = N.o(d)
    if o is not None:
        o.title = s


@nat('TimerDialogDisplay')
def _tddisplay(N, d, b):
    o = N.o(d)
    if o is not None:
        o.shown = bool(b)


@nat('IsTimerDialogDisplayed')
def _tdshown(N, d):
    o = N.o(d)
    return bool(o is not None and getattr(o, 'shown', False))


@nat('TimerDialogSetTitleColor', 'TimerDialogSetTimeColor', 'TimerDialogSetSpeed', 'TimerDialogSetRealTimeRemaining')
def _tdnoop(N, *a):
    return None


# ---- leaderboards (recorded)

@nat('CreateLeaderboard')
def _createlb(N):
    return Leaderboard()


@nat('LeaderboardDisplay')
def _lbdisplay(N, lb, b):
    o = N.o(lb, Leaderboard)
    if o:
        o.displayed = bool(b)


@nat('IsLeaderboardDisplayed')
def _lbshown(N, lb):
    o = N.o(lb, Leaderboard)
    return bool(o and o.displayed)


@nat('LeaderboardGetItemCount')
def _lbcount(N, lb):
    o = N.o(lb, Leaderboard)
    return len(o.items) if o else 0


@nat('LeaderboardSetSizeByItemCount')
def _lbsize(N, lb, n):
    o = N.o(lb, Leaderboard)
    if o:
        o.size = n


@nat('LeaderboardAddItem')
def _lbadd(N, lb, label, value, p):
    o = N.o(lb, Leaderboard)
    if o and N.o(p, Player):         # a null / invalid player aborts the call (0044 §3 [obs 0x6f2d99b0])
        o.items.append([label, value, p])


@nat('LeaderboardRemoveItem')
def _lbremove(N, lb, i):
    o = N.o(lb, Leaderboard)
    if o and 0 <= i < len(o.items):
        del o.items[i]


@nat('LeaderboardRemovePlayerItem')
def _lbremovep(N, lb, p):
    o = N.o(lb, Leaderboard)
    if o and N.o(p, Player):         # the FIRST row of that player only (0044 §5 [obs 0x6f142a40])
        for i, it in enumerate(o.items):
            if it[2] == p:
                del o.items[i]
                break


@nat('LeaderboardClear')
def _lbclear(N, lb):
    o = N.o(lb, Leaderboard)
    if o:
        o.items = []


def _lb_qsort(items, key, asc):
    """PORT5-NativesB round 2, 0044 §6 [obs 0x6f142730]: the engine sorts the item pointers with the C runtime qsort, which
    is not stable. For up to 8 items the CRT's insertion stage is a selection sort: the strictly greatest element by the
    comparator (the first one on a tie) is swapped to the end, then the range shrinks [inf: the CRT flavour; the swap
    order decides ties]. Larger lists keep the stable sort (the CRT quicksort partition is not reproduced, open)."""
    n = len(items)
    if n > 8:
        items.sort(key=key, reverse=not asc)
        return
    hi = n - 1
    while hi > 0:
        mx = 0
        for i in range(1, hi + 1):
            a, b = key(items[i]), key(items[mx])
            if (a > b) if asc else (a < b):          # comparator > 0: the descending one swaps its arguments
                mx = i
        items[mx], items[hi] = items[hi], items[mx]
        hi -= 1


@nat('LeaderboardSortItemsByValue')
def _lbsortv(N, lb, asc):
    o = N.o(lb, Leaderboard)
    if o:
        _lb_qsort(o.items, lambda it: it[1], asc)


@nat('LeaderboardSortItemsByPlayer')
def _lbsortp(N, lb, asc):
    o = N.o(lb, Leaderboard)
    if o:
        _lb_qsort(o.items, lambda it: _getpid(N, it[2]), asc)


@nat('LeaderboardSortItemsByLabel')
def _lbsortl(N, lb, asc):
    o = N.o(lb, Leaderboard)
    if o:
        _lb_qsort(o.items, lambda it: it[0] or '', asc)


@nat('LeaderboardHasPlayerItem')
def _lbhas(N, lb, p):
    o = N.o(lb, Leaderboard)
    return bool(o and N.o(p, Player) and any(it[2] == p for it in o.items))


@nat('LeaderboardGetPlayerIndex')
def _lbindex(N, lb, p):
    o = N.o(lb, Leaderboard)
    if o and N.o(p, Player):
        for i, it in enumerate(o.items):
            if it[2] == p:
                return i
    return -1


@nat('LeaderboardSetLabel')
def _lblabel(N, lb, s):
    o = N.o(lb, Leaderboard)
    if o:
        o.label = s


@nat('LeaderboardGetLabelText')
def _lbgetlabel(N, lb):
    o = N.o(lb, Leaderboard)
    return o.label if o else ''     # empty string for a bad handle (0045 [obs 0x6f2da660])


@nat('LeaderboardSetItemValue')
def _lbitemval(N, lb, i, v):
    o = N.o(lb, Leaderboard)
    if o and 0 <= i < len(o.items):
        o.items[i][1] = v


@nat('LeaderboardSetItemLabel')
def _lbitemlabel(N, lb, i, s):
    o = N.o(lb, Leaderboard)
    if o and 0 <= i < len(o.items):
        o.items[i][0] = s


@nat('PlayerSetLeaderboard')
def _psetlb(N, p, lb):
    pl = N.o(p, Player)
    if pl:
        pl.leaderboard = lb


@nat('PlayerGetLeaderboard')
def _pgetlb(N, p):
    pl = N.o(p, Player)
    return pl.leaderboard if pl and pl.leaderboard else 0


@nat('LeaderboardSetStyle', 'LeaderboardSetLabelColor', 'LeaderboardSetValueColor', 'LeaderboardSetItemStyle',
     'LeaderboardSetItemLabelColor', 'LeaderboardSetItemValueColor')
def _lbnoop(N, *a):
    return None


# ---- multiboards (recorded)

MB_COLS = 32                        # cells are kept per (row, column < 32) (td_plan T6; display only)


@nat('CreateMultiboard')
def _createmb(N):
    o = Simple('multiboard', rows=0, cols=0, title='', shown=False, minimized=False, tcolor=-1, cells={})
    N.multiboards.append(o)
    return o


def _argb(r, g, b, a):
    c = ((a & 255) << 24) | ((r & 255) << 16) | ((g & 255) << 8) | (b & 255)
    return c - (1 << 32) if c >= (1 << 31) else c


def _mbcell(mb, r, c):
    """the cell record of (r, c): value, argb, icon, style bits (1 value, 2 icon), width (0 = default)."""
    k = (r, c)
    cell = mb.cells.get(k)
    if cell is None:
        cell = mb.cells[k] = ['', -1, '', 3, 0.0]
    return cell


def multiboard_lines(N):
    """the live multiboards and their set cells, as compared by the harness (td_plan T6)."""
    out = []
    for mb in N.multiboards:
        out.append('MB %s|%d|%d|%d|%d|%d' % (mb.title, mb.rows, mb.cols, 1 if mb.shown else 0,
                                            1 if mb.minimized else 0, mb.tcolor))
        for (r, c) in sorted(mb.cells):
            v, col, ic, st, w = mb.cells[(r, c)]
            out.append('C %d %d|%s|%d|%s|%d|%s' % (r, c, v, col, ic, st, fmt_real(w)))
    return out


def _mbo(N, h):
    o = N.o(h)
    return o if isinstance(o, Simple) and o.tname == 'multiboard' else None


def _mbi(N, h):
    """multiboarditem -> its cell (None when the item / board is gone or out of range)."""
    it = N.o(h)
    if not isinstance(it, Simple) or it.tname != 'multiboarditem':
        return None
    mb = N.o(getattr(it, 'mb', 0))
    if not isinstance(mb, Simple) or mb.tname != 'multiboard' or not (0 <= it.r and 0 <= it.c < MB_COLS):
        return None
    return _mbcell(mb, it.r, it.c)


@nat('MultiboardDisplay')
def _mbdisplay(N, mb, f):
    o = _mbo(N, mb)
    if o is not None:
        o.shown = bool(f)
        N.mb_current = o if f else None      # 0046 §2.2 [obs 0x6f3e0830]: the registration is single valued (display only)


@nat('MultiboardMinimize')
def _mbminimize(N, mb, f):
    o = _mbo(N, mb)
    if o is not None:
        o.minimized = bool(f)


@nat('MultiboardSetTitleText')
def _mbtitle(N, mb, s):
    o = _mbo(N, mb)
    if o is not None:
        o.title = s or ''


@nat('MultiboardGetTitleText')
def _mbgettitle(N, mb):
    o = _mbo(N, mb)
    return o.title if o is not None else None


@nat('MultiboardSetTitleTextColor')
def _mbtcolor(N, mb, r, g, b, a):
    o = _mbo(N, mb)
    if o is not None:
        o.tcolor = _argb(r, g, b, a)


@nat('MultiboardClear')
def _mbclear(N, mb):
    o = _mbo(N, mb)
    if o is not None:
        o.cells = {}                 # 0046 §3.1 [obs 0x6f2db840]: rows and columns are set to 0, the grid is rebuilt empty
        o.rows = 0
        o.cols = 0


def _mball(N, mb, fn):
    o = _mbo(N, mb)
    if o is None:
        return
    for r in range(max(0, o.rows)):
        for c in range(min(max(0, o.cols), MB_COLS)):
            fn(_mbcell(o, r, c))


@nat('MultiboardSetItemsStyle')
def _mbistyle(N, mb, sv, si):
    _mball(N, mb, lambda c: c.__setitem__(3, (1 if sv else 0) | (2 if si else 0)))


@nat('MultiboardSetItemsValue')
def _mbivalue(N, mb, s):
    _mball(N, mb, lambda c: c.__setitem__(0, s or ''))


@nat('MultiboardSetItemsValueColor')
def _mbicolor(N, mb, r, g, b, a):
    v = _argb(r, g, b, a)
    _mball(N, mb, lambda c: c.__setitem__(1, v))


@nat('MultiboardSetItemsWidth')
def _mbiwidth(N, mb, w):
    _mball(N, mb, lambda c: c.__setitem__(4, w))


@nat('MultiboardSetItemsIcon')
def _mbiicon(N, mb, s):
    _mball(N, mb, lambda c: c.__setitem__(2, s or ''))


@nat('MultiboardSetItemStyle')
def _mbstyle(N, it, sv, si):
    c = _mbi(N, it)
    if c is not None:
        c[3] = (1 if sv else 0) | (2 if si else 0)


@nat('MultiboardSetItemValue')
def _mbvalue(N, it, s):
    c = _mbi(N, it)
    if c is not None:
        c[0] = s or ''


@nat('MultiboardSetItemValueColor')
def _mbcolor(N, it, r, g, b, a):
    c = _mbi(N, it)
    if c is not None:
        c[1] = _argb(r, g, b, a)


@nat('MultiboardSetItemWidth')
def _mbwidth(N, it, w):
    c = _mbi(N, it)
    if c is not None:
        c[4] = w


@nat('MultiboardSetItemIcon')
def _mbicon(N, it, s):
    c = _mbi(N, it)
    if c is not None:
        c[2] = s or ''


@nat('MultiboardSetRowCount')
def _mbrows(N, mb, n):
    o = N.o(mb)
    if o is not None:
        o.rows = n


@nat('MultiboardSetColumnCount')
def _mbcols(N, mb, n):
    o = N.o(mb)
    if o is not None:
        o.cols = n


@nat('MultiboardGetRowCount')
def _mbgetrows(N, mb):
    o = N.o(mb)
    return getattr(o, 'rows', 0) if o is not None else 0


@nat('MultiboardGetColumnCount')
def _mbgetcols(N, mb):
    o = N.o(mb)
    return getattr(o, 'cols', 0) if o is not None else 0


@nat('MultiboardReleaseItem')
def _mbrelease(N, it):
    """BUGS W9-1 [obs 0x6f2dc5a0]: the item object is destroyed (its handle is freed once unreferenced)."""
    o = N.o(it)
    if isinstance(o, Simple) and o.tname == 'multiboarditem':
        N.vm.H.destroyed(o)


@nat('MultiboardGetItem')
def _mbitem(N, mb, r, c):
    if _mbo(N, mb) is None:          # null / wrong-type board -> null handle (0047 §3.1 [obs 0x6f2dc460]); row / column unchecked
        return 0
    return Simple('multiboarditem', r=r, c=c, mb=mb)


@nat('MultiboardSuppressDisplay')
def _mbsuppress(N, f):
    """0046 §2.1 [obs 0x6f2db2c0]: a game-wide switch that hides every multiboard; a plain boolean store, display only
    (the per-board flags the harness compares stay as they are)."""
    N.mb_suppress = bool(f)


# ---- dialogs (NOTES_skibi §1): DialogDisplay is local UI only; a click is player input (engine kind 4)

DLG_CLICK_EVENT = 92                # the game reports 0x8025c − 0x80200 = 92 (common.j: 91) [obs NOTES_skibi §1.4]
DLG_BUTTON_EVENT = 90               # EVENT_DIALOG_BUTTON_CLICK
DLG_MAX_BUTTONS = 12                # the UI reports clicks of buttons 0…11 only [obs]


@nat('DialogCreate')
def _dialogcreate(N):
    return Simple('dialog', buttons=[], msg='', regs=[])


@nat('DialogClear')
def _dialogclear(N, d):
    o = N.o(d)
    if o is not None:
        o.buttons = []


@nat('DialogSetMessage')
def _dialogmsg(N, d, s):
    o = N.o(d)
    if o is not None:
        o.msg = s


@nat('DialogAddButton')
def _dialogbutton(N, d, s, *rest):
    o = N.o(d)
    b = Simple('button', text=s, quit=False, regs=[])
    if o is not None and hasattr(o, 'buttons'):    # another handle type has no button list (was AttributeError)
        o.buttons.append(b)
    return b


@nat('DialogAddQuitButton')
def _dialogquitbutton(N, d, score, s, *rest):
    b = _dialogbutton(N, d, s)
    b.quit = True                                  # leaves the game, no dialog events [obs]
    return b


def dialog_click(N, player, d, idx):
    """NOTES_skibi §1.3: the dialog's EVENT_DIALOG_CLICK registrations (dialog + button), then the button's
    EVENT_DIALOG_BUTTON_CLICK registrations (button only); a cleared / destroyed dialog or button is ignored.
    -> True if the click was valid."""
    o = N.o(d)
    if o is None or getattr(o, 'tname', '') != 'dialog' or not 0 <= idx < min(len(o.buttons), DLG_MAX_BUTTONS):
        return False
    b = o.buttons[idx]
    pid = getattr(player, 'id', -1)
    if N.hud_dlg.get(pid) == d:      # DBZ-DIALOG (NOTES_skibi 1.3 step 1): the clicked dialog hides itself in the clicking player's UI
        del N.hud_dlg[pid]
    if b.quit:
        return False
    vm, hp, hd, hb = N.vm, N.H(player), N.H(o), N.H(b)
    for regs, ev, data in ((o.regs, DLG_CLICK_EVENT, {'dialog': hd, 'button': hb}),
                           (b.regs, DLG_BUTTON_EVENT, {'button': hb})):
        for reg in list(regs):
            if reg.alive and reg.trig.alive:
                evd = dict(data)
                evd['event'] = ev
                vm.fire(reg.trig, {V.CTX_TRIGGER_PLAYER: hp, V.CTX_EVENT_ID: ev}, evd)
    return True


@nat('TriggerRegisterDialogEvent')
def _regdialog(N, t, d):
    o = N.o(d)
    if o is None or getattr(o, 'tname', '') != 'dialog':
        return None
    reg = _reg(N, t, 'dialog', DLG_CLICK_EVENT)
    if reg is not None:
        o.regs.append(reg)
    return reg


@nat('TriggerRegisterDialogButtonEvent')
def _regdialogbutton(N, t, b):
    o = N.o(b)
    if o is None or getattr(o, 'tname', '') != 'button':
        return None
    reg = _reg(N, t, 'button', DLG_BUTTON_EVENT)
    if reg is not None:
        o.regs.append(reg)
    return reg


@nat('DialogDisplay')
def _dialogdisplay(N, p, d, b):
    pl = N.o(p)
    pid = getattr(pl, 'id', -1)
    if pid >= 0:                     # DBZ-DIALOG: what the player's UI shows (C#: W3Hud.HudDialog)
        if b:
            N.hud_dlg[pid] = d
        else:
            N.hud_dlg.pop(pid, None)
    N.text('dialog', p, d, b)


# ---- text output (recorded)

@nat('DisplayTextToPlayer')
def _disptext(N, p, x, y, s):
    N.text('text', p, s)


@nat('DisplayTimedTextToPlayer')
def _disptimedtext(N, p, x, y, d, s):
    N.text('text', p, s)


@nat('DisplayTimedTextFromPlayer')
def _dispfromtext(N, p, x, y, d, s):
    N.text('text', p, s)


# ---- W7: Blizzard.j text functions for every player of the force (compiler.BJ_OVERRIDES)


def _force_players(N, f):
    fo = N.o(f, Force)
    return [N.players[i] for i in sorted(fo.players)] if fo else []


@nat('DisplayTextToForce')
def _disptextforce(N, f, s):
    for pl in _force_players(N, f):
        N.text('text', pl, s)


@nat('DisplayTimedTextToForce')
def _disptimedforce(N, f, d, s):
    for pl in _force_players(N, f):
        N.text('text', pl, s)


@nat('ClearTextMessagesBJ')
def _clearforce(N, f):
    for pl in _force_players(N, f):
        N.text('clear', pl)


@nat('QuestMessageBJ')
def _questmsg(N, f, kind, s):
    """bj_QUESTMESSAGE_* 0…12: a blank line first (not for REQUIREMENT = 4), then the message; other types show
    nothing (sounds / quest button flash are presentation stubs)."""
    if not 0 <= kind <= 12:
        return
    for pl in _force_players(N, f):
        if kind != 4:
            N.text('text', pl, ' ')
        N.text('text', pl, s)


@nat('ClearTextMessages')
def _cleartext(N):
    N.text('clear')


# ---- sound (only what scripts read back)

@nat('CreateSound', 'CreateSoundFilenameWithLabel')
def _createsound(N, fname, *a):
    # docs/sound_plan.md S2: the file is kept; StartSound reports it (W3Sound in the world plays it)
    return Simple('sound', duration=0, file=fname or '', x=0.0, y=0.0, pos=False, unit=None, vol=-1, pitch=1.0, ch=-1)


_LABELS = [None]


def sound_labels():
    """the install's sound label table (tools/w3sound.sound_labels, cached) — the pack's WSLB has the same rows."""
    if _LABELS[0] is None:
        try:
            from . import world as JW
            _LABELS[0] = JW.sound_labels()
        except Exception:                      # noqa: BLE001 — no install: no labels
            _LABELS[0] = {}
    return _LABELS[0]


@nat('CreateSoundFromLabel')
def _createsoundlabel(N, label, *a):
    # docs/sound_plan.md: the label's file and volume (UI\\SoundInfo tables)
    f, v = sound_labels().get(label or '', ('', -1))
    return Simple('sound', duration=0, file=f, x=0.0, y=0.0, pos=False, unit=None, vol=v, pitch=1.0, ch=-1)


@nat('CreateMIDISound')
def _createmidi(N, *a):
    return Simple('sound', duration=0, file='', x=0.0, y=0.0, pos=False, unit=None, vol=-1, pitch=1.0, ch=-1)


@nat('SetSoundParamsFromLabel')
def _soundparams(N, s, label):
    o = N.o(s)
    e = sound_labels().get(label or '')
    if o is not None and e is not None:
        o.vol = e[1]                            # the label's volume [ext]; the file stays


@nat('SetSoundPosition')
def _setsoundpos(N, s, x, y, z):
    o = N.o(s)
    if o is not None:
        o.x, o.y, o.pos = x, y, True


@nat('AttachSoundToUnit')
def _attachsound(N, s, u):
    o = N.o(s)
    if o is not None:
        o.unit = u


@nat('SetSoundVolume')
def _setsoundvol(N, s, v):
    o = N.o(s)
    if o is not None:
        o.vol = v


@nat('SetSoundPitch')
def _setsoundpitch(N, s, p):
    o = N.o(s)                                  # A-SN2: the playback rate of the next StartSound [inf: absolute]
    if o is not None:
        o.pitch = r32(p)


@nat('SetSoundChannel')
def _setsoundchannel(N, s, c):
    o = N.o(s)                                  # A-SN2: kept, no audible effect in the world (no channel mixer) [inf]
    if o is not None:
        o.ch = c


SND_QUEUE = 32                                  # the U# queue's capacity (W3Natives SndNative): the rule-test lines


def _sndlog(N, line):
    if len(N.sndlog) < SND_QUEUE:
        N.sndlog.append(line)


@nat('StopSound')
def _stopsound(N, s, kill, fade):
    o = N.o(s)                                  # A-SN2: the world stops what plays this sound; killWhenDone ->
    if o is None or getattr(o, 'tname', '') != 'sound':     # destroyed now (the sound is done) like KillSoundWhenDone
        return
    _sndlog(N, 'stop %d %d %d' % (s, 1 if kill else 0, 1 if fade else 0))
    if kill:
        N.vm.H.destroyed(o)


def sound_lines(N):
    """A-SN2: the sound events (start / stop) as the world's queue holds them (export_rule_tests -> .sounds)."""
    return list(N.sndlog)


class TextTag(Agent):
    """td_plan T4: floating text — display state only (JASS has no text tag getters, the trace is unchanged).
    Lazy: born at game ms t0 (SetTextTagAge moves it), velocity in the natives' screen units per second
    (blizzard.j TextTagSpeed2Velocity: 0.071 = 128 world units / s [ext]); a non-permanent tag is over at age >=
    lifespan and fades from the fadepoint (the presentation hides it; its handle stays until DestroyTextTag)."""
    tname = 'texttag'

    def __init__(self, t0):
        self.text, self.height = '', 0.0
        self.x = self.y = self.z = 0.0
        self.unit = 0                    # handle of the unit it sits on (SetTextTagPosUnit), 0 = at (x, y)
        self.argb = -1                   # 0xAARRGGBB, white opaque
        self.vx = self.vy = 0.0
        self.t0 = t0
        self.life = self.fade = 0.0
        self.permanent, self.visible, self.suspended = True, True, False


def texttag_lines(N):
    """the live text tags as compared by the harness (export_inputs / export_rule_tests -> .texttags)."""
    out = []
    for t in N.texttags:
        u = N.o(t.unit) if t.unit else None
        f = fmt_real
        out.append('%s|%s|%s|%s|%s|%d|%s|%s|%d|%s|%s|%d|%d' % (
            t.text, f(t.x), f(t.y), f(t.z), f(t.height), t.argb, f(t.vx), f(t.vy), t.t0, f(t.life), f(t.fade),
            (1 if t.permanent else 0) | (2 if t.visible else 0) | (4 if t.suspended else 0),
            getattr(u, 'serial', 0) if u is not None else 0))
    return out


class Effect(Agent):
    """td_plan T4: a special effect — display state only (no getters). The model plays birth / stand; DestroyEffect
    plays its death and the presentation drops it after the animation; the VM forgets destroyed effects
    EFFECT_KEEP_MS after their destruction (pruned when the next one is created)."""
    tname = 'effect'

    def __init__(self, path, x, y, unit, attach, t0):
        self.path, self.x, self.y, self.unit, self.attach = path, x, y, unit, attach
        self.t0, self.dead = t0, -1


EFFECT_KEEP_MS = 3000


def _effect(N, path, x, y, unit=0, attach=''):
    now = N.vm.game_ms()
    N.effects = [e for e in N.effects if e.dead < 0 or now - e.dead <= EFFECT_KEEP_MS]
    e = Effect(path or '', x, y, unit, attach or '', now)
    N.effects.append(e)
    return e


def effect_lines(N):
    out = []
    for e in N.effects:
        u = N.o(e.unit) if e.unit else None
        out.append('%s|%s|%s|%d|%s|%d|%d' % (e.path, fmt_real(e.x), fmt_real(e.y),
                                            getattr(u, 'serial', 0) if u is not None else 0, e.attach, e.t0, e.dead))
    return out


@nat('AddSpecialEffect')
def _addeffect(N, path, x, y):
    return _effect(N, path, x, y)


@nat('AddSpecialEffectLoc')
def _addeffectloc(N, path, loc):
    lo = N.o(loc)
    return _effect(N, path, getattr(lo, 'x', 0.0), getattr(lo, 'y', 0.0))


@nat('AddSpecialEffectTarget')
def _addeffecttarget(N, path, w, attach):
    o = N.o(w)
    return _effect(N, path, getattr(o, 'x', 0.0), getattr(o, 'y', 0.0), w if o is not None else 0, attach)


class Quest(Agent):
    """td_plan T4 quest log: title, description, icon, flags; items (description, completed). Defaults A-Q1 [inf]:
    required, discovered, enabled true; completed, failed false."""
    tname = 'quest'

    def __init__(self):
        self.title = self.desc = self.icon = ''
        self.required, self.completed, self.discovered, self.failed, self.enabled = True, False, True, False, True
        self.items = []


class QuestItem(Agent):
    tname = 'questitem'

    def __init__(self, q):
        self.quest, self.desc, self.completed = q, '', False


def quest_lines(N):
    out = []
    for q in N.quests:
        out.append('Q %s|%s|%s|%d' % (q.title, q.desc, q.icon, (1 if q.required else 0) | (2 if q.completed else 0) |
                                      (4 if q.discovered else 0) | (8 if q.failed else 0) | (16 if q.enabled else 0)))
        for it in q.items:
            out.append('I %s|%d' % (it.desc, 1 if it.completed else 0))
    return out


@nat('CreateQuest')
def _createquest(N):
    q = Quest()
    N.quests.append(q)
    return q


def _q(N, h):
    o = N.o(h)
    return o if isinstance(o, Quest) else None


def _qset(attr):
    def f(N, h, v):
        q = _q(N, h)
        if q is not None:
            setattr(q, attr, (v or '') if attr in ('title', 'desc', 'icon') else bool(v))
    return f


for _n, _a in (('QuestSetTitle', 'title'), ('QuestSetDescription', 'desc'), ('QuestSetIconPath', 'icon'),
               ('QuestSetRequired', 'required'), ('QuestSetCompleted', 'completed'),
               ('QuestSetDiscovered', 'discovered'), ('QuestSetFailed', 'failed'), ('QuestSetEnabled', 'enabled')):
    nat(_n)(_qset(_a))


def _qget(attr):
    def f(N, h):
        q = _q(N, h)
        return bool(getattr(q, attr)) if q is not None else False
    return f


for _n, _a in (('IsQuestRequired', 'required'), ('IsQuestCompleted', 'completed'), ('IsQuestDiscovered', 'discovered'),
               ('IsQuestFailed', 'failed'), ('IsQuestEnabled', 'enabled')):
    nat(_n)(_qget(_a))


@nat('QuestCreateItem')
def _questitem(N, h):
    q = _q(N, h)
    if q is None:
        return None
    it = QuestItem(q)
    q.items.append(it)
    return it


def _qi(N, h):
    o = N.o(h)
    return o if isinstance(o, QuestItem) else None


@nat('QuestItemSetDescription')
def _qidesc(N, h, v):
    it = _qi(N, h)
    if it is not None:
        it.desc = v or ''


@nat('QuestItemSetCompleted')
def _qicomp(N, h, v):
    it = _qi(N, h)
    if it is not None:
        it.completed = bool(v)


@nat('IsQuestItemCompleted')
def _qiget(N, h):
    it = _qi(N, h)
    return it.completed if it is not None else False


@nat('FlashQuestDialogButton', 'ForceQuestDialogUpdate')
def _questui(N, *a):
    return None


# ---- weather (td_plan T4): display state only (no getters)

def weather_lines(N):
    return ['%s|%s|%s|%s|%s|%d' % (_id4(w.wid), fmt_real(w.minx), fmt_real(w.miny), fmt_real(w.maxx), fmt_real(w.maxy),
                                   1 if w.on else 0) for w in N.weather]


# what the display draws per weather id (W3Weather.Kind; the harness compares the two): by the id's first letter
# [inf: the weather ids the maps pass, grouped by name in the World Editor's weather list [ext]: R… rain, S… snow,
# L… light rays, W… wind; M… (Dalaran shield) and F… (fog) are not drawn]
WK_RAIN, WK_SNOW, WK_RAYS, WK_WIND = 1, 2, 3, 4
WEATHER_KIND = {'R': WK_RAIN, 'S': WK_SNOW, 'L': WK_RAYS, 'W': WK_WIND}


def weather_kind(wid):
    return WEATHER_KIND.get(chr((wid >> 24) & 255), 0)


@nat('AddWeatherEffect')
def _addweather(N, r, wid):
    ro = N.o(r)
    w = Simple('weathereffect', wid=wid, on=False, minx=getattr(ro, 'minx', 0.0), miny=getattr(ro, 'miny', 0.0),
               maxx=getattr(ro, 'maxx', 0.0), maxy=getattr(ro, 'maxy', 0.0))
    N.weather.append(w)
    return w


@nat('EnableWeatherEffect')
def _enweather(N, h, f):
    w = N.o(h)
    if isinstance(w, Simple) and w.tname == 'weathereffect':
        w.on = bool(f)


# ---- the cinematic filter (td_plan T4): one global state; IsCineFilterDisplayed is its only getter

def cine_lines(N):
    c = N.cine
    return ['%s|%d|%d|%s|%s|%d|%d|%s|%d|%d' % (c['tex'], c['blend'], c['flags'], ','.join(fmt_real(v) for v in c['suv']),
                                               ','.join(fmt_real(v) for v in c['euv']), c['sc'], c['ec'],
                                               fmt_real(c['dur']), 1 if c['shown'] else 0, c['t0'])]


@nat('SetCineFilterTexture')
def _cinetex(N, s):
    N.cine['tex'] = s or ''


@nat('SetCineFilterBlendMode')
def _cineblend(N, m):
    N.cine['blend'] = m


@nat('SetCineFilterTexMapFlags')
def _cineflags(N, f):
    N.cine['flags'] = f


@nat('SetCineFilterStartUV')
def _cinesuv(N, a, b, c, d):
    N.cine['suv'] = (a, b, c, d)


@nat('SetCineFilterEndUV')
def _cineeuv(N, a, b, c, d):
    N.cine['euv'] = (a, b, c, d)


@nat('SetCineFilterStartColor')
def _cinesc(N, r, g, b, a):
    N.cine['sc'] = _argb(r, g, b, a)


@nat('SetCineFilterEndColor')
def _cineec(N, r, g, b, a):
    N.cine['ec'] = _argb(r, g, b, a)


@nat('SetCineFilterDuration')
def _cinedur(N, d):
    N.cine['dur'] = d


@nat('DisplayCineFilter')
def _cineshow(N, f):
    N.cine['shown'] = bool(f)
    N.cine['t0'] = N.vm.game_ms()


@nat('IsCineFilterDisplayed')
def _cineis(N):
    return N.cine['shown']


@nat('CreateTextTag')
def _createtexttag(N):
    t = TextTag(N.vm.game_ms())
    N.texttags.append(t)
    return t


def _tt(N, h):
    o = N.o(h)
    return o if isinstance(o, TextTag) else None


@nat('SetTextTagText')
def _ttext(N, h, s, height):
    t = _tt(N, h)
    if t is not None:
        t.text, t.height = s or '', height


@nat('SetTextTagPos')
def _ttpos(N, h, x, y, z):
    t = _tt(N, h)
    if t is not None:
        t.x, t.y, t.z, t.unit = x, y, z, 0


@nat('SetTextTagPosUnit')
def _ttposunit(N, h, u, z):
    t = _tt(N, h)
    v = N.o(u)
    if t is not None and v is not None:
        t.unit, t.z = u, z
        t.x, t.y = v.x, v.y


@nat('SetTextTagColor')
def _ttcolor(N, h, r, g, b, a):
    t = _tt(N, h)
    if t is not None:
        c = ((a & 255) << 24) | ((r & 255) << 16) | ((g & 255) << 8) | (b & 255)
        t.argb = c - (1 << 32) if c >= (1 << 31) else c


@nat('SetTextTagVelocity')
def _ttvel(N, h, vx, vy):
    t = _tt(N, h)
    if t is not None:
        t.vx, t.vy = vx, vy


def _ttset(N, h, attr, f):
    t = _tt(N, h)
    if t is not None:
        setattr(t, attr, bool(f))


@nat('SetTextTagVisibility')
def _ttvis(N, h, f):
    _ttset(N, h, 'visible', f)


@nat('SetTextTagSuspended')
def _ttsusp(N, h, f):
    _ttset(N, h, 'suspended', f)


@nat('SetTextTagPermanent')
def _ttperm(N, h, f):
    _ttset(N, h, 'permanent', f)


@nat('SetTextTagAge')
def _ttage(N, h, age):
    t = _tt(N, h)
    if t is not None:
        t.t0 = N.vm.game_ms() - r2i(age * 1000.0 + 0.5)       # BUGS UDON-THROW: NaN / huge age no exception


@nat('SetTextTagLifespan')
def _ttlife(N, h, v):
    t = _tt(N, h)
    if t is not None:
        t.life = v


@nat('SetTextTagFadepoint')
def _ttfade(N, h, v):
    t = _tt(N, h)
    if t is not None:
        t.fade = v


@nat('StartSound')
def _startsound(N, s):
    o = N.o(s)
    if o is None or not getattr(o, 'file', ''):
        return
    u = N.o(o.unit) if getattr(o, 'unit', None) else None
    x, y = (u.x, u.y) if u is not None else (o.x, o.y)
    N.sounds.append((N.vm.game_ms(), o.file, x, y, 1 if (u is not None or o.pos) else 0,
                     min(1.0, max(0.0, o.vol / 127.0)) if o.vol >= 0 else 1.0))
    f = fmt_real
    _sndlog(N, 'start %d %d %s %s %s %s %d %s' % (
        s, 1 if (u is not None or o.pos) else 0, f(r32(x)), f(r32(y)),
        f(r32(min(1.0, max(0.0, o.vol / 127.0)))) if o.vol >= 0 else '1.0', f(r32(getattr(o, 'pitch', 1.0))),
        getattr(o, 'ch', -1), o.file))


@nat('SetSoundDuration')
def _setsounddur(N, s, d):
    o = N.o(s)
    if o is not None:
        o.duration = d


@nat('GetSoundDuration', 'GetSoundFileDuration')
def _getsounddur(N, s):
    if s is None or isinstance(s, str):          # GetSoundFileDuration(file): no sound data -> 0 (a file name
        return 0                                  # used to reach the handle lookup: TypeError)
    o = N.o(s)
    return getattr(o, 'duration', 0) if o is not None else 0


@nat('GetSoundIsPlaying', 'GetSoundIsLoading')
def _soundplaying(N, s):
    return False


# ---- game cache

def _gc_pack(s):
    """PORT5-NativesB round 2 (CGameCacheManager_p1 §7.1 [obs 0x6f29eb90, 0x6f2ea170]): a cache name and a mission key are
    stored "strip-packed": split on the single space character and concatenated into a 256-byte buffer, so spaces do not
    count and a longer name is cut at 255 characters. The comparison keeps the case [obs; ordinal 509 open]."""
    return (s or '').replace(' ', '')[:255]


def gc_pack(s):
    """PORT6-Game name for _gc_pack (None stays None for the direct test; the natives use _gc_pack)."""
    return None if s is None else _gc_pack(s)


@nat('InitGameCache')
def _initgc(N, name):
    """0038 §7.1 [obs 0x6f2d35f0]: an empty name answers the null handle, so does the reserved campaign cache name
    (only a campaign game may open it); else find-or-create by the packed name."""
    if not name or name == 'Campaigns.w3v':
        return 0
    key = _gc_pack(name)
    if key not in N.caches:
        N.caches[key] = GameCache(key)
        d = N.gc_disk.cache(key)                      # SAVE-2: the file's copy of this cache (docs/save_system.md)
        if d:
            N.caches[key].data = d
    return N.caches[key]


def _gc_store(kind):
    def f(N, c, mk, k, v):
        o = N.o(c, GameCache)
        if o:
            o.data[(kind, _gc_pack(mk), k)] = v
    return f


def _gc_get(kind, zero):
    def f(N, c, mk, k):
        o = N.o(c, GameCache)
        v = o.data.get((kind, _gc_pack(mk), k), zero) if o else zero
        # Unclassed_Native_0039 §4 [obs 0x6f2d4650 / 0x6f3caa40]: a missing string materialises the empty
        # string object, not a null string (a stored null string reads back the same way).
        return '' if kind == 'String' and v is None else v
    return f


def _gc_have(kind):
    def f(N, c, mk, k):
        o = N.o(c, GameCache)
        return bool(o and (kind, _gc_pack(mk), k) in o.data)
    return f


for _kind, _zero in (('Integer', 0), ('Real', 0.0), ('Boolean', 0), ('String', '')):
    REG['Store' + _kind] = _gc_store(_kind)
    REG['GetStored' + _kind] = _gc_get(_kind, _zero)
    REG['HaveStored' + _kind] = _gc_have(_kind)


@nat('FlushGameCache')
def _flushgc(N, c):
    o = N.o(c, GameCache)
    if o:
        o.data.clear()


@nat('FlushStoredMission')
def _flushmission(N, c, mk):
    o = N.o(c, GameCache)
    if o:
        mk = _gc_pack(mk)
        for k in [k for k in o.data if k[1] == mk]:
            del o.data[k]


@nat('SaveGameCache')
def _savegc(N, c):
    """SAVE-2 [obs 0038 §7.2, CGameCacheManager_p1 §10]: a null / wrong handle answers false; else the WHOLE manager (every cache) is
    written; true after a completed write. Size limit: gamecache.BUDGET (false, the file stays as it was)."""
    if N.o(c, GameCache) is None:
        return False
    return N.gc_disk.save(dict((k, v.data) for k, v in N.caches.items()))


from .gamecache import persistable as gamecache_persistable   # noqa: E402


@nat('ReloadGameCachesFromDisk')
def _reloadgc(N):
    """SAVE-2 [obs 0038 §7.3]: every cache's tables are replaced by the file's (empty when it has none: unsaved data is discarded);
    the handles stay valid. The script sees false [inf]: the MMD library of Gem TD / Legion TD reads it as "online game"."""
    if not N.gc_disk.has_map():                       # no file for this map: memory stays as it was (the native used to do nothing)
        return False
    for k, v in N.caches.items():
        if gamecache_persistable(k):
            v.data = N.gc_disk.cache(k) or {}
    return False


@nat('SyncStoredInteger', 'SyncStoredReal', 'SyncStoredBoolean',
     'SyncStoredString', 'SyncStoredUnit', 'StoreUnit', 'RestoreUnit', 'GetStoredUnit')
def _gcnoop(N, *a):
    return None


# ================================================================ W9: 1.24+ / 1.31 API (docs/NOTES_hashtable.md)

def string_hash(s):
    """StringHash = Storm SStrHash2 (NOTES_hashtable §1): ≤ 1023 bytes, a–z upper-cased, '/' → '\\', Bob Jenkins
    lookup2 with a = b = 0x9e3779b9, c = 0; -> signed 32-bit."""
    if s is None:
        s = ''
    try:
        raw = s.encode('latin-1')                   # the VM reads scripts as latin-1: one char = one original byte
    except UnicodeEncodeError:
        raw = s.encode('utf-8')                     # text from the .wts (UTF-8 decoded)
    b = bytearray(raw[:1023])
    if 0 in b:
        b = b[:b.index(0)]
    for i, ch in enumerate(b):
        if 0x61 <= ch <= 0x7a:
            b[i] = ch - 0x20
        elif ch == 0x2f:
            b[i] = 0x5c
    M = 0xFFFFFFFF

    def mix(a, bb, c):
        a = (a - bb - c) & M; a ^= c >> 13
        bb = (bb - c - a) & M; bb ^= (a << 8) & M
        c = (c - a - bb) & M; c ^= bb >> 13
        a = (a - bb - c) & M; a ^= c >> 12
        bb = (bb - c - a) & M; bb ^= (a << 16) & M
        c = (c - a - bb) & M; c ^= bb >> 5
        a = (a - bb - c) & M; a ^= c >> 3
        bb = (bb - c - a) & M; bb ^= (a << 10) & M
        c = (c - a - bb) & M; c ^= bb >> 15
        return a, bb, c
    a = bb = 0x9e3779b9
    c = 0
    n = len(b)
    k = 0
    while n - k >= 12:
        a = (a + (b[k] | b[k + 1] << 8 | b[k + 2] << 16 | b[k + 3] << 24)) & M
        bb = (bb + (b[k + 4] | b[k + 5] << 8 | b[k + 6] << 16 | b[k + 7] << 24)) & M
        c = (c + (b[k + 8] | b[k + 9] << 8 | b[k + 10] << 16 | b[k + 11] << 24)) & M
        a, bb, c = mix(a, bb, c)
        k += 12
    c = (c + n) & M
    t = b[k:]
    for i, ch in enumerate(t):
        if i < 4:
            a = (a + (ch << (8 * i))) & M
        elif i < 8:
            bb = (bb + (ch << (8 * (i - 4)))) & M
        else:
            c = (c + (ch << (8 * (i - 7)))) & M
    a, bb, c = mix(a, bb, c)
    return c - (1 << 32) if c & 0x80000000 else c


@nat('StringHash')
def _stringhash(N, s):
    return string_hash(s)


@nat('GetObjectName')
def _getobjectname(N, i):
    """[obs 0x6f2b9e50 -> 0x6f112e60] the object's profile field "name" (entry 0 of its list); no record ->
    "Default string" (the lookup's fallback); an empty name -> null. Units and items; A-W9-3: ability / upgrade
    names are not in the pack (their ids give "Default string")."""
    d = N.unit_types.get(i)
    if d is None and N.eng is not None:
        d = N.eng.item_types.get(i)
    if d is None:
        return 'Default string'
    return d.get('name') or None


@nat('GetHandleId')
def _gethandleid(N, h):
    return h or 0                                   # the handle value itself (0x100000 + index) [ext high]


def _v131(N):
    return getattr(N, 'core_ver', '121') == '131'


# 1.29+ slots: players 0…23, neutral aggressive 24, victim 25, extra 26, passive 27 [ext]; the engine keeps 16 slots:
# the neutral four map to 12…15, players 12…23 do not exist (A-W9-1)
@nat('GetPlayerNeutralPassive')
def _neutpassive(N):
    return 27 if _v131(N) else 15


@nat('GetPlayerNeutralAggressive')
def _neutaggr(N):
    return 24 if _v131(N) else 12


@nat('GetBJMaxPlayers')
def _bjmaxpl(N):
    return 24 if _v131(N) else 12


@nat('GetBJMaxPlayerSlots')
def _bjmaxslots(N):
    return 28 if _v131(N) else 16


@nat('GetBJPlayerNeutralVictim')
def _bjvictim(N):
    return 25 if _v131(N) else 13


@nat('GetBJPlayerNeutralExtra')
def _bjextra(N):
    return 26 if _v131(N) else 14


def jass_to_slot(N, i):
    """JASS player index -> engine slot (None: no such player)."""
    if _v131(N):
        if 24 <= i <= 27:
            return i - 12
        if 12 <= i <= 23:
            return None
    return i


def slot_to_jass(N, i):
    return i + 12 if _v131(N) and i >= 12 else i


class Hashtable(Agent):
    tname = 'hashtable'

    def __init__(self):
        self.tab = {}                               # (parent, child, category) -> value (NOTES_hashtable §2)


HT_INT, HT_REAL, HT_BOOL, HT_STR, HT_HANDLE = range(5)
_HT_CAT = {'Integer': HT_INT, 'Real': HT_REAL, 'Boolean': HT_BOOL, 'String': HT_STR, 'Str': HT_STR, 'Handle': HT_HANDLE}
_HT_ZERO = {HT_INT: 0, HT_REAL: 0.0, HT_BOOL: False, HT_STR: None, HT_HANDLE: None}


def _ht(N, h):
    return N.o(h, Hashtable)


@nat('InitHashtable')
def _inithash(N):
    return Hashtable()


def _ht_save(cat):
    def f(N, h, p, c, v):
        t = _ht(N, h)
        if t is not None:
            t.tab[(p, c, cat)] = v if cat != HT_HANDLE else (v or 0)
        return True if cat in (HT_STR, HT_HANDLE) else None
    return f


def _ht_load(cat):
    def f(N, h, p, c):
        t = _ht(N, h)
        v = t.tab.get((p, c, cat)) if t is not None else None
        if v is None:
            return _HT_ZERO[cat]
        if cat == HT_HANDLE:
            return N.o(v)                           # only the id is stored; a destroyed object loads as null
        return v
    return f


def _ht_have(cat):
    def f(N, h, p, c):
        t = _ht(N, h)
        return t is not None and (p, c, cat) in t.tab
    return f


def _ht_remove(cat):
    def f(N, h, p, c):
        t = _ht(N, h)
        if t is not None:
            t.tab.pop((p, c, cat), None)
    return f


REG['SaveInteger'] = _ht_save(HT_INT)
REG['SaveReal'] = _ht_save(HT_REAL)
REG['SaveBoolean'] = _ht_save(HT_BOOL)
REG['SaveStr'] = _ht_save(HT_STR)
REG['LoadInteger'] = _ht_load(HT_INT)
REG['LoadReal'] = _ht_load(HT_REAL)
REG['LoadBoolean'] = _ht_load(HT_BOOL)
REG['LoadStr'] = _ht_load(HT_STR)
for _k, _c in (('Integer', HT_INT), ('Real', HT_REAL), ('Boolean', HT_BOOL), ('String', HT_STR), ('Handle', HT_HANDLE)):
    REG['HaveSaved' + _k] = _ht_have(_c)
    REG['RemoveSaved' + _k] = _ht_remove(_c)
for _n in ['SavePlayerHandle', 'SaveWidgetHandle', 'SaveDestructableHandle', 'SaveItemHandle', 'SaveUnitHandle', 'SaveAbilityHandle', 'SaveTimerHandle', 'SaveTriggerHandle', 'SaveTriggerConditionHandle', 'SaveTriggerActionHandle', 'SaveTriggerEventHandle', 'SaveForceHandle', 'SaveGroupHandle', 'SaveLocationHandle', 'SaveRectHandle', 'SaveBooleanExprHandle', 'SaveSoundHandle', 'SaveEffectHandle', 'SaveUnitPoolHandle', 'SaveItemPoolHandle', 'SaveQuestHandle', 'SaveQuestItemHandle', 'SaveDefeatConditionHandle', 'SaveTimerDialogHandle', 'SaveLeaderboardHandle', 'SaveMultiboardHandle', 'SaveMultiboardItemHandle', 'SaveTrackableHandle', 'SaveDialogHandle', 'SaveButtonHandle', 'SaveTextTagHandle', 'SaveLightningHandle', 'SaveImageHandle', 'SaveUbersplatHandle', 'SaveRegionHandle', 'SaveFogStateHandle', 'SaveFogModifierHandle', 'SaveAgentHandle', 'SaveHashtableHandle', 'SaveFrameHandle']:
    REG[_n] = _ht_save(HT_HANDLE)
for _n in ['LoadPlayerHandle', 'LoadWidgetHandle', 'LoadDestructableHandle', 'LoadItemHandle', 'LoadUnitHandle', 'LoadAbilityHandle', 'LoadTimerHandle', 'LoadTriggerHandle', 'LoadTriggerConditionHandle', 'LoadTriggerActionHandle', 'LoadTriggerEventHandle', 'LoadForceHandle', 'LoadGroupHandle', 'LoadLocationHandle', 'LoadRectHandle', 'LoadBooleanExprHandle', 'LoadSoundHandle', 'LoadEffectHandle', 'LoadUnitPoolHandle', 'LoadItemPoolHandle', 'LoadQuestHandle', 'LoadQuestItemHandle', 'LoadDefeatConditionHandle', 'LoadTimerDialogHandle', 'LoadLeaderboardHandle', 'LoadMultiboardHandle', 'LoadMultiboardItemHandle', 'LoadTrackableHandle', 'LoadDialogHandle', 'LoadButtonHandle', 'LoadTextTagHandle', 'LoadLightningHandle', 'LoadImageHandle', 'LoadUbersplatHandle', 'LoadRegionHandle', 'LoadFogStateHandle', 'LoadFogModifierHandle', 'LoadHashtableHandle', 'LoadFrameHandle']:
    REG[_n] = _ht_load(HT_HANDLE)


@nat('FlushChildHashtable')
def _flushchild(N, h, p):
    t = _ht(N, h)
    if t is not None:
        for k in [k for k in t.tab if k[0] == p]:
            del t.tab[k]


@nat('FlushParentHashtable')
def _flushparent(N, h):
    t = _ht(N, h)
    if t is not None:
        t.tab.clear()                               # A-W9-2: the table stays usable (destroying it: Open)


# ================================================================ BUGS MAP-NAT: natives the fan-map audit found
# called but missing (docs/map_audit.md). Behaviour only; tags as in the NOTES files.

def _gc_flush(kind):
    """FlushStored<Kind>(cache, mission, key): the one entry goes (HaveStored… false afterwards) [ext]."""
    def f(N, c, mk, k):
        o = N.o(c, GameCache)
        if o:
            o.data.pop((kind, _gc_pack(mk), k), None)
    return f


for _kind in ('Integer', 'Real', 'Boolean', 'String'):
    REG['FlushStored' + _kind] = _gc_flush(_kind)


@nat('UnitMakeAbilityPermanent')
def _abilpermanent(N, u, perm, a):
    """Unclassed_Native_0032 §6 [obs 0x6f2cb9b0]: a false answer only when the unit is bad or lacks the ability.
    True/false moves the ability instance's counter by +1/-1 (it accumulates and never clamps); the answer means
    "the unit carries the ability", not "the flag changed". The counter keeps the ability through a type morph."""
    uo = N.o(u, Unit)
    if uo is None or a not in uo.abilities:
        return False
    uo.perm_abil[a] = uo.perm_abil.get(a, 0) + (1 if perm else -1)
    return True


def _abil_step(d):
    def f(N, u, a):
        uo = N.o(u, Unit)
        if not uo or a not in uo.abilities:
            return 0
        cur = uo.abilities[a]
        if d > 0:
            r = N.eng.ab.rec(a)
            mx = len(r['lv']) if r and r.get('lv') else 0
            if mx and cur >= mx:
                return cur                               # Unclassed_Native_0029 §2: at the ceiling, no side effect
            lvl = cur + 1
        else:
            lvl = max(1, cur - 1)                        # [inf] Dec stops at level 1
        N.eng.ab.level_change(uo, a, lvl)
        return lvl
    return f


REG['IncUnitAbilityLevel'] = _abil_step(1)
REG['DecUnitAbilityLevel'] = _abil_step(-1)


@nat('GetWidgetX')
def _widgetx(N, w):
    o = N.o_read(w)
    return getattr(o, 'x', 0.0) if o is not None else 0.0


@nat('GetWidgetY')
def _widgety(N, w):
    o = N.o_read(w)
    return getattr(o, 'y', 0.0) if o is not None else 0.0


@nat('GetUnitAcquireRange')
def _acquirerange(N, u):
    """the unit's current acquisition radius (attack ability +0x244; SetUnitAcquireRange stores, NAT-7)."""
    uo = N.o(u, Unit)
    return float(uo.data.get('acquire', 0) or 0) if uo else 0.0


@nat('GetUnitDefaultAcquireRange')
def _defaultacquire(N, u):
    """the unit-type record's factory radius (+0x194), unaffected by SetUnitAcquireRange (CUnit_p2 0x6f2c4760)."""
    uo = N.o(u, Unit)
    if uo is None:
        return 0.0
    d = N.unit_types.get(uo.tid)
    return float(d.get('acquire', 0) or 0) if d else 0.0


def _type_field(key):
    def f(N, tid):
        d = N.unit_types.get(tid)
        return int(float(d.get(key, 0) or 0)) if d else 0
    return f


# natives the engine has but common.j does not declare (Castle Fight declares them in its own script) [ext]:
# the unit type's gold / lumber cost and build time as integers
REG['GetUnitGoldCost'] = _type_field('gold')
REG['GetUnitWoodCost'] = _type_field('lumber')
REG['GetUnitBuildTime'] = _type_field('bldtm')


@nat('BlzGetUnitMaxHP')
def _blzmaxhp(N, u):
    uo = N.o(u, Unit)
    return int(uo.maxlife) if uo else 0


@nat('BlzGetUnitMaxMana')
def _blzmaxmana(N, u):
    uo = N.o(u, Unit)
    return int(uo.maxmana) if uo else 0


@nat('BlzSetUnitMaxHP')
def _blzsetmaxhp(N, u, hp):
    """sets the maximum, the current life keeps its fraction (at least 1 while alive) [inf]."""
    uo = N.o(u, Unit)
    if uo and not uo.dead and uo.maxlife > 0:
        frac = uo.life / uo.maxlife
        uo.maxlife = r32(max(1.0, float(hp)))
        uo.life = r32(max(1.0, min(uo.maxlife, frac * uo.maxlife)))


@nat('BlzSetUnitMaxMana')
def _blzsetmaxmana(N, u, m):
    uo = N.o(u, Unit)
    if uo and not uo.dead:
        frac = uo.mana / uo.maxmana if uo.maxmana > 0 else 1.0
        uo.maxmana = r32(max(0.0, float(m)))
        uo.mana = r32(min(uo.maxmana, frac * uo.maxmana))


class UnitPool(Agent):
    tname = 'unitpool'

    def __init__(self):
        self.types = []                  # [(unit type id, weight)] in insertion order


@nat('CreateUnitPool')
def _createunitpool(N):
    return UnitPool()


@nat('UnitPoolAddUnitType')
def _poolpadd(N, pool, tid, weight):
    po = N.o(pool, UnitPool)
    if po is not None:
        po.types.append((tid, max(0.0, weight)))        # [inf] a type added twice is two entries


@nat('UnitPoolRemoveUnitType')
def _poolremove(N, pool, tid):
    po = N.o(pool, UnitPool)
    if po is not None:
        po.types = [t for t in po.types if t[0] != tid]


@nat('PlaceRandomUnit')
def _placerandomunit(N, pool, p, x, y, face):
    """one real draw (unit() * total weight, float32) on the item-pool stream (game 0x23), walked in insertion
    order; an empty or zero-weight pool creates nothing and draws nothing [inf: stream and walk follow ChooseRandomItem;
    the weights are reals]."""
    po = N.o(pool, UnitPool)
    pl = N.o(p, Player)
    if po is None or pl is None:
        return None
    total = 0.0
    for _t, w in po.types:
        total = r32(total + w)
    if total <= 0.0:
        return None
    r = r32(N.streams.game[0x23].unit() * total)
    last = None
    for tid, w in po.types:
        if w <= 0.0:
            continue
        last = tid
        if r < w:
            break
        r = r32(r - w)
    else:
        tid = last                                    # rounding left r at the end: the last weighted type
    return N.create_unit(pl, tid, x, y, face)


@nat('BlzFrameSetVisible')
def _framesetvisible(N, f, vis):
    """frames are placeholders (no UI), but a map polls their state: Gem TD loops "for ctx = 0 …: exitwhen
    BlzFrameIsVisible(BlzGetFrameByName(...))" - the visible flag is kept, a new frame is visible [inf]."""
    o = N.o(f)
    if o is not None:
        o.frame_hidden = not vis


@nat('BlzFrameIsVisible')
def _frameisvisible(N, f):
    o = N.o(f)
    return o is not None and not getattr(o, 'frame_hidden', False)


@nat('UnitAlive')
def _unitalive(N, u):
    """declared by the map itself (Gem TD): the engine's IsUnitAliveBJ without the GetWidgetLife compare [ext]."""
    uo = N.o(u, Unit)
    return bool(uo and not uo.dead)


@nat('IsHeroUnitId')
def _isheroid(N, tid):
    return 65 <= ((tid >> 24) & 0xff) <= 90          # the hero check of Unit.is_hero (T§5) on a type id [inf]


REG['BlzBitOr'] = lambda N, a, b: s32(a | b)
REG['BlzBitAnd'] = lambda N, a, b: s32(a & b)
REG['BlzBitXor'] = lambda N, a, b: s32(a ^ b)


# ================================================================ BUGS CHAT-1 / SEL-1 (docs/map_audit.md section 6 item 1)
CHAT_MAX = 128                  # entries of the chat table (the hand menu pages them)
CHAT_DYN = 16                   # SAVE-1: typed messages outside the table (the game menu's "-load CODE"), a ring; index CHAT_MAX + k; every entry knows its player


def chat_scan(p):
    """the string literals of every TriggerRegisterPlayerChatEvent(trig, player, "text", exact) call in the script, in
    code order (the compiler pushes the arguments in order: ... PUSH_S text, <exact>, CALL_N). Static part of the
    chat table: its indices are the same in every run of the map, whatever the registration order at runtime."""
    from . import compiler as C
    out = []
    ni = p.native_by_name.get('TriggerRegisterPlayerChatEvent')
    if ni is None:
        return out
    ops, A, B = p.ops, p.A, p.B
    for i in range(2, len(ops)):
        if ops[i] == C.OP['CALL_N'] and A[i] == ni and ops[i - 2] == C.OP['PUSH_S'] and B[i - 2] >= 0:
            s = p.strings[B[i - 2]]
            if s and s not in out and len(out) < CHAT_MAX:
                out.append(s)
    return out


def chat_index(N, s):
    if s in N.chat_cmds:
        return N.chat_cmds.index(s)
    if len(N.chat_cmds) >= CHAT_MAX:
        return -1
    N.chat_cmds.append(s)
    return len(N.chat_cmds) - 1


@nat('TriggerRegisterPlayerChatEvent')
def _regchat(N, t, p, s, exact):
    # [ext] common.j: the event fires when the player types a message that equals (exact) or contains (not exact) the
    # string; [inf] case-sensitive. An empty string with exact = false matches every message (never offered in the menu).
    pl = N.o(p, Player)
    reg = _reg(N, t, 'chat', N.ev_id('EVENT_PLAYER_CHAT'), None, pl)
    if reg is not None and pl is not None:
        reg.extra = [s or '', bool(exact)]
        N.chat_regs.append(reg)
        if s:
            chat_index(N, s)
    return reg


def chat_menu(N, player):
    """the hand menu's commands of a player: per chat table index the string when a live registration of that player
    would react to it, else '' (indices stay stable)."""
    pl = N.players[player]
    out = [''] * len(N.chat_cmds)
    for reg in N.chat_regs:
        if reg.alive and reg.trig.alive and reg.src is pl and reg.extra[0] and reg.extra[0] in N.chat_cmds:
            out[N.chat_cmds.index(reg.extra[0])] = reg.extra[0]
    return out


def chat_dyn_add(N, msg, player):
    """SAVE-1: a typed message that is not a table entry -> its input index (CHAT_MAX + k). CHAT_DYN slots used in turn: the
    master queues the input right after and consumes it at the next turn, so a slot is not reused before its message ran."""
    msg = (msg or '')[:255]
    k = N.chat_dyn_n % CHAT_DYN
    N.chat_dyn_n += 1
    if k < len(N.chat_dyn):
        N.chat_dyn[k] = msg
        N.chat_dyn_owner[k] = player
    else:
        N.chat_dyn.append(msg)
        N.chat_dyn_owner.append(player)
    return CHAT_MAX + k


def chat_input(N, player, idx):
    """input kind 7: the player 'types' chat table entry idx. -> the number of registrations that matched, -1 for a bad index."""
    if CHAT_MAX <= idx < CHAT_MAX + CHAT_DYN:
        if idx - CHAT_MAX >= len(N.chat_dyn) or N.chat_dyn_owner[idx - CHAT_MAX] != player:
            return -1                                 # never filled, or the slot was reused by another player's text meanwhile
        msg = N.chat_dyn[idx - CHAT_MAX]
    elif not 0 <= idx < len(N.chat_cmds):
        return -1
    else:
        msg = N.chat_cmds[idx]
    pl = N.players[player]
    if N.save_capture is not None:                # SAVE-1: the player's own save command starts the capture of the code
        N.save_capture.on_chat(player, msg, N.vm.game_ms() / 1000.0)
    ev = N.ev_id('EVENT_PLAYER_CHAT')
    hits = []
    for reg in N.chat_regs:                       # the matching registrations are fixed when the message arrives
        if not reg.alive or not reg.trig.alive or reg.src is not pl:
            continue
        s, exact = reg.extra
        if (msg == s) if exact else (s in msg):
            hits.append(reg)
    for reg in hits:
        if reg.alive and reg.trig.alive:          # an earlier action may have destroyed it
            N.vm.fire(reg.trig, {V.CTX_TRIGGER_PLAYER: N.H(pl), V.CTX_EVENT_ID: ev},
                      {'event': ev, 'chat': msg, 'chatmatch': reg.extra[0]})
    return len(hits)


@nat('IsUnitSelected')
def _isunitselected(N, u, p):
    uo, pl = N.o(u, Unit), N.o(p, Player)
    return uo is not None and pl is not None and uo in N.eng.selected(pl)
# ================================================================ BEGIN BUGS NAT-1 .. NAT-7: unit field API, event damage,
# ability availability, owner-change events, hero attribute setters, tax rate, acquire range (docs/map_audit.md section 6).
# Udon twin: W3Natives.cs / W3Engine.cs, marked block "NAT-1..NAT-7", impl ids 1250-1299. Clean room: behaviour only; the
# field ids are the fourcc values of common.j 1.31 (ConvertUnitIntegerField('ulev') ...) [ext].

def _fid(s):
    return (ord(s[0]) << 24) | (ord(s[1]) << 16) | (ord(s[2]) << 8) | ord(s[3])


def _own(uo):
    """copy-on-write: a unit's field overrides live in a private copy of its type's data row (Udon: oUr[u], a private
    copy of the type's JUT5 row). A morph (new type) drops them."""
    if not uo.own_data:
        uo.data = dict(uo.data)
        uo.own_data = True


def _field_miss(N, name, fid):
    """an unknown / unmodelled field: reads give 0, writes are ignored (the Set natives return false); recorded like a
    stub (map_audit 'stubbed_called', key 'Native(fourcc)'). The Udon VM returns the same without the record."""
    k = '%s(%s)' % (name, _id4(fid))
    N.stub_counts[k] = N.stub_counts.get(k, 0) + 1


# unit integer fields kept in the type's data row [ext: unitintegerfield names]: the bounty ones are the dice / sides /
# base of Engine.bounty (gold and lumber), 'umvt' the move type bits (2 = flying)
UF_INT_DATA = {_fid('ubdi'): 'bountydice', _fid('ubsi'): 'bountysides', _fid('ubba'): 'bountyplus',
               _fid('ulbd'): 'lumberbountydice', _fid('ulbs'): 'lumberbountysides', _fid('ulba'): 'lumberbountyplus',
               _fid('umvt'): 'movetp'}
UF_HERO_ATTR = {_fid('uagc'): 'AGI', _fid('uagm'): 'AGI', _fid('uagb'): 'AGI', _fid('uinc'): 'INT', _fid('uinm'): 'INT',
                _fid('uinb'): 'INT', _fid('ustc'): 'STR', _fid('ustm'): 'STR', _fid('ustb'): 'STR'}
UF_DEFTYPE, UF_LEVEL = _fid('udty'), _fid('ulev')
UF_REAL_DATA = {_fid('uacq'): 'acquire', _fid('umvr'): 'turnRate', _fid('uamn'): 'minRange', _fid('ucpt'): 'castpt',
                _fid('ucbs'): 'castbsw'}
UF_SCALE, UF_SELSCALE = _fid('usca'), _fid('ussc')
UF_HP, UF_MANA, UF_DEFENSE, UF_SPEED = _fid('uhpc'), _fid('umpc'), _fid('udfc'), _fid('umvc')
# weapon fields (weapon index 0 / 1 -> key suffix 1 / 2)
UW_INT_DATA = {_fid('ua1d'): 'dice', _fid('ua1b'): 'dmgplus', _fid('ua1s'): 'sides', _fid('utc1'): 'targCount',
               _fid('ua1t'): 'atkType'}
UW_REAL_DATA = {_fid('ua1c'): 'cool', _fid('ua1r'): 'rangeN', _fid('udp1'): 'dmgpt', _fid('ubs1'): 'backSw',
                _fid('ua1z'): 'Missilespeed', _fid('uma1'): 'Missilearc', _fid('ua1f'): 'Farea', _fid('ua1h'): 'Harea',
                _fid('ua1q'): 'Qarea', _fid('uhd1'): 'Hfact', _fid('uqd1'): 'Qfact', _fid('udl1'): 'damageLoss'}
UW_ENABLED, UW_HOMING = _fid('uaen'), _fid('umh1')


def _hero_attr_set(N, uo, k, v, permanent=False):
    """SetHeroStr / Agi / Int and the attribute fields: the target is compared against the bonus-free value and the
    delta moves the attribute [Unclassed_Native_0027 §2]. [obs runtime 1.26a] docs/oracle.md "hero STR loss at low life" (ext_combat):
    NO clamp at 0 (SetHeroStr -5 gives max life -25 and the hero dies; heroes.apply_attributes floors life at 1 first). The adder's routing of the
    permanent flag is not traced, and the bonus-free getter only sees the permanent layer, so the delta always goes
    to the permanent layer and the flag is a no-op [inf]: repeating the call is idempotent. Every effect follows
    (NOTES_heroes 2.3)."""
    if not getattr(uo, 'hero', False):
        return False
    h = N.eng.hero
    d = v - h.attr_no_bonus(uo, k)
    bonus = uo.h_bonus_perm                               # [inf] flag forwarded, routing untraced
    bonus[k] = bonus.get(k, 0) + d
    h.apply_attributes(uo)
    return True


def _refresh_fighter(N, u):
    """the unit has / has no enabled weapon after BlzSetUnitWeaponBooleanField: fighter list as in morph_type."""
    eng = N.eng
    fights = any(eng.has_weapon(u, i) for i in (0, 1))
    if fights and not u.fighter:
        eng.fighters.append(u)
        u.fighter = True
        u.next_scan = eng.now()
        eng.wake(u, eng.now())
    elif not fights and u.fighter:
        if u in eng.fighters:
            eng.fighters.remove(u)
        u.fighter = False
        u.atk_target = None


@nat('BlzGetUnitIntegerField')
def _blzgetuif(N, u, f):
    uo = N.o_read(u, Unit)
    if uo is None:
        return 0
    if f in UF_INT_DATA:
        return int(float(uo.data.get(UF_INT_DATA[f], 0) or 0))
    if f == UF_DEFTYPE:
        return uo.def_type
    if f == UF_LEVEL:
        return uo.hlevel if getattr(uo, 'hero', False) else uo.level
    if f in UF_HERO_ATTR:
        return N.eng.hero.attr(uo, UF_HERO_ATTR[f]) if getattr(uo, 'hero', False) else 0
    _field_miss(N, 'BlzGetUnitIntegerField', f)
    return 0


@nat('BlzSetUnitIntegerField')
def _blzsetuif(N, u, f, v):
    uo = N.o(u, Unit)
    if uo is None:
        return False
    if f in UF_INT_DATA:
        _own(uo)
        uo.data[UF_INT_DATA[f]] = v
        return True
    if f == UF_DEFTYPE:
        uo.def_type = v
        return True
    if f == UF_LEVEL:
        if getattr(uo, 'hero', False):
            return False
        uo.level = v
        return True
    if f in UF_HERO_ATTR:
        return _hero_attr_set(N, uo, UF_HERO_ATTR[f], v)
    _field_miss(N, 'BlzSetUnitIntegerField', f)
    return False


@nat('BlzGetUnitRealField')
def _blzgetuf(N, u, f):
    uo = N.o_read(u, Unit)
    if uo is None:
        return 0.0
    if f in UF_REAL_DATA:
        return float(uo.data.get(UF_REAL_DATA[f], 0) or 0)
    if f == UF_SCALE:
        return float(uo.data.get('usca', 1.0))
    if f == UF_SELSCALE:
        return float(uo.data.get('ussc', 1.0))
    if f == UF_HP:
        return uo.life
    if f == UF_MANA:
        return uo.mana
    if f == UF_DEFENSE:
        return uo.armor
    if f == UF_SPEED:
        return N.eng.eff_speed(uo, True)                    # = GetUnitMoveSpeed (clamped)
    _field_miss(N, 'BlzGetUnitRealField', f)
    return 0.0


@nat('BlzSetUnitRealField')
def _blzsetuf(N, u, f, v):
    uo = N.o(u, Unit)
    if uo is None:
        return False
    if f in UF_REAL_DATA:
        _own(uo)
        uo.data[UF_REAL_DATA[f]] = v
        if f == _fid('uacq'):
            uo.data['acqSet'] = 1                     # an explicit 0 means "never acquires by itself" (NAT-7)
        return True
    if f == UF_SCALE or f == UF_SELSCALE:
        _own(uo)
        uo.data['usca' if f == UF_SCALE else 'ussc'] = v
        return True
    if f == UF_HP:
        _setunitstate(N, u, 0, v)
        return True
    if f == UF_MANA:
        _setunitstate(N, u, 2, v)
        return True
    if f == UF_DEFENSE:
        uo.armor = r32(v)
        return True
    if f == UF_SPEED:
        _setmovespeed(N, u, v)
        return True
    _field_miss(N, 'BlzSetUnitRealField', f)
    return False


@nat('BlzGetUnitBooleanField')
def _blzgetubf(N, u, f):
    uo = N.o_read(u, Unit)
    if uo is not None and f == _fid('ubdg'):
        return uo.is_bldg
    _field_miss(N, 'BlzGetUnitBooleanField', f)
    return False


@nat('BlzSetUnitBooleanField')
def _blzsetubf(N, u, f, v):
    _field_miss(N, 'BlzSetUnitBooleanField', f)
    return False


@nat('BlzGetUnitStringField')
def _blzgetusf(N, u, f):
    uo = N.o_read(u, Unit)
    if uo is not None and f == _fid('unam'):
        return uo.data.get('name', _id4(uo.tid))
    _field_miss(N, 'BlzGetUnitStringField', f)
    return None


@nat('BlzSetUnitStringField')
def _blzsetusf(N, u, f, v):
    _field_miss(N, 'BlzSetUnitStringField', f)
    return False


def _wkey(name, i):
    return '%s%d' % (name, i + 1)


@nat('BlzGetUnitWeaponIntegerField')
def _blzgetuwif(N, u, f, i):
    uo = N.o_read(u, Unit)
    if uo is not None and i in (0, 1) and f in UW_INT_DATA:
        return int(float(uo.data.get(_wkey(UW_INT_DATA[f], i), 0) or 0))
    _field_miss(N, 'BlzGetUnitWeaponIntegerField', f)
    return 0


@nat('BlzSetUnitWeaponIntegerField')
def _blzsetuwif(N, u, f, i, v):
    uo = N.o(u, Unit)
    if uo is not None and i in (0, 1) and f in UW_INT_DATA:
        _own(uo)
        uo.data[_wkey(UW_INT_DATA[f], i)] = v
        return True
    _field_miss(N, 'BlzSetUnitWeaponIntegerField', f)
    return False


@nat('BlzGetUnitWeaponRealField')
def _blzgetuwf(N, u, f, i):
    uo = N.o_read(u, Unit)
    if uo is not None and i in (0, 1) and f in UW_REAL_DATA:
        return float(uo.data.get(_wkey(UW_REAL_DATA[f], i), 0) or 0)
    _field_miss(N, 'BlzGetUnitWeaponRealField', f)
    return 0.0


@nat('BlzSetUnitWeaponRealField')
def _blzsetuwf(N, u, f, i, v):
    uo = N.o(u, Unit)
    if uo is not None and i in (0, 1) and f in UW_REAL_DATA:
        _own(uo)
        uo.data[_wkey(UW_REAL_DATA[f], i)] = v
        return True
    _field_miss(N, 'BlzSetUnitWeaponRealField', f)
    return False


@nat('BlzGetUnitWeaponBooleanField')
def _blzgetuwbf(N, u, f, i):
    uo = N.o_read(u, Unit)
    if uo is not None and i in (0, 1):
        if f == UW_ENABLED:
            return bool((int(uo.data.get('weapsOn', 0) or 0) >> i) & 1)
        if f == UW_HOMING:
            return bool(int(float(uo.data.get(_wkey('MissileHoming', i), 0) or 0)))
    _field_miss(N, 'BlzGetUnitWeaponBooleanField', f)
    return False


@nat('BlzSetUnitWeaponBooleanField')
def _blzsetuwbf(N, u, f, i, v):
    uo = N.o(u, Unit)
    if uo is not None and i in (0, 1):
        if f == UW_ENABLED:
            _own(uo)
            on = int(uo.data.get('weapsOn', 0) or 0)
            uo.data['weapsOn'] = (on | (1 << i)) if v else (on & ~(1 << i))
            _refresh_fighter(N, uo)
            return True
        if f == UW_HOMING:
            _own(uo)
            uo.data[_wkey('MissileHoming', i)] = 1 if v else 0
            return True
    _field_miss(N, 'BlzSetUnitWeaponBooleanField', f)
    return False


@nat('BlzGetUnitWeaponStringField')
def _blzgetuwsf(N, u, f, i):
    _field_miss(N, 'BlzGetUnitWeaponStringField', f)
    return None


@nat('BlzSetUnitWeaponStringField')
def _blzsetuwsf(N, u, f, i, v):
    _field_miss(N, 'BlzSetUnitWeaponStringField', f)
    return False


@nat('BlzGetUnitBaseDamage')
def _blzgetbasedmg(N, u, i):
    """the weapon's "Damage - Base" (the part before the dice) [ext: BlzGetUnitBaseDamage(unit, weaponIndex)]."""
    uo = N.o_read(u, Unit)
    return int(float(uo.data.get(_wkey('dmgplus', i), 0) or 0)) if uo is not None and i in (0, 1) else 0


@nat('BlzSetUnitBaseDamage')
def _blzsetbasedmg(N, u, v, i):
    uo = N.o(u, Unit)
    if uo is not None and i in (0, 1):
        _own(uo)
        uo.data[_wkey('dmgplus', i)] = v


@nat('BlzGetUnitArmor')
def _blzgetarmor(N, u):
    """the unit's base armor (the value damage reduction reads before temporary bonuses) [inf]."""
    uo = N.o_read(u, Unit)
    return uo.armor if uo is not None else 0.0


@nat('BlzSetUnitArmor')
def _blzsetarmor(N, u, v):
    uo = N.o(u, Unit)
    if uo is not None:
        uo.armor = r32(v)


@nat('SetUnitAcquireRange')
def _setacquire(N, u, v):
    """Unclassed_Native_0025 §1 [obs 1.26a]: a plain > 0 test, so zero and negatives are ignored (1.21 accepted
    zero). The radius is the scan radius of NOTES_combat 2.4 (Engine.scan); an explicit > 0 still sets NAT-7."""
    if v > 0:
        _blzsetuf(N, u, _fid('uacq'), v)


# ---- hero attributes (x hero siege 36 calls)
@nat('SetHeroStr')
def _setherostr(N, u, v, perm):
    h = N.o(u, Unit)
    if h is not None:
        _hero_attr_set(N, h, 'STR', v, bool(perm))


@nat('SetHeroAgi')
def _setheroagi(N, u, v, perm):
    h = N.o(u, Unit)
    if h is not None:
        _hero_attr_set(N, h, 'AGI', v, bool(perm))


@nat('SetHeroInt')
def _setheroint(N, u, v, perm):
    h = N.o(u, Unit)
    if h is not None:
        _hero_attr_set(N, h, 'INT', v, bool(perm))


# ---- NAT-2 event damage: BlzSetEventDamage changes the damage the running DAMAGED event will apply
@nat('BlzSetEventDamage')
def _blzseteventdamage(N, v):
    d = N.vm.evdata[-1] if N.vm.evdata else None
    if d is None or 'dmgbox' not in d:
        return                                       # not inside a damage event
    d['dmgbox'][0] = r32(v)
    d['damage'] = r32(v)


@nat('BlzGetEventDamageTarget')
def _blzeventdamagetarget(N):
    d = N.vm.evdata[-1] if N.vm.evdata else None
    return N.vm.ctx_top(V.CTX_TRIGGER_UNIT) if d is not None and 'dmgbox' in d else None


@nat('BlzUnitInterruptAttack')
def _blzinterrupt(N, u):
    """cancels the swing in progress: no damage, no missile; the cooldown keeps running [inf]."""
    uo = N.o(u, Unit)
    if uo is not None:
        uo.dp_at = -1
        uo.dp_target = None


@nat('BlzIsUnitInvulnerable')
def _blzisinvuln(N, u):
    uo = N.o_read(u, Unit)
    return bool(uo and uo.invulnerable)


# ---- NAT-3 SetPlayerAbilityAvailable: an unavailable ability cannot be cast by the player's units (Spells.order check)
@nat('SetPlayerAbilityAvailable')
def _setabilavail(N, p, a, avail):
    pl = N.o(p, Player)
    if pl is not None:
        if avail:
            pl.abil_off.discard(a)
        else:
            pl.abil_off.add(a)


# ---- NAT-6 SetPlayerTaxRate / GetPlayerTaxRate: stored per (other player, resource); no transfer is modelled
@nat('SetPlayerTaxRate')
def _settaxrate(N, p, o, res, rate):
    pl = N.o(p, Player)
    po = N.o(o, Player)
    if pl is not None and po is not None:
        pl.tax[(po.id, res)] = rate


@nat('GetPlayerTaxRate')
def _gettaxrate(N, p, o, res):
    pl = N.o(p, Player)
    po = N.o(o, Player)
    return pl.tax.get((po.id, res), 0) if pl is not None and po is not None else 0


REG['GetChangingUnitPrevOwner'] = _evd('prevowner')
# ================================================================ END NAT-1 .. NAT-7


# ================================================================ BEGIN BUGS NAT-8 .. NAT-14: neutral purchase orders, unit race,
# resource amount, mouse getters (stub, NAT-11 = STUB_RE), tech levels down, hero revive, item name (docs/map_audit.md section 6).
# Udon twin: W3NatMore.cs (impl ids 1281-1299). Clean room: behaviour only; tags [obs] / [inf] / [ext].

# NAT-8 IssueNeutralImmediateOrder / ...ById (Legion TD 11 sites, Castle Fight 1): the player buys the unit type (or item type)
# from the neutral structure, exactly like a purchase click (shops.buy: stock, delay, gold / lumber, food, the buyer's
# Aneu reach) [ext: the order is "sell unit"; same refusal codes as the click, inf]. True = the purchase happened.
# The point / target variants (IssueNeutralPointOrder / TargetOrder: neutral spells) are not modelled (no audited map
# calls them) and stay unknown.
def _neutral_buy(N, p, shop_h, tid):
    pl, shop = N.o(p, Player), N.o(shop_h, Unit)
    if pl is None or shop is None or not tid:
        return False
    code, _sold = N.eng.shops.buy(pl.id, shop, tid)
    return code == 0


@nat('IssueNeutralImmediateOrderById')
def _neutralimmid(N, p, u, tid):
    return _neutral_buy(N, p, u, tid)


@nat('IssueNeutralImmediateOrder')
def _neutralimm(N, p, u, name):
    return _neutral_buy(N, p, u, _unitid(N, name))


# PORT13-NativesB: IssueNeutralTargetOrder / ...ById (Unclassed_Native_0035 §4/§5 [obs 0x6f2ce1f0 / 0x6f3c8d30]):
# the neutral structure (the unit argument) is ordered to serve the acting player with a target widget. A null player,
# unit or target widget answers false before dispatch [obs]; the structure is not tested for shop / tavern capability,
# the order dispatcher decides [inf]. The string form resolves the order string and a table miss (-1 / 0) answers false
# before dispatch [obs 0x6f2ce620 / 0x6f3c8ee0]. Unlike IssueTargetOrderById this is not the target-then-point kernel
# (0x6f2cd580): the neutral target dispatcher 0x6f2cd730 only reports whether the target form was accepted.
def _neutral_target(N, p, u, oid, t):
    pl, uo = N.o(p, Player), N.o(u, Unit)
    if pl is None or uo is None or not oid:
        return False
    tu = N.o(t, Unit) or _item(N, t) or _destr_target(N, t)
    if tu is None:
        return False
    return N.issue_order(uo, oid, target=tu)


@nat('IssueNeutralTargetOrderById')
def _neutraltgtid(N, p, u, oid, t):
    return _neutral_target(N, p, u, oid, t)


@nat('IssueNeutralTargetOrder')
def _neutraltgt(N, p, u, name, t):
    return _neutral_target(N, p, u, ORDER_IDS.get(name, 0), t)


# NAT-9 GetUnitRace: the UnitData race value of the unit's type (unitdata.RACES = the game's race numbers
# RACE_HUMAN 1, ORC 2, UNDEAD 3, NIGHTELF 4, DEMON 5, OTHER 7 [obs 0x6f807720]; creeps / critters / naga keep their own
# numbers, 0 = unknown) [inf for the extra ones]
@nat('GetUnitRace')
def _getunitrace(N, u):
    uo = N.o_read(u, Unit)
    return int(float(uo.data.get('race', 0) or 0)) if uo is not None else 0


# NAT-10 Get/Set/AddResourceAmount: the amount a unit holds (mines). Unclassed_Native_0029 §9/§10 and
# Unclassed_Native_0030 §1 [obs 0x6f2c8be0, 0x6f2c8d10, 0x6f2c8e40]: only a unit carrying the mine / cargo object
# answers (bit 0x20000 of +0x5c; the port's mine_info), the write is not clamped, and a non-carrier is a silent no-op
# (Get answers 0). Mining is not modelled, so nothing consumes it.
@nat('GetResourceAmount')
def _getresamt(N, u):
    uo = N.o_read(u, Unit)
    if uo is None or N.eng.hv.mine_info(uo)[0] is None:
        return 0
    return getattr(uo, 'res_amt', 0)


@nat('SetResourceAmount')
def _setresamt(N, u, v):
    uo = N.o(u, Unit)
    if uo is not None and N.eng.hv.mine_info(uo)[0] is not None:
        uo.res_amt = v


@nat('AddResourceAmount')
def _addresamt(N, u, v):
    uo = N.o(u, Unit)
    if uo is not None and N.eng.hv.mine_info(uo)[0] is not None:
        uo.res_amt = getattr(uo, 'res_amt', 0) + v


# NAT-12 (Gem TD) BlzDecPlayerTechResearched: the researched level goes down by `levels`, not below 0 [ext 1.31 common.j;
# the clamp is inf]; nothing else follows (a unit type id has no level: pl.tech is not read for it)
@nat('BlzDecPlayerTechResearched')
def _dectech(N, p, t, lvl):
    pl = N.o(p, Player)
    if pl is not None and lvl > 0:
        pl.tech[t] = max(0, pl.tech.get(t, 0) - lvl)
        N.eng.ue.sync_player(pl)                           # BUGS FF-UPG: the levels (and so their numbers) go down


# NAT-13 ReviveHero / ReviveHeroLoc (x hero siege 4 sites): a dead hero comes back at the point with full life and mana
# [inf], the order cleared, food counted again, the weapon / regeneration lists rejoined; the position change raises the
# enter-region events like SetUnitPosition. A hero that is alive (or not a hero) is not revived (False). The hero's items
# stay where they dropped, level / XP / skills are kept. This core raises no event (the altar queue raises them, engine.
# revive_finish; the natives ReviveHero(Loc) raise REVIVE_START / REVIVE_FINISH in _revive_native, BUGS BOSS-ANKH; the Ankh
# reincarnation none); a queued entry of this hero is cancelled with its refund and no event. A dead hero never decays (BONE decay does not remove it) [inf: the handle stays valid for ReviveHero].
def _revive_hero(N, uo, x, y, life=None, mana=None, food=True):
    """food False: an altar revive, whose food was already reserved when the queue slot started (§8.22)."""
    if uo is None or not uo.alive or not uo.dead or not getattr(uo, 'hero', False):
        return False
    uo.dead = False
    uo.death_time = None
    if uo in N.corpses:
        N.corpses.remove(uo)
    uo.life = uo.maxlife if life is None else life          # W9R3-1: an altar revive passes HeroReviveLifeFactor / ManaStart
    uo.mana = uo.maxmana if mana is None else mana
    N.eng.set_regen(uo)
    _refresh_fighter(N, uo)
    _set_pos_native(N, uo, x, y)
    N.eng.revive_cancel_hero(uo)          # W9R3-1: a queued altar revive of this hero ends with its refund, no event [inf]
    if food:
        N.food_add(uo.owner, int(uo.data.get('fused', 0) or 0), int(uo.data.get('fmade', 0) or 0))
    return True


def _revive_native(N, uo, x, y):
    """ReviveHero / ReviveHeroLoc (NOTES_heroes 6, 6.1 [obs 0x6f1b3160 + runtime probe hero_revive]): the same validity as
    _revive_hero; the unit's order is cleared (a dead unit has none: no-op here), then the food precondition: a food cost > 0
    and FOOD_USED + cost > FoodCeiling (the map's constant, not the player's FOOD_CAP) -> False with the hero still dead. On
    success EVENT_*_HERO_REVIVE_START (hero still dead), the revive with full life and the unit type's start mana
    (HeroReviveManaStart <> 0, else HeroReviveManaFactor x max mana, as the altar finish), then EVENT_*_HERO_REVIVE_FINISH
    in the same tick (GetRevivingUnit = GetTriggerUnit = hero, GetRevivableUnit null) [obs runtime]. The slot of START in
    the chain (before the state change) follows 'alive flag 0 at START, 1 at FINISH' [obs]; the rest is [inf]."""
    if uo is None or not uo.alive or not uo.dead or not getattr(uo, 'hero', False) or getattr(uo, 'illusion', False):
        return False
    uo.order = 0
    uo.target = None
    fused = int(uo.data.get('fused', 0) or 0)
    if fused > 0 and uo.owner.state.get(5, 0) + fused > N.eng.food_ceiling:
        return False
    N.hero_event(uo, 'REVIVE_START', {'reviving': N.H(uo)})
    if not uo.alive or not uo.dead:
        return True                      # a START trigger revived or removed it already [inf]
    eng = N.eng
    mana = r32(float(uo.data.get('mana0', 0) or 0)) if eng.rv_mana_start else r32(uo.maxmana * eng.rv_mana)
    _revive_hero(N, uo, x, y, uo.maxlife, min(mana, uo.maxmana))
    N.hero_event(uo, 'REVIVE_FINISH', {'reviving': N.H(uo)})
    return True


@nat('ReviveHero')
def _revivehero(N, u, x, y, eye):
    return _revive_native(N, N.o(u, Unit), x, y)


@nat('ReviveHeroLoc')
def _revivehero_loc(N, u, l, eye):
    lo = N.o(l, Location)
    return _revive_native(N, N.o(u, Unit), lo.x, lo.y) if lo is not None else False


# NAT-14 GetItemName: the item type's display name (null when it has none)
@nat('GetItemName')
def _getitemname(N, i):
    it = _item(N, i)
    nm = it.data.get('name') if it is not None else None
    return nm if nm else None
# ================================================================ END NAT-8 .. NAT-14


# ================================================================ BEGIN W9-5: unit-scoped events (tools/jvm/unitevents.py)
# Udon twin: W3NatEvents.cs (impl ids 1320-1339). Clean room: behaviour only; tags [obs] / [ext] / [inf] in unitevents.py.
# Getter set of the handlers the audited maps use (read in work/maps/<key>/war3map.j, 2026-10-27):
#   death:      Wintermaul V1.51 / X10 (CustomDefeatBJ(Player(k))) and Skibi 10 (DisableTrigger(GetTriggeringTrigger()), a
#               condition on globals) read no event getter; Castle Fight: GetTriggerDestructable (a tree: wait 30 s, restore)
#   acquired:   Legion TD: GetTriggerUnit            in range: Footman Frenzy: GetTriggerUnit, GetTriggeringTrigger;
#               Element TD: GetTriggerUnit, GetTriggeringTrigger            state: Legion TD, Element TD: GetTriggerUnit
# No audited map reads GetEventTargetUnit / GetEventUnitState; they are implemented as common.j declares them (the
# acquired / watched unit; the unitstate of the registration).
@nat('TriggerRegisterDeathEvent')
def _regdeath(N, t, w):
    return UE.reg_death(N, t, w)


@nat('TriggerRegisterUnitInRange')
def _reginrange(N, t, u, rng, filt):
    return UE.reg_inrange(N, t, u, rng, _bx(N, filt))


@nat('TriggerRegisterUnitStateEvent')
def _regunitstate(N, t, u, st, op, lim):
    return UE.reg_state(N, t, u, st, op, lim)


@nat('GetTriggerDestructable')
def _trigdestr(N):
    return UE.trigger_destructable(N)

# ================================================================ END W9-5


# ================================================================ BEGIN W9R2-1: random creeps, item fields, proper names
# tools/jvm/randdata.py (docs/NOTES_units.md "ChooseRandomCreep"); Udon twin: W3NatRand.cs (impl ids 1340-1349).
@nat('ChooseRandomCreep')
def _chooserandcreep(N, level):
    return RD.choose_random_creep(N, level)


@nat('ChooseRandomNPBuilding')
def _chooserandnp(N):
    return RD.choose_random_np_building(N)


@nat('SetItemUserData')
def _setitemud(N, i, v):
    RD.set_item_user_data(_item(N, i), v)


@nat('GetItemUserData')
def _getitemud(N, i):
    return RD.get_item_user_data(_item(N, i))


@nat('SetItemInvulnerable')
def _setiteminv(N, i, flag):
    RD.set_item_invulnerable(_item(N, i), flag)


@nat('IsItemInvulnerable')
def _isiteminv(N, i):
    return RD.is_item_invulnerable(_item(N, i))


@nat('GetHeroProperName')
def _heropname(N, u):
    return RD.hero_proper_name(N.o(u, Unit))

# ================================================================ END W9R2-1

# ================================================================ BEGIN W9R2-2: item pools + HERO_REVIVABLE (tools/jvm/itempool.py)
# Udon twin: W3NatPool.cs (impl ids 1350-1359). Clean room: behaviour only; tags [obs] / [inf] in itempool.py.
from . import itempool as IP  # noqa: E402,F401
# ================================================================ END W9R2-2

# ================================================================ BEGIN FOG: fog of war (tools/jvm/fog.py)
# Udon twin: W3Fog.cs (impl ids 1360-1399). Clean room: behaviour only; tags [obs] / [inf] in fog.py and docs/NOTES_fog.md.
from . import fog as FG  # noqa: E402,F401
# ================================================================ END FOG

# ================================================================ BEGIN PORT3-AI: computer-player AI natives
# Reference: docs/decomp/classes/Unclassed_Native_0001.md (§0 fields, §3 command stack, §4 MeleeDifficulty),
# Unclassed_Native_0003.md (§3 configuration word, §4 melee/campaign mode) and Unclassed_Native_0041.md
# (§1 start / CommandAI / PauseCompAI / GetAIDifficulty).  The AI script VM is still open (A-AI2); these natives
# own only the engine-side state the scripts call (tools/jvm/ai.py), plus the melee bot entry the port uses as
# its AI player.  Udon twin: W3Ai.cs (impl ids 1400-1439).

def _ai_slot(N, p):
    """the AI object of a player handle (None when the handle is bad or the player has no AI)."""
    pl = N.o(p, Player)
    return N.ais.get(pl.id) if pl is not None else None


def _ai_make(N, p, melee):
    pl = N.o(p, Player)
    if pl is None:
        return None
    a = N.ais.get(pl.id)
    if a is None:
        d = int(N.game.get('difficulty', 1) or 0)
        lv = N.ai_levels
        if lv is not None and 0 <= pl.id < len(lv) and 0 <= lv[pl.id] <= 2:
            d = lv[pl.id]                              # S7: the lobby's computer level of this slot (the original: the slot's AI difficulty)
        if d < 0 or d > 2:
            d = 1
        a = AI.AIPlayer(pl.id, d)
        a.on = True                                    # W3Ai.cs AiMake: aiOn[slot] = true
        if d == 0:
            a.flags |= AI.FLAG_EASY
        elif d == 2:
            a.flags |= AI.FLAG_HARD
        N.ais[pl.id] = a
    if melee:
        a.flags |= AI.FLAG_MELEE
    else:
        a.flags &= ~AI.FLAG_MELEE
    return a


def _ai_bot(N, slot):
    for b in N.bots:
        if b.slot == slot:
            return b
    return None


def _ai_start_bot(N, slot):
    # PORT3-AI model: the original starts the race .ai script in the CCompAI.  Until the AI VM exists the port's
    # AI player is the melee bot; it only acts on a melee start (own worker + the hall that trains it).
    from . import bot as B
    if slot not in N.bot_slots:
        B.LtwBot(N, slot, melee_only=True)


@nat('StartMeleeAI')
def _startmelee(N, p, script):
    a = _ai_make(N, p, True)
    if a is not None:
        if a.script is None:
            a.script = script
        N.ai_current = a                           # the started player is the current AI context
        h = N.ai_host                              # S1 (docs/ai_plan.md): the real race script, only when the host is on
        if h is not None and h.start(a.slot, script):
            return                                 # the script program owns the slot: no fallback bot
        _ai_start_bot(N, a.slot)


@nat('StartCampaignAI')
def _startcamp(N, p, script):
    a = _ai_make(N, p, False)
    if a is not None:
        if a.script is None:
            a.script = script
        N.ai_current = a


@nat('SetMeleeAI')
def _setmelee(N):
    if N.ai_current is not None:
        N.ai_current.flags |= AI.FLAG_MELEE       # 0003 §4.4: bit 14 set by SetMeleeAI, cleared by SetCampaignAI


@nat('SetCampaignAI')
def _setcamp(N):
    if N.ai_current is not None:
        N.ai_current.flags &= ~AI.FLAG_MELEE


@nat('PauseCompAI')
def _pauseai(N, p, pause):
    a = _ai_slot(N, p)
    if a is None:
        return
    N.ai_current = a
    a.pause += 1 if pause else -1                  # 0041 §1.3: a depth counter, never clamped
    b = _ai_bot(N, a.slot)
    if b is not None:
        # only the pause itself toggles the controller: a bot disabled for another reason stays disabled on resume
        if a.pause > 0 and b.enabled:
            b.enabled = False
            b.ai_paused = True
        elif a.pause <= 0 and getattr(b, 'ai_paused', False):
            b.enabled = True
            b.ai_paused = False


@nat('GetAIDifficulty')
def _getaidiff(N, p):
    a = _ai_slot(N, p)
    return a.difficulty if a is not None else 0    # 0041 §1.4: 0 for a player without an AI object


@nat('CommandAI')
def _commandai(N, p, command, data):
    a = _ai_slot(N, p)
    if a is not None:
        N.ai_current = a
        a.command(command, data)                   # 0001 §0.3 / CCompAI_p1 §6.11: silently drop at 12


@nat('CommandsWaiting')
def _cmdswaiting(N):
    return N.ai_current.commands_waiting() if N.ai_current is not None else 0


@nat('GetLastCommand')
def _lastcmd(N):
    return N.ai_current.last_command() if N.ai_current is not None else -1


@nat('GetLastData')
def _lastdata(N):
    return N.ai_current.last_data() if N.ai_current is not None else -1


@nat('PopLastCommand')
def _popcmd(N):
    if N.ai_current is not None:
        N.ai_current.pop_command()


@nat('MeleeDifficulty')
def _meleediff(N):
    # 0001 §4: bit 15 easy (1), bit 16 hard (3), neither normal (2); a missing AI reads the null object (normal).
    return N.ai_current.melee_difficulty() if N.ai_current is not None else 2


@nat('GetAiPlayer')
def _getaiplayer(N):
    return N.ai_current.slot if N.ai_current is not None else -1   # 0001 §6: -1 outside an AI script


@nat('DoAiScriptDebug')
def _aidebug(N):
    return False                                   # 0001 §2.1: constant false in both builds


@nat('GetEnemyPower')
def _enemypower(N):
    return 0                                       # 0003 §6.3: constant 0 in both builds


def _ai_flag(N, mask, on):
    if N.ai_current is not None:
        N.ai_current.set_flag(mask, on)


@nat('SetNewHeroes')
def _setnewheroes(N, on):
    _ai_flag(N, AI.FLAG_NEW_HEROES, on)


@nat('SetHeroesFlee')
def _setheroesflee(N, on):
    _ai_flag(N, AI.FLAG_HEROES_FLEE, on)


@nat('SetUnitsFlee')
def _setunitsflee(N, on):
    _ai_flag(N, AI.FLAG_UNITS_FLEE, on)


@nat('SetGroupsFlee')
def _setgroupsflee(N, on):
    _ai_flag(N, AI.FLAG_GROUPS_FLEE, on)


@nat('SetSlowChopping')
def _setslowchop(N, on):
    _ai_flag(N, AI.FLAG_SLOW_CHOPPING, on)


@nat('SetTargetHeroes')
def _settargetheroes(N, on):
    _ai_flag(N, AI.FLAG_TARGET_HEROES, on)


@nat('SetHeroesTakeItems')
def _setheroestake(N, on):
    _ai_flag(N, AI.FLAG_HEROES_TAKE_ITEMS, on)


@nat('SetPeonsRepair')
def _setpeonsrepair(N, on):
    _ai_flag(N, AI.FLAG_PEONS_REPAIR, on)


@nat('SetIgnoreInjured')
def _setignoreinjured(N, on):
    _ai_flag(N, AI.FLAG_IGNORE_INJURED, on)


@nat('SetWatchMegaTargets')
def _setwatchmega(N, on):
    _ai_flag(N, AI.FLAG_WATCH_MEGA, on)


@nat('SetRandomPaths')
def _setrandompaths(N, on):
    _ai_flag(N, AI.FLAG_RANDOM_PATHS, on)


@nat('SetDefendPlayer')
def _setdefendplayer(N, on):
    _ai_flag(N, AI.FLAG_DEFEND_PLAYER, on)


@nat('SetHeroesBuyItems')
def _setheroesbuy(N, on):
    _ai_flag(N, AI.FLAG_HEROES_BUY_ITEMS, on)


@nat('SetReplacementCount')
def _setrepl(N, n):
    if N.ai_current is not None:
        N.ai_current.repl = int(n)                 # 0003 §3: full dword, reset value 3, no clamp


@nat('SetHeroLevels')
def _setherolevels(N, code):
    if N.ai_current is not None:
        N.ai_current.hero_lv = int(code)           # 0003 §3: stores the level-up code (0 clears); no consumer yet


@nat('RecycleGuardPosition')
def _recycleguard(N, u):
    # 0041 §1.5: remove the old guard point, then re-register the unit's current position.
    uo = N.o(u, Unit)
    if uo is not None and uo.alive and not uo.dead:
        uo.guard = True
        uo.guard_x, uo.guard_y = uo.x, uo.y
        uo.guard_away = False
        uo.guard_due = -1
        N.eng.guard_arm(uo)
# ---- S2 (docs/ai_plan.md): census natives, read-only queries of the engine state (tools/jvm/aicensus.py, twin
# W3AiCensus.cs).  The unit costs / build time / food natives the scripts call are the map VM's own (static type fields).
@nat('GetUnitCountDone')
def _aicount_done(N, tid):
    return AC.unit_count(N, N.ai_current, tid, True)


@nat('GetUnitCount')
def _aicount(N, tid):
    return AC.unit_count(N, N.ai_current, tid, False)


@nat('GetTownUnitCount')
def _aitowncount(N, tid, town, done):
    return AC.town_unit_count(N, N.ai_current, tid, town, bool(done))


@nat('GetPlayerUnitTypeCount')
def _aiptypecount(N, p, tid):
    pl = N.o(p, Player)
    return AC.count(N, pl, tid, False) if pl is not None and pl.id < 12 else 0  # 0001 §6: 0 when the handle does not resolve; neutral slots 0 [inf] (C# p < 12)


@nat('GetUpgradeLevel')
def _aiupglevel(N, rid):
    return AC.upgrade_level(N, N.players[N.ai_current.slot], rid) if N.ai_current is not None else 0


@nat('GetUpgradeGoldCost')
def _aiupgcostg(N, rid):
    return AC.upgrade_cost(N, N.ai_current, rid, 0)


@nat('GetUpgradeWoodCost')
def _aiupgcostw(N, rid):
    return AC.upgrade_cost(N, N.ai_current, rid, 1)


@nat('GetMinesOwned')
def _aiminesowned(N):
    return AC.mines_owned(N, N.ai_current)


@nat('GetGoldOwned')
def _aigoldowned(N):
    return AC.gold_owned(N, N.ai_current)


@nat('TownWithMine')
def _aitownwithmine(N):
    return AC.town_with_mine(N, N.ai_current)


@nat('TownHasMine')
def _aitownhasmine(N, i):
    return AC.town_has_mine(N, N.ai_current, i)


@nat('TownHasHall')
def _aitownhashall(N, i):
    return AC.town_has_hall(N, N.ai_current, i)

# ---- S3 / S4 (docs/ai_plan.md): build planner, expansion and harvest natives (tools/jvm/aibuild.py, twin W3AiBuild.cs).
@nat('SetProduce')
def _aisetproduce(N, qty, tid, town):
    return AB.set_produce(N, N.ai_current, qty, tid, town)


@nat('SetUpgrade')
def _aisetupgrade(N, rid):
    AB.set_upgrade(N, N.ai_current, rid)
    return False                                   # Unclassed_Native_0001 s10.2: the worker's answer is discarded


@nat('SetExpansion')
def _aisetexpansion(N, u, tid):
    return AB.set_expansion(N, N.ai_current, N.o(u, Unit), tid)


@nat('GetNextExpansion')
def _aigetnextexp(N):
    return AB.next_expansion(N, N.ai_current)


@nat('GetExpansionPeon')
def _aiexppeon(N):
    return AB.expansion_peon(N, N.ai_current)


@nat('GetExpansionFoe')
def _aiexpfoe(N):
    return AB.expansion_foe(N, N.ai_current)


@nat('GetExpansionX')
def _aiexpx(N):
    return AB.expansion_xy(N, N.ai_current, 0)


@nat('GetExpansionY')
def _aiexpy(N):
    return AB.expansion_xy(N, N.ai_current, 1)


@nat('HarvestGold')
def _aiharvestgold(N, town, peons):
    AB.harvest(N, N.ai_current, 1, town, peons)


@nat('HarvestWood')
def _aiharvestwood(N, town, peons):
    AB.harvest(N, N.ai_current, 2, town, peons)


@nat('ClearHarvestAI')
def _aiclearharvest(N):
    AB.clear_harvest(N, N.ai_current)


@nat('StopGathering')
def _aistopgathering(N):
    AB.stop_gathering(N, N.ai_current)

# ---- S5a (docs/ai_plan.md): captains, assault groups and the attack intel (tools/jvm/aicaptain.py, twin W3AiCaptain.cs).
@nat('CreateCaptains')
def _aicreatecaptains(N):
    CP.create(N, N.ai_current)


@nat('SetCaptainHome')
def _aisetcaptainhome(N, which, x, y):
    CP.set_home(N, N.ai_current, which, x, y)


@nat('ResetCaptainLocs')
def _airesetcaptainlocs(N):
    if N.ai_current is not None and N.ai_current.cp_on:
        CP.reset_locs(N, N.ai_current)


@nat('ShiftTownSpot')
def _aishifttownspot(N, x, y):
    CP.set_home(N, N.ai_current, 3, x, y)


@nat('SetStagePoint')
def _aisetstagepoint(N, x, y):
    CP.set_stage(N, N.ai_current, x, y)


@nat('ClearCaptainTargets')
def _aiclearcaptaintargets(N):
    CP.clear_targets(N, N.ai_current)


@nat('SetCaptainChanges')
def _aisetcaptainchanges(N, on):
    CP.set_flag(N.ai_current, 1, on)


@nat('SetSmartArtillery')
def _aisetsmartart(N, on):
    CP.set_flag(N.ai_current, 2, on)


@nat('GroupTimedLife')
def _aigrouptimedlife(N, on):
    CP.set_flag(N.ai_current, 4, on)


@nat('CaptainVsUnits')
def _aicaptainvsunits(N, p):
    if N.o(p, Player) is not None:                  # 0002 s2.1: a zero argument returns false and changes nothing
        CP.set_vs(N.ai_current, 1)


@nat('CaptainVsPlayer')
def _aicaptainvsplayer(N, p):
    pl = N.o(p, Player)
    if pl is not None:
        CP.set_vs(N.ai_current, 2 + pl.id)


@nat('CaptainAttack')
def _aicaptainattack(N, x, y):
    CP.attack_move_xy(N, N.ai_current, x, y)


@nat('AttackMoveXY')
def _aiattackmovexy(N, x, y):
    CP.attack_move_xy(N, N.ai_current, x, y)


@nat('AttackMoveKill')
def _aiattackmovekill(N, u):
    CP.attack_move_kill(N, N.ai_current, N.o(u, Unit))


@nat('CaptainGoHome')
def _aicaptaingohome(N):
    CP.go_home(N, N.ai_current)


@nat('CaptainIsHome')
def _aicaptainishome(N):
    return CP.is_home(N, N.ai_current)


@nat('CaptainIsFull')
def _aicaptainisfull(N):
    return CP.is_full(N, N.ai_current)


@nat('CaptainIsEmpty')
def _aicaptainisempty(N):
    return CP.group_size(N, N.ai_current) == 0


@nat('CaptainGroupSize')
def _aicaptaingroupsize(N):
    return CP.group_size(N, N.ai_current)


@nat('CaptainReadiness')
def _aicaptainreadiness(N):
    return CP.readiness(N, N.ai_current, True, True)


@nat('CaptainReadinessHP')
def _aicaptainreadinesshp(N):
    return CP.readiness(N, N.ai_current, True, False)


@nat('CaptainReadinessMa')
def _aicaptainreadinessma(N):
    return CP.readiness(N, N.ai_current, False, True)


@nat('CaptainRetreating')
def _aicaptainretreating(N):
    return CP.retreating(N.ai_current)


@nat('CaptainInCombat')
def _aicaptainincombat(N, attack):
    return CP.in_combat(N, N.ai_current, bool(attack))


@nat('CaptainAtGoal')
def _aicaptainatgoal(N):
    return N.ai_current is not None and N.ai_current.cp_on and N.ai_current.cp_atg[0]


@nat('InitAssault')
def _aiinitassault(N):
    CP.init_assault(N, N.ai_current)


@nat('AddAssault')
def _aiaddassault(N, qty, tid):
    return CP.add_assault(N, N.ai_current, 0, qty, tid)


@nat('AddDefenders')
def _aiadddefenders(N, qty, tid):
    return CP.add_assault(N, N.ai_current, 1, qty, tid)


@nat('GetCreepCamp')
def _aigetcreepcamp(N, lo, hi, flyers):
    return CP.creep_camp(N, N.ai_current, lo, hi, bool(flyers))


@nat('StartGetEnemyBase')
def _aistartgetenemybase(N):
    CP.start_enemy_base(N, N.ai_current)


@nat('WaitGetEnemyBase')
def _aiwaitgetenemybase(N):
    return False


@nat('GetEnemyBase')
def _aigetenemybase(N):
    return CP.enemy_base(N, N.ai_current)


@nat('TownThreatened')
def _aitownthreatened(N):
    return CP.town_threatened(N.ai_current)


@nat('GetMegaTarget')
def _aigetmegatarget(N):
    return None


@nat('SetAllianceTarget')
def _aisetalliancetarget(N, u):
    CP.set_alliance_target(N.ai_current, N.o(u, Unit))


@nat('GetAllianceTarget')
def _aigetalliancetarget(N):
    return CP.alliance_target(N, N.ai_current)


@nat('UnitInvis')
def _aiunitinvis(N, u):
    return CP.unit_invis(N, N.ai_current, N.o(u, Unit))   # 0002 s3.2: invisible to the AI's player (detector-aware)


@nat('CreepsOnMap')
def _aicreepsonmap(N):
    return CP.creeps_on_map(N)

# ---- S5b / S6 (docs/ai_plan.md): housekeeping, enemy intel, heroes, debug natives (tools/jvm/aiextra.py, twin W3AiExtra.cs).
# DisplayText* / Debug*: the existing no-op natives of the map VM (above) are the AI's too (0001 s2: a bare return)


@nat('RemoveInjuries')
def _airemoveinjuries(N):
    EX.remove_injuries(N, N.ai_current)


@nat('RemoveSiege')
def _airemovesiege(N):
    EX.remove_siege(N, N.ai_current)


@nat('IgnoredUnits')
def _aiignoredunits(N, tid):
    return EX.ignored_units(N, N.ai_current, tid)


@nat('GetEnemyExpansion')
def _aigetenemyexpansion(N):
    return EX.enemy_expansion(N, N.ai_current)


@nat('IsTowered')
def _aiistowered(N, u):
    return EX.is_towered(N, N.ai_current, N.o(u, Unit))


@nat('TeleportCaptain')
def _aiteleportcaptain(N, x, y):
    def place(uo, px, py):
        _set_pos(N, uo, px, py)
        N.update_regions(uo)                           # the region events of the next step (not the sync filters of the native)
    EX.teleport(N, N.ai_current, x, y, place)


@nat('AddGuardPost')
def _aiaddguardpost(N, tid, x, y):
    EX.add_guard_post(N.ai_current, tid, x, y)


@nat('FillGuardPosts')
def _aifillguardposts(N):
    EX.fill_guard_posts(N, N.ai_current)


@nat('ReturnGuardPosts')
def _aireturnguardposts(N):
    EX.return_guard_posts(N, N.ai_current)


@nat('DisablePathing')
def _aidisablepathing(N):
    EX.set_cp_flag(N.ai_current, EX.CF_NOPATH)


@nat('SetAmphibious')
def _aisetamphibious(N):
    EX.set_cp_flag(N.ai_current, EX.CF_AMPHIB)


@nat('GetBuilding')
def _aigetbuilding(N, p):
    return EX.get_building(N, N.ai_current, N.o(p, Player))


@nat('Unsummon')
def _aiunsummon(N, u):
    pass                                               # open: the Auns ability (0002 s3.3) has no engine path


@nat('MergeUnits')
def _aimergeunits(N, qty, a, b, make):
    return EX.merge_units(N, N.ai_current, qty, a, b, make)


@nat('ConvertUnits')
def _aiconvertunits(N, qty, tid):
    return EX.convert_units(N, N.ai_current, qty, tid)


@nat('PurchaseZeppelin')
def _aipurchasezeppelin(N):
    EX.purchase_zeppelin(N, N.ai_current)


@nat('LoadZepWave')
def _ailoadzepwave(N, x, y):
    if N.ai_current is not None:
        N.ai_current.zp_x = float(x)                   # open: cargo loading has no engine path
        N.ai_current.zp_y = float(y)


@nat('SuicidePlayer')
def _aisuicideplayer(N, p, check):
    return False                                       # 0003 s2: the documented refusal (open: campaign suicide mode)


@nat('SuicidePlayerUnits')
def _aisuicideplayerunits(N, p, check):
    return False


@nat('SuicideUnit')
def _aisuicideunit(N, n, tid):
    pass


@nat('SuicideUnitEx')
def _aisuicideunitex(N, n, tid, pid):
    pass


@nat('GetHeroId')
def _aigetheroid(N):
    return EX.hero_id(N, N.ai_current)


@nat('GetHeroLevelAI')
def _aigetherolevelai(N):
    return EX.hero_level(N, N.ai_current)

# ================================================================ END PORT3-AI


# ================================================================ BEGIN PORT5-NativesB round 3 (impl ids 1626 / 1627)
# Udon twin: W3NatB.cs cases 1626 / 1627 (NbDamagePoint, NbDpTick, NbScoreKill); clean room: values and behaviour only.

def _score_kill(N, u, killer):
    """GetPlayerScore counters 1 (units killed), 3 (structures razed), 7 (heroes killed): the killer's owner when it is not an
    ally of the victim's owner [inf: the engine's kill bookkeeping is not decoded; common.j index meaning only]."""
    if killer is None or killer.owner is None or u.owner is None:
        return
    ko = killer.owner
    if N.eng.ally(u.owner, ko):
        return
    if u.data.get('bldg'):
        ko.score[3] += 1
    else:
        ko.score[1] += 1
    if getattr(u, 'hero', False):
        ko.score[7] += 1


def _dp_collect(N, x, y, radius):
    """the units an UnitDamagePoint splash reaches (Unclassed_Native_0034 §2): a single ring, factor 1, around (x, y). The target
    filter behind the engine's selector word is not decoded [inf]: every living, visible unit whose centre is inside the radius,
    in creation order, no alliance test (the damage pipeline decides; an invulnerable unit gets a zero record)."""
    hits = []
    if radius < 0:
        return hits
    for c in N.eng.query(x, y, radius):
        N.eng.wk += 1
        if not c.alive or c.dead or c.hidden:
            continue
        dx, dy = c.x - x, c.y - y
        if dx * dx + dy * dy <= radius * radius:
            hits.append(c)
    hits.sort(key=lambda c: c.serial)
    return hits


def _dp_apply(N, src_h, hits, amount, atk, dmg, flags):
    src = N.o(src_h, Unit)
    for c in hits:
        N.eng.damage(c, src, amount, atk if atk < 7 else 0, dmg & 31, bool(flags & 1), bool(flags & 2))


@nat('UnitDamagePoint')
def _unitdamagepoint(N, u, delay, radius, x, y, amount, attack, ranged, atk, dmg, weapon):
    """Unclassed_Native_0034 §2 [obs]: a bad unit -> false; attack type > 7, weapon type > 24 or a damage type with the high bit
    set -> false (damage types above 31 are NOT rejected, they wrap with & 31 - the difference to UnitDamageTarget); delay <= 0
    hits now, else at the step the delay has passed. true = handed to the engine, whatever it hits; a negative amount heals."""
    uo = N.o(u, Unit)
    if uo is None or atk < 0 or atk > 7 or weapon < 0 or weapon > 24 or dmg < 0:
        return False
    flags = (1 if attack else 0) | (2 if ranged else 0)
    if delay <= 0:
        _dp_apply(N, u, _dp_collect(N, x, y, radius), amount, atk, dmg, flags)
    else:
        N.dp_pend.append((N.vm.qgame.clock + delay, u, x, y, radius, amount, atk, dmg, flags))
    return True


def _dp_tick(N):
    now = N.vm.qgame.clock
    due = [e for e in N.dp_pend if e[0] <= now]
    if not due:
        return
    N.dp_pend = [e for e in N.dp_pend if e[0] > now]
    sel = [_dp_collect(N, e[2], e[3], e[4]) for e in due]       # every due splash picks its units before any of them hits
    for e, hits in zip(due, sel):
        _dp_apply(N, e[1], hits, e[5], e[6], e[7], e[8])
# ================================================================ END PORT5-NativesB round 3
