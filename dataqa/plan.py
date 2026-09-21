"""The query plan: the only thing the model (or the template parser) is allowed to produce.

A plan is data, not code. It is validated against the real catalog — every table and
column it names must exist, every function must fit the column's type — before it is
compiled to SQL. Anything that can't be expressed as a plan is refused, not improvised.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from dataqa.joins import ConfirmedJoin
from dataqa.loader import ColumnInfo, TableInfo

FilterOp = Literal["=", "!=", ">", ">=", "<", "<=", "in", "not in", "contains", "between", "is null", "is not null"]
AggFunc = Literal["sum", "avg", "count", "count_distinct", "min", "max", "ratio"]
CondOp = Literal["", "=", "!=", ">", ">=", "<", "<=", "in", "not in", "contains", "is null", "is not null"]
HavingOp = Literal["", ">", ">=", "<", "<=", "=", "!="]
Bucket = Literal["none", "day", "week", "month", "quarter", "year"]

MAX_LIMIT = 1000
DEFAULT_ROW_LIMIT = 100


class ColumnRef(BaseModel):
    table: str
    column: str


class Filter(BaseModel):
    table: str
    column: str
    op: FilterOp
    value: str = Field(default="", description="For scalar ops. Dates may be partial: 2025, 2025-03, 2025-Q1. Empty for in/between/null ops.")
    values: list[str] = Field(default_factory=list, description="For in / not in (any number) and between (exactly two). Empty otherwise.")


class GroupBy(BaseModel):
    table: str
    column: str
    bucket: Bucket = Field(default="none", description="For date columns: day/week/month/quarter/year. 'none' otherwise.")


class Aggregate(BaseModel):
    """One measure. A condition (where_*) makes it conditional: count where status = Present.
    func 'ratio' = rows matching the condition as a percentage of all rows in the group
    (or, with a column, sum of that column where the condition holds ÷ sum of the column)."""

    func: AggFunc
    table: str = Field(default="", description="Empty with empty column for count(*).")
    column: str = Field(default="", description="Empty for count(*) or a row-share ratio.")
    alias: str = Field(default="", description="Name for the result column; empty for the default.")
    where_column: str = Field(default="", description="Condition column (required for ratio; optional otherwise).")
    where_op: CondOp = Field(default="", description="Condition operator; empty for none.")
    where_value: str = Field(default="", description="Condition value; dates may be partial.")
    where_values: list[str] = Field(default_factory=list, description="For in / not in.")

    @property
    def conditional(self) -> bool:
        return bool(self.where_column and self.where_op)


class Having(BaseModel):
    """Keep only groups where a measure passes a threshold: pct_present > 75."""

    by: str = Field(default="", description="An aggregate alias. Empty for no threshold.")
    op: HavingOp = ""
    value: str = ""


class Sort(BaseModel):
    by: str = Field(default="", description="An aggregate alias or a group-by column name. Empty for default order.")
    desc: bool = Field(default=False, description="true = highest first. Only meaningful with a non-empty 'by'.")


class Plan(BaseModel):
    """What to compute. Joins are NOT specified here — the compiler derives them from the
    links applied at upload time. Listing more than one table implies a join."""

    tables: list[str] = Field(min_length=1)
    filters: list[Filter] = Field(default_factory=list)
    group_by: list[GroupBy] = Field(default_factory=list)
    aggregates: list[Aggregate] = Field(default_factory=list)
    select: list[ColumnRef] = Field(default_factory=list, description="Row listing only (no aggregates).")
    sort: Sort = Field(default_factory=Sort)
    having: Having = Field(default_factory=Having)
    limit: int = Field(default=0, ge=0, le=MAX_LIMIT, description="0 = no limit.")

    @property
    def is_listing(self) -> bool:
        return not self.aggregates and not self.group_by


class PlanError(Exception):
    """The plan can't be run as written. The message is written for the user, who asked a
    question and never saw a plan.

    kind:
      "fixable"     the model got the shape right and a detail wrong (unknown column, missing
                    value, bad sort key). Worth feeding back to the model once.
      "capability"  the plan can't express it (arithmetic, ratios, fan-out, no link). Retrying
                    won't help; refuse and say what can be done instead.
    """

    def __init__(self, message: str, kind: str = "fixable"):
        super().__init__(message)
        self.kind = kind


class Ambiguous(PlanError):
    """One detail has a few plausible readings. Ask — once — instead of guessing or refusing.
    `location` is (section, index, attr) in the plan where the chosen option goes.

    Two very different situations share this UI, and the wording must keep them apart:
      substitute=False  the question was unclear ("dep" -> department or grade?) and the
                        options are readings of what the user meant.
      substitute=True   the question was clear and the model read it right, but the plan can't
                        express it ("total pay" = base + bonus - deductions). The options are
                        NOT what the user meant — they're the nearest thing on offer. The result
                        must say so, or a correct refusal becomes a plausible wrong answer."""

    def __init__(self, question: str, options: list[str], location: tuple[str, int, str], plan: "Plan",
                 substitute: bool = False, asked_for: str = "", option_tables: dict[str, str] | None = None):
        super().__init__(question, kind="ambiguous")
        self.question, self.options, self.location, self.plan = question, options, location, plan
        self.substitute, self.asked_for = substitute, asked_for
        self.option_tables = option_tables or {}  # option -> table, when the candidates span files

    def table_for(self, choice: str) -> str:
        return self.option_tables.get(choice, "")

    def note_for(self, choice: str) -> str:
        """The provenance line the answer must carry after a choice."""
        if not self.substitute:
            return f"Read it as '{choice}'."
        verb = "Ranked by" if self.plan.sort.by or self.plan.limit else "Used"
        return f"{verb} {choice} only — not {self.asked_for}. That's a substitute, not what you asked."


def apply_choice(plan: "Plan", location: tuple[str, int, str], choice: str, table: str = "") -> "Plan":
    """`table` is set when the chosen column lives in a file the plan didn't use yet (Ambiguous.table_for)."""
    section, idx, attr = location
    plan = plan.model_copy(deep=True)
    if section == "tables":  # a file name: rename it everywhere the plan used it
        old = plan.tables[idx]
        plan.tables = list(dict.fromkeys(choice if t == old else t for t in plan.tables))
        for part in (plan.filters, plan.group_by, plan.aggregates, plan.select):
            for obj in part:
                if obj.table == old:
                    obj.table = choice
        return plan
    obj = getattr(plan, section)[idx]
    setattr(obj, attr, choice)
    if attr == "column" and table and table != obj.table:
        obj.table = table
        if table not in plan.tables:
            plan.tables.append(table)  # validate() joins it through the applied links
    if section == "aggregates" and attr == "column":
        obj.alias = ""  # the model's alias named its own pick ('avg_engagement'); the default follows the column
    return plan


_ARITH_RE = re.compile(r"\s[+\-*/]\s|[+*/]")  # 'a + b', 'a - b', 'a*b'; a bare hyphen is a name, not a minus


def looks_like_arithmetic(column: str) -> bool:
    return bool(_ARITH_RE.search(column))


def _similar(a: str, b: str) -> float:
    """Column-name similarity: 'salary' vs 'base_salary' scores high (substring), 'bonus' low."""
    from difflib import SequenceMatcher
    na, nb = re.sub(r"[^a-z0-9]", "", a.lower()), re.sub(r"[^a-z0-9]", "", b.lower())
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.85 + 0.15 * min(len(na), len(nb)) / max(len(na), len(nb))
    return SequenceMatcher(None, na, nb).ratio()


