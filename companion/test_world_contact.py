"""World contact + status.json privacy + dark title bar (w32 laptop evidence 2026-11-24).

(1) any loopback request without the GUI header counts as world contact (VRChat's UA differs per platform; a world may only long-poll
    /pack/wait.bin); the checklist also trusts the log's allowed GSVR world (request this session or > 20 s in the log).
(2) status.json for a world: minimal outside an allowed world, no filesystem path / host numbers leak; the full one is GUI only.
(3) ui_theme.dark_title_bar: COLORREF conversion, no-op off Windows.
Run: cd companion && python3 -m unittest test_world_contact
"""
import json, os, re, shutil, sys, tempfile, threading, time, types, unittest, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import companion  # noqa: E402
import firstrun  # noqa: E402
import hostinfo  # noqa: E402
import i18n  # noqa: E402
import ui_theme  # noqa: E402
import worlds  # noqa: E402

PATHISH = re.compile(r"[A-Za-z]:[\\/]|\\\\|/Users/|/home/|<home>")
HL_ID = worlds.WORLDS["hl"][0]


def leaks(obj):
    """every decoded string (keys + values) that looks like a filesystem path"""
    if isinstance(obj, dict):
        return [x for k, v in obj.items() for x in leaks(k) + leaks(v)]
    if isinstance(obj, list):
        return [x for v in obj for x in leaks(v)]
    return [obj] if isinstance(obj, str) and PATHISH.search(obj) else []
HL_ROOT = "c:\\program files (x86)\\steam\\steamapps\\common\\Half-Life"


def full_status(world_ok=True, world=HL_ID):
    return {"app": companion.APP, "proto": companion.PROTO, "port": 38427, "v": 1, "companion": "0.9.57", "seq": 7, "wait": 20,
            "time": 1, "state": "error", "map": "valve/c1a0", "req": "r1", "error": "map not found in D:\\Games\\HL\\valve (check)",
            "world": world, "world_ok": world_ok, "music": {"a": "Sound\\Music\\x.mp3"},
            "games": [{"id": "valve", "name": "Half-Life", "found": True, "root": HL_ROOT, "source": "steam-manifest", "mods": ["valve"],
                       "setting": "hl_root", "hint": "found in C:\\Users\\Jane Doe\\Games, okay"},
                      {"id": "war3", "name": "Warcraft III", "found": True, "root": "E:\\WC3", "maps_root": "E:\\WC3\\Maps",
                       "version": "1.26", "source": "install-folder", "mods": [], "setting": "war3_root", "hint": ""}],
            "rose_build": {"install": "/Users/jane/ROSE", "install_ok": True, "install_reason": "ok: /home/jane/ROSE/data.idx",
                           "packs": {"birthisland": {"state": "ready", "error": None}}},
            "rose": {"state": "idle", "error": None, "text": ""},
            "host": {"ram_total_mb": 16384, "ram_free_mb": 6144, "cpu_pct": 20}, "ownership": {"valve": True}}


class WorldStatusShape(unittest.TestCase):
    def test_foreign_world_minimal(self):
        out = companion.world_status(full_status(world_ok=False, world="wrld_someone_else"))
        self.assertEqual(set(out), set(companion.MIN_STATUS_KEYS))
        self.assertIs(out["world_ok"], False)
        for k in ("games", "host", "rose_build", "ownership", "error", "music"):
            self.assertNotIn(k, out)
        self.assertEqual(leaks(out), [])

    def test_allowed_world_no_paths(self):
        full = full_status()
        out = companion.world_status(full)
        txt = json.dumps(out)
        self.assertEqual(leaks(out), [])
        self.assertNotIn("Jane", txt)
        self.assertNotIn("Program Files", txt.title())
        for g in out["games"]:
            self.assertFalse({"root", "maps_root"} & set(g))
        self.assertEqual([g["id"] for g in out["games"]], ["valve", "war3"])
        self.assertEqual((out["games"][0]["found"], out["games"][1]["version"], out["games"][0]["mods"]), (True, "1.26", ["valve"]))
        self.assertNotIn("install", out["rose_build"])
        self.assertTrue(out["rose_build"]["install_ok"])
        self.assertEqual(out["host"], full["host"])                         # the ROSE memory guard keeps its numbers
        self.assertEqual(out["ownership"], {"valve": True})                 # fields the worlds read stay
        for k in ("app", "world_ok", "state", "map", "req", "seq", "wait", "companion"):
            self.assertEqual(out[k], full[k])
        self.assertEqual(out["error"], "map not found in <path> (check)")
        self.assertEqual(out["music"], {"a": "Sound\\Music\\x.mp3"})       # archive-relative: not a filesystem path
        self.assertEqual(full["games"][0]["root"], HL_ROOT)                 # the GUI copy is untouched

    def test_strip_text(self):
        sp = companion.strip_paths
        self.assertEqual(sp(HL_ROOT), "<path>")
        self.assertEqual(sp("x /Users/bob/ROSE/data.idx: missing"), "x <path>: missing")
        self.assertEqual(sp("bad \\\\nas\\share\\x, retry"), "bad <path>, retry")
        self.assertEqual(sp("valve/c1a0"), "valve/c1a0")
        self.assertEqual(sp("https://x.y/home/a"), "https://x.y/home/a")
        self.assertEqual(sp({"a_root": "x", "dir": "y", "game_path": "z", "k": 1}), {"k": 1})


