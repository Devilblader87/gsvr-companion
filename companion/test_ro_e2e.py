"""Ragnarok prototype END TO END, offline (ro-proto): a SYNTHETIC client (data.grf written here: a 16 x 16 tile "prontera" with a blocked wall
column, one RSM box, a Korean-named ground texture, Novice body / head + poring SPR / ACT made of coloured blocks; nothing of the game) ->
ro_conv.api.build (the real converter, map + sprite sections in ONE RPG1 pack) -> rosebuild.Manager flavor RO -> a live companion serving
GET /pack/ro.pack for "[GSVR-REQ] ro=prontera" -> the same bytes the world's GsvrRoFetch downloads. The C# side (the compiled runtime
parsing that pack like the Udon loader + sprite set + test bus) is tools/usharp_offline/ro_e2e.cs, fed by `--write-pack` below (check.sh).

  cd companion && python3 -I test_ro_e2e.py                 (unit tests)
  python3 -I companion/test_ro_e2e.py --write-pack OUT       (writes the fixture pack for ro_e2e.cs, prints one summary line)
"""
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ro_conv  # noqa: E402
from ro_conv import grf, img, sprite as S  # noqa: E402
import test_ro_conv as RC  # noqa: E402  (fixture writers + the Python mirror of the C# loader validators)
import test_ro_sprite as RS  # noqa: E402  (sprite fixture writers)
import rpg1 as P  # noqa: E402
import ro, rosebuild  # noqa: E402

ROOT = os.path.dirname(HERE)
UN = os.path.join(ROOT, "unity", "Assets", "GoldSrcVR")
W = H = 16                  # GND tiles
GW = GH = 32                # GAT cells
WALL_X = 20                 # blocked GAT column x = 20 for y 0..25 (the +x run from the centre is blocked, -x is free)
BRIDGE_Y, BRIDGE_X0, BRIDGE_X1 = 28, 4, 9      # w34 rointerior: GAT 2 m above the GND on these cells (ro_e2e.cs checks the decoded walk surface there)


def _cell_type(cx, cy):
    if cx in (0, GW - 1) or cy in (0, GH - 1):
        return 1
    if cx == WALL_X and cy <= 25:
        return 1
    return 0


def client_files(sprites=True):
    """{grf name: bytes} of the synthetic client (backslash names like the real archive)."""
    lm = bytes([200] * 64) + bytes([10, 20, 30] * 64)
    surfs = [((0, 1, 0, 1), (0, 0, 1, 1), 0, 0, (255, 255, 255, 255))]
    cubes = []
    for ty in range(H):
        for tx in range(W):
            h0, h1 = -0.4 * tx, -0.4 * (tx + 1)          # gentle slope up toward +x (RO heights: negative = up)
            cubes.append((h0, h1, h0, h1, 0, -1, -1))
    gnd = RC.gnd_bytes(W, H, [RC.KTEX], surfs, cubes, [lm])
    hts, types = [], []
    for cy in range(GH):
        for cx in range(GW):
            a, b = -0.4 * (cx / 2.0), -0.4 * ((cx + 1) / 2.0)
            if cy == BRIDGE_Y and BRIDGE_X0 <= cx <= BRIDGE_X1:
                a, b = a - 10.0, b - 10.0          # a 2 m bridge deck (an RSM model in RO: only the GAT carries it) -> GWLK walk surface + raised list
            hts.append((a, b, a, b))
            types.append(_cell_type(cx, cy))
    gat = RC.gat_bytes(GW, GH, hts, types)
    rsw = RC.rsw_bytes('prontera.gnd', 'prontera.gat', [('box.rsm', (-30.0, 0.0, -30.0), (0, 0, 0), (1, 1, 1))], water=5.0)
    files = {'data\\prontera.rsw': rsw, 'data\\prontera.gnd': gnd, 'data\\prontera.gat': gat,
             'data\\texture\\' + RC.KTEX: img.write_bmp24(16, 16, RC.solid(16, 16, (40, 160, 40))),
             'data\\texture\\box.bmp': img.write_bmp24(8, 8, RC.solid(8, 8, (150, 110, 60))),
             'data\\model\\box.rsm': RC.rsm_bytes(['box.bmp'], [RC.cube_node('main', '', 4.0)], 'main')}
    if sprites:
        bs, ba = RS._fixture_body()
        hs, ha = RS._fixture_head()
        ps = S.write_spr([RS._block(3, 3, 2)], palette=RS._pal())
        pa = S.write_act([[dict(layers=[RS._layer(0, -1, 0)], event=-1, anchors=[])]] * 40)
        for base, sb, ab in ((S.PATH_BODY['m'], bs, ba), (S.PATH_HEAD['m'], hs, ha), (S.PATH_PORING, ps, pa)):
            files[base.replace('/', '\\') + '.spr'] = sb
            files[base.replace('/', '\\') + '.act'] = ab
    return files


