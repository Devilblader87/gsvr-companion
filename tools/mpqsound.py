#!/usr/bin/env python3
"""MPQ sound-only compressions: Huffman (mask 0x01) and ADPCM (0x40 mono / 0x80 stereo), Python stdlib only.

Why: a 1.2x MPQ stores .wav files with the sector compression 0x41 (Huffman over ADPCM, mono) or 0x81 (stereo), so a pack
built from a 1.21 / 1.26 install had almost no sounds (W3VR log: "sound: 5 clips" for Wintermaul, 74 from the 1.31 CASC
files).  BUGS SND-MPQ.

Clean room.  Everything here is written from scratch from (a) the public MPQ format description ([ext]: a sector's first
byte is the compression mask; Huffman = adaptive Huffman tree seeded with a 256 byte weight table per "tree type" 0..8 plus
an escape leaf 0x100 and an end-of-stream leaf 0x101, bits read LSB first; ADPCM = a 2 byte header, one 16 bit start
sample per channel, then one code byte per sample; the step table is the public IMA ADPCM step table) and (b) what the
shipped 1.21 / 1.26 sectors show when compared with the uncompressed 1.31 files ([obs]).  No code of StormLib or any
other implementation was read or copied.  The nine weight tables are DATA of the game's own Storm.dll: they are read from
the user's install at run time (the install's table sources / W3_STORM_DLL), found by their structure, and not stored in this repo.

[obs] rules, each checked against the real data (7592 mono + 2508 War3x sectors, 5847 stereo sectors with an uncompressed
reference, 6800 sectors of 498 fan maps; all of them tree type 7, masks 0x41 / 0x81 only):
  * tree build: leaves ordered by weight, equal weights: the larger symbol first, the escape leaf then the end leaf before
    any regular symbol of weight 1; repeatedly the two first nodes become the children (first = bit 0) of a parent that is
    inserted before the first remaining node of equal or larger weight; the last node is the root.
  * a decoded symbol only adds 1 to the weights on its path (no rebalancing).
  * escape: 8 raw bits (LSB first) are a new symbol.  The pair node q of the escape and end leaf gets a new parent u =
    (new leaf = bit 0, q = bit 1); the escape / end pair flips its order each time; u and the leaf go to the front of
    the rank order; the weights of the leaf and all its ancestors get +1 with the "slide / swap" step of _Tree._update;
    the escape symbol itself counts once on its path.  Two observed variants of this (REGIMES), see there.
  * ADPCM: code byte: bit 0x40 = sign, low `shift` bits = magnitude m (shift = header byte 1, only 4 is seen), step
    difference = (step >> shift) + sum over set bits i of (step >> i), index += ADJ4[m], start index 44; 0x80 repeats the
    sample and lowers the index by 1, 0x81 raises the index by 8 and writes nothing.  Stereo: the channels alternate per
    sample, each has its own sample / index.  Against the 1.31 files: SNR median 37 dB (mono, 3688 files), 40 dB (stereo).

Exactness is the safety net: a decoded sector must hit the end code with fewer than 8 padding bits left and produce exactly
sector_size / 2 samples; these checks are necessary, not sufficient: against the 1.31 files no decoded stereo file shows a wrong sector by SNR
(all 22 decoded game stereo files > 15 dB), but docs/BUGS.md SND-MPQ records 1 sector in ~5800 that passed them with
different samples (a swapped same-length code cannot be seen).

Safety (untrusted data): output never larger than the caller's limit, every loop is bounded by the input length or the
output limit, corrupt input raises ValueError (never IndexError / hangs), a missing weight table raises WeightsMissing.
"""
import os
import re
import struct

# --------------------------------------------------------------------------
# Huffman weight tables (nine 258 byte rows: 256 symbol weights + 2 unused escape / end slots)
# They are data of the game's own Storm.dll, loaded at run time from the user's install (not shipped in this repository).
# [obs] the rows sit back to back; row 0 has only symbols 0 and 255 set, row 4 symbols 0..15, row 5 symbols 0..63 and the
# last two bytes of every row are 0.  The block is found by that structure, not by a fixed file offset.
# --------------------------------------------------------------------------

ROW = 258
NTYPES = 9


class WeightsMissing(Exception):
    """No Storm.dll with the Huffman weight tables is reachable (set W3_STORM_DLL or register the install folder)."""


_cache = {}                      # file path -> list of 9 rows or None
_BLOCK0 = re.compile(rb'[\x01-\xff]\x00{254}[\x01-\xff]\x00\x00', re.S)
CHUNK = 4 * 1024 * 1024
OVERLAP = ROW * NTYPES + 512     # a block that straddles a chunk border is still whole in the next chunk


