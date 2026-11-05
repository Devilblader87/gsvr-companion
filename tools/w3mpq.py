#!/usr/bin/env python3
"""MPQ v1 reader (Warcraft III Classic / Frozen Throne era), Python stdlib only.

Clean-room implementation from the publicly documented MPQ format (header,
hash/block tables, Storm crypt table, sector layout).  No Blizzard code.

Supported:
  * header search at 512-byte boundaries (W3M/W3X maps carry a 512-byte
    'HM3W' preamble), 'MPQ\\x1B' user-data shunt
  * format v0/v1 headers (v1 high bits read, but only <4 GB archives expected)
  * encrypted hash/block tables, file keys incl. FIX_KEY (BLOCK_OFFSET_ADJUSTED)
  * single-unit and sectored files, encrypted sectors / sector tables
  * compressions: zlib (0x02), bzip2 (0x10), PKWARE DCL implode (0x08 mask or
    the IMPLODE block flag; binary-literal mode), Huffman (0x01) and ADPCM
    (0x40 mono / 0x80 stereo) via tools/mpqsound.py (the sound-only
    compressions of .wav files: 0x41 / 0x81 in every 1.2x archive and map).
    The Huffman weight tables are read from the install's Storm.dll
    (passed down as `tables`: Storm.dll of the install, or the exes of 1.31 / env W3_STORM_DLL); without one those sectors
    raise Unsupported.
  * (listfile) inside the archive + a built-in list of well-known map names
  * lenient parsing for "protected" maps (bogus archive size, oversized
    tables, out-of-range entries are clipped/ignored)

CLI:
  mpq.py list    <archive> [--listfile F]
  mpq.py extract <archive> <name> [-o out]
  mpq.py extract-all <archive> <outdir>
  mpq.py chain   <file-name> <archive1> [archive2 ...]   (first wins = highest precedence)
"""
import bz2
import os
import struct
import sys
import zlib

import mpqsound

# --------------------------------------------------------------------------
# Storm crypt table
# --------------------------------------------------------------------------

def _build_crypt_table():
    table = [0] * 0x500
    seed = 0x00100001
    for i in range(256):
        idx = i
        for _ in range(5):
            seed = (seed * 125 + 3) % 0x2AAAAB
            t1 = (seed & 0xFFFF) << 16
            seed = (seed * 125 + 3) % 0x2AAAAB
            t2 = seed & 0xFFFF
            table[idx] = t1 | t2
            idx += 256
    return table


CRYPT = _build_crypt_table()
M32 = 0xFFFFFFFF

HASH_OFFSET, HASH_A, HASH_B, HASH_KEY = 0, 1, 2, 3


def hash_string(s, htype):
    seed1 = 0x7FED7FED
    seed2 = 0xEEEEEEEE
    base = htype << 8
    for ch in s.upper().replace('/', '\\').encode('latin-1', 'replace'):
        seed1 = (CRYPT[base + ch] ^ ((seed1 + seed2) & M32)) & M32
        seed2 = (ch + seed1 + seed2 + (seed2 << 5) + 3) & M32
    return seed1


def decrypt_u32s(data, key):
    """Decrypt bytes (length multiple of 4 is processed; tail left as is)."""
    n = len(data) // 4
    if n == 0:
        return bytes(data)
    vals = struct.unpack_from('<%dI' % n, data)
    out = [0] * n
    seed = 0xEEEEEEEE
    for i in range(n):
        seed = (seed + CRYPT[0x400 + (key & 0xFF)]) & M32
        ch = vals[i] ^ ((key + seed) & M32)
        out[i] = ch
        key = ((((~key) << 0x15) + 0x11111111) & M32) | (key >> 0x0B)
        seed = (ch + seed + (seed << 5) + 3) & M32
    return struct.pack('<%dI' % n, *out) + bytes(data[n * 4:])


HASH_TABLE_KEY = hash_string('(hash table)', HASH_KEY)
BLOCK_TABLE_KEY = hash_string('(block table)', HASH_KEY)

