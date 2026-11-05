#!/usr/bin/env python3
"""W5 engine core of the reference world (Python; the Udon port mirrors it): unit state, orders, ground pathing,
acquisition + attack cycle, projectiles, splash / bounce, the damage pipeline, death / bounty / decay, building
placement + construction, and the player input queue. docs/engine_w5.md describes rules, step order and the
assumptions (A1..); every rule cites its NOTES section.

Units stay `natives.Unit` objects (fields = flat ints / floats / refs, one per Udon parallel array); lists are kept
in creation (serial) order so every iteration is deterministic; times are integer 5 ms steps (200 per second).
Every loop iteration charges `self.wk` (+ grid.wk) — the work counter the Udon port charges against its budget.
"""
import heapq
import os
import math

from .vm import r32, Agent
from . import unitdata as UD
from . import ai as AI
from .pathing import Grid, MARK_X, GATE_WAIT
from . import pathing as PTH
from . import pathfield as PF
from .pathab import AbRoute
from . import unitevents as UE
from .upgeff import UpgradeEffects

STEP = 0.005


def cos_sin(x):
    """cos and sin by their series (no libm: Python and C# produce the same bits); |x| <= pi + 1 (STEER, BUGS PATH-AB-STEER)."""
    x2 = x * x
    c = tc = 1.0
    sn = ts = x
    for k in range(1, 14):
        tc = -tc * x2 / ((2 * k - 1) * (2 * k))
        ts = -ts * x2 / ((2 * k) * (2 * k + 1))
        c += tc
        sn += ts
    return c, sn


SPS = 200                           # steps per second
RECENT_ATK_STEPS = 10 * SPS         # CAbilityAttack recent-attacker slot life 10 s (NOTES_combat §7.3 / §7.11)
RECENT_ATK_SLOTS = 8
DEATH_LIFE = r32(0.4)               # NOTES_combat §4 threshold 0.4: destructables / items (items: PORT9 NOTES_items §3.2 own 0.4 rule)
UNIT_DEATH_LIFE = r32(0.405)        # [obs runtime 1.26a] docs/oracle.md "life kill threshold" (ext_combat): a UNIT with life <= 0.405 dies, 0.41 lives
ARRIVE_EPS = 0.5                    # W3 mini world arrival tolerance (kept)
ARRIVE_SHORT = os.environ.get('W3_ARRIVE_SHORT') == '1'   # W3_ARRIVE_SHORT=1 switches it on (measurements, the harness twin)
                                    # [obs runtime 1.26a] docs/oracle.md "move arrival" (ext_combat): the real order ends 8.5-14.5 u short of the goal (within 15.68 = FOLLOW_R),
                                    # not 0.5. The PATH_AB movers (melee maps) already arrive that way (step_to); this switch gives the legacy seg_step movers (every other map)
                                    # the same rule: arrived when rem <= FOLLOW_R at the start of a tick (no step, no snap). Default OFF: it changes every fan-map trace
                                    # (docs/oracle.md notes the measurement). The twin is W3Vm.arriveShort.
MOVE_EVERY = 16                     # A19c: a mover advances every 16th step (by 16 × the step distance), staggered by serial
                                    # (was 4, then 8; A19c-8 / A19c-16: same outcome over many seeds, invisible)
MOVE_EVERY_AB = 6                   # STEER (switch PATH_AB on): the movement tick of the original is 0.03 s = 6 steps [obs 0x6f32c440, NOTES_pathing 8.1]
COLL_WAIT_AB = 66                   # ... A-COL1 wait in those ticks (~2 s)
ARRIVE_STOP = 11.5                  # ARRIVE_SHORT: the stop distance when one stand-in tick crosses the ring: the middle of the observed 8.5 .. 14.5 [obs runtime 1.26a]
FOLLOW_R = 15.68                    # STEER [obs 0x6f67b2a0]: a mover is arrived within the goal radius max(r, 0.49 cells) = 15.68 world units at the start of a tick (no step, no snap)
COLL_WAIT = 25                      # A-COL1: a mover waits at most 25 of its movement steps (~2 s) behind an occupied
                                    # cell, then squeezes in (no lock between units walking against each other)
OCCS_EVERY = 40                     # PATH-AB step 4 [inf]: the standing units' cells are counted anew every 40 steps (0.2 s)
OCC_REBUILD = 200                   # A-COL1: the cell occupancy is counted anew every 200 steps (1 s; teleports, spawns)
MISSILE_EVERY = 4                   # A19d: a missile advances every 4th step (by 4 × the step distance)
PB_STEP = 1370                      # A-PB1: path budget per player and step (oracle wm_pathbudget: 100 straight requests
                                    # served in ~0.3 s, 100 far searches of ~15.3k expansions at ~17/s, in pairs)
PB_REQ = 800                        # A-PB1: fixed cost of one path request; + 1 per A* expansion
PB_COLL = 10500                    # A-PB2: + cost of a collision repath (CROWD-1 refit with A-PB4 / A-PB5: oracle wmsolo lives
                                    # 18 / 15 / 13 / 11 / 9 at 210 .. 250 s; 9000 -> 17 / 15 / 12 / 10 / 7 / 5 at 200 .. 250,
                                    # 11000 -> 20 / 19 / 19 / 18 / 16 / 15: smooth in the cost, equal for six seeds)
PATH_TICK = 12                      # PATH-AB step 3: steps per pathing tick [inf: NOTES_pathing 2.5, 1100 pops per 2 ticks = the 17 far searches a second of oracle pbl -> ~59 ms = 12 steps of 5 ms]
PB_THROTTLE = 10 * PATH_TICK        # PATH-AB: one search of each kind per mover per 10 ticks [obs 0x6f67f220]
PB_BLOCK_WAIT = 4 * PATH_TICK       # PATH-AB: a blocked mover waits at least 4 ticks before its re-search is served (the blocker moving; 20 ticks when it stands) [obs NOTES_pathing 2.7]
PB_COLL_AB = 330                    # PATH-AB: pops a blocked mover's re-search (search B around the crowd) uses up in the player's B record. FITTED [inf], the one constant that stands in for the crowd
                                    # (MOVING units as obstacles, the 4 / 20 tick blocker waits, steering / pushing are not modelled: a wave that only waits 4 ticks per moving blocker runs the Wintermaul lane in 20 s,
                                    # measured): oracle wmsolo lives 18 / 15 / 8 at 210 / 225 / 255 s = 19 / 15 / 9 here (PATH-AB-3: 340 = 20 / 17 / 10); at 320 and below the first wave of maze.py runs through and the game ends before 300 s, at 340 wmsolo leaves its window (17 .. 19 at 210 s)
AB_ARR2 = 245.8624                  # PATH-AB step 4: the follower has arrived at a route point within 0.49 cells (0.49 x 0.49 cells^2 = 245.8624 world units^2) [obs 2.6 0x6f67fdc0]
AB_RETRY_NEAR = 2                   # the blocked handler's retry counter [obs 2.7 0x6f67f140]: 2 when the goal is within 12 A0 cells [inf: A0 cells, NOTES_pathing 8.x of steer], ...
AB_RETRY_FAR = 7                    # ... else 7 + a small random number [inf: the unit serial's parity, the original's random generator is not the game's]
AB_CLS_ON = os.environ.get('W3_AB_CLS', '1') == '1'   # STEER-4: unit size classes in search B and the sight (W3_AB_CLS=0: all class 0, the stage 3 behaviour; measurements only, the C# twin has no switch)
AB_PASS_ON = os.environ.get('W3_AB_PASS', '1') == '1'   # STEER-4: head-on passing of group-order movers (W3_AB_PASS=0: stage 3; measurements only)
REVIVE_OVERFLOW = os.environ.get('W3_REVIVE_OVERFLOW') == '1'   # PORT14-Hero: CAbilityRevive §8.24 step 7 (the six queued
                                    # slots are full) pays the revive and stores nothing. Default OFF: the chapter marks the
                                    # branch [Open] (whether the order system prevents it) and copying the loss would make a
                                    # fun map that over-queues revives lose gold; OFF keeps the safe refusal (0x20). Twin: W3Vm.reviveOverflow.
AB_PASS_DOT = -0.5                  # STEER-4 [inf]: two movers whose headings differ by more than 120 degrees pass through each other
AB_BOX_MAX = 50                     # STEER-2 [inf]: a boxed-in unit does not count at most 50 empty searches in a row (~3 s), then the retry counter
AB_RETRY_D2 = 589824.0              # (12 A0 cells = 768 world units)^2
PB_NEVER = -(1 << 30)               # a throttle stamp that never blocks
PB_CHEAP_A = 32                     # a search A with fewer pops / B with fewer pops does not set the throttle [obs 0x6f67f4f0]
PB_CHEAP_B = 64
PB_REC = ((800, 3), (300, 2), (900, 2), (1100, 1))      # PATH-AB: the player's four quota records [obs 2.5]: pops per window, period; `used` resets every period + 1 ticks.
                                                         # R0 = cluster A (budget > 400), R1 = group flag A, R2 = A of a single unit (budget <= 400), R3 = B
PB_SYNC_MAX = 8                     # ticks a record catches up at most (an idle player: its windows are long over)
PATH_PIECE = 500                    # A-PB4: a path search that needed more A* expansions than this is followed only for the
                                    # share PATH_PIECE / expansions of the route (the limited search of the original), then asked for again
PIECE_MIN = 300.0                   # A-PB4: shortest piece of such a route (world units)
PB_EXP = 31                         # A-PB4: budget cost of one A* expansion, at most PATH_PIECE of them: a search that hit the
                                    # limit costs 800 + 15 500 (oracle pbl: 100 far searches at ~17 / s)
SPLASH_COLL_Q = 256.0               # BUGS SPL-COLL: splash queries reach this far beyond the largest ring (collision sizes)
BKT = 1024.0                        # spatial bucket size of the unit index (scan / splash / bounce queries)
BUILD_REACH = 64.0                  # A12: builder starts construction within half footprint + 64
MINE_BLOCK_R = 750.0                # HALL-MINE: a gold-accepting hall type is refused (0x47) within this of a gold mine [obs 0x2ee in 0x6f2a3ce0]
CONSTRUCT_LIFE0 = 0.1             # A13: construction starts at 10 % life, rises linearly over bldtm

# order ids (NOTES_units §1)
O_SMART, O_STOP, O_ATTACK, O_ATTACKONCE, O_MOVE, O_AIMOVE, O_PATROL, O_HOLD = (851971, 851972, 851983, 851985,
                                                                              851986, 851988, 851990, 851993)
PATROL_MIN = 100.0                 # MOVE-PORT [obs runtime 1.26a R3 / NOTES_class_CAbilityMove §5.3]: a patrol point nearer than this is dropped
O_PATROL2 = 851991                 # 0xd0017: the order id a patrolling unit reports (GetUnitCurrentOrder) [obs R3]
FOLLOW_RANGE = 300.0               # MOVE-PORT FollowRange / StructureFollowRange (NOTES_class_CAbilityMove §5.2 [obs] 300 / 100)
FOLLOW_RANGE_BLDG = 100.0
FLEE_DIST = 300.0                  # MOVE-PORT NOTES_class_CAbilityMove §8
FLEE_CREEP_DIST = 1200.0           # [obs runtime 1.26a] a creep-owned worker runs ~1200 (CAbilityMove_p2 §6.1 step 4); heroes never flee
FLEE_CREEP_OID = 852018            # [obs runtime 1.26a] docs/oracle.md "flee": a creep-owned worker's flee reports order 0xd0012 (distance ~1200)
FLEE_MIN_SPEED = 100.0             # MOVE-PORT [obs CAbilityMove_p2 §7.4]: the flee decision refuses below speed 100
O_SETRALLY = 851980                # "setrally"; smart is remapped to it by the rally ability (NOTES_orders.md)
O_ATTACKGROUND = 851984           # "attackground": artillery / msplash weapons fire at a ground point (BUGS ATK-GND)
O_CANCEL = 851976                  # "cancel": the train queue drops its last slot (NOTES_production §1.6 [inf])
O_ROOT = 852165                    # 0xd00c5 root (CAbilityRoot §6)
O_UNROOT = 852166                  # 0xd00c6 unroot
O_CANCEL2 = 851978                 # 0xd000a: the second cancel id (CAbilityResearch §4.1, CAbilityQueue §4.33)
# damage pipeline masks (NOTES_combat §1.2 step 2)
MAGICAL_MASK = 0x033ee700
PHYSICAL_MASK = 0x00c11830
DMG_NORMAL = 4                      # DAMAGE_TYPE_NORMAL -> mask 0x10
NEG_ARMOR = (0, -0.06, -0.1164, -0.16941, -0.21925, -0.26609, -0.31013, -0.35152, -0.39043, -0.427, -0.46138,
             -0.4937, -0.52407, -0.55263, -0.57947, -0.6047, -0.62842, -0.65072, -0.67167, -0.69137, -0.70989)
DB_KEYS = ('Spells', 'Normal', 'Pierce', 'Siege', 'Magic', 'Chaos', 'Hero')       # row order = attacktype index
ETH_ORDER = (1, 2, 3, 4, 5, 0, 6)   # EtherealDamageBonus list order NORMAL,PIERCE,SIEGE,MAGIC,CHAOS,SPELLS,HERO

PLAYER_GOLD, PLAYER_LUMBER, PLAYER_HERO_TOKENS, PLAYER_GIVES_BOUNTY = 1, 2, 3, 7


def fmt_g(v):
    from .natives import fmt_real                 # the trace's float32 format (U# FmtValue F_REAL)
    return fmt_real(v)


def _rc(s):
    return (ord(s[0]) << 24) | (ord(s[1]) << 16) | (ord(s[2]) << 8) | ord(s[3])


def parse_reqs(ids_s, amounts_s):
    """'hkee,hcas' + '1,2' (amount default 1) -> [(rawcode, amount)] — a unit type's Requires / Requiresamount and the
    per-level research lists (BUGS ETD-ORA3)."""
    ids = [t.strip() for t in str(ids_s or '').split(',') if len(t.strip()) == 4]
    am = [t.strip() for t in str(amounts_s or '').split(',')]
    out = []
    for i, t in enumerate(ids):
        try:
            n = int(am[i]) if i < len(am) and am[i] else 1
        except ValueError:
            n = 1
        out.append((_rc(t), max(n, 1)))
    return out


def steps_of(t):
    """seconds -> whole steps, rounded up, at least 1 (A2)."""
    n = int(math.ceil(t * SPS - 1e-6))
    return n if n > 0 else 1


def _f(misc, key, default):
    try:
        return float(str(misc.get(key, default)).split(',')[0])
    except ValueError:
        return default


def _flist(misc, key, n, default):
    v = misc.get(key)
    if v is None:
        return list(default)
    out = []
    for p in str(v).split(','):
        try:
            out.append(float(p))
        except ValueError:
            out.append(0.0)
    return (out + list(default)[len(out):])[:n]



def rally_dest_alive(t):
    """a rally destructable lives: not killed (dalive), life above 0, not removed (= C# DestAlive) [obs]."""
    return bool(getattr(t, 'dalive', False)) and t.life > 0.0 and not getattr(t, 'removed', False)


class DestRec(Agent):
    """a destructable the game placed from war3map.doo (BUGS DS-2). It has no JASS handle until a script gets hold of it
    (EnumDestructablesInRect, BUGS W9R3-4-CHA): then it is handled like a CreateDestructable one (tid, life = the type's HP
    or 0 when it starts dead)."""
    tname = 'destructable'

    def __init__(self, id4, x, y, facing, alive, hp=100.0):
        self.id4, self.x, self.y, self.facing, self.dalive, self.fp = id4, x, y, facing, alive, None
        self.fpalive = alive            # the texture / footprint currently registered (dalive = the life-based alive state)
        self.tid = (ord(id4[0]) << 24) | (ord(id4[1]) << 16) | (ord(id4[2]) << 8) | ord(id4[3])
        self.maxlife = hp
        self.life = hp if alive else 0.0


DEST_SERIAL = 2000000            # BUGS W9-PYR-DEST: input serial base of destructables (units < 1000000, items from 1000000)
HERO_TECH = 0x4845524F           # 'HERO': SetPlayerMaxHeroesAllowed = SetPlayerTechMaxAllowed(p, 'HERO', n) (Blizzard.j)

class Pt:
    """a ground point as an attack target (BUGS ATK-GND): the unit-shaped fields in_range / facing_ok / damage_point read."""
    __slots__ = ('x', 'y')
    coll = 0.0
    alive = True
    dead = False
    serial = 0

    def __init__(self, x, y):
        self.x, self.y = x, y


class DestPt:
    """BUGS W9-PYR-DEST: a destructable as an attack target (explicit order only, a unit never acquires one). It rides the
    ground-target machinery (u.gpt): range / facing / chase read x, y, which is the point of the destructable's footprint box
    that is nearest to the attacker (edge-to-edge reach, a gate is wider than the weapon range); alive turns false when it dies."""
    coll = 0.0
    dead = False
    hidden = False
    invisible = 0
    serial = 0

    def __init__(self, o, u, box):
        self.o, self.u, self.box = o, u, box

    @property
    def alive(self):
        return self.o.dalive and self.o.life > 0.0 and not getattr(self.o, 'removed', False)

    @property
    def x(self):
        b = self.box
        ux = self.u.x
        return b[0] if ux < b[0] else (b[2] if ux > b[2] else ux)

    @property
    def y(self):
        b = self.box
        uy = self.u.y
        return b[1] if uy < b[1] else (b[3] if uy > b[3] else uy)


class Missile:
    """one projectile (Udon: parallel arrays). kind = weapTp."""
    __slots__ = ('src', 'owner', 'target', 'x', 'y', 'tx', 'ty', 'speed', 'kind', 'amount', 'atk', 'w', 'left',
                 'hit', 'serial', 'hit_rec', 'evaded', 'rings0', 'mask0', 'pri')


def upkeep_tables(misc):
    """NOTES_production §2.3: (thresholds, gold tax, lumber tax) as ints / floats, 10 entries each. A Misc key that is present
    replaces the whole table (an entry that is not listed reads 0); otherwise the TFT built-in rows: 50, 80, 10000 x 8 /
    0.0, 0.3, then 0.6 x 8 / 0.0 x 10."""
    usage = [int(v) for v in _flist(misc, 'UpkeepUsage', 10, [0.0] * 10)] if 'UpkeepUsage' in misc else [50, 80] + [10000] * 8
    gold = [r32(v) for v in _flist(misc, 'UpkeepGoldTax', 10, [0.0] * 10)] if 'UpkeepGoldTax' in misc else [0.0, r32(0.3)] + [r32(0.6)] * 8
    lumber = [r32(v) for v in _flist(misc, 'UpkeepLumberTax', 10, [0.0] * 10)] if 'UpkeepLumberTax' in misc else [0.0] * 10
    return usage, gold, lumber