def _rows_at(data, o):
    rows = []
    for t in range(NTYPES):
        r = data[o + ROW * t:o + ROW * (t + 1)]
        if len(r) != ROW or r[256] or r[257]:
            return None
        rows.append(r[:256])
    nz4 = [i for i, b in enumerate(rows[4]) if b]
    nz5 = [i for i, b in enumerate(rows[5]) if b]
    if nz4 != list(range(16)) or nz5 != list(range(64)):
        return None
    if [i for i, b in enumerate(rows[0]) if b] != [0, 255]:
        return None
    return rows


def load_rows(path):
    """The nine weight rows of a file (Storm.dll of 1.2x, or the game exe of 1.31: bytes of 256) or None when the file has
    no such block.  The whole file is scanned in chunks (bounded memory, any file size)."""
    if path in _cache:
        return _cache[path]
    rows = None
    try:
        with open(path, 'rb') as fh:
            tail = b''
            while rows is None:
                new = fh.read(CHUNK)
                if not new:
                    break
                data = tail + new
                for m in _BLOCK0.finditer(data):
                    rows = _rows_at(data, m.start())
                    if rows:
                        break
                tail = data[-OVERLAP:]
    except OSError:
        rows = None
    _cache[path] = rows
    return rows


TEST_FALLBACK_DIRS = ()          # tests only: repo-relative input/war3*/ folders; production builds never look there


def _candidates(tables=()):
    """The table sources of ONE install (`tables`: its Storm.dll, or the exes of a 1.31 install, passed down by the
    caller, never process-global) and the env W3_STORM_DLL, so a pack never depends on the host it was built on and one
    install's file cannot leak into another's decode; TEST_FALLBACK_DIRS (a test hook) adds more."""
    out = []
    env = os.environ.get('W3_STORM_DLL')
    if env:
        out.append(os.path.join(env, 'Storm.dll') if os.path.isdir(env) else env)
    out.extend(tables or ())
    for d in TEST_FALLBACK_DIRS:
        out.append(os.path.join(d, 'Storm.dll'))
    return out


_logged = set()


def _log_once(msg):
    if msg not in _logged:
        _logged.add(msg)
        print('  sound: ' + msg)


def source(tables=()):
    """(path, rows) of the first candidate file that holds the tables, or (None, None)."""
    for p in _candidates(tables):
        if os.path.isfile(p):
            rows = load_rows(p)
            if rows:
                _log_once('Huffman weight tables from %s' % p)
                return p, rows
    return None, None


def weights(tree_type, tables=()):
    if not 0 <= tree_type < NTYPES:
        raise ValueError('huffman: tree type %d' % tree_type)
    p, rows = source(tables)
    if rows:
        return rows[tree_type]
    _log_once('no Storm.dll / no table block (1.2x MPQ sounds are skipped)')
    raise WeightsMissing('Huffman weight table %d: no Storm.dll found (set W3_STORM_DLL or install folder)' % tree_type)


# --------------------------------------------------------------------------
# Huffman tree
# --------------------------------------------------------------------------

ESC, END = 256, 257


