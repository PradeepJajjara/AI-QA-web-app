"""Headless UI tests via streamlit.testing. These exercise app.py the way a user sees it —
in particular the state right after upload, before anything is clicked."""

import importlib
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from dataqa.loader import load_file, open_connection
from dataqa.plan import Aggregate, GroupBy, Plan, Sort
from dataqa.hosted import ProviderChain
from dataqa.providers import StubProvider

DEMO = Path(__file__).parent.parent / "data" / "demo"
APP = str(Path(__file__).parent.parent / "app.py")


@pytest.fixture(autouse=True)
def offline_only(monkeypatch, tmp_path):
    """UI tests never touch a model or the network: offline provider, no keys, no Ollama probe."""
    import streamlit
    monkeypatch.chdir(tmp_path)  # away from any real .streamlit/secrets.toml
    monkeypatch.setenv("DATAQA_PROVIDER", "stub")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(streamlit, "secrets", {})
    import dataqa.providers
    monkeypatch.setattr(dataqa.providers, "ollama_reachable", lambda *a, **k: False)


def booted(files=("employees.csv", "payroll.csv", "attendance.xlsx")) -> AppTest:
    """AppTest with the demo files already loaded (the uploader can't be driven headlessly)."""
    at = AppTest.from_file(APP, default_timeout=30)
    con = open_connection(None)
    tables = []
    for f in files:
        tables += load_file(con, DEMO / f, {t.name for t in tables}).tables
    at.session_state.workdir = Path(".")
    at.session_state.con = con
    at.session_state.tables = tables
    at.session_state.loaded_files = set()  # the headless uploader is empty; a non-empty set would read as "file removed"
    return at.run()


def ask(at: AppTest, q: str) -> AppTest:
    return at.chat_input[0].set_value(q).run()


def answer_frames(at: AppTest):
    """Dataframes that belong to the answer (the info popover's schema tables all start with 'column')."""
    return [d.value for d in at.dataframe if list(d.value.columns)[:2] != ["column", "type"]]


def link_lines(at: AppTest) -> list[str]:
    return [c.value.split("  ⚠️")[0] for c in at.caption if c.value.startswith("Linked")]


# --- the state after upload, before any click -----------------------------------------

def test_detected_links_are_active_with_no_interaction():
    at = booted()
    assert not at.exception
    links = sorted(j.describe() for j in at.session_state.confirmed_joins)
    assert links == ["employees.employee_id = attendance.emp_id", "employees.employee_id = payroll.staff_id"]


def test_cross_file_template_question_answers_immediately():
    at = ask(booted(), "average base_salary by department")
    assert not at.error and not at.exception
    assert list(answer_frames(at)[0].columns) == ["department", "avg_base_salary"]
    assert link_lines(at) == ["Linked employees to payroll on employee_id ↔ staff_id · 120 of 120 employees matched"]


def test_cross_file_model_question_answers_immediately():
    at = ask(booted(), "which department has the highest average base salary")  # stub plan, joins employees+payroll
    assert not at.error and not at.exception
    assert len(link_lines(at)) == 1


def test_links_survive_a_module_reload():
    """Streamlit reloads dataqa modules on a source change; session state must not hold
    objects whose class identity changes. (This is the regression that dropped every link.)"""
    at = booted()
    import dataqa.joins
    importlib.reload(dataqa.joins)
    at = ask(at, "average base_salary by department")
    assert not at.error and len(at.session_state.confirmed_joins) == 2


# --- override / unlink / manual --------------------------------------------------------

def test_provenance_line_is_plain_text_with_no_override_control():
    at = ask(booted(), "average base_salary by department")
    assert len(link_lines(at)) == 1
    assert not [e for e in at.expander if e.label == "Not right?"]
    assert not [r for r in at.radio if r.key and r.key.startswith("override::")]


