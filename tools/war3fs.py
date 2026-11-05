"""One file view over any classic Warcraft III install: 1.2x MPQ archives or 1.30/1.31 CASC (docs/versions.md).

  fs = open_install(DIR)          # detects the storage
  fs.read('Units\\Human\\Footman\\Footman.mdx', missing_ok=True)
  fs.version  ('121', '131' …),  fs.kind ('mpq' | 'casc'),  fs.maps() → {key: (display name, path or casc path)}
  fs.read_map(path) → map file bytes;  fs.set_tileset('A') (1.31 tileset overrides for terrain art)

1.31 lookup order (CASC mods) [ext + observed listing]: locale (enus) → the map's balance set (below) → tileset
override (_tilesets\\<t>.w3mod) → base war3.w3mod → _deprecated.w3mod. MPQ: War3Patch > War3xlocal > War3x > war3
(tools/mpq.py war3_chain), the map's balance set first.

Balance data set (BUGS DS-1, real 1.21 game, docs/oracle.md): a map without the w3i melee flag (0x0004) plays with
the custom set — `Custom_V1\\Units\\…` (MPQ) / `_balance\\custom_v1.w3mod:` (CASC) replaces the file of the same name
(UnitBalance, UnitWeapons, UnitAbilities, UpgradeData, ItemData, MiscGame, *Func / *Strings …) [obs: Blizzard TD
knight 800 HP = Custom_V1, Echo Isles 835 = base; Echo Isles without the melee flag paid the Custom_V1 upgrade price;
the RoC map Line Tower Wars (.w3m, w3i 18) also gets the Custom_V1 HP in the 1.21 Frozen Throne game]. A melee map uses
the base files [obs]; Custom_V0 / Melee_V0 are not used by the TFT game for any map seen [inf].
  fs.set_data_set(data_set(w3i_bytes))   before reading base data for a map
"""
import os, re

import w3mpq as mpq

MAP_RE = re.compile(r'\(\d+\)[^:\\/]*?\.w3[xm]$', re.I)


def data_set(w3i):
    """war3map.w3i bytes -> the balance set folder ('Custom_V1') or None (the base files: melee maps)."""
    if not w3i or len(w3i) < 12:
        return None
    import w3map
    try:
        info = w3map.parse_w3i(w3i)
    except Exception:                          # noqa: BLE001
        return None
    if info.get('flags', 0) & 0x0004:
        return None                            # melee: the base files [obs Echo Isles]; a RoC melee map too [inf]
    return 'Custom_V1'                         # every custom map, RoC ones too [obs Line Tower Wars .w3m: V1 HP]


class MpqInstall:
    kind = 'mpq'

    def __init__(self, root):
        self.root = root
        self.chain = mpq.war3_chain(root)
        self.version = _mpq_version(root)
        self.tables = self.chain.tables              # weight table sources for the map archives of this install
        self.ds = None

    def set_data_set(self, ds):
        self.ds = ds

    def read(self, name, missing_ok=False, limit=None):
        if self.ds:
            d = self.chain.read(self.ds + '\\' + name, missing_ok=True, limit=limit)
            if d is not None:
                return d
        return self.chain.read(name, missing_ok=missing_ok, limit=limit)

    def set_tileset(self, t):
        pass

    def maps(self):
        out = {}
        for d in ('Maps', 'maps'):
            md = os.path.join(self.root, d)
            if not os.path.isdir(md):
                continue
            for dp, dns, fns in os.walk(md):
                dns.sort()
                for fn in sorted(fns):
                    if fn.lower().endswith(('.w3x', '.w3m')):
                        key = os.path.splitext(fn)[0].lower()
                        out.setdefault(key, (fn, os.path.join(dp, fn)))
        return out

    def read_map(self, path):
        with open(path, 'rb') as fh:
            return fh.read()


class CascInstall:
    kind = 'casc'

    def __init__(self, root, locale='enus'):
        import casc
        self.root = root
        self.c = casc.CASC(root)
        m = re.match(r'(\d+)\.(\d+)', self.c.build_name)
        self.version = (m.group(1) + m.group(2)) if m else '13x'
        self.locale = locale
        self.tileset = None
        self.ds = None
        # CASC installs have no Storm.dll; the game exes carry the same Huffman weight rows (1.31 map imports may still
        # hold MPQ-compressed wavs)
        self.tables = tuple(p for d in ('x86', 'x86_64') for n in ('Warcraft III.exe', 'World Editor.exe')
                            for p in [os.path.join(root, d, n)] if os.path.isfile(p))

    def set_data_set(self, ds):
        self.ds = ds

    def set_tileset(self, t):
        self.tileset = t.lower() if t else None

    def _order(self):
        o = ['war3.w3mod:_locales\\%s.w3mod:' % self.locale]
        if self.ds:                                # BUGS DS-1: only the map's set (was always custom_v1, custom_v0)
            o.append('war3.w3mod:_balance\\%s.w3mod:' % self.ds.lower())
        if self.tileset:
            o.append('war3.w3mod:_tilesets\\%s.w3mod:' % self.tileset)
        return o + ['war3.w3mod:', 'war3.w3mod:_deprecated.w3mod:']

    def read(self, name, missing_ok=False, limit=None):
        for pre in self._order():
            d = self.c.read(pre + name, missing_ok=True)
            if d is not None:
                return d
        if missing_ok:
            return None
        raise KeyError(name)

    def maps(self):
        out = {}
        for p in self.c.names():
            if not p.startswith('war3.w3mod:') or ':maps' not in p.lower():
                continue
            m = MAP_RE.search(p)
            if m:
                out.setdefault(os.path.splitext(m.group(0))[0].lower(), (m.group(0), p))
        return out

    def read_map(self, path):
        return self.c.read(path)


def _mpq_version(root):
    """'121', '126' … from Patch.txt's first 'Patch 1.xx' line, else '12x'."""
    for fn in ('Patch.txt', 'patch.txt'):
        p = os.path.join(root, fn)
        if os.path.isfile(p):
            m = re.search(r'Patch 1\.(\d+)', open(p, encoding='latin1', errors='replace').read())
            if m:
                return '1' + m.group(1)
    return '12x'


def open_install(root):
    if os.path.isfile(os.path.join(root, '.build.info')) and os.path.isdir(os.path.join(root, 'Data')):
        return CascInstall(root)
    return MpqInstall(root)
