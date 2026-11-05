"""SAVE-2 (docs/save_system.md "Stage 2: game cache persistence"): SaveGameCache / InitGameCache / ReloadGameCachesFromDisk write and
read the JASS game caches from a store that outlives the game: the stand-in of the VRChat PlayerData key `w3s.g` (W3SaveStore.cs).

Semantics follow docs/decomp/classes/Unclassed_Native_0038.md §7 and CGameCacheManager_p1.md §10 (the original keeps ONE file,
Campaigns.w3v, for the whole cache manager):
  * SaveGameCache(c) writes the WHOLE manager: every cache, every mission table, every typed register; it returns true after a
    completed write (here: false when the encoded file would exceed BUDGET - the file on "disk" is then left as it was);
  * InitGameCache(name) finds-or-creates the cache by its packed name (spaces dropped, 255 characters); when the file has a cache of
    that name its data is loaded into the new cache (the original loads the file when the manager starts, which is observably the same);
  * ReloadGameCachesFromDisk() replaces every table with the file's: data not saved is discarded, handles stay valid and point at
    the reloaded records; the script sees false [inf, 0038 §7.3: neither original body returns a value; the MMD library of the fan
    maps (Gem TD, Legion TD) reads that false as "online game", which must not change];
  * FlushGameCache clears the memory only: the file keeps its copy until the next SaveGameCache;
  * StoreUnit / RestoreUnit are not implemented in this port (they snapshot a unit): nothing of a unit persists.
Nothing is saved without a SaveGameCache call; "Campaigns.w3v" (the engine's own campaign cache) is never a map's cache.

File format (own, versioned, little endian; Python tools/jvm/gamecache.py == Udon W3SaveGc.cs, byte for byte):
  "W3GC" | version u8 (1) | flags u8 (0) | varint caches
  per cache:   str name | varint missions
  per mission: str mission key | varint entries
  per entry:   u8 kind (0 integer, 1 real, 2 boolean, 3 string) | str key | value
  integer = zigzag varint of the signed 32-bit value; real = IEEE float32 (4 bytes); boolean = 1 byte; string = str
  str = varint (byte length) + the UTF-8 bytes (a VM string's characters are code points; the engine's 8-bit characters become 2 bytes)
Order: caches as first seen (the file's, then the new ones), missions and entries in creation order (an overwrite keeps its place).
The mission key and key are already packed (spaces dropped) by the natives.
"""
import struct

MAGIC = b'W3GC'
VERSION = 1
BUDGET = 32768              # bytes of the file: PlayerData is ~100 KB per player and world; the codes take <= ~25 KB (savecode caps), the options a few bytes
KINDS = ('Integer', 'Real', 'Boolean', 'String')


def varint(n):
    out = bytearray()
    while True:
        b = n & 0x7f
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def zigzag(v):
    v = ((v + 0x80000000) & 0xFFFFFFFF) - 0x80000000                  # the 32-bit signed value the engine stores
    return ((v << 1) ^ (v >> 31)) & 0xFFFFFFFF


def unzigzag(u):
    v = (u >> 1) ^ -(u & 1)
    return ((v + 0x80000000) & 0xFFFFFFFF) - 0x80000000


def pstr(s):
    raw = (s or '').encode('utf-8', 'replace')
    return varint(len(raw)) + raw


def encode(caches):
    """caches = [(name, {(kind, mission, key): value})] -> the file bytes. kind: 'Integer' / 'Real' / 'Boolean' / 'String'."""
    out = bytearray(MAGIC + bytes((VERSION, 0)) + varint(len(caches)))
    for name, data in caches:
        out += pstr(name)
        missions = []
        for (kind, mk, key) in data:                                   # missions in first-seen order
            if mk not in missions:
                missions.append(mk)
        out += varint(len(missions))
        for mk in missions:
            ents = [(k, v) for k, v in data.items() if k[1] == mk]
            out += pstr(mk) + varint(len(ents))
            for (kind, _mk, key), v in ents:
                ki = KINDS.index(kind)
                out.append(ki)
                out += pstr(key)
                if ki == 0:
                    out += varint(zigzag(int(v)))
                elif ki == 1:
                    out += struct.pack('<f', float(v))
                elif ki == 2:
                    out.append(1 if v else 0)
                else:
                    out += pstr(v)
    return bytes(out)


class _Rd:
    def __init__(self, b):
        self.b, self.i = b, 0

    def byte(self):
        if self.i >= len(self.b):
            raise ValueError('truncated')
        v = self.b[self.i]
        self.i += 1
        return v

    def varint(self):
        n = s = 0
        while True:
            b = self.byte()
            n |= (b & 0x7f) << s
            if not b & 0x80:
                return n
            s += 7
            if s > 35:
                raise ValueError('varint')

    def take(self, n):
        if n < 0 or n > len(self.b) - self.i:
            raise ValueError('truncated')
        v = self.b[self.i:self.i + n]
        self.i += n
        return v

    def str(self):
        return bytes(self.take(self.varint())).decode('utf-8')


