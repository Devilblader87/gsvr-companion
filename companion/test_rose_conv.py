"""rose_conv (the companion's copy of the ROSE converter, docs/design/rose_companion_build.md lane B): API without game data.

  cd companion && python3 -I -m unittest discover -p "test_rose_conv.py"
"""
import hashlib
import inspect
import math
import json
import os
import re
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rose_conv  # noqa: E402
import rpg1  # noqa: E402  (importable once rose_conv put its folder on sys.path)

HERE = os.path.dirname(os.path.abspath(rose_conv.__file__))


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _fake_idx(stores):
    """data.idx with the given store names (ROOT.VFS first like the client), each with an empty file table"""
    head = bytearray(struct.pack("<III", 0, 0, len(stores)))
    names = b""
    for nm in stores:
        names += struct.pack("<H", len(nm) + 1) + nm.encode() + b"\0" + struct.pack("<I", 0)
    return bytes(head) + names


def _fake_install(root, stores=("ROOT.VFS", "DATA.VFS", "MAP.VFS"), xml=True, sound=True, make_vfs=True):
    with open(os.path.join(root, "data.idx"), "wb") as f:
        f.write(_fake_idx(stores))
    if make_vfs:
        for s in stores[1:]:
            open(os.path.join(root, s), "wb").close()
    if xml:
        os.makedirs(os.path.join(root, "3Ddata", "Control", "Xml"))
    if sound:
        os.makedirs(os.path.join(root, "Sound"))
    return root


class VersionAndTable(unittest.TestCase):
    def test_version_format(self):
        self.assertRegex(rose_conv.VERSION, r"^rv-[0-9a-f]{8}-\d+$")

    def test_pack_version(self):
        """ROSE w13: WRAPPER_REV 5 (weapon-class motions in every zone pack). ROSE w12 summons: WRAPPER_REV 4 (SKDB carries the magic item skills + summon npcs, every zone the summon actors: all packs rebuild once).
        LOAD PACKAGE 5: every zone + the login pack one revision more (BONT, the pre-baked bone texture), the avatar pack (no clips) keeps its version; Birth Island had 2 already.
        ROSE w14 effects: only the avatar pack rebuilds (EFCT, rose_effects.py). fx11: avatar 3 (EFCT version 2: effect meshes, dummy bones, statuses). w15 skilltips: WRAPPER_REV 6 (SKDB skill descriptions).
        w17-fx: avatar 4 (EFCT version 3: full resolution textures, per-part materials, transform animations, cast frame events)."""
        self.assertEqual(rose_conv.WRAPPER_REV, 6)
        self.assertEqual(rose_conv.BONT_REV, 1)
        tiny = ("junonpolis", "adventurersplain", "zant", "elverloondesert", "animalake", "forestofwisdom", "kenjibeach", "gorgeofsilence", "dolphinisland")
        self.assertEqual(rose_conv.PACK_REV, dict({n: 1 for n in rose_conv.PACKS}, birthisland=3, avatar=4, **{n: 2 for n in tiny}))   # 0.9.47g: tiny transparent DXT as RGBA32
        self.assertEqual(rose_conv.ZONE_MAX_SLOPE, {20: 2.0})
        self.assertEqual(rose_conv.pack_version("birthisland"), rose_conv.VERSION + "-p3")
        self.assertEqual(rose_conv.pack_version("avatar"), rose_conv.VERSION + "-p4")          # ROSE w14: QGLB global quest texts + EFCT effects (w17-fx: EFCT v3)
        for n in rose_conv.PACKS:
            if n not in ("birthisland", "avatar") + tiny:
                self.assertEqual(rose_conv.pack_version(n), rose_conv.VERSION + "-p1")
        self.assertEqual(rose_conv.pack_version("junonpolis"), rose_conv.VERSION + "-p2")
        self.assertTrue(re.fullmatch(r"[0-9a-f]{40}", rose_conv.SOURCE), rose_conv.SOURCE)

    def test_tiny_dxt_keeps_alpha(self):
        """0.9.47g: a 2x2 DXT3 texture (Unity cannot create block-compressed < 4x4; the loader's stand-in is OPAQUE) is packed as RGBA32 with its alpha: the fully transparent
        collision texture of the Junon gate must not turn into a solid box. Synthetic data only."""
        import rosedds
        blk = bytes(8) + struct.pack("<HHI", 0xFFFF, 0x0000, 0)          # DXT3 block: alpha 0 everywhere, colour 0 = white
        out = rosedds.to_pack_texture(dict(w=2, h=2, fmt="DXT3", levels=[blk]))
        self.assertEqual(out[0], rosedds.FMT_RGBA32)
        self.assertEqual((out[1], out[2]), (2, 2))
        self.assertEqual(out[3][0], bytes([255, 255, 255, 0]) * 4)
        opaque = struct.pack("<HHI", 0xF800, 0x0000, 0)                  # DXT1 1x1, red, 2 mip levels (1x1 only has one)
        out = rosedds.to_pack_texture(dict(w=1, h=1, fmt="DXT1", levels=[opaque]))
        self.assertEqual((out[0], out[3][0]), (rosedds.FMT_RGBA32, bytes([255, 0, 0, 255])))
        big = rosedds.to_pack_texture(dict(w=4, h=4, fmt="DXT3", levels=[blk]))          # 4x4 and larger keep the block format (DXT3 -> DXT5)
        self.assertEqual(big[0], rosedds.FMT_DXT5)

    def test_weapon_motion_table(self):
        """W13: the weapon class -> motion type table the runtime also holds (GsvrRoseItems.WeaponMotionType); every motion type is baked for the avatar"""
        import rose_chars
        self.assertEqual(rose_chars.WEAPON_CLASS_MOTION, {211: 1, 212: 3, 221: 5, 222: 6, 223: 8, 231: 10, 232: 12, 233: 9, 241: 6, 242: 14, 251: 16, 252: 7, 253: 12, 271: 11})
        self.assertEqual(rose_chars.WEAPON_MOTION_TYPES, (0, 1, 3, 5, 6, 7, 8, 9, 10, 11, 12, 14, 16))
        self.assertTrue(set(rose_chars.AVATAR_WEAPON_TYPES) <= set(rose_chars.WEAPON_MOTION_TYPES))

    def test_packs_table(self):
        P = rose_conv.PACKS
        self.assertEqual(set(P), {"birthisland", "luxemtower", "adventurersplain", "breezyhills", "zant", "elverloondesert", "animalake", "forestofwisdom", "kenjibeach", "gorgeofsilence", "goblincaveb1", "goblincaveb2", "goblincaveb3", "forgottentemple", "shadyjungle", "lunaclanfield", "junonpolis", "dolphinisland", "luna", "eldeon", "clanfield", "junonclanfield", "manasnowfields", "arumicvalley", "crystalsnowfields", "freezingplateau", "login", "avatar"})
        zones = {n: (s["zone"], s["all"]) for n, s in P.items() if s["kind"] == "zone"}
        self.assertEqual(zones, {"birthisland": (20, True), "luxemtower": (21, True), "adventurersplain": (22, True), "breezyhills": (23, True),
                                 "zant": (1, True), "elverloondesert": (24, True), "animalake": (25, True), "forestofwisdom": (26, True),
                                 "kenjibeach": (27, True), "gorgeofsilence": (28, True), "goblincaveb1": (31, True),
                                 "goblincaveb2": (32, True), "goblincaveb3": (33, True), "forgottentemple": (56, True), "shadyjungle": (62, True), "lunaclanfield": (59, True), "junonpolis": (2, True), "dolphinisland": (3, True), "luna": (51, True), "eldeon": (61, True), "clanfield": (15, True), "junonclanfield": (11, True), "manasnowfields": (52, True), "arumicvalley": (53, True), "crystalsnowfields": (54, True), "freezingplateau": (55, True)})
        for n, s in P.items():
            self.assertIn(s["kind"], ("zone", "lobby", "avatar"))
            self.assertTrue(s["label"])
            if s["kind"] == "zone":
                self.assertTrue(s["ui"])
                self.assertEqual(s["argv"][:2], ["--zone", str(s["zone"])])
        self.assertEqual(P["login"]["kind"], "lobby")
        self.assertEqual(P["avatar"]["kind"], "avatar")

    def test_lobby_default(self):
        with open(rose_conv.LOBBY_DEFAULT) as f:
            j = json.load(f)
        self.assertEqual(j["zone"], 4)
        self.assertEqual(j["camera"], "3DDATA/TITLE/CAMERA01_INSELECT01.ZMO")
        self.assertEqual(j["frame"], "bevy")
        self.assertEqual(j["scale"], 1.5)
        self.assertEqual(j["positions"], [[5205.0, 1.0, -5205.0], [5202.70, 1.0, -5206.53], [5200.00, 1.0, -5207.07],
                                          [5197.30, 1.0, -5206.53], [5195.00, 1.0, -5205.00]])
        cfg = rose_conv.rose_lobby.resolve(None, None, None, rose_conv.LOBBY_DEFAULT, env={})
        self.assertEqual(cfg, dict(zone=4, camera=j["camera"], positions=rose_conv.LOBBY_DEFAULT))

    def test_copied_files_unchanged(self):
        """SOURCE_HASHES (tools/sync_rose_converter.sh): a copied converter file must not be edited here"""
        listed = {}
        with open(os.path.join(HERE, "SOURCE_HASHES")) as f:
            for line in f:
                h, name = line.split()
                listed[name] = h
        self.assertIn("rose_pack.py", listed)
        for name, h in listed.items():
            self.assertEqual(_sha(os.path.join(HERE, name)), h, "%s edited locally: change rose-vrchat and run tools/sync_rose_converter.sh" % name)
        own = {"__init__.py", "rose_qglob.py", "rose_skilltips.py"}          # rose_qglob: companion-owned (ROSE w14 global quest texts)
        extra = [f for f in os.listdir(HERE) if f.endswith(".py") and f not in listed and f not in own]
        self.assertEqual(extra, [], "unlisted modules in rose_conv/")

    def test_mirrored_mains_pinned(self):
        """_zone_bytes / _avatar_bytes copy the upstream main() logic: a sync that changes a main must update them"""
        for key, fn in (("rose_pack.main", rose_conv.RP.main), ("rose_avatar_pack.main", rose_conv.RA.main)):
            self.assertEqual(hashlib.sha256(inspect.getsource(fn).encode()).hexdigest(), rose_conv.MIRRORED[key],
                             "%s changed upstream: re-check the mirror in rose_conv/__init__.py, then update MIRRORED + WRAPPER_REV" % key)


