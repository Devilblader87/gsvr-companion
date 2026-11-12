"""CD music of a map -> extra clips of the GSV3 "SNDS" section (tools/pack.py). The mp3 decode uses the PyPI package `miniaudio` (dr_mp3 based, a 0.3 MB
wheel, frozen into the companion) and falls back to an `ffmpeg` binary when that package is missing or cannot read a file; with neither, or without the
tracks, the map simply has no music. Spec: docs/goldsrc_base_layer.md "Music clips in SNDS".

Which tracks a map can play (hl.so / opfor.so, tools/hlport and docs/decomp/hl_port/global_part2.md:39):
  worldspawn `sounds` N  -> CWorld::KeyValue stores it as gpGlobals->cdAudioTrack [obs CWorld::KeyValue, decomp/valve/evidence/hl_so_cond/CWorld.c];
                            the engine sends "cd loop N" to the client when it enters the server [ext xash3d-fwgs engine/server/sv_client.c:1470],
                            so this track LOOPS (c0a0 `sounds 3`, c1a0c `sounds 5`, ...). The worldspawn key is `sounds`, not `cdtrack`.
  trigger_cdaudio / target_cdaudio `health` N -> PlayCDTrack: "cd play N" (plays once), -1 = "cd stop", 0..30 valid, anything else is an
                            alert and sends nothing [obs PlayCDTrack 0x132220, CTriggerCDAudio::Touch, CTargetCDAudio::Use / Think / Play].
Track N -> file: the client's cd emulation reads media/cdaudio.txt, one entry per track, entry i = track i + 1; a missing file is created with the
Valve playlist, `blank` = no audio data [ext xash3d-fwgs engine/client/dll_int/cl_game.c:143-171 default list, :182-215 parser; the retail Steam
install ships no cdaudio.txt, but exactly the 27 mp3 of that list in valve/media, tracks 2..28]. Files are looked up in tools/gamedirs.py order
(<mod>_addon, <mod>, <mod>_downloads, valve) under media/.

Storage: the pack holds only the START of each track (a GoldSrc CD track is 1 - 4 minutes; float32 SNDS samples would be 10 - 40 MB each):
at most `seconds` (default 60) of mono audio at `rate` (default 11025 Hz) = 2.6 MB per track, the last 2 s fade out when the track was cut.
Clip name = the TRACK NUMBER, "media/cdtrack<NN>.mp3" (clip_name; GsvrEntAudio.MusicClipName builds the same string), whatever file the playlist
(default or the install's media/cdaudio.txt) maps that track to, so the runtime needs no playlist and a custom cdaudio.txt cannot desynchronise it.
loopStart 0, loopEnd = clip end (so "cd loop" repeats the stored part, "cd play" plays it once). Clips are appended after every other SNDS clip,
so existing clip indices never shift. Track 1 (and 0) is the blank data track of the default playlist: nothing is packed for a `blank` entry.

Decoders (both give mono = (L + R) / 2 at `rate`; the plain average keeps the level and never clips):
  miniaudio  decoder + resampler inside the wheel; ~0.04 s for 60 s of audio. Needs Python >= 3.11 (the companion's build venv is 3.12).
  ffmpeg     any ffmpeg binary ($GSVR_FFMPEG, next to the tools / the companion, or PATH); about 0.1 s plus a process start.
tools/test_music.py checks the two against each other on the real valve/media files (RMS of the difference 29 - 43 dB under the signal).
"""
import os, shutil, subprocess, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import miniaudio
except ImportError:                       # not installed (Python < 3.11, a plain checkout): the ffmpeg fallback
    miniaudio = None

# xash3d-fwgs engine/client/dll_int/cl_game.c:143-171 (CL_CreatePlaylist), index 0 = track 1
DEFAULT_PLAYLIST = (
    "blank", "Half-Life01.mp3", "Prospero01.mp3", "Half-Life12.mp3", "Half-Life07.mp3", "Half-Life10.mp3", "Suspense01.mp3",
    "Suspense03.mp3", "Half-Life09.mp3", "Half-Life02.mp3", "Half-Life13.mp3", "Half-Life04.mp3", "Half-Life15.mp3",
    "Half-Life14.mp3", "Half-Life16.mp3", "Suspense02.mp3", "Half-Life03.mp3", "Half-Life08.mp3", "Prospero02.mp3",
    "Half-Life05.mp3", "Prospero04.mp3", "Half-Life11.mp3", "Half-Life06.mp3", "Prospero03.mp3", "Half-Life17.mp3",
    "Prospero05.mp3", "Suspense05.mp3", "Suspense07.mp3",
)
MAX_TRACK = 30            # PlayCDTrack: health + 1 < 32 -> -1 .. 30
FADE_SECONDS = 2.0
DEFAULT_SECONDS = 60.0
DEFAULT_RATE = 11025


