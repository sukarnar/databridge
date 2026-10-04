"""Sheet Profile: how to read a spreadsheet (or CSV) reliably, reused for every new file.

Handles header-row detection, junk rows (blank, "Total"), merged cells, multi-row headers,
duplicate or blank headers, and type inference with per-column overrides.
"""

import csv
import io
import re
from dataclasses import asdict, dataclass, field
from pathlib import PurePath
from typing import Any

import polars as pl

from databridge.ingest.infer import build_column

SPREADSHEET_EXT = {".xlsx", ".xlsm", ".xls", ".xlsb", ".ods"}
DELIMITED_EXT = {".csv", ".tsv", ".txt"}
STRUCTURED_EXT = {".json", ".parquet", ".jsonl", ".ndjson"}
SUPPORTED_EXT = SPREADSHEET_EXT | DELIMITED_EXT | STRUCTURED_EXT


@dataclass
class SheetProfile:
    sheet: str | None = None
    header_row: int = 1  # 1-based row number of the (first) header row; 0 = no header
    header_rows: int = 1  # rows spanned by a multi-row header
    skip_blank_rows: bool = True
    skip_patterns: list[str] = field(default_factory=lambda: ["total", "subtotal", "grand total"])
    fill_merged: bool = True
    type_overrides: dict[str, str] = field(default_factory=dict)
    last_column: int | None = None  # trim columns beyond this (1-based)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "SheetProfile":
        if not d:
            return cls()
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass
class ParseResult:
    df: pl.DataFrame
    fields: list[dict[str, Any]]
    notes: list[str]
    profile: SheetProfile


def ext_of(filename: str) -> str:
    return PurePath(filename).suffix.lower()


