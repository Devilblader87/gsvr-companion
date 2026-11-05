"""GSVR companion — GUI (Tkinter, Python stdlib only). Also the GSVR.exe of the frozen build.

Start:  GSVR.exe (frozen build)  ·  pyw -3 companion\\gui.py  ·  double-click companion\\run-gui.bat

One window:
  top     big Start/Stop button, status lamp + one-line headline (running on port … / stopped / could not start),
          update banner ("Update available vX → Install") when the release feed has a newer version
  Status  companion version, port, games found, VRChat world, last map request, prefetch, weapon models, errors;
          if the companion can't start (exit code 3: no free port) its message is shown in red
  bars    (between headline and tabs, only while a converter runs) one progress bar per build job with percent + time left
          (status.json "build", companion/buildprog.py)
  Spiele  per game: folder, how it was found (Steam / Registry / Ordner-Suche / manuell), Aendern... / Zuruecksetzen
  Log     live companion output + GUI/update messages (Copy / Clear)
  Settings allowed world IDs, extra arguments, update server + optional read token, auto check, cache tools
Settings: %APPDATA%\\GSVR\\gui.json (macOS/Linux: ~/.config/gsvr/gui.json) — kept across updates.
Updates come from the Gitea release feed (updater.py); no git needed.

The logic lives in `Controller` (no Tkinter) so it can be tested headless: `python3 -I companion/gui.py --selftest`.
"""
import collections, json, os, queue, shutil, socket, subprocess, sys, threading, time, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)                                            # -I drops the script dir from sys.path
import companion, games, runtime, steam, updater                                  # noqa: E402  (PORT_CANDIDATES, APP, default cache)
from version import DEFAULT_UPDATE_BASE, REPO_NAME, REPO_OWNER, VERSION                    # noqa: E402

REPO = os.path.dirname(HERE)                                        # source checkout root (test data in work/)
DEFAULT_WORLDS = "wrld_5ab8566b-7e6a-4cfb-a0ac-b6d099b94ffb"
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0          # CREATE_NO_WINDOW
DEFAULT_SETTINGS = {"worlds": DEFAULT_WORLDS, "extra": "", "war3_root": "", "war3_maps": "", "hl_root": "",
                    "relay_url": "https://w3relay.heimserver.me", "map_autodownload": "on",
                    "update_base": DEFAULT_UPDATE_BASE, "update_token": "",
                    "auto_update_check": True, "prereleases": True, "auto_start": True}


def settings_path():
    if sys.platform == "win32":
        return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "GSVR", "gui.json")
    return os.path.expanduser("~/.config/gsvr/gui.json")


def cache_dir():
    return companion.default_cache_dir()                            # same default as the CLI


def ports_from_args(args):
    """Ports the companion may be on, given its argv: --port N → [N] first; --port-candidates replaces the list."""
    cands = list(companion.PORT_CANDIDATES)
    first = []
    for i, x in enumerate(args[:-1]):
        try:
            if x == "--port-candidates":
                cands = [int(v) for v in args[i + 1].split(",") if v.strip()]
            elif x == "--port" and args[i + 1] != "auto":
                first = [int(args[i + 1])]
        except ValueError:
            pass
    return first + [p for p in cands if p not in first]


python_cmd = runtime.console_python                                 # kept for older callers



def _is_old_lan_feed(url):
    """<= 0.4.x saved a private-LAN Gitea (http://<private IPv4>:3000) as the update server."""
    import ipaddress, urllib.parse
    try:
        u = urllib.parse.urlparse(url.rstrip("/"))
        return u.scheme == "http" and u.port == 3000 and ipaddress.ip_address(u.hostname or "").is_private
    except ValueError:
        return False

