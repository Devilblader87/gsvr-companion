"""W18 DUNGEONS: QSD condition 5 (Party) and rewards 8 (SpawnMonster), 20 (SetTeamNumber), 21 (SetRevivePosition) of the vendored rose_quest, on synthetic data
(no game files). docs/design/rose_w18_dungeons_quests.md

  cd companion && PYTHONPATH=. python3 -m unittest -q test_rose_w18_questops
"""
import collections
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rose_conv  # noqa: E402,F401  (puts the converter folder on sys.path)
import rose_quest as Q  # noqa: E402


def _s(text):
    b = text.encode("cp949")
    return struct.pack("<H", len(b)) + b


def _rec(op, payload):
    return struct.pack("<II", 8 + len(payload), op) + payload


def _qsd(triggers):
    """triggers: [(name, [cond record bytes], [reward record bytes])] -> QSD file bytes (one group)"""
    out = struct.pack("<II", 1, 1) + _s("synthetic.qsd")
    out += struct.pack("<I", len(triggers)) + _s("grp")
    for name, conds, rews in triggers:
        out += struct.pack("<BII", 0, len(conds), len(rews)) + _s(name) + b"".join(conds) + b"".join(rews)
    return out


class QuestOpsTests(unittest.TestCase):
    def parse(self, conds, rews):
        warn = collections.Counter()
        t = Q.parse_qsd(_qsd([("w18-test", conds, rews)]), warn)
        self.assertEqual(len(t), 1)
        return t[0], warn

    def test_party_condition(self):
        # leader flag 1, level 5, operator byte 0 (>=)
        t, warn = self.parse([_rec(5, struct.pack("<B3xiB3x", 1, 5, 0))], [])
        self.assertEqual(t["conds"], [(5, 1, 0, 5, 2, 0)])
        self.assertFalse(warn)
        # member (not leader), operator byte 1 (less than)
        t, _ = self.parse([_rec(5, struct.pack("<B3xiB3x", 0, 90, 1))], [])
        self.assertEqual(t["conds"], [(5, 0, 0, 90, 3, 0)])

    def test_spawn_monster_reward(self):
        # npc 354, count 2, location 3 (position), zone 20, x / y in cm, distance 300, team 0
        t, warn = self.parse([], [_rec(8, struct.pack("<IIB3xIiiiI", 354, 2, 3, 20, 520002, 546006, 300, 0))])
        self.assertEqual(t["rews"], [(8, 3, 2, 354, 520002, 546006)])
        self.assertFalse(warn)
        # a huge count is clamped to the u16 field
        t, _ = self.parse([], [_rec(8, struct.pack("<IIB3xIiiiI", 1, 70000, 0, 0, 0, 0, 0, 0))])
        self.assertEqual(t["rews"][0][2], 65535)

    def test_team_and_revive_rewards(self):
        t, warn = self.parse([], [_rec(20, struct.pack("<B3x", 2)), _rec(21, struct.pack("<ii", 503548, 519511))])
        self.assertEqual(t["rews"], [(20, 2, 0, 0, 0, 0), (21, 0, 0, 503548, 519511, 0)])
        self.assertFalse(warn)

    def test_still_unsupported_stay_255(self):
        t, warn = self.parse([_rec(18, struct.pack("<BBBBB3x", 1, 0, 0, 23, 59))], [_rec(19, struct.pack("<HH", 1, 0) + _s("x"))])
        self.assertEqual(t["conds"][0][0], 255)
        self.assertEqual(t["rews"][0][0], 255)
        self.assertEqual(sum(warn.values()), 2)

    def test_docs_cover_new_ops(self):
        for op in (5,):
            self.assertIn(op, Q.COND_DOC)
        for op in (8, 20, 21):
            self.assertIn(op, Q.REW_DOC)


if __name__ == "__main__":
    unittest.main()
