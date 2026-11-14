"""Zone music + core sound effects for the Unity runtime (RPG1 1.5, docs/RPG1_PACK.md section 12): the optional section SNDS.

The layout is byte for byte the GoldSrcVR "SNDS" section (tools/sounds.py of the half-life repo, read by GsvrPackLoader.ReadSounds): mono float32 PCM in one block, a clip table,
names. Clips are named by EVENT, not by file (the runtime never sees a game path): lower case ASCII with '/' separators.

  bgm/day, bgm/night   zone music (LIST_ZONE cells 5 / 6, Sound\\BGM\\*.ogg). Mono, music_rate Hz, the first `music_seconds` of the track, made loopable by a cross fade
                       (the last `xfade` seconds fade into the first ones, so the loop point is seamless); loopStart 0, loopEnd = clip end. bgm/night only with night_music
                       and only when the night file differs from the day file.
  ui/click ui/open ui/close ui/error   FILE_SOUND rows 7 (button), 3 (window open), 4 (window close), 1 (error / cannot do that)
  fx/levelup           row 16, fx/pickup row 531 (item picked up), fx/miss row 26 (melee miss)
  fx/swing0..2         the avatar weapon's swing sounds (LIST_WEAPON cell 40 of the start sword = the middle one, +- 1 inside the same FILE_SOUND family)
  fx/hit               weapon hit sound vs the default material, fx/hurt = the player is hit (unarmed hit sound on cloth, LIST_HITSOUND row 1)
  mon/<npc>/die|atk|pain|hit   per monster type of the window: LIST_NPC cell 35 die sound, 31 attack, 32 hit (pain) sound; `hit` = the start weapon's hit sound against
                       the monster's material (LIST_NPC cell 28, LIST_WEAPON cell 42 = LIST_HITSOUND row, 3 material columns)
  skill/<id>/cast|fire|hit   per skill of SKIL_SOUND_SKILLS (level 1 row): LIST_SKILL cell 58 cast sound, 73 fire sound, 76 hit sound
Clips whose table cell is empty or whose file is missing are left out (the runtime treats a missing name as "no sound").

Records may share their sample range (same source and trim = one copy of the samples, several names), see enc_snds.

Sources: loose files under the client's Sound\\ folder (wav 16 bit PCM, mostly 44.1 kHz mono; BGM = ogg vorbis 44.1 kHz stereo). Read only. Music needs an ogg decoder: the PyPI package
`miniaudio` when installed, else an `ffmpeg` binary ($ROSE_FFMPEG, PATH); with neither the pack simply has no music (warning), sound effects need only the standard library.

Size: Birth Island default = music 64 s at 22.05 kHz mono float32 = 5.6 MB + effects well under 1 MB; `budget` (default 8 MB) caps the section, the music length shrinks to fit.
"""
import array
import math
import os
import shutil
import struct
import subprocess
import wave

import rosefmt as F

try:
    import miniaudio
except ImportError:
    miniaudio = None

TAG_SNDS = b'SNDS'
SNDS_HDR = struct.Struct('<4i')
SNDS_REC = struct.Struct('<8i')
MUSIC_SECONDS = 64.0
MUSIC_RATE = 22050
SFX_RATE = 22050
XFADE_SECONDS = 3.0
AUDIO_BUDGET = 8 * 1024 * 1024
MAX_CLIPS = 4096
MAX_NAME = 64
# level 1 skill ids of the four real bar skills (rose_gameplay.DEFAULT_SKILLS without the basic attack): Power Attack, Double Slash, Mana Bolt, Cure
SKIL_SOUND_SKILLS = (1501, 1541, 901, 931)
# FILE_SOUND rows used by the interface / system events (docs/ROSE_SPEC.md 3.15, verified on the SearchMMO client 2026-11-13)
FS_ERROR, FS_WINDOW_OPEN, FS_WINDOW_CLOSE, FS_BUTTON, FS_LEVELUP, FS_MISS_MELEE, FS_PICKUP = 1, 3, 4, 7, 16, 26, 531
START_WEAPON_ROW = 1                # LIST_WEAPON row of the avatar's start sword (rose_chars: AVATAR_WEAPON_TYPES = (1, 0), code(12) % 1000 or 1)
LEVELUP_MAX_SECONDS = 3.5
SFX_MAX_SECONDS = 4.0
LITTLE = struct.pack('=H', 1) == struct.pack('<H', 1)


