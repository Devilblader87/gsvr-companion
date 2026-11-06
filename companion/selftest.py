"""Self-test for the GSVR companion (stdlib only, runs on macOS/Linux/Windows).

usage: python3 -I companion/selftest.py [--game-root input/Half-Life] [--prebuilt-src work/cs] [--sabotage]
Also runs selftest_update.py (updater / release script / frozen dispatch); GUI controller: gui.py --selftest.
  --sabotage   run the same test with a converter template that writes a junk pack, and with a corrupted
               prebuilt pack — the test MUST fail. The weapon model route (/pack/models.bin) is sabotaged the
               same way: a model converter that writes a GSM1 header over junk, and a truncated prebuilt models pack.
Scratch data goes to work/selftest/ (fake VRChat log dir, fake Steam library, pack cache).
"""
import argparse, hashlib, json, os, shutil, socket, struct, subprocess, sys, threading, time, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
for _st in (sys.stdout, sys.stderr):       # -I ignores PYTHONIOENCODING; Windows consoles default to cp1252
    try:
        _st.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
import steam, vdf          # noqa: E402

FAILS = []


def check(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what, flush=True)
    if not cond:
        FAILS.append(what)
    return cond


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def http(port, path, host=None, method="GET", timeout=10):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                 headers={"User-Agent": "gsvr-selftest", **({"Host": host} if host else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def status(port):
    return json.loads(http(port, "/status.json")[1])


def wait_status(port, pred, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            s = status(port)
            if pred(s):
                return s
        except (OSError, ValueError):
            pass
        time.sleep(0.25)
    return None


def vrc_line(msg):
    return f"{time.strftime('%Y.%m.%d %H:%M:%S')} Debug      -  {msg}\n"


def append(path, *msgs):
    with open(path, "a", encoding="utf-8") as fh:
        for m in msgs:
            fh.write(vrc_line(m))


def unit_tests(work, game_root):
    print("[unit] VDF parser + Steam ownership gate")
    new = vdf.loads('"libraryfolders"\n{\n "0"\n {\n  "path"  "C:\\\\Program Files (x86)\\\\Steam"\n'
                    '  "apps" { "70" "123" }\n }\n "1" { "path" "D:\\\\SteamLibrary" } // comment\n}\n')
    check(new["libraryfolders"]["1"]["path"] == "D:\\SteamLibrary", "new-style libraryfolders.vdf")
    old = vdf.loads('"LibraryFolders" { "TimeNextStatsReport" "1" "1" "E:\\\\Games\\\\Steam" }')
    check(old["libraryfolders"]["1"] == "E:\\Games\\Steam", "legacy libraryfolders.vdf (case-insensitive keys)")

    # fake Steam: root + 2nd library; common/Half-Life is a SYMLINK to the real install (no copying).
    s = os.path.join(work, "steam"); lib2 = os.path.join(work, "steamlib2")
    shutil.rmtree(s, ignore_errors=True); shutil.rmtree(lib2, ignore_errors=True)
    os.makedirs(os.path.join(s, "steamapps")); os.makedirs(os.path.join(lib2, "steamapps", "common"))
    with open(os.path.join(s, "steamapps", "libraryfolders.vdf"), "w") as fh:
        fh.write('"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n\t"1"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n}\n'
                 % (s.replace("\\", "\\\\"), lib2.replace("\\", "\\\\")))
    try:
        os.symlink(os.path.abspath(game_root), os.path.join(lib2, "steamapps", "common", "Half-Life"))
    except OSError as e:
        print(f"  skip ownership-from-steam test (symlink not permitted: {e})"); return
    acf = '"AppState"\n{\n\t"appid"\t\t"%d"\n\t"name"\t\t"x"\n\t"StateFlags"\t\t"%d"\n\t"installdir"\t\t"Half-Life"\n}\n'
    for appid, flags in ((10, 4), (70, 2), (20, 1026 | 4)):        # cstrike ok, valve not installed, tfc updating
        with open(os.path.join(lib2, "steamapps", f"appmanifest_{appid}.acf"), "w") as fh:
            fh.write(acf % (appid, flags))
    check(len(steam.library_folders(s)) == 2, "library_folders finds both libraries")
    own = steam.ownership_from_steam(s)
    check(own["cstrike"]["allowed"], "cstrike allowed (appmanifest_10 StateFlags=4)")
    check(not own["valve"]["allowed"], "valve denied (appmanifest_70 StateFlags=2, not fully installed)")
    check(not own["czero"]["allowed"], "czero denied (no appmanifest_80) although czero/ exists on disk")
    check(own["tfc"]["allowed"], "tfc allowed (StateFlags=1030 has FullyInstalled bit)")
    idx = steam.build_index(own)
    check(set(idx) == {"cstrike", "tfc"} and "de_dust2" in idx["cstrike"]["maps"], "index only lists allowed mods")
    check(idx["cstrike"]["maps"].get("surf_dust", {}).get("dir") == "cstrike_downloads", "cstrike_downloads maps indexed")
    sven_names(work)


def sven_names(work):
    """Map names outside SAFE_NAME (Sven Co-op '-sp_campaign_portal', community '+' / '(' …) and the content folder
    precedence _addon → mod → _downloads, on a synthetic game root."""
    print("[unit] map names + content folders (Sven Co-op)")
    good = ["-sp_campaign_portal", "_server_start", "BlackMesaEPF", "sc_tetris1", "map+(v2)", "a", "x.y", "[x]-1"]
    bad = ["", ".", "..", ".hidden", "a/b", "a\\b", "c:x", "a b", "a\tb", "x.", "con", "LPT1", "a|b", 'a"b', "a*b",
           "a?b", "a<b", "x" * 65, "été"]
    check(all(steam.safe_map_name(n) for n in good), "safe_map_name accepts " + ", ".join(good))
    wrong = [n for n in bad if steam.safe_map_name(n)]
    check(not wrong, f"safe_map_name refuses separators, '..'/leading or trailing dot, whitespace, ':', Windows "
                     f"reserved chars / device names, > 64 chars, non-ASCII (accepted: {wrong})")
    root = os.path.join(work, "fakesven")
    shutil.rmtree(root, ignore_errors=True)
    files = {"svencoop/maps/-sp_campaign_portal.bsp": b"base", "svencoop/maps/both.bsp": b"base",
             "svencoop_addon/maps/both.bsp": b"addon!", "svencoop_addon/maps/addon_only.bsp": b"a",
             "svencoop_downloads/maps/both.bsp": b"dl", "svencoop_downloads/maps/dl+map(2).bsp": b"d",
             "svencoop_downloads/maps/bad name.bsp": b"x", "svencoop/maps/.hidden.bsp": b"x"}
    for rel, data in files.items():
        p = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(data)
    idx = steam.build_index(steam.ownership_manual(root, only_mods={"svencoop"}))
    maps = idx.get("svencoop", {}).get("maps", {})
    check(maps.get("-sp_campaign_portal", {}).get("dir") == "svencoop", "'-sp_campaign_portal' indexed from svencoop/maps")
    check(maps.get("both", {}).get("dir") == "svencoop_addon", f"duplicate map: svencoop_addon wins ({maps.get('both', {}).get('dir')})")
    check(maps.get("addon_only", {}).get("dir") == "svencoop_addon" and maps.get("dl+map(2)", {}).get("dir") == "svencoop_downloads",
          "svencoop_addon and svencoop_downloads maps indexed")
    check("bad name" not in maps and ".hidden" not in maps, "names with a space / a leading dot are not indexed")
    sys.path.insert(0, os.path.join(REPO, "tools"))
    import gamedirs
    check(gamedirs.content_names(root, "svencoop") == ["svencoop_addon", "svencoop", "svencoop_downloads"],
          "tools/gamedirs.py: same folder order as the companion index (" + ", ".join(gamedirs.content_names(root, "svencoop")) + ")")
    check(tuple(gamedirs.CONTENT_SUFFIXES) == tuple(steam.MOD_DIR_SUFFIXES), "gamedirs.CONTENT_SUFFIXES == steam.MOD_DIR_SUFFIXES")


def integration(work, game_root, sabotage):
    print("[integration] companion end-to-end" + ("  (SABOTAGE: converter writes junk)" if sabotage else ""))
    logdir = os.path.join(work, "vrclog"); cache = os.path.join(work, "cache")
    shutil.rmtree(logdir, ignore_errors=True); shutil.rmtree(cache, ignore_errors=True)
    os.makedirs(logdir)
    log1 = os.path.join(logdir, "output_log_2026-10-18_10-00-00.txt")
    append(log1, "[GSVR-REQ] map=valve/c1a0 req=old")          # history: must NOT be replayed
    port = free_port()
    cmd = [sys.executable, "-I", os.path.join(HERE, "companion.py"), "--no-prewarm", "--game-root", game_root,
           "--vrchat-log-dir", logdir, "--cache-dir", cache, "--port", str(port),
           "--http-log", os.path.join(work, "http.log")]
    if sabotage:
        cmd += ["--pack-cmd", "{python} -I -c \"open(r'{out}','wb').write(b'JUNK'*256)\""]
    out = open(os.path.join(work, "companion.out"), "w")
    proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT)
    try:
        s = wait_status(port, lambda s: True, 20)
        if not check(s is not None, "companion up, /status.json answers"):
            return
        check(s["state"] == "idle" and s["ownership"]["cstrike"], "idle, cstrike allowed via --game-root")
        time.sleep(1.0)
        check(status(port)["state"] == "idle", "old log lines not replayed")
        code, body = http(port, "/index.json")
        idx = json.loads(body)
        check(code == 200 and any(m["name"] == "de_dust2" for m in idx["mods"]["cstrike"]["maps"]), "index.json lists cstrike/de_dust2")
        print(f"       index.json: {len(body)} bytes, " + ", ".join(f"{k}={len(v['maps'])}" for k, v in idx["mods"].items()))
        check(http(port, "/status.json", host="evil.example")[0] == 421, "foreign Host header rejected (DNS rebinding)")
        check(http(port, "/status.json", host="localhost:%d" % port)[0] == 200, "Host localhost:port accepted")
        check(http(port, "/../../etc/passwd")[0] == 404, "path traversal → 404")
        check(http(port, "/pack/")[0] == 404, "no directory listing")
        check(http(port, "/status.json", method="POST")[0] == 405, "POST rejected")
        check(http(port, "/pack/current.bin")[0] == 503, "current.bin 503 before any request")
        try:
            pvc = http(port, "/pack/previews.bin")[0]
        except Exception as e:                       # noqa: BLE001 — the handler crashed (KeyError 'key', L-5)
            pvc = repr(e)[:60]
        check(pvc in (200, 503), "previews.bin answers (200 / 503), the handler does not crash (got %s)" % pvc)

        append(log1, "[Behaviour] Joining wrld_00000000-1111-2222-3333-444444444444:1~private",
               "[GSVR-REQ] ping=abc123", "[GSVR-REQ] map=cstrike/de_dust2 req=1")
        s = wait_status(port, lambda s: s["req"] == "1" and s["state"] in ("ready", "error"))
        check(s and s["ping"] == "abc123" and s["world"].startswith("wrld_0000"), "ping echoed, world id tracked")
        if check(s and s["state"] == "ready", f"de_dust2 ready (status: {s and (s['state'], s['error'])})"):
            code, data = http(port, "/pack/current.bin")
            check(code == 200 and len(data) == s["size"], f"current.bin served, {len(data)} bytes = status.size")
            check(data[:3] == b"GSV" and data[3:4].isdigit(), f"pack magic is GSVn (got {data[:4]!r})")

        append(log1, "[GSVR-REQ] map=cstrike/../../../etc/passwd req=2")
        s = wait_status(port, lambda s: s["req"] == "2" and s["state"] in ("ready", "error"))
        check(s and s["state"] == "error", f"traversal map spec → error ({s and s['error']})")
        append(log1, "[GSVR-REQ] map=svencoop/svencoop1 req=3")
        s = wait_status(port, lambda s: s["req"] == "3" and s["state"] in ("ready", "error"))
        check(s and s["state"] == "error" and "not allowed" in s["error"], f"unowned mod → error ({s and s['error']})")
        check(http(port, "/pack/current.bin")[0] == 503, "after error, current.bin not served")

        log2 = os.path.join(logdir, "output_log_2026-10-18_11-00-00.txt")   # VRChat restarted → new log
        time.sleep(1.1)
        append(log2, "[GSVR-REQ] map=valve/crossfire req=4", "[GSVR-REQ] next=valve/c1a0 req=5")
        s = wait_status(port, lambda s: s["req"] == "4" and s["state"] in ("ready", "error")
                        and s["next"]["state"] in ("ready", "error"))
        check(s and s["state"] == "ready" and s["map"] == "valve/crossfire", "log rotation followed, crossfire ready")
        check(s and s["next"]["state"] == "ready" and http(port, "/pack/next.bin")[1][:3] == b"GSV", "next.bin prefetch served")
        append(log2, "[GSVR-REQ] map=valve/crossfire req=6")
        s = wait_status(port, lambda s: s["req"] == "6" and s["state"] == "ready")
        check(s and s["seconds"] is not None and s["seconds"] < 0.5, f"second request is a cache hit ({s and s['seconds']} s)")

        # loopback-only bind: the same port must not answer on a non-loopback address
        try:
            u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); u.connect(("192.0.2.1", 9)); lan = u.getsockname()[0]; u.close()
        except OSError:
            lan = None
        if lan and not lan.startswith("127."):
            c = socket.socket(); c.settimeout(2)
            try:
                c.connect((lan, port)); reach = True
            except OSError:
                reach = False
            c.close()
            check(not reach, f"not reachable on LAN address {lan}:{port}")
        with open(os.path.join(work, "http.log"), encoding="utf-8") as fh:
            check("gsvr-selftest" in fh.read(), "HTTP log records User-Agent")
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
        out.close()


def queued_test(work, game_root):
    """BUGS C-QUEUE (the maintainer's laptop 2026-10-23): a request that arrives while another map is still converting
    (BlizzardTD took 253 s there) must show up in status.json at once as "queued" with ITS req; before, status kept
    the old req and the world reported "companion can't see VRChat" until the old build was done."""
    print("[queued] new request while a slow build runs")
    logdir = os.path.join(work, "vrclog_q"); cache = os.path.join(work, "cache_q")
    shutil.rmtree(logdir, ignore_errors=True); shutil.rmtree(cache, ignore_errors=True)
    os.makedirs(logdir)
    log1 = os.path.join(logdir, "output_log_2026-10-23_19-00-00.txt")
    append(log1, "start")
    port = free_port()
    cmd = [sys.executable, "-I", os.path.join(HERE, "companion.py"), "--no-prewarm", "--game-root", game_root,
           "--vrchat-log-dir", logdir, "--cache-dir", cache, "--port", str(port),
           "--pack-cmd", "{python} -I -c \"import time; time.sleep(6); open(r'{out}','wb').write(b'JUNK'*256)\""]
    out = open(os.path.join(work, "companion_q.out"), "w")
    proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT)
    try:
        if not check(wait_status(port, lambda s: True, 20) is not None, "companion up"):
            return
        append(log1, "[Behaviour] Joining wrld_00000000-1111-2222-3333-444444444444:1~private",
               "[GSVR-REQ] map=cstrike/de_dust2 req=a1")
        s = wait_status(port, lambda s: s["req"] == "a1" and s["state"] == "building", 10)
        check(s is not None, "slow build of de_dust2 running")
        append(log1, "[GSVR-REQ] map=valve/crossfire req=b2")
        s = wait_status(port, lambda s: s["req"] == "b2", 3)
        check(s is not None and s["state"] == "queued" and s["map"] == "valve/crossfire",
              f"the new request is answered at once as queued ({s and (s['state'], s['map'])})")
        time.sleep(4.5)                                    # the first build ends: status must stay on b2
        s = status(port)
        check(s["req"] == "b2" and s["map"] == "valve/crossfire" and s["state"] in ("queued", "building"),
              f"the superseded build does not overwrite the queued request ({s['state']} {s['map']} {s['req']})")
        s = wait_status(port, lambda s: s["req"] == "b2" and s["state"] in ("ready", "error"), 20)
        check(s is not None and s["map"] == "valve/crossfire", "the queued request is built afterwards")
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
        out.close()