class MissingTable(PlanError):
    """The plan names a table no loaded file has. Fixable (the model may pick the right file on a
    retry); when it stands as the refusal, the router rewrites it from the real schema with
    explain_missing(), leading with what the user asked for rather than the name the model made up."""

    def __init__(self, name: str, loaded: str):
        super().__init__(f"'{name}' isn't one of the loaded files ({loaded}).")
        self.name = name


class _TableAmbiguous(PlanError):
    """Raised inside Catalog.resolve_table; validate() turns it into an Ambiguous with a location."""

    def __init__(self, name: str, options: list[str]):
        super().__init__(f"'{name}' isn't one of the loaded files. Which did you mean?")
        self.name, self.options = name, options


@dataclass
class Catalog:
    """Everything a plan is validated against: loaded tables and applied links."""

    tables: list[TableInfo]
    links: list[ConfirmedJoin]
    notes: list[str] = field(default_factory=list)  # rewrites made during validation, shown to the user

    def table(self, name: str) -> TableInfo:
        for t in self.tables:
            if t.name == name:
                return t
        raise MissingTable(name, ", ".join(t.name for t in self.tables))

    def resolve_table(self, name: str) -> str:
        """The loaded table a model meant by `name`: exact, then case/punctuation-insensitive,
        then one strong near-miss ('employees' for employee_data) — noted. Else PlanError."""
        for t in self.tables:
            if t.name == name:
                return t.name
        norm = lambda x: re.sub(r"[^a-z0-9]", "", x.lower())  # noqa: E731
        hits = [t.name for t in self.tables if norm(t.name) == norm(name)]
        if len(hits) == 1:
            return hits[0]
        scored = sorted(((max(_similar(name, t.name), _similar(name.rstrip("s"), t.name)), t.name) for t in self.tables), reverse=True)
        strong = [n for sc, n in scored if sc >= 0.85]
        if len(strong) == 1:
            self.notes.append(f"Read '{name}' as the file {strong[0]}.")
            return strong[0]
        if 2 <= len(strong) <= 3:
            raise _TableAmbiguous(name, strong)
        return self.table(name).name  # raises with the list of loaded files

    def find_column(self, column: str, tables: list[str] | None = None) -> list[tuple[str, ColumnInfo]]:
        """Exact (case-insensitive) matches of a column name across tables."""
        names = tables or [t.name for t in self.tables]
        return [(t.name, c) for t in self.tables if t.name in names
                for c in t.columns if c.name.lower() == column.lower()]

    def suggest(self, column: str, table: str, floor: float = 0.5) -> list[tuple[float, str]]:
        """Closest column names in `table`, best first."""
        t = self.table(table)
        scored = sorted(((_similar(column, c.name), c.name) for c in t.columns), reverse=True)
        return [(sc, n) for sc, n in scored if sc >= floor][:3]

    def column(self, table: str, column: str) -> ColumnInfo:
        t = self.table(table)
        # A real column name wins outright — 'Work-Life Balance Score' and 'Salary (USD)' are
        # names, not arithmetic. Only an unknown string with operators is treated as arithmetic.
        for c in t.columns:
            if c.name == column:
                return c
        for c in t.columns:  # the model often lowercases
            if c.name.lower() == column.lower():
                return c
        norm = lambda x: re.sub(r"[^a-z0-9]", "", x.lower())  # noqa: E731
        same = [c for c in t.columns if norm(c.name) == norm(column)]  # 'application_date' = 'Application Date'
        if len(same) == 1:
            return same[0]
        if looks_like_arithmetic(column):
            raise PlanError(f"I can't do arithmetic between columns ('{column}') yet — ask for each column on its own, "
                            f"e.g. 'total base_salary by department' and 'total bonus by department'.", kind="capability")
        hints = self.suggest(column, table)
        if hints:
            raise PlanError(f"'{table}' has no column '{column}'. Did you mean {' or '.join(n for _, n in hints)}?")
        raise PlanError(f"'{table}' has no column '{column}'. Its columns are: {', '.join(t.column_names)}.")


def agg_alias(a: Aggregate) -> str:
    if a.alias:
        return a.alias
    if a.func == "ratio":
        tag = re.sub(r"[^A-Za-z0-9]+", "_", a.where_value or a.where_column).strip("_").lower() or "match"
        return f"pct_{tag}"
    base = "count" if not a.column else f"{a.func}_{a.column}"
    if a.conditional and a.where_value:
        base += "_" + re.sub(r"[^A-Za-z0-9]+", "_", a.where_value).strip("_").lower()
    return base


def group_alias(g: GroupBy) -> str:
    return f"{g.column}_{g.bucket}" if g.bucket != "none" else g.column


def strict_schema(model) -> dict:
    """JSON schema with every property required and every object closed. Constrained decoding
    lets a model skip optional keys, and a small model skips exactly the ones that matter
    (bucket, values). Groq's json_schema mode refuses any object without
    additionalProperties: false — without it every call paid a 400 and downgraded to
    json_object (schema in the prompt, not enforced)."""
    schema = model.model_json_schema()

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["required"] = list(node["properties"])
                node["additionalProperties"] = False
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(schema)
    return schema


_VALUE_SPLIT = re.compile(r",| and | or ")
_NULL_WORDS = {"none", "null", "nan", "n/a", "na", "empty", "missing", "blank"}


def _check_value_type(c: ColumnInfo, op: str, value: str, values: list[str]) -> None:
    """Numbers must parse as numbers and dates as dates. The compiler checks this too, but a
    PlanError raised here is a fixable detail the model gets one retry on; from the compiler it
    is a refusal."""
    from dataqa.engine import coerce, date_range  # local import: engine imports this module
    if op in ("is null", "is not null", "contains"):
        return
    for v in (values if op in ("in", "not in", "between") else [value]):
        if not v:
            continue
        if c.is_temporal:
            date_range(v)
        elif c.is_numeric:
            coerce(v, c)


def _all_known(c: ColumnInfo) -> bool:
    """Every value of the column is in the catalog (the same rule schema_text uses to print them)."""
    return not c.is_numeric and not c.is_temporal and bool(c.distinct) and c.distinct <= len(c.samples)


