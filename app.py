"""Data Q&A — upload CSV/Excel, ask questions in plain English, get verified answers."""

from __future__ import annotations

import numbers
import os
import re
import tempfile
import threading
from pathlib import Path

import pandas as pd
import streamlit as st

from dataqa.joins import ConfirmedJoin, confirm, default_selection, detect_joins, manual_join, unrelated_pairs, value_overlap
from dataqa.loader import MAX_FILE_MB, MAX_FILES, LoadError, load_file, open_connection, schema_text
from dataqa.charts import chart_with_reason
from dataqa.plan import Catalog, apply_choice, describe_plan
from dataqa.hosted import resolve_key
from dataqa.providers import build_chain, label, ollama_reachable
from dataqa.router import answer

st.set_page_config(page_title="Data Q&A", layout="wide")


def fmt_number(v) -> str:
    """1234567 -> 1,234,567 · 12.3456 -> 12.35 · anything else -> str. Handles numpy scalars."""
    if v is None:
        return "—"
    if isinstance(v, numbers.Integral):
        return f"{int(v):,}"
    if isinstance(v, numbers.Real):
        f = float(v)
        return f"{f:,.0f}" if f.is_integer() and abs(f) >= 1000 else f"{f:,.2f}"
    return str(v)


def is_missing(v) -> bool:
    """None, NaN or NaT — a measure over zero rows."""
    try:
        return v is None or bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


def no_rows_message(plan) -> str:
    """'No rows matched' plus the filters that were applied, so the user can see which value or
    date range to check — not a generic hint."""
    applied = "; ".join(f"{f.column} {f.op} {f.value or ', '.join(f.values)}".strip() for f in (plan.filters if plan else []))
    if applied:
        return f"No rows matched these filters: {applied}. Check the values — e.g. a department name or a date range."
    return "No rows matched. Check the filter values — e.g. a department name or a date range."


# --- session state -------------------------------------------------------------
def init_state():
    if "workdir" not in st.session_state:
        st.session_state.workdir = Path(tempfile.mkdtemp(prefix="dataqa_"))
        st.session_state.con = open_connection(st.session_state.workdir / "session.duckdb")
        st.session_state.tables = []  # list[TableInfo]
        st.session_state.loaded_files = set()  # original filenames
        # Streamlit reruns the script in a new thread whenever a widget changes, and a rerun can
        # start while a previous run is still loading a file. Both see the file as not-yet-loaded
        # and the second CREATE TABLE fails: 'Table "attendance" already exists'. One loader at a time.
        st.session_state.load_lock = threading.Lock()


init_state()
# DuckDB connections aren't thread-safe and Streamlit runs each rerun on a new thread; an
# overlapping rerun (e.g. changing the provider mid-detection) corrupts in-flight results.
# A cursor is a per-thread handle on the same database.
con = st.session_state.con.cursor()
tables = st.session_state.tables


