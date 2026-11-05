#!/usr/bin/env python3
"""The map's sounds -> pack section WSND (docs/sound_plan.md S1). Layout = GSVR's SNDS (half-life tools/sounds.py),
so the world loads them the same way (AudioClip.Create + SetData, one BlockCopy):

  int32 N, int32 nameBytes (padded), int32 totalSamples, int32 0
  N x 8 int32: rate, sampleCount, sampleBase (index into samples), loopStart (-1), loopEnd, nameOff, nameLen, bits
  names: ASCII, lower case, backslash separators as in the game ("sound\\interface\\questnew.wav"), padded to 4
  float32 samples[totalSamples]: mono PCM in [-1, 1]; rates above 22050 Hz are halved (box filter)

Which files: every .wav the map script names in a string literal (CreateSound / CreateSoundFilenameWithLabel paths
[inf: the script is the only place a TD map's sounds are named]) plus the menu / HUD click sounds. .mp3 (music) are
skipped (no decoder, S5). The 1.2x MPQs store the .wav files with the MPQ sound compressions (Huffman + ADPCM): tools/mpqsound.py
decodes them (BUGS SND-MPQ; needs the install's Storm.dll for the Huffman weights, without it those clips are skipped with a
log line); a sound that does not decode is skipped, never fatal.

  python3 tools/w3sound.py <map key> [--install input/war3_131]   -> lists the sounds and their sizes
"""
import inspect
import re
import struct
import sys

UI_SOUNDS = ('Sound\\Interface\\MouseClick1.wav', 'Sound\\Interface\\BigButtonClick.wav')
MAX_RATE = 22050
MAX_SECONDS = 20.0            # longer clips (speech, ambience loops) are cut: pack size [inf budget]


def script_sounds(script_text):
    """.wav paths in the script's string literals (JASS escapes \\\\ -> \\), first-seen order, case kept."""
    out, seen = [], set()
    for s in re.findall(r'"((?:[^"\\]|\\.)*\.wav)"', script_text, re.I):
        p = s.replace('\\\\', '\\')
        if p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return out


LABEL_SLKS = ('UI\\SoundInfo\\UISounds.slk', 'UI\\SoundInfo\\AbilitySounds.slk', 'UI\\SoundInfo\\AmbienceSounds.slk',
              'UI\\SoundInfo\\DialogSounds.slk', 'UI\\SoundInfo\\AnimSounds.slk')


def sound_labels(read):
    """read(name) -> bytes or None. -> {label: (file, volume)} from the install's UI\\SoundInfo tables: file =
    DirectoryBase + the first of FileNames (the game picks one of the variants [inf: the first]), volume 0..127."""
    import w3slk as slk
    out = {}
    for name in LABEL_SLKS:
        try:
            raw = read(name)
        except Exception:                      # noqa: BLE001
            raw = None
        if not raw:
            continue
        _h, rows = slk.parse(raw.decode('latin-1'))
        for r in rows:
            lab = r.get('SoundName')
            fn = str(r.get('FileNames') or '').split(',')[0].strip()
            if not isinstance(lab, str) or not fn or lab in out:
                continue
            base = str(r.get('DirectoryBase') or '')
            vol = r.get('Volume')
            out[lab] = (base + fn, int(vol) if isinstance(vol, (int, float)) else 127)
    return out


def script_labels(text):
    """labels a script names: CreateSoundFromLabel("X", ...) / SetSoundParamsFromLabel(s, "X"), first-seen order."""
    out = []
    for m in re.finditer(r'(?:CreateSoundFromLabel\(\s*|SetSoundParamsFromLabel\(\s*[^,()]+,\s*)"([^"]+)"', text):
        if m.group(1) not in out:
            out.append(m.group(1))
    return out


