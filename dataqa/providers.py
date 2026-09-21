"""Plan providers: where a Plan comes from when the template parser can't produce one.

    stub    deterministic canned plans for the demo questions. No model, no network. Default.
    ollama  local open-source model via Ollama structured outputs (schema-constrained decoding).
    hosted  any OpenAI-compatible endpoint serving an open-source model (env-configured).

Every provider returns a Plan or raises PlanError. The plan is then validated against the
real catalog and compiled by code — a provider never executes anything.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass

from pydantic import BaseModel, Field, ValidationError

from dataqa.loader import schema_text
from dataqa.plan import Aggregate, Catalog, ColumnRef, Filter, GroupBy, Plan, PlanError, Sort, strict_schema

DEFAULT_OLLAMA_MODEL = "qwen3.5:4b"


class ModelOutput(BaseModel):
    """What the model must emit. It may decline instead of inventing a plan."""

    can_answer: bool = Field(description="false if the question needs something a plan can't express")
    reason: str = Field(default="", description="If can_answer is false: one sentence saying why, for the user.")
    plan: Plan | None = None


# --- prompt --------------------------------------------------------------------------

SYSTEM = """You turn a question about uploaded data tables into a query plan (JSON). You never see data rows.

Rules:
- Use only the tables and columns listed. Column names must match exactly.
- Do NOT specify joins. List every table you need in "tables"; they are linked automatically using the links shown.
- Filters: values are strings. Dates may be partial: "2025", "2025-03", "March 2025", "2025-Q1". A month, quarter or year filter is ONE "=" filter with a partial date. Text comparisons are case-insensitive.
- Every key must be present. Use "" for an unused string, [] for an unused list, "none" for no bucket, 0 for no limit.
- A "trend" or "over time" or "monthly/weekly/yearly" question = group_by with a bucket on a date column.
- "Compare A and B" = a filter with op "in" plus a group_by on that column.
- "Which X has the highest Y" = group_by X, aggregate Y, sort desc, limit 1.
- "List/show" rows = no aggregates; optional "select" columns; limit.
- "Top N X by Y", "N highest/lowest", "which X has the most" = group_by X (include the id and name columns), aggregate Y, sort by that aggregate's alias (desc true for highest), and limit N (1 for "which"). Never leave sort.by empty or limit 0 on a ranking question.
- aggregates: sum/avg only on numeric columns. count with no column = count rows. count_distinct for "how many distinct".
- Any aggregate can be conditional with where_column / where_op / where_value: "present days" = count where status = Present.
- Percentages and rates use func "ratio" with a condition: "percentage completed" = ratio where Outcome = Completed (percentage of rows in the group). "attendance rate per employee" = group_by employee, ratio where status = Present.
- A threshold on a measure ("more than 75% attendance", "at least 10 leave days") = "having": {"by": <that aggregate's alias>, "op": ">", "value": "75"} — the group_by is what the threshold applies to (per employee, per department).
- If the question needs per-row arithmetic (a + b) or anything else not expressible, set can_answer=false and say why in one plain sentence, then one sentence on the closest thing you CAN answer. Never approximate. Even when declining, fill "plan" with your closest attempt (the tables and columns you would have used) — it is checked, not run.
"""

EXAMPLES = [
    ("total bonus paid in March 2025",
     {"can_answer": True, "reason": "", "plan": {"tables": ["payroll"],
      "filters": [{"table": "payroll", "column": "pay_month", "op": "=", "value": "2025-03", "values": []}],
      "group_by": [], "aggregates": [{"func": "sum", "table": "payroll", "column": "bonus", "alias": "", "where_column": "", "where_op": "", "where_value": "", "where_values": []}],
      "select": [], "sort": {"by": "", "desc": False}, "having": {"by": "", "op": "", "value": ""}, "limit": 0}}),
    ("average hours worked per month",
     {"can_answer": True, "reason": "", "plan": {"tables": ["attendance"], "filters": [],
      "group_by": [{"table": "attendance", "column": "date", "bucket": "month"}],
      "aggregates": [{"func": "avg", "table": "attendance", "column": "hours", "alias": "", "where_column": "", "where_op": "", "where_value": "", "where_values": []}],
      "select": [], "sort": {"by": "", "desc": False}, "having": {"by": "", "op": "", "value": ""}, "limit": 0}}),
    ("which department has the most leave days in Q1 2025",
     {"can_answer": True, "reason": "", "plan": {"tables": ["attendance", "employees"],
      "filters": [{"table": "attendance", "column": "status", "op": "=", "value": "Leave", "values": []},
                  {"table": "attendance", "column": "date", "op": "=", "value": "2025-Q1", "values": []}],
      "group_by": [{"table": "employees", "column": "department", "bucket": "none"}],
      "aggregates": [{"func": "count", "table": "", "column": "", "alias": "leave_days", "where_column": "", "where_op": "", "where_value": "", "where_values": []}],
      "select": [], "sort": {"by": "leave_days", "desc": True}, "having": {"by": "", "op": "", "value": ""}, "limit": 1}}),
    ("compare total deductions between HR and Finance",
     {"can_answer": True, "reason": "", "plan": {"tables": ["employees", "payroll"],
      "filters": [{"table": "employees", "column": "department", "op": "in", "value": "", "values": ["HR", "Finance"]}],
      "group_by": [{"table": "employees", "column": "department", "bucket": "none"}],
      "aggregates": [{"func": "sum", "table": "payroll", "column": "deductions", "alias": "", "where_column": "", "where_op": "", "where_value": "", "where_values": []}],
      "select": [], "sort": {"by": "", "desc": False}, "having": {"by": "", "op": "", "value": ""}, "limit": 0}}),
    ("top 5 employees by bonus",
     {"can_answer": True, "reason": "", "plan": {"tables": ["employees", "payroll"], "filters": [],
      "group_by": [{"table": "employees", "column": "employee_id", "bucket": "none"},
                   {"table": "employees", "column": "name", "bucket": "none"}],
      "aggregates": [{"func": "sum", "table": "payroll", "column": "bonus", "alias": "total_bonus", "where_column": "", "where_op": "", "where_value": "", "where_values": []}],
      "select": [], "sort": {"by": "total_bonus", "desc": True}, "having": {"by": "", "op": "", "value": ""}, "limit": 5}}),
    ("which employees have more than 75% attendance",
     {"can_answer": True, "reason": "", "plan": {"tables": ["attendance"], "filters": [],
      "group_by": [{"table": "attendance", "column": "emp_id", "bucket": "none"}],
      "aggregates": [{"func": "ratio", "table": "attendance", "column": "", "alias": "pct_present",
                      "where_column": "status", "where_op": "=", "where_value": "Present", "where_values": []}],
      "select": [], "sort": {"by": "pct_present", "desc": True},
      "having": {"by": "pct_present", "op": ">", "value": "75"}, "limit": 0}}),
    ("what percentage of attendance records are WFH, by department",
     {"can_answer": True, "reason": "", "plan": {"tables": ["attendance", "employees"], "filters": [],
      "group_by": [{"table": "employees", "column": "department", "bucket": "none"}],
      "aggregates": [{"func": "ratio", "table": "attendance", "column": "", "alias": "pct_wfh",
                      "where_column": "status", "where_op": "=", "where_value": "WFH", "where_values": []}],
      "select": [], "sort": {"by": "pct_wfh", "desc": True}, "having": {"by": "", "op": "", "value": ""}, "limit": 0}}),
    ("net pay per employee",
     {"can_answer": False, "reason": "Net pay is base_salary + bonus - deductions, and I can't combine columns arithmetically. "
      "I can give total base_salary, bonus and deductions per employee instead.", "plan": None}),
]


def build_messages(question: str, cat: Catalog) -> list[dict]:
    """Schema context once, in the system message; examples are bare Q -> A pairs.
    Repeating the context per example put the prompt at ~7k tokens for no gain."""
    links = "\n".join(f"- {l.left_table}.{l.left_col} = {l.right_table}.{l.right_col}" for l in cat.links) or "- (none)"
    context = f"TABLES:\n{schema_text(cat.tables)}\n\nLINKS BETWEEN TABLES:\n{links}"
    msgs = [{"role": "system", "content": f"{SYSTEM}\n{context}"}]
    for q, out in EXAMPLES:
        msgs.append({"role": "user", "content": f"QUESTION: {q}"})
        msgs.append({"role": "assistant", "content": json.dumps(out)})
    msgs.append({"role": "user", "content": f"QUESTION: {question}"})
    return msgs


def retry_messages(question: str, cat: Catalog, failed: Plan, error: str) -> list[dict]:
    msgs = build_messages(question, cat)
    msgs.append({"role": "assistant", "content": json.dumps({"can_answer": True, "reason": "", "plan": failed.model_dump()})})
    msgs.append({"role": "user", "content": f"That plan didn't validate: {error} "
                 "Fix only that and return the full corrected plan. If it can't be fixed, set can_answer=false."})
    return msgs


GENERIC_DECLINE = ("I can't express this question with the operations I have "
                   "(filter, group, total, average, count, min, max).")


class ModelDeclined(PlanError):
    """The model said it can't answer. Carries its best-attempt plan (if any) so our own
    checks can run on it before we accept the decline."""

    def __init__(self, reason: str, plan: Plan | None):
        super().__init__(reason, kind="capability")
        self.plan = plan


def sanitise_reason(reason: str, cat: Catalog | None) -> str:
    """A decline is the only path where unvalidated model text reaches the user. Scan it for
    table/column names; any sentence that pairs a column with a table it isn't in is dropped.
    If nothing survives, the generic refusal is shown. Never print a claim the schema contradicts."""
    if not reason or cat is None:
        return reason or GENERIC_DECLINE
    tables = {t.name.lower(): t for t in cat.tables}
    col_home = {}  # column name (lower) -> set of tables that have it
    for t in cat.tables:
        for c in t.columns:
            col_home.setdefault(c.name.lower(), set()).add(t.name.lower())
    kept = []
    for sentence in re.split(r"(?<=[.!?])\s+", reason.strip()):
        low = sentence.lower()
        mentioned_tables = [n for n in tables if re.search(rf"\b{re.escape(n)}\b", low)]
        mentioned_cols = [c for c in col_home if len(c) > 2 and re.search(rf"\b{re.escape(c)}\b", low)]
        contradicted = any(
            mentioned_tables and not (col_home[c] & set(mentioned_tables)) and c not in tables
            for c in mentioned_cols
        )
        if not contradicted:
            kept.append(sentence)
    return " ".join(kept).strip() or GENERIC_DECLINE


def parse_output(text: str, cat: Catalog | None = None) -> Plan:
    try:
        out = ModelOutput.model_validate_json(text)
    except ValidationError as e:
        raise PlanError("The model's answer wasn't in a form I could check, so nothing ran — try rephrasing the question. "
                        f"(Detail: {e.errors()[0].get('msg', e)}.)", kind="capability") from e
    if not out.can_answer or out.plan is None:
        raise ModelDeclined(sanitise_reason(out.reason, cat), out.plan)
    return out.plan


# --- providers -----------------------------------------------------------------------

@dataclass
class ProviderInfo:
    name: str
    detail: str


class StubProvider:
    """Canned plans keyed by normalised question. Lets a reviewer run the app with no model."""

    name = "stub"

    def __init__(self):
        self.plans: dict[str, Plan] = {}
        for q, p in _STUB_PLANS.items():
            self.plans[_norm(q)] = p

    @property
    def known_questions(self) -> list[str]:
        return list(_STUB_PLANS)

    def plan(self, question: str, cat: Catalog) -> Plan:
        p = self.plans.get(_norm(question))
        if p is None:
            raise PlanError("Without a model I can only answer simple questions — totals, averages, counts and "
                            "'X by Y' breakdowns — plus the demo questions listed in the sidebar. For anything else, "
                            "paste a Groq key in the sidebar, or run Ollama.", kind="capability")
        return p.model_copy(deep=True)


class OllamaProvider:
    name = "ollama"

    def __init__(self, model: str | None = None, host: str | None = None):
        self.model = model or os.environ.get("DATAQA_OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)
        self.host = host or os.environ.get("OLLAMA_HOST")
        self.last_ms = 0.0
        self.last_raw = ""

    def plan(self, question: str, cat: Catalog) -> Plan:
        self._cat = cat
        return self._call(build_messages(question, cat))

    def retry(self, question: str, cat: Catalog, failed: Plan, error: str) -> Plan:
        """One correction pass: show the model its own plan and what was wrong with it."""
        self._cat = cat
        return self._call(retry_messages(question, cat, failed, error))

    def _call(self, messages: list[dict]) -> Plan:
        import ollama

        client = ollama.Client(host=self.host)
        t0 = time.perf_counter()
        try:
            resp = client.chat(
                model=self.model,
                messages=messages,
                format=strict_schema(ModelOutput),  # constrained decoding, every key required
                options={"temperature": 0, "num_ctx": 16384},
                think=False,
            )
        except Exception as e:  # connection refused, model missing, ...
            if "not found" in str(e).lower() or "pull" in str(e).lower():
                raise ProviderUnavailable("ollama", f"model {self.model} isn't installed — run `ollama pull {self.model}`") from e
            raise ProviderUnavailable("ollama", f"couldn't reach Ollama ({self.model}) — is it running?") from e
        self.last_ms = (time.perf_counter() - t0) * 1000
        self.last_raw = resp.message.content or ""
        return parse_output(self.last_raw, self._cat)


from dataqa.hosted import (  # noqa: E402  (hosted providers live in their own module)
    GroqProvider, HostedProvider, OpenRouterProvider, ProviderChain, ProviderUnavailable, ollama_reachable, resolve_key,
)

PROVIDER_ORDER = ["groq", "ollama", "stub"]  # auto-chain order; the offline provider always answers last
OPT_IN = ["openrouter", "hosted"]  # only when picked explicitly (OpenRouter needs a credit balance even for open models)

LABELS = {"stub": "Offline (no model)", "ollama": "Ollama (local)", "groq": "Groq", "openrouter": "OpenRouter (opt-in)",
          "hosted": "Custom endpoint", "auto": "Auto"}
SHORT = {"stub": "offline"}  # for tables and paths shown to a reviewer


def label(name: str) -> str:
    return LABELS.get(name, name)


def short(name: str) -> str:
    return SHORT.get(name, name)


def make_provider(name: str, **kw):
    cls = {"stub": StubProvider, "ollama": OllamaProvider, "hosted": HostedProvider,
           "groq": GroqProvider, "openrouter": OpenRouterProvider}[name]
    return StubProvider() if name == "stub" else cls(**{k: v for k, v in kw.items() if v})


def build_chain(preferred: str = "auto", keys: dict[str, str] | None = None, ollama_ok: bool | None = None,
                groq_model: str | None = None) -> ProviderChain:
    """preferred = 'auto' or a provider name to put first. keys = {'groq': ..., 'openrouter': ...}
    from the sidebar (session only); otherwise st.secrets / env. Providers without a key are
    skipped; Ollama only if reachable; stub is always last so something always answers.
    groq_model: the Groq model to try first; the other GROQ_MODELS follow it, so a rate limit on
    one (Groq's limits are per model) moves to the next before leaving Groq."""
    from dataqa.hosted import GROQ_MODELS

    keys = keys or {}
    groq_model = groq_model or os.environ.get("DATAQA_GROQ_MODEL") or GROQ_MODELS[0]
    groq_models = [groq_model] + [m for m in GROQ_MODELS if m != groq_model]
    if ollama_ok is None:
        ollama_ok = ollama_reachable()
    order = PROVIDER_ORDER if preferred == "auto" else [preferred] + [n for n in PROVIDER_ORDER if n != preferred]
    chain = []
    for n in order:
        if n == "groq":
            key = resolve_key("GROQ_API_KEY", keys.get("groq"))
            ps = [GroqProvider(model=m, key=key) for m in groq_models]
            if ps[0].configured or preferred == n:
                chain.extend(ps)
        elif n == "openrouter":  # opt-in: only when picked
            chain.append(OpenRouterProvider(key=resolve_key("OPENROUTER_API_KEY", keys.get("openrouter"))))
        elif n == "hosted":  # opt-in: only when picked
            chain.append(HostedProvider(key=resolve_key("DATAQA_HOSTED_KEY", keys.get("hosted"))))
        elif n == "ollama":
            if ollama_ok or preferred == n:
                chain.append(OllamaProvider())
        elif n == "stub":
            chain.append(StubProvider())
    return ProviderChain(chain)


def _norm(q: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", q.lower()).strip()


# --- stub plans: the demo question set ------------------------------------------------
_STUB_PLANS: dict[str, Plan] = {
    "total bonus in March 2025": Plan(
        tables=["payroll"],
        filters=[Filter(table="payroll", column="pay_month", op="=", value="2025-03")],
        aggregates=[Aggregate(func="sum", table="payroll", column="bonus")]),
    "how many employees are in Engineering": Plan(
        tables=["employees"],
        filters=[Filter(table="employees", column="department", op="=", value="Engineering")],
        aggregates=[Aggregate(func="count", table="employees")]),
    "average hours worked per month": Plan(
        tables=["attendance"],
        group_by=[GroupBy(table="attendance", column="date", bucket="month")],
        aggregates=[Aggregate(func="avg", table="attendance", column="hours")]),
    "monthly total base salary": Plan(
        tables=["payroll"],
        group_by=[GroupBy(table="payroll", column="pay_month", bucket="month")],
        aggregates=[Aggregate(func="sum", table="payroll", column="base_salary")]),
    "which department has the highest average base salary": Plan(
        tables=["employees", "payroll"],
        group_by=[GroupBy(table="employees", column="department")],
        aggregates=[Aggregate(func="avg", table="payroll", column="base_salary", alias="avg_salary")],
        sort=Sort(by="avg_salary", desc=True), limit=1),
    "leave days by department in Q1 2025": Plan(
        tables=["attendance", "employees"],
        filters=[Filter(table="attendance", column="status", op="=", value="Leave"),
                 Filter(table="attendance", column="date", op="=", value="2025-Q1")],
        group_by=[GroupBy(table="employees", column="department")],
        aggregates=[Aggregate(func="count", alias="leave_days")]),
    "compare average bonus between Sales and Engineering": Plan(
        tables=["employees", "payroll"],
        filters=[Filter(table="employees", column="department", op="in", values=["Sales", "Engineering"])],
        group_by=[GroupBy(table="employees", column="department")],
        aggregates=[Aggregate(func="avg", table="payroll", column="bonus")]),
    "list employees in Hyderabad": Plan(
        tables=["employees"],
        filters=[Filter(table="employees", column="location", op="=", value="Hyderabad")],
        select=[ColumnRef(table="employees", column="employee_id"), ColumnRef(table="employees", column="name"),
                ColumnRef(table="employees", column="department")]),
}
