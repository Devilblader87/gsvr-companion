"""Build progress (companion/buildprog.py, tools/bprog.py, gui.build_rows) -> status.json "build".

Run: python3 -I companion/test_buildprog.py
"""
import io, json, os, shutil, subprocess, sys, tempfile, textwrap, threading, time, unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, "tools"))
import buildprog, bprog, gui                       # noqa: E402


def snap(job, at=None):
    return job.snapshot(job.t0 + at if at is not None else None)


class JobParse(unittest.TestCase):
    def setUp(self):
        self.t = buildprog.Tracker()

    def test_plain_lines_are_not_progress(self):
        with self.t.job("map", "cstrike", "de_dust2") as j:
            self.assertFalse(j.feed("  atlas 1024x1024"))
            self.assertFalse(j.feed("email@example.com"))
            self.assertTrue(j.feed("@progress a 1 2"))
            self.assertTrue(j.feed("@progress broken x y"))      # swallowed, ignored

    def test_weighted_stages(self):
        with self.t.job("models", "cstrike", "cstrike") as j:
            j.feed("@plan weapons=50 players=50")
            self.assertEqual(snap(j, 1)["pct"], 0)
            j.feed("@progress weapons 5 10")
            self.assertEqual(snap(j, 1)["pct"], 25)
            j.feed("@progress weapons 10 10")
            j.feed("@progress players 1 4")
            self.assertEqual(snap(j, 1)["pct"], 62)

    def test_unplanned_stage_default_weight_and_even_split(self):
        with self.t.job("map", "valve", "c1a0") as j:
            j.feed("@progress a 1 1")
            self.assertEqual(snap(j, 1)["pct"], 99)              # a single finished stage, but never 100 while the job runs
            j.feed("@progress b 0 1")
            self.assertLessEqual(snap(j, 2)["pct"], 99)

    def test_monotonic(self):
        with self.t.job("map", "valve", "c1a0") as j:
            j.feed("@plan a=10")
            j.feed("@progress a 8 10")
            p1 = snap(j, 1)["pct"]
            j.feed("@progress z 0 1")                              # a new unplanned stage widens the total
            self.assertGreaterEqual(snap(j, 2)["pct"], p1)

    def test_all_stages_done_is_99_and_eta_0(self):
        with self.t.job("hologram", "valve", "valve") as j:
            j.feed("@plan a=1")
            j.feed("@progress a 1 1")
            s = snap(j, 3)
            self.assertEqual((s["pct"], s["eta_s"]), (99, 0))

    def test_eta_from_rate(self):
        with self.t.job("models", "valve", "valve") as j:
            j.feed("@plan a=100")
            j.feed("@progress a 25 100")
            s = snap(j, 30)                                        # 25 % in 30 s -> 90 s left
            self.assertEqual((s["pct"], s["eta_s"]), (25, 90))

    def test_eta_unknown_without_data(self):
        with self.t.job("map", "valve", "c1a0") as j:
            s = snap(j, 5)
            self.assertEqual((s["pct"], s["eta_s"], s["state"]), (0, -1, "building"))

    def test_history_time_based(self):
        self.t.history[self.t.hkey("map", "valve", "c1a0")] = 100.0
        with self.t.job("map", "valve", "c1a0") as j:
            s = snap(j, 40)
            self.assertEqual((s["pct"], s["eta_s"]), (40, 60))
            s = snap(j, 500)
            self.assertEqual((s["pct"], s["eta_s"]), (95, 0))      # overran the last build: stays below 100

    def test_history_eta_early_then_rate(self):
        self.t.history[self.t.hkey("models", "cstrike", "cstrike")] = 120.0
        with self.t.job("models", "cstrike", "cstrike") as j:
            j.feed("@plan a=100")
            j.feed("@progress a 2 100")                            # < 8 %: the rate is not trusted yet
            self.assertEqual(snap(j, 10)["eta_s"], 110)

    def test_name_fields_clipped_and_war3_version_dropped(self):
        with self.t.job("war3", "war3", "Maps/x.w3x@1.31") as j:
            self.assertEqual(snap(j)["name"], "Maps/x.w3x")

    def test_public_compact_and_capped(self):
        js = [self.t.job("map", "valve", "m%d" % i) for i in range(7)]
        pub = self.t.public()
        self.assertEqual(len(pub), buildprog.MAX_JOBS)
        self.assertEqual(sorted(pub[0]), ["eta_s", "kind", "mod", "name", "pct", "state"])
        self.assertLess(len(json.dumps(pub)), 600)
        for j in js:
            self.t.finish(j)
        self.assertEqual(self.t.public(), [])

    def test_history_saved_and_loaded(self):
        d = tempfile.mkdtemp(prefix="bptest")
        try:
            p = os.path.join(d, "build_times.json")
            self.t.set_history_file(p)
            with self.t.job("map", "valve", "c1a0"):
                time.sleep(0.05)
            self.assertTrue(os.path.isfile(p))
            t2 = buildprog.Tracker()
            t2.set_history_file(p)
            self.assertIn("map|valve|c1a0", t2.history)
            with self.assertRaises(RuntimeError):                  # a failed build is not remembered
                with t2.job("map", "valve", "bad"):
                    raise RuntimeError("x")
            self.assertNotIn("map|valve|bad", t2.history)
            self.assertEqual(t2.public(), [])                      # and the job is gone
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_corrupt_history_file(self):
        d = tempfile.mkdtemp(prefix="bptest")
        try:
            p = os.path.join(d, "h.json")
            open(p, "w").write("{not json")
            self.t.set_history_file(p)
            self.assertEqual(self.t.history, {})
        finally:
            shutil.rmtree(d, ignore_errors=True)


