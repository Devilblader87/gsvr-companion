"""Companion build-manager contract of the Ragnarok converter (rosebuild.py flavor RO; the stub companion/ro_conv_stub.py documents the same API).

  VERSION, PACKS, check_install(folder) -> (ok, reason), install_files(folder) -> [relative paths], pack_version(name),
  build(name, folder, out_path, progress=cb(stage, 0..1), cancel=callable) -> {"warnings": [...], ...}, Cancelled, BuildError.

One pack per map (ro__<map>.pack in the companion's ro dir, served as GET /pack/ro.pack): the map sections of mappack.build (AREA CELL HGHT
TERR TATL TTIL OTEX OMAT OMSH SPWN SPWE [WATR], read by GsvrRpg1Loader). The start map's pack (SPRITE_PACK = prontera) also carries the
player sprite set of sprite.py (RSAT RSAN, read by GsvrRoSpriteSet) in the SAME RPG1 container; the other maps of PACKS (MAP_ORDER) are
map only (docs/design/ro_multimap.md: the world loads every map of PACKS at once and switches between them at the rAthena warps). A client without the Novice sprites still gets the map (warning
"sprites: ...", the world then draws placeholders). Reason strings never carry a folder path (status.json is public).
"""
import os
import struct

from . import equip, items, jobs, mappack, sprite, towns, culvert, dungeons, jobmaps, maps2, maps3, ui, roefx, npcs
from .grf import Vfs, GrfError, MAGICS

VERSION = 'ro-%d.%d' % (mappack.TOOL_VERSION, sprite.RSAN_VERSION)
# RO prototype "all maps loaded" (docs/design/ro_multimap.md): the world asks for these packs one after the other (one VRChat string load
# each, ~5 s apart) and keeps every one of them built at once; the companion builds them in THIS order (prontera = the start map first).
PACKS = {
    # first zone = the start map of the world (+ the player sprite set). start = the GAT cell a player arrives on: rAthena's "@go prontera" point
    # (atcommand.cpp, map 0 = prontera 156,191): the plaza just SOUTH of the fountain, the castle straight ahead to the north. Without a start the
    # converter takes the walkable cell nearest the map centre (Prontera: 156,196 = inside the fountain basin, the statue in the face).
    'prontera': {'kind': 'zone', 'label': 'Prontera', 'start': (156, 191)},
    'prt_in': {'kind': 'zone', 'label': 'Prontera interiors'},
    'prt_church': {'kind': 'zone', 'label': 'Prontera church'},
    'prt_fild08': {'kind': 'zone', 'label': 'Prontera field 08'},
    'izlude': {'kind': 'zone', 'label': 'Izlude'},
}
# towns lane (docs/design/ro_towns.md): Geffen, Payon + Archer Village, Alberta, Morroc and the fields that link them (ids 5.. after the prototype maps)
for _m in towns.NAMES:
    PACKS.setdefault(_m, {'kind': 'zone', 'label': towns.LABEL[_m]})
# culvert lane (docs/design/ro_culvert.md, w35, rebased w39): the four Prontera Culvert floors = ids 27..30, right after the towns
for _m in culvert.NAMES:
    PACKS.setdefault(_m, {'kind': 'zone', 'label': culvert.LABEL[_m]})
# dungeons lane (docs/design/ro_dungeons.md, w37): Payon Cave, Ant Hell, Orc Dungeon + their entry maps = ids 31..42, appended after every earlier lane's maps
for _m in dungeons.NAMES:
    PACKS.setdefault(_m, {'kind': 'zone', 'label': dungeons.LABEL[_m]})
# first jobs lane (docs/design/ro_jobs1.md, w41): the guild / test maps of the six first-job tests = ids 43..49, on demand like the dungeons
for _m in jobmaps.NAMES:
    PACKS.setdefault(_m, {'kind': 'zone', 'label': jobmaps.LABEL[_m]})