class _Tree:
    """Binary tree in parallel lists.  `order` is the rank order (ascending weight); sibling nodes are adjacent."""
    __slots__ = ('w', 'par', 'c0', 'c1', 'sym', 'pos', 'order', 'root', 'leaf', 'pend', 'nadds', 'regime', 'seq')

    def __init__(self):
        self.w, self.par, self.c0, self.c1, self.sym, self.pos = [], [], [], [], [], []
        self.order = []
        self.root = -1
        self.leaf = {}
        self.pend = []                 # symbols decoded since the last flush(): count per leaf node
        self.nadds = 0                 # symbols inserted so far
        self.regime = 0                # which of the observed insertion behaviours the running insertion follows
        self.seq = (0,)                # behaviour per insertion (the last entry repeats), see REGIMES

    def _node(self, w, sym=-1):
        self.w.append(w)
        self.par.append(-1)
        self.c0.append(-1)
        self.c1.append(-1)
        self.sym.append(sym)
        self.pos.append(0)
        self.pend.append(0)
        return len(self.w) - 1

    @classmethod
    def build(cls, wts, seq=(0,)):
        t = cls()
        t.seq = seq
        t.regime = seq[0]
        items = [(wts[s], 1, -s, s) for s in range(256) if wts[s]]
        items.append((1, 0, 0, ESC))
        items.append((1, 0, 1, END))
        items.sort()
        order = []
        for w, _a, _b, s in items:
            n = t._node(w, s)
            t.leaf[s] = n
            order.append(n)
        w = t.w
        p = 0
        while len(order) - p > 1:
            a, b = order[p], order[p + 1]
            p += 2
            pw = w[a] + w[b]
            n = t._node(pw)
            t.c0[n], t.c1[n] = a, b
            t.par[a] = t.par[b] = n
            i = p
            while i < len(order) and w[order[i]] < pw:
                i += 1
            order.insert(i, n)
        t.order = order
        t.root = order[-1]
        for i, n in enumerate(order):
            t.pos[n] = i
        return t

    def clone(self):
        c = _Tree()
        c.w, c.par, c.c0, c.c1, c.sym, c.pos = (self.w[:], self.par[:], self.c0[:], self.c1[:], self.sym[:], self.pos[:])
        c.order = self.order[:]
        c.root = self.root
        c.leaf = dict(self.leaf)
        c.pend = self.pend[:]
        c.nadds = self.nadds
        c.regime = self.regime
        c.seq = self.seq
        return c

    def flush(self):
        """A decoded symbol adds 1 to every weight on its path, no rebalancing [obs].  The decoder only counts the
        leaf (pend) and settles the path weights here, right before a tree change needs them."""
        w, par, pend = self.w, self.par, self.pend
        for n in range(len(pend)):
            c = pend[n]
            if c:
                pend[n] = 0
                while n >= 0:
                    w[n] += c
                    n = par[n]

    def _swap(self, a, b):
        """Exchange the places of two nodes (neither is an ancestor of the other)."""
        par, c0, c1, pos, order = self.par, self.c0, self.c1, self.pos, self.order
        pa, pb = par[a], par[b]
        if pa == pb:
            c0[pa], c1[pa] = c1[pa], c0[pa]
        else:
            if c0[pa] == a:
                c0[pa] = b
            else:
                c1[pa] = b
            if c0[pb] == b:
                c0[pb] = a
            else:
                c1[pb] = a
            par[a], par[b] = pb, pa
        ia, ib = pos[a], pos[b]
        order[ia], order[ib] = b, a
        pos[a], pos[b] = ib, ia

    def _linked(self, a, b):
        """True when one node is an ancestor of the other (the parent included): such a pair is never swapped; a
        swap would make a cycle, which a corrupt or odd stream could otherwise use to hang the walk up to the root."""
        par = self.par
        x = par[a]
        while x >= 0:
            if x == b:
                return True
            x = par[x]
        x = par[b]
        while x >= 0:
            if x == a:
                return True
            x = par[x]
        return False

    def _update(self, n):
        """add 1 to n and every ancestor.  Before each: the run of the next nodes in rank order whose weight is at most
        n's new weight.  A run of leaves with pairwise different weights: n slides up past all of them (they move down
        one rank each); any other run: n swaps with the last node of the run [obs].  A node never moves past its own
        ancestors or descendants."""
        w, par, pos, order, c0 = self.w, self.par, self.pos, self.order, self.c0
        top = len(order) - 1
        steps = 0
        r0 = self.regime == 0
        while n >= 0:
            steps += 1
            if steps > len(order) + 2:
                raise ValueError('huffman: tree loop')
            i = pos[n]
            j = i
            wn = w[n] + 1
            lim = wn if r0 else wn - 1
            while j < top and w[order[j + 1]] <= lim:
                j += 1
            if j != i and not self._linked(n, order[j]):
                run = order[i + 1:j + 1]
                ws = [w[x] for x in run]
                if r0 and len(set(ws)) == len(ws) and all(c0[x] < 0 for x in run):
                    while pos[n] < j:
                        nxt = order[pos[n] + 1]
                        if self._linked(n, nxt):
                            break
                        self._swap(n, nxt)
                else:
                    self._swap(n, order[j])
            w[n] = wn
            n = par[n]

    def add(self, s):
        """Insert a new symbol [obs]: the escape/end pair node q gets a new parent u = (new leaf, q); the pair's
        order flips; the new nodes go to the front of the rank order ((leaf, u) for the first three symbols, (u, leaf)
        from the fourth on); then the update runs from the new leaf (weight 0) up; the escape symbol itself counts."""
        self.flush()
        self.regime = self.seq[self.nadds] if self.nadds < len(self.seq) else self.seq[-1]
        e = self.leaf[ESC]
        q = self.par[e]
        d = self.c1[q] if self.c0[q] == e else self.c0[q]
        nl = self._node(0, s)
        u = self._node(self.w[q])
        self.leaf[s] = nl
        g = self.par[q]
        if g >= 0:
            if self.c0[g] == q:
                self.c0[g] = u
            else:
                self.c1[g] = u
        else:
            self.root = u
        self.par[u] = g
        self.c0[u], self.c1[u] = nl, q
        self.par[q] = self.par[nl] = u
        if self.regime == 0:
            self.order[0:0] = [nl, u] if self.nadds < 3 else [u, nl]
        else:
            pq = self.pos[q]
            self.order.insert(pq, nl)
            self.order.insert(pq + 2, u)
        for i, x in enumerate(self.order):
            self.pos[x] = i
        self._swap(e, d)
        self._update(nl)
        if self.regime == 0:
            self.pend[e] += 1              # the escape symbol itself was decoded: +1 on its path
        self.nadds += 1


