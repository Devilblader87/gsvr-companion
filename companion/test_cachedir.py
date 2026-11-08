"""Cache folder choice + move (companion/cachedir.py, gui.Controller cache_dir/argv).

Run: python3 -I companion/test_cachedir.py        (no game files, no Tk)
"""
import ntpath, os, posixpath, sys, tempfile, threading, unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cachedir, gui            # noqa: E402


def write(p, data):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as fh:
        fh.write(data)


def quiet(*_a):
    pass


class T(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.src, self.dst = os.path.join(self.t, "old"), os.path.join(self.t, "new")
        write(os.path.join(self.src, "a.bin"), b"a" * 100)
        write(os.path.join(self.src, "mapdl", "b.bsp"), b"b" * 50)
        write(os.path.join(self.src, "half.tmp"), b"x" * 10)

    def test_drive_root_mapping(self):
        self.assertEqual(cachedir.resolve("D:\\", "dflt", ntpath), "D:\\GSVR\\cache")
        self.assertEqual(cachedir.resolve("D:", "dflt", ntpath), "D:\\GSVR\\cache")
        self.assertEqual(cachedir.resolve("E:\\Games\\c", "dflt", ntpath), "E:\\Games\\c")
        self.assertEqual(cachedir.resolve("", "dflt", ntpath), "dflt")
        self.assertEqual(cachedir.resolve("/", "dflt", posixpath), "/GSVR/cache")
        self.assertEqual(cachedir.resolve("/mnt/x/", "dflt", posixpath), "/mnt/x")

    def test_validate(self):
        game = os.path.join(self.t, "game")
        os.makedirs(game)
        self.assertTrue(cachedir.validate(os.path.join(game, "cache"), [game])[0])
        self.assertEqual(cachedir.validate(self.dst, [game])[0], [])
        with mock.patch.object(cachedir, "free_bytes", return_value=1 << 30):
            self.assertTrue(cachedir.validate(self.dst)[1])
        blocker = os.path.join(self.t, "file")
        write(blocker, b"x")
        self.assertTrue(cachedir.validate(os.path.join(blocker, "sub"))[0])     # not creatable

    def test_move_ok(self):
        seen = []
        ok, done, total = cachedir.move_cache(self.src, self.dst, progress=lambda d, t: seen.append((d, t)), log=quiet)
        self.assertTrue(ok)
        self.assertEqual((done, total), (150, 150))
        self.assertEqual(seen[-1], (150, 150))
        with open(os.path.join(self.dst, "mapdl", "b.bsp"), "rb") as fh:
            self.assertEqual(fh.read(), b"b" * 50)
        self.assertFalse(os.path.exists(os.path.join(self.src, "a.bin")))
        self.assertFalse(os.path.exists(os.path.join(self.src, "mapdl")))              # empty dir pruned
        self.assertTrue(os.path.exists(os.path.join(self.src, "half.tmp")))            # *.tmp skipped
        self.assertFalse(os.path.exists(os.path.join(self.dst, "half.tmp")))

    def test_cancel_keeps_old(self):
        cancel = threading.Event()
        cancel.set()
        self.assertFalse(cachedir.move_cache(self.src, self.dst, cancel=cancel, log=quiet)[0])
        self.assertEqual(sorted(r for r, _s in cachedir.list_files(self.src)), sorted(["a.bin", os.path.join("mapdl", "b.bsp")]))

    def test_cancel_midway_keeps_old(self):
        cancel = threading.Event()
        ok = cachedir.move_cache(self.src, self.dst, progress=lambda d, t: cancel.set(), cancel=cancel, log=quiet)[0]
        self.assertFalse(ok)
        self.assertEqual(len(cachedir.list_files(self.src)), 2)

    def test_failure_midway_keeps_everything(self):
        real = cachedir.shutil.copyfile
        calls = []

        def flaky(a, b):
            calls.append(a)
            if len(calls) == 2:
                raise OSError("disk full")
            return real(a, b)
        with mock.patch.object(cachedir.shutil, "copyfile", flaky):
            ok = cachedir.move_cache(self.src, self.dst, log=quiet)[0]
        self.assertFalse(ok)
        with open(os.path.join(self.src, "a.bin"), "rb") as fh:
            self.assertEqual(fh.read(), b"a" * 100)
        self.assertEqual(len(cachedir.list_files(self.src)), 2)

    def test_size_mismatch_detected(self):
        real = cachedir.shutil.copyfile

        def longer(a, b):
            real(a, b)
            with open(b, "ab") as fh:
                fh.write(b"?")
        with mock.patch.object(cachedir.shutil, "copyfile", longer):
            ok = cachedir.move_cache(self.src, self.dst, log=quiet)[0]
        self.assertFalse(ok)
        self.assertEqual(len(cachedir.list_files(self.src)), 2)

    def test_overlap_refused(self):
        self.assertFalse(cachedir.move_cache(self.src, os.path.join(self.src, "sub"), log=quiet)[0])
        self.assertTrue(os.path.exists(os.path.join(self.src, "a.bin")))

    def test_controller_uses_choice(self):
        with mock.patch.object(gui, "settings_path", lambda: os.path.join(self.t, "gui.json")):
            c = gui.Controller()
            c.log = quiet
            self.assertEqual(c.cache_dir(), gui.companion.default_cache_dir())
            self.assertNotIn("--cache-dir", c.argv())
            c.settings["cache_dir"] = self.src
            a = c.argv()
            self.assertEqual(a[a.index("--cache-dir") + 1], self.src)
            ok, old, new = c.change_cache_dir(self.dst, True)
            self.assertTrue(ok)
            self.assertEqual((old, new), (self.src, self.dst))
            self.assertEqual(c.cache_dir(), self.dst)
            self.assertTrue(os.path.exists(os.path.join(self.dst, "a.bin")))
            c2 = gui.Controller()
            self.assertEqual(c2.settings["cache_dir"], self.dst)       # persisted


if __name__ == "__main__":
    unittest.main()