class Engine:
    def __init__(self, N, env, seed=1):
        self.N = N
        self.vm = N.vm
        misc = env.get('misc') or {}
        self.misc = misc
        # CGuardPost [obs NOTES_combat §7 / MiscData]: 600 leash distance, 1000 max chase, 5 s return delay.
        self.guard_dist = _f(misc, 'GuardDistance', 600.0)
        self.guard_wait = _f(misc, 'GuardReturnTime', 5.0)
        self.pathtex = env.get('pathtex') or {}
        self.reader = env.get('reader')
        wpm = env.get('wpm')
        ox, oy = env.get('origin') or (0.0, 0.0)
        self.pab = PTH.ab_for(N.vm.p)                 # BUGS PATH-AB-5: the two-level path finder for this map (melee maps; PATH_AB_ALL: every map)
        self.grid = Grid(wpm['w'], wpm['h'], wpm['cells'], ox, oy, self.pab) if wpm else None
        if self.grid is not None:
            self.grid.fld_on = PF.fld_for(N.vm.p)           # PATH-FIELD: flow fields for the validated maps only (pathfield.LIVE_MAPS)
            if self.grid.fld_on:
                if PF.fld_fast(N.vm.p):
                    self.grid.fld_n = PF.FLD_N_FAST         # ... and their demand profile (pathfield.FAST_MAPS)
                    self.grid.fld_win = PF.FLD_WIN_FAST
            elif PF.PATH_FIELD is None and not self.pab:
                self.grid.au_obs = True                     # PERF-AUTOFIELD: any other map: the automatic TD rule watches the requests (pathfield.AUTO_*)
        self.arrive_short = ARRIVE_SHORT                           # [obs runtime 1.26a] switch (default OFF), see ARRIVE_SHORT
        self.mv = MOVE_EVERY_AB if self.pab else MOVE_EVERY        # STEER: steps per movement tick (6 with the two-level finder: the original's 0.03 s; else the A19c stand-in 16)
        self.cw = COLL_WAIT_AB if self.pab else COLL_WAIT
        # BUGS DS-2: destructables block walking / building with their path texture (alive: pathTex, dead:
        # pathTexDeath); the ones the game places from war3map.doo have no handle here (Open: the game makes agents)
        self.dest_types = env.get('dest_types') or {}
        self.dests = []
        self.dest_seq = 0                            # BUGS W9-PYR-DEST: input serial of a destructable = 2000000 + n (placed ones first)
        self.dest_ser = {}
        self.reinc = []                              # BUGS BOSS-ANKH: (due step, hero, life) in death order
        self.dests_init = []                         # the placed ones as given (the rule-test export writes them to the pack)
        for id4, x, y, ang, life in env.get('dests') or ():
            self.dest_place(id4, x, y, ang, life)
        from .abilities import Abilities
        self.streams = N.streams                    # NOTES_abilities §4.2: the game streams (seeded with the JASS one)
        self.rng = N.streams.game[2]                # stream 2: attack dice, miss, evasion, first scan delay
        self.ab = Abilities(self, env.get('abilities'))  # W6 abilities / buffs / auras (abilities.py)
        from .harvest import Harvest
        self.hv = Harvest(self)                     # PORT-Harvest: worker trips, mines, wisp, eat tree
        from .spells import Spells
        self.sp = Spells(self)                      # W8 spell casting (spells.py, NOTES_production §5)
        from .heroes import Heroes
        self.hero = Heroes(self)                    # W8 heroes (heroes.py, NOTES_heroes)
        from .items import Items
        self.itm = Items(self)                      # W8 items in the world / inventories (items.py, NOTES_items)
        from .shops import Shops
        self.ue = UpgradeEffects(self)              # BUGS FF-UPG: research effects on units (upgeff.py, NOTES_production 3a)
        self.shops = Shops(self)                    # W8 shops: buying items / units, pawning (shops.py, NOTES_items §7)
        # constants (NOTES_combat §0, NOTES_units §2 / §4)
        self.defense_armor = _f(misc, 'DefenseArmor', 0.06)
        self.dmg_table = [_flist(misc, 'DamageBonus' + k, 8, [1.0] * 8) for k in DB_KEYS]
        eth = _flist(misc, 'EtherealDamageBonus', 7, [0.0] * 7)
        self.eth_bonus = [0.0] * 7
        for i, atk in enumerate(ETH_ORDER):
            self.eth_bonus[atk] = eth[i]
        self.eth_ally = _f(misc, 'EtherealDamageBonusAlly', 0.0)
        self.half_angle = _f(misc, 'AttackHalfAngle', 0.5)
        self.reaction_s = _f(misc, 'ReactionDelay', 0.25)
        self.min_unit_speed = _f(misc, 'MinUnitSpeed', 150.0)
        self.max_unit_speed = _f(misc, 'MaxUnitSpeed', 400.0)
        self.min_bldg_speed = _f(misc, 'MinBldgSpeed', 25.0)
        self.max_bldg_speed = _f(misc, 'MaxBldgSpeed', 400.0)
        self.bone_decay = _f(misc, 'BoneDecayTime', 88.0)
        self.structure_decay = _f(misc, 'StructureDecayTime', 30.0)
        self.building_angle = _f(misc, 'BuildingAngle', 270.0)
        self.mine_types = {}            # is_mine_type cache (id of the type dict -> bool)
        self.occ = {}                   # A-COL1: cell index -> ground units standing in it
        self.occs = {}                  # PATH-AB step 4: ... of them the ones that STAND (not walking: idle, or in range of their target) as of the last rebuild - obstacles of search B and the follower's sight, the blocker that sends a mover to the blocked handler
        self.fighters = []              # units with a weapon, serial order
        self.wakeq = []                 # heap (step, serial): fighters due (docs/engine_w5.md §2)
        self.cur = None                 # the fighter being processed
        self.buckets = {}               # (bx, by) -> [units] in insertion order
        self.max_coll = 0.0
        self.missiles = []              # creation order
        self.constructing = []          # buildings under construction, start order
        self.upgrading = []             # W8 buildings upgrading (NOTES_skibi §3), start order
        self.req_cache = {}             # type id -> [(required id, level)]
        self.trainers = []              # units whose train queue slot 0 runs (start order), NOTES_production §1
        self.reach = {}                 # type id -> weapon_reach (scan pre-filter)
        self.food_ceiling = max(1, min(300, int(_f(misc, 'FoodCeiling', 100))))
        self.upkeep_usage, self.upkeep_gold, self.upkeep_lumber = upkeep_tables(misc)     # UPKEEP (NOTES_production §2.3)
        self.train_refund = _f(misc, 'TrainRefundRate', 1.0)
        self.upg_refund = _f(misc, 'UpgradeRefundRate', 0.75)          # NOTES_skibi §3.4
        self.research_refund = _f(misc, 'ResearchRefundRate', 1.0)
        # W9R3-1 hero revive at an altar (NOTES_production §1.4): the Misc keys are floats [obs 0x6f008570], defaults = MiscGame
        self.revive_refund = _f(misc, 'ReviveRefundRate', 1.0)
        self.rv_base = r32(_f(misc, 'ReviveBaseFactor', 0.4))
        self.rv_lvl = r32(_f(misc, 'ReviveLevelFactor', 0.1))
        self.rv_maxf = r32(_f(misc, 'ReviveMaxFactor', 4.0))
        self.rv_lbase = r32(_f(misc, 'ReviveBaseLumberFactor', 0.0))
        self.rv_llvl = r32(_f(misc, 'ReviveLumberLevelFactor', 0.0))
        self.rv_maxgold = int(_f(misc, 'HeroMaxReviveCostGold', 700))
        self.rv_maxlumber = int(_f(misc, 'HeroMaxReviveCostLumber', 0))
        self.rv_tf = r32(_f(misc, 'ReviveTimeFactor', 0.65))
        self.rv_maxtf = r32(_f(misc, 'ReviveMaxTimeFactor', 2.0))
        self.rv_maxt = r32(_f(misc, 'HeroMaxReviveTime', 150.0))
        self.rv_life = r32(_f(misc, 'HeroReviveLifeFactor', 1.0))
        self.rv_mana = r32(_f(misc, 'HeroReviveManaFactor', 0.0))
        self.rv_mana_start = int(_f(misc, 'HeroReviveManaStart', 1))
        self.rv_stamp = 0               # CAbilityRevive §3: insertion age of the player's revivable list
        self.upgrades = env.get('upgrades') or {}       # {research id int: UpgradeData record} (NOTES_production §3)
        self.items = env.get('items') or []             # W8 item types in list order (itemdata.py, NOTES_items §1)
        self.item_types = {_rc(r['id4']): r for r in self.items}
        self.neg_regen = []             # units whose life falls by itself (death watch, NOTES_units §5)
        self.timed = []                 # W8 units with a timed life, in application order
        self.by_serial = {}
        self.inputs = []                # posted player inputs (A20, docs/engine_w5.md §6)
        self.input_seq = 0
        self.in_inp = False                         # PERF-AUTOFIELD: set by inp_issue around a player's order (engine.issue_order takes and clears it)
        self.missile_serial = 0
        self.wk = 0
        self.guard_units = []            # PORT2-FIX: the units with a guard point (guard_step walks only these)
        self.bk_ext = None              # BUGS Q-EXT: (min bx, max bx, min by, max by) of the buckets ever made
        self.pbq = {}                   # A-PB1: owner id -> [FIFO of units waiting for a path, balance, refill step]
        self.trace = None               # list -> engine debug lines ("#E ...") when enabled
        self.stats = {'attacks': 0, 'damage_events': 0, 'missiles': 0, 'kills': {}, 'bounty': {}, 'built': {},
                      'deaths': {}, 'repaths': 0, 'shares': 0, 'inputs': 0, 'inputs_rejected': 0}
        self.vm.on_turn.append(self.apply_inputs)

    # ================================================================ helpers
    def now(self):
        return self.vm.steps

    def log(self, msg):
        if self.trace is not None:
            self.trace.append('#E t=%d %s' % (self.vm.steps * 5, msg))

    def work(self):
        return self.wk + (self.grid.wk if self.grid else 0)

    @staticmethod
    def wv(u, i, key):
        return u.data.get('%s%d' % (key, i + 1), 0)

    def has_weapon(self, u, i):
        if u.root_st:
            on = u.root_mask                         # PORT4-Build CAbilityRoot §7.5: the state's weapon slots (DataA rooted / DataB uprooted)
        else:
            on = u.upg_weap if getattr(u, 'upg_weap_on', False) else int(u.data.get('weapsOn', 0) or 0)
        return (on >> i) & 1 and int(self.wv(u, i, 'weapTp') or 0) != 0

    @staticmethod
    def can_move(u):
        """a move ability exists (NOTES_units §2: towers have none) [A4: type speed > 0 and not a structure]; an uprooted
        Ancient (PORT4-Build CAbilityRoot) moves."""
        return u.base_speed > 0 and (not u.is_bldg or u.root_st == 2)

    @staticmethod
    def flying(u):
        return bool(int(u.data.get('movetp', 0) or 0) & 0x2)

    def target_class(self, u):
        m = int(u.data.get('targType', 0) or 0) & UD.T_CLASS
        if m:
            return m
        if u.is_bldg:
            return UD.T_STRUCTURE
        return UD.T_AIR if self.flying(u) else UD.T_GROUND

    def ally(self, pa, pb):
        """natives.IsPlayerAlly without allocating handle ids."""
        if pa is pb:
            return True
        return bool(pb.hid and pa.alliance.get((pb.hid, 0), False))

    def eff_speed(self, u, free=False):
        """NOTES_units §2: (base) clamped to [min, max]; min = unit minSpd or MinUnitSpeed (MinBldgSpeed),
        max = maxSpd or MaxUnitSpeed (MaxBldgSpeed); Min >= 1, Max <= 522 and >= Min; 0 without move ability.
        MOVE-PORT root counter (NOTES_class_CAbilityMove §7): a rooted / stunned unit reads speed 0; the port carries
        the counter as `u.stunned`, the producer of the stun-style root (BPSE, BEer, BUsl).
        free=True ignores the root counter (the stun-free modified speed): used by the 'can this unit ever move' tests that
        queue a unit for movement (a stunned unit must be queued so it moves once the stun ends) and by the speed reads of
        GetUnitMoveSpeed / UNIT_RF_SPEED (BUGS PORT3-UM1/UM4)."""
        if u.base_speed <= 0 or (u.stunned > 0 and not free):
            return 0.0
        lo = float(u.data.get('minSpd', 0) or 0) or (self.min_bldg_speed if u.is_bldg else self.min_unit_speed)
        hi = float(u.data.get('maxSpd', 0) or 0) or (self.max_bldg_speed if u.is_bldg else self.max_unit_speed)
        lo = max(1.0, lo)
        hi = max(lo, min(522.0, hi))
        sp = u.speed + u.item_speed if u.item_speed else u.speed             # NOTES_items §10: AIms, before the factor
        if u.upg_spd:
            sp = sp + u.upg_spd                                              # BUGS FF-UPG rmvx
        if u.move_delta:
            sp = sp * (1.0 + u.move_delta)                                   # NOTES_abilities §6.3 move factor
        return r32(min(hi, max(lo, sp)))

    # ---------------------------------------------------------------- W9-4 item passives (NOTES_items §10)
    @staticmethod
    def item_bonus(u, lst, aid, d):
        """+1: the carried ability joins the unit's list; -1: the first equal entry leaves it."""
        if d > 0:
            lst.append(aid)
        elif aid in lst:
            lst.remove(aid)

    def item_speed_calc(self, u):
        """AIms [obs 0x6f48ce90 / 0x6f55f9f0]: the unit's speed = (base + bonus) x factor; the bonus is the LARGEST
        DataA of the carried AIms while MoveSpeedBonusesStack is 0 (the default), the sum when the Misc key is set [inf]."""
        stack = _f(self.misc, 'MoveSpeedBonusesStack', 0.0) != 0.0
        b = 0.0
        for a in u.item_spd:
            v = self.ab.defs[a]['lv'][0]['A']
            b = b + v if stack else (v if v > b else b)
        u.item_speed = b

    def item_mreg_calc(self, u):
        """AIrm [obs 0x6f57eaf0]: every instance adds DataA x (type regenMana + hero INT regeneration) per second."""
        m = 0.0
        for a in u.item_mrg:
            m = m + self.ab.defs[a]['lv'][0]['A']
        u.item_mreg = m
        self.set_regen(u)

    def item_lreg_calc(self, u):
        """Arel [obs 0x6f57dcd0 -> 0x6f57e0f0]: every instance - carried (level 1) or an ability of the unit (its current
        level) - adds its cached DataA (hit points per second) to the life rate, no hero gate, instances add [inf]."""
        v = 0.0
        for a in u.item_lrg:
            v = v + self.ab.defs[a]['lv'][0]['A']
        for code, b, lv in self.ab.abil_list(u):                  # Arel as an ability of the unit itself: its current level
            if b == 'Arel' and lv is not None:
                v = v + lv['A']
        u.item_lreg = v
        self.set_regen(u)

    @staticmethod
    def add_max_mana(u, delta):
        """AImm [obs 0x6f57d830 / 0x6f57d860 -> 0x6f1bba10 mode 1]: max mana + delta, current = old fraction x new max
        (a unit without a pool has fraction 0: the new pool starts empty)."""
        old = u.maxmana
        frac = u.mana / old if old > 0 else 0.0
        u.maxmana = r32(max(0.0, old + delta))
        u.mana = r32(min(u.maxmana, frac * u.maxmana))

    # ================================================================ unit lifecycle
    def init_unit(self, u):
        d = u.data
        u.vm = self.vm
        u.is_bldg = bool(d.get('bldg'))
        u.base_speed = float(d.get('spd', 0) or 0)
        u.coll = float(d.get('collision', 0) or 0)
        u.armor = float(d.get('def', 0) or 0)
        u.def_type = int(d.get('defType', 4) if d.get('defType', '') != '' else 4)
        u.magic_immune = 0
        u.ethereal = 0
        u.ucls = int(d.get('ucls', 0) or 0)                      # W8 IsUnitType class bits (NOTES_unittype)
        u.cast = None
        self.by_serial[u.serial] = u
        for a in str(d.get('abilList', '') or '').split(','):     # A5: type abilities at level 1
            a = a.strip()
            if len(a) == 4:
                code = (ord(a[0]) << 24) | (ord(a[1]) << 16) | (ord(a[2]) << 8) | ord(a[3])
                if code not in u.abilities:
                    u.abilities[code] = 1
                    self.ab.on_add(u, code)
        u.invulnerable = AVUL in u.abilities or u.invuln_n > 0    # NOTES_units §5: 'Avul'; W8 counter
        self.set_regen(u)
        if u.coll > self.max_coll:
            self.max_coll = u.coll
        self.relocate(u)
        if not u.fighter and any(self.has_weapon(u, i) for i in (0, 1)):     # PORT4: Aroo's root_init may have registered it
            self.fighters.append(u)
            u.fighter = True
            # CAbilityAttack init: first scan at U(stream 2) × 0.5 s (NOTES_abilities §4.4 [obs]; steps rounded down)
            u.next_scan = self.now() + int(self.rng.unit() * 0.5 * SPS)
            self.wake(u, self.now())
        if u.is_bldg and not u.locust:
            self.add_footprint(u)                                 # §5.34: a locust has no pathing (A-SP65)
        self.hero.init(u)                                         # W8: hero state + attributes (spawns full)
        for code in list(u.abilities):                            # Aamk of the type's abilList: the hero state exists only now
            if self.ab.base(code) == 'Aamk':
                self.ab.aamk_apply(u, code)
        self.shops.init_unit(u)                                   # W8: shop slots of the type's sale lists
        self.ue.sync(u)                                           # BUGS FF-UPG: the owner's researched levels reach a unit made later
        self.hv.init_unit(u)                                      # PORT-Harvest: seed a gold mine's pool from DataA

    def set_regen(self, u):
        """life / mana rates (NOTES_units §5, A6: regenType always and day/night regenerate, blight/none not)."""
        d = u.data
        rate = 0.0
        if u.constructing:
            rate = u.construct_rate
        elif not u.dead and int(d.get('regenType', 0) or 0) in (1, 3, 4):
            rate = float(d.get('regenHP', 0) or 0)
        if not u.dead and not u.constructing:
            rate += u.regen_bonus + u.h_regen + u.item_lreg + u.upg_hreg   # BUGS FF-UPG rhpr; auras (NOTES_abilities §5.6), hero STR (NOTES_heroes §2.3), Arel (NOTES_items §11.1)
        u.set_life_rate(rate)
        mr = float(d.get('regenMana', 0) or 0) + u.h_mregen + u.upg_mreg      # BUGS FF-UPG rmnr
        if u.item_mreg:
            mr = mr + mr * u.item_mreg                        # NOTES_items §10: AIrm, DataA x the base regeneration
        u.set_mana_rate(0.0 if u.dead else mr)
        if rate < 0 and u not in self.neg_regen:
            self.neg_regen.append(u)

    def on_remove(self, u):
        if u.cbuilder is not None:
            self.release_builder(u, False)               # PORT4-Build: a structure removed under construction keeps its builder gone
        self.hv.on_remove(u)                            # PORT-Harvest: free the mine slot / drop the queue
        self.hero.on_remove(u)
        if u.wg_slot:
            self.wg_unregister(u)                   # NOTES_pathing 1.1: a removed gate leaves the pathing table
        if u.ef_caster is not None:
            self.sp.ef_summon_gone(u)
        self.sp.lc_unit_gone(u)                     # NOTES_spells §5.16 (A-SP83)
        self.drop_lists(u)
        if u.footprint:
            self.remove_footprint(u)
        self.by_serial.pop(u.serial, None)
        if u.bkt is not None:
            self.buckets[u.bkt].remove(u)
            u.bkt = None

    # ---- spatial index + fighter wake queue
    def relocate(self, u):
        if u.locust:                              # NOTES_abilities §5.34: out of every area enumeration (A-SP65)
            if u.bkt is not None:
                self.buckets[u.bkt].remove(u)
                u.bkt = None
            return
        k = (int(math.floor(u.x / BKT)), int(math.floor(u.y / BKT)))
        if k != u.bkt:
            if u.bkt is not None:
                self.buckets[u.bkt].remove(u)
            if k not in self.buckets:
                e = self.bk_ext                   # BUGS Q-EXT: the bounds of every bucket ever made
                self.bk_ext = (k[0], k[0], k[1], k[1]) if e is None else (min(e[0], k[0]), max(e[1], k[0]),
                                                                           min(e[2], k[1]), max(e[3], k[1]))
            self.buckets.setdefault(k, []).append(u)
            u.bkt = k
            self.wk += 1

    def query(self, x, y, r):
        """units of the buckets overlapping the square x +- r, y +- r (rows south to north, west to east, insertion
        order inside a bucket). Callers make the result order-independent (total-order keys / sort by serial)."""
        out = []
        e = self.bk_ext
        if e is None:
            return out
        # BUGS Q-EXT (performance, same result): only the rows / columns where buckets exist — DBZ TD's "Normal Mode"
        # marker has an aura of radius 1e9 (2e6 x 2e6 bucket cells per step otherwise)
        bx0, bx1 = max(int(math.floor((x - r) / BKT)), e[0]), min(int(math.floor((x + r) / BKT)), e[1])
        by0, by1 = max(int(math.floor((y - r) / BKT)), e[2]), min(int(math.floor((y + r) / BKT)), e[3])
        for by in range(by0, by1 + 1):
            for bx in range(bx0, bx1 + 1):
                self.wk += 1
                lst = self.buckets.get((bx, by))
                if lst:
                    out.extend(lst)
        return out

    def wake(self, u, s):
        """schedule fighter u at step s (earliest wins; stale heap entries are skipped)."""
        if not u.fighter:
            return
        if u is self.cur and s <= self.now():
            s = self.now() + 1
        if u.wake_at < 0 or s < u.wake_at:
            u.wake_at = s
            heapq.heappush(self.wakeq, (s, u.serial))
            self.wk += 1

    def next_wake(self, u):
        now = self.now()
        if u.atk_target is not None or u.gpt is not None:
            return now + 1                 # with a target: every step (range, facing, chase, cooldown)
        if u.dp_at >= 0:
            return max(now + 1, u.dp_at)
        if self.may_acquire(u):
            return max(now + 1, u.next_scan)
        return -1                          # woken by an order / arrival

    def timed_life(self, u, dur):
        """UnitApplyTimedLife / summons: the unit dies dur s later (a new call replaces the time) (A-SP19)."""
        u.expire_at = self.now() + steps_of(dur) if dur > 0 else self.now() + 1
        u.ucls |= 0x4                                             # the timed-life buff sets SUMMONED [obs]
        if u not in self.timed:
            self.timed.append(u)

    def drop_lists(self, u):
        if u in self.timed:
            self.timed.remove(u)
        if u.fighter and u in self.fighters:
            self.fighters.remove(u)
        u.fighter = False
        u.wake_at = -1
        if u in self.constructing:
            self.constructing.remove(u)
        if u in self.upgrading:
            self.upgrading.remove(u)                      # A-UP6: death / removal ends the upgrade, no event
        u.upg = None
        u.upg_ion = False
        if u in self.neg_regen:
            self.neg_regen.remove(u)
        u.atk_target = None
        u.gpt = None
        u.dp_at = -1
        self.refund_build(u)                          # PORT10-Build: death / removal of a worker with an armed build
        u.build = None
        u.path = None

    def on_death(self, u, killer):
        """NOTES_combat §4: dead flag, life 0, orders stopped (natives.kill_unit), bounty before EVENT_UNIT_DEATH."""
        if u.cbuilder is not None:
            self.release_builder(u, False)               # PORT4-Build: CAbilityNightElfBuild §5 step 4, the builder dies with the structure
        if u.mine_prev is not None:
            self.restore_mine(u)                         # PORT4-Build: a destroyed Haunted Gold Mine gives the neutral mine back [inf]
        self.hv.on_remove(u)                             # PORT-Harvest: free the mine slot / drop the queue
        if u.wg_slot:
            self.wg_unregister(u)                   # [inf] a dead gate no longer links
        self.ab.on_death(u)
        self.sp.on_death(u)
        self.queue_abort(u)
        self.drop_lists(u)
        u.constructing = False
        u.set_life_rate(0.0)
        u.set_mana_rate(0.0)
        if u.footprint:                                  # A14: the footprint clears at death
            self.remove_footprint(u)
        own = u.owner.id
        self.stats['deaths'][own] = self.stats['deaths'].get(own, 0) + 1
        if killer is not None:
            kp = killer.owner.id
            self.stats['kills'][kp] = self.stats['kills'].get(kp, 0) + 1
            if not u.illusion:                           # an illusion gives no bounty and no XP (NOTES_heroes §1.2)
                self.bounty(u, killer)
                self.hero.on_kill(u, killer)             # W8: hero XP before EVENT_UNIT_DEATH (NOTES_heroes §1.2)
        if u.ef_caster is not None:
            self.sp.ef_summon_gone(u)                    # W8 elemental fury: the caster comes back
        self.itm.on_death(u)                             # W8: the carrier's items drop (A-IT2)
        self.log('death u%d %s by %s' % (u.serial, u.data.get('id4'), ('u%d' % killer.serial) if killer else '-'))

    def decay_time(self, u):
        return self.structure_decay if u.is_bldg else self.bone_decay      # NOTES_combat §4

    def roll(self, dice, sides, plus, stream=2):
        """NOTES_combat §2.1 dice routine: <= 16 dice each (rand32 * sides) >> 32 + 1; > 16 normal approximation
        (A7: Irwin-Hall of 12 uniforms for the normal variate)."""
        dice, sides = int(dice), int(sides)
        rng = self.streams.game[stream]
        if dice <= 16:
            s = 0
            for _ in range(dice):
                s += rng.below(sides) + 1
                self.wk += 1
            return plus + s
        span = dice * (sides - 1)
        if span < 0:                           # BUGS UDON-THROW: sides <= 0 (sqrt of a negative: the U# twin has no throw)
            return plus + dice
        mean = span / 2.0
        dev = math.sqrt(span)
        z = 0.0
        for _ in range(12):
            z += rng.unit()
        v = int(round(mean + (z - 6.0) * dev))
        v = max(0, min(span, v))
        return plus + v + dice

    def bounty(self, u, killer):
        """NOTES_combat §4: enemy killer, victim owner gives bounty (A8: PLAYER_STATE_GIVES_BOUNTY != 0);
        dice or sides 0 -> plus, else trunc(plus + dice roll); gold and lumber alike."""
        vo, ko = u.owner, killer.owner
        if self.ally(vo, ko) or not vo.state.get(PLAYER_GIVES_BOUNTY, 0):
            return
        d = u.data
        for state, pre in ((PLAYER_GOLD, 'bounty'), (PLAYER_LUMBER, 'lumberbounty')):
            dice = int(d.get(pre + 'dice', 0) or 0)
            sides = int(d.get(pre + 'sides', 0) or 0)
            plus = int(d.get(pre + 'plus', 0) or 0)
            if dice <= 0 or sides <= 0:
                b = plus
            else:
                b = int(self.roll(dice, sides, plus, stream=0))     # bounty dice: stream 0 (NOTES_abilities §4.3)
            if b:
                self.N.credit_resource(ko, state, b * 10)         # UPKEEP: the bounty is taxed by the owner's rate [obs runtime 1.21]
                if state == PLAYER_GOLD:
                    self.stats['bounty'][ko.id] = self.stats['bounty'].get(ko.id, 0) + b

    # ================================================================ footprints / placement (NOTES_units §4)
    def tex(self, d):
        name = d.get('pathTex', '') or ''
        if not name:
            return None
        k = UD.tex_key(name)
        if k not in self.pathtex and self.reader is not None:
            UD.load_pathtex(self.reader, name, self.pathtex)
        return self.pathtex.get(k)

    def oriented(self, d, angle=None):
        """footprint rotated by the widget's own angle (default BuildingAngle): orientation 1 and 3 swap width /
        height (A15: 90° steps clockwise). CWidget_p2 §5.13 step 3: the quadrant comes from the position object's
        angle (the unit's facing), not from the global BuildingAngle; a map's CreateUnit can give any facing."""
        t = self.tex(d)
        if not t:
            return None
        w, h, m = t
        o = int(round((self.building_angle if angle is None else angle) / 90.0)) % 4
        for _ in range(o):
            nm = [0] * (w * h)
            for y2 in range(w):
                for x2 in range(h):
                    nm[y2 * h + x2] = m[x2 * w + (w - 1 - y2)]
            w, h, m = h, w, nm
        return w, h, m

    def snap(self, x, w, origin, ncells):
        """grid snap of one axis: clamp into the bounds - 32 (A16: the wpm extent), trunc toward zero to a multiple
        of 64, then +-32 if bit 1 of the width is set and +-16 if the width is odd (sign of x) [obs 0x6f2f3910]."""
        x = min(origin + ncells * 32.0 - 32.0, max(origin + 32.0, x))
        q = float(int(x / 64.0) * 64)
        sg = 1.0 if x >= 0 else -1.0
        if w & 2:
            q += 32.0 * sg
        if w & 1:
            q += 16.0 * sg
        return q

    def footprint_cells(self, d, x, y, angle=None):
        """-> ([(cx, cy, mask)] or None, snapped centre x, y)."""
        fp = self.oriented(d, angle)
        if fp is None or self.grid is None:
            return None, x, y
        w, h, m = fp
        g = self.grid
        cx = self.snap(x, w, g.ox, g.w)
        cy = self.snap(y, h, g.oy, g.h)
        bx, by = g.cell_of(cx - w * 16.0 + 1.0, cy - h * 16.0 + 1.0)
        cells = []
        for j in range(h):
            for i in range(w):
                mk = m[j * w + i]
                if mk:
                    cells.append((bx + i, by + j, mk))
        return cells, cx, cy

    def add_footprint(self, u):
        cells, cx, cy = self.footprint_cells(u.data, u.x, u.y, u.facing)
        if cells:
            self.grid.add_footprint(cells, 1)
            u.footprint = cells
            self.wk += len(cells)

    def remove_footprint(self, u):
        self.grid.add_footprint(u.footprint, -1)
        self.wk += len(u.footprint)
        u.footprint = None

    # ---------------------------------------------------------------- destructables (BUGS DS-2)
    def dest_cells(self, o):
        """o's footprint now: its type's pathTex (alive) or pathTexDeath (dead), centred on its position, turned in 90°
        steps by its facing [inf: the game's rotation rule for destructables], no grid snap (placed on the grid)."""
        t = self.dest_types.get(o.id4)
        if t is None or self.grid is None:
            return None
        name = t['pathTex'] if getattr(o, 'fpalive', o.dalive) else t['pathTexDeath']
        if not name:
            return None
        ft = self.pathtex.get(UD.tex_key(name))
        if not ft:
            return None
        w, h, m = ft
        # BUGS W9R3-6-DEST-ROT [inf]: the texture is upright at facing 270 (the editor's default angle), so the turns are
        # (quarter turns of the facing + 1) % 4: facing 90 / 270 keep a wide texture wide, 0 / 180 stand it up
        for _ in range((int(round(o.facing / 90.0)) % 4 + 1) % 4):
            nm = [0] * (w * h)
            for y2 in range(w):
                for x2 in range(h):
                    nm[y2 * h + x2] = m[x2 * w + (w - 1 - y2)]
            w, h, m = h, w, nm
        g = self.grid
        bx, by = g.cell_of(o.x - w * 16.0 + 1.0, o.y - h * 16.0 + 1.0)
        return [(bx + i, by + j, m[j * w + i]) for j in range(h) for i in range(w) if m[j * w + i]]

    def dest_place(self, id4, x, y, ang, life):
        """a destructable the game places from war3map.doo (ang in radians, life in percent); no handle yet (BUGS W9R3-4-CHA)."""
        t = self.dest_types.get(id4)
        self.dests_init.append((id4, x, y, ang, life))
        o = DestRec(id4, x, y, math.degrees(ang), life > 0, float(t['hp']) if t and t['hp'] > 0 else 100.0)
        self.dest_add(o, False)
        return o

    def dest_add(self, o, work=True):
        self.dests.append(o)
        o.dser = DEST_SERIAL + self.dest_seq
        self.dest_seq += 1
        self.dest_ser[o.dser] = o
        o.fp = self.dest_cells(o)
        if o.fp:
            self.grid.add_footprint(o.fp, 1)
            if work:                                  # the map load is no frame work
                self.wk += len(o.fp)

    def dest_set_alive(self, o, alive):
        """KillDestructable / a restore: the logical alive state and the footprint swap to the other texture (gates open).
        The +0x124 life setter alone does NOT re-register the placement; only the death crossing (to dead) and
        DestructableRestoreLife step 8 (to alive) touch the cells, so the footprint state is kept separately."""
        was = o.dalive
        if was == alive and getattr(o, 'fpalive', was) == alive:
            return
        died = was and not alive
        if o.fp:
            self.grid.add_footprint(o.fp, -1)
        o.dalive = alive
        o.fpalive = alive
        o.fp = self.dest_cells(o)
        if o.fp:
            self.grid.add_footprint(o.fp, 1)
        self.wk += 1
        if died:
            UE.dest_died(self.N, o)                       # W9-5: EVENT_WIDGET_DEATH of a destructable

    def dest_set_life(self, o, v, restore=False):
        """the absolute life setter of a destructable (vtable +0x124, CDestructable_p1 §10): the value is clamped
        into [0, max life]; a living destructable (old life > 0.4) whose new life reaches the 0.4 death threshold
        dies (the death handler writes 0.0 and removes the placement). A non-crossing write only changes the life
        / alive state: the +0x124 slot has no placement call, so a dead destructable set to a positive life reads
        that life but keeps its dead texture until DestructableRestoreLife (step 8 is the placement re-add).
        The restore form additionally floors a finite request at 0.5 and replaces NaN / infinity with the
        maximum life (0x6f18c0c0 / 0x6f26a330). The Udon twin returns the "death event task was pushed" flag."""
        if restore:
            if not math.isfinite(v):
                v = float(o.maxlife)
            elif v < 0.5:
                v = 0.5
        if not (v >= 0.0):                                # NaN from the absolute setter clamps to 0
            v = 0.0
        if v > o.maxlife:
            v = float(o.maxlife)
        old = o.life
        o.life = r32(v)
        if old > DEATH_LIFE and o.life - DEATH_LIFE <= 0.001:   # death: new life at / below 0.4 (+ the 0.001 epsilon)
            o.life = 0.0
            self.dest_set_alive(o, False)
        elif restore:
            self.dest_set_alive(o, o.life > 0.0)          # §10 step 8: the restore re-adds the placement
        else:
            o.dalive = o.life > 0.0                       # +0x124 only: no footprint change
        return False

    def dest_set_max_life(self, o, v):
        """the max-life setter (+0x144, CDestructable_p1 §10; reached by the SetDestructableMaxLife native,
        Unclassed_Native_0021 §7): write the new ceiling, then re-clamp the current life into [0, max life]
        through the same clamp the life setter uses. The chapter gives no death-crossing test for this slot, so
        the placed footprint / widget-death event are untouched (a ceiling below 0.4 leaves a placed destructable
        whose next death handler is a later crossing)."""
        o.maxlife = r32(v)
        if not (o.life >= 0.0):                        # NaN / negative current life clamps to the low end
            o.life = 0.0
        if o.life > o.maxlife:
            o.life = float(o.maxlife)
        o.dalive = o.life > 0.0                        # the life-based state follows the re-clamp; cells do not
        return False

    def dest_remove(self, o):
        if o in self.dests:
            self.dests.remove(o)
        self.dest_ser.pop(getattr(o, 'dser', 0), None)
        o.removed = True
        if o.fp:
            self.grid.add_footprint(o.fp, -1)
            o.fp = None

    def dest_box(self, o):
        """world box (x0, y0, x1, y1) of o's footprint cells; a destructable without a footprint is a 32 x 32 box."""
        g = self.grid
        if g is None or not o.fp:
            return (o.x - 16.0, o.y - 16.0, o.x + 16.0, o.y + 16.0)
        ix0 = min(c[0] for c in o.fp)
        ix1 = max(c[0] for c in o.fp)
        iy0 = min(c[1] for c in o.fp)
        iy1 = max(c[1] for c in o.fp)
        return (g.ox + ix0 * 32.0, g.oy + iy0 * 32.0, g.ox + (ix1 + 1) * 32.0, g.oy + (iy1 + 1) * 32.0)

    def dest_weapon(self, u, o):
        """BUGS W9-PYR-DEST: the first weapon of u that may attack destructable o (-1 none). Rule [ext data + NOTES_unittype §2]:
        the weapon's targs list must name the destructable's targType (debris / tree / bridge / wall); an invulnerable one also needs
        'invulnerable' [inf: the unit rule]; a weapon with dice 0 is the 'none' weapon and never attacks."""
        t = self.dest_types.get(o.id4)
        flag = t.get('targ', 0) if t else 0
        if not flag or not o.dalive or o.life <= 0.0 or getattr(o, 'removed', False):
            return -1
        for i in (0, 1):
            if not self.has_weapon(u, i) or int(self.wv(u, i, 'dice') or 0) <= 0:
                continue
            m = int(self.wv(u, i, 'targs') or 0)
            if m & flag and (not getattr(o, 'invul', False) or m & UD.T_INVULN):
                return i
        return -1

    def order_dest(self, u, oid, o):
        """attack / smart / attackonce on a destructable: the unit walks into reach and swings at it until it is dead (explicit order,
        like an attack-ground order); other orders go to its position."""
        if oid not in (O_SMART, O_ATTACK, O_ATTACKONCE):
            return self.issue_order(u, oid, o.x, o.y)
        wi = self.dest_weapon(u, o)
        if wi < 0:
            return self.issue_order(u, O_SMART, o.x, o.y) if oid == O_SMART else False
        self.clear_order(u)
        u.order = oid
        u.atk_w = wi
        u.gpt = DestPt(o, u, self.dest_box(o))
        u.explicit = True
        u.tx, u.ty = u.gpt.x, u.gpt.y
        self.wake(u, self.now())
        return True

    def dest_strike(self, u, t, i, amount):
        """the damage of one hit on destructable t.o: life -= amount (0 when invulnerable) [obs NOTES_orders §5: destructable
        vtable +0x120, no armor in the path; inf: the rolled amount]; the §10 +0x128 rule: only a pre-hit life above 0.4
        whose new life is at/below 0.4 (+0.001) dies, which swaps the footprint to pathTexDeath and raises
        EVENT_WIDGET_DEATH; otherwise the life is clamped at 0 and the placement stays. Missile weapons land at the damage
        point (no flight) [inf]."""
        o = t.o
        amt = 0.0 if getattr(o, 'invul', False) else amount
        self.stats['dest_hits'] = self.stats.get('dest_hits', 0) + 1
        if amt > 0.0:
            old = o.life
            nl = r32(old - amt)
            if nl < 0.0:
                nl = 0.0
            o.life = nl
            if old > DEATH_LIFE and nl - DEATH_LIFE <= 0.001:   # §10: only an old life above 0.4 crosses the threshold
                o.life = 0.0
                self.log('dest %s dies u%d' % (o.id4, u.serial))
                self.dest_set_alive(o, False)
            else:
                o.dalive = nl > 0.0                    # +0x128 also only changes the life / alive state, not the cells

    def gold_depot_type(self, d):
        """HALL-MINE: the type record's +0x264 flag [obs 0x6f29c9a0 / 0x6f1acbe0, loader 0x6f1a9902]: set when one of the
        type's abilities is class Artn (Return Resources) with DataA1 != 0 (gold accepted; CAbilityReturn_p1 §5) =
        Argl / Argd of the base data [inf: by id, a custom Artn-derived id is not seen]. The undead halls unpl / unp1 /
        unp2 are excluded by id as in the placement test [obs 0x6f2a50c0] (they carry Arlm only, so the flag is 0 anyway)."""
        al = str(d.get('abilList', '') or '')
        if not al or ('Argl' not in al and 'Argd' not in al):
            return False
        if d.get('id4') in ('unpl', 'unp1', 'unp2'):
            return False
        codes = [a.strip() for a in al.split(',')]
        return 'Argl' in codes or 'Argd' in codes

    def mine_near(self, cx, cy):
        """HALL-MINE: a living gold mine (a unit with a Agmb-class mine ability: Agld / Abgm / Aegm, any owner) within
        MINE_BLOCK_R (750 = 0x2ee) of the snapped centre refuses the placement with 0x47 (Tooclosetomine, 'Unable to build
        so close to the gold mine.') [obs 0x6f2a3ce0 / 0x6f1b7360: unit enumeration, circle, callback 0x6f26f070 =
        has-Agmb-ability; 1.21 and 1.26a agree]. The disc test is on the unit position, <= as GroupEnumUnitsInRange
        [inf: the boundary and a collision-radius allowance are not read]. The second callback of the pair
        (0x6f26f020: a spatial entry with bit 0x100 of its word +0x48 within 750) is not decoded [Open]."""
        r2 = MINE_BLOCK_R * MINE_BLOCK_R
        for o in self.N.units:
            self.wk += 1
            if not o.alive or o.dead:
                continue
            dx = o.x - cx
            dy = o.y - cy
            if dx * dx + dy * dy <= r2 and self.hv.is_mine(o):
                return True
        return False

    def can_place(self, owner, tid, x, y, builder=None):
        """-> (ok, cx, cy). Footprint cells buildable (wpm walk + build flags, no other footprint), no ground unit
        except the builder inside the footprint (0xba) [A17: preventPlace/requirePlace letters not decoded]."""
        d = self.N.unit_types.get(tid)
        if d is None:
            return False, x, y
        cells, cx, cy = self.footprint_cells(d, x, y)
        if cells is None:
            return True, r32(x), r32(y)
        if self.gold_depot_type(d) and self.mine_near(cx, cy):
            return False, cx, cy                         # 0x47 Tooclosetomine (HALL-MINE)
        g = self.grid
        for c in cells:
            self.wk += 1
            if not g.buildable(c[0], c[1]):
                return False, cx, cy
        fw = self.oriented(d)
        hx, hy = fw[0] * 16.0, fw[1] * 16.0
        for o in self.N.units:
            self.wk += 1
            if o is builder or not o.alive or o.dead or (o.is_bldg and o.root_st != 2) or self.flying(o):
                continue
            if cx - hx <= o.x < cx + hx and cy - hy <= o.y < cy + hy:
                return False, cx, cy
        return True, cx, cy

    def place_cells(self, owner, tid, x, y, builder=None):
        """placement preview (NOTES_units §4.1, CBuildFrame [obs 0x6f165a30, 0x6f2f3ea0]) -> (ok, cx, cy, cells):
        one entry (cell x, cell y, state) per footprint cell that blocks walking or building (pathTex pixel with
        flag 0x4a; flying-only cells are transparent), state 1 green / 2 red. Red = the cell fails the same tests as
        can_place (not buildable, or a ground unit other than the builder stands in it). When can_place refuses for a
        reason no single cell shows (a unit on a transparent cell), the whole grid is red, as the game fills the
        texture with the red colour for a refused placement [obs 0x6f1862a0 → 0x6f2f48d0]. No footprint -> []."""
        ok, cx, cy = self.can_place(owner, tid, x, y, builder)
        d = self.N.unit_types.get(tid)
        if d is None:
            return ok, cx, cy, []
        cells, _cx, _cy = self.footprint_cells(d, x, y)
        if not cells:
            return ok, cx, cy, []
        g = self.grid
        w, h, _m = self.oriented(d)
        bx, by = g.cell_of(cx - w * 16.0 + 1.0, cy - h * 16.0 + 1.0)
        hit = set()
        for o in self.N.units:
            if o is builder or not o.alive or o.dead or (o.is_bldg and o.root_st != 2) or self.flying(o):
                continue
            if cx - w * 16.0 <= o.x < cx + w * 16.0 and cy - h * 16.0 <= o.y < cy + h * 16.0:
                hit.add(g.cell_of(o.x, o.y))
        out = []
        for (ix, iy, mk) in cells:
            if not mk & 5:
                continue
            red = not g.buildable(ix, iy) or (ix, iy) in hit
            out.append((ix, iy, 2 if red else 1))
        if not ok and all(s == 1 for _x, _y, s in out):
            out = [(ix, iy, 2) for ix, iy, _s in out]
        return ok, cx, cy, out

    # ================================================================ orders (NOTES_units §1, NOTES_combat §2.5)
    def refund_build(self, u):
        """CAbilityBuild_p1 §5.3 / p2 §3.1: the armed build paid its cost when the placement was armed (the port's build
        order); aborting it refunds 100 % of the stored pair. The construction-start path does not come through here
        (it consumes the paid pair)."""
        p = getattr(u, 'build_paid', None)
        if not p:
            return
        u.build_paid = None
        g, l = p
        if g:
            self.N.set_player_state(u.owner, PLAYER_GOLD, u.owner.state.get(PLAYER_GOLD, 0) + g)
        if l:
            self.N.set_player_state(u.owner, PLAYER_LUMBER, u.owner.state.get(PLAYER_LUMBER, 0) + l)

    def clear_order(self, u):
        self.refund_build(u)                         # CAbilityBuild §5.3: an aborted armed placement gives the cost back
        self.hv.stop(u)                              # PORT-Harvest: a new order ends a harvest trip / leaves the mine
        if u.cast is not None:
            self.sp.interrupt(u)                     # NOTES_production §5.2: a new order ends a cast (ENDCAST)
        u.order = 0
        u.flee_oid = 0                               # [obs runtime 1.26a] a creep-owned worker's flee reports 852018 until the next order
        u.pick = None
        u.iop = None
        u.target = None
        u.wg_via = None                              # A-AB22: a new order leaves the smart-on-gate walk
        u.atk_target = None
        u.gpt = None
        u.explicit = False
        u.chasing = False
        u.hold = False
        u.build = None
        u.path = None
        u.fo = None
        self.N.moving.discard(u)

    # ---- STEER-3 (NOTES_pathing 2.19 / steer 8.4): one group order = one cluster (up to 12 members) with a formation: members are grouped by unit type, each group laid out in rows perpendicular to the heading
    FORM_A = ((1, 2, 3, 2, 3, 3, 3, 3, 3, 4, 4, 4), (1, 2, 3, 2, 3, 3, 3, 3, 3, 3, 3, 3))      # row capacity by the group size 1 .. 12 (small units, big units), one type group [obs 0x6f7dc2a0]
    FORM_B = ((1, 2, 3, 4, 3, 3, 4, 4, 4, 4, 4, 4), (1, 2, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3))      # ... several type groups [obs 0x6f7dc308]
    FORM_JOIN2 = 107.0 * 107.0                   # units farther apart than ~40 A cost units (1.7 A0 cells) form separate clusters [obs 0x6f682b00 / 2.8, inf: Euclid]
    FORM_MAX = 12
    FORM_CAP = 64

    def cluster_form(self, us, oid, x, y):
        """STEER-3: the units that took one group point order (move / attack-move / smart) form clusters: connected by distance, at most 12 each; a cluster of >= 2 gets formation slots around the order point"""
        if not self.pab:
            return
        mem = [u for u in us if u.order == oid and u in self.N.moving and u.target is None and not u.chasing and u.pathing and not self.flying(u) and u.alive and not u.dead]
        mem.sort(key=lambda v: v.serial)
        del mem[self.FORM_CAP:]                   # a bound for the work of the connection search: the first 64 (serial order) form clusters
        self.wk += len(mem)
        rest = mem
        while rest:
            cl = [rest[0]]
            rest = rest[1:]
            grew = True
            while grew and len(cl) < self.FORM_MAX:
                grew = False
                for v in rest:
                    self.wk += 1
                    for m in cl:
                        dx, dy = v.x - m.x, v.y - m.y
                        if dx * dx + dy * dy <= self.FORM_JOIN2:
                            cl.append(v)
                            rest = [w for w in rest if w is not v]
                            grew = True
                            break
                    if grew or len(cl) >= self.FORM_MAX:
                        break
            if len(cl) >= 2:
                self.form_layout(cl, x, y)

    def form_layout(self, cl, x, y):
        """the formation of one cluster [obs 0x6f6846c0 and callees]: heading from the centroid to the point; per type group R = the largest radius (cells), w = 2R, rows of n = table[count] members spread over n w + (n - 1) (w + 2) cells,
        rows 2.5 cells (+ w) behind each other, the next group 5.5 cells behind the first row's axis; the members keep their sides (sorted by their lateral position now); the layout is centred on its centroid and rotated by the heading"""
        n = len(cl)
        cx = sum(m.x for m in cl) / n
        cy = sum(m.y for m in cl) / n
        hx, hy = x - cx, y - cy
        hl = math.sqrt(hx * hx + hy * hy)
        if hl > 0.0:
            hx, hy = hx / hl, hy / hl
        else:
            hx, hy = 1.0, 0.0
        groups = {}
        for m in cl:
            groups.setdefault(m.tid, []).append(m)
        keys = sorted(groups)
        tab = self.FORM_A if len(keys) == 1 else self.FORM_B
        offs = []                                 # (member, forward, lateral) in cells
        org = 0.0
        for k in keys:
            ms = groups[k]
            self.wk += len(ms)
            r = 0.0
            for m in ms:
                c = float(m.coll) if m.coll > 1.0 else 1.0
                if c / 32.0 > r:
                    r = c / 32.0
            w = 2.0 * r
            ms.sort(key=lambda m: (-(m.x - cx) * hy + (m.y - cy) * hx, m.serial))
            cap = tab[1 if r >= 1.5 else 0][len(ms) - 1]       # [inf] the notes say w >= 1.5; the real footmen (R 0.97, w 1.94) stand 4 to a row (G4), so the test is on R
            row = 0
            i = 0
            while i < len(ms):
                rm = ms[i:i + cap]
                cnt = len(rm)
                width = cnt * w + (cnt - 1) * (w + 2.0)
                for j, m in enumerate(rm):
                    lat = 0.0 if cnt == 1 else -(width - w) / 2.0 + j * ((width - w) / (cnt - 1))
                    offs.append((m, org - row * (w + 2.5), lat))
                row += 1
                i += cap
            org -= 5.5
        fx = sum(o[1] for o in offs) / n
        fl = sum(o[2] for o in offs) / n
        for m, f, l in offs:
            f = (f - fx) * 32.0
            l = (l - fl) * 32.0
            m.fo = (r32(f * hx - l * hy), r32(f * hy + l * hx))
            m.seg = None
            m.path = None                          # the route goes to the slot, not to the order point

    def issue(self, u, oid, x=None, y=None, target=None):
        """-> True if the order was taken. Mirrors natives.issue_order's old contract (events fired there).
        W8: then the spell events that are due (spells.flush: an interrupted cast's ENDCAST, a new cast's CHANNEL)."""
        r = self.issue_order(u, oid, x, y, target)
        if self.sp.pend_end or self.sp.pend_begin is not None:
            self.sp.flush()
        return r

    def issue_order(self, u, oid, x=None, y=None, target=None):
        N = self.N
        u.usr = self.in_inp                              # PERF-AUTOFIELD: who gave the order (a player's input only; nested orders and events are not)
        self.in_inp = False
        if u.consumed:
            return False                                 # PORT4-Build: a consumed builder takes no order
        if oid == O_ROOT or oid == O_UNROOT:
            return self.order_root(u, oid, x, y)         # PORT4-Build: CAbilityRoot §6 (the orders have no other consumer)
        hr = self.hv.order(u, oid, x, y, target)     # PORT-Harvest: harvest / return / autoharvest / eat tree
        if hr is not None:
            return hr
        if target is not None and getattr(target, 'tname', '') == 'destructable':
            return self.order_dest(u, oid, target)
        if target is not None and getattr(target, 'tname', '') == 'item':
            return self.itm.order_pickup(u, target, smart=(oid == O_SMART)) if oid in (O_SMART, 851981) else False
        if u.hero and x is None and target is None and self.hero.slot_ok(u, oid):
            return self.hero.learn(u, oid)          # W8: learn a hero ability (NOTES_heroes §3; AUfa → AUfu slot)
        r = self.sp.order(u, oid, x, y, target)    # W8: an ability that answers the order id (spells.py)
        if r is not None:
            return r
        # CAbilityRevive §4 / §8.21: the altar's 12 revive buttons 0xd003b…0xd0046 and 0xd0047 carry the dead hero as
        # the target unit (the list index / the target itself)
        if (target is not None and 852027 <= oid <= 852039 and getattr(target, 'hero', False)
                and int(u.data.get('Revive') or 0) > 0):
            return self.order_revive_hero(u, target, oid - 852027 if oid <= 852038 else None)
        # build order: order id = unit type id in the builder's Builds list (NOTES_units §4)
        if x is not None and target is None and oid in N.unit_types and self.builds(u, oid):
            return self.order_build(u, oid, x, y)
        if x is None and target is None and oid in N.unit_types and 0x41 <= (oid >> 24) <= 0x5a and (
                self.trains(u, oid) or int(u.data.get('Revive') or 0) > 0):
            r = self.order_revive(u, oid)                         # W9R3-1: a dead hero of that type revives instead
            if r is not None:
                return r
        if x is None and target is None and oid in N.unit_types and self.trains(u, oid):
            return self.order_train(u, oid)
        if x is None and target is None and oid in self.upgrades and self.listed(u, 'Researches', oid):
            return self.order_research(u, oid)
        if x is None and target is None and oid in N.unit_types and self.listed(u, 'Upgrade', oid):
            return self.order_upgrade(u, oid)                     # W8 building upgrade (NOTES_skibi §3)
        if x is None and target is None and oid in (O_CANCEL, O_CANCEL2) and u.constructing:
            return self.construct_cancel(u)                       # BUGS ETD-ORA8 [obs oracle td_construct]
        if x is None and target is None and oid in (O_CANCEL, O_CANCEL2) and u.upg is not None:
            return self.upgrade_cancel(u)                         # A-UP5: cancel ends the upgrade first
        if x is None and target is None and oid in (O_CANCEL, O_CANCEL2) and u.tq:
            return self.queue_cancel_last(u)                      # cancel = last slot [inf]
        if oid == O_SETRALLY or (oid == O_SMART and u.is_bldg and (x is not None or target is not None)
                                 and (target is None or self.pick_weapon(u, target, explicit=True) < 0)):
            if self.has_rally(u) and (x is not None or target is not None):
                return self.set_rally(u, x, y, target)
            if oid == O_SETRALLY:
                return False
        if oid in (O_STOP, O_HOLD):
            self.clear_order(u)
            u.hold = oid == O_HOLD
            u.next_scan = self.now()
            self.wake(u, self.now())
            return True
        if oid == O_SMART and target is not None and target is not u and self.wg_open(target) and self.wg_user(u):
            self.clear_order(u)                  # A-AB22: right-click on an active waygate = walk through it
            u.order = oid
            u.tx, u.ty = self.wg_dest(target)
            u.wg_via = target
            u.attack_move = False
            if self.eff_speed(u, True) > 0:
                N.moving.add(u)
            u.next_scan = self.now()
            self.wake(u, self.now())
            return True
        if oid == O_ATTACKGROUND and x is not None and target is None:
            wi = self.ground_weapon(u)             # BUGS ATK-GND: only an artillery / msplash weapon takes the order
            if wi < 0:
                return False
            self.clear_order(u)
            u.order = oid
            u.atk_w = wi
            u.gpt = Pt(x, y)
            u.explicit = True
            u.tx, u.ty = x, y
            self.wake(u, self.now())
            return True
        if target is not None and getattr(target, 'tname', '') == 'unit' and oid in (
                O_SMART, O_MOVE, O_AIMOVE, O_PATROL, 851970):
            if not self.order_unit_target_ok(u, target, oid):
                return False                             # MOVE-PORT slot 191 / §9.2: the target checks of the move ability
        if oid in (O_SMART, O_ATTACK, O_ATTACKONCE) and target is not None:
            enemy = not self.ally(u.owner, target.owner)
            wi = self.pick_weapon(u, target, explicit=True)
            if wi >= 0 and (enemy or oid != O_SMART):
                self.clear_order(u)
                u.order = oid
                u.atk_target = target
                u.atk_w = wi
                u.explicit = True
                u.tx, u.ty = target.x, target.y
                self.wake(u, self.now())
                return True
            if oid != O_SMART:
                return False
        if oid in (O_SMART, O_MOVE, O_AIMOVE, O_ATTACK, O_ATTACKONCE, O_PATROL) and (x is not None or
                                                                                    target is not None):
            if not self.can_move(u):
                return False                     # A25: no move ability -> the order is rejected
            if oid == O_PATROL and target is None and math.hypot(r32(x) - u.x, r32(y) - u.y) < PATROL_MIN:
                return True                      # MOVE-PORT [obs R3]: a patrol leg shorter than 100 is taken and dropped
            self.clear_order(u)
            u.order = oid
            u.pat = (u.x, u.y) if oid == O_PATROL and target is None else None
            if target is not None:
                u.target = target
                u.tx, u.ty = target.x, target.y
            else:
                u.tx, u.ty = r32(x), r32(y)
            u.attack_move = oid in (O_ATTACK, O_ATTACKONCE, O_PATROL) and target is None
            if self.eff_speed(u, True) > 0:
                N.moving.add(u)
            u.next_scan = self.now()
            self.wake(u, self.now())
            return True
        return False

    # ================================================================ training + research (NOTES_production §1-§3)
    # queue entries: (id, level) — level 0 = a unit type, else the research level the slot pays for
    def listed(self, u, key, tid):
        t = str(u.data.get(key, '') or '')
        if not t:
            return False
        from .natives import _id4
        return _id4(tid) in [x.strip() for x in t.split(',')]

    def trains(self, u, tid):
        return self.listed(u, 'Trains', tid)

    # ---- rally point (NOTES_orders.md "Rally point" [obs 0x6f1a5190, 0x6f2c2fe0, 0x6f462570])
    def has_rally(self, u):
        """CAbilityRally §1 [obs 0x6f1a5190]: the unit really carries the 'ARal' ability (type list or
        UnitAddAbility); a trainer with an empty Trains list has none."""
        if _rc('ARal') in u.abilities:
            return True
        # fallback (BUGS FF-RALLY): the game adds ARal at unit setup for a type with a valid Trains entry or Revive=1; the
        # port does not add it to the list, so those types answer through the profile [obs 0x6f1a5190, inf]
        from .natives import _id4
        if int(u.data.get('Revive') or 0) > 0:
            return True
        t = str(u.data.get('Trains', '') or '')
        if not t:
            return False
        ids = set(_id4(k) for k in self.N.unit_types)
        return any(x.strip() in ids for x in t.split(','))

    def set_rally(self, u, x, y, target):
        """setrally: own unit / structure, or a neutral-passive (player 15) structure -> follow that widget; a
        destructable -> follow it; else the order's point [obs] (CAbilityRally §4.2)."""
        if target is not None and getattr(target, 'tname', '') == 'unit' and (
                target.owner is u.owner or (target.is_bldg and getattr(target.owner, 'id', -1) == 15)):
            u.rally = ('unit', target)
        elif target is not None and getattr(target, 'tname', '') == 'destructable':
            u.rally = ('dest', target)
        else:
            px, py = (target.x, target.y) if target is not None else (x, y)
            u.rally = ('point', r32(px), r32(py))
        return True

    def rally_order(self, u, nu):
        """TRAIN_FINISH with a rally set: the new unit gets the internal smart-like order 851970 [obs]; ours: smart on
        the widget / move to the point [inf mapping]. The default rally (the building itself) gives no order."""
        r = getattr(u, 'rally', None)
        if not r or nu is None:
            return
        if r[0] == 'unit':
            t = r[1]
            if t is u or not getattr(t, 'alive', False):
                return
            self.issue(nu, O_SMART, target=t)
        elif r[0] == 'dest':
            t = r[1]
            if not rally_dest_alive(t):
                return
            self.issue(nu, O_SMART, target=t)
        else:
            self.issue(nu, O_MOVE, r[1], r[2])

    def food_ok(self, pl, fused):
        cap = min(pl.state.get(4, 0), pl.state.get(6, 0), self.food_ceiling)
        return pl.state.get(5, 0) + fused <= cap

    def research_cost(self, rid, lvl):
        r = self.upgrades.get(rid) or {}
        k = lvl - 1
        return (int(r.get('goldbase', 0) + r.get('goldmod', 0) * k), int(r.get('lumberbase', 0) + r.get('lumbermod', 0) * k),
                int(r.get('timebase', 0) + r.get('timemod', 0) * k))

    def entry_cost(self, e):
        tid, lvl = e
        if lvl < 0:                                   # W9R3-1: a hero revive (no hero any more: nothing to refund [inf])
            h = self.by_serial.get(-lvl)
            return self.revive_cost(h) if h is not None and h.alive else (0, 0)
        if lvl:
            g, l, _t = self.research_cost(tid, lvl)
            return g, l
        d = self.N.unit_types.get(tid) or {}
        return int(d.get('gold', 0) or 0), int(d.get('lumber', 0) or 0)

    def tech_ok(self, pl, tid):
        """§1.2 0xdd: SetPlayerTechMaxAllowed — A-PR7: allowed while the player's living units of the type plus the
        type's slots in its train queues stay below the maximum (−1 = no limit). W8: upgrades to the type count.
        BUGS W8-HERO: a hero type (first char 'A'…'Z') also counts against the 'HERO' maximum
        (SetPlayerMaxHeroesAllowed), over all the player's heroes."""
        if 0x41 <= (tid >> 24) <= 0x5a and not self.hero_room(pl):
            return False
        m = pl.tech_max.get(tid, -1)
        if m < 0:
            return True
        n = 0
        for v in self.N.units:
            if v.owner is pl and v.alive and not v.dead:
                if (v.tid == tid and v.upg is None) or (v.upg is not None and v.upg[0] == tid):
                    n += 1
                for e in v.tq:
                    if e[0] == tid and e[1] == 0:
                        n += 1
        return n < m

    def hero_room(self, pl):
        """'HERO' tech maximum (NOTES_production §1.2, [obs] hero-count limits use the 'HERO' tech count): living
        heroes of the player plus hero slots in its train queues below it; dead heroes are not counted [inf, Open]."""
        m = pl.tech_max.get(HERO_TECH, -1)
        if m < 0:
            return True
        n = 0
        for v in self.N.units:
            if v.owner is pl and v.alive and not v.dead:
                if 0x41 <= (v.tid >> 24) <= 0x5a:
                    n += 1
                for e in v.tq:
                    if e[1] == 0 and 0x41 <= (e[0] >> 24) <= 0x5a:
                        n += 1
        return n < m

    # ---------------------------------------------------------------- W8 requirements (NOTES_skibi §3.5)
    def requires(self, tid):
        r = self.req_cache.get(tid)
        if r is None:
            d = self.N.unit_types.get(tid) or {}
            ids = [t.strip() for t in str(d.get('Requires', '') or '').split(',') if len(t.strip()) == 4]
            am = [t.strip() for t in str(d.get('Requiresamount', '') or '').split(',')]
            r = []
            for i, t in enumerate(ids):
                try:
                    n = int(am[i]) if i < len(am) and am[i] else 1
                except ValueError:
                    n = 1
                r.append(((ord(t[0]) << 24) | (ord(t[1]) << 16) | (ord(t[2]) << 8) | ord(t[3]), max(n, 1)))
            self.req_cache[tid] = r
        return r

    def upgrade_list(self, tid):
        d = self.N.unit_types.get(tid) or {}
        out = []
        for t in str(d.get('Upgrade', '') or '').split(','):
            t = t.strip()
            if len(t) == 4:
                out.append((ord(t[0]) << 24) | (ord(t[1]) << 16) | (ord(t[2]) << 8) | ord(t[3]))
        return out

    def tech_count(self, pl, x, specific, progress=False):
        """BUGS ETD-ORA7 [obs oracle td_techcount_units / td_construct]: GetPlayerTechCount of a unit type: the player's
        living finished units of the type (a structure under construction and a unit being upgraded are not counted);
        specificonly false also counts the types x upgrades to (its Upgrade list, recursive) and a unit being upgraded
        FROM any of them (counted as its old type, not as the type it becomes). `progress` also counts the structures
        under construction / being upgraded to one of those types (GetPlayerTypedUnitCount includeIncomplete,
        Unclassed_Native_0036 §8 [obs 0x6f2d0fb0])."""
        fam = [x]
        if not specific:
            i = 0
            while i < len(fam):
                for t in self.upgrade_list(fam[i]):
                    if t not in fam:
                        fam.append(t)
                i += 1
        n = 0
        for v in self.N.units:
            if v.owner is not pl or not v.alive or v.dead or v.consumed:
                continue
            if progress and ((v.constructing and v.tid in fam) or (v.upg is not None and v.upg[0] in fam)):
                n += 1
                continue
            if v.constructing:
                continue
            if v.upg is None:
                if v.tid in fam:
                    n += 1
            elif not specific and (v.upg[4] or v.tid) in fam:
                n += 1
        return n

    def req_count(self, pl, x, progress, seen):
        """A-UP1: finished living units of x (not upgrading) [+ x under construction / being upgraded to, with
        progress], plus the same count with progress for every type of x's Upgrade list (recursive)."""
        if x in seen:
            return 0
        seen.add(x)
        n = 0
        for v in self.N.units:
            if v.owner is not pl or not v.alive or v.dead or v.consumed:
                continue
            if v.tid == x and not v.constructing and v.upg is None:
                n += 1
            elif progress and ((v.tid == x and v.constructing) or (v.upg is not None and v.upg[0] == x)):
                n += 1
        for t in self.upgrade_list(x):
            n += self.req_count(pl, t, True, seen)
        return n

    def req_met(self, pl, x, n):
        if x in self.upgrades:
            return pl.tech.get(x, 0) >= n                        # a research level
        if x in self.N.unit_types:
            return self.req_count(pl, x, False, set()) >= n
        return True

    def req_ok(self, pl, tid):
        for x, n in self.requires(tid):
            if not self.req_met(pl, x, n):
                return False
        return True

    def research_requires(self, rid, lvl):
        """BUGS ETD-ORA3 [obs oracle td_techmax_research]: the requirements of research level `lvl` (1-based) from the
        upgrade record's 'req' (per level [ids, amounts] strings of UpgradeFunc Requires / Requires1.. and the map's w3q
        greq / grqc): [(type or research id, amount)] — the same lists and amount default as a unit type's Requires."""
        r = self.upgrades.get(rid) or {}
        req = r.get('req') or []
        if lvl < 1 or lvl > len(req):
            return []
        return parse_reqs(req[lvl - 1][0], req[lvl - 1][1])

    def research_req_ok(self, pl, rid, lvl):
        for x, n in self.research_requires(rid, lvl):
            if not self.req_met(pl, x, n):
                return False
        return True

    # ---------------------------------------------------------------- W8 building upgrades (NOTES_skibi §3)
    def upgrade_check(self, u, tid):
        """§3.1 validation of a building upgrade u -> tid (first failure wins): (ok, gold, lumber) with the cost the order
        charges. Shared with the scripted player (autoplay.SoakPlayer), so it never posts an input this rejects."""
        N, pl = self.N, u.owner
        d, od = N.unit_types[tid], u.data
        dg = int(d.get('gold', 0) or 0)
        dl = int(d.get('lumber', 0) or 0)
        if not _f(self.misc, 'RelativeUpgradeCost', 0.0):
            # 0 (melee data): the difference of the cumulative costs [obs]; 1 (Custom_V1): the full new cost [obs real
            # 1.21 game, BUGS DS-1]
            dg -= int(od.get('gold', 0) or 0)
            dl -= int(od.get('lumber', 0) or 0)
        if u.dead or u.constructing or u.upg is not None:
            return False, dg, dl                                  # A-UP5: one upgrade at a time
        if tid not in self.upgrade_list(u.tid):
            return False, dg, dl                                  # CAbilityUpgrade §12: only a target of the type's Upgrade list
        if not self.tech_ok(pl, tid) or not self.req_ok(pl, tid):
            return False, dg, dl                                  # 0xdd / 0xb7
        if pl.state.get(PLAYER_GOLD, 0) < dg or pl.state.get(PLAYER_LUMBER, 0) < dl:
            return False, dg, dl                                  # 8 / 9
        df = max(0, int(d.get('fused', 0) or 0) - int(od.get('fused', 0) or 0))
        if df > 0 and not self.food_ok(pl, df):
            return False, dg, dl                                  # 3
        return True, dg, dl

    def order_upgrade(self, u, tid):
        """§3.1 validation (upgrade_check), §3.2 start: Δ cost paid now (a negative Δ gives gold back), time =
        bldtm(new) as whole seconds, the unit keeps its type until the finish, UPGRADE_START."""
        N, pl = self.N, u.owner
        d, od = N.unit_types[tid], u.data
        ok, dg, dl = self.upgrade_check(u, tid)
        if not ok:
            return False
        t = float(int(float(d.get('bldtm', 0) or 0)))             # whole seconds [obs]
        u.upg = [tid, self.now() + (steps_of(t) if t > 0 else 1), dg, dl, 0]
        u.upg_ion = False
        if not _f(self.misc, 'UpgradeInProgressIdChange', 0.0):
            # CAbilityUpgradeInProgress §4.3 / NOTES_skibi §3.2: max life runs old -> new linearly when the two
            # differ by at least 0.05; the current-life fraction is kept on every step.
            m0 = float(od.get('hp', 0) or 0)
            m1 = float(d.get('hp', 0) or 0)
            if abs(m1 - m0) >= 0.05:
                u.upg_ion = True
                u.upg_i0, u.upg_i1, u.upg_ic = m0, m1, m0   # ic is the interpolated TYPE max, not the live max
                u.upg_is = self.now()
        self.upgrading.append(u)
        if dg:
            N.set_player_state(pl, PLAYER_GOLD, pl.state.get(PLAYER_GOLD, 0) - dg)
        if dl:
            N.set_player_state(pl, PLAYER_LUMBER, pl.state.get(PLAYER_LUMBER, 0) - dl)
        if _f(self.misc, 'UpgradeInProgressIdChange', 0.0):
            # 1 (Custom_V1): the unit is the new type from the start [obs real 1.21 game: GetUnitTypeId right after the
            # order]; the morph itself (life %, abilities, food) at the start [inf]; the finish only fires the event
            u.upg[4] = u.tid
            self.morph_type(u, tid, abils=False)                  # BUGS ETD-ORA5 [obs oracle td_upgrade_events]: abilities at the FINISH
        self.log('upgrade start u%d %s -> %s' % (u.serial, od.get('id4'), d.get('id4')))
        N.unit_event(u, 'UPGRADE_START', {})
        return True

    def morph_abilities(self, u, od, d):
        """the type's ability set swaps: the old type's abilities the new one lacks go, the new type's missing ones come
        at level 1 (a shared ability keeps its level)."""
        ab = self.ab
        olds = [a.strip() for a in str(od.get('abilList', '') or '').split(',') if len(a.strip()) == 4]
        news = [a.strip() for a in str(d.get('abilList', '') or '').split(',') if len(a.strip()) == 4]
        for a in olds:
            code = (ord(a[0]) << 24) | (ord(a[1]) << 16) | (ord(a[2]) << 8) | ord(a[3])
            if a not in news and code in u.abilities:
                if u.perm_abil.get(code, 0) > 0:      # Unclassed_Native_0032 §6: a permanent instance survives the morph
                    continue
                u.perm_abil.pop(code, None)
                ab.on_remove_abil(u, code, u.abilities.pop(code))
        for a in news:
            code = (ord(a[0]) << 24) | (ord(a[1]) << 16) | (ord(a[2]) << 8) | ord(a[3])
            if code not in u.abilities:
                u.perm_abil.pop(code, None)           # a new ability instance starts with a zero counter
                u.abilities[code] = 1
                ab.on_add(u, code)

    def refresh_fighter(self, u):
        """re-register a unit in the fighter list after a runtime weapon-enabled change (renw / the native field write)."""
        fights = any(self.has_weapon(u, i) for i in (0, 1))
        if fights and not u.fighter:
            self.fighters.append(u)
            u.fighter = True
            u.next_scan = self.now()
            self.wake(u, self.now())
        elif not fights and u.fighter:
            if u in self.fighters:
                self.fighters.remove(u)
            u.fighter = False
            u.atk_target = None

    def morph_type(self, u, tid, food=True, abils=True):
        """§3.3: the unit becomes tid in place (same handle): life percentage kept, type abilities swapped, weapons /
        armor / speed / regen / food made of the new type. abils False: the ability set stays (BUGS ETD-ORA5: with
        UpgradeInProgressIdChange 1 the type changes at the START, the abilities morph at the FINISH)."""
        N = self.N
        od, d = u.data, N.unit_types[tid]
        pct = u.life / u.maxlife if u.maxlife > 0 else 1.0
        if food:
            N.food_add(u.owner, 0, -int(od.get('fmade', 0) or 0))
        u.tid, u.data, u.own_data = tid, d, False             # NAT-1: the unit field overrides go with the old type
        u.ucls = int(d.get('ucls', 0) or 0) | (u.ucls & 0x4)
        if abils:
            self.morph_abilities(u, od, d)
        u.maxlife = r32(float(d.get('hp', 0) or 1))
        u.life = r32(u.maxlife * pct)
        u.maxmana = r32(float(d.get('mana', 0)))
        if u.mana > u.maxmana:
            u.mana = u.maxmana
        u.base_speed = float(d.get('spd', 0) or 0)
        u.armor = float(d.get('def', 0) or 0)
        u.def_type = int(d.get('defType', 4) if d.get('defType', '') != '' else 4)
        u.invulnerable = AVUL in u.abilities or u.invuln_n > 0
        self.ue.reset_type(u)                                 # BUGS FF-UPG: the new type's life / mana start without the old bonuses
        self.set_regen(u)
        fights = any(self.has_weapon(u, i) for i in (0, 1))
        if fights and not u.fighter:
            self.fighters.append(u)
            u.fighter = True
        elif not fights and u.fighter:
            if u in self.fighters:
                self.fighters.remove(u)
            u.fighter = False
            u.atk_target = None
        u.next_scan = self.now()
        self.wake(u, self.now())
        if food:
            N.food_add(u.owner, 0, int(d.get('fmade', 0) or 0))
        N.dirty.add(u)

    def upgrade_interp(self, u):
        """max life = old max + (new max - old max) * progress, applied as a delta so other life bonuses stay;
        the current-life fraction is kept (NOTES_skibi §3.2 / CAbilityUpgradeInProgress §5)."""
        if not u.upg_ion or u.upg is None:
            return
        span = float(u.upg[1] - u.upg_is)
        prog = 1.0 if span <= 0 else (self.now() - u.upg_is) / span
        if prog < 0.0:
            prog = 0.0
        elif prog > 1.0:
            prog = 1.0
        target = u.upg_i0 + (u.upg_i1 - u.upg_i0) * prog
        d = target - u.upg_ic
        if d:
            old = u.maxlife
            frac = u.life / old if old > 0 else 1.0
            u.maxlife = r32(max(1.0, old + d))
            nl = r32(frac * u.maxlife)
            if u.alive and not u.dead and nl < 1.0:
                nl = 1.0
            u.life = nl
            u.upg_ic = target

    def upgrade_finish(self, u):
        tid, old = u.upg[0], u.upg[4]
        u.upg = None
        u.upg_ion = False
        od = u.data if not old else self.N.unit_types[old]
        if not old:
            self.morph_type(u, tid)
        else:                                         # UpgradeInProgressIdChange 1: the type changed at the start, now the abilities
            self.morph_abilities(u, od, u.data)
        self.log('upgrade finish u%d %s -> %s' % (u.serial, od.get('id4'), u.data.get('id4')))
        self.N.unit_event(u, 'UPGRADE_FINISH', {})

    def upgrade_cancel(self, u):
        """§3.4: refund trunc(Δ × UpgradeRefundRate), the unit stays the old type, UPGRADE_CANCEL."""
        N, pl = self.N, u.owner
        _tid, _end, dg, dl, old = u.upg[:5]
        if u.upg_ion and not old:
            d = u.upg_i0 - u.upg_ic                          # the provider goes: back to the old type's max
            if d:
                cur = u.maxlife
                frac = u.life / cur if cur > 0 else 1.0
                u.maxlife = r32(max(1.0, cur + d))
                nl = r32(frac * u.maxlife)
                if u.alive and not u.dead and nl < 1.0:
                    nl = 1.0
                u.life = nl
        u.upg = None
        u.upg_ion = False
        if u in self.upgrading:
            self.upgrading.remove(u)
        if old:                                       # UpgradeInProgressIdChange 1: back to the old type [inf]; the abilities never changed
            self.morph_type(u, old, abils=False)
        rg, rl = int(dg * self.upg_refund), int(dl * self.upg_refund)
        if rg:
            N.set_player_state(pl, PLAYER_GOLD, pl.state.get(PLAYER_GOLD, 0) + rg)
        if rl:
            N.set_player_state(pl, PLAYER_LUMBER, pl.state.get(PLAYER_LUMBER, 0) + rl)
        self.log('upgrade cancel u%d' % u.serial)
        u.order = O_CANCEL                            # BUGS ETD-ORA6 [obs]: GetUnitCurrentOrder in UPGRADE_CANCEL = 851976
        N.unit_event(u, 'UPGRADE_CANCEL', {})
        if u.order == O_CANCEL:
            u.order = 0
        return True

    @staticmethod
    def tok_pop(u, i):
        """drop the hero-token bit of slot i; tolerant of queues filled without it (tq_tok shorter than tq)."""
        if i < len(u.tq_tok):
            u.tq_tok.pop(i)

    def train_price(self, pl, tid):
        """CAbilityQueue §4.30 [obs 0x6f45ee10]: a unit type whose first character is 'A'…'Z' costs 0 gold and 0
        lumber while the owner's hero-token counter (state 3) is positive; the order consumes one token instead."""
        d = self.N.unit_types.get(tid) or {}
        gold, lumber = int(d.get('gold', 0) or 0), int(d.get('lumber', 0) or 0)
        if 0x41 <= (tid >> 24) <= 0x5a and pl.state.get(PLAYER_HERO_TOKENS, 0) > 0:
            return 0, 0, True
        return gold, lumber, False

    def train_check(self, u, tid):
        """§1.2 validation of order_train (first failure wins), no side effect: -> (ok, gold, lumber, free)."""
        N = self.N
        d = N.unit_types.get(tid)
        if d is None or not self.trains(u, tid) or u.constructing or not self.tech_ok(u.owner, tid):
            return False, 0, 0, False
        if not self.req_ok(u.owner, tid):
            return False, 0, 0, False                             # 0xb7 requirements (NOTES_skibi §3.5)
        if len(u.tq) >= 7:
            return False, 0, 0, False                             # 0x20 queue full
        pl = u.owner
        gold, lumber, free = self.train_price(pl, tid)             # §4.30 hero-token override
        if pl.state.get(PLAYER_GOLD, 0) < gold or pl.state.get(PLAYER_LUMBER, 0) < lumber:
            return False, 0, 0, False                             # 8 / 9
        fused = int(d.get('fused', 0) or 0)
        if fused > 0 and pl.state.get(5, 0) + fused > self.food_ceiling:
            return False, 0, 0, False                             # 7
        if not u.tq and not self.food_ok(pl, fused):
            return False, 0, 0, False                             # 3
        return True, gold, lumber, free

    def order_train(self, u, tid):
        """§1.2 validation (first failure wins) + §1.3 enqueue: cost paid now, slot 0 starts at once."""
        ok, gold, lumber, free = self.train_check(u, tid)
        if not ok:
            return False
        return self.enqueue(u, (tid, 0), gold, lumber, free=free)

    # ---- W9R3-1 hero revive at an altar (NOTES_production §1.4; queue entry (hero type, -hero serial))
    def revive_cost(self, h):
        """(gold, lumber) to revive hero h [obs 0x6f45e140 / 0x6f467740 / 0x6f467870]: factor = min(Base + LevelFactor ×
        (level − 1), ReviveMaxFactor); amount = 5 × trunc((cost × factor + 0.5) / 5), capped by HeroMaxReviveCost<Res>.
        float32 steps (the game's own soft-float truncates; ours rounds [inf], identical in both VMs)."""
        lv = float(h.hlevel - 1)
        d = h.data
        out = []
        for key, base, per, cap in (('gold', self.rv_base, self.rv_lvl, self.rv_maxgold),
                                    ('lumber', self.rv_lbase, self.rv_llvl, self.rv_maxlumber)):
            f = r32(r32(per * r32(lv)) + base)
            f = f if f < self.rv_maxf else self.rv_maxf
            x = r32(r32(float(int(d.get(key, 0) or 0)) * f) + r32(0.5))
            v = int(r32(float(int(r32(x / 5.0))) * 5.0))
            out.append(min(v, cap if cap >= 0 else 0x7fffffff))     # unsigned minimum [obs]
        return out[0], out[1]

    def revive_time(self, h):
        """seconds [obs 0x6f447370]: tf = min(ReviveTimeFactor × level, ReviveMaxTimeFactor); t = 5 × trunc(tf × bldtm / 5),
        at least 1, at most HeroMaxReviveTime."""
        tf = r32(self.rv_tf * r32(float(h.hlevel)))
        tf = tf if tf < self.rv_maxtf else self.rv_maxtf
        t = r32(tf * r32(float(int(float(h.data.get('bldtm', 0) or 0)))))
        t = r32(float(int(r32(t / 5.0))) * 5.0)
        if t < 1.0:
            t = r32(1.0)
        return t if t < self.rv_maxt else self.rv_maxt

    def note_revivable(self, h):
        """CAbilityRevive §3: a dying hero enters the owner's revivable list with an insertion stamp (order age)."""
        if not getattr(h, 'hero', False):
            return
        self.rv_stamp += 1
        h.revive_stamp = self.rv_stamp

    def revivable_list(self, pl):
        """the owner's dead, not-yet-queued heroes in insertion order (§3 / §7): a paid or finished revive leaves it,
        a cancel re-inserts the hero with the stamp it had, so the order survives."""
        out = [v for v in self.N.units if v.owner is pl and v.alive and v.dead and v.hero and not v.illusion
               and not self.revive_queued(v)]
        out.sort(key=lambda v: (getattr(v, 'revive_stamp', 0), v.serial))
        return out

    def dead_heroes(self, pl, tid):
        return [v for v in self.revivable_list(pl) if v.tid == tid]

    def revive_queued(self, h):
        return any(e[1] == -h.serial for v in self.N.units for e in v.tq)

    def order_revive(self, u, tid):
        """None = no dead hero of that type (the normal train path decides); else accepted / rejected with the train codes
        (0x20 queue full, 0xb7 requirements, 8 / 9 gold / lumber, 7 food, 3 acceptance when nothing runs)."""
        pl = u.owner
        dead = self.dead_heroes(pl, tid)
        if not dead:
            return None
        if u.constructing:
            return False
        return self._revive_enqueue(u, dead[0])

    def order_revive_hero(self, u, h, index=None):
        """orders 0xd003b…0xd0047 (§4, §8.21/§8.24): 0xd003b+j must target the list entry at index j; 0xd0047 targets
        the hero itself (its list position). A stale button / a mismatched index is refused."""
        if (u.constructing or h is None or not h.alive or not h.dead or not getattr(h, 'hero', False)
                or h.illusion or h.owner is not u.owner):
            return False
        lst = self.revivable_list(u.owner)
        if index is None:
            if h not in lst:
                return False
        elif index < 0 or index >= len(lst) or lst[index] is not h:
            return False
        return self._revive_enqueue(u, h)

    def _revive_enqueue(self, u, h):
        """§8.21 validation + §8.24 enqueue: gold / lumber paid now, the food reserved when the slot starts."""
        pl = u.owner
        full = len(u.tq) >= 7
        if full and not REVIVE_OVERFLOW:
            return False                                          # 0x20 queue full (the safe refusal; twin: W3Vm.reviveOverflow)
        if not self.req_ok(pl, h.tid):
            return False                                          # 0xb7 requirements
        gold, lumber = self.revive_cost(h)
        if (gold > 0 and pl.state.get(PLAYER_GOLD, 0) < gold) or (lumber > 0 and pl.state.get(PLAYER_LUMBER, 0) < lumber):
            return False                                          # 8 / 9 (a free revive is never refused for a negative stock)
        fused = int(h.data.get('fused', 0) or 0)
        if fused > 0 and pl.state.get(5, 0) + fused > self.food_ceiling:
            return False                                          # 7 not enough food
        if not u.tq and not self.food_ok(pl, fused):
            return False                                          # 3 acceptance test (nothing runs)
        if full:
            # §8.24 step 7: the payment happens and nothing is stored (the branch is [Open] in the chapter; switch OFF keeps refusal)
            return self.enqueue(u, (h.tid, -h.serial), gold, lumber, store=False)
        return self.enqueue(u, (h.tid, -h.serial), gold, lumber)

    def revive_entry_hero(self, e):
        h = self.by_serial.get(-e[1])
        return h if h is not None and h.alive and h.dead else None

    def revive_cancel_hero(self, h):
        """a map-driven ReviveHero: every queued entry of this hero ends with its refund and no event [inf]; the next
        slot of that queue starts."""
        for v in list(self.N.units):
            for i in range(len(v.tq) - 1, -1, -1):
                if v.tq[i][1] == -h.serial:
                    self.queue_cancel_at(v, i, events=False, restart=True)

    def revive_removed(self, h):
        """CAbilityQueue §4.38 [obs 0x6f460b70]: the unit-removal path cancels the slot holding this object with events
        off. A queued hero removed from the game ends silently, nothing is refunded, and the next slot still starts
        (NOTES_production §1.8 [inf])."""
        for v in list(self.N.units):
            for i in range(len(v.tq) - 1, -1, -1):
                if v.tq[i][1] == -h.serial:
                    self.queue_cancel_at(v, i, events=False, restart=True, refund=False)

    def revive_finish(self, u, h):
        """the hero stands next to the altar with HeroReviveLifeFactor life (full life is the native's)."""
        N = self.N
        d = N.unit_types.get(h.tid) or h.data
        nx, ny = u.x, r32(u.y - (u.coll + float(d.get('collision', 0) or 0) + 32.0))
        fp = self.oriented(u.data, u.facing)
        if fp is not None:
            nx, ny = r32(u.x - fp[0] * 16.0 + 16.0), r32(u.y - fp[1] * 16.0 - 16.0)
        life = r32(h.maxlife * self.rv_life)
        mana = r32(float(h.data.get('mana0', 0) or 0)) if self.rv_mana_start else r32(h.maxmana * self.rv_mana)
        N.revive_hero(h, nx, ny, life if life > DEATH_LIFE else DEATH_LIFE, min(mana, h.maxmana), food=False)

    # ---------------------------------------------------------------- Ankh of Reincarnation (BUGS BOSS-ANKH)
    def reinc_begin(self, u):
        """a hero dies carrying an item with an AIrc ability (the Ankh): the first such item (slot order) is used up at once, no
        DROP and no other event [inf], and the hero revives where it died DataA seconds later (7 s [obs runtime 1.21, hero_revive,
        NOTES_heroes 6.1]) with DataB life (500 [obs]); mana unchanged, no REVIVABLE and no REVIVE_* events [obs]. The death
        events (EVENT_UNIT_DEATH, bounty, XP for the killer) are not changed [inf: the probe logged no death line]."""
        if not getattr(u, 'hero', False):
            self.reinc_unit_begin(u)                                  # BUGS ETD-REINC: the unit ability ACrn
            return False
        if u.illusion or self.itm.inventory(u) is None:
            return False
        itm = self.itm
        for k in range(itm.size(u)):
            it = u.items[k]
            if it is None or not it.live:
                continue
            for a in itm.abilities(it):
                r = self.ab.defs[a]
                if r['code'] != 'AIrc' or not r['lv']:
                    continue
                lv = r['lv'][0]
                if itm.inventory(u)['C'] != 0:
                    itm.carry_item(u, it, -1)
                u.items[k] = None
                it.carrier, it.slot, it.live = None, -1, False          # used up; the handle stays valid (A-IT7)
                self.reinc.append((self.now() + int(lv['A'] * SPS), u, r32(lv['B'])))
                return True
        return False

    def reinc_unit_begin(self, u):
        """BUGS ETD-REINC [inf from the map: Element TD gives its undead creeps A00U (base ACrn, DataA 3 s, Cool 3600): its death
        trigger pays no bounty while the unit carries it and its life >= .405 trigger removes it and orders the unit on]: a
        non-hero unit with a ready ACrn dies as usual (the death events run, the ability is still there) and DataA seconds
        later it comes back in place with full life and mana; the ability's cooldown (Cool) then keeps it from repeating."""
        if u.illusion or getattr(u, 'hidden', False):
            return                                       # CAbilityCreepReincarnation §4 steps 2-3: illusion / hidden mask
        if getattr(u, 'rc_wait', False) or getattr(u, 'silenced', 0) > 0:
            return                                       # §4: already in the class group / disabled
        now = self.now()
        if getattr(u, 'rc_ready', 0) > now:
            return
        for code, lvl in u.abilities.items():
            r = self.ab.rec(code)
            if r is None or r.get('code') != 'ACrn' or not r.get('lv'):
                continue
            if code in u.owner.abil_off:
                continue                                 # §4 step 7: the ability is unavailable to the owner
            lv = r['lv'][min(max(lvl, 1), len(r['lv'])) - 1]
            if not (lv['A'] > 0.0):
                return                                   # §4 step 5: the level real must be strictly > 0
            self.reinc.append((now + int(lv['A'] * SPS), u, -1.0))      # life -1: a unit (full life), not an Ankh hero
            u.rc_ready = now + int(lv['Cool'] * SPS)
            u.rc_wait = True                                          # the corpse is not decayed while it waits
            return

    def reinc_due(self):
        return self.reinc and min(e[0] for e in self.reinc) <= self.now()

    def reinc_run(self):
        """world step start: the first due reincarnation in death order, one after the other (the Udon twin is a task per entry)."""
        N = self.N
        while True:
            now = self.now()
            i = next((j for j, e in enumerate(self.reinc) if e[0] <= now), -1)
            if i < 0:
                return
            _due, u, life = self.reinc.pop(i)
            self.wk += 1
            if u.alive and u.dead and getattr(u, 'hero', False) and life >= 0.0:
                N.reinc_revive(u, life)
            elif u.alive and u.dead and life < 0.0:
                N.reinc_unit_revive(u)
            u.rc_wait = False                                     # BUGS ETD-REINC

    def research_busy(self, pl, rid):
        """CAbilityResearch §4.1 [obs 0x6f466430]: the player's tech record already has this research in progress —
        any building of the player counts, not only the one receiving the order."""
        for v in self.N.units:
            if v.owner is pl:
                for e in v.tq:
                    if e[0] == rid and e[1] > 0:
                        return True
        return False

    def research_check(self, u, rid):
        """§3 validation of order_research, no side effect: -> (ok, gold, lumber, level)."""
        r = self.upgrades.get(rid)
        if r is None or not self.listed(u, 'Researches', rid) or u.constructing:
            return False, 0, 0, 0
        pl = u.owner
        cur = pl.tech.get(rid, 0)
        if cur >= int(r.get('maxlevel', 0)) or self.research_busy(pl, rid):
            return False, 0, 0, 0
        tm = pl.tech_max.get(rid, -1)
        if tm >= 0 and cur >= tm:
            return False, 0, 0, 0                                 # BUGS ETD-TECHMAX: 0xdd "allowed" (+0x308) [obs NOTES_production §3]
        if not self.research_req_ok(pl, rid, cur + 1):
            return False, 0, 0, 0                                 # BUGS ETD-ORA3: the level's own requirements (0xb7)
        if len(u.tq) >= 7:
            return False, 0, 0, 0
        gold, lumber, _t = self.research_cost(rid, cur + 1)
        if pl.state.get(PLAYER_GOLD, 0) < gold or pl.state.get(PLAYER_LUMBER, 0) < lumber:
            return False, 0, 0, 0
        return True, gold, lumber, cur + 1

    def order_research(self, u, rid):
        """§3: listed, level below maxlevel, not already researching (0xdd, player-wide); queue full; cost of level current + 1."""
        ok, gold, lumber, lvl = self.research_check(u, rid)
        if not ok:
            return False
        return self.enqueue(u, (rid, lvl), gold, lumber)

    def enqueue(self, u, e, gold, lumber, free=False, store=True):
        N, pl = self.N, u.owner
        if free:                                          # §4.30: a hero token pays for the slot (bit in tq_tok)
            N.set_player_state(pl, PLAYER_HERO_TOKENS, pl.state.get(PLAYER_HERO_TOKENS, 0) - 1)
        else:
            N.set_player_state(pl, PLAYER_GOLD, pl.state.get(PLAYER_GOLD, 0) - gold)
            N.set_player_state(pl, PLAYER_LUMBER, pl.state.get(PLAYER_LUMBER, 0) - lumber)
        if not store:                                     # PORT14-Hero: §8.24 step 7, paid without a slot
            return True
        u.tq.append(e)
        u.tq_tok.append(1 if free else 0)
        if len(u.tq) == 1:
            self.queue_start(u)
        return True

    def queue_start(self, u):
        """§1.4: slot 0 starts — unit: if the food fits (reserve, timer bldtm s, TRAIN_START), else wait for a food
        change; research: timer of its level (whole seconds, A-PR5), RESEARCH_START."""
        if not u.tq or u.tq_end >= 0 or not u.alive or u.dead:
            return
        N = self.N
        tid, lvl = u.tq[0]
        if lvl < 0:                                       # W9R3-1 §1.4 / CAbilityRevive §8.22: a hero revive
            h = self.revive_entry_hero(u.tq[0])
            if h is None:                                 # the hero is gone / alive already: the entry ends silently [inf]
                u.tq.pop(0)
                self.tok_pop(u, 0)
                self.queue_start(u)
                return
            fused = int(h.data.get('fused', 0) or 0)
            if not self.food_ok(u.owner, fused):          # §8.22: the food is reserved at the start
                u.tq_wait = True
                return
            u.tq_wait = False
            if fused:
                N.set_player_state(u.owner, 5, u.owner.state.get(5, 0) + fused)
            u.tq_end = self.now() + max(1, int(self.revive_time(h)) * SPS)    # whole seconds like bldtm
            if u not in self.trainers:
                self.trainers.append(u)
            N.hero_event(h, 'REVIVE_START', {'reviving': N.H(h)})
            return
        if lvl:
            u.tq_end = self.now() + max(1, self.research_cost(tid, lvl)[2] * SPS)
            if u not in self.trainers:
                self.trainers.append(u)
            N.unit_event(u, 'RESEARCH_START', {'research': tid, 'researcher': N.H(u)})
            return
        d = N.unit_types.get(tid) or {}
        fused = int(d.get('fused', 0) or 0)
        if not self.food_ok(u.owner, fused):
            u.tq_wait = True
            return
        u.tq_wait = False
        u.tq_end = self.now() + max(1, int(float(d.get('bldtm', 0) or 0)) * SPS)   # whole seconds [obs]
        if u not in self.trainers:
            self.trainers.append(u)
        if fused:
            N.set_player_state(u.owner, 5, u.owner.state.get(5, 0) + fused)
        N.unit_event(u, 'TRAIN_START', {'trainedtype': tid})

    def food_retry(self, pl):
        for b in [v for v in self.N.units if v.owner is pl and v.tq_wait]:
            if b.tq_wait:
                self.queue_start(b)

    def train_finish(self, u):
        """§1.5: unit next to the building (A-PR1), facing 0, food already reserved; TRAIN_FINISH; next slot.
        §3: research — level applied first, then RESEARCH_FINISH."""
        N = self.N
        tid, lvl = u.tq.pop(0)
        self.tok_pop(u, 0)
        u.tq_end = -1
        if u in self.trainers:
            self.trainers.remove(u)
        if lvl < 0:                                       # W9R3-1: the hero is alive first, then the events, then the rally
            h = self.revive_entry_hero((tid, lvl))
            if h is not None:
                self.revive_finish(u, h)                  # §8.22: the slot's food reservation stays (no second add)
                N.hero_event(h, 'REVIVE_FINISH', {'reviving': N.H(h)})
                self.rally_order(u, h)
        elif lvl:
            pl = u.owner
            if pl.tech.get(tid, 0) < lvl:
                old = pl.tech.get(tid, 0)
                pl.tech[tid] = lvl
                self.ue.apply_rtma(pl, tid, old, lvl)            # PORT8-Upgrade rtma (player-wide tech max)
                self.ue.sync_player(pl)                       # BUGS FF-UPG: the effects reach the player's units before the event
            N.unit_event(u, 'RESEARCH_FINISH', {'research': tid, 'researcher': N.H(u)})
        else:
            d = N.unit_types.get(tid) or {}
            # A-PR1 [obs runtime oracle train1 / mtrain1]: below the trainer's lower-left corner, x = left edge + 16,
            # y = bottom edge - 16 (path texture cells x 32); no free-spot search yet (A-PR1b); no texture: below
            nx, ny = u.x, r32(u.y - (u.coll + float(d.get('collision', 0) or 0) + 32.0))
            fp = self.oriented(u.data, u.facing)
            if fp is not None:
                nx, ny = r32(u.x - fp[0] * 16.0 + 16.0), r32(u.y - fp[1] * 16.0 - 16.0)
            nu = N.create_unit(u.owner, tid, nx, ny, 0.0, food=False)
            N.unit_event(u, 'TRAIN_FINISH', {'trainedtype': tid, 'trained': N.H(nu)})
            self.rally_order(u, nu)
        if u.alive and not u.dead:
            self.queue_start(u)

    def tech_raised(self, pl, tid):
        """BUGS ETD-ORA1 [obs oracle td_research_bump] + CAbilityQueue §4.42 [obs 0x6f460f10]: a script raising the
        researched level (SetPlayerTechResearched / AddPlayerTechResearched) finishes the researches the raise reached
        at once — inside the native: RESEARCH_FINISH before the next statement, the level stays as set (no increment),
        no refund. The binary walks **every** slot of the queue and tests the slot's own id and level (not the raised
        id), so a later slot of another upgrade ends too and the entries shifted behind it are visited; slot 0 then
        starts the next entry [obs]. Running the walk for every queue of the player is [inf] (the caller that visits
        the abilities is outside the chapter; two buildings researching the same upgrade both finish)."""
        for u in list(self.N.units):
            if u.owner is not pl:
                continue
            i = 0
            while i < len(u.tq):
                e = u.tq[i]
                if e[1] > 0 and e[1] <= pl.tech.get(e[0], 0):
                    if i == 0:
                        self.train_finish(u)              # §1.5: slot cleared, RESEARCH_FINISH, next slot starts
                    else:
                        self.research_finish_at(u, i)      # §4.42: the running slot 0 keeps going
                    continue                              # the shifted entries are visited (the binary decrements i)
                i += 1

    def research_finish_at(self, u, i):
        """§4.42: a queued research (slot i > 0) whose level the player already has ends where it stands — the slot is
        dropped and RESEARCH_FINISH fires on the building; no refund, no food, the running slot 0 is untouched."""
        tid, lvl = u.tq.pop(i)
        self.tok_pop(u, i)
        pl = u.owner
        if pl.tech.get(tid, 0) < lvl:
            old = pl.tech.get(tid, 0)
            pl.tech[tid] = lvl
            self.ue.apply_rtma(pl, tid, old, lvl)          # PORT8-Upgrade rtma
            self.ue.sync_player(pl)
        self.N.unit_event(u, 'RESEARCH_FINISH', {'research': tid, 'researcher': self.N.H(u)})

    def queue_cancel_last(self, u, events=True, refund_to=None):
        """§1.6: last occupied slot; TRAIN_CANCEL / RESEARCH_CANCEL before the refund; slot 0 frees its food."""
        if not u.tq:
            return False
        return self.queue_cancel_at(u, len(u.tq) - 1, events, refund_to)

    def queue_cancel_at(self, u, i, events=True, refund_to=None, restart=None, refund=True):
        """one slot: its CANCEL event (W9R3-1: REVIVE_CANCEL of the hero) before the refund; slot 0 frees its food and, with
        events (or `restart`: a revive entry ended by a map ReviveHero), the next slot starts. `refund=False` is the
        hero-removal path (§4.38): the slot ends silently, nothing is paid back."""
        N = self.N
        e = u.tq[i]
        tid, lvl = e
        if events:
            if lvl < 0:
                h = self.revive_entry_hero(e)
                if h is not None:
                    N.hero_event(h, 'REVIVE_CANCEL', {'reviving': N.H(h)})
            elif lvl:
                N.unit_event(u, 'RESEARCH_CANCEL', {'research': tid, 'researcher': N.H(u)})
            else:
                N.unit_event(u, 'TRAIN_CANCEL', {'trainedtype': tid})
        if i >= len(u.tq) or u.tq[i] != e:
            return True                                           # the trigger changed the queue
        pl = refund_to or u.owner
        tok = u.tq_tok[i] if i < len(u.tq_tok) else 0
        if refund:
            if tok:                                               # §4.30: a token slot gives the token back, no gold
                N.set_player_state(pl, PLAYER_HERO_TOKENS, pl.state.get(PLAYER_HERO_TOKENS, 0) + 1)
            else:
                gold, lumber = self.entry_cost(e)
                rate = self.revive_refund if lvl < 0 else (self.research_refund if lvl else self.train_refund)
                N.set_player_state(pl, PLAYER_GOLD, pl.state.get(PLAYER_GOLD, 0) + int(gold * rate))
                N.set_player_state(pl, PLAYER_LUMBER, pl.state.get(PLAYER_LUMBER, 0) + int(lumber * rate))
        u.tq.pop(i)
        self.tok_pop(u, i)
        if i == 0:
            if u.tq_end >= 0 and lvl <= 0:                # §8.25: slot 0 releases its food reservation (train / revive)
                fused = int((N.unit_types.get(tid) or {}).get('fused', 0) or 0)
                if fused:
                    N.set_player_state(pl, 5, pl.state.get(5, 0) - fused)
            u.tq_end = -1
            u.tq_wait = False
            if u in self.trainers:
                self.trainers.remove(u)
            if events or restart:
                self.queue_start(u)
        return True

    def queue_abort(self, u, refund_to=None):
        """death / removal / owner change: every slot from the last, refunded. Train slots stay silent (§1.6);
        a research slot raises RESEARCH_CANCEL (CAbilityResearch §3.2: its death / owner-change cancel does)."""
        while u.tq:
            e = u.tq[-1]
            if e[1] > 0:
                self.N.unit_event(u, 'RESEARCH_CANCEL', {'research': e[0], 'researcher': self.N.H(u)})
            self.queue_cancel_last(u, events=False, refund_to=refund_to)

    def builds(self, u, tid):
        b = str(u.data.get('Builds', '') or '')
        if not b:
            return False
        from .natives import _id4
        return _id4(tid) in [s.strip() for s in b.split(',')]

    def order_build(self, u, tid, x, y):
        d = self.N.unit_types[tid]
        pl = u.owner
        if not self.tech_ok(pl, tid):
            return False
        if not self.req_ok(pl, tid):
            return False                             # 0xb7 requirements (NOTES_skibi §3.5)                         # §1.2 / NOTES_units §4: tech max allowed (A-PR7)
        fused = int(d.get('fused', 0) or 0)
        if fused > 0 and pl.state.get(5, 0) + fused > self.food_ceiling:
            return False                             # 7: FOOD_USED + fused > FoodCeiling (CAbilityBuild_p1 §7.2)
        gold, lumber = int(d.get('gold', 0) or 0), int(d.get('lumber', 0) or 0)
        # CAbilityBuild_p1 §5.3: the cost is paid when the placement is armed (this order). A pending build of the same
        # worker is refunded first, so its amount still counts as available for the new one.
        old = getattr(u, 'build_paid', None)
        eg = pl.state.get(PLAYER_GOLD, 0) + (old[0] if old else 0)
        el = pl.state.get(PLAYER_LUMBER, 0) + (old[1] if old else 0)
        if eg < gold or el < lumber:
            return False                         # A18: resources checked (and paid) at the order
        mine = None
        if self.is_mine_type(d):
            mine = self.mine_site(x, y)                      # PORT4-Build: the mine structure is built ON a neutral gold mine
            if mine is None:
                return False
            x, y = mine.x, mine.y
        ok, cx, cy = self.can_place_over(pl, tid, x, y, u, mine)
        if not ok:
            return False
        self.clear_order(u)                          # refunds a previous armed build (CAbilityBuild §5.3)
        if gold:
            self.N.set_player_state(pl, PLAYER_GOLD, pl.state.get(PLAYER_GOLD, 0) - gold)
        if lumber:
            self.N.set_player_state(pl, PLAYER_LUMBER, pl.state.get(PLAYER_LUMBER, 0) - lumber)
        u.build_paid = (gold, lumber)
        u.order = tid
        u.build = (tid, cx, cy)
        u.tx, u.ty = r32(cx), r32(cy)
        if self.eff_speed(u, True) > 0:
            self.N.moving.add(u)
        self.log('build order u%d %s at %g,%g' % (u.serial, d.get('id4'), cx, cy))
        return True

    # ---- PORT4-Build: CAbilityRoot (Aroo: Ancients root / uproot), docs/decomp/classes/CAbilityRoot_p1.md
    def root_level(self, u):
        for _code, b, lv in self.ab.abil_list(u):
            if b == 'Aroo' and lv is not None:
                return lv
        return None

    def root_init(self, u):
        """the ability is on the unit (type list at creation, UnitAddAbility): a rooted unit [inf: a placed / created Ancient
        stands rooted, §9.4's periodic root keeps it so], its weapon slots are DataA (§3 step 2, §7.5)."""
        lv = self.root_level(u)
        if lv is None:
            u.root_st = 0
            u.root_mask = -1
            return
        if u.root_st == 0:
            u.root_st = 1
        u.root_mask = int(lv['A' if u.root_st == 1 else 'B']) & 3
        if u.bkt is not None:                        # not during creation (init_unit registers the fighter after the ability list)
            self.refresh_fighter(u)

    def root_drop(self, u):
        if u.root_st == 2 and u.footprint is None and u.is_bldg and self.grid is not None and not u.locust:
            self.add_footprint(u)                    # an uprooted Ancient that loses the ability stands where it is
        u.root_st = 0
        u.root_mask = -1
        self.refresh_fighter(u)

    def root_busy(self, u, dur):
        """§4 step 5 / §5 step 4: the unit's abilities are held for the transition (Dur rooting, HeroDur unrooting)."""
        if dur > 0:
            u.root_hold = True
            u.root_pz = not u.paused                 # only a pause the transition set is the transition's to clear
            u.paused = True
            self.ab.at(self.now() + steps_of(dur), 'root_end', u, None)

    def root_end(self, u):
        if u.root_hold:
            u.root_hold = False
            if u.root_pz:
                u.paused = False
            u.root_pz = False
            if u.alive and not u.dead:
                self.wake(u, self.now())

    def order_root(self, u, oid, x, y):
        """§6: root (0xd00c5) is accepted by an unrooted unit with the ability, unroot (0xd00c6) by a rooted one; a point walks
        there first and roots on arrival (§8.2: the placement is tested at the point). A unit in its transition takes neither."""
        lv = self.root_level(u) if u.root_st else None
        if lv is None or u.root_hold or u.constructing:
            return False
        if oid == O_UNROOT:
            if u.root_st != 1:
                return False
            self.clear_order(u)
            self.N.moving.discard(u)
            if u.footprint:
                self.remove_footprint(u)             # §5: the structure's pathing is released as it leaves the rooted state
            u.root_st = 2
            u.root_mask = int(lv['B']) & 3           # §5 step 5: the Uprooted Weapons slots
            self.refresh_fighter(u)
            self.root_busy(u, lv['HeroDur'])
            self.log('unroot u%d' % u.serial)
            return True
        if u.root_st != 2:
            return False
        px = u.x if x is None else r32(x)
        py = u.y if y is None else r32(y)
        self.clear_order(u)
        u.order = O_ROOT
        u.tx, u.ty = px, py
        if (px - u.x) ** 2 + (py - u.y) ** 2 > 16.0 * 16.0 and self.eff_speed(u, True) > 0:
            self.N.moving.add(u)
            return True
        return self.root_arrive(u)

    def root_arrive(self, u):
        """§4: at the point the structure takes its place (a refused placement, §8.2 code 0x44, ends the order) and the unit
        enters the rooted state: Rooted Weapons (DataA), held for Dur."""
        lv = self.root_level(u)
        u.order = 0
        self.N.moving.discard(u)
        u.path = None
        if lv is None or u.root_st != 2:
            return False
        ok, cx, cy = self.can_place(u.owner, u.tid, u.x, u.y, u)
        if not ok:
            self.log('root refused u%d' % u.serial)
            return False
        u.x, u.y = r32(cx), r32(cy)
        self.relocate(u)
        self.N.dirty.add(u)
        if u.is_bldg and not u.locust and self.grid is not None and u.footprint is None:
            self.add_footprint(u)
        u.root_st = 1
        u.root_mask = int(lv['A']) & 3
        self.refresh_fighter(u)
        self.root_busy(u, lv['Dur'])
        self.log('root u%d' % u.serial)
        return True

    # ---- PORT4-Build: a mine structure (Abgm, the Haunted Gold Mine) is built on a neutral gold mine
    # [inf]: CAbilityBuild_p1 §7.3 sends a build order whose target is a gold mine to the type-keyed handlers
    # 0x6f4fba80 / 0x6f56a540 / 0x6f5f2830 (ngol / egol / ugol, outside the chapters, Open) and CAbilityUndeadBuild §5
    # binds the new structure's Abgm to the target's Agld (the pool carries over) and sends the builder into the finished
    # site with harvest (852018, slot 209).  The port: the neutral mine is replaced by the structure, a cancel brings it back.
    MINE_SITE_R = 96.0

    def is_mine_type(self, d):
        r = self.mine_types.get(id(d))
        if r is None:
            r = 'Abgm' in [a.strip() for a in str(d.get('abilList', '') or '').split(',')]
            self.mine_types[id(d)] = r              # cached per unit type (no list parse per order)
        return r

    def mine_site(self, x, y):
        """the neutral gold mine (Agld, owner slot >= 12) nearest to (x, y) within MINE_SITE_R, else None."""
        best, bd = None, None
        r2 = self.MINE_SITE_R * self.MINE_SITE_R
        for o in self.N.units:
            if not o.alive or o.dead or o.owner is None or o.owner.id < 12 or not o.is_bldg:
                continue
            if self.hv.mine_info(o)[0] != 'Agld':
                continue
            d2 = (o.x - x) ** 2 + (o.y - y) ** 2
            if d2 <= r2 and (bd is None or (d2, o.serial) < (bd, best.serial)):
                best, bd = o, d2
        return best

    def can_place_over(self, pl, tid, x, y, builder, mine):
        """can_place with the footprint of the mine it is built on lifted for the test."""
        if mine is None or not mine.footprint:
            return self.can_place(pl, tid, x, y, builder)
        self.remove_footprint(mine)
        r = self.can_place(pl, tid, x, y, builder)
        self.add_footprint(mine)
        return r

    def start_construction(self, u):
        tid, cx, cy = u.build
        d = self.N.unit_types[tid]
        pl = u.owner
        u.build = None
        u.order = 0
        self.N.moving.discard(u)
        mine = self.mine_site(cx, cy) if self.is_mine_type(d) else None
        ok, _cx, _cy = self.can_place_over(pl, tid, cx, cy, u, mine)
        if mine is None and self.is_mine_type(d):
            ok = False
        fused = int(d.get('fused', 0) or 0)
        if not ok or (fused > 0 and pl.state.get(5, 0) + fused > self.food_ceiling):
            self.log('build start failed u%d' % u.serial)
            self.refund_build(u)                     # CAbilityBuild_p1 §5.3: an aborted armed placement is refunded 100 %
            return
        prev = None
        if mine is not None:
            prev = (mine.owner, mine.tid, mine.x, mine.y, mine.facing, getattr(mine, 'res_amt', None))
            self.N.remove_unit(mine)                          # replaced by the structure; a cancel brings it back
        b = self.N.create_unit(pl, tid, cx, cy, self.building_angle, constructing=True)
        if b is None:
            self.refund_build(u)
            return
        u.build_paid = None                          # the paid pair is consumed by the structure that now stands
        if prev is not None:
            b.mine_prev = prev
            b.site_builder = u
        self.log('construct start u%d %s' % (b.serial, d.get('id4')))
        if b.ucls & 0x80 and int(float(u.data.get('race', 0) or 0)) == 4:
            self.consume_builder(u, b)
        self.N.unit_event(b, 'CONSTRUCT_START', {'constructing': self.N.H(b)})

    def consume_builder(self, u, b):
        """PORT4-Build, CAbilityNightElfBuild §3 slot 208 [obs 0x6f564cd0 / 0x6f0f7ea0]: the placed structure is an Ancient (type
        class 0x80) and the builder is a night elf worker (the race picks the build ability, CAbilityBaseBuild §5.4): the
        builder leaves the player's economy and techtree while the structure grows on its own: its food used and food made
        go (§3.3, the same move as a death), it no longer counts for GetPlayerTechCount / requirements, its orders are
        dropped (§3.2) and it takes no more orders. [inf] The unit is hidden while it is consumed (the game keeps it
        out of play; the chapter leaves the physical state Open, §9.1)."""
        u.consumed = True
        b.cbuilder = u
        u.hidden = True
        self.clear_order(u)
        u.build = None
        self.N.moving.discard(u)
        self.N.food_add(u.owner, -int(u.data.get('fused', 0) or 0), -int(u.data.get('fmade', 0) or 0))

    def release_builder(self, b, back):
        """CAbilityNightElfBuild §4 / §5 slot 209 / 210 [obs 0x6f564fc0, 0x6f564e60]: the structure's construction is over.
        back=False (finished, or the structure died / was removed under construction): the consumed builder stays gone
        for good [inf, §5 step 4 / Open 1: the builder dies with a dead structure] and is removed without an event; its
        food was taken at the consumption. back=True (cancelled): food used / made come back (§5 step 3) and the builder is
        released to idle where it stands (§5.1)."""
        u = b.cbuilder
        b.cbuilder = None
        if u is None or not u.consumed:
            return
        if back:
            u.consumed = False
            u.hidden = False
            self.N.food_add(u.owner, int(u.data.get('fused', 0) or 0), int(u.data.get('fmade', 0) or 0))
            self.wake(u, self.now())
        else:
            self.N.remove_unit(u)

    def begin_construction(self, b):
        """called from create_unit(constructing=True) before the unit is visible to JASS."""
        bt = float(b.data.get('bldtm', 0) or 0)
        b.constructing = True
        b.construct_done = self.now() + steps_of(bt) if bt > 0 else self.now() + 1
        b.life = r32(b.maxlife * CONSTRUCT_LIFE0)
        b.construct_pub = CONSTRUCT_LIFE0 * 100.0
        b.construct_rate = (b.maxlife * (1.0 - CONSTRUCT_LIFE0)) / max(bt, STEP)
        self.set_regen(b)
        self.constructing.append(b)

    def construction_time(self, b, t):
        """a construction with its own time (item build, NOTES_items §8): life from 10 % over t seconds."""
        b.construct_done = self.now() + steps_of(t)
        b.construct_pub = CONSTRUCT_LIFE0 * 100.0
        b.construct_rate = (b.maxlife * (1.0 - CONSTRUCT_LIFE0)) / max(t, STEP)

    def set_construction_progress(self, b, pct):
        """UnitSetConstructionProgress [obs Unclassed_Native_0033 §9 0x6f2ccac0]: the comparison is unsigned,
        `99 < pct` -> 100, so every value above 99 AND every negative value becomes 100; 0 stays 0. The native
        writes the percentage (the port's life ramp), it does not write hit points and 100 does not finish the
        building by itself (the construction timer does)."""
        if b is None or not b.alive or b.dead or not b.constructing:
            return
        p = int(pct)
        if p < 0 or p > 99:
            p = 100
        # FloatModifier §Port relevance [obs 0x6f68a530]: a publish below the 0.001 (fraction) epsilon is silent;
        # the native percentage is the 0..100 form, so the threshold is 0.1.
        # PORT2-FIX: compared with the building's actual progress (the life ramp moved on since the last publish)
        cur = b.life * 100.0 / b.maxlife if b.maxlife > 0.0 else 0.0
        if abs(p - cur) < 0.1:
            return
        b.construct_pub = float(p)
        b.life = r32(b.maxlife * (p / 100.0))

    def construct_cancel(self, b):
        """BUGS ETD-ORA8 [obs oracle td_construct]: 851976 on a structure under construction: refund trunc(cost x
        ConstructionRefundRate x (1 - damage / max life)) first (CAbilityBuildInProgress §4.7 / §5, +0x10c), then
        EVENT_PLAYER_UNIT_CONSTRUCT_CANCEL (GetCancelledStructure, gold already refunded), then the structure is gone
        (removed, no death). The no-refund flag (+0x20 & 0x400) belongs to mode-0 item towers and is not modelled."""
        N, pl = self.N, b.owner
        rate = _f(self.misc, 'ConstructionRefundRate', 0.75)
        if _f(self.misc, 'ConstructionDamageRefundPenalty', 1.0) != 0.0 and b.maxlife > 0.0:
            frac = 1.0 - min(1.0, getattr(b, 'construct_dmg', 0.0) / b.maxlife)
            rate *= max(0.0, frac)
        rg, rl = int(int(b.data.get('gold', 0) or 0) * rate), int(int(b.data.get('lumber', 0) or 0) * rate)
        if rg:
            N.set_player_state(pl, PLAYER_GOLD, pl.state.get(PLAYER_GOLD, 0) + rg)
        if rl:
            N.set_player_state(pl, PLAYER_LUMBER, pl.state.get(PLAYER_LUMBER, 0) + rl)
        self.log('construct cancel u%d %s' % (b.serial, b.data.get('id4')))
        if b.cbuilder is not None:
            self.release_builder(b, True)                 # PORT4-Build: CAbilityNightElfBuild §5 slot 210
        N.unit_event(b, 'CONSTRUCT_CANCEL', {'cancelled': N.H(b)})
        b.removed_step = N.vm.steps                   # BUGS RM-READ: its getters still answer in this step
        N.remove_unit(b)
        if b.mine_prev is not None:
            self.restore_mine(b)
        return True

    def restore_mine(self, b):
        """a cancelled or destroyed mine structure gives the neutral gold mine back (same owner, place) [inf]: the gold it
        had when the structure was placed, or, for a finished structure that tracks a pool, what is left of it.
        The decompile is silent on the destruction (CAbilityUndeadBuild §5 binds the pool to the target's Agld)."""
        owner, tid, x, y, face, amt = b.mine_prev
        b.mine_prev = None
        if not b.constructing and hasattr(b, 'res_amt'):
            amt = int(b.res_amt)
        m = self.N.create_unit(owner, tid, x, y, face)
        if m is not None and amt is not None:
            m.res_amt = amt

    def finish_construction(self, b):
        b.constructing = False
        b.next_scan = self.now()
        self.wake(b, self.now())
        b.life = b.maxlife if b.life < b.maxlife else b.life
        self.set_regen(b)
        pid = b.owner.id
        self.stats['built'][pid] = self.stats['built'].get(pid, 0) + 1
        self.log('construct finish u%d %s' % (b.serial, b.data.get('id4')))
        if b.cbuilder is not None:
            self.release_builder(b, False)                # PORT4-Build: CAbilityNightElfBuild §4 slot 209
        w = b.site_builder
        if w is not None:                                 # PORT4-Build: CAbilityUndeadBuild §5.1 slot 209 (harvest 852018 into the finished site)
            b.site_builder = None
            if w.alive and not w.dead and not w.hidden and w.order == 0 and w.owner is b.owner:
                self.issue(w, 852018, None, None, b)
        if b.mine_prev is not None:
            # CAbilityBuildInProgress §7.2 [obs 0x6f568470 / 0x6f10e140, inf]: a completed structure that stands on a
            # gold mine gains the Entangle Gold Mine ability (the lookup is unconditional in the binary; the type gate
            # is elsewhere). The port's only build-on-mine path is the Abgm replacement, marked by mine_prev.
            from .harvest import _rawcode
            code = _rawcode('Aent')
            if code not in b.abilities and code in self.ab.defs:
                b.abilities[code] = 1
                self.ab.on_add(b, code)
        self.N.unit_event(b, 'CONSTRUCT_FINISH', {'constructed': self.N.H(b)})

    # ================================================================ player input (docs/engine_w5.md §6)
    def post_input(self, player, kind, unit_serial, oid, x=0.0, y=0.0, target_serial=0):
        """kind 0 immediate, 1 point, 2 target. Applied at the start of the next turn (after the resume commands),
        in posting order."""
        self.input_seq += 1
        self.inputs.append((self.input_seq, player, kind, unit_serial, oid, r32(x), r32(y), target_serial))

    def post_chat_text(self, player, msg):
        """SAVE-1: the player 'types' a chat message that is no table entry (the game menu's "-load CODE"): the same input
        kind 7 with a dynamic index (natives.chat_dyn_add)."""
        from . import natives as NN
        self.post_input(player, 7, 0, NN.chat_dyn_add(self.N, msg, player))

    def player_controls(self, player, owner):
        """0x6f2a2e60: the owner, or a player the owner shares advanced / shared control with. The input channel
        carries the issuing player, so unlike a JASS Issue*Order this gate has the player context the chapter needs."""
        return owner.id == player or self.shops.control(owner.id, player)

    def apply_inputs(self, turn):
        if not self.inputs:
            return
        self.apply_inputs1(turn)

    def inp_issue(self, u, oid, x=None, y=None, target=None):
        """PERF-AUTOFIELD: the order of a player's input (engine.issue_order takes the flag, the first order issued after it is the player's; events and nested orders are not)."""
        self.in_inp = True
        try:
            return self.N.issue_order(u, oid, x, y, target)
        finally:
            self.in_inp = False

    def apply_inputs1(self, turn):
        batch, self.inputs = self.inputs, []
        run = []                                  # STEER-3: consecutive point orders of one player to one point = one group order (the UI sends one input per selected unit)
        runkey = None
        for _seq, player, kind, us, oid, x, y, ts in batch:
            key = (player, oid, x, y) if kind == 1 else None
            if key != runkey or key is None:
                if len(run) >= 2:
                    self.cluster_form(run, run[0].order, run[0].tx, run[0].ty)
                run = []
                runkey = key
            self.stats['inputs'] += 1
            if kind == 3:                     # test only (soak player / autotest): gold + oid for the player
                pl = self.N.players[player]    # (target serial 1: lumber instead, the maze scenario's walls)
                st, nm = (PLAYER_LUMBER, 'lumber') if ts == 1 else (PLAYER_GOLD, 'gold')
                self.N.set_player_state(pl, st, pl.state.get(st, 0) + oid)
                self.log('input p%d kind 3 %s +%d' % (player, nm, oid))
                continue
            if kind == 10:                    # W9R3-1 test only (ff_duel --revive): a unit of type oid for the player at (x, y)
                nu = self.N.create_unit(self.N.players[player], oid, x, y, 0.0)
                if nu is None:
                    self.stats['inputs_rejected'] += 1
                self.log('input p%d kind 10 spawn %d -> %s' % (player, oid, nu is not None))
                continue
            if kind == 11:                    # W9R3-1 test only: a scripted death of the unit with serial us (no killer)
                ku = self.by_serial.get(us)
                ok = ku is not None and ku.alive and not ku.dead
                if ok:
                    self.N.kill_unit(ku, None)
                else:
                    self.stats['inputs_rejected'] += 1
                self.log('input p%d kind 11 kill u%d -> %s' % (player, us, ok))
                continue
            if kind == 4:                     # W8 dialog click: unit_serial = the dialog handle, oid = button index
                from . import natives as NN
                if us == 0:                   # DBZ-DIALOG: "my dialog" (W3Game.SendInput names the dialog the player's UI shows)
                    us = self.N.hud_dlg.get(player, 0)
                ok = us != 0 and NN.dialog_click(self.N, self.N.players[player], us, oid)
                if not ok:
                    self.stats['inputs_rejected'] += 1
                self.log('input p%d kind 4 dialog h%x button %d -> %s' % (player, us, oid, ok))
                continue
            if kind == 5:                     # W8 purchase (NOTES_items §7): us = shop, oid = type id, ts = buyer (0 none)
                shop = self.by_serial.get(us)
                b = self.by_serial.get(ts) if ts else None
                if ts and (b is None or not b.alive or b.dead or b.owner.id != player):
                    code = -1
                else:
                    code, _sold = self.shops.buy(player, shop, oid, b)
                if code:
                    self.stats['inputs_rejected'] += 1
                self.log('input p%d kind 5 u%d order %d -> %d' % (player, us, oid, code))
                continue
            if kind == 7:                     # BUGS CHAT-1: chat command, oid = index into the chat table (natives.chat_cmds)
                from . import natives as NN
                n = NN.chat_input(self.N, player, oid)
                if n < 0:
                    self.stats['inputs_rejected'] += 1
                self.log('input p%d kind 7 chat %d -> %d' % (player, oid, n))
                continue
            if kind == 8:                     # BUGS SEL-1: selection change, oid = op (select_op), u = the unit
                ok = self.select_op(self.N.players[player], oid, self.by_serial.get(us))
                if not ok:
                    self.stats['inputs_rejected'] += 1
                self.log('input p%d kind 8 sel %d u%d -> %s' % (player, oid, us, ok))
                continue
            u = self.by_serial.get(us)
            if u is None or not u.alive or u.dead or not self.player_controls(player, u.owner):
                self.stats['inputs_rejected'] += 1
                continue
            if kind == 6:                     # W8 pawn (NOTES_items §7.4): u = carrier, oid = slot, ts = shop
                it = u.items[oid] if 0 <= oid < 6 else None
                ok = self.shops.pawn(u, it, self.by_serial.get(ts))
                if not ok:
                    self.stats['inputs_rejected'] += 1
                self.log('input p%d kind 6 u%d pawn slot %d -> %s' % (player, us, oid, ok))
                continue
            if kind == 12:                    # HERO-UI inventory order (items.input_op): oid = slot | op << 3, op 1 move, 2 drop, 3 give
                ok = self.itm.input_op(u, oid & 7, oid >> 3, x, y, ts)
                if not ok:
                    self.stats['inputs_rejected'] += 1
                self.log('input p%d kind 12 u%d op %d slot %d -> %s' % (player, us, oid >> 3, oid & 7, ok))
                continue
            if kind == 0:
                ok = self.inp_issue(u, oid)
            elif kind == 1:
                ok = self.inp_issue(u, oid, x, y)
                if ok and len(run) < self.FORM_CAP:
                    run.append(u)
            else:
                if ts >= DEST_SERIAL:
                    t = self.dest_ser.get(ts)
                    t = t if t is not None and t.dalive and not getattr(t, 'removed', False) else None
                    ok = self.inp_issue(u, oid, target=t) if t is not None else False
                    if not ok:
                        self.stats['inputs_rejected'] += 1
                    self.log('input p%d kind %d u%d order %d -> %s' % (player, kind, us, oid, ok))
                    continue
                if ts < 1000000:
                    t = self.by_serial.get(ts)
                else:                                                  # W8: item serials, ground items only
                    t = self.itm.by_serial.get(ts)
                    t = t if t is not None and t.live and t.carrier is None and t.alive else None
                ok = self.inp_issue(u, oid, target=t) if t is not None and t.alive else False
            if not ok:
                self.stats['inputs_rejected'] += 1
            self.log('input p%d kind %d u%d order %d -> %s' % (player, kind, us, oid, ok))
        if len(run) >= 2:
            self.cluster_form(run, run[0].order, run[0].tx, run[0].ty)

    # ================================================================ selection (BUGS SEL-1)
    SEL_MAX = 12                              # [ext] the game's selection limit
    SEL_GROUP_R = 1500.0                      # [inf] "same type on screen" of a double pick (W3Command.SameTypeR)
    SEL_PRUNE_EVERY = 8                       # CSelectionWar3 §5: the async prune runs per UI tick; 8 steps = 40 ms [inf]

    @staticmethod
    def selectable(u):
        return u is not None and u.alive and not u.dead and not u.hidden and not u.locust

    def selected(self, pl):
        """the player's selection in selection order (dead / hidden / removed units are not in it). A dropped unit
        is only dropped from the returned list: this getter has no side effect (PORT2-FIX: the order stop of a
        dropped member [obs 0x6f312510 virtual +0x194] is not done here, GetUnitsSelected fires it on every call)."""
        return [u for u in pl.sel if self.selectable(u)]

    def prune_selection(self):
        """CSelectionWar3 §5 (0x6f312510): a selected unit that is no longer selectable (dead / hidden / locust)
        leaves the list. Only a dead or removed unit has its orders stopped (virtual +0x194): a merely hidden one
        keeps them (PORT8-FIX: a worker in a mine is hidden, and stopping it would end the trip; likewise a buff
        hidden unit or a consumed night-elf builder). PORT8: the stop runs here, once per tick, not inside the
        selected() getter (PORT2-FIX). The async list, item slot +0x1e4 and leader election stay open."""
        flush = False
        for pl in self.N.players:
            sel = pl.sel
            if not sel:
                continue
            keep = []
            for u in sel:
                self.wk += 1
                if self.selectable(u):
                    keep.append(u)
                else:
                    if u is not None and (not u.alive or u.dead):
                        self.clear_order(u)
                        flush = True
            if len(keep) != len(sel):
                pl.sel = keep
        if flush and self.sp.pend_end:                    # an interrupted channel's ENDCAST is due now (A-SP7)
            self.sp.flush()

    def select_op(self, pl, op, u):
        """op 0 clear, 1 select only u, 2 add u, 3 remove u, 4 select u and the own units of its type within
        SEL_GROUP_R (creation order, max SEL_MAX). Events: DESELECTED of the units that left (old order), then
        SELECTED of the new ones (new order) [inf]; nothing for units that stay. -> False if the op is invalid."""
        old = self.selected(pl)
        if op == 0:
            new = []
        elif op in (1, 2, 4):
            if not self.selectable(u):
                return False
            if op == 1:
                new = [u]
            elif op == 2:
                new = old + [u] if u not in old else old
                if len(new) > self.SEL_MAX:
                    return False
            else:
                new = [u]
                R2 = self.SEL_GROUP_R * self.SEL_GROUP_R
                if u.owner is pl:
                    for v in self.N.units:
                        if len(new) >= self.SEL_MAX:
                            break
                        if v is u or v.tid != u.tid or v.owner is not pl or not self.selectable(v):
                            continue
                        dx, dy = v.x - u.x, v.y - u.y
                        if dx * dx + dy * dy <= R2:
                            new.append(v)
        elif op == 3:
            if u is None:
                return False
            new = [v for v in old if v is not u]
            if u in pl.sel:                          # 0030 SelectUnit §4: the remove primitive stops the unit's orders
                self.clear_order(u)                  # (raw container, so a dead / hidden selected unit is found too)
        else:
            return False
        pl.sel = list(new)
        N = self.N
        for v in old:
            if v not in new:
                N.unit_event(v, 'DESELECTED', {}, player=pl)
        for v in new:
            if v not in old:
                N.unit_event(v, 'SELECTED', {}, player=pl)
        return True

    # ---- control groups (PORT10-Net; CNetCommandUnitDefineControlGroup §1, §3.3)
    GROUP_COUNT = 10                          # ten banks per player (player +0xdc, set stride 0x14)
    GROUP_MAX = SEL_MAX                       # at most 12 units in a set

    def group_member(self, u):
        """The 0x17 sender's filter in port terms: the serial still resolves to this live unit object and the
        unit is not dead. Like the game's filter it does not test hidden / locust (a worker in a mine stays in
        the group). [inf] ``by_serial`` identity stands in for the (handle, birth) pair + live tag."""
        return u is not None and u.alive and not u.dead and self.by_serial.get(u.serial) is u

    def define_group(self, pl, group, units):
        """0x17: fill bank ``group`` 0…9 from ``units``. The sender clamps the *incoming element count* to 12
        before the unit filter runs (Group §3.2 step 1, §3.3), so candidates past the twelfth are not considered;
        the filter then skips invalid elements and compacts them from slot 0 (the game keeps the requested *count*
        on the wire; the in-memory bank stores the compacted list). -> False for a group index outside 0…9."""
        if group < 0 or group >= self.GROUP_COUNT or pl is None:
            return False
        out = []
        n = len(units)
        if n > self.GROUP_MAX:
            n = self.GROUP_MAX
        for u in units[:n]:
            if self.group_member(u):
                out.append(u)
        pl.groups[group] = out
        return True

    def select_group(self, pl, group, add=False):
        """0x18 recall: replace the selection by bank ``group`` (``add`` appends instead). The wire byte order is
        modifier, group; the modifier's meaning is Open (Rep3 §7), so the caller passes the interpreted flag.
        Each member goes through ``select_op``, so dead / hidden members are skipped as for a pick. -> False for
        a group index outside 0…9 or no player."""
        if pl is None or group < 0 or group >= self.GROUP_COUNT:
            return False
        members = pl.groups[group]
        if not add:
            self.select_op(pl, 0, None)
        for u in members:
            self.select_op(pl, 2, u)
        return True

    # ================================================================ combat (NOTES_combat §2)
    def weapon_allows(self, u, i, t, mask=None, explicit=False):
        m = int(self.wv(u, i, 'targs') or 0) if mask is None else mask
        if (m & UD.T_CLASS or not (m & UD.T_ENEMY and not m & 1023)) and not (m & UD.T_CLASS & self.target_class(t)):
            return False                             # BUGS W9-3-LTD2 [inf]: "enemies" and no target-type flag at all = any class
        if t.invulnerable and not (m & UD.T_INVULN):
            return False
        if t.ethereal > 0 and int(self.wv(u, i, 'atkType') or 0) in (1, 2, 3, 5, 6):
            return False                             # A-SP32: physical attacks cannot target ethereal units
        al = m & UD.T_ALLIANCE
        if t is u:
            return bool(al & UD.T_SELF)
        enemy = not self.ally(u.owner, t.owner)
        if not explicit or al:                       # A9: no alliance flag = enemies only
            if al:
                # BUGS W-NEU [obs runtime oracle heron1]: "neutral" alone still lets the weapon attack enemies
                ok = (enemy and al & (UD.T_ENEMY | UD.T_NEUTRAL)) or (not enemy and al & (UD.T_ALLY | UD.T_PLAYER))
                if not ok:
                    return False
            elif not enemy:
                return False
        mr = float(u.data.get('minRange', 0) or 0)
        if mr > 0:
            dx, dy = t.x - u.x, t.y - u.y
            if dx * dx + dy * dy < mr * mr:
                return False                         # NOTES_combat §2.3 minimum range (A10: centre distance)
        return True

    def weapon_reach(self, u):
        """(class mask any weapon targets, any weapon allows allies) of u's type — a pure pre-filter for scan."""
        r = None if u.own_data else self.reach.get(u.tid)          # NAT-1: a unit with field overrides is not its type
        if r is None:
            cls, ally = 0, False
            for i in (0, 1):
                if self.has_weapon(u, i):
                    m = int(self.wv(u, i, 'targs') or 0)
                    cls |= UD.T_CLASS if (m & UD.T_ENEMY and not m & 1023) else m & UD.T_CLASS    # BUGS W9-3-LTD2
                    if m & (UD.T_ALLY | UD.T_PLAYER):
                        ally = True
            r = (cls, ally)
            if not u.own_data:
                self.reach[u.tid] = r
        return r

    def ground_weapon(self, u):
        """first weapon slot that can shoot a ground point: artillery / artillery line / missile splash (ATK-GND)."""
        for i in (0, 1):
            if self.has_weapon(u, i) and int(self.wv(u, i, 'weapTp') or 0) in (3, 5, 8):
                return i
        return -1

    def pick_weapon(self, u, t, explicit=False):
        for i in (0, 1):
            if self.has_weapon(u, i) and self.weapon_allows(u, i, t, explicit=explicit):
                return i
        return -1

    def valid_target(self, u, t):
        if t is None or not t.alive or t.dead or t.hidden:
            return False
        return not t.invisible or self.ab.visible_to(u, t)

    # ---- MOVE-PORT target usability (CAbilityMove_p1 §2.3 slot 191, §2.4 followable test, §9.2)
    def order_visible(self, u, t):
        """the unit is visible to u's owner (slot 191 step 6 / §9.2: 0xba / 0xdd). Fog off = everything visible;
        own units are always visible. The hidden flag is checked separately (0xa9/0xaa)."""
        N = self.N
        if not N.fog[0] and not N.fog[1]:
            return True
        if t.owner is u.owner:
            return True
        g = N.fogg
        if g is None:
            return True                                     # the fog layer has not been built yet (no query side effect here)
        bit = 1 << u.owner.id
        g.want(bit)
        cx, cy = g.cell(t.x, t.y)
        from .fog import V
        return g.state(cx, cy, bit) == V

    def followable(self, u, t):
        """CAbilityMove_p1 §2.4: a patrol may follow t only when t can move, is not hostile and is not an active
        waygate. The two [Open] unit-flag tests (+0x244 bit 0x400, +0x5c bit 0x8000000) have no port flag."""
        if t is None or t is u:
            return False
        if not t.alive or t.dead or t.hidden or not self.can_move(t):
            return False
        if not self.ally(u.owner, t.owner):
            return False                                       # step 6: an enemy is never followed by a patrol
        if self.wg_is_gate(t) and self.wg_open(t):
            return False                                       # step 7: an active gate is walked through instead
        return True

    def order_unit_target_ok(self, u, t, oid):
        """slot 191 / §9.2 for a unit target: self (smart 1 / else 0xdd), dead 0xdd (getitem excepted), hidden
        0xa9/0xaa and not visible 0xba are all rejected by the JASS Boolean path. -> True when accepted."""
        if t is None or t is u:
            return False
        if oid != 851981 and (not t.alive or t.dead):          # getitem keeps a 'dead' item pickable
            return False
        if t.hidden:
            return False
        if not self.order_visible(u, t):
            return False
        if oid == O_PATROL and not self.followable(u, t):
            return False
        return True

    def rng_of(self, u, i):
        # ATTACK-PORT R1 (Initialize lowers the range to a smaller acquire range, 0x6f5508d0) is NOT applied: the 1.26a run had
        # no type with acquire < range, and applying it moves the DBZ map result (-1 s defeat time). NOTES_combat §7.12.
        return max(float(self.wv(u, i, 'rangeN') or 0) + u.upg_rng, 32.0)          # NOTES_combat §2.3 min 32; BUGS FF-UPG ratr

    def in_range(self, u, t, i, extra=0.0):
        """dist^2 <= (range + coll_a + coll_t)^2 (0.001 tolerance), edge to edge [obs 0x6f08ce80]."""
        r = self.rng_of(u, i) + extra + u.coll + t.coll
        dx, dy = t.x - u.x, t.y - u.y
        return dx * dx + dy * dy <= r * r + 0.001

    def may_acquire(self, u):
        if u.explicit or u.build is not None:
            return False
        return u.order == 0 or getattr(u, 'attack_move', False)

    def scan(self, u):
        """NOTES_combat §2.4: radius = acquire (immobile and attack range smaller: attack range; with a target at
        least the attack range); priority A11: in range > current target > closer > lower serial."""
        now = self.now()
        cur = u.atk_target
        best = None
        bkey = None
        bw = -1
        acq = float(u.data.get('acquire', 0) or 0)
        mob = self.can_move(u)
        rmax = max(self.rng_of(u, i) for i in (0, 1) if self.has_weapon(u, i))
        qr = acq if acq > 0 else rmax
        if not mob and rmax < qr:
            qr = rmax
        if cur is not None and qr < rmax:
            qr = rmax
        qr += u.coll + self.max_coll                       # query square covers every candidate radius below
        none_seen = acq <= 0 and cur is None and u.data.get('acqSet')       # NAT-7: acquire range explicitly 0
        for c in (() if none_seen else self.query(u.x, u.y, qr)):
            self.wk += 1
            if c is u or not c.alive or c.dead or c.hidden:
                continue
            cls, _ally = self.weapon_reach(u)
            if not (cls & self.target_class(c)):
                continue                                   # no weapon reaches its class (= pick_weapon -1, cheap)
            if self.ally(u.owner, c.owner):
                continue                                   # A-CB1: never own / allied units by itself (an ally / player
                                                           # flag allows them for an explicit attack only)
            if c.invisible and not self.ab.visible_to(u, c):
                continue                                   # NOTES_abilities §5.9: invisible without a detector
            wi = self.pick_weapon(u, c)
            if wi < 0:
                continue
            rng = self.rng_of(u, wi)
            rad = acq if acq > 0 else rng
            if not mob and rng < rad:
                rad = rng
            if cur is not None and rad < rng:
                rad = rng
            dx, dy = c.x - u.x, c.y - u.y
            d2 = dx * dx + dy * dy
            rr = rad + u.coll + c.coll
            if d2 > rr * rr + 0.001:
                continue
            key = ((2 if self.in_range(u, c, wi) else 0) + (1 if c is cur else 0), -d2, -c.serial)
            if bkey is None or key > bkey:
                best, bkey, bw = c, key, wi
        if best is None:
            la = self.recent_attacker(u, now)
            if la is not None and la.alive and not la.dead and not la.hidden and not self.ally(u.owner, la.owner):
                dx, dy = la.x - u.x, la.y - u.y                # an enemy only (§2.4)
                if dx * dx + dy * dy <= 2000.0 * 2000.0:
                    wi = self.pick_weapon(u, la)
                    if wi >= 0:
                        best, bw = la, wi
        acq = best if (best is not None and best is not cur) else None
        if best is not None and best is not cur:
            if cur is None:
                # ReactionDelay × U(stream 9) (NOTES_abilities §4.4 [obs]; whole steps rounded down: A-RNG2)
                u.react_at = now + int(self.streams.game[9].unit() * self.reaction_s * SPS)
            u.atk_target = best
            u.atk_w = bw
            u.chasing = False
            self.log('acquire u%d -> u%d' % (u.serial, best.serial))
        u.next_scan = now + (SPS if u.atk_target is not None else SPS // 2)
        if acq is not None:
            UE.acquired(self.N, u, acq)                   # W9-5: EVENT_UNIT_ACQUIRED_TARGET (O(1) unregistered)

    def recent_attacker(self, u, now):
        """ATTACK-PORT gap 2: the recent-attacker list of CAbilityAttack (8 slots, 10 s life each, NOTES_combat §7.11 / §7.3).
        The port keeps the 8 newest attackers (one slot per attacker); the answer is the newest slot that is still alive
        [inf: the game's slot choice rides on its remembered preferred target, +0x78]."""
        lst = u.recent_atk
        if not lst:
            return None
        lim = now - RECENT_ATK_STEPS
        keep = [e for e in lst if e[1] > lim]
        if len(keep) != len(lst):
            u.recent_atk = keep
        for a, _at in reversed(keep):
            if a.alive and not a.dead and not a.hidden:
                return a
        return None

    def note_attacker(self, t, a, now):
        lst = t.recent_atk
        for k in range(len(lst)):
            if lst[k][0] is a:
                del lst[k]
                break
        lst.append((a, now))
        if len(lst) > RECENT_ATK_SLOTS:
            del lst[0]

    def owner_changed(self, u):
        """A2 [obs runtime 1.21 owner_swap_target, NOTES_combat 6.4]: a real owner change drops the current attack target
        and cancels the swing in progress (the pending damage point never lands); the cooldown keeps running and the
        unit rescans under the new owner. An alliance change alone does not touch the target."""
        if u.atk_target is not None or u.gpt is not None or u.dp_at >= 0:
            u.dp_at = -1
            u.dp_target = None
            self.drop_target(u)

    def drop_target(self, u):
        u.atk_target = None
        u.gpt = None
        u.chasing = False
        u.path = None
        u.next_scan = self.now()                           # A11: re-scan at once
        self.wake(u, self.now())
        if u.explicit:
            u.explicit = False
            u.order = 0
            self.N.moving.discard(u)
        if u.lc is not None and u.lc['mode'] == 'atk':
            self.sp.lc_dropped(u)                    # NOTES_spells §5.16: the attack-once target is gone (A-SP83)

    def facing_ok(self, u, t):
        dx, dy = t.x - u.x, t.y - u.y
        if dx == 0 and dy == 0:
            return True
        want = math.degrees(math.atan2(dy, dx)) % 360.0
        diff = (want - u.facing + 540.0) % 360.0 - 180.0
        lim = math.degrees(self.half_angle)
        if abs(diff) <= lim:
            return True
        tr = float(u.data.get('turnRate', 0) or 0)
        stepdeg = tr * 360.0 * STEP if tr > 0 else 360.0          # A19: turnRate x 360 deg/s
        if abs(diff) <= stepdeg:
            u.facing = r32(want)
        else:
            u.facing = r32((u.facing + (stepdeg if diff > 0 else -stepdeg)) % 360.0)
        return False

    def fight(self, u):
        now = self.now()
        if u.stunned or u.atk_off:
            return                                    # BPSE stun / BNsi attack block: no attack (woken when it ends)
        if u.cast is not None:
            return                                    # W8: casting / walking to cast (woken at the end)
        if u.dp_at >= 0 and now >= u.dp_at:
            self.damage_point(u)
            if not u.alive or u.dead:
                return
        if now < u.bs_end:
            return
        t = u.gpt if u.gpt is not None else u.atk_target       # BUGS ATK-GND: a ground point is a target
        if t.__class__ is DestPt and not t.alive:
            self.drop_target(u)                    # BUGS W9-PYR-DEST: the destructable is dead / gone: the order is done
            t = None
        if t is not None and t is not u.gpt and not self.valid_target(u, t):
            self.drop_target(u)
            t = None
        if self.may_acquire(u) and now >= u.next_scan:
            self.scan(u)
            t = u.atk_target
        if t is None:
            return
        i = u.atk_w
        if not self.in_range(u, t, i):
            if self.can_move(u) and not u.hold:
                if not u.chasing:
                    u.chasing = True                  # NOTES_combat §2.3: chase to max(range, 32)
                    u.path = None
                if self.eff_speed(u, True) > 0:
                    self.N.moving.add(u)
            else:
                self.drop_target(u)                   # immobile: drop
            return
        if u.chasing:
            u.chasing = False
            u.path = None
        if self.can_move(u) and not self.facing_ok(u, t):
            return                                    # NOTES_combat §2.3 facing (towers skip)
        if now >= u.cd_end and now >= u.react_at:
            self.swing(u, t, i)

    def attack_factor(self, u):
        f = 1.0 + u.atk_delta + u.upg_as                # BUGS FF-UPG rats: the research adds to the same sum
        return 0.2 if f < 0.2 else (5.0 if f > 5.0 else f)

    def set_attack_factor(self, u, delta):
        """NOTES_abilities §6.3: a factor change rescales the running cooldown / damage point / backswing (A-AB3:
        remaining steps × old / new, rounded up)."""
        old = self.attack_factor(u)
        u.atk_delta = delta
        self.refactor_attack(u, old)

    def refactor_attack(self, u, old):
        """the running cooldown / damage point / backswing follow a changed factor (old = the factor before the change)."""
        new = self.attack_factor(u)
        if new == old:
            return
        now = self.now()
        k = old / new
        for name in ('cd_end', 'dp_at', 'bs_end'):
            v = getattr(u, name)
            if v > now:
                setattr(u, name, now + int(math.ceil((v - now) * k - 1e-9)))
        if u.fighter and u.alive and not u.dead:
            nw = self.next_wake(u)
            if nw >= 0:
                self.wake(u, nw)

    def hud_stats(self, u):
        """BUGS FF-UPG: what the unit panel shows (W3Vm.EngUnitArmor / EngUnitDamage): armor with its bonuses (auras, items,
        researches) and the damage range of the first weapon (-1 when it has none): plus + bonuses + dice .. + dice x sides."""
        arm = u.armor + u.armor_bonus
        dmin = dmax = -1
        if self.has_weapon(u, 0):
            dice = int(self.wv(u, 0, 'dice') or 0)
            if dice > 0:
                dice += u.upg_dice
                plus = int(self.wv(u, 0, 'dmgplus') or 0) + u.h_dmg + u.dmg_int + u.upg_dmg
                dmin, dmax = plus + dice, plus + dice * int(self.wv(u, 0, 'sides') or 0)
        return arm, dmin, dmax

    def hud_lines(self):
        """harness twin of W3Vm.HudStatLines: the units that carry research effects, creation order."""
        out = []
        for u in self.N.units:
            if u.alive and u.upg_any:
                arm, dmin, dmax = self.hud_stats(u)
                out.append('u%d %d %d %d %d' % (u.serial, int(arm * 1000.0 + 0.5), dmin, dmax, int(u.maxlife + 0.5)))
        return out

    def speed_changed(self, u):
        pass                                          # eff_speed reads move_delta each step

    def stun_started(self, u):
        """a stun cancels the pending damage point (A-AB9 [inf]); cooldown keeps running. A-SP6: a stun does not end a
        cast yet (the game: ENDCAST, NOTES_production §5.2 [obs]; Open for the U# port)."""
        u.dp_at = -1
        u.dp_target = None

    def swing(self, u, t, i):
        """NOTES_combat §2.2: cooldown starts at the swing start; damage point = dmgpt capped to cooldown - 0.01."""
        now = self.now()
        f = self.attack_factor(u)                     # NOTES_abilities §6.3: clamp(1 + deltas, 0.2, 5)
        cool = float(self.wv(u, i, 'cool') or 0) / f
        dp = min(float(self.wv(u, i, 'dmgpt') or 0) / f, cool - 0.01)
        u.cd_end = now + steps_of(cool)
        u.dp_at = now + steps_of(dp)
        u.dp_target = t
        u.dp_w = i
        self.stats['attacks'] += 1
        if t.__class__ is Pt or t.__class__ is DestPt:
            return                                    # BUGS ATK-GND: no crit / bash roll, nobody is attacked
        self.ab.on_swing(u, t)                        # crit / bash roll at the swing start (§5.1)
        t.last_attacker = u
        self.note_attacker(t, u, now)
        self.N.unit_event(t, 'ATTACKED', {'attacker': self.N.H(u)})     # EVENT_UNIT_ATTACKED at the swing start

    def damage_point(self, u):
        """NOTES_combat §2.2 damage point: range re-check with RngBuff, roll, fire; then backswing."""
        now = self.now()
        t, i = u.dp_target, u.dp_w
        u.dp_at = -1
        u.dp_target = None
        lcu = u.lc is not None and u.lc['mode'] == 'atk'
        if t is None or not t.alive or t.dead or not self.in_range(u, t, i, float(self.wv(u, i, 'RngBuff') or 0)):
            if lcu:
                self.sp.lc_hit(u, False)             # NOTES_spells §5.16: attack-once done (A-SP83)
            return                                   # cancelled: no damage, no missile
        dice = int(self.wv(u, i, 'dice') or 0)
        amount = 0.0
        if dice > 0:                                 # NOTES_combat §2.1: dice <= 0 -> 0 (base not added)
            amount = float(self.roll(dice + u.upg_dice, int(self.wv(u, i, 'sides') or 0),                # BUGS FF-UPG ratd / ratx
                                     int(self.wv(u, i, 'dmgplus') or 0) + u.h_dmg + u.dmg_int + u.upg_dmg))   # + hero /
        # avatar bonus (W8)
        if amount < 0:
            amount = 0.0
        if t.__class__ is DestPt:
            self.dest_strike(u, t, i, r32(amount))   # BUGS W9-PYR-DEST
        elif t.__class__ is Pt:
            self.fire(u, t, i, r32(amount), None)    # BUGS ATK-GND: no ability hooks on a ground shot
        else:
            hit = self.ab.on_damage_point(u, t, i, r32(amount))
            self.fire(u, t, i, hit.amount, hit)
        rem = u.cd_end - now
        if rem < 4:                                  # re-armed at 0.02 s
            u.cd_end = now + 4
            rem = 4
        bs = steps_of(max(0.01, float(self.wv(u, i, 'backSw') or 0)))
        u.bs_end = now + min(bs, rem - 2)
        if lcu and u.lc['mode'] == 'atk':
            self.sp.lc_hit(u, True)

    def fire(self, u, t, i, amount, hit=None):
        tp = int(self.wv(u, i, 'weapTp') or 0)
        atk = int(self.wv(u, i, 'atkType') or 0)
        if tp in (1, 4):                             # normal / instant: no projectile (A21: no cliff misses yet)
            if hit is not None:
                if self.ab.missed(u, t, hit):        # NOTES_abilities §3.2: evasion → no damage
                    return
                self.ab.melee_launch(u, t, hit, atk)
                if not t.alive or t.dead:
                    return
                amount = hit.amount                  # §5.43: feedback adds to the hit
            self.damage(t, u, amount, atk, DMG_NORMAL, True, tp == 4)
            if hit is not None and hit.pulv is not None:
                self.ab.pulverize(u, hit.pulv)       # NOTES_abilities §5.25: after the hit's damage (A-SP49)
            return
        if hit is not None:
            hit = self.ab.missile_launch(u, t, hit, tp, atk)
            amount = hit.amount
            if hit.claim is not None and hit.claim[1] in self.ab.DUR_SNAP:
                # the missile carries the buff duration resolved at the launch (CMissileColdArrow §2.1 etc.)
                hit.bdur = self.ab.dur(hit.claim[2], t)
        m = Missile()
        self.missile_serial += 1
        m.serial = self.missile_serial
        m.src, m.owner = u, u.owner
        m.x, m.y = u.x, u.y
        m.kind, m.amount, m.atk, m.w = tp, amount, atk, i
        m.speed = float(self.wv(u, i, 'Missilespeed') or 0)
        if hit is not None and hit.mspeed > 0:
            m.speed = hit.mspeed                     # NOTES_abilities §5.49: the ability's own missile
        m.left = int(self.wv(u, i, 'targCount') or 1) + u.upg_tgt        # PORT4-Upgrade ratc (CUpgradeAttackTargetCount)
        m.hit = [] if t.__class__ is Pt else [t]
        m.hit_rec = hit
        m.evaded = False
        if tp in (3, 8) or t.__class__ is Pt:                             # artillery / aline: a ground point, fixed at launch
            m.target = None
        else:
            m.target = t
        # CArtillery §6.2 step 2 [obs 0x6f1f4590]: the shot's registered target pair is accepted by the filter before
        # the rings (the primary is hit regardless of the per-player / alliance test); a ground-point shot has none.
        m.pri = t if tp in (3, 8) and t is not None and t.__class__ is not Pt else None
        m.tx, m.ty = t.x, t.y
        # CMissileSplash §9 / §11.1: the five splash floats and the target mask are copied into the missile at
        # launch; an upgrade / weapon swap in flight does not change the shot.
        m.rings0 = self.rings(u, i, tp in (3, 8))     # CArtillery §0.2: artillery zero factors take the defaults
        m.mask0 = int(self.wv(u, i, 'splashTargs') or 0) or int(self.wv(u, i, 'targs') or 0)
        # BUGS ART-LEAD2 [obs runtime oracle th2]: the point stays where the target stood at the launch (no lead)
        self.stats['missiles'] += 1
        if m.speed <= 0:
            self.impact(m)
        else:
            self.missiles.append(m)

    def step_missiles(self, dt):
        if not self.missiles:
            return
        now = self.now()
        for m in list(self.missiles):
            self.wk += 1
            if (now + m.serial) % MISSILE_EVERY:
                continue                             # A19d: a missile advances every 4th step (the maintainer's OK)
            t = m.target
            if t is not None and t.alive and not t.dead:
                m.tx, m.ty = t.x, t.y                # homing (A22: missile types home, artillery does not)
            dx, dy = m.tx - m.x, m.ty - m.y
            dist = math.sqrt(dx * dx + dy * dy)
            d = m.speed * dt * MISSILE_EVERY
            if dist <= d:
                m.x, m.y = m.tx, m.ty
                self.missiles.remove(m)
                self.impact(m)
            else:
                m.x = m.x + dx / dist * d
                m.y = m.y + dy / dist * d

    def impact(self, m):
        """NOTES_combat §3: missile = carried damage on the target; msplash / artillery = splash rings around the
        impact point (msplash: primary target gets the largest factor); mbounce = damage then bounce."""
        k = m.kind
        hr = getattr(m, 'hit_rec', None)
        if hr is not None and m.target is not None:
            t = m.target
            if hr.fb is not None and t.alive and not t.dead:
                self.ab.feedback_impact(m, t)                        # NOTES_abilities §5.43: before the miss check
            if k in (2, 5, 3) and self.ab.missed(m.src, t, hr):
                m.amount = r32(m.amount * self.ab.miss_reduction)   # NOTES_abilities §3.2: evaded missile 50 %
                m.evaded = True
            if t.alive and not t.dead:
                # claimer buff / stun first (A-AB8); the buff step has no miss test, the verdict only scales the carried
                # damage - an evaded missile still applies it (CMissileIncinerate §3.1, CMissileFrost / Corruption slot 51)
                self.ab.on_impact(m, t)
        if k in (2, 7):
            self.damage(m.target, m.src, m.amount, m.atk, DMG_NORMAL, True, True)
            self.ab.corruption_impact(m)
            self.ab.liquid_impact(m)                                     # NOTES_abilities §5.42
        elif k == 5:
            self.splash(m, m.target)
            self.ab.corruption_impact(m)
            self.ab.liquid_impact(m)
        elif k in (3, 8):
            # the registered primary target bypasses the accept filter; the ring that contains it still decides the
            # factor (CArtillery §6.2 step 2 / §6.3), and a ground-point shot has no primary. Unlike msplash the
            # primary is applied in the enumeration order, not before the area sweep.
            self.splash(m, getattr(m, 'pri', None), first=False)
        elif k == 6:
            self.damage(m.target, m.src, m.amount, m.atk, DMG_NORMAL, True, True)
            self.bounce(m)

    def rings(self, u, i, art=False):
        """(Farea, 1), (Harea, Hfact), (Qarea, Qfact) in the game's fixed full -> medium -> small order; radius 0
        rings dropped. The first ring that contains a unit decides its factor, so the order is not the radius order
        (CArtillery §6.3 [obs 0x6f1f4740]). CSplashData §constructor: the block exists only for weapon types
        3 / 5 / 6 / 7 / 8, so a custom weapon of another type has no rings at all. art = the CArtillery launch:
        a zero medium / small factor becomes the startup default 0.5 / 0.25 (CArtillery §0.2 [obs 0x6f1f3af0]);
        CMissileSplash keeps a zero factor as zero (CMissileSplash §9 [obs 0x6f1f8890])."""
        if int(self.wv(u, i, 'weapTp') or 0) not in (3, 5, 6, 7, 8):
            return []
        hf = float(self.wv(u, i, 'Hfact') or 0)
        qf = float(self.wv(u, i, 'Qfact') or 0)
        if art:
            if hf == 0.0:
                hf = 0.5
            if qf == 0.0:
                qf = 0.25
        rs = [(float(self.wv(u, i, 'Farea') or 0), 1.0), (float(self.wv(u, i, 'Harea') or 0), hf),
              (float(self.wv(u, i, 'Qarea') or 0), qf)]
        return [r for r in rs if r[0] > 0]

    def splash(self, m, primary, first=True):
        """first = the primary is applied before the area sweep (CMissileSplash §11.1 step 3). An artillery shot
        (CArtillery §6.2) accepts its registered primary through the filter but applies it in the enumeration order
        like every other victim, so it calls with first=False."""
        u, i = m.src, m.w
        rings = getattr(m, 'rings0', None)                    # launch snapshot (CMissileSplash §9)
        if rings is None:
            rings = self.rings(u, i, getattr(m, 'kind', 0) in (3, 8))
        mask = getattr(m, 'mask0', None)
        if mask is None:
            mask = int(self.wv(u, i, 'splashTargs') or 0) or int(self.wv(u, i, 'targs') or 0)
        if first and primary is not None and primary.alive and not primary.dead:
            # CMissileSplash §11.1 step 3 / §11.2: the primary goes through the same per-victim path as the area
            # victims; the ring that contains it decides the factor (no max-factor shortcut).
            cc = primary.coll
            dx, dy = primary.x - m.tx, primary.y - m.ty
            d2 = dx * dx + dy * dy
            for r, f in rings:
                if d2 <= (r + cc) * (r + cc):
                    if f != 0:
                        self.damage(primary, u, r32(m.amount * f), m.atk, DMG_NORMAL, True, True)
                    break
        if not rings:
            return
        # CArtillery §5.1 step 3 / CMissileSplash §11.1 step 5: the query radius is the small ring when it is > 0,
        # else the medium, else the full ring (not the maximum radius: out-of-order map data enumerates only the
        # small ring). rings keeps the full -> medium -> small order and drops zero radii, so the last entry is
        # the first present of small / medium / full.
        qr = rings[-1][0]
        hits = []
        for c in self.query(m.tx, m.ty, qr + SPLASH_COLL_Q):
            self.wk += 1
            if c is primary and first:
                continue
            if not c.alive or c.dead:
                continue
            isp = c is primary
            if not isp and c.hidden:                 # CArtillery §6.2 step 2: the registered primary is accepted
                continue                             # before the per-player test; the area victims are not
            dx, dy = c.x - m.tx, c.y - m.ty
            d2 = dx * dx + dy * dy
            cc = c.coll                              # BUGS SPL-COLL: a ring reaches the unit's collision circle
            if d2 > (qr + cc) * (qr + cc):
                continue
            if not isp and not self.splash_ok(m, c, mask):
                continue
            for r, f in rings:
                if d2 <= (r + cc) * (r + cc):
                    if f != 0:
                        hits.append((c, f))
                    break
        hits.sort(key=lambda h: h[0].serial)         # creation order
        for c, f in hits:                            # NOTES_combat §3.2: each hit its own full take-damage
            self.damage(c, u, r32(m.amount * f), m.atk, DMG_NORMAL, True, True)

    def splash_ok(self, m, c, mask):
        if not (mask & UD.T_CLASS & self.target_class(c)):
            return False
        if c.invulnerable and not (mask & UD.T_INVULN):
            return False
        al = mask & UD.T_ALLIANCE
        enemy = not self.ally(m.owner, c.owner)
        if al:
            return bool((enemy and al & UD.T_ENEMY) or (not enemy and al & (UD.T_ALLY | UD.T_PLAYER)))
        return enemy                                 # A9

    def bounce_immunity_ok(self, m, c):
        """CMissileBounce §6 step 3 [obs 0x6f1f9270]: with MagicImmunesResistDamage on, a magic shot (attack type 4)
        refuses a magic-immune candidate and a physical one (types 1, 2, 3, 5, 6) an ethereal candidate."""
        if m.atk == 4:
            return not c.magic_immune > 0
        if m.atk in (1, 2, 3, 5, 6):
            return not c.ethereal > 0
        return True

    def bounce(self, m):
        """NOTES_combat §3.3: remaining -1 (targCount includes the first), damage x (1 - loss), closest valid unit
        within Farea of the impact point, nobody twice."""
        m.left -= 1
        if m.left <= 0:
            return
        loss = float(self.wv(m.src, m.w, 'damageLoss') or 0) + m.src.upg_dloss    # PORT8-Upgrade radl
        amt = m.amount
        if abs(loss) >= 0.001:
            amt = r32(amt * (1.0 - loss))
        if abs(amt) < 0.001:
            return
        rad = float(self.wv(m.src, m.w, 'Farea') or 0)
        mask = int(self.wv(m.src, m.w, 'targs') or 0)
        best, bd = None, None
        for c in self.query(m.tx, m.ty, rad):
            self.wk += 1
            if c in m.hit or not c.alive or c.dead or c.hidden:
                continue
            if not self.splash_ok(m, c, mask) or not self.bounce_immunity_ok(m, c):
                continue
            dx, dy = c.x - m.tx, c.y - m.ty
            d2 = dx * dx + dy * dy
            if d2 <= rad * rad and (bd is None or d2 < bd or (d2 == bd and c.serial < best.serial)):
                best, bd = c, d2
        if best is None:
            return
        n = Missile()
        self.missile_serial += 1
        n.serial = self.missile_serial
        n.src, n.owner, n.target = m.src, m.owner, best
        n.x, n.y, n.tx, n.ty = m.tx, m.ty, best.x, best.y
        n.speed, n.kind, n.amount, n.atk, n.w, n.left = m.speed, m.kind, amt, m.atk, m.w, m.left
        n.rings0, n.mask0 = getattr(m, 'rings0', None), getattr(m, 'mask0', None)
        n.pri = None                                  # a bounce is a unit-target missile, not an artillery primary
        n.hit = m.hit + [best]
        self.stats['missiles'] += 1
        if n.speed <= 0:
            self.impact(n)
        else:
            self.missiles.append(n)

    # ================================================================ damage pipeline (NOTES_combat §1.2)
    def armor_mult(self, a):
        """NOTES_combat §0.2: a >= 0: r = a k / (1 + a k); a < 0: table[min(20, -trunc(a))]; mult = 1 - r (r <= 1)."""
        if a >= 0:
            ak = a * self.defense_armor
            r = ak / (1.0 + ak)
        else:
            r = NEG_ARMOR[min(20, -int(a))]
        return 1.0 - r if r <= 1.0 else 0.0

    def damage(self, t, src, amount, atk, dmg_type=DMG_NORMAL, attack=True, ranged=False, dot=False):
        """-> damage dealt. Steps 1..16 of NOTES_combat §1.2 (no buffs: step 7 empty; no cheats: step 13)."""
        if t is None or not t.alive or t.dead:
            return 0.0                                                   # 1
        mask = 1 << dmg_type if 0 <= dmg_type < 32 else 0
        magical = atk == 4 or bool(mask & MAGICAL_MASK)
        physical = atk in (1, 2, 3, 5, 6) or bool(mask & PHYSICAL_MASK)
        if magical and t.magic_immune > 0:
            return 0.0                                                   # 2
        if not magical and physical and t.ethereal > 0:
            return 0.0
        if t.abilities and src is not None and not ranged:              # NOTES_abilities §5.15 thorns, priority 1
            self.ab.thorns(t, src, mask, ranged)
        amount = r32(amount)
        if amount == 0 or t.invulnerable:                                # 6
            if src is not None and src.lsteal is not None:
                self.ab.life_steal(src, t, 0.0)                          # §5.41: a 0 record uses the flag up [inf]
            self.damaged_event(t, src, 0.0)
            return 0.0
        if t.abilities or t.illusion:                                    # 7: priority hooks (NOTES_abilities §3.3)
            amount = self.ab.damage_hooks(t, src, amount, atk, mask, attack, ranged, dot)
            if self.ab.phase_pending is not None:
                pu, pcode = self.ab.phase_pending
                self.ab.phase_pending = None
                self.issue(pu, self.sp.order_id(pcode))             # W8 phase shift (A-SP30)
            if amount == 0:
                if src is not None and src.lsteal is not None:
                    self.ab.life_steal(src, t, 0.0)
                self.damaged_event(t, src, 0.0)
                return 0.0
        if src is not None and t.buffs and mask in (0x10, 0x20):        # NOTES_abilities §5.49 incinerate (prio 8)
            amount = self.ab.incinerate_bonus(t, src, amount)
        floor = min(amount, 1.0)                                         # 8
        if amount < 0 and atk != 0:
            amount = 0.0
        if mask == 0x10:                                                 # 9: armor only for DAMAGE_TYPE_NORMAL
            amount = r32(amount * self.armor_mult(t.armor + t.armor_bonus))
        dt_ = t.def_type if 0 <= t.def_type < 8 else 4
        f = self.dmg_table[atk][dt_] if 0 <= atk < 7 else 1.0
        if f != 1.0:                                                     # 10
            amount = r32(amount * f)
        if t.ethereal > 0 and 0 <= atk < 7 and self.eth_bonus[atk] != 1.0:     # 11
            if src is None or self.eth_ally or not self.ally(src.owner, t.owner):
                amount = r32(amount * self.eth_bonus[atk])
        if amount < floor:                                               # 12
            amount = floor
        amount = self.damaged_event(t, src, amount)                      # 14 (before the life drop; NAT-2: BlzSetEventDamage)
        if not t.alive or t.dead:
            return 0.0
        t.last_src = src                                                 # 15
        life = t.life
        nl = life - amount                                               # 16
        if nl < 0:
            nl = 0.0
        if nl > t.maxlife:
            nl = t.maxlife
        t.life = r32(nl)
        if t.constructing and amount > 0.0:
            t.construct_dmg = min(t.maxlife, t.construct_dmg + amount)   # +0x10c damage for the cancel refund
        if self.trace is not None:
            self.log('dmg u%d %s -> %s' % (t.serial, fmt_g(amount), fmt_g(t.life)))
        if src is not None and src.lsteal is not None:
            self.ab.life_steal(src, t, amount if amount < life else life)   # §5.41 damage-dealt hook (A-AB15)
        if src is not None and src.lc is not None:
            self.sp.lc_dealt(src, amount if amount < life else life)        # NOTES_spells §5.16 Aloc +0x170 (A-SP83)
        if t.life <= UNIT_DEATH_LIFE:                                        # [obs runtime 1.26a] unit threshold 0.405 (inclusive)
            if not (t.abilities and self.sp.phx_veto(t)):                    # §5.50 the phoenix death claim
                self.N.kill_unit(t, src)
        elif attack and amount > 0 and src is not None:
            self.flee_check(t, src)                                          # MOVE-PORT NOTES_class_CAbilityMove §8
        return amount

    def damaged_event(self, t, src, amount):
        """NAT-2: the damage of the event is one shared value (a box): BlzSetEventDamage in any registered trigger changes
        it for the triggers after it and for the damage the unit takes; -> the final amount."""
        self.stats['damage_events'] += 1
        box = [r32(amount)]
        self.N.unit_event(t, 'DAMAGED', {'damage': box[0], 'damagesource': self.N.H(src) if src else 0, 'dmgbox': box})
        return box[0]

    # ================================================================ MOVE-PORT (docs/NOTES_class_CAbilityMove.md §5.2 / §5.3 / §8, §14)
    def follow_stop(self, u):
        """FollowRange (smart / move on a unit): the follower stands once the centre distance is within range + the
        target's collision radius + its own [obs runtime 1.26a R4: footman -> footman stopped at 354.9, footman -> barracks
        at 171.0 (collision 144 = diameter); the exact edge terms are [inf], the oracle numbers sit inside the stop distance]. Range 300, 100 for a
        structure target. -> None (keep walking), False (stands, the order goes on: the target may walk away again) or
        True (a structure target: the order is done) [obs R4: order 851971 stays with the unit target, 0 with the structure]."""
        t = u.target
        if not t.alive or t.dead or t is u or u.lc is not None or not self.ally(u.owner, t.owner):
            return None                           # swarm locusts steer themselves; a hostile target is the attack path's
        bl = bool(t.is_bldg)
        rng = (FOLLOW_RANGE_BLDG + t.coll * 0.5 if bl else FOLLOW_RANGE + t.coll) + u.coll       # a structure's collision value is its diameter [inf]
        dx, dy = t.x - u.x, t.y - u.y
        if dx * dx + dy * dy > rng * rng:
            return None
        u.seg = None
        return bl

    def patrol_turn(self, u):
        """two-point patrol 0xd0017 [obs R3]: at the far point the unit turns round to the point it started from (and back)."""
        px, py = u.pat
        u.pat = (u.tx, u.ty)
        u.tx, u.ty = px, py
        u.path = None
        u.seg = None
        self.log('patrol turn u%d' % u.serial)
        self.wake(u, self.now())

    def flee_player_controlled(self, t):
        """the unit flag +0x5c bit 2 ('player-controlled'): [obs runtime 1.26a, docs/oracle.md "flee"] the flag means
        "player slot vs creep", not the controller - a worker of USER, COMPUTER (no AI) and an empty USER slot all fled
        with the player-slot distance, a creep / neutral owner (slot >= 12) with the creep one; the controller did not
        change anything. Used by the allied-hero friendly-fire branch (§7.5) and the flee distance split."""
        return getattr(t.owner, 'id', 12) < 12

    def flee_can_move_now(self, src):
        """0x6f1b8910(A, 1, 0) 'the attacker can move now' [inf: the port's move gate on the ability, root, pause,
        cast and speed]. A tower or a rooted / casting attacker cannot move, so the kiting branch applies."""
        return (self.can_move(src) and self.eff_speed(src) > 0 and not src.stunned
                and not src.paused and src.cast is None and not src.constructing)

    def flee_has_bens(self, src):
        """the attacker carries the Bens (ensnare) buff [inf: the port's ensnare buff kind, Bena / Beng / Bwea / Bweb]."""
        return any(getattr(bf, 'kind', '') == 'ensnare' for bf in getattr(src, 'buffs', ()))

    def flee_ai_flags(self, t):
        """the owner player record +0x2d0 [obs NOTES §8 step 2]: hero bit 0x8, other unit bit 0x10. The port keeps that
        word as the AI configuration flags; a player without an AI object, or with the AI slot not started, has 0
        (no AI-requested flee) - the same gate as W3Engine.FleeAiFlag (aiOn[slot])."""
        a = self.N.ais.get(t.owner.id) if getattr(t.owner, 'id', None) is not None else None
        if a is None or not getattr(a, 'on', False):
            return 0
        return int(getattr(a, 'flags', 0) or 0)

    def flee_decide(self, t, src):
        """CAbilityMove_p2 §7 / NOTES_class_CAbilityMove §8 step 1-6: the binary's flee decision for a hostile attacker.
        The port proxies: the canFlee unit flag is not carried (a unit with an attack ability may decide), the +0x244
        bit 4 / +0x194 busy tests have no producer, the Bens buff is the ensnare buff kind, and 'can move now' is the
        port's move gate. Life windows are the 10-point bands above 600/500/400/300/200/100. [inf] the port keeps the
        idle / no-order-target gate: a unit that is mid-attack (an attack order target and not idle) keeps its order."""
        if getattr(t, 'illusion', False):
            return False
        if t.atk_target is not None and t.order != 0:
            return False                               # [inf] mid-attack units keep their order (no flee)
        if t.order == O_MOVE:                          # step 1: a plain move order never decides to flee
            return False
        flags = self.flee_ai_flags(t)
        if t.hero:
            if not (flags & AI.FLAG_HEROES_FLEE):      # step 2: the owner record's hero-flee bit
                return False
        elif not (flags & AI.FLAG_UNITS_FLEE):         # step 2: the owner record's unit-flee bit
            return False
        if not t.fighter:                              # step 3: a unit without an attack ability flees
            return True
        if self.eff_speed(t) < FLEE_MIN_SPEED:         # step 3: no flee below speed 100
            return False
        if not src.fighter:                            # step 3: an attacker without an attack ability: no flee
            return False
        wi = self.pick_weapon(t, src)
        rs = self.rng_of(t, wi) if wi >= 0 else 0.0
        wj = self.pick_weapon(src, t)
        ra = self.rng_of(src, wj) if wj >= 0 else 0.0
        cur = t.atk_target
        # step 4: kiting when the attacker cannot move; a snared attacker with no longer reach
        if not self.flee_can_move_now(src) and rs > ra:
            return True
        if rs <= ra and cur is not src and self.flee_has_bens(src):
            return True
        # step 5: the 10-point life windows
        L = t.life
        if ra < 200.0 or rs > 200.0:
            if (600.0 <= L <= 610.0) or (500.0 <= L <= 510.0) or (400.0 <= L <= 410.0) or (300.0 <= L <= 310.0):
                return True
        if (200.0 <= L <= 210.0) or (100.0 <= L <= 110.0):
            return True
        # step 6
        if not t.hero:
            return L < 150.0
        if (400.0 <= L <= 410.0) or (300.0 <= L <= 310.0):
            return True
        if src.hero and ra < 200.0 and rs > 200.0:
            return True
        if L < 200.0:
            return True
        return t.maxlife > 0 and L / t.maxlife < 1.0 / 3.0

    def flee_proxy(self, t, src):
        """[obs runtime 1.26a] docs/oracle.md "1.26a rule round" / flee: ONLY workers flee (flee_rules.j, 2 runs). The idle
        Ahar worker of any owner runs; footmen, riflemen and Paladins (full or wounded, any owner and controller) never do,
        so the older proxy branches (a wounded hero, an idle armed non-hero of a computer / neutral owner) are gone. The
        mid-attack gate (an attack target and a non-idle order) refuses the proxy too."""
        if t.atk_target is not None and t.order != 0:
            return False                               # [inf] mid-attack units keep their order (no flee)
        if t.hero or AHAR not in t.abilities:
            return False
        return t.order == 0 and not t.explicit and self.eff_speed(t) >= FLEE_MIN_SPEED

    def flee_allied(self, t, src):
        """CAbilityMove_p2 §7.5: an allied hero attacking the unit makes a player-controlled unit retreat; nothing while
        that hero's current attack target is the unit. The mid-attack gate (an attack target and a non-idle order)
        refuses this branch too. 1.26a prefers the damage record's word 4 as the flee source [inf: the port passes the
        attacker, as the record carries no separate unit]."""
        if t.atk_target is not None and t.order != 0:
            return False                               # [inf] mid-attack units keep their order (no flee)
        if src.fighter and src.atk_target is t:
            return False
        return (self.flee_player_controlled(t) and src.hero and not getattr(src, 'illusion', False))

    def flee_check(self, t, src):
        """NOTES_class_CAbilityMove §8 + CAbilityMove_p2 §6/§7 (the unit was damaged). A hostile attacker uses the
        decompiled flee decision (§7 step 1-6) or the port's observation proxy; an allied hero that attacks the unit
        makes a player-controlled unit retreat (§7.5). Distance [obs runtime 1.26a docs/oracle.md flee]: a worker of a
        player-slot owner 300 (plain move order), a worker of a creep owner ~1200 (order 852018); heroes never flee; a
        non-worker (AI-requested / allied) keeps max(300, attacker range + 100). A
        zero-length difference uses the unit's facing (§6.1 step 3, epsilon 2e-7); the attacker must be visible to the
        owner (§7.2)."""
        if not t.alive or t.dead or src is None:
            return
        if t.paused or t.stunned or t.cast is not None or t.constructing or not src.alive or src.dead:
            return
        if t.hold or t.build is not None or t.gpt is not None:
            return
        if not self.can_move(t):
            return
        if not self.order_visible(t, src):
            return                                    # §7.2: an attacker outside the owner's fog never triggers it
        if self.ally(t.owner, src.owner):
            if not self.flee_allied(t, src):
                return
        elif not (self.flee_decide(t, src) or self.flee_proxy(t, src)):
            return
        dx, dy = t.x - src.x, t.y - src.y
        d = math.hypot(dx, dy)
        if d < 2e-7:                                  # CAbilityMove_p2 §6.1 step 3: the unit's facing vector
            a = math.radians(t.facing)
            dx, dy, d = math.cos(a), math.sin(a), 1.0
        # [obs runtime 1.26a] docs/oracle.md flee: a worker of a player-slot owner (user or computer, slot state irrelevant) runs
        # exactly 300 with a plain move order (851986), independent of the attacker's range (no range + 100); a worker of a
        # creep / neutral owner (slot >= 12) runs ~1200 and reports order 852018 (0xd0012). The wounded-hero 1200 rule is gone
        # (heroes never flee). A non-worker (the AI-requested decision, an allied hero's hit) keeps the old attacker-range rule.
        creep = not self.flee_player_controlled(t)
        worker = AHAR in t.abilities and not t.hero
        if worker:
            dist = FLEE_CREEP_DIST if creep else FLEE_DIST
        else:
            # CAbilityMove_p2 §6.1 step 4: the attacker's range *against the unit* (0x6f557210 resolves the weapon by the
            # target's type), not the attacker's current weapon slot (src.atk_w may be a weapon that cannot hit t).
            wi = self.pick_weapon(src, t) if src.fighter else -1
            dist = max(FLEE_DIST, (self.rng_of(src, wi) if wi >= 0 else 0.0) + 100.0) if src.fighter else FLEE_DIST
        self.issue_order(t, O_MOVE, r32(t.x + dx / d * dist), r32(t.y + dy / d * dist))
        if worker and creep and t.order == O_MOVE:
            t.flee_oid = FLEE_CREEP_OID

    # ================================================================ movement (NOTES_units §2)
    def goal_of(self, u):
        if u.chasing and (u.gpt is not None or u.atk_target is not None):
            ct = u.gpt if u.gpt is not None else u.atk_target
            return ct.x, ct.y
        if u.wg_via is not None:
            return u.wg_via.x, u.wg_via.y         # A-AB22: first to the waygate
        if u.iop is not None and self.itm.follow_give(u):
            return u.tx, u.ty                     # HERO-UI: a pending give walks to the target's live position
        if u.target is not None:
            if not u.target.alive:
                u.target = None                   # NOTES_units §1: continues to the stored point
            else:
                u.tx, u.ty = u.target.x, u.target.y
        if u.fo is not None and u.target is None:
            return u.tx + u.fo[0], u.ty + u.fo[1]       # STEER-3: the unit's slot in its cluster's formation
        return u.tx, u.ty

    # ---- unit collision (A-COL1, maintainer 2026-10-24: units stacked on one spot; the real game keeps every creep of a
    # Wintermaul wave in its own 32-cell, nearest neighbour ~33 [obs runtime wm3])
    def collides(self, u):
        # a collision size of 0 still keeps units apart: Wintermaul's shades (ushd, collision 0) stand one per cell
        return u.alive and not u.dead and u.pathing and (not u.is_bldg or u.root_st == 2) and not u.locust and not self.flying(u)   # an uprooted Ancient (CAbilityRoot) is a ground unit

    def occ_key(self, u):
        g = self.grid
        if g is None or not self.collides(u):
            return None
        cx, cy = g.cell_of(u.x, u.y)
        return cy * g.w + cx

    # ---- PB-3 / PB-4 [obs oracle ssp1 / ssp2, ssp_oC11 / ssp_edot / ssp_Edem]: units created one after another at
    # one point
    SPREAD_R = 12                               # cells searched around the creation cell (a 25 x 25 square) [inf]

    def spread_box_ok(self, g, x, y, c):
        """the collision box [x-c, x+c] x [y-c, y+c] lies on walkable cells only (a collision 0 unit: its own cell)"""
        cx0, cy0 = g.cell_of(x, y)
        if c <= 0:
            return g.walkable(cx0, cy0)
        bx0 = int(math.floor((x - c - g.ox) / 32.0))
        bx1 = int(math.ceil((x + c - g.ox) / 32.0)) - 1
        by0 = int(math.floor((y - c - g.oy) / 32.0))
        by1 = int(math.ceil((y + c - g.oy) / 32.0)) - 1
        for yy in range(by0, by1 + 1):
            for xx in range(bx0, bx1 + 1):
                if not g.walkable(xx, yy):
                    return False
        return True

    def spread_spawn(self, u):
        """a ground unit created where it does not fit stands on the first fitting position of a square spiral of
        32-cell centres around its cell: the point itself first, then the cells south, east, north, west with arm
        lengths 1, 1, 2, 2, 3, 3, ... A position fits when the unit's collision box lies on walkable cells only and no
        other colliding unit stands closer than ceil((2 collision + 1) / 32) cells (Chebyshev, in cells) [obs oracle
        ssp1: 20 ushd (collision 0) at a corner, one per cell; ssp_oC11 / ssp_edot / ssp_Edem: collision 5 / 16 / 32 at
        a point inside a wall, spacing 32 / 64 / 96 — all 80 offsets exact; Chebyshev vs Euclid not told apart [inf]].
        Flyers (hfoo in ssp2 stayed stacked), buildings and units under construction are not spread. Occupancy is read
        live (units created in this very step count), not from occ."""
        g = self.grid
        if g is None or not self.collides(u):
            return
        c = float(u.coll)
        sp = int(math.ceil((2.0 * c + 1.0) / 32.0))
        cx, cy = g.cell_of(u.x, u.y)
        r = self.SPREAD_R
        taken = set()
        for o in self.query(u.x, u.y, (r + sp) * 32.0):
            if o is not u and self.collides(o):
                k = self.occ_key(o)
                if k is not None:
                    taken.add(k)

        def free(x, y):
            for yy in range(y - sp + 1, y + sp):
                for xx in range(x - sp + 1, x + sp):
                    if g.inside(xx, yy) and yy * g.w + xx in taken:
                        return False
            return True
        if self.spread_box_ok(g, u.x, u.y, c) and free(cx, cy):
            return
        x, y = cx, cy
        dx, dy = 0, -1
        arm = 1
        while arm <= 2 * r + 1:
            for _rep in range(2):
                for _i in range(arm):
                    x += dx
                    y += dy
                    if abs(x - cx) <= r and abs(y - cy) <= r and g.inside(x, y) and free(x, y):
                        nx = r32(g.ox + (x + 0.5) * 32.0)
                        ny = r32(g.oy + (y + 0.5) * 32.0)
                        if self.spread_box_ok(g, nx, ny, c):
                            u.x = nx
                            u.y = ny
                            self.relocate(u)
                            return
                dx, dy = -dy, dx                  # south -> east -> north -> west
            arm += 1

    def stands(self, u):
        """PATH-AB step 4: a ground unit that does not walk (the original's search B and sight see only these [obs steer 7.1.4]): not moving, or moving only in name - in range of its target.
        STEER-2: a unit STANDS when it has no velocity - it did not step at its last movement tick (waiting behind a blocker, turning in place, arrived, in range, no route yet)"""
        return u not in self.N.moving or not u.vmv

    def occ_rebuild(self):
        self.occ = {}
        for u in self.N.units:
            self.wk += 1
            k = self.occ_key(u)
            if k is not None:
                self.occ[k] = self.occ.get(k, 0) + 1

    def occs_rebuild(self):
        """PATH-AB step 4: the standing units' cells, counted anew every OCCS_EVERY steps (a unit that starts to walk is no obstacle for long)"""
        occs = {}
        for u in self.N.units:
            self.wk += 1
            k = self.occ_key(u)
            if k is not None and self.stands(u):
                occs[k] = occs.get(k, 0) + 1
                u.occk = k
            else:
                u.occk = None
        self.occs = occs
        self.grid.ab_su = occs

    def occs_note(self, u, stand):
        """STEER-2: keep engine.occs (the standing units' cells) exact for the unit that just stepped: counted under its cell when it stands, not counted when it walks"""
        k = self.occ_key(u) if stand else None
        if k != u.occk:
            occs = self.occs
            if u.occk is not None:
                occs[u.occk] = occs.get(u.occk, 0) - 1
            if k is not None:
                occs[k] = occs.get(k, 0) + 1
            u.occk = k

    def ab_block(self, u, k1):
        """STEER-2 [obs 0x6f680010 / 0x6f67fec0]: the ground units in fine cell k1 other than the mover and its order target -> (ticks to wait, standing blocker). A MOVING blocker (it has a velocity) sets a wait of 4 ticks when the
        mover's speed is not above its speed or it belongs to another player, else 20; a standing one sets none (the blocked handler decides)."""
        g = self.grid
        px, py = g.center(k1 % g.w, k1 // g.w)
        wt = 0
        st = False
        sp = -1.0
        for o in self.query(px, py, 24.0):
            self.wk += 1
            if o is u or o is u.target or o is u.atk_target or not self.collides(o) or self.occ_key(o) != k1:
                continue
            if AB_PASS_ON and u.fo is not None and o.fo is not None and o in self.N.moving and o.hx * u.hx + o.hy * u.hy < AB_PASS_DOT:
                continue                  # STEER-4 [inf, probe G1: 24 of 24 real footmen of two groups pass in a corridor]: a group-order mover (formation slot) meets another one headed the opposite way: they pass through each other (no wait, no blocked handler); single movers (workers at a mine) queue
            if o in self.N.moving and o.vmv:
                if sp < 0.0:
                    sp = self.eff_speed(u) if u.vmv else 0.0
                w = 4 if (sp <= self.eff_speed(o) or o.owner is not u.owner) else 20
                if w > wt:
                    wt = w
            else:
                st = True
        return wt, st

    def ab_in_block(self, u, x0, y0, k):
        """STEER-4: is fine cell k inside the footprint block of u's class at the position (x0, y0) (node = cell of position + 16 cls, block {x - cls .. x} x {y - cls .. y})"""
        g = self.grid
        cls = self.ab_cls(u)
        o = 16.0 * cls
        n = g.cell_of(x0 + o, y0 + o)
        cx = k % g.w
        cy = k // g.w
        return n[0] - cls <= cx <= n[0] and n[1] - cls <= cy <= n[1]

    def move_unit(self, u, dt):
        """one step of movement; -> True when the order point was reached."""
        if (u.atk_target is not None or u.gpt is not None) and not u.chasing:
            return False                          # standing to attack
        if self.now() < u.bs_end or u.stunned or self.now() < u.wg_hold:
            return False                          # (wg_hold: a gate crossing waits for a free landing spot)
        if u.target is not None and u.order in (O_SMART, O_MOVE) and u.chasing is False:
            fr = self.follow_stop(u)
            if fr is not None:
                return fr
        gx, gy = self.goal_of(u)
        sp = self.eff_speed(u)
        if sp <= 0:
            return False
        d = sp * dt
        g = self.grid
        if g is None or not u.pathing or self.flying(u):
            return self.straight(u, gx, gy, d, u.target is None and not u.chasing)
        if self.pab:
            return self.move_ab(u, g, gx, gy, d)
        now = self.now()
        follow = u.chasing or u.target is not None or (u.iop is not None and u.iop[0] == 3 and u.iop[3] is not None)
        if u.path is not None and u.path_ver != g.version and self.path_clear(u):
            u.path_ver = g.version                # A27b: the grid changed but the rest of the path is still free
        hard = u.path is None or u.path_ver != g.version
        if u.preq or hard or (follow and now >= u.repath_at and g.cell_of(gx, gy) != u.path_goal):
            if not u.preq:
                u.preq = True
                self.pb_queue(u.owner.id).append(u)
            if hard:
                return False                      # A-PB1: no path yet -> the unit stands in the player's queue (served
                                                  # at the start of the next step, before movement)
        if u.det is not None and not g.walkable(*g.cell_of(*u.det)):
            u.det = None                          # A-PB2: a structure was placed on the detour cell
        if u.det is not None:
            if self.seg_step(u, u.det[0], u.det[1], d, False):
                u.det = None                      # A-PB2: the detour cell reached, the path goes on
                if u.path_i < len(u.path) and u.path[u.path_i][0] != MARK_X and not g.los(u.x, u.y, *u.path[u.path_i]):
                    u.path = None                 # ... unless its next waypoint is out of sight from here: a new path
            return False
        while u.path_i < len(u.path):
            wx, wy = u.path[u.path_i]
            if wx == MARK_X:                      # WAYGATE-PORT: the gate cell is reached, the marker is next [obs 0x6f67fcc0]
                r = self.wg_cross(u, int(wy))
                if r == 0:
                    return False
                if u.path_i >= len(u.path):
                    if u.path_partial:
                        u.path = None
                        return False
                    return not follow
                if r == 1:
                    return False                  # carried: the walk goes on from the landing at the next update
                wx, wy = u.path[u.path_i]
                if not g.los(u.x, u.y, wx, wy):
                    u.path = None                 # the gate is off: a straight leg from the gate cell would cross a wall: search anew
                    return False
            last = u.path_i == len(u.path) - 1
            if last and follow:
                wx, wy = gx, gy
            arrive = self.seg_step(u, wx, wy, d, last and follow)
            if arrive:
                u.path_i += 1
                if u.path_i >= len(u.path):
                    if u.path_partial:            # node limit hit: search on from here
                        u.path = None
                        return False
                    return not follow
            return False
        return not follow

    # ---- PATH-AB step 4 (NOTES_pathing 2.6 / 2.7 / 2.15): the follower of the raw route, the retry counter
    def ab_cls(self, u):
        """STEER-4 [obs steer 8.2 0x6f1b99c0 / 0x6f08cde0 / 0x6f08a2e0, probe G2 / G2b]: the unit's footprint class of search B and the sight = the size class of its radius (max(collision, 1) world units / 32 cells: < 0.5, then <= 1.0, <= 1.5, else = 0 .. 3, footprint 1x1 .. 4x4 cells;
        [inf] the upper bounds are inclusive: a collision of exactly 32 / 48 (Archer, hero sizes) stays in the smaller class - with the notes' strict < the night elf armies cannot leave their base on BootyBay (3x3 blocks); a worker, collision 16, is class 1); the real class-1 footmen do not pass a gap of one cell"""
        if not AB_CLS_ON:
            return 0
        c = u.coll
        return 0 if c < 16.0 else (1 if c <= 32.0 else (2 if c <= 48.0 else 3))

    def ab_ctx(self, u, gx, gy):
        """the cells search B and the sight do not treat as occupied for this mover: its own and its goal's"""
        g = self.grid
        g.ab_su = self.occs
        c = g.cell_of(u.x, u.y)
        g.ab_x1 = c[1] * g.w + c[0]
        c = g.cell_of(gx, gy)
        g.ab_x2 = c[1] * g.w + c[0]

    def ab_aim(self, u, k, gx, gy):
        """the aim after an arrival [obs 2.6 0x6f67f8a0]: scanning from route point k toward the goal, the first point whose segment from the unit fails the line-of-sight
        test (1-cell steps, footprint-sized); the last point when all pass. A gate cell ends the scan (a marker follows it: no smoothing across a crossing). After a blocked
        step (ab_safe) the next raw point."""
        pts = u.path
        n = len(pts)
        if u.ab_safe:
            u.ab_safe = False
            return k
        g = self.grid
        gx, gy = self.goal_of(u)
        self.ab_ctx(u, gx, gy)
        x, y = u.x, u.y
        j = k
        while j < n - 1:
            px, py = pts[j]
            self.wk += 1
            if px == MARK_X:
                return j
            if not g.ab_los(x, y, px, py, self.ab_cls(u)):
                return j
            if pts[j + 1][0] == MARK_X:
                return j
            j += 1
        return n - 1

    def ab_box_inc(self, u):
        u.bx += 1
        return True

    def ab_boxed(self, u):
        """STEER-2 [inf]: the unit's start is boxed in by standing units - none of the eight neighbour cells is free and at least one is held by a unit (a creep in the middle of a spawn heap, the outer ones are served later):
        the empty search is no futile cycle of the retry counter (at most AB_BOX_MAX in a row), the unit asks again at the next tick"""
        g = self.grid
        cx, cy = g.cell_of(u.x, u.y)
        nu = 0
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                self.wk += 1
                if g.walkable(cx + dx, cy + dy):
                    if self.occs.get((cy + dy) * g.w + cx + dx, 0) > 0:
                        nu += 1
                    else:
                        return False
        return nu > 0

    def ab_blocked(self, u, gx, gy):
        """the blocked handler [obs 2.7 0x6f680280 / 0x6f67f140]: retry counter 0 -> 2 (goal within 12 A0 cells) or 7 + parity; counter 1 -> the path ends (True: the
        order ends); else the fine route is dropped (the next tick searches again, the A route stays), counter - 1."""
        if u.retry == 0:
            dx, dy = gx - u.x, gy - u.y
            u.retry = AB_RETRY_NEAR if dx * dx + dy * dy <= AB_RETRY_D2 else AB_RETRY_FAR + (u.serial & 1)
        if u.retry == 1:
            u.retry = 0
            return True
        u.retry -= 1
        u.path = None
        u.rt_cont = True
        u.seg = None
        return False

    def ab_ended(self, u):
        """the retry counter ran out: a chase is given up like an unreachable target (A37); any other order ends (True = done)"""
        ct = u.gpt if u.gpt is not None else u.atk_target
        if u.chasing and ct is not None:
            self.stats['repaths'] += 1
            self.log('give up chase u%d -> u%d' % (u.serial, ct.serial))
            self.drop_target(u)
            u.next_scan = self.now() + 2 * SPS
            u.path = None
            if not u.attack_move:
                self.N.moving.discard(u)
            return False
        u.path = None
        self.ab_gu = True
        return True

    def ab_step(self, u, wx, wy, d, moving_goal, last):
        """one step toward the route point; a step into a blocked cell slides along the free axis (the original's movement physics, not decompiled), else the unit stays and
        aims at the previous raw point. -> (arrived, blocked)"""
        g = self.grid
        x0, y0 = u.x, u.y
        if self.seg_step(u, wx, wy, d, moving_goal):
            return True, False
        c0 = g.cell_of(x0, y0)
        c1 = g.cell_of(u.x, u.y)
        if c1 == c0 or g.walkable(c1[0], c1[1]):
            return False, False
        nx, ny = u.x, u.y
        u.seg = None
        self.wk += 2
        for ax in ((0, 1) if abs(nx - x0) >= abs(ny - y0) else (1, 0)):
            tx, ty = (nx, y0) if ax == 0 else (x0, ny)
            ct = g.cell_of(tx, ty)
            if (tx != x0 or ty != y0) and (ct == c0 or g.walkable(ct[0], ct[1])):
                u.x, u.y = tx, ty
                return False, False
        u.x, u.y = x0, y0
        return False, True

    def move_ab(self, u, g, gx, gy, d):
        """move_unit for a ground mover with the switch PATH_AB on: the follower of the raw route (smoothing at follow time, 0.49 cell arrival), the retry counter."""
        now = self.now()
        if u.ab_end:
            u.ab_end = False
            return self.ab_ended(u)
        follow = u.chasing or u.target is not None or (u.iop is not None and u.iop[0] == 3 and u.iop[3] is not None)
        hard = u.path is None
        if u.path is not None and u.path_ver != g.version:
            u.path_ver = g.version                # a footprint change notifies nobody (2.0 rule 13): the unit notices at its next line-of-sight test
            i = u.path_i
            self.ab_ctx(u, gx, gy)
            if i < len(u.path) and u.path[i][0] != MARK_X and not g.ab_los(u.x, u.y, u.path[i][0], u.path[i][1], self.ab_cls(u)):
                if self.ab_blocked(u, gx, gy):
                    return self.ab_ended(u)
                return False
        if u.preq or hard or (follow and now >= u.repath_at and g.cell_of(gx, gy) != u.path_goal):
            if not u.preq:
                u.preq = True
                self.pb_queue(u.owner.id).append(u)
            if hard:
                return False                      # no route yet -> the unit stands in the player's request list
        if u.det is not None and not g.walkable(*g.cell_of(*u.det)):
            u.det = None                          # A-PB2: a structure was placed on the detour cell
        if u.det is not None:
            if self.seg_step(u, u.det[0], u.det[1], d, False):
                u.det = None                      # A-PB2: the detour cell reached, the route goes on
            return False
        while True:
            i = u.path_i
            n = len(u.path)
            if i >= n:
                if u.path_partial:                # the leg is walked: the next one (A route in the mover) or a new search
                    u.path = None
                    u.rt_cont = True
                    return False
                return not follow
            wx, wy = u.path[i]
            if wx == MARK_X:                      # the gate cell is reached, the marker is next [obs 0x6f67fcc0]
                r = self.wg_cross(u, int(wy))
                if r == 0:
                    return False
                if r == 2:
                    u.path = None                 # the gate is off: the A route that crossed it is void (the unit stands at the gate cell, not at the landing): a new search A
                    return False
                if u.path_i >= n:
                    if u.path_partial:
                        u.path = None
                        u.rt_cont = True
                        return False
                    return not follow
                if r == 1:
                    return False                  # carried: the walk goes on from the landing at the next update
                wx, wy = u.path[u.path_i]
                if not g.los(u.x, u.y, wx, wy):
                    u.path = None                 # the gate is off: a straight leg from the gate cell would cross a wall: search anew
                    return False
                i = u.path_i
            last = i == n - 1
            if last and follow:
                wx, wy = gx, gy
            if not last:
                ddx = wx - u.x
                ddy = wy - u.y
                if ddx * ddx + ddy * ddy <= AB_ARR2:
                    u.path_i = self.ab_aim(u, i + 1, gx, gy)         # arrived within 0.49 cells (no step): aim at the first point not in sight
                    u.seg = None
                    continue
            arrive, blk = self.ab_step(u, wx, wy, d, last and follow, last)
            if blk:
                if u.path_i > 0:
                    u.path_i -= 1
                    u.ab_safe = True              # back to the previous raw point, then on point by point
                    u.seg = None
                elif self.ab_blocked(u, gx, gy):
                    return self.ab_ended(u)
                return False
            if arrive:
                if last:
                    u.path_i = i + 1
                    if u.path_partial:
                        u.path = None
                        u.rt_cont = True
                        return False
                    return not follow
                u.path_i = self.ab_aim(u, i + 1, gx, gy)
            return False

    # ---- waygates (NOTES_abilities §5.54, NOTES_pathing 1; WAYGATE-PORT): the gate is an edge of the ground A* (pathing.Grid), the
    # follower carries a unit that reaches the gate cell of its route
    def wg_is_gate(self, u):
        return any(b == 'Awrp' for _c, b, _l in self.ab.abil_list(u))

    def wg_row(self, u):
        for _c, b, lv in self.ab.abil_list(u):
            if b == 'Awrp':
                return lv or {}
        return None

    def wg_state(self, u):
        if u.wg is None:
            u.wg = [False, 0, 0]                   # the pathing table entry starts inactive, cell (0, 0) [obs 0x6f080800]
        return u.wg

    def wg_register(self, u, lv):
        """ability add (type list, UnitAddAbility): the first free slot, the area written once at the position now [obs 0x6f458d20,
        0x6f080b10]; the area is the cells centred within DataA / 2, DataB / 2 [obs runtime 1.26a, pathing.Grid.gate_rect]."""
        g = self.grid
        if g is None or u.wg_slot:
            return
        a = float((lv or {}).get('A', 0.0) or 0.0) * 0.5
        b = float((lv or {}).get('B', 0.0) or 0.0) * 0.5
        u.wg_ab = (a, b)
        u.wg_slot = g.gate_add(g.gate_rect(u.x, u.y, a, b))
        if u.wg_slot and u.wg is not None:
            self.wg_sync(u)

    def wg_unregister(self, u):
        """ability / unit removal (a dead gate too): slot 0 over the rectangle of its position NOW, the slot is freed [obs 0x6f458f70]."""
        g = self.grid
        if g is None or not u.wg_slot:
            return
        g.gate_remove(u.wg_slot, g.gate_rect(u.x, u.y, u.wg_ab[0], u.wg_ab[1]))
        u.wg_slot = 0

    def wg_sync(self, u):
        g = self.grid
        if g is not None and u.wg_slot:
            st = u.wg
            g.gate_set(u.wg_slot, st[0], st[1], st[2])

    def wg_set_active(self, u, on):
        st = self.wg_state(u)
        st[0] = on
        self.wg_sync(u)

    def wg_origin(self):
        g = self.grid
        return (g.ox, g.oy) if g is not None else (0.0, 0.0)

    def wg_set_dest(self, u, x, y):
        """the destination is kept as a pathing cell [obs 0x6f0809b0 -> 0x6f6897f0: floor((x - min) / 32), int]."""
        st = self.wg_state(u)
        ox, oy = self.wg_origin()
        st[1] = int(math.floor((x - ox) / 32.0))
        st[2] = int(math.floor((y - oy) / 32.0))
        self.wg_sync(u)

    def wg_dest(self, u):
        """WaygateGetDestination and the landing point: the cell's lower corner, cell x 32 + min [obs runtime 1.26a: every value read
        back is a multiple of 32 from the map minimum, NOT the cell centre of the static reading]."""
        st = self.wg_state(u)
        ox, oy = self.wg_origin()
        return r32(ox + st[1] * 32.0), r32(oy + st[2] * 32.0)

    def wg_open(self, w):
        return w.alive and not w.dead and w.wg is not None and w.wg[0] and w.wg_slot != 0

    def wg_user(self, u):
        """ground units that path (the gate is an edge of the ground pathfinder [obs 0x6f67fcc0]; air: inf)."""
        return u.pathing and not self.flying(u)

    WG_RING = 6                                    # free-spot search: cells around the landing point [inf: args 0x20 / 0x18 / 6 of 0x6f67f030]

    def wg_landing(self, u, slot):
        """the free spot for a unit carried through a gate: the landing point itself when its cell is not taken by another colliding
        unit, else the nearest free walkable cell centre within WG_RING cells; None = no free spot (the follower waits)."""
        g = self.grid
        px, py = g.gate_point(slot)
        cx, cy = g.cell_of(px, py)
        mine = self.occ_key(u)
        coll = self.collides(u)
        for r in range(self.WG_RING + 1):
            best = None
            for dy in range(-r, r + 1):
                for dx in range(-r, r + 1):
                    if max(abs(dx), abs(dy)) != r:
                        continue
                    self.wk += 1
                    x, y = cx + dx, cy + dy
                    if not g.walkable(x, y):
                        continue
                    k = y * g.w + x
                    if coll and k != mine and self.occ.get(k, 0) > 0:
                        continue
                    fx, fy = (px, py) if r == 0 else g.center(x, y)
                    d2 = (fx - px) * (fx - px) + (fy - py) * (fy - py)
                    if best is None or d2 < best[0]:
                        best = (d2, fx, fy)
            if best is not None:
                return r32(best[1]), r32(best[2])
        return None

    def wg_cross(self, u, slot):
        """the follower reached the gate cell and the next entry is the marker [obs 0x6f67fcc0]: an active gate carries the unit to its
        landing (free-spot search; none free: wait GATE_WAIT mover updates, same marker again); an inactive one is just walked past.
        Marker and landing entry are consumed either way. -> 0 held, 1 carried, 2 passed."""
        g = self.grid
        if g.gate_open(slot):
            spot = self.wg_landing(u, slot)
            if spot is None:
                u.wg_hold = self.now() + GATE_WAIT * self.mv
                return 0
            u.x, u.y = spot
            u.seg = None
            u.det = None
            self.log('waygate u%d' % u.serial)
            u.path_i += 2
            return 1
        u.path_i += 2
        return 2

    def wg_post(self, u, done):
        """smart (right-click) on an active gate [obs runtime 1.26a S8 / S8b]: the unit walks to the gate itself (also when the
        destination is nearer, S8b), is carried to the destination when it is there (or its walk ended at the gate's footprint),
        and the order ends at the destination."""
        w = u.wg_via
        if not self.wg_open(w):
            u.wg_via = None                        # gone / switched off on the way: on to the stored point (the destination)
            u.path = None
            return False
        reach = float(w.coll) + float(u.coll) + 32.0
        dx, dy = w.x - u.x, w.y - u.y
        if not done and dx * dx + dy * dy > reach * reach:
            return done
        spot = self.wg_landing(u, w.wg_slot)
        if spot is None:
            u.wg_hold = self.now() + GATE_WAIT * self.mv
            return False
        u.wg_via = None
        u.x, u.y = spot
        u.path = None
        u.seg = None
        self.log('waygate u%d' % u.serial)
        return True

    def path_clear(self, u):
        """A27b: every remaining segment of u's path (from its position) still has line of sight."""
        g = self.grid
        x, y = u.x, u.y
        jump = False
        for i in range(u.path_i, len(u.path)):
            wx, wy = u.path[i]
            self.wk += 1
            if wx == MARK_X:
                if not g.gate_open(int(wy)):
                    return False                  # the gate was switched off / removed: search anew
                jump = True                       # WAYGATE-PORT: no line of sight across a gate crossing
                continue
            if not jump and not g.los(x, y, wx, wy):
                return False
            jump = False
            x, y = wx, wy
        return True

    def pb_queue(self, pid):
        q = self.pbq.get(pid)
        if q is None:
            q = self.pbq[pid] = [[], PB_STEP, self.now(), [0, 0, 0, 0], [0, 0, 0, 0], self.now() // PATH_TICK]
        return q[0]

    def pb_serve(self, pid):
        """A-PB1 [obs oracle wm_pathbudget / wm_owners, model inf]: every player has a path budget of PB_STEP per step
        (not saved up beyond one step); its requests are served first come first served while the balance is positive,
        each costs PB_REQ + its A* expansions (the balance may go negative)."""
        if self.pab:
            self.pb_serve_ab(pid)
            return
        e = self.pbq[pid]
        q = e[0]
        now = self.now()
        if now > e[2]:
            e[1] = min(PB_STEP, e[1] + PB_STEP * (now - e[2]))
            e[2] = now
        g = self.grid
        while q and e[1] > 0:
            v = q.pop(0)
            self.wk += 1
            pr, cr = v.preq, v.creq
            v.preq = v.creq = False
            if not v.alive or v.dead or v not in self.N.moving:
                continue
            if pr and g is not None and v.pathing and not self.flying(v):
                gx, gy = self.goal_of(v)
                self.repath(v, gx, gy)
                v.pexp = g.last_exp
                e[1] -= PB_REQ + min(g.last_exp, PATH_PIECE) * PB_EXP
            elif cr:
                v.side = True                     # A-PB2: a blocked unit's repath around the unit ahead
                e[1] -= PB_REQ + PB_COLL + min(v.pexp, PATH_PIECE) * PB_EXP

    # ---- PATH-AB step 3 (NOTES_pathing 2.5): the real request throttle and quota records
    def pb_records(self, e):
        """the player's four records advance one tick at a time to the current tick: countdown 0 -> used := 0, countdown := period, else countdown - 1"""
        tk = self.now() // PATH_TICK
        n = tk - e[5]
        if n <= 0:
            return
        e[5] = tk
        if n > PB_SYNC_MAX:
            n = PB_SYNC_MAX
        used, cd = e[3], e[4]
        for _ in range(n):
            for r in range(4):
                if cd[r] == 0:
                    used[r] = 0
                    cd[r] = PB_REC[r][1]
                else:
                    cd[r] -= 1

    def pb_gate(self, kind):
        """the gate of search kind 0 = A (record R2), 1 = B (record R3) for the mover being served (pb_cur): its throttle stamp and the player's record
        [obs 0x6f67e700]: refused when the mover searched this kind within 10 ticks or when `used` is above the limit (the request is asked again at the next tick).
        The FIFO of the original is the order of the player's request list (served first come first served, a refused mover keeps its place)."""
        u = self.pb_cur
        self.wk += 1
        if kind == 0:
            if self.now() - u.pt_a < PB_THROTTLE:
                return False
            r = 0
        else:
            if self.now() - u.pt_b < PB_THROTTLE:
                return False
            r = 3
        return self.pb_cure[3][r] <= PB_REC[r][0]

    def pb_charge(self, kind, pops):
        """after a search: its pops are used up in the record; the throttle stamp is set unless the search was cheap (A < 32, B < 64 pops)"""
        u = self.pb_cur
        now = self.now()
        if kind == 0:
            self.pb_cure[3][0] += pops
            u.pt_a = PB_NEVER if pops < PB_CHEAP_A else now
        else:
            self.pb_cure[3][3] += pops
            u.pt_b = PB_NEVER if pops < PB_CHEAP_B else now

    def pb_serve_ab(self, pid):
        """one pathing tick for the player's request list (a refused path waits for the whole tick); a served request leaves the list, a refused one keeps its place"""
        if self.now() % PATH_TICK:
            return
        e = self.pbq[pid]
        self.pb_records(e)
        self.pb_cure = e
        q = e[0]
        g = self.grid
        keep = []
        for v in q:
            self.wk += 1
            if not v.alive or v.dead or v not in self.N.moving:
                v.preq = v.creq = False
                continue
            if v.preq and g is not None and v.pathing and not self.flying(v):
                gx, gy = self.goal_of(v)
                self.pb_cur = v
                if not self.repath(v, gx, gy):
                    keep.append(v)                # refused (throttle or quota): asks again at the next tick
                    continue
                v.preq = v.creq = False
                v.pexp = g.last_exp
            elif v.creq and not v.preq:
                self.pb_cur = v
                if self.now() - v.creq_at < PB_BLOCK_WAIT or not self.pb_gate(1):
                    keep.append(v)                # the blocked mover's re-search is a search B: the same throttle and record
                    continue
                v.preq = v.creq = False
                v.side = True                     # A-PB2: a blocked unit's repath around the unit ahead
                self.pb_charge(1, PB_COLL_AB)
            else:
                v.preq = v.creq = False
        e[0][:] = keep

    def pb_sidestep(self, u, k0):
        """A-PB2 [inf]: the unit walks first to the free walkable neighbour cell closest to its next waypoint — at most
        one cell farther from it than the unit's own cell, no diagonal past an occupied / blocked side cell; a cell on
        the walker's left counts half a cell farther (head-on walkers part to their right and pass, oracle meet2)."""
        u.side = False
        g = self.grid
        if u.path is None or u.path_i >= len(u.path):
            return
        from .pathing import NB, CELL
        wx, wy = u.path[u.path_i]
        if wx == MARK_X:
            return
        cx, cy = k0 % g.w, k0 // g.w
        px, py = g.center(cx, cy)
        best = (math.sqrt((px - wx) * (px - wx) + (py - wy) * (py - wy)) + CELL, None, None)
        for dx, dy in NB:
            self.wk += 1
            nx, ny = cx + dx, cy + dy
            if not g.walkable(nx, ny) or self.occ.get(ny * g.w + nx, 0) > 0:
                continue
            if dx and dy and (not g.walkable(nx, cy) or not g.walkable(cx, ny) or self.occ.get(cy * g.w + nx, 0) > 0
                              or self.occ.get(ny * g.w + cx, 0) > 0):
                continue
            px, py = g.center(nx, ny)
            dd = math.sqrt((px - wx) * (px - wx) + (py - wy) * (py - wy))
            if (wx - u.x) * dy - (wy - u.y) * dx > 0:
                dd += CELL / 2                    # left of the walking direction
            if dd < best[0]:
                best = (dd, px, py)
        if best[1] is None:
            return
        u.det = (best[1], best[2])
        u.seg = None

    def repath(self, u, gx, gy):
        g = self.grid
        from .pathing import CHASE_EXPAND, MAX_EXPAND, TAIL_ON
        ct = u.gpt if u.gpt is not None else u.atk_target
        chase = u.chasing and ct is not None
        pool = not chase and getattr(u.owner, 'controller', 0) != 0          # PB-SHARE scope: waves of computer / neutral owners, never a user slot (human or bot)
        if self.pab:
            pool = False                          # PATH-AB: no route sharing between units (NOTES_pathing 2.0 rule 13): every request is a search under the throttle / quota
        if not chase and not u.usr and getattr(g, "au_obs", False):
            sc = g.cell_of(u.x, u.y)                  # PERF-AUTOFIELD: a wave on a lane switches the flow fields on (pathfield.fld_auto_note)
            gc = g.cell_of(gx, gy)
            g.fld_auto_note(self.now(), u.owner.id, u.serial, sc[0], sc[1], gc[0], gc[1])
        sh = g.share_find(u.x, u.y, gx, gy) if pool else None
        if sh is not None:
            u.path, g.last_exp = sh                   # PB-SHARE: the route of a search served nearby (costs like a cache hit)
            g.last_limited = False
            g.last_reached = True
            g.last_refused = False
            self.stats['shares'] += 1
        else:
            sx0, sy0 = u.x, u.y
            g.fast_ok = TAIL_ON and getattr(u.owner, 'controller', 0) == 0       # PERF-MELEE: route tails (off, pathing.TAIL_ON) would serve the units of user slots; waves share (PB-SHARE)
            g.fld_ctx = (self.now(), u.owner.id) if getattr(g, "fld_on", False) and not chase else None      # PATH-FIELD: this search may read its route off a flow field
            if self.pab:
                g.ab_cb = self                    # PATH-AB step 3: the gate and the charge of the throttle / quota records
                self.ab_ctx(u, gx, gy)
                rt = u.rt if u.rt_cont and u.rt is not None else AbRoute()     # step 4: the route in the mover goes on only when the engine itself dropped the leg
                if g.cell_of(gx, gy) != u.path_goal:
                    u.retry = 0                   # a new goal: the retry counter starts anew
                pts = g.find_path(u.x, u.y, gx, gy, CHASE_EXPAND if chase else MAX_EXPAND, None, self.ab_cls(u), rt)
                g.ab_cb = None
                g.fld_ctx = None
                if g.last_refused:
                    g.fast_ok = False
                    return False                  # the request waits for the next tick; nothing else changed
                u.rt = rt
                u.rt_cont = False
                u.path = pts
            else:
                u.path = g.find_path(u.x, u.y, gx, gy, CHASE_EXPAND if chase else MAX_EXPAND)
                g.fld_ctx = None
            g.fast_ok = False
            g.st_search += 1
            g.st_pops += g.last_exp
        if chase and not g.last_reached and not (self.pab and g.last_limited and u.path):      # PATH-AB S1: a budget-limited A route is not "unreachable": walk it and ask again
            self.stats['repaths'] += 1
            self.log('give up chase u%d -> u%d' % (u.serial, ct.serial))
            self.drop_target(u)                   # A37: unreachable chase target -> give up, no scan for 2 s
            u.next_scan = self.now() + 2 * SPS
            u.path = None
            if not u.attack_move:
                self.N.moving.discard(u)
            return True
        u.det = None
        if self.pab:
            u.path_partial = g.last_limited       # the leg ends before the goal (more legs, budget): asked again at its end
            u.path_i = 0
            u.path_ver = g.version
            u.path_goal = g.cell_of(gx, gy)
            u.repath_at = self.now() + SPS // 2
            self.stats['repaths'] += 1
            if not u.path and g.last_limited and not (u.bx < AB_BOX_MAX and self.ab_boxed(u) and self.ab_box_inc(u)):
                if self.ab_blocked(u, gx, gy):    # nowhere to go (the search ends at the start): a futile cycle of the retry counter
                    u.ab_end = True
                    u.path = []
            return True
        u.path_partial = g.last_limited and u.path and g.cell_of(*u.path[-1]) != g.cell_of(u.x, u.y)
        if sh is None and pool and u.path and not u.path_partial and g.last_reached:
            g.share_store(sx0, sy0, gx, gy, u.path, g.last_exp)      # PB-SHARE: before the piece is cut off
        if not chase and u.path and not u.path_partial and g.last_exp > PATH_PIECE:
            self.cut_piece(u, g.last_exp)
        u.path_i = 0
        u.path_ver = g.version
        u.path_goal = g.cell_of(gx, gy)
        u.repath_at = self.now() + SPS // 2
        self.stats['repaths'] += 1
        return True

    def cut_piece(self, u, exp):
        """A-PB4 [inf, fitted to oracle wmmaze / wmsolo]: a search that needed more than PATH_PIECE A* expansions only gives
        the unit the first PATH_PIECE / exp share of the route (at least PIECE_MIN): the original's limited search
        ends a long route early and the unit asks again where it ended (the route itself is the full optimal one, so
        a winding lane is still walked to its end). The cut point lies on a segment of the smoothed route."""
        if self.pab:
            return                                # PATH-AB: the searches are bounded by their own pop budgets, a long route is a chain of legs
        tot = 0.0
        px, py = u.x, u.y
        jump = False
        for wx, wy in u.path:
            if wx == MARK_X:
                jump = True                       # WAYGATE-PORT: a crossing costs no length
                continue
            if not jump:
                dx, dy = wx - px, wy - py
                tot += math.sqrt(dx * dx + dy * dy)
            jump = False
            px, py = wx, wy
        self.wk += len(u.path)
        keep = tot * PATH_PIECE / exp
        if keep < PIECE_MIN:
            keep = PIECE_MIN
        if keep >= tot - 32.0:
            return
        out = []
        px, py = u.x, u.y
        jump = False
        for wx, wy in u.path:
            if wx == MARK_X:
                jump = True
                out.append((wx, wy))
                continue
            dx, dy = wx - px, wy - py
            sl = 0.0 if jump else math.sqrt(dx * dx + dy * dy)
            if jump:
                jump = False
                out.append((wx, wy))
                px, py = wx, wy
                continue
            if keep <= sl:
                f = keep / sl if sl > 0.0 else 0.0
                out.append((r32(px + dx * f), r32(py + dy * f)))
                break
            out.append((wx, wy))
            keep -= sl
            px, py = wx, wy
        self.wk += len(out)
        u.path = out
        u.path_partial = True

    def turn_to(self, u, want, dt=STEP):
        """CGameState_p3 / NIpse_CPoPosBh §6.1 [obs turnRate]: one steering step may turn at most
        turnRate x 360 x dt degrees (no turnRate -> instant, 360)."""
        diff = (want - u.facing + 540.0) % 360.0 - 180.0
        tr = float(u.data.get('turnRate', 0) or 0)
        stepdeg = tr * 360.0 * dt if tr > 0 else 360.0
        if abs(diff) <= stepdeg:
            u.facing = r32(want % 360.0)
        else:
            u.facing = r32((u.facing + (stepdeg if diff > 0 else -stepdeg)) % 360.0)

    def seg_step(self, u, wx, wy, d, moving_goal):
        """A19b: one step towards (wx, wy). Direction, heading and remaining length are computed when a segment
        starts (new target, or the unit was moved / is chasing a moving goal) and then only advanced: x += ux·d,
        remaining −= d; arrival when remaining ≤ d + 0.5. -> True on arrival (position = the target).
        PORT2-FIX: the facing turns toward the segment heading on EVERY step (turn clamp per step), also on the
        cached steps (the U# fast paths do the same with the stored heading)."""
        if self.pab:
            return self.step_to(u, wx, wy, d, moving_goal)
        sg = u.seg
        if not moving_goal and sg is not None and sg[0] == wx and sg[1] == wy and sg[2] == u.x and sg[3] == u.y:
            ux, uy, rem, ang = sg[4], sg[5], sg[6], sg[7]
        else:
            dx, dy = wx - u.x, wy - u.y
            rem = math.hypot(dx, dy)
            if rem > 0:
                ang = math.degrees(math.atan2(dy, dx)) % 360.0
                ux, uy = dx / rem, dy / rem
            else:
                ang = 0.0
                ux = uy = 0.0
        if rem > 0:
            self.turn_to(u, ang, self.mv * STEP)
        if self.arrive_short and not moving_goal:
            if rem <= FOLLOW_R:
                u.seg = None                             # [obs runtime 1.26a] the order ends short of the goal: no step, no snap
                return True
            if rem <= d + ARRIVE_EPS:                    # the 16-step stand-in tick (up to 41 u) would pass the arrival ring: stop ARRIVE_STOP short of the goal
                u.x, u.y = r32(wx - ux * ARRIVE_STOP), r32(wy - uy * ARRIVE_STOP)
                u.seg = None
                return True
        if rem <= d + ARRIVE_EPS:
            u.x, u.y = wx, wy
            u.seg = None
            return True
        u.x, u.y = r32(u.x + ux * d), r32(u.y + uy * d)
        u.seg = None if moving_goal else (wx, wy, u.x, u.y, ux, uy, rem - d, ang)
        return False

    def near(self, u, wx, wy):
        """STEER (switch on): within 0.49 cells (15.68) of the aim at the start of the tick = arrived. A cached segment carries the rest (rem, advanced by the step
        length each tick); the C# fast path decides with it, so the slow path does too (the true distance can differ from it by 1e-5, which at the radius would
        flip a step in one VM only)."""
        sg = u.seg
        if sg is not None and sg[0] == wx and sg[1] == wy and sg[2] == u.x and sg[3] == u.y:
            return sg[6] <= FOLLOW_R
        dx, dy = wx - u.x, wy - u.y
        return dx * dx + dy * dy <= AB_ARR2

    def turn_consts(self, u):
        """STEER [obs 0x6f08a670 / 0x6f08a7a0, probe G5, NOTES_pathing 8.2]: (cos ang, sin ang, cos prw) of the unit: ang = the turn rate value (rad per tick, min
        0.001; a type without one: the agent's 0.4), prw = the propulsion window (data: degrees; default 1.2 rad). Series, not libm: both VMs get the same bits."""
        d = u.data
        tr = float(d.get('turnRate', 0) or 0)
        ang = tr if tr > 0.0 else 0.4
        if ang < 0.001:
            ang = 0.001
        pw = float(d.get('propWin', 0) or 0)
        prw = pw * (math.pi / 180.0) if pw > 0.0 else 1.2
        ca, sa = cos_sin(ang)
        cp = -2.0 if prw >= 3.14159 else cos_sin(prw)[0]
        u.tc = (ca, sa, cp)
        return u.tc

    def step_to(self, u, wx, wy, d, moving_goal):
        """STEER (NOTES_pathing 8.3, replaces A19b seg_step with the switch on): one tick towards (wx, wy). -> True when arrived: within 0.49 cells at the start of the
        tick (no step, no snap). The commanded heading turns towards the aim by at most `ang`; when the angle between heading and aim is >= `prw` the speed of the
        tick is 0 (turn in place); the unit moves along its NEW heading. A segment is cached (direction, rest) while the heading equals its direction."""
        sg = u.seg
        cached = not moving_goal and sg is not None and sg[0] == wx and sg[1] == wy and sg[2] == u.x and sg[3] == u.y
        if not moving_goal and (sg[6] <= FOLLOW_R if cached else ((wx - u.x) * (wx - u.x) + (wy - u.y) * (wy - u.y) <= AB_ARR2)):
            u.seg = None
            return True
        if cached:
            ux, uy, rem = sg[4], sg[5], sg[6]
            u.x, u.y = r32(u.x + ux * d), r32(u.y + uy * d)
            u.seg = (wx, wy, u.x, u.y, ux, uy, rem - d)
            return False
        dx, dy = wx - u.x, wy - u.y
        if -1e-3 < dy < 1e-3 and (dx > 1.0 or dx < -1.0):
            u.y = wy                                  # a lateral error below the float32 resolution of the position never closes by walking (the unit would
            dy = 0.0                                  # stand 1e-5 below a cell edge for ever): snap it
        elif -1e-3 < dx < 1e-3 and (dy > 1.0 or dy < -1.0):
            u.x = wx
            dx = 0.0
        rem = math.hypot(dx, dy)
        if rem <= 0.0:
            u.seg = None
            return False
        ux, uy = dx / rem, dy / rem
        tc = u.tc or self.turn_consts(u)
        if u.hf != u.facing:                          # the facing was set by something else (creation, attack turn, SetUnitFacing): the heading follows it
            a = u.facing
            if a > 180.0:
                a -= 360.0
            u.hx, u.hy = cos_sin(a * (math.pi / 180.0))
        hx, hy = u.hx, u.hy
        dot = hx * ux + hy * uy
        mv = dot > tc[2]
        if dot > tc[0]:
            nx_, ny_ = ux, uy                         # within `ang`: the heading is the aim
            al = True
        else:
            sgn = 1.0 if hx * uy - hy * ux >= 0.0 else -1.0
            nx_, ny_ = hx * tc[0] - sgn * hy * tc[1], hy * tc[0] + sgn * hx * tc[1]
            nn = math.sqrt(nx_ * nx_ + ny_ * ny_)
            nx_, ny_ = nx_ / nn, ny_ / nn
            al = False
        if nx_ != hx or ny_ != hy or u.hf != u.facing:
            u.facing = r32(math.degrees(math.atan2(ny_, nx_)) % 360.0)
        u.hx, u.hy, u.hf = nx_, ny_, u.facing
        if not mv:
            u.seg = None
            return False
        if al and rem <= d:
            u.x, u.y = wx, wy                         # (a step longer than the rest: only a moving goal / a short detour; never an arrival rule)
            u.seg = None
            return moving_goal
        u.x, u.y = r32(u.x + nx_ * d), r32(u.y + ny_ * d)
        u.seg = (wx, wy, u.x, u.y, ux, uy, rem - d) if (al and not moving_goal) else None
        return False

    def straight(self, u, gx, gy, d, finish):
        """the W3 straight-line move (pathing off / flying / no map)."""
        g = self.grid
        if g is not None and self.flying(u):
            x0, y0 = u.x, u.y
            arrived = self.seg_step(u, gx, gy, d, not finish)
            if not g.flyable(*g.cell_of(u.x, u.y)) and g.flyable(*g.cell_of(x0, y0)):
                u.x, u.y = x0, y0                     # LTW-LANE: a flyer stops at a no-fly cell (wpm 0x04); the order ends
                u.seg = None
                self.clear_order(u)
                u.attack_move = False
                u.next_scan = self.now()
                self.wake(u, self.now())
                self.log('fly blocked u%d' % u.serial)
                return False
            return finish if arrived else False
        if self.seg_step(u, gx, gy, d, not finish):
            return finish
        return False

    def reach_site(self, u):
        tid, cx, cy = u.build
        fp = self.oriented(self.N.unit_types[tid])
        half = max(fp[0], fp[1]) * 16.0 if fp else 0.0
        r = half + BUILD_REACH
        dx, dy = cx - u.x, cy - u.y
        return dx * dx + dy * dy <= r * r

    # ================================================================ the step
    def guard_arm(self, u):
        """a unit with a guard point joins the guard list (guard_step walks only this list, not every unit)."""
        if u not in self.guard_units:
            self.guard_units.append(u)

    def guard_step(self, now):
        """CGuardPost / NOTES_combat §7 [obs 0x6f5539c0 / 0x6f553af0 / 0x6f553b30 / 0x6f553c10]: a creep (owner slot
        >= 12 [obs NOTES_combat §7]; SetUnitCreepGuard arms any unit) with a guard point that is beyond GuardDistance
        (MiscData 600), not fighting and without an explicit order / path returns to it after GuardReturnTime (5 s).
        A unit without a guard point is never sent back. The next check is re-armed after each return order, so a
        blocked creep retries. PORT2-FIX: computer players' own units get no guard point (TD creeps are not dragged
        back); the list holds only the guarded units (lazy removal)."""
        gd2 = self.guard_dist * self.guard_dist
        keep = []
        for u in self.guard_units:
            if not u.guard or u.guard_x is None or not u.alive:
                continue
            keep.append(u)
            if u.dead:
                continue
            if u.owner.id == 15 and now < steps_of(6.0):
                continue                                  # 0x6f553c10: owner slot 15 does not return in the first 6000 ms [obs]
            self.wk += 1
            dx, dy = u.x - u.guard_x, u.y - u.guard_y
            if dx * dx + dy * dy <= gd2:
                u.guard_away = False
                u.guard_due = -1
                continue
            if u.atk_target is not None or u.gpt is not None:
                continue                                  # still fighting (0x6f553af0 runs again when it stops)
            returning = u.order == O_MOVE and u.target is None and u.tx == u.guard_x and u.ty == u.guard_y
            if not returning and (u.order or u.path is not None or u.target is not None):
                u.guard_away = False                      # an explicit order / path of the map owns the unit
                u.guard_due = -1
                continue
            if not u.guard_away:
                u.guard_away = True
                u.guard_due = now + steps_of(self.guard_wait)
            if u.guard_due >= 0 and now >= u.guard_due:
                if not returning:
                    self.issue_order(u, O_MOVE, u.guard_x, u.guard_y)
                u.guard_due = now + steps_of(self.guard_wait)     # retry if the way back is blocked
        self.guard_units = keep

    def step(self, dt):
        """one 5 ms game step (docs/engine_w5.md §2): construction, death watch, missiles, fighters, movement."""
        now = self.now()
        N = self.N
        self.itm.step()                                  # CItem §5.4: the 5 s dead-object timer (0xd01c1)
        self.hv.step()                                   # PORT-Harvest: worker trips / mine ticks / wisp credit
        if self.grid is not None and getattr(self.grid, "fld_on", False):   # test grids without the field slots
            self.grid.fld_tick(now)                      # PATH-FIELD: fields due at this step are installed (the client stalls here until they arrived)
        if now % self.SEL_PRUNE_EVERY == 0:
            self.prune_selection()                       # CSelectionWar3 §5: dead / hidden selected units leave and stop (after the harvest step, as W3Engine SelPrune)
        if self.constructing:
            for b in list(self.constructing):
                self.wk += 1
                if b.alive and not b.dead and now >= b.construct_done and b in self.constructing:
                    self.constructing.remove(b)
                    self.finish_construction(b)
        if self.upgrading:
            for b in list(self.upgrading):
                self.wk += 1
                if b in self.upgrading and b.alive and not b.dead and b.upg is not None:
                    self.upgrade_interp(b)                # CAbilityUpgradeInProgress §5: linear max life
                    if now >= b.upg[1]:
                        self.upgrading.remove(b)
                        self.upgrade_finish(b)            # A-UP2: after construction, before training
        if self.trainers:
            for b in list(self.trainers):
                self.wk += 1
                if b in self.trainers and b.alive and not b.dead and b.tq_end >= 0 and now >= b.tq_end:
                    self.train_finish(b)                  # A-PR3: after construction, before the death watch
        if self.neg_regen:
            for u in list(self.neg_regen):
                self.wk += 1
                if u.alive and not u.dead and u.life <= UNIT_DEATH_LIFE:
                    N.kill_unit(u, None)
        if self.timed:
            for u in list(self.timed):                    # W8 timed life ends: dies without a killer (A-SP19)
                self.wk += 1
                if u in self.timed and u.alive and not u.dead and u.expire_at >= 0 and now >= u.expire_at:
                    self.timed.remove(u)
                    N.kill_unit(u, None)
        if self.sp.casts or self.sp.missiles or self.sp.areas or self.sp.flames or self.sp.monsoons:
            self.sp.step(dt)                              # W8 spell missiles + cast phases (A-SP5, A-SP8)
        if self.ab.timers:
            self.ab.step()                                # buffs, DoTs, auras, wander (A-AB2: before missiles' end)
        self.step_missiles(dt)
        q = self.wakeq
        while q and q[0][0] <= now:
            s, serial = heapq.heappop(q)
            self.wk += 1
            u = self.by_serial.get(serial)
            if u is None or u.wake_at != s:
                continue                                  # stale entry
            u.wake_at = -1
            if not u.alive or u.dead or not u.fighter or u.constructing:
                continue
            if u.paused or u.hidden:
                continue                                  # woken again by PauseUnit(false) / ShowUnit(true) [W9R3-6-HIDDEN-FIGHT: a hidden unit does nothing]
            self.cur = u
            self.fight(u)
            self.cur = None
            if u.alive and not u.dead and u.fighter:
                nw = self.next_wake(u)
                if nw >= 0:
                    self.wake(u, nw)
        if self.grid is not None and now % OCC_REBUILD == 0:
            self.occ_rebuild()                    # A-COL1: on a fixed step grid (both VMs, movers or not)
        if self.pab and self.grid is not None and now % OCCS_EVERY == 0:
            self.occs_rebuild()
        if self.pbq:
            for pid in sorted(self.pbq):
                if self.pbq[pid][0]:
                    self.pb_serve(pid)            # A-PB1: waiting requests are served every step, before movement
        self.guard_step(now)                      # CGuardPost: creep leash / guard return
        if N.moving:
            for u in sorted(N.moving, key=lambda v: v.serial):
                self.wk += 1
                if (now + u.serial) % self.mv:
                    continue                      # A19c: not this unit's movement step
                if not u.alive or u.dead or u.paused or u not in N.moving:
                    continue
                if u.creq and not u.preq:
                    continue                      # A-PB5: a blocked unit stands until its collision repath is served
                if u.pick is not None and self.itm.arrived(u):
                    N.dirty.add(u)
                    continue
                if u.cast is not None and self.sp.arrived(u):
                    N.dirty.add(u)
                    continue
                if u.build is not None and self.reach_site(u):
                    self.start_construction(u)
                    N.dirty.add(u)
                    continue
                if self.pab and now < u.wait_until:
                    if u.vmv:
                        u.vmv = False                 # STEER-2: the wait counter runs: the unit stands (its footprint has no velocity)
                        self.occs_note(u, True)
                    continue
                k0 = self.occ_key(u)
                x0, y0 = u.x, u.y
                sg0, pi0, pa0, sf0 = u.seg, u.path_i, u.path, u.ab_safe
                self.ab_gu = False
                done = self.move_unit(u, dt * self.mv)
                k1 = self.occ_key(u) if k0 is not None else None
                if self.pab:
                    # STEER-2 [obs 0x6f680510 / 0x6f680010 / 0x6f67fec0, steer 8.0 rule 9]: the unit that enters a cell another ground unit is in waits 4 ticks (the blocker MOVES and is not faster / belongs to another player) or 20 ticks
                    # (the blocker moves and is slower, same owner); a STANDING blocker sets no wait: the blocked handler decides (retry counter, the fine route is dropped, search B sees the unit). No squeezing in, no collision request
                    wt = 0
                    st = False
                    if k1 is not None and k1 != k0 and self.occ.get(k1, 0) > 0:
                        wt, st = self.ab_block(u, k1)
                        if st and self.ab_cls(u) and self.ab_in_block(u, x0, y0, k1):
                            st = False            # STEER-4 [inf]: a standing unit inside the footprint block the mover already had before the step is no blocker (the original looks at the block AHEAD): it slides off
                    if wt or st:
                        u.x, u.y = x0, y0
                        if u.path is pa0:
                            u.path_i = pi0
                            u.ab_safe = sf0
                        done = False
                        if wt:
                            u.wait_until = now + (wt + 1) * self.mv      # the tick that found the blocker + wt ticks of the wait counter, the unit stands in all of them
                            if sg0 is not None and sg0[2] == x0 and sg0[3] == y0 and u.path is pa0 and u.path_i == pi0:
                                u.seg = sg0               # the same segment goes on after the wait (the U# fast path)
                        else:
                            u.seg = None
                            u.coll_wait += 1
                            gx_, gy_ = self.goal_of(u)
                            if self.ab_blocked(u, gx_, gy_):
                                done = self.ab_ended(u)
                    elif k1 is not None and k1 != k0:
                        self.occ[k0] = self.occ.get(k0, 0) - 1
                        self.occ[k1] = self.occ.get(k1, 0) + 1
                        u.coll_wait = 0
                    mvd = u.x != x0 or u.y != y0
                    if mvd and u.bx:
                        u.bx = 0                      # the unit walked: its boxed-in cycles start anew
                    u.vmv = mvd                       # the velocity bit follows the tick: a unit that did not step stands
                    self.occs_note(u, not mvd)
                elif k1 is not None and k1 != k0:
                    blk = self.occ.get(k1, 0) > 0
                    if blk and (u.coll_wait < self.cw or not u.side):
                        u.x, u.y = x0, y0                 # A-COL1: the cell ahead is taken: wait this step
                        if u.path is pa0:
                            u.path_i = pi0                # A-PB2: a waypoint "reached" by the undone step is not reached
                            u.ab_safe = sf0
                        if sg0 is not None and sg0[2] == x0 and sg0[3] == y0 and u.path is pa0 and u.path_i == pi0:
                            u.seg = sg0                   # the same segment goes on next time (the U# fast path)
                        u.coll_wait += 1
                        done = False
                        if u.side:
                            self.pb_sidestep(u, k0)       # A-PB2: the served collision request: around the blocker
                        elif not u.creq and not u.preq:
                            u.creq = True
                            u.creq_at = self.now()
                            self.pb_queue(u.owner.id).append(u)
                    else:
                        if blk:
                            u.side = False                # A-PB2: squeezing in after COLL_WAIT needs a served request too
                        self.occ[k0] = self.occ.get(k0, 0) - 1
                        self.occ[k1] = self.occ.get(k1, 0) + 1
                        u.coll_wait = 0
                if done and self.pab and u.order == 852018 and not self.ab_gu:
                    u.ab_arr = True               # STEER-4b [inf]: the route of a harvest walk is walked to its end (the target footprint touched; the step was not undone by a blocker, no give-up): the range check of the order is due now
                if self.pab and u.retry and (u.x != x0 or u.y != y0):
                    u.retry = 0                           # the follower walked: the retry counter of the blocked handler starts anew [obs 2.6 0x6f67fdc0]
                if u.wg_via is not None:
                    done = self.wg_post(u, done)  # A-AB22: entering the gate's area teleports
                self.relocate(u)
                N.dirty.add(u)
                if u.lc is not None:
                    done = self.sp.lc_moved(u, done)     # NOTES_spells §5.16: the locust's own arrival (A-SP83)
                if done:
                    if u.pick is not None:
                        if not self.itm.arrived(u):
                            u.pick = None                 # the item moved / is gone: the order ends (A-IT6)
                            u.iop = None
                            u.order = 0
                            N.moving.discard(u)
                    elif u.cast is not None:
                        self.sp.reached_point(u)          # W8: at the order point and still out of range
                    elif u.build is not None:
                        self.start_construction(u)
                    elif u.order == O_ROOT:
                        self.root_arrive(u)               # PORT4-Build: CAbilityRoot §8.2, at the point
                    elif u.order == O_PATROL and u.pat is not None and u.atk_target is None and u.gpt is None:
                        self.patrol_turn(u)               # MOVE-PORT: the other leg of the patrol
                    elif u.atk_target is None and u.gpt is None:
                        self.log('arrive u%d' % u.serial)
                        u.order = 0
                        u.attack_move = False
                        u.path = None
                        N.moving.discard(u)
                        u.next_scan = now
                        self.wake(u, now)


AHAR = (ord('A') << 24) | (ord('h') << 16) | (ord('a') << 8) | ord('r')
AVUL = (ord('A') << 24) | (ord('v') << 16) | (ord('u') << 8) | ord('l')