def http_timed(port, path, timeout=30):
    t0 = time.monotonic()
    code, body = http(port, path, timeout=timeout)
    return code, body, time.monotonic() - t0


def split_wait(body):
    """/pack/wait.bin body -> (status dict, pack bytes or None)."""
    if body[-4:] == b"GSVW":
        n = struct.unpack("<I", body[-8:-4])[0]
        return json.loads(body[-8 - n:-8]), body[:-8 - n]
    return json.loads(body), None


def wait_test(work, game_root):
    """BUGS L-SLOT (W3VR load times): GET /pack/wait.bin holds the answer until the build for the world's latest
    request is ready, so the string-load slot that would only have said "ready" already carries the pack. Ready ->
    at once; pending -> when the build finishes; past --wait-max -> the normal status JSON; old-style status.json +
    current.bin unchanged; other requests are served while one is held."""
    print("[wait] long-poll /pack/wait.bin")
    logdir = os.path.join(work, "vrclog_w"); cache = os.path.join(work, "cache_w")
    shutil.rmtree(logdir, ignore_errors=True); shutil.rmtree(cache, ignore_errors=True)
    os.makedirs(logdir)
    log1 = os.path.join(logdir, "output_log_2026-10-23_21-00-00.txt")
    append(log1, "start")
    port = free_port()
    # crossfire converts for 6 s (> --wait-max 3), every other map for 1.5 s
    cmd = [sys.executable, "-I", os.path.join(HERE, "companion.py"), "--no-prewarm", "--game-root", game_root,
           "--vrchat-log-dir", logdir, "--cache-dir", cache, "--port", str(port), "--wait-max", "3",
           "--pack-cmd", "{python} -I -c \"import time; time.sleep(6 if 'crossfire' in r'{map}' else 1.5); "
                         "open(r'{out}','wb').write(b'GSV1'+b'PACK'*1000)\""]
    out = open(os.path.join(work, "companion_w.out"), "w")
    proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT)
    try:
        s = wait_status(port, lambda s: True, 20)
        if not check(s is not None, "companion up"):
            return
        check(s.get("wait") == 3, f"status.json advertises the hold limit (wait={s.get('wait')})")
        append(log1, "[Behaviour] Joining wrld_00000000-1111-2222-3333-444444444444:1~private")
        time.sleep(1.0)
        code, body, dt = http_timed(port, "/pack/wait.bin")
        st, pack = split_wait(body)
        check(code == 200 and pack is None and st["app"] == "gsvr-companion" and st["state"] == "idle" and 2.5 < dt < 3.6,
              f"no request yet: the status after the {3.0} s grace ({code} {st.get('state')} {dt:.1f} s)")
        # pending request: answered when the build finishes, while status.json stays responsive
        append(log1, "[GSVR-REQ] map=cstrike/de_dust2 req=w1")
        time.sleep(0.6)
        res = {}
        th = threading.Thread(target=lambda: res.update(r=http_timed(port, "/pack/wait.bin")))
        th.start()
        time.sleep(0.3)
        t0 = time.monotonic(); s = status(port); dts = time.monotonic() - t0
        check(dts < 0.5 and s["req"] == "w1" and s["state"] in ("building", "queued"),
              f"status.json answers at once while wait.bin is held ({dts:.2f} s, {s['state']})")
        th.join(15)
        code, body, dt = res.get("r", (0, b"{}", 0))
        st, pack = split_wait(body)
        check(code == 200 and pack is not None and st["req"] == "w1" and st["state"] == "ready"
              and st["map"] == "cstrike/de_dust2" and len(pack) == st["size"] and 0.8 < dt < 3.0,
              f"pending request: the pack + trailer when the build is done ({code} {st.get('state')} {dt:.1f} s)")
        code2, cur = http(port, "/pack/current.bin")
        check(code2 == 200 and pack == cur and cur[:4] == b"GSV1", "wait.bin carries exactly the current.bin bytes")
        s = status(port)
        check(s["state"] == "ready" and s["req"] == "w1" and s["size"] == len(cur),
              "old-style status.json + current.bin unchanged (no trailer)")
        # ready + fresh request: at once
        code, body, dt = http_timed(port, "/pack/wait.bin")
        st, pack = split_wait(body)
        check(pack is not None and st["req"] == "w1" and dt < 0.5, f"ready request: answered at once ({dt:.2f} s)")
        # past --wait-max: the normal status ("building"), then the next poll gets the pack
        append(log1, "[GSVR-REQ] map=valve/crossfire req=w2")
        time.sleep(0.6)
        code, body, dt = http_timed(port, "/pack/wait.bin")
        st, pack = split_wait(body)
        check(code == 200 and pack is None and st["req"] == "w2" and st["state"] in ("building", "queued") and 2.8 < dt < 3.6,
              f"slow build: the status after --wait-max 3 s ({st.get('state')} {dt:.1f} s)")
        code, body, dt = http_timed(port, "/pack/wait.bin")
        st, pack = split_wait(body)
        check(pack is not None and st["req"] == "w2" and st["map"] == "valve/crossfire",
              f"the next wait.bin gets the finished pack ({st.get('state')} {dt:.1f} s)")
        # stale ready + a request that arrives during the grace: held for it (cache hit -> fast)
        time.sleep(5.2)
        res = {}
        th = threading.Thread(target=lambda: res.update(r=http_timed(port, "/pack/wait.bin")))
        th.start()
        time.sleep(0.8)
        append(log1, "[GSVR-REQ] map=cstrike/de_dust2 req=w3")
        th.join(15)
        code, body, dt = res.get("r", (0, b"{}", 0))
        st, pack = split_wait(body)
        check(pack is not None and st["req"] == "w3" and st["map"] == "cstrike/de_dust2",
              f"a request read during the grace is waited for ({st.get('req')} {dt:.1f} s)")
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
        out.close()


