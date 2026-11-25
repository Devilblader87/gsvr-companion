"""First run in 1 click - the headless logic (no Tk; the screens are in firstrun_ui.py; flows: docs/design/companion_first_run.md).

detect()         installed games (reuses games.local_view = Steam libraries + manual folder + non-Steam scan, and rosefind = registry /
                 Steam / usual folders / bounded drive scan). Never touches network drives (steam.fixed_drives = DRIVE_FIXED only).
validate_folder  a folder the player picked -> (ok, message in the player's language)
FirstRunJob      "Start": saves the chosen folders, starts the companion, tells it the ROSE folder, queues the packs, reports one
                 progress number + eta + a row per game; cancel(); finish -> worlds to open
world_rows()     the per-world status list (traffic light + state + hint + help key), also used by the Worlds tab
Nothing here sends anything over the network except to the local companion on 127.0.0.1.
"""
import threading
import time

import games as games_mod
import i18n
import rofind
import rosefind
import steam
import worlds
from i18n import tr

ORDER = ("rose", "ro", "valve", "cstrike", "svencoop", "gearbox", "war3")           # first-run list order (games that have a GSVR world)
GOLDSRC = ("valve", "cstrike", "svencoop", "gearbox")


def game_name(gid, fallback=""):
    key = "game." + gid
    return tr(key) if key in i18n.CATALOG else (fallback or gid)


def kind_of(gid):
    return gid if gid in ("rose", "ro") else "goldsrc"


def start_view(settings, fresh_install):
    """which screen the GUI opens: "firstrun" (first start on this PC) or "simple" (the same checklist screen, setup done).
    Never "main": the tabbed Advanced view is only opened by its button; a stored "view": "advanced" (<= 0.9.56) is ignored."""
    if fresh_install and not settings.get("first_run_done"):
        return "firstrun"
    return "simple"            # owner 2026-11-24: ALWAYS the simple view; the Advanced view is a choice per session, never restored


def detect(settings, local_view=None, rose_find=None, ro_find=None):
    """-> [entry] in ORDER. entry = {id, name, found, root, how (catalog key suffix), world (key|None), picked}
    settings: the gui.json dict (hl_root / rose_install = folders the player chose before). Injectable for tests."""
    lv = local_view or games_mod.local_view
    rf = rose_find or (lambda configured: rosefind.find(configured=configured or None, with_reasons=True))
    rof = ro_find or (lambda configured: rofind.find(configured=configured or None, with_reasons=True))
    hl = (settings.get("hl_root") or "").strip() or None
    try:
        gs = {g["id"]: g for g in lv(hl, (settings.get("war3_root") or "").strip() or None, (settings.get("war3_maps") or "").strip() or None, True)}
    except Exception:                                          # noqa: BLE001 - a broken detection must not block the first run
        gs = {}
    out = []
    for gid in ORDER:
        if gid == "rose":
            try:
                found = rf((settings.get("rose_install") or "").strip())
            except Exception:                                  # noqa: BLE001
                found = []
            if found:
                p, how, _why = found[0]
                out.append(_entry(gid, True, p, how if how != "common folder" else "common", how == "configured"))
            else:
                out.append(_entry(gid, False, "", "none", False))
            continue
        if gid == "ro":                                        # Ragnarok Online: folder with data.grf (registry / defaults / manual pick)
            try:
                found = rof((settings.get("ro_install") or "").strip())
            except Exception:                                  # noqa: BLE001
                found = []
            if found:
                p, how, _why = found[0]
                out.append(_entry(gid, True, p, how if how != "common folder" else "common", how == "configured"))
            else:
                out.append(_entry(gid, False, "", "none", False))
            continue
        g = gs.get(gid, {})
        # a "found" game that is only an unconfirmed folder (non-steam-scan) counts as found: the player confirms by pressing Start
        found = bool(g.get("found")) or (bool(g.get("root")) and g.get("source") == "non-steam-scan")
        src = g.get("source", "none")
        out.append(_entry(gid, found, g.get("root", "") if found else "", src, src == "manual"))
    return out


def _entry(gid, found, root, how, picked):
    how = {"drive scan": "scan", "common folder": "common"}.get(how, how)
    return {"id": gid, "name": game_name(gid), "found": found, "root": root, "how": how,
            "world": worlds.world_for_game(gid), "picked": picked}


def how_text(how):
    key = "how." + how
    return tr(key) if key in i18n.CATALOG else how


