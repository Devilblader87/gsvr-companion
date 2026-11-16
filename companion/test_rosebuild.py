"""ROSE build manager (companion/rosebuild.py) with the stand-in converter rose_conv_stub (no game data, no real converter).

Run: cd companion && python3 -I -m unittest test_rosebuild     (a live companion part runs on a 127.0.0.1 ephemeral port)
"""
import json, os, shutil, subprocess, sys, tempfile, threading, time, unittest, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import rose, rosebuild, rose_conv_stub as stub      # noqa: E402

A, B, C = "birthisland", "adventurersplain", "zant"


def until(pred, timeout=10):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.01)
    return False


class Base(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="rosebuild")
        self.rdir = os.path.join(self.work, "rose")
        self.inst = os.path.join(self.work, "ROSE Online")
        os.makedirs(self.rdir)
        os.makedirs(self.inst)
        with open(os.path.join(self.inst, "data.idx"), "wb") as fh:
            fh.write(b"idx" * 10)
        stub.STEP_S, stub.FAIL, stub.HOLD, stub.NO_PART_CLEANUP = 0.001, set(), None, False
        stub.FAIL_AT_ONCE, stub.HOLD_FROM = False, 0
        self.addCleanup(setattr, rosebuild, "FAIL_RETRY_S", rosebuild.FAIL_RETRY_S)
        del stub.CALLS[:]
        self.logs, self.updates = [], []
        self.lk = threading.Lock()
        self.m = self.mk()

    def mk(self):
        return rosebuild.Manager(self.rdir, conv=stub, log=self.logs.append, on_update=self.on_update)

    def on_update(self, name, fields):
        with self.lk:
            self.updates.append((name, dict(fields)))

    def last(self, name):
        with self.lk:
            u = [f for n, f in self.updates if n == name]
        return u[-1] if u else None

    def tearDown(self):
        if stub.HOLD is not None:
            stub.HOLD.set()
        self.m.stop()
        shutil.rmtree(self.work, ignore_errors=True)

    def drop(self, name, data=None):
        with open(rosebuild.pack_path(self.rdir, name), "wb") as fh:
            fh.write(data or stub._pack_bytes(name, 512))


class NoInstall(Base):
    def test_hand_dropped_served(self):
        self.drop(A)
        self.assertEqual(self.m.pack_state(A), "fresh")
        r = self.m.request(A)
        self.assertEqual((r["state"], r["size"]), ("ready", 512))
        self.assertEqual(stub.CALLS, [])

    def test_missing_error_text(self):
        r = self.m.request(A)
        self.assertEqual(r["state"], "error")
        self.assertIn(rosebuild.NO_INSTALL, r["error"])
        self.assertIn("no rose__", r["error"])                 # the route keeps answering 404 for a missing file

    def test_bad_install(self):
        ok, why = self.m.set_install(os.path.join(self.work, "nothing"))
        self.assertFalse(ok)
        r = self.m.request(A)
        self.assertEqual(r["state"], "error")
        self.assertIn("not usable", r["error"])
        self.assertFalse(self.m.state()["install_ok"])

    def test_no_converter(self):
        m = rosebuild.Manager(self.rdir, conv=None, autoload=False, log=self.logs.append)
        self.assertEqual(m.set_install(self.inst)[0], False)
        self.drop(A)
        self.assertEqual(m.request(A)["state"], "ready")
        self.assertEqual(m.state()["packs"], {})
        self.assertIn("no rose__x1.rpg1", m.request("x1")["error"])