_templates = {}

# [obs] two insertion behaviours.  0: the common one (every mono sector of the game archives and 97% of the stereo
# ones).  1: the plain one (new nodes beside the escape pair, swap with the last node of equal weight, no escape
# count).  A sector is tried with the behaviour sequences below in turn (per insertion; the last entry repeats):
# (0,) the common one; (1,) the plain one decodes a further part of the stereo sectors whose header holds one of the
# lightest symbols 7 / 15 / 71 / 79; (0, 1, 0): plain only for the second insertion.  The end code and the sample
# count must come out exactly (a wrong tree does not), so a hit is never a guess.
REGIMES = ((0,), (1,), (0, 1, 0))


def _template(tree_type, seq=(0,), tables=()):
    w = weights(tree_type, tables)
    key = (w, tree_type, seq)                  # keyed by the rows themselves: no state shared between installs
    t = _templates.get(key)
    if t is None:
        t = _Tree.build(w, seq)
        _templates[key] = t
    return t


LUT_BITS = 8
_luts = {}                       # tree shape -> (node per 8 bit window, code length or 0 = longer)


def _lut(tree):
    """Decode table for the first LUT_BITS bits of a code.  Many sectors share one tree shape (the tree only changes
    at an escape), so the table is cached by shape; the leaves' symbols are not part of the key (they are looked up
    in the sector's own tree)."""
    key = (tuple(tree.c0), tuple(tree.c1))
    t = _luts.get(key)
    if t is None:
        if len(_luts) > 2000:
            _luts.clear()
        c0, c1 = tree.c0, tree.c1
        size = 1 << LUT_BITS
        nodes, lens = [0] * size, [0] * size
        stack = [(tree.root, 0, 0)]
        while stack:
            n, d, code = stack.pop()
            if c0[n] >= 0 and d < LUT_BITS:
                stack.append((c0[n], d + 1, code))
                stack.append((c1[n], d + 1, code | (1 << d)))
                continue
            ln = d if c0[n] < 0 else 0
            for k in range(1 << (LUT_BITS - d)):
                nodes[code | (k << d)] = n
                lens[code | (k << d)] = ln
        t = _luts[key] = (nodes, lens)
    return t


def huffman_decode(data, limit, regime=(0,), tables=()):
    """data = tree type byte + bit stream.  Returns the decoded bytes (at most `limit`)."""
    if len(data) < 2:
        raise ValueError('huffman: short input')
    tree = _template(data[0], tuple(regime), tables).clone()
    c0, c1, sym, pend = tree.c0, tree.c1, tree.sym, tree.pend
    out = bytearray()
    nbits = len(data) * 8
    pad = bytes(data) + b'\0\0\0'              # window reads past the end see zeros; pos > nbits is caught below
    pos = 8
    nodes, lens = _lut(tree)
    while True:
        i = pos >> 3
        v = ((pad[i] | (pad[i + 1] << 8)) >> (pos & 7)) & 255
        node = nodes[v]
        ln = lens[v]
        if ln:
            pos += ln
        else:                                     # a code longer than LUT_BITS: continue bit by bit
            pos += LUT_BITS
            while c0[node] >= 0:
                if pos >= nbits:
                    raise ValueError('huffman: truncated')
                node = c1[node] if (pad[pos >> 3] >> (pos & 7)) & 1 else c0[node]
                pos += 1
        if pos > nbits:
            raise ValueError('huffman: truncated')
        s = sym[node]
        if s == END:
            if nbits - pos >= 8 or (pad[pos >> 3] >> (pos & 7)) or any(pad[(pos >> 3) + 1:]):
                raise ValueError('huffman: padding after the end code')
            return bytes(out)
        if len(out) >= limit:
            raise ValueError('huffman: output exceeds %d' % limit)
        if s == ESC:
            if pos + 8 > nbits:
                raise ValueError('huffman: truncated')
            s = 0
            for k in range(8):
                s |= ((pad[pos >> 3] >> (pos & 7)) & 1) << k
                pos += 1
            if s in tree.leaf:
                raise ValueError('huffman: escaped symbol already in the tree')
            tree.add(s)
            nodes, lens = _lut(tree)
        else:
            pend[node] += 1
        out.append(s)


