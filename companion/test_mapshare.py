"""MAP-RELAY tests (localhost only): relay round trip through the companion's client, sha mismatch, size / format
rejection, the fetch path against a fake relay, stock maps never uploaded, decompression-bomb limits, and the
State-level fetch ('fetching' status, ask mode, off).

Run: python3 -I companion/test_mapshare.py   (also part of companion/selftest.sh)
"""
import argparse, hashlib, http.server, os, shutil, struct, sys, tempfile, threading, time, unittest, zlib

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, "relay"))
import runtime                                           # noqa: E402
runtime.tools_dir = lambda: os.path.join(REPO, "tools")
import mapshare, relay, war3                             # noqa: E402


def sha(b):
    return hashlib.sha256(b).hexdigest()


def encrypt(w, data, key):
    n = len(data) // 4
    vals = struct.unpack("<%dI" % n, data)
    out, seed = [], 0xEEEEEEEE
    for v in vals:
        seed = (seed + w.CRYPT[0x400 + (key & 0xFF)]) & w.M32
        out.append(v ^ ((key + seed) & w.M32))
        key = ((((~key) << 0x15) + 0x11111111) & w.M32) | (key >> 0x0B)
        seed = (v + seed + (seed << 5) + 3) & w.M32
    return struct.pack("<%dI" % n, *out)


def make_mpq(files, prefix=b"HM3W" + b"\0" * 508):
    """files: [(name, data, flags, fsize)] -> bytes of a minimal MPQ v0 (HM3W header + archive at offset 512)."""
    w = mapshare.harden_mpq()
    body, blocks, pos = b"", [], 32
    for _n, data, flags, fsize in files:
        blocks.append((pos, len(data), fsize, flags | w.F_EXISTS))
        body += data
        pos += len(data)
    hn = 16
    ht = [[0xFFFFFFFF, 0xFFFFFFFF, 0xFFFF, 0xFFFF, 0xFFFFFFFF] for _ in range(hn)]
    for i, (n, _d, _f, _s) in enumerate(files):
        j = w.hash_string(n, w.HASH_OFFSET) & (hn - 1)
        while ht[j][4] != 0xFFFFFFFF:
            j = (j + 1) % hn
        ht[j] = [w.hash_string(n, w.HASH_A), w.hash_string(n, w.HASH_B), 0, 0, i]
    htb = b"".join(struct.pack("<IIHHI", *e) for e in ht)
    btb = b"".join(struct.pack("<IIII", *b) for b in blocks)
    htpos, btpos = pos, pos + len(htb)
    total = btpos + len(btb)
    hdr = struct.pack("<4sIIHHIIII", b"MPQ\x1a", 32, total, 0, 3, htpos, btpos, hn, len(blocks))
    return (prefix + hdr + body + encrypt(w, htb, w.HASH_TABLE_KEY) + encrypt(w, btb, w.BLOCK_TABLE_KEY))


def good_map(tag=b"a"):
    data = b"x" * 300 + tag
    return make_mpq([("war3map.w3i", b"i" + tag, 0, 2), ("war3map.j", data, 0, len(data))])   # the relay wants both


def bomb_map():
    sector = b"\x02" + zlib.compress(b"\0" * (3 << 20))          # 3 MiB from a ~3 KB sector that claims 4096
    data = struct.pack("<2I", 8, 8 + len(sector)) + sector
    return make_mpq([("war3map.j", data, 0x200, 4096)])


class TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mapshare")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cache = os.path.join(self.tmp, "cache")
        os.makedirs(self.cache)

    def install(self, name="w3"):
        root = os.path.join(self.tmp, name)
        os.makedirs(os.path.join(root, "Maps"))
        for fn in ("war3.mpq", "War3x.mpq"):
            with open(os.path.join(root, fn), "wb") as fh:
                fh.write(b"MPQ\x1a" + b"\0" * 28)
        return root

    def relay_server(self, **kw):
        cfg = relay.Config(data=os.path.join(self.tmp, "relaydata"), **kw)
        srv = relay.make_server(cfg, "127.0.0.1", 0, log=lambda m: None)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))
        return srv, "http://127.0.0.1:%d" % srv.server_address[1]