def wait_glue_test(work):
    """W3VR menu (docs/load_times.md item 5): the wait.bin answer for war3/__glue carries the map list (index_json),
    so the lobby needs no separate index.json slot; other maps' trailers stay without it."""
    print("[wait] glue answer carries the map list")
    pb = os.path.join(work, "pb_wait"); logdir = os.path.join(work, "vrclog_wg")
    for d in (pb, logdir):
        shutil.rmtree(d, ignore_errors=True); os.makedirs(d)
    for n in ("war3____glue.bin", "war3__(2)a.bin"):
        open(os.path.join(pb, n), "wb").write(b"GSV1" + n.encode() * 64)
    log1 = os.path.join(logdir, "output_log_2026-10-23_22-00-00.txt")
    append(log1, "start")
    port = free_port()
    cmd = [sys.executable, "-I", os.path.join(HERE, "companion.py"), "--prebuilt-dir", pb, "--vrchat-log-dir", logdir,
           "--cache-dir", os.path.join(work, "cache_wg"), "--port", str(port)]
    out = open(os.path.join(work, "companion_wg.out"), "w")
    proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT)
    try:
        if not check(wait_status(port, lambda s: True, 20) is not None, "companion up"):
            return
        append(log1, "[GSVR-REQ] map=war3/__glue req=g1")
        time.sleep(0.6)
        st, pack = split_wait(http(port, "/pack/wait.bin", timeout=30)[1])
        idx = json.loads(st.get("index_json") or "{}")
        names = [m["name"] for m in idx.get("mods", {}).get("war3", {}).get("maps", [])]
        check(pack is not None and st["req"] == "g1" and "(2)a" in names and idx == json.loads(http(port, "/index.json")[1]),
              f"glue: pack + the map list in the trailer ({names})")
        append(log1, "[GSVR-REQ] map=war3/(2)a req=g2")
        time.sleep(0.6)
        st, pack = split_wait(http(port, "/pack/wait.bin", timeout=30)[1])
        check(pack is not None and st["req"] == "g2" and "index_json" not in st, "other maps: no map list in the trailer")
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
        out.close()


LATE_WORLD = "wrld_00000000-late-0000-0000-000000000000"


