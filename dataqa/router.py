"""Question in, answer out.

    template parser ─┐
                     ├─► plan ─► validate ─► execute
    provider ────────┘     │
                           ├─ fixable error   ─► feed it back to the provider ONCE, re-validate
                           ├─ ambiguous       ─► ask the user (options), no retry
                           └─ capability      ─► refuse, say what can be done instead
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

import duckdb

from dataqa import templates
from dataqa.engine import QueryResult, compile_plan, execute
from dataqa.plan import (Ambiguous, Catalog, MissingTable, Plan, PlanError, check_entity_count, check_intent,
                         check_unsupported, check_word_ambiguity, explain_missing, ratio_disclosure, validate)
from dataqa.providers import GENERIC_DECLINE, ModelDeclined


@dataclass
class Attempt:
    plan: Plan | None
    error: str | None
    source: str  # "template" | "model" | "model:retry" | "user choice"


@dataclass
class Answer:
    question: str
    path: str  # "template" | "model:<name>" | "refused" | "ask"
    plan: Plan | None = None
    result: QueryResult | None = None
    error: str | None = None
    error_kind: str | None = None  # "fixable" | "capability" | "ambiguous"
    ask: Ambiguous | None = None  # set when path == "ask"
    attempts: list[Attempt] = field(default_factory=list)
    elapsed_ms: float = 0.0
    notes: list[str] = field(default_factory=list)
    substitute: bool = False  # the answer is a stand-in for what was asked; say so loudly
    headline: int | None = None  # "how many X over a threshold": the count is the answer, the rows are the detail


_ASKS_COUNT = re.compile(r"\b(how many|number of|count of|count)\b", re.IGNORECASE)
_ASKS_LIST = re.compile(r"^\s*(which|who|list|show|name|give me the|what are the)\b", re.IGNORECASE)


def _headline(question: str, plan: Plan, result: QueryResult, cat: Catalog) -> int | None:
    """'How many X over a threshold': the threshold keeps one row per group, and the question
    wants the count of those rows. The count is the answer; the rows are the detail behind it.
    'Which X ...' wants the rows themselves. Shared by every path that runs a plan."""
    if not plan.having.by or len(result.rows) == 1:
        return None
    if not _ASKS_COUNT.search(question) or _ASKS_LIST.search(question):
        return None
    what = plan.group_by[0].column if plan.group_by else "rows"
    cat.notes.insert(0, f"Counted the {what} values over the threshold; the table lists each one.")
    return len(result.rows)


def answer(question: str, con: duckdb.DuckDBPyConnection, cat: Catalog, provider=None,
           plan_override: Plan | None = None, override_note: str = "") -> Answer:
    """`plan_override` is the plan after the user picked an option from an Ambiguous ask;
    `override_note` is what the answer must say about that choice (Ambiguous.note_for)."""
    t0 = time.perf_counter()
    done = lambda a: (setattr(a, "elapsed_ms", (time.perf_counter() - t0) * 1000), a)[1]  # noqa: E731
    attempts: list[Attempt] = []
    fallback = ""

    if plan_override is not None:
        plan, path, source = plan_override, "model:user-choice", "user choice"
    else:
        plan = templates.parse(question, cat)
        path, source = "template", "template"
        if plan is None:
            # A statistic no plan can express (median, correlation, tenure…) is refused here, by
            # code, before a model gets to answer it with an average or a 'closest attempt'.
            try:
                check_unsupported(question, cat)
            except PlanError as e:
                return done(Answer(question, "refused", error=str(e), error_kind=e.kind))
            if provider is None:
                return done(Answer(question, "refused", error=(
                    "I can only answer simple questions without a model right now — "
                    "e.g. 'total base_salary by department' or 'how many employees'."), error_kind="capability"))
            path, source = f"model:{provider.name}", "model"
            try:
                plan = provider.plan(question, cat)
            except ModelDeclined as e:
                # The model declined. Its text is the model's opinion of its own limits, and it is
                # unreliable at any size: it fabricates schema facts, and it argues against plans
                # that are perfectly valid. So: if it gave a plan and that plan passes OUR checks
                # (validate + compile, incl. fan-out), run the plan and carry the model's doubt as a
                # note. Accept the decline only when there is no plan or our checks reject it.
                reason = str(e)
                if e.plan is not None:
                    trial = e.plan.model_copy(deep=True)
                    try:
                        cat.notes = []
                        trial = validate(trial, cat)
                        trial = check_intent(question, trial, cat)
                        check_word_ambiguity(question, trial, cat)
                        compile_plan(trial, cat)
                    except Ambiguous as amb:
                        attempts.append(Attempt(e.plan, str(amb), source))
                        return done(Answer(question, "ask", plan=e.plan, ask=amb, error_kind="ambiguous", attempts=attempts))
                    except PlanError as ours:
                        if ours.kind == "capability":
                            reason = str(ours)  # our check, our words
                        attempts.append(Attempt(e.plan, reason, source))
                        return done(Answer(question, "refused", plan=e.plan, error=reason, error_kind="capability", attempts=attempts))
                    # valid: run it, and say what the model was unsure about
                    plan = trial
                    doubt = [f"The model wasn't sure this answers the question: {reason}"] if reason and reason != GENERIC_DECLINE else []
                    attempts.append(Attempt(e.plan, f"declined, but the proposal passed every check — ran it", source))
                    try:
                        result = execute(con, plan, cat)
                    except PlanError as ex:
                        attempts.append(Attempt(plan, str(ex), source))
                        return done(Answer(question, "refused", plan=plan, error=str(ex), error_kind=ex.kind, attempts=attempts))
                    attempts.append(Attempt(plan, None, source))
                    from dataqa.providers import short

                    who = short(getattr(provider, 'last_provider', '') or provider.name)
                    headline = _headline(question, plan, result, cat)
                    return done(Answer(question, f"model:{who}", plan=plan, result=result, attempts=attempts,
                                       notes=doubt + cat.notes + result.notes, headline=headline))
                attempts.append(Attempt(None, reason, source))
                return done(Answer(question, "refused", plan=None, error=reason, error_kind="capability", attempts=attempts))
            except PlanError as e:
                msg = str(e)
                fb = getattr(provider, "last_fallbacks", [])
                if fb:  # e.g. Groq was picked but had no key; say so instead of a bare offline refusal
                    from dataqa.providers import label

                    # records are (provider, reason, model); model is set only for a per-model rate limit
                    def who(n, m):
                        return f"{label(n)} {m.rsplit('/', 1)[-1]}" if m else label(n)
                    msg = "; ".join(f"{who(n, m)} — {r}" for n, r, m in fb) + ". Fell back to offline. " + msg
                attempts.append(Attempt(None, msg, source))
                return done(Answer(question, "refused", error=msg, error_kind="capability", attempts=attempts))
            from dataqa.providers import short

            path = f"model:{short(getattr(provider, 'last_provider', '') or provider.name)}"  # who actually answered
            fallback = getattr(provider, "fallback_note", "")

    def full(pl: Plan) -> Plan:
        """Structural validation, then intent checks for plans the model produced."""
        pl = validate(pl, cat)
        if source == "template":
            # A template plan is exactly what was typed, so the model-facing intent checks don't
            # apply. The entity-count repair is about the data, not the model: 'how many employees
            # by status' over attendance must count employees, not attendance rows.
            check_entity_count(question, pl, cat)
            return pl
        pl = check_intent(question, pl, cat)
        if source == "model":  # not after the user's own choice — that would ask the same question again
            check_word_ambiguity(question, pl, cat)
        can_share, note = ratio_disclosure(question, pl)
        if note:
            cat.notes.append(note)
            object.__setattr__(pl, "_derive_share", can_share)
        return pl

    cat.notes = []
    try:
        plan = full(plan)
    except Ambiguous as e:
        attempts.append(Attempt(plan, str(e), source))
        return done(Answer(question, "ask", plan=plan, ask=e, error_kind="ambiguous", attempts=attempts))
    except PlanError as e:
        attempts.append(Attempt(plan, str(e), source))
        can_retry = e.kind == "fixable" and source == "model" and hasattr(provider, "retry")

        def refusal(err: PlanError, pl: Plan) -> str:
            # A made-up file name is the model's mistake, not the user's; say what the files do have.
            return explain_missing(question, pl, cat) if isinstance(err, MissingTable) else str(err)

        if not can_retry:
            return done(Answer(question, "refused", plan=plan, error=refusal(e, plan), error_kind=e.kind, attempts=attempts))
        # One retry: tell the model exactly what was wrong, ask for the corrected plan.
        try:
            plan = provider.retry(question, cat, plan, str(e))
            cat.notes = []
            plan = full(plan)
        except Ambiguous as e2:
            attempts.append(Attempt(plan, str(e2), "model:retry"))
            return done(Answer(question, "ask", plan=plan, ask=e2, error_kind="ambiguous", attempts=attempts))
        except PlanError as e2:
            attempts.append(Attempt(plan, str(e2), "model:retry"))
            return done(Answer(question, "refused", plan=plan, error=refusal(e2, plan), error_kind=e2.kind, attempts=attempts))
        path = f"{path}:retry"

    try:
        result = execute(con, plan, cat)
    except PlanError as e:
        attempts.append(Attempt(plan, str(e), source))
        return done(Answer(question, "refused", plan=plan, error=str(e), error_kind=e.kind, attempts=attempts))
    attempts.append(Attempt(plan, None, source))
    headline = _headline(question, plan, result, cat)
    notes = ([override_note] if override_note else []) + ([fallback] if fallback else []) + cat.notes + result.notes
    return done(Answer(question, path, plan=plan, result=result, attempts=attempts, notes=notes,
                       substitute="substitute" in override_note, headline=headline))
