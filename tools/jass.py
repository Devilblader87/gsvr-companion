#!/usr/bin/env python3
r"""JASS2 tokenizer + recursive-descent parser (clean-room, stdlib only).

Parses common.j, Blizzard.j and compiled map scripts (war3map.j; GUI triggers compile to plain JASS).
Produces a small tuple AST that a later VM / bytecode compiler can consume:

  file   -> JassFile(types, globals, natives, functions)
  expr   -> ('int', v) ('real', v) ('str', s) ('bool', b) ('null',) ('var', name) ('idx', name, e)
            ('call', name, [e]) ('fref', name) ('neg', e) ('not', e) ('bin', op, a, b)
  stmt   -> ('set', name, idx|None, e, line) ('call', name, [e], line) ('if', [(cond, body)], else_body, line)
            ('loop', body, line) ('exitwhen', e, line) ('return', e|None, line)

Precedence (default prec='game', NOTES_jass §9 / NOTES_triggers §8, from the game parser tables), lowest first:
  and (right-assoc) < or (right-assoc) < == != (left) < < > <= >= (left) < + - (left) < * / (left) < not, unary -
  -> `not a == b` = `(not a) == b`, `a or b and c` = `(a or b) and c`.
prec='pjass' keeps the old pjass-like table (and/or one level, left-assoc; == and < one level) for comparison.

Literals (NOTES_jass §7): 'x' = character code (escapes \" \' \\ \b \f \n \r \t, other escapes -> 0,
'\uXXXX' -> 4 hex digits); 'abcd' = big-endian pack of the characters as SIGNED bytes; $ff / 0xff hex;
a leading-0 digit string is read as octal (pjass rule, not yet checked against the game scanner -> Open);
integer overflow wraps to 32 bit (not checked in the game -> Open).
"""
import re
import sys

TOKEN_RE = re.compile(r'''
 (?P<nl>\r\n|\r|\n)
|(?P<ws>[ \t\f\v\x00\xa0]+)
|(?P<comment>//[^\r\n]*)
|(?P<str>"(?:\\.|[^"\\])*")
|(?P<raw>'(?:\\.|[^'\\])*')
|(?P<real>\d+\.\d*|\.\d+)
|(?P<hex>0[xX][0-9a-fA-F]+|\$[0-9a-fA-F]+)
|(?P<int>\d+)
|(?P<id>[A-Za-z_][A-Za-z0-9_]*)
|(?P<op>==|!=|<=|>=|[=<>+\-*/(),\[\]])
''', re.X | re.S)

KEYWORDS = {'globals', 'endglobals', 'type', 'extends', 'native', 'constant', 'function', 'takes', 'returns',
            'nothing', 'endfunction', 'local', 'array', 'set', 'call', 'if', 'then', 'else', 'elseif', 'endif',
            'loop', 'endloop', 'exitwhen', 'return', 'debug', 'and', 'or', 'not', 'true', 'false', 'null'}


class JassError(Exception):
    pass


def tokenize(src):
    toks = []
    pos = 0
    line = 1
    n = len(src)
    m = TOKEN_RE.match
    last_nl = True
    while pos < n:
        mo = m(src, pos)
        if not mo:
            raise JassError('line %d: bad character %r' % (line, src[pos]))
        kind = mo.lastgroup
        text = mo.group()
        pos = mo.end()
        if kind == 'nl':
            if not last_nl:
                toks.append(('nl', None, line))
            last_nl = True
            line += 1
            continue
        if kind in ('ws', 'comment'):
            continue
        last_nl = False
        if kind == 'str':
            toks.append(('str', _unescape(text[1:-1]), line))
            line += text.count('\n') + text.count('\r') - text.count('\r\n')
        elif kind == 'raw':
            toks.append(('int', rawcode(text[1:-1]), line))
        elif kind == 'real':
            toks.append(('real', float(text), line))
        elif kind == 'hex':
            toks.append(('int', _s32(int(text[2:] if text[0] == '0' else text[1:], 16)), line))
        elif kind == 'int':
            if len(text) > 1 and text[0] == '0':
                # NodeGlobal §1 [obs 0x6f6beff0]: a leading-zero literal is always folded in base 8, without a
                # digit check: 0129 is 89 (1*8*8 + 2*8 + 9), not 129.
                v = 0
                for ch in text[1:]:
                    d = ord(ch) - 48
                    v = v * 8 + (d if 0 <= d <= 9 else 0)
            else:
                v = int(text)
            toks.append(('int', _s32(v), line))
        elif kind == 'id':
            toks.append(('kw' if text in KEYWORDS else 'id', text, line))
        else:
            toks.append(('op', text, line))
    toks.append(('nl', None, line))
    toks.append(('eof', None, line))
    return toks


