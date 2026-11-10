# Builds a GSM1 weapon + player model + sound pack for the VRChat loader (unity/Assets/GoldSrcVR/Scripts/GsvrModelLoader.cs)
# from the player's OWN game install (companion route /pack/models.bin). Stdlib only (runs inside the companion).
# usage: python3 -I modelpack.py --game-root input/Half-Life --mod cstrike|valve|svencoop|gearbox|bshift|ricochet|dmc out.bin
#        optional: --no-view (skip v_ models), --max-atlas-px N (texel budget, default 4096*2048), --max-clip-s 6,
#                  --players default (cstrike terror,urban; valve gordon,helmet; 'all', 'none' or a comma list),
#                  --player-classes all (or a subset of the mod's classes, e.g. ak47,rifle,mp5,onehanded,carbine),
#                  --monsters all (valve/svencoop: the MONSTERS models of the GSX1 MON1 chunk; 'none' or a list)
#        python3 -I modelpack.py --version
# Mods (PROFILES): cstrike = CS weapons, CS player models, sprites. valve = HL weapons (iId 1..15) + item pickups
# (16..31, world mesh only), HL multiplayer player models with the same PAnim records, monsters (MON1). svencoop =
# the valve tables from the SD sets models/hlclassic + models/opfor, plus the Sven weapons 32..44, pickups 37 / 48..52 and
# 19 Sven monsters (S2, docs/sven_world.md §8.4 / §8.7). Check: tools/valvepack_check.py (--mod svencoop).
#
# ── GSM1 layout ── little endian; every section starts 4-byte aligned (all section sizes are multiples of 4)
#   header (v1: 64 bytes, v2: 80 bytes; int32) — this builder writes v2; the loader accepts both
#    0  char[4] "GSM1"          4  headerBytes = 64 (v1) / 80 (v2)          8  version = 1 / 2
#   12  weaponCount W          16  meshCount M              20  soundCount S          24  eventCount E
#   28  uvVertexCount U        (sum of vertexCount over meshes; uv array holds U*2 floats)
#   32  indexCount I           (sum of indexCount over meshes)
#   36  poseVertexCount P      (sum of vertexCount*poseCount; pos/normal arrays hold P*3 floats each)
#   40  sampleCount N          (float32 samples of all sounds)
#   44  atlasW   48 atlasH     (multiples of 8)            52  atlasMips (levels stored, level l = (W>>l)x(H>>l))
#   56  nameBytes              (string table length, ASCII, padded to 4)          60  flags (bit0 = view meshes)
#   v2 only:
#   64  playerCount Q          68  playerAnimCount A        72  playerPoseCount H (sum of poseCount over player meshes)
#   76  reserved 0
#   sections, in this order (the v1 sections first, unchanged; offsets start at headerBytes):
#   int32   weapon [W*24]   0 weaponId (CS id)  1 meshP  2 meshW  3 meshV (mesh index or -1)
#                           4..13 sound index per kind (-1 = none): 0 fire 1 fire2 2 empty 3 draw 4 reload1
#                                 5 reload2 6 reload3 7 special1 8 special2 9 special3 (see KINDS below)
#                           14 eventFirst 15 eventCount 16 nameOff 17 nameLen (short name, e.g. "ak47")
#                           18 flags: bit0 muzzleP from an MDL attachment (else estimated from the mesh),
#                                 bit1 muzzleV from an attachment, bit2 P mesh levelled (barrel → +Z)
#                           19 v2: player upper-body anim class (index into PLAYER_CLASSES, -1 none; v1: 0)
#                           20..23 reserved 0
#   float32 weaponF [W*8]   0..2 muzzle of P (weapon frame, m)  3..5 muzzle of V (view frame, m)
#                           6 reload anim seconds (v_ model)   7 draw anim seconds
#   int32   mesh [M*8]      0 vertexCount V  1 indexCount I  2 poseCount (1 for P/W, 4 for V, all poses for a player)
#                           3 uvBase (vertex offset into uv)  4 idxBase (offset into idx)
#                           5 poseBase (vertex offset into pos/nrm; pose k of the mesh = poseBase + k*V)
#                           6 kind (0 P held, 1 W world, 2 V view, 3 player body, 4 monster part) 7 weaponId (kind 3:
#                           player index, kind 4: monster index); kinds 3 and 4 may have up to 1024 poses, others ≤ 16
#   int32   sound [S*6]     0 sampleRate 1 sampleCount 2 sampleBase (float offset into samples) 3 channels (1)
#                           4 nameOff 5 nameLen (e.g. "weapons/ak47-1.wav"); the table also carries the player
#                           sounds (PLAYER_SOUNDS: "player/pl_step1.wav" …, no weapon record points at them), which
#                           the loader looks up by this name (GetClipByName)
#   int32   event [E*4]     0 anim (0 reload, 1 draw, 2 attack)  1 time in ms from anim start  2 sound index
#                           3 frame (v_ model sound events, e.g. AK reload: clipout @ 351 ms, clipin @ 1541 ms)
#   int32   idx [I]         triangle list, LOCAL to its mesh (0..V-1), Unity front-face winding
#   float32 uv  [U*2]       atlas UV in [0,1]; v = atlas row / atlasH (row 0 = first row of the atlas data)
#   float32 pos [P*3]       metres, Unity axes (see FRAMES)
#   float32 nrm [P*3]       unit normals, same axes
#   float32 samples [N]     mono PCM in [-1, 1] at the sound's own rate (AudioClip.Create + SetData)
#   uint8   atlas RGBA32, mip levels 0..atlasMips-1 concatenated (Unity raw layout for
#                           Texture2D(W, H, RGBA32, mips, linear=false)); 8 px edge-extended gutter per texture
#   uint8   names [nameBytes]
#   v2 only, after names:
#   int32   player [Q*8]    0 team (0 T, 1 CT)  1 mesh index (kind 3)  2 animFirst  3 animCount  4 nameOff 5 nameLen
#                           6 handBase (row of pose 0 in hand[])  7 reserved 0
#   int32   panim [A*4]     0 anim (A_* below: 0 idle 1 walk 2 run 3 crouch_idle 4 crouch_walk 5 shoot 6 crouch_shoot
#                           7 death 8 crouch_death)  1 class (PLAYER_CLASSES index; -1 = any, deaths)
#                           2 poseFirst (pose index inside the player mesh)  3 frameCount
#   float32 panimF [A*4]    0 cycle distance in GoldSrc units (gait: linearmovement of the leg sequence; 0 = time based)
#                           1 seconds (gait: cycle at the source fps; death: time until the end pose)  2 loop (1/0)  3 0
#   float32 hand [H*8]      per player pose: "Bip01 R Hand" in the body frame: pos xyz (m), rotation quat xyzw, 0
#   float32 grip [W*4]      per weapon: quat xyzw, P-mesh frame → hand bone frame (inverse of the barrel levelling)
#                           gun in the body frame: localPosition = hand pos, localRotation = hand rot * grip
#   optional GSX1 extension block at the END of the names region (nameBytes includes it, so older loaders still
#   validate the size and just ignore it): chunks + trailer int32 blockBytes, "GSX1" — layout in tools/spr.py.
#   Chunk "SPR1" = sprites (SPRITES below: the smoke grenade cloud gas_puff_01 + black_smoke1-4) as RGBA frames
#   in their own atlas; --no-sprites leaves the block out (byte-identical to the pre-sprite packs).
#   Chunk "SPR2" = effect sprites (FX_SPRITES + the sprites the mod's maps reference, FX_TEXELS budget) shelf-packed into a second
#   atlas (layout in tools/spr.py); --no-sprites leaves it out too. Sprites only ever come from the player's own install.
#   Chunk "HUD1" = the Half-Life client HUD art (valve / gearbox / bshift [+ dmc / ricochet when they ship a hud.txt]): the 640 sprite rects of
#   hud.txt + weapon_*.txt (numbers, cross, suit, flashlight, damage icons, bucket + weapon + ammo icons ...) over one 8-bit atlas of the
#   sprite sheets, pain arrows, train frames (layout in tools/hudpack.py). --no-sprites leaves it out; older loaders skip it (text HUD).
#   Chunk "GIB1" = gib models (GIB_MODELS + the maps' gibmodel / shootmodel models): int32 N, int32 P, N x int32[4] (nameOff, nameLen,
#   pieceFirst, pieceCount), P x int32[2] (mesh index or -1, vertex count). A piece = one model of bodypart 0 (the CGib `body` value
#   selects it), a kind-6 mesh with one pose in model space (frame like the W meshes). --no-gibs leaves it out.
#   Chunks "VAN1" / "VGUN" / "VARM" / "VSIL" / "VSKY" = smooth v_ anims, VR view rigs, arm vertices, CS silencer part meshes, silencer bone pose per v_ pose / anim key (layouts at their writer in write()).
#   Chunk "PGRP" (valve / svencoop only) = p_ grip points + grip sources per held weapon (layout at its writer in write()); the
#   v_ grip itself (VGUN) is the trigger hand's wrist: Builder.grip_pick (hand-named bone in view with arm vertices → glove
#   cluster → arm-less hand bone → gun centre), the muzzle Builder.muzzle_pick. tools/gripcheck.py lists / asserts them.
#   Chunk "MON1" = monsters (--mod valve/svencoop; MONSTERS below), all int32 / float32, payload:
#     int32 [8]          monsterCount MC, animCount MA, eventCount ME, partCount MP, keyCount MK, 0, 0, 0
#     int32 mon [MC*12]  0 nameOff 1 nameLen ("scientist" = models/<name>.mdl) 2 animFirst 3 animCount 4 partFirst
#                        5 partCount 6 default body value 7 skin (0) 8 poseCount (every part mesh has these poses)
#                        9..11 0
#     int32 part [MP*6]  one row per kept bodypart MODEL (alternatives over MON_PART_VERTS are left out → the loader
#                        falls back to the default body's model): 0 bodypart 1 model 2 mesh (kind 4; −1 = blank) 3 base
#                        4 models in that bodypart (model = (body / base) % models) 5 source vertex count
#     int32 anim [MA*12] one MDL sequence: 0 activity (hl.so Activity enum; 0 = only by name) 1 actweight
#                        2 nameOff 3 nameLen (sequence label) 4 poseFirst (pose of key 0 in the part meshes)
#                        5 keyCount 6 keyFirst (into keys) 7 eventFirst 8 eventCount 9 flags (bit0 = looping)
#                        10 numframes 11 sequence index in the .mdl
#     float anim2 [MA*4] 0 fps 1 seconds of one cycle = (numframes − 1) / fps 2 ground speed u/s = |linearmovement|
#                        · fps / (numframes − 1) [obs GetSequenceInfo 0x7b060] 3 |linearmovement| (units per cycle)
#     float keys [MK]    key time in s from the anim start (= frame / fps); key k = pose poseFirst + k
#     int32 event [ME*4] 0 frame (time = frame / fps) 1 event id (1003 fire target, 1004/1008 sound, 2001 body drop,
#                        monster-specific 1..20 …) 2 optOff 3 optLen (options string, e.g. the sound name)
#   Chunk "MLM1" (right after MON1; scriptanim2) = int32 MA (must equal MON1's animCount), float32 [MA*3] per MON1 anim record: the sequence's
#     linearmovement vector (x, y, z, GoldSrc units per cycle, model frame; anim2[3] is its length). The loader uses it for the post-script
#     root move (CBaseMonster::CineCleanup moves the actor to bone 0 at the end of the play: the vector rotated by the actor's yaw);
#     a pack without MLM1 (older, PACK_REV below cstrike 5 / valve 11 / svencoop 8 / gearbox 15 / bshift 9) falls back to anim2[3] along the yaw.
#   Chunk "SKL1" = skinned monsters (default; --no-skel writes the old baked MON1 only). The MON1 tables stay (records, key times,
#   events) but every part mesh has ONE pose (the reference = key 0 of the monster's first record) and a record's poses
#   poseFirst.. are rows of a float bone texture (tools/studioskel.py: per key and bone a quaternion + origin texel, per controller
#   slot an axis texel). An old loader sees part meshes with 1 pose != MON1 poses and skips the monsters (placeholders); the new
#   loader skins them in GoldSrcVR/Model (_SkOn). Payload, int32 / float32:
#     int32 [16]          version 1, model rows NS, texture W, H, mesh rows NMR, controller rows NC, attachment rows NA, anim rows NR
#                         (= MON1 anim count), bone masks NBM, vertices NV, skin rows NK, skin uv floats NU, 0 x 4
#     int32 model [NS*16] kind (0 = MON1 monster), owner (MON1 monster index), bones NB, stride (2 NB + slots), texel of key 0, keys
#                         (= MON1 poses), ctlFirst, ctlCount, attFirst, attCount, maskFirst, meshFirst, meshCount, animFirst, animCount, slots
#     float modelF [NS*8] bounds centre xyz, extents xyz (model frame, m), 0, 0
#     int32 mesh [NMR*4]  GSM1 mesh index, model row, vertFirst, vertCount (= the mesh's vertex count, same order)
#     int32 ctl [NC*4]    bone, type (XR 8 / YR 16 / ZR 32, | 0x8000 RLOOP), entity controller (0..3, 4 = mouth), slot; float ctlF
#                         [NC*4] start, end (degrees), 0, 0. Slots are sorted deepest bone first (the order the shader applies them)
#     int32 att [NA*2]    bone, 0; float attF [NA*4] origin in the bone frame (Unity axes, m), 0
#     int32 anim [NR*4]   blend-1 pose first (numblends 2 sequences: the second pose set, same key times; -1 none), blend type, 0, 0;
#                         float animF [NR*4] blend start, end (degrees), 0, 0
#     int32 mask [NBM]    per bone: bit s = controller slot s moves it (256 = gait leg bone)
#     int32 vbone [NV]; float vert [NV*6] per vertex of the skinned meshes: bone, position + normal in that bone's frame (Unity axes)
#     int32 skin [NK*4]   mesh, skin family, uvFirst (float index), vertices; float skinUv [NU] alternate atlas UVs of that family
#     float tex [W*H*4]   the bone texture (RGBAFloat rows, texel i at (i % W, i / W))
#   Monster frame = W frame (model space at angles 0, origin = entity origin, GoldSrc +x → Unity +Z): place with
#   Quaternion.Euler(0, 90 − yaw, 0). Keys are picked like VAN1 (pick_keys, ≤ MON_KEYS per activity, MON_TOL units of
#   bone-position error); linear-movement sequences play in place (the motion bone's x/y/z is zeroed, mdl10).
#
# FRAMES (all right/up/forward = Unity +X/+Y/+Z, metres = GoldSrc units × 0.0254). The map converter maps GoldSrc
# (x, y, z) → Unity (x, z, y); weapons use the same mapping rotated by -90° yaw so that the GoldSrc model forward (+x)
# becomes Unity +Z: Unity = (-y, z, x) · 0.0254 (a proper rotation of the map convention, same winding rule).
#   P (held, p_*.mdl): reference pose (frame 0 of "idle"), expressed in the player model's "Bip01 R Hand" bone frame
#     (origin = wrist pivot of the player model, i.e. exactly how CS attaches it to the hand); guns are then
#     rotated about the origin so the barrel axis (PCA of the front 40 % of the vertices) points along +Z.
#     Only geometry bound to the right-hand bone subtree is kept (drops the left Elite).
#   W (world, w_*.mdl): model space at angles 0 (lying on the floor, origin on the ground).
#     In a map: rotation = Quaternion.Euler(0, 90 - yaw, 0) for a GoldSrc entity yaw (same as the spawn code).
#   V (view, v_*.mdl): view space (origin = eye, +Z = view direction), arms included, mirrored to right-handed for
#     cstrike (CS stores its v_ models left-handed; the CS client mirrors them by default), 4 poses:
#     0 idle, 1 attack (shoot frame 2 / knife slash / grenade throw / C4 press), 2 reload start (frame of the first
#     reload sound = magazine out), 3 reload end (frame of the last reload sound = magazine in); see VIEW_POSES.
#     Choice: baked key poses instead of bone tracks — Udon has no skinned-mesh API worth using at runtime, and VR
#     players hold P models; V is only for desktop first person, where pose swaps + the script's recoil kick read fine.
#   Player (models/player/<name>/<name>.mdl, v2): body frame = feet at the origin (GoldSrc origin − 36 units standing,
#     − 18 crouched: the hull centre), +Z = facing (GoldSrc +x), same (-y, z, x) mapping; place at the feet with
#     Quaternion.Euler(0, yaw, 0). Each pose = CS's two-sequence player blend: legs (Mdl.gait_bones: root, pelvis,
#     thighs …) from the gait sequence (idle1 / walk / run / crouch_idle / crouchrun), the rest from the upper-body
#     sequence of the weapon class (ref_/crouch_ aim/shoot_<class>, blend 4 of the 3x3 aim grid = straight ahead).
#     Baked per class: idle 1, walk 6, run 8, crouch idle 1, crouch walk 6, shoot 1, crouch shoot 1 frame(s); per
#     model: each distinct death sequence (this CS release ships death1 … forward as identical data → 1) and
#     crouch_die, 4 frames each (3 falling + end pose). See CLASS_ANIMS, docs/models_sounds.md "Player models".
#
# Size limits: the atlas texel budget (--max-atlas-px, default 4096×2048 + mips ≈ 45 MB) halves the largest v_
# textures until everything fits; clips longer than --max-clip-s (6 s) are cut with a 50 ms fade. All CS weapons
# with view models: ≈ 2.5 MB geometry + ≈ 3 MB sound + atlas → see the printed size (well under 100 MB).
# Player models: ≈ 4.2 MB geometry + ≈ 1.4 MB atlas each with all 11 classes (terror + urban: 41.3 → 52.5 MB;
# all 8: 90.9 MB — too close to the 100 MB cap, use --player-classes to trim: 5 classes → 72 MB).
# valve (local install): 30 weapons/items, gordon + helmet, 11 monsters (the 10 H4 models + the roach; 153 anims,
# 22.4 MB pose geometry for the H4 set) → about 59 MB.
import argparse, array, hashlib, math, os, struct, sys, time, wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mdl10 import Mdl, xform, rotate, mat_mul   # noqa: E402
import spr                              # noqa: E402
import hudpack                          # noqa: E402
import studioskel                       # noqa: E402
import bsp30                            # noqa: E402

VERSION = "gsm1-2.7"      # 2.1: valve / svencoop VR grips (hand pick, glove clusters, muzzle pick) + GSX1 PGRP chunk; header version stays 2
                          # 2.2: cstrike v_: silencer part moved out of the v_ mesh (parked 45 cm behind the USP in unsil poses) into a VSIL bone-frame mesh, knife / grenade hand
                          #      next to the gun (was the other hand), muzzle on the barrel end of the unsilenced USP / M4A1
                          # 2.3: cstrike USP / M4A1: GSX1 VSKY chunk (silencer bone pose per v_ pose / anim key, so the silencer follows its own
                          #      bone) + view anims 6 / 7 (add_silencer / detach_silencer) with their sound events; the silencer mesh is now in the
                          #      frame of the silencer bone (was the gun bone). Header version stays 2; older loaders skip VSKY and anims 6 / 7
                          # 2.4: valve: monsters with no activity-matching sequence (floater: only "idle1", activity 0) keep sequence 0 (was 0 anims →
                          #      the loader rejected the whole MON1 chunk: no monsters at all). Header version stays 2
                          # 2.5: GSX1 SPR2 chunk (shelf-packed effect sprites: explosions, muzzle flashes, blood, beams, glows, map sprites; all mods).
                          #      Header version stays 2; older loaders skip SPR2 (placeholder effects)
                          # 2.6: GSX1 GIB1 chunk (real gib models: hgibs / agibs / germangibs + the func_breakable material gibs + the gibmodel /
                          #      shootmodel models the mod's maps name; kind-6 meshes, one per gib piece). Header version stays 2; older loaders
                          #      skip GIB1 (cube gibs). The monster voice wavs are NOT here: they go through the map pack's SNDS (tools/sounds.py)
                          # 2.7: valve / svencoop VR grip: the hand that CARRIES the gun (hand-bone ancestor of the gun bone) is the grip even when it
                          #      owns no arm vertices (shotgun, egon, rpg were held by the pump / support hand 25-35 cm away), two-handed objects
                          #      (tripmine) by the midpoint of both hands. Only the VGUN grip values / PGRP v_ source change; layout stays 2
VERSION_NUM = 2
# Per-mod pack revision (the companion's model cache key = VERSION + this, see version_for_mod / companion/models.py).
# Bump PACK_REV[mod] whenever a change makes THAT mod's pack output differ (profile table, shared code that the mod uses,
# VERSION bump ...); a change that only touches another mod's table / code leaves it alone, so the players' cached pack of
# this mod is reused after a companion update. tools/modelpack_rev_check.py builds every pack and FAILS when the output
# changed but the rev did not (and `--update` records the new golden tools/modelpack_rev.json).
PACK_REV = {"cstrike": 5, "valve": 12, "svencoop": 9, "gearbox": 19, "bshift": 10, "ricochet": 4, "dmc": 2}   # gearbox 19: vgibs gib model + ammo_spore world mesh spore_ammo.mdl (MODEL_STANDINS); 18: sniper (v_m40a1 firelastround/reload2/reload3 + events) + OF ally/medic/torch acts 65/51/10/21/22


def version_for_mod(mod):
    """Cache version of one mod's pack: '<VERSION>-<mod>-r<PACK_REV>' (no source hash: the rev table is the contract)."""
    return f"{VERSION}-{mod}-r{PACK_REV.get(mod, 0)}"


def source_version():
    """Old global converter version (VERSION + sha1 over the converter sources): any edit changes it. Plain `--version`."""
    h = hashlib.sha1()
    for fn in ("modelpack.py", "mdl10.py", "spr.py", "studioskel.py"):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), fn), "rb") as fh:
            h.update(fh.read())
    return f"{VERSION}-{h.hexdigest()[:12]}"


INCH = 0.0254
# grip source texts -> codes of the PGRP chunk (GsvrModelLoader.GripSourceName decodes them); prefix match, +16 = "was ... -> gun centre"
VSRC_NAMES = ("legacy", "hand-bone", "glove cluster", "gun centre", "gun bone origin")
PSRC_NAMES = ("hand bone frame", "forearm", "main bone")


def src_code(names, text):
    for i, n in enumerate(names):
        if text.startswith(n):
            return i + (16 if " was " in text or "; was" in text else 0)
    return 0


CS_GRIP_MAX_GUN_DIST = 0.10       # m: cstrike keeps its legacy hand unless it is further than this from the gun mesh
CS_MUZZLE_MAX_GUN_DIST = 0.05     # m: a cstrike v_ attachment further than this from the gun mesh is not the muzzle
GRIP_MAX_GUN_DIST = 0.2          # m: a v_ grip further than this from the gun mesh is replaced by the gun centre
HAND_TO_WRIST = (0.0, -0.025, -0.065)     # glove-cluster centre → wrist (Unity view frame), measured on the hand-bone models
HEADER = 80
GUTTER = 8
MIPS = 4
KINDS = ("fire", "fire2", "empty", "draw", "reload1", "reload2", "reload3", "special1", "special2", "special3")
K = {k: i for i, k in enumerate(KINDS)}
POSE_NAMES = ("idle", "attack", "reload_start", "reload_end")
MIRROR_VIEW = {"cstrike": True}        # mods whose v_ models are stored left-handed

# id, stem (v_/p_/w_<stem>.mdl), class (gun|pistol|knife|grenade|c4|armor), sounds {kind: wav under sound/}
# CS weapon ids (decomp/cstrike/NOTES.md §1); 31/32 = kevlar / assault suit (equipment, world model only).
# Fire sounds are the unsilenced ones (the world uses unsilenced USP/M4A1 values).
W = "weapons/"
WEAPONS = [
    (1, "p228", "pistol", {"fire": W + "p228-1.wav"}),
    (3, "scout", "gun", {"fire": W + "scout_fire-1.wav"}),
    (4, "hegrenade", "grenade", {"fire": W + "pinpull.wav", "special1": W + "hegrenade-1.wav",
                                 "special2": W + "he_bounce-1.wav", "special3": W + "hegrenade-2.wav"}),
    (5, "xm1014", "gun", {"fire": W + "xm1014-1.wav"}),
    (6, "c4", "c4", {"fire": W + "c4_plant.wav", "special1": W + "c4_beep1.wav", "special2": W + "c4_explode1.wav",
                     "special3": W + "c4_disarm.wav"}),
    (7, "mac10", "gun", {"fire": W + "mac10-1.wav"}),
    (8, "aug", "gun", {"fire": W + "aug-1.wav"}),
    (9, "smokegrenade", "grenade", {"fire": W + "pinpull.wav", "special1": W + "sg_explode.wav",
                                    "special2": W + "grenade_hit1.wav"}),
    (10, "elite", "pistol", {"fire": W + "elite_fire.wav", "draw": W + "elite_deploy.wav"}),
    (11, "fiveseven", "pistol", {"fire": W + "fiveseven-1.wav"}),
    (12, "ump45", "gun", {"fire": W + "ump45-1.wav"}),
    (13, "sg550", "gun", {"fire": W + "sg550-1.wav"}),
    (14, "galil", "gun", {"fire": W + "galil-1.wav", "fire2": W + "galil-2.wav"}),
    (15, "famas", "gun", {"fire": W + "famas-1.wav", "fire2": W + "famas-2.wav"}),
    (16, "usp", "pistol", {"fire": W + "usp_unsil-1.wav", "special1": W + "usp1.wav"}),          # special1 = silenced shot
    (17, "glock18", "pistol", {"fire": W + "glock18-1.wav", "fire2": W + "glock18-2.wav"}),
    (18, "awp", "gun", {"fire": W + "awp1.wav", "draw": W + "awp_deploy.wav"}),
    (19, "mp5", "gun", {"fire": W + "mp5-1.wav", "fire2": W + "mp5-2.wav"}),
    (20, "m249", "gun", {"fire": W + "m249-1.wav", "fire2": W + "m249-2.wav"}),
    (21, "m3", "gun", {"fire": W + "m3-1.wav"}),
    (22, "m4a1", "gun", {"fire": W + "m4a1_unsil-1.wav", "fire2": W + "m4a1_unsil-2.wav", "draw": W + "m4a1_deploy.wav",
                         "special1": W + "m4a1-1.wav"}),
    (23, "tmp", "gun", {"fire": W + "tmp-1.wav", "fire2": W + "tmp-2.wav"}),
    (24, "g3sg1", "gun", {"fire": W + "g3sg1-1.wav"}),
    (25, "flashbang", "grenade", {"fire": W + "pinpull.wav", "special1": W + "flashbang-1.wav",
                                  "special2": W + "grenade_hit1.wav", "special3": W + "flashbang-2.wav"}),
    (26, "deagle", "pistol", {"fire": W + "deagle-1.wav", "fire2": W + "deagle-2.wav", "draw": W + "de_deploy.wav"}),
    (27, "sg552", "gun", {"fire": W + "sg552-1.wav", "fire2": W + "sg552-2.wav"}),
    (28, "ak47", "gun", {"fire": W + "ak47-1.wav", "fire2": W + "ak47-2.wav"}),
    (29, "knife", "knife", {"fire": W + "knife_slash1.wav", "fire2": W + "knife_slash2.wav",
                            "draw": W + "knife_deploy1.wav", "special1": W + "knife_hit1.wav",
                            "special2": W + "knife_stab.wav", "special3": W + "knife_hitwall1.wav"}),
    (30, "p90", "gun", {"fire": W + "p90-1.wav"}),
    (31, "kevlar", "armor", {"fire": "items/kevlar.wav"}),
    (32, "assault", "armor", {"fire": "items/kevlar.wav"}),
]

