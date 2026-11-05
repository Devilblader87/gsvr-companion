#!/usr/bin/env python3
"""Reference JASS VM: interpreter + threads + handle table + trigger/timer runtime + clocks.

Every rule cites docs/NOTES_jass.md (J§) or docs/NOTES_triggers.md (T§). Natives live in natives.py; this module
only has the VM core and the trigger machinery the natives call into.
"""
import heapq
import math
import os
import struct

from . import compiler as C

OP_LIMIT = 300000            # J§4: budget per run, always 300 000
HANDLE_BASE = 0x100000       # J§5: first handle id
WAIT_MIN = 0.0001            # T§1.1: CAgentTimer clamp
REAL_EPS = C.f32(0.001)      # J§3 op 0x1a
STEP = 0.005                 # T§1.2: game step 5 ms
TURN_REAL = 0.025            # one 25 ms turn of real time
WAIT_GRID = os.environ.get('W3_WAIT_GRID') != '0'   # [obs runtime 1.26a] docs/oracle.md "TriggerSleepAction(d)": a wait resumes on the 0.1 s game-time grid, d + 0.1 .. 0.2 s
                                                    # later (0.0 -> 0.2, 0.5 -> 0.7); W3_WAIT_GRID=0 = the old model (d + one 25 ms turn). The twin is W3Vm.waitGrid.
WAIT_GRID_STEPS = 20         # the 0.1 s grid in 5 ms game steps
# PORT14-Game: [obs runtime 1.26a, ext_determinism] a periodic timer started before a one-shot fires first
# at an equal due time, so its periodic re-arm keeps the original queue sequence (the decompiled insert is
# FIFO on the sequence it is given). Default ON, the 1.26a reference; W3_TIMER_TIE_START_ORDER=0 keeps the
# old fresh-sequence re-arm (the pre-PORT14 shipped behaviour / golden traces).
TIMER_TIE_START_ORDER = os.environ.get('W3_TIMER_TIE_START_ORDER') != '0'
STEPS_PER_TURN = {'slow': 3, 'normal': 4, 'fast': 5}
TIME_EPS = 1e-6              # due <= clock + 1 us counts as due (float noise of the 5 ms grid; Open)

# thread statuses (J§4)
ST_DONE, ST_LIMIT, ST_WAIT, ST_SUSPEND, ST_NOFUNC, ST_UNSET, ST_DIV0 = 1, 2, 3, 4, 5, 6, 7
ST_DEPTH, ST_NATIVE = 8, 9      # AI host only (aivm): call depth / value stack cap hit; a native that needed tasks
STATUS_NAME = {1: 'done', 2: 'oplimit', 3: 'wait', 4: 'suspend', 5: 'nofunc', 6: 'unset', 7: 'div0', 8: 'depth',
               9: 'native'}
DEPTH_FRAMES = 1024             # AI host: max call frames of one thread (== W3Vm AV_MAXFRAMES)
DEPTH_STACK = 16384             # AI host: max value-stack slots (locals of all frames + operands) (== AV_MAXSTACK)

# context slots (T§4)
CTX_TRIGGER_UNIT, CTX_TRIGGER_WIDGET, CTX_TRIGGER, CTX_TRIGGER_PLAYER, CTX_SOURCE, CTX_EXPIRED_TIMER, \
    CTX_EVENT_ID, CTX_FILTER_UNIT, CTX_FILTER_PLAYER, CTX_FILTER_DESTR, CTX_ENUM_UNIT, CTX_ENUM_PLAYER, \
    CTX_ENUM_DESTR, CTX_ENUM_ITEM, CTX_FILTER_ITEM, CTX_REVIVABLE = range(16)   # W9R2-2: CTX_REVIVABLE = GetRevivableUnit
NCTX = 16


class _Unset:
    __slots__ = ()

    def __repr__(self):
        return 'UNSET'


UNSET = _Unset()

_F = struct.Struct('<f')
_I = struct.Struct('<i')


def r32(x):
    """round to float32 (every real op, J§3 / T§0 soft float ~ IEEE single)."""
    try:
        return _F.unpack(_F.pack(x))[0]
    except OverflowError:
        return float('inf') if x > 0 else float('-inf')


def rbits(x):
    try:
        return _I.unpack(_F.pack(x))[0]
    except OverflowError:
        return 0x7f800000 if x > 0 else -0x800000


def bits2r(i):
    return _F.unpack(_I.pack(i))[0]


def s32(v):
    return ((v + 0x80000000) & 0xFFFFFFFF) - 0x80000000


def r2i(x):
    """R2I [obs runtime 1.26a docs/oracle.md "R2I out of range"]: truncation that WRAPS modulo 2^32 (3e9 -> -1294967296,
    -3e9 -> 1294967296, 2^32 -> 0, 1e10 -> 1410065408), no saturation. NaN -> 0x80000000 (unobserved, as before); |x| >= 2^63
    -> 0 [inf: the 64-bit conversion's indefinite value 0x8000000000000000, low word 0]. The twin is W3World.TruncI."""
    if x != x:
        return -0x80000000
    if x >= 9223372036854775808.0 or x <= -9223372036854775808.0:
        return 0
    return s32(int(x))


class JArray(dict):
    __slots__ = ('z',)


# ---------------------------------------------------------------- handles (J§5)

class Agent:
    """base of every world object that can get a handle id."""
    hid = 0
    alive = True
    tname = 'handle'


