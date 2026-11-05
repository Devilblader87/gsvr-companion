#!/usr/bin/env python3
"""AI program host (docs/ai_plan.md S1, group G0): runs the race AI scripts (human.ai ...) as JASS threads owned by a
computer player, next to the map VM.  Clean room: behaviour only, tags [obs] / [inf] as in docs/ai_plan.md.

Model (one rule set for this module and its Udon twin unity/Assets/W3VR/Scripts/W3AiHost.cs):

  * One *program* per race script = common.j + common.ai + <race>.ai, compiled from the player's own install (never
    committed, never shipped; the pack carries it only in the local JAIP section).  One *instance* (own globals, own
    VM object, own thread list) per computer player; the handle table is the map VM's, so a player handle means the
    same in both worlds.
  * `StartMeleeAI(player, "<race>.ai")` starts `main()` as an AI thread owned by that player.  `StartThread(code)`
    adds another one (at most MAX_LIVE_THREADS live per player, the original's six thread slots; a finished thread's
    slot is reused, the rest are counted and dropped).
  * StartMeleeAI only registers the player; the player's first tick runs the program's global initialisers and
    starts main() (so no native ever re-enters the interpreter).
  * The host ticks inside the sim step (after the map VM's step callbacks): players in ascending slot order, each
    player's runnable threads in a rotating order, all sharing the per-slot per-step op budget OP_BUDGET (constant,
    counted like the VM's instruction costs).  A thread that runs out of budget yields and continues next step.
  * `Sleep(seconds)` is game time on the 5 ms step grid: wake step = now + max(1, ceil(seconds / 0.005 - 1e-6)).
    There is no wall clock anywhere.  [inf] unit and rounding, pending S0 oracle facts.
  * A thread whose function ends or traps (unset variable, division by zero, bad code) dies; the others go on.
    [inf] the original's behaviour on a script error is open (decomp_plan_w10 C2).
  * Natives: HOST (StartThread, Sleep), PASS (the existing AI-state natives and a few pure common.j natives, run
    through the same implementation as the map VM), everything else is UNIMPLEMENTED: counted per name, logged once,
    returns the type's zero, never halts anything.  Later stages move names from UNIMPLEMENTED to PASS.
  * PauseCompAI(depth > 0) stops ticking the player's threads (game time keeps running) [inf].
"""
import math

from . import aibuild as AB
from . import aicaptain as CP
from . import aiextra as EX
from . import aispell as SPL
from . import aimicro as MIC
from . import compiler as C
from . import vm as V
from .vm import r32

OP_BUDGET = 2000                 # ops per player per step (docs/ai_plan.md section 4, constant)
HOLD_MAX = 100                   # AI-BATON: a thread the budget cut keeps the slot (no other thread of it runs) for at most 0.5 s ...
HOLD_OFF = 400                   # ... and then no new hold for 2 s, so a runaway loop cannot starve the other threads [inf]
MAX_THREADS = 64                 # thread record storage per player (Udon arrays are fixed size)
MAX_LIVE_THREADS = 6             # live AI thread slots of the original (Unclassed_AI_0001 s12.1); a finished slot is reused
MAX_SLOTS = 12
STEP = V.STEP
GINIT_BUDGET = V.OP_LIMIT        # each global initialiser: its own budget (J section 4)

RACE_SCRIPTS = ('human.ai', 'orc.ai', 'undead.ai', 'elf.ai')

NK_HOST, NK_PASS, NK_UNIMPL = 0, 1, 2

