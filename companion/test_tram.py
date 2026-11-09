"""c0a0 tram pack route (/pack/tram.bin, tools/pack.py --only-model, companion/companion.py tram_slot).

Run: python3 -I companion/test_tram.py   (unit tests + live companion tests; the live part needs input/Half-Life with valve/maps/c0a0.bsp,
or GSVR_INPUT=<dir containing Half-Life>; it is skipped without).
"""
import os, shutil, struct, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import builder                                       # noqa: E402


def input_root():
    for d in (os.environ.get("GSVR_INPUT"), os.path.join(REPO, "input"), os.path.expanduser("~/dev/decomp/half-life/input")):
        if d and os.path.isfile(os.path.join(d, "Half-Life", "valve", "maps", "c0a0.bsp")):
            return os.path.join(d, "Half-Life")
    return None


def rb(path):
    with open(path, "rb") as fh:
        return fh.read()


def tiny_gsv3(wv, wi):
    """Smallest GSV3 that validate_pack can judge: 3 vertices, 1 triangle, 8x8 atlas, 1x1 lightmap, MODL only."""
    v, i, aw, ah, lw, lh = 3, 3, 8, 8, 1, 1
    body = (v * 11 + i) * 4 + aw * ah * 4 + lw * lh * 4
    dir_off = 64 + body
    head = struct.pack("<4s15i", b"GSV3", 64, v, i, 0, aw, ah, 1, lw, lh, 0, 0, 0, wv, wi, dir_off)
    return head + bytes(body) + struct.pack("<i", 1) + struct.pack("<4s3i", b"MODL", dir_off + 20, 0, 0)


def modl_records(data):
    """GSV3 bytes -> (header ints, [record int tuples]) of the MODL section."""
    dir_off, = struct.unpack_from("<i", data, 60)
    n, = struct.unpack_from("<i", data, dir_off)
    for k in range(n):
        tag, off, nbytes, cnt = struct.unpack_from("<4s3i", data, dir_off + 4 + 16 * k)
        if tag == b"MODL":
            return struct.unpack_from("<4s15i", data, 0), [struct.unpack_from("<24i", data, off + 96 * r) for r in range(cnt)]
    return None, []


class Validate(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="tramtest")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def check(self, data):
        p = os.path.join(self.d, "t.bin")
        with open(p, "wb") as fh:
            fh.write(data)
        return builder.validate_pack(p)

    def test_empty_world_accepted(self):
        data = tiny_gsv3(0, 0)
        self.assertEqual(self.check(data), len(data))

    def test_world_counts_still_checked(self):
        for wv, wi in ((4, 0), (0, 6), (-1, 0), (0, 1)):
            with self.assertRaises(RuntimeError):
                self.check(tiny_gsv3(wv, wi))


def build_tram(game_root, out, *extra):
    return subprocess.run([sys.executable, "-I", os.path.join(REPO, "tools", "pack.py"), "--bsp",
                           os.path.join(game_root, "valve", "maps", "c0a0.bsp"), "--game-root", game_root, "--mod", "valve",
                           "--only-model", "train", *extra, "-q", out], capture_output=True, text=True)