class HandleTable:
    def __init__(self):
        self.slots = []          # index = id - HANDLE_BASE -> [obj, refcount] or None
        self.free = []           # LIFO free list
        self.allocs = 0
        self.frees = 0

    def id_of(self, obj):
        """J§5: existing id, else last freed id, else new id 0x100000 + table size."""
        if obj is None:
            return 0
        if obj.hid:
            return obj.hid
        if self.free:
            i = self.free.pop()
            self.slots[i] = [obj, 0]
        else:
            i = len(self.slots)
            self.slots.append([obj, 0])
        obj.hid = HANDLE_BASE + i
        self.allocs += 1
        return obj.hid

    def get(self, hid):
        i = hid - HANDLE_BASE
        if 0 <= i < len(self.slots):
            e = self.slots[i]
            if e is not None:
                return e[0]
        return None

    def addref(self, hid):
        i = hid - HANDLE_BASE
        if 0 <= i < len(self.slots):
            e = self.slots[i]
            if e is not None:
                e[1] += 1

    def release(self, hid):
        i = hid - HANDLE_BASE
        if 0 <= i < len(self.slots):
            e = self.slots[i]
            if e is not None:
                e[1] -= 1
                if e[1] <= 0 and not e[0].alive:
                    self._free(i)

    def destroyed(self, obj):
        """object destroyed: the id stays taken while VM references remain (J§5)."""
        obj.alive = False
        if obj.hid:
            i = obj.hid - HANDLE_BASE
            e = self.slots[i]
            if e is not None and e[1] <= 0:
                self._free(i)

    def _free(self, i):
        self.slots[i] = None
        self.free.append(i)
        self.frees += 1

    def live(self):
        return len(self.slots) - len(self.free)

    def refcount(self, hid):
        i = hid - HANDLE_BASE
        if 0 <= i < len(self.slots) and self.slots[i] is not None:
            return self.slots[i][1]
        return None


# ---------------------------------------------------------------- threads

class Thread:
    __slots__ = ('id', 'kind', 'stack', 'frames', 'pc', 'func', 'locs', 'result', 'wait_req', 'wait_child',
                 'status', 'execution', 'ops')

    def __init__(self, tid, kind):
        self.id = tid
        self.kind = kind
        self.stack = []
        self.frames = []
        self.pc = 0
        self.func = -1
        self.locs = None
        self.result = None
        self.wait_req = None
        self.wait_child = None
        self.status = 0
        self.execution = None
        self.ops = 0


class Timer(Agent):
    tname = 'timer'

    def __init__(self):
        self.code = 0
        self.timeout = 0.0
        self.periodic = False
        self.running = False
        self.due = 0.0
        self.period = 0.0
        self.saved_remaining = 0.0
        self.resumed = False     # 0x80272: this schedule is a ResumeTimer fire (re-arm once, §6.2)
        self.node = None
        self.regs = []           # TriggerRegisterTimerExpireEvent registrations
        self.internal = False


class TimerQueue:
    """binary heap by due time, ties FIFO by sequence number (T§1.2)."""

    def __init__(self, name):
        self.name = name
        self.clock = 0.0
        self.heap = []
        self.seq = 0

    def add(self, delay, fn):
        self.seq += 1
        node = [self.clock + delay, self.seq, fn, True]
        heapq.heappush(self.heap, node)
        return node

    def advance(self, new_clock):
        h = self.heap
        while h and h[0][0] <= new_clock + TIME_EPS:
            node = heapq.heappop(h)
            if not node[3]:
                continue
            node[3] = False
            self.clock = node[0]
            node[2](node)
        self.clock = new_clock


# ---------------------------------------------------------------- trigger objects (T§0, T§2)

class Trigger(Agent):
    tname = 'trigger'

    def __init__(self):
        self.events = []
        self.conds = []          # TriggerCondition objects
        self.actions = []        # TriggerAction objects
        self.enabled = True
        self.evalc = 0
        self.execc = 0
        self.wait_on_sleeps = False   # T§1.5 default false
        self.live_execs = 0


class TriggerCondition(Agent):
    tname = 'triggercondition'

    def __init__(self, bx):
        self.bx = bx


class TriggerAction(Agent):
    tname = 'triggeraction'

    def __init__(self, code):
        self.code = code


class BoolExpr(Agent):
    tname = 'boolexpr'

    def __init__(self, kind, code=0, a=None, b=None):
        self.kind = kind         # 'cond' | 'filter' | 'and' | 'or' | 'not'
        self.code = code
        self.a = a
        self.b = b


class Execution:
    """one per trigger run (T§0 CTriggerExecution)."""
    __slots__ = ('trig', 'actions', 'thread', 'idx', 'ctx', 'evd', 'caller_waits', 'wait_on_sleeps',
                 'one_shot', 'seq', 'children', 'parent', 'done', 'id', 'suspended')

    def __init__(self):
        self.thread = None
        self.idx = 0
        self.seq = 0
        self.children = []
        self.parent = None
        self.done = False
        self.suspended = False


# ---------------------------------------------------------------- VM

