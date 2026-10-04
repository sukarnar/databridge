import polars as pl

from databridge.ingest.infer import infer_type
from databridge.ingest.profiling import detect_drift
from databridge.ingest.sheet_profile import SheetProfile, detect_header_row, parse_file, suggest_profile


def test_header_detection_skips_banner_rows(sales_bytes):
    prof = suggest_profile(sales_bytes, "vendor_sales.xlsx")
    assert prof.sheet == "Sales"
    assert prof.header_row == 4


def test_parse_handles_messy_sheet(sales_bytes):
    res = parse_file(sales_bytes, "vendor_sales.xlsx")
    df = res.df
    assert df.columns == ["Cust ID", "First Name", "Last Name", "Region", "Order Date", "Amount", "Status"]
    assert df.height == 6  # blank row and Total row skipped
    assert df["Cust ID"].to_list()[0] == "00123"  # leading zeros kept
    assert df.schema["Order Date"] == pl.Date
    assert df.schema["Amount"] == pl.Float64
    assert df["Region"].to_list()[1] == "East"  # merged cell filled
    assert df["Amount"].to_list()[2] == 1045.0  # "1,045.00" parsed
    assert df["Amount"].to_list()[5] is None  # "n/a" treated as blank


def test_type_overrides(sales_bytes):
    prof = suggest_profile(sales_bytes, "vendor_sales.xlsx")
    prof.type_overrides = {"Amount": "string"}
    res = parse_file(sales_bytes, "vendor_sales.xlsx", prof)
    assert res.df.schema["Amount"] == pl.String


def test_infer_excel_serial_dates():
    ctype, note = infer_type([45544, 45545, None], "Invoice Date")
    assert ctype == "date" and "serial" in note


def test_csv_and_header_detection():
    csv = b"Report,,\n,,\nid,name,amount\n1,A,10\n2,B,20\n"
    res = parse_file(csv, "x.csv")
    assert res.df.columns == ["id", "name", "amount"]
    assert res.df.height == 2
    assert detect_header_row([["Title", None], ["a", "b"], [1, 2]]) == 2


def test_drift():
    old = [{"name": "Cust ID", "type": "string"}, {"name": "Amount", "type": "float"}]
    new = [{"name": "Customer ID", "type": "string"}, {"name": "Amount", "type": "string"}, {"name": "Notes", "type": "string"}]
    d = detect_drift(old, new)
    assert d["changed"] and d["added"] == ["Customer ID", "Notes"] or d["renamed"]
    assert d["retyped"][0]["name"] == "Amount"