def _int(v, d=0):
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return d


def parse_playlist(text):
    """cdaudio.txt text -> list of file names (None = blank). Tokens as COM_ParseFile: whitespace separated, `//` comments."""
    out = []
    for line in text.splitlines():
        line = line.split("//", 1)[0]
        for tok in line.split():
            tok = tok.strip('"')
            if tok.lower() == "blank":
                out.append(None)
            else:
                out.append(tok.lstrip("/") if tok.startswith("/") else "media/" + tok)
    return out


def playlist(game_root, mod):
    """-> list, index = track - 1, entry = path relative to the content folder ("media/Half-Life01.mp3") or None (blank)."""
    from gamedirs import content_dirs
    for d in content_dirs(game_root, mod):
        p = os.path.join(d, "media", "cdaudio.txt")
        if os.path.isfile(p):
            with open(p, "r", encoding="latin-1") as fh:
                return parse_playlist(fh.read())
    return [None if n == "blank" else "media/" + n for n in DEFAULT_PLAYLIST]


def clip_name(track):
    """SNDS clip name of CD track `track` (1..MAX_TRACK): canonical per track number, see the module doc. Keep in step with
    GsvrEntAudio.MusicClipName (tools/test_music.py pins both to the same format)."""
    return f"media/cdtrack{track:02d}.mp3"


def tracks_of(ents):
    """-> [(track, loops)] in first-use order: worldspawn `sounds` first (loops), then trigger_cdaudio / target_cdaudio `health` (play once).
    Only tracks 1..MAX_TRACK are kept (-1 stops, 0 is no track, > 30 is out of range); a blank playlist entry (track 1 by default) packs nothing."""
    world, trig = [], []
    for e in ents:
        c = e.get("classname", "")
        if c == "worldspawn":
            t = _int(e.get("sounds"))
            if 1 <= t <= MAX_TRACK and t not in world:
                world.append(t)
        elif c in ("trigger_cdaudio", "target_cdaudio"):
            t = _int(e.get("health"))
            if 1 <= t <= MAX_TRACK and t not in trig:
                trig.append(t)
    return [(t, True) for t in world] + [(t, False) for t in trig if t not in world]


def find(game_root, mod, rel):
    from gamedirs import content_dirs
    parts = rel.replace("\\", "/").split("/")
    for root in content_dirs(game_root, mod):
        d = root
        ok = True
        for part in parts:
            hit = [f for f in os.listdir(d) if f.lower() == part.lower()] if os.path.isdir(d) else []
            if not hit:
                ok = False
                break
            d = os.path.join(d, hit[0])
        if ok and os.path.isfile(d):
            return d
    return None


def fade(vals, rate, tail=True, seconds=FADE_SECONDS):
    """In place: 20 ms fade in (no click at the start), optional fade out over the last `seconds`."""
    n = len(vals)
    a = min(n, int(0.02 * rate))
    for i in range(a):
        vals[i] *= i / a
    if tail:
        b = min(n, int(seconds * rate))
        for i in range(b):
            vals[n - 1 - i] *= i / b
    return vals


def ffmpeg_path():
    """$GSVR_FFMPEG, an ffmpeg(.exe) next to this script or the frozen companion, else PATH."""
    env = os.environ.get("GSVR_FFMPEG")
    if env and os.path.isfile(env):
        return env
    for d in (os.path.dirname(os.path.abspath(__file__)), os.path.dirname(os.path.abspath(sys.executable))):
        for n in ("ffmpeg.exe", "ffmpeg"):
            p = os.path.join(d, n)
            if os.path.isfile(p):
                return p
    return shutil.which("ffmpeg")


def backends():
    """-> the decoders usable here, preferred first: ["miniaudio", "ffmpeg"] (either, both or none)."""
    return (["miniaudio"] if miniaudio is not None else []) + (["ffmpeg"] if ffmpeg_path() else [])


