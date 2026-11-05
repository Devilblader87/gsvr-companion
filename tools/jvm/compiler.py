#!/usr/bin/env python3
"""JASS AST (tools/jass.py) -> compact typed bytecode for the reference VM and the later Udon VM.

Format (docs/jvm_bytecode.md): fixed 3-int instructions [op, a, b] in one int array, a parallel int array
`cost` (game-instruction weight of each of our instructions, used for the 300 000 op limit), a real pool
(float32 values), a string pool, a function table, a native table and a global table. Values are untyped
32-bit slots for integer/boolean/handle/code, float32 for real, string-or-null for string; the compiler knows
every type statically, so the VM needs no tags.

All op-count weights live in COST below (one place); see docs/jvm_bytecode.md "Op counting".
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import jass  # noqa: E402

# ---------------------------------------------------------------- opcodes (ours; game op in comment)
OPS = [
    'NOP',       # 0
    'PUSH_I',    # 1  b = 32-bit value (integer/boolean/null handle)           game 0x0c
    'PUSH_R',    # 2  b = real pool index                                      game 0x0c
    'PUSH_S',    # 3  b = string pool index, -1 = null                         game 0x0c
    'PUSH_C',    # 4  b = code value (function index + 1)                      game 0x0f
    'LOAD_L',    # 5  a = local slot                                           game 0x0e
    'LOAD_G',    # 6  a = global index                                         game 0x0e
    'LOAD_LA',   # 7  a = local slot (array), index popped                     game 0x10
    'LOAD_GA',   # 8  a = global (array), index popped                         game 0x10
    'STORE_L',   # 9  a = local slot, b = 1 if handle (ref-counted)            game 0x11
    'STORE_G',   # 10 a = global, b = handle flag                              game 0x11
    'STORE_LA',  # 11 a = local slot, b = handle flag; pops value, index       game 0x12
    'STORE_GA',  # 12 a = global, b = handle flag                              game 0x12
    'CALL_N',    # 13 a = native index, b = argc                               game 0x13.. 0x15 0x0b
    'CALL_J',    # 14 a = function index, b = argc                             game 0x13.. 0x16 0x0b
    'RET',       # 15 a = 1 if a value is returned                             game 0x27
    'JMP',       # 16 b = target pc                                            game 0x2b + 0x28
    'JZ',        # 17 b = target pc (pop, jump if false)                       game 0x2a
    'JNZ',       # 18 b = target pc (pop, jump if true)                        game 0x29
    'POP',       # 19 drop the top value (call statement of a value function)  (none in game)
    'I2R',       # 20 integer -> real of the top value                         game 0x17
    'ADD_I', 'ADD_R', 'ADD_S', 'SUB_I', 'SUB_R', 'MUL_I', 'MUL_R', 'DIV_I', 'DIV_R', 'MOD_I',   # 21..30
    'NEG_I', 'NEG_R', 'NOT', 'AND', 'OR',                                                     # 31..35
    'EQ_I', 'EQ_R', 'EQ_S', 'EQ_B', 'NE_I', 'NE_R', 'NE_S', 'NE_B',                           # 36..43
    'LT_I', 'LT_R', 'LE_I', 'LE_R', 'GT_I', 'GT_R', 'GE_I', 'GE_R',                           # 44..51
    'CAST',      # 52 a = from slot class, b = to slot class (0 int, 1 real, 2 string): return bug
    'END',       # 53 end of a global initialiser (status 1)
]
OP = {n: i for i, n in enumerate(OPS)}
globals().update(OP)

# slot classes
SC_INT, SC_REAL, SC_STR = 0, 1, 2

# ---------------------------------------------------------------- op-count weights (THE one place)
# Our instruction -> number of game bytecode instructions it stands for (NOTES_jass §3 instruction set;
# game codegen inferred, see docs/jvm_bytecode.md "Op counting"):
#  literal / code ref / variable load / array load / store = 1 game op each (0x0c 0x0f 0x0e 0x10 0x11);
#  array store = PUSH index + POP + MOVAR = 3; binary op = PUSH left + POP + op = 3; unary / I2R = 1;
#  call = one PUSH per argument + CALL + POPN (if args) [+ FUNCARG per parameter + LOCAL per local +
#  RETURN for JASS functions]; jump = JUMP + LABEL = 2; conditional jump = 1 + LABEL = 2.
COST = {
    'literal': 1, 'load': 1, 'store': 1, 'store_array': 3, 'binary': 3, 'unary': 1, 'i2r': 1,
    'call': 1, 'call_arg': 1, 'call_popn': 1, 'funcarg': 1, 'local_decl': 1, 'ret': 1,
    'jump': 2, 'cjump': 2, 'pop': 0, 'cast': 0, 'end': 0,
}
BIN_OPS = set(range(OP['ADD_I'], OP['MOD_I'] + 1)) | {OP['AND'], OP['OR']} | set(range(OP['EQ_I'], OP['GE_R'] + 1))

JASS_ARRAY_SIZE = 8192   # 1.21 array size; outside: read -> zero, write ignored (Open: not decompiled)

NULL_TYPE = 'null'


class CompileError(Exception):
    pass


class FuncInfo:
    __slots__ = ('name', 'index', 'entry', 'nparams', 'nlocals', 'param_ref', 'local_arrays', 'local_types',
                 'ret_type', 'ret_sc', 'entry_cost', 'src', 'constant', 'param_types')


class Program:
    """Everything the VM needs; serialisable as int arrays (see to_arrays)."""

    def __init__(self):
        self.ops, self.A, self.B, self.cost = [], [], [], []
        self.reals, self.strings = [], []
        self._real_ix, self._str_ix = {}, {}
        self.functions, self.func_by_name = [], {}
        self.natives, self.native_by_name = [], {}
        self.globals, self.global_by_name = [], {}
        self.ginit = []          # function indices of global initialisers, declaration order
        self.types = {}          # type -> parent
        self.enum_types = set()  # handle types made by Convert* natives (raw ints, never ref-counted)
        self.line_of_pc = []

    # pools
    def real(self, v):
        v = f32(v)
        key = f32bits(v)
        if key not in self._real_ix:
            self._real_ix[key] = len(self.reals)
            self.reals.append(v)
        return self._real_ix[key]

    def string(self, s):
        if s is None:
            return -1
        if s not in self._str_ix:
            self._str_ix[s] = len(self.strings)
            self.strings.append(s)
        return self._str_ix[s]

    def to_arrays(self):
        code = []
        for i in range(len(self.ops)):
            code += [self.ops[i], self.A[i], self.B[i]]
        return {
            'code': code, 'cost': list(self.cost), 'reals': list(self.reals), 'strings': list(self.strings),
            'functions': [[f.entry, f.nparams, f.nlocals, f.ret_sc, f.entry_cost] for f in self.functions],
            'natives': [n['name'] for n in self.natives],
            'globals': [[g['name'], g['type'], int(g['array'])] for g in self.globals],
            'ginit': list(self.ginit),
        }


def f32(v):
    import struct
    try:
        return struct.unpack('<f', struct.pack('<f', v))[0]
    except OverflowError:
        return float('inf') if v > 0 else float('-inf')


def f32bits(v):
    import struct
    try:
        return struct.unpack('<I', struct.pack('<f', v))[0]
    except OverflowError:
        return 0x7f800000 if v > 0 else 0xff800000


# ---------------------------------------------------------------- types

class Types:
    def __init__(self, prog):
        self.p = prog

    def is_handle(self, t):
        seen = 0
        while t in self.p.types and seen < 64:
            t = self.p.types[t]
            seen += 1
        return t == 'handle'

    def slot(self, t):
        if t == 'real':
            return SC_REAL
        if t == 'string':
            return SC_STR
        return SC_INT

    def refcounted(self, t):
        # every handle type except the Convert* enums; at run time only ids >= 0x100000 are counted
        return self.is_handle(t) and t not in self.p.enum_types

    def extends(self, t, base):
        n = 0
        while t is not None and n < 64:
            if t == base:
                return True
            t = self.p.types.get(t)
            n += 1
        return False


# ---------------------------------------------------------------- compiler

# W7 (master-authoritative multiplayer): Blizzard.j functions that show text only to GetLocalPlayer() are compiled as
# calls to built-in natives that record the text for every player of the force (tools/jvm/natives.py, U# W3Hud).
BJ_OVERRIDES = ('DisplayTextToForce', 'DisplayTimedTextToForce', 'ClearTextMessagesBJ', 'QuestMessageBJ')


class Compiler:
    def __init__(self, resolve_string=None):
        self.p = Program()
        self.T = Types(self.p)
        self.resolve_string = resolve_string or (lambda s: s)
        self.warnings = []
        self.cur = None
        self.fixups = []

    # -- emit
    def emit(self, op, a=0, b=0, cost=None):
        self.p.ops.append(op)
        self.p.A.append(a)
        self.p.B.append(b)
        self.p.cost.append(cost if cost is not None else 0)
        self.p.line_of_pc.append(self.cur.name if self.cur else '')
        return len(self.p.ops) - 1

    def pc(self):
        return len(self.p.ops)

    def patch(self, at, target):
        self.p.B[at] = target

    # -- declarations
    def add_file(self, jf, origin):
        for t, parent in jf.types.items():
            self.p.types[t] = parent
        for name, (params, ret) in jf.natives.items():
            if name.startswith('Convert') and ret != 'nothing':
                self.p.enum_types.add(ret)
            if name in self.p.native_by_name:
                continue
            self.p.native_by_name[name] = len(self.p.natives)
            self.p.natives.append({'name': name, 'params': [pt for pt, _ in params], 'ret': ret})
        self._pending_globals = getattr(self, '_pending_globals', [])
        for name, (typ, arr, const, init) in jf.globals.items():
            if name in self.p.global_by_name:
                self.warnings.append('global %s redeclared (%s)' % (name, origin))
                continue
            self.p.global_by_name[name] = len(self.p.globals)
            self.p.globals.append({'name': name, 'type': typ, 'array': arr, 'const': const, 'origin': origin})
            if init is not None:
                self._pending_globals.append((name, typ, init))
        self._pending_funcs = getattr(self, '_pending_funcs', [])
        for name, f in jf.functions.items():
            if name in self.p.func_by_name:
                self.warnings.append('function %s redefined (%s)' % (name, origin))
                fi = self.p.functions[self.p.func_by_name[name]]
            else:
                fi = FuncInfo()
                fi.index = len(self.p.functions)
                self.p.func_by_name[name] = fi.index
                self.p.functions.append(fi)
            fi.name = name
            fi.src = f
            fi.constant = f.constant
            fi.param_types = [pt for pt, _ in f.params]
            fi.ret_type = f.returns
            fi.ret_sc = self.T.slot(f.returns)
            self._pending_funcs.append(fi)

    def finish(self):
        # global initialisers first (as pseudo functions), then the bodies
        for name, typ, init in self._pending_globals:
            fi = FuncInfo()
            fi.index = len(self.p.functions)
            fi.name = '<init %s>' % name
            fi.src = None
            fi.constant = False
            fi.param_types = []
            fi.ret_type = 'nothing'
            fi.ret_sc = SC_INT
            fi.nparams = 0
            fi.nlocals = 0
            fi.param_ref = []
            fi.local_arrays = []
            fi.local_types = []
            fi.entry_cost = 0
            self.p.functions.append(fi)
            self.p.ginit.append(fi.index)
            self.cur = fi
            self.locals = {}
            fi.entry = self.pc()
            gi = self.p.global_by_name[name]
            self.expr(init, typ)
            self.emit(STORE_G, gi, int(self.T.refcounted(typ)), COST['store'])
            self.emit(END, 0, 0, COST['end'])
        for fi in self._pending_funcs:
            self.compile_function(fi)
        self.cur = None
        return self.p

    # -- functions
    def compile_function(self, fi):
        f = fi.src
        self.cur = fi
        self.locals = {}
        types = []
        for pt, pn in f.params:
            self.locals[pn] = (len(types), pt, False)
            types.append(pt)
        fi.nparams = len(types)
        fi.param_ref = [self.T.refcounted(t) for t in types]
        arrays = []
        for (lt, arr, ln, _init) in f.locals:
            self.locals[ln] = (len(types), lt, arr)
            if arr:
                arrays.append(len(types))
            types.append(lt)
        fi.nlocals = len(types)
        fi.local_types = types
        fi.local_arrays = arrays
        fi.entry_cost = COST['funcarg'] * fi.nparams + COST['local_decl'] * (fi.nlocals - fi.nparams)
        fi.entry = self.pc()
        for (lt, arr, ln, init) in f.locals:
            if init is not None:
                slot = self.locals[ln][0]
                self.expr(init, lt)
                self.emit(STORE_L, slot, int(self.T.refcounted(lt)), COST['store'])
        self.loop_exits = []
        self.saw_return = False
        self.block(f.body)
        # NodeFunction §Adjacent [obs 0x6f6ad280 / 0x6f456a00]: the default zero return exists only when the
        # body contains at least one return; otherwise the function falls off the end (our VM needs a RET:
        # a non-nothing function that falls off the end returns the class' zero via RET a=2; a nothing function a bare RET).
        self.emit_ret_default()

    def emit_ret_default(self):
        fi = self.cur
        if fi.ret_type != 'nothing':
            if self.saw_return:
                self.push_zero(fi.ret_type)
                self.emit(RET, 1, 0, COST['ret'])
            else:
                # NodeFunction [obs 0x6f456a00]: the extra zero load exists only when the body has a return;
                # the fall-off path still returns (RET a=2: the VM supplies the class' zero).
                self.emit(RET, 2, 0, COST['ret'])
        else:
            self.emit(RET, 0, 0, COST['ret'])

    def push_zero(self, t):
        sc = self.T.slot(t)
        if sc == SC_REAL:
            self.emit(PUSH_R, 0, self.p.real(0.0), 0)
        elif sc == SC_STR:
            self.emit(PUSH_S, 0, -1, 0)
        else:
            self.emit(PUSH_I, 0, 0, 0)

    def block(self, body):
        for s in body:
            self.stmt(s)

    def stmt(self, s):
        k = s[0]
        if k == 'set':
            _, name, idx, e, ln = s
            slot, typ, is_arr, is_local = self.lookup(name, ln)
            ref = int(self.T.refcounted(typ))
            if idx is not None:
                if not is_arr:
                    raise CompileError('line %d: %s is not an array' % (ln, name))
                self.expr(idx, 'integer')
                self.expr(e, typ)
                self.emit(STORE_LA if is_local else STORE_GA, slot, ref, COST['store_array'])
            else:
                self.expr(e, typ)
                self.emit(STORE_L if is_local else STORE_G, slot, ref, COST['store'])
        elif k == 'call':
            _, name, args, ln = s
            rt = self.call(name, args, ln)
            if rt != 'nothing':
                self.emit(POP, 0, 0, COST['pop'])
        elif k == 'if':
            _, branches, els, ln = s
            ends = []
            for cond, body in branches:
                self.expr(cond, 'boolean')
                jz = self.emit(JZ, 0, 0, COST['cjump'])
                self.block(body)
                ends.append(self.emit(JMP, 0, 0, COST['jump']))
                self.patch(jz, self.pc())
            if els:
                self.block(els)
            for j in ends:
                self.patch(j, self.pc())
        elif k == 'loop':
            top = self.pc()
            self.loop_exits.append([])
            self.block(s[1])
            self.emit(JMP, 0, top, COST['jump'])
            for j in self.loop_exits.pop():
                self.patch(j, self.pc())
        elif k == 'exitwhen':
            if not self.loop_exits:
                raise CompileError('line %d: exitwhen outside loop' % s[2])
            self.expr(s[1], 'boolean')
            self.loop_exits[-1].append(self.emit(JNZ, 0, 0, COST['cjump']))
        elif k == 'return':
            _, e, ln = s
            fi = self.cur
            self.saw_return = True
            if fi.ret_type == 'nothing':
                if e is not None:
                    # value is computed (side effects) and dropped: game puts it in a register nobody reads
                    t = self.expr(e, None)
                    self.emit(POP, 0, 0, COST['pop'])
                self.emit(RET, 0, 0, COST['ret'])
            elif e is None:
                self.push_zero(fi.ret_type)
                self.emit(RET, 1, 0, COST['ret'])
            else:
                # NOTES_jass §7: return is NOT type-checked; the raw 32-bit value goes back (return bug)
                if e[0] == 'null':
                    self.expr(e, fi.ret_type)
                    src = fi.ret_sc
                else:
                    src = self.T.slot(self.expr(e, None, convert=False))
                # NodeReturn §1 / NOTES_jass §7: no numeric conversion at the return site; the port's CAST is the
                # raw 32-bit reinterpretation the game's untyped return slot performs (the "return bug").
                if src != fi.ret_sc:
                    self.emit(CAST, src, fi.ret_sc, COST['cast'])
                self.emit(RET, 1, 0, COST['ret'])
        else:
            raise CompileError('bad statement %r' % (k,))

    def lookup(self, name, ln=0):
        if name in self.locals:
            slot, typ, arr = self.locals[name]
            return slot, typ, arr, True
        gi = self.p.global_by_name.get(name)
        if gi is None:
            raise CompileError('line %s: unknown variable %s in %s' % (ln, name, self.cur.name))
        g = self.p.globals[gi]
        return gi, g['type'], g['array'], False

    def call(self, name, args, ln=0):
        fi_ix = self.p.func_by_name.get(name)
        if fi_ix is not None and name in BJ_OVERRIDES:
            fi = self.p.functions[fi_ix]
            if name not in self.p.native_by_name:
                self.p.native_by_name[name] = len(self.p.natives)
                self.p.natives.append({'name': name, 'params': list(fi.param_types), 'ret': fi.ret_type})
            fi_ix = None
        if fi_ix is not None:
            fi = self.p.functions[fi_ix]
            ptypes, rt = fi.param_types, fi.ret_type
        else:
            ni = self.p.native_by_name.get(name)
            if ni is None:
                raise CompileError('line %s: unknown function %s in %s' % (ln, name, self.cur.name))
            ptypes, rt = self.p.natives[ni]['params'], self.p.natives[ni]['ret']
        if len(ptypes) != len(args):
            raise CompileError('line %s: %s takes %d args, got %d' % (ln, name, len(ptypes), len(args)))
        for a, pt in zip(args, ptypes):
            self.expr(a, pt)
        n = len(args)
        cost = COST['call'] + COST['call_arg'] * n + (COST['call_popn'] if n else 0)
        if fi_ix is not None:
            self.emit(CALL_J, fi_ix, n, cost)
        else:
            self.emit(CALL_N, ni, n, cost)
        return rt

    # -- expressions
    def etype(self, e):
        k = e[0]
        if k == 'int':
            return 'integer'
        if k == 'real':
            return 'real'
        if k == 'str':
            return 'string'
        if k == 'bool':
            return 'boolean'
        if k == 'null':
            return NULL_TYPE
        if k == 'fref':
            return 'code'
        if k == 'var':
            return self.lookup(e[1])[1]
        if k == 'idx':
            return self.lookup(e[1])[1]
        if k == 'call':
            fi_ix = self.p.func_by_name.get(e[1])
            if fi_ix is not None:
                return self.p.functions[fi_ix].ret_type
            ni = self.p.native_by_name.get(e[1])
            if ni is None:
                raise CompileError('unknown function %s in %s' % (e[1], self.cur.name))
            return self.p.natives[ni]['ret']
        if k == 'neg':
            return self.etype(e[1])
        if k == 'not':
            return 'boolean'
        if k == 'bin':
            op = e[1]
            if op in ('and', 'or', '==', '!=', '<', '>', '<=', '>='):
                return 'boolean'
            lt, rt = self.etype(e[2]), self.etype(e[3])
            if lt == 'real' or rt == 'real':
                return 'real'
            if op == '+' and (lt == 'string' or rt == 'string'):
                return 'string'
            return lt
        raise CompileError('bad expression %r' % (k,))

    def expr(self, e, want, convert=True):
        """emit code for e; `want` = target type (null typing, int->real node). Returns the type pushed."""
        k = e[0]
        if k == 'int':
            if want == 'real' and convert:
                # game: literal + int->real node; folded here, cost kept
                self.emit(PUSH_R, 0, self.p.real(float(e[1])), COST['literal'] + COST['i2r'])
                return 'real'
            self.emit(PUSH_I, 0, e[1], COST['literal'])
            return 'integer'
        if k == 'real':
            self.emit(PUSH_R, 0, self.p.real(e[1]), COST['literal'])
            return 'real'
        if k == 'str':
            self.emit(PUSH_S, 0, self.p.string(self.resolve_string(e[1])), COST['literal'])
            return 'string'
        if k == 'bool':
            self.emit(PUSH_I, 0, 1 if e[1] else 0, COST['literal'])
            return 'boolean'
        if k == 'null':
            if want == 'string':
                self.emit(PUSH_S, 0, -1, COST['literal'])
            elif want == 'real':
                self.emit(PUSH_R, 0, self.p.real(0.0), COST['literal'])
            else:
                self.emit(PUSH_I, 0, 0, COST['literal'])
            return NULL_TYPE
        if k == 'fref':
            fi_ix = self.p.func_by_name.get(e[1])
            if fi_ix is None:
                raise CompileError('unknown function %s (code ref) in %s' % (e[1], self.cur.name))
            self.emit(PUSH_C, 0, fi_ix + 1, COST['literal'])
            return self._conv('code', want, convert)
        if k == 'var':
            slot, typ, arr, is_local = self.lookup(e[1])
            if arr:
                raise CompileError('array %s used without index in %s' % (e[1], self.cur.name))
            self.emit(LOAD_L if is_local else LOAD_G, slot, 0, COST['load'])
            return self._conv(typ, want, convert)
        if k == 'idx':
            slot, typ, arr, is_local = self.lookup(e[1])
            if not arr:
                raise CompileError('%s is not an array in %s' % (e[1], self.cur.name))
            self.expr(e[2], 'integer')
            self.emit(LOAD_LA if is_local else LOAD_GA, slot, 0, COST['load'])
            return self._conv(typ, want, convert)
        if k == 'call':
            rt = self.call(e[1], e[2])
            return self._conv(rt, want, convert)
        if k == 'neg':
            t = self.expr(e[1], None)
            self.emit(NEG_R if t == 'real' else NEG_I, 0, 0, COST['unary'])
            return self._conv(t, want, convert)
        if k == 'not':
            self.expr(e[1], 'boolean')
            self.emit(NOT, 0, 0, COST['unary'])
            return 'boolean'
        if k == 'bin':
            return self._conv(self.binary(e), want, convert)
        raise CompileError('bad expression %r' % (k,))

    def _conv(self, t, want, convert):
        if convert and want == 'real' and t == 'integer':
            self.emit(I2R, 0, 0, COST['i2r'])
            return 'real'
        return t

    def binary(self, e):
        _, op, a, b = e
        if op in ('and', 'or'):
            # NodeNE §1 [obs 0x6f6ae6b0]: short-circuit — the right operand is not evaluated when the left
            # decides (its call / wait / side effect is skipped). and: false -> false; or: true -> true.
            self.expr(a, 'boolean')
            skip = self.emit(JZ if op == 'and' else JNZ, 0, 0, COST['cjump'])
            self.expr(b, 'boolean')
            end = self.emit(JMP, 0, 0, COST['jump'])
            self.patch(skip, self.pc())
            if op == 'and':
                self.push_zero('boolean')
            else:
                self.emit(PUSH_I, 0, 1, COST['literal'])
            self.patch(end, self.pc())
            return 'boolean'
        lt, rt = self.etype(a), self.etype(b)
        if op in ('+', '-', '*', '/', '<', '>', '<=', '>='):
            if lt == 'real' or rt == 'real':
                self.expr(a, 'real')
                self.expr(b, 'real')
                kind = 'R'
            elif op == '+' and (lt == 'string' or rt == 'string'):
                self.expr(a, 'string')
                self.expr(b, 'string')
                kind = 'S'
            else:
                self.expr(a, None)
                self.expr(b, None)
                kind = 'I'
            name = {'+': 'ADD', '-': 'SUB', '*': 'MUL', '/': 'DIV',
                    '<': 'LT', '>': 'GT', '<=': 'LE', '>=': 'GE'}[op] + '_' + kind
            if name not in OP:
                raise CompileError('operator %s on %s/%s in %s' % (op, lt, rt, self.cur.name))
            self.emit(OP[name], 0, 0, COST['binary'])
            if op in ('<', '>', '<=', '>='):
                return 'boolean'
            return {'R': 'real', 'S': 'string', 'I': lt}[kind]
        # == / !=
        if lt == 'real' or rt == 'real':
            kind = 'R'
            self.expr(a, 'real')
            self.expr(b, 'real')
        elif lt == 'string' or rt == 'string':
            kind = 'S'
            self.expr(a, 'string')
            self.expr(b, 'string')
        elif lt == 'boolean' or rt == 'boolean':
            kind = 'B'
            self.expr(a, 'boolean')
            self.expr(b, 'boolean')
        else:
            kind = 'I'
            self.expr(a, None)
            self.expr(b, None)
        self.emit(OP[('EQ_' if op == '==' else 'NE_') + kind], 0, 0, COST['binary'])
        return 'boolean'


# ---------------------------------------------------------------- driver

def compile_sources(sources, resolve_string=None, prec='game'):
    """sources: list of (origin, text). Returns Program."""
    c = Compiler(resolve_string)
    from .compat import with_compat
    for origin, text in with_compat(sources):
        jf = jass.parse(text, prec)
        c.add_file(jf, origin)
    p = c.finish()
    p.warnings = c.warnings
    return p


def disasm(p, start=0, end=None):
    out = []
    end = len(p.ops) if end is None else end
    for pc in range(start, end):
        op = OPS[p.ops[pc]]
        a, b = p.A[pc], p.B[pc]
        extra = ''
        if op == 'CALL_N':
            extra = p.natives[a]['name']
        elif op == 'CALL_J':
            extra = p.functions[a].name
        elif op == 'PUSH_R':
            extra = repr(p.reals[b])
        elif op == 'PUSH_S' and b >= 0:
            extra = repr(p.strings[b])
        elif op in ('LOAD_G', 'STORE_G', 'LOAD_GA', 'STORE_GA'):
            extra = p.globals[a]['name']
        out.append('%6d  %-9s %6d %11d  c%d %s' % (pc, op, a, b, p.cost[pc], extra))
    return '\n'.join(out)


if __name__ == '__main__':
    srcs = [(os.path.basename(x), open(x, 'rb').read().decode('latin-1')) for x in sys.argv[1:]]
    prog = compile_sources(srcs)
    print('%d instructions, %d functions, %d natives, %d globals, %d reals, %d strings, %d warnings' % (
        len(prog.ops), len(prog.functions), len(prog.natives), len(prog.globals), len(prog.reals),
        len(prog.strings), len(prog.warnings)))