class CheckInstall(unittest.TestCase):
    def test_empty_and_missing(self):
        with tempfile.TemporaryDirectory() as d:
            ok, why = rose_conv.check_install(d)
            self.assertFalse(ok)
            self.assertIn("data.idx", why)
            self.assertFalse(rose_conv.check_install(os.path.join(d, "nope"))[0])
        self.assertFalse(rose_conv.check_install("")[0])
        self.assertFalse(rose_conv.check_install(None)[0])

    def test_fake_install(self):
        with tempfile.TemporaryDirectory() as d:
            _fake_install(d)
            self.assertEqual(rose_conv.check_install(d), (True, "ok"))
            self.assertEqual(rose_conv.install_files(d), ["data.idx", "DATA.VFS", "MAP.VFS"])

    def test_fake_install_problems(self):
        with tempfile.TemporaryDirectory() as d:
            _fake_install(d, make_vfs=False)
            ok, why = rose_conv.check_install(d)
            self.assertFalse(ok)
            self.assertIn("DATA.VFS", why)
        with tempfile.TemporaryDirectory() as d:
            _fake_install(d, xml=False)
            ok, why = rose_conv.check_install(d)
            self.assertFalse(ok)
            self.assertIn("Xml", why)
        with tempfile.TemporaryDirectory() as d:
            _fake_install(d, sound=False)
            ok, why = rose_conv.check_install(d)
            self.assertTrue(ok)
            self.assertIn("Sound", why)
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "data.idx"), "wb") as f:
                f.write(b"\1\2")
            ok, why = rose_conv.check_install(d)
            self.assertFalse(ok)
            self.assertIn("unreadable", why)

    def test_store_names_stay_inside(self):
        """audit 7: a data.idx naming a store outside the install (".." / absolute / drive) is refused by the wrapper"""
        for bad in ("../evil.vfs", "..\\evil.vfs", "sub/../../x.vfs", "/etc/passwd", "\\\\server\\share.vfs", "C:\\x.vfs", "c:x.vfs"):
            with tempfile.TemporaryDirectory() as d:
                _fake_install(d, stores=("ROOT.VFS", "DATA.VFS", bad), make_vfs=False)
                open(os.path.join(d, "DATA.VFS"), "wb").close()
                with self.assertRaises(ValueError, msg=bad):
                    rose_conv.install_files(d)
                ok, why = rose_conv.check_install(d)
                self.assertFalse(ok, bad)
                self.assertIn("outside the install", why)
        for good in ("DATA.VFS", "sub/MAP.VFS", "a..b.vfs"):
            self.assertTrue(rose_conv.safe_store_name(good), good)

    def test_idx_read_capped(self):
        """audit 7: at most IDX_MAX_READ bytes of data.idx are read (the store list is at its start)"""
        with tempfile.TemporaryDirectory() as d:
            _fake_install(d)
            with open(os.path.join(d, "data.idx"), "ab") as f:
                f.write(b"\0" * 5000)                   # a big file table behind the store list
            old = rose_conv.IDX_MAX_READ
            reads = []
            real_open = open

            class Spy:
                def __init__(self, fh):
                    self.fh = fh

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    self.fh.close()

                def read(self, n=-1):
                    reads.append(n)
                    return self.fh.read(n)
            try:
                rose_conv.IDX_MAX_READ = 200
                import builtins
                builtins_open = builtins.open
                builtins.open = lambda p, *a, **k: Spy(real_open(p, *a, **k)) if str(p).endswith("data.idx") else real_open(p, *a, **k)
                try:
                    files = rose_conv.install_files(d)
                finally:
                    builtins.open = builtins_open
            finally:
                rose_conv.IDX_MAX_READ = old
            self.assertEqual(files, ["data.idx", "DATA.VFS", "MAP.VFS"])
            self.assertEqual(reads, [200])
        self.assertEqual(rose_conv.IDX_MAX_READ, 64 * 1024 * 1024)

    def test_install_extra(self):
        """audit 6: Xml + Sound folder stats and the decoder are part of the fingerprint input"""
        with tempfile.TemporaryDirectory() as d:
            _fake_install(d, sound=False)
            e0 = rose_conv.install_extra(d)
            self.assertEqual(e0[1], "sound 0")
            self.assertTrue(e0[0].startswith("xml 1 0 0"))
            self.assertIn(e0[2], ("decoder miniaudio", "decoder ffmpeg", "decoder none"))
            self.assertEqual(e0[2], "decoder " + rose_conv.decoder())
            os.makedirs(os.path.join(d, "Sound", "Music"))
            with open(os.path.join(d, "Sound", "Music", "a.ogg"), "wb") as f:
                f.write(b"x" * 10)
            e1 = rose_conv.install_extra(d)
            self.assertTrue(e1[1].startswith("sound 1 1 10 "), e1)
            with open(os.path.join(d, "3Ddata", "Control", "Xml", "w.xml"), "wb") as f:
                f.write(b"<x/>")
            e2 = rose_conv.install_extra(d)
            self.assertTrue(e2[0].startswith("xml 1 1 4 "), e2)
            import rose_audio
            old = rose_audio.miniaudio, rose_audio.ffmpeg_path
            try:
                rose_audio.miniaudio, rose_audio.ffmpeg_path = None, (lambda: None)
                self.assertEqual(rose_conv.install_extra(d)[2], "decoder none")
                rose_audio.ffmpeg_path = lambda: "/usr/bin/ffmpeg"
                self.assertEqual(rose_conv.install_extra(d)[2], "decoder ffmpeg")
                rose_audio.miniaudio = object()
                self.assertEqual(rose_conv.install_extra(d)[2], "decoder miniaudio")
            finally:
                rose_audio.miniaudio, rose_audio.ffmpeg_path = old


class CancelLatency(unittest.TestCase):
    def test_heartbeat_sees_cancel(self):
        """audit 4c: the heartbeat polls cancel(); the next check raises at once (no 0.25 s throttle)"""
        import threading
        import time
        flag = {"c": False}
        run = rose_conv._Run(None, lambda: flag["c"], rose_conv._ZONE_STAGES, 60.0)
        hb = threading.Thread(target=run.heartbeat, daemon=True)
        hb.start()
        try:
            run.check(force=True)                       # not cancelled
            flag["c"] = True
            t0 = time.monotonic()
            while not run.cancel_seen and time.monotonic() - t0 < 3:
                time.sleep(0.02)
            self.assertTrue(run.cancel_seen)
            run.t_cancel = time.monotonic()             # a throttled check would skip now
            with self.assertRaises(rose_conv.Cancelled):
                run.check()
        finally:
            run.done.set()
            hb.join(2)


