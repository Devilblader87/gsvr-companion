"""ROSE builds in a CHILD process (rosebuild.py isolate=True, `gsvr_main.py --run-rose-build`) with the stand-in converter
rose_conv_stub, the cached state() for the HTTP path, quiet aborted connections (companion.py) and the GUI's slow-poll / ROSE
status line (gui.py). Maintainer 2026-11-14: a ROSE build in a thread starved the HTTP server (status.json 2+ s, the GUI flipped
between "Running" and "Starting", ConnectionAbortedError tracebacks).

Run: cd companion && python3 -I -m unittest test_rosebuild_child
"""
import io, json, os, shutil, socket, statistics, subprocess, sys, tempfile, threading, time, unittest, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import rose, rosebuild, rose_conv_stub as stub      # noqa: E402

A, B = "birthisland", "adventurersplain"


def until(pred, timeout=10):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.01)
    return False


def gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


class Parse(unittest.TestCase):
    def test_parse_child_line(self):
        p = rosebuild.parse_child_line
        self.assertEqual(p("@progress 0.4200 terrain and objects\n"), ("progress", 0.42, "terrain and objects"))
        self.assertEqual(p(b"@progress 1.5 done\r\n"), ("progress", 1.0, "done"))         # clamped
        self.assertEqual(p("@progress -1 x"), ("progress", 0.0, "x"))
        self.assertEqual(p("@progress 0.1"), ("progress", 0.1, ""))
        self.assertIsNone(p("@progress nan x"))
        self.assertIsNone(p("@progress abc x"))
        self.assertEqual(p('@result {"conv": "v", "result": {"bytes": 3}}'), ("result", {"conv": "v", "result": {"bytes": 3}}))
        self.assertEqual(p('@error {"error": "boom", "cancelled": false}'), ("error", {"error": "boom", "cancelled": False}))
        self.assertIsNone(p("@result [1]"))
        self.assertIsNone(p("@result {broken"))
        self.assertIsNone(p("atlas: 12 tiles"))
        self.assertIsNone(p(""))

    def test_argv_and_flags(self):
        argv = rosebuild.child_argv(A, "/i", "/o.rpg1")
        self.assertEqual(argv[-4:], [rosebuild.CHILD_FLAG, A, "/i", "/o.rpg1"])
        self.assertIn("-I", argv)
        self.assertTrue(argv[-5].endswith("gsvr_main.py"))
        kw = rosebuild.child_popen_kwargs()
        if sys.platform == "win32":
            self.assertEqual(kw["creationflags"], 0x00004000 | 0x08000000)       # BELOW_NORMAL + CREATE_NO_WINDOW
        else:
            self.assertEqual(kw, {})                                              # the child calls os.nice itself
        self.assertEqual(rosebuild.conv_env_value(stub), "stub")
        self.assertIsNone(rosebuild.conv_env_value(None))

    def test_default_isolation(self):
        self.assertFalse(rosebuild.Manager(None, conv=stub).isolate)              # explicit converter (tests): in-process
        old = os.environ.pop("GSVR_ROSE_INPROC", None)
        try:
            self.assertTrue(rosebuild.Manager(None, conv=None, autoload=False).isolate)     # the companion: child process
            os.environ["GSVR_ROSE_INPROC"] = "1"
            self.assertFalse(rosebuild.Manager(None, conv=None, autoload=False).isolate)
        finally:
            os.environ.pop("GSVR_ROSE_INPROC", None)
            if old is not None:
                os.environ["GSVR_ROSE_INPROC"] = old