def test_no_link_prompts_on_single_table_or_linked_answers():
    at = ask(booted(), "how many employees")
    assert not [e for e in at.expander if "don't appear to be related" in e.label]
    at = ask(at, "average base_salary by department")
    assert not [e for e in at.expander if "don't appear to be related" in e.label]


# --- ask flows ---------------------------------------------------------------------------

def test_substitute_ask_labels_result():
    at = booted()

    class PayStub(StubProvider):
        def plan(self, q, cat):
            return Plan(tables=["employees", "payroll"], group_by=[GroupBy(table="employees", column="name")],
                        aggregates=[Aggregate(func="sum", table="payroll", column="base_salary + bonus - deductions", alias="pay")],
                        sort=Sort(by="pay", desc=True), limit=5)

    at.session_state.provider = ProviderChain([PayStub()])
    at = ask(at, "show me the 5 highest paid employees")
    assert at.warning and "not the same thing" in at.warning[0].value
    assert [b.label for b in at.button if b.key and b.key.startswith("choice::")] == ["base_salary only", "bonus only", "deductions only"]
    at = [b for b in at.button if b.key == "choice::base_salary"][0].click().run()
    assert len(answer_frames(at)[0]) == 5
    assert "Ranked by base_salary only — not base_salary + bonus - deductions" in at.warning[0].value



def test_sidebar_shows_chain_and_masked_keys():
    at = booted()
    assert any(c.value == "Will try: offline" for c in at.caption)
    keys = [t for t in at.text_input if t.key == "key_groq"]
    assert len(keys) == 1
    from streamlit.proto.TextInput_pb2 import TextInput as P
    assert all(t.proto.type == P.PASSWORD for t in keys)  # masked, never shown



def test_removing_a_file_resets_tables_links_and_answer():
    """Uploader shows fewer files than were loaded -> everything derived from them is dropped."""
    at = ask(booted(), "average base_salary by department")
    assert at.session_state.last_answer is not None and len(at.session_state.tables) == 3
    at.session_state.loaded_files = {"employees.csv"}  # was loaded; the (empty) uploader no longer lists it
    at = at.run()
    assert at.session_state.tables == [] and "last_answer" not in at.session_state and "join_choice" not in at.session_state
    assert any("Upload one or more" in i.value for i in at.info)


def test_schema_lives_behind_the_info_icon():
    at = booted()
    assert not [e for e in at.expander if "Files and columns" in e.label]  # no text expander any more


# --- overnight bug hunt additions (see OVERNIGHT.md) ------------------------------------

def test_total_over_no_matching_rows_says_so_instead_of_nan():
    """'total training cost in 2023' on a text date column showed the number 'nan'. A sum over
    zero rows is NULL; the UI must say no rows matched, not print nan."""
    from dataqa.plan import Filter
    at = booted()

    class EmptyStub(StubProvider):
        def plan(self, q, cat):
            return Plan(tables=["payroll"], filters=[Filter(table="payroll", column="pay_month", op="=", value="2031")],
                        aggregates=[Aggregate(func="sum", table="payroll", column="bonus")])

    at.session_state.provider = ProviderChain([EmptyStub()])
    at = ask(at, "total bonus in 2031")
    assert not at.exception
    assert not at.metric or all("nan" not in m.value.lower() for m in at.metric)
    assert any("No rows matched" in w.value for w in at.warning)
    assert any("pay_month" in w.value for w in at.warning)  # names the filter that matched nothing


def test_sidebar_and_eye_use_plain_words():
    at = ask(booted(), "how many employees")
    texts = [c.value for c in at.caption] + [m.value for m in at.markdown]
    joined = "\n".join(texts).lower()
    assert "template" not in joined and "stub" not in joined and "parser" not in joined
    assert any("answered directly from the question" in t.lower() for t in texts)


