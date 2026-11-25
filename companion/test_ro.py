"""Ragnarok Online companion lane: rofind (detection), ro.py (pack route), rosebuild flavor RO with the fake converter ro_conv_stub, and a
live companion (request ro=prontera -> build in a child process -> /pack/ro.pack, status.json "ro" / "ro_build" without any path).
SYNTHETIC fixtures only (a data.grf made of the 15 byte GRF magic + filler); no game data, no network beyond 127.0.0.1.

Run: cd companion && python3 -m unittest test_ro      (PYTHONPATH=. when run with -I)
"""
import json, os, shutil, struct, subprocess, sys, tempfile, threading, time, unittest, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ro, rofind, rosebuild, ro_conv_stub as stub, steam      # noqa: E402

GRF_MAGIC = b"Master of Magic"


def make_client(d, magic=GRF_MAGIC, size=64):
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "data.grf"), "wb") as fh:
        fh.write(magic + b"\0" * max(0, size - len(magic)))
    return os.path.normpath(d)


def make_pack(total=256):
    head = b"RPG1" + struct.pack("<7I", (1 << 16) | 1, 32, total, 0, total, 0, 0)
    return head + bytes((i * 7) & 255 for i in range(total - len(head)))


class FakeReg:
    def __init__(self, tree):
        self.tree = tree

    def subkeys(self, hive, key):
        pre = key + "\\"
        return sorted({k[len(pre):].split("\\")[0] for (h, k) in self.tree if h == hive and k.startswith(pre)})

    def read(self, hive, key, value):
        return self.tree.get((hive, key), {}).get(value)


