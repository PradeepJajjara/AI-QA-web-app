"""OpenAI-compatible hosted providers (Groq, OpenRouter, generic) with retry, fallback and key handling.

Open-weight models only — Llama or Qwen — to stay inside the brief's "use open-source AI models".

Retry policy:  429 / 500 / 502 / 503 / 504 and connection errors -> (2^attempt + jitter) * base,
               or the server's Retry-After when it sends one. 3 attempts max.
               400 / 401 / 403 / 404 are never retried.
Fallback:      after the attempts are exhausted (or with no key) the provider raises
               ProviderUnavailable and the ProviderChain moves to the next provider.
Keys:          sidebar field (session only) -> st.secrets -> environment variable. Never logged,
               never stored, never included in any error message or provenance.
"""

from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass, field

from dataqa.plan import Catalog, Plan, PlanError

RETRY_STATUSES = {429, 500, 502, 503, 504}
NEVER_RETRY = {400, 401, 403, 404}
MAX_ATTEMPTS = 3
MAX_WAIT = 60.0  # seconds we're willing to sleep on one Retry-After; longer means "not now"


def _retry_after_seconds(value: str | None) -> float | None:
    """Retry-After in seconds, or None if absent / HTTP-date form."""
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _human(seconds: float) -> str:
    m = int(round(seconds / 60))
    return f"about {m} minute{'s' if m != 1 else ''}" if m >= 1 else f"{int(seconds)} seconds"

# Open-weight defaults. Overridable with DATAQA_GROQ_MODEL / DATAQA_OPENROUTER_MODEL.
# Groq: Llama 3.3 70B is enterprise-only there; Qwen3.8-27B (preview) and openai/gpt-oss-120b
# (Apache-2.0 open-weight) are on the developer plan.
GROQ_MODEL = "qwen/qwen3.8-27b"
# Groq's free-tier limits are per model, so the second model usually has quota when the first is out.
# Only the two models the results table was produced with: we ship what we tested.
GROQ_MODELS = ["qwen/qwen3.8-27b", "openai/gpt-oss-120b"]


def model_short(model: str) -> str:
    """'openai/gpt-oss-120b' -> 'gpt-oss-120b': the vendor prefix is routing, not identity."""
    return model.rsplit("/", 1)[-1] if model else ""
OPENROUTER_MODEL = "meta-llama/llama-3.3-70b-instruct"


class ProviderUnavailable(PlanError):
    """This provider can't answer right now (no key, quota, outage). Try the next one."""

    def __init__(self, provider: str, reason: str, rate_limited: bool = False):
        super().__init__(f"{provider}: {reason}", kind="unavailable")
        self.provider, self.reason, self.rate_limited = provider, reason, rate_limited


def resolve_key(env_var: str, sidebar_value: str | None = None) -> str:
    """sidebar -> st.secrets / .streamlit/secrets.toml -> env. Returns '' if none. Never logged."""
    if sidebar_value and sidebar_value.strip():  # an empty or whitespace field means "use the app's key"
        return sidebar_value.strip()
    try:  # Streamlit Community Cloud puts secrets here; absent outside Streamlit
        import streamlit as st

        v = st.secrets.get(env_var)  # type: ignore[union-attr]
        if v:
            return str(v).strip()
    except Exception:
        pass
    try:  # scripts (eval) outside Streamlit: read the same file directly
        import tomllib

        for path in (".streamlit/secrets.toml", os.path.expanduser("~/.streamlit/secrets.toml")):
            if os.path.exists(path):
                with open(path, "rb") as f:
                    v = tomllib.load(f).get(env_var)
                if v:
                    return str(v).strip()
    except Exception:
        pass
    return os.environ.get(env_var, "").strip()


def _redact(text: str, key: str) -> str:
    return text.replace(key, "•••") if key else text