# Player sounds (footsteps per material, ladder, water, landing, body hits, deaths; NOTES_client.md §3.3/§3.5). They
# are plain entries of the sound table, referenced by no weapon record: the loader finds them by NAME
# (GsvrModelLoader.GetClipByName("player/pl_step1.wav")). Old loaders just build a few more clips. Missing files are
# skipped (e.g. pl_snow5/6 only exist in some installs).
P = "player/"
PLAYER_SOUNDS = ([P + f"pl_{s}{i}.wav" for s in ("step", "dirt", "metal", "grate", "duct", "slosh", "wade", "ladder")
                  for i in range(1, 5)]
                 + [P + f"pl_tile{i}.wav" for i in range(1, 6)] + [P + f"pl_snow{i}.wav" for i in range(1, 7)]
                 + [P + f"pl_swim{i}.wav" for i in range(1, 5)] + [P + f"pl_fallpain{i}.wav" for i in (1, 2, 3)]
                 + [P + f"bhit_flesh-{i}.wav" for i in (1, 2, 3)] + [P + "bhit_helmet-1.wav", P + "bhit_kevlar-1.wav"]
                 + [P + f"headshot{i}.wav" for i in (1, 2, 3)] + [P + f"die{i}.wav" for i in (1, 2, 3)]
                 + [P + "death6.wav", P + "pl_die1.wav"] + [P + f"pl_pain{i}.wav" for i in (2, 4, 5, 6, 7)]
                 # bullet impacts (NOTES_client §3.3, GsvrCsAudio.PlayImpact): wood / glass texture sounds + ricochets
                 + [f"debris/wood{i}.wav" for i in (1, 2, 3)] + [f"debris/glass{i}.wav" for i in (1, 2, 3)]
                 + ["items/gunpickup2.wav"]   # armoury_entity pickups (GsvrCsAudio.PlayPickup)
                 # debris / gib impact tables (CBreakable::MaterialSoundRandom + DamageSound; GsvrCsAudio.PlayDebris): wood / glass are above
                 + [f"debris/metal{i}.wav" for i in (1, 2, 3)] + [f"debris/concrete{i}.wav" for i in (1, 2, 3)]
                 + [f"debris/flesh{i}.wav" for i in (1, 2, 3, 5, 6, 7)]
                 + [f"weapons/ric{i}.wav" for i in range(1, 6)] + ["weapons/ric_conc-1.wav", "weapons/ric_conc-2.wav",
                                                                    "weapons/ric_metal-1.wav", "weapons/ric_metal-2.wav"]
                 # func_breakable break sounds (HLSDK CBreakable::Die per material; GsvrCsAudio.PlayBreak), valve/sound
                 + [f"debris/bust{m}{i}.wav" for m in ("glass", "crate", "metal", "flesh", "concrete") for i in (1, 2)]
                 + ["debris/bustceiling.wav"]
                 # classic rounds (GsvrCsRounds / GsvrCsAudio.PlayRadio), cstrike/sound/radio
                 + ["radio/" + n + ".wav" for n in ("terwin", "ctwin", "rounddraw", "letsgo", "locknload", "moveout", "go",
                                                     "bombpl", "bombdef", "rescued")]
                 # C4 (GsvrCsBomb): beeps in 5 waves, plant / disarm / disarmed, explosion
                 + ["weapons/c4_" + n + ".wav" for n in ("beep1", "beep2", "beep3", "beep4", "beep5", "plant", "disarm",
                                                          "disarmed", "explode1", "click")])

# Sprites for the GSX1 "SPR1" chunk (decomp/cstrike/NOTES_client.md §9: smoke grenade cloud = gas_puff_01 via the
# particle manager, the per-second wisps = black_smoke1-4 tempents). Missing files are skipped.
SPRITES = ("sprites/gas_puff_01.spr", "sprites/black_smoke1.spr", "sprites/black_smoke2.spr",
           "sprites/black_smoke3.spr", "sprites/black_smoke4.spr")

# Effect sprites for the GSX1 "SPR2" chunk, in priority order (the pack budget FX_TEXELS drops the tail; map sprites are
# appended after these, most used first). Names as the game code precaches them: explosions (fireball, water, steam smoke,
# CS/HL mixed variants), muzzle flashes (HL muzzleflash*, CS muzzleflash1-4, the monster muz1-8), blood, beams (laserbeam, xbeam,
# zbeam, lgtning, xenobeam, plasma), sparks/ricochets, glows/flares (env_glow / env_sprite), puffs. Missing files are skipped.
FX_SPRITES = (
    # most needed first: explosion + smoke, muzzle flashes, blood, impact puff / sparks, the common beams and glows ...
    "sprites/zerogxplode.spr", "sprites/fexplo.spr", "sprites/steam1.spr", "sprites/WXplo1.spr",
    "sprites/muzzleflash1.spr", "sprites/muzzleflash2.spr", "sprites/muzzleflash3.spr", "sprites/muzzleflash4.spr", "sprites/muzzleflash.spr",
    "sprites/blood.spr", "sprites/bloodspray.spr", "sprites/wallpuff.spr", "sprites/xspark1.spr",
    "sprites/laserbeam.spr", "sprites/lgtning.spr", "sprites/xbeam1.spr", "sprites/smoke.spr",
    "sprites/glow01.spr", "sprites/flare1.spr", "sprites/glow02.spr", "sprites/glow03.spr",
    # ... then the rarer ones (the tail goes first when the pack is tight)
    "sprites/fexplo1.spr", "sprites/eexplo.spr", "sprites/eexplosion.spr", "sprites/explode1.spr", "sprites/explode2.spr",
    "sprites/xsmoke1.spr", "sprites/xsmoke3.spr", "sprites/blooddrop.spr", "sprites/bhit.spr", "sprites/richo1.spr",
    "sprites/richo2.spr", "sprites/spark1.spr", "sprites/xspark2.spr", "sprites/xspark3.spr", "sprites/xspark4.spr",
    "sprites/zbeam1.spr", "sprites/zbeam2.spr", "sprites/zbeam3.spr", "sprites/zbeam4.spr", "sprites/zbeam5.spr",
    "sprites/zbeam6.spr", "sprites/xbeam2.spr", "sprites/xbeam3.spr", "sprites/xbeam4.spr", "sprites/xbeam5.spr",
    "sprites/xenobeam.spr", "sprites/plasma.spr", "sprites/white.spr", "sprites/rope.spr", "sprites/hotglow.spr",
    "sprites/streak.spr", "sprites/glow04.spr", "sprites/glow05.spr", "sprites/flare2.spr", "sprites/flare3.spr",
    "sprites/flare4.spr", "sprites/flare5.spr", "sprites/flare6.spr", "sprites/xflare1.spr", "sprites/xflare2.spr",
    "sprites/animglow01.spr", "sprites/shockwave.spr", "sprites/laserdot.spr",
    "sprites/muz1.spr", "sprites/muz2.spr", "sprites/muz3.spr", "sprites/muz4.spr", "sprites/muz5.spr", "sprites/muz6.spr",
    "sprites/muz7.spr", "sprites/muz8.spr",
    "sprites/wall_puff1.spr", "sprites/wall_puff2.spr", "sprites/wall_puff3.spr", "sprites/wall_puff4.spr",
    "sprites/pistol_smoke1.spr", "sprites/rifle_smoke1.spr",
)
# Per-mod additions to FX_SPRITES (spliced into the priority order by fx_sprite_order): the sprites the ported
# effects name that only these mods ship (GsvrTempFx.SpriteCreate / SpriteExpand / BeamStyled resolve them by lower-case name through
# FindSprite): gonome / bullsquid spit (bigspit, tinyspit), big mama spit, gargantua eye glow, xen exit flash, OF displacer ring,
# grapple tongue, spore launcher bursts. valve / bshift ship the HL set; gearbox has its own plus the valve fallback of find().
FX_SPRITES_MOD = {
    "valve": ("sprites/bigspit.spr", "sprites/mommaspit.spr", "sprites/gargeye1.spr", "sprites/tinyspit.spr", "sprites/exit1.spr"),
    "bshift": ("sprites/bigspit.spr", "sprites/mommaspit.spr", "sprites/gargeye1.spr", "sprites/tinyspit.spr", "sprites/exit1.spr"),
    "gearbox": ("sprites/bigspit.spr", "sprites/gargeye1.spr", "sprites/tinyspit.spr", "sprites/tongue.spr", "sprites/disp_ring.spr",
                "sprites/spore_exp_01.spr", "sprites/spore_exp_b_01.spr", "sprites/spore_exp_c_01.spr"),
}
# Effect sprites stored at HALF resolution per mod (spr.half_sprite: frames 2x2 box-filtered, k = 2 in the SPR2 frame table, the
# runtime world size is unchanged). gearbox: fexplo is 30 frames of 80x96 (the largest of the set); at half size the OF displacer
# ring / spore bursts fit the 99 MB guard's texel budget (they were dropped before, see PACK_REV gearbox 10).
FX_HALF = {"gearbox": ("sprites/fexplo.spr",)}
FX_MOD_BEFORE = {"gearbox": "sprites/smoke.spr"}   # per-mod splice point (default FX_MOD_BEFORE_DEFAULT): the gearbox pack sits at the size guard, so its
FX_MOD_BEFORE_DEFAULT = "sprites/glow04.spr"          # block goes ahead of the glow / smoke sprites it would otherwise lose entirely; the budget drops from the end


def fx_sprite_order(mod):
    """FX_SPRITES with the mod's FX_SPRITES_MOD block spliced in before FX_MOD_BEFORE[mod] (appended when that one is absent)."""
    extra = FX_SPRITES_MOD.get(mod, ())
    if not extra:
        return FX_SPRITES
    at = FX_MOD_BEFORE.get(mod, FX_MOD_BEFORE_DEFAULT)
    i = FX_SPRITES.index(at) if at in FX_SPRITES else len(FX_SPRITES)
    return FX_SPRITES[:i] + tuple(n for n in extra if n not in FX_SPRITES) + FX_SPRITES[i:]


# per-mod default budgets (texels at level 0, 5.25 bytes each with the mips): the cstrike pack is already ~94 MB, so it only keeps the core
FX_TEXELS_MOD = {"cstrike": 1024 * 512}
FX_TEXELS = 1024 * 1536          # SPR2 atlas budget (texels at level 0; 5.25 bytes per texel with the mips); valve / cstrike packs stay near 90 MB


# Gib models for the GSX1 "GIB1" chunk (kind-6 meshes, one pose per bodypart-0 model = the individual gib pieces). Order = priority
# when the pack is tight. hgibs / agibs / germangibs: CGib::SpawnHeadGib / SpawnRandomGibs (human: body 0 = head, 1..5 = pieces; alien
# 0..3; German low-violence set 0..3); the rest are CBreakable's material gib models (Precache: glass, wood, metal plate, flesh, cinder
# block, ceiling tile, computer, rocks) which func_breakable debris uses unless the map names its own `gibmodel`. Missing files are skipped
# (germangibs.mdl only ships in some localisations); models the mod's maps name in gibmodel / shootmodel keys are appended
# (map_gib_models) up to GIB_MAX_MODELS.
GIB_MODELS = ("hgibs", "agibs", "germangibs", "glassgibs", "woodgibs", "metalplategibs", "cindergibs", "ceilinggibs",
              "computergibs", "fleshgibs", "rockgibs")

# Stand-ins for models the game code names but a normal install may lack or that cannot be read (maintainer decision 2026-11-10:
# a stand-in from the player's OWN install, no foreign files, no in-game hint). The original file always wins; only when
# load_mdl() finds no readable file does the stand-in step in: name -> (stand-in model, uniform scale, where).
#   where "pack":    the stand-in .mdl is baked under the ORIGINAL name (scaled), so the runtime finds the name as usual.
#   where "runtime": the stand-in is a row of the pack anyway at scale 1; nothing is duplicated, the runtime falls back to it by name
#                    (GsvrHlMonsters.PackMonsterOf, GsvrHlGibs.ModelOf, the item world mesh id).
# pit_worm.mdl (COFPitWorm, monster_pitworm): Steam Opposing Force ships only pit_worm_up.mdl (the 578 u stationary boss, no walk /
#   run activities); CBigMomma's big_mom.mdl has the same 32 x 64 spawn hull and the walk / run / melee activities the schedules use.
# baby_voltigore.mdl: the original is voltigore.mdl at exactly 1/3 (vertex extents 60 x 42 x 32.8 vs 180 x 127 x 98.5, same 16 sequences).
# cine3-scientist.mdl (CCine3Scientist): no Steam install ships any cine3-scientist; the scientist carries the same body.
# vgibs.mdl (COFVoltigore / CGib, 9 pieces): the alien gibs (4 pieces, bodies past 3 wrap at run time).
# spore_ammo.mdl (CSporeAmmo, ammo_spore): spore.mdl, the spore model of the spore launcher's CSpore.
MODEL_STANDINS = {
    "pit_worm": ("big_mom", 1.0, "runtime"),
    "baby_voltigore": ("voltigore", 1.0 / 3.0, "pack"),
    "cine3-scientist": ("scientist", 1.0, "runtime"),
    "vgibs": ("agibs", 1.0, "runtime"),
    "spore_ammo": ("spore", 1.0, "pack"),
}


def standin_of(name):
    """-> (stand-in model name, scale, where) for a model name (with or without models/ and .mdl), None when it has none."""
    n = name.lower()
    if n.startswith("models/"):
        n = n[7:]
    if n.endswith(".mdl"):
        n = n[:-4]
    return MODEL_STANDINS.get(n)
GIB_MATERIAL = GIB_MODELS[3:]   # the CBreakable material models only (the cstrike pack: no monsters, so no human / alien gibs)
GIB_OPTIONAL = ("germangibs",)
GIB_MAX_MODELS = 40
GIB_EXTRA_TEXELS = 80 * 1024   # atlas texels (new textures only, cell size incl. the 8-px gutters, level 0 = ~5.3 bytes each with the mips) the
                               # map-named extra gib models may add to the atlas; the core set (about 0.9 MB) is free
GIB_MAX_PIECES = 32            # pieces (bodypart-0 models) kept per gib model; bodies past it fall back to modulo at run time
MESH_GIB = 6                   # mesh kind of a gib piece (kinds 0..5 as before)


def map_entity_lumps(root, mod):
    """Entity lump text of every BSP30 map of the mod (<mod>/maps, <mod>_addon/maps), parsed as text only."""
    dirs = [os.path.join(root, d, "maps") for d in (mod, mod + "_addon")]
    seen = set()
    for d in dirs:
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for fn in names:
            if not fn.lower().endswith(".bsp") or fn.lower() in seen:
                continue
            seen.add(fn.lower())
            try:
                with open(os.path.join(d, fn), "rb") as fh:
                    data = fh.read()
                    if len(data) < 4 + 15 * 8 or struct.unpack_from("<i", data, 0)[0] != 30:
                        continue
                    off, ln = bsp30.lump_dir(data)[0]       # Blue Shift stores entities/planes swapped
                    if not 0 < ln < 4 << 20:
                        continue
                    yield data[off:off + ln].split(b"\0")[0].decode("latin-1")
            except (OSError, struct.error):
                continue


def map_sprites(root, mod):
    """Sprites the mod's maps reference (any entity key whose value ends in .spr: env_sprite / env_glow `model`, env_beam /
    env_laser `texture`, ...), most-used first, ties by name. Entity lumps are only parsed as text."""
    import collections, re
    cnt = collections.Counter()
    for ents in map_entity_lumps(root, mod):
        for m in set(re.findall(r'"[A-Za-z_]+"\s*"([^"\\]*\.spr)"', ents, re.I)):
            m = m.replace("\\", "/").lower()
            if m.startswith("sprites/") and ".." not in m:
                cnt[m] += 1
    return [n for n, _c in sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))]


def map_gib_models(root, mod):
    """Gib models the mod's maps name: func_breakable `gibmodel` and env_shooter / gibshooter `shootmodel` values that are
    models/*.mdl (sprites are left to map_sprites), most-used first, ties by name. Parsed as text, names sanitised."""
    import collections, re
    cnt = collections.Counter()
    for ents in map_entity_lumps(root, mod):
        for m in set(re.findall(r'"(?:gibmodel|shootmodel)"\s*"([^"\\]*\.mdl)"', ents, re.I)):
            m = m.replace("\\", "/").lower()
            if m.startswith("models/") and ".." not in m.split("/") and len(m) < 96 and re.fullmatch(r"[a-z0-9_./()\-]+", m):
                cnt[m] += 1
    return [n for n, _c in sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))]


# ── studio props (MON1 rows the runtime draws as GsvrHlMonsters prop variants, VProp0..) ──
# The map's decorative studio-model entities: `cycler` (CGenericCycler: the map `model` key), `cycler_prdroid`
# (CCyclerProbe: models/prdroid.mdl), `xen_spore_small / _medium / _large` (CXenSpore: the skin-selected entry of
# pModelNames = fungus(small) / fungus / fungus(large) [obs string_arrays CXenSpore::pModelNames]) and `xen_hair`
# (CXenHair: models/hair.mdl). Every sequence is kept ("*": a cycler steps through all of them when shot) under its own
# pose vertex budget. Only the mods whose profile sets props=True (valve, gearbox) carry them.
PROP_FIXED = {"cycler_prdroid": "models/prdroid.mdl", "xen_spore_small": "models/fungus(small).mdl",
              "xen_spore_medium": "models/fungus.mdl", "xen_spore_large": "models/fungus(large).mdl", "xen_hair": "models/hair.mdl"}
PROP_MAX_MODELS = 24           # prop models per pack (cycler / fixed first, then monster_generic ones, most-used first; the runtime has 8 prop rows per map)
PROP_POSE_VERTS = 12000        # pose vertex budget per prop model (key caps scale down like the Sven monsters)


def map_prop_models(root, mod):
    """Studio-prop model stems the mod's maps place (see PROP_FIXED / `cycler` model keys), most-used first, ties by name.
    Entity lumps are parsed as text only; names are sanitised like the gib models."""
    import collections, re
    cnt = collections.Counter()
    gen = collections.Counter()          # monster_generic models (the runtime simulates only the known monster models; the rest is a studio prop)
    for ents in map_entity_lumps(root, mod):
        seen = set()
        seen_gen = set()
        for body in re.findall(r"\{([^{}]*)\}", ents):
            m = re.search(r'"classname"\s*"([^"\\]*)"', body)
            if not m:
                continue
            cls = m.group(1).lower()
            if cls in ("cycler", "monster_generic"):
                mo = re.search(r'"model"\s*"([^"\\]*)"', body)
                name = mo.group(1) if mo else ""
            else:
                name = PROP_FIXED.get(cls, "")
            name = name.replace("\\", "/").lower()
            if not (name.startswith("models/") and name.endswith(".mdl") and ".." not in name.split("/") and len(name) < 96
                    and re.fullmatch(r"[a-z0-9_./()\-]+", name)) or "/" in name[7:]:
                continue
            (seen_gen if cls == "monster_generic" else seen).add(name[7:-4])
        for n in seen:
            cnt[n] += 1
        for n in seen_gen:
            gen[n] += 1
    out = [n for n, _c in sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))]
    # generic-actor models after the cycler / fixed ones: of0a0's mini_osprey / mini_apache / intro_* actors (monster_generic with a model no
    # simulated monster uses) are drawn as studio props, and must never push a cycler model out of the pack
    return out + [n for n, _c in sorted(gen.items(), key=lambda kv: (-kv[1], kv[0])) if n not in cnt]


# view-model sequences per pose (first existing label wins); unsilenced variants first for USP/M4A1
VIEW_SEQS = {
    "idle": ("idle_unsil", "idle", "idle1"),
    "attack": ("shoot1_unsil", "shoot1", "shoot", "shoot_right1", "midslash1", "throw", "pressbutton"),
    "reload": ("reload_unsil", "reload", "insert", "stab", "pullpin", "drop"),
    "draw": ("draw_unsil", "draw", "deploy"),
}

# ── player models (GSM1 v2) ──
# cstrike/models/player/<name>/<name>.mdl (+ <name>T.mdl when the textures are split off); team 0 = T, 1 = CT.
PLAYER_MODELS = (("terror", 0), ("leet", 0), ("arctic", 0), ("guerilla", 0),
                 ("urban", 1), ("gsg9", 1), ("sas", 1), ("gign", 1))
DEFAULT_PLAYERS = ("terror", "urban")
# upper-body animation classes = the weapons' DefaultDeploy() szAnimExt (decomp/cstrike/evidence/weapons_condensed.txt:
# CAK47::Deploy "ak47", CAUG "carbine", CAWP "rifle", ... ; the shield variants are not used)
PLAYER_CLASSES = ("ak47", "carbine", "rifle", "mp5", "onehanded", "dualpistols", "shotgun", "m249", "knife",
                  "grenade", "c4")
ANIM_EXT = {"p228": "onehanded", "scout": "rifle", "hegrenade": "grenade", "xm1014": "m249", "c4": "c4",
            "mac10": "onehanded", "aug": "carbine", "smokegrenade": "grenade", "elite": "dualpistols",
            "fiveseven": "onehanded", "ump45": "carbine", "sg550": "rifle", "galil": "ak47", "famas": "carbine",
            "usp": "onehanded", "glock18": "onehanded", "awp": "rifle", "mp5": "mp5", "m249": "m249", "m3": "shotgun",
            "m4a1": "rifle", "tmp": "onehanded", "g3sg1": "mp5", "flashbang": "grenade", "deagle": "onehanded",
            "sg552": "mp5", "ak47": "ak47", "knife": "knife", "p90": "carbine"}
# anim ids (GsvrModelLoader.PAnim*) → (upper sequence pattern, gait sequence or None, baked frames, crouched)
A_IDLE, A_WALK, A_RUN, A_CIDLE, A_CWALK, A_SHOOT, A_CSHOOT, A_DEATH, A_CDEATH = range(9)
ANIM_NAMES = ("idle", "walk", "run", "crouch_idle", "crouch_walk", "shoot", "crouch_shoot", "death", "crouch_death")
CLASS_ANIMS = (  # per weapon class; the gait sequence drives the legs (Mdl.gait_bones), the upper one the torso/arms
    (A_IDLE, "ref_aim_%s", "idle1", 1, False),
    (A_WALK, "ref_aim_%s", "walk", 6, False),
    (A_RUN, "ref_aim_%s", "run", 8, False),
    (A_CIDLE, "crouch_aim_%s", "crouch_idle", 1, True),
    (A_CWALK, "crouch_aim_%s", "crouchrun", 6, True),
    (A_SHOOT, "ref_shoot_%s", "idle1", 1, False),
    (A_CSHOOT, "crouch_shoot_%s", "crouch_idle", 1, True),
)
# VAN1 view anims (smooth reload / draw / attack on the desktop v_ model): max key frames per anim (≤ 16: pre-VAN1
# loaders cap weapon meshes at 16 poses) and the in-between tolerance in GoldSrc units
VAN_KEYS = (12, 6, 4)
VAN_SIL_KEYS = 16               # attach / detach silencer anims (anim 6, 7): the most a kind-2 mesh may carry (16 poses)
VAN_SHOOT_KEYS = 3               # extra shoot variants (anim 3, 4) and the last-round shot (anim 5)
# Shoot sequences per v_ model as cs/client.so picks them (NOTES_client.md §3.2 [obs EV_Fire*]): random among the
# list, the empty one when the clip runs out (pistols). Unsilenced USP / M4A1 (the pack's attack anim). The elite
# (left / right alternation) keeps its single attack anim.
SHOOT_SEQS = {
    "ak47": ([3, 4, 5], None), "aug": ([3, 4, 5], None), "awp": ([1, 2, 3], None), "deagle": ([1, 2], 3),
    "fiveseven": ([1, 2], 3), "g3sg1": ([1, 2], None), "galil": ([3, 4, 5], None), "famas": ([3, 4, 5], None),
    "glock18": ([5], 6), "m249": ([1, 2], None), "m3": ([1, 2], None), "m4a1": ([8, 9, 10], None),
    "mac10": ([3, 4, 5], None), "mp5": ([3, 4, 5], None), "p228": ([1, 2, 3], 4), "p90": ([3, 4, 5], None),
    "scout": ([1, 2], None), "sg550": ([1, 2], None), "sg552": ([3, 4, 5], None), "tmp": ([3, 4, 5], None),
    "ump45": ([3, 4, 5], None), "usp": ([9, 10, 11], 12), "xm1014": ([1, 2], None),
}
# HL-family v_ sequences the client weapon code plays besides reload / draw / attack (docs/decomp/hl_client/EvWeapons*.md,
# Wpn_*.md): (VAN1 anim, sequence index, its label in the HL model, key cap). Anim 3 / 4 = ViewAttack2 / 3 (shoot variants), 5 =
# ViewAttackLast (the empty-clip shot), 100 + n = sequence n itself (VIEW_SEQ0: idles, fidgets, the double shot). A model whose
# sequence n has another label (another mod's file) skips the entry.
VIEW_SEQ0 = 100
VIEW_SEQ_EXTRA = {
    "9mmhandgun": ((5, 4, "shoot_empty", 4),),                                        # EV_FireGlock1: 4 on the last round
    "shotgun": ((VIEW_SEQ0 + 2, 2, "shoot_big", 6),                                    # EV_FireShotGunDouble
                (VIEW_SEQ0 + 9, 9, "deepidle", 8), (VIEW_SEQ0 + 0, 0, "sm_idle", 4), (VIEW_SEQ0 + 8, 8, "idle4", 4)),   # CShotgun::WeaponIdle
    "9mmar": ((3, 6, "shoot", 3), (4, 7, "shoot", 3),                                  # EV_FireMP5: 5 + RandomLong(0, 2)
              (VIEW_SEQ0 + 0, 0, "longidle", 6), (VIEW_SEQ0 + 1, 1, "idle1", 8)),      # CMP5::WeaponIdle: RandomLong(0, 1)
    "tripmine": ((VIEW_SEQ0 + 0, 0, "idle1", 6), (VIEW_SEQ0 + 1, 1, "idle2", 6), (VIEW_SEQ0 + 4, 4, "fidget", 8)),    # CTripmine::WeaponIdle
    "357": ((VIEW_SEQ0 + 1, 1, "fidget1", 8), (VIEW_SEQ0 + 6, 6, "idle2", 6), (VIEW_SEQ0 + 7, 7, "idle3", 6)),        # CPython::WeaponIdle
    "crossbow": ((VIEW_SEQ0 + 0, 0, "idle1", 6), (VIEW_SEQ0 + 1, 1, "idle2", 4), (VIEW_SEQ0 + 2, 2, "fidget1", 8),
                 (VIEW_SEQ0 + 3, 3, "fidget2", 8)),                                     # CCrossbow::WeaponIdle
    "egon": ((VIEW_SEQ0 + 7, 7, "fire3", 4), (VIEW_SEQ0 + 8, 8, "fire4", 4)),          # EV_EgonFire: one of fire1..4 (1 and 2 are single frames)
    # EV_Crowbar: the swing counter cycles 4 / 5 / 7; CCrowbar::Swing on a hit alternates the hit sequences 6 / 8
    "crowbar": ((VIEW_SEQ0 + 4, 4, "attack1miss", 4), (VIEW_SEQ0 + 5, 5, "attack2", 4), (VIEW_SEQ0 + 7, 7, "attack3", 5),
                (VIEW_SEQ0 + 6, 6, "attack2hit", 4), (VIEW_SEQ0 + 8, 8, "attack3hit", 5)),
    # OF EV_PipeWrench: normal miss 6 / 7 / 9, hit 5 / 8 / 10 by the swing counter, big swing 13 miss / 12 hit
    "pipe_wrench": tuple((VIEW_SEQ0 + s, s, lab, 5) for s, lab in ((5, "attack1hit"), (6, "attack1miss"), (7, "attack2hit"), (8, "attack2miss"),
                                                                  (9, "attack3hit"), (10, "attack3miss"), (12, "attackbighit"), (13, "attackbigmiss"))),
    "knife": ((VIEW_SEQ0 + 6, 6, "attack2hit", 5), (VIEW_SEQ0 + 8, 8, "attack3hit", 5)),     # OF CKnife::Swing: a hit alternates 6 / 8
    # OF M40A1: EV_FireSniper plays 2 (fire) / 3 (firelastround, first int param 0); CSniperRifle::Reload anim 4 (empty, = the generic reload slot) /
    # 6 (reload3, partial clip); WeaponIdle sends 5 (reload2) 2.324 s after an empty-clip reload started
    "m40a1": ((5, 3, "firelastround", 4), (VIEW_SEQ0 + 5, 5, "reload2", 8), (VIEW_SEQ0 + 6, 6, "reload3", 12)),
}
# View anims (besides reload / draw / attack = 0 / 1 / 2) whose v_ sound events (model event 5004) are baked into the event table under that
# anim id: the M40A1 bolt2 on the last-round shot (5) and the second reload stage (100 + 5). Runtime: GsvrHlWeapons.ScheduleViewEvents.
VIEW_SEQ_EVENTS = {"m40a1": (5, VIEW_SEQ0 + 5)}
VAN_TOL = 0.3
DEATH_FRAMES = 12                # per death sequence: 11 falling frames + the end pose (4 looked like a jump into a sitting pose, maintainer 2026-10-24)
FEET_STAND, FEET_CROUCH = 36.0, 18.0   # player origin above the feet (hull −36..36 standing, −18..18 ducked)
IDENT = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0]

