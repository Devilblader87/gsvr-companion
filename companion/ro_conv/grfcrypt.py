"""GRF entry "encryption" (Gravity's DES variant), decode side. Stdlib only.

Written from the public descriptions of the GRF container (OpenKore / rAthena / roBrowser format notes, read as documentation; no
code copied). The cipher is ONE Feistel round of DES with an all-zero key (so no key schedule): IP, L ^= f(R) (E, S-boxes, P), FP,
on every 8 byte block that is selected. One round with a zero subkey is its own inverse (`des_block` both ways). Decode only: the converter never writes
encrypted archives (the synthetic tests carry their own encoder).

Entry flags (bit 0x01 = file):
  0x04 DES   : only the first 20 blocks are DES'd.
  0x02 MIXCRYPT: blocks with index < 20 or index % cycle == 0 are DES'd; of the other blocks every 8th one (counter j reaches 7)
             is byte-shuffled (`_unshuffle`, last byte through a small substitution). cycle depends on the decimal digit count
             of the entry's packed (compressed) size: 1 if < 3 digits, digits + 1 if < 5, digits + 9 if < 7, else digits + 15.
Tables below are the standard DES tables (FIPS 46), 1-based bit numbers, bit 1 = most significant.
"""

_IP = (58, 50, 42, 34, 26, 18, 10, 2, 60, 52, 44, 36, 28, 20, 12, 4, 62, 54, 46, 38, 30, 22, 14, 6, 64, 56, 48, 40, 32, 24, 16, 8,
       57, 49, 41, 33, 25, 17, 9, 1, 59, 51, 43, 35, 27, 19, 11, 3, 61, 53, 45, 37, 29, 21, 13, 5, 63, 55, 47, 39, 31, 23, 15, 7)
_FP = (40, 8, 48, 16, 56, 24, 64, 32, 39, 7, 47, 15, 55, 23, 63, 31, 38, 6, 46, 14, 54, 22, 62, 30, 37, 5, 45, 13, 53, 21, 61, 29,
       36, 4, 44, 12, 52, 20, 60, 28, 35, 3, 43, 11, 51, 19, 59, 27, 34, 2, 42, 10, 50, 18, 58, 26, 33, 1, 41, 9, 49, 17, 57, 25)
_P = (16, 7, 20, 21, 29, 12, 28, 17, 1, 15, 23, 26, 5, 18, 31, 10, 2, 8, 24, 14, 32, 27, 3, 9, 19, 13, 30, 6, 22, 11, 4, 25)
_S = (
    (14, 4, 13, 1, 2, 15, 11, 8, 3, 10, 6, 12, 5, 9, 0, 7, 0, 15, 7, 4, 14, 2, 13, 1, 10, 6, 12, 11, 9, 5, 3, 8,
     4, 1, 14, 8, 13, 6, 2, 11, 15, 12, 9, 7, 3, 10, 5, 0, 15, 12, 8, 2, 4, 9, 1, 7, 5, 11, 3, 14, 10, 0, 6, 13),
    (15, 1, 8, 14, 6, 11, 3, 4, 9, 7, 2, 13, 12, 0, 5, 10, 3, 13, 4, 7, 15, 2, 8, 14, 12, 0, 1, 10, 6, 9, 11, 5,
     0, 14, 7, 11, 10, 4, 13, 1, 5, 8, 12, 6, 9, 3, 2, 15, 13, 8, 10, 1, 3, 15, 4, 2, 11, 6, 7, 12, 0, 5, 14, 9),
    (10, 0, 9, 14, 6, 3, 15, 5, 1, 13, 12, 7, 11, 4, 2, 8, 13, 7, 0, 9, 3, 4, 6, 10, 2, 8, 5, 14, 12, 11, 15, 1,
     13, 6, 4, 9, 8, 15, 3, 0, 11, 1, 2, 12, 5, 10, 14, 7, 1, 10, 13, 0, 6, 9, 8, 7, 4, 15, 14, 3, 11, 5, 2, 12),
    (7, 13, 14, 3, 0, 6, 9, 10, 1, 2, 8, 5, 11, 12, 4, 15, 13, 8, 11, 5, 6, 15, 0, 3, 4, 7, 2, 12, 1, 10, 14, 9,
     10, 6, 9, 0, 12, 11, 7, 13, 15, 1, 3, 14, 5, 2, 8, 4, 3, 15, 0, 6, 10, 1, 13, 8, 9, 4, 5, 11, 12, 7, 2, 14),
    (2, 12, 4, 1, 7, 10, 11, 6, 8, 5, 3, 15, 13, 0, 14, 9, 14, 11, 2, 12, 4, 7, 13, 1, 5, 0, 15, 10, 3, 9, 8, 6,
     4, 2, 1, 11, 10, 13, 7, 8, 15, 9, 12, 5, 6, 3, 0, 14, 11, 8, 12, 7, 1, 14, 2, 13, 6, 15, 0, 9, 10, 4, 5, 3),
    (12, 1, 10, 15, 9, 2, 6, 8, 0, 13, 3, 4, 14, 7, 5, 11, 10, 15, 4, 2, 7, 12, 9, 5, 6, 1, 13, 14, 0, 11, 3, 8,
     9, 14, 15, 5, 2, 8, 12, 3, 7, 0, 4, 10, 1, 13, 11, 6, 4, 3, 2, 12, 9, 5, 15, 10, 11, 14, 1, 7, 6, 0, 8, 13),
    (4, 11, 2, 14, 15, 0, 8, 13, 3, 12, 9, 7, 5, 10, 6, 1, 13, 0, 11, 7, 4, 9, 1, 10, 14, 3, 5, 12, 2, 15, 8, 6,
     1, 4, 11, 13, 12, 3, 7, 14, 10, 15, 6, 8, 0, 5, 9, 2, 6, 11, 13, 8, 1, 4, 10, 7, 9, 5, 0, 15, 14, 2, 3, 12),
    (13, 2, 8, 4, 6, 15, 11, 1, 10, 9, 3, 14, 5, 0, 12, 7, 1, 15, 13, 8, 10, 3, 7, 4, 12, 5, 6, 11, 0, 14, 9, 2,
     7, 11, 4, 1, 9, 12, 14, 2, 0, 6, 10, 13, 15, 3, 5, 8, 2, 1, 14, 7, 4, 10, 8, 13, 15, 12, 9, 0, 3, 5, 6, 11))