class Controller:
    """Start/stop/update/status logic. All output goes to `self.log_queue` as text lines."""

    def __init__(self, extra_args=None):
        self.proc = None
        self.log_queue = queue.Queue()
        self.extra_args = extra_args or []
        self.settings = dict(DEFAULT_SETTINGS)
        self.active_port = None          # port where the GSVR companion answered last
        self.foreign = set()             # candidate ports where ANOTHER program answers (not app=gsvr-companion)
        self.fatal = None                # companion's startup error (exit code 3), shown in red in the Status tab
        self.update_info = None          # last updater.check_for_update() result
        self.update_error = None
        self._tail = collections.deque(maxlen=40)
        self.load_settings()

    # ---- settings ----
    def load_settings(self):
        try:
            with open(settings_path(), encoding="utf-8") as fh:
                d = json.load(fh)
            if isinstance(d, dict):
                self.settings.update({k: v for k, v in d.items() if k in DEFAULT_SETTINGS})
                # <= 0.4.x saved the LAN Gitea feed as the update server: move to the public default (GitHub)
                if _is_old_lan_feed(str(self.settings.get("update_base", ""))):
                    self.settings["update_base"] = DEFAULT_UPDATE_BASE
        except (OSError, ValueError):
            pass

    def save_settings(self):
        p = settings_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.settings, fh, indent=1)
        os.replace(tmp, p)

    def log(self, line):
        self.log_queue.put(time.strftime("%H:%M:%S ") + line)

    # ---- companion process ----
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    # official GSVR worlds (one per mod), published next to the releases: a new world needs no companion update
    WORLDS_URL = "https://raw.githubusercontent.com/Devilblader87/gsvr-companion/main/worlds.txt"

    def official_worlds(self):
        """wrld_… ids from WORLDS_URL (one per line, # comments), cached for the session; [] when offline."""
        if getattr(self, "_official", None) is not None:
            return self._official
        ids = []
        try:
            ids = companion.fetch_world_list(self.WORLDS_URL)
            self.log("[gui] official worlds: %d (from %s)" % (len(ids), self.WORLDS_URL))
        except (OSError, ValueError) as e:
            self.log("[gui] official world list not reachable (%s) - using the settings only" % e)
        self._official = ids
        return ids

    def argv(self):
        a = runtime.companion_argv()
        worlds = self.settings.get("worlds", "").strip().replace(" ", "")
        extra_w = [w for w in self.official_worlds() if w not in worlds.split(",")]
        if extra_w:
            worlds = ",".join([w for w in worlds.split(",") if w] + extra_w)
        if worlds:
            a += ["--world-allow", worlds, "--world-list", self.WORLDS_URL]
        hl = self.settings.get("hl_root", "").strip()               # HL-DETECT: non-Steam / custom GoldSrc folder
        if hl:
            a += ["--game-root", hl]
        # Warcraft III: an own install folder (besides the registry / usual folders) and a fan-map folder
        for key, flag in (("war3_root", "--war3-root"), ("war3_maps", "--war3-maps")):
            v = self.settings.get(key, "").strip()
            if v:
                a += [flag, v]
        relay = self.settings.get("relay_url", "").strip()          # MAP-RELAY: empty = feature off
        if relay:
            a += ["--relay-url", relay]
            mode = self.settings.get("map_autodownload", "on")
            if mode in ("on", "ask", "off"):
                a += ["--map-autodownload", mode]
        extra = self.settings.get("extra", "").strip()
        if extra:
            a += extra.split()
        return a + self.extra_args

    def start(self):
        if self.running():
            self.log("[gui] companion already running (pid %d)" % self.proc.pid)
            return False
        argv = self.argv()
        self.fatal = None
        self._tail.clear()
        self.log("[gui] starting: " + " ".join(argv))
        try:
            self.proc = subprocess.Popen(argv, cwd=runtime.install_root(), stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
                                         encoding="utf-8", errors="replace", bufsize=1, creationflags=NO_WINDOW)
        except OSError as e:
            self.log("[gui] ERROR: could not start companion: %s" % e)
            self.proc = None
            return False
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()
        return True

    def _pump(self, proc):
        for line in proc.stdout:
            self.log_queue.put(line.rstrip("\n"))
            self._tail.append(line.rstrip("\n"))
        code = proc.wait()
        self.log("[gui] companion exited with code %s" % code)
        if code == companion.EXIT_NO_PORT:
            tail = list(self._tail)
            first = next((i for i, l in enumerate(tail) if "ERROR" in l), max(0, len(tail) - 10))
            msg = [l.split(" ", 1)[1] if l[:2].isdigit() and l[2:3] == ":" else l for l in tail[first:]]
            self.fatal = "\n".join(msg) or "companion could not start (exit code 3)"

    def stop(self):
        if not self.running():
            return False
        self.log("[gui] stopping companion (pid %d)" % self.proc.pid)
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        return True

    # ---- update (Gitea release feed, see updater.py) ----
    def _client(self):
        return updater.Client(self.settings.get("update_base") or DEFAULT_UPDATE_BASE,
                              self.settings.get("update_token") or None)

    def check_update(self, quiet=False):
        """→ updater result dict (status available/current/no-asset/none) or None on error (logged)."""
        try:
            r = updater.check_for_update(prereleases=bool(self.settings.get("prereleases", True)), client=self._client())
        except updater.UpdateError as e:
            self.update_info, self.update_error = None, str(e)
            self.log("[update] check failed: %s" % e)
            return None
        self.update_info, self.update_error = r, None
        if not quiet or r["status"] != "current":
            self.log("[update] " + r["message"])
        return r

    def install_update(self, root=None, kind=None, progress=None):
        """Stop the companion, download + verify + swap. → relaunch argv, or None on error (logged)."""
        info = self.update_info
        if not info or info.get("status") != "available":
            self.log("[update] nothing to install")
            return None
        was_running = self.running()
        self.stop()
        try:
            argv = updater.install(info, root=root, kind=kind, client=self._client(),
                                   log=self.log, progress=progress)
        except updater.UpdateError as e:
            self.update_error = str(e)
            self.log("[update] ERROR: %s" % e)
            if was_running:
                self.start()
            return None
        self.log("[update] installed %s — restarting" % info["latest"])
        return argv

    # ---- GAME-FOLDERS ----
    @staticmethod
    def game_setting(game_id):
        """Setting behind a game's folder: war3_root for Warcraft III, hl_root (one GoldSrc folder) for everything else."""
        return "war3_root" if game_id == "war3" else "hl_root"

    def set_game_folder(self, game_id, path):
        key = self.game_setting(game_id)
        self.settings[key] = (path or "").strip()
        self._games_cache = None
        try:
            self.save_settings()
        except OSError as e:
            self.log("[gui] could not save settings: %s" % e)
        return key

    def reset_game_folder(self, game_id):
        return self.set_game_folder(game_id, "")

    def games_view(self, st):
        """-> (games, live). The running companion's list (status.json "games"); otherwise the same detection run
        locally (cached 20 s per settings)."""
        if st and st.get("games"):
            return st["games"], True
        key = (self.settings.get("hl_root", ""), self.settings.get("war3_root", ""), self.settings.get("war3_maps", ""))
        cache = getattr(self, "_games_cache", None)
        if not cache or cache[0] != key or time.monotonic() - cache[1] > 20:
            try:
                g = games.local_view(key[0].strip() or None, key[1].strip() or None, key[2].strip() or None)
            except Exception as e:                                  # noqa: BLE001 - never break the GUI poll
                self.log("[gui] local game detection failed: %s" % e)
                g = []
            self._games_cache = cache = (key, time.monotonic(), g)
        return cache[2], False

    # ---- status ----
    def ports(self):
        return ports_from_args(self.argv()[1:])

    @staticmethod
    def fetch_status(port, timeout=1.0):
        """→ ("gsvr", dict) | ("foreign", None) | (None, None) = nothing listening."""
        try:            # connect first (Windows needs ~2 s to report "refused" on loopback): no listener → None
            socket.create_connection(("127.0.0.1", port), timeout=2.5).close()
        except OSError:
            return None, None
        try:
            req = urllib.request.Request("http://127.0.0.1:%d/status.json" % port, headers={"Host": "127.0.0.1"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read(1 << 20).decode("utf-8"))
        except urllib.error.HTTPError:
            return "foreign", None
        except (OSError, ValueError):
            return "foreign", None
        if isinstance(d, dict) and d.get("app") == companion.APP:
            return "gsvr", d
        return "foreign", None

    def status(self):
        """status.json of the GSVR companion (tries the candidate ports, last good one first) or None.
        Only a status with app == "gsvr-companion" is accepted; other answers mark that port as foreign."""
        ports = self.ports()
        if self.active_port in ports:
            ports.remove(self.active_port)
            ports.insert(0, self.active_port)
        for p in ports:
            kind, d = self.fetch_status(p)
            if kind == "gsvr":
                self.active_port = p
                self.foreign.discard(p)
                return d
            if kind == "foreign":
                self.foreign.add(p)
            else:
                self.foreign.discard(p)
        self.active_port = None
        return None

    def answer_ask(self, key, yes):
        """MAP-RELAY ask mode: tell the companion the player's decision (POST /ask, custom header = no CSRF from a page)."""
        if self.active_port is None:
            return False
        try:
            req = urllib.request.Request("http://127.0.0.1:%d/ask?sha=%s&answer=%s" % (self.active_port, key, "yes" if yes else "no"),
                                         data=b"", method="POST", headers={"Host": "127.0.0.1", "X-GSVR-Ask": "1"})
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status == 200
        except OSError:
            return False

    def clear_cache(self):
        d = cache_dir()
        n = 0
        if os.path.isdir(d):
            for f in os.listdir(d):
                p = os.path.join(d, f)
                if os.path.isfile(p):
                    os.remove(p)
                    n += 1
        self.log("[gui] cache cleared (%d files) in %s" % (n, d))
        return n


def _mb(n):
    return ("%.1f MB" % (n / 1e6)) if n else ""


def _slot(d, name_key="map"):
    if not isinstance(d, dict) or d.get("state") in (None, "idle"):
        return "-"
    s = "%s — %s" % (d.get(name_key) or "?", d.get("state"))
    if d.get("state") == "ready":
        s += "  %s%s" % (_mb(d.get("size", 0)), ("  (%.2f s)" % d["seconds"]) if d.get("seconds") is not None else "")
    if d.get("error"):
        s += "  " + str(d["error"])
    return s


KIND_DE = {"map": "Karte", "models": "Modelle", "hologram": "Hologramm", "war3": "Karte (WC3)"}


def fmt_eta(eta_s):
    """seconds left (-1 / None = unknown) -> "noch ca. 1 Min" / "noch 40 s" / ""."""
    if eta_s is None or eta_s < 0:
        return ""
    if eta_s < 60:
        return "noch %d s" % max(1, int(eta_s))
    return "noch ca. %d Min" % int((eta_s + 30) // 60)


def build_rows(st):
    """status.json "build" -> [(label, pct 0-100, eta text)] for the progress bars (empty list = idle / old companion)."""
    out = []
    for j in (st or {}).get("build") or ():
        if not isinstance(j, dict):
            continue
        what = KIND_DE.get(j.get("kind"), str(j.get("kind") or "?"))
        who = j.get("name") if j.get("kind") in ("map", "war3") else j.get("mod")
        try:
            pct = max(0, min(100, int(j.get("pct", 0))))
        except (TypeError, ValueError):
            pct = 0
        eta = j.get("eta_s")
        out.append(("%s: %s" % (what, who or "?"), pct, fmt_eta(eta if isinstance(eta, (int, float)) else None)))
    return out


def headline(st, running, fatal):
    """→ (lamp colour, one-line text) for the top of the window."""
    if running and st:
        return "green", "Running — 127.0.0.1:%s" % st.get("port", "?")
    if running:
        return "orange", "Starting…"
    if fatal:
        return "red", "Could not start — see the Status tab"
    return "grey", "Stopped"


SOURCE_DE = {"steam-manifest": "Steam", "registry": "Registry", "install-folder": "Standardordner",
             "non-steam-scan": "Ordner-Suche (unbestaetigt)", "manual": "manuell", "none": "-"}


def game_rows(gs):
    """games list (status.json "games") -> [(game_id, name, folder, source, found)] for the Spiele table."""
    rows = []
    for g in gs:
        if g.get("found"):
            folder = g.get("root", "")
            if g.get("version"):
                folder += "   (Version %s)" % g["version"]
            if g.get("maps_root"):
                folder += "   Maps: %s" % g["maps_root"]
        elif g.get("root"):
            folder = "%s - %s" % (g["root"], g.get("hint", ""))
        else:
            folder = "nicht gefunden - " + (g.get("hint") or "")
        rows.append((g.get("id", ""), g.get("name", "?"), folder, SOURCE_DE.get(g.get("source"), g.get("source", "-")),
                     bool(g.get("found"))))
    return rows


def status_rows(st, foreign=()):
    """status.json → [(label, value)] for the Status tab (foreign = candidate ports used by other programs)."""
    notes = [("Note", "Port %d: used by another program (not the GSVR companion) — skipped" % p) for p in sorted(foreign)]
    if st is None:
        return [("Companion", "not reachable on 127.0.0.1 (ports %s)" % ", ".join(map(str, companion.PORT_CANDIDATES)))] + notes
    own = st.get("ownership", {})
    owned = [m for m, ok in sorted(own.items()) if ok]
    rows = [("Companion", "%s — %s" % (st.get("companion", "?"), st.get("state"))),
            ("Port", "127.0.0.1:%s" % st.get("port", "?")),
            ("Games found", ", ".join(owned) if owned else "none (is Steam + the game installed?)"),
            ("VRChat world", "%s %s" % (st.get("world") or "(not in a world yet)",
                                        ("✔ allowed" if st.get("world_ok") else "✖ not allowed") if st.get("world") else "")),
            ("Last map request", _slot({"map": st.get("map"), "state": st.get("state"), "size": st.get("size"),
                                        "seconds": st.get("seconds"), "error": st.get("error")})
             + (("   req %s" % st["req"]) if st.get("req") else "")),
            ("Prefetch", _slot(st.get("next"))),
            ("Weapon models", _slot(st.get("models"), "mod")),
            ("Map holograms", _slot(st.get("holo"), "mod"))]
    if st.get("log") is False:
        rows.append(("Note", "no VRChat log folder found — map requests from the world can't arrive"))
    return rows + notes


def describe(st, foreign=()):
    """Plain-text lines of status_rows (log / selftest)."""
    return ["%s: %s" % r for r in status_rows(st, foreign)]


# ===================================================================================================== GUI
def run_gui():
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog

    c = Controller()
    threading.Thread(target=lambda: updater.cleanup_old(retries=20), daemon=True).start()   # *.gsvr-old of the last update
    root = tk.Tk()
    root.title("GSVR Companion %s" % VERSION)
    root.geometry("780x520")
    root.minsize(620, 420)
    ui_q = queue.Queue()                 # Tk may only be touched from the main thread → background results go here

    def bg(fn):                          # run blocking actions off the UI thread
        threading.Thread(target=fn, daemon=True).start()

    # ---- top: big Start/Stop + headline ----
    top = ttk.Frame(root, padding=(10, 10, 10, 4))
    top.pack(fill="x")
    btn_toggle = tk.Button(top, text="Start", width=9, font=("Segoe UI", 16, "bold"))
    btn_toggle.pack(side="left")
    lamp = tk.Label(top, text="●", fg="grey", font=("Segoe UI", 22))
    lamp.pack(side="left", padx=(14, 4))
    head_lbl = tk.Label(top, text="Stopped", font=("Segoe UI", 13), anchor="w")
    head_lbl.pack(side="left", fill="x", expand=True)

    # ---- update banner (hidden until an update is found) ----
    banner = tk.Frame(root, background="#fff4c2", padx=10, pady=6)
    banner_lbl = tk.Label(banner, text="", background="#fff4c2", anchor="w", justify="left")
    banner_lbl.pack(side="left", fill="x", expand=True)
    btn_install = ttk.Button(banner, text="Install")
    btn_install.pack(side="right")

    # ---- build progress (hidden while idle): one bar per running converter job (status.json "build") ----
    prog_frame = ttk.Frame(root, padding=(10, 2, 10, 2))
    prog_widgets = []                                        # (label, bar, pct/eta label)

    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True, padx=8, pady=(4, 8))

    def show_build(rows):
        while len(prog_widgets) < len(rows):
            lb = ttk.Label(prog_frame, width=34, anchor="w")
            bar = ttk.Progressbar(prog_frame, orient="horizontal", mode="determinate", maximum=100, length=260)
            tx = ttk.Label(prog_frame, width=24, anchor="w")
            i = len(prog_widgets)
            lb.grid(row=i, column=0, sticky="w")
            bar.grid(row=i, column=1, sticky="we", padx=8, pady=1)
            tx.grid(row=i, column=2, sticky="w")
            prog_widgets.append((lb, bar, tx))
        prog_frame.columnconfigure(1, weight=1)
        for i, (lb, bar, tx) in enumerate(prog_widgets):
            if i < len(rows):
                lb.configure(text=rows[i][0])
                bar.configure(value=rows[i][1])
                tx.configure(text=("%d %%  %s" % (rows[i][1], rows[i][2])).strip())
                lb.grid()
                bar.grid()
                tx.grid()
            else:
                lb.grid_remove()
                bar.grid_remove()
                tx.grid_remove()
        if rows:
            if not prog_frame.winfo_ismapped():
                prog_frame.pack(fill="x", padx=8, before=nb)
        else:
            prog_frame.pack_forget()

    # ---- Status tab ----
    st_tab = ttk.Frame(nb, padding=10)
    nb.add(st_tab, text="Status")
    fatal_lbl = tk.Label(st_tab, text="", fg="#c00000", justify="left", anchor="w", font=("Consolas", 9, "bold"))
    fatal_lbl.grid(row=0, column=0, columnspan=2, sticky="we")
    rows_frame = ttk.Frame(st_tab)
    rows_frame.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(6, 0))
    row_widgets = []
    ttk.Label(st_tab, text="VRChat: Settings → \"Allow Untrusted URLs\" must be ON.  Then join the GSVR world.",
              foreground="#555").grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))
    st_upd = ttk.Frame(st_tab)                               # always visible (the Settings tab can be taller than the window)
    st_upd.grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
    ttk.Label(st_upd, text="Version %s" % VERSION, foreground="#555").pack(side="left", padx=(0, 10))
    st_btn_check = ttk.Button(st_upd, text="Nach Updates suchen", command=lambda: check_now())
    st_btn_check.pack(side="left")
    st_tab.columnconfigure(0, weight=1)
    st_tab.rowconfigure(1, weight=1)

    def show_rows(rows):
        while len(row_widgets) < len(rows):
            i = len(row_widgets)
            k = ttk.Label(rows_frame, font=("Segoe UI", 10, "bold"))
            v = ttk.Label(rows_frame, wraplength=560, justify="left")
            k.grid(row=i, column=0, sticky="nw", padx=(0, 12), pady=2)
            v.grid(row=i, column=1, sticky="nw", pady=2)
            row_widgets.append((k, v))
        for i, (k, v) in enumerate(row_widgets):
            if i < len(rows):
                k.configure(text=rows[i][0])
                v.configure(text=rows[i][1])
            else:
                k.configure(text="")
                v.configure(text="")

    # ---- Spiele tab (GAME-FOLDERS): game | folder | source | Aendern... / Zuruecksetzen ----
    gm_tab = ttk.Frame(nb, padding=8)
    nb.add(gm_tab, text="Spiele")                                     # 2nd tab (Status first); insert(1, …) needs an existing tab 1
    gm_frame = ttk.Frame(gm_tab)
    gm_frame.grid(row=0, column=0, sticky="nsew")
    gm_note = ttk.Label(gm_tab, text="", foreground="#555", wraplength=700, justify="left")
    gm_note.grid(row=1, column=0, sticky="w", pady=(8, 0))
    ttk.Label(gm_tab, text="Nur mit eigener, legal erworbener Kopie. Ohne Steam kann der Besitz nicht gepr\u00fcft werden. "
              "Ein manueller Half-Life-Ordner gilt f\u00fcr alle GoldSrc-Spiele (Half-Life, Counter-Strike, Sven Co-op ...).",
              foreground="#555", wraplength=700, justify="left").grid(row=2, column=0, sticky="w", pady=(4, 0))
    gm_tab.columnconfigure(0, weight=1)
    gm_state = {"rows": None}

    def game_changed(gid, path):
        key = c.set_game_folder(gid, path)
        (war3_var if key == "war3_root" else hl_var).set(path or "")
        gm_state["rows"] = None
        gm_note.configure(text="Gespeichert. " + ("Companion neu starten (Stop / Start), damit der Ordner gilt." if c.running()
                                                  else "Gilt beim n\u00e4chsten Start."))
        bg(lambda: ui_q.put(("games", c.games_view(c.status()))))

    def game_change(gid):
        cur = c.settings.get(c.game_setting(gid), "")
        d = filedialog.askdirectory(initialdir=cur or None, title="Ordner w\u00e4hlen")
        if d:
            game_changed(gid, d)

    def show_games(val):
        gs, live = val
        rows = game_rows(gs)
        if rows == gm_state["rows"]:
            return
        gm_state["rows"] = rows
        for w in gm_frame.winfo_children():
            w.destroy()
        for i, h in enumerate(("Spiel", "Ordner", "Quelle", "")):
            ttk.Label(gm_frame, text=h, font=("Segoe UI", 10, "bold")).grid(row=0, column=i, sticky="w", padx=(0, 10))
        for i, (gid, name, folder, src, found) in enumerate(rows, 1):
            ttk.Label(gm_frame, text=name).grid(row=i, column=0, sticky="nw", padx=(0, 10), pady=2)
            ttk.Label(gm_frame, text=folder, wraplength=330, justify="left",
                      foreground="" if found else "#a00").grid(row=i, column=1, sticky="nw", padx=(0, 10), pady=2)
            ttk.Label(gm_frame, text=src).grid(row=i, column=2, sticky="nw", padx=(0, 10), pady=2)
            bf = ttk.Frame(gm_frame)
            bf.grid(row=i, column=3, sticky="ne", pady=2)
            ttk.Button(bf, text="\u00c4ndern\u2026", command=lambda g=gid: game_change(g)).pack(side="left")
            ttk.Button(bf, text="Zur\u00fccksetzen (automatisch)", command=lambda g=gid: game_changed(g, "")).pack(side="left", padx=(4, 0))
        gm_frame.columnconfigure(1, weight=1)

    # ---- Log tab ----
    log_tab = ttk.Frame(nb, padding=4)
    nb.add(log_tab, text="Log")
    log_txt = tk.Text(log_tab, wrap="none", font=("Consolas", 9))
    ys = ttk.Scrollbar(log_tab, orient="vertical", command=log_txt.yview)
    log_txt.configure(yscrollcommand=ys.set)
    log_txt.grid(row=0, column=0, sticky="nsew")
    ys.grid(row=0, column=1, sticky="ns")
    lb = ttk.Frame(log_tab)
    lb.grid(row=1, column=0, sticky="w", pady=(4, 0))
    log_tab.columnconfigure(0, weight=1)
    log_tab.rowconfigure(0, weight=1)

    def copy_log():
        root.clipboard_clear()
        root.clipboard_append(log_txt.get("1.0", "end"))
    ttk.Button(lb, text="Copy", command=copy_log).pack(side="left", padx=3)
    ttk.Button(lb, text="Clear", command=lambda: log_txt.delete("1.0", "end")).pack(side="left", padx=3)

    # ---- Settings tab ----
    se = ttk.Frame(nb, padding=10)
    nb.add(se, text="Settings")
    worlds_var = tk.StringVar(value=c.settings.get("worlds", ""))
    extra_var = tk.StringVar(value=c.settings.get("extra", ""))
    base_var = tk.StringVar(value=c.settings.get("update_base", DEFAULT_UPDATE_BASE))
    token_var = tk.StringVar(value=c.settings.get("update_token", ""))
    auto_var = tk.BooleanVar(value=bool(c.settings.get("auto_update_check", True)))
    pre_var = tk.BooleanVar(value=bool(c.settings.get("prereleases", True)))
    war3_var = tk.StringVar(value=c.settings.get("war3_root", ""))
    w3maps_var = tk.StringVar(value=c.settings.get("war3_maps", ""))
    hl_var = tk.StringVar(value=c.settings.get("hl_root", ""))
    # GAME-FOLDERS (maintainer 2026-10-29): folder settings live only on the Spiele tab, not on Settings
    gx = ttk.LabelFrame(gm_tab, text="Ordner von Hand / suchen", padding=8)
    gx.grid(row=3, column=0, sticky="we", pady=(10, 0))
    gx.columnconfigure(0, weight=1)
    r = 0
    # Half-Life / Counter-Strike folder (HL-DETECT): empty = Steam detection; set = used as-is (no ownership check)
    ttk.Label(gx, text="Half-Life / Counter-Strike Ordner (leer = Steam wird automatisch gefunden):"
              ).grid(row=r, column=0, columnspan=3, sticky="w")
    ttk.Entry(gx, textvariable=hl_var, width=70).grid(row=r + 1, column=0, columnspan=2, sticky="we")
    ttk.Button(gx, text="Browse…", command=lambda: hl_var.set(filedialog.askdirectory(initialdir=hl_var.get() or None) or hl_var.get())
               ).grid(row=r + 1, column=2, sticky="w", padx=(6, 0))
    hl_lbl = ttk.Label(gx, text="",
                       foreground="#555", wraplength=640, justify="left")
    hl_lbl.grid(row=r + 2, column=0, columnspan=2, sticky="w")

    def hl_validate(*_):
        v = hl_var.get().strip()
        if v:
            d = steam.describe_root(v)
            hl_lbl.configure(text=("OK - " if d["valid"] else "Problem - ") + d["reason"])

    def hl_search():
        hl_lbl.configure(text="Suche l\u00e4uft ...")

        def work():
            cands = steam.detect_non_steam(force=True)
            ui_q.put(("hl_found", cands))
        bg(work)
    ttk.Button(gx, text="Suchen", command=hl_search).grid(row=r + 2, column=2, sticky="w", padx=(6, 0))
    hl_list = tk.Listbox(gx, height=3, width=70)
    hl_list.grid(row=r + 3, column=0, columnspan=2, sticky="we")
    hl_items = []

    def hl_pick(_e=None):
        sel = hl_list.curselection()
        if sel and sel[0] < len(hl_items):
            hl_var.set(hl_items[sel[0]]["root"])
            hl_validate()
    hl_list.bind("<<ListboxSelect>>", hl_pick)
    ttk.Button(gx, text="Pr\u00fcfen", command=hl_validate).grid(row=r + 3, column=2, sticky="nw", padx=(6, 0))
    r += 4
    # Warcraft III folders (maintainer 2026-10-22: the companion finds the install or lets the player pick it)
    for label, var in (("Warcraft III folder (empty = found automatically: registry, usual folders):", war3_var),
                       ("Warcraft III fan-map folder (optional: maps you downloaded yourself):", w3maps_var)):
        ttk.Label(gx, text=label).grid(row=r, column=0, columnspan=3, sticky="w", pady=(8 if r else 0, 0))
        ttk.Entry(gx, textvariable=var, width=70).grid(row=r + 1, column=0, columnspan=2, sticky="we")
        ttk.Button(gx, text="Browse…", command=lambda v=var: v.set(filedialog.askdirectory(initialdir=v.get() or None) or v.get())
                   ).grid(row=r + 1, column=2, sticky="w", padx=(6, 0))
        r += 2
    w3_lbl = ttk.Label(gx, text="", foreground="#555", wraplength=640, justify="left")
    w3_lbl.grid(row=r, column=0, columnspan=2, sticky="w")

    def war3_detect():
        import war3
        e = war3.ownership(war3_var.get().strip() or None)["war3"]
        w3_lbl.configure(text=("found: " + e["reason"]) if e["allowed"] else ("not found: " + e["reason"]))
    ttk.Button(gx, text="Detect", command=war3_detect).grid(row=r, column=2, sticky="w", padx=(6, 0))
    r += 1
    r = 0
    relay_var = tk.StringVar(value=c.settings.get("relay_url", ""))
    dl_var = tk.StringVar(value=c.settings.get("map_autodownload", "on"))
    ttk.Label(se, text="Map relay URL (https://..., empty = off): share the maps you host, fetch maps the host shared:"
              ).grid(row=r, column=0, columnspan=3, sticky="w", pady=(8, 0))
    ttk.Entry(se, textvariable=relay_var, width=80).grid(row=r + 1, column=0, columnspan=3, sticky="we")
    dlf = ttk.Frame(se)
    dlf.grid(row=r + 2, column=0, columnspan=3, sticky="w")
    ttk.Label(dlf, text="Download missing maps:").pack(side="left")
    ttk.Combobox(dlf, textvariable=dl_var, values=("on", "ask", "off"), width=6, state="readonly").pack(side="left", padx=6)
    r += 3
    # maintainer 2026-10-29: no world-ID row any more - the official worlds come from worlds.txt on GitHub (WORLDS_URL);
    # a value already in gui.json still counts (worlds_var is saved unchanged)
    for label, var, kw in (("Extra companion arguments (advanced, e.g. --game-root \"D:\\Games\\Half-Life\"):", extra_var, {}),
                           ("Update server (releases of %s/%s):" % (REPO_OWNER, REPO_NAME), base_var, {}),
                           ("Read token (only if the repository is private; never logged):", token_var, {"show": "•"})):
        ttk.Label(se, text=label).grid(row=r, column=0, columnspan=3, sticky="w", pady=(8 if r else 0, 0))
        ttk.Entry(se, textvariable=var, width=80, **kw).grid(row=r + 1, column=0, columnspan=3, sticky="we")
        r += 2
    ttk.Checkbutton(se, text="Check for updates when the app starts", variable=auto_var).grid(row=r, column=0, columnspan=3, sticky="w", pady=(8, 0))
    ttk.Checkbutton(se, text="Include pre-releases (all v0.x releases are pre-releases)", variable=pre_var).grid(row=r + 1, column=0, columnspan=3, sticky="w")
    sb = ttk.Frame(se)
    sb.grid(row=r + 2, column=0, columnspan=3, sticky="w", pady=(10, 0))
    btn_check = ttk.Button(sb, text="Check for updates now")
    btn_cache = ttk.Button(sb, text="Open cache folder")
    btn_clear = ttk.Button(sb, text="Clear cache")
    for b in (btn_check, btn_cache, btn_clear):
        b.pack(side="left", padx=(0, 6))
    upd_lbl = ttk.Label(se, text="", foreground="#555", wraplength=640, justify="left")
    upd_lbl.grid(row=r + 3, column=0, columnspan=3, sticky="w", pady=(8, 0))
    ttk.Label(se, text="Version %s · %s install (%s) · %s" % (VERSION, runtime.install_kind(), updater.platform_key(),
                                                              runtime.install_root()),
              foreground="#555").grid(row=r + 4, column=0, columnspan=3, sticky="w", pady=(12, 0))
    se.columnconfigure(0, weight=1)

    def remember():
        c.settings.update(worlds=worlds_var.get(), extra=extra_var.get(), war3_root=war3_var.get().strip(), hl_root=hl_var.get().strip(),
                          war3_maps=w3maps_var.get().strip(), relay_url=relay_var.get().strip(),
                          map_autodownload=dl_var.get() if dl_var.get() in ("on", "ask", "off") else "on", update_base=base_var.get().strip(),
                          update_token=token_var.get().strip(), auto_update_check=bool(auto_var.get()),
                          prereleases=bool(pre_var.get()))
        try:
            c.save_settings()
        except OSError as e:
            c.log("[gui] could not save settings: %s" % e)

    # ---- actions ----
    installing = threading.Event()

    def toggle():
        if c.running():
            btn_toggle.configure(state="disabled", text="Stopping…")
            bg(lambda: (c.stop(), ui_q.put(("refresh", None))))
            return
        remember()
        c.start()
        nb.select(st_tab)
        refresh_buttons()
    btn_toggle.configure(command=toggle)

    def refresh_buttons():
        run = c.running()
        btn_toggle.configure(state="disabled" if installing.is_set() else "normal", text="Stop" if run else "Start",
                             fg="#a00000" if run else "#006000")

    def check_now(quiet=False):
        remember()
        btn_check.configure(state="disabled")
        upd_lbl.configure(text="checking %s …" % base_var.get())
        bg(lambda: ui_q.put(("update", c.check_update(quiet=quiet))))
    btn_check.configure(command=check_now)

    def install():
        info = c.update_info or {}
        if not messagebox.askyesno("Install update",
                                   "Install %s now?\n\nThe companion is stopped, the files are replaced and the app "
                                   "restarts. Settings and the map cache are kept." % info.get("latest")):
            return
        remember()
        installing.set()
        btn_install.configure(state="disabled")
        btn_toggle.configure(state="disabled")
        banner_lbl.configure(text="Installing %s …" % info.get("latest"))

        def prog(n, total):
            ui_q.put(("progress", "Downloading %s … %s%s" % (info.get("latest"), _mb(n) or "0 MB",
                                                             (" of " + _mb(total)) if total else "")))
        bg(lambda: ui_q.put(("installed", c.install_update(progress=prog))))
    btn_install.configure(command=install)

    def open_cache():
        d = cache_dir()
        os.makedirs(d, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(d)                                             # noqa: S606 (local folder)
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", d])
    btn_cache.configure(command=open_cache)

    def clear_cache():
        if messagebox.askyesno("Clear cache", "Delete all converted map/model packs? They are rebuilt on demand."):
            c.clear_cache()
    btn_clear.configure(command=clear_cache)

    # ---- periodic work ----
    def pump_logs():
        n = 0
        at_end = log_txt.yview()[1] >= 0.999                        # follow the log only while the view is at its end
        while n < 500:
            try:
                line = c.log_queue.get_nowait()
            except queue.Empty:
                break
            log_txt.insert("end", line + "\n")
            n += 1
        if n and at_end:
            log_txt.see("end")
        if n and int(log_txt.index("end-1c").split(".")[0]) > 5000:    # keep the widget light
            log_txt.delete("1.0", "1000.0")
        root.after(200, pump_logs)

    polling = threading.Event()          # at most one status poll in flight (each port may take ~2 s on Windows)

    def poll_once():
        try:
            st = c.status()
            ui_q.put(("status", st))
            ui_q.put(("games", c.games_view(st)))
        finally:
            polling.clear()

    def poll_status():
        if not polling.is_set():
            polling.set()
            bg(poll_once)
        root.after(2000, poll_status)

    def show_update(info):
        btn_check.configure(state="normal")
        if info is None:
            upd_lbl.configure(text="Update check failed: %s" % (c.update_error or "?"))
            return
        upd_lbl.configure(text=info["message"])
        if info["status"] == "available":
            banner_lbl.configure(text="Update available: %s → %s" % (VERSION, info["latest"]))
            btn_install.configure(state="normal")
            banner.pack(fill="x", padx=8, before=nb)
        else:
            banner.pack_forget()

    asked = set()

    def ask_map(key):
        asked.add(key)
        yes = messagebox.askyesno("GSVR - map from the host", "The host shares a map you do not have "
                                  "(%s).\n\nDownload it from the relay? Only the map file (Counter-Strike: and the custom "
                                  "files that map needs, never Valve's own content) is downloaded; everything is checked "
                                  "before use." % key, parent=root)
        bg(lambda: c.answer_ask(key, yes))

    def show_status(st):
        for key in (st or {}).get("ask") or ():
            if key not in asked:
                ask_map(key)
        colour, text = headline(st, c.running(), c.fatal)
        lamp.configure(fg=colour)
        head_lbl.configure(text=text)
        fatal_lbl.configure(text=("Companion could not start:\n" + c.fatal) if (c.fatal and not c.running()) else "")
        show_rows(status_rows(st, c.foreign))
        show_build(build_rows(st))
        refresh_buttons()

    def pump_ui():
        try:
            while True:
                kind, val = ui_q.get_nowait()
                if kind == "status":
                    show_status(val)
                elif kind == "games":
                    show_games(val)
                elif kind == "update":
                    show_update(val)
                elif kind == "progress":
                    banner_lbl.configure(text=val)
                elif kind == "installed":
                    if val:
                        updater.spawn_detached(val, cwd=runtime.install_root())
                        root.destroy()
                        return
                    installing.clear()
                    banner_lbl.configure(text="Update failed: %s" % (c.update_error or "see the Log tab"))
                    btn_install.configure(state="normal")
                    refresh_buttons()
                elif kind == "refresh":
                    refresh_buttons()
                elif kind == "hl_found":
                    hl_items[:] = val
                    hl_list.delete(0, "end")
                    for it in val:
                        hl_list.insert("end", "%s  [%s]" % (it["root"], ", ".join(it["mods"]) or "?"))
                    hl_lbl.configure(text=("%d Installation(en) gefunden - eine anklicken, dann Start." % len(val)) if val
                                     else "Nichts gefunden - bitte mit Browse den Ordner mit hl.exe waehlen.")
        except queue.Empty:
            pass
        root.after(200, pump_ui)

    def on_close():
        remember()
        c.stop()
        root.destroy()
    root.protocol("WM_DELETE_WINDOW", on_close)

    c.log("[gui] GSVR companion %s (%s install, %s) in %s" % (VERSION, runtime.install_kind(), updater.platform_key(),
                                                            runtime.install_root()))
    show_rows(status_rows(None))
    refresh_buttons()
    pump_logs()
    poll_status()
    pump_ui()
    if c.settings.get("auto_update_check", True):
        root.after(800, lambda: check_now(quiet=True))
    # players just want it running: start the companion right away (Windows autostart passes --minimized)
    if c.settings.get("auto_start", True) and not c.running():
        root.after(400, lambda: (c.start(), refresh_buttons()))
    if "--minimized" in sys.argv:
        root.iconify()
    root.mainloop()


# ===================================================================================================== selftest
def _foreign_server(port):
    """A dummy 'other program' on a candidate port: answers every GET with JSON that is NOT a GSVR status."""
    import http.server

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = b'{"v":1,"state":"idle","app":"lmgrd-ish","ownership":{"cstrike":true}}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _free_ports(n):
    socks, ports = [], []
    for _ in range(n):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); socks.append(s); ports.append(s.getsockname()[1])
    for s in socks:
        s.close()
    return ports


