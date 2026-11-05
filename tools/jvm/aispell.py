#!/usr/bin/env python3
"""AI hero spells (docs/ai_plan.md AI-CAST): the part of the computer player that casts the heroes' active abilities.

The race scripts only learn the skills (SetHeroLevels, aiextra._hero_pass); in the original game the unit AI of a computer
player then uses them in a fight.  The decompile chapters carry no cast rule ("Open", NOTES_spells), so every number below is
[inf] and the whole pass sits behind the switch AI_CAST (env W3_AI_CAST=1 or W3_AI_EXTRAS=1, default OFF; Udon twin: W3Vm.aiCast / aiExtras).

Rules [inf]:
  * every CAST_EVERY steps (0.5 s) each of the player's live heroes (creation order, at most CAST_MAX) that is not casting
    gets at most one spell order, as a player input (the engine validates mana / range / target exactly like a HUD click);
  * a fight = an enemy (not allied, not a structure, not invulnerable, not a locust, visible to the hero) within FIGHT_R of
    the hero; without a fight only a heal is cast (a friendly unit below HEAL_PCT percent life);
  * the hero's abilities are walked in list order; the first one that is a modelled spell class, not a button, is ready (mana,
    cooldown, no attempt in the last GAP_STEPS steps) and finds a target is cast:
      - T_NONE (instant / self): an enemy within the spell's Area (400 without one, at most NONE_MAX) - Divine Shield only
        below 40 % life, mirror image / avatar / big bad voodoo / elemental fury need two enemies;
      - T_POINT: the position of the enemy with the most enemies within CLUSTER_R, inside the cast range (Rng, else REACH);
      - T_UNIT with an enemy in its targs: the enemy in range that passes targs, heroes first, then the most life;
      - T_UNIT friendly only: a heal (Holy Light, Healing Wave, Rejuvenation) goes to the most hurt ally below HEAL_PCT, any
        other buff to the nearest allied non-structure (the hero itself first) during a fight;
    never cast: toggles / autocast classes (mana shield, immolation, defend, berserk, phase shift), blink, channel buttons,
    the repair classes (the peon-repair rule is separate), corpse targets (raise dead, cannibalize), every item class (AI..).
Udon twin: unity/Assets/W3VR/Scripts/W3AiSpell.cs.  Clean room: behaviour only, [inf] throughout.
"""
from . import unitdata as UD
from .ai import CS_N, switch
from .spells import CLASSES, BUTTONS, T_NONE, T_UNIT, T_POINT

AI_CAST = switch('W3_AI_CAST')    # default OFF: an unmeasured heuristic that changes every fight of a computer hero
CAST_EVERY = 100               # steps between two passes (0.5 s) [inf]
CAST_MAX = 4                   # heroes served per pass
ENEMY_MAX = 256                # enemies kept per hero (creation order; a fight is still seen past the cap)
FIGHT_R2 = 800.0 * 800.0       # an enemy this close to the hero = a fight [inf]
REACH = 600.0                  # cast range when the level record has none [inf]
NONE_R = 400.0                 # area of an instant spell without one [inf]
NONE_MAX = 600.0
CLUSTER_R2 = 250.0 * 250.0     # enemies this close to a candidate point count for it [inf]
HEAL_PCT = 0.6                 # a heal goes to an ally below this life fraction [inf]
SHIELD_PCT = 0.4               # Divine Shield below this life fraction [inf]
GAP_STEPS = 400                # no second attempt of the same (hero, spell) within 2 s [inf]
BUFF_GAP = 1600                # a non-heal friendly buff at most every 8 s per (hero, spell) [inf]
NEED_TWO = ('AOmi', 'AHav', 'AOvd', 'ANef', 'AEsf', 'Aphx')
HEAL = ('AHhb', 'AOhw', 'Arej')
SKIP = ('AEbl', 'Apsh', 'ANms', 'AEim', 'Adef', 'Absk', 'Arst', 'Arep', 'AAns', 'Arai', 'Acan', 'AIbl')
SHIELD = 'AHds'


def _live(u):
    return u is not None and u.alive and not u.dead


def _fresh(ai, serial, aid, steps, gap):
    """False while the last attempt of (serial, aid) is younger than `gap` steps."""
    for i in range(CS_N):
        if ai.cs_ser[i] == serial and ai.cs_aid[i] == aid:
            return steps - ai.cs_at[i] >= gap
    return True


def _note(ai, serial, aid, steps):
    k = -1
    for i in range(CS_N):
        if ai.cs_ser[i] == serial and ai.cs_aid[i] == aid:
            k = i
    if k < 0:
        for i in range(CS_N):
            if k < 0 and ai.cs_ser[i] == 0:
                k = i
    if k < 0:
        k = 0
        for i in range(CS_N):
            if ai.cs_at[i] < ai.cs_at[k]:
                k = i
    ai.cs_ser[k] = serial
    ai.cs_aid[k] = aid
    ai.cs_at[k] = steps


def _enemy_ok(eng, ab, h, v):
    """a unit the hero may cast at: alive, in play, not allied, not a structure / locust, not invulnerable, visible."""
    if v is h or not _live(v) or v.hidden or v.is_bldg or v.locust or v.invulnerable or v.owner.id == 15:
        return False
    if eng.ally(h.owner, v.owner):
        return False
    if v.invisible and not ab.visible_to(h, v):
        return False
    return True


def _enemy_targs(mask):
    al = mask & UD.T_ALLIANCE
    return al == 0 or (al & UD.T_ENEMY) != 0