def not_found_text(gid):
    return tr("err.nf.rose" if gid == "rose" else "err.nf.ro" if gid == "ro" else "err.nf.war3" if gid == "war3" else "err.nf.goldsrc",
              game=game_name(gid))


def help_key_not_found(gid):
    return "help.nf.rose" if gid == "rose" else "help.nf.ro" if gid == "ro" else "help.nf.war3" if gid == "war3" else "help.nf.goldsrc"


def validate_folder(gid, path):
    """-> (ok, message). ok -> message is a short success text; else the clear 'which folder' message in the current language."""
    if gid == "rose":
        ok, why = rosefind.check(path)
        if not ok:                                              # the pick may be the parent of the game folder
            for q in rosefind.child_dirs(path):
                ok2, why2 = rosefind.check(q)
                if ok2:
                    return True, q
        return (True, path) if ok else (False, tr("err.bad.rose", why=why))
    if gid == "ro":
        ok, why = rofind.check(path)
        if not ok:                                              # the pick may be the parent of the client folder
            for q in rosefind.child_dirs(path):
                ok2, _why2 = rofind.check(q)
                if ok2:
                    return True, q
        return (True, path) if ok else (False, tr("err.bad.ro", why=why))
    if gid == "war3":
        import war3
        e = war3.ownership(path or None)["war3"]
        if e.get("allowed") and e.get("game_root"):
            return True, e["game_root"]
        return False, tr("err.nf.war3")
    d = steam.describe_root(path) if path else {"valid": False, "reason": ""}
    if not d.get("valid"):
        return False, tr("err.bad.goldsrc", game=game_name(gid), why=d.get("reason", ""))
    mods = d.get("mods") or []
    if mods and gid not in mods:
        return False, tr("err.bad.goldsrc", game=game_name(gid), why=", ".join(mods))
    return True, path


def resolved_root(gid, path):
    """validate_folder's ok path -> the folder to store (the ROSE game folder may be a child of the picked folder)"""
    ok, msg = validate_folder(gid, path)
    return msg if ok else ""


def apply_choices(settings, entries):
    """save the folders the player picked (picked=True) into the settings dict: ROSE -> rose_install, GoldSrc -> hl_root. -> dict of changes"""
    ch = {}
    for e in entries:
        if not e.get("found") or not e.get("root"):
            continue
        if e["id"] == "rose" and e.get("picked"):
            ch["rose_install"] = e["root"]
        elif e["id"] == "ro" and e.get("picked"):
            ch["ro_install"] = e["root"]
        elif e["id"] == "war3" and e.get("picked"):
            ch["war3_root"] = e["root"]
        elif e["id"] in GOLDSRC and e.get("picked"):
            ch["hl_root"] = e["root"]
    settings.update(ch)
    return ch


# ---------------------------------------------------------------------------------------------- progress
def rose_progress(rb, conv_err="err.rose_conv"):
    """status.json "rose_build" -> (pct 0-100, eta_s or -1, done, error text). done = every pack fresh."""
    if not isinstance(rb, dict):
        return 0, -1, False, ""
    packs = rb.get("packs") if isinstance(rb.get("packs"), dict) else {}
    if not rb.get("conv"):
        return 0, -1, False, tr(conv_err)
    if not rb.get("install_ok"):
        return 0, -1, False, ""
    total = len(packs)
    if not total:
        return 0, -1, False, ""
    fresh = sum(1 for p in packs.values() if isinstance(p, dict) and p.get("state") == "fresh")
    cur = rb.get("current") if isinstance(rb.get("current"), dict) else None
    part = max(0.0, min(1.0, float(cur.get("progress") or 0))) if cur else 0.0
    pct = int(100 * min(total, fresh + part) / total)
    if fresh >= total:
        return 100, 0, True, ""
    eta = -1
    if cur and isinstance(cur.get("eta_s"), (int, float)) and cur["eta_s"] >= 0:
        rest = total - fresh - 1
        per = (cur["eta_s"] / max(1e-3, 1 - part)) if part < 0.999 else cur["eta_s"]
        eta = int(cur["eta_s"] + max(0, rest) * per)
    err = ""
    for name, p in packs.items():
        if isinstance(p, dict) and p.get("error"):
            err = str(p["error"])[:160]
    return pct, eta, False, err


