"""DataBridge branding for the web client: loading screen, favicon and app icons instead of Flet's.

Flet serves files from the app's assets_dir before its own web files, so placing our PNGs under the same names
(favicon.png, icons/loading-animation.png, icons/icon-*.png) replaces the Flet logo. The loading page itself is
the installed Flet's index.html with one change: the splash image becomes a <picture> with a dark-mode variant
and a "Loading DataBridge" caption. Built at startup from the installed Flet, so a Flet upgrade keeps working; if
its markup ever changes, the page is left as is and only the images are replaced.
"""

import json
import logging
import shutil
from pathlib import Path

log = logging.getLogger("databridge.web_assets")
BRANDING = Path(__file__).resolve().parent / "branding"
GUIDE_IMAGES = Path(__file__).resolve().parents[1] / "docs" / "guide" / "img"  # served as guide/<name>.png
SPLASH_IMG = '<img src="icons/loading-animation.png" alt="" />'
SPLASH = """<picture>
      <source srcset="icons/loading-dark.png" media="(prefers-color-scheme: dark)" />
      <img src="icons/loading-animation.png" alt="DataBridge" />
    </picture>
    <div class="db-loading-text">Loading DataBridge<span class="db-bar"><span></span></span></div>"""
SPLASH_CSS = """
      #loading { flex-direction: column; gap: 28px; font-family: Roboto, "Segoe UI", Arial, sans-serif; }
      #loading img { width: 260px; max-width: 60vw; }
      .db-loading-text { display: flex; flex-direction: column; align-items: center; gap: 10px;
                         font-size: 13px; letter-spacing: .3px; color: #5f5f6b; }
      .db-bar { position: relative; display: block; width: 160px; height: 3px; border-radius: 2px;
                background: rgba(81, 91, 146, .18); overflow: hidden; }
      .db-bar span { position: absolute; top: 0; left: -40%; width: 40%; height: 100%; border-radius: 2px;
                     background: #515b92; animation: db-slide 1.2s ease-in-out infinite; }
      @keyframes db-slide { from { left: -40%; } to { left: 100%; } }
      @media (prefers-color-scheme: dark) {
        .db-loading-text { color: #c7c5d0; }
        .db-bar { background: rgba(186, 195, 255, .2); }
        .db-bar span { background: #bac3ff; }
      }
    </style>"""


def _flet_index() -> Path | None:
    try:
        import flet_web
    except ImportError:
        return None
    path = Path(flet_web.__file__).parent / "web" / "index.html"
    return path if path.is_file() else None


def branded_index(html: str) -> str | None:
    """Flet's index.html with the DataBridge splash, or None if the markup isn't the one we know."""
    if SPLASH_IMG not in html or "</style>" not in html.split(SPLASH_IMG)[0]:
        return None
    head, tail = html.split(SPLASH_IMG, 1)
    style_end = head.rfind("</style>")  # the loading screen's own <style>, just above the image
    head = head[:style_end] + SPLASH_CSS + head[style_end + len("</style>"):]
    return head + SPLASH + tail


def prepare(target: Path) -> Path:
    """Writes the branded web assets into `target` (used as Flet's assets_dir) and returns it."""
    target.mkdir(parents=True, exist_ok=True)
    shutil.copytree(BRANDING, target, dirs_exist_ok=True)
    if GUIDE_IMAGES.is_dir():  # diagrams in the user guide (PNG only; the SVG sources stay in the package)
        (target / "guide").mkdir(exist_ok=True)
        for png in GUIDE_IMAGES.glob("*.png"):
            shutil.copy2(png, target / "guide" / png.name)
    index = target / "index.html"
    src = _flet_index()
    html = branded_index(src.read_text(encoding="utf-8")) if src else None
    if html:
        index.write_text(html, encoding="utf-8")
    else:
        index.unlink(missing_ok=True)  # fall back to Flet's page (images are still ours)
        log.warning("Flet's loading page changed; using it with DataBridge images only")
    manifest = src.parent / "manifest.json" if src else None
    if manifest and manifest.is_file():  # browser/installed-app colours: DataBridge indigo, not Flet pink
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data.update(name="DataBridge", short_name="DataBridge", theme_color="#515B92", background_color="#FFFFFF")
        (target / "manifest.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    return target
