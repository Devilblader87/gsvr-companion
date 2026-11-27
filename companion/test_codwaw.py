"""Call of Duty: World at War in the companion (companion/codwaw.py + the vendored converters tools/codwaw_pack.py, tools/waw_mappack.py).

Run: python3 -I companion/test_codwaw.py
  unit: vendored converter == tools/codwaw_SOURCE.txt, install detection (manual root, Steam library + appmanifest_10090,
        any zone/<language>), ownership / index / GAME-FOLDERS row, converter command lines, weapon pack validation (a synthetic
        CoD pack from the converter's own encoder; GSX1 without the CoD chunks is refused), --run-converter whitelist
  live: a companion on 127.0.0.1 with a FAKE install + fake converters: ownership codwaw true, index mp_dome, models=codwaw ->
        /pack/models.bin, map=codwaw/mp_dome -> /pack/current.bin, both byte-identical, cache hit on the second request.
No game files (synthetic data only), no network beyond 127.0.0.1.
"""
import hashlib, json, os, shutil, socket, struct, subprocess, sys, tempfile, time, unittest, urllib.request
from array import array

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
TOOLS = os.path.join(REPO, "tools")
sys.path.insert(0, HERE)
import codwaw, games, runtime                     # noqa: E402
sys.path.insert(0, os.path.join(HERE, "packaging"))
import toolfiles                                  # noqa: E402


def tiny_weapon_pack(with_chunks=True):
    """A synthetic CoD weapon pack written by the vendored converter's encoder (tools/codwaw_pack.encode)."""
    sys.path.insert(0, TOOLS)
    import codwaw_pack as C
    aw, ah, atlas, _ = C.pack_atlas({"a": (8, 8, bytes(range(256)))})
    tri = {"kind": 0, "wid": 1, "idx": [0, 1, 2], "uv": [0.0, 0.0, 1.0, 0.0, 0.0, 1.0],
           "poses": [([0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 1.0] * 3)]}
    w = {"id": 1, "meshP": 0, "meshW": -1, "meshV": -1, "sounds": [-1] * 10, "ev0": 0, "nev": 0, "name": "thompson_mp",
         "flags": 3, "animClass": -1, "muzzleP": (0.0, 0.0, 0.5), "muzzleV": (0.0, 0.0, 0.5), "reload_s": 1.0, "draw_s": 0.5,
         "grip": (0.0, 0.0, 0.0, 1.0)}
    csk = {"texW": C.TEX_W, "vbone": array("i", [0, 0, 0]), "vweight": array("f", [1, 0, 0, 0] * 3),
           "tex": array("f", [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0]),
           "rigs": [{"weapon": 0, "arms": 0, "boneFirst": 0, "boneCount": 1, "clipFirst": 0, "clipCount": 1,
                     "armsName": "viewmodel_x_arms", "tags": [0, 0, -1, 0], "slots": [0] + [-1] * (C.NUM_SLOTS - 1)}],
           "bones": [{"parent": -1, "name": "tag_view", "flags": 1, "q": (0.0, 0.0, 0.0, 1.0), "t": (0.0, 0.0, 0.0)}],
           "clips": [{"rig": 0, "frames": 1, "texelFirst": 0, "noteFirst": 0, "noteCount": 0, "name": "viewmodel_x_idle",
                      "flags": 0, "fps": 30.0, "seconds": 0.0, "srcFps": 30.0}],
           "notes": [], "skins": [{"mesh": 0, "rig": 0, "vertFirst": 0, "vertCount": 3}]}
    wdf = {"cols": [{"name": "damage", "enum": []}], "rows": ["thompson_mp"], "val": array("f", [40.0])}
    sal = {"aliases": [], "variants": []}
    pk = {"flags": 1, "atlas": {"w": aw, "h": ah, "mips": C.MIPS, "data": atlas}, "weapons": [w], "meshes": [tri],
          "sounds": [], "events": [],
          "chunks": [("CSK1", csk), ("WDF1", wdf), ("SAL1", sal)] if with_chunks else [("WDJ1", {"json": "{}"})]}
    return C.encode(pk)


def tiny_gsv3():
    v, i, aw, ah, lw, lh = 3, 3, 8, 8, 1, 1
    body = (v * 11 + i) * 4 + aw * ah * 4 + lw * lh * 4
    dir_off = 64 + body
    head = struct.pack("<4s15i", b"GSV3", 64, v, i, 0, aw, ah, 1, lw, lh, 0, 0, 0, 0, 0, dir_off)
    return head + bytes(body) + struct.pack("<i", 1) + struct.pack("<4s3i", b"MODL", dir_off + 20, 0, 0)


