"""Ragnarok Online sound + effect packs (BYOG, stdlib + the ROSE decoder helpers). Design: docs/design/ro_sound.md.

Nothing of the game is in the repo or in the world: the companion reads the player's own client (data.grf wav\\ + texture\\effect\\*.str, the loose BGM\\*.mp3 folder
of the install, mp3nametable.txt) and writes two kinds of small RPG1 containers (companion/rose_conv/rpg1.py write_pack, no map sections):

  snd_common  SNDS (sound effects, ids below) + REFX / RFTX (skill / level up / portal effects, strfx.py)
  snd_<map>   SNDS with ONE clip "bgm/<map>" = the map's BGM (mp3nametable.txt, `BGM\\NN.mp3`), whole track, natural loop, mono float32 (RO tracks are 1.5 - 3 min)

SNDS = the layout of rose_audio.enc_snds (float32 mono block), so GsvrRoAudio reads it like GsvrRoseAudio does. Clip names (ASCII, the world builds them with the same names):
  bgm/<map>                    music, looping
  fx/levelup, fx/joblevelup    base / job level up (wav\\levelup.wav, wav\\effect\\st_job_level_up.wav)
  fx/warp, fx/portal, fx/readyportal, fx/teleport    warp portal (wav\\effect\\ef_portal / ef_readyportal / ef_teleportation)
  fx/cast                      magic cast begin (ef_beginspell)
  fx/swing/<w>  fx/hit/<w>     w = fist dagger sword axe mace rod spear bow (wav\\_attack_<w> / _hit_<w>, bow hit = _hit_arrow)
  fx/hurt/0..3                 the player / a monster is hit (wav\\_enemy_hit1..4)
  mon/<name>/atk|die|hurt|move|stand    <name>_attack / _die / _damage / _move / _stand (first existing of the usual spellings)
  skill/<rathena id>/fire      the skill's own sound (wav\\effect\\ef_*.wav, table SKILL_SOUNDS)
Missing files are skipped (the runtime treats a missing name as "no sound").
"""
import array
import os
import sys

from . import mappack
from .grf import Vfs

_ROSE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'rose_conv')
if _ROSE not in sys.path:
    sys.path.append(_ROSE)
import rpg1 as P            # noqa: E402
import rose_audio as AU     # noqa: E402  (wav reader, resampler, mp3 / ogg decoder via miniaudio or ffmpeg, SNDS codec; only imported, never edited)

from . import strfx          # noqa: E402

AUDIO_REV = 2               # 2 (w42 atmo): + fx/click, fx/cancel, fx/pickup, fx/equip in snd_common; snd_<map> also carries that map's monsters (mon/<sprite>/atk|die|hurt)
SFX_RATE = 22050
MUSIC_RATE = 22050
MUSIC_BUDGET = 15 * 1000 * 1000            # bytes per music pack (VRChat string download limit 18 MB, ro_pack.md)
SFX_SECONDS = 2.5                          # longest effect clip
MUSIC_MAX_SECONDS = 600.0

WEAPONS = ('fist', 'dagger', 'sword', 'axe', 'mace', 'rod', 'spear', 'bow')
SWING = {'dagger': '_attack_dagger', 'sword': '_attack_sword', 'axe': '_attack_axe', 'mace': '_attack_mace', 'rod': '_attack_rod', 'spear': '_attack_spear',
         'bow': '_attack_bow', 'fist': '_attack_katar'}
HIT = {'dagger': '_hit_dagger', 'sword': '_hit_sword', 'axe': '_hit_axe', 'mace': '_hit_mace', 'rod': '_hit_rod', 'spear': '_hit_spear', 'bow': '_hit_arrow',
       'fist': '_hit_fist1'}
# monsters of the prototype maps (prontera field / Izlude): the sprite / ronpc lanes own the real list, `monsters=` overrides it
DEFAULT_MONSTERS = ('poring', 'lunatic', 'fabre', 'pupa', 'chonchon', 'thief_bug', 'roda_frog', 'condor', 'savage_babe', 'picky', 'spore', 'wolf', 'drops', 'willow', 'rocker')
MON_EVENTS = (('atk', ('attack', 'attack1', 'atk')), ('die', ('die', 'dead')), ('hurt', ('damage', 'dam', 'hit')), ('move', ('move', 'hop', 'move1')), ('stand', ('stand',)))

