"""ro_conv.rotxt + lub.tables (w43 lane txt, docs/design/ro_rotxt.md) on SYNTHETIC Lua 5.1 chunks written by this test (made-up texts, nothing of the game anywhere).

  cd companion && python3 -I -m unittest discover -p "test_ro_rotxt.py"
  python3 -I test_ro_rotxt.py --write-fixture      (re-writes ../tools/usharp_offline/rotxt_fixture.bin, the synthetic ROTX section the C# rules load)
"""
import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ro_conv import lub, rotxt, ui  # noqa: E402

HEADER = b'\x1bLuaQ' + b'\x00\x01\x04\x04\x04\x08\x00'
FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'tools', 'usharp_offline', 'rotxt_fixture.bin')


class Asm:
    """A tiny Lua 5.1 bytecode writer for straight-line table constructors."""

    def __init__(self):
        self.k, self.code = [], []

    def kidx(self, v):
        if v not in self.k:
            self.k.append(v)
        return self.k.index(v)

    def rk(self, v):
        return 256 + self.kidx(v)

    def abc(self, op, a, b, c):
        self.code.append(op | (a << 6) | (c << 14) | (b << 23))

    def abx(self, op, a, bx):
        self.code.append(op | (a << 6) | (bx << 14))

    def build(self, v, reg):
        if isinstance(v, list):
            self.abc(10, reg, 0, 0)
            for i, x in enumerate(v):
                self.scalar(x, reg + 1 + i)
            self.abc(34, reg, len(v), 1)
            return
        self.abc(10, reg, 0, 0)
        for key, val in v.items():
            if isinstance(val, (dict, list)):
                self.build(val, reg + 1)
                self.abc(9, reg, self.rk(key), reg + 1)
            else:
                self.abc(9, reg, self.rk(key), self.rk(val))

    def scalar(self, x, reg):
        self.abx(1, reg, self.kidx(x))

    def chunk(self, name, value):
        self.build(value, 0)
        self.abx(7, 0, self.kidx(name))
        self.abc(36 - 6, 0, 1, 0)                 # RETURN
        out = HEADER + struct.pack('<I', 0) + struct.pack('<II', 0, 0) + bytes((0, 0, 0, 20))
        out += struct.pack('<I', len(self.code)) + b''.join(struct.pack('<I', c) for c in self.code)
        out += struct.pack('<I', len(self.k))
        for c in self.k:
            if isinstance(c, str):
                b = c.encode('cp949') + b'\0'
                out += b'\x04' + struct.pack('<I', len(b)) + b
            else:
                out += b'\x03' + struct.pack('<d', float(c))
        out += struct.pack('<IIII', 0, 0, 0, 0)  # protos, lines, locals, upvalues
        return out


def item(name, unid, lines, slots):
    return {'unidentifiedDisplayName': unid, 'unidentifiedResourceName': 'x', 'unidentifiedDescriptionName': ['unknown'],
            'identifiedDisplayName': name, 'identifiedResourceName': 'x', 'identifiedDescriptionName': list(lines), 'slotCount': slots, 'ClassNum': 0}


ITEMS = {501: item('Red Potion', 'Red Potion', ['A made-up potion.', 'Class: ^808080Recovery^000000', 'Weight: ^8080807^000000'], 0),
         1201: item('Knife', 'Dagger', ['A made-up blade.', 'Attack: ^80808017^000000', 'Weapon Level: ^8080801^000000', 'Usable By: ^808080Novice Jobs, Swordman Jobs, Merchant Jobs, Thief Jobs^000000'], 3)}
MSGS = ['Zero line#', 'x#', 'y#']
MSGS_FIX = {153: 'You got %s (%d).', 327: 'You got %Zeny', 487: 'You got %s Base EXP.', 488: 'You got %s Job EXP.', 55: 'You do not have enough zeny.'}
TOWN = {'prontera': {1: {'name': 'Convenience', 'X': 146, 'Y': 89, 'TYPE': 6}, 2: {'name': 'Guide', 'X': 154, 'Y': 187, 'TYPE': 4}, 3: {'name': 'Inn', 'X': 204, 'Y': 191, 'TYPE': 5}},
        'geffen': {1: {'name': 'Guide', 'X': 120, 'Y': 100, 'TYPE': 4}}, 'nowhere': {1: {'name': 'Guide', 'X': 1, 'Y': 1, 'TYPE': 4}}}