# ------------------------------------------------------------------ section codec (layout of the GoldSrcVR SNDS section)
def enc_snds(clips, clamp=True):
    """clips: list of dict(name, rate, samples, loop=None | (start, end), key=None, bits=16). `samples` is an array('f') / list of floats in [-1, 1].
    Clips with the same non-None `key` and the same sample count share one range of the sample block. -> section bytes (4-byte aligned)."""
    names = bytearray()
    recs = []
    block = array.array('f')
    seen = {}
    for c in clips:
        nm = c['name'].encode('ascii')
        smp = c['samples']
        if clamp and any(v > 1.0 or v < -1.0 for v in smp):          # resampled / crossfaded music can overshoot slightly (peak 1.049 seen): hard clamp to the SNDS range
            smp = array.array('f', (1.0 if v > 1.0 else (-1.0 if v < -1.0 else v) for v in smp))
        n = len(smp)
        k = (c.get('key'), n) if c.get('key') is not None else None
        if k is not None and k in seen:
            base = seen[k]
        else:
            base = len(block)
            block.extend(smp)
            if k is not None:
                seen[k] = base
        loop = c.get('loop')
        ls, le = (-1, n) if loop is None else (int(loop[0]), int(loop[1]))
        recs.append((int(c['rate']), n, base, ls, le, len(names), len(nm), int(c.get('bits', 16))))
        names += nm
    while len(names) % 4:
        names += b'\0'
    out = bytearray(SNDS_HDR.pack(len(clips), len(names), len(block), 0))
    for r in recs:
        out += SNDS_REC.pack(*r)
    out += names
    if not LITTLE:
        block.byteswap()
    out += block.tobytes()
    return bytes(out)


def dec_snds(d):
    """-> list of dict(name, rate, count, base, loop_start, loop_end, bits, samples: array('f')). Raises ValueError when a record leaves its block (same rule as GsvrPackLoader.ReadSounds)."""
    if len(d) < SNDS_HDR.size:
        raise ValueError('SNDS too short')
    n, name_bytes, total, _z = SNDS_HDR.unpack_from(d, 0)
    rec_off = SNDS_HDR.size
    names_off = rec_off + n * SNDS_REC.size
    smp_off = names_off + name_bytes
    if n < 0 or n > MAX_CLIPS or name_bytes < 0 or total < 0 or smp_off + total * 4 > len(d):
        raise ValueError('SNDS header out of range')
    block = array.array('f')
    block.frombytes(bytes(d[smp_off:smp_off + total * 4]))
    if not LITTLE:
        block.byteswap()
    out = []
    for i in range(n):
        rate, cnt, base, ls, le, noff, nlen, bits = SNDS_REC.unpack_from(d, rec_off + i * SNDS_REC.size)
        if rate <= 0 or cnt < 0 or base < 0 or base + cnt > total or noff < 0 or nlen < 0 or noff + nlen > name_bytes:
            raise ValueError('SNDS clip %d out of range' % i)
        out.append(dict(name=bytes(d[names_off + noff:names_off + noff + nlen]).decode('ascii'), rate=rate, count=cnt, base=base, loop_start=ls, loop_end=le, bits=bits,
                        samples=block[base:base + cnt]))
    return out


# ------------------------------------------------------------------ file access
def find_file(root, rel):
    """case-insensitive lookup of a client path like 'Sound\\avata\\Levelup_1.wav' below `root`; None when missing (a Linux / macOS client folder is case sensitive)."""
    parts = [p for p in rel.replace('\\\\', '/').replace('\\', '/').split('/') if p]
    cur = root
    for p in parts:
        try:
            names = os.listdir(cur)
        except OSError:
            return None
        hit = p if p in names else None
        if hit is None:
            low = p.lower()
            for nm in sorted(names):
                if nm.lower() == low:
                    hit = nm
                    break
        if hit is None:
            return None
        cur = os.path.join(cur, hit)
    return cur if os.path.isfile(cur) else None


def read_wav(path):
    """PCM wav (8 / 16 / 24 / 32 bit, any channel count) -> (rate, array('f') mono). Raises ValueError for anything else (ADPCM, float)."""
    try:
        w = wave.open(path, 'rb')
    except (wave.Error, EOFError) as e:
        raise ValueError('%s: %s' % (os.path.basename(path), e))
    with w:
        ch, width, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if ch < 1 or rate < 1000 or width not in (1, 2, 3, 4):
        raise ValueError('%s: unsupported wav (channels %d, width %d, rate %d)' % (os.path.basename(path), ch, width, rate))
    cnt = len(raw) // (width * ch)
    if width == 2:
        a = array.array('h')
        a.frombytes(raw[:cnt * ch * 2])
        if not LITTLE:
            a.byteswap()
        vals = [v / 32768.0 for v in a]
    elif width == 1:
        vals = [(b - 128) / 128.0 for b in raw[:cnt * ch]]
    elif width == 3:
        vals = [int.from_bytes(raw[i:i + 3], 'little', signed=True) / 8388608.0 for i in range(0, cnt * ch * 3, 3)]
    else:
        a = array.array('i')
        a.frombytes(raw[:cnt * ch * 4])
        if not LITTLE:
            a.byteswap()
        vals = [v / 2147483648.0 for v in a]
    if ch > 1:
        vals = [sum(vals[i:i + ch]) / ch for i in range(0, cnt * ch, ch)]
    return rate, array.array('f', vals)


