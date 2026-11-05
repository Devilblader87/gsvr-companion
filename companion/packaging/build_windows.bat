@echo off
rem Builds the Windows release of the GSVR companion:  companion\build\dist\GSVR\{GSVR.exe, gsvr-cli.exe, _internal\}
rem and the release archives in companion\build\release\ (gsvr-companion-windows-x64.zip + .sha256,
rem gsvr-companion-source.zip + .sha256).  Uploading is a separate, manual step (see companion\README.md "Releases").
rem Needs: 64-bit Python 3.10+ from python.org (with Tcl/Tk, i.e. the default installer options) and internet for pip.
setlocal
rem self-tests print arrows etc.; a redirected console would be cp1252
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0..\.."
set "BUILD=companion\build"
set "VENV=%BUILD%\venv-win"

if not exist "%VENV%\Scripts\python.exe" (
  rem pinned PyInstaller 6.11.1 needs Python < 3.14: prefer 3.13 / 3.12 when the newest Python is too new
  py -3.13 -m venv "%VENV%" 2>nul || py -3.12 -m venv "%VENV%" 2>nul || py -3 -m venv "%VENV%" || goto :fail
)
"%VENV%\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r companion\packaging\requirements-build.txt || goto :fail
rem 1b. PyInstaller bootloader compiled here (MSVC Build Tools) instead of the prebuilt one: the prebuilt loader is
rem     shared with lots of malware and trips antivirus heuristics; ours is unique. Done once per venv.
if exist "%VENV%\bootloader-compiled.txt" goto :bootloader_ok
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
if not exist "%VSWHERE%" ( echo MSVC Build Tools not found - using the prebuilt PyInstaller bootloader & goto :bootloader_ok )
set "PYINSTALLER_COMPILE_BOOTLOADER=1"
"%VENV%\Scripts\python.exe" -m pip install --disable-pip-version-check -q --force-reinstall --no-deps --no-binary pyinstaller -r companion\packaging\requirements-build.txt || goto :fail
echo compiled> "%VENV%\bootloader-compiled.txt"
:bootloader_ok

rem 1. stdlib self-tests from source first (headless, no window)
"%VENV%\Scripts\python.exe" -I companion\selftest_update.py || goto :fail

rem 2. PyInstaller one-folder build
"%VENV%\Scripts\python.exe" -m PyInstaller --noconfirm --clean --distpath "%BUILD%\dist" --workpath "%BUILD%\work" companion\packaging\gsvr.spec || goto :fail

rem 3. smoke tests of the frozen build: version, converter dispatch, headless GUI-controller selftest
"%BUILD%\dist\GSVR\gsvr-cli.exe" --version || goto :fail
"%BUILD%\dist\GSVR\gsvr-cli.exe" --run-converter "%BUILD%\dist\GSVR\_internal\tools\modelpack.py" --version || goto :fail
"%BUILD%\dist\GSVR\gsvr-cli.exe" --selftest || goto :fail

rem 4. archives + checksums (no upload)
"%VENV%\Scripts\python.exe" -I companion\packaging\release.py package --platform windows-x64 || goto :fail
"%VENV%\Scripts\python.exe" -I companion\packaging\release.py source || goto :fail
rem 5. installer (Inno Setup 6, per user, no admin): companion\build\release\GSVR-Setup.exe + .sha256
set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" ( echo Inno Setup 6 not found - skipping the installer & goto :done )
for /f "delims=" %%v in ('"%BUILD%\dist\GSVR\gsvr-cli.exe" --version') do set "GSVRVER=%%v"
"%ISCC%" /Q /DAppVersion=%GSVRVER% /DDistDir="%CD%\%BUILD%\dist\GSVR" /DOutDir="%CD%\%BUILD%\release" companion\packaging\gsvr_setup.iss || goto :fail
"%VENV%\Scripts\python.exe" -I -c "import hashlib,sys;p=sys.argv[1];open(p+'.sha256','w',newline='\n').write(hashlib.sha256(open(p,'rb').read()).hexdigest()+'  GSVR-Setup.exe\n')" "%BUILD%\release\GSVR-Setup.exe" || goto :fail
:done
echo.
echo OK - archives in %BUILD%\release\
echo Upload:  py -3 -I companion\packaging\release.py upload --tag v^<VERSION^>   (GITEA_TOKEN must be set)
exit /b 0

:fail
echo.
echo BUILD FAILED (exit code %errorlevel%)
exit /b 1
