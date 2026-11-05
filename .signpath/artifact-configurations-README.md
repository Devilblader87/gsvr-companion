# SignPath artifact configurations

The XML files next to this file are the artifact configurations the build workflow
(`.github/workflows/build.yml`) expects in the SignPath project `gsvr-companion`. Paste each one into
SignPath (Project > Artifact configurations) with the slug named below.

| file | slug | what is signed |
| --- | --- | --- |
| `initial.xml` | `initial` | `GSVR.exe` and `gsvr-cli.exe` of the PyInstaller build (uploaded as one GitHub artifact) |
| `installer.xml` | `installer` | `GSVR-Setup.exe` (Inno Setup installer built from the signed executables) |
