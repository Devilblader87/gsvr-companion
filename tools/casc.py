"""Read-only CASC + TVFS reader for a local Warcraft III 1.30/1.31 install (stdlib only).

Public format knowledge [ext]: .build.info → build config (Data/config/xx/yy/<key>); local index files
Data/data/<bucket><version>.idx (v7, 18-byte entries: 9-byte EKey, 5-byte big-endian archive|offset, 4-byte size);
data.NNN blobs (30-byte header, then BLTE: N raw / Z zlib chunks); TVFS directory tree (vfs-root, nested vfs-N
files are themselves TVFS trees) mapping paths → spans of EKeys.

API:  c = CASC('input/war3_131');  c.read('war3.w3mod:units\\human\\footman\\footman.mdx')  →  bytes
      c.names()  → all paths;  c.has(path);  c.build_name (e.g. '1.31.1.12164-retail')
Path matching is case-insensitive and ignores path separators: WC3 1.31's TVFS tree stores many directory/file
boundaries without a separator byte (e.g. `commandbuttonsdisabled` + `disbtnazuredragon.blp`), so keys are the
lower-case path with every '\\' and '/' removed (the ':' between nested mods stays). names() returns the paths as
the tree spells them.
CLI:  python3 tools/casc.py DIR list [prefix] | extract PATH OUT | info
"""
import os, struct, sys, zlib


class CascError(Exception):
    pass


def _key(path):
    return path.replace('\\', '').replace('/', '').lower()


def _be(b):
    return int.from_bytes(b, 'big')


