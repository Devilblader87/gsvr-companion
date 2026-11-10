"""Map sounds → the GSV3 "SNDS" section (tools/pack.py): every wav a map's entities can play, from the user's game
directory (<mod>/sound, then valve/sound). Stdlib only. Spec: docs/goldsrc_base_layer.md §1 SNDS.

Which wavs (hl.so, decomp/valve/NOTES_campaign.md §3.17; tags there):
  ambient_generic `message` (a "!SENTENCE" name is skipped: sentences are not packed), env_message `messagesound`,
  func_door / func_door_rotating / momentary_door `movesnd` 1..10 → doors/doormove<n>.wav, `stopsnd` 1..8 →
  doors/doorstop<n>.wav, `locked_sound` / `unlocked_sound` → ButtonSound; func_button / func_rot_button /
  momentary_rot_button `sounds` → ButtonSound; func_plat / func_platrot / func_train / func_trackchange(auto)
  `movesnd` / `stopsnd` → the plats tables; func_tracktrain `sounds` → plats/ttrain<n>.wav + ttrain_start1 /
  ttrain_brake1; func_rotating `message` or `sounds` 1..5 → fans/fan<n>.wav; func_tank / func_tanklaser /
  func_tankrocket / func_tankmortar `rotatesound` (a wav, pev->noise [obs CFuncTank::KeyValue 0xb0340], looped while
  the turret turns, vol 0.85 attn 0.8 [obs CFuncTank::StartRotSound 0xb3ce0]); func_mortar_field's launch / hit
  wavs; wall chargers, env_spark, env_explosion / explodemagnitude, func_pushable: their fixed wavs.
Loop points follow the engine's wav reader [ext: Quake snd_mem.c GetWavinfo lineage]: the first "cue " point is the
loop start (no cue chunk → the sound never loops, also on a looping channel); a LIST/adtl/ltxt "mark" length sets the
end (loopStart + length), else the end of the data.

SNDS layout (count = N clips, little endian, everything 4-byte aligned):
  int32 N, int32 nameBytes (padded), int32 totalSamples, int32 0
  N × 8 int32: rate, sampleCount, sampleBase (float index into samples), loopStart (−1 = no loop), loopEnd
               (exclusive, ≤ sampleCount), nameOff, nameLen, sourceBits (8 / 16)
  names: ASCII, lower case, '/' separators, relative to sound/ ("ambience/drips.wav"), padded to 4
  float32 samples[totalSamples]: mono PCM in [−1, 1] at the clip's own rate (AudioClip.Create + SetData; the Udon
               loader BlockCopies them, no per-sample loop). Rates above 22050 Hz are halved (box filter).
"""
import os, struct

DOOR_MOVE = {n: f"doors/doormove{n}.wav" for n in range(1, 11)}
DOOR_STOP = {n: f"doors/doorstop{n}.wav" for n in range(1, 9)}
# ButtonSound() [obs 0x8bb50: table @0x14c080 (vaddr), 26 entries, default button9]
BUTTON = (["common/null.wav"] + [f"buttons/button{n}.wav" for n in range(1, 12)]
          + ["buttons/latchlocked1.wav", "buttons/latchunlocked1.wav", "buttons/lightswitch2.wav"]
          + ["buttons/button9.wav"] * 6 + [f"buttons/lever{n}.wav" for n in range(1, 6)])
# CBasePlatTrain::Precache [obs 0xf4920] (func_train: CFuncTrain::Precache 0xf93c0, same table)
PLAT_MOVE = {1: "plats/bigmove1.wav", 2: "plats/bigmove2.wav", 3: "plats/elevmove1.wav", 4: "plats/elevmove2.wav",
             5: "plats/elevmove3.wav", 6: "plats/freightmove1.wav", 7: "plats/freightmove2.wav",
             8: "plats/heavymove1.wav", 9: "plats/rackmove1.wav", 10: "plats/railmove1.wav",
             11: "plats/squeekmove1.wav", 12: "plats/talkmove1.wav", 13: "plats/talkmove2.wav"}