def _check_known_values(table: str, c: ColumnInfo, op: str, value: str, values: list[str],
                        cat: Catalog, tables: list[str]) -> tuple[str, str, str, str, list[str]]:
    """For a text column whose every value the catalog knows (low cardinality), a filter value
    that matches none of them is a mistake, not an empty result. Repaired shapes:
      - '=' with an 'A or B' value where A and B are both real values -> 'in' (a note says so)
      - '= none/null/…' on a column with empties -> 'is null'
      - a value that belongs to exactly one OTHER column of the question's tables
        ('BusinessUnit = Sales' when Sales is a DepartmentType) -> the filter moves there
      - anything else unknown -> fixable PlanError naming the real values, so the model gets one
        more go and the user never sees a silent 0.
    Returns (table, column, op, value, values), possibly rewritten."""
    if op in ("contains", "between", "is null", "is not null") or not _all_known(c):
        return table, c.name, op, value, values
    known = {str(s).strip().lower(): str(s).strip() for s in c.samples}
    key = lambda v: str(v).strip().lower()  # noqa: E731
    if op in ("=", "!="):
        if key(value) in known:
            return table, c.name, op, value, values
        if key(value) in _NULL_WORDS and c.null_frac > 0:
            # 'manager_id = none': 'none' isn't a value of the column, and the column has empties.
            new_op = "is null" if op == "=" else "is not null"
            cat.notes.append(f"Read {c.name} {'=' if op == '=' else '≠'} '{value}' as '{c.name} {_OP_WORD[new_op]}'.")
            return table, c.name, new_op, "", []
        parts = [p.strip() for p in _VALUE_SPLIT.split(value) if p.strip()]
        if len(parts) >= 2 and all(key(p) in known for p in parts):
            cat.notes.append(f"Read {c.name} = '{value}' as any of {', '.join(known[key(p)] for p in parts)}.")
            return table, c.name, ("in" if op == "=" else "not in"), "", parts
        elsewhere = [(t, oc) for t in tables for oc in cat.table(t).columns
                     if oc is not c and _all_known(oc) and key(value) in {key(s) for s in oc.samples}]
        if len(elsewhere) == 1:
            t, oc = elsewhere[0]
            cat.notes.append(f"'{value}' is a {oc.name}, not a {c.name} — filtered on {oc.name}.")
            return t, oc.name, op, value, values
        hint = f" ('{value}' is a value of {' and '.join(oc.name for _, oc in elsewhere)}.)" if elsewhere else ""
        raise PlanError(f"No {c.name} is '{value}'. The values are: {', '.join(known.values())}.{hint}")
    if op in ("in", "not in"):
        unknown = [v for v in values if key(v) not in known]
        if unknown:
            raise PlanError(f"No {c.name} is {' or '.join(repr(u) for u in unknown)}. The values are: {', '.join(known.values())}.")
    return table, c.name, op, value, values


