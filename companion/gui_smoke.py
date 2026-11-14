import sys, os, tkinter as tk
sys.path.insert(0, sys.argv[1])
os.environ.setdefault("GSVR_GUI_SMOKE", "1")
orig = tk.Tk.mainloop
checks = {}


def walk(w):
    yield w
    for ch in w.winfo_children():
        yield from walk(ch)


def short(self, n=0):
    def inspect():
        from tkinter import ttk
        nb = next(w for w in walk(self) if isinstance(w, ttk.Notebook))
        tabs = {nb.tab(t, "text"): self.nametowidget(t) for t in nb.tabs()}
        for name in ("Status", "Spiele", "Settings"):
            sub = list(walk(tabs[name]))
            checks[name + " scrollbar"] = any(isinstance(x, ttk.Scrollbar) for x in sub)
            checks[name + " canvas"] = any(isinstance(x, tk.Canvas) for x in sub)
        texts = [str(x.cget("text")) for x in walk(tabs["Spiele"]) if isinstance(x, ttk.Label)]
        checks["Spiele ROSE row"] = "ROSE Online" in texts
        cv = next(x for x in walk(tabs["Spiele"]) if isinstance(x, tk.Canvas))
        reg = [float(v) for v in str(cv.cget("scrollregion")).split()]
        checks["Spiele scrollregion set"] = len(reg) == 4 and reg[3] > 0
        print("Spiele canvas height %d, scrollregion %s" % (cv.winfo_height(), reg))
        ok = True                                           # 0.9.40 audit: Start selects the Status tab through inner.tab
        for t in tabs.values():
            for x in walk(t):
                if hasattr(x, "tab"):
                    try:
                        nb.select(x.tab)
                    except tk.TclError:
                        ok = False
        checks["scrollable tabs selectable via .tab"] = ok
        shot = os.environ.get("GSVR_SMOKE_SHOT")             # optional: screenshot of the Spiele tab at 1280x600
        if shot:
            ttk.Style().theme_use("clam")            # readable capture under macOS dark mode (Tk 8.5 draws black on dark)
            self.configure(background="#eeeeee")
            nb.select(tabs["Spiele"])
            self.geometry("1280x600+20+40")
            # capture later from the event loop (no nested update(): it processes the closing timers)
            self.after(1500, lambda: os.system("screencapture -x -R20,40,1280,600 '%s'" % shot))
        self.after(3000 if shot else 300, self.destroy)
    self.after(2500, inspect)
    self.after(9000, self.destroy)          # safety net; inspect() closes earlier
    orig(self, n)
tk.Tk.mainloop = short
tk.Misc.mainloop = short
import gui
gui.run_gui()
bad = [k for k, v in checks.items() if not v]
assert checks and not bad, "GUI smoke checks failed: %s (%s)" % (bad, checks)
print("GUI SMOKE OK", sorted(checks))
