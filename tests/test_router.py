from pathlib import Path

import pytest

from dataqa.joins import confirm, default_selection, detect_joins
from dataqa.loader import load_file, open_connection
from dataqa.plan import Aggregate, Catalog, GroupBy, Plan, PlanError, apply_choice
from dataqa.router import answer

DEMO = Path(__file__).parent.parent / "data" / "demo"


@pytest.fixture(scope="module")
def demo():
    con = open_connection(None)
    tables = []
    for f in ["employees.csv", "payroll.csv", "attendance.xlsx"]:
        tables += load_file(con, DEMO / f, {t.name for t in tables}).tables
    return con, Catalog(tables, [confirm(c) for c in default_selection(detect_joins(con, tables))])


class FakeModel:
    """Returns scripted plans; records retry calls."""
    name = "fake"

    def __init__(self, first: Plan, fixed: Plan | None = None):
        self.first, self.fixed, self.retries = first, fixed, []

    def plan(self, q, cat):
        return self.first.model_copy(deep=True)

    def retry(self, q, cat, failed, error):
        self.retries.append(error)
        if self.fixed is None:
            raise PlanError("still can't", kind="capability")
        return self.fixed.model_copy(deep=True)


def test_fixable_error_retried_once_and_logged(demo):
    con, cat = demo
    bad = Plan(tables=["payroll"], aggregates=[Aggregate(func="sum", table="payroll", column="zzz")])
    good = Plan(tables=["payroll"], aggregates=[Aggregate(func="sum", table="payroll", column="bonus")])
    m = FakeModel(bad, good)
    a = answer("what is the total zzz", con, cat, provider=m)
    assert a.error is None and a.path == "model:fake:retry"
    assert len(m.retries) == 1 and "has no column 'zzz'" in m.retries[0]
    assert [x.source for x in a.attempts] == ["model", "model"] and a.attempts[0].error and a.attempts[1].error is None


def test_retry_that_still_fails_is_a_refusal(demo):
    con, cat = demo
    bad = Plan(tables=["payroll"], aggregates=[Aggregate(func="sum", table="payroll", column="zzz")])
    a = answer("total zzz", con, cat, provider=FakeModel(bad, None))
    assert a.path == "refused" and a.error == "still can't" and len(a.attempts) == 2


def test_capability_error_not_retried(demo):
    con, cat = demo
    bad = Plan(tables=["payroll", "attendance"], aggregates=[Aggregate(func="sum", table="payroll", column="bonus")])
    m = FakeModel(bad, bad)
    a = answer("total bonus by attendance", con, cat, provider=m)
    assert a.path == "refused" and a.error_kind == "capability" and m.retries == []
    assert "counted once per matching attendance row" in a.error


def test_ambiguous_asks_and_choice_answers(demo):
    con, cat = demo
    amb = Plan(tables=["employees"], aggregates=[Aggregate(func="count_distinct", table="employees", column="dep")])
    m = FakeModel(amb, amb)
    a = answer("how many distinct dep", con, cat, provider=m)
    assert a.path == "ask" and m.retries == []  # the model can't resolve the user's intent
    assert "department" in a.ask.options
    chosen = apply_choice(a.ask.plan, a.ask.location, "department")
    b = answer("how many distinct dep", con, cat, provider=m, plan_override=chosen)
    assert b.error is None and b.result.scalar == 5 and b.path == "model:user-choice"


def test_template_path_never_retries(demo):
    con, cat = demo
    a = answer("how many employees", con, cat, provider=FakeModel(Plan(tables=["x"])))
    assert a.path == "template" and a.result.scalar == 120


def test_messages_are_user_facing(demo):
    con, cat = demo
    a = answer("x", con, cat, provider=FakeModel(Plan(tables=["employees"], aggregates=[Aggregate(func="count_distinct")]), None))
    assert a.attempts[0].error.startswith("I couldn't tell which column to count distinct values of")


def test_arithmetic_over_real_columns_asks(demo):
    con, cat = demo
    p = Plan(tables=["payroll"], group_by=[GroupBy(table="payroll", column="pay_month")],
             aggregates=[Aggregate(func="sum", table="payroll", column="base_salary + bonus")])
    a = answer("total salary by month", con, cat, provider=FakeModel(p, p))
    assert a.path == "ask" and a.ask.options == ["base_salary", "bonus"] and a.ask.substitute
    assert a.ask.question == ("I can't combine columns arithmetically yet, so I can't compute base_salary + bonus. "
                              "I can compute base_salary or bonus individually — that's not the same thing.")
    note = a.ask.note_for("base_salary")
    assert note == "Used base_salary only — not base_salary + bonus. That's a substitute, not what you asked."
    b = answer("total salary by month", con, cat, provider=None,
               plan_override=apply_choice(a.ask.plan, a.ask.location, "base_salary"), override_note=note)
    assert b.error is None and len(b.result.rows) == 12
    assert b.substitute and b.notes[0] == note


def test_ranking_substitute_says_ranked_by(demo):
    con, cat = demo
    from dataqa.plan import Sort
    p = Plan(tables=["employees", "payroll"],
             group_by=[GroupBy(table="employees", column="name")],
             aggregates=[Aggregate(func="sum", table="payroll", column="base_salary + bonus - deductions", alias="pay")],
             sort=Sort(by="pay"), limit=5)
    a = answer("5 highest paid employees", con, cat, provider=FakeModel(p, p))
    assert a.path == "ask" and a.ask.question.startswith("I can't combine columns arithmetically yet, so I can't rank by")
    assert a.ask.note_for("bonus") == ("Ranked by bonus only — not base_salary + bonus - deductions. "
                                       "That's a substitute, not what you asked.")


def test_genuine_ambiguity_is_not_a_substitute(demo):
    con, cat = demo
    amb = Plan(tables=["employees"], aggregates=[Aggregate(func="count_distinct", table="employees", column="dep")])
    a = answer("how many distinct dep", con, cat, provider=FakeModel(amb, amb))
    assert a.path == "ask" and not a.ask.substitute and a.ask.note_for("department") == "Read it as 'department'."


def test_arithmetic_over_unknown_columns_still_refuses(demo):
    con, cat = demo
    p = Plan(tables=["payroll"], aggregates=[Aggregate(func="sum", table="payroll", column="foo + bar")])
    a = answer("x", con, cat, provider=FakeModel(p, p))
    assert a.path == "refused" and a.error_kind == "capability"



# --- intent: top-N -------------------------------------------------------------------

def test_topn_without_sort_or_limit_is_repaired_when_one_measure(demo):
    con, cat = demo
    p = Plan(tables=["employees", "payroll"], group_by=[GroupBy(table="employees", column="name")],
             aggregates=[Aggregate(func="sum", table="payroll", column="base_salary", alias="pay")])
    m = FakeModel(p, p)
    a = answer("show me the 5 highest paid employees", con, cat, provider=m)
    assert a.error is None and m.retries == []
    assert len(a.result.rows) == 5 and a.plan.sort.by == "pay" and a.plan.sort.desc and a.plan.limit == 5
    assert {"Ranked by pay, highest first.", "Showing 5."} <= set(a.notes)
    assert list(a.result.rows["pay"]) == sorted(a.result.rows["pay"], reverse=True)


def test_lowest_ranks_ascending(demo):
    con, cat = demo
    p = Plan(tables=["employees", "payroll"], group_by=[GroupBy(table="employees", column="name")],
             aggregates=[Aggregate(func="sum", table="payroll", column="bonus", alias="b")])
    a = answer("3 lowest bonus earners", con, cat, provider=FakeModel(p, p))
    assert len(a.result.rows) == 3 and not a.plan.sort.desc


def test_topn_with_two_measures_is_fed_back_once(demo):
    con, cat = demo
    p = Plan(tables=["employees", "payroll"], group_by=[GroupBy(table="employees", column="name")],
             aggregates=[Aggregate(func="sum", table="payroll", column="bonus", alias="b"),
                         Aggregate(func="sum", table="payroll", column="deductions", alias="d")])
    from dataqa.plan import Sort
    fixed = p.model_copy(deep=True); fixed.sort = Sort(by="b", desc=True); fixed.limit = 5
    m = FakeModel(p, fixed)
    a = answer("top 5 employees by bonus", con, cat, provider=m)
    assert a.error is None and len(m.retries) == 1 and "couldn't tell what to rank by" in m.retries[0]
    assert len(a.result.rows) == 5


