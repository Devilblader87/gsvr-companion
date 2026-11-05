"""FOG: fog of war (docs/NOTES_fog.md, decompiled 1.21 + 1.26a; docs/BUGS.md FOG). Python reference; the Udon twin is
unity/Assets/W3VR/Scripts/W3Fog.cs (impl ids 1360-1371, task TK_FOG) plus W3Present.cs (the drawing, W3VR/Fog shader).

Clean room: behaviour only; tags [obs] observed in the game, [ext] common.j / Blizzard.j, [inf] our choice.

Model [obs NOTES_fog sec. 1-3]
  grid        two 16-bit layers per 128-unit cell: MASK (init 0xffff, permanent) and VISIBLE (init 0, rebuilt); one bit per
              player slot. Width/height = trunc(range / 128) of the map (world) rect; origin = its min corner.
              PORT14-Fog: gated by WORLD_GRID (default OFF) -- OFF keeps the legacy camera-bounds rect.
              Cell = clamp(trunc((v - origin) / 128), 0, size-1).
  state       A = VISIBLE | 0xf000, B = MASK & 0x0fff (slots 12..15: always visible, never masked); visible wins over masked,
              else masked, else fogged; then the 12-entry table by (mask flag, fog flag) (TABLE).
  writers     mode 4 (visible): MASK &= ~bits, VISIBLE |= bits; mode 1 (masked): MASK |= bits, VISIBLE &= ~bits; mode 2 (fogged):
              both &= ~bits; other values nothing. Rect fill half open (x0..x1-1, y0..y1-1, after clamping x0,y0 >= 0 and
              x1 <= w-1, y1 <= h-1); radius r = trunc(radius / 128): 0 nothing, 1 and 2 the 2x2 block (cx-1..cx, cy-1..cy)
              (+ the four single cells at distance 2 for r = 2, coordinates & 0xff, no clamp), r >= 3 a circle of inclusive spans.
  modifiers   rebuilt every 4th update (NOTES_fog 1.6): VISIBLE layer wiped, then the started
              modifiers without "after units", the unit sight, the started modifiers with "after units".
  FogEnable(false) wipes VISIBLE; FogMaskEnable never touches a layer.

What the port does differently (all [inf], bounded Udon cost)
  * the grid exists only once something needs it (a SetFogState*, a query, FogEnable(true) / FogMaskEnable(true), or a
    CreateFogModifier* of a known player); a map that never touches fog pays nothing and its traces are unchanged.
  * a created modifier is applied once immediately, with the shared-vision expansion of its player mask whatever the
    useSharedVision argument says (CFogModifier_p1 sec. 8.4): a created but never started VISIBLE shape shows until the
    next rebuild, a MASKED one sticks (the MASK layer is permanent).
  * the whole update of NOTES_fog 1.6 (wipe, modifiers, sight) runs once per 8 turns (turn & 7 == 0: 200 ms at game speed fast) instead of 4
    updates of one turn each: the Udon cost is the cells written, so the period is the knob (a one-shot VISIBLE write now lasts up to 200 ms).
  * while every started modifier is VISIBLE the update is incremental: each source (unit, modifier) keeps per-slot counters of the cells
    that see it and only the difference to the last update is written (a moved circle touches its rim); the result is the layers of
    the wipe and rewrite (`reference`), which runs while a MASKED / FOGGED modifier is started and after anything the counters do not
    know (one-shot SetFogState*, FogEnable(false), the first query of a slot, a skipped update). test_incremental_equals_reference.
  * the update is skipped while FogEnable and FogMaskEnable are both off (every query answers visible then, table row 0).
  * unit sight and modifier writes are made for the INTEREST slots only: the user slots that play, and every slot a query
    asked about (the first query of a new slot applies the modifiers and the units' sight for that slot at once). Writes of a
    one-shot SetFogState* always cover the player (or its shared-vision mask) in full.
  * unit sight = the type's day / night sight (UnitBalance sight / nsight, day = 06:00 .. 18:00), centred on the unit's cell.
    The radius is sight / 128 rounded to the nearest integer, halves up, at most 14 (SPEC-0032 "Radius"; EXT-ow3 SPEC-0032:
    the sight radius is not in the decompile, NOTES_fog sec. 5 is open). Ground and hovering types reveal the asymmetric
    3x3-plus-rings mask of SPEC-0032 "The mask" (EXT-ow3 SPEC-0032; docs/decomp/classes/CFogMask_p1.md sec. 6.1 has the 3x3
    core and the dependency rings but not the exact shape); flying types (movetp bit 2) reveal the whole circle. The circle
    half widths are Bresenham's S(d) of SPEC-0032 "The circle" (EXT-ow3 SPEC-0032; the decompile has such a table in
    0x6f30a790 but not its numbers). There are no height / cliff / tree rules yet (NOTES_fog sec. 5 is open).
    Units that are dead or hidden have no sight. It clears the MASK bit (explored) [inf].
  * consecutive modifiers of the same shape and state in one phase are applied once with the OR of their bits (the same result).
  * IsUnitVisible = not hidden, then the stealth gate of NOTES_fog 3.3 (an invisible unit needs owner / ally / a point
    reveal or detector of the asked player; PORT14-Fog), then "owned by the player [inf] or the unit's cell reads
    visible"; IsUnitFogged / IsUnitMasked compare the cell state only.
"""
import math
import os

