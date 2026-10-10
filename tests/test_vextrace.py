"""Tests for the tracing pipeline and CLI. Run with:  python -m pytest"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import vextrace as vt

SVG = "{http://www.w3.org/2000/svg}"


@pytest.fixture
def logo(tmp_path: Path) -> Path:
    """A flat two-colour image on a white background."""
    img = Image.new("RGB", (240, 160), "white")
    draw = ImageDraw.Draw(img)
    draw.ellipse((20, 20, 120, 140), fill="#e74c3c")
    draw.rectangle((140, 40, 220, 120), fill="#2c3e50")
    path = tmp_path / "logo.png"
    img.save(path)
    return path


def viewbox(svg: bytes) -> list[float]:
    root = ET.fromstring(svg.decode().split("\n", 1)[1])
    return [float(v) for v in root.get("viewBox").split()]


def test_color_trace_produces_svg_with_viewbox(logo: Path):
    svg = vt.vectorize(logo, vt.Settings())
    assert svg.startswith(b"<?xml")
    assert viewbox(svg) == [0, 0, 240, 160]
    assert vt.count_paths(svg) >= 2


def test_square_viewbox_pads_and_centres(logo: Path):
    svg = vt.vectorize(logo, vt.Settings(square=True, padding=0.1))
    x, y, w, h = viewbox(svg)
    assert (x, y) == (0, 0)
    assert w == h == pytest.approx(240 * 1.2)


def test_remove_background_drops_the_backdrop(logo: Path):
    plain = vt.count_paths(vt.vectorize(logo, vt.Settings()))
    cleaned = vt.count_paths(vt.vectorize(logo, vt.Settings(remove_bg=True)))
    assert cleaned < plain


@pytest.mark.skipif(not vt.potrace_available(), reason="potrace not installed")
def test_potrace_mode(logo: Path):
    svg = vt.vectorize(logo, vt.Settings(mode=vt.MODE_POTRACE))
    assert vt.count_paths(svg) >= 1


def test_presets_and_aliases():
    s = vt.apply_preset(vt.Settings(), "logo")
    assert s.layer_difference == vt.PRESETS["Logo / Flat art"]["layer_difference"]
    with pytest.raises(KeyError):
        vt.apply_preset(vt.Settings(), "nonsense")


def test_unique_target_avoids_collisions(tmp_path: Path):
    used: set[Path] = set()
    assert vt.unique_target(tmp_path, "logo", used).name == "logo.svg"
    assert vt.unique_target(tmp_path, "logo", used).name == "logo-2.svg"


def test_human_size():
    assert vt.human_size(512) == "512 B"
    assert vt.human_size(2048) == "2.0 KB"


def test_cli_single_and_batch(logo: Path, tmp_path: Path):
    out = tmp_path / "out.svg"
    assert vt.main([str(logo), "-o", str(out), "--preset", "logo"]) == 0
    assert out.read_bytes().startswith(b"<?xml")

    batch_dir = tmp_path / "svgs"
    assert vt.main([str(logo.parent), "--batch", "-o", str(batch_dir)]) == 0
    assert (batch_dir / "logo.svg").is_file()


def test_large_images_are_traced_small_but_keep_their_size(tmp_path: Path):
    path = tmp_path / "big.png"
    Image.new("RGB", (4000, 3000), "white").save(path)
    svg = vt.vectorize(path, vt.Settings())
    root = ET.fromstring(svg.decode().split("\n", 1)[1])
    assert viewbox(svg)[2] == vt.MAX_SIDE
    assert (root.get("width"), root.get("height")) == ("4000", "3000")


def test_exif_rotation_is_applied(tmp_path: Path):
    path = tmp_path / "sideways.jpg"
    exif = Image.Exif()
    exif[0x0112] = 6  # stored sideways: rotate 90° clockwise to view
    Image.new("RGB", (300, 200), "white").save(path, exif=exif)
    assert vt.load_image(path).size == (200, 300)


def test_silhouette_fills_the_subject_and_drops_the_background(tmp_path: Path):
    img = Image.new("RGBA", (200, 200), "white")
    ImageDraw.Draw(img).ellipse((40, 40, 160, 160), outline="#e74c3c", width=12)  # a ring
    ink = vt.silhouette(img, 24)
    assert ink.getpixel((100, 100))[:3] == (0, 0, 0)  # the hole is filled in
    assert ink.getpixel((5, 5))[:3] == (255, 255, 255)  # the background is gone
    path = tmp_path / "ring.png"
    img.save(path)
    assert vt.count_paths(vt.vectorize(path, vt.Settings(mode=vt.MODE_SILHOUETTE))) >= 1


def test_lines_trace_edges_not_areas(logo: Path):
    img = vt.load_image(logo)
    ink = vt.line_drawing(img, detail=75, weight=1)
    assert ink.getpixel((70, 80))[:3] == (255, 255, 255)  # inside the red circle: empty
    assert any(ink.getpixel((x, 80))[:3] == (0, 0, 0) for x in range(116, 126))  # its edge
    assert vt.count_paths(vt.vectorize(logo, vt.Settings(mode=vt.MODE_LINES))) >= 1


def test_cli_new_modes(logo: Path, tmp_path: Path):
    for mode in ("silhouette", "lines"):
        out = tmp_path / f"{mode}.svg"
        assert vt.main([str(logo), "-o", str(out), "--mode", mode, "--line-weight", "3"]) == 0
        assert vt.count_paths(out.read_bytes()) >= 1
