# Windows release build without a Windows PC (Wine LXC)

`build_windows.bat` runs unchanged under Wine. Set up 2026-10-24 as LXC 171 `gsvr-winbuild` on a Proxmox
host (Debian 12, unprivileged, 4 cores / cpulimit 2 / cpuunits 512, 4 GB, 20 GB on local-lvm, DHCP).
Used when the Windows PC is off; it produced v0.7.8.

## Setup (once)

1. WineHQ stable (Wine 11; Debian's Wine 8 lacks `propsys.VariantToString`, which the 3.12 venv launcher needs):
   `dl.winehq.org` key + `winehq-bookworm.sources`, then `apt install --install-recommends winehq-stable xvfb xauth`.
2. Python 3.12.10 from the per-component MSIs (the bundle installer does not run under Wine):
   `https://www.python.org/ftp/python/3.12.10/amd64/{core,exe,lib,tcltk,dev}.msi`, each
   `wine msiexec /i X.msi TARGETDIR=C:\Python312 /qn`, then `C:\Python312\python.exe -m ensurepip`.
3. Source as `git archive` under `C:\gsvr`, then `C:\Python312\python.exe -m venv companion\build\venv-win` and
   `pip install -r companion\packaging\requirements-build.txt`.
4. Own bootloader (no MSVC here): `apt install gcc-mingw-w64-x86-64`, unpack the pyinstaller-6.11.1 sdist, in
   `bootloader/`: `CC=x86_64-w64-mingw32-gcc AR=x86_64-w64-mingw32-ar WINDRES=x86_64-w64-mingw32-windres
   STRIP=x86_64-w64-mingw32-strip python3 ./waf --target-arch=64bit --gcc distclean configure all`, copy
   `PyInstaller/bootloader/Windows-64bit-intel/*.exe` into the venv's PyInstaller and write
   `venv-win\bootloader-compiled.txt` (the bat then skips its MSVC step).
5. Inno Setup 6.7.3 (`github.com/jrsoftware/issrc/releases`), `/VERYSILENT /CURRENTUSER`; lands where the bat looks.

## Build

    cd /root/.wine/drive_c/gsvr && xvfb-run -a nice -n 10 wine cmd /c companion\\packaging\\build_windows.bat

Artifacts in `companion/build/release/`. Check, v0.7.8: all self-tests PASS under Wine, and the frozen
`gsvr-cli.exe --run-converter tools\w3pack.py` built Wintermaul byte-identical to the macOS build (77 s, parallel
bake). Not run on real Windows before that release.