class Client(TmpCase):
    def test_round_trip_through_the_relay(self):
        srv, url = self.relay_server()
        parts = mapshare.parse_relay_url(url)
        d = good_map()
        p = os.path.join(self.tmp, "m.w3x")
        open(p, "wb").write(d)
        self.assertEqual(mapshare.relay_head(parts, sha(d)), 404)
        self.assertEqual(mapshare.relay_put(parts, sha(d), p), 201)
        self.assertEqual(mapshare.relay_head(parts, sha(d)), 200)
        self.assertEqual(open(os.path.join(srv.store.dir, sha(d)), "rb").read(), d)

    def test_sha_mismatch_and_format_rejected(self):
        _srv, url = self.relay_server()
        parts = mapshare.parse_relay_url(url)
        p = os.path.join(self.tmp, "m.w3x")
        open(p, "wb").write(good_map())
        self.assertEqual(mapshare.relay_put(parts, sha(b"other"), p), 422)
        open(p, "wb").write(b"PK\x03\x04" + b"\0" * 2000)
        self.assertEqual(mapshare.relay_put(parts, sha(open(p, "rb").read()), p), 415)

    def test_oversize_rejected_by_the_relay(self):
        _srv, url = self.relay_server(max_bytes=1000)
        p = os.path.join(self.tmp, "m.w3x")
        open(p, "wb").write(good_map() + b"\0" * 5000)
        self.assertEqual(mapshare.relay_put(mapshare.parse_relay_url(url), sha(open(p, "rb").read()), p), 413)

    def test_url_rules(self):
        self.assertIsNone(mapshare.parse_relay_url(""))
        self.assertIsNone(mapshare.parse_relay_url("http://example.org"))        # plain http only for loopback
        self.assertIsNone(mapshare.parse_relay_url("ftp://example.org"))
        self.assertEqual(mapshare.parse_relay_url("https://r.example.org/base/")[3], "/base")


