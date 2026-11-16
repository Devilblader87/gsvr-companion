"""ROSE pack route (/pack/rose.rpg1, companion/rose.py): validation units + a live companion on a localhost ephemeral port.

Run: python3 -I companion/test_rose_route.py   (synthetic RPG1 headers only, no game files, no network beyond 127.0.0.1)
"""
import os, shutil, struct, subprocess, sys, tempfile, threading, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import rose                                         # noqa: E402


def make_pack(total=256, major=1, minor=1, magic=b"RPG1", file_size=None, header_bytes=32):
    """Synthetic RPG1: 32 byte header (magic, u32 version = major<<16|minor, headerBytes, dirOff, count, fileSize, flags, 0) + filler."""
    fs = total if file_size is None else file_size
    head = magic + struct.pack("<7I", (major << 16) | minor, header_bytes, total, 0, fs, 0, 0)
    return head + bytes((i * 7) & 255 for i in range(total - len(head)))


class Validate(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="rosetest")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def put(self, name, data):
        p = os.path.join(self.d, f"rose__{name}.rpg1")
        with open(p, "wb") as fh:
            fh.write(data)
        return p

    def test_valid(self):
        b = make_pack(1000)
        self.put("island", b)
        r = rose.prepare(self.d, "island")
        self.assertEqual((r["state"], r["size"], r["error"]), ("ready", 1000, None))
        self.assertEqual(open(r["path"], "rb").read(), b)

    def test_higher_minor_ok(self):
        self.put("m", make_pack(64, minor=9))
        self.assertEqual(rose.prepare(self.d, "m")["state"], "ready")

    def test_rejects(self):
        cases = {"magic": make_pack(64, magic=b"GSV1"), "major2": make_pack(64, major=2), "major0": make_pack(64, major=0),
                 "sizebig": make_pack(64, file_size=65), "sizesmall": make_pack(64, file_size=63),
                 "short": b"RPG1" + b"\0" * 10, "empty": b"", "hdr": make_pack(64, header_bytes=8),
                 "hdrbig": make_pack(64, header_bytes=65), "trailing": make_pack(64) + b"\0"}
        for k, data in cases.items():
            self.put("bad", data)
            r = rose.prepare(self.d, "bad")
            self.assertEqual(r["state"], "error", k)
            self.assertIsNone(r["path"], k)
            self.assertTrue(r["error"], k)

    def test_missing(self):
        r = rose.prepare(self.d, "nothere")
        self.assertEqual(r["state"], "error")
        self.assertIn("no rose__nothere.rpg1", r["error"])

    def test_names(self):
        for n in ("a", "birthisland", "x_1", "a" * 32):
            self.assertTrue(rose.valid_name(n), n)
        for n in ("", "A", "a" * 33, "a/b", "../x", "a.b", "a b", "a\n", "ä", "a-b", "..", None):
            self.assertFalse(rose.valid_name(n), repr(n))

    def test_test_scenario(self):
        f = os.path.join(self.d, "test_scenario.txt")
        self.assertEqual(rose.test_scenario(self.d), "")                       # no file: no selection
        for text, want in (("rosecombat\n", "rosecombat"), ("  roseparty  \r\nxx", "roseparty"), ("Rose Combat", ""), ("../x", ""), ("", ""), ("a" * 33, "")):
            with open(f, "w") as fh:
                fh.write(text)
            self.assertEqual(rose.test_scenario(self.d), want, repr(text))
        self.assertEqual(rose.test_scenario(None), "")

    def test_traversal(self):
        sub = os.path.join(self.d, "rose")
        os.makedirs(sub)
        with open(os.path.join(self.d, "rose__outside.rpg1"), "wb") as fh:
            fh.write(make_pack(64))
        for n in ("../rose__outside", "..", "../outside", "a/../../rose__outside", "/etc/passwd"):
            r = rose.prepare(sub, n)
            self.assertEqual(r["state"], "error", n)
            self.assertIsNone(r["path"])
        # a symlink inside the folder that points outside is refused
        try:
            os.symlink(os.path.join(self.d, "rose__outside.rpg1"), os.path.join(sub, "rose__link.rpg1"))
        except (OSError, NotImplementedError):
            return
        r = rose.prepare(sub, "link")
        self.assertEqual(r["state"], "error")

    def test_too_large(self):
        p = self.put("big", make_pack(64))
        old = rose.MAX_BYTES
        rose.MAX_BYTES = 32
        try:
            self.assertIn("too large", rose.prepare(self.d, "big")["error"])
        finally:
            rose.MAX_BYTES = old
        self.assertEqual(os.path.getsize(p), 64)

    def test_slot_public_hides_path(self):
        s = rose.new_slot()
        s["path"] = "/x"
        self.assertNotIn("path", rose.public(s))
        self.assertEqual(set(rose.public(s)), {"state", "req", "name", "size", "error"})