def title_key(phase):
    """catalog key of the progress screen's headline: the helper text only while the helper is really starting"""
    return "fr.starting_helper" if phase == "starting" else "fr.building"


class FirstRunJob:
    """The 'Start' button. All blocking calls (HTTP to the local companion) happen in step(), which the UI runs off the Tk thread.
    phases: starting -> building -> done | cancelled | error"""
    HELPER_TIMEOUT_S = 45.0
    MODELS_GRACE_S = 6.0

    def __init__(self, controller, entries, now=time.monotonic):
        self.c = controller
        self.entries = [e for e in entries if e.get("found")]
        self.now = now
        self.phase = "starting"
        self.pct = 0
        self.eta = -1
        self.error = ""
        self.rows = []                      # [(game name, text, pct)]
        self.game_rows = []                 # [{id, world, name, text, pct, ready, can_open, hint}] - per-game readiness for the progress screen
        self.t0 = now()
        self.t_up = None
        self._rose_sent = False
        self._ro_sent = False
        self._lock = threading.RLock()      # phase transitions: step() runs on a worker thread, cancel() on another
        self.worlds = []                    # world keys to offer when done (unique, in ORDER)

    def has(self, gid):
        return any(e["id"] == gid for e in self.entries)

    def begin(self):
        """save the chosen folders, start the companion (when not running)"""
        apply_choices(self.c.settings, self.entries)
        try:
            self.c.save_settings()
        except OSError as e:
            self.c.log("[firstrun] could not save settings: %s" % e)
        if not self.c.running():
            self.c.start()
        self.t0 = self.now()

    def cancel(self):
        """owns the phase change (the UI must not set job.phase itself). Only the first-run batch is cancelled
        (scope=batch): an on-demand build the world is waiting for keeps running."""
        with self._lock:
            if self.phase in self.TERMINAL:
                return
            self.phase = "cancelled"
        if self.c.active_port is not None:
            self.c.rose_post("/rose/cancel?scope=batch")
            if hasattr(self.c, "ro_post") and self.has("ro"):
                self.c.ro_post("/ro/cancel?scope=batch")

    TERMINAL = ("done", "cancelled", "error")

    def _fail(self, text):
        with self._lock:
            if self.phase not in self.TERMINAL:
                self.phase, self.error = "error", text

    def _set_phase(self, phase):
        """never leaves a terminal phase; -> False when the job is already finished/cancelled"""
        with self._lock:
            if self.phase in self.TERMINAL:
                return False
            self.phase = phase
            return True

    def step(self, st=None):
        """one poll. st: the companion's status.json (None = ask now). Updates phase / pct / eta / rows."""
        if self.phase in self.TERMINAL:
            return self.phase
        if st is None:
            st = self.c.status()
        if self.phase in self.TERMINAL:                 # cancel() ran during the blocking status call
            return self.phase
        if st is None:
            if not self.c.running() and self.c.fatal:
                self._fail(tr("err.helper", why=(self.c.fatal or "").splitlines()[0][:200]))
            elif not self.c.running() and self.now() - self.t0 > 5:
                self._fail(tr("err.helper_timeout"))
            elif self.now() - self.t0 > self.HELPER_TIMEOUT_S:
                self._fail(tr("err.helper_timeout"))
            return self.phase
        if self.t_up is None:
            self.t_up = self.now()
        if not self._set_phase("building"):
            return self.phase
        rose = next((e for e in self.entries if e["id"] == "rose"), None)
        parts, rows, grows = [], [], []
        if rose:
            with self._lock:
                send = not self._rose_sent and self.phase == "building"
                if send:
                    self._rose_sent = True
            if send:
                self.c.rose_post("/rose/install?dir=" + _quote(rose["root"]))
                with self._lock:
                    still = self.phase == "building"     # cancel() during the install post: no build_all after it
                if still:
                    self.c.rose_post("/rose/build_all")
            pct, eta, done, err = rose_progress(st.get("rose_build"))
            if err and not done:
                self.error = err
            parts.append((pct, done, eta))
            name = rose["name"]
            text = tr("fr.game_ready", game=name) if done else tr("fr.game_pct", game=name, pct=pct)
            rows.append((name, text, pct))
            # ROSE can be opened while its packs build: the world asks the companion on demand and that slot has priority
            grows.append({"id": "rose", "world": rose["world"], "name": name, "text": text, "pct": pct, "ready": done,
                          "can_open": bool(rose["world"]), "hint": "" if done else tr("fr.rose_early_hint")})
        ro_e = next((e for e in self.entries if e["id"] == "ro"), None)
        if ro_e:                                         # Ragnarok Online: same flow as ROSE (install -> build_all = the town map), no world yet
            with self._lock:
                send = not self._ro_sent and self.phase == "building"
                if send:
                    self._ro_sent = True
            if send:
                self.c.ro_post("/ro/install?dir=" + _quote(ro_e["root"]))
                with self._lock:
                    still = self.phase == "building"
                if still:
                    self.c.ro_post("/ro/build_all")
            pct, eta, done, err = rose_progress(st.get("ro_build"), "err.ro_conv")
            if err and not done:
                self.error = err
            parts.append((pct, done, eta))
            text = tr("fr.game_ready", game=ro_e["name"]) if done else tr("fr.game_pct", game=ro_e["name"], pct=pct)
            rows.append((ro_e["name"], text, pct))
            grows.append({"id": "ro", "world": ro_e["world"], "name": ro_e["name"], "text": text, "pct": pct, "ready": done,
                          "can_open": bool(ro_e["world"]) and done, "hint": ""})
        offered = set()
        building = {j.get("mod") for j in (st.get("build") or ()) if isinstance(j, dict)}
        for e in self.entries:
            if e["id"] in ("rose", "ro"):
                continue
            busy = e["id"] in building or (self.now() - self.t_up < self.MODELS_GRACE_S)
            text = tr("fr.game_wait", game=e["name"]) if busy else tr("fr.game_ondemand", game=e["name"])
            rows.append((e["name"], text, 0 if busy else 100))
            grows.append({"id": e["id"], "world": e["world"], "name": e["name"], "text": text, "pct": 0 if busy else 100,
                          "ready": not busy, "can_open": (not busy) and bool(e["world"]) and e["world"] not in offered, "hint": ""})
            if not busy and e["world"]:
                offered.add(e["world"])                      # games sharing a hub world (HL/OF/BS/Sven): one Open button
            parts.append((0 if busy else 100, not busy, -1))
        self.rows = rows
        self.game_rows = grows
        if parts:
            self.pct = int(sum(p[0] for p in parts) / len(parts))
            etas = [p[2] for p in parts if not p[1] and p[2] >= 0]
            self.eta = max(etas) if etas else -1
            if all(p[1] for p in parts) and self._set_phase("done"):
                self.pct, self.eta = 100, 0
        else:
            self._fail(tr("err.nogames"))
        if self.phase == "cancelled":
            return self.phase
        if self.phase == "done":
            seen = []
            for e in self.entries:
                if e["world"] and e["world"] not in seen:
                    seen.append(e["world"])
            self.worlds = [k for k in worlds.WORLD_ORDER if k in seen]
        return self.phase