class OpenAICompatProvider:
    """Chat-completions with response_format. Subclasses set name / base_url / defaults."""

    name = "hosted"
    base_url = ""
    key_env = "DATAQA_HOSTED_KEY"
    default_model = ""
    extra_headers: dict = {}

    def __init__(self, model: str | None = None, key: str | None = None, base_url: str | None = None,
                 sleep=time.sleep, backoff_base: float = 1.0):
        self.model = model or os.environ.get(f"DATAQA_{self.name.upper()}_MODEL", self.default_model)
        self.base_url = (base_url or self.base_url or os.environ.get("DATAQA_HOSTED_URL", "")).rstrip("/")
        self._key = key if key is not None else resolve_key(self.key_env)
        self._sleep, self._backoff_base = sleep, backoff_base
        self._json_object_mode = False  # set after a 400 on json_schema; stays for the session
        self.last_ms = 0.0        # wall time of the whole call, retries included
        self.last_wait_ms = 0.0   # of which: time spent sleeping between retries
        self.last_raw = ""
        self.last_attempts = 0

    # -- public -----------------------------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(self._key and self.model and self.base_url)

    def plan(self, question: str, cat: Catalog) -> Plan:
        from dataqa.providers import build_messages

        self._cat = cat
        return self._call(build_messages(question, cat))

    def retry(self, question: str, cat: Catalog, failed: Plan, error: str) -> Plan:
        from dataqa.providers import retry_messages

        self._cat = cat
        return self._call(retry_messages(question, cat, failed, error))

    # -- request ------------------------------------------------------------------
    def _body(self, messages: list[dict]) -> dict:
        from dataqa.plan import strict_schema
        from dataqa.providers import ModelOutput

        body = {"model": self.model, "messages": messages, "temperature": 0}
        if self._json_object_mode:
            schema = json.dumps(strict_schema(ModelOutput))
            messages = [dict(m) for m in messages]
            messages[0]["content"] += f"\n\nRespond with JSON only, matching exactly this schema:\n{schema}"
            body["messages"] = messages
            body["response_format"] = {"type": "json_object"}
        else:
            body["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "plan", "strict": True, "schema": strict_schema(ModelOutput)}}
        return body

    def _call(self, messages: list[dict]) -> Plan:
        import requests

        if not self.configured:
            missing = "API key" if not self._key else "model" if not self.model else "URL"
            raise ProviderUnavailable(self.name, f"no {missing} configured")
        headers = {"Authorization": f"Bearer {self._key}", **self.extra_headers}
        url = f"{self.base_url}/chat/completions"
        t0 = time.perf_counter()
        last_reason, last_status = "", 0
        self.last_attempts, self.last_wait_ms = 0, 0.0
        for attempt in range(MAX_ATTEMPTS):
            self.last_attempts = attempt + 1
            try:
                r = requests.post(url, json=self._body(messages), headers=headers, timeout=60)
            except requests.RequestException as e:  # DNS, refused, timeout
                last_reason, last_status = "couldn't connect", 0
                self._backoff(attempt)
                continue
            if r.status_code == 200:
                self.last_ms = (time.perf_counter() - t0) * 1000
                self.last_raw = r.json()["choices"][0]["message"]["content"] or ""
                from dataqa.providers import parse_output

                return parse_output(self.last_raw, getattr(self, "_cat", None))
            text = _redact(r.text[:300], self._key)
            if r.status_code == 400 and not self._json_object_mode and "response_format" in text.lower() \
                    or r.status_code == 400 and "json_schema" in text.lower() and not self._json_object_mode:
                # This endpoint doesn't enforce json_schema; ask for json_object with the schema in the prompt.
                self._json_object_mode = True
                continue  # a different request, not a retry of the same one
            if r.status_code in NEVER_RETRY:
                what = {401: "rejected the API key", 403: "rejected the API key", 404: "model or endpoint not found",
                        400: "rejected the request"}[r.status_code]
                raise ProviderUnavailable(self.name, f"{what} (HTTP {r.status_code})")
            if r.status_code == 429:
                wait = _retry_after_seconds(r.headers.get("Retry-After"))
                if wait is not None and wait > MAX_WAIT:
                    # The server says minutes, not seconds (a daily quota). Waiting 60s twice
                    # can't help; fail now so the next provider answers, and say when to retry.
                    raise ProviderUnavailable(self.name, f"rate-limited — try again in {_human(wait)}", rate_limited=True)
                last_reason, last_status = "rate-limited", 429
                self._backoff(attempt, r.headers.get("Retry-After"))
                continue
            if r.status_code in RETRY_STATUSES:
                last_reason, last_status = f"not responding (HTTP {r.status_code})", r.status_code
                self._backoff(attempt, r.headers.get("Retry-After"))
                continue
            raise ProviderUnavailable(self.name, f"unexpected reply (HTTP {r.status_code})")
        if last_status == 429:
            raise ProviderUnavailable(self.name, "rate-limited — too many requests in a row; try again in a minute", rate_limited=True)
        raise ProviderUnavailable(self.name, f"{last_reason}, {MAX_ATTEMPTS} attempts")

    def _backoff(self, attempt: int, retry_after: str | None = None) -> None:
        """Honour the server's Retry-After (seconds) when present; otherwise (2^attempt + jitter) * base.
        Jitter matters: without it, concurrent clients retry in lockstep."""
        if attempt >= MAX_ATTEMPTS - 1:
            return
        delay = _retry_after_seconds(retry_after)
        if delay is not None:
            delay = min(delay, MAX_WAIT)
        if delay is None:
            delay = (2 ** attempt + random.uniform(0, 1)) * self._backoff_base
        self.last_wait_ms += delay * 1000
        self._sleep(delay)


class GroqProvider(OpenAICompatProvider):
    name = "groq"
    base_url = "https://api.groq.com/openai/v1"
    key_env = "GROQ_API_KEY"
    default_model = GROQ_MODEL


class OpenRouterProvider(OpenAICompatProvider):
    name = "openrouter"
    base_url = "https://openrouter.ai/api/v1"
    key_env = "OPENROUTER_API_KEY"
    default_model = OPENROUTER_MODEL
    extra_headers = {"HTTP-Referer": "https://github.com/dataqa", "X-Title": "Data Q&A"}


class HostedProvider(OpenAICompatProvider):
    """Any other OpenAI-compatible endpoint: DATAQA_HOSTED_URL / _MODEL / _KEY."""

    name = "hosted"
    key_env = "DATAQA_HOSTED_KEY"

    def __init__(self, **kw):
        super().__init__(**kw)
        self.model = kw.get("model") or os.environ.get("DATAQA_HOSTED_MODEL", "")


# --- chain ---------------------------------------------------------------------------

@dataclass
class ProviderChain:
    """Try providers in order; the first that answers wins. Records who answered and why
    others were skipped, so the answer can say so."""

    providers: list
    last_provider: str = ""
    last_fallbacks: list[tuple[str, str]] = field(default_factory=list)
    last_ms: float = 0.0
    last_raw: str = ""
    _answered_by: object = None

    @property
    def name(self) -> str:
        return self.last_provider or (self.providers[0].name if self.providers else "none")

    @property
    def answered_by(self):
        """The provider object whose plan ran — with several Groq models in the chain, the name
        alone doesn't say which."""
        return self._answered_by

    @property
    def describe(self) -> str:
        """'groq (qwen3.8-27b, then gpt-oss-120b) → ollama → offline'."""
        from dataqa.providers import short

        out, i = [], 0
        while i < len(self.providers):
            p = self.providers[i]
            run = [p]
            while i + len(run) < len(self.providers) and self.providers[i + len(run)].name == p.name:
                run.append(self.providers[i + len(run)])
            if len(run) > 1:
                out.append(f"{short(p.name)} ({', then '.join(model_short(getattr(q, 'model', '')) for q in run)})")
            else:
                out.append(short(p.name))
            i += len(run)
        return " → ".join(out)

    def plan(self, question: str, cat: Catalog) -> Plan:
        return self._run(lambda p: p.plan(question, cat))

    def retry(self, question: str, cat: Catalog, failed: Plan, error: str) -> Plan:
        p = self._answered_by
        if p is None or not hasattr(p, "retry"):
            raise PlanError(error)
        out = p.retry(question, cat, failed, error)
        self.last_ms, self.last_raw = getattr(p, "last_ms", 0.0), getattr(p, "last_raw", "")
        return out

    def _run(self, fn) -> Plan:
        self.last_fallbacks = []
        skip_name = ""  # a provider that failed for a reason its sibling models share (bad key, down)
        for p in self.providers:
            if p.name == skip_name:
                continue
            try:
                out = fn(p)
            except ProviderUnavailable as e:
                self.last_fallbacks.append((p.name, e.reason, getattr(p, "model", "") if e.rate_limited else ""))
                if not e.rate_limited:
                    skip_name = p.name  # only a rate limit is per model; anything else would fail the same way
                continue
            self.last_provider, self._answered_by = p.name, p
            self.last_ms, self.last_raw = getattr(p, "last_ms", 0.0), getattr(p, "last_raw", "")
            return out
        tried = "; ".join(f"{self._label(n, m)}: {r}" for n, r, m in self.last_fallbacks)  # m is "" unless the limit was per model
        raise PlanError(f"No model could answer — {tried}.", kind="capability")

    def _label(self, name: str, model: str) -> str:
        """'groq' when one Groq model is in the chain; 'groq gpt-oss-120b' when several are."""
        from dataqa.providers import short

        siblings = sum(1 for p in self.providers if p.name == name)
        return f"{short(name)} {model_short(model)}" if siblings > 1 and model else short(name)

    @property
    def fallback_note(self) -> str:
        if not self.last_fallbacks:
            return ""
        from dataqa.providers import short

        winner = self._answered_by
        same = all(n == self.last_provider for n, _, _ in self.last_fallbacks)
        if same and winner is not None and getattr(winner, "model", ""):
            # Same provider, other model: the story is the models, not the provider.
            models = [model_short(m) for _, _, m in self.last_fallbacks]
            were = "was" if len(models) == 1 else "were"
            listed = models[0] if len(models) == 1 else ", ".join(models[:-1]) + f" and {models[-1]}"
            return f"{listed} {were} rate-limited — answered by {model_short(winner.model)}."
        skipped = ", ".join(f"{self._label(n, m)} ({r})" for n, r, m in self.last_fallbacks)
        return f"Answered by {short(self.last_provider)} after falling back from {skipped}."


def ollama_reachable(host: str | None = None, timeout: float = 1.0) -> bool:
    import requests

    base = (host or os.environ.get("OLLAMA_HOST") or "http://localhost:11434").rstrip("/")
    try:
        return requests.get(f"{base}/api/tags", timeout=timeout).status_code == 200
    except Exception:
        return False
