# Project: Data Q&A Web App

Upload CSV/Excel files, ask analytical questions in plain English, get correct answers and charts.

This is a take-home assignment for the Darwinbox **Forward Deployed Engineer** role. **Deadline: Monday 21 Sept 2026, 12:00 PM IST.** Target 4-6 focused hours of build.

---

## ⚠️ SOURCE OF TRUTH — read these first

Two files in this repo root are the actual brief:

- **`FDE_Assignment_Darwinbox.pdf`** — the task as issued. Sections 1-6.
- **`email.txt`** — the covering email from the recruiter.

**Read both before writing any code.** Everything below is my reading of them, not a replacement for them.

Where this file and those two disagree, **the PDF and email win.** Tell me about the conflict rather than silently picking one.

If something in this file isn't supported by the brief, say so. I'd rather cut an assumption than build on it.

**Submission (from email):** reply to Atharva's email with repo link, demo recording link, and the write-up. No repo visibility stated — make it public.

---

## What I'm actually being evaluated on

The brief says it directly (PDF §1):

> *"You are not being tested on your ability to write code by hand. You are being tested on how you scope an ambiguous problem, make sound decisions under constraints, and use AI tools to go from idea to a working prototype."*

And (§6):

> *"Keep this small. A smaller, well thought-through app beats a sprawling, half-working one."*

**So: restraint is the test.** Do not add features I didn't ask for. If you think of something clever, put it in `NEXT.md` instead of building it.

There's also a fourth acceptance criterion (§3.4): **"Delta solutioning on top of what AI does."**

⚠️ **This phrase is not defined in the brief** (researched — not an established term anywhere). My reading, which the panel conversation will test: an AI tool can generate a working CSV Q&A app in an hour, so the question is what *I* added on top — and, stacked on that, what the *app* adds on top of what the *model* does: verification, provenance, refusing to guess. The write-up claims both readings explicitly: *the model generates; code verifies; the delta is everything that makes the number trustworthy.*

**Role context (from the public JD, not the brief):** FDEs at Darwinbox embed with enterprise HR customers, run discovery with CHROs/HRBPs/HRIS leads to "surface the real problem behind the stated ask", and ship a prototype in ~a week. Required skills include evaluation frameworks. The generic FDE take-home differentiator question is *"How do you know your AI system is actually working?"* — the README results table is the answer to that, not a formality.

---

## Acceptance criteria — verified against PDF §3

1. Multi-file upload — multiple CSV/Excel files in one session
2. Cross-file analysis — totals, averages, filters, comparisons, trends **across** files
3. Visual insights — charts where the question calls for one
4. Delta solutioning on top of what AI does

Constraint (§4, exact wording): *"Use open-source AI models and AI coding tools to build this. You may also use AI coding tools (Claude, ChatGPT, Cursor, Copilot, etc.) to build, debug, and refine it."* The sentence separates the runtime model from the coding tools — **the model doing the analysis must be open-source.** Verified.