def _quote(p):
    import urllib.parse
    return urllib.parse.quote(p or "")


def entries_from_view(gs, rose_path="", ro_path=""):
    """the Games tab data (status.json "games" list + the ROSE folder found) -> detect()-style entries for world_rows()"""
    out = [{"id": g.get("id", ""), "found": bool(g.get("found")), "root": g.get("root", "")} for g in gs or ()]
    out.append({"id": "rose", "found": bool(rose_path), "root": rose_path or ""})
    out.append({"id": "ro", "found": bool(ro_path), "root": ro_path or ""})
    return out


# ---------------------------------------------------------------------------------------------- per-world status list
WORLD_FRESH_S = 180      # a world asks every few seconds while it needs something, but an idle hub may be quiet for minutes
WORLD_LOG_S = 20         # the VRChat log has named an allowed GSVR world this long -> connected even without a request


def _int(st, key, default=-1):
    try:
        return int((st or {}).get(key, default))
    except (TypeError, ValueError):
        return default


def world_rows(entries, st, running):
    """-> [row] one per world in WORLD_ORDER. row = {key, world_id, name, light (green|yellow|red|grey), state, hint, help (catalog key
    or ""), can_open, url}. entries = detect() result, st = status.json or None, running = companion process alive."""
    rb = (st or {}).get("rose_build") if st else None
    build = [j for j in ((st or {}).get("build") or ()) if isinstance(j, dict)]
    out = []
    for key in worlds.WORLD_ORDER:
        wid, _n, gids = worlds.WORLDS[key]
        es = [e for e in entries if e["id"] in gids]
        found = [e for e in es if e.get("found")]
        row = {"key": key, "world_id": wid, "name": tr("w.name." + key), "light": "grey", "state": "", "hint": "", "help": "",
               "can_open": False, "url": worlds.launch_url(wid), "can_rebuild": False, "rebuild": ""}
        if not found:
            gid = gids[0]
            row.update(light="red", state=tr("ws.notfound"), hint=not_found_text(gid), help=help_key_not_found(gid))
        elif not running or st is None:
            row.update(light="grey", state=tr("ws.stopped"), hint=tr("wh.stopped"), help="help.stopped", can_open=True)
        elif key in ("rose", "ro"):                      # ROSE / Ragnarok Online: the world needs its built packs (rose_build / ro_build)
            kb = (st or {}).get("ro_build") if key == "ro" else rb
            pct, _eta, done, err = rose_progress(kb, "err.ro_conv" if key == "ro" else "err.rose_conv")
            cur = (kb or {}).get("current")
            if err:
                row.update(light="red", state=tr("ws.error"), hint=tr("wh.error", why=err), help="help.error")
            elif cur or (kb or {}).get("queue"):
                pk = (kb or {}).get("packs") or {}
                nf = sum(1 for p in pk.values() if isinstance(p, dict) and p.get("state") == "fresh")
                row.update(light="yellow", state=tr("ws.building_n", done=nf, total=len(pk)) if pk else tr("ws.building", pct=pct),
                           hint=tr("wh.building"), can_open=True)
            elif not done:                               # idle but stale: the companion's watchdog queues it within seconds; button = now
                row.update(light="yellow", state=tr("ws.update"), hint=tr("wh.update"), help="help.update", can_open=True,
                           can_rebuild=True, rebuild="/ro/build_all" if key == "ro" else "/rose/build_all")
            else:
                row.update(light="green", state=tr("ws.ready"), hint=tr("wh.ready"), can_open=True)
        else:
            mine = [j for j in build if j.get("mod") in gids]
            if mine:
                try:
                    pct = int(min(int(j.get("pct", 0)) for j in mine))
                except (TypeError, ValueError):
                    pct = 0
                row.update(light="yellow", state=tr("ws.building", pct=pct), hint=tr("wh.building"), can_open=True)
            else:
                row.update(light="green", state=tr("ws.ready"), hint=tr("wh.ready"), can_open=True)
        out.append(row)
    return out