def wslb_section(labels, table):
    """pack section WSLB: "label\\tfile\\tvolume\\n" per label the scripts use (the world's CreateSoundFromLabel)."""
    lines = ['%s\t%s\t%d' % (l, table[l][0], table[l][1]) for l in labels if l in table]
    if not lines:
        return None
    data = ('\n'.join(lines) + '\n').encode('latin-1', 'replace')
    return (b'WSLB', data + b'\0' * ((-len(data)) % 4), len(lines))


ACK_KINDS = ('What', 'Yes', 'YesAttack')     # S4: on selection / an order / an attack order [obs UnitAckSounds.slk names]
ACK_VARIANTS = 2                             # variants per kind packed (the game picks one at random [ext]; pack size)
ACK_MAX_LINES = 48                           # pack budget [inf]: builders first, then the preplaced player units


def unit_ack(files, uids, units_placed, log=print):
    """S4: the acknowledgement voices of the unit types a player commands. files.read / files.map.read; uids = the
    map's unit types (w3pack_models.collect's set). Commanded = a builder (Builds list) or a preplaced non-building
    unit of a player slot (< 12) [inf: creeps never answer the player]. Sound set = UnitUI unitSound (map w3u 'usnd'
    overrides) [obs]; files = UI\\SoundInfo\\UnitAckSounds.slk rows <set><kind> [obs], first ACK_VARIANTS of FileNames.
    -> (wav names to pack, section b'WUAK' "type\\tkind\\tvolume\\tfile,file\\n" lines) or ([], None)."""
    import w3slk as slk
    import w3map
    import w3pack_models
    from jvm import unitdata
    rd = lambda n: files.read(n, missing_ok=True)                                         # noqa: E731
    raw = rd('Units\\UnitUI.slk')
    ui = slk.table(raw.decode('latin1'), 'unitUIID') if raw else {}
    w3u = files.map.read('war3map.w3u', missing_ok=True)
    if w3u:
        try:
            w3pack_models._apply_mods(ui, w3map.parse_objects(w3u, '.w3u'), {'usnd': 'unitSound'}, 'unitUIID')
        except Exception as e:                 # noqa: BLE001
            log('  sound: war3map.w3u unreadable for unit sounds (%s)' % e)

    def need(n):
        b = rd(n)
        if b is None:
            raise KeyError(n)
        return b
    types = unitdata.merge(unitdata.base_records(need), w3u)
    placed = set(u['id'] for u in units_placed if isinstance(u.get('player'), int) and 0 <= u['player'] < 12)
    ack_raw = rd('UI\\SoundInfo\\UnitAckSounds.slk')
    if not ack_raw:
        return [], None
    _h, rows = slk.parse(ack_raw.decode('latin-1'))
    by = {r.get('SoundName'): r for r in rows if isinstance(r.get('SoundName'), str)}
    lines, files_out = [], []

    def order(uid):
        return (0 if str((types.get(uid) or {}).get('Builds') or '').strip() else 1, uid)
    for uid in sorted(uids, key=order):
        if len(lines) >= ACK_MAX_LINES:
            break
        rec = types.get(uid) or {}
        ss = str((ui.get(uid) or {}).get('unitSound') or '').strip()
        if not ss or ss in ('-', '_'):
            continue
        bld = (ui.get(uid) or {}).get('isbldg') in (1, '1', True) or rec.get('isbldg') in (1, '1', True)
        if not (str(rec.get('Builds') or '').strip() or (uid in placed and not bld)):
            continue
        for kind in ACK_KINDS:
            r = by.get(ss + kind)
            if not r:
                continue
            base = str(r.get('DirectoryBase') or '')
            fl = [f.strip() for f in str(r.get('FileNames') or '').split(',') if f.strip()][:ACK_VARIANTS]
            if not fl:
                continue
            vol = r.get('Volume')
            paths = [base + f for f in fl]
            files_out += paths
            lines.append('%s\t%s\t%d\t%s' % (uid, kind, int(vol) if isinstance(vol, (int, float)) else 127, ','.join(paths)))
    if not lines:
        return [], None
    data = ('\n'.join(lines) + '\n').encode('latin-1', 'replace')
    log('  sound: unit voices for %d lines (%d files)' % (len(lines), len(files_out)))
    return files_out, (b'WUAK', data + b'\0' * ((-len(data)) % 4), len(lines))


