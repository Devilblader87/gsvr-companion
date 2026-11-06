"""CS-SHARE tests (localhost only): the BSP dependency parser (hostile input included), what is packed / never packed,
the bundle rules shared by companion and relay, and the share -> relay -> fetch -> install flow against a loopback relay.

Run: python3 -I companion/test_csshare.py   (also part of companion/selftest.sh)
"""
import argparse, hashlib, io, os, shutil, struct, sys, tempfile, threading, time, unittest, zipfile, zlib

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, "relay"))
import runtime                                           # noqa: E402
runtime.tools_dir = lambda: os.path.join(REPO, "tools")
import csshare, mapshare, relay                          # noqa: E402


def sha(b):
    return hashlib.sha256(b).hexdigest()


def make_bsp(entities, pad=2048, version=30, ent_len=None, lump_override=None):
    """Minimal GoldSrc BSP: header with 15 lumps, the entity lump first, `pad` bytes of planes after it."""
    ent = entities.encode("latin-1") + b"\0"
    off = 124
    lumps = [(off, len(ent) if ent_len is None else ent_len)] + [(off + len(ent), pad if i == 0 else 0) for i in range(14)]
    if lump_override:
        lumps[lump_override[0]] = lump_override[1]
    hdr = struct.pack("<i", version) + b"".join(struct.pack("<ii", *l) for l in lumps)
    return hdr + ent + b"\0" * pad


def ents(**world):
    kv = "".join('"%s" "%s"\n' % (k, v) for k, v in world.items())
    return '{\n"classname" "worldspawn"\n%s}\n' % kv


def entity(**kv):
    return '{\n%s}\n' % "".join('"%s" "%s"\n' % (k, v) for k, v in kv.items())


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


def swap_first_lumps(bsp):
    """Blue Shift layout: a v30 file whose directory entries 0 (entities) and 1 (planes) are swapped."""
    e0, e1 = bsp[4:12], bsp[12:20]
    return bsp[:4] + e1 + e0 + bsp[20:]


class BlueShiftLumpSwap(unittest.TestCase):
    TEXT = ents(wad="\\half-life\\valve\\halflife.wad", skyname="desert")

    def test_swapped_directory_is_swapped_back(self):
        plain = make_bsp(self.TEXT, pad=2000)             # 2000 = multiple of 20 bytes of planes
        bs = swap_first_lumps(plain)
        self.assertNotEqual(plain, bs)
        self.assertEqual(csshare.bsp_lump_dir(bs), csshare.bsp_lump_dir(plain))
        self.assertEqual(csshare.bsp_entity_text(bs), csshare.bsp_entity_text(plain))
        self.assertEqual(csshare.parse_bsp(bs).sky, "desert")

    def test_normal_and_textless_maps_are_untouched(self):
        plain = make_bsp(self.TEXT)
        self.assertEqual(csshare.bsp_lump_dir(plain)[0], struct.unpack_from("<ii", plain, 4))
        # no entity text anywhere: left alone, not guessed
        junk = struct.pack("<i", 30) + struct.pack("<ii", 124, 40) + struct.pack("<ii", 164, 40) + b"\0" * (13 * 8) + bytes(range(1, 41)) * 2
        self.assertEqual(csshare.bsp_lump_dir(junk)[0], (124, 40))

    def test_binary_garbage_after_the_opening_text_still_counts(self):
        # ba_tram2 has binary bytes ~10 bytes into the entity lump (a mangled wad value)
        txt = b'{\n"wad" "\xd4\xc2\x9c\x81\xd8\n\x9e\x81\\half-life"\n}\n\0'
        planes = b"\x00\x00\x00\x00" * 10
        hdr = struct.pack("<i", 30) + struct.pack("<ii", 124 + len(txt), len(planes)) + struct.pack("<ii", 124, len(txt)) + b"\0" * (13 * 8)
        self.assertEqual(csshare.bsp_lump_dir(hdr + txt + planes)[0], (124, len(txt)))

    def test_agrees_with_tools_bsp30(self):
        sys.path.insert(0, os.path.join(REPO, "tools"))
        import bsp30
        for d in (make_bsp(self.TEXT, pad=2000), swap_first_lumps(make_bsp(self.TEXT, pad=2000)), make_bsp(self.TEXT, pad=0)):
            self.assertEqual(bsp30.lump_dir(d), csshare.bsp_lump_dir(d))

    def test_changelevel_targets_reads_swapped_maps(self):
        import companion
        text = '{\n"classname" "worldspawn"\n}\n' + entity(classname="trigger_changelevel", map="ba_next", landmark="lm")
        bs = swap_first_lumps(make_bsp(text, pad=2000))
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "ba_x.bsp")
            write(p, bs)
            self.assertEqual(companion.changelevel_targets(p), ["ba_next"])


