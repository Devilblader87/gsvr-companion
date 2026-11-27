"""Minimal reader for the client's compiled Lua 5.1 tables (*.lub: AccessoryId, AccName, WeaponTable, ...). Stdlib only, read only.

Written from the public Lua 5.1 bytecode layout (header, function block, 32 bit instructions op6 A8 C9 B9). It is NOT a Lua VM: it follows
the few opcodes the client's data tables use (LOADK, MOVE, GETGLOBAL, SETGLOBAL, SETTABLE, NEWTABLE, SETLIST ordering ignored) and reports
every "table[key] = value" and "global = value" it sees, with keys / values resolved through constants and earlier GETGLOBAL names:

    tables(data)-> {global name: value}     w43: the data tables of iteminfo / Towninfo / quest lists with NESTED tables evaluated (dict; array part keys 1..n)
    parse(data) -> [(scope, key, value)]   scope = '' for a global assignment, else the global name of the table when it can be told
                                            (the last SETGLOBAL target of the table register is resolved after the function ends)
    flat(data)  -> {key: value}             every assignment merged (later wins); keys / values are str | int | float | ('g', name) for a global reference

Raises LubError on anything that is not Lua 5.1 bytecode with the standard 4 byte int / size_t, 4 byte instruction and 8 byte number.
"""
import struct


class LubError(ValueError):
    pass


OP_MOVE, OP_LOADK, OP_LOADBOOL, OP_LOADNIL, OP_GETGLOBAL, OP_GETTABLE, OP_SETGLOBAL, OP_SETTABLE, OP_NEWTABLE = 0, 1, 2, 3, 5, 6, 7, 9, 10
OP_SETLIST, OP_RETURN, OP_CLOSE, OP_CLOSURE = 34, 30, 35, 36


class _R:
    def __init__(self, d):
        self.d, self.p = d, 0

    def take(self, n):
        if self.p + n > len(self.d):
            raise LubError('truncated')
        b = self.d[self.p:self.p + n]
        self.p += n
        return b

    def u32(self):
        return struct.unpack('<I', self.take(4))[0]

    def string(self):
        n = self.u32()
        if n == 0:
            return None
        if n > 1 << 24:
            raise LubError('string too long')
        return self.take(n)[:-1]


def _func(r, out):
    r.string()                          # source
    r.take(8)                           # line defined, last line
    r.take(4)                           # nups, nparams, vararg, maxstack
    n = r.u32()
    if n > 1 << 22:
        raise LubError('code too long')
    code = struct.unpack('<%dI' % n, r.take(4 * n))
    k = []
    for _ in range(r.u32()):
        t = r.take(1)[0]
        if t == 0:
            k.append(None)
        elif t == 1:
            k.append(bool(r.take(1)[0]))
        elif t == 3:
            v = struct.unpack('<d', r.take(8))[0]
            k.append(int(v) if v == int(v) else v)
        elif t == 4:
            s = r.string()
            k.append(s)
        else:
            raise LubError('constant type %d' % t)
    for _ in range(r.u32()):
        _func(r, out)
    for _ in range(r.u32()):            # line info
        r.take(4)
    for _ in range(r.u32()):            # locals
        r.string()
        r.take(8)
    for _ in range(r.u32()):            # upvalue names
        r.string()
    out.append((code, k))


