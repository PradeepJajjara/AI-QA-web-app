from pathlib import Path

import pandas as pd
import pytest

from dataqa.loader import LoadError, load_file, open_connection, schema_text, table_name_for


@pytest.fixture
def con():
    return open_connection(None)


def write_csv(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text)
    return p


def test_csv_loads_with_types(con, tmp_path):
    p = write_csv(tmp_path, "sales.csv", "id,amount,day\n1,10.5,2025-01-01\n2,20,2025-01-02\n")
    t = load_file(con, p, set()).tables[0]
    assert t.name == "sales" and t.rows == 2
    by_name = {c.name: c for c in t.columns}
    assert by_name["amount"].is_numeric
    assert by_name["day"].is_temporal
    assert by_name["amount"].min == 10.5 and by_name["amount"].max == 20


def test_excel_loads(con, tmp_path):
    p = tmp_path / "People Data.xlsx"
    pd.DataFrame({"emp_id": ["a", "b"], "score": [1, 2]}).to_excel(p, index=False)
    t = load_file(con, p, set()).tables[0]
    assert t.name == "people_data" and t.rows == 2
    assert t.column_names == ["emp_id", "score"]


def test_empty_file_rejected(con, tmp_path):
    p = write_csv(tmp_path, "empty.csv", "")
    with pytest.raises(LoadError, match="empty"):
        load_file(con, p, set())


def test_header_only_rejected(con, tmp_path):
    p = write_csv(tmp_path, "hdr.csv", "a,b\n")
    with pytest.raises(LoadError, match="no data rows"):
        load_file(con, p, set())
    assert con.execute("SELECT count(*) FROM information_schema.tables").fetchone()[0] == 0


def test_single_column_ok(con, tmp_path):
    p = write_csv(tmp_path, "one.csv", "x\n1\n2\n3\n")
    t = load_file(con, p, set()).tables[0]
    assert t.column_names == ["x"] and t.rows == 3


def test_all_null_column_flagged(con, tmp_path):
    p = write_csv(tmp_path, "nulls.csv", "id,blank\n1,\n2,\n")
    t = load_file(con, p, set()).tables[0]
    blank = next(c for c in t.columns if c.name == "blank")
    assert blank.all_null
    assert "ALL NULL" in schema_text([t])


def test_unsupported_extension(con, tmp_path):
    p = write_csv(tmp_path, "notes.json", "{}")
    with pytest.raises(LoadError, match="unsupported"):
        load_file(con, p, set())


def test_max_files_enforced(con, tmp_path):
    p = write_csv(tmp_path, "f.csv", "a\n1\n")
    with pytest.raises(LoadError, match="At most"):
        load_file(con, p, {"t1", "t2", "t3", "t4", "t5"})


def test_table_name_sanitised_and_unique():
    assert table_name_for("2024 Sales Report.CSV", set()) == "t_2024_sales_report"
    assert table_name_for("sales.csv", {"sales"}) == "sales_2"
    assert table_name_for("!!!.csv", set()) == "table"


def test_multi_sheet_excel_one_table_per_sheet(con, tmp_path):
    p = tmp_path / "hr.xlsx"
    with pd.ExcelWriter(p) as xw:
        pd.DataFrame({"emp_id": ["a", "b"]}).to_excel(xw, sheet_name="Employees", index=False)
        pd.DataFrame({"emp_id": ["a"], "amt": [5]}).to_excel(xw, sheet_name="Pay Roll", index=False)
        pd.DataFrame().to_excel(xw, sheet_name="Blank", index=False)
    r = load_file(con, p, set())
    assert [t.name for t in r.tables] == ["hr_employees", "hr_pay_roll"]
    assert r.tables[1].source == "hr.xlsx [Pay Roll]"
    assert r.notices == ["hr.xlsx [Blank]: empty sheet, skipped."]


def test_excel_all_sheets_empty_rejected(con, tmp_path):
    p = tmp_path / "blank.xlsx"
    pd.DataFrame().to_excel(p, index=False)
    with pytest.raises(LoadError, match="no sheet has any data"):
        load_file(con, p, set())


def test_sheets_count_toward_cap(con, tmp_path):
    p = tmp_path / "wide.xlsx"
    with pd.ExcelWriter(p) as xw:
        for i in range(3):
            pd.DataFrame({"x": [1]}).to_excel(xw, sheet_name=f"s{i}", index=False)
    with pytest.raises(LoadError, match="At most"):
        load_file(con, p, {"a", "b", "c"})