class Child(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="rosechild")
        self.rdir = os.path.join(self.work, "rose")
        self.inst = os.path.join(self.work, "ROSE Online")
        os.makedirs(self.rdir)
        os.makedirs(self.inst)
        with open(os.path.join(self.inst, "data.idx"), "wb") as fh:
            fh.write(b"idx" * 10)
        del stub.CALLS[:]
        self.logs, self.updates = [], []
        self.lk = threading.Lock()
        self.addCleanup(setattr, rosebuild, "FAIL_RETRY_S", rosebuild.FAIL_RETRY_S)
        self.m = self.mk()

    def mk(self, **env):
        e = {"GSVR_ROSE_STUB_STEP_S": "0.01"}
        e.update(env)
        return rosebuild.Manager(self.rdir, conv=stub, isolate=True, child_env=e, log=self.logs.append,
                                 on_update=self.on_update)

    def on_update(self, name, fields):
        with self.lk:
            self.updates.append((name, dict(fields)))

    def last(self, name):
        with self.lk:
            u = [f for n, f in self.updates if n == name]
        return u[-1] if u else None

    def tearDown(self):
        self.m.stop()
        self.m.wait_idle(15)
        shutil.rmtree(self.work, ignore_errors=True)

    def test_build_in_child(self):
        self.m.set_install(self.inst, prebuild=False)
        self.assertEqual(self.m.request(A)["state"], "building")
        self.assertTrue(until(lambda: (self.last(A) or {}).get("state") in ("ready", "error"), 60))
        self.assertEqual(self.last(A)["state"], "ready", self.logs)
        self.assertEqual(stub.CALLS, [])                                  # the converter ran in the child, not here
        with self.lk:
            b = [f for n, f in self.updates if n == A and f["state"] == "building"]
        self.assertTrue(any(f["progress"] > 0 for f in b))
        self.assertEqual(sorted(f["progress"] for f in b), [f["progress"] for f in b])
        self.assertTrue({"vfs", "terrain", "objects"} & {f["stage"] for f in b})
        sc = rosebuild.read_sidecar(self.rdir, A)
        self.assertEqual((sc["conv"], sc["install"]), (stub.pack_version(A), rosebuild.fingerprint(stub, self.inst)))   # birthisland: VERSION + its PACK_REV
        self.assertEqual(sc["warnings"], [])
        with open(rosebuild.pack_path(self.rdir, A), "rb") as fh:
            self.assertEqual(fh.read(), stub._pack_bytes(A))
        self.assertIn(f"[rose] build {A} done in", "\n".join(self.logs))
        self.assertTrue(self.m.wait_idle(60))                            # the background prebuild of the rest, also in children
        self.assertTrue(all(self.m.pack_state(n) == "fresh" for n in stub.PACKS))

    def test_cancel_terminates_child(self):
        self.m.child_env["GSVR_ROSE_STUB_STEP_S"] = "0.5"
        self.m.child_env["GSVR_ROSE_CHILD_STDIN_WATCH"] = "0"           # only terminate() may end it
        self.m.set_install(self.inst, prebuild=False)
        self.m.request(A)
        part = rosebuild.pack_path(self.rdir, A) + ".part"
        self.assertTrue(until(lambda: os.path.exists(part) and self.m.cur is not None and "pid" in self.m.cur, 30))
        pid = self.m.cur["pid"]
        self.assertFalse(gone(pid))
        t0 = time.monotonic()
        self.m.cancel()
        self.assertTrue(self.m.wait_idle(15))
        self.assertLess(time.monotonic() - t0, 5)
        self.assertTrue(gone(pid))
        self.assertFalse(os.path.exists(part))                           # the stub never got to clean it: the manager did
        self.assertEqual(self.last(A)["state"], "error")
        self.assertIn("cancelled", self.last(A)["error"])
        self.assertEqual(self.m.pack_state(A), "missing")
        self.assertIn(f"[rose] build {A} cancelled", "\n".join(self.logs))

    def test_preempt_background_child(self):
        self.m.child_env["GSVR_ROSE_STUB_STEP_S"] = "0.5"
        self.m.set_install(self.inst)                                    # background prebuild: first PACKS entry
        first = list(stub.PACKS)[0]
        self.assertNotEqual(first, B)
        self.assertTrue(until(lambda: self.m.cur is not None and self.m.cur["name"] == first and "pid" in self.m.cur, 30))
        pid = self.m.cur["pid"]
        self.m.child_env["GSVR_ROSE_STUB_STEP_S"] = "0.01"
        self.assertEqual(self.m.request(B)["state"], "building")
        self.assertTrue(until(lambda: (self.last(B) or {}).get("state") == "ready", 60), self.logs)
        self.assertTrue(gone(pid))
        self.assertIn(f"[rose] build {first} cancelled (pre-empted by {B}, requeued)", "\n".join(self.logs))
        self.assertTrue(self.m.wait_idle(90))
        self.assertTrue(all(self.m.pack_state(n) == "fresh" for n in stub.PACKS))
        self.assertEqual([f for f in os.listdir(self.rdir) if f.endswith((".part", ".tmp"))], [])

    def test_child_failure(self):
        self.m.child_env["GSVR_ROSE_STUB_FAIL"] = A
        self.m.set_install(self.inst, prebuild=False)
        self.m.request(A)
        self.assertTrue(self.m.wait_idle(30))
        self.assertEqual(self.last(A)["state"], "error")
        self.assertIn(f"ROSE build of {A} failed: stub failure at once", self.last(A)["error"])
        self.assertEqual(self.m.request(A)["state"], "error")             # failure cool-down as in-process

    def test_child_crash(self):
        """a child that dies without a result line -> error with the exit code + its last output"""
        orig = rosebuild.child_argv
        rosebuild.child_argv = lambda n, i, o: [sys.executable, "-I", "-c", "print('converter exploded'); raise SystemExit(4)"]
        try:
            self.m.set_install(self.inst, prebuild=False)
            self.m.request(A)
            self.assertTrue(self.m.wait_idle(30))
        finally:
            rosebuild.child_argv = orig
        err = self.last(A)["error"]
        self.assertIn("exited with code 4", err)
        self.assertIn("converter exploded", err)

    def test_child_exits_when_companion_goes(self):
        """the companion died (its end of our stdin closed) -> the child stops at once instead of building on"""
        env = dict(os.environ, GSVR_ROSE_CONV="stub", GSVR_ROSE_STUB_STEP_S="0.5")
        out = rosebuild.pack_path(self.rdir, A)
        p = subprocess.Popen(rosebuild.child_argv(A, self.inst, out), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, env=env)
        try:
            line = p.stdout.readline()
            self.assertTrue(line.startswith(b"@progress "), line)
            p.stdin.close()
            self.assertEqual(p.wait(10), 3)
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()
            p.stdout.close()

    def test_child_protocol_direct(self):
        env = dict(os.environ, GSVR_ROSE_CONV="stub", GSVR_ROSE_STUB_STEP_S="0.001")
        out = rosebuild.pack_path(self.rdir, B)
        r = subprocess.run(rosebuild.child_argv(B, self.inst, out), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           env=dict(env, GSVR_ROSE_CHILD_STDIN_WATCH="0"), timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = [rosebuild.parse_child_line(x) for x in r.stdout.splitlines()]
        self.assertNotIn(None, lines)                                    # nothing but protocol lines on stdout
        self.assertEqual(lines[-1][0], "result")
        self.assertEqual(lines[-1][1]["conv"], stub.VERSION)
        self.assertGreaterEqual(sum(1 for x in lines if x[0] == "progress"), 3)
        self.assertEqual(rose.validate(out), len(stub._pack_bytes(B)))
        r = subprocess.run(rosebuild.child_argv(B, self.inst, out)[:-1], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           env=env, timeout=30)
        self.assertEqual(r.returncode, 2)                                # usage


class CachedState(unittest.TestCase):
    def test_cached_state_never_scans_on_the_caller(self):
        work = tempfile.mkdtemp(prefix="rosestate")
        self.addCleanup(shutil.rmtree, work, True)
        inst = os.path.join(work, "inst")
        os.makedirs(inst)
        with open(os.path.join(inst, "data.idx"), "wb") as fh:
            fh.write(b"idx")
        m = rosebuild.Manager(os.path.join(work, "rose"), conv=stub, log=lambda s: None)
        self.addCleanup(m.stop)
        os.makedirs(m.rose_dir)
        m.set_install(inst, prebuild=False)
        slow = threading.Event()
        orig = m._pack_rows

        def rows():
            if slow.is_set():
                time.sleep(1.0)                                         # a slow disk / a huge Sound folder
            return orig()
        m._pack_rows = rows
        slow.set()
        t0 = time.perf_counter()
        st = m.state(cached=True)
        self.assertLess(time.perf_counter() - t0, 0.05)
        self.assertEqual(st["packs"], {})                               # before the first refresh
        self.assertTrue(until(lambda: len(m.state(cached=True)["packs"]) == len(stub.PACKS), 5))
        t0 = time.perf_counter()
        for _ in range(20):
            m.state(cached=True)
        self.assertLess(time.perf_counter() - t0, 0.1)
        self.assertEqual(m.state(cached=True)["packs"][A]["state"], "missing")
        with open(rosebuild.pack_path(m.rose_dir, A), "wb") as fh:      # hand-dropped: rows follow after an invalidation
            fh.write(stub._pack_bytes(A))
        with m.cv:
            m._invalidate()
        slow.clear()
        m.state(cached=True)
        self.assertTrue(until(lambda: m.state(cached=True)["packs"][A]["state"] == "stale", 5))


class QuietDrops(unittest.TestCase):
    def setUp(self):
        import companion
        self.c = companion
        self.logged = []
        self.orig_log = companion.log
        companion.log = self.logged.append
        companion._drops.update(n=0, t=None)

    def tearDown(self):
        self.c.log = self.orig_log

    def handler(self, exc):
        H = self.c.make_handler(None)
        h = H.__new__(H)

        class W:
            def write(self, b):
                raise exc

            def flush(self):
                pass
        h.wfile, h.command, h.path, h.request_version = W(), "GET", "/status.json", "HTTP/1.1"
        h.requestline, h.client_address, h.close_connection = "GET /status.json HTTP/1.1", ("127.0.0.1", 1), False
        return h

    def test_send_swallows_aborted(self):
        for exc in (ConnectionAbortedError(10053, "aborted"), ConnectionResetError(), BrokenPipeError()):
            h = self.handler(exc)
            err = io.StringIO()
            old, sys.stderr = sys.stderr, err
            try:
                h._send(200, "{}", "application/json")                  # no exception, no traceback
            finally:
                sys.stderr = old
            self.assertEqual(err.getvalue(), "")
            self.assertTrue(h.close_connection)
        self.assertEqual(len(self.logged), 1)                           # one short line per minute at most
        self.assertNotIn("Traceback", self.logged[0])
        self.assertIn("closed the connection early", self.logged[0])

    def test_server_handle_error_quiet(self):
        srv = self.c.Server(("127.0.0.1", 0), self.c._NoHandler)
        self.addCleanup(srv.server_close)
        err = io.StringIO()
        old, sys.stderr = sys.stderr, err
        try:
            try:
                raise ConnectionAbortedError(10053, "An established connection was aborted by the software in your host machine")
            except ConnectionAbortedError:
                srv.handle_error(None, ("127.0.0.1", 5))
            try:
                raise ValueError("a real bug")
            except ValueError:
                srv.handle_error(None, ("127.0.0.1", 5))                # other errors keep socketserver's traceback
        finally:
            sys.stderr = old
        self.assertNotIn("ConnectionAbortedError", err.getvalue())
        self.assertIn("ValueError", err.getvalue())


class GuiPoll(unittest.TestCase):
    def ctl(self, answers, running=True):
        import gui
        c = gui.Controller.__new__(gui.Controller)
        c.active_port, c.foreign, c._last_status, c._status_misses = None, set(), None, 0
        c.ports = lambda: [27015, 27016]
        c.running = lambda: running
        c.logs = []
        c.log = c.logs.append
        seq = iter(answers)
        c.fetch_status = lambda p: next(seq)
        return c

    def test_slow_poll_keeps_running(self):
        import gui
        self.assertEqual(gui.STATUS_TIMEOUT_S, 3.0)
        ok = {"app": "gsvr-companion", "port": 27015}
        # good, slow, slow, slow: the first two misses keep the last status (headline "Running"), the third gives up
        c = self.ctl([("gsvr", ok), ("slow", None), ("slow", None), ("slow", None), (None, None)])
        self.assertEqual(c.status(), ok)
        for i in (1, 2):
            st = c.status()
            self.assertIsNotNone(st)
            self.assertEqual(gui.headline(st, True, None)[1], "Running — 127.0.0.1:27015")
            self.assertEqual(st["_stale"], i)
            self.assertEqual(c.active_port, 27015)
            self.assertNotIn(27015, c.foreign)                         # busy is not "another program"
        self.assertIsNone(c.status())
        self.assertEqual(gui.headline(None, True, None)[1], "Starting…")

    def test_good_poll_resets_misses(self):
        ok = {"app": "gsvr-companion", "port": 27015}
        c = self.ctl([("gsvr", ok), ("slow", None), ("gsvr", ok), ("slow", None), ("slow", None)])
        c.status(), c.status(), c.status()
        self.assertEqual(c._status_misses, 0)
        self.assertIsNotNone(c.status())
        self.assertIsNotNone(c.status())

    def test_stopped_companion_not_kept(self):
        ok = {"app": "gsvr-companion", "port": 27015}
        c = self.ctl([("gsvr", ok), (None, None), (None, None)], running=False)
        c.status()
        self.assertIsNone(c.status())                                 # nothing listening + not running: no stale status

    def test_fetch_status_slow(self):
        """a port that accepts but answers after the timeout -> "slow" (not "foreign")"""
        import gui
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)
        self.addCleanup(srv.close)
        port = srv.getsockname()[1]
        self.assertEqual(gui.Controller.fetch_status(port, timeout=0.3), ("slow", None))

    def test_rose_status_line(self):
        import gui
        packs = {n: {"state": "fresh"} for n in stub.PACKS}
        rb = {"install": "<home>/ROSE", "install_ok": True, "conv": "v", "current": None, "queue": [], "packs": packs}
        base = {"app": "gsvr-companion", "port": 1, "ownership": {}}
        st = dict(base, rose_build=rb, rose={"state": "idle", "all_ready": True}, _rose_state={"install": "/Users/x/ROSE"})
        rows = dict(gui.status_rows(st))
        self.assertEqual(rows["ROSE Online"], "found: /Users/x/ROSE — all zones ready")
        self.assertEqual([r[0] for r in gui.status_rows(st)][:4], ["Companion", "Port", "Games found", "ROSE Online"])
        rb2 = dict(rb, current={"name": "luxemtower", "progress": 0.22, "eta_s": 40}, queue=["zant"],
                   packs=dict(packs, luxemtower={"state": "stale"}))
        t = gui.rose_status_text(dict(base, rose_build=rb2, rose={"state": "idle", "all_ready": False}))
        self.assertEqual(t, "found: <home>/ROSE — building luxemtower 22% (background)  noch 40 s, 1 queued")
        t = gui.rose_status_text(dict(base, rose_build=rb2, rose={"state": "building", "name": "zant", "progress": 0.5}))
        self.assertTrue(t.startswith("found: <home>/ROSE — building zant 50%"), t)
        t = gui.rose_status_text(dict(base, rose_build=dict(rb, current=None, packs=dict(packs, zant={"state": "missing"})),
                                      rose={"state": "idle", "all_ready": False}))
        self.assertTrue(t.endswith("%d/%d zones ready" % (len(packs) - 1, len(packs))), t)
        t = gui.rose_status_text(dict(base, rose_build=dict(rb, install=None, install_ok=False), rose={}))
        self.assertEqual(t, "folder not found - set it in the Spiele tab")
        t = gui.rose_status_text(dict(base, rose_build=dict(rb, install_ok=False, install_reason="no data.idx"), rose={}))
        self.assertTrue(t.startswith("folder not usable: <home>/ROSE (no data.idx)"), t)
        self.assertNotIn("ROSE Online", dict(gui.status_rows(base)))   # an old companion without ROSE: no line


