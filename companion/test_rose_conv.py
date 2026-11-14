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
        self.assertTrue(re.fullmatch(r"[0-9a-f]{40}", rose_conv.SOURCE), rose_conv.SOURCE)

    def test_packs_table(self):
        P = rose_conv.PACKS
        self.assertEqual(set(P), {"birthisland", "luxemtower", "adventurersplain", "breezyhills", "zant", "elverloondesert", "login", "avatar"})
        zones = {n: (s["zone"], s["all"]) for n, s in P.items() if s["kind"] == "zone"}
        self.assertEqual(zones, {"birthisland": (20, False), "luxemtower": (21, True), "adventurersplain": (22, True), "breezyhills": (23, True),
                                 "zant": (1, True), "elverloondesert": (24, True)})
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


if __name__ == "__main__":
    unittest.main()