HOST_NATIVES = ('StartThread', 'Sleep')
# natives run through the map VM's implementation (names are exact; each also needs a Udon implementation id)
PASS_NATIVES = (
    'DoAiScriptDebug', 'GetAiPlayer', 'GetEnemyPower', 'SetHeroLevels', 'SetNewHeroes', 'SetCampaignAI', 'SetMeleeAI',
    'SetTargetHeroes', 'SetPeonsRepair', 'SetRandomPaths', 'SetDefendPlayer', 'SetHeroesFlee', 'SetHeroesBuyItems',
    'SetWatchMegaTargets', 'SetIgnoreInjured', 'SetHeroesTakeItems', 'SetUnitsFlee', 'SetGroupsFlee',
    'SetSlowChopping', 'SetReplacementCount', 'CommandsWaiting', 'GetLastCommand', 'GetLastData', 'PopLastCommand',
    'MeleeDifficulty', 'GetRandomInt', 'GetPlayers', 'I2R', 'VersionCompatible',
    'Player', 'GetPlayerState',
    # S2 census (aicensus.py): read-only queries of the engine state
    'GetUnitCountDone', 'GetUnitCount', 'GetTownUnitCount', 'GetPlayerUnitTypeCount', 'GetUpgradeLevel',
    'GetUpgradeGoldCost', 'GetUpgradeWoodCost', 'GetMinesOwned', 'GetGoldOwned', 'TownWithMine', 'TownHasMine',
    'TownHasHall', 'GetUnitGoldCost', 'GetUnitWoodCost', 'GetUnitBuildTime', 'GetFoodMade', 'GetFoodUsed',
    # S3 / S4 (aibuild.py): build planner, expansion, harvest
    'SetProduce', 'SetUpgrade', 'SetExpansion', 'GetNextExpansion', 'GetExpansionPeon', 'GetExpansionFoe',
    'GetExpansionX', 'GetExpansionY', 'HarvestGold', 'HarvestWood', 'ClearHarvestAI', 'StopGathering',
    # S5a (aicaptain.py): captains, assault groups, attack intel; common.j natives the attack thread reads
    'CreateCaptains', 'SetCaptainHome', 'ResetCaptainLocs', 'ShiftTownSpot', 'SetStagePoint', 'ClearCaptainTargets',
    'SetCaptainChanges', 'SetSmartArtillery', 'GroupTimedLife', 'CaptainVsUnits', 'CaptainVsPlayer', 'CaptainAttack',
    'AttackMoveXY', 'AttackMoveKill', 'CaptainGoHome', 'CaptainIsHome', 'CaptainIsFull', 'CaptainIsEmpty', 'CaptainGroupSize',
    'CaptainReadiness', 'CaptainReadinessHP', 'CaptainReadinessMa', 'CaptainRetreating', 'CaptainInCombat', 'CaptainAtGoal',
    'InitAssault', 'AddAssault', 'AddDefenders', 'GetCreepCamp', 'StartGetEnemyBase', 'WaitGetEnemyBase', 'GetEnemyBase',
    'TownThreatened', 'GetMegaTarget', 'SetAllianceTarget', 'GetAllianceTarget', 'CreepsOnMap', 'UnitInvis',
    'GetPlayerAlliance', 'GetPlayerStructureCount', 'GetFloatGameState', 'UnitAlive',
    # S5b / S6 (aiextra.py): housekeeping, enemy intel, guard posts, zeppelin, heroes, debug no-ops
    'DisplayText', 'DisplayTextI', 'DisplayTextII', 'DisplayTextIII', 'DebugS', 'DebugFI', 'DebugUnitID',
    'RemoveInjuries', 'RemoveSiege', 'IgnoredUnits', 'GetEnemyExpansion', 'IsTowered', 'TeleportCaptain', 'AddGuardPost',
    'FillGuardPosts', 'ReturnGuardPosts', 'DisablePathing', 'SetAmphibious', 'GetBuilding', 'Unsummon', 'MergeUnits',
    'ConvertUnits', 'PurchaseZeppelin', 'LoadZepWave', 'SuicidePlayer', 'SuicidePlayerUnits', 'SuicideUnit', 'SuicideUnitEx',
    'GetHeroId', 'GetHeroLevelAI')


def native_kind(name):
    """the pack writes this per native of an AI program (JAIP JNAT kind column); the hosts read it back."""
    if name in HOST_NATIVES:
        return NK_HOST
    if name in PASS_NATIVES or name.startswith('Convert'):
        return NK_PASS
    return NK_UNIMPL