def world_gate(work, game_root):
    print("[integration] --world-allow gate")
    logdir = os.path.join(work, "vrclog_wg"); shutil.rmtree(logdir, ignore_errors=True); os.makedirs(logdir)
    lg = os.path.join(logdir, "output_log_2026-10-18_12-00-00.txt"); append(lg, "start")
    wl = os.path.join(logdir, "worlds.txt")     # official list published after the start (re-read on an unknown world)
    with open(wl, "w") as f:
        f.write("# list\n" + LATE_WORLD + "\n")
    port = free_port()
    proc = subprocess.Popen([sys.executable, "-I", os.path.join(HERE, "companion.py"), "--no-prewarm", "--game-root", game_root,
                             "--vrchat-log-dir", logdir, "--cache-dir", os.path.join(work, "cache"), "--port", str(port),
                             "--world-allow", "wrld_good", "--world-list", "file://" + wl], stdout=subprocess.DEVNULL,
                            stderr=subprocess.STDOUT)
    try:
        wait_status(port, lambda s: True, 20)
        append(lg, "[Behaviour] Joining wrld_evil:1", "[GSVR-REQ] map=cstrike/de_dust2 req=9")
        s = wait_status(port, lambda s: s["world"] == "wrld_evil", 10); time.sleep(1.0); s = status(port)
        check(s["world_ok"] is False and s["req"] is None, "requests from a non-allowed world are ignored")
        check(http(port, "/index.json")[0] == 403, "index.json/pack 403 inside a non-allowed world")
        append(lg, "[GSVR-REQ] models=cstrike req=m9")
        time.sleep(1.0)
        check(status(port)["models"]["req"] is None and http(port, "/pack/models.bin")[0] == 403,
              "models request ignored and models.bin 403 inside a non-allowed world")
        append(lg, "[Behaviour] Joining wrld_good:1", "[GSVR-REQ] map=cstrike/de_dust2 req=10")
        s = wait_status(port, lambda s: s["req"] == "10" and s["state"] == "ready")
        check(s is not None and http(port, "/pack/current.bin")[0] == 200, "allowed world gets the pack")
        append(lg, "[Behaviour] Joining " + LATE_WORLD + ":1")
        s = wait_status(port, lambda s: s["world"] == LATE_WORLD, 10)
        check(s is not None and s["world_ok"] is True, "a world from --world-list (not in --world-allow) is allowed")
    finally:
        proc.terminate(); proc.wait(5)


