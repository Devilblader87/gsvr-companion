"""Net command records, turn blocks and replay-data chunks (PORT-Net).

Clean-room port of the 1.21/1.26a net-command chapters in ``docs/decomp/classes/``: the per-id record bodies
(CNetCommandUnitOrderBasic / TargetPoint / TargetImage, UnitSelectionEvent, UnitDefineControlGroup, AllyPing,
CheatSetTOD / CheatCritter, the cheat slot/value pair), the command-block framing of the observer dispatcher, the
framed block reader, the replay-data chunk messages 0x81/0x82/0x83 and their fixed-Huffman codec.

The port has no net layer today (docs/multiplayer.md is master-authoritative, docs/decomp/port/Net.md records the
comparison); this module is the wire contract a lockstep / replay path needs. It is deliberately free of engine
state: a caller maps the decoded entity pairs (handle, birth id) to the port's serials, and the 16-bit order/flag
word is *not* a JASS order id (the chapters state the two numberings differ).

Field offsets are record-relative, as in the chapters: every record has the six common header words (event id =
0xa0000 | id at +0x08, id byte at +0x14, sender slot at +0x15) and the body starts at +0x18. ``block()`` writes
that header; the body writers below return only the body.

Constants: block append refuses at 0x400 bytes (warns above 0x3ff); control groups hold at most 12 units and the
sender keeps the requested count, tail-padding skipped units with (-1, -1); replay chunks carry at most 0x3fd
bytes, a length of 0x3fe or more is a truncated record, and the sender may have at most 5 chunks in flight.
"""
import struct

EVENT_BASE = 0xA0000
NONE = 0xFFFFFFFF

CMD_ORDER_BASIC = 0x10
CMD_ORDER_TARGET_POINT = 0x11
CMD_ORDER_TARGET_IMAGE = 0x12
CMD_ORDER_TARGET_IMAGE2 = 0x13
CMD_ORDER_TARGET_FOGGED = 0x14
CMD_ORDER_TARGET_FOGGED2 = 0x15
CMD_SELECTION_MODIFY = 0x16
CMD_DEFINE_GROUP = 0x17
CMD_SELECT_GROUP = 0x18
CMD_SELECT_SUBGROUP = 0x19
CMD_REFRESH_SUBGROUP = 0x1A
CMD_SELECTION_EVENT = 0x1B
CMD_CHEAT_CRITTER = 0x21
CMD_CHEAT_GOLD = 0x27
CMD_CHEAT_LUMBER = 0x28
CMD_CHEAT_RESOURCES = 0x2D
CMD_CHEAT_TOD = 0x2E
CMD_TEAM_ALLIANCE = 0x50
CMD_TEAM_TRADE = 0x51
CMD_TRAIN_CANCEL = 0x1E
CMD_ALLY_PING = 0x68
CMD_ARROW_KEY = 0x75
CMD_REPLAY_UNCOMPRESSED = 0x81
CMD_REPLAY_COMPRESSED = 0x82
CMD_REPLAY_DONE = 0x83

BLOCK_MAX = 0x400                 # append refuses at 0x400 bytes
BLOCK_WARN = 0x3FF
FRAME_MAX = 0x400                 # framed-block reader accepts a length <= 0x400
REPLAY_CHUNK = 0x3FD              # sender cap and decompressor output capacity
REPLAY_READ_MAX = 0x3FD           # a length of 0x3fe or more is rejected
REPLAY_WINDOW = 5                 # credit: at most five chunk messages in flight
GROUP_MAX = 12
SLOT_NONE = 0xFF                  # sender slot 0xff: the block is not posted at all
PING_MIN2 = 250000.0              # squared distance 500^2 (the ally ping is dropped below it)

# Ids the dispatcher knows (Rep1 §2.3). 0x6e is known but its length is not decodable yet (the version-gated
# sub-record has no documented size), so read_block stops there instead of mistaking its body for records.
KNOWN_IDS = frozenset(
    list(range(0x10, 0x1F)) + list(range(0x20, 0x33)) + [0x50, 0x51] +
    list(range(0x60, 0x6F)) + list(range(0x70, 0x74)) + [0x75, 0x81, 0x82, 0x83, 0x84, 0x85])