def validate(plan: Plan, cat: Catalog) -> Plan:
    """Referential + type checks. Returns the plan with column names canonicalised.
    Raises PlanError with a message the user can act on."""
    for i, name in enumerate(list(plan.tables)):
        try:
            resolved = cat.resolve_table(name)
        except _TableAmbiguous as e:
            raise Ambiguous(str(e), e.options, ("tables", i, ""), plan) from None
        if resolved != name:
            for section in (plan.filters, plan.group_by, plan.aggregates, plan.select):
                for obj in section:
                    if obj.table == name:
                        obj.table = resolved
            plan.tables[i] = resolved
    plan.tables = list(dict.fromkeys(plan.tables))
    for section in (plan.filters, plan.group_by, plan.aggregates, plan.select):
        for obj in section:
            if obj.table and not any(t.name == obj.table for t in cat.tables):
                try:
                    obj.table = cat.resolve_table(obj.table)
                except _TableAmbiguous as e:
                    raise Ambiguous(str(e), e.options, ("tables", len(plan.tables), ""), plan.model_copy(
                        update={"tables": plan.tables + [obj.table]})) from None
                except PlanError:
                    pass  # left for resolve() below, which can retarget a column to the right table
    used = set(plan.tables)

    def resolve(section: str, idx: int, obj) -> ColumnInfo:
        """Resolve obj.table/obj.column, repairing what code can repair:
          - column on the wrong table but unique among loaded tables -> retarget (note)
          - misspelt column with one strong match -> correct (note)
          - misspelt column with 2-3 plausible matches -> Ambiguous (ask)
        """
        # 'employees.employee_id' written into the column field: split on a known table prefix
        if "." in obj.column:
            prefix, _, rest = obj.column.partition(".")
            if any(t.name.lower() == prefix.lower() for t in cat.tables):
                obj.table = next(t.name for t in cat.tables if t.name.lower() == prefix.lower())
                obj.column = rest
                if obj.table not in used:
                    plan.tables.append(obj.table); used.add(obj.table)
        table, column = obj.table, obj.column
        if table not in used and column:
            cat.table(table)  # unknown file -> fixable
            hits = cat.find_column(column)
            if len(hits) == 1:
                obj.table = hits[0][0]
                if obj.table not in used:
                    plan.tables.append(obj.table); used.add(obj.table)
                cat.notes.append(f"Took '{column}' from {obj.table}.")
                return hits[0][1]
            raise PlanError(f"'{column}' is in {table}, which this question doesn't otherwise use — "
                            f"say which file you mean, e.g. 'from {table}'.")
        try:
            return cat.column(table, column)
        except PlanError as e:
            if e.kind != "fixable":
                # Arithmetic over real columns ('base_salary + bonus - deductions'). The model read
                # the question RIGHT; the plan just can't express it. Offer the parts as a
                # substitute — never as "what you meant" — and label the result accordingly.
                parts = [x.strip() for x in re.split(r"\s[+\-*/]\s|[+*/]", column) if x.strip()]
                real = [c.name for c in cat.table(table).columns if c.name.lower() in {x.lower() for x in parts}]
                if 2 <= len(real) <= 3 and len(real) == len(parts):
                    verb = "rank by" if plan.sort.by or plan.limit else "compute"
                    raise Ambiguous(
                        f"I can't combine columns arithmetically yet, so I can't {verb} {column}. "
                        f"I can {verb} {', '.join(real[:-1])} or {real[-1]} individually — that's not the same thing.",
                        real, (section, idx, "column"), plan, substitute=True, asked_for=column) from None
                raise
            hints = cat.suggest(column, table)
            if len(hints) == 1 and hints[0][0] >= 0.85:
                obj.column = hints[0][1]
                cat.notes.append(f"Read '{column}' as '{obj.column}'.")
                return cat.column(table, obj.column)
            if 2 <= len(hints) <= 3 and hints[0][0] >= 0.5:
                raise Ambiguous(f"'{column}' isn't a column in {table}. Which did you mean?",
                                [n for _, n in hints], (section, idx, "column"), plan) from None
            # elsewhere among the loaded files?
            hits = cat.find_column(column)
            if len(hits) == 1:
                obj.table = hits[0][0]
                if obj.table not in used:
                    plan.tables.append(obj.table); used.add(obj.table)
                cat.notes.append(f"Took '{column}' from {obj.table}.")
                return hits[0][1]
            raise

    def ref(table: str, column: str) -> ColumnInfo:  # for places already resolved
        return cat.column(table, column)

    # Several '=' filters on one column can only be an OR the model wrote as AND ("A vs B").
    # AND is provably empty, so merge into 'in' and say so.
    by_col: dict[tuple[str, str], list[Filter]] = {}
    for f in plan.filters:
        if f.op == "=" and f.value:
            by_col.setdefault((f.table, f.column.lower()), []).append(f)
    for (table, col), fs in by_col.items():
        if len(fs) > 1:
            merged = Filter(table=table, column=fs[0].column, op="in", values=[f.value for f in fs])
            plan.filters = [f for f in plan.filters if f not in fs] + [merged]
            cat.notes.append(f"Read '{fs[0].column}' = {' / '.join(f.value for f in fs)} as any of those.")

    for i, f in enumerate(plan.filters):
        c = resolve("filters", i, f)
        f.column = c.name
        if f.op in ("is null", "is not null"):
            continue
        if f.op in ("in", "not in"):
            if not f.values:
                if f.value:  # model put the list in the wrong slot
                    f.values = [v.strip() for v in re.split(r",| and | or ", f.value) if v.strip()]
                    f.value = ""
                else:
                    raise PlanError(f"I couldn't tell which {f.column} values to include. Name them — "
                                    f"e.g. 'for Sales and Engineering'.")
        elif f.op == "between":
            if not f.values or len(f.values) != 2:
                raise PlanError(f"A range on {f.column} needs a start and an end — e.g. 'between 2025-01 and 2025-03'.")
        elif not f.value:
            raise PlanError(f"I couldn't tell what to compare {f.column} against. Give a value — e.g. '{f.column} = Sales'.")
        if f.op == "contains" and (c.is_numeric or c.is_temporal):
            raise PlanError(f"'contains' only works on text, and {f.column} holds {'numbers' if c.is_numeric else 'dates'}. "
                            f"Try an exact value or a range instead.", kind="capability")
        _check_value_type(c, f.op, f.value, f.values)  # a bad number/date is fixable: catch it here, inside the retry window
        f.table, f.column, f.op, f.value, f.values = _check_known_values(f.table, c, f.op, f.value, f.values, cat, plan.tables)

    for i, g in enumerate(plan.group_by):
        c = resolve("group_by", i, g)
        g.column = c.name
        if g.bucket != "none" and not c.is_temporal:
            raise PlanError(f"{g.column} isn't a date, so it can't be grouped by {g.bucket}. "
                            f"Group by {g.column} as it is, or pick a date column.")

    aliases = set()
    for i, a in enumerate(plan.aggregates):
        if a.func == "ratio" and not a.conditional:
            raise PlanError("A percentage needs a condition — e.g. 'percentage of trainings where Outcome is Completed'.")
        if a.conditional:
            # the condition column must exist in a plan table; default the table from it
            cond_hits = cat.find_column(a.where_column, plan.tables)
            if len(cond_hits) != 1:
                raise PlanError(f"'{a.where_column}' (the condition column) isn't a column in {', '.join(plan.tables)}.")
            a.where_column = cond_hits[0][1].name
            if not a.table:
                a.table = cond_hits[0][0]
            if a.where_op in ("in", "not in") and not a.where_values:
                if a.where_value:
                    a.where_values = [v.strip() for v in re.split(r",| and | or ", a.where_value) if v.strip()]
                else:
                    raise PlanError(f"I couldn't tell which {a.where_column} values count. Name them — e.g. 'Completed or Passed'.")
            elif a.where_op not in ("in", "not in", "is null", "is not null") and not a.where_value:
                raise PlanError(f"I couldn't tell what {a.where_column} should equal for the percentage. Give a value — e.g. 'where status is Present'.")
            _check_value_type(cond_hits[0][1], a.where_op, a.where_value, a.where_values)
            # (a condition is compiled against the measure's table, so a retarget stays in that table)
            _, a.where_column, a.where_op, a.where_value, a.where_values = _check_known_values(
                cond_hits[0][0], cond_hits[0][1], a.where_op, a.where_value, a.where_values, cat, [cond_hits[0][0]])
        if not a.column:
            if a.func not in ("count", "ratio"):
                what = {"sum": "add up", "avg": "average", "min": "take the minimum of", "max": "take the maximum of",
                        "count_distinct": "count distinct values of"}[a.func]
                raise PlanError(f"I couldn't tell which column to {what}. Name it — e.g. 'how many distinct departments'.")
            if a.table and a.table not in used:
                raise PlanError(f"The count refers to {a.table}, which this question doesn't otherwise use.")
        else:
            if not a.table:
                if len(plan.tables) == 1:
                    a.table = plan.tables[0]
                else:
                    hits = cat.find_column(a.column, plan.tables)
                    if len(hits) != 1:
                        raise PlanError(f"'{a.column}' could come from more than one file — say which, e.g. 'from payroll'.")
                    a.table = hits[0][0]
            c = resolve("aggregates", i, a)
            a.column = c.name
            if a.func in ("sum", "avg", "ratio") and not c.is_numeric:
                verb = "add it up" if a.func == "sum" else "average it"
                raise PlanError(f"{a.column} holds {'dates' if c.is_temporal else 'text'}, so I can't {verb}. "
                                f"Try counting it, or pick a numeric column.", kind="capability")
        al = agg_alias(a)
        if al in aliases:
            a.alias = f"{al}_{i}"
            al = a.alias
        aliases.add(al)

    for i, sel in enumerate(plan.select):
        c = resolve("select", i, sel)
        sel.column = c.name
    if plan.select and not plan.is_listing:
        plan.select = []  # summaries don't take a column list; drop it rather than refuse

    if not plan.sort.by and plan.sort.desc:
        # "sort descending" with nothing to sort by is an incomplete plan, not a valid one.
        # With exactly one measure the intent is unambiguous: rank by it, and say so.
        if len(plan.aggregates) == 1 and (plan.group_by or plan.is_listing):
            plan.sort.by = agg_alias(plan.aggregates[0])
            cat.notes.append(f"Ranked by {plan.sort.by}, highest first.")
        else:
            raise PlanError("I couldn't tell what to rank by. Say it — e.g. 'highest by base_salary'.")
    if plan.having.by:
        if not plan.having.op or not plan.having.value:
            raise PlanError("A threshold needs an operator and a number — e.g. 'more than 75%'.")
        if plan.having.by not in aliases:
            hit = [x for x in aliases if re.sub(r"[^a-z0-9]", "", x.lower()) == re.sub(r"[^a-z0-9]", "", plan.having.by.lower())]
            if not hit and len(aliases) == 1:
                hit = list(aliases)
            if not hit:
                raise PlanError(f"The threshold refers to '{plan.having.by}', which isn't one of the measures ({', '.join(sorted(aliases))}).")
            plan.having.by = hit[0]
        try:
            float(plan.having.value.rstrip("%"))
        except ValueError:
            raise PlanError(f"'{plan.having.value}' isn't a number for the threshold on {plan.having.by}.")
        plan.having.value = plan.having.value.rstrip("%").strip()
        if not plan.group_by:
            raise PlanError("A threshold like 'more than 75%' needs something to compare per group — e.g. 'per employee'.")
        # The model often applies the threshold twice: as a row filter on the measured column AND
        # as the threshold on the aggregate. The row filter silently biases the aggregate (an
        # "average above 150k" computed only from rows above 150k). Drop it and say so.
        target = next((a for a in plan.aggregates if agg_alias(a) == plan.having.by), None)
        if target is not None and target.column:
            dup = [f for f in plan.filters
                   if f.column.lower() == target.column.lower() and f.op in (">", ">=", "<", "<=", "=")
                   and f.value.rstrip("%").strip() == plan.having.value]
            if dup:
                plan.filters = [f for f in plan.filters if f not in dup]
                word = _FUNC_WORD.get(target.func, target.func)
                cat.notes.append(f"Applied the {plan.having.value} threshold to the {word} of {target.column} per group — "
                                 f"not to each row first, which would have skewed the {word}.")

    if plan.sort.by:
        for g in plan.group_by:
            if g.bucket != "none" and plan.sort.by == g.column:
                plan.sort.by = group_alias(g)  # the bucketed expression is what's in the result
        sortable = aliases | {group_alias(g) for g in plan.group_by} | {g.column for g in plan.group_by if g.bucket == "none"}
        if plan.is_listing:
            sortable |= {c.name for t in plan.tables for c in cat.table(t).columns}
        if plan.sort.by not in sortable:
            # near-miss rescue: 'average_base_salary' -> 'avg_base_salary'; 'total' -> the only aggregate
            norm = lambda x: re.sub(r"[^a-z0-9]", "", x.lower().replace("average", "avg").replace("total", "sum"))  # noqa: E731
            hit = [x for x in sortable if norm(x) == norm(plan.sort.by)]
            if not hit and len(aliases) == 1 and not plan.is_listing:
                hit = list(aliases)
            if not hit:
                raise PlanError(f"I can't order the result by '{plan.sort.by}' — it isn't one of the result columns "
                                f"({', '.join(sorted(sortable))}).")
            cat.notes.append(f"Sorted by '{hit[0]}' (asked for '{plan.sort.by}').")
            plan.sort.by = hit[0]

    if len(plan.tables) > 1 and not cat.links:
        raise PlanError("This question needs data from more than one file, but the files aren't linked. "
                        "Set which columns link them under the answer.", kind="capability")
    return plan