from .vm import Agent, r32
from .natives import nat, Player, Unit, Rect, Location, tod_now, slot_to_jass

SC = 0.0078125                    # 1 / 128, exact in float32
V, F, M = 4, 2, 1                 # fog state values (FOG_OF_WAR_VISIBLE, FOGGED, MASKED)
# NOTES_fog 1.4: [mask flag + 2 * fog flag][raw 0 = visible, 1 = fogged, 2 = masked]
TABLE = ((4, 4, 4), (4, 4, 1), (4, 2, 2), (4, 2, 1))
ALLIANCE_SHARED_VISION = 5

FORCE_REFERENCE = False           # tests: always the wipe-and-rewrite update (the incremental one must give the same layers)
WORLD_GRID = os.environ.get('W3_FOG_WORLD_GRID') == '1'   # PORT14-Fog, default OFF: the grid covers the map (world) rectangle
_HW = {}
_MASK = {}
SIGHT_MAX = 14                    # SPEC-0032 "Radius": the fog radius is capped at 14 cells


def grid_bounds(N):
    """the world rectangle the fog grid is built from (NOTES_fog 1.2, CFogOfWarMap_p1 10).

    The engine builds it from the map/world rectangle 0x6f075e40 (the same record the wander clamp and the pathing grid
    use); the grid is therefore fixed at map load and a script's SetCameraBounds never moves or resizes it.
    PORT14-Fog: OFF keeps the legacy camera-bounds model (the port's [inf], golden traces and fun-map fog unchanged);
    ON uses the pathing grid's map rectangle (its cells are 32 units, the fog cells 128, so w/4 x h/4)."""
    if WORLD_GRID:
        gr = getattr(getattr(N, 'eng', None), 'grid', None)
        if gr is not None and gr.w >= 4 and gr.h >= 4:
            return (gr.ox, gr.oy, gr.ox + gr.w * 32.0, gr.oy + gr.h * 32.0)
    return N.camera_bounds


def hw(r, dy):
    """half width S(dy) (cells) of a circle of radius r, from Bresenham's midpoint circle: take the points (x, y) of
    x = 0, y = r, e = 3 - 2r; after each point e += 4x + 6 when e < 0, else e += 4(x - y) + 10 and y -= 1; x += 1 while
    x <= y; add the mirror images (y, x); S(d) is the largest first coordinate of a point whose second is d.
    EXT-ow3 SPEC-0032 "The circle": the shipped table 0x6f30a790 is built by such an incremental integer scan
    (docs/decomp/classes/CFogMask_p1.md sec. 6.3) but its numbers were not decoded; the spec's table matches this."""
    t = _HW.get(r)
    if t is None:
        t = [-1] * (r + 1)
        x, y, e = 0, r, 3 - 2 * r
        while x <= y:
            if x <= r and x > t[y]:
                t[y] = x
            if y <= r and y > t[x]:
                t[x] = y
            if e < 0:
                e += 4 * x + 6
            else:
                e += 4 * (x - y) + 10
                y -= 1
            x += 1
        _HW[r] = t
    if dy > r or dy < 0:
        return 0
    return t[dy]


def mask_rows(r):
    """[(dy, dx_min, dx_max)] of the reveal mask of a ground unit of radius r >= 3, rows dy = r down to -r:
    the 3x3 cells, then for every ring k = 1 .. r with L = min(k, S(k)) the cells (k, b) for b = 0 .. L and
    (k, -b) for b = 1 .. L - 1 and the same two sets turned by 90, 180 and 270 degrees [(dx, dy) -> (-dy, dx)].
    EXT-ow3 SPEC-0032 "The mask"; the rows match SPEC-0032-masks.tsv for r = 3 .. 14."""
    t = _MASK.get(r)
    if t is None:
        rows = {}

        def add(dx, dy):
            e = rows.get(dy)
            if e is None:
                rows[dy] = [dx, dx]
            else:
                if dx < e[0]:
                    e[0] = dx
                if dx > e[1]:
                    e[1] = dx

        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                add(dx, dy)
        for k in range(1, r + 1):
            lo = k
            s = hw(r, k)
            if s < lo:
                lo = s
            b = 0
            while b <= lo:
                dx, dy = k, b
                for _q in range(4):
                    add(dx, dy)
                    dx, dy = -dy, dx
                b += 1
            b = 1
            while b < lo:
                dx, dy = k, -b
                for _q in range(4):
                    add(dx, dy)
                    dx, dy = -dy, dx
                b += 1
        t = [(dy, rows[dy][0], rows[dy][1]) for dy in range(r, -r - 1, -1)]
        _MASK[r] = t
    return t