class Parser(unittest.TestCase):
    def test_collects_the_files_a_map_names(self):
        text = (ents(wad="\\half-life\\valve\\halflife.wad;/maps/custom.wad;C:\\x\\Other.WAD", skyname="mysky")
                + entity(classname="ambient_generic", message="ambience/wind.wav")
                + entity(classname="env_sprite", model="sprites/glow.spr")
                + entity(classname="monster_x", model="models/x.mdl")
                + entity(classname="func_door", model="*12", noise1="doors/d1.wav"))
        info = csshare.parse_bsp(make_bsp(text))
        self.assertEqual(info.wads, ["halflife.wad", "custom.wad", "other.wad"])
        self.assertEqual(info.sky, "mysky")
        self.assertEqual(info.models, ["models/x.mdl"])
        self.assertEqual(info.sprites, ["sprites/glow.spr"])
        self.assertEqual(info.sounds, ["sound/ambience/wind.wav", "sound/doors/d1.wav"])

    def test_hostile_values_are_dropped(self):
        text = (ents(wad="../../evil.wad;a b.wad;..wad;good.wad", skyname="../../x")
                + entity(classname="a", model="models/../../etc/x.mdl", noise="/etc/passwd.wav", message="sound/../../y.wav")
                + entity(classname="b", model="models/ok.mdl", noise="C:\\win\\x.wav", noise1="x" * 300 + ".wav")
                + entity(classname="c", model="sprites/a.exe", noise2="a/b/c/d/e/f/g/h.wav"))
        info = csshare.parse_bsp(make_bsp(text))
        self.assertEqual(info.wads, ["evil.wad", "good.wad"])       # basename only: the path part is thrown away
        self.assertEqual(info.sky, "")
        self.assertEqual(info.models, ["models/ok.mdl"])
        self.assertEqual(info.sprites, [])
        self.assertEqual(info.sounds, ["sound/etc/passwd.wav"])     # a leading '/' only means "below sound/"; traversal, drive letters, too long / deep are gone

    def test_bad_bsp_rejected(self):
        good = make_bsp(ents())
        csshare.parse_bsp(good)
        for bad in (b"", b"x" * 50, make_bsp(ents(), version=29), make_bsp(ents(), version=31),
                    make_bsp(ents(), lump_override=(0, (124, 1 << 30))),             # lump beyond the file
                    make_bsp(ents(), lump_override=(3, (-5, 10))),
                    make_bsp(ents(), ent_len=csshare.MAX_ENT_LUMP + 1)):
            with self.assertRaises(ValueError):
                csshare.parse_bsp(bad)

    def test_entity_flood_is_bounded(self):
        text = ents(wad="a.wad") + "".join(entity(classname="info_target", model="models/m%d.mdl" % i) for i in range(5000))
        info = csshare.parse_bsp(make_bsp(text))
        self.assertLessEqual(len(info.models), csshare.MAX_REFS)
        long_kv = '{ "classname" "worldspawn" ' + " ".join('"k%d" "v"' % i for i in range(500)) + ' "wad" "late.wad" }'
        self.assertEqual(csshare.parse_bsp(make_bsp(long_kv)).wads, [])  # past MAX_KEYS: ignored, no crash

    def test_wanted_never_lists_stock(self):
        info = csshare.parse_bsp(make_bsp(ents(wad="halflife.wad;cstrike.wad;mine.wad", skyname="desert")))
        rels = [r for r, _k in csshare.wanted(info, "de_mine")]
        self.assertIn("mine.wad", rels)
        self.assertNotIn("halflife.wad", rels)
        self.assertNotIn("cstrike.wad", rels)
        self.assertFalse([r for r in rels if r.startswith("gfx/env")])      # stock sky
        info = csshare.parse_bsp(make_bsp(ents(skyname="mysky")))
        self.assertEqual(len([r for r, k in csshare.wanted(info, "de_mine") if k == "sky"]), 6)
        # extra stock names from the cache dir
        rels = [r for r, _k in csshare.wanted(csshare.parse_bsp(make_bsp(ents(wad="mine.wad"))), "m", (), frozenset({"mine.wad"}))]
        self.assertNotIn("mine.wad", rels)

    def test_res_file(self):
        self.assertEqual(csshare.parse_res('// c\nmodels/a.mdl\n"sound/x y.wav"\nsprites\\b.spr extra\n../bad.mdl\nfoo.exe\nmy.wad\n'),
                         ["models/a.mdl", "sprites/b.spr", "my.wad"])


