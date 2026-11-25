"""Simple view (Tk): ONE dark checklist screen for first start, "one step left" and "ready" + the help dialog.
Logic lives in firstrun.py (checklist(), FirstRunJob - headless, tested). Design: the companion redesign mock (5 rows, the next open row
highlighted, one big primary button, footer with tray hint / Help on Discord / Advanced view, EN/DE/JA switch).
Blocking work (game detection, HTTP to the local companion, the VRChat log folder look-up) runs in threads; the Tk thread only reads
self.q. Plain tk widgets + Canvas only (no ttk theme, no images, no new dependency); sizes go through px() so DPI scaling stays sane.
Folder paths are never drawn on this screen: only on click (chip info) or in the Advanced view.
Demo / screenshot mode: GSVR_UI_DEMO=start|first|step|ready (ui_demo.py) renders simulated states without touching folders/network.
"""
import os
import queue
import sys
import time

import firstrun
import i18n
import logwatch
import ui_theme
import worlds
from i18n import tr

DETECT_KEYS = ("hl_root", "rose_install", "ro_install", "war3_root", "war3_maps")
DETECT_EVERY = 60.0         # s between full game scans in the simple view (status still refreshes every few seconds)
REFRESH_S = 3.0

# one design system for both views (ui_theme.py): dark palette, lights on dark; text contrast >= 4.5:1 on BG and PANEL (test_simple_view.py)
COL = ui_theme.COL
LIGHTS = ui_theme.LIGHTS
FIT_QUIET_S = 0.6           # no window resize while the player drags / resizes the window (a resize mid-drag fights the move = lag)
DOT = {"ok": ("ok", "\u2713"), "warn": ("warn", None), "bad": ("bad", "!"), "idle": ("idle", None), "busy": ("accent", "\u2026")}


def dpi_aware():
    """Windows: declare the process DPI aware BEFORE the first Tk() so text is drawn sharp at 125-200 % instead of bitmap-stretched.
    System-DPI aware (1), like IDLE: Tk 8.6 takes its scaling once at start and does not follow per-monitor DPI changes, so on a second
    monitor with another scaling Windows keeps resizing the window for us. No-op elsewhere / on old Windows / when already set (manifest).
    GSVR_NO_DPI=1 switches it off."""
    if sys.platform != "win32" or os.environ.get("GSVR_NO_DPI"):
        return False
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)         # Windows 8.1+
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()              # Vista / 7 / 8
        return True
    except Exception:                                          # noqa: BLE001 - cosmetic only, never block the start
        return False


def copy_to_clipboard(root, text):
    root.clipboard_clear()
    root.clipboard_append(text)


def help_dialog(root, help_key, title=None):
    """modal-ish window: the fix steps in the user's language + the Discord help channel (Open / Copy)"""
    import tkinter as tk
    from tkinter import ttk
    w = ui_theme.dark_toplevel(tk.Toplevel(root))
    w.title(title or tr("help.title"))
    w.transient(root)
    f = ttk.Frame(w, padding=14)
    f.pack(fill="both", expand=True)
    ttk.Label(f, text=tr(help_key), wraplength=460, justify="left").pack(anchor="w")
    ttk.Separator(f).pack(fill="x", pady=10)
    ttk.Label(f, text=tr("help.discord", url=worlds.DISCORD_URL), wraplength=460, justify="left").pack(anchor="w")
    row = ttk.Frame(f)
    row.pack(anchor="w", pady=(10, 0))
    ttk.Button(row, text=tr("help.open_discord"), command=lambda: worlds.open_link(worlds.DISCORD_URL)).pack(side="left")
    ttk.Button(row, text=tr("help.copy_discord"), command=lambda: copy_to_clipboard(root, worlds.DISCORD_URL)).pack(side="left", padx=6)
    ttk.Button(row, text="OK", command=w.destroy).pack(side="left")
    return w


def open_world(root, url):
    """vrchat:// launch; when the OS has no handler the link is copied and the player told how to continue"""
    from tkinter import messagebox
    if not worlds.open_link(url):
        copy_to_clipboard(root, url)
        messagebox.showinfo("GSVR", tr("fr.open_failed"), parent=root)


def vrchat_log_dir(settings):
    """the VRChat log folder the companion would watch (override from the settings, else the platform default); None = none here"""
    try:
        return logwatch.default_log_dir(override=(settings.get("vrchat_log_dir") or "").strip() or None)
    except Exception:                                          # noqa: BLE001 - detection must never break the screen
        return None


# ----------------------------------------------------------------------------------------------------- canvas widgets
def _round_pts(x1, y1, x2, y2, r):
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    return (x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r,
            x1, y1 + r, x1, y1)


def round_rect(cv, x1, y1, x2, y2, r, **kw):
    return cv.create_polygon(_round_pts(x1, y1, x2, y2, r), smooth=True, **kw)


