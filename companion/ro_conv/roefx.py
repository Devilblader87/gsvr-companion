"""RO skill / level up / warp portal effects of the world (w43 lane fx, docs/design/ro_fx.md): the 'roefx' pack, built from the PLAYER's own client (data.grf), read only.

BYOG: nothing of the game is in the repo or the world. Tests write synthetic STR / SPR / ACT / TGA files (test_ro_fx.py).

The original client plays an effect id (EffectTable of the client = roBrowser's DB/Effects/EffectTable.js, the open re-implementation used as the reference)
as a list of parts. Four kinds exist, and the pack carries each the way the world draws it:
  STR       an effect animation file  data\\texture\\effect\\<name>.str              -> REFX layers (strfx.parse_str, unchanged keys)
  SPR       an effect sprite          data\\sprite\\<effect folder>\\<name>.spr / .act   -> REFX layers: one flip book layer, one base key per ACT frame (texture index = frame)
  3D / 2D   particles the client code spawns (textures of texture\\effect)            -> REFX layers baked from the table's numbers (fixed seed per effect)
  CYLINDER  cones / rings the client code draws (ring_blue.tga ...)                    -> RFCY rows, drawn by the GPU (GoldSrcVR/RoFxCyl, no Udon work per frame)
Bolts (Fire / Cold Bolt), arrows and Soul Strike balls fly: the world simulates them (GsvrRoFx missiles, rules of BoltRainSim.js); the pack only carries their textures as
texture-only effects 'tex_<name>' (one layer, no keys). Only the effects the world plays are built (WORLD_FX).

Sections (RPG1, all little endian):
  REFX (strfx.enc_refx, section version 1): every effect of WORLD_FX that has quads (+ the tex_ effects); RFTX: the one DXT5 atlas of their textures (+ the cylinder textures)
  RFCY (section version 1): header 4 x u32 (rows, row bytes 80, effects, 0); then per row 6 x i32 + 14 x f32:
    effect (index into REFX's effect table; an effect with cylinders only has 0 layers there), atlas slot, anim (0..6), flags, total sides, shown sides,
    delay s, duration s (0 = the duration the world gives at spawn: a cast), period s (repeat), bottom, top, height, posZ (cells), r, g, b, alpha max, accel, angle random (deg), 0
  anim = Cylinder.js 0..5 (1 height grows, 2 top grows, 3 shrink + rise / fall, 4 grow, 5 rise / fall); 6 = the client's flat ring primitive (EffectPrimSim 'ring'):
    radius = bottom + height * t + accel * t^2 / 2 (cells, s), band width = top, alpha in over the first third, out over the last third.
  flags: 1 fade (in over the first quarter, out over the last), 2 rotate (250 deg / s about the axis), 4 alpha blend (else additive), 8 face the camera (a disc), 16 repeat.
Units: cells (the world multiplies by the map's cell size) and seconds. The client: 5 units a cell, 60 ticks a second, STR pixels 35 a cell.
"""
import math
import os
import random
import struct
import sys

from . import img, sprite, strfx
from .grf import Vfs, norm

_ROSE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'rose_conv')
if _ROSE not in sys.path:
    sys.path.append(_ROSE)
import rpg1 as P            # noqa: E402

FX_PACK = 'roefx'
FX_REV = 1
TAG_RFCY = b'RFCY'
RFCY_VERSION = 1
RFCY_HDR = struct.Struct('<4I')
RFCY_ROW = struct.Struct('<6i14f')            # 80 bytes
UNITS_PER_CELL = 5.0
TICKS = 60.0
PX = strfx.PX_PER_M                            # STR pixels per cell (the world scales the effect by the cell size)
OX, OY = strfx.ORIGIN_X, strfx.ORIGIN_Y
FPS = 30                                       # frame rate of the baked particle / sprite layers
SPR_DIR = 'data\\sprite\\이팩트\\'          # "이팩트" (the client's effect sprite folder)
EFF_DIR = 'texture\\effect\\'
ANIM_RING = 6
F_FADE, F_ROTATE, F_ALPHA, F_FACE, F_REPEAT = 1, 2, 4, 8, 16
MAX_SIDES = 30


class FxError(ValueError):
    pass


