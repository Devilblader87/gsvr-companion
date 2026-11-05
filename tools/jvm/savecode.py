"""SAVE-1 (docs/save_system.md "Stage 1: map save codes"): the codes of the fun maps' own "-save" / "-load <code>" commands.

Many fun maps (RPGs, hero sieges, survival) implement their own persistence: the chat command "-save" makes the map's JASS
show a code (DisplayTextToPlayer / DisplayTimedTextToForce ...), "-load <code>" (a chat event with a prefix string, not
exact) restores it. The game menu's Save Game / Load Game of this port do not touch the map: they

  * Save Game = "type" the map's save command (chat input kind 7, a table entry of the map's own TriggerRegisterPlayerChatEvent
    strings) and CAPTURE the code the map shows to that player afterwards (SaveCapture below; the U# twin is
    W3Vm.SaveFeed in W3SaveCode.cs),
  * the capture is stored per map in the player's persistence (SaveStore here; W3SaveStore.cs on the VRChat PlayerData),
  * Load Game = list the stored codes of the map and "type" `<load prefix><code>` as a chat message of that player
    (chat_text_input: a message that is not a table entry; the master matches it like a typed message).

Detection is heuristic and generic (extract_code); per-map overrides in MAP_CFG. Clean room: behaviour only, the texts are the
maps' own. Nothing here is the install's data.
"""
import re

COLOR_RE = re.compile(r'\|[cC][0-9a-fA-F]{8}|\|[rR]')
SEP_RE = re.compile(r'^[\s=\-_*~#.+:|<>\[\]()]*$')                 # a decoration line (===== / ----- / ******)
# a marker: "code:" / "Code -" / "password:" / "Passwort:" / "Save code =" (case-insensitive); the code follows on the line or
# on the next non-decoration line when nothing follows
MARK_RE = re.compile(r'(?i)(?:\b(?:code|codes|passwort|password|pass ?code|savecode|save ?key|schluessel|schlüssel)\b)\s*[:=\-–>]+\s*')
BAD_RE = re.compile(r'(?i)\b(error|fehler|invalid|ung[uü]ltig|cannot|can\'t|can not|kann nicht|need|brauchst|only|nur|wrong|falsch|'
                    r'unknown|not valid|failed|fehlgeschlagen|already|bereits)\b')
TOKEN_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9\-_=+/#@!$%&.]*$')

CODE_MAX = 128              # characters of one stored code (a longer one is not stored)
CODES_PER_MAP = 6           # most recent codes kept per map (Load Game lists them)
MAPS_MAX = 24               # maps with stored codes (the oldest map's codes go first); 24 x 6 x 128 = 18 KB at the very most
KEY_MAX = 48                # characters of a PlayerData key name made of a map key (the platform's own limit is not documented [inf])
VALUE_VERSION = 1

DEFAULT_CFG = {
    'save': ('-save', '-savecode', '-save code', '-sc', '-speichern'),   # chat strings (lower case) that start a capture; a table entry
    #                                                                      equal to one of them (ignoring case) is the map's save command
    'load': ('-load', '-loadcode', '-load code', '-lc', '-laden'),       # a table entry that starts with one of them is the load prefix
    'window': 30.0,                                                     # game seconds after the save command in which texts count
    'alpha': False,                                                     # a code of letters only (no digit) is accepted too
    'loose': True,                                                      # a lone long mixed token line counts without a marker
}
# per-map overrides: (a lower-case substring of the map's key / title, {cfg overrides}); first match wins. The generic
# heuristics already read the two fan maps of the repo that implement a code (Dawn of the Dead 4.1: "Save Successful - Code: "
# + one coloured block per character; Kiga TD: a "Your code:" line, the code on the next line between ===== lines); the
# entries make the intent explicit and let a map with an odd format be fixed without touching the heuristics.
MAP_CFG = (
    ('dawnofthedead', {}),
    ('kigatd', {'window': 10.0}),
)


def cfg_for(map_key):
    k = re.sub(r'[^a-z0-9]', '', (map_key or '').lower())
    c = dict(DEFAULT_CFG)
    for sub, over in MAP_CFG:
        if sub in k:
            c.update(over)
            break
    return c


def plain(s):
    """the text without WC3 colour codes; |n is a line break."""
    s = (s or '').replace('|n', '\n').replace('|N', '\n')
    return COLOR_RE.sub('', s)