def pass_hash():
    """fingerprint of PASS_NATIVES (FNV-1a 32 over the sorted names joined by newlines, 8 hex digits).  The pack's JNAT kind
    column freezes which natives are pass-through, so the pack writes this into each program's JMET (`pass=`) and a host whose
    own list hashes differently loads no AI programs (the bot plays).  The Udon host holds the same value as the constant
    AV_PASS_HASH (W3AiHost.cs); test_melee_ai pins the two together."""
    h = 0x811c9dc5
    for b in '\n'.join(sorted(PASS_NATIVES)).encode('ascii'):
        h = ((h ^ b) * 0x01000193) & 0xffffffff
    return '%08x' % h


def pass_meta_ok(meta_hash):
    """True when a pack program written with pass-list hash `meta_hash` matches this host's PASS_NATIVES."""
    return meta_hash == pass_hash()


def script_key(script):
    """'Scripts\\Human.ai' -> 'human.ai' when it is one of the four race scripts, else None (campaign / unknown names
    keep the fallback bot).  The name comes from the map: nothing but the table lookup is trusted."""
    if not isinstance(script, str):
        return None
    k = script.replace('/', '\\').split('\\')[-1].strip().lower()
    return k if k in RACE_SCRIPTS else None


def _decode(raw):
    if raw[:3] == b'\xef\xbb\xbf':
        raw = raw[3:]
    return raw.decode('latin-1')


def ai_source_list(read, common_j, key):
    """[(origin, text)] to compile for race script `key`, or None when the install lacks common.ai or the script.
    read(name) -> bytes | None with names like 'Scripts\\common.ai' (an install reader)."""
    ca = read('Scripts\\common.ai')
    rc = read('Scripts\\' + key)
    if not ca or not rc:
        return None
    return [('common.j', common_j), ('common.ai', _decode(ca)), (key, _decode(rc))]


def compile_ai(sources):
    return C.compile_sources(sources)


def sleep_steps(w):
    """wake delay of Sleep(w): ceil(w / STEP - 1e-6) steps, at least 1; a non-finite w -> 1; clamped to 1e9
    (W3AiHost.AvTickSlot does the same)."""
    if not math.isfinite(w):
        return 1
    x = math.ceil(w / STEP - 1e-6)
    return 1 if x < 1 else (1000000000 if x > 1000000000 else int(x))


def zero_of(ret):
    return None if ret == 'nothing' else (0.0 if ret == 'real' else (None if ret == 'string' else 0))


class AiThread:
    __slots__ = ('th', 'wake', 'done')

    def __init__(self, th, wake):
        self.th = th
        self.wake = wake
        self.done = False


class AiSlot:
    def __init__(self, slot, prog, vm):
        self.slot = slot
        self.prog = prog
        self.vm = vm
        self.threads = []
        self.rr = 0
        self.hold = -1                   # AI-BATON: the thread the budget cut (it keeps the slot until it sleeps), its latest step, no new hold before
        self.hold_until = 0
        self.hold_off = 0
        self.ops = 0
        self.yields = 0
        self.dropped = 0
        self.errors = {}
        self.booted = False
        self.bad = False                 # a pass-through native asked the thread to wait / run tasks (ST_NATIVE)


