"""Compile a validated Plan to SQL and run it on DuckDB.

Values are always bound as parameters — the plan is data, never interpolated.
Joins come from the applied links only; the compiler finds the path between the
plan's tables and reports which links it used, so the answer can disclose them.
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date, timedelta

import duckdb
import pandas as pd

from dataqa.joins import ConfirmedJoin
from dataqa.loader import _q
from dataqa.plan import DEFAULT_ROW_LIMIT, MAX_LIMIT, Catalog, Filter, Plan, PlanError, agg_alias, group_alias


@dataclass
class Compiled:
    sql: str
    params: list
    used_links: list[ConfirmedJoin]
    tables: list[str]  # all tables in the FROM clause, incl. intermediates


@dataclass
class QueryResult:
    plan: Plan
    compiled: Compiled
    rows: pd.DataFrame
    elapsed_ms: float
    truncated: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def scalar(self):
        """Single-cell answers (e.g. a total) come back as the value itself."""
        if self.rows.shape == (1, 1):
            return self.rows.iat[0, 0]
        return None


# --- join path -------------------------------------------------------------------

def join_path(tables: list[str], links: list[ConfirmedJoin]) -> tuple[list[str], list[ConfirmedJoin]]:
    """Order tables and pick the links to connect them (BFS over the link graph).
    Intermediate tables are pulled in as needed. Raises PlanError if disconnected."""
    if len(tables) == 1:
        return list(tables), []
    adj: dict[str, list[tuple[str, ConfirmedJoin]]] = {}
    for l in links:
        adj.setdefault(l.left_table, []).append((l.right_table, l))
        adj.setdefault(l.right_table, []).append((l.left_table, l))

    order = [tables[0]]
    used: list[ConfirmedJoin] = []
    for target in tables[1:]:
        if target in order:
            continue
        # BFS from any already-included table to the target
        prev: dict[str, tuple[str, ConfirmedJoin] | None] = {t: None for t in order}
        dq = deque(order)
        found = False
        while dq and not found:
            cur = dq.popleft()
            for nxt, link in adj.get(cur, []):
                if nxt in prev:
                    continue
                prev[nxt] = (cur, link)
                if nxt == target:
                    found = True
                    break
                dq.append(nxt)
        if not found:
            raise PlanError(f"{tables[0]} and {target} aren't linked, so I can't combine them. "
                            f"Set which columns link them under the answer.", kind="capability")
        # walk back, adding tables/links in order
        chain = []
        node = target
        while prev[node] is not None:
            p, link = prev[node]
            chain.append((node, link))
            node = p
        for node, link in reversed(chain):
            if node not in order:
                order.append(node)
                used.append(link)
    return order, used


def fanout_check(plan: Plan, order: list[str], used: list[ConfirmedJoin]) -> None:
    """Refuse sums/averages/counts that a join would inflate.

    Joining payroll (12 rows per employee) to attendance (~130 rows per employee) repeats
    every payroll row ~130 times; sum(bonus) then looks plausible and is wrong by 130x.
    For each additive aggregate on table T, root the join tree at T: if any link's far
    side is a many-side, T's rows are duplicated. count_distinct / min / max are immune."""
    additive = [a for a in plan.aggregates if a.func in ("sum", "avg", "count", "ratio")]
    if not additive or len(order) < 2:
        return
    adj: dict[str, list[tuple[str, ConfirmedJoin]]] = {}
    for l in used:
        adj.setdefault(l.left_table, []).append((l.right_table, l))
        adj.setdefault(l.right_table, []).append((l.left_table, l))
    def inflated_by(root: str) -> str | None:
        """Name of the first table whose join repeats `root` rows, or None."""
        seen, dq = {root}, deque([root])
        while dq:
            cur = dq.popleft()
            for nxt, link in adj.get(cur, []):
                if nxt in seen:
                    continue
                seen.add(nxt)
                dq.append(nxt)
                if link.many_side(nxt):
                    return nxt
        return None

    for a in additive:
        if not a.column and not a.table:  # count(*): fine if some table sees every other as a one-side
            if any(inflated_by(t) is None for t in order):
                continue
            raise PlanError("Counting rows across these files would double-count: each row on one side "
                            "matches several on the other. Try 'how many distinct employees' instead.", kind="capability")
        culprit = inflated_by(a.table)
        if culprit:
            verb = "a total" if a.func == "sum" else "an average" if a.func == "avg" else "a percentage" if a.func == "ratio" else "a count"
            what = a.column or f"{a.table} rows"
            raise PlanError(
                f"I can't give {verb} of {what} together with {culprit}: each {a.table} row would be "
                f"counted once per matching {culprit} row, inflating the result. Ask about {what} without "
                f"{culprit}, or ask for a distinct count.", kind="capability"
            )


# --- value coercion --------------------------------------------------------------

