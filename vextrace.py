#!/usr/bin/env python3
"""VexTrace - convert raster images (PNG/JPG/BMP/WebP/GIF/TIFF) into clean SVG.

Two tracing backends:
  * vtracer (pip)            - colour and black & white tracing
  * potrace (system package) - crisp 1-bit tracing for logos and line art

Run with no arguments for the PyQt6 GUI, or pass an image path for CLI mode.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps

APP_NAME = "VexTrace"
APP_VERSION = "1.0.0"

SVG_NS = "http://www.w3.org/2000/svg"
_DOCTYPE_RE = re.compile(r"<!DOCTYPE[^>]*>", re.IGNORECASE)
_UNIT_RE = re.compile(r"[-+0-9.eE]+")

# Tracing time and output size grow with pixel count, and a 12-megapixel phone
# photo can take minutes. Past this size extra pixels only add noise paths.
MAX_SIDE = 1600

SUPPORTED_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif", ".tif", ".tiff"}

MODE_COLOR = "color"
MODE_BW = "bw"
MODE_POTRACE = "potrace"
MODE_SILHOUETTE = "silhouette"
MODE_LINES = "lines"
MODES = (MODE_COLOR, MODE_BW, MODE_POTRACE, MODE_SILHOUETTE, MODE_LINES)

# "&&" is how Qt shows a literal "&" in button text.
MODE_LABELS = {
    MODE_COLOR: "Color",
    MODE_BW: "B&&W",
    MODE_POTRACE: "Potrace",
    MODE_SILHOUETTE: "Silhouette",
    MODE_LINES: "Lines",
}


# --------------------------------------------------------------------------------------
# Settings and presets
# --------------------------------------------------------------------------------------


@dataclass
class Settings:
    """Everything that affects the traced result."""

    mode: str = MODE_COLOR

    # vtracer knobs
    filter_speckle: int = 4        # discard patches smaller than N pixels
    color_precision: int = 6       # bits of colour kept per channel (1-8)
    layer_difference: int = 16     # colour gap between stacked layers
    corner_threshold: int = 60     # angle (deg) below which a corner stays sharp
    path_precision: int = 8        # decimal places kept in path data

    # potrace / thresholding
    bw_threshold: int = 128        # luminance split point for 1-bit modes

    # lines mode
    line_detail: int = 75          # 0-100: higher keeps fainter edges
    line_weight: int = 2           # 1-6: thickness of the traced lines

    # pre/post processing
    remove_bg: bool = False        # make the dominant edge colour transparent
    bg_tolerance: int = 24         # how close a pixel must be to count as background
                                   # (also used to find the subject in silhouette mode)
    square: bool = False           # force a 1:1 viewBox
    padding: float = 0.05          # padding as a fraction of the square side
    upscale: bool = False          # enlarge small images before tracing
    max_side: int = MAX_SIDE       # shrink larger images before tracing (0 = never)

    def colormode(self) -> str:
        """vtracer's colormode for this mode."""
        return "color" if self.mode == MODE_COLOR else "binary"


PRESETS: dict[str, dict[str, int]] = {
    # Flat art wants aggressive speckle removal and wide colour layers so that
    # each flat region collapses into a single path.
    "Logo / Flat art": {
        "filter_speckle": 8, "color_precision": 6, "layer_difference": 32,
        "corner_threshold": 60, "path_precision": 8,
    },
    # A middle ground: keep more colour steps but still clean up noise.
    "Illustration": {
        "filter_speckle": 4, "color_precision": 7, "layer_difference": 16,
        "corner_threshold": 60, "path_precision": 8,
    },
    # Photos need many thin layers; the result is a posterised vector.
    "Photo (posterized)": {
        "filter_speckle": 2, "color_precision": 8, "layer_difference": 8,
        "corner_threshold": 80, "path_precision": 6,
    },
}

PRESET_ALIASES = {
    "logo": "Logo / Flat art",
    "flat": "Logo / Flat art",
    "illustration": "Illustration",
    "illo": "Illustration",
    "photo": "Photo (posterized)",
}

SLIDER_HELP = {
    "filter_speckle": "Discard traced patches smaller than this many pixels. "
                      "Raise it to clean up noise and JPEG artefacts.",
    "color_precision": "How many bits of colour detail to keep per channel. "
                       "Lower values give flatter, poster-like output.",
    "layer_difference": "Minimum colour gap between stacked layers. "
                        "Higher values merge similar shades into one path.",
    "corner_threshold": "Angles sharper than this stay as corners; "
                        "anything blunter is smoothed into a curve.",
    "path_precision": "Decimal places kept in the path data. "
                      "Lower values give smaller files with slightly coarser curves.",
    "bw_threshold": "Luminance split point for 1-bit tracing. "
                    "Pixels darker than this become ink.",
    "bg_tolerance": "How far a colour can stray from the background and still count as "
                    "background. Raise it if the silhouette picks up parts of the backdrop.",
    "line_detail": "How faint an edge can be and still become a line. "
                   "Higher values draw more lines.",
    "line_weight": "How thick the traced lines are.",
    "padding": "Empty margin around the artwork when a square viewBox is forced, "
               "as a percentage of the square's side.",
}


def apply_preset(settings: Settings, name: str) -> Settings:
    """Return a copy of *settings* with the named preset's values applied."""
    key = PRESET_ALIASES.get(name.strip().lower(), name)
    if key not in PRESETS:
        raise KeyError(name)
    return replace(settings, **PRESETS[key])


# --------------------------------------------------------------------------------------
# Image loading and pre-processing
# --------------------------------------------------------------------------------------


def load_image(src: str | os.PathLike[str] | Image.Image) -> Image.Image:
    if isinstance(src, Image.Image):
        return src.convert("RGBA")
    with Image.open(src) as img:
        # Phone photos are often stored sideways with an EXIF rotation flag.
        return ImageOps.exif_transpose(img).convert("RGBA")


def _dominant_edge_color(img: Image.Image) -> tuple[int, int, int, int]:
    """The most common colour along the outer border of the image."""
    w, h = img.size
    px = img.load()
    edge: Counter[tuple[int, int, int, int]] = Counter()
    for x in range(w):
        edge[px[x, 0]] += 1
        edge[px[x, h - 1]] += 1
    for y in range(h):
        edge[px[0, y]] += 1
        edge[px[w - 1, y]] += 1
    return edge.most_common(1)[0][0]


def remove_background(img: Image.Image, tolerance: int) -> Image.Image:
    """Make every pixel close to the dominant edge colour fully transparent.

    vtracer honours the alpha channel, so a transparent background simply
    produces no background path at all.
    """
    bg = _dominant_edge_color(img)
    if bg[3] == 0:  # already transparent
        return img

    # A pixel is background when every channel is within *tolerance* of bg.
    diff = ImageChops.difference(img.convert("RGB"), Image.new("RGB", img.size, bg[:3]))
    r, g, b = diff.split()
    furthest = ImageChops.lighter(ImageChops.lighter(r, g), b)
    tol = max(0, tolerance)
    is_bg = furthest.point(lambda d: 255 if d <= tol else 0)
    out = img.copy()
    out.putalpha(ImageChops.subtract(img.getchannel("A"), is_bg))
    return out


def upscale_small(img: Image.Image, min_side: int = 512) -> Image.Image:
    """Enlarge images whose longest edge is below *min_side*.

    Tracing works on pixel edges, so a tiny source produces chunky, stair-stepped
    paths. Resampling first gives the tracer smoother edges to follow.
    """
    longest = max(img.size)
    if longest >= min_side:
        return img
    factor = min_side / longest
    size = (max(1, round(img.width * factor)), max(1, round(img.height * factor)))
    return img.resize(size, Image.Resampling.LANCZOS)


def downscale_large(img: Image.Image, max_side: int) -> Image.Image:
    """Shrink images whose longest edge is above *max_side* (0 means never)."""
    if max_side <= 0 or max(img.size) <= max_side:
        return img
    out = img.copy()
    out.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    return out


def preprocess(img: Image.Image, s: Settings) -> Image.Image:
    img = downscale_large(img, s.max_side)
    if s.upscale:
        img = upscale_small(img)
    if s.remove_bg:
        img = remove_background(img, s.bg_tolerance)
    return img


def _ink(mask: Image.Image) -> Image.Image:
    """Black where *mask* is set, white elsewhere: ready for 1-bit tracing."""
    out = Image.new("RGBA", mask.size, (255, 255, 255, 255))
    out.paste((0, 0, 0, 255), mask=mask)
    return out


def silhouette(img: Image.Image, tolerance: int) -> Image.Image:
    """The subject as one solid black shape on white.

    The background is everything that matches the most common edge colour (or
    is transparent) and connects to the border, so holes inside the subject are
    filled in. This works best on a plain backdrop.
    """
    work = img.copy()
    work.thumbnail((800, 800), Image.Resampling.LANCZOS)  # the fill is slow pixel by pixel
    bg = _dominant_edge_color(work)
    if bg[3] == 0:
        is_bg = work.getchannel("A").point(lambda a: 255 if a < 128 else 0)
    else:
        diff = ImageChops.difference(work.convert("RGB"), Image.new("RGB", work.size, bg[:3]))
        r, g, b = diff.split()
        furthest = ImageChops.lighter(ImageChops.lighter(r, g), b)
        tol = max(0, tolerance)
        is_bg = furthest.point(lambda d: 255 if d <= tol else 0)

    # A one-pixel background border lets a single fill reach every edge.
    padded = Image.new("L", (work.width + 2, work.height + 2), 255)
    padded.paste(is_bg, (1, 1))
    ImageDraw.floodfill(padded, (0, 0), 128)
    subject = padded.crop((1, 1, work.width + 1, work.height + 1))
    subject = subject.point(lambda v: 0 if v == 128 else 255)
    if subject.size != img.size:
        subject = subject.resize(img.size, Image.Resampling.LANCZOS)
        subject = subject.point(lambda v: 255 if v >= 128 else 0)
    return _ink(subject)


def line_drawing(img: Image.Image, detail: int, weight: int) -> Image.Image:
    """The image's edges as black lines on white."""
    flat = Image.new("RGB", img.size, (255, 255, 255))
    flat.paste(img, mask=img.getchannel("A"))
    # Edges are found per colour channel, so a change of hue at equal
    # brightness still counts.
    r, g, b = flat.filter(ImageFilter.GaussianBlur(1.0)).filter(ImageFilter.FIND_EDGES).split()
    edges = ImageChops.lighter(ImageChops.lighter(r, g), b)
    edges = ImageOps.expand(ImageOps.crop(edges, 2), 2, 0)  # the filter marks the border
    cutoff = round(4 + (100 - max(0, min(100, detail))) * 0.6)
    lines = edges.point(lambda v: 255 if v > cutoff else 0)
    weight = max(1, min(6, int(weight)))
    if weight > 1:
        lines = lines.filter(ImageFilter.MaxFilter(2 * weight - 1))
    # Soften and re-cut the mask so the tracer follows smooth edges, not pixel steps.
    lines = lines.filter(ImageFilter.GaussianBlur(1.2)).point(lambda v: 255 if v >= 100 else 0)
    return _ink(lines)


# --------------------------------------------------------------------------------------
# Tracing backends
# --------------------------------------------------------------------------------------


class TraceError(RuntimeError):
    """Raised when a backend is unavailable or fails."""


def _import_vtracer():
    try:
        import vtracer
    except ImportError as exc:
        raise TraceError(
            "The 'vtracer' module is not installed. Install it with: pip install vtracer"
        ) from exc
    return vtracer


def _trace_vtracer(src_png: Path, out_svg: Path, s: Settings) -> None:
    """Trace with vtracer.

    IMPORTANT: every argument is passed POSITIONALLY on purpose.

    vtracer 0.6.15's cp314 wheel segfaults the interpreter the moment any
    keyword argument is used (a PyO3 keyword-handling bug on Python 3.14) --
    even `colormode=None`. Positional calls are unaffected. Do not "tidy" this
    call into keyword form; it will crash the whole process, not raise.

    Positional order, from the module's own signature:
        image_path, out_path, colormode, hierarchical, mode, filter_speckle,
        color_precision, layer_difference, corner_threshold, length_threshold,
        max_iterations, splice_threshold, path_precision
    """
    vtracer = _import_vtracer()
    vtracer.convert_image_to_svg_py(
        str(src_png),
        str(out_svg),
        s.colormode(),            # colormode: "color" | "binary"
        "stacked",                # hierarchical
        "spline",                 # mode: curve fitting
        int(s.filter_speckle),    # filter_speckle
        int(s.color_precision),   # color_precision
        int(s.layer_difference),  # layer_difference
        int(s.corner_threshold),  # corner_threshold
        4.0,                      # length_threshold
        10,                       # max_iterations
        45,                       # splice_threshold
        int(s.path_precision),    # path_precision
    )


