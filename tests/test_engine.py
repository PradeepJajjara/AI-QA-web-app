from datetime import date
from pathlib import Path

import pytest

from dataqa.engine import compile_plan, date_range, execute, join_path
from dataqa.joins import ConfirmedJoin, confirm, default_selection, detect_joins
from dataqa.loader import load_file, open_connection
from dataqa.plan import Aggregate, Catalog, Filter, GroupBy, Plan, PlanError, Sort, validate

DEMO = Path(__file__).parent.parent / "data" / "demo"


@pytest.fixture(scope="module")
def demo():
    con = open_connection(None)
    tables = []
    for f in ["employees.csv", "payroll.csv", "attendance.xlsx"]:
        tables += load_file(con, DEMO / f, {t.name for t in tables}).tables
    links = [confirm(c) for c in default_selection(detect_joins(con, tables))]
    return con, Catalog(tables, links)


def run(demo, plan: Plan):
    con, cat = demo
    return execute(con, validate(plan, cat), cat)


# --- date ranges -------------------------------------------------------------------

@pytest.mark.parametrize("value,start,end", [
    ("2025", date(2025, 1, 1), date(2026, 1, 1)),
    ("2025-03", date(2025, 3, 1), date(2025, 4, 1)),
    ("2025-12", date(2025, 12, 1), date(2026, 1, 1)),
    ("2025-03-15", date(2025, 3, 15), date(2025, 3, 16)),
    ("2025-Q1", date(2025, 1, 1), date(2025, 4, 1)),
    ("2025Q4", date(2025, 10, 1), date(2026, 1, 1)),
    ("Q2 2025", date(2025, 4, 1), date(2025, 7, 1)),
    ("March 2025", date(2025, 3, 1), date(2025, 4, 1)),
    ("dec 2025", date(2025, 12, 1), date(2026, 1, 1)),
])
def test_date_range(value, start, end):
    assert date_range(value) == (start, end)


def test_date_range_rejects_garbage():
    with pytest.raises(PlanError, match="couldn.t read"):
        date_range("last tuesday")
    with pytest.raises(PlanError, match="not a valid date"):
        date_range("2025-13")


# --- join path ---------------------------------------------------------------------

L = [ConfirmedJoin("employees", "employee_id", "payroll", "staff_id", left_unique=True),
     ConfirmedJoin("employees", "employee_id", "attendance", "emp_id", left_unique=True)]


def test_join_path_direct():
    assert join_path(["employees", "payroll"], L) == (["employees", "payroll"], [L[0]])


def test_join_path_via_intermediate():
    order, used = join_path(["payroll", "attendance"], L)
    assert order == ["payroll", "employees", "attendance"]
    assert used == [L[0], L[1]]


def test_join_path_disconnected():
    with pytest.raises(PlanError, match="aren't linked"):
        join_path(["payroll", "attendance"], [L[0]])


# --- validation refusals -----------------------------------------------------------

def test_unknown_table(demo):
    from dataqa.plan import MissingTable
    with pytest.raises(MissingTable, match="'salaries' isn't one of the loaded files") as e:
        run(demo, Plan(tables=["salaries"], aggregates=[Aggregate(func="count")]))
    assert e.value.kind == "fixable" and e.value.name == "salaries"


def test_single_strong_match_autocorrected_with_note(demo):
    con, cat = demo
    cat.notes = []
    r = run(demo, Plan(tables=["payroll"], aggregates=[Aggregate(func="sum", table="payroll", column="salary")]))
    assert r.scalar == con.execute("SELECT sum(base_salary) FROM payroll").fetchone()[0]
    assert cat.notes == ["Read 'salary' as 'base_salary'."]


def test_unknown_column_no_match_lists_columns(demo):
    with pytest.raises(PlanError, match="has no column 'zzz'. Its columns are: staff_id") as e:
        run(demo, Plan(tables=["payroll"], aggregates=[Aggregate(func="sum", table="payroll", column="zzz")]))
    assert e.value.kind == "fixable"


