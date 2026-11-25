"""RO NPCs (docs/design/ro_npc.md), converter side: the NPC sprites ride in the start pack as 'npc_<id>', on SYNTHETIC files only (made-up pixels, nothing of the game);
the id table matches Editor/ro_npcs.txt; the pack revision was bumped so older caches rebuild. Mac only (skipped without the owner's client / converted packs, never in git):
  - every NPC_SPRITES entry exists in the owner's data.grf (read only),
  - every NPC cell of ro_npcs.txt and at least one neighbour is walkable in the converted packs (the world blocks the NPC cell itself and the player stands next to it).

  cd companion && python3 -I test_ro_npc.py
"""
import os
import re
import struct
import sys
import tempfile
import shutil
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ro_conv  # noqa: E402
from ro_conv import api, grf, sprite as S  # noqa: E402
import test_ro_e2e as E2E  # noqa: E402
import test_ro_sprite as RS  # noqa: E402
import rpg1 as P  # noqa: E402

ROOT = os.path.dirname(HERE)
NPCS = os.path.join(ROOT, "unity", "Assets", "GoldSrcVR", "Editor", "ro_npcs.txt")
REAL_CLIENT = os.environ.get("GSVR_RO_CLIENT", "/path/to/gsvr-testdata/ro_data/Ragnarok Online")
REAL_PACKS = os.environ.get("GSVR_RO_REAL_DIR", "/path/to/gsvr-testdata/ro_out/lmv3")


def npc_rows():
    """[(kind, map, x, y, facing, sprite id, tag)] of ro_npcs.txt, parsed with its own header rules."""
    out = []
    with open(NPCS, encoding="utf-8") as fh:
        for ln in fh:
            f = ln.split()
            if len(f) >= 8 and f[0] == "npc":
                out.append((f[1], f[2], int(f[3]), int(f[4]), int(f[5]), int(f[6]), f[7]))
    return out


def npc_files(ids, actions=8):
    files = {}
    for sid in ids:
        base = (api.NPC_PATH % api.NPC_SPRITES[sid]).replace("/", "\\")
        files[base + ".spr"] = S.write_spr([RS._block(3 + sid % 5, 4, 2)], palette=RS._pal())
        files[base + ".act"] = S.write_act([[dict(layers=[RS._layer(0, -1, 0)], event=-1, anchors=[])]] * actions)
    return files


def client(folder, ids, actions=8, sprites=True):
    files = E2E.client_files(sprites)
    files.update(npc_files(ids, actions))
    os.makedirs(folder, exist_ok=True)
    grf.write_grf(os.path.join(folder, "data.grf"), files)
    return folder


class Table(unittest.TestCase):
    def test_table_matches_the_data_file(self):
        rows = npc_rows()
        self.assertEqual(len(rows), 3)
        self.assertEqual(sorted(r[5] for r in rows), sorted(api.NPC_SPRITES))        # one sprite per NPC, nothing extra converted
        for sid, nm in api.NPC_SPRITES.items():
            self.assertRegex(nm, r"^[0-9a-z_]+$")                                       # a client file name, no path
            (api.NPC_PATH % nm).encode("cp949")

    def test_pack_revision_bumped(self):
        self.assertGreaterEqual(api.PACK_REV["prontera"], 2)
        self.assertTrue(ro_conv.pack_version("prontera").endswith("-p%d" % api.PACK_REV["prontera"]))


