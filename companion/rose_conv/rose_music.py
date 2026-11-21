"""Full-length zone music for the SNDS section (companion-owned, ROSE w23 music; the vendored rose_audio.py stays as it is).

Owner report 2026-11-21: "in Breezy Hills the music is a very short loop and repeats very early". The vendored rose_audio.build_clips keeps only the first
MUSIC_SECONDS = 64 s of the zone BGM and loops that excerpt with a 3 s cross fade; the original client plays the whole track and starts it again at its end.
Breezy Hills (Plain01_junon.ogg) is 114.2 s long, so the world repeated after 64 s with an artificial seam.

install() wraps rose_audio.build_clips once (idempotent; rose_audio.build_sections looks the name up at call time):
  1. the vendored function builds the effects exactly as before (music=False, the same `budget` -> the same effects cap budget // 4 = 2 MB);
  2. this module adds bgm/day (and bgm/night when night_music asks for it) as the WHOLE track: mono, music_rate Hz (22.05 kHz), float32 like every SNDS sample
     (the section has one float32 block; the uploaded worlds' GsvrRoseAudio BlockCopies it as float[], so the format is unchanged and old worlds play the
     full track with no world update), loop 0..end = the natural loop at the track end, no cross fade.
     De-click: when the jump from the last sample back to the first one is larger than DECLICK_MIN_JUMP (3 of the 19 retail tracks, e.g. Dungeon_junon 0.19),
     the last DECLICK_SECONDS (10 ms) are ramped towards the first sample. The track keeps its full length.
  3. music budget MUSIC_BUDGET (16 MB) per track on top of the effects (bgm/night, when asked for, gets its own 16 MB, so day + night of the longest
     zone both stay whole): the longest retail BGM (Plain04_eldeon, Eldeon, 152.8 s) is 13.5 MB.
     A track longer than the budget is cut to it and looped with the vendored cross fade (old behaviour), with a warning.

Measured 2026-11-21 on the SearchMMO client (ffmpeg, 22.05 kHz mono): docs/design/rose_music_full.md.
"""
import array

import rose_audio as AU

MUSIC_REV = 1                            # PACK_REV step for every pack with SNDS (rose_conv/__init__.py)
MUSIC_BUDGET = 16 * 1024 * 1024          # float32 music bytes per bgm track (day; night gets its own)
MAX_TRACK_SECONDS = 600.0                # safety cap per track (retail max 152.8 s)
DECLICK_SECONDS = 0.01
DECLICK_MIN_JUMP = 0.05

_orig = None


def declick_seam(x, rate, seconds=DECLICK_SECONDS, min_jump=DECLICK_MIN_JUMP):
    """In place: when |x[-1] - x[0]| > min_jump, add a linear ramp over the last `seconds` so the loop end meets x[0]. -> True when applied.
    Length is unchanged; nothing happens for a clip shorter than the ramp."""
    n = len(x)
    k = int(seconds * rate)
    if n < 2 or k < 2 or n <= k:
        return False
    jump = x[0] - x[n - 1]
    if abs(jump) <= min_jump:
        return False
    for i in range(k):
        j = n - k + i
        v = x[j] + jump * (i + 1) / float(k + 1)
        x[j] = 1.0 if v > 1.0 else (-1.0 if v < -1.0 else v)
    return True


def track_seconds_cap(music_budget, rate, music_seconds=None):
    """seconds one track may use: the per-track music budget (float32 mono), at most MAX_TRACK_SECONDS / music_seconds (when given)."""
    share = (music_budget - 4096) / float(rate * 4)
    cap = min(share, MAX_TRACK_SECONDS)
    if music_seconds:
        cap = min(cap, float(music_seconds))
    return cap


def music_clip(pcm, rate, cap_seconds, xfade=AU.XFADE_SECONDS):
    """decoded track (decoded up to cap_seconds + xfade) -> (samples, info). Whole track: natural loop + de-click. Longer than the cap: the vendored
    excerpt behaviour (first cap_seconds, cross-fade loop)."""
    cap_n = int(cap_seconds * rate)
    if len(pcm) <= cap_n:
        smp = array.array('f', pcm)
        AU.clamp_unit(smp)
        dc = declick_seam(smp, rate)
        return smp, dict(full=True, crossfade_loop=False, declick=dc)
    smp, xf = AU.make_loop(pcm[:cap_n + int(xfade * rate)], rate, xfade)
    AU.clamp_unit(smp)
    return smp, dict(full=False, crossfade_loop=xf, declick=False)


def build_clips_full(orig, client_root, tables, zone_cells, monsters, music_seconds=None, music_rate=AU.MUSIC_RATE, sfx_rate=AU.SFX_RATE, xfade=AU.XFADE_SECONDS,
                     night_music=False, budget=AU.AUDIO_BUDGET, music=True, music_budget=MUSIC_BUDGET):
    """same signature + return as rose_audio.build_clips; music_seconds None = whole track (a number keeps the old excerpt length as an upper bound)."""
    clips, info, warn = orig(client_root, tables, zone_cells, monsters, music_rate=music_rate, sfx_rate=sfx_rate, xfade=xfade, night_music=night_music,
                             budget=budget, music=False)
    minfo = {}
    if music:
        day, nightp = AU.zone_music(zone_cells)
        tracks = [('bgm/day', day)]
        if night_music and nightp and nightp.lower() != day.lower():
            tracks.append(('bgm/night', nightp))
        cap = track_seconds_cap(music_budget, music_rate, music_seconds)
        for nm, rel in tracks:
            if not rel:
                warn.append('zone has no %s music cell' % nm)
                continue
            fp = AU.find_file(client_root, rel)
            if fp is None:
                warn.append('%s: %s not found in the client' % (nm, rel))
                continue
            if cap < 10.0:
                warn.append('%s: no room in the music budget' % nm)
                continue
            try:
                pcm = AU.decode_music(fp, music_rate, cap + xfade)
            except ValueError as e:
                warn.append('%s: %s' % (nm, e))
                continue
            if len(pcm) < music_rate * 2:
                warn.append('%s: decoded %d samples only' % (nm, len(pcm)))
                continue
            smp, mi = music_clip(pcm, music_rate, cap, xfade)
            if not mi['full']:
                warn.append('%s: %s is longer than %.1f s, looped excerpt' % (nm, rel, cap))
            clips.insert(0 if nm == 'bgm/day' else len(clips), dict(name=nm, rate=music_rate, samples=smp, loop=(0, len(smp)), key=None, bits=16, source=rel))
            minfo[nm] = dict(source=rel, seconds=round(len(smp) / float(music_rate), 2), rate=music_rate, crossfade_loop=mi['crossfade_loop'],
                             full_track=mi['full'], declick=mi['declick'])
    uniq = {}
    for c in clips:
        uniq[(c['key'], len(c['samples'])) if c['key'] is not None else id(c)] = len(c['samples']) * 4
    info = dict(info, clips=len(clips), music=minfo, sample_bytes=sum(uniq.values()))
    return clips, info, warn


def install():
    """wraps rose_audio.build_clips once (idempotent)."""
    global _orig
    if _orig is not None:
        return
    _orig = AU.build_clips
    orig = _orig

    def build_clips(*a, **kw):
        return build_clips_full(orig, *a, **kw)
    build_clips.__doc__ = build_clips_full.__doc__
    AU.build_clips = build_clips
