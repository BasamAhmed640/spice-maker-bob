"""Render an LTspice ``.asy`` symbol to PNG, so a generated symbol can be looked at.

    python tools/render_symbol.py MODEL.asy [MORE.asy ...] --out symbols.png

Draws the body (RECTANGLE/LINE/CIRCLE), each PIN as a small square at its connection
point, and each pin's name where LTspice puts it: ``LEFT n`` left-aligned n units right
of the pin, ``RIGHT n`` right-aligned n units to its left, ``TOP n`` centred n units below
it, ``BOTTOM n`` centred n units above it, ``NONE`` hidden. Several symbols are laid out
side by side with their file names underneath. This is a review aid, not LTspice: it
checks that labels are visible, placed on the right side and do not collide.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SCALE = 1.6  # pixels per LTspice unit
MARGIN = 120  # units around each symbol
CHAR_UNITS = 16  # the width budget the generator assumes per label character


def _font(size: int) -> ImageFont.ImageFont:
    for name in ("consola.ttf", "cour.ttf", "DejaVuSansMono.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def parse(text: str) -> dict:
    shapes, pins, attrs, windows = [], [], {}, {}
    pin = None
    for raw in text.splitlines():
        parts = raw.split()
        if not parts:
            continue
        head = parts[0]
        if head in ("LINE", "RECTANGLE", "CIRCLE"):
            shapes.append((head, [int(v) for v in parts[2:6]]))
        elif head == "PIN":
            pin = {"x": int(parts[1]), "y": int(parts[2]), "just": parts[3], "off": int(parts[4])}
            pins.append(pin)
        elif head == "PINATTR" and pin is not None:
            pin[parts[1]] = " ".join(parts[2:])
        elif head == "SYMATTR":
            attrs[parts[1]] = " ".join(parts[2:])
        elif head == "WINDOW":
            windows[parts[1]] = (int(parts[2]), int(parts[3]), parts[4])
    return {"shapes": shapes, "pins": pins, "attrs": attrs, "windows": windows}


def _bounds(symbol: dict) -> tuple[int, int, int, int]:
    xs, ys = [], []
    for _kind, (x1, y1, x2, y2) in symbol["shapes"]:
        xs += [x1, x2]
        ys += [y1, y2]
    for pin in symbol["pins"]:
        xs.append(pin["x"])
        ys.append(pin["y"])
    for x, y, _j in symbol["windows"].values():
        xs.append(x)
        ys.append(y)
    return min(xs) - MARGIN, min(ys) - MARGIN, max(xs) + MARGIN, max(ys) + MARGIN


def render(paths: list[Path], out: Path) -> Path:
    symbols = [(path, parse(path.read_text(encoding="utf-8", errors="replace"))) for path in paths]
    boxes = [_bounds(symbol) for _path, symbol in symbols]
    width = sum(int((b[2] - b[0]) * SCALE) for b in boxes)
    height = max(int((b[3] - b[1]) * SCALE) for b in boxes) + 40
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    font = _font(int(CHAR_UNITS * SCALE / 0.55))
    small = _font(18)
    x_offset = 0
    for (path, symbol), (left, top, right, bottom) in zip(symbols, boxes, strict=True):

        def px(x: float, y: float, left=left, top=top, x_offset=x_offset) -> tuple[int, int]:
            return int(x_offset + (x - left) * SCALE), int((y - top) * SCALE)

        for kind, (x1, y1, x2, y2) in symbol["shapes"]:
            if kind == "RECTANGLE":
                draw.rectangle([px(x1, y1), px(x2, y2)], outline="navy", width=3)
            elif kind == "CIRCLE":
                draw.ellipse([px(x1, y1), px(x2, y2)], outline="navy", width=3)
            else:
                draw.line([px(x1, y1), px(x2, y2)], fill="navy", width=3)
        for pin in symbol["pins"]:
            cx, cy = px(pin["x"], pin["y"])
            draw.rectangle([cx - 5, cy - 5, cx + 5, cy + 5], outline="red", width=2)
            name, just, off = pin.get("PinName", "?"), pin["just"], pin["off"]
            if just == "NONE":
                continue
            if just == "LEFT":
                anchor, at = "lm", px(pin["x"] + off, pin["y"])
            elif just == "RIGHT":
                anchor, at = "rm", px(pin["x"] - off, pin["y"])
            elif just == "TOP":
                anchor, at = "mt", px(pin["x"], pin["y"] + off)
            else:  # BOTTOM
                anchor, at = "mb", px(pin["x"], pin["y"] - off)
            draw.text(at, name, fill="black", font=font, anchor=anchor)
            order = pin.get("SpiceOrder", "")
            draw.text((cx + 8, cy - 22), order, fill="grey", font=small)
        for key, label in (("0", "U1"), ("3", symbol["attrs"].get("Value", ""))):
            if key in symbol["windows"]:
                x, y, _j = symbol["windows"][key]
                draw.text(px(x, y), label, fill="darkgreen", font=font, anchor="mm")
        draw.text((x_offset + 10, height - 30), path.name, fill="grey", font=small)
        x_offset += int((right - left) * SCALE)
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("symbols", nargs="+", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    print(render(args.symbols, args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