# ------------------------------------------------------------------ recipe helpers (numbers of the client's effect table, converted to cells / seconds)
def cyl(tex, anim, bottom, top, height, rgb=(1.0, 1.0, 1.0), alpha=1.0, dur=0.0, delay=0.0, fade=True, rotate=False, add=True, face=False, repeat=False,
        period=0.0, posz=0.0, sides=20, shown=None, accel=0.0, angle_rand=0.0):
    """one CYLINDER part (Cylinder.js): sizes in cells, times in seconds; dur 0 = the world's spawn duration (cast time)."""
    flags = (F_FADE if fade else 0) | (F_ROTATE if rotate else 0) | (0 if add else F_ALPHA) | (F_FACE if face else 0) | (F_REPEAT if repeat else 0)
    return dict(kind='cyl', tex=tex, anim=anim, flags=flags, sides=sides, shown=sides if shown is None else shown, delay=delay, dur=dur, period=period or dur,
                bottom=bottom, top=top, height=height, posz=posz, rgb=tuple(rgb), alpha=alpha, accel=accel, angle_rand=angle_rand)


def ring(tex, frames, speed, accel, width, rgb=(1.0, 1.0, 1.0), alpha=1.0, delay=0.0, r0=0.0):
    """the client's flat ring primitive (EffectPrimSim 'ring', BoltRainSim BOLT_RING): speed / accel in units a tick (/ tick^2), width in units, frames in ticks."""
    return cyl(tex, ANIM_RING, r0 / UNITS_PER_CELL, width / UNITS_PER_CELL, speed * TICKS / UNITS_PER_CELL, rgb=rgb, alpha=alpha, dur=frames / TICKS, delay=delay,
               fade=False, sides=20, accel=accel * TICKS * TICKS / UNITS_PER_CELL)


def puff(tex, n, dur, delay=0.0, between=0.0, xr=0.0, z0=0.0, z0r=0.0, z1=None, z1r=0.0, size=10.0, size_end=None, sizey=None, alpha=1.0, rgb=(1.0, 1.0, 1.0),
         fade_in=False, fade_out=True, add=True, x1r=None):
    """3D particles (ThreeD of the client's table): n copies, dur s each, starting delay + i * between; ground spread xr (cells) -> screen x, height z0 (+- z0r) rising to
    z1 (+- z1r) cells; size = the table's size (100 = 1 m quad), sizey = its height when not square. Billboards (the world's STR renderer)."""
    return dict(kind='puff', tex=tex, n=n, dur=dur, delay=delay, between=between, xr=xr, x1r=x1r, z0=z0, z0r=z0r, z1=z0 if z1 is None else z1, z1r=z1r,
                size=size, size_end=size if size_end is None else size_end, sizey=sizey, alpha=alpha, rgb=tuple(rgb), fade_in=fade_in, fade_out=fade_out, add=add)


def rays(texs, n, dur, length0, length1, width, alpha=1.0, step=50.0, add=True):
    """the 2D lens burst of EF_HIT2 (circle pattern): n thin quads at angle i * step, stretching from length0 to length1 px (screen size / 4 in the world), fading."""
    return dict(kind='rays', texs=list(texs), n=n, dur=dur, l0=length0, l1=length1, w=width, alpha=alpha, step=step, add=add)


def strp(name):
    return dict(kind='str', file=name)


def sprp(name, dur=None, yoff=0.0):
    return dict(kind='spr', file=name, dur=dur, yoff=yoff)


def flip(texs, n, dur, spread=0.6, size=80.0, z=0.6):
    """an animated explosion (Napalm Beat's 8 frames): n quads near the target, the frames played once over dur s."""
    return dict(kind='flip', texs=list(texs), n=n, dur=dur, spread=spread, size=size, z=z)


def grow(tex, dur, size0, size1, z=0.8, alpha=1.0, add=True):
    """one billboard growing from size0 to size1 (STR px) and fading out (Magnum Break's fire sphere)."""
    return dict(kind='grow', tex=tex, dur=dur, s0=size0, s1=size1, z=z, alpha=alpha, add=add)


def texonly(texs):
    return dict(kind='tex', texs=list(texs))


_WHITE = (1.0, 1.0, 1.0)