# rAthena skill id (classic pre-renewal, GPL ids) -> wav\effect\<name>.wav (RO client sound of the skill, first job skills + the 1st class skills of the prototype)
SKILL_SOUNDS = {
    5: 'ef_bash', 6: 'swordman_provoke', 7: 'ef_magnumbreak', 8: 'ef_endure', 10: 'ef_sight', 11: 'ef_napalmbeat', 12: 'ef_glasswall', 13: 'ef_soulstrike',
    14: 'ef_icearrow', 15: 'ef_frostdiver', 16: 'ef_stonecurse', 17: 'ef_fireball', 18: 'ef_firewall', 19: 'ef_firearrow', 20: 'ef_lightbolt', 21: 'ef_thunderstorm',
    24: 'ef_ruwach', 26: 'ef_teleportation', 27: 'ef_readyportal', 28: 'ef_healsp', 29: 'ef_incagility', 30: 'ef_decagility', 31: 'ef_aqua', 32: 'ef_signum',
    33: 'ef_angelus', 34: 'ef_blessing', 35: 'acolyte_cure', 50: 'ef_steal', 51: 'ef_hiding', 52: 'ef_poisonattack', 54: 'priest_resurrection', 74: 'priest_magnus',
    77: 'priest_turn_undead', 156: 'priest_impositio',
}

# effect name -> STR path below data\texture\effect\ (classic skill / status effects that exist as .str in the iRO client). Keys are also the world's effect ids.
# warp portal, magnum break, bash, heal, level up: the client draws some of them in code; where no .str exists the stand-in below is used.
EFFECT_STR = {
    'levelup': 'h_levelup.str', 'joblevelup': 'joblvup.str', 'hit': 'hit.str',
    'provoke': 'provoke.str', 'angelus': 'angelus.str', 'kyrie': 'kyrie.str', 'cure': 'cure.str', 'recovery': 'recovery.str',
    'firewall': 'firewall.str', 'firepillar': 'firepillar.str', 'stonecurse': 'stonecurse.str', 'safetywall': 'safetywall.str', 'thunderstorm': 'thunderstorm.str',
    'lightning': 'lightning.str', 'pneuma': 'pneuma1.str', 'aspersio': 'aspersio.str', 'sanctuary': 'sanctuary.str', 'resurrection': 'resurrection.str',
    'magnus': 'magnus.str', 'gloria': 'gloria.str', 'impositio': 'impositio.str', 'lexaeterna': 'lexaeterna.str', 'lexdivina': 'lexdivina.str',
    'ignorance': 'ignorance.str', 'sleep': 'sleep.str', 'stun': 'stun.str', 'silence': 'silence.str', 'poison': 'poison.str', 'freeze': 'freeze.str',
    'firehit': 'firehit.str', 'windhit': 'windhit.str', 'earthhit': 'earthhit.str', 'holyhit': 'holyhit.str', 'concentration': 'concentration.str',
    'detoxication': 'detoxication.str', 'cross': 'cross.str', 'moonlight': 'moonlight_1.str', 'steal': 'steal_coin.str', 'strip': 'strip_weapon.str',
}
# rAthena skill id -> effect name (cast / hit visual of the first job skills; the skills lane owns the skill table, this is only the visual id)
SKILL_EFFECT = {6: 'provoke', 10: 'concentration', 12: 'safetywall', 16: 'stonecurse', 18: 'firewall', 20: 'lightning', 21: 'thunderstorm', 28: 'recovery',
                33: 'angelus', 34: 'cure', 35: 'cure', 50: 'steal', 51: 'strip', 53: 'poison', 54: 'resurrection', 70: 'sanctuary', 73: 'kyrie', 77: 'cross'}

# procedural stand-ins (no .str in the client): effect name -> list of layer dicts for strfx (textures name + keys); built by stand_in()
STAND_INS = ('portal', 'warp', 'magnumbreak', 'bash')


class AudioError(ValueError):
    pass


