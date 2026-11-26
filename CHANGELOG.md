# Changelog

All versions of the GSVR Companion, newest first. Downloads: [Releases](https://github.com/Devilblader87/gsvr-companion/releases). Back to the [README](README.md).

## 0.9.63

**Ragnarok Online (for the updated world GSVR - RO)**
- The original Ragnarok windows: status, inventory, skills, chat and minimap pictures, built from your own install.
- ROSE-style controls: point with the laser, click to walk, attack and talk.
- Monsters on all maps, including the culvert, dungeons and MVPs, with the right look per map.
- The first jobs, and skills that work on monsters.
- Sound and music from your own install, including town and field music and ambient sound. Changed music or decoder settings rebuild the packs by themselves.

**Companion**
- The Windows installer carries the mp3 decoder used for the music.

Works with all current GSVR worlds (they need companion 0.9.57 or newer). Help on Discord: https://discord.gg/nc2rw6Snx

## 0.9.62

**Ragnarok Online (new, bring your own game)**
- New world [GSVR - RO](https://vrchat.com/home/world/wrld_9b3f4e50-27c3-4d0e-b6d6-394957a6748c). You need your own Ragnarok Online client on this PC; the companion builds the maps, characters and monsters from your own install, on your PC (nothing from the game is in the world).
- How to play: start the companion (choose the folder with `data.grf` in the Ragnarok Online row if it is not found), turn on *Allow Untrusted URLs* in VRChat, open the world. The first visit builds the packs (a few minutes, cached).
- First alpha: walk Prontera, its houses and fields with your character, fight monsters and pick up items. The original interface and more come later.

**Companion**
- New look: one clear checklist screen (your games, VRChat, Allow Untrusted URLs, world connected, app version) with one big "Open world" button.
- Ragnarok Online row in the world list, with its own folder choice.
- Outdated ROSE and Ragnarok Online packs rebuild by themselves; a "Rebuild" button per world builds them again by hand.
- Closing the window keeps the companion running in the system tray.

Works with all current GSVR worlds (they need companion 0.9.57 or newer).

## 0.9.57

**ROSE Online**
- Carts like the original: your cart is now an animated vehicle with its driver and passenger poses. It drives and attacks with its own motions, and a party member can ride along on it.
- Character select: the waiting room no longer lifts you up while the scene loads, also after "Change character".
- Smoother play: monsters that sleep or stand far away cost less, and the HUD only redraws when something changes (about a quarter less frame time in towns and fields than in 0.9.55).
- Debuffs from monsters now also hit players, like in the original (since 0.9.56).
- The text size slider now also works in the bag, skill and quest windows, and item tooltips fit their text.
- Fixed: travelling to Canyon City of Zant or Eldeon could send you back to the previous zone.

**Half-Life family**
- Kill feed like the original: newest line at the bottom, a suicide shows only the victim.
- Chase camera after death like the original.
- More client code ported from the original.

**Worlds**
- START HERE boards show one clear next step for every status, and a reminder after 20 seconds when the companion is not found yet.

Needs companion 0.9.57: the ROSE packs rebuild once in the background; you can keep playing meanwhile.

## 0.9.55

**ROSE Online**
- Skills look like the original: every skill plays its own original animation, and arrows, bullets and spells fly visibly to their target; the damage number appears on impact.
- Loot like the original: monsters drop from their own list or the zone's list, and equipment drops get random values (sockets, grade, durability, unappraised gems).
- The Forgotten Temple quest chain can be played through, and the group quests are offered by their NPC (learn the Luna language first, like in the original).
- Desktop: the menus now have the right size for your screen and field of view (100 % = like the original at 1024x768). New "Interface" window (MENU > Options): size 75-200 %, text size, reset. Shortcuts Ctrl + Plus / Ctrl + Minus / Ctrl + 0.
- Desktop: drag windows by their title bar (bag, chat, minimap, skills, quests, party ...); your layout is saved for your next visit.
- Fixed: picked-up drops keep their rolled values.

**Companion**
- The app now checks for updates every 6 hours while it runs, and the simple view shows its version with a "Check for updates" link.
- The START HERE board in the worlds tells you when your companion is outdated.

**Half-Life family**
- More client code ported from the original.

**Worlds**
- START HERE boards: all lines fit on the board in every language.

Needs companion 0.9.55: all ROSE packs rebuild once in the background; you can keep playing meanwhile.

## 0.9.54

**ROSE Online**
- Shops look like the original now: an 8×6 icon grid per tab, every item of a tab visible at once (before: 3 rows per page).
- Item tooltips are readable: dark box, larger text, price line, placed beside the item instead of over it. In VR the laser shows the tooltip beside the shop window.
- Compare stats: shop items show the difference to the piece you wear in that slot (green better, red worse), with the worn piece next to it.
- Recovery like the original: HP/MP regenerate every 4 s by your stats, sitting gives the original sit bonus; standing out of combat adds a small HP top-up.
- Monsters follow day and night like the original, and sleep/stun really stops them.
- Night music in the outdoor zones that have it in the original (dungeons keep their day track).
- Gem appraisal at the NPC (free for now).
- VR window buttons are translated (German, Japanese) and fit their text.

**Half-Life family**
- Opposing Force: all 100 effect sprites of the original are in the model pack now (smaller bone data keeps it within the size limit).
- More client code ported from the original.

Needs companion 0.9.54: the Opposing Force model pack and some ROSE zones rebuild once in the background; you can keep playing meanwhile.

## 0.9.53

**ROSE Online**
- Zone music now plays the whole original track and loops at its natural end (before: only the first minute with a cross-fade).
- Change character: you are back in the zone in under a second, and the select room with podiums looks like the first join.
- Japanese: our menus, HUD, windows and boards now show Japanese text correctly (font: Noto Sans JP, SIL Open Font License 1.1). Item, NPC and quest names still come from your own client.
- VR: hold a window and push the right stick up / down to move it farther / closer (like overlay apps).
- VR: the laser now also works in the character select lobby.
- Lobby boards stay readable from every angle.
- Co-op: debuffs and poison from party members now land on monsters correctly.
- Towns and fields run smoother.

**Half-Life family**
- Opposing Force: the Gargantua is back (model pack rebuilt within the size limit) and stomps like the original.
- More client code ported from the original.

**Worlds**
- New "START HERE" board at the spawn of ROSE, Half-Life and Counter-Strike: 4 steps, QR code and a live status (red / amber / green) that tells you what is still missing.
- New VRChat group board: join the group and see the next group event in your local time.
- Weekly group events: ROSE Saturday 21:00 JST, Half-Life co-op night Friday 20:00 CET.

Needs companion 0.9.53: all ROSE packs and the Opposing Force model pack rebuild once in the background; you can keep playing meanwhile.

## 0.9.51

**ROSE Online**
- Storage: talk to "[Ferrell Guild Staff] Crow" in Zant. Your storage is shared by all your characters.
- Player trade: trade with another player in the same instance. Trades are safe, and items cannot be duplicated.
- Monsters now use buffs and debuffs like the original, and you can see their cast bars in dungeons.
- Party money is split between the members.
- Sit / stand: press X on desktop, or use the VR wheel. HP and MP recover faster while you sit. Emotes are now available too.
- Wheel changes: Char has Sit/Stand; Party/Clan has Trade.

**Half-Life family**
- Allies that drop in on ropes now call their own squad for help, like in the original.
- Blue Shift: snarks are thrown from the original distance and height (also while ducked).
- Deathmatch Classic: shotgun punch adjusted.
- Opposing Force: snow impacts are slightly quieter.
- Spectator: with no target, the HL camera now goes to the map start instead of staying put.

**Worlds**
- New ROSE poster and portal in the Half-Life hub and in the Counter-Strike world.
- The ROSE help board now shows the first steps as pictures.

Needs companion 0.9.51: all ROSE packs rebuild once in the background; you can keep playing meanwhile.

## 0.9.50

**New**
- The VRChat menu is usable in every zone, also with a group: the zone with the most players sits at the centre of the world, and the load world is now above the game. (Hotfixes 0.9.49m / 0.9.49m2 are included: the menu no longer fights the ROSE laser and chat field, no forced player moves while the VRChat menu is open, the big VRChat menu is usable again while you are alone in a zone.)
- Tip on the help board: if the VRChat menu is hard to click, set the VRChat option "Forced Camera Near Distance" to None.
- Smoother frame rate in VR while the next zone is being prepared.
- No surprise teleports: you are only moved when you choose to or when you enter a new area, and arriving is smoother. Fights and click-to-move are no longer interrupted by position corrections.
- Saving is more robust after a freeze or a short disconnect.

**ROSE Online**
- Monsters use their skills and spells in every zone like the original, including ranged and area spells.
- Drops belong to the killer and the party for 60 seconds.
- Quests: more condition types are understood, ready for future zones.
- VR wheel: new Drive button for the PAT cart.
- Sea waves, shore foam, torches, street lights and fires at night.
- Dropped items are now fully textured and lit.
- "Change character" from inside the game (menu or wheel, Yes/No, your progress is saved first).
- Master tools are easier to find: Master sector in the wheel, "Go to player" (click) on the join message, "Return to my character".

**Half-Life family**
- Opposing Force: rope climbing; allies drop from ropes.
- Alien slaves call for help, gargantua stomp timing fixed, scientists and Barneys have their scared lines.

Needs companion 0.9.50: all ROSE packs rebuild once in the background; you can keep playing meanwhile.

## 0.9.49.2 (hotfix)
- Updates now show in the Simple view. In 0.9.49 / 0.9.49.1 the "Update available" bar could only appear in the Advanced view; in the Simple view the update was found but never shown (and the window stopped refreshing).
- If you are on 0.9.49 or 0.9.49.1: switch to the Advanced view once (button at the top), where the yellow "Update available -> Install" bar appears, or download this release directly.
- Includes 0.9.49.1.

## 0.9.49.1 (hotfix)
- Simple view: no more CPU load and laggy window. It searched all drives for games every 4 seconds; it now searches when it opens, after you change a game folder, and at most once a minute. World status still updates every few seconds.
- The tabs of the Advanced view are no longer redrawn in the background while the Simple view is open.

## 0.9.49 (alpha)
- 1-click setup: games are found automatically, one Start button builds what is needed, then "Open world in VRChat". You can already play while ROSE is still preparing in the background.
- Simple view with all worlds (ROSE, Half-Life family, Counter-Strike, Warcraft 3) and a traffic-light status; the Advanced view keeps all previous tabs.
- Companion in Japanese, English and German, with a Help button on every problem and a link to the Discord.
- ROSE Online: new zones Shady Jungle (via Eldeon, monsters up to level 160) and Luna Clan Field (clan teleport from Crystal Snowfields).
- ROSE Online: less CPU load (quest marks over NPCs and resting characters).
- Needs the ROSE world update of 2026-11-18.

## 0.9.32
- Fixes new maps failing to convert with ModuleNotFoundError 'nodegraph' (the Windows build of 0.9.31 and earlier lacked the node graph module; update to this version).

## 0.9.31
- Opposing Force capture-the-flag end-of-round scoreboard.
- Stand-in models from your own install for the Opposing Force models Steam does not ship (pit worm, baby voltigore, spore ammo, voltigore gibs); rebuilds the Opposing Force model pack.

## 0.9.30
- Monsters that survive a level change keep their place and health when you come back.
- More exact scripted scenes.
- Opposing Force ally / medic / engineer and sniper behaviour with their sounds.
- Big Mama sounds; grunts sweep / dance / rappel.
- Running conveyor textures; pushable crates float; real corpse gibs.
- DMC grenade blast radius and telefrag.
- The Opposing Force campaign no longer stalls after of3a1.
- Rebuilds model, map, sound and sentence packs.

## 0.9.28
- The load cabin is the original Black Mesa intro tram from your own Half-Life install (next stop and loading bar on its front wall, black space with silver streaks outside).
- The Black Mesa Inbound ride no longer stalls in c0a0d.
- Co-op guests change level at once and arrive on the carried tram.
- Tram seats follow braking trams on time.

## 0.9.27
- Co-op parity: everybody starts, loads, changes level, respawns and late-joins at exactly the host's spot with the same gear and world state.
- TELEPORT TO HOST in the player menu.
- Arriving on the carried tram after a level change; a transit cabin while the next map loads.
- A global voice switch.
- The world state is kept in every save slot.
- The Opposing Force intro plays to the end.
- A choosable cache folder.
- Desktop: E = use.

## 0.9.26
- New VR HUD: ammo on the gun, health and armour on the wrist, damage and geiger indicators with the original sprites.
- Bots in the Half-Life deathmatch rooms.
- All Blue Shift weapons visible; the real Ricochet disc in your hand.
- Opposing Force boot camp exit door and drill sergeant voice.
- Smoother crouching in vents.
- Bug report board with QR code.
- More effects, duck fixes.

## 0.9.24
- Crouch through windows and vents with auto step-up and crouch toggle.
- Original jump height.
- Save slots per mod room.
- Half-Life node-graph AI (cover, flanking, doors).
- Loading bar; per-mod model cache.

## 0.9.23
- Opposing Force room in the HL mod hub (14 chapters from your own OF install).
- Laser menus in all hubs; start console buttons fixed.
- Blue Shift / Opposing Force / Ricochet / DMC rules extended.

## 0.9.22
- Real gibs and debris; monster voices.
- HL rules pass 2; HL VR weapon grips.
- Changelog boards.
- Warcraft III tavern hub with holodeck reveal and holo table.
- Packs gsm1-2.7 / w3p1-85.

## 0.9.21
- Real sprite effects: explosions, muzzle flashes for everyone, blood with a settings switch, textured beams.
- Sven Co-op walk cycles.
- Model packs gsm1-2.5.

## 0.9.20
- HL monsters visible again.
- CS silencer animations and walkable de_aztec bridge.
- Model packs gsm1-2.4.

## 0.9.19
- Warcraft III packs w3p1-84 for the new W3VR world.

## 0.9.18
- Weapon packs gsm1-2.2: VR grips for Half-Life / Sven weapons, Counter-Strike knife + grenade hand, silencer at the muzzle.

## 0.9.17
- Pack format w3p1-83: Warcraft III PORT13 rule wave (items, harvesting, missiles, movement).

## 0.9.16
- Pack format w3p1-82: Warcraft III armor-type upgrades and the PORT12 rule wave.
- Hub model packs load right after a direct Sven start.

## 0.9.15
- Map hologram previews for the GoldSrc mod hub, built from your own install.
- Warcraft III items, upgrades, missiles and movement closer to the original.

## 0.9.14
- Pack format w3p1-81: Warcraft III save system through the original game menu (map save codes and game caches kept in your VRChat data).
- Rules measured in the real 1.26a; undead AI expansion.
- GPU flow fields on more TD maps.

## 0.9.13
- Pack format w3p1-80: melee maps on the two-level pathfinder with one gold miner at a time.
- Computer opponents attack the nearest enemy base.
- Item abilities and figurines.
- More JASS natives (UnitDamagePoint, GetPlayerScore, buffs, shared vision, game cache).
- GPU flow fields on Line Tower Wars and Wintermaul.

## 0.9.12
- Pack format w3p1-75: lobby computer opponents (easy / normal / insane) on the real melee AI scripts of your install.
- Night elf / undead build rules; Ancients root and uproot.
- Town halls keep 750 from gold mines.
- TD waves spawned at a region edge walk again.
- Counter-Strike custom maps are shared through the relay.

## 0.9.11
- Pack format w3p1-74: attack, spell, chop, build and death animations with sounds.
- Flyers fly and ships float.
- Workers drop off only at real drop-off buildings.
- Hero item / inventory controls and hero bar.

## 0.9.10
- Pack format w3p1-72 and many game rules from the 1.26a reference.
- Stunned units keep their order; Footman Frenzy rally points.
- Map-script `and` short-circuits.
- Computer AI natives.

## 0.9.9
- Classic melee maps: workers visible, the build menu shows all buildings, gold / lumber harvest works, fog of war is on.
- A simple computer opponent: in the lobby click an "Open" row, then "Computer (easy)".