def sight_cells(sight):
    """the fog radius of a unit's sight: sight / 128 rounded to the nearest integer, halves up, at most 14; <= 0 -> 0.
    EXT-ow3 SPEC-0032 "Radius" (the decompile has no sight radius yet, NOTES_fog sec. 5)."""
    t = r32(sight) * SC
    if not t > 0.0:
        return 0
    n = int(math.floor(t + 0.5))
    return SIGHT_MAX if n > SIGHT_MAX else n


def unit_sight(u, day):
    """the type's day / night sight plus the rsig research total (CUpgradeSight §6: the delta lands on the unit)."""
    key = 'sight' if day else 'nsight'
    return sight_cells(float(u.data.get(key, 0) or 0) + float(getattr(u, 'upg_sight', 0.0) or 0.0))


def ci(t, n):
    """clamp(trunc(t), 0, n - 1); NaN -> 0."""
    if not t > 0.0:
        return 0
    return n - 1 if t >= n else int(t)


def rad_cells(radius):
    """trunc(radius / 128), capped at 255; NaN / negative -> 0."""
    t = r32(radius) * SC
    if not t > 0.0:
        return 0
    return 255 if t >= 255.0 else int(t)


def shared_bits(N, pl):
    """the slots that see what pl sees: its own bit + every player pl grants ALLIANCE_SHARED_VISION [inf, NOTES_fog 2.1]."""
    bits = 1 << pl.id
    for (hq, t), v in pl.alliance.items():
        if t == ALLIANCE_SHARED_VISION and v:
            q = N.vm.H.get(hq)
            if isinstance(q, Player):
                bits |= 1 << q.id
    return bits


class FogMod(Agent):
    """a fog modifier (CFogModifier); kind/pid/state/a..d/on feed natives.fog_lines (td_score display state). `loc` is
    the Location of the RadiusLoc form: the shape is read live from it (CFogModifier_p1 sec. 2.1), a and b only keep its
    creation coordinates for the display."""
    tname = 'fogmodifier'

    def __init__(self, pid, bits, state, kind, a, b, c, d, shared, after, loc=None, shape=None):
        self.pid, self.bits, self.state, self.kind = pid, bits, state, kind
        self.a, self.b, self.c, self.d = a, b, c, d
        # CFogModifier_p1 sec. 2 / sec. 8: the constructors OR the WHOLE fogstate argument into the flags word, so a
        # non-canonical value also carries the started / shared / after-units bits (0x40 / 0x80 / 0x100).
        self.shared = bool(shared) or bool(state & 0x80)
        self.after = bool(after) or bool(state & 0x100)
        self.loc = loc
        self.shape = shape if shape is not None else (0x08 if kind == 'R' else (0x20 if loc is not None else 0x10))
        self.on = bool(state & 0x40)

    def center(self):
        return (self.loc.x, self.loc.y) if self.loc is not None else (self.a, self.b)

    def apply_state(self):
        """the fogstate the applier really writes: flags = state | the constructor's shape bit, dispatch on
        flags & 0x38 (only that shape survives), then the low 3 bits (CFogModifier_p1 sec. 2 / sec. 6). 0 = nothing."""
        flags = self.state | self.shape
        if flags & 0x38 != self.shape:
            return 0
        return flags & 7