PLAT_STOP = {1: "plats/bigstop1.wav", 2: "plats/bigstop2.wav", 3: "plats/freightstop1.wav", 4: "plats/heavystop2.wav",
             5: "plats/rackstop1.wav", 6: "plats/railstop1.wav", 7: "plats/squeekstop1.wav", 8: "plats/talkstop1.wav"}
# CFuncTrackTrain::Precache [obs 0xf4e30]
TTRAIN = {1: "plats/ttrain1.wav", 2: "plats/ttrain2.wav", 3: "plats/ttrain3.wav", 4: "plats/ttrain4.wav",
          5: "plats/ttrain6.wav", 6: "plats/ttrain7.wav"}
FANS = {n: f"fans/fan{n}.wav" for n in range(1, 6)}      # CFuncRotating::Precache [obs 0x84920]
FIXED = {
    "func_healthcharger": ("items/medshot4.wav", "items/medcharge4.wav", "items/medshotno1.wav"),   # [obs CWallHealth 0xc3aa0]
    "func_recharge": ("items/suitchargeok1.wav", "items/suitcharge1.wav", "items/suitchargeno1.wav"),  # [obs CRecharge::Precache]
    "func_mortar_field": ("weapons/mortar.wav", "weapons/mortarhit.wav"),                              # [obs CFuncMortarField::Precache]
    "env_spark": tuple(f"buttons/spark{n}.wav" for n in range(1, 7)),                                # [obs CEnvSpark::Precache 0x89550]
    # CRope::Precache: the three creak clips; CRopeSegment::Touch: the grab clip [obs opfor.so]
    "env_rope": ("items/rope1.wav", "items/rope2.wav", "items/rope3.wav", "items/grab_rope.wav"),
    "env_explosion": ("weapons/explode3.wav", "weapons/explode4.wav", "weapons/explode5.wav"),       # [ext: TE_EXPLOSION client sound]
    "func_pushable": ("debris/pushbox1.wav", "debris/pushbox2.wav", "debris/pushbox3.wav"),         # [obs NOTES §3.11]
    "monster_cockroach": ("roach/rch_die.wav", "roach/rch_walk.wav", "roach/rch_smash.wav"),          # [obs CRoach::Precache]
    # CHAssassin::Precache + RunAI: the two pistol waves, the cloak-start cue and the shared player footsteps
    "monster_human_assassin": ("weapons/pl_gun1.wav", "weapons/pl_gun2.wav", "debris/beamstart1.wav",
                               "player/pl_step1.wav", "player/pl_step2.wav", "player/pl_step3.wav",
                               "player/pl_step4.wav"),
    # Sven Co-op (S1): the xen portal wav (point_checkpoint.as precaches it for the same TE_C_XEN_PORTAL effect)
    # Opposing Force CTF: CTFGoalFlag::Precache's four flag cues (taken by each side, captured by each side), the damage powerup's victim ping
    "item_ctfflag": ("ctf/bm_flagtaken.wav", "ctf/soldier_flagtaken.wav", "ctf/civ_flag_capture.wav", "ctf/marine_flag_capture.wav"),   # [obs CTFGoalFlag::Precache]
    "item_ctfaccelerator": ("turret/tu_ping.wav", "ctf/itemthrow.wav"),                              # [obs CBasePlayer::TakeDamage, DropPlayerCTFPowerup]
    "env_xenmaker": ("debris/beamstart7.wav",),                                                      # [inf]
    "point_checkpoint": ("debris/beamstart7.wav",),                                                  # [ext point_checkpoint.as]
    # CBaseTurret::Precache [obs 0x136050] (shared by monster_turret / miniturret / sentry)
    "monster_turret": ("turret/tu_fire1.wav", "turret/tu_ping.wav", "turret/tu_active2.wav", "turret/tu_die.wav",
                       "turret/tu_die2.wav", "turret/tu_die3.wav", "turret/tu_deploy.wav", "turret/tu_spinup.wav",
                       "turret/tu_spindown.wav", "turret/tu_search.wav", "turret/tu_alert.wav"),
    "monster_miniturret": ("turret/tu_fire1.wav", "turret/tu_ping.wav", "turret/tu_active2.wav", "turret/tu_die.wav",
                           "turret/tu_die2.wav", "turret/tu_die3.wav", "turret/tu_deploy.wav", "turret/tu_spinup.wav",
                           "turret/tu_spindown.wav", "turret/tu_search.wav", "turret/tu_alert.wav"),
    "monster_sentry": ("turret/tu_fire1.wav", "turret/tu_ping.wav", "turret/tu_active2.wav", "turret/tu_die.wav",
                       "turret/tu_die2.wav", "turret/tu_die3.wav", "turret/tu_deploy.wav", "turret/tu_spinup.wav",
                       "turret/tu_spindown.wav", "turret/tu_search.wav", "turret/tu_alert.wav"),
    # CXenTree::Precache: the three hit sounds + two miss sounds
    "xen_tree": ("zombie/claw_strike1.wav", "zombie/claw_strike2.wav", "zombie/claw_strike3.wav",
                 "zombie/claw_miss1.wav", "zombie/claw_miss2.wav"),
}