# ───────────────────────── Half-Life (--mod valve; svencoop uses the same tables) ─────────────────────────
# Weapon ids = the HL iId values [obs <Weapon>::GetItemInfo in hl.so: crowbar 1, glock 2, python 3, mp5 4, crossbow 6,
# shotgun 7, rpg 8, gauss 9, egon 10, hornetgun 11, handgrenade 12, tripmine 13, satchel 14, snark 15]; 16.. = items
# (pickups with only a world mesh). Model files [obs <Weapon>::Precache / Deploy]: the stems differ per weapon, so each
# row names its v_/p_/w_ files (None = the model does not exist; the tripmine's world model is v_tripmine.mdl and is not
# repeated here). Sounds [obs Precache string lists in hl.so; which one is fire / fire2 / reload is the client event
# code (ev_hldm), not in hl.so: inf]. KindEmpty for guns/pistols = weapons/357_cock1.wav (CBasePlayerWeapon::
# PlayEmptySound [obs], vol 0.8). Reload kinds come from the v_ sound events where the model has them (glock, python,
# mp5); otherwise the code's own reload sounds below. HL v_ models are right-handed: MIRROR_VIEW stays off for valve.
VALVE_WEAPONS = [
    (1, "crowbar", "knife", {"fire": W + "cbar_miss1.wav", "special1": W + "cbar_hitbod1.wav",
                             "special2": W + "cbar_hitbod2.wav", "special3": W + "cbar_hit1.wav"}, {}),
    (2, "9mmhandgun", "pistol", {"fire": W + "pl_gun3.wav"}, {}),
    (3, "357", "pistol", {"fire": W + "357_shot1.wav", "fire2": W + "357_shot2.wav"}, {}),
    (4, "9mmar", "gun", {"fire": W + "hks1.wav", "fire2": W + "hks2.wav", "special1": W + "glauncher.wav",
                         "special2": W + "glauncher2.wav", "special3": W + "hks3.wav"}, {}),
    (6, "crossbow", "gun", {"fire": W + "xbow_fire1.wav", "reload1": W + "xbow_reload1.wav",
                            "special1": W + "xbow_hit1.wav", "special2": W + "xbow_hitbod1.wav",
                            "special3": W + "xbow_fly1.wav"}, {}),
    (7, "shotgun", "gun", {"fire": W + "sbarrel1.wav", "fire2": W + "dbarrel1.wav", "reload1": W + "reload1.wav",
                           "reload2": W + "reload3.wav", "reload3": W + "scock1.wav"}, {}),   # reload1/3 per shell, pump
    (8, "rpg", "gun", {"fire": W + "rocketfire1.wav", "special1": W + "rocket1.wav"}, {}),
    (9, "gauss", "gun", {"fire": W + "gauss2.wav", "fire2": "ambience/pulsemachine.wav", "special1": W + "electro4.wav",
                         "special2": W + "electro5.wav", "special3": W + "electro6.wav"}, {}),  # fire2 = charge loop
    (10, "egon", "gun", {"fire": W + "egon_run3.wav", "special1": W + "egon_windup2.wav",
                         "special2": W + "egon_off1.wav"}, {"attack": ("fire3",)}),   # fire1/2: one frame
    (11, "hgun", "gun", {"fire": "agrunt/ag_fire1.wav", "fire2": "agrunt/ag_fire2.wav", "special1": "agrunt/ag_fire3.wav",
                         "special2": "hornet/ag_hornethit1.wav", "special3": "hornet/ag_buzz1.wav"}, {}),
    (12, "grenade", "grenade", {"special1": W + "explode3.wav", "special2": W + "grenade_hit1.wav",
                                "special3": W + "explode4.wav"}, {}),
    (13, "tripmine", "grenade", {"fire": W + "mine_deploy.wav", "special1": W + "mine_charge.wav",
                                 "special2": W + "mine_activate.wav"}, {"w": None}),
    (14, "satchel", "grenade", {"special1": W + "g_bounce1.wav", "special2": W + "explode3.wav"}, {}),
    # the satchel radio variants (v_/p_satchel_radio.mdl) stay unpacked: one P/W/V mesh per weapon id, no charge-state
    # view/player swap (docs/decomp/hl_port/G_CSatchel_CBeam.md, open)
    (15, "squeak", "grenade", {"fire": "squeek/sqk_hunt2.wav", "fire2": "squeek/sqk_hunt3.wav",
                               "special1": "squeek/sqk_blast1.wav", "special2": "squeek/sqk_die1.wav"},
     {"w": "w_sqknest.mdl"}),
    # items [obs CItem*/C*Ammo Spawn + Precache: model, touch sound]: world mesh + pickup sound (KindFire)
    (16, "battery", "item", {"fire": "items/gunpickup2.wav"}, {}),
    (17, "medkit", "item", {"fire": "items/smallmedkit1.wav"}, {}),
    (18, "suit", "item", {"fire": "items/gunpickup2.wav"}, {}),
    (19, "longjump", "item", {"fire": "items/gunpickup2.wav"}, {}),
    (20, "antidote", "item", {"fire": "items/gunpickup2.wav"}, {}),
    (21, "security", "item", {"fire": "items/gunpickup2.wav"}, {}),
    (22, "9mmclip", "item", {"fire": "items/9mmclip1.wav"}, {}),
    (23, "9mmarclip", "item", {"fire": "items/9mmclip1.wav"}, {}),
    (24, "argrenade", "item", {"fire": "items/9mmclip1.wav"}, {}),
    (25, "chainammo", "item", {"fire": "items/9mmclip1.wav"}, {}),
    (26, "357ammobox", "item", {"fire": "items/9mmclip1.wav"}, {}),
    (27, "shotbox", "item", {"fire": "items/9mmclip1.wav"}, {}),
    (28, "crossbow_clip", "item", {"fire": "items/9mmclip1.wav"}, {}),
    (29, "rpgammo", "item", {"fire": "items/9mmclip1.wav"}, {}),
    (30, "gaussammo", "item", {"fire": "items/9mmclip1.wav"}, {}),        # uranium for gauss + egon
    (31, "weaponbox", "item", {"fire": "items/gunpickup2.wav"}, {}),
]
# HL view-model sequence names (first existing label wins). idle: idle1 / idle / sm_idle (shotgun) / longidle (mp5:
# its "idle1" is 111 frames, both fine); attack: shoot (glock, mp5, shotgun), fire1 (357, crossbow), fire (rpg, gauss,
# satchel radio), attack1 (crowbar ATTACK1HIT), fire3 (egon: fire1/2 are single frames), Shoot (hgun), throw1 (grenade),
# throw (snark), drop (satchel), place (tripmine); reload: reload (the shotgun's "reload" inserts one shell after
# start_reload); draw: draw / deploy (mp5) / draw1 (crossbow, rpg) / up (hgun, snark).
VALVE_VIEW_SEQS = {
    "idle": ("idle1", "idle", "sm_idle", "longidle"),
    "attack": ("shoot", "fire1", "fire", "attack1", "fire3", "throw1", "throw", "drop", "place"),
    "reload": ("reload",),
    "draw": ("draw", "deploy", "draw1", "up"),
}
# HL player model upper-body classes = the weapons' Deploy szAnimExt [obs <Weapon>::Deploy: crowbar "crowbar", glock
# "onehanded", python "python", mp5 "mp5", shotgun "shotgun", crossbow "bow", rpg "rpg", gauss "gauss", egon "egon",
# hornetgun "hive", handgrenade "crowbar", satchel "trip" (radio: "hive"), tripmine "trip", snark "squeak"]
VALVE_PLAYER_CLASSES = ("crowbar", "onehanded", "python", "mp5", "shotgun", "bow", "rpg", "gauss", "egon", "hive",
                        "trip", "squeak")
VALVE_ANIM_EXT = {"crowbar": "crowbar", "9mmhandgun": "onehanded", "357": "python", "9mmar": "mp5", "shotgun": "shotgun",
                  "crossbow": "bow", "rpg": "rpg", "gauss": "gauss", "egon": "egon", "hgun": "hive", "grenade": "crowbar",
                  "satchel": "trip", "tripmine": "trip", "squeak": "squeak"}
# gait sequences [obs CBasePlayer::SetAnimation 0x100180]: ducked → ACT_CROUCHIDLE (crouch_idle) when still, else
# ACT_CROUCH (crawl); standing → "deep_idle" at speed 0, ACT_RUN (run2) over 220 u/s, else ACT_WALK (walk2handed).
# Upper body: ref_aim_/ref_shoot_/crouch_aim_/crouch_shoot_<ext>. Mapped onto the same PAnim ids as CS (the run
# threshold differs: 220 u/s in HL, 135 in CS — the runtime picks it).
VALVE_CLASS_ANIMS = (
    (A_IDLE, "ref_aim_%s", "deep_idle", 1, False),
    (A_WALK, "ref_aim_%s", "walk2handed", 6, False),
    (A_RUN, "ref_aim_%s", "run2", 8, False),
    (A_CIDLE, "crouch_aim_%s", "crouch_idle", 1, True),
    (A_CWALK, "crouch_aim_%s", "crawl", 6, True),
    (A_SHOOT, "ref_shoot_%s", "deep_idle", 1, False),
    (A_CSHOOT, "crouch_shoot_%s", "crouch_idle", 1, True),
)
# deaths [obs player.mdl activities: die_simple 36, die_backwards1 / die_backwards 37, die_forwards 38, headshot /
# die_spin 66, gutshot 68]; HL has no crouch death: A_CDEATH gets a record that points at the die_simple poses
VALVE_DEATHS = ((A_DEATH, ("die_simple", "die_backwards1", "die_backwards", "die_forwards", "headshot", "die_spin",
                           "gutshot"), FEET_STAND),)
# multiplayer models (models/player/<name>/<name>.mdl, the co-op / remote player bodies); no teams in HL → team 0
VALVE_PLAYER_MODELS = (("gordon", 0), ("helmet", 0), ("scientist", 0), ("barney", 0), ("gina", 0), ("gman", 0),
                       ("hgrunt", 0), ("recon", 0), ("robo", 0), ("zombie", 0))
VALVE_DEFAULT_PLAYERS = ("gordon", "helmet")
VALVE_PLAYER_SOUNDS = ([P + f"pl_{s}{i}.wav" for s in ("step", "dirt", "metal", "grate", "duct", "slosh", "wade", "ladder")
                        for i in range(1, 5)]
                       + [P + f"pl_tile{i}.wav" for i in range(1, 6)] + [P + f"pl_swim{i}.wav" for i in range(1, 5)]
                       + [P + f"pl_fallpain{i}.wav" for i in (2, 3)] + [P + f"pl_pain{i}.wav" for i in (2, 4, 5, 6, 7)]
                       + [P + "pl_die1.wav", P + "pl_jumpland2.wav", P + "pl_wade1.wav"]
                       + [f"debris/bust{m}{i}.wav" for m in ("glass", "crate", "metal", "flesh", "concrete") for i in (1, 2)]
                       + ["debris/bustceiling.wav", "items/suitchargeok1.wav", "common/wpn_denyselect.wav",
                          "common/wpn_select.wav"]
                       # HEV flashlight on / off click (CBasePlayer::FlashlightTurnOn / Off, GsvrHlFlashlight)
                       + ["items/flashlight1.wav"]
                       # CBreakable::MaterialSoundRandom / DamageSound tables (func_breakable hits, gib and debris impacts;
                       # GsvrCsAudio.PlayDebris, GsvrEntSystem.BreakDamageSound)
                       + [f"debris/{m}{i}.wav" for m in ("wood", "glass", "metal", "concrete") for i in (1, 2, 3)]
                       + [f"debris/flesh{i}.wav" for i in (1, 2, 3, 5, 6, 7)]
                       # CHornet impact / sharp-turn sounds: the weapon record maps only one hit and one buzz clip,
                       # the other variants are found by name (GsvrHlProjectiles / GsvrHlMonsters)
                       + [f"hornet/ag_{n}{i}.wav" for n in ("buzz", "hornethit") for i in (1, 2, 3)]
                       # boid ambient flock (CFlockingFlyer::MakeSound; looked up by name at runtime)
                       + [f"boid/boid_{k}{i}.wav" for k in ("alert", "idle") for i in (1, 2)]
                       # CLeech bite / alert samples (Precache + HandleAnimEvent / AlertSound, by-name lookup)
                       + [f"leech/leech_bite{i}.wav" for i in (1, 2, 3)]
                       + [f"leech/leech_alert{i}.wav" for i in (1, 2)]
                       # crowbar / crossbow impact variants the weapon records do not all carry as kind slots
                       # (CCrowbar::Swing / CCrossbowBolt::BoltTouch; looked up by name, missing files are skipped)
                       + ["weapons/cbar_hit2.wav", "weapons/cbar_hitbod3.wav", "weapons/xbow_hitbod2.wav",
                          "fvox/beep.wav"]
                       # CSqueakGrenade::Precache / GibMonster (by-name lookup: bite, hunt variants, gib splat).
                       # The snark weapon record only maps fire/fire2/special1/special2, so the rest ride here.
                       + ["squeek/sqk_hunt1.wav", "squeek/sqk_deploy1.wav", "common/bodysplat.wav"])

# ── monsters (GSX1 chunk "MON1", --mod valve) ──
# Activity ids = the DWARF enum Activity of hl.so [obs evidence/enums.txt]: 1 IDLE 3 WALK 4 RUN 7 HOP 8 LEAP 10 LAND
# 17 CROUCH 18 CROUCHIDLE 19 STAND 21-23 SIGNAL1-3 25 COWER 26 SMALL_FLINCH 27 BIG_FLINCH 28/29 RANGE_ATTACK1/2
# 30/31 MELEE_ATTACK1/2 32 RELOAD 33 ARM 34 DISARM 35 EAT 36 DIESIMPLE 37 DIEBACKWARD 38 DIEFORWARD 40-43 BARNACLE
# HIT/PULL/CHOMP/CHEW 45 INSPECT_FLOOR 47 IDLE_ANGRY 48/49 WALK/RUN_HURT 51 GLIDE 54 DETECT_SCENT 58 FEAR_DISPLAY
# 59 EXCITED 60 SPECIAL_ATTACK1 62 COMBAT_IDLE 63/64 WALK/RUN_SCARED 66 DIE_HEADSHOT 68 DIE_GUTSHOT.
# The game picks a sequence per activity with LookupActivity [obs 0x7ad20]: a weighted random choice among the
# sequences whose activity matches (weight = actweight; the records keep it). Ground speed = |linearmovement| ·
# fps / (numframes − 1), frame rate = 256 · fps / (numframes − 1) per 0..256 cycle [obs GetSequenceInfo 0x7b060],
# i.e. one cycle lasts (numframes − 1) / fps s and frame f is reached at f / fps s.
MON_ACTS = (1, 3, 4, 5, 26, 27, 28, 29, 30, 31, 36, 37, 38, 66, 68)    # every monster: idle, move, fly, attacks, pain, death
MON_PER_ACT = 2                    # sequences kept per activity (the heaviest first, file order on ties)
# key-frame caps per activity group (adaptive keys like VAN1, MON_TOL units of bone-position error between keys)
MON_KEYS = {1: 8, 3: 10, 4: 10, 26: 5, 27: 6, 36: 8, 37: 8, 38: 8, 66: 8, 68: 8}
MON_KEYS_DEFAULT = 8               # attacks and the extras
MON_KEYS_LOCO = 8                  # walk / run cycles under a pose vertex budget (not scaled, see monster())
MON_KEYS_LOCO2 = 4                 # the other moving loops (limping, scared walk) under that budget


def is_loco(sq):
    """a walk / run cycle: ACT_WALK / ACT_RUN, or any looping sequence that moves the monster (limping, scared walk)"""
    lin = math.sqrt(sum(c * c for c in sq["linear"]))
    return sq["activity"] in (3, 4) or (sq["flags"] & 1 and lin > 1.0)


def loco_keys(sq):
    return MON_KEYS_LOCO if sq["activity"] in (3, 4) else MON_KEYS_LOCO2
MON_TOL = 0.6
MON_PART_VERTS = 2500             # source vertices per monster over all kept bodypart models (default body always kept)
SKEL_PART_VERTS = 12000           # the same budget for skinned monsters (SKL1: one pose per part mesh, so alternatives are cheap)
# (model, extra activities, extra sequence names, default body, note). Bodyparts are kept per model (every head /
# gun variant is its own small mesh with the same poses); the body value picks one model per bodypart.
# scripted_sequence / aiscripted_sequence names (m_iszPlay / m_iszIdle / m_iszEntry) the HL campaign maps c*.bsp use
# per monster model (collected from the user's maps by tools/modelpack.py's author once; missing ones are skipped):
# baked with fewer keys (MON_KEYS_SCRIPT) so scripts play their real animation and fire their 1003 events (§3.1)
MON_KEYS_SCRIPT = 5
SCRIPT_SEQS = {
    "barney": ('almost', 'almostidle', 'assassinated', 'barn_wave', 'buttonpush', 'c1a3_bidle', 'c1a3_ventb', 'c3a2_draw', 'cprbarney', 'cprbarneyrevive', 'dying_barney', 'dying_barneyidle', 'dying_friend', 'dying_friendidle', 'flashlight', 'haulbarney', 'idle1', 'intropush', 'relaxstand', 'retina', 'sit1', 'sit2', 'sit3', 'standing_idle', 'trackswitch', 'unlatch'),
    "bullsquid": ('draggrunt', 'draggruntidle', 'eat', 'idle', 'scare'),
    "gman": ('bigno', 'bigyes', 'idle01', 'idlebrush', 'listen'),
    "headcrab": ('headcrabbed#1', 'headcrabbed#2', 'headidle', 'idle1'),
    "hgrunt": ('bustwall', 'combatidle', 'converse1', 'converse2', 'crouching_idle', 'divecover', 'draghole', 'dragholeidle', 'dragleft', 'dragright', 'drop_grenade', 'frontkick', 'idle1', 'impaled', 'pipetoss', 'plunger', 'walk1'),
    "houndeye": ('idle1', 'jumpwindow'),
    "islave": ('downup', 'grab', 'idle1', 'jabber', 'jibber', 'pushup', 'updown'),
    "scientist": ('beatdoor', 'buysoda', 'c1a0_catwalk', 'c1a0_catwalkidle', 'c1a4_dying_speech', 'c1a4_wounded_idle', 'ceiling_dangle', 'checktie', 'coffee', 'console', 'converse1', 'converse2', 'cowering_in_corner', 'cprscientist', 'cprscientistrevive', 'crouch', 'deskidle', 'divewindow', 'fall_elevator', 'flinch', 'franticbutton', 'gluonshow', 'handrail', 'hanging_idle', 'haulscientist', 'headcrabbed', 'ickyjump_sci', 'idle1', 'no', 'onguard', 'panic1', 'pause', 'pondering', 'pondering2', 'pondering3', 'portal', 'pulldoor', 'push_button', 'quicklook', 'retina', 'rocketcrawl', 'seeya', 'sitidle', 'sitlookleft', 'sitlookright', 'sitscared', 'sitting2', 'sitting3', 'sitstand', 'studycart', 'tentacle_grab', 'tieshoe', 'ventpull1', 'ventpull2', 'ventpullidle1', 'ventpullidle2', 'wave', 'writeboard', 'yes'),
    "zombie": ('attack1', 'attack2', 'bust_window', 'eatbody', 'eatbodystand', 'eatbodytable', 'freak', 'freakdie', 'haulzombie', 'idle1', 'kick_punch_wall', 'pause', 'ripdoor', 'slideidle', 'slidewall', 'soda', 'vent_c1a3', 'vent_zidle', 'ventclimb', 'ventclimbidle'),
}
# valve-only additions found by tools/scriptaudit.py (every scripted_sequence / aiscripted_sequence of the valve c*.bsp maps whose
# m_iszPlay / m_iszIdle exists in the actor's .mdl but was not packed; docs/scriptaudit_c1a0.md). Kept out of SCRIPT_SEQS so the
# bshift / gearbox packs (which share the table and sit near their size limits) do not change.
#   c1a1 'cut' (the two Barneys at the laser): laser_top / laser_bottom with the idle laseridle (m_fMoveTo 4, SF 8)
SCRIPT_SEQS_VALVE = {"barney": ("laser_top", "laser_bottom", "laseridle")}
SCRIPT_SEQS_VALVE_ALL = {k: SCRIPT_SEQS.get(k, ()) + tuple(n for n in SCRIPT_SEQS_VALVE.get(k, ()) if n not in SCRIPT_SEQS.get(k, ()))
                         for k in set(SCRIPT_SEQS) | set(SCRIPT_SEQS_VALVE)}
MONSTERS = (
    # scientist: body −1 → RandomLong(0,3) = one of 4 heads; body 2 → skin 1 [obs CScientist::Spawn 0x110980];
    # heal: ACT_ARM (pull_needle, event 2 → body%4 + 4 = needle) / MELEE1 give_shot / DISARM [obs §2.2]
    ("scientist", (63, 64, 58, 59, 18, 19, 33, 34, 40, 41, 42, 43), ("crouch",), 0),
    # barney: bodypart gun 0 holstered / 1 drawn / 2 none (event 2 draw → 1, event 4 holster → 0) [obs §2.3]
    ("barney", (32, 33, 34, 40, 41, 42, 43), ("cower_stand",), 0),
    ("headcrab", (), ("angry",), 0),
    ("zombie", (), (), 0),
    ("houndeye", (47, 18, 19, 8, 7), (), 0),
    ("islave", (), (), 0),
    # hgrunt: RANGE_ATTACK1 = named standing_mp5 / crouching_mp5 (SetActivity by weapon bit [obs 0xc5690])
    # + 65 victorydance (slGruntVictoryDance PLAY_SEQUENCE ACT_VICTORY_DANCE) and 5 repel_shoot (slGruntRepelAttack ACT_FLY), gruntsched
    ("hgrunt", (47, 62, 21, 22, 23, 32, 17, 48, 49, 60, 51, 10, 40, 41, 42, 43, 65, 5), ("standing_mp5", "crouching_mp5"), 0),
    ("bullsquid", (59, 35, 45, 54, 7), (), 0),
    # barnacle: the grab is sequence "attack1" by name [obs CBarnacle BarnacleThink], idle tongue = bone controller 0
    ("barnacle", (35,), ("attack1",), 0),
    ("gman", (), ("listen", "yes", "no", "lookdown"), 0),      # scripted-only (§2.11): a few gesture sequences
    # static cine props (CLegacyCineMonster): a map `impulse` key picks the original .mdl sequence index → keep all
    ("cine-barney", (), ("*",), 0),
    ("cine3-barney", (), ("*",), 0),
    ("cine-panther", (), ("*",), 0),
    ("cine-scientist", (), ("*",), 0),
    ("cine2-scientist", (), ("*",), 0),
    ("cine2_slave", (), ("*",), 0),
    ("cine3-scientist", (), ("*",), 0),       # CCine3Scientist; no Steam install ships it: MODEL_STANDINS, the runtime draws the scientist
    # bloater: registered in this build (custom Sven maps), idle / move / death from MON_ACTS
    ("floater", (), (), 0),
    ("roach", (), (), 0),                                      # CRoach (monster_cockroach): idle / walk / eat activities
    # Xen tree (CXenTree): ACT_IDLE / ACT_MELEE_ATTACK1 from the model; event 1 = the swipe
    ("tree", (), (), 0),
    # CHAssassin (monster_human_assassin): the hop uses ACT_HOP plus the named fly_up / fly_attack / fly_down sequences
    # [obs CHAssassin::RunTask, HandleAnimEvent]
    ("hassassin", (), ("fly_up", "fly_down", "fly_attack"), 0),
    # boid: the flocking decoration of monster_flyer; its sequences are ACT_FLY (5), so the whole model is kept
    ("boid", (), (), 0),
    ("leech", (6, 15, 16, 24), (), 0),                         # CLeech (monster_leech): swim / turn / twitch + 1x1x2 hull
    ("big_mom", (), (), 0),                                    # CBigMomma (monster_bigmomma): node route / acid mortar / babycrabs
)

# Counter-Strike hostages (hostage_entity `model`: models/hostage.mdl or models/scientist.mdl; maintainer 2026-10-25: they
# were Unity capsules) through the same MON1 pipeline: idle / walk / run / deaths from MON_ACTS
CS_MONSTERS = (("hostage", (), (), 0), ("scientist", (), (), 0))

CS_DEATHS = ((A_DEATH, ("death1", "death2", "death3", "head", "gutshot", "back", "forward", "left", "right"), FEET_STAND),
             (A_CDEATH, ("crouch_die",), FEET_CROUCH))