class Fog:
    def __init__(self, N):
        self.N = N
        a, b, c, d = grid_bounds(N)
        self.ox, self.oy = r32(a), r32(b)
        self.w = max(1, min(512, ci(r32(r32(c) - self.ox) * SC, 1 << 20)))
        self.h = max(1, min(512, ci(r32(r32(d) - self.oy) * SC, 1 << 20)))
        n = self.w * self.h
        self.mask = [0xffff] * n
        self.vis = [0] * n
        self.vmin = [self.w] * self.h          # per row: extent of the cells VISIBLE may be non-zero in
        self.vmax = [-1] * self.h
        self.queried = 0
        self.rebuilds = 0
        self.cnt = [None] * 16                 # per slot: cells' number of sources that see them (the incremental sight)
        self.src = {}                          # source (a unit or a started VISIBLE modifier) -> the shape it was added with
        self.cnt_ok = False                    # counters, sources and VISIBLE agree (else the next update starts from the wipe)
        self.unit_added_on = True               # EXT-ow3 SPEC-0032: a created unit reveals at once (Fog.unit_added), default ON since the 1.26a
                                               # oracle fog_create_reveal [obs runtime 1.26a] (docs/oracle.md "created unit reveals sight at once":
                                               # visible 0.000 s after CreateUnit, 600 u). False = the old rule (the next periodic update reveals).
                                               # The twin is W3Fog.fgUnitAddedOn.
        N.vm.on_turn.append(self.on_turn)

    # ------------------------------------------------------------------ cells
    def cell(self, x, y):
        return ci(r32(r32(x) - self.ox) * SC, self.w), ci(r32(r32(y) - self.oy) * SC, self.h)

    def raw(self, cx, cy, bit):
        i = cy * self.w + cx
        if (self.vis[i] | 0xf000) & bit:
            return 0
        return 2 if (self.mask[i] & 0x0fff) & bit else 1

    def state(self, cx, cy, bit):
        N = self.N
        return TABLE[(1 if N.fog[1] else 0) + (2 if N.fog[0] else 0)][self.raw(cx, cy, bit)]

    # ------------------------------------------------------------------ writers
    def span(self, y, s, e, mode, bits):
        """cells s..e (inclusive, in range) of row y."""
        base = y * self.w
        mk, vs = self.mask, self.vis
        nb = ~bits & 0xffff
        if mode == 4:
            for i in range(base + s, base + e + 1):
                mk[i] &= nb
                vs[i] |= bits
            if s < self.vmin[y]:
                self.vmin[y] = s
            if e > self.vmax[y]:
                self.vmax[y] = e
        elif mode == 1:
            for i in range(base + s, base + e + 1):
                mk[i] |= bits
                vs[i] &= nb
        elif mode == 2:
            for i in range(base + s, base + e + 1):
                mk[i] &= nb
                vs[i] &= nb

    def rect(self, mode, bits, x0, y0, x1, y1):
        if mode != 1 and mode != 2 and mode != 4:
            return
        x0 = max(x0, 0)
        y0 = max(y0, 0)
        x1 = min(x1, self.w - 1)
        y1 = min(y1, self.h - 1)
        if x1 - 1 < x0:
            return
        for y in range(y0, y1):
            self.span(y, x0, x1 - 1, mode, bits)

    def radius(self, mode, bits, cx, cy, r):
        if (mode != 1 and mode != 2 and mode != 4) or r <= 0:
            return
        if r < 3:
            self.rect(mode, bits, cx - 1, cy - 1, cx + 1, cy + 1)
            if r == 2:
                for sx, sy in ((cx - 2, cy), (cx + 2, cy), (cx, cy - 2), (cx, cy + 2)):
                    sx &= 0xff
                    sy &= 0xff
                    if sx < self.w and sy < self.h:
                        self.span(sy, sx, sx, mode, bits)
            return
        for y in range(max(0, cy - r), min(self.h - 1, cy + r) + 1):
            k = hw(r, abs(y - cy))
            s, e = max(0, cx - k), min(self.w - 1, cx + k)
            if s <= e:
                self.span(y, s, e, mode, bits)

    def mask_write(self, mode, bits, cx, cy, r):
        """the reveal mask of a ground unit (SPEC-0032 "The mask"), written like radius(): rows dy = r .. -r, the cells
        cx + dx_min .. cx + dx_max, clipped. EXT-ow3 SPEC-0032."""
        if mode != 1 and mode != 2 and mode != 4:
            return
        if r < 3:
            self.radius(mode, bits, cx, cy, r)
            return
        w, h = self.w, self.h
        for dy, s, e in mask_rows(r):
            y = cy + dy
            if y < 0 or y >= h:
                continue
            s += cx
            e += cx
            if s < 0:
                s = 0
            if e > w - 1:
                e = w - 1
            if s <= e:
                self.span(y, s, e, mode, bits)

    def wipe(self):
        """VISIBLE to zero (and the sight counters: they are non-zero only where VISIBLE may be); the sources are forgotten."""
        vs = self.vis
        cs = [c for c in self.cnt if c is not None]
        for y in range(self.h):
            lo, hi = self.vmin[y], self.vmax[y]
            if hi >= lo:
                base = y * self.w
                for i in range(base + lo, base + hi + 1):
                    vs[i] = 0
                for c in cs:
                    for i in range(base + lo, base + hi + 1):
                        c[i] = 0
                self.vmin[y] = self.w
                self.vmax[y] = -1
        self.src.clear()
        self.cnt_ok = False

    # ------------------------------------------------------------------ the shared-vision masks
    def shared_bits(self, pl):
        """the slots that see what pl sees (see shared_bits above)."""
        return shared_bits(self.N, pl)

    def expand(self, bits):
        """the shared-vision expansion of a 16-bit player mask: the OR of the players' shared bits over every set slot
        (the applier's mask -1 mode, CFogModifier_p1 sec. 6)."""
        out = 0
        for pl in self.N.players:
            if (bits >> pl.id) & 1:
                out |= shared_bits(self.N, pl)
        return out

    def mod_bits(self, m):
        return m.bits if not m.shared else self.expand(m.bits)

    # ------------------------------------------------------------------ the rebuild (NOTES_fog 1.6)
    def interest(self):
        out = self.queried
        for pl in self.N.players[:12]:
            if pl.controller == 0 and pl.slot_state == 1:
                out |= 1 << pl.id
        return out

    def on_turn(self, turn):
        if turn & 7 == 0:
            self.periodic()

    def periodic(self):
        """the update of NOTES_fog 1.6, once per 8 turns. With only VISIBLE modifiers (the usual case) the sources' sight is
        kept in per-slot counters and only what changed since the last update is written (the same layers as the wipe and
        rewrite, which `reference` does and which any MASKED / FOGGED modifier needs); a one-shot write, FogEnable(false), a
        new interest slot or an update with both flags off make the next update start from the wipe."""
        N = self.N
        if not N.fog[0] and not N.fog[1]:
            self.cnt_ok = False
            return
        interest = self.interest()
        mods = [m for m in N.fogmods if m.on]
        self.rebuilds += 1
        if FORCE_REFERENCE or any(m.apply_state() in (1, 2) for m in mods):
            self.reference(mods, interest, True)
            return
        if not self.cnt_ok:
            self.wipe()
        self.incremental(mods, interest)
        self.cnt_ok = True

    def reference(self, mods, interest, wipe):
        """wipe, modifiers without "after units", the sight of every unit, modifiers with "after units" (NOTES_fog 1.6)."""
        if wipe:
            self.wipe()
        else:
            self.cnt_ok = False                # direct writes the counters do not know
        self.apply_mods([m for m in mods if not m.after], interest)
        self.apply_units(interest)
        self.apply_mods([m for m in mods if m.after], interest)

    def rebuild_now(self, interest):
        """the sources' writes for these slots at once, no wipe (the first query of a slot)."""
        N = self.N
        if not N.fog[0] and not N.fog[1]:
            return
        self.rebuilds += 1
        self.reference([m for m in N.fogmods if m.on], interest, False)

    def unit_added(self, u):
        """a created unit reveals its sight at once [EXT-ow3 SPEC-0032 "Updates": a unit reveals when it is created; the
        decompile has no owner for the create-time fog call yet]. The unit is added as a source for every interest slot,
        so the next periodic update finds it unchanged; without this it was blind until that update (up to 8 turns).
        Switch-gated: unit_added_on, default OFF (the old rule: the periodic update reveals)."""
        if not self.unit_added_on:
            return
        N = self.N
        if N.fogg is not self or (not N.fog[0] and not N.fog[1]):
            return
        o = u.owner
        if o is None or u.dead or u.hidden:
            return
        interest = self.interest()
        if not interest:
            return
        day = 6.0 <= tod_now(N) < 18.0
        r = unit_sight(u, day)
        if r <= 0:
            return
        bits = self.shared_bits(o) & interest
        if u.share_vis:
            bits = bits | (u.share_vis & interest)
        if not bits or u in self.src:
            return
        cx, cy = self.cell(u.x, u.y)
        st = self.unit_shape(u, cx, cy, r, bits)
        self.src_apply(st, 1)
        self.src[u] = st

    # ------------------------------------------------------------------ incremental sight
    def cspan(self, y, s, e, bits, d):
        """the counters of cells s..e (inclusive) of row y by d (+1 / -1) for every slot of bits: VISIBLE follows 0 <-> 1, an
        add clears MASK (explored)."""
        base = y * self.w
        mk, vs = self.mask, self.vis
        k = 0
        b = bits
        while b:
            if b & 1:
                c = self.cnt[k]
                if c is None:
                    c = self.cnt[k] = [0] * (self.w * self.h)
                bit = 1 << k
                nb = ~bit & 0xffff
                if d > 0:
                    for i in range(base + s, base + e + 1):
                        n = c[i]
                        c[i] = n + 1
                        if n == 0:
                            vs[i] |= bit
                        mk[i] &= nb
                else:
                    for i in range(base + s, base + e + 1):
                        n = c[i] - 1
                        c[i] = n
                        if n == 0:
                            vs[i] &= nb
            b >>= 1
            k += 1
        if d > 0:
            if s < self.vmin[y]:
                self.vmin[y] = s
            if e > self.vmax[y]:
                self.vmax[y] = e

    def shape_spans(self, st):
        """[(row, first, last)] of a source shape, the cells rect() / radius() / mask_write() write: ('R', x0, y0, x1, y1, bits),
        ('C', cx, cy, r, bits) or ('M', cx, cy, r, bits)."""
        out = []
        if st[0] == 'R':
            x0, y0, x1, y1 = max(st[1], 0), max(st[2], 0), min(st[3], self.w - 1), min(st[4], self.h - 1)
            if x1 - 1 >= x0:
                for y in range(y0, y1):
                    out.append((y, x0, x1 - 1))
            return out
        cx, cy, r = st[1], st[2], st[3]
        if r <= 0:
            return out
        if st[0] == 'M' and r >= 3:
            for dy, a, b in mask_rows(r):
                y = cy + dy
                if 0 <= y < self.h:
                    s = max(cx + a, 0)
                    e = min(cx + b, self.w - 1)
                    if s <= e:
                        out.append((y, s, e))
            return out
        if r < 3:
            x0, y0, x1, y1 = max(cx - 1, 0), max(cy - 1, 0), min(cx + 1, self.w - 1), min(cy + 1, self.h - 1)
            if x1 - 1 >= x0:
                for y in range(y0, y1):
                    out.append((y, x0, x1 - 1))
            if r == 2:
                for sx, sy in ((cx - 2, cy), (cx + 2, cy), (cx, cy - 2), (cx, cy + 2)):
                    sx &= 0xff
                    sy &= 0xff
                    if sx < self.w and sy < self.h:
                        out.append((sy, sx, sx))
            return out
        for y in range(max(0, cy - r), min(self.h - 1, cy + r) + 1):
            k = hw(r, abs(y - cy))
            s, e = max(0, cx - k), min(self.w - 1, cx + k)
            if s <= e:
                out.append((y, s, e))
        return out

    def src_apply(self, st, d):
        for y, s, e in self.shape_spans(st):
            self.cspan(y, s, e, st[-1], d)

    def src_change(self, old, new):
        """old -> new: a source of the same kind, radius and slots that moved (r >= 3) only touches the cells that differ
        per row (a circle or a mask)."""
        if old[0] == new[0] and old[3] == new[3] and old[4] == new[4] and old[3] >= 3:
            bits = old[4]
            om = {y: (s, e) for y, s, e in self.shape_spans(old)}
            nm = {y: (s, e) for y, s, e in self.shape_spans(new)}
            for y in sorted(set(om) | set(nm)):
                o, n = om.get(y), nm.get(y)
                if o is not None and n is not None:
                    s0, e0 = o
                    s1, e1 = n
                    t = min(e0, s1 - 1)                       # old minus new: the part left of new, the part right of it
                    if s0 <= t:
                        self.cspan(y, s0, t, bits, -1)
                    t = max(s0, e1 + 1)
                    if t <= e0:
                        self.cspan(y, t, e0, bits, -1)
                    t = min(e1, s0 - 1)                       # new minus old
                    if s1 <= t:
                        self.cspan(y, s1, t, bits, 1)
                    t = max(s1, e0 + 1)
                    if t <= e1:
                        self.cspan(y, t, e1, bits, 1)
                elif o is not None:
                    self.cspan(y, o[0], o[1], bits, -1)
                else:
                    self.cspan(y, n[0], n[1], bits, 1)
            return
        self.src_apply(old, -1)
        self.src_apply(new, 1)

    def incremental(self, mods, interest):
        N = self.N
        cur = {}
        for m in mods:
            if m.apply_state() != 4:
                continue
            bits = self.mod_bits(m) & interest
            if not bits:
                continue
            if m.kind == 'R':
                x0, y0 = self.cell(m.a, m.b)
                x1, y1 = self.cell(m.c, m.d)
                cur[m] = ('R', x0, y0, x1, y1, bits)
            else:
                mx, my = m.center()
                cx, cy = self.cell(mx, my)
                cur[m] = ('C', cx, cy, rad_cells(m.c), bits)
        day = 6.0 <= tod_now(N) < 18.0
        shared = {}
        for u in N.units:
            o = u.owner
            if o is None or u.dead or u.hidden:
                continue
            r = unit_sight(u, day)
            if r <= 0:
                continue
            bits = shared.get(o.id)
            if bits is None:
                bits = shared[o.id] = self.shared_bits(o) & interest
            if u.share_vis:
                bits = bits | (u.share_vis & interest)       # PORT5-NativesB: UnitShareVision slots (0032 §4)
            if not bits:
                continue
            cx, cy = self.cell(u.x, u.y)
            cur[u] = self.unit_shape(u, cx, cy, r, bits)
        src = self.src
        for k, old in list(src.items()):
            new = cur.get(k)
            if new is None:
                self.src_apply(old, -1)
                del src[k]
            elif new != old:
                self.src_change(old, new)
                src[k] = new
        for k, st in cur.items():
            if k not in src:
                self.src_apply(st, 1)
                src[k] = st

    def apply_mods(self, mods, interest):
        pend = None
        for m in mods:
            st = m.apply_state()
            if not st:
                continue
            bits = self.mod_bits(m) & interest
            if not bits:
                continue
            if m.kind == 'R':
                x0, y0 = self.cell(m.a, m.b)
                x1, y1 = self.cell(m.c, m.d)
                key = ('R', st, x0, y0, x1, y1)
            else:
                mx, my = m.center()
                cx, cy = self.cell(mx, my)
                key = ('C', st, cx, cy, rad_cells(m.c))
            if pend is not None and pend[0] == key:
                pend[1] |= bits
                continue
            if pend is not None:
                self.fire(pend)
            pend = [key, bits]
        if pend is not None:
            self.fire(pend)

    def fire(self, p):
        k, bits = p
        if k[0] == 'R':
            self.rect(k[1], bits, k[2], k[3], k[4], k[5])
        else:
            self.radius(k[1], bits, k[2], k[3], k[4])

    def apply_units(self, interest):
        N = self.N
        day = 6.0 <= tod_now(N) < 18.0
        shared = {}
        for u in N.units:
            o = u.owner
            if o is None or u.dead or u.hidden:
                continue
            r = unit_sight(u, day)
            if r <= 0:
                continue
            bits = shared.get(o.id)
            if bits is None:
                bits = shared[o.id] = self.shared_bits(o) & interest
            if u.share_vis:
                bits = bits | (u.share_vis & interest)       # PORT5-NativesB: UnitShareVision slots (0032 §4)
            if not bits:
                continue
            cx, cy = self.cell(u.x, u.y)
            st = self.unit_shape(u, cx, cy, r, bits)
            if st[0] == 'M':
                self.mask_write(4, bits, cx, cy, r)
            else:
                self.radius(4, bits, cx, cy, r)

    @staticmethod
    def unit_shape(u, cx, cy, r, bits):
        """the source shape of a unit's sight: ('M', ...) the mask of a ground / hovering type with r >= 3, else the
        circle ('C', ...) of a flying type (movetp bit 2, EXT-ow3 SPEC-0032) or of a radius below 3."""
        if r >= 3 and not (int(u.data.get('movetp', 0) or 0) & 0x2):
            return ('M', cx, cy, r, bits)
        return ('C', cx, cy, r, bits)

    def want(self, bit):
        """a query asks about this slot: from now on its sight is maintained; the first time it is applied at once."""
        if self.queried & bit:
            return
        new = bit & ~self.interest()
        self.queried |= bit
        if new:
            self.rebuild_now(new)


