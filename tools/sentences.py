"""sentences.txt → the GSV3 "SENT" section (tools/pack.py): the speech sentences a map can say, as word lists, plus
their word wavs (appended to the map's SNDS clips). Stdlib only. Spec: docs/goldsrc_base_layer.md §1 SENT.

Source files [obs NOTES_campaign §3.3 SENTENCEG_Init 0x11aca0; ext Sven Co-op files]: the first of
<content dir>/sound/sentences.txt or sound/default_sentences.txt (Sven Co-op 5 ships only the latter) in tools/gamedirs.py
order; Sven: a map cfg (maps/<map>.cfg) line `sentence_file <path>` (path relative to the content folder) is read after
it: same names replace the base entry in place, new names are appended [ext Sven map cfg].

Line syntax (engine VOX, [ext] NOTES_campaign §3.3): leading blanks skipped; empty lines, `/` comments and lines whose
first character is not a letter are ignored. First token = sentence name (upper-cased here). If the next token
contains '/', the part up to the last '/' is the folder ("hgrunt/", "scientist/" …), default "vox/" [ext]; the rest of
that token is a word or an option group. `(p110 t40)` standing alone sets the running values for all following words;
`word(p120)` applies to that word only. Keys: p pitch %, v volume %, s start % of the sample, e end %, t time
compression % (defaults 100, 100, 0, 100, 0). A ',' / '.' after a word (or alone) adds the word "_comma" / "_period"
(each voice folder has _comma.wav / _period.wav). Word wav = <folder><word>.wav, the word keeps its '!'
("hgrunt/grenade!.wav"). A duplicate name keeps the first line (logged).
Groups [obs USENTENCEG / SENTENCEG_PlayRndSz, NOTES §3.3]: the name with its trailing digits stripped (SC_HELLO3 →
SC_HELLO); consecutive lines with the same stem form one group; names without a trailing digit form none.

Which sentences (wanted, by priority; the budget is spent in this order, sentence by sentence):
  0  scripted_sentence `sentence` ("!NAME" = that sentence, else a group = all its members) [obs §3.2 KeyValue
     0x112ee0]; ambient_generic `message` "!NAME" [obs §3.8];
  1  monster keys UseSentence / UnUseSentence [obs CTalkMonster::KeyValue 0x125860];
  2  follow / answer talk groups of the monster classes on the map (monster_* entities, monstermaker / squadmaker
     `monstertype`): talkers <P>_OK / _WAIT / _STOP / _POK / _HELLO / _SCARED, Barney (and Otis) _SHOT / _MAD /
     _KILL / _ATTACK [obs §2.1 TalkInit, §2.3]; grunts <P>_ALERT / _MONST / _GREN / _COVER [obs §2.8 pGruntSentences];
  3  chatter: talkers _IDLE / _QUESTION / _ANSWER / _STARE / _SMELL / _WOUND / _MORTAL / _FEAR / _PLFEAR / _SCREAM /
     _HEAL / _PHELLO / _PIDLE / _PQUEST, !<P>_CUREA/B/C; grunts _THROW / _CHARGE / _TAUNT / _CHECK / _QUEST / _IDLE /
     _CLEAR / _ANSWER [obs §2.1, §2.8 IdleSound 0xc59a0].
  Class prefixes: scientist / sitting / cleansuit scientist SC, barney BA, otis OT, human_grunt HG, Sven grunt allies
  (human_grunt_ally / medic / torch) FG [inf]. A sentence is packed when its new word wavs fit the remaining budget
  (--sentence-mb, float32 after halving rates above 22050 Hz like sounds.py); words already in SNDS cost nothing; a
  missing wav gives clip −1 (the sentence stays if any word is present). cstrike: never (CS packs stay unchanged).

SENT layout (count = N sentences, little endian, 4-byte aligned):
  int32 N, int32 W (word records), int32 G (groups), int32 textBytes (padded)
  N × 4 int32: nameOff, nameLen, firstWord, wordCount        (included sentences in file order)
  W × 8 int32: clip (index into the pack's SNDS clips, −1 = missing), textOff, textLen (word without folder:
               "hellothere", "grenade!", "_comma"), pitch, volume, start, end, time (percent)
  G × 4 int32: nameOff, nameLen (stem, e.g. "SC_HELLO"), firstSentence, count   (over the included sentences)
  text blob: ASCII ('?' for other bytes), names upper case, strings deduplicated, padded with NUL to 4
"""
import os, re, struct

