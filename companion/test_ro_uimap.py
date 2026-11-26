"""ro_conv.uimap (minimap pictures for the RO windows) on SYNTHETIC fixtures written by this test (no game data anywhere).

  cd companion && python3 -I -m unittest discover -p "test_ro_uimap.py"
"""
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ro_conv import uimap  # noqa: E402


def bmp24(w, h, rgb):
    """A 24 bit BMP of one colour (bottom-up rows padded to 4 bytes)."""
    row = (bytes((rgb[2], rgb[1], rgb[0])) * w)
    row += b'\0' * ((4 - len(row) % 4) % 4)
    data = row * h
    hdr = b'BM' + struct.pack('<IHHI', 54 + len(data), 0, 0, 54) + struct.pack('<IiiHHIIiiII', 40, w, h, 1, 24, 0, len(data), 2835, 2835, 0, 0)
    return hdr + data


class FakeVfs:
    def __init__(self, files):
        self.files = files

    def exists(self, p):
        return p in self.files

    def read(self, p):
        return self.files[p]


class UiMapTest(unittest.TestCase):
    def test_plan(self):
        p = uimap.plan(['prontera', 'prt_in', '', 'prontera', None])
        self.assertEqual(p, [('map_prontera', 'data/texture/유저인터페이스/map/prontera.bmp'), ('map_prt_in', 'data/texture/유저인터페이스/map/prt_in.bmp')])
        self.assertEqual(uimap.tex_name('geffen'), 'map_geffen')

    def test_load_scales_and_reports_missing(self):
        vfs = FakeVfs({uimap.PATH_MAP % 'prontera': bmp24(8, 8, (10, 200, 30)), uimap.PATH_MAP % 'bad': b'not an image at all'})
        out, miss = uimap.load(vfs, ['prontera', 'nowhere', 'bad'], 4)
        self.assertEqual(miss, ['nowhere', 'bad'])
        self.assertEqual(len(out), 1)
        name, w, h, px = out[0]
        self.assertEqual((name, w, h, len(px)), ('map_prontera', 4, 4, 4 * 4 * 4))
        self.assertEqual(tuple(px[:3]), (10, 200, 30))                     # colour survives the resample (R, G, B order)
        self.assertEqual(px[3], 255)

    def test_default_size_is_classic(self):
        self.assertEqual(uimap.DEFAULT_SIZE, 128)


if __name__ == '__main__':
    unittest.main()