def test_two_plausible_matches_ask(demo):
    from dataqa.plan import Ambiguous, apply_choice
    with pytest.raises(Ambiguous) as e:
        run(demo, Plan(tables=["employees"], aggregates=[Aggregate(func="count_distinct", table="employees", column="dep")]))
    assert e.value.options == ["department"] or set(e.value.options) >= {"department"}
    fixed = apply_choice(e.value.plan, e.value.location, "department")
    assert run(demo, fixed).scalar == 5


def test_sum_of_text_refused(demo):
    with pytest.raises(PlanError, match="holds text, so I can't add it up") as e:
        run(demo, Plan(tables=["employees"], aggregates=[Aggregate(func="sum", table="employees", column="name")]))
    assert e.value.kind == "capability"


def test_bucket_on_non_date_refused(demo):
    with pytest.raises(PlanError, match="isn't a date, so it can't be grouped by month"):
        run(demo, Plan(tables=["employees"], group_by=[GroupBy(table="employees", column="grade", bucket="month")],
                       aggregates=[Aggregate(func="count")]))


def test_column_on_unused_table_pulls_table_in(demo):
    con, cat = demo
    cat.notes = []
    r = run(demo, Plan(tables=["payroll"], filters=[Filter(table="employees", column="department", op="=", value="HR")],
                       aggregates=[Aggregate(func="count")]))
    assert r.scalar == con.execute("SELECT count(*) FROM payroll p JOIN employees e ON p.staff_id=e.employee_id WHERE e.department='HR'").fetchone()[0]
    assert cat.notes == ["Took 'department' from employees."]


def test_bad_sort_key_refused_when_ambiguous(demo):
    with pytest.raises(PlanError, match="can't order the result by 'banana'"):
        run(demo, Plan(tables=["payroll"], aggregates=[Aggregate(func="sum", table="payroll", column="bonus"),
                                                       Aggregate(func="sum", table="payroll", column="deductions")],
                       sort=Sort(by="banana")))


def test_sort_key_near_miss_rescued_with_note(demo):
    con, cat = demo
    cat.notes = []
    r = run(demo, Plan(tables=["employees", "payroll"], group_by=[GroupBy(table="employees", column="department")],
                       aggregates=[Aggregate(func="avg", table="payroll", column="base_salary")],
                       sort=Sort(by="average_base_salary"), limit=1))
    assert len(r.rows) == 1 and cat.notes == ["Sorted by 'avg_base_salary' (asked for 'average_base_salary')."]


def test_multi_table_without_links_refused(demo):
    con, cat = demo
    with pytest.raises(PlanError, match="aren't linked"):
        validate(Plan(tables=["employees", "payroll"], aggregates=[Aggregate(func="count")]), Catalog(cat.tables, []))


def test_case_insensitive_column_rescue(demo):
    r = run(demo, Plan(tables=["employees"], aggregates=[Aggregate(func="count_distinct", table="employees", column="DEPARTMENT")]))
    assert r.scalar == 5


# --- execution on demo data (expected values computed independently) ----------------

def test_total(demo):
    con, _ = demo
    expected = con.execute("SELECT sum(base_salary) FROM payroll").fetchone()[0]
    r = run(demo, Plan(tables=["payroll"], aggregates=[Aggregate(func="sum", table="payroll", column="base_salary")]))
    assert r.scalar == expected and r.compiled.params == []


def test_filter_text_case_insensitive(demo):
    con, _ = demo
    expected = con.execute("SELECT count(*) FROM employees WHERE department='Engineering'").fetchone()[0]
    r = run(demo, Plan(tables=["employees"], filters=[Filter(table="employees", column="department", op="=", value="engineering")],
                       aggregates=[Aggregate(func="count")]))
    assert r.scalar == expected > 0
    assert r.compiled.params == ["engineering"]


