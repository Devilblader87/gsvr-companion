# GSVR companion

A small local program the player runs on their own Windows PC next to VRChat. It finds the GoldSrc games
in their Steam library, converts maps from **their own install** into packs, and serves them to the VRChat
world over `http://127.0.0.1` (one of three fixed ports, see "Ports") only. The world itself contains no Valve assets.

The companion is Python 3 stdlib only (3.9+; tested with 3.9 and 3.14 on macOS). Players get it as a
**Windows app** (`GSVR.exe`, a PyInstaller build, no Python needed) that updates itself from our Gitea releases;
the same code also runs from source. Files:

| file | what it does |
|---|---|
| `companion.py` | entry point: HTTP server, request worker, status |
| `gui.py` | the app window (Start/Stop, status, log, settings, update banner); `Controller` = headless logic |
| `version.py` | **single source of truth** for `VERSION` + update server defaults |
| `updater.py` | self-update from Gitea releases: check, download, SHA-256 verify, stage, swap, rollback |
| `runtime.py` | source install vs frozen build: paths, how to start the companion / converter |
| `gsvr_main.py` | entry point of the frozen build (`GSVR.exe` / `gsvr-cli.exe`), dispatches GUI / CLI / `--run-converter` |
| `steam.py` | Steam discovery, libraries, appmanifests, ownership gate v0, map index |
| `vdf.py` | Valve KeyValues (`.vdf` / `.acf`) parser |
| `logwatch.py` | tails the newest VRChat `output_log_*.txt`, follows rotation, parses `[GSVR-REQ]` |
| `builder.py` | runs the converter (`tools/pack.py`) as a subprocess, pack cache, sanity checks (`validate_pack`) |
| `prebuilt.py` | `--prebuilt-dir` mode: serves already-built packs, no game files, no converter |
| `models.py` | weapon model + sound pack (GSM1, `tools/modelpack.py`): `/pack/models.bin`, `models=` command, `validate_models`, cache |
| `buildprog.py` | build progress: converters print `@plan stage=weight ...` / `@progress <stage> <done> <total>` (`tools/bprog.py`), the companion streams their stdout per job and publishes `status.json` `build`: `[{kind: map\|models\|hologram\|war3, mod, name, pct 0-100, eta_s (-1 unknown), state}]` (empty = idle; eta from the rate, or the last build of the same key in `<cache>/build_times.json`); GUI bars + world HUD (`GsvrBuildHud`) read it |
| `holo.py` | map hologram bundle for the mod hub (GSPV, `tools/previewpack.py`): `/pack/hologram.bin`, `holo=` command, `validate_hologram`, cache, `--prebuilt-dir` `<mod>__hologram.gspv` |
| `selftest.py` / `selftest.sh` | end-to-end self-test (`--sabotage` = must fail); also runs `selftest_update.py` |
| `selftest_update.py` | updater / release script / frozen-dispatch tests against a local fake Gitea |
| `packaging/` | `gsvr.spec` (PyInstaller), `build_windows.bat`, `build_linux.sh`, `release.py`, `requirements-build.txt` |

## The app (GSVR.exe)

One window: a big **Start/Stop** button with a status lamp and a one-line headline (`Running — 127.0.0.1:38427`,
`Stopped`, `Could not start`), and three tabs:

- **Status**: companion version + state, port, games found (owned), VRChat world (allowed or not), last map
  request (state, size, build time, req id), prefetch slot, weapon models, notes (e.g. a foreign program on a
  candidate port). If the companion can't start (exit code 3, no free port) its fix-it text is shown in red.
- **Log**: the companion's live output plus GUI / update messages (Copy, Clear).
- **Settings**: allowed world IDs, extra companion arguments (e.g. `--game-root "D:\Games\Half-Life"`), update
  server URL, optional read token, "check for updates on start", "include pre-releases", open / clear cache.

Settings are saved to `%APPDATA%\GSVR\gui.json` (macOS/Linux `~/.config/gsvr/gui.json`), the pack cache is
`%LOCALAPPDATA%\GSVR\cache`; both live outside the install folder and survive updates.
The cache folder can be moved to any drive in the Settings tab ("Cache folder" > Change... / Default); the GUI offers to move the existing cache (verified copy, then the old files are deleted), and `--cache-dir` does the same on the command line.