_U8 = struct.Struct('<B')
_U16 = struct.Struct('<H')
_U32 = struct.Struct('<I')
_F32 = struct.Struct('<f')


def f32(v):
    """round to a 4-byte float (the game's software-float helpers work on 32-bit floats)."""
    return _F32.unpack(_F32.pack(float(v)))[0]


def event_id(cmd):
    return EVENT_BASE | (int(cmd) & 0xFF)


def input_kind(cmd):
    """The port's player-input kind for a unit-order record (docs/engine_w5.md §6, docs/decomp/...p3 §17):
    0x10 = kind 0 (immediate), 0x11 = kind 1 (point), 0x12/0x13/0x14/0x15 = kind 2 (target: the second entity
    pair and point are the target image). -> -1 for every other command."""
    c = int(cmd) & 0xFF
    if c == CMD_ORDER_BASIC:
        return 0
    if c == CMD_ORDER_TARGET_POINT:
        return 1
    if c in (CMD_ORDER_TARGET_IMAGE, CMD_ORDER_TARGET_IMAGE2, CMD_ORDER_TARGET_FOGGED, CMD_ORDER_TARGET_FOGGED2):
        return 2
    return -1


# -------------------------------------------------------------------------------------------------------- writing
def _u8(v):
    return _U8.pack(int(v) & 0xFF)


def _u16(v):
    return _U16.pack(int(v) & 0xFFFF)


def _u32(v):
    return _U32.pack(int(v) & 0xFFFFFFFF)


def _f(v):
    return _F32.pack(f32(v))


def order_basic(flag, handle=NONE, birth=NONE, field1c=NONE):
    """0x10 id-0x10 body: u16 flag, then +0x1c (a 32-bit order/handle word, -1 when unset), then the entity
    pair +0x20/+0x24 (handle, birth id; (-1,-1) for no unit)."""
    return _u16(flag) + _u32(field1c) + _u32(handle) + _u32(birth)


def order_target_point(flag, handle, birth, x, y):
    """0x11: the 0x10 body plus the target point at +0x28/+0x2c (two 4-byte floats)."""
    return order_basic(flag, handle, birth) + _f(x) + _f(y)


def order_target_image(flag, handle, birth, x, y, th=NONE, tb=NONE):
    """0x12: 0x11 plus a second entity pair +0x30/+0x34 (the target image; -1 = none)."""
    return order_target_point(flag, handle, birth, x, y) + _u32(th) + _u32(tb)


def selection(op, units):
    """0x16 UnitSelectionModify: u8 op, u16 count, count * (handle, birth). The list is the player's selection
    (at most 12); the dispatcher reads exactly ``count`` pairs."""
    pairs = list(units)
    out = _u8(op) + _u16(len(pairs))
    for h, b in pairs:
        out += _u32(h) + _u32(b)
    return out


def group_pairs(units):
    """The 0x17 filter of §3.3: skip a null / unresolvable entry, keep the rest compacted from slot 0. The count
    stays the clamped requested count, so the tail is filled with the (-1, -1) "no unit" pair."""
    src = list(units)
    n = len(src)
    if n > GROUP_MAX:
        n = GROUP_MAX
    good = []
    for u in src[:n]:
        if u is not None:
            good.append(u)
    out = b''
    for i in range(n):
        if i < len(good):
            out += _u32(good[i][0]) + _u32(good[i][1])
        else:
            out += _u32(NONE) + _u32(NONE)
    return out, n


def define_group(group, units):
    """0x17 UnitDefineControlGroup: u8 group, u16 count, count * pair. Returns b'' for an empty group (the sender
    produces no message at all then)."""
    body, n = group_pairs(units)
    if n == 0:
        return b''
    return _u8(group) + _u16(n) + body


def select_group(group, modifier):
    """0x18 UnitSelectControlGroup: the body reader takes +0x19 **before** +0x18, so the modifier byte is first on
    the wire and the group number second [obs 0x6f29adb0]."""
    return _u8(modifier) + _u8(group)


def select_subgroup(a, b, c):
    """0x19: three 32-bit values at +0x18/+0x1c/+0x20."""
    return _u32(a) + _u32(b) + _u32(c)