def test_which_has_most_gets_limit_one(demo):
    con, cat = demo
    p = Plan(tables=["employees"], group_by=[GroupBy(table="employees", column="location")],
             aggregates=[Aggregate(func="count", alias="n")])
    a = answer("which location has the most employees", con, cat, provider=FakeModel(p, p))
    assert len(a.result.rows) == 1 and "Showing the top result." in a.notes


def test_highest_as_max_needs_no_ranking(demo):
    con, cat = demo
    p = Plan(tables=["payroll"], aggregates=[Aggregate(func="max", table="payroll", column="bonus")])
    a = answer("highest bonus", con, cat, provider=FakeModel(p, p))
    assert a.error is None and a.result.scalar > 0 and a.notes == []


def test_desc_without_by_repaired_when_one_measure(demo):
    con, cat = demo
    from dataqa.plan import Sort
    p = Plan(tables=["employees"], group_by=[GroupBy(table="employees", column="location")],
             aggregates=[Aggregate(func="count")], sort=Sort(by="", desc=True))
    a = answer("employees per location", con, cat, provider=FakeModel(p, None))
    assert a.error is None and a.plan.sort.by == "count" and "Ranked by count, highest first." in a.notes


def test_desc_without_by_is_incomplete_when_ambiguous(demo):
    con, cat = demo
    from dataqa.plan import Sort
    p = Plan(tables=["payroll"], group_by=[GroupBy(table="payroll", column="staff_id")],
             aggregates=[Aggregate(func="sum", table="payroll", column="bonus"), Aggregate(func="sum", table="payroll", column="deductions")],
             sort=Sort(by="", desc=True))
    a = answer("employees by pay", con, cat, provider=FakeModel(p, None))
    assert a.path == "refused" and "couldn't tell what to rank by" in a.attempts[0].error


def test_template_plans_skip_intent_checks(demo):
    con, cat = demo
    a = answer("highest base_salary per grade", con, cat, provider=None)  # template: max by grade, 4 rows
    assert a.error is None and len(a.result.rows) == 4



def test_vague_aggregate_reading_is_disclosed(demo):
    con, cat = demo
    from dataqa.plan import Sort
    p = Plan(tables=["employees", "payroll"], group_by=[GroupBy(table="employees", column="department")],
             aggregates=[Aggregate(func="sum", table="payroll", column="base_salary")], sort=Sort(by="sum_base_salary", desc=True), limit=1)
    a = answer("which department has the highest salaries", con, cat, provider=FakeModel(p, p))
    assert a.notes == ["Read 'base_salary' as a total (sum) per group — ask for 'average base_salary' if you meant per person."]
    b = answer("which department has the highest total salaries", con, cat, provider=FakeModel(p, p))
    assert b.notes == []



def test_sort_by_bucketed_column_maps_to_bucket_alias(demo):
    con, cat = demo
    from dataqa.plan import Sort
    p = Plan(tables=["attendance"], group_by=[GroupBy(table="attendance", column="date", bucket="week")],
             aggregates=[Aggregate(func="count", alias="n")], sort=Sort(by="date", desc=False))
    a = answer("weekly count", con, cat, provider=FakeModel(p, p))
    assert a.error is None and a.plan.sort.by == "date_week" and len(a.result.rows) == 27


def test_pay_read_as_one_column_is_disclosed(demo):
    con, cat = demo
    from dataqa.plan import Sort
    p = Plan(tables=["employees", "payroll"], group_by=[GroupBy(table="employees", column="name")],
             aggregates=[Aggregate(func="max", table="payroll", column="base_salary", alias="highest")],
             sort=Sort(by="highest", desc=True), limit=5)
    a = answer("show me the 5 highest paid employees", con, cat, provider=FakeModel(p, p))
    assert a.error is None and len(a.result.rows) == 5
    assert "Read 'paid' as base_salary only — not including bonus, deductions." in a.notes



# --- declines are sanitised; our checks run on the model's best-attempt plan --------------

def test_decline_reason_contradicting_schema_is_suppressed(demo):
    from dataqa.providers import sanitise_reason, GENERIC_DECLINE
    con, cat = demo
    bad = "Hours is in the employees table, which has no dates. I can count rows per department instead."
    out = sanitise_reason(bad, cat)  # hours lives in attendance, not employees -> that sentence goes
    assert "employees table" not in out and out == "I can count rows per department instead."
    assert sanitise_reason("Bonus is in payroll, not employees.", cat) == "Bonus is in payroll, not employees."  # true claim kept
    assert sanitise_reason("hours is in payroll.", cat) == GENERIC_DECLINE  # nothing true survives


def test_decline_with_plan_runs_our_fanout_check_first(demo):
    from dataqa.providers import ModelDeclined
    con, cat = demo

    class Decliner:
        name = "fake"
        def plan(self, q, cat):
            raise ModelDeclined("Made-up reason about the schema.",
                                Plan(tables=["payroll", "attendance"], aggregates=[Aggregate(func="sum", table="payroll", column="bonus")]))
    a = answer("total bonus per attendance day", con, cat, provider=Decliner())
    assert a.path == "refused" and "counted once per matching attendance row" in a.error  # ours, not the model's


# --- ratio questions ----------------------------------------------------------------------

def test_percentage_over_plain_count_gets_share_column(demo):
    con, cat = demo
    p = Plan(tables=["attendance"], group_by=[GroupBy(table="attendance", column="status")], aggregates=[Aggregate(func="count", alias="n")])
    a = answer("what percentage of attendance records are WFH", con, cat, provider=FakeModel(p, p))
    assert a.error is None and list(a.result.rows.columns) == ["status", "n", "share_pct"]
    assert abs(a.result.rows["share_pct"].sum() - 100) < 0.2
    assert any("Percentages are of all rows" in n for n in a.notes)


def test_percentage_over_distinct_count_is_disclosed_not_derived(demo):
    con, cat = demo
    p = Plan(tables=["attendance"], group_by=[GroupBy(table="attendance", column="status")],
             aggregates=[Aggregate(func="count_distinct", table="attendance", column="emp_id", alias="people")])
    a = answer("what percentage of employees were on leave", con, cat, provider=FakeModel(p, p))
    assert a.error is None and "share_pct" not in a.result.rows.columns
    assert any(n.startswith("Answered with number of distinct of emp_id — not a percentage") for n in a.notes)



# --- intent: trend --------------------------------------------------------------------------

def test_monthly_question_with_invented_month_filter_is_fed_back(demo):
    con, cat = demo
    from dataqa.plan import Filter
    bad = Plan(tables=["payroll"], filters=[Filter(table="payroll", column="pay_month", op="=", value="2025-01")],
               aggregates=[Aggregate(func="sum", table="payroll", column="base_salary")])
    good = Plan(tables=["payroll"], group_by=[GroupBy(table="payroll", column="pay_month", bucket="month")],
                aggregates=[Aggregate(func="sum", table="payroll", column="base_salary")])
    m = FakeModel(bad, good)
    a = answer("monthly total base salary", con, cat, provider=m)
    assert a.error is None and len(a.result.rows) == 12 and a.path.endswith(":retry")
    assert "doesn't mention a date, but the proposal filters pay_month to '2025-01'" in m.retries[0]


def test_monthly_question_without_bucket_is_repaired_when_one_date_column(demo):
    con, cat = demo
    p = Plan(tables=["payroll"], aggregates=[Aggregate(func="sum", table="payroll", column="base_salary")])
    a = answer("monthly total base salary", con, cat, provider=FakeModel(p, p))
    assert a.error is None and len(a.result.rows) == 12
    assert "Grouped by pay_month per month" in " ".join(a.notes)


def test_trend_with_a_real_date_filter_is_fine(demo):
    con, cat = demo
    from dataqa.plan import Filter
    p = Plan(tables=["attendance"], filters=[Filter(table="attendance", column="date", op="=", value="2025-Q1")],
             group_by=[GroupBy(table="attendance", column="date", bucket="week")], aggregates=[Aggregate(func="count")])
    a = answer("weekly attendance count in Q1 2025", con, cat, provider=FakeModel(p, p))
    assert a.error is None and len(a.result.rows) == 14 and not any("Grouped by" in n for n in a.notes)