Frozen build layout (one folder, so the updater can swap single files and antivirus heuristics are calmer than
with a one-file exe):
```
GSVR\GSVR.exe            windowed GUI
GSVR\gsvr-cli.exe        console: companion CLI (same options as companion.py), --run-converter, --selftest, --diag
GSVR\_internal\          Python runtime + tools\{pack,bsp30,wad3,mdl10,spr,modelpack}.py
GSVR\gsvr-manifest.txt   "# gsvr-companion <version> <platform>" + every shipped file (used by the updater)
```
A frozen build has no `python` to run `python -X utf8 -I tools/pack.py`, so the converter runs as
`gsvr-cli.exe --run-converter <bundled tools\pack.py> …` (in-process via `runpy`; only `pack.py` and
`modelpack.py` from the bundled `tools\` are accepted). UTF-8 mode is baked into both exes (`X utf8` in the spec,
`gsvr-cli --diag` shows `utf8_mode 1`), so converter output never depends on the console code page.
The cache key still hashes the bundled `tools\*.py`, so a converter change in an update invalidates old packs.

## Updates

The app checks the release feed on start (Settings: on by default) and on "Check for updates now". If there is
a newer release, a yellow banner says **Update available: 0.4.0 → v0.5.0 [Install]**. Install stops the
companion, downloads, verifies, swaps the files and restarts the app. No git needed.

- Feed: `GET {server}/api/v1/repos/Devilblad3r/gsvr-companion/releases?limit=30` (+ `/releases/latest`); the
  newest non-draft release wins (pre-releases included unless switched off). Tags are compared with
  `version.VERSION` (`v0.10.0 > v0.9.0`, `v0.4.0-rc1 < v0.4.0`).
- **Release repo `Devilblad3r/gsvr-companion` is PUBLIC** (binaries + checksums only), so no login or token is needed.
  The source repo `goldsrc-vrchat` stays private. `release.py upload` creates the release and its tag there.
- **Server: a LAN-only Gitea (historical).** Players outside the LAN can't update until the feed is
  reachable from the internet (Settings → Update server takes any URL). The optional read token (Settings) is only
  sent after an anonymous 404/401/403, only to the configured server origin, and never logged.
- Asset per platform, each with a `.sha256` next to it (format `<sha256>  <file name>`):

  | install | asset |
  |---|---|
  | `GSVR.exe` (Windows) | `gsvr-companion-windows-x64.zip` + `.sha256` |
  | Linux build | `gsvr-companion-linux-x64.tar.gz` + `.sha256` |
  | from source (python) | `gsvr-companion-source.zip` + `.sha256` |

  No asset or no `.sha256` → no update offered. Checksum mismatch → refused, nothing touched.
- Install: download + unpack into `<install>\.gsvr-update\` (member checks: no absolute paths, no `..`, no links,
  size limits; the archive's `gsvr-manifest.txt` must name the same version and platform). Then for every file:
  rename the old one to `<name>.gsvr-old` (Windows allows renaming a running exe/dll, not overwriting it), move
  the new one in. Files the old manifest listed but the new one doesn't are moved away too. Any error → full
  rollback. The app relaunches itself; the next start deletes the `*.gsvr-old` files.
- Source installs: only `companion/` and `tools/` are written, and the updater refuses to run in a git checkout
  (use `git pull` there). Installs from the old v0.2/v0.3 zips (git-based "Update" button) need one manual
  download of the new version; after that they update themselves.
- CLI: `gsvr-cli.exe --update-check` (or `python3 -I companion/gsvr_main.py --update-check`) prints the result.

## Building the exe and releasing

The Windows exe has to be built **on Windows** (PyInstaller doesn't cross-compile). Options:
1. **the maintainer's Windows PC** (simplest): install 64-bit Python 3.12 from python.org (default options include
   Tcl/Tk), check out the repo, run `companion\packaging\build_windows.bat`. It creates a venv in
   `companion\build\`, installs the pinned PyInstaller (`packaging/requirements-build.txt`), runs
   `selftest_update.py`, builds `companion\build\dist\GSVR\`, smoke-tests the frozen exe (`--version`,
   `--run-converter … modelpack.py --version`, `--selftest`) and writes the archives + `.sha256` into
   `companion\build\release\`.
2. **Gitea Actions with a Windows runner**: register `act_runner` on a Windows machine/VM (label e.g.
   `windows`), and add a workflow on tag push that runs `build_windows.bat` and then `release.py upload` with a
   repository secret `GITEA_TOKEN`. Linux: same with `build_linux.sh` on an `ubuntu` runner (build on the
   oldest distro you want to support — glibc).
3. A Windows VM on Proxmox used like option 1.

Release steps:
```
# 1. bump companion/version.py (VERSION = "0.5.0"), commit, tag v0.5.0, push the tag
# 2. Windows:  companion\packaging\build_windows.bat
#    (Linux:   companion/packaging/build_linux.sh)
#    source:   python3 -I companion/packaging/release.py source      (build_windows.bat already does this)
# 3. upload (token with write access, never printed):
set GITEA_TOKEN=…            (or: --token-file C:\path\token.txt)
py -3 -I companion\packaging\release.py upload --tag v0.5.0 --dry-run
py -3 -I companion\packaging\release.py upload --tag v0.5.0      [--stable] [--notes notes.md]
```
`release.py upload` refuses a tag that isn't `"v" + VERSION`, a tag that doesn't exist on the server yet, and any
archive without a matching `.sha256`. It creates the release (pre-release unless `--stable`) if needed and
replaces assets with the same name, so the Windows PC and a Linux runner can each upload their own archive to
the same release. Unsigned PyInstaller exes can trigger SmartScreen / antivirus warnings; a code-signing
certificate (`signtool sign` on `GSVR.exe` + `gsvr-cli.exe` before `release.py package`) fixes that later.

## Running it

```
# Windows player: double-click GSVR.exe (GUI). Console variant with the same options as companion.py:
gsvr-cli.exe [--game-root DIR] [--world-allow …]

# from source (Windows): GUI / CLI
pyw -3 companion\gui.py          (or companion\run-gui.bat)
py -3 -I companion\companion.py  (or companion\run-companion.bat)

# dev / test on the Mac (no Steam, no VRChat): manual game root + fake log dir
python3 -I companion/companion.py --game-root input/Half-Life --vrchat-log-dir work/vrclog --cache-dir work/cache
```

Options: `--game-root DIR` (manual override, skips Steam ownership), `--mods valve,cstrike` (restrict the
override), `--prebuilt-dir DIR` + optional `--prebuilt-mod MOD` (see below), `--steam-root DIR`, `--vrchat-log-dir DIR`, `--port auto|N` (see "Ports"), `--cache-dir DIR`
(default `%LOCALAPPDATA%\GSVR\cache`), `--converter tools/pack.py`, `--pack-cmd TEMPLATE`,
`--build-timeout 600`, `--world-allow wrld_…,wrld_…`, `--http-log FILE`.

### Ports

The port list has one source of truth, `PORT_CANDIDATES` in `companion.py`, mirrored by
`GsvrSceneBuilder.CompanionPorts` (the self-test checks that both are equal):

| candidate | port |
|---|---|
| 0 | **38427** |
| 1 | **41213** |
| 2 | **44391** |

They avoid 27000–27100 (Steam, and FlexLM license servers such as Autodesk's `lmgrd.exe`, which listens on
0.0.0.0:27080, the companion's old port) and the Windows dynamic range 49152+.

- `--port auto` (default) binds the **first free** candidate on 127.0.0.1. For each port it can't use it logs one
  line (`port 38427: in use — another program answers there …`, or for WinError 10013/10048 `port in use or
  reserved by Windows`) and tries the next. If another GSVR companion already answers on a candidate, it stops
  instead of starting a second one.
- `--port N` forces one port. If N is not a candidate, the world won't find it (a warning is logged).
- If no port works, it prints how to fix it and exits with **code 3** (no traceback):
  ```
  netstat -ano | findstr :38427          → PID in the last column
  tasklist /fi "PID eq <PID>"            → which program it is; close it, or reserve the port (cmd as admin):
  netsh int ipv4 add excludedportrange protocol=tcp startport=38427 numberofports=1
  netsh int ipv4 show excludedportrange protocol=tcp      (ranges Windows/Hyper-V/WSL already reserved)
  ```
  If `add excludedportrange` fails because Hyper-V/WSL holds the range: `net stop winnat`, add, `net start winnat`.
- Identity: `status.json` always has `"app": "gsvr-companion"`, `"proto": 1`, `"port": <bound port>`. The world and
  the GUI accept a status **only** if `app == "gsvr-companion"`; any other answer on a candidate port (another
  program, non-JSON, HTTP error) counts as "foreign program" and they try the next candidate.
- On Windows the server socket uses `SO_EXCLUSIVEADDRUSE` (not `SO_REUSEADDR`), so a port another program holds
  fails loudly instead of being shared.

### Prebuilt packs (`--prebuilt-dir`) — test clients without the game

```
# serve <mod>__<map>.bin files, e.g. cstrike__de_dust2.bin
py -3 -I companion\companion.py --prebuilt-dir D:\gsvr-packs
# serve bare <map>.bin files as one mod (the packs in work/cs/ as they are)
python3 -I companion/companion.py --prebuilt-dir work/cs --prebuilt-mod cstrike --vrchat-log-dir <dir>
```
The index and ownership come only from the file names (`<mod>__<map>.bin`, names `[A-Za-z0-9_.-]`, max 64
chars). Mods with packs count as "owned", every other known mod is denied. Other files are ignored, and the
status/HTTP protocol is the same. Each pack is re-checked every time it is served: magic `GSVn`, ≤ 100 MB,
and for GSV2 the header's section sizes must fit the file. A truncated or corrupt pack therefore turns into
`state: error` and is never served. This is a trust-the-operator mode for test VMs: the world cannot tell it
apart from a real install. `--prebuilt-dir` and `--game-root` are mutually exclusive.

Self-test: `./companion/selftest.sh` (or `python3 -I companion/selftest.py`); `--sabotage` swaps in a
converter that writes junk and a truncated prebuilt pack, and the test has to come out red. The
`--prebuilt-dir` part uses the 4 packs in `work/cs/` (`--prebuilt-src` to change that). The port part runs on
free ports (hidden option `--port-candidates a,b,c` replaces the list): candidate 0 held by a dummy HTTP server
with non-GSVR JSON → the companion must bind candidate 1; all busy → exit code 3 + the help text; status has
`app/proto/port`; the port list equals the scene builder's. GUI: `python3 -I companion/gui.py --selftest` (headless, never opens a window: finds the
companion on candidate 1, ignores the foreign server, keeps the exit-3 message for the red status line, then
update check + install through the Controller against a fake Gitea). Updater / release / frozen dispatch:
`python3 -I companion/selftest_update.py` (also part of `selftest.py`): version compare, newer/older/same/
pre-release/draft, private repo + token, missing asset / missing `.sha256`, bad checksum, path traversal,
manifest version mismatch, git checkout, mid-swap failure → rollback, frozen-layout swap with a "locked" backup,
tar.gz exec bits, release.py packaging + upload, `--run-converter` (incl. a real map build through a shim exe).
Frozen build: `gsvr-cli.exe --selftest`.

## Protocol between the world and the companion

VRCUrls can't be built at runtime, so the world bakes in a **fixed URL set per candidate port**
(`http://127.0.0.1:<38427|41213|44391>/…`). It loads `status.json` from candidate 0, 1, 2 until one answers with
`app == "gsvr-companion"`, then uses that port for everything (and searches again after a connection error).
Commands go the other way through the VRChat output log: Udon `Debug.Log` lines that the companion tails.

| URL | content | world usage |
|---|---|---|
| `GET /status.json` | `{app: "gsvr-companion", proto: 1, port, state: idle|building|ready|error, map, req, size, key, error, seconds, next:{…}, ping, world, world_ok, index_rev, seq, ownership:{mod: bool}}` | poll **at most once per 5 s** (VRChat string-load rate limit is shared by all URLs) |
| `GET /index.json` | `{rev, mods: {mod: {appid, name, maps: [{name, size, dir}]}}}` (~9 KB for valve+cstrike+czero) | fetch once per session, again when `status.index_rev` changes |
| `GET /pack/current.bin` | pack for the last `map=` request (GSVn binary) | after status shows `ready` with your `req` |
| `GET /pack/wait.bin` | long poll (W3VR): held until the build for the latest `map=` request is ready, at most `status.wait` s (`--wait-max`, default 20); then the pack + trailer `[status JSON][u32 LE length]["GSVW"]`, otherwise (still building, error, no request, world not allowed) the status JSON itself. For `war3/__glue` the trailer also has `index_json` (the map list). Other requests are served meanwhile (threaded server) | instead of a status poll when `status.wait` > 0; the world checks the trailer's `req`/`map` like a status (VRCUrls can't carry the req). Old companions answer 404 → poll status.json |
| `GET /pack/next.bin` | pack for the last `next=` (prefetch) request | e.g. load the next map of a map cycle |
| `GET /pack/models.bin` | weapon model + sound pack (GSM1) for the last `models=` request; `status.json` field `models: {state, mod, req, size, key, error, seconds}` | once per session, after `status.models.state == "ready"` with your `req` (see `docs/models_sounds.md`) |

