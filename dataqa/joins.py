"""Detect candidate join keys between tables — deterministically, in code.

The model never guesses how files relate. Candidates come from name similarity,
value overlap and cardinality. The best one per table pair is applied automatically
and disclosed beneath every answer as "Linked A to B on key · X of N matched", with
an override. If nothing clears the threshold the app says so and asks — it never guesses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from itertools import combinations

import duckdb

from dataqa.loader import ColumnInfo, TableInfo, _q

MIN_OVERLAP = 0.5  # containment needed to suggest on values alone
MIN_NAME_FOR_WEAK_OVERLAP = 0.8  # with a strong name match, accept lower overlap
WEAK_OVERLAP = 0.2
KEY_SUFFIXES = ("id", "key", "code", "no", "num", "number", "pk", "fk")


@dataclass
class JoinCandidate:
    left_table: str
    left_col: str
    right_table: str
    right_col: str
    name_score: float
    overlap: float  # |L ∩ R| / min(|L|, |R|) on distinct non-null values
    cardinality: str  # "1:1" | "1:N" | "N:1" | "N:N"
    type_mismatch: bool  # dtypes differ; join must cast both sides to VARCHAR
    matched: int = 0  # distinct keys present on both sides
    left_distinct: int = 0
    right_distinct: int = 0

    @property
    def score(self) -> float:
        return round(0.7 * self.overlap + 0.3 * self.name_score, 3)

    @property
    def warnings(self) -> list[str]:
        w = []
        if self.cardinality == "N:N":
            w.append("many-to-many: joining will multiply rows")
        if self.type_mismatch:
            w.append("column types differ; values compared as text")
        if self.overlap < 0.9:
            w.append(f"only {self.overlap:.0%} of keys match")
        return w

    def describe(self) -> str:
        return f"{self.left_table}.{self.left_col} = {self.right_table}.{self.right_col}"

    def match_line(self) -> str:
        """Plain-language provenance: 'Linked employees to payroll on employee_id ↔ staff_id · 120 of 120 matched'.
        The count is against the side that looks like the key table (unique side), so a low
        number reads as 'most of these employees have no payroll rows'."""
        if self.cardinality in ("N:1",):
            table, total = self.right_table, self.right_distinct
        else:
            table, total = self.left_table, self.left_distinct
        on = self.left_col if self.left_col == self.right_col else f"{self.left_col} ↔ {self.right_col}"
        return (f"Linked {self.left_table} to {self.right_table} on {on} · "
                f"{self.matched} of {total} {table} matched")


@dataclass(frozen=True)
class ConfirmedJoin:
    left_table: str
    left_col: str
    right_table: str
    right_col: str
    cast_to_text: bool = False
    left_unique: bool = False  # is the key unique on this side? Drives the fan-out check.
    right_unique: bool = False

    def many_side(self, table: str) -> bool:
        """True if joining *towards* `table` can repeat rows from the other side."""
        return not (self.left_unique if table == self.left_table else self.right_unique)

    def describe(self) -> str:
        return f"{self.left_table}.{self.left_col} = {self.right_table}.{self.right_col}"


def normalise_name(name: str) -> str:
    """'Employee ID' -> 'employee', 'emp_id' -> 'emp', 'staffId' -> 'staff'."""
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)  # camelCase -> camel_Case
    s = re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")
    parts = [p for p in s.split("_") if p]
    while len(parts) > 1 and parts[-1] in KEY_SUFFIXES:
        parts.pop()
    return "_".join(parts) or s


def name_similarity(a: str, b: str) -> float:
    na, nb = normalise_name(a), normalise_name(b)
    if na == nb:
        return 1.0
    if na.startswith(nb) or nb.startswith(na):
        return 0.8
    return round(SequenceMatcher(None, na, nb).ratio(), 3)


def _joinable(c: ColumnInfo, rows: int) -> bool:
    if c.all_null or c.distinct < 2:
        return False
    if c.is_temporal or any(t in c.dtype for t in ("DOUBLE", "FLOAT", "DECIMAL")):
        return False
    return True


def _is_unique(c: ColumnInfo, rows: int) -> bool:
    return c.distinct == rows and c.null_frac == 0


def value_overlap(con: duckdb.DuckDBPyConnection, lt: str, lc: str, rt: str, rc: str) -> tuple[int, int, int]:
    """(matched, left_distinct, right_distinct) on distinct non-null values, compared as
    trimmed text so mismatched types still match."""
    sql = f"""
    WITH l AS (SELECT DISTINCT trim(CAST({_q(lc)} AS VARCHAR)) v FROM {_q(lt)} WHERE {_q(lc)} IS NOT NULL),
         r AS (SELECT DISTINCT trim(CAST({_q(rc)} AS VARCHAR)) v FROM {_q(rt)} WHERE {_q(rc)} IS NOT NULL)
    SELECT (SELECT count(*) FROM (SELECT v FROM l INTERSECT SELECT v FROM r)),
           (SELECT count(*) FROM l),
           (SELECT count(*) FROM r)
    """
    row = con.execute(sql).fetchone()
    if row is None:  # interrupted query on a shared connection; treat as no evidence
        return 0, 0, 0
    both, nl, nr = row
    return both, nl, nr


def detect_joins(con: duckdb.DuckDBPyConnection, tables: list[TableInfo]) -> list[JoinCandidate]:
    """All plausible join keys between every pair of tables, best first."""
    out: list[JoinCandidate] = []
    for lt, rt in combinations(tables, 2):
        for lc in lt.columns:
            if not _joinable(lc, lt.rows):
                continue
            for rc in rt.columns:
                if not _joinable(rc, rt.rows):
                    continue
                lu, ru = _is_unique(lc, lt.rows), _is_unique(rc, rt.rows)
                name = name_similarity(lc.name, rc.name)
                # Cheap prefilters, before the overlap query:
                #  - an N:N pair is only worth checking if names agree;
                #  - integer columns overlap by accident (row numbers, surrogate keys),
                #    so numeric pairs need name evidence too. Text IDs can match on values alone.
                if not lu and not ru and name < 0.5:
                    continue
                if lc.is_numeric and rc.is_numeric and name < 0.5:
                    continue
                matched, nl, nr = value_overlap(con, lt.name, lc.name, rt.name, rc.name)
                overlap = round(matched / min(nl, nr), 3) if min(nl, nr) else 0.0
                strong = overlap >= MIN_OVERLAP
                weak = name >= MIN_NAME_FOR_WEAK_OVERLAP and overlap >= WEAK_OVERLAP
                if not (strong or weak):
                    continue
                card = {(True, True): "1:1", (True, False): "1:N", (False, True): "N:1"}.get(
                    (lu, ru), "N:N"
                )
                out.append(
                    JoinCandidate(
                        lt.name, lc.name, rt.name, rc.name,
                        name_score=name, overlap=overlap, cardinality=card,
                        type_mismatch=lc.dtype != rc.dtype,
                        matched=matched, left_distinct=nl, right_distinct=nr,
                    )
                )
    out.sort(key=lambda j: (-j.score, j.left_table, j.right_table))
    return out


def default_selection(cands: list[JoinCandidate]) -> list[JoinCandidate]:
    """Auto-apply at most one candidate per table pair, never an N:N one."""
    seen: set[tuple[str, str]] = set()
    picked = []
    for c in cands:  # already best-first
        pair = (c.left_table, c.right_table)
        if pair in seen or c.cardinality == "N:N":
            continue
        seen.add(pair)
        picked.append(c)
    return picked


def confirm(c: JoinCandidate) -> ConfirmedJoin:
    return ConfirmedJoin(c.left_table, c.left_col, c.right_table, c.right_col, cast_to_text=c.type_mismatch,
                         left_unique=c.cardinality in ("1:1", "1:N"), right_unique=c.cardinality in ("1:1", "N:1"))


def manual_join(lt: TableInfo, lc: str, rt: TableInfo, rc: str) -> ConfirmedJoin:
    """A user-specified link; uniqueness is measured, not assumed."""
    l = next(c for c in lt.columns if c.name == lc)
    r = next(c for c in rt.columns if c.name == rc)
    return ConfirmedJoin(lt.name, lc, rt.name, rc, cast_to_text=l.dtype != r.dtype,
                         left_unique=_is_unique(l, lt.rows), right_unique=_is_unique(r, rt.rows))


def unrelated_pairs(tables: list[TableInfo], links: list) -> list[tuple[str, str]]:
    """Table pairs not connected by any path of links — the one case where the app asks
    instead of guessing. payroll↔attendance is *related* if both link to employees."""
    parent = {t.name: t.name for t in tables}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for l in links:
        parent[find(l.left_table)] = find(l.right_table)
    return [(a.name, b.name) for a, b in combinations(tables, 2) if find(a.name) != find(b.name)]