# --------------------------------------------------------------------------
# ADPCM (mask 0x40 mono, 0x80 stereo) -> 16 bit little endian PCM, channels interleaved
# --------------------------------------------------------------------------

# the public IMA ADPCM step table [ext]
IMA_STEPS = (
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45, 50, 55, 60, 66, 73, 80, 88, 97, 107,
    118, 130, 143, 157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724, 796, 876, 963,
    1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358, 5894,
    6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794,
    32767)
# [obs] step index change per magnitude (shift 4: 16 values), start index 44; measured on 7592 sectors (SNR 41 dB median
# against the uncompressed 1.31 files; every other tried value of the ADJ / 0x80 / 0x81 / base term is 5..50 dB worse)
ADJ4 = (-1, 0, -1, 4, -1, 2, -1, 6, -1, 1, -1, 5, -1, 3, -1, 7)
START_INDEX = 44
_tables = {}


def _delta_tables(shift):
    t = _tables.get(shift)
    if t is None:
        if shift != 4:
            raise ValueError('adpcm: shift %d not supported (only 4 was ever seen)' % shift)
        delta, nxt = [], []
        for idx, step in enumerate(IMA_STEPS):
            drow, nrow = [], []
            for m in range(16):
                d = step >> shift
                for i in range(shift):
                    if m & (1 << i):
                        d += step >> i
                drow.append(d)
                nrow.append(max(0, min(88, idx + ADJ4[m])))
            delta.append(drow)
            nxt.append(nrow)
        t = _tables[shift] = (delta, nxt)
    return t


def adpcm_decode(buf, channels, want):
    """buf = the ADPCM stream (header 0, shift, one s16 start sample per channel; then one code byte per sample).
    Returns exactly `want` bytes of s16 PCM or raises ValueError.  Control codes: 0x80 repeats the sample and lowers the
    step index by 1, 0x81 raises the index by 8 (no sample); stereo alternates the channels per sample and keeps one
    sample / index per channel [obs: both against the uncompressed 1.31 files]."""
    if channels not in (1, 2):
        raise ValueError('adpcm: channels')
    hdr = 2 + 2 * channels
    if len(buf) < hdr:
        raise ValueError('adpcm: short input')
    delta, nxt = _delta_tables(buf[1])
    nsamp = want // 2
    if nsamp < channels:
        raise ValueError('adpcm: sector too small')
    st = [struct.unpack_from('<h', buf, 2 + 2 * c)[0] for c in range(channels)]
    idx = [START_INDEX] * channels
    out = list(st)                                   # the start samples are the first output frame
    limit = nsamp
    if channels == 1:
        s, ix = st[0], START_INDEX
        n = 1
        append = out.append
        for b in buf[hdr:]:
            if b < 0x80:
                m = b & 15
                if b & 0x40:
                    s -= delta[ix][m]
                    if s < -32768:
                        s = -32768
                else:
                    s += delta[ix][m]
                    if s > 32767:
                        s = 32767
                ix = nxt[ix][m]
                append(s)
                n += 1
            elif b == 0x80:
                if ix:
                    ix -= 1
                append(s)
                n += 1
            elif b == 0x81:
                ix += 8
                if ix > 88:
                    ix = 88
            else:
                raise ValueError('adpcm: control code 0x%02x' % b)
            if n > limit:
                raise ValueError('adpcm: more samples than the sector holds')
    else:
        cur = 0
        n = channels
        for b in buf[hdr:]:
            if b < 0x80:
                m = b & 15
                if b & 0x40:
                    st[cur] = max(-32768, st[cur] - delta[idx[cur]][m])
                else:
                    st[cur] = min(32767, st[cur] + delta[idx[cur]][m])
                idx[cur] = nxt[idx[cur]][m]
                out.append(st[cur])
                n += 1
                cur ^= 1
            elif b == 0x80:
                if idx[cur]:
                    idx[cur] -= 1
                out.append(st[cur])
                n += 1
                cur ^= 1
            elif b == 0x81:
                idx[cur] = min(88, idx[cur] + 8)
            else:
                raise ValueError('adpcm: control code 0x%02x' % b)
            if n > limit:
                raise ValueError('adpcm: more samples than the sector holds')
    if n != limit:
        raise ValueError('adpcm: %d samples, the sector holds %d' % (n, limit))
    data = struct.pack('<%dh' % n, *out)
    return data + b'\0' * (want - len(data))