def _live_companion(work, extra_env):
    import selftest as st
    pdir, rdir, logdir, inst = (os.path.join(work, x) for x in ("pre", "rose", "log", "inst"))
    for d in (pdir, rdir, logdir, inst):
        os.makedirs(d)
    with open(os.path.join(pdir, "valve__c1a0.bin"), "wb") as fh:
        fh.write(b"GSV1" + b"PACK" * 10)
    with open(os.path.join(inst, "data.idx"), "wb") as fh:
        fh.write(b"idx")
    st.append(os.path.join(logdir, "output_log_2026-11-14_10-00-00.txt"), "start")
    port = st.free_port()
    env = dict(os.environ, GSVR_ROSE_CONV="stub", HOME=work, APPDATA=work, USERPROFILE=work, **extra_env)
    cmd = [sys.executable, "-I", os.path.join(HERE, "companion.py"), "--prebuilt-dir", pdir, "--vrchat-log-dir", logdir,
           "--cache-dir", os.path.join(work, "cache"), "--rose-dir", rdir, "--rose-install", inst, "--port", str(port),
           "--no-prewarm"]
    out = open(os.path.join(work, "companion.out"), "w")
    proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, env=env)
    return proc, out, port


def _poll_latencies(port, n=25, gap=0.08):
    lat, last = [], None
    for _ in range(n):
        t0 = time.perf_counter()
        req = urllib.request.Request("http://127.0.0.1:%d/status.json" % port, headers={"Host": "127.0.0.1"})
        with urllib.request.urlopen(req, timeout=10) as r:
            last = json.loads(r.read())
        lat.append((time.perf_counter() - t0) * 1000.0)
        time.sleep(gap)
    return lat, last


