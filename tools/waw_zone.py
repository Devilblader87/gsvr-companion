#!/usr/bin/env python3
"""Sequential zone walker for Call of Duty: World at War (T4, PC) fastfiles. Python 3 stdlib only.

BYOG: reads the player's own zone/<lang>/*.ff. Never write game data into the repo.

The walker interprets tools/t4_schema.py (struct layouts + zone code rules derived from OpenAssetTools, GPL-3) the
same way OpenAssetTools' generated loaders do, but instead of building game memory it keeps, per block, the map
block offset -> file position, so every back-reference can be resolved:

  * zone pointer 0xFFFFFFFF = data follows in the stream, 0xFFFFFFFE = follows + "insert" an alias slot in the
    virtual block (assets / temp data shared later), else (block << 29 | offset) + 1 = already loaded data.
  * blocks: 0 temp (offset resets on pop), 1-3 runtime (allocated but NOT in the file), 4 virtual, 5 large,
    6 physical. Alloc aligns the block offset only; the file stream itself has no padding.
  * assets live in the temp block, their members in the virtual block; a reference to an asset points at a 4-byte
    alias slot (the XAsset header slot or an inserted slot) - we map slot -> parsed asset object.

Result: Walker.assets = [(type name, struct dict)], Walker.by_name[(type, name)] for all assets, top-level and
inline (materials, images, loaded sounds, ... that only exist inside other assets).

Parsed objects: structs are dicts of member -> value; pointers are replaced by what they point at:
dict (struct), list, str (string), Blob (leaf data, decoded on demand), Unresolved (offset we could not map) or None.

Usage:
  waw_zone.py summary <file.ff> [--json out.json]     asset counts per type + names (all assets, including inline)
  waw_zone.py dump <file.ff> <type> <name> [--depth 3] print one parsed asset
"""
import argparse
import json
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import t4_schema as S  # noqa: E402

FOLLOW = 0xFFFFFFFF
INSERT = 0xFFFFFFFE
B_TEMP, B_RUNTIME, B_VIRTUAL = 0, 1, 4
BLOCK_KIND = [b["kind"] for b in S.BLOCKS]
BLOCK_NUM = {b["name"]: i for i, b in enumerate(S.BLOCKS)}
T = S.TYPES

# XAssetType index -> struct (WaW PC, same order as tools/waw_ff.ASSET_TYPES; None = not in zones / skipped)
ASSET_STRUCTS = [
    None, "PhysPreset", "PhysConstraints", "DestructibleDef", "XAnimParts", "XModel", "Material",
    "MaterialTechniqueSet", "GfxImage", "snd_alias_list_t", "LoadedSound", "clipMap_t", "clipMap_t", "ComWorld",
    "GameWorldSp", "GameWorldMp", "MapEnts", "GfxWorld", "GfxLightDef", None, "Font_s", "MenuList", "menuDef_t",
    "LocalizeEntry", "WeaponDef", "SndDriverGlobals", "FxEffectDef", "FxImpactTable", None, None, None, None,
    "RawFile", "StringTable", "PackIndex",
]
# name member per asset struct (for the index)
NAME_FIELD = {"MaterialTechniqueSet": "name", "snd_alias_list_t": "aliasName", "WeaponDef": "szInternalName",
              "Material": ("info", "name"), "MenuList": "name", "Font_s": "fontName", "menuDef_t": ("window", "name")}


class Blob:
    """Leaf data in the zone: count elements of `elem` bytes at file position pos (fmt / struct type for decode)."""
    __slots__ = ("u", "pos", "count", "elem", "fmt", "tname")

    def __init__(self, u, pos, count, elem, fmt=None, tname=None):
        self.u, self.pos, self.count, self.elem, self.fmt, self.tname = u, pos, count, elem, fmt, tname

    @property
    def nbytes(self):
        return self.count * self.elem

    def bytes(self):
        return bytes(self.u[self.pos:self.pos + self.nbytes]) if self.pos is not None else bytes(self.nbytes)

    def values(self):
        """flat list of primitives (fmt) or list of dicts (struct type)"""
        if self.tname:
            return [decode(self.tname, self.u, self.pos + i * self.elem) for i in range(self.count)]
        if not self.fmt:
            return self.bytes()
        n = self.nbytes // struct.calcsize("<" + self.fmt)
        return list(struct.unpack_from(f"<{n}{self.fmt}", self.u, self.pos)) if self.pos is not None else [0] * n

    def __repr__(self):
        return f"Blob({self.tname or self.fmt} x{self.count}, {self.nbytes} B @0x{self.pos or 0:x})"