def looks_like_code(tail, alpha=False):
    """one candidate (the rest of a marker line, or a whole line): True when it is shaped like a save code."""
    t = tail.strip().strip('.,;')
    if not 4 <= len(t) <= CODE_MAX or BAD_RE.search(t):
        return False
    toks = t.split()
    if not 1 <= len(toks) <= 12 or any(not TOKEN_RE.match(x) or len(x) > 64 for x in toks):
        return False
    dig = sum(1 for x in toks if any(c.isdigit() for c in x))
    if len(toks) == 1:
        return dig == 1 or (alpha and len(t) >= 8)
    return dig * 2 >= len(toks)                                     # "AB12 CD34 EF56": most groups hold a digit


def clean_code(tail):
    return ' '.join(tail.strip().strip('.,;').split())


def extract_code(lines, cfg=None):
    """lines = the plain text lines the map showed after the save command (in order) -> the first code or None."""
    cfg = cfg or DEFAULT_CFG
    alpha = bool(cfg.get('alpha'))
    pending = False
    for line in lines:
        ln = line.strip()
        if not ln:
            continue
        if SEP_RE.match(ln):
            continue                                                # decoration: keeps a pending marker
        m = MARK_RE.search(ln)
        if m:
            tail = ln[m.end():]
            if tail.strip():
                if looks_like_code(tail, alpha):
                    return clean_code(tail)
                pending = False
                continue
            pending = True                                          # "Your code:" ... the code is the next line
            continue
        if pending:
            pending = False
            if looks_like_code(ln, alpha):
                return clean_code(ln)
            continue
        if cfg.get('loose', True) and len(ln) >= 10 and ' ' not in ln and looks_like_code(ln, alpha) and \
                any(c.isdigit() for c in ln) and any(c.isalpha() for c in ln):
            return clean_code(ln)
    return None


def save_command(chat_cmds, cfg=None):
    """the table index and the exact spelling of the map's own save command (an entry equal to one of cfg['save'],
    ignoring case), else (-1, '')."""
    cfg = cfg or DEFAULT_CFG
    want = [w.lower() for w in cfg['save']]
    for w in want:                                                  # the preferred spelling first
        for i, s in enumerate(chat_cmds):
            if s.strip().lower() == w:
                return i, s
    return -1, ''


def load_prefix(chat_cmds, cfg=None):
    """the exact prefix of the map's load command: the shortest table entry that starts with one of cfg['load']
    (ignoring case); a map registers it case-sensitively ("-Load " in Dawn of the Dead), so the message must repeat the spelling
    of the registration. None when the map has none."""
    cfg = cfg or DEFAULT_CFG
    best = None
    for s in chat_cmds:
        sl = s.lower()
        if any(sl.startswith(w) for w in cfg['load']) and (best is None or len(s) < len(best)):
            best = s
    if best is None:
        return None
    return best if best.endswith(' ') else best + ' '


def is_save_message(msg, cfg=None):
    cfg = cfg or DEFAULT_CFG
    m = (msg or '').strip().lower()
    return any(m == w for w in cfg['save'])


class SaveCapture:
    """The capture state of one running map (per player slot): a save command of the player arms it, the texts shown to the
    player in the next cfg['window'] game seconds are scanned, the first code is kept (seq counts the captures so a
    watcher sees a new one even when the code repeats)."""

    def __init__(self, cfg=None):
        self.cfg = cfg or DEFAULT_CFG
        self.until = {}          # player -> game seconds the capture lasts
        self.pending = {}        # player -> a marker line without a code was seen (the next line is the code)
        self.code = {}           # player -> last captured code
        self.seq = {}            # player -> number of captures
        self.log = []            # (player, code) in capture order

    def on_chat(self, player, msg, now):
        if is_save_message(msg, self.cfg):
            self.until[player] = now + float(self.cfg['window'])
            self.pending[player] = False

    def on_text(self, player, text, now):
        """a text shown to player; -> the captured code or None."""
        if player not in self.until or now > self.until[player]:
            return None
        lines = plain(text).split('\n')
        if self.pending.get(player):
            lines = ['Code:'] + lines                               # the marker line came with an earlier message
        c = extract_code(lines, self.cfg)
        if c is None:
            low = [ln for ln in lines if ln.strip() and not SEP_RE.match(ln.strip())]
            m = MARK_RE.search(low[-1]) if low else None
            self.pending[player] = m is not None and not low[-1][m.end():].strip()    # "Your code:" alone: the next text is the code
            return None
        del self.until[player]
        self.pending[player] = False
        self.code[player] = c
        self.seq[player] = self.seq.get(player, 0) + 1
        self.log.append((player, c))
        return c