def test_latin1_fallback_with_notice(con, tmp_path):
    p = tmp_path / "latin1.csv"
    p.write_bytes("id,city\n1,Zürich\n".encode("latin-1"))
    r = load_file(con, p, set())
    assert r.tables[0].rows == 1 and r.notices == ["latin1.csv: not UTF-8, read as Latin-1."]
    assert con.execute("SELECT city FROM latin1").fetchone()[0] == "Zürich"


def test_duplicate_column_names_deduplicated(con, tmp_path):
    p = write_csv(tmp_path, "dup.csv", "id,name,name\n1,a,b\n")
    assert load_file(con, p, set()).tables[0].column_names == ["id", "name", "name_1"]


def test_parse_error_is_one_line(con, tmp_path):
    p = tmp_path / "bad.xlsx"
    p.write_bytes(b"this is not an excel file")
    with pytest.raises(LoadError) as e:
        load_file(con, p, set())
    assert "\n" not in str(e.value) and len(str(e.value)) < 300


def test_column_with_spaces_end_to_end(con, tmp_path):
    from dataqa.engine import execute
    from dataqa.plan import Aggregate, Catalog, Filter, Plan, validate
    p = write_csv(tmp_path, "sp.csv", "Employee ID,Base Salary\nE1,100\nE2,200\n")
    t = load_file(con, p, set()).tables[0]
    cat = Catalog([t], [])
    plan = Plan(tables=["sp"], filters=[Filter(table="sp", column="Employee ID", op="=", value="e2")],
                aggregates=[Aggregate(func="sum", table="sp", column="Base Salary")])
    assert execute(con, validate(plan, cat), cat).scalar == 200


# --- overnight bug hunt additions (see OVERNIGHT.md) ------------------------------------

def test_day_mon_yy_text_dates_become_dates(con, tmp_path):
    """The Kaggle HR export writes dates as 16-Mar-23. DuckDB doesn't sniff that, so the column
    loaded as text: 'total training cost in 2023' answered nan and every trend question refused."""
    p = write_csv(tmp_path, "training.csv", "Employee ID,Training Date,Training Cost\n"
                  "1001,21-Sep-22,510.83\n1002,19-Jul-23,582.37\n1003,05-Jan-23,100\n1004,,50\n")
    res = load_file(con, p, set())
    t = res.tables[0]
    by_name = {c.name: c for c in t.columns}
    assert by_name["Training Date"].is_temporal
    assert str(by_name["Training Date"].min) == "2022-09-21" and str(by_name["Training Date"].max) == "2023-07-19"
    assert by_name["Training Date"].null_frac == 0.25
    # said beside the column, not as a banner: the sidebar stays quiet
    assert by_name["Training Date"].note == "converted from text like 21-Sep-22"
    assert by_name["Training Cost"].note == "" and res.notices == []


def test_ambiguous_day_month_order_is_disclosed(con, tmp_path):
    """03/08/2023 could be 3 Aug or 8 Mar. DuckDB reads it day-first; when nothing in the column
    settles it, the load says so rather than silently picking."""
    p = write_csv(tmp_path, "s.csv", "id,when\n1,03/08/2023\n2,04/05/2023\n3,01/02/2023\n")
    res = load_file(con, p, set())
    by_name = {c.name: c for c in res.tables[0].columns}
    assert by_name["when"].is_temporal
    assert "day/month/year" in by_name["when"].note and "month/day/year" in by_name["when"].note
    assert by_name["id"].note == "" and res.notices == []


def test_unambiguous_slash_dates_become_dates(con, tmp_path):
    p = write_csv(tmp_path, "s.csv", "id,when\n1,03/08/2023\n2,25/05/2023\n3,01/02/2023\n")  # 25 forces day-first
    by_name = {c.name: c for c in load_file(con, p, set()).tables[0].columns}
    assert by_name["when"].is_temporal and str(by_name["when"].min) == "2023-02-01"


def test_text_that_is_not_all_dates_stays_text(con, tmp_path):
    p = write_csv(tmp_path, "s.csv", "id,note\n1,16-Mar-23\n2,pending\n3,01-Jan-24\n")
    by_name = {c.name: c for c in load_file(con, p, set()).tables[0].columns}
    assert not by_name["note"].is_temporal
