"""File uploads from the studio over HTTPS, not over the studio websocket.

The browser PUTs the picked file to Flet's upload endpoint (/upload) with a signed URL that expires; the file is
streamed to a private folder, read once and deleted. Keeping files off the websocket lets the websocket accept
only small messages (see web_security.STUDIO_MAX_MESSAGE), so nobody can exhaust memory by sending huge
websocket frames before signing in, and large uploads get the normal HTTP size limits at the proxy.
"""

import asyncio
import logging
import re
import secrets
import shutil
import uuid
from pathlib import Path

import flet as ft

from databridge.config import settings

log = logging.getLogger("databridge.uploads")
UPLOAD_DIR = (settings.data_dir / "studio-uploads").resolve()
# Signs upload URLs. New on every start: URLs are only valid for minutes and only for this process.
SECRET = secrets.token_urlsafe(32)
URL_SECONDS = 900
WAIT_SECONDS = 1800


def prepare() -> str:
    """Empties the upload folder (leftovers of interrupted uploads) and returns it for Flet's upload_dir."""
    shutil.rmtree(UPLOAD_DIR, ignore_errors=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    return str(UPLOAD_DIR)


def _safe_name(name: str) -> str:
    base = Path(name.replace("\\", "/")).name
    return re.sub(r"[^A-Za-z0-9._ -]", "_", base)[:120] or "upload"


async def pick_and_upload(page: ft.Page, extensions: list[str], max_bytes: int,
                          dialog_title: str = "Choose a file") -> tuple[str, bytes] | None:
    """Opens the file picker, uploads the chosen file over HTTPS and returns (file name, content).

    Returns None if the user cancelled. Raises ValueError with a readable message if the file is too large or
    the upload failed.
    """
    done = asyncio.Event()
    outcome: dict[str, str] = {}

    async def on_upload(e: ft.FilePickerUploadEvent):
        if e.error:
            outcome["error"] = e.error
            done.set()
        elif e.progress is not None and e.progress >= 1.0:
            done.set()

    # The handler must be given at construction: that is when the picker registers with the browser.
    picker = ft.FilePicker(on_upload=on_upload)
    files = await picker.pick_files(dialog_title=dialog_title, allow_multiple=False,
                                    file_type=ft.FilePickerFileType.CUSTOM,
                                    allowed_extensions=[e.lstrip(".") for e in extensions])
    if not files:
        return None
    f = files[0]
    if f.size > max_bytes:
        limit = f"{max_bytes / 1024 / 1024:.0f} MB" if max_bytes >= 1024 * 1024 else f"{max_bytes // 1024} KB"
        raise ValueError(f"{f.name} is larger than {limit}")
    folder = uuid.uuid4().hex
    rel = f"{folder}/{_safe_name(f.name)}"
    url = page.get_upload_url(rel, URL_SECONDS)
    await picker.upload([ft.FilePickerUploadFile(upload_url=url, method="PUT", id=f.id, name=f.name)])
    try:
        await asyncio.wait_for(done.wait(), WAIT_SECONDS)
    except asyncio.TimeoutError:
        raise ValueError(f"Upload of {f.name} did not finish") from None
    path = UPLOAD_DIR / rel
    try:
        if "error" in outcome:
            raise ValueError(f"Upload of {f.name} failed: {outcome['error']}")
        for _ in range(50):  # the last progress event can arrive just before the file is closed
            if path.is_file() and path.stat().st_size >= f.size:
                break
            await asyncio.sleep(0.1)
        if not path.is_file():
            raise ValueError(f"Upload of {f.name} failed")
        data = path.read_bytes()
        if len(data) > max_bytes:
            raise ValueError(f"{f.name} is too large")
        return f.name, data
    finally:
        shutil.rmtree(UPLOAD_DIR / folder, ignore_errors=True)