_RAW_ESC = {'"': 34, "'": 39, '\\': 92, 'b': 8, 'f': 12, 'n': 10, 'r': 13, 't': 9}


def rawcode(body):
    """value of a character literal body (between the quotes), NOTES_jass §7."""
    if body.startswith('\\'):
        if len(body) == 6 and body[1] == 'u':
            try:
                return int(body[2:], 16)
            except ValueError:
                return 0
        if len(body) == 2:
            return _RAW_ESC.get(body[1], 0)
    v = 0
    for ch in body.encode('latin-1', 'replace'):
        sb = ch - 256 if ch >= 0x80 else ch          # signed byte
        v = v * 256 + sb
    return _s32(v)


def _s32(v):
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v & 0x80000000 else v


_ESC = {'n': '\n', 'r': '\r', 't': '\t', 'b': '\b', 'f': '\f', '"': '"', '\\': '\\', "'": "'"}


def _unescape(s):
    if '\\' not in s:
        return s
    out = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == '\\' and i + 1 < len(s):
            out.append(_ESC.get(s[i + 1], s[i + 1]))
            i += 2
        else:
            out.append(c)
            i += 1
    return ''.join(out)


class Function:
    __slots__ = ('name', 'params', 'returns', 'locals', 'body', 'constant', 'line', 'nstmts')

    def __repr__(self):
        return '<function %s>' % self.name


class JassFile:
    def __init__(self):
        self.types = {}        # name -> parent
        self.globals = {}      # name -> (type, is_array, constant, init_expr)
        self.natives = {}      # name -> (params, returns)
        self.functions = {}    # name -> Function
        self.lines = 0