Deliverables (§5): working prototype (hosted link **or** local run instructions + short demo recording), Git repo with README covering setup and tech stack, 1-page write-up (approach, key decisions, what I'd build next). Nothing else required.

---

## Core design decisions — the delta

⚠️ **These are my decisions, not requirements from the brief.** The brief is deliberately ambiguous about cross-file relationships and about what "delta solutioning" means — that ambiguity is the test (§1). These four are how I'm choosing to answer it. Implement them properly; everything else is table stakes.

### 1. The model emits a validated JSON plan. Code executes it. No generated code runs, ever.

The LLM never reads data rows. It receives the schema (table names, columns, dtypes, sample values, confirmed joins) and emits a **structured JSON plan** — not pandas code, not SQL. Ollama's structured-output API (`format=<json schema>`) constrains the plan shape at decode time, so malformed output is impossible; my code then validates that every table and column the plan names actually exists.

The plan covers §3.2's list: **filter, group-by, aggregate (sum/avg/count/min/max, conditional), ratio (percentage of rows or share of a measure), having (threshold on a measure), sort/limit, join, time-bucket** (day/week/month/quarter/year). A validated plan compiles to SQL and runs on DuckDB.

**No free-code fallback.** If a question can't map to a valid plan, the app **refuses with a clear message** ("I can't express that as a supported operation — try X"). Abstention is a better answer than a plausible wrong number.

**Why:** a model reading rows and computing a total gets it mostly right, which is worse than wrong because the variance is invisible. A model emitting free pandas code needs a sandbox and produces column-name and syntax errors you retry on. A constrained plan is checkable before it runs, testable without a model, and has exactly one executor.

### 2. Join keys are detected in code, applied automatically, and disclosed — never confirmed, never silent.

Never let the model guess how files relate. Detect candidates deterministically:
- Column name match or near-match (`employee_id` ≈ `emp_id`; `staff_id` only matches on values)
- Value overlap between columns, with real counts
- Cardinality (one-to-many vs many-to-many)

**No confirmation screen.** The brief is upload → ask → answer; a non-technical user shouldn't be asked to confirm a join key — that's engineer language. The highest-confidence candidate per table pair is applied automatically and shown as a provenance line beneath the answer:

> *Linked employees to payroll on employee_id ↔ staff_id · 118 of 120 employees matched*

The line expands to override — the other candidates with their match rates, or "not related". Most people never will. **The match count is the important part:** "4 of 120 matched" tells a non-technical user something is wrong without them knowing what a join is.

Two rules:
1. **Never join silently.** Every answer that used a link shows the line. A wrong join produces a number that looks completely normal, and nobody catches it.
2. **If no candidate clears the threshold, don't guess.** Say the files don't appear related and ask which columns link them. That's the one case where asking is correct. "Related" means reachable through the link graph — payroll↔attendance is related if both link to employees.

The plan's `join` step may only use applied links.

**Why:** the delta is that the system shows its working, not that it asks permission.

### 3. Deterministic path for template questions.

Questions matching a known shape never touch the model. "Total X", "average Y", "count of Z", "X by Y" → parse → **emit the same JSON plan** → same executor. Zero tokens. Require exact (case-insensitive) column-name match; on any doubt, fall through to the LLM. Keep the parser to those shapes only — a clever NL parser that misfires is worse than none.

**Why:** cost, latency, and reliability. A groupby doesn't need a language model.

### 4. Provider abstraction with a no-model default.

Three plan providers behind one interface:
- **`stub`** (default) — deterministic canned plans for the demo question set. No model, no network. A reviewer clones and runs it immediately.
- **`ollama`** — `qwen3.5:4b` locally via structured outputs. `think=False`, `temperature=0`.
- **`hosted`** (optional) — any OpenAI-compatible endpoint serving an open-source model, via `response_format`. Env-var configured.

The template path needs no provider at all.

**Why:** the reviewer's first experience must not be "install Ollama and pull 3.4 GB." And the abstraction is what lets the results table compare providers honestly.

---

## Schema in the prompt — no retrieval layer

**Cut: ChromaDB / schema retrieval.** Under my file caps the combined schema is ~100-150 lines and fits in any model's context trivially. A vector store would be a dependency solving a problem the caps already prevent. Trigger for adding it is in `NEXT.md` (~200+ combined columns). The write-up states this as a deliberate cut, not an omission.

---

## Stack

- Python 3.12
- **DuckDB** for all query execution — reads CSV off disk without loading everything into memory, handles millions of rows, and the JSON plan maps to SQL more naturally than to pandas expressions
- **pandas** only for Excel loading (via openpyxl) and schema sampling
- Streamlit for the UI (single file, fast to build)
- **Ollama + `qwen3.5:4b`** (3.4 GB, fits in 8 GB VRAM, 256K ctx, native structured outputs). Brief requires open-source. No OpenAI/Anthropic API.
- Plotly for charts
- pydantic for the plan schema (also generates the JSON schema Ollama constrains on)

Keep dependencies minimal. Every one needs to justify itself.

**Dev machine:** RTX 5060 Laptop 8 GB VRAM. `pandas 3.0.2` is installed (copy-on-write, string dtype defaults changed) — one more reason not to run model-generated pandas.

---

## Demo data — HR-flavoured, deliberately messy keys

Three files that match Darwinbox's world: `employees`, `attendance`, `payroll`. **Name the join column differently in each** — `employee_id`, `emp_id`, `staff_id` — so join detection is visible in the demo rather than theoretical. Mirrors the real discovery problem an FDE walks into. Include at least one Excel file.

---

## Explicitly NOT building

⚠️ **My scoping calls, not brief requirements.** Each is a deliberate cut, and the write-up should say why.

Do not build these. If they come up, add to `NEXT.md`:

- Free-form code generation / execution (see Decision 1)
- Schema retrieval / ChromaDB (see above)
- MCP layer (no protocol needed for a single-user local app — even though Darwinbox ships an MCP server; that's a NEXT.md item)
- Auth, user accounts, sessions beyond the current one
- Query history / saved queries
- Composite join keys
- Schema versioning
- Multi-agent orchestration (this is a pipeline, not agents)
- Anything beyond the 4 acceptance criteria

---

## Constraints to implement and document

⚠️ **The brief sets no limits on file size, row count or file count.** These are my assumptions — state every one in the README so the panel can see they were deliberate.

- Cap file size, row count, and number of files per session. Pick sensible numbers, state them in the README.
- Handle: empty file, single column, all-null column, mismatched types on a join key, duplicate column names across files.
- Fail loudly with a clear message. Never return a silent wrong answer.
- Every answer shows the plan that produced it (and the SQL) — provenance is part of the answer.

---

## Deliverables

**Repo** — clean commit history, README covering setup and tech stack. Target 80-120 lines, not 500.

**README structure:**
```
Title + one-line description
Quickstart (3-4 commands, works with no model — stub provider)
Results table (queries tested, expected vs actual, pass/fail, path taken (template/stub/ollama), latency)
Architecture (short ASCII diagram)
Key decisions (the four above, one para each)
Limitations (stated, not hidden)
```

**Write-up** — **1 page max.** Approach, key decisions, what I'd build next. The four core decisions are the spine of this.

**Demo** — local run instructions + short screen recording (Win+G / OBS, upload to Drive). Streamlit needs a Python backend, and deployment isn't what's being evaluated.

---

## Build order

1. Upload + parse CSV/Excel → DuckDB tables. Show schema.
2. Schema extraction + join key detection; auto-apply best link; provenance line with override; ask only when unrelated.
3. Plan schema (pydantic) + validator + plan→SQL compiler + DuckDB executor. Tests.
4. Deterministic template path → plan.
5. Provider abstraction: stub → ollama → hosted. Schema prompt → structured plan.
6. Charts where the plan implies one (group-by → bar, time-bucket → line).
7. Constraints, error handling, edge cases, refusal messages.
8. README + results table + write-up + recording.

Get 1-5 working before touching charts. A working app that answers correctly beats a pretty one that doesn't.

---

## How to work with me

- Small increments. Show me the diff before moving on.
- Tests on the deterministic parts — join detection, template parsing, plan validation, plan→SQL. Those have definite right answers.
- If something's ambiguous, ask rather than assume. The ambiguity is deliberate in this brief and I want to make the call myself.
- Flag anything you think is over-engineering. I'd rather cut than defend.
- **Flag anything in this file the brief doesn't support.** The PDF and email are authoritative.

**Track decisions as we go** in `DECISIONS.md` — "chose X over Y because Z", and which ones were assumptions rather than requirements. I'll need these for the panel conversation (§1), not for the submission. `DECISIONS.md` is gitignored.
