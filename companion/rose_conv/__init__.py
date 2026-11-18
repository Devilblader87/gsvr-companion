"""ROSE converter inside the companion (BYOG): builds the RPG1 packs from the player's own ROSE install.

Design + fixed API: docs/design/rose_companion_build.md (lane B). The converter modules next to this file are copied verbatim from
rose-vrchat/tools by tools/sync_rose_converter.sh (VERSION_SOURCE = the rose-vrchat commit, SOURCE_HASHES = sha256 per copied file;
never edit them here). This file is the companion side: VERSION, check_install, PACKS, build.

  import rose_conv
  ok, reason = rose_conv.check_install(path)
  info = rose_conv.build("birthisland", path, "/rose/rose__birthisland.rpg1", progress=cb, cancel=should_stop)

build() runs the converter in-process with exactly the arguments of the deployed test packs (out/w5, see PACKS[...]["argv"]) and
mirrors rose_pack.main / rose_avatar_pack.main step by step, so the pack bytes are the same as the command line converter's
(test_rose_conv.py pins the source of both mains: when upstream changes them, update _zone_bytes / _avatar_bytes and WRAPPER_REV).
Only the .rpg1 is written (the command line's manifest / actors / npcs JSON files are debugging aids the companion does not serve).

Notes for the caller:
- progress(stage, frac) is called from the build thread: at every converter stage and throttled (0.5 s) while the converter reads
  client files, plus by a small heartbeat thread when the converter computes for a while without reading (keeps the gap under ~1 s).
  So progress must be thread-safe (store the values, do not touch a GUI directly).
- cancel() is polled at the stage boundaries and at most every 0.25 s while the converter reads files; True -> the .part file is
  removed and Cancelled is raised. The progress heartbeat thread polls cancel() too (every 0.5 s): once it saw True, the very next
  converter read / log line raises Cancelled without the 0.25 s throttle. A Python thread cannot be stopped from outside, so the
  bound is the longest stretch the converter computes without reading a client file or logging (atlas packing, sound encoding,
  the final pack write: a few seconds of the 50-70 s builds measured on a Mac M4), plus at most 0.5 s.
- Music needs an ogg decoder: the PyPI package miniaudio, else an ffmpeg binary ($ROSE_FFMPEG / PATH); with neither the pack has sound
  effects only (a warning). The decoder used changes the SNDS bytes (miniaudio and ffmpeg resample differently).
- The converter also honours $ROSE_SKILL_IDS (rose_gameplay, a developer override of the skill bar ids); leave it unset.
- Memory: a full zone build holds the whole pack plus its parts in memory (see the measurements in the lane B hand-back); run one
  build at a time.
"""
import gc
import json
import os
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)           # the copied converter modules import each other by plain name (import rosevfs, rpg1 as P, ...)

import rosevfs                           # noqa: E402
import rpg1 as P                         # noqa: E402
import rose_pack as RP                   # noqa: E402
import rose_lobby                        # noqa: E402
import rose_avatar_pack as RA            # noqa: E402
import rose_ui                           # noqa: E402
import rose_skilltips                    # noqa: E402

rose_skilltips.install()                 # ROSE w15: SKDB description table + TXJA skill descriptions (companion-owned, the vendored rose_skills.py stays as it is)

