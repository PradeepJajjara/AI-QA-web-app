import pytest

from dataqa.joins import default_selection, detect_joins, name_similarity, normalise_name
from dataqa.loader import describe_table, open_connection


@pytest.fixture
def con():
    return open_connection(None)


def make(con, name, sql):
    con.execute(f'CREATE TABLE "{name}" AS {sql}')
    return describe_table(con, name)


def find(cands, lt, lc, rt, rc):
    return next((c for c in cands if (c.left_table, c.left_col, c.right_table, c.right_col) == (lt, lc, rt, rc)), None)


# --- name normalisation -------------------------------------------------------

def test_normalise_strips_key_suffixes_and_case():
    assert normalise_name("Employee ID") == "employee"
    assert normalise_name("emp_id") == "emp"
    assert normalise_name("staffId") == "staff"
    assert normalise_name("id") == "id"  # never strip to nothing


def test_name_similarity_levels():
    assert name_similarity("employee_id", "EmployeeID") == 1.0
    assert name_similarity("employee_id", "emp_id") == 0.8  # prefix
    assert name_similarity("employee_id", "staff_id") < 0.5  # different word


# --- detection ---------------------------------------------------------------

def test_detects_by_name_and_values(con):
    emp = make(con, "emp", "SELECT 'E' || i AS employee_id, 'D' || (i % 3) AS dept FROM range(1, 51) t(i)")
    att = make(con, "att", "SELECT 'E' || (i % 50 + 1) AS emp_id, i AS hours FROM range(200) t(i)")
    cands = detect_joins(con, [emp, att])
    c = find(cands, "emp", "employee_id", "att", "emp_id")
    assert c is not None
    assert c.overlap == 1.0 and c.cardinality == "1:N" and not c.type_mismatch


def test_detects_by_values_alone_when_names_differ(con):
    """staff_id vs employee_id: name score is low, value overlap must carry it."""
    emp = make(con, "emp", "SELECT 'E' || i AS employee_id FROM range(1, 51) t(i)")
    pay = make(con, "pay", "SELECT 'E' || (i % 50 + 1) AS staff_id, i * 10 AS salary FROM range(100) t(i)")
    cands = detect_joins(con, [emp, pay])
    c = find(cands, "emp", "employee_id", "pay", "staff_id")
    assert c is not None and c.name_score < 0.5 and c.overlap == 1.0


def test_type_mismatch_flagged_and_still_matches(con):
    emp = make(con, "emp", "SELECT i AS employee_id FROM range(1, 21) t(i)")  # BIGINT
    pay = make(con, "pay", "SELECT CAST(i % 20 + 1 AS VARCHAR) AS employee_id, i AS amt FROM range(40) t(i)")
    c = find(detect_joins(con, [emp, pay]), "emp", "employee_id", "pay", "employee_id")
    assert c is not None and c.type_mismatch and c.overlap == 1.0
    assert any("types differ" in w for w in c.warnings)


def test_unrelated_columns_not_suggested(con):
    emp = make(con, "emp", "SELECT 'E' || i AS employee_id, 'Name' || i AS name FROM range(1, 51) t(i)")
    pay = make(con, "pay", "SELECT 'P' || i AS payslip_id, i AS amt FROM range(1, 51) t(i)")
    assert detect_joins(con, [emp, pay]) == []


def test_many_to_many_flagged_and_not_preselected(con):
    a = make(con, "a", "SELECT 'D' || (i % 3) AS department, i AS x FROM range(30) t(i)")
    b = make(con, "b", "SELECT 'D' || (i % 3) AS department, i AS y FROM range(30) t(i)")
    cands = detect_joins(con, [a, b])
    c = find(cands, "a", "department", "b", "department")
    assert c is not None and c.cardinality == "N:N"
    assert default_selection(cands) == []


def test_default_selection_one_per_pair_best_first(con):
    emp = make(con, "emp", "SELECT 'E' || i AS employee_id, 'M' || (i % 5) AS manager_id FROM range(1, 51) t(i)")
    att = make(con, "att", "SELECT 'E' || (i % 50 + 1) AS emp_id, 'M' || (i % 5) AS mgr FROM range(200) t(i)")
    picked = default_selection(detect_joins(con, [emp, att]))
    assert len(picked) == 1
    assert (picked[0].left_col, picked[0].right_col) == ("employee_id", "emp_id")


def test_partial_overlap_reported(con):
    emp = make(con, "emp", "SELECT 'E' || i AS employee_id FROM range(1, 101) t(i)")
    pay = make(con, "pay", "SELECT 'E' || i AS employee_id, i AS amt FROM range(1, 61) t(i)")  # 60 of 100
    c = find(detect_joins(con, [emp, pay]), "emp", "employee_id", "pay", "employee_id")
    assert c is not None and c.overlap == 1.0  # containment: all 60 pay keys exist in emp


def test_integer_sequences_need_name_evidence(con):
    """Two unrelated 0..N integer columns must not be proposed as a 1:1 join."""
    a = make(con, "a", "SELECT i AS row_no, 'x' AS p FROM range(30) t(i)")
    b = make(con, "b", "SELECT i AS seq, 'y' AS q FROM range(30) t(i)")
    assert detect_joins(con, [a, b]) == []


def test_match_line_counts_against_key_table(con):
    emp = make(con, "employees", "SELECT 'E' || i AS employee_id FROM range(1, 121) t(i)")
    pay = make(con, "payroll", "SELECT 'E' || (i % 118 + 1) AS staff_id, i AS amt FROM range(500) t(i)")
    c = find(detect_joins(con, [emp, pay]), "employees", "employee_id", "payroll", "staff_id")
    assert c.match_line() == "Linked employees to payroll on employee_id ↔ staff_id · 118 of 120 employees matched"


def test_unrelated_pairs_uses_graph_connectivity(con):
    from dataqa.joins import ConfirmedJoin, unrelated_pairs
    a = make(con, "a", "SELECT 1 AS x"); b = make(con, "b", "SELECT 1 AS x"); c = make(con, "c", "SELECT 1 AS x")
    d = make(con, "d", "SELECT 1 AS x")
    links = [ConfirmedJoin("a", "x", "b", "x"), ConfirmedJoin("a", "x", "c", "x")]
    # b and c are connected through a; d is on its own
    assert unrelated_pairs([a, b, c, d], links) == [("a", "d"), ("b", "d"), ("c", "d")]
    assert unrelated_pairs([a, b, c], links) == []