def resample(x, src, dst):
    """mono float samples src Hz -> dst Hz: box average for integer down ratios, otherwise linear interpolation after a box prefilter (good enough for effects)."""
    if src == dst or not len(x):
        return array.array('f', x)
    if src > dst and src % dst == 0:
        k = src // dst
        n = len(x) // k
        return array.array('f', [sum(x[i * k:(i + 1) * k]) / k for i in range(n)])
    if src > dst:
        k = int(math.ceil(src / dst))
        pre = array.array('f', [0.0] * len(x))
        acc = 0.0
        for i in range(len(x)):
            acc += x[i]
            if i >= k:
                acc -= x[i - k]
            pre[i] = acc / min(i + 1, k)
    else:
        pre = x
    n = max(1, int(len(pre) * dst / src))
    out = array.array('f', [0.0] * n)
    step = src / dst
    last = len(pre) - 1
    for i in range(n):
        p = i * step
        j = int(p)
        f = p - j
        a = pre[j if j <= last else last]
        b = pre[j + 1 if j + 1 <= last else last]
        out[i] = a + (b - a) * f
    return out


def fade_out(x, rate, seconds):
    n = min(len(x), int(seconds * rate))
    for i in range(n):
        j = len(x) - n + i
        x[j] = x[j] * (1.0 - (i + 1) / n)


def trim(x, rate, max_seconds):
    """-> (samples, cut): at most max_seconds, with a 40 ms fade out when it was cut"""
    n = int(max_seconds * rate)
    if len(x) <= n:
        return array.array('f', x), False
    y = array.array('f', x[:n])
    fade_out(y, rate, 0.04)
    return y, True


def clamp_unit(x):
    for i in range(len(x)):
        v = x[i]
        if v > 1.0:
            x[i] = 1.0
        elif v < -1.0:
            x[i] = -1.0
    return x


def make_loop(x, rate, xfade):
    """Seamless loop of a track excerpt: output = x[X:], its last X samples cross faded (equal power) with x[0:X], so playing the end and then the start again
    continues x[n-1] -> ... -> x[X-1] -> x[X] without a jump. A track too short for that is returned as is (loop by plain wrap). -> (samples, looped_by_crossfade)"""
    n = len(x)
    xs = int(xfade * rate)
    if xs < 16 or n < 4 * xs:
        return array.array('f', x), False
    out = array.array('f', x[xs:])
    base = n - 2 * xs
    for i in range(xs):
        t = (i + 0.5) / xs
        out[base + i] = x[n - xs + i] * math.cos(t * math.pi / 2) + x[i] * math.sin(t * math.pi / 2)
    return out, True


# ------------------------------------------------------------------ music decoding
def ffmpeg_path():
    p = os.environ.get('ROSE_FFMPEG')
    if p and os.path.isfile(p):
        return p
    return shutil.which('ffmpeg')