class AiHost:
    """loader(key) -> Program | None (called once per race script, cached)."""

    def __init__(self, N, loader, budget=OP_BUDGET):
        self.N = N
        self.loader = loader
        self.budget = budget
        self.progs = {}
        self.stale_logged = False
        self.slots = {}
        self.native_counts = {}
        self.unimpl = {}
        self.unimpl_log = []             # one entry per unimplemented native name, first call only
        self.error_log = []              # one line per thread that died of ST_DEPTH / ST_NATIVE
        self.tick_no = 0
        self._cur = None
        N.vm.on_step.append(self.tick)

    # ------------------------------------------------------------ start
    def program(self, key):
        if key not in self.progs:
            p = self.loader(key)
            h = getattr(p, 'pass_hash', None)          # set on a program read back from a pack (JMET pass=)
            if p is not None and h is not None and not pass_meta_ok(h):
                if not self.stale_logged:
                    self.stale_logged = True
                    self.N.eng.log('aivm: AI programs were packed with another pass-through native list (%s, this host %s): '
                                   'no AI programs loaded, the bot plays' % (h, pass_hash()))
                p = None
            self.progs[key] = p
        return self.progs[key]

    def start(self, slot, script):
        """True when the real AI owns the slot now (or already did); False -> the caller keeps the fallback bot."""
        if slot in self.slots:
            return True
        if slot < 0 or slot >= MAX_SLOTS:
            return False
        key = script_key(script)
        if key is None:
            return False
        prog = self.program(key)
        if prog is None or 'main' not in prog.func_by_name:
            return False
        main = self.N.vm
        vm = V.VM(prog, speed='fast')
        vm.depth_cap = True                 # frames / value stack capped (vm.DEPTH_FRAMES, DEPTH_STACK)
        vm.H = main.H                       # one handle table: a player handle is the same number in both worlds
        vm.world = self.N
        st = AiSlot(slot, prog, vm)
        vm.natives = self._bind(st)
        self.slots[slot] = st
        return True                         # globals and main() start at the player's first tick (_boot)

    def _boot(self, st):
        """the first tick of a started player: the global initialisers (each its own budget), then main() as the
        first thread.  Deferred from StartMeleeAI so that the Udon twin never re-enters its interpreter from a
        native."""
        st.booted = True
        vm = st.vm
        for fidx in st.prog.ginit:
            th = vm.new_thread('aiginit')
            vm.start_func(th, fidx)
            before = vm.total_ops
            s = vm.run(th, GINIT_BUDGET)
            st.ops += vm.total_ops - before
            if st.bad:
                st.bad = False
                s = V.ST_NATIVE
            if s != V.ST_DONE:
                self._err(st, s)
        self._new_thread(st, st.prog.func_by_name['main'] + 1)

    # ------------------------------------------------------------ natives
    def _bind(self, st):
        fns = []
        N = self.N
        vm = st.vm
        counts = self.native_counts
        for info in st.prog.natives:
            fns.append(self._make(st, vm, N, counts, info))
        return fns

    def _make(self, st, vm, N, counts, info):
        name = info['name']
        ret = info['ret']
        kind = native_kind(name)
        zero = zero_of(ret)
        impl = None
        if kind == NK_PASS:
            from .natives import REG
            impl = REG.get(name)
            if impl is None:
                kind = NK_UNIMPL
        host = self

        def call(args):
            counts[name] = counts.get(name, 0) + 1
            if kind == NK_HOST:
                res = host._host_native(st, name, args)
            elif kind == NK_PASS:
                res = impl(N, *args)
                th = vm.thread_stack[-1]
                if th.wait_req is not None or th.wait_child is not None:
                    st.bad = True            # the Udon host: NativeCall pushed tasks -> the thread dies
                    host.error_log.append('slot %d: native %s needs tasks' % (st.slot, name))

            else:
                host.unimpl[name] = host.unimpl.get(name, 0) + 1
                if host.unimpl[name] == 1:
                    host.unimpl_log.append(name)
                res = zero
            if ret == 'nothing':
                return None
            if res is None:
                return zero
            if ret == 'boolean':
                return 1 if res else 0
            if ret == 'real':
                return r32(float(res))
            if isinstance(res, V.Agent):
                return vm.h(res)
            return res
        return call

    def _host_native(self, st, name, args):
        if name == 'Sleep':
            th = st.vm.thread_stack[-1]
            th.wait_req = float(args[0])
            return None
        # StartThread(code)
        self._new_thread(st, args[0])
        return None

    def _new_thread(self, st, code):
        live = 0
        free = -1
        for i in range(len(st.threads)):
            t = st.threads[i]
            if t.done:
                if free < 0:
                    free = i
            else:
                live += 1
        if live >= MAX_LIVE_THREADS:
            st.dropped += 1
            return
        vm = st.vm
        th = vm.new_thread('ai')
        if code <= 0 or code > len(st.prog.functions):
            self._err(st, V.ST_NOFUNC)
            return
        vm.start_func(th, code - 1)
        if free >= 0:                     # Unclassed_AI_0001 s12.1: the scan takes the first record that is free again
            st.threads[free] = AiThread(th, self.N.vm.steps)
        elif len(st.threads) < MAX_THREADS:
            st.threads.append(AiThread(th, self.N.vm.steps))
        else:
            st.dropped += 1

    def _err(self, st, status):
        n = V.STATUS_NAME.get(status, str(status))
        st.errors[n] = st.errors.get(n, 0) + 1

    # ------------------------------------------------------------ the step
    def tick(self, dt=None):
        """sim-step callback: ascending slots, each with the shared op budget."""
        steps = self.N.vm.steps
        self.tick_no += 1
        saved = self.N.ai_current
        try:
            for slot in sorted(self.slots):
                st = self.slots[slot]
                ai = self.N.ais.get(slot)
                if ai is not None and ai.pause > 0:
                    continue
                self.N.ai_current = ai
                self._tick_slot(st, steps)
                if ai is not None:
                    AB.engine_tick(self.N, ai, steps)       # S3 / S4: the engine-side periodic work of the player
                    CP.engine_tick(self.N, ai, steps)       # S5a: the captain pass
                    self._hero_pass(st, ai, steps)          # S6: the skill callback of the player's heroes
                    SPL.engine_tick(self.N, ai, steps)      # AI-CAST: the heroes' spells (switch W3_AI_CAST, default off)
                    MIC.engine_tick(self.N, ai, steps)      # AI-MICRO: worker repair, hero flight (switch W3_AI_MICRO, default off)
        finally:
            self.N.ai_current = saved

    def _tick_slot(self, st, steps):
        if not st.booted:
            self._boot(st)
        vm = st.vm
        n0 = len(st.threads)
        rem = self.budget
        if st.hold >= 0 and (st.hold >= n0 or steps > st.hold_until):
            if st.hold < n0:
                st.rr = (st.hold + 1) % n0              # the hold ran out: the others get their turn again, no new hold for a while
            st.hold = -1
            st.hold_off = steps + HOLD_OFF
        for k in range(n0):
            if rem <= 0:
                break
            i = (st.rr + k) % n0
            if st.hold >= 0 and i != st.hold:
                continue                                 # AI-BATON: a thread cut by the budget runs on before any other thread of the slot
            t = st.threads[i]
            if t.done or t.wake > steps:
                if t.done and st.hold == i:
                    st.hold = -1
                continue
            before = vm.total_ops
            s = vm.run(t.th, rem)
            used = vm.total_ops - before
            st.ops += used
            rem -= used
            if st.bad:                      # a native that needed tasks: the thread dies
                st.bad = False
                t.done = True
                if st.hold == i:
                    st.hold = -1
                self._err(st, V.ST_NATIVE)
            elif s == V.ST_WAIT:
                t.wake = steps + sleep_steps(t.th.wait_req)
                if st.hold == i:
                    st.hold = -1
            elif s == V.ST_LIMIT:
                t.wake = steps + 1
                st.yields += 1
                rem = 0
                if st.hold == i or steps >= st.hold_off:
                    if st.hold != i:
                        st.hold = i
                        st.hold_until = steps + HOLD_MAX
                    st.rr = i
                else:
                    st.rr = (i + 1) % n0
            else:
                t.done = True
                if st.hold == i:
                    st.hold = -1
                if s != V.ST_DONE:
                    self._err(st, s)
                    if s == V.ST_DEPTH:
                        self.error_log.append('slot %d: call depth cap' % st.slot)

    def _hero_pass(self, st, ai, steps):
        """S6 (aiextra.py): once per HERO_EVERY steps every hero with an unspent skill point (creation order, at most HERO_MAX)
        gets the script's SetHeroLevels code run as a short scratch thread of the player's program; the integer it returns is the
        ability to learn (a player input).  Not a native: no re-entry of the interpreter."""
        if ai.hero_lv == 0 or steps - ai.hp_step < EX.HERO_EVERY:
            return
        ai.hp_step = steps
        vm = st.vm
        code = ai.hero_lv
        if code <= 0 or code > len(st.prog.functions):
            return
        for u in EX.hero_targets(self.N, ai):
            ai.hero_cur = u.serial
            th = vm.new_thread('aihero')
            vm.start_func(th, code - 1)
            before = vm.total_ops
            s = vm.run(th, EX.HERO_BUDGET)
            st.ops += vm.total_ops - before
            ai.hero_cur = 0
            if st.bad:
                st.bad = False
                s = V.ST_NATIVE
            if s != V.ST_DONE:
                if s not in (V.ST_WAIT, V.ST_LIMIT):
                    self._err(st, s)
                continue
            aid = th.result
            if isinstance(aid, int) and aid != 0:
                self.N.eng.post_input(ai.slot, 0, u.serial, aid)
                ai.ex_learn += 1

    # ------------------------------------------------------------ introspection (tests, parity)
    def thread_states(self, slot):
        """[(alive, wake step)] per thread of the slot, creation order."""
        st = self.slots[slot]
        return [(not t.done, t.wake) for t in st.threads]

    def digest(self):
        """deterministic text of the host state; the Udon host (W3AiHost.AiDigest) prints the same lines."""
        out = []
        for slot in sorted(self.slots):
            st = self.slots[slot]
            alive = sum(1 for t in st.threads if not t.done)
            ai = self.N.ais.get(slot)
            out.append('ai slot %d threads %d alive %d ops %d yields %d dropped %d diff %d' % (
                slot, len(st.threads), alive, st.ops, st.yields, st.dropped, ai.melee_difficulty() if ai is not None else 0))
        for slot in sorted(self.slots):                  # S3 / S4: what the player owns and has marked (build / harvest divergence)
            out.extend(AB.digest_lines(self.N, slot))
            out.extend(CP.digest_lines(self.N, slot))
            out.extend(EX.digest_lines(self.N, slot))
        for name in sorted(self.native_counts):
            out.append('ai native %s %d' % (name, self.native_counts[name]))
        for name in sorted(self.unimpl):
            out.append('ai unimpl %s %d' % (name, self.unimpl[name]))
        return '\n'.join(out) + '\n'


