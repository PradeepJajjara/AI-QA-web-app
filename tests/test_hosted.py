import json
from pathlib import Path

import pytest

from dataqa.hosted import GroqProvider, OpenRouterProvider, ProviderChain, ProviderUnavailable, resolve_key
from dataqa.joins import confirm, default_selection, detect_joins
from dataqa.loader import load_file, open_connection
from dataqa.plan import Catalog
from dataqa.providers import StubProvider, build_chain
from dataqa.router import answer

DEMO = Path(__file__).parent.parent / "data" / "demo"
KEY = "gsk_test_secret_key_123"
GOOD = {"can_answer": True, "reason": "", "plan": {
    "tables": ["payroll"], "filters": [], "group_by": [],
    "aggregates": [{"func": "sum", "table": "payroll", "column": "bonus", "alias": ""}],
    "select": [], "sort": {"by": "", "desc": False}, "limit": 0}}


@pytest.fixture(scope="module")
def demo():
    con = open_connection(None)
    tables = []
    for f in ["employees.csv", "payroll.csv", "attendance.xlsx"]:
        tables += load_file(con, DEMO / f, {t.name for t in tables}).tables
    return con, Catalog(tables, [confirm(c) for c in default_selection(detect_joins(con, tables))])


class Resp:
    def __init__(self, status, body=None, text="", headers=None):
        self.status_code, self._body, self.text, self.headers = status, body, text, headers or {}

    def json(self):
        return self._body


def scripted(monkeypatch, responses):
    """requests.post returns the scripted responses in order; records every call."""
    import requests
    calls = []

    def post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json, "headers": headers})
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(requests, "post", post)
    return calls


def ok_body():
    return {"choices": [{"message": {"content": json.dumps(GOOD)}}]}


def test_retries_429_500_503_then_succeeds(monkeypatch, demo):
    sleeps = []
    calls = scripted(monkeypatch, [Resp(429), Resp(503), Resp(200, ok_body())])
    p = GroqProvider(key=KEY, sleep=sleeps.append, backoff_base=1.0)
    plan = p.plan("total bonus", demo[1])
    assert plan.aggregates[0].column == "bonus" and p.last_attempts == 3 and len(calls) == 3
    assert len(sleeps) == 2 and 1.0 <= sleeps[0] <= 2.0 and 2.0 <= sleeps[1] <= 3.0  # (2^n + U(0,1)) * base


def test_gives_up_after_three_and_is_unavailable(monkeypatch, demo):
    scripted(monkeypatch, [Resp(500), Resp(500), Resp(500)])
    p = GroqProvider(key=KEY, sleep=lambda s: None)
    with pytest.raises(ProviderUnavailable, match=r"not responding \(HTTP 500\), 3 attempts"):
        p.plan("total bonus", demo[1])


@pytest.mark.parametrize("status", [400, 401])
def test_never_retries_400_or_401(monkeypatch, demo, status):
    calls = scripted(monkeypatch, [Resp(status, text="nope"), Resp(200, ok_body())])
    p = GroqProvider(key=KEY, sleep=lambda s: None)
    with pytest.raises(ProviderUnavailable, match="rejected the"):
        p.plan("total bonus", demo[1])
    assert len(calls) == 1


def test_connection_error_is_retried(monkeypatch, demo):
    import requests
    calls = scripted(monkeypatch, [requests.ConnectionError("boom"), Resp(200, ok_body())])
    p = OpenRouterProvider(key=KEY, sleep=lambda s: None)
    p.plan("total bonus", demo[1])
    assert len(calls) == 2


def test_json_schema_400_downgrades_to_json_object_once(monkeypatch, demo):
    calls = scripted(monkeypatch, [Resp(400, text='{"error": "response_format json_schema not supported"}'),
                                   Resp(200, ok_body()), Resp(200, ok_body())])
    p = GroqProvider(key=KEY, sleep=lambda s: None)
    p.plan("total bonus", demo[1])
    assert calls[0]["json"]["response_format"]["type"] == "json_schema"
    assert calls[1]["json"]["response_format"]["type"] == "json_object"
    assert "matching exactly this schema" in calls[1]["json"]["messages"][0]["content"]
    p.plan("total bonus", demo[1])  # stays in json_object mode; no second probe
    assert calls[2]["json"]["response_format"]["type"] == "json_object" and len(calls) == 3


def test_key_only_in_auth_header_never_in_body_or_errors(monkeypatch, demo):
    calls = scripted(monkeypatch, [Resp(401, text=f"invalid key {KEY}")])
    p = GroqProvider(key=KEY, sleep=lambda s: None)
    with pytest.raises(ProviderUnavailable) as e:
        p.plan("total bonus", demo[1])
    assert KEY not in str(e.value)
    assert calls[0]["headers"]["Authorization"] == f"Bearer {KEY}"
    assert KEY not in json.dumps(calls[0]["json"])