def decode(blob):
    """the file bytes -> [(name, {(kind, mission, key): value})], or None when it is not a version-1 file (a bad / newer / cut file
    is treated as no file: the caches start empty and the next SaveGameCache replaces it)."""
    if not blob or len(blob) < 7 or blob[:4] != MAGIC or blob[4] != VERSION:
        return None
    try:
        r = _Rd(blob)
        r.i = 6
        out = []
        for _c in range(r.varint()):
            name = r.str()
            data = {}
            for _m in range(r.varint()):
                mk = r.str()
                for _e in range(r.varint()):
                    ki = r.byte()
                    if ki > 3:
                        return None
                    key = r.str()
                    if ki == 0:
                        v = unzigzag(r.varint())
                    elif ki == 1:
                        v = struct.unpack('<f', bytes(r.take(4)))[0]
                    elif ki == 2:
                        v = r.byte()
                    else:
                        v = r.str()
                    data[(KINDS[ki], mk, key)] = v
            out.append((name, data))
        return out if r.i == len(blob) else None
    except (ValueError, struct.error, UnicodeDecodeError):
        return None


def persistable(name):
    """The MMD / W3MMD libraries (Gem TD, Legion TD ...) keep their transport caches ("MMD.Dat") in a game cache: never persisted."""
    n = (name or '').lower()
    return not (n.startswith('mmd') or n.startswith('w3mmd'))


class GcDisk:
    """The persistent side: the file as the VRChat PlayerData key holds it. `blob` is what W3SaveStore writes (dirty after a save)."""

    def __init__(self, blob=None, budget=BUDGET, map_key=''):
        self.budget = budget
        self.map = map_key              # the running map's identity: its caches are stored as "<map>|<name>" (maps never cross-load)
        self.blob = b''
        self.data = {}                  # name -> {(kind, mission, key): value}, the file's caches in file order
        self.dirty = False
        self.saves = 0                  # completed SaveGameCache writes
        self.refused = 0                # writes refused for size
        self.last_bytes = 0             # size of the last encoded file (accepted or not)
        if blob:
            self.load(blob)

    def load(self, blob):
        d = decode(blob)
        self.data = {} if d is None else dict(d)
        self.blob = bytes(blob) if d is not None else b''
        return d is not None

    def fname(self, name):
        return (self.map + '|' + name) if self.map else name

    def has_map(self):
        """the file holds at least one cache of this map (ReloadGameCachesFromDisk replaces tables only then)."""
        if not self.map:
            return bool(self.data)
        return any(n.startswith(self.map + '|') for n in self.data)

    def cache(self, name):
        if not persistable(name):
            return None
        d = self.data.get(self.fname(name))
        return None if d is None else dict(d)

    def save(self, memory):
        """memory = {name: data} of every cache of the running game, creation order. The file keeps the caches the game did not touch.
        -> True when written; False when the file would exceed the budget (nothing changes)."""
        mem = dict((self.fname(n), d) for n, d in memory.items() if persistable(n))
        order = list(self.data) + [n for n in mem if n not in self.data]
        blob = encode([(n, mem[n] if n in mem else self.data[n]) for n in order])
        self.last_bytes = len(blob)
        if len(blob) > self.budget:
            self.refused += 1
            return False
        self.blob = blob
        self.data = dict(decode(blob))      # what a later load sees: float32 / 32-bit values
        self.dirty = True
        self.saves += 1
        return True


def golden_blobs():
    """(name, blob) pairs the Udon twin must reproduce from the same operation list (save_check.cs); the operations are in
    GOLDEN_OPS below as plain text lines so both sides replay the identical sequence."""
    out = []
    for name, ops in GOLDEN_OPS:
        out.append((name + '|' + ';'.join(ops), encode(replay(ops))))
    return out


# operation scripts: "init <cache>" | "int|real|bool|str <cache> <mission> <key> <value>" | "flush <cache>" | "flushm <cache> <mission>"
GOLDEN_OPS = (
    ('simple', ['init A', 'int A m k 5', 'real A m r 0.5', 'bool A m b 1', 'str A m s hello']),
    ('negative', ['init Hero', 'int Hero stats xp -2147483648', 'int Hero stats lvl 2147483647', 'int Hero stats z 0', 'int Hero stats n -1']),
    ('order', ['init Z', 'init A', 'int Z m2 k 1', 'int Z m1 k 2', 'int Z m2 j 3', 'int Z m2 k 4', 'int A x y 7']),
    ('flush', ['init C', 'int C m a 1', 'int C m b 2', 'int C n a 3', 'flushm C m', 'int C m c 4']),
    ('strings', ['init S', 'str S m empty ', 'str S m long ' + 'x' * 130, 'str S m uni äöüЖ中', 'str S m lat äöü']),
    ('reals', ['init R', 'real R m a 3.14159', 'real R m b -0.5', 'real R m c 1e30', 'real R m d 0.1', 'real R m e 16777217']),
    ('empty', ['init E']),
)


def replay(ops):
    """GOLDEN_OPS -> [(name, data)] in creation order (the model of the natives: overwrite keeps the place, flush removes)."""
    caches = {}
    for op in ops:
        f = op.split(' ', 4)
        if f[0] == 'init':
            caches.setdefault(f[1], {})
        elif f[0] in ('int', 'real', 'bool', 'str'):
            kind = {'int': 'Integer', 'real': 'Real', 'bool': 'Boolean', 'str': 'String'}[f[0]]
            raw = f[4] if len(f) > 4 else ''
            v = {'Integer': lambda: int(raw), 'Real': lambda: float(raw), 'Boolean': lambda: int(raw), 'String': lambda: raw}[kind]()
            caches[f[1]][(kind, f[2], f[3])] = v
        elif f[0] == 'flush':
            caches[f[1]].clear()
        elif f[0] == 'flushm':
            for k in [k for k in caches[f[1]] if k[1] == f[2]]:
                del caches[f[1]][k]
    return list(caches.items())