# --- sidebar: upload -----------------------------------------------------------
with st.sidebar:
    st.title("Data Q&A")
    st.caption(f"Up to {MAX_FILES} CSV/Excel files, {MAX_FILE_MB} MB each.")
    uploads = st.file_uploader(
        "Upload files", type=["csv", "tsv", "xlsx", "xls"], accept_multiple_files=True
    )
    current = [up.name for up in (uploads or [])]

    # A file was removed from the uploader: start the session over from what's left. Tables,
    # detected links and the last answer all depended on the removed file.
    if st.session_state.loaded_files - set(current):
        st.session_state.con.close()
        st.session_state.workdir = Path(tempfile.mkdtemp(prefix="dataqa_"))
        st.session_state.con = open_connection(st.session_state.workdir / "session.duckdb")
        con = st.session_state.con.cursor()
        st.session_state.tables = tables = []
        st.session_state.loaded_files = set()
        for k in ("join_key", "join_choice", "last_question", "last_answer", "answer_key", "plan_override", "ask_origin"):
            st.session_state.pop(k, None)

    st.session_state.setdefault("load_lock", threading.Lock())  # sessions started before this key existed
    with st.session_state.load_lock:
        for up in uploads or []:
            if up.name in st.session_state.loaded_files:
                continue
            if len(st.session_state.loaded_files) >= MAX_FILES:
                st.error(f"Up to {MAX_FILES} files per session — remove one to add {up.name}.")
                break
            path = st.session_state.workdir / up.name
            path.write_bytes(up.getbuffer())
            try:
                result = load_file(con, path, {t.name for t in tables})
                tables.extend(result.tables)
                st.session_state.loaded_files.add(up.name)
                for note in result.notices:
                    st.warning(note)
            except LoadError as e:
                st.error(str(e))

    if tables:
        st.subheader("Loaded")
        for t in tables:
            st.write(f"**{t.name}** — {t.rows:,} rows × {len(t.columns)} cols")

    st.subheader("Model")
    if "ollama_ok" not in st.session_state:
        st.session_state.ollama_ok = ollama_reachable()  # probed once per session; never true on Streamlit Cloud
    names = ["auto", "groq"] + (["ollama"] if st.session_state.ollama_ok else []) + ["stub"]
    default = os.environ.get("DATAQA_PROVIDER", "auto")
    pname = st.selectbox(
        "Provider", names, index=names.index(default) if default in names else 0, format_func=label,
        help=("Auto: first available of Groq → Ollama → Offline. " if st.session_state.ollama_ok else "Auto: Groq if a key is set, else Offline. ")
             + "Open-weight models only (Qwen / gpt-oss). If one fails, the next answers and the answer says so.",
    )
    groq_model = None
    if pname in ("auto", "groq"):
        from dataqa.hosted import GROQ_MODELS, model_short
        env_model = os.environ.get("DATAQA_GROQ_MODEL", GROQ_MODELS[0])
        groq_model = st.selectbox(
            "Groq model", GROQ_MODELS, index=GROQ_MODELS.index(env_model) if env_model in GROQ_MODELS else 0,
            format_func=model_short, key="groq_model",
            help="Tried first. Groq's free-tier limits are per model, so if this one is rate-limited the other two "
                 "are tried before Ollama or offline, and the answer says which one replied.",
        )
    with st.expander("API key (this session only)"):
        st.caption("Kept in memory for this browser session. Never stored, never logged. "
                   "Also read from environment / Streamlit secrets if set there.")
        keys = {
            "groq": st.text_input("Groq API key", type="password", key="key_groq", autocomplete="off"),
        }
        keys["groq"] = (keys["groq"] or "").strip()  # a cleared or blank field must never keep the previous key
        if not keys["groq"] and resolve_key("GROQ_API_KEY"):
            # A key from secrets / the environment is in use. Say so; never show any of it.
            st.caption("Using the app's Groq key — paste your own to use it instead.")
    import hashlib
    key_sig = hashlib.sha256(keys["groq"].encode()).hexdigest()[:12] if keys["groq"] else ""  # which key, never the key
    chain_sig = (pname, key_sig, groq_model)
    if st.session_state.get("chain_sig") != chain_sig:
        st.session_state.provider = build_chain(pname, keys=keys, ollama_ok=st.session_state.ollama_ok, groq_model=groq_model)
        st.session_state.provider_name = pname
        st.session_state.chain_sig = chain_sig
    provider = st.session_state.provider
    st.caption("Will try: " + provider.describe.replace(" → ", ", then "))
    first = provider.providers[0]
    if first.name == "stub":
        st.caption("No key, no network. Simple questions (totals, averages, counts, 'X by Y') are answered "
                   "without a model; anything else needs a model — paste a Groq key above"
                   + (", or run Ollama." if st.session_state.ollama_ok else "."))
        with st.expander("Demo questions that work offline"):
            for kq in first.known_questions:
                st.write(f"· {kq}")
    elif first.name == "ollama":
        st.caption(f"`{first.model}` via Ollama, structured outputs, temperature 0.")
    else:
        st.caption(f"`{first.model}` via {first.name}, structured outputs, temperature 0.")