class Names(unittest.TestCase):
    def test_company_and_relay_agree_on_names(self):
        good = ["maps/de_x.bsp", "maps/de_x.res", "mine.wad", "Mine.WAD", "gfx/env/skybk.tga", "sound/a/b.wav", "models/p/q.mdl",
                "sprites/x.spr", "overviews/de_x.bmp", "maps/de-x_2.bsp"]
        bad = ["", "/maps/a.bsp", "maps/a.bsp/", "../maps/a.bsp", "maps/../a.bsp", "maps/.hidden.bsp", "maps/a.exe", "a.dll", "x/a.bsp",
               "maps\\a.bsp", "maps/a b.bsp", "maps/a..bsp", "maps/a.bsp.", "sound/a.mp3", "models/a.spr", "gfx/a.wav", "C:/a.wad",
               "maps/" + "a" * 120 + ".bsp", "a/b/c/d/e/f/g.wav", "maps/a\x00.bsp", "maps/ä.bsp",
               "maps/con.bsp", "maps/NUL.txt", "aux.wad", "Com1.wad", "sound/lpt9.wav", "models/prn.mdl", "sound/con/a.wav",
               "maps/aux.res", "gfx/env/com5.tga"]
        good += ["maps/console.bsp", "maps/com10.bsp", "maps/de_nul.bsp", "maps/lpt.bsp"]            # only the exact device names are out
        for n in good:
            self.assertTrue(csshare.safe_rel(n), n)
            self.assertTrue(relay.cs_name_ok(n), n)
        for n in bad:
            self.assertFalse(csshare.safe_rel(n), n)
            self.assertFalse(relay.cs_name_ok(n), n)


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="csshare")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cache = os.path.join(self.tmp, "cache")
        os.makedirs(self.cache)

    def game(self, name="host"):
        root = os.path.join(self.tmp, name)
        os.makedirs(os.path.join(root, "cstrike", "maps"))
        return root

    def relay_server(self, **kw):
        cfg = relay.Config(data=os.path.join(self.tmp, "relaydata"), **kw)
        srv = relay.make_server(cfg, "127.0.0.1", 0, log=lambda m: None)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))
        return srv, "http://127.0.0.1:%d" % srv.server_address[1]

    def custom_map(self, root, name="de_custom", folder="cstrike_downloads", extra_text=""):
        text = (ents(wad="halflife.wad;custom.wad;base_custom.wad", skyname="mysky")
                + entity(classname="env_sprite", model="sprites/glow.spr")
                + entity(classname="ambient_generic", message="ambience/own.wav")
                + entity(classname="ambient_generic", message="ambience/valve.wav")
                + entity(classname="monster_x", model="models/own.mdl") + extra_text)
        bsp = make_bsp(text)
        path = os.path.join(root, folder, "maps", name + ".bsp")
        write(path, bsp)
        write(os.path.join(root, folder, "custom.wad"), b"WAD3" + b"c" * 100)
        write(os.path.join(root, "cstrike", "base_custom.wad"), b"WAD3" + b"b" * 100)      # custom wad in the base folder: shared
        write(os.path.join(root, "cstrike", "halflife.wad"), b"WAD3stock")                  # stock wad: never
        for side in csshare.SKY_SIDES:
            write(os.path.join(root, "cstrike_downloads", "gfx", "env", "mysky%s.tga" % side), b"T" + side.encode())
        write(os.path.join(root, "cstrike_downloads", "sprites", "glow.spr"), b"SPR")
        write(os.path.join(root, "cstrike_downloads", "sound", "ambience", "own.wav"), b"RIFFown")
        write(os.path.join(root, "cstrike", "sound", "ambience", "valve.wav"), b"RIFFvalve")  # base folder: counts as stock
        write(os.path.join(root, "cstrike_addon", "models", "own.mdl"), b"IDST")
        write(os.path.join(root, folder, "maps", name + ".txt"), b"description")
        write(os.path.join(root, folder, "overviews", name + ".bmp"), b"BM")
        return path, bsp


class Plan(Tmp):
    def test_packs_custom_never_stock(self):
        root = self.game()
        path, bsp = self.custom_map(root)
        plan = csshare.plan_bundle(path, root)
        rels = [f[0] for f in plan.files]
        self.assertEqual(plan.sha, sha(bsp))
        self.assertEqual(rels[0], "maps/de_custom.bsp")
        for want in ("custom.wad", "base_custom.wad", "sprites/glow.spr", "sound/ambience/own.wav", "models/own.mdl",
                     "maps/de_custom.txt", "overviews/de_custom.bmp", "gfx/env/mysky" + "bk.tga"):
            self.assertIn(want, rels)
        self.assertNotIn("halflife.wad", rels)                                   # stock wad
        self.assertNotIn("sound/ambience/valve.wav", rels)                       # base folder = stock
        self.assertIn(("sound/ambience/valve.wav", "base folder = stock"), plan.skipped)

    def test_classic_and_unsafe_maps_are_not_shared(self):
        root = self.game()
        write(os.path.join(root, "cstrike", "maps", "de_dust2.bsp"), make_bsp(ents()))
        with self.assertRaises(csshare.NotShareable):
            csshare.plan_bundle(os.path.join(root, "cstrike", "maps", "de_dust2.bsp"), root)
        write(os.path.join(root, "cstrike", "maps", "de_junk.bsp"), b"nope" * 100)
        with self.assertRaises(csshare.NotShareable):
            csshare.plan_bundle(os.path.join(root, "cstrike", "maps", "de_junk.bsp"), root)
        write(os.path.join(root, "cstrike", "maps", "de_big.bsp"), make_bsp(ents(), pad=1 << 20))
        with self.assertRaises(csshare.NotShareable):
            csshare.plan_bundle(os.path.join(root, "cstrike", "maps", "de_big.bsp"), root, limit=512 * 1024)
        path, _b = self.custom_map(root, "de_xx")
        with self.assertRaises(csshare.NotShareable):                             # hash on the stock list
            csshare.plan_bundle(path, root, extra=frozenset({sha(open(path, "rb").read())}))

    def test_budget_leaves_big_files_out(self):
        root = self.game()
        path, _b = self.custom_map(root)
        write(os.path.join(root, "cstrike_downloads", "custom.wad"), b"W" * 300000)
        plan = csshare.plan_bundle(path, root, limit=csshare.BUNDLE_RESERVE + 200000)
        self.assertNotIn("custom.wad", [f[0] for f in plan.files])
        self.assertIn(("custom.wad", "bundle limit"), plan.skipped)

    def test_bundle_is_deterministic_and_passes_the_relay_rules(self):
        root = self.game()
        path, bsp = self.custom_map(root)
        plan = csshare.plan_bundle(path, root)
        a, b = os.path.join(self.tmp, "a.zip"), os.path.join(self.tmp, "b.zip")
        csshare.build_bundle(plan, a)
        time.sleep(1.1)
        os.utime(os.path.join(root, "cstrike_downloads", "custom.wad"))
        csshare.build_bundle(plan, b)
        self.assertEqual(open(a, "rb").read(), open(b, "rb").read())
        self.assertIsNone(relay.is_cs_bundle(a, sha(bsp), 50 << 20))
        self.assertEqual(relay.is_cs_bundle(a, sha(b"other"), 50 << 20), "map sha mismatch")
        self.assertEqual(csshare.validate_bundle(a, sha(bsp)[:16]).name, "de_custom")

    def test_share_keys_cache(self):
        root = self.game()
        path, bsp = self.custom_map(root)
        k = csshare.ShareKeys(self.cache)
        self.assertEqual(k.key("de_custom", path), sha(bsp)[:16])
        self.assertIsNone(k.key("de_dust2", path))                                # classic name: no key
        k.save()
        k2 = csshare.ShareKeys(self.cache)
        self.assertEqual(len(k2.cache), 1)
        self.assertEqual(k2.key("de_custom", path), sha(bsp)[:16])