def live_test():
    import selftest as st
    http, wait_status, append, free_port = st.http, st.wait_status, st.append, st.free_port
    work = tempfile.mkdtemp(prefix="roselive")
    fails = []

    def check(ok, msg):
        print(("  ok   " if ok else "  FAIL ") + msg)
        if not ok:
            fails.append(msg)
        return ok

    pdir, rdir, logdir = (os.path.join(work, x) for x in ("pre", "rose", "log"))
    for d in (pdir, rdir, logdir):
        os.makedirs(d)
    with open(os.path.join(pdir, "valve__c1a0.bin"), "wb") as fh:
        fh.write(b"GSV1" + b"PACK" * 10)
    good = make_pack(300000)
    zone2 = make_pack(200000, minor=5)               # a second zone pack: the world switches zones by asking for another name (docs/design/rose_zones.md)
    for n, data in (("good", good), ("zone2", zone2), ("badmagic", make_pack(64, magic=b"XXXX")), ("badsize", make_pack(64, file_size=99))):
        with open(os.path.join(rdir, f"rose__{n}.rpg1"), "wb") as fh:
            fh.write(data)
    lg = os.path.join(logdir, "output_log_2026-11-12_10-00-00.txt")
    append(lg, "start")
    port = free_port()
    cmd = [sys.executable, "-I", os.path.join(HERE, "companion.py"), "--prebuilt-dir", pdir, "--vrchat-log-dir", logdir,
           "--cache-dir", os.path.join(work, "cache"), "--rose-dir", rdir, "--no-rose-find", "--world-allow", "wrld_ok", "--port", str(port)]
    proc = subprocess.Popen(cmd, stdout=open(os.path.join(work, "companion.out"), "w"), stderr=subprocess.STDOUT)

    def req(name, rid):
        append(lg, f"[GSVR-REQ] rose={name} req={rid}")
        s = wait_status(port, lambda s: s["rose"]["req"] == rid and s["rose"]["state"] in ("ready", "error"), 20)
        return s["rose"] if s else None
    try:
        s = wait_status(port, lambda s: True, 20)
        idle = {"state": "idle", "req": None, "name": None, "size": 0, "error": None}
        if not check(s is not None and {k: s.get("rose", {}).get(k) for k in idle} == idle
                     and isinstance(s["rose"].get("text"), str) and s["rose"].get("all_ready") is False
                     and not {"stage", "progress", "eta_s", "path"} & set(s["rose"]) and "rose_build" in s,
                     "status.json has the idle rose slot (+ text, all_ready, rose_build)"):
            return 1
        for k in ("models", "holo", "build", "world_ok", "ownership"):
            check(k in s, f"existing status key '{k}' still present")
        append(lg, "Joining wrld_other")
        wait_status(port, lambda s: s["world"] == "wrld_other", 10)
        append(lg, "[GSVR-REQ] rose=good req=g0")
        import time
        time.sleep(1.5)
        s = wait_status(port, lambda s: True, 5)
        check(s["rose"]["state"] == "idle", "request from a foreign world is ignored")
        check(http(port, "/pack/rose.rpg1")[0] == 403, "rose.rpg1 403 in a foreign world")
        append(lg, "Joining wrld_ok")
        wait_status(port, lambda s: s["world_ok"], 10)
        check(http(port, "/pack/rose.rpg1")[0] == 503, "rose.rpg1 503 before any request")
        r = req("good", "g1")
        check(r and r["state"] == "ready" and r["size"] == len(good) and r["name"] == "good", f"good pack ready ({r})")
        code, data = http(port, "/pack/rose.rpg1")
        check(code == 200 and data == good, "rose.rpg1 served byte-identical")
        results = []

        def getter():
            results.append(http(port, "/pack/rose.rpg1"))
        ts = [threading.Thread(target=getter) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        check(len(results) == 8 and all(c == 200 and d == good for c, d in results), "8 concurrent GETs all byte-identical")
        check(http(port, "/pack/rose.rpg1", host="evil.example")[0] == 421, "foreign Host -> 421")
        check(http(port, "/pack/rose.rpg1", method="POST")[0] == 405, "POST -> 405")
        for n, why in (("badmagic", "magic"), ("badsize", "fileSize")):
            r = req(n, "e_" + n)
            check(r and r["state"] == "error" and why in (r["error"] or ""), f"{n}: error ({r and r['error']})")
            check(http(port, "/pack/rose.rpg1")[0] == 503, f"{n}: not served")
        r = req("missing", "m1")
        check(r and r["state"] == "error", "missing file -> state error")
        check(http(port, "/pack/rose.rpg1")[0] == 404, "missing file -> 404")
        for i, n in enumerate(("..%2Fx", "../x", "A", "a" * 40)):
            r = req(n, f"t{i}")
            check(r and r["state"] == "error" and r["name"] is None, f"bad name {n!r} rejected ({r and r['error']})")
        r = req("good", "g2")
        check(r and r["state"] == "ready" and http(port, "/pack/rose.rpg1")[0] == 200, "slot recovers with a good request")
        # zone travel: switch packs on a new request, no restart; back and forth, each time byte-identical, name reported
        for i, (n, data) in enumerate((("zone2", zone2), ("good", good), ("zone2", zone2))):
            r = req(n, f"z{i}")
            code, got = http(port, "/pack/rose.rpg1")
            check(r and r["state"] == "ready" and r["name"] == n and r["size"] == len(data) and code == 200 and got == data,
                  f"zone switch {i} -> {n} served without restart ({r and r['size']} bytes)")
        check(proc.poll() is None, "companion still the same process after the zone switches")
    finally:
        proc.terminate()
        proc.wait(10)
        shutil.rmtree(work, ignore_errors=True)
    return 1 if fails else 0


if __name__ == "__main__":
    u = unittest.main(argv=[sys.argv[0]], exit=False, verbosity=1)
    rc = live_test()
    sys.exit(0 if u.result.wasSuccessful() and rc == 0 else 1)
