"""rose_conv (the companion's copy of the ROSE converter, docs/design/rose_companion_build.md lane B): API without game data.

  cd companion && python3 -I -m unittest discover -p "test_rose_conv.py"
"""
import hashlib
import inspect
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
        """ROSE w13: WRAPPER_REV 5 (weapon-class motions in every zone pack). ROSE w12 summons: WRAPPER_REV 4 (SKDB carries the magic item skills + summon npcs, every zone the summon actors: all packs rebuild once); Birth Island alone carries a per-pack revision."""
        self.assertEqual(rose_conv.WRAPPER_REV, 5)
        self.assertEqual(rose_conv.PACK_REV, {"birthisland": 2})
        self.assertEqual(rose_conv.ZONE_MAX_SLOPE, {20: 2.0})
        self.assertEqual(rose_conv.pack_version("birthisland"), rose_conv.VERSION + "-p2")
        for n in rose_conv.PACKS:
            if n != "birthisland":
                self.assertEqual(rose_conv.pack_version(n), rose_conv.VERSION)
        self.assertTrue(re.fullmatch(r"[0-9a-f]{40}", rose_conv.SOURCE), rose_conv.SOURCE)

    def test_weapon_motion_table(self):
        """W13: the weapon class -> motion type table the runtime also holds (GsvrRoseItems.WeaponMotionType); every motion type is baked for the avatar"""
        import rose_chars
        self.assertEqual(rose_chars.WEAPON_CLASS_MOTION, {211: 1, 212: 3, 221: 5, 222: 6, 223: 8, 231: 10, 232: 12, 233: 9, 241: 6, 242: 14, 251: 16, 252: 7, 253: 12, 271: 11})
        self.assertEqual(rose_chars.WEAPON_MOTION_TYPES, (0, 1, 3, 5, 6, 7, 8, 9, 10, 11, 12, 14, 16))
        self.assertTrue(set(rose_chars.AVATAR_WEAPON_TYPES) <= set(rose_chars.WEAPON_MOTION_TYPES))

    def test_packs_table(self):
        P = rose_conv.PACKS
        self.assertEqual(set(P), {"birthisland", "luxemtower", "adventurersplain", "breezyhills", "zant", "elverloondesert", "animalake", "forestofwisdom", "kenjibeach", "gorgeofsilence", "goblincaveb1", "goblincaveb2", "goblincaveb3", "junonpolis", "dolphinisland", "luna", "eldeon", "clanfield", "junonclanfield", "manasnowfields", "arumicvalley", "crystalsnowfields", "freezingplateau", "login", "avatar"})
        zones = {n: (s["zone"], s["all"]) for n, s in P.items() if s["kind"] == "zone"}
        self.assertEqual(zones, {"birthisland": (20, True), "luxemtower": (21, True), "adventurersplain": (22, True), "breezyhills": (23, True),
                                 "zant": (1, True), "elverloondesert": (24, True), "animalake": (25, True), "forestofwisdom": (26, True),
                                 "kenjibeach": (27, True), "gorgeofsilence": (28, True), "goblincaveb1": (31, True),
                                 "goblincaveb2": (32, True), "goblincaveb3": (33, True), "junonpolis": (2, True), "dolphinisland": (3, True), "luna": (51, True), "eldeon": (61, True), "clanfield": (15, True), "junonclanfield": (11, True), "manasnowfields": (52, True), "arumicvalley": (53, True), "crystalsnowfields": (54, True), "freezingplateau": (55, True)})
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
        own = {"__init__.py"}
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


if __name__ == "__main__":
    unittest.main()