# MIXCRYPT byte substitution (an involution: pairs swapped both ways)
_SUB = bytearray(range(256))
for _a, _b in ((0x00, 0x2B), (0x6C, 0x80), (0x01, 0x68), (0x48, 0x77), (0x60, 0xFF), (0xB9, 0xC0), (0xFE, 0xEB)):
    _SUB[_a], _SUB[_b] = _b, _a
_SUB = bytes(_SUB)


def _perm_bits(x, table, nin):
    r = 0
    for t in table:
        r = (r << 1) | ((x >> (nin - t)) & 1)
    return r


def _byte_tables(table):
    """64 -> 64 bit permutation as 8 x 256 lookups (one per input byte, ORed)."""
    out = []
    for pos in range(8):
        sh = 56 - 8 * pos
        out.append(tuple(_perm_bits(v << sh, table, 64) for v in range(256)))
    return tuple(out)


_IPT = _byte_tables(_IP)
_FPT = _byte_tables(_FP)
# S-box i output (4 bits at position 28 - 4i of the 32-bit word) already pushed through P, indexed by the 6-bit E group
_SP = tuple(tuple(_perm_bits(_S[i][(((six >> 4) & 2) | (six & 1)) * 16 + ((six >> 1) & 15)] << (28 - 4 * i), _P, 32)
                  for six in range(64)) for i in range(8))


def _perm64(x, t):
    return (t[0][x >> 56] | t[1][(x >> 48) & 255] | t[2][(x >> 40) & 255] | t[3][(x >> 32) & 255] |
            t[4][(x >> 24) & 255] | t[5][(x >> 16) & 255] | t[6][(x >> 8) & 255] | t[7][x & 255])


def des_block(b8):
    """One GRF DES round on 8 bytes (decrypt == encrypt)."""
    x = _perm64(int.from_bytes(b8, 'big'), _IPT)
    L, R = x >> 32, x & 0xFFFFFFFF
    X = ((R & 1) << 33) | (R << 1) | (R >> 31)          # E: 6-bit groups i = bits 28-4i.. of this 34-bit word
    f = 0
    for i in range(8):
        f |= _SP[i][(X >> (28 - 4 * i)) & 0x3F]
    return _perm64(((L ^ f) << 32) | R, _FPT).to_bytes(8, 'big')


def _unshuffle(b):
    return bytes((b[3], b[4], b[6], b[0], b[1], b[2], b[5], _SUB[b[7]]))


def mix_cycle(packed):
    d = len(str(packed))
    return 1 if d < 3 else d + 1 if d < 5 else d + 9 if d < 7 else d + 15


def _plan(n, flags, packed):
    """-> per block index: 'd' DES, 's' shuffle, None untouched."""
    if flags & 0x02:
        cyc = mix_cycle(packed)
        j = 0
        for i in range(n):
            if i < 20 or i % cyc == 0:
                yield i, 'd'
            else:
                if j == 7:
                    yield i, 's'
                    j = 0
                j += 1
    elif flags & 0x04:
        for i in range(min(20, n)):
            yield i, 'd'


def decode(buf, flags, packed):
    """Decrypt an entry's aligned bytes (returns a copy). flags: the GRF entry flags; packed: its compressed size."""
    if not flags & 0x06:
        return bytes(buf)
    out = bytearray(buf)
    for i, op in _plan(len(out) // 8, flags, packed):
        o = i * 8
        blk = bytes(out[o:o + 8])
        out[o:o + 8] = des_block(blk) if op == 'd' else _unshuffle(blk)
    return bytes(out)