class FakeRelay(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    cfg = {}

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self.go(False)

    def do_GET(self):
        self.go(True)

    def go(self, body):
        c = FakeRelay.cfg
        c["heads"] = c.get("heads", 0) + (0 if body else 1)
        if not body and c["heads"] <= c.get("n404", 0):
            self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers(); return
        data = c["data"]
        self.send_response(200)
        self.send_header("Content-Length", str(c.get("claim", len(data))))
        if c.get("disp"):
            self.send_header("Content-Disposition", c["disp"])
        self.end_headers()
        if body:
            self.wfile.write(data)


class Fetch(TmpCase):
    def fake(self, data, **kw):
        FakeRelay.cfg = dict(data=data, **kw)
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeRelay)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))
        return "http://127.0.0.1:%d" % srv.server_address[1]

    def inst(self):
        return [{"root": self.install(), "kind": "mpq", "version": "126"}]

    def test_fetch_polls_then_stores(self):
        d = good_map(b"f")
        url = self.fake(d, n404=2)
        inst = self.inst()
        seen = []
        p = mapshare.fetch_map(url, sha(d)[:16], inst, self.cache, lambda pc, ph: seen.append((pc, ph)),
                               sleep=lambda s: None, poll=0)
        self.assertEqual(p, os.path.join(inst[0]["root"], "Maps", "Download", "GSVR", "sha-" + sha(d)[:16] + ".w3x"))
        self.assertEqual(open(p, "rb").read(), d)
        self.assertEqual(seen[0], (0, "waiting"))
        self.assertIn((100, "download"), seen)
        self.assertEqual(os.listdir(os.path.dirname(p)), ["sha-" + sha(d)[:16] + ".w3x"])          # no .part left

    def test_fetch_keeps_the_original_name(self):
        d = good_map(b"n")
        url = self.fake(d, disp="attachment; filename=\"Gr_ner.w3x\"; filename*=UTF-8''Gr%C3%BCner%20Wald%20V1.51.w3x")
        inst = self.inst()
        p = mapshare.fetch_map(url, sha(d)[:16], inst, self.cache, sleep=lambda s: None, poll=0)
        self.assertEqual(os.path.basename(p), "Grüner Wald V1.51 [%s].w3x" % sha(d)[:8])
        self.assertEqual(open(p, "rb").read(), d)
        self.assertEqual(len(os.listdir(os.path.dirname(p))), 1)

    def test_bad_name_falls_back_to_sha_name(self):
        d = good_map(b"o")
        inst = self.inst()
        for disp in ("attachment; filename*=UTF-8''..%2F..%2Fevil.exe", "attachment", ""):
            url = self.fake(d, disp=disp)
            p = mapshare.fetch_map(url, sha(d)[:16], inst, self.cache, sleep=lambda s: None, poll=0)
            self.assertEqual(os.path.basename(p), "sha-%s.w3x" % sha(d)[:16])
        url = self.fake(d, disp="attachment; filename*=UTF-8''..%2F..%2Fevil.w3x")
        p = mapshare.fetch_map(url, sha(d)[:16], inst, self.cache, sleep=lambda s: None, poll=0)
        self.assertEqual(os.path.basename(p), "evil [%s].w3x" % sha(d)[:8])       # traversal -> basename, stays in the folder
        self.assertEqual(os.path.basename(os.path.dirname(p)), "GSVR")

    def test_timeout_keeps_not_found(self):
        url = self.fake(b"", n404=10 ** 6)
        t = [0.0]
        with self.assertRaises(LookupError):
            mapshare.fetch_map(url, "0" * 16, self.inst(), self.cache, sleep=lambda s: t.__setitem__(0, t[0] + 2),
                               clock=lambda: t[0])
        self.assertGreaterEqual(t[0], 60)

    def test_hash_mismatch_rejected_nothing_stored(self):
        d = good_map(b"g")
        url = self.fake(d)
        inst = self.inst()
        with self.assertRaises(ValueError):
            mapshare.fetch_map(url, "1" * 16, inst, self.cache, sleep=lambda s: None)
        self.assertEqual(os.listdir(os.path.join(inst[0]["root"], "Maps", "Download", "GSVR")), [])

    def test_oversize_download_rejected(self):
        d = good_map(b"h")
        url = self.fake(d, claim=mapshare.MAX_MAP_BYTES + 1)
        with self.assertRaises(ValueError):
            mapshare.fetch_map(url, sha(d)[:16], self.inst(), self.cache, sleep=lambda s: None)

    def test_not_a_map_rejected(self):
        d = b"PK\x03\x04" + b"\0" * 3000
        url = self.fake(d)
        inst = self.inst()
        with self.assertRaises(ValueError):
            mapshare.fetch_map(url, sha(d)[:16], inst, self.cache, sleep=lambda s: None)
        self.assertEqual(os.listdir(os.path.join(inst[0]["root"], "Maps", "Download", "GSVR")), [])

    def test_unwritable_maps_folder_falls_back_to_data_dir(self):
        d = good_map(b"i")
        url = self.fake(d)
        inst = [{"root": os.path.join(self.tmp, "nope"), "kind": "mpq", "version": "126"}]
        os.makedirs(inst[0]["root"])
        open(os.path.join(inst[0]["root"], "Maps"), "w").write("a file, not a folder")   # makedirs under it fails
        p = mapshare.fetch_map(url, sha(d)[:16], inst, self.cache, sleep=lambda s: None)
        self.assertEqual(os.path.dirname(p), mapshare.download_fallback_dir(self.cache))


