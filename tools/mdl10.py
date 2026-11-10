# GoldSrc studio model (MDL version 10) reader + pose baker — clean-room, written from the public file-format
# description (HLSDK studio.h struct layouts used only as a byte-layout reference; no code copied).
#
# usage: python3 -I mdl10.py <model.mdl> [--seq NAME|INDEX] [--frame N] [--obj out.obj]   (dump / OBJ export)
#
# What is read (all little endian):
#   header (244 B)        "IDST", version 10, name, length, eye/bbox, flags, then (count, offset) pairs for bones,
#                         bone controllers, hitboxes, sequences, sequence groups, textures (+ data offset), skins
#                         (numskinref, numskinfamilies, offset), bodyparts, attachments, sounds, transitions
#   bone (112 B)          name[32], parent, flags, controller[6], value[6] (default pos xyz + euler xyz), scale[6]
#   bone controller (24 B) bone, type, start, end, rest, index (0..3 entity controller, 4 mouth)
#   sequence (176 B)      label[32], fps, flags, activity, actweight, events, numframes, pivots, motiontype,
#                         motionbone, linearmovement, …, numblends, animindex, blend info, seqgroup, …
#   seqgroup (104 B)      label[32], name[64] (file of external groups "<model>NN.mdl"), 2 unused ints
#   anim                  per blend, per bone: 6 uint16 offsets (relative to that bone's anim record) to RLE
#                         value streams; an RLE block is a header short {valid:u8, total:u8} followed by `valid`
#                         int16 values; frames past `valid` (but < total) repeat the last value
#   bodypart (76 B)       name[64], nummodels, base, modelindex
#   model (112 B)         name[64], type, radius, nummesh, meshindex, numverts, vertinfoindex (u8 bone per vertex),
#                         vertindex (vec3), numnorms, norminfoindex (u8 bone per normal), normindex (vec3), groups
#   mesh (20 B)           numtris, triindex (tri commands), skinref, numnorms, normindex
#   tri commands          int16 n: 0 = end, > 0 strip of n, < 0 fan of −n; each vertex = 4 int16
#                         (vertex index, normal index, s, t) with s/t in texels
#   texture (80 B)        name[64], flags, width, height, data offset → w·h palette indices + 256·RGB palette
#                         flags: 2 chrome, 4 fullbright, 32 additive, 64 masked (index 255 = transparent)
#   skins                 int16 [numskinfamilies][numskinref] → texture index (mesh.skinref is the column)
#   attachment (88 B)     name[32], type, bone, org (in the bone's frame), vectors[3]
# Textures may live in a companion "<name>T.mdl" (header numtextures == 0 in the main file).
#
# Pose: bone local transform = translation(value[0..2] + anim·scale) · rotation(euler value[3..5] + anim·scale),
# euler applied as R = Rz(z)·Ry(y)·Rx(x); world = parent · local. Vertices/normals are stored in the frame of
# their bone and are transformed by that bone's world matrix. Bone controllers are left at rest (0).
# pose()/bake() also take a blend index (CS aim sequences: 9 blends = 3x3 yaw/pitch grid, 4 = centre), fractional
# frames (neighbouring frames lerped, angles by the shortest way) and gait=(seq, frame): the CS player-model rule
# that the legs (gait_bones()) play the gait sequence while the rest plays the upper-body sequence.
import math, os, struct, sys

STUDIO_NF_CHROME = 2
STUDIO_NF_FULLBRIGHT = 4
STUDIO_NF_ADDITIVE = 32
STUDIO_NF_MASKED = 64

# motion type bits (sequence root motion)
STUDIO_X, STUDIO_Y, STUDIO_Z = 1, 2, 4


def _cstr(b):
    return b.split(b"\0", 1)[0].decode("latin-1")