Everything else gets 404. Only GET/HEAD are allowed. Packs that aren't ready yet return 503.

Log commands. The tag can appear anywhere in the line; tokens are `key=value` or bare words:
```
[GSVR-REQ] map=cstrike/de_dust2 req=17   build/serve as current; req is echoed in status.json
[GSVR-REQ] next=valve/c1a1 req=18        prefetch into the next slot
[GSVR-REQ] ping=8f3a                     echoed as status.ping (shows the companion is alive and reading the log, and how far behind it is)
[GSVR-REQ] rescan                        re-run the ownership check and the map index (after installing a game)
[GSVR-REQ] models=cstrike req=m4711      build/serve the weapon model+sound pack of an ALLOWED mod (status.models)
[GSVR-REQ] holo=valve maps=c1a0,c1a1 req=h3x81  build/serve the mod hub's map hologram bundle (decimated world faces of those installed maps, status.holo,
                                         GET /pack/hologram.bin, <= 10 MB; prebuilt mode: <mod>__hologram.gspv as is; companion/holo.py)
```
The model pack is converted from the same owned install (`models/{p,v,w}_*.mdl`, `sound/weapons`, `sound/items`)
by `tools/modelpack.py` (subprocess, `-X utf8 -I`, timeout), checked by `models.validate_models` (magic `GSM1`,
≤ 100 MB, header section sizes must equal the file size) and cached as
`models-<mod>-<sha1(input names/sizes/mtimes)>-<converter version>.gsm`. Options: `--model-converter`,
`--models-cmd` (placeholders `{python} {converter} {game_root} {mod} {out}`). In `--prebuilt-dir` mode the
companion serves `<prebuilt-dir>/<mod>__models.gsm` (validated) for mods that have map packs there.
The world should pick a fresh `req` id per request (for example a counter plus a random value) and only
fetch `current.bin` once `status.req == its id && status.state == "ready"`. A typical load sequence:
log `map=…` → wait 5 s → poll status (`building`) → … → `ready` → wait 5 s → fetch the pack. The
5 s limit on string loads is the main source of latency, not the build itself (a cached pack is
served in about 2 ms).
The companion also tracks `[Behaviour] Joining wrld_…` lines, so it knows which world is running.