def test_group_by_sorted_desc_by_default(demo):
    con, _ = demo
    expected = con.execute("SELECT department, count(*) c FROM employees GROUP BY 1 ORDER BY c DESC").fetchall()
    r = run(demo, Plan(tables=["employees"], group_by=[GroupBy(table="employees", column="department")],
                       aggregates=[Aggregate(func="count")]))
    assert list(r.rows.itertuples(index=False, name=None)) == expected
    assert list(r.rows.columns) == ["department", "count"]


def test_time_bucket_trend_sorted_asc(demo):
    con, _ = demo
    expected = con.execute("SELECT date_trunc('month', CAST(date AS DATE)) m, sum(hours) FROM attendance GROUP BY 1 ORDER BY 1").fetchall()
    r = run(demo, Plan(tables=["attendance"], group_by=[GroupBy(table="attendance", column="date", bucket="month")],
                       aggregates=[Aggregate(func="sum", table="attendance", column="hours")]))
    assert len(r.rows) == 6 and list(r.rows.columns) == ["date_month", "sum_hours"]
    assert [tuple(x) for x in r.rows.itertuples(index=False, name=None)] == [(a, b) for a, b in expected]


def test_partial_date_filter(demo):
    con, _ = demo
    expected = con.execute("SELECT count(*) FROM attendance WHERE date >= '2025-03-01' AND date < '2025-04-01' AND status='Leave'").fetchone()[0]
    r = run(demo, Plan(tables=["attendance"],
                       filters=[Filter(table="attendance", column="date", op="=", value="March 2025"),
                                Filter(table="attendance", column="status", op="=", value="Leave")],
                       aggregates=[Aggregate(func="count")]))
    assert r.scalar == expected > 0


def test_cross_file_join_uses_applied_link(demo):
    con, cat = demo
    expected = con.execute("""SELECT e.department, avg(p.base_salary) FROM employees e
                              JOIN payroll p ON e.employee_id = p.staff_id GROUP BY 1 ORDER BY 2 DESC""").fetchall()
    r = run(demo, Plan(tables=["employees", "payroll"],
                       group_by=[GroupBy(table="employees", column="department")],
                       aggregates=[Aggregate(func="avg", table="payroll", column="base_salary")]))
    assert [tuple(x) for x in r.rows.itertuples(index=False, name=None)] == pytest.approx(expected)
    assert [l.describe() for l in r.compiled.used_links] == ["employees.employee_id = payroll.staff_id"]


def test_fanout_sum_refused(demo):
    """sum(payroll.bonus) joined to attendance would be inflated ~130x. Refuse, explain."""
    with pytest.raises(PlanError, match="total of bonus together with attendance") as e:
        run(demo, Plan(tables=["payroll", "attendance"],
                       aggregates=[Aggregate(func="sum", table="payroll", column="bonus")]))
    assert e.value.kind == "capability"


def test_fanout_one_side_aggregate_refused(demo):
    """sum(employees.x) joined to payroll repeats each employee 12x."""
    con, _ = demo
    con.execute("ALTER TABLE employees ADD COLUMN headcount INTEGER DEFAULT 1")
    try:
        cat = demo[1]; cat.tables[0].columns.append(type(cat.tables[0].columns[0])("headcount", "INTEGER", 0.0, 1))
        with pytest.raises(PlanError, match="counted once per matching payroll row"):
            run(demo, Plan(tables=["employees", "payroll"],
                           aggregates=[Aggregate(func="sum", table="employees", column="headcount")]))
    finally:
        con.execute("ALTER TABLE employees DROP COLUMN headcount"); cat.tables[0].columns.pop()


def test_many_side_aggregate_allowed(demo):
    """sum(payroll.bonus) by employees.department is fine: employees is the one-side."""
    r = run(demo, Plan(tables=["employees", "payroll"], group_by=[GroupBy(table="employees", column="department")],
                       aggregates=[Aggregate(func="sum", table="payroll", column="bonus")]))
    assert len(r.rows) == 5