class RoundButton:
    """a rounded button on a Canvas (tk.Button ignores colours on macOS and looks native elsewhere). kind: primary | secondary.
    Keyboard: Tab focus + Return/space; hover = accent outline."""

    def __init__(self, ui, parent, text, command, kind="secondary", enabled=True, big=False):
        tk = ui.tk
        self.ui, self.command, self.kind, self.enabled = ui, command, kind, enabled
        if kind == "primary":
            self.font = ui.f(14, "bold")
        else:
            self.font = ui.f(10.5 if big else 10)
        self.padx, self.pady = ui.px(26 if kind == "primary" else 14 if big else 12), ui.px(13 if big else 6)
        self.text = text
        self.hover = self.focus = False
        self.cv = tk.Canvas(parent, highlightthickness=0, bd=0, bg=parent.cget("bg"), takefocus=1,
                            cursor="hand2" if enabled else "")
        self._size()
        self.cv.bind("<Configure>", lambda _e: self.redraw())
        self.cv.bind("<Enter>", lambda _e: self._set(hover=True))
        self.cv.bind("<Leave>", lambda _e: self._set(hover=False))
        self.cv.bind("<FocusIn>", lambda _e: self._set(focus=True))
        self.cv.bind("<FocusOut>", lambda _e: self._set(focus=False))
        self.cv.bind("<ButtonRelease-1>", self._click)
        self.cv.bind("<Return>", self._click)
        self.cv.bind("<space>", self._click)

    def _size(self):
        f = self.ui.font_obj(self.font)
        self.cv.configure(width=f.measure(self.text) + 2 * self.padx, height=f.metrics("linespace") + 2 * self.pady)

    def _set(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        self.redraw()

    def _click(self, _e=None):
        if self.enabled and self.command:
            self.command()

    def set(self, text=None, enabled=None):
        try:
            self._set_now(text, enabled)
        except self.ui.tk.TclError:                            # the screen was redrawn meanwhile (e.g. the 'Link copied' timer)
            pass

    def _set_now(self, text, enabled):
        if text is not None and text != self.text:
            self.text = text
            self._size()
        if enabled is not None:
            self.enabled = enabled
            self.cv.configure(cursor="hand2" if enabled else "")
        self.redraw()

    def redraw(self):
        cv = self.cv
        try:
            cv.delete("all")
        except self.ui.tk.TclError:
            return
        w, h = max(cv.winfo_width(), int(cv.cget("width"))), max(cv.winfo_height(), int(cv.cget("height")))
        if cv.winfo_width() > 1:
            w = cv.winfo_width()
        C = COL
        r = self.ui.px(10 if self.kind == "primary" else 8)
        if self.kind == "primary":
            fill, ink = (C["ok"], C["ink"]) if self.enabled else (C["off"], C["muted"])
            outline = C["accent"] if (self.focus and self.enabled) else fill
        else:
            fill, ink = cv.cget("bg"), C["text"] if self.enabled else C["muted"]
            outline = C["accent"] if (self.hover or self.focus) and self.enabled else C["line"]
        round_rect(cv, 1, 1, w - 1, h - 1, r, fill=fill, outline=outline, width=1)
        cv.create_text(w // 2, h // 2, text=self.text, fill=ink, font=self.font)

    def pack(self, **kw):
        self.cv.pack(**kw)
        return self

    def grid(self, **kw):
        self.cv.grid(**kw)
        return self


class FirstRun:
    """The simple view. Same entry points as before (gui.py): show() = first start, show_home() = setup done, hide(), update_result()."""

    def __init__(self, root, c, bg, on_advanced, on_lang, on_check=None, on_install=None, tray_ok=None, detect_fn=None, vrc_fn=None):
        """c = gui.Controller, bg = run-in-thread helper, on_advanced() = leave for the tabbed window, on_lang(code) = language chosen,
        on_check() = run an update check now (result comes back through update_result), on_install() = install the found update,
        tray_ok() -> bool (closing hides to the tray), detect_fn / vrc_fn = injectable game detection / VRChat state (demo, tests)"""
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.root, self.c, self.bg = root, c, bg
        self.on_advanced, self.on_lang, self.on_check, self.on_install = on_advanced, on_lang, on_check, on_install
        self.tray_ok = tray_ok or (lambda: False)
        self.detect_fn = detect_fn or firstrun.detect
        self.vrc_fn = vrc_fn or (lambda settings, connected: firstrun.vrchat_state(vrchat_log_dir(settings), connected))
        self.upd = ("", None)            # (state, latest) of the update line: "", "checking", "current", "available", "failed"
        self.frame = tk.Frame(root, bg=COL["bg"])
        self.q = queue.Queue()
        self.entries = []
        self.st, self.running, self.vrc = None, False, "unknown"
        self.job = None
        self.job_error = ""
        self.demo_job = None             # demo mode: a fixed {phase, pct, eta} instead of a real job
        self.phase = "home"
        self.searching = False
        self.visible = False
        self._ticking = False
        self.busy = False
        self._t = 0.0
        self._det = None                 # (settings signature, time, entries): the drive scan is slow, refreshes reuse it
        self._sig = None
        self.model = None
        self.w = {}                      # widgets updated in place: title, sub, row texts, progress, primary
        self._copied_until = 0.0
        self._fonts = {}
        self._fam = "TkDefaultFont"
        self.det_busy = False            # the game scan runs in its own thread (slow: drive scan); status / VRChat do not wait for it
        self._fit = None                 # last size fit_window() applied
        self._grow_only = False          # set by a language switch: the next fit may only grow the window
        self._moved = 0.0                # time of the last move/resize of the window (<Configure> of the root)
        self._fit_after = None
        try:
            root.bind("<Configure>", self._on_configure, add="+")
        except Exception:                                      # noqa: BLE001
            pass

    # ---------------------------------------------------------------- sizes / fonts
    def scale(self):
        return ui_theme.scale(self.root)

    def px(self, n):
        return ui_theme.px(self.root, n)

    def family(self):
        """cached per process + language (enumerating the fonts is slow on Windows and used to run on every redraw)"""
        return ui_theme.family(self.root)

    def font(self):
        return self.family()

    def f(self, size, weight="normal"):
        return (self._fam, int(round(size)), weight)

    def font_obj(self, spec):
        from tkinter import font as tkfont
        o = self._fonts.get(spec)
        if o is None:
            o = self._fonts[spec] = tkfont.Font(root=self.root, family=spec[0], size=spec[1], weight=spec[2])
        return o

    # ---------------------------------------------------------------- lifecycle
    def show(self):
        """first start (gui: fresh install): the same checklist screen; the primary button starts the preparation"""
        self._open()

    def show_home(self):
        """setup already done: the same checklist screen"""
        self._open()

    def _open(self):
        self.visible = True
        self.phase = "home"
        self._det = None                                       # fresh scan whenever the simple view opens
        self._sig = None
        self.frame.pack(fill="both", expand=True)
        self.apply()
        self.refresh(force_detect=True)
        if not self._ticking:
            self._ticking = True
            self._tick()

    def hide(self):
        self.visible = False
        self.frame.pack_forget()

    def first_done(self):
        return bool(self.c.settings.get("first_run_done"))

    def job_summary(self):
        if self.demo_job is not None:
            return self.demo_job
        j = self.job
        if j is not None and j.phase in ("starting", "building"):
            return {"phase": j.phase, "pct": j.pct, "eta": j.eta, "error": j.error}
        if self.job_error:
            return {"phase": "error", "error": self.job_error}
        return None

    def refresh(self, force_detect=False):
        """two background jobs, results through self.q (the Tk thread only reads the queue):
        status + VRChat state ("inputs", every REFRESH_S, quick) and the (cached) game detection ("games", slow drive scan) -
        so the window paints and the VRChat row fills while the games row still says 'Searching ...'"""
        self._t = time.time()
        s = dict(self.c.settings)
        for e in self.entries:                                 # folders picked this session (saved by the job)
            if e.get("picked") and e.get("found"):
                s[{"rose": "rose_install", "ro": "ro_install", "war3": "war3_root"}.get(e["id"], "hl_root")] = e["root"]
        sig = tuple((s.get(k) or "").strip() for k in DETECT_KEYS)
        if force_detect:
            self._det = None
        d = self._det
        if not self.det_busy and (d is None or d[0] != sig or time.time() - d[1] > DETECT_EVERY):
            self.det_busy = True
            if d is None or not self.entries:
                self.searching = True

            def detect():
                ents = None
                try:
                    ents = self.detect_fn(s)
                    self._det = (sig, time.time(), ents)
                except Exception:                              # noqa: BLE001 - keep the last state
                    pass
                self.q.put(("games", ents))
            self.bg(detect)
        if self.busy:
            return
        self.busy = True

        def work():
            st, run, vrc = None, False, "unknown"
            try:
                run = bool(self.c.running())
                st = self.c.status() if run else None
                vrc = self.vrc_fn(s, False)
            except Exception:                                  # noqa: BLE001 - keep the last state
                pass
            self.q.put(("inputs", (st, run, vrc)))
        self.bg(work)

    def compute(self):
        from version import VERSION
        return firstrun.checklist(self.entries, self.st, self.running, self.vrc, self.job_summary(), self.upd, VERSION, self.first_done(),
                                  scanning=self.searching)

    # ---------------------------------------------------------------- drawing
    @staticmethod
    def _row_sig(r):
        """what needs a rebuild of the row ('next' only recolours: mark_next)"""
        return (r["key"], r["status"], r["button"], tuple(r["chips"] or ()), r["progress"] is not None, r["how"])

    def _struct(self, m):
        """-> (frame signature, {row key: row signature}, buttons signature): what forces a rebuild of which part"""
        frame = (i18n.lang(), self.tray_ok(), tuple(r["key"] for r in m["rows"]), bool(m.get("hint")))
        cta = (m["primary"]["action"], m["primary"]["enabled"], tuple((b["label"], b["action"]) for b in m["secondary"]))
        return frame, {r["key"]: self._row_sig(r) for r in m["rows"]}, cta, tuple(bool(r["next"]) for r in m["rows"])

    def apply(self):
        """recompute the model and touch only what changed: texts in place, a changed row / the button bar rebuilt on its own,
        the whole screen only when the language or the row list changed. A full rebuild costs 20-40 ms (more on Windows and while a
        scan thread competes for the GIL) - the first seconds used to rebuild everything 4-5 times = the window lagged while dragged."""
        if not self.visible:
            return
        m = self.compute()
        self.model = m
        sig = self._struct(m)
        old = self._sig
        if old is None or sig[0] != old[0]:
            self._sig = sig
            self.draw()
            return
        try:
            self._sig = sig
            for i, r in enumerate(m["rows"]):
                if sig[1][r["key"]] != old[1].get(r["key"]):
                    self.redraw_row(r)
                elif sig[3][i] != old[3][i]:
                    self.mark_next(r)
            if sig[2] != old[2]:
                self.draw_cta(m)
            self.w["title"].configure(text=m["title"])
            self.w["sub"].configure(text=m["sub"])
            for r in m["rows"]:
                self.w["row_" + r["key"]].configure(text=r["text"])
                if r["progress"] is not None and ("prog_" + r["key"]) in self.w:
                    self._draw_prog(self.w["prog_" + r["key"]], r["progress"])
            self.w["primary"].set(text=m["primary"]["label"])
            if sig[1] != old[1]:
                self.fit_soon()
        except (KeyError, self.tk.TclError):
            self._sig = None
            self.draw()

    def clear(self):
        for w in self.frame.winfo_children():
            w.destroy()
        self.w = {}

    def draw(self):
        if not self.visible:
            return
        tk = self.tk
        C = COL
        px = self.px
        self._fam = self.family()
        m = self.model or self.compute()
        self.clear()
        self.root.configure(bg=C["bg"])
        body = tk.Frame(self.frame, bg=C["bg"], padx=px(22), pady=px(20))
        body.pack(fill="both", expand=True)
        wrap = px(560)

        # header: title + language switch, then the one-line explanation
        top = tk.Frame(body, bg=C["bg"])
        top.pack(fill="x")
        self.w["title"] = tk.Label(top, text=m["title"], bg=C["bg"], fg=C["text"], font=self.f(18, "bold"), anchor="w", justify="left")
        self.w["title"].pack(side="left", anchor="w")
        self.lang_switch(top).pack(side="right", anchor="n")
        self.w["sub"] = tk.Label(body, text=m["sub"], bg=C["bg"], fg=C["muted"], font=self.f(11), anchor="w", justify="left",
                                 wraplength=px(740))
        self.w["sub"].pack(fill="x", pady=(px(10), 0))

        # the checklist (one holder frame per row: a changed row is rebuilt inside its holder, the others stay)
        lst = tk.Frame(body, bg=C["panel"], highlightthickness=1, highlightbackground=C["line"], highlightcolor=C["line"])
        lst.pack(fill="x", pady=(px(14), 0))
        self._wrap = wrap
        for i, r in enumerate(m["rows"]):
            if i:
                tk.Frame(lst, bg=C["line"], height=1).pack(fill="x")
            holder = tk.Frame(lst, bg=C["panel"])
            holder.pack(fill="x")
            self.w["holder_" + r["key"]] = holder
            self.draw_row(holder, r, wrap)

        if m.get("hint"):
            self.hint_card(body).pack(fill="x", pady=(px(12), 0))

        # buttons
        self.w["cta"] = tk.Frame(body, bg=C["bg"])
        self.w["cta"].pack(fill="x", pady=(px(16), 0))
        self.draw_cta(m)

        # footer
        tk.Frame(body, bg=C["line"], height=1).pack(fill="x", pady=(px(14), px(12)))
        foot = tk.Frame(body, bg=C["bg"])
        foot.pack(fill="x")
        tk.Label(foot, text=tr("cl.tray_hint" if self.tray_ok() else "cl.min_hint"), bg=C["bg"], fg=C["muted"], font=self.f(10),
                 anchor="w").pack(side="left")
        self.link(foot, tr("fr.open_app"), self.on_advanced).pack(side="right")
        self.link(foot, tr("cl.discord"), lambda: worlds.open_link(worlds.DISCORD_URL)).pack(side="right", padx=(0, px(16)))
        if self._fit is None:                                  # first paint: right size before the window shows
            self.frame.update_idletasks()
            self.fit_window()
        else:
            self.fit_soon()

    def draw_cta(self, m):
        px, cta = self.px, self.w["cta"]
        for w in cta.winfo_children():
            w.destroy()
        p = m["primary"]
        self.w["primary"] = RoundButton(self, cta, p["label"], lambda a=p["action"]: self.act(a), kind="primary", enabled=p["enabled"],
                                        big=True).pack(side="left", fill="x", expand=True)
        for b in m["secondary"]:
            btn = RoundButton(self, cta, b["label"], None, big=True)
            btn.command = (lambda a=b["action"], bb=btn: self.act(a, bb))
            btn.pack(side="left", padx=(px(10), 0))

    def redraw_row(self, r):
        holder = self.w["holder_" + r["key"]]
        for w in holder.winfo_children():
            w.destroy()
        self.w.pop("prog_" + r["key"], None)
        self.draw_row(holder, r, self._wrap)

    def lang_switch(self, parent):
        """EN / DE / 日本語 as one rounded segmented pill (Canvas); click = switch language"""
        tk, C, px = self.tk, COL, self.px
        segs = (("en", "EN"), ("de", "DE"), ("ja", "日本語"))
        fo, fb = self.f(9), self.f(9, "bold")
        widths = [max(self.font_obj(fo).measure(t), self.font_obj(fb).measure(t)) + 2 * px(10) for _c, t in segs]
        h = self.font_obj(fo).metrics("linespace") + 2 * px(5)
        W = sum(widths) + 2
        cv = tk.Canvas(parent, width=W, height=h + 2, bg=C["bg"], highlightthickness=0, bd=0, cursor="hand2")
        r = (h + 1) / 2
        x = 1
        for i, ((code, text), w) in enumerate(zip(segs, widths)):
            on = code == i18n.lang()
            if on:
                if i == 0:
                    round_rect(cv, x, 1, x + w + r, h + 1, r, fill=C["panel"], outline="")
                    cv.create_rectangle(x + w, 1, x + w + r, h + 1, fill=C["bg"], outline="")
                elif i == len(segs) - 1:
                    round_rect(cv, x - r, 1, x + w, h + 1, r, fill=C["panel"], outline="")
                    cv.create_rectangle(x - r, 1, x, h + 1, fill=C["bg"], outline="")
                else:
                    cv.create_rectangle(x, 1, x + w, h + 1, fill=C["panel"], outline="")
            x += w
        x = 1
        for i, ((code, text), w) in enumerate(zip(segs, widths)):            # texts + separators after all backgrounds
            on = code == i18n.lang()
            cv.create_text(x + w / 2, h / 2 + 1, text=text, fill=C["text"] if on else C["muted"], font=fb if on else fo)
            if i:
                cv.create_line(x, 1, x, h + 1, fill=C["line"])
            x += w
        round_rect(cv, 1, 1, W - 1, h + 1, r, fill="", outline=C["line"])
        bounds = []
        x = 1
        for (code, _t), w in zip(segs, widths):
            bounds.append((x, x + w, code))
            x += w

        def click(e):
            for a, b, code in bounds:
                if a <= e.x < b:
                    self.set_lang(code)
        cv.bind("<Button-1>", click)
        return cv

    def set_lang(self, code):
        if code != i18n.lang():
            self.on_lang(code)
            self._grow_only = True
            self._sig = None
            self.model = None
            self.apply()

    def link(self, parent, text, cmd):
        tk, C = self.tk, COL
        lb = tk.Label(parent, text=text, bg=C["bg"], fg=C["accent"], font=self.f(10), cursor="hand2")
        lb.bind("<Button-1>", lambda _e: cmd())
        lb.bind("<Enter>", lambda _e: lb.configure(font=(self._fam, 10, "underline")))
        lb.bind("<Leave>", lambda _e: lb.configure(font=self.f(10)))
        return lb

    def dot(self, parent, r, bg):
        s = self.px(24)
        cv = self.tk.Canvas(parent, width=s, height=s, bg=bg, highlightthickness=0, bd=0)
        self.paint_dot(cv, r)
        return cv

    def paint_dot(self, cv, r):
        C = COL
        s = self.px(24)
        col_key, glyph = DOT[r["status"]]
        if r["status"] == "warn" and not r["next"]:            # open, but not its turn yet: grey number, only the next row is yellow
            col_key = "idle"
        col = C[col_key]
        lw = self.px(2)
        cv.delete("all")
        cv.create_oval(lw, lw, s - lw, s - lw, outline=col, width=lw)
        cv.create_text(s / 2, s / 2, text=glyph or str(r["num"]), fill=col, font=self.f(9, "bold"))

    @staticmethod
    def _stripe_col(r):
        return (COL["bad"] if r["status"] == "bad" else COL["warn"]) if r["next"] else COL["panel"]

    @staticmethod
    def _small_fg(r):
        return COL["text"] if (r["next"] or r["status"] == "bad") else COL["muted"]

    def mark_next(self, r):
        """the 'next' highlight moved: recolour stripe, number and small text in place (no rebuild)"""
        k = r["key"]
        self.w["stripe_" + k].configure(bg=self._stripe_col(r))
        self.paint_dot(self.w["dot_" + k], r)
        self.w["row_" + k].configure(fg=self._small_fg(r))

    def draw_row(self, lst, r, wrap):
        tk, C, px = self.tk, COL, self.px
        bg = C["panel"]
        outer = tk.Frame(lst, bg=bg)
        outer.pack(fill="x")
        self.w["stripe_" + r["key"]] = tk.Frame(outer, bg=self._stripe_col(r), width=px(3))      # the next open row: thin bar on the left
        self.w["stripe_" + r["key"]].pack(side="left", fill="y")
        row = tk.Frame(outer, bg=bg, pady=px(11))
        row.pack(side="left", fill="x", expand=True)
        row = self._pad(row, bg, px(11), px(14))
        row.columnconfigure(1, weight=1)
        self.w["dot_" + r["key"]] = self.dot(row, r, bg)
        self.w["dot_" + r["key"]].grid(row=0, column=0, sticky="nw", padx=(0, px(14)), pady=(px(2), 0))
        txt = tk.Frame(row, bg=bg)
        txt.grid(row=0, column=1, sticky="w")
        tk.Label(txt, text=r["title"], bg=bg, fg=C["text"], font=self.f(11, "bold"), anchor="w").pack(anchor="w")
        self.w["row_" + r["key"]] = tk.Label(txt, text=r["text"], bg=bg, fg=self._small_fg(r), font=self.f(10), anchor="w", justify="left",
                                             wraplength=wrap)
        self.w["row_" + r["key"]].pack(anchor="w")
        if r["button"]:
            label, action = r["button"]
            b = RoundButton(self, row, tr(label), None)
            b.command = (lambda a=action, bb=b: self.act(a, bb))
            b.grid(row=0, column=2, sticky="ne", padx=(px(12), 0))
        extra = 1
        if r["chips"]:
            self.chips(row, r["chips"], bg).grid(row=extra, column=1, columnspan=2, sticky="w", pady=(px(6), 0))
            extra += 1
        if r["progress"] is not None:
            pc = tk.Canvas(row, height=px(6), bg=bg, highlightthickness=0, bd=0)
            pc.grid(row=extra, column=1, columnspan=2, sticky="we", pady=(px(8), 0))
            pc.bind("<Configure>", lambda _e, cv=pc, k=r["key"]: self._draw_prog(cv, self._prog_of(k)))
            self.w["prog_" + r["key"]] = pc
            extra += 1

    def _pad(self, row, bg, left, right):
        """inner frame with asymmetric horizontal padding (tk.Frame padx is symmetric)"""
        inner = self.tk.Frame(row, bg=bg)
        inner.pack(fill="x", expand=True, padx=(left, right))
        return inner

    def _prog_of(self, key):
        for r in (self.model or {}).get("rows", ()):
            if r["key"] == key:
                return r["progress"] or 0
        return 0

    def _draw_prog(self, cv, pct):
        C = COL
        try:
            cv.delete("all")
            w, h = cv.winfo_width(), int(cv.cget("height"))
            round_rect(cv, 0, 0, w, h, h / 2, fill=C["line"], outline=C["line"])
            fw = int(w * max(0, min(100, pct or 0)) / 100)
            if fw > 2:
                round_rect(cv, 0, 0, fw, h, h / 2, fill=C["accent"], outline=C["accent"])
        except self.tk.TclError:
            pass

    def chips(self, parent, chips, bg):
        """one line: found games first, then the not-found ones that still fit, the rest as one '+N more' chip (click = Add a game menu);
        only found chips may wrap onto a next line"""
        tk, C, px = self.tk, COL, self.px
        box = tk.Frame(parent, bg=bg)
        maxw = px(690)
        line = None
        used = maxw + 1
        f = self.font_obj(self.f(10))
        h = f.metrics("linespace") + 2 * px(3)

        def cw(text):
            return f.measure(text) + 2 * px(10) + 2
        yes = [c for c in chips if c[1]]
        no = [c for c in chips if not c[1]]
        n = firstrun.chips_fit([cw("\u2713 " + c[0]) for c in yes], [cw("\u2013 " + c[0]) for c in no], maxw, px(6),
                               cw(tr("cl.more", n=len(no))))
        items = yes + no[:n]
        if n < len(no):
            items.append((tr("cl.more", n=len(no) - n), None, None))
        for name, found, gid in items:
            if found is None:                                  # the '+N more' chip
                w = cw(name)
                if used + w + px(6) > maxw:
                    line = tk.Frame(box, bg=bg)
                    line.pack(anchor="w", pady=(0 if used > maxw else px(6), 0))
                    used = 0
                cv = tk.Canvas(line, width=w, height=h, bg=bg, highlightthickness=0, bd=0, cursor="hand2")
                round_rect(cv, 1, 1, w - 1, h - 1, h / 2, fill=bg, outline=C["line"])
                cv.create_text(w / 2, h / 2, text=name, fill=C["muted"], font=self.f(10))
                cv.pack(side="left", padx=(0 if used == 0 else px(6), 0))
                cv.bind("<Button-1>", lambda _e, a=cv: self.add_menu(a))
                break
            text = ("\u2713 " if found else "\u2013 ") + name
            w = cw(text)
            if used + w + px(6) > maxw:
                line = tk.Frame(box, bg=bg)
                line.pack(anchor="w", pady=(0 if used > maxw else px(6), 0))
                used = 0
            cv = tk.Canvas(line, width=w, height=h, bg=bg, highlightthickness=0, bd=0, cursor="hand2" if found else "")
            round_rect(cv, 1, 1, w - 1, h - 1, h / 2, fill=bg, outline=C["line"])
            mark = "\u2713 " if found else "\u2013 "
            mw = f.measure(mark)
            cv.create_text(px(10), h / 2, text=mark, fill=C["ok"] if found else C["idle"], font=self.f(10), anchor="w")
            cv.create_text(px(10) + mw, h / 2, text=name, fill=C["text"] if found else C["muted"], font=self.f(10), anchor="w")
            cv.pack(side="left", padx=(0 if used == 0 else px(6), 0))
            if found:
                cv.bind("<Button-1>", lambda _e, g=gid: self.chip_info(g))
            used += w + px(6)
        return box

    def hint_card(self, parent):
        """static hint (no status, no colour): where the VRChat setting 'Allow Untrusted URLs' is"""
        tk, C, px = self.tk, COL, self.px
        box = tk.Frame(parent, bg=C["line"], padx=1, pady=1)
        inner = tk.Frame(box, bg=C["how"], padx=px(12), pady=px(8))
        inner.pack(fill="x")
        self.w["hint"] = tk.Label(inner, text=(self.model or {}).get("hint", ""), bg=C["how"], fg=C["muted"], font=self.f(10), anchor="w",
                                  justify="left", wraplength=px(700))
        self.w["hint"].pack(anchor="w")
        return box

    def _on_configure(self, e):
        if e.widget is self.root:
            self._moved = time.time()

    def required_size(self):
        """(w, h) the content needs, capped to the usable screen (update_idletasks first so Tk has the new text sizes)"""
        try:
            self.root.update_idletasks()
        except Exception:                                      # noqa: BLE001
            pass
        need_h = self.frame.winfo_reqheight()
        need_w = max(self.frame.winfo_reqwidth(), self.px(820))
        h = min(need_h, max(420, self.root.winfo_screenheight() - 110))
        w = min(need_w, max(620, self.root.winfo_screenwidth() - 40))
        return w, h

    def fit_window(self, grow_only=False):
        """size the window to the content (never past the screen's usable height, 1080p at 100-150 % scaling).
        Only when the needed size changed, and never while the window is being dragged/resized (deferred until it rests):
        a geometry() call during a drag fights the move on Windows (the window stuttered in the first seconds).
        grow_only (after a language change: DE/JA are longer than EN): never smaller than the current window; the content size
        is also the window's minsize, so nothing is ever clipped and the player does not have to drag it to the right size."""
        try:
            grow_only = grow_only or self._grow_only
            if not grow_only and time.time() - self._moved < FIT_QUIET_S and self._fit is not None:
                if self._fit_after is None:
                    self._fit_after = self.root.after(int(FIT_QUIET_S * 1000), self._fit_later)
                return
            w, h = self.required_size()
            try:
                self.root.minsize(w, h)
            except Exception:                                  # noqa: BLE001
                pass
            if grow_only:
                cw, ch = self.root.winfo_width(), self.root.winfo_height()
                if cw > 1 and ch > 1:
                    w, h = max(w, cw), max(h, ch)
                self._grow_only = False
            if (w, h) != self._fit or (w, h) != (self.root.winfo_width(), self.root.winfo_height()):
                self._fit = (w, h)
                self.root.geometry("%dx%d" % (w, h))
        except Exception:                                      # noqa: BLE001 - cosmetic only
            pass

    def fit_soon(self):
        """fit the window in its own idle callback (after Tk computed the new requested sizes) instead of inside the redraw"""
        if self._fit_after is None:
            try:
                self._fit_after = self.root.after_idle(self._fit_later)
            except self.tk.TclError:
                pass

    def _fit_later(self):
        self._fit_after = None
        if self.visible:
            self.fit_window()

    # ---------------------------------------------------------------- actions
    def act(self, action, btn=None):
        if not action:
            return
        if action == "start":
            self.start()
        elif action == "add":
            self.add_menu(btn)
        elif action == "cancel":
            self.cancel()
        elif action == "help_error":
            help_dialog(self.root, "help.error")
        elif action == "install":
            if self.on_install is not None:
                self.on_install()
        elif action == "check":
            self.check_clicked()
        elif action.startswith("open:"):
            open_world(self.root, worlds.launch_url(worlds.WORLDS[action[5:]][0]))
        elif action.startswith("copy:"):
            copy_to_clipboard(self.root, worlds.launch_url(worlds.WORLDS[action[5:]][0]))
            if btn is not None:
                old = btn.text
                btn.set(text=tr("cl.copied"))
                self.root.after(1800, lambda: btn.set(text=old))

    def chip_info(self, gid):
        """the folder of a found game: only on click, never on the screen itself"""
        from tkinter import messagebox
        e = next((x for x in self.entries if x["id"] == gid), None)
        if e:
            messagebox.showinfo(e["name"], tr("cl.chip_info", game=e["name"], how=firstrun.how_text(e.get("how", "none"))) + "\n\n" + e["root"],
                                parent=self.root)

    def add_menu(self, btn=None):
        from tkinter import messagebox
        missing = [e for e in self.entries if not e.get("found")]
        if not missing:
            messagebox.showinfo("GSVR", tr("cl.add_none"), parent=self.root)
            return
        m = self.tk.Menu(self.root, tearoff=0)
        for e in missing:
            m.add_command(label=e["name"] + " ...", command=lambda g=e["id"]: self.pick(g))
        try:
            w = getattr(btn, "cv", btn) if btn is not None else self.root     # a RoundButton or the '+N more' chip canvas
            m.tk_popup(w.winfo_rootx(), w.winfo_rooty() + w.winfo_height())
        finally:
            m.grab_release()

    def pick(self, gid):
        from tkinter import filedialog
        d = filedialog.askdirectory(title=tr("fr.pick_title", game=firstrun.game_name(gid)), parent=self.root)
        if not d:
            return

        def work():
            ok, msg = firstrun.validate_folder(gid, d)
            self.q.put(("picked", (gid, ok, msg)))
        self.bg(work)

    def start(self, chosen=None):
        """'Get my games ready' (all found games) or a game added later: FirstRunJob saves the folders, starts the helper, builds"""
        if self.job is not None and self.job.phase in ("starting", "building"):
            return
        chosen = chosen or [e for e in self.entries if e.get("found")]
        if not chosen:
            return
        self.job_error = ""
        self.job = firstrun.FirstRunJob(self.c, chosen)
        self.apply()

        def work():
            self.job.begin()
            while self.job.phase not in ("done", "cancelled", "error"):
                self.job.step()
                self.q.put(("job", None))
                time.sleep(1.0)
            self.q.put(("job", None))
        self.bg(work)

    def cancel(self):
        from tkinter import messagebox
        if self.job and messagebox.askyesno("GSVR", tr("fr.confirm_cancel"), parent=self.root):
            self.bg(self.job.cancel)                 # job.cancel() owns the phase change

    # ---------------------------------------------------------------- update line (version row)
    def upd_text(self):
        """text of the update state (empty = nothing to say): checking / up to date / update available / failed"""
        state, latest = self.upd
        if state == "checking":
            return tr("fr.upd_checking")
        if state == "current":
            return tr("fr.upd_current")
        if state == "available":
            return tr("fr.upd_avail", b=latest or "?")
        if state == "failed":
            return tr("fr.upd_failed")
        return ""

    def update_result(self, info):
        """gui: result of an update check (updater dict, None = failed). Quiet when nothing was asked on this screen and nothing is new."""
        if info is None:
            state, latest = "failed", None
        elif info.get("status") == "available":
            state, latest = "available", info.get("latest")
        else:
            state, latest = "current", None
        asked = self.upd[0] == "checking"
        if state == "current" and not asked:
            self.upd = ("", None)                        # a background check that found nothing: keep the screen quiet
        elif state == "failed" and not asked:
            return                                       # background failure: the log has it, no red text on the home screen
        else:
            self.upd = (state, latest)
        self._show_upd()

    def _show_upd(self):
        if self.visible:
            try:
                self.apply()
            except self.tk.TclError:
                pass

    def check_clicked(self):
        if self.on_check is None or self.upd[0] == "checking":
            return
        self.upd = ("checking", None)
        self._show_upd()
        self.on_check()

    # ---------------------------------------------------------------- event pump
    def _tick(self):
        if not self.visible:
            self._ticking = False
            return
        try:
            if time.time() - self._t > REFRESH_S:
                self.refresh()
            changed = False
            while True:
                kind, val = self.q.get_nowait()
                if kind == "inputs":
                    self.busy = False
                    st, run, vrc = val
                    if (st, run, vrc) != (self.st, self.running, self.vrc):
                        self.st, self.running, self.vrc = st, run, vrc
                        changed = True
                elif kind == "games":
                    self.det_busy = False
                    if val is not None:
                        keep = {e["id"]: e for e in self.entries if e.get("picked") and e.get("found")}
                        ents = [keep.get(e["id"], e) for e in val]
                        changed = changed or ents != self.entries or self.searching
                        self.entries = ents
                    elif self.searching:
                        changed = True
                    self.searching = False
                elif kind == "picked":
                    gid, ok, msg = val
                    if ok:
                        for e in self.entries:
                            if e["id"] == gid:
                                e.update(found=True, root=msg, how="configured", picked=True)
                        if self.first_done():
                            self.start([e for e in self.entries if e["id"] == gid])
                        changed = True
                    else:
                        from tkinter import messagebox
                        messagebox.showerror("GSVR", msg, parent=self.root)
                elif kind == "job" and self.job is not None:
                    if self.job.phase == "done":
                        self.job = None
                        self.c.settings.update(first_run_done=True)
                        try:
                            self.c.save_settings()
                        except OSError:
                            pass
                        self._det = None
                        self.refresh()
                    elif self.job.phase == "cancelled":
                        self.job = None
                    elif self.job.phase == "error":
                        self.job_error = self.job.error or "error"
                        self.job = None
                    changed = True
        except queue.Empty:
            if changed:
                self.apply()
        self.root.after(150, self._tick)


# ----------------------------------------------------------------------------------------------------- demo / screenshots
DEMO_STATES = ("start", "first", "step", "ready")


def demo_inputs(state):
    """simulated inputs for one demo state: (settings, entries, status, running, vrc, job, upd). Nothing is read from disk or network."""
    names = {g: firstrun.game_name(g) for g in firstrun.ORDER}
    found = {"rose": "C:\\Games\\ROSE Online", "valve": "C:\\Steam\\steamapps\\common\\Half-Life"}
    ents = [{"id": g, "name": names[g], "found": g in found, "root": found.get(g, ""), "how": "steam" if g == "valve" else "registry"
             if g in found else "none", "world": worlds.world_for_game(g), "picked": False} for g in firstrun.ORDER]
    settings = {"first_run_done": state in ("step", "ready")}
    st = {"app": "gsvr-companion", "world": None, "world_ok": True, "build": [], "rose_build": {}}
    job = None
    running = state != "start"
    if state == "first":
        job = {"phase": "building", "pct": 46, "eta": 180, "error": ""}
    upd = ("available", "0.9.60") if state == "step" else ("current", None)       # step = one open item: the new version
    return settings, ents, st, running, "running", job, upd


class _DemoController:
    def __init__(self, settings, st, running):
        self.settings, self._st, self._run = settings, st, running

    def running(self):
        return self._run

    def status(self):
        return self._st

    def save_settings(self):
        pass


def run_demo(state, lang="en", shot=None, hold_ms=1400):
    """render the simple view in a simulated state (GSVR_UI_DEMO); shot = PNG path (macOS screencapture of the window)"""
    import tkinter as tk
    state = state if state in DEMO_STATES else "step"
    i18n.set_lang(lang)
    settings, ents, st, running, vrc, job, upd = demo_inputs(state)
    dpi_aware()
    root = tk.Tk()
    from version import VERSION
    root.title("GSVR Companion %s" % VERSION)
    ui_theme.dark_title_bar(root)
    ui_theme.set_window_icon(root)
    if os.environ.get("GSVR_UI_DEMO_SCALING", "win") == "win":
        root.tk.call("tk", "scaling", 96.0 / 72.0)               # look like Windows at 100 % (fonts in pt, sizes via px())
    c = _DemoController(settings, st, running)

    def bg(fn):
        fn()                                                     # synchronous: everything is simulated
    fr = FirstRun(root, c, bg, on_advanced=lambda: None, on_lang=lambda code: i18n.set_lang(code), on_check=lambda: None,
                  tray_ok=lambda: True, detect_fn=lambda s: ents, vrc_fn=lambda s, conn: vrc)
    fr.demo_job = job
    fr.upd = upd
    ui_theme.apply_ttk(root)                                     # the help dialog (ttk) in the same dark style as in the app
    fr._open()
    root.geometry("+40+60")
    if shot:
        schedule_shot(root, shot, hold_ms)
    root.mainloop()
    return fr


def schedule_shot(root, shot, hold_ms=1400):
    """PNG of THIS window only (its own screen rectangle, raised + topmost), then close it. Never a full-screen capture."""
    def raise_then_grab():                                       # the main loop raises the window; root.update() here can spin forever
        root.lift()                                              # in the system Tk 8.5 of macOS (endless redraw), so no update() call
        root.attributes("-topmost", True)
        root.after(500, grab)

    def grab():
        try:
            root.update_idletasks()
            x, y, w, h = root.winfo_rootx(), root.winfo_rooty(), root.winfo_width(), root.winfo_height()
            if sys.platform == "darwin":
                import subprocess
                subprocess.run(["screencapture", "-x", "-o", "-R%d,%d,%d,%d" % (x, y, w, h), shot], check=False)
                srgb = "/System/Library/ColorSync/Profiles/sRGB Profile.icc"     # display profile -> sRGB (true Tk colours)
                if os.path.isfile(srgb):
                    subprocess.run(["sips", "--matchTo", srgb, shot, "--out", shot], check=False, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
            else:
                try:
                    from PIL import ImageGrab                    # only where Pillow is already installed (build machines)
                    ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(shot)
                except ImportError:
                    print("no screenshot tool on this system")
        finally:
            root.after(200, root.destroy)
    root.after(hold_ms, raise_then_grab)