# block flags
F_IMPLODE = 0x00000100
F_COMPRESS = 0x00000200
F_ENCRYPTED = 0x00010000
F_FIX_KEY = 0x00020000
F_SINGLE_UNIT = 0x01000000
F_DELETE = 0x02000000
F_SECTOR_CRC = 0x04000000
F_EXISTS = 0x80000000


class Unsupported(Exception):
    pass


# --------------------------------------------------------------------------
# PKWARE DCL "explode" (decoder for the implode format)
# Canonical-Huffman tables expressed as (count-1)<<4 | bitlength run bytes,
# as published in the format description (e.g. zlib contrib/blast notes).
# --------------------------------------------------------------------------

_LENLEN = [2, 35, 36, 53, 38, 23]
_DISTLEN = [2, 20, 53, 230, 247, 151, 248]
_LEN_BASE = [3, 2, 4, 5, 6, 7, 8, 9, 10, 12, 16, 24, 40, 72, 136, 264]
_LEN_EXTRA = [0, 0, 0, 0, 0, 0, 0, 0, 1, 2, 3, 4, 5, 6, 7, 8]


def _construct(rep):
    lengths = []
    for b in rep:
        lengths += [b & 15] * ((b >> 4) + 1)
    maxbits = max(lengths)
    count = [0] * (maxbits + 1)
    for l in lengths:
        count[l] += 1
    # symbols in canonical order: by length, then symbol index
    symbols = [s for l in range(1, maxbits + 1) for s, ll in enumerate(lengths) if ll == l]
    return count, symbols


_LENCODE = _construct(_LENLEN)
_DISTCODE = _construct(_DISTLEN)


class _BitReader:
    __slots__ = ('data', 'pos', 'buf', 'cnt')

    def __init__(self, data, pos=0):
        self.data = data
        self.pos = pos
        self.buf = 0
        self.cnt = 0

    def bits(self, n):
        while self.cnt < n:
            if self.pos >= len(self.data):
                raise EOFError
            self.buf |= self.data[self.pos] << self.cnt
            self.pos += 1
            self.cnt += 8
        v = self.buf & ((1 << n) - 1)
        self.buf >>= n
        self.cnt -= n
        return v

    def decode(self, table):
        count, symbols = table
        code = first = index = 0
        for l in range(1, len(count)):
            code |= self.bits(1) ^ 1          # codes are stored inverted
            c = count[l]
            if code - first < c:
                return symbols[index + code - first]
            index += c
            first += c
            first <<= 1
            code <<= 1
        raise ValueError('explode: bad code')


def explode(data, out_size=None):
    if len(data) < 2:
        raise ValueError('explode: short input')
    lit_mode, dict_bits = data[0], data[1]
    if lit_mode == 1:
        raise Unsupported('PKWARE DCL ASCII-literal mode not implemented')
    if lit_mode != 0 or not 4 <= dict_bits <= 6:
        raise ValueError('explode: bad header %d/%d' % (lit_mode, dict_bits))
    br = _BitReader(data, 2)
    out = bytearray()
    try:
        while out_size is None or len(out) < out_size:
            if br.bits(1):
                sym = br.decode(_LENCODE)
                length = _LEN_BASE[sym] + br.bits(_LEN_EXTRA[sym])
                if length == 519:
                    break
                sb = 2 if length == 2 else dict_bits
                dist = (br.decode(_DISTCODE) << sb) + br.bits(sb) + 1
                if dist > len(out):
                    raise ValueError('explode: distance too far')
                start = len(out) - dist
                for i in range(length):
                    out.append(out[start + i])
            else:
                out.append(br.bits(8))
    except EOFError:
        pass
    return bytes(out)


# --------------------------------------------------------------------------
# Multi-compression
# --------------------------------------------------------------------------

# Decompression limits for untrusted (fan / downloaded) maps, same numbers as the GSVR companion's harden_mpq()
MAX_CALL_BYTES = 64 << 20        # zlib / bz2 output of one block
MAX_FILE_BYTES = 128 << 20       # declared size of one file (MPQ(limits=True))
MAX_TOTAL_BYTES = 512 << 20      # declared size of all files
MAX_RATIO = 2000                 # declared / stored size of a file above 1 MiB