def grid(N):
    """the grid, created on first use (the first rebuild is immediate and covers all interest slots)."""
    g = N.fogg
    if g is None:
        g = N.fogg = Fog(N)
        g.periodic()
    return g


def _bits(N, pl, shared):
    g = grid(N)
    return g.shared_bits(pl) if shared else 1 << pl.id


# ---------------------------------------------------------------------------------------------- natives
@nat('IsFogEnabled')
def _fogon(N):
    return N.fog[0]


@nat('IsFogMaskEnabled')
def _fogmaskon(N):
    return N.fog[1]


@nat('FogEnable')
def _fogenable(N, b):
    b = bool(b)
    if not b and N.fogg is not None:
        N.fogg.wipe()
    N.fog[0] = b
    if b:
        grid(N)


@nat('FogMaskEnable')
def _fogmask(N, b):
    b = bool(b)
    N.fog[1] = b
    if b:
        grid(N)


@nat('SetFogStateRect')
def _setfogrect(N, p, state, r, shared):
    pl, ro = N.o(p, Player), N.o(r, Rect)
    if pl is None or ro is None:
        return
    g = grid(N)
    g.cnt_ok = False                     # a write the sight counters do not know: the next update starts from the wipe
    x0, y0 = g.cell(ro.minx, ro.miny)
    x1, y1 = g.cell(ro.maxx, ro.maxy)
    g.rect(state, _bits(N, pl, shared), x0, y0, x1, y1)