def potrace_available() -> bool:
    return shutil.which("potrace") is not None


def to_bilevel(img: Image.Image, threshold: int) -> Image.Image:
    """Flatten onto white, then split into 1-bit ink/paper at *threshold*."""
    flat = Image.new("RGB", img.size, (255, 255, 255))
    flat.paste(img, mask=img.split()[3])
    grey = flat.convert("L")
    t = max(0, min(255, int(threshold)))
    return grey.point(lambda p: 255 if p > t else 0, mode="1")


def _trace_potrace(img: Image.Image, work: Path, out_svg: Path, s: Settings) -> None:
    """Trace with the potrace binary.

    The GUI sliders are shared across backends, so they are mapped onto
    potrace's equivalents:
        filter_speckle   -> --turdsize      (drop specks below N pixels)
        corner_threshold -> --alphamax      (0 = all corners, 1.334 = all curves)
        path_precision   -> --opttolerance  (curve-fitting slack)
    """
    if not potrace_available():
        raise TraceError(
            "potrace is not installed.\n"
            "Install it with your package manager, e.g. sudo pacman -S potrace.\n"
            "Or switch the mode to Color / Black & White, which use vtracer."
        )

    pbm = work / "input.pbm"
    to_bilevel(img, s.bw_threshold).save(pbm)

    # corner_threshold 60 (the default) maps to potrace's own default of 1.0.
    alphamax = max(0.0, min(1.334, s.corner_threshold / 60.0))
    opttolerance = max(0.01, (10 - int(s.path_precision)) * 0.05)

    cmd = [
        "potrace", "--svg",
        "--turdsize", str(int(s.filter_speckle)),
        "--alphamax", f"{alphamax:.4f}",
        "--opttolerance", f"{opttolerance:.4f}",
        "--output", str(out_svg),
        str(pbm),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise TraceError(f"potrace failed: {proc.stderr.strip() or proc.returncode}")


# --------------------------------------------------------------------------------------
# SVG post-processing
# --------------------------------------------------------------------------------------


def _num(value: float) -> str:
    """Format a number without a trailing '.0'."""
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def _parse_length(value: str | None) -> float | None:
    if not value:
        return None
    match = _UNIT_RE.search(value)
    if not match:
        return None
    try:
        return float(match.group())
    except ValueError:
        return None


def _content_box(root: ET.Element) -> tuple[float, float, float, float]:
    """The drawing's box, from viewBox if present, otherwise width/height."""
    viewbox = root.get("viewBox")
    if viewbox:
        parts = [p for p in re.split(r"[\s,]+", viewbox.strip()) if p]
        if len(parts) == 4:
            try:
                x, y, w, h = (float(p) for p in parts)
                if w > 0 and h > 0:
                    return x, y, w, h
            except ValueError:
                pass
    w = _parse_length(root.get("width")) or 0.0
    h = _parse_length(root.get("height")) or 0.0
    if w <= 0 or h <= 0:
        raise TraceError("The tracer returned an SVG with no usable size.")
    return 0.0, 0.0, w, h


def postprocess_svg(raw: bytes, s: Settings, scale: float = 1.0) -> bytes:
    """Give the SVG a proper viewBox, and optionally square it off with padding.

    vtracer emits width/height but no viewBox, which makes the result scale
    badly in viewers. Adding one costs nothing and makes the file behave.
    *scale* sets width/height relative to the traced size, so an image that was
    resized before tracing still comes out at its original dimensions.
    """
    text = _DOCTYPE_RE.sub("", raw.decode("utf-8", "replace"))
    ET.register_namespace("", SVG_NS)
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise TraceError(f"The tracer returned invalid SVG: {exc}") from exc

    x, y, w, h = _content_box(root)

    if s.square:
        side = max(w, h)
        pad = side * max(0.0, s.padding)
        total = side + 2 * pad
        # Centre the artwork inside the square, then shift it past the padding.
        dx = pad + (side - w) / 2 - x
        dy = pad + (side - h) / 2 - y
        group = ET.Element(f"{{{SVG_NS}}}g")
        group.set("transform", f"translate({_num(dx)},{_num(dy)})")
        for child in list(root):
            root.remove(child)
            group.append(child)
        root.append(group)
        box = (0.0, 0.0, total, total)
    else:
        box = (x, y, w, h)

    root.set("viewBox", " ".join(_num(v) for v in box))
    root.set("width", _num(box[2] * scale))
    root.set("height", _num(box[3] * scale))

    body = ET.tostring(root, encoding="unicode")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n").encode("utf-8")


def count_paths(svg: bytes) -> int:
    """Number of drawable path elements in the SVG."""
    try:
        root = ET.fromstring(_DOCTYPE_RE.sub("", svg.decode("utf-8", "replace")))
    except ET.ParseError:
        return 0
    return sum(1 for _ in root.iter(f"{{{SVG_NS}}}path"))


def human_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes / (1024 * 1024):.1f} MB"


# --------------------------------------------------------------------------------------
# The one call that does everything
# --------------------------------------------------------------------------------------


def vectorize(src: str | os.PathLike[str] | Image.Image, s: Settings) -> bytes:
    """Trace *src* with *s* and return finished SVG bytes."""
    original = load_image(src)
    img = preprocess(original, s)
    scale = max(original.size) / max(img.size)

    with tempfile.TemporaryDirectory(prefix="vextrace-") as tmp:
        work = Path(tmp)
        out_svg = work / "out.svg"

        if s.mode == MODE_POTRACE:
            _trace_potrace(img, work, out_svg, s)
        else:
            if s.mode == MODE_SILHOUETTE:
                img = silhouette(img, s.bg_tolerance)
            elif s.mode == MODE_LINES:
                img = line_drawing(img, s.line_detail, s.line_weight)
            # vtracer's reliable entry point is file-to-file, so the
            # pre-processed image goes to disk first.
            src_png = work / "input.png"
            img.save(src_png)
            _trace_vtracer(src_png, out_svg, s)

        if not out_svg.exists():
            raise TraceError("The tracer produced no output.")
        raw = out_svg.read_bytes()

    return postprocess_svg(raw, s, scale)


def default_output_for(path: Path) -> Path:
    return path.with_suffix(".svg")


def iter_images(folder: Path):
    for entry in sorted(folder.iterdir()):
        if entry.is_file() and entry.suffix.lower() in SUPPORTED_SUFFIXES:
            yield entry


def unique_target(out_dir: Path, stem: str, used: set[Path]) -> Path:
    """An unused '<stem>.svg' inside *out_dir*.

    A folder may hold logo.png and logo.webp; without this they would both want
    logo.svg and the second would silently clobber the first.
    """
    target = out_dir / f"{stem}.svg"
    counter = 2
    while target in used:
        target = out_dir / f"{stem}-{counter}.svg"
        counter += 1
    used.add(target)
    return target


# --------------------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------------------

# Design tokens. Every colour the interface uses comes from one of these two
# tables, so light and dark stay in step. Values are opaque on purpose: Qt style
# sheets and QColor both parse plain #RRGGBB reliably.
THEME_LIGHT = {
    "window": "#f5f5f7", "panel": "#ffffff", "card": "#ffffff",
    "card_border": "#e3e3e8", "separator": "#e5e5ea",
    "text": "#1d1d1f", "secondary": "#6e6e73", "tertiary": "#a1a1a6",
    "accent": "#0071e3", "accent_hover": "#0077ed", "accent_pressed": "#0062c4",
    "on_accent": "#ffffff",
    "control": "#ececf0", "control_hover": "#e2e2e7", "control_pressed": "#d8d8dd",
    "segment_on": "#ffffff", "segment_border": "#d9d9de",
    "groove": "#dcdce1", "handle": "#ffffff", "handle_border": "#c7c7cc",
    "switch_off": "#d6d6db", "knob": "#ffffff",
    "checker_a": "#ffffff", "checker_b": "#f1f1f4",
    "drop_border": "#c7c7cc", "drop_active": "#eef5ff",
    "danger": "#d70015", "danger_bg": "#fff1f0",
    "scrollbar": "#c7c7cc", "frame": "#c7c7cc",
}

THEME_DARK = {
    "window": "#161617", "panel": "#1e1e20", "card": "#232325",
    "card_border": "#333336", "separator": "#2c2c2e",
    "text": "#f5f5f7", "secondary": "#a1a1a6", "tertiary": "#6e6e73",
    "accent": "#0a84ff", "accent_hover": "#3396ff", "accent_pressed": "#0a72dc",
    "on_accent": "#ffffff",
    "control": "#2c2c2e", "control_hover": "#363638", "control_pressed": "#404042",
    "segment_on": "#5a5a5e", "segment_border": "#5a5a5e",
    "groove": "#3a3a3c", "handle": "#f5f5f7", "handle_border": "#f5f5f7",
    "switch_off": "#3a3a3c", "knob": "#ffffff",
    "checker_a": "#28282a", "checker_b": "#2f2f32",
    "drop_border": "#48484a", "drop_active": "#10233a",
    "danger": "#ff453a", "danger_bg": "#3a1d1d",
    "scrollbar": "#48484a", "frame": "#48484a",
}

