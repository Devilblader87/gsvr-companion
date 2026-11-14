"""Independent ROSE base-VFS reader (spec: ROSE_SPEC.md section 1). Read-only."""
import struct, os

def norm(p):
    return p.replace('\\', '/').replace('//', '/').upper().strip()

class Vfs:
    def __init__(self, root):
        self.root = root
        d = open(os.path.join(root, 'data.idx'), 'rb').read()
        self.base, self.cur, n = struct.unpack_from('<III', d, 0)
        pos = 12
        self.stores = []      # (name, nfiles, sub_hdr)
        self.files = {}       # normpath -> (storename, off, size)
        self.counts = {}
        self.rawnames = {}
        for _ in range(n):
            ln, = struct.unpack_from('<H', d, pos); pos += 2
            name = d[pos:pos+ln-1].decode('cp949'); pos += ln
            sub, = struct.unpack_from('<I', d, pos); pos += 4
            nf, a, b = struct.unpack_from('<III', d, sub)
            self.stores.append((name, nf, a, b, sub))
            if name.upper() == 'ROOT.VFS':
                continue
            p = sub + 12
            cnt = 0
            for _ in range(nf):
                l, = struct.unpack_from('<H', d, p); p += 2
                fn = d[p:p+l-1].decode('cp949'); p += l
                off, size, blk = struct.unpack_from('<III', d, p); p += 12
                dele, comp, enc = d[p], d[p+1], d[p+2]; p += 3
                ver, crc = struct.unpack_from('<II', d, p); p += 8
                if dele == 0:
                    k = norm(fn)
                    if k not in self.files:
                        self.files[k] = (name, off, size, comp, enc)
                    cnt += 1
            self.counts[name] = cnt
        self.dir = {}
    def read(self, path):
        name, off, size, *_ = self.files[norm(path)]
        with open(os.path.join(self.root, name), 'rb') as f:
            f.seek(off); return f.read(size)