The cache key is `<mod>-<map>-<sha1(bsp)[:16]>-<converter version>`. The converter version is the
`--version` output if `pack.py` supports it, otherwise a sha1 over `tools/*.py` plus the command
template, so changing the converter invalidates the cache. Output is written to `.tmp` and renamed into
place, then checked: magic `GSV?` and size ≤ 100 MB (the VRChat limit). The converter command is detected
from `pack.py --help`: `--game-root` and `--mod` are added when the help lists them. You can override it
with `--pack-cmd` (placeholders `{python} {converter} {bsp} {out} {mod} {map} {game_root} {mod_dir}`; each
placeholder becomes one argv element and no shell is involved; write literal paths with forward slashes).

## Ownership gate

**v0 (implemented):** a mod is served only if
1. `appmanifest_<appid>.acf` for the app that ships it exists in one of the Steam libraries
   (from `steamapps/libraryfolders.vdf`, both the current and the legacy format), and
2. its `StateFlags` has bit **4** (`StateFullyInstalled`) set and bit 1 (`StateUninstalled`) clear.
   Typical values: 4 = installed, 6 = installed with an update pending, 1026 = updating. Other bits:
   2 UpdateRequired, 32 FilesMissing, 256 UpdateRunning, 512 UpdatePaused, 1024 UpdateStarted…
