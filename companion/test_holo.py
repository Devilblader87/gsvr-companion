"""Map hologram bundle route (/pack/hologram.bin, companion/holo.py, tools/previewpack.py).

Run: python3 -I companion/test_holo.py [--sabotage]   (unit tests + live companion tests; the live source-install part needs
input/Half-Life, or GSVR_INPUT=<dir containing Half-Life>; it is skipped without). Also run by companion/selftest.py (holo_test).
"""
import os, shutil, struct, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, "tools"))
import holo, previewpack as pp                     # noqa: E402


def synthetic_bundle(n=2):
    """A valid GSPV from synthetic geometry (no game files)."""
    maps = []
    for k in range(n):
        pos = [(-5.0, 0.0, -5.0), (5.0, 0.0, -5.0), (5.0, 0.0, 5.0), (-5.0, 0.0, 5.0), (0.0, 3.0, 0.0)]
        tris = [(0, 1, 2), (0, 2, 3), (0, 1, 4), (1, 2, 4)]
        maps.append(pp.MapPreview(f"map{k}", pos, [0.5] * 5, tris, 0, (5.0, 1.5, 5.0), (0.0, 0.0, 0.0)))
    return pp.pack_bundle(maps)


class Validate(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="holotest")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def write(self, data, name="b.gspv"):
        p = os.path.join(self.d, name)
        with open(p, "wb") as fh:
            fh.write(data)
        return p

    def test_valid(self):
        b = synthetic_bundle(3)
        self.assertEqual(holo.validate_hologram(self.write(b)), len(b))
        self.assertEqual(len(pp.parse_bundle(b)), 3)

    def test_rejects(self):
        b = synthetic_bundle(2)
        for what, data in (("empty", b""), ("magic", b"GSM1" + b[4:]), ("truncated", b[:-4]), ("trailing", b + b"\0\0\0\0"),
                           ("version", b[:4] + struct.pack("<i", 9) + b[8:]),
                           ("dir offset", b[:32 + 44] + struct.pack("<i", 4) + b[32 + 48:]),
                           ("tri count", b[:32 + 36] + struct.pack("<i", 99999) + b[32 + 40:])):
            with self.assertRaises(RuntimeError, msg=what):
                holo.validate_hologram(self.write(data))
            if data[:4] == b"GSPV":
                with self.assertRaises(ValueError, msg=what):
                    pp.parse_bundle(data)

    def test_parse_maps(self):
        self.assertEqual(holo.parse_maps("c1a0,C1A1.bsp,../x,,c1a0,a b"), ["c1a0", "c1a1"])
        self.assertEqual(holo.parse_maps(""), [])
        self.assertEqual(len(holo.parse_maps(",".join(f"m{i}" for i in range(500)))), holo.MAX_MAPS)


def input_root():
    for d in (os.environ.get("GSVR_INPUT"), os.path.join(REPO, "input"), os.path.expanduser("~/dev/decomp/half-life/input")):
        if d and os.path.isdir(os.path.join(d, "Half-Life", "valve", "maps")):
            return os.path.join(d, "Half-Life")
    return None