# --- intent checks: does the plan reflect what was asked? ---------------------------
# validate() checks structural validity. A plan can pass every structural check and still
# answer a different question — "5 highest paid" with no sort and no limit returns all 120
# rows, ordered arbitrarily. These checks catch the intent-level mismatches we know about.

_TOPN_WORDS = r"top|highest|lowest|largest|smallest|biggest|best|worst|most|least|first|bottom"
_TOPN_RE = re.compile(rf"\b(?:{_TOPN_WORDS})\b", re.IGNORECASE)
_N_RE = re.compile(rf"\b(?:top|first|bottom)\s+(\d+)\b|\b(\d+)\s+(?:{_TOPN_WORDS})\b", re.IGNORECASE)
_LOW_RE = re.compile(r"\b(?:lowest|smallest|least|bottom|worst)\b", re.IGNORECASE)


_TREND_RE = re.compile(r"\b(monthly|weekly|daily|yearly|annually|quarterly|per (?:month|week|day|year|quarter)|"
                       r"by (?:month|week|day|year|quarter)|each (?:month|week|day|year|quarter)|over time|trend)\b", re.IGNORECASE)
_PERIOD_WORD = {"month": "month", "week": "week", "day": "day", "year": "year", "quarter": "quarter",
                "monthly": "month", "weekly": "week", "daily": "day", "yearly": "year", "annually": "year", "quarterly": "quarter"}
_MENTIONS_DATE = re.compile(r"\b(20\d{2}|19\d{2})\b|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b|\bq[1-4]\b|"
                            r"\b(today|yesterday|last|this|next|since|until|before|after|between)\b", re.IGNORECASE)


# 'by department', 'per employee', 'each location', 'across teams' — a breakdown the question names.
# The period words are excluded: 'by month' is the trend itself, not a breakdown.
_BREAKDOWN_RE = re.compile(r"\b(?:by|per|each|for each|across)\s+(?!(?:month|week|day|year|quarter)s?\b)\w", re.IGNORECASE)


def _bucket_for(question: str) -> str:
    m = _TREND_RE.search(question)
    if not m:
        return "none"
    words = re.findall(r"[a-z]+", m.group(0).lower())
    for w in words:
        if w in _PERIOD_WORD:
            return _PERIOD_WORD[w]
    return "month"  # "over time" / "trend" with no period named


def _check_trend(question: str, plan: Plan, cat: Catalog) -> None:
    """'Monthly total X' must produce a time series. A small model sometimes reads 'monthly'
    as 'filter to one month' and invents 2025-01 — a single number, silently wrong."""
    if not _TREND_RE.search(question) or plan.is_listing:
        return
    # 1. a date filter the question never asked for -> fixable (the model invented it)
    if not _MENTIONS_DATE.search(question):
        for f in plan.filters:
            c = cat.column(f.table, f.column)
            if c.is_temporal:
                raise PlanError(f"The question doesn't mention a date, but the proposal filters {f.column} to "
                                f"'{f.value or ', '.join(f.values)}'. Group by {f.column} per "
                                f"{_bucket_for(question)} instead of filtering it.")
    # 2. a bucket that isn't the period the question names ('weekly' answered by month) -> fix it
    bucket = _bucket_for(question)
    named = any(w in _PERIOD_WORD for w in re.findall(r"[a-z]+", (_TREND_RE.search(question) or re.match("", "")).group(0).lower()))
    for g in plan.group_by:
        if g.bucket != "none" and named and g.bucket != bucket:
            cat.notes.append(f"Grouped {g.column} per {bucket}, not per {g.bucket} — the question asked for a per-{bucket} view.")
            g.bucket = bucket
    # 3. no time bucket -> add one if exactly one date column is in play, else fixable
    if any(g.bucket != "none" for g in plan.group_by):
        return
    temporal = [(t, c) for t in plan.tables for c in cat.table(t).columns if c.is_temporal]
    if len(temporal) > 1:  # the column the model filtered by date is the one it meant
        filtered = [(t, c) for t, c in temporal if any(f.table == t and f.column == c.name for f in plan.filters)]
        if len(filtered) == 1:
            temporal = filtered
    if len(temporal) == 1:
        t, c = temporal[0]
        # The model produced no time bucket at all, so it misread the question's shape. Groups
        # it added that the question never names ('weekly WFH count trend' grouped by employee)
        # are part of that misreading: 367 rows for a question that wants one per week.
        extra = [g for g in plan.group_by if not _BREAKDOWN_RE.search(question)]
        if extra:
            plan.group_by = [g for g in plan.group_by if g not in extra]
            cat.notes.append(f"Dropped the breakdown by {', '.join(g.column for g in extra)} — the question asked for a "
                             f"per-{bucket} trend, not one per {' and '.join(g.column for g in extra)}.")
        plan.group_by.insert(0, GroupBy(table=t, column=c.name, bucket=bucket))
        cat.notes.append(f"Grouped by {c.name} per {bucket} — the question asked for a per-{bucket} view.")
        return
    raise PlanError(f"The question asks for a per-{bucket} view, but the proposal doesn't group by a date column. "
                    f"Group by one of: {', '.join(c.name for _, c in temporal) or 'a date column'}.")


_COMPARE_RE = re.compile(r"\b(vs\.?|versus|compare|compared|between)\b", re.IGNORECASE)
_HOW_MANY_RE = re.compile(r"\b(how many|number of|count of)\s+([a-z_]+)", re.IGNORECASE)


def _check_compare(question: str, plan: Plan, cat: Catalog) -> None:
    """'A vs B' must come back as one row per side. A model sometimes filters to both and
    returns a single combined total — a normal-looking number for a different question."""
    if not _COMPARE_RE.search(question) or plan.group_by or plan.is_listing:
        return
    for f in plan.filters:
        if f.op == "in" and len(f.values) >= 2:
            c = cat.column(f.table, f.column)
            plan.group_by.append(GroupBy(table=f.table, column=f.column, bucket="month" if c.is_temporal else "none"))
            cat.notes.append(f"Compared side by side: one row per {f.column}, not one combined total.")
            return


def _check_degenerate_group(plan: Plan, cat: Catalog) -> None:
    """count_distinct(X) grouped by X is one row of '1' per value — the model meant the total.
    A ratio 'where X = v' grouped by X is 100% for v and 0% for every other value — the model
    meant the share of all rows."""
    for a in plan.aggregates:
        if a.func == "count_distinct" and any(g.column == a.column and g.bucket == "none" for g in plan.group_by):
            plan.group_by = [g for g in plan.group_by if g.column != a.column]
            cat.notes.append(f"Counted distinct {a.column} overall — grouping by {a.column} would give 1 for every value.")
        if a.func == "ratio" and a.where_column and any(g.column == a.where_column and g.bucket == "none" for g in plan.group_by):
            plan.group_by = [g for g in plan.group_by if g.column != a.where_column]
            if plan.sort.by == a.where_column:
                plan.sort = Sort()
            cat.notes.append(f"Computed the percentage over all rows — grouping by {a.where_column} would give 100% for "
                             f"{a.where_value or ', '.join(a.where_values)} and 0% for everything else.")
    # A group pinned to one value by an '=' filter ('… in Sales' grouped by department) can only
    # ever hold one row; it isn't a breakdown, and it turns a single number into a 1×2 table.
    pinned = {(f.table, f.column.lower()): f.value for f in plan.filters if f.op == "=" and f.value}
    for g in list(plan.group_by):
        if g.bucket == "none" and (g.table, g.column.lower()) in pinned and plan.aggregates:
            plan.group_by.remove(g)
            if plan.sort.by == g.column:
                plan.sort = Sort()
            cat.notes.append(f"Dropped the grouping by {g.column} — the question pins it to {pinned[(g.table, g.column.lower())]}.")