def make_client(folder, sprites=True):
    os.makedirs(folder, exist_ok=True)
    grf.write_grf(os.path.join(folder, 'data.grf'), client_files(sprites))
    return folder


def rd(*p):
    with open(os.path.join(*p), encoding="utf-8") as fh:
        return fh.read()


class Api(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="roe2e")
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        self.client = make_client(os.path.join(self.work, "Ragnarok"))

    def test_contract(self):
        # the names rosebuild.Manager (flavor RO) and rofind use (ro_conv_stub.py header)
        for n in ("VERSION", "PACKS", "check_install", "install_files", "build", "Cancelled", "BuildError", "pack_version"):
            self.assertTrue(hasattr(ro_conv, n), n)
        self.assertEqual(next(iter(ro_conv.PACKS)), "prontera")
        self.assertEqual(ro_conv.PACKS["prontera"]["kind"], "zone")
        self.assertEqual(ro_conv.check_install(self.client), (True, "ok"))
        self.assertEqual(ro_conv.install_files(self.client), ["data.grf"])
        ok, why = ro_conv.check_install(self.work)
        self.assertFalse(ok)
        self.assertNotIn(self.work, why)
        bad = os.path.join(self.work, "bad")
        os.makedirs(bad)
        with open(os.path.join(bad, "DATA.GRF"), "wb") as fh:
            fh.write(b"not a grf at all")
        ok, why = ro_conv.check_install(bad)
        self.assertFalse(ok)
        self.assertIn("not a GRF", why)
        self.assertTrue(issubclass(ro_conv.BuildError, Exception) and issubclass(ro_conv.Cancelled, Exception))

    def test_build_one_pack_map_and_sprites(self):
        out = os.path.join(self.work, "ro", "ro__prontera.pack")
        prog = []
        res = ro_conv.build("prontera", self.client, out, progress=lambda s, f: prog.append((s, f)))
        self.assertFalse(os.path.exists(out + ".part"))
        self.assertTrue(res["sprites"], res)
        with open(out, "rb") as fh:
            pack = fh.read()
        self.assertEqual(ro.validate(out), len(pack))                    # the companion route accepts it
        p, a, terr, texs, mats, chunks = RC.validate_like_loader(pack)  # the C# loader validators (Python mirror)
        self.assertEqual((a['grid_w'], a['grid_h']), (GW, GH))
        self.assertGreater(len(terr), 0)
        self.assertGreater(len(chunks), 0)
        secs = p['sections']
        self.assertIn(S.TAG_RSAT, secs)
        self.assertIn(S.TAG_RSAN, secs)
        rs = S.dec_rsan(secs[S.TAG_RSAN]['data'])
        self.assertEqual([s['name'] for s in rs['sprites']], ["body_m", "head_m", "poring", "hi_1_m", "bi_m"])     # the scene's GsvrRoSprite bodyName / headName; w43 equip appends the indexed hair 1 / body
        self.assertGreater(rs['ref_height_px'], 0)
        # start = walkable cell nearest the centre, a straight run of 10 walkable cells exists (GsvrRoTest.LFindRun on the same grid in ro_e2e.cs)
        cells = secs[b'CELL']['data']
        sx, sz = int(a['start_x'] // a['cell_size']), int(a['start_z'] // a['cell_size'])
        self.assertTrue(cells[sz * GW + sx])
        self.assertFalse(cells[5 * GW + WALL_X])
        self.assertFalse(cells[0])
        # progress: monotonic, ends at 1, the stages of api.build
        fr = [f for _s, f in prog]
        self.assertEqual(fr, sorted(fr))
        self.assertAlmostEqual(fr[-1], 1.0)
        self.assertTrue({"grf", "sprites", "terrain", "objects", "write"} <= set(s for s, _f in prog))

    def test_cancel_and_errors(self):
        out = os.path.join(self.work, "ro", "ro__prontera.pack")
        n = [0]

        def cancel():
            n[0] += 1
            return n[0] > 5
        with self.assertRaises(ro_conv.Cancelled):
            ro_conv.build("prontera", self.client, out, cancel=cancel)
        self.assertFalse(os.path.exists(out) or os.path.exists(out + ".part"))
        with self.assertRaises(ro_conv.BuildError):
            ro_conv.build("geffen", self.client, out)
        with self.assertRaises(ro_conv.BuildError) as cm:
            ro_conv.build("prontera", self.work, out)
        self.assertNotIn(self.work, str(cm.exception))

    def test_client_without_sprites_still_gets_the_map(self):
        c2 = make_client(os.path.join(self.work, "NoSprites"), sprites=False)
        out = os.path.join(self.work, "ro2", "ro__prontera.pack")
        res = ro_conv.build("prontera", c2, out)
        self.assertFalse(res["sprites"])
        self.assertTrue(any(w.startswith("sprites:") for w in res["warnings"]), res["warnings"])
        with open(out, "rb") as fh:
            p = P.read_pack(fh.read())
        self.assertNotIn(S.TAG_RSAN, p['sections'])

    def test_cli_writes_the_same_kind_of_pack(self):
        from ro_conv.__main__ import main as cli
        out = os.path.join(self.work, "cli", "ro__prontera.pack")
        self.assertEqual(cli(["build", "--grf", self.client, "--map", "prontera", "--out", out]), 0)
        with open(out, "rb") as fh:
            self.assertIn(S.TAG_RSAN, P.read_pack(fh.read())['sections'])

    def test_manager_real_converter(self):
        rdir = os.path.join(self.work, "ro")
        ups = []
        m = rosebuild.Manager(rdir, conv=ro_conv, log=lambda s: None, on_update=lambda n, f: ups.append((n, dict(f))), flavor=rosebuild.RO)
        self.addCleanup(m.stop)
        m.set_install(self.client, prebuild=False)
        r = m.request("prontera")
        if r["state"] == "building":
            t0 = time.time()
            while time.time() - t0 < 60 and not any(n == "prontera" and f.get("state") in ("ready", "error") for n, f in ups):
                time.sleep(0.05)
            r = [f for n, f in ups if n == "prontera"][-1]
        self.assertEqual(r["state"], "ready", r)
        self.assertTrue(os.path.isfile(os.path.join(rdir, "ro__prontera.pack")))
        self.assertEqual(m.request("prontera")["state"], "ready")                  # cached (data.grf size + mtime)


class WorldContract(unittest.TestCase):
    """the companion route == the world fetch (source level; the live part is Live below)"""
    @unittest.skipUnless(os.path.isdir(UN), "no repo checkout")
    def test_names_line_up(self):
        f = rd(UN, "Scripts", "GsvrRoFetch.cs")
        b = rd(UN, "Editor", "GsvrSceneBuilder.Ragnarok.cs")
        c = rd(HERE, "companion.py")
        self.assertIn('"[GSVR-REQ] ro="', f)
        self.assertIn('"ro", TokenType.DataDictionary', f)
        self.assertIn('CompanionUrls("/pack/ro.pack")', b)
        self.assertIn('"/pack/ro.pack"', c)
        self.assertIn('if "ro" in cmd', c)
        self.assertIn('"ro": ro.public(self.ro)', c)
        self.assertIn('fetch.packName = zone', b)
        self.assertIn('RoZoneDefault = "prontera"', b)
        self.assertIn('sp.bodyName = "body_m"; sp.headName = "head_m"', b)
        self.assertEqual(ro_conv.api.SPRITE_SEX, "m")


class Live(unittest.TestCase):
    def test_live_companion_real_converter(self):
        import selftest as st
        work = tempfile.mkdtemp(prefix="roe2elive")
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)
        pdir, rdir, logdir = (os.path.join(work, x) for x in ("pre", "ro", "log"))
        for d in (pdir, logdir):
            os.makedirs(d)
        client = make_client(os.path.join(work, "Gravity", "Ragnarok"))
        with open(os.path.join(pdir, "valve__c1a0.bin"), "wb") as fh:
            fh.write(b"GSV1" + b"PACK" * 10)
        lg = os.path.join(logdir, "output_log_2026-11-25_10-00-00.txt")
        st.append(lg, "start")
        port = st.free_port()
        env = dict(os.environ)
        env.pop("GSVR_RO_CONV", None)                                            # the REAL converter (companion/ro_conv)
        cmd = [sys.executable, "-I", os.path.join(HERE, "companion.py"), "--prebuilt-dir", pdir, "--vrchat-log-dir", logdir,
               "--cache-dir", os.path.join(work, "cache"), "--ro-dir", rdir, "--ro-install", client, "--no-rose-find",
               "--no-ro-find", "--world-allow", "wrld_ok", "--port", str(port)]
        out = open(os.path.join(work, "companion.out"), "w")
        self.addCleanup(out.close)
        proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, env=env)
        self.addCleanup(proc.wait, 10)
        self.addCleanup(proc.terminate)
        self.assertIsNotNone(st.wait_status(port, lambda s: True, 20))
        st.append(lg, "Joining wrld_ok")
        st.wait_status(port, lambda s: s["world_ok"], 10)
        st.append(lg, "[GSVR-REQ] ro=prontera req=r77")                           # GsvrRoFetch.LogRequest
        s = st.wait_status(port, lambda s: s["ro"]["req"] == "r77" and s["ro"]["state"] in ("ready", "error"), 90)
        self.assertIsNotNone(s, rd(work, "companion.out")[-2000:])
        self.assertEqual(s["ro"]["state"], "ready", s["ro"])
        code, data = st.http(port, "/pack/ro.pack")
        self.assertEqual(code, 200)
        self.assertEqual(len(data), s["ro"]["size"])                              # GsvrRoFetch compares status.ro.size with the download
        secs = P.read_pack(data)['sections']
        for t in (b'AREA', b'CELL', b'TERR', b'OMSH', S.TAG_RSAT, S.TAG_RSAN):
            self.assertIn(t, secs)
        self.assertNotIn(client, json.dumps(s))