DEFAULTS = (100, 100, 0, 100, 0)          # pitch, volume, start, end, time
KEYS = "pvset"
TALK_PREFIX = {
    "monster_scientist": "SC", "monster_sitting_scientist": "SC", "monster_cleansuit_scientist": "SC",
    "monster_barney": "BA", "monster_otis": "OT", "monster_human_grunt": "HG",
    "monster_drillsergeant": "DR",
    "monster_human_grunt_ally": "FG", "monster_human_medic_ally": "FG", "monster_human_torch_ally": "FG",
    # the OF repel points drop an ally / medic / torch (CHGruntAllyRepel, COFMedicAllyRepel, COFTorchAllyRepel::RepelUse)
    "monster_grunt_ally_repel": "FG", "monster_medic_ally_repel": "FG", "monster_torch_ally_repel": "FG",
}
MEDIC_CLASSES = ("monster_human_medic_ally", "monster_medic_ally_repel")
GRUNTS = ("HG", "FG")
SLAVE_CLASSES = ("monster_alien_slave",)     # CISlave::AlertSound / IdleSound: SLV_ALERT (pri 2), SLV_IDLE (pri 3)
TALK2 = ("OK", "WAIT", "STOP", "POK", "HELLO", "SCARED")
BARNEY2 = ("SHOT", "MAD", "KILL", "ATTACK")
GRUNT2 = ("ALERT", "MONST", "GREN", "COVER")
TALK3 = ("IDLE", "QUESTION", "ANSWER", "STARE", "SMELL", "WOUND", "MORTAL", "FEAR", "PLFEAR", "SCREAM", "HEAL",
         "PHELLO", "PIDLE", "PQUEST")
GRUNT3 = ("THROW", "CHARGE", "TAUNT", "CHECK", "QUEST", "IDLE", "CLEAR", "ANSWER")
# The OF allies (COFAllyMonster hierarchy: grunt ally, medic, torch) also speak the talk-monster follow / chatter groups of their
# TalkInit slots [obs CHGruntAlly::TalkInit m_szGrp: FG_ANSWER/QUESTION/IDLE/STARE/OK/WAIT/STOP/SCARED/HELLO/PQUEST/SMELL/WOUND/MORTAL,
# !FG_CUREA-C] and the Barney-style combat reactions (FG_SHOT / MAD / KILL / ATTACK, CTalkMonster::TakeDamage and the dead-enemy line).
# Groups that sentences.txt does not define (FG_STARE, FG_SMELL, FG_FEAR ...) simply select nothing. The medic's own heal lines are the
# literal sentences MG_HEAL / MG_NOTHEAL [obs COFMedicAlly::HealerUse]; its pMedicSentences table (pending index 3 / 5) is the FG_ table
# of the grunt ally (FG_GREN / ALERT / MONSTER / COVER / THROW / CHARGE / TAUNT, read from opfor.so .data).
ALLY2 = ("OK", "WAIT", "STOP", "POK", "HELLO", "SCARED", "SHOT", "MAD", "KILL", "ATTACK")
ALLY3 = ("QUESTION", "STARE", "SMELL", "WOUND", "MORTAL", "PQUEST", "CURE")
MEDIC_LINES = ("MG_HEAL", "MG_NOTHEAL")


def stem(name):
    """Group stem: trailing digits stripped; '' when the name has none (no group)."""
    s = name.rstrip("0123456789")
    return s if s != name and s else ""