def check_entity_count(question: str, plan: Plan, cat: Catalog) -> None:
    """'How many employees …' over a table with several rows per employee (payroll: 12 a year)
    must count distinct employees, not rows. Uses the applied links: if the question names a
    table that is the one-side of a link into the plan, count its key on the many-side."""
    m = _HOW_MANY_RE.search(question)
    if not m or not plan.aggregates:
        return
    word = m.group(2).lower().rstrip("s")
    entity = next((t for t in plan.tables if t.lower().rstrip("s") == word), None)
    if entity is None:
        return
    for a in plan.aggregates:
        if a.func != "count" or a.column:
            continue
        for link in cat.links:
            if link.left_table == entity and link.right_table in plan.tables and link.left_unique and not link.right_unique:
                a.func, a.table, a.column = "count_distinct", link.right_table, link.right_col
            elif link.right_table == entity and link.left_table in plan.tables and link.right_unique and not link.left_unique:
                a.func, a.table, a.column = "count_distinct", link.left_table, link.left_col
            else:
                continue
            if not a.alias:
                a.alias = f"{entity}_count"
            cat.notes.append(f"Counted distinct {entity} — {a.table} has several rows per {entity.rstrip('s')}, "
                             f"so counting rows would overstate it.")
            return


_LISTING_ASK = re.compile(r"^\s*(?:show(?: me)?|list|display|give me|which|who|what are)\b", re.IGNORECASE)
_SUMMARY_WORD = re.compile(r"\b(how many|number of|count|total|sum|average|avg|mean|highest|lowest|max|min|percentage|"
                           r"percent|share|rate|ratio|most|least|top|bottom)\b|%", re.IGNORECASE)


def _check_listing(question: str, plan: Plan, cat: Catalog) -> None:
    """'Show me employees in engineering with grade L3' answered by count(*) is the right rows
    in the wrong shape: the question asked to see them. With nothing grouped and no measure
    named, list the rows instead."""
    if not _LISTING_ASK.search(question) or _SUMMARY_WORD.search(question):
        return
    if len(plan.aggregates) != 1 or plan.group_by or plan.having.by or plan.aggregates[0].conditional:
        return
    a = plan.aggregates[0]
    if a.func == "count" and not a.column:
        plan.aggregates = []
        plan.sort = Sort()
        cat.notes.append("Listed the rows — the question asked to see them, not to count them.")
    elif a.func == "count_distinct" and a.column:
        # 'which employees were on leave on …' answered by count_distinct(emp_id) = 10.
        # The distinct values are what was asked for: group by the column, no measure.
        plan.aggregates = []
        plan.group_by = [GroupBy(table=a.table, column=a.column)]
        plan.sort = Sort(by=a.column)
        cat.notes.append(f"Listed the distinct {a.column} values — the question asked which, not how many.")


# Statistics the plan has no operation for. A model that answers 'median salary' with
# avg(base_salary) produces a normal-looking wrong number; a model that declines and offers
# its 'closest attempt' produces a normal-looking non-answer. Both are refused here, by code,
# before any plan is considered. A word that names a real column ('age' when an age column
# exists) is a column, not a statistic.
_UNSUPPORTED = [
    (r"median", "a median"), (r"percentiles?", "a percentile"), (r"quartiles?", "a quartile"),
    (r"standard deviation|std\.? ?dev(?:iation)?|stdev", "a standard deviation"), (r"variance", "a variance"),
    (r"correlat(?:e|ed|ion|ions)", "a correlation"), (r"regression", "a regression"),
    (r"forecast|predict(?:ed|ion)?|projected|projection", "a forecast"),
]
_DATE_ARITH = [(r"tenure", "tenure"), (r"age|aged|older|younger|years old", "age"),
               (r"length of service|years of service", "length of service")]


