"""Simulated inputs for the WHOLE companion window (gui.run_gui(sim=...)): screenshots of the Advanced view and the startup timing
harness (ui_timing.py). Nothing is read from game folders, nothing goes to the network, no tray icon, no settings file is written,
no companion process is started.

  GSVR_UI_DEMO=advanced [GSVR_UI_TAB=worlds|status|games|log|settings] [GSVR_LANG=ja] [GSVR_UI_SHOT=x.png]  python3 -I companion/ui_demo.py
"""
import queue
import threading
import time

import firstrun
import gui
import worlds

ROSE_ROOT = "C:\\Games\\ROSE Online"
HL_ROOT = "C:\\Program Files (x86)\\Steam\\steamapps\\common\\Half-Life"


def sim_games():
    """status.json "games" list as the companion sends it (Half-Life found via Steam, the others not)"""
    out = []
    for gid, name in (("valve", "Half-Life"), ("cstrike", "Counter-Strike"), ("gearbox", "Half-Life: Opposing Force"),
                      ("svencoop", "Sven Co-op"), ("war3", "Warcraft III")):
        found = gid == "valve"
        out.append({"id": gid, "name": name, "found": found, "root": HL_ROOT if found else "", "source": "steam-manifest" if found else "none",
                    "mods": ["valve"] if found else [], "hint": "" if found else "Not found"})
    return out


def sim_status():
    return {"app": "gsvr-companion", "companion": gui.VERSION, "state": "idle", "port": 27080, "world": worlds.WORLDS["rose"][0],
            "world_ok": True, "build": [], "rose_build": {"conv": True, "install": ROSE_ROOT, "install_ok": True,
                                                                              "packs": {"z1": {"state": "fresh"}}, "queue": []},
            "rose": {"all_ready": True}, "ownership": {"valve": True}, "games": sim_games(), "log": True, "log_ok": True,
            "map": "c1a0", "size": 18300000, "seconds": 2.1}


class SimTray:
    ok = True

    def __init__(self):
        self.events = queue.Queue()

    def start(self):
        return True

    def stop(self):
        pass


class SimController(gui.Controller):
    """gui.Controller without disk, network or processes; slow=True makes status/detection take as long as on a slow Windows PC"""

    def __init__(self, slow=False):                      # noqa: D107 - no super().__init__: it would read gui.json
        self.proc = None
        self.log_queue = queue.Queue()
        self.extra_args = []
        self.settings = dict(gui.DEFAULT_SETTINGS, first_run_done=True, rose_install="", view="advanced")   # stored 'advanced' is ignored
        self.active_port = 27080
        self.foreign = set()
        self._last_status, self._status_misses = None, 0
        self.fatal = None
        self.update_info = None
        self.update_error = None
        self._tail = []
        self._start_lock = threading.Lock()
        self.fresh_install = False
        self._official = []
        self._run = False
        self.slow = slow

    def save_settings(self):
        pass

    def running(self):
        return self._run

    def _start(self):
        if self.slow:
            time.sleep(0.4)                              # Popen + virus scan on Windows
        self._run = True
        self.log("[sim] companion started")
        return True

    def stop(self):
        self._run = False
        return True

    def status(self):
        if self.slow:
            time.sleep(0.25)
        return sim_status() if self._run else None

    def rose_state(self):
        return None

    def rose_post(self, path):
        return None

    def answer_ask(self, key, yes):
        return False

    def check_update(self, quiet=False):
        if self.slow:
            time.sleep(0.6)
        self.update_info = {"status": "current", "latest": gui.VERSION, "message": "GSVR %s is the newest version." % gui.VERSION}
        return self.update_info

    def install_update(self, root=None, kind=None, progress=None):
        return None

    def cache_dir(self):
        return "C:\\Users\\Player\\AppData\\Local\\GSVR\\cache"

    def games_view(self, st):
        return sim_games(), True


CPU_ONLY = False         # ui_timing.py --cpu: the scan never enters a syscall (worst case, holds the GIL except when preempted)


def _burn(seconds):
    """a directory walk as the scan threads do it: ~1 ms of pure-Python work per folder (sorting, name matching) + the listdir
    syscall (releases the GIL, ~0.3 ms) - so the harness also sees the main thread competing for the GIL"""
    end = time.perf_counter() + seconds
    n = 0
    while time.perf_counter() < end:
        t = time.perf_counter() + 0.001
        while time.perf_counter() < t:
            n += sum(i * i for i in range(200))
        if not CPU_ONLY:
            time.sleep(0.0003)
    return n


class Sim:
    def __init__(self, lang="en", view="advanced", tab="worlds", shot=None, slow=False, close_after_s=None, drag=False):
        self.lang, self.view, self.tab, self.shot = lang, view, tab, shot
        self.slow, self.close_after_s, self.drag = slow, close_after_s, drag
        self.controller = SimController(slow=slow)
        self.root = None

    # ---- inputs
    def detect(self, settings):
        if self.slow:                                    # registry + Steam libraries + drive scan of a big PC: 6 s, partly CPU
            for _ in range(12):
                _burn(0.25)
                time.sleep(0.25)
        names = {g: firstrun.game_name(g) for g in firstrun.ORDER}
        found = {"rose": ROSE_ROOT, "valve": HL_ROOT}
        return [{"id": g, "name": names[g], "found": g in found, "root": found.get(g, ""), "how": "steam" if g == "valve" else
                 "registry" if g in found else "none", "world": worlds.world_for_game(g), "picked": False} for g in firstrun.ORDER]

    def vrc(self, settings, connected):
        if self.slow:
            time.sleep(0.2)
        return "running"

    def rose_find(self, configured=None, with_reasons=True):
        if self.slow:
            _burn(1.0)
        return [(ROSE_ROOT, "registry", "ok")]

    def tray(self):
        return SimTray()

    # ---- window
    def attach(self, root):
        self.root = root
        try:
            root.tk.call("tk", "scaling", 96.0 / 72.0)   # look like Windows at 100 % (fonts in pt, sizes via px())
        except Exception:                                # noqa: BLE001
            pass

    def ready(self, leave_first_run, nb, tabs):
        root = self.root
        self.controller._run = True                      # as if the auto start already happened (screenshots)
        root.geometry("+40+60")
        if self.view == "advanced":
            def go():
                leave_first_run()
                t = tabs.get(self.tab)
                if t is not None:
                    try:
                        nb.select(getattr(t, "tab", t))
                    except Exception:                    # noqa: BLE001
                        pass
            root.after(300, go)
        if self.drag:                                    # the player drags the window around during the first seconds
            pos = {"i": 0}

            def move():
                pos["i"] += 1
                x = 40 + (pos["i"] * 7) % 300
                try:
                    root.geometry("+%d+%d" % (x, 60 + (pos["i"] * 3) % 80))
                    root.after(16, move)
                except Exception:                        # noqa: BLE001
                    pass
            root.after(50, move)
        if self.shot:
            import firstrun_ui
            firstrun_ui.schedule_shot(root, self.shot, hold_ms=1800)
        elif self.close_after_s:
            root.after(int(self.close_after_s * 1000), root.destroy)