STYLESHEET = """
QWidget { color: $text; }
QMainWindow, #root { background: $window; }

QToolTip {
    background: $card; color: $text; border: 1px solid $card_border;
    border-radius: 6px; padding: 6px 8px;
}

#titleBar { background: $panel; border-bottom: 1px solid $separator; }
#titleText { font-size: 13px; font-weight: 600; }
#titleFile { color: $secondary; font-size: 13px; }
#workspace, #home { background: $window; }

#homeTitle { font-size: 30px; font-weight: 600; }
#homeSubtitle { color: $secondary; font-size: 15px; }
#homeHeading { font-size: 15px; font-weight: 600; }
#tileTitle { font-size: 14px; font-weight: 600; }
#shortcut { color: $tertiary; font-size: 12px; }

QPushButton#tile {
    background: $card; border: 1px solid $card_border; border-radius: 12px;
    padding: 0; text-align: left;
}
QPushButton#tile:hover { border-color: $accent; background: $card; }
QPushButton#tile:pressed { background: $control; }
QPushButton#sample {
    background: transparent; border: 1px solid transparent; border-radius: 12px; padding: 0;
}
QPushButton#sample:hover { background: $card; border-color: $card_border; }
QPushButton#recentRow {
    background: transparent; border-radius: 8px; padding: 0; text-align: left;
}
QPushButton#recentRow:hover { background: $control; }
QPushButton#ghost { background: transparent; padding: 6px 10px; }
QPushButton#ghost:hover { background: $control_hover; }
QToolButton#titleMenu { background: transparent; }
QToolButton#titleMenu:hover { background: $control_hover; }

#sidebar { background: $panel; border-left: 1px solid $separator; }
#sidebarFooter { background: $panel; border-top: 1px solid $separator; }
#sectionTitle { font-size: 13px; font-weight: 600; }
#rowLabel { font-size: 13px; }
#rowValue { color: $secondary; font-size: 13px; }
#caption { color: $secondary; font-size: 12px; }
#paneTitle { font-size: 13px; font-weight: 600; }

#notice {
    background: $danger_bg; color: $danger; border-radius: 8px;
    padding: 8px 10px; font-size: 12px;
}

QPushButton, QToolButton {
    background: $control; border: none; border-radius: 8px;
    padding: 6px 14px; font-weight: 500; min-height: 18px;
}
QPushButton:hover, QToolButton:hover { background: $control_hover; }
QPushButton:pressed, QToolButton:pressed { background: $control_pressed; }
QPushButton:disabled { color: $tertiary; }
QToolButton { padding: 6px 10px; }
QToolButton::menu-indicator { image: none; width: 0; }

QPushButton#primary { background: $accent; color: $on_accent; }
QPushButton#primary:hover { background: $accent_hover; }
QPushButton#primary:pressed { background: $accent_pressed; }
QPushButton#primary:disabled { background: $control; color: $tertiary; }

QPushButton#link {
    background: transparent; color: $accent; padding: 0; font-weight: 400;
}
QPushButton#link:hover { text-decoration: underline; }

#segmented { background: $control; border-radius: 9px; }
#segmented QPushButton {
    background: transparent; border: 1px solid transparent; border-radius: 7px;
    padding: 4px 0; min-height: 18px; font-weight: 500;
}
#segmented QPushButton:hover { background: $control_hover; }
#segmented QPushButton:checked { background: $segment_on; border-color: $segment_border; }

QComboBox {
    background: $control; border: none; border-radius: 8px;
    padding: 6px 10px; min-height: 18px;
}
QComboBox:hover { background: $control_hover; }
QComboBox::drop-down { border: none; width: 26px; }
QComboBox::down-arrow { image: url("$chevron"); width: 10px; height: 10px; }
QComboBox QAbstractItemView {
    background: $card; border: 1px solid $card_border; border-radius: 8px;
    padding: 4px; outline: 0;
    selection-background-color: $accent; selection-color: $on_accent;
}
QComboBox QAbstractItemView::item { min-height: 26px; padding: 0 6px; border-radius: 5px; }

QSlider { min-height: 22px; }
QSlider::groove:horizontal { height: 4px; background: $groove; border-radius: 2px; }
QSlider::sub-page:horizontal { background: $accent; border-radius: 2px; }
QSlider::handle:horizontal {
    background: $handle; border: 1px solid $handle_border;
    width: 18px; height: 18px; margin: -8px 0; border-radius: 10px;
}
QSlider::sub-page:horizontal:disabled { background: $groove; }

QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: $scrollbar; border-radius: 3px; min-height: 30px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

#chip {
    background: $control; color: $secondary; border-radius: 10px;
    padding: 3px 10px; font-size: 12px;
}
#status { color: $secondary; font-size: 12px; }
#status[tone="error"] { color: $danger; }

QMenu {
    background: $card; border: 1px solid $card_border; border-radius: 8px; padding: 4px;
}
QMenu::item { padding: 6px 22px 6px 12px; border-radius: 5px; }
QMenu::item:selected { background: $accent; color: $on_accent; }
QMenu::separator { height: 1px; background: $separator; margin: 4px 6px; }

QProgressBar {
    background: $groove; border: none; border-radius: 3px;
    max-height: 6px; color: transparent;
}
QProgressBar::chunk { background: $accent; border-radius: 3px; }
QDialog, QMessageBox { background: $panel; }

#tourCard { background: $panel; border: 1px solid $card_border; border-radius: 14px; }
#tourTitle { font-size: 16px; font-weight: 600; }
#tourBody { color: $secondary; font-size: 13px; }
"""

CHEVRON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
    '<path d="M2 3.5 5 6.5 8 3.5" fill="none" stroke="{color}" stroke-width="1.6" '
    'stroke-linecap="round" stroke-linejoin="round"/></svg>'
)