# per-mod tables; svencoop ships its own copies of all HL weapon / monster files (svencoop/models) → valve tables
PROFILES = {
    "cstrike": dict(weapons=[w + ({},) for w in WEAPONS], view_seqs=VIEW_SEQS,
                    empty={"pistol": W + "dryfire_pistol.wav", "gun": W + "dryfire_rifle.wav"},
                    classes=PLAYER_CLASSES, anim_ext=ANIM_EXT, class_anims=CLASS_ANIMS, deaths=CS_DEATHS,
                    death_alias=(), player_models=PLAYER_MODELS, default_players=DEFAULT_PLAYERS,
                    player_sounds=PLAYER_SOUNDS, sprites=SPRITES, monsters=CS_MONSTERS, gib_models=GIB_MATERIAL,
                    arm_tex=("view_glove", "view_skin", "view_finger", "newhand")),
    "valve": dict(weapons=VALVE_WEAPONS, view_seqs=VALVE_VIEW_SEQS,
                  empty={"pistol": W + "357_cock1.wav", "gun": W + "357_cock1.wav"},
                  classes=VALVE_PLAYER_CLASSES, anim_ext=VALVE_ANIM_EXT, class_anims=VALVE_CLASS_ANIMS,
                  deaths=VALVE_DEATHS, death_alias=((A_CDEATH, A_DEATH),), player_models=VALVE_PLAYER_MODELS,
                  default_players=VALVE_DEFAULT_PLAYERS, player_sounds=VALVE_PLAYER_SOUNDS, sprites=(),
                  monsters=MONSTERS, script_seqs=SCRIPT_SEQS_VALVE_ALL, props=True,
                  # arms / hands of the v_ models: HL HEV glove set, Sven soldier_arm / soldier_hand, medkit ld_hev_*,
                  # displacer hand.BMP / skin.BMP; hand_tex = the hand proper (glove, not sleeve) for the grip fallback
                  arm_tex=("glove", "sleeve", "knuckle", "handpak", "forearm", "soldier_arm", "soldier_hand", "hev_arm",
                           "cuff", "hand.bmp", "skin.bmp"),
                  hand_tex=("glove", "knuckle", "handpak", "soldier_hand", "hand.bmp")),
}
# ───────────────────────── Sven Co-op (--mod svencoop, milestone S2) ─────────────────────────
# svencoop/models/ holds HD remakes (grunt 9k vertices, 512² skins, player models ≈ 30 MB each); the same game ships
# the standard-definition sets in models/hlclassic/ (the HL models) and models/opfor/ (Opposing Force): model_dirs
# tries those first (S1 used the HD set: 80.8 MB with only 10 monsters). Weapon ids 1..15 = the HL table (same
# files, SD); Sven's own weapons are ids 32..44 (16..31 stay the HL items), their pickups 37 / 48..52. Files = the
# FGD's studio() models (svencoop/sven-coop.fgd [ext]); sounds = the v_ model events + the files under
# sound/weapons|hassault of that name [ext]; which one is fire / fire2 / special is [inf] (the client event code).
SVEN_WEAPONS = list(VALVE_WEAPONS) + [
    (32, "pipe_wrench", "knife", {"fire": W + "pwrench_miss1.wav", "fire2": W + "pwrench_big_miss.wav",
                                  "special1": W + "pwrench_hitbod1.wav", "special2": W + "pwrench_big_hitbod1.wav",
                                  "special3": W + "pwrench_hit1.wav"}, {}),
    (33, "medkit", "grenade", {"fire": "items/medshot4.wav", "fire2": "items/medshot5.wav",
                               "special1": "items/medshotno1.wav", "special2": "items/medcharge4.wav"},
     {"w": "w_pmedkit.mdl"}),
    (35, "desert_eagle", "pistol", {"fire": W + "desert_eagle_fire.wav", "reload1": W + "desert_eagle_reload.wav",
                                    "special1": W + "desert_eagle_sight.wav",
                                    "special2": W + "desert_eagle_sight2.wav"}, {}),
    (36, "uzi", "gun", {"fire": W + "uzi/shoot1.wav", "fire2": W + "uzi/shoot2.wav", "special1": W + "uzi/fire_both1.wav",
                        "special2": W + "uzi/akimbo_pull1.wav", "special3": W + "uzi/akimbo_deploy.wav"}, {}),
    (37, "2uzis", "item", {"fire": "items/gunpickup2.wav"}, {}),               # weapon_uziakimbo pickup (w_2uzis)
    (38, "m16", "gun", {"fire": W + "m16_3round.wav", "fire2": W + "m16_single.wav", "special1": W + "glauncher.wav",
                        "special2": W + "glauncher2.wav"}, {"v": "v_m16a2.mdl"}),
    (39, "saw", "gun", {"fire": W + "saw_fire1.wav", "fire2": W + "saw_fire2.wav", "special1": W + "saw_fire3.wav"}, {}),
    (40, "m40a1", "gun", {"fire": W + "sniper_fire.wav", "special1": W + "sniper_zoom.wav",
                          "special2": W + "sniper_bolt1.wav"}, {}),
    (41, "minigun", "gun", {"fire": "hassault/hw_shoot1.wav", "fire2": "hassault/hw_shoot2.wav",
                            "special1": "hassault/hw_spinup.wav", "special2": "hassault/hw_spindown.wav",
                            "special3": "hassault/hw_spin.wav"}, {"p": "p_minigunidle.mdl"}),
    (42, "displacer", "gun", {"fire": W + "displacer_fire.wav", "special1": W + "displacer_spin.wav",
                              "special2": W + "displacer_impact.wav", "special3": W + "displacer_teleport.wav"}, {}),
    (43, "spore_launcher", "gun", {"fire": W + "splauncher_fire.wav", "fire2": W + "splauncher_altfire.wav",
                                   "reload1": W + "splauncher_reload.wav", "special1": W + "splauncher_impact.wav",
                                   "special2": W + "splauncher_bounce.wav"}, {}),
    (44, "shock", "gun", {"fire": W + "shock_fire.wav", "special1": W + "shock_impact.wav",
                          "special2": W + "shock_recharge.wav"}, {"w": "w_shock_rifle.mdl"}),
    # Sven ammo pickups [ext FGD studio()]: ammo_556 w_saw_clip, ammo_762 w_m40a1clip, ammo_uziclip w_uzi_clip,
    # ammo_sporeclip spore.mdl (ammo_556clip shows w_9mmARclip = HL item 23)
    (48, "saw_clip", "item", {"fire": "items/9mmclip1.wav"}, {}),
    (50, "m40a1clip", "item", {"fire": "items/9mmclip1.wav"}, {}),
    (51, "uzi_clip", "item", {"fire": "items/9mmclip1.wav"}, {}),
    (52, "spore", "item", {"fire": W + "spore_ammo.wav"}, {"w": "spore.mdl"}),
]
# Sven v_ sequence names (first existing label wins): HL names first (the hlclassic v_ models pick exactly what the
# valve table picks), then the Sven / Opposing Force ones: slowidle (m40a1), shoot_1 (m16 burst, saw), attack1hit
# (wrench), shortuse (medkit), spinfire (minigun), reload_m16 / reload_start (saw belt) / reload1 (m40a1) /
# reload_reach (spores)
SVEN_VIEW_SEQS = {
    "idle": VALVE_VIEW_SEQS["idle"] + ("slowidle", "breathe"),
    "attack": VALVE_VIEW_SEQS["attack"] + ("shoot_1", "attack1hit", "shortuse", "spinfire"),
    "reload": VALVE_VIEW_SEQS["reload"] + ("reload_m16", "reload_start", "reload1", "reload_reach"),
    "draw": VALVE_VIEW_SEQS["draw"],
}
# Monsters: the 10 HL ones (models/hlclassic) + the Sven classes the S2 runtime simulates (docs/sven_world.md §2).
# Names = model files (models/[hlclassic|opfor/]<name>.mdl), the runtime finds them by these names. Extra activities /
# names [obs the sequence tables]: corpse poses (dead*), the medic's needle (ARM / DISARM, give_shot = MELEE2),
# turrets (no activities at all: idle_off / deploy / fire / spin / retire / die by name).
SVEN_MONSTERS = tuple((n, a, x, b) for n, a, x, b in MONSTERS if n != "gman") + (
    ("gman", (), ("listen",), 0),
    # human_grunt_ally / torch / _dead / _repel (Opposing Force grunt; heads 8, torsos 4, weapons 4)
    ("hgrunt_opfor", (47, 32, 48, 49), ("crouching_saw", "deadstomach", "deadside", "deadsitting", "dead_on_back"), 0),
    ("hgrunt_medic", (47, 33, 34, 48, 49), ("deadstomach", "deadside", "deadsitting"), 0),
    ("otis", (32, 33, 34), ("lying_on_back", "lying_on_side", "lying_on_stomach"), 0),
    ("gonome", (65,), ("attack2",), 0),                              # attack2 = the bite chain (events 19..22); 65 victory dance
    ("zombie_barney", (), (), 0),
    ("zombie_soldier", (), (), 0),
    ("pit_drone", (7,), (), 0),
    ("strooper", (47,), (), 0),
    ("massn", (47,), ("crouching_m40a1",), 0),
    # agrunt: slAGruntVictoryDance plays ACT_CROUCH / ACT_VICTORY_DANCE / ACT_STAND / ACT_THREAT_DISPLAY at the corpse
    ("agrunt", (17, 19, 57, 65), (), 0),
    # controller: the floating sequences by name (LookupFloat picks them by the velocity [obs CController 0x9aeb0])
    ("controller", (), ("up", "down", "forward", "backward", "left", "right"), 0),
    ("babygarg", (), (), 0),
    ("turret", (), ("idle_off", "deploy", "fire", "spin", "retire", "die"), 0),
    ("miniturret", (), ("idle_off", "deploy", "fire", "retire", "die"), 0),
    ("sentry", (), ("idle_off", "deploy", "fire", "spin", "retire", "die"), 0),
    # S2 additions: the gargantua (models/hlclassic), voltigore, chumtoad, ichthyosaur; the 5th field = this
    # monster's pose vertex budget. The robo grunt's own rgrunt.mdl (HD only: 5.5k vertices with its default parts,
    # 512² skins) needs ≈ 5 MB even at 2 keys per anim and would push the pack past 85 MB: it stays on hgrunt.
    ("garg", (), (), 0, 55000),
    ("voltigore", (), (), 0, 40000),
    ("chubby", (), (), 0, 12000),
    ("icky", (), (), 0, 32000),
    ("baby_headcrab", (), (), 0, 12000),                        # Sven CBabyCrab (quarter health, 180 u fast leap)
)
SVEN_PLAYER_SOUNDS = VALVE_PLAYER_SOUNDS + ["items/smallmedkit1.wav", "weapons/uzi/reload1.wav",
                                            "weapons/uzi/reload2.wav", "weapons/uzi/reload3.wav"]
# no player models by default (--players gordon still works), no VAN1 view anims; P/W textures ≤ 256 px, monster
# key caps scaled to a pose-vertex budget per monster (≥ 2 keys per anim); the atlas budget halves the v_ textures.
PROFILES["svencoop"] = dict(PROFILES["valve"], weapons=SVEN_WEAPONS, view_seqs=SVEN_VIEW_SEQS, monsters=SVEN_MONSTERS,
                            player_sounds=SVEN_PLAYER_SOUNDS, model_dirs=("hlclassic", "opfor"), default_players=(),
                            script_seqs={}, props=False, mon_part_verts=800, mon_pose_verts=55000, pw_tex_max=128, v_tex_max=128,
                            atlas_px=4096 * 1536, view_anims=False)

# ───────────────────────── Opposing Force (--mod gearbox) ─────────────────────────
# gearbox/models ships the OF versions of the HL weapons / monsters (v_ p_ w_ files of the same names win over valve/, the
# fallback find() uses for files OF does not repeat: zombie, houndeye, bullsquid, hgrunt ...) plus the OF weapons and monsters.
# Weapon ids = the runtime's (GsvrHlWeaponDb): HL 1..15 + items 16..29, the Sven block that OF shares (32 pipe wrench, 35 desert
# eagle, 39 M249 = "saw", 40 sniper = "m40a1", 42 displacer, 43 spore launcher, 44 shock rifle, items 48 / 50 / 52), 34 barnacle
# grapple and 59 knife (Knife = 59). The penguin thrower is id 60 in the runtime (it was 26, the .357 ammo box model id, until 2026-11-10); its
# v_ / p_ / w_penguin.mdl rows are not in OF_WEAPONS yet (sound names unverified) - add (60, "penguin", ...) when packing them.
# Sounds: the OF files under gearbox/sound/weapons (names listed by tools/modelpack.py's author from the install; which one is fire / fire2 /
# special is [inf], as for Sven).
OF_WEAPONS = list(VALVE_WEAPONS) + [r for r in SVEN_WEAPONS if r[0] in (32, 35, 39, 40, 42, 43, 44, 48, 50)] + [
    # 52 = ammo_spore (CSporeAmmo::Precache models/spore_ammo.mdl): the resting pose of CSporeAmmo::Spawn / Idling is sequence 5
    # (idle1, after spawndn) with body 1 (the spore sack shown); spore.mdl stands in when the file is missing (MODEL_STANDINS)
    (52, "spore", "item", {"fire": W + "spore_ammo.wav"}, {"w": "spore_ammo.mdl", "w_pose": (5, {1: 1})}),
    (34, "bgrap", "gun", {"fire": W + "bgrapple_fire.wav", "fire2": W + "bgrapple_pull.wav", "special1": W + "bgrapple_impact.wav",
                          "special2": W + "bgrapple_release.wav", "special3": W + "bgrapple_wait.wav"}, {}),
    (59, "knife", "knife", {"fire": W + "knife1.wav", "fire2": W + "knife2.wav", "special1": W + "knife_hit_flesh1.wav",
                            "special2": W + "knife_hit_wall1.wav", "special3": W + "knife3.wav"}, {}),
]
OF_MONSTERS = tuple(MONSTERS) + (
    # OF grunt family: the Sven-shared rows (SVEN_MONSTERS) plus the torch ally and the cleansuit scientist; sequence names as in Sven's table
    # + allysched: 65 victorydance (slGruntAlly / MedicAlly / TorchAllyVictoryDance), 51 repel_repel (ACT_GLIDE) and 10 repel_land (ACT_LAND) of the
    # three repel schedules, 21 advance_signal / 22 flank_signal (slGruntAllyFoundEnemy ACT_SIGNAL1, slGruntAllySignalSuppress ACT_SIGNAL2)
    ("hgrunt_opfor", (47, 32, 48, 49, 65, 51, 10, 21, 22), ("crouching_saw", "deadstomach", "deadside", "deadsitting", "dead_on_back"), 0),
    ("hgrunt_medic", (47, 33, 34, 48, 49, 65, 51, 10, 21, 22), ("deadstomach", "deadside", "deadsitting"), 0),
    ("hgrunt_torch", (47, 32, 48, 49, 65, 51, 10, 21, 22), ("deadstomach", "deadside", "deadsitting"), 0),
    ("otis", (32, 33, 34), ("lying_on_back", "lying_on_side", "lying_on_stomach"), 0),
    ("gonome", (65,), ("attack2",), 0),                              # 65 = slGonomeVictoryDance's ACT_VICTORY_DANCE
    ("zombie_barney", (), (), 0),
    ("zombie_soldier", (), (), 0),
    ("pit_drone", (7, 35, 45, 54), (), 0),           # + ACT_EAT / ACT_INSPECT_FLOOR / ACT_DETECT_SCENT (CPitdrone eat / wallow schedules)
    ("strooper", (47,), (), 0),
    ("massn", (47,), ("crouching_m40a1",), 0),
    ("cleansuit_scientist", (63, 64, 58, 59, 18, 19, 33, 34, 40, 41, 42, 43), ("crouch",), 0),
    ("recruit", (), (), 0),
    ("drill", (), (), 0),
    # bosses / big props: own pose vertex budget (5th field)
    ("voltigore", (65,), (), 0, 40000),                         # 65 = the victory dance at the enemy corpse
    ("baby_voltigore", (65,), (), 0, 12000),
    ("geneworm", (), (), 0, 30000),
    ("pit_worm_up", (), (), 0, 30000),
    ("pit_worm", (), (), 0, 30000),           # not in the Steam install: MODEL_STANDINS, the runtime then draws big_mom
    ("loader", (), (), 0, 20000),
    ("skeleton", (), (), 0),
    ("blkop_apache", (), (), 0, 30000),
    ("blkop_osprey", (), (), 0, 30000),
    ("osprey", (), (), 0, 30000),
)
OF_PLAYER_SOUNDS = VALVE_PLAYER_SOUNDS + ["items/smallmedkit1.wav", "ctf/pow_backpack.wav", "ctf/pow_armor_charge.wav", "ctf/pow_health_charge.wav",
                                           "ctf/pow_big_jump.wav", "ctf/itemthrow.wav",       # CTF powerup cues (charge loops, long jump, throw)
                                           "player/hud_nightvision.wav",      # night vision toggle (GsvrHlFlashlight, gearbox; items/nightvision1/2.wav are not in the install)
                                           "weapons/pwrench_miss2.wav"]       # EV_PipeWrench second miss layer (GsvrHlWeapons.OfWrenchSwing)
PROFILES["gearbox"] = dict(PROFILES["valve"], script_seqs=SCRIPT_SEQS, weapons=OF_WEAPONS, view_seqs=SVEN_VIEW_SEQS, monsters=OF_MONSTERS,
                           player_sounds=OF_PLAYER_SOUNDS, default_players=(),
                           # + vgibs (COFVoltigore::GibMonster / CGib: models/vgibs.mdl, 9 pieces) after the CGib models
                           gib_models=GIB_MODELS[:3] + ("vgibs",) + GIB_MODELS[3:],
                           # the unrestricted OF set is 158 MB (92 MB monster poses, 44 MB atlas): same caps as the Sven pack (<= 100 MB companion / VRChat limit)
                           mon_part_verts=1200, mon_pose_verts=40000, pw_tex_max=128, v_tex_max=128, atlas_px=4096 * 1536,
                           # studio-prop skins at most 128 px: the pack sits at the 99 MB guard (the props push the tail sprite fexplo1 out)
                           prop_tex_max=128)


# ───────────────────────── Blue Shift (--mod bshift) ─────────────────────────
# bshift/models holds the Blue Shift versions of the HL weapons (same file stems as valve, so the rows are the valve rows) and Barney's
# set; files the mod does not repeat fall back to valve/ (find()). Blue Shift DOES have rpg / gauss / egon / hornet gun (ids 8..11) and the
# rpg / gauss ammo boxes (29, 30): its server dll links + precaches them (decomp/bshift weapon_rpg, UTIL_PrecacheOtherWeapon("weapon_rpg")),
# bshift/models ships v_/p_/w_ of all four and ba_yard5 places a weapon_rpg. An earlier cut dropped them, so the RPG (ids 8) was invisible
# (no pack entry -> placeholder) when it was equipped (2026-11-06 live). Monsters: the valve table + Otis (the OF row; bshift/models/otis.mdl, same sequences). Caps as for the other
# non-CS mods (the unrestricted set would be over the 100 MB pack limit).
BS_WEAPONS = list(VALVE_WEAPONS)
BS_MONSTERS = tuple(MONSTERS) + tuple(r for r in OF_MONSTERS if r[0] == "otis")
PROFILES["bshift"] = dict(PROFILES["valve"], script_seqs=SCRIPT_SEQS, weapons=BS_WEAPONS, monsters=BS_MONSTERS, default_players=(),
                          mon_part_verts=1200, mon_pose_verts=40000, pw_tex_max=128, v_tex_max=128, atlas_px=4096 * 1536,
                          prop_tex_max=128)

# ───────────────────────── Ricochet (--mod ricochet) ─────────────────────────
# One weapon: the disc launcher (id 45, GsvrHlWeaponDb.RicDisc), v_disc / p_disc (the runtime names models/p_disc.mdl and models/v_disc.mdl;
# the flying disc models/disc.mdl is a projectile and is not a weapon mesh). No monsters, no players, no props. Sounds are not listed: the
# install's sound names could not be checked on the machine that wrote this profile (no ricochet content there), the runtime's HL stand-ins play.
# Rev 3 (flying disc) + 4 (no v_ rig, held p_disc): the flying disc models (disc.mdl = world mesh of id 45, disc_hard.mdl = pickup-class row 46; GsvrHlProjectiles.RicDisc*WorldId).
# The thrown disc was a plain 8 cm placeholder box before. Ids 46+ do not collide with DMC's: a pack holds one mod.
# Ricochet's v_disc.mdl is byte-identical to Half-Life's v_grenade.mdl (a hand with a grenade; checked 2026-11-06, cmp), so the original draws a GRENADE
# view model for the disc launcher. a VR player report (0.9.25): "I hold a grenade, there is no disc". So the pack carries NO v_ for the disc and the VR hand
# holds p_disc.mdl (the real disc, PGRP grip) instead; the thrown disc is disc.mdl.
RIC_WEAPONS = [(45, "disc", "grenade", {}, {"w": "disc.mdl", "v": None}),
               (46, "disc_hard", "item", {}, {"w": "disc_hard.mdl"})]
PROFILES["ricochet"] = dict(PROFILES["valve"], weapons=RIC_WEAPONS, monsters=(), props=False, player_sounds=[], default_players=(),
                            script_seqs={}, view_anims=True, pw_tex_max=256, v_tex_max=256)

# ───────────────────────── Deathmatch Classic (--mod dmc) ─────────────────────────
# The Quake weapon layer: ids 51..58 (GsvrHlWeaponDb.QAx..QLightning) use the models the runtime names (QView / QWorld: v_crowbar = axe, v_shot /
# v_shot2, v_nail / v_nail2, v_rock = rocket launcher, v_rock2 = grenade launcher, v_light) and the five weapon pickups (ids 46..50, DmcWorldModel:
# g_nail, g_nail2, g_shot2, g_rock2, g_light) as world-mesh items. Same caveat as Ricochet: no sounds listed (names unchecked), no monsters / players.
DMC_WEAPONS = [
    (46, "g_nail", "item", {}, {"w": "g_nail.mdl"}),
    (47, "g_nail2", "item", {}, {"w": "g_nail2.mdl"}),
    (48, "g_shot2", "item", {}, {"w": "g_shot2.mdl"}),
    (49, "g_rock2", "item", {}, {"w": "g_rock2.mdl"}),
    (50, "g_light", "item", {}, {"w": "g_light.mdl"}),
    (51, "crowbar", "knife", {}, {"w": None}),
    (52, "shot", "gun", {}, {"w": None}),
    (53, "shot2", "gun", {}, {"w": None}),
    (54, "nail", "gun", {}, {"w": None}),
    (55, "nail2", "gun", {}, {"w": None}),
    (56, "rock", "gun", {}, {"w": None}),
    (57, "rock2", "gun", {}, {"w": None}),
    (58, "light", "gun", {}, {"w": None}),
]
PROFILES["dmc"] = dict(PROFILES["valve"], weapons=DMC_WEAPONS, monsters=(), props=False, player_sounds=[], default_players=(),
                       script_seqs={}, view_anims=True, pw_tex_max=256, v_tex_max=256)

# HL client HUD sounds (CHudAmmo weapon menu clicks, CHudGeiger clicks: GsvrHlHud); valve / gearbox / bshift only (svencoop keeps its pack)
HUD_SOUNDS = ["common/wpn_hudon.wav", "common/wpn_moveselect.wav", "common/wpn_hudoff.wav"] + [f"player/geiger{i}.wav" for i in range(1, 5)]
for _m in ("valve", "gearbox", "bshift"):
    PROFILES[_m]["player_sounds"] = list(PROFILES[_m]["player_sounds"]) + [x for x in HUD_SOUNDS if x not in PROFILES[_m]["player_sounds"]]


def log(msg):
    print("  " + msg, flush=True)


# ───────────────────────── small vector helpers ─────────────────────────

def sub(a, b): return (a[0] - b[0], a[1] - b[1], a[2] - b[2])
def dot(a, b): return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
def cross(a, b): return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def norm(a):
    l = math.sqrt(dot(a, a))
    return (a[0] / l, a[1] / l, a[2] / l) if l > 1e-12 else (0.0, 0.0, 1.0)


def front_point(pts):
    """Centre of the front-most vertices (within 0.5 in of the max x) — muzzle estimate without an attachment."""
    xm = max(p[0] for p in pts)
    front = [p for p in pts if p[0] >= xm - 0.5]
    return (xm, sum(p[1] for p in front) / len(front), sum(p[2] for p in front) / len(front))


def rigid_inverse(m):
    r = (m[0], m[4], m[8], m[1], m[5], m[9], m[2], m[6], m[10])
    t = (m[3], m[7], m[11])
    return [r[0], r[1], r[2], -(r[0] * t[0] + r[1] * t[1] + r[2] * t[2]),
            r[3], r[4], r[5], -(r[3] * t[0] + r[4] * t[1] + r[5] * t[2]),
            r[6], r[7], r[8], -(r[6] * t[0] + r[7] * t[1] + r[8] * t[2])]


def rot_to_x(d):
    """3x4 rotation (no translation) taking unit vector d onto +x (Rodrigues)."""
    x = (1.0, 0.0, 0.0)
    v = cross(d, x)
    s = math.sqrt(dot(v, v))
    c = dot(d, x)
    if s < 1e-9:
        return [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0]
    k = (v[0] / s, v[1] / s, v[2] / s)
    C = 1 - c
    return [c + k[0] * k[0] * C, k[0] * k[1] * C - k[2] * s, k[0] * k[2] * C + k[1] * s, 0,
            k[1] * k[0] * C + k[2] * s, c + k[1] * k[1] * C, k[1] * k[2] * C - k[0] * s, 0,
            k[2] * k[0] * C - k[1] * s, k[2] * k[1] * C + k[0] * s, c + k[2] * k[2] * C, 0]


def principal_axis(pts):
    n = len(pts)
    c = [sum(p[k] for p in pts) / n for k in range(3)]
    cov = [[0.0] * 3 for _ in range(3)]
    for p in pts:
        d = (p[0] - c[0], p[1] - c[1], p[2] - c[2])
        for i in range(3):
            for j in range(3):
                cov[i][j] += d[i] * d[j]
    v = (1.0, 0.0, 0.0)
    for _ in range(64):
        w = tuple(sum(cov[i][j] * v[j] for j in range(3)) for i in range(3))
        v = norm(w)
    return v if v[0] >= 0 else (-v[0], -v[1], -v[2])


def to_unity(p):
    """GoldSrc model axes (x fwd, y left, z up; inches) → Unity weapon frame (metres)."""
    return (-p[1] * INCH, p[2] * INCH, p[0] * INCH)


def to_unity_dir(n):
    return norm((-n[1], n[2], n[0]))


_C = ((0, -1, 0), (0, 0, 1), (1, 0, 0))       # to_unity() as a matrix (a reflection: GoldSrc is right-handed)


def unity_rotation(m):
    """Rotation part of a GoldSrc 3x4 matrix → the same rotation in Unity axes (C·R·Cᵀ, a proper rotation)."""
    r = ((m[0], m[1], m[2]), (m[4], m[5], m[6]), (m[8], m[9], m[10]))
    return tuple(tuple(sum(_C[i][a] * r[a][b] * _C[j][b] for a in range(3) for b in range(3)) for j in range(3))
                 for i in range(3))


