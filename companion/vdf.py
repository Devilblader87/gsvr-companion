"""Minimal Valve KeyValues (VDF/ACF) text parser — stdlib only.

Handles: quoted strings with \\ \" \n \t escapes, unquoted tokens, nested { } blocks,
// line comments and [$PLATFORM] conditionals (ignored). Keys are lower-cased
(KeyValues is case-insensitive); on duplicate keys the last value wins.
"""

_ESC = {"n": "\n", "t": "\t", "\\": "\\", '"': '"'}


def _tokens(text):
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
        elif c in "{}":
            yield c
            i += 1
        elif c == "[":                       # conditional like [$WIN32] — skip
            j = text.find("]", i)
            i = n if j < 0 else j + 1
        elif c == '"':
            i += 1
            out = []
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    out.append(_ESC.get(text[i + 1], "\\" + text[i + 1]))
                    i += 2
                else:
                    out.append(text[i])
                    i += 1
            i += 1                           # closing quote
            yield ("s", "".join(out))
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in '{}"':
                j += 1
            yield ("s", text[i:j])
            i = j


def loads(text):
    """Parse KeyValues text into nested dicts (str -> str | dict)."""
    root, stack, key = {}, [], None
    cur = root
    for tok in _tokens(text):
        if tok == "{":
            if key is None:
                raise ValueError("'{' without key")
            child = {}
            cur[key] = child
            stack.append(cur)
            cur, key = child, None
        elif tok == "}":
            if not stack:
                raise ValueError("unbalanced '}'")
            cur, key = stack.pop(), None
        else:
            s = tok[1]
            if key is None:
                key = s.lower()
            else:
                cur[key] = s
                key = None
    if stack:
        raise ValueError("unterminated block")
    return root


def load(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return loads(fh.read())
