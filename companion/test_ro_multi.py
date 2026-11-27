"""RO prototype "all maps loaded" (docs/design/ro_multimap.md), companion side: the five map packs, their build order, the per map status rows
and the sprite rule (only the start map's pack carries RSAT / RSAN). Stdlib only, no game data (synthetic client of test_ro_e2e.py).

  cd companion && python3 -I test_ro_multi.py
"""
import os
import re
import shutil
import sys
import tempfile
import time
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ro_conv  # noqa: E402
from ro_conv import api, grf, culvert, dungeons, jobmaps, maps2, sprite as S, towns  # noqa: E402
import ro_conv_stub  # noqa: E402
import rosebuild  # noqa: E402
import rpg1 as P  # noqa: E402
import companion  # noqa: E402
import test_ro_e2e as E  # noqa: E402
import test_ro_conv as RC  # noqa: E402

ROOT = os.path.dirname(HERE)
UN = os.path.join(ROOT, "unity", "Assets", "GoldSrcVR")
PROTO = ["prontera", "prt_in", "prt_church", "prt_fild08", "izlude"]
MAPS = list(towns.NAMES) + list(culvert.NAMES)                    # towns lane (docs/design/ro_towns.md): the prototype maps keep ids 0..4, the towns + fields follow


def rd(*p):
    with open(os.path.join(*p), encoding="utf-8") as fh:
        return fh.read()


def ro_like_stub():
    """the stub converter with the REAL ro_conv map list + order hooks (fast builds, no game data)"""
    m = types.ModuleType("ro_conv_multi_stub")
    for n in ("VERSION", "check_install", "install_files", "build", "Cancelled", "BuildError", "pack_version", "CALLS"):
        setattr(m, n, getattr(ro_conv_stub, n))
    m.PACKS = dict(api.PACKS)
    m.PREBUILD_ORDER = api.PREBUILD_ORDER
    m.NO_PREBUILD = api.NO_PREBUILD
    m.warm_after = api.warm_after
    return m