class RealPack(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = input_root()
        if not cls.root:
            raise unittest.SkipTest("no input/Half-Life with c0a0.bsp")
        cls.d = tempfile.mkdtemp(prefix="tramreal")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.d, ignore_errors=True)

    def test_pack(self):
        out = os.path.join(self.d, "x.bin")
        r = build_tram(self.root, out, "--max-tex", "256")
        self.assertEqual(r.returncode, 0, r.stderr)
        data = rb(out)
        self.assertEqual(builder.validate_pack(out), len(data))
        self.assertLess(len(data), 1 << 20)
        head, recs = modl_records(data)
        self.assertEqual(len(recs), 2)                                  # world + the tram
        w, t = recs[0], recs[1]
        self.assertEqual((w[3], w[6], w[8]), (0, 0, 0))                 # empty world: no vertices, render or collide indices
        self.assertEqual(struct.unpack_from("<2i", data, 52), (0, 0))   # worldVertexCount, worldIndexCount
        self.assertEqual(t[0], 12)                                      # bsp model *12
        self.assertGreater(t[6], 0)                                     # render indices
        self.assertTrue(t[9] & 1)
        _m, _h, v, i, _s, aw, ah, mips, lw, lh, e = head[:11]
        ents = data[64 + (v * 11 + i) * 4 + sum((aw >> l) * (ah >> l) * 4 for l in range(mips)) + lw * lh * 4:][:e].decode("latin-1")
        self.assertIn('"targetname" "train"', ents)
        self.assertEqual(ents.count("{"), 2)

    def test_selectors(self):
        a, b = os.path.join(self.d, "a.bin"), os.path.join(self.d, "b.bin")
        build_tram(self.root, a)
        build_tram(self.root, b)
        self.assertEqual(rb(a), rb(b))      # deterministic
        r = subprocess.run([sys.executable, "-I", os.path.join(REPO, "tools", "pack.py"), "--bsp",
                            os.path.join(self.root, "valve", "maps", "c0a0.bsp"), "--game-root", self.root, "--mod", "valve",
                            "--only-model", "*12", "-q", os.path.join(self.d, "c.bin")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(rb(os.path.join(self.d, "c.bin")), rb(a))
        r = subprocess.run([sys.executable, "-I", os.path.join(REPO, "tools", "pack.py"), "--bsp",
                            os.path.join(self.root, "valve", "maps", "c0a0.bsp"), "--game-root", self.root, "--mod", "valve",
                            "--only-model", "nosuch", "-q", os.path.join(self.d, "d.bin")], capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)


def tram_test(work, game_root):
    """Live companion: source install (route, cache, 404 for a mod set without valve, Host / method checks) and --prebuilt-dir."""
    import selftest as st
    check, http, wait_status, free_port = st.check, st.http, st.wait_status, st.free_port
    print("[tram] /pack/tram.bin")
    logdir = os.path.join(work, "vrclog_tram")
    shutil.rmtree(logdir, ignore_errors=True)
    os.makedirs(logdir)
    st.append(os.path.join(logdir, "output_log_2026-11-09_10-00-00.txt"), "start")

    def run(extra, cache):
        port = free_port()
        cmd = [sys.executable, "-I", os.path.join(HERE, "companion.py"), "--no-prewarm", "--vrchat-log-dir", logdir,
               "--cache-dir", cache, "--port", str(port)] + extra
        proc = subprocess.Popen(cmd, stdout=open(os.path.join(work, "companion_tram.out"), "w"), stderr=subprocess.STDOUT)
        return proc, port

    if not game_root:
        print("  (no Half-Life install: live part skipped)")
        return
    cache = os.path.join(work, "cache_tram")
    shutil.rmtree(cache, ignore_errors=True)
    proc, port = run(["--game-root", game_root, "--mods", "valve"], cache)
    try:
        check(wait_status(port, lambda s: True, 20) is not None, "companion up (source install)")
        code, data = http(port, "/pack/tram.bin", timeout=120)
        head, recs = modl_records(data) if code == 200 else (None, [])
        check(code == 200 and data[:4] == b"GSV3" and len(data) < 1 << 20, f"tram.bin served ({code}, {len(data)} bytes < 1 MB)")
        check(len(recs) == 2 and recs[1][6] > 0 and recs[0][6] == 0, "empty world + exactly one brush model with render tris")
        code2, data2 = http(port, "/pack/tram.bin")
        check(code2 == 200 and data2 == data, "second request identical (cache hit)")
        check(http(port, "/pack/tram.bin", host="evil.example")[0] == 421, "foreign Host -> 421")
        check(http(port, "/pack/tram.bin", method="POST")[0] == 405, "POST -> 405")
        check(st.status(port)["state"] == "idle", "map slot untouched")
        tmp = os.path.join(work, "tram_prebuilt")
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp)
        with open(os.path.join(tmp, "valve__c0a0_tram.bin"), "wb") as fh:
            fh.write(data)
    finally:
        proc.terminate()
        proc.wait(10)
    # a mod set without valve: no tram
    proc, port = run(["--game-root", game_root, "--mods", "cstrike"], os.path.join(work, "cache_tram2"))
    try:
        wait_status(port, lambda s: True, 20)
        check(http(port, "/pack/tram.bin")[0] == 404, "valve not allowed -> 404")
    finally:
        proc.terminate()
        proc.wait(10)
    # --world-allow: a world that is not allowed gets nothing
    proc, port = run(["--game-root", game_root, "--mods", "valve", "--world-allow", "wrld_00000000-0000-0000-0000-000000000000"], cache)
    try:
        wait_status(port, lambda s: True, 20)
        check(http(port, "/pack/tram.bin")[0] == 403, "no allowed world joined -> 403")
    finally:
        proc.terminate()
        proc.wait(10)
    # --prebuilt-dir
    proc, port = run(["--prebuilt-dir", tmp], os.path.join(work, "cache_tram3"))
    try:
        wait_status(port, lambda s: True, 20)
        code, d3 = http(port, "/pack/tram.bin")
        check(code == 200 and d3 == data, "prebuilt mode serves valve__c0a0_tram.bin byte-identical")
    finally:
        proc.terminate()
        proc.wait(10)
    os.remove(os.path.join(tmp, "valve__c0a0_tram.bin"))
    with open(os.path.join(tmp, "valve__c1a0.bin"), "wb") as fh:
        fh.write(b"GSV1" + b"PACK" * 10)
    proc, port = run(["--prebuilt-dir", tmp], os.path.join(work, "cache_tram4"))
    try:
        wait_status(port, lambda s: True, 20)
        check(http(port, "/pack/tram.bin")[0] == 404, "prebuilt without the tram pack -> 404")
    finally:
        proc.terminate()
        proc.wait(10)


if __name__ == "__main__":
    r = unittest.main(argv=[sys.argv[0]], exit=False, verbosity=1)
    import selftest
    work = os.path.join(REPO, "work", "selftest")
    os.makedirs(work, exist_ok=True)
    tram_test(work, input_root())
    ok = r.result.wasSuccessful() and not selftest.FAILS
    print("RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