class Names(TmpCase):
    def test_sanitize_matches_relay(self):
        for raw in ("Wintermaul%20V1.51.w3x", "../../etc/evil.w3x", "a%00b.w3x", "map.zip", "a" * 117 + ".w3x", "Gr%C3%BCn.W3M",
                    "a..b.w3x", ".w3x", "C:\\x\\y.w3x", None, "", "%FF.w3x", 'a<b>"c.w3x'):
            self.assertEqual(mapshare.sanitize_name(raw), relay.sanitize_name(raw), raw)

    def test_share_sends_the_original_name_and_guest_stores_it(self):
        srv, url = self.relay_server()
        parts = mapshare.parse_relay_url(url)
        d = good_map(b"s")
        p = os.path.join(self.tmp, "Wintermaul V1.51.w3x")
        open(p, "wb").write(d)
        self.assertEqual(mapshare.relay_put(parts, sha(d), p), 201)
        self.assertEqual(srv.store.get_name(sha(d)), "Wintermaul V1.51.w3x")
        inst = [{"root": self.install("guest"), "kind": "mpq", "version": "126"}]
        got = mapshare.fetch_map(url, sha(d)[:16], inst, self.cache, sleep=lambda s: None, poll=0)
        self.assertEqual(os.path.basename(got), "Wintermaul V1.51 [%s].w3x" % sha(d)[:8])

    def test_renamed_download_is_served_for_its_sha_key(self):
        d = good_map(b"w")
        root = self.install("guest")
        dl = os.path.join(root, "Maps", "Download", "GSVR")
        os.makedirs(dl)
        fn = "Wintermaul V1.51 [%s].w3x" % sha(d)[:8]
        open(os.path.join(dl, fn), "wb").write(d)
        own = {"war3": {"appid": 0, "name": "Warcraft III", "allowed": True, "game_root": root,
                        "installs": [{"root": root, "kind": "mpq", "version": "126"}]}}
        idx = war3.build_index(own, [], self.cache)["war3"]["maps"]
        key = "sha-" + sha(d)[:16]
        self.assertIn(key, idx)
        self.assertEqual(os.path.basename(idx[key]["path"]), fn)
        self.assertEqual(len([k for k in idx if k != "__glue"]), 1)


class Validate(TmpCase):
    def check(self, data):
        p = os.path.join(self.tmp, "v.w3x")
        open(p, "wb").write(data)
        return mapshare.validate_map(p)

    def test_good_map_passes(self):
        self.check(good_map())

    def test_bad_magic(self):
        with self.assertRaises(ValueError):
            self.check(b"\0" * 2000)

    def test_zlib_bomb(self):
        w = mapshare.harden_mpq()                        # the limit lives in w3mpq now (its own _bounded)
        old, w.MAX_CALL_BYTES = w.MAX_CALL_BYTES, 1 << 20
        try:
            with self.assertRaises(ValueError) as cm:
                self.check(bomb_map())
            self.assertIn("bomb", str(cm.exception))
        finally:
            w.MAX_CALL_BYTES = old

    def test_declared_size_and_ratio(self):
        with self.assertRaises(ValueError):
            self.check(make_mpq([("war3map.j", b"x" * 64, 0, 300 << 20)]))      # 300 MB claimed
        with self.assertRaises(ValueError):
            self.check(make_mpq([("war3map.j", b"x" * 64, 0, 100 << 20)]))      # 100 MB from 64 bytes: ratio
        with self.assertRaises(ValueError):
            self.check(make_mpq([("war3map.j", b"x" * 64, 0, 100 << 20), ("war3map.w3e", b"y" * 64, 0, 100 << 20),
                                 ("war3map.w3i", b"y" * 64, 0, 100 << 20), ("war3map.doo", b"y" * 64, 0, 100 << 20),
                                 ("war3map.w3u", b"y" * 64, 0, 100 << 20), ("war3map.w3t", b"y" * 64, 0, 100 << 20)]))

    def test_size_cap(self):
        with self.assertRaises(ValueError):
            self.check(good_map() + b"\0" * (mapshare.MAX_MAP_BYTES + 1))