def test_decline_with_a_valid_plan_runs_the_plan(demo):
    """The model argued against a plan that validates and compiles. The validator wins."""
    from dataqa.providers import ModelDeclined
    con, cat = demo
    from dataqa.plan import Sort
    good = Plan(tables=["employees", "payroll"], group_by=[GroupBy(table="employees", column="name")],
                aggregates=[Aggregate(func="avg", table="payroll", column="base_salary", alias="avg_sal"),
                            Aggregate(func="avg", table="payroll", column="bonus", alias="avg_bonus")],
                sort=Sort(by="avg_sal", desc=True))

    class Doubter:
        name = "fake"
        def plan(self, q, cat):
            raise ModelDeclined("This needs a per-employee comparison a single group-by can't express.", good)
    a = answer("which employee has the highest salary but lowest bonus", con, cat, provider=Doubter())
    assert a.error is None and a.path == "model:fake"
    assert len(a.result.rows) == 1 and "Showing the top result." in a.notes  # 'which employee' -> top 1
    assert a.notes[0].startswith("The model wasn't sure this answers the question:")
    assert any("passed every check" in (x.error or "") for x in a.attempts)


def test_decline_with_invalid_plan_is_still_a_decline(demo):
    from dataqa.providers import ModelDeclined
    con, cat = demo
    bad = Plan(tables=["payroll"], aggregates=[Aggregate(func="sum", table="payroll", column="zzz")])

    class Doubter:
        name = "fake"
        def plan(self, q, cat):
            raise ModelDeclined("Can't do it.", bad)
    a = answer("total zzz", con, cat, provider=Doubter())
    assert a.path == "refused" and a.error == "Can't do it."


def test_describe_plan_reads_as_english(demo):
    from dataqa.plan import Filter, Sort, describe_plan
    p = Plan(tables=["employees", "payroll"],
             filters=[Filter(table="employees", column="department", op="in", values=["Sales", "HR"])],
             group_by=[GroupBy(table="employees", column="department")],
             aggregates=[Aggregate(func="avg", table="payroll", column="base_salary"), Aggregate(func="avg", table="payroll", column="bonus")],
             sort=Sort(by="avg_base_salary", desc=True), limit=3)
    assert describe_plan(p) == ("Group employees and payroll by department; average base_salary; average bonus; "
                                "where department is one of Sales, HR; sort by avg_base_salary, highest first; top 3.")
    assert describe_plan(Plan(tables=["employees"], aggregates=[Aggregate(func="count")])) == "From employees; count of rows."



# --- ratios, conditional aggregates, thresholds ---------------------------------------------

def test_ratio_of_rows_per_group(demo):
    con, cat = demo
    p = Plan(tables=["attendance"], group_by=[GroupBy(table="attendance", column="emp_id")],
             aggregates=[Aggregate(func="ratio", table="attendance", where_column="status", where_op="=", where_value="Present")])
    a = answer("attendance rate per employee", con, cat, provider=FakeModel(p, p))
    assert a.error is None and list(a.result.rows.columns) == ["emp_id", "pct_present"]
    exp = con.execute("SELECT emp_id, avg(CASE WHEN status='Present' THEN 100.0 ELSE 0 END) FROM attendance GROUP BY 1").fetchall()
    got = {r[0]: round(r[1], 4) for r in a.result.rows.itertuples(index=False, name=None)}
    assert got == {k: round(v, 4) for k, v in exp}
    assert not any("not a percentage" in n for n in a.notes)  # a real ratio, no disclosure needed


def test_threshold_on_a_ratio(demo):
    con, cat = demo
    from dataqa.plan import Having, Sort
    p = Plan(tables=["attendance"], group_by=[GroupBy(table="attendance", column="emp_id")],
             aggregates=[Aggregate(func="ratio", table="attendance", where_column="status", where_op="=", where_value="Present", alias="pct_present")],
             having=Having(by="pct_present", op=">", value="75"), sort=Sort(by="pct_present", desc=True))
    a = answer("which employees have more than 75% attendance", con, cat, provider=FakeModel(p, p))
    exp = con.execute("SELECT count(*) FROM (SELECT emp_id, avg(CASE WHEN status='Present' THEN 100.0 ELSE 0 END) p FROM attendance GROUP BY 1) WHERE p > 75").fetchone()[0]
    assert a.error is None and len(a.result.rows) == exp > 0
    b = answer("how many employees have more than 75% attendance", con, cat, provider=FakeModel(p, p))
    assert b.headline == exp and len(b.result.rows) == exp  # the count is the answer; the rows are the detail
    assert b.notes[0].startswith("Counted the emp_id values over the threshold")
    assert a.headline is None  # "which employees" wants the list itself
    assert (a.result.rows["pct_present"] > 75).all()
    assert "? " not in a.result.compiled.sql.split("WHERE")[-1] or a.result.compiled.params[-1] == 75.0


def test_conditional_count_and_share_of_measure(demo):
    con, cat = demo
    p = Plan(tables=["attendance"], group_by=[GroupBy(table="attendance", column="emp_id")],
             aggregates=[Aggregate(func="count", table="attendance", where_column="status", where_op="=", where_value="Leave", alias="leave_days"),
                         Aggregate(func="ratio", table="attendance", column="hours", where_column="status", where_op="=", where_value="WFH", alias="pct_hours_wfh")])
    a = answer("leave days and share of hours worked from home per employee", con, cat, provider=FakeModel(p, p))
    assert a.error is None
    exp = con.execute("SELECT count(*) FILTER (WHERE status='Leave') FROM attendance WHERE emp_id='E0001'").fetchone()[0]
    row = a.result.rows[a.result.rows.emp_id == "E0001"].iloc[0]
    assert row["leave_days"] == exp and 0 <= row["pct_hours_wfh"] <= 100


def test_ratio_without_condition_is_refused_plainly(demo):
    con, cat = demo
    p = Plan(tables=["attendance"], group_by=[GroupBy(table="attendance", column="emp_id")], aggregates=[Aggregate(func="ratio", table="attendance")])
    m = FakeModel(p, None)
    a = answer("percentage per employee", con, cat, provider=m)
    assert a.path == "refused" and "needs a condition" in m.retries[0]  # fixable: fed back to the model once


def test_threshold_without_group_is_refused(demo):
    con, cat = demo
    from dataqa.plan import Having
    p = Plan(tables=["attendance"], aggregates=[Aggregate(func="ratio", table="attendance", where_column="status", where_op="=", where_value="Present", alias="p")],
             having=Having(by="p", op=">", value="75"))
    m = FakeModel(p, None)
    a = answer("more than 75% present", con, cat, provider=m)
    assert a.path == "refused" and "needs something to compare per group" in m.retries[0]


def test_describe_plan_ratio_and_having(demo):
    from dataqa.plan import Having, describe_plan
    p = Plan(tables=["attendance"], group_by=[GroupBy(table="attendance", column="emp_id")],
             aggregates=[Aggregate(func="ratio", table="attendance", where_column="status", where_op="=", where_value="Present", alias="pct_present")],
             having=Having(by="pct_present", op=">", value="75"))
    assert describe_plan(p) == "Group attendance by emp_id; percentage where status is Present; keep only where pct_present is more than 75."


def test_threshold_applied_twice_drops_the_row_filter(demo):
    """'locations with average base_salary above 150000': the model also filtered rows to
    base_salary >= 150000 before averaging — a biased average that looks normal."""
    con, cat = demo
    from dataqa.plan import Filter, Having
    p = Plan(tables=["employees", "payroll"],
             filters=[Filter(table="payroll", column="base_salary", op=">=", value="150000")],
             group_by=[GroupBy(table="employees", column="location")],
             aggregates=[Aggregate(func="avg", table="payroll", column="base_salary")],
             having=Having(by="avg_base_salary", op=">=", value="150000"))
    a = answer("which locations have an average base salary above 150000", con, cat, provider=FakeModel(p, p))
    assert a.error is None and a.plan.filters == []
    exp = con.execute("SELECT e.location FROM employees e JOIN payroll p ON e.employee_id=p.staff_id "
                      "GROUP BY 1 HAVING avg(p.base_salary) >= 150000").fetchall()
    assert sorted(a.result.rows["location"]) == sorted(r[0] for r in exp)
    assert any("not to each row first" in n for n in a.notes)