# --- main -----------------------------------------------------------------------
if not tables:
    st.info("Upload one or more CSV/Excel files to begin. Try the files in `data/demo/`.")
    st.stop()

# --- relationships: detected in code, auto-applied, disclosed ------------------------
# No confirmation step. The best candidate per table pair is applied; the provenance
# line shows what happened and lets the user override. Unrelated pairs are asked about.
#
# Session state holds only plain strings, never JoinCandidate objects: Streamlit reloads
# dataqa modules on a source change, and objects created before the reload fail
# isinstance() against the reloaded class — which silently dropped every link once.
table_key = tuple(t.name for t in tables)
if st.session_state.get("join_key") != table_key:
    st.session_state.join_key = table_key
    st.session_state.join_choice = {}  # (left, right) -> "cand:<describe>" | "manual:lt.lc=rt.rc" | None
    for c in default_selection(detect_joins(con, tables)):
        st.session_state.join_choice[(c.left_table, c.right_table)] = f"cand:{c.describe()}"

# Recomputed every run (fast: ~50 ms on the demo) so nothing stale can survive a reload.
join_cands = detect_joins(con, tables)
by_desc = {c.describe(): c for c in join_cands}
by_name = {t.name: t for t in tables}


def forget_widget(key: str) -> None:
    """Drop a widget's stored value. Streamlit raises KeyError for a key whose widget isn't on
    the page this run (e.g. the override radio after its pair was unlinked)."""
    try:
        del st.session_state[key]
    except KeyError:
        pass


def resolve_choice(pair, key):
    """String in session state -> the live object for this run (JoinCandidate | ConfirmedJoin | None)."""
    if key is None:
        return None
    if key.startswith("cand:"):
        return by_desc.get(key[5:])  # None if detection no longer proposes it
    lt, rest = key[7:].split(".", 1)
    lc, right = rest.split("=", 1)
    rt, rc = right.split(".", 1)
    return manual_join(by_name[lt], lc, by_name[rt], rc)


def manual_match_line(link: ConfirmedJoin) -> str:
    """The same provenance as a detected link — with the match count, which is the one thing
    that tells a non-technical user the columns they picked are wrong."""
    matched, nl, nr = value_overlap(con, link.left_table, link.left_col, link.right_table, link.right_col)
    table, total = (link.left_table, nl) if link.left_unique or not link.right_unique else (link.right_table, nr)
    on = link.left_col if link.left_col == link.right_col else f"{link.left_col} ↔ {link.right_col}"
    return f"Linked {link.left_table} to {link.right_table} on {on} (set by you) · {matched} of {total} {table} matched"


def render_join_provenance(container=st, only: set | None = None):
    """One line per linked pair, expandable to override. Rendered beneath the answer."""
    choice = st.session_state.join_choice
    for pair, key in list(choice.items()):
        if only is not None and pair not in only:
            continue
        chosen = resolve_choice(pair, key)
        alts = [c for c in join_cands if (c.left_table, c.right_table) == pair]
        if chosen is None and not alts:
            continue  # nothing to link and nothing to offer
        manual = chosen if key and key.startswith("manual:") else None
        options = alts + ([manual] if manual else []) + [None]
        keys = [f"cand:{c.describe()}" for c in alts] + ([key] if manual else []) + [None]
        labels = (
            [c.match_line() for c in alts]
            + ([manual_match_line(manual)] if manual else [])
            + ["Not related — don't link these files"]
        )
        cur = keys.index(key)
        # The line itself is plain text; the alternatives sit behind a small "Not right?" expander.
        warn = f"  ⚠️ {'; '.join(chosen.warnings)}" if chosen is not None and manual is None and chosen.warnings else ""
        container.caption((f"Not linked: {pair[0]} and {pair[1]}" if chosen is None else labels[cur]) + warn)