WRAPPER_REV = 6                          # bump when this file or lobby_default.json changes what EVERY pack contains (all packs rebuild); 6 = ROSE w15 skill tooltips (SKDB description table + TXJA skill descriptions, rose_skilltips.py); 5 = ROSE w13 weapon-class motions in the AVAT clip map (every zone pack); 4 = ROSE w12 summons (SKDB item skills + summon actors)
# per-pack revision: bump ONE entry when only that pack changes, so only it rebuilds (pack_version). birthisland 1: the whole zone (ROSE w8 mob streaming);
# birthisland 2: walk grid slope limit 2.0 (ROSE w11 leveling: at 1.0 the five arms of the island were separate walk components, 67 of 81 monster points unreachable from the start)
PACK_REV = {"birthisland": 2}
# 0.9.47g: a 2x2 fully transparent DXT3 collision texture (castlegate01coll / guroomdaricoll[2]: alpha 0 = invisible in the original) is now packed as RGBA32 instead of an
# opaque 4x4 loader stand-in (solid untextured boxes in the Junon gate): the 9 zones that use one rebuild (tools/rose_patches/tiny_dxt_alpha.patch)
PACK_REV.update({n: 1 for n in ("junonpolis", "adventurersplain", "zant", "elverloondesert", "animalake", "forestofwisdom", "kenjibeach", "gorgeofsilence", "dolphinisland")})
# per-zone walk grid slope limit (rise / run per cell) when it differs from the converter default 1.0. Birth Island: 1.75 still leaves 32 monster points unreachable, 2.0 connects all 81
# (tools/rose_spawn_fixture.py, docs/design/rose_leveling.md); the other zones are connected at 1.0 (every monster point reachable from the start)
ZONE_MAX_SLOPE = {20: 2.0}
# sha256 of the upstream main() sources that _zone_bytes / _avatar_bytes mirror (test_rose_conv.py fails when a sync changes them)
MIRRORED = {"rose_pack.main": "32af484d1d0a80dfdf4957d72910a1d4af932d422a857a47afb08923af8c5e37",
            "rose_avatar_pack.main": "e2539f0ba3ab644610d0d9a69fae4c3f992c1201e70e0152d7c5e2b3ab7428d3"}


def _read_source():
    try:
        with open(os.path.join(_HERE, "VERSION_SOURCE")) as f:
            return f.read().strip()
    except OSError:
        return "unknown"


SOURCE = _read_source()                  # full rose-vrchat commit hash of the copied converter
VERSION = "rv-%s-%d" % (SOURCE[:8], WRAPPER_REV)


def pack_version(name):
    """the version a built pack of `name` is stamped with (sidecar "conv"): VERSION, plus "-p<rev>" when PACK_REV has a revision for it.
    rosebuild compares this per pack, so bumping one PACK_REV entry rebuilds only that pack."""
    rev = PACK_REV.get(name, 0)
    return VERSION if not rev else "%s-p%d" % (VERSION, rev)
LOBBY_DEFAULT = os.path.join(_HERE, "lobby_default.json")


class BuildError(Exception):
    """the pack could not be built (message is meant for the user / status.json)"""


class Cancelled(Exception):
    """cancel() returned True; the .part file is gone"""


def _zone(zone, label, all_blocks=True):
    argv = ["--zone", str(zone)] + (["--all"] if all_blocks else []) + ["--ui"]
    return dict(kind="zone", zone=zone, all=all_blocks, ui=True, label=label, argv=argv)