class Stock(TmpCase):
    def setUp(self):
        super().setUp()
        self.root = self.install()
        self.inst = [{"root": self.root, "kind": "mpq", "version": "126"}]
        self.stock = os.path.join(self.root, "Maps", "FrozenThrone", "(2)Blizz.w3x")
        self.fan = os.path.join(self.root, "Maps", "Download", "My Fan Map.w3x")
        self.copy = os.path.join(self.tmp, "elsewhere", "Renamed Blizz.w3x")
        for p, tag in ((self.stock, b"s"), (self.fan, b"f")):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "wb").write(good_map(tag))
        os.makedirs(os.path.dirname(self.copy))
        shutil.copy(self.stock, self.copy)

    def test_is_stock_by_folder_and_hash(self):
        shas = mapshare.StockSet(self.cache).refresh(self.inst)
        ssha, fsha = sha(open(self.stock, "rb").read()), sha(open(self.fan, "rb").read())
        self.assertIn(ssha, shas)
        self.assertTrue(mapshare.is_stock(self.stock, self.inst, ssha))                 # by folder
        self.assertTrue(mapshare.is_stock(self.copy, self.inst, ssha, shas))            # copied away: by hash
        self.assertFalse(mapshare.is_stock(self.fan, self.inst, fsha, shas))
        self.assertTrue(mapshare.is_stock("casc:maps\\x.w3x", self.inst, "0" * 64))     # CASC-internal path

    def test_sharer_uploads_fan_map_never_stock(self):
        srv, url = self.relay_server()
        logs = []
        sh = mapshare.Sharer(url, self.cache, logs.append)
        ssha, fsha = sha(open(self.stock, "rb").read()), sha(open(self.fan, "rb").read())
        for sha_, path in ((ssha, self.stock), (ssha, self.copy), (fsha, self.fan)):
            t = sh.maybe_share(sha_, path, self.inst)
            if t:
                t.join(10)
        self.assertEqual(sorted(os.listdir(srv.store.dir)), [fsha, fsha + ".name"])   # map + name sidecar
        # HEAD finds it now: a second host does not upload again (no PUT -> no change of the stored file's mtime needed)
        sh2 = mapshare.Sharer(url, self.cache, logs.append)
        sh2.maybe_share(fsha, self.fan, self.inst).join(10)
        self.assertTrue(any("stock map" in l for l in logs))

    def test_sharer_off_without_url(self):
        self.assertIsNone(mapshare.Sharer("", self.cache).maybe_share(sha(b"x"), self.fan, self.inst))