def check_unsupported(question: str, cat: Catalog) -> None:
    """Raise a capability PlanError if the question asks for a statistic no plan can express."""
    col_words: set[str] = set()  # every word of every column name: 'StartDate' -> start, date
    for t in cat.tables:
        for c in t.columns:
            col_words |= set(re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", c.name).lower().replace("_", " ").split())
    col_words = {re.sub(r"[^a-z0-9]", "", w) for w in col_words}

    def names_a_column(word: str) -> bool:
        return re.sub(r"[^a-z0-9]", "", word.lower()) in col_words
    numeric = next((c.name for t in cat.tables for c in t.columns if c.is_numeric), "a numeric column")
    dated = next((c.name for t in cat.tables for c in t.columns if c.is_temporal), "a date column")
    for pat, what in _UNSUPPORTED:
        m = re.search(rf"\b(?:{pat})\b", question, re.IGNORECASE)
        if m and not names_a_column(m.group(0)):
            raise PlanError(f"I can't compute {what} yet. I can give totals, averages, counts, minimums, maximums "
                            f"and percentages — e.g. 'average {numeric}'.", kind="capability")
    for pat, what in _DATE_ARITH:
        m = re.search(rf"\b(?:{pat})\b", question, re.IGNORECASE)
        if m and not names_a_column(m.group(0)):
            raise PlanError(f"I can't compute {what} yet — it needs date arithmetic (today minus a date), which I "
                            f"don't do. I can group or filter by the date itself — e.g. 'how many per year by {dated}'.",
                            kind="capability")


_WANTS_VALUES = re.compile(r"\b(distinct|unique|list|show|which|who|what are|names? of)\b", re.IGNORECASE)


def _check_bare_group(question: str, plan: Plan, cat: Catalog) -> None:
    """'employees hired in 2022 by department' -> group by department with no measure: six
    department names. A breakdown with nothing to break down is a count, unless the question
    asked for the distinct values themselves."""
    if plan.aggregates or not plan.group_by or plan.select:
        return
    if _WANTS_VALUES.search(question) or not _BREAKDOWN_RE.search(question):
        return
    plan.aggregates = [Aggregate(func="count")]
    cat.notes.append(f"Counted rows per {', '.join(g.column for g in plan.group_by)} — ask for "
                     f"'distinct {plan.group_by[0].column}' if you wanted just the list of values.")


def check_intent(question: str, plan: Plan, cat: Catalog) -> Plan:
    """Intent checks: does the plan answer the question's *shape*?
      - trend: 'monthly X' must be a time series (bucketed group), not one month's number
      - compare: 'A vs B' must be one row per side, not one combined total
      - entity count: 'how many employees' over payroll counts employees, not payroll rows
      - degenerate: count_distinct(X) grouped by X is meaningless
      - top-N: a ranking question must rank (and cut) the rows
    Repairs what code can, raises a fixable PlanError for the rest so the model gets one more go."""
    _check_bare_group(question, plan, cat)
    _check_trend(question, plan, cat)
    _check_compare(question, plan, cat)
    _check_degenerate_group(plan, cat)
    check_entity_count(question, plan, cat)
    _check_listing(question, plan, cat)
    _disclose_aggregate_reading(question, plan, cat)
    if not _TOPN_RE.search(question):
        return plan
    multi_row = bool(plan.group_by) or plan.is_listing
    if not multi_row:
        return plan  # "highest salary" -> max(salary): one row, nothing to rank
    m = _N_RE.search(question)
    n = int(m.group(1) or m.group(2)) if m else 0
    if not plan.sort.by:
        if len(plan.aggregates) == 1:
            plan.sort.by = agg_alias(plan.aggregates[0])
            plan.sort.desc = not _LOW_RE.search(question)
            cat.notes.append(f"Ranked by {plan.sort.by}, {'lowest' if not plan.sort.desc else 'highest'} first.")
        else:
            raise PlanError("The question asks for a ranking, but I couldn't tell what to rank by. "
                            "Say it — e.g. 'top 5 by bonus'.")
    if n and plan.limit == 0:
        plan.limit = n
        cat.notes.append(f"Showing {n}.")
    if not n and plan.limit == 0 and _TOPN_RE.search(question) and not plan.is_listing and len(plan.group_by) >= 1:
        # "which department has the most X": a ranking question with no N means the first row
        if re.search(r"\bwhich\b|\bwho\b", question, re.IGNORECASE):
            plan.limit = 1
            cat.notes.append("Showing the top result.")
    return plan


_SAYS_TOTAL = re.compile(r"\b(total|sum|overall|combined)\b", re.IGNORECASE)
_SAYS_AVG = re.compile(r"\b(average|avg|mean|typical|per employee|per person)\b", re.IGNORECASE)


_SAYS_RATIO = re.compile(r"\b(percentage|percent|pct|share|proportion|ratio|rate|fraction)\b|%", re.IGNORECASE)


def ratio_disclosure(question: str, plan: Plan) -> tuple[bool, str]:
    """A percentage/ratio question answered by a count plan. Returns (can_derive_share, note).
    can_derive_share: one non-time group_by + exactly one plain count(*) -> code can add a
    share column after execution (count / total of the same result), which is the honest
    'percentage of rows' — computed by code, not by the model."""
    if not _SAYS_RATIO.search(question) or any(a.func == "ratio" for a in plan.aggregates):
        return False, ""
    one_group = len(plan.group_by) == 1 and plan.group_by[0].bucket == "none"
    if len(plan.aggregates) == 1 and one_group:
        a = plan.aggregates[0]
        if a.func == "count" and not a.column and not a.conditional:
            return True, "Percentages are of all rows that matched the filters, computed from the counts."
        if a.func == "sum" and not a.conditional:
            return True, f"Percentages are of the total {a.column} across every group, computed from the totals."
    what = ", ".join(f"{_FUNC_WORD.get(a.func, a.func)} of {a.column}" if a.column else "row counts" for a in plan.aggregates) or "row counts"
    example = "percentage of employees where department is Sales"
    return False, (f"Answered with {what} — not a percentage. Divide by the total yourself, or ask for the "
                   f"percentage directly — e.g. '{example}'.")


_SAYS_PAY = re.compile(r"\b(paid|pay|earn(?:ing|er|ers|s)?|compensation|income|wages?)\b", re.IGNORECASE)
_MONEY = re.compile(r"salary|bonus|deduction|pay|wage|comp|allowance|overtime|tax", re.IGNORECASE)


def _disclose_aggregate_reading(question: str, plan: Plan, cat: Catalog) -> None:
    """The model picked a reading the question didn't spell out. Say which, on the result.

    - 'highest salaries' -> sum or avg? Sum favours big groups; the column name is the only clue.
    - 'highest paid' -> base_salary only? A bigger model quietly picks one money column while the
      table also has bonus and deductions; a smaller one tries the arithmetic and gets caught.
      Either way the user must see which columns the number does NOT include."""
    if _SAYS_PAY.search(question):
        for a in plan.aggregates:
            if a.column and a.table and _MONEY.search(a.column):
                others = [c.name for c in cat.table(a.table).columns
                          if c.is_numeric and c.name != a.column and _MONEY.search(c.name)]
                if others:
                    cat.notes.append(f"Read '{' '.join(_SAYS_PAY.findall(question)[:1])}' as {a.column} only — "
                                     f"not including {', '.join(others)}.")
    if not plan.group_by:
        return
    for a in plan.aggregates:
        if a.func == "sum" and not _SAYS_TOTAL.search(question):
            cat.notes.append(f"Read '{a.column}' as a total (sum) per group — ask for 'average {a.column}' if you meant per person.")
        elif a.func == "avg" and not _SAYS_AVG.search(question):
            cat.notes.append(f"Read '{a.column}' as an average per group — ask for 'total {a.column}' if you meant the sum.")


_WORD_STOP = {"the", "and", "for", "with", "per", "each", "every", "how", "many", "much", "what", "which", "who",
              "are", "was", "were", "all", "any", "from", "into", "over", "than", "that", "this", "top", "show",
              "list", "give", "get", "sum", "total", "average", "avg", "mean", "count", "number", "rate", "share",
              "percentage", "percent", "highest", "lowest", "most", "least", "max", "min", "trend", "month",
              "monthly", "week", "weekly", "year", "yearly", "quarter", "quarterly", "day", "daily"}


def _name_tokens(name: str) -> set[str]:
    """'Work-Life Balance Score' -> {work, life, balance, score}; 'DepartmentType' -> {department, type}."""
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)
    return {_singular(t) for t in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if len(t) >= 3}


def _singular(w: str) -> str:
    return w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w


def _reachable(cat: Catalog, tables: list[str]) -> list[str]:
    """The plan's tables plus every table connected to them through the applied links."""
    seen, todo = list(tables), list(tables)
    while todo:
        t = todo.pop()
        for l in cat.links:
            for a, b in ((l.left_table, l.right_table), (l.right_table, l.left_table)):
                if a == t and b not in seen:
                    seen.append(b)
                    todo.append(b)
    return seen