# ------------------------------------------------------------------ mp3nametable
def parse_nametable(text):
    """mp3nametable.txt (`<map>.rsw#bgm\\NN.mp3#`, // comments, CP949 Korean comments) -> {map name (lower, no .rsw): 'bgm\\NN.mp3'}. A later line wins; commented lines are ignored."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('//') or '#' not in line:
            continue
        parts = line.split('#')
        if len(parts) < 2 or not parts[0].lower().endswith('.rsw'):
            continue
        rel = parts[1].strip().replace('\\\\', '\\').replace('/', '\\')
        if rel:
            out[parts[0][:-4].lower()] = rel
    return out


def bgm_for(table, mapname):
    return table.get(mapname.lower())


def find_bgm(folder, vfs, rel):
    """the BGM mp3 of `rel` ('bgm\\08.mp3'): the install's own BGM\\ folder next to data.grf (the usual place), else data\\ inside the GRF / loose data folder. -> ('file', path) | ('vfs', key) | None"""
    fp = AU.find_file(folder, rel)
    if fp:
        return ('file', fp)
    if vfs is not None and vfs.exists(rel):
        return ('vfs', rel)
    return None


# ------------------------------------------------------------------ clips
def _wav_clip(name, data, rate_cap=SFX_RATE, seconds=SFX_SECONDS, key=None):
    import tempfile
    # AU.read_wav wants a path: stage the bytes
    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as t:
        t.write(data)
        tmp = t.name
    try:
        rate, smp = AU.read_wav(tmp)
    finally:
        os.unlink(tmp)
    if rate > rate_cap:
        smp = AU.resample(smp, rate, rate_cap)
        rate = rate_cap
    smp, _cut = AU.trim(smp, rate, seconds)
    AU.clamp_unit(smp)
    return dict(name=name, rate=rate, samples=smp, loop=None, key=key if key is not None else name, bits=16)


def sfx_plan(monsters=DEFAULT_MONSTERS):
    """-> [(clip name, [candidate wav paths relative to data\\wav\\])]; the first existing candidate is used"""
    plan = [('fx/levelup', ['levelup.wav']), ('fx/joblevelup', ['effect\\st_job_level_up.wav']), ('fx/warp', ['effect\\warp.wav']),
            ('fx/portal', ['effect\\ef_portal.wav']), ('fx/readyportal', ['effect\\ef_readyportal.wav']), ('fx/teleport', ['effect\\ef_teleportation.wav']),
            ('fx/cast', ['effect\\ef_beginspell.wav'])]
    for w in WEAPONS:
        plan.append(('fx/swing/' + w, [SWING[w] + '.wav']))
        plan.append(('fx/hit/' + w, [HIT[w] + '.wav']))
    for i in range(4):
        plan.append(('fx/hurt/%d' % i, ['_enemy_hit%d.wav' % (i + 1)]))
    for m in monsters:
        for ev, sufs in MON_EVENTS:
            plan.append(('mon/%s/%s' % (m, ev), ['%s_%s.wav' % (m, s) for s in sufs]))
    for sid in sorted(SKILL_SOUNDS):
        plan.append(('skill/%d/fire' % sid, ['effect\\%s.wav' % SKILL_SOUNDS[sid]]))
    for nm, wav in UI_SOUNDS:          # w42: window click / cancel, item pick up, equip (the client's own UI sounds)
        plan.append((nm, [wav]))
    return plan


# w42 atmo: the client's UI sounds (data\wav\; the Korean names are CP949 in the GRF). Item pick up has no file of its own in the iRO client: the coin sound is its closest.
UI_SOUNDS = (('fx/click', '\ubc84\ud2bc\uc18c\ub9ac.wav'), ('fx/cancel', '\ucde8\uc18c.wav'), ('fx/pickup', 'se_get_coin.wav'), ('fx/equip', 'se_equip_item_01.wav'))
MAP_MON_EVENTS = ('atk', 'die', 'hurt')          # the per map monster clips (move / stand are ambient noise: not worth 40 % of the pack)


def map_monsters(mapname):
    """monster sprite names of a map (ro_conv.api.MOB_SPRITES: the monsters of its rAthena spawn list) -> tuple, () for a map without monsters"""
    from .api import MOB_SPRITES
    return tuple(MOB_SPRITES.get(mapname.lower(), ()))