# maps wave 2 (docs/design/ro_maps2.md, w46): Glast Heim, Comodo, Lutie + Toy Factory = ids 51..62, on demand like the dungeons
for _m in maps2.NAMES:
    PACKS.setdefault(_m, {'kind': 'zone', 'label': maps2.LABEL[_m]})
# maps wave 3 part 1 (docs/design/ro_maps3.md, w53): Byalan + Undersea Tunnel, Sunken Ship, Geffen Dungeon = ids 62..74, on demand like the dungeons
for _m in maps3.NAMES:
    PACKS.setdefault(_m, {'kind': 'zone', 'label': maps3.LABEL[_m]})
MAP_ORDER = tuple(PACKS)                                       # == GsvrRoMaps.mapNames in the scene (one index per map on both sides)
PREBUILD_ORDER = tuple(m for m in MAP_ORDER if m not in dungeons.NAMES and m not in jobmaps.NAMES and m not in maps2.NAMES and m not in maps3.NAMES)
# w42 ui-a (docs/design/ro_ui.md): the original interface atlas = its own small pack 'roui' (NOT in PACKS: the world loads every PACKS entry as a map). The build manager
# serves it like the audio packs (rosebuild merges UI_PACKS); the world asks for it once, right after the start map (GsvrRoFetch ui). Prebuilt right after the start map.
UI_PACKS = {ui.UI_PACK: {'kind': 'ui', 'label': 'Interface'}}
# w43 fx (docs/design/ro_fx.md): the world's skill / level up / warp portal effects = their own pack 'roefx' (ro_conv/roefx.py), served by name like 'roui' (rosebuild merges
# UI_PACKS), asked for once after snd_common; prebuilt right after the interface.
UI_PACKS[roefx.FX_PACK] = {'kind': 'ui', 'label': 'Effects'}
PREBUILD_ORDER = PREBUILD_ORDER[:1] + (ui.UI_PACK, roefx.FX_PACK) + PREBUILD_ORDER[1:]                                     # rosebuild: background prebuild order (instead of the ROSE lobby / avatar rule)
# w43 npc lane (docs/design/ro_npc_world.md): the classic NPCs' sprite packs, one per town group ('npc_<town>', RSAT + RSAN only, ro_conv/npcs.py). NOT in PACKS (not maps);
# served by name like the ui / audio packs (rosebuild merges NPC_PACKS); prebuilt: the start town's right after the interface + effects packs (w43 integrate), the others after every map.
NPC_PACKS = dict(npcs.NPC_PACKS)
_NPC_FIRST = tuple(g for g in npcs.NPC_ORDER if npcs.group_of(MAP_ORDER[0]) == g)
PREBUILD_ORDER = PREBUILD_ORDER[:3] + _NPC_FIRST + PREBUILD_ORDER[3:] + tuple(g for g in npcs.NPC_ORDER if g not in _NPC_FIRST)
NO_PREBUILD = frozenset(dungeons.NAMES) | frozenset(jobmaps.NAMES) | frozenset(maps2.NAMES) | frozenset(maps3.NAMES)                                    # never built in the background: only when the world asks (docs/design/ro_dungeons.md)
SPRITE_PACK = 'prontera'                                       # the ONLY pack with RSAT / RSAN: the world builds the sprite set once