# name -> spec; argv = the rose_pack.py / rose_avatar_pack.py arguments the deployed test packs (rose-vrchat out/w5) were made with
PACKS = {
    "birthisland": _zone(20, "Birth Island"),                           # the whole island (all blocks; a 5x5 window before PACK_REV birthisland 1, docs/design/rose_mobs_stream.md)
    "luxemtower": _zone(21, "Luxem Tower"),
    "adventurersplain": _zone(22, "Adventurer's Plain"),
    "breezyhills": _zone(23, "Breezy Hills"),
    "zant": _zone(1, "Zant"),
    "elverloondesert": _zone(24, "El Verloon Desert"),
    "animalake": _zone(25, "Anima Lake"),
    "forestofwisdom": _zone(26, "Forest of Wisdom"),
    "kenjibeach": _zone(27, "Kenji Beach"),
    "gorgeofsilence": _zone(28, "Gorge of Silence"),
    "junonpolis": _zone(2, "Junon Polis"),
    "dolphinisland": _zone(3, "Dolphin Island"),                         # WAVE 8: reached from zones 22 / 27 (move22_3); 0 monsters, no gates, quest NPC sends back
    "luna": _zone(51, "Luna"),                                          # WAVE 8: Junon NPC 1118 gotoLunar (5000 Zuly); snow planet, own sky + lightmaps
    "eldeon": _zone(61, "Eldeon"),                                       # WAVE 8: Junon NPC 1118 gotoEldeon (20000 Zuly); 53 MB
    "manasnowfields": _zone(52, "Mana Snowfields"),                      # W10 ZONES: Luna gate LP01 (zone 52); gates 53 / 51
    "arumicvalley": _zone(53, "Arumic Valley"),                          # W10 ZONES: Luna gate LP02
    "crystalsnowfields": _zone(54, "Crystal Snowfields"),                # W10 ZONES: Luna gate LP03
    "freezingplateau": _zone(55, "Freezing Plateau"),                    # W10 ZONES: Luna gate LP04
    "clanfield": _zone(15, "Clan Field"),                                # CLAN FIELD: zone 15 (the clan hideout AGIT01) = Junon NPC Clan-033 target (needs clan skill 3431)
    "junonclanfield": _zone(11, "Junon Clan Field"),                    # CLAN FIELD: zone 11 (JG08), the real Junon clan field; Clan-warp01..04 (reward 28) land here
    "goblincaveb1": _zone(31, "Goblin Cave (B1)"),                       # DUNGEON: the Goblin Cave floors (underground: rose_pack seals the walk grid to the cave)
    "goblincaveb2": _zone(32, "Goblin Cave (B2)"),
    "goblincaveb3": _zone(33, "Goblin Cave (B3)"),
    "forgottentemple": _zone(56, "Forgotten Temple"),                     # W18 DUNGEONS: Luna dungeon, entered by the quest teleport of Crystal Snowfields NPC EM54-001 (quests 2051 - 2053), exits (WARP rows 118 - 120) to zone 54
    "shadyjungle": _zone(62, "Shady Jungle"),                             # W19 ZONES: Eldeon field (11 x 7 = 77 blocks, 745 object chunks: scene pools 80 / 800); the two Eldeon (61) gates lead in, its gates lead back
    "lunaclanfield": _zone(59, "Luna Clan Field"),                        # W19 ZONES: entered by the clan teleport Clan-warp05 (clan member) of a Crystal Snowfields NPC, its gate leads back to zone 54
    "login": dict(kind="lobby", zone=None, all=False, ui=True, label="Character select",         # out/w5 login has UIAT + UIDL: built with --ui
                  argv=["--lobby", "--lobby-config", "<rose_conv/lobby_default.json>", "--ui"]),
    "avatar": dict(kind="avatar", zone=None, all=False, ui=False, label="Avatar + items", argv=["rose_avatar_pack.py"]),
}
# LOAD PACKAGE 5 (0.9.44): every pack with characters + clips (the zones and the login pack) carries the pre-baked bone texture BONT (rose_bont.py) and
# rebuilds once; the avatar pack has no clips, so it keeps its version (no 71 s rebuild for it). One more revision per zone / lobby entry instead of a
# WRAPPER_REV bump for exactly that reason.
BONT_REV = 1
for _n, _s in PACKS.items():
    if _s["kind"] != "avatar":
        PACK_REV[_n] = PACK_REV.get(_n, 0) + BONT_REV
del _n, _s
PACK_REV["avatar"] = 4          # ROSE w14: the avatar pack carries QGLB (global quest texts, rose_qglob.py) + EFCT (skill / npc / weapon effects, rose_effects.py); only it rebuilds.
#                                 3 = fx11: EFCT version 2 (real effect meshes + ZMO vertex animation, dummy bones, status / item / level-up effects, 64 emitters per effect)
#                                 4 = w17-fx: EFCT version 3 (textures at full resolution in variable-size cells, per-part blend / z / cull / lighting flags, part transform
#                                     animations, cast effects on the motion frame events, the party level-up effect)


# ------------------------------------------------------------------ install check
IDX_MAX_READ = 64 * 1024 * 1024          # data.idx: never read more than this (the store list sits at its start)


def safe_store_name(name):
    """a VFS store name from data.idx is a plain relative path inside the install: no "..", not absolute, no drive."""
    n = name.replace("\\", "/")
    if not n or n.startswith("/") or os.path.isabs(name) or (len(n) > 1 and n[1] == ":") or "\0" in n:
        return False
    return all(part not in ("..",) for part in n.split("/"))


