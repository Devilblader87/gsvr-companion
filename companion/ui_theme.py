"""One dark design system for both views of the GSVR companion window (Tk only, no images, no new dependency).

The simple view (firstrun_ui.py) draws with plain tk widgets + Canvas in these colours; the Advanced view (gui.py, ttk tabs) gets
the same palette through a ttk.Style on the 'clam' theme (the native vista/aqua themes ignore colours). Sizes go through px() so DPI
scaling stays sane; the font family is looked up ONCE per process and language (tkfont.families() costs 50-200 ms on Windows with many
fonts installed - calling it on every redraw made the window lag while it was dragged).
Text colours keep >= 4.5:1 contrast on BG and PANEL (test_simple_view.py / test_ui_theme.py).
"""
import i18n

# dark palette (same tokens as the redesign mock)
COL = {"bg": "#121821", "panel": "#18202c", "line": "#273244", "text": "#e9eef5", "muted": "#9dabbd", "accent": "#4fd1e0",
       "ok": "#3ecf7a", "warn": "#f0b429", "bad": "#ff7070", "idle": "#6f7d90", "ink": "#04200f", "off": "#273244", "how": "#141b25",
       "hover": "#263246", "select": "#24455a", "btn": "#1e2836", "btn_line": "#3a4a62"}
# traffic lights on the dark background (Worlds tab, headline lamp)
LIGHTS = {"green": COL["ok"], "yellow": COL["warn"], "red": COL["bad"], "grey": COL["idle"], "orange": COL["warn"]}
MONO = ("Consolas", "Menlo", "DejaVu Sans Mono", "Courier New")

_families = {}          # id(tk interpreter) -> tuple of installed font families
_mono = {}


def families(root):
    """installed font families, cached per Tk interpreter (the expensive call)"""
    key = str(root.tk)
    fams = _families.get(key)
    if fams is None:
        from tkinter import font as tkfont
        try:
            fams = tuple(tkfont.families(root))
        except Exception:                                      # noqa: BLE001
            fams = ()
        _families[key] = fams
    return fams


def family(root, lang=None):
    """UI font for the language (JA needs a font with Japanese glyphs; Segoe UI has none); cached via families()"""
    fam = i18n.pick_font(families(root), lang or i18n.lang())
    if fam:
        return fam
    try:
        from tkinter import font as tkfont
        return tkfont.nametofont("TkDefaultFont").actual("family")
    except Exception:                                          # noqa: BLE001
        return "TkDefaultFont"


def mono(root):
    key = str(root.tk)
    if key not in _mono:
        have = {f.lower(): f for f in families(root)}
        _mono[key] = next((have[m.lower()] for m in MONO if m.lower() in have), "TkFixedFont")
    return _mono[key]


def scale(root):
    """1.0 at 96 dpi (Windows 100 %); Tk's scaling is in pixels per point"""
    try:
        return max(0.75, float(root.tk.call("tk", "scaling")) / (96.0 / 72.0))
    except Exception:                                          # noqa: BLE001
        return 1.0


def px(root, n):
    return max(1, int(round(n * scale(root))))


def _lum(hx):
    hx = hx.lstrip("#")
    out = []
    for i in (0, 2, 4):
        c = int(hx[i:i + 2], 16) / 255.0
        out.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]


def contrast(a, b):
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


# (foreground, background) pairs the Advanced view draws text with; all must be >= 4.5:1
TEXT_PAIRS = (("text", "bg"), ("text", "panel"), ("muted", "bg"), ("muted", "panel"), ("text", "how"), ("bad", "bg"), ("bad", "panel"),
              ("ok", "bg"), ("warn", "bg"), ("accent", "bg"), ("ink", "ok"), ("text", "select"), ("text", "hover"), ("muted", "how"),
              ("text", "btn"), ("bad", "btn"))