def forge(entries, compress=False):
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED) as zf:
        for n, d in entries:
            zf.writestr(n, d)
    return bio.getvalue()


class Flow(Tmp):
    def share(self, url, root, path):
        sh = csshare.CsSharer(url, self.cache, lambda m: self.logs.append(m))
        th = sh.maybe_share(path, root)
        self.assertIsNotNone(th)
        th.join(15)
        return sh

    def setUp(self):
        super().setUp()
        self.logs = []

    def test_host_uploads_guest_installs(self):
        host, guest = self.game("host"), self.game("guest")
        path, bsp = self.custom_map(host)
        srv, url = self.relay_server()
        parts = mapshare.parse_relay_url(url)
        key = sha(bsp)
        self.assertEqual(mapshare.relay_head(parts, key, "cs"), 404)
        self.share(url, host, path)
        self.assertEqual(mapshare.relay_head(parts, key, "cs"), 200)
        self.assertEqual(mapshare.relay_head(parts, key[:16], "cs"), 200)           # the 16-hex key the world hands out
        n_before = len(self.logs)
        self.share(url, host, path)                                                    # second host: HEAD says there, no PUT
        self.assertFalse(any("PUT -> 201" in l for l in self.logs[n_before:]))
        # a guest with a stock wad and its own sound already installed
        write(os.path.join(guest, "cstrike", "sound", "ambience", "own.wav"), b"GUESTS-OWN")
        seen = []
        written = csshare.fetch_cs(url, key[:16], "de_custom", guest, "cstrike", self.cache, lambda p, ph: seen.append((p, ph)),
                                   sleep=lambda s: None, poll=0)
        dl = os.path.join(guest, "cstrike_downloads")
        self.assertEqual(open(os.path.join(dl, "maps", "de_custom.bsp"), "rb").read(), bsp)
        self.assertIn("custom.wad", written)
        self.assertIn("base_custom.wad", written)
        self.assertTrue(os.path.isfile(os.path.join(dl, "gfx", "env", "mysky" + "up.tga")))
        self.assertTrue(os.path.isfile(os.path.join(dl, "models", "own.mdl")))
        self.assertFalse(os.path.exists(os.path.join(dl, "halflife.wad")))               # stock never travels
        self.assertFalse(os.path.exists(os.path.join(dl, "sound", "ambience", "valve.wav")))
        self.assertFalse(os.path.exists(os.path.join(dl, "sound", "ambience", "own.wav")))   # the guest had it: kept
        self.assertEqual(open(os.path.join(guest, "cstrike", "sound", "ambience", "own.wav"), "rb").read(), b"GUESTS-OWN")
        self.assertIn((100, "download"), seen)
        self.assertEqual([f for f in os.listdir(os.path.join(self.cache, "csdl"))], [])   # no .part left

    def test_guest_waits_for_the_upload(self):
        host, guest = self.game("host"), self.game("guest")
        path, bsp = self.custom_map(host)
        srv, url = self.relay_server()
        seen = []
        res = {}

        def guest_fetch():
            try:
                res["w"] = csshare.fetch_cs(url, sha(bsp)[:16], "de_custom", guest, "cstrike", self.cache,
                                            lambda p, ph: seen.append(ph), poll=0.05, timeout=10)
            except Exception as e:                                                            # noqa: BLE001
                res["e"] = e
        th = threading.Thread(target=guest_fetch)
        th.start()
        time.sleep(0.4)
        self.share(url, host, path)
        th.join(15)
        self.assertNotIn("e", res, res.get("e"))
        self.assertIn("waiting", seen)
        self.assertTrue(os.path.isfile(os.path.join(guest, "cstrike_downloads", "maps", "de_custom.bsp")))

    def test_not_on_the_relay_and_cancel(self):
        guest = self.game("guest")
        srv, url = self.relay_server()
        clock = [0.0]
        with self.assertRaises(LookupError):
            csshare.fetch_cs(url, "ab" * 8, "de_x", guest, "cstrike", self.cache, sleep=lambda s: clock.__setitem__(0, clock[0] + 30),
                             clock=lambda: clock[0], poll=0, timeout=60)
        with self.assertRaises(mapshare.Cancelled):
            csshare.fetch_cs(url, "ab" * 8, "de_x", guest, "cstrike", self.cache, cancel=lambda: True)
        self.assertFalse(os.path.exists(os.path.join(guest, "cstrike_downloads", "maps")))
        with self.assertRaises(LookupError):
            csshare.fetch_cs("", "ab" * 8, "de_x", guest, "cstrike", self.cache)
        with self.assertRaises(LookupError):
            csshare.fetch_cs(url, "zz", "de_x", guest, "cstrike", self.cache)
        with self.assertRaises(LookupError):
            csshare.fetch_cs(url, "ab" * 8, "de_x", guest, "valve", self.cache)           # not a shared mod

    def test_relay_rejects_bad_bundles(self):
        srv, url = self.relay_server()
        parts = mapshare.parse_relay_url(url)
        bsp = make_bsp(ents(wad="a.wad"))

        def put(data, key=None):
            p = os.path.join(self.tmp, "up.bin")
            write(p, data)
            return mapshare.relay_put(parts, key or sha(bsp), p, kind="cs")
        good = forge([("maps/de_a.bsp", bsp), ("a.wad", b"WAD3")])
        self.assertEqual(put(good), 201)
        self.assertEqual(put(good), 200)
        self.assertEqual(put(good, sha(b"x")), 422)                                        # key is not the map's hash
        self.assertEqual(put(forge([("maps/de_a.bsp", bsp), ("a.wad", b"WAD3")], compress=True), sha(bsp)), 415)   # deflated
        self.assertEqual(put(forge([("maps/de_a.bsp", bsp), ("../evil.dll", b"x")])), 415)
        self.assertEqual(put(forge([("maps/de_a.bsp", bsp), ("tool.exe", b"x")])), 415)
        self.assertEqual(put(forge([("maps/de_a.bsp", bsp), ("maps/de_b.bsp", bsp)])), 415)       # two maps
        self.assertEqual(put(forge([("a.wad", b"WAD3")])), 415)                                    # no map
        self.assertEqual(put(forge([("maps/de_a.bsp", make_bsp(ents(), version=29))], False), sha(make_bsp(ents(), version=29))), 415)
        self.assertEqual(put(b"HM3W" + b"\0" * 3000), 415)                                         # a bare map file is no bundle
        self.assertEqual(put(b"PK\x03\x04" + b"\0" * 3000), 415)
        self.assertEqual(put(forge([("maps/de_a.bsp", bsp)] + [("sound/s%d.wav" % i, b"x") for i in range(300)])), 415)  # entry count

    def test_relay_size_limit_and_wc3_unchanged(self):
        srv, url = self.relay_server(max_pack_bytes=20000)
        parts = mapshare.parse_relay_url(url)
        bsp = make_bsp(ents(), pad=30000)
        p = os.path.join(self.tmp, "big.zip")
        write(p, forge([("maps/de_a.bsp", bsp)]))
        self.assertEqual(mapshare.relay_put(parts, sha(bsp), p, kind="cs"), 413)
        # a Warcraft III map on the cs route is refused, a bundle on the war3 route too
        import test_mapshare
        w3 = test_mapshare.good_map(b"z")
        write(p, w3)
        self.assertEqual(mapshare.relay_put(parts, sha(w3), p, kind="cs"), 415)
        srv2, url2 = self.relay_server()
        parts2 = mapshare.parse_relay_url(url2)
        good = forge([("maps/de_a.bsp", make_bsp(ents()))])
        write(p, good)
        self.assertEqual(mapshare.relay_put(parts2, sha(good), p), 415)

    def test_hostile_bundle_cannot_write_outside_or_bring_extras(self):
        guest = self.game("guest")
        bsp = make_bsp(ents(wad="halflife.wad;mine.wad") + entity(classname="x", model="models/m.mdl"))
        p = os.path.join(self.tmp, "h.zip")
        write(p, forge([("maps/de_h.bsp", bsp), ("mine.wad", b"WAD3"), ("halflife.wad", b"STOCK"), ("models/m.mdl", b"IDST"),
                        ("models/unrelated.mdl", b"IDST"), ("maps/de_h.res", b"models/m.mdl\n")]))
        written = csshare.install_bundle(p, guest, "cstrike", "de_h", sha(bsp)[:16])
        self.assertEqual(sorted(written), ["maps/de_h.bsp", "maps/de_h.res", "mine.wad", "models/m.mdl"])
        dl = os.path.join(guest, "cstrike_downloads")
        self.assertFalse(os.path.exists(os.path.join(dl, "halflife.wad")))
        self.assertFalse(os.path.exists(os.path.join(dl, "models", "unrelated.mdl")))
        # traversal in the zip itself
        write(p, forge([("maps/de_h.bsp", bsp), ("../../escape.wad", b"x")]))
        with self.assertRaises(ValueError):
            csshare.install_bundle(p, guest, "cstrike", "de_h", None)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "escape.wad")))
        # wrong key, wrong map name, compressed, not a zip - nothing new written
        before = sorted(os.listdir(dl))
        write(p, forge([("maps/de_h.bsp", bsp)]))
        for kw in (dict(key16="0" * 16), dict(expect_name="de_other")):
            with self.assertRaises(ValueError):
                csshare.install_bundle(p, os.path.join(self.tmp, "g2"), "cstrike", **kw)
        write(p, forge([("maps/de_h.bsp", bsp)], compress=True))
        with self.assertRaises(ValueError):
            csshare.validate_bundle(p)
        write(p, b"not a zip" * 100)
        with self.assertRaises(ValueError):
            csshare.validate_bundle(p)
        self.assertEqual(sorted(os.listdir(dl)), before)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "g2", "cstrike_downloads", "maps", "de_h.bsp")))