class Builds(Base):
    def test_on_demand_then_prebuild(self):
        self.m.set_install(self.inst, prebuild=False)
        self.assertEqual(self.m.pack_state(A), "missing")
        r = self.m.request(A)
        self.assertEqual(r["state"], "building")
        for k in ("stage", "progress", "eta_s"):
            self.assertIn(k, r)
        self.assertTrue(self.m.wait_idle(20))
        self.assertEqual(self.last(A)["state"], "ready")
        self.assertEqual(stub.CALLS[0], A)
        self.assertEqual(sorted(stub.CALLS), sorted(stub.PACKS))   # background prebuild of the rest after the first build
        sc = rosebuild.read_sidecar(self.rdir, A)
        self.assertEqual(sc["conv"], stub.pack_version(A))      # birthisland has a per-pack revision (PACK_REV)
        self.assertEqual(sc["install"], rosebuild.fingerprint(stub, self.inst))
        self.assertEqual(sc["bytes"], os.path.getsize(rosebuild.pack_path(self.rdir, A)))
        self.assertIn("T", sc["built"])
        st = self.m.state()
        self.assertTrue(all(p["state"] == "fresh" for p in st["packs"].values()))
        self.assertIsNone(st["current"])
        self.assertEqual(st["queue"], [])
        self.assertEqual((st["packs"][A]["label"], st["packs"][C]["label"]), ("Birth Island", C))
        json.dumps(st)
        txt = "\n".join(self.logs)
        for want in (f"[rose] build {A} started", f"[rose] build {A} done in", "% "):
            self.assertIn(want, txt)
        self.assertEqual(self.m.request(A)["state"], "ready")      # fresh -> served, no build
        self.assertEqual(stub.CALLS.count(A), 1)
        self.assertEqual([f for f in os.listdir(self.rdir) if f.endswith((".part", ".tmp"))], [])

    def test_progress_updates(self):
        stub.STEP_S = 0.02
        self.m.set_install(self.inst, prebuild=False)
        self.m.request(A)
        self.assertTrue(self.m.wait_idle(20))
        with self.lk:
            b = [f for n, f in self.updates if n == A and f["state"] == "building"]
        self.assertGreaterEqual(len(b), 3)
        self.assertEqual(sorted(p["progress"] for p in b), [p["progress"] for p in b])   # never goes back
        self.assertTrue({"vfs", "terrain", "objects"} <= {p["stage"] for p in b})

    def test_first_contact_order(self):
        """W10 FIRST LOAD: the background prebuild builds lobby, avatar, start zone first, then the rest in PACKS order"""
        self.assertEqual(rosebuild.start_zone(stub), A)
        self.assertEqual(rosebuild.first_contact(stub), ["login", "avatar", A])
        order = rosebuild.prebuild_order(stub, list(stub.PACKS))
        self.assertEqual(order[:3], ["login", "avatar", A])
        self.assertEqual(sorted(order), sorted(stub.PACKS))
        rest = [n for n in stub.PACKS if n not in ("login", "avatar", A)]
        self.assertEqual(order[3:], rest)
        self.assertEqual(rosebuild.warm_after(stub, "login"), ["avatar", A])
        self.assertEqual(rosebuild.warm_after(stub, "avatar"), [A])
        self.assertEqual(rosebuild.warm_after(stub, A), [])
        self.assertEqual(rosebuild.warm_after(stub, B), [])
        self.m.set_install(self.inst)
        self.assertTrue(self.m.wait_idle(20))
        self.assertEqual(stub.CALLS[:3], ["login", "avatar", A])
        self.assertEqual(sorted(stub.CALLS), sorted(stub.PACKS))

    def test_warm_up_after_lobby_request(self):
        """W10 FIRST LOAD: a request for the lobby pack queues avatar + start zone right behind it (built while the player is in the lobby);
        the avatar request queues the start zone; a zone request queues nothing; asking again does not reorder or log twice"""
        stub.HOLD = threading.Event()
        self.m.set_install(self.inst, prebuild=False)
        self.assertEqual(self.m.request("login")["state"], "building")
        self.assertTrue(until(lambda: self.m.cur is not None and self.m.cur["name"] == "login"))
        self.assertEqual(self.m.state()["queue"][:2], ["avatar", A])
        self.m.request("login")                                     # the world logs the request again: no change
        self.assertEqual("\n".join(self.logs).count("[rose] warm-up after login: avatar, " + A + " next"), 1)
        stub.HOLD.set()
        self.assertTrue(until(lambda: (self.last("login") or {}).get("state") == "ready"))
        self.assertTrue(self.m.wait_idle(30))
        self.assertEqual(stub.CALLS[:3], ["login", "avatar", A])
        self.assertEqual(self.m.pack_state(A), "fresh")
        self.assertEqual(self.m.request("avatar")["state"], "ready")    # built in the background meanwhile: served at once
        self.assertEqual(self.m.request(A)["state"], "ready")

    def test_warm_up_skips_fresh_and_zone_requests(self):
        self.m.set_install(self.inst, prebuild=False)
        self.m.request(A)                                           # a zone request: no warm-up
        self.assertNotIn("warm-up", "\n".join(self.logs))
        self.assertTrue(self.m.wait_idle(30))                       # (the first on-demand build starts the background prebuild of the rest)
        n0 = len(stub.CALLS)
        self.assertEqual(self.m.request("avatar")["state"], "ready")    # fresh: no build, no warm-up of the fresh start zone
        self.assertTrue(self.m.wait_idle(10))
        self.assertEqual(len(stub.CALLS), n0)

    def test_install_known_starts_prebuild(self):
        self.m.set_install(self.inst)
        self.assertTrue(self.m.wait_idle(20))
        self.assertEqual(sorted(stub.CALLS), sorted(stub.PACKS))
        self.assertEqual(self.updates, [])                       # background builds do not touch the slot

    def test_pack_rev_rebuilds_only_that_pack(self):
        """ROSE w8: bumping one PACK_REV entry makes only that pack stale (the others keep their sidecar version = VERSION)."""
        self.m.set_install(self.inst, prebuild=True)
        self.m.request(A)
        self.assertTrue(self.m.wait_idle(30))
        self.m.queue.clear()
        self.assertEqual({n: self.m.pack_state(n) for n in (A, B, C)}, {A: "fresh", B: "fresh", C: "fresh"})
        self.assertEqual(rosebuild.read_sidecar(self.rdir, B)["conv"], stub.VERSION)
        old = dict(stub.PACK_REV)
        try:
            stub.PACK_REV[A] = old.get(A, 0) + 1
            self.assertEqual({n: self.m.pack_state(n) for n in (A, B, C)}, {A: "stale", B: "fresh", C: "fresh"})
        finally:
            stub.PACK_REV.clear()
            stub.PACK_REV.update(old)
        self.assertEqual(self.m.pack_state(A), "fresh")

    def test_stale_rules(self):
        self.m.set_install(self.inst, prebuild=False)
        self.m.request(A)
        self.assertTrue(self.m.wait_idle(20))
        self.m.queue.clear()
        self.assertEqual(self.m.pack_state(A), "fresh")
        sc = rosebuild.read_sidecar(self.rdir, A)
        # other converter VERSION
        rosebuild.write_sidecar(self.rdir, A, dict(sc, conv="rv-old"))
        self.assertEqual(self.m.pack_state(A), "stale")
        rosebuild.write_sidecar(self.rdir, A, sc)
        self.assertEqual(self.m.pack_state(A), "fresh")
        # missing sidecar (hand-dropped pack while an install is configured)
        os.remove(rosebuild.sidecar_path(self.rdir, A))
        self.assertEqual(self.m.pack_state(A), "stale")
        rosebuild.write_sidecar(self.rdir, A, sc)
        # the install changed (patched VFS index: other size + mtime)
        idx = os.path.join(self.inst, "data.idx")
        with open(idx, "ab") as fh:
            fh.write(b"patch")
        os.utime(idx, (time.time() + 100, time.time() + 100))
        self.assertNotEqual(rosebuild.fingerprint(stub, self.inst), sc["install"])
        self.assertEqual(self.m.pack_state(A), "stale")
        n = len(stub.CALLS)
        self.assertEqual(self.m.request(A)["state"], "building")   # stale -> rebuilt on demand
        self.assertTrue(self.m.wait_idle(20))
        self.assertEqual(stub.CALLS[n], A)
        self.assertEqual(self.m.pack_state(A), "fresh")
        self.assertEqual(self.last(A)["state"], "ready")

    def test_fingerprint(self):
        f1 = rosebuild.fingerprint(stub, self.inst)
        self.assertEqual(f1, rosebuild.fingerprint(stub, self.inst))
        self.assertEqual(len(f1), 32)
        # moving the same install elsewhere keeps the fingerprint (relative paths)
        other = os.path.join(self.work, "copy")
        shutil.copytree(self.inst, other, copy_function=shutil.copy2)
        self.assertEqual(f1, rosebuild.fingerprint(stub, other))
        # fallback without install_files(): the *.idx files at the top of the install
        class NoIdx:
            install_extra = staticmethod(stub.install_extra)
        self.assertEqual(f1, rosebuild.fingerprint(NoIdx, self.inst))

    def test_fingerprint_loose_folders(self):
        """audit 6: install_extra (rose_conv: Xml + Sound stats, decoder) is part of the fingerprint: adding Sound -> stale"""
        self.m.set_install(self.inst, prebuild=False)
        self.m.request(A)
        self.assertTrue(self.m.wait_idle(20))
        self.m.queue.clear()
        self.assertEqual(self.m.pack_state(A), "fresh")
        os.makedirs(os.path.join(self.inst, "Sound"))
        with open(os.path.join(self.inst, "Sound", "a.wav"), "wb") as fh:
            fh.write(b"RIFF")
        self.assertEqual(self.m.pack_state(A), "stale")
        class Odd:
            @staticmethod
            def install_files(p):
                return ["data.idx"]

            @staticmethod
            def install_extra(p):
                return ["decoder ffmpeg"]
        class Odd2(Odd):
            @staticmethod
            def install_extra(p):
                return ["decoder miniaudio"]
        self.assertNotEqual(rosebuild.fingerprint(Odd, self.inst), rosebuild.fingerprint(Odd2, self.inst))

    def test_corrupt_fresh_pack_rebuilt(self):
        self.m.set_install(self.inst, prebuild=False)
        self.m.request(A)
        self.assertTrue(self.m.wait_idle(20))
        self.m.queue.clear()
        with open(rosebuild.pack_path(self.rdir, A), "r+b") as fh:
            fh.write(b"XXXX")
        self.assertEqual(self.m.request(A)["state"], "building")
        self.assertTrue(self.m.wait_idle(20))
        self.assertEqual(rose.prepare(self.rdir, A)["state"], "ready")

    def test_join(self):
        stub.HOLD = threading.Event()
        self.m.set_install(self.inst, prebuild=False)
        r1 = self.m.request(A)
        self.assertTrue(until(lambda: self.m.cur is not None))
        r2 = self.m.request(A)
        self.assertEqual((r1["state"], r2["state"]), ("building", "building"))
        self.assertEqual(self.m.state()["current"]["name"], A)
        stub.HOLD.set()
        self.assertTrue(until(lambda: (self.last(A) or {}).get("state") == "ready"))
        self.assertEqual(stub.CALLS.count(A), 1)

    def test_join_background_build(self):
        stub.HOLD = threading.Event()
        self.m.set_install(self.inst)                         # prebuild: the first PACKS entry runs in the background
        first = rosebuild.prebuild_order(stub, list(stub.PACKS))[0]     # W10: the lobby pack first
        self.assertTrue(until(lambda: self.m.cur is not None and self.m.cur["name"] == first))
        self.assertEqual(self.m.cur["kind"], "background")
        self.assertEqual(self.m.request(first)["state"], "building")
        self.assertEqual(self.m.cur["kind"], "demand")          # joined, not restarted
        stub.HOLD.set()
        self.assertTrue(until(lambda: (self.last(first) or {}).get("state") == "ready"))
        self.assertEqual(stub.CALLS.count(first), 1)

    def test_preempt_background(self):
        stub.HOLD = threading.Event()
        self.m.set_install(self.inst)
        first = rosebuild.prebuild_order(stub, list(stub.PACKS))[0]     # W10: the lobby pack first
        self.assertNotEqual(first, B)
        self.assertTrue(until(lambda: self.m.cur is not None and self.m.cur["name"] == first))
        self.assertTrue(os.path.exists(rosebuild.pack_path(self.rdir, first) + ".part"))
        self.assertEqual(self.m.request(B)["state"], "building")
        self.assertTrue(until(lambda: self.m.cur is not None and self.m.cur["name"] == B))   # B runs now
        st = self.m.state()
        self.assertEqual(st["queue"][0], first)                  # cancelled + requeued at the front
        self.assertFalse(os.path.exists(rosebuild.pack_path(self.rdir, first) + ".part"))
        self.assertIn(f"[rose] build {first} cancelled (pre-empted by {B}, requeued)", "\n".join(self.logs))
        stub.HOLD.set()
        self.assertTrue(self.m.wait_idle(30))
        self.assertEqual(stub.CALLS[:3], [first, B, first])
        self.assertEqual(sorted(set(stub.CALLS)), sorted(stub.PACKS))
        self.assertEqual(self.last(B)["state"], "ready")

    def test_preempt_on_demand_latest_wins(self):
        stub.HOLD = threading.Event()
        self.m.set_install(self.inst, prebuild=False)
        self.m.request(A)
        self.assertTrue(until(lambda: self.m.cur is not None and self.m.cur["name"] == A))
        self.m.request(B)                                        # the world travelled to another zone
        self.assertTrue(until(lambda: self.m.cur is not None and self.m.cur["name"] == B))
        self.assertEqual(self.m.state()["queue"][:1], [A])
        stub.HOLD.set()
        self.assertTrue(self.m.wait_idle(30))
        self.assertEqual(stub.CALLS[:3], [A, B, A])
        self.assertEqual(self.last(B)["state"], "ready")
        self.assertEqual(self.m.pack_state(A), "fresh")

    def test_no_preempt_past_half(self):
        """audit 4b: an on-demand build past 50 % is finished first; the new request runs next (no ping-pong restarts)"""
        stub.HOLD, stub.HOLD_FROM = threading.Event(), 6        # A holds at 60 %
        self.m.set_install(self.inst, prebuild=False)
        self.m.request(A)
        self.assertTrue(until(lambda: self.m.cur is not None and self.m.cur["progress"] >= 0.5))
        self.assertEqual(self.m.request(B)["state"], "building")
        self.assertEqual(self.m.request(A)["state"], "building")  # back again: joins A, B stays queued
        self.assertEqual(self.m.request(B)["state"], "building")
        self.assertFalse(self.m.cur["cancel"].is_set())
        self.assertEqual(self.m.cur["name"], A)
        self.assertEqual(self.m.state()["queue"][:1], [B])
        self.assertIn(f"[rose] {B} runs next: {A} is 60% done (not pre-empted)", "\n".join(self.logs))
        stub.HOLD.set()
        self.assertTrue(self.m.wait_idle(30))
        self.assertEqual(stub.CALLS[:2], [A, B])
        self.assertEqual(stub.CALLS.count(A), 1)
        self.assertEqual(self.last(B)["state"], "ready")

    def test_no_disk_io_under_lock(self):
        """audit 9: fingerprint / sidecar reads / prepare never run with the manager lock held"""
        bad = []
        m = self.m

        def chk(what):
            if m.cv._is_owned():
                bad.append(what)

        class Conv:
            VERSION, PACKS, BuildError, Cancelled = stub.VERSION, stub.PACKS, stub.BuildError, stub.Cancelled
            check_install = staticmethod(stub.check_install)
            build = staticmethod(stub.build)

            @staticmethod
            def install_files(p):
                chk("install_files")
                return stub.install_files(p)

            @staticmethod
            def install_extra(p):
                chk("install_extra")
                return stub.install_extra(p)
        m.conv = Conv
        orig_rs, orig_prep = rosebuild.read_sidecar, rose.prepare
        rosebuild.read_sidecar = lambda d, n: (chk("read_sidecar"), orig_rs(d, n))[1]
        rose.prepare = lambda d, n: (chk("prepare"), orig_prep(d, n))[1]
        try:
            m.set_install(self.inst)
            m.request(A)
            m.state()
            self.assertTrue(m.wait_idle(30))
            m.state()
            m.build_all(force=True)
            m.request(B)
            self.assertTrue(m.wait_idle(30))
            m.request(A)
            m.state()
        finally:
            rosebuild.read_sidecar, rose.prepare = orig_rs, orig_prep
        self.assertEqual(bad, [])

    def test_cancel_and_part_cleanup(self):
        stub.HOLD = threading.Event()
        stub.NO_PART_CLEANUP = True                              # a converter that leaves its .part behind
        self.m.set_install(self.inst, prebuild=False)
        self.m.request(A)
        part = rosebuild.pack_path(self.rdir, A) + ".part"
        self.assertTrue(until(lambda: os.path.exists(part)))
        self.m.cancel()
        self.assertTrue(self.m.wait_idle(10))
        self.assertFalse(os.path.exists(part))                   # removed by the manager
        self.assertEqual(self.m.state()["queue"], [])
        self.assertEqual(self.last(A)["state"], "error")
        self.assertIn("cancelled", self.last(A)["error"])
        self.assertEqual(self.m.pack_state(A), "missing")

    def test_leftover_parts_removed_at_start(self):
        for f in (f"rose__{A}.rpg1.part", f"rose__{B}.rpg1.json.tmp"):
            with open(os.path.join(self.rdir, f), "wb") as fh:
                fh.write(b"x")
        self.mk()
        self.assertEqual(os.listdir(self.rdir), [])

    def test_build_error(self):
        stub.FAIL = {A}
        self.m.set_install(self.inst, prebuild=False)
        self.m.request(A)
        self.assertTrue(self.m.wait_idle(20))
        u = self.last(A)
        self.assertEqual(u["state"], "error")
        self.assertIn(f"ROSE build of {A} failed: stub failure", u["error"])
        self.assertIn(f"[rose] build {A} failed: stub failure", "\n".join(self.logs))
        self.assertIn("stub failure", self.m.state()["packs"][A]["error"])
        self.assertFalse(os.path.exists(rosebuild.pack_path(self.rdir, A) + ".part"))
        self.assertEqual(self.m.pack_state(A), "missing")
        stub.FAIL = set()
        # audit 4a: no rebuild on every request: the error is answered again during the cool-down
        n = len(stub.CALLS)
        for _ in range(3):
            r = self.m.request(A)
            self.assertEqual(r["state"], "error")
            self.assertIn("stub failure", r["error"])
            self.assertIn("next try in", r["error"])
        self.assertTrue(self.m.wait_idle(5))
        self.assertEqual(len(stub.CALLS), n)
        rosebuild.FAIL_RETRY_S = 0.0                             # cool-down over
        self.assertEqual(self.m.request(A)["state"], "building")  # a new request retries
        self.assertTrue(self.m.wait_idle(20))
        self.assertEqual(self.last(A)["state"], "ready")
        self.assertIsNone(self.m.state()["packs"][A]["error"])

    def test_failed_retry_on_install_change_and_build_all(self):
        stub.FAIL = {A}
        self.m.set_install(self.inst, prebuild=False)
        self.m.request(A)
        self.assertTrue(self.m.wait_idle(20))
        self.assertEqual(self.m.request(A)["state"], "error")     # cool-down
        q = self.m.build_all()                                   # GUI "Build all" = explicit retry
        self.assertIn(A, q)
        self.assertTrue(self.m.wait_idle(30))
        self.assertEqual(stub.CALLS.count(A), 2)
        self.assertEqual(self.m.request(A)["state"], "error")     # failed again -> cool-down again
        self.assertNotIn(A, self.m._prebuild(self.m.gen))        # the background prebuild leaves it out too
        stub.FAIL = set()
        idx = os.path.join(self.inst, "data.idx")                # a patched install (other fingerprint) lifts the cool-down
        with open(idx, "ab") as fh:
            fh.write(b"patch")
        os.utime(idx, (time.time() + 50, time.time() + 50))
        self.assertEqual(self.m.request(A)["state"], "building")
        self.assertTrue(self.m.wait_idle(30))
        self.assertEqual(self.last(A)["state"], "ready")
        self.assertEqual(stub.CALLS.count(A), 3)

    def test_install_change_restarts_demand(self):
        stub.HOLD = threading.Event()
        self.m.set_install(self.inst, prebuild=False)
        self.m.request(A)
        self.assertTrue(until(lambda: self.m.cur is not None))
        other = os.path.join(self.work, "other")
        shutil.copytree(self.inst, other)
        self.assertTrue(self.m.set_install(other, prebuild=False)[0])
        stub.HOLD.set()
        self.assertTrue(self.m.wait_idle(20))
        self.assertEqual(self.last(A)["state"], "ready")
        self.assertEqual(stub.CALLS.count(A), 2)
        # clearing the install while a demand runs -> clear error on the slot
        stub.HOLD.clear()
        os.remove(rosebuild.pack_path(self.rdir, B))             # the prebuild built B already
        self.m.request(B)
        self.assertTrue(until(lambda: self.m.cur is not None))
        self.m.set_install(None)
        stub.HOLD.set()
        self.assertTrue(self.m.wait_idle(10))
        with self.lk:
            errs = [f["error"] for n, f in self.updates if n == B and f["state"] == "error"]
        self.assertTrue(errs and rosebuild.NO_INSTALL in errs[-1], errs)

    def test_build_all(self):
        self.assertEqual(self.m.build_all(), [])                 # no install: nothing
        self.m.set_install(self.inst, prebuild=False)
        q = self.m.build_all()
        self.assertEqual(sorted(q), sorted(stub.PACKS))
        self.assertTrue(self.m.wait_idle(30))
        self.assertEqual(self.m.build_all(), [])                 # everything fresh
        self.assertEqual(sorted(self.m.build_all(force=True)), sorted(stub.PACKS))
        self.assertTrue(self.m.wait_idle(30))
        self.assertEqual(len(stub.CALLS), 2 * len(stub.PACKS))