def _fogradius(N, pl, state, x, y, rad, shared):
    g = grid(N)
    g.cnt_ok = False
    cx, cy = g.cell(x, y)
    g.radius(state, _bits(N, pl, shared), cx, cy, rad_cells(rad))


@nat('SetFogStateRadius')
def _setfograd(N, p, state, x, y, rad, shared):
    pl = N.o(p, Player)
    if pl is not None:
        _fogradius(N, pl, state, x, y, rad, shared)


@nat('SetFogStateRadiusLoc')
def _setfogradloc(N, p, state, l, rad, shared):
    pl, lo = N.o(p, Player), N.o(l, Location)
    if pl is not None and lo is not None:
        _fogradius(N, pl, state, lo.x, lo.y, rad, shared)


def _fogmod(N, p, state, kind, a, b, c, d, shared, after, loc=None, shape=None):
    pl = N.o(p, Player)
    m = FogMod(slot_to_jass(N, pl.id) if pl else -1, 1 << pl.id if pl else 0, state, kind, a, b, c, d, shared, after,
               loc, shape)
    N.fogmods.append(m)
    _fogmod_once(N, m)                   # CFogModifier_p1 sec. 8.4: the constructor applies the shape once
    return m


def _fogmod_once(N, m):
    """the immediate write of a new modifier (0x6f2e8e30 / 0x6f2e8eb0 / 0x6f2e8f30, applier 0x6f30d590): the shape is
    written at once, and always with the shared-vision expansion of the player mask, whatever useSharedVision says."""
    if not m.bits:
        return
    g = grid(N)
    g.cnt_ok = False                     # a write the sight counters do not know: the next update starts from the wipe
    st = m.apply_state()                 # the applier's dispatch: low 3 bits of the flags word, its own shape only
    if not st:
        return
    bits = g.expand(m.bits)
    if m.kind == 'R':
        x0, y0 = g.cell(m.a, m.b)
        x1, y1 = g.cell(m.c, m.d)
        g.rect(st, bits, x0, y0, x1, y1)
    else:
        mx, my = m.center()
        cx, cy = g.cell(mx, my)
        g.radius(st, bits, cx, cy, rad_cells(m.c))