COMBAT_VARIANTS = 1                          # S4b: files per combat / death label (the game picks one at random [ext]; pack size)
COMBAT_MAX_LABELS = 96
COMBAT_BUDGET_S = 90.0                       # pack budget [inf]: seconds of audio for all combat clips (~8 MB float PCM)
COMBAT_MAX_TYPES = 256
COMBAT_SECONDS = 2.0                         # longer death / impact clips are cut [inf budget]
COMBAT_SLKS = ('UI\\SoundInfo\\UnitCombatSounds.slk', 'UI\\SoundInfo\\AnimSounds.slk')


def model_sounds(mdx_bytes, lookups):
    """the SND event objects of a model per sequence kind [obs model data: event names 'SNDx' + a 4-letter
    UI\\SoundInfo\\AnimLookups.slk key, keyed at a frame inside a sequence] -> {'death': label, 'attack': label,
    'birth': label, 'work': label} (first event per kind; sequences named Death* / Attack* / Birth* / Stand Work*: the
    hammering of a builder, BUGS UNIT-ANIM)."""
    import mdx
    m = mdx.parse(mdx_bytes)
    out = {}
    for name, frames in m.events:
        if not name.upper().startswith('SND') or len(name) < 8:
            continue
        lab = lookups.get(name[4:8].upper())
        if not lab:
            continue
        for q in m.sequences:
            low = q['name'].lower()
            kind = 'death' if low.startswith('death') else ('attack' if low.startswith('attack') else
                                                             ('birth' if low.startswith('birth') else
                                                              ('work' if low.startswith('stand work') else None)))
            if kind and kind not in out and any(q['start'] <= f <= q['end'] for f in frames):
                out[kind] = lab
    return out