def _n(prefix, nums, suffix=".wav"):
    return tuple(f"{prefix}{k}{suffix}" for k in nums)


# Monster voice / attack wavs, per classname (priority 3: after the ambient, mover and fixed sounds, so a tight SNDS budget drops
# them first and logs the dropped names). The lists are the original Precache tables [obs evidence/string_arrays.txt] plus the
# wavs GsvrHlMonsters plays by name (MonsterSound / PlayVoiceClip / HoundSnd ...); a monster class that is on the map brings its set.
# Sentence-based speech (HG_*, SC_*, BA_*, SLV_*) is the SENT section's business, not listed here.
_CLAW_HIT = _n("zombie/claw_strike", (1, 2, 3))
_CLAW_MISS = _n("zombie/claw_miss", (1, 2))
_ZOMBIE = (_n("zombie/zo_idle", (1, 2, 3, 4)) + _n("zombie/zo_pain", (1, 2)) + _n("zombie/zo_alert", (10, 20, 30))
           + _n("zombie/zo_attack", (1, 2)) + _CLAW_HIT + _CLAW_MISS)
_HEADCRAB = (_n("headcrab/hc_idle", (1, 2, 3)) + _n("headcrab/hc_alert", (1,)) + _n("headcrab/hc_pain", (1, 2, 3))
             + _n("headcrab/hc_die", (1, 2)) + _n("headcrab/hc_attack", (1, 2, 3)) + ("headcrab/hc_headbite.wav",))
_GRUNT = (_n("hgrunt/gr_pain", (1, 2, 3, 4, 5)) + _n("hgrunt/gr_die", (1, 2, 3)) + _n("hgrunt/gr_mgun", (1, 2))
          + ("hgrunt/gr_reload1.wav", "weapons/sbarrel1.wav", "weapons/glauncher.wav"))
_SLAVE = (_n("aslave/slv_pain", (1, 2)) + _n("aslave/slv_die", (1, 2)) + _CLAW_HIT + _CLAW_MISS
          + ("debris/zap4.wav", "weapons/electro4.wav"))
_AGRUNT = (_n("agrunt/ag_alert", (1, 3, 4, 5)) + _n("agrunt/ag_attack", (1, 2, 3)) + _n("agrunt/ag_die", (1, 4, 5))
           + _n("agrunt/ag_fire", (1, 2, 3)) + _n("agrunt/ag_idle", (1, 2, 3, 4)) + _n("agrunt/ag_pain", (1, 2, 3, 4, 5))
           + _CLAW_HIT + _CLAW_MISS + _n("hornet/ag_buzz", (1, 2, 3)) + _n("hornet/ag_hornethit", (1, 2, 3)))