# ---------------------------------------------------------------------------------------------- simple view: one checklist screen
# Three rows (games, VRChat, app version); the first open row is the "next" one. Only checks this app can really do: world contact and
# the VRChat "Allow Untrusted URLs" setting cannot be detected reliably, so they are NOT rows - the setting is a static hint card.
# No folder path, world id or other private detail is ever put into these texts (paths only on click / in the Advanced view).
VRC_RUNNING_S = 180      # the newest VRChat output_log was written this recently -> VRChat counts as running
ROW_KEYS = ("games", "vrchat", "version")
SECOND_LABEL = {"rose": "cl.short.rose", "hl": "cl.short.hl", "cs": "cl.short.cs", "ro": "cl.short.ro", "war3": "cl.short.war3"}
WORLD_LABEL = {"rose": "cl.wname.rose", "hl": "cl.wname.hl", "cs": "cl.wname.cs", "ro": "cl.wname.ro", "war3": "cl.wname.war3"}


def vrchat_state(log_dir, connected=False, now=None, newest_mtime=None):
    """-> "running" | "found" | "missing" | "unknown" from the VRChat log folder (logwatch, read-only, local).
    unknown = this system has no VRChat log folder to look at (macOS); newest_mtime is injectable for tests."""
    if connected:
        return "running"
    if not log_dir:
        return "unknown"
    if newest_mtime is None:
        import logwatch
        import os
        if not os.path.isdir(log_dir):
            return "missing"
        t = logwatch._newest_mtime(log_dir)
    else:
        t = newest_mtime if newest_mtime >= 0 else None
    if t is None:
        return "missing"
    return "running" if (time.time() if now is None else now) - t <= VRC_RUNNING_S else "found"


def world_key_of(world_id):
    """status.json "world" (wrld id) -> our world key or None"""
    for k, (wid, _n, _g) in worlds.WORLDS.items():
        if wid == world_id:
            return k
    return None