# w36 sound lane (docs/design/ro_sound.md): audio / effect packs next to the map packs. NOT in PACKS (the world loads every PACKS entry as a map); the lead merges them
# into the build manager's table (PACKS.update(AUDIO_PACKS) in rosebuild) and the world asks for them by name: snd_common (sound effects + skill effects), snd_<map> (that map's BGM).
AUDIO_PACKS = dict([('snd_common', {'kind': 'audio', 'label': 'Sounds and effects'})] + [('snd_' + _m, {'kind': 'audio', 'label': 'Music ' + _m}) for _m in PACKS])
AUDIO_ORDER = tuple(AUDIO_PACKS)
PACK_REV = {'prontera': 9, 'prt_fild08': 1}                   # prontera 1: start cell 156,191 (rAthena @go); 2: + the NPC sprites (docs/design/ro_npc.md); 3: + the w36 item looks / icons (and, on its own branch, the w37 jobs2 bodies); 4: NPC + items; 5: NPC + items + jobs2 bodies (w37 integrate); 6: + the 16 first-job test NPC sprites (w41 jobs, docs/design/ro_jobs1.md); 7: + the w43 equip look (indexed hair / body, RPAL, robes, IMF flags); 8: + the w44 body actions (sit, pickup, attack1, hurt, dead, cast; docs/design/ro_anim.md); 9: + the w46 second-job guild NPC stand-ins (JOB2_NPC_IDS); prt_fild08 1: w34 monster + item sprites
EQUIP_LOOK = True                                              # w43 equip (docs/design/ro_equip.md): indexed hair + Novice body, robes, RPAL palette rows, IMF draw-order flags
JOB2_SPRITES = True                                            # w37 jobs2 (docs/design/ro_jobs2.md): append the second-job bodies body_j<id>_<sex> to the start pack
SPRITE_SEX = 'm'                                               # body_m / head_m (the scene's GsvrRoSprite names)
BuildError = mappack.BuildError


class Cancelled(Exception):
    pass


def warm_after(name):
    """rosebuild W10 warm-up hook: the world asked for `name` -> the maps it asks for next (they go to the front of the background queue).
    Towns: the world fetches only the maps resident around the player (GsvrRoMaps.LResident == towns.resident), in map id order; the culvert floors are built after each other once
    one is asked for; the dungeons lane never warms in the background (on demand only, docs/design/ro_dungeons.md)."""
    if name not in MAP_ORDER or name in dungeons.NAMES or name in jobmaps.NAMES or name in maps2.NAMES or name in maps3.NAMES:          # w46 maps2 + w41: the job maps are on demand like the dungeons
        return []
    if name in culvert.NAMES:
        return culvert.warm_after(name)
    return [m for m in towns.warm_after(name, names=MAP_ORDER) if m not in dungeons.NAMES and m not in culvert.NAMES and m not in jobmaps.NAMES and m not in maps2.NAMES and m not in maps3.NAMES]


def pack_version(name):
    if name == roefx.FX_PACK:
        return 'ro-fx-%d' % roefx.FX_REV
    if name in NPC_PACKS:
        return 'ro-npc-%d.%d' % (npcs.NPC_REV, sprite.RSAN_VERSION)
    if name in UI_PACKS:
        return 'ro-ui-%d' % ui.UI_REV
    if name in AUDIO_PACKS:
        from . import audio
        return 'ro-snd-%d' % audio.AUDIO_REV
    rev = PACK_REV.get(name, 0)
    return VERSION if not rev else '%s-p%d' % (VERSION, rev)


def _grf_names(folder):
    try:
        return sorted(n for n in os.listdir(folder) if n.lower().endswith('.grf') and os.path.isfile(os.path.join(folder, n)))
    except OSError:
        return []


def check_install(folder):
    """-> (ok, reason): the folder holds data.grf (any case) starting with a GRF magic (0x200 "Master of Magic" / 0x300 "Event Horizon"). No path in the reason."""
    if not folder or not os.path.isdir(folder):
        return False, 'folder does not exist'
    names = _grf_names(folder)
    data = [n for n in names if n.lower() == 'data.grf']
    if not data:
        return False, 'no data.grf in the folder'
    try:
        with open(os.path.join(folder, data[0]), 'rb') as f:
            head = f.read(16)
    except OSError:
        return False, 'data.grf is not readable'
    if not head.startswith(MAGICS):
        return False, 'data.grf is not a GRF archive'
    return True, 'ok'