class Unresolved:
    __slots__ = ("ptr", "why")

    def __init__(self, ptr, why):
        self.ptr, self.why = ptr, why

    def __repr__(self):
        return f"Unresolved(0x{self.ptr:08x}, {self.why})"


# ---- struct decode ------------------------------------------------------------------------------------------------
_plans = {}


def _plan(tname):
    """per type: list of (name, offset, kind, extra) for decoding a fixed struct image"""
    p = _plans.get(tname)
    if p is not None:
        return p
    t = T[tname]
    p = []
    for m in t["members"]:
        mods = m["mods"]
        nm = m["name"]
        if m.get("bits") is not None:
            p.append((nm, m["offset"], "bits", (m["fmt"], m["bitoff"] % 8, m["bits"], m["tsize"])))
            continue
        if m.get("dynamic"):
            p.append((nm, m["offset"], "dyn", None))
            continue
        arrs = []
        is_ptr = False
        for x in mods:
            if x[0] == "ptr":
                is_ptr = True
                break
            arrs.append(x[1])
        n = 1
        for a in arrs:
            n *= a
        if is_ptr:
            p.append((nm, m["offset"], "ptr", (n, arrs)))
        elif m["type"] in T and T[m["type"]]["kind"] in ("struct", "union"):
            p.append((nm, m["offset"], "struct", (m["type"], n, arrs, m["tsize"], T[m["type"]].get("anonymous") and nm is None)))
        else:
            fmt = m.get("fmt") or "B"
            p.append((nm, m["offset"], "prim", (fmt, n, arrs, m["tsize"])))
    _plans[tname] = p
    return p


def _shape(flat, arrs):
    if len(arrs) <= 1:
        return flat
    step = len(flat) // arrs[0]
    return [_shape(flat[i * step:(i + 1) * step], arrs[1:]) for i in range(arrs[0])]


def decode(tname, u, pos, limit=None, out=None):
    """Decode the fixed image of struct tname at u[pos:]. limit = only members starting before this offset."""
    d = {} if out is None else out
    for nm, off, kind, x in _plan(tname):
        if limit is not None and off >= limit:
            continue
        if kind == "dyn":
            continue
        p = pos + off
        if kind == "ptr":
            n, arrs = x
            v = list(struct.unpack_from(f"<{n}I", u, p)) if n > 1 or arrs else struct.unpack_from("<I", u, p)[0]
            d[nm] = _shape(v, arrs) if arrs else v
        elif kind == "prim":
            fmt, n, arrs, ts = x
            if arrs:
                d[nm] = _shape(list(struct.unpack_from(f"<{n}{fmt}", u, p)), arrs)
            else:
                d[nm] = struct.unpack_from("<" + fmt, u, p)[0]
        elif kind == "struct":
            st, n, arrs, ts, anon = x
            if anon:
                decode(st, u, p, out=d)
            elif arrs:
                d[nm] = _shape([decode(st, u, p + i * ts) for i in range(n)], arrs)
            else:
                d[nm] = decode(st, u, p)
        elif kind == "bits":
            fmt, bo, nb, ts = x
            v = int.from_bytes(u[p:p + ts], "little") if ts > 1 else u[p]
            d[nm] = (v >> bo) & ((1 << nb) - 1)
    return d


# ---- expressions --------------------------------------------------------------------------------------------------
def c_div(a, b):
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b >= 0) else -q


