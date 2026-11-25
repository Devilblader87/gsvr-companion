"""GRF archive reader + virtual file system over the player's own Ragnarok Online client (BYOG, read only, stdlib only).

Written from the public descriptions of the GRF container (e.g. the format notes in the OpenKore / rAthena / roBrowser
communities); no code copied. Supported: GRF 0x200 and 0x300, zlib entries, encrypted entries (DES / MIXCRYPT, grfcrypt.py).
GRF 0x1xx (old name-obfuscated tables) is reported, not decoded.

Header (46 bytes, little endian), magic "Master of Magic\\0" (0x200) or "Event Horizon" (0x300, current iRO data.grf):
    0x200: char[16] magic, byte[14] key, u32 table_offset (relative to the end of the header), u32 seed,
           u32 count_raw (file count = count_raw - seed - 7), u32 version
    0x300: char[16] magic, byte[14] key, u64 table_offset, u32 file_count (plain), u32 version
File table at 46 + table_offset: [0x300: u32 0 pad] u32 packed_size, u32 real_size, zlib(packed_size) ->
    per entry: CP949 name (NUL terminated), u32 csize, u32 csize_aligned, u32 size, u8 flags, u32 (0x300: u64) offset (relative to 46)
    flags: 1 = file (entries without it are directories, skipped), 2 = MIXCRYPT, 4 = DES on the first 20 blocks.
Entry data: read csize_aligned bytes, decrypt (flags 2 / 4), zlib-inflate the first csize bytes.

Paths: Windows style (backslashes), case-insensitive for ASCII, Korean (CP949) folder names. Every lookup key is normalised by
`norm()`: decoded text, '/' -> '\\', ASCII lower case, no leading '\\'. Keys always start with 'data\\'.
"""
import os
import struct
import zlib

from . import grfcrypt

HEADER = struct.Struct('<16s14sIIII')
MAGIC = b'Master of Magic'
MAGIC_300 = b'Event Horizon'
MAGICS = (MAGIC, MAGIC_300)
EXCLUDE_DEFAULT = ('event.grf',)      # seasonal overlay (Christmas Prontera ...): never the base map unless asked for
F_FILE, F_MIXCRYPT, F_DES14 = 1, 2, 4


class GrfError(Exception):
    pass


def decode_name(raw):
    """GRF / RSW / GND / RSM names are CP949 bytes. Invalid sequences fall back to latin-1 per byte so every name stays unique."""
    if isinstance(raw, str):
        return raw
    raw = raw.split(b'\0', 1)[0]
    try:
        return raw.decode('cp949')
    except UnicodeDecodeError:
        return raw.decode('latin-1')


def norm(path):
    """lookup key: decoded, backslashes, ASCII lower case (Korean has no case), no leading slash, no doubled separators."""
    p = decode_name(path).replace('/', '\\')
    p = ''.join(c.lower() if c < '\x80' else c for c in p)
    while '\\\\' in p:
        p = p.replace('\\\\', '\\')
    return p.lstrip('\\')


class Grf:
    """One GRF file. entries: {norm name: (offset, csize, csize_aligned, size, flags)}; dirs = skipped directory entries."""

    def __init__(self, path):
        self.path = path
        self.entries = {}
        self.encrypted = 0
        self.dirs = 0
        bn = os.path.basename(path)
        with open(path, 'rb') as f:
            hd = f.read(HEADER.size)
            if len(hd) < HEADER.size or not hd.startswith(MAGICS):
                raise GrfError('%s: not a GRF archive' % bn)
            ver = struct.unpack_from('<I', hd, 42)[0]
            self.version = ver
            if ver == 0x200:
                toff, seed, nraw = struct.unpack_from('<III', hd, 30)
                self.count = nraw - seed - 7
                off_fmt = '<I'
            elif ver == 0x300:
                toff, self.count = struct.unpack_from('<QI', hd, 30)
                off_fmt = '<Q'
            else:
                raise GrfError('%s: GRF version 0x%x is not supported (only 0x200 / 0x300)' % (bn, ver))
            f.seek(HEADER.size + toff)
            if ver == 0x300:
                f.read(4)                                 # u32 0 before the table sizes
            th = f.read(8)
            if len(th) < 8:
                raise GrfError('%s: truncated file table' % bn)
            psize, rsize = struct.unpack('<II', th)
            try:
                table = zlib.decompress(f.read(psize))
            except zlib.error as e:
                raise GrfError('%s: file table does not decompress (%s)' % (bn, e))
        if len(table) != rsize:
            raise GrfError('%s: file table size %d != %d' % (bn, len(table), rsize))
        tail = 13 + struct.calcsize(off_fmt)
        fixed = struct.Struct('<IIIB' + off_fmt[1:])
        pos = 0; n = len(table)
        ents = self.entries
        while pos < n:
            end = table.find(b'\0', pos)
            if end < 0 or end + 1 + tail > n:
                break
            name = table[pos:end]
            cs, csa, sz, fl, off = fixed.unpack_from(table, end + 1)
            pos = end + 1 + tail
            if not fl & F_FILE:
                self.dirs += 1
                continue
            if fl & (F_MIXCRYPT | F_DES14):
                self.encrypted += 1
            ents[norm(name)] = (off, cs, csa, sz, fl)

    def read(self, key):
        off, cs, csa, sz, fl = self.entries[key]
        with open(self.path, 'rb') as f:
            f.seek(HEADER.size + off)
            blob = f.read(max(csa, cs))
        if len(blob) < cs:
            raise GrfError('%s: truncated entry' % key)
        if fl & (F_MIXCRYPT | F_DES14):
            blob = grfcrypt.decode(blob, fl, cs)
        if cs == sz and sz > 0 and blob[:1] != b'\x78':
            data = blob[:sz]            # stored (some tools write uncompressed entries)
        else:
            try:
                data = zlib.decompress(blob[:cs])
            except zlib.error as e:
                raise GrfError('%s: entry does not decompress (%s)' % (key, e))
        if len(data) != sz:
            raise GrfError('%s: size %d != %d' % (key, len(data), sz))
        return data


