"""SYLK (.slk) table reader for the WC3 data tables (TerrainArt\\Terrain.slk, Units\\UnitData.slk ...).

Only the records the game files use: ID, B (bounds), C (cell: X/Y/K), F (format, ignored), E (end). A C record
without Y keeps the current row; without X keeps the current column. Values: "text" -> str, numbers -> int/float.
parse(text) -> (header list, rows as dicts keyed by the header of row 1).
"""


def _value(s):
    if s.startswith('"'):
        return s[1:-1] if s.endswith('"') else s[1:]
    try:
        return int(s)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return s


def parse(text):
    cells = {}
    x = y = 0
    for line in text.splitlines():
        if not line.startswith(('C;', 'F;')):
            continue
        parts = line.split(';')
        val = None
        for p in parts[1:]:
            if not p:
                continue
            if p[0] == 'X':
                x = int(p[1:])
            elif p[0] == 'Y':
                y = int(p[1:])
            elif p[0] == 'K' and parts[0] == 'C':
                val = _value(p[1:])
        if parts[0] == 'C' and val is not None:
            cells[(y, x)] = val
    if not cells:
        return [], []
    max_x = max(k[1] for k in cells)
    max_y = max(k[0] for k in cells)
    header = [cells.get((1, c)) for c in range(1, max_x + 1)]
    rows = []
    for r in range(2, max_y + 1):
        row = {}
        for c in range(1, max_x + 1):
            h = header[c - 1]
            if h is not None and (r, c) in cells:
                row[h] = cells[(r, c)]
        if row:
            rows.append(row)
    return header, rows


def table(text, key):
    """rows keyed by column `key` (first occurrence wins)."""
    _, rows = parse(text)
    out = {}
    for row in rows:
        k = row.get(key)
        if k is not None and k not in out:
            out[k] = row
    return out