def _cast(tex, a, b, c, rgb, add=True):
    """EF_BEGINSPELL2..7: a tall thin column (height 30, anim 1), a short ring (anim 1) and the cone opening while casting (anim 2, top 4)."""
    out = []
    if a:
        out.append(cyl(tex, 1, 1.0, 1.0, 30.0, rgb=rgb, alpha=a, rotate=True, add=add))
    out.append(cyl(tex, 1, 1.0 if add else 1.1, 1.3 if add else 1.2, 1.0 if tex == 'ring_blue' else (3.0 if not add else 2.0), rgb=rgb, alpha=b, rotate=True, add=add))
    out.append(cyl(tex, 2, 1.0, 4.0, 3.0, rgb=rgb, alpha=c, rotate=True, add=add))
    return out


_FIRE = ['불화살%d.tga' % i for i in range(1, 7)]          # 불화살1..6 (Fire Bolt frames)
_EXPLO = ['폭발%d.tga' % i for i in range(1, 9)]               # 폭발1..8 (Napalm Beat)
_BIG = '대폭발.tga'                                        # 대폭발 (Magnum Break sphere)
_BLESS = '축복'                                                # 축복 (Blessing sprite)

# world effect name -> parts. Names are the world's effect ids (GsvrRoFx / GsvrRoFxPlay look them up by name). Numbers in the comments = the client's effect id.
WORLD_FX = {
    # cast auras (12 neutral / ghost / dark, 54 water, 55 fire, 56 wind, 57 earth, 58 holy, 59 poison): live as long as the cast (duration 0 = spawn duration)
    'cast_neutral': [cyl('ring_yellow', 2, 1.0, 5.0, 4.0, alpha=0.8)],
    'cast_water': _cast('ring_blue', 0.2, 0.2, 0.6, (0.5, 0.5, 1.0)),
    'cast_fire': _cast('ring_red', 0.3, 0.7, 0.7, (1.0, 0.4, 0.4)),
    'cast_wind': _cast('ring_white', 0.3, 0.6, 0.6, (0.6, 1.0, 0.6)),
    'cast_earth': _cast('ring_yellow', 0.5, 1.0, 1.0, _WHITE, add=False),
    'cast_holy': [cyl('ring_white', 2, 1.0, 5.0, 4.0, alpha=0.8, rotate=True)],
    'cast_poison': _cast('ring_purple', 0.3, 0.7, 0.7, (0.8, 0.8, 0.8)),
    # 0 EF_HIT1: four pok3 sparks flying off the target; 1 EF_HIT2: the lens burst (Bash, Napalm Beat, skill hits)
    'hit': [puff('pok3.tga', 4, 0.3, z0=1.0, xr=0.2, z1=1.0, z1r=2.0, x1r=2.0, size=10.0, size_end=30.0, alpha=0.8, fade_in=True)],
    'hit_lens': [rays(['lens1.tga', 'lens2.tga'], 7, 0.25, 10.0, 70.0, 8.0, alpha=1.0, step=51.0),
                 puff('pok3.tga', 4, 0.3, z0=1.0, xr=0.2, z1=1.0, z1r=2.0, x1r=2.0, size=10.0, size_end=30.0, alpha=0.8, fade_in=True)],
    # 16 EF_BASH (on the caster of Double Strafe): camera facing discs of alpha_down / alpha_center wedges
    'strafe_caster': [cyl('alpha_down', 2, 0.1, 2.0, 0.0, alpha=0.6, dur=1.0, rotate=True, face=True, posz=1.5)]
                     + [cyl('alpha_center', 2, 0.01, 2.5 if i < 10 else 4.0, 0.0, alpha=0.6, dur=1.0, rotate=True, face=True, posz=1.5, sides=30, shown=1, angle_rand=360.0)
                        for i in range(18)],
    # 17 EF_MAGNUMBREAK: two flat yellow rings + the fire sphere (대폭발), 30 ticks
    'magnumbreak': [ring('ring_yellow', 30, 1.75, -(1.75 / 30) * 0.5, 12, alpha=1.0), ring('ring_yellow', 30, 1.75, -(1.75 / 30) * 0.5, 12, alpha=1.0, delay=1.0 / 60),
                    grow(_BIG, 0.5, 30.0, 180.0, z=0.6, alpha=1.0)],
    'provoke': [strp('provoke.str')],                                   # 67
    'firewall1': [strp('firewall1.str')], 'firewall2': [strp('firewall2.str')],          # 25 firewall%d (rand 1..2), one per wall cell
    'firehit1': [strp('firehit1.str')], 'firehit2': [strp('firehit2.str')], 'firehit3': [strp('firehit3.str')],          # 49
    'windhit1': [strp('windhit1.str')], 'windhit2': [strp('windhit2.str')], 'windhit3': [strp('windhit3.str')],          # 52
    'lightning': [strp('lightning.str')],                               # 29 Lightning Bolt (one per hit)
    'thunderstorm': [strp('thunderstorm.str')],                         # 30
    'napalm': [flip(_EXPLO, 3, 0.5)],                                   # 32 EF_NAPALMBEAT: clustered explosions (폭발1..8)
    # 312 EF_HEAL (self) / 320 EF_HEAL3 (on an undead monster): white rings rising + pok3 sparkles
    'heal': [cyl('ring_white', 1, 0.95, 0.95, 8.0, rgb=(0.7, 1.0, 0.7), alpha=0.2, dur=1.5, rotate=True),
             cyl('ring_white', 1, 1.0, 1.0, 8.0, rgb=(0.7, 1.0, 0.7), alpha=0.2, dur=1.5, rotate=True),
             puff('pok3.tga', 15, 1.3, delay=0.4, between=0.01, xr=1.5, z0=0.0, z1=4.0, z1r=2.0, size=9.0, alpha=0.6, fade_in=True),
             puff('pok3.tga', 7, 1.1, delay=0.2, between=0.05, xr=1.0, z0=0.5, z0r=0.5, z1=5.0, size=9.0, alpha=0.6, fade_in=True)],
    'heal_hit': [cyl('ring_white', 1, 0.95, 0.95, 10.0, alpha=0.2, dur=1.0, rotate=True), cyl('ring_white', 1, 1.0, 1.0, 9.0, alpha=0.2, dur=1.0, rotate=True),
                 puff('pok3.tga', 10, 1.0, delay=0.4, between=0.01, xr=1.5, z0=0.0, z1=4.5, z1r=1.5, size=9.0, alpha=0.8, fade_in=True)],
    # 37 EF_INCAGILITY: ac_center2 streaks rising around the target + the agi_up label
    'incagi': [puff('ac_center2.tga', 7, 1.0, delay=0.5, xr=1.5, z0=1.0, z0r=1.0, z1=6.0, z1r=1.0, size=25.0, sizey=45.0),
               puff('ac_center2.tga', 3, 1.0, delay=0.4, xr=1.5, z0=1.0, z0r=1.0, z1=6.0, z1r=1.0, size=25.0, sizey=45.0, alpha=0.75),
               puff('ac_center2.tga', 10, 1.0, xr=1.5, z0=1.0, z0r=1.0, z1=6.0, z1r=1.0, size=25.0, sizey=45.0),
               puff('agi_up.bmp', 1, 1.0, z0=0.4, z1=3.0, size=100.0, sizey=45.0, fade_in=True)],
    # 42 EF_BLESSING: the 축복 sprite over the head, particle6 sparkles coming down, a soft pok2 glow
    'blessing': [sprp(_BLESS, dur=1.5, yoff=-75.0),
                 puff('pok2.tga', 1, 2.5, z0=0.8, size=140.0, alpha=0.3, rgb=(0.1, 0.75, 1.0), fade_in=True)],
    'levelup': [strp('angel.str')],                                     # 371 EF_ANGEL (the level up of the client)
    'joblevelup': [strp('joblvup.str')],                                # 158 EF_JOBLVUP (also played with the angel on a job change)
    # 321 EF_WARPZONE2 = the warp NPC (sprite 45): two ring_blue cones, 4 copies 1 s apart, 4 s each, repeating, + pok1 sparkles rising
    'portal': [cyl('ring_blue', 3, b, t, 1.1, rgb=(0.5, 0.5, 1.0), alpha=0.4, dur=4.0, delay=float(i), repeat=True, period=4.0)
               for (b, t) in ((2.0, 3.3), (1.9, 3.2)) for i in range(4)]
              + [puff('pok1.tga', 5, 1.0, between=0.3, xr=1.5, z0=0.0, z1=2.0, z1r=2.0, size=50.0, rgb=(0.9, 1.0, 0.9), fade_in=True)],
    # bolt rings (BoltRainSim BOLT_RING) the world opens where a bolt lands
    'ring_fire': [ring('ring_yellow', 30, 1.2, (1.2 / 70) * -2, 10)],
    'ring_cold': [ring('ring_blue', 30, 1.2, (1.2 / 70) * -2, 10)],
    # textures of what the world flies itself (GsvrRoFx missiles)
    'tex_firebolt': [texonly(_FIRE)], 'tex_coldbolt': [texonly(['icearrow.tga'])], 'tex_arrow': [texonly(['arrown.tga'])], 'tex_soul': [texonly(['ghost.bmp'])],
    'tex_spark': [texonly(['pok3.tga'])],
}
# the world's effect name for each loop / instance-duration effect (the rest play once)
LOOPING = ('portal',)