def fixture_bytes():
    marks = [('prontera', 'Convenience', 6, 146, 89), ('prontera', 'Guide', 4, 154, 187), ('prontera', 'Inn', 5, 204, 191), ('prontera', 'Weapons', 1, 175, 220),
             ('prontera', 'Convenience', 6, 29, 207), ('prontera', 'Guide', 4, 160, 29), ('geffen', 'Guide', 4, 120, 100)]
    items = rotxt.read_items(Asm().chunk('tbl', ITEMS), (501, 1201))
    return rotxt.encode(items, MSGS_FIX, marks)


class Lub(unittest.TestCase):
    def test_tables_nested_and_lists(self):
        t = lub.tables(Asm().chunk('tbl', ITEMS))['tbl']
        self.assertEqual(sorted(t), [501, 1201])
        self.assertEqual(t[501]['identifiedDisplayName'], 'Red Potion')
        self.assertEqual(t[501]['identifiedDescriptionName'], {1: 'A made-up potion.', 2: 'Class: ^808080Recovery^000000', 3: 'Weight: ^8080807^000000'})
        self.assertEqual(t[1201]['slotCount'], 3)

    def test_rejects_other_files(self):
        with self.assertRaises(lub.LubError):
            lub.tables(b'not lua')
        bad = bytearray(Asm().chunk('tbl', {1: 2}))
        with self.assertRaises(lub.LubError):
            lub.tables(bytes(bad[:20]))                       # truncated

    def test_opcode_outside_subset(self):
        a = Asm()
        a.abc(21, 0, 0, 0)                                    # ADD: not part of the data subset
        a.abc(30, 0, 1, 0)
        raw = HEADER + struct.pack('<I', 0) + struct.pack('<II', 0, 0) + bytes((0, 0, 0, 20)) + struct.pack('<I', 2) + b''.join(struct.pack('<I', c) for c in a.code) + struct.pack('<IIIII', 0, 0, 0, 0, 0)
        with self.assertRaises(lub.LubError):
            lub.tables(raw)