def decode_music(path, rate, seconds):
    """ogg (or anything ffmpeg / miniaudio reads) -> mono float samples at `rate`, the first `seconds` of it. Raises ValueError when no decoder works."""
    err = []
    if miniaudio is not None:
        try:
            dec = miniaudio.decode_file(path, output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1, sample_rate=rate)
            a = array.array('h')
            a.frombytes(dec.samples.tobytes() if hasattr(dec.samples, 'tobytes') else bytes(dec.samples))
            n = int(seconds * rate) if seconds else len(a)
            return array.array('f', [v / 32768.0 for v in a[:n]])
        except Exception as e:      # the wheel cannot read this file: ffmpeg next
            err.append('miniaudio: %s' % e)
    ff = ffmpeg_path()
    if ff:
        cmd = [ff, '-v', 'error', '-nostdin', '-i', path]
        if seconds:
            cmd += ['-t', '%.3f' % seconds]
        cmd += ['-ac', '1', '-ar', str(rate), '-f', 'f32le', '-']
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if r.returncode == 0 and r.stdout:
            a = array.array('f')
            a.frombytes(r.stdout[:len(r.stdout) // 4 * 4])
            if not LITTLE:
                a.byteswap()
            return clamp_unit(a)
        err.append('ffmpeg: %s' % (r.stderr.decode('utf-8', 'replace').strip()[:200] or 'exit %d' % r.returncode))
    else:
        err.append('no ogg decoder (install the PyPI package miniaudio or an ffmpeg binary, or set ROSE_FFMPEG)')
    raise ValueError('; '.join(err))


# ------------------------------------------------------------------ the event plan (pure table logic, testable with fake tables)
def _cell(stb, row, col):
    try:
        v = stb['cells'][row][col]
    except (IndexError, TypeError):
        return ''
    return str(v).strip()


def _int(s, d=0):
    try:
        return int(float(str(s).strip()))
    except (TypeError, ValueError):
        return d


def sound_path(tables, sid):
    """FILE_SOUND row -> client path ('Sound\\attack\\Sword_D01.wav') or ''"""
    if sid <= 0:
        return ''
    return _cell(tables['sound'], sid, 0).replace('\\\\', '\\')


def hit_sound_id(tables, hit_row, material):
    """LIST_HITSOUND row x material (1 = iron armour, 2 = cloth, 3 = stone; the 3 cells of the row) -> FILE_SOUND row or 0"""
    m = 1 if material < 1 else (3 if material > 3 else material)
    return _int(_cell(tables['hitsound'], hit_row, m - 1))


def plan_events(tables, monsters, skills=SKIL_SOUND_SKILLS, weapon_row=START_WEAPON_ROW):
    """-> list of (clip name, FILE_SOUND row, max seconds or None), in priority order (the section budget drops from the end). tables: dict of parsed STBs
    (rosefmt.stb layout): sound = FILE_SOUND, npc = LIST_NPC, skill = LIST_SKILL, weapon = LIST_WEAPON, hitsound = LIST_HITSOUND. monsters: iterable of npc ids."""
    plan = []

    def add(name, sid, mx=None):
        if sid and sid > 0 and sound_path(tables, sid):
            plan.append((name, sid, mx))
    add('ui/click', FS_BUTTON)
    add('ui/open', FS_WINDOW_OPEN)
    add('ui/close', FS_WINDOW_CLOSE)
    add('ui/error', FS_ERROR)
    add('fx/levelup', FS_LEVELUP, LEVELUP_MAX_SECONDS)
    add('fx/pickup', FS_PICKUP)
    add('fx/miss', FS_MISS_MELEE)
    swing = _int(_cell(tables['weapon'], weapon_row, 40))
    keys = tables['sound'].get('keys') or []
    wkey = keys[swing] if 0 < swing < len(keys) else ''
    fam = [s for s in (swing - 1, swing, swing + 1) if 0 < s < len(keys) and sound_path(tables, s) and keys[s] == wkey] if wkey else ([swing] if swing > 0 else [])
    for i, s in enumerate(fam[:3]):
        add('fx/swing%d' % i, s)
    whit = _int(_cell(tables['weapon'], weapon_row, 42))
    add('fx/hit', hit_sound_id(tables, whit, 1))
    add('fx/hurt', hit_sound_id(tables, 1, 2))
    for sid in skills:
        add('skill/%d/cast' % sid, _int(_cell(tables['skill'], sid, 58)))
        add('skill/%d/fire' % sid, _int(_cell(tables['skill'], sid, 73)))
        add('skill/%d/hit' % sid, _int(_cell(tables['skill'], sid, 76)))
    for nid in sorted(set(int(n) for n in monsters)):
        add('mon/%d/die' % nid, _int(_cell(tables['npc'], nid, 35)))
        add('mon/%d/atk' % nid, _int(_cell(tables['npc'], nid, 31)))
        add('mon/%d/pain' % nid, _int(_cell(tables['npc'], nid, 32)))
        add('mon/%d/hit' % nid, hit_sound_id(tables, whit, _int(_cell(tables['npc'], nid, 28), 1)))
    return plan


def zone_music(zone_cells):
    """LIST_ZONE cells -> (day path, night path) ('' when empty)"""
    def c(i):
        return str(zone_cells[i]).strip().replace('\\\\', '\\') if i < len(zone_cells) else ''
    return c(5), c(6)


# ------------------------------------------------------------------ building the section
def load_tables(vfs):
    t = {}
    for key, path in (('sound', 'FILE_SOUND'), ('npc', 'LIST_NPC'), ('skill', 'LIST_SKILL'), ('weapon', 'LIST_WEAPON'), ('hitsound', 'LIST_HITSOUND')):
        t[key] = F.stb(vfs.read('3DDATA/STB/%s.STB' % path))
    return t


def build_clips(client_root, tables, zone_cells, monsters, music_seconds=MUSIC_SECONDS, music_rate=MUSIC_RATE, sfx_rate=SFX_RATE, xfade=XFADE_SECONDS,
                night_music=False, budget=AUDIO_BUDGET, music=True):
    """-> (clips for enc_snds, info dict, warnings). Never raises on missing files / decoders (the pack just gets fewer sounds)."""
    warn = []
    clips = []
    skipped = []
    plan = plan_events(tables, monsters)
    cache = {}
    sfx_bytes = 0
    for name, sid, mx in plan:
        rel = sound_path(tables, sid)
        key = (sid, mx)
        if key not in cache:
            fp = find_file(client_root, rel)
            if fp is None:
                cache[key] = None
                skipped.append('%s (%s missing)' % (name, rel))
                continue
            try:
                rate, smp = read_wav(fp)
                if rate > sfx_rate:
                    smp = resample(smp, rate, sfx_rate)
                    rate = sfx_rate
                smp, _cut = trim(smp, rate, mx or SFX_MAX_SECONDS)
                clamp_unit(smp)
                cache[key] = (rate, smp)
            except ValueError as e:
                cache[key] = None
                skipped.append('%s (%s)' % (name, e))
                continue
        if cache[key] is None:
            continue
        rate, smp = cache[key]
        add = 0 if any(c.get('key') == key for c in clips) else len(smp) * 4
        if sfx_bytes + add > budget // 4:
            skipped.append('%s (effects budget)' % name)
            continue
        sfx_bytes += add
        clips.append(dict(name=name, rate=rate, samples=smp, loop=None, key=key, bits=16, source=rel))
    minfo = {}
    if music:
        day, nightp = zone_music(zone_cells)
        tracks = [('bgm/day', day)]
        if night_music and nightp and nightp.lower() != day.lower():
            tracks.append(('bgm/night', nightp))
        left = budget - sfx_bytes - 4096
        for nm, rel in tracks:
            if not rel:
                warn.append('zone has no %s music cell' % nm)
                continue
            fp = find_file(client_root, rel)
            if fp is None:
                warn.append('%s: %s not found in the client' % (nm, rel))
                continue
            secs = min(music_seconds, left // len(tracks) / float(music_rate * 4))
            if secs < 10.0:
                warn.append('%s: no room in the audio budget' % nm)
                continue
            try:
                pcm = decode_music(fp, music_rate, secs + xfade)
            except ValueError as e:
                warn.append('%s: %s' % (nm, e))
                continue
            if len(pcm) < music_rate * 2:
                warn.append('%s: decoded %d samples only' % (nm, len(pcm)))
                continue
            loop_pcm, xf = make_loop(pcm, music_rate, xfade)
            clips.insert(0 if nm == 'bgm/day' else len(clips), dict(name=nm, rate=music_rate, samples=loop_pcm, loop=(0, len(loop_pcm)), key=None, bits=16, source=rel))
            minfo[nm] = dict(source=rel, seconds=round(len(loop_pcm) / float(music_rate), 2), rate=music_rate, crossfade_loop=xf)
    uniq = {}
    for c in clips:
        uniq[(c['key'], len(c['samples'])) if c['key'] is not None else id(c)] = len(c['samples']) * 4
    info = dict(clips=len(clips), effects=len([c for c in clips if not c['name'].startswith('bgm/')]), music=minfo, skipped=skipped,
                sample_bytes=sum(uniq.values()), sfx_rate=sfx_rate, music_rate=music_rate)
    return clips, info, warn


def build_sections(vfs, zone, monsters=None, side_actors=None, client_root=None, **opts):
    """-> (sections, info, warnings): [(b'SNDS', 1, clip count, bytes)] (empty list when nothing could be built) for rose_pack.convert. `monsters`: npc ids of the window
    (default: every actor of side_actors). Never raises."""
    try:
        root = client_root or vfs.root
        tables = load_tables(vfs)
        mons = monsters if monsters is not None else [int(k) for k in (side_actors or {})]
        clips, info, warn = build_clips(root, tables, zone.get('cells') or [], mons, **opts)
        if not clips:
            warn.append('SNDS: no sound found (is the client\'s Sound folder present?)')
            return [], info, warn
        data = enc_snds(clips)
        info['bytes'] = len(data)
        info['names'] = [c['name'] for c in clips]
        return [(TAG_SNDS, 1, len(clips), data)], info, warn
    except Exception as e:
        return [], {}, ['SNDS: %s' % e]