def fake_install(root, lang="German", maps=("mp_dome",)):
    os.makedirs(os.path.join(root, "main"))
    os.makedirs(os.path.join(root, "zone", lang))
    for p in [os.path.join(root, "main", "iw_00.iwd"), os.path.join(root, "zone", lang, "common_mp.ff"),
              os.path.join(root, "zone", lang, "localized_mp_dome.ff")] + [os.path.join(root, "zone", lang, m + ".ff") for m in maps]:
        with open(p, "wb") as fh:
            fh.write(b"fake " + os.path.basename(p).encode())
    return root


class Vendored(unittest.TestCase):
    def test_source_hashes(self):
        """every file of tools/codwaw_SOURCE.txt is the synced copy (edit them in the codwaw repo + tools/sync_codwaw_converter.sh)"""
        lines = open(os.path.join(TOOLS, codwaw.SOURCE_FILE)).read().splitlines()
        self.assertTrue(lines[0].startswith("source ") and len(lines[0]) == 7 + 40, lines[0])
        names = []
        for ln in lines[1:]:
            h, _, n = ln.partition("  ")
            names.append(n)
            with open(os.path.join(TOOLS, n), "rb") as fh:
                self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), h, n)
        self.assertIn(codwaw.WEAPON_CONVERTER, names)
        self.assertIn(codwaw.MAP_CONVERTER, names)
        # the frozen build bundles them (packaging/toolfiles.py follows the imports), --run-converter may start both
        tf = toolfiles.tool_files(TOOLS)
        self.assertTrue(set(names) <= set(tf), sorted(set(names) - set(tf)))
        self.assertEqual(toolfiles.missing_imports(TOOLS, tf), [])
        self.assertIn(codwaw.WEAPON_CONVERTER, runtime.CONVERTERS)
        self.assertIn(codwaw.MAP_CONVERTER, runtime.CONVERTERS)
        # GPL attribution of the OpenAssetTools-derived schema stays in the copy
        self.assertIn("GNU General Public License v3.0", open(os.path.join(TOOLS, "t4_schema.py")).read(2000))

    def test_converter_runs(self):
        r = subprocess.run(runtime.script_argv(sys.executable, os.path.join(TOOLS, codwaw.WEAPON_CONVERTER), "--version", frozen=False),
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout.strip(), r"^cwp-\d+\.\d+-r\d+$")


class Installs(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="codwawtest")
        self.saved = (codwaw.reg_read, codwaw.STEAM_ROOTS)
        codwaw.reg_read = lambda *a: None
        codwaw.STEAM_ROOTS = []

    def tearDown(self):
        codwaw.reg_read, codwaw.STEAM_ROOTS = self.saved
        shutil.rmtree(self.d, ignore_errors=True)

    def test_none(self):
        o = codwaw.ownership()[codwaw.MOD]
        self.assertFalse(o["allowed"])
        self.assertEqual(codwaw.build_index({codwaw.MOD: o}), {})
        g = codwaw.game_entry({codwaw.MOD: o})
        self.assertFalse(g["found"])
        self.assertTrue(g["hint"])

    def test_manual_any_language(self):
        root = fake_install(os.path.join(self.d, "WaW"), lang="German")
        os.makedirs(os.path.join(root, "zone", "French"))
        open(os.path.join(root, "zone", "French", "common_mp.ff"), "wb").close()
        self.assertEqual(codwaw.languages(root), ["German", "French"])          # LANG_ORDER: english, german, french ...
        own = codwaw.ownership([root])
        o = own[codwaw.MOD]
        self.assertTrue(o["allowed"])
        self.assertEqual((o["lang"], o["game_root"], o["source"]), ("German", os.path.normpath(root), "manual-override"))
        self.assertEqual(codwaw.ownership([root], "french")[codwaw.MOD]["lang"], "French")
        idx = codwaw.build_index(own)[codwaw.MOD]
        self.assertEqual(list(idx["maps"]), ["mp_dome"])
        self.assertEqual(idx["maps"]["mp_dome"]["dir"], "main")
        self.assertTrue(idx["maps"]["mp_dome"]["path"].endswith(os.path.join("zone", "German", "mp_dome.ff")))
        g = codwaw.game_entry(own)
        self.assertEqual((g["id"], g["found"], g["source"]), ("codwaw", True, "manual"))
        # the GUI table gets the row next to the HL mods (companion.rescan appends it to games_section)
        self.assertTrue(all(isinstance(x, dict) and "id" in x for x in games.games_section(own) + [g]))

    def test_not_an_install(self):
        root = os.path.join(self.d, "half")
        os.makedirs(os.path.join(root, "main"))
        open(os.path.join(root, "main", "iw_00.iwd"), "wb").close()               # no zone/<lang>/common_mp.ff
        self.assertFalse(codwaw.ownership([root])[codwaw.MOD]["allowed"])

    def test_steam_library(self):
        steam = os.path.join(self.d, "Steam")
        lib = os.path.join(self.d, "Lib2")
        os.makedirs(os.path.join(steam, "steamapps"))
        os.makedirs(os.path.join(lib, "steamapps", "common"))
        fake_install(os.path.join(lib, "steamapps", "common", "CoD WaW custom"), lang="english")
        with open(os.path.join(steam, "steamapps", "libraryfolders.vdf"), "w") as fh:
            fh.write('"libraryfolders"\n{\n "0" { "path" "%s" }\n "1" { "path" "%s" }\n}\n'
                     % (steam.replace("\\", "\\\\"), lib.replace("\\", "\\\\")))
        with open(os.path.join(lib, "steamapps", "appmanifest_10090.acf"), "w") as fh:
            fh.write('"AppState"\n{\n "appid" "10090"\n "StateFlags" "4"\n "installdir" "CoD WaW custom"\n}\n')
        codwaw.STEAM_ROOTS = [steam]
        f = codwaw.find_installs()
        self.assertEqual(len(f), 1)
        self.assertEqual((f[0]["source"], f[0]["languages"]), ("steam", ["english"]))
        o = codwaw.ownership()[codwaw.MOD]
        self.assertTrue(o["allowed"] and o["source"] == "steam")