_CONTROLLER = (_n("controller/con_alert", (1, 2, 3)) + _n("controller/con_attack", (1, 2, 3)) + _n("controller/con_die", (1, 2))
               + _n("controller/con_idle", (1, 2, 3, 4, 5)) + _n("controller/con_pain", (1, 2, 3)) + ("weapons/electro4.wav",))
_HOUND = (_n("houndeye/he_alert", (1, 2, 3)) + _n("houndeye/he_idle", (1, 2, 3)) + _n("houndeye/he_die", (1, 2, 3))
          + _n("houndeye/he_pain", (1, 3, 4, 5)) + _n("houndeye/he_attack", (1, 3)) + _n("houndeye/he_hunt", (1, 2, 3))
          + _n("houndeye/he_blast", (1, 2, 3)))
_SQUID = (("bullchicken/bc_acid1.wav",) + _n("bullchicken/bc_attack", (2, 3)) + ("bullchicken/bc_attackgrowl.wav",)
          + _n("bullchicken/bc_attackgrowl", (2, 3)) + _n("bullchicken/bc_bite", (2, 3)) + _n("bullchicken/bc_die", (1, 2, 3))
          + _n("bullchicken/bc_idle", (1, 2, 3, 4, 5)) + _n("bullchicken/bc_pain", (1, 2, 3, 4)) + _n("bullchicken/bc_spithit", (1, 2)))
_ICHY = (_n("ichy/ichy_die", (2, 4)) + _n("ichy/ichy_bite", (1, 2)) + _n("ichy/ichy_idle", (1, 2, 3, 4))
         + _n("ichy/ichy_pain", (2, 3, 5)) + _n("ichy/ichy_alert", (2, 3)) + _n("ichy/ichy_attack", (1, 2)))
_LEECH = _n("leech/leech_alert", (1, 2)) + _n("leech/leech_bite", (1, 2, 3))
_BARNACLE = (("barnacle/bcl_alert2.wav", "barnacle/bcl_bite3.wav") + _n("barnacle/bcl_chew", (1, 2, 3))
             + ("barnacle/bcl_die1.wav", "barnacle/bcl_die3.wav"))
_TENTACLE = (_n("tentacle/te_alert", (1, 2)) + _n("tentacle/te_move", (1, 2)) + _n("tentacle/te_roar", (1, 2))
             + _n("tentacle/te_search", (1, 2)) + _n("tentacle/te_sing", (1, 2)) + _n("tentacle/te_strike", (1, 2))
             + _n("tentacle/te_swing", (1, 2)) + _n("player/pl_dirt", (1, 2, 3, 4)) + _n("player/pl_slosh", (1, 2, 3, 4)))
_GARG = (_n("garg/gar_alert", (1, 2, 3)) + _n("garg/gar_attack", (1, 2, 3)) + _n("garg/gar_breathe", (1, 2, 3))
         + _n("garg/gar_idle", (1, 2, 3, 4, 5)) + _n("garg/gar_pain", (1, 2, 3)) + _n("garg/gar_step", (1, 2))
         + ("garg/gar_stomp1.wav", "garg/gar_flameoff1.wav", "garg/gar_flameon1.wav", "garg/gar_flamerun1.wav",
            "weapons/mine_charge.wav") + _CLAW_HIT + _CLAW_MISS)
_BIGMOMMA = (_n("gonarch/gon_alert", (1, 2, 3)) + _n("gonarch/gon_attack", (1, 2, 3)) + _n("gonarch/gon_birth", (1, 2, 3))
             + _n("gonarch/gon_childdie", (1, 2, 3)) + ("gonarch/gon_die1.wav",) + _n("gonarch/gon_pain", (2, 4, 5))
             + _n("gonarch/gon_sack", (1, 2, 3)) + _n("gonarch/gon_step", (1, 2, 3)) + _CLAW_HIT)
_NIHILANTH = (_n("x/x_attack", (1, 2, 3)) + ("x/x_ballattack1.wav", "x/x_die1.wav") + _n("x/x_laugh", (1, 2))
              + _n("x/x_pain", (1, 2)) + _n("x/x_recharge", (1, 2, 3)) + ("x/x_shoot1.wav", "x/x_teleattack1.wav"))