def install_files(path):
    """relative paths of the VFS index + stores (data.idx and every *.VFS it names): what a build reads from the VFS.
    Lane C fingerprints these (size, mtime). Raises OSError / ValueError on an unreadable index or a store name that leaves
    the install (".." / absolute). Reads at most IDX_MAX_READ bytes of data.idx."""
    with open(os.path.join(path, "data.idx"), "rb") as f:
        d = f.read(IDX_MAX_READ)
    if len(d) < 12:
        raise ValueError("data.idx too short")
    n = int.from_bytes(d[8:12], "little")
    if not (0 < n < 1000):
        raise ValueError("data.idx: bad store count %d" % n)
    pos, out = 12, ["data.idx"]
    for _ in range(n):
        if pos + 2 > len(d):
            raise ValueError("data.idx truncated")
        ln = int.from_bytes(d[pos:pos + 2], "little")
        pos += 2
        name = d[pos:pos + ln - 1].decode("cp949", "replace")
        pos += ln + 4
        if pos > len(d) or not name:
            raise ValueError("data.idx truncated")
        if not safe_store_name(name):
            raise ValueError("data.idx names a store outside the install: %r" % name[:80])
        if name.upper() != "ROOT.VFS":
            out.append(name)
    return out


def check_install(path):
    """-> (ok, reason). Cheap: data.idx readable, every VFS store it names present, the loose window XML folder (3Ddata/Control/Xml,
    needed by the zone packs' UI) present. Missing Sound folder is not fatal (packs without sound), it is named in the reason."""
    if not path or not os.path.isdir(path):
        return False, "not a folder: %s" % (path or "(empty)")
    if not os.path.isfile(os.path.join(path, "data.idx")):
        return False, "no data.idx in %s (pick the ROSE client folder)" % path
    try:
        files = install_files(path)
    except (OSError, ValueError) as e:
        return False, "data.idx unreadable: %s" % e
    missing = [f for f in files[1:] if not os.path.isfile(os.path.join(path, f))]
    if missing:
        return False, "VFS files missing: %s" % ", ".join(missing[:5])
    if not rose_ui.find_xml_dir(path):
        return False, "3Ddata/Control/Xml missing in %s (the client's window files)" % path
    if not rose_ui.find_ci(path, "Sound"):
        return True, "ok (no Sound folder: packs without sound)"
    return True, "ok"


def _tree_stats(d, cap=50000):
    """(file count, total bytes, max mtime) of a folder tree; stat only, at most cap files"""
    n = size = mt = 0
    for root, _dirs, files in os.walk(d):
        for fn in files:
            try:
                st = os.stat(os.path.join(root, fn))
            except OSError:
                continue
            n += 1
            size += st.st_size
            mt = max(mt, int(st.st_mtime))
            if n >= cap:
                return n, size, mt
    return n, size, mt


def decoder():
    """the ogg decoder a build would use: "miniaudio" | "ffmpeg" | "none" (the SNDS bytes differ per decoder)"""
    import rose_audio
    if getattr(rose_audio, "miniaudio", None) is not None:
        return "miniaudio"
    return "ffmpeg" if rose_audio.ffmpeg_path() else "none"


def install_extra(path):
    """lines for the build manager's install fingerprint besides the VFS files: the loose folders a build reads
    (3Ddata/Control/Xml window files, Sound: exists, file count, total size, max mtime) and the ogg decoder available.
    Adding a Sound folder or installing miniaudio / ffmpeg makes the cached packs stale."""
    out = []
    try:
        xml = rose_ui.find_xml_dir(path)
    except OSError:
        xml = None
    try:
        snd = rose_ui.find_ci(path, "Sound")
    except OSError:
        snd = None
    for label, d in (("xml", xml), ("sound", snd)):
        if d and os.path.isdir(d):
            out.append("%s 1 %d %d %d" % ((label,) + _tree_stats(d)))
        else:
            out.append("%s 0" % label)
    out.append("decoder %s" % decoder())
    return out


# ------------------------------------------------------------------ progress / cancel
# converter log prefix -> (stage shown to the player, frac when it starts); fracs measured 2026-11-14 on a Mac M4
# (Birth Island 60 s, Adventurer's Plain 64 s, login 49 s: the tables / sound / UI part is the longest)
_ZONE_STAGES = [
    ("zone ", "terrain textures", 0.02),
    ("atlas:", "terrain and objects", 0.10),
    ("objects:", "characters", 0.22),
    ("characters:", "walk grid", 0.33),
    ("walk grid:", "tables, quests, sound, UI", 0.35),
]
_AVATAR_STAGES = [                       # the avatar build logs only at its very end (71 s, all of it in add_equipment)
    ("avatar items:", "ground models", 0.95),
    ("field models:", "textures", 0.96),
    ("textures:", "packing", 0.965),
]
_FIRST_STAGE = {"zone": "reading the client", "lobby": "reading the client", "avatar": "item models"}