def test_no_key_is_unavailable_without_a_request(monkeypatch, demo):
    calls = scripted(monkeypatch, [])
    with pytest.raises(ProviderUnavailable, match="no API key"):
        GroqProvider(key="").plan("total bonus", demo[1])
    assert calls == []


@pytest.fixture(autouse=True)
def no_real_secrets(monkeypatch, tmp_path):
    """Never let tests touch a real secrets.toml or a real key in the environment —
    a failing assertion would print it."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    import streamlit
    monkeypatch.setattr(streamlit, "secrets", {})  # st.secrets caches the real file per process


def test_retry_after_header_is_honoured(monkeypatch, demo):
    sleeps = []
    scripted(monkeypatch, [Resp(429, headers={"Retry-After": "7"}), Resp(200, ok_body())])
    GroqProvider(key=KEY, sleep=sleeps.append).plan("total bonus", demo[1])
    assert sleeps == [7.0]


def test_exhausted_429s_are_flagged_rate_limited(monkeypatch, demo):
    scripted(monkeypatch, [Resp(429), Resp(429), Resp(429)])
    with pytest.raises(ProviderUnavailable) as e:
        GroqProvider(key=KEY, sleep=lambda s: None).plan("total bonus", demo[1])
    assert e.value.rate_limited


def test_resolve_key_order(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "from_env")
    assert resolve_key("GROQ_API_KEY", "from_sidebar") == "from_sidebar"
    assert resolve_key("GROQ_API_KEY", "") == "from_env"
    monkeypatch.delenv("GROQ_API_KEY")
    assert resolve_key("GROQ_API_KEY", None) == ""


# --- chain -------------------------------------------------------------------------

def test_chain_falls_through_and_discloses(monkeypatch, demo):
    con, cat = demo
    scripted(monkeypatch, [Resp(429), Resp(429), Resp(429)])
    groq = GroqProvider(key=KEY, sleep=lambda s: None)
    chain = ProviderChain([groq, StubProvider()])
    a = answer("total bonus in March 2025", con, cat, provider=chain)
    assert a.error is None and a.path == "model:offline"
    assert a.notes[0] == "Answered by offline after falling back from groq (rate-limited — too many requests in a row; try again in a minute)."


def test_chain_first_success_has_no_fallback_note(monkeypatch, demo):
    con, cat = demo
    scripted(monkeypatch, [Resp(200, ok_body())])
    chain = ProviderChain([GroqProvider(key=KEY), StubProvider()])
    a = answer("total bonus please", con, cat, provider=chain)
    assert a.path == "model:groq" and not any("falling back" in n for n in a.notes)


def test_model_decline_is_final_not_a_fallback(monkeypatch, demo):
    con, cat = demo
    decline = {"choices": [{"message": {"content": json.dumps({"can_answer": False, "reason": "Needs a ratio.", "plan": None})}}]}
    scripted(monkeypatch, [Resp(200, decline)])
    chain = ProviderChain([GroqProvider(key=KEY), StubProvider()])
    a = answer("what percent of employees are on leave", con, cat, provider=chain)
    assert a.path == "refused" and a.error == "Needs a ratio." and chain.last_fallbacks == []


def test_build_chain_order_and_skips(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    import streamlit
    monkeypatch.setattr(streamlit, "secrets", {})  # st.secrets caches the real file per process
    assert build_chain("auto", ollama_ok=False).describe == "offline"
    assert build_chain("auto", keys={"groq": "k"}, ollama_ok=True).describe == \
        "groq (qwen3.8-27b, then gpt-oss-120b) → ollama → offline"
    assert build_chain("auto", keys={"openrouter": "k"}, ollama_ok=False).describe == "offline"  # opt-in only
    assert build_chain("openrouter", keys={"openrouter": "k"}, ollama_ok=False).describe == "openrouter → offline"
    assert build_chain("ollama", keys={"groq": "k"}, ollama_ok=True).describe == \
        "ollama → groq (qwen3.8-27b, then gpt-oss-120b) → offline"
    assert build_chain("groq", ollama_ok=False).describe.startswith("groq (qwen3.8-27b")  # explicit pick shows even without a key
    assert build_chain("groq", keys={"groq": "k"}, ollama_ok=False, groq_model="openai/gpt-oss-120b").describe == \
        "groq (gpt-oss-120b, then qwen3.8-27b) → offline"


# --- overnight bug hunt additions (see OVERNIGHT.md) ------------------------------------

GROQ_TPD = {"error": {"message": "Rate limit reached for model `qwen/qwen3.8-27b` in organization `org_x` service tier "
            "`on_demand` on tokens per day (TPD): Limit 200000, Used 198738, Requested 3171. Please try again in "
            "13m44.688s. Need more tokens? Upgrade to Dev Tier today at https://console.groq.com/settings/billing",
            "type": "tokens", "code": "rate_limit_exceeded"}}


def test_long_retry_after_fails_fast_with_the_servers_timing(monkeypatch, demo):
    """Groq's daily quota ran out: Retry-After 825, 'try again in 13m44s'. The provider capped the
    wait at 60s, slept twice for nothing and reported 'HTTP 429 after 3 attempts' — two dead
    minutes before the chain fell back, and no hint of when Groq would work again."""
    sleeps = []
    calls = scripted(monkeypatch, [Resp(429, body=GROQ_TPD, text=json.dumps(GROQ_TPD), headers={"Retry-After": "825"})])
    with pytest.raises(ProviderUnavailable) as e:
        GroqProvider(key=KEY, sleep=sleeps.append).plan("total bonus", demo[1])
    assert sleeps == [] and len(calls) == 1 and e.value.rate_limited
    assert "13m44" in e.value.reason or "14 min" in e.value.reason
    assert "HTTP 429" not in e.value.reason and "attempts" not in e.value.reason
    assert "org_x" not in e.value.reason and "billing" not in e.value.reason  # not the raw server text


def test_short_retry_after_still_waits_and_retries(monkeypatch, demo):
    sleeps = []
    scripted(monkeypatch, [Resp(429, headers={"Retry-After": "12"}), Resp(200, ok_body())])
    GroqProvider(key=KEY, sleep=sleeps.append).plan("total bonus", demo[1])
    assert sleeps == [12.0]


def test_rate_limit_reason_reads_as_a_sentence(monkeypatch, demo):
    scripted(monkeypatch, [Resp(429), Resp(429), Resp(429)])
    with pytest.raises(ProviderUnavailable) as e:
        GroqProvider(key=KEY, sleep=lambda s: None).plan("total bonus", demo[1])
    assert e.value.reason.startswith("rate-limited")
    assert "HTTP" not in e.value.reason


@pytest.mark.parametrize("status,expect", [(401, "rejected the API key"), (400, "rejected the request"), (404, "not found")])
def test_never_retried_statuses_read_plainly(monkeypatch, demo, status, expect):
    scripted(monkeypatch, [Resp(status, text="nope")])
    with pytest.raises(ProviderUnavailable) as e:
        GroqProvider(key=KEY, sleep=lambda s: None).plan("total bonus", demo[1])
    assert expect in e.value.reason and f"HTTP {status}" in e.value.reason


def test_server_errors_and_connection_errors_read_plainly(monkeypatch, demo):
    import requests
    scripted(monkeypatch, [Resp(503), Resp(503), Resp(503)])
    with pytest.raises(ProviderUnavailable) as e:
        GroqProvider(key=KEY, sleep=lambda s: None).plan("total bonus", demo[1])
    assert e.value.reason.startswith("not responding") and "HTTP 503" in e.value.reason
    scripted(monkeypatch, [requests.ConnectionError("x")] * 3)
    with pytest.raises(ProviderUnavailable) as e:
        GroqProvider(key=KEY, sleep=lambda s: None).plan("total bonus", demo[1])
    assert e.value.reason.startswith("couldn't connect") and "ConnectionError" not in e.value.reason


# --- one Groq model out of quota: the next Groq model, then Ollama / offline --------------

def _groq_chain(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("DATAQA_GROQ_MODEL", raising=False)
    import streamlit
    monkeypatch.setattr(streamlit, "secrets", {})
    chain = build_chain("groq", keys={"groq": KEY}, ollama_ok=False)
    for p in chain.providers:
        if hasattr(p, "_sleep"):
            p._sleep = lambda s: None
    return chain


def test_rate_limited_groq_model_hands_to_the_next_groq_model(monkeypatch, demo):
    """Daily quota on qwen3.8-27b (long Retry-After): gpt-oss-120b answers, and the note says which
    model was out and which replied. Offline never enters into it."""
    con, cat = demo
    calls = scripted(monkeypatch, [Resp(429, body=GROQ_TPD, text=json.dumps(GROQ_TPD), headers={"Retry-After": "825"}),
                                   Resp(200, ok_body())])
    chain = _groq_chain(monkeypatch)
    a = answer("total bonus please", con, cat, provider=chain)
    assert a.error is None and a.path == "model:groq"
    assert [c["json"]["model"] for c in calls] == ["qwen/qwen3.8-27b", "openai/gpt-oss-120b"]
    assert a.notes[0] == "qwen3.8-27b was rate-limited — answered by gpt-oss-120b."
    assert chain.answered_by.model == "openai/gpt-oss-120b"


def test_selected_model_first_then_the_other(monkeypatch, demo):
    """With gpt-oss-120b selected, it goes first and qwen3.8-27b is the one tried on its rate limit."""
    con, cat = demo
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    import streamlit
    monkeypatch.setattr(streamlit, "secrets", {})
    calls = scripted(monkeypatch, [Resp(429, body=GROQ_TPD, text=json.dumps(GROQ_TPD), headers={"Retry-After": "825"}),
                                   Resp(200, ok_body())])
    chain = build_chain("groq", keys={"groq": KEY}, ollama_ok=False, groq_model="openai/gpt-oss-120b")
    for p in chain.providers:
        if hasattr(p, "_sleep"):
            p._sleep = lambda s: None
    a = answer("total bonus please", con, cat, provider=chain)
    assert [c["json"]["model"] for c in calls] == ["openai/gpt-oss-120b", "qwen/qwen3.8-27b"]
    assert a.notes[0] == "gpt-oss-120b was rate-limited — answered by qwen3.8-27b."


def test_both_groq_models_out_falls_to_offline_and_names_each(monkeypatch, demo):
    """Offline only when both Groq models are exhausted — and the note says both were."""
    con, cat = demo
    tpd = Resp(429, body=GROQ_TPD, text=json.dumps(GROQ_TPD), headers={"Retry-After": "825"})
    calls = scripted(monkeypatch, [tpd, tpd])
    a = answer("total bonus in March 2025", con, cat, provider=_groq_chain(monkeypatch))
    assert a.error is None and a.path == "model:offline" and len(calls) == 2
    assert a.notes[0].startswith("Answered by offline after falling back from groq qwen3.8-27b (rate-limited")
    assert "groq gpt-oss-120b (rate-limited" in a.notes[0]


def test_a_bad_key_does_not_try_the_other_groq_models(monkeypatch, demo):
    """A 401 is about the key, not the model: one call, straight to offline. The three-model chain
    must not turn one rejected key into three."""
    con, cat = demo
    calls = scripted(monkeypatch, [Resp(401, text="nope")])
    a = answer("total bonus in March 2025", con, cat, provider=_groq_chain(monkeypatch))
    assert a.path == "model:offline" and len(calls) == 1
    assert a.notes[0] == "Answered by offline after falling back from groq (rejected the API key (HTTP 401))."


def test_short_rate_limit_still_moves_to_the_next_model(monkeypatch, demo):
    """Three quick 429s (per-minute limit) exhaust the first model's attempts; the next model
    gets the question rather than waiting another minute."""
    con, cat = demo
    calls = scripted(monkeypatch, [Resp(429), Resp(429), Resp(429), Resp(200, ok_body())])
    a = answer("total bonus please", con, cat, provider=_groq_chain(monkeypatch))
    assert a.path == "model:groq" and len(calls) == 4
    assert a.notes[0] == "qwen3.8-27b was rate-limited — answered by gpt-oss-120b."


# --- Groq fails, offline can't answer either: the refusal must say why, not crash ---------

def test_bad_key_then_offline_refusal_names_the_key_problem(monkeypatch, demo):
    """Live crash 21 Sep: the fallback records gained a model field and router.py still unpacked
    two values, so a bad key plus an offline refusal raised ValueError instead of a message."""
    con, cat = demo
    calls = scripted(monkeypatch, [Resp(401, text="nope")])
    a = answer("which grade has the most WFH days", con, cat, provider=_groq_chain(monkeypatch))  # not in the offline set
    assert a.path == "refused" and len(calls) == 1
    assert a.error.startswith("Groq — rejected the API key (HTTP 401). Fell back to offline. ")
    assert "Without a model" in a.error


def test_both_models_rate_limited_then_offline_refusal_names_both(monkeypatch, demo):
    con, cat = demo
    tpd = Resp(429, body=GROQ_TPD, text=json.dumps(GROQ_TPD), headers={"Retry-After": "825"})
    scripted(monkeypatch, [tpd, tpd])
    a = answer("which grade has the most WFH days", con, cat, provider=_groq_chain(monkeypatch))
    assert a.path == "refused"
    assert a.error.startswith("Groq qwen3.8-27b — rate-limited")
    assert "; Groq gpt-oss-120b — rate-limited" in a.error and ". Fell back to offline. " in a.error


def test_blank_sidebar_value_falls_back_to_the_secret(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_secret_key_123")
    import streamlit
    monkeypatch.setattr(streamlit, "secrets", {})
    assert resolve_key("GROQ_API_KEY", "") == "gsk_test_secret_key_123"
    assert resolve_key("GROQ_API_KEY", "   ") == "gsk_test_secret_key_123"
    assert resolve_key("GROQ_API_KEY", " gsk_mine ") == "gsk_mine"