def install_loader(common_j, install=None, core_ver=None, log=None):
    """loader(key) for AiHost: compile common.j + common.ai + <key> from the player's own install (never from the
    repository).  install: a directory; default $W3_INSTALL, else the one matching core_ver (run.build's: '131' ->
    <repo>/input/war3_131, anything else <repo>/input/war3_126).  Anything missing -> None, which leaves the fallback
    bot in charge; log(line) (default: print) says why."""
    import os
    import sys
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.abspath(os.path.join(here, '..', '..'))
    inst = install or os.environ.get('W3_INSTALL') or os.path.join(
        root, 'input', 'war3_131' if str(core_ver) == '131' else 'war3_126')
    say = log or print
    state = {}

    def reader():
        if 'r' not in state:
            state['r'] = None
            try:
                tools = os.path.join(root, 'tools')
                if tools not in sys.path:
                    sys.path.insert(0, tools)
                import war3fs
                core = war3fs.open_install(inst)
                state['r'] = lambda n: core.read(n, missing_ok=True)
            except Exception:                      # no install / unreadable: fallback
                state['r'] = None
        return state['r']

    def load(key):
        rd = reader()
        if rd is None:
            say('aivm: install %s unreadable: %s uses the bot' % (inst, key))
            return None
        try:
            srcs = ai_source_list(rd, common_j, key)
            if not srcs:
                say('aivm: no Scripts\\common.ai / %s in %s: the bot plays' % (key, inst))
                return None
            return compile_ai(srcs)
        except Exception as e:                     # untrusted / broken script: parse failures never reach the game
            say('aivm: %s did not compile (%s: %s): the bot plays' % (key, type(e).__name__, e))
            return None
    return load