def selftest():
    """Headless test of the Controller against the real companion (prebuilt mode, temp dirs). Never opens a window.
    Candidate ports are replaced by free ones (--port-candidates); candidate 0 is held by a foreign HTTP server,
    so the companion must pick candidate 1 and the Controller must find it there and skip the foreign one.
    Then the update check + install through the Controller against a fake release server (selftest_update.py)."""
    import tempfile
    ok = True

    def chk(cond, what):
        print(("  ok   " if cond else "  FAIL ") + what, flush=True)
        return bool(cond)
    tmp = tempfile.mkdtemp()
    os.environ["APPDATA"] = tmp                                         # isolate settings on Windows
    logdir = os.path.join(tmp, "vrclog"); os.makedirs(logdir)
    packs = os.path.join(tmp, "packs"); os.makedirs(packs)
    src = os.path.join(REPO, "work", "cs", "de_dust2.bin")
    if os.path.isfile(src):
        shutil.copy(src, os.path.join(packs, "cstrike__de_dust2.bin"))
    cands = _free_ports(3)
    foreign = _foreign_server(cands[0])
    base = ["--prebuilt-dir", packs, "--vrchat-log-dir", logdir, "--cache-dir", os.path.join(tmp, "cache")]
    c = Controller(extra_args=base + ["--port-candidates", ",".join(map(str, cands))])
    c.settings.update(worlds="wrld_test", extra="")                   # ignore a real gui.json's extra args
    a = c.argv()
    ok &= chk(a[:len(runtime.companion_argv())] == runtime.companion_argv(), "argv starts with the companion launcher (%s)" % runtime.install_kind())
    ok &= chk("--world-allow" in a and "wrld_test" in a[a.index("--world-allow") + 1].split(",") and (runtime.FROZEN or ("-X" in a and "utf8" in a)),
              "argv has world allowlist + utf8")
    c.settings.update(war3_root="D:\\Games\\Warcraft III", war3_maps="")
    a3 = c.argv()
    ok &= chk("--war3-root" in a3 and a3[a3.index("--war3-root") + 1] == "D:\\Games\\Warcraft III" and "--war3-maps" not in a3,
              "Warcraft III folder setting -> --war3-root (one argument, spaces kept)")
    c.settings.update(war3_root="")
    ok &= chk("--game-root" not in c.argv(), "HL-DETECT: hl_root empty -> no --game-root (Steam detection stays default)")
    c.settings.update(hl_root="D:\\Spiele\\Counter-Strike 1.6 ")
    ah = c.argv()
    ok &= chk(ah.count("--game-root") == 1 and ah[ah.index("--game-root") + 1] == "D:\\Spiele\\Counter-Strike 1.6",
              "HL-DETECT: hl_root setting -> --game-root (one argument, spaces kept)")
    c.settings.update(hl_root="")
    # GAME-FOLDERS: Aendern... / Zuruecksetzen per game -> the existing settings -> argv (save_settings goes to the temp APPDATA)
    for gid in ("cstrike", "valve", "svencoop", "gearbox"):
        c.set_game_folder(gid, "D:\\Spiele\\HL ")
        ag = c.argv()
        ok &= chk(ag.count("--game-root") == 1 and ag[ag.index("--game-root") + 1] == "D:\\Spiele\\HL" and "--war3-root" not in ag,
                  "GAME-FOLDERS: Aendern (%s) -> hl_root -> --game-root only" % gid)
        c.reset_game_folder(gid)
        ok &= chk("--game-root" not in c.argv() and c.settings["hl_root"] == "", "GAME-FOLDERS: Zuruecksetzen (%s) -> no --game-root" % gid)
    c.set_game_folder("war3", "D:\\Warcraft III")
    ag = c.argv()
    ok &= chk(ag[ag.index("--war3-root") + 1] == "D:\\Warcraft III" and "--game-root" not in ag and c.settings["hl_root"] == "",
              "GAME-FOLDERS: Aendern (war3) -> war3_root -> --war3-root only")
    c.reset_game_folder("war3")
    ok &= chk("--war3-root" not in c.argv(), "GAME-FOLDERS: Zuruecksetzen (war3) -> no --war3-root")
    gv, live = c.games_view({"games": [{"id": "valve", "name": "Half-Life", "found": False, "root": "", "source": "none", "hint": "x"}]})
    ok &= chk(live and game_rows(gv)[0][2].startswith("nicht gefunden") and game_rows(gv)[0][3] == "-",
              "GAME-FOLDERS: games_view uses status.json games; not found -> 'nicht gefunden' + hint")
    c.settings.update(war3_root="")
    a0 = c.argv()
    ok &= chk("--relay-url" in a0 and a0[a0.index("--relay-url") + 1] == "https://w3relay.heimserver.me",
              "MAP-RELAY: default relay_url = https://w3relay.heimserver.me -> --relay-url")
    c.settings.update(relay_url="")
    ok &= chk("--relay-url" not in c.argv(), "MAP-RELAY: relay_url emptied -> feature off, no --relay-url argument")
    c.settings.update(relay_url="https://relay.example.org", map_autodownload="ask")
    a4 = c.argv()
    ok &= chk(a4[a4.index("--relay-url") + 1] == "https://relay.example.org" and a4[a4.index("--map-autodownload") + 1] == "ask",
              "MAP-RELAY: relay_url + map_autodownload settings -> --relay-url / --map-autodownload")
    c.settings.update(relay_url="", map_autodownload="on")
    ok &= chk(c.ports() == cands, "Controller derives the candidate ports from the companion argv")
    ok &= chk(c.start(), "companion started")
    st = None
    for _ in range(40):
        time.sleep(0.25)
        st = c.status()
        if st:
            break
    ok &= chk(st is not None and st.get("app") == "gsvr-companion" and st.get("port") == cands[1] and c.active_port == cands[1],
              "companion found on candidate 1 (%s), app=gsvr-companion" % (st and st.get("port")))
    ok &= chk(st is not None and st.get("companion") == VERSION, "status.json reports version.VERSION (%s)" % (st and st.get("companion")))
    ok &= chk(cands[0] in c.foreign, "foreign server on candidate 0 (%d) detected and skipped" % cands[0])
    ok &= chk(st is not None and st.get("world_ok") is False, "gate closed until the world is joined")
    ok &= chk(headline(st, c.running(), c.fatal) == ("green", "Running — 127.0.0.1:%d" % cands[1]), "headline: running + port")
    with open(os.path.join(logdir, "output_log_2026-10-18_23-00-00.txt"), "w") as fh:
        fh.write("2026.10.18 23:00:00 Debug - [Behaviour] Joining wrld_test:1~private\n")
    for _ in range(40):
        time.sleep(0.25)
        st = c.status()
        if st and st.get("world_ok"):
            break
    ok &= chk(st and st.get("world") == "wrld_test" and st.get("world_ok"), "world joined + allowed")
    lines = describe(st, c.foreign)
    ok &= chk(any("wrld_test" in l for l in lines) and any("Port: 127.0.0.1:%d" % cands[1] in l for l in lines)
              and any(("Port %d: used by another program" % cands[0]) in l for l in lines), "Status tab shows port + foreign port")
    ok &= chk((not os.path.isfile(src) or any(l.startswith("Games found: cstrike") for l in lines))
              and any(l.startswith("Last map request:") for l in lines),
              "Status tab shows games found + last map request")
    ok &= chk(c.stop() and not c.running(), "stopped")
    ok &= chk(c.status() is None and c.active_port is None, "no status after stop (foreign server alone is not accepted)")
    ok &= chk(headline(None, False, None) == ("grey", "Stopped"), "headline: stopped")
    logs = []
    while not c.log_queue.empty():
        logs.append(c.log_queue.get())
    ok &= chk(any("listening on http://127.0.0.1:%d/" % cands[1] in l for l in logs) and any("[gui] starting" in l for l in logs),
              "log shows the bound port")
    print("\n".join(describe(st, c.foreign)))

    # all candidates busy → companion exits with code 3, the GUI keeps its message for the red status line
    busy = [foreign, _foreign_server(cands[1]), _foreign_server(cands[2])]
    c2 = Controller(extra_args=base + ["--port-candidates", ",".join(map(str, cands))])
    c2.settings.update(worlds="", extra="")
    ok &= chk(c2.start(), "second companion started (all ports busy)")
    try:
        c2.proc.wait(30)
    except subprocess.TimeoutExpired:
        c2.stop()
    for _ in range(40):
        if c2.fatal:
            break
        time.sleep(0.05)
    ok &= chk(c2.proc.returncode == 3, "exit code 3 when no port is free (got %s)" % c2.proc.returncode)
    ok &= chk(bool(c2.fatal) and "netstat -ano" in c2.fatal and "excludedportrange" in c2.fatal and "Traceback" not in c2.fatal,
              "GUI holds the companion's message for the red status line")
    ok &= chk(headline(None, False, c2.fatal)[0] == "red", "headline: red when the companion could not start")
    print("  fatal: " + (c2.fatal or "-").splitlines()[0])
    for srv in busy:
        srv.shutdown(); srv.server_close()

    # update check + install through the Controller (fake Gitea, temp source install)
    import selftest_update
    ok &= selftest_update.controller_test(Controller, chk)
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    run_gui()