def install_files(folder):
    """relative paths whose size + mtime key the build cache: every *.grf at the top of the client folder (data.grf, rdata.grf ...)."""
    return _grf_names(folder)


def install_extra(folder):
    """lines for the build manager's install fingerprint besides the GRFs: the loose BGM folder next to data.grf (any case: found, file count, bytes, newest mtime) and the
    mp3 decoder a build would use. Copying BGM\\ into the client or installing miniaudio / ffmpeg makes the cached snd_<map> packs (built without music) stale."""
    import rose_audio as AU
    found = n = size = mt = 0
    try:
        for nm in sorted(os.listdir(folder)):
            d = os.path.join(folder, nm)
            if nm.lower() == 'bgm' and os.path.isdir(d):
                found = 1
                for fn in os.listdir(d):
                    try:
                        st = os.stat(os.path.join(d, fn))
                    except OSError:
                        continue
                    n += 1
                    size += st.st_size
                    mt = max(mt, int(st.st_mtime))
                break
    except OSError:
        pass
    dec = 'miniaudio' if getattr(AU, 'miniaudio', None) is not None else ('ffmpeg' if AU.ffmpeg_path() else 'none')
    return ['bgm %d %d %d %d' % (found, n, size, mt), 'decoder %s' % dec]


# monster anims baked into the atlas: idle walk attack hurt. 'dead' (anim 4) is left out on purpose: the iRO poring's death is an expanding
# 3.6x-scaled ghost layer, 1.8 Mpx of composed frames (a 2048^2 RGBA page, 16 MB) for 11 frames; the world falls back to idle for it
MONSTER_KEEP = (0, 1, 2, 3)
# w34 MONSTERS (docs/design/ro_mobs.md): the fight field's pack carries the monsters of its rAthena spawn list (npc/pre-re/mobs/fields/prontera.txt) with ALL five
# anims, the death too: a composite larger than MOB_MAX_SIDE px is composed again at 1 / 2^n (sprite.FF_SHIFT; the poring's 370 px ghost -> 93 px), and the item
# sprites of their mob_db drops (data\idnum2itemresnametable.txt -> data\sprite\<items>\<name>), named item_<id>. Sprite names = the client file names
# under data\sprite\<monsters>\ (looked up read-only in the player's own GRF). The world's table Editor/ro_mobs.txt holds the same classes / drop items
# (test_ro_mobs.py compares both lists).
MONSTER_ALL = (0, 1, 2, 3, 4)
MOB_MAX_SIDE = 128
MOB_SPRITES = {'prt_fild08': ('lunatic', 'pupa', 'poring', 'drops')}
DROP_ITEMS = {'prt_fild08': (507, 512, 515, 619, 620, 622, 705, 713, 909, 915, 935, 938, 949, 1002, 1010, 1202, 1602, 2102, 2262, 4001, 4003, 4004, 4006)}
# w41 (docs/design/ro_mobs.md "w41 multi-map"): EVERY monster map's pack carries its own monsters + drop items (generated lists, ro_conv/mobsets.py); the w34 field keeps its
# lists above (identical to the generated ones). Each such map's pack gets PACK_REV >= MOB_PACK_REV, so packs built before w41 (map only) are rebuilt with their sprites.
from . import mobsets as _mobsets          # noqa: E402
for _m, _v in _mobsets.MOB_SPRITES.items():
    MOB_SPRITES.setdefault(_m, _v)
for _m, _v in _mobsets.DROP_ITEMS.items():
    DROP_ITEMS.setdefault(_m, _v)
MOB_PACK_REV = 1
for _m in MOB_SPRITES:
    if _m != SPRITE_PACK and PACK_REV.get(_m, 0) < MOB_PACK_REV:
        PACK_REV[_m] = MOB_PACK_REV
