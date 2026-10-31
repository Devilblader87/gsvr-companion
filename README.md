# GSVR Companion

Play your own copy of **Counter-Strike 1.6, Half-Life, Sven Co-op and Warcraft III** in VRChat: the companion converts maps, models and sounds from the games installed on **your** PC.

**Download & instructions:** https://devilblader87.github.io/gsvr-companion/

* Releases: see the [Releases](https://github.com/Devilblader87/gsvr-companion/releases) page. The companion updates itself from here.
* Worlds: GSVR - Counter-Strike, GSVR - Half-Life, GSVR - Sven Co-op and **Warcraft III** (melee, tower defense and other custom maps on a table, multiplayer with your friends).
* The VRChat worlds contain no game files. The companion reads your install, converts it on your PC and serves it to VRChat on `127.0.0.1` only.
* The "Spiele" tab shows for every game where it was found (Steam, registry, folder scan or set by hand) and lets you change the folder. Half-Life / Counter-Strike are found through Steam; installs without Steam or in other folders can be picked there.
* Warcraft III: classic installs 1.21 – 1.31 (your own copy). **Map sharing:** when the host plays a fan map a friend does not have, only that map file is passed on through the GSVR relay and saved in the friend's `Warcraft III\Maps\Download\GSVR` (it also works in the real game). Blizzard's own maps and game data are never sent. Can be switched off in the settings ("Karten vom Host automatisch laden"). The game's texts (e.g. the end-of-game dialog) come from the string tables of your own install. After a companion update the maps are converted again by themselves (0.9.10: pack format w3p1-72 and many game rules from the 1.26a reference — stunned units keep their order, Footman Frenzy rally points, map-script `and` short-circuits, computer AI natives; 0.9.9: classic melee maps — workers visible, the build menu shows all buildings, gold / lumber harvest works, fog of war is on, and a simple computer opponent: in the lobby click an "Open" row → "Computer (easy)").
* PC (Windows) VRChat only. Needs the VRChat setting *Allow Untrusted URLs*.

Fan project, not affiliated with Valve, Blizzard Entertainment or VRChat. Counter-Strike and Half-Life are trademarks of Valve Corporation; Warcraft is a trademark of Blizzard Entertainment.