class Find(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = self.tmp.name
        self.kw = dict(registry=FakeReg({}), steam_roots=[], env={}, drives=[])
        self._conv = sys.modules.pop("ro_conv", None)
        sys.modules["ro_conv"] = None          # import fails -> local_check

    def tearDown(self):
        self.tmp.cleanup()
        if self._conv is not None:
            sys.modules["ro_conv"] = self._conv
        else:
            sys.modules.pop("ro_conv", None)

    def test_validation(self):
        good = make_client(os.path.join(self.t, "Ragnarok"))
        nogrf = os.path.join(self.t, "Ragnarok2")
        os.makedirs(nogrf)
        fake = make_client(os.path.join(self.t, "Ragnarok3"), magic=b"not a grf at all")
        self.assertEqual(rofind.local_check(good)[0], True)
        self.assertEqual(rofind.local_check(nogrf), (False, "no data.grf in the folder"))
        self.assertFalse(rofind.local_check(fake)[0])
        self.assertFalse(rofind.local_check(os.path.join(self.t, "nope"))[0])
        for ok, why in (rofind.local_check(nogrf), rofind.local_check(fake)):
            self.assertNotIn(self.t, why)                       # reasons never carry a folder name
        self.assertEqual(rofind.find(configured=nogrf, **self.kw), [])
        self.assertEqual(rofind.find(configured=fake, **self.kw), [])
        self.assertEqual(rofind.find(configured=good, **self.kw), [good])
        self.assertEqual(rofind.find(**dict(self.kw, drives=[self.t])), [good])             # drive scan: folder named like a client

    def test_case_insensitive_grf_name(self):
        d = os.path.join(self.t, "RO")
        os.makedirs(d)
        with open(os.path.join(d, "DATA.GRF"), "wb") as fh:
            fh.write(GRF_MAGIC + b"\0" * 8)
        self.assertTrue(rofind.local_check(d)[0])

    def test_order_and_sources(self):
        cfg = make_client(os.path.join(self.t, "cfg"))
        regd = make_client(os.path.join(self.t, "reg"))
        vend = make_client(os.path.join(self.t, "vend"))
        lib = os.path.join(self.t, "lib")
        stm = make_client(os.path.join(lib, "steamapps", "common", "Ragnarok Online"))
        sd = os.path.join(self.t, "sysdrive")
        com = make_client(os.path.join(sd, "Gravity", "Ragnarok"))
        warp = make_client(os.path.join(sd, "WarpPortal", "Ragnarok Online"))
        drv = make_client(os.path.join(self.t, "drv", "Games", "MyRagnarok"))
        reg = FakeReg({("HKLM", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\r"):
                       {"DisplayName": "Ragnarok Online", "InstallLocation": regd},
                       ("HKCU", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\x"):
                       {"DisplayName": "Other", "InstallLocation": os.path.join(self.t, "nope")},
                       ("HKLM", r"SOFTWARE\Gravity Co., Ltd.\Ragnarok"): {"InstallDir": vend}})
        old = steam.library_folders
        steam.library_folders = lambda root: [lib]
        try:
            kw = dict(registry=reg, steam_roots=["x"], env={"SystemDrive": sd}, drives=[os.path.join(self.t, "drv")])
            got = rofind.find(configured=cfg, with_reasons=True, **kw)
            self.assertEqual([(p, h) for p, h, _r in got],
                             [(cfg, "configured"), (regd, "registry"), (vend, "registry"), (stm, "steam"),
                              (os.path.normpath(com), "common folder"), (os.path.normpath(warp), "common folder"), (drv, "drive scan")])
        finally:
            steam.library_folders = old

    def test_parent_folder_pick_and_child(self):
        parent = os.path.join(self.t, "Gravity")
        inner = make_client(os.path.join(parent, "Ragnarok"))
        self.assertEqual(rofind.find(configured=parent, **self.kw), [inner])

    def test_config_roundtrip(self):
        f = os.path.join(self.t, "gui.json")
        self.assertEqual(rofind.load_config(f), "")
        with open(f, "w") as fh:
            json.dump({"ro_install": " /x/y ", "rose_install": "/r"}, fh)
        self.assertEqual(rofind.load_config(f), "/x/y")

    def test_check_prefers_converter(self):
        class C:
            @staticmethod
            def check_install(p):
                return False, "converter says no"
        sys.modules["ro_conv"] = C
        self.assertEqual(rofind.check(make_client(os.path.join(self.t, "x"))), (False, "converter says no"))


class Route(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="rotest")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def put(self, name, data):
        with open(os.path.join(self.d, f"ro__{name}.pack"), "wb") as fh:
            fh.write(data)

    def test_valid_and_errors(self):
        self.put("prontera", make_pack(1000))
        r = ro.prepare(self.d, "prontera")
        self.assertEqual((r["state"], r["size"]), ("ready", 1000))
        self.assertIn("no ro__nothere.pack", ro.prepare(self.d, "nothere")["error"])
        bad = {"magic": b"GSV1" + make_pack(64)[4:], "size": make_pack(64)[:-1], "short": b"RPG1", "trail": make_pack(64) + b"\0"}
        for k, data in bad.items():
            self.put("bad", data)
            r = ro.prepare(self.d, "bad")
            self.assertEqual(r["state"], "error", k)
            self.assertIsNone(r["path"], k)

    def test_names_and_traversal(self):
        for n in ("prontera", "a" * 32, "x_1"):
            self.assertTrue(ro.valid_name(n))
        for n in ("", "A", "a" * 33, "../x", "a.b", None, "a/b"):
            self.assertFalse(ro.valid_name(n), repr(n))
        sub = os.path.join(self.d, "ro")
        os.makedirs(sub)
        with open(os.path.join(self.d, "ro__out.pack"), "wb") as fh:
            fh.write(make_pack(64))
        for n in ("../ro__out", "..", "/etc/passwd"):
            self.assertEqual(ro.prepare(sub, n)["state"], "error")

    def test_larger_cap_than_rose(self):
        self.assertGreater(ro.MAX_BYTES, 100 * 1024 * 1024)

    def test_public_slot_hides_path(self):
        s = ro.new_slot()
        s["path"] = "/x"
        self.assertEqual(set(ro.public(s)), {"state", "req", "name", "size", "error"})


def until(pred, timeout=20):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


class Build(unittest.TestCase):
    """rosebuild.Manager(flavor=RO) with the fake converter: cache keyed by data.grf size/mtime, file names, queue/progress, failure"""
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="robuild")
        self.rdir = os.path.join(self.work, "ro")
        self.client = make_client(os.path.join(self.work, "Ragnarok"))
        self.ups, self.logs = [], []
        stub.CALLS.clear()
        stub.FAIL.clear()
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)

    def mgr(self, **kw):
        m = rosebuild.Manager(self.rdir, conv=stub, log=self.logs.append, on_update=lambda n, f: self.ups.append((n, dict(f))),
                              flavor=rosebuild.RO, **kw)
        self.addCleanup(m.stop)
        return m

    def ask_until_ready(self, m, name="prontera"):
        r = m.request(name)
        if r["state"] == "building":
            self.assertTrue(until(lambda: any(n == name and f.get("state") in ("ready", "error") for n, f in self.ups)))
            r = [f for n, f in self.ups if n == name][-1]
        return r

    def test_build_and_cache(self):
        m = self.mgr()
        self.assertEqual(m.set_install(self.client, prebuild=False)[0], True)
        r = self.ask_until_ready(m)
        self.assertEqual(r["state"], "ready", r)
        self.assertEqual(os.path.basename(r["path"]), "ro__prontera.pack")
        self.assertTrue(os.path.isfile(r["path"] + ".json"))
        sc = rosebuild.read_sidecar(self.rdir, "prontera", rosebuild.RO)
        self.assertEqual(sc["install"], rosebuild.fingerprint(stub, self.client))
        # fresh -> served at once, no rebuild
        self.assertEqual(m.request("prontera")["state"], "ready")
        self.assertEqual(stub.CALLS.count("prontera"), 1)
        self.assertFalse(any("[rose]" in l for l in self.logs))
        self.assertTrue(any("[ro] build prontera started" in l for l in self.logs))

    def test_cache_key_is_grf_size_and_mtime(self):
        fp0 = rosebuild.fingerprint(stub, self.client)
        g = os.path.join(self.client, "data.grf")
        st = os.stat(g)
        os.utime(g, (st.st_atime, st.st_mtime + 100))
        fp1 = rosebuild.fingerprint(stub, self.client)
        self.assertNotEqual(fp0, fp1)
        make_client(self.client, size=200)
        self.assertNotEqual(fp1, rosebuild.fingerprint(stub, self.client))
        # an unrelated file in the folder does not change the key
        fp2 = rosebuild.fingerprint(stub, self.client)
        with open(os.path.join(self.client, "readme.txt"), "w") as fh:
            fh.write("x")
        self.assertEqual(fp2, rosebuild.fingerprint(stub, self.client))
        # stale after a client update -> rebuilt
        m = self.mgr()
        m.set_install(self.client, prebuild=False)
        self.assertEqual(self.ask_until_ready(m)["state"], "ready")
        self.assertEqual(m.pack_state("prontera"), "fresh")
        make_client(self.client, size=999)
        self.assertEqual(m.pack_state("prontera"), "stale")
        self.ups.clear()
        self.assertEqual(self.ask_until_ready(m)["state"], "ready")
        m.wait_idle(10)
        self.assertEqual(stub.CALLS.count("prontera"), 2)

    def test_no_install_and_bad_folder(self):
        m = self.mgr()
        r = m.request("prontera")
        self.assertEqual(r["state"], "error")
        self.assertIn("Ragnarok install not found", r["error"])
        self.assertIn("no ro__prontera.pack", r["error"])
        ok, why = m.set_install(os.path.join(self.work, "empty"))
        self.assertFalse(ok)
        self.assertIn("Ragnarok", m.request("prontera")["error"] + why + "Ragnarok")

    def test_hand_dropped_pack_served_without_converter(self):
        os.makedirs(self.rdir)
        with open(os.path.join(self.rdir, "ro__prontera.pack"), "wb") as fh:
            fh.write(make_pack(500))
        m = rosebuild.Manager(self.rdir, conv=None, autoload=False, log=self.logs.append, flavor=rosebuild.RO)
        self.addCleanup(m.stop)
        self.assertEqual(m.request("prontera")["state"], "ready")

    def test_failure_reported(self):
        stub.FAIL.add("prontera")
        stub.FAIL_AT_ONCE = False
        self.addCleanup(setattr, stub, "FAIL_AT_ONCE", stub.FAIL_AT_ONCE)
        m = self.mgr()
        m.set_install(self.client, prebuild=False)
        r = self.ask_until_ready(m)
        self.assertEqual(r["state"], "error")
        self.assertIn("Ragnarok build of prontera failed", r["error"])

    def test_rose_manager_unchanged(self):
        self.assertEqual(rosebuild.pack_path("/d", "x"), os.path.join("/d", "rose__x.rpg1"))
        self.assertEqual(rosebuild.pack_path("/d", "x", rosebuild.RO), os.path.join("/d", "ro__x.pack"))
        self.assertEqual(rosebuild.child_argv("n", "i", "o", rosebuild.RO)[-4], "--run-ro-build")
        self.assertEqual(rosebuild.child_argv("n", "i", "o")[-4], "--run-rose-build")

    def test_child_process_build(self):
        m = rosebuild.Manager(self.rdir, conv=stub, isolate=True, log=self.logs.append, flavor=rosebuild.RO,
                              on_update=lambda n, f: self.ups.append((n, dict(f))), child_env={"GSVR_RO_STUB_STEP_S": "0.01"})
        self.addCleanup(m.stop)
        m.set_install(self.client, prebuild=False)
        r = self.ask_until_ready(m)
        self.assertEqual(r["state"], "ready", r)
        self.assertTrue(os.path.isfile(os.path.join(self.rdir, "ro__prontera.pack")))


def public_has_no_path(obj, forbidden):
    s = json.dumps(obj)
    return not any(f and f in s for f in forbidden)


class Live(unittest.TestCase):
    def test_live_companion(self):
        import selftest as st
        http, wait_status, append, free_port = st.http, st.wait_status, st.append, st.free_port
        work = tempfile.mkdtemp(prefix="rolive")
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)
        pdir, rdir, logdir = (os.path.join(work, x) for x in ("pre", "ro", "log"))
        for d in (pdir, logdir):
            os.makedirs(d)
        client = make_client(os.path.join(work, "Gravity", "Ragnarok"))
        with open(os.path.join(pdir, "valve__c1a0.bin"), "wb") as fh:
            fh.write(b"GSV1" + b"PACK" * 10)
        lg = os.path.join(logdir, "output_log_2026-11-12_10-00-00.txt")
        append(lg, "start")
        port = free_port()
        env = dict(os.environ, GSVR_RO_CONV="stub", GSVR_RO_STUB_STEP_S="0.05")
        cmd = [sys.executable, "-I", os.path.join(HERE, "companion.py"), "--prebuilt-dir", pdir, "--vrchat-log-dir", logdir,
               "--cache-dir", os.path.join(work, "cache"), "--ro-dir", rdir, "--ro-install", client, "--no-rose-find",
               "--no-ro-find", "--world-allow", "wrld_ok", "--port", str(port)]
        proc = subprocess.Popen(cmd, stdout=open(os.path.join(work, "companion.out"), "w"), stderr=subprocess.STDOUT, env=env)
        self.addCleanup(proc.wait, 10)
        self.addCleanup(proc.terminate)
        s = wait_status(port, lambda s: True, 20)
        self.assertIsNotNone(s)
        # w32 privacy: before an allowed world the world gets only the minimal status; the GUI route has the full one
        self.assertFalse(s["world_ok"])
        self.assertNotIn("ro", s)
        self.assertNotIn("ro_build", s)
        s = json.loads(http(port, "/gui/status.json", headers={"X-GSVR-Gui": "1"})[1])
        self.assertEqual(s["ro"], dict(state="idle", req=None, name=None, size=0, error=None, **{k: s["ro"][k] for k in ("text", "all_ready", "maps")}))
        self.assertIn("ro_build", s)
        append(lg, "Joining wrld_ok")
        wait_status(port, lambda s: s["world_ok"], 10)
        self.assertEqual(http(port, "/pack/ro.pack")[0], 503)
        append(lg, "[GSVR-REQ] ro=prontera req=r1")
        s = wait_status(port, lambda s: s["ro"]["req"] == "r1" and s["ro"]["state"] in ("ready", "error"), 40)
        self.assertIsNotNone(s)
        self.assertEqual(s["ro"]["state"], "ready", s["ro"])
        self.assertEqual(s["ro"]["name"], "prontera")
        code, data = http(port, "/pack/ro.pack")
        self.assertEqual(code, 200)
        self.assertEqual(data[:4], b"RPG1")
        # privacy: nothing in status.json names the client folder or a path
        self.assertTrue(public_has_no_path(s, [client, work, os.path.dirname(client)]))
        self.assertNotIn("install", s["ro_build"])
        self.assertTrue(s["ro_build"]["install_set"])
        self.assertTrue(s["ro_build"]["install_ok"])
        self.assertNotIn("path", s["ro"])
        # missing + bad names
        append(lg, "[GSVR-REQ] ro=geffen2 req=r2")
        s = wait_status(port, lambda s: s["ro"]["req"] == "r2" and s["ro"]["state"] in ("ready", "error"), 20)
        self.assertEqual(s["ro"]["state"], "error")
        self.assertEqual(http(port, "/pack/ro.pack")[0], 404)
        append(lg, "[GSVR-REQ] ro=..%2Fx req=r3")
        s = wait_status(port, lambda s: s["ro"]["req"] == "r3", 20)
        self.assertEqual(s["ro"]["state"], "error")
        self.assertIsNone(s["ro"]["name"])
        # RO QA one-build selection (docs/design/ro_qa.md): the round writes <ro dir>/test_scenario.txt, status.json shows it, and a bad name / no file shows nothing
        self.assertEqual(s["test_scenario"], "")
        tf = os.path.join(rdir, "test_scenario.txt")
        with open(tf, "w") as fh:
            fh.write("roqafight\n")
        s = wait_status(port, lambda s: s.get("test_scenario") == "roqafight", 10)
        self.assertIsNotNone(s, "status.json test_scenario follows <ro dir>/test_scenario.txt")
        with open(tf, "w") as fh:
            fh.write("../x\n")
        s = wait_status(port, lambda s: s.get("test_scenario") == "", 10)
        self.assertIsNotNone(s, "an invalid name is not shown")
        os.remove(tf)
        # GUI control routes need the header
        self.assertEqual(http(port, "/ro/state")[0], 403)
        req = urllib.request.Request(f"http://127.0.0.1:{port}/ro/state", headers={"Host": "127.0.0.1", "X-GSVR-Rose": "1"})
        with urllib.request.urlopen(req, timeout=10) as r:
            body = json.loads(r.read())
        self.assertNotIn("install", body)
        self.assertEqual(body["packs"]["prontera"]["state"], "fresh")
        self.assertEqual(http(port, "/ro/install", method="POST")[0], 405)
        req = urllib.request.Request(f"http://127.0.0.1:{port}/ro/install?dir=" + urllib.request.quote(os.path.join(work, "nothing")),
                                     data=b"", method="POST", headers={"Host": "127.0.0.1", "X-GSVR-Rose": "1"})
        with urllib.request.urlopen(req, timeout=10) as r:
            ans = json.loads(r.read())
        self.assertFalse(ans["ok"])
        self.assertNotIn("install", ans["ro_build"])
        self.assertIsNone(proc.poll())


if __name__ == "__main__":
    unittest.main()