# w53 mobskills (docs/design/ro_mobskills.md): the slave / class-change classes a map's summoners bring (generated ro_conv/mobskill_sets.py) ride in that map's pack too;
# their packs get PACK_REV >= MOBSKILL_PACK_REV, so packs built before w53 are rebuilt with the extra sprites + drop items.
from . import mobskill_sets as _mss          # noqa: E402
MOBSKILL_PACK_REV = 3
for _m, _v in _mss.EXTRA_SPRITES.items():
    if _m not in MOB_SPRITES:
        continue
    MOB_SPRITES[_m] = tuple(MOB_SPRITES[_m]) + tuple(x for x in _v if x not in MOB_SPRITES[_m])
    DROP_ITEMS[_m] = tuple(sorted(set(DROP_ITEMS.get(_m, ())) | set(_mss.EXTRA_DROPS.get(_m, ()))))
    if PACK_REV.get(_m, 0) < MOBSKILL_PACK_REV:
        PACK_REV[_m] = MOBSKILL_PACK_REV
PATH_MONSTER = 'data/sprite/몬스터/'                           # + <name>.spr / .act (CP949 in the GRF)
PATH_ITEM = 'data/sprite/아이템/'
ITEM_TABLE = 'data/idnum2itemresnametable.txt'
OTHER_SEX = {'m': 'f', 'f': 'm'}

# NPC sprites of the Prontera NPCs (docs/design/ro_npc.md, Editor/ro_npcs.txt): rAthena sprite id -> client file name under data/sprite/npc (read-only lookup in the owner's
# client: JT_<name> in npcidentity.lub gives the id, jobName.lub the file). The pack names them 'npc_<id>' (idle, 8 directions, the standing pose).
NPC_SPRITES = {105: '8w_soldier', 53: '1_m_innkeeper', 54: '1_m_jobguider'}
# w41 first jobs (docs/design/ro_jobs1.md, Editor/ro_jobs1.txt): the NPCs of the six job tests. Looked up ONCE in the owner's client 2026-11-25 (npcidentity.lub JT id ->
# jobname.lub file, both files exist under data/sprite/npc); 111 = JT_HIDDEN_NPC (the Mixing Machine / Bookshelf: no sprite, the map model is the look).
NPC_SPRITES.update({119: '2_m_swordmaster', 85: '4_m_03', 92: '4_f_03', 123: '2_f_magicmaster', 88: '4_m_orient01', 93: '4_f_04', 60: '1_m_pastor', 89: '4_m_orient02',
                    95: '4_f_sister', 98: '4w_m_02', 86: '4_m_04', 83: '4_m_01', 47: '1_m_01', 116: '4_f_kafra2', 69: '1_f_04', 118: '2_m_thiefmaster'})
NPC_PATH = 'data/sprite/npc/%s'
# w46 jobs2 (docs/design/ro_jobs2.md "w46", Editor/ro_jobs2q.txt): the stand-in second-job guild NPCs (registry rows j2_*) of guilds whose maps the world does not load.
# Their client file comes from the owner's npcidentity.lub + jobname.lub (ro_conv.npcs.id_files, checked once 2026-11-26: all 19 exist); start pack PACK_REV prontera 9.
JOB2_NPC_IDS = (55, 57, 60, 70, 86, 101, 109, 120, 724, 727, 730, 735, 741, 743, 744, 747, 749, 750, 754)


def _pair(vfs, sex):
    out = []
    for nm, kind, base in (('body_' + sex, sprite.KIND_BODY, sprite.PATH_BODY[sex]), ('head_' + sex, sprite.KIND_HEAD, sprite.PATH_HEAD[sex])):
        out.append(sprite.convert_sprite(nm, kind, vfs.read(base + '.spr'), vfs.read(base + '.act'), anims=items.PLAYER_KEEP))
    return out