def test_manual_link_shows_its_match_count():
    """A link the user set by hand showed 'Linked employees.employee_id = payroll.staff_id (set
    manually)' with no match count — and the count is the one thing that tells a non-technical
    user the link is wrong."""
    at = booted()
    at.session_state.join_choice[("employees", "payroll")] = "manual:employees.employee_id=payroll.staff_id"
    at = ask(at.run(), "average base_salary by department")
    assert not at.exception and not at.error
    lines = link_lines(at)
    assert lines and "120 of 120 employees matched" in lines[0] and "set by you" in lines[0]
    # the wrong columns: the count says so, and the answer doesn't pretend
    at.session_state.join_choice[("employees", "payroll")] = "manual:employees.name=payroll.staff_id"
    at = ask(at.run(), "average base_salary by location")
    assert not at.exception
    text = "\n".join(c.value for c in at.caption) + "\n".join(w.value for w in at.warning)
    assert "0 of 84 employees matched" in text  # 84 distinct names, none of them a staff_id


def test_eye_credits_the_proposing_model_after_a_choice():
    """After a button, the answer ran no model. The 👁 line names the one whose proposal was
    chosen from — not '? via user-choice'."""
    at = booted()

    class PayStub(StubProvider):
        name, model = "groq", "qwen/qwen3.8-27b"

        def plan(self, q, cat):
            return Plan(tables=["employees", "payroll"], group_by=[GroupBy(table="employees", column="name")],
                        aggregates=[Aggregate(func="sum", table="payroll", column="base_salary + bonus - deductions", alias="pay")],
                        sort=Sort(by="pay", desc=True), limit=5)

    at.session_state.provider = ProviderChain([PayStub()])
    at = ask(at, "show me the 5 highest paid employees")
    lines = [m.value for m in at.markdown if m.value.startswith("**Answered by:**")]
    assert lines and "qwen/qwen3.8-27b via groq" in lines[0]
    at = [b for b in at.button if b.key == "choice::base_salary"][0].click().run()
    lines = [m.value for m in at.markdown if m.value.startswith("**Answered by:**")]
    assert lines and "qwen/qwen3.8-27b via groq, then your choice" in lines[0]
    assert "?" not in lines[0] and "user-choice" not in lines[0] and "in the model" not in lines[0]


def test_date_conversion_shows_beside_the_column_not_as_a_banner(tmp_path):
    """A column the load converted from text says so in the ℹ️ table's type cell, on its own row.
    Nothing about it goes to the sidebar (the loader returns no notice for it)."""
    p = tmp_path / "training.csv"
    p.write_text("Employee ID,Training Date,Training Cost\n1,16-Mar-23,510.83\n2,19-Jul-23,582.37\n", encoding="utf-8")
    at = AppTest.from_file(APP, default_timeout=30)
    con = open_connection(None)
    res = load_file(con, p, set())
    assert res.notices == []
    at.session_state.workdir = Path(".")
    at.session_state.con = con
    at.session_state.tables = res.tables
    at.session_state.loaded_files = set()
    at = at.run()
    frames = [df.value for df in at.dataframe if "column" in df.value.columns]
    assert frames, "the files-and-columns table wasn't rendered"
    rows = frames[0].set_index("column")["type"]
    assert rows["Training Date"] == "DATE (converted from text like 16-Mar-23)"
    assert rows["Training Cost"] == "DOUBLE"
    assert not any("read as dates" in w.value or "converted from text" in w.value for w in at.warning)


def test_eye_reads_plain_on_top_and_keeps_the_sql_under_a_fold():
    """Layer 1 has no SQL or JSON: the question restated as steps, who answered, which files.
    Layer 2 is one collapsed expander holding the exact query with its parameters."""
    at = ask(booted(), "average base_salary by department")
    md = [m.value for m in at.markdown]
    assert any(m.startswith("**Read your question as**") for m in md)
    assert any("1. Group payroll and employees by department" in m and "2. Average base_salary" in m for m in md)
    assert "**Answered by:** Answered directly from the question — no model" in md
    assert any(m.startswith("**Files used:** payroll, employees — linked employees ↔ payroll on employee_id = staff_id") for m in md)
    assert not any("SELECT" in m or '"tables"' in m for m in md)  # nothing technical in the plain layer
    folds = [e for e in at.expander if e.label.startswith("Technical details")]
    assert len(folds) == 1
    sql = [c.value for c in at.code if "SELECT" in c.value]
    assert sql and 'avg("payroll"."base_salary")' in sql[0] and 'JOIN "payroll"' in sql[0] or 'JOIN "employees"' in sql[0]