class _Run:
    """progress + cancel state of one build (no module state: two builds never share anything)."""

    def __init__(self, progress, cancel, stages, expect_s):
        self.progress, self.cancel = progress, cancel
        self.stages = stages
        self.expect_s = expect_s
        self.stage, self.f0, self.f1 = "reading the client", 0.0, stages[0][2] if stages else 0.9
        self.t_stage = self.t_emit = self.t_cancel = time.monotonic()
        self.messages = []
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.cancel_seen = False         # the heartbeat saw cancel() == True: the next check raises at once

    def _frac(self, now):
        # creep from f0 towards f1 (never reaching it) with the expected stage length
        span = max(1.0, self.expect_s * (self.f1 - self.f0))
        k = min(0.95, (now - self.t_stage) / span)
        return self.f0 + (self.f1 - self.f0) * k

    def emit(self, force=False):
        now = time.monotonic()
        with self.lock:
            if not force and now - self.t_emit < 0.5:
                return
            self.t_emit = now
            stage, frac = self.stage, self._frac(now)
        if self.progress:
            self.progress(stage, round(min(1.0, max(0.0, frac)), 4))

    def check(self, force=False):
        if self.cancel_seen:
            raise Cancelled()
        now = time.monotonic()
        if self.cancel and (force or now - self.t_cancel >= 0.25):
            self.t_cancel = now
            if self.cancel():
                raise Cancelled()

    def set_stage(self, stage, f0, f1):
        with self.lock:
            self.stage, self.f0, self.f1 = stage, f0, f1
            self.t_stage = time.monotonic()
        self.emit(force=True)
        self.check(force=True)

    def log(self, msg):
        """the converter's log(): stage boundaries; everything else is kept as a warning line"""
        msg = str(msg)
        for i, (prefix, stage, f0) in enumerate(self.stages):
            if msg.startswith(prefix):
                f1 = self.stages[i + 1][2] if i + 1 < len(self.stages) else 0.95
                self.set_stage(stage, f0, f1)
                return
        self.messages.append(msg)
        self.tick()

    def tick(self):
        self.emit()
        self.check()

    def heartbeat(self):
        while not self.done.wait(0.5):
            if self.cancel and not self.cancel_seen:
                try:
                    if self.cancel():
                        self.cancel_seen = True
                except Exception:
                    pass
            if time.monotonic() - self.t_emit >= 0.9:
                try:
                    self.emit()
                except Exception:
                    pass


class _Vfs(rosevfs.Vfs):
    """rosevfs.Vfs that ticks progress / cancel on every read (same data, same behaviour)."""

    def __init__(self, root, run):
        super().__init__(root)
        self._run = run

    def read(self, path):
        self._run.tick()
        return super().read(path)


# ------------------------------------------------------------------ the converter calls (mirror rose_pack.main / rose_avatar_pack.main)
def _zone_bytes(spec, install_dir, run):
    """rose_pack.main for `--zone N [--all] --ui` and `--lobby --lobby-config lobby_default.json` with every other option at its default."""
    lobby = spec["kind"] == "lobby"
    zone_id = spec["zone"]
    lobby_camera = lobby_positions = None
    if lobby:
        try:
            lcfg = rose_lobby.resolve(None, None, None, LOBBY_DEFAULT, env={})
        except ValueError as e:
            raise BuildError(str(e))
        zone_id, lobby_camera, lobby_positions = lcfg["zone"], lcfg["camera"], lcfg["positions"]
    if zone_id is None:
        zone_id = 20
    vfs = _Vfs(install_dir, run)
    size = 2 if lobby else 5
    zone = RP.zone_row(vfs, zone_id)
    zon = RP.F.zon(vfs.read(zone["zon"]))
    window = None
    center = None
    if spec["all"]:
        window = "all"
    elif lobby and rose_lobby.lobby_center_block(vfs, lobby_positions, lobby_camera) is not None:
        center = rose_lobby.lobby_center_block(vfs, lobby_positions, lobby_camera)
    else:
        st = [p for nm, p in zon["events"] if nm == zone["start"]]
        if not st:
            raise BuildError("zone %d has no start event" % zone_id)
        center = RP.Window(0, 0, 1).block_of_file(st[0][0], st[0][2])
    pack, man, _side = RP.convert(vfs, zone, center, size, 2.5, 2, ZONE_MAX_SLOPE.get(zone_id, 1.0), 1.0,
                                  chars=True, avatar=True, anim_fps=RP.C.FPS_OUT, with_actj=False, window=window,
                                  atlas_mips=RP.ATLAS_MIPS, atlas_slot=RP.SLOT_PX, env=True, lightmaps=True, sky_px=1024, ui=spec["ui"],
                                  audio=True, audio_opts={},
                                  lobby=dict(positions=lobby_positions, camera=lobby_camera) if lobby else None,
                                  log=run.log)
    warnings = ["%s (x%d)" % (k, v) for k, v in sorted((man.get("warnings") or {}).items())]
    return pack, warnings