def war3_test(work, war3_root):
    """Warcraft III: install check, map index, W3P1 validation; with Pillow also a real conversion (tools/w3pack.py)."""
    print("[cache] packs of older converter versions are pruned at start")
    import builder as _b
    cd = os.path.join(work, "prune"); shutil.rmtree(cd, ignore_errors=True); os.makedirs(cd)
    for fn in ("cstrike-de_dust2-0123456789abcdef-v-aaaaaaaaaaaa.bin", "cstrike-de_dust2-0123456789abcdef-v-bbbbbbbbbbbb.bin",
               "war3-(2)a-0123456789abcdef-v-cccccccccccc.bin", "war3_fan_sha.json", "models-valve-0123456789abcdef-s-dddddddddddd.bin"):
        open(os.path.join(cd, fn), "wb").write(b"x" * 10)
    n, _f = _b.prune_cache(cd, ["v-bbbbbbbbbbbb", "s-dddddddddddd"], log=lambda *_: None, spare=("war3-",))
    left = sorted(os.listdir(cd))
    check(n == 1 and "cstrike-de_dust2-0123456789abcdef-v-aaaaaaaaaaaa.bin" not in left and len(left) == 4,
          "old-version pack removed; current versions, spared war3 packs and non-pack files kept")
    print("[war3] ownership / index / W3P1")
    import builder, war3
    fake = os.path.join(work, "war3_fake"); shutil.rmtree(fake, ignore_errors=True)
    os.makedirs(os.path.join(fake, "Maps", "Sub"))
    open(os.path.join(fake, "war3.mpq"), "wb").write(b"MPQ\x1a" + b"\0" * 28)
    check(not war3.ownership(fake)["war3"]["allowed"], "install without War3x.mpq (no Frozen Throne) is denied")
    open(os.path.join(fake, "War3x.mpq"), "wb").write(b"MPQ\x1a" + b"\0" * 28)
    for n in ("(2)A.w3x", "(4)B.w3m", "has space.w3x", "readme.txt"):
        open(os.path.join(fake, "Maps", "Sub", n), "wb").write(b"HM3W")
    own = war3.ownership(fake)
    idx = war3.build_index(own)
    odd = [k for k in idx["war3"]["maps"] if k.startswith("sha-")]
    check(own["war3"]["allowed"] and sorted(k for k in idx["war3"]["maps"] if not k.startswith("sha-")) == ["(2)a", "(4)b", "__glue"]
          and idx["war3"]["skipped"] == 0 and len(odd) == 1 and idx["war3"]["maps"][odd[0]]["title"] == "has space"
          and idx["war3"]["maps"][odd[0]]["dir"] == "Maps/Sub",
          "install with both core archives allowed; .w3x/.w3m indexed (+ the __glue start menu); a name with a space is "
          "listed by content (sha key, its folder kept)")
    wm = idx["war3"]["maps"]
    check(wm["(2)a"].get("folder") == "Sub" and wm[odd[0]].get("folder") == "Sub" and "cat" not in wm["(2)a"]
          and "folder" not in wm["__glue"], "MAP-FOLDERS: maps carry their folder below Maps (also the sha-keyed one)")
    flat = {"(2)losttemple": {"dir": "Maps", "size": 1, "title": "(2)LostTemple"},
            "sha-1": {"dir": "Maps", "size": 1, "title": "Footman Frenzy"},
            "sha-2": {"dir": "Maps/Mine/TD", "size": 1, "title": "Wintermaul"}}
    check(war3.annotate_folders(flat) and flat["(2)losttemple"]["cat"] == "classic" and flat["sha-1"]["cat"] == "fun"
          and flat["sha-2"]["folder"] == "Mine/TD", "MAP-FOLDERS: flat maps sorted into Classic / Fun Maps, nested folders kept")
    import companion
    st = companion.State.__new__(companion.State)
    st.lock = __import__("threading").RLock(); st.index_rev = 1
    st.index = {"war3": {"appid": 0, "name": "w", "maps": dict(flat, **{"__glue": {"dir": "glue", "size": 0}})}}
    ij = st.index_json()
    names = [m["name"] for m in ij["mods"]["war3"]["maps"]]
    check(ij["v"] == 1 and ij["fv"] == 2 and names == ["__glue", "(2)losttemple", "sha-1", "sha-2"],
          "MAP-FOLDERS: index.json carries fv 2 + folder / cat, sorted by folder")
    st.index = {"war3": {"appid": 0, "name": "w", "maps": {"a": {"dir": "Maps", "size": 1}}}}
    ij = st.index_json()
    check("fv" not in ij and "folder" not in ij["mods"]["war3"]["maps"][0], "MAP-FOLDERS: unannotated index stays format 1")
    # maintainer 2026-10-24: maps copied in later show up without a restart (signature of the scanned folders changes)
    s0 = war3.dirs_signature(war3.LAST_SCAN["dirs"] + [os.path.join(fake, "Maps")], war3.LAST_SCAN["files"])
    open(os.path.join(fake, "Maps", "Sub", "(2)New One.w3x"), "wb").write(b"HM3W2")
    s1 = war3.dirs_signature(war3.LAST_SCAN["dirs"] + [os.path.join(fake, "Maps")], war3.LAST_SCAN["files"])
    idx2 = war3.build_index(war3.ownership(fake))
    check(s0 != s1 and len([k for k in idx2["war3"]["maps"] if k.startswith("sha-")]) == 2,
          "a map copied into the Maps folder later changes the folder signature and is indexed by the next scan")
    # INDEX-REV (maintainer 2026-10-27): the companion's OWN watch (State.maps_changed, no hand-fed dirs) must see a map that is
    # added to / changed in / removed from the install's Maps tree, and a rescan must bump index_rev (the world polls it)
    import companion
    w = os.path.join(work, "war3_watch"); shutil.rmtree(w, ignore_errors=True); os.makedirs(os.path.join(w, "Maps", "Sub"))
    for n in ("war3.mpq", "War3x.mpq"):
        open(os.path.join(w, n), "wb").write(b"MPQ\x1a" + b"\0" * 28)
    open(os.path.join(w, "Maps", "Sub", "(2)Old.w3x"), "wb").write(b"HM3W")
    wst = companion.State.__new__(companion.State)
    wst.lock = __import__("threading").RLock(); wst.index_rev = 1; wst.seq = 0
    wst.last_watch, wst.watch_sig, wst.index, wst.ownership = 0.0, None, {}, {}
    wst.cs_keys = None                                 # no relay in this test: rescan skips the CS share keys
    wst.args = argparse.Namespace(prebuilt_dir=None, game_root=w, mods=None, steam_root=None, no_war3=False,
                                  war3_root=w, war3_maps=None, cache_dir=os.path.join(work, "war3_watch_cache"))
    old_watch_s, companion.MAP_WATCH_S = companion.MAP_WATCH_S, 0
    try:
        wst.rescan()
        r0 = wst.index_rev
        check(not wst.maps_changed(), "INDEX-REV: unchanged install Maps tree -> no change reported")
        open(os.path.join(w, "Maps", "Sub", "(2)Added.w3x"), "wb").write(b"HM3W2")
        ch = wst.maps_changed()
        if ch:
            wst.rescan()
        check(ch and wst.index_rev > r0 and "(2)added" in wst.index["war3"]["maps"],
              "INDEX-REV: a map added to the install's Maps tree is noticed by the watch and bumps index_rev")
        r1 = wst.index_rev
        open(os.path.join(w, "Maps", "Sub", "(2)Added.w3x"), "wb").write(b"HM3W2-longer")
        check(wst.maps_changed(), "INDEX-REV: a changed map file is noticed")
        wst.rescan()
        os.remove(os.path.join(w, "Maps", "Sub", "(2)Old.w3x"))
        ch = wst.maps_changed()
        wst.rescan()
        check(ch and "(2)old" not in wst.index["war3"]["maps"] and wst.index_rev > r1,
              "INDEX-REV: a removed map is noticed and leaves the index")
        # review: the baseline is taken BEFORE the scan - a map that lands while the scan hashes triggers one more rescan
        wst.rescan()
        check(not wst.maps_changed(), "INDEX-REV: quiet again after a rescan (no spurious second rescan)")
        real_info = war3.add_map_info
        def landing(maps, roots, cache):
            open(os.path.join(w, "Maps", "Sub", "(2)Landed.w3x"), "wb").write(b"HM3W3")
            return real_info(maps, roots, cache)
        war3.add_map_info = landing
        try:
            wst.rescan()
        finally:
            war3.add_map_info = real_info
        check(wst.maps_changed(), "INDEX-REV: a map that landed during the scan is not in the baseline -> another rescan")
        # review: war3 denied / not installed still records the watch list (a later install is noticed)
        war3.LAST_SCAN["watch"] = []
        war3.build_index({}, None, None)
        check(war3.LAST_SCAN["watch"] and war3.LAST_SCAN["pre_sig"] is not None, "INDEX-REV: watch list set even when war3 is not allowed")
        war3.build_index(war3.ownership(w))
        wl = [os.path.normcase(os.path.abspath(x)) for x in war3.LAST_SCAN["watch"]]
        dl = [os.path.normcase(os.path.abspath(x)) for x in war3.LAST_SCAN["dirs"]]
        check(not any(x == y or x.startswith(y + os.sep) for x in wl for y in dl), "INDEX-REV: watch list does not repeat scanned trees")
    finally:
        companion.MAP_WATCH_S = old_watch_s
    casc = os.path.join(work, "war3_casc"); shutil.rmtree(casc, ignore_errors=True); os.makedirs(os.path.join(casc, "Maps"))
    ud = war3.user_map_dirs([{"root": casc, "kind": "casc"}, {"root": fake, "kind": "mpq"}])
    check(os.path.join(casc, "Maps") in ud and os.path.join(fake, "Maps") in ud,
          "every install's Maps folder is scanned (CASC: the listing has only the built-in maps; 1.2x: name collisions, "
          "names with spaces, maps copied in later) - MAP-FOLDERS-2")
    import test_war3_index
    import unittest
    r = unittest.TextTestRunner(verbosity=0).run(unittest.defaultTestLoader.loadTestsFromModule(test_war3_index))
    check(r.wasSuccessful(), "MAP-FOLDERS-2: index matches the player's folders (fan folder = install root, no duplicates)")
    import test_hl_detect                     # HL-DETECT: registry Steam, non-Steam scan, depth/time budget, game-root check
    r = unittest.TextTestRunner(verbosity=0).run(unittest.defaultTestLoader.loadTestsFromModule(test_hl_detect))
    check(r.wasSuccessful(), "HL-DETECT: Steam via registry / fixed drives, non-Steam GoldSrc scan (depth + time budget), "
                             "uninstall entries, game-root validation")
    import test_mapshare                      # MAP-RELAY: relay client, fetch, stock maps, bomb limits, State fetch
    r = unittest.TextTestRunner(verbosity=0).run(unittest.defaultTestLoader.loadTestsFromModule(test_mapshare))
    check(r.wasSuccessful(), "MAP-RELAY: relay round trip, sha mismatch / size / format rejected, fetch with a fake relay, "
                             "stock maps never uploaded, MPQ bomb limits, ask / off modes")
    sys.path.insert(0, os.path.join(REPO, "relay"))
    import test_csshare                       # CS-SHARE: BSP dependency parser, bundle rules, share -> relay -> fetch -> install
    r = unittest.TextTestRunner(verbosity=0).run(unittest.defaultTestLoader.loadTestsFromModule(test_csshare))
    check(r.wasSuccessful(), "CS-SHARE: BSP entity / wad / sky parser (hostile input), stock content never packed or written, "
                             "bundle rules shared with the relay, host upload -> guest install into cstrike_downloads")
    import test_relay
    r = unittest.TextTestRunner(verbosity=0).run(unittest.defaultTestLoader.loadTestsFromModule(test_relay))
    check(r.wasSuccessful(), "MAP-RELAY: relay service (limits, TTL, eviction, rate limits, no listing, admin delete)")
    check(idx["war3"]["maps"]["(2)a"].get("title") == "(2)A" and idx["war3"]["maps"]["(4)b"].get("title") == "(4)B",
          "map titles keep the file name's case without the extension (the lobby's map list)")
    import prebuilt as _pb
    pbd = os.path.join(work, "pb_info"); shutil.rmtree(pbd, ignore_errors=True); os.makedirs(pbd)
    open(os.path.join(pbd, "war3__(2)a.bin"), "wb").write(b"x")
    json.dump({"title": "Two A", "players": 2}, open(os.path.join(pbd, "war3__(2)a.info.json"), "w"))
    open(os.path.join(pbd, "war3__previews.bin"), "wb").write(b"W3P1")
    _o, pidx = _pb.scan(pbd)
    check("previews" not in pidx.get("war3", {}).get("maps", {}), "prebuilt war3__previews.bin is not listed as a map")
    pm = pidx.get("war3", {}).get("maps", {}).get("(2)a", {})
    check(pm.get("title") == "Two A" and pm.get("info", {}).get("players") == 2,
          "prebuilt pack: lobby info from the .info.json sidecar")
    def w3p(secs, bad_off=False):
        head = 16 + 16 * len(secs)
        d, body, off = b"", b"", head
        for tag in secs:
            data = b"\0" * 8
            d += struct.pack("<4s3i", tag, off + (2 if bad_off else 0), len(data), 1)
            body += data
            off += len(data)
        return struct.pack("<4s3i", b"W3P1", 1, len(secs), 0) + d + body
    good = [b"INFO", b"TATL", b"TVTX", b"TIDX", b"TLAY"]
    pth = os.path.join(work, "t.w3p")
    open(pth, "wb").write(w3p(good)); ok1 = builder.validate_pack(pth) > 0
    open(pth, "wb").write(w3p(good[:4]))
    try:
        builder.validate_pack(pth); ok2 = False
    except RuntimeError:
        ok2 = True
    open(pth, "wb").write(w3p(good, bad_off=True))
    try:
        builder.validate_pack(pth); ok3 = False
    except RuntimeError:
        ok3 = True
    check(ok1 and ok2 and ok3, "W3P1 accepted; missing section / misaligned section refused")
    r131 = os.path.join(os.path.dirname(war3_root), "war3_131")
    if os.path.isdir(r131):
        own2 = war3.ownership([war3_root, r131])
        idx2 = war3.build_index(own2)
        az = idx2.get("war3", {}).get("maps", {}).get("(8)azuretowerdefense", {})
        check(az.get("info", {}).get("players") == 8 and az.get("title") == "Azure Tower Defense"
              and az.get("info", {}).get("tileset"), "real install: lobby map info from war3map.w3i (players, title, tileset)")
        try:
            import PIL  # noqa: F401
            pv = war3.previews(idx2["war3"], os.path.join(work, "pv_cache"), log=lambda *_: None)
            raw = open(pv, "rb").read()
            check(raw[:4] == b"W3P1" and b"P " in raw[:20000] and b"(8)azuretowerdefense" in raw[:20000],
                  "real install: lobby preview atlas (PVIX + PVAT) with the Azure TD preview")
        except ImportError:
            print("  skip preview atlas (no Pillow in this Python)")
        check(sorted(i["version"] for i in own2["war3"]["installs"]) == ["121", "131"]
              and sorted(az.get("versions", {})) == ["121", "131"] and az.get("ver") == "131"
              and war3.pick(az, "121")[2] == "121",
              "1.21 (MPQ) + 1.31 (CASC) installs found; Azure TD indexed in both, newest is the default, ver=121 picks 1.21")
        mp3 = war3.music_bytes(idx2["war3"], "Sound\\Music\\mp3Music\\Mainscreen.mp3", log=lambda *_: None)
        check(bool(mp3) and (mp3[:3] == b"ID3" or mp3[:2] in (b"\xff\xfb", b"\xff\xfa", b"\xff\xf3")),
              "W3VR music: the menu mp3 comes from the install unchanged (%s bytes)" % (len(mp3) if mp3 else 0))
        check(war3.music_bytes(idx2["war3"], "Sound\\..\\war3map.j") is None
              and war3.music_bytes(idx2["war3"], "Maps\\x.mp3") is None
              and war3.music_bytes(idx2["war3"], "Sound\\Music\\x.wav") is None,
              "W3VR music: only Sound\\...mp3 paths are served (no .., no other folders / types)")
    else:
        print("  skip multi-version check (no " + r131 + ")")
    try:
        import PIL  # noqa: F401
    except ImportError:
        print("  skip conversion (no Pillow in this python)")
        return
    m = os.path.join(war3_root, "Maps", "FrozenThrone", "Scenario", "(8)AzureTowerDefense.w3x")
    if not os.path.isfile(m):
        print("  skip conversion (no " + m + ")")
        return
    out = os.path.join(work, "azure.w3p")
    r = subprocess.run([sys.executable, "-I", os.path.join(REPO, "tools", "w3pack.py"), "--map", m, "--install",
                        war3_root, out], capture_output=True, text=True)
    check(r.returncode == 0 and builder.validate_pack(out) > 1_000_000, "w3pack converts Azure TD to a valid W3P1 pack")


