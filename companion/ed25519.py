"""Pure-python Ed25519 (RFC 8032, the reference algorithm), stdlib only.

Used by modules.py to verify the signature of the module manifest (verify), and by packaging/modtool.py and the tests to
sign (sign / publickey). It is NOT constant time: fine for verifying public data and for an offline maintainer tool; the
private seed never lives in the companion. Pinned by the RFC 8032 section 7.1 test vectors (test_modules.py).
"""
import hashlib

_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _sha512(b):
    return hashlib.sha512(b).digest()


def _inv(x):
    return pow(x, _P - 2, _P)


def _xrecover(y):
    xx = (y * y - 1) * _inv(_D * y * y + 1)
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P != 0:
        x = x * _I % _P
    if (x * x - xx) % _P != 0:
        raise ValueError("not a curve point")
    return _P - x if x & 1 else x


_BY = 4 * _inv(5) % _P
_BX = _xrecover(_BY)
_B = (_BX, _BY, 1, _BX * _BY % _P)        # extended coordinates (X, Y, Z, T)


def _add(p, q):
    a = (p[1] - p[0]) * (q[1] - q[0]) % _P
    b = (p[1] + p[0]) * (q[1] + q[0]) % _P
    c = 2 * p[3] * q[3] * _D % _P
    d = 2 * p[2] * q[2] % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _mul(s, p):
    q = (0, 1, 1, 0)
    while s:
        if s & 1:
            q = _add(q, p)
        p = _add(p, p)
        s >>= 1
    return q


def _encode(p):
    zi = _inv(p[2])
    x, y = p[0] * zi % _P, p[1] * zi % _P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _decode(b):
    if len(b) != 32:
        raise ValueError("bad point length")
    y = int.from_bytes(b, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    if y >= _P:
        raise ValueError("non-canonical point")
    x = _xrecover(y)
    if x == 0 and sign:
        raise ValueError("bad point")
    if x & 1 != sign:
        x = _P - x
    return (x, y, 1, x * y % _P)


def _eq(p, q):
    return (p[0] * q[2] - q[0] * p[2]) % _P == 0 and (p[1] * q[2] - q[1] * p[2]) % _P == 0


def _clamp(h):
    a = int.from_bytes(h[:32], "little")
    return (a & ((1 << 254) - 8)) | (1 << 254)


def publickey(seed):
    """32-byte private seed -> 32-byte public key"""
    return _encode(_mul(_clamp(_sha512(seed)), _B))


def sign(seed, msg):
    h = _sha512(seed)
    a = _clamp(h)
    pub = _encode(_mul(a, _B))
    r = int.from_bytes(_sha512(h[32:] + msg), "little") % _L
    rb = _encode(_mul(r, _B))
    k = int.from_bytes(_sha512(rb + pub + msg), "little") % _L
    return rb + ((r + k * a) % _L).to_bytes(32, "little")


def verify(pub, msg, sig):
    """True only for a valid signature; never raises on malformed input."""
    try:
        if len(pub) != 32 or len(sig) != 64:
            return False
        s = int.from_bytes(sig[32:], "little")
        if s >= _L:
            return False
        a = _decode(pub)
        r = _decode(sig[:32])
        k = int.from_bytes(_sha512(sig[:32] + pub + msg), "little") % _L
        return _eq(_mul(s, _B), _add(r, _mul(k, a)))
    except (ValueError, TypeError):
        return False
