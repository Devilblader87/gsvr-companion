#!/usr/bin/env python3
"""Small textured software rasteriser (stdlib only; fast enough under pypy3) for the map pack:
skybox face rendering at build time and the GSV3 pack preview (waw_mappack_preview.py).

Images are RGBA bytearrays, row 0 = top. Perspective-correct UV interpolation, z-buffer, nearest sampling.
"""
import math


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _norm(a):
    ln = math.sqrt(_dot(a, a)) or 1.0
    return (a[0] / ln, a[1] / ln, a[2] / ln)


class Target:
    def __init__(self, W, H, bg=(22, 24, 28)):
        self.W, self.H = W, H
        self.img = bytearray(bytes((bg[0], bg[1], bg[2], 255)) * (W * H))
        self.zb = [1e30] * (W * H)        # stores depth (smaller = nearer)
        self.hit = bytearray(W * H)

    def tri(self, P, attrs, shade, persp=True):
        """P: 3 x (sx, sy, depth > 0); attrs: 3 tuples of floats interpolated perspective-correctly (persp) or
        linearly (orthographic: depth only used for the z test); shade(attr tuple) -> (r, g, b) or None (discard)."""
        (x0, y0, z0), (x1, y1, z1), (x2, y2, z2) = P
        den = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
        if not abs(den) > 1e-9:
            return
        W, H = self.W, self.H
        minx, maxx = max(0, int(min(x0, x1, x2))), min(W - 1, int(max(x0, x1, x2)))
        miny, maxy = max(0, int(min(y0, y1, y2))), min(H - 1, int(max(y0, y1, y2)))
        if minx > maxx or miny > maxy:
            return
        if not persp:
            self._tri_ortho(P, attrs, shade, den, minx, maxx, miny, maxy)
            return
        iz0, iz1, iz2 = 1.0 / z0, 1.0 / z1, 1.0 / z2
        na = len(attrs[0])
        a0 = [v * iz0 for v in attrs[0]]
        a1 = [v * iz1 for v in attrs[1]]
        a2 = [v * iz2 for v in attrs[2]]
        inv = 1.0 / den
        zb, img, hit = self.zb, self.img, self.hit
        for py in range(miny, maxy + 1):
            fy = py + 0.5
            for px in range(minx, maxx + 1):
                fx = px + 0.5
                w0 = ((y1 - y2) * (fx - x2) + (x2 - x1) * (fy - y2)) * inv
                if w0 < 0:
                    continue
                w1 = ((y2 - y0) * (fx - x2) + (x0 - x2) * (fy - y2)) * inv
                if w1 < 0:
                    continue
                w2 = 1.0 - w0 - w1
                if w2 < 0:
                    continue
                iz = w0 * iz0 + w1 * iz1 + w2 * iz2
                z = 1.0 / iz
                k = py * W + px
                if z >= zb[k]:
                    continue
                at = tuple((w0 * a0[i] + w1 * a1[i] + w2 * a2[i]) * z for i in range(na))
                col = shade(at)
                if col is None:
                    continue
                zb[k] = z
                hit[k] = 1
                o = 4 * k
                img[o] = col[0]
                img[o + 1] = col[1]
                img[o + 2] = col[2]
                if len(col) > 3:
                    img[o + 3] = col[3]

    def _tri_ortho(self, P, attrs, shade, den, minx, maxx, miny, maxy):
        (x0, y0, z0), (x1, y1, z1), (x2, y2, z2) = P
        W = self.W
        na = len(attrs[0])
        a0, a1, a2 = attrs
        inv = 1.0 / den
        zb, img, hit = self.zb, self.img, self.hit
        for py in range(miny, maxy + 1):
            fy = py + 0.5
            for px in range(minx, maxx + 1):
                fx = px + 0.5
                w0 = ((y1 - y2) * (fx - x2) + (x2 - x1) * (fy - y2)) * inv
                if w0 < 0:
                    continue
                w1 = ((y2 - y0) * (fx - x2) + (x0 - x2) * (fy - y2)) * inv
                if w1 < 0:
                    continue
                w2 = 1.0 - w0 - w1
                if w2 < 0:
                    continue
                z = w0 * z0 + w1 * z1 + w2 * z2
                k = py * W + px
                if z >= zb[k]:
                    continue
                col = shade(tuple(w0 * a0[i] + w1 * a1[i] + w2 * a2[i] for i in range(na)))
                if col is None:
                    continue
                zb[k] = z
                hit[k] = 1
                o = 4 * k
                img[o] = col[0]
                img[o + 1] = col[1]
                img[o + 2] = col[2]
                if len(col) > 3:
                    img[o + 3] = col[3]