class Parser:
    def __init__(self, toks, prec='game'):
        self.t = toks
        self.i = 0
        if prec not in ('game', 'pjass'):
            raise ValueError(prec)
        self.prec = prec

    # -- helpers
    def peek(self, k=0):
        return self.t[self.i + k]

    def next(self):
        tok = self.t[self.i]
        self.i += 1
        return tok

    def at(self, kind, val=None):
        tk = self.t[self.i]
        return tk[0] == kind and (val is None or tk[1] == val)

    def accept(self, kind, val=None):
        if self.at(kind, val):
            return self.next()
        return None

    def expect(self, kind, val=None):
        tk = self.next()
        if tk[0] != kind or (val is not None and tk[1] != val):
            raise JassError('line %d: expected %s %s, got %s %r' % (tk[2], kind, val or '', tk[0], tk[1]))
        return tk

    def name(self):
        tk = self.next()
        if tk[0] == 'id' or (tk[0] == 'kw' and tk[1] in ('nothing',)):
            return tk[1]
        raise JassError('line %d: expected name, got %r' % (tk[2], tk[1]))

    def eol(self):
        if not self.accept('nl') and not self.at('eof'):
            tk = self.peek()
            raise JassError('line %d: expected end of line, got %r' % (tk[2], tk[1]))

    def skip_nl(self):
        while self.accept('nl'):
            pass

    # -- top level
    def parse_file(self):
        jf = JassFile()
        self.skip_nl()
        while not self.at('eof'):
            if self.accept('kw', 'type'):
                n = self.name()
                self.expect('kw', 'extends')
                jf.types[n] = self.name()
                self.eol()
            elif self.accept('kw', 'globals'):
                self.eol()
                self.parse_globals(jf)
            elif self.at('kw', 'constant') and self.peek(1)[1] == 'native' or self.at('kw', 'native'):
                self.accept('kw', 'constant')
                self.expect('kw', 'native')
                n = self.name()
                params, ret = self.parse_sig()
                jf.natives[n] = (params, ret)
                self.eol()
            elif self.at('kw', 'constant') or self.at('kw', 'function'):
                f = self.parse_function()
                jf.functions[f.name] = f
            else:
                tk = self.peek()
                raise JassError('line %d: unexpected %r at top level' % (tk[2], tk[1]))
            self.skip_nl()
        jf.lines = self.t[-1][2]
        return jf

    def parse_globals(self, jf):
        while True:
            self.skip_nl()
            if self.accept('kw', 'endglobals'):
                self.eol()
                return
            const = bool(self.accept('kw', 'constant'))
            typ = self.name()
            arr = bool(self.accept('kw', 'array'))
            n = self.name()
            init = None
            if self.accept('op', '='):
                init = self.expr()
            jf.globals[n] = (typ, arr, const, init)
            self.eol()

    def parse_sig(self):
        self.expect('kw', 'takes')
        params = []
        if not self.accept('kw', 'nothing'):
            while True:
                pt = self.name()
                pn = self.name()
                params.append((pt, pn))
                if not self.accept('op', ','):
                    break
        self.expect('kw', 'returns')
        ret = self.name()
        return params, ret

    def parse_function(self):
        f = Function()
        f.constant = bool(self.accept('kw', 'constant'))
        tk = self.expect('kw', 'function')
        f.line = tk[2]
        f.name = self.name()
        f.params, f.returns = self.parse_sig()
        self.eol()
        f.locals = []
        self.nst = 0
        while True:
            self.skip_nl()
            if self.accept('kw', 'local'):
                typ = self.name()
                arr = bool(self.accept('kw', 'array'))
                n = self.name()
                init = self.expr() if self.accept('op', '=') else None
                f.locals.append((typ, arr, n, init))
                self.nst += 1 if init is not None else 0
                self.eol()
            else:
                break
        f.body = self.block(('endfunction',))
        self.expect('kw', 'endfunction')
        self.eol()
        f.nstmts = self.nst
        return f

    def block(self, enders):
        body = []
        while True:
            self.skip_nl()
            tk = self.peek()
            if tk[0] == 'kw' and tk[1] in enders:
                return body
            if tk[0] == 'eof':
                raise JassError('unexpected end of file (missing %s)' % enders[0])
            body.append(self.stmt())

    def stmt(self):
        self.accept('kw', 'debug')
        tk = self.next()
        ln = tk[2]
        self.nst += 1
        if tk == ('kw', 'set', ln):
            n = self.name()
            idx = None
            if self.accept('op', '['):
                idx = self.expr()
                self.expect('op', ']')
            self.expect('op', '=')
            e = self.expr()
            self.eol()
            return ('set', n, idx, e, ln)
        if tk[0] == 'kw':
            k = tk[1]
            if k == 'call':
                n = self.name()
                args = self.args()
                self.eol()
                return ('call', n, args, ln)
            if k == 'if':
                branches = []
                cond = self.expr()
                self.expect('kw', 'then')
                self.eol()
                body = self.block(('elseif', 'else', 'endif'))
                branches.append((cond, body))
                els = None
                while True:
                    if self.accept('kw', 'elseif'):
                        cond = self.expr()
                        self.expect('kw', 'then')
                        self.eol()
                        branches.append((cond, self.block(('elseif', 'else', 'endif'))))
                    elif self.accept('kw', 'else'):
                        self.eol()
                        els = self.block(('endif',))
                    else:
                        self.expect('kw', 'endif')
                        self.eol()
                        return ('if', branches, els, ln)
            if k == 'loop':
                self.eol()
                body = self.block(('endloop',))
                self.expect('kw', 'endloop')
                self.eol()
                return ('loop', body, ln)
            if k == 'exitwhen':
                e = self.expr()
                self.eol()
                return ('exitwhen', e, ln)
            if k == 'return':
                e = None if self.at('nl') else self.expr()
                self.eol()
                return ('return', e, ln)
        raise JassError('line %d: unexpected %r' % (ln, tk[1]))

    def args(self):
        self.expect('op', '(')
        out = []
        if not self.accept('op', ')'):
            while True:
                out.append(self.expr())
                if self.accept('op', ')'):
                    break
                self.expect('op', ',')
        return out

    # -- expressions
    def expr(self):
        if self.prec == 'pjass':
            a = self.cmp_pjass()
            while self.at('kw', 'and') or self.at('kw', 'or'):
                op = self.next()[1]
                a = ('bin', op, a, self.cmp_pjass())
            return a
        return self.and_expr()

    def and_expr(self):                 # lowest, right-associative
        a = self.or_expr()
        if self.accept('kw', 'and'):
            return ('bin', 'and', a, self.and_expr())
        return a

    def or_expr(self):                  # binds tighter than and, right-associative
        a = self.eq_expr()
        if self.accept('kw', 'or'):
            return ('bin', 'or', a, self.or_expr())
        return a

    def eq_expr(self):
        a = self.rel_expr()
        while self.peek()[0] == 'op' and self.peek()[1] in ('==', '!='):
            op = self.next()[1]
            a = ('bin', op, a, self.rel_expr())
        return a

    def rel_expr(self):
        a = self.add()
        while self.peek()[0] == 'op' and self.peek()[1] in ('<', '>', '<=', '>='):
            op = self.next()[1]
            a = ('bin', op, a, self.add())
        return a

    def cmp_pjass(self):
        a = self.add()
        while self.peek()[0] == 'op' and self.peek()[1] in ('==', '!=', '<', '>', '<=', '>='):
            op = self.next()[1]
            a = ('bin', op, a, self.add())
        return a

    def add(self):
        a = self.mul()
        while self.peek()[0] == 'op' and self.peek()[1] in ('+', '-'):
            op = self.next()[1]
            a = ('bin', op, a, self.mul())
        return a

    def mul(self):
        a = self.unary()
        while self.peek()[0] == 'op' and self.peek()[1] in ('*', '/'):
            op = self.next()[1]
            a = ('bin', op, a, self.unary())
        return a

    def unary(self):
        if self.accept('op', '-'):
            return ('neg', self.unary())
        if self.accept('op', '+'):
            return self.unary()
        if self.accept('kw', 'not'):
            return ('not', self.unary())
        return self.atom()

    def atom(self):
        tk = self.next()
        k, v, ln = tk
        if k == 'int':
            return ('int', v)
        if k == 'real':
            return ('real', v)
        if k == 'str':
            return ('str', v)
        if k == 'kw':
            if v == 'true':
                return ('bool', True)
            if v == 'false':
                return ('bool', False)
            if v == 'null':
                return ('null',)
            if v == 'function':
                return ('fref', self.name())
        if k == 'op' and v == '(':
            e = self.expr()
            self.expect('op', ')')
            return e
        if k == 'id':
            if self.at('op', '('):
                return ('call', v, self.args())
            if self.accept('op', '['):
                e = self.expr()
                self.expect('op', ']')
                return ('idx', v, e)
            return ('var', v)
        raise JassError('line %d: unexpected %r in expression' % (ln, v))