class Order(unittest.TestCase):
    def test_map_list(self):
        self.assertEqual([m for m in api.PACKS if m not in dungeons.NAMES and m not in jobmaps.NAMES and m not in maps2.NAMES], MAPS)          # + the w46 maps2 lane (test_ro_maps2.py), the dungeons lane's maps after them (test_ro_dungeons.py), then the w41 job-test maps (test_ro_jobs1.py)
        self.assertEqual(MAPS[:5], PROTO)                                     # the prototype's ids never move
        self.assertEqual(len(MAPS), 31)                                       # 27 towns + 4 culvert floors (w39: the dungeons' 12 follow as ids 31..42)
        self.assertEqual(list(api.MAP_ORDER[:len(MAPS)]), MAPS)
        self.assertEqual([m for m in ro_conv.PACKS if m not in dungeons.NAMES and m not in jobmaps.NAMES and m not in maps2.NAMES], MAPS)                          # the package exports the api names
        self.assertEqual(api.SPRITE_PACK, "prontera")
        self.assertTrue(all(p["kind"] == "zone" for p in api.PACKS.values()))     # never two RO builds at once (memory)
        # towns: after a map the world asks for the maps resident around it (cluster + warp neighbours), in id order
        self.assertEqual(api.warm_after("prontera"), PROTO[1:] + ["prt_fild05"])
        self.assertEqual(api.warm_after("prt_fild08"), ["izlude", "prt_fild05", "moc_fild01"])
        self.assertEqual(api.warm_after("izlude"), ["prt_fild05"])
        self.assertEqual(api.warm_after("geffen"), ["geffen_in"])
        self.assertEqual(api.warm_after("payon"), ["payon_in01", "payon_in02", "payon_in03", "pay_arche"])
        self.assertEqual(api.warm_after("morocc_in"), [])
        self.assertEqual(api.warm_after("glast_01"), [])

    def test_audio_packs_merged_into_the_manager_table(self):
        # w39 wiring: PACKS.update(AUDIO_PACKS) in rosebuild - as a VIEW (ro_conv.PACKS stays the map list), the manager builds + serves snd_* too
        mc = rosebuild.merge_audio(ro_conv)
        self.assertIsNot(mc, ro_conv)
        self.assertEqual(set(mc.PACKS), set(api.PACKS) | set(api.AUDIO_PACKS) | set(getattr(api, 'UI_PACKS', {})) | set(getattr(api, 'NPC_PACKS', {})))          # w42 ui-a: + the 'roui' interface pack; w43: + the npc_<town> sprite packs
        self.assertTrue(all(n not in api.PACKS for n in api.AUDIO_PACKS))            # the module's own table is untouched
        self.assertEqual(mc.PACKS["snd_common"]["kind"], "audio")
        self.assertEqual(rosebuild.conv_version(mc, "snd_common"), api.pack_version("snd_common"))   # attributes delegate
        self.assertIs(rosebuild.merge_audio(mc), mc)
        order = rosebuild.prebuild_order(mc, list(mc.PACKS))
        self.assertEqual([n for n in order if n not in api.AUDIO_PACKS], [n for n in api.PREBUILD_ORDER])      # maps first, in order
        self.assertEqual(order[-len(api.AUDIO_PACKS):], list(api.AUDIO_ORDER))                                   # then the audio packs
        self.assertFalse(any(n in order for n in api.NO_PREBUILD))
        self.assertIsNone(rosebuild.merge_audio(None))
        stub = ro_like_stub()
        self.assertIs(rosebuild.merge_audio(stub), stub)                                                         # no AUDIO_PACKS: unchanged
        m = rosebuild.Manager(os.path.join(tempfile.mkdtemp(prefix="roaud"), "ro"), conv=ro_conv, log=lambda s: None, flavor=rosebuild.RO)
        self.addCleanup(m.stop)
        self.assertIn("snd_prontera", m.packs())
        self.assertNotIn("snd_prontera", rosebuild.Manager(os.path.join(tempfile.mkdtemp(prefix="roaud"), "rose"), conv=ro_like_stub(), log=lambda s: None, flavor=rosebuild.RO).packs())

    def test_prebuild_order_hooks(self):
        self.assertEqual(rosebuild.prebuild_order(ro_conv, list(reversed(MAPS)), zone="izlude"), MAPS)       # fixed: start map first, whatever was asked last
        self.assertEqual(rosebuild.warm_after(ro_conv, "prt_in"), PROTO[2:] + ["prt_fild05"])
        # a converter without the hooks (ROSE) keeps the old rule: lobby, avatar, likely zone, then PACKS order
        rose_like = types.SimpleNamespace(PACKS={"login": {"kind": "lobby"}, "avatar": {"kind": "avatar"}, "a": {"kind": "zone"}, "b": {"kind": "zone"}})
        self.assertEqual(rosebuild.prebuild_order(rose_like, ["b", "a", "avatar", "login"], zone="b"), ["login", "avatar", "b", "a"])
        self.assertEqual(rosebuild.warm_after(rose_like, "login", "a"), ["avatar", "a"])
        self.assertEqual(rosebuild.warm_after(rose_like, "a", "a"), [])

    def test_scene_uses_the_same_names(self):
        if not os.path.isdir(UN):
            self.skipTest("no repo checkout")
        b = rd(UN, "Editor", "GsvrSceneBuilder.Ragnarok.cs")
        m = re.search(r"RoMapNames\s*=\s*\{([^}]*)\}", b)
        self.assertIsNotNone(m, "GsvrSceneBuilder.Ragnarok.cs RoMapNames")
        self.assertEqual(re.findall(r'"([a-z0-9_]+)"', re.sub(r"//[^\n]*", "", m.group(1))), MAPS + list(dungeons.NAMES) + list(jobmaps.NAMES) + list(maps2.NAMES))          # towns + culvert + dungeons + job-test maps = api.MAP_ORDER
        warps = rd(UN, "Editor", "ro_warps.txt")
        used = set()
        for ln in warps.splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                f = ln.split()
                used.add(f[0]); used.add(f[5])
        self.assertEqual(used, set(towns.NAMES))                            # every warp end of the towns table is a towns map, every towns map has a warp
        cw = rd(UN, "Editor", "ro_culvert_warps.txt")                       # w39: the culvert lane's own table (entrance from prt_fild05 + the four floors)
        cu = set()
        for ln in cw.splitlines():
            if ln.strip() and not ln.startswith("#"):
                f = ln.split(); cu.add(f[0]); cu.add(f[5])
        self.assertEqual(cu, set(culvert.NAMES) | {"prt_fild05"})