class Converters(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="codwawconv")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_command_lines(self):
        w = codwaw.WeaponBuilder(frozen=False, python="py")
        a = w.argv(game_root="/g", lang="German", out="/c/x.tmp")
        self.assertEqual(a, ["py", "-X", "utf8", "-I", os.path.join(TOOLS, "codwaw_pack.py"), "build", "--game-root", "/g",
                             "--lang", "German", "/c/x.tmp"])
        m = codwaw.MapConverter(frozen=False, python="py")
        a = m.argv(bsp="/g/zone/German/mp_dome.ff", out="/c/m.tmp", mod="codwaw", map="mp_dome", game_root="/g", mod_dir="/g/main")
        self.assertEqual(a, ["py", "-X", "utf8", "-I", os.path.join(TOOLS, "waw_mappack.py"), "/g/zone/German/mp_dome.ff", "/c/m.tmp",
                             "--iwd", "/g/main"])
        self.assertTrue(w.version.startswith("v-") and m.version.startswith("v-") and w.version != m.version)
        wf = codwaw.WeaponBuilder(frozen=True, python="gsvr-cli")
        self.assertEqual(wf.argv(game_root="/g", lang="english", out="o")[:3], ["gsvr-cli", "--run-converter", os.path.join(TOOLS, "codwaw_pack.py")])

    def test_validate_weapons(self):
        good = os.path.join(self.d, "good.gsm")
        with open(good, "wb") as fh:
            fh.write(tiny_weapon_pack())
        self.assertEqual(codwaw.validate_weapons(good), os.path.getsize(good))
        bad = os.path.join(self.d, "bad.gsm")
        with open(bad, "wb") as fh:
            fh.write(tiny_weapon_pack(with_chunks=False))
        with self.assertRaises(RuntimeError):
            codwaw.validate_weapons(bad)
        trunc = os.path.join(self.d, "trunc.gsm")
        with open(trunc, "wb") as fh:
            fh.write(tiny_weapon_pack()[:-4])
        with self.assertRaises(RuntimeError):
            codwaw.validate_weapons(trunc)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def http(port, path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except OSError:
        return 0, b""


def gui_status(port):
    rq = urllib.request.Request(f"http://127.0.0.1:{port}/gui/status.json", headers={"X-GSVR-Gui": "1"})
    try:
        with urllib.request.urlopen(rq, timeout=10) as r:
            return json.loads(r.read())
    except OSError:
        return None


def wait(port, pred, t):
    end = time.time() + t
    while time.time() < end:
        s = gui_status(port)
        if s is not None and pred(s):
            return s
        time.sleep(0.2)
    return None


class Live(unittest.TestCase):
    def test_routes(self):
        work = tempfile.mkdtemp(prefix="codwawlive")
        proc = None
        try:
            root = fake_install(os.path.join(work, "WaW"))
            wpack, mpack = tiny_weapon_pack(), tiny_gsv3()
            src = os.path.join(work, "src")
            os.makedirs(src)
            for n, data in (("w.gsm", wpack), ("m.bin", mpack)):
                with open(os.path.join(src, n), "wb") as fh:
                    fh.write(data)
            fw, fm = os.path.join(work, "fake_cw.py"), os.path.join(work, "fake_map.py")
            with open(fw, "w") as fh:     # argv: build --game-root R --lang L out
                fh.write("import shutil, sys\nassert sys.argv[1:4] == ['build', '--game-root', %r]\nprint('@progress weapons 1 1')\n"
                         "shutil.copyfile(%r, sys.argv[-1])\n" % (os.path.normpath(root), os.path.join(src, "w.gsm")))
            with open(fm, "w") as fh:     # argv: <ff> <out> --iwd <main>
                fh.write("import shutil, sys\nassert sys.argv[1].endswith('mp_dome.ff') and sys.argv[3] == '--iwd'\n"
                         "shutil.copyfile(%r, sys.argv[2])\n" % os.path.join(src, "m.bin"))
            logdir, steam = os.path.join(work, "log"), os.path.join(work, "nosteam")
            os.makedirs(logdir)
            os.makedirs(steam)
            lg = os.path.join(logdir, "output_log_2026-11-27_10-00-00.txt")
            with open(lg, "w") as fh:
                fh.write("start\n")
            port = free_port()
            cmd = [sys.executable, "-I", os.path.join(HERE, "companion.py"), "--steam-root", steam, "--no-war3", "--no-rose-find",
                   "--codwaw-root", root, "--codwaw-converter", fw, "--codwaw-map-converter", fm, "--no-prewarm",
                   "--vrchat-log-dir", logdir, "--cache-dir", os.path.join(work, "cache"), "--world-allow", "wrld_ok",
                   "--port", str(port)]
            proc = subprocess.Popen(cmd, stdout=open(os.path.join(work, "companion.out"), "w"), stderr=subprocess.STDOUT)

            def log(line):
                with open(lg, "a") as fh:
                    fh.write(line + "\n")
            s = wait(port, lambda s: True, 30)
            self.assertIsNotNone(s, open(os.path.join(work, "companion.out")).read()[-2000:])
            self.assertTrue(s["ownership"].get("codwaw"))
            self.assertTrue(any(g["id"] == "codwaw" and g["found"] for g in s["games"]))
            log("Joining wrld_ok")
            self.assertIsNotNone(wait(port, lambda s: s["world_ok"], 15))
            code, body = http(port, "/index.json")
            idx = json.loads(body)
            self.assertEqual([m["name"] for m in idx["mods"]["codwaw"]["maps"]], ["mp_dome"])
            log("[GSVR-REQ] models=codwaw req=cw1")
            s = wait(port, lambda s: s["models"]["req"] == "cw1" and s["models"]["state"] in ("ready", "error"), 30)
            self.assertEqual(s["models"]["state"], "ready", s["models"])
            self.assertEqual(http(port, "/pack/models.bin"), (200, wpack))
            log("[GSVR-REQ] map=codwaw/mp_dome req=cm1")
            s = wait(port, lambda s: s["req"] == "cm1" and s["state"] in ("ready", "error"), 30)
            self.assertEqual((s["state"], s["map"]), ("ready", "codwaw/mp_dome"), s.get("error"))
            self.assertEqual(http(port, "/pack/current.bin"), (200, mpack))
            os.remove(fw)                                           # second visit: cache hits, no converter run
            log("[GSVR-REQ] models=codwaw req=cw2")
            s = wait(port, lambda s: s["models"]["req"] == "cw2" and s["models"]["state"] in ("ready", "error"), 30)
            self.assertEqual(s["models"]["state"], "ready", s["models"])
            log("[GSVR-REQ] map=codwaw/mp_castle req=cm2")          # not in MAPS: never converted
            s = wait(port, lambda s: s["req"] == "cm2" and s["state"] in ("ready", "error"), 60)
            self.assertEqual(s["state"], "error")
        finally:
            if proc:
                proc.terminate()
                proc.wait(10)
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=1)
