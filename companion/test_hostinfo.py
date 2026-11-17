"""status.json "host" (companion/hostinfo.py, docs/design/rose_preload_adaptive.md): the coarse RAM / CPU numbers the ROSE world sizes its
hidden neighbour zones from. Only numbers, rounded, never personal data; cached; a failing OS call = unknown (-1), never a broken status.

Run: python3 -I companion/test_hostinfo.py
"""
import json, os, shutil, sys, tempfile, types, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import hostinfo                                     # noqa: E402

KEYS = {"ram_total_mb", "ram_free_mb", "cpu_pct"}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Rounding(unittest.TestCase):
    def test_only_three_rounded_ints(self):
        p = hostinfo.public(16303.7, 7123.4, 37.2)
        self.assertEqual(set(p), KEYS)
        self.assertTrue(all(type(v) is int for v in p.values()))
        self.assertEqual(p, {"ram_total_mb": 16384, "ram_free_mb": 7168, "cpu_pct": 35})
        self.assertEqual(hostinfo.public(32651, 20000, 99.9)["ram_total_mb"], 32768)
        self.assertEqual(hostinfo.public(32651, 20000, 99.9)["cpu_pct"], 100)

    def test_unknown_is_minus_one(self):
        self.assertEqual(hostinfo.public(None, None, None), {"ram_total_mb": -1, "ram_free_mb": -1, "cpu_pct": -1})
        self.assertEqual(hostinfo.public(-5, -1, -3)["ram_free_mb"], -1)
        self.assertEqual(hostinfo.public(8192, None, 10)["ram_free_mb"], -1)

    def test_nan_and_inf_are_unknown(self):
        nan, inf = float("nan"), float("inf")
        self.assertEqual(hostinfo.public(nan, inf, nan), {"ram_total_mb": -1, "ram_free_mb": -1, "cpu_pct": -1})
        self.assertEqual(hostinfo.public(16384, -inf, inf), {"ram_total_mb": 16384, "ram_free_mb": -1, "cpu_pct": -1})
        self.assertEqual(hostinfo.public("x", None, 50), {"ram_total_mb": -1, "ram_free_mb": -1, "cpu_pct": 50})
        p = hostinfo.HostInfo(mem=lambda: (nan, inf), cpu=lambda: None, clock=Clock()).public()
        self.assertEqual(p, {"ram_total_mb": -1, "ram_free_mb": -1, "cpu_pct": -1})
        json.dumps(p, allow_nan=False)                                   # strict JSON: never NaN / Infinity in status.json

    def test_free_never_above_total(self):
        p = hostinfo.public(8192, 9999, 0)
        self.assertLessEqual(p["ram_free_mb"], p["ram_total_mb"])


class Parsers(unittest.TestCase):
    def test_meminfo(self):
        t = "MemTotal:       16316412 kB\nMemFree:          812344 kB\nMemAvailable:    9876543 kB\nCached:  1000 kB\n"
        tot, av = hostinfo.parse_meminfo(t)
        self.assertAlmostEqual(tot, 16316412 / 1024.0)
        self.assertAlmostEqual(av, 9876543 / 1024.0)
        tot, av = hostinfo.parse_meminfo("MemTotal: 2048 kB\nMemFree: 512 kB\nCached: 512 kB\n")    # old kernel: MemFree + Cached
        self.assertEqual((tot, av), (2.0, 1.0))
        self.assertIsNone(hostinfo.parse_meminfo("garbage"))

    def test_proc_stat_and_cpu_delta(self):
        a = hostinfo.parse_proc_stat("cpu  100 0 100 700 100 0 0 0 0 0\ncpu0 1 2 3 4\n")
        b = hostinfo.parse_proc_stat("cpu  200 0 200 1300 100 0 0 0 0 0\n")
        self.assertEqual(a, (800.0, 1000.0))
        self.assertAlmostEqual(hostinfo.cpu_pct(a, b), 25.0)            # 200 busy of 800 jiffies
        self.assertIsNone(hostinfo.cpu_pct(None, b))
        self.assertIsNone(hostinfo.cpu_pct(b, b))                        # no time passed
        self.assertIsNone(hostinfo.parse_proc_stat("intr 1 2 3"))

    def test_this_system_answers_without_error(self):
        p = hostinfo.HostInfo().public()                                 # real OS readers (Mac: total only, Linux: all, Windows: all)
        self.assertEqual(set(p), KEYS)
        self.assertTrue(all(type(v) is int and v >= -1 for v in p.values()))
        json.dumps(p)