class Parallel(Base):
    """LOAD PACKAGE 5: two builds at once (child processes in the companion; here the in-process stub with parallel=2), never two zones,
    login + avatar side by side on a first join, then the likely zone (the last zone the world asked for, else the start zone)."""
    def mk(self):
        self.live, self.peak, self.zpeak = set(), 0, 0
        lk = threading.Lock()
        test = self

        def build(name, install, out, progress=None, cancel=None):
            with lk:
                test.live.add(name)
                test.peak = max(test.peak, len(test.live))
                test.zpeak = max(test.zpeak, sum(1 for n in test.live if rosebuild.is_zone(stub, n)))
            try:
                return stub.build(name, install, out, progress=progress, cancel=cancel)
            finally:
                with lk:
                    test.live.discard(name)

        class Conv:
            VERSION, PACKS, BuildError, Cancelled = stub.VERSION, stub.PACKS, stub.BuildError, stub.Cancelled
            pack_version = staticmethod(stub.pack_version)
            check_install = staticmethod(stub.check_install)
            install_files = staticmethod(stub.install_files)
            install_extra = staticmethod(stub.install_extra)
        Conv.build = staticmethod(build)
        self.conv = Conv
        return rosebuild.Manager(self.rdir, conv=Conv, log=self.logs.append, on_update=self.on_update, parallel=2)

    def running(self):
        with self.m.cv:
            return sorted(c["name"] for c in self.m.running)

    def test_defaults(self):
        self.assertEqual(rosebuild.Manager(self.rdir, conv=stub).parallel, 1)            # in-process: one at a time (GIL, converter module state)
        self.assertEqual(rosebuild.Manager(self.rdir, conv=stub, isolate=True).parallel, rosebuild.PARALLEL_MAX)
        self.assertEqual(rosebuild.PARALLEL_MAX, 2)
        self.assertEqual(self.m.parallel, 2)

    def test_first_join_builds_login_and_avatar_together(self):
        stub.HOLD = threading.Event()
        self.m.set_install(self.inst, prebuild=False)
        self.assertEqual(self.m.request("login")["state"], "building")
        self.assertTrue(until(lambda: self.running() == ["avatar", "login"]), self.running())
        st = self.m.state()
        self.assertEqual(st["current"]["name"], "login")                          # the on-demand one is shown first
        self.assertEqual(sorted(r["name"] for r in st["running"]), ["avatar", "login"])
        self.assertEqual(st["queue"][:1], [A])                                    # the start zone waits for a free worker
        self.assertEqual(self.m.request("avatar")["state"], "building")           # the world asks for the avatar pack: joins
        self.assertEqual(self.m.cur["kind"], "demand")
        stub.HOLD.set()
        self.assertTrue(self.m.wait_idle(30))
        self.assertEqual(sorted(stub.CALLS[:2]), ["avatar", "login"])
        self.assertEqual(stub.CALLS[2], A)
        self.assertEqual(stub.CALLS.count("login") + stub.CALLS.count("avatar"), 2)
        self.assertEqual((self.last("login")["state"], self.last("avatar")["state"]), ("ready", "ready"))
        self.assertLessEqual(self.peak, 2)
        self.assertEqual(self.zpeak, 1)                                            # never two zone builds at once

    def test_never_two_zones(self):
        self.m.set_install(self.inst)
        self.assertTrue(self.m.wait_idle(60))
        self.assertEqual(sorted(stub.CALLS), sorted(stub.PACKS))
        self.assertEqual(self.peak, 2)
        self.assertEqual(self.zpeak, 1)

    def test_likely_zone_is_the_last_requested_zone(self):
        self.assertIsNone(rosebuild.read_last_zone(self.rdir))
        self.assertEqual(self.m._zone(), A)                                      # nothing known: the start zone
        self.m.set_install(self.inst, prebuild=False)
        self.m.request(B)
        self.assertTrue(self.m.wait_idle(60))
        self.assertEqual(rosebuild.read_last_zone(self.rdir), B)
        self.m.request("login")                                                   # lobby / avatar requests never overwrite it
        self.assertTrue(self.m.wait_idle(60))
        self.assertEqual(rosebuild.read_last_zone(self.rdir), B)
        m2 = self.mk()                                                            # a restarted companion reads it back
        self.addCleanup(m2.stop)
        self.assertEqual(m2._zone(), B)
        self.assertEqual(rosebuild.warm_after(stub, "login", m2._zone()), ["avatar", B])
        self.assertEqual(rosebuild.prebuild_order(stub, list(stub.PACKS), B)[:3], ["login", "avatar", B])
        self.assertEqual(m2.state()["likely_zone"], B)
        self.assertEqual(rosebuild.likely_zone(stub, "login"), A)                # not a zone / unknown: the start zone
        self.assertEqual(rosebuild.likely_zone(stub, "nosuchzone"), A)
        with open(os.path.join(self.rdir, rosebuild.LAST_ZONE_FILE), "w") as fh:
            fh.write('{"zone": "../x"}')
        self.assertIsNone(rosebuild.read_last_zone(self.rdir))

    def test_first_join_warms_the_likely_zone(self):
        rosebuild.write_last_zone(self.rdir, C)
        self.m = self.mk()
        stub.HOLD = threading.Event()
        self.m.set_install(self.inst, prebuild=False)
        self.m.request("login")
        self.assertIn("[rose] warm-up after login: avatar, " + C + " next", "\n".join(self.logs))
        self.assertTrue(until(lambda: self.running() == ["avatar", "login"]), self.running())
        stub.HOLD.set()
        self.assertTrue(self.m.wait_idle(30))
        self.assertEqual(stub.CALLS[2], C)

    def test_demand_preempts_a_background_build_when_both_workers_are_busy(self):
        stub.HOLD = threading.Event()
        self.m.set_install(self.inst)                                             # background: login + avatar
        self.assertTrue(until(lambda: self.running() == ["avatar", "login"]), self.running())
        self.assertEqual(self.m.request(B)["state"], "building")
        self.assertTrue(until(lambda: B in self.running()), self.running())
        self.assertEqual(len(self.running()), 2)
        txt = "\n".join(self.logs)
        self.assertEqual(txt.count(f"(pre-empted by {B}, requeued)"), 1)         # exactly one background build gave way
        stub.HOLD.set()
        self.assertTrue(self.m.wait_idle(60))
        self.assertEqual(sorted(set(stub.CALLS)), sorted(stub.PACKS))
        self.assertEqual(self.last(B)["state"], "ready")
        self.assertEqual(self.zpeak, 1)

    def test_zone_demand_waits_for_a_zone_past_half(self):
        stub.HOLD, stub.HOLD_FROM = threading.Event(), 6
        self.m.set_install(self.inst, prebuild=False)
        self.m.request(A)
        self.assertTrue(until(lambda: self.m.cur is not None and self.m.cur["progress"] >= 0.5))
        self.m.request(B)                                                         # a free worker, but A is a zone past 50 %: B waits
        self.assertIn(f"[rose] {B} runs next: {A} is 60% done (not pre-empted)", "\n".join(self.logs))
        self.assertEqual(self.running(), [A])
        stub.HOLD.set()
        self.assertTrue(self.m.wait_idle(30))
        self.assertEqual(stub.CALLS[:2], [A, B])
        self.assertEqual(self.zpeak, 1)