def mon_plan(monsters):
    """-> [(clip name, [candidate wav])] for the atk / die / hurt sounds of `monsters`"""
    plan = []
    for m in monsters:
        for ev, sufs in MON_EVENTS:
            if ev in MAP_MON_EVENTS:
                plan.append(('mon/%s/%s' % (m, ev), ['%s_%s.wav' % (m, s) for s in sufs]))
    return plan


def build_sfx(vfs, monsters=DEFAULT_MONSTERS, warn=None, plan=None):
    """-> list of clip dicts (rose_audio.enc_snds); clips of the same wav share their samples (key = the wav path). plan = an explicit [(name, [wav])] list (the per map monsters)."""
    clips = []
    cache = {}
    for name, cands in (plan if plan is not None else sfx_plan(monsters)):
        hit = None
        for c in cands:
            p = 'wav\\' + c
            if vfs.exists(p):
                hit = p
                break
        if hit is None:
            continue
        try:
            if hit not in cache:
                cache[hit] = _wav_clip(name, vfs.read(hit), key=hit)
            c = dict(cache[hit])
            c['name'] = name
            clips.append(c)
        except (ValueError, KeyError) as e:
            if warn is not None:
                warn.append('sfx %s: %s' % (name, e))
    return clips


def build_music(folder, vfs, mapname, warn, rate=MUSIC_RATE, budget=MUSIC_BUDGET):
    """-> clip dict 'bgm/<map>' or None (warning appended): the whole track, loop 0..end, de-clicked like rose_music (declick at the seam)."""
    try:
        table = parse_nametable(vfs.read('mp3nametable.txt').decode('cp949', 'replace'))
    except KeyError:
        warn.append('bgm: mp3nametable.txt not in the client')
        return None
    rel = bgm_for(table, mapname)
    if not rel:
        warn.append('bgm: no music entry for %s in mp3nametable.txt' % mapname)
        return None
    hit = find_bgm(folder, vfs, rel)
    if hit is None:
        warn.append('bgm: %s not found (the client\'s BGM folder is missing)' % rel.replace('\\', '/'))
        return None
    cap = min(MUSIC_MAX_SECONDS, (budget - 4096) / float(rate * 4))
    path = hit[1]
    tmp = None
    try:
        if hit[0] == 'vfs':
            import tempfile
            with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as t:
                t.write(vfs.read(hit[1]))
                tmp = path = t.name
        try:
            pcm = AU.decode_music(path, rate, cap)
        except ValueError as e:
            warn.append('bgm: %s' % e)
            return None
    finally:
        if tmp:
            os.unlink(tmp)
    if len(pcm) < rate * 2:
        warn.append('bgm: decoded %d samples only' % len(pcm))
        return None
    smp = array.array('f', pcm)
    AU.clamp_unit(smp)
    if len(smp) >= int(cap * rate) - rate // 10:           # the track is longer than the pack budget: cut + fade out (the loop restarts after the fade)
        AU.fade_out(smp, rate, 3.0)
        warn.append('bgm: %s is longer than %d s, cut to the pack budget' % (rel.replace('\\', '/'), int(cap)))
    _declick(smp, rate)
    return dict(name='bgm/' + mapname.lower(), rate=rate, samples=smp, loop=(0, len(smp)), key=None, bits=16)


def _declick(x, rate, seconds=0.01, min_jump=0.05):
    n, k = len(x), int(seconds * rate)
    if n <= k or k < 2:
        return False
    jump = x[0] - x[n - 1]
    if abs(jump) <= min_jump:
        return False
    for i in range(k):
        j = n - k + i
        v = x[j] + jump * (i + 1) / float(k + 1)
        x[j] = 1.0 if v > 1.0 else (-1.0 if v < -1.0 else v)
    return True