def prebuilt_test(work, src, sabotage):
    """--prebuilt-dir: serves <mod>__<map>.bin as-is (no game files); bare names via --prebuilt-mod."""
    print("[integration] --prebuilt-dir" + ("  (SABOTAGE: cstrike__de_dust2.bin is corrupted)" if sabotage else ""))
    maps = ["de_dust2", "de_inferno", "de_nuke", "cs_office"]
    have = [m for m in maps if os.path.isfile(os.path.join(src, m + ".bin"))]
    if not check(len(have) == 4, f"4 prebuilt source packs in {src} ({', '.join(have) or 'none'})"):
        return
    pdir = os.path.join(work, "prebuilt"); shutil.rmtree(pdir, ignore_errors=True); os.makedirs(pdir)
    for m in maps:
        dst = os.path.join(pdir, f"cstrike__{m}.bin")
        if sabotage and m == "de_dust2":
            with open(os.path.join(src, m + ".bin"), "rb") as fh:
                data = fh.read()
            with open(dst, "wb") as fh:                      # right magic, truncated body → must be rejected
                fh.write(data[:len(data) // 2])
        else:
            try:
                os.symlink(os.path.abspath(os.path.join(src, m + ".bin")), dst)
            except OSError:
                shutil.copyfile(os.path.join(src, m + ".bin"), dst)
    with open(os.path.join(pdir, "cstrike__broken.bin"), "wb") as fh:
        fh.write(b"JUNK" * 256)
    with open(os.path.join(pdir, "cstrike__short.bin"), "wb") as fh:
        fh.write(b"GSV2" + b"\0" * 60)                      # valid magic, header claims nothing / truncated
    for junk in ("README.txt", "nomod.bin", "bad name__x.bin"):
        open(os.path.join(pdir, junk), "wb").close()

    logdir = os.path.join(work, "vrclog_pb"); shutil.rmtree(logdir, ignore_errors=True); os.makedirs(logdir)
    lg = os.path.join(logdir, "output_log_2026-10-18_13-00-00.txt"); append(lg, "start")
    port = free_port()
    out = open(os.path.join(work, "companion_prebuilt.out"), "w")
    proc = subprocess.Popen([sys.executable, "-I", os.path.join(HERE, "companion.py"), "--prebuilt-dir", pdir,
                             "--vrchat-log-dir", logdir, "--cache-dir", os.path.join(work, "cache_pb"), "--port", str(port)],
                            stdout=out, stderr=subprocess.STDOUT)
    try:
        s = wait_status(port, lambda s: True, 20)
        if not check(s is not None, "prebuilt companion up"):
            return
        check(s["ownership"].get("cstrike") is True and s["ownership"].get("valve") is False,
              f"ownership from packs: cstrike allowed, valve denied ({s['ownership']})")
        idx = json.loads(http(port, "/index.json")[1])
        names = sorted(m["name"] for m in idx["mods"].get("cstrike", {}).get("maps", []))
        check(names == sorted(maps + ["broken", "short"]) and set(idx["mods"]) == {"cstrike"},
              f"index lists exactly the <mod>__<map>.bin files ({names})")
        req = 100
        for m in maps:
            req += 1
            append(lg, f"[GSVR-REQ] map=cstrike/{m} req={req}")
            s = wait_status(port, lambda s, r=str(req): s["req"] == r and s["state"] in ("ready", "error"), 30)
            ok = s is not None and s["state"] == "ready" and s["map"] == f"cstrike/{m}"
            if check(ok, f"cstrike/{m} ready from prebuilt ({s and (s['state'], s['error'])})"):
                code, data = http(port, "/pack/current.bin")
                with open(os.path.join(src, m + ".bin"), "rb") as fh:
                    ref = fh.read()
                check(code == 200 and hashlib.sha1(data).digest() == hashlib.sha1(ref).digest(),
                      f"  served bytes identical to {m}.bin ({len(data)} bytes)")
        for m, why in (("broken", "bad magic"), ("short", "out of range")):
            req += 1
            append(lg, f"[GSVR-REQ] map=cstrike/{m} req={req}")
            s = wait_status(port, lambda s, r=str(req): s["req"] == r and s["state"] in ("ready", "error"), 30)
            check(s is not None and s["state"] == "error" and why in (s["error"] or ""),
                  f"corrupt pack cstrike/{m} rejected ({s and s['error']})")
        check(http(port, "/pack/current.bin")[0] == 503, "nothing served after a rejected pack")
        for spec, why in (("valve/c1a0", "not allowed"), ("cstrike/de_aztec", "not found"), ("cstrike/../../etc/passwd", "bad map spec")):
            req += 1
            append(lg, f"[GSVR-REQ] map={spec} req={req}")
            s = wait_status(port, lambda s, r=str(req): s["req"] == r and s["state"] in ("ready", "error"), 30)
            check(s is not None and s["state"] == "error" and why in (s["error"] or ""), f"{spec} → error '{why}' ({s and s['error']})")
    finally:
        proc.terminate(); proc.wait(5); out.close()

    # bare <map>.bin names (the maintainer's work/cs/ as-is) via --prebuilt-mod
    port = free_port()
    proc = subprocess.Popen([sys.executable, "-I", os.path.join(HERE, "companion.py"), "--prebuilt-dir", src,
                             "--prebuilt-mod", "cstrike", "--vrchat-log-dir", logdir,
                             "--cache-dir", os.path.join(work, "cache_pb"), "--port", str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        wait_status(port, lambda s: True, 20)
        time.sleep(0.3)
        append(lg, "[GSVR-REQ] map=cstrike/cs_office req=200")
        s = wait_status(port, lambda s: s["req"] == "200" and s["state"] in ("ready", "error"), 30)
        check(s is not None and s["state"] == "ready" and s["size"] == os.path.getsize(os.path.join(src, "cs_office.bin")),
              f"--prebuilt-mod cstrike serves bare {src}/cs_office.bin ({s and (s['state'], s['error'])})")
    finally:
        proc.terminate(); proc.wait(5)


def gsm1_summary(data):
    """Parses a GSM1 pack like GsvrModelLoader.cs → dict, or raises ValueError."""
    import struct
    if data[:4] != b"GSM1":
        raise ValueError(f"magic {data[:4]!r}")
    hdr, ver, w, m, s, e, u, i, p, n, aw, ah, mips, nb, _f = struct.unpack_from("<15i", data, 4)
    need = hdr + 4 * (w * 32 + m * 8 + s * 6 + e * 4 + i + u * 2 + p * 6 + n) + sum((aw >> l) * (ah >> l) * 4 for l in range(mips)) + nb
    if ver == 2:
        q, a, h, _r = struct.unpack_from("<4i", data, 64)
        need += 4 * (q * 8 + a * 8 + h * 8 + w * 4)
    if need != len(data):
        raise ValueError(f"size {len(data)} != header {need}")
    ids = [struct.unpack_from("<i", data, hdr + k * 96)[0] for k in range(w)]
    rec = {wid: struct.unpack_from("<24i", data, hdr + k * 96) for k, wid in enumerate(ids)}
    return dict(weapons=w, meshes=m, sounds=s, ids=ids, rec=rec, atlas=(aw, ah, mips))


def models_test(work, game_root, sabotage):
    """/pack/models.bin: build the GSM1 weapon pack from the (manual-override) install, serve, cache, reject."""
    print("[integration] weapon model pack /pack/models.bin" + ("  (SABOTAGE: model converter writes junk)" if sabotage else ""))
    logdir = os.path.join(work, "vrclog_md"); shutil.rmtree(logdir, ignore_errors=True); os.makedirs(logdir)
    lg = os.path.join(logdir, "output_log_2026-10-18_14-00-00.txt"); append(lg, "start")
    cache = os.path.join(work, "cache_md"); shutil.rmtree(cache, ignore_errors=True)
    port = free_port()
    cmd = [sys.executable, "-I", os.path.join(HERE, "companion.py"), "--no-prewarm", "--game-root", game_root, "--mods", "cstrike,valve",
           "--vrchat-log-dir", logdir, "--cache-dir", cache, "--port", str(port)]
    if sabotage:     # right magic, junk body → validate_models must reject it
        cmd += ["--models-cmd", "{python} -I -c \"open(r'{out}','wb').write(b'GSM1'+bytes(60)+b'JUNK'*256)\""]
    out = open(os.path.join(work, "companion_models.out"), "w")
    proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT)
    try:
        s = wait_status(port, lambda s: True, 20)
        if not check(s is not None and s.get("models", {}).get("state") == "idle", "status.json has models slot (idle)"):
            return
        check(http(port, "/pack/models.bin")[0] == 503, "models.bin 503 before any request")
        append(lg, "[GSVR-REQ] models=cstrike req=m1")
        s = wait_status(port, lambda s: s["models"]["req"] == "m1" and s["models"]["state"] in ("ready", "error"), 120)
        mdl = s and s["models"]
        if check(mdl and mdl["state"] == "ready", f"cstrike weapon pack ready ({mdl and (mdl['state'], mdl['error'], mdl['seconds'])})"):
            code, data = http(port, "/pack/models.bin")
            check(code == 200 and len(data) == mdl["size"] and len(data) <= 100 * 1024 * 1024,
                  f"models.bin served, {len(data)} bytes = status.models.size, <= 100 MB")
            try:
                g = gsm1_summary(data)
                ak = g["rec"].get(28)
                check(g["weapons"] >= 31 and ak is not None and ak[1] >= 0 and ak[2] >= 0 and ak[4] >= 0,
                      f"GSM1 parses: {g['weapons']} weapons, {g['meshes']} meshes, {g['sounds']} sounds, atlas {g['atlas']}; AK has P/W mesh + fire sound")
            except ValueError as e:
                check(False, f"GSM1 parses ({e})")
            check(http(port, "/pack/models.bin", host="evil.example")[0] == 421, "models.bin with foreign Host → 421")
            check(http(port, "/pack/models.bin", method="POST")[0] == 405, "POST models.bin → 405")
            append(lg, "[GSVR-REQ] models=cstrike req=m2")
            s = wait_status(port, lambda s: s["models"]["req"] == "m2" and s["models"]["state"] == "ready", 60)
            check(s is not None and s["models"]["seconds"] < 0.5 and s["models"]["key"] == mdl["key"],
                  f"second models request is a cache hit ({s and s['models']['seconds']} s)")
        # valve: HL weapons + items, HL player models, monsters (GSX1 MON1)
        append(lg, "[GSVR-REQ] models=valve req=m3")
        s = wait_status(port, lambda s: s["models"]["req"] == "m3" and s["models"]["state"] in ("ready", "error"), 300)
        mdl = s and s["models"]
        if check(mdl and mdl["state"] == "ready", f"valve weapon + monster pack ready ({mdl and (mdl['state'], mdl['error'], mdl['seconds'])})"):
            code, data = http(port, "/pack/models.bin")
            try:
                g = gsm1_summary(data)
                mp5 = g["rec"].get(4)
                check(code == 200 and len(data) == mdl["size"] and len(data) <= 99 * 1000 * 1000 and mp5 is not None
                      and mp5[1] >= 0 and mp5[4] >= 0 and b"MON1" in data,     # not a tail window: the 8 MB SPR2 chunk follows MON1
                      f"valve GSM1: {g['weapons']} weapons, {g['meshes']} meshes, {len(data) / 1e6:.1f} MB (<= 99, modelpack max_bytes guard), "
                      f"MP5 has P mesh + fire sound, MON1 chunk present")
            except ValueError as e:
                check(False, f"valve GSM1 parses ({e})")
        for req, spec, why in (("m4", "svencoop", "not allowed"), ("m5", "../../etc", "bad mod")):
            append(lg, f"[GSVR-REQ] models={spec} req={req}")
            s = wait_status(port, lambda s, r=req: s["models"]["req"] == r and s["models"]["state"] in ("ready", "error"), 60)
            check(s is not None and s["models"]["state"] == "error" and why in (s["models"]["error"] or ""),
                  f"models={spec} → error '{why}' ({s and s['models']['error']})")
        check(http(port, "/pack/models.bin")[0] == 503, "after a failed models request, models.bin not served")
        check(status(port)["state"] == "idle", "map slot untouched by models requests")
    finally:
        proc.terminate(); proc.wait(5); out.close()

    # --prebuilt-dir: <mod>__models.gsm served as-is (sabotage: truncated)
    pdir = os.path.join(work, "prebuilt_md"); shutil.rmtree(pdir, ignore_errors=True); os.makedirs(pdir)
    built = [f for f in os.listdir(cache) if f.endswith(".gsm")] if os.path.isdir(cache) else []
    src_pack = os.path.join(cache, built[0]) if built else None
    if not check(src_pack is not None or sabotage, "a built models pack exists for the prebuilt test"):
        return
    if src_pack is None:          # sabotage run: nothing valid was built — use a junk file with the right magic
        data = b"GSM1" + bytes(60) + b"JUNK" * 64
    else:
        with open(src_pack, "rb") as fh:
            data = fh.read()
    with open(os.path.join(pdir, "cstrike__models.gsm"), "wb") as fh:
        fh.write(data[:len(data) // 2] if sabotage else data)
    os.symlink(os.path.abspath(os.path.join(REPO, "work", "cs", "de_dust2.bin")), os.path.join(pdir, "cstrike__de_dust2.bin")) \
        if os.path.isfile(os.path.join(REPO, "work", "cs", "de_dust2.bin")) else open(os.path.join(pdir, "cstrike__x.bin"), "wb").close()
    port = free_port()
    proc = subprocess.Popen([sys.executable, "-I", os.path.join(HERE, "companion.py"), "--prebuilt-dir", pdir,
                             "--vrchat-log-dir", logdir, "--cache-dir", os.path.join(work, "cache_pb"), "--port", str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        wait_status(port, lambda s: True, 20); time.sleep(0.3)
        append(lg, "[GSVR-REQ] models=cstrike req=p1")
        s = wait_status(port, lambda s: s["models"]["req"] == "p1" and s["models"]["state"] in ("ready", "error"), 30)
        ok = s is not None and s["models"]["state"] == "ready"
        if check(ok, f"prebuilt cstrike__models.gsm ready ({s and s['models']['error']})"):
            code, got = http(port, "/pack/models.bin")
            check(code == 200 and hashlib.sha1(got).digest() == hashlib.sha1(data).digest(), "prebuilt models pack served byte-identical")
        append(lg, "[GSVR-REQ] models=valve req=p2")
        s = wait_status(port, lambda s: s["models"]["req"] == "p2" and s["models"]["state"] in ("ready", "error"), 30)
        check(s is not None and s["models"]["state"] == "error" and "not allowed" in (s["models"]["error"] or ""),
              f"prebuilt: models for a mod without packs → not allowed ({s and s['models']['error']})")
    finally:
        proc.terminate(); proc.wait(5)


def foreign_server(port):
    """Dummy 'other program' on a port: HTTP 200 with JSON that is not a GSVR status (like a foreign web UI)."""
    import http.server, threading

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = b'{"v":1,"state":"idle","server":"not-gsvr"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def free_ports(n):
    socks = []
    for _ in range(n):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); socks.append(s)
    ports = [s.getsockname()[1] for s in socks]
    for s in socks:
        s.close()
    return ports


def ports_test(work):
    """Port auto-selection + identity: candidate 0 held by a foreign server → companion binds candidate 1;
    status.json carries app/proto/port; all candidates busy (or --port N busy) → exit code 3 + help, no traceback."""
    print("[integration] port auto-selection / companion identity")
    import companion, re
    check(tuple(companion.PORT_CANDIDATES) == (38427, 41213, 44391), f"default candidates {companion.PORT_CANDIDATES}")
    check(all(not 27000 <= p <= 27100 and p < 49152 for p in companion.PORT_CANDIDATES),
          "candidates outside 27000-27100 (Steam/FlexLM) and below 49152 (Windows dynamic range)")
    sb = open(os.path.join(REPO, "unity", "Assets", "GoldSrcVR", "Editor", "GsvrSceneBuilder.cs"), encoding="utf-8").read()
    m = re.search(r"CompanionPorts\s*=\s*\{([^}]*)\}", sb)
    check(m is not None and tuple(int(x) for x in m.group(1).split(",") if x.strip()) == tuple(companion.PORT_CANDIDATES),
          "GsvrSceneBuilder.CompanionPorts == companion.PORT_CANDIDATES (world bakes the same list)")
    logdir = os.path.join(work, "vrclog_port"); shutil.rmtree(logdir, ignore_errors=True); os.makedirs(logdir)
    base = [sys.executable, "-I", os.path.join(HERE, "companion.py"), "--prebuilt-dir", os.path.join(REPO, "work", "cs"),
            "--prebuilt-mod", "cstrike", "--vrchat-log-dir", logdir, "--cache-dir", os.path.join(work, "cache_port")]
    cands = free_ports(3)
    clist = ",".join(map(str, cands))
    fsrv = [foreign_server(cands[0])]
    out_path = os.path.join(work, "companion_port.out")
    out = open(out_path, "w")
    proc = subprocess.Popen(base + ["--port-candidates", clist], stdout=out, stderr=subprocess.STDOUT)
    try:
        s = wait_status(cands[1], lambda s: True, 20)
        check(s is not None and s.get("app") == "gsvr-companion" and s.get("proto") == 1 and s.get("port") == cands[1],
              f"candidate 0 busy (foreign JSON) → companion on candidate 1, status app/proto/port "
              f"({s and (s.get('app'), s.get('proto'), s.get('port'))})")
        check(json.loads(http(cands[0], "/status.json")[1]).get("app") != "gsvr-companion", "foreign server still owns candidate 0")
        try:
            socket.create_connection(("127.0.0.1", cands[2]), timeout=1).close(); third = True
        except OSError:
            third = False
        check(not third, "candidate 2 left alone")
        time.sleep(0.2)
        txt = open(out_path, encoding="utf-8").read()
        check(f"port {cands[0]}: in use" in txt, "one-line reason logged for the busy port")
        # a second companion must not start next to the running one
        r = subprocess.run(base + ["--port-candidates", f"{cands[1]},{cands[2]}"], capture_output=True, text=True, timeout=30)
        check(r.returncode == 3 and "already running" in r.stdout, f"second companion refuses to start (exit {r.returncode})")
    finally:
        proc.terminate(); proc.wait(5); out.close()
    fsrv += [foreign_server(cands[1])]
    held = socket.socket(); held.bind(("127.0.0.1", cands[2]))     # bound, not listening → bind() itself fails
    try:
        r = subprocess.run(base + ["--port-candidates", clist], capture_output=True, text=True, timeout=30)
        o = r.stdout + r.stderr
        check(r.returncode == 3, f"all candidates busy → exit code 3 (got {r.returncode})")
        check("netstat -ano | findstr :" in o and "tasklist /fi" in o and "excludedportrange" in o and "Traceback" not in o,
              "actionable message (netstat/tasklist/excludedportrange), no traceback")
        check(f"port {cands[2]}: port in use" in o, f"bind failure reason logged for {cands[2]}")
        r = subprocess.run(base + ["--port", str(cands[0])], capture_output=True, text=True, timeout=30)
        check(r.returncode == 3 and "Traceback" not in r.stdout + r.stderr, f"--port N busy → exit code 3 (got {r.returncode})")
    finally:
        held.close()
        for f in fsrv:
            f.shutdown(); f.server_close()
    port = free_ports(1)[0]
    proc = subprocess.Popen(base + ["--port", str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        s = wait_status(port, lambda s: True, 20)
        check(s is not None and s.get("port") == port and s.get("app") == "gsvr-companion", "--port N forces that port")
    finally:
        proc.terminate(); proc.wait(5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--game-root", default=os.path.join(REPO, "input", "Half-Life"))
    ap.add_argument("--work", default=os.path.join(REPO, "work", "selftest"))
    ap.add_argument("--prebuilt-src", default=os.path.join(REPO, "work", "cs"),
                    help="dir with de_dust2.bin, de_inferno.bin, de_nuke.bin, cs_office.bin for the --prebuilt-dir test")
    ap.add_argument("--war3-root", default=os.path.join(os.path.dirname(REPO), "warcraft3", "input", "war3"))
    ap.add_argument("--sabotage", action="store_true")
    a = ap.parse_args()
    os.makedirs(a.work, exist_ok=True)
    unit_tests(a.work, a.game_root)
    ports_test(a.work)
    integration(a.work, a.game_root, a.sabotage)
    queued_test(a.work, a.game_root)
    wait_test(a.work, a.game_root)
    wait_glue_test(a.work)
    world_gate(a.work, a.game_root)
    prebuilt_test(a.work, a.prebuilt_src, a.sabotage)
    war3_test(a.work, a.war3_root)
    models_test(a.work, a.game_root, a.sabotage)
    import test_holo
    test_holo.holo_test(a.work, a.game_root, a.sabotage)
    import selftest_update                   # self-updater, release script, frozen-build dispatch
    FAILS.extend(selftest_update.run_all(a.game_root))
    print(f"\nRESULT: {'FAIL (' + str(len(FAILS)) + ')' if FAILS else 'PASS'}   (companion output: {a.work}/companion.out)")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