class BuildFlow(unittest.TestCase):
    """build() with the converter stage replaced by a fake (no game data)"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.client = os.path.join(self.tmp.name, "client")
        os.makedirs(self.client)
        _fake_install(self.client)
        self.out = os.path.join(self.tmp.name, "rose", "rose__birthisland.rpg1")
        self.saved = rose_conv._zone_bytes, rose_conv._avatar_bytes

    def tearDown(self):
        rose_conv._zone_bytes, rose_conv._avatar_bytes = self.saved
        self.tmp.cleanup()

    def fake(self, fail=None):
        def stage(spec, install_dir, run):
            run.log('zone 20 "x": window')
            run.log("atlas: 1 textures")
            run.log("objects: 1 instances")
            run.log("quests: something odd (x1)")
            if fail is not None:
                raise fail
            run.log("walk grid: done")
            return rpg1.write_pack([(b"AREA", 1, 1, b"abcd"), (b"CELL", 1, 4, b"\1\1\1\1")]), ["w1 (x2)"]
        rose_conv._zone_bytes = stage
        rose_conv._avatar_bytes = stage

    def test_success(self):
        self.fake()
        seen = []
        info = rose_conv.build("birthisland", self.client, self.out, progress=lambda s, f: seen.append((s, f)), cancel=lambda: False)
        self.assertTrue(os.path.isfile(self.out))
        self.assertFalse(os.path.exists(self.out + ".part"))
        self.assertEqual(info["name"], "birthisland")
        self.assertEqual(info["bytes"], os.path.getsize(self.out))
        self.assertEqual(info["sections"], ["AREA", "CELL"])
        self.assertIn("w1 (x2)", info["warnings"])
        self.assertIn("quests: something odd (x1)", info["warnings"])
        self.assertGreaterEqual(info["seconds"], 0)
        self.assertEqual(seen[-1], ("done", 1.0))
        fr = [f for _s, f in seen]
        self.assertEqual(fr, sorted(fr))
        self.assertTrue(all(0.0 <= f <= 1.0 for f in fr))
        self.assertIn("terrain and objects", [s for s, _f in seen])

    def test_cancel_between_stages(self):
        self.fake()
        stages = []

        def prog(s, f):
            stages.append(s)
        with self.assertRaises(rose_conv.Cancelled):
            rose_conv.build("birthisland", self.client, self.out, progress=prog, cancel=lambda: "terrain and objects" in stages)
        self.assertFalse(os.path.exists(self.out))
        self.assertFalse(os.path.exists(self.out + ".part"))
        self.assertNotIn("characters", stages)

    def test_cancel_after_part_written(self):
        self.fake()
        stages = []
        os.makedirs(os.path.dirname(self.out))
        with open(self.out, "wb") as f:
            f.write(b"old pack")
        with self.assertRaises(rose_conv.Cancelled):
            rose_conv.build("birthisland", self.client, self.out, progress=lambda s, f: stages.append(s),
                            cancel=lambda: os.path.exists(self.out + ".part"))
        self.assertFalse(os.path.exists(self.out + ".part"))
        with open(self.out, "rb") as f:
            self.assertEqual(f.read(), b"old pack")          # the old pack stays until a build completes

    def test_errors_become_builderror(self):
        for exc in (ValueError("broken table"), KeyError("3DDATA/X"), SystemExit("3Ddata/Control/Xml not found")):
            self.fake(fail=exc)
            with self.assertRaises(rose_conv.BuildError) as cm:
                rose_conv.build("avatar", self.client, self.out)
            self.assertIn("avatar", str(cm.exception))
            self.assertFalse(os.path.exists(self.out + ".part"))
            self.assertFalse(os.path.exists(self.out))

    def test_bad_name_and_install(self):
        with self.assertRaises(rose_conv.BuildError):
            rose_conv.build("nosuchzone", self.client, self.out)
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(rose_conv.BuildError) as cm:
                rose_conv.build("birthisland", d, self.out)
            self.assertIn("data.idx", str(cm.exception))


class ItemTypes(unittest.TestCase):
    """ITEMTYPES: ITEM carries the plain item types 11 / 12 / 13 (gems, materials, quest items); every code of DROP / SHOP / QUST / PRODUCT resolves to a name + icon"""

    def setUp(self):
        import rose_items
        import rose_gameplay
        import rose_shop
        self.I, self.G, self.S = rose_items, rose_gameplay, rose_shop

    def test_tables_cover_all_types(self):
        self.assertEqual(sorted(self.I.TABLES), [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14])
        self.assertEqual(self.I.TABLES[12], "LIST_NATURAL")

    def test_plain_record(self):
        cells = [""] * 19
        cells[4], cells[5], cells[7], cells[8], cells[9], cells[10], cells[16], cells[17] = "429", "60", "1", "12", "1969", "211", "33", "44"
        r = self.I.item_record(12171, cells, "Thin Bug Shell")
        self.assertEqual((r["icon"], r["price"], r["weight"], r["cls"], r["field"], r["name"]), (1969, 60, 1, 429, 211, "Thin Bug Shell"))
        self.assertEqual((r["job_mask"], r["job_class"], r["req"], r["add"], r["defence"]), (0, 0, [(0, 0), (0, 0)], [(0, 0), (0, 0)], 0))
        recs, _s, _h = self.I.dec_item(self.I.enc_item([r]))
        self.assertEqual((recs[0]["code"], recs[0]["icon"], recs[0]["name"]), (12171, 1969, "Thin Bug Shell"))

    def _secs(self):
        drop = self.G.enc_drop([], [dict(code=12171, icon=1969, type=12, price=60, weight=1, quality=12, name="x"), dict(code=12999, icon=0, type=12, price=0, weight=0, quality=0, name="")])
        shop = self.S.enc_shop([], [dict(name="t", slots=[10001, 0, 12173])], [])
        return {b"DROP": drop, b"SHOP": shop}

    def test_reachable_and_missing(self):
        reach = self.I.reachable_codes(self._secs())
        self.assertEqual(reach["DROP"], {12171, 12999})
        self.assertEqual(reach["SHOP"], {10001, 12173})
        self.assertEqual(reach["QUST"], set())
        recs = [self.I.item_record(c, [""] * 9 + ["5"], "n") for c in (10001, 12171)]
        miss = self.I.missing_codes(reach, recs)
        self.assertEqual(miss, {"DROP": [12999], "SHOP": [12173]})
        recs += [self.I.item_record(c, [""] * 9 + ["5"], "n") for c in (12173, 12999)]
        self.assertEqual(self.I.missing_codes(reach, recs), {})
        nameless = [self.I.item_record(12171, [""] * 9 + ["5"], ""), self.I.item_record(12999, [""] * 9 + ["0"], "n")]
        self.assertEqual(self.I.missing_codes(dict(DROP={12171, 12999}), nameless), {"DROP": [12171, 12999]})


class Summons(unittest.TestCase):
    """SUMMONS lane (docs/design/rose_summons.md): SKDB carries the use skills of magic items (no page) and the summon npc / gauge of summon skills; the zones carry the summon actors"""

    def setUp(self):
        import rose_skills
        self.S = rose_skills

    @staticmethod
    def _row(**cells):
        c = [""] * 72
        for k, v in cells.items():
            c[int(k[1:])] = str(v)
        return c

    def test_item_skill_without_page(self):
        c = self._row(c2=1, c5=14, c16=76, c17=90, c28=9001)                      # a capsule's skill: type 14, no page (cell 4 empty), no icon, summon npc 9001
        self.assertIsNone(self.S.skill_record(7001, c, ""))                      # not a player skill
        npc = dict(rows=10000, cells={})
        npc["cells"] = [[""] * 30 for _ in range(10000)]
        npc["cells"][9001][21] = "30"
        r = self.S.skill_record(7001, c, "", None, True, npc)
        self.assertEqual((r["page"], r["flags"], r["type"], r["summon_npc"], r["summon_pts"], r["level"]), (0, self.S.FLAG_ITEM, 14, 9001, 30, 1))
        p = self.S.skill_record(7301, self._row(c2=1, c4=1, c5=14, c51=11, c28=8601), "Hire", None, False, npc)
        self.assertEqual((p["page"], p["flags"], p["summon_npc"], p["summon_pts"]), (1, 0, 8601, 0))
        b = self.S.skill_record(7401, self._row(c2=1, c4=1, c5=8, c51=12, c21=38, c22=210), "", None, True)
        self.assertEqual((b["flags"], b["summon_npc"]), (0, 0))                  # a scroll with a page is a normal row (no item flag)

    def test_round_trip(self):
        npc = dict(rows=10000, cells=[[""] * 30 for _ in range(10000)])
        npc["cells"][9002][21] = "33"
        recs = [self.S.skill_record(7002, self._row(c2=1, c5=14, c28=9002), "", None, True, npc),
                self.S.skill_record(7101, self._row(c2=1, c4=1, c5=18, c51=13, c21=1, c22=333, c23=222), "Return", None, True)]
        sk, _st = self.S.dec_skdb(self.S.enc_skdb(recs))
        self.assertEqual(len(self.S.enc_skdb(recs)) - 48 >= 2 * self.S.REC_BYTES, True)
        self.assertEqual((sk[0]["id"], sk[0]["summon_npc"], sk[0]["summon_pts"], sk[0]["flags"]), (7002, 9002, 33, self.S.FLAG_ITEM))
        self.assertEqual((sk[1]["type"], sk[1]["add_type"][0], sk[1]["add_value"][0], sk[1]["add_rate"][0], sk[1]["summon_npc"]), (18, 1, 333, 222, 0))
        self.assertEqual(self.S.REC.size, 136)

    def test_item_skill_ids(self):
        ui = dict(rows=4, cells=[[""] * 30 for _ in range(4)])
        ui["cells"][1][4], ui["cells"][1][20] = "313", "7001"
        ui["cells"][2][4], ui["cells"][2][20] = "314", "201"                     # a skill book: learnt, not fired
        ui["cells"][3][4], ui["cells"][3][20] = "313", ""
        self.assertEqual(self.S.item_skill_ids(ui), {7001})
        self.assertEqual(self.S.item_skill_ids(None), set())

    def test_summon_npc_list_and_budget(self):
        sk = dict(rows=5, cells=[self._row() for _ in range(5)])
        sk["cells"][1] = self._row(c5=14, c28=9001)                               # capsule skill (item skill)
        sk["cells"][2] = self._row(c4=1, c5=14, c51=5, c28=8601)                  # player summon skill
        sk["cells"][3] = self._row(c5=14, c28=9999)                               # monster-only row: no page, not an item skill
        sk["cells"][4] = self._row(c4=1, c5=3, c51=5, c28=7777)                   # not a summon
        self.assertEqual(self.S.summon_npc_list(sk, {1}), [8601, 9001])
        skel = {1: 0, 2: 1, 9001: 0, 8601: 7, 8602: 8, 8603: None}
        add, drop = self.S.pick_summon_actors({1, 2}, [9001, 8601, 8602, 8603], skel.get, max_skel=3)
        self.assertEqual((add, drop), ([8601, 9001], [8602, 8603]))                  # 9001 shares skeleton 0; 8601 takes the last skeleton; 8602 over budget; 8603 has no model


class SkillTips(unittest.TestCase):
    """ROSE w15 SKILLTIPS (rose_skilltips.py): description table appended to SKDB (header words 10 / 11), JA via TXJA D_SKILL sub 1"""

    def setUp(self):
        import rose_skills
        import rose_skilltips
        self.S, self.T = rose_skills, rose_skilltips

    def _rec(self, sid):
        c = [""] * 72
        c[2], c[4], c[5], c[51] = "1", "1", "3", "9"
        return self.S.skill_record(sid, c, "Skill %d" % sid)

    def test_clean_desc(self):
        self.assertEqual(self.T.clean_desc("Shoot a poison arrow.<br>Poison too. \x00"), "Shoot a poison arrow.\nPoison too.")
        self.assertEqual(self.T.clean_desc("a<BR/>\n<br />b"), "a\nb")
        self.assertEqual(self.T.clean_desc("   "), "")

    def test_cut_utf8(self):
        b = "a\u3042\u3044".encode("utf-8")
        self.assertEqual(self.T.cut_utf8(b, 5), "a\u3042".encode("utf-8"))
        self.assertEqual(self.T.cut_utf8(b, 3), b"a")
        self.assertEqual(self.T.cut_utf8(b, 99), b)

    def test_round_trip_keeps_jobs_and_dedups(self):
        recs = [self._rec(302), self._rec(301), self._rec(500)]
        d0 = self.S.enc_skdb(recs, (), [(1, "Soldier")])
        d = self.T.append_table(d0, [(302, "Same text."), (301, "Same text."), (500, "Other<br>line"), (9, ""), (0, "x")])
        self.assertEqual(self.T.dec_descs(d), {301: "Same text.", 302: "Same text.", 500: "Other\nline"})
        self.assertEqual(self.S.dec_jobs(d), [(1, "Soldier")])
        self.assertEqual([r["id"] for r in self.S.dec_skdb(d)[0]], [301, 302, 500])
        self.assertEqual(d.count(b"Same text."), 1)
        h = self.S.HDR.unpack_from(d, 0)
        self.assertEqual((h[10], h[11] % 4, len(d) % 4), (3, 0, 0))

    def test_no_table(self):
        d = self.S.enc_skdb([self._rec(301)])
        self.assertEqual(self.T.append_table(d, []), d)
        self.assertEqual(self.T.append_table(d, [(301, "  ")]), d)
        self.assertEqual(self.T.dec_descs(d), {})
        self.assertEqual(self.T.dec_descs(b"short"), {})

    def test_bad_table_reads_as_none(self):
        d = bytearray(self.T.append_table(self.S.enc_skdb([self._rec(301)]), [(301, "Hello")]))
        self.assertEqual(self.T.dec_descs(bytes(d)), {301: "Hello"})
        h = list(self.S.HDR.unpack_from(d, 0))
        h[11] = len(d) + 100
        self.S.HDR.pack_into(d, 0, *h)
        self.assertEqual(self.T.dec_descs(bytes(d)), {})

    def test_build_with_descs_japanese_overlay(self):
        import rose_text as TX
        T, S = self.T, self.S
        row = [""] * 72
        c = [""] * 72
        c[2], c[4], c[5], c[51], c[71] = "1", "1", "3", "9", "K1"
        sk = dict(rows=2, cells=[row, c])
        data = S.enc_skdb([S.skill_record(1, c, "Heavy Attack")])
        orig = (S._stb, T.stl_descs)
        try:
            S._stb = lambda vfs, n: sk
            T.stl_descs = lambda vfs, lang=TX.EN: {"K1": "Bash a target." if lang == TX.EN else "J1"}
            TX.reset()
            secs, _w = T.build_with_descs(lambda v: ([(S.TAG_SKDB, 1, 1, data)], []), None)
            self.assertEqual(T.dec_descs(secs[0][3]), {1: "Bash a target."})
            self.assertEqual(TX.current().d[(TX.D_SKILL, 1, 1)], ("Bash a target.", "J1"))
            self.assertEqual(S.dec_skdb(secs[0][3])[0][0]["name"], "Heavy Attack")
        finally:
            S._stb, T.stl_descs = orig

    def test_installed(self):
        import rose_conv
        self.assertIsNot(self.S.build_section, None)
        self.assertEqual(self.S.build_section.__name__, "build_section")
        self.assertIsNotNone(self.T._orig)


class PrebakedBones(unittest.TestCase):
    """LOAD PACKAGE 5: BONT (rose_conv/rose_bont.py), the bone texture pre-baked like GsvrRpg1Actors.StepBones. The bit-exact comparison with the
    compiled runtime runs in tools/usharp_offline/bont_check.py (check.sh); here: layout, header, lazy marking, guards, the rose_pack hook."""

    @staticmethod
    def _pack(clipmap=(), actor_clips=None, nclips=4):
        import rose_bont
        joints = [dict(parent=-1, flags=0, name="root", lpos=(0.0, 1.0, 0.0), lrot=(0.0, 0.0, 0.0, 1.0), gpos=(0.0, 1.0, 0.0), grot=(0.0, 0.0, 0.0, 1.0)),
                  dict(parent=0, flags=0, name="arm", lpos=(0.5, 0.0, 0.0), lrot=(0.0, 0.0, 0.0, 1.0), gpos=(0.5, 1.0, 0.0), grot=(0.0, 0.0, 0.0, 1.0))]
        skel = rpg1.enc_skel([dict(name="s", nreal=2, flags=1, joints=joints)])
        clips = [dict(name="still", skeleton=0, frames=3, loop=True, src_fps=30, tracks=[], events=[])]
        for c in range(1, nclips):           # the root rises by 0.25 m in frame 1
            clips.append(dict(name="c%d" % c, skeleton=0, frames=2, loop=False, src_fps=30, events=[],
                              tracks=[dict(joint=0, pos=[(0.0, 1.0, 0.0), (0.0, 1.25, 0.0)], rot=None, scale=None)]))
        canm = rpg1.enc_canm(clips, 30)
        avat = rpg1.enc_avat([], [], list(clipmap)) if clipmap else None
        actr = None
        if actor_clips is not None:
            actr = struct.pack("<6I", 1, 128, 24, 152, 156, 0) + bytes(88) + struct.pack("<12h", *(list(actor_clips) + [-1] * (12 - len(actor_clips)))) + bytes(16) + bytes(8)
        return rose_bont, skel, canm, avat, actr

    def test_identity_clip_and_header(self):
        B, skel, canm, avat, actr = self._pack()
        tag, ver, count, data = B.bont_section(skel, canm)
        self.assertEqual((tag, ver, count), (b"BONT", 1, 4))
        h = B.HEADER.unpack_from(data, 0)
        texels = 2 * 2 + 3 * 4 + 3 * 2 * 4                                      # bind + clip 0 (3 frames) + 3 clips of 2 frames, 2 texels per joint
        self.assertEqual(h[:3], (texels, 4, 1))
        self.assertEqual(h[3:5], (struct.unpack("<i", struct.pack("<I", zlib_crc(skel)))[0], struct.unpack("<i", struct.pack("<I", zlib_crc(canm)))[0]))
        self.assertEqual(h[5:7], (0, 0))                                         # no AVAT / ACTR
        self.assertEqual(h[7:], (texels, 48, 64, 68, 0))
        self.assertEqual(len(data), 68 + texels * 16)
        rise = struct.unpack_from("<4f", data, 48)
        self.assertEqual(rise[0], 0.0)
        self.assertAlmostEqual(rise[1], 0.25, places=5)                          # u16 quantised: 1.25 - 1.0
        self.assertEqual(list(data[64:68]), [1, 1, 1, 1])
        tex = struct.unpack_from("<%df" % (texels * 4), data, 68)
        ident = (0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0)
        for t in range(2 + 3 * 2):                                               # bind frame + the still clip: every skin transform is the identity
            self.assertEqual(tex[t * 8:t * 8 + 8], ident)
        self.assertEqual(B.bont_section(skel, canm), B.bont_section(skel, canm))   # deterministic

    def test_lazy_weapon_sets(self):
        """MarkLazyClips like the runtime: weapon type >= 2 only = lazy, unless an unarmed / sword row or an ACTR actor uses the clip; lazy_too bakes them anyway"""
        B, skel, canm, avat, actr = self._pack(clipmap=[(0, 0, 0, 1), (0, 0, 3, 2), (0, 0, 10, 3), (0, 1, 1, 3)], actor_clips=[0])
        self.assertEqual(B.mark_lazy(avat, None, 4), [False, False, True, False])
        self.assertEqual(B.mark_lazy(avat, actr, 4), [False, False, True, False])
        _B, _s, _c, avat2, actr2 = self._pack(clipmap=[(0, 0, 3, 2), (0, 0, 10, 3)], actor_clips=[3])
        self.assertEqual(B.mark_lazy(avat2, actr2, 4), [False, False, True, False])          # the actor keeps clip 3 eager
        self.assertEqual(B.mark_lazy(None, actr2, 4), [False] * 4)
        full = B.bont_section(skel, canm, avat, None)[3]
        eager = B.bont_section(skel, canm, avat, None, lazy_too=False)[3]
        self.assertEqual(list(full[64:68]), [1, 1, 1, 1])
        self.assertEqual(list(eager[64:68]), [1, 1, 0, 1])
        self.assertEqual(len(full) - len(eager), 2 * 2 * 2 * 16)                # clip 2: 2 frames x 2 joints x 2 texels
        self.assertEqual(struct.unpack_from("<f", eager, 48 + 8)[0], 0.0)        # a clip that is not baked has rise 0

    def test_guards_and_hook(self):
        B, skel, canm, avat, actr = self._pack()
        self.assertIsNone(B.bont_section(None, canm))
        self.assertIsNone(B.bont_section(skel, None))
        self.assertIsNone(B.from_sections([(b"AREA", 1, 1, b"x" * 128)]))
        secs = [(b"SKEL", 1, 1, skel), (b"CANM", 1, 4, canm)]
        self.assertEqual(B.from_sections(secs)[0], b"BONT")
        self.assertIsNone(B.from_sections(secs, max_pack=1000))                  # the pack would pass the limit: no BONT, the runtime bakes
        self.assertLess(B.MAX_PACK_BYTES, rose_conv_rose_max())
        self.assertIn(b"BONT", rose_conv.RP.FULL_ZONE_TAGS)
        src = inspect.getsource(rose_conv.RP.convert)
        self.assertIn("rose_bont.from_sections(sections)", src)                   # every zone / lobby pack with characters gets it
        self.assertLess(src.index("rose_bont.from_sections(sections)"), src.index("pack = P.write_pack(sections)"))


def _s32(s):
    b = s.encode()
    return struct.pack("<I", len(b)) + b


def _fx_ptl(tex="FX/A.DDS", keys=(), loops=1, num=4, coords=1, src=5, dst=2, life=(4.8, 9.6), emit=(10.0, 10.0), rad=(0, 0, 0, 0, 0, 0), grav=(0, 0, 0, 0, 0, 0)):
    """synthetic PTL with one sequence (layout of rose-file-readers ptl.rs); keys = [(type, t0, t1, fade, floats)]"""
    b = struct.pack("<I", 1) + _s32("seq") + struct.pack("<4fi", life[0], life[1], emit[0], emit[1], loops) + struct.pack("<6f", *([0.0] * 6))
    b += struct.pack("<6f", *rad) + struct.pack("<6f", *grav) + _s32(tex)
    b += struct.pack("<iIIIIIIII", num, 0, coords, 2, 2, 0, dst, src, 1) + struct.pack("<I", len(keys))
    for typ, t0, t1, fade, vals in keys:
        b += struct.pack("<I2fB", typ, t0, t1, fade) + struct.pack("<%df" % len(vals), *vals)
    return b


def _fx_eft(ptls=(), meshes=(), rot=(0.0, 90.0, 0.0), panim=None):
    """synthetic EFT (rose-file-readers eft.rs): particle parts [(ptl path, pos, delay ms)], mesh parts [(mesh, anim, tex, src, dst)]; rot = (pitch, yaw, roll) of every part;
    panim = {particle part index: (use flag, transform animation path, repeat)} (w17-fx)"""
    b = _s32("name") + struct.pack("<I", 0) + _s32("") + struct.pack("<I", 0) + struct.pack("<I", len(ptls))
    for pi, (path, pos, delay) in enumerate(ptls):
        use, apath, arep = (panim or {}).get(pi, (0, "NULL", 0))
        b += _s32("n") + _s32("u") + bytes(4) + _s32(path) + struct.pack("<I", use) + _s32(apath) + struct.pack("<I", arep) + bytes(4)
        b += struct.pack("<3f", *pos) + struct.pack("<3f", *rot) + bytes(4) + struct.pack("<II", delay, 0)
    b += struct.pack("<I", len(meshes))
    for mesh, anim, tex, src, dst in meshes:
        b += _s32("n") + _s32("u") + bytes(4) + _s32(mesh) + _s32(anim) + _s32(tex) + struct.pack("<8I", 1, 1, 0, 1, 0, src, dst, 1)
        b += struct.pack("<I", 0) + _s32("NULL") + struct.pack("<I", 0) + bytes(4) + struct.pack("<3f", 0, 0, 0) + struct.pack("<3f", 0, 0, 0) + bytes(4) + struct.pack("<III", 0, 2, 0)
    return b


def _fx_zms(pts, uvs, tris):
    """synthetic ZMS0008 (rosefmt.zms layout): positions (metres, file axes) + uv1 + triangles"""
    b = b"ZMS0008\0" + struct.pack("<I", 2 | 128) + struct.pack("<6f", *([0.0] * 6)) + struct.pack("<H", 0) + struct.pack("<H", len(pts))
    for p_ in pts:
        b += struct.pack("<3f", *p_)
    for u in uvs:
        b += struct.pack("<2f", *u)
    b += struct.pack("<H", len(tris)) + b"".join(struct.pack("<3H", *t) for t in tris)
    return b + struct.pack("<H", 0) + struct.pack("<H", 0) + struct.pack("<H", 0)


def _fx_zmo(fps, frames, chans):
    """synthetic ZMO0002 vertex animation: chans = [(type, vertex, [values per frame])] (2 position xyz cm, 32 uv, 16 alpha)"""
    b = b"ZMO0002\0" + struct.pack("<III", fps, frames, len(chans)) + b"".join(struct.pack("<II", t, i) for t, i, _ in chans)
    for f in range(frames):
        for t, _i, vals in chans:
            v = vals[f]
            b += struct.pack("<%df" % (len(v) if isinstance(v, tuple) else 1), *(v if isinstance(v, tuple) else (v,)))
    return b


def _fx_dds(rgba=(255, 0, 0, 255), w=8, h=8):
    import rosedds
    return rosedds.make_dds(rosedds.FMT_RGBA32, w, h, [bytes(rgba) * (w * h)])


def _fx_stb(rows):
    """rosefmt.stb-like dict from {row: {cell: value}} (row 0 unused like the client tables)"""
    n = max(rows) + 1
    width = max(max(c) for c in rows.values()) + 1
    cells = [[""] * width for _ in range(n)]
    for r, c in rows.items():
        for k, v in c.items():
            cells[r][k] = str(v)
    return dict(rows=n, cells=cells)


class Effects(unittest.TestCase):
    """ROSE w14 effects: EFCT (rose_conv/rose_effects.py) from synthetic EFT / PTL / DDS / STB data (no game data). The runtime rules (keyframes, pool, budget)
    run in tools/usharp_offline/rosefxrules.cs against the compiled GsvrRoseFx."""

    def setUp(self):
        import rose_effects
        self.E = rose_effects
        size = (1, 0.0, 0.0, 0, (10.0, 20.0, 30.0, 40.0))                       # x 10..30 cm, y 20..40 cm (file order: xmin, ymin, xmax, ymax)
        fade_a = (6, 4.8, 4.8, 1, (0.0, 0.0))
        vely = (9, 0.0, 9.6, 0, (100.0, 200.0))                                 # file y velocity (north) 100..200
        self.files = {
            "FX/A.PTL": _fx_ptl(keys=[fade_a, size, vely]),
            "FX/B.PTL": _fx_ptl(tex="FX/MISSING.DDS"),
            "FX/A.DDS": _fx_dds(),
            "FX/C.DDS": _fx_dds((0, 0, 255, 128), 256, 128),
            "FX/CAST.EFT": _fx_eft([("FX/A.PTL", (100.0, 200.0, 300.0), 500)]),
            "FX/HIT.EFT": _fx_eft([("FX/A.PTL", (0, 0, 0), 0), ("FX/B.PTL", (0, 0, 0), 0)]),
            "FX/RING.EFT": _fx_eft([], [("FX/RING.ZMS", "FX/RING.ZMO", "FX/C.DDS", 5, 6)]),
            "FX/RING.ZMS": _fx_zms([(1.0, 2.0, 3.0), (0.0, 0.0, 0.0), (0.0, 1.0, 0.0)], [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0)], [(0, 1, 2)]),
            "FX/RING.ZMO": _fx_zmo(10, 2, [(2, 0, [(100.0, 200.0, 300.0), (200.0, 400.0, 600.0)]), (16, 1, [0.5, 0.25])]),
            "FX/NOMESH.EFT": _fx_eft([], [("FX/NOPE.ZMS", "", "FX/C.DDS", 5, 6)]),
            "3DDATA/EFFECT/LEVELUP_01.EFT": _fx_eft([("FX/A.PTL", (0, 0, 0), 0)]),
            "FX/BAD.EFT": b"\x01\x00",
        }
        self.read = self.files.__getitem__
        self.fe = _fx_stb({1: {1: "FX\\CAST.EFT"}, 2: {1: "FX\\HIT.EFT"}, 3: {1: "FX\\RING.EFT"}, 4: {1: "FX\\BAD.EFT"}, 5: {1: "FX\\GONE.EFT"}, 6: {1: "FX\\NOMESH.EFT"}})
        self.le = _fx_stb({1: {9: 2, 11: 3, 15: 1500}, 2: {9: 4}})
        self.sk = _fx_stb({10: {4: 1, 5: 3, 51: 7, 56: 1, 57: 3, 71: 1, 72: 1, 75: 999, 11: 2},   # cast 1 on dummy 3, bullet effect 1 (from dummy 1) -> bullet file 3 + hit 2; status 2
                           11: {4: 1, 5: 3, 51: 7, 74: 2, 77: 3},                    # own hit file 2, hit dummy 3
                           12: {4: 1, 5: 3, 51: 7, 56: 4, 59: 5, 71: 2},             # only broken / missing files -> no record
                           13: {5: 3, 56: 1}})                                       # monster skill (no page / icon): still in the table
        self.npc = _fx_stb({7: {33: 1, 34: 1}, 8: {33: 2}})
        self.wpn = _fx_stb({3: {38: 1, 39: 1}})
        self.st = _fx_stb({2: {10: 2, 15: 1}, 3: {10: 0, 5: 2}, 4: {10: 6}})        # status 2: step 2 / end 1; status 3 applies status 2; status 4 only a mesh-less effect
        self.ui = _fx_stb({7: {22: 1, 24: 3}, 8: {22: 6}})                          # item 7: use effect 1, potion status 3 (-> the step effect of status 2)

    def test_parse_roundtrip(self):
        e = self.E.parse_eft(self.files["FX/CAST.EFT"])
        self.assertEqual(len(e["particles"]), 1)
        self.assertEqual(e["particles"][0]["ptl"], "FX/A.PTL")
        self.assertEqual(e["particles"][0]["delay"], 500)
        self.assertEqual(e["particles"][0]["yaw"], 90.0)
        m = self.E.parse_eft(self.files["FX/RING.EFT"])["meshes"]
        self.assertEqual((m[0]["tex"], m[0]["src"], m[0]["dst"], m[0]["repeat"], m[0]["anim"]), ("FX/C.DDS", 5, 6, 2, "FX/RING.ZMO"))
        p = self.E.parse_ptl(self.files["FX/A.PTL"])
        self.assertEqual((p[0]["num"], p[0]["coords"], p[0]["cols"], p[0]["dst"], len(p[0]["keys"])), (4, 1, 2, 2, 3))
        with self.assertRaises((ValueError, struct.error)):
            self.E.parse_eft(self.files["FX/BAD.EFT"])
        with self.assertRaises(ValueError):
            self.E.parse_ptl(_fx_ptl(keys=[(14, 0, 0, 0, ())]))

    def test_key_conversion_axes_units(self):
        E = self.E
        t, f, tm, v = E.convert_key(dict(type=1, t=(0.0, 0.0), fade=0, v=(10.0, 20.0, 30.0, 40.0)))
        self.assertEqual((t, f, tm), (1, 0, 0.0))
        self.assertEqual([round(x, 6) for x in v[:4]], [0.1, 0.3, 0.2, 0.4])            # (xlo, xhi, ylo, yhi) metres
        t, f, tm, v = E.convert_key(dict(type=9, t=(0.0, 9.6), fade=0, v=(100.0, 200.0)))
        self.assertEqual(t, 10)                                                       # file y -> Unity z (front), negated
        self.assertAlmostEqual(tm, 1.0)                                               # middle of 0..9.6 particle units / 4.8
        self.assertAlmostEqual(v[0], -9.6); self.assertAlmostEqual(v[1], -4.8)        # 100..200 cm x 4.8 / 100, negated
        t, _f, _t, v = E.convert_key(dict(type=10, t=(0, 0), fade=1, v=(100.0, 100.0)))
        self.assertEqual(t, 9); self.assertAlmostEqual(v[0], 4.8)                    # file z (up) -> Unity y
        t, _f, _t, v = E.convert_key(dict(type=11, t=(0, 0), fade=0, v=(1.0, 2.0, 3.0, 1.0, 2.0, 3.0)))
        self.assertEqual(t, 11)
        self.assertEqual([round(x / 0.048, 4) for x in v[:6]], [-1.0, -1.0, 3.0, 3.0, -2.0, -2.0])
        self.assertEqual(E.rng(5.0, 2.0), (5.0, 8.0))                                 # the original's |max - min| + min for an inverted range
        t, _f, _t, v = E.convert_key(dict(type=2, t=(0, 0), fade=0, v=(4.8, 9.6)))
        self.assertAlmostEqual(v[0], 1.0); self.assertAlmostEqual(v[1], 2.0)

    def test_order_keys_next_fade(self):
        ks = self.E.order_keys([(6, 1, 2.0, [0] * 9), (1, 0, 0.0, [0] * 9), (6, 0, 0.0, [0] * 9), (6, 1, 1.0, [0] * 9), (3, 1, 0.5, [0] * 9)])
        self.assertEqual([k[2] for k in ks], [0.0, 0.0, 0.5, 1.0, 2.0])
        self.assertEqual([(k[0], k[4]) for k in ks], [(1, -1), (6, 3), (3, -1), (6, 4), (6, -1)])

    def test_blend_and_duration(self):
        E = self.E
        self.assertEqual(E.blend_mode(5, 2), E.MODE_ADD)
        self.assertEqual(E.blend_mode(5, 6), E.MODE_ALPHA)
        self.assertEqual(E.emitter_duration(2.0, 10.0, 1, 4, 0.5), 0.5 + 0.4 + 2.0)
        self.assertEqual(E.emitter_duration(2.0, 10.0, 0, 4, 0.0), -1.0)              # endless
        self.assertEqual(E.emitter_duration(2.0, 10.0, 3, 0, 0.25), 0.25)             # no particles: finite (ends at its delay), never endless
        self.assertEqual(E.emitter_duration(2.0, 10.0, 0, 0, 0.0), 0.0)
        self.assertEqual((E._int(["12"], 0), E._int(["x"], 0), E._int([], 3), E._int([None], 0), E._int(["9" * 400], 0) > 0), (12, 0, 0, 0, True))
        self.assertEqual((E.fin(float("nan")), E.fin(float("inf")), E.fin(-1e30), E.fin("x"), E.fin(2.5)), (0.0, 0.0, -E.F_LIMIT, 0.0, 2.5))
        self.assertEqual(E.pos_unity((100.0, 200.0, 300.0)), (-1.0, 3.0, -2.0))

    def test_tables_and_section(self):
        E = self.E
        skills, npcs, weapons, b = E.build_tables(self.read, self.fe, self.le, self.sk, self.npc, self.wpn)
        by = {s["id"]: s for s in skills}
        self.assertEqual(sorted(by), [10, 11, 13])
        self.assertEqual((by[10]["cast"], by[10]["bullet"], by[10]["hit"], by[10]["speed"]), ([1, 0, 0, 0], 3, 2, 1500))
        self.assertEqual((by[10]["cast_dummy"][0], by[10]["bullet_dummy"], by[10]["hit_bone"]), (3, 1, E.NO_DUMMY))      # 999 = the actor root
        self.assertEqual((by[11]["hit"], by[11]["hit_dummy"], by[11]["bullet"]), (2, [3, 0], 0))
        self.assertEqual([(n["id"], n["hit"], n["die"]) for n in npcs], [(7, 2, 1)])   # npc 8: LIST_EFFECT 2 hits with the broken file 4 -> nothing
        self.assertEqual([(w["id"], w["hit"], w["bullet"], w["speed"]) for w in weapons], [(3, 2, 3, 1500)])
        self.assertEqual(sorted(f for f, r in b.files.items() if r), [1, 2, 3, E.LEVELUP_ID])          # + the level-up effect (fx11)
        self.assertIsNone(b.files.get(6))                                             # a mesh part without its ZMS is not drawable
        self.assertEqual(b.files[2]["count"], 1)                                      # FX/B.PTL has no texture: skipped
        self.assertEqual(b.warn.get("particle texture missing"), 1)
        ring = b.emitters[b.files[3]["first"]]
        self.assertEqual((ring["kind"], ring["mode"], ring["mrep"], ring["cell"], ring["mesh"]), (1, E.MODE_ALPHA, 2.0, 1, 0))
        self.assertAlmostEqual(ring["mdur"], 0.2)                                   # 2 frames at 10 fps
        self.assertAlmostEqual(b.files[3]["dur"], 0.4)                              # x 2 repeats
        cast = b.emitters[b.files[1]["first"]]
        self.assertEqual(cast["pos"], (-1.0, 3.0, -2.0))
        self.assertAlmostEqual(cast["delay"], 0.5)
        self.assertEqual(([round(x, 5) for x in cast["life"]], cast["num"], cast["mode"], cast["cell"]), ([1.0, 2.0], 4, E.MODE_ADD, 0))
        self.assertAlmostEqual(b.files[1]["dur"], 0.5 + 0.4 + 2.0, places=5)
        d = E.enc_efct(b, skills, npcs, weapons)
        x = E.dec_efct(d)
        self.assertEqual([f[0] for f in x["files"]], [1, 2, 3, E.LEVELUP_ID])
        self.assertEqual([s[0] for s in x["skills"]], [10, 11, 13])
        self.assertEqual(x["skills"][0][:16], (10, 1, 0, 0, 0, 3, E.NO_DUMMY, E.NO_DUMMY, E.NO_DUMMY, 3, 1, 2, E.NO_DUMMY, 0, 0, 1500))
        self.assertEqual(len(x["keys"]), 3)
        self.assertEqual(x["keys"][0][0], 1)                                         # sorted by time: size (0) before the alpha fade (1 s) and velocity (1 s)
        fmt, w, h, lv, nb, _z0, _z1, cells = x["atlas"]
        self.assertEqual((fmt, w, h, lv, cells), (2, 256, 160, 4, 2))                # w17-fx: 256 x 128 at full size + the 8 x 8 scaled up to 32 x 32 below it
        self.assertEqual(x["cells"], [(0, 128, 32, 32), (0, 0, 256, 128)])
        self.assertEqual(nb, sum(max(1, (w >> k) // 4) * max(1, (h >> k) // 4) * 16 for k in range(4)))
        self.assertEqual(x["skills"][0][20:24], (0, E.CAST_NEVER, E.CAST_NEVER, E.CAST_NEVER))   # no motion tables: the cast starts with the cast
        h0 = E.HDR.unpack_from(d, 0)
        self.assertEqual(h0[17] + h0[18], len(d))
        self.assertEqual(h0[17] % 4, 0)
        with self.assertRaises(ValueError):
            E.dec_efct(d[:100])

    def test_atlas_cells(self):
        import rosedds
        E = self.E
        a = E.Atlas(self.read)
        self.assertEqual(a.add("FX/A.DDS"), 0)
        self.assertEqual(a.add("fx/a.dds"), 0)                                       # case-insensitive VFS paths, one cell each
        self.assertEqual(a.add("FX/C.DDS"), 1)
        self.assertIsNone(a.add("FX/NOPE.DDS"))
        w, h, levels, rects = a.encode()
        self.assertEqual((w, h, len(levels)), (256, 160, 4))
        self.assertEqual(rects, [(0, 128, 32, 32), (0, 0, 256, 128)])               # w17-fx: the 256 x 128 keeps its size (was a 128 px cell)
        px = rosedds.decode_dxt("DXT5", levels[0], w, h)
        o = (130 * w + 2) * 4
        self.assertEqual(tuple(px[o:o + 4]), (255, 0, 0, 255))                        # cell 0 red
        o = (64 * w + 200) * 4
        self.assertEqual(tuple(px[o:o + 3]), (0, 0, 255))                            # cell 1 blue
        self.assertLess(abs(px[o + 3] - 128), 3)
        small = E.Atlas(self.read, max_cells=1)
        small.add("FX/A.DDS")
        self.assertIsNone(small.add("FX/C.DDS"))                                    # full

    def test_w17_atlas_full_resolution_blocks_copied(self):
        """w17-fx: a DXT5 texture's blocks land in the atlas unchanged (every level it has; missing levels box-filtered), a DXT1 4-colour block keeps its colour block with
        an opaque alpha block, a DXT1 3-colour block with transparency keeps the transparent pixels; > TEX_MAX = its first mip level that fits; packing stays block aligned"""
        import rosedds
        E = self.E
        lv0 = bytes(range(16)) * 64                                                     # 32 x 32 DXT5 = 64 blocks
        lv1 = bytes(range(16, 32)) * 16
        dds5 = rosedds.make_dds(rosedds.FMT_DXT5, 32, 32, [lv0, lv1])
        t = E.cell_tex(rosedds.parse_dds(dds5))
        self.assertEqual((t["w"], t["h"], len(t["levels"])), (32, 32, E.ATLAS_LEVELS))
        self.assertEqual(t["levels"][0], lv0)
        self.assertEqual(t["levels"][1], lv1)
        self.assertEqual(len(t["levels"][3]), 16)                                       # 4 x 4 px: one block
        four = struct.pack("<HHI", 0xF800, 0x001F, 0x55555555)                         # c0 > c1: 4-colour block
        three = struct.pack("<HHI", 0x001F, 0xF800, 0xFFFFFFFF)                        # c0 <= c1, every index 3: transparent black
        self.assertEqual(E._dxt1_block_to_dxt5(four), b"\xff\xff" + bytes(6) + four)
        px = rosedds.decode_dxt("DXT5", E._dxt1_block_to_dxt5(three), 4, 4)
        self.assertEqual(px[3::4], bytes(16))                                           # alpha 0 kept
        big = rosedds.make_dds(rosedds.FMT_DXT1, 1024, 64, [four * (256 * 16), four * (128 * 8), four * (64 * 4), four * (32 * 2), four * (16 * 1)])
        t = E.cell_tex(rosedds.parse_dds(big))
        self.assertEqual((t["w"], t["h"]), (512, 32))                                   # 1024 > TEX_MAX: mip 1
        self.assertEqual(t["levels"][0][8:16], four)
        w, h, pos = E.pack_cells([(64, 64), (32, 32), (32, 64), (512, 512)], 2048)
        self.assertEqual(w % 4, 0); self.assertEqual(h % 32, 0)
        for (x, y), (sw, sh) in zip(pos, [(64, 64), (32, 32), (32, 64), (512, 512)]):
            self.assertEqual((x % 32, y % 32), (0, 0))
            self.assertLessEqual(x + sw, w); self.assertLessEqual(y + sh, h)
        rects = [(x, y, sw, sh) for (x, y), (sw, sh) in zip(pos, [(64, 64), (32, 32), (32, 64), (512, 512)])]
        for i, r in enumerate(rects):                                                   # no overlaps
            for q in rects[i + 1:]:
                self.assertTrue(r[0] + r[2] <= q[0] or q[0] + q[2] <= r[0] or r[1] + r[3] <= q[1] or q[1] + q[3] <= r[1])
        self.files["FX/BIG.DDS"] = rosedds.make_dds(rosedds.FMT_DXT5, 256, 256, [bytes(16) * 4096])
        a = E.Atlas(self.read, width=256, max_h=128)
        a.add("FX/BIG.DDS"); a.add("FX/A.DDS")
        w, h, levels, rects = a.encode()
        self.assertLessEqual(h, 128)
        self.assertEqual(a.halved, 1)                                                   # did not fit: the largest texture one level down
        self.assertEqual(rects[0][2:], (128, 128))

    def test_broken_floats_never_reach_the_section(self):
        """a PTL with NaN / inf values: the section carries only finite floats (the runtime never sees NaN)"""
        E = self.E
        self.files["FX/A.PTL"] = _fx_ptl(life=(float("nan"), float("inf")), emit=(float("inf"), 1.0), keys=[(6, float("nan"), 0.0, 0, (float("nan"), 1.0))])
        skills, npcs, weapons, b = E.build_tables(self.read, self.fe, self.le, self.sk)
        x = E.dec_efct(E.enc_efct(b, skills))
        import math
        floats = [v for e in x["emitters"] for v in e[10:35]] + [v for k in x["keys"] for v in k[5:]] + [f[4] for f in x["files"]]
        self.assertTrue(floats and all(math.isfinite(v) and abs(v) <= E.F_LIMIT for v in floats))

    def test_bad_table_fails_missing_table_skips(self):
        """error policy (like QGLB): no tables = no section; a table that is there but broken raises (the avatar build fails); one bad EFT is only skipped"""
        E = self.E

        class V:
            def __init__(s, files):
                s.files = files

            def read(s, p):
                return s.files[p]
        self.assertEqual(E.build_section(V({}), log=lambda *_: None)[0], [])
        with self.assertRaises(Exception):
            E.build_section(V({"3DDATA/STB/FILE_EFFECT.STB": b"junk", "3DDATA/STB/LIST_SKILL.STB": b"junk"}), log=lambda *_: None)
        b = E.Builder(self.read)
        self.assertFalse(b.add_file(4, "FX/BAD.EFT"))
        self.assertEqual(b.warn.get("EFT missing or unreadable"), 1)

    def test_avatar_pack_hook(self):
        src = inspect.getsource(rose_conv.RA.build)
        self.assertIn("rose_effects.build_section(vfs, log=log)", src)
        self.assertNotIn("log('effects: %s' % e)", src)                                # not swallowed: an import / table error fails the avatar build
        self.assertLess(src.index("rose_effects.build_section"), src.index("info = dict("))
        self.assertEqual(rose_conv.pack_version("avatar"), rose_conv.VERSION + "-p4")

    def test_fx11_meshes_vertex_animation(self):
        """fx11: an EFT mesh part becomes real geometry (effect frame (-x, z, -y), metres) + its ZMO vertex animation as RGBAHalf texels (frame-major: frame f vertex v at
        base + f x vertices + v, x y z alpha; cm -> m; vertices without a channel keep their rest position / alpha 1)"""
        E = self.E
        skills, npcs, weapons, b = E.build_tables(self.read, self.fe, self.le, self.sk)
        self.assertEqual(len(b.meshes), 1)
        m = b.meshes[0]
        self.assertEqual((m["vcount"], m["icount"], m["flags"], m["frames"], m["fps"], m["vpos"], m["vuv"]), (3, 3, 1 | 4, 2, 10, 0, 0xFFFFFFFF))
        self.assertEqual(b.verts[0][:3], (-1.0, 3.0, -2.0))
        self.assertEqual(b.verts[2][3:], (0.0, 1.0))
        self.assertEqual(b.vat_texels, 6)
        tex = [struct.unpack_from("<4e", b.vat, 8 * i) for i in range(6)]
        self.assertEqual(tex[0], (-1.0, 3.0, -2.0, 1.0))                             # frame 0 vertex 0: 100 / 200 / 300 cm
        self.assertEqual(tex[3], (-2.0, 6.0, -4.0, 1.0))                             # frame 1 vertex 0
        self.assertEqual(tex[1], (0.0, 0.0, 0.0, 0.5))                               # vertex 1: rest position, alpha track
        self.assertEqual(tex[4][3], 0.25)
        self.assertEqual(tex[5][:3], (0.0, 0.0, -1.0))                               # vertex 2 rest (0, 1, 0) -> (-0, 0, -1)
        d = E.enc_efct(b, skills)
        x = E.dec_efct(d)
        self.assertEqual(x["vat"][:2], (E.VAT_W, 1))
        self.assertEqual(x["vat"][3], E.VAT_W * 8)
        self.assertEqual(list(x["idx"]), [0, 2, 1])                                  # w17-fx: winding reversed (the axis swap is a reflection)
        self.assertEqual(x["meshes"][0][:2], (0, 3))
        h = E.HDR.unpack_from(d, 0)
        self.assertEqual(h[27] % 4, 0); self.assertEqual(h[30] % 4, 0)
        bad = bytearray(d)
        struct.pack_into("<H", bad, h[27], 7)                                        # an index past the mesh
        with self.assertRaises(ValueError):
            E.dec_efct(bytes(bad))
        self.assertIsNone(E.Builder(self.read).mesh("FX/NOPE.ZMS", "")[0])
        self.files["FX/BADANIM.ZMO"] = b"ZMO0002\0" + struct.pack("<III", 10, 0, 0)
        mi, dur = E.Builder(self.read).mesh("FX/RING.ZMS", "FX/BADANIM.ZMO")
        self.assertEqual((mi, dur), (0, 0.0))                                        # a broken animation: the static mesh

    def test_fx11_part_rotation_and_parse(self):
        E = self.E
        self.assertEqual(E.part_quat(0, 0, 0), (0.0, 0.0, 0.0, 1.0))
        q = E.part_quat(0, 90.0, 0)
        self.assertAlmostEqual(q[1], -math.sqrt(0.5), places=6); self.assertAlmostEqual(q[3], math.sqrt(0.5), places=6)      # the original's yaw is mirrored (frame mirrored in x)
        q = E.part_quat(90.0, 0, 0)
        self.assertAlmostEqual(q[0], math.sqrt(0.5), places=6)                                                             # pitch about x unchanged
        q = E.part_quat(0, 0, 90.0)
        self.assertAlmostEqual(q[2], -math.sqrt(0.5), places=6)                                                            # roll mirrored
        z = E.parse_zmo_vertex(self.files["FX/RING.ZMO"])
        self.assertEqual((z["fps"], z["nf"], sorted(z["pos"]), sorted(z["alpha"])), (10, 2, [0], [1]))
        with self.assertRaises(ValueError):
            E.parse_zmo_vertex(b"ZMO0002\0" + struct.pack("<III", 10, 2, 1) + struct.pack("<II", 7, 0))
        with self.assertRaises(ValueError):
            E.parse_zmo_vertex(b"ZMO0002\0" + struct.pack("<III", 10, 50, 1) + struct.pack("<II", 2, 0))           # data past the end
        e = E.parse_eft(self.files["FX/RING.EFT"])["meshes"][0]
        self.assertEqual((e["two"], e["atest"], e["ztest"], e["zwrite"], e["alpha"]), (1, 0, 1, 0, 1))
        self.assertEqual(E.blend_mode(2, 1), E.MODE_OPAQUE)

    def test_fx11_status_item_levelup_tables(self):
        E = self.E
        skills, npcs, weapons, b = E.build_tables(self.read, self.fe, self.le, self.sk, status_stb=self.st, useitem_stb=self.ui)
        self.assertEqual([(x["id"], x["step"], x["end"]) for x in b.statuses], [(2, 2, 1)])           # status 3 / 4 have no drawable effect of their own
        self.assertEqual([(x["id"], x["use"], x["status"]) for x in b.items], [(7, 1, 2)])           # potion status 3 -> the step effect of the status it applies (2)
        by = {s["id"]: s for s in skills}
        self.assertEqual(by[10]["status"], [2, 0])
        self.assertEqual(b.levelup, E.LEVELUP_ID)                                                    # LEVELUP_01.EFT is not in FILE_EFFECT: the reserved file id
        x = E.dec_efct(E.enc_efct(b, skills))
        self.assertEqual((x["statuses"], x["items"], x["levelup"]), ([(2, 2, 1, 0)], [(7, 1, 2, 0)], E.LEVELUP_ID))
        self.assertIn(E.LEVELUP_ID, [f[0] for f in x["files"]])
        fe2 = _fx_stb({1: {1: "FX\\CAST.EFT"}, 9: {1: "3DDATA\\EFFECT\\LEVELUP_01.EFT"}})
        _s, _n, _w, b2 = E.build_tables(self.read, fe2, self.le, self.sk)
        self.assertEqual(b2.levelup, 9)                                                              # its FILE_EFFECT row when there is one

    def test_fx11_emitter_cap_64(self):
        E = self.E
        self.files["FX/MANY.PTL"] = _fx_ptl()
        self.files["FX/MANY.EFT"] = _fx_eft([("FX/MANY.PTL", (0, 0, 0), 0)] * 70)
        b = E.Builder(self.read)
        self.assertTrue(b.add_file(1, "FX/MANY.EFT"))
        self.assertEqual(b.files[1]["count"], 64)                                   # the retail maximum (w14 kept 16)
        self.assertGreaterEqual(b.warn.get("emitters above the per-effect budget", 0), 1)

    def test_w17_blend_modes_and_mesh_flags(self):
        """w17-fx: the original's blend factors per part (srcCode x 3 + dstCode), the blend op (reverse subtract / max: their own materials), alpha disabled, lighting
        for a mesh with normals (rose-offline effect_mesh_material: zone map ambient), z test / z write / two sided kept per part"""
        E = self.E
        self.assertEqual(E.blend_mode(5, 2), E.SRC_ALPHA * 3 + E.DST_ONE)            # additive
        self.assertEqual(E.blend_mode(3, 2), E.SRC_COLOR * 3 + E.DST_ONE)            # src colour, one (28 retail particle parts)
        self.assertEqual(E.blend_mode(2, 2), E.SRC_ONE * 3 + E.DST_ONE)
        self.assertEqual(E.blend_mode(3, 6), E.SRC_COLOR * 3 + E.DST_INVALPHA)
        self.assertEqual(E.blend_mode(2, 1), E.MODE_OPAQUE)
        self.assertEqual(E.blend_mode(5, 4), E.MODE_ADD)                              # inv src colour ("screen") drawn additive (approximation)
        self.assertEqual((E.blend_op_flags(1), E.blend_op_flags(3), E.blend_op_flags(5), E.blend_op_flags(2)), (0, E.MF_REVSUB, E.MF_MAX, 0))
        nz = _fx_zms([(1.0, 2.0, 3.0), (0.0, 0.0, 0.0), (0.0, 1.0, 0.0)], [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0)], [(0, 1, 2)])
        nz = nz[:8] + struct.pack("<I", 2 | 4 | 128) + nz[12:]                         # + normals: 3 vec3 after the positions
        cut = 8 + 4 + 24 + 4 + 36
        self.files["FX/LIT.ZMS"] = nz[:cut] + struct.pack("<9f", *([0.0, 0.0, 1.0] * 3)) + nz[cut:]
        eft = bytearray(_fx_eft([], [("FX/LIT.ZMS", "", "FX/C.DDS", 3, 2)]))
        i = eft.index(struct.pack("<8I", 1, 1, 0, 1, 0, 3, 2, 1))
        eft[i:i + 32] = struct.pack("<8I", 0, 0, 1, 0, 1, 3, 2, 3)                     # alpha off, one sided, alpha test, no z test, z write, reverse subtract
        self.files["FX/LIT.EFT"] = bytes(eft)
        b = E.Builder(self.read)
        self.assertTrue(b.add_file(1, "FX/LIT.EFT"))
        e = b.emitters[0]
        self.assertEqual(b.meshes[0]["flags"] & E.MESH_NORMALS, E.MESH_NORMALS)
        self.assertEqual(e["mode"], E.SRC_COLOR * 3 + E.DST_ONE)
        self.assertEqual(e["mflags"], E.MF_ZWRITE | E.MF_ATEST | E.MF_LIT | E.MF_NOALPHA | E.MF_REVSUB)
        self.files["FX/OP.PTL"] = _fx_ptl(src=5, dst=2)
        seq = E.parse_ptl(self.files["FX/OP.PTL"])
        self.assertEqual(seq[0]["op"], 1)

    def test_w17_transform_animation_tracks(self):
        """w17-fx: an EFT part's transform animation (ZMO bone 0: position cm, rotation w x y z, scale) -> 8 floats per frame in the effect-part frame + 2 RGBAHalf texels;
        an emitter with a limited repeat ends with its animation (rose-offline effect_system), an endless one stays endless"""
        E = self.E
        q = (math.cos(math.radians(45)), 0.0, 0.0, math.sin(math.radians(45)))       # w x y z: 90 degrees about file z (up)
        zmo = b"ZMO0002\0" + struct.pack("<III", 10, 2, 3) + struct.pack("<6I", 2, 0, 4, 0, 1024, 0)
        zmo += struct.pack("<3f4ff", 100.0, 200.0, 300.0, *q, 2.0) + struct.pack("<3f4ff", 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
        z = E.parse_zmo_transform(zmo)
        self.assertEqual((z["fps"], z["nf"], len(z["pos"]), z["rot"], z["scale"]), (10, 2, 2, None, None))   # 3 channels all on bone 0: by channel index (rose-offline)
        zmo2 = b"ZMO0002\0" + struct.pack("<III", 10, 2, 2) + struct.pack("<4I", 2, 0, 4, 0)
        zmo2 += struct.pack("<3f4f", 100.0, 200.0, 300.0, *q) + struct.pack("<3f4f", 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)
        z = E.parse_zmo_transform(zmo2)
        fr = E.track_frames(z)
        self.assertEqual(fr[0][:4], [-1.0, 3.0, -2.0, 1.0])                           # (-x, z, -y) / 100, no scale channel = 1
        self.assertAlmostEqual(fr[0][5], -math.sqrt(0.5), places=5)                    # about file z (up) = about Unity y, mirrored: (x, -z, y, w)
        self.assertAlmostEqual(fr[0][7], math.sqrt(0.5), places=5)
        self.assertEqual(fr[1][4:], [0.0, 0.0, 0.0, 1.0])
        with self.assertRaises(ValueError):
            E.parse_zmo_transform(b"ZMO0002\0" + struct.pack("<III", 10, 5, 1) + struct.pack("<II", 2, 0))
        self.files["FX/T.ZMO"] = zmo2
        self.files["FX/E.PTL"] = _fx_ptl(loops=0)
        eft = _fx_eft([("FX/A.PTL", (0, 0, 0), 0), ("FX/E.PTL", (0, 0, 0), 0)], panim={0: (0xCDCDCD01, "FX\\T.ZMO", 3), 1: (0xCDCDCD01, "FX\\T.ZMO", 0)})
        self.files["FX/T.EFT"] = eft
        p = E.parse_eft(self.files["FX/T.EFT"])["particles"]
        self.assertEqual((p[0]["tanim"], p[0]["trep"], p[1]["trep"]), ("FX/T.ZMO", 3, 0))
        self.assertEqual(E._tanim(0, "FX/T.ZMO"), "")
        self.assertEqual(E._tanim(0xCDCDCD00, "NULL"), "")
        b = E.Builder(self.read)
        self.assertTrue(b.add_file(1, "FX/T.EFT"))
        self.assertEqual(len(b.tracks), 1)                                             # one track for both parts (cached)
        self.assertEqual(b.tracks[0]["flags"], 1 | 2)
        self.assertEqual((b.emitters[0]["track"], b.emitters[0]["trep"], b.emitters[1]["trep"]), (0, 3, 0))
        self.assertEqual(b.files[1]["flags"] & 1, 1)                                   # the endless part (no limited animation) keeps the effect endless
        self.assertEqual(len(b.track_data), 16)
        self.assertEqual(struct.unpack_from("<4e", b.vat, 0), (-1.0, 3.0, -2.0, 1.0))
        x = E.dec_efct(E.enc_efct(b, []))
        self.assertEqual(x["tracks"][0][:3], (0, 2, 10))
        self.assertEqual(x["emitters"][0][37:39], (0, 3))
        self.assertEqual(x["emitters"][1][37:39], (0, 0))
        b2 = E.Builder(self.read)
        self.files["FX/T1.EFT"] = _fx_eft([("FX/E.PTL", (0, 0, 0), 0)], panim={0: (1, "FX\\T.ZMO", 3)})
        self.assertTrue(b2.add_file(2, "FX/T1.EFT"))
        self.assertEqual(b2.files[2]["flags"] & 1, 0)
        self.assertAlmostEqual(b2.files[2]["dur"], 0.2 * 3)                            # the limited animation (2 frames at 10 fps, 3 passes) ends the part

    def test_w17_cast_timing_motion_events(self):
        """w17-fx: casting effect k starts at the EFFECT_SKILL_CASTING_k frame event (44 / 64 / 74 / 84) of the skill's casting motion (ms after the cast start, at its speed),
        its repeat motion (after one casting pass) or its action motion (CAST_FIRE | ms after the fire); no such event = never; no motions = with the cast"""
        E = self.E

        def zmo(fps, nf, events):
            ev = [0] * nf
            for f, e in events:
                ev[f] = e
            body = b"ZMO0002\0" + struct.pack("<III", fps, nf, 0)
            off = len(body)
            return body + struct.pack("<H", nf) + struct.pack("<%dH" % nf, *ev) + struct.pack("<I", off) + b"EZMO"
        self.files["M/CAST.ZMO"] = zmo(30, 31, [(15, 44)])
        self.files["M/REP.ZMO"] = zmo(30, 10, [(3, 64)])
        self.files["M/ACT.ZMO"] = zmo(30, 20, [(6, 74)])
        tm = _fx_stb({5: {0: 1, 1: 1}, 6: {0: 2}, 7: {0: 3}})
        fm = _fx_stb({1: {0: "M\\CAST.ZMO"}, 2: {0: "M\\REP.ZMO"}, 3: {0: "M\\ACT.ZMO"}})
        cells = _fx_stb({1: {52: 5, 53: 50, 54: 6, 68: 7, 69: 200}})["cells"][1]
        at = E.cast_timing(cells, self.read, tm, fm, {})
        self.assertEqual(at[0], 1000)                                                  # frame 15 at 30 fps, speed 50 %
        self.assertEqual(at[1], round((31 / 30.0 / 0.5 + 3 / 30.0 / 0.5) * 1000))       # after one casting pass
        self.assertEqual(at[2], E.CAST_FIRE | 100)                                     # action motion frame 6 at 30 fps, 200 %
        self.assertEqual(at[3], E.CAST_NEVER)
        self.assertEqual(E.cast_timing(cells, self.read, None, None, {}), [0, 0, 0, 0])
        self.assertEqual(E.cast_timing(_fx_stb({1: {52: 9}})["cells"][1], self.read, tm, fm, {}), [0, 0, 0, 0])   # no motion for it: with the cast
        sk = _fx_stb({10: {4: 1, 5: 3, 51: 7, 56: 1, 57: 3, 59: 2, 52: 5, 53: 50, 54: 6, 68: 7, 69: 200}})
        skills, _n, _w, b = E.build_tables(self.read, self.fe, self.le, sk, type_motion=tm, file_motion=fm)
        self.assertEqual(skills[0]["cast"][:2], [1, 2])
        self.assertEqual(skills[0]["cast_at"][:2], [1000, at[1]])
        x = E.dec_efct(E.enc_efct(b, skills))
        self.assertEqual(x["skills"][0][20:24], (1000, at[1], E.CAST_NEVER, E.CAST_NEVER))

    def test_w17_party_levelup(self):
        E = self.E
        self.files["3DDATA/EFFECT/PARTY_UP_01.EFT"] = _fx_eft([("FX/A.PTL", (0, 0, 0), 0)])
        _s, _n, _w, b = E.build_tables(self.read, self.fe, self.le, self.sk)
        self.assertEqual(b.partyup, E.PARTY_UP_ID)
        fe2 = _fx_stb({1: {1: "FX\\CAST.EFT"}, 297: {1: "3DDATA\\EFFECT\\PARTY_UP_01.EFT"}})
        _s, _n, _w, b2 = E.build_tables(self.read, fe2, self.le, self.sk)
        self.assertEqual(b2.partyup, 297)
        x = E.dec_efct(E.enc_efct(b2, _s))
        self.assertEqual(x["partyup"], 297)


def zlib_crc(b):
    import zlib
    return zlib.crc32(b) & 0xffffffff


def rose_conv_rose_max():
    import rose
    return rose.MAX_BYTES


if __name__ == "__main__":
    unittest.main()