class Manager(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="romulti")
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        self.inst = os.path.join(self.work, "Ragnarok")
        os.makedirs(self.inst)
        with open(os.path.join(self.inst, "data.grf"), "wb") as fh:
            fh.write(b"stub")
        del ro_conv_stub.CALLS[:]
        old = ro_conv_stub.PACKS
        ro_conv_stub.PACKS = dict(api.PACKS)                                  # the stub's build() accepts the five RO maps
        self.addCleanup(setattr, ro_conv_stub, "PACKS", old)

    def wait(self, ups, name, t=20):
        t0 = time.time()
        while time.time() - t0 < t:
            r = [f for n, f in ups if n == name and f.get("state") in ("ready", "error")]
            if r:
                return r[-1]
            time.sleep(0.02)
        self.fail("no answer for " + name)

    def test_start_map_first_then_the_rest_in_order(self):
        conv = ro_like_stub()
        rdir = os.path.join(self.work, "ro")
        ups = []
        m = rosebuild.Manager(rdir, conv=conv, log=lambda s: None, on_update=lambda n, f: ups.append((n, dict(f))), flavor=rosebuild.RO)
        self.addCleanup(m.stop)
        m.set_install(self.inst, prebuild=False)
        r = m.request("prontera")                                            # the world's first request
        self.assertEqual(r["state"], "building")
        self.assertEqual(self.wait(ups, "prontera")["state"], "ready")
        self.assertTrue(m.wait_idle(30))
        self.assertEqual(ro_conv_stub.CALLS, MAPS)                             # warm-up after prontera: the other maps in MAP_ORDER, one at a time
        st = m.state()
        rows = companion.ro_maps(st)
        self.assertEqual([n for n in rows if n not in dungeons.NAMES and n not in jobmaps.NAMES and n not in maps2.NAMES], MAPS)
        self.assertTrue(all(v == {"state": "ready", "progress": 1.0} for n, v in rows.items() if n in MAPS), rows)
        self.assertTrue(all(rows[n]["state"] == "missing" for n in dungeons.NAMES + maps2.NAMES), rows)         # the dungeons lane and the w46 maps2 lane are never prebuilt
        for n in MAPS:                                                       # the world's next requests are answered from the cache
            self.assertEqual(m.request(n)["state"], "ready", n)
            self.assertTrue(os.path.isfile(os.path.join(rdir, "ro__%s.pack" % n)))
        self.assertEqual(ro_conv_stub.CALLS, MAPS)                             # nothing rebuilt

    def test_a_later_map_asked_first_jumps_the_queue(self):
        conv = ro_like_stub()
        rdir = os.path.join(self.work, "ro")
        ups = []
        m = rosebuild.Manager(rdir, conv=conv, log=lambda s: None, on_update=lambda n, f: ups.append((n, dict(f))), flavor=rosebuild.RO)
        self.addCleanup(m.stop)
        m.set_install(self.inst, prebuild=False)
        m.request("prt_fild08")                                              # e.g. a warp to a map whose pack is not there yet
        self.assertEqual(self.wait(ups, "prt_fild08")["state"], "ready")
        self.assertTrue(m.wait_idle(30))
        self.assertEqual(ro_conv_stub.CALLS[0], "prt_fild08")
        self.assertEqual(ro_conv_stub.CALLS[1:2], ["izlude"])                # its warm-up, then the background prebuild in map order
        self.assertEqual(sorted(ro_conv_stub.CALLS), sorted(MAPS))

    def test_status_rows(self):
        st = {"packs": {"prontera": {"state": "fresh"}, "prt_in": {"state": "missing"}, "prt_church": {"state": "stale"},
                        "prt_fild08": {"state": "missing", "error": "boom"}, "izlude": {"state": "missing"}},
              "running": [{"name": "prt_in", "progress": 0.4567}], "queue": ["izlude"]}
        rows = companion.ro_maps(st)
        self.assertEqual(rows["prontera"], {"state": "ready", "progress": 1.0})
        self.assertEqual(rows["prt_in"], {"state": "building", "progress": 0.457})
        self.assertEqual(rows["prt_church"]["state"], "stale")
        self.assertEqual(rows["prt_fild08"]["state"], "error")
        self.assertEqual(rows["izlude"]["state"], "queued")
        s = companion.ro_summary({"state": "ready"}, dict(st, install="x", install_ok=True))
        self.assertEqual(s["maps"], rows)
        self.assertNotIn("x", str(s["maps"]))


