#!/usr/bin/env python3
"""
Render the pepper brand assets for the installer from a single geometry.

Outputs, written to --out:
  pepper-splash.gif   460x300 looping splash for Velopack's --splashImage
  pepper-mark.svg     vector mark (docs, About box, a future custom installer)
  pepper.ico          Windows icon, 16-256 px; small sizes use a simplified mark

Only dependency is Pillow. Fonts: fonts/BarlowSemiCondensed-*.ttf (SIL OFL 1.1).

    python render_assets.py --name "Spice Maker" --version 1.0.0 --out assets

Velopack draws its own progress bar over the bottom 12 px of the splash, full
width, in --splashProgressColor. The splash keeps that strip as a plain track,
so pass the chili red (#B52A1F) as the progress colour.
"""

import argparse
import io
import itertools
import math
import struct
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parent
FONT_DIR = HERE / "fonts"

# ---------------------------------------------------------------- palette ---
PAPER = (235, 239, 236)  # drafting film
GRID = (176, 187, 180)  # schematic dot grid
EDGE = (140, 150, 144)  # window outline
INK = (29, 43, 58)  # technical-pen navy: outlines, lettering
INK_SOFT = (86, 99, 112)  # secondary lettering
CHILI = (181, 42, 31)  # body fill, progress bar
CHILI_HOT = (206, 58, 36)  # body at the "powered" peak of the loop
TRACE = (226, 142, 126)  # copper traces under red solder mask
STEM = (79, 122, 58)  # calyx and stem
TRACK = (214, 220, 215)  # strip Velopack paints its progress bar over
HOT = (255, 244, 222)  # signal pulse head
SIGNAL = (242, 104, 52)  # signal pulse body
GLOW = (247, 166, 96)  # signal pulse halo

SPLASH_W, SPLASH_H = 460, 300
TRACK_H = 12  # Velopack: 12 px * DPI scale, flush with the bottom
SS = 4  # supersampling factor for anti-aliasing