def mat_mul(a, b):
    """3x4 affine matrices as 12-tuples (row-major: r0 r1 r2, last column = translation)."""
    out = [0.0] * 12
    for r in range(3):
        a0, a1, a2, a3 = a[r * 4:r * 4 + 4]
        for c in range(4):
            v = a0 * b[c] + a1 * b[4 + c] + a2 * b[8 + c]
            if c == 3:
                v += a3
            out[r * 4 + c] = v
    return out


def euler_matrix(x, y, z, tx, ty, tz):
    """R = Rz(z)·Ry(y)·Rx(x) plus translation."""
    cx, sx = math.cos(x), math.sin(x)
    cy, sy = math.cos(y), math.sin(y)
    cz, sz = math.cos(z), math.sin(z)
    return [cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx, tx,
            sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx, ty,
            -sy, cy * sx, cy * cx, tz]


def xform(m, v):
    return (m[0] * v[0] + m[1] * v[1] + m[2] * v[2] + m[3],
            m[4] * v[0] + m[5] * v[1] + m[6] * v[2] + m[7],
            m[8] * v[0] + m[9] * v[1] + m[10] * v[2] + m[11])


def rotate(m, v):
    return (m[0] * v[0] + m[1] * v[1] + m[2] * v[2],
            m[4] * v[0] + m[5] * v[1] + m[6] * v[2],
            m[8] * v[0] + m[9] * v[1] + m[10] * v[2])


class Texture:
    __slots__ = ("name", "flags", "width", "height", "indices", "palette")

    def rgba(self):
        """→ bytes w·h·4, row 0 = first row in the file (top of the image as authored)."""
        pal = self.palette
        masked = bool(self.flags & STUDIO_NF_MASKED)
        out = bytearray(self.width * self.height * 4)
        o = 0
        for i in self.indices:
            out[o] = pal[i * 3]; out[o + 1] = pal[i * 3 + 1]; out[o + 2] = pal[i * 3 + 2]
            out[o + 3] = 0 if (masked and i == 255) else 255
            o += 4
        return bytes(out)


