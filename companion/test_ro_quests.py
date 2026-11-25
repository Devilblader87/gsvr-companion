"""RO quests (docs/design/ro_quests.md): the data file Editor/ro_quests.txt.

Needs no game data: grammar of the rows, step rules (the C# parser's rules re-stated independently), unique ids / tags, the NPC rows match the ro_npcs.txt grammar.
Mac only (skipped without the converted packs): every quest NPC stands on a walkable GAT cell with at least 3 walkable neighbours in the converted pack of its map
(an NPC inside a wall would be unreachable), and the cells do not collide with the ronpc NPCs.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "rose_conv"))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "unity", "Assets", "GoldSrcVR", "Editor", "ro_quests.txt")
NPCS = os.path.join(ROOT, "unity", "Assets", "GoldSrcVR", "Editor", "ro_npcs.txt")
REAL_PACKS = os.environ.get("GSVR_RO_REAL_DIR", "/path/to/gsvr-testdata/ro_out/lmv3")
MAPS = ["prontera", "prt_in", "prt_church", "prt_fild08", "izlude"]
TALK, KILL, ITEM, GIVE, PAY = 1, 2, 3, 4, 5
MAX_Q, MAX_STEPS = 32, 6


def rows():
    npcs, quests, steps = [], {}, {}
    with open(DATA, encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            f = ln.split()
            if f[0] == "npc":
                npcs.append(f)
            elif f[0] == "quest":
                quests[int(f[1])] = f
                steps[int(f[1])] = []
            elif f[0] == "step":
                steps[int(f[1])].append(f)
    return npcs, quests, steps


class Data(unittest.TestCase):
    def test_counts(self):
        npcs, quests, steps = rows()
        self.assertEqual(len(npcs), 16)
        self.assertEqual(sorted(quests), [1, 10, 11, 12, 13, 14, 15, 20, 21, 22, 23, 24])

    def test_npc_grammar_matches_ro_npcs(self):
        npcs, _, _ = rows()
        tags = [f[7] for f in npcs]
        self.assertEqual(len(set(tags)), len(tags), "duplicate NPC tag")
        for f in npcs:
            self.assertGreaterEqual(len(f), 8)
            self.assertEqual(f[1], "talk")
            self.assertIn(f[2], MAPS)
            x, y, face, sid = (int(v) for v in f[3:7])
            self.assertTrue(x >= 0 and y >= 0 and 0 <= face <= 7, f)
            self.assertIn(sid, (105, 53, 54), "only the three converter NPC sprites exist: %s" % f[7])
            self.assertTrue(f[7].startswith("q_"), "quest NPC tags start with q_")
        cells = [(f[2], f[3], f[4]) for f in npcs]
        self.assertEqual(len(set(cells)), len(cells), "two quest NPCs on one cell")
        old = []
        if os.path.isfile(NPCS):        # ro_npcs.txt comes with the ronpc lane (not in every tree)
            with open(NPCS, encoding="utf-8") as fh:
                old = [l.split() for l in fh if l.startswith("npc ")]
        taken = {(f[2], f[3], f[4]) for f in old}
        self.assertFalse(taken & set(cells), "a quest NPC on a ronpc NPC cell")
        self.assertFalse({f[7] for f in old} & set(tags), "tag clash with ro_npcs.txt")

    def test_quest_rules(self):
        npcs, quests, steps = rows()
        tags = {f[7] for f in npcs}
        for qid, q in quests.items():
            self.assertTrue(0 < qid < MAX_Q)
            self.assertEqual(len(q), 15, "quest %d row" % qid)
            cls, minjob, minbase, pre = (int(v) for v in q[2:6])
            giver = q[6]
            self.assertIn(giver, tags)
            self.assertIn(cls, (0, 1, 2))
            rb, rj, rz, i1, q1, i2, q2, jid = (int(v) for v in q[7:15])
            self.assertTrue(min(rb, rj, rz, i1, q1, i2, q2, jid) >= 0)
            self.assertEqual(jid > 0, cls == 1, "only job quests change the job (quest %d)" % qid)
            if cls == 1:
                self.assertEqual(minjob, 10, "classic job level 10")
                self.assertTrue(1 <= jid <= 6)
            st = steps[qid]
            self.assertTrue(2 <= len(st) <= MAX_STEPS, "quest %d steps" % qid)
            ty = [int(s[2]) for s in st]
            self.assertEqual(st[0][5], giver, "step 1 at the giver")
            self.assertNotEqual(ty[0], KILL)
            self.assertNotEqual(ty[-1], KILL, "the last step must be a report, not a kill")
            for s in st:
                t, a, b, npc = int(s[2]), int(s[3]), int(s[4]), s[5]
                self.assertEqual(int(s[1]), qid)
                if t == KILL:
                    self.assertEqual(npc, "-")
                    self.assertTrue(a > 0 and 0 < b <= 99)
                else:
                    self.assertIn(npc, tags)
                    if t in (ITEM, GIVE):
                        self.assertTrue(a > 0 and 0 < b <= 999)
                    if t == PAY:
                        self.assertTrue(0 < a <= 1000000)

    def test_job_quests_cover_the_six_jobs(self):
        _, quests, _ = rows()
        jobs = sorted(int(q[14]) for q in quests.values() if int(q[2]) == 1)
        self.assertEqual(jobs, [1, 2, 3, 4, 5, 6])

    def test_no_dialogue_in_the_data(self):
        with open(DATA, encoding="utf-8") as fh:
            body = [l for l in fh if l.strip() and not l.lstrip().startswith("#")]
        for l in body:
            self.assertTrue(l.split()[0] in ("npc", "quest", "step"), l)
            self.assertNotIn('"', l)


def _grid(path):
    import rpg1 as P
    with open(path, "rb") as fh:
        p = P.read_pack(fh.read())
    a = P.dec_area(p["sections"][b"AREA"]["data"])
    return a["grid_w"], a["grid_h"], p["sections"][b"CELL"]["data"]


class RealPacks(unittest.TestCase):
    def test_npc_cells_walkable_with_free_neighbours(self):
        npcs, _, _ = rows()
        grids = {}
        for m in {f[2] for f in npcs}:
            p = os.path.join(REAL_PACKS, "ro__%s.pack" % m)
            if not os.path.isfile(p):
                self.skipTest("no converted pack %s (Mac only)" % p)
            grids[m] = _grid(p)
        for f in npcs:
            m, x, y, tag = f[2], int(f[3]), int(f[4]), f[7]
            w, h, c = grids[m]
            self.assertTrue(c[y * w + x], "%s (%d,%d) in %s is not walkable" % (tag, x, y, m))
            free = [1 for dx in (-1, 0, 1) for dz in (-1, 0, 1) if (dx or dz) and c[(y + dz) * w + x + dx]]
            self.assertGreaterEqual(len(free), 3, "%s has fewer than 3 walkable neighbours" % tag)


if __name__ == "__main__":
    unittest.main()