def _tokens(rest):
    """Whitespace tokens, a '(…)' group kept in one token even when it holds blanks."""
    out, cur, depth = [], "", 0
    for ch in rest:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch in " \t" and depth == 0:
            if cur:
                out.append(cur)
            cur = ""
            continue
        cur += ch
    if cur:
        out.append(cur)
    return out


def _opts(s, base):
    v = list(base)
    for k, n in re.findall(r"([pvsetPVSET])\s*(-?\d+)", s):
        v[KEYS.index(k.lower())] = int(n)
    return tuple(v)


def _word(text, folder, vals):
    p, v, s, e, t = vals
    return dict(text=text, folder=folder, pitch=p, volume=v, start=s, end=e, time=t)


def parse_line(line):
    """→ (NAME, words) or None for comments / blank / non-sentence lines."""
    ln = line.lstrip(" \t")
    if not ln or ln[0] == "/" or not ln[0].isalpha():
        return None
    parts = ln.split(None, 1)
    name = parts[0].upper()
    toks = _tokens(parts[1]) if len(parts) > 1 else []
    folder = "vox/"
    if toks and "/" in toks[0]:
        i = toks[0].rfind("/")
        folder = toks[0][:i + 1].lower()
        toks[0] = toks[0][i + 1:]
    run = DEFAULTS
    words = []
    for tok in toks:
        m = re.match(r"^([^(]*)(?:\(([^)]*)\)?)?(.*)$", tok)
        w, o, tail = m.group(1), m.group(2), m.group(3)
        lead = w[:len(w) - len(w.lstrip(",."))]
        w = w[len(lead):]
        for ch in lead:
            words.append(_word("_comma" if ch == "," else "_period", folder, run))
        pause = ""
        core = w.rstrip(",.")
        pause += w[len(core):]
        pause += "".join(ch for ch in tail if ch in ",.")
        if not core:
            if o is not None:
                run = _opts(o, run)            # standing alone: running values
        else:
            words.append(_word(core, folder, _opts(o, run) if o is not None else run))
        for ch in pause:
            words.append(_word("_comma" if ch == "," else "_period", folder, run))
    return name, words


def parse(text, log=None):
    """→ ordered list of {name, words}; a duplicate name keeps its first line."""
    out, seen, dups = [], set(), []
    for line in text.replace("\r", "").split("\n"):
        r = parse_line(line)
        if r is None:
            continue
        if r[0] in seen:
            dups.append(r[0])
            continue
        seen.add(r[0])
        out.append(dict(name=r[0], words=r[1]))
    if dups and log:
        log(f"sentences: {len(dups)} duplicate names kept at their first line ({', '.join(dups[:6])})")
    return out


def overlay(base, extra):
    """Sven sentence_file: same names replace the base entry in place, new ones are appended."""
    idx = {s["name"]: i for i, s in enumerate(base)}
    out = list(base)
    for s in extra:
        if s["name"] in idx:
            out[idx[s["name"]]] = s
        else:
            idx[s["name"]] = len(out)
            out.append(s)
    return out


def groups(sents):
    """[(stem, first, count)] of consecutive same-stem sentences (names ending in a digit)."""
    out = []
    for i, s in enumerate(sents):
        st = stem(s["name"])
        if not st:
            continue
        if out and out[-1][0] == st and out[-1][1] + out[-1][2] == i:
            out[-1] = (st, out[-1][1], out[-1][2] + 1)
        else:
            out.append((st, i, 1))
    return out