def test_groq_model_dropdown_defaults_to_qwen_and_lists_the_two_evaluated_models(monkeypatch):
    """Under the Groq provider: a model dropdown with exactly the two models in the results table,
    qwen3.8-27b as default. Offline mode has no dropdown — there's no Groq in that chain."""
    at = booted()
    assert not [s for s in at.selectbox if s.key == "groq_model"]
    monkeypatch.setenv("DATAQA_PROVIDER", "groq")
    at = booted()
    boxes = [s for s in at.selectbox if s.key == "groq_model"]
    assert len(boxes) == 1
    assert boxes[0].options == ["qwen3.8-27b", "gpt-oss-120b"]  # AppTest sees the display labels; only the two evaluated models
    assert boxes[0].value == "qwen/qwen3.8-27b"
    assert any(c.value == "Will try: groq (qwen3.8-27b, then gpt-oss-120b), then offline" for c in at.caption)
    at = boxes[0].select("openai/gpt-oss-120b").run()
    assert any(c.value == "Will try: groq (gpt-oss-120b, then qwen3.8-27b), then offline" for c in at.caption)
    assert any("`openai/gpt-oss-120b` via groq" in c.value for c in at.caption)


def test_app_key_in_secrets_is_announced_but_never_shown(monkeypatch):
    """A key in Streamlit secrets / the environment: the sidebar says the app's key is in use, the
    field stays visible and empty for an override, and no fragment of the key appears anywhere."""
    monkeypatch.setenv("DATAQA_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_secret_key_123")
    at = booted()
    field = [t for t in at.text_input if t.key == "key_groq"]
    assert len(field) == 1 and field[0].value == ""
    assert any(c.value == "Using the app's Groq key — paste your own to use it instead." for c in at.caption)
    texts = [c.value for c in at.caption] + [m.value for m in at.markdown] + [c.value for c in at.code]
    assert not any("gsk_test" in t or "secret_key_123" in t for t in texts)
    assert any(c.value.startswith("Will try: groq (qwen3.8-27b, then gpt-oss-120b)") for c in at.caption)


def test_no_app_key_means_no_such_caption(monkeypatch):
    monkeypatch.setenv("DATAQA_PROVIDER", "groq")
    at = booted()  # offline_only fixture: no key anywhere
    assert not any("Using the app's Groq key" in c.value for c in at.caption)


def test_clearing_the_sidebar_key_returns_to_the_apps_key(monkeypatch):
    """Paste a key: the chain uses it. Clear the field (or leave spaces): the chain is rebuilt on
    the app's key. Change to a different key: rebuilt again — the signature tracks which key."""
    monkeypatch.setenv("DATAQA_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_secret_key_123")
    at = booted()
    groq = lambda at: at.session_state.provider.providers[0]  # noqa: E731
    assert groq(at)._key == "gsk_test_secret_key_123"
    field = lambda at: [t for t in at.text_input if t.key == "key_groq"][0]  # noqa: E731
    at = field(at).set_value("gsk_user_one").run()
    assert groq(at)._key == "gsk_user_one"
    at = field(at).set_value("gsk_user_two").run()
    assert groq(at)._key == "gsk_user_two"  # a second key replaces the first, not ignored
    at = field(at).set_value("   ").run()
    assert groq(at)._key == "gsk_test_secret_key_123"
    at = field(at).set_value("gsk_user_one").run()
    at = field(at).set_value("").run()
    assert groq(at)._key == "gsk_test_secret_key_123"
    assert any(c.value == "Using the app's Groq key — paste your own to use it instead." for c in at.caption)