# ------------------------------------------------------------------ baking
def _key(frame, typ, pos, xy, color, angle=0.0, aniframe=0.0, anitype=0, delay=0.0, add=True):
    return dict(frame=int(frame), type=typ, pos=[float(pos[0]), float(pos[1])], xy=[float(v) for v in xy], angle=float(angle), aniframe=float(aniframe), anitype=anitype,
                delay=float(delay), color=[float(c) for c in color], src=5, dst=2 if add else 6)


def _quad(hw, hh):
    return (hw, -hw, -hw, hw, -hh, -hh, hh, hh)


def _seg(f0, f1, p0, p1, s0, s1, c0, c1, angle=0.0, aniframe=0.0, anitype=0, delay=0.0, add=True):
    """base key at f0 + per-frame delta key to the state at f1 (strfx semantics: base + delta * (frame - f0))."""
    n = float(max(1, f1 - f0))
    b = _key(f0, 0, p0, _quad(s0[0], s0[1]), c0, angle, aniframe, anitype, delay, add)
    d = _key(f0, 1, ((p1[0] - p0[0]) / n, (p1[1] - p0[1]) / n), [(a - b2) / n for a, b2 in zip(_quad(s1[0], s1[1]), _quad(s0[0], s0[1]))],
             [(c1[i] - c0[i]) / n for i in range(4)], add=add)
    return [b, d]