3. the mod folder exists in `steamapps/common/<installdir>/`.

Map table: 70 → `valve`, 10 → `cstrike`, 80 → `czero`, 225840 → `svencoop`, 20 → `tfc`, 30 → `dod`,
40 → `dmc`, 50 → `gearbox`, 130 → `bshift`. For each mod, maps are listed from `<mod>_addon/maps`,
`<mod>/maps` and `<mod>_downloads/maps`, in that order; when the same name appears twice, the first
wins. The world can only ask for names that are in the index, and request text is never joined into a
file path. Note: a CS-only owner also has a `valve/` folder (shared base files), but without
`appmanifest_70` its maps are **not** served.
`--game-root` is a manual override that treats every known mod folder as allowed. It is meant for
testing and non-Steam layouts, and is clearly a trust-the-user mode.

**v1 (design, not implemented): Steamworks `ISteamApps::BIsSubscribedApp`.**
- Ship `steam_api64.dll` (Steamworks SDK redistributable) next to the companion, plus a `steam_appid.txt`.
  Python can call it through stdlib `ctypes` using the flat API: `SteamAPI_InitFlat` (or `SteamAPI_Init`),
  `SteamAPI_SteamApps_v008()` → `SteamAPI_ISteamApps_BIsSubscribedApp(apps, appid)`; also
  `BIsAppInstalled`, `GetAppInstallDir`, and `BIsSubscribedFromFreeWeekend` to exclude free-weekend
  licences. Then `SteamAPI_Shutdown()` straight away, so the "in game" status disappears.
- Which app ID to initialise with: init only works for an app ID the user is subscribed to. Options:
  (a) **our own Steam app ID**, a free "tool" on Steam (Steam Direct fee). This is the clean option and
  also gives signed distribution and auto-update. (b) app 480 (Spacewar, the SDK sample). Works for
  everyone, but friends see "playing Spacewar" and it's a ToS grey area for a shipped product.
  (c) the game's own ID (70/10/…). Init fails for non-owners, which is itself a check, but friends see
  "playing Half-Life" and it can collide with a running hl.exe.
- Combined rule for v1: `BIsSubscribedApp(id) && !BIsSubscribedFromFreeWeekend()` **and** the v0
  manifest check, and `GetAppInstallDir(id)` must match the folder we read from. Family Sharing
  counts as subscribed, which is intended. Sven Co-op is free, so for it "owned" just means installed.
- Caveats: Steam has to be running and logged in as the same user. Any local check can be patched or
  faked by a determined user. This is an honest gate (no Valve files in the world, each player's own
  install converted locally), not DRM. Offline mode works with cached licences.