def quat_from_rows(r):
    """3x3 rotation (rows) → unit quaternion (x, y, z, w), Unity's convention (q * v = r · v)."""
    t = r[0][0] + r[1][1] + r[2][2]
    if t > 0:
        s = math.sqrt(t + 1.0) * 2
        q = ((r[2][1] - r[1][2]) / s, (r[0][2] - r[2][0]) / s, (r[1][0] - r[0][1]) / s, 0.25 * s)
    elif r[0][0] > r[1][1] and r[0][0] > r[2][2]:
        s = math.sqrt(1.0 + r[0][0] - r[1][1] - r[2][2]) * 2
        q = (0.25 * s, (r[0][1] + r[1][0]) / s, (r[0][2] + r[2][0]) / s, (r[2][1] - r[1][2]) / s)
    elif r[1][1] > r[2][2]:
        s = math.sqrt(1.0 + r[1][1] - r[0][0] - r[2][2]) * 2
        q = ((r[0][1] + r[1][0]) / s, 0.25 * s, (r[1][2] + r[2][1]) / s, (r[0][2] - r[2][0]) / s)
    else:
        s = math.sqrt(1.0 + r[2][2] - r[0][0] - r[1][1]) * 2
        q = ((r[0][2] + r[2][0]) / s, (r[1][2] + r[2][1]) / s, 0.25 * s, (r[1][0] - r[0][1]) / s)
    n = math.sqrt(sum(c * c for c in q)) or 1.0
    q = tuple(c / n for c in q)
    return q if q[3] >= 0 else tuple(-c for c in q)


# ───────────────────────── baking ─────────────────────────

class MeshOut:
    """One mesh: shared topology (uv + indices) and 1..n poses of (pos, nrm) in Unity axes."""

    def __init__(self, kind, wid):
        self.kind, self.wid = kind, wid
        self.uv, self.idx, self.poses = [], [], []
        self.texkeys = []          # per vertex: texture key (for the atlas remap)
        self.st = []               # per vertex: normalised (s, t) inside its texture


def collect(subs, tex_keys, keep=None, frame=None):
    """bake() submeshes → (keys in vertex order, {key: (pos, nrm)}, triangles as key triples, {key: (texkey, s, t)})."""
    order, data, tris, uvinfo = [], {}, [], {}
    for smi, sm in enumerate(subs):
        tk = tex_keys.get(sm["tex"])
        for tri in sm["tris"]:
            if keep is not None and not all(v[4] in keep for v in tri):
                continue
            kt = []
            for pos, nrm, s, t, _bone, key in tri:
                if frame is not None:
                    pos, nrm = xform(frame, pos), rotate(frame, nrm)
                if key not in data:
                    order.append(key)
                    data[key] = (pos, nrm)
                    uvinfo[key] = (tk, min(1.0, max(0.0, s)), min(1.0, max(0.0, t)))
                kt.append(key)
            tris.append(tuple(kt) + (smi,))
    return order, data, tris, uvinfo


def make_mesh(kind, wid, poses, label):
    """poses: list of (order, data, tris, uvinfo) with identical topology (pose 0 defines it)."""
    order, data0, tris, uvinfo = poses[0]
    m = MeshOut(kind, wid)
    m.order = list(order)
    index = {k: i for i, k in enumerate(order)}
    for k in order:
        m.texkeys.append(uvinfo[k][0])
        m.st.append(uvinfo[k][1:])
    # winding: GoldSrc strip/fan order vs Unity front face — decided per source mesh (one MDL mesh = one texture
    # group) by majority vote against the stored vertex normals; a few artist-mirrored parts vote the other way.
    votes = {}
    for a, b, c, smi in tris:
        pa, pb, pc = (to_unity(data0[x][0]) for x in (a, b, c))
        g = cross(sub(pb, pa), sub(pc, pa))
        nsum = [sum(to_unity_dir(data0[x][1])[i] for x in (a, b, c)) for i in range(3)]
        v = votes.setdefault(smi, [0, 0])
        v[0 if dot(g, nsum) > 0 else 1] += 1
    flips = {smi for smi, (yes, no) in votes.items() if no > yes}
    agree = sum(max(v) for v in votes.values())
    for a, b, c, smi in tris:
        m.idx += [index[a], index[c], index[b]] if smi in flips else [index[a], index[b], index[c]]
    frac = agree / max(1, len(tris))
    flip = len(flips) > 0
    for order_k, data_k, _t, _u in poses:
        pos, nrm = [], []
        for k in order:
            p, n = data_k[k]
            pos += to_unity(p)
            nrm += to_unity_dir(n)
        m.poses.append((pos, nrm))
    log(f"{label}: {len(order)} verts, {len(tris)} tris, {len(poses)} pose(s), winding "
        f"{'reversed for ' + str(len(flips)) + '/' + str(len(votes)) + ' parts' if flip else 'kept'} "
        f"({100 * frac:.0f}% agree with normals)")
    return m


def descendants(mdl, root):
    keep = {root}
    changed = True
    while changed:
        changed = False
        for i, b in enumerate(mdl.bones):
            if i not in keep and b["parent"] in keep:
                keep.add(i); changed = True
    return keep


def seq_pick(mdl, names):
    low = [s["label"].lower() for s in mdl.sequences]
    for n in names:
        if n in low:
            return low.index(n)
    return None


def monster_seq_plan(m, extra_acts, extra_names, script_names=()):
    """The MON1 sequence choice of Builder.monster for one model (pure, also used by tools/scriptaudit.py): MON_ACTS + extra_acts
    (≤ MON_PER_ACT per activity, heaviest first), then extra_names + script_names by label (case-insensitive like the
    original's LookupSequence). -> (chosen sequence indices in pack order, scripted indices (MON_KEYS_SCRIPT keys), names not
    in the model). A sequence whose anim data another chosen one already uses is skipped (houndeye ships "run" three times
    with one anim), so its label is not in the pack."""
    chosen, data_seen = [], set()

    def take(i):
        sq = m.sequences[i]
        src = (sq["seqgroup"], sq["animindex"])
        if i in chosen or src in data_seen:
            return
        chosen.append(i)
        data_seen.add(src)
    scripted, missing = set(), []
    # legacy cine props select their pose by the original .mdl sequence index (the map's `impulse` key), so keep
    # every sequence; the MON1 anim record carries that index (GetMonsterAnimSequence).
    if len(extra_names) == 1 and extra_names[0] == "*":
        for i in range(len(m.sequences)):
            take(i)
        return chosen, scripted, missing
    for act in list(MON_ACTS) + [a for a in extra_acts if a not in MON_ACTS]:
        cand = [i for i, s in enumerate(m.sequences) if s["activity"] == act]
        cand.sort(key=lambda i: -m.sequences[i]["actweight"])     # stable: file order on equal weights
        for i in cand[:MON_PER_ACT]:
            take(i)
    for nm in tuple(extra_names) + tuple(script_names):
        i = seq_pick(m, (nm,))
        if i is not None and i not in chosen and nm not in extra_names:
            scripted.add(i)
        if i is None:
            missing.append(nm)
        else:
            take(i)
    return chosen, scripted, missing


def pick_keys(P, n, kmax, tol):
    """Adaptive key frames (greedy, as bake_view_anim): start with the first and last frame, then add the frame whose
    linear in-between is worst until kmax keys or every in-between is < tol units off. P[f] = sample points."""
    if n < 2:
        return [0]

    def err(a, b, f):
        t = (f - a) / (b - a)
        worst = 0.0
        for pa, pb, pf in zip(P[a], P[b], P[f]):
            e = sum((pf[c] - (pa[c] + (pb[c] - pa[c]) * t)) ** 2 for c in range(3))
            if e > worst:
                worst = e
        return worst ** 0.5
    keys = [0, n - 1]
    while len(keys) < kmax:
        we, wf = 0.0, None
        for a, b in zip(keys, keys[1:]):
            for f in range(a + 1, b):
                e = err(a, b, f)
                if e > we:
                    we, wf = e, f
        if wf is None or we < tol:
            break
        keys.append(wf)
        keys.sort()
    return keys


def sound_events(seq):
    return [(fr, opt.replace("\\", "/")) for fr, ev, opt in seq["events"] if opt.lower().endswith(".wav")]