OPS = {
    "+": lambda a, b: a + b, "-": lambda a, b: a - b, "*": lambda a, b: a * b, "/": c_div,
    "%": lambda a, b: a - b * c_div(a, b), "<<": lambda a, b: a << b, ">>": lambda a, b: a >> b,
    "<": lambda a, b: int(a < b), "<=": lambda a, b: int(a <= b), ">": lambda a, b: int(a > b),
    ">=": lambda a, b: int(a >= b), "==": lambda a, b: int(a == b), "!=": lambda a, b: int(a != b),
    "&": lambda a, b: a & b, "^": lambda a, b: a ^ b, "|": lambda a, b: a | b,
    "&&": lambda a, b: int(bool(a) and bool(b)), "||": lambda a, b: int(bool(a) or bool(b)),
}


class Walker:
    def __init__(self, u, strict=False, log=None):
        self.u = u
        self.pos = 0
        self.off = [0] * 7
        self.stack = []
        self.temp_saved = []
        self.segs = [[] for _ in range(7)]     # per block: [blockoff, filepos, length]
        self.objs = {}                         # (block, offset) -> object loaded there
        self.alias = {}                        # virtual offset of a pointer slot -> object
        self.cur = {}
        self.assets = []                       # top-level (type, obj)
        self.by_name = {}                      # (type, name) -> obj (all assets incl. inline)
        self.inline_count = {}
        self.unresolved = 0
        self.strict = strict
        self.log = log
        self.script_strings = []
        self.refpos = {}                       # id(obj) -> file position, for objects decoded from a back-reference
        self._addr = None

    def where(self, obj):
        """File position of obj in the zone: for a back-referenced (interior) target the position it was decoded
        from, for a loaded array / struct the start of its data. None if unknown."""
        p = self.refpos.get(id(obj))
        if p is not None:
            return p
        if isinstance(obj, Blob):
            return obj.pos
        if self._addr is None or self._addr[0] != len(self.objs):
            self._addr = (len(self.objs), {id(o): k for k, o in self.objs.items()})
        k = self._addr[1].get(id(obj))
        return self.filepos(*k) if k is not None else None

    def ptr_filepos(self, ptr):
        """File position a zone pointer value (block << 29 | offset) + 1 refers to (None for runtime / unmapped)."""
        b, o = self.split(ptr)
        return self.filepos(b, o) if b < 7 else None

    # ---- block stream
    def push(self, b):
        self.stack.append(b)
        if BLOCK_KIND[b] == "temp":
            self.temp_saved.append(self.off[b])

    def pop(self):
        b = self.stack.pop()
        if BLOCK_KIND[b] == "temp":
            self.off[b] = self.temp_saved.pop()

    def alloc(self, align):
        b = self.stack[-1]
        if align > 1:
            self.off[b] = (self.off[b] + align - 1) // align * align
        return b, self.off[b]

    def read(self, n):
        """load n bytes into the current block; returns file position (None for runtime blocks)"""
        b = self.stack[-1]
        kind = BLOCK_KIND[b]
        if kind == "runtime":
            self.off[b] += n
            return None
        p = self.pos
        if kind == "normal" and n:
            sg = self.segs[b]
            if sg and sg[-1][0] + sg[-1][2] == self.off[b] and sg[-1][1] + sg[-1][2] == p:
                sg[-1][2] += n
            else:
                sg.append([self.off[b], p, n])
        self.pos += n
        self.off[b] += n
        if self.pos > len(self.u):
            raise EOFError(f"read past end of zone ({self.pos} > {len(self.u)})")
        return p

    def insert_slot(self):
        b = B_VIRTUAL
        self.off[b] = (self.off[b] + 3) // 4 * 4
        s = self.off[b]
        self.off[b] += 4
        return s

    def filepos(self, block, off):
        sg = self.segs[block]
        lo, hi = 0, len(sg)
        while lo < hi:
            mid = (lo + hi) // 2
            if sg[mid][0] <= off:
                lo = mid + 1
            else:
                hi = mid
        if lo == 0:
            return None
        s = sg[lo - 1]
        if off < s[0] + s[2]:
            return s[1] + off - s[0]
        return None

    def split(self, ptr):
        v = (ptr - 1) & 0xFFFFFFFF
        return v >> 29, v & 0x1FFFFFFF

    def resolve(self, ptr):
        """offset pointer -> object loaded there, else file position (int), else Unresolved"""
        b, o = self.split(ptr)
        if b >= 7:
            return Unresolved(ptr, "bad block")
        obj = self.objs.get((b, o))
        if obj is not None:
            return obj
        if BLOCK_KIND[b] == "runtime":
            return Unresolved(ptr, "runtime block")
        fp = self.filepos(b, o)
        if fp is None:
            self.unresolved += 1
            return Unresolved(ptr, "no segment")
        return fp

    def resolve_alias(self, ptr):
        b, o = self.split(ptr)
        obj = self.alias.get((b, o))
        if obj is None:
            obj = self.objs.get((b, o))
        if obj is None:
            self.unresolved += 1
            return Unresolved(ptr, "alias")
        return obj

    # ---- expressions
    def ev(self, e):
        k = e[0]
        if k == "num":
            return e[1]
        if k == "op":
            return OPS[e[1]](self.ev(e[2]), self.ev(e[3]))
        obj = self.cur[e[1]]
        for m in e[2]:
            obj = obj[m]
        for ix in e[3]:
            obj = obj[self.ev(ix)]
        if isinstance(obj, float):
            obj = int(obj)
        return obj

    # ---- strings
    def cstr_at(self, fp):
        e = self.u.index(b"\0", fp)
        return self.u[fp:e].decode("latin1")

    def xstring(self, ptr):
        if ptr == 0:
            return None
        if ptr == FOLLOW or ptr == INSERT:
            b, o = self.alloc(1)
            e = self.u.index(b"\0", self.pos)
            s = self.u[self.pos:e].decode("latin1")
            self.read(e + 1 - self.pos)
            self.objs[(b, o)] = s
            return s
        r = self.resolve(ptr)
        if isinstance(r, int):
            return self.cstr_at(r)
        return r

    # ---- structs
    def load_struct(self, tname):
        """read (the fixed or partial image of) a struct from the stream and process its members"""
        t = T[tname]
        b = self.stack[-1]
        base = (b, self.off[b]) if BLOCK_KIND[b] == "normal" else None
        dyn = t["dynamic"]
        if dyn is None:
            p = self.read(t["size"])
            inst = decode(tname, self.u, p) if p is not None else {}
        elif t["kind"] == "union":
            inst = {}
        else:
            lim = next(m["offset"] for m in t["members"] if m["name"] == dyn)
            p = self.read(lim)
            inst = decode(tname, self.u, p, limit=lim) if p is not None else {}
        self.process(tname, inst, base)
        return inst

    def process(self, tname, inst, base=None):
        t = T[tname]
        self.cur[tname] = inst
        blk = None
        if t.get("asset"):
            blk = B_VIRTUAL
        elif t.get("block"):
            blk = BLOCK_NUM[t["block"]]
        if blk is not None:
            self.push(blk)
        byname = {m["name"]: m for m in t["members"]}
        is_union = t["kind"] == "union"
        union_dyn = is_union and t["dynamic"] is not None
        for nm in t["order"]:
            m = byname[nm]
            if m.get("ignore"):
                continue
            if is_union:
                if not union_dyn and m["leaf"]:
                    continue
                c = m.get("condition")
                if c is not None and not self.ev(c):
                    continue
                self.member(t, inst, m, union_dyn, base)
                break
            needs = m["dynmember"] or not m["leaf"]
            if not needs:
                continue
            c = m.get("condition")
            if c is not None and not self.ev(c):
                continue
            self.member(t, inst, m, False, base)
        if blk is not None:
            self.pop()

    def member(self, t, inst, m, after_partial, base=None):
        nm = m["name"]
        mblk = m.get("block")
        push = mblk is not None and not (mblk == "XFILE_BLOCK_VIRTUAL")
        if push:
            self.push(BLOCK_NUM[mblk])
        try:
            if m["dynmember"] or after_partial:
                v = self.read_member(m)
                if nm is None:
                    inst.update(v)
                else:
                    inst[nm] = v
            elif nm is None:
                self.process(m["type"], inst, _at(base, m["offset"]))   # anonymous struct/union: merged fields
            else:
                inst[nm] = self.ref(m, m["mods"], inst[nm], 0, _at(base, m["offset"]))
        finally:
            if push:
                self.pop()

    def read_member(self, m):
        """member data that follows in the stream after a partial struct load (dynamic members / dynamic unions)"""
        mods = m["mods"]
        tn = m["type"]
        is_struct = tn in T and T[tn]["kind"] in ("struct", "union")
        if mods and mods[0][0] == "arr":
            n = self.ev(mods[0][2]) if len(mods[0]) > 2 else mods[0][1]
            for x in mods[1:]:
                n *= x[1]
            if is_struct and not T[tn]["leaf"]:
                if T[tn]["dynamic"]:
                    return [self.load_struct(tn) for _ in range(n)]
                b0 = self.stack[-1]
                a0 = (b0, self.off[b0]) if BLOCK_KIND[b0] == "normal" else None
                p = self.read(n * m["tsize"])
                arr = [decode(tn, self.u, p + i * m["tsize"]) if p is not None else {} for i in range(n)]
                for i, x in enumerate(arr):
                    self.process(tn, x, _at(a0, i * m["tsize"]))
                return arr
            p = self.read(n * m["tsize"])
            return Blob(self.u, p, n, m["tsize"], None if is_struct else m.get("fmt"), tn if is_struct else None)
        if mods and mods[0][0] == "ptr":
            b0 = self.stack[-1]
            a0 = (b0, self.off[b0]) if BLOCK_KIND[b0] == "normal" else None
            p = self.read(4)
            v = struct.unpack_from("<I", self.u, p)[0] if p is not None else 0
            return self.ref(m, mods, v, 0, a0)
        if is_struct:
            return self.load_struct(tn)
        p = self.read(m["tsize"])
        return struct.unpack_from("<" + m["fmt"], self.u, p)[0] if p is not None else 0

    def ref(self, m, mods, val, flat, addr=None):
        if not mods:
            tn = m["type"]
            if tn in T and not T[tn]["leaf"]:
                if isinstance(val, list):
                    for i, x in enumerate(val):
                        self.process(tn, x, _at(addr, i * m["tsize"]))
                else:
                    self.process(tn, val, addr)
            return val
        k = mods[0]
        if k[0] == "arr":
            if not isinstance(val, list):
                return val
            stride = _stride(m, mods[1:])
            return [self.ref(m, mods[1:], v, flat * k[1] + i, _at(addr, i * stride)) for i, v in enumerate(val)]
        # pointer
        if val == 0 or val is None:
            return None
        cnt_e = k[1] if len(k) > 1 else None
        if len(k) > 2 and k[2] and flat in k[2]:
            cnt_e = k[2][flat]
        rest = mods[1:]
        tn = m["type"]
        if m.get("string") and not rest:
            return self.xstring(val)
        if rest and rest[0][0] == "ptr":
            # pointer array: count pointers, each a single pointer
            n = self.ev(cnt_e) if cnt_e is not None else 1
            if val != FOLLOW and val != INSERT:
                r = self.resolve(val)
                return r if not isinstance(r, int) else Unresolved(val, "ptr array by file pos")
            b, o = self.alloc(4)
            p = self.read(4 * n)
            ptrs = list(struct.unpack_from(f"<{n}I", self.u, p)) if p is not None else [0] * n
            normal = BLOCK_KIND[b] == "normal"
            out = [self.ref(m, rest, v, i, (b, o + 4 * i) if normal else None) for i, v in enumerate(ptrs)]
            self.objs[(b, o)] = out
            return out
        if tn in T and T[tn].get("asset"):
            obj = self.asset_ptr(tn, val)
            if addr is not None and obj is not None and not isinstance(obj, Unresolved):
                self.alias.setdefault(addr, obj)     # any pointer slot to an asset can be referenced later
            return obj
        n = self.ev(cnt_e) if cnt_e is not None else None
        elem = m["tsize"]
        for x in rest:
            elem *= x[1]
        in_temp = mblock_is_temp(m)
        is_struct = tn in T and T[tn]["kind"] in ("struct", "union")
        if val != FOLLOW and not (in_temp and val == INSERT):
            if in_temp:
                return self.resolve_alias(val)
            r = self.resolve(val)
            if isinstance(r, int):
                if is_struct and n is None:
                    obj = decode(tn, self.u, r)
                else:
                    obj = Blob(self.u, r, n if n is not None else 1, elem, None if is_struct else m.get("fmt"),
                               tn if is_struct else None)
                self.refpos[id(obj)] = r
                return obj
            return r
        if "allocalign" in m:
            align = self.ev(m["allocalign"])
        else:
            align = m["talign"]
        b, o = self.alloc(align)
        slot = self.insert_slot() if (in_temp and val == INSERT) else None
        runtime = BLOCK_KIND[self.stack[-1]] == "runtime"
        if is_struct and not T[tn]["leaf"] and not runtime and not rest:
            if n is None:
                obj = self.load_struct(tn)
            elif T[tn]["dynamic"]:
                obj = [self.load_struct(tn) for _ in range(n)]
            else:
                p = self.read(n * elem)
                obj = [decode(tn, self.u, p + i * elem) for i in range(n)]
                normal = BLOCK_KIND[b] == "normal"
                for i, x in enumerate(obj):
                    self.process(tn, x, (b, o + i * elem) if normal else None)
        elif is_struct and T[tn]["dynamic"] and not runtime and not rest and n is None:
            obj = self.load_struct(tn)       # leaf struct with dynamic tail (GfxImageLoadDef)
        else:
            p = self.read((n if n is not None else 1) * elem)
            if is_struct and n is None and not rest and p is not None:
                obj = decode(tn, self.u, p)
            else:
                obj = Blob(self.u, p, n if n is not None else 1, elem, None if is_struct else m.get("fmt"),
                           tn if is_struct else None)
        self.objs[(b, o)] = obj
        if slot is not None:
            self.alias[(B_VIRTUAL, slot)] = obj
        return obj

    def asset_ptr(self, tname, val):
        if val == 0:
            return None
        t = T[tname]
        in_temp = t.get("block") == "XFILE_BLOCK_TEMP"   # StringTable (and a few others) live in the virtual block
        if in_temp:
            self.push(B_TEMP)
        try:
            if val == FOLLOW or (in_temp and val == INSERT):
                align = self.ev(t["allocalign"]) if "allocalign" in t else t["align"]
                b, o = self.alloc(align)
                slot = self.insert_slot() if val == INSERT else None
                obj = self.load_struct(tname)
                if slot is not None:
                    self.alias[(B_VIRTUAL, slot)] = obj
                if not in_temp:
                    self.objs[(b, o)] = obj
                self.index(tname, obj, inline=True)
                return obj
            if in_temp:
                return self.resolve_alias(val)
            r = self.resolve(val)
            return r if not isinstance(r, int) else Unresolved(val, "asset by file pos")
        finally:
            if in_temp:
                self.pop()

    def index(self, tname, obj, inline):
        nf = NAME_FIELD.get(tname, "name")
        try:
            name = obj[nf[0]][nf[1]] if isinstance(nf, tuple) else obj.get(nf)
        except (KeyError, TypeError):
            name = None
        if isinstance(name, str):
            self.by_name.setdefault((tname, name), obj)
        if inline:
            self.inline_count[tname] = self.inline_count.get(tname, 0) + 1

    # ---- whole zone
    def walk(self, start=36, progress=False, stop_after=None, on_asset=None):
        """Walk the whole zone. on_asset(type name, obj, zone file position) is called after each top-level asset."""
        u = self.u
        sc, sp, ac, ap = struct.unpack_from("<iIiI", u, start)
        self.pos = start + 16
        self.push(B_VIRTUAL)
        if sp:
            self.alloc(4)
            p = self.read(4 * sc)
            ptrs = struct.unpack_from(f"<{sc}I", u, p)
            self.script_strings = [self.xstring(x) for x in ptrs]
        if ap:
            b, o = self.alloc(4)
            p = self.read(8 * ac)
            heads = [struct.unpack_from("<iI", u, p + 8 * i) for i in range(ac)]
            t0 = time.time()
            for i, (typ, hp) in enumerate(heads):
                tn = ASSET_STRUCTS[typ] if 0 <= typ < len(ASSET_STRUCTS) else None
                if tn is None:
                    raise ValueError(f"asset {i}: unsupported type {typ}")
                at = self.pos
                try:
                    obj = self.asset_ptr(tn, hp)
                except Exception as ex:
                    raise RuntimeError(f"asset {i}/{ac} type {tn} at zone 0x{at:x}: {ex!r}") from ex
                self.alias[(B_VIRTUAL, o + 8 * i + 4)] = obj
                self.assets.append((tn, obj, at))
                self.inline_count[tn] -= 1
                self.index(tn, obj, inline=False)
                if on_asset is not None:
                    on_asset(tn, obj, at)
                if progress and i % 500 == 0:
                    print(f"  asset {i}/{ac} {tn} @0x{at:x} {time.time() - t0:.1f}s", file=sys.stderr)
                if stop_after is not None and i + 1 >= stop_after:
                    break
        self.pop()
        return self


