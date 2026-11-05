"""W9-1 part D: expected-error allowlist (kept out of run.py to avoid merge conflicts)."""
import re


# W9-1 part D: errors that are expected for a map ([inf] the real game returns null / ignores them too); the VM
# behaviour is unchanged, only the report separates them. Per map key: regexes matched against the error text.
EXPECTED_ERRORS = {
    # Gem TD: 39 CreateUnit of 'Ng0:'.. at t=0 (audit GTD, ids built from a character range the map has no types for)
    'fan_GemTDPlus1613': [r"^t=0 CreateUnit: unknown unit type 'Ng0.+'$"],
}


def split_errors(map_name, errors):
    """-> (expected, unexpected) lists."""
    pats = [re.compile(p) for p in EXPECTED_ERRORS.get(map_name, [])]
    exp = [e for e in errors if any(p.search(e) for p in pats)]
    return exp, [e for e in errors if e not in set(exp)]


def error_summary(n_unexpected, n_expected):
    return '%d errors%s' % (n_unexpected, (' (%d expected)' % n_expected) if n_expected else '')