class Sampler(unittest.TestCase):
    def test_cache_and_cpu_from_two_samples(self):
        clk = Clock()
        calls = {"m": 0}
        cpu = iter([(800.0, 1000.0), (1400.0, 1800.0), (1900.0, 2800.0)])

        def mem():
            calls["m"] += 1
            return 32700.0, 21000.0
        h = hostinfo.HostInfo(mem=mem, cpu=lambda: next(cpu), clock=clk)
        p1 = h.public()
        self.assertEqual(p1["cpu_pct"], -1)                              # one sample: no load yet
        self.assertEqual((p1["ram_total_mb"], p1["ram_free_mb"]), (32768, 20992))
        clk.t += 1.0
        self.assertEqual(h.public(), p1)                                 # cached (status polls stay cheap)
        self.assertEqual(calls["m"], 1)
        clk.t += hostinfo.CACHE_S
        self.assertEqual(h.public()["cpu_pct"], 25)                      # 200 busy of 800
        clk.t += hostinfo.CACHE_S
        self.assertEqual(h.public()["cpu_pct"], 50)

    def test_thread_mode_load_is_the_last_sample_interval(self):
        clk = Clock()
        cpu = iter([(0.0, 0.0), (100.0, 1000.0), (100.0, 2000.0), (200.0, 3000.0)])
        h = hostinfo.HostInfo(mem=lambda: (16384.0, 8192.0), cpu=lambda: next(cpu), clock=clk, cpu_thread=True)
        h._thread = object()                                             # no real thread in the test: ticks by hand
        h.cpu_tick(); h.cpu_tick()                                       # (0,0) -> (100,1000): 90 % busy
        self.assertEqual(h.public()["cpu_pct"], 90)
        h.cpu_tick()                                                     # a long quiet gap is not averaged in: only the last interval counts
        clk.t += 120.0
        self.assertEqual(h.public()["cpu_pct"], 100)                     # (100,1000) -> (100,2000): no idle at all
        h.cpu_tick()
        clk.t += hostinfo.CACHE_S
        self.assertEqual(h.public()["cpu_pct"], 90)

    def test_failing_readers_are_unknown(self):
        def boom():
            raise OSError("no")
        p = hostinfo.HostInfo(mem=boom, cpu=boom, clock=Clock()).public()
        self.assertEqual(p, {"ram_total_mb": -1, "ram_free_mb": -1, "cpu_pct": -1})

    def test_returned_dict_is_a_copy(self):
        h = hostinfo.HostInfo(mem=lambda: (8192.0, 4096.0), cpu=lambda: None, clock=Clock())
        h.public()["ram_free_mb"] = 1
        self.assertEqual(h.public()["ram_free_mb"], 4096)


class StatusJson(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_status_has_host_numbers_only(self):
        import companion
        a = types.SimpleNamespace(prebuilt_dir=None, prebuilt_mod=None, game_root=self.tmp, steam_root=None, mods=None, no_war3=True,
                                  war3_root=None, war3_maps=None, cache_dir=os.path.join(self.tmp, "cache"), world_allow=None, relay_url="",
                                  vrchat_log_dir=None, wait_max=0)
        st = companion.State(a)
        orig = hostinfo.HOST
        hostinfo.HOST = hostinfo.HostInfo(mem=lambda: (16000.0, 6000.0), cpu=lambda: None, clock=Clock())
        try:
            s = st.status_json()
        finally:
            hostinfo.HOST = orig
        h = s["host"]
        self.assertEqual(h, {"ram_total_mb": 16384, "ram_free_mb": 5888, "cpu_pct": -1})
        txt = json.dumps(h)
        for bad in (os.path.expanduser("~"), os.environ.get("USER") or "\0", os.environ.get("USERNAME") or "\0", "/", "\\"):
            self.assertNotIn(bad, txt)                                   # no folder / user names
        self.assertEqual(set(h), KEYS)


if __name__ == "__main__":
    unittest.main()