class CASC:
    def __init__(self, root):
        self.root = root
        self.data_dir = os.path.join(root, 'Data', 'data')
        info = self._build_info()
        self.build_key = info['Build Key']
        cfg = self._config(self.build_key)
        self.build_name = cfg.get('build-name', '?')
        self.index = {}
        self._load_indices()
        enc_ekey = cfg['encoding'].split()[1]
        self.encoding = self._load_encoding(self._read_ekey(bytes.fromhex(enc_ekey)))
        self.files = {}               # key (see _key) → list of (ekey9, span size)
        self.paths = {}               # key → path as spelled in the tree
        root_ekey = cfg['vfs-root'].split()[1]
        self._load_tvfs(self._read_ekey(bytes.fromhex(root_ekey)), '')

    # ── config ──
    def _build_info(self):
        p = os.path.join(self.root, '.build.info')
        lines = [l for l in open(p, encoding='utf-8').read().splitlines() if l.strip()]
        head = [h.split('!')[0] for h in lines[0].split('|')]
        for l in lines[1:]:
            row = dict(zip(head, l.split('|')))
            if row.get('Active', '1') == '1':
                return row
        raise CascError('no active build in .build.info')

    def _config(self, key):
        p = os.path.join(self.root, 'Data', 'config', key[:2], key[2:4], key)
        out = {}
        for l in open(p, encoding='utf-8').read().splitlines():
            if '=' in l and not l.startswith('#'):
                k, v = l.split('=', 1)
                out[k.strip()] = v.strip()
        return out

    # ── local storage ──
    def _load_indices(self):
        best = {}
        for fn in os.listdir(self.data_dir):
            if fn.endswith('.idx') and len(fn) == 14:
                bucket, ver = int(fn[:2], 16), int(fn[2:10], 16)
                if bucket not in best or ver > best[bucket][0]:
                    best[bucket] = (ver, fn)
        for bucket, (_v, fn) in sorted(best.items()):
            d = open(os.path.join(self.data_dir, fn), 'rb').read()
            hsize = struct.unpack_from('<I', d, 0)[0]
            ver, _bi, _ex, size_len, off_len, key_len, off_bits = struct.unpack_from('<H6B', d, 8)
            if ver != 7:
                raise CascError('%s: index version %d' % (fn, ver))
            p = (8 + hsize + 15) & ~15
            esize = struct.unpack_from('<I', d, p)[0]
            p += 8
            step = key_len + off_len + size_len
            mask = (1 << off_bits) - 1
            for k in range(esize // step):
                e = p + k * step
                key = d[e:e + key_len]
                pos = _be(d[e + key_len:e + key_len + off_len])
                size = struct.unpack_from('<I', d, e + key_len + off_len)[0]
                if key not in self.index:
                    self.index[key] = (pos >> off_bits, pos & mask, size)

    def _read_ekey(self, ekey):
        loc = self.index.get(ekey[:9])
        if loc is None:
            raise CascError('EKey %s not in local storage' % ekey.hex())
        arc, off, size = loc
        with open(os.path.join(self.data_dir, 'data.%03d' % arc), 'rb') as fh:
            fh.seek(off)
            blob = fh.read(size)
        return self._blte(blob[30:])

    def _peek_ekey(self, ekey, n=4):
        """-> the first n content bytes of a file (or None: not stored locally / not BLTE). Only the BLTE header and
        the first chunk are read, and a zlib chunk is inflated just far enough: _add_file asks this for EVERY file
        of the tree (is it a nested TVFS?), and inflating all ~65000 files cost 6.6 s per conversion on a Mac,
        11.6 s in the frozen Windows build (BUGS L-CASC)."""
        loc = self.index.get(ekey[:9])
        if loc is None:
            return None
        arc, off, size = loc
        with open(os.path.join(self.data_dir, 'data.%03d' % arc), 'rb') as fh:
            fh.seek(off)
            head = fh.read(min(size, 30 + 12))
            b = head[30:]
            if b[:4] != b'BLTE':
                return None
            hsize = _be(b[4:8])
            if hsize == 0:
                p, csize = 8, size - 30 - 8
            else:
                b += fh.read(min(size - len(head), max(0, hsize + 16 - len(b))))
                if len(b) < 20:
                    return None
                p, csize = hsize, _be(b[12:16])
            fh.seek(off + 30 + p)
            c = fh.read(min(csize, size - 30 - p))
        mode = c[:1]
        if mode == b'N':
            return c[1:1 + n]
        if mode == b'Z':
            try:
                return zlib.decompressobj().decompress(c[1:], n)
            except zlib.error:
                return None
        return None                                  # 'F' / 'E' / unknown: the caller reads the whole file

    @staticmethod
    def _blte(b):
        if b[:4] != b'BLTE':
            raise CascError('not BLTE')
        hsize = _be(b[4:8])
        if hsize == 0:
            chunks = [(len(b) - 8, None)]
            p = 8
        else:
            n = _be(b[9:12])
            chunks = [(_be(b[12 + 24 * i:16 + 24 * i]), _be(b[16 + 24 * i:20 + 24 * i])) for i in range(n)]
            p = hsize
        out = bytearray()
        for csize, _dsize in chunks:
            c = b[p:p + csize]
            p += csize
            mode = c[:1]
            if mode == b'N':
                out += c[1:]
            elif mode == b'Z':
                out += zlib.decompress(c[1:])
            elif mode == b'F':
                out += CASC._blte(c[1:])
            elif mode == b'E':
                raise CascError('encrypted BLTE chunk')
            else:
                raise CascError('BLTE chunk mode %r' % mode)
        return bytes(out)

    @staticmethod
    def _load_encoding(d):
        if d[:2] != b'EN':
            raise CascError('not an encoding file')
        ver, ck, ek = d[2], d[3], d[4]
        ce_kb, es_kb = _be(d[5:7]), _be(d[7:9])
        ce_n, es_n = _be(d[9:13]), _be(d[13:17])
        es_size = _be(d[18:22])
        p = 22 + es_size
        p += ce_n * (ck + 16)
        out = {}
        for _ in range(ce_n):
            page = d[p:p + ce_kb * 1024]
            p += ce_kb * 1024
            q = 0
            while q + 6 + ck <= len(page):
                n = page[q]
                if n == 0:
                    break
                ckey = page[q + 6:q + 6 + ck]
                out[ckey] = page[q + 6 + ck:q + 6 + ck + ek]
                q += 6 + ck + n * ek
        return out

    # ── TVFS ──
    def _load_tvfs(self, d, prefix):
        if d[:4] != b'TVFS':
            raise CascError('not TVFS')
        ver, hsize, ek_size, pk_size = d[4], d[5], d[6], d[7]
        flags = _be(d[8:12])
        pt_off, pt_size, vt_off, vt_size, ct_off, ct_size = (_be(d[12 + 4 * i:16 + 4 * i]) for i in range(6))
        cft_entry = ek_size + 4 + (pk_size if flags & 2 else 0)
        off_size = 1 if ct_size <= 0xFF else 2 if ct_size <= 0xFFFF else 3 if ct_size <= 0xFFFFFF else 4
        vt = d[vt_off:vt_off + vt_size]
        ct = d[ct_off:ct_off + ct_size]

        def spans(vo):
            n = vt[vo]
            q = vo + 1
            out = []
            for _ in range(n):
                _cofs, span = _be(vt[q:q + 4]), _be(vt[q + 4:q + 8])
                co = _be(vt[q + 8:q + 8 + off_size])
                q += 8 + off_size
                out.append((ct[co:co + ek_size], span))
            return out

        def walk(p, end, base):
            # base = folder path (ends with a separator or is empty); acc collects name fragments of one entry
            # chain: an entry without node value continues in the next entry; a file / folder value ends the chain
            acc = base
            while p < end:
                if d[p] == 0:                       # separator before the name
                    p += 1
                    if acc and not acc.endswith(('\\', ':')):
                        acc += '\\'
                if p < end and d[p] != 0xFF:
                    n = d[p]
                    acc += d[p + 1:p + 1 + n].decode('utf-8', 'replace')
                    p += 1 + n
                if p < end and d[p] == 0:           # separator after the name
                    p += 1
                    acc += '\\'
                if p < end and d[p] == 0xFF:
                    val = _be(d[p + 1:p + 5])
                    p += 5
                    if val & 0x80000000:
                        sub_end = p - 4 + (val & 0x7FFFFFFF)
                        walk(p, sub_end, acc)
                        p = sub_end
                    else:
                        self._add_file(acc.rstrip('\\'), spans(val))
                    acc = base
        walk(pt_off, pt_off + pt_size, prefix)

    def _add_file(self, path, sp):
        # nested TVFS (vfs-N): a single span whose content is another directory tree → mount it under path + ':'
        if len(sp) == 1 and sp[0][1] < 8 * 1024 * 1024:
            try:
                magic = self._peek_ekey(sp[0][0])
                d = self._read_ekey(sp[0][0]) if magic in (None, b'TVFS') else None
            except CascError:
                d = None
            if d is not None and d[:4] == b'TVFS':
                self._load_tvfs(d, path + ':')
                return
        self.files[_key(path)] = sp
        self.paths[_key(path)] = path

    # ── public ──
    def names(self):
        return sorted(self.paths.values())

    def has(self, path):
        return _key(path) in self.files

    def read(self, path, missing_ok=False):
        sp = self.files.get(_key(path))
        if sp is None:
            if missing_ok:
                return None
            raise KeyError(path)
        return b''.join(self._read_ekey(k) for k, _s in sp)


def main():
    c = CASC(sys.argv[1])
    cmd = sys.argv[2] if len(sys.argv) > 2 else 'info'
    if cmd == 'info':
        print('%s: %d files, %d index entries, %d encoding entries' % (c.build_name, len(c.files), len(c.index), len(c.encoding)))
    elif cmd == 'list':
        pre = sys.argv[3].lower() if len(sys.argv) > 3 else ''
        for n in c.names():
            if n.startswith(pre):
                print(n)
    elif cmd == 'extract':
        open(sys.argv[4], 'wb').write(c.read(sys.argv[3]))


if __name__ == '__main__':
    main()