def decode_miniaudio(path, seconds, rate):
    """mp3 -> list of float in [-1, 1], mono (L + R) / 2, `rate` Hz, at most `seconds` (the stream stops there: no full-track decode)."""
    if miniaudio is None:
        raise OSError("miniaudio not installed")
    import array
    want = int(seconds * rate)
    out = array.array("f")
    try:
        for chunk in miniaudio.stream_file(path, output_format=miniaudio.SampleFormat.FLOAT32, nchannels=1, sample_rate=rate, frames_to_read=4096):
            out.extend(chunk)
            if len(out) >= want:
                break
    except Exception as e:                # noqa: BLE001 — miniaudio.MiniaudioError / DecodeError, a truncated file ...
        raise ValueError("miniaudio failed: " + str(e)[:200])
    if not out:
        raise ValueError("miniaudio decoded no audio")
    return [max(-1.0, min(1.0, v)) for v in out[:want]]


def decode_ffmpeg(path, seconds, rate):
    """Same through an ffmpeg binary. Decoded as stereo and averaged here: ffmpeg's own `-ac 1` downmix is +3 dB (0.707 L + 0.707 R) and clips."""
    exe = ffmpeg_path()
    if not exe:
        raise OSError("ffmpeg not found")
    cmd = [exe, "-v", "error", "-nostdin", "-i", path, "-t", f"{seconds:g}", "-ac", "2", "-ar", str(rate), "-f", "f32le", "-"]
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    if r.returncode != 0 or not r.stdout:
        raise ValueError("ffmpeg failed: " + r.stderr.decode("latin-1", "replace").strip()[:200])
    import array
    a = array.array("f")
    a.frombytes(r.stdout[:len(r.stdout) // 8 * 8])
    if sys.byteorder == "big":
        a.byteswap()
    return [max(-1.0, min(1.0, (x + y) * 0.5)) for x, y in zip(a[0::2], a[1::2])]


def decode(path, seconds, rate, backend=None):
    """mp3 -> samples (see decode_miniaudio). backend None = miniaudio first, ffmpeg when that is missing or fails on this file.
    Raises OSError (no decoder at all) / ValueError."""
    if backend == "miniaudio":
        return decode_miniaudio(path, seconds, rate)
    if backend == "ffmpeg":
        return decode_ffmpeg(path, seconds, rate)
    err = None
    if miniaudio is not None:
        try:
            return decode_miniaudio(path, seconds, rate)
        except ValueError as e:
            err = e
    if ffmpeg_path():
        return decode_ffmpeg(path, seconds, rate)
    if err is not None:
        raise err
    raise OSError("no mp3 decoder: install the `miniaudio` package (Python 3.11+) or put ffmpeg on PATH")


def load(ents, game_root, mod, max_bytes=8 * 1024 * 1024, seconds=DEFAULT_SECONDS, rate=DEFAULT_RATE, log=print, have=()):
    """-> [(name, rate, samples, loopStart, loopEnd, bits)] like sounds.load, music clips of the map within the byte budget
    (worldspawn track first; a clip per TRACK number, also when two tracks map to the same file)."""
    tracks = tracks_of(ents)
    if not tracks or not game_root:
        return []
    pl = playlist(game_root, mod)
    clips, used, missing, skipped, seen, cache = [], 0, [], [], set(have), {}
    for t, _loop in tracks:
        rel = pl[t - 1] if t - 1 < len(pl) else None
        if not rel:
            continue                                  # blank entry / track past the playlist: nothing to play
        name = clip_name(t)
        if name in seen:
            continue
        p = find(game_root, mod, rel)
        if not p:
            missing.append(f"{t}:{rel}")
            continue
        if p not in cache:
            try:
                vals = decode(p, seconds, rate)
            except (OSError, ValueError, subprocess.SubprocessError) as e:
                log(f"music track {t} ({rel}): skipped ({e})")
                cache[p] = None
                continue
            cut = len(vals) >= int(seconds * rate) - 1
            cache[p] = fade(vals, rate, tail=cut)
        vals = cache[p]
        if vals is None:
            continue
        if used + 4 * len(vals) > max_bytes:
            skipped.append(name)
            continue
        used += 4 * len(vals)
        seen.add(name)
        clips.append((name, rate, vals, 0, len(vals), 16))
    log(f"music: {len(clips)} tracks ({', '.join(c[0] for c in clips)}), {used / 1e6:.2f} MB"
        + (f", {len(missing)} missing ({', '.join(missing)})" if missing else "")
        + (f", {len(skipped)} over the {max_bytes / 1e6:.0f} MB budget: {', '.join(skipped)}" if skipped else ""))
    return clips