class Texts(unittest.TestCase):
    def test_items_messages_markers(self):
        it = rotxt.read_items(Asm().chunk('tbl', ITEMS), (501, 1201, 9999))
        self.assertEqual(sorted(it), [501, 1201])                      # an id the client lacks is simply absent
        self.assertEqual(it[1201][0:2], ('Knife', 'Dagger'))
        self.assertEqual(it[1201][3], 3)
        self.assertEqual(it[501][2].split('\n')[1], 'Class: ^808080Recovery^000000')
        msgs = rotxt.read_messages(b'Zero line#\r\nOne#\nTwo#\n\nFour#', (0, 1, 2, 3, 4, 99))
        self.assertEqual(msgs, {0: 'Zero line', 1: 'One', 2: 'Two', 4: 'Four'})          # empty lines + out of range dropped, the trailing '#' removed
        marks = rotxt.read_markers(Asm().chunk('mapNPCInfoTable', TOWN), ('prontera', 'geffen'))
        self.assertEqual([m[0] for m in marks], ['prontera'] * 3 + ['geffen'])
        self.assertEqual(marks[0], ('prontera', 'Convenience', 6, 146, 89))

    def test_encode_decode_round_trip_ascii_only(self):
        items = {7: ('Café Blade', 'Blade', 'line one\nline two', 2)}
        d = rotxt.encode(items, {153: 'You got %s (%d).'}, [('prt_in', 'Inn', 5, 3, 4)])
        it, ms, mk = rotxt.decode(d)
        self.assertEqual(it[7], ('Caf? Blade', 'Blade', 'line one\nline two', 2))               # non ASCII -> '?' (the world reads bytes as chars)
        self.assertEqual(ms, {153: 'You got %s (%d).'})
        self.assertEqual(mk, [('prt_in', 'Inn', 5, 3, 4)])
        hdr = rotxt.HDR.unpack_from(d, 0)
        self.assertEqual(hdr[0:4], (1, 1, 1, 1))
        self.assertEqual(hdr[5], rotxt.HDR.size)                                           # item table right behind the header (GsvrRoTxt.LCheck layout)
        self.assertEqual(len(d), hdr[7] + rotxt.MARK.size + hdr[4])                         # marker table + pool end the section

    def test_section_from_folder(self):
        tmp = tempfile.mkdtemp(prefix='rotxt_')
        os.makedirs(os.path.join(tmp, 'System'))
        with open(os.path.join(tmp, 'System', 'ITEMINFO.LUB'), 'wb') as f:                   # case-insensitive file names (the client folder is Windows)
            f.write(Asm().chunk('tbl', ITEMS))
        with open(os.path.join(tmp, 'System', 'Towninfo.lub'), 'wb') as f:
            f.write(Asm().chunk('mapNPCInfoTable', TOWN))

        class V:
            def read(self, p):
                if p != rotxt.PATH_MSG:
                    raise KeyError(p)
                return b'\n' * 153 + b'You got %s (%d).#\n'
        warn = []
        tag, ver, count, data = rotxt.section(tmp, V(), (501, 1201, 2222), ('prontera',), warn)
        self.assertEqual((tag, ver), (b'ROTX', 1))
        it, ms, mk = rotxt.decode(data)
        self.assertEqual(sorted(it), [501, 1201])
        self.assertEqual(ms, {153: 'You got %s (%d).'})
        self.assertEqual(len(mk), 3)
        self.assertEqual(count, 2 + 1 + 3)
        self.assertTrue(any('1 of 3 world items' in w for w in warn))

    def test_missing_files_only_drop_their_part(self):
        tmp = tempfile.mkdtemp(prefix='rotxt_')

        class V:
            def read(self, p):
                raise KeyError(p)
        warn = []
        self.assertIsNone(rotxt.section(tmp, V(), (501,), ('prontera',), warn))            # nothing at all -> no section, warnings name the files
        self.assertEqual(len(warn), 3)
        os.makedirs(os.path.join(tmp, 'System'))
        with open(os.path.join(tmp, 'System', 'iteminfo.lub'), 'wb') as f:
            f.write(b'broken')
        warn = []
        self.assertIsNone(rotxt.section(tmp, V(), (501,), ('prontera',), warn))
        self.assertTrue(any('unreadable' in w for w in warn))

    def test_message_ids_are_the_documented_ones(self):
        for i in (153, 327, 487, 488, 284, 259, 52, 55, 202, 203):
            self.assertIn(i, rotxt.MSG_IDS)
        self.assertEqual(list(rotxt.MSG_IDS), sorted(set(rotxt.MSG_IDS)))

    def test_fixture_in_sync(self):
        self.assertTrue(os.path.exists(FIXTURE), 'run: python3 -I test_ro_rotxt.py --write-fixture')
        with open(FIXTURE, 'rb') as f:
            self.assertEqual(f.read(), fixture_bytes())


class UiPack(unittest.TestCase):
    def test_pack_carries_rotxt_and_rev(self):
        self.assertGreaterEqual(ui.UI_REV, 3)
        for n in ('statwin0_bg', 'skill_upbar_left', 'skill_botbar_mid', 'tab_a_m', 'tab_l', 'sysboxs_lu', 'btn_out_mid'):
            self.assertIn(n, ui.UI_FILES)
        self.assertEqual(len(set(ui.UI_FILES)), len(ui.UI_FILES))                           # no piece twice
        self.assertIs(ui.rotxt, rotxt)


if __name__ == '__main__':
    if '--write-fixture' in sys.argv:
        with open(FIXTURE, 'wb') as f:
            f.write(fixture_bytes())
        print('wrote', os.path.abspath(FIXTURE))
    else:
        unittest.main()