# --- parameter order, compare, entity count, degenerate group ------------------------------

def test_conditional_aggregate_with_filter_binds_params_in_sql_order(demo):
    """'% of attendance that is leave in Sales' produced NaN: the CASE's '?' came first in the
    SQL but its value was appended after the WHERE's. Compiler bug, silent wrong number."""
    con, cat = demo
    from dataqa.plan import Filter
    p = Plan(tables=["attendance", "employees"],
             filters=[Filter(table="employees", column="department", op="=", value="Sales")],
             aggregates=[Aggregate(func="ratio", table="attendance", where_column="status", where_op="=", where_value="Leave", alias="pct_leave")])
    a = answer("percentage of attendance records that are leave in Sales", con, cat, provider=FakeModel(p, p))
    exp = con.execute("SELECT avg(CASE WHEN a.status='Leave' THEN 100.0 ELSE 0 END) FROM attendance a JOIN employees e "
                      "ON a.emp_id=e.employee_id WHERE e.department='Sales'").fetchone()[0]
    assert a.error is None and round(float(a.result.scalar), 4) == round(exp, 4)
    assert a.result.compiled.params == ["leave", "sales"]  # CASE first, WHERE second — as in the SQL text


def test_vs_question_is_grouped_not_totalled(demo):
    con, cat = demo
    from dataqa.plan import Filter
    p = Plan(tables=["employees", "payroll"],
             filters=[Filter(table="employees", column="department", op="in", values=["Engineering", "Sales"])],
             aggregates=[Aggregate(func="sum", table="payroll", column="deductions")])
    a = answer("total deductions for engineering vs sales", con, cat, provider=FakeModel(p, p))
    assert a.error is None and len(a.result.rows) == 2 and "department" in a.result.rows.columns
    assert any("side by side" in n for n in a.notes)


def test_how_many_employees_over_payroll_counts_distinct(demo):
    con, cat = demo
    from dataqa.plan import Filter
    p = Plan(tables=["employees", "payroll"],
             filters=[Filter(table="payroll", column="base_salary", op=">", value="200000")],
             aggregates=[Aggregate(func="count", alias="employee_count")])
    a = answer("how many employees earn more than 200000 base salary", con, cat, provider=FakeModel(p, p))
    exp = con.execute("SELECT count(DISTINCT staff_id) FROM payroll WHERE base_salary > 200000").fetchone()[0]
    assert a.error is None and a.result.scalar == exp == 28
    assert any("Counted distinct employees" in n for n in a.notes)


def test_count_distinct_grouped_by_itself_is_collapsed(demo):
    con, cat = demo
    p = Plan(tables=["employees"], group_by=[GroupBy(table="employees", column="manager_id")],
             aggregates=[Aggregate(func="count_distinct", table="employees", column="manager_id")])
    a = answer("how many distinct managers", con, cat, provider=FakeModel(p, p))
    assert a.error is None and a.result.scalar == 10
    assert any("grouping by manager_id would give 1" in n for n in a.notes)


def test_trend_repair_prefers_the_filtered_date_column(demo):
    con, cat = demo
    from dataqa.plan import Filter
    p = Plan(tables=["attendance", "employees"],
             filters=[Filter(table="attendance", column="status", op="=", value="WFH"),
                      Filter(table="attendance", column="date", op="=", value="2025-Q2")],
             aggregates=[Aggregate(func="count", alias="wfh_count")])
    a = answer("weekly WFH count trend in Q2 2025", con, cat, provider=FakeModel(p, p))
    assert a.error is None and len(a.result.rows) == 14 and a.plan.group_by[0].column == "date"


def test_table_qualified_column_name_is_split_not_retried(demo):
    con, cat = demo
    from dataqa.plan import Filter
    p = Plan(tables=["payroll"], filters=[Filter(table="payroll", column="base_salary", op=">", value="200000")],
             aggregates=[Aggregate(func="count_distinct", table="payroll", column="employees.employee_id")])
    m = FakeModel(p, p)
    a = answer("how many employees earn more than 200000 base salary", con, cat, provider=m)
    assert a.error is None and a.result.scalar == 28 and m.retries == []
    assert a.plan.aggregates[0].table == "employees" and a.plan.aggregates[0].column == "employee_id"


# --- overnight bug hunt additions (see OVERNIGHT.md) ------------------------------------

def test_how_many_over_threshold_via_decline_path_gets_headline_count(demo):
    """Groq declines 'how many employees have more than 75% attendance' but supplies the
    threshold plan; the decline path ran it and returned 120 rows with no count."""
    from dataqa.providers import ModelDeclined
    from dataqa.plan import Having, Sort
    con, cat = demo
    p = Plan(tables=["attendance"], group_by=[GroupBy(table="attendance", column="emp_id")],
             aggregates=[Aggregate(func="ratio", table="attendance", where_column="status", where_op="=",
                                   where_value="Present", alias="pct_present")],
             having=Having(by="pct_present", op=">", value="75"), sort=Sort(by="pct_present", desc=True))

    class Doubter:
        name = "fake"
        def plan(self, q, cat):
            raise ModelDeclined("I can't count employees over a threshold in one plan.", p)
    a = answer("how many employees have more than 75% attendance", con, cat, provider=Doubter())
    exp = con.execute("SELECT count(*) FROM (SELECT emp_id, avg(CASE WHEN status='Present' THEN 100.0 ELSE 0 END) p "
                      "FROM attendance GROUP BY 1) WHERE p > 75").fetchone()[0]
    assert a.error is None
    assert a.headline == exp
    assert any(n.startswith("Counted the emp_id values over the threshold") for n in a.notes)


def test_how_many_over_threshold_phrasing_variants(demo):
    """'tell me how many …' and 'what is the number of …' are count questions too; 'which …' is not."""
    from dataqa.plan import Having, Sort
    con, cat = demo
    p = Plan(tables=["attendance"], group_by=[GroupBy(table="attendance", column="emp_id")],
             aggregates=[Aggregate(func="ratio", table="attendance", where_column="status", where_op="=",
                                   where_value="Present", alias="pct_present")],
             having=Having(by="pct_present", op=">", value="75"), sort=Sort(by="pct_present", desc=True))
    for q in ("tell me how many employees have more than 75% attendance",
              "what is the number of employees with attendance above 75%",
              "count of employees whose attendance is over 75%"):
        a = answer(q, con, cat, provider=FakeModel(p, p))
        assert a.error is None and a.headline == len(a.result.rows) > 1, q
    a = answer("which employees have more than 75% attendance", con, cat, provider=FakeModel(p, p))
    assert a.headline is None


def test_show_me_rows_answered_with_a_bare_count_becomes_a_listing(demo):
    """'show me employees in engineering with grade L3' came back as the number 5. A listing
    question answered by count(*) with nothing grouped is the right rows in the wrong shape."""
    from dataqa.plan import Filter
    con, cat = demo
    p = Plan(tables=["employees"], filters=[Filter(table="employees", column="department", op="=", value="Engineering"),
                                           Filter(table="employees", column="grade", op="=", value="L3")],
             aggregates=[Aggregate(func="count", table="employees")])
    for q in ("show me employees in engineering with grade L3", "list the employees in engineering with grade L3",
              "which employees are in engineering with grade L3"):
        a = answer(q, con, cat, provider=FakeModel(p, p))
        assert a.error is None, q
        assert a.plan.is_listing and len(a.result.rows) == 5 and "name" in a.result.rows.columns, q
        assert any("Listed the rows" in n for n in a.notes)
    # 'show me how many' really is a count
    a = answer("show me how many employees are in engineering with grade L3", con, cat, provider=FakeModel(p, p))
    assert a.result.scalar == 5


def test_null_word_on_a_known_value_column_is_read_as_is_null(demo):
    """'how many employees have no manager' -> the model wrote manager_id = 'none'. 'none' isn't
    a manager_id and the column has empties: it can only mean 'is empty'."""
    from dataqa.plan import Filter
    con, cat = demo
    p = Plan(tables=["employees"], filters=[Filter(table="employees", column="manager_id", op="=", value="none")],
             aggregates=[Aggregate(func="count", table="employees")])
    a = answer("how many employees have no manager", con, cat, provider=FakeModel(p, p))
    exp = con.execute("SELECT count(*) FROM employees WHERE manager_id IS NULL").fetchone()[0]
    assert a.error is None and a.result.scalar == exp > 0
    assert a.plan.filters[0].op == "is null" and any("is empty" in n for n in a.notes)
    p = Plan(tables=["employees"], filters=[Filter(table="employees", column="manager_id", op="!=", value="NULL")],
             aggregates=[Aggregate(func="count", table="employees")])
    a = answer("how many employees have a manager", con, cat, provider=FakeModel(p, p))
    assert a.error is None and a.result.scalar == 120 - exp and a.plan.filters[0].op == "is not null"