def _at(addr, delta):
    return None if addr is None else (addr[0], addr[1] + delta)


def _stride(m, rest):
    """byte size of one element described by the remaining modifiers"""
    s = 1
    for x in rest:
        if x[0] == "ptr":
            return s * 4
        s *= x[1]
    return s * m["tsize"]


def mblock_is_temp(m):
    b = m.get("block")
    return b is not None and BLOCK_KIND[BLOCK_NUM[b]] == "temp"


def asset_name(tname, obj):
    nf = NAME_FIELD.get(tname, "name")
    try:
        return obj[nf[0]][nf[1]] if isinstance(nf, tuple) else obj.get(nf)
    except (KeyError, TypeError):
        return None


def open_zone(path, cache=None, **kw):
    """Decompress (cached) and walk a fastfile. Returns the Walker."""
    import waw_ff
    _, u = waw_ff.load_zone(path, cache or waw_ff.DEFAULT_CACHE)
    w = Walker(u, **kw)
    w.walk(progress=kw.get("log") is not None)
    return w


_WALK_CACHE = {}


def zone(path, cache=None):
    """memoised open_zone for tools that work on the same file several times in one process"""
    if path not in _WALK_CACHE:
        _WALK_CACHE[path] = open_zone(path, cache)
    return _WALK_CACHE[path]