def parse(src, prec='game'):
    return Parser(tokenize(src), prec).parse_file()


def parse_expr(src, prec='game'):
    """parse a single expression (tests / tools)."""
    p = Parser(tokenize(src), prec)
    e = p.expr()
    p.eol()
    return e


def parse_file(path, prec='game'):
    with open(path, 'rb') as fh:
        return parse(fh.read().decode('latin-1'), prec)


# ---------------------------------------------------------------- walkers

def walk_expr(e, fn):
    fn(e)
    k = e[0]
    if k == 'idx' or k in ('neg', 'not'):
        walk_expr(e[-1], fn)
    elif k == 'call':
        for a in e[2]:
            walk_expr(a, fn)
    elif k == 'bin':
        walk_expr(e[2], fn)
        walk_expr(e[3], fn)


def walk_stmts(body, sfn, efn):
    for s in body:
        sfn(s)
        k = s[0]
        if k == 'set':
            if s[2] is not None:
                walk_expr(s[2], efn)
            walk_expr(s[3], efn)
        elif k == 'call':
            efn(('call', s[1], s[2]))
            for a in s[2]:
                walk_expr(a, efn)
        elif k == 'if':
            for cond, b in s[1]:
                walk_expr(cond, efn)
                walk_stmts(b, sfn, efn)
            if s[2]:
                walk_stmts(s[2], sfn, efn)
        elif k == 'loop':
            walk_stmts(s[1], sfn, efn)
        elif k in ('exitwhen',):
            walk_expr(s[1], efn)
        elif k == 'return' and s[1] is not None:
            walk_expr(s[1], efn)


def function_refs(f):
    """-> (calls: list of names incl. duplicates, frefs: set, strings: list)"""
    calls, frefs, strs = [], set(), []

    def efn(e):
        k = e[0]
        if k == 'call':
            calls.append(e[1])
            if e[1] == 'ExecuteFunc' and e[2] and e[2][0][0] == 'str':
                strs.append(e[2][0][1])
        elif k == 'fref':
            frefs.add(e[1])

    for (_t, _a, _n, init) in f.locals:
        if init is not None:
            walk_expr(init, efn)
    walk_stmts(f.body, lambda s: None, efn)
    return calls, frefs, strs


if __name__ == '__main__':
    for p in sys.argv[1:]:
        jf = parse_file(p)
        print('%s: %d lines, %d types, %d globals, %d natives, %d functions' % (
            p, jf.lines, len(jf.types), len(jf.globals), len(jf.natives), len(jf.functions)))