def render_unrelated(container=st, among: set | None = None):
    """The one case where asking is right: no candidate cleared the threshold. Only for the
    tables the current question needed (`among`), never a permanent list of every pair."""
    links = [resolve_choice(p, k) for p, k in st.session_state.join_choice.items()]
    for lt, rt in unrelated_pairs(tables, [l for l in links if l is not None]):
        if among is not None and not ({lt, rt} <= among):
            continue
        with container.expander(f"{lt} and {rt} don't appear to be related — which columns link them?"):
            a, b = st.columns(2)
            lc = a.selectbox(f"Column in {lt}", by_name[lt].column_names, key=f"man_l::{lt}::{rt}")
            rc = b.selectbox(f"Column in {rt}", by_name[rt].column_names, key=f"man_r::{lt}::{rt}")
            if st.button("Link", key=f"man_btn::{lt}::{rt}"):
                st.session_state.join_choice[(lt, rt)] = f"manual:{lt}.{lc}={rt}.{rc}"
                forget_widget(f"override::{(lt, rt)}")  # reset the radio for this pair
                st.rerun()


def confirmed_joins() -> list[ConfirmedJoin]:
    """What the compiler may join on. Nothing else."""
    out = []
    for pair, key in st.session_state.join_choice.items():
        c = resolve_choice(pair, key)
        if c is None:
            continue
        out.append(c if isinstance(c, ConfirmedJoin) else confirm(c))
    return out


st.session_state.confirmed_joins = confirmed_joins()
catalog = Catalog(tables, st.session_state.confirmed_joins)


ASKED_FOR_CHART = re.compile(r"\b(chart|graph|plot|bar|line chart|visuali[sz]e|visual)\b", re.IGNORECASE)


# --- ask: the page's anchor. Big input, first thing on the page, nothing above it. ---------
st.markdown(
    """<style>
    div[data-testid="stTextInput"] input { font-size: 1.35rem; padding: 0.9rem 1rem; }
    </style>""",
    unsafe_allow_html=True,
)
st.title("Ask a question")
cap_col, info_col = st.columns([12, 1])
cap_col.caption(f"{len(tables)} file{'s' if len(tables) != 1 else ''} loaded · "
                + " · ".join(f"{t.name} ({t.rows:,})" for t in tables))
with info_col.popover("ℹ️", help="Files and columns — everything the model sees"):
    st.caption("This is everything the model ever sees. Data rows are never sent to it.")
    for t in tables:
        st.markdown(f"**{t.name}** · {t.rows:,} rows · from {t.source}")
        st.dataframe(
            [
                {
                    "column": c.name,
                    "type": f"{c.dtype} ({c.note})" if c.note else c.dtype,
                    "nulls": f"{c.null_frac:.0%}",
                    "distinct": c.distinct,
                    "range": f"{c.min} → {c.max}" if c.min is not None else "",
                    "samples": ", ".join(str(s) for s in c.samples),
                }
                for c in t.columns
            ],
            hide_index=True,
            width="stretch",
        )
    st.markdown("**Schema as sent to the model**")
    st.code(schema_text(tables), language="text")
with st.container():  # inside a container the chat box sits here, not pinned to the bottom of the page
    question = st.chat_input(
        placeholder="e.g. average base_salary by department · how many employees · total bonus in March 2025",
    )
if question:
    st.session_state.last_question = question