class Bomb(ValueError):
    """A decompression bomb (limits exceeded)."""


def _bounded(d, data):
    out = d.decompress(data, MAX_CALL_BYTES + 1)
    if len(out) > MAX_CALL_BYTES:
        raise Bomb('decompression bomb (more than %d MB from one block)' % (MAX_CALL_BYTES >> 20))
    return out


def bz2_decompress(data):
    return _bounded(bz2.BZ2Decompressor(), data)


def zlib_decompress(data):
    return _bounded(zlib.decompressobj(), data)


C_HUFFMAN, C_ZLIB, C_IMPLODE, C_BZIP2, C_ADPCM_MONO, C_ADPCM_STEREO = 0x01, 0x02, 0x08, 0x10, 0x40, 0x80


def decompress_multi(data, out_size, tables=()):
    mask = data[0]
    buf = data[1:]
    known = C_HUFFMAN | C_ZLIB | C_IMPLODE | C_BZIP2 | C_ADPCM_MONO | C_ADPCM_STEREO
    if mask & ~known:
        raise Unsupported('compression mask 0x%02x' % mask)
    if mask & C_ADPCM_MONO and mask & C_ADPCM_STEREO:
        raise ValueError('compression mask 0x%02x: mono and stereo ADPCM' % mask)
    # decompression order (reverse of compression order)
    if mask & C_BZIP2:
        buf = bz2_decompress(buf)
    if mask & C_IMPLODE:
        buf = explode(buf, out_size)
    if mask & C_ZLIB:
        buf = zlib_decompress(buf)
    adpcm = mask & (C_ADPCM_MONO | C_ADPCM_STEREO)
    if mask & C_HUFFMAN:
        err = None
        for regime in mpqsound.REGIMES:          # see mpqsound: a wrong tree never passes the exact end / count checks
            try:
                # the ADPCM stream is a little longer than half the PCM (control codes); a bare Huffman sector is out_size
                out = mpqsound.huffman_decode(buf, out_size + 64 if adpcm else out_size, regime, tables)
                if not adpcm and len(out) != out_size:
                    raise ValueError('huffman: %d bytes, the sector holds %d' % (len(out), out_size))
                if adpcm:
                    out = mpqsound.adpcm_decode(out, 1 if mask & C_ADPCM_MONO else 2, out_size)
                return out
            except mpqsound.WeightsMissing as e:
                raise Unsupported(str(e))
            except ValueError as e:
                err = err or e
        raise err
    if adpcm:
        buf = mpqsound.adpcm_decode(buf, 1 if mask & C_ADPCM_MONO else 2, out_size)
    return buf


# --------------------------------------------------------------------------
# Known names (maps + core archives)
# --------------------------------------------------------------------------

MAP_NAMES = [
    '(listfile)', '(attributes)', '(signature)',
    'war3map.j', 'Scripts\\war3map.j', 'war3map.w3e', 'war3map.w3i', 'war3map.wts',
    'war3map.shd', 'war3map.mmp', 'war3map.wpm', 'war3map.doo', 'war3mapUnits.doo',
    'war3map.w3r', 'war3map.w3c', 'war3map.w3s', 'war3map.wct', 'war3map.wtg',
    'war3map.imp', 'war3map.w3u', 'war3map.w3t', 'war3map.w3a', 'war3map.w3b',
    'war3map.w3d', 'war3map.w3q', 'war3map.w3h', 'war3map.wai',
    'war3mapMap.blp', 'war3mapMap.tga', 'war3mapPreview.tga', 'war3mapMisc.txt',
    'war3mapSkin.txt', 'war3mapExtra.txt', 'war3mapPath.tga', 'war3map.mmp',
    'war3campaign.w3u', 'conversation.json',
]
CORE_NAMES = [
    'Scripts\\common.j', 'Scripts\\Blizzard.j', 'Scripts\\common.ai',
    'Units\\UnitData.slk', 'Units\\UnitBalance.slk', 'Units\\UnitWeapons.slk',
    'Units\\UnitAbilities.slk', 'Units\\UnitUI.slk', 'Units\\AbilityData.slk',
    'Units\\ItemData.slk', 'Units\\UpgradeData.slk', 'Units\\DestructableData.slk',
    'Doodads\\Doodads.slk', 'TerrainArt\\Terrain.slk', 'TerrainArt\\CliffTypes.slk',
    'TerrainArt\\Water.slk', 'UI\\TriggerData.txt', 'UI\\WorldEditStrings.txt',
    'Units\\CommandFunc.txt', 'Units\\HumanUnitFunc.txt',
]


