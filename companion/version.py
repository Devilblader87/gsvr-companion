"""Single source of truth for the GSVR companion version and the update server defaults.

companion.py (status.json "companion"), gui.py (title, update check), updater.py (version compare),
packaging/release.py (archive names, tag check) and the PyInstaller build all read VERSION from here.
Bump it before tagging a release: the tag must be "v" + VERSION (release.py refuses anything else).
"""
VERSION = "0.9.28"

# Release feed the self-updater reads: GitHub (public) by default; a Gitea server URL can be set in Settings.
DEFAULT_UPDATE_BASE = "https://github.com"      # public releases (no home server involved); a Gitea URL works too
REPO_OWNER = "Devilblader87"
REPO_NAME = "gsvr-companion"      # PUBLIC release-only repo (no login needed); the source repo goldsrc-vrchat stays private