def column_letter(idx: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    s = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        s = chr(65 + rem) + s
    return s


# ---------------------------------------------------------------- raw grid readers


def list_sheets(content: bytes, filename: str) -> list[str]:
    ext = ext_of(filename)
    if ext in {".xlsx", ".xlsm"}:
        from openpyxl import load_workbook

        wb = load_workbook(io.BytesIO(content), read_only=True)
        try:
            return list(wb.sheetnames)
        finally:
            wb.close()
    if ext in SPREADSHEET_EXT:
        import fastexcel

        return list(fastexcel.read_excel(content).sheet_names)
    return ["(file)"]


def read_grid(content: bytes, filename: str, sheet: str | None = None, fill_merged: bool = True,
              max_rows: int | None = None) -> list[list[Any]]:
    """Reads raw cell values as a list of rows (no header handling, no typing)."""
    ext = ext_of(filename)
    if ext in {".xlsx", ".xlsm"}:
        return _grid_openpyxl(content, sheet, fill_merged, max_rows)
    if ext in SPREADSHEET_EXT:
        return _grid_fastexcel(content, sheet, max_rows)
    if ext in DELIMITED_EXT:
        return _grid_csv(content, max_rows)
    raise ValueError(f"No grid reader for {ext}")


def _grid_openpyxl(content: bytes, sheet: str | None, fill_merged: bool, max_rows: int | None):
    from openpyxl import load_workbook

    # Full (not read-only) mode is needed to see merged ranges.
    wb = load_workbook(io.BytesIO(content), data_only=True, read_only=not fill_merged)
    ws = wb[sheet] if sheet else wb.worksheets[0]
    if fill_merged and hasattr(ws, "merged_cells"):
        for rng in list(ws.merged_cells.ranges):
            value = ws.cell(rng.min_row, rng.min_col).value
            ws.unmerge_cells(str(rng))
            for r in range(rng.min_row, rng.max_row + 1):
                for c in range(rng.min_col, rng.max_col + 1):
                    ws.cell(r, c).value = value
    rows: list[list[Any]] = []
    for row in ws.iter_rows(values_only=True, max_row=max_rows):
        rows.append(list(row))
    wb.close()
    return _trim(rows)


def _grid_fastexcel(content: bytes, sheet: str | None, max_rows: int | None):
    import fastexcel

    reader = fastexcel.read_excel(content)
    name = sheet or reader.sheet_names[0]
    sh = reader.load_sheet(name, header_row=None, n_rows=max_rows)
    df = sh.to_polars()
    return _trim([list(r) for r in df.iter_rows()])


def _grid_csv(content: bytes, max_rows: int | None):
    text = content.decode("utf-8-sig", errors="replace")
    sample = text[:20000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = []
    for i, row in enumerate(csv.reader(io.StringIO(text), dialect)):
        if max_rows and i >= max_rows:
            break
        rows.append([c if c != "" else None for c in row])
    return _trim(rows)


def _trim(rows: list[list[Any]]) -> list[list[Any]]:
    """Drops fully empty trailing columns and pads ragged rows."""
    width = 0
    for r in rows:
        for i in range(len(r) - 1, -1, -1):
            if r[i] not in (None, ""):
                width = max(width, i + 1)
                break
    return [list(r[:width]) + [None] * (width - len(r[:width])) for r in rows]


# ---------------------------------------------------------------- detection


def _nonblank(row: list[Any]) -> int:
    return sum(1 for v in row if v not in (None, "") and str(v).strip() != "")


def detect_header_row(grid: list[list[Any]], scan: int = 30) -> int:
    """Returns the 1-based row number most likely to be the header (0 if none looks like one).

    Heuristic: the first row whose non-blank cells are mostly text, that is at least 60% as wide
    as the widest row in the scanned area, and is followed by a row that has data.
    """
    head = grid[:scan]
    if not head:
        return 0
    widest = max((_nonblank(r) for r in head), default=0)
    if widest == 0:
        return 0
    for i, row in enumerate(head):
        n = _nonblank(row)
        if n < max(2, 0.6 * widest) and widest >= 2:
            continue
        distinct = {str(v).strip() for v in row if v not in (None, "") and str(v).strip()}
        if widest >= 2 and len(distinct) < 2:
            continue  # a merged banner/title row repeats one value across columns
        texts = sum(1 for v in row if isinstance(v, str) and v.strip() and not _looks_numeric(v))
        if texts >= 0.7 * n and i + 1 < len(grid) and _nonblank(grid[i + 1]) > 0:
            return i + 1
    return 1


def _looks_numeric(s: str) -> bool:
    return bool(re.fullmatch(r"[-+$€£]?[\d,]+(\.\d+)?%?", s.strip()))


def suggest_profile(content: bytes, filename: str, sheet: str | None = None) -> SheetProfile:
    grid = read_grid(content, filename, sheet, fill_merged=True, max_rows=40)
    header = detect_header_row(grid)
    header_rows = 1
    # Two-row header: the row below the header is also mostly text and the header row has merged/repeated cells.
    if header and header < len(grid):
        below = grid[header] if header < len(grid) else []
        top = grid[header - 1]
        top_vals = [v for v in top if v not in (None, "")]
        repeated = 1 < len(set(top_vals)) < len(top_vals)
        below_text = _nonblank(below) and all(
            isinstance(v, str) and not _looks_numeric(v) for v in below if v not in (None, "")
        )
        if repeated and below_text:
            header_rows = 2
    sheets = list_sheets(content, filename) if ext_of(filename) in SPREADSHEET_EXT else [None]
    return SheetProfile(sheet=sheet or sheets[0], header_row=header, header_rows=header_rows)


# ---------------------------------------------------------------- apply


def _make_headers(grid: list[list[Any]], profile: SheetProfile, width: int) -> list[str]:
    if profile.header_row <= 0:
        return [f"column_{column_letter(i)}" for i in range(width)]
    start = profile.header_row - 1
    parts_rows = grid[start : start + profile.header_rows]
    names: list[str] = []
    for c in range(width):
        parts: list[str] = []
        for r in parts_rows:
            v = r[c] if c < len(r) else None
            s = str(v).strip() if v not in (None, "") else ""
            if s and (not parts or parts[-1] != s):
                parts.append(s)
        name = " ".join(parts) or f"column_{column_letter(c)}"
        names.append(re.sub(r"\s+", " ", name))
    seen: dict[str, int] = {}
    unique = []
    for n in names:
        key = n.lower()
        if key in seen:
            seen[key] += 1
            unique.append(f"{n}_{seen[key]}")
        else:
            seen[key] = 1
            unique.append(n)
    return unique


def _is_skip_row(row: list[Any], profile: SheetProfile) -> bool:
    if profile.skip_blank_rows and _nonblank(row) == 0:
        return True
    first = next((str(v).strip().lower() for v in row if v not in (None, "") and str(v).strip()), "")
    return any(first == p.lower() or first.startswith(p.lower() + " ") or first.startswith(p.lower() + ":")
               for p in profile.skip_patterns)


def parse_file(content: bytes, filename: str, profile: SheetProfile | None = None) -> ParseResult:
    """Applies a Sheet Profile and returns a typed DataFrame plus field metadata."""
    ext = ext_of(filename)
    if ext not in SUPPORTED_EXT:
        raise ValueError(f"Unsupported file type {ext}. Supported: {', '.join(sorted(SUPPORTED_EXT))}")
    if ext in STRUCTURED_EXT:
        df = _read_structured(content, ext)
        prof = profile or SheetProfile(sheet=None, header_row=1)
        from databridge.ingest.profiling import profile_fields

        return ParseResult(df=df, fields=profile_fields(df), notes=[], profile=prof)

    profile = profile or suggest_profile(content, filename)
    grid = read_grid(content, filename, profile.sheet, fill_merged=profile.fill_merged)
    width = max((len(r) for r in grid), default=0)
    if profile.last_column:
        width = min(width, profile.last_column)
        grid = [r[:width] for r in grid]
    headers = _make_headers(grid, profile, width)
    first_data = (profile.header_row - 1 + profile.header_rows) if profile.header_row > 0 else 0
    body = [r for r in grid[first_data:] if not _is_skip_row(r, profile)]
    skipped = len(grid[first_data:]) - len(body)

    notes: list[str] = []
    if skipped:
        notes.append(f"Skipped {skipped} blank or total rows")
    cols: list[pl.Series] = []
    field_notes: dict[str, str] = {}
    for c, name in enumerate(headers):
        values = [r[c] if c < len(r) else None for r in body]
        col = build_column(values, name, profile.type_overrides.get(name))
        cols.append(col.series)
        if col.note:
            field_notes[name] = col.note
    df = pl.DataFrame(cols) if cols else pl.DataFrame()

    from databridge.ingest.profiling import profile_fields

    fields = profile_fields(df)
    for f, letter in zip(fields, (column_letter(i) for i in range(width))):
        f["column_letter"] = letter
        if f["name"] in field_notes:
            f["note"] = field_notes[f["name"]]
    return ParseResult(df=df, fields=fields, notes=notes, profile=profile)


def _read_structured(content: bytes, ext: str) -> pl.DataFrame:
    buf = io.BytesIO(content)
    if ext == ".parquet":
        return pl.read_parquet(buf)
    if ext in {".jsonl", ".ndjson"}:
        return pl.read_ndjson(buf)
    df = pl.read_json(buf)
    # Flatten one level of nested objects into dotted column names.
    structs = [c for c, t in df.schema.items() if isinstance(t, pl.Struct)]
    for c in structs:
        df = df.with_columns(pl.col(c).struct.rename_fields(
            [f"{c}.{f.name}" for f in df.schema[c].fields])).unnest(c)
    return df