_DATE_RE = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?$")
_QTR_RE = re.compile(r"^(\d{4})-?[Qq]([1-4])$|^[Qq]([1-4])\s*(\d{4})$")
_HALF_RE = re.compile(r"^(\d{4})-?[Hh]([12])$|^[Hh]([12])\s*(\d{4})$")
_MONTH_RE = re.compile(r"^([A-Za-z]{3,9})\s+(\d{4})$")
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def date_range(value: str) -> tuple[date, date]:
    """'2025' -> [2025-01-01, 2026-01-01); '2025-03' -> March; '2025-Q1' -> Q1; full date -> that day."""
    v = value.strip()
    if m := _HALF_RE.match(v):  # '2025-H1', 'H2 2025'
        y, h = (int(m[1]), int(m[2])) if m[1] else (int(m[4]), int(m[3]))
        return (date(y, 1, 1), date(y, 7, 1)) if h == 1 else (date(y, 7, 1), date(y + 1, 1, 1))
    if m := _QTR_RE.match(v):
        y, q = (int(m[1]), int(m[2])) if m[1] else (int(m[4]), int(m[3]))
        start = date(y, 3 * (q - 1) + 1, 1)
        end = date(y + 1, 1, 1) if q == 4 else date(y, 3 * q + 1, 1)
        return start, end
    if m := _DATE_RE.match(v):
        y, mo, d = int(m[1]), m[2] and int(m[2]), m[3] and int(m[3])
        try:
            if d:
                s = date(y, mo, d)
                return s, s + timedelta(days=1)
            if mo:
                s = date(y, mo, 1)
                return s, date(y + 1, 1, 1) if mo == 12 else date(y, mo + 1, 1)
            return date(y, 1, 1), date(y + 1, 1, 1)
        except ValueError as e:
            raise PlanError(f"'{value}' is not a valid date: {e}") from e
    if m := _MONTH_RE.match(v):  # 'March 2025', 'Mar 2025'
        mo = _MONTHS.get(m[1].lower()[:3])
        if mo:
            y = int(m[2])
            return date(y, mo, 1), (date(y + 1, 1, 1) if mo == 12 else date(y, mo + 1, 1))
    raise PlanError(f"I couldn't read '{value}' as a date. Try 2025, 2025-03, March 2025, 2025-Q1, 2025-H1 or 2025-03-15.")


def coerce(value: str, col) -> object:
    if col.is_numeric:
        try:
            return float(value) if ("." in value or "e" in value.lower()) else int(value)
        except ValueError:
            raise PlanError(f"'{value}' isn't a number, and {col.name} holds numbers. Give a number — e.g. '{col.name} > 10'.")
    return value


# --- compile ---------------------------------------------------------------------

def compile_plan(plan: Plan, cat: Catalog) -> Compiled:
    params: list = []
    order, used = join_path(plan.tables, cat.links)
    fanout_check(plan, order, used)

    def col(table: str, column: str) -> str:
        return f"{_q(table)}.{_q(column)}"

    # FROM + JOINs
    from_sql = _q(order[0])
    for t, link in zip(order[1:], used):
        l, r = col(link.left_table, link.left_col), col(link.right_table, link.right_col)
        if link.cast_to_text:
            l, r = f"trim(CAST({l} AS VARCHAR))", f"trim(CAST({r} AS VARCHAR))"
        from_sql += f"\n  JOIN {_q(t)} ON {l} = {r}"

    # SELECT / GROUP BY  (built first: their '?' placeholders precede the WHERE clause in the
    # final SQL, and DuckDB binds parameters positionally — order of append == order in text)
    select, group = [], []
    for g in plan.group_by:
        c = cat.column(g.table, g.column)
        expr = col(g.table, g.column)
        if g.bucket != "none":
            expr = f"date_trunc('{g.bucket}', CAST({expr} AS DATE))"
        select.append(f"{expr} AS {_q(group_alias(g))}")
        group.append(expr)
    for a in plan.aggregates:
        cond = None
        if a.conditional:
            cond = _filter_sql(Filter(table=a.table, column=a.where_column, op=a.where_op, value=a.where_value,
                                      values=a.where_values), cat, col, params)
        if a.func == "ratio":
            if a.column:  # share of a measure: sum where cond / sum all, as a percentage
                c = col(a.table, a.column)
                expr = f"100.0 * sum(CASE WHEN {cond} THEN {c} END) / NULLIF(sum({c}), 0)"
            else:  # share of rows: avg over 100/0 is NULL-safe and never divides by zero
                expr = f"avg(CASE WHEN {cond} THEN 100.0 ELSE 0.0 END)"
        elif not a.column:
            expr = "count(*)"
        elif a.func == "count_distinct":
            expr = f"count(DISTINCT {col(a.table, a.column)})"
        else:
            expr = f"{a.func}({col(a.table, a.column)})"
        if cond is not None and a.func != "ratio":
            expr += f" FILTER (WHERE {cond})"
        select.append(f"{expr} AS {_q(agg_alias(a))}")

    # WHERE (after SELECT so its parameters follow the SELECT's)
    where = []
    for f in plan.filters:
        where.append(_filter_sql(f, cat, col, params))

    limit = plan.limit or None
    if plan.is_listing:
        if plan.select:
            select = [f"{col(s.table, s.column)} AS {_q(s.column)}" for s in plan.select]
        else:
            select = [f"{_q(t)}.*" for t in plan.tables]
        limit = limit or DEFAULT_ROW_LIMIT

    # ORDER BY: explicit, else time ascending, else first aggregate descending
    order_sql = ""
    if plan.sort.by:
        order_sql = f"\nORDER BY {_q(plan.sort.by)} {'DESC' if plan.sort.desc else 'ASC'}"
    elif any(g.bucket != "none" for g in plan.group_by):
        order_sql = f"\nORDER BY {_q(group_alias(next(g for g in plan.group_by if g.bucket != 'none')))} ASC"
    elif plan.group_by and plan.aggregates:
        order_sql = f"\nORDER BY {_q(agg_alias(plan.aggregates[0]))} DESC"

    sql = f"SELECT {', '.join(select)}\nFROM {from_sql}"
    if where:
        sql += "\nWHERE " + "\n  AND ".join(where)
    if group:
        sql += "\nGROUP BY " + ", ".join(group)
    if plan.having.by:  # threshold on a measure: filter the grouped result
        params.append(float(plan.having.value))
        sql = f"SELECT * FROM (\n{sql}\n) AS grouped\nWHERE {_q(plan.having.by)} {plan.having.op} ?"
    sql += order_sql
    if limit:
        sql += f"\nLIMIT {min(limit, MAX_LIMIT)}"
    return Compiled(sql=sql, params=params, used_links=used, tables=order)


