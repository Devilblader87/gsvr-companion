"""ROSE avatar / item pack: every equipment mesh of both genders (cap, body, hands, feet, back, face item, weapon, sub weapon), all faces and hairs, and the ground models of dropped items
-> one RPG1 pack `rose__avatar.rpg1` that the companion serves like a zone pack and the runtime keeps across zone switches (GsvrRpg1Actors.BuildItemsFromBytes).

  python3 -I tools/rose_avatar_pack.py <client dir> -o out/rose__avatar.rpg1

Sections (docs/RPG1_PACK.md section 23): SKEL (MALE, FEMALE, FIELD), CMSH (every mesh once; rigid parts baked on their joint like the zone pack), OTEX / OMAT (DXT, deduplicated by texture file),
AVAT (items only: genders without clips, every (gender, slot, number) of the item tables with a mesh; sorted so the runtime can binary search) and FLDM (field models + the drop animation),
EFCT (rose_effects.py: skill / npc / weapon effects + their particle atlas; one copy here because the runtime keeps this pack across zone switches).
Item number = item table row = ZSC object index (the client's get_model_part_index). Items without a mesh in the client get no AVAT item. The client is only read; nothing of it is committed.
"""
import os, sys, json, argparse, collections, hashlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rosevfs, rosefmt as F, rpg1 as P, rose_chars as RC

FIELD_TABLES = tuple('LIST_' + n for n in ('FACEITEM', 'CAP', 'BODY', 'ARMS', 'FOOT', 'BACK', 'JEWEL', 'WEAPON', 'SUBWPN', 'USEITEM', 'ETCITEM', 'NATURAL', 'QUESTITEM'))
FIELD_COLUMN = 10


def field_indices(vfs, log=print):
    """every field model index named by an item row (cell 10), plus 0 (money)"""
    idx = {0}
    for name in FIELD_TABLES:
        try:
            stb = F.stb(vfs.read('3DDATA/STB/%s.STB' % name))
        except KeyError:
            continue
        for r in range(1, stb['rows']):
            c = stb['cells'][r]
            if len(c) > FIELD_COLUMN and c[FIELD_COLUMN].strip().isdigit():
                idx.add(int(c[FIELD_COLUMN].strip()))
    return idx


def dedup_textures(ob):
    """merge textures with identical bytes (different files, same picture): remaps the OMAT rows, shrinks ob.textures. -> bytes saved"""
    seen = {}; remap = []; keep = []; saved = 0
    for t in ob.textures:
        h = hashlib.sha1(repr((t['fmt'], t['w'], t['h'])).encode() + b''.join(t['levels'])).digest()
        if h in seen:
            remap.append(seen[h]); saved += sum(len(l) for l in t['levels'])
        else:
            seen[h] = len(keep); remap.append(len(keep)); keep.append(t)
    for m in ob.mats:
        if m['tex'] != P.NO_TEX:
            m['tex'] = remap[m['tex']]
    ob.textures = keep
    return saved


def build(vfs, log=print, genders=(0, 1), with_field=True):
    """-> (sections, info). Needs an ObjBuilder for the shared texture / material tables (the zone converter's)."""
    import rose_pack as RP
    ob = RP.ObjBuilder(vfs, None, None)
    cb = RC.CharBuilder(vfs, ob)
    av = cb.add_equipment(genders)
    import rose_carts
    cart_items = rose_carts.add_cart_items(cb, genders)          # vehicle parts (AVAT-format items of slots 10..13 for the optional ACAR section), docs/design/rose_carts.md
    if cart_items:
        log('cart / castle gear part items: %d' % len(cart_items))
    log('avatar items: %d (%s), %d meshes' % (len(av['items']), ', '.join('%s %d' % (P.SLOT_NAMES[s], n) for s, n in sorted(collections.Counter(i['slot'] for i in av['items']).items())), len(cb.meshes)))
    fld = []; anim = []
    if with_field:
        fld = cb.field_models(field_indices(vfs))
        anim = cb.field_animation()
        log('field models: %d, drop animation %d frames' % (len(fld), len(anim)))
    saved = dedup_textures(ob)
    log('textures: %d after merging identical ones (%.2f MB saved)' % (len(ob.textures), saved / 1e6))
    sections = []
    secs = {t: (v, c, d) for t, v, c, d in cb.sections()}
    for tag in (b'SKEL', b'CMSH', b'AVAT'):
        if tag in secs:
            v, c, d = secs[tag]
            sections.append((tag, v, c, d))
    if cart_items:
        sections.append((b'ACAR', 1, len(cart_items), P.enc_avat(av['genders'], cart_items, [])))          # same layout as AVAT, only the vehicle slots; older runtimes ignore the tag
    sections.append((b'OTEX', 1, len(ob.textures), P.enc_otex(ob.textures)))
    sections.append((b'OMAT', 1, len(ob.mats), P.enc_omat(ob.mats)))
    if fld:
        sections.append((b'FLDM', 1, len(fld), P.enc_fldm(fld, anim)))
    # EFCT (rose_effects.py): skill cast / bullet / hit effects, npc hit / die and weapon hit effects for the runtime's pooled effect player. A client without the effect tables
    # yields no section; one bad EFT / PTL / texture is skipped inside the builder; anything else (the module missing in a frozen build, a table that does not parse) FAILS the
    # avatar build like QGLB, so a pack without EFCT is never cached as good.
    import rose_effects
    fs, fx = rose_effects.build_section(vfs, log=log)
    sections += fs
    info = dict(texture_dedup_bytes=saved, items=len(av['items']), meshes=len(cb.meshes), textures=len(ob.textures), materials=len(ob.mats), field_models=len(fld), cart_items=len(cart_items),
                warnings=dict(list(cb.warn.items()) + [('effects: ' + k, v) for k, v in (fx.get('warnings') or {}).items()]),
                effects={k: v for k, v in fx.items() if k != 'warnings'}, section_bytes={t.decode(): len(d) for t, _v, _c, d in sections})
    return sections, info


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('client', help='ROSE client directory (data.idx + *.VFS), read only')
    ap.add_argument('-o', '--out', default='out/rose__avatar.rpg1', help='output pack (the companion serves rose__<name>.rpg1; name "avatar")')
    ap.add_argument('--no-field', action='store_true', help='skip the ground models (FLDM)')
    a = ap.parse_args(argv)
    vfs = rosevfs.Vfs(a.client)
    sections, info = build(vfs, with_field=not a.no_field)
    pack = P.write_pack(sections)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, 'wb') as f:
        f.write(pack)
    with open(os.path.splitext(a.out)[0] + '.manifest.json', 'w') as f:
        json.dump(info, f, indent=1, sort_keys=True)
    print('wrote %s (%.2f MB)' % (a.out, len(pack) / 1e6))
    print('  ' + ', '.join('%s %d KB' % (k, v // 1024) for k, v in sorted(info['section_bytes'].items())))
    for k, v in sorted(info['warnings'].items()):
        print('  warning: %s x%d' % (k, v))


if __name__ == '__main__':
    main()