class Mdl:
    def __init__(self, path):
        self.path = path
        with open(path, "rb") as fh:
            d = self.data = fh.read()
        if d[:4] != b"IDST":
            raise ValueError(f"{path}: not a studio model (magic {d[:4]!r})")
        ver = struct.unpack_from("<i", d, 4)[0]
        if ver != 10:
            raise ValueError(f"{path}: studio version {ver}, expected 10")
        self.name = _cstr(d[8:72])
        h = struct.unpack_from("<i15fi26i", d, 72)
        self.length = h[0]
        self.eye = h[1:4]
        self.hullmin, self.hullmax = h[4:7], h[7:10]
        self.bbmin, self.bbmax = h[10:13], h[13:16]
        self.flags = h[16]
        (nbones, boneidx, nctl, ctlidx, nhb, hbidx, nseq, seqidx, nsg, sgidx, ntex, texidx, texdata,
         nskinref, nskinfam, skinidx, nbp, bpidx, natt, attidx, _st, _si, _sg, _sgi, _nt, _ti) = h[17:43]
        self.bones = []
        for i in range(nbones):
            o = boneidx + i * 112
            name = _cstr(d[o:o + 32])
            parent, flags = struct.unpack_from("<ii", d, o + 32)
            ctl = struct.unpack_from("<6i", d, o + 40)
            value = struct.unpack_from("<6f", d, o + 64)
            scale = struct.unpack_from("<6f", d, o + 88)
            self.bones.append(dict(name=name, parent=parent, flags=flags, ctl=ctl, value=value, scale=scale))
        self.seqgroups = []
        for i in range(nsg):
            o = sgidx + i * 104
            self.seqgroups.append(dict(label=_cstr(d[o:o + 32]), name=_cstr(d[o + 32:o + 96]),
                                       data=struct.unpack_from("<i", d, o + 100)[0]))
        self.sequences = []
        for i in range(nseq):
            o = seqidx + i * 176
            label = _cstr(d[o:o + 32])
            fps, sflags, act, actw, nev, evi, nframes, npiv, pivi, mtype, mbone = struct.unpack_from("<f10i", d, o + 32)
            lin = struct.unpack_from("<3f", d, o + 76)
            nblends, animidx = struct.unpack_from("<2i", d, o + 120)
            btype = struct.unpack_from("<2i", d, o + 128)
            bstart = struct.unpack_from("<2f", d, o + 136)
            bend = struct.unpack_from("<2f", d, o + 144)
            seqgroup = struct.unpack_from("<i", d, o + 156)[0]
            events = []
            for e in range(nev):
                eo = evi + e * 76                               # frame, event, type, options[64]
                fr, ev, _ty = struct.unpack_from("<3i", d, eo)
                events.append((fr, ev, _cstr(d[eo + 12:eo + 76])))
            self.sequences.append(dict(label=label, fps=fps, flags=sflags, activity=act, actweight=actw, numframes=nframes,
                                       motiontype=mtype, motionbone=mbone, linear=lin, numblends=nblends,
                                       animindex=animidx, seqgroup=seqgroup, events=events,
                                       blendtype=btype, blendstart=bstart, blendend=bend))
        # bone controllers (24 B): bone, type (STUDIO_X/Y/Z 1/2/4 translation, XR/YR/ZR 8/16/32 rotation, 0x8000 RLOOP), start, end,
        # rest, index (0..3 = entity controller, 4 = mouth)
        self.controllers = []
        for i in range(nctl):
            bone, typ, cstart, cend, rest, index = struct.unpack_from("<2i2f2i", d, ctlidx + i * 24)
            self.controllers.append(dict(bone=bone, type=typ, start=cstart, end=cend, rest=rest, index=index))
        self.attachments = []
        for i in range(natt):
            o = attidx + i * 88
            name = _cstr(d[o:o + 32])
            _ty, bone = struct.unpack_from("<ii", d, o + 32)
            org = struct.unpack_from("<3f", d, o + 40)
            self.attachments.append(dict(name=name, bone=bone, org=org))
        self.bodyparts = []
        for i in range(nbp):
            o = bpidx + i * 76
            name = _cstr(d[o:o + 64])
            nmodels, base, midx = struct.unpack_from("<3i", d, o + 64)
            models = []
            for m in range(nmodels):
                mo = midx + m * 112
                models.append(self._read_model(mo))
            self.bodyparts.append(dict(name=name, base=base, models=models))
        # textures + skins (maybe from <name>T.mdl)
        tsrc, tntex, ttexidx, tnskinref, tnskinfam, tskinidx = d, ntex, texidx, nskinref, nskinfam, skinidx
        self.texture_file = path
        if ntex == 0:
            base, ext = os.path.splitext(path)
            for cand in (base + "T" + ext, base + "t" + ext, base + "T" + ext.upper()):
                if os.path.exists(cand):
                    with open(cand, "rb") as fh:
                        tsrc = fh.read()
                    th = struct.unpack_from("<i15fi26i", tsrc, 72)
                    tntex, ttexidx = th[27], th[28]
                    tnskinref, tnskinfam, tskinidx = th[30], th[31], th[32]
                    self.texture_file = cand
                    break
        self.textures = []
        for i in range(tntex):
            o = ttexidx + i * 80
            t = Texture()
            t.name = _cstr(tsrc[o:o + 64])
            t.flags, t.width, t.height, idx = struct.unpack_from("<4i", tsrc, o + 64)
            t.indices = tsrc[idx:idx + t.width * t.height]
            t.palette = tsrc[idx + t.width * t.height: idx + t.width * t.height + 768]
            self.textures.append(t)
        self.skins = []
        for f in range(tnskinfam):
            self.skins.append(list(struct.unpack_from(f"<{tnskinref}h", tsrc, tskinidx + f * tnskinref * 2)))
        self._groupdata = {}

    def scale_uniform(self, s):
        """Scales the whole model by s in place (tools/modelpack.py MODEL_STANDINS: a stand-in drawn at another monster's size).
        Bone translations (rest value + anim scale of the x/y/z channels), vertices, sequence linear movement, attachment offsets,
        translation bone controllers and the header boxes scale; rotations / normals / uvs stay. Every pose = the original pose
        scaled about the model origin, so baked meshes, key times, anim speed and distance all follow."""
        if s == 1.0:
            return self
        for b in self.bones:
            b["value"] = tuple(v * s if j < 3 else v for j, v in enumerate(b["value"]))
            b["scale"] = tuple(v * s if j < 3 else v for j, v in enumerate(b["scale"]))
        for bp in self.bodyparts:
            for mo in bp["models"]:
                mo["verts"] = [(x * s, y * s, z * s) for x, y, z in mo["verts"]]
        for sq in self.sequences:
            sq["linear"] = tuple(c * s for c in sq["linear"])
        for a in self.attachments:
            a["org"] = tuple(c * s for c in a["org"])
        for c in self.controllers:
            if c["type"] & 7:                       # STUDIO_X / Y / Z: a translation range in units
                c["start"] *= s
                c["end"] *= s
        self.eye = tuple(c * s for c in self.eye)
        self.hullmin, self.hullmax = tuple(c * s for c in self.hullmin), tuple(c * s for c in self.hullmax)
        self.bbmin, self.bbmax = tuple(c * s for c in self.bbmin), tuple(c * s for c in self.bbmax)
        return self

    def _read_model(self, mo):
        d = self.data
        name = _cstr(d[mo:mo + 64])
        (_ty, _rad, nmesh, meshidx, nverts, vinfo, vidx, nnorms, ninfo, nidx, _ng, _gi) = \
            struct.unpack_from("<if10i", d, mo + 64)
        verts = [struct.unpack_from("<3f", d, vidx + i * 12) for i in range(nverts)]
        vbone = list(d[vinfo:vinfo + nverts])
        norms = [struct.unpack_from("<3f", d, nidx + i * 12) for i in range(nnorms)]
        nbone = list(d[ninfo:ninfo + nnorms])
        meshes = []
        for m in range(nmesh):
            numtris, triidx, skinref, _nn, _ni = struct.unpack_from("<5i", d, meshidx + m * 20)
            meshes.append(dict(skinref=skinref, tris=self._read_tricmds(triidx)))
        return dict(name=name, verts=verts, vbone=vbone, norms=norms, nbone=nbone, meshes=meshes)

    def _read_tricmds(self, o):
        """→ list of triangles, each 3 × (vertex index, normal index, s, t), in the file's own winding:
        strip i: (i-2, i-1, i) for even i, (i-1, i-2, i) for odd; fan: (0, i-1, i)."""
        d = self.data
        tris = []
        while True:
            n = struct.unpack_from("<h", d, o)[0]; o += 2
            if n == 0:
                break
            fan = n < 0
            n = abs(n)
            pts = [struct.unpack_from("<4h", d, o + k * 8) for k in range(n)]
            o += n * 8
            for i in range(2, n):
                if fan:
                    tris.append((pts[0], pts[i - 1], pts[i]))
                elif i % 2 == 0:
                    tris.append((pts[i - 2], pts[i - 1], pts[i]))
                else:
                    tris.append((pts[i - 1], pts[i - 2], pts[i]))
        return tris

    # ───────────── animation ─────────────

    def _anim_source(self, seq):
        """→ (bytes, base offset) holding the anim records of this sequence."""
        g = seq["seqgroup"]
        if g == 0:
            return self.data, seq["animindex"]
        if g not in self._groupdata:
            base, ext = os.path.splitext(self.path)
            p = f"{base}{g:02d}{ext}"
            with open(p, "rb") as fh:
                self._groupdata[g] = fh.read()
        return self._groupdata[g], self.seqgroups[g]["data"] + seq["animindex"]

    @staticmethod
    def _anim_value(d, p, frame):
        """Decode one RLE channel at absolute offset p for the given frame."""
        k = frame
        guard = 0
        while True:
            valid, total = d[p], d[p + 1]
            if total > k or total == 0 or guard > 4096:
                break
            k -= total
            p += (valid + 1) * 2
            guard += 1
        if valid > k:
            return struct.unpack_from("<h", d, p + (k + 1) * 2)[0]
        return struct.unpack_from("<h", d, p + valid * 2)[0] if valid > 0 else 0

    def find_sequence(self, *names):
        """First sequence whose label equals (case-insensitive) one of names, else whose label starts with one."""
        low = [s["label"].lower() for s in self.sequences]
        for n in names:
            if n.lower() in low:
                return low.index(n.lower())
        for n in names:
            for i, l in enumerate(low):
                if l.startswith(n.lower()):
                    return i
        return None

    def _locals_int(self, seq, frame, blend):
        """Per bone [tx, ty, tz, rx, ry, rz] of one integer frame (rest values without a sequence)."""
        nb = len(self.bones)
        out = [list(b["value"]) for b in self.bones]
        if seq is None:
            return out
        frame = max(0, min(frame, seq["numframes"] - 1))
        d, base = self._anim_source(seq)
        base += min(blend, max(0, seq["numblends"] - 1)) * nb * 12
        for i, b in enumerate(self.bones):
            vals = out[i]
            ao = base + i * 12
            offs = struct.unpack_from("<6H", d, ao)
            for j in range(6):
                if offs[j]:
                    vals[j] = b["value"][j] + self._anim_value(d, ao + offs[j], frame) * b["scale"][j]
            if i == seq["motionbone"]:
                mt = seq["motiontype"]
                if mt & STUDIO_X: vals[0] = 0.0
                if mt & STUDIO_Y: vals[1] = 0.0
                if mt & STUDIO_Z: vals[2] = 0.0
        return out

    def _locals(self, seq_index, frame, blend):
        """Like _locals_int, but a fractional frame lerps the two neighbouring frames (angles by their shortest
        difference); between the last frame and n it blends towards frame 0 (looping gait cycles). Integer frames are
        clamped as before."""
        seq = self.sequences[max(0, min(seq_index, len(self.sequences) - 1))] if self.sequences else None
        if seq is None:
            return self._locals_int(None, 0, 0)
        n = seq["numframes"]
        f0 = int(math.floor(frame))
        t = frame - f0
        a = self._locals_int(seq, f0, blend)
        if t < 1e-6 or f0 < 0 or f0 >= n:
            return a
        b = self._locals_int(seq, f0 + 1 if f0 + 1 < n else 0, blend)
        out = []
        for va, vb in zip(a, b):
            v = []
            for j in range(6):
                dlt = vb[j] - va[j]
                if j >= 3:
                    dlt = (dlt + math.pi) % (2 * math.pi) - math.pi
                v.append(va[j] + dlt * t)
            out.append(v)
        return out

    def gait_bones(self):
        """Bones driven by the gait (leg) sequence when a player model combines two sequences — the CS client rule
        (cl_dll GameStudioModelRenderer): walking the bone list in order, a bone named "Bip01 Spine" switches copying
        off, a bone whose parent is "Bip01 Pelvis" switches it on; root and pelvis come first and are copied."""
        out, copy = set(), True
        for i, b in enumerate(self.bones):
            if b["name"] == "Bip01 Spine":
                copy = False
            elif b["parent"] >= 0 and self.bones[b["parent"]]["name"] == "Bip01 Pelvis":
                copy = True
            if copy:
                out.add(i)
        return out

    CTL_AXIS = {1: 0, 2: 1, 4: 2, 8: 3, 16: 4, 32: 5}      # controller type bit -> local value slot (tx ty tz rx ry rz)

    def pose(self, seq_index=0, frame=0, blend=0, gait=None, ctl=None):
        """→ list of bone world matrices (3x4) for a sequence frame (clamped; fractional frames interpolate).
        gait = (seq_index, frame): the leg bones (gait_bones()) take that sequence instead — CS player models play
        an upper-body sequence (aim/shoot per weapon class) over a gait sequence (idle1/walk/run/crouch...).
        ctl = per bone-controller record (self.controllers order) an adjustment added to that bone's local value
        (radians for XR/YR/ZR, units for X/Y/Z) — the reference for the runtime's model-space controller rotations."""
        return self.world_of(self.pose_locals(seq_index, frame, blend, gait, ctl))

    def pose_locals(self, seq_index=0, frame=0, blend=0, gait=None, ctl=None):
        """The per-bone local values [tx, ty, tz, rx, ry, rz] pose() builds its matrices from."""
        nb = len(self.bones)
        vals = self._locals(seq_index, frame, blend)
        if gait is not None and self.sequences:
            gv = self._locals(gait[0], gait[1], 0)
            for i in self.gait_bones():
                vals[i] = gv[i]
        if ctl:
            for c, adj in zip(self.controllers, ctl):
                j = self.CTL_AXIS.get(c["type"] & 0x3F)
                if j is not None and 0 <= c["bone"] < nb and adj:
                    vals[c["bone"]][j] += adj
        return vals

    def world_of(self, vals):
        """Local values per bone → world matrices (parent before child, the file's bone order)."""
        nb = len(self.bones)
        world = [None] * nb
        for i, b in enumerate(self.bones):
            v = vals[i]
            local = euler_matrix(v[3], v[4], v[5], v[0], v[1], v[2])
            p = b["parent"]
            world[i] = local if p < 0 else mat_mul(world[p], local)
        return world

    def bake(self, seq_index=0, frame=0, skin=0, bodygroups=None, blend=0, gait=None):
        """Posed geometry → list of submeshes: dict(tex=texture index, tris=[3 × (pos, normal, s, t, bone, key)]).
        pos/normal in GoldSrc model space (x forward, y left, z up; units = inches), s/t normalised by the texture
        size, key = (bodypart, vertex, normal, s, t, texture) identifies the source vertex (identical across poses,
        so meshes baked from different frames share one topology)."""
        world = self.pose(seq_index, frame, blend, gait)
        fam =self.skins[min(skin, len(self.skins) - 1)] if self.skins else None
        out = []
        for bi, bp in enumerate(self.bodyparts):
            sel = 0 if not bodygroups else bodygroups.get(bi, 0)
            if not bp["models"]:
                continue
            mdl = bp["models"][min(sel, len(bp["models"]) - 1)]
            pv = [xform(world[b], v) for v, b in zip(mdl["verts"], mdl["vbone"])]
            pn = [rotate(world[b], n) for n, b in zip(mdl["norms"], mdl["nbone"])]
            for m in mdl["meshes"]:
                ti = fam[m["skinref"]] if fam and m["skinref"] < len(fam) else m["skinref"]
                tex = self.textures[ti] if 0 <= ti < len(self.textures) else None
                tw = tex.width if tex else 1
                th = tex.height if tex else 1
                tris = []
                for tri in m["tris"]:
                    tris.append(tuple((pv[vi], pn[ni] if ni < len(pn) else (0.0, 0.0, 1.0), s / tw, t / th,
                                       mdl["vbone"][vi], (bi, vi, ni, s, t, ti)) for vi, ni, s, t in tri))
                out.append(dict(tex=ti, tris=tris))
        return out

    def bake_parts(self, seq_index=0, frame=0, skin=0, world=None):
        """Every model of every bodypart in one pose → {(bodypart, model): submeshes like bake()}. The vertex key is
        (bodypart, model, vertex, normal, s, t, texture), so two models of one bodypart never share a key. Monster
        packs keep each bodypart model as a mesh of its own (scientist heads, Barney's holstered / drawn gun), all with
        the same poses; body value → model per bodypart is the usual (body / base) % models. world = precomputed
        pose() matrices (optional)."""
        if world is None:
            world = self.pose(seq_index, frame)
        fam = self.skins[min(skin, len(self.skins) - 1)] if self.skins else None
        out = {}
        for bi, bp in enumerate(self.bodyparts):
            for mi, mdl in enumerate(bp["models"]):
                pv = [xform(world[b], v) for v, b in zip(mdl["verts"], mdl["vbone"])]
                pn = [rotate(world[b], n) for n, b in zip(mdl["norms"], mdl["nbone"])]
                subs = []
                for m in mdl["meshes"]:
                    ti = fam[m["skinref"]] if fam and m["skinref"] < len(fam) else m["skinref"]
                    tex = self.textures[ti] if 0 <= ti < len(self.textures) else None
                    tw = tex.width if tex else 1
                    th = tex.height if tex else 1
                    tris = [tuple((pv[vi], pn[ni] if ni < len(pn) else (0.0, 0.0, 1.0), s / tw, t / th,
                                   mdl["vbone"][vi], (bi, mi, vi, ni, s, t, ti)) for vi, ni, s, t in tri)
                            for tri in m["tris"]]
                    subs.append(dict(tex=ti, tris=tris))
                out[(bi, mi)] = subs
        return out

    def attachment_pos(self, index, world):
        a = self.attachments[index]
        return xform(world[a["bone"]], a["org"])

    def summary(self):
        lines = [f"{os.path.basename(self.path)}: '{self.name}' bones {len(self.bones)}, seq {len(self.sequences)}, "
                 f"groups {len(self.seqgroups)}, tex {len(self.textures)} (from {os.path.basename(self.texture_file)}), "
                 f"skins {len(self.skins)}, bodyparts {len(self.bodyparts)}, attachments {len(self.attachments)}"]
        for i, b in enumerate(self.bones):
            lines.append(f"  bone {i:2d} {b['name']:<24} parent {b['parent']}")
        for i, s in enumerate(self.sequences):
            lines.append(f"  seq {i:2d} {s['label']:<20} frames {s['numframes']:3d} fps {s['fps']:.0f} group {s['seqgroup']}"
                         f" events {[(e[0], e[1], e[2]) for e in s['events']][:4]}")
        for t in self.textures:
            lines.append(f"  tex {t.name:<28} {t.width}x{t.height} flags {t.flags}")
        for bp in self.bodyparts:
            lines.append(f"  bodypart {bp['name']}: " + ", ".join(
                f"{m['name']} ({len(m['verts'])} v, {sum(len(x['tris']) for x in m['meshes'])} tris)" for m in bp["models"]))
        for a in self.attachments:
            lines.append(f"  attachment {a['name']!r} bone {a['bone']} org {tuple(round(x, 2) for x in a['org'])}")
        return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("mdl")
    ap.add_argument("--seq", default="0")
    ap.add_argument("--frame", type=int, default=0)
    ap.add_argument("--obj")
    a = ap.parse_args()
    m = Mdl(a.mdl)
    print(m.summary())
    si = int(a.seq) if a.seq.isdigit() else m.find_sequence(a.seq)
    subs = m.bake(si or 0, a.frame)
    pts = [v[0] for s in subs for t in s["tris"] for v in t]
    if pts:
        mn = [min(p[k] for p in pts) for k in range(3)]
        mx = [max(p[k] for p in pts) for k in range(3)]
        print(f"posed bounds (seq {si}, frame {a.frame}): min {[round(x, 1) for x in mn]} max {[round(x, 1) for x in mx]}"
              f" → {[round((mx[k] - mn[k]) * 0.0254, 3) for k in range(3)]} m")
    if a.obj:
        with open(a.obj, "w") as fh:
            n = 1
            for s in subs:
                for t in s["tris"]:
                    for p, _nn, _s, _t, _b, _k in t:
                        fh.write(f"v {p[0]:.3f} {p[1]:.3f} {p[2]:.3f}\n")
                    fh.write(f"f {n} {n + 1} {n + 2}\n")
                    n += 3
