"""Column type inference for spreadsheet and CSV values.

Rules that matter for business spreadsheets:
- Digit strings with leading zeros (IDs, zip codes) stay text.
- Thousands separators and currency symbols are stripped before numeric parsing.
- Integer columns named like dates, holding Excel serial numbers, become dates.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import polars as pl

from databridge.core.types import base_type, to_polars

_INT_RE = re.compile(r"[-+]?\d{1,18}")
_NUM_RE = re.compile(r"[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?")
_LEADING_ZERO_RE = re.compile(r"0\d+")
_CURRENCY = str.maketrans("", "", "$€£¥₹ ")
_TRUE = {"true", "yes", "y", "t"}
_FALSE = {"false", "no", "n", "f"}
_BOOL_WORDS = _TRUE | _FALSE
_DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%m-%d-%Y", "%d-%b-%Y", "%d %b %Y", "%b %d, %Y", "%Y/%m/%d"]
_TS_FORMATS = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M"]
_DATE_NAME_RE = re.compile(r"(date|dt|day|dob|_on$|period)", re.I)
_EXCEL_EPOCH = datetime(1899, 12, 30)


@dataclass
class InferredColumn:
    ctype: str
    series: pl.Series
    note: str = ""


_NULL_TOKENS = {"", "n/a", "na", "#n/a", "null", "none", "-", "--", "nan"}


def _is_blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and v.strip().lower() in _NULL_TOKENS)


def _clean_num(s: str) -> str:
    s = s.strip().translate(_CURRENCY).replace(",", "")
    if s.startswith("(") and s.endswith(")"):  # accounting negative: (1,200.00)
        s = "-" + s[1:-1]
    return s


def _parse_date(s: str, formats: list[str]) -> datetime | None:
    for fmt in formats:
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    return None


def _all(values: list[Any], pred) -> bool:
    return all(pred(v) for v in values)


def infer_type(values: list[Any], name: str = "") -> tuple[str, str]:
    """Returns (canonical_type, note) for a list of raw cell values."""
    vals = [v for v in values if not _is_blank(v)]
    if not vals:
        return "string", "empty column"

    if _all(vals, lambda v: isinstance(v, bool)):
        return "boolean", ""
    if _all(vals, lambda v: isinstance(v, (datetime, date)) and not isinstance(v, bool)):
        if _all(vals, lambda v: not isinstance(v, datetime) or v.time() == time(0, 0)):
            return "date", ""
        return "timestamp", ""
    if _all(vals, lambda v: isinstance(v, (int, float, Decimal)) and not isinstance(v, bool)):
        if _all(vals, lambda v: float(v).is_integer()):
            ints = [int(v) for v in vals]
            if _DATE_NAME_RE.search(name) and all(20000 <= i <= 80000 for i in ints):
                return "date", "converted from Excel serial dates"
            return "integer", ""
        return "float", ""

    strs = [str(v).strip() if not isinstance(v, float) or not v.is_integer() else str(int(v)) for v in vals]
    if any(_LEADING_ZERO_RE.fullmatch(s) for s in strs):
        return "string", "kept as text: values have leading zeros"
    if _all(strs, lambda s: s.lower() in _BOOL_WORDS):
        return "boolean", ""
    cleaned = [_clean_num(s) for s in strs]
    if _all(cleaned, lambda s: bool(_INT_RE.fullmatch(s))):
        return "integer", ""
    if _all(cleaned, lambda s: bool(_NUM_RE.fullmatch(s))):
        return "float", ""
    if _all(strs, lambda s: _parse_date(s, _DATE_FORMATS) is not None):
        return "date", ""
    if _all(strs, lambda s: _parse_date(s, _TS_FORMATS + _DATE_FORMATS) is not None):
        return "timestamp", ""
    return "string", ""


def _convert(v: Any, ctype: str, from_serial: bool) -> Any:
    if _is_blank(v):
        return None
    b = base_type(ctype)
    try:
        if b == "string" or b == "json":
            if isinstance(v, float) and v.is_integer():
                return str(int(v))
            if isinstance(v, datetime):
                return v.isoformat(sep=" ")
            return str(v).strip()
        if b == "boolean":
            if isinstance(v, bool):
                return v
            s = str(v).strip().lower()
            return True if s in _TRUE or s == "1" else False if s in _FALSE or s == "0" else None
        if b == "integer":
            if isinstance(v, (int, float, Decimal)):
                return int(v)
            return int(Decimal(_clean_num(str(v))))
        if b in ("float", "decimal"):
            if isinstance(v, (int, float)):
                return float(v)
            return float(Decimal(_clean_num(str(v))))
        if b == "date":
            if isinstance(v, datetime):
                return v.date()
            if isinstance(v, date):
                return v
            if isinstance(v, (int, float)) and from_serial:
                return (_EXCEL_EPOCH + timedelta(days=int(v))).date()
            d = _parse_date(str(v), _DATE_FORMATS + _TS_FORMATS)
            return d.date() if d else None
        if b == "timestamp":
            if isinstance(v, datetime):
                return v
            if isinstance(v, date):
                return datetime.combine(v, time())
            return _parse_date(str(v), _TS_FORMATS + _DATE_FORMATS)
    except (ValueError, InvalidOperation, OverflowError):
        return None
    return v


def build_column(values: list[Any], name: str, override: str | None = None) -> InferredColumn:
    """Infer (or apply an override) and convert raw values into a typed Polars Series."""
    if override:
        ctype, note = override, "type set by profile"
    else:
        ctype, note = infer_type(values, name)
    from_serial = "Excel serial" in note
    converted = [_convert(v, ctype, from_serial) for v in values]
    dtype = to_polars(ctype)
    if base_type(ctype) == "decimal":
        dtype = pl.Float64()  # stored as float in snapshots; cast to exact decimal at mapping time
    series = pl.Series(name, converted, dtype=dtype, strict=False)
    return InferredColumn(ctype=ctype if base_type(ctype) != "decimal" else "float", series=series, note=note)