class StatusShape(unittest.TestCase):
    def test_public_slot(self):
        s = rose.new_slot()
        self.assertEqual(set(rose.public(s)), {"state", "req", "name", "size", "error"})
        s.update(state="building", name=A, stage="terrain", progress=0.4, eta_s=12, path=None)
        self.assertEqual(set(rose.public(s)), {"state", "req", "name", "size", "error", "stage", "progress", "eta_s"})
        s.update(state="ready", path="/x", size=10)
        self.assertEqual(set(rose.public(s)), {"state", "req", "name", "size", "error"})

    def test_scrub_public_state(self):
        """audit 5: no user folder names in status.json"""
        sc = rosebuild.scrub
        self.assertEqual(sc("C:\\Users\\Jane Doe\\Games\\ROSE Online"), "<home>\\Games\\ROSE Online")
        self.assertEqual(sc("no data.idx in c:/users/bob/x (pick the ROSE client folder)"), "no data.idx in <home>/x (pick the ROSE client folder)")
        self.assertEqual(sc("/Users/someone/ROSE"), "<home>/ROSE")
        self.assertEqual(sc("/home/al/r"), "<home>/r")
        self.assertEqual(sc(os.path.join(os.path.expanduser("~"), "r")), os.path.join("<home>", "r"))
        self.assertEqual(sc("D:\\Games\\ROSE"), "D:\\Games\\ROSE")
        self.assertIsNone(sc(None))
        st = {"install": "C:\\Users\\Jane\\ROSE", "install_ok": False, "install_reason": "no data.idx in C:\\Users\\Jane\\ROSE",
              "conv": "v", "current": None, "queue": [], "packs": {A: {"state": "missing", "error": "x C:\\Users\\Jane\\ROSE\\data.idx"}}}
        pub = rosebuild.public_state(st)
        self.assertNotIn("Jane", json.dumps(pub))
        self.assertEqual(pub["install"], "<home>\\ROSE")
        self.assertEqual(st["install"], "C:\\Users\\Jane\\ROSE")                 # the GUI copy is untouched

    def test_summary(self):
        import companion
        packs = {n: {"state": "fresh"} for n in stub.PACKS}
        rb = {"install": "/r", "install_ok": True, "conv": "v", "current": None, "packs": packs}
        self.assertEqual(companion.rose_summary({"state": "idle"}, rb), {"text": "all ROSE zones ready", "all_ready": True})
        rb["packs"] = dict(packs, zant={"state": "stale"})
        r = companion.rose_summary({"state": "building", "name": "zant", "progress": 0.42}, rb)
        self.assertEqual(r, {"text": "building zant 42%", "all_ready": False})
        self.assertEqual(companion.rose_summary({"state": "idle"}, dict(rb, install=None))["text"], "ROSE folder not set")