def test_template_how_many_entity_by_many_side_column_counts_distinct(demo):
    """'how many employees by status' is a template shape, and offline it refused (fan-out).
    The entity-count repair is deterministic and applies to template plans too."""
    con, cat = demo
    a = answer("how many employees by status", con, cat, provider=None)
    assert a.error is None and a.path == "template"
    exp = {r[0]: r[1] for r in con.execute("SELECT status, count(DISTINCT emp_id) FROM attendance GROUP BY 1").fetchall()}
    got = dict(zip(a.result.rows.iloc[:, 0], a.result.rows.iloc[:, 1]))
    assert got == exp
    assert any(n.startswith("Counted distinct employees") for n in a.notes)


def test_which_rows_answered_with_count_distinct_lists_the_values(demo):
    """'which employees were on leave on 2025-03-03' came back as the number 10."""
    from dataqa.plan import Filter
    con, cat = demo
    p = Plan(tables=["attendance"], filters=[Filter(table="attendance", column="date", op="=", value="2025-03-03"),
                                            Filter(table="attendance", column="status", op="=", value="Leave")],
             aggregates=[Aggregate(func="count_distinct", table="attendance", column="emp_id")])
    a = answer("which employees were on leave on 2025-03-03", con, cat, provider=FakeModel(p, p))
    exp = con.execute("SELECT count(DISTINCT emp_id) FROM attendance WHERE status='Leave' AND date >= '2025-03-03' AND date < '2025-03-04'").fetchone()[0]
    assert a.error is None and a.result.scalar is None
    assert len(a.result.rows) == exp > 1 and list(a.result.rows.columns) == ["emp_id"]
    assert any("Listed" in n for n in a.notes)


def test_unsupported_statistic_is_refused_even_when_a_plan_would_run(demo):
    """'correlation between hours and bonus': the model declined and offered total bonus per
    employee as its closest attempt. That plan runs, but it doesn't answer the question, and the
    'validator outranks the decline' rule turned a correct refusal into a plausible non-answer."""
    from dataqa.providers import ModelDeclined
    con, cat = demo
    fallback = Plan(tables=["employees", "payroll"], group_by=[GroupBy(table="employees", column="employee_id")],
                    aggregates=[Aggregate(func="sum", table="payroll", column="bonus")])

    class Doubter:
        name = "fake"
        def plan(self, q, cat):
            raise ModelDeclined("Correlation needs row-wise arithmetic.", fallback)
    a = answer("correlation between hours and bonus", con, cat, provider=Doubter())
    assert a.path == "refused" and a.error_kind == "capability" and a.result is None
    assert "correlation" in a.error.lower() and "average" in a.error  # says what it can do instead


@pytest.mark.parametrize("q,word", [
    ("what's the median salary", "median"),
    ("standard deviation of bonus by department", "standard deviation"),
    ("90th percentile of hours", "percentile"),
    ("average tenure by department", "tenure"),
    ("average age of employees", "age"),
])
def test_unsupported_statistic_answered_with_an_average_is_refused(demo, q, word):
    """A model that answers 'median salary' with avg(base_salary) produces a normal-looking wrong number."""
    con, cat = demo
    p = Plan(tables=["payroll"], aggregates=[Aggregate(func="avg", table="payroll", column="base_salary")])
    a = answer(q, con, cat, provider=FakeModel(p, p))
    assert a.path == "refused" and a.error_kind == "capability", q
    assert word in a.error.lower(), q


def test_statistic_word_that_names_a_real_column_is_not_refused(demo):
    """'average age' on a table that HAS an age column is an ordinary average."""
    con, cat = demo
    con.execute("ALTER TABLE employees ADD COLUMN age INTEGER DEFAULT 30")
    cat.tables[0].columns.append(type(cat.tables[0].columns[0])("age", "INTEGER", 0.0, 1))
    try:
        p = Plan(tables=["employees"], aggregates=[Aggregate(func="avg", table="employees", column="age")])
        a = answer("average age of employees", con, cat, provider=FakeModel(p, p))
        assert a.error is None and a.result.scalar == 30
        assert answer("average age", con, cat, provider=None).result.scalar == 30  # template path too
    finally:
        con.execute("ALTER TABLE employees DROP COLUMN age"); cat.tables[0].columns.pop()


def test_sum_of_text_message_reads_naturally(demo):
    a = answer("sum of name", con := demo[0], cat := demo[1], provider=None)
    assert a.error == "name holds text, so I can't add it up. Try counting it, or pick a numeric column."


def test_threshold_and_ratio_notes_use_words_not_function_names(demo):
    """'Applied the 150000 threshold to the avg of base_salary' / 'Answered with avg of hours' —
    'avg' and 'sum' are our function names."""
    from dataqa.plan import Filter, Having
    con, cat = demo
    p = Plan(tables=["employees", "payroll"], group_by=[GroupBy(table="employees", column="location")],
             filters=[Filter(table="payroll", column="base_salary", op=">", value="150000")],
             aggregates=[Aggregate(func="avg", table="payroll", column="base_salary", alias="avg_salary")],
             having=Having(by="avg_salary", op=">", value="150000"))
    a = answer("which locations have an average base salary above 150000", con, cat, provider=FakeModel(p, p))
    note = next(n for n in a.notes if n.startswith("Applied the"))
    assert " avg " not in note and "average of base_salary" in note
    p = Plan(tables=["attendance"], group_by=[GroupBy(table="attendance", column="status")],
             aggregates=[Aggregate(func="avg", table="attendance", column="hours")])
    a = answer("what percentage of hours by status", con, cat, provider=FakeModel(p, p))
    note = next(n for n in a.notes if n.startswith("Answered with"))
    assert "avg of" not in note and "average of hours" in note


def test_query_failure_message_is_not_a_database_dump(demo, monkeypatch):
    import duckdb
    con, cat = demo
    p = Plan(tables=["payroll"], aggregates=[Aggregate(func="sum", table="payroll", column="bonus")])

    class Boom:
        def execute(self, sql, params):
            raise duckdb.BinderException("Binder Error: Referenced column \"x\" not found in FROM clause!\nLINE 1: SELECT ...")
    a = answer("total bonus", Boom(), cat, provider=None)
    assert a.error.startswith("Something went wrong running this question")
    assert "LINE 1" not in a.error and "Binder Error" not in a.error


def test_trend_repair_drops_groups_the_question_never_asked_for(demo):
    """'weekly WFH count trend in Q2 2025': the model grouped by employee_id and name (no time
    bucket). The repair added the week bucket but kept the per-employee groups: 367 rows for a
    question that asked for one number per week."""
    from dataqa.plan import Filter, Sort
    con, cat = demo
    p = Plan(tables=["attendance", "employees"],
             filters=[Filter(table="attendance", column="status", op="=", value="WFH"),
                      Filter(table="attendance", column="date", op="=", value="2025-Q2")],
             group_by=[GroupBy(table="employees", column="employee_id"), GroupBy(table="employees", column="name")],
             aggregates=[Aggregate(func="count", alias="wfh_count")], sort=Sort(by="wfh_count", desc=False))
    a = answer("weekly WFH count trend in Q2 2025", con, cat, provider=FakeModel(p, p))
    exp = con.execute("SELECT date_trunc('week', CAST(date AS DATE)) w, count(*) FROM attendance WHERE status='WFH' "
                      "AND date >= '2025-04-01' AND date < '2025-07-01' GROUP BY 1 ORDER BY 1").fetchall()
    assert a.error is None and len(a.result.rows) == len(exp) == 14
    assert [g.column for g in a.plan.group_by] == ["date"]
    assert any("employee_id" in n and "name" in n for n in a.notes)
    # but a trend that names the breakdown keeps it
    p2 = p.model_copy(deep=True); p2.group_by = [GroupBy(table="employees", column="department")]
    a = answer("weekly WFH count trend by department in Q2 2025", con, cat, provider=FakeModel(p2, p2))
    assert [g.column for g in a.plan.group_by] == ["date", "department"]


