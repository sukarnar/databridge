"""DataBridge branding replaces Flet's loading logo, favicon and app icons."""

import json
import struct

from databridge.ui import web_assets


def _png_size(path) -> tuple[int, int]:
    """Width and height from the PNG header (no imaging library needed)."""
    head = path.read_bytes()[:24]
    assert head[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", head[16:24])


def test_branded_assets(tmp_path):
    out = web_assets.prepare(tmp_path / "web")
    for name in ("favicon.png", "icons/loading-animation.png", "icons/loading-dark.png", "icons/icon-192.png",
                 "icons/icon-512.png", "icons/icon-maskable-192.png", "icons/icon-maskable-512.png",
                 "icons/apple-touch-icon-192.png"):
        assert (out / name).is_file(), name
    assert _png_size(out / "icons/icon-512.png") == (512, 512)
    html = (out / "index.html").read_text()
    assert 'media="(prefers-color-scheme: dark)"' in html and "Loading DataBridge" in html
    assert html.count("<style>") == html.count("</style>")  # still well-formed around our CSS
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["theme_color"] == "#515B92" and manifest["name"] == "DataBridge"


def test_unknown_flet_markup_is_left_alone():
    assert web_assets.branded_index("<html><body>something else</body></html>") is None


def test_guide_diagrams_are_served(tmp_path):
    out = web_assets.prepare(tmp_path / "web")
    for name in ("architecture-overview", "architecture-data-flow", "architecture-ai", "architecture-deployment"):
        assert (out / "guide" / f"{name}.png").is_file(), name