def test_count_star_rooted_at_fact_table_allowed(demo):
    con, _ = demo
    expected = con.execute("SELECT count(*) FROM employees e JOIN payroll p ON e.employee_id=p.staff_id").fetchone()[0]
    r = run(demo, Plan(tables=["employees", "payroll"], aggregates=[Aggregate(func="count")]))
    assert r.scalar == expected == 1411


def test_count_star_chasm_refused(demo):
    with pytest.raises(PlanError, match="double-count"):
        run(demo, Plan(tables=["payroll", "attendance"], aggregates=[Aggregate(func="count")]))


def test_cross_file_via_intermediate_is_disclosed(demo):
    r = run(demo, Plan(tables=["payroll", "attendance"],
                       aggregates=[Aggregate(func="count_distinct", table="attendance", column="emp_id")]))
    assert r.scalar == 120
    assert r.compiled.tables == ["payroll", "employees", "attendance"]
    assert r.notes == ["Went through employees to connect the files."]


def test_listing_defaults_to_limit(demo):
    r = run(demo, Plan(tables=["employees"], filters=[Filter(table="employees", column="grade", op="in", values=["L4"])]))
    assert 0 < len(r.rows) <= 100 and "LIMIT 100" in r.compiled.sql


def test_between_and_comparison_on_numbers(demo):
    con, _ = demo
    expected = con.execute("SELECT count(*) FROM payroll WHERE bonus BETWEEN 10000 AND 20000").fetchone()[0]
    r = run(demo, Plan(tables=["payroll"], filters=[Filter(table="payroll", column="bonus", op="between", values=["10000", "20000"])],
                       aggregates=[Aggregate(func="count")]))
    assert r.scalar == expected > 0
    with pytest.raises(PlanError, match="isn't a number"):
        run(demo, Plan(tables=["payroll"], filters=[Filter(table="payroll", column="bonus", op=">", value="lots")],
                       aggregates=[Aggregate(func="count")]))


def test_values_are_parameters_not_interpolated(demo):
    evil = "x'; DROP TABLE employees; --"
    r = run(demo, Plan(tables=["employees"], filters=[Filter(table="employees", column="name", op="=", value=evil)],
                       aggregates=[Aggregate(func="count")]))
    assert r.scalar == 0 and evil not in r.compiled.sql
    assert demo[0].execute("SELECT count(*) FROM employees").fetchone()[0] == 120


def test_two_equals_on_one_column_merged_to_in(demo):
    con, cat = demo
    cat.notes = []
    r = run(demo, Plan(tables=["employees"],
                       filters=[Filter(table="employees", column="department", op="=", value="Sales"),
                                Filter(table="employees", column="department", op="=", value="Engineering")],
                       group_by=[GroupBy(table="employees", column="department")],
                       aggregates=[Aggregate(func="count")]))
    assert len(r.rows) == 2
    assert cat.notes == ["Read 'department' = Sales / Engineering as any of those."]


def test_arithmetic_over_real_columns_asks_which(demo):
    from dataqa.plan import Ambiguous
    with pytest.raises(Ambiguous) as e:
        run(demo, Plan(tables=["payroll"], aggregates=[Aggregate(func="sum", table="payroll", column="base_salary + bonus")]))
    assert e.value.options == ["base_salary", "bonus"]


def test_arithmetic_over_unknown_columns_refused_plainly(demo):
    with pytest.raises(PlanError, match="can't do arithmetic between columns") as e:
        run(demo, Plan(tables=["payroll"], aggregates=[Aggregate(func="sum", table="payroll", column="foo + bar")]))
    assert e.value.kind == "capability"



