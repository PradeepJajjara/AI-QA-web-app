"""Deterministic path: questions with a known shape become a Plan without any model.

Shapes (case-insensitive):
    total / sum of <col>                 [by <col>]
    average / avg / mean <col>           [by <col>]
    min / max / highest / lowest <col>   [by <col>]
    how many <table>                     [by <col>]
    number of distinct / unique <col>    [by <col>]

Column names match exactly (ignoring case, punctuation and a trailing plural 's'). Anything else —
a filter, a date, a column that exists in two files, a word we don't know — returns
None and the question goes to the model. A parser that guesses is worse than none.
"""

from __future__ import annotations

import re

from dataqa.plan import Aggregate, Catalog, GroupBy, Plan

_FUNCS = [
    (r"total|sum(?: of)?|overall", "sum"),
    (r"average|avg|mean", "avg"),
    (r"max(?:imum)?|highest|largest|biggest", "max"),
    (r"min(?:imum)?|lowest|smallest", "min"),
    (r"(?:(?:number|count) of|how many) (?:distinct|unique)|distinct|unique", "count_distinct"),
]
# A column name as a person types it: words, spaces, and the punctuation real headers carry
# ('Work-Life Balance Score', 'Training Duration(Days)', 'Salary (USD)').
_NAME = r"[\w \-()/]+?"
_FUNC_RE = "|".join(f"(?P<{name}>{pat})" for pat, name in _FUNCS)
# Leading filler a person types before the real question. A fixed list, not a wildcard, so
# "engineering total bonus" can never be read as "total bonus". Typos of the common ones included.
_FILLER = (r"(?:(?:what|whats|what's|waht|wht|wat|is|are|the|a|an|tell|me|please|pls|show|give|find|can|you|"
           r"could|would|i|want|to|know|of|our|my)\s+){0,6}")
_AGG_RE = re.compile(
    rf"^{_FILLER}(?:{_FUNC_RE})\s+(?:of\s+)?(?P<col>{_NAME})"
    rf"(?:\s+(?:by|per|for each)\s+(?P<by>{_NAME}))?\s*\??$",
    re.IGNORECASE,
)
_COUNT_RE = re.compile(
    rf"^{_FILLER}(?:how many|number of|count(?: of)?)\s+(?P<table>{_NAME})(?:\s+(?:are there|do we have|records|rows))?"
    rf"(?:\s+(?:by|per|for each)\s+(?P<by>{_NAME}))?\s*\??$",
    re.IGNORECASE,
)


def _norm(s: str) -> str:
    """Spaces, underscores and punctuation all fold to one separator, so 'work life balance score'
    and 'Work-Life Balance Score' compare equal, as do 'training duration days' and
    'Training Duration(Days)'."""
    return re.sub(r"[^a-z0-9]+", "_", s.strip().lower()).strip("_")


def find_column(cat: Catalog, name: str) -> tuple[str, str] | None:
    """(table, column) if exactly one loaded column matches; None if zero or ambiguous.
    An exact match wins; failing that, a plural of a column name ('locations' -> location)."""
    n = _norm(name)
    for target in (n, n[:-1] if n.endswith("s") else None):
        if target is None:
            continue
        hits = [(t.name, c.name) for t in cat.tables for c in t.columns if _norm(c.name) == target]
        if len(hits) == 1:
            return hits[0]
        if hits:  # the same name in two files is ambiguous; don't try the plural of it
            return None
    return None


def find_table(cat: Catalog, name: str) -> str | None:
    n = _norm(name).rstrip("s")
    for t in cat.tables:
        if _norm(t.name).rstrip("s") == n:
            return t.name
    return None


def parse(question: str, cat: Catalog) -> Plan | None:
    q = question.strip()

    if m := _AGG_RE.match(q):
        func = next(n for n in ("sum", "avg", "max", "min", "count_distinct") if m.group(n))
        hit = find_column(cat, m["col"])
        if not hit:
            return None
        table, col = hit
        plan = Plan(tables=[table], aggregates=[Aggregate(func=func, table=table, column=col)])
        return _with_group(plan, m["by"], cat)

    if m := _COUNT_RE.match(q):
        table = find_table(cat, m["table"])
        if not table:
            return None
        plan = Plan(tables=[table], aggregates=[Aggregate(func="count", table=table)])
        return _with_group(plan, m["by"], cat)

    return None


def _with_group(plan: Plan, by: str | None, cat: Catalog) -> Plan | None:
    if not by:
        return plan
    hit = find_column(cat, by)
    if not hit:
        return None
    table, col = hit
    if table not in plan.tables:
        if not _reachable(cat, plan.tables[0], table):
            return None  # the only column by that name is in an unrelated file: not a template shape
        plan.tables.append(table)
    plan.group_by.append(GroupBy(table=table, column=col))
    return plan


def _reachable(cat: Catalog, a: str, b: str) -> bool:
    """Is there a path of applied links from table a to table b?"""
    adj: dict[str, set[str]] = {}
    for l in cat.links:
        adj.setdefault(l.left_table, set()).add(l.right_table)
        adj.setdefault(l.right_table, set()).add(l.left_table)
    seen, todo = {a}, [a]
    while todo:
        cur = todo.pop()
        if cur == b:
            return True
        for nxt in adj.get(cur, ()):
            if nxt not in seen:
                seen.add(nxt); todo.append(nxt)
    return False
