"""W20 QUEST: QSD conditions 15 (PartyMemberCount), 17 (CompareNpcVariables), 18 (MonthDayTime), 19 (WeekDayTime) of the vendored rose_quest, on synthetic data
(no game files). docs/design/rose_w20_quest_audit.md

  cd companion && PYTHONPATH=. python3 -m unittest -q test_rose_w20_questops
"""
import collections
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rose_conv  # noqa: E402,F401
import rose_quest as Q  # noqa: E402
from test_rose_w18_questops import _qsd, _rec  # noqa: E402


class W20QuestOps(unittest.TestCase):
    def parse(self, conds):
        warn = collections.Counter()
        t = Q.parse_qsd(_qsd([("w20-test", conds, [])]), warn)
        self.assertEqual(len(t), 1)
        return t[0], warn

    def test_party_member_count(self):
        t, warn = self.parse([_rec(15, struct.pack("<HH", 2, 3))])
        self.assertEqual(t["conds"], [(15, 0, 0, 2, 3, 0)])
        self.assertFalse(warn)

    def test_compare_npc_variables(self):
        # npc 900 variable 1  >=(2)  npc 901 variable 2
        t, warn = self.parse([_rec(17, struct.pack("<IH2xIH2xB3x", 900, 1, 901, 2, 2))])
        self.assertEqual(t["conds"], [(17, 2, 1, 900, 901, 2)])
        self.assertFalse(warn)

    def test_time_windows_are_minutes_of_the_day(self):
        t, warn = self.parse([_rec(18, struct.pack("<BBBBB3x", 15, 10, 30, 12, 5)), _rec(19, struct.pack("<BBBBB3x", 0, 0, 0, 23, 59))])
        self.assertEqual(t["conds"], [(18, 15, 0, 630, 725, 0), (19, 0, 0, 0, 1439, 0)])
        self.assertFalse(warn)

    def test_docs_cover_new_ops(self):
        for op in (15, 17, 18, 19):
            self.assertIn(op, Q.COND_DOC)


if __name__ == "__main__":
    unittest.main()