@nat('CreateFogModifierRect')
def _fogmodrect(N, p, state, r, shared, after):
    ro = N.o(r)
    return _fogmod(N, p, state, 'R', getattr(ro, 'minx', 0.0), getattr(ro, 'miny', 0.0), getattr(ro, 'maxx', 0.0),
                   getattr(ro, 'maxy', 0.0), shared, after)


@nat('CreateFogModifierRadius')
def _fogmodrad(N, p, state, x, y, r, shared, after):
    return _fogmod(N, p, state, 'C', x, y, r, None, shared, after)


@nat('CreateFogModifierRadiusLoc')
def _fogmodradloc(N, p, state, l, r, shared, after):
    lo = N.o(l, Location)
    return _fogmod(N, p, state, 'C', lo.x if lo else 0.0, lo.y if lo else 0.0, r, None, shared, after, lo, 0x20)


def _fogmodon(N, h, on):
    m = N.o(h)
    if isinstance(m, FogMod):
        m.on = on


@nat('FogModifierStart')
def _fogmodstart(N, h):
    _fogmodon(N, h, True)


@nat('FogModifierStop')
def _fogmodstop(N, h):
    _fogmodon(N, h, False)


def _both_off(N):
    return not N.fog[0] and not N.fog[1]


def _point_state(N, x, y, p):
    """-> the state value (1/2/4) of a point for a player, None for a null / invalid player (every query answers false)."""
    pl = N.o(p, Player)
    if pl is None:
        return None
    if _both_off(N):
        return V
    g = grid(N)
    g.want(1 << pl.id)
    cx, cy = g.cell(x, y)
    return g.state(cx, cy, 1 << pl.id)