def post(port, path, hdr=True):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST", data=b"",
                                 headers={"User-Agent": "gsvr-test", **({"X-GSVR-Rose": "1"} if hdr else {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, None


def get_state(port, hdr=True):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/rose/state",
                                 headers={"User-Agent": "gsvr-test", **({"X-GSVR-Rose": "1"} if hdr else {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, None


def real_format_install(root):
    """an install the REAL rose_conv.check_install accepts (data.idx + its stores + 3Ddata/Control/Xml), no game data"""
    import struct
    os.makedirs(root, exist_ok=True)
    stores = ("ROOT.VFS", "DATA.VFS")
    head = struct.pack("<III", 0, 0, len(stores))
    body = b"".join(struct.pack("<H", len(n) + 1) + n.encode() + b"\0" + struct.pack("<I", 0) for n in stores)
    with open(os.path.join(root, "data.idx"), "wb") as fh:
        fh.write(head + body)
    open(os.path.join(root, "DATA.VFS"), "wb").close()
    os.makedirs(os.path.join(root, "3Ddata", "Control", "Xml"))
    return root


class SlotRace(unittest.TestCase):
    """audit 3: a build that fails (or finishes) at once must not leave the "rose" slot stuck in building"""
    def mk_state(self, rb):
        import companion
        st = companion.State.__new__(companion.State)
        st.lock = threading.Lock()
        st.changed = threading.Condition(st.lock)
        st.rose, st.seq, st.world_ok = rose.new_slot(), 0, True
        st.rosebuild = rb
        return st

    def test_instant_update_before_slot_write(self):
        """deterministic: the build thread delivers "error" while request() is still returning (before the handler wrote
        the slot). Fixed handler: the update waits for the slot write and is applied; old handler: lost + stuck."""
        holder = {}

        class FakeRb:
            def request(self, name):
                t = threading.Thread(target=lambda: holder["st"].rose_update(name, {"state": "error", "size": 0, "path": None,
                                                                                    "error": "stub failure at once"}))
                t.start()
                t.join(0.3)                     # the old handler had no lock here: the update ran (and was dropped) now
                holder["t"] = t
                return {"state": "building", "size": 0, "error": None, "path": None, "stage": "queued", "progress": 0.0, "eta_s": -1}
        st = self.mk_state(FakeRb())
        holder["st"] = st
        st.handle("req", {"rose": A, "req": "r1"})
        holder["t"].join(5)
        self.assertEqual((st.rose["state"], st.rose["req"], st.rose["name"]), ("error", "r1", A))
        self.assertIn("stub failure", st.rose["error"])

    def test_stub_converter_fails_instantly(self):
        work = tempfile.mkdtemp(prefix="roserace")
        self.addCleanup(shutil.rmtree, work, True)
        rdir, inst = os.path.join(work, "rose"), os.path.join(work, "inst")
        os.makedirs(rdir)
        os.makedirs(inst)
        with open(os.path.join(inst, "data.idx"), "wb") as fh:
            fh.write(b"idx")
        old = stub.STEP_S, stub.FAIL, stub.FAIL_AT_ONCE, stub.HOLD
        stub.STEP_S, stub.FAIL, stub.FAIL_AT_ONCE, stub.HOLD = 0.0, {A, B}, True, None
        try:
            st = self.mk_state(None)
            st.rosebuild = rosebuild.Manager(rdir, conv=stub, log=lambda m: None, on_update=st.rose_update)
            self.addCleanup(st.rosebuild.stop)
            st.rosebuild.set_install(inst, prebuild=False)
            for i, n in enumerate((A, B, A)):
                rosebuild.FAIL_RETRY_S = 0.0 if i < 2 else 300.0
                st.handle("req", {"rose": n, "req": f"r{i}"})
                self.assertTrue(st.rosebuild.wait_idle(10))
                self.assertTrue(until(lambda: st.rose["state"] != "building", 5), st.rose)
                self.assertEqual((st.rose["state"], st.rose["req"]), ("error", f"r{i}"), st.rose)
                self.assertIn("stub failure", st.rose["error"])
        finally:
            stub.STEP_S, stub.FAIL, stub.FAIL_AT_ONCE, stub.HOLD = old
            rosebuild.FAIL_RETRY_S = 300.0


class Live(unittest.TestCase):
    """companion.py with GSVR_ROSE_CONV=stub + --rose-install: building -> ready -> served, POST /rose/*."""
    def test_live(self):
        import selftest as st
        work = tempfile.mkdtemp(prefix="rosebuildlive")
        self.addCleanup(shutil.rmtree, work, True)
        pdir, rdir, logdir, inst = (os.path.join(work, x) for x in ("pre", "rose", "log", "inst"))
        for d in (pdir, rdir, logdir, inst):
            os.makedirs(d)
        with open(os.path.join(pdir, "valve__c1a0.bin"), "wb") as fh:
            fh.write(b"GSV1" + b"PACK" * 10)
        with open(os.path.join(inst, "data.idx"), "wb") as fh:
            fh.write(b"idx")
        lg = os.path.join(logdir, "output_log_2026-11-14_10-00-00.txt")
        st.append(lg, "start")
        port = st.free_port()
        env = dict(os.environ, GSVR_ROSE_CONV="stub", GSVR_ROSE_STUB_STEP_S="0.15", HOME=work, APPDATA=work, USERPROFILE=work)
        cmd = [sys.executable, "-I", os.path.join(HERE, "companion.py"), "--prebuilt-dir", pdir, "--vrchat-log-dir", logdir,
               "--cache-dir", os.path.join(work, "cache"), "--rose-dir", rdir, "--port", str(port)]
        out = open(os.path.join(work, "companion.out"), "w")
        self.addCleanup(out.close)
        proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, env=env)

        def fin():
            proc.terminate()
            proc.wait(10)
        self.addCleanup(fin)
        s = st.wait_status(port, lambda s: True, 20)
        self.assertIsNotNone(s)
        self.assertEqual(s["rose"]["text"], "ROSE folder not set")
        self.assertFalse(s["rose"]["all_ready"])
        st.append(lg, f"[GSVR-REQ] rose={A} req=r1")
        s = st.wait_status(port, lambda s: s["rose"]["req"] == "r1", 10)
        self.assertEqual(s["rose"]["state"], "error")
        self.assertIn(rosebuild.NO_INSTALL, s["rose"]["error"])
        self.assertEqual(st.http(port, "/pack/rose.rpg1")[0], 404)
        self.assertEqual(post(port, "/rose/install?dir=x", hdr=False)[0], 405)   # needs the GUI header
        code, body = post(port, "/rose/install?dir=" + urllib.request.quote(os.path.join(work, "none")))
        self.assertEqual((code, body["ok"]), (200, False))
        code, body = post(port, "/rose/install?dir=" + urllib.request.quote(inst, safe=""))
        self.assertEqual((code, body["ok"]), (200, True))
        self.assertEqual(body["rose_build"]["install"], os.path.abspath(inst))
        # audit 5: status.json (any world reads it) has no user folder; the full state is GUI only (GET /rose/state + header)
        s = st.wait_status(port, lambda s: s["rose_build"]["install_ok"], 10)
        self.assertEqual(s["rose_build"]["install"], "<home>" + os.sep + "inst")
        self.assertNotIn(work, json.dumps(s))
        self.assertEqual(get_state(port, hdr=False)[0], 403)
        code, full = get_state(port)
        self.assertEqual((code, full["install"], full["install_ok"]), (200, os.path.abspath(inst), True))
        st.append(lg, f"[GSVR-REQ] rose={B} req=r2")
        s = st.wait_status(port, lambda s: s["rose"]["req"] == "r2" and s["rose"]["state"] == "building"
                           and s["rose"]["progress"] > 0, 30)
        self.assertIsNotNone(s, "building with progress seen")
        for k in ("stage", "progress", "eta_s", "text"):
            self.assertIn(k, s["rose"])
        self.assertTrue(s["rose"]["text"].startswith(f"building {B}"))
        self.assertEqual(st.http(port, "/pack/rose.rpg1")[0], 503)
        s = st.wait_status(port, lambda s: s["rose"]["req"] == "r2" and s["rose"]["state"] in ("ready", "error"), 60)
        self.assertEqual(s["rose"]["state"], "ready", s["rose"])
        self.assertNotIn("stage", s["rose"])
        code, data = st.http(port, "/pack/rose.rpg1")
        self.assertEqual((code, data), (200, stub._pack_bytes(B)))
        code, body = post(port, "/rose/build_all")
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        s = st.wait_status(port, lambda s: s["rose"]["all_ready"], 90)
        self.assertIsNotNone(s, "all packs built in the background")
        self.assertEqual(s["rose"]["text"], "all ROSE zones ready")
        self.assertEqual(set(s["rose_build"]["packs"]), set(stub.PACKS))
        fin()
        out.close()
        with open(os.path.join(work, "companion.out")) as fh:
            txt = fh.read()
        self.assertIn(f"[rose] build {B} started", txt)
        self.assertIn(f"[rose] build {B} done in", txt)

    def test_live_auto_find(self):
        """audit 1: no --rose-install, no --no-rose-find: the companion reads the GUI's gui.json (rose_install) and the auto
        search validates + sets it (the TypeError of load_config() without its settings file was swallowed before)"""
        import selftest as st
        import rosefind
        work = tempfile.mkdtemp(prefix="roseautofind")
        self.addCleanup(shutil.rmtree, work, True)
        pdir, rdir, logdir = (os.path.join(work, x) for x in ("pre", "rose", "log"))
        for d in (pdir, rdir, logdir):
            os.makedirs(d)
        with open(os.path.join(pdir, "valve__c1a0.bin"), "wb") as fh:
            fh.write(b"GSV1" + b"PACK" * 10)
        inst = real_format_install(os.path.join(work, "Games", "My ROSE"))
        env = dict(os.environ, GSVR_ROSE_CONV="stub", HOME=work, APPDATA=work, USERPROFILE=work)
        old = {k: os.environ.get(k) for k in ("HOME", "APPDATA")}
        os.environ["HOME"], os.environ["APPDATA"] = work, work
        try:
            gj = rosefind.settings_file()                        # the same path the child resolves with HOME / APPDATA = work
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertTrue(gj.startswith(work), gj)
        rosefind.save_config(gj, inst)
        st.append(os.path.join(logdir, "output_log_2026-11-14_10-00-00.txt"), "start")
        port = st.free_port()
        cmd = [sys.executable, "-I", os.path.join(HERE, "companion.py"), "--prebuilt-dir", pdir, "--vrchat-log-dir", logdir,
               "--cache-dir", os.path.join(work, "cache"), "--rose-dir", rdir, "--port", str(port), "--no-prewarm"]
        out = open(os.path.join(work, "companion.out"), "w")
        self.addCleanup(out.close)
        proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, env=env)

        def fin():
            proc.terminate()
            proc.wait(10)
        self.addCleanup(fin)
        self.assertIsNotNone(st.wait_status(port, lambda s: True, 20))
        got = None
        t0 = time.monotonic()
        while time.monotonic() - t0 < 20:
            code, full = get_state(port)
            if code == 200 and full.get("install_ok"):
                got = full
                break
            time.sleep(0.2)
        fin()
        out.close()
        with open(os.path.join(work, "companion.out")) as fh:
            txt = fh.read()
        self.assertIsNotNone(got, txt[-2000:])
        self.assertEqual(os.path.normcase(got["install"]), os.path.normcase(os.path.abspath(inst)))
        self.assertNotIn("install search failed", txt)
        self.assertIn("[rose] install: ", txt)


if __name__ == "__main__":
    unittest.main()