class StateFetch(TmpCase):
    """companion.State.fetch_from_relay against a real relay: status 'fetching' with percent, ask mode, off."""
    def setUp(self):
        super().setUp()
        import companion
        self.companion = companion
        self.root = self.install("guest")
        self.host = good_map(b"H")
        self.key = sha(self.host)[:16]
        self.srv, self.url = self.relay_server()
        open(os.path.join(self.srv.store.dir, sha(self.host)), "wb").write(self.host)

    def state(self, mode="on", url=None):
        a = argparse.Namespace(prebuilt_dir=None, game_root=self.root, mods=None, steam_root=None, no_war3=False,
                               war3_root=self.root, war3_maps=None, cache_dir=self.cache, world_allow=None,
                               relay_url=self.url if url is None else url, map_autodownload=mode, wait_max=0,
                               vrchat_log_dir=None)
        st = self.companion.State(a)
        self.states = []
        orig = st._set
        st._set = lambda slot, **kw: (self.states.append((kw.get("state"), kw.get("percent"), kw.get("phase"))), orig(slot, **kw))[1]
        return st

    def miss(self):
        return RuntimeError("map 'war3/sha-%s' not found" % self.key)

    def test_fetch_then_index_has_it(self):
        st = self.state()
        self.assertNotIn("sha-" + self.key, st.index["war3"]["maps"])
        self.assertTrue(st.fetch_from_relay("current", "war3/sha-" + self.key, self.miss()))
        self.assertIn("sha-" + self.key, st.index["war3"]["maps"])
        self.assertTrue(any(s[0] == "fetching" and s[2] == "download" for s in self.states), self.states)
        self.assertIn(("fetching", 100, "download"), self.states)
        self.assertEqual(st.slots["current"]["state"], "building")
        self.assertIsNone(st._status_locked()["percent"])

    def test_status_while_fetching(self):
        st = self.state()
        st._set("current", state="fetching", percent=42, phase="download")
        s = st.status_json()
        self.assertEqual((s["state"], s["percent"], s["phase"]), ("fetching", 42, "download"))

    def test_off_and_no_relay_keep_not_found(self):
        self.assertFalse(self.state("off").fetch_from_relay("current", "war3/sha-" + self.key, self.miss()))
        self.assertFalse(self.state("on", url="").fetch_from_relay("current", "war3/sha-" + self.key, self.miss()))
        self.assertFalse(self.state().fetch_from_relay("current", "war3/(2)a", RuntimeError("map 'war3/(2)a' not found")))

    def test_unknown_on_relay_fails_soft_then_backs_off(self):
        st = self.state()
        st.sharer.parts = mapshare.parse_relay_url(self.url)
        old = mapshare.FETCH_TIMEOUT_S
        orig = mapshare.fetch_map
        mapshare.fetch_map = lambda *a, **k: orig(*a, **dict(k, timeout=0))
        try:
            key = "f" * 16
            self.assertFalse(st.fetch_from_relay("current", "war3/sha-" + key, RuntimeError("map x not found")))
            self.assertIn(key, st.fetch_failed)
            n = len(self.states)
            self.assertFalse(st.fetch_from_relay("current", "war3/sha-" + key, RuntimeError("map x not found")))
            self.assertEqual(len(self.states), n, "no second relay round within the back-off")
        finally:
            mapshare.fetch_map = orig

    def test_ask_mode_waits_for_the_answer(self):
        st = self.state("ask")
        res = []
        th = threading.Thread(target=lambda: res.append(st.fetch_from_relay("current", "war3/sha-" + self.key, self.miss())))
        th.start()
        t0 = time.time()
        while time.time() - t0 < 5 and st.status_json()["ask"] != [self.key]:
            time.sleep(0.05)
        self.assertEqual(st.status_json()["ask"], [self.key])
        self.assertEqual(st.status_json()["phase"], "ask")
        self.assertTrue(st.answer_ask(self.key, True))
        th.join(20)
        self.assertEqual(res, [True])
        self.assertEqual(st.status_json()["ask"], [])
        self.assertIn("sha-" + self.key, st.index["war3"]["maps"])

    def test_ask_mode_declined_once_per_sha(self):
        st = self.state("ask")
        th = threading.Thread(target=lambda: st.fetch_from_relay("current", "war3/sha-" + self.key, self.miss()))
        th.start()
        t0 = time.time()
        while time.time() - t0 < 5 and not st.status_json()["ask"]:
            time.sleep(0.05)
        st.answer_ask(self.key, False)
        th.join(20)
        n = len(os.listdir(self.srv.store.dir))
        self.assertFalse(st.fetch_from_relay("current", "war3/sha-" + self.key, self.miss()))     # no second prompt
        self.assertEqual(st.status_json()["ask"], [])
        self.assertFalse(st.answer_ask(self.key, True))
        self.assertEqual(len(os.listdir(self.srv.store.dir)), n)
        self.assertFalse(os.path.isdir(os.path.join(self.root, "Maps", "Download", "GSVR")) and
                         os.listdir(os.path.join(self.root, "Maps", "Download", "GSVR")))


class RealMapValidation(unittest.TestCase):
    """e2e 2026-10-29: the old harden_mpq shim broke zlib maps (every guest fetch failed); validate real maps."""
    def test_real_fan_maps_validate(self):
        import glob
        maps = sorted(glob.glob(os.path.expanduser("~/dev/decomp/warcraft3/input/fanmaps*/*.w3x")))[:6]
        if not maps:
            self.skipTest("no real fan maps on this machine")
        for m in maps:
            mapshare.validate_map(m)                     # raises ValueError on failure


if __name__ == "__main__":
    unittest.main(verbosity=1)