_BARNEY = ("barney/ba_attack2.wav",) + _n("barney/ba_pain", (1, 2, 3)) + _n("barney/ba_die", (1, 2, 3))
_SCIENTIST = _n("scientist/sci_pain", (1, 2, 3, 4, 5))
_APACHE = ("apache/ap_rotor2.wav", "apache/ap_rotor4.wav")
MONSTER_VOICES = {
    "monster_zombie": _ZOMBIE, "monster_zombie_barney": _ZOMBIE, "monster_zombie_soldier": _ZOMBIE,
    "monster_headcrab": _HEADCRAB, "monster_babycrab": _HEADCRAB,
    "monster_human_grunt": _GRUNT, "monster_human_grunt_ally": _GRUNT, "monster_human_medic_ally": _GRUNT,
    "monster_human_torch_ally": _GRUNT, "monster_grunt_repel": _GRUNT, "monster_grunt_ally_repel": _GRUNT,
    "monster_medic_ally_repel": _GRUNT, "monster_hwgrunt": _GRUNT + ("hassault/hw_shoot1.wav",),
    "monster_hwgrunt_repel": _GRUNT + ("hassault/hw_shoot1.wav",),
    "monster_alien_slave": _SLAVE, "monster_alien_grunt": _AGRUNT, "monster_alien_controller": _CONTROLLER,
    "monster_houndeye": _HOUND, "monster_bullchicken": _SQUID, "monster_ichthyosaur": _ICHY, "monster_leech": _LEECH,
    "monster_barnacle": _BARNACLE, "monster_tentacle": _TENTACLE, "monster_gargantua": _GARG, "monster_babygarg": _GARG,
    "monster_bigmomma": _BIGMOMMA, "monster_nihilanth": _NIHILANTH, "monster_barney": _BARNEY, "monster_scientist": _SCIENTIST,
    "monster_cleansuit_scientist": _SCIENTIST, "monster_apache": _APACHE,
}
# keys of the spawner entities that name a monster class (monstermaker / squadmaker / env_xenmaker / trigger_createentity)
MAKER_KEYS = {"monstermaker": "monstertype", "squadmaker": "monstertype", "env_xenmaker": "monstertype",
              "trigger_createentity": "m_iszCreateClassname"}
# CBaseMonster generic animation events (body drop 2001 / 2002, claw miss 2010) of every monster class.
MONSTER_COMMON = ("common/bodydrop3.wav", "common/bodydrop4.wav", "zombie/claw_miss2.wav")
MAX_RATE = 22050
# Model animation events that carry a sound name in their options string: CBaseMonster::HandleAnimEvent 1004 (EMIT_SOUND_DYN
# CHAN_BODY, vol 1, attn 2.0, pitch 100) and 1008 (CHAN_VOICE, same) [obs CBaseMonster::HandleAnimEvent 0xd8af0], plus the
# Opposing Force 1011..1013 channel events. The wavs sit in the model's sequences (scripted sequences, npc steps, bust sounds), so
# the map pack reads them from the monster's .mdl at pack time (model_event_sounds); the MON1 event records already carry the names.
MODEL_SOUND_EVENTS = (1004, 1008, 1011, 1012, 1013)
# class → default studio model (basename in models/) of the monsters whose sequences carry sound events; monster_generic and
# every other class that names its model uses the entity's `model` key instead.
MONSTER_MODEL = {
    "monster_scientist": "scientist", "monster_sitting_scientist": "scientist", "monster_barney": "barney",
    "monster_human_grunt": "hgrunt", "monster_hwgrunt": "hgrunt", "monster_alien_grunt": "agrunt", "monster_bullchicken": "bullsquid",
    "monster_gargantua": "garg", "monster_alien_slave": "islave", "monster_zombie": "zombie", "monster_gman": "gman",
    "monster_headcrab": "headcrab", "monster_houndeye": "houndeye", "monster_alien_controller": "controller",
    "monster_bigmomma": "big_mom", "monster_nihilanth": "nihilanth", "monster_barnacle": "barnacle", "monster_tentacle": "tentacle",
    "monster_ichthyosaur": "icky", "monster_leech": "leech", "monster_apache": "apache",
}