def refresh_subgroup():
    """0x1a: no payload."""
    return b''


def selection_event(op, handle, birth):
    """0x1b: u8 operation, then the unit pair +0x1c/+0x20. The sender does not validate the operation byte, the
    pair is copied verbatim (a dead unit can travel)."""
    return _u8(op) + _u32(handle) + _u32(birth)


def revive_cancel(a, b):
    """0x1d: two 32-bit values."""
    return _u32(a) + _u32(b)


def train_cancel(slot, value):
    """0x1e: u8 at +0x18 then a 32-bit value at +0x1c."""
    return _u8(slot) + _u32(value)


def team_alliance(slot, flag):
    """0x50: u8 target slot, 32-bit alliance flag."""
    return _u8(slot) + _u32(flag)


def team_trade(slot, a, b):
    """0x51: u8 target slot, two 32-bit values."""
    return _u8(slot) + _u32(a) + _u32(b)


def _keystr(s):
    """One fixed 0x100-byte key field. The wrapper pre-zeroes the three strings and the reader stops at the first
    NUL (Rep3 §13). The bytes are the UTF-8 encoding (the same as the other Udon string writers); keys are
    ordinary ASCII map strings in the observed maps, non-ASCII is [inf]."""
    b = str(s).encode('utf-8')[:0x100]
    return b + b'\x00' * (0x100 - len(b))


def sync_store_int(cmd, key1, key2, key3, value):
    """0x6b SyncStoreInteger / 0x6d SyncStoreBoolean: three 0x100-byte keys, then the value as a dword (the boolean
    is a dword too, not a byte; Rep3 §13)."""
    if cmd not in (0x6B, 0x6D):
        raise ValueError('sync_store_int: command 0x%02x is not 0x6b / 0x6d' % cmd)
    return _keystr(key1) + _keystr(key2) + _keystr(key3) + _u32(value)


def sync_store_real(key1, key2, key3, value):
    """0x6c SyncStoreReal: three keys, then the value as a 4-byte float (Rep3 §13)."""
    return _keystr(key1) + _keystr(key2) + _keystr(key3) + _f(value)


def sync_clear(cmd, key1, key2, key3):
    """0x70–0x73 SyncClearInteger / Real / Boolean / Unit: the same three keys and no value (Rep3 §13)."""
    if cmd not in (0x70, 0x71, 0x72, 0x73):
        raise ValueError('sync_clear: command 0x%02x carries no clear body' % cmd)
    return _keystr(key1) + _keystr(key2) + _keystr(key3)


def ally_ping(x, y, fade=0.0):
    """0x68: three 4-byte values at +0x18/+0x1c/+0x20 (x, y, fade)."""
    return _f(x) + _f(y) + _f(fade)


def cheat_critter(handle, birth):
    """0x21: the critter's entity pair (two 32-bit values)."""
    return _u32(handle) + _u32(birth)


def cheat_slot_value(cmd, slot, value):
    """0x27 gold / 0x28 lumber / 0x2d resources: u8 player slot then a 32-bit amount."""
    if cmd not in (CMD_CHEAT_GOLD, CMD_CHEAT_LUMBER, CMD_CHEAT_RESOURCES):
        raise ValueError('cheat_slot_value: command 0x%02x carries no slot/value body' % cmd)
    return _u8(slot) + _u32(value)


def cheat_tod(hours):
    """0x2e: one 4-byte float hour at +0x18 (the sender copies the argument verbatim, no range check)."""
    return _f(hours)


def arrow_key(code):
    """0x75: one byte."""
    return _u8(code)


# -------------------------------------------------------------------------------------------------------- reading
class _Rd(object):
    __slots__ = ('b', 'p', 'ok')

    def __init__(self, b, p):
        self.b, self.p, self.ok = b, p, True

    def raw(self, n):
        if self.p + n > len(self.b):
            self.ok = False
            self.p = len(self.b)
            return b'\x00' * n
        v = self.b[self.p:self.p + n]
        self.p += n
        return v

    def u8(self):
        return _U8.unpack(self.raw(1))[0]

    def u16(self):
        return _U16.unpack(self.raw(2))[0]

    def u32(self):
        return _U32.unpack(self.raw(4))[0]

    def f(self):
        return _F32.unpack(self.raw(4))[0]

    def cstr(self, n):
        """a fixed C string field: the reader reads n bytes and stops at the first NUL (a truncated field reads as
        empty, §2 / §11)."""
        v = self.raw(n).split(b'\x00', 1)[0]
        return v.decode('latin-1')