# ------------------------------------------------------------------ effects (REFX / RFTX)
def stand_in(kind):
    """procedural effects for things the client draws in code (no .str): -> parsed-STR dict. Textures: ring_blue.tga + lens / glow sprites that exist in every client.
    portal / warp: a blue ring on the ground + a rising blue column of glow (the classic warp portal look); magnumbreak: expanding red ring; bash: a short white flash."""
    def key(frame, typ, pos=(320.0, 292.0), xy=None, color=(255.0, 255.0, 255.0, 255.0), src=5, dst=2, angle=0.0):
        return dict(frame=frame, type=typ, pos=list(pos), xy=list(xy or (0.0,) * 8), angle=angle, aniframe=0.0, anitype=0, delay=0.0, color=list(color), src=src, dst=dst)

    def quad(r, ry=None):
        ry = r if ry is None else ry
        return [r, -r, -r, r, -ry, -ry, ry, ry]
    if kind in ('portal', 'warp'):
        ring = dict(textures=['ring_blue.tga'], keys=[key(0, 0, xy=quad(34, 14), color=(120, 170, 255, 0)), key(0, 1, pos=(0, 0), color=(0, 0, 0, 24)),
                                                       key(10, 0, xy=quad(34, 14), color=(120, 170, 255, 240)), key(10, 1, pos=(0, 0), color=(0, 0, 0, 0)),
                                                       key(50, 0, xy=quad(34, 14), color=(120, 170, 255, 240)), key(50, 1, pos=(0, 0), color=(0, 0, 0, -12)), key(70, 0, xy=quad(34, 14), color=(120, 170, 255, 0))])
        col = dict(textures=['whitelight.tga'], keys=[key(0, 0, pos=(320.0, 250.0), xy=quad(18, 52), color=(100, 160, 255, 0)), key(0, 1, pos=(0, 0), color=(0, 0, 0, 20)),
                                                       key(10, 0, pos=(320.0, 250.0), xy=quad(18, 52), color=(100, 160, 255, 200)), key(10, 1, pos=(0, 0), color=(0, 0, 0, 0)),
                                                       key(50, 0, pos=(320.0, 250.0), xy=quad(18, 52), color=(100, 160, 255, 200)), key(50, 1, pos=(0, 0), color=(0, 0, 0, -10)),
                                                       key(70, 0, pos=(320.0, 250.0), xy=quad(18, 52), color=(100, 160, 255, 0))])
        return dict(version=148, fps=30, max_frame=70, layers=[ring, col])
    if kind == 'magnumbreak':
        r = [dict(textures=['ring_blue.tga'], keys=[key(0, 0, xy=quad(8, 4), color=(255, 120, 60, 255)), key(0, 1, pos=(0, 0), xy=[3.2, -3.2, -3.2, 3.2, -1.6, -1.6, 1.6, 1.6], color=(0, 0, 0, -12)),
                                                      key(20, 0, xy=quad(72, 36), color=(255, 120, 60, 15)), key(21, 0, xy=quad(72, 36), color=(255, 120, 60, 0))])]
        return dict(version=148, fps=30, max_frame=21, layers=r)
    if kind == 'bash':
        r = [dict(textures=['whitelight.tga'], keys=[key(0, 0, pos=(320.0, 262.0), xy=quad(26, 26), color=(255, 255, 255, 255)), key(0, 1, pos=(0, 0), xy=[1.0, -1.0, -1.0, 1.0, -1.0, -1.0, 1.0, 1.0], color=(0, 0, 0, -42)),
                                                      key(6, 0, pos=(320.0, 262.0), xy=quad(32, 32), color=(255, 255, 255, 3))])]
        return dict(version=148, fps=30, max_frame=6, layers=r)
    raise AudioError('unknown stand-in %r' % kind)


def _load_effect_texture(vfs, name):
    """data\\texture\\effect\\<name> -> (w, h) square RGBA at most SLOT_PX (additive textures keep alpha 255; TGA keeps its alpha) or None"""
    for cand in (name, os.path.splitext(name)[0] + '.tga', os.path.splitext(name)[0] + '.bmp'):
        p = 'texture\\effect\\' + cand
        if vfs.exists(p):
            try:
                w, h, px = mappack.img.decode(vfs.read(p), cand)
            except mappack.img.ImageError:
                return None
            px = mappack.img.resize(px, w, h, strfx.SLOT_PX, strfx.SLOT_PX)
            return px
    return None