class VM:
    def __init__(self, prog, speed='fast', latency_turns=1, trace=None, op_limit=OP_LIMIT):
        self.p = prog
        self.ops, self.A, self.B, self.cost = prog.ops, prog.A, prog.B, prog.cost
        self.reals = prog.reals
        self.strings = prog.strings
        self.op_limit = op_limit
        self.depth_cap = False                 # AI host sets it: CALL_J past DEPTH_FRAMES / DEPTH_STACK ends the thread
        self.H = HandleTable()
        self.G = [UNSET] * len(prog.globals)
        self.var_watch = {}                    # W9: global index -> variable-event regs (natives._regvarevent)
        for i, g in enumerate(prog.globals):
            if g['array']:
                self.G[i] = self._new_array(g['type'])
        self.ctx = [[] for _ in range(NCTX)]
        self.last_filter_unit = 0      # CFilterFunc: the last unit a filter was evaluated on (GetFilterUnit stays valid)
        self.evdata = []
        self.thread_stack = []
        self.exec_stack = []
        self.next_tid = 0
        self.next_exid = 0
        self.qgame = TimerQueue('game')
        self.qreal = TimerQueue('real')
        self.steps_per_turn = STEPS_PER_TURN[speed]
        self.latency_turns = latency_turns
        self.turn = 0
        self.steps = 0
        self.turn_step0 = 0      # game steps at the start of the current turn (the wait grid)
        self.wait_grid = WAIT_GRID   # per VM: a test scenario timed under the old wait model sets it False (the rule-test exporter writes a .oldwait marker)
        self.commands = []       # (turn, seq, fn) resume messages (T§1.1 type 0x62)
        self.cmd_seq = 0
        self.trace = trace       # file-like or None
        self.str_ids = {}
        self.str_by_id = [None]
        # statistics
        self.total_ops = 0
        self.native_counts = {}
        self.unknown_counts = {}
        self.threads_created = 0
        self.thread_kinds = {}
        self.status_counts = {}
        self.max_concurrent_waits = 0
        self.waiting = set()
        self.ops_by_sec = {}     # game second -> counted instructions (statistics)
        self.natives = None      # bound by natives.bind()
        self.nat_ret = [n['ret'] != 'nothing' for n in prog.natives]
        self.world = None
        self.native_zero = []
        self.paused = False
        self.on_step = []        # world update callbacks(dt)
        self.on_turn = []        # turn-start callbacks(turn), after the resume commands (player inputs)
        self.errors = []

    # ---------------------------------------------------------- helpers
    def _new_array(self, typ):
        a = JArray()
        sc = C.Types(self.p).slot(typ)
        a.z = 0.0 if sc == C.SC_REAL else (None if sc == C.SC_STR else 0)
        return a

    def h(self, obj):
        return self.H.id_of(obj)

    def obj(self, hid, cls=None):
        if not hid:
            return None
        o = self.H.get(hid)
        if o is not None and not o.alive:       # destroyed object: id may still be referenced -> invalid
            return None
        if cls is not None and o is not None and not isinstance(o, cls):
            return None
        return o

    def game_ms(self):
        """trace time: game time of the current 5 ms step (callbacks inside a step report that step)."""
        return self.steps * 5

    def global_value(self, name):
        return self.G[self.p.global_by_name[name]]

    def cur_thread(self):
        return self.thread_stack[-1] if self.thread_stack else None

    def cur_exec(self):
        return self.exec_stack[-1] if self.exec_stack else None

    def str_id(self, s):
        if s is None:
            return 0
        i = self.str_ids.get(s)
        if i is None:
            i = len(self.str_by_id)
            self.str_ids[s] = i
            self.str_by_id.append(s)
        return i

    # ---------------------------------------------------------- context stacks (T§4)
    def ctx_push(self, slot, v):
        if v >= HANDLE_BASE:
            self.H.addref(v)
        self.ctx[slot].append(v)

    def ctx_pop(self, slot):
        v = self.ctx[slot].pop()
        if v >= HANDLE_BASE:
            self.H.release(v)

    def ctx_top(self, slot):
        s = self.ctx[slot]
        return s[-1] if s else 0

    def ev(self, key, default=0):
        """event data of the current event (GetEnteringUnit, GetDyingUnit, ...)."""
        d = self.evdata[-1] if self.evdata else None
        if d is None:
            return default
        return d.get(key, default)

    # ---------------------------------------------------------- threads
    def new_thread(self, kind):
        self.next_tid += 1
        self.threads_created += 1
        self.thread_kinds[kind] = self.thread_kinds.get(kind, 0) + 1
        return Thread(self.next_tid, kind)

    def start_func(self, th, fidx):
        """prepare th to run function fidx with no arguments (J§8 / T§7: no argument values are passed;
        parameters stay unset -> reading them ends the thread, Open)."""
        fi = self.p.functions[fidx]
        locs = [UNSET] * fi.nlocals
        for s in fi.local_arrays:
            locs[s] = self._new_array(fi.local_types[s])
        th.locs = locs
        th.func = fidx
        th.pc = fi.entry
        th.frames = []
        th.stack = []
        th.ops = fi.entry_cost

    def run_code(self, code, kind):
        """new thread for a code value, run to completion or suspension. Returns (status, result, thread)."""
        th = self.new_thread(kind)
        if code <= 0 or code > len(self.p.functions):
            th.status = ST_NOFUNC
            self._count_status(ST_NOFUNC)
            return ST_NOFUNC, None, th
        self.start_func(th, code - 1)
        st = self.run(th)
        return st, th.result, th

    def _count_status(self, st):
        self.status_counts[st] = self.status_counts.get(st, 0) + 1

    # ---------------------------------------------------------- interpreter
    def run(self, th, budget=None):
        """run th for at most `budget` (default 300 000) counted game instructions (J§4). Returns status."""
        if budget is None:
            budget = self.op_limit
        ops, A, B, cost = self.ops, self.A, self.B, self.cost
        reals, strings = self.reals, self.strings
        G = self.G
        H = self.H
        VW = self.var_watch
        dcap = self.depth_cap
        funcs = self.p.functions
        natives = self.natives
        nat_ret = self.nat_ret
        stack = th.stack
        push = stack.append
        pop = stack.pop
        locs = th.locs
        pc = th.pc
        cur_f = th.func
        frames = th.frames
        count = th.ops
        th.ops = 0
        th.wait_req = None
        th.wait_child = None
        self.thread_stack.append(th)
        status = 0
        try:
            while True:
                op = ops[pc]
                count += cost[pc]
                if count > budget:
                    status = ST_LIMIT
                    break
                if op == 5:                                   # LOAD_L
                    v = locs[A[pc]]
                    if v is UNSET:
                        status = ST_UNSET
                        break
                    push(v)
                    pc += 1
                elif op == 6:                                 # LOAD_G
                    v = G[A[pc]]
                    if v is UNSET:
                        status = ST_UNSET
                        break
                    push(v)
                    pc += 1
                elif op == 1:                                 # PUSH_I
                    push(B[pc])
                    pc += 1
                elif op == 13:                                # CALL_N
                    n = B[pc]
                    if n:
                        args = stack[-n:]
                        del stack[-n:]
                    else:
                        args = []
                    th.pc = pc
                    a = A[pc]
                    res = natives[a](args)
                    if nat_ret[a]:
                        push(res)
                    pc += 1
                    if th.wait_req is not None or th.wait_child is not None:
                        status = ST_WAIT if th.wait_child is None else ST_SUSPEND
                        break
                elif op == 17:                                # JZ
                    if not pop():
                        pc = B[pc]
                    else:
                        pc += 1
                elif op == 14:                                # CALL_J
                    fi = funcs[A[pc]]
                    n = B[pc]
                    nl = fi.nlocals
                    if dcap and (len(frames) >= DEPTH_FRAMES or len(stack) - n + len(locs) + nl
                                 + sum([len(f[2]) for f in frames]) > DEPTH_STACK):
                        status = ST_DEPTH
                        break
                    if n:
                        new = stack[-n:]
                        del stack[-n:]
                        pr = fi.param_ref
                        for i in range(n):                    # op 0x08 binds = store -> add-ref (J§5)
                            if pr[i]:
                                v = new[i]
                                if v >= HANDLE_BASE:
                                    H.addref(v)
                        if nl > n:
                            new.extend([UNSET] * (nl - n))
                    else:
                        new = [UNSET] * nl
                    for s in fi.local_arrays:
                        new[s] = self._new_array(fi.local_types[s])
                    frames.append((cur_f, pc + 1, locs))
                    locs = new
                    cur_f = fi.index
                    pc = fi.entry
                    count += fi.entry_cost
                elif op == 15:                                # RET
                    a_ret = A[pc]
                    if a_ret == 1:
                        v = pop()
                    elif a_ret == 2:                          # NodeFunction fall-off: return the class' zero
                        v = 0.0 if funcs[cur_f].ret_sc == 1 else (None if funcs[cur_f].ret_sc == 2 else 0)
                    else:
                        v = None
                    fi = funcs[cur_f]
                    pr = fi.param_ref
                    for i in range(fi.nparams):               # only parameters are released (J§5 leak)
                        if pr[i]:
                            pv = locs[i]
                            if pv is not UNSET and pv >= HANDLE_BASE:
                                H.release(pv)
                    if not frames:
                        th.result = v
                        status = ST_DONE
                        break
                    has = 1 if a_ret else 0
                    cur_f, pc, locs = frames.pop()
                    if has:
                        push(v)
                elif op == 16:                                # JMP
                    pc = B[pc]
                elif op == 18:                                # JNZ
                    if pop():
                        pc = B[pc]
                    else:
                        pc += 1
                elif op == 9:                                 # STORE_L
                    v = pop()
                    a = A[pc]
                    if B[pc]:
                        if v >= HANDLE_BASE:
                            H.addref(v)
                        old = locs[a]
                        if old is not UNSET and old >= HANDLE_BASE:
                            H.release(old)
                    locs[a] = v
                    pc += 1
                elif op == 10:                                # STORE_G
                    v = pop()
                    a = A[pc]
                    if B[pc]:
                        if v >= HANDLE_BASE:
                            H.addref(v)
                        old = G[a]
                        if old is not UNSET and old >= HANDLE_BASE:
                            H.release(old)
                    G[a] = v
                    pc += 1
                    if a in VW:                               # TriggerRegisterVariableEvent: after the store
                        th.pc = pc - 1
                        self.var_stored(a, v)
                elif op == 36:                                # EQ_I
                    b = pop()
                    push(1 if pop() == b else 0)
                    pc += 1
                elif op == 3:                                 # PUSH_S
                    b = B[pc]
                    push(strings[b] if b >= 0 else None)
                    pc += 1
                elif op == 2:                                 # PUSH_R
                    push(reals[B[pc]])
                    pc += 1
                elif op == 19:                                # POP
                    pop()
                    pc += 1
                elif op == 21:                                # ADD_I
                    b = pop()
                    v = pop() + b
                    if v > 2147483647 or v < -2147483648:
                        v = ((v + 0x80000000) & 0xFFFFFFFF) - 0x80000000
                    push(v)
                    pc += 1
                elif op == 8 or op == 7:                      # LOAD_GA / LOAD_LA
                    i = pop()
                    arr = G[A[pc]] if op == 8 else locs[A[pc]]
                    push(arr.get(i, arr.z))
                    pc += 1
                elif op == 12 or op == 11:                    # STORE_GA / STORE_LA
                    v = pop()
                    i = pop()
                    arr = G[A[pc]] if op == 12 else locs[A[pc]]
                    if 0 <= i < C.JASS_ARRAY_SIZE:
                        if B[pc]:
                            if v >= HANDLE_BASE:
                                H.addref(v)
                            old = arr.get(i, 0)
                            if old >= HANDLE_BASE:
                                H.release(old)
                        arr[i] = v
                    pc += 1
                elif op == 4:                                 # PUSH_C
                    push(B[pc])
                    pc += 1
                elif op == 20:                                # I2R
                    stack[-1] = r32(float(stack[-1]))
                    pc += 1
                elif op == 33:                                # NOT: value < 1 (J§3 0x26)
                    stack[-1] = 1 if stack[-1] < 1 else 0
                    pc += 1
                elif op == 34:                                # AND (both evaluated, J§3 0x18)
                    b = pop()
                    a = pop()
                    push(1 if (a and b) else 0)
                    pc += 1
                elif op == 35:                                # OR
                    b = pop()
                    a = pop()
                    push(1 if (a or b) else 0)
                    pc += 1
                elif op == 40:                                # NE_I
                    b = pop()
                    push(1 if pop() != b else 0)
                    pc += 1
                elif op == 22:                                # ADD_R
                    b = pop()
                    push(r32(pop() + b))
                    pc += 1
                elif op == 23:                                # ADD_S (concat)
                    b = pop()
                    a = pop()
                    if a is None:
                        push(b)
                    elif b is None:
                        push(a)
                    else:
                        push(a + b)
                    pc += 1
                elif op == 24:                                # SUB_I
                    b = pop()
                    v = pop() - b
                    if v > 2147483647 or v < -2147483648:
                        v = ((v + 0x80000000) & 0xFFFFFFFF) - 0x80000000
                    push(v)
                    pc += 1
                elif op == 25:                                # SUB_R
                    b = pop()
                    push(r32(pop() - b))
                    pc += 1
                elif op == 26:                                # MUL_I
                    b = pop()
                    v = pop() * b
                    if v > 2147483647 or v < -2147483648:
                        v = ((v + 0x80000000) & 0xFFFFFFFF) - 0x80000000
                    push(v)
                    pc += 1
                elif op == 27:                                # MUL_R
                    b = pop()
                    push(r32(pop() * b))
                    pc += 1
                elif op == 28:                                # DIV_I (truncating; 0 -> thread ends, J§3 0x23)
                    b = pop()
                    a = pop()
                    if b == 0:
                        status = ST_DIV0
                        break
                    q = abs(a) // abs(b)
                    if (a < 0) != (b < 0):
                        q = -q
                    push(s32(q))
                    pc += 1
                elif op == 29:                                # DIV_R (0.0 -> thread ends)
                    b = pop()
                    a = pop()
                    if b == 0.0:
                        status = ST_DIV0
                        break
                    push(r32(a / b))
                    pc += 1
                elif op == 30:                                # MOD_I: divisor 0 -> 0 (J§3 0x24)
                    b = pop()
                    a = pop()
                    if b == 0:
                        push(0)
                    else:
                        q = abs(a) // abs(b)
                        if (a < 0) != (b < 0):
                            q = -q
                        push(s32(a - q * b))
                    pc += 1
                elif op == 31:                                # NEG_I
                    stack[-1] = s32(-stack[-1])
                    pc += 1
                elif op == 32:                                # NEG_R
                    stack[-1] = -stack[-1]
                    pc += 1
                elif op == 37:                                # EQ_R: |a-b| < 0.001 (J§3 0x1a)
                    b = pop()
                    push(1 if abs(pop() - b) < REAL_EPS else 0)
                    pc += 1
                elif op == 41:                                # NE_R: raw bits (J§3 0x1b)
                    b = pop()
                    push(1 if rbits(pop()) != rbits(b) else 0)
                    pc += 1
                elif op == 38:                                # EQ_S
                    b = pop()
                    push(1 if pop() == b else 0)
                    pc += 1
                elif op == 42:                                # NE_S
                    b = pop()
                    push(1 if pop() != b else 0)
                    pc += 1
                elif op == 39:                                # EQ_B truthiness
                    b = pop()
                    push(1 if (pop() != 0) == (b != 0) else 0)
                    pc += 1
                elif op == 43:                                # NE_B
                    b = pop()
                    push(1 if (pop() != 0) != (b != 0) else 0)
                    pc += 1
                elif op == 44 or op == 45:                    # LT
                    b = pop()
                    push(1 if pop() < b else 0)
                    pc += 1
                elif op == 46 or op == 47:                    # LE
                    b = pop()
                    push(1 if pop() <= b else 0)
                    pc += 1
                elif op == 48 or op == 49:                    # GT
                    b = pop()
                    push(1 if pop() > b else 0)
                    pc += 1
                elif op == 50 or op == 51:                    # GE
                    b = pop()
                    push(1 if pop() >= b else 0)
                    pc += 1
                elif op == 52:                                # CAST (return bug: raw 32-bit value, J§7)
                    v = stack[-1]
                    src, dst = A[pc], B[pc]
                    if src == C.SC_REAL:
                        v = rbits(v)
                    elif src == C.SC_STR:
                        v = self.str_id(v)
                    if dst == C.SC_REAL:
                        v = bits2r(s32(v))
                    elif dst == C.SC_STR:
                        v = self.str_by_id[v] if 0 < v < len(self.str_by_id) else None
                    stack[-1] = v
                    pc += 1
                elif op == 53:                                # END (global initialiser)
                    status = ST_DONE
                    break
                elif op == 0:
                    pc += 1
                else:
                    raise RuntimeError('bad opcode %d at %d' % (op, pc))
        finally:
            self.thread_stack.pop()
        th.pc = pc
        th.func = cur_f
        th.locs = locs
        th.status = status
        self.total_ops += count
        sec = int(self.qgame.clock + 1e-9)
        self.ops_by_sec[sec] = self.ops_by_sec.get(sec, 0) + count
        self._count_status(status)
        if status in (ST_WAIT, ST_SUSPEND):
            pass
        return status

    # ---------------------------------------------------------- init (J§4: global initialisers first)
    def run_global_inits(self):
        for fidx in self.p.ginit:
            th = self.new_thread('ginit')
            self.start_func(th, fidx)
            st = self.run(th)
            if st != ST_DONE:
                self.errors.append('global init %s: %s' % (self.p.functions[fidx].name, STATUS_NAME[st]))

    def run_function(self, name, kind='main'):
        """run a function by name in its own thread (config / main). A wait ends the thread (Open)."""
        fidx = self.p.func_by_name.get(name)
        if fidx is None:
            return ST_NOFUNC
        st, _res, th = self.run_code(fidx + 1, kind)
        if st != ST_DONE:
            self.errors.append('%s: %s' % (name, STATUS_NAME[st]))
        return st

    # ---------------------------------------------------------- conditions / boolexprs (T§2)
    def eval_boolexpr(self, bx):
        """invalid / null boolexpr -> true; a function that does not finish -> false."""
        if bx is None or not bx.alive:
            return True
        k = bx.kind
        if k in ('cond', 'filter'):
            st, res, _th = self.run_code(bx.code, k)
            return st == ST_DONE and bool(res)
        if k == 'and':                       # BUGS GTD-OR: short-circuit [inf], the second operand only when needed
            return self.eval_boolexpr(bx.a) and self.eval_boolexpr(bx.b)
        if k == 'or':
            return self.eval_boolexpr(bx.a) or self.eval_boolexpr(bx.b)
        if k == 'not':
            return not self.eval_boolexpr(bx.a)
        return True

    def eval_conditions(self, trig):
        """T§2: every condition evaluated in list order, AND-ed (mode 2), empty -> true."""
        ok = True
        for c in list(trig.conds):
            r = self.eval_boolexpr(c.bx) if c.alive else True
            ok = ok and r
        return ok

    def eval_filter(self, bx, slot, value):
        """filter for an enum / event registration: push the filter slot, evaluate (T§5). CFilterFunc §0x6f2aa030:
        the engine keeps one filter object per code and its current unit stays readable through GetFilterUnit
        between enumerations; the port keeps the last candidate in the VM (the per-code object is the
        natives._condition cache)."""
        if bx is None:
            return True
        if slot == CTX_FILTER_UNIT:
            self.last_filter_unit = value
        self.ctx_push(slot, value)
        try:
            return self.eval_boolexpr(bx)
        finally:
            self.ctx_pop(slot)

    def var_stored(self, gi, v):
        """W9 variable events (natives._regvarevent): the store callback [obs 0x6f32a5c0] runs after every store;
        each registration of the name compares the new value and fires its trigger when true, every time (no
        previous-value state) [obs 0x6f338250], in registration order, synchronously."""
        from . import natives as NN
        g = self.p.globals[gi] if 0 <= gi < len(self.p.globals) else None
        is_int = bool(g is not None and g.get('type') in ('integer', 'boolean'))
        for reg in list(self.var_watch.get(gi, ())):
            if reg.alive and reg.trig.alive and NN.limit_true(reg.extra[0], v, reg.extra[1], is_int):
                self.fire(reg.trig, {CTX_EVENT_ID: reg.event_id}, {'event': reg.event_id})

    # ---------------------------------------------------------- trigger execution (T§2)
    def fire(self, trig, slots, evd=None):
        """event fire: push context slots, eval count +1 (even when disabled), enabled + conditions ->
        exec count +1 and run the actions synchronously; pop."""
        if not trig.alive:
            return
        slots = dict(slots)
        slots[CTX_TRIGGER] = self.h(trig)
        for s, v in slots.items():
            self.ctx_push(s, v)
        self.evdata.append(evd)
        try:
            trig.evalc += 1
            if trig.enabled and self.eval_conditions(trig):
                trig.execc += 1
                self.start_execution(trig, [a for a in trig.actions], caller_waits=False)
        finally:
            self.evdata.pop()
            for s in slots:
                self.ctx_pop(s)

    def trigger_execute(self, trig, wait):
        """TriggerExecute / TriggerExecuteWait: only slot 2 pushed, no enabled / condition check."""
        self.ctx_push(CTX_TRIGGER, self.h(trig))
        try:
            trig.evalc += 1
            trig.execc += 1
            return self.start_execution(trig, [a for a in trig.actions], caller_waits=wait)
        finally:
            self.ctx_pop(CTX_TRIGGER)

    def trigger_evaluate(self, trig):
        self.ctx_push(CTX_TRIGGER, self.h(trig))
        try:
            return self.eval_conditions(trig)
        finally:
            self.ctx_pop(CTX_TRIGGER)

    def execute_func(self, name):
        """T§7: unknown name -> nothing; else a one-shot execution in the caller's context, synchronous."""
        fidx = self.p.func_by_name.get(name)
        if fidx is None:
            return None
        act = TriggerAction(fidx + 1)
        ex = self.start_execution(None, [act], caller_waits=True, one_shot=True)
        return ex

    def start_execution(self, trig, actions, caller_waits, one_shot=False):
        ex = Execution()
        self.next_exid += 1
        ex.id = self.next_exid
        ex.trig = trig
        ex.actions = actions                 # snapshot
        ex.caller_waits = caller_waits
        ex.one_shot = one_shot
        ex.wait_on_sleeps = trig.wait_on_sleeps if trig is not None else False
        # capture the top of all 16 context stacks (+ add-ref, released when the execution ends) (T§4)
        ex.ctx = [self.ctx_top(s) for s in range(NCTX)]
        for v in ex.ctx:
            if v >= HANDLE_BASE:
                self.H.addref(v)
        ex.evd = self.evdata[-1] if self.evdata else None
        if trig is not None:
            trig.live_execs += 1
        self.run_execution(ex)
        if ex.suspended and caller_waits:
            self._child_suspended(ex)
        return ex

    def _child_suspended(self, child):
        """T§1.5: a TriggerExecuteWait / ExecuteFunc child suspended: a listening parent (WaitOnSleeps)
        waits for its completion."""
        parent = self.cur_exec()
        th = self.cur_thread()
        if parent is None or th is None or not parent.wait_on_sleeps or th.execution is not parent:
            return
        child.parent = parent
        parent.children.append(child)
        th.wait_child = child

    def run_execution(self, ex):
        for s in range(NCTX):
            self.ctx_push(s, ex.ctx[s])
        self.evdata.append(ex.evd)
        self.exec_stack.append(ex)
        ex.suspended = False
        try:
            while ex.idx < len(ex.actions):
                act = ex.actions[ex.idx]
                if ex.thread is None:
                    if not act.alive:                # destroyed action: not run (T§2)
                        ex.idx += 1
                        continue
                    th = self.new_thread('action')
                    th.execution = ex
                    if act.code <= 0 or act.code > len(self.p.functions):
                        ex.idx += 1
                        continue
                    self.start_func(th, act.code - 1)
                    ex.thread = th
                th = ex.thread
                st = self.run(th)
                if st == ST_WAIT:
                    self._schedule_wait(ex, th.wait_req)
                    ex.suspended = True
                    return
                if st == ST_SUSPEND:                 # waiting for children (WaitOnSleeps)
                    ex.suspended = True
                    return
                # 1 -> next action; 2/5/6/7 -> action abandoned, next action (T§2)
                ex.thread = None
                ex.idx += 1
            self._finish_execution(ex)
        finally:
            self.exec_stack.pop()
            self.evdata.pop()
            for s in reversed(range(NCTX)):
                self.ctx_pop(s)

    def _finish_execution(self, ex):
        ex.done = True
        self.waiting.discard(ex)
        for v in ex.ctx:
            if v >= HANDLE_BASE:
                self.H.release(v)
        if ex.trig is not None:
            ex.trig.live_execs -= 1
        p = ex.parent
        if p is not None and ex in p.children:
            p.children.remove(ex)
            if not p.children:
                self._send_resume(p)

    def _schedule_wait(self, ex, t):
        """T§1.1/1.3: delay max(t, 0.0001) on the real-time queue, then a resume command one turn later."""
        d = t if t > WAIT_MIN else WAIT_MIN
        if self.wait_grid:
            # [obs runtime 1.26a] docs/oracle.md "TriggerSleepAction(d)": the thread resumes on the next 0.1 s game-time grid point strictly after
            # now + d + 0.1 (d 0: 0.2, 0.5: 0.7; the observed 2.0 -> 2.1 is inside d + 0.1 .. 0.2, the model gives 2.2 [open]). The resume command
            # is due at that turn: the real-time node is placed in the middle of the turn before it (the command goes out at the end of a turn
            # and is processed one turn later), so both VMs keep the one command path.
            x = (self.steps + (d if d < 1000000.0 else 1000000.0) * 200.0 + WAIT_GRID_STEPS) / WAIT_GRID_STEPS + 1e-9      # (waits beyond 1e6 s: capped, an int cast in the twin)
            rs = (math.floor(x) + 1) * WAIT_GRID_STEPS
            spt = self.steps_per_turn
            rt = self.turn + (rs - self.turn_step0 + spt - 1) // spt
            d = TURN_REAL * (rt - 1) + TURN_REAL * 0.5 - self.qreal.clock
        # > 120 s chunks + remainder: total unchanged (T§1.1) -> one node here
        self.waiting.add(ex)
        if len(self.waiting) > self.max_concurrent_waits:
            self.max_concurrent_waits = len(self.waiting)
        seq = ex.seq
        self.qreal.add(d, lambda node, ex=ex, seq=seq: self._send_resume(ex, seq))

    def _send_resume(self, ex, seq=None):
        if seq is None:
            seq = ex.seq
        self.cmd_seq += 1
        self.commands.append((self.turn + self.latency_turns, self.cmd_seq, ex, seq))

    def _process_commands(self):
        due = [c for c in self.commands if c[0] <= self.turn]
        if not due:
            return
        self.commands = [c for c in self.commands if c[0] > self.turn]
        due.sort(key=lambda c: (c[0], c[1]))
        for _t, _s, ex, seq in due:
            if ex.done or seq != ex.seq:           # duplicate / stale resume (T§1.1)
                continue
            ex.seq += 1
            self.waiting.discard(ex)
            self.run_execution(ex)

    # ---------------------------------------------------------- callbacks (ForGroup, timers, ...)
    def run_callback(self, code, kind, slot=None, value=0):
        """new thread with its own budget; a wait or any unfinished status drops the rest (T§5/T§6)."""
        if slot is not None:
            self.ctx_push(slot, value)
        try:
            st, res, _th = self.run_code(code, kind)
        finally:
            if slot is not None:
                self.ctx_pop(slot)
        return st

    # ---------------------------------------------------------- timers (T§6)
    def timer_start(self, tm, timeout, periodic, code):
        if tm.node is not None:
            tm.node[3] = False                 # restart cancels the old schedule
        tm.code = code
        tm.timeout = timeout
        tm.periodic = periodic
        tm.resumed = False
        d = timeout if timeout > WAIT_MIN else WAIT_MIN
        tm.period = d
        tm.running = True
        tm.node = self.qgame.add(d, lambda node, tm=tm: self._timer_fire(tm, node))

    def timer_pause(self, tm):
        if tm.running:
            tm.saved_remaining = self.timer_remaining(tm)
            if tm.node is not None:
                tm.node[3] = False
            tm.node = None
            tm.running = False

    def timer_resume(self, tm):
        if not tm.running:
            d = tm.saved_remaining if tm.saved_remaining > WAIT_MIN else WAIT_MIN
            tm.running = True
            tm.resumed = True                  # scheduled with event 0x80272 (CTimerWar3 §10)
            tm.node = self.qgame.add(d, lambda node, tm=tm: self._timer_fire(tm, node))

    def timer_remaining(self, tm):
        if tm.running and tm.node is not None:
            r = tm.node[0] - self.qgame.clock
            return r32(r if r > 0 else 0.0)
        return r32(tm.saved_remaining)

    def _timer_fire(self, tm, node):
        if not tm.alive:
            return
        resumed = tm.resumed                 # 0x80272 schedule: re-arm once after this dispatch
        tm.resumed = False
        if tm.periodic:                        # re-armed at previous due + period (no drift, T§3/T§6)
            if TIMER_TIE_START_ORDER:
                # [obs runtime 1.26a, ext_determinism]: the re-arm keeps the schedule's original queue
                # sequence, so at equal due times a periodic timer started before a one-shot still fires
                # first instead of losing the tie to the newly inserted node.
                nn = [node[0] + tm.period, node[1], node[2], True]
            else:
                # default (old maps / goldens): the re-arm is a fresh insertion and takes the next FIFO
                # sequence (FloatMini §10: the queue orders equal due times by the inserted sequence).
                self.qgame.seq += 1
                nn = [node[0] + tm.period, self.qgame.seq, node[2], True]
            heapq.heappush(self.qgame.heap, nn)
            tm.node = nn
        else:
            tm.running = False                 # marked not running before the dispatch
            tm.node = None
            tm.saved_remaining = 0.0
        hid = self.h(tm) if not tm.internal else 0
        self.ctx_push(CTX_EXPIRED_TIMER, hid)
        try:
            for reg in list(tm.regs):          # expire-event triggers first, registration order
                if reg.alive and reg.trig.alive:
                    self.fire(reg.trig, {CTX_EVENT_ID: reg.event_id, CTX_SOURCE: hid}, {'event': reg.event_id})
            if tm.code:
                self.run_callback(tm.code, 'timer')
        finally:
            self.ctx_pop(CTX_EXPIRED_TIMER)
        if resumed:                            # 0x80272 handler: expiry dispatch, context pop, then re-arm with the full timeout
            # 1.26a §6.1/§6.2: the re-arm runs after the dispatch and reads the current fields, so a pause
            # or restart made by the expiry callback is overridden; a zero code or a destroyed timer skips it.
            if tm.alive and tm.code:
                self.timer_start(tm, tm.timeout, tm.periodic, tm.code)

    # ---------------------------------------------------------- main loop (T§1.2)
    def run_turn(self):
        self.turn_step0 = self.steps
        self._process_commands()
        for fn in self.on_turn:
            fn(self.turn)
        for _ in range(self.steps_per_turn):
            self.steps += 1
            clock = self.steps * STEP
            self.qgame.advance(clock)
            self.qgame.clock = clock
            for fn in self.on_step:
                fn(STEP)
        # BUGS FF-PAUSE: the real-time waits keep running under PauseGame (Footman Frenzy: PauseGameOn, then dialogs
        # with 'no response after 40 s' waits; they would never end) [inf]
        self.qreal.advance(self.qreal.clock + TURN_REAL)
        self.turn += 1

    def run_until(self, seconds):
        while self.steps * STEP < seconds - 1e-9:
            self.run_turn()