# ---------------------------------------------------------------- the store (VRChat PlayerData stand-in)

def safe_key(map_key):
    """the PlayerData key suffix of a map key: ASCII letters / digits / . _ - only, at most KEY_MAX; anything else is replaced by "_"
    and a hash of the original is appended, so two map keys never share a key. Same arithmetic in W3SaveStore.SafeKey."""
    h = 0
    for c in map_key:
        h = (h * 31 + ord(c)) % 1000003
    t = ''.join(c if (c.isascii() and (c.isalnum() or c in '._-')) else '_' for c in map_key)
    if t != map_key or len(t) > KEY_MAX:
        t = t[:KEY_MAX - 9] + '~' + str(h)
    return t


class PlayerDataStub:
    """What the world keeps of the local player: key -> str | bytes | int | float. The VRChat limit is about 100 KB per
    player and world [ext, maintainer 2026-11-02]; `used()` counts key + value bytes (the real accounting is the platform's,
    the guard here is conservative)."""
    LIMIT = 102400

    def __init__(self, limit=None):
        self.d = {}
        self.limit = limit or self.LIMIT

    @staticmethod
    def _n(v):
        return len(v.encode('utf-8')) if isinstance(v, str) else (len(v) if isinstance(v, (bytes, bytearray)) else 8)

    def used(self):
        return sum(len(k.encode('utf-8')) + self._n(v) for k, v in self.d.items())

    def set(self, key, value):
        """True when stored; False (nothing changed) when the limit would be exceeded."""
        old = self.d.get(key)
        delta = len(key.encode('utf-8')) + self._n(value) - ((len(key.encode('utf-8')) + self._n(old)) if old is not None else 0)
        if self.used() + delta > self.limit:
            return False
        self.d[key] = value
        return True

    def get(self, key):
        return self.d.get(key)

    def has(self, key):
        return key in self.d

    def delete(self, key):
        self.d.pop(key, None)


class SaveStore:
    """Per map the most recent save codes of the local player (newest first). Keys: "w3s.m" = the map keys, newest use
    first, one per line (at most MAPS_MAX); "w3s.c.<map key>" = "1" + newline + codes, one per line."""
    KEY_MAPS = 'w3s.m'
    KEY_CODE = 'w3s.c.'

    def __init__(self, pd=None):
        self.pd = pd if pd is not None else PlayerDataStub()

    def _maps(self):
        s = self.pd.get(self.KEY_MAPS)
        return [x for x in (s or '').split('\n') if x]

    def codes(self, map_key):
        s = self.pd.get(self.KEY_CODE + safe_key(map_key))
        if not s:
            return []
        ls = s.split('\n')
        if ls[0] != str(VALUE_VERSION):
            return []
        return [x for x in ls[1:] if x]

    def add(self, map_key, code):
        """store a code as the newest of the map; -> True when stored (False: refused, nothing changed)."""
        if not map_key or not code or len(code) > CODE_MAX or '\n' in code or '\n' in map_key:
            return False
        cur = [c for c in self.codes(map_key) if c != code]
        cur.insert(0, code)
        cur = cur[:CODES_PER_MAP]
        maps = [m for m in self._maps() if m != map_key]
        maps.insert(0, map_key)
        drop = maps[MAPS_MAX:]
        maps = maps[:MAPS_MAX]
        for m in drop:
            self.pd.delete(self.KEY_CODE + safe_key(m))
        ok = self.pd.set(self.KEY_CODE + safe_key(map_key), str(VALUE_VERSION) + '\n' + '\n'.join(cur))
        if not ok:
            return False
        self.pd.set(self.KEY_MAPS, '\n'.join(maps))
        return True

    def remove(self, map_key, code):
        cur = [c for c in self.codes(map_key) if c != code]
        if cur:
            self.pd.set(self.KEY_CODE + safe_key(map_key), str(VALUE_VERSION) + '\n' + '\n'.join(cur))
        else:
            self.pd.delete(self.KEY_CODE + safe_key(map_key))
            self.pd.set(self.KEY_MAPS, '\n'.join(m for m in self._maps() if m != map_key))