class Builder:
    def __init__(self, game_root, mod, max_atlas_px, max_clip_s, with_view):
        self.root, self.mod = game_root, mod
        self.prof = PROFILES[mod]
        self.max_atlas_px, self.max_clip_s, self.with_view = max_atlas_px, max_clip_s, with_view
        self.player_classes = set(self.prof["classes"])
        self.monsters = []      # dict per monster (GSX1 MON1)
        self.manims = []        # dict per monster anim record
        self.mevents = []       # (frame, event, options) per monster anim event
        self.mkeys = []         # key times (s) of all monster anims
        self.textures = {}      # content hash → (w, h, rgba, origin name, is_view)
        self.meshes = []
        self.sounds = []        # (name, rate, float32 bytes)
        self.sound_ix = {}
        self.events = []        # (anim, ms, sound, frame)
        self.weapons = []       # dict per weapon
        self.players = []       # dict per player model (v2)
        self.panims = []        # (anim, class, poseFirst, frames, cycleDist, cycleSec, loop)
        self.hands = []         # per player pose: R hand bone (pos xyz, quat xyzw), Unity body frame
        self.missing = []
        self.grips = {}         # weapon id -> dict(vsrc, vbone, vgrip, vmuzzle, psrc, pgrip) for tools/gripcheck.py + the PGRP chunk
        self.sprites = []       # (rel name, spr.Sprite) for the GSX1 SPR1 chunk
        self.fx_sprites = []    # same for the GSX1 SPR2 chunk (effect sprites, shelf-packed)
        self.hud = None         # hudpack.HudData for the GSX1 HUD1 chunk (HL client HUD sprites), None = no chunk
        self.fx_texels = FX_TEXELS
        self.max_bytes = 99_000_000
        self.vanims = []        # (weapon id, anim, mesh index, key frames, key times s) for the GSX1 VAN1 chunk
        self.vguns = []         # (mesh index, weapon id, grip xyz, [pose (pos xyz, quat xyzw)]) for the GSX1 VGUN chunk
        self.vsky = []          # (mesh index, [(pos xyz, quat xyzw)]) silencer bone pose per pose / key of that v_ mesh (GSX1 VSKY)
        self._silbone = None    # bone of the silencer part (CS USP / M4A1), set by bake_silencer
        self.vsils = []         # (weapon id, silencer mesh index, silenced muzzle xyz) for the GSX1 VSIL chunk (CS USP / M4A1)
        self.varms = []         # (mesh index, [0/1 per vertex]) for the GSX1 VARM chunk (arm/hand vertices of v_ meshes)
        self.gibs = []          # (model name, [(mesh index or -1, vertex count)]) per packed gib model (GSX1 GIB1)
        self.view_anims = True
        self.skel = True        # monsters as skinned meshes + bone texture (GSX1 SKL1); False = baked pose meshes (--no-skel)
        self.skp = studioskel.SkelPack()
        self.skel_skins = []    # (mesh index, skin family, [texture key or None per vertex]) -> SKL1 alternate UV sets
        self._arm2hand = None   # p_ forearm → wrist offset (rest pose), for p_ models without a hand bone

    # ---- files ----
    def find(self, sub, name):
        for d in (self.mod, self.mod + "_addon", "valve"):
            p = os.path.join(self.root, d, sub, name)
            if os.path.isfile(p):
                return p
        # case-insensitive fallback for case-sensitive file systems (svencoop ships p_9mmAR.mdl, player/gman/Gman.mdl)
        low = name.lower().split("/")
        for d in (self.mod, self.mod + "_addon", "valve"):
            cur = os.path.join(self.root, d, sub)
            for part in low:
                try:
                    hit = [e for e in os.listdir(cur) if e.lower() == part]
                except OSError:
                    hit = []
                if not hit:
                    cur = None
                    break
                cur = os.path.join(cur, hit[0])
            if cur and os.path.isfile(cur):
                return cur
        return None

    def load_mdl(self, name):
        # svencoop: models/hlclassic/ (the original HL models) and models/opfor/ (Opposing Force) first — the
        # standard-definition sets, far smaller than Sven's HD models/ of the same names (profile "model_dirs")
        p = None
        for d in self.prof.get("model_dirs", ()):
            p = p or self.find("models", d + "/" + name)
        p = p or self.find("models", name)
        if not p:
            return None
        try:
            return Mdl(p)
        except (ValueError, struct.error, OSError, IndexError) as e:
            log(f"{name}: unreadable ({e})")
            return None

    def tex_keys(self, mdl, is_view, cap_key="pw_tex_max"):
        keys = {}
        # svencoop: P/W (held / world) textures halved down to the profile's pw_tex_max side, v_ ones to v_tex_max
        cap = self.prof.get(cap_key) if (not is_view or cap_key != "pw_tex_max") else None
        for i, t in enumerate(mdl.textures):
            rgba = t.rgba()
            h = hashlib.sha1(struct.pack("<ii", t.width, t.height) + rgba).hexdigest()
            if h not in self.textures:
                tw, th = t.width, t.height
                while cap and max(tw, th) > cap and min(tw, th) >= 16:
                    rgba = half_rgba(rgba, tw, th)
                    tw, th = tw // 2, th // 2
                if (tw, th) != (t.width, t.height):
                    log(f"texture cap {cap}: {os.path.basename(mdl.path)}:{t.name} {t.width}x{t.height} → {tw}x{th}")
                self.textures[h] = [tw, th, rgba, f"{os.path.basename(mdl.path)}:{t.name}", is_view]
            elif not is_view:
                self.textures[h][4] = False          # also used by a P/W model → never downscale it
            keys[i] = h
        return keys

    def sound(self, rel):
        """Load sound/<rel> once → index, or -1."""
        rel = rel.replace("\\", "/").lstrip("/")
        if rel in self.sound_ix:
            return self.sound_ix[rel]
        idx = -1
        if ".." not in rel.split("/") and not os.path.isabs(rel):
            p = self.find("sound", rel)
            if p:
                try:
                    rate, data = read_wav(p, self.max_clip_s)
                    idx = len(self.sounds)
                    self.sounds.append((rel, rate, data))
                except (wave.Error, EOFError, OSError, ValueError) as e:
                    log(f"sound {rel}: unreadable ({e})")
            else:
                self.missing.append("sound/" + rel)
        self.sound_ix[rel] = idx
        return idx

    def sprite(self, rel):
        p = self.find("sprites", rel.split("/", 1)[1]) if rel.startswith("sprites/") else None
        if not p:
            self.missing.append(rel)
            return
        try:
            self.sprites.append((rel, spr.load(p)))
        except (ValueError, struct.error, OSError) as e:
            log(f"{rel}: unreadable ({e})")

    def fx_sprite(self, rel):
        """Effect sprite candidate for SPR2. Names are stored lower-case (the game's file lookup is case-insensitive);
        duplicates are skipped."""
        want = rel
        rel = rel.lower()
        if any(r == rel for r, _s in self.fx_sprites):
            return
        p = self.find("sprites", want.split("/", 1)[1]) if rel.startswith("sprites/") else None
        if not p:
            if want in FX_SPRITES or any(want in v for v in FX_SPRITES_MOD.values()):
                self.missing.append(want)
            return
        try:
            s = spr.load(p)
            if rel in FX_HALF.get(self.mod, ()):
                s = spr.half_sprite(s, half_rgba)
            self.fx_sprites.append((rel, s))
        except (ValueError, struct.error, OSError) as e:
            log(f"{rel}: unreadable ({e})")

    # ---- per weapon ----
    def weapon(self, wid, stem, cls, snd, files=None):
        """files: per-model file overrides {"p"/"w"/"v": name or None (= the model does not exist)}."""
        classes, ext = self.prof["classes"], self.prof["anim_ext"]
        w = dict(id=wid, name=stem, meshP=-1, meshW=-1, meshV=-1, sounds=[-1] * len(KINDS), ev0=len(self.events),
                 nev=0, flags=0, muzzleP=(0.0, 0.0, 0.0), muzzleV=(0.0, 0.0, 0.0), reload_s=0.0, draw_s=0.0,
                 animClass=classes.index(ext[stem]) if stem in ext else -1, grip=IDENT)
        for kind, rel in snd.items():
            w["sounds"][K[kind]] = self.sound(rel)
        if w["sounds"][K["fire2"]] < 0:
            w["sounds"][K["fire2"]] = w["sounds"][K["fire"]]
        if cls in ("gun", "pistol"):
            w["sounds"][K["empty"]] = self.sound(self.prof["empty"][cls])
        files = dict(files or {})
        w["seq_attack"] = files.get("attack")      # per-weapon v_ attack sequence override (egon)
        held = cls not in ("armor", "item")          # equipment / pickups: world mesh only
        name = {k: files.get(k, f"{k}_{stem}.mdl") for k in ("p", "w", "v")}

        # P — held model in the hand-bone frame
        m = self.load_mdl(name["p"]) if held and name["p"] else None
        if m is not None:
            self.bake_p(m, w, cls)
        elif held and name["p"]:
            self.missing.append(f"models/{name['p']}")
        # W — world model
        m = self.load_mdl(name["w"]) if name["w"] else None
        wseq, wbody = files.get("w_pose", (0, None))     # the original's resting pose (sequence, {bodypart: model}); stand-ins use 0
        if m is None and name["w"] and standin_of(name["w"]):
            st, sc, _where = standin_of(name["w"])
            m = self.load_mdl(st + ".mdl")
            if m is not None:
                m.scale_uniform(sc)
                wseq, wbody = 0, None
                log(f"{name['w']}: not in the install, stand-in {st}.mdl")
        if m is not None:
            tk = self.tex_keys(m, False)
            w["meshW"] = self.add(make_mesh(1, wid, [collect(m.bake(min(wseq, len(m.sequences) - 1), 0, bodygroups=wbody), tk)],
                                            name["w"][:-4]))
        elif name["w"]:
            self.missing.append(f"models/{name['w']}")
        # V — view model, 4 key poses + sound events
        if self.with_view and held and name["v"]:
            m = self.load_mdl(name["v"])
            if m is not None:
                self.bake_v(m, w, stem)
            else:
                self.missing.append(f"models/{name['v']}")
        w["nev"] = len(self.events) - w["ev0"]
        self.weapons.append(w)

    def add(self, mesh):
        self.meshes.append(mesh)
        return len(self.meshes) - 1

    def bake_p(self, m, w, cls):
        stem = w["name"]
        si = seq_pick(m, ("idle",)) or 0
        world = m.pose(si, 0)
        hand = [i for i, b in enumerate(m.bones) if b["name"].lower().endswith("r hand")]
        keep, frame = None, None
        psrc, pcen = "hand bone frame", None
        arm = [i for i, b in enumerate(m.bones) if b["name"].lower().endswith("r arm2")]
        if hand:
            keep = descendants(m, hand[0])
            frame = rigid_inverse(world[hand[0]])
            if arm and self._arm2hand is None:
                self._arm2hand = mat_mul(rigid_inverse(world[arm[0]]), world[hand[0]])   # forearm → wrist, rest pose
        elif arm and self._arm2hand is not None:
            # HL p_egon stops at the forearm: put it in the wrist frame of the other p_ models (same player skeleton)
            keep = descendants(m, arm[0])
            frame = rigid_inverse(mat_mul(world[arm[0]], self._arm2hand))
            psrc = "forearm + rest-pose wrist offset"
            log(f"p_{stem}: no 'R Hand' bone — forearm + the rest-pose wrist offset of the other p_ models")
        else:
            log(f"p_{stem}: no 'R Hand' bone — model space kept")
            # no hand frame: the mesh origin is the model origin, not a wrist. Hold it by the middle of the bone that
            # carries most of its vertices (a medkit's case, a gun's receiver)
            cnt, acc = {}, {}
            for sm in m.bake(si, 0):
                for tri in sm["tris"]:
                    for pos, _n, _s, _t, bone, _key in tri:
                        cnt[bone] = cnt.get(bone, 0) + 1
                        a = acc.setdefault(bone, [0.0, 0.0, 0.0])
                        for k in range(3):
                            a[k] += pos[k]
            if cnt:
                bb = max(cnt, key=cnt.get)
                pcen = tuple(acc[bb][k] / cnt[bb] for k in range(3))
                psrc = f"main bone '{m.bones[bb]['name']}' centre"
        tk = self.tex_keys(m, False)
        pose = collect(m.bake(si, 0), tk, keep, frame)
        # muzzle from the first attachment on a kept bone
        muzzle, from_att = None, False
        for a in m.attachments:
            if keep is None or a["bone"] in keep:
                p = xform(world[a["bone"]], a["org"])
                muzzle = xform(frame, p) if frame else p
                from_att = True
                break
        pts = [v[0] for v in pose[1].values()]
        if cls in ("gun", "pistol") and pts:
            # barrel axis, iterated: rotate the mesh so the current axis estimate is +x, take the vertices at or above
            # the muzzle height (barrel, slide, receiver top, sights — not the grip or magazine), refit their principal
            # axis (PCA), repeat. Rifles converge to the receiver/barrel line, pistols to the slide.
            d = (1.0, 0.0, 0.0)
            near = []
            for _it in range(6):
                R = rot_to_x(d)
                rp = [xform(R, p) for p in pts]
                mz = xform(R, muzzle) if muzzle is not None else front_point(rp)
                near = [p for p in rp if p[2] >= mz[2] - 1.0]
                if len(near) < 4:
                    break
                dl = principal_axis(near)
                Rt = [R[0], R[4], R[8], 0, R[1], R[5], R[9], 0, R[2], R[6], R[10], 0]     # R^-1 (rotation only)
                d = norm(rotate(Rt, dl))
            ang = math.degrees(math.acos(max(-1.0, min(1.0, d[0]))))
            if ang < 25.0:
                R = rot_to_x(d)
                order, data, tris, uvi = pose
                data = {k: (xform(R, p), rotate(R, n)) for k, (p, n) in data.items()}
                pose = (order, data, tris, uvi)
                if muzzle is not None:
                    muzzle = xform(R, muzzle)
                pts = [v[0] for v in data.values()]
                if pcen is not None:
                    pcen = xform(R, pcen)
                w["flags"] |= 4
                w["grip"] = R
                log(f"p_{stem}: barrel levelled by {ang:.1f}° ({len(near)} barrel vertices)")
            else:
                log(f"p_{stem}: barrel axis {ang:.1f}° off — not levelled")
        if muzzle is None and pts:
            if cls in ("gun", "pistol"):
                muzzle = front_point(pts)
            elif cls == "knife":
                muzzle = max(pts, key=lambda p: dot(p, p))
            else:
                muzzle = tuple(sum(p[k] for p in pts) / len(pts) for k in range(3))
        if from_att:
            w["flags"] |= 1
        w["muzzleP"] = to_unity(muzzle) if muzzle else (0.0, 0.0, 0.0)
        # p_ grip point in the P mesh frame (PGRP chunk): the wrist = the origin when the mesh is in a hand frame
        self.grips.setdefault(w["id"], {}).update(psrc=psrc, pgrip=to_unity(pcen) if pcen else (0.0, 0.0, 0.0))
        w["meshP"] = self.add(make_mesh(0, w["id"], [pose], f"p_{stem}"))

    def bake_view_anim(self, m, w, stem, anim, si, kmax, tk, mirror, rig=None, probe=False):
        """VAN1: one v_ sequence as adaptive key frames (greedy: split the interval whose linear in-between is worst,
        until kmax keys or < VAN_TOL units off) → its own kind-2 mesh (≤ 16 poses, so pre-VAN1 loaders still accept
        and simply ignore it). The loader shows key k blended towards k+1 (_PoseBlend) at the source fps."""
        sq = m.sequences[si]
        n = sq["numframes"]
        if n < 2:
            return
        fps = sq["fps"] if sq["fps"] > 0 else 30.0
        frames = {f: collect(self.vbake(m, si, f), tk, frame=mirror) for f in range(n)}
        order = frames[0][0]
        samp = order[::5] or order
        P = {f: [frames[f][1][k][0] for k in samp] + (self.sil_probe(m, si, f, mirror) if probe and self._silbone is not None else []) for f in range(n)}

        def err(a, b, f):
            t = (f - a) / (b - a)
            worst = 0.0
            for pa, pb, pf in zip(P[a], P[b], P[f]):
                e = sum((pf[c] - (pa[c] + (pb[c] - pa[c]) * t)) ** 2 for c in range(3))
                if e > worst:
                    worst = e
            return worst ** 0.5
        keys = [0, n - 1]
        while len(keys) < kmax:
            we, wf = 0.0, None
            for a, b in zip(keys, keys[1:]):
                for f in range(a + 1, b):
                    e = err(a, b, f)
                    if e > we:
                        we, wf = e, f
            if wf is None or we < VAN_TOL:
                break
            keys.append(wf)
            keys.sort()
        mi = self.add(make_mesh(2, w["id"], [frames[f] for f in keys],
                                f"v_{stem} anim {('reload', 'draw', 'attack', 'attack2', 'attack3', 'attack_last', 'silencer_on', 'silencer_off')[anim] if anim < 8 else 'seq%d' % (anim - VIEW_SEQ0)} '{sq['label']}' {n} frames -> keys {keys}"))
        self.vanims.append((w["id"], anim, mi, keys, [f / fps for f in keys]))
        if rig is not None:
            self.add_view_rig(m, w, mi, rig, [(si, f) for f in keys], mirror)
        self.add_sil_keys(m, mi, [(si, f) for f in keys], mirror)

    # ── VR: the v_ model held in the controller (GsvrCsWeapon): the gun's main bone per pose (to keep the gun fixed in
    #    the hand while the anim plays), the trigger hand's idle position (grip point) and the arm/hand vertices
    #    (Bip01* bones, hidden in VR where the real hands are the controllers) ──
    def view_rig(self, m, idle, mirror, w=None):
        subs = self.vbake(m, idle, 0)
        names = [b["name"] for b in m.bones]
        # arms / hands by texture (every CS v_ model: view_glove / view_skin / view_finger, knife_r: newhand; HL: the HEV
        # glove set GLOVED_sleeve / rubbergloveCHROME / GLOVED_knuckle / GLOVE_handpak, shotgun HAND_ForeArm1, xbow_sleeve;
        # Sven: soldier_arm / soldier_hand, ld_hev_arm / ld_hev_glove, displacer hand / skin; see PROFILES arm_tex);
        # the bone names are not uniform (Bip01 … in some, Bone_Righthand / BoneNN / r_wrist / R_Hand in others)
        # + the shotgun's ejected-shell bodypart (texture shell.bmp, bone "shell" at the model origin = the eye): a 3 cm cylinder that
        # hangs at the eye in the desktop view (cut by the near plane there) and floats behind the controller in VR -> hidden like the arms
        armtex = {i for i, t in enumerate(m.textures) if any(k in t.name.lower() for k in self.prof["arm_tex"]) or t.name.lower() == "shell.bmp"}
        handtex = {i for i, t in enumerate(m.textures) if any(k in t.name.lower() for k in self.prof.get("hand_tex", ()))}
        count = {}
        keyarm = {}
        armverts = []                       # (bone, texture is a hand, Unity position) of every arm vertex (once per key)
        gunpts = []
        seen = set()
        for sm in subs:
            is_arm = sm["tex"] in armtex
            for tri in sm["tris"]:
                for _p, _n, _s, _t, bone, key in tri:
                    keyarm[key] = is_arm
                    if not is_arm:
                        count[bone] = count.get(bone, 0) + 1
                    if key not in seen:
                        seen.add(key)
                        up = to_unity(xform(mirror, _p) if mirror else _p)
                        if is_arm:
                            armverts.append((bone, sm["tex"] in handtex, up))
                        else:
                            gunpts.append(up)
        gun = max(count, key=count.get) if count else 0
        self._gunpts = gunpts
        pose0 = m.pose(idle, 0)
        if self.mod == "cstrike":
            grip, src = self.grip_legacy(m, names, pose0, gun, mirror), "legacy"
            gd = min((sum((p[k] - grip[k]) ** 2 for k in range(3)) for p in gunpts), default=0.0) ** 0.5
            if gd > CS_GRIP_MAX_GUN_DIST and gunpts:
                # the rear-most hand is the other one (knife: the blade sits in the hand that is not nearest the eye;
                # grenades: a hand bone parked away from the model) -> the hand bone / glove next to the gun mesh
                grip, src = self.grip_pick(m, names, pose0, gun, mirror, armverts, gunpts, near=CS_GRIP_MAX_GUN_DIST)
        else:
            grip, src = self.grip_pick(m, names, pose0, gun, mirror, armverts, gunpts)
        na = sum(1 for v in keyarm.values() if v)
        gd = min((sum((p[k] - grip[k]) ** 2 for k in range(3)) for p in gunpts), default=0.0) ** 0.5
        if self.mod != "cstrike" and gd > GRIP_MAX_GUN_DIST and gunpts:
            # the chosen hand is nowhere near the gun (hidden / parked hand bone): hold the gun by its middle instead
            grip = self.gun_centre(gunpts)
            src += f"; was {gd * 100:.0f} cm from the gun -> gun centre"
            gd = min((sum((p[k] - grip[k]) ** 2 for k in range(3)) for p in gunpts)) ** 0.5
        log(f"  VR rig: gun bone '{names[gun]}', grip ({grip[0]:.3f}, {grip[1]:.3f}, {grip[2]:.3f}) m [{src}], "
            f"{gd * 100:.0f} cm from the gun mesh, arm verts {na}/{len(keyarm)}")
        if w is not None:
            self.grips.setdefault(w["id"], {}).update(vsrc=src, vbone=names[gun], vgrip=tuple(grip), armverts=na, gundist=gd)
        return gun, keyarm, grip

    def grip_legacy(self, m, names, pose0, gun, mirror):
        """cstrike (unchanged): the hand bone nearest the eye (the trigger hand sits behind the fore grip)."""
        hands = [i for i, n in enumerate(names) if n in ("Bip01 R Hand", "Bip01 L Hand", "Bone_Righthand", "Bone_Lefthand")]
        grip = None
        best = None
        for hi in hands:
            mm = self.view_matrix(pose0[hi], mirror)
            pz = mm[1][2]
            if best is None or pz < best:
                best, grip = pz, mm[1]
        if grip is None:
            grip = self.view_matrix(pose0[gun], mirror)[1]
        return grip

    @staticmethod
    def is_hand_bone(name):
        n = name.lower().strip()
        return "finger" not in n and (n.endswith("hand") or n.endswith("wrist"))

    @staticmethod
    def hand_visible(p):
        """A hand that is parked outside the view (HL: the off-hand of one-handed weapons hangs below the screen)."""
        return p[1] > -0.5 and p[2] > -0.05 and abs(p[0]) < 0.6

    def grip_pick(self, m, names, pose0, gun, mirror, armverts, gunpts, near=None):
        """valve / svencoop: the trigger hand's wrist in the v_ mesh frame. Order: (1) a hand-named bone (Bip01 R Hand,
        R_Hand, r_wrist, Hands biped R Hand …) that is inside the view and owns arm vertices; the rear-most wins (the
        trigger hand sits behind the fore grip), the right one on ties; (2) no such bone: the cluster of hand-texture
        vertices that is inside the view, rear-most, minus the mean wrist-to-glove offset; (3) the visible hand bones
        without arm vertices (arm-less models); (4) the centre of the gun's main bone (last resort)."""
        hands = [i for i, n in enumerate(names) if self.is_hand_bone(n)]
        pos = {hi: self.view_matrix(pose0[hi], mirror)[1] for hi in hands}
        owned = {}
        for bone, _h, _p in armverts:
            b = bone
            while b >= 0 and b not in pos:
                b = m.bones[b]["parent"]
            if b >= 0:
                owned[b] = owned.get(b, 0) + 1
        vis = [hi for hi in hands if self.hand_visible(pos[hi])]
        if near is not None and gunpts:
            # cstrike: only hands that are really at the gun (the knife is held by one hand only)
            def gdist(p):
                return min(sum((q[k] - p[k]) ** 2 for k in range(3)) for q in gunpts) ** 0.5
            vis = [hi for hi in vis if gdist(pos[hi]) <= near] or vis

        def rearmost(cands):
            zmin = min(pos[c][2] for c in cands)
            near = [c for c in cands if pos[c][2] <= zmin + 0.03]
            return max(near, key=lambda c: pos[c][0])
        withv = [hi for hi in vis if owned.get(hi, 0) >= 20]
        # the hand the gun is attached to carries it (v_shotgun / v_egon / v_rpg: the trigger hand has no glove vertices, the
        # rear-most hand WITH vertices is the pump / support hand 25-35 cm away from the gun's grip)
        b = gun
        while b >= 0 and b not in pos:
            b = m.bones[b]["parent"]
        if b >= 0 and b in vis and near is None:
            return pos[b], "hand-bone '" + names[b] + "' (carries the gun)"
        if near is None and len(withv) >= 2:
            # an object held in both hands (tripmine): the middle between the two, not one hand at its edge
            for i1 in withv:
                for i2 in withv:
                    p1, p2 = pos[i1], pos[i2]
                    if i1 < i2 and abs(p1[0] - p2[0]) > 0.2 and abs(p1[1] - p2[1]) < 0.1 and abs(p1[2] - p2[2]) < 0.1 \
                            and not any(m.bones[g]["parent"] in (i1, i2) for g in [gun]):
                        return tuple((p1[k] + p2[k]) * 0.5 for k in range(3)), "hand-bone midpoint '" + names[i1] + "' + '" + names[i2] + "' (two-handed)"
        if withv:
            hi = rearmost(withv)
            return pos[hi], "hand-bone '" + names[hi] + "'"
        if not armverts and vis:
            hi = rearmost(vis)
            return pos[hi], "hand-bone '" + names[hi] + "' (arm-less model)"
        hv = [p for _b, h, p in armverts if h] or [p for _b, _h, p in armverts]
        clusters = self.cluster(hv, 0.05)
        best = None
        for c in clusters:
            if len(c) < 30:
                continue
            ctr = tuple(sum(p[k] for p in c) / len(c) for k in range(3))
            if self.hand_visible(ctr) and (best is None or ctr[2] < best[2]):
                best = ctr
        if best is not None:
            return (best[0] + HAND_TO_WRIST[0], best[1] + HAND_TO_WRIST[1], best[2] + HAND_TO_WRIST[2]), "glove cluster"
        if vis:
            hi = rearmost(vis)
            return pos[hi], "hand-bone '" + names[hi] + "' (no arm vertices)"
        if gunpts:
            return self.gun_centre(gunpts), "gun centre (no hand found)"
        return self.view_matrix(pose0[gun], mirror)[1], "gun bone origin (no hand found)"

    def muzzle_pick(self, m, world, mirror, gunpts, use_att=True):
        """valve / svencoop v_ muzzle (Unity view frame): attachments are not always the muzzle (Sven models list shell
        ejection / effect points first, the displacer four tips, the shock rifle tentacles), so an attachment counts only
        if it sits at the front of the gun mesh and near its barrel axis; the one furthest forward wins. Else the front
        of the gun mesh on its axis. Returns (xyz, from attachment, source text)."""
        if not gunpts:
            return (0.0, 0.0, 0.0), False, "none"
        zs = sorted(p[2] for p in gunpts)
        zmax = zs[min(len(zs) - 1, int(len(zs) * 0.995))]
        front = [p for p in gunpts if p[2] >= zmax - 0.15]
        ax = sum(p[0] for p in front) / len(front)
        ay = sum(p[1] for p in front) / len(front)
        best = None
        for i, a in enumerate(m.attachments if use_att else ()):
            p = to_unity(xform(mirror, xform(world[a["bone"]], a["org"])) if mirror else xform(world[a["bone"]], a["org"]))
            if abs(p[0] - ax) < 0.3 and abs(p[1] - ay) < 0.3 and zmax - 0.45 <= p[2] <= zmax + 0.15:
                if best is None or p[2] > best[1][2]:
                    best = (i, p)
        if best is not None:
            return best[1], True, f"attachment {best[0]}"
        return (ax, ay, zmax), False, "gun mesh front" + (" (attachments rejected)" if m.attachments else "")

    @staticmethod
    def gun_centre(gunpts):
        """Last resort: the middle of the rear half of the gun mesh, lowered a little (where a hand would hold it)."""
        zs = sorted(p[2] for p in gunpts)
        mid = [p for p in gunpts if p[2] <= zs[len(zs) // 2]]
        ctr = tuple(sum(p[k] for p in mid) / len(mid) for k in range(3))
        return (ctr[0], ctr[1] - 0.04, ctr[2])

    @staticmethod
    def cluster(points, cell):
        """Connected components of points (grid hashing, 26-neighbourhood of cells `cell` m wide)."""
        grid = {}
        for i, p in enumerate(points):
            grid.setdefault((int(p[0] // cell), int(p[1] // cell), int(p[2] // cell)), []).append(i)
        seen, out = set(), []
        for start in grid:
            if start in seen:
                continue
            seen.add(start)
            stack, comp = [start], []
            while stack:
                c = stack.pop()
                comp.extend(points[i] for i in grid[c])
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        for dz in (-1, 0, 1):
                            n = (c[0] + dx, c[1] + dy, c[2] + dz)
                            if n in grid and n not in seen:
                                seen.add(n)
                                stack.append(n)
            out.append(comp)
        return out

    @staticmethod
    def view_matrix(mat, mirror):
        """GoldSrc bone matrix (3x4) → (Unity rotation quat, Unity position m) in the (mirrored) v_ mesh frame."""
        mm = list(mat)
        if mirror:
            # M·G·M with M = diag(1, −1, 1): flip the sign where exactly one index is y, and the y translation
            for r in range(3):
                for c in range(3):
                    if (r == 1) != (c == 1):
                        mm[r * 4 + c] = -mm[r * 4 + c]
            mm[7] = -mm[7]
        return quat_from_rows(unity_rotation(mm)), to_unity((mm[3], mm[7], mm[11]))

    def add_view_rig(self, m, w, mi, rig, poses, mirror):
        gun, keyarm, grip = rig
        mesh = self.meshes[mi]
        frames = []
        for si, f in poses:
            q, pos = self.view_matrix(m.pose(si, f)[gun], mirror)
            frames.append((pos, q))
        self.vguns.append((mi, w["id"], grip, frames))
        self.varms.append((mi, [1 if keyarm.get(k) else 0 for k in mesh.order]))

    _vdrop = frozenset()

    def bake_silencer(self, m, w, stem, gun, tk, mirror, wl):
        """The silencer part (_vdrop textures) of the model's own silenced idle pose as a one-pose kind-5 mesh in the frame
        of the gun bone (mirrored like the gun), so it rides the gun in every pose. Returns the silenced muzzle (attachment 0
        in the unsilenced idle pose frame, Unity) or None when the model has no silenced idle."""
        si = m.find_sequence("idle")
        if si is None or si < 0 or "unsil" in m.sequences[si]["label"].lower():
            return None
        subs = [sm for sm in m.bake(si, 0) if sm["tex"] in self._vdrop]
        if not subs:
            return None
        cnt = {}
        for sm in subs:
            for tri in sm["tris"]:
                for v in tri:
                    cnt[v[4]] = cnt.get(v[4], 0) + 1
        sb = max(cnt, key=cnt.get)           # the silencer is rigid on one bone of its own (it moves against the gun in attach / detach)
        self._silbone = sb
        g = m.pose(si, 0)[sb]
        frame = mat_mul(mirror, rigid_inverse(g)) if mirror else rigid_inverse(g)
        mi = self.add(make_mesh(5, w["id"], [collect(subs, tk, frame=frame)], f"v_{stem} silencer (gun bone frame)"))
        mz = ()
        if m.attachments:
            a = m.attachments[0]
            mp = xform(wl[a["bone"]], a["org"])
            mz = tuple(to_unity(xform(mirror, mp) if mirror else mp))
        self.vsils.append((w["id"], mi, mz if mz else (0.0, 0.0, 0.0)))
        return mz

    @staticmethod
    def sil_counterpart(m, si):
        """The silenced twin of an *_unsil sequence (the ones we bake the gun from): the silencer bone of the twin is where the
        silencer really is. Sequences without the suffix (add_silencer / detach_silencer) are their own twin."""
        lab = m.sequences[si]["label"].lower()
        if lab.endswith("_unsil"):
            for j, q in enumerate(m.sequences):
                if q["label"].lower() == lab[:-6]:
                    return j
        return si

    def sil_probe(self, m, si, f, mirror):
        """Four points on the silencer bone (origin + 8 units along each axis) in the baked (mirrored) mesh frame: they steer the
        key frame choice so the silencer's travel in attach / detach is sampled as well as the gun's."""
        ss = self.sil_counterpart(m, si)
        pm = m.pose(ss, min(f, m.sequences[ss]["numframes"] - 1))[self._silbone]
        pts = [xform(pm, q) for q in ((0, 0, 0), (8, 0, 0), (0, 8, 0), (0, 0, 8))]
        return [xform(mirror, q) for q in pts] if mirror else pts

    def add_sil_keys(self, m, mi, poses, mirror):
        """VSKY row of v_ mesh `mi`: the silencer bone's pose (view mesh frame, like VGUN) at each (sequence, frame) of the mesh's
        poses / keys, taken from the silenced twin of the sequence. Only for models that have a silencer mesh."""
        if self._silbone is None:
            return
        frames = []
        for si, f in poses:
            ss = self.sil_counterpart(m, si)
            q, pos = self.view_matrix(m.pose(ss, min(f, m.sequences[ss]["numframes"] - 1))[self._silbone], mirror)
            frames.append((pos, q))
        self.vsky.append((mi, frames))

    def vbake(self, m, si, f):
        """m.bake() for a v_ pose minus the parts that are not on the gun in the unsilenced poses we bake (bake_v)."""
        subs = m.bake(si, f)
        if self._vdrop:
            subs = [sm for sm in subs if sm["tex"] not in self._vdrop]
        return [self.chrome_uv(m, sm) for sm in subs]

    @staticmethod
    def chrome_uv(m, sm):
        """STUDIO_NF_CHROME textures (the HL crowbar / shotgun metal and every rubber glove, CS view_glove) store ONE constant
        (s, t) per vertex: the engine ignores it and env-maps the texture from the vertex normal in view space (R_StudioChrome).
        Without that the whole surface samples one texel = a flat, untextured grey/black mesh (2026-11-05, v_crowbar).
        View models are in the eye frame (x forward, y left, z up): s from the right component (-y), t from the up one (z)."""
        ti = sm["tex"]
        if not (0 <= ti < len(m.textures)) or not (m.textures[ti].flags & 2):
            return sm
        tris = []
        for tri in sm["tris"]:
            tris.append(tuple((pos, nrm, 0.5 - 0.5 * nrm[1], 0.5 - 0.5 * nrm[2], bone, key) for pos, nrm, _s, _t, bone, key in tri))
        return dict(sm, tris=tris)

    def bake_v(self, m, w, stem):
        tk = self.tex_keys(m, True, "v_tex_max")
        idle = seq_pick(m, self.prof["view_seqs"]["idle"])
        idle = 0 if idle is None else idle
        # CS USP / M4A1: the silencer is a mesh part of the one body, shown or hidden by the animation alone. The
        # *_unsil sequences (the ones we bake) park it behind the gun (USP ~18 in = 45 cm behind the barrel end, M4A1
        # ~30 in = 77 cm), where the flat-screen view never shows it but in VR it floats behind the pistol. No silenced pose is
        # ever baked, so the part is dropped from every pose / anim of this model (they share one topology).
        self._vdrop = frozenset()
        self._silbone = None
        if m.sequences and "unsil" in m.sequences[idle]["label"].lower():
            self._vdrop = frozenset(i for i, t in enumerate(m.textures) if "silencer" in t.name.lower())
            if self._vdrop:
                log(f"  v_{stem}: silencer part dropped (parked behind the gun in the unsilenced poses)")
        att = seq_pick(m, w.get("seq_attack") or self.prof["view_seqs"]["attack"])
        # CS: random shoot sequences per shot (SHOOT_SEQS, cs client.so); HL keeps its single attack anim
        shoot, shoot_last = SHOOT_SEQS.get(stem, ([], None)) if self.mod == "cstrike" else ([], None)
        shoot = [i for i in shoot if i < len(m.sequences)]
        if shoot_last is not None and shoot_last >= len(m.sequences):
            shoot_last = None
        if shoot:
            att = shoot[0]
        rel = seq_pick(m, self.prof["view_seqs"]["reload"])
        drw = seq_pick(m, self.prof["view_seqs"]["draw"])
        # shell-by-shell reload: CS "insert", HL "reload" after "start_reload" (v_shotgun)
        shotgun = rel is not None and (m.sequences[rel]["label"].lower() == "insert"
                                       or seq_pick(m, ("start_reload",)) is not None)
        poses = [(idle, 0)]
        if att is not None:
            n = m.sequences[att]["numframes"]
            lab = m.sequences[att]["label"].lower()
            poses.append((att, min(2, n - 1) if lab.startswith("shoot") else n // 3))
        else:
            poses.append((idle, 0))
        if rel is not None:
            sq = m.sequences[rel]
            n = sq["numframes"]
            evs = sound_events(sq)
            if shotgun:
                a = seq_pick(m, ("start_reload",))
                b = seq_pick(m, ("after_reload",))
                poses.append((a if a is not None else rel, (m.sequences[a]["numframes"] - 1) if a is not None else n // 2))
                poses.append((rel, n // 2) if b is None else (b, m.sequences[b]["numframes"] // 3))
            elif evs and len(evs) >= 2 and sq["label"].lower().startswith("reload"):
                poses.append((rel, max(0, min(n - 1, evs[0][0] + 2))))
                poses.append((rel, max(0, min(n - 1, evs[-1][0]))))
            else:
                poses.append((rel, n // 4 if sq["label"].lower().startswith("reload") else n // 2))
                poses.append((rel, (3 * n) // 4 if sq["label"].lower().startswith("reload") else n - 1))
        else:
            poses += [(idle, 0), (idle, 0)]
        # CS view models are authored left-handed (gun at +y = left; the CS client mirrors them for the default
        # cl_righthand 1, Half-Life's own v_ models sit at -y). Mirror y so the pack shows the familiar right-handed
        # view; the winding vote in make_mesh() re-orients the mirrored triangles.
        mirror = [1, 0, 0, 0, 0, -1, 0, 0, 0, 0, 1, 0] if MIRROR_VIEW.get(self.mod) else None
        baked = [collect(self.vbake(m, s, f), tk, frame=mirror) for s, f in poses]
        names = [f"{m.sequences[s]['label']}@{f}" if m.sequences else "-" for s, f in poses]
        w["meshV"] = self.add(make_mesh(2, w["id"], baked, f"v_{stem} [{', '.join(names)}]"))
        rig = self.view_rig(m, idle, mirror, w)
        self.add_view_rig(m, w, w["meshV"], rig, poses, mirror)
        wl = m.pose(idle, 0)
        silmz = self.bake_silencer(m, w, stem, rig[0], tk, mirror, wl) if self._vdrop else None
        if silmz is None:
            self._silbone = None
        else:
            self.add_sil_keys(m, w["meshV"], poses, mirror)
        if self.mod != "cstrike":
            mz, from_att, msrc = self.muzzle_pick(m, wl, mirror, self._gunpts)
            w["muzzleV"] = tuple(mz)
            if from_att:
                w["flags"] |= 2
            md = min((sum((q[k] - mz[k]) ** 2 for k in range(3)) for q in self._gunpts), default=0.0) ** 0.5
            self.grips.setdefault(w["id"], {}).update(msrc=msrc, vmuzzle=tuple(w["muzzleV"]), mzdist=md)
            log(f"  v_ muzzle ({mz[0]:.3f}, {mz[1]:.3f}, {mz[2]:.3f}) m [{msrc}]")
        else:
            msrc = "none"
            if m.attachments:
                a = m.attachments[0]
                mp = xform(wl[a["bone"]], a["org"])
                w["muzzleV"] = to_unity(xform(mirror, mp) if mirror else mp)
                w["flags"] |= 2
                msrc = "attachment 0"
                # USP / M4A1: attachment 0 is the muzzle of the silenced gun; with the silencer part dropped (vbake) the
                # barrel ends where the mesh does -> the muzzle flash goes to the gun mesh front on the barrel axis
                gp = self._gunpts
                md = min((sum((q[k] - w["muzzleV"][k]) ** 2 for k in range(3)) for q in gp), default=0.0) ** 0.5
                if self._vdrop and gp and md > CS_MUZZLE_MAX_GUN_DIST:
                    w["muzzleV"], _fa, _s = self.muzzle_pick(m, wl, mirror, gp, use_att=False)
                    w["flags"] &= ~2
                    msrc = f"gun mesh front (attachment 0 was {md * 100:.0f} cm off)"
            else:
                w["muzzleV"] = to_unity(front_point([v[0] for v in baked[0][1].values()]))
            md = min((sum((q[k] - w["muzzleV"][k]) ** 2 for k in range(3)) for q in self._gunpts), default=0.0) ** 0.5
            if silmz is not None:
                self.vsils[-1] = (self.vsils[-1][0], self.vsils[-1][1], silmz if silmz != () else tuple(w["muzzleV"]))
            self.grips.setdefault(w["id"], {}).update(msrc=msrc, vmuzzle=tuple(w["muzzleV"]), mzdist=md,
                                                      silencer="separate mesh" if silmz is not None else ("dropped" if self._vdrop else ""))
            log(f"  v_ muzzle ({w['muzzleV'][0]:.3f}, {w['muzzleV'][1]:.3f}, {w['muzzleV'][2]:.3f}) m [{msrc}]")
        if self.view_anims:
            for anim, si, kmax in ((0, rel, VAN_KEYS[0]), (1, drw, VAN_KEYS[1]), (2, att, VAN_KEYS[2])):
                if si is None or (anim == 0 and shotgun):
                    continue                     # shotguns reload shell by shell (start / insert / after): key poses stay
                self.bake_view_anim(m, w, stem, anim, si, kmax, tk, mirror, rig)
            # shoot variants 2 / 3 (anim 3, 4) and the last-round shot (anim 5); attack sound events stay on anim 2
            extra = [(3 + j, si) for j, si in enumerate(shoot[1:3])] + ([(5, shoot_last)] if shoot_last is not None else [])
            for anim, si in extra:
                self.bake_view_anim(m, w, stem, anim, si, VAN_SHOOT_KEYS, tk, mirror, rig)
            if self.mod != "cstrike":
                for anim, si, label, kmax in VIEW_SEQ_EXTRA.get(stem, ()):
                    if si < len(m.sequences) and m.sequences[si]["label"].lower() == label:
                        self.bake_view_anim(m, w, stem, anim, si, kmax, tk, mirror, rig)
        # attach / detach silencer (anim 6 / 7): the v_ model's own add_silencer / detach_silencer sequences (CS USP: 7 / 15, M4A1: 6 / 13)
        silseq = []
        if silmz is not None:
            silseq = [(6 + j, seq_pick(m, (lab,))) for j, lab in enumerate(("add_silencer", "detach_silencer"))]
            if self.view_anims:
                for anim, si in silseq:
                    if si is not None:
                        self.bake_view_anim(m, w, stem, anim, si, VAN_SIL_KEYS, tk, mirror, rig, probe=True)
        # sound events: reload (0), draw (1), attack (2), silencer on / off (6, 7)
        for anim, si in ((0, rel), (1, drw), (2, att)) + tuple(silseq):
            if si is None:
                continue
            sq = m.sequences[si]
            fps = sq["fps"] if sq["fps"] > 0 else 30.0
            if anim == 0:
                w["reload_s"] = sq["numframes"] / fps
            if anim == 1:
                w["draw_s"] = sq["numframes"] / fps
            for fr, opt in sound_events(sq):
                s = self.sound(opt)
                if s >= 0:
                    self.events.append((anim, int(round(fr / fps * 1000)), s, fr))
            if anim == 0:
                rs = []
                for _a, _ms, s, _f in self.events[w["ev0"]:]:
                    if _a == 0 and s not in rs:
                        rs.append(s)
                for j, s in enumerate(rs[:3]):
                    w["sounds"][K["reload1"] + j] = s
            if anim == 1 and w["sounds"][K["draw"]] < 0:
                for _a, _ms, s, _f in self.events[w["ev0"]:]:
                    if _a == 1:
                        w["sounds"][K["draw"]] = s
                        break
        # extra view anims with their own sound events (VIEW_SEQ_EVENTS), only when the sequence was baked above
        if self.view_anims and self.mod != "cstrike":
            for anim, si, label, _kmax in VIEW_SEQ_EXTRA.get(stem, ()):
                if anim not in VIEW_SEQ_EVENTS.get(stem, ()) or si >= len(m.sequences) or m.sequences[si]["label"].lower() != label:
                    continue
                sq = m.sequences[si]
                fps = sq["fps"] if sq["fps"] > 0 else 30.0
                for fr, opt in sound_events(sq):
                    s = self.sound(opt)
                    if s >= 0:
                        self.events.append((anim, int(round(fr / fps * 1000)), s, fr))

    # ---- player models (v2) ----
    def player(self, name, team):
        """Bakes models/player/<name>/<name>.mdl into ONE mesh (kind 3) with every pose of every anim record."""
        p = self.find("models", f"player/{name}/{name}.mdl")
        m = None
        if p:
            try:
                m = Mdl(p)
            except (ValueError, struct.error, OSError, IndexError) as e:
                log(f"player {name}: unreadable ({e})")
        if m is None or not m.sequences:
            self.missing.append(f"models/player/{name}/{name}.mdl")
            return
        hand = [i for i, b in enumerate(m.bones) if b["name"].lower() == "bip01 r hand"]
        hand = hand[0] if hand else -1
        tk = self.tex_keys(m, False)
        poses, hands, anims, labels = [], [], [], []

        def blend_of(si):
            nb = m.sequences[si]["numblends"]
            return 0 if nb <= 2 else nb // 2         # CS aim sequences: 3x3 grid (yaw x pitch), 4 = straight ahead

        def add_pose(upper, frame, gait, feet):
            shift = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, feet]
            bl = blend_of(upper)
            poses.append(collect(m.bake(upper, frame, blend=bl, gait=gait), tk, frame=shift))
            if hand >= 0:
                h = m.pose(upper, frame, bl, gait)[hand]
                t = to_unity((h[3], h[7], h[11] + feet))
                hands.append(t + quat_from_rows(unity_rotation(h)))
            else:
                hands.append((0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0))

        def period(si):
            n = m.sequences[si]["numframes"]
            a, b = m.pose(si, 0), m.pose(si, n - 1)
            same = all(abs(x - y) < 0.05 for ma, mb in zip(a, b) for x, y in zip(ma, mb))
            return max(1, n - 1 if same and n > 1 else n)

        for ci, cls in enumerate(self.prof["classes"]):
            if cls not in self.player_classes:
                continue
            seqs = [(aid, seq_pick(m, (up % cls,)), seq_pick(m, (gait,)), nf, cr)
                    for aid, up, gait, nf, cr in self.prof["class_anims"]]
            if any(s[1] is None or s[2] is None for s in seqs):
                log(f"player {name}: class {cls} incomplete — skipped (falls back to class 0 at runtime)")
                continue
            for aid, up, gs, nf, cr in seqs:
                first = len(poses)
                gq = m.sequences[gs]
                feet = FEET_CROUCH if cr else FEET_STAND
                un = m.sequences[up]["numframes"]
                uf = 0
                if aid in (A_SHOOT, A_CSHOOT):
                    uf = min(1, un - 1) if un <= 8 else un // 3        # recoil peak / mid slash / mid throw
                if nf == 1:
                    add_pose(up, uf, (gs, 0), feet)
                else:
                    per = period(gs)
                    for k in range(nf):
                        add_pose(up, uf, (gs, k * per / nf), feet)
                fps = gq["fps"] if gq["fps"] > 0 else 30.0
                dist = gq["linear"][0] if nf > 1 and gq["linear"][0] > 0 else 0.0
                anims.append((aid, ci, first, nf, dist, (period(gs) / fps) if nf > 1 else 0.0, 1))
            labels.append(cls)
        # deaths: whole-body sequences, identical copies dropped (this CS release ships death1..forward as one anim)
        seen = set()
        for aid, names, feet in self.prof["deaths"]:
            for nm in names:
                si = seq_pick(m, (nm,))
                if si is None:
                    continue
                sq = m.sequences[si]
                n = sq["numframes"]
                ends = [m.pose(si, f) for f in range(n)]
                sig = tuple(round(v, 1) for f in (n // 4, n // 2, n - 1) for v in ends[f][7][3:12:4])
                if sig in seen:
                    continue
                seen.add(sig)
                last = ends[-1]
                settle = n - 1
                while settle > 1 and all(abs(ends[settle - 1][b][c] - last[b][c]) < 0.5
                                         for b in range(len(m.bones)) for c in (3, 7, 11)):
                    settle -= 1
                first = len(poses)
                for k in range(DEATH_FRAMES):
                    f = n - 1 if k == DEATH_FRAMES - 1 else int(round(settle * (k + 1) / DEATH_FRAMES))
                    add_pose(si, f, None, feet)
                fps = sq["fps"] if sq["fps"] > 0 else 30.0
                anims.append((aid, -1, first, DEATH_FRAMES, 0.0, settle / fps, 0))
                labels.append(nm)
        # aliases (HL: no crouch death → A_CDEATH records reuse the first standing death's poses; no extra geometry)
        for aid, src in self.prof["death_alias"]:
            for a in list(anims):
                if a[0] == src:
                    anims.append((aid,) + a[1:])
                    labels.append(f"{ANIM_NAMES[aid]}={ANIM_NAMES[src]}")
                    break
        if not anims:
            self.missing.append(f"models/player/{name}/{name}.mdl (no usable sequences)")
            return
        pi = len(self.players)
        mi = self.add(make_mesh(3, pi, poses, f"player {name} [{len(anims)} anims, {len(poses)} poses: "
                                               f"{', '.join(labels)}]"))
        self.players.append(dict(name=name, team=team, mesh=mi, anim0=len(self.panims), nanim=len(anims),
                                 hand0=len(self.hands)))
        self.panims.extend(anims)
        self.hands.extend(hands)
        if self.skel and self.mod != "cstrike":
            self.player_skel(m, pi, mi, hand)

    def player_skel(self, m, pi, mesh, hand):
        """Skinned player body (GSX1 SKL1 kind-1 model row; the baked kind-3 mesh above stays for old loaders): the gait sequences
        (legs), the upper-body sequences of every packed weapon class (aim / shoot, standing / crouched, both pitch blends) and the
        deaths as bone texture key sets, feet at the origin like the baked poses. Sequence kinds: gait = the A_* of its class_anims
        entry (A_IDLE / A_WALK / A_RUN / A_CIDLE / A_CWALK), upper = 0 aim, 1 shoot, 2 crouch aim, 3 crouch shoot, death = A_DEATH."""
        sk = self.skp
        slots = studioskel.slots_of(m, log)
        legs = m.gait_bones()
        att = studioskel.attach_bone(m, legs)
        if att is None:
            log(f"player {self.players[pi]['name']}: the upper body does not hang from one gait bone - stays baked")
            return
        rows, nkey, seqs, done = [], 0, [], set()

        def add(si, role, kind, cls, feet):
            nonlocal nkey
            sq = m.sequences[si]
            keys, sets = studioskel.sequence_keys(m, si)
            k0 = nkey
            for bl in sorted(sets):
                for vals, world in sets[bl]:
                    # upper-body sequences relative to the attach bone (the gait layer places them), the others absolute
                    rows.extend(studioskel.key_rows_rel(m, vals, world, slots, att) if role == 1 else studioskel.key_rows(m, vals, world, slots, feet))
                    nkey += 1
            n = max(1, sq["numframes"])
            fps = sq["fps"] if sq["fps"] > 0 else 30.0
            t0 = len(sk.ptimes)
            sk.ptimes.extend(f / fps for f in keys)
            two = 1 in sets
            seqs.append(([role, kind, cls, k0, len(keys), t0, k0 + len(keys) if two else -1],
                         [fps, (n - 1) / fps if n > 1 else 0.0, sq["linear"][0], float(sq["flags"] & 1),
                          sq["blendstart"][0] if two else 0.0, sq["blendend"][0] if two else 0.0, float(n), 0.0]))
        for aid, _up, gait, _nf, cr in self.prof["class_anims"]:
            if aid in (A_SHOOT, A_CSHOOT):
                continue
            si = seq_pick(m, (gait,))
            if si is not None and ("g", aid) not in done:
                done.add(("g", aid))
                add(si, 0, aid, -1, FEET_CROUCH if cr else FEET_STAND)
        for ci, cls in enumerate(self.prof["classes"]):
            if cls not in self.player_classes:
                continue
            for kind, pat, cr in ((0, "ref_aim_%s", False), (1, "ref_shoot_%s", False), (2, "crouch_aim_%s", True), (3, "crouch_shoot_%s", True)):
                si = seq_pick(m, (pat % cls,))
                if si is not None:
                    add(si, 1, kind, ci, FEET_CROUCH if cr else FEET_STAND)
        seen = set()
        for aid, names, feet in self.prof["deaths"]:
            for nm in names:
                si = seq_pick(m, (nm,))
                if si is None or (m.sequences[si]["seqgroup"], m.sequences[si]["animindex"]) in seen:
                    continue
                seen.add((m.sequences[si]["seqgroup"], m.sequences[si]["animindex"]))
                add(si, 2, aid, -1, feet)
        if not seqs:
            return
        mo_ = self.meshes[mesh]
        ref_world = m.pose(0, 0)
        v0, rad = len(sk.verts_i), 0.0
        for key in mo_.order:
            b, lp, ln = studioskel.local_vertex(m, (key[0], 0, key[1], key[2]), ref_world)   # bake(): model 0 of each bodypart
            sk.verts_i.append(b)
            sk.verts_f.extend(lp + ln)
            rad = max(rad, math.sqrt(sum(c * c for c in lp)))
        row = len(sk.models)
        sk.meshes.append((mesh, row, v0, len(mo_.order)))
        key0 = sk.add_texels(rows)
        nb = len(m.bones)
        lo, hi = [1e9] * 3, [-1e9] * 3
        stride = 2 * nb + len(slots)
        absk = set()
        for ints, _f in seqs:
            if ints[0] != 1:
                absk.update(range(ints[3], ints[3] + (ints[4] * (2 if ints[6] >= 0 else 1))))
        for k in sorted(absk):
            for b in range(nb):
                p = rows[k * stride + 2 * b + 1]
                for c in range(3):
                    lo[c] = min(lo[c], p[c])
                    hi[c] = max(hi[c], p[c])
        ctl0 = len(sk.ctl_i) // 4
        for s_, c in enumerate(slots):
            sk.ctl_i.extend([c["bone"], c["type"], c["index"], s_])
            sk.ctl_f.extend([c["start"], c["end"], 0.0, 0.0])
        att0 = len(sk.att_i) // 2
        for a in m.attachments:
            sk.att_i.extend([a["bone"], 0])
            sk.att_f.extend(list(studioskel.to_u(a["org"])) + [0.0])
        mask0 = len(sk.masks)
        sk.masks.extend(studioskel.bone_masks(m, slots, legs))
        seq0 = len(sk.pseq_i) // 8
        for ints, floats in seqs:
            sk.pseq_i.extend([row] + ints)
            sk.pseq_f.extend(floats)
        sk.prow_i.extend([pi, row, seq0, len(seqs), hand, mesh, att, 0])
        sk.models.append(dict(kind=1, owner=pi, nb=nb, nslots=len(slots), key0=key0, keys=nkey, ctl0=ctl0, nctl=len(slots), att0=att0,
                              natt=len(m.attachments), mask0=mask0, mesh0=len(sk.meshes) - 1, nmesh=1, anim0=seq0, nanim=len(seqs),
                              bounds=[(lo[c] + hi[c]) * 0.5 for c in range(3)] + [(hi[c] - lo[c]) * 0.5 + rad for c in range(3)]))
        log(f"player {self.players[pi]['name']} (skinned): {len(seqs)} sequences ({sum(1 for i, _f in seqs if i[0] == 0)} gait, "
            f"{sum(1 for i, _f in seqs if i[0] == 1)} upper, {sum(1 for i, _f in seqs if i[0] == 2)} deaths), {nkey} keys x {stride} texels, "
            f"{len(slots)} controller slots")

    # ---- monsters (GSX1 MON1) ----
    def monster(self, name, extra_acts, extra_names, body, pose_verts=None, tex_cap_key=None):
        """models/<name>.mdl → one kind-4 mesh per bodypart model, all with the same poses: the key frames of every
        kept sequence (MON_ACTS + the extras, ≤ MON_PER_ACT per activity, plus named ones), adaptive like VAN1 but on
        bone positions. Records: activity, weight, speed, key times, events (see the MON1 layout in the header)."""
        m = self.load_mdl(name + ".mdl")
        if (m is None or not m.sequences) and standin_of(name):
            st, sc, where = standin_of(name)
            if where == "runtime":
                log(f"monster {name}: not in the install, the runtime draws its stand-in {st}")
                return
            m = self.load_mdl(st + ".mdl")
            if m is not None:
                m.scale_uniform(sc)
                log(f"monster {name}: not in the install, stand-in {st}.mdl x {sc:.3f}")
        if m is None or not m.sequences:
            self.missing.append(f"models/{name}.mdl")
            return
        chosen, scripted, missing = monster_seq_plan(m, extra_acts, extra_names, self.prof.get("script_seqs", {}).get(name, ()))
        for nm in missing:
            self.missing.append(f"models/{name}.mdl sequence '{nm}'")
        if not chosen:
            # no sequence matched an activity / name (floater.mdl: one sequence "idle1", activity 0): the game then plays
            # sequence 0 (SetActivity falls back to it). A monster with 0 anims / 0 poses fails the loader's MON1 check.
            chosen.append(0)
            log(f"monster {name}: no activity matched, keeping sequence 0 '{m.sequences[0]['label']}'")
        # monster skins may be halved under the atlas budget (like v_ textures); studio props: the profile's prop_tex_max side
        tk = self.tex_keys(m, True, tex_cap_key) if tex_cap_key else self.tex_keys(m, True)
        # bodypart models: the default body's model of every bodypart always; the alternatives (heads, guns) while the
        # monster stays within MON_PART_VERTS source vertices (svencoop's HD grunt has 12 models / 9k vertices)
        dflt = [(bi, (body // max(1, bp["base"])) % max(1, len(bp["models"]))) for bi, bp in enumerate(m.bodyparts)
                if bp["models"]]
        nverts = lambda p: len(m.bodyparts[p[0]]["models"][p[1]]["verts"])       # noqa: E731
        parts, used = list(dflt), sum(nverts(p) for p in dflt)
        for bi, bp in enumerate(m.bodyparts):
            for mo in range(len(bp["models"])):
                budget = SKEL_PART_VERTS if self.skel else self.prof.get("mon_part_verts", MON_PART_VERTS)
                if (bi, mo) not in parts and (nverts((bi, mo)) == 0 or used + nverts((bi, mo)) <= budget):
                    parts.append((bi, mo))
                    used += nverts((bi, mo))
                elif (bi, mo) not in parts:
                    log(f"monster {name}: bodypart {bp['name']} model {mo} skipped (part vertex budget {budget})")
        parts.sort()
        if self.skel:
            return self.monster_skel(m, name, chosen, scripted, parts, tk, body)
        part_poses = {p: [] for p in parts}
        mi_ = len(self.monsters)
        anim0, labels = len(self.manims), []
        npose = 0
        kscale = 1.0
        budget_pv = pose_verts or self.prof.get("mon_pose_verts")
        loco = set(si for si in chosen if is_loco(m.sequences[si]))
        if budget_pv:
            # budget: baked vertices ≈ 1.6 × source vertices (uv seams); worst case every anim uses its key cap. The walk /
            # run cycles keep MON_KEYS_LOCO keys outside the scale: with 2 keys (first + last frame = the same pose of a
            # loop) a heavy Sven Co-op grunt slid along without moving its legs (maintainer 2026-11-04)
            cap = lambda si: MON_KEYS.get(m.sequences[si]["activity"], MON_KEYS_DEFAULT)       # noqa: E731
            fixed = sum(min(cap(si), loco_keys(m.sequences[si])) for si in loco)
            plan = sum(cap(si) for si in chosen if si not in loco)
            kscale = min(1.0, max(0.0, budget_pv / (1.6 * used) - fixed) / max(1.0, plan))
            if kscale < 1.0:
                log(f"monster {name}: key caps × {kscale:.2f} (pose vertex budget {budget_pv}, {len(loco)} walk/run cycles keep {MON_KEYS_LOCO} / {MON_KEYS_LOCO2})")
        for si in chosen:
            sq = m.sequences[si]
            n = max(1, sq["numframes"])
            fps = sq["fps"] if sq["fps"] > 0 else 30.0
            kmax = MON_KEYS_SCRIPT if si in scripted else MON_KEYS.get(sq["activity"], MON_KEYS_DEFAULT)
            if kscale < 1.0:
                kmax = min(kmax, loco_keys(sq)) if si in loco else max(2, int(kmax * kscale + 0.5))
            worlds = {f: m.pose(si, f) for f in range(n)}
            P = {f: [(w[3], w[7], w[11]) for w in worlds[f]] for f in range(n)}
            keys = pick_keys(P, n, kmax, MON_TOL)
            for f in keys:
                baked = m.bake_parts(si, f, world=worlds[f])
                for p in parts:
                    part_poses[p].append(collect(baked[p], tk))
            lin = math.sqrt(sum(c * c for c in sq["linear"]))
            ev0 = len(self.mevents)
            for fr, ev, opt in sq["events"]:
                self.mevents.append((fr, ev, opt.replace("\\", "/")))
            self.manims.append(dict(act=sq["activity"], weight=sq["actweight"], name=sq["label"], pose0=npose,
                                    keys=len(keys), key0=len(self.mkeys), ev0=ev0, nev=len(self.mevents) - ev0,
                                    loop=sq["flags"] & 1, frames=n, seq=si, fps=fps,
                                    seconds=(n - 1) / fps if n > 1 else 0.0,
                                    speed=lin * fps / (n - 1) if n > 1 else 0.0, dist=lin, linvec=tuple(sq["linear"])))
            self.mkeys.extend(f / fps for f in keys)
            npose += len(keys)
            labels.append(f"{sq['label']}:{sq['activity']}x{len(keys)}")
        if npose > 1024:
            raise SystemExit(f"monster {name}: {npose} poses > 1024 (lower MON_KEYS)")
        prow = []
        for bi, mo in parts:
            nv = len(m.bodyparts[bi]["models"][mo]["verts"])
            mesh = -1
            if part_poses[(bi, mo)] and part_poses[(bi, mo)][0][0]:
                mesh = self.add(make_mesh(4, mi_, part_poses[(bi, mo)],
                                          f"monster {name} part {bi}.{mo} '{m.bodyparts[bi]['models'][mo]['name']}'"))
            prow.append((bi, mo, mesh, m.bodyparts[bi]["base"], len(m.bodyparts[bi]["models"]), nv))
        if npose <= 0 or len(self.manims) == anim0:
            self.missing.append(f"models/{name}.mdl (no usable sequences)")     # the loader rejects a monster without anims / poses
            del self.manims[anim0:]
            return
        self.monsters.append(dict(name=name, anim0=anim0, nanim=len(self.manims) - anim0, parts=prow, body=body,
                                  skin=0, poses=npose))
        log(f"monster {name}: {len(chosen)} anims, {npose} poses, {len(prow)} part meshes "
            f"({', '.join(m.bodyparts[b]['name'] + '.' + str(o) for b, o, _m, _b, _n, _v in prow)}): {' '.join(labels)}")

    def monster_skel(self, m, name, chosen, scripted, parts, tk, body):
        """Skinned variant of monster() (GSX1 SKL1, tools/studioskel.py): one reference pose per bodypart mesh, the key frames as
        bone transforms in the bone texture. MON1 keeps its records (activity, weight, key times, events); a record's poses
        poseFirst.. are key rows of the bone texture, numblends-2 sequences add a second key set (blend 1, SKL1 anim row)."""
        sk = self.skp
        mi_ = len(self.monsters)
        anim0, labels = len(self.manims), []
        slots = studioskel.slots_of(m, log)
        rows, npose = [], 0
        lo, hi = [1e9] * 3, [-1e9] * 3
        anim_rows = []
        ref = None
        for si in chosen:
            sq = m.sequences[si]
            n = max(1, sq["numframes"])
            fps = sq["fps"] if sq["fps"] > 0 else 30.0
            kmax = studioskel.SKEL_KEYS_MAX // 2 if si in scripted else studioskel.SKEL_KEYS_MAX
            keys, sets = studioskel.sequence_keys(m, si, kmax)
            if ref is None:
                ref = sets[0][0]
            pose0 = npose
            for bl in sorted(sets):
                for vals, world in sets[bl]:
                    rows.extend(studioskel.key_rows(m, vals, world, slots))
                    for w in world:
                        p = studioskel.to_u((w[3], w[7], w[11]))
                        for c in range(3):
                            lo[c] = min(lo[c], p[c])
                            hi[c] = max(hi[c], p[c])
                    npose += 1
            b1 = pose0 + len(keys) if 1 in sets else -1
            anim_rows.append((b1, sq["blendtype"][0] if 1 in sets else 0,
                              sq["blendstart"][0] if 1 in sets else 0.0, sq["blendend"][0] if 1 in sets else 0.0))
            lin = math.sqrt(sum(c * c for c in sq["linear"]))
            ev0 = len(self.mevents)
            for fr, ev, opt in sq["events"]:
                self.mevents.append((fr, ev, opt.replace("\\", "/")))
            self.manims.append(dict(act=sq["activity"], weight=sq["actweight"], name=sq["label"], pose0=pose0,
                                    keys=len(keys), key0=len(self.mkeys), ev0=ev0, nev=len(self.mevents) - ev0,
                                    loop=sq["flags"] & 1, frames=n, seq=si, fps=fps,
                                    seconds=(n - 1) / fps if n > 1 else 0.0,
                                    speed=lin * fps / (n - 1) if n > 1 else 0.0, dist=lin, linvec=tuple(sq["linear"])))
            self.mkeys.extend(f / fps for f in keys)
            labels.append(f"{sq['label']}:{sq['activity']}x{len(keys)}{'+b' if 1 in sets else ''}")
        if npose <= 0 or len(self.manims) == anim0:
            self.missing.append(f"models/{name}.mdl (no usable sequences)")
            del self.manims[anim0:]
            return
        _ref_vals, ref_world = ref
        baked = m.bake_parts(chosen[0], 0, world=ref_world)
        prow, mesh0, rad = [], len(sk.meshes), 0.0
        for bi, mo in parts:
            nv = len(m.bodyparts[bi]["models"][mo]["verts"])
            mesh = -1
            col = collect(baked[(bi, mo)], tk)
            if col[0]:
                mesh = self.add(make_mesh(4, mi_, [col], f"monster {name} part {bi}.{mo} '{m.bodyparts[bi]['models'][mo]['name']}' (skinned)"))
                mo_ = self.meshes[mesh]
                v0 = len(sk.verts_i)
                for key in mo_.order:
                    b, lp, ln = studioskel.local_vertex(m, key, ref_world)
                    sk.verts_i.append(b)
                    sk.verts_f.extend(lp + ln)
                    rad = max(rad, math.sqrt(sum(c * c for c in lp)))
                sk.meshes.append((mesh, len(sk.models), v0, len(mo_.order)))
                self.skin_families(m, mesh, mo_, tk)
            prow.append((bi, mo, mesh, m.bodyparts[bi]["base"], len(m.bodyparts[bi]["models"]), nv))
        key0 = sk.add_texels(rows)
        ctl0 = len(sk.ctl_i) // 4
        for s_, c in enumerate(slots):
            sk.ctl_i.extend([c["bone"], c["type"], c["index"], s_])
            sk.ctl_f.extend([c["start"], c["end"], 0.0, 0.0])
        att0 = len(sk.att_i) // 2
        for a in m.attachments:
            sk.att_i.extend([a["bone"], 0])
            sk.att_f.extend(list(studioskel.to_u(a["org"])) + [0.0])
        mask0 = len(sk.masks)
        sk.masks.extend(studioskel.bone_masks(m, slots))
        for b1, bt, bs, be in anim_rows:
            sk.anim_i.extend([b1, bt, 0, 0])
            sk.anim_f.extend([bs, be, 0.0, 0.0])
        cen = [(lo[c] + hi[c]) * 0.5 for c in range(3)]
        ext = [(hi[c] - lo[c]) * 0.5 + rad for c in range(3)]
        sk.models.append(dict(kind=0, owner=mi_, nb=len(m.bones), nslots=len(slots), key0=key0, keys=npose, ctl0=ctl0, nctl=len(slots),
                              att0=att0, natt=len(m.attachments), mask0=mask0, mesh0=mesh0, nmesh=len(sk.meshes) - mesh0,
                              anim0=anim0, nanim=len(self.manims) - anim0, bounds=cen + ext))
        self.monsters.append(dict(name=name, anim0=anim0, nanim=len(self.manims) - anim0, parts=prow, body=body,
                                  skin=0, poses=npose))
        log(f"monster {name} (skinned): {len(chosen)} anims, {npose} keys x {2 * len(m.bones) + len(slots)} texels, "
            f"{len(slots)} controller slots, {len(m.attachments)} attachments, {len(prow)} part meshes "
            f"({', '.join(m.bodyparts[b]['name'] + '.' + str(o) for b, o, _m, _b, _n, _v in prow)}): {' '.join(labels)}")

    def skin_families(self, m, mesh, mo_, tkm):
        """Alternate skin families of a skinned part mesh: per family whose texture differs for any vertex, the texture key per vertex
        (written as an alternate UV set in SKL1; tex_keys registers every texture of the model, so they are in the atlas)."""
        if len(m.skins) < 2:
            return
        fam0 = m.skins[0]
        for f in range(1, len(m.skins)):
            fam = m.skins[f]
            remap = {}
            for col, ti in enumerate(fam0):
                if col < len(fam) and fam[col] != ti:
                    remap.setdefault(ti, fam[col])
            if not remap:
                continue
            keys, hit = [], False
            for key in mo_.order:
                ti = key[6]
                if ti in remap and remap[ti] in tkm:
                    keys.append(tkm[remap[ti]])
                    hit = True
                else:
                    keys.append(None)
            if hit:
                self.skel_skins.append((mesh, f, keys))

    # ---- gib models (GSX1 GIB1) ----
    def gib(self, name, texel_budget=None):
        """models/<name>.mdl → one kind-6 mesh per bodypart-0 model (the gib pieces; frame 0 of sequence 0, model space like the world
        meshes). Empty models (hgibs-style placeholder bodies) keep a row with mesh -1. Textures go through the shared atlas."""
        name = name.lower()
        if name.startswith("models/"):
            name = name[7:]
        if name.endswith(".mdl"):
            name = name[:-4]
        if any(g[0] == name for g in self.gibs):
            return 0
        m = self.load_mdl(name + ".mdl")
        if m is None or not m.sequences or not m.bodyparts or not m.bodyparts[0]["models"]:
            if standin_of(name):
                log(f"gib model {name}: not in the install, the runtime uses its stand-in {standin_of(name)[0]}")
            elif name not in GIB_OPTIONAL:
                self.missing.append(f"models/{name}.mdl")
            return 0
        if texel_budget is not None:
            new = sum((-(-t.width // 8) * 8 + 2 * GUTTER) * (-(-t.height // 8) * 8 + 2 * GUTTER) for t in m.textures
                      if hashlib.sha1(struct.pack("<ii", t.width, t.height) + t.rgba()).hexdigest() not in self.textures)
            if new > texel_budget:
                log(f"gib model {name}: {new} new texels over the extra-gib budget ({texel_budget} left), dropped")
                return 0
        else:
            new = 0
        tk = self.tex_keys(m, False)
        baked = m.bake_parts(0, 0)
        gi = len(self.gibs)
        pieces = []
        for mo in range(min(GIB_MAX_PIECES, len(m.bodyparts[0]["models"]))):
            col = collect(baked[(0, mo)], tk)
            nv = len(col[0])
            mesh = -1
            if nv >= 3 and col[2]:
                mesh = self.add(make_mesh(MESH_GIB, gi, [col], f"gib {name} piece {mo} '{m.bodyparts[0]['models'][mo]['name']}'"))
            pieces.append((mesh, nv))
        if not any(p[0] >= 0 for p in pieces):
            self.missing.append(f"models/{name}.mdl (no geometry)")
            return 0
        self.gibs.append((name, pieces))
        log(f"gib model {name}: {len(pieces)} pieces ({', '.join(str(p[1]) for p in pieces)} verts)")
        return new

    # ---- atlas ----
    def build_atlas(self):
        keys = sorted(self.textures, key=lambda k: self.textures[k][3])
        items = {k: list(self.textures[k][:2]) + [self.textures[k][2], self.textures[k][4]] for k in keys}

        def cell(w, h):
            return (-(-w // 8) * 8 + 2 * GUTTER, -(-h // 8) * 8 + 2 * GUTTER)

        while True:
            area = sum(cell(it[0], it[1])[0] * cell(it[0], it[1])[1] for it in items.values())
            placed = None
            if area <= self.max_atlas_px:
                for aw in (1024, 2048, 4096):
                    pos, used = shelf_pack([cell(items[k][0], items[k][1]) for k in keys], aw)
                    if pos is None:
                        continue
                    ah = -(-used // 8) * 8
                    if ah <= 4096 and aw * ah <= self.max_atlas_px:
                        if placed is None or max(aw, ah) * 1e9 + aw * ah < max(placed[0], placed[1]) * 1e9 + placed[0] * placed[1]:
                            placed = (aw, ah, pos)
            if placed:
                break
            # halve the largest view-only texture (P/W textures stay full size)
            cand = [k for k in keys if items[k][3] and min(items[k][0], items[k][1]) >= 32]
            if not cand:
                raise SystemExit("atlas budget too small even with all view textures halved")
            k = max(cand, key=lambda k: items[k][0] * items[k][1])
            w, h, rgba, v = items[k]
            items[k] = [w // 2, h // 2, half_rgba(rgba, w, h), v]
            log(f"atlas budget: halved {self.textures[k][3]} → {w // 2}x{h // 2}")
        aw, ah, pos = placed
        img = bytearray(aw * ah * 4)
        rects = {}
        for k, (x, y) in zip(keys, pos):
            w, h, rgba, _v = items[k]
            cw, ch = cell(w, h)
            rects[k] = (x + GUTTER, y + GUTTER, w, h)
            rows = [rgba[r * w * 4:(r + 1) * w * 4] for r in range(h)]
            right = cw - GUTTER - w
            for r in range(ch):
                src = rows[min(h - 1, max(0, r - GUTTER))]
                line = src[:4] * GUTTER + src + src[-4:] * right
                d = ((y + r) * aw + x) * 4
                img[d:d + len(line)] = line
        levels = [bytes(img)]
        cw, chh, cur = aw, ah, bytes(img)
        for _ in range(1, MIPS):
            cur, cw, chh = half_rgba(cur, cw, chh), cw // 2, chh // 2
            levels.append(cur)
        log(f"atlas {aw}x{ah} ({len(keys)} unique textures, {100.0 * sum(cell(i[0], i[1])[0] * cell(i[0], i[1])[1] for i in items.values()) / (aw * ah):.0f}% used), {MIPS} mips")
        return aw, ah, b"".join(levels), rects

    # ---- write ----
    def write(self, out):
        aw, ah, atlas, rects = self.build_atlas()
        names = bytearray()

        def name(s):
            off = len(names)
            names.extend(s.encode("ascii", "replace"))
            return off, len(s)

        wint, wflt = array.array("i"), array.array("f")
        for w in self.weapons:
            no, nl = name(w["name"])
            wint.extend([w["id"], w["meshP"], w["meshW"], w["meshV"]] + w["sounds"] +
                        [w["ev0"], w["nev"], no, nl, w["flags"], w["animClass"], 0, 0, 0, 0])
            wflt.extend(list(w["muzzleP"]) + list(w["muzzleV"]) + [w["reload_s"], w["draw_s"]])
        mtab, idx, uv, pos, nrm = array.array("i"), array.array("i"), array.array("f"), array.array("f"), array.array("f")
        for m in self.meshes:
            v = len(m.texkeys)
            mtab.extend([v, len(m.idx), len(m.poses), len(uv) // 2, len(idx), len(pos) // 3, m.kind, m.wid])
            idx.extend(m.idx)
            for tk, (s, t) in zip(m.texkeys, m.st):
                if tk is None:
                    uv.extend([0.0, 0.0]); continue
                x, y, w_, h_ = rects[tk]
                uv.extend([(x + s * w_) / aw, (y + t * h_) / ah])
            for p, n in m.poses:
                pos.extend(p); nrm.extend(n)
        stab, samples = array.array("i"), array.array("f")
        for nm, rate, data in self.sounds:
            no, nl = name(nm)
            stab.extend([rate, len(data), len(samples), 1, no, nl])
            samples.extend(data)
        etab = array.array("i")
        for e in self.events:
            etab.extend(e)
        ptab = array.array("i")
        for pl in self.players:
            no, nl = name(pl["name"])
            ptab.extend([pl["team"], pl["mesh"], pl["anim0"], pl["nanim"], no, nl, pl["hand0"], 0])
        chunks = []
        if self.sprites:
            ch, (saw, sah, scell, sn) = spr.spr1_chunk(self.sprites, name, MIPS, half_rgba)
            chunks.append(ch)
            log(f"sprites: {len(self.sprites)} ({sn} frames) in a {saw}x{sah}x{MIPS} atlas, {scell} px cells, "
                f"{len(ch) / 1e6:.2f} MB")
        if self.vanims:
            # VAN1: int32 count, count × (weapon id, anim 0 reload / 1 draw / 2 attack / 3, 4 attack variants / 5 last-round shot, mesh, keys, timeFirst),
            # then float32 key times (s from the anim start; key k = pose k of the mesh)
            tab, times = array.array("i"), array.array("f")
            for wid, anim, mi, keys, ts in self.vanims:
                tab.extend([wid, anim, mi, len(keys), len(times)])
                times.extend(ts)
            payload = struct.pack("<i", len(self.vanims)) + tab.tobytes() + times.tobytes()
            chunks.append(b"VAN1" + struct.pack("<i", len(payload)) + payload)
            log(f"view anims: {len(self.vanims)} (reload / draw / attack key frames, {len(times)} keys)")
        if self.vguns:
            # VGUN: int32 count, per entry int32 mesh, poses, weapon id, float32 grip xyz, poses × (pos xyz, quat xyzw, 0)
            body = bytearray(struct.pack("<i", len(self.vguns)))
            for mi, wid, grip, frames in self.vguns:
                body += struct.pack("<3i3f", mi, len(frames), wid, *grip)
                for gp, gq in frames:                  # (not "pos": that is the vertex array of write())
                    body += struct.pack("<8f", *gp, *gq, 0.0)
            chunks.append(b"VGUN" + struct.pack("<i", len(body)) + bytes(body))
            # VARM: int32 count, per entry int32 mesh, verts, then verts bytes (1 = arm / hand vertex) padded to 4
            body = bytearray(struct.pack("<i", len(self.varms)))
            for mi, flags in self.varms:
                body += struct.pack("<2i", mi, len(flags)) + bytes(flags) + b"\0" * (-len(flags) % 4)
            chunks.append(b"VARM" + struct.pack("<i", len(body)) + bytes(body))
            if self.vsils:
                # VSIL: int32 count, per entry int32 weapon id, int32 silencer mesh (kind 5, one pose, vertices in the frame of
                # the weapon's VGUN gun bone, so the loader places it with the rig pos / rot of the pose on screen), float32
                # xyz = the muzzle of the silenced gun (v_ mesh frame, like the weapon record's muzzleV). Old loaders skip it.
                body = struct.pack("<i", len(self.vsils)) + b"".join(struct.pack("<2i3f", wid, mi, *mz) for wid, mi, mz in self.vsils)
                chunks.append(b"VSIL" + struct.pack("<i", len(body)) + body)
                log(f"VR silencers: {len(self.vsils)} (bone-frame meshes)")
            if self.vsky:
                # VSKY: int32 count, per entry int32 v_ mesh, int32 poses, poses × (pos xyz, quat xyzw) float32 = the pose of the weapon's
                # silencer bone (view mesh frame, like VGUN) at each pose / anim key of that mesh. The VSIL mesh is in the frame of that bone.
                body = bytearray(struct.pack("<i", len(self.vsky)))
                for mi, frames in self.vsky:
                    body += struct.pack("<2i", mi, len(frames))
                    for gp, gq in frames:
                        body += struct.pack("<7f", *gp, *gq)
                chunks.append(b"VSKY" + struct.pack("<i", len(body)) + bytes(body))
                log(f"VR silencer bone poses: {len(self.vsky)} v_ meshes")
            log(f"VR view rigs: {len(self.vguns)} v_ meshes (gun bone per pose + grip point + arm vertices)")
        if self.mod != "cstrike" and any("pgrip" in g for g in self.grips.values()):
            # PGRP: int32 count, per entry int32 weapon id + float32 xyz = where the hand holds the P mesh (mesh frame, m;
            # (0,0,0) = the mesh origin already is the wrist, hand-bone frame) + int32 P source code (PSRC_NAMES) + int32
            # v_ grip source code (VSRC_NAMES, +16 = replaced because the hand was far from the gun). Older loaders skip it.
            rows = [(wid, g["pgrip"], src_code(PSRC_NAMES, g.get("psrc", "")), src_code(VSRC_NAMES, g.get("vsrc", "")))
                    for wid, g in sorted(self.grips.items()) if "pgrip" in g]
            body = struct.pack("<i", len(rows)) + b"".join(struct.pack("<i3f2i", wid, *pg, pc, vc) for wid, pg, pc, vc in rows)
            chunks.append(b"PGRP" + struct.pack("<i", len(body)) + body)
            log(f"P grips: {len(rows)} held models ({sum(1 for _w, pg, _p, _v in rows if pg != (0.0, 0.0, 0.0))} off the wrist)")
        if self.monsters:
            # MON1 (layout in the file header): counts, monster / part / anim / animF / key / event tables
            mt, pt, at, af, kt, et = (array.array("i"), array.array("i"), array.array("i"), array.array("f"),
                                      array.array("f", self.mkeys), array.array("i"))
            for mo in self.monsters:
                no, nl = name(mo["name"])
                mt.extend([no, nl, mo["anim0"], mo["nanim"], len(pt) // 6, len(mo["parts"]), mo["body"], mo["skin"],
                           mo["poses"], 0, 0, 0])
                for row in mo["parts"]:
                    pt.extend(row)
            for a in self.manims:
                no, nl = name(a["name"])
                at.extend([a["act"], a["weight"], no, nl, a["pose0"], a["keys"], a["key0"], a["ev0"], a["nev"],
                           a["loop"], a["frames"], a["seq"]])
                af.extend([a["fps"], a["seconds"], a["speed"], a["dist"]])
            for fr, ev, opt in self.mevents:
                no, nl = name(opt) if opt else (0, 0)
                et.extend([fr, ev, no, nl])
            tabs = [mt, pt, at, af, kt, et]
            if sys.byteorder != "little":
                for t in tabs:
                    t.byteswap()
            body = struct.pack("<8i", len(self.monsters), len(self.manims), len(self.mevents), len(pt) // 6,
                               len(self.mkeys), 0, 0, 0) + b"".join(t.tobytes() for t in tabs)
            chunks.append(b"MON1" + struct.pack("<i", len(body)) + body)
            # MLM1 (after MON1): per MON1 anim record the sequence's linearmovement vector (x, y, z in GoldSrc units per cycle, model frame):
            # the post-script root move (CineCleanup: bone 0 at the end of the play). int32 MA, float32 [MA*3]. Older loaders skip the chunk.
            lm = array.array("f")
            for a in self.manims:
                lm.extend(a["linvec"])
            if sys.byteorder != "little":
                lm.byteswap()
            lbody = struct.pack("<i", len(self.manims)) + lm.tobytes()
            chunks.append(b"MLM1" + struct.pack("<i", len(lbody)) + lbody)
            mverts = sum(len(self.meshes[r[2]].texkeys) * len(self.meshes[r[2]].poses)
                         for mo in self.monsters for r in mo["parts"] if r[2] >= 0)
            log(f"monsters: {len(self.monsters)} ({len(self.manims)} anims, {len(self.mkeys)} keys, "
                f"{len(self.mevents)} events, {mverts} pose verts = {mverts * 24 / 1e6:.2f} MB), MON1 {len(body)} bytes")
        if self.skp.models:
            # SKL1 (layout in tools/studioskel.py + the file header): skinned monster models, bone texture, alternate skin UVs
            for mesh, fam, keys in self.skel_skins:
                mo_ = self.meshes[mesh]
                uvs = []
                for tk, k2, (s_, t_) in zip(mo_.texkeys, keys, mo_.st):
                    k = k2 if k2 is not None else tk
                    if k is None:
                        uvs.extend([0.0, 0.0])
                        continue
                    x, y, w_, h_ = rects[k]
                    uvs.extend([(x + s_ * w_) / aw, (y + t_ * h_) / ah])
                self.skp.skins.append((mesh, fam, uvs))
            ch = self.skp.chunk()
            chunks.append(ch)
            tw, th = self.skp.tex_size()
            log(f"skinned models: {len(self.skp.models)} ({sum(mo['keys'] for mo in self.skp.models)} keys, {len(self.skp.texels)} bone texels in a "
                f"{tw}x{th} float texture, {len(self.skp.verts_i)} vertices, {len(self.skp.skins)} alternate skins), SKL1 {len(ch) / 1e6:.2f} MB")
        if self.gibs:
            # GIB1: int32 count N, int32 total pieces P, N x (nameOff, nameLen, pieceFirst, pieceCount), P x (mesh index or -1, vertex count).
            # Meshes are kind 6 (one pose each, model space). Old loaders skip the chunk and keep the cube gibs.
            gt, gp = array.array("i"), array.array("i")
            for gname, pieces in self.gibs:
                no, nl = name(gname)
                gt.extend([no, nl, len(gp) // 2, len(pieces)])
                for mesh, nv in pieces:
                    gp.extend([mesh, nv])
            gbody = struct.pack("<2i", len(self.gibs), len(gp) // 2) + (gt.tobytes() + gp.tobytes() if sys.byteorder == "little"
                                                                       else b"".join(struct.pack("<i", v) for v in list(gt) + list(gp)))
            chunks.append(b"GIB1" + struct.pack("<i", len(gbody)) + gbody)
            log(f"gib models: {len(self.gibs)} ({len(gp) // 2} pieces, {sum(len(self.meshes[p[0]].texkeys) for _n, ps in self.gibs for p in ps if p[0] >= 0)} verts)")
        if self.hud is not None:
            ch, (haw, hah, hn) = hudpack.hud1_chunk(self.hud, name)
            chunks.append(ch)
            log(f"HUD sprites: {hn} rects from {len(self.hud.sheets)} sheets in a {haw}x{hah} 8-bit atlas, {len(ch) / 1e6:.2f} MB")
        atab, aflt, hflt, gflt = array.array("i"), array.array("f"), array.array("f"), array.array("f")
        for aid, cls, first, nf, dist, sec, loop in self.panims:
            atab.extend([aid, cls, first, nf])
            aflt.extend([dist, sec, float(loop), 0.0])
        for h in self.hands:
            hflt.extend(list(h) + [0.0])
        for w in self.weapons:
            g = w["grip"]
            gt = [g[0], g[4], g[8], 0, g[1], g[5], g[9], 0, g[2], g[6], g[10], 0]      # Lᵀ: mesh frame → hand bone
            gflt.extend(quat_from_rows(unity_rotation(gt)))
        secs = [wint, wflt, mtab, stab, etab, idx, uv, pos, nrm, samples]
        ext = [ptab, atab, aflt, hflt, gflt]
        names.extend(b"\0" * (-len(names) % 4))
        # SPR2 effect sprites go in LAST: they take what is left under --max-bytes (priority order: FX_SPRITES core first, then the
        # map-named extras; the tail is dropped first and logged)
        if self.fx_sprites:
            fixed = 80 + sum(len(a) * a.itemsize for a in secs + ext) + len(atlas) + len(names) + sum(len(c) for c in chunks) + 16
            room = self.max_bytes - fixed - 8192 - 40 * len(self.fx_sprites)
            budget = min(self.fx_texels, int(max(0, room) / 5.25))      # RGBA32 + 3 mips = 5.25 bytes per texel
            if budget < self.fx_texels:
                log(f"pack-size guard: effect sprite budget cut from {self.fx_texels} to {budget} texels (--max-bytes {self.max_bytes})")
            kept, dropped = spr.spr2_select(self.fx_sprites, budget)
            if dropped:
                log(f"effect sprites over the {budget} texel budget, dropped (lowest priority first): {', '.join(dropped)}")
            if kept:
                ch, (faw, fah, fn) = spr.spr2_chunk(kept, name, spr.SPR2_MIPS, half_rgba)
                chunks.append(ch)
                log(f"effect sprites: {len(kept)} ({fn} frames) in a {faw}x{fah}x{spr.SPR2_MIPS} atlas, {len(ch) / 1e6:.2f} MB")
        names.extend(b"\0" * (-len(names) % 4))
        names.extend(spr.gsx1_block(chunks))        # GSX1 extension block: last part of the names region
        if sys.byteorder != "little":
            for a in secs + ext:
                a.byteswap()
        hdr = b"GSM1" + struct.pack("<19i", HEADER, VERSION_NUM, len(self.weapons), len(self.meshes), len(self.sounds),
                                    len(self.events), len(uv) // 2, len(idx), len(pos) // 3, len(samples), aw, ah,
                                    MIPS, len(names), 1 if self.with_view else 0,
                                    len(self.players), len(self.panims), len(self.hands), 0)
        with open(out, "wb") as fh:
            fh.write(hdr)
            for a in secs:
                fh.write(a.tobytes())
            fh.write(atlas)
            fh.write(bytes(names))
            for a in ext:
                fh.write(a.tobytes())
        size = os.path.getsize(out)
        if size > 95_000_000:
            print(f"WARNING: pack is {size / 1e6:.1f} MB, close to the 100 MB cap of the companion / VRChat")
        print(f"{out}: {len(self.weapons)} weapons, {len(self.meshes)} meshes ({len(uv) // 2} verts, {len(idx) // 3} tris, "
              f"{len(pos) // 3} pose verts), {len(self.sounds)} sounds ({len(samples)} samples, "
              f"{sum(len(d) / r for _n, r, d in self.sounds):.1f} s), {len(self.events)} events, atlas {aw}x{ah}x{MIPS}, "
              f"{size} bytes ({size / 1e6:.2f} MB)")
        if self.players:
            pb = sum(len(self.meshes[pl["mesh"]].poses) * len(self.meshes[pl["mesh"]].texkeys) * 24 +
                     len(self.meshes[pl["mesh"]].texkeys) * 8 + len(self.meshes[pl["mesh"]].idx) * 4
                     for pl in self.players) + 4 * (len(ptab) + len(atab) + len(aflt) + len(hflt))
            print(f"players: {', '.join(pl['name'] for pl in self.players)} — {len(self.panims)} anims, "
                  f"{len(self.hands)} poses, {pb / 1e6:.2f} MB geometry (+ their textures in the atlas)")
        if self.missing:
            print(f"missing (skipped): {', '.join(sorted(set(self.missing)))}")
        return size


def shelf_pack(sizes, width):
    order = sorted(range(len(sizes)), key=lambda i: (-sizes[i][1], -sizes[i][0]))
    pos = [None] * len(sizes)
    x = y = shelf = 0
    for i in order:
        w, h = sizes[i]
        if w > width:
            return None, 1 << 30
        if x + w > width:
            y += shelf; x = 0; shelf = 0
        pos[i] = (x, y)
        x += w
        shelf = max(shelf, h)
    return pos, y + shelf


half_rgba = spr.half_rgba      # 2x2 box filter (defined in spr.py with half_sprite)


def read_wav(path, max_s):
    """PCM WAV (8-bit unsigned / 16-bit signed, mono or stereo) → (rate, array('f') mono in [-1, 1])."""
    with wave.open(path, "rb") as wf:
        ch, sw, rate, n = wf.getnchannels(), wf.getsampwidth(), wf.getframerate(), wf.getnframes()
        raw = wf.readframes(n)
    if sw == 1:
        vals = [(b - 128) / 128.0 for b in raw]
    elif sw == 2:
        a = array.array("h")
        a.frombytes(raw[:len(raw) // 2 * 2])
        if sys.byteorder != "little":
            a.byteswap()
        vals = [v / 32768.0 for v in a]
    else:
        raise ValueError(f"{sw * 8}-bit PCM not supported")
    if ch > 1:
        vals = [sum(vals[i:i + ch]) / ch for i in range(0, len(vals) - ch + 1, ch)]
    lim = int(max_s * rate)
    if len(vals) > lim:
        vals = vals[:lim]
        fade = max(1, int(0.05 * rate))
        for i in range(fade):
            vals[lim - 1 - i] *= i / fade
    return rate, array.array("f", vals)


def main(argv=None):
    for _stream in (sys.stdout, sys.stderr):     # never crash on a console code page (Windows cp1252)
        try:
            _stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="GSM1 weapon model + sound pack builder")
    ap.add_argument("--version", action="store_true", help="print the converter version and exit (with --mod: that mod's pack version, else the global source-hash version)")
    ap.add_argument("--game-root", help="GoldSrc game dir containing valve/, cstrike/ …")
    ap.add_argument("--mod", default=None, help="default cstrike")
    ap.add_argument("--no-view", action="store_true", help="skip v_ (first-person) models")
    ap.add_argument("--max-atlas-px", type=int, default=None,
                    help="atlas texel budget (default 4096*2048, or the profile's atlas_px)")
    ap.add_argument("--max-clip-s", type=float, default=6.0)
    ap.add_argument("--player-classes", default="all",
                    help="upper-body classes to bake (comma list of the mod's classes, e.g. cstrike "
                         + ",".join(PLAYER_CLASSES) + ", or 'all'); missing ones fall back to class 0 / any at runtime")
    ap.add_argument("--players", default="default",
                    help="player models to bake: comma list (cstrike " + ",".join(n for n, _t in PLAYER_MODELS) +
                         "; valve " + ",".join(n for n, _t in VALVE_PLAYER_MODELS) + "), 'all', 'none' or 'default' "
                         "(cstrike " + ",".join(DEFAULT_PLAYERS) + ", valve " + ",".join(VALVE_DEFAULT_PLAYERS) + ")")
    ap.add_argument("--monsters", default="all",
                    help="valve/svencoop: monster models for the GSX1 MON1 chunk (comma list of "
                         + ",".join(r[0] for r in MONSTERS) + ", 'all' or 'none')")
    ap.add_argument("--no-sprites", action="store_true", help="leave out the GSX1 sprite chunks (SPR1 smoke cloud sprites, SPR2 effect sprites)")
    ap.add_argument("--fx-texels", type=int, default=None,
                    help="SPR2 effect sprite atlas budget in texels (default %d, cstrike %d); sprites over it are dropped and logged"
                         % (FX_TEXELS, FX_TEXELS_MOD["cstrike"]))
    ap.add_argument("--max-bytes", type=int, default=99_000_000,
                    help="pack size guard (the companion refuses > 100 MB): the SPR2 effect sprites shrink to what is left under it")
    ap.add_argument("--no-gibs", action="store_true", help="leave out the GSX1 GIB1 gib models (hgibs / agibs / material gibs)")
    ap.add_argument("--no-view-anims", action="store_true", help="leave out the VAN1 smooth v_ anims (reload / draw / attack)")
    ap.add_argument("--no-skel", action="store_true", help="monsters as baked pose meshes (MON1 only, the pre-SKL1 format) instead of skinned")
    ap.add_argument("out", nargs="?")
    a = ap.parse_args(argv)
    mod_given = a.mod is not None
    a.mod = a.mod or "cstrike"
    if a.version:
        print(version_for_mod(a.mod) if mod_given else source_version())   # --mod: per-mod version; plain: old global one
        return 0
    if not a.game_root or not a.out:
        ap.error("--game-root and out are required")
    if a.mod not in PROFILES:
        raise SystemExit(f"no weapon table for mod '{a.mod}' (only {', '.join(sorted(PROFILES))})")
    prof = PROFILES[a.mod]
    t0 = time.time()
    b = Builder(a.game_root, a.mod, a.max_atlas_px or prof.get("atlas_px", 4096 * 2048), a.max_clip_s, not a.no_view)
    b.view_anims = not a.no_view_anims and prof.get("view_anims", True)
    b.skel = not a.no_skel
    if a.player_classes != "all":
        pc = {c.strip().lower() for c in a.player_classes.split(",") if c.strip()}
        bad = pc - set(prof["classes"])
        if bad:
            ap.error(f"unknown player classes: {', '.join(sorted(bad))}")
        b.player_classes = pc
    import bprog
    bprog.plan(weapons=35, players=25, monsters=25, extras=5, write=10)
    nweap = len(prof["weapons"])
    for wi, (wid, stem, cls, snd, files) in enumerate(prof["weapons"]):
        b.weapon(wid, stem, cls, snd, files)
        bprog.progress("weapons", wi + 1, nweap)
    want = [n.strip().lower() for n in a.players.split(",") if n.strip()]
    if want == ["all"]:
        want = [n for n, _t in prof["player_models"]]
    elif want == ["none"]:
        want = []
    elif want == ["default"]:
        want = list(prof["default_players"])
    teams = dict(prof["player_models"])
    for n in want:
        if n not in teams:
            ap.error(f"unknown player model '{n}'")
    pdone = 0
    for n, t in prof["player_models"]:   # table order (cstrike: T models first, then CT)
        if n in want:
            b.player(n, t)
            pdone += 1
            bprog.progress("players", pdone, len(want))
    known = [r[0] for r in prof["monsters"]]
    mons = [n.strip().lower() for n in a.monsters.split(",") if n.strip()]
    if mons == ["all"]:
        mons = known
    elif mons == ["none"]:
        mons = []
    for n in mons:
        if n not in known:
            ap.error(f"unknown monster '{n}' for mod {a.mod}")
    mdone = 0
    for row in prof["monsters"]:         # MONSTERS order = MON1 monster index
        if row[0] in mons:
            b.monster(*row)
            mdone += 1
            bprog.progress("monsters", mdone, len(mons))
    if prof.get("props") and mons:
        # studio props after the monster table (their MON1 index is looked up by name, never by position)
        props = [p for p in map_prop_models(a.game_root, a.mod) if p not in known][:PROP_MAX_MODELS]
        for p in props:
            b.monster(p, (), ("*",), 0, PROP_POSE_VERTS, "prop_tex_max")
        log(f"studio props: {len(props)} map-named models ({', '.join(props)})")
    bprog.progress("extras", 0, 1)
    if not a.no_gibs:
        for g in prof.get("gib_models", GIB_MODELS):
            b.gib(g)
        extras = [g for g in map_gib_models(a.game_root, a.mod) if g[7:-4] not in prof.get("gib_models", GIB_MODELS)]
        left = GIB_EXTRA_TEXELS
        for g in extras:
            if len(b.gibs) >= GIB_MAX_MODELS:
                log(f"gib models over the {GIB_MAX_MODELS} model limit, dropped: {g}")
                continue
            left -= b.gib(g, left)
    if not any(w["meshP"] >= 0 or w["meshW"] >= 0 for w in b.weapons):
        raise SystemExit("no weapon models found under " + os.path.join(a.game_root, a.mod, "models"))
    for rel in prof["player_sounds"]:    # player sounds (by-name lookup), after all weapon sounds
        b.sound(rel)
    if not a.no_sprites:
        for rel in prof["sprites"]:
            b.sprite(rel)
        b.fx_texels = a.fx_texels or FX_TEXELS_MOD.get(a.mod, FX_TEXELS)
        b.max_bytes = a.max_bytes
        for rel in fx_sprite_order(a.mod):
            b.fx_sprite(rel)
        extra = map_sprites(a.game_root, a.mod)
        for rel in extra:
            b.fx_sprite(rel)
        log(f"effect sprite candidates: {len(b.fx_sprites)} ({len(extra)} referenced by the {a.mod} maps)")
        b.hud = hudpack.load(a.game_root, a.mod)
        if b.hud is not None and b.hud.missing:
            log("HUD sprites missing / out of range: " + ", ".join(b.hud.missing[:8]))
    bprog.progress("extras", 1, 1)
    bprog.progress("write", 0, 1)
    b.write(a.out)
    bprog.progress("write", 1, 1)
    print(f"built in {time.time() - t0:.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
