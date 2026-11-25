"""CLI: python3 -m ro_conv build --grf <dir> --map prontera --out ro__prontera.pack (map + player sprites in one RPG1 pack; docs/design/ro_pack.md).
The companion serves <ro dir>/ro__<map>.pack as GET /pack/ro.pack (companion/ro.py) and builds it itself through ro_conv.api.build."""
import argparse
import json
import os
import sys
import time

from . import mappack as B
from . import api
from .grf import Vfs, GrfError


def _start(s):
    if not s:
        return None
    x, y = s.split(',')
    return int(x), int(y)


def main(argv=None):
    ap = argparse.ArgumentParser(prog='ro_conv', description='Ragnarok Online map -> RPG1 pack (reads your own client, read only)')
    sub = ap.add_subparsers(dest='cmd')
    b = sub.add_parser('build', help='convert one map')
    b.add_argument('--grf', required=True, help='RO client folder (with data.grf / rdata.grf / data\\) or one .grf file')
    b.add_argument('--map', required=True, help='map name, e.g. prontera')
    b.add_argument('--out', required=True, help='output pack path (the companion serves <ro dir>/ro__<map>.pack)')
    b.add_argument('--no-sprites', action='store_true', help='map only, no RSAT / RSAN player sprite set (maps other than ro_conv.api.SPRITE_PACK never carry it)')
    b.add_argument('--cell', type=float, default=1.0, help='metres per GAT cell (default 1.0; one GND tile = 2 cells)')
    b.add_argument('--block', type=int, default=0, help='GND tiles per terrain block (default: 32, grown until the scene pool fits)')
    b.add_argument('--max-chunks', type=int, default=700, help='object chunk budget (scene pool 800)')
    b.add_argument('--model-z', choices=('auto', 'a', 'b'), default='auto', help='RSW model z: a = as stored, b = mirrored, auto = best ground fit')
    b.add_argument('--start', default='', help='start GAT cell x,y (default: the map\'s api.PACKS start, else the walkable cell nearest the map centre)')
    b.add_argument('--zone-id', type=int, default=B.ZONE_ID)
    b.add_argument('--tex-cap', type=int, default=256, help='largest object texture side (power of two)')
    b.add_argument('--no-models', action='store_true', help='ground + grid only (fast)')
    b.add_argument('--manifest', action='store_true', help='also write <out>.json (stats, warnings)')
    l = sub.add_parser('list', help='list the maps (rsw files) in the client')
    l.add_argument('--grf', required=True)
    l.add_argument('--filter', default='')
    a = ap.parse_args(argv)
    if a.cmd not in ('build', 'list'):
        ap.print_help(); return 2
    try:
        vfs = Vfs(a.grf)
    except GrfError as e:
        print('error: %s' % e, file=sys.stderr); return 1
    for e in vfs.errors:
        print('warn: %s' % e, file=sys.stderr)
    if a.cmd == 'list':
        names = sorted(n[5:-4] for n in vfs.names() if n.startswith('data\\') and n.endswith('.rsw') and '\\' not in n[5:])
        for n in names:
            if a.filter in n:
                print(n)
        return 0
    t0 = time.time()
    sw = []
    extra = []          # like api.build: the player sprites in the start map's pack, the monsters + drop items in the fight field's (w34)
    if not a.no_sprites and a.map == api.SPRITE_PACK:
        extra = api.sprite_sections(vfs, warn=sw)
    elif not a.no_sprites and a.map in api.MOB_SPRITES:
        extra = api.mob_sprite_sections(vfs, a.map, warn=sw)
    for w_ in sw:
        print('warn: %s' % w_, file=sys.stderr)
    if extra:
        rs = api.sprite.dec_rsan(extra[1][3])
        print('sprites: %d bytes (RSAT + RSAN), %d sprites, %d frames, ref height %.0f px' % (sum(len(x[3]) for x in extra), len(rs['sprites']),
                                                                                         len(rs['frames']), rs['ref_height_px']))
    try:
        pack, man = B.build(vfs, a.map, cell_m=a.cell, tiles_per_block=a.block or None, max_chunks=a.max_chunks, model_z=a.model_z,
                            start=_start(a.start) or api.PACKS.get(a.map, {}).get('start'), zone_id=a.zone_id, tex_cap=a.tex_cap, models=not a.no_models, extra_sections=extra)
    except (B.BuildError, GrfError, ValueError) as e:
        print('error: %s' % e, file=sys.stderr); return 1
    tmp = a.out + '.part'
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(tmp, 'wb') as f:
        f.write(pack)
    os.replace(tmp, a.out)
    if a.manifest:
        with open(a.out + '.json', 'w') as f:
            json.dump(man, f, indent=1, sort_keys=True)
    print('wrote %s (%d bytes, %.1f MiB) in %.1f s' % (a.out, len(pack), len(pack) / 1048576.0, time.time() - t0))
    return 0


if __name__ == '__main__':
    sys.exit(main())