def test_ratio_grouped_by_its_own_condition_column_is_collapsed(demo):
    """'what percentage of employees are in Sales' -> ratio where department = Sales, grouped by
    department: 100% for Sales and 0% for everyone else. The question wants one number."""
    con, cat = demo
    p = Plan(tables=["employees"], group_by=[GroupBy(table="employees", column="department")],
             aggregates=[Aggregate(func="ratio", table="employees", where_column="department", where_op="=",
                                   where_value="Sales", alias="pct_sales")])
    a = answer("what percentage of employees are in Sales", con, cat, provider=FakeModel(p, p))
    exp = con.execute("SELECT avg(CASE WHEN department='Sales' THEN 100.0 ELSE 0 END) FROM employees").fetchone()[0]
    assert a.error is None and a.result.scalar is not None and round(float(a.result.scalar), 4) == round(exp, 4)
    assert any("grouping by department" in n for n in a.notes)


def test_share_is_derived_for_a_single_sum_per_group(demo):
    """'what share of total bonus went to Engineering' answered with sum(bonus) by department and
    the note 'I can't compute ratios yet' — which is no longer true. A single sum per group can
    carry a share column exactly as a single count does."""
    con, cat = demo
    p = Plan(tables=["employees", "payroll"], group_by=[GroupBy(table="employees", column="department")],
             aggregates=[Aggregate(func="sum", table="payroll", column="bonus", alias="dept_bonus_sum")])
    a = answer("what share of total bonus went to Engineering", con, cat, provider=FakeModel(p, p))
    assert a.error is None and "share_pct" in a.result.rows.columns
    eng = a.result.rows.set_index("department").loc["Engineering", "share_pct"]
    exp = con.execute("SELECT 100.0 * sum(CASE WHEN e.department='Engineering' THEN p.bonus END) / sum(p.bonus) "
                      "FROM payroll p JOIN employees e ON e.employee_id=p.staff_id").fetchone()[0]
    assert abs(float(eng) - exp) < 0.1
    assert not any("can't compute ratios" in n for n in a.notes)
    assert any("Percentages are of the total" in n for n in a.notes)


def test_group_pinned_to_one_value_by_a_filter_is_dropped(demo):
    """'percentage of attendance records that are leave in Sales' -> ratio grouped by department
    AND filtered to department = Sales: one row, two columns, no scalar. The group can only ever
    hold one value, so it isn't a breakdown."""
    from dataqa.plan import Filter
    con, cat = demo
    p = Plan(tables=["attendance", "employees"],
             filters=[Filter(table="employees", column="department", op="=", value="Sales")],
             group_by=[GroupBy(table="employees", column="department")],
             aggregates=[Aggregate(func="ratio", table="attendance", where_column="status", where_op="=", where_value="Leave", alias="pct_leave")])
    a = answer("percentage of attendance records that are leave in Sales", con, cat, provider=FakeModel(p, p))
    exp = con.execute("SELECT avg(CASE WHEN a.status='Leave' THEN 100.0 ELSE 0 END) FROM attendance a "
                      "JOIN employees e ON a.emp_id=e.employee_id WHERE e.department='Sales'").fetchone()[0]
    assert a.error is None and a.result.scalar is not None and abs(float(a.result.scalar) - exp) < 1e-6
    assert any("Sales" in n and "department" in n for n in a.notes)
    # an 'in' filter (compare A and B) keeps its group
    p2 = p.model_copy(deep=True); p2.filters = [Filter(table="employees", column="department", op="in", values=["Sales", "HR"])]
    a = answer("percentage of leave records, Sales vs HR", con, cat, provider=FakeModel(p2, p2))
    assert len(a.result.rows) == 2


@pytest.mark.parametrize("q", ["employees older than 50", "how many employees are younger than 30", "staff aged over 40",
                               "employees who are 50 years old"])
def test_age_phrasings_are_refused_not_answered_with_a_date_cutoff(demo, q):
    """'employees older than 50' listed every employee (DOB <= 2023-12-31)."""
    from dataqa.plan import Filter
    con, cat = demo
    p = Plan(tables=["employees"], filters=[Filter(table="employees", column="join_date", op="<=", value="2023-12-31")])
    a = answer(q, con, cat, provider=FakeModel(p, p))
    assert a.path == "refused" and "date arithmetic" in a.error, q


def test_column_name_matching_up_to_punctuation_is_not_ambiguous(demo):
    """'application_date' for a column called 'Application Date' asked 'which did you mean:
    Application Date / Applicant ID?'. Same letters in the same order is the column."""
    from dataqa.plan import Filter
    con, cat = demo
    con.execute("ALTER TABLE employees ADD COLUMN \"Join Year\" INTEGER DEFAULT 2020")
    con.execute("ALTER TABLE employees ADD COLUMN \"Joined\" INTEGER DEFAULT 1")
    cat.tables[0].columns.append(type(cat.tables[0].columns[0])("Join Year", "INTEGER", 0.0, 1))
    cat.tables[0].columns.append(type(cat.tables[0].columns[0])("Joined", "INTEGER", 0.0, 1))
    try:
        p = Plan(tables=["employees"], filters=[Filter(table="employees", column="join_year", op="=", value="2020")],
                 aggregates=[Aggregate(func="count", table="employees")])
        a = answer("how many employees have join_year 2020", con, cat, provider=FakeModel(p, p))
        assert a.path != "ask" and a.error is None and a.result.scalar == 120
        assert a.plan.filters[0].column == "Join Year"
    finally:
        con.execute("ALTER TABLE employees DROP COLUMN \"Join Year\""); con.execute("ALTER TABLE employees DROP COLUMN \"Joined\"")
        cat.tables[0].columns.pop(); cat.tables[0].columns.pop()


def test_table_name_near_miss_is_resolved_like_a_column(demo):
    """The Kaggle file is employee_data; the model writes 'employees' and, on retry, 'employees'
    again. One strong match is a repair, not a refusal."""
    con, cat = demo
    import copy
    cat2 = copy.deepcopy(cat)
    cat2.tables[0].name = "employee_data"  # pretend the demo file was named like the Kaggle one
    con.execute("CREATE OR REPLACE VIEW employee_data AS SELECT * FROM employees")
    for l in list(cat2.links):
        cat2.links.remove(l)
    from dataqa.joins import ConfirmedJoin
    cat2.links.append(ConfirmedJoin("employee_data", "employee_id", "payroll", "staff_id", left_unique=True))
    try:
        p = Plan(tables=["employees"], aggregates=[Aggregate(func="count", table="employees")])
        a = answer("how many employees", con, cat2, provider=FakeModel(p, p))
        assert a.error is None and a.result.scalar == 120 and a.plan.tables == ["employee_data"]
        assert any("employee_data" in n and "employees" in n for n in a.notes)
    finally:
        con.execute("DROP VIEW employee_data")


def test_bare_group_by_on_a_by_question_gets_a_count(demo):
    """'employees hired in 2022 by department' -> group by department, no measure: a list of
    department names. 'by department' wants a number per department."""
    from dataqa.plan import Filter
    con, cat = demo
    p = Plan(tables=["employees"], filters=[Filter(table="employees", column="join_date", op="=", value="2022")],
             group_by=[GroupBy(table="employees", column="department")])
    a = answer("employees hired in 2022 by department", con, cat, provider=FakeModel(p, p))
    exp = {r[0]: r[1] for r in con.execute("SELECT department, count(*) FROM employees WHERE join_date >= '2022-01-01' AND join_date < '2023-01-01' GROUP BY 1").fetchall()}
    assert a.error is None and list(a.result.rows.columns) == ["department", "count"]
    assert dict(zip(a.result.rows["department"], a.result.rows["count"])) == exp
    assert any("Counted" in n for n in a.notes)
    # 'distinct departments of employees hired in 2022' really is the list
    a = answer("distinct departments of employees hired in 2022", con, cat, provider=FakeModel(p, p))
    assert list(a.result.rows.columns) == ["department"]