def _rng(name):
    return random.Random(sum((i + 1) * ord(ch) for i, ch in enumerate(name)) * 7919 + 17)


def bake_puff(p, rnd, fps=FPS):
    """-> [layer] one layer per particle: fade in (or full) -> fade out between the start and end points (screen x = ground spread, y = height)."""
    layers = []
    for i in range(p['n']):
        t0 = p['delay'] + p['between'] * i
        f0 = int(round(t0 * fps))
        f1 = f0 + max(2, int(round(p['dur'] * fps)))
        x0 = (rnd.random() * 2 - 1) * p['xr']
        x1 = x0 if p['x1r'] is None else x0 + (rnd.random() * 2 - 1) * p['x1r']
        z0 = p['z0'] + (rnd.random() * 2 - 1) * p['z0r']
        z1 = p['z1'] + (rnd.random() * 2 - 1) * p['z1r']
        hw0 = p['size'] * PX / 200.0
        hw1 = p['size_end'] * PX / 200.0
        ry = (p['sizey'] / p['size']) if p['sizey'] else 1.0
        rgb = [255.0 * c for c in p['rgb']]
        a = 255.0 * min(1.0, p['alpha'])
        P0 = (OX + x0 * PX, OY - z0 * PX)
        P1 = (OX + x1 * PX, OY - z1 * PX)
        keys = []
        if p['fade_in']:
            fm = (f0 + f1) // 2
            Pm = ((P0[0] + P1[0]) / 2, (P0[1] + P1[1]) / 2)
            hm = (hw0 + hw1) / 2
            keys += _seg(f0, fm, P0, Pm, (hw0, hw0 * ry), (hm, hm * ry), rgb + [0.0], rgb + [a], add=p['add'])
            keys += _seg(fm, f1, Pm, P1, (hm, hm * ry), (hw1, hw1 * ry), rgb + [a], rgb + [0.0 if p['fade_out'] else a], add=p['add'])
        else:
            keys += _seg(f0, f1, P0, P1, (hw0, hw0 * ry), (hw1, hw1 * ry), rgb + [a], rgb + [0.0 if p['fade_out'] else a], add=p['add'])
        keys.append(_key(f1, 0, P1, _quad(hw1, hw1 * ry), rgb + [0.0], add=p['add']))
        layers.append(dict(textures=[p['tex']], keys=keys))
    return layers