def npc_sprites(vfs, warn=None):
    """-> [sprite dict 'npc_<id>'] for every NPC_SPRITES entry the client has (idle only, kind monster = body only). A client without one gets a warning and the
    world draws a placeholder for that NPC; the other sprites are not affected."""
    out = []
    miss = []
    _E = (sprite.FormatError, ValueError, IndexError, struct.error)
    for sid in sorted(NPC_SPRITES):
        base = NPC_PATH % NPC_SPRITES[sid]
        try:
            sp = sprite.convert_sprite('npc_%d' % sid, sprite.KIND_MONSTER, vfs.read(base + '.spr'), vfs.read(base + '.act'), anims=(sprite.ANIM_IDLE,))
            for d in range(1, 8):          # a one-direction NPC (ACT with fewer than 8 actions) shows its one pose from every side
                if not sp['clips'][d]['frames']:
                    sp['clips'][d] = sp['clips'][0]
            out.append(sp)
        except KeyError:
            miss.append(sid)
        except _E as e:
            if warn is not None:
                warn.append('sprites: npc %d: %s' % (sid, e))
    if miss and warn is not None:          # w41: ONE line for every missing NPC sprite (19 ids would push the other warnings out of the 40-line status list)
        warn.append('sprites: %s not in the client data (placeholders in the world)' % ', '.join('npc %d' % s for s in miss))
    out.extend(job2_npc_sprites(vfs, warn))
    return out


def job2_npc_sprites(vfs, warn=None):
    """w46 jobs2: 'npc_<id>' (idle) for JOB2_NPC_IDS not in NPC_SPRITES, the file name through the client's own npcidentity / jobname tables. A client without
    them (or a missing file) keeps the placeholder; one warning line."""
    from . import npcs
    try:
        table = npcs.id_files(vfs)
    except (AttributeError, KeyError):
        table = {}
    if not table:          # a client without the two tables: placeholders, the NPC sprite warning line of npc_sprites stays the only one
        return []
    out, miss = [], []
    _E = (sprite.FormatError, ValueError, IndexError, struct.error)
    for sid in JOB2_NPC_IDS:
        if sid in NPC_SPRITES:
            continue
        fn = table.get(sid)
        if not fn:
            miss.append(sid)
            continue
        base = NPC_PATH % fn
        try:
            sp = sprite.convert_sprite('npc_%d' % sid, sprite.KIND_MONSTER, vfs.read(base + '.spr'), vfs.read(base + '.act'), anims=(sprite.ANIM_IDLE,))
            for d in range(1, 8):
                if not sp['clips'][d]['frames']:
                    sp['clips'][d] = sp['clips'][0]
            out.append(sp)
        except KeyError:
            miss.append(sid)
        except _E as e:
            if warn is not None:
                warn.append('sprites: npc %d: %s' % (sid, e))
    if miss and warn is not None:
        warn.append('sprites: second-job guild NPCs %s not in the client data (placeholders)' % ', '.join(str(s) for s in miss))
    return out