def unit_combat(files, uids, log=print):
    """S4b: death / weapon sounds of the map's unit types (uids + their Builds / Upgrade / Trains). Data path [obs data
    files, which-sound-when A-SN3 [inf]]:
      death  = the unit model's (UnitUI file, w3u umdl) SND event inside its Death sequence -> AnimLookups -> AnimSounds
      melee  = UnitCombatSounds row weapType1 (UnitWeapons, w3u ucs1) + the target's armor (UnitUI armor, w3u uarm)
      impact = the missile model's (Missileart, w3u ua1m) Death-sequence SND event; launch = its Birth event, else the
               unit model's Attack-sequence event
    -> (wav names, section b'WUCS' with "T\\ttype\\tdeath\\tweapType\\tarmor\\timpact\\tlaunch\\twork" + "S\\tlabel\\tvolume\\tfiles"
       lines, '-' = none) or ([], None).
    BUGS UNIT-ANIM: `work` = the unit model's SND event inside its 'Stand Work' sequence (Peasant / Peon: AnimLookups AREP ->
    'Repair' = PeonRepair1-3.wav [obs]); a harvester (abilList Ahar / Ahrl) also gets its weapType + 'Wood' melee label
    (MetalLightChopWood ...: what its axe sounds like on a tree [inf]) packed, the world plays it at each chop hit."""
    import w3slk as slk
    import w3map
    import w3pack_models
    from jvm import unitdata
    rd = lambda n: files.read(n, missing_ok=True)                                         # noqa: E731

    def tab(name, key):
        raw = rd(name)
        return slk.table(raw.decode('latin1'), key) if raw else {}
    ui = tab('Units\\UnitUI.slk', 'unitUIID')
    wp = tab('Units\\UnitWeapons.slk', 'unitWeapID')
    prof = {}
    for race in unitdata.PROFILE_FILES:
        raw = rd('Units\\%sUnitFunc.txt' % race)
        if raw:
            unitdata.parse_profile(raw.decode('latin-1'), prof)
    pr = {}
    for k, v in prof.items():
        arts = [a.strip() for a in str(v.get('Missileart') or '').strip('"').split(',')]
        pr[k] = {'Missileart1': arts[0] if arts else ''}
    w3u = files.map.read('war3map.w3u', missing_ok=True)
    if w3u:
        try:
            objs = w3map.parse_objects(w3u, '.w3u')
            w3pack_models._apply_mods(ui, objs, {'umdl': 'file', 'uarm': 'armor'}, 'unitUIID')
            w3pack_models._apply_mods(wp, objs, {'ucs1': 'weapType1'}, 'unitWeapID')
            w3pack_models._apply_mods(pr, objs, {'ua1m': 'Missileart1'}, 'id')
        except Exception as e:                 # noqa: BLE001
            log('  sound: war3map.w3u unreadable for combat sounds (%s)' % e)

    def need(n):
        b = rd(n)
        if b is None:
            raise KeyError(n)
        return b
    types = unitdata.merge(unitdata.base_records(need), w3u)
    labels = {}
    for name in COMBAT_SLKS:
        raw = rd(name)
        if not raw:
            continue
        _h, rows = slk.parse(raw.decode('latin-1'))
        for r in rows:
            lab = r.get('SoundName')
            if isinstance(lab, str) and lab not in labels:
                labels[lab] = r
    raw = rd('UI\\SoundInfo\\AnimLookups.slk')
    lookups = {}
    if raw:
        _h, rows = slk.parse(raw.decode('latin-1'))
        for r in rows:
            k, lab = r.get('AnimSoundEvent'), r.get('SoundLabel')
            if isinstance(k, str) and isinstance(lab, str):
                lookups.setdefault(k.upper(), lab)
    # the map's types + what they build / upgrade to / train; towers = reached through Builds / Upgrade
    todo = [u for u in sorted(uids) if u in types]
    seen = set(todo)
    towers = set()
    while todo:
        u = todo.pop(0)
        rec = types.get(u) or {}
        for key in ('Builds', 'Upgrade', 'Trains'):
            for a in str(rec.get(key) or '').split(','):
                a = a.strip()
                if len(a) == 4 and a in types:
                    if key != 'Trains':
                        towers.add(a)
                    if a not in seen:
                        seen.add(a)
                        todo.append(a)
    cache = {}

    def snd_of(path):
        base = w3pack_models._model_path(path)
        if not base:
            return {}
        if base not in cache:
            b = rd(base + '.mdx')
            try:
                cache[base] = model_sounds(b, lookups) if b else {}
            except Exception:                  # noqa: BLE001 — an unreadable model: no sounds
                cache[base] = {}
        return cache[base]
    trows, used = [], []
    spent = [0.0]
    def peek(n):
        if _takes_limit(files.read):
            return files.read(n, missing_ok=True, limit=HEAD_BYTES)
        return files.read(n, missing_ok=True)


    def secs(lab):
        r = labels[lab]
        fl = [f.strip() for f in str(r.get('FileNames') or '').split(',') if f.strip()][:COMBAT_VARIANTS]
        t = 0.0
        for f in fl:
            d = None
            try:                               # only the RIFF header is needed (MPQ: the first sector is decoded, not the file)
                b = peek(str(r.get('DirectoryBase') or '') + f) if f.lower().endswith('.wav') else None
                d = wav_seconds(b) if b else None
            except Exception:                  # noqa: BLE001 — unreadable sound: estimated 1 s
                d = None
            t += min(COMBAT_SECONDS, d if d is not None else 1.0)
        return t

    def use(lab):
        if lab and lab in labels and lab not in used and len(used) < COMBAT_MAX_LABELS:
            t = secs(lab)
            if spent[0] + t <= COMBAT_BUDGET_S:
                spent[0] += t
                used.append(lab)
        return lab if lab in used else '-'
    weaps, armors = set(), set()
    for u in sorted(seen):
        w = str((wp.get(u) or {}).get('weapType1') or '').strip()
        if w and w not in ('_', '-'):
            weaps.add(w)
        a = str((ui.get(u) or {}).get('armor') or '').strip()
        if a and a not in ('_', '-'):
            armors.add(a)
    # budget order [inf]: the towers' weapons (missile impact, launch, melee hit on every armor of the map), then the
    # deaths of the non-building types (the creeps), then the rest
    info = {}
    for u in sorted(seen):
        info[u] = (snd_of((ui.get(u) or {}).get('file')), snd_of((pr.get(u) or {}).get('Missileart1')))
    tw = [u for u in sorted(seen) if u in towers]
    for u in tw:
        ms, mm = info[u]
        use(mm.get('death'))
        use(mm.get('birth') or ms.get('attack'))
    for w in sorted(set(str((wp.get(u) or {}).get('weapType1') or '').strip() for u in tw) & weaps):
        for a in sorted(armors):
            use(w + a)
    bld = lambda u: bool((types.get(u) or {}).get('bldg'))                             # noqa: E731
    for u in sorted(seen):                                                              # UNIT-ANIM: harvesters' chop + work sounds
        rec = types.get(u) or {}
        if 'Ahar' in str(rec.get('abilList') or '') or 'Ahrl' in str(rec.get('abilList') or ''):
            use(info[u][0].get('work'))
            w = str((wp.get(u) or {}).get('weapType1') or '').strip()
            if w and w not in ('_', '-'):
                use(w + 'Wood')
    for u in sorted(seen):
        if not bld(u):
            use(info[u][0].get('death'))
    for w in sorted(weaps):
        for a in sorted(armors):
            use(w + a)
    for u in sorted(seen):
        ms, mm = info[u]
        use(mm.get('death'))
        use(mm.get('birth') or ms.get('attack'))
        use(ms.get('death'))
    for u in sorted(seen):
        if len(trows) >= COMBAT_MAX_TYPES:
            break
        ms, mm = info[u]
        w = str((wp.get(u) or {}).get('weapType1') or '').strip()
        a = str((ui.get(u) or {}).get('armor') or '').strip()
        row = (u, use(ms.get('death')), w if w and w not in ('_', '-') else '-', a if a and a not in ('_', '-') else '-',
               use(mm.get('death')), use(mm.get('birth') or ms.get('attack')), use(ms.get('work')))
        if row[1:] != ('-',) * 6:
            trows.append(row)
    if not trows:
        return [], None
    lines = ['T\t' + '\t'.join(r) for r in trows]
    files_out = []
    for lab in used:
        r = labels[lab]
        base = str(r.get('DirectoryBase') or '')
        fl = [f.strip() for f in str(r.get('FileNames') or '').split(',') if f.strip()][:COMBAT_VARIANTS]
        if not fl:
            continue
        vol = r.get('Volume')
        paths = [base + f for f in fl]
        files_out += paths
        lines.append('S\t%s\t%d\t%s' % (lab, int(vol) if isinstance(vol, (int, float)) else 127, ','.join(paths)))
    data = ('\n'.join(lines) + '\n').encode('latin-1', 'replace')
    log('  sound: combat sounds for %d types, %d labels (%d files)' % (len(trows), len(used), len(files_out)))
    return files_out, (b'WUCS', data + b'\0' * ((-len(data)) % 4), len(trows))