# --------------------------------------------------------------------------
# Archive
# --------------------------------------------------------------------------

class MPQ:
    def __init__(self, path, extra_names=(), tables=(), limits=False):
        """path: a file name, or the archive's bytes (maps read out of a 1.31 CASC install)."""
        if isinstance(path, (bytes, bytearray)):
            import io
            self.path = '<memory>'
            self.f = io.BytesIO(path)
            self.size = len(path)
        else:
            self.path = path
            self.f = open(path, 'rb')
            self.size = os.fstat(self.f.fileno()).st_size
        self.warnings = []
        self.limits = limits
        self.tables = tuple(tables)
        self._find_header()
        self._read_tables()
        self.names = {}          # name -> (hash entry) for names we know
        self._add_names(MAP_NAMES + CORE_NAMES + list(extra_names))
        lf = self.read('(listfile)', missing_ok=True)
        self.has_listfile = lf is not None
        if lf:
            names = [n.strip() for n in lf.decode('latin-1').replace(';', '\n').splitlines()]
            self._add_names([n for n in names if n])

    def close(self):
        self.f.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def _read_at(self, off, n):
        self.f.seek(off)
        return self.f.read(n)

    def _find_header(self):
        off = 0
        while off + 32 <= self.size:
            sig = self._read_at(off, 4)
            if sig == b'MPQ\x1b':                      # user data shunt
                _, udsize, hdroff = struct.unpack('<4sII', self._read_at(off, 12))
                off2 = off + hdroff
                if self._read_at(off2, 4) == b'MPQ\x1a':
                    self.user_data_offset = off
                    off = off2
                    break
            if sig == b'MPQ\x1a':
                break
            off += 512
        else:
            raise ValueError('no MPQ header found in %s' % self.path)
        self.base = off
        hdr = self._read_at(off, 44)
        (_, hsize, asize, fver, sshift, htpos, btpos, htsize, btsize) = struct.unpack_from('<4sIIHHIIII', hdr)
        self.header_size, self.archive_size, self.format_version = hsize, asize, fver
        hi_bt = ht_hi = bt_hi = 0
        if fver >= 1 and hsize >= 44:
            hi_bt, ht_hi, bt_hi = struct.unpack_from('<QHH', hdr, 32)
        if fver >= 1 and hsize < 44:
            self.warnings.append('format v%d with short header %d' % (fver, hsize))
        self.sector_size = 512 << (sshift & 0x1F)
        self.ht_pos = htpos | (ht_hi << 32 if fver >= 1 else 0)
        self.bt_pos = btpos | (bt_hi << 32 if fver >= 1 else 0)
        self.ht_size, self.bt_size = htsize, btsize

    def _read_table(self, pos, count, key, label):
        start = self.base + pos
        want = count * 16
        if start >= self.size:
            self.warnings.append('%s outside file' % label)
            return b''
        raw = self._read_at(start, want)
        if len(raw) < want:
            self.warnings.append('%s truncated (%d/%d bytes)' % (label, len(raw), want))
            raw = raw[:len(raw) // 16 * 16]
        return decrypt_u32s(raw, key)

    def _read_tables(self):
        # protected maps sometimes claim absurd table sizes; clip to file
        ht = self._read_table(self.ht_pos, min(self.ht_size, 1 << 20), HASH_TABLE_KEY, 'hash table')
        bt = self._read_table(self.bt_pos, min(self.bt_size, 1 << 20), BLOCK_TABLE_KEY, 'block table')
        self.hash_table = [struct.unpack_from('<IIHHI', ht, i) for i in range(0, len(ht), 16)]
        self.block_table = [struct.unpack_from('<IIII', bt, i) for i in range(0, len(bt), 16)]

    def _add_names(self, names):
        for n in names:
            if n in self.names:
                continue
            e = self._lookup(n)
            if e is not None:
                self.names[n] = e

    def _lookup(self, name, locale=0):
        n = len(self.hash_table)
        if n == 0:
            return None
        a = hash_string(name, HASH_A)
        b = hash_string(name, HASH_B)
        mask = n - 1 if (n & (n - 1)) == 0 else None
        start = hash_string(name, HASH_OFFSET)
        i = (start & mask) if mask is not None else start % n
        found = None
        for _ in range(n):
            ea, eb, loc, plat, bidx = self.hash_table[i]
            if bidx == 0xFFFFFFFF:
                break
            if ea == a and eb == b and bidx != 0xFFFFFFFE and bidx < len(self.block_table):
                if loc == locale:
                    return bidx
                if found is None:
                    found = bidx
            i = (i + 1) % n
        return found

    def block_info(self, bidx):
        pos, csize, fsize, flags = self.block_table[bidx]
        return {'pos': pos, 'csize': csize, 'fsize': fsize, 'flags': flags}

    def listing(self):
        """[(name or None, block index, block tuple)] for every existing block."""
        by_block = {}
        for n, b in self.names.items():
            by_block.setdefault(b, n)
        out = []
        for i, blk in enumerate(self.block_table):
            if blk[3] & F_EXISTS:
                out.append((by_block.get(i), i, blk))
        return out

    def check_limits(self):
        """Raises Bomb when a declared file size or the total is over the limits (once per archive)."""
        if getattr(self, '_limits_ok', False):
            return
        total = 0
        for _n, _b, (_pos, csize, fsize, _fl) in self.listing():
            total += fsize
            if fsize > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                raise Bomb('declared content too large (decompression bomb)')
            if fsize > (1 << 20) and fsize > MAX_RATIO * max(csize, 1):
                raise Bomb('implausible compression ratio (decompression bomb)')
        self._limits_ok = True

    def has(self, name):
        return self._lookup(name) is not None

    def read(self, name, missing_ok=False, limit=None):
        """limit: only the first sectors up to at least `limit` bytes are decoded (a header peek); the result may be
        shorter than the file then."""
        bidx = self._lookup(name)
        if bidx is None:
            if missing_ok:
                return None
            raise KeyError(name)
        return self.read_block(bidx, name, limit)

    def read_block(self, bidx, name=None, limit=None):
        pos, csize, fsize, flags = self.block_table[bidx]
        if not flags & F_EXISTS:
            raise KeyError('block %d deleted' % bidx)
        key = 0
        if flags & F_ENCRYPTED:
            if name is None:
                raise Unsupported('encrypted file without known name')
            key = hash_string(name.replace('/', '\\').split('\\')[-1], HASH_KEY)
            if flags & F_FIX_KEY:
                key = ((key + pos) & M32) ^ fsize
        fpos = self.base + pos
        if fsize == 0:
            return b''
        if self.limits:
            self.check_limits()
            if fsize > (1 << 20) and fsize > MAX_RATIO * max(csize, 1):
                raise Bomb('implausible compression ratio (decompression bomb)')
        if flags & F_SINGLE_UNIT:
            raw = self._read_at(fpos, csize)
            if flags & F_ENCRYPTED:
                raw = decrypt_u32s(raw, key)
            return self._decomp_sector(raw, fsize, flags)
        nsec = (fsize + self.sector_size - 1) // self.sector_size
        compressed = flags & (F_COMPRESS | F_IMPLODE)
        if compressed:
            ntab = nsec + 1 + (1 if flags & F_SECTOR_CRC else 0)
            tab = self._read_at(fpos, ntab * 4)
            if flags & F_ENCRYPTED:
                tab = decrypt_u32s(tab, (key - 1) & M32)
            offs = struct.unpack_from('<%dI' % ntab, tab)
        else:
            offs = [min(i * self.sector_size, fsize) for i in range(nsec + 1)]
        out = bytearray()
        for i in range(nsec):
            a, b = offs[i], offs[i + 1]
            raw = self._read_at(fpos + a, b - a)
            if flags & F_ENCRYPTED:
                raw = decrypt_u32s(raw, (key + i) & M32)
            want = min(self.sector_size, fsize - i * self.sector_size)
            out += self._decomp_sector(raw, want, flags, self.tables) if compressed else raw
            if limit is not None and len(out) >= limit:
                break
        return bytes(out[:fsize])

    @staticmethod
    def _decomp_sector(raw, want, flags, tables=()):
        if len(raw) >= want:          # stored uncompressed
            return raw[:want]
        if flags & F_IMPLODE:
            return explode(raw, want)
        if flags & F_COMPRESS:
            return decompress_multi(raw, want, tables)
        return raw


class MPQChain:
    """Several archives; earlier ones take precedence (e.g. War3Patch first)."""

    def __init__(self, paths, tables=()):
        self.tables = tuple(tables)
        self.archives = [MPQ(p, tables=self.tables) for p in paths]
        # archives without (listfile) (e.g. War3Patch.mpq) learn names from the others
        allnames = set()
        for a in self.archives:
            allnames.update(a.names)
        for a in self.archives:
            a._add_names(sorted(allnames))

    def read(self, name, missing_ok=False, limit=None):
        for a in self.archives:
            d = a.read(name, missing_ok=True, limit=limit)
            if d is not None:
                return d
        if missing_ok:
            return None
        raise KeyError(name)

    def which(self, name):
        for a in self.archives:
            if a.has(name):
                return a.path
        return None


def war3_chain(install_dir):
    storm = os.path.join(install_dir, 'Storm.dll')   # the install's own Huffman weight tables, scoped to this chain
    order = ['War3Patch.mpq', 'War3xlocal.mpq', 'War3x.mpq', 'war3.mpq']
    return MPQChain([os.path.join(install_dir, n) for n in order
                     if os.path.exists(os.path.join(install_dir, n))],
                    tables=[storm] if os.path.isfile(storm) else ())


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _flags_str(fl):
    s = ''
    s += 'I' if fl & F_IMPLODE else '-'
    s += 'C' if fl & F_COMPRESS else '-'
    s += 'E' if fl & F_ENCRYPTED else '-'
    s += 'K' if fl & F_FIX_KEY else '-'
    s += 'S' if fl & F_SINGLE_UNIT else '-'
    return s


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd = argv[0]
    if cmd == 'list':
        extra = []
        if '--listfile' in argv:
            with open(argv[argv.index('--listfile') + 1], encoding='latin-1') as fh:
                extra = [l.strip() for l in fh if l.strip()]
        with MPQ(argv[1], extra) as m:
            print('# base=0x%x fmt=%d sector=%d hash=%d block=%d listfile=%s' % (
                m.base, m.format_version, m.sector_size, len(m.hash_table), len(m.block_table), m.has_listfile))
            for w in m.warnings:
                print('# warning:', w)
            for name, i, (pos, cs, fs, fl) in m.listing():
                print('%5d %s %9d %9d  %s' % (i, _flags_str(fl), cs, fs, name or '<unknown #%d>' % i))
    elif cmd == 'extract':
        with MPQ(argv[1]) as m:
            data = m.read(argv[2])
        out = argv[argv.index('-o') + 1] if '-o' in argv else os.path.basename(argv[2].replace('\\', '/'))
        with open(out, 'wb') as fh:
            fh.write(data)
        print('%s: %d bytes -> %s' % (argv[2], len(data), out))
    elif cmd == 'extract-all':
        outdir = argv[2]
        with MPQ(argv[1]) as m:
            for name, i, blk in m.listing():
                if not name:
                    continue
                try:
                    data = m.read_block(i, name)
                except Unsupported as e:
                    print('skip %s: %s' % (name, e))
                    continue
                p = os.path.join(outdir, *name.split('\\'))
                os.makedirs(os.path.dirname(p) or '.', exist_ok=True)
                with open(p, 'wb') as fh:
                    fh.write(data)
                print('%9d %s' % (len(data), name))
    elif cmd == 'chain':
        ch = MPQChain(argv[2:])
        print(ch.which(argv[1]))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