def _ini_order(folder):
    """GRF names from DATA.INI ([Data] n=name, lower n = higher priority), [] if there is none."""
    for nm in os.listdir(folder):
        if nm.lower() == 'data.ini':
            out = []
            try:
                with open(os.path.join(folder, nm), 'rb') as f:
                    txt = f.read().decode('latin-1')
            except OSError:
                return []
            sec = ''
            for line in txt.splitlines():
                line = line.strip()
                if line.startswith('['):
                    sec = line.strip('[]').strip().lower()
                    continue
                if sec == 'data' and '=' in line:
                    k, v = line.split('=', 1)
                    try:
                        out.append((int(k.strip()), v.strip()))
                    except ValueError:
                        pass
            return [v for _k, v in sorted(out)]
    return []


def grf_order(folder, exclude=EXCLUDE_DEFAULT):
    """GRF files of a client folder in priority order (first wins): DATA.INI order if present, else rdata.grf, data.grf, then the
    remaining *.grf alphabetically. `exclude` (lower-case names, default event.grf: the seasonal overlay with its own Christmas
    Prontera) is left out unless DATA.INI lists it."""
    have = {nm.lower(): nm for nm in os.listdir(folder) if nm.lower().endswith('.grf') and os.path.isfile(os.path.join(folder, nm))}
    ini = [nm.lower() for nm in _ini_order(folder)]
    for k in exclude or ():
        if k in have and k not in ini:
            del have[k]
    order = []
    for nm in _ini_order(folder):
        k = nm.lower()
        if k in have and have[k] not in order:
            order.append(have[k])
    for k in ('rdata.grf', 'data.grf'):
        if k in have and have[k] not in order:
            order.append(have[k])
    for k in sorted(have):
        if have[k] not in order:
            order.append(have[k])
    return [os.path.join(folder, nm) for nm in order]


class Vfs:
    """The client's data: loose files under <folder>/data/ override every GRF; GRFs in grf_order() priority.

    src: a client folder (with *.grf and/or data/) or one .grf file; exclude: GRF names grf_order() leaves out (default event.grf)."""

    def __init__(self, src, loose=True, exclude=EXCLUDE_DEFAULT):
        self.grfs = []
        self.loose = {}
        self.errors = []
        if os.path.isfile(src):
            self.grfs.append(Grf(src))
            folder = None
        else:
            folder = src
            for p in grf_order(folder, exclude):
                try:
                    self.grfs.append(Grf(p))
                except GrfError as e:
                    self.errors.append(str(e))
        if folder and loose:
            for nm in os.listdir(folder):
                if nm.lower() == 'data' and os.path.isdir(os.path.join(folder, nm)):
                    base = os.path.join(folder, nm)
                    for root, _dirs, files in os.walk(base):
                        rel = os.path.relpath(root, folder)
                        for fn in files:
                            key = norm(os.path.join(rel, fn).replace(os.sep, '\\'))
                            self.loose[key] = os.path.join(root, fn)
        if not self.grfs and not self.loose:
            raise GrfError('no readable GRF archive or data folder in %s%s' % (src, (': ' + '; '.join(self.errors)) if self.errors else ''))

    def find(self, path):
        """-> ('loose', filepath) | (grf, key) | None"""
        k = norm(path)
        if not k.startswith('data\\'):
            k = 'data\\' + k
        if k in self.loose:
            return ('loose', self.loose[k])
        for g in self.grfs:
            if k in g.entries:
                return (g, k)
        return None

    def exists(self, path):
        return self.find(path) is not None

    def read(self, path):
        hit = self.find(path)
        if hit is None:
            raise KeyError(norm(path))
        src, k = hit
        if src == 'loose':
            with open(k, 'rb') as f:
                return f.read()
        return src.read(k)

    def names(self):
        s = set(self.loose)
        for g in self.grfs:
            s.update(g.entries)
        return s


def write_grf(path, files, version=0x200, seed=0):
    """Test / fixture helper: writes a GRF 0x200 with the given {name (str, CP949 encodable): bytes} (zlib entries)."""
    body = bytearray(); table = bytearray()
    for name, data in files.items():
        comp = zlib.compress(data)
        al = (len(comp) + 7) & ~7
        off = len(body)
        body += comp + b'\0' * (al - len(comp))
        table += name.encode('cp949') + b'\0' + struct.pack('<IIIBI', len(comp), al, len(data), F_FILE, off)
    ct = zlib.compress(bytes(table))
    hd = HEADER.pack(MAGIC + b'\0', b'\0' * 14, len(body), seed, len(files) + seed + 7, version)
    with open(path, 'wb') as f:
        f.write(hd + bytes(body) + struct.pack('<II', len(ct), len(table)) + ct)