def build_effects(vfs, names, warn):
    """-> (REFX bytes, RFTX bytes, info). names: effect names from EFFECT_STR / STAND_INS (unknown / missing ones are skipped with a warning)."""
    effects = []
    for nm in names:
        try:
            if nm in STAND_INS:
                e = stand_in(nm)
            else:
                e = strfx.parse_str(vfs.read('texture\\effect\\' + EFFECT_STR[nm]))
        except KeyError:
            warn.append('effect %s: .str not in the client' % nm)
            continue
        except (strfx.StrError, AudioError) as e2:
            warn.append('effect %s: %s' % (nm, e2))
            continue
        effects.append((nm, e))
    # texture atlas (every texture of every layer once)
    texnames = []
    seen = {}
    for _nm, e in effects:
        for ly in e['layers']:
            for t in ly['textures']:
                k = strfx.norm(t)
                if k not in seen:
                    seen[k] = len(texnames)
                    texnames.append(t)
    images = []
    slots = {}
    for t in texnames:
        px = _load_effect_texture(vfs, t)
        if px is None:
            continue
        slots[strfx.norm(t)] = len(images)
        images.append(px)
    atlas = strfx.build_atlas(images) if images else None
    refx = strfx.enc_refx(effects, slots)
    rftx = strfx.enc_rftx(atlas) if atlas else b''
    missing = len(texnames) - len(images)
    if missing:
        warn.append('effects: %d of %d textures missing (those layers are skipped)' % (missing, len(texnames)))
    return refx, rftx, dict(effects=[n for n, _ in effects], textures=len(images), missing_textures=missing, refx_bytes=len(refx), rftx_bytes=len(rftx))


# ------------------------------------------------------------------ pack
def _sections(clips):
    return [(AU.TAG_SNDS, 1, len(clips), AU.enc_snds(clips))]


def build(name, folder, out_path, progress=None, cancel=None, log=None, monsters=DEFAULT_MONSTERS, effects=None):
    """one audio pack -> `out_path` (RPG1 container). name: 'snd_common' | 'snd_<map>'. -> dict(name, bytes, clips, warnings, info). Never raises for missing optional files."""
    if not name.startswith('snd_'):
        raise AudioError('not an audio pack: %r' % name)
    from .api import check_install, BuildError, Cancelled
    ok, why = check_install(folder)
    if not ok:
        raise BuildError('Ragnarok folder not usable: %s' % why)
    warns = []

    def tick(stage, f):
        if cancel is not None and cancel():
            raise Cancelled(name)
        if progress is not None:
            progress(stage, f)
    tick('grf', 0.0)
    vfs = Vfs(folder)
    warns += list(vfs.errors)
    sections = []
    info = {}
    if name == 'snd_common':
        tick('sfx', 0.1)
        clips = build_sfx(vfs, monsters, warns)
        if clips:
            sections += _sections(clips)
        else:
            warns.append('sfx: no wav found in the client')
        tick('effects', 0.5)
        want = list(effects) if effects is not None else list(EFFECT_STR) + list(STAND_INS)
        refx, rftx, einfo = build_effects(vfs, want, warns)
        if einfo['effects']:
            sections.append((strfx.TAG_REFX, strfx.SECTION_VERSION, len(einfo['effects']), refx))
        if rftx:
            sections.append((strfx.TAG_RFTX, strfx.SECTION_VERSION, einfo['textures'], rftx))
        info = dict(clips=len(clips), **einfo)
    else:
        m = name[4:]
        tick('music', 0.1)
        c = build_music(folder, vfs, m, warns)
        clips = [c] if c else []
        mons = build_sfx(vfs, warn=warns, plan=mon_plan(map_monsters(m)))          # w42: this map's monsters (the world's NameMon clip names)
        clips += mons
        if clips:
            sections += _sections(clips)
        else:
            sections.append((AU.TAG_SNDS, 1, 0, AU.enc_snds([])))     # an empty pack is still a valid pack: the world plays no music for this map
        info = dict(clips=len(clips), monster_clips=len(mons), seconds=round(len(c['samples']) / float(c['rate']), 1) if c else 0)
    data = P.write_pack(sections)
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
        log('%s: %d bytes, %s' % (name, len(data), info))
    return {'name': name, 'bytes': len(data), 'clips': info.get('clips', 0), 'warnings': warns[:40], 'info': info, 'log': []}
