# AI-Powered Data Q&A Web App

Upload a few CSV or Excel files, ask a question in plain English, get a correct number, or a clear reason why not.

The model never sees your data and never writes code. It only describes *what to compute* (which tables, which filter, which total) as a small structured request. Code checks that request against the real columns, joins the files, runs the query, and shows its working under every answer: which files were linked and how many rows matched, the exact query, and what the model proposed. Every answer is auditable and every number is traceable to the query that produced it. Anything the request can't express is refused or clarified, never approximated.

Darwinbox Forward Deployed Engineer take-home.

## Try it

- **Hosted:** https://pradeep-db-question-answer-web-app.streamlit.app
- **Demo recording (~3 min):** [watch on Google Drive](https://drive.google.com/file/d/1xnE-bfrDEa2Z7nzocyXKJ4rEOttKbqon/view?usp=drive_link)
- **Run locally:**
```bash
pip install -r requirements.txt
streamlit run app.py
```

Upload the three files in `data/demo/` (employees, payroll, attendance: a small HR dataset where the employee ID column is named differently in each file, on purpose).

The demo recording uses a public four-file Kaggle HR set instead, to show it on data it wasn't built for.

**It runs with no API key and no network.** Common questions (totals, averages, counts, "by department") are answered without any model. Free-form questions need an open-source model.

The hosted link runs on my Groq key, so it works with nothing to set up. If you'd rather use your own, paste it in the sidebar — it overrides mine for your session only and is never stored. Two open-weight models are available from the dropdown: qwen3.8-27b and gpt-oss-120b, both evaluated in the results below. If the selected one is rate-limited, the other answers and the answer says so.

Running locally, you can also use **Ollama**: install [Ollama](https://ollama.com), then `ollama pull qwen3.5:4b` (3.4 GB). ~3 s per question on an 8 GB GPU after a one-off ~60 s load; it appears in the picker only when Ollama is running. Leave the provider on **Auto** and it uses whichever is available: Groq first, then Ollama, then offline.

Things to ask:

| Ask | What you'll see |
|---|---|
| `total base_salary by department` | instant answer, bar chart, and "Linked employees to payroll on employee_id ↔ staff_id · 120 of 120 employees matched" |
| `which employees have more than 75% attendance` | a percentage per employee, kept above the threshold |
| `weekly WFH count trend in Q2 2025` | a line chart |
| `total bonus by attendance status` | **refused**: that join would count each payroll row ~130 times and the total would look perfectly normal |
| `show me the 5 highest paid employees` | asks which column you mean, then labels the answer as a substitute for "total pay" |

Click **👁 How this was computed** under any answer: how the question was read, in plain steps, who answered and which files were linked; the exact query, parameters and raw model output sit under *Technical details*.

**Tech stack:** Python 3.12 · Streamlit (UI) · DuckDB (all queries; reads CSV directly, spills to disk) · pandas + openpyxl (Excel only) · pydantic (the request schema, which also constrains the model's output) · Plotly (charts) · open-source models via Ollama locally or Groq hosted. Built with Claude Code; `CLAUDE.md` is the brief I gave it. Tests: `pytest -q` (288; 8 of them load a public Kaggle HR set from a local path and skip cleanly if it isn't there). Question set: `python scripts/eval.py --providers stub,ollama,groq` (`stub` is the offline provider's name on the command line; add `--hard` for the harder set).

## How well does it work

Every test question has its expected answer computed by a separate hand-written SQL query against the same tables; the app's answer has to match it exactly. A refusal counts as correct only when refusing was the right thing to do. Each model runs alone, no fallback.

| Questions | Model | Correct | Wrong numbers | Answers the checks had to fix | Model time per question | Waiting on rate limits |
|---|---|---|---|---|---|---|
| 28: totals, trends, comparisons, filters, percentages, thresholds, 3 that should be refused | qwen3.5:4b, local, 8 GB laptop | 28 / 28 | 0 | 3 of 22 free-form | 3.6 s | none |
| same 28 | gpt-oss-120b on Groq | 27 / 28 | 0 | 1 of 22 | 3.1 s | 386 s total |
| same 28 | Qwen3.8-27B on Groq | 7 / 7 answered, 21 not reached | 0 | 0 of 7 | 1.8 s | daily quota hit mid-run |
| 44: the 28 above plus 16 written to trip it up | qwen3.5:4b | 42 / 44 | 0 | 5 of 37 | 3.3 s | none |

What "the checks had to fix" means: the model produced a *valid* request that answered a different question, and code caught it before running. On the 4B model: a ranking with no sort order; "monthly total" answered as one month (it invented a `pay_month = 2025-01` filter); arithmetic where a column name should be; "how many employees" counted as payroll rows (330 instead of 28); a threshold applied as a row filter *and* as a threshold, which biased the average. On the bigger models: one case each, and the same one, "highest paid" read as base salary only, ignoring bonus and deductions. Every fix is shown under the answer as a plain sentence.

The 120B model's one miss is a refusal: it put `department` on the payroll table, the check named the mistake, and the retry didn't fix it. The two misses on the harder set: one refusal (a threshold with no value to compare against), and one answer with the right numbers in the wrong shape (two rows where one was asked for). No answer was numerically wrong. An earlier 38-question version of that set scored 29/38 on its first run; five of the misses became code-side checks, one of them a bug in my own compiler (parameters bound in the wrong order when a filter and a conditional total both appeared). The 27B row stopped at 7 questions because Groq's daily token quota, a rolling 24-hour window, was spent on the previous night's runs; the 7 it answered were all correct. Full per-question tables, with what was fixed on each, are in `eval/`.

Two things worth knowing about those numbers. The local 4B model isn't deterministic run to run even at temperature 0: across the runs behind this table three questions changed shape between passes (a trend split per employee one time and not the next), never a number. A single pass is a sample, not a proof. The Groq rows were produced on the free tier (8k tokens/min, 200k/day, about 4k tokens a question), which is where the waiting column comes from, and a full run of both sets is close to the daily ceiling. And bigger models aren't strictly safer: they get the shape right more often, but when they narrow a question they do it quietly. The checks stay on for every model because you don't get to choose which one runs in production.

## How it works

```
 files ──► DuckDB tables ──► columns + samples ──► link detection (names · shared values · one-to-many)
                                                            best link applied and disclosed
 question ──► common shapes? ──yes──► request ─┐
              (total X by Y…)                   ├──► checks ──► SQL ──► DuckDB ──► answer · chart · working
              └──no──► model ──► request ───────┘    (columns exist, types fit, join won't inflate,
                       (open-source, output          shape matches the question) → fix, ask, or refuse
                        constrained to the schema)
```

**Link detection** compares column names *and* the values in them: `staff_id` links to `employee_id` on values alone, with a name similarity of zero. Cardinality is measured so the checks know which side of a link is unique. The answer always says which link was used and how many rows matched. "4 of 120 matched" tells a non-technical user something is wrong without knowing what a join is. If no link clears the threshold, the app says the files don't appear related and asks which columns connect them. That's the one place it asks.

**The request** is a JSON object with a fixed shape: tables, filters, group-by (with day/week/month/quarter/year buckets), aggregates (sum/avg/count/count-distinct/min/max, each optionally conditional), percentages and shares, a threshold on a measure, sort, limit. The model's output is constrained to that schema at decode time, and every field is required, so a small model can't skip the sort or the date bucket. It never names a join key; the compiler finds the path through the detected links.

**The checks** run before anything executes. Every table and column must exist and fit its type. A request that would repeat rows through a join (payroll × attendance) is refused with the reason. A ranking question with no sort, a "monthly" question with no months, "how many employees" counted as payroll rows, "A vs B" returned as one total: corrected and disclosed. A misspelt column gets one round-trip back to the model with the error, never a loop. Arithmetic between columns is offered as a labelled substitute, never silently. If the model declines but attaches a valid request, the request runs and the model's doubt is shown as a note; a model's opinion of its own limits is not trusted over the checks.

**Refusals come in two classes.** A *fixable* model mistake (unknown column, missing value, missing sort) is fed back once and the corrected request re-checked. A *capability* limit (arithmetic between columns, a join that would inflate, files that aren't linked) is refused immediately, with what can be done instead. When one detail has two or three plausible readings, the app asks once, with buttons — including when a word in the question names several columns (`average score` over Engagement, Satisfaction and Work-Life Balance Score) and the model picked one silently. A request naming a file that isn't loaded is refused from the real schema ("None of the loaded files has a salary column. The closest is Desired Salary in recruitment_data."); the invented name never reaches the user.

**Providers.** Simple questions never reach a model. Otherwise: Groq, then Ollama, then offline. Groq offers the two evaluated models, qwen3.8-27b (default) and gpt-oss-120b; its free-tier limits are per model, so when the selected one is rate-limited the other is tried first, and the answer says so ("qwen3.8-27b was rate-limited — answered by gpt-oss-120b"). Calls retry on 429/5xx with exponential backoff and jitter, honouring `Retry-After`, three attempts; a `Retry-After` in minutes fails at once; 400/401 never retry and skip the sibling model. If a provider fails, the next answers and the answer says so. Keys live in the sidebar (session only), Streamlit secrets, or the environment, in that order, and are never logged.

## Decisions worth knowing

**The model proposes; code decides.** A model that reads rows and adds them up is usually right, and you can't tell when it isn't. A model that writes SQL produces valid queries that answer the wrong question. Constraining it to a small structured request makes every mistake checkable *before* it runs, and lets a 4B model on a laptop do the job, because filling a form is easier than following instructions. Requiring every field took the test set from 15/21 to 21/21 without touching the prompt.

**Joins are found in code and shown, not asked.** A confirmation screen asks a non-technical user to approve "employee_id = staff_id", which they can't judge. A match count under the answer, they can. The first version had the confirmation screen; it was cut.

**Refuse rather than approximate.** The double-counting case above returned 877 million against a true 6.8 million and looked completely normal. It was caught by eye, not by a test, with 128 tests passing at the time. That's why every test now has an independent oracle, and why anything the request can't express is refused with a reason instead of answered approximately.

**Cut: retrieval over the schema.** With a five-file cap the column list is ~20 lines; it fits in the prompt. A vector store would solve a problem the cap already prevents. The trigger for adding it is in `NEXT.md`.

## What it won't do

- Arithmetic between columns (`base_salary + bonus`): offered as a labelled substitute, not computed. First on the list in `NEXT.md`.
- Anything beyond filter / group / total / average / count / min / max / percentage / threshold / sort / top-N is declined with a reason.
- More than 5 tables or 50 MB per file (my caps, not the brief's). Dates in text forms other than ISO, day/month/year or `16-Mar-23` / `Mar 16, 2023` styles. Composite join keys.
- The local model isn't deterministic run to run; a question can pass once and fail the next time. The checks exist for that.
- Single user, single session. No history, no auth.

## Layout

```
app.py                 Streamlit UI
dataqa/loader.py       files → DuckDB tables + column stats
dataqa/joins.py        link detection (names · values · cardinality)
dataqa/plan.py         the structured request, its checks, plain-English rendering
dataqa/engine.py       request → SQL → DuckDB, incl. the double-counting check
dataqa/templates.py    common question shapes answered with no model
dataqa/providers.py    offline / Ollama · hosted.py: Groq (two models, per-model rate-limit rotation), retry, fallback chain
dataqa/charts.py       chart choice and the reason when there isn't one
data/demo/             the three demo files (+ make_demo.py)
scripts/eval.py        the test questions and their SQL oracles → eval/*.md
tests/                 288 tests (pytest -q; 8 skip without the Kaggle set)
NEXT.md                deliberate cuts, and what would make me revisit each
```
