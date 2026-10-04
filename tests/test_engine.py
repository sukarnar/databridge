import polars as pl
import pytest

from databridge.engine.automap import suggest
from databridge.engine.formula import FormulaError, compile_formula, referenced_columns
from databridge.engine.mapper import MappingSpec, run_mapping

DF = pl.DataFrame({
    "First": [" jane ", "ravi", None],
    "Last": ["Doe", "Kumar", "X"],
    "Amount": ["1,200.50", "310", "abc"],
    "When": ["2026-08-01", "08/03/2026", None],
})


def ev(formula):
    return DF.select(compile_formula(formula, set(DF.columns)).alias("v"))["v"].to_list()


def test_text_functions():
    assert ev('PROPER([First]) & " " & [Last]') == ["Jane Doe", "Ravi Kumar", " X"]
    assert ev("UPPER(LEFT([Last], 2))") == ["DO", "KU", "X"]
    assert ev('CONCAT([First], "-", [Last])')[2] == "-X"
    assert ev('SPLIT([Last] & "|b|c", "|", 2)') == ["b", "b", "b"]


def test_numbers_and_logic():
    assert ev("ROUND(TO_NUMBER([Amount]) * 2, 1)") == [2401.0, 620.0, None]
    assert ev('IF(TO_NUMBER([Amount]) > 500, "big", "small")') == ["big", "small", "small"]  # blank condition = false
    assert ev("ISBLANK([First])") == [False, False, True]


def test_dates():
    assert [str(d) for d in ev("DATEVALUE([When])")] == ["2026-08-01", "2026-08-03", "None"]
    assert ev('TEXT([When], "%Y-%m")') == ["2026-08", "2026-08", None]


def test_errors_and_refs():
    with pytest.raises(FormulaError):
        compile_formula("[Nope]", set(DF.columns))
    with pytest.raises(FormulaError):
        compile_formula("FOO([First])", set(DF.columns))
    with pytest.raises(FormulaError):
        compile_formula("TRIM([First]", set(DF.columns))
    assert referenced_columns('[A] & [B] & [A]') == ["A", "B"]


def test_formula_cannot_execute_python():
    with pytest.raises(FormulaError):
        compile_formula("__import__('os')", set())


def test_run_mapping_valid_and_rejects():
    tf = [
        {"name": "name", "type": "string", "required": True},
        {"name": "amount", "type": "decimal(10,2)", "required": True},
        {"name": "when", "type": "date"},
    ]
    spec = MappingSpec(
        rules=[{"target": "name", "formula": "TRIM([First])"}, {"target": "amount", "formula": "[Amount]"},
               {"target": "when", "formula": "DATEVALUE([When])"}],
        validations=[{"field": "amount", "kind": "max", "value": 1000}],
    )
    res = run_mapping(DF, spec, tf)
    assert res.valid["name"].to_list() == ["ravi"]
    assert res.rejects.height == 2
    assert "above maximum" in res.rejects["errors"][0]
    assert "required" in res.rejects["errors"][1]


def test_row_steps_unpivot_and_filter():
    wide = pl.DataFrame({"sku": ["A", "B"], "Jan": [1, 2], "Feb": [3, None]})
    spec = MappingSpec(
        rules=[{"target": "sku", "formula": "[sku]"}, {"target": "month", "formula": "[month]"},
               {"target": "qty", "formula": "[qty]"}],
        row_steps=[{"kind": "unpivot", "value_columns": ["Jan", "Feb"], "variable_name": "month", "value_name": "qty"},
                   {"kind": "filter", "formula": "NOT_A_FUNCTION()"}][:1] + [{"kind": "filter", "formula": "[qty] > 1"}],
    )
    tf = [{"name": "sku", "type": "string"}, {"name": "month", "type": "string"}, {"name": "qty", "type": "integer"}]
    res = run_mapping(wide, spec, tf)
    assert res.valid.to_dicts() == [{"sku": "B", "month": "Jan", "qty": 2}, {"sku": "A", "month": "Feb", "qty": 3}]


def test_automap():
    src = [{"name": "Cust No", "type": "string"}, {"name": "Order Dt", "type": "date"}, {"name": "Amt", "type": "float"}]
    tgt = [{"name": "customer_id", "type": "string"}, {"name": "order_date", "type": "date"},
           {"name": "amount", "type": "decimal(12,2)"}]
    pairs = {(s.source, s.target) for s in suggest(src, tgt)}
    assert pairs == {("Cust No", "customer_id"), ("Order Dt", "order_date"), ("Amt", "amount")}
