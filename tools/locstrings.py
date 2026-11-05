"""Localised strings for JASS GetLocalizedString (BUGS GAMEOVER-DIALOG).

The game resolves GetLocalizedString("KEY") through the FDF string tables of the player's install (in the MPQ
archives these are the UI\\FrameDef\\*Strings.fdf files of war3.mpq / War3x.mpq / War3xlocal.mpq; a 1.31 CASC
install serves the same UI\\FrameDef\\... paths from its locale mod). The reference VM returned the key itself, so
Blizzard.j's dialog buttons showed GAMEOVER_RESTART instead of "Restart Game".

At conversion time this module reads every FDF string list of the install and keeps the keys the scripts use
(literal GetLocalizedString arguments in common.j / Blizzard.j / war3map.j) plus every GAMEOVER_* key. A missing
key stays the key (the caller writes no pair, so the VM's fallback is the old behaviour). The converter stores the
pairs in the pack section JLOC (tools/w3pack_jvm.py); W3Vm.GetLocalizedString reads them (W3Natives.cs).
"""
import re

import w3fdf as fdf

# The two stock string tables (w3glue reads the same files for the menu). More tables are found by listing the
# install's UI\\FrameDef tree below.
SOURCES = ('UI\\FrameDef\\GlobalStrings.fdf', 'UI\\FrameDef\\NetworkStrings.fdf')
EXTRA_PREFIXES = ('GAMEOVER_',)                 # dialog buttons Blizzard.j shows for every defeat / victory
_KEY = re.compile(r'GetLocalizedString\s*\(\s*"((?:[^"\\]|\\.)*)"\s*\)', re.I)


def _decode(raw):
    """FDF string tables are UTF-8; a legacy byte falls back to Latin-1."""
    if raw is None:
        return ''
    if isinstance(raw, str):
        return raw
    try:
        return raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        return raw.decode('latin-1')


def parse_strings(raw):
    """an FDF file's StringList -> {key: value}."""
    return fdf.parse(_decode(raw))['strings']


def _strip_comments(text):
    """JASS // comments out (quotes stay intact), so a commented-out call adds no key."""
    out = []
    for line in text.splitlines():
        q = False
        i = 0
        while i < len(line):
            c = line[i]
            if c == '"':
                q = not q
            elif not q and line.startswith('//', i):
                break
            out.append(c)
            i += 1
        out.append('\n')
    return ''.join(out)


def script_keys(texts):
    """literal keys of GetLocalizedString calls, in script order, without duplicates."""
    keys = []
    seen = set()
    for text in texts:
        for m in _KEY.finditer(_strip_comments(text or '')):
            k = m.group(1)
            if k not in seen:
                seen.add(k)
                keys.append(k)
    return keys


def string_files(core=None):
    """the FDF files to read: the stock tables first, then every UI\\FrameDef\\*.fdf the install lists."""
    names = list(SOURCES)
    extra = []
    try:
        if core is not None and hasattr(core, 'chain'):                   # MPQ install (tools/war3fs.MpqInstall)
            for a in core.chain.archives:
                for n, _i, _info in a.listing():
                    if n:
                        extra.append(n)
        elif core is not None and hasattr(core, 'c'):                     # CASC install
            extra = list(core.c.names())
    except Exception:                                                     # noqa: BLE001 - a broken listing only skips extras
        extra = []
    for n in extra:
        low = n.lower()
        if low.startswith('war3.w3mod:'):
            low = low[len('war3.w3mod:'):]
        if low.startswith('ui\\framedef\\') and low.endswith('.fdf'):
            names.append(n)
    out = []
    seen = set()
    for n in names:
        k = n.lower()
        if k not in seen:
            seen.add(k)
            out.append(n)
    return out


def _read(core, name):
    if core is None:
        return None
    if getattr(core, 'kind', '') == 'casc' and ':' in name and hasattr(core, 'c'):
        return core.c.read(name, missing_ok=True)
    return core.read(name, missing_ok=True)


def collect_strings(core=None, names=None):
    """every StringList key of the install's FDF string tables -> {key: value} (first definition wins)."""
    out = {}
    for n in (names if names is not None else string_files(core)):
        raw = _read(core, n)
        if not raw:
            continue
        try:
            strings = parse_strings(raw)
        except Exception:                                                 # noqa: BLE001 - one odd FDF is not fatal
            continue
        for k, v in strings.items():
            if k not in out:
                out[k] = str(v)
    return out


def pair_table(strings, scripts=(), extra_prefixes=EXTRA_PREFIXES):
    """-> ([(key, value)], [missing key]). Keys = literal script calls + all GAMEOVER_* entries; a missing key is
    left out (the VM returns the key, the fallback of GetLocalizedString)."""
    keys = script_keys(scripts)
    seen = set(keys)
    for k in sorted(strings):
        if any(k.startswith(p) for p in extra_prefixes) and k not in seen:
            seen.add(k)
            keys.append(k)
    pairs = [(k, strings[k]) for k in keys if k in strings]
    missing = [k for k in keys if k not in strings]
    return pairs, missing


def table(core, scripts=(), names=None, extra_prefixes=EXTRA_PREFIXES):
    """the install's FDF strings -> the JLOC pairs the scripts need (and the keys no table has)."""
    return pair_table(collect_strings(core, names), scripts, extra_prefixes)