## Karten-Relay (Karten automatisch vom Host holen)

Wenn der Host eine Warcraft-III-Karte spielt, die du nicht hast (zum Beispiel eine Fan-Karte), kann dein GSVR sie
automatisch holen, wie im Originalspiel. Es wird nur die Kartendatei (`.w3x`/`.w3m`) uebertragen, nie Blizzard-Daten:
jeder baut das Paket aus seiner eigenen Warcraft-III-Installation.

- Einstellungen, Feld **Map relay URL**: Adresse des Relays eintragen (`https://...`). Leer = Funktion aus (Standard).
- **Download missing maps**: `on` = fehlende Karten automatisch laden (Standard), `ask` = vorher nachfragen,
  `off` = nie laden. Hosten (Hochladen deiner eigenen Fan-Karten) laeuft automatisch, sobald eine URL eingetragen ist;
  Blizzard-Karten werden nie hochgeladen. Auf dem Relay bleiben Karten 24 Stunden.
- Geteilte Karten behalten ihren Originalnamen und liegen danach auch im echten WC3 unter Maps/Download/GSVR.
- Geladene Karten liegen in `<Warcraft III>\Maps\Download\GSVR\` (oder im GSVR-Datenordner, falls dort nichts
  geschrieben werden darf). Die Datei wird vor der Benutzung geprueft (Pruefsumme, Format, Groessenlimits).
- Im Spiel zeigt das Brett "Karte wird vom Host geladen..." mit Prozentanzeige. Klappt es nicht (Host hat nicht
  geteilt, Relay nicht erreichbar, nach 60 s), erscheint die bisherige Meldung "nicht gefunden".

## Map relay (technical)

Source: `companion/mapshare.py`, service in `relay/` (own README: API, limits, deployment).

- Settings (`%APPDATA%\GSVR\settings.json` via the GUI, or CLI): `relay_url` -> `--relay-url URL` (default empty =
  off, https only, plain http just for loopback test relays) and `map_autodownload` -> `--map-autodownload on|ask|off`
  (default `on`, only effective with a relay URL).
- **Share** (host): after a war3 map with a content hash (`sha-<16 hex>` key, i.e. fan / user maps) was served,
  a background thread does `HEAD /v1/map/<sha256>` and, if 404, `PUT`s the file. Never uploaded: CASC-internal maps,
  files below `<install>/Maps/{FrozenThrone,Scenario,Test,Campaigns}` (and the same folders beside Maps), and any
  file whose SHA-256 equals a stock map of an install (computed from those folders, so renamed copies are caught;
  extra hashes can be listed one per line in `<cache dir>/war3_stock_sha.txt`). Files over 50 MB or without
  HM3W / MPQ magic are skipped. Failures never reach the build or the status. Maps keyed by file name (listed by an
  install's own scan) are not shared because guests can only ask for `sha-` keys.
- **Fetch** (guest): `State.worker` -> `resolve()` raises "not found" for `war3/sha-<16 hex>` and relay + autodownload
  are set -> `fetch_from_relay()`: `HEAD /v1/map/<hex16>` every 2 s for up to 60 s (the relay resolves a unique
  16-hex prefix), `GET`, SHA-256 must start with the key, `validate_map()`, atomic move to
  `<install>/Maps/Download/GSVR/<original name> [<sha8>].w3x` (fallback `<cache dir>/mapdl/`, scanned as a fan folder), rescan,
  then the normal build. The original name comes from the relay's `Content-Disposition` (sent by the host via
  `X-Map-Name`), is sanitized again here with the same rules as the relay, and the file is `sha-<sha16>.w3x` when there is
  no valid name. The index keys the file by content (`sha-<sha16>`): a name with space/brackets is never a safe name
  key, so the MPQ install listing skips it and the content scan indexes it (test: `Names` in test_mapshare.py). On any failure the old "map ... not found" error stays and the same
  key is not retried for 15 s. A newer request on the slot cancels a running fetch.
- **Status while fetching** (`/status.json`, also in the `/pack/wait.bin` trailer): `state: "fetching"`, `percent`
  (0-100), `phase` (`"ask"` = waiting for the player's yes/no in ask mode, `"waiting"` = polling for the host's
  upload, `"download"` = transferring), plus `ask: ["<hex16>"]` while a prompt is pending. `percent`/`phase` are
  `null` in every other state. `wait.bin` treats `fetching` as busy (held up to `status.wait`).
- **Ask mode**: the GUI sees `status.ask`, shows one yes/no dialog per sha and answers with
  `POST /ask?sha=<hex16>&answer=yes|no` (loopback only, Host check, header `X-GSVR-Ask: 1`). No answer within 120 s
  counts as "not now" (asked again on the next request); "no" is remembered until the companion restarts.
- **Untrusted files**: download cap 50 MB; `validate_map()` checks magic, reads the MPQ tables, rejects declared file
  sizes above 128 MB / 512 MB total or a ratio over 2000:1 on files above 1 MiB, and reads every file through
  `tools/w3mpq.py` with zlib / bz2 output limited to 64 MB per block (`harden_mpq()` swaps in bounded decompressors
  at runtime, the synced `tools/` file is untouched; PKWARE explode and Huffman paths were already bounded by the
  sector size). Damaged or unsupported single files do not reject a map (protected maps exist), a bomb does.
  Note: the converter subprocess uses the unpatched `w3mpq` of the warcraft3 repo; it only ever sees files that
  passed `validate_map()`, but the same bounded `zlib`/`bz2` calls should be adopted upstream (`tools/mpq.py`).
- Tests: `companion/test_mapshare.py` and `relay/test_relay.py` (both run from `selftest.sh`).

## Security notes

- The HTTP server binds to **127.0.0.1 only** (never 0.0.0.0) and answers only loopback clients, so it is
  invisible to the LAN and doesn't trigger a Windows firewall prompt.
- Requests whose `Host` isn't `127.0.0.1`/`localhost` (with optional port) get 421. This defends
  against DNS rebinding, where a web page in your browser resolves its own domain to 127.0.0.1.
- Fixed routes only: no directory listing, no file paths from the client, and map names are looked up
  in the index (`[A-Za-z0-9_.-]`, max 64 characters).
- Every request is logged with its User-Agent (stdout, plus `--http-log` as TSV). VRChat shows up as
  `UnityPlayer/… (UnityWebRequest/1.0, libcurl/…)`.
- **Any** VRChat world can reach 127.0.0.1 once the player has turned on *Allow Untrusted URLs*, and any
  world can write `[GSVR-REQ]` lines. A hostile world could fetch packs (derived from Valve content) and,
  in theory, sync them to other players. Mitigation: `--world-allow wrld_<our id>`. Log commands from
  other worlds are then ignored, and `index.json`/`pack/*` return 403 while the current world (from the
  log) isn't allowlisted. v1 should ship with the official world ID as the default allowlist.
- The converter runs as a subprocess with `-I`, `stdin` closed and a timeout. Its output is checked
  (magic, size ≤ 100 MB) before it is served.

## Player-facing steps

1. Own the game(s) on Steam and install them (Half-Life, Counter-Strike, CS: Condition Zero, Sven Co-op …).
2. Download `gsvr-companion-windows-x64.zip` from the release page, unpack it, for example to `Documents\GSVR`.
3. Double-click `GSVR\GSVR.exe` and press **Start**. The Status tab lists the games it found.
   (SmartScreen may warn about an unknown publisher until the exe is code-signed: "More info" → "Run anyway".)
4. In VRChat: Settings → search for **"Allow Untrusted URLs"** and turn it on (one-time setting).
5. Join the GSVR world and pick a map on the panel. The first load of a map converts it (seconds);
   after that it comes from the cache.
6. Updates: when the yellow banner appears, press **Install** — no re-download needed.
7. Close the window to stop. Nothing is installed system-wide; delete the folder to uninstall (plus
   `%LOCALAPPDATA%\GSVR` for the cache and `%APPDATA%\GSVR` for the settings).

## Half-Life / Counter-Strike ohne Steam (Anleitung)

Hat dein Counter-Strike 1.6 / Half-Life keine Steam-Version (zum Beispiel eine alte CD- oder Installer-Version),
findet GSVR es meistens nicht von selbst. So gibst du den Ordner an:

1. GSVR starten, Reiter **Settings**, ganz oben **Half-Life / Counter-Strike Ordner**.
2. **Suchen** druecken: GSVR sucht auf allen festen Laufwerken nach Ordnern mit `hl.exe` (oder `hlds.exe`) und
   einem `valve`-Ordner. Treffer erscheinen in der Liste, anklicken uebernimmt den Ordner.
   Oder **Browse...** und den Ordner waehlen, in dem `hl.exe` liegt (der Ordner mit `valve` und `cstrike`).
3. Unter dem Feld steht, welche Mods gefunden wurden (zum Beispiel `cstrike, valve`). **Pruefen** zeigt das erneut.
4. **Start** druecken (bei laufendem GSVR erst Stop, dann Start). Im Log steht am Anfang, was gefunden wurde.

Nur mit eigener, legal erworbener Kopie. Ohne Steam kann der Besitz nicht geprueft werden. Ist das Feld leer,
bleibt alles wie bisher: Steam wird automatisch gefunden (auch mit eigener Installation, Registry, anderen
Laufwerken und zusaetzlichen Steam-Bibliotheken).

## GAME-FOLDERS (Tab "Spiele")

Der Tab **Spiele** im GSVR-Fenster zeigt pro Spiel (Half-Life, Counter-Strike, Sven Co-op, weitere GoldSrc-Mods, Warcraft III) den gefundenen Ordner und wie er gefunden wurde (Steam, Registry, Standardordner, Ordner-Suche, manuell). Mit **Ändern…** wählst du den Ordner selbst (Half-Life/GoldSrc: Einstellung `hl_root`, gilt für alle GoldSrc-Spiele; Warcraft III: `war3_root`), **Zurücksetzen (automatisch)** löscht ihn wieder. Nicht gefundene Spiele zeigen "nicht gefunden" mit Hinweis. Die Liste kommt aus `status.json` (`games`, gleiche Erkennung wie der Companion); läuft der Companion nicht, rechnet das Fenster sie lokal mit denselben Funktionen (`games.py`). Nach einer Änderung den Companion neu starten.

`status.json` `games`: Liste von `{id, name, found, root, source, mods, setting, hint[, version, maps_root]}`; `source` = `steam-manifest` | `manual` | `registry` | `install-folder` | `non-steam-scan` (gefunden, aber unbestätigt, wird nicht ausgeliefert) | `none`. Tests: `python3 -I companion/test_hl_detect.py`, `gui.py --selftest`.

## HL-DETECT (technical)

- `steam.py` reaches the registry and the drive list only through `steam._registry` (`read(hive, key, value)`,
  `subkeys(hive, key)`; hive `"HKCU"`/`"HKLM"`) and `steam.fixed_drives()`; tests replace both, so they run on macOS.
- Steam roots (`find_steam_roots()`, first hit = `find_steam_root()`): `HKCU\Software\Valve\Steam` `SteamPath` / `SteamExe`,
  `HKLM\SOFTWARE\WOW6432Node\Valve\Steam` and `HKLM\SOFTWARE\Valve\Steam` `InstallPath`, the default
  Program Files (x86) folder, then on every fixed drive `Steam`, `Program Files (x86)\Steam`, `Program Files\Steam`,
  `Games\Steam`, `SteamLibrary`, `Games\SteamLibrary`. A root counts if it contains `steamapps\`; all
  `libraryfolders.vdf` libraries are used as before. Ownership gate unchanged.
- Non-Steam scan `detect_non_steam()`: candidate = folder with `hl.exe` or `hlds.exe` (case-insensitive) AND a `valve/`
  folder. Sources: registry uninstall entries (`InstallLocation`, `DisplayIcon`, HKLM/HKCU, 32/64-bit views) and a
  folder walk on all fixed drives: below `Program Files (x86)`, `Program Files`, `Games`, `Spiele` every folder up to
  depth 3 (counted from the drive root), at the drive root only names like `Counter-Strike*`, `Half-Life*`, `CS 1.6*`,
  `Valve*`, `hlds*`. Skips `steamapps`, `Windows`, recycle bin etc.; unreadable folders are skipped; one hard time
  budget of 3 s (`SCAN_BUDGET_S`) across everything; result cached per process (`force=True` rescans).
  Results are suggestions with `source: "detected-non-steam"`; they are never served automatically. The GUI
  (**Suchen**) lists them; picking one stores it as `hl_root` in `gui.json`.
- `hl_root` (GUI setting) -> `--game-root <dir>` as one argument (spaces kept). That is the existing manual override
  (`source: "manual-override"`, mods present in the folder are allowed, no ownership check). Empty = Steam detection.
- `companion.py` logs a discovery report at start (`steam.discovery_report`: every place tried, libraries, our
  `appmanifest_*.acf` files; for `--game-root` the mods present). If `cstrike` ends up not allowed it also runs the
  non-Steam scan, logs the candidates (not served) and the hint
  `Ordner in der GSVR-Oberflaeche unter 'Half-Life / Counter-Strike Ordner' angeben`.
- Tests: `python3 -I companion/test_hl_detect.py` (also run by `selftest.py`); GUI argv checks in `gui.py --selftest`.

## Plan for v1

- Code-sign `GSVR.exe` / `gsvr-cli.exe` (OV/EV certificate) — unsigned PyInstaller binaries trip SmartScreen
  and some antivirus heuristics. (Alternative kept in mind: a C#/.NET 8 rewrite with a self-contained
  single-file publish; the converter would then have to be ported or bundled.)
- A public update feed (internet-reachable Gitea or a public release repo) instead of the LAN server.
- Tray icon (status: idle/building/ready, last request, "open cache folder", quit).
- Optional autostart: a shortcut in `shell:startup`, or start the companion with VRChat via a Steam
  launch option wrapper. Off by default.
- Default `--world-allow` set to the official world ID (the GUI already pre-fills it), the Steamworks ownership check.
- Linux build in CI (`build_linux.sh` exists; needs a runner and a test on a real desktop).