def apply_ttk(root, fam=None):
    """style every ttk widget class the Advanced view uses (and the help dialog) with the dark palette -> ttk.Style"""
    from tkinter import ttk
    C = COL
    fam = fam or family(root)
    p = lambda n: px(root, n)                                  # noqa: E731
    base = (fam, 10)
    st = ttk.Style(root)
    try:
        st.theme_use("clam")                                   # the only built-in theme that honours every colour on all platforms
    except Exception:                                          # noqa: BLE001
        pass
    st.configure(".", background=C["bg"], foreground=C["text"], fieldbackground=C["how"], bordercolor=C["line"],
                 darkcolor=C["bg"], lightcolor=C["bg"], troughcolor=C["bg"], focuscolor=C["accent"], selectbackground=C["select"],
                 selectforeground=C["text"], insertcolor=C["text"], font=base, relief="flat")
    st.map(".", foreground=[("disabled", C["idle"])])
    st.configure("TFrame", background=C["bg"])
    st.configure("Panel.TFrame", background=C["panel"])
    st.configure("TLabel", background=C["bg"], foreground=C["text"])
    st.configure("Muted.TLabel", foreground=C["muted"])
    st.configure("Bad.TLabel", foreground=C["bad"])
    st.configure("Head.TLabel", font=(fam, 14, "bold"))
    st.configure("Bold.TLabel", font=(fam, 10, "bold"))
    st.configure("Name.TLabel", font=(fam, 11, "bold"))
    st.configure("Panel.TLabel", background=C["panel"])
    st.configure("PanelMuted.TLabel", background=C["panel"], foreground=C["muted"])
    st.configure("PanelBad.TLabel", background=C["panel"], foreground=C["bad"])
    st.configure("PanelName.TLabel", background=C["panel"], foreground=C["text"], font=(fam, 11, "bold"))
    # buttons: flat, rounded look comes from the thin border in the line colour; hover = accent border
    st.configure("TButton", background=C["btn"], foreground=C["text"], bordercolor=C["btn_line"], lightcolor=C["btn"],
                 darkcolor=C["btn"], padding=(p(12), p(5)), relief="flat", focusthickness=1, anchor="center")
    st.map("TButton", background=[("disabled", C["bg"]), ("pressed", C["line"]), ("active", C["hover"])],
           bordercolor=[("disabled", C["line"]), ("focus", C["accent"]), ("active", C["accent"])],
           lightcolor=[("pressed", C["line"]), ("active", C["hover"])], darkcolor=[("pressed", C["line"]), ("active", C["hover"])],
           foreground=[("disabled", C["idle"])])
    for name, fill, ink in (("Primary.TButton", C["ok"], C["ink"]), ("Stop.TButton", C["btn"], C["bad"])):
        st.configure(name, background=fill, foreground=ink, lightcolor=fill, darkcolor=fill, bordercolor=fill if name[0] == "P" else C["bad"],
                     font=(fam, 13, "bold"), padding=(p(18), p(7)))
        st.map(name, background=[("disabled", C["off"]), ("pressed", fill), ("active", fill)],
               foreground=[("disabled", C["muted"])], bordercolor=[("focus", C["accent"]), ("active", C["accent"])],
               lightcolor=[("active", fill)], darkcolor=[("active", fill)])
    st.configure("Link.TButton", background=C["bg"], foreground=C["accent"], bordercolor=C["line"], lightcolor=C["bg"], darkcolor=C["bg"])
    st.map("Link.TButton", background=[("active", C["hover"])], bordercolor=[("active", C["accent"]), ("focus", C["accent"])])
    st.configure("TCheckbutton", background=C["bg"], foreground=C["text"], indicatorbackground=C["how"], indicatorforeground=C["accent"],
                 indicatorcolor=C["how"], upperbordercolor=C["line"], lowerbordercolor=C["line"], padding=p(2))
    st.map("TCheckbutton", background=[("active", C["bg"])], indicatorcolor=[("pressed", C["line"])],
           indicatorbackground=[("pressed", C["line"])], upperbordercolor=[("selected", C["accent"]), ("active", C["accent"])],
           lowerbordercolor=[("selected", C["accent"]), ("active", C["accent"])], foreground=[("disabled", C["idle"])])
    st.configure("TEntry", fieldbackground=C["how"], foreground=C["text"], insertcolor=C["text"], bordercolor=C["line"],
                 lightcolor=C["how"], darkcolor=C["how"], padding=p(4))
    st.map("TEntry", bordercolor=[("focus", C["accent"])], lightcolor=[("focus", C["accent"])])
    st.configure("TCombobox", fieldbackground=C["how"], background=C["panel"], foreground=C["text"], arrowcolor=C["text"],
                 bordercolor=C["line"], lightcolor=C["how"], darkcolor=C["how"], padding=p(3))
    st.map("TCombobox", fieldbackground=[("readonly", C["how"])], foreground=[("readonly", C["text"])],
           selectbackground=[("readonly", C["how"])], selectforeground=[("readonly", C["text"])],
           background=[("active", C["hover"])], bordercolor=[("focus", C["accent"]), ("active", C["accent"])])
    root.option_add("*TCombobox*Listbox.background", C["panel"])
    root.option_add("*TCombobox*Listbox.foreground", C["text"])
    root.option_add("*TCombobox*Listbox.selectBackground", C["select"])
    root.option_add("*TCombobox*Listbox.selectForeground", C["text"])
    root.option_add("*TCombobox*Listbox.font", base)
    st.configure("TNotebook", background=C["bg"], bordercolor=C["line"], lightcolor=C["bg"], darkcolor=C["bg"],
                 tabmargins=(0, p(4), 0, 0))
    st.configure("TNotebook.Tab", background=C["bg"], foreground=C["muted"], bordercolor=C["line"], lightcolor=C["bg"],
                 darkcolor=C["bg"], padding=(p(14), p(6)), font=(fam, 10))
    st.map("TNotebook.Tab", background=[("selected", C["panel"]), ("active", C["hover"])],
           foreground=[("selected", C["text"]), ("active", C["text"])],
           lightcolor=[("selected", C["accent"])], bordercolor=[("selected", C["line"])], expand=[("selected", (0, p(2), 0, 0))])
    st.configure("TLabelframe", background=C["bg"], bordercolor=C["line"], lightcolor=C["bg"], darkcolor=C["bg"], relief="solid")
    st.configure("TLabelframe.Label", background=C["bg"], foreground=C["text"], font=(fam, 10, "bold"))
    for orient in ("Vertical", "Horizontal"):
        st.configure(orient + ".TScrollbar", background=C["panel"], troughcolor=C["bg"], bordercolor=C["bg"], arrowcolor=C["muted"],
                     lightcolor=C["panel"], darkcolor=C["panel"], gripcount=0, relief="flat")
        st.map(orient + ".TScrollbar", background=[("active", C["line"])])
        st.configure(orient + ".TProgressbar", background=C["accent"], troughcolor=C["line"], bordercolor=C["line"],
                     lightcolor=C["accent"], darkcolor=C["accent"], thickness=p(8))
    st.configure("TSeparator", background=C["line"])
    st.configure("Treeview", background=C["panel"], fieldbackground=C["panel"], foreground=C["text"], bordercolor=C["line"],
                 rowheight=p(24))
    st.map("Treeview", background=[("selected", C["select"])], foreground=[("selected", C["text"])])
    st.configure("Treeview.Heading", background=C["bg"], foreground=C["text"], bordercolor=C["line"], font=(fam, 10, "bold"))
    return st