def check_word_ambiguity(question: str, plan: Plan, cat: Catalog) -> None:
    """'average score by department type' — 'score' is a whole word of three columns (Engagement
    Score, Satisfaction Score, Work-Life Balance Score) and nothing else in the question separates
    them. The model picked one and said nothing, or picked Current Employee Rating, a column with no
    'score' in it at all. Either is the quiet narrowing of 'highest paid' -> base_salary, for any
    word rather than money words. Ask, with the candidates as buttons.

    For each column slot the plan filled (a measure or a group column), every content word of the
    question is looked up as a whole token of column names across the tables the plan uses and the
    ones linked to them. Candidates score the number of their tokens the question contains; the
    plan's pick stands when it out-scores every other candidate ('average engagement score' ->
    Engagement Score 2, the others 1). Two cases ask:
      tie          the pick is one of 2-3 equally matched columns
      substitution the word matches 2-3 columns and the pick isn't one of them, and no other slot
                   of the plan accounts for the word either
    A tie among more than three is disclosed on the answer instead, since a button per column stops
    being a question. Measures under sum/avg/min/max only consider numeric candidates. Not run on
    template plans (the column was typed) or after a user's choice."""
    words = {_singular(w) for w in re.split(r"[^A-Za-z0-9]+", question.lower()) if len(w) >= 3} - _WORD_STOP
    if not words:
        return
    slots = [("aggregates", i, a) for i, a in enumerate(plan.aggregates) if a.column and a.table and a.func != "ratio"]
    slots += [("group_by", i, g) for i, g in enumerate(plan.group_by) if g.column and g.table]
    if not slots:
        return
    # words the plan already accounts for: any column it names, in any role, and its table names
    covered: set[str] = set()
    for part in (plan.aggregates, plan.group_by, plan.filters, plan.select):
        for obj in part:
            if getattr(obj, "column", ""):
                covered |= _name_tokens(obj.column)
            if getattr(obj, "where_column", ""):
                covered |= _name_tokens(obj.where_column)
    for t in plan.tables:
        covered |= _name_tokens(t)
    columns = [(t, c) for t in _reachable(cat, plan.tables) for c in cat.table(t).columns]
    for section, idx, obj in slots:
        numeric_only = section == "aggregates" and obj.func in ("sum", "avg", "min", "max")
        picked_tokens = _name_tokens(obj.column)
        for w in sorted((words & picked_tokens) | (words - covered)):
            cands = [(t, c.name) for t, c in columns if w in _name_tokens(c.name) and (c.is_numeric or not numeric_only)]
            names = list(dict.fromkeys(n for _, n in cands))
            if len(names) < 2:
                continue
            substitution = obj.column not in names
            if substitution:
                tied = names  # every matching column is a reading; the pick isn't among them
            else:
                scores = {n: len(_name_tokens(n) & words) for n in names}
                best = max(scores.values())
                tied = [n for n in names if scores[n] == best]
                if len(tied) < 2 or obj.column not in tied:
                    continue  # the question's other words single one out
            tables = {n: t for t, n in cands if n in tied}
            multi = len(set(tables.values())) > 1
            elsewhere = multi or any(t != obj.table for t in tables.values())  # the choice must carry its table
            listed = ", ".join(tied[:-1]) + f" and {tied[-1]}"
            where = " across the linked files" if multi else f" in {tables[tied[0]]}"
            if len(tied) <= 3:
                lead = (f"'{w}' isn't in {obj.column}; it matches {len(tied)} columns{where}: {listed}."
                        if substitution else f"'{w}' matches {len(tied)} columns{where}: {listed}.")
                raise Ambiguous(f"{lead} Which did you mean?", tied, (section, idx, "column"), plan,
                                option_tables=tables if elsewhere else None)
            others = [n for n in tied if n != obj.column]
            cat.notes.append(f"Read '{w}' as {obj.column} — it also matches {', '.join(others)}.")
            return


def explain_missing(question: str, plan: Plan, cat: Catalog) -> str:
    """The refusal when the plan names a file that isn't loaded, written from the real schema only.

    Leads with the user's word for what they wanted ('salary'), never with the name the model
    invented ('payroll'). The word is taken from the columns the plan put on the missing table,
    kept only where it also appears in the question; failing that, a question word no real column
    has. Then the closest real column anywhere, by name similarity, or — if nothing is close —
    what the files do contain.

      "None of the loaded files has a salary column. The closest is Desired Salary in recruitment_data."
      "None of the loaded files has a revenue column. These files cover employee_data, ... and recruitment_data."
    """
    real = {t.name for t in cat.tables}
    words = {_singular(w) for w in re.split(r"[^A-Za-z0-9]+", question.lower()) if len(w) >= 3} - _WORD_STOP
    all_cols = [(t.name, c.name) for t in cat.tables for c in t.columns]
    covered: set[str] = set()
    for t, c in all_cols:
        covered |= _name_tokens(c)
    for t in real:
        covered |= _name_tokens(t)
    # what the model looked for on the file that doesn't exist, in the user's own words
    wanted: list[str] = []
    for part in (plan.aggregates, plan.group_by, plan.filters, plan.select):
        for obj in part:
            if getattr(obj, "table", "") and obj.table not in real and getattr(obj, "column", ""):
                for w in sorted(_name_tokens(obj.column) & words):
                    if w not in wanted:
                        wanted.append(w)
    if not wanted:
        wanted = sorted(words - covered)
    files = ", ".join(t.name for t in cat.tables[:-1]) + (f" and {cat.tables[-1].name}" if len(cat.tables) > 1 else cat.tables[0].name if cat.tables else "")
    if not wanted:
        return f"I couldn't find what you asked for in the loaded files. These files cover {files}."
    term = wanted[0]
    best = max(((_similar(term, c), t, c) for t, c in all_cols), default=(0.0, "", ""))
    lead = f"None of the loaded files has a {term} column."
    if best[0] >= 0.5:
        return f"{lead} The closest is {best[2]} in {best[1]}."
    return f"{lead} These files cover {files}."


# --- plain-English rendering of a plan, for the UI ---------------------------------------
_FUNC_WORD = {"sum": "total", "avg": "average", "count": "count of rows", "count_distinct": "number of distinct",
              "min": "lowest", "max": "highest", "ratio": "percentage"}
_OP_WORD = {"=": "is", "!=": "is not", ">": "is more than", ">=": "is at least", "<": "is less than", "<=": "is at most",
            "in": "is one of", "not in": "is not one of", "contains": "contains", "between": "is between",
            "is null": "is empty", "is not null": "is not empty"}


def describe_plan(plan: Plan) -> str:
    """'Group employee_engagement_survey_data by Employee ID; average Work-Life Balance Score and
    Satisfaction Score; sort by Work-Life Balance Score, highest first.'"""
    parts = []
    tables = " and ".join(plan.tables)
    if plan.group_by:
        groups = " and ".join(f"{g.column} per {g.bucket}" if g.bucket != "none" else g.column for g in plan.group_by)
        parts.append(f"Group {tables} by {groups}")
    elif plan.is_listing:
        cols = ", ".join(s.column for s in plan.select) if plan.select else "all columns"
        parts.append(f"List rows from {tables} ({cols})")
    else:
        parts.append(f"From {tables}")
    if plan.aggregates:
        bits = []
        for a in plan.aggregates:
            if a.func == "ratio":
                val = a.where_value or ", ".join(a.where_values)
                bit = f"percentage where {a.where_column} {_OP_WORD.get(a.where_op, a.where_op)} {val}".rstrip()
                if a.column:
                    bit = f"share of {a.column} where {a.where_column} {_OP_WORD.get(a.where_op, a.where_op)} {val}".rstrip()
            else:
                bit = f"{_FUNC_WORD[a.func]} {a.column}" if a.column else _FUNC_WORD[a.func]
                if a.conditional:
                    val = a.where_value or ", ".join(a.where_values)
                    bit += f" where {a.where_column} {_OP_WORD.get(a.where_op, a.where_op)} {val}".rstrip()
            bits.append(bit)
        parts.append("; ".join(bits))
    for f in plan.filters:
        val = f.value if f.value else ", ".join(f.values)
        parts.append(f"where {f.column} {_OP_WORD.get(f.op, f.op)} {val}".rstrip())
    if plan.having.by:
        parts.append(f"keep only where {plan.having.by} {_OP_WORD.get(plan.having.op, plan.having.op)} {plan.having.value}")
    if plan.sort.by:
        parts.append(f"sort by {plan.sort.by}, {'highest' if plan.sort.desc else 'lowest'} first")
    if plan.limit:
        parts.append(f"top {plan.limit}")
    text = "; ".join(parts)
    return text[0].upper() + text[1:] + "."
