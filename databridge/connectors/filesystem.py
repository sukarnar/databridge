"""File system connector built on fsspec: local/mounted paths, SMB, SFTP, S3, Azure, GCS."""

import fnmatch
import json
from collections.abc import Iterator
from typing import Any, Literal, Optional

import fsspec
import polars as pl
from pydantic import BaseModel, Field, field_validator, model_validator

from databridge.connectors.base import Connector, Node, TestResult
from databridge.ingest.sheet_profile import (
    SPREADSHEET_EXT,
    SUPPORTED_EXT,
    SheetProfile,
    ext_of,
    list_sheets,
    parse_file,
)

# protocol -> pip package providing it (None = built in)
PROTOCOL_PACKAGES = {
    "file": None,
    "sftp": "sshfs",
    "smb": "smbprotocol",
    "s3": "s3fs",
    "abfs": "adlfs",
    "gcs": "gcsfs",
    "ftp": None,
}


class FileSystemConfig(BaseModel):
    protocol: Literal["file", "smb", "sftp", "ftp", "s3", "abfs", "gcs"] = Field(
        "file", description="Storage type")
    root: str = Field(..., description="Folder (or one file) on the DataBridge server, e.g. C:\\Data\\HR or /data/exports; share/folder or bucket/prefix")
    host: Optional[str] = Field(None, description="Server host (SMB, SFTP, FTP)")
    port: Optional[int] = Field(None, description="Port (optional)")
    username: Optional[str] = Field(None, description="User name or access key")
    password: Optional[str] = Field(None, description="Password or secret key")
    file_pattern: str = Field("*", description="Files to show and watch, e.g. sales_*.xlsx")
    options_json: str = Field("{}", description="Extra fsspec options as JSON")

    @field_validator("root")
    @classmethod
    def _clean_root(cls, v: str) -> str:
        # Pasted paths: Windows "Copy as path" adds quotes; backslashes become / (fsspec's form on every OS)
        v = (v or "").strip().strip('"').strip("'").strip()
        if not v:
            raise ValueError("Enter the folder, e.g. C:\\Data\\Exports or /data/exports")
        return v.replace("\\", "/")

    @model_validator(mode="after")
    def _file_as_root(self):
        """Root pointing at one file (a common first try): watch that file in its folder."""
        if self.protocol == "file":
            from pathlib import Path

            p = Path(self.root)
            if p.is_file():
                if self.file_pattern in ("", "*"):
                    self.file_pattern = p.name
                self.root = p.parent.as_posix()
        return self