def wanted(ents):
    """[(key, priority)]: key "!NAME" = one sentence, else a group stem; first use order within a priority."""
    out = {}

    def add(k, pri):
        k = (k or "").strip().upper()
        if not k or k == "!":
            return
        if k not in out or pri < out[k]:
            out[k] = pri
    classes = []
    for e in ents:
        c = e.get("classname", "")
        if c == "scripted_sentence":
            add(e.get("sentence", ""), 0)
        elif c == "ambient_generic" and (e.get("message") or "").startswith("!"):
            add(e["message"], 0)
        if c.startswith("monster_"):
            for k in ("UseSentence", "UnUseSentence"):
                v = e.get(k) or e.get(k.lower())
                if v:
                    add(v if v.startswith("!") else v, 1)
            classes.append(c)
        if c in ("monstermaker", "squadmaker") and e.get("monstertype"):
            classes.append(e["monstertype"].strip().lower())
    prefixes = []
    if any(c in SLAVE_CLASSES for c in classes):
        add("SLV_ALERT", 2)
        add("SLV_IDLE", 3)
    for c in classes:
        p = TALK_PREFIX.get(c)
        if p and p not in prefixes:
            prefixes.append(p)
    for p in prefixes:
        if p in GRUNTS:
            for g in GRUNT2:
                add(f"{p}_{g}", 2)
            if p == "FG":
                for g in ALLY2:
                    add(f"{p}_{g}", 2)
        else:
            for g in TALK2:
                add(f"{p}_{g}", 2)
            if p in ("BA", "OT", "DR"):               # CDrillSergeant::TalkInit shares Barney's SHOT / MAD / KILL / ATTACK
                for g in BARNEY2:
                    add(f"{p}_{g}", 2)
    if any(c in MEDIC_CLASSES for c in classes):
        for g in MEDIC_LINES:
            add(g, 2)
    for p in prefixes:
        if p in GRUNTS:
            for g in GRUNT3:
                add(f"{p}_{g}", 3)
            if p == "FG":
                for g in ALLY3:
                    add(f"{p}_{g}", 3)
        else:
            for g in TALK3:
                add(f"{p}_{g}", 3)
            for g in ("A", "B", "C"):
                add(f"!{p}_CURE{g}", 3)
    return sorted(out.items(), key=lambda kv: kv[1])


def _find_rel(game_root, mod, rel):
    """<content dir>/<rel> (case-insensitive per path part) in gamedirs order, else None."""
    from gamedirs import content_dirs
    parts = [p for p in rel.replace("\\", "/").split("/") if p]
    for root in content_dirs(game_root, mod):
        d = root
        for part in parts:
            if not os.path.isdir(d):
                d = None
                break
            hit = [f for f in os.listdir(d) if f.lower() == part.lower()]
            if not hit:
                d = None
                break
            d = os.path.join(d, hit[0])
        if d and os.path.isfile(d):
            return d
    return None


def _read(p):
    with open(p, "rb") as fh:
        return fh.read().decode("latin-1")


def find_files(game_root, mod, map_name=None):
    """→ (base path or None, Sven sentence_file path or None)."""
    from gamedirs import content_dirs
    base = None
    for root in content_dirs(game_root, mod):
        for n in ("sentences.txt", "default_sentences.txt"):
            p = os.path.join(root, "sound", n)
            if os.path.isfile(p):
                base = p
                break
        if base:
            break
    extra = None
    if map_name:
        cfg = _find_rel(game_root, mod, f"maps/{map_name}.cfg")
        if cfg:
            for line in _read(cfg).replace("\r", "").split("\n"):
                w = line.strip().split(None, 1)
                if len(w) == 2 and w[0].lower() == "sentence_file":
                    extra = _find_rel(game_root, mod, w[1].strip().strip('"'))
    return base, extra


def wav_name(w):
    from sounds import norm
    return norm(w["folder"] + w["text"] + ".wav")


def select(sents, want):
    """Resolve wanted keys → [(sentence index, priority)] in budget order (each sentence once)."""
    by_name = {s["name"]: i for i, s in enumerate(sents)}
    by_stem = {}
    for i, s in enumerate(sents):
        st = stem(s["name"])
        if st:
            by_stem.setdefault(st, []).append(i)
    order, seen = [], set()
    for k, pri in want:
        idx = [by_name[k[1:]]] if k.startswith("!") and k[1:] in by_name else (
            by_stem.get(k, []) if not k.startswith("!") else [])
        if not idx and not k.startswith("!") and k in by_name:
            idx = [by_name[k]]
        for i in idx:
            if i not in seen:
                seen.add(i)
                order.append((i, pri))
    return order