def _read_cheat(cmd, r):
    if cmd == CMD_CHEAT_CRITTER:
        return {'handle': r.u32(), 'birth': r.u32()}
    if cmd in (CMD_CHEAT_GOLD, CMD_CHEAT_LUMBER, CMD_CHEAT_RESOURCES):
        return {'slot': r.u8(), 'value': r.u32()}
    if cmd == CMD_CHEAT_TOD:
        return {'hour': r.f()}
    return {}


def decode_body(cmd, data, pos=0):
    """Decode the id-specific body at ``pos``. -> (fields, newpos), or (None, pos) when the data is truncated.

    Field names: flag, field1c, handle/birth (the primary entity pair, -1 = none), x/y, th/tb (a second pair),
    op, units, group, modifier, slot, value, hour, code, payload. ``strings`` for the sync/chat commands."""
    r = _Rd(data, pos)
    d = {'cmd': cmd}
    cmd = int(cmd) & 0xFF
    if cmd in (CMD_ORDER_BASIC, CMD_ORDER_TARGET_POINT, CMD_ORDER_TARGET_IMAGE, CMD_ORDER_TARGET_IMAGE2,
               CMD_ORDER_TARGET_FOGGED, CMD_ORDER_TARGET_FOGGED2):
        d['flag'] = r.u16()
        d['field1c'] = r.u32()
        d['handle'] = r.u32()
        d['birth'] = r.u32()
        if cmd != CMD_ORDER_BASIC:
            d['x'] = r.f()
            d['y'] = r.f()
        if cmd in (CMD_ORDER_TARGET_IMAGE, CMD_ORDER_TARGET_IMAGE2):
            d['th'] = r.u32()
            d['tb'] = r.u32()
        if cmd == CMD_ORDER_TARGET_IMAGE2:
            d['t2h'] = r.u32()
            d['t2b'] = r.u32()
        if cmd in (CMD_ORDER_TARGET_FOGGED, CMD_ORDER_TARGET_FOGGED2):
            d['th'] = r.u32()
            d['tb'] = r.u32()
            d['fog'] = r.u8()
            d['fx'] = r.f()
            d['fy'] = r.f()
        if cmd == CMD_ORDER_TARGET_FOGGED2:
            d['t2h'] = r.u32()
            d['t2b'] = r.u32()
    elif cmd in (CMD_SELECTION_MODIFY, CMD_DEFINE_GROUP):
        d['op'] = r.u8()
        if cmd == CMD_DEFINE_GROUP:
            d['group'] = d['op']
        d['count'] = r.u16()
        units = []
        for _ in range(d['count']):
            units.append((r.u32(), r.u32()))
        d['units'] = units
    elif cmd == CMD_SELECT_GROUP:
        d['modifier'] = r.u8()
        d['group'] = r.u8()
    elif cmd == CMD_SELECT_SUBGROUP:
        d['a'], d['b'], d['c'] = r.u32(), r.u32(), r.u32()
    elif cmd == CMD_REFRESH_SUBGROUP:
        pass
    elif cmd in (CMD_SELECTION_EVENT, 0x1C):
        d['op'] = r.u8()
        d['handle'] = r.u32()
        d['birth'] = r.u32()
    elif cmd == 0x1D:
        d['a'], d['b'] = r.u32(), r.u32()
    elif cmd == CMD_TRAIN_CANCEL:
        d['slot'] = r.u8()
        d['value'] = r.u32()
    elif cmd in (0x20, 0x22, 0x23, 0x24, 0x25, 0x26, 0x29, 0x2A, 0x2B, 0x2C, 0x2F, 0x30, 0x31, 0x32):
        pass
    elif cmd == CMD_CHEAT_CRITTER or cmd in (CMD_CHEAT_GOLD, CMD_CHEAT_LUMBER, CMD_CHEAT_RESOURCES, CMD_CHEAT_TOD):
        d.update(_read_cheat(cmd, r))
    elif cmd in (CMD_TEAM_ALLIANCE,):
        d['slot'] = r.u8()
        d['value'] = r.u32()
    elif cmd == CMD_TEAM_TRADE:
        d['slot'] = r.u8()
        d['a'], d['b'] = r.u32(), r.u32()
    elif cmd == 0x60:
        d['a'], d['b'] = r.u32(), r.u32()
        d['text'] = r.cstr(0x100)
    elif cmd in (0x61, 0x66, 0x67):
        pass
    elif cmd == 0x62:
        d['a'], d['b'], d['c'] = r.u32(), r.u32(), r.u32()
    elif cmd in (0x63, 0x64, 0x65):
        d['a'], d['b'] = r.u32(), r.u32()
    elif cmd == CMD_ALLY_PING:
        d['x'], d['y'], d['fade'] = r.f(), r.f(), r.f()
    elif cmd in (0x69, 0x6A):
        d['a'], d['b'], d['c'], d['e'] = r.u32(), r.u32(), r.u32(), r.u32()
    elif cmd in (0x6B, 0x6C, 0x6D, 0x70, 0x71, 0x72, 0x73):
        d['key1'] = r.cstr(0x100)
        d['key2'] = r.cstr(0x100)
        d['key3'] = r.cstr(0x100)
        if cmd in (0x6B, 0x6D):
            d['value'] = r.u32()
        elif cmd == 0x6C:
            d['value'] = r.f()
    elif cmd == CMD_ARROW_KEY:
        d['code'] = r.u8()
    elif cmd in (CMD_REPLAY_UNCOMPRESSED, CMD_REPLAY_COMPRESSED):
        n = r.u16()
        if n > REPLAY_READ_MAX:
            r.p = len(data)
            r.ok = False
            d['length'] = 0
            d['payload'] = b''
        else:
            d['length'] = n
            d['payload'] = r.raw(n)
    elif cmd in (CMD_REPLAY_DONE, 0x84, 0x85):
        pass
    else:
        return None, pos               # unknown id: the dispatcher's default arm skips it
    if not r.ok:
        return None, pos
    return d, r.p