def sprite_sections(vfs, sex=SPRITE_SEX, warn=None):
    """-> [(tag, version, count, data)] RSAT + RSAN: Novice body + head of `sex` (required, first: the reference height), the poring
    (idle walk attack hurt) and the other sex's Novice body + head when present. [] when the client lacks the first pair.
    Order: body_<sex>, head_<sex>, poring, body_<other>, head_<other>, then (JOB2_SPRITES) the second-job bodies body_j<id>_<sex> (jobs.py)
    (the world looks sprites up by name)."""
    _E = (sprite.FormatError, ValueError, IndexError, struct.error)
    try:
        sp = _pair(vfs, sex)
    except KeyError as e:
        if warn is not None:
            warn.append('sprites: %s not in the client data (placeholders in the world)' % os.path.basename(str(e).strip("'")))
        return []
    except _E as e:
        if warn is not None:
            warn.append('sprites: %s' % e)
        return []
    try:
        sp.append(sprite.convert_sprite('poring', sprite.KIND_MONSTER, vfs.read(sprite.PATH_PORING + '.spr'), vfs.read(sprite.PATH_PORING + '.act'),
                                        anims=MONSTER_KEEP))
    except KeyError:
        pass
    except _E as e:
        if warn is not None:
            warn.append('sprites: poring: %s' % e)
    o = OTHER_SEX.get(sex)
    if o:
        try:
            sp += _pair(vfs, o)
        except KeyError:
            pass
        except _E as e:
            if warn is not None:
                warn.append('sprites: novice %s: %s' % (o, e))
    ref = (sp[0], sp[1])
    sp += npc_sprites(vfs, warn)
    sp += items.extra_sprites(vfs, sex, warn)       # w36 items: headgear / weapon / shield looks + item icons, AFTER every other sprite (names + order of the rest unchanged)
    if JOB2_SPRITES:                                            # w37 jobs2: AFTER every other sprite (names + order of the others unchanged)
        sp += jobs.job_sprites(vfs, warn=warn)
    rpal = None
    if EQUIP_LOOK:                                              # w43 equip: indexed hair + Novice body, robes, the palette rows (AFTER everything else: names + order of the others unchanged)
        eq_sp, rpal = equip.equip_sprites(vfs, warn)
        sp += eq_sp
        equip.apply_imf_all(vfs, sp, warn)                      # draw order (head behind body) of every body sprite the client has an IMF for
    rsat, rsan = sprite.build_set(sp, ref=ref)
    secs = [(sprite.TAG_RSAT, 1, 1, rsat), (sprite.TAG_RSAN, sprite.RSAN_VERSION, len(sp), rsan)]
    if rpal is not None:
        secs.append((equip.TAG_RPAL, equip.RPAL_VERSION, 1, rpal))
    return secs


def item_resnames(vfs):
    """{item id: client resource name} from the client's data/idnum2itemresnametable.txt (CP949 'id#name#' lines); {} when the client has none."""
    try:
        raw = vfs.read(ITEM_TABLE)
    except KeyError:
        return {}
    out = {}
    for line in raw.decode('cp949', 'replace').splitlines():
        line = line.split('//')[0].strip()
        p = line.split('#')
        if len(p) >= 2 and p[0].strip().isdigit() and p[1].strip():
            out[int(p[0])] = p[1].strip()
    return out


def mob_sprite_sections(vfs, name, warn=None):
    """w34: -> [(tag, version, count, data)] RSAT + RSAN of the fight field `name`: its monsters (MOB_SPRITES, all five anims, FF_SHIFT above MOB_MAX_SIDE) and the
    item sprites of their drops (item_<id>, idle only), scaled like the player: ref height = the Novice of the same client (the world uses one pxM for both sets).
    [] when the map has no monster list or none of its monsters is in the client."""
    _E = (sprite.FormatError, ValueError, IndexError, struct.error)
    sp = []
    for m in MOB_SPRITES.get(name, ()):
        try:
            sp.append(sprite.convert_sprite(m, sprite.KIND_MONSTER, vfs.read(PATH_MONSTER + m + '.spr'), vfs.read(PATH_MONSTER + m + '.act'),
                                            anims=MONSTER_ALL, max_side=MOB_MAX_SIDE))
        except KeyError:
            if warn is not None:
                warn.append('sprites: monster %s not in the client data (placeholder in the world)' % m)
        except _E as e:
            if warn is not None:
                warn.append('sprites: monster %s: %s' % (m, e))
    if not sp:
        return []
    res = item_resnames(vfs)
    for iid in DROP_ITEMS.get(name, ()):
        r = res.get(iid)
        if not r:
            if warn is not None:
                warn.append('sprites: item %d has no resource name in the client table' % iid)
            continue
        try:
            sp.append(sprite.convert_sprite('item_%d' % iid, sprite.KIND_OTHER, vfs.read(PATH_ITEM + r + '.spr'), vfs.read(PATH_ITEM + r + '.act'), anims=(0,), still_anims=(0,)))
        except KeyError:
            if warn is not None:
                warn.append('sprites: item %d sprite not in the client data' % iid)
        except _E as e:
            if warn is not None:
                warn.append('sprites: item %d: %s' % (iid, e))
    refh = None
    try:
        body, head = _pair(vfs, SPRITE_SEX)
        refh = sprite.ref_height_px(body, head)
    except (KeyError,) + _E:
        pass
    rsat, rsan = sprite.build_set(sp, ref=(None, None), ref_px=refh if refh else 0.0)
    return [(sprite.TAG_RSAT, 1, 1, rsat), (sprite.TAG_RSAN, sprite.RSAN_VERSION, len(sp), rsan)]


