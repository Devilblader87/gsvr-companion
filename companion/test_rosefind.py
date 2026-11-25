import json, os, sys, tempfile, types, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rosefind, steam


def make_install(d):
    os.makedirs(d, exist_ok=True)
    for n in ("data.idx", "base.vfs"):
        with open(os.path.join(d, n), "wb") as fh:
            fh.write(b"x")
    return os.path.normpath(d)


class FakeReg:
    def __init__(self, tree):
        self.tree = tree          # {(hive, key): {value: str}}

    def subkeys(self, hive, key):
        pre = key + "\\"
        return sorted({k[len(pre):].split("\\")[0] for (h, k) in self.tree if h == hive and k.startswith(pre)})

    def read(self, hive, key, value):
        return self.tree.get((hive, key), {}).get(value)


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = self.tmp.name
        self.kw = dict(registry=FakeReg({}), steam_roots=[], env={}, drives=[])
        self._conv = sys.modules.pop("rose_conv", None)
        sys.modules["rose_conv"] = None          # import fails -> rosefind.local_check (the real rose_conv wants a real VFS index)

    def tearDown(self):
        self.tmp.cleanup()
        if self._conv is not None:
            sys.modules["rose_conv"] = self._conv
        else:
            sys.modules.pop("rose_conv", None)

    def test_validation(self):
        good = make_install(os.path.join(self.t, "ROSE Online"))
        bad = os.path.join(self.t, "ROSE Empty")
        os.makedirs(bad)
        r = rosefind.find(configured=bad, **dict(self.kw, drives=[self.t]))
        self.assertEqual(r, [good])

    def test_order(self):
        cfg = make_install(os.path.join(self.t, "cfg"))
        regd = make_install(os.path.join(self.t, "reg"))
        lib = os.path.join(self.t, "lib")
        stm = make_install(os.path.join(lib, "steamapps", "common", "ROSE Online"))
        com = make_install(os.path.join(self.t, "local", "ROSE"))
        drv = make_install(os.path.join(self.t, "drv", "Games", "MyROSE"))
        reg = FakeReg({("HKLM", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\r"):
                       {"DisplayName": "ROSE Online", "InstallLocation": regd},
                       ("HKCU", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\x"):
                       {"DisplayName": "Other", "InstallLocation": os.path.join(self.t, "nope")}})
        old = steam.library_folders
        steam.library_folders = lambda root: [lib]
        try:
            r = rosefind.find(configured=cfg, registry=reg, steam_roots=["S"],
                              env={"LOCALAPPDATA": os.path.join(self.t, "local")}, drives=[os.path.join(self.t, "drv")])
        finally:
            steam.library_folders = old
        self.assertEqual(r, [cfg, regd, stm, com, drv])
        how = [h for _p, h, _r in rosefind.find(configured=cfg, with_reasons=True, **self.kw)]
        self.assertEqual(how, ["configured"])

    def test_registry_displayicon_exe(self):
        d = make_install(os.path.join(self.t, "g"))
        reg = FakeReg({("HKCU", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\r"):
                       {"DisplayName": "Rose Online", "DisplayIcon": '"%s,0"' % os.path.join(d, "trose.exe")}})
        self.assertEqual(rosefind.find(**dict(self.kw, registry=reg)), [d])

    def test_dedupe(self):
        d = make_install(os.path.join(self.t, "ROSE"))
        self.assertEqual(rosefind.find(configured=d, **dict(self.kw, drives=[self.t])), [d])

    def test_drive_scan_depth_and_timebox(self):
        make_install(os.path.join(self.t, "a", "b", "c", "ROSE"))      # depth 4: too deep
        self.assertEqual(rosefind.find(**dict(self.kw, drives=[self.t])), [])
        deep = make_install(os.path.join(self.t, "a", "ROSE"))
        self.assertEqual(rosefind.find(**dict(self.kw, drives=[self.t])), [deep])
        clock = iter(range(0, 1000))                                   # every tick is 1 s -> budget gone at once
        r = rosefind.candidates(**dict(self.kw, drives=[self.t]), budget=0.5, now=lambda: next(clock))
        self.assertEqual(r, [])

    def test_configured_only_without_injection(self):
        if sys.platform != "win32":
            d = make_install(os.path.join(self.t, "x"))
            self.assertEqual(rosefind.candidates(configured=d, registry=None, steam_roots=[], env={}, drives=[]), [(d, "configured")])

    def test_config_roundtrip(self):
        g = os.path.join(self.t, "g.json")
        with open(g, "w") as fh:
            json.dump({"hl_root": "H"}, fh)
        self.assertEqual(rosefind.load_config(g), "")
        rosefind.save_config(g, " D:\\ROSE ")
        self.assertEqual(rosefind.load_config(g), "D:\\ROSE")
        with open(g) as fh:
            self.assertEqual(json.load(fh)["hl_root"], "H")
        f = os.path.join(self.t, "sub", "gui.json")
        rosefind.save_config(f, "x")
        self.assertEqual(rosefind.load_config(f), "x")
        self.assertEqual(rosefind.CONFIG_KEY, "rose_install")

    def test_uses_rose_conv_when_present(self):
        m = types.ModuleType("rose_conv")
        m.check_install = lambda p: (p.endswith("ok"), "stub")
        sys.modules["rose_conv"] = m
        try:
            os.makedirs(os.path.join(self.t, "ok"))
            os.makedirs(os.path.join(self.t, "no"))
            self.assertEqual(rosefind.check(os.path.join(self.t, "ok")), (True, "stub"))
            self.assertFalse(rosefind.check(os.path.join(self.t, "no"))[0])
        finally:
            del sys.modules["rose_conv"]


class StatusText(unittest.TestCase):
    def test_live_text(self):
        try:
            import gui
        except Exception as e:
            self.skipTest("gui import: %s" % e)
        f = gui.rose_live_text
        self.assertEqual(f({"state": "building", "name": "luxemtower", "progress": 0.456}), "building luxemtower 46%")
        self.assertEqual(f({"state": "ready", "all_ready": True}), "all zones ready")
        self.assertTrue(f({"state": "error", "text": "x"}).startswith("error"))
        self.assertEqual(f(None), "")


class GuiWiring(unittest.TestCase):
    """audit 1 + 8 (source guards, gui.py needs Tk to import): one settings path; no blocking ROSE calls on the Tk thread"""
    def src(self, name):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), name), encoding="utf-8") as fh:
            return fh.read()

    def body(self, s, start, end):
        i = s.index(start)
        return s[i:s.index(end, i + len(start))]

    def test_settings_path_shared(self):
        g, c = self.src("gui.py"), self.src("companion.py")
        self.assertIn("return rosefind.settings_file()", self.body(g, "def settings_path():", "def cache_dir("))
        self.assertIn("rosefind.load_config(rosefind.settings_file())", c)
        self.assertNotIn("rosefind.load_config()", c)
        self.assertTrue(rosefind.settings_file().endswith("gui.json"))

    def test_no_blocking_rose_calls_on_tk_thread(self):
        g = self.src("gui.py")
        show = self.body(g, "def rose_show(", "def rose_apply(")
        self.assertNotIn("rosefind.check(", show)                       # only inside rose_check_bg's worker
        chk = self.body(g, "def rose_check_bg(", "def rose_show(")
        self.assertIn("bg(work)", chk)
        self.assertIn("rosefind.check(path)", chk)
        ap = self.body(g, "def rose_apply(", "def rose_detect(")
        self.assertIn('bg(lambda: ui_q.put(("rose_applied", c.rose_post(q))))', ap)
        ba = self.body(g, "def rose_build_all(", "def rose_built(")
        self.assertIn('bg(lambda: ui_q.put(("rose_built", c.rose_post("/rose/build_all"))))', ba)
        self.assertIn('command=lambda p=r["rebuild"]: bg(lambda: c.rose_post(p))', g)   # 'Neu bauen' button (worlds list)
        self.assertEqual(g.count("c.rose_post("), 3)                     # all three inside bg()
        self.assertIn('st["_rose_state"] = c.rose_state()', self.body(g, "def poll_once(", "def poll_status("))
        self.assertNotIn("rosebuild.state()", g)                          # the GUI reads GET /rose/state, not its own process

    def test_save_posts_typed_folder(self):
        g = self.src("gui.py")
        rem = self.body(g, "def remember():", "c.settings.update(")
        self.assertIn('if typed_rose != rose_sent["v"] and c.running():', rem)
        self.assertIn("rose_apply(typed_rose)", rem)


if __name__ == "__main__":
    unittest.main()