def button_sound(v):
    """ButtonSound(n): table entry, out of range → buttons/button9.wav."""
    n = _int(v)
    return BUTTON[n] if 0 <= n < len(BUTTON) else "buttons/button9.wav"


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def norm(rel):
    """Engine-style sound name → our key: lower case, '/' separators, no leading '/' or 'sound/'."""
    r = (rel or "").strip().replace("\\", "/").lstrip("/*").lower()      # a leading '*' marks a streamed wav; the engine drops it
    if r.startswith("sound/"):
        r = r[6:]
    return r


def model_event_sounds(path, ids=MODEL_SOUND_EVENTS):
    """Wav names in the sound events (ids) of a studio model's sequences, in file order, normalised (norm). [] when the
    file is missing / not an IDST v10 model. Layout as tools/mdl10.py: header nseq / seqidx at 164 / 168, 176-byte sequence
    records (events count / index at +48 / +52), 76-byte events (frame, event, type, options[64])."""
    try:
        with open(path, "rb") as fh:
            d = fh.read()
        if d[:4] != b"IDST" or struct.unpack_from("<i", d, 4)[0] != 10:
            return []
        nseq, seqidx = struct.unpack_from("<2i", d, 164)
        out = []
        for i in range(nseq):
            nev, evi = struct.unpack_from("<2i", d, seqidx + i * 176 + 48)
            for e in range(nev):
                eo = evi + e * 76
                _fr, ev, _ty = struct.unpack_from("<3i", d, eo)
                if ev not in ids:
                    continue
                opt = d[eo + 12:eo + 76].split(b"\0", 1)[0].decode("ascii", "replace")
                n = norm(opt)
                if n.endswith(".wav") and n not in out:
                    out.append(n)
        return out
    except (OSError, struct.error):
        return []


def find_model(game_root, mod, name):
    """<content folder>/models/<name>.mdl in gamedirs order (case-insensitive file name), else None."""
    from gamedirs import content_dirs
    base = norm(name)
    if base.startswith("models/"):
        base = base[7:]
    if not base.endswith(".mdl"):
        base += ".mdl"
    for root in content_dirs(game_root, mod):
        d = os.path.join(root, "models")
        p = os.path.join(d, *base.split("/"))
        if os.path.isfile(p):
            return p
        if os.path.isdir(d) and "/" not in base:
            for f in os.listdir(d):
                if f.lower() == base:
                    return os.path.join(d, f)
    return None