class Latency(unittest.TestCase):
    """status.json stays fast while a CPU-burning (stub) ROSE build runs: the build is in a child process."""
    def run_case(self, extra_env):
        import selftest as st
        work = tempfile.mkdtemp(prefix="roselat")
        self.addCleanup(shutil.rmtree, work, True)
        proc, out, port = _live_companion(work, dict({"GSVR_ROSE_STUB_STEP_S": "0.5", "GSVR_ROSE_STUB_BURN": "1"}, **extra_env))

        def fin():
            if proc.poll() is None:
                proc.terminate()
                proc.wait(10)
            out.close()
        self.addCleanup(fin)
        s = st.wait_status(port, lambda s: s["rose_build"]["current"] is not None, 30)
        self.assertIsNotNone(s, "a background build started")
        lat, last = _poll_latencies(port)
        self.assertIsNotNone(last["rose_build"]["current"], "the build still ran while measuring")
        return lat

    def test_status_fast_during_child_build(self):
        lat = self.run_case({})
        msg = "status.json during a CPU-burning child build: max %.1f ms, median %.1f ms" % (max(lat), statistics.median(lat))
        print("\n  " + msg, file=sys.stderr)
        self.assertLess(max(lat), 200.0, msg)


if __name__ == "__main__":
    unittest.main()