def hexc(rgb):
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def mix(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


# --------------------------------------------------------------- geometry ---
def bez(p, t):
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = p
    u = 1 - t
    return (
        u**3 * x0 + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t**3 * x3,
        u**3 * y0 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t**3 * y3,
    )


def bez_d(p, t):
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = p
    u = 1 - t
    return (
        3 * u * u * (x1 - x0) + 6 * u * t * (x2 - x1) + 3 * t * t * (x3 - x2),
        3 * u * u * (y1 - y0) + 6 * u * t * (y2 - y1) + 3 * t * t * (y3 - y2),
    )


def unit(v):
    n = math.hypot(*v) or 1.0
    return (v[0] / n, v[1] / n)


def add(p, v, k=1.0):
    return (p[0] + v[0] * k, p[1] + v[1] * k)


class Pepper:
    """The schematic pepper, in splash pixel coordinates.

    The body is a curved centreline with a width profile. The stem is a lead
    that leaves the calyx, turns orthogonal like a schematic wire, and ends in
    a pin (open-circle terminal).
    """

    CENTER = [(96, 100), (80, 164), (118, 234), (216, 246)]
    W0 = 70.0  # shoulder width
    CAP = 18.0  # depth of the rounded shoulder behind the centreline start
    TC = 0.085  # where the calyx lets go of the body sides

    def frame(self, t):
        c = bez(self.CENTER, t)
        tx, ty = unit(bez_d(self.CENTER, t))
        return c, (tx, ty), (-ty, tx)  # point, tangent, left normal

    def width(self, t):
        return self.W0 * math.cos(math.pi / 2 * t**1.7) ** 0.8

    def side(self, sign, t0=0.0, t1=1.0, n=140):
        pts = []
        for i in range(n + 1):
            t = t0 + (t1 - t0) * i / n
            c, _, nrm = self.frame(t)
            pts.append(add(c, nrm, sign * self.width(t) / 2))
        return pts

    def cap(self, grow=0.0, n=48):
        c, tan, nrm = self.frame(0)
        w = self.width(0) / 2 * (1 + grow)
        pts = []
        for i in range(n + 1):
            phi = math.pi * i / n  # right -> back -> left
            p = add(c, nrm, -w * math.cos(phi))
            pts.append(add(p, tan, -(self.CAP + 21 * grow) * math.sin(phi)))
        return pts

    def outline(self):
        return self.cap()[1:-1] + self.side(+1) + self.side(-1)[::-1][1:]

    def calyx(self, n=120):
        c, tan, nrm = self.frame(0)
        w = self.width(0) / 2 * 1.08
        back = self.cap(grow=0.08)
        front = []
        for i in range(n + 1):
            u = 1 - 2 * i / n  # +1 left .. -1 right
            s = min((u + 1) * 1.5, 2.9999)
            tri = 1 - abs(2 * (s - math.floor(s)) - 1)  # sepal tips at u = -2/3, 0, 2/3
            d = 3 + 19 * tri**1.5 * (1 - 0.25 * abs(u))
            front.append(add(add(c, nrm, w * u), tan, d))
        return back + front

    def stem_top(self):
        c, tan, _ = self.frame(0)
        return add(c, tan, -(self.CAP + 2.5))

    def stem(self, n=40):
        s0 = self.stem_top()
        _, tan, nrm = self.frame(0)
        up = (-tan[0], -tan[1])
        ctrl = [s0, add(s0, up, 12), add(add(s0, up, 22), nrm, 5), add(add(s0, up, 25), nrm, 15)]
        return [bez(ctrl, i / n) for i in range(n + 1)]

    def lead(self):
        """Stem end -> up -> left to the pin. Returns (polyline, pin centre, pin r)."""
        e = self.stem()[-1]
        k = (e[0], e[1] - 14)
        pin = (k[0] - 32, k[1])
        r = 4.6
        return [e, k, (pin[0] + r, pin[1])], pin, r

    def traces(self):
        """Two copper runs on the lit side of the body; each ends in a via."""
        runs = []
        for t0, t1 in ((0.13, 0.55), (0.61, 0.73)):
            pts = []
            for i in range(61):
                t = t0 + (t1 - t0) * i / 60
                c, _, nrm = self.frame(t)
                pts.append(add(c, nrm, self.width(t) * 0.27))
            runs.append(pts)
        return runs

    def tip(self):
        return bez(self.CENTER, 1.0)


P = Pepper()


# ------------------------------------------------------- polyline helpers ---
def cumlen(pts):
    out = [0.0]
    for a, b in itertools.pairwise(pts):
        out.append(out[-1] + math.dist(a, b))
    return out


def point_at(pts, cum, s):
    s = max(0.0, min(s, cum[-1]))
    for i in range(1, len(cum)):
        if cum[i] >= s:
            seg = cum[i] - cum[i - 1] or 1.0
            k = (s - cum[i - 1]) / seg
            a, b = pts[i - 1], pts[i]
            return (a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k)
    return pts[-1]


def sub(pts, cum, s0, s1):
    s0, s1 = max(0.0, s0), min(cum[-1], s1)
    if s1 <= s0:
        return []
    out = [point_at(pts, cum, s0)]
    out += [p for p, c in zip(pts, cum, strict=False) if s0 < c < s1]
    out.append(point_at(pts, cum, s1))
    return out


class Route:
    """A polyline with arc-length lookup."""

    def __init__(self, pts):
        self.pts = pts
        self.cum = cumlen(pts)
        self.length = self.cum[-1]

    def sub(self, s0, s1):
        return sub(self.pts, self.cum, s0, s1)

    def at(self, s):
        return point_at(self.pts, self.cum, s)


# --------------------------------------------------------------- drawing ----
class Canvas:
    """Supersampled drawing surface; coordinates are given in output pixels."""

    def __init__(self, w, h, bg=PAPER, mode="RGB", k=SS, rot=0.0):
        self.k = k
        self.cs, self.sn = math.cos(rot), math.sin(rot)
        self.im = Image.new(mode, (w * k, h * k), bg)
        self.d = ImageDraw.Draw(self.im)

    def s(self, pts, off=(0, 0), sc=1.0):
        c, s, k = self.cs, self.sn, self.k
        return [
            (((x * c - y * s) * sc + off[0]) * k, ((x * s + y * c) * sc + off[1]) * k)
            for x, y in pts
        ]

    def poly(self, pts, fill, **kw):
        self.d.polygon(self.s(pts, **kw), fill=fill)

    def stroke(self, pts, fill, width, closed=False, caps=True, **kw):
        if len(pts) < 2:
            return
        sp = self.s(pts + pts[:2] if closed else pts, **kw)
        wpx = max(1, round(width * self.k * kw.get("sc", 1.0)))
        self.d.line(sp, fill=fill, width=wpx, joint="curve")
        if wpx >= 6:  # Pillow can leave hairline gaps at joints
            r = wpx / 2
            for x, y in sp[1:-1] if not caps else sp:
                self.d.ellipse([x - r, y - r, x + r, y + r], fill=fill)
        if caps and not closed:
            r = wpx / 2
            for x, y in (sp[0], sp[-1]):
                self.d.ellipse([x - r, y - r, x + r, y + r], fill=fill)

    def dot(self, c, r, fill=None, outline=None, width=0, off=(0, 0), sc=1.0):
        x, y = self.s([c], off=off, sc=sc)[0]
        rr = r * self.k * sc
        self.d.ellipse(
            [x - rr, y - rr, x + rr, y + rr],
            fill=fill,
            outline=outline,
            width=max(1, round(width * self.k * sc)) if outline else 0,
        )

    def rect_px(self, x0, y0, x1, y1, fill):
        """Pixel-exact filled rectangle, for crisp 1 px rules."""
        k = self.k
        self.d.rectangle([x0 * k, y0 * k, x1 * k - 1, y1 * k - 1], fill=fill)

    def text(self, xy, s, font, fill, anchor="ls"):
        x, y = xy
        self.d.text((x * self.k, y * self.k), s, font=font, fill=fill, anchor=anchor)


def draw_mark(cv, detail="splash", body=CHILI, fill_body=True, boost=1.0, **kw):
    """Draw the pepper mark.

    detail: 'splash' pin, lead, junction, vias   (the installer splash, SVG)
            'large'  traces and vias             (icons 96 px and up)
            'mid'    traces                      (icons 40-64 px)
            'small'  silhouette only             (icons up to 32 px)
    """
    c = (lambda rgb: (*rgb, 255)) if cv.im.mode == "RGBA" else (lambda rgb: rgb)
    runs = P.traces()
    if fill_body:
        cv.poly(P.outline(), c(body), **kw)
    if detail != "small":
        for run in runs:
            cv.stroke(run, c(TRACE), 2.1 * boost, **kw)
    if detail == "splash":
        cv.dot(runs[0][0], 2.3, fill=c(TRACE), **kw)
    if detail in ("splash", "large"):
        for run in runs:
            cv.dot(run[-1], 2.6, fill=c(body), outline=c(TRACE), width=1.3, **kw)
    ink_w = (2.0 if detail in ("splash", "large") else 2.2) * boost
    cv.stroke(P.outline(), c(INK), ink_w, closed=True, **kw)
    if detail == "splash":
        lead, pin, r = P.lead()
        cv.stroke(lead, c(INK), 1.8, **kw)
        cv.dot(pin, r, fill=c(PAPER), outline=c(INK), width=1.8, **kw)
    stem = P.stem()
    cv.stroke(stem, c(INK), 7.0 + ink_w * 1.6, **kw)
    cv.stroke(stem, c(STEM), 7.0, **kw)
    cal = P.calyx()
    cv.poly(cal, c(STEM), **kw)
    cv.stroke(cal, c(INK), ink_w * 0.9, closed=True, **kw)
    if detail == "splash":
        cv.dot(P.stem_top(), 3.1, fill=c(INK), **kw)


# ------------------------------------------------------------------ splash ---
def fonts():
    def f(w, s):
        return ImageFont.truetype(str(FONT_DIR / f"BarlowSemiCondensed-{w}.ttf"), s * SS)

    return {"name": f("SemiBold", 29), "row": f("Medium", 13), "rev": f("Regular", 13)}


FRAME = (8, 8, 452, 280)  # drawing border, inclusive-exclusive pixel box
TITLE = (262, 206, 452, 280)  # title block in the frame's lower-right corner
ROW = 245  # rule between name and status rows
COL = 372  # rule between status and revision cells


def splash_base(name, version):
    cv = Canvas(SPLASH_W, SPLASH_H)
    x0, y0, x1, y1 = FRAME
    tx0, ty0, tx1, ty1 = TITLE
    # dot grid inside the frame, clear of the title block
    for gx in range(x0 + 12, x1, 12):
        for gy in range(y0 + 12, y1, 12):
            if tx0 - 4 <= gx and ty0 - 4 <= gy:
                continue
            cv.dot((gx + 0.5, gy + 0.5), 0.8, fill=GRID)
    # window edge, drawing frame, title block rules
    cv.rect_px(0, 0, SPLASH_W, 1, EDGE)
    cv.rect_px(0, 0, 1, SPLASH_H, EDGE)
    cv.rect_px(SPLASH_W - 1, 0, SPLASH_W, SPLASH_H, EDGE)
    cv.rect_px(0, SPLASH_H - TRACK_H, SPLASH_W, SPLASH_H, TRACK)
    cv.rect_px(0, SPLASH_H - TRACK_H - 1, SPLASH_W, SPLASH_H - TRACK_H, EDGE)
    for a, b, c, d in (
        (x0, y0, x1, y0 + 1),
        (x0, y1 - 1, x1, y1),
        (x0, y0, x0 + 1, y1),
        (x1 - 1, y0, x1, y1),
        (tx0, ty0, tx1, ty0 + 1),
        (tx0, ty0, tx0 + 1, ty1),
        (tx0, ROW, tx1, ROW + 1),
        (COL, ROW, COL + 1, ty1),
    ):
        cv.rect_px(a, b, c, d, INK)
    cv.rect_px(tx0 + 1, ty0 + 1, tx1 - 1, ROW, PAPER)
    cv.rect_px(tx0 + 1, ROW + 1, tx1 - 1, ty1 - 1, PAPER)
    f = fonts()
    cv.text((tx0 + 11, ROW - 10), name, f["name"], INK)
    cv.text((tx0 + 11, ty1 - 12), "Installing", f["row"], INK)
    if version:
        cv.text((tx1 - 10, ty1 - 12), f"Rev {version}", f["rev"], INK_SOFT, anchor="rs")
    return cv


def ease(x):
    x = max(0.0, min(1.0, x))
    return x * x * (3 - 2 * x)


def window(u, a, b):
    """0 before a, ramps to 1 at b."""
    return ease((u - a) / (b - a)) if b > a else float(u >= a)


class Signal:
    def __init__(self):
        lead, _pin, _r = P.lead()
        stem = P.stem()
        self.a = Route([lead[2], *lead[1::-1], *stem[::-1][1:]])  # pin -> stem top
        self.left = Route(P.side(+1, P.TC, 1.0))
        self.right = Route(P.side(-1, P.TC, 1.0))
        self.runs = [Route(r) for r in P.traces()]


def pulse(glow_cv, core_cv, path, head, length=34.0, strength=1.0):
    """A travelling pulse: tapered signal-orange tail, hot head, tight halo."""
    if strength <= 0:
        return
    n = 12
    for i in range(n):
        s0 = head - length * (1 - i / n)
        s1 = head - length * (1 - (i + 1) / n)
        seg = path.sub(s0, s1)
        if len(seg) < 2:
            continue
        k = (i + 1) / n
        a = round(255 * strength * k**1.2)
        core_cv.stroke(seg, (*SIGNAL, a), 1.8 + 1.8 * k, caps=False)
        glow_cv.stroke(seg, (*GLOW, round(a * 0.5)), 6.0, caps=False)
    if 0 < head <= path.length + 2:
        core_cv.dot(path.at(head), 1.9, fill=(*HOT, round(255 * strength)))


def splash_frame(base, sig, u):
    """u in [0, 1): one loop. Rest state at both ends so the loop is seamless."""
    out = base.im.copy()
    glow = Canvas(SPLASH_W, SPLASH_H, bg=(0, 0, 0, 0), mode="RGBA")
    core = Canvas(SPLASH_W, SPLASH_H, bg=(0, 0, 0, 0), mode="RGBA")

    # 1) pin -> lead -> stem: 0.00-0.20, disappears under the calyx
    if u < 0.215:
        head = sig.a.length * ease(u / 0.20) + 6
        fade = window(u, 0.0, 0.03) * (1 - window(u, 0.19, 0.215))
        pulse(glow, core, sig.a, head, strength=fade)
    # junction dot catches the signal as it arrives
    j = window(u, 0.17, 0.21) * (1 - window(u, 0.23, 0.33))
    if j > 0:
        glow.dot(P.stem_top(), 5.5, fill=(*GLOW, round(150 * j)))
        core.dot(P.stem_top(), 2.2, fill=(*HOT, round(255 * j)))

    # 2) both sides of the body, calyx -> tip: 0.24-0.60
    prog = ease((u - 0.24) / 0.36) if u >= 0.24 else 0.0
    if 0.24 <= u < 0.64:
        fade = window(u, 0.24, 0.27) * (1 - window(u, 0.59, 0.64))
        for side in (sig.left, sig.right):
            pulse(glow, core, side, side.length * prog + 4, strength=fade)

    # 3) copper runs light up behind the left pulse, then cool off: 0.30-0.84
    cool = 1 - window(u, 0.64, 0.84)
    if u >= 0.24 and cool > 0:
        lit_t = P.TC + (1 - P.TC) * prog
        for (t0, t1), run in zip(((0.13, 0.55), (0.61, 0.73)), sig.runs, strict=False):
            f = max(0.0, min(1.0, (lit_t - t0) / (t1 - t0)))
            if f <= 0:
                continue
            seg = run.sub(0, run.length * f)
            core.stroke(seg, (*HOT, round(210 * cool)), 2.1)
            glow.stroke(seg, (*GLOW, round(110 * cool)), 5.0)
            if f >= 1:
                core.dot(run.pts[-1], 2.9, fill=(*HOT, round(235 * cool)))

    # 4) tip flash when the two pulses meet, body warms briefly: 0.57-0.86
    flash = window(u, 0.56, 0.60) * (1 - window(u, 0.61, 0.72))
    if flash > 0:
        glow.dot(P.tip(), 9, fill=(*GLOW, round(170 * flash)))
        core.dot(P.tip(), 2.6, fill=(*HOT, round(255 * flash)))
    warm = window(u, 0.57, 0.62) * (1 - window(u, 0.64, 0.88))

    if warm > 0:
        over = Canvas(SPLASH_W, SPLASH_H, bg=(0, 0, 0, 0), mode="RGBA")
        draw_mark(over, "splash", body=mix(CHILI, CHILI_HOT, 0.9 * warm))
        out = out.convert("RGBA")
        out.alpha_composite(over.im)
    g = glow.im.filter(ImageFilter.GaussianBlur(1.4 * SS))
    out = out.convert("RGBA")
    out.alpha_composite(g)
    out.alpha_composite(core.im)
    return out.convert("RGB").resize((SPLASH_W, SPLASH_H), Image.LANCZOS)


def render_splash(name, version, frames=90):
    base = splash_base(name, version)
    draw_mark(base, "splash")
    sig = Signal()
    return [splash_frame(base, sig, i / frames) for i in range(frames)]


def save_gif(frames, path, ms=40):
    # One palette for every frame: no colour shimmer between frames.
    sample = Image.new("RGB", (SPLASH_W, SPLASH_H * 4))
    for i, k in enumerate((0, 20, 45, 55)):
        sample.paste(frames[k * len(frames) // 90], (0, SPLASH_H * i))
    pal = sample.quantize(colors=255, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    q = [f.quantize(palette=pal, dither=Image.Dither.NONE) for f in frames]
    # Velopack plays every frame on one fixed timer (the average delay), but
    # Pillow merges identical consecutive frames into one longer frame, which
    # would collapse the quiet part of the loop. Keep them distinct: entry 255
    # duplicates the paper colour, and one margin pixel alternates between the
    # two indices, which is invisible.
    x, y = 203, 123  # plain paper inside the animated area: keeps deltas small
    paper_idx = q[0].getpixel((x, y))
    assert frames[0].getpixel((x, y)) == PAPER, "toggle pixel must sit on plain paper"
    entries = pal.getpalette()[:765]
    entries += entries[paper_idx * 3 : paper_idx * 3 + 3]
    for i, f in enumerate(q):
        f.putpalette(entries)
        f.putpixel((x, y), 255 if i % 2 else paper_idx)
    q[0].save(
        path, save_all=True, append_images=q[1:], duration=ms, loop=0, optimize=False, disposal=1
    )


# -------------------------------------------------------------------- icon ---
ICON_TILT = math.radians(-16)  # lean the pepper so it fills a square icon


def mark_bbox(with_pin, rot=0.0):
    pts = P.outline() + P.calyx() + P.stem()
    if with_pin:
        lead, pin, r = P.lead()
        pts += [*lead, (pin[0] - r - 1, pin[1] - r - 1), (pin[0] + r, pin[1] + r)]
    c, s = math.cos(rot), math.sin(rot)
    pts = [(x * c - y * s, x * s + y * c) for x, y in pts]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def icon_image(size):
    """Silhouette-first icon. The pin and lead stay in the splash and SVG mark:
    as a thin navy line they vanish on dark taskbars and leave a floating dot."""
    detail = "large" if size >= 96 else "mid" if size >= 40 else "small"
    x0, y0, x1, y1 = mark_bbox(False, ICON_TILT)
    pad = size * (0.05 if size >= 40 else 0.02)
    sc = (size - 2 * pad) / max(x1 - x0, y1 - y0)
    off = (
        pad - x0 * sc + ((size - 2 * pad) - (x1 - x0) * sc) / 2,
        pad - y0 * sc + ((size - 2 * pad) - (y1 - y0) * sc) / 2,
    )
    k = 8 if size <= 64 else 4
    cv = Canvas(size, size, bg=(0, 0, 0, 0), mode="RGBA", k=k, rot=ICON_TILT)
    boost = max(1.0, 0.9 / sc) if detail != "large" else 1.0
    draw_mark(cv, detail, boost=boost, off=off, sc=sc)
    return cv.im.resize((size, size), Image.LANCZOS)


def save_ico(path, sizes=(16, 20, 24, 32, 40, 48, 64, 96, 128, 256)):
    """Multi-image ICO with PNG entries, each drawn at its own size."""
    blobs = []
    for s in sizes:
        buf = io.BytesIO()
        icon_image(s).save(buf, "PNG")
        blobs.append((s, buf.getvalue()))
    head = struct.pack("<HHH", 0, 1, len(blobs))
    offset = 6 + 16 * len(blobs)
    entries, data = b"", b""
    for s, png in blobs:
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
        data += png
    Path(path).write_bytes(head + entries + data)


# --------------------------------------------------------------------- svg ---
def svg_path(pts, closed=False):
    d = "M" + " L".join(f"{x:.1f} {y:.1f}" for x, y in pts)
    return d + (" Z" if closed else "")


def save_svg(path):
    x0, y0, x1, y1 = mark_bbox(True)
    pad = 6
    vb = f"{x0 - pad:.1f} {y0 - pad:.1f} {x1 - x0 + 2 * pad:.1f} {y1 - y0 + 2 * pad:.1f}"
    lead, pin, r = P.lead()
    runs = P.traces()
    stem = svg_path(P.stem())
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}" role="img" aria-label="Pepper mark">',
        f'<path d="{svg_path(P.outline(), True)}" fill="{hexc(CHILI)}"/>',
        *[
            f'<path d="{svg_path(run)}" fill="none" stroke="{hexc(TRACE)}" stroke-width="2.1" '
            f'stroke-linecap="round" stroke-linejoin="round"/>'
            for run in runs
        ],
        f'<circle cx="{runs[0][0][0]:.1f}" cy="{runs[0][0][1]:.1f}" r="2.3" fill="{hexc(TRACE)}"/>',
        *[
            f'<circle cx="{run[-1][0]:.1f}" cy="{run[-1][1]:.1f}" r="2.6" fill="{hexc(CHILI)}" '
            f'stroke="{hexc(TRACE)}" stroke-width="1.3"/>'
            for run in runs
        ],
        f'<path d="{svg_path(P.outline(), True)}" fill="none" stroke="{hexc(INK)}" stroke-width="2" '
        f'stroke-linejoin="round"/>',
        f'<path d="{svg_path(lead)}" fill="none" stroke="{hexc(INK)}" stroke-width="1.8" '
        f'stroke-linejoin="round"/>',
        f'<circle cx="{pin[0]:.1f}" cy="{pin[1]:.1f}" r="{r}" fill="{hexc(PAPER)}" '
        f'stroke="{hexc(INK)}" stroke-width="1.8"/>',
        f'<path d="{stem}" fill="none" stroke="{hexc(INK)}" stroke-width="10.2" stroke-linecap="round"/>',
        f'<path d="{stem}" fill="none" stroke="{hexc(STEM)}" stroke-width="7" stroke-linecap="round"/>',
        f'<path d="{svg_path(P.calyx(), True)}" fill="{hexc(STEM)}" stroke="{hexc(INK)}" '
        f'stroke-width="1.8" stroke-linejoin="round"/>',
        f'<circle cx="{P.stem_top()[0]:.1f}" cy="{P.stem_top()[1]:.1f}" r="3.1" fill="{hexc(INK)}"/>',
        "</svg>",
    ]
    Path(path).write_text("\n".join(parts) + "\n")


# -------------------------------------------------------------------- main ---
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--name", default="Spice Maker")
    ap.add_argument("--version", default="", help="shown as Rev in the title block")
    ap.add_argument("--out", default="assets")
    ap.add_argument("--frames", type=int, default=90, help="loop length at 40 ms/frame")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    save_gif(render_splash(a.name, a.version, a.frames), out / "pepper-splash.gif")
    save_svg(out / "pepper-mark.svg")
    save_ico(out / "pepper.ico")
    print(f"wrote {out / 'pepper-splash.gif'}, {out / 'pepper-mark.svg'}, {out / 'pepper.ico'}")


if __name__ == "__main__":
    main()