def build(name, folder, out_path, progress=None, cancel=None, log=None):
    if name == roefx.FX_PACK:
        return roefx.build(name, folder, out_path, progress=progress, cancel=cancel, log=log)
    if name in NPC_PACKS:
        return npcs.build(name, folder, out_path, progress=progress, cancel=cancel, log=log)
    if name in UI_PACKS:
        return ui.build(name, folder, out_path, progress=progress, cancel=cancel, log=log)
    if name in AUDIO_PACKS:
        from . import audio
        return audio.build(name, folder, out_path, progress=progress, cancel=cancel, log=log)
    if name not in PACKS:
        raise BuildError('unknown ro pack %r' % name)
    ok, why = check_install(folder)
    if not ok:
        raise BuildError('Ragnarok folder not usable: %s' % why)
    weights = (('grf', 0.0, 0.03), ('sprites', 0.03, 0.10), ('terrain', 0.10, 0.45), ('objects', 0.45, 0.95), ('write', 0.95, 1.0))
    span = dict((s, (a, b)) for s, a, b in weights)

    def tick(stage, f):
        if cancel is not None and cancel():
            raise Cancelled(name)
        if progress is not None:
            a, b = span.get(stage, (0.0, 1.0))
            progress(stage, a + (b - a) * max(0.0, min(1.0, f)))

    lines = []

    def _log(s):
        lines.append(s)
        if log is not None:
            log(s)

    tick('grf', 0.0)
    try:
        vfs = Vfs(folder)
    except GrfError as e:
        raise BuildError(str(e).replace(folder, '<folder>'))
    warns = list(vfs.errors)
    tick('sprites', 0.0)
    extra = []
    if name == SPRITE_PACK:                                     # the other maps carry no sprites (2.1 MB less per pack, parsed once)
        try:
            extra = sprite_sections(vfs, warn=warns)
        except sprite.FormatError as e:
            extra = []
            warns.append('sprites: %s' % e)
    elif name in MOB_SPRITES:                                   # w34: the fight field carries its monsters + drop items
        try:
            extra = mob_sprite_sections(vfs, name, warn=warns)
        except sprite.FormatError as e:
            extra = []
            warns.append('sprites: %s' % e)
    try:
        pack, man = mappack.build(vfs, name, log=_log, extra_sections=extra, tick=tick, start=PACKS[name].get('start'))
    except GrfError as e:
        raise BuildError(str(e))
    tick('write', 0.5)
    part = out_path + '.part'
    d = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(d, exist_ok=True)
    try:
        with open(part, 'wb') as f:
            f.write(pack)
        os.replace(part, out_path)
    finally:
        if os.path.exists(part):
            os.remove(part)
    tick('write', 1.0)
    warns += ['%s x%d' % (k, v) for k, v in sorted(man.get('warnings', {}).items())]
    return {'name': name, 'bytes': len(pack), 'sprites': bool(extra), 'grid': man.get('grid'), 'start_cell': man.get('start_cell'),
            'chunks': man.get('chunks'), 'terrain_blocks': man.get('terrain_blocks'), 'warnings': warns[:40], 'log': lines[-20:]}
