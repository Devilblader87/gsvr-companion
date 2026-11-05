#!/usr/bin/env python3
"""Warcraft III frame definition files (UI\\FrameDef\\**\\*.fdf, the player's install) -> a tree of nodes.

Format (from the text): line based. A block opens with `<Kind> ["<type>"] ["<name>"] [INHERITS [WITHCHILDREN]
"<parent>"] {` and closes with `}`; inside are child blocks and properties `<Key> <value>, <value>, ...,` (values:
"quoted strings", numbers, bare words such as TOPLEFT). `//` starts a comment. Top level also has
`IncludeFile "<path>",` and `StringList { "Key" "Value", ... }` (localised strings).

  python3 tools/fdf.py [install]     -> dump of the in-game frame files to work/ui/fdf_ingame.json + a key report

Nothing of the parsed content goes into the repo (work/ is git-ignored).
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..'))
TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([^\s,"{}]+)|(\{)|(\})|(,)')


def _strip_comment(line):
    out, q = [], False
    i = 0
    while i < len(line):
        c = line[i]
        if c == '"':
            q = not q
        elif not q and line.startswith('//', i):
            break
        out.append(c)
        i += 1
    return ''.join(out)


def _value(tok):
    s, w = tok
    if s is not None:
        return s
    try:
        return float(w) if re.match(r'^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$', w) else w
    except ValueError:
        return w


def tokens(text):
    for line in text.split('\n'):
        line = _strip_comment(line)
        for m in TOKEN.finditer(line):
            if m.group(1) is not None:
                yield ('s', m.group(1))
            elif m.group(2) is not None:
                yield ('w', m.group(2))
            elif m.group(3):
                yield ('{', '{')
            elif m.group(4):
                yield ('}', '}')
            else:
                yield (',', ',')
        yield ('nl', '')


def parse(text):
    """-> {'includes': [...], 'strings': {key: value}, 'nodes': [node]}; node = {'kind', 'type', 'name',
    'inherits', 'withchildren', 'props': [(key, [values])], 'children': [node]}"""
    toks = list(tokens(text))
    pos = 0
    root = {'includes': [], 'strings': {}, 'nodes': []}

    def block(head):
        nonlocal pos
        node = {'kind': head[0], 'type': None, 'name': None, 'inherits': None, 'withchildren': False,
                'props': [], 'children': []}
        args = [h for h in head[1:]]
        if 'INHERITS' in args:
            i = args.index('INHERITS')
            rest = args[i + 1:]
            if rest and rest[0] == 'WITHCHILDREN':
                node['withchildren'] = True
                rest = rest[1:]
            node['inherits'] = rest[0] if rest else None
            args = args[:i]
        if len(args) == 2:
            node['type'], node['name'] = args
        elif len(args) == 1:
            node['name'] = args[0]
        while pos < len(toks):
            t, v = toks[pos]
            if t == '}':
                pos += 1
                return node
            if t in ('nl', ','):
                pos += 1
                continue
            # a statement on this line: words / strings up to '{' (child block) or the line end (property)
            line = []
            while pos < len(toks) and toks[pos][0] not in ('nl', '{', '}'):
                line.append(toks[pos])
                pos += 1
            if pos < len(toks) and toks[pos][0] == '{':
                pos += 1
                hd = [x[1] for x in line if x[0] != ',']
                if node['kind'] == 'StringList' or hd == []:
                    pass
                node['children'].append(block(hd))
                continue
            vals = [x for x in line if x[0] != ',']
            if not vals:
                continue
            key = vals[0][1]
            node['props'].append((key, [_value((x[1] if x[0] == 's' else None, x[1] if x[0] == 'w' else None))
                                        for x in vals[1:]]))
        return node

    while pos < len(toks):
        t, v = toks[pos]
        if t in ('nl', ','):
            pos += 1
            continue
        line = []
        while pos < len(toks) and toks[pos][0] not in ('nl', '{'):
            line.append(toks[pos])
            pos += 1
        if pos < len(toks) and toks[pos][0] == '{':
            pos += 1
            hd = [x[1] for x in line if x[0] != ',']
            if hd and hd[0] == 'StringList':
                n = block(hd)
                for k, vals in n['props']:
                    if vals:
                        root['strings'][k] = vals[0]
            elif hd:
                root['nodes'].append(block(hd))
            continue
        vals = [x for x in line if x[0] != ',']
        if vals and vals[0][1] == 'IncludeFile' and len(vals) > 1:
            root['includes'].append(vals[1][1])
    return root


def file_keys(node, out):
    """texture keys (File / BackdropBackground …) used by a node tree"""
    for k, vals in node['props']:
        if k in ('File', 'BackdropBackground', 'BackdropEdgeFile', 'BackdropCornerFile', 'ControlBackdrop',
                 'Font') and vals and isinstance(vals[0], str):
            out.add((k, vals[0]))
    for c in node['children']:
        file_keys(c, out)
    return out


def skins(install):
    """UI\\war3skins.txt: [section] key=value; -> {section: {key: value}}"""
    raw = install.read('UI\\war3skins.txt', missing_ok=True)
    out, cur = {}, None
    if not raw:
        return out
    for line in raw.decode('latin-1').splitlines():
        line = line.split('//')[0].strip()
        if line.startswith('[') and line.endswith(']'):
            cur = out.setdefault(line[1:-1], {})
        elif '=' in line and cur is not None:
            k, v = line.split('=', 1)
            cur[k.strip()] = v.strip()
    return out


def main():
    sys.path.insert(0, HERE)
    import war3fs
    inst = war3fs.open_install(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, 'input', 'war3'))
    names = set()
    for m in inst.chain.archives:
        for n, _i, _info in m.listing():
            if n:
                names.add(n)
    files = sorted(n for n in names if n.lower().startswith('ui\\framedef\\ui\\') and n.lower().endswith('.fdf'))
    dump, keys = {}, set()
    for n in files:
        tree = parse(inst.read(n).decode('latin-1'))
        dump[n] = tree
        for node in tree['nodes']:
            file_keys(node, keys)
    sk = skins(inst)
    out = os.path.join(ROOT, 'work', 'ui')
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, 'fdf_ingame.json'), 'w') as fh:
        json.dump(dump, fh, indent=1)
    print('%d in-game fdf files, %d frames at top level, skin sections: %s' % (
        len(files), sum(len(t['nodes']) for t in dump.values()), ', '.join(sorted(sk))[:300]))
    unres = []
    for k, v in sorted(keys):
        if k == 'Font':
            continue
        hit = [s for s, kv in sk.items() if v in kv]
        if not hit and not v.lower().endswith(('.blp', '.tga')):
            unres.append(v)
    print('texture keys %d, not in war3skins.txt (and not a file path): %s' % (len(keys), unres[:30]))


if __name__ == '__main__':
    main()
