# Code signing and how to verify a download

The Windows binaries of the GSVR Companion (`GSVR-Setup.exe`, and `GSVR.exe` / `gsvr-cli.exe` inside
`gsvr-companion-windows-x64.zip`, published on the [Releases](https://github.com/Devilblader87/gsvr-companion/releases)
page) are **not code-signed**. There is no code-signing certificate for this project.

## What to expect from Windows

Because the app is unsigned and new, Windows SmartScreen may show "Windows protected your PC" when you start the installer
or the app. This is expected. If you trust the download (see below), click **More info** and then **Run anyway**.
Some antivirus programs may also warn about unsigned, newly built programs.

## How to verify a download

* **SHA-256 checksum.** Every release file has a matching `.sha256` file (for example `GSVR-Setup.exe.sha256`) attached to
  the release. Compare it with the hash of your download, for example in PowerShell:
  `Get-FileHash .\GSVR-Setup.exe -Algorithm SHA256`. The two hashes must be identical. The companion's self-updater
  refuses archives without a matching checksum.
* **Built from public source.** The binaries are built by the GitHub Actions workflow
  [`.github/workflows/build.yml`](.github/workflows/build.yml) on a GitHub-hosted runner from the tagged public source of
  this repository; the build log is public.
* **Build it yourself.** The full source is in this repository. Build instructions and the build command are in
  `companion/packaging/` (`build_windows.bat`); you can build the app yourself and use that instead of the download.

## Privacy policy

The GSVR Companion does not collect or transmit personal data, has no telemetry, analytics or crash reporting, and sends no
identifiers. It only contacts the following servers (and nothing else):

1. **Your own PC (`127.0.0.1`, loopback only).** The companion serves the converted game data to VRChat on the local machine.
   The server binds to 127.0.0.1 and rejects other clients and foreign `Host` headers; it is not reachable from the network.
2. **`raw.githubusercontent.com`:** the list of official VRChat world IDs
   (`https://raw.githubusercontent.com/Devilblader87/gsvr-companion/main/worlds.txt`), read when the app starts.
3. **GitHub releases (update check):** `api.github.com` (release list of this repository) and, when you choose to install an
   update, the release assets from `github.com` (and the GitHub download hosts it redirects to). Can be switched off in
   Settings ("check for updates on start"). The update server can be changed in Settings; an optional read token is only
   sent to that configured server.
4. **Map relay (Warcraft III / Counter-Strike custom maps), default `https://w3relay.heimserver.me`:** operated by the
   project author. If you host a game with a fan-made (non-Blizzard, non-Valve-stock) map, the companion uploads that map
   file (identified by its SHA-256) so your friends' companions can download it; a guest's companion downloads the map file
   the host shared. Blizzard's own maps and game data are never uploaded. The relay URL can be changed or emptied in Settings
   (empty = feature off) and automatic download can be set to "ask" or off. The relay sees the IP address of the connecting
   client like any web server.

The companion reads game files only from game installations on your PC and converts them locally; it does not upload them.
(The converter scripts in `tools/` use no network functions at all.)

## Reporting problems

Please open an issue on this repository.