def style_text(w, root=None):
    """tk.Text / tk.Listbox in the dark palette"""
    C = COL
    kw = dict(background=C["how"], foreground=C["text"], selectbackground=C["select"], selectforeground=C["text"],
              highlightthickness=1, highlightbackground=C["line"], highlightcolor=C["accent"], borderwidth=0, relief="flat")
    try:
        w.configure(insertbackground=C["text"], **kw)
    except Exception:                                          # noqa: BLE001 - Listbox has no insertbackground
        w.configure(**kw)
    return w


def icon_path():
    """Absolute path of gsvr.ico (frozen: _MEIPASS/_internal or next to the exe; dev: packaging/gsvr.ico) or None."""
    import os
    import sys
    here = os.path.dirname(os.path.abspath(__file__))
    cands = []
    for base in (getattr(sys, "_MEIPASS", None), os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False) else None):
        if base:
            cands += [os.path.join(base, "gsvr.ico"), os.path.join(base, "_internal", "gsvr.ico")]
    cands += [os.path.join(here, "packaging", "gsvr.ico"), os.path.join(here, "gsvr.ico")]
    for c in cands:
        if os.path.isfile(c):
            return c
    return None


def set_window_icon(w, platform=None):
    """Windows: the GSVR icon for the Tk window w (title bar / taskbar); other systems and any error: no-op. -> True when set."""
    import sys
    if (platform or sys.platform) != "win32":
        return False
    p = icon_path()
    if not p:
        return False
    try:
        w.iconbitmap(default=p)
        return True
    except Exception:                                          # noqa: BLE001 - cosmetic only
        return False