class FileSystemConnector(Connector):
    type_name = "filesystem"
    label = "File system (local, SMB, SFTP, S3, Azure, GCS)"
    config_model = FileSystemConfig
    secret_fields = {"password"}
    capabilities = {"browse", "preview", "watch"}

    def __init__(self, config: dict[str, Any], secrets: dict[str, Any] | None = None):
        super().__init__(config, secrets)
        self.cfg: FileSystemConfig
        self._fs = None

    # -------------------------------------------------------------- fs plumbing

    @property
    def fs(self):
        if self._fs is None:
            c = self.cfg
            opts: dict[str, Any] = json.loads(c.options_json or "{}")
            if c.protocol in {"smb", "sftp", "ftp"}:
                opts.setdefault("host", c.host)
                if c.port:
                    opts.setdefault("port", c.port)
                if c.username:
                    opts.setdefault("username", c.username)
                if c.password:
                    opts.setdefault("password", c.password)
            elif c.protocol == "s3":
                if c.username:
                    opts.setdefault("key", c.username)
                if c.password:
                    opts.setdefault("secret", c.password)
            elif c.protocol == "abfs":
                if c.username:
                    opts.setdefault("account_name", c.username)
                if c.password:
                    opts.setdefault("account_key", c.password)
            self._fs = fsspec.filesystem(c.protocol, **opts)
        return self._fs

    def _full(self, path: str | None) -> str:
        root = self.cfg.root.replace("\\", "/").rstrip("/") or "/"
        if not path:
            return root
        path = path.replace("\\", "/")
        # fsspec returns full paths (on Windows "C:/Users/..."); compare without case on Windows drives
        if path.lower().startswith(root.lower()):
            return path
        return f"{root.rstrip('/')}/{path.lstrip('/')}"

    def _matches(self, name: str) -> bool:
        return fnmatch.fnmatch(name.lower(), (self.cfg.file_pattern or "*").lower())

    # -------------------------------------------------------------- contract

    def test(self) -> TestResult:
        try:
            entries = self.fs.ls(self._full(None), detail=False)
            return TestResult(True, f"Connected. {len(entries)} entries in {self.cfg.root}")
        except ImportError as e:
            pkg = PROTOCOL_PACKAGES.get(self.cfg.protocol)
            return TestResult(False, f"Driver missing: pip install {pkg} ({e})")
        except Exception as e:  # noqa: BLE001 - surface any backend error to the user
            return TestResult(False, f"{type(e).__name__}: {e}")

    def browse(self, ref: dict[str, Any] | None = None) -> list[Node]:
        ref = ref or {}
        if ref.get("path") and ext_of(ref["path"]) in SPREADSHEET_EXT and not ref.get("sheet"):
            content = self.read_bytes(ref)
            return [Node(s, "sheet", {"path": ref["path"], "sheet": s}) for s in list_sheets(content, ref["path"])]
        base = self._full(ref.get("path"))
        nodes: list[Node] = []
        for e in sorted(self.fs.ls(base, detail=True), key=lambda x: (x["type"] != "directory", x["name"])):
            name = e["name"].rstrip("/").split("/")[-1]
            if e["type"] == "directory":
                nodes.append(Node(name, "folder", {"path": e["name"]}, has_children=True))
            elif ext_of(name) in SUPPORTED_EXT and self._matches(name):
                size = e.get("size") or 0
                nodes.append(Node(name, "file", {"path": e["name"]},
                                  has_children=ext_of(name) in SPREADSHEET_EXT,
                                  detail=f"{size / 1024:.0f} KB"))
        return nodes

    def read_bytes(self, ref: dict[str, Any]) -> bytes:
        with self.fs.open(self._full(ref["path"]), "rb") as fh:
            return fh.read()

    def file_info(self, ref: dict[str, Any]) -> dict[str, Any]:
        return self.fs.info(self._full(ref["path"]))

    def _parse(self, ref: dict[str, Any], profile: dict[str, Any] | None = None):
        content = self.read_bytes(ref)
        prof = SheetProfile.from_dict(profile) if profile else None
        if prof is None and ref.get("sheet"):
            from databridge.ingest.sheet_profile import suggest_profile

            prof = suggest_profile(content, ref["path"], ref["sheet"])
        return parse_file(content, ref["path"], prof)

    def describe(self, ref: dict[str, Any]) -> list[dict[str, Any]]:
        return self._parse(ref).fields

    def preview(self, ref: dict[str, Any], limit: int = 100) -> pl.DataFrame:
        return self._parse(ref).df.head(limit)

    def read(self, ref: dict[str, Any], batch_size: int = 50_000) -> Iterator[pl.DataFrame]:
        df = self._parse(ref, ref.get("profile")).df
        for start in range(0, max(df.height, 1), batch_size):
            yield df.slice(start, batch_size)

    def list_matching(self, folder: str | None = None) -> list[dict[str, Any]]:
        """Files in `folder` matching file_pattern, oldest first (used by the folder watcher)."""
        base = self._full(folder)
        files = [e for e in self.fs.ls(base, detail=True)
                 if e["type"] != "directory" and self._matches(e["name"].split("/")[-1])]
        return sorted(files, key=lambda e: e.get("mtime") or e.get("LastModified") or 0)