class Streaming(unittest.TestCase):
    def test_run_streams_progress_while_the_child_runs(self):
        script = textwrap.dedent("""
            import sys, time
            print("@plan a=1", flush=True)
            print("hello", flush=True)
            print("@progress a 1 2", flush=True)
            print("warn", file=sys.stderr, flush=True)
            time.sleep(1.5)
            print("@progress a 2 2", flush=True)
            """)
        t = buildprog.Tracker()
        seen = []

        def watch():
            for _ in range(40):
                time.sleep(0.1)
                seen.extend(j["pct"] for j in t.public())

        with t.job("models", "valve", "valve") as j:
            w = threading.Thread(target=watch)
            w.start()
            r = buildprog.run([sys.executable, "-c", script], 30, on_line=j.feed, stdin=subprocess.DEVNULL)
            w.join()
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "hello\n")                      # progress lines are stripped
        self.assertEqual(r.stderr, "warn\n")
        self.assertIn(50, seen[:15])                               # half done was visible while the child still slept
        self.assertEqual(t.public(), [])

    def test_run_timeout_kills(self):
        t0 = time.time()
        with self.assertRaises(subprocess.TimeoutExpired):
            buildprog.run([sys.executable, "-c", "import time; time.sleep(30)"], 0.5)
        self.assertLess(time.time() - t0, 10)

    def test_run_nonzero_and_big_output(self):
        r = buildprog.run([sys.executable, "-c", "import sys; print('x' * 200000); sys.stderr.write('e' * 200000); sys.exit(3)"], 30)
        self.assertEqual(r.returncode, 3)
        self.assertEqual(len(r.stdout), 200001)
        self.assertEqual(len(r.stderr), 200000 + 1)

    def test_run_missing_binary_is_oserror(self):
        with self.assertRaises(OSError):
            buildprog.run(["/nonexistent/gsvr-cli"], 5)


class Emitter(unittest.TestCase):
    def test_bprog_lines(self):
        bprog._last.clear()
        buf = io.StringIO()
        with redirect_stdout(buf):
            bprog.plan(a=3, b=7)
            bprog.progress("a", 0, 4)
            bprog.progress("a", 1, 4)                              # throttled (< 0.25 s after the previous one)
            bprog.progress("a", 4, 4)                              # last of a stage is never throttled
            bprog.progress("b", 9, 4)                              # clamped
        self.assertEqual(buf.getvalue().splitlines(), ["@plan a=3 b=7", "@progress a 0 4", "@progress a 4 4", "@progress b 4 4"])
        j = buildprog.Tracker().job("map", "valve", "x")
        for line in buf.getvalue().splitlines():
            self.assertTrue(j.feed(line))

    def test_converters_emit_progress(self):
        for fn, needles in (("modelpack.py", ('"weapons"', '"players"', '"monsters"', '"write"')),
                            ("previewpack.py", ('"prepare"', '"finish"')),
                            ("pack.py", ('"convert"', '"nav"', '"sounds"', '"write"'))):
            src = open(os.path.join(REPO, "tools", fn), encoding="utf-8").read()
            self.assertIn("bprog.plan(", src, fn)
            for n in needles:
                self.assertIn("bprog.progress(" + n, src, fn + " " + n)

    def test_previewpack_synthetic_flow_emits(self):
        import previewpack as pp
        # build_bundle on an unreadable map: every stage still reports and the error comes back as before
        buf = io.StringIO()
        with redirect_stdout(buf):
            try:
                pp.build_bundle([("nomap", "/nonexistent.bsp")], None, "valve", log=lambda *_a: None)
            except ValueError:
                pass
        lines = buf.getvalue().splitlines()
        self.assertEqual(lines[0], "@plan prepare=70 finish=25 pack=5")
        self.assertIn("@progress prepare 1 1", lines)


class GuiRows(unittest.TestCase):
    def test_fmt_eta(self):
        self.assertEqual(gui.fmt_eta(-1), "")
        self.assertEqual(gui.fmt_eta(None), "")
        self.assertEqual(gui.fmt_eta(0), "noch 1 s")
        self.assertEqual(gui.fmt_eta(42), "noch 42 s")
        self.assertEqual(gui.fmt_eta(60), "noch ca. 1 Min")
        self.assertEqual(gui.fmt_eta(95), "noch ca. 2 Min")

    def test_rows(self):
        st = {"build": [{"kind": "models", "mod": "cstrike", "name": "cstrike", "pct": 62, "eta_s": 70, "state": "building"},
                        {"kind": "map", "mod": "valve", "name": "c1a0", "pct": 250, "eta_s": -1, "state": "building"},
                        {"kind": "hologram", "mod": "valve", "name": "valve", "pct": "x"}, "junk"]}
        self.assertEqual(gui.build_rows(st), [("Modelle: cstrike", 62, "noch ca. 1 Min"), ("Karte: c1a0", 100, ""),
                                              ("Hologramm: valve", 0, "")])

    def test_rows_idle_and_old_companion(self):
        self.assertEqual(gui.build_rows({"build": []}), [])
        self.assertEqual(gui.build_rows({}), [])
        self.assertEqual(gui.build_rows(None), [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