# -------------------------------------------------------------------------------------------------------- blocks
def known_id(cmd):
    """True for a command id the dispatcher has a case for (Rep1 §2.3); an id outside this set takes the
    default arm (flush, no body, continue with the next id byte)."""
    return (int(cmd) & 0xFF) in KNOWN_IDS


def post_block(slot, commands):
    """A block as posted: a sender slot of 0xff means "no player" and suppresses the post entirely (Rep1 §1,
    Rep2 §4.1). -> None when suppressed, else ``block(slot, commands)``."""
    if (int(slot) & 0xFF) == SLOT_NONE:
        return None
    return block(slot, commands)


def block(slot, commands):
    """One received command block: a 32-bit header (low byte = sender slot, bytes 1-2 = command count, u16 LE)
    followed by ``count`` records of one id byte + body. ``commands`` is an iterable of (cmd, body)."""
    cmds = list(commands)
    if len(cmds) > 0xFFFF:
        raise ValueError('block: %d commands' % len(cmds))
    out = _u32((int(slot) & 0xFF) | ((len(cmds) & 0xFFFF) << 8))
    for cmd, body in cmds:
        out += _u8(cmd) + body
    return out


def read_block(data, pos=0):
    """-> (slot, commands, newpos); commands is a list of (cmd, fields). A truncated command stops the block and
    the fields of that command are dropped (the dispatcher reports a parse error and does not post it). -> None on
    a truncated header."""
    if pos + 4 > len(data):
        return None
    hdr = _U32.unpack(data[pos:pos + 4])[0]
    pos += 4
    slot = hdr & 0xFF
    n = (hdr >> 8) & 0xFFFF
    out = []
    for _ in range(n):
        if pos >= len(data):
            break
        cmd = data[pos]
        pos += 1
        if not known_id(cmd):
            continue                  # the default arm has no body and continues with the next id byte
        d, np = decode_body(cmd, data, pos)
        if d is None:
            break                     # known id with a truncated / not-yet-decoded body: drop the tail
        out.append((cmd, d))
        pos = np
    return slot, out, pos