def dark_toplevel(w):
    try:
        w.configure(bg=COL["bg"])
    except Exception:                                          # noqa: BLE001
        pass
    dark_title_bar(w)
    set_window_icon(w)
    return w


# ---- Windows title bar in the dark palette (DWM; Windows 10 1809+ dark mode, Windows 11 22000+ exact caption / text colour) ----
DWMWA_USE_IMMERSIVE_DARK_MODE = 20            # Windows 10 20H1+ / 11
DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19        # Windows 10 1809-1909
DWMWA_CAPTION_COLOR = 35                      # Windows 11 only (older builds answer E_INVALIDARG: ignored)
DWMWA_TEXT_COLOR = 36
_SWP_FRAMECHANGED_ONLY = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020   # NOSIZE|NOMOVE|NOZORDER|NOACTIVATE|FRAMECHANGED


def colorref(hexcol):
    """'#rrggbb' -> Win32 COLORREF 0x00BBGGRR (int)"""
    h = hexcol.lstrip("#")
    if len(h) != 6:
        raise ValueError("need #rrggbb: %r" % hexcol)
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (b << 16) | (g << 8) | r


def dark_title_bar(w, platform=None):
    """Windows: dark title bar for the Tk window w (root or Toplevel) in our palette. No-op (False) on every other system and on any
    error (old Windows, no dwmapi). -> True when the dark-mode attribute was accepted."""
    import sys
    if (platform or sys.platform) != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        w.update_idletasks()                                   # the Tk wrapper (frame) window must exist
        u32, dwm = ctypes.windll.user32, ctypes.windll.dwmapi
        u32.GetParent.argtypes, u32.GetParent.restype = [wintypes.HWND], wintypes.HWND
        u32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        dwm.DwmSetWindowAttribute.restype = ctypes.c_long
        hwnd = u32.GetParent(w.winfo_id()) or w.winfo_id()

        def put(attr, value):
            v = ctypes.c_uint(value)
            return dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v)) == 0
        ok = put(DWMWA_USE_IMMERSIVE_DARK_MODE, 1) or put(DWMWA_USE_IMMERSIVE_DARK_MODE_OLD, 1)
        put(DWMWA_CAPTION_COLOR, colorref(COL["bg"]))
        put(DWMWA_TEXT_COLOR, colorref(COL["text"]))
        u32.SetWindowPos(hwnd, None, 0, 0, 0, 0, _SWP_FRAMECHANGED_ONLY)   # repaint the frame now (Windows 10 keeps the old one otherwise)
        return bool(ok)
    except Exception:                                          # noqa: BLE001 - cosmetic only, never break the window
        return False
