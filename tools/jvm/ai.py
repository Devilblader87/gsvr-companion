#!/usr/bin/env python3
"""Computer-player AI state (W11 slice): the engine-side object the AI natives read and write.

Reference: docs/decomp/classes/Unclassed_Native_0001.md (§0 fields, §3 command queue, §4 difficulty),
Unclassed_Native_0003.md (§3 configuration word, §4 melee/campaign mode) and Unclassed_Native_0041.md
(§1 start / CommandAI / PauseCompAI / GetAIDifficulty).  The AI *script* VM lives in aivm.py (S1-S7; docs/ai_plan.md); this
object holds the per-player state the AI natives own:

  * `flags`  — the AI configuration word (+0x2d0): bit 2 new heroes, 3 heroes flee, 4 units flee, 5 groups
               flee, 6 slow chopping, 9 target heroes, 10 heroes take items, 11 peons repair, 12 ignore
               injured, 13 watch mega targets, 14 melee mode (SetCampaignAI clears it, SetMeleeAI sets it),
               19 random paths, 21 defend player, 22 heroes buy items;
  * `pause`  — the pause depth (+0x1d8): PauseCompAI(pause) increments, PauseCompAI(resume) decrements;
  * `hero_lv` / `script` — SetHeroLevels code and the start script name;
  * `repl`   — SetReplacementCount (+0x1d4), reset value 3;
  * `difficulty` — GetAIDifficulty's word (+0x26c), 0 easy / 1 normal / 2 hard;
  * `cmds`   — the 12-entry LIFO command stack CommandAI fills (+0x1dc/+0x1e4/+0x218).

The Udon twin is unity/Assets/W3VR/Scripts/W3Ai.cs; both VMs keep the same fields and answers.
"""
import os

HV_N = 128                   # harvest mark table entries (aibuild.py)
RV_N = 16                    # reservation entries
FAIL_N = 8                   # spot-search failure entries
CP_MEM = 40                  # S5a (aicaptain.py): members per captain
CP_WANT = 12                 # ... wanted unit types per captain (the 12-slot group table of CCaptainAI_p2 s1)
CP_BAD_N = 8                 # ... goals the wave could not reach (skipped by GetCreepCamp for a while)
GP_N = 16                    # S6 (aiextra.py): guard posts
CS_N = 16                    # AI-CAST (aispell.py): hero spell attempts remembered
RP_N = 8                     # AI-MICRO (aimicro.py): repair assignments (structure, worker)
FL_N = 4                     # ... fleeing heroes
IT_N = 8                     # ... item attempts (pickup of an item serial, use of a hero's slot)
CMD_MAX = 12                 # CCompAI_p1 §6.11: the command stack holds 12 pairs
FLAG_NEW_HEROES = 0x4
FLAG_HEROES_FLEE = 0x8
FLAG_UNITS_FLEE = 0x10
FLAG_GROUPS_FLEE = 0x20
FLAG_SLOW_CHOPPING = 0x40
FLAG_TARGET_HEROES = 0x200
FLAG_HEROES_TAKE_ITEMS = 0x400
FLAG_PEONS_REPAIR = 0x800
FLAG_IGNORE_INJURED = 0x1000
FLAG_WATCH_MEGA = 0x2000
FLAG_MELEE = 0x4000
FLAG_RANDOM_PATHS = 0x80000
FLAG_DEFEND_PLAYER = 0x200000
FLAG_HEROES_BUY_ITEMS = 0x400000
FLAG_EASY = 0x8000           # MeleeDifficulty: bit 15 easy, bit 16 hard, neither normal
FLAG_HARD = 0x10000


def switch(name):
    """AI-CAST / AI-MICRO / AI-DEFEND switches (docs/ai_plan.md "Post-S7 subsystems"): the variable `name` = 1, or W3_AI_EXTRAS=1 for all of
    them. Default OFF. The Udon twin has one public bool per switch and aiExtras for all (W3Vm)."""
    return os.environ.get(name) == '1' or os.environ.get('W3_AI_EXTRAS') == '1'


