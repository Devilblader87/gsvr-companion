"""ROSE w25 DROPS: the zone rows of the companion-owned rose_zonerow.py (zone ITEM_DROP row in DROP, LIST_ZONE join / kill / dead triggers in QUST), on synthetic data
(no game files, made-up numbers). docs/design/rose_w25_drops.md

  cd companion && python3 -I test_rose_w25_zonerow.py
"""
import collections
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rose_conv  # noqa: E402,F401  (puts the converter folder on sys.path, installs the wrappers)
import rose_gameplay as G  # noqa: E402
import rose_quest as RQ  # noqa: E402
import rose_zonerow as Z  # noqa: E402
import rosefmt as F  # noqa: E402


def _s16(t):
    b = t.encode('utf-8')
    return struct.pack('<H', len(b)) + b


def stb_bytes(rows, cols):
    """rows: list of cell lists (cols - 1 cells each, the key column excluded) -> STB version 1 bytes that rosefmt.stb reads back"""
    n = len(rows) + 1
    head = b'STB1'
    names = b''.join(_s16('c%d' % i) for i in range(cols)) + _s16('title') + b''.join(_s16('k%d' % i) for i in range(n - 1))
    widths = b'\0' * (2 * (cols + 1))
    dpos = len(head) + 16 + len(widths) + len(names)
    body = b''.join(_s16(str(c)) for r in rows for c in (list(r) + [''] * (cols - 1 - len(r))))
    return head + struct.pack('<IIII', dpos, n, cols, 0) + widths + names + body


class _Vfs:
    def __init__(self, files):
        self.files = files

    def read(self, p):
        return self.files[p]


def drop_stb(zone, zone_cells, npc_row, npc_cells):
    """ITEM_DROP with 51 cells per row (0 = text, 1..30 slots, 31..50 pools); rows zone and npc_row filled"""
    rows = [[''] * 51 for _ in range(max(zone, npc_row) + 1)]
    for c, v in zone_cells.items():
        rows[zone][c] = v
    for c, v in npc_cells.items():
        rows[npc_row][c] = v
    return stb_bytes(rows, 52)


class ZoneDropRow(unittest.TestCase):
    def setUp(self):
        self.z0 = Z.ZONE

    def tearDown(self):
        Z.ZONE = self.z0

    def test_zone_record_appended(self):
        vfs = _Vfs({'3DDATA/STB/ITEM_DROP.STB': drop_stb(7, {0: 'zone text', 1: 12144, 2: 1, 31: 10001, 32: 10002}, 120, {1: 8001, 5: 3001})})
        actors = {'900': dict(drop_row=120)}
        Z.ZONE = 7
        rows, stb = G.drop_rows(vfs, actors, {900})
        self.assertEqual([r['npc'] for r in rows], [900, Z.ZONE_DROP_NPC])
        zr = rows[-1]
        self.assertEqual(zr['drop_row'], 7)
        self.assertEqual(zr['slots'][:2], [12144, 1])          # slot cells 1..30 (cell 0 is the row's text)
        self.assertEqual(zr['pools'][0][:2], [10001, 10002])
        sec = G.build_sections(vfs, actors, items=None, monsters={900})[0]
        drop = [s for s in sec if s[0] == G.TAG_DROP][0]
        dec_rows, items = G.dec_drop(drop[3])
        self.assertEqual([r['npc'] for r in dec_rows], [900, 0])
        self.assertEqual(sorted(i['code'] for i in items), [3001, 8001, 10001, 10002, 12144])          # zone row codes join the item table

    def test_no_zone_rows(self):
        vfs = _Vfs({'3DDATA/STB/ITEM_DROP.STB': drop_stb(7, {}, 120, {1: 8001})})
        actors = {'900': dict(drop_row=120)}
        Z.ZONE = 7          # empty zone row
        rows, _ = G.drop_rows(vfs, actors, {900})
        self.assertEqual([r['npc'] for r in rows], [900])
        Z.ZONE = None       # lobby
        vfs2 = _Vfs({'3DDATA/STB/ITEM_DROP.STB': drop_stb(7, {1: 12144}, 120, {1: 8001})})
        rows, _ = G.drop_rows(vfs2, actors, {900})
        self.assertEqual([r['npc'] for r in rows], [900])
        Z.ZONE = 7          # no monsters in the window: no section content from the zone row alone
        rows, _ = G.drop_rows(vfs2, actors, set())
        self.assertEqual(rows, [])


class ZoneTriggers(unittest.TestCase):
    def test_names(self):
        cells = [[''] * 40 for _ in range(60)]
        cells[56][22], cells[56][23], cells[56][24] = 'T-join', 'T-kill', 'T-dead'
        cells[20][22] = 'T-other'
        vfs = _Vfs({'3DDATA/STB/LIST_ZONE.STB': stb_bytes(cells, 41)})
        self.assertEqual(Z.zone_trigger_names(vfs, 56), [(Z.EV_JOIN, 'T-join'), (Z.EV_KILL, 'T-kill'), (Z.EV_DEAD, 'T-dead')])
        self.assertEqual(Z.zone_trigger_names(vfs, 20), [(Z.EV_JOIN, 'T-other')])
        self.assertEqual(Z.zone_trigger_names(vfs, 21), [])
        self.assertEqual(Z.zone_trigger_names(vfs, None), [])
        self.assertEqual(Z.zone_trigger_names(_Vfs({}), 56), [])

    def test_retag_keeps_everything_else(self):
        """the event entries of the appended names get the fixed ids; every other table and the strings stay byte for byte"""
        sb = RQ.StrBlob()
        tables = dict(trig=[RQ.TRIG.pack(0, 0, 0, 0, -1, sb.add('a')), RQ.TRIG.pack(0, 0, 0, 0, -1, sb.add('b')), RQ.TRIG.pack(0, 0, 0, 0, -1, sb.add('c'))],
                      event=[RQ.PAIR.pack(0, 0), RQ.PAIR.pack(1, 1), RQ.PAIR.pack(3, 2)], death=[RQ.PAIR.pack(451, 2)])
        data = RQ.enc_qust(tables, bytes(sb.buf))
        out = Z._retag_events(data, 1, [(Z.EV_JOIN, 'b'), (Z.EV_KILL, 'x'), (Z.EV_DEAD, 'c')])
        d0, d1 = RQ.dec_qust(data), RQ.dec_qust(out)
        self.assertEqual(d1['tables']['event'], [(0, 0), (Z.EV_JOIN, 1), (Z.EV_DEAD, 2)])
        for k in RQ.TABLES:
            if k != 'event':
                self.assertEqual(d0['tables'][k], d1['tables'][k], k)
        self.assertEqual(d0['strings'], d1['strings'])
        self.assertEqual(len(out), len(data))

    def test_ids_never_collide_with_event_objects(self):
        self.assertGreater(min(Z.EV_JOIN, Z.EV_KILL, Z.EV_DEAD), 65535)          # IFO event object indices are small (one zone's block 12 objects)
        self.assertEqual(len({Z.EV_JOIN, Z.EV_KILL, Z.EV_DEAD}), 3)

    def test_installed(self):
        self.assertIs(G.drop_rows.__doc__, Z.drop_rows_with_zone.__doc__)
        self.assertIs(RQ.build_section.__doc__, Z.build_section_with_zone.__doc__)


if __name__ == '__main__':
    unittest.main()