q = st.session_state.get("last_question")
if q:
    st.markdown(f"**{q}**")  # the chat box clears itself after sending; keep the question in view above its answer
    # Streamlit reruns this script on every click; don't re-ask the model unless something changed.
    links_sig = tuple(sorted(j.describe() for j in catalog.links))
    cache_key = (q, st.session_state.chain_sig, links_sig)
    override = st.session_state.pop("plan_override", None)  # (plan, note) after the user picked an option
    if st.session_state.get("answer_key") != cache_key or override is not None:
        hint = "Thinking… (first question loads the model, ~1 min)" if provider.providers[0].name == "ollama" else "Thinking…"
        with st.spinner(hint):
            st.session_state.last_answer = answer(q, con, catalog, provider=st.session_state.provider,
                                                  plan_override=override[0] if override else None,
                                                  override_note=override[1] if override else "")
        st.session_state.answer_key = cache_key
    a = st.session_state.last_answer

    def asker() -> tuple[str, str]:
        """(model, provider) behind this answer's proposal — for an ask, whoever proposed the plan
        that was ambiguous; recorded when the user picks, so the final answer can credit both."""
        chain = st.session_state.provider
        who = a.path.split(":")[1] if a.path.startswith("model:") else ""
        if not who and a.attempts and a.attempts[0].source.startswith("model"):
            who = getattr(chain, "last_provider", "") or getattr(chain, "name", "")
        answered = getattr(chain, "answered_by", None)  # the object that ran: with several Groq models, the name isn't enough
        if answered is None or getattr(answered, "name", "") != who:
            answered = next((p for p in getattr(chain, "providers", []) if p.name == who), None)
        return getattr(answered, "model", "?"), who

    def render_eye():
        """👁 — two layers. The top reads without SQL or JSON: how the question was read (plain
        steps), who answered, which files were used and linked, what the checks changed. The
        exact query, params, plan JSON, attempts and raw model output sit under one collapsed
        'Technical details' fold. Same control for an answer, a refusal and a clarifying question."""
        chain = st.session_state.provider
        model, who = asker()
        if a.path == "template":
            model_line = "Answered directly from the question — no model"
        elif a.path == "model:user-choice":
            origin = st.session_state.get("ask_origin")  # (model, provider) that proposed the plan you chose from
            model_line = f"{origin[0]} via {origin[1]}, then your choice" if origin and origin[1] else "Your choice"
            who = ""  # no model ran for this answer, so no "in the model" time
        elif who in ("offline", "stub"):
            model_line = "Offline — a built-in answer, no model"
        elif not who:
            model_line = "—"
        else:
            model_line = f"{model} via {who}" + (" (after one correction)" if a.path.endswith(":retry") else "")
        hosted = who not in ("offline", "stub", "")
        with st.popover("👁 How this was computed", help="How your question was read, who answered, which files were used — and, under Technical details, the exact query"):
            # --- layer 1: plain words -------------------------------------------------------
            if a.plan is not None:
                steps = describe_plan(a.plan).rstrip(".").split("; ")
                st.markdown("**Read your question as**")
                if len(steps) == 1:
                    st.markdown(steps[0] + ".")
                else:
                    st.markdown("\n".join(f"{i}. {step[0].upper() + step[1:]}" for i, step in enumerate(steps, 1)))
            outcome = {"refused": " · declined — nothing ran", "ask": " · asked you to choose before running"}.get(a.path, "")
            st.markdown(f"**Answered by:** {model_line}{outcome}")
            if a.result is not None:
                used = a.result.compiled.tables
                links = a.result.compiled.used_links
                files = ", ".join(used) if used else "—"
                if links:
                    on = "; ".join(f"{l.left_table} ↔ {l.right_table} on {l.left_col} = {l.right_col}" for l in links)
                    st.markdown(f"**Files used:** {files} — linked {on}. The match counts are under the answer.")
                else:
                    st.markdown(f"**Files used:** {files}")
            if a.notes:
                st.markdown("**Checks applied**")
                st.markdown("\n".join(f"- {n}" for n in a.notes))
            if a.result is not None and st.session_state.get("chart_reason"):
                st.markdown(f"**Chart:** {st.session_state.chart_reason}")
            in_model = f" · {chain.last_ms:.0f} ms in the model" if hosted and getattr(chain, "last_ms", 0) else ""
            st.caption(f"{a.elapsed_ms:.0f} ms{in_model}")
            # --- layer 2: the exact record, folded --------------------------------------------
            with st.expander("Technical details — query, parameters, proposal, attempts"):
                if a.result is not None:
                    st.markdown("**Query that ran**")
                    params = ("\n-- params: " + str(a.result.compiled.params)) if a.result.compiled.params else ""
                    st.code(a.result.compiled.sql + params, language="sql")
                else:
                    st.markdown("**Query:** none ran — the proposal was stopped before it reached the database.")
                if a.plan is not None:
                    st.markdown("**Proposal (JSON)**")
                    st.json(a.plan.model_dump(exclude_none=True, exclude_defaults=True), expanded=False)
                if len(a.attempts) > 1 or (a.attempts and a.attempts[0].error):
                    st.markdown("**Attempts**")
                    for i, at in enumerate(a.attempts, 1):
                        src = {"model": "model", "model:retry": "model, second try", "template": "no model", "user choice": "your choice"}.get(at.source, at.source)
                        st.markdown(f"{i}. **{src}** — " + (f"❌ {at.error}" if at.error else "✅ ran"))
                        if at.plan is not None and at.plan is not a.plan:
                            st.caption(describe_plan(at.plan))
                if hosted and getattr(chain, "last_raw", ""):
                    st.markdown("**Raw model output**")
                    st.code(chain.last_raw, language="json", wrap_lines=True)

    if a.path == "ask":
        # One detail is ambiguous: ask once, with the options as buttons. No conversation.
        # A substitute offer is a warning, not a question — the user asked for something else.
        (st.warning if a.ask.substitute else st.info)(a.ask.question)
        cols = st.columns(len(a.ask.options))
        for col, opt in zip(cols, a.ask.options):
            label = f"{opt} only" if a.ask.substitute else opt
            if col.button(label, key=f"choice::{opt}", width="stretch"):
                st.session_state.plan_override = (apply_choice(a.ask.plan, a.ask.location, opt, a.ask.table_for(opt)), a.ask.note_for(opt))
                st.session_state.ask_origin = asker()
                st.rerun()
        render_eye()
    elif a.error:
        st.error(a.error)
        render_eye()
    else:
        r = a.result
        chart_reason = ""
        st.session_state.chart_reason = ""
        asked = bool(ASKED_FOR_CHART.search(q))
        if a.headline is not None:
            st.metric(label=q, value=f"{a.headline:,}")
            chart_reason = "No chart: you asked how many; the count is the answer. The table lists them."
            st.session_state.chart_reason = chart_reason
            st.dataframe(r.rows, hide_index=True, width="stretch")
        elif r.scalar is not None and is_missing(r.scalar):
            # sum/avg/min/max over zero rows is NULL, which pandas shows as nan. Say what happened.
            st.warning(no_rows_message(r.plan))
        elif r.scalar is not None:
            st.metric(label=q, value=fmt_number(r.scalar))
            fig, chart_reason = chart_with_reason(r, force=asked)
            st.session_state.chart_reason = chart_reason
            if fig is not None:
                st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
        elif r.rows.empty:
            st.warning(no_rows_message(r.plan))
        else:
            fig, chart_reason = chart_with_reason(r, force=asked)
            st.session_state.chart_reason = chart_reason
            if fig is not None:
                st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
            elif asked:
                st.warning(chart_reason)  # they asked for a chart and didn't get one: say so without a click
            st.dataframe(r.rows, hide_index=True, width="stretch")
            if r.truncated:
                st.caption(f"Showing the first {len(r.rows):,} rows.")

        # provenance: which files were linked to get this answer, and how
        used = {(l.left_table, l.right_table) for l in r.compiled.used_links}
        if used:
            st.caption("Files linked for this answer:")
            render_join_provenance(only=used)
        for i, n in enumerate(a.notes):
            if a.substitute and i == 0:
                st.warning(n)  # "Ranked by base_salary only — not base_salary + bonus - deductions. ..."
            else:
                st.caption(n)
        render_eye()

    # Link controls only when this question needed tables that aren't linked.
    # A refusal on a multi-file question still shows the links it would have used: a wrong link
    # (0 of 120 matched) is usually *why* it was refused, and the count is how the user sees that.
    if a.error and a.plan is not None and len(a.plan.tables) > 1:
        used_tables = set(a.plan.tables)
        pairs = {p for p in st.session_state.join_choice if set(p) <= used_tables}
        if pairs:
            st.caption("Files this question links:")
        render_join_provenance(only=pairs)
        if "aren't linked" in a.error:
            render_unrelated(among=used_tables)