class Build(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="ronpc")
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)

    def _build(self, folder):
        out = os.path.join(self.work, "out", "ro__prontera.pack")
        res = ro_conv.build("prontera", folder, out)
        with open(out, "rb") as fh:
            p = P.read_pack(fh.read())
        return res, S.dec_rsan(p["sections"][S.TAG_RSAN]["data"]), p

    def test_npc_sprites_are_in_the_start_pack(self):
        res, rs, p = self._build(client(os.path.join(self.work, "c1"), sorted(api.NPC_SPRITES)))
        names = [s["name"] for s in rs["sprites"]]
        self.assertEqual(names[:3], ["body_m", "head_m", "poring"])                    # the player set keeps its names / order (GsvrRoSprite bodyName / headName)
        for sid in api.NPC_SPRITES:
            self.assertIn("npc_%d" % sid, names)
        self.assertEqual(names.count("npc_105"), 1)
        for s in rs["sprites"]:
            if s["name"].startswith("npc_"):
                self.assertEqual(s["clips"], 8)                                    # idle, 8 directions
                self.assertEqual(s["kind"], S.KIND_MONSTER)
        self.assertGreater(rs["ref_height_px"], 0)
        self.assertFalse([w for w in res["warnings"] if "npc" in w])

    def test_every_direction_has_a_frame(self):
        # a client whose NPC ACT has ONE action only: the pose is shown from all 8 sides (an empty clip would draw nothing)
        res, rs, p = self._build(client(os.path.join(self.work, "c2"), sorted(api.NPC_SPRITES), actions=1))
        clips = rs["clips"]
        for s in rs["sprites"]:
            if s["name"].startswith("npc_"):
                for d in range(8):
                    self.assertGreater(clips[s["first_clip"] + d][1], 0, "%s dir %d" % (s["name"], d))

    def test_missing_npc_sprite_warns_but_the_rest_stays(self):
        have = [105, 54]                                                                # no sprite 53
        res, rs, p = self._build(client(os.path.join(self.work, "c3"), have))
        names = [s["name"] for s in rs["sprites"]]
        self.assertIn("npc_105", names)
        self.assertNotIn("npc_53", names)
        self.assertEqual(names[:3], ["body_m", "head_m", "poring"])
        w = [x for x in res["warnings"] if "npc 53" in x]
        self.assertEqual(len(w), 1, res["warnings"])
        self.assertNotIn(self.work, w[0])                                               # status.json is public: no paths
        self.assertTrue(res["sprites"])

    def test_no_player_sprites_means_no_npc_sprites_either(self):
        res, rs = None, None
        c = client(os.path.join(self.work, "c4"), sorted(api.NPC_SPRITES), sprites=False)
        out = os.path.join(self.work, "out4", "ro__prontera.pack")
        res = ro_conv.build("prontera", c, out)
        self.assertFalse(res["sprites"])                                                # the Novice pair is required first: the world keeps its placeholders
        with open(out, "rb") as fh:
            p = P.read_pack(fh.read())
        self.assertNotIn(S.TAG_RSAN, p["sections"])


@unittest.skipUnless(os.path.isfile(os.path.join(REAL_CLIENT, "data.grf")), "no owner client (Mac only)")
class RealClient(unittest.TestCase):
    def test_npc_sprite_files_exist(self):
        v = grf.Vfs(REAL_CLIENT)
        for sid, nm in api.NPC_SPRITES.items():
            base = api.NPC_PATH % nm
            self.assertTrue(v.exists(base + ".spr") and v.exists(base + ".act"), "%d %s" % (sid, nm))
            a = S.parse_act(v.read(base + ".act"))
            self.assertGreaterEqual(len(a["actions"]), 8, "%s has fewer than 8 actions" % nm)


def _grid(path):
    with open(path, "rb") as fh:
        p = P.read_pack(fh.read())
    a = P.dec_area(p["sections"][b"AREA"]["data"])
    return a["grid_w"], a["grid_h"], p["sections"][b"CELL"]["data"]


class RealPacks(unittest.TestCase):
    PACK = {"prontera": "ro__prontera.pack", "prt_in": "ro__prt_in.pack"}

    def test_npc_cells_walkable_with_a_free_neighbour(self):
        grids = {}
        for m, fn in self.PACK.items():
            p = os.path.join(REAL_PACKS, fn)
            if not os.path.isfile(p):
                self.skipTest("no converted pack %s (Mac only)" % fn)
            grids[m] = _grid(p)
        for kind, m, x, y, face, sid, tag in npc_rows():
            w, h, c = grids[m]
            self.assertTrue(c[y * w + x], "%s cell (%d,%d) of %s is not walkable in the GAT (NPC would stand in a wall)" % (tag, x, y, m))
            free = [(x + dx, y + dz) for dx in (-1, 0, 1) for dz in (-1, 0, 1) if (dx or dz) and c[(y + dz) * w + x + dx]]
            self.assertGreaterEqual(len(free), 3, "%s has fewer than 3 walkable neighbours" % tag)


if __name__ == "__main__":
    unittest.main()
