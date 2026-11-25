"""ro_conv (Ragnarok Online -> RPG1) on SYNTHETIC fixtures written by this test (no game data anywhere).

  cd companion && python3 -I -m unittest discover -p "test_ro_conv.py"
"""
import math
import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ro_conv import grf, rofmt, img, mappack as B  # noqa: E402
from ro_conv.__main__ import main as cli  # noqa: E402
import rpg1 as P  # noqa: E402  (ro_conv.mappack put companion/rose_conv on sys.path)
import rosedds as D  # noqa: E402


# ------------------------------------------------------------------ fixture writers (layouts as documented in ro_conv.rofmt)
def _name(s, n):
    b = s.encode('cp949')
    return b + b'\0' * (n - len(b))


def gat_bytes(w, h, heights, types):
    out = b'GRAT' + bytes((1, 2)) + struct.pack('<ii', w, h)
    for i in range(w * h):
        out += struct.pack('<4fI', *heights[i], types[i])
    return out


def gnd_bytes(w, h, textures, surfaces, cubes, lightmaps, zoom=10.0, ver=(1, 7), water=None):
    out = b'GRGN' + bytes(ver) + struct.pack('<iif', w, h, zoom) + struct.pack('<ii', len(textures), 80)
    out += b''.join(_name(t, 80) for t in textures)
    out += struct.pack('<4i', len(lightmaps), 8, 8, 1) + b''.join(lightmaps)
    out += struct.pack('<i', len(surfaces))
    for u, v, tex, lm, bgra in surfaces:
        out += struct.pack('<8fhH4B', *u, *v, tex, lm, *bgra)
    for c in cubes:
        out += struct.pack('<4f3i', *c)
    if water is not None:
        out += struct.pack('<fifffi', water, 0, 1.0, 2.0, 50.0, 3)
    return out


def rsw_bytes(gnd, gat, models, water=5.0, extra=b'', ver=(2, 1)):
    out = b'GRSW' + bytes(ver) + extra + _name('', 40) + _name(gnd, 40) + _name(gat, 40) + _name('', 40)
    out += struct.pack('<fifffi', water, 0, 1.0, 2.0, 50.0, 3)
    out += struct.pack('<ii3f3ff', 45, 45, 1.0, 1.0, 1.0, 0.3, 0.3, 0.3, 0.5)
    out += struct.pack('<4i', 0, 0, 0, 0)
    objs = b''
    n = 0
    for f, pos, rot, sc in models:
        objs += struct.pack('<i', 1) + _name('obj%d' % n, 40) + struct.pack('<ifi', 0, 1.0, 0) + _name(f, 80) + _name('', 80)
        objs += struct.pack('<9f', *pos, *rot, *sc); n += 1
    objs += struct.pack('<i', 2) + _name('light', 80) + struct.pack('<3f3if', 0, 0, 0, 255, 255, 255, 10.0); n += 1
    objs += struct.pack('<i', 3) + _name('snd', 80) + _name('a.wav', 80) + struct.pack('<3ffiiff', 0, 0, 0, 1.0, 10, 10, 50.0, 4.0); n += 1
    objs += struct.pack('<i', 4) + _name('fx', 80) + struct.pack('<3fif4f', 0, 0, 0, 1, 0.5, 0, 0, 0, 0); n += 1
    return out + struct.pack('<i', n) + objs


def cube_node(name, parent, size, pos=(0, 0, 0), offset=(0, 0, 0)):
    """axis aligned box of half-size `size` around the origin, one texture slot."""
    vs = [(x * size, y * size, z * size) for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)]
    quads = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    faces = []
    for a, b, c, d in quads:
        faces.append((a, b, c, 0, 1, 2)); faces.append((a, c, d, 0, 2, 3))
    return dict(name=name, parent=parent, tex=[0], mat3=(1, 0, 0, 0, 1, 0, 0, 0, 1), offset=offset, pos=pos, rotangle=0.0, rotaxis=(0, 0, 0),
                scale=(1, 1, 1), verts=vs, tverts=[(0, 0), (1, 0), (1, 1), (0, 1)], faces=faces)


def rsm_bytes(textures, nodes, main, ver=(1, 4), shade=2):
    out = b'GRSM' + bytes(ver) + struct.pack('<ii', 0, shade) + bytes((255,)) + b'\0' * 16
    out += struct.pack('<i', len(textures)) + b''.join(_name(t, 40) for t in textures) + _name(main, 40) + struct.pack('<i', len(nodes))
    for n in nodes:
        out += _name(n['name'], 40) + _name(n['parent'], 40) + struct.pack('<i', len(n['tex'])) + b''.join(struct.pack('<i', t) for t in n['tex'])
        out += struct.pack('<9f3f3ff3f3f', *n['mat3'], *n['offset'], *n['pos'], n['rotangle'], *n['rotaxis'], *n['scale'])
        out += struct.pack('<i', len(n['verts'])) + b''.join(struct.pack('<3f', *v) for v in n['verts'])
        out += struct.pack('<i', len(n['tverts'])) + b''.join(struct.pack('<I2f', 0xffffffff, *t) for t in n['tverts'])
        out += struct.pack('<i', len(n['faces'])) + b''.join(struct.pack('<6HHHii', *f[:6], 0, 0, 0, f[6] if len(f) > 6 else 0) for f in n['faces'])
        out += struct.pack('<i', 0)          # rotation keys
    return out


def solid(w, h, rgb):
    return bytes(list(rgb) + [255]) * (w * h)


KTEX = '유저인터페이스\\ground.bmp'          # Korean folder name like the real client (CP949 in the archive)


