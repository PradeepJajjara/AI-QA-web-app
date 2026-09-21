from pathlib import Path

import pytest

from dataqa.joins import confirm, default_selection, detect_joins
from dataqa.loader import load_file, open_connection
from dataqa.plan import Catalog
from dataqa.router import answer
from dataqa.templates import parse

DEMO = Path(__file__).parent.parent / "data" / "demo"


@pytest.fixture(scope="module")
def demo():
    con = open_connection(None)
    tables = []
    for f in ["employees.csv", "payroll.csv", "attendance.xlsx"]:
        tables += load_file(con, DEMO / f, {t.name for t in tables}).tables
    return con, Catalog(tables, [confirm(c) for c in default_selection(detect_joins(con, tables))])


@pytest.mark.parametrize("q,tables,func,col,by", [
    ("total base_salary", ["payroll"], "sum", "base_salary", None),
    ("Total base salary", ["payroll"], "sum", "base_salary", None),
    ("what is the sum of bonus?", ["payroll"], "sum", "bonus", None),
    ("average hours", ["attendance"], "avg", "hours", None),
    ("avg bonus by department", ["payroll", "employees"], "avg", "bonus", "department"),
    ("highest base_salary per grade", ["payroll", "employees"], "max", "base_salary", "grade"),
    ("minimum deductions", ["payroll"], "min", "deductions", None),
    ("number of distinct location", ["employees"], "count_distinct", "location", None),
    ("unique status", ["attendance"], "count_distinct", "status", None),
    ("waht is the maximum base salary", ["payroll"], "max", "base_salary", None),      # typo'd filler
    ("please tell me the total bonus by department", ["payroll", "employees"], "sum", "bonus", "department"),
    ("can you show me the average hours", ["attendance"], "avg", "hours", None),
])
def test_aggregate_shapes(demo, q, tables, func, col, by):
    p = parse(q, demo[1])
    assert p is not None, q
    assert p.tables == tables
    assert (p.aggregates[0].func, p.aggregates[0].column) == (func, col)
    assert ([g.column for g in p.group_by] or [None])[0] == by


@pytest.mark.parametrize("q,table,by", [
    ("how many employees", "employees", None),
    ("How many employees are there?", "employees", None),
    ("tell me how many employees", "employees", None),
    ("number of payroll rows", "payroll", None),
    ("count of employees by department", "employees", "department"),
    ("how many attendance per status", "attendance", "status"),
])
def test_count_shapes(demo, q, table, by):
    p = parse(q, demo[1])
    assert p is not None and p.tables[0] == table and p.aggregates[0].column == ""
    assert ([g.column for g in p.group_by] or [None])[0] == by


@pytest.mark.parametrize("q", [
    "total salary",                              # no such column
    "total base_salary in engineering",          # filter -> model
    "average hours in March 2025",               # date -> model
    "which department has the highest bonus",    # not a template shape
    "total base_salary by team",                 # unknown group column
    "how many people",                           # unknown table
    "show me employees",                         # listing -> model
    "engineering total bonus",                   # a real word before the shape is not filler -> model
    "",
])
def test_falls_through(demo, q):
    assert parse(q, demo[1]) is None


def test_ambiguous_column_falls_through(demo):
    con, cat = demo
    con.execute("ALTER TABLE payroll ADD COLUMN department VARCHAR")
    cat.tables[1].columns.append(type(cat.tables[1].columns[0])("department", "VARCHAR", 1.0, 0))
    try:
        assert parse("count of employees by department", cat) is None
    finally:
        con.execute("ALTER TABLE payroll DROP COLUMN department"); cat.tables[1].columns.pop()


def test_router_end_to_end(demo):
    con, cat = demo
    a = answer("average base_salary by department", con, cat)
    assert a.path == "template" and a.error is None
    assert len(a.result.rows) == 5 and list(a.result.rows.columns) == ["department", "avg_base_salary"]
    assert [l.describe() for l in a.result.compiled.used_links] == ["employees.employee_id = payroll.staff_id"]


def test_router_refuses_without_provider(demo):
    a = answer("which department has the most leave?", *demo)
    assert a.path == "refused" and "simple questions" in a.error


# --- overnight bug hunt additions (see OVERNIGHT.md) ------------------------------------

KAGGLE = Path(r"E:\Persona\interview\archive")


@pytest.fixture(scope="module")
def kaggle():
    if not KAGGLE.exists():
        pytest.skip("Kaggle HR set not present")
    con = open_connection(None)
    tables = []
    for f in ["employee_data.csv", "employee_engagement_survey_data.csv", "training_and_development_data.csv",
              "recruitment_data.csv"]:
        tables += load_file(con, KAGGLE / f, {t.name for t in tables}).tables
    return con, Catalog(tables, [confirm(c) for c in default_selection(detect_joins(con, tables))])


@pytest.mark.parametrize("q,func,col", [
    ("average work-life balance score", "avg", "Work-Life Balance Score"),   # hyphen in the name
    ("average work life balance score", "avg", "Work-Life Balance Score"),   # user drops the hyphen
    ("average Training Duration(Days)", "avg", "Training Duration(Days)"),   # parentheses
    ("average training duration days", "avg", "Training Duration(Days)"),    # user drops them
    ("total Training Cost", "sum", "Training Cost"),
])
def test_punctuated_column_names_match(kaggle, q, func, col):
    p = parse(q, kaggle[1])
    assert p is not None, q
    assert (p.aggregates[0].func, p.aggregates[0].column) == (func, col)


@pytest.mark.parametrize("q,func,col", [
    ("how many distinct departments", "count_distinct", "department"),
    ("how many unique locations", "count_distinct", "location"),
    ("unique locations", "count_distinct", "location"),                     # plural of a real column
    ("number of distinct grades", "count_distinct", "grade"),
])
def test_how_many_distinct_and_plural_columns(demo, q, func, col):
    p = parse(q, demo[1])
    assert p is not None, q
    assert (p.aggregates[0].func, p.aggregates[0].column) == (func, col)


@pytest.mark.parametrize("q,table", [
    ("how many employee", "employees"),
    ("how many attendances", "attendance"),
    ("how many payrolls", "payroll"),
])
def test_table_singular_plural_symmetry(demo, q, table):
    p = parse(q, demo[1])
    assert p is not None and p.tables[0] == table, q


def test_group_column_in_an_unlinked_file_falls_through(kaggle):
    """'average satisfaction score by gender': the only column called Gender is in the
    recruitment file, which isn't linked to the survey. The template matched it anyway and the
    app refused with 'set which columns link them' — for a question that never meant recruitment.
    Not a template shape: let the model read it (GenderCode on the employee file)."""
    con, cat = kaggle
    assert parse("average satisfaction score by gender", cat) is None
    assert parse("average satisfaction score by GenderCode", cat) is not None  # linked: fine
