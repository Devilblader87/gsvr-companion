"""RO classic NPCs (w43 npc lane, docs/design/ro_npc_world.md), converter side: the 'npc_<town>' sprite packs on SYNTHETIC files (made-up pixels, nothing of the game),
the generated id lists (npcsets.py) == the decor rows of Editor/ro_npc_world.txt, the build-manager view (served + prebuilt by name, versioned). Mac only (skipped without the owner's
client / converted packs / the rAthena checkout, never in git): every sprite id resolves in the owner's client (npcidentity + jobname, monster fallback), one real pack stays under the
size budget, every decor cell is inside its converted map's grid, the generated files are up to date.

  cd companion && python3 -I test_ro_npcworld.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ro_conv  # noqa: E402
import rosebuild  # noqa: E402
from ro_conv import api, grf, npcs, npcsets, sprite as S  # noqa: E402
import test_ro_e2e as E2E  # noqa: E402
import test_ro_sprite as RS  # noqa: E402
import rpg1 as P  # noqa: E402

ROOT = os.path.dirname(HERE)
WORLD = os.path.join(ROOT, "unity", "Assets", "GoldSrcVR", "Editor", "ro_npc_world.txt")
REAL_CLIENT = os.environ.get("GSVR_RO_CLIENT", "/path/to/gsvr-testdata Online")
REAL_PACKS = os.environ.get("GSVR_RO_REAL_DIR", "/path/to/gsvr-testdata")
RATHENA = os.environ.get("GSVR_RATHENA", "/path/to/gsvr-testdata")


def rows(kind):
    with open(WORLD, encoding="utf-8") as fh:
        return [l.split() for l in fh if l.startswith(kind + " ")]


def client(folder, ids, actions=8):
    files = E2E.client_files(True)
    for sid in ids:
        base = (npcs.NPC_PATH % api.NPC_SPRITES[sid]).replace("/", "\\")
        files[base + ".spr"] = S.write_spr([RS._block(3 + sid % 5, 4, 2)], palette=RS._pal())
        files[base + ".act"] = S.write_act([[dict(layers=[RS._layer(0, -1, 0)], event=-1, anchors=[])]] * actions)
    os.makedirs(folder, exist_ok=True)
    grf.write_grf(os.path.join(folder, "data.grf"), files)
    return folder


class Tables(unittest.TestCase):
    def test_sets_match_the_world_rows(self):
        groups = dict((r[1], r[2:]) for r in rows("group"))
        self.assertEqual(dict((g, list(ms)) for g, ms in npcsets.NPC_GROUPS.items()), groups)
        want = {}
        for r in rows("deco"):
            want.setdefault(npcs.group_of(r[1]), set()).add(int(r[5]))
        qf = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "unity", "Assets", "GoldSrcVR", "Editor", "ro_quests.txt")
        for ln in open(qf, encoding="utf-8"):                                          # w53 quests2: a quest giver on a classic cell replaced its decor row, the sprite stays in the town pack
            t = ln.split("#")[0].split()
            if len(t) >= 8 and t[0] == "npc" and t[7].startswith("q_") and int(t[6]) not in (105, 53, 54) and npcs.group_of(t[2]):
                want.setdefault(npcs.group_of(t[2]), set()).add(int(t[6]))
        self.assertNotIn("", want)                                                     # every decor map has a group
        for g in npcsets.NPC_GROUPS:
            self.assertEqual(sorted(want.get(g, ())), list(npcsets.NPC_SET_IDS[g]), g)   # one sprite per look, nothing extra converted
        for g in npcsets.NPC_GROUPS:
            self.assertRegex(g, r"^npc_[a-z0-9_]{1,28}$")                              # the world's LPackName + the companion's pack-name rule
            for m in npcsets.NPC_GROUPS[g]:
                self.assertIn(m, api.MAP_ORDER)

    def test_count_rows_balance(self):
        for r in rows("count"):
            a, x, c, d, e = (int(v) for v in r[2:7])
            self.assertEqual(a - x, c + d - e, r)
            self.assertEqual(d, sum(1 for q in rows("deco") if q[1] == r[1]), r)
        with open(WORLD.replace("ro_npc_world", "ro_npc_audit")) as fh:
            self.assertEqual(sum(int(r[2]) for r in rows("count")), sum(1 for l in fh if l.startswith("audit ")))          # every audit row is accounted for

    def test_rows_are_numbers_and_short_names(self):
        for r in rows("deco"):
            self.assertEqual(len(r), 10, r)                                             # name is one token ('_' for blanks)
            self.assertIn(r[6], ("talk", "shop", "kafra"))
            self.assertTrue(0 <= int(r[4]) <= 7 and int(r[5]) > 0 and 0 <= int(r[7]) < 13)
            self.assertLessEqual(len(r[9]), 24)
            self.assertRegex(r[9], r"^[A-Za-z0-9_'.-]+$")
            if r[6] == "kafra":
                self.assertTrue(r[8].startswith("kaf"), r)
        deco = rows("deco")
        for r in rows("ditem"):
            self.assertEqual(deco[int(r[1])][6], "shop", r)
            self.assertTrue(int(r[2]) > 0 and int(r[3]) >= 0)

    def test_manager_view(self):
        mc = rosebuild.merge_audio(ro_conv)
        for g in npcs.NPC_PACKS:
            self.assertIn(g, mc.PACKS)
            self.assertNotIn(g, api.PACKS)                                              # not a map
            self.assertIn(g, api.PREBUILD_ORDER)
            self.assertTrue(api.pack_version(g).startswith("ro-npc-"))
        self.assertEqual(api.PREBUILD_ORDER[:4], ("prontera", "roui", "roefx", "npc_prontera"))   # the start town's NPCs right after the interface + effects (w43 integrate)
        order = rosebuild.prebuild_order(mc, list(mc.PACKS))
        self.assertEqual([n for n in order if n not in api.AUDIO_PACKS], list(api.PREBUILD_ORDER))
        self.assertEqual(api.warm_after("npc_geffen"), [])


class Build(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="ronpcw")
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        self.ids = npcsets.NPC_SET_IDS
        npcsets.NPC_SET_IDS = dict(self.ids)
        npcsets.NPC_SET_IDS["npc_izlude"] = (53, 105, 116, 1999)                       # 53 / 105 / 116 via the fallback table, 1999 nowhere
        self.addCleanup(setattr, npcsets, "NPC_SET_IDS", self.ids)

    def _build(self, folder):
        out = os.path.join(self.work, "out", "ro__npc_izlude.pack")
        res = ro_conv.build("npc_izlude", folder, out)
        with open(out, "rb") as fh:
            p = P.read_pack(fh.read())
        return res, p

    def test_pack_holds_the_group_sprites_only(self):
        res, p = self._build(client(os.path.join(self.work, "c1"), (53, 105, 116)))
        self.assertEqual(sorted(p["sections"]), sorted([S.TAG_RSAT, S.TAG_RSAN]))        # no map sections: the world never takes it for a map
        rs = S.dec_rsan(p["sections"][S.TAG_RSAN]["data"])
        self.assertEqual([s["name"] for s in rs["sprites"]], ["npc_53", "npc_105", "npc_116"])
        for s in rs["sprites"]:
            self.assertEqual(s["clips"], 8)
            self.assertEqual(s["kind"], S.KIND_MONSTER)
        self.assertGreater(rs["ref_height_px"], 0)                                     # scaled like the player (the Novice of the same client)
        w = [x for x in res["warnings"] if "npc sprites not in the client" in x]
        self.assertEqual(len(w), 1, res["warnings"])                                    # ONE grouped line for the missing id
        self.assertIn("1999", w[0])
        self.assertNotIn(self.work, w[0])
        self.assertEqual(res["sprites"], 3)

    def test_one_direction_npc_shows_from_every_side(self):
        res, p = self._build(client(os.path.join(self.work, "c2"), (53, 105, 116), actions=1))
        rs = S.dec_rsan(p["sections"][S.TAG_RSAN]["data"])
        for s in rs["sprites"]:
            for d in range(8):
                self.assertGreater(rs["clips"][s["first_clip"] + d][1], 0, "%s dir %d" % (s["name"], d))

    def test_no_sprite_at_all_is_a_build_error(self):
        c = client(os.path.join(self.work, "c3"), ())
        with self.assertRaises(api.BuildError):
            ro_conv.build("npc_izlude", c, os.path.join(self.work, "out3", "x.pack"))
        with self.assertRaises(api.BuildError):
            npcs.build("prontera", c, os.path.join(self.work, "out3", "y.pack"))


@unittest.skipUnless(os.path.isfile(os.path.join(REAL_CLIENT, "data.grf")), "no owner client (Mac only)")
class RealClient(unittest.TestCase):
    def test_every_id_resolves(self):
        v = grf.Vfs(REAL_CLIENT)
        table = npcs.id_files(v)
        self.assertGreater(len(table), 500)
        miss = []
        for g, ids in npcsets.NPC_SET_IDS.items():
            for sid in ids:
                fn = table.get(sid) or api.NPC_SPRITES.get(sid)
                base = npcs.NPC_PATH % fn if fn else ""
                if fn and sid >= 1000 and not v.exists(base + ".spr"):
                    base = api.PATH_MONSTER + fn
                if not fn or not (v.exists(base + ".spr") and v.exists(base + ".act")):
                    miss.append((g, sid))
        self.assertEqual(miss, [])

    def test_small_pack_under_budget(self):
        out = os.path.join(tempfile.mkdtemp(prefix="ronpcr"), "npc_izlude.pack")
        self.addCleanup(shutil.rmtree, os.path.dirname(out), ignore_errors=True)
        res = npcs.build("npc_izlude", REAL_CLIENT, out)
        self.assertEqual(res["sprites"], len(npcsets.NPC_SET_IDS["npc_izlude"]))
        self.assertLess(res["bytes"], npcs.NPC_MAX_BYTES)
        self.assertFalse([w for w in res["warnings"] if "npc" in w])


class RealPacks(unittest.TestCase):
    def test_decor_cells_inside_their_grid(self):
        seen = 0
        by = {}
        for r in rows("deco"):
            by.setdefault(r[1], []).append(r)
        for m, rs in sorted(by.items()):
            p = os.path.join(REAL_PACKS, "ro__%s.pack" % m)
            if not os.path.isfile(p):
                continue
            with open(p, "rb") as fh:
                a = P.dec_area(P.read_pack(fh.read())["sections"][b"AREA"]["data"])          # one pack in memory at a time
            for r in rs:
                self.assertTrue(0 <= int(r[2]) < a["grid_w"] and 0 <= int(r[3]) < a["grid_h"], r)
                seen += 1
        if not seen:
            self.skipTest("no converted packs (Mac only)")


@unittest.skipUnless(os.path.isdir(os.path.join(RATHENA, "npc")), "no rAthena checkout (Mac only)")
class Generated(unittest.TestCase):
    def test_up_to_date(self):
        r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "ro_npc_world_gen.py"), "check", RATHENA], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