MUSIC_RACES = ('Human', 'Orc', 'NightElf', 'Undead')
MENU_MUSIC = 'Sound\\Music\\mp3Music\\Mainscreen.mp3'   # [obs UI\\WorldEditData.txt music list] the menu theme [inf]


def music_lines(read):
    """S5: INFO lines for W3Music — per race the playlist / victory / defeat tracks of UI\\war3skins.txt [obs]
    (Music_V1, the expansion's, else Music_V0), and the menu theme. The mp3 stay in the install: the companion
    serves them unchanged, the world's video player decodes them (docs/sound_plan.md S5)."""
    out = ['music_menu=' + MENU_MUSIC]
    try:
        raw = read('UI\\war3skins.txt')
    except Exception:                          # noqa: BLE001
        raw = None
    if not raw:
        return out
    sec, vals = None, {}
    for l in raw.decode('latin-1').splitlines():
        s = l.strip()
        if s.startswith('[') and s.endswith(']'):
            sec = s[1:-1]
        elif sec in MUSIC_RACES and '=' in s and not s.startswith('//'):
            k, v = s.split('=', 1)
            vals[(sec, k.strip())] = v.strip()
    for race in MUSIC_RACES:
        for key, name in (('Music', 'music'), ('VictoryMusic', 'victory'), ('DefeatMusic', 'defeat')):
            v = vals.get((race, key + '_V1')) or vals.get((race, key + '_V0'))
            if v:
                out.append('%s_%s=%s' % (name, race.lower(), v))
    return out