def make_map(root, models=True, water=5.0, ver=(2, 1), extra=b''):
    W = H = 4
    lm = bytes([200] * 64) + bytes([10, 20, 30] * 64)
    surfs = [((0, 1, 0, 1), (0, 0, 1, 1), 0, 0, (255, 255, 255, 255)), ((0, 1, 0, 1), (0, 0, 1, 1), 1, 0, (255, 255, 255, 255))]
    cubes = []
    for ty in range(H):
        for tx in range(W):
            h = -10.0 if (tx, ty) == (2, 2) else 0.0
            front = 1 if (tx, ty) == (2, 1) else -1
            cubes.append((h, h, h, h, 0, front, -1))
    gnd = gnd_bytes(W, H, [KTEX, 'wall.bmp'], surfs, cubes, [lm])
    gw = gh = 8
    hts = []; types = []
    for cy in range(gh):
        for cx in range(gw):
            h = -10.0 if (cx // 2, cy // 2) == (2, 2) else 0.0
            hts.append((h, h, h, h))
            types.append(1 if (cx, cy) == (0, 0) else (5 if (cx, cy) == (7, 7) else 0))
    gat = gat_bytes(gw, gh, hts, types)
    ms = [('box.rsm', (-5.0, 0.0, -15.0), (0, 0, 0), (1, 1, 1)), ('missing.rsm', (5.0, -10.0, 5.0), (0, 0, 0), (1, 1, 1))] if models else []
    rsw = rsw_bytes('testmap.gnd', 'testmap.gat', ms, water=water, ver=ver, extra=extra)
    tex = img.write_bmp24(16, 16, solid(16, 16, (40, 160, 40)))
    pal = [(255, 0, 255), (200, 100, 50)]
    boxtex = img.write_bmp8(8, 8, [0 if (i % 8) < 2 else 1 for i in range(64)], pal)
    rsm = rsm_bytes(['box.bmp'], [cube_node('main', '', 5.0)], 'main')
    files = {'data\\testmap.rsw': rsw, 'data\\testmap.gnd': gnd, 'data\\testmap.gat': gat, 'data\\texture\\' + KTEX: tex,
             'data\\texture\\wall.bmp': img.write_bmp24(8, 8, solid(8, 8, (90, 90, 90))),
             'data\\texture\\box.bmp': boxtex, 'data\\model\\box.rsm': rsm}
    grf.write_grf(os.path.join(root, 'data.grf'), files)
    return files


# ------------------------------------------------------------------ Python mirror of the C# pack validators (GsvrRpg1Pack.Validate*)
def validate_like_loader(pack):
    p = P.read_pack(pack)
    S = p['sections']
    for t in (b'AREA', b'CELL', b'HGHT', b'TERR', b'TATL', b'TTIL', b'OTEX', b'OMAT', b'OMSH', b'SPWN', b'SPWE'):
        assert t in S, t
    a = P.dec_area(S[b'AREA']['data'])
    assert 1 <= a['grid_w'] <= 4096 and 1 <= a['grid_h'] <= 4096 and a['cell_size'] > 0 and a['height_unit'] > 0
    assert 1 <= a['blocks_x'] <= 64 and 1 <= a['blocks_y'] <= 64
    n = a['grid_w'] * a['grid_h']
    assert len(S[b'CELL']['data']) == n and len(S[b'HGHT']['data']) == 2 * n
    terr = P.dec_terr(S[b'TERR']['data'])
    assert len(terr) <= a['blocks_x'] * a['blocks_y']
    seen = set()
    for b in terr:
        assert 0 <= b['bx'] - a['first_bx'] < a['blocks_x'] and 0 <= b['by'] - a['first_by'] < a['blocks_y']
        assert (b['bx'], b['by']) not in seen; seen.add((b['bx'], b['by']))
        assert len(b['verts']) <= 65535
        for tri in b['tris']:
            assert max(tri) < len(b['verts'])
    d = S[b'TATL']['data']
    fmt, w, h, mips, ns, spx, gut, _ = struct.unpack_from('<8I', d, 0)
    assert ns <= (w // spx) * (h // spx) and 2 * gut < spx and w >= spx and mips <= 14
    for i in range(ns):
        r = struct.unpack_from('<4f', d, 32 + i * 24 + 4)
        sr = B.slot_rect(i, spx, gut, w, h, 1.0)
        for k in range(4):
            assert abs(r[k] - sr[k]) < 0.5 / w + 1e-6, (r, sr)
    size = 32 + ns * 24
    lw, lh = w, h
    for _ in range(mips):
        size += max(1, (lw + 3) // 4) * max(1, (lh + 3) // 4) * (8 if fmt == 1 else 16)
        lw = max(1, lw // 2); lh = max(1, lh // 2)
    assert len(d) >= size
    tt = S[b'TTIL']['data']
    tc, bc = struct.unpack_from('<2I', tt, 0)
    assert 8 + tc * 8 + bc * 520 <= len(tt)
    texs = P.dec_otex(S[b'OTEX']['data'])
    mats = P.dec_omat(S[b'OMAT']['data'])
    for m in mats:
        assert m['tex'] == P.NO_TEX or m['tex'] < len(texs)
        assert 0.0 <= m['alpha_ref'] <= 1.0
    chunks = P.dec_omsh(S[b'OMSH']['data'])
    for c in chunks:
        assert 0 <= c['mat'] < len(mats) and len(c['verts']) <= 65535 and len(c['idx']) % 3 == 0
        assert max(c['idx']) < len(c['verts'])
    if b'WATR' in S:
        wt = P.dec_watr(S[b'WATR']['data'])
        assert 1 <= wt['fps'] <= 120 and wt['tile'] > 0.01
        for pl in wt['planes']:
            assert pl['x0'] < pl['x1'] and pl['z0'] < pl['z1']
    # TLMP (GsvrRpg1Pack.ValidateTlmp) + the offline uv1 rule of ro_e2e (TerrUv1OutsideBlock): ro_conv 3 always writes it
    assert b'TLMP' in S
    lt = S[b'TLMP']['data']
    fmt, w, h, mips, ns, spx, cols, _r = struct.unpack_from('<8I', lt, 0)
    assert fmt in (1, 2, 3) and 4 <= w <= 4096 and 4 <= h <= 4096 and w % 4 == 0 and h % 4 == 0
    assert 4 <= spx <= 64 and spx % 4 == 0 and w % spx == 0 and h % spx == 0 and cols == w // spx
    assert 1 <= ns <= cols * (h // spx) and 1 <= mips <= 6 and (spx >> (mips - 1)) >= 2
    size = 32; lw, lh = w, h
    for _ in range(mips):
        size += (lw * lh * 4) if fmt == 3 else ((lw + 3) // 4) * ((lh + 3) // 4) * (8 if fmt == 1 else 16)
        lw = max(1, lw // 2); lh = max(1, lh // 2)
    assert len(lt) >= size
    for b in terr:
        for v in b['verts']:
            assert 0.0 <= v[12] <= 1.0 and 0.0 <= v[13] <= 1.0, v
    return p, a, terr, texs, mats, chunks


class GrfTest(unittest.TestCase):
    def test_read_korean_case_insensitive(self):
        with tempfile.TemporaryDirectory() as d:
            grf.write_grf(os.path.join(d, 'data.grf'), {'data\\Texture\\유저인터페이스\\T.BMP': b'hello', 'data\\a.txt': b'x' * 1000})
            v = grf.Vfs(d)
            self.assertEqual(v.read('DATA/texture/유저인터페이스/t.bmp'), b'hello')
            self.assertEqual(v.read('data\\A.TXT'), b'x' * 1000)
            self.assertTrue(v.exists('texture\\유저인터페이스\\t.bmp'))         # 'data\\' prefix added
            with self.assertRaises(KeyError):
                v.read('data\\nope.txt')

    def test_priority_rdata_ini_and_loose(self):
        with tempfile.TemporaryDirectory() as d:
            grf.write_grf(os.path.join(d, 'data.grf'), {'data\\f.txt': b'data', 'data\\g.txt': b'data'})
            grf.write_grf(os.path.join(d, 'rdata.grf'), {'data\\f.txt': b'rdata'})
            v = grf.Vfs(d)
            self.assertEqual(v.read('data\\f.txt'), b'rdata')
            self.assertEqual(v.read('data\\g.txt'), b'data')
            with open(os.path.join(d, 'DATA.INI'), 'w') as f:
                f.write('[Data]\n0=data.grf\n1=rdata.grf\n')
            self.assertEqual(grf.Vfs(d).read('data\\f.txt'), b'data')
            os.makedirs(os.path.join(d, 'Data', 'Sub'))
            with open(os.path.join(d, 'Data', 'Sub', 'G.TXT'), 'wb') as f:
                f.write(b'loose')
            v = grf.Vfs(d)
            self.assertEqual(v.read('data\\sub\\g.txt'), b'loose')
            self.assertEqual(grf.Vfs(d, loose=False).read('data\\g.txt'), b'data')

    def test_bad_archives(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'x.grf')
            with open(p, 'wb') as f:
                f.write(b'not a grf at all' * 4)
            with self.assertRaises(grf.GrfError):
                grf.Grf(p)
            grf.write_grf(p, {'data\\a': b'1'}, version=0x103)
            with self.assertRaises(grf.GrfError):
                grf.Grf(p)
            with self.assertRaises(grf.GrfError):
                grf.Vfs(d)            # the only archive is unreadable, no data folder

    def test_name_decoding(self):
        self.assertEqual(grf.norm('유저인터페이스'.encode('cp949') + b'\\A.BMP\0junk'), '유저인터페이스\\a.bmp')
        self.assertEqual(grf.norm(b'\\data//x\\\\Y'), 'data\\x\\y')
        self.assertEqual(grf.decode_name(b'\xff\xfe'), '\xff\xfe')       # invalid CP949 stays unique (latin-1)


def _test_encode(buf, flags, packed):
    """test-side ENCRYPT counterpart of grfcrypt.decode (the converter itself only decodes): DES is its own inverse, the MIXCRYPT shuffle
    is inverted here."""
    from ro_conv import grfcrypt as C
    sub = C._SUB
    out = bytearray(buf)
    for i, op in C._plan(len(out) // 8, flags, packed):
        o = i * 8; b = bytes(out[o:o + 8])
        out[o:o + 8] = C.des_block(b) if op == 'd' else bytes((b[3], b[4], b[5], b[0], b[1], b[6], b[2], sub[b[7]]))
    return bytes(out)


def write_grf300(path, files, dirs=(), magic=b'Event Horizon'):
    """SYNTHETIC GRF 0x300: {name: (bytes, flags)} (flags 1 plain, 3 MIXCRYPT, 5 DES), u64 table offset + u64 entry offsets, the u32 0 pad
    before the table sizes, directory entries (flag 0, dummy sizes) that the reader must skip."""
    import zlib
    body = bytearray(); table = bytearray()
    for name in dirs:
        table += name.encode('cp949') + b'\0' + struct.pack('<IIIBQ', 1097, 1104, 1812, 0, 0)
    for name, (data, fl) in files.items():
        comp = zlib.compress(data)
        al = (len(comp) + 7) & ~7
        blob = comp + b'\0' * (al - len(comp))
        if fl & 6:
            blob = _test_encode(blob, fl, len(comp))
        table += name.encode('cp949') + b'\0' + struct.pack('<IIIBQ', len(comp), al, len(data), fl, len(body))
        body += blob
    ct = zlib.compress(bytes(table))
    hd = magic + b'\0' * (16 - len(magic)) + bytes(range(1, 15)) + struct.pack('<QII', len(body), len(files) + len(dirs), 0x300)
    with open(path, 'wb') as f:
        f.write(hd + bytes(body) + struct.pack('<III', 0, len(ct), len(table)) + ct)


class Grf300Test(unittest.TestCase):
    def test_des_block_known_and_involution(self):
        from ro_conv import grfcrypt as C
        # one Feistel round, zero key: decrypting twice is the identity; zero block -> fixed value (FIPS tables)
        for b in (bytes(8), b'\x01\x23\x45\x67\x89\xab\xcd\xef', b'\xff' * 8):
            self.assertEqual(C.des_block(C.des_block(b)), b)
        self.assertNotEqual(C.des_block(bytes(8)), bytes(8))
        self.assertEqual([C.mix_cycle(n) for n in (9, 99, 100, 9999, 10000, 999999, 1000000)], [1, 1, 4, 5, 14, 15, 22])

    def test_round_trip_encrypted_entries(self):
        import random
        r = random.Random(7)
        big = bytes(r.randrange(256) for _ in range(60000))          # incompressible: many blocks, mix cycle > 1, shuffled blocks
        files = {
            'data\\sprite\\몬스터\\Poring.SPR': (big, 3),
            'data\\sprite\\인간족\\머리통\\남\\1_남.act': (b'AC' + bytes(r.randrange(256) for _ in range(3000)), 5),
            'data\\prontera.rsw': (b'GRSW' + b'x' * 5000, 1),
            'data\\tiny.txt': (b'abc', 3),                         # shorter than one block
        }
        with tempfile.TemporaryDirectory() as d:
            write_grf300(os.path.join(d, 'data.grf'), files, dirs=('data\\sprite', 'data\\sprite\\몬스터'))
            g = grf.Grf(os.path.join(d, 'data.grf'))
            self.assertEqual(g.version, 0x300)
            self.assertEqual(g.count, 6)
            self.assertEqual(g.dirs, 2)
            self.assertEqual(len(g.entries), 4)
            self.assertEqual(g.encrypted, 3)
            v = grf.Vfs(d)
            self.assertEqual(v.read('data/sprite/몬스터/poring.spr'), big)
            self.assertEqual(v.read('DATA\\SPRITE\\인간족\\머리통\\남\\1_남.ACT'), files['data\\sprite\\인간족\\머리통\\남\\1_남.act'][0])
            self.assertEqual(v.read('prontera.rsw'), files['data\\prontera.rsw'][0])
            self.assertEqual(v.read('data\\tiny.txt'), b'abc')
            self.assertFalse(v.exists('data\\sprite'))              # directory entries are not files
            from ro_conv import api
            self.assertEqual(api.check_install(d), (True, 'ok'))     # "Event Horizon" magic accepted

    def test_without_decrypt_it_would_fail(self):
        import zlib
        data = b'hello world ' * 300
        comp = zlib.compress(data)
        enc = _test_encode(comp + b'\0' * (-len(comp) % 8), 5, len(comp))
        with self.assertRaises(zlib.error):
            zlib.decompress(enc[:len(comp)])

    def test_event_grf_ignored_by_default(self):
        with tempfile.TemporaryDirectory() as d:
            write_grf300(os.path.join(d, 'data.grf'), {'data\\prontera.rsw': (b'normal', 1)})
            grf.write_grf(os.path.join(d, 'event.grf'), {'data\\prontera.rsw': b'christmas'})
            self.assertEqual(grf.Vfs(d).read('data\\prontera.rsw'), b'normal')
            self.assertEqual(len(grf.Vfs(d).grfs), 1)
            v = grf.Vfs(d, exclude=())
            self.assertEqual(len(v.grfs), 2)
            self.assertEqual(v.read('data\\prontera.rsw'), b'normal')                           # opted in: data.grf still wins
            with open(os.path.join(d, 'DATA.INI'), 'w') as f:
                f.write('[Data]\n0=event.grf\n1=data.grf\n')
            self.assertEqual(grf.Vfs(d).read('data\\prontera.rsw'), b'christmas')              # listed in DATA.INI: used


class ImageTest(unittest.TestCase):
    def test_bmp_roundtrip_and_magenta(self):
        px = bytes(sum(([x * 10, y * 10, 5, 255] for y in range(4) for x in range(5)), []))
        w, h, out = img.decode(img.write_bmp24(5, 4, px))
        self.assertEqual((w, h, out), (5, 4, px))
        w, h, out = img.decode(img.write_bmp8(3, 2, [0, 1, 1, 1, 0, 1], [(255, 0, 255), (10, 20, 30)]))
        self.assertEqual(out[:4], bytes((255, 0, 255, 255)))          # row 0 = top row
        k, alpha = img.key_magenta(out, w, h)
        self.assertTrue(alpha)
        self.assertEqual(k[3], 0)
        self.assertEqual(k[:3], bytes((10, 20, 30)))                   # colour bled from the opaque neighbours
        self.assertEqual(img.pow2_at_most(300, 256), 256)
        self.assertEqual(len(img.resize(out, 3, 2, 4, 4)), 64)

    def test_tga(self):
        hdr = bytes([0, 0, 2]) + b'\0' * 9 + struct.pack('<HHBB', 2, 1, 32, 0x20)
        w, h, out = img.decode(hdr + bytes([1, 2, 3, 4, 5, 6, 7, 8]), 'x.tga')
        self.assertEqual(out, bytes([3, 2, 1, 4, 7, 6, 5, 8]))
        with self.assertRaises(img.ImageError):
            img.decode(b'\xff\xd8\xff\xe0' + b'\0' * 40, 'x.jpg')


class FormatTest(unittest.TestCase):
    def test_gat_gnd_rsw(self):
        with tempfile.TemporaryDirectory() as d:
            files = make_map(d)
        t = rofmt.gat(files['data\\testmap.gat'])
        self.assertEqual((t['w'], t['h']), (8, 8))
        self.assertEqual(t['types'][0], 1)
        g = rofmt.gnd(files['data\\testmap.gnd'])
        self.assertEqual((g['w'], g['h'], g['zoom']), (4, 4, 10.0))
        self.assertEqual(g['textures'][0], KTEX)
        self.assertEqual(g['surfaces'][0][4], (255, 255, 255, 255))
        self.assertEqual(g['cubes'][2 * 4 + 2][0], -10.0)
        self.assertEqual(rofmt.lightmap_px(g, 0, 3, 3), (200, (10, 20, 30)))
        r = rofmt.rsw(files['data\\testmap.rsw'])
        self.assertEqual((r['gnd'], r['gat']), ('testmap.gnd', 'testmap.gat'))
        self.assertEqual(len(r['models']), 2)
        self.assertEqual(r['counts'], dict(lights=1, sounds=1, effects=1))
        self.assertAlmostEqual(r['water']['level'], 5.0)
        self.assertEqual(r['warnings'], [])

    def test_rsw_header_variants(self):
        for ver, extra in (((2, 2), b'\x07'), ((2, 5), struct.pack('<i', 123) + b'\x00')):
            b = rsw_bytes('m.gnd', 'm.gat', [('box.rsm', (1, 2, 3), (0, 0, 0), (1, 1, 1))], ver=ver, extra=extra)
            r = rofmt.rsw(b)
            self.assertEqual(r['gnd'], 'm.gnd', ver)
            self.assertEqual(r['models'][0]['pos'], (1.0, 2.0, 3.0))
        self.assertEqual(rofmt.rsw(rsw_bytes('m.gnd', 'm.gat', [], ver=(2, 5), extra=struct.pack('<i', 123) + b'\x00'))['build'], 123)

    def test_gnd_water_and_errors(self):
        g = rofmt.gnd(gnd_bytes(1, 1, ['a.bmp'], [], [(0, 0, 0, 0, -1, -1, -1)], [], ver=(1, 8), water=3.5))
        self.assertAlmostEqual(g['water']['level'], 3.5)
        with self.assertRaises(rofmt.FormatError):
            rofmt.gnd(b'GRGN' + bytes((1, 5)) + b'\0' * 40)
        with self.assertRaises(rofmt.FormatError):
            rofmt.gat(b'GRAT\x01\x02' + struct.pack('<ii', 10, 10))          # truncated
        with self.assertRaises(rofmt.FormatError):
            rofmt.rsm(b'GRSM' + bytes((2, 2)) + b'\0' * 64)                     # RSM2 not yet

    def test_rsm_centering_and_hierarchy(self):
        m = rofmt.rsm(rsm_bytes(['t.bmp'], [cube_node('main', '', 5.0)], 'main'))
        parts = B.model_parts(m)
        vs, ts = parts[0]
        self.assertEqual(len(ts), 12)
        ys = [v[1] for v in vs]
        self.assertAlmostEqual(max(ys), 0.0)               # lowest point (Y down) on the ground
        self.assertAlmostEqual(min(ys), -10.0)
        self.assertAlmostEqual(sum(v[0] for v in vs) / len(vs), 0.0)
        # child 20 units east of the root (pos), plus its own offset 3: whole model centred again
        m2 = rofmt.rsm(rsm_bytes(['t.bmp'], [cube_node('main', '', 1.0), cube_node('kid', 'main', 1.0, pos=(20, 0, 0), offset=(3, 0, 0))], 'main'))
        vs, _ = B.model_parts(m2)[0]
        xs = sorted(set(round(v[0], 4) for v in vs))
        self.assertEqual(xs, [-12.5, -10.5, 10.5, 12.5])  # root box -1..1, child box (pos 20 + offset 3) 22..24 -> centre 11.5

    def test_rotation_matrix(self):
        m = B._rz(90)
        x, y, z = B._ap(m, (1, 0, 0))
        self.assertAlmostEqual(x, 0.0); self.assertAlmostEqual(y, 1.0)
        q = B._quat(0, 0, math.sin(math.pi / 4), math.cos(math.pi / 4))
        for a, b in zip(q, m):
            self.assertAlmostEqual(a, b)



class LookTest(unittest.TestCase):
    """Ground light (lightmap shadows) and model shading (2026-11-25 look pass: owner compared with the real client)."""

    @staticmethod
    def _lm(shadow_fn, rgb=(0, 0, 0)):
        return bytes(shadow_fn(x, y) for y in range(8) for x in range(8)) + bytes(rgb) * 64

    def test_lightmap_sample_bilinear(self):
        g = dict(lightmaps=dict(n=1, cw=8, ch=8, data=self._lm(lambda x, y: 100 if x < 4 else 200, (8, 16, 24))))
        self.assertEqual(rofmt.lightmap_sample(g, 0, 2.5, 3.5), (100.0, (8.0, 16.0, 24.0)))       # texel centre
        self.assertAlmostEqual(rofmt.lightmap_sample(g, 0, 4.0, 3.5)[0], 150.0)                  # between texel 3 (100) and 4 (200)
        self.assertEqual(rofmt.lightmap_sample(g, 0, -3, 99)[0], 100.0)                           # clamped to the edge texel
        self.assertEqual(rofmt.lightmap_sample(g, -1, 1, 1), (255.0, (0.0, 0.0, 0.0)))           # no lightmap = lit

    def _shadow_map(self, d):
        # 2 x 1 tiles: tile 0 has a hard shadow edge (left half of its 6x6 interior at 128), tile 1 is uniformly lit (255)
        W, H = 2, 1
        lm_edge = self._lm(lambda x, y: 128 if x <= 3 else 255)
        lm_lit = self._lm(lambda x, y: 255)
        surfs = [((0, 1, 0, 1), (0, 0, 1, 1), 0, 0, (255, 255, 255, 255)), ((0, 1, 0, 1), (0, 0, 1, 1), 0, 1, (255, 255, 255, 255))]
        cubes = [(0.0, 0.0, 0.0, 0.0, 0, -1, -1), (0.0, 0.0, 0.0, 0.0, 1, -1, -1)]
        files = {'data\\lm.rsw': rsw_bytes('lm.gnd', 'lm.gat', []),
                 'data\\lm.gnd': gnd_bytes(W, H, [KTEX], surfs, cubes, [lm_edge, lm_lit]),
                 'data\\lm.gat': gat_bytes(4, 2, [(0.0, 0.0, 0.0, 0.0)] * 8, [0] * 8),
                 'data\\texture\\' + KTEX: img.write_bmp24(16, 16, solid(16, 16, (200, 200, 200)))}
        grf.write_grf(os.path.join(d, 'data.grf'), files)

    @staticmethod
    def _lm_texel(pack, u, v):
        """the decoded TLMP mip 0 texel (r, g, b, a) at atlas uv (u, v) (nearest)."""
        at = B.dec_tlmp(P.read_pack(pack)['sections'][b'TLMP']['data'])
        px = D.decode_dxt('DXT5', at['levels'][0], at['w'], at['h'])
        x = min(at['w'] - 1, int(u * at['w'])); y = min(at['h'] - 1, int(v * at['h']))
        o = (y * at['w'] + x) * 4
        return tuple(px[o:o + 4]), at

    def test_ground_keeps_lightmap_shadow(self):
        """ro_conv 3: the shadow edge lives in the TLMP lightmap atlas (uv1), the ground keeps 4 vertices per tile."""
        with tempfile.TemporaryDirectory() as d:
            self._shadow_map(d)
            pack, man = B.build(grf.Vfs(d), 'lm', log=lambda *_: None)
        p, a, terr, texs, mats, chunks = validate_like_loader(pack)
        vs = terr[0]['verts']
        self.assertEqual(len(vs), 8)                                          # 2 tiles x 4 corners, no subdivision
        self.assertEqual(len(terr[0]['tris']), 4)
        self.assertNotIn('terrain_subdiv', man)
        self.assertEqual(man['lightmap']['lightmaps'], 2)
        self.assertEqual(man['lightmap']['slots'], 3)                         # lit slot 0 + the two lightmaps
        self.assertTrue(all(v[6:9] == (255, 255, 255) for v in vs))           # vertex colour = the tile colour (the shadow is in the atlas)
        # tile 0 (x 0..2 m): uv1 spans its slot's interior; the left half of the 6x6 interior is shadowed (128), the right half lit
        t0 = sorted(vs[0:4], key=lambda v: (v[2], v[0]))                     # tile 0 = the first 4 vertices
        u0, v0, u1 = t0[0][12], t0[0][13], t0[1][12]
        self.assertGreater(u1, u0)
        at = B.dec_tlmp(p['sections'][b'TLMP']['data'])
        self.assertAlmostEqual((u1 - u0) * at['w'], 6.0, places=4)          # 6 texels per tile (1 .. 7 of the 8x8 slot)
        left, _ = self._lm_texel(pack, u0 + 1.5 / at['w'], v0 + 2.5 / at['h'])
        right, _ = self._lm_texel(pack, u0 + 4.5 / at['w'], v0 + 2.5 / at['h'])
        self.assertEqual(left[3], 128)
        self.assertEqual(right[3], 255)
        # tile 1: uniformly lit
        t1 = sorted(vs[4:8], key=lambda v: (v[2], v[0]))
        self.assertEqual(self._lm_texel(pack, t1[0][12] + 0.5 / at['w'], t1[0][13] + 0.5 / at['h'])[0][3], 255)
        for v in vs:
            self.assertGreater(v[4], 100)                                    # every quad still faces up

    def test_lightmap_atlas_layout_and_dedupe(self):
        lm_a = self._lm(lambda x, y: (x * 30) % 256, (4, 8, 12))
        g = dict(lightmaps=dict(n=3, cw=8, ch=8, data=lm_a + lm_a + self._lm(lambda x, y: 255)))
        at = B.build_lightmap_atlas(g, [0, 1, 2])
        self.assertEqual(at['slot_of'], {0: 1, 1: 1, 2: 2})                  # identical lightmaps share one slot
        self.assertEqual((at['slots'], at['fmt'], len(at['levels'])), (3, D.FMT_DXT5, 2))
        self.assertEqual(at['w'] % 8, 0); self.assertEqual(at['h'] % 8, 0)
        px = D.decode_dxt('DXT5', at['levels'][0], at['w'], at['h'])
        def texel(slot, x, y):
            col, row = slot % at['cols'], slot // at['cols']
            o = ((row * 8 + y) * at['w'] + col * 8 + x) * 4
            return tuple(px[o:o + 4])
        self.assertEqual(texel(0, 3, 3), (0, 0, 0, 255))                       # lit slot: no colour, full light
        for x in range(8):
            self.assertLessEqual(abs(texel(1, x, 5)[3] - (x * 30) % 256), 7)   # a soft gradient: within half a DXT5 alpha step (8 levels per block)
        edge = B.build_lightmap_atlas(dict(lightmaps=dict(n=1, cw=8, ch=8, data=self._lm(lambda x, y: 128 if x <= 3 else 255))), [0])
        epx = D.decode_dxt('DXT5', edge['levels'][0], edge['w'], edge['h'])
        self.assertEqual([epx[((1 * 0 + 4) * edge['w'] + 8 + x) * 4 + 3] for x in range(8)], [128] * 4 + [255] * 4)    # a hard shadow edge is exact
        self.assertTrue(all(abs(c - e) <= 4 for c, e in zip(texel(1, 2, 2)[:3], (4, 8, 12))))   # colour within one 565 step
        u, v = B.lm_uv(at, 2, 1.0, 7.0)
        self.assertAlmostEqual(u * at['w'], (2 % at['cols']) * 8 + 1.0)
        self.assertAlmostEqual(v * at['h'], (2 // at['cols']) * 8 + 7.0)

    def test_cutout_texture_is_dxt1_punch_through(self):
        rgba = bytes(sum(([200, 100, 50, 0 if (x < 2) else 255] for y in range(8) for x in range(8)), []))
        self.assertTrue(B.binary_alpha(rgba))
        self.assertFalse(B.binary_alpha(rgba[:-1] + b'\x80'))
        enc = B._enc_blocks(rgba, 8, 8, False, True)
        self.assertEqual(len(enc), 4 * 8)                                     # 8 bytes per 4x4 block (DXT5 would be 16)
        dec = D.decode_dxt('DXT1', enc, 8, 8)
        for y in range(8):
            for x in range(8):
                o = (y * 8 + x) * 4
                if x < 2:
                    self.assertEqual(dec[o + 3], 0)
                else:
                    self.assertEqual(dec[o + 3], 255)
                    self.assertTrue(all(abs(dec[o + k] - (200, 100, 50)[k]) <= 8 for k in range(3)))

    def test_rsm_smoothing_groups_and_flat(self):
        node = cube_node('main', '', 5.0)
        quads = [node['faces'][i:i + 2] for i in range(0, 12, 2)]
        def build(shade, groups):
            n = dict(node); n['faces'] = [f + (groups[qi],) for qi, q in enumerate(quads) for f in q]
            m = rofmt.rsm(rsm_bytes(['t.bmp'], [n], 'main', shade=shade))
            return m, B.model_parts(m)
        m, parts = build(2, [0] * 6)
        self.assertEqual(m['shade'], 2)
        self.assertEqual(m['nodes'][0]['faces'][0][8], 0)
        one = len(parts[0][0])
        m, parts = build(2, [1, 2, 3, 4, 5, 6])
        self.assertEqual([f[8] for f in m['nodes'][0]['faces'][::2]], [1, 2, 3, 4, 5, 6])
        self.assertEqual(len(parts[0][0]), 24)                               # one smoothing group per side: 4 own vertices each
        self.assertLessEqual(one, 24)
        m, parts = build(1, [0] * 6)
        self.assertEqual(len(parts[0][0]), 36)                               # flat: 3 own vertices per triangle (face normals)
        self.assertEqual(len(parts[0][1]), 12)

class BuildTest(unittest.TestCase):
    def test_full_pack(self):
        with tempfile.TemporaryDirectory() as d:
            make_map(d)
            v = grf.Vfs(d)
            pack, man = B.build(v, 'testmap', log=lambda *_: None)
        p, a, terr, texs, mats, chunks = validate_like_loader(pack)
        self.assertEqual((a['grid_w'], a['grid_h'], a['cell_size']), (8, 8, 1.0))
        self.assertEqual(a['zone_id'], B.ZONE_ID)
        cells = p['sections'][b'CELL']['data']
        self.assertEqual(cells[0], 0)                         # type 1
        self.assertEqual(cells[63], 0)                        # type 5 (cliff)
        self.assertEqual(sum(cells), 62)
        h = P.dec_hght(p['sections'][b'HGHT']['data'])
        self.assertEqual(h[5 * 8 + 5], 200)                   # raised tile: 10 units up = 2 m = 200 cm
        self.assertEqual(h[0], 0)
        # terrain: 16 tops + 1 wall (both sides), Unity Y up
        b = terr[0]
        self.assertEqual(len(b['tris']), 16 * 2 + 4)
        ys = sorted(set(round(v[1], 3) for v in b['verts']))
        self.assertEqual(ys, [0.0, 2.0])
        xs = [v[0] for v in b['verts']]
        self.assertAlmostEqual(max(xs), 8.0)                  # 4 tiles x 2 m
        # first top tile: clockwise from above -> normal +Y
        v0 = b['verts'][0]
        self.assertGreater(v0[4], 100)
        # vertex colour = the tile colour (255), the lightmap (shadow 200, colour 10 20 30) is the TLMP atlas on uv1
        self.assertTrue(all(v[6:9] == (255, 255, 255) for v in b['verts']))
        tx_, at = LookTest._lm_texel(pack, b['verts'][0][12], b['verts'][0][13])
        self.assertEqual(tx_[3], 200)
        self.assertTrue(all(abs(c - e) <= 4 for c, e in zip(tx_[:3], (10, 20, 30))))
        # model box: one chunk at RSW (-5, -15) from the map centre (20, 20) -> (15, 5) units = (3 m, 1 m), bottom on the ground, top 2 m up
        self.assertEqual(len(chunks), 1)
        cv = chunks[0]['verts']
        self.assertAlmostEqual(min(v[1] for v in cv), 0.0, places=4)
        self.assertAlmostEqual(max(v[1] for v in cv), 2.0, places=4)
        self.assertAlmostEqual((min(v[0] for v in cv) + max(v[0] for v in cv)) / 2, 3.0, places=4)
        self.assertAlmostEqual((min(v[2] for v in cv) + max(v[2] for v in cv)) / 2, 1.0, places=4)
        self.assertEqual(man['model_z'], 'a')                 # stored z puts 2 of 2 instances on the ground (missing.rsm sits on the raised tile)
        self.assertEqual(len(texs), 1)
        self.assertEqual(texs[0]['fmt'], 1)                   # magenta (1-bit alpha) -> DXT1 punch-through cut-out
        self.assertEqual(man['texture_formats']['dxt1_cutout'], 1)
        dec = D.decode_dxt('DXT1', texs[0]['levels'][0], texs[0]['w'], texs[0]['h'])
        self.assertEqual(dec[3], 0)                           # texel (0, 0) is magenta -> transparent
        self.assertEqual(dec[(0 * 8 + 4) * 4 + 3], 255)
        self.assertTrue(mats[0]['flags'] & P.MF_ALPHATEST and mats[0]['flags'] & P.MF_TWOSIDED)
        self.assertEqual(man['skipped_instances'], 1)         # missing.rsm
        self.assertEqual(man['warnings'].get('model missing'), 1)
        # water 5 units below ground: no plane (nothing under water)
        self.assertNotIn(b'WATR', p['sections'])
        self.assertEqual(a['start_x'], 4.5)

    def test_water_plane_and_options(self):
        with tempfile.TemporaryDirectory() as d:
            make_map(d, models=False, water=-5.0)            # water 1 m above the flat ground
            pack, man = B.build(grf.Vfs(d), 'TESTMAP.rsw', cell_m=2.0, start=(1, 0), log=lambda *_: None)
        p, a, terr, texs, mats, chunks = validate_like_loader(pack)
        self.assertIn(b'WATR', p['sections'])
        wt = P.dec_watr(p['sections'][b'WATR']['data'])
        self.assertAlmostEqual(wt['planes'][0]['y'], 2.0)    # -(-5) units x 0.4 m
        self.assertAlmostEqual(a['water_level'], 2.0)
        self.assertEqual(a['cell_size'], 2.0)
        self.assertEqual(len(chunks), 0)
        self.assertAlmostEqual(a['start_x'], 3.0)            # cell (1, 0) centre at 2 m cells
        self.assertTrue(man['water']['plane'])

    def test_texture_dedupe(self):
        # two models whose textures have different paths but the same image -> one OTEX texture, one material
        with tempfile.TemporaryDirectory() as d:
            files = make_map(d, models=False)
            files['data\\texture\\box2.bmp'] = files['data\\texture\\box.bmp']
            files['data\\model\\box2.rsm'] = rsm_bytes(['box2.bmp'], [cube_node('main', '', 5.0)], 'main')
            files['data\\testmap.rsw'] = rsw_bytes('testmap.gnd', 'testmap.gat', [('box.rsm', (-5.0, 0.0, -15.0), (0, 0, 0), (1, 1, 1)),
                                                                                  ('box2.rsm', (5.0, 0.0, -15.0), (0, 0, 0), (1, 1, 1))])
            grf.write_grf(os.path.join(d, 'data.grf'), files)
            pack, man = B.build(grf.Vfs(d), 'testmap', log=lambda *_: None)
        p, a, terr, texs, mats, chunks = validate_like_loader(pack)
        self.assertEqual(len(texs), 1)
        self.assertEqual(len(mats), 1)
        self.assertEqual(man['texture_formats']['deduped'], 1)
        self.assertEqual(man['instances'], 2)

    def test_missing_map(self):
        with tempfile.TemporaryDirectory() as d:
            make_map(d)
            with self.assertRaises(B.BuildError):
                B.build(grf.Vfs(d), 'prontera', log=lambda *_: None)

    def test_model_z_auto(self):
        # a model stored at +z on a map whose ground is only raised at the mirrored spot -> auto picks "mirrored"
        with tempfile.TemporaryDirectory() as d:
            files = make_map(d, models=False)
            files['data\\testmap.rsw'] = rsw_bytes('testmap.gnd', 'testmap.gat', [('box.rsm', (5.0, -10.0, -5.0), (0, 0, 0), (1, 1, 1))])
            grf.write_grf(os.path.join(d, 'data.grf'), files)
            pack, man = B.build(grf.Vfs(d), 'testmap', log=lambda *_: None)
            self.assertEqual(man['model_z'], 'b')             # (25, 25) is the raised tile (2, 2); stored z would be 15
            pack2, man2 = B.build(grf.Vfs(d), 'testmap', model_z='a', log=lambda *_: None)
            self.assertEqual(man2['model_z'], 'a')

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            make_map(d)
            out = os.path.join(d, 'out', 'ro__testmap.rpg1')
            self.assertEqual(cli(['build', '--grf', d, '--map', 'testmap', '--out', out, '--manifest']), 0)
            with open(out, 'rb') as f:
                validate_like_loader(f.read())
            self.assertTrue(os.path.exists(out + '.json'))
            self.assertEqual(cli(['build', '--grf', d, '--map', 'nope', '--out', out]), 1)
            self.assertEqual(cli(['list', '--grf', d]), 0)


# ------------------------------------------------------------------ w34 rointerior: GAT walk surface (GWLK) + interior shells
def _flat_surf():
    return [((0, 1, 0, 1), (0, 0, 1, 1), 0, 0, (255, 255, 255, 255)), ((0, 1, 0, 1), (0, 0, 1, 1), 1, 0, (255, 255, 255, 255))]


def make_custom(root, W, H, cube_fn, gat_fn):
    """a map without models: cube_fn(tx, ty) -> GND cube (h1..h4, up, front, right); gat_fn(cx, cy) -> ((h1..h4), type). RO units, Y down."""
    lm = bytes([255] * 64) + bytes([0, 0, 0] * 64)
    cubes = [cube_fn(tx, ty) for ty in range(H) for tx in range(W)]
    gnd = gnd_bytes(W, H, [KTEX, 'wall.bmp'], _flat_surf(), cubes, [lm])
    hts = []; types = []
    for cy in range(2 * H):
        for cx in range(2 * W):
            hh, ty_ = gat_fn(cx, cy)
            hts.append(hh); types.append(ty_)
    gat = gat_bytes(2 * W, 2 * H, hts, types)
    files = {'data\\testmap.rsw': rsw_bytes('testmap.gnd', 'testmap.gat', []), 'data\\testmap.gnd': gnd, 'data\\testmap.gat': gat,
             'data\\texture\\' + KTEX: img.write_bmp24(16, 16, solid(16, 16, (40, 160, 40))),
             'data\\texture\\wall.bmp': img.write_bmp24(8, 8, solid(8, 8, (90, 90, 90)))}
    grf.write_grf(os.path.join(root, 'data.grf'), files)


def _gwlk(pack):
    S = P.read_pack(pack)['sections']
    return B.dec_gwlk(S[B.TAG_GWLK]['data']), S


class WalkSurfaceTest(unittest.TestCase):
    """the GAT carries bridges / stairs (RSM models without colliders): walkable cells above the GND get the GAT surface as colliders (GsvrRoWalk)."""

    def _bridge(self, d):
        # 6 x 6 tiles flat ground at 0; GAT: a bridge along x over cells 4..7 at y = 5, 2 m up (-10 units), ramps on cells 3 and 8; y = 4 / 6 next to it blocked
        def cube(tx, ty):
            return (0.0, 0.0, 0.0, 0.0, 0, -1, -1)

        def gat(cx, cy):
            if cy in (4, 6) and 3 <= cx <= 8:
                return (0.0, 0.0, 0.0, 0.0), 1
            if cy == 5 and 4 <= cx <= 7:
                return (-10.0, -10.0, -10.0, -10.0), 0
            if cy == 5 and cx == 3:
                return (0.0, -10.0, 0.0, -10.0), 0          # ramp up toward +x (h2 / h4 = the +x corners)
            if cy == 5 and cx == 8:
                return (-10.0, 0.0, -10.0, 0.0), 0
            return (0.0, 0.0, 0.0, 0.0), 0
        make_custom(d, 6, 6, cube, gat)

    def test_bridge_surface_follows_the_gat(self):
        with tempfile.TemporaryDirectory() as d:
            self._bridge(d)
            pack, man = B.build(grf.Vfs(d), 'testmap', log=lambda *_: None)
        validate_like_loader(pack)
        ws, S = _gwlk(pack)
        self.assertEqual(ws['version'], B.GWLK_VERSION)
        self.assertEqual(ws['flags'] & B.GW_INTERIOR, 0)                  # an outdoor map: no void
        self.assertFalse(man['interior'])
        self.assertEqual(man['walk']['raised_cells'], 6)                  # ramp cells 3 + 8 (corners 2 m up) and the 4 bridge cells
        listed = sorted((r[0], r[1]) for r in ws['raised'])
        self.assertEqual(listed, [(3, 5), (4, 5), (5, 5), (6, 5), (7, 5), (8, 5)])        # GAT >= GND + 0.5 m at the centre (the ramp cells are 1 m up there)
        for cx in (4, 5, 6, 7):
            self.assertAlmostEqual(B.walk_height(ws, 1.0, cx + 0.5, 5.5), 2.0, places=3)
        self.assertAlmostEqual(B.walk_height(ws, 1.0, 3.5, 5.5), 1.0, places=3)          # the ramp: the GAT corners, welded with the bridge
        self.assertAlmostEqual(B.walk_height(ws, 1.0, 3.05, 5.5), 0.1, places=2)
        self.assertAlmostEqual(B.walk_height(ws, 1.0, 2.5, 5.5), 0.0, places=3)          # the walkable ring at ground height (flush start)
        self.assertIsNone(B.walk_height(ws, 1.0, 0.5, 0.5))                              # plain ground: GND colliders only
        h = P.dec_hght(S[b'HGHT']['data'])
        self.assertEqual(h[5 * 12 + 5], 200)                                             # HGHT = the GAT corner mean (the walk grid's height)
        # walls toward the blocked cells beside the bridge: 1.5 m up, both windings
        walls = [t for v, tr in ws['pieces'] for t in tr if len({(v[i][0], v[i][1]) for i in t}) == 2]
        self.assertGreater(len(walls), 0)
        tops = {v[i][2] for v, tr in ws['pieces'] for t in tr if len({(v[i][0], v[i][1]) for i in t}) == 2 for i in t}
        self.assertIn(200 + 150, tops)
        self.assertEqual(ws['wall_cm'], 150)

    def test_listed_raised_only_with_gnd_below(self):
        with tempfile.TemporaryDirectory() as d:
            make_map(d, models=False)
            pack, man = B.build(grf.Vfs(d), 'testmap', log=lambda *_: None)
        ws, _S = _gwlk(pack)
        self.assertEqual(ws['raised'], [])                                # GAT == GND everywhere (the raised tile is GND too)
        self.assertEqual(ws['pieces'], [])
        self.assertEqual(man['walk']['cells'], 0)

    def test_walkable_over_the_void(self):
        # a GND tile without top whose GAT cells are walkable (RO door corridors reach into the void): only the GAT holds the player there
        def cube(tx, ty):
            return (0.0, 0.0, 0.0, 0.0, -1 if (tx, ty) == (1, 1) else 0, -1, -1)
        with tempfile.TemporaryDirectory() as d:
            make_custom(d, 3, 3, cube, lambda cx, cy: ((0.0, 0.0, 0.0, 0.0), 0))
            pack, man = B.build(grf.Vfs(d), 'testmap', log=lambda *_: None)
        ws, _S = _gwlk(pack)
        self.assertEqual(man['walk']['raised_cells'], 4)
        self.assertAlmostEqual(B.walk_height(ws, 1.0, 2.5, 2.5), 0.0, places=3)


class InteriorTest(unittest.TestCase):
    """RO interiors are rooms over a void with walls for the top-down camera only: black shells close them, door passages stay walkable (black curtain, no collider)."""

    def _room(self, d, door=True, ring=False):
        # 8 x 8 tiles, room = tiles (3..4, 3..4) at height 0; optional ring of 4 m wall-top tiles around it WITHOUT side surfaces (RO's open south / west walls)
        def cube(tx, ty):
            if 3 <= tx <= 4 and 3 <= ty <= 4:
                return (0.0, 0.0, 0.0, 0.0, 0, -1, -1)
            if ring and 2 <= tx <= 5 and 2 <= ty <= 5:
                return (-20.0, -20.0, -20.0, -20.0, 1, -1, -1)
            return (0.0, 0.0, 0.0, 0.0, -1, -1, -1)

        def gat(cx, cy):
            if 6 <= cx <= 9 and 6 <= cy <= 9:
                return (0.0, 0.0, 0.0, 0.0), 0
            if door and cy == 7 and cx in (10, 11):
                return (0.0, 0.0, 0.0, 0.0), 0              # the door corridor east of the room, over the void
            return (0.0, 0.0, 0.0, 0.0), 1
        make_custom(d, 8, 8, cube, gat)

    def test_room_over_the_void_gets_shells_and_a_door(self):
        with tempfile.TemporaryDirectory() as d:
            self._room(d)
            pack, man = B.build(grf.Vfs(d), 'testmap', log=lambda *_: None)
        p, a, terr, texs, mats, chunks = validate_like_loader(pack)
        self.assertTrue(man['interior'])
        ws, _S = _gwlk(pack)
        self.assertEqual(ws['flags'] & B.GW_INTERIOR, B.GW_INTERIOR)
        # 2 x 2 tile room: 8 tile edges toward the void = 16 cell edges; the door row (cell y 7 -> x 10) is one of them
        self.assertEqual(man['shell_walls'], 15)
        self.assertEqual(man['door_curtains'], 1)
        black = [v for b in terr for v in b['verts'] if v[6:9] == (0, 0, 0)]
        self.assertEqual(len(black), 15 * 2 * 4)                           # double sided quads
        self.assertAlmostEqual(min(v[1] for v in black), 0.0, places=4)
        self.assertAlmostEqual(max(v[1] for v in black), B.SHELL_M, places=4)
        xs = sorted({round(v[0], 3) for v in black}); zs = sorted({round(v[2], 3) for v in black})
        self.assertEqual((xs[0], xs[-1], zs[0], zs[-1]), (5.0, 11.0, 5.0, 11.0))     # 1 m outside the 6..10 m room, stretched to meet at the corners
        # black colour = vertex colour 0 with lightmap slot 0 (no colour) whatever the atlas texel
        self.assertTrue(all(abs(v[12] - black[0][12]) < 1e-9 for v in black))
        # the door: one black two sided object chunk quad (no collider in an RO pack), on the room's east side at the door row
        dm = [i for i, m in enumerate(mats) if texs[m['tex']]['name'] == 'ro_shell_black']
        self.assertEqual(len(dm), 1)
        self.assertTrue(mats[dm[0]]['flags'] & P.MF_TWOSIDED)
        dc = [c for c in chunks if c['mat'] == dm[0]]
        self.assertEqual(len(dc), 1)
        dv = dc[0]['verts']
        self.assertEqual({round(v[0], 3) for v in dv}, {11.0})
        self.assertEqual(sorted({round(v[2], 3) for v in dv}), [7.0, 8.0])
        dec = D.decode_dxt('DXT1', texs[mats[dm[0]]['tex']]['levels'][0], 4, 4)
        self.assertEqual(tuple(dec[0:4]), (0, 0, 0, 255))
        # the door corridor over the void is walk surface (GAT), so the player reaches the warp there
        self.assertAlmostEqual(B.walk_height(ws, 1.0, 10.5, 7.5), 0.0, places=3)

    def test_open_wall_to_a_higher_top_is_closed(self):
        with tempfile.TemporaryDirectory() as d:
            self._room(d, door=False, ring=True)
            pack, man = B.build(grf.Vfs(d), 'testmap', log=lambda *_: None)
        p, a, terr, texs, mats, chunks = validate_like_loader(pack)
        self.assertTrue(man['interior'])
        black = [v for b in terr for v in b['verts'] if v[6:9] == (0, 0, 0)]
        # 16 cell edges room -> ring (4 m higher, no side surface) are gap fills from 0 to 4 m, 0.5 m inside the ring; the ring's outer edges toward the void get 6 m shells
        gap = [v for v in black if 5.4 < v[0] < 5.6 or 10.4 < v[0] < 10.6 or 5.4 < v[2] < 5.6 or 10.4 < v[2] < 10.6]
        self.assertGreater(len(gap), 0)
        self.assertAlmostEqual(max(v[1] for v in gap), 4.0, places=4)
        self.assertAlmostEqual(min(v[1] for v in gap), 0.0, places=4)
        self.assertEqual(man['door_curtains'], 0)

    def test_outdoor_map_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            make_map(d, models=False)
            pack, man = B.build(grf.Vfs(d), 'testmap', log=lambda *_: None)
            pack2, man2 = B.build(grf.Vfs(d), 'testmap', interior=True, log=lambda *_: None)
        self.assertFalse(man['interior'])
        self.assertEqual(man['shell_walls'], 0)
        self.assertTrue(man2['interior'])                                  # forced: the map border (outside = void) and the raised tile's open sides get shells
        self.assertGreater(man2['shell_walls'], 0)
        self.assertEqual(B.interior_of({'w': 2, 'h': 1, 'cubes': [(0, 0, 0, 0, -1, -1, -1), (0, 0, 0, 0, 0, -1, -1)], 'surfaces': [None]}), (True, 0.5))   # >= INTERIOR_VOID


# ------------------------------------------------------------------ the owner's converted packs (Mac only, never game data in git): bridges / stairs walk at the GAT height
REAL = os.environ.get('GSVR_RO_REAL_DIR', '/path/to/gsvr-testdata/ro_out/lmv3')


def _terr_tops(terr, cell):
    """GND height lookup: up-facing TERR triangles (tops; walls / shells are vertical) indexed by the cell of their centroid."""
    idx = {}
    for b in terr:
        v = b['verts']
        for t in b['tris']:
            a_, b_, c_ = v[t[0]], v[t[1]], v[t[2]]
            ux, uy, uz = b_[0] - a_[0], b_[1] - a_[1], b_[2] - a_[2]
            wx, wy, wz = c_[0] - a_[0], c_[1] - a_[1], c_[2] - a_[2]
            ny = uz * wx - ux * wz
            nl = math.sqrt((uy * wz - uz * wy) ** 2 + ny ** 2 + (ux * wy - uy * wx) ** 2) or 1.0
            if abs(ny) / nl < 0.5:
                continue
            for k in range(int(min(a_[0], b_[0], c_[0]) // cell), int(max(a_[0], b_[0], c_[0]) // cell) + 1):
                for m in range(int(min(a_[2], b_[2], c_[2]) // cell), int(max(a_[2], b_[2], c_[2]) // cell) + 1):
                    idx.setdefault((k, m), []).append((a_[:3], b_[:3], c_[:3]))
    return idx


def _tri_y(tris, x, z):
    best = None
    for a_, b_, c_ in tris:
        den = (b_[2] - c_[2]) * (a_[0] - c_[0]) + (c_[0] - b_[0]) * (a_[2] - c_[2])
        if abs(den) < 1e-12:
            continue
        l1 = ((b_[2] - c_[2]) * (x - c_[0]) + (c_[0] - b_[0]) * (z - c_[2])) / den
        l2 = ((c_[2] - a_[2]) * (x - c_[0]) + (a_[0] - c_[0]) * (z - c_[2])) / den
        l3 = 1 - l1 - l2
        if min(l1, l2, l3) < -1e-6:
            continue
        y = l1 * a_[1] + l2 * b_[1] + l3 * c_[1]
        best = y if best is None or y > best else best
    return best


@unittest.skipUnless(all(os.path.isfile(os.path.join(REAL, 'ro__%s.pack' % m)) for m in ('prontera', 'prt_in', 'izlude')), 'no converted iRO packs in ' + REAL)
class RealWalkTest(unittest.TestCase):
    """On the real prontera / prt_in / izlude packs: every walkable cell whose GAT (HGHT) lies >= 0.5 m above the GND ground (found here from the TERR tops, not from the
    converter's list) is covered by the walk surface at the GAT height: exactly (5 cm) where the GAT is continuous around the cell (bridge decks, stair tops, smooth
    ramps; 10 cm where no cell sharing a corner differs by more than 5 cm); next to a GAT step between two walkable cells (prt_in: 0.8 - 1.2 m platforms that RO's
    characters simply pop onto) the welded surface is a ramp across the step (corner = mean of the up to 4 cells sharing it), within the step of the cell's GAT
    height, so a VR player can walk up at all."""

    def _check(self, name):
        with open(os.path.join(REAL, 'ro__%s.pack' % name), 'rb') as f:
            pack = f.read()
        S = P.read_pack(pack)['sections']
        if B.TAG_GWLK not in S:
            self.skipTest('%s pack is older than ro_conv 4 (no GWLK)' % name)
        a = P.dec_area(S[b'AREA']['data']); gw, gh, cell = a['grid_w'], a['grid_h'], a['cell_size']
        cells = S[b'CELL']['data']; h = P.dec_hght(S[b'HGHT']['data'])
        tops = _terr_tops(P.dec_terr(S[b'TERR']['data']), cell)
        ws = B.dec_gwlk(S[B.TAG_GWLK]['data'])
        widx = {}
        for verts, tris in ws['pieces']:
            for t in tris:
                pts = [(verts[i][0] * cell, verts[i][2] / 100.0, verts[i][1] * cell) for i in t]
                if len({(q[0], q[2]) for q in pts}) < 3:
                    continue
                widx.setdefault((int(min(q[0] for q in pts) // cell), int(min(q[2] for q in pts) // cell)), []).append(pts)
        found = smooth = bad = 0
        worst = worst_s = 0.0
        hu = a['height_unit']
        for cy in range(gh):
            for cx in range(gw):
                i = cy * gw + cx
                if not cells[i]:
                    continue
                x, z = (cx + 0.5) * cell, (cy + 0.5) * cell
                gy = h[i] * hu
                g = _tri_y(tops.get((cx, cy), []), x, z)
                if g is None or gy - g < 0.5:
                    continue
                found += 1
                step = 0.0
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):          # every cell that shares a welded corner
                    nx, ny = cx + dx, cy + dy
                    if 0 <= nx < gw and 0 <= ny < gh and cells[ny * gw + nx]:
                        step = max(step, abs(h[ny * gw + nx] * hu - gy))
                wy = _tri_y(widx.get((cx, cy), []), x, z)
                if step <= 0.05:
                    smooth += 1
                lim = 0.05 + step
                if wy is None or abs(wy - gy) > lim:
                    bad += 1
                    if bad <= 5:
                        print('    %s cell (%d,%d): GAT %.2f m, walk surface %s, step to a neighbour %.2f m' % (name, cx, cy, gy, 'none' if wy is None else '%.2f m' % wy, step))
                else:
                    worst = max(worst, abs(wy - gy))
                    if step <= 0.05:
                        worst_s = max(worst_s, abs(wy - gy))
        print('  %s: %d walkable cells with the GAT >= 0.5 m above the GND (%d with a continuous GAT around them: worst %.3f m), %d off the walk surface, worst diff %.3f m'
              % (name, found, smooth, worst_s, bad, worst))
        self.assertGreater(found, 0)
        self.assertEqual(bad, 0)
        self.assertLessEqual(worst_s, 0.10)

    def test_prontera(self):
        self._check('prontera')

    def test_prt_in(self):
        self._check('prt_in')

    def test_izlude(self):
        self._check('izlude')


if __name__ == '__main__':
    unittest.main()