def test_hyphen_and_parens_in_real_column_names_are_not_arithmetic(con=None):
    from dataqa.loader import load_file, open_connection
    from dataqa.plan import Aggregate, Catalog, GroupBy, Plan, looks_like_arithmetic, validate
    from dataqa.engine import execute
    import pathlib, tempfile
    c = open_connection(None)
    d = pathlib.Path(tempfile.mkdtemp())
    (d / "survey.csv").write_text("Employee ID,Work-Life Balance Score,Salary (USD)\nE1,4,100\nE2,2,200\n")
    t = load_file(c, d / "survey.csv", set()).tables[0]
    cat = Catalog([t], [])
    plan = Plan(tables=["survey"], group_by=[GroupBy(table="survey", column="Employee ID")],
                aggregates=[Aggregate(func="max", table="survey", column="Work-Life Balance Score"),
                            Aggregate(func="sum", table="survey", column="Salary (USD)")])
    assert len(execute(c, validate(plan, cat), cat).rows) == 2
    assert not looks_like_arithmetic("Work-Life Balance Score") and not looks_like_arithmetic("Salary (USD)")
    assert looks_like_arithmetic("base_salary + bonus") and looks_like_arithmetic("a - b") and looks_like_arithmetic("a*b")



# --- overnight bug hunt additions (see OVERNIGHT.md) ------------------------------------

@pytest.fixture(scope="module")
def messy():
    """A table the way the Kaggle HR export actually looks: a value with trailing spaces and a
    low-cardinality status column whose values are all known to the catalog."""
    import pathlib, tempfile
    c = open_connection(None)
    d = pathlib.Path(tempfile.mkdtemp())
    (d / "staff.csv").write_text(
        "id,dept,status\n"
        "1,Production       ,Active\n"
        "2,Production       ,Voluntarily Terminated\n"
        "3,Sales,Active\n"
        "4,Sales,Terminated for Cause\n"
        "5,IT/IS,Active\n"
    )
    t = load_file(c, d / "staff.csv", set()).tables[0]
    return c, Catalog([t], [])


def test_text_filter_ignores_surrounding_whitespace_in_data(messy):
    """'how many employees in Production' answered 0 on the Kaggle set: the stored value is
    'Production       ' and the equality compared it untrimmed."""
    con, cat = messy
    plan = Plan(tables=["staff"], filters=[Filter(table="staff", column="dept", op="=", value="Production")],
                aggregates=[Aggregate(func="count", table="staff")])
    assert execute(con, validate(plan, cat), cat).scalar == 2
    plan = Plan(tables=["staff"], filters=[Filter(table="staff", column="dept", op="in", values=["production ", " sales"])],
                aggregates=[Aggregate(func="count", table="staff")])
    assert execute(con, validate(plan, cat), cat).scalar == 4
    plan = Plan(tables=["staff"], filters=[Filter(table="staff", column="dept", op="!=", value="Production")],
                aggregates=[Aggregate(func="count", table="staff")])
    assert execute(con, validate(plan, cat), cat).scalar == 3


def test_or_joined_value_on_equals_becomes_in(messy):
    """'attrition rate by department' came back 0.0 everywhere: the model wrote
    status = "Terminated for Cause or Voluntarily Terminated". Every part is a known value of
    the column, so it can only mean 'either'."""
    con, cat = messy
    cat.notes = []
    plan = Plan(tables=["staff"], group_by=[GroupBy(table="staff", column="dept")],
                aggregates=[Aggregate(func="ratio", table="staff", where_column="status", where_op="=",
                                      where_value="Terminated for Cause or Voluntarily Terminated", alias="attrition")])
    r = execute(con, validate(plan, cat), cat)
    got = dict(zip(r.rows["dept"].str.strip(), r.rows["attrition"].round(1)))
    assert got == {"Production": 50.0, "Sales": 50.0, "IT/IS": 0.0}
    assert any("any of" in n for n in cat.notes)
    # same thing as a row filter
    cat.notes = []
    plan = Plan(tables=["staff"], filters=[Filter(table="staff", column="status", op="=",
                                                  value="Terminated for Cause or Voluntarily Terminated")],
                aggregates=[Aggregate(func="count", table="staff")])
    assert execute(con, validate(plan, cat), cat).scalar == 2