def test_table_name_matching_two_files_asks_and_the_choice_renames_everywhere(demo):
    """Kaggle: the model writes 'employees'; employee_data and employee_engagement_survey_data
    both match strongly. Ask once; the choice must apply to every place the plan named it."""
    import copy, pathlib, tempfile
    from dataqa.loader import load_file, open_connection
    from dataqa.joins import ConfirmedJoin
    c = open_connection(None)
    d = pathlib.Path(tempfile.mkdtemp())
    (d / "employee_data.csv").write_text("EmpID,Dept\n1,HR\n2,IT\n3,HR\n")
    (d / "employee_engagement_survey_data.csv").write_text("Employee ID,Score\n1,4\n2,2\n3,5\n")
    tables = []
    for f in ("employee_data.csv", "employee_engagement_survey_data.csv"):
        tables += load_file(c, d / f, {t.name for t in tables}).tables
    cat = Catalog(tables, [ConfirmedJoin("employee_data", "EmpID", "employee_engagement_survey_data", "Employee ID",
                                         left_unique=True, right_unique=True)])
    p = Plan(tables=["employee_engagement_survey_data", "employees"],
             group_by=[GroupBy(table="employees", column="Dept")],
             aggregates=[Aggregate(func="avg", table="employee_engagement_survey_data", column="Score")])
    a = answer("average score by department", c, cat, provider=FakeModel(p, p))
    assert a.path == "ask" and a.ask.options == ["employee_data", "employee_engagement_survey_data"]
    chosen = apply_choice(a.ask.plan, a.ask.location, "employee_data")
    assert "employees" not in chosen.tables and chosen.group_by[0].table == "employee_data"
    b = answer("average score by department", c, cat, provider=FakeModel(p, p), plan_override=chosen)
    assert b.error is None and dict(zip(b.result.rows["Dept"], b.result.rows["avg_Score"])) == {"HR": 4.5, "IT": 2.0}


def test_bad_number_in_a_filter_gets_the_one_retry(demo):
    """'applicants with more than 10 years experience' -> the model emitted the value '}'.
    The refusal came from compile-time coercion, after the retry window, so the model never got
    its one correction. A bad value is a fixable detail like an unknown column."""
    from dataqa.plan import Filter
    con, cat = demo
    bad = Plan(tables=["payroll"], filters=[Filter(table="payroll", column="bonus", op=">", value="}")],
               aggregates=[Aggregate(func="count", table="payroll")])
    good = Plan(tables=["payroll"], filters=[Filter(table="payroll", column="bonus", op=">", value="40000")],
                aggregates=[Aggregate(func="count", table="payroll")])
    m = FakeModel(bad, good)
    a = answer("how many payroll rows with bonus over 40000", con, cat, provider=m)
    assert a.error is None and a.path == "model:fake:retry" and len(m.retries) == 1
    assert "isn't a number" in m.retries[0]
    assert a.result.scalar == con.execute("SELECT count(*) FROM payroll WHERE bonus > 40000").fetchone()[0]


def test_named_period_overrides_the_models_bucket(demo):
    """'weekly WFH count trend in Q2 2025' bucketed by month: right counts for the wrong period."""
    from dataqa.plan import Filter
    con, cat = demo
    p = Plan(tables=["attendance"],
             filters=[Filter(table="attendance", column="status", op="=", value="WFH"),
                      Filter(table="attendance", column="date", op="=", value="2025-Q2")],
             group_by=[GroupBy(table="attendance", column="date", bucket="month")],
             aggregates=[Aggregate(func="count", alias="wfh_count")])
    a = answer("weekly WFH count trend in Q2 2025", con, cat, provider=FakeModel(p, p))
    assert a.error is None and a.plan.group_by[0].bucket == "week" and len(a.result.rows) == 14
    assert any("per week" in n and "month" in n for n in a.notes)
    # 'over time' names no period: the model's bucket stands
    a = answer("WFH count over time in Q2 2025", con, cat, provider=FakeModel(p, p))
    assert a.plan.group_by[0].bucket == "month"


# --- a word that names several columns: ask, don't pick ---------------------------------