class Camera:
    """Perspective camera; world given in a frame where `right = cross(up, fwd)` (Unity, left-handed) or
    `right = cross(fwd, up)` (game axes, right-handed): pass right_handed accordingly."""

    def __init__(self, eye, fwd, up, W, H, fov_deg, right_handed, near=0.05):
        self.eye = eye
        self.f = _norm(fwd)
        r = _cross(self.f, up) if right_handed else _cross(up, self.f)
        self.r = _norm(r)
        self.u = _cross(self.r, self.f) if right_handed else _cross(self.f, self.r)
        self.W, self.H = W, H
        self.foc = (W / 2) / math.tan(math.radians(fov_deg / 2))
        self.near = near

    def project(self, p):
        d = (p[0] - self.eye[0], p[1] - self.eye[1], p[2] - self.eye[2])
        z = _dot(d, self.f)
        if z < self.near:
            return None
        return (self.W / 2 + self.foc * _dot(d, self.r) / z, self.H / 2 - self.foc * _dot(d, self.u) / z, z)

    def ray(self, px, py):
        x = (px + 0.5 - self.W / 2) / self.foc
        y = -(py + 0.5 - self.H / 2) / self.foc
        return _norm(tuple(self.f[k] + x * self.r[k] + y * self.u[k] for k in range(3)))


def clip_near(cam, tri, attrs):
    """Clip a triangle against the near plane -> list of (P3, attrs3) triangles (0..2)."""
    d = [_dot((p[0] - cam.eye[0], p[1] - cam.eye[1], p[2] - cam.eye[2]), cam.f) - cam.near for p in tri]
    if min(d) >= 0:
        P = [cam.project(p) for p in tri]
        return [(P, attrs)]
    if max(d) < 0:
        return []
    poly = []
    n = 3
    for i in range(n):
        j = (i + 1) % n
        if d[i] >= 0:
            poly.append((tri[i], attrs[i]))
        if (d[i] >= 0) != (d[j] >= 0):
            t = d[i] / (d[i] - d[j])
            p = tuple(tri[i][k] + t * (tri[j][k] - tri[i][k]) for k in range(3))
            a = tuple(attrs[i][k] + t * (attrs[j][k] - attrs[i][k]) for k in range(len(attrs[i])))
            poly.append((p, a))
    out = []
    for k in range(1, len(poly) - 1):
        tri3 = (poly[0], poly[k], poly[k + 1])
        P = [cam.project(x[0]) for x in tri3]
        if None in P:
            continue
        out.append((P, [x[1] for x in tri3]))
    return out


def render_sky_face(layers, tex, fwd, up, side):
    """layers: [(image name, [((p0, p1, p2) game axes, (uv0, uv1, uv2))])] drawn in this order without depth test
    between layers, alpha-blended by the texture alpha (the skybox model's surfaces are layered shells);
    tex: {name: (w, h, rgba)}. Camera at the origin, 90 degree face. Returns RGBA (top row first)."""
    cam = Camera((0.0, 0.0, 0.0), fwd, up, side, side, 90.0, right_handed=True, near=1.0)
    img = bytearray(bytes((90, 95, 100, 255)) * (side * side))
    for name, tris in layers:
        w, h, px = tex[name]

        def shade(at, w=w, h=h, px=px):
            u, v = at[0] % 1.0, at[1] % 1.0
            k = 4 * (int(v * h) % h * w + int(u * w) % w)
            return (px[k], px[k + 1], px[k + 2], px[k + 3])
        tg = Target(side, side, bg=(0, 0, 0))          # one shell: nearest triangle wins, alpha kept in channel 3
        for P3, UV3 in tris:
            for P, A in clip_near(cam, P3, [tuple(uv) for uv in UV3]):
                tg.tri(P, A, shade)
        src = tg.img
        for k in range(side * side):
            if not tg.hit[k]:
                continue
            o = 4 * k
            a = src[o + 3] / 255.0
            for c in range(3):
                img[o + c] = int(src[o + c] * a + img[o + c] * (1.0 - a) + 0.5)
    return img