def wanted(ents, mod=None, game_root=None):
    """[(name, priority)] in first-use order; priority 0 ambient_generic / env_message, 1 movers, 2 the rest, 3 monsters.
    game_root: also the sound events (1004 / 1008 / OF 1011..1013) of every monster model on the map, read from its .mdl.
    mod "dmc": trigger_teleport without spawnflag 2 hums ambience/hum1.wav (CTriggerTeleport::Spawn, DMC only)."""
    out = {}
    seen_models = set()

    def add(n, pri):
        n = norm(n)
        if not n or n.startswith("!") or n == "common/null.wav" or not n.endswith(".wav") or ".." in n.split("/"):
            return
        if n not in out or pri < out[n]:
            out[n] = pri
    for e in ents:
        c = e.get("classname", "")
        if c == "ambient_generic":
            add(e.get("message", ""), 0)
        elif c == "trigger_teleport" and mod == "dmc" and not (_int(e.get("spawnflags")) & 2):
            add("ambience/hum1.wav", 0)
        elif c == "env_message":
            add(e.get("messagesound", ""), 0)
        elif c in ("func_door", "func_door_rotating", "momentary_door"):
            add(DOOR_MOVE.get(_int(e.get("movesnd")), ""), 1)
            add(DOOR_STOP.get(_int(e.get("stopsnd")), ""), 1)
            for k in ("locked_sound", "unlocked_sound"):
                if _int(e.get(k)):
                    add(button_sound(e.get(k)), 2)
        elif c in ("func_button", "func_rot_button", "momentary_rot_button"):
            add(button_sound(e.get("sounds", "0")), 1)
            for k in ("locked_sound", "unlocked_sound"):
                if _int(e.get(k)):
                    add(button_sound(e.get(k)), 2)
        elif c in ("func_plat", "func_platrot", "func_train", "func_trackchange", "func_trackautochange"):
            add(PLAT_MOVE.get(_int(e.get("movesnd")), ""), 1)
            add(PLAT_STOP.get(_int(e.get("stopsnd")), ""), 1)
        elif c == "func_tracktrain":
            if _int(e.get("sounds")) in TTRAIN:
                add(TTRAIN[_int(e.get("sounds"))], 1)
            add("plats/ttrain_start1.wav", 1)
            add("plats/ttrain_brake1.wav", 1)
        elif c == "func_rotating":
            add(e.get("message") or FANS.get(_int(e.get("sounds")), ""), 1)
        elif c in ("func_tank", "func_tanklaser", "func_tankrocket", "func_tankmortar"):
            add(e.get("rotatesound", ""), 1)
        elif c in FIXED:
            for n in FIXED[c]:
                add(n, 2)
            if c == "point_checkpoint":                    # activation sound = `message`, default beamstart4 [ext]
                add(e.get("message") or "debris/beamstart4.wav", 2)
        if c.startswith("monster_"):
            for n in MONSTER_COMMON:
                add(n, 2)
        spawned = c if c in MONSTER_VOICES else ""
        if c in MAKER_KEYS:
            spawned = (e.get(MAKER_KEYS[c], "") or "").strip().lower()
        for n in MONSTER_VOICES.get(spawned, ()):
            add(n, 3)
        if game_root and (c.startswith("monster_") or c in MAKER_KEYS):
            mdl = (e.get("model") or "").strip() if c.startswith("monster_") else ""
            mdl = mdl if mdl.lower().endswith(".mdl") else MONSTER_MODEL.get(spawned, "")
            if mdl and mdl not in seen_models:
                seen_models.add(mdl)
                p = find_model(game_root, mod, mdl)
                for n in (model_event_sounds(p) if p else ()):
                    add(n, 3)
        if c == "func_breakable" and _int(e.get("explodemagnitude")) > 0:
            for n in FIXED["env_explosion"]:
                add(n, 2)
    return sorted(out.items(), key=lambda kv: kv[1])     # stable: first use within a priority


def read_wav(data):
    """RIFF PCM 8/16-bit → (rate, mono float list, loopStart, loopEnd, bits). loopStart −1 = not looped."""
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE file")
    fmt = None
    pcm = None
    cue = -1
    ltxt = -1
    o = 12
    while o + 8 <= len(data):
        cid, size = data[o:o + 4], struct.unpack_from("<I", data, o + 4)[0]
        body = data[o + 8:o + 8 + size]
        if cid == b"fmt ":
            fmt = struct.unpack_from("<HHIIHH", body, 0)
        elif cid == b"data":
            pcm = body
        elif cid == b"cue " and len(body) >= 28 and cue < 0:
            if struct.unpack_from("<I", body, 0)[0] > 0:
                cue = struct.unpack_from("<I", body, 24)[0]      # first cue point: dwSampleOffset
        elif cid == b"LIST" and body[:4] == b"adtl":
            p = 4
            while p + 8 <= len(body):
                sid, ssz = body[p:p + 4], struct.unpack_from("<I", body, p + 4)[0]
                if sid == b"ltxt" and ssz >= 12 and body[p + 16:p + 20] == b"mark":
                    ltxt = struct.unpack_from("<I", body, p + 12)[0]
                p += 8 + ssz + (ssz & 1)
        o += 8 + size + (size & 1)
    if fmt is None or pcm is None:
        raise ValueError("no fmt/data chunk")
    tag, ch, rate, _br, _al, bits = fmt
    if tag != 1 or bits not in (8, 16) or ch < 1:
        raise ValueError(f"unsupported wav (format {tag}, {bits} bit, {ch} ch)")
    if bits == 8:
        vals = [(b - 128) / 128.0 for b in pcm]
    else:
        n = len(pcm) // 2
        vals = [v / 32768.0 for v in struct.unpack_from(f"<{n}h", pcm, 0)]
    if ch > 1:
        vals = [sum(vals[i:i + ch]) / ch for i in range(0, len(vals) - ch + 1, ch)]
    n = len(vals)
    end = n
    if cue >= 0 and ltxt > 0:
        end = min(n, cue + ltxt)
    if cue >= n:
        cue = -1
    return rate, vals, cue, end, bits