class SpriteRule(unittest.TestCase):
    """the real converter: the start map's pack carries the sprite set, every other map pack is map only"""
    def test_sprites_only_in_the_start_map(self):
        work = tempfile.mkdtemp(prefix="romultisp")
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)
        files = E.client_files(True)
        rsw = RC.rsw_bytes('prt_in.gnd', 'prt_in.gat', [('box.rsm', (-30.0, 0.0, -30.0), (0, 0, 0), (1, 1, 1))])
        files['data\\prt_in.rsw'] = rsw
        files['data\\prt_in.gnd'] = files['data\\prontera.gnd']
        files['data\\prt_in.gat'] = files['data\\prontera.gat']
        client = os.path.join(work, "Ragnarok")
        os.makedirs(client)
        grf.write_grf(os.path.join(client, "data.grf"), files)
        out = {}
        for n in ("prontera", "prt_in"):
            o = os.path.join(work, "ro", "ro__%s.pack" % n)
            res = ro_conv.build(n, client, o)
            with open(o, "rb") as fh:
                out[n] = (res, P.read_pack(fh.read())['sections'])
        self.assertTrue(out["prontera"][0]["sprites"])
        self.assertIn(S.TAG_RSAN, out["prontera"][1])
        self.assertFalse(out["prt_in"][0]["sprites"])
        self.assertNotIn(S.TAG_RSAT, out["prt_in"][1])
        self.assertNotIn(S.TAG_RSAN, out["prt_in"][1])
        self.assertFalse(any(w.startswith("sprites:") for w in out["prt_in"][0]["warnings"]))     # no "placeholders" warning for a map-only pack
        for t in (b'AREA', b'CELL', b'HGHT', b'TERR', b'OMSH'):
            self.assertIn(t, out["prt_in"][1])


class StartCell(unittest.TestCase):
    """r142: Prontera starts on rAthena's @go point south of the fountain (not the map centre = inside the fountain basin); a start outside a
    (synthetic) map's grid falls back to the centre with a warning instead of failing the build"""
    def test_prontera_start_is_the_rathena_go_point(self):
        self.assertEqual(api.PACKS["prontera"].get("start"), (156, 191))
        self.assertGreaterEqual(api.PACK_REV.get("prontera", 0), 1)          # packs built with the old centre start are stale
        self.assertNotEqual(api.pack_version("prontera"), api.pack_version("prt_in"))
        for n in MAPS[1:]:
            self.assertIsNone(api.PACKS[n].get("start"))

    def test_build_uses_the_pack_start_and_survives_a_small_grid(self):
        work = tempfile.mkdtemp(prefix="romultist")
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)
        client = os.path.join(work, "Ragnarok")
        os.makedirs(client)
        grf.write_grf(os.path.join(client, "data.grf"), E.client_files(True))
        o = os.path.join(work, "ro", "ro__prontera.pack")
        res = ro_conv.build("prontera", client, o)
        self.assertTrue(any(w.startswith("start cell 156,191 outside") for w in res["warnings"]), res["warnings"])
        gw, gh = res["grid"]
        self.assertTrue(0 <= res["start_cell"][0] < gw and 0 <= res["start_cell"][1] < gh)
        old = dict(api.PACKS["prontera"])
        try:
            api.PACKS["prontera"]["start"] = (1, 1)
            res = ro_conv.build("prontera", client, o)
            with open(o, "rb") as fh:
                sec = P.read_pack(fh.read())['sections']
            a = P.dec_area(sec[b'AREA']['data'])
            if sec[b'CELL']['data'][gw + 1]:
                self.assertEqual(list(res["start_cell"]), [1, 1])
            self.assertAlmostEqual(a["start_x"], (res["start_cell"][0] + 0.5) * a["cell_size"])
            self.assertFalse(any(w.startswith("start cell") for w in res["warnings"]))
        finally:
            api.PACKS["prontera"].clear()
            api.PACKS["prontera"].update(old)


if __name__ == "__main__":
    unittest.main()