def wav_seconds(data):
    """RIFF header -> duration in seconds (plain PCM) or None."""
    if len(data) < 44 or data[:4] != b'RIFF' or data[8:12] != b'WAVE':
        return None
    p, fmt, n = 12, None, None
    while p + 8 <= len(data):
        cid, size = data[p:p + 4], struct.unpack_from('<I', data, p + 4)[0]
        if cid == b'fmt ' and size >= 16:
            fmt = struct.unpack_from('<HHIIHH', data, p + 8)
        elif cid == b'data':
            n = size
        p += 8 + size + (size & 1)
    if fmt is None or n is None or fmt[3] <= 0:
        return None
    return n / float(fmt[3])


def decode_wav(data, max_seconds=None):
    """RIFF PCM 8 / 16 bit, any channels -> (rate, [float mono], bits) or None (not plain PCM)."""
    if len(data) < 44 or data[:4] != b'RIFF' or data[8:12] != b'WAVE':
        return None
    p, fmt, pcm = 12, None, None
    while p + 8 <= len(data):
        cid, size = data[p:p + 4], struct.unpack_from('<I', data, p + 4)[0]
        body = data[p + 8:p + 8 + size]
        if cid == b'fmt ' and len(body) >= 16:
            fmt = struct.unpack_from('<HHIIHH', body, 0)
        elif cid == b'data':
            pcm = body
        p += 8 + size + (size & 1)
    if fmt is None or pcm is None or fmt[0] != 1 or fmt[5] not in (8, 16) or fmt[1] < 1:
        return None
    tag, ch, rate, _br, _al, bits = fmt
    if bits == 16:
        n = len(pcm) // 2
        vals = struct.unpack('<%dh' % n, pcm[:n * 2])
        scale = 1.0 / 32768.0
        vals = [v * scale for v in vals]
    else:
        vals = [(b - 128) / 128.0 for b in pcm]
    if ch > 1:
        vals = [sum(vals[i:i + ch]) / ch for i in range(0, len(vals) - ch + 1, ch)]
    while rate > MAX_RATE:
        vals = [(vals[i] + vals[i + 1]) * 0.5 for i in range(0, len(vals) - 1, 2)]
        rate //= 2
    cap = int((max_seconds or MAX_SECONDS) * rate)
    return rate, vals[:cap], bits


HEAD_BYTES = 4096                # one MPQ sector: the RIFF header of any wav


def _takes_limit(read):
    """True when `read` accepts a limit (a parameter literally named limit, or **kwargs; a second positional such as CASC.read(path, missing_ok) is not one)."""
    try:
        ps = list(inspect.signature(read).parameters.values())
    except (TypeError, ValueError):
        return False
    return any(p.name == 'limit' or p.kind == p.VAR_KEYWORD for p in ps)


def read_limited(read, name, limit):
    """read(name[, limit]) -> the first bytes of a file (at least `limit` when the file is longer; MPQ archives decode
    only the sectors needed, other sources may return the whole file) or the whole file when `read` takes no limit."""
    return read(name, limit=limit) if _takes_limit(read) else read(name)