def load(ents, game_root, mod, map_name=None, existing_clip_names=(), max_bytes=8 * 1024 * 1024, log=print):
    """→ (sentences for section(), extra clips in the sounds.py tuple format, to append after the entity clips)."""
    import sounds
    if not game_root or mod == "cstrike":
        return [], []
    base, extra = find_files(game_root, mod, map_name)
    if not base:
        return [], []
    sents = parse(_read(base), log)
    if extra:
        sents = overlay(sents, parse(_read(extra), log))
        log(f"sentences: map cfg sentence_file {os.path.basename(extra)}")
    want = wanted(ents)
    if not want:
        return [], []
    order = select(sents, want)
    if not order:
        return [], []
    idx = {n: i for i, n in enumerate(existing_clip_names)}
    nxt = len(existing_clip_names)
    cache = {}
    clips, used = [], 0
    chosen = {}
    over, empty, missing = 0, 0, []

    def wav(n):
        if n not in cache:
            p = sounds.find(game_root, mod, n)
            r = None
            if p:
                try:
                    with open(p, "rb") as fh:
                        rate, vals, ls, le, bits = sounds.read_wav(fh.read())
                    while rate > sounds.MAX_RATE:
                        vals = sounds.halve(vals)
                        rate //= 2
                    r = (n, rate, vals, -1, len(vals), bits)
                except (ValueError, struct.error, OSError) as e:
                    log(f"sentence word {n}: unreadable ({e})")
            elif n not in missing:
                missing.append(n)
            cache[n] = r
        return cache[n]
    for i, _pri in order:
        s = sents[i]
        names = [wav_name(w) for w in s["words"]]
        new = []
        cost = 0
        present = 0
        for n in names:
            if n in idx:
                present += 1
                continue
            c = wav(n)
            if c is None:
                continue
            present += 1
            if n not in [x[0] for x in new]:
                new.append(c)
                cost += 4 * len(c[2])
        if present == 0:
            empty += 1
            continue
        if used + cost > max_bytes:
            over += 1
            continue
        used += cost
        for c in new:
            idx[c[0]] = nxt
            nxt += 1
            clips.append(c)
        chosen[i] = [(idx.get(n, -1), w) for n, w in zip(names, s["words"])]
    out = []
    for i in sorted(chosen):
        out.append(dict(name=sents[i]["name"],
                        words=[(c, w["text"], w["pitch"], w["volume"], w["start"], w["end"], w["time"]) for c, w in chosen[i]]))
    nw = sum(len(s["words"]) for s in out)
    log(f"sentences: {len(out)} of {len(order)} wanted ({len(groups(out))} groups), {nw} words, {len(clips)} clips, "
        f"{used / 1e6:.2f} MB" + (f", {over} over the {max_bytes / 1e6:.0f} MB budget" if over else "")
        + (f", {empty} without any wav" if empty else "")
        + (f", {len(missing)} words missing ({', '.join(missing[:6])}{' …' if len(missing) > 6 else ''})" if missing else ""))
    return out, clips


def section(sents):
    """SENT blob (see module doc)."""
    blob = bytearray()
    pos = {}

    def text(s):
        b = s.encode("ascii", "replace")
        if b not in pos:
            pos[b] = len(blob)
            blob.extend(b)
        return pos[b], len(b)
    recs, words = b"", b""
    nw = 0
    for s in sents:
        o, n = text(s["name"].upper())
        recs += struct.pack("<4i", o, n, nw, len(s["words"]))
        for clip, t, p, v, st, en, tc in s["words"]:
            wo, wl = text(t)
            words += struct.pack("<8i", clip, wo, wl, p, v, st, en, tc)
            nw += 1
    gs = b""
    gl = groups(sents)
    for st, first, cnt in gl:
        o, n = text(st)
        gs += struct.pack("<4i", o, n, first, cnt)
    blob.extend(b"\0" * (-len(blob) % 4))
    head = struct.pack("<4i", len(sents), nw, len(gl), len(blob))
    return head + recs + words + gs + bytes(blob)
