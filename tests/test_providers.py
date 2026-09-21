import json
from pathlib import Path

import pytest

from dataqa.joins import confirm, default_selection, detect_joins
from dataqa.loader import load_file, open_connection
from dataqa.plan import Catalog, PlanError
from dataqa.providers import ModelOutput, StubProvider, build_messages, parse_output
from dataqa.router import answer

DEMO = Path(__file__).parent.parent / "data" / "demo"


@pytest.fixture(scope="module")
def demo():
    con = open_connection(None)
    tables = []
    for f in ["employees.csv", "payroll.csv", "attendance.xlsx"]:
        tables += load_file(con, DEMO / f, {t.name for t in tables}).tables
    return con, Catalog(tables, [confirm(c) for c in default_selection(detect_joins(con, tables))])


def test_every_stub_plan_runs(demo):
    con, cat = demo
    stub = StubProvider()
    for q in stub.known_questions:
        a = answer(q, con, cat, provider=stub)
        assert a.error is None, f"{q}: {a.error}"
        assert a.path in ("template", "model:offline")
        assert len(a.result.rows) > 0, q


def test_stub_is_case_and_punctuation_insensitive(demo):
    a = answer("Total bonus in March 2025?", *demo, provider=StubProvider())
    assert a.error is None and a.result.scalar > 0


def test_stub_unknown_question_refuses(demo):
    a = answer("what colour is the sky", *demo, provider=StubProvider())
    assert a.path == "refused" and "Without a model" in a.error


def test_parse_output_decline_becomes_plain_refusal():
    with pytest.raises(PlanError, match="needs a ratio"):
        parse_output(json.dumps({"can_answer": False, "reason": "That needs a ratio."}))


def test_parse_output_plan():
    p = parse_output(json.dumps({"can_answer": True, "plan": {"tables": ["payroll"], "aggregates": [{"func": "count"}]}}))
    assert p.tables == ["payroll"]


def test_parse_output_garbage_is_a_refusal_not_a_crash():
    with pytest.raises(PlanError, match="wasn't in a form I could check"):
        parse_output("not json at all")


def test_prompt_contains_schema_links_and_no_rows(demo):
    con, cat = demo
    msgs = build_messages("total bonus", cat)
    system = msgs[0]["content"]
    assert "TABLE payroll" in system and "employees.employee_id = payroll.staff_id" in system
    assert "values: 'Engineering', 'Finance', 'HR', 'Operations', 'Sales'" in system
    assert msgs[-1]["content"] == "QUESTION: total bonus"
    assert len(msgs) == 2 + 2 * 8 and sum(len(m["content"]) for m in msgs) < 11000


def test_schema_has_no_union_typed_values_and_all_keys_required():
    """Only ModelOutput.plan may be nullable. Every object requires every key, so the model
    can't skip 'bucket' or 'values'."""
    from dataqa.plan import strict_schema
    schema = strict_schema(ModelOutput)
    plan_def = schema["$defs"]["Plan"]
    assert set(plan_def["required"]) == set(plan_def["properties"])
    assert set(schema["$defs"]["GroupBy"]["required"]) == {"table", "column", "bucket"}
    def walk(node):
        if isinstance(node, dict):
            if "anyOf" in node:
                kinds = [b.get("type") or "$ref" for b in node["anyOf"]]
                assert "null" in kinds and len(kinds) == 2, kinds
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(schema)
    assert len(json.dumps(schema)) < 7000


# --- overnight bug hunt additions (see OVERNIGHT.md) ------------------------------------

def test_strict_schema_closes_every_object():
    """Groq rejects a json_schema response_format unless every object sets
    additionalProperties: false — every call was paying a 400 and silently downgrading to
    json_object mode (schema in the prompt, not enforced). Ollama accepts the closed form too."""
    from dataqa.plan import strict_schema
    schema = strict_schema(ModelOutput)
    objects = []

    def walk(n):
        if isinstance(n, dict):
            if n.get("type") == "object" and "properties" in n:
                objects.append(n)
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)
    walk(schema)
    assert len(objects) >= 7  # ModelOutput, Plan, Filter, GroupBy, Aggregate, Sort, Having, ColumnRef
    assert all(o.get("additionalProperties") is False for o in objects)


INTERNAL_WORDS = ("template", "stub", "validat", "compil", "catalog", "fan-out", "group-by", "group_by",
                  "aggregate", "alias", "bucket", "json", "http ", "http)", "parser", "pydantic")


def _clean(text: str) -> None:
    low = text.lower()
    hits = [w for w in INTERNAL_WORDS if w in low]
    assert not hits, f"internal vocabulary {hits} in user-facing text: {text!r}"


def test_offline_refusal_uses_plain_words(demo):
    from dataqa.plan import PlanError
    with pytest.raises(PlanError) as e:
        StubProvider().plan("which department has the most leave?", demo[1])
    _clean(str(e.value))
    assert "Groq key" in str(e.value)


def test_unparseable_model_output_is_explained_plainly(demo):
    from dataqa.plan import PlanError
    with pytest.raises(PlanError) as e:
        parse_output('{"can_answer": "maybe"}', demo[1])
    _clean(str(e.value))
    assert "rephras" in str(e.value)


def test_ollama_unreachable_reads_plainly(monkeypatch, demo):
    from dataqa.providers import OllamaProvider
    import ollama
    from dataqa.hosted import ProviderUnavailable

    class Down:
        def __init__(self, host=None): pass
        def chat(self, **kw): raise ConnectionError("[WinError 10061] No connection could be made")
    monkeypatch.setattr(ollama, "Client", Down)
    with pytest.raises(ProviderUnavailable) as e:
        OllamaProvider().plan("total bonus", demo[1])
    _clean(e.value.reason)
    assert "ConnectionError" not in e.value.reason and "running" in e.value.reason

    class Missing:
        def __init__(self, host=None): pass
        def chat(self, **kw): raise ollama.ResponseError("model 'qwen3.5:4b' not found, try pulling it first", 404)
    monkeypatch.setattr(ollama, "Client", Missing)
    with pytest.raises(ProviderUnavailable) as e:
        OllamaProvider().plan("total bonus", demo[1])
    assert "ollama pull" in e.value.reason