def block_size(slot, commands):
    """Serialised size of ``block(slot, commands)`` — the queue's 0x400 / 0x3ff test."""
    return 4 + sum(1 + len(body) for _cmd, body in commands)


# -------------------------------------------------------------------------------------------------------- frames
def frame(tag, payload):
    """The framed-block reader of §15: one tag byte, a u16 length (accepted up to 0x400) and the body."""
    if len(payload) > FRAME_MAX:
        raise ValueError('frame: payload of %d bytes' % len(payload))
    return _u8(tag) + _u16(len(payload)) + bytes(payload)


def read_frame(data, pos=0):
    """-> (tag, payload, newpos) or None. A length above 0x400 is a zero-length block (the reader's rejection)."""
    if pos + 3 > len(data):
        return None
    tag = data[pos]
    n = _U16.unpack(data[pos + 1:pos + 3])[0]
    pos += 3
    if n > FRAME_MAX:
        return tag, b'', pos
    if pos + n > len(data):
        return tag, b'', pos
    return tag, data[pos:pos + n], pos + n


# -------------------------------------------------------------------------------------------------------- replay data
def replay_chunk_body(payload, compressed=True):
    """-> (cmd, body) for one replay-data message. The sender caps a chunk at 0x3fd bytes, builds both chunk
    objects and sends the compressed one only when the encoder fitted the chunk length (a 0 result means
    "uncompressed"); an empty payload sends nothing (None)."""
    payload = bytes(payload)
    if not payload:
        return None
    if len(payload) > REPLAY_CHUNK:
        raise ValueError('replay chunk of %d bytes' % len(payload))
    if compressed:
        enc = huff_encode(payload, len(payload))
        if enc is not None:
            return CMD_REPLAY_COMPRESSED, _u16(len(enc)) + enc
    return CMD_REPLAY_UNCOMPRESSED, _u16(len(payload)) + payload


def replay_messages(data, compressed=True):
    """Stream a replay buffer as the sender does: chunks of at most 0x3fd bytes, then the 0x83 done message.
    -> list of (cmd, body). The transport's five-chunk credit window is a caller concern (REPLAY_WINDOW)."""
    out = []
    pos = 0
    while pos < len(data):
        n = len(data) - pos
        if n > REPLAY_CHUNK:
            n = REPLAY_CHUNK
        msg = replay_chunk_body(data[pos:pos + n], compressed)
        if msg is not None:
            out.append(msg)
        pos += n
    out.append((CMD_REPLAY_DONE, b''))
    return out


def read_replay_chunk(cmd, payload, cap=REPLAY_CHUNK):
    """Decode the ``payload`` field of one 0x81 / 0x82 record (``decode_body`` already stripped the length word)
    -> bytes, or None for a truncated record / bad compressed data."""
    if cmd == CMD_REPLAY_UNCOMPRESSED:
        return payload
    if cmd == CMD_REPLAY_COMPRESSED:
        if not payload:
            return None
        dec = huff_decode(payload, cap)
        return dec if dec else None
    return None


# -------------------------------------------------------------------------------------------------------- pings
def ping_accept(x0, y0, x, y):
    """The engine-side ally ping gate (§1.3): a ping is produced only when the squared distance to the previously
    stored point is **not below** 250000 (500 units); the caller then stores the new point. The subtraction,
    squaring and addition run on 4-byte floats like the game's software-float helpers."""
    dx = f32(f32(x) - f32(x0))
    dy = f32(f32(y) - f32(y0))
    d2 = f32(f32(dx * dx) + f32(dy * dy))
    return not (d2 < PING_MIN2)