def parse(data):
    if data[:5] != b'\x1bLuaQ' or data[5:12] != b'\x00\x01\x04\x04\x04\x08\x00':
        raise LubError('not a Lua 5.1 chunk with the standard sizes')
    r = _R(data)
    r.p = 12
    fns = []
    _func(r, fns)
    code, k = fns[-1]                   # the main function is written last
    reg = {}                            # register -> value | ('g', name) | ('t', id)
    tabname = {}                        # table id -> global name
    res = []
    tid = 0

    def rk(x):
        return k[x - 256] if x >= 256 else reg.get(x)

    def txt(v):
        return v.decode('cp949', 'replace') if isinstance(v, (bytes, bytearray)) else v

    for ins in code:
        op, a, c, b = ins & 63, (ins >> 6) & 255, (ins >> 14) & 511, (ins >> 23) & 511
        bx = (ins >> 14) & 0x3ffff
        if op == OP_MOVE:
            reg[a] = reg.get(b)
        elif op == OP_LOADK:
            reg[a] = k[bx]
        elif op == OP_LOADBOOL:
            reg[a] = bool(b)
        elif op == OP_LOADNIL:
            for i in range(a, b + 1):
                reg[i] = None
        elif op == OP_GETGLOBAL:
            reg[a] = ('g', txt(k[bx]))
        elif op == OP_GETTABLE:
            t = reg.get(b)
            kk = rk(c)
            reg[a] = ('gt', t[1], txt(kk)) if isinstance(t, tuple) and t[0] == 'g' else None
        elif op == OP_NEWTABLE:
            tid += 1
            reg[a] = ('t', tid)
        elif op == OP_SETGLOBAL:
            v = reg.get(a)
            if isinstance(v, tuple) and v[0] == 't':
                tabname[v[1]] = txt(k[bx])
            else:
                res.append((None, txt(k[bx]), txt(v)))
        elif op == OP_SETTABLE:
            t = reg.get(a)
            key, val = rk(b), rk(c)
            if isinstance(t, tuple) and t[0] == 't':
                res.append((t[1], txt(key), txt(val)))
    named = [(tabname.get(t) if t is not None else '', key, val) for t, key, val in res]
    tables = {}
    for sc, key, val in named:
        tables.setdefault(sc, {})[key] = val

    def fix(v):
        if isinstance(v, tuple) and v[0] == 'gt':
            return tables.get(v[1], {}).get(v[2], v)
        return v
    return [(sc, fix(key), fix(val)) for sc, key, val in named]


def flat(data):
    return dict((key, val) for _s, key, val in parse(data))


def tables(data):
    """w43 (docs/design/ro_rotxt.md): evaluates the straight-line data chunk of a client table file (iteminfo.lub, Towninfo.lub, *QuestInfoList*.lub ...):
    NEWTABLE / SETTABLE / SETLIST / LOADK / LOADBOOL / LOADNIL / MOVE / GETGLOBAL / SETGLOBAL, CLOSURE / CLOSE ignored, nothing else (no calls, no jumps: the loader
    function `main` of those files is a child function and is not run). -> {global name: value}; a table is a dict (SETLIST array part = keys 1..n), strings are bytes
    decoded as cp949 (the client's own code page), numbers int / float, booleans bool. Raises LubError on an opcode outside that set that touches a table."""
    if data[:5] != b'\x1bLuaQ' or data[5:12] != b'\x00\x01\x04\x04\x04\x08\x00':
        raise LubError('not a Lua 5.1 chunk with the standard sizes')
    r = _R(data)
    r.p = 12
    fns = []
    _func(r, fns)
    code, k = fns[-1]
    reg = {}
    glob = {}

    def rk(x):
        return k[x - 256] if x >= 256 else reg.get(x)

    def txt(v):
        return v.decode('cp949', 'replace') if isinstance(v, (bytes, bytearray)) else v

    for ins in code:
        op, a, c, b = ins & 63, (ins >> 6) & 255, (ins >> 14) & 511, (ins >> 23) & 511
        bx = (ins >> 14) & 0x3ffff
        if op == OP_MOVE:
            reg[a] = reg.get(b)
        elif op == OP_LOADK:
            reg[a] = k[bx]
        elif op == OP_LOADBOOL:
            reg[a] = bool(b)
        elif op == OP_LOADNIL:
            for i in range(a, b + 1):
                reg[i] = None
        elif op == OP_GETGLOBAL:
            reg[a] = glob.get(txt(k[bx]))
        elif op == OP_NEWTABLE:
            reg[a] = {}
        elif op == OP_SETGLOBAL:
            glob[txt(k[bx])] = reg.get(a)
        elif op == OP_SETTABLE:
            t = reg.get(a)
            if not isinstance(t, dict):
                raise LubError('SETTABLE on a non table')
            t[txt(rk(b))] = rk(c)
        elif op == OP_SETLIST:
            t = reg.get(a)
            if not isinstance(t, dict):
                raise LubError('SETLIST on a non table')
            n = b
            base = (c - 1) * 50 if c else 0
            for i in range(1, n + 1):
                t[base + i] = reg.get(a + i)
        elif op == OP_CLOSURE:
            reg[a] = None                       # the loader function (main) is only defined here, never run
        elif op == OP_CLOSE:
            pass
        elif op == OP_RETURN:
            break
        else:
            raise LubError('opcode %d outside the data subset' % op)
    # strings were kept as bytes in registers / constants: decode them in place once
    def fix(v):
        if isinstance(v, (bytes, bytearray)):
            return v.decode('cp949', 'replace')
        if isinstance(v, dict):
            for kk in list(v):
                v[kk] = fix(v[kk])
        return v
    return dict((kk, fix(vv)) for kk, vv in glob.items())