def test_unknown_value_on_a_known_value_column_is_refused_not_zero(messy):
    """When the catalog knows every value of a column, a filter value that matches none of them
    is a mistake to report, not a silent 0."""
    con, cat = messy
    plan = Plan(tables=["staff"], filters=[Filter(table="staff", column="status", op="=", value="Resigned")],
                aggregates=[Aggregate(func="count", table="staff")])
    with pytest.raises(PlanError, match="No status is 'Resigned'") as e:
        validate(plan, cat)
    assert e.value.kind == "fixable" and "Voluntarily Terminated" in str(e.value)
    plan = Plan(tables=["staff"], group_by=[GroupBy(table="staff", column="dept")],
                aggregates=[Aggregate(func="ratio", table="staff", where_column="status", where_op="=", where_value="Left")])
    with pytest.raises(PlanError, match="No status is 'Left'"):
        validate(plan, cat)


def test_known_value_check_is_case_and_space_insensitive(messy):
    con, cat = messy
    plan = Plan(tables=["staff"], filters=[Filter(table="staff", column="status", op="in", values=["active", " terminated for cause "])],
                aggregates=[Aggregate(func="count", table="staff")])
    assert execute(con, validate(plan, cat), cat).scalar == 4


def test_fanout_refusal_for_a_row_count_names_the_table_not_a_blank(demo):
    """'how many employees by status' refused with 'I can't give a count of  together with
    attendance … Ask about  without attendance' — an empty column name in the sentence."""
    with pytest.raises(PlanError) as e:
        run(demo, Plan(tables=["employees", "attendance"], group_by=[GroupBy(table="attendance", column="status")],
                       aggregates=[Aggregate(func="count", table="employees")]))
    msg = str(e.value)
    assert "  " not in msg and "count of employees rows" in msg and "distinct" in msg


def test_fanout_refusal_grammar(demo):
    with pytest.raises(PlanError, match="an average of bonus") as e:
        run(demo, Plan(tables=["payroll", "attendance"], group_by=[GroupBy(table="attendance", column="status")],
                       aggregates=[Aggregate(func="avg", table="payroll", column="bonus")]))


def test_value_that_belongs_to_another_column_is_retargeted(messy):
    """'list active employees in sales' -> the model wrote BusinessUnit = 'Sales'. 'Sales' isn't
    a BusinessUnit; it is a value of exactly one other column (DepartmentType). That's the
    only reading, so the filter moves there and the note says so."""
    con, cat = messy
    con.execute("ALTER TABLE staff ADD COLUMN unit VARCHAR DEFAULT 'BPC'")
    cat.tables[0].columns.append(type(cat.tables[0].columns[0])("unit", "VARCHAR", 0.0, 1, samples=["BPC"]))
    try:
        cat.notes = []
        plan = Plan(tables=["staff"], filters=[Filter(table="staff", column="unit", op="=", value="Sales")],
                    aggregates=[Aggregate(func="count", table="staff")])
        r = execute(con, validate(plan, cat), cat)
        assert r.scalar == 2 and plan.filters[0].column == "dept"
        assert any("'Sales' is a dept, not a unit" in n for n in cat.notes)
    finally:
        con.execute("ALTER TABLE staff DROP COLUMN unit"); cat.tables[0].columns.pop()


@pytest.mark.parametrize("value,start,end", [
    ("2025-H1", date(2025, 1, 1), date(2025, 7, 1)),
    ("2025H2", date(2025, 7, 1), date(2026, 1, 1)),
    ("H1 2025", date(2025, 1, 1), date(2025, 7, 1)),
])
def test_half_year_dates(value, start, end):
    """'which 3 employees took the most leave in H1' -> the model wrote 2025-H1 and was refused."""
    assert date_range(value) == (start, end)
