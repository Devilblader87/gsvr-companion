"""ROSE w20 DUNGEON: the monsters' skill table behind the AIPS records (companion-owned rose_npcskills.py), on synthetic data (no game files, made-up numbers).
docs/design/rose_w20_dungeon_audit.md

  cd companion && python3 -I test_rose_w20_npcskills.py
"""
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rose_conv  # noqa: E402,F401  (puts the converter folder on sys.path, installs the wrappers)
import rose_aips  # noqa: E402
import rose_npcskills as N  # noqa: E402


def _prog(acts_by_trigger):
    """[(trigger, [action tuples])] -> program dict in the rose_aips form (one event per entry, no conditions)"""
    trig = [[] for _ in range(rose_aips.TRIGGERS)]
    for t, acts in acts_by_trigger:
        trig[t].append(([], acts))
    return dict(idle_s=2, new_target_pct=50, triggers=trig)


class _Vfs:
    def __init__(self, files):
        self.files = files

    def read(self, p):
        return self.files[p]


def _skill_stb(rows):
    """{row: {cell: value}} -> a stb dict like rosefmt.stb returns (80 cells per row)"""
    n = max(rows) + 1
    cells = [[''] * 80 for _ in range(n)]
    for r, cs in rows.items():
        for c, v in cs.items():
            cells[r][c] = str(v)
    return dict(rows=n, cells=cells)


# made-up skills: 7 = self damage around the caster (type 17, weapon damage, power 120, range 2 m, scope 6 m), 9 = a bullet (type 6, magic, power 80, range 12 m), 11 = a self buff (type 8)
ROWS = {7: {5: 17, 15: 1, 9: 120, 6: 200, 8: 600, 70: 2, 13: 90, 11: 3, 14: 10, 20: 5},
        9: {5: 6, 15: 2, 9: 80, 6: 1200, 8: 0, 70: 1},
        11: {5: 8, 15: 0, 9: 0, 11: 4, 14: 30}}


class NpcSkillTests(unittest.TestCase):
    def section(self, programs):
        data = rose_aips.encode([(100, 0), (101, 1 if len(programs) > 1 else None)], programs)
        return (rose_aips.TAG, 1, len(programs), data)

    def run_wrapper(self, programs, rows=ROWS):
        sec = self.section(programs)
        stb = _skill_stb(rows)
        return N.build_with_skills(lambda v, a, r: (sec, [], {"programs": len(programs)}), _Vfs({'3DDATA/STB/LIST_SKILL.STB': b'x'}), {}, lambda b: stb)

    def test_table_of_the_used_skills(self):
        p0 = _prog([(3, [(25, 2, 0, 0, 7, 40, 0), (25, 1, 0, 0, 9, 41, 0)]), (1, [(14, 0, 0, 0, 0, 0, 0)])])
        p1 = _prog([(2, [(25, 2, 0, 0, 11, 42, 0), (25, 1, 0, 0, 9, 41, 0)])])
        (tag, ver, cnt, data), warn, info = self.run_wrapper([p0, p1])
        self.assertEqual((tag, ver, cnt), (rose_aips.TAG, 1, 2))
        tab = N.dec_table(data)
        self.assertEqual([r[0] for r in tab], [7, 9, 11], "one record per used skill id, sorted, no duplicates")
        self.assertEqual(tab[0], (7, 17, 1, 120, 200, 600, 2, 90, 3, 0, 10, 5, 0, 0, 0, 0))
        self.assertEqual(tab[1][:7], (9, 6, 2, 80, 1200, 0, 1))
        self.assertEqual(info["skills"], 3)
        self.assertEqual(warn, [])
        h = rose_aips.HDR.unpack_from(data, 0)
        self.assertEqual(h[N.HDR_SK_OFF] % 4, 0)
        # the records and programs are untouched: decode gives back the same programs
        npcs, progs = rose_aips.decode(data)
        self.assertEqual(progs[0]['triggers'][3][0][1][0], (25, 2, 0, 0, 7, 40, 0))
        self.assertEqual(len(progs), 2)

    def test_no_skills_no_table(self):
        p0 = _prog([(1, [(14, 0, 0, 0, 0, 0, 0)])])
        (tag, ver, cnt, data), warn, info = self.run_wrapper([p0])
        orig = self.section([p0])[3]
        self.assertEqual(data, orig, "a zone whose scripts use no skill keeps the AIPS bytes")
        self.assertEqual(N.dec_table(data), [])

    def test_unknown_skill_warns(self):
        p0 = _prog([(3, [(25, 1, 0, 0, 77, 0, 0), (25, 1, 0, 0, 9, 0, 0)])])
        (_t, _v, _c, data), warn, info = self.run_wrapper([p0])
        self.assertEqual([r[0] for r in N.dec_table(data)], [9])
        self.assertTrue(any("no LIST_SKILL row" in w for w in warn), warn)

    def test_bad_tables_read_as_none(self):
        p0 = _prog([(3, [(25, 1, 0, 0, 9, 0, 0)])])
        data = self.run_wrapper([p0])[0][3]
        h = list(rose_aips.HDR.unpack_from(data, 0))
        bad = bytearray(data)
        h2 = list(h); h2[N.HDR_SK_COUNT] = 5
        rose_aips.HDR.pack_into(bad, 0, *h2)
        self.assertEqual(N.dec_table(bytes(bad)), [], "a count past the end reads as no table")
        self.assertEqual(N.dec_table(data[:40]), [])

    def test_installed_once(self):
        self.assertIsNotNone(N._orig, "rose_conv installs the wrapper")
        w = rose_aips.build_section
        N.install()
        self.assertIs(rose_aips.build_section, w, "install is idempotent")

    def test_missing_list_skill_keeps_section(self):
        p0 = _prog([(3, [(25, 1, 0, 0, 9, 0, 0)])])
        sec = self.section([p0])
        out, warn, info = N.build_with_skills(lambda v, a, r: (sec, [], {}), _Vfs({}), {}, lambda b: None)
        self.assertEqual(out, sec)
        self.assertTrue(any("AIPS skills" in w for w in warn))


if __name__ == "__main__":
    unittest.main()
