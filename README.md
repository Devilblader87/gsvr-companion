# GSVR Companion

Play your own copy of **Half-Life** (+ Opposing Force, Blue Shift, Ricochet, DMC), **Counter-Strike 1.6**, **Sven Co-op**, **Warcraft III**, **ROSE Online** and **Ragnarok Online** in VRChat. Bring your own game: the companion converts maps, models and sounds from the games installed on **your** PC.

**[Download & instructions](https://devilblader87.github.io/gsvr-companion/)** | **[Latest release](https://github.com/Devilblader87/gsvr-companion/releases/latest)** | **[Discord](https://discord.gg/nc2rw6Snx)** | [Changelog](CHANGELOG.md)

## Quick start

1. Download `GSVR-Setup.exe` from the [latest release](https://github.com/Devilblader87/gsvr-companion/releases/latest) and install it.
2. Start the companion. Your games are found automatically; press **Start** (1-click setup) to build what is needed.
3. In VRChat: **Settings -> Comfort & Safety -> Allow Untrusted URLs: on**.
4. Press "Open world in VRChat" (or use a link below) and join.

PC (Windows) VRChat only. Linux / Steam Deck: see [Proton](#linux--steam-deck-proton). The companion UI is available in English, Japanese and German.

## Worlds

| World | Game you need | Status | Link |
|---|---|---|---|
| GSVR - Half-Life (campaign, mod hub with Opposing Force, Blue Shift, Ricochet, DMC, Sven Co-op) | Half-Life (+ mods / Sven Co-op) | alpha | [open](https://vrchat.com/home/world/wrld_b0402df7-7bfb-44b2-9406-7aafd8e67e1a) |
| GSVR - Counter-Strike | Counter-Strike 1.6 | alpha | [open](https://vrchat.com/home/world/wrld_5ab8566b-7e6a-4cfb-a0ac-b6d099b94ffb) |
| Warcraft III | Warcraft III 1.21 - 1.31 | alpha | [open](https://vrchat.com/home/world/wrld_71563253-4115-40d3-b165-7cdba23bff06) |
| GSVR - ROSE Online ローズオンライン | ROSE Online | alpha | [open](https://vrchat.com/home/world/wrld_3a03fa73-b6c8-4116-8785-40fbe353b9b2) |
| GSVR - RO (Ragnarok Online) | Ragnarok Online (your own client install) | alpha | [open](https://vrchat.com/home/world/wrld_9b3f4e50-27c3-4d0e-b6d6-394957a6748c) |

Each world only needs the game it is built from. A banner at each world's spawn shows its current status and changes.

## How it works

The companion reads your game install, converts maps, models and sounds on your PC and serves them to VRChat on `127.0.0.1` only. The VRChat worlds contain no game files, and nothing is downloaded. Nothing is shared, except optionally Warcraft III fan maps (and Counter-Strike custom maps) between friends via the GSVR relay; Blizzard's own maps and game data are never sent, and this can be switched off.

## Per-game notes

**Half-Life family.** Half-Life, Opposing Force, Blue Shift, Ricochet and DMC are found through Steam. Installs without Steam or in other folders can be picked in the **Games** tab (Advanced view). Each mod uses your own install; models Steam does not ship get stand-ins.

**Counter-Strike.** Counter-Strike 1.6 is found through Steam; custom maps are shared through the relay.

**Sven Co-op.** Found through Steam; its co-op maps (Sven weapons, monsters and allies) are played in the Half-Life hub world.

**Warcraft III.** Classic installs 1.21 - 1.31 (your own copy). Melee, tower defense and other custom maps on a table, with friends. **Map sharing:** when the host plays a fan map a friend does not have, only that map file is passed on through the relay and saved in the friend's `Warcraft III\Maps\Download\GSVR` (it also works in the real game). Switch it off in the settings ("Load maps from host automatically"). Texts and dialogs come from your own install.

**ROSE Online (alpha).** The install is found automatically (registry, Steam, usual folders); otherwise set the "ROSE Online folder" in the Games tab or start with `--rose-install <folder>`. On first join the zone, avatar and lobby packs are built from your own install, about 1 minute per zone, in the background (progress is shown in the world's load room); they are cached. Zone loads in the world can still take 1.5 to 2.5 minutes, neighbour zones are preloaded.
Zones (0.9.49): Birth Island, Valley of Luxem Tower, Adventurer's Plain, Breezy Hills, Canyon City of Zant, El Verloon Desert, Anima Lake, Forest of Wisdom, Kenji Beach, Gorge of Silence, Goblin Cave (B1-B3), Junon Polis, Dolphin Island, Junon Clan Field, clan hideout; Luna: Magic City of Eucar, Mana Snowfields, Arumic Valley, Crystal Snowfields, Freezing Plateau, Luna Clan Field; Eldeon: Refuge Xita, Shady Jungle.
Co-op up to 4, parties (with party voice), clans and a global chat. Original skill effects, skill tooltips, 3 x 30 bag, second job change, level cap 250, bows/guns use ammo, all NPCs and most quests run.

**Ragnarok Online (alpha, since 0.9.62).** Bring your own game: you need your own Ragnarok Online client on this PC (start it once so it finishes updating). Short how-to:
1. Install and start the companion. It looks for Ragnarok Online by itself (registry, Steam, usual folders); if it is not found, choose the game folder (the one with `data.grf`) in the Ragnarok Online row.
2. In VRChat: **Settings -> Comfort & Safety -> Allow Untrusted URLs: on**.
3. Open the world [GSVR - RO](https://vrchat.com/home/world/wrld_9b3f4e50-27c3-4d0e-b6d6-394957a6748c) (or press "Open world" in the companion).
On the first visit the maps, characters and monsters are built from your own install on your PC (a few minutes, cached; outdated packs rebuild by themselves, or press **Rebuild** in the world row). First alpha: walk Prontera, its houses and fields with your character, fight monsters and pick up items; the original interface and more come in later versions. Help on [Discord](https://discord.gg/nc2rw6Snx).

## 日本語クイックスタート

自分のPCにインストールした Half-Life、Counter-Strike 1.6、Sven Co-op、Warcraft III、ROSE Online、Ragnarok Online(ラグナロクオンライン)を VRChat で遊べるツールです(ゲームは各自でご用意ください)。
1. [ダウンロードページ](https://devilblader87.github.io/gsvr-companion/)から `GSVR-Setup.exe` をダウンロードしてインストールします。
2. コンパニオンを起動すると、ゲームが自動で検出されます。「スタート」を押すと準備が始まります。
3. VRChat の設定 → Comfort & Safety → 「Allow Untrusted URLs(信頼されていないURL)」をオンにします。
4. 「VRChatでワールドを開く」を押して参加します。
Ragnarok Online はご自身のクライアントのインストールが必要です(見つからない場合は、コンパニオンの Ragnarok Online の行で `data.grf` があるフォルダを選びます)。ワールド: [GSVR - RO](https://vrchat.com/home/world/wrld_9b3f4e50-27c3-4d0e-b6d6-394957a6748c)
Windows PC の VRChat のみ対応です。質問・不具合報告は [Discord](https://discord.gg/nc2rw6Snx) へ。

## Linux / Steam Deck (Proton)

The Windows build also runs under Wine/Proton. Since 0.9.42.1 it looks for VRChat's log in VRChat's own Proton prefix (`steamapps/compatdata/438100/pfx/.../LocalLow/VRChat/VRChat`) in your Steam libraries, so it sees which world you joined. If the status still says "not in a world yet" while you are in a GSVR world, start `gsvr-cli.exe --companion` with `--vrchat-log-dir <the folder that holds output_log_*.txt>`.

## Troubleshooting

- **The world says the companion is missing:** start the companion and make sure *Allow Untrusted URLs* is on in VRChat.
- **A game is not found:** set its folder in the **Games** tab (Advanced view); it shows where each game was found (Steam, registry, folder scan or by hand).
- **The first load is slow:** the first build converts your game data and is cached; later loads are much faster. ROSE builds in the background, about 1 minute per zone.
- **The menu is hard to click in VR:** set VRChat's *Forced Camera Near Distance* to *None*.
- **A Warcraft III map behaves like before an update:** update the companion; a new version rebuilds the maps by itself the next time they are played.
- **No update banner on 0.9.49 / 0.9.49.1:** switch to the **Advanced view** once (button at the top); the yellow "Update available" bar appears there. Later versions show updates in both views and update automatically.

## Releases & changelog

New versions are published on the [Releases](https://github.com/Devilblader87/gsvr-companion/releases) page; the companion updates itself from there. The full history is in [CHANGELOG.md](CHANGELOG.md).

## License, code signing and privacy

GSVR Companion is free software under the [GNU GPL v3](LICENSE). The Windows binaries are built by GitHub Actions from this source and are **not code-signed**, so Windows SmartScreen may warn on first start (More info -> Run anyway). Verify a download with the SHA-256 file attached to each release or build it yourself; see [code signing and download verification, and the privacy statement](CODE_SIGNING_POLICY.md). The source is in `companion/` and `tools/`; build instructions: `companion/packaging/` (`build_windows.bat`, `.github/workflows/build.yml`).

    GSVR Companion - copyright (C) 2026 the GSVR Companion contributors.
    This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public
    License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later
    version. It is distributed WITHOUT ANY WARRANTY; see the license for details.

## Notice

Fan project, not affiliated with Valve, Blizzard Entertainment, VRChat, the owners of ROSE Online or Gravity. Counter-Strike and Half-Life are trademarks of Valve Corporation; Warcraft is a trademark of Blizzard Entertainment; ROSE Online is a trademark of its owners; Ragnarok Online is a trademark of Gravity Co., Ltd.