class Live(unittest.TestCase):
    """a real companion State + handler on an ephemeral port"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gsvrwc")
        os.makedirs(os.path.join(self.tmp, "game", "valve", "maps"))
        a = types.SimpleNamespace(prebuilt_dir=None, prebuilt_mod=None, game_root=os.path.join(self.tmp, "game"), steam_root=None,
                                  mods=None, no_war3=True, war3_root=None, war3_maps=None, cache_dir=os.path.join(self.tmp, "cache"),
                                  world_allow={HL_ID}, world_list=None, relay_url="", vrchat_log_dir=None, wait_max=0, http_log=None)
        self.logged = []
        self.orig_log, companion.log = companion.log, self.logged.append
        self.st = companion.State(a)
        self.st.port = 0
        self.srv = companion.Server(("127.0.0.1", 0), companion.make_handler(self.st))
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.orig_host = hostinfo.HOST
        hostinfo.HOST = hostinfo.HostInfo(mem=lambda: (16000.0, 6000.0), cpu=lambda: None)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        hostinfo.HOST = self.orig_host
        companion.log = self.orig_log
        shutil.rmtree(self.tmp, ignore_errors=True)

    def get(self, path, ua="UnityPlayer/2022.3.22f1-DWR (UnityWebRequest/1.0, libcurl/8.5.0-DEV)", **hdr):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), headers=dict({"User-Agent": ua}, **hdr))
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_gui_requests_are_not_world_contact(self):
        self.assertEqual(self.get("/gui/status.json", ua="Python-urllib/3.12", **{"X-GSVR-Gui": "1"})[0], 200)
        self.assertEqual(self.get("/status.json", ua="Python-urllib/3.12", **{"X-GSVR-Gui": "1"})[0], 200)
        self.assertEqual(self.get("/rose/state", ua="Python-urllib/3.12", **{"X-GSVR-Rose": "1"})[0], 200)
        self.assertFalse(getattr(self.st, "unity_poll_t", 0))
        self.assertFalse(any(m.startswith("world contact") for m in self.logged))

    def test_any_other_request_is_world_contact(self):
        # Windows VRChat: no "UnityPlayer" in the UA, and the world may only long-poll wait.bin / fetch index.json
        self.assertEqual(self.get("/pack/wait.bin?x=1", ua="Mozilla/5.0 (Windows NT 10.0; Win64; x64)")[0], 200)
        self.assertTrue(self.st.unity_poll_t)          # only feeds log_ok(), no GUI state
        first = [m for m in self.logged if m.startswith("world contact")]
        self.assertEqual(len(first), 1)
        self.assertIn("/pack/wait.bin UA=Mozilla/5.0", first[0])
        self.assertNotIn("x=1", first[0])                                  # path only, no query
        self.get("/index.json")
        self.assertEqual(len([m for m in self.logged if m.startswith("world contact")]), 1)   # once per session
        self.st.handle("world", HL_ID)
        self.get("/status.json")
        self.assertEqual(len([m for m in self.logged if m.startswith("world contact")]), 2)   # again in a new world

    def test_gui_route_forbidden_without_header(self):
        self.assertEqual(self.get("/gui/status.json")[0], 403)
        self.assertEqual(self.get("/gui/status.json", **{"X-GSVR-Gui": "0"})[0], 403)

    def test_status_foreign_world(self):
        self.st.handle("world", "wrld_foreign")
        self.assertFalse(self.st.world_ok)
        for path in ("/status.json", "/pack/wait.bin"):
            code, body = self.get(path)
            d = json.loads(body)
            self.assertEqual(code, 200)
            self.assertEqual(set(d), set(companion.MIN_STATUS_KEYS), path)
            self.assertEqual((d["app"], d["world_ok"], d["world"]), (companion.APP, False, "wrld_foreign"))
        self.assertEqual(self.get("/index.json")[0], 403)
        code, body = self.get("/gui/status.json", **{"X-GSVR-Gui": "1"})
        g = json.loads(body)
        self.assertIn(os.path.join(self.tmp, "game"), json.dumps(g["games"]).replace("\\\\", "\\"))   # the GUI still sees the folders

    def test_status_allowed_world(self):
        self.st.handle("world", HL_ID)
        self.assertTrue(self.st.world_ok)
        for path in ("/status.json", "/pack/wait.bin"):
            txt = self.get(path)[1].decode()
            d = json.loads(txt)
            self.assertEqual(leaks(d), [], path)
            self.assertNotIn(self.tmp, txt)
            self.assertTrue(d["world_ok"])
            for gm in d.get("games", []):
                self.assertNotIn("root", gm)
        d = json.loads(self.get("/status.json")[1])
        self.assertEqual(set(d["host"]), {"ram_total_mb", "ram_free_mb", "cpu_pct"})
        self.assertIn("valve", d["ownership"])
        self.assertIn("games", d)
        g = json.loads(self.get("/gui/status.json", **{"X-GSVR-Gui": "1"})[1])
        self.assertTrue(any(x.get("root") for x in g["games"]))
        for gone in ("world_seen_s", "world_for_s"):                         # the checklist no longer shows a world state
            self.assertNotIn(gone, g)
            self.assertNotIn(gone, d)


class Checklist(unittest.TestCase):
    def setUp(self):
        i18n.set_lang("en")

    def ents(self):
        return [{"id": "valve", "name": "Half-Life", "found": True, "root": "", "how": "steam", "world": "hl", "picked": False}]

    def model(self, st, vrc="running", upd=("current", None)):
        return firstrun.checklist(self.ents(), dict({"app": companion.APP, "build": [], "rose_build": {}}, **st), True, vrc,
                                  None, upd, "0.9.57", True, False)

    def test_no_world_or_untrusted_rows(self):
        """owner 2026-11-24: world contact + the untrusted-URL setting cannot be detected -> not in the checklist, whatever status says"""
        for st in ({}, {"world": HL_ID, "world_ok": True}, {"world": None, "world_ok": True}):
            m = self.model(st)
            self.assertEqual([r["key"] for r in m["rows"]], ["games", "vrchat", "version"])
            self.assertEqual(m["state"], "ready")
            self.assertTrue(m["hint"])
            self.assertTrue(m["primary"]["enabled"])

    def test_ready_needs_only_games_and_current_app(self):
        self.assertEqual(self.model({}, upd=("available", "0.9.60"))["state"], "step")
        self.assertEqual(self.model({}, vrc="missing")["state"], "step")


class GuiFallback(unittest.TestCase):
    def test_older_companion_403_falls_back(self):
        """review: an older companion answers /gui/status.json with 403 "world not allowed" outside an allowed world -> the GUI
        must fall back to /status.json, not mark the port foreign"""
        import http.server
        import gui

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/gui/status.json":
                    body, code = b"world not allowed\n", 403
                else:
                    body, code = json.dumps({"app": companion.APP, "world_ok": False}).encode(), 200
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            kind, d = gui.Controller.fetch_status(srv.server_address[1], timeout=3)
            self.assertEqual(kind, "gsvr")
            self.assertEqual(d["app"], companion.APP)
        finally:
            srv.shutdown()
            srv.server_close()


class TitleBar(unittest.TestCase):
    def test_colorref(self):
        self.assertEqual(ui_theme.colorref("#121821"), 0x00211812)
        self.assertEqual(ui_theme.colorref("#e9eef5"), 0x00F5EEE9)
        self.assertEqual(ui_theme.colorref("ff0000"), 0x000000FF)
        self.assertEqual(ui_theme.colorref("#0000ff"), 0x00FF0000)
        with self.assertRaises(ValueError):
            ui_theme.colorref("#fff")

    def test_noop_off_windows(self):
        class Boom:
            def __getattr__(self, n):
                raise AssertionError("window touched off Windows: " + n)
        for plat in ("darwin", "linux"):
            self.assertFalse(ui_theme.dark_title_bar(Boom(), platform=plat))
        if sys.platform != "win32":
            self.assertFalse(ui_theme.dark_title_bar(Boom()))

    def test_windows_error_is_swallowed(self):
        class Bad:
            def update_idletasks(self):
                raise RuntimeError("no window")
        self.assertFalse(ui_theme.dark_title_bar(Bad(), platform="win32"))   # off Windows: ctypes.windll missing -> False too


if __name__ == "__main__":
    unittest.main()
