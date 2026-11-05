"""The converter scripts the frozen build bundles into _internal/tools (gsvr.spec) — one list, checked.

BUGS C-SYNC (warcraft3/docs/BUGS.md): gsvr.spec and tools/sync_war3.sh each had their own file list; w3spellart.py
was in neither, so every frozen companion skipped the spell art, and w3missileart.py would have crashed w3pack.
tool_files() returns the entry scripts plus everything they import from tools/ (transitively); missing_imports()
is what the selftest checks for any explicit list.
"""
import os
import re

ENTRIES = ["pack.py", "modelpack.py", "w3pack.py", "previewpack.py",       # what --run-converter starts (builder.py)
           "w3info.py", "war3fs.py", "w3mapfolders.py"]                      # imported by companion/war3.py (lobby map info)
_IMPORT = re.compile(r'^\s*(?:import|from)\s+([A-Za-z_]\w*)(?:\s+as\s+\w+)?', re.M)


def _imports(path):
    with open(path, encoding="utf-8") as fh:
        return set(_IMPORT.findall(fh.read()))


def missing_imports(tools, files):
    """-> sorted 'mod.py (imported by x.py)' for local tools modules that files import but do not contain."""
    have = set(files)
    out = set()
    for f in files:
        for mod in _imports(os.path.join(tools, f)):
            if os.path.isfile(os.path.join(tools, mod + ".py")) and mod + ".py" not in have:
                out.add("%s (imported by %s)" % (mod + ".py", f))
    return sorted(out)


def tool_files(tools):
    """the entries + their transitive tools/ imports, in a stable order."""
    files, todo = [], list(ENTRIES)
    while todo:
        f = todo.pop(0)
        if f in files or not os.path.isfile(os.path.join(tools, f)):
            continue
        files.append(f)
        for mod in sorted(_imports(os.path.join(tools, f))):
            if os.path.isfile(os.path.join(tools, mod + ".py")):
                todo.append(mod + ".py")
    return files