@nat('IsVisibleToPlayer')
def _isvis(N, x, y, p):
    return _point_state(N, x, y, p) == V


@nat('IsFoggedToPlayer')
def _isfog(N, x, y, p):
    return _point_state(N, x, y, p) == F


@nat('IsMaskedToPlayer')
def _ismask(N, x, y, p):
    return _point_state(N, x, y, p) == M


def _loc_state(N, l, p):
    lo = N.o(l, Location)
    return _point_state(N, lo.x, lo.y, p) if lo is not None else None


@nat('IsLocationVisibleToPlayer')
def _islocvis(N, l, p):
    return _loc_state(N, l, p) == V


@nat('IsLocationFoggedToPlayer')
def _islocfog(N, l, p):
    return _loc_state(N, l, p) == F


@nat('IsLocationMaskedToPlayer')
def _islocmask(N, l, p):
    return _loc_state(N, l, p) == M


def _unit_cell_state(N, u, p):
    uo, pl = N.o(u, Unit), N.o(p, Player)
    if uo is None or pl is None:
        return None, None
    if _both_off(N):
        return uo, V
    g = grid(N)
    g.want(1 << pl.id)
    cx, cy = g.cell(uo.x, uo.y)
    return uo, g.state(cx, cy, 1 << pl.id)


@nat('IsUnitVisible')
def _unitvisible(N, u, p):
    uo, pl = N.o(u, Unit), N.o(p, Player)
    if uo is None or pl is None or uo.hidden:
        return False
    # NOTES_fog 3.3 [obs 0x6f1c04a0, 0x6f2a33e0]: the widget test's stealth gate comes BEFORE the cell test -- an
    # invisible unit is visible to its owner / an ally, or when a point reveal / detector of the player covers it,
    # whatever the fog cell says (also with FogEnable / FogMaskEnable off). The INVERSE of IsUnitInvisible.
    if getattr(uo, 'invisible', 0) and uo.owner is not pl and not N.eng.ally(pl, uo.owner) \
            and not N.eng.ab.revealed_to(pl, uo):
        return False
    if _both_off(N) or uo.owner is pl:
        return True
    g = grid(N)
    g.want(1 << pl.id)
    cx, cy = g.cell(uo.x, uo.y)
    return g.state(cx, cy, 1 << pl.id) == V


@nat('IsUnitFogged')
def _unitfogged(N, u, p):
    return _unit_cell_state(N, u, p)[1] == F


@nat('IsUnitMasked')
def _unitmasked(N, u, p):
    return _unit_cell_state(N, u, p)[1] == M


def fog_counts(N, slot):
    """-> (visible, fogged, masked) cells for a slot under the flags (tests, the W9 play check)."""
    g = N.fogg
    if g is None:
        return None
    out = [0, 0, 0]
    bit = 1 << slot
    for cy in range(g.h):
        for cx in range(g.w):
            s = g.state(cx, cy, bit)
            out[0 if s == V else (1 if s == F else 2)] += 1
    return tuple(out)