def bake_rays(p, fps=FPS):
    layers = []
    f1 = max(2, int(round(p['dur'] * fps)))
    for i in range(p['n']):
        ang = i * p['step']
        a = 255.0 * min(1.0, p['alpha'])
        c = (OX, OY - PX)          # the burst centre: 1 cell up (the body)
        keys = _seg(0, f1, c, c, (p['w'], p['l0']), (p['w'] * 0.2, p['l1']), [255.0, 255.0, 255.0, a], [255.0, 255.0, 255.0, 0.0], angle=ang, add=p['add'])
        keys.append(_key(f1, 0, c, _quad(p['w'] * 0.2, p['l1']), [255.0, 255.0, 255.0, 0.0], angle=ang, add=p['add']))
        layers.append(dict(textures=[p['texs'][i % len(p['texs'])]], keys=keys))
    return layers


def bake_flip(p, rnd, fps=FPS):
    layers = []
    f1 = max(2, int(round(p['dur'] * fps)))
    for i in range(p['n']):
        x = (rnd.random() * 2 - 1) * p['spread']
        z = p['z'] + (rnd.random() * 2 - 1) * p['spread'] * 0.5
        f0 = i * 2
        hw = p['size'] * PX / 200.0
        pos = (OX + x * PX, OY - z * PX)
        delay = len(p['texs']) / float(f1)                  # frames of the flip book per STR frame: once over the effect
        keys = [_key(f0, 0, pos, _quad(hw, hw), (255.0, 255.0, 255.0, 255.0), anitype=1, delay=delay), _key(f0 + f1, 0, pos, _quad(hw, hw), (255.0, 255.0, 255.0, 0.0))]
        layers.append(dict(textures=list(p['texs']), keys=keys))
    return layers