def to_jsonable(o, depth=4):
    if depth < 0:
        return "..."
    if isinstance(o, dict):
        return {k: to_jsonable(v, depth - 1) for k, v in o.items()}
    if isinstance(o, list):
        if len(o) > 24:
            return [to_jsonable(v, depth - 1) for v in o[:24]] + [f"... {len(o)} total"]
        return [to_jsonable(v, depth - 1) for v in o]
    if isinstance(o, (Blob, Unresolved)):
        return repr(o)
    return o


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("summary"); p.add_argument("file"); p.add_argument("--json")
    p = sp.add_parser("dump"); p.add_argument("file"); p.add_argument("type"); p.add_argument("name")
    p.add_argument("--depth", type=int, default=3)
    a = ap.parse_args()
    t0 = time.time()
    w = open_zone(a.file)
    dt = time.time() - t0
    if a.cmd == "summary":
        top = {}
        for tn, obj, _ in w.assets:
            top[tn] = top.get(tn, 0) + 1
        names = {}
        for (tn, nm) in w.by_name:
            names.setdefault(tn, []).append(nm)
        print(f"{os.path.basename(a.file)}: walked {len(w.assets)} assets in {dt:.1f}s, stream end 0x{w.pos:x} of "
              f"0x{len(w.u):x} ({'complete' if w.pos == len(w.u) else 'NOT at end'}), unresolved refs {w.unresolved}")
        for tn in sorted(set(top) | set(names)):
            print(f"  {tn:22s} top-level {top.get(tn, 0):5d}  named (incl. inline) {len(names.get(tn, [])):5d}")
        if a.json:
            with open(a.json, "w") as f:
                json.dump({"top": top, "names": {k: sorted(v) for k, v in names.items()}, "stream_end": w.pos,
                           "zone_size": len(w.u), "unresolved": w.unresolved}, f, indent=1)
    else:
        obj = w.by_name.get((a.type, a.name))
        if obj is None:
            sys.exit(f"{a.type} {a.name} not found")
        print(json.dumps(to_jsonable(obj, a.depth), indent=1, default=str))


if __name__ == "__main__":
    main()