def halve(vals):
    return [(vals[i] + vals[i + 1]) * 0.5 for i in range(0, len(vals) - 1, 2)]


def find(game_root, mod, rel):
    """<content folder>/sound/<rel> in gamedirs order (<mod>_addon, <mod>, <mod>_downloads, valve), else None."""
    from gamedirs import content_dirs
    for root in content_dirs(game_root, mod):
        p = os.path.join(root, "sound", *rel.split("/"))
        if os.path.isfile(p):
            return p
        # case-insensitive fallback (Linux/macOS installs keep the original case)
        d = os.path.join(root, "sound")
        parts = rel.split("/")
        ok = True
        for part in parts:
            if not os.path.isdir(d):
                ok = False
                break
            hit = [f for f in os.listdir(d) if f.lower() == part]
            if not hit:
                ok = False
                break
            d = os.path.join(d, hit[0])
        if ok and os.path.isfile(d):
            return d
    return None


def load(ents, game_root, mod, max_bytes=24 * 1024 * 1024, max_seconds=60.0, log=print):
    """→ list of (name, rate, samples, loopStart, loopEnd, bits) within the byte budget (ambient first)."""
    if not game_root:
        return []
    clips, used, missing, skipped = [], 0, [], []
    for name, _pri in wanted(ents, mod, game_root):
        p = find(game_root, mod, name)
        if not p:
            missing.append(name)
            continue
        try:
            with open(p, "rb") as fh:
                rate, vals, ls, le, bits = read_wav(fh.read())
        except (ValueError, struct.error, OSError) as e:
            log(f"sound {name}: unreadable ({e})")
            continue
        while rate > MAX_RATE:
            vals = halve(vals)
            rate //= 2
            ls = ls // 2 if ls >= 0 else -1
            le //= 2
        lim = int(max_seconds * rate)
        if len(vals) > lim:
            vals = vals[:lim]
        le = min(le, len(vals))
        if ls >= le:
            ls = -1
        if used + 4 * len(vals) > max_bytes:
            skipped.append(name)
            continue
        used += 4 * len(vals)
        clips.append((name, rate, vals, ls, le, bits))
    loops = sum(1 for c in clips if c[3] >= 0)
    log(f"sounds: {len(clips)} wavs ({loops} with loop points), {used / 1e6:.2f} MB"
        + (f", {len(missing)} missing ({', '.join(missing[:6])}{' …' if len(missing) > 6 else ''})" if missing else "")
        + (f", {len(skipped)} over the {max_bytes / 1e6:.0f} MB budget, dropped (last = lowest priority): "
           f"{', '.join(skipped[:12])}{' ...' if len(skipped) > 12 else ''}" if skipped else ""))
    return clips


def section(clips):
    """SNDS blob (see module doc)."""
    names = b""
    recs = b""
    base = 0
    for name, rate, vals, ls, le, bits in clips:
        nb = name.encode("ascii", "replace")
        recs += struct.pack("<8i", rate, len(vals), base, ls, le, len(names), len(nb), bits)
        names += nb
        base += len(vals)
    names += b"\0" * (-len(names) % 4)
    head = struct.pack("<4i", len(clips), len(names), base, 0)
    samples = b"".join(struct.pack(f"<{len(c[2])}f", *c[2]) for c in clips)
    return head + recs + names + samples