def wav_head_need(head, seconds):
    """(bytes that hold the RIFF header and `seconds` of PCM, whole file size) from the first bytes of a plain wav, or
    None when `head` has no such header."""
    if len(head) < 44 or head[:4] != b'RIFF' or head[8:12] != b'WAVE':
        return None
    fmt, p = None, 12
    while p + 8 <= len(head):
        cid, size = head[p:p + 4], struct.unpack_from('<I', head, p + 4)[0]
        if cid == b'fmt ' and size >= 16 and p + 24 <= len(head):
            fmt = struct.unpack_from('<HHIIHH', head, p + 8)
        elif cid == b'data':
            if fmt is None or fmt[3] <= 0:
                return None
            return p + 8 + int(seconds * fmt[3]) + 4096, p + 8 + size
        p += 8 + size + (size & 1)
    return None


def wsnd_section(read, names, log=print, caps=None):
    """read(name) -> bytes or None; caps = {name: max seconds} (S4b combat clips). -> (b'WSND', bytes, count) or None when no sound decodes."""
    clips, skipped = [], []
    for n in names:
        try:
            cap = (caps or {}).get(n) or MAX_SECONDS
            raw = None
            head = read_limited(read, n, HEAD_BYTES)
            need = wav_head_need(head, cap) if head else None
            if head and need is None:
                raw = read(n)                  # no plain RIFF header in the first sector: do not trust the truncated head
            elif need is not None and len(head) < min(need):
                raw = read_limited(read, n, need[0])
            else:
                raw = head
        except Exception as e:                 # noqa: BLE001 — a sound that does not decode (stereo ambience, no Storm.dll)
            skipped.append('%s (%s)' % (n, type(e).__name__))
            continue
        dec = decode_wav(raw, (caps or {}).get(n)) if raw else None
        if dec is None:
            skipped.append(n)
            continue
        clips.append((n.lower(), dec))
    if skipped:
        log('  sound: %d skipped: %s' % (len(skipped), ', '.join(skipped[:6]) + (' ...' if len(skipped) > 6 else '')))
    if not clips:
        return None
    names_b = bytearray()
    rows, samples = [], []
    for name, (rate, vals, bits) in clips:
        nb = name.encode('ascii', 'replace')
        rows.append((rate, len(vals), len(samples), -1, len(vals), len(names_b), len(nb), bits))
        names_b += nb
        samples.extend(vals)
    names_b += b'\0' * ((-len(names_b)) % 4)
    out = struct.pack('<4i', len(clips), len(names_b), len(samples), 0)
    out += b''.join(struct.pack('<8i', *r) for r in rows) + bytes(names_b)
    out += struct.pack('<%df' % len(samples), *samples)
    log('  sound: %d clips, %.1f s, %.1f MB' % (len(clips), sum(len(v) / r for _n, (r, v, _b) in clips),
                                              len(out) / 1048576.0))
    return (b'WSND', out, len(clips))


def main():
    import argparse, os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import war3fs, w3mpq as mpq
    ap = argparse.ArgumentParser()
    ap.add_argument('map')
    ap.add_argument('--install', default='input/war3_131')
    a = ap.parse_args()
    fs = war3fs.open_install(a.install)
    hit = fs.maps().get(a.map.lower())
    arc = mpq.MPQ(fs.read_map(hit[1]), tables=fs.tables)
    script = (arc.read('war3map.j', missing_ok=True) or arc.read('scripts\\war3map.j', missing_ok=True) or b'')
    from jvm.run import strip_bom
    script = strip_bom(script)                      # JASS-BOM
    script = script.decode('latin1')
    names = list(UI_SOUNDS) + script_sounds(script)

    def read(n, limit=None):
        return arc.read(n, missing_ok=True, limit=limit) or fs.read(n, missing_ok=True, limit=limit)
    sec = wsnd_section(read, names)
    print(sec[0], sec[2], len(sec[1])) if sec else print('no sounds')


if __name__ == '__main__':
    main()