def _avatar_bytes(spec, install_dir, run):
    """rose_avatar_pack.main with its defaults (FLDM ground models included)."""
    vfs = _Vfs(install_dir, run)
    sections, info = RA.build(vfs, log=run.log, with_field=True)
    # ROSE w14 GLOBAL QUEST TEXTS (QGLB + TXJA, PACK_REV avatar 1). A client without the quest tables simply yields no section (rose_qglob.build returns nothing);
    # any real error (import failure in a frozen build, a table that does not parse) FAILS the avatar build: the pack must never be cached as good without QGLB.
    import rose_qglob
    import rose_text
    rose_text.reset()
    gs, ginfo, gj = rose_qglob.build(vfs)
    sections = list(sections) + gs + gj
    if gs:
        run.log('global quest texts: %d quests, %d quest items, %d bytes' % (ginfo['quests'], ginfo['quest_items'], ginfo['bytes']))
    pack = P.write_pack(sections)
    warnings = ["%s (x%d)" % (k, v) for k, v in sorted((info.get("warnings") or {}).items())]
    return pack, warnings


# typical seconds of a build on a Mac M4 (lane B measurement); only shapes the progress creep
_EXPECT_S = {"zone": 62.0, "lobby": 50.0, "avatar": 71.0}


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def build(name, install_dir, out_path, progress=None, cancel=None):
    """Build one pack into out_path + ".part", then os.replace -> out_path. -> dict(name, bytes, sections, seconds, warnings).
    Raises Cancelled (cancel() said stop; .part removed) or BuildError (anything else; .part removed, out_path untouched)."""
    spec = PACKS.get(name)
    if spec is None:
        raise BuildError("unknown ROSE pack %r" % (name,))
    ok, reason = check_install(install_dir)
    if not ok:
        raise BuildError("ROSE install not usable: %s" % reason)
    t0 = time.monotonic()
    run = _Run(progress, cancel, _AVATAR_STAGES if spec["kind"] == "avatar" else _ZONE_STAGES, _EXPECT_S[spec["kind"]])
    part = out_path + ".part"
    hb = threading.Thread(target=run.heartbeat, name="rose_conv-progress", daemon=True)
    hb.start()
    pack = None
    try:
        run.set_stage(_FIRST_STAGE[spec["kind"]], 0.0, run.stages[0][2])
        if spec["kind"] == "avatar":
            pack, warnings = _avatar_bytes(spec, install_dir, run)
        else:
            pack, warnings = _zone_bytes(spec, install_dir, run)
        run.set_stage("writing", 0.97, 0.99)
        sections = [t.decode("ascii") for t in P.read_pack(pack)["order"]]
        d = os.path.dirname(os.path.abspath(out_path))
        os.makedirs(d, exist_ok=True)
        with open(part, "wb") as f:
            f.write(pack)
            f.flush()
            os.fsync(f.fileno())
        run.check(force=True)
        os.replace(part, out_path)
        size = len(pack)
    except Cancelled:
        _remove(part)
        raise
    except BuildError:
        _remove(part)
        raise
    except (Exception, SystemExit) as e:          # rose_ui raises SystemExit for a client without window XML files
        _remove(part)
        raise BuildError("%s: %s: %s" % (name, type(e).__name__, e)) from e
    finally:
        run.done.set()
        hb.join(2.0)
        pack = None
        gc.collect()
    if progress:
        progress("done", 1.0)
    warnings = warnings + [m for m in run.messages if m not in warnings]
    return dict(name=name, bytes=size, sections=sections, seconds=round(time.monotonic() - t0, 2), warnings=warnings)