def _filter_sql(f: Filter, cat: Catalog, col, params: list) -> str:
    c = cat.column(f.table, f.column)
    expr = col(f.table, f.column)
    if f.op == "is null":
        return f"{expr} IS NULL"
    if f.op == "is not null":
        return f"{expr} IS NOT NULL"

    if c.is_temporal:
        # Partial dates become ranges: '2025-03' = March; 'between 2025-01 and 2025-03' = Jan..Mar.
        d = f"CAST({expr} AS DATE)"
        if f.op == "between":
            s, _ = date_range(f.values[0])
            _, e = date_range(f.values[1])
            params += [s, e]
            return f"{d} >= ? AND {d} < ?"
        if f.op in ("in", "not in"):
            parts = []
            for v in f.values:
                s, e = date_range(v)
                params += [s, e]
                parts.append(f"({d} >= ? AND {d} < ?)")
            joined = " OR ".join(parts)
            return f"({joined})" if f.op == "in" else f"NOT ({joined})"
        s, e = date_range(f.value)
        if f.op == "=":
            params += [s, e]
            return f"{d} >= ? AND {d} < ?"
        if f.op == "!=":
            params += [s, e]
            return f"NOT ({d} >= ? AND {d} < ?)"
        if f.op == ">=":
            params.append(s); return f"{d} >= ?"
        if f.op == ">":
            params.append(e); return f"{d} >= ?"
        if f.op == "<":
            params.append(s); return f"{d} < ?"
        if f.op == "<=":
            params.append(e); return f"{d} < ?"
        raise PlanError(f"'{f.op}' doesn't apply to dates.")

    text = not c.is_numeric
    if text:
        # Text matches ignore case and surrounding whitespace: the Kaggle HR export stores
        # 'Production       ', and 'how many in Production' must not answer 0.
        expr = f"trim(lower(CAST({expr} AS VARCHAR)))"
    norm = (lambda v: str(v).strip().lower()) if text else (lambda v: coerce(v, c))

    if f.op in ("in", "not in"):
        params.extend(norm(v) for v in f.values)
        ph = ", ".join("?" for _ in f.values)
        return f"{expr} {'IN' if f.op == 'in' else 'NOT IN'} ({ph})"
    if f.op == "between":
        params.extend(norm(v) for v in f.values)
        return f"{expr} BETWEEN ? AND ?"
    if f.op == "contains":
        params.append(f"%{norm(f.value)}%")
        return f"{expr} LIKE ?"
    params.append(norm(f.value))
    return f"{expr} {f.op} ?"


# --- execute ---------------------------------------------------------------------

def execute(con: duckdb.DuckDBPyConnection, plan: Plan, cat: Catalog) -> QueryResult:
    compiled = compile_plan(plan, cat)
    t0 = time.perf_counter()
    try:
        df = con.execute(compiled.sql, compiled.params).df()
    except duckdb.Error as e:
        first = next((l.strip() for l in str(e).splitlines() if l.strip()), "")
        first = re.sub(r"^\w+ Error:\s*", "", first)[:160]  # 'Binder Error: …' -> '…'; never the LINE/^^^ dump
        raise PlanError(f"Something went wrong running this question — {first}. Try rephrasing it.", kind="capability") from e
    elapsed = (time.perf_counter() - t0) * 1000
    truncated = len(df) >= MAX_LIMIT
    notes = []
    if getattr(plan, "_derive_share", False) and len(df.columns) == 2 and len(df):
        total = df.iloc[:, 1].sum()
        if total:
            df["share_pct"] = (df.iloc[:, 1] / total * 100).round(1)
    for t in compiled.tables:
        if t not in plan.tables:
            notes.append(f"Went through {t} to connect the files.")
    return QueryResult(plan=plan, compiled=compiled, rows=df, elapsed_ms=elapsed, truncated=truncated, notes=notes)