def write_pack(out):
    work = tempfile.mkdtemp(prefix="roe2efix")
    try:
        client = make_client(os.path.join(work, "Ragnarok"))
        res = ro_conv.build("prontera", client, out)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("ro e2e fixture: %s %d bytes, grid %s, start cell %s, %d chunks, sprites %s" % (
        os.path.basename(out), res["bytes"], res["grid"], res["start_cell"], res["chunks"], res["sprites"]))
    return 0


# all maps loaded (docs/design/ro_multimap.md): a second synthetic map "prt_in" (same ground, map only: no sprite set) and a warp table for the two
# synthetic grids (32 x 32 cells, blocked border + wall column x = 20), read by ro_e2e.cs like Editor/ro_warps.txt
E2E_WARPS = """# synthetic warps for ro_e2e.cs (same columns as unity/Assets/GoldSrcVR/Editor/ro_warps.txt)
prontera 10 12 1 1 prt_in 6 6 e2e_in
prt_in 25 25 1 1 prontera 12 20 e2e_out
prt_in 4 25 1 1 prt_in 10 25 e2e_room
"""


def write_packs(out0, out1, warps):
    """the start map pack (map + sprites) and a map-only second pack of the same synthetic client, plus the warp table"""
    work = tempfile.mkdtemp(prefix="roe2efix2")
    try:
        files = client_files(True)
        files['data\\prt_in.rsw'] = RC.rsw_bytes('prt_in.gnd', 'prt_in.gat', [('box.rsm', (-30.0, 0.0, -30.0), (0, 0, 0), (1, 1, 1))])
        files['data\\prt_in.gnd'] = files['data\\prontera.gnd']
        files['data\\prt_in.gat'] = files['data\\prontera.gat']
        client = os.path.join(work, "Ragnarok")
        os.makedirs(client)
        grf.write_grf(os.path.join(client, 'data.grf'), files)
        r0 = ro_conv.build("prontera", client, out0)
        r1 = ro_conv.build("prt_in", client, out1)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    with open(warps, "w", encoding="utf-8") as fh:
        fh.write(E2E_WARPS)
    print("ro e2e multi fixture: %s %d bytes (sprites %s), %s %d bytes (sprites %s), warps %s" % (
        os.path.basename(out0), r0["bytes"], r0["sprites"], os.path.basename(out1), r1["bytes"], r1["sprites"], os.path.basename(warps)))
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--write-pack":
        sys.exit(write_pack(sys.argv[2]))
    if len(sys.argv) == 5 and sys.argv[1] == "--write-packs":
        sys.exit(write_packs(sys.argv[2], sys.argv[3], sys.argv[4]))
    unittest.main()