# -------------------------------------------------------------------------------------------------------- Huffman
# The fixed 256-byte code-length table of the replay compressor/decompressor [obs 0x6f84b810 / 0x6fa9b820,
# byte-identical in both builds]. The codes are generated from it (longest first, ascending symbol, first code 0,
# each next code = previous + 1 shifted right by the drop in length) — never stored.
HUFF_LEN = bytes((
    1, 4, 8, 8, 8, 8, 8, 8, 8, 8, 8, 5, 9, 9, 10, 6,
    10, 10, 9, 10, 10, 10, 10, 10, 10, 10, 10, 11, 10, 10, 10, 10,
    10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10,
    11, 11, 11, 10, 10, 10, 11, 11, 10, 11, 10, 11, 10, 10, 10, 10,
    10, 9, 10, 10, 10, 9, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10,
    10, 10, 10, 10, 9, 10, 10, 10, 11, 10, 10, 10, 11, 11, 7, 9,
    8, 9, 10, 10, 9, 8, 8, 8, 10, 9, 10, 9, 7, 10, 9, 7,
    7, 11, 7, 10, 9, 10, 10, 10, 10, 10, 10, 10, 10, 10, 11, 9,
    7, 10, 11, 11, 11, 10, 10, 11, 10, 11, 11, 10, 11, 11, 11, 11,
    10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10,
    12, 12, 12, 12, 11, 12, 12, 12, 12, 12, 12, 12, 12, 11, 12, 11,
    12, 12, 12, 12, 11, 12, 12, 11, 12, 12, 12, 12, 12, 12, 12, 12,
    12, 12, 12, 12, 12, 12, 12, 11, 11, 12, 12, 11, 11, 11, 12, 12,
    12, 12, 11, 11, 11, 12, 12, 11, 11, 12, 12, 12, 12, 12, 12, 12,
    12, 12, 12, 12, 12, 11, 12, 12, 11, 12, 12, 11, 12, 12, 11, 12,
    11, 12, 12, 12, 11, 11, 11, 11, 11, 11, 11, 11, 11, 10, 10, 3,
))

_CODES = None


def huff_codes():
    """-> list of 256 (code, length) pairs, canonical from the long end (see the module comments)."""
    global _CODES
    if _CODES is None:
        out = [None] * 256
        code = 0
        prev = 12
        first = True
        for ln in range(12, 0, -1):
            for sym in range(256):
                if HUFF_LEN[sym] != ln:
                    continue
                if not first and ln < prev:
                    code >>= prev - ln
                out[sym] = (code, ln)
                code += 1
                prev = ln
                first = False
        _CODES = out
    return _CODES


def huff_encode(src, capacity=None):
    """The fixed-table encoder of §3.2: codes are packed most-significant bit first, the final byte zero-padded.
    -> bytes, or None when the output would exceed ``capacity`` (the caller then sends the uncompressed chunk)."""
    src = bytes(src)
    if capacity is None:
        capacity = len(src)
    codes = huff_codes()
    out = bytearray()
    acc = 0
    free = 8
    for v in src:
        code, ln = codes[v]
        while ln >= free:
            ln -= free
            if len(out) >= capacity:
                return None
            out.append(((code >> ln) | acc) & 0xFF)
            acc = 0
            free = 8
        if ln:
            free -= ln
            acc |= (code << free) & 0xFF
    if free < 8:
        if len(out) >= capacity:
            return None
        out.append(acc)
    return bytes(out)


def huff_decode(src, capacity=REPLAY_CHUNK):
    """Inverse of ``huff_encode`` with the game's termination rules: -> bytes, or b'' when a symbol does not fit
    the output capacity (the caller reads 0 as "bad replay data")."""
    src = bytes(src)
    codes = huff_codes()
    # a decode tree: node 0 = root, nodes 1.. are created in code order; tree_sym = -1 for an inner node
    left = [-1]
    right = [-1]
    sym = [-1]
    for ch in range(256):
        code, ln = codes[ch]
        node = 0
        for k in range(ln - 1, -1, -1):
            if (code >> k) & 1:
                if right[node] < 0:
                    left.append(-1)
                    right.append(-1)
                    sym.append(-1)
                    right[node] = len(sym) - 1
                node = right[node]
            else:
                if left[node] < 0:
                    left.append(-1)
                    right.append(-1)
                    sym.append(-1)
                    left[node] = len(sym) - 1
                node = left[node]
        sym[node] = ch
    out = bytearray()
    node = 0
    for b in src:
        for k in range(7, -1, -1):
            node = right[node] if ((b >> k) & 1) else left[node]
            if node < 0:
                return bytes(out) if out else b''
            if sym[node] >= 0:
                if len(out) >= capacity:
                    return b''
                out.append(sym[node])
                node = 0
    return bytes(out)