@pytest.fixture(scope="module")
def survey(tmp_path_factory):
    """Two files shaped like the Kaggle HR set: three '… Score' columns in one, a camel-cased
    DepartmentType in the other, linked on differently named id columns."""
    import csv
    d = tmp_path_factory.mktemp("survey")
    with open(d / "staff.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["EmpID", "DepartmentType", "TerminationType", "Current Employee Rating"])
        for i in range(1, 41):
            w.writerow([i, ["Sales", "IT", "HR", "Ops"][i % 4], ["Unk", "Voluntary"][i % 2], i % 5 + 1])
    with open(d / "survey.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["Employee ID", "Engagement Score", "Satisfaction Score", "Work-Life Balance Score"])
        for i in range(1, 41):
            w.writerow([i, i % 5 + 1, (i * 3) % 5 + 1, (i * 7) % 5 + 1])
    con = open_connection(None)
    tables = []
    for name in ["staff.csv", "survey.csv"]:
        tables += load_file(con, d / name, {t.name for t in tables}).tables
    return con, Catalog(tables, [confirm(c) for c in default_selection(detect_joins(con, tables))])


def _score_plan(column="Engagement Score", alias="avg_engagement"):
    return Plan(tables=["staff", "survey"],
                group_by=[GroupBy(table="staff", column="DepartmentType")],
                aggregates=[Aggregate(func="avg", table="survey", column=column, alias=alias)])


def test_bare_word_matching_three_columns_asks_with_the_candidates(survey):
    """'score' is a word of three columns and nothing else in the question separates them. The
    model picked Engagement silently; the app asks instead, naming all three."""
    con, cat = survey
    a = answer("average score by department type", con, cat, provider=FakeModel(_score_plan(), _score_plan()))
    assert a.path == "ask" and a.error is None
    assert a.ask.options == ["Engagement Score", "Satisfaction Score", "Work-Life Balance Score"]
    assert "'score' matches 3 columns" in str(a.ask) and "Satisfaction Score" in str(a.ask)
    assert not a.ask.substitute  # a reading of what was meant, not a stand-in for something the plan can't do


def test_choice_runs_with_the_chosen_column_and_its_own_alias(survey):
    """After the button: the chosen column is what runs, the model's alias for its own pick goes,
    and the question isn't asked a second time."""
    from dataqa.plan import apply_choice
    con, cat = survey
    m = FakeModel(_score_plan(), _score_plan())
    a = answer("average score by department type", con, cat, provider=m)
    fixed = apply_choice(a.ask.plan, a.ask.location, "Satisfaction Score")
    b = answer("average score by department type", con, cat, provider=m,
               plan_override=fixed, override_note=a.ask.note_for("Satisfaction Score"))
    assert b.path == "model:user-choice" and b.error is None and b.ask is None
    assert b.plan.aggregates[0].column == "Satisfaction Score" and "avg_engagement" not in b.result.rows.columns
    assert "Satisfaction Score" in b.result.compiled.sql and "Engagement Score" not in b.result.compiled.sql
    assert b.notes[0] == "Read it as 'Satisfaction Score'."


def test_a_word_the_question_does_separate_is_not_asked(survey):
    """'average engagement score …' — 'engagement' singles one column out; the model's pick stands."""
    con, cat = survey
    a = answer("average engagement score by department type", con, cat, provider=FakeModel(_score_plan(), _score_plan()))
    assert a.path == "model:fake" and a.error is None and len(a.result.rows) == 4
    # 'type' is a word of DepartmentType and TerminationType, but 'department' settles it
    assert not any("matches" in n for n in a.notes)


def test_group_column_word_is_checked_too(survey):
    """'how many by type' — 'type' is a word of DepartmentType and TerminationType. The group
    column is a pick as much as the measure is."""
    con, cat = survey
    p = Plan(tables=["staff"], group_by=[GroupBy(table="staff", column="DepartmentType")],
             aggregates=[Aggregate(func="count", alias="n")])
    a = answer("how many staff by type", con, cat, provider=FakeModel(p, p))
    assert a.path == "ask" and a.ask.options == ["DepartmentType", "TerminationType"]
    assert a.ask.location == ("group_by", 0, "column")


@pytest.mark.skipif(not Path(r"E:\Persona\interview\archive").exists(), reason="Kaggle HR set not present")
def test_kaggle_average_score_by_department_type_asks():
    """The case as seen on Groq qwen3.8-27b: the model answered with Engagement Score, no ask."""
    from dataqa.hosted import GroqProvider  # noqa: F401  (the real provider isn't called; the plan is scripted)
    K = Path(r"E:\Persona\interview\archive")
    con = open_connection(None)
    tables = []
    for f in ["employee_data.csv", "employee_engagement_survey_data.csv"]:
        tables += load_file(con, K / f, {t.name for t in tables}).tables
    cat = Catalog(tables, [confirm(c) for c in default_selection(detect_joins(con, tables))])
    p = Plan(tables=["employee_data", "employee_engagement_survey_data"],
             group_by=[GroupBy(table="employee_data", column="DepartmentType")],
             aggregates=[Aggregate(func="avg", table="employee_engagement_survey_data", column="Engagement Score", alias="avg_engagement")])
    a = answer("average score by department type", con, cat, provider=FakeModel(p, p))
    assert a.path == "ask"
    assert a.ask.options == ["Engagement Score", "Satisfaction Score", "Work-Life Balance Score"]


def test_substitution_asks_when_the_pick_has_no_such_word(survey):
    """The plan Groq qwen3.8-27b produced: 'average score by department type' -> avg(Current
    Employee Rating) from the staff file. 'score' isn't a word of that column at all; the three
    columns it is a word of live in the linked survey file. Ask, spanning the link."""
    con, cat = survey
    p = Plan(tables=["staff"], group_by=[GroupBy(table="staff", column="DepartmentType")],
             aggregates=[Aggregate(func="avg", table="staff", column="Current Employee Rating", alias="avg_score")])
    a = answer("average score by department type", con, cat, provider=FakeModel(p, p))
    assert a.path == "ask" and a.error is None
    assert a.ask.options == ["Engagement Score", "Satisfaction Score", "Work-Life Balance Score"]
    assert "isn't in Current Employee Rating" in str(a.ask) and "in survey" in str(a.ask)
    assert a.ask.table_for("Satisfaction Score") == "survey"


def test_choice_from_another_file_joins_it_through_the_link(survey):
    """Picking a survey column for a staff-only plan: the slot moves to the survey table, the
    plan gains the table, the compiler joins on the applied link, and the link is disclosed."""
    from dataqa.plan import apply_choice
    con, cat = survey
    p = Plan(tables=["staff"], group_by=[GroupBy(table="staff", column="DepartmentType")],
             aggregates=[Aggregate(func="avg", table="staff", column="Current Employee Rating", alias="avg_score")])
    m = FakeModel(p, p)
    a = answer("average score by department type", con, cat, provider=m)
    fixed = apply_choice(a.ask.plan, a.ask.location, "Work-Life Balance Score", a.ask.table_for("Work-Life Balance Score"))
    b = answer("average score by department type", con, cat, provider=m,
               plan_override=fixed, override_note=a.ask.note_for("Work-Life Balance Score"))
    assert b.error is None and b.path == "model:user-choice" and len(b.result.rows) == 4
    assert b.plan.aggregates[0].table == "survey" and "survey" in b.plan.tables
    assert 'JOIN "survey"' in b.result.compiled.sql and len(b.result.compiled.used_links) == 1
    expected = con.execute('SELECT avg("Work-Life Balance Score") FROM staff JOIN survey ON staff."EmpID" = survey."Employee ID" WHERE "DepartmentType" = \'IT\'').fetchone()[0]
    row = b.result.rows[b.result.rows["DepartmentType"] == "IT"].iloc[0]
    assert abs(float(row.iloc[1]) - expected) < 1e-9


def test_a_value_word_is_not_mistaken_for_a_column(demo):
    """'total bonus in Sales' — 'sales' is a filter value, and no column has it as a word. Nothing
    to ask; and 'bonus' is the pick itself with no rival, so the answer runs untouched."""
    from dataqa.plan import Filter
    con, cat = demo
    p = Plan(tables=["payroll", "employees"],
             filters=[Filter(table="employees", column="department", op="=", value="Sales")],
             aggregates=[Aggregate(func="sum", table="payroll", column="bonus")])
    a = answer("total bonus in Sales", con, cat, provider=FakeModel(p, p))
    assert a.path == "model:fake" and a.error is None and a.result.scalar is not None


# --- a file the model made up: refuse from the real schema ------------------------------

@pytest.fixture(scope="module")
def hr_no_pay(tmp_path_factory):
    """Employee records and an applicants file, no payroll anywhere. 'Desired Salary' exists
    only for applicants — the nearest thing to a salary, and not the same thing."""
    import csv
    d = tmp_path_factory.mktemp("hr")
    with open(d / "employee_data.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["EmpID", "DepartmentType", "Current Employee Rating"])
        for i in range(1, 21):
            w.writerow([i, ["Sales", "IT"][i % 2], i % 5 + 1])
    with open(d / "recruitment_data.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["Applicant ID", "Desired Salary", "Job Title"])
        for i in range(1, 21):
            w.writerow([1000 + i, 50000 + i * 1000, "Analyst"])
    con = open_connection(None)
    tables = []
    for name in ["employee_data.csv", "recruitment_data.csv"]:
        tables += load_file(con, d / name, {t.name for t in tables}).tables
    return con, Catalog(tables, [confirm(c) for c in default_selection(detect_joins(con, tables))])


def test_invented_file_refusal_leads_with_the_users_word_and_the_closest_real_column(hr_no_pay):
    """The model answered 'average salary by department' with a payroll table that doesn't exist.
    The refusal never says 'payroll'; it says no file has a salary column and names the nearest."""
    con, cat = hr_no_pay
    p = Plan(tables=["payroll", "employee_data"],
             group_by=[GroupBy(table="employee_data", column="DepartmentType")],
             aggregates=[Aggregate(func="avg", table="payroll", column="salary", alias="avg_salary")])
    a = answer("average salary by department", con, cat, provider=FakeModel(p, p))
    assert a.path == "refused"
    assert a.error == "None of the loaded files has a salary column. The closest is Desired Salary in recruitment_data."
    assert "payroll" not in a.error and "looked for" not in a.error
    assert len(a.attempts) == 2  # fixable: the model got one retry, and made the same file up again


def test_invented_file_with_nothing_close_says_what_the_files_cover(hr_no_pay):
    con, cat = hr_no_pay
    p = Plan(tables=["finance"], group_by=[GroupBy(table="finance", column="region")],
             aggregates=[Aggregate(func="sum", table="finance", column="revenue")])
    a = answer("total revenue by region", con, cat, provider=FakeModel(p, p))
    assert a.path == "refused"
    assert a.error == "None of the loaded files has a revenue column. These files cover employee_data and recruitment_data."
    assert "finance" not in a.error


def test_invented_file_without_a_matching_word_falls_back_to_the_question(hr_no_pay):
    """The model's column name shares no word with the question ('headcount' -> 'staff_total');
    the refusal uses the question's own uncovered word rather than the model's."""
    con, cat = hr_no_pay
    p = Plan(tables=["hr_summary"], aggregates=[Aggregate(func="sum", table="hr_summary", column="staff_total")])
    a = answer("total headcount", con, cat, provider=FakeModel(p, p))
    assert a.path == "refused"
    assert a.error.startswith("None of the loaded files has a headcount column.")
    assert "staff_total" not in a.error and "hr_summary" not in a.error


@pytest.mark.skipif(not Path(r"E:\Persona\interview\archive").exists(), reason="Kaggle HR set not present")
def test_kaggle_average_salary_refusal_names_desired_salary():
    K = Path(r"E:\Persona\interview\archive")
    con = open_connection(None)
    tables = []
    for f in ["employee_data.csv", "employee_engagement_survey_data.csv", "training_and_development_data.csv", "recruitment_data.csv"]:
        tables += load_file(con, K / f, {t.name for t in tables}).tables
    cat = Catalog(tables, [confirm(c) for c in default_selection(detect_joins(con, tables))])
    p = Plan(tables=["payroll", "employee_data"],
             group_by=[GroupBy(table="employee_data", column="DepartmentType")],
             aggregates=[Aggregate(func="avg", table="payroll", column="salary", alias="avg_salary")])
    a = answer("average salary by department", con, cat, provider=FakeModel(p, p))
    assert a.error == "None of the loaded files has a salary column. The closest is Desired Salary in recruitment_data."
