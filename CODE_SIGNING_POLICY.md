# Code signing policy

Free code signing provided by [SignPath.io](https://about.signpath.io/), certificate by
[SignPath Foundation](https://signpath.org/).

This policy applies to the Windows binaries of the GSVR Companion published on the
[Releases](https://github.com/Devilblader87/gsvr-companion/releases) page of this repository:
`GSVR-Setup.exe` and the executables `GSVR.exe` / `gsvr-cli.exe` inside `gsvr-companion-windows-x64.zip`.

## What is signed

* Only binaries built from the source in this repository by the GitHub Actions workflow
  [`.github/workflows/build.yml`](.github/workflows/build.yml) on a GitHub-hosted runner, from a version tag
  (`v*`). The signing request is submitted by that workflow and SignPath verifies the origin (repository, commit,
  workflow run) of the uploaded artifact.
* The signed files are `GSVR.exe`, `gsvr-cli.exe` and the installer `GSVR-Setup.exe`
  (configurations: [`.signpath/initial.xml`](.signpath/initial.xml), [`.signpath/installer.xml`](.signpath/installer.xml)).
* Third-party files bundled by PyInstaller (the Python runtime, Tcl/Tk, Pillow) keep their own signatures or are unsigned;
  they are not re-signed with our certificate.
* Every release asset has a `.sha256` file; the companion's self-updater refuses archives without a matching checksum.

## Roles

| Role | Member (GitHub user) |
| --- | --- |
| Author (writes the code) | [Devilblader87](https://github.com/Devilblader87) |
| Committer (commits to the main branch and creates release tags) | [Devilblader87](https://github.com/Devilblader87) |
| Reviewer (reviews changes before they are released) | [Devilblader87](https://github.com/Devilblader87) |
| Approver (approves each signing request) | [Devilblader87](https://github.com/Devilblader87) |

Signing requests are approved manually by the approver in SignPath; nothing is signed automatically. This is a one-person
project; if more people join, this table is updated first.

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

Please open an issue on this repository. If you believe a signed binary is malicious or was signed by mistake, report it
there as well and write to the SignPath Foundation (<https://signpath.org/>).