def run_gui(initial: Path | None = None) -> int:
    from string import Template

    from PyQt6.QtCore import (
        QByteArray,
        QEasingCurve,
        QEvent,
        QPoint,
        QPointF,
        QPropertyAnimation,
        QRect,
        QRectF,
        QSettings,
        QSize,
        QSizeF,
        Qt,
        QThread,
        QTimer,
        pyqtProperty,
        pyqtSignal,
    )
    from PyQt6.QtGui import (
        QAction,
        QBrush,
        QColor,
        QFont,
        QIcon,
        QImageReader,
        QKeySequence,
        QPainter,
        QPainterPath,
        QPalette,
        QPen,
        QPixmap,
    )
    from PyQt6.QtSvg import QSvgRenderer
    from PyQt6.QtWidgets import (
        QApplication,
        QBoxLayout,
        QButtonGroup,
        QCheckBox,
        QComboBox,
        QFileDialog,
        QFrame,
        QGridLayout,
        QHBoxLayout,
        QLabel,
        QMainWindow,
        QMenu,
        QMessageBox,
        QProgressDialog,
        QPushButton,
        QScrollArea,
        QSizePolicy,
        QSlider,
        QStackedWidget,
        QToolButton,
        QVBoxLayout,
        QWidget,
    )

    PREVIEW_DEBOUNCE_MS = 180
    DRAFT_SIDE = 640  # long side of the quick preview pass on big images
    TITLE_BAR_HEIGHT = 44
    RESIZE_MARGIN = 6  # the see-through band around the window where dragging resizes it
    RECENT_LIMIT = 8
    SIDEBAR_WIDTH = 304
    CARD_RADIUS = 14.0

    # The live token table. Custom-painted widgets read it on every paint, so a
    # theme switch only needs a repaint.
    theme: dict[str, str] = dict(THEME_LIGHT)
    theme_dir = tempfile.TemporaryDirectory(prefix="vextrace-theme-")

    def color(token: str) -> QColor:
        return QColor(theme[token])

    def label(text: str, name: str) -> QLabel:
        widget = QLabel(text)
        widget.setObjectName(name)
        return widget

    def section_title(text: str) -> QLabel:
        return label(text, "sectionTitle")

    # -- custom controls -----------------------------------------------------------

    class Switch(QCheckBox):
        """An iOS-style toggle: label on the left, animated switch on the right."""

        TRACK_W, TRACK_H = 36, 22

        def __init__(self, text: str):
            super().__init__(text)
            self._knob = 0.0
            self._anim = QPropertyAnimation(self, b"knob", self)
            self._anim.setDuration(170)
            self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            self.toggled.connect(self._animate)
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        def _get_knob(self) -> float:
            return self._knob

        def _set_knob(self, value: float) -> None:
            self._knob = value
            self.update()

        knob = pyqtProperty(float, _get_knob, _set_knob)

        def _animate(self, on: bool) -> None:
            self._anim.stop()
            self._anim.setStartValue(self._knob)
            self._anim.setEndValue(1.0 if on else 0.0)
            self._anim.start()

        def sizeHint(self) -> QSize:
            text_w = self.fontMetrics().horizontalAdvance(self.text())
            return QSize(text_w + self.TRACK_W + 16, max(28, self.fontMetrics().height() + 8))

        def hitButton(self, pos) -> bool:
            return self.rect().contains(pos)

        def paintEvent(self, event):
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            if not self.isEnabled():
                p.setOpacity(0.45)

            w, h = self.TRACK_W, self.TRACK_H
            track = QRectF(self.width() - w - 1, (self.height() - h) / 2, w, h)
            off, on = color("switch_off"), color("accent")
            t = self._knob
            fill = QColor(
                round(off.red() + (on.red() - off.red()) * t),
                round(off.green() + (on.green() - off.green()) * t),
                round(off.blue() + (on.blue() - off.blue()) * t),
            )
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(fill)
            p.drawRoundedRect(track, h / 2, h / 2)

            d = h - 4
            knob = QRectF(track.left() + 2 + t * (w - h), track.top() + 2, d, d)
            p.setBrush(QColor(0, 0, 0, 40))
            p.drawEllipse(knob.translated(0, 0.75))
            p.setBrush(color("knob"))
            p.drawEllipse(knob)

            p.setPen(color("text"))
            text_rect = self.rect().adjusted(0, 0, -(w + 10), 0)
            p.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                       self.fontMetrics().elidedText(
                           self.text(), Qt.TextElideMode.ElideRight, text_rect.width()))

    class Segmented(QFrame):
        """A macOS-style segmented control."""

        changed = pyqtSignal(int)

        def __init__(self, labels: list[str], columns: int):
            super().__init__()
            self.setObjectName("segmented")
            layout = QGridLayout(self)
            layout.setContentsMargins(2, 2, 2, 2)
            layout.setSpacing(2)
            self._group = QButtonGroup(self)
            self._group.setExclusive(True)
            for index, text in enumerate(labels):
                button = QPushButton(text)
                button.setCheckable(True)
                button.setCursor(Qt.CursorShape.PointingHandCursor)
                button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
                self._group.addButton(button, index)
                layout.addWidget(button, index // columns, index % columns)
            self._group.idClicked.connect(self.changed.emit)

        def set_index(self, index: int) -> None:
            self._group.button(index).setChecked(True)

    class Spinner(QWidget):
        """The classic twelve-spoke activity indicator."""

        def __init__(self):
            super().__init__()
            self.setFixedSize(16, 16)
            self._step = 0
            self._timer = QTimer(self)
            self._timer.setInterval(80)
            self._timer.timeout.connect(self._tick)
            self.hide()

        def start(self) -> None:
            self.show()
            self._timer.start()

        def stop(self) -> None:
            self._timer.stop()
            self.hide()

        def _tick(self) -> None:
            self._step = (self._step + 1) % 12
            self.update()

        def paintEvent(self, event):
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.translate(self.width() / 2, self.height() / 2)
            base = color("secondary")
            for i in range(12):
                c = QColor(base)
                c.setAlphaF(0.15 + 0.85 * (((i - self._step) % 12) / 11))
                pen = QPen(c, 1.8)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                p.setPen(pen)
                p.drawLine(0, -4, 0, -7)
                p.rotate(30)

    class Canvas(QWidget):
        """A rounded card that shows a pixmap or an SVG over a checkerboard."""

        INSET = 24

        def __init__(self):
            super().__init__()
            self._pixmap: QPixmap | None = None
            self._renderer: QSvgRenderer | None = None
            self._dimmed = False
            self._light = False
            self.setMinimumSize(180, 180)
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        def set_pixmap(self, pixmap: QPixmap | None) -> None:
            self._pixmap, self._renderer = pixmap, None
            self.update()

        def set_svg(self, data: bytes | None) -> None:
            self._pixmap = None
            self._renderer = None
            if data:
                renderer = QSvgRenderer(QByteArray(data), self)
                if renderer.isValid():
                    self._renderer = renderer
            self.update()

        def set_light(self, light: bool) -> None:
            """Use a light backdrop whatever the theme, so black ink stays visible."""
            if light != self._light:
                self._light = light
                self.update()

        def set_dimmed(self, dimmed: bool) -> None:
            if dimmed != self._dimmed:
                self._dimmed = dimmed
                self.update()

        def _paint_busy(self, p: QPainter, rect: QRectF) -> None:
            """A small "Tracing…" pill in the middle while a trace is running."""
            text = "Tracing…"
            metrics = self.fontMetrics()
            pill = QRectF(0, 0, metrics.horizontalAdvance(text) + 28, metrics.height() + 14)
            pill.moveCenter(rect.center())
            back = color("panel")
            back.setAlphaF(0.92)
            p.setPen(QPen(color("card_border"), 1))
            p.setBrush(back)
            p.drawRoundedRect(pill, pill.height() / 2, pill.height() / 2)
            p.setPen(color("secondary"))
            p.drawText(pill, Qt.AlignmentFlag.AlignCenter, text)

        def _content_size(self) -> QSizeF | None:
            if self._pixmap is not None and not self._pixmap.isNull():
                return QSizeF(self._pixmap.deviceIndependentSize())
            if self._renderer is not None:
                box = self._renderer.viewBoxF()
                if box.width() > 0 and box.height() > 0:
                    return box.size()
                return QSizeF(self._renderer.defaultSize())
            return None

        def paintEvent(self, event):
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

            rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            path = QPainterPath()
            path.addRoundedRect(rect, CARD_RADIUS, CARD_RADIUS)
            tokens = THEME_LIGHT if self._light else theme
            p.fillPath(path, QColor(tokens["card"]))
            p.save()
            p.setClipPath(path)

            tile = QPixmap(16, 16)
            tile.fill(QColor(tokens["checker_a"]))
            tp = QPainter(tile)
            tp.fillRect(0, 0, 8, 8, QColor(tokens["checker_b"]))
            tp.fillRect(8, 8, 8, 8, QColor(tokens["checker_b"]))
            tp.end()
            p.fillRect(rect, QBrush(tile))

            size = self._content_size()
            if size is not None and not size.isEmpty():
                inner = rect.adjusted(self.INSET, self.INSET, -self.INSET, -self.INSET)
                fitted = size.scaled(inner.size(), Qt.AspectRatioMode.KeepAspectRatio)
                target = QRectF(
                    inner.left() + (inner.width() - fitted.width()) / 2,
                    inner.top() + (inner.height() - fitted.height()) / 2,
                    fitted.width(), fitted.height(),
                )
                if self._dimmed:
                    p.setOpacity(0.55)
                if self._pixmap is not None:
                    p.drawPixmap(target, self._pixmap, QRectF(self._pixmap.rect()))
                elif self._renderer is not None:
                    self._renderer.render(p, target)
                p.setOpacity(1.0)
            if self._dimmed:
                self._paint_busy(p, rect)
            p.restore()

            p.setPen(QPen(color("card_border"), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)

    class Glyph(QWidget):
        """A small line drawing in the accent colour: an image or a folder."""

        def __init__(self, kind: str, size: int = 36):
            super().__init__()
            self._kind = kind
            self.setFixedSize(size, size)
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        def paintEvent(self, event):
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            tint = color("accent")
            back = QColor(tint)
            back.setAlphaF(0.12)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(back)
            p.drawRoundedRect(QRectF(self.rect()), 9, 9)

            pen = QPen(tint, 1.8)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            s = self.width() / 36
            p.scale(s, s)
            if self._kind == "image":
                p.drawRoundedRect(QRectF(9, 10, 18, 16), 2.5, 2.5)
                mountains = QPainterPath()
                mountains.moveTo(10, 24)
                mountains.lineTo(16, 18)
                mountains.lineTo(20, 22)
                mountains.lineTo(22, 20)
                mountains.lineTo(26, 24)
                p.drawPath(mountains)
                p.drawEllipse(QRectF(21, 13.5, 3, 3))
            else:
                folder = QPainterPath()
                folder.moveTo(9, 25)
                folder.lineTo(9, 12)
                folder.lineTo(15, 12)
                folder.lineTo(17, 14.5)
                folder.lineTo(27, 14.5)
                folder.lineTo(27, 25)
                folder.closeSubpath()
                p.drawPath(folder)
                p.drawLine(QPointF(9, 17.5), QPointF(27, 17.5))

    class ActionTile(QPushButton):
        """A large home-page button: glyph, title, description and shortcut."""

        def __init__(self, kind: str, title: str, detail: str, shortcut: str):
            super().__init__()
            self.setObjectName("tile")
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            layout = QHBoxLayout(self)
            layout.setContentsMargins(14, 12, 16, 12)
            layout.setSpacing(14)
            layout.addWidget(Glyph(kind))
            text = QVBoxLayout()
            text.setSpacing(1)
            text.addWidget(label(title, "tileTitle"))
            text.addWidget(label(detail, "caption"))
            layout.addLayout(text, 1)
            layout.addWidget(label(shortcut, "shortcut"))
            for child in self.findChildren(QLabel):
                child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        def sizeHint(self) -> QSize:
            return self.layout().sizeHint()

        def minimumSizeHint(self) -> QSize:
            return self.layout().minimumSize()

    class RecentRow(QPushButton):
        """One recently opened image: thumbnail, name and folder."""

        THUMB = 40

        def __init__(self, path: Path):
            super().__init__()
            self.path = path
            self.setObjectName("recentRow")
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setToolTip(str(path))
            layout = QHBoxLayout(self)
            layout.setContentsMargins(8, 6, 10, 6)
            layout.setSpacing(12)

            thumb = QLabel()
            thumb.setFixedSize(self.THUMB, self.THUMB)
            thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
            thumb.setPixmap(thumbnail(path, QSize(self.THUMB, self.THUMB),
                                      self.devicePixelRatioF()))
            layout.addWidget(thumb)

            text = QVBoxLayout()
            text.setSpacing(0)
            text.addWidget(label(path.name, "rowLabel"))
            folder = label("", "caption")
            folder.setText(folder.fontMetrics().elidedText(
                str(path.parent).replace(str(Path.home()), "~", 1),
                Qt.TextElideMode.ElideMiddle, 300))
            text.addWidget(folder)
            layout.addLayout(text, 1)
            for child in self.findChildren(QLabel):
                child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        def sizeHint(self) -> QSize:
            return self.layout().sizeHint()

        def minimumSizeHint(self) -> QSize:
            return self.layout().minimumSize()

    class FlipBox(QWidget):
        """Lays its children out side by side, or stacked when it gets narrow."""

        def __init__(self, breakpoint: int, spacing: int):
            super().__init__()
            self._breakpoint = breakpoint
            self.box = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
            self.box.setContentsMargins(0, 0, 0, 0)
            self.box.setSpacing(spacing)

        def resizeEvent(self, event):
            wide = event.size().width() >= self._breakpoint
            direction = (QBoxLayout.Direction.LeftToRight if wide
                         else QBoxLayout.Direction.TopToBottom)
            if self.box.direction() != direction:
                self.box.setDirection(direction)
            super().resizeEvent(event)

    def thumbnail(path: Path, size: QSize, ratio: float, cover: bool = False) -> QPixmap:
        """A small, upright, quickly decoded preview of an image file."""
        reader = QImageReader(str(path))
        reader.setAutoTransform(True)
        full = reader.size()
        target = QSize(round(size.width() * ratio), round(size.height() * ratio))
        if full.isValid():
            mode = (Qt.AspectRatioMode.KeepAspectRatioByExpanding if cover
                    else Qt.AspectRatioMode.KeepAspectRatio)
            reader.setScaledSize(full.scaled(target, mode))
        image = reader.read()
        if image.isNull():
            return QPixmap()
        if cover:
            image = image.copy((image.width() - target.width()) // 2,
                               (image.height() - target.height()) // 2,
                               target.width(), target.height())
        pixmap = QPixmap.fromImage(image)
        pixmap.setDevicePixelRatio(ratio)
        return pixmap

    class SampleCard(QPushButton):
        """A sample image that ships with the app, ready to try."""

        SIZE = QSize(150, 96)

        def __init__(self, path: Path):
            super().__init__()
            self.path = path
            self.setObjectName("sample")
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setToolTip(f"Open the {path.stem} sample")
            layout = QVBoxLayout(self)
            layout.setContentsMargins(6, 6, 6, 8)
            layout.setSpacing(6)
            image = QLabel()
            image.setFixedSize(self.SIZE)
            image.setPixmap(rounded(thumbnail(path, self.SIZE, self.devicePixelRatioF(),
                                              cover=True), 8))
            layout.addWidget(image)
            layout.addWidget(label(path.stem.replace("-", " ").capitalize(), "rowLabel"))
            for child in self.findChildren(QLabel):
                child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        def sizeHint(self) -> QSize:
            return self.layout().sizeHint()

    def rounded(pixmap: QPixmap, radius: float) -> QPixmap:
        if pixmap.isNull():
            return pixmap
        out = QPixmap(pixmap.size())
        out.setDevicePixelRatio(pixmap.devicePixelRatio())
        out.fill(Qt.GlobalColor.transparent)
        p = QPainter(out)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(QPointF(0, 0), pixmap.deviceIndependentSize()), radius, radius)
        p.setClipPath(clip)
        p.drawPixmap(0, 0, pixmap)
        p.end()
        return out

    class HomePage(QWidget):
        """What the app opens to: ways to start, samples, and recently opened images."""

        def __init__(self, on_open, on_batch, on_recent, on_clear):
            super().__init__()
            self._active = False
            self._on_recent = on_recent

            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            page = QVBoxLayout(self)
            page.setContentsMargins(0, 0, 0, 0)
            page.addWidget(scroll)

            holder = QWidget()
            outer = QHBoxLayout(holder)
            outer.setContentsMargins(36, 0, 36, 0)
            outer.addStretch(1)
            content = QWidget()
            content.setMaximumWidth(900)
            content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            outer.addWidget(content, 100)
            outer.addStretch(1)
            scroll.setWidget(holder)

            layout = QVBoxLayout(content)
            layout.setContentsMargins(0, 48, 0, 36)
            layout.setSpacing(0)

            hero = QHBoxLayout()
            hero.setSpacing(18)
            logo = QLabel()
            icon = _find_icon()
            if icon:
                logo.setPixmap(QIcon(str(icon)).pixmap(QSize(64, 64), self.devicePixelRatioF()))
            hero.addWidget(logo)
            words = QVBoxLayout()
            words.setSpacing(2)
            words.addStretch(1)
            words.addWidget(label(APP_NAME, "homeTitle"))
            words.addWidget(label("Turn images into clean, scalable SVG.", "homeSubtitle"))
            words.addStretch(1)
            hero.addLayout(words, 1)
            layout.addLayout(hero)
            layout.addSpacing(40)

            columns = FlipBox(breakpoint=760, spacing=44)

            start_box = QWidget()
            start = QVBoxLayout(start_box)
            start.setContentsMargins(0, 0, 0, 0)
            start.setSpacing(10)
            start.addWidget(label("Start", "homeHeading"))
            self.open_tile = ActionTile("image", "Open an image",
                                        "PNG, JPEG, WebP, BMP, GIF or TIFF", "Ctrl+O")
            self.open_tile.clicked.connect(on_open)
            start.addWidget(self.open_tile)
            batch_tile = ActionTile("folder", "Convert a folder",
                                    "Trace every image in a folder at once", "Ctrl+B")
            batch_tile.clicked.connect(on_batch)
            start.addWidget(batch_tile)
            start.addWidget(label("You can also drop an image anywhere in this window.",
                                  "caption"))

            self.samples = QWidget()
            samples = QVBoxLayout(self.samples)
            samples.setContentsMargins(0, 22, 0, 0)
            samples.setSpacing(10)
            samples.addWidget(label("Try a sample", "homeHeading"))
            row = QHBoxLayout()
            row.setSpacing(10)
            for path in sample_images():
                card = SampleCard(path)
                card.clicked.connect(lambda _checked=False, p=path: on_recent(p))
                row.addWidget(card)
            row.addStretch(1)
            samples.addLayout(row)
            self.samples.setVisible(bool(sample_images()))
            start.addWidget(self.samples)
            start.addStretch(1)
            columns.box.addWidget(start_box, 1)

            recent_box = QWidget()
            recent = QVBoxLayout(recent_box)
            recent.setContentsMargins(0, 0, 0, 0)
            recent.setSpacing(6)
            heading = QHBoxLayout()
            heading.addWidget(label("Recent", "homeHeading"))
            heading.addStretch(1)
            self._clear = QPushButton("Clear")
            self._clear.setObjectName("link")
            self._clear.setCursor(Qt.CursorShape.PointingHandCursor)
            self._clear.clicked.connect(on_clear)
            heading.addWidget(self._clear)
            recent.addLayout(heading)
            recent.addSpacing(4)
            self._recent_box = QVBoxLayout()
            self._recent_box.setSpacing(2)
            recent.addLayout(self._recent_box)
            recent.addStretch(1)
            columns.box.addWidget(recent_box, 1)

            layout.addWidget(columns, 1)

        def set_recent(self, paths: list[Path]) -> None:
            while self._recent_box.count():
                widget = self._recent_box.takeAt(0).widget()
                if widget is not None:
                    widget.deleteLater()
            for path in paths:
                row = RecentRow(path)
                row.clicked.connect(lambda _checked=False, p=path: self._on_recent(p))
                self._recent_box.addWidget(row)
            if not paths:
                empty = label("Images you open will show up here.", "caption")
                self._recent_box.addWidget(empty)
            self._clear.setVisible(bool(paths))

        def set_active(self, active: bool) -> None:
            if active != self._active:
                self._active = active
                self.update()

        def paintEvent(self, event):
            p = QPainter(self)
            p.fillRect(self.rect(), color("window"))
            if not self._active:
                return
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setBrush(color("drop_active"))
            pen = QPen(color("accent"), 1.5)
            pen.setStyle(Qt.PenStyle.CustomDashLine)
            pen.setDashPattern([5, 4])
            p.setPen(pen)
            p.drawRoundedRect(QRectF(self.rect()).adjusted(12, 12, -12, -12), 18, 18)

    class WindowButton(QPushButton):
        """Minimise, maximise/restore or close, drawn like the Windows caption buttons."""

        def __init__(self, kind: str):
            super().__init__()
            self.kind = kind
            self.setFixedSize(46, TITLE_BAR_HEIGHT)
            self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self.setAttribute(Qt.WidgetAttribute.WA_Hover)  # repaint on hover
            self.setToolTip({"min": "Minimize", "max": "Maximize", "close": "Close"}[kind])

        def paintEvent(self, event):
            p = QPainter(self)
            hovered = self.underMouse()
            if self.kind == "close" and (hovered or self.isDown()):
                p.fillRect(self.rect(), QColor("#c42b1c" if hovered else "#b22a1d"))
                ink = QColor("#ffffff")
            else:
                if self.isDown():
                    p.fillRect(self.rect(), color("control_pressed"))
                elif hovered:
                    p.fillRect(self.rect(), color("control_hover"))
                ink = color("text")
            p.setRenderHint(QPainter.RenderHint.Antialiasing, self.kind == "close")
            pen = QPen(ink, 1)
            pen.setCosmetic(True)
            p.setPen(pen)
            cx, cy = self.width() // 2, self.height() // 2
            if self.kind == "min":
                p.drawLine(cx - 5, cy, cx + 5, cy)
            elif self.kind == "close":
                p.drawLine(QPointF(cx - 5, cy - 5), QPointF(cx + 5, cy + 5))
                p.drawLine(QPointF(cx - 5, cy + 5), QPointF(cx + 5, cy - 5))
            elif self.window().isMaximized():
                p.drawRect(cx - 5, cy - 3, 8, 8)
                p.drawLine(cx - 3, cy - 5, cx + 5, cy - 5)
                p.drawLine(cx + 5, cy - 5, cx + 5, cy + 3)
            else:
                p.drawRect(cx - 5, cy - 5, 10, 10)

    class TitleBar(QWidget):
        """The app's own title bar: drag to move, double-click to maximise."""

        def mousePressEvent(self, event):
            if event.button() == Qt.MouseButton.LeftButton:
                self.window().windowHandle().startSystemMove()

        def mouseDoubleClickEvent(self, event):
            if event.button() == Qt.MouseButton.LeftButton:
                self.window().toggle_maximized()

    class Frame(QWidget):
        """The window's outer edge. Without a system frame, resizing starts here."""

        def __init__(self):
            super().__init__()
            self.setObjectName("root")
            self.setMouseTracking(True)

        def _edges(self, pos) -> Qt.Edge:
            edges = Qt.Edge(0)
            if self.window().isMaximized():
                return edges
            m = RESIZE_MARGIN
            if pos.x() < m:
                edges |= Qt.Edge.LeftEdge
            elif pos.x() >= self.width() - m:
                edges |= Qt.Edge.RightEdge
            if pos.y() < m:
                edges |= Qt.Edge.TopEdge
            elif pos.y() >= self.height() - m:
                edges |= Qt.Edge.BottomEdge
            return edges

        def mouseMoveEvent(self, event):
            edges = self._edges(event.position().toPoint())
            left_or_right = Qt.Edge.LeftEdge | Qt.Edge.RightEdge
            top_or_bottom = Qt.Edge.TopEdge | Qt.Edge.BottomEdge
            if edges in (Qt.Edge.LeftEdge | Qt.Edge.TopEdge,
                         Qt.Edge.RightEdge | Qt.Edge.BottomEdge):
                self.setCursor(Qt.CursorShape.SizeFDiagCursor)
            elif edges in (Qt.Edge.RightEdge | Qt.Edge.TopEdge,
                           Qt.Edge.LeftEdge | Qt.Edge.BottomEdge):
                self.setCursor(Qt.CursorShape.SizeBDiagCursor)
            elif edges & left_or_right:
                self.setCursor(Qt.CursorShape.SizeHorCursor)
            elif edges & top_or_bottom:
                self.setCursor(Qt.CursorShape.SizeVerCursor)
            else:
                self.unsetCursor()

        def mousePressEvent(self, event):
            edges = self._edges(event.position().toPoint())
            if event.button() == Qt.MouseButton.LeftButton and edges:
                self.window().windowHandle().startSystemResize(edges)

        def leaveEvent(self, event):
            self.unsetCursor()

        def paintEvent(self, event):
            p = QPainter(self)
            if self.window().isMaximized():
                p.fillRect(self.rect(), color("panel"))
                return
            # The resize band is see-through, with a soft shadow like any other window.
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            m = RESIZE_MARGIN
            for i in range(m):
                p.setPen(QColor(0, 0, 0, round(28 * (i + 1) / m)))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(QRectF(self.rect()).adjusted(i + 0.5, i + 0.5, -i - 0.5, -i - 0.5),
                                  m - i + 2, m - i + 2)
            inner = QRectF(self.rect()).adjusted(m, m, -m, -m)
            p.fillRect(inner, color("panel"))
            p.setPen(color("frame"))
            p.drawRect(inner.adjusted(-0.5, -0.5, 0.5, 0.5))

    class Tour(QWidget):
        """A first-run walkthrough: dims the window and spotlights one control at a time."""

        CARD_WIDTH = 320

        def __init__(self, host: QWidget, steps: list[dict], on_finish):
            super().__init__(host)
            self._host = host
            self._steps = steps
            self._on_finish = on_finish
            self._index = 0
            self._hole = QRect()
            self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

            self.card = QFrame(self)
            self.card.setObjectName("tourCard")
            self.card.setFixedWidth(self.CARD_WIDTH)
            layout = QVBoxLayout(self.card)
            layout.setContentsMargins(20, 18, 20, 16)
            layout.setSpacing(6)
            self._counter = label("", "caption")
            self._title = label("", "tourTitle")
            self._body = label("", "tourBody")
            self._body.setWordWrap(True)
            for widget in (self._counter, self._title, self._body):
                layout.addWidget(widget)
            layout.addSpacing(10)
            buttons = QHBoxLayout()
            skip = QPushButton("Skip tour")
            skip.setObjectName("link")
            skip.clicked.connect(self.finish)
            self._back = QPushButton("Back")
            self._back.clicked.connect(lambda: self.show_step(self._index - 1))
            self._next = QPushButton("Next")
            self._next.setObjectName("primary")
            self._next.clicked.connect(self._advance)
            buttons.addWidget(skip)
            buttons.addStretch(1)
            buttons.addWidget(self._back)
            buttons.addWidget(self._next)
            for button in (skip, self._back, self._next):
                button.setCursor(Qt.CursorShape.PointingHandCursor)
            layout.addLayout(buttons)

            self.setGeometry(host.rect())
            self.show()
            self.raise_()
            self.show_step(0)

        def show_step(self, index: int) -> None:
            self._index = max(0, min(index, len(self._steps) - 1))
            step = self._steps[self._index]
            if step.get("before"):
                step["before"]()
            self._counter.setText(f"{self._index + 1} of {len(self._steps)}")
            self._title.setText(step["title"])
            self._body.setText(step["body"])
            # Wrapped labels don't grow on their own; give the text the height it needs.
            text_width = self.CARD_WIDTH - 40
            self._body.setFixedSize(text_width, self._body.heightForWidth(text_width))
            self._back.setVisible(self._index > 0)
            last = self._index == len(self._steps) - 1
            self._next.setText("Done" if last else "Next")
            self._next.setFocus()
            # Let the page finish laying out before measuring the target.
            QTimer.singleShot(80, self.relayout)

        def _advance(self) -> None:
            if self._index == len(self._steps) - 1:
                self.finish()
            else:
                self.show_step(self._index + 1)

        def relayout(self) -> None:
            self.setGeometry(self._host.rect())
            target = self._steps[self._index]["target"]()
            if target is None or not target.isVisible():
                self._hole = QRect()
            else:
                top_left = target.mapTo(self._host, QPoint(0, 0))
                self._hole = QRect(top_left, target.size()).adjusted(-6, -6, 6, 6)
            self.card.adjustSize()
            self.card.move(self._card_position())
            self.update()

        def _card_position(self) -> QPoint:
            card, gap, edge = self.card.size(), 16, 12
            area = self.rect().adjusted(edge, edge, -edge, -edge)
            hole = self._hole
            if hole.isNull():
                return QPoint(area.center().x() - card.width() // 2,
                              area.center().y() - card.height() // 2)
            middle_y = hole.center().y() - card.height() // 2
            middle_x = hole.center().x() - card.width() // 2
            options = [
                QPoint(hole.right() + gap, middle_y),               # right of it
                QPoint(hole.left() - gap - card.width(), middle_y),  # left of it
                QPoint(middle_x, hole.bottom() + gap),               # below
                QPoint(middle_x, hole.top() - gap - card.height()),  # above
            ]
            for spot in options:
                if area.contains(QRect(spot, card)):
                    return spot
            # Nowhere fits cleanly (a huge target): sit inside it, clamped to the window.
            spot = QPoint(hole.right() - card.width() - gap, hole.bottom() - card.height() - gap)
            return QPoint(max(area.left(), min(spot.x(), area.right() - card.width())),
                          max(area.top(), min(spot.y(), area.bottom() - card.height())))

        def finish(self) -> None:
            self.hide()
            self.deleteLater()
            self._on_finish()

        def paintEvent(self, event):
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            shade = QPainterPath()
            shade.addRect(QRectF(self.rect()))
            if not self._hole.isNull():
                spot = QPainterPath()
                spot.addRoundedRect(QRectF(self._hole), 12, 12)
                shade = shade.subtracted(spot)
            p.fillPath(shade, QColor(0, 0, 0, 140))
            if not self._hole.isNull():
                p.setPen(QPen(color("accent"), 2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(QRectF(self._hole), 12, 12)

        def mousePressEvent(self, event):
            event.accept()  # the rest of the window waits until the tour is done

        def keyPressEvent(self, event):
            key = event.key()
            if key == Qt.Key.Key_Escape:
                self.finish()
            elif key in (Qt.Key.Key_Right, Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._advance()
            elif key == Qt.Key.Key_Left:
                self.show_step(self._index - 1)
            else:
                super().keyPressEvent(event)

    class SliderRow(QWidget):
        """Label and value on one line, slider beneath — the Apple settings layout."""

        def __init__(self, text: str, low: int, high: int, value: int, help_text: str,
                     suffix: str = ""):
            super().__init__()
            self._suffix = suffix
            layout = QVBoxLayout(self)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(2)

            top = QHBoxLayout()
            name = label(text, "rowLabel")
            self.value_label = label("", "rowValue")
            self.value_label.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            top.addWidget(name)
            top.addStretch(1)
            top.addWidget(self.value_label)
            layout.addLayout(top)

            self.slider = QSlider(Qt.Orientation.Horizontal)
            self.slider.setRange(low, high)
            self.slider.setPageStep(max(1, (high - low) // 10))
            self.slider.setCursor(Qt.CursorShape.PointingHandCursor)
            self.slider.valueChanged.connect(self._show)
            self.slider.setValue(value)
            layout.addWidget(self.slider)

            for widget in (self, name, self.slider):
                widget.setToolTip(help_text)
            self._show(self.slider.value())

        def _show(self, value: int) -> None:
            self.value_label.setText(f"{value}{self._suffix}")

    # -- workers -------------------------------------------------------------------

    class TraceWorker(QThread):
        """Runs one trace off the UI thread."""

        finished_ok = pyqtSignal(int, bytes, float, bool)
        failed = pyqtSignal(int, str)

        def __init__(self, token: int, path: Path, settings: Settings, draft: bool):
            super().__init__()
            self.token = token
            self._path = path
            self._settings = settings
            self._draft = draft

        def run(self) -> None:
            started = time.perf_counter()
            try:
                svg = vectorize(self._path, self._settings)
            except Exception as exc:  # noqa: BLE001 - shown in the status line, never fatal
                self.failed.emit(self.token, str(exc))
            else:
                self.finished_ok.emit(self.token, svg, time.perf_counter() - started,
                                      self._draft)

    class BatchWorker(QThread):
        """Converts a folder, reporting progress as it goes."""

        progressed = pyqtSignal(int, str)
        done = pyqtSignal(int, int, list)

        def __init__(self, files: list[Path], out_dir: Path, settings: Settings):
            super().__init__()
            self._files = files
            self._out_dir = out_dir
            self._settings = settings
            self._cancelled = False

        def cancel(self) -> None:
            self._cancelled = True

        def run(self) -> None:
            ok = 0
            errors: list[str] = []
            used: set[Path] = set()
            for index, path in enumerate(self._files):
                if self._cancelled:
                    break
                self.progressed.emit(index, path.name)
                try:
                    svg = vectorize(path, self._settings)
                    unique_target(self._out_dir, path.stem, used).write_bytes(svg)
                except Exception as exc:  # noqa: BLE001 - one bad file must not stop the batch
                    errors.append(f"{path.name}: {exc}")
                else:
                    ok += 1
            self.done.emit(ok, len(self._files), errors)

    # -- main window ---------------------------------------------------------------

    SLIDER_SPECS = [
        ("filter_speckle", "Speckle filter", 0, 128),
        ("color_precision", "Color precision", 1, 8),
        ("layer_difference", "Layer difference", 0, 128),
        ("corner_threshold", "Corner threshold", 0, 180),
        ("path_precision", "Path precision", 0, 10),
        ("bw_threshold", "B&W threshold", 0, 255),
        ("bg_tolerance", "Background tolerance", 0, 128),
        ("line_detail", "Line detail", 0, 100),
        ("line_weight", "Line weight", 1, 6),
    ]

    class MainWindow(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle(APP_NAME)
            # The app draws its own title bar, the same on every desktop.
            self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.resize(1240, 800)
            self.setMinimumSize(820, 560)
            self.setAcceptDrops(True)

            self.settings = Settings()
            self.image_path: Path | None = None
            self.svg: bytes | None = None
            self._token = 0
            self._worker: TraceWorker | None = None
            self._pending = False  # settings changed while a trace was running
            self._refine = False  # a quick draft is showing; the full trace is next
            self._image_side = 0  # longest side of the open image, in pixels
            self._batch: BatchWorker | None = None
            self._loading = False  # suppress re-renders while widgets are updated
            self._prefs = QSettings(APP_NAME, APP_NAME)
            self._tour: Tour | None = None

            icon = _find_icon()
            if icon:
                self.setWindowIcon(QIcon(str(icon)))

            self.frame = Frame()
            outer = QVBoxLayout(self.frame)
            outer.setSpacing(0)
            outer.addWidget(self._build_title_bar())

            self.home = HomePage(self.choose_file, self.batch_convert, self.open_path,
                                 self._clear_recent)
            self.home.setObjectName("home")
            editor = QWidget()
            body = QHBoxLayout(editor)
            body.setContentsMargins(0, 0, 0, 0)
            body.setSpacing(0)
            body.addWidget(self._build_workspace(), 1)
            body.addWidget(self._build_sidebar())

            self.pages = QStackedWidget()
            self.pages.addWidget(self.home)
            self.pages.addWidget(editor)
            outer.addWidget(self.pages, 1)
            self.setCentralWidget(self.frame)
            # The frame shows resize cursors at its edge; everything inside it
            # gets an ordinary arrow so those cursors don't leak inwards.
            for child in (self.pages, self.frame.layout().itemAt(0).widget()):
                child.setCursor(Qt.CursorShape.ArrowCursor)

            self._build_actions()
            self._load_widgets_from_settings()

            self._debounce = QTimer(self)
            self._debounce.setSingleShot(True)
            self._debounce.setInterval(PREVIEW_DEBOUNCE_MS)
            self._debounce.timeout.connect(self._start_trace)

            self._sync_frame()
            if initial:
                self.open_path(initial)
            else:
                self.go_home()

        # -- construction ------------------------------------------------------------

        def _build_title_bar(self) -> QWidget:
            bar = TitleBar()
            bar.setObjectName("titleBar")
            bar.setFixedHeight(TITLE_BAR_HEIGHT)
            layout = QHBoxLayout(bar)
            layout.setContentsMargins(14, 0, 0, 0)
            layout.setSpacing(8)

            icon = _find_icon()
            if icon:
                badge = QLabel()
                badge.setPixmap(QIcon(str(icon)).pixmap(QSize(20, 20), self.devicePixelRatioF()))
                layout.addWidget(badge)
            layout.addWidget(label(APP_NAME, "titleText"))
            layout.addSpacing(6)

            self.home_button = QPushButton("Home")
            self.home_button.setObjectName("ghost")
            self.home_button.setToolTip("Back to the home page")
            self.home_button.clicked.connect(self.go_home)
            layout.addWidget(self.home_button)
            self.file_label = label("", "titleFile")
            layout.addWidget(self.file_label)
            layout.addStretch(1)

            self.open_button = QPushButton("Open…")
            self.open_button.setToolTip("Open an image  (Ctrl+O)")
            self.open_button.clicked.connect(self.choose_file)

            self.batch_button = QPushButton("Batch…")
            self.batch_button.setToolTip("Convert every image in a folder with these settings  (Ctrl+B)")
            self.batch_button.clicked.connect(self.batch_convert)

            self.save_button = QPushButton("Export SVG")
            self.save_button.setObjectName("primary")
            self.save_button.setToolTip("Save the traced SVG  (Ctrl+S)")
            self.save_button.clicked.connect(self.save_as)
            self.save_button.setEnabled(False)

            more = QToolButton()
            more.setObjectName("titleMenu")
            more.setText("•••")
            more.setToolTip("More")
            more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            menu = QMenu(more)
            menu.addAction("Show tutorial", self.start_tour)
            menu.addAction(f"About {APP_NAME}", self._about)
            menu.addSeparator()
            menu.addAction("Quit", self.close)
            more.setMenu(menu)

            self.editor_buttons = (self.home_button, self.file_label, self.open_button,
                                   self.batch_button, self.save_button)
            for button in (self.home_button, self.open_button, self.batch_button,
                           self.save_button, more):
                button.setCursor(Qt.CursorShape.PointingHandCursor)
            for button in (self.open_button, self.batch_button, self.save_button):
                layout.addWidget(button)
            layout.addSpacing(6)
            layout.addWidget(more)
            layout.addSpacing(6)

            self.window_buttons = [WindowButton(kind) for kind in ("min", "max", "close")]
            minimize, maximize, close = self.window_buttons
            minimize.clicked.connect(self.showMinimized)
            maximize.clicked.connect(self.toggle_maximized)
            close.clicked.connect(self.close)
            controls = QHBoxLayout()
            controls.setSpacing(0)
            for button in self.window_buttons:
                controls.addWidget(button)
            layout.addLayout(controls)
            return bar

        def _build_workspace(self) -> QWidget:
            workspace = QWidget()
            workspace.setObjectName("workspace")
            layout = QVBoxLayout(workspace)
            layout.setContentsMargins(22, 20, 22, 14)
            layout.setSpacing(12)

            self.compare = FlipBox(breakpoint=560, spacing=16)
            self.original_canvas, self.original_meta = self._pane(self.compare.box, "Original")
            self.svg_canvas, self.svg_meta = self._pane(self.compare.box, "Vector")
            layout.addWidget(self.compare, 1)

            footer = QHBoxLayout()
            footer.setSpacing(8)
            self.spinner = Spinner()
            footer.addWidget(self.spinner)
            self.status_label = label("", "status")
            footer.addWidget(self.status_label, 1)
            self.chips_box = QWidget()
            chips = QHBoxLayout(self.chips_box)
            chips.setContentsMargins(0, 0, 0, 0)
            chips.setSpacing(8)
            self.chips = [label("", "chip") for _ in range(3)]
            for chip in self.chips:
                chip.hide()
                chips.addWidget(chip)
            footer.addWidget(self.chips_box)
            layout.addLayout(footer)
            return workspace

        def _pane(self, parent: QBoxLayout, title: str) -> tuple[Canvas, QLabel]:
            column = QVBoxLayout()
            column.setSpacing(8)
            top = QHBoxLayout()
            top.setContentsMargins(4, 0, 4, 0)
            top.addWidget(label(title, "paneTitle"))
            top.addStretch(1)
            meta = label("", "caption")
            top.addWidget(meta)
            column.addLayout(top)
            canvas = Canvas()
            column.addWidget(canvas, 1)
            parent.addLayout(column, 1)
            return canvas, meta

        def _build_sidebar(self) -> QWidget:
            sidebar = QWidget()
            sidebar.setObjectName("sidebar")
            sidebar.setFixedWidth(SIDEBAR_WIDTH)
            outer = QVBoxLayout(sidebar)
            outer.setContentsMargins(0, 0, 0, 0)
            outer.setSpacing(0)

            content = QWidget()
            layout = QVBoxLayout(content)
            layout.setContentsMargins(20, 20, 20, 20)
            layout.setSpacing(10)

            # Mode
            layout.addWidget(section_title("Mode"))
            self.mode_control = Segmented([MODE_LABELS[m] for m in MODES], columns=3)
            self.mode_control.setToolTip(
                "Color: full-colour tracing.\n"
                "B&W and Potrace: black and white. Potrace is cleanest on logos and ink.\n"
                "Silhouette: the subject as one solid shape, background removed.\n"
                "Lines: the image's edges as a line drawing."
            )
            self.mode_control.changed.connect(self._on_mode_changed)
            layout.addWidget(self.mode_control)
            self.notice = label("", "notice")
            self.notice.setWordWrap(True)
            self.notice.setTextFormat(Qt.TextFormat.RichText)
            self.notice.hide()
            layout.addWidget(self.notice)
            layout.addSpacing(12)

            # Preset
            layout.addWidget(section_title("Preset"))
            self.preset_combo = QComboBox()
            self.preset_combo.addItem("Custom")
            for name in PRESETS:
                self.preset_combo.addItem(name)
            self.preset_combo.setToolTip("Jump to a set of settings that suits this kind of artwork.")
            self.preset_combo.setCursor(Qt.CursorShape.PointingHandCursor)
            self.preset_combo.currentIndexChanged.connect(self._on_preset_changed)
            layout.addWidget(self.preset_combo)
            layout.addSpacing(12)

            # Detail sliders
            layout.addWidget(section_title("Detail"))
            self.slider_rows: dict[str, SliderRow] = {}
            for key, text, low, high in SLIDER_SPECS:
                row = SliderRow(text, low, high, int(getattr(self.settings, key)),
                                SLIDER_HELP[key])
                row.slider.valueChanged.connect(lambda value, k=key: self._on_slider(k, value))
                self.slider_rows[key] = row
                layout.addWidget(row)
            layout.addSpacing(12)

            # Options
            layout.addWidget(section_title("Options"))
            self.bg_check = Switch("Remove background")
            self.bg_check.setToolTip(
                "Find the most common colour around the edge of the image and make it\n"
                "transparent, so the SVG has no background rectangle behind the artwork."
            )
            self.bg_check.toggled.connect(self._on_option("remove_bg"))
            layout.addWidget(self.bg_check)

            self.square_check = Switch("Square 1:1 canvas")
            self.square_check.setToolTip(
                "Pad the artwork out to a square viewBox, centred, so it drops into\n"
                "square slots (icons, avatars, app tiles) without being stretched."
            )
            self.square_check.toggled.connect(self._on_option("square"))
            layout.addWidget(self.square_check)

            self.pad_row = SliderRow("Padding", 0, 25, round(self.settings.padding * 100),
                                     SLIDER_HELP["padding"], suffix="%")
            self.pad_row.layout().setContentsMargins(14, 0, 0, 4)
            self.pad_row.slider.valueChanged.connect(self._on_padding)
            layout.addWidget(self.pad_row)

            self.upscale_check = Switch("Upscale small images")
            self.upscale_check.setToolTip(
                "Images under 512px trace with chunky, stair-stepped edges.\n"
                "Resampling them up first gives the tracer smoother edges to follow."
            )
            self.upscale_check.toggled.connect(self._on_option("upscale"))
            layout.addWidget(self.upscale_check)
            layout.addStretch(1)

            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setWidget(content)
            outer.addWidget(scroll, 1)

            footer = QWidget()
            footer.setObjectName("sidebarFooter")
            footer_layout = QHBoxLayout(footer)
            footer_layout.setContentsMargins(20, 10, 20, 12)
            reset = QPushButton("Reset to Defaults")
            reset.setObjectName("link")
            reset.setCursor(Qt.CursorShape.PointingHandCursor)
            reset.clicked.connect(self._reset_settings)
            footer_layout.addWidget(reset)
            footer_layout.addStretch(1)
            footer_layout.addWidget(label(f"v{APP_VERSION}", "caption"))
            outer.addWidget(footer)
            return sidebar

        def _build_actions(self) -> None:
            bindings = [
                (QKeySequence.StandardKey.Open, self.choose_file),
                (QKeySequence.StandardKey.Save, self.save_as),
                (QKeySequence.StandardKey.SaveAs, self.save_as),
                (QKeySequence("Ctrl+B"), self.batch_convert),
                (QKeySequence.StandardKey.Quit, self.close),
            ]
            for shortcut, slot in bindings:
                action = QAction(self)
                action.setShortcut(shortcut)
                action.triggered.connect(slot)
                self.addAction(action)

        def _about(self) -> None:
            backend = "available" if potrace_available() else "not installed"
            QMessageBox.about(
                self, f"About {APP_NAME}",
                f"<h3 style='margin-bottom:4px'>{APP_NAME}</h3>"
                f"<p style='color:{theme['secondary']}'>Version {APP_VERSION}</p>"
                "<p>Turns raster images into clean SVG vector graphics.</p>"
                "<p>Tracing by vtracer (color, B&amp;W) and "
                f"potrace ({backend}).</p>",
            )

        # -- settings plumbing -------------------------------------------------------

        def _load_widgets_from_settings(self) -> None:
            """Push self.settings into every control without triggering traces."""
            self._loading = True
            self.mode_control.set_index(MODES.index(self.settings.mode))
            for key, row in self.slider_rows.items():
                row.slider.setValue(int(getattr(self.settings, key)))
            self.bg_check.setChecked(self.settings.remove_bg)
            self.square_check.setChecked(self.settings.square)
            self.upscale_check.setChecked(self.settings.upscale)
            self.pad_row.slider.setValue(round(self.settings.padding * 100))
            self.pad_row.setVisible(self.settings.square)
            self._loading = False
            self._sync_mode_widgets()

        def _reset_settings(self) -> None:
            self.settings = Settings()
            self._loading = True
            self.preset_combo.setCurrentIndex(0)
            self._loading = False
            self._load_widgets_from_settings()
            self.request_trace()

        def _on_slider(self, key: str, value: int) -> None:
            setattr(self.settings, key, value)
            if not self._loading:
                self._mark_custom()
                self.request_trace()

        def _on_padding(self, value: int) -> None:
            self.settings.padding = value / 100.0
            if not self._loading and self.settings.square:
                self.request_trace()

        def _on_option(self, key: str):
            def handler(value: bool) -> None:
                setattr(self.settings, key, value)
                if key == "square":
                    self.pad_row.setVisible(value)
                if not self._loading:
                    self.request_trace()
            return handler

        def _on_mode_changed(self, index: int) -> None:
            self.settings.mode = MODES[index]
            self._sync_mode_widgets()
            if not self._loading:
                self.request_trace()

        def _on_preset_changed(self) -> None:
            name = self.preset_combo.currentText()
            if name == "Custom" or self._loading:
                return
            self.settings = apply_preset(self.settings, name)
            self._loading = True
            for key, row in self.slider_rows.items():
                row.slider.setValue(int(getattr(self.settings, key)))
            self._loading = False
            self.request_trace()

        def _mark_custom(self) -> None:
            if self.preset_combo.currentIndex() != 0:
                self._loading = True
                self.preset_combo.setCurrentIndex(0)
                self._loading = False

        def _sync_mode_widgets(self) -> None:
            """Show only the sliders that apply to the current backend."""
            mode = self.settings.mode
            for key in ("color_precision", "layer_difference"):
                self.slider_rows[key].setVisible(mode == MODE_COLOR)
            self.slider_rows["bw_threshold"].setVisible(mode == MODE_POTRACE)
            self.slider_rows["bg_tolerance"].setVisible(mode == MODE_SILHOUETTE)
            for key in ("line_detail", "line_weight"):
                self.slider_rows[key].setVisible(mode == MODE_LINES)
            self.bg_check.setVisible(mode != MODE_SILHOUETTE)  # it always removes it
            self.svg_canvas.set_light(mode != MODE_COLOR)  # black ink needs a light backdrop

            if mode == MODE_POTRACE and not potrace_available():
                self.notice.setText(
                    "potrace is not installed.<br>"
                    "Install it with your package manager, or pick another mode."
                )
                self.notice.show()
            else:
                self.notice.hide()

        # -- status ------------------------------------------------------------------

        def _set_status(self, text: str, error: bool = False) -> None:
            self.status_label.setText(text)
            self.status_label.setProperty("tone", "error" if error else "")
            self.status_label.style().unpolish(self.status_label)
            self.status_label.style().polish(self.status_label)

        def _set_chips(self, values: list[str] | None) -> None:
            for index, chip in enumerate(self.chips):
                if values and index < len(values):
                    chip.setText(values[index])
                    chip.show()
                else:
                    chip.hide()

        # -- file handling -----------------------------------------------------------

        def choose_file(self) -> None:
            start = str(self.image_path.parent) if self.image_path else str(Path.home())
            path, _ = QFileDialog.getOpenFileName(
                self, "Open Image", start,
                "Images (*.png *.jpg *.jpeg *.bmp *.webp *.gif *.tif *.tiff);;All files (*)",
            )
            if path:
                self.open_path(Path(path))

        def open_path(self, path: Path) -> None:
            if not path.is_file():
                QMessageBox.warning(self, APP_NAME, f"That file no longer exists:\n{path}")
                self._forget_recent(path)
                return
            reader = QImageReader(str(path))
            reader.setAutoTransform(True)  # honour EXIF rotation, as the tracer does
            pixmap = QPixmap.fromImage(reader.read())
            if pixmap.isNull():
                QMessageBox.warning(self, APP_NAME, f"Could not read that image:\n{path}")
                return
            self.image_path = path
            self.svg = None
            self.save_button.setEnabled(False)
            self.original_canvas.set_pixmap(pixmap)
            self.original_meta.setText(f"{pixmap.width()} × {pixmap.height()}")
            self._image_side = max(pixmap.width(), pixmap.height())
            self.svg_canvas.set_svg(None)
            self.svg_meta.setText("")
            self._set_chips(None)
            self.file_label.setText(path.name)
            self.setWindowTitle(f"{path.name} — {APP_NAME}")
            self._remember_recent(path)
            self._show_editor()
            self.request_trace()

        # -- pages and recent files --------------------------------------------------

        def go_home(self) -> None:
            self.home.set_recent(self._recent())
            self.pages.setCurrentWidget(self.home)
            for widget in self.editor_buttons:
                widget.hide()
            self.setWindowTitle(APP_NAME)

        def _show_editor(self) -> None:
            self.pages.setCurrentIndex(1)
            for widget in self.editor_buttons:
                widget.show()

        def _recent(self) -> list[Path]:
            stored = self._prefs.value("recent", [], type=list)
            return [Path(p) for p in stored if Path(p).is_file()][:RECENT_LIMIT]

        def _store_recent(self, paths: list[Path]) -> None:
            self._prefs.setValue("recent", [str(p) for p in paths])

        def _remember_recent(self, path: Path) -> None:
            path = path.resolve()
            if path.parent == SAMPLES_DIR:
                return  # samples have their own spot on the home page
            self._store_recent([path] + [p for p in self._recent() if p != path])

        def _forget_recent(self, path: Path) -> None:
            self._store_recent([p for p in self._recent() if p != path])
            if self.pages.currentWidget() is self.home:
                self.home.set_recent(self._recent())

        def _clear_recent(self) -> None:
            self._store_recent([])
            self.home.set_recent([])

        # -- tutorial ----------------------------------------------------------------

        def maybe_start_tour(self) -> None:
            if not self._prefs.value("tutorial_done", False, type=bool):
                self.start_tour()

        def start_tour(self) -> None:
            if self._tour is not None:
                return
            steps = [{
                "target": lambda: self.home.open_tile, "before": self.go_home,
                "title": f"Welcome to {APP_NAME}",
                "body": "VexTrace turns pictures into SVG vector graphics that stay sharp at "
                        "any size. Open your own image here, or drop one anywhere on the window.",
            }]
            samples = sample_images()
            if samples:
                sample = next((p for p in samples if p.stem == "harbour"), samples[0])
                steps += [{
                    "target": lambda: self.home.samples, "before": self.go_home,
                    "title": "Try a sample",
                    "body": "Not sure where to start? These samples come with the app. "
                            f"Press Next to open the {sample.stem} photo.",
                }, {
                    "target": lambda: self.compare,
                    "before": lambda: self._open_for_tour(sample),
                    "title": "Before and after",
                    "body": "Your original and the traced vector, next to each other. A quick "
                            "preview shows up first, then the full-quality trace replaces it.",
                }, {
                    "target": lambda: self.mode_control,
                    "title": "Pick a style",
                    "body": "Color keeps every colour. B&W and Potrace give black and white. "
                            "Silhouette makes one solid shape, and Lines draws the outlines.",
                }, {
                    "target": lambda: self.preset_combo,
                    "title": "Presets and sliders",
                    "body": "Start from a preset, then fine-tune with the sliders below it. "
                            "Hover over any setting to see what it does.",
                }, {
                    "target": lambda: self.chips_box,
                    "title": "Check the result",
                    "body": "Fewer paths means a cleaner, smaller file. Tens of paths is tidy. "
                            "Thousands usually means the speckle filter should go up.",
                }, {
                    "target": lambda: self.save_button,
                    "title": "Export",
                    "body": "Happy with it? Save the SVG with Export SVG or Ctrl+S. You can replay "
                            "this tour any time from the ••• menu.",
                }]
            self._tour = Tour(self.frame, steps, self._tour_done)

        def _open_for_tour(self, sample: Path) -> None:
            if self.image_path != sample:
                self.open_path(sample)
            else:
                self._show_editor()

        def _tour_done(self) -> None:
            self._tour = None
            self._prefs.setValue("tutorial_done", True)

        def resizeEvent(self, event):
            super().resizeEvent(event)
            if self._tour is not None:
                self._tour.relayout()

        # -- window ------------------------------------------------------------------

        def toggle_maximized(self) -> None:
            self.showNormal() if self.isMaximized() else self.showMaximized()

        def _sync_frame(self) -> None:
            margin = 0 if self.isMaximized() else RESIZE_MARGIN
            self.frame.layout().setContentsMargins(margin, margin, margin, margin)
            self.window_buttons[1].setToolTip("Restore" if self.isMaximized() else "Maximize")
            self.frame.update()

        def changeEvent(self, event):
            if event.type() == QEvent.Type.WindowStateChange:
                self._sync_frame()
            super().changeEvent(event)

        def dragEnterEvent(self, event):
            if event.mimeData().hasUrls():
                event.acceptProposedAction()
                self.home.set_active(True)

        def dragLeaveEvent(self, event):
            self.home.set_active(False)

        def dropEvent(self, event):
            self.home.set_active(False)
            for url in event.mimeData().urls():
                if url.isLocalFile():
                    self.open_path(Path(url.toLocalFile()))
                    event.acceptProposedAction()
                    return

        # -- tracing -----------------------------------------------------------------

        def request_trace(self) -> None:
            """Queue a re-render; repeated calls collapse into one."""
            if self.image_path is None:
                return
            self._set_status("Tracing…")
            self.spinner.start()
            self.svg_canvas.set_dimmed(True)
            self.save_button.setEnabled(False)  # until the full-quality trace is in
            self._debounce.start()

        def _start_trace(self) -> None:
            if self.image_path is None:
                return
            self._token += 1
            self._refine = False
            if self._worker is not None:
                # A running trace can't be interrupted. Start the newest request
                # when it finishes instead of piling up threads.
                self._pending = True
                return
            self._launch(draft=self._needs_draft())

        def _needs_draft(self) -> bool:
            """Big images get a quick low-resolution pass first, then full quality."""
            limit = self.settings.max_side or self._image_side
            return min(self._image_side, limit) > DRAFT_SIDE * 1.4

        def _launch(self, draft: bool) -> None:
            settings = replace(self.settings)
            if draft:
                settings.max_side = DRAFT_SIDE
            worker = TraceWorker(self._token, self.image_path, settings, draft)
            worker.finished_ok.connect(self._on_trace_done)
            worker.failed.connect(self._on_trace_failed)
            worker.finished.connect(self._on_worker_finished)
            self._worker = worker
            worker.start()

        def _on_worker_finished(self) -> None:
            self._worker.deleteLater()
            self._worker = None
            if self._pending:
                self._pending = False
                self._start_trace()
            elif self._refine:
                self._refine = False
                self._launch(draft=False)

        def _is_stale(self, token: int) -> bool:
            return token != self._token or self._pending or self._debounce.isActive()

        def _on_trace_done(self, token: int, svg: bytes, elapsed: float, draft: bool) -> None:
            if self._is_stale(token):
                return  # a newer trace is on its way
            self.svg_canvas.set_svg(svg)
            self.svg_canvas.set_dimmed(False)
            paths = count_paths(svg)
            path_text = f"{paths} path{'s' if paths != 1 else ''}"
            self.svg_meta.setText(path_text)
            if draft:
                # Show the quick pass now; the full-quality trace follows.
                self._refine = True
                self._set_status("Refining…")
                return
            self.svg = svg
            self.spinner.stop()
            self.save_button.setEnabled(True)
            self._set_status(f"Traced in {elapsed:.2f} s")
            self._set_chips([
                MODE_LABELS[self.settings.mode].replace("&&", "&"),
                path_text,
                human_size(len(svg)),
            ])

        def _on_trace_failed(self, token: int, message: str) -> None:
            if self._is_stale(token):
                return
            self.spinner.stop()
            self.svg_canvas.set_dimmed(False)
            self._set_status(message.splitlines()[0] if message else "Trace failed", error=True)

        # -- output ------------------------------------------------------------------

        def save_as(self) -> None:
            if self.svg is None or self.image_path is None or not self.save_button.isEnabled():
                return  # nothing traced yet, or the full-quality trace is still running
            suggested = str(default_output_for(self.image_path))
            path, _ = QFileDialog.getSaveFileName(
                self, "Export SVG", suggested, "SVG files (*.svg);;All files (*)")
            if not path:
                return
            if not path.lower().endswith(".svg"):
                path += ".svg"
            try:
                Path(path).write_bytes(self.svg)
            except OSError as exc:
                QMessageBox.critical(self, APP_NAME, f"Could not save:\n{exc}")
                return
            self._set_status(f"Saved {Path(path).name}")

        def batch_convert(self) -> None:
            if self._batch is not None:
                return
            in_dir = QFileDialog.getExistingDirectory(
                self, "Folder of images to convert", str(Path.home()))
            if not in_dir:
                return
            files = list(iter_images(Path(in_dir)))
            if not files:
                QMessageBox.information(self, APP_NAME, "No supported images in that folder.")
                return

            out_dir = QFileDialog.getExistingDirectory(
                self, "Where should the SVGs go?", in_dir)
            if not out_dir:
                return

            dialog = QProgressDialog("Converting…", "Cancel", 0, len(files), self)
            dialog.setWindowTitle(f"{APP_NAME} — Batch")
            dialog.setWindowModality(Qt.WindowModality.WindowModal)
            dialog.setMinimumDuration(0)
            dialog.setMinimumWidth(380)

            worker = BatchWorker(files, Path(out_dir), replace(self.settings))
            self._batch = worker
            dialog.canceled.connect(worker.cancel)
            worker.progressed.connect(
                lambda i, name: (dialog.setValue(i), dialog.setLabelText(f"Converting {name}"))
            )
            worker.done.connect(lambda ok, total, errs: self._batch_done(dialog, ok, total, errs))
            worker.finished.connect(worker.deleteLater)
            worker.start()

        def _batch_done(self, dialog, ok: int, total: int, errors: list[str]) -> None:
            dialog.setValue(total)
            dialog.close()
            self._batch = None
            summary = f"Converted {ok} of {total} image(s)."
            if errors:
                summary += "\n\nProblems:\n" + "\n".join(errors[:10])
                if len(errors) > 10:
                    summary += f"\n…and {len(errors) - 10} more."
            QMessageBox.information(self, f"{APP_NAME} — Batch", summary)
            self._set_status(f"Batch finished: {ok}/{total}")

        def closeEvent(self, event):
            self._token += 1  # invalidate anything still running
            if self._batch is not None:
                self._batch.cancel()
                self._batch.wait(3000)
            self._pending = self._refine = False
            if self._worker is not None:
                self._worker.wait()
            super().closeEvent(event)

    # -- application & theme -------------------------------------------------------

    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setDesktopFileName("vextrace")
    # Fusion is the one built-in style that honours style sheets completely, so the
    # look is identical on Plasma, GNOME and everything else.
    app.setStyle("Fusion")

    font = QFont()
    font.setFamilies(["SF Pro Text", "Inter", "Noto Sans", "Cantarell", "Sans Serif"])
    font.setPointSizeF(10)
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    app.setFont(font)

    icon = _find_icon()
    if icon:
        app.setWindowIcon(QIcon(str(icon)))

    def is_dark() -> bool:
        forced = os.environ.get("VEXTRACE_THEME", "").strip().lower()
        if forced in ("dark", "light"):
            return forced == "dark"
        scheme = app.styleHints().colorScheme()
        if scheme != Qt.ColorScheme.Unknown:
            return scheme == Qt.ColorScheme.Dark
        return app.palette().color(QPalette.ColorRole.Window).lightness() < 128

    def apply_theme() -> None:
        theme.clear()
        theme.update(THEME_DARK if is_dark() else THEME_LIGHT)

        chevron = Path(theme_dir.name) / f"chevron-{theme['secondary'][1:]}.svg"
        chevron.write_text(CHEVRON_SVG.format(color=theme["secondary"]))

        palette = QPalette()
        roles = {
            QPalette.ColorRole.Window: "window", QPalette.ColorRole.WindowText: "text",
            QPalette.ColorRole.Base: "card", QPalette.ColorRole.AlternateBase: "control",
            QPalette.ColorRole.Text: "text", QPalette.ColorRole.Button: "control",
            QPalette.ColorRole.ButtonText: "text", QPalette.ColorRole.Highlight: "accent",
            QPalette.ColorRole.HighlightedText: "on_accent", QPalette.ColorRole.ToolTipBase: "card",
            QPalette.ColorRole.ToolTipText: "text", QPalette.ColorRole.Link: "accent",
            QPalette.ColorRole.PlaceholderText: "tertiary", QPalette.ColorRole.Mid: "separator",
        }
        for role, token in roles.items():
            palette.setColor(role, color(token))
        for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText,
                     QPalette.ColorRole.ButtonText):
            palette.setColor(QPalette.ColorGroup.Disabled, role, color("tertiary"))
        app.setPalette(palette)

        app.setStyleSheet(Template(STYLESHEET).substitute(theme, chevron=chevron.as_posix()))
        for widget in app.allWidgets():
            widget.update()

    apply_theme()
    app.styleHints().colorSchemeChanged.connect(lambda _scheme: apply_theme())

    window = MainWindow()
    window.show()
    QTimer.singleShot(400, window.maybe_start_tour)
    try:
        return app.exec()
    finally:
        theme_dir.cleanup()


SAMPLES_DIR = Path(__file__).resolve().parent / "assets" / "samples"


def sample_images() -> list[Path]:
    """The sample images that ship with the app, if they're installed."""
    if not SAMPLES_DIR.is_dir():
        return []
    return [p for p in sorted(SAMPLES_DIR.iterdir()) if p.suffix.lower() in SUPPORTED_SUFFIXES]


def _find_icon() -> Path | None:
    candidates = [
        Path(__file__).resolve().parent / "assets" / "vextrace.svg",
        Path.home() / ".local/share/icons/hicolor/scalable/apps/vextrace.svg",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vextrace",
        description="Convert raster images into clean SVG vector graphics.",
        epilog="Run with no arguments to open the GUI.",
    )
    parser.add_argument("input", nargs="?", help="image file, or a folder with --batch")
    parser.add_argument("-o", "--output", help="output .svg (or output folder with --batch)")
    parser.add_argument("--mode", choices=MODES, default=MODE_COLOR,
                        help="tracing mode (default: color)")
    parser.add_argument("--preset", help="logo, illustration or photo")
    parser.add_argument("--batch", action="store_true",
                        help="treat the input as a folder and convert every image in it")
    parser.add_argument("--gui", action="store_true", help="force the GUI open")

    tuning = parser.add_argument_group("tuning")
    tuning.add_argument("--filter-speckle", type=int, metavar="N")
    tuning.add_argument("--color-precision", type=int, metavar="N")
    tuning.add_argument("--layer-difference", type=int, metavar="N")
    tuning.add_argument("--corner-threshold", type=int, metavar="N")
    tuning.add_argument("--path-precision", type=int, metavar="N")
    tuning.add_argument("--bw-threshold", type=int, metavar="N",
                        help="luminance split point for potrace mode (0-255)")
    tuning.add_argument("--bg-tolerance", type=int, metavar="N",
                        help="background match for --remove-bg and silhouette mode (0-128)")
    tuning.add_argument("--line-detail", type=int, metavar="N",
                        help="lines mode: higher draws fainter edges too (0-100)")
    tuning.add_argument("--line-weight", type=int, metavar="N",
                        help="lines mode: line thickness (1-6)")
    tuning.add_argument("--remove-bg", action="store_true",
                        help="make the dominant edge colour transparent")
    tuning.add_argument("--square", action="store_true",
                        help="force a square 1:1 viewBox")
    tuning.add_argument("--padding", type=float, metavar="PCT", default=5.0,
                        help="padding for --square, in percent (default: 5)")
    tuning.add_argument("--upscale", action="store_true",
                        help="enlarge images under 512px before tracing")
    tuning.add_argument("--max-size", type=int, metavar="PX", default=MAX_SIDE,
                        help=f"shrink larger images to this many pixels on the long "
                             f"side before tracing; 0 keeps full size (default: {MAX_SIDE})")

    parser.add_argument("--version", action="version", version=f"{APP_NAME} {APP_VERSION}")
    return parser


def settings_from_args(args: argparse.Namespace) -> Settings:
    settings = Settings(mode=args.mode)

    if args.preset:
        try:
            settings = apply_preset(settings, args.preset)
        except KeyError:
            names = ", ".join(sorted(PRESET_ALIASES))
            raise SystemExit(
                f"vextrace: unknown preset '{args.preset}' (try: {names})"
            ) from None

    # Explicit flags win over the preset.
    for key in ("filter_speckle", "color_precision", "layer_difference",
                "corner_threshold", "path_precision", "bw_threshold",
                "bg_tolerance", "line_detail", "line_weight"):
        value = getattr(args, key)
        if value is not None:
            setattr(settings, key, value)

    settings.remove_bg = args.remove_bg
    settings.square = args.square
    settings.padding = max(0.0, args.padding) / 100.0
    settings.upscale = args.upscale
    settings.max_side = max(0, args.max_size)
    return settings


def run_cli(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)
    source = Path(args.input).expanduser()

    if args.batch:
        if not source.is_dir():
            print(f"vextrace: not a folder: {source}", file=sys.stderr)
            return 2
        out_dir = Path(args.output).expanduser() if args.output else source
        out_dir.mkdir(parents=True, exist_ok=True)

        files = list(iter_images(source))
        if not files:
            print(f"vextrace: no supported images in {source}", file=sys.stderr)
            return 1

        failures = 0
        used: set[Path] = set()
        for path in files:
            target = unique_target(out_dir, path.stem, used)
            try:
                svg = vectorize(path, settings)
                target.write_bytes(svg)
            except (TraceError, OSError, ValueError) as exc:
                print(f"  {path.name}: FAILED - {exc}", file=sys.stderr)
                failures += 1
            else:
                print(f"  {path.name} -> {target.name} "
                      f"({count_paths(svg)} paths, {human_size(len(svg))})")
        print(f"Converted {len(files) - failures} of {len(files)} image(s) into {out_dir}")
        return 1 if failures else 0

    if not source.is_file():
        print(f"vextrace: no such file: {source}", file=sys.stderr)
        return 2

    target = Path(args.output).expanduser() if args.output else default_output_for(source)
    try:
        svg = vectorize(source, settings)
    except (TraceError, OSError, ValueError) as exc:
        print(f"vextrace: {exc}", file=sys.stderr)
        return 1

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(svg)
    except OSError as exc:
        print(f"vextrace: could not write {target}: {exc}", file=sys.stderr)
        return 1

    print(f"{source.name} -> {target} ({count_paths(svg)} paths, {human_size(len(svg))})")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.gui or args.input is None:
        initial = Path(args.input).expanduser() if args.input else None
        if initial is not None and not initial.is_file():
            initial = None
        return run_gui(initial)

    return run_cli(args)


if __name__ == "__main__":
    sys.exit(main())