def holo_test(work, game_root, sabotage=False):
    """Live companion: prebuilt mode (<mod>__hologram.gspv), source-install mode (real converter, cache hit, errors), world gate."""
    import selftest as st
    check, http, wait_status, append, free_port = st.check, st.http, st.wait_status, st.append, st.free_port
    print("[holo] map hologram bundle /pack/hologram.bin" + ("  (SABOTAGE: converter writes junk)" if sabotage else ""))
    # 1) --prebuilt-dir
    pdir = os.path.join(work, "holo_prebuilt")
    shutil.rmtree(pdir, ignore_errors=True)
    os.makedirs(pdir)
    with open(os.path.join(pdir, "valve__c1a0.bin"), "wb") as fh:
        fh.write(b"GSV1" + b"PACK" * 10)                       # makes 'valve' an allowed mod (the pack itself is never served here)
    bundle = synthetic_bundle(3)
    if sabotage:
        bundle = bundle[:-8]
    with open(os.path.join(pdir, "valve__hologram.gspv"), "wb") as fh:
        fh.write(bundle)
    logdir = os.path.join(work, "vrclog_holo")
    shutil.rmtree(logdir, ignore_errors=True)
    os.makedirs(logdir)
    lg = os.path.join(logdir, "output_log_2026-11-02_10-00-00.txt")
    append(lg, "start")
    port = free_port()
    cmd = [sys.executable, "-I", os.path.join(HERE, "companion.py"), "--prebuilt-dir", pdir, "--vrchat-log-dir", logdir,
           "--cache-dir", os.path.join(work, "cache_holo_pb"), "--port", str(port)]
    proc = subprocess.Popen(cmd, stdout=open(os.path.join(work, "companion_holo_pb.out"), "w"), stderr=subprocess.STDOUT)
    try:
        s = wait_status(port, lambda s: True, 20)
        if not check(s is not None and s.get("holo", {}).get("state") == "idle", "status.json has the holo slot (idle)"):
            return
        check(http(port, "/pack/hologram.bin")[0] == 503, "hologram.bin 503 before any request")
        append(lg, "[GSVR-REQ] holo=valve maps=c1a0,c1a1 req=h1")
        s = wait_status(port, lambda s: s["holo"]["req"] == "h1" and s["holo"]["state"] in ("ready", "error"), 20)
        if sabotage:
            check(s is not None and s["holo"]["state"] == "error",
                  f"prebuilt: a truncated bundle is rejected ({s and s['holo']['error']})")
            check(http(port, "/pack/hologram.bin")[0] == 503, "after a rejected bundle, hologram.bin is not served")
        elif check(s is not None and s["holo"]["state"] == "ready", f"prebuilt bundle ready ({s and s['holo']['error']})"):
            code, data = http(port, "/pack/hologram.bin")
            check(code == 200 and data == bundle and s["holo"]["size"] == len(bundle),
                  "prebuilt bundle served byte-identical, status.holo.size matches")
            check(http(port, "/pack/hologram.bin", host="evil.example")[0] == 421, "hologram.bin with a foreign Host -> 421")
            check(http(port, "/pack/hologram.bin", method="POST")[0] == 405, "POST hologram.bin -> 405")
        if sabotage:
            raise StopIteration
        append(lg, "[GSVR-REQ] holo=svencoop maps=a req=h2")
        s = wait_status(port, lambda s: s["holo"]["req"] == "h2" and s["holo"]["state"] in ("ready", "error"), 20)
        check(s is not None and s["holo"]["state"] == "error" and "not allowed" in (s["holo"]["error"] or ""),
              f"prebuilt: a mod without packs -> not allowed ({s and s['holo']['error']})")
        check(http(port, "/pack/hologram.bin")[0] == 503, "after a failed request hologram.bin is not served")
        check(s["state"] == "idle", "map slot untouched by hologram requests")
    except StopIteration:
        pass
    finally:
        proc.terminate()
        proc.wait(10)
    # 2) source install: the real converter
    if not game_root or not os.path.isdir(os.path.join(game_root, "valve", "maps")):
        print("  (no Half-Life install: live converter part skipped)")
        return
    logdir = os.path.join(work, "vrclog_holo2")
    shutil.rmtree(logdir, ignore_errors=True)
    os.makedirs(logdir)
    lg = os.path.join(logdir, "output_log_2026-11-02_11-00-00.txt")
    append(lg, "start")
    cache = os.path.join(work, "cache_holo")
    shutil.rmtree(cache, ignore_errors=True)
    port = free_port()
    cmd = [sys.executable, "-I", os.path.join(HERE, "companion.py"), "--no-prewarm", "--game-root", game_root, "--mods", "valve",
           "--vrchat-log-dir", logdir, "--cache-dir", cache, "--port", str(port)]
    if sabotage:
        cmd += ["--holo-cmd", "{python} -I -c \"open(r'{out}','wb').write(b'GSPV'+bytes(28)+b'JUNK'*256)\" {bsps}"]
    proc = subprocess.Popen(cmd, stdout=open(os.path.join(work, "companion_holo.out"), "w"), stderr=subprocess.STDOUT)
    try:
        s = wait_status(port, lambda s: True, 20)
        check(s is not None, "companion up (source install)")
        append(lg, "[GSVR-REQ] holo=valve maps=c1a0,c1a1,c1a2,nosuchmap req=s1")
        s = wait_status(port, lambda s: s["holo"]["req"] == "s1" and s["holo"]["state"] in ("ready", "error"), 120)
        h = s and s["holo"]
        if sabotage:
            check(h and h["state"] == "error", f"junk converter output rejected ({h and h['error']})")
            return
        if check(h and h["state"] == "ready", f"real bundle ready ({h and (h['state'], h['error'], h['seconds'])})"):
            code, data = http(port, "/pack/hologram.bin")
            check(code == 200 and len(data) == h["size"], f"hologram.bin served, {len(data)} bytes = status.holo.size")
            maps = pp.parse_bundle(data)
            check([m["name"] for m in maps] == ["c1a0", "c1a1", "c1a2"] and h["maps"] == 3,
                  "three installed maps in request order, the missing one left out")
            check(all(500 < m["T"] <= pp.HARD_TRIS for m in maps), "triangle counts within (500, cap]: " + str([m["T"] for m in maps]))
            check(len(data) < 4 * 1024 * 1024, "bundle of three small chapter maps < 4 MB")
            append(lg, "[GSVR-REQ] holo=valve maps=c1a0,c1a1,c1a2,nosuchmap req=s2")
            s = wait_status(port, lambda s: s["holo"]["req"] == "s2" and s["holo"]["state"] == "ready", 30)
            check(s is not None and s["holo"]["seconds"] < 0.5 and s["holo"]["key"] == h["key"], "second identical request is a cache hit")
        append(lg, "[GSVR-REQ] holo=valve maps=nosuchmap req=s3")
        s = wait_status(port, lambda s: s["holo"]["req"] == "s3" and s["holo"]["state"] in ("ready", "error"), 30)
        check(s is not None and s["holo"]["state"] == "error" and "none of" in (s["holo"]["error"] or ""),
              f"no installed map -> error ({s and s['holo']['error']})")
        append(lg, "[GSVR-REQ] holo=cstrike maps=de_dust2 req=s4")
        s = wait_status(port, lambda s: s["holo"]["req"] == "s4" and s["holo"]["state"] in ("ready", "error"), 30)
        check(s is not None and s["holo"]["state"] == "error" and "not allowed" in (s["holo"]["error"] or ""),
              "mod without ownership -> not allowed")
        append(lg, "[GSVR-REQ] holo=valve maps=../../etc/passwd req=s5")
        s = wait_status(port, lambda s: s["holo"]["req"] == "s5" and s["holo"]["state"] in ("ready", "error"), 30)
        check(s is not None and s["holo"]["state"] == "error", "path-like map names are refused")
        check(s["state"] == "idle", "map slot untouched")
    finally:
        proc.terminate()
        proc.wait(10)


if __name__ == "__main__":
    sab = "--sabotage" in sys.argv
    r = unittest.main(argv=[sys.argv[0]], exit=False, verbosity=1)
    import selftest
    work = os.path.join(REPO, "work", "selftest")
    os.makedirs(work, exist_ok=True)
    holo_test(work, input_root(), sab)
    ok = r.result.wasSuccessful() and not selftest.FAILS
    print("RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
