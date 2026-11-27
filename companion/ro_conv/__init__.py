"""Ragnarok Online -> RPG1 converter (BYOG): reads the player's own RO client data (data.grf / rdata.grf / loose data\\ folder),
writes RPG1 packs the GSVR world reads. Nothing from the game is in this repo or in the world.

  python3 -m ro_conv build --grf <client dir or .grf> --map prontera --out ro__prontera.pack      (run from companion/)
  python3 -m ro_conv list  --grf <client dir> [--filter prt]                                     (maps in the client)

Modules: grf (GRF 0x200 + VFS), rofmt (GAT / GND / RSW / RSM v1 parsers), img (BMP / TGA), build (map pack writer),
sprite (SPR / ACT -> RSAT / RSAN sprite sections), audio + strfx (sound / BGM packs, STR skill effects: docs/design/ro_sound.md), api (companion build-manager contract, re-exported here).
Format + coordinate decisions: docs/design/ro_pack.md, docs/design/ro_sprites.md.
"""
from .api import VERSION, PACKS, AUDIO_PACKS, AUDIO_ORDER, UI_PACKS, NPC_PACKS, PACK_REV, MAP_ORDER, PREBUILD_ORDER, NO_PREBUILD, SPRITE_PACK, warm_after, check_install, install_files, install_extra, pack_version, build, sprite_sections, Cancelled, BuildError  # noqa: E402,F401
