"""One string catalog for the whole companion UI (English / Deutsch / Japanese) + language detection + UI font choice.

tr("key", name=value) -> text in the current language (fallback: English, then the key itself).
Catalog: i18n_catalog.CATALOG = {key: (en, de, ja)} - every user-visible string of gui.py / firstrun*.py lives there.
Language: gui.json "lang" ("auto" or en/de/ja); "auto" = the Windows UI language (GetUserDefaultUILanguage), else $LC_ALL/$LANG.
Stdlib only. Nothing here touches the network.
"""
import os
import sys

from i18n_catalog import CATALOG
from i18n_catalog_worlds import CATALOG_WORLDS

CATALOG = dict(CATALOG, **CATALOG_WORLDS)

LANGS = ("en", "de", "ja")
LANG_NAMES = {"en": "English", "de": "Deutsch", "ja": "日本語"}          # shown in the selector, always in their own language
_IDX = {l: i for i, l in enumerate(LANGS)}
_current = "en"                                                          # tests / headless code default to English

# Windows primary language ids (LANGID & 0x3ff)
_WIN_PRIMARY = {0x09: "en", 0x07: "de", 0x11: "ja"}


def normalize(code, default="en"):
    """'ja_JP.UTF-8' / 'ja-JP' / 'JA' -> 'ja'; unknown -> default"""
    c = str(code or "").strip().lower().replace("_", "-")
    c = c.split(".")[0].split("@")[0].split("-")[0]
    return c if c in LANGS else default


def detect_system_lang(windows_langid=None, env=None, default="en"):
    """The OS UI language. windows_langid: injected for tests (else ctypes on Windows); env: injected environment."""
    lid = windows_langid
    if lid is None and sys.platform == "win32":
        try:
            import ctypes
            lid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        except (OSError, AttributeError):
            lid = None
    if lid:
        return _WIN_PRIMARY.get(int(lid) & 0x3FF, default)
    e = os.environ if env is None else env
    for k in ("LC_ALL", "LC_MESSAGES", "LANGUAGE", "LANG"):
        v = (e.get(k) or "").split(":")[0]
        if v and v not in ("C", "POSIX"):
            return normalize(v, default)
    return default


def resolve(setting, **kw):
    """gui.json value ('auto' / '' / en / de / ja) -> a concrete language"""
    s = str(setting or "auto").strip().lower()
    return detect_system_lang(**kw) if s in ("", "auto") else normalize(s)


def set_lang(code):
    global _current
    _current = normalize(code)
    return _current


def lang():
    return _current


def tr(key, **kw):
    ent = CATALOG.get(key)
    if ent is None:
        return key
    s = ent[_IDX[_current]] or ent[0]
    if kw:
        try:
            return s.format(**kw)
        except (KeyError, IndexError, ValueError):
            return s
    return s


def tr_in(code, key, **kw):
    """tr() in a given language without changing the current one (tests)"""
    global _current
    old = _current
    _current = normalize(code)
    try:
        return tr(key, **kw)
    finally:
        _current = old


# ---- fonts: Segoe UI has no Japanese glyphs; pick an installed CJK-capable UI font when the language needs it ----
JA_FONTS = ("Yu Gothic UI", "Meiryo UI", "Meiryo", "MS UI Gothic", "Noto Sans CJK JP", "Hiragino Sans", "Hiragino Kaku Gothic ProN")
LATIN_FONTS = ("Segoe UI", "Helvetica Neue", "DejaVu Sans")


def pick_font(families, code=None):
    """-> font family for the language: the first installed of JA_FONTS for ja, else the first installed LATIN font ("" = Tk default)"""
    have = {f.lower(): f for f in families or ()}
    order = (list(JA_FONTS) if normalize(code or _current) == "ja" else []) + list(LATIN_FONTS)
    for name in order:
        if name.lower() in have:
            return have[name.lower()]
    return ""


def format_eta(eta_s):
    """seconds left (-1 / None = unknown) -> '' / '42 s left' / 'about 3 min left' (current language)"""
    if eta_s is None or eta_s < 0:
        return ""
    if eta_s < 60:
        return tr("eta.s", n=max(1, int(eta_s)))
    return tr("eta.min", n=int((eta_s + 30) // 60))