def _pick_enemy(ab, h, lv, reach2, enemies, ne):
    """the enemy for a single-target spell: heroes first, then the most life, then the lowest serial."""
    best = None
    bk = None
    for i in range(ne):
        v = enemies[i]
        d2 = (v.x - h.x) ** 2 + (v.y - h.y) ** 2
        if d2 > reach2 or not ab.targ_ok(h, v, lv['targs']):
            continue
        key = (1 if v.hero else 0, v.life, -v.serial)
        if bk is None or key > bk:
            best, bk = v, key
    return best


def _pick_point(h, reach2, enemies, ne):
    """the enemy position with the most enemies around it inside the cast range (nearest, lowest serial on a tie)."""
    best = None
    bk = None
    for i in range(ne):
        v = enemies[i]
        d2 = (v.x - h.x) ** 2 + (v.y - h.y) ** 2
        if d2 > reach2:
            continue
        n = 0
        for j in range(ne):
            w = enemies[j]
            if (w.x - v.x) ** 2 + (w.y - v.y) ** 2 <= CLUSTER_R2:
                n += 1
        key = (n, -d2, -v.serial)
        if bk is None or key > bk:
            best, bk = v, key
    return best


def _pick_ally(eng, ab, N, h, lv, reach2, heal):
    """a heal: the ally below HEAL_PCT with the lowest life fraction; a buff: the hero itself, else the nearest ally."""
    best = None
    bk = None
    for v in N.units:
        if not _live(v) or v.hidden or v.locust or v.is_bldg or v.owner.id == 15:
            continue
        if v is not h and not eng.ally(h.owner, v.owner):
            continue
        if v.invisible or not ab.targ_ok(h, v, lv['targs']):
            continue
        d2 = (v.x - h.x) ** 2 + (v.y - h.y) ** 2
        if d2 > reach2:
            continue
        if heal:
            fr = v.life / v.maxlife if v.maxlife > 0 else 1.0
            if fr >= HEAL_PCT:
                continue
            key = (-fr, -v.serial)
        else:
            key = (1 if v is h else 0, -d2, -v.serial)
        if bk is None or key > bk:
            best, bk = v, key
    return best


def _cast_one(N, ai, h, steps, enemies, ne, fight):
    """one spell order for hero h or none -> True when an order was posted."""
    eng = N.eng
    ab, sp = eng.ab, eng.sp
    for aid, code, lv in ab.abil_list(h):
        if lv is None or code not in CLASSES or code in BUTTONS or code in SKIP or code[:2] == 'AI':
            continue
        if h.mana < lv['Cost'] or sp.cooling(h, aid):
            continue
        oid = sp.order_id(aid)
        if oid == 0 or not _fresh(ai, h.serial, aid, steps, GAP_STEPS):
            continue
        kind = CLASSES[code][0]
        reach = lv['Rng'] if lv['Rng'] > 0 else REACH
        if kind == T_NONE:
            if not fight:
                continue
            r = lv['Area'] if lv['Area'] > 0 else NONE_R
            if r > NONE_MAX:
                r = NONE_MAX
            if code == SHIELD:
                if h.life >= SHIELD_PCT * h.maxlife:
                    continue
            n = 0
            for i in range(ne):
                v = enemies[i]
                if (v.x - h.x) ** 2 + (v.y - h.y) ** 2 <= r * r:
                    n += 1
            if n < (2 if code in NEED_TWO else 1):
                continue
            eng.post_input(ai.slot, 0, h.serial, oid)
        elif kind == T_POINT:
            if not fight:
                continue
            v = _pick_point(h, reach * reach, enemies, ne)
            if v is None:
                continue
            eng.post_input(ai.slot, 1, h.serial, oid, v.x, v.y)
        else:
            if _enemy_targs(lv['targs']):
                if not fight:
                    continue
                v = _pick_enemy(ab, h, lv, reach * reach, enemies, ne)
            elif code in HEAL:
                v = _pick_ally(eng, ab, N, h, lv, reach * reach, True)
            else:
                if not fight or not _fresh(ai, h.serial, aid, steps, BUFF_GAP):
                    continue
                v = _pick_ally(eng, ab, N, h, lv, reach * reach, False)
            if v is None:
                continue
            eng.post_input(ai.slot, 2, h.serial, oid, 0.0, 0.0, v.serial)
        _note(ai, h.serial, aid, steps)
        ai.cs_n += 1
        return True
    return False


def engine_tick(N, ai, steps):
    """the AI player's hero spell pass (called by the host after the captain pass, slots ascending)."""
    if not AI_CAST or ai is None or steps - ai.cs_step < CAST_EVERY:
        return
    ai.cs_step = steps
    pl = N.players[ai.slot]
    eng = N.eng
    ab = eng.ab
    n = 0
    for h in N.units:
        if n >= CAST_MAX:
            break
        if h.owner is not pl or not _live(h) or not h.hero or h.illusion or h.hidden or h.constructing or h.cast is not None:
            continue
        n += 1
        enemies = []
        fight = False
        for v in N.units:
            if _enemy_ok(eng, ab, h, v):
                d2 = (v.x - h.x) ** 2 + (v.y - h.y) ** 2
                if d2 <= 1000.0 * 1000.0:
                    if len(enemies) < ENEMY_MAX:
                        enemies.append(v)
                    if d2 <= FIGHT_R2:
                        fight = True
        _cast_one(N, ai, h, steps, enemies, len(enemies), fight)