class AIPlayer:
    """one computer-player object (player +0x2d4 in the original; keyed by slot here)."""

    def __init__(self, slot, difficulty=1):
        self.slot = slot
        self.on = False                      # the AI slot is started (Udon twin: W3Ai.cs aiOn[slot])
        self.flags = 0
        self.pause = 0
        self.repl = 3
        self.hero_lv = 0                     # SetHeroLevels code (+0x40), no consumer yet
        self.script = None                   # script name given to the first Start*AI call
        self.difficulty = difficulty
        self.cmds = []                       # [(command, data)] newest last, max CMD_MAX
        self.town_h = [0, 0, 0, 0]           # S2 census (aicensus.py): per town slot the serial of its hall / mine (0 none)
        self.town_m = [0, 0, 0, 0]
        self.town_mx = [0.0, 0.0, 0.0, 0.0]  # AI-MINEBUILD: where the slot's mine stood (a haunted mine replaces the neutral mine unit: same place, new serial)
        self.town_my = [0.0, 0.0, 0.0, 0.0]
        self.town_try = [-1000000, -1000000, -1000000, -1000000]   # step of the slot's last mine search
        self.town_step = -1                  # step of the last refresh
        # S3/S4 (aibuild.py): harvest marks per worker (table of HV_N serials), producer / worker reservations of the orders
        # posted but not yet applied, the chosen expansion mine, the spot-search failure table
        self.hv_ser = [0] * HV_N             # worker serial (0 = free entry)
        self.hv_req = [0] * HV_N             # this round's request: 0 none, 1 gold, 2 lumber
        self.hv_rtn = [0] * HV_N             # ... and its town slot
        self.hv_act = [0] * HV_N             # the previous round's request (ClearHarvestAI moves req -> act)
        self.hv_atn = [0] * HV_N
        self.hv_ord = [-1000000] * HV_N      # step of the last harvest order given to the worker
        self.hv_step = -1                    # step of the last harvest pass
        self.rv_ser = [0] * RV_N             # reservations: producer / builder serial (0 = free)
        self.rv_step = [0] * RV_N             # step the order was posted
        self.rv_kind = [0] * RV_N             # 1 build, 2 train, 3 upgrade-to, 0 research / none
        self.rv_tid = [0] * RV_N              # type of a reserved build / train / upgrade (0 for a research)
        self.rv_x = [0.0] * RV_N              # site of a reserved build
        self.rv_y = [0.0] * RV_N
        self.cs_step = -1000000              # AI-CAST (aispell.py): step of the last hero spell pass, attempts (hero serial, ability, step), casts ordered
        self.cs_ser = [0] * CS_N
        self.cs_aid = [0] * CS_N
        self.cs_at = [0] * CS_N
        self.cs_n = 0
        self.cp_thw = 0                      # AI-DEFEND (aicaptain.py): weighted strength of the hostiles around the threat, the wave turned back (flag), turn-backs
        self.cp_dfd = False
        self.cp_dn = 0
        self.mi_step = -1000000              # AI-MICRO (aimicro.py): step of the last pass, repair assignments (structure / worker serial, step), fleeing heroes, counters
        self.rp_str = [0] * RP_N
        self.rp_wk = [0] * RP_N
        self.rp_at = [0] * RP_N
        self.fl_ser = [0] * FL_N
        self.fl_at = [0] * FL_N
        self.it_a = [0] * IT_N               # AI-MICRO item attempts: (item serial, 0) = a pickup order, (hero serial, slot + 1) = a use order, and its step
        self.it_b = [0] * IT_N
        self.it_at = [0] * IT_N
        self.mi_take = 0
        self.mi_use = 0
        self.mi_rep = 0
        self.mi_flee = 0
        self.exp_mine = 0                    # serial of the mine GetNextExpansion chose (0 none)
        self.exp_town = -1                   # the town slot it would become
        self.fail_tid = [0] * FAIL_N         # building types whose last spot search found nothing ...
        self.fail_until = [0] * FAIL_N       # ... until this step
        # S5a (aicaptain.py): two captains, 0 = attack, 1 = defense.  Members are unit serials (0 = free entry).
        self.cp_on = False                   # CreateCaptains was called
        self.cp_hset = False                 # the home points were set (SetCaptainHome / ResetCaptainLocs / first use)
        self.cp_mem = [0] * (2 * CP_MEM)
        self.cp_mord = [-1000000] * (2 * CP_MEM)     # step of the last order given to the member
        self.cp_wt = [0] * (2 * CP_WANT)             # wanted unit types / quantities (InitAssault clears, AddAssault adds)
        self.cp_wq = [0] * (2 * CP_WANT)
        self.cp_wn = [0, 0]
        self.cp_asl = [False, False]         # assault mode (InitAssault / AddDefenders was called)
        self.cp_state = [0, 0]               # 0 idle, 1 attacking (goal set), 2 retreating
        self.cp_size = [0, 0]                # wave size at launch (bc of CCaptainAI_p1 s0.1)
        self.cp_tgt = [0, 0]                 # serial of the unit the goal follows (0: a point goal)
        self.cp_hx = [0.0, 0.0]              # home points
        self.cp_hy = [0.0, 0.0]
        self.cp_gx = [0.0, 0.0]              # goal points
        self.cp_gy = [0.0, 0.0]
        self.cp_sx = [0.0, 0.0]              # stage points (SetStagePoint)
        self.cp_sy = [0.0, 0.0]
        self.cp_sset = [False, False]
        self.cp_gstep = [0, 0]               # step the current goal / retreat began
        self.cp_atg = [False, False]         # CaptainAtGoal latch
        self.cp_wait = [-1, -1]              # step the wants were first met while the members were still gathering
        self.cp_vs = [0, 0]                  # CaptainVsUnits (1) / CaptainVsPlayer (2 + slot) mark (stored, not consumed yet)
        self.cp_flags = 0                    # 1 SetCaptainChanges, 2 SetSmartArtillery, 4 GroupTimedLife (stored, not consumed yet)
        self.cp_step = -1000000              # step of the last captain pass
        self.cp_thr = False                  # a hostile non-creep unit near a town (TownThreatened)
        self.cp_tx = 0.0
        self.cp_ty = 0.0
        self.cp_waves = 0                    # waves launched (attack captain) and the step of the first one
        self.cp_first = -1
        self.cp_sent = 0                     # units sent in all waves
        self.cp_px = 0.0                     # centroid of the wave at the last progress sample, the sample step, stuck samples in a row
        self.cp_py = 0.0
        self.cp_pstep = 0
        self.cp_stuck = 0
        self.cp_fstep = -1                   # captain pass step of the cached "a member is fighting" answer, and the answer
        self.cp_fval = False
        self.cp_bx = [0.0] * CP_BAD_N        # unreachable goals: position and the step until which GetCreepCamp skips the camp there
        self.cp_by = [0.0] * CP_BAD_N
        self.cp_bu = [0] * CP_BAD_N
        self.cp_bn = 0                       # entries ever written (ring)
        self.al_tgt = 0                      # SetAllianceTarget (stored, not consumed yet)
        self.eb_ser = 0                      # GetEnemyBase result
        self.acq_tgt = 0                     # PORT8: CCompAI p1 s6.3 current AI target (unit serial, 0 none)
        # S6 (aiextra.py): guard posts, the hero the skill callback runs for, counters of the housekeeping natives
        self.gp_n = 0
        self.gp_tid = [0] * GP_N
        self.gp_x = [0.0] * GP_N
        self.gp_y = [0.0] * GP_N
        self.gp_ser = [0] * GP_N
        self.hero_cur = 0                    # serial of the hero while its skill callback runs (GetHeroId / GetHeroLevelAI)
        self.hp_step = -1000000              # step of the last hero pass
        self.zp_x = 0.0                      # LoadZepWave (stored)
        self.zp_y = 0.0
        self.ex_learn = 0                    # skills ordered / zeppelins ordered / units released by RemoveInjuries, RemoveSiege / teleported
        self.ex_zep = 0
        self.ex_inj = 0
        self.ex_sie = 0
        self.ex_tele = 0

    # ---- command stack (+0x1dc …): LIFO, read without popping
    def commands_waiting(self):
        return len(self.cmds)

    def last_command(self):
        return self.cmds[-1][0] if self.cmds else -1

    def last_data(self):
        return self.cmds[-1][1] if self.cmds else -1

    def pop_command(self):
        if self.cmds:
            self.cmds.pop()

    def command(self, cmd, data):
        if len(self.cmds) < CMD_MAX:
            self.cmds.append((int(cmd), int(data)))

    # ---- MeleeDifficulty: bits 15 / 16 of the configuration word
    def melee_difficulty(self):
        if self.flags & FLAG_EASY:
            return 1
        if self.flags & (FLAG_EASY | FLAG_HARD):
            return 3
        return 2

    def set_flag(self, mask, on):
        if on:
            self.flags |= mask
        else:
            self.flags &= ~mask


def is_fleeing(ai, serial):
    """AI-MICRO: the hero is running home (aimicro.flee_pass); the captains do not recruit it."""
    for i in range(FL_N):
        if ai.fl_ser[i] == serial:
            return True
    return False
