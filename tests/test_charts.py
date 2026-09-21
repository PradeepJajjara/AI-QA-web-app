from pathlib import Path

import pytest

from dataqa.charts import chart_for, chart_with_reason
from dataqa.engine import execute
from dataqa.joins import confirm, default_selection, detect_joins
from dataqa.loader import load_file, open_connection
from dataqa.plan import Aggregate, Catalog, Filter, GroupBy, Plan, validate

DEMO = Path(__file__).parent.parent / "data" / "demo"


@pytest.fixture(scope="module")
def demo():
    con = open_connection(None)
    tables = []
    for f in ["employees.csv", "payroll.csv", "attendance.xlsx"]:
        tables += load_file(con, DEMO / f, {t.name for t in tables}).tables
    return con, Catalog(tables, [confirm(c) for c in default_selection(detect_joins(con, tables))])


def run(demo, plan):
    con, cat = demo
    return execute(con, validate(plan, cat), cat)


def test_scalar_has_no_chart(demo):
    r = run(demo, Plan(tables=["employees"], aggregates=[Aggregate(func="count")]))
    assert chart_for(r) is None


def test_listing_has_no_chart(demo):
    r = run(demo, Plan(tables=["employees"], limit=5))
    assert chart_for(r) is None


def test_category_bar_single_series_no_legend(demo):
    r = run(demo, Plan(tables=["employees"], group_by=[GroupBy(table="employees", column="department")],
                       aggregates=[Aggregate(func="count")]))
    fig = chart_for(r)
    assert fig is not None and fig.data[0].type == "bar" and len(fig.data) == 1
    assert fig.layout.showlegend is False
    assert list(fig.data[0].x) == list(r.rows["department"])


def test_time_bucket_is_line_sorted(demo):
    r = run(demo, Plan(tables=["attendance"], group_by=[GroupBy(table="attendance", column="date", bucket="month")],
                       aggregates=[Aggregate(func="sum", table="attendance", column="hours")]))
    fig = chart_for(r)
    assert fig.data[0].type == "scatter" and fig.data[0].mode == "lines+markers"
    xs = list(fig.data[0].x)
    assert xs == sorted(xs) and len(xs) == 6


def test_time_plus_category_is_one_line_per_category_with_legend(demo):
    r = run(demo, Plan(tables=["attendance", "employees"],
                       filters=[Filter(table="attendance", column="status", op="=", value="Leave")],
                       group_by=[GroupBy(table="attendance", column="date", bucket="month"),
                                 GroupBy(table="employees", column="department")],
                       aggregates=[Aggregate(func="count", alias="leave_days")]))
    fig = chart_for(r)
    assert len(fig.data) == 5 and fig.layout.showlegend is True
    assert {d.name for d in fig.data} == set(r.rows["department"].unique())


def test_two_categories_grouped_bar(demo):
    r = run(demo, Plan(tables=["employees"],
                       group_by=[GroupBy(table="employees", column="department"), GroupBy(table="employees", column="grade")],
                       aggregates=[Aggregate(func="count")]))
    fig = chart_for(r)
    assert fig.layout.barmode == "group" and len(fig.data) == 4  # one per grade


def test_many_categories_still_chart(demo):
    r = run(demo, Plan(tables=["employees"], group_by=[GroupBy(table="employees", column="name")],
                       aggregates=[Aggregate(func="count")]))
    fig, why = chart_with_reason(r)
    assert fig is not None and len(fig.data[0].x) == 84 and why.startswith("Bar chart")



def test_single_row_result_has_no_chart(demo):
    from dataqa.plan import Sort
    r = run(demo, Plan(tables=["employees"], group_by=[GroupBy(table="employees", column="department")],
                       aggregates=[Aggregate(func="count", alias="n")], sort=Sort(by="n", desc=True), limit=1))
    assert len(r.rows) == 1 and chart_for(r) is None



def test_reasons_are_given_when_no_chart(demo):
    _, why = chart_with_reason(run(demo, Plan(tables=["employees"], aggregates=[Aggregate(func="count")])))
    assert why == "No chart: the answer is a single number."
    _, why = chart_with_reason(run(demo, Plan(tables=["employees"], limit=5)))
    assert why.startswith("No chart: the result is a list of rows")
    _, why = chart_with_reason(run(demo, Plan(tables=["employees"],
                                              group_by=[GroupBy(table="employees", column="department"), GroupBy(table="employees", column="grade"),
                                                        GroupBy(table="employees", column="location")],
                                              aggregates=[Aggregate(func="count")])))
    assert why.startswith("No chart: grouped by more than two columns")
    from dataqa.plan import Sort
    _, why = chart_with_reason(run(demo, Plan(tables=["employees"], group_by=[GroupBy(table="employees", column="department")],
                                              aggregates=[Aggregate(func="count", alias="n")], sort=Sort(by="n", desc=True), limit=1)))
    assert why == "No chart: single row."


def test_reasons_are_given_when_chart_drawn(demo):
    fig, why = chart_with_reason(run(demo, Plan(tables=["employees"], group_by=[GroupBy(table="employees", column="department")],
                                                aggregates=[Aggregate(func="count")])))
    assert fig is not None and why == "Bar chart: one category (department) with a numeric measure."
    fig, why = chart_with_reason(run(demo, Plan(tables=["attendance"], group_by=[GroupBy(table="attendance", column="date", bucket="month")],
                                                aggregates=[Aggregate(func="sum", table="attendance", column="hours")])))
    assert fig is not None and why == "Line chart: one value per month."



def test_forced_chart_draws_single_row_scalar_and_listing(demo):
    from dataqa.plan import Sort
    single = run(demo, Plan(tables=["employees"], group_by=[GroupBy(table="employees", column="department")],
                            aggregates=[Aggregate(func="count", alias="n")], sort=Sort(by="n", desc=True), limit=1))
    fig, why = chart_with_reason(single, force=True)
    assert fig is not None and len(fig.data[0].x) == 1 and "single value" in why
    scalar = run(demo, Plan(tables=["employees"], aggregates=[Aggregate(func="count")]))
    fig, why = chart_with_reason(scalar, force=True)
    assert fig is not None and why.startswith("Bar chart, as asked")
    listing = run(demo, Plan(tables=["payroll"], limit=5))
    fig, why = chart_with_reason(listing, force=True)
    assert fig is not None and len(fig.data[0].x) == 5 and "base_salary" in why
    assert chart_with_reason(listing, force=False)[0] is None  # not asked -> suppressed with a reason


# --- overnight bug hunt additions (see OVERNIGHT.md) ------------------------------------

def test_chart_reasons_use_plain_words(demo):
    """'values bucketed by month', 'two time buckets' — 'bucket' is our word, not the user's."""
    from dataqa.plan import Aggregate, GroupBy, Plan, validate
    from dataqa.engine import execute
    con, cat = demo
    p = Plan(tables=["payroll"], group_by=[GroupBy(table="payroll", column="pay_month", bucket="month")],
             aggregates=[Aggregate(func="sum", table="payroll", column="bonus")])
    _, why = chart_with_reason(execute(con, validate(p, cat), cat))
    assert "bucket" not in why and "per month" in why
    p = Plan(tables=["payroll"], group_by=[GroupBy(table="payroll", column="pay_month", bucket="month"),
                                           GroupBy(table="payroll", column="pay_month", bucket="quarter")],
             aggregates=[Aggregate(func="sum", table="payroll", column="bonus")])
    fig, why = chart_with_reason(execute(con, validate(p, cat), cat))
    assert fig is None and "bucket" not in why