def build_progress(st, job=None):
    """-> (building, pct, eta_s, error) from the first-run job (preferred while it runs) or status.json (rose_build / build)"""
    if job is not None and job.get("phase") in ("starting", "building"):
        return True, int(job.get("pct") or 0), job.get("eta", -1), job.get("error") or ""
    if job is not None and job.get("phase") == "error":
        return False, 0, -1, job.get("error") or "error"
    st = st or {}
    rb = st.get("rose_build") if isinstance(st.get("rose_build"), dict) else None
    parts, eta, err = [], -1, ""
    if rb and (rb.get("current") or rb.get("queue")):
        pct, eta, done, err = rose_progress(rb)
        if not done:
            parts.append(pct)
    elif rb:
        err = rose_progress(rb)[3] if rb.get("install_ok") else ""
    for j in st.get("build") or ():
        if isinstance(j, dict):
            try:
                parts.append(int(j.get("pct", 0)))
            except (TypeError, ValueError):
                parts.append(0)
    if parts:
        return True, int(sum(parts) / len(parts)), eta, ""
    return False, 0, -1, err


def eta_short(eta_s):
    """seconds -> 'about 3 min' / '40 s' (no 'left'; for 'ready in ...'); '' when unknown"""
    if eta_s is None or eta_s < 0:
        return ""
    if eta_s < 60:
        return tr("cl.in_s", n=max(1, int(eta_s)))
    return tr("cl.in_min", n=int((eta_s + 30) // 60))


def chips_fit(found_w, rest_w, maxw, gap, more_w):
    """game chips on one line: found games always show (they wrap if they must); -> how many not-found chips still fit behind them
    when the rest collapses into one '+N more' chip of width more_w (all of them when everything fits). Pure, widths in px."""
    used = 0
    for w in found_w:
        used = w if (used and used + gap + w > maxw) else used + (gap if used else 0) + w
    u = used
    for w in rest_w:
        u += (gap if u else 0) + w
    if u <= maxw:
        return len(rest_w)
    n, u = 0, used
    for w in rest_w:
        nu = u + (gap if u else 0) + w
        if nu + gap + more_w > maxw:
            break
        n, u = n + 1, nu
    return n


def _row(key, status, title, text, button=None, **extra):
    r = {"key": key, "status": status, "title": title, "text": text, "button": button, "chips": None, "progress": None, "how": False}
    r.update(extra)
    return r


def checklist(entries, st, running, vrc="unknown", job=None, upd=("", None), version="", first_done=True, scanning=False):
    """The simple view as data (no Tk). entries = detect() result ([] while scanning), st = status.json or None, running = helper alive,
    vrc = vrchat_state(), job = {phase, pct, eta, error} of a running first-run job or None, upd = (state, latest) of the update line.
    -> {state: start|build|step|ready, title, sub, rows: [row], next: row key|None, primary: {label, action, enabled}, secondary: [..]}
    row = {key, status: ok|warn|bad|idle|busy, num, title, text, button: (label, action)|None, chips: [(name, found, gid)]|None,
    progress: pct|None, how: bool}. Every text is final (translated); none of them contains a folder path."""
    found = [e for e in entries if e.get("found")]
    building, pct, eta, err = build_progress(st, job)
    job_live = job is not None and job.get("phase") in ("starting", "building")

    # 1 games
    chips = [(e["name"], bool(e.get("found")), e["id"]) for e in entries]
    t_games = tr("cl.games")
    if scanning and not entries:
        games = _row("games", "busy", t_games, tr("fr.searching"))          # '...' dot while the (slow) first scan runs
    elif err and not building:
        games = _row("games", "bad", t_games, tr("cl.games_err"), ("w.help", "help_error"), chips=chips)
    elif not found:
        games = _row("games", "warn", t_games, tr("cl.games_none"), ("cl.add", "add"), chips=chips)
    elif building:
        e_txt = i18n.format_eta(eta) if eta is not None and eta >= 0 else tr("fr.estimating")
        games = _row("games", "busy", t_games, tr("cl.games_build", pct=pct, eta=e_txt),
                     ("fr.cancel", "cancel") if job_live else ("cl.add", "add"), chips=chips, progress=pct)
    elif not first_done:
        games = _row("games", "warn", t_games, tr("cl.games_start", n=len(found)), ("cl.add", "add"), chips=chips)
    else:
        games = _row("games", "ok", t_games, tr("cl.games_ok", n=len(found)), ("cl.add", "add"), chips=chips)

    # 2 VRChat
    t_vrc = "VRChat"
    if vrc == "running":
        vrow = _row("vrchat", "ok", t_vrc, tr("cl.vrc_running"))
    elif vrc == "found":                 # installed; a quiet log does not prove it is closed (idle VRChat writes nothing) -> no claim, no step
        vrow = _row("vrchat", "ok", t_vrc, tr("cl.vrc_found"))
    elif vrc == "missing":
        vrow = _row("vrchat", "warn", t_vrc, tr("cl.vrc_missing"))
    else:
        vrow = _row("vrchat", "idle", t_vrc, tr("cl.vrc_unknown"))

    # 3 app version
    t_v = tr("cl.version")
    us, latest = upd
    if us == "available":
        verrow = _row("version", "warn", t_v, tr("cl.ver_avail", v=version, b=latest or "?"), ("d.install", "install"))
    elif us == "checking":
        verrow = _row("version", "idle", t_v, tr("cl.ver_checking", v=version))
    elif us == "failed":
        verrow = _row("version", "ok", t_v, tr("cl.ver_failed", v=version), ("fr.check_upd", "check"))
    elif us == "current":
        verrow = _row("version", "ok", t_v, tr("cl.ver_current", v=version))
    else:
        verrow = _row("version", "ok", t_v, tr("cl.ver_auto", v=version), ("fr.check_upd", "check"))

    rows = [games, vrow, verrow]
    for i, r in enumerate(rows):
        r["num"] = i + 1
    nxt = next((r["key"] for r in rows if r["status"] in ("warn", "bad")), None)
    for r in rows:
        r["next"] = r["key"] == nxt

    # state + headline
    if found and first_done and not err and not building and nxt is None:   # games found + app up to date; never while packs build
        state = "ready"
    elif building:
        state = "build"
    elif not found or not first_done:
        state = "start"
    else:
        state = "step"
    open_n = sum(1 for r in rows if r["status"] in ("warn", "bad"))
    if state == "ready":
        title, sub = tr("cl.h_ready"), tr("cl.p_ready")
    elif state == "build":
        title, sub = tr("cl.h_build"), tr("cl.p_build")
    elif state == "start":
        title, sub = tr("cl.h_first"), tr("cl.p_first" if found else "cl.p_first_none")
    else:
        title = tr("cl.h_step") if open_n <= 1 else tr("cl.h_steps", n=open_n)
        sub = tr({"vrchat": "cl.p_step_vrc", "games": "cl.p_step_games", "version": "cl.p_step_ver"}.get(nxt, "cl.p_step"))

    # buttons: one big primary, the other worlds of the found games + Copy link as secondary
    wkeys = []
    for k in worlds.WORLD_ORDER:
        if any(e.get("world") == k for e in found):
            wkeys.append(k)
    prim_key = wkeys[0] if wkeys else None
    if state == "start" and found:
        primary = {"label": tr("cl.go_start"), "action": "start", "enabled": True}
    elif prim_key is None:
        primary = {"label": tr("cl.go_none"), "action": "", "enabled": False}
    elif state == "build":
        e_txt = eta_short(eta)
        primary = {"label": tr("cl.go_build", eta=e_txt) if e_txt else tr("cl.go_build_wait"), "action": "", "enabled": False}
    else:
        primary = {"label": tr("cl.go", world=tr(WORLD_LABEL[prim_key])), "action": "open:" + prim_key, "enabled": True}
    secondary = [{"label": tr(SECOND_LABEL[k]), "action": "open:" + k} for k in wkeys if k != prim_key and state != "start"]
    if prim_key is not None and state != "start":
        secondary.append({"label": tr("fr.copy_link"), "action": "copy:" + prim_key})
    return {"state": state, "title": title, "sub": sub, "rows": rows, "next": nxt, "primary": primary, "secondary": secondary,
            "hint": tr("cl.hint") if state in ("step", "ready") else ""}


def checklist_texts(model):
    """every visible text of a checklist() model (tests: no paths / forbidden words on the main screen)"""
    out = [model["title"], model["sub"], model.get("hint", ""), model["primary"]["label"]] + [b["label"] for b in model["secondary"]]
    for r in model["rows"]:
        out += [r["title"], r["text"]] + ([tr(r["button"][0])] if r["button"] else []) + [c[0] for c in r["chips"] or ()]
    return out