def bake_grow(p, fps=FPS):
    f1 = max(2, int(round(p['dur'] * fps)))
    c = (OX, OY - p['z'] * PX)
    a = 255.0 * p['alpha']
    keys = _seg(0, f1 // 2, c, c, (p['s0'] / 2, p['s0'] / 2), ((p['s0'] + p['s1']) / 4, (p['s0'] + p['s1']) / 4), [255.0, 255.0, 255.0, 0.0], [255.0, 255.0, 255.0, a], add=p['add'])
    keys += _seg(f1 // 2, f1, c, c, ((p['s0'] + p['s1']) / 4, (p['s0'] + p['s1']) / 4), (p['s1'] / 2, p['s1'] / 2), [255.0, 255.0, 255.0, a], [255.0, 255.0, 255.0, 0.0], add=p['add'])
    keys.append(_key(f1, 0, c, _quad(p['s1'] / 2, p['s1'] / 2), [255.0, 255.0, 255.0, 0.0], add=p['add']))
    return [dict(textures=[p['tex']], keys=keys)]


def bake_spr(name, spr, act, dur=None, yoff=0.0, fps=FPS):
    """an effect sprite -> (layers, images): action 0 played at its ACT delay, repeated for `dur` s; every frame = one composed image (texture index), keys hold its rectangle.
    images: [(texture name, w, h, rgba)] - the names are '<sprite>#<frame>' (atlas slots only)."""
    frames = act['actions'][0] if act['actions'] else []
    images, keys = [], []
    tex = []
    rects = []
    for fi, fr in enumerate(frames):
        left, top, w, h, px = sprite.compose_frame(spr, fr)
        if w == 0 or h == 0:
            rects.append(None)
            continue
        tn = '%s#%d' % (name, fi)
        tex.append(tn)
        images.append((tn, w, h, px))
        rects.append((len(tex) - 1, left, top, w, h))
    if not tex:
        return [], []
    ms = act['delays'][0] if act['delays'] and act['delays'][0] > 0 else 100.0
    step = max(1, int(round(ms / 1000.0 * fps)))
    total = int(round((dur if dur else len(frames) * ms / 1000.0) * fps))
    f, i = 0, 0
    while f < total:
        r = rects[i % len(rects)]
        if r is not None:
            ti, left, top, w, h = r
            pos = (OX + left + w / 2.0, OY + yoff + top + h / 2.0)
            keys.append(_key(f, 0, pos, _quad(w / 2.0, h / 2.0), (255.0, 255.0, 255.0, 255.0), aniframe=ti, add=False))
        f += step
        i += 1
    if keys:
        last = dict(keys[-1])
        last['frame'] = total
        keys.append(last)
    return [dict(textures=tex, keys=keys)], images


def _load_tex(vfs, name):
    """texture\\effect\\<name> (or .tga / .bmp of the stem) -> SLOT_PX square RGBA or None"""
    for cand in (name, os.path.splitext(name)[0] + '.tga', os.path.splitext(name)[0] + '.bmp'):
        p = EFF_DIR + cand
        if vfs.exists(p):
            try:
                w, h, px = img.decode(vfs.read(p), cand)
            except img.ImageError:
                return None
            return img.resize(px, w, h, strfx.SLOT_PX, strfx.SLOT_PX)
    return None


def build_effects(vfs, names=None, warn=None):
    """-> (effects [(name, STR-like dict)], cylinders [(effect index, part)], images {texture key: rgba}, info). Missing files drop that part (warning)."""
    warn = warn if warn is not None else []
    names = list(WORLD_FX) if names is None else list(names)
    effects, cyls, images = [], [], {}
    for nm in names:
        parts = WORLD_FX[nm]
        layers, fps, maxf = [], None, 0
        rnd = _rng(nm)
        my_cyl = []
        for part in parts:
            k = part['kind']
            if k == 'str':
                try:
                    e = strfx.parse_str(vfs.read(EFF_DIR + part['file']))
                except KeyError:
                    warn.append('fx %s: %s not in the client' % (nm, part['file']))
                    continue
                except strfx.StrError as ex:
                    warn.append('fx %s: %s: %s' % (nm, part['file'], ex))
                    continue
                if fps is not None and fps != e['fps']:
                    warn.append('fx %s: %s has %d fps, the effect %d (part dropped)' % (nm, part['file'], e['fps'], fps))
                    continue
                fps = e['fps']
                layers += e['layers']
                maxf = max(maxf, e['max_frame'])
            elif k == 'spr':
                try:
                    spr = sprite.parse_spr(vfs.read(SPR_DIR + part['file'] + '.spr'))
                    act = sprite.parse_act(vfs.read(SPR_DIR + part['file'] + '.act'))
                except KeyError:
                    warn.append('fx %s: sprite %s not in the client' % (nm, part['file']))
                    continue
                except sprite.FormatError as ex:
                    warn.append('fx %s: sprite %s: %s' % (nm, part['file'], ex))
                    continue
                f2 = fps or FPS
                lys, imgs = bake_spr(part['file'], spr, act, part['dur'], part['yoff'], f2)
                for tn, w, h, px in imgs:
                    images[norm(tn)] = img.resize(px, w, h, strfx.SLOT_PX, strfx.SLOT_PX)
                if lys:
                    fps = f2
                    layers += lys
                    maxf = max(maxf, max(kk['frame'] for ly in lys for kk in ly['keys']))
            elif k in ('puff', 'rays', 'flip', 'grow'):
                f2 = fps or FPS
                lys = bake_puff(part, rnd, f2) if k == 'puff' else bake_rays(part, f2) if k == 'rays' else bake_flip(part, rnd, f2) if k == 'flip' else bake_grow(part, f2)
                fps = f2
                layers += lys
                maxf = max(maxf, max(kk['frame'] for ly in lys for kk in ly['keys']))
            elif k == 'tex':
                layers.append(dict(textures=list(part['texs']), keys=[]))
            elif k == 'cyl':
                my_cyl.append(part)
        if not layers and not my_cyl:
            warn.append('fx %s: nothing of it in the client (dropped)' % nm)
            continue
        ei = len(effects)
        effects.append((nm, dict(version=148, fps=fps or FPS, max_frame=maxf, layers=layers)))
        cyls += [(ei, c) for c in my_cyl]
    # textures: every layer texture + every cylinder texture once
    keys = []
    for _nm, e in effects:
        for ly in e['layers']:
            for t in ly['textures']:
                if norm(t) not in keys:
                    keys.append(norm(t))
    for _ei, c in cyls:
        t = norm(c['tex'] + '.tga')
        if t not in keys:
            keys.append(t)
    missing = []
    for t in keys:
        if t in images:
            continue
        px = _load_tex(vfs, t)
        if px is None:
            missing.append(t)
        else:
            images[t] = px
    if missing:
        warn.append('fx: %d of %d textures missing (those layers / cylinders are skipped): %s' % (len(missing), len(keys), ', '.join(missing[:6])))
    info = dict(effects=[n for n, _ in effects], cylinders=len(cyls), textures=len(images), missing=missing)
    return effects, cyls, images, info


def enc_rfcy(rows):
    """rows: [(effect index, slot, part)] -> RFCY section bytes"""
    out = bytearray(RFCY_HDR.pack(len(rows), RFCY_ROW.size, len(set(r[0] for r in rows)), 0))
    for ei, slot, c in rows:
        out += RFCY_ROW.pack(ei, slot, c['anim'], c['flags'], min(MAX_SIDES, c['sides']), min(c['sides'], c['shown']), c['delay'], c['dur'], c['period'],
                             c['bottom'], c['top'], c['height'], c['posz'], c['rgb'][0], c['rgb'][1], c['rgb'][2], c['alpha'], c['accel'], c['angle_rand'], 0.0)
    return bytes(out)


def dec_rfcy(d):
    n, rb, _ne, _z = RFCY_HDR.unpack_from(d, 0)
    if rb != RFCY_ROW.size or len(d) != RFCY_HDR.size + n * rb:
        raise FxError('RFCY size mismatch')
    keys = ('effect', 'slot', 'anim', 'flags', 'sides', 'shown', 'delay', 'dur', 'period', 'bottom', 'top', 'height', 'posz', 'r', 'g', 'b', 'alpha', 'accel', 'angle_rand', 'z')
    return [dict(zip(keys, RFCY_ROW.unpack_from(d, RFCY_HDR.size + i * rb))) for i in range(n)]


def fx_sections(vfs, names=None, warn=None):
    """-> [(tag, version, count, data)]: REFX + RFTX (+ RFCY when any cylinder has its texture); [] when the client has none of the effects."""
    effects, cyls, images, info = build_effects(vfs, names, warn)
    if not effects:
        return [], info
    order = list(images)
    slots = dict((k, i) for i, k in enumerate(order))
    atlas = strfx.build_atlas([images[k] for k in order]) if order else None
    refx = strfx.enc_refx(effects, slots)
    secs = [(strfx.TAG_REFX, strfx.SECTION_VERSION, len(effects), refx)]
    if atlas:
        secs.append((strfx.TAG_RFTX, strfx.SECTION_VERSION, len(order), strfx.enc_rftx(atlas)))
    rows = [(ei, slots[norm(c['tex'] + '.tga')], c) for ei, c in cyls if norm(c['tex'] + '.tga') in slots]
    if rows:
        secs.append((TAG_RFCY, RFCY_VERSION, len(rows), enc_rfcy(rows)))
    info = dict(info, cylinder_rows=len(rows), refx_bytes=len(refx), slots=len(order))
    return secs, info


def build(name, folder, out_path, progress=None, cancel=None, log=None, names=None):
    """the 'roefx' pack -> out_path. -> dict(name, bytes, effects, warnings, info)."""
    from .api import check_install, BuildError, Cancelled
    if name != FX_PACK:
        raise BuildError('not the effects pack: %r' % name)
    ok, why = check_install(folder)
    if not ok:
        raise BuildError('Ragnarok folder not usable: %s' % why)

    def tick(stage, f):
        if cancel is not None and cancel():
            raise Cancelled(name)
        if progress is not None:
            progress(stage, f)
    tick('grf', 0.0)
    vfs = Vfs(folder)
    warns = list(vfs.errors)
    tick('effects', 0.2)
    secs, info = fx_sections(vfs, names, warns)
    if not secs:
        raise BuildError('no effect of the world in the client data')
    data = P.write_pack(secs)
    tick('write', 0.9)
    part = out_path + '.part'
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    try:
        with open(part, 'wb') as f:
            f.write(data)
        os.replace(part, out_path)
    finally:
        if os.path.exists(part):
            os.remove(part)
    tick('write', 1.0)
    if log is not None:
        log('%s: %d bytes, %d effects, %d cylinder rows, %d textures' % (name, len(data), len(info['effects']), info.get('cylinder_rows', 0), info['textures']))
    return {'name': name, 'bytes': len(data), 'effects': len(info['effects']), 'warnings': warns[:40], 'info': info, 'log': []}


def main(argv):
    """python3 -m ro_conv.roefx <client folder> <out.pack>: build + print the summary (manual check against a real install)."""
    if len(argv) != 2:
        print('usage: roefx <client folder> <out.pack>')
        return 2
    r = build(FX_PACK, argv[0], argv[1], log=print)
    for w in r['warnings']:
        print('warning:', w)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