def corrupt_crc(data, needle):
    """flip one byte of the stored payload `needle` (CRC stays, so zipfile raises BadZipFile when the entry is read)"""
    i = data.index(needle)
    return data[:i] + bytes([data[i] ^ 0xFF]) + data[i + 1:]


class Partial(Tmp):
    def bundle(self, bsp, extra, name="de_h"):
        return forge([("maps/%s.bsp" % name, bsp)] + extra)

    def files(self, root):
        out = []
        for d, _ds, fs in os.walk(os.path.join(root, "cstrike_downloads")):
            out += [os.path.relpath(os.path.join(d, f), root) for f in fs]
        return out

    def test_broken_zips_are_value_errors(self):
        guest = self.game("guest")
        bsp = make_bsp(ents(wad="mine.wad"))
        p = os.path.join(self.tmp, "b.zip")
        good = self.bundle(bsp, [("mine.wad", b"WAD3" + b"m" * 64)])
        for bad in (good[:len(good) // 2], good[:-5], b"PK\x03\x04" + b"\0" * 100, corrupt_crc(good, b"WAD3" + b"m" * 64), b""):
            write(p, bad)
            with self.assertRaises(ValueError):
                csshare.install_bundle(p, guest, "cstrike", "de_h")
        for exc in (EOFError("x"), RuntimeError("encrypted"), zlib.error("bad")):               # whatever zipfile raises is data, not a crash
            orig = zipfile.ZipFile.read

            def boom(self, *a, **k):
                raise exc
            zipfile.ZipFile.read = boom
            try:
                write(p, good)
                with self.assertRaises(ValueError):
                    csshare.validate_bundle(p)
                with self.assertRaises(ValueError):
                    csshare.install_bundle(p, guest, "cstrike", "de_h")
            finally:
                zipfile.ZipFile.read = orig
        self.assertEqual(self.files(guest), [])

    def test_bad_crc_in_a_late_entry_writes_nothing(self):
        guest = self.game("guest")
        bsp = make_bsp(ents(wad="mine.wad") + entity(classname="x", model="models/m.mdl"))
        p = os.path.join(self.tmp, "b.zip")
        write(p, corrupt_crc(self.bundle(bsp, [("mine.wad", b"WAD3"), ("models/m.mdl", b"IDST" + b"z" * 50)]), b"IDST" + b"z" * 50))
        with self.assertRaises(ValueError):
            csshare.install_bundle(p, guest, "cstrike", "de_h")
        self.assertEqual(self.files(guest), [])
        self.assertEqual([f for f in os.listdir(os.path.join(guest, "cstrike_downloads")) if f.startswith(".")], [])

    def test_bsp_is_moved_last_and_a_retry_repairs(self):
        guest = self.game("guest")
        bsp = make_bsp(ents(wad="mine.wad") + entity(classname="x", model="models/m.mdl"))
        p = os.path.join(self.tmp, "b.zip")
        write(p, self.bundle(bsp, [("mine.wad", b"WAD3"), ("models/m.mdl", b"IDST")]))
        order, real = [], os.replace

        def replace(a, b):
            if os.path.basename(b).endswith(".bsp"):
                raise OSError("disk full")
            order.append(os.path.basename(b))
            return real(a, b)
        os.replace = replace
        try:
            with self.assertRaises(OSError):
                csshare.install_bundle(p, guest, "cstrike", "de_h")
        finally:
            os.replace = real
        self.assertEqual(sorted(order), ["m.mdl", "mine.wad"])                                # everything else was in place first
        self.assertFalse(os.path.exists(os.path.join(guest, "cstrike_downloads", "maps", "de_h.bsp")))
        self.assertEqual([f for f in os.listdir(os.path.join(guest, "cstrike_downloads")) if f.startswith(".")], [])   # stage removed
        written = csshare.install_bundle(p, guest, "cstrike", "de_h")                         # the next attempt repairs: the .bsp lands
        self.assertEqual(written, ["maps/de_h.bsp"])
        self.assertEqual(open(os.path.join(guest, "cstrike_downloads", "maps", "de_h.bsp"), "rb").read(), bsp)

    def test_map_must_be_exactly_maps_name_bsp(self):
        bsp = make_bsp(ents())
        p = os.path.join(self.tmp, "b.zip")
        for entries in ([("maps/sub/de_h.bsp", bsp)], [("maps/de_h.bsp", bsp), ("maps/sub/x.bsp", bsp)], [("maps/de_h.txt", b"x")]):
            write(p, forge(entries))
            with self.assertRaises(ValueError):
                csshare.validate_bundle(p)
            self.assertIsNotNone(relay.is_cs_bundle(p, sha(bsp), 50 << 20))

    def test_an_installed_map_counts_as_present(self):
        guest = self.game("guest")
        bsp = make_bsp(ents())
        p = os.path.join(self.tmp, "b.zip")
        write(p, self.bundle(bsp, []))
        write(os.path.join(guest, "cstrike", "maps", "de_h.bsp"), bsp)                         # already there: kept, nothing to write
        self.assertEqual(csshare.install_bundle(p, guest, "cstrike", "de_h"), [])

    def test_fetch_cs_bad_data_is_value_error_and_leaves_nothing(self):
        host_bsp = make_bsp(ents(wad="mine.wad"))
        guest = self.game("guest")
        srv, url = self.relay_server()
        parts = mapshare.parse_relay_url(url)
        p = os.path.join(self.tmp, "up.zip")
        write(p, corrupt_crc(self.bundle(host_bsp, [("mine.wad", b"WAD3" + b"q" * 80)]), b"WAD3" + b"q" * 80))
        self.assertEqual(mapshare.relay_put(parts, sha(host_bsp), p, kind="cs"), 201)         # the relay only checks the map entry
        with self.assertRaises(ValueError):
            csshare.fetch_cs(url, sha(host_bsp)[:16], "de_h", guest, "cstrike", self.cache, sleep=lambda s: None, poll=0)
        self.assertEqual(self.files(guest), [])
        self.assertEqual(os.listdir(os.path.join(self.cache, "csdl")), [])

    def test_fetch_cs_checks_the_relays_bundle_hash(self):
        host, guest = self.game("host"), self.game("guest")
        path, bsp = self.custom_map(host)
        srv, url = self.relay_server()
        sh = csshare.CsSharer(url, self.cache, lambda m: None)
        sh.maybe_share(path, host).join(15)
        parts = mapshare.parse_relay_url(url)
        meta = {}
        tmp = os.path.join(self.tmp, "dl.zip")
        st, digest = mapshare.relay_download(parts, sha(bsp), tmp, None, None, meta, kind="cs")
        self.assertEqual(meta["bundle_sha"], digest)                                         # the relay recorded the bundle's own hash
        orig = mapshare.relay_download

        def lying(parts, key, tmp, progress=None, cancel=None, meta=None, kind="map"):
            r = orig(parts, key, tmp, progress, cancel, meta, kind)
            meta["bundle_sha"] = "0" * 64
            return r
        mapshare.relay_download = lying
        try:
            with self.assertRaises(ValueError):
                csshare.fetch_cs(url, sha(bsp)[:16], "de_custom", guest, "cstrike", self.cache, sleep=lambda s: None, poll=0)
        finally:
            mapshare.relay_download = orig
        self.assertEqual(self.files(guest), [])


class FirstUploadWins(Tmp):
    def test_live_key_is_not_replaced_and_other_bundles_are_refused(self):
        srv, url = self.relay_server()
        parts = mapshare.parse_relay_url(url)
        bsp = make_bsp(ents(wad="a.wad"))
        p = os.path.join(self.tmp, "up.zip")

        def put(entries):
            write(p, forge(entries))
            return mapshare.relay_put(parts, sha(bsp), p, kind="cs")
        first = [("maps/de_a.bsp", bsp), ("a.wad", b"WAD3-one")]
        self.assertEqual(put(first), 201)
        stored = os.path.join(srv.store.dir, sha(bsp))
        before = (open(stored, "rb").read(), os.stat(stored).st_mtime_ns)
        self.assertEqual(put(first), 200)                                                     # same bundle again: fine, not committed
        self.assertEqual(put([("maps/de_a.bsp", bsp), ("a.wad", b"WAD3-TWO")]), 409)          # same .bsp, other extras
        self.assertEqual(put([("maps/de_a.bsp", bsp)]), 409)
        self.assertEqual((open(stored, "rb").read(), os.stat(stored).st_mtime_ns), before)    # untouched
        self.assertEqual(len([f for f in os.listdir(srv.store.dir) if f.startswith(".tmp-")]), 0)
        # the record is served with the bundle and removed with it
        self.assertEqual(srv.store.get_bundle_sha(sha(bsp)), sha(open(stored, "rb").read()))
        self.assertTrue(os.path.exists(srv.store.bundle_path(sha(bsp))))
        srv.store.delete(sha(bsp))
        self.assertFalse(os.path.exists(srv.store.bundle_path(sha(bsp))))
        self.assertEqual(put([("maps/de_a.bsp", bsp), ("a.wad", b"WAD3-TWO")]), 201)          # gone (expired / taken down): a new upload may win

    def test_expired_key_can_be_replaced(self):
        srv, url = self.relay_server(ttl_hours=0.0002)
        parts = mapshare.parse_relay_url(url)
        bsp = make_bsp(ents())
        p = os.path.join(self.tmp, "up.zip")
        write(p, forge([("maps/de_a.bsp", bsp)]))
        self.assertEqual(mapshare.relay_put(parts, sha(bsp), p, kind="cs"), 201)
        time.sleep(1.0)
        write(p, forge([("maps/de_a.bsp", bsp), ("a.wad", b"W")]))
        self.assertEqual(mapshare.relay_put(parts, sha(bsp), p, kind="cs"), 201)

    def test_get_sends_the_bundle_hash(self):
        import http.client
        srv, url = self.relay_server()
        parts = mapshare.parse_relay_url(url)
        bsp = make_bsp(ents())
        p = os.path.join(self.tmp, "up.zip")
        write(p, forge([("maps/de_a.bsp", bsp)]))
        mapshare.relay_put(parts, sha(bsp), p, kind="cs")
        c = http.client.HTTPConnection(parts[1], parts[2])
        c.request("GET", "/v1/cs/" + sha(bsp)[:16])
        r = c.getresponse()
        body = r.read()
        self.assertEqual(r.getheader("X-Bundle-Sha256"), sha(body))


class StateFlow(Tmp):
    """companion.State: index "share" keys, the [GSVR-REQ] share= token, fetch_from_relay for cstrike."""
    def state(self, root, url, mode="on"):
        import companion
        a = argparse.Namespace(prebuilt_dir=None, game_root=root, mods=None, steam_root=None, no_war3=True, war3_root=None,
                               war3_maps=None, cache_dir=self.cache, world_allow=None, relay_url=url, map_autodownload=mode,
                               wait_max=0, vrchat_log_dir=None)
        st = companion.State(a)
        self.states = []
        orig = st._set
        st._set = lambda slot, **kw: (self.states.append((kw.get("state"), kw.get("percent"), kw.get("phase"))), orig(slot, **kw))[1]
        return st

    def test_index_share_keys_and_fetch(self):
        host, guest = self.game("host"), self.game("guest")
        path, bsp = self.custom_map(host)
        write(os.path.join(host, "cstrike", "maps", "de_dust2.bsp"), make_bsp(ents()))
        srv, url = self.relay_server()
        gate, orig_key = threading.Event(), csshare.ShareKeys.key                 # hold the background hasher: it would race the assertion below
        def gated(self_, name, path, compute=True):
            if compute:
                gate.wait(15)
            return orig_key(self_, name, path, compute)
        csshare.ShareKeys.key = gated
        try:
            h = self.state(host, url)
            rev = h.index_rev
            maps = {m["name"]: m for m in h.index_json()["mods"]["cstrike"]["maps"]}
            self.assertNotIn("share", maps["de_custom"])                          # the scan itself never hashes
        finally:
            gate.set()
            csshare.ShareKeys.key = orig_key
        h.share_thread.join(15)                                                   # the background hasher fills the keys in
        self.assertGreater(h.index_rev, rev)
        maps = {m["name"]: m for m in h.index_json()["mods"]["cstrike"]["maps"]}
        self.assertEqual(maps["de_custom"]["share"], sha(bsp)[:16])
        h2 = self.state(host, url)                                                # second start: keys come from the cache at once
        self.assertEqual({m["name"]: m for m in h2.index_json()["mods"]["cstrike"]["maps"]}["de_custom"]["share"], sha(bsp)[:16])
        os.remove(path)                                                           # stale cache entries are pruned on the next scan
        self.assertEqual(len(h2.cs_keys.cache), 1)
        h2.rescan()
        self.assertEqual(len(h2.cs_keys.cache), 0)
        path, bsp = self.custom_map(host)
        self.assertNotIn("share", maps["de_dust2"])                               # classic maps carry no key
        self.assertNotIn("share", {m["name"]: m for m in self.state(host, "").index_json()["mods"]["cstrike"]["maps"]}["de_custom"])  # no relay: no key
        csshare.CsSharer(url, self.cache, lambda m: None).maybe_share(path, host).join(15)
        g = self.state(guest, url)
        with self.assertRaises(RuntimeError):
            g.resolve("cstrike/de_custom")
        miss = RuntimeError("map 'cstrike/de_custom' not found")
        self.assertFalse(g.fetch_from_relay("current", "cstrike/de_custom", miss, None))      # no key: nothing to ask for
        self.assertFalse(g.fetch_from_relay("current", "cstrike/de_custom", miss, "bad"))
        self.assertTrue(g.fetch_from_relay("current", "cstrike/de_custom", miss, sha(bsp)[:16]))
        self.assertTrue(any(s[0] == "fetching" and s[2] == "download" for s in self.states), self.states)
        g.share_thread.join(15)
        mod, name, e, m = g.resolve("cstrike/de_custom")
        self.assertEqual(m["dir"], "cstrike_downloads")
        self.assertEqual(m["share"], sha(bsp)[:16])                                          # the guest can pass it on
        off = self.state(self.game("g3"), url, "off")
        self.assertFalse(off.fetch_from_relay("current", "cstrike/de_custom", miss, sha(bsp)[:16]))
        # a key nobody uploaded: soft failure and back-off
        g2 = self.state(self.game("g4"), url)
        orig = mapshare.wait_on_relay
        mapshare.wait_on_relay = lambda parts, key, progress=None, cancel=None, timeout=0, *r, **k: orig(parts, key, progress, cancel, 0, *r, **k)
        try:
            self.assertFalse(g2.fetch_from_relay("current", "cstrike/de_x", RuntimeError("map 'cstrike/de_x' not found"), "cd" * 8))
        finally:
            mapshare.wait_on_relay = orig
        self.assertIn("cd" * 8, g2.fetch_failed)

    def test_request_token_parsed(self):
        import logwatch
        k, cmd = logwatch.parse_line("12:00 Debug - [GSVR-REQ] map=cstrike/de_x req=3x9 share=0123456789abcdef")
        self.assertEqual(cmd["share"], "0123456789abcdef")
        st = self.state(self.game("g"), "")
        st.world_ok = True
        st.handle("cmd", cmd)
        self.assertEqual(st.jobs["current"], ("cstrike/de_x", "3x9", None, "0123456789abcdef"))
        st.handle("cmd", {"map": "cstrike/de_y", "share": "../../etc"})
        self.assertIsNone(st.jobs["current"][3])


if __name__ == "__main__":
    unittest.main()
