"""First-run screens (Tk) + the help dialog. Logic lives in firstrun.py (headless, tested). Mockups: docs/design/companion_first_run.md.

Screens (one frame, redrawn per phase): detect -> building -> done. "Advanced" leaves for the normal tabbed window.
Blocking work (detection, HTTP to the local companion) runs in threads; the Tk thread only reads self.q.
"""
import queue
import threading

import firstrun
import i18n
import worlds
from i18n import tr

DETECT_KEYS = ("hl_root", "rose_install", "war3_root", "war3_maps")
DETECT_EVERY = 60.0         # s between full game scans in the simple view (status still refreshes every 4 s)

GREEN, YELLOW, RED, GREY = "#1a9c3a", "#e0a800", "#c62828", "#8a8a8a"
LIGHTS = {"green": GREEN, "yellow": YELLOW, "red": RED, "grey": GREY}


def copy_to_clipboard(root, text):
    root.clipboard_clear()
    root.clipboard_append(text)


def help_dialog(root, help_key, title=None):
    """modal-ish window: the fix steps in the user's language + the Discord help channel (Open / Copy)"""
    import tkinter as tk
    from tkinter import ttk
    w = tk.Toplevel(root)
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


class FirstRun:
    def __init__(self, root, c, bg, on_advanced, on_lang):
        """c = gui.Controller, bg = run-in-thread helper, on_advanced() = leave for the tabbed window, on_lang(code) = language chosen"""
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.root, self.c, self.bg = root, c, bg
        self.on_advanced, self.on_lang = on_advanced, on_lang
        self.frame = ttk.Frame(root, padding=18)
        self.q = queue.Queue()
        self.entries = []
        self.checks = {}                 # game id -> BooleanVar
        self.job = None
        self.phase = "detect"
        self.searching = False
        self.msg = ""
        self.visible = False
        self._ticking = False
        self.home_rows = []
        self.home_busy = False
        self._home_t = 0.0
        self._det = None                 # (settings signature, time, entries): the drive scan is slow, the home view reuses it

    # ---------------------------------------------------------------- lifecycle
    def show(self):
        self.visible = True
        self.phase = "detect"
        self.job = None
        self.msg = ""
        self.frame.pack(fill="both", expand=True)
        self.rescan()
        if not self._ticking:
            self._ticking = True
            self._tick()

    def show_home(self):
        """simple view: setup already done -> 'All set!' with the live per-world status (no detect/build wizard)"""
        self.visible = True
        self.phase = "home"
        self.job = None
        self.msg = ""
        self.home_rows = []
        self._det = None                                       # fresh scan whenever the simple view opens
        self.frame.pack(fill="both", expand=True)
        self.draw()
        self.refresh_home()
        if not self._ticking:
            self._ticking = True
            self._tick()

    def refresh_home(self):
        if self.home_busy:
            return
        self.home_busy = True
        s = dict(self.c.settings)

        sig = tuple((s.get(k) or "").strip() for k in DETECT_KEYS)

        def work():
            import time
            try:
                d = self._det
                if d is None or d[0] != sig or time.time() - d[1] > DETECT_EVERY:
                    d = (sig, time.time(), firstrun.detect(s))     # full scan only on open, after a settings change, or once a minute
                    self._det = d
                ents = d[2]
                st = self.c.status() if self.c.running() else None
                rows = firstrun.world_rows(ents, st, self.c.running())
            except Exception:                                  # noqa: BLE001 - keep the last rows
                rows = None
            self.q.put(("home", rows))
        self.bg(work)

    def hide(self):
        self.visible = False
        self.frame.pack_forget()

    def rescan(self):
        if self.searching:
            return
        self.searching = True
        self.draw()
        s = dict(self.c.settings)
        # keep the folders the player picked this session (not yet saved)
        for e in self.entries:
            if e.get("picked") and e.get("found"):
                s["rose_install" if e["id"] == "rose" else "hl_root"] = e["root"]

        def work():
            ents = firstrun.detect(s)
            self.q.put(("detected", ents))
        self.bg(work)

    # ---------------------------------------------------------------- UI helpers
    def clear(self):
        for w in self.frame.winfo_children():
            w.destroy()

    def header(self):
        ttk, tk = self.ttk, self.tk
        top = ttk.Frame(self.frame)
        top.pack(fill="x")
        ttk.Label(top, text=tr("fr.title"), font=(self.font(), 20, "bold")).pack(side="left")
        lang_var = tk.StringVar(value=i18n.LANG_NAMES[i18n.lang()])
        cb = ttk.Combobox(top, textvariable=lang_var, values=[i18n.LANG_NAMES[l] for l in i18n.LANGS], state="readonly", width=10)
        cb.pack(side="right")
        ttk.Label(top, text=tr("fr.language") + ": ").pack(side="right")

        def chosen(_e=None):
            code = next((l for l in i18n.LANGS if i18n.LANG_NAMES[l] == lang_var.get()), "en")
            if code != i18n.lang():
                self.on_lang(code)
                self.draw()
        cb.bind("<<ComboboxSelected>>", chosen)

    def font(self):
        try:
            from tkinter import font as tkfont
            return tkfont.nametofont("TkDefaultFont").actual("family")
        except Exception:                                       # noqa: BLE001
            return "TkDefaultFont"

    def draw(self):
        if not self.visible:
            return
        self.clear()
        self.header()
        {"detect": self.draw_detect, "building": self.draw_building, "done": self.draw_done, "home": self.draw_home}[self.phase]()
        self.fit_window()

    def fit_window(self):
        """grow the window to the screen's usable height so the bottom buttons are never cut off (1080p at 100-150 % scaling)"""
        try:
            self.root.update_idletasks()
            need_h = self.frame.winfo_reqheight() + 40
            need_w = self.frame.winfo_reqwidth() + 40
            h = min(max(self.root.winfo_height(), need_h), max(420, self.root.winfo_screenheight() - 110))
            w = min(max(self.root.winfo_width(), need_w), max(620, self.root.winfo_screenwidth() - 40))
            self.root.geometry("%dx%d" % (w, h))
        except Exception:                                      # noqa: BLE001 - cosmetic only
            pass

    # ---------------------------------------------------------------- screen 1: detected games
    def draw_detect(self):
        ttk, tk = self.ttk, self.tk
        f = self.frame
        ttk.Label(f, text=tr("fr.intro"), wraplength=640, justify="left").pack(anchor="w", pady=(8, 2))
        ttk.Label(f, text=tr("fr.byog"), wraplength=640, justify="left", foreground="#555").pack(anchor="w")
        box = ttk.Frame(f)
        box.pack(fill="x", pady=14)
        if self.searching and not self.entries:
            ttk.Label(box, text=tr("fr.searching")).pack(anchor="w")
        for e in self.entries:
            row = ttk.Frame(box)
            row.pack(fill="x", pady=4, anchor="w")
            if e["found"]:
                var = self.checks.setdefault(e["id"], tk.BooleanVar(value=True))
                ttk.Checkbutton(row, text="%s   ✔" % e["name"], variable=var).pack(side="left")
                ttk.Label(row, text="   %s  (%s)" % (e["root"], tr("fr.src", how=firstrun.how_text(e["how"]))), foreground="#555",
                          wraplength=420).pack(side="left")
            else:
                ttk.Label(row, text="☐ %s   ✖  %s" % (e["name"], tr("fr.notfound")), foreground=RED).pack(side="left")
                ttk.Button(row, text=tr("fr.pick"), command=lambda g=e["id"]: self.pick(g)).pack(side="left", padx=(10, 0))
                ttk.Button(row, text=tr("w.help"), command=lambda g=e["id"]: help_dialog(self.root, firstrun.help_key_not_found(g))
                           ).pack(side="left", padx=4)
        if self.msg:
            mrow = ttk.Frame(f)
            mrow.pack(anchor="w", pady=(0, 8))
            ttk.Label(mrow, text=self.msg, foreground=RED, wraplength=540, justify="left").pack(side="left")
            ttk.Button(mrow, text=tr("w.help"), command=lambda: help_dialog(self.root, "help.error")).pack(side="left", padx=8)
        any_found = any(e["found"] for e in self.entries)
        if self.entries and not any_found:
            ttk.Label(f, text=tr("err.nogames"), foreground=RED, wraplength=640, justify="left").pack(anchor="w")
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(10, 0))
        start = self.tk.Button(bar, text=tr("fr.start"), font=(self.font(), 18, "bold"), fg="white", bg="#1a9c3a", activebackground="#157a2d",
                               padx=40, pady=8, command=self.start, state="normal" if any_found else "disabled")
        start.pack(side="left")
        ttk.Label(bar, text=tr("fr.start_hint"), foreground="#555", wraplength=300).pack(side="left", padx=12)
        ttk.Button(bar, text=tr("fr.advanced"), command=self.on_advanced).pack(side="right")
        ttk.Button(bar, text=tr("fr.rescan"), command=self.rescan).pack(side="right", padx=8)

    def pick(self, gid):
        from tkinter import filedialog
        d = filedialog.askdirectory(title=tr("fr.pick_title", game=firstrun.game_name(gid)), parent=self.root)
        if not d:
            return

        def work():
            ok, msg = firstrun.validate_folder(gid, d)
            self.q.put(("picked", (gid, ok, msg)))
        self.bg(work)

    def start(self):
        chosen = [e for e in self.entries if e["found"] and self.checks.get(e["id"]) is not None and self.checks[e["id"]].get()]
        if not chosen:
            self.msg = tr("fr.nothing_checked")
            self.draw()
            return
        self.msg = ""
        self.phase = "building"
        self.job = firstrun.FirstRunJob(self.c, chosen)
        self.draw()

        def work():
            self.job.begin()
            while self.job.phase not in ("done", "cancelled", "error"):
                self.job.step()
                self.q.put(("job", None))
                import time
                time.sleep(1.0)
            self.q.put(("job", None))
        self.bg(work)

    # ---------------------------------------------------------------- screen 2: progress
    def draw_building(self):
        ttk = self.ttk
        f = self.frame
        j = self.job
        self.lbl_title = ttk.Label(f, text=firstrun.title_key(j.phase) and tr(firstrun.title_key(j.phase)),
                                   font=(self.font(), 14))
        self.lbl_title.pack(anchor="w", pady=(16, 6))
        self._rows_sig = None
        self.bar = ttk.Progressbar(f, orient="horizontal", mode="determinate", maximum=100, length=560)
        self.bar.pack(anchor="w", fill="x")
        self.lbl_total = ttk.Label(f, text="")
        self.lbl_total.pack(anchor="w", pady=(6, 0))
        self.lbl_eta = ttk.Label(f, text="", foreground="#555")
        self.lbl_eta.pack(anchor="w")
        self.rows_box = ttk.Frame(f)
        self.rows_box.pack(fill="x", pady=10, anchor="w")
        self.lbl_err = ttk.Label(f, text="", foreground=RED, wraplength=640, justify="left")
        self.lbl_err.pack(anchor="w")
        row = ttk.Frame(f)
        row.pack(anchor="w", pady=10)
        ttk.Button(row, text=tr("fr.cancel"), command=self.cancel).pack(side="left")
        self.btn_help = ttk.Button(row, text=tr("w.help"), command=lambda: help_dialog(self.root, "help.error"))
        ttk.Button(row, text=tr("fr.background"), command=self.on_advanced).pack(side="left", padx=8)
        ttk.Label(f, text=tr("fr.background_hint"), foreground="#555", wraplength=640, justify="left").pack(anchor="w")
        self.refresh_job()

    def refresh_job(self):
        j = self.job
        if self.phase != "building" or j is None or not hasattr(self, "bar"):
            return
        try:
            self.bar.configure(value=j.pct)
        except self.tk.TclError:
            return
        self.lbl_title.configure(text=tr(firstrun.title_key(j.phase)))
        self.lbl_total.configure(text=tr("fr.total", pct=j.pct))
        eta = i18n.format_eta(j.eta)
        self.lbl_eta.configure(text=eta or tr("fr.estimating"))
        sig = [(g["id"], g["text"], g["can_open"], g["hint"]) for g in j.game_rows]
        if sig != self._rows_sig:                      # redraw rows only on change, so a button is not rebuilt under the mouse
            self._rows_sig = sig
            for w in self.rows_box.winfo_children():
                w.destroy()
            for g in j.game_rows:
                r = self.ttk.Frame(self.rows_box)
                r.pack(anchor="w", fill="x", pady=2)
                self.ttk.Label(r, text="• " + g["text"]).pack(side="left")
                if g["can_open"] and g["world"] in worlds.WORLDS:
                    url = worlds.launch_url(worlds.WORLDS[g["world"]][0])
                    self.ttk.Button(r, text=tr("fr.open_world_game", game=g["name"]),
                                    command=lambda u=url: open_world(self.root, u)).pack(side="left", padx=10)
                if g["hint"]:
                    self.ttk.Label(r, text=g["hint"], foreground="#555", wraplength=560, justify="left").pack(side="left")
        self.lbl_err.configure(text=j.error if j.phase != "error" else "")
        if j.phase == "error" or j.error:
            self.btn_help.pack(side="left", padx=8)

    def cancel(self):
        from tkinter import messagebox
        if self.job and messagebox.askyesno("GSVR", tr("fr.confirm_cancel"), parent=self.root):
            self.bg(self.job.cancel)                 # job.cancel() owns the phase change

    # ---------------------------------------------------------------- screen 3: done -> open worlds
    def draw_done(self):
        ttk = self.ttk
        f = self.frame
        j = self.job
        ttk.Label(f, text=tr("fr.done"), font=(self.font(), 16, "bold")).pack(anchor="w", pady=(16, 2))
        ttk.Label(f, text=tr("fr.done_sub")).pack(anchor="w")
        self.tk.Button(f, text=tr("fr.open_app"), font=(self.font(), 12, "bold"), padx=16, pady=4,
                       command=self.on_advanced).pack(anchor="w", pady=(8, 2))
        keys = list(j.worlds)
        if not keys:
            ttk.Label(f, text=tr("fr.no_world"), foreground="#555").pack(anchor="w", pady=10)
        for k in keys:
            wid, _n, _g = worlds.WORLDS[k]
            url = worlds.launch_url(wid)
            row = ttk.Frame(f)
            row.pack(anchor="w", fill="x", pady=8)
            label = tr("fr.open_world") if len(keys) == 1 else tr("fr.open_world_game", game=tr("w.name." + k))
            self.tk.Button(row, text=label, font=(self.font(), 15, "bold"), fg="white", bg="#2b6cb0", activebackground="#245a94",
                           padx=24, pady=8, command=lambda u=url: open_world(self.root, u)).pack(side="left")
            ttk.Button(row, text=tr("fr.copy_link"), command=lambda u=url: self.copy(u)).pack(side="left", padx=10)
            names = ", ".join(e["name"] for e in j.entries if e.get("world") == k)
            ttk.Label(f, text=tr("fr.world_info", state=tr("ws.ready"), games=names), foreground="#555",
                      wraplength=640, justify="left").pack(anchor="w", padx=4)
        self.lbl_copied = ttk.Label(f, text="", foreground=GREEN)
        self.lbl_copied.pack(anchor="w")
        ttk.Label(f, text=tr("fr.open_app_hint"), foreground="#555", wraplength=640, justify="left").pack(anchor="w", pady=(10, 0))
        ttk.Label(f, text=tr("fr.vrc_hint"), wraplength=640, justify="left", foreground="#555").pack(anchor="w", pady=(12, 0))
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=14)
        ttk.Button(bar, text=tr("fr.advanced"), command=self.on_advanced).pack(side="right")
        ttk.Button(bar, text=tr("fr.back"), command=self.show).pack(side="right", padx=8)

    def draw_home(self):
        ttk = self.ttk
        f = self.frame
        ttk.Label(f, text=tr("fr.done"), font=(self.font(), 16, "bold")).pack(anchor="w", pady=(16, 2))
        ttk.Label(f, text=tr("fr.done_sub")).pack(anchor="w")
        self.tk.Button(f, text=tr("fr.open_app"), font=(self.font(), 12, "bold"), padx=16, pady=4,
                       command=self.on_advanced).pack(anchor="w", pady=(8, 2))
        rows = self.home_rows
        if not rows:
            ttk.Label(f, text=tr("fr.searching"), foreground="#555").pack(anchor="w", pady=10)
        for r in rows:
            cell = ttk.Frame(f)
            cell.pack(anchor="w", fill="x", pady=6)
            self.tk.Label(cell, text="\u25cf", fg=LIGHTS[r["light"]], font=(self.font(), 20)).grid(row=0, column=0, rowspan=2, padx=(0, 8))
            ttk.Label(cell, text=r["name"], font=(self.font(), 11, "bold")).grid(row=0, column=1, sticky="w")
            ttk.Label(cell, text=r["state"]).grid(row=0, column=2, sticky="w", padx=12)
            bf = ttk.Frame(cell)
            bf.grid(row=0, column=3, sticky="e")
            if r["can_open"]:
                self.tk.Button(bf, text=tr("w.open"), fg="white", bg="#2b6cb0", activebackground="#245a94", padx=12,
                               command=lambda u=r["url"]: open_world(self.root, u)).pack(side="left")
                ttk.Button(bf, text=tr("fr.copy_link"), command=lambda u=r["url"]: self.copy(u)).pack(side="left", padx=4)
            if r["help"]:
                ttk.Button(bf, text=tr("w.help"), command=lambda k=r["help"]: help_dialog(self.root, k)).pack(side="left")
            ttk.Label(cell, text=r["hint"], foreground="#555" if r["light"] != "red" else "#a00", wraplength=600,
                      justify="left").grid(row=1, column=1, columnspan=3, sticky="w")
        self.lbl_copied = ttk.Label(f, text="", foreground=GREEN)
        self.lbl_copied.pack(anchor="w")
        ttk.Label(f, text=tr("fr.vrc_hint"), wraplength=640, justify="left", foreground="#555").pack(anchor="w", pady=(12, 0))
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=14)
        ttk.Button(bar, text=tr("fr.redo"), command=self.show).pack(side="left")

    def copy(self, url):
        copy_to_clipboard(self.root, url)
        self.lbl_copied.configure(text=tr("fr.copied"))

    # ---------------------------------------------------------------- event pump
    def _tick(self):
        if not self.visible:
            self._ticking = False
            return
        try:
            import time
            if self.phase == "home" and time.time() - self._home_t > 4:
                self._home_t = time.time()
                self.refresh_home()
            while True:
                kind, val = self.q.get_nowait()
                if kind == "home":
                    self.home_busy = False
                    if val is not None and self.phase == "home":
                        sig = [(r["key"], r["light"], r["state"], r["can_open"]) for r in val]
                        if sig != [(r["key"], r["light"], r["state"], r["can_open"]) for r in self.home_rows] or not self.home_rows:
                            self.home_rows = val
                            self.draw()
                elif kind == "detected":
                    self.searching = False
                    self.entries = val
                    for e in val:
                        if e["found"]:
                            self.checks.setdefault(e["id"], self.tk.BooleanVar(value=True))
                    if self.phase == "detect":
                        self.draw()
                elif kind == "picked":
                    gid, ok, msg = val
                    if ok:
                        for e in self.entries:
                            if e["id"] == gid:
                                e.update(found=True, root=msg, how="configured", picked=True)
                                self.checks[gid] = self.tk.BooleanVar(value=True)
                        self.msg = ""
                    else:
                        self.msg = msg
                    self.draw()
                elif kind == "job" and self.job is not None:
                    if self.job.phase == "done":
                        self.phase = "done"
                        self.c.settings.update(first_run_done=True)
                        try:
                            self.c.save_settings()
                        except OSError:
                            pass
                        self.draw()
                    elif self.job.phase == "cancelled":
                        self.phase, self.msg = "detect", tr("fr.cancelled")
                        self.draw()
                    elif self.job.phase == "error" and self.phase == "building":
                        self.phase = "detect"
                        self.msg = self.job.error
                        self.draw()
                    else:
                        self.refresh_job()
        except queue.Empty:
            pass
        self.root.after(150, self._tick)
