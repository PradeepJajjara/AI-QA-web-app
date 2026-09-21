"""Run the question set against the demo data and write eval/results.md.

Every expected value is computed by an independent SQL query against the same DuckDB
tables — never typed in by hand, never taken from the app. A refusal is an expected
outcome for questions the plan can't express; a wrong number is the only real failure.

    python scripts/eval.py                                   # template + stub (no model)
    python scripts/eval.py --providers stub,ollama,groq,openrouter
    # Each provider runs alone (no fallback chain) so every result is attributable.
    # Writes eval/results.md (per provider) and eval/comparison.md (side by side).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dataqa.joins import confirm, default_selection, detect_joins  # noqa: E402
from dataqa.loader import load_file, open_connection  # noqa: E402
from dataqa.plan import Catalog  # noqa: E402
from dataqa.providers import make_provider  # noqa: E402
from dataqa.router import Answer, answer  # noqa: E402

DEMO = Path(__file__).resolve().parent.parent / "data" / "demo"
OUT = Path(__file__).resolve().parent.parent / "eval" / "results.md"
CMP = Path(__file__).resolve().parent.parent / "eval" / "comparison.md"


# (question, expected) — expected is one of:
#   ("scalar", sql)          answer is a single value equal to sql's single value
#   ("rows", sql)            answer rows equal sql's rows (order-insensitive on the first column)
#   ("count_rows", n)        answer has n rows
#   ("headline", sql)        answer is shown as a count (rows over a threshold) equal to sql's value
#   ("rows_values", sql)     the measure column only, order-insensitive (top-N with ties; extra name columns)
#   ("share", (label, sql))  a percentage: a scalar, or a per-group table whose share_pct for label matches
#   ("refuse", substring)    a refusal whose message contains substring
#   ("ask", n)               the app asks a one-shot clarifying question with n options
QUESTIONS = [
    # --- template path (no model) ---
    ("how many employees", ("scalar", "SELECT count(*) FROM employees")),
    ("total base_salary", ("scalar", "SELECT sum(base_salary) FROM payroll")),
    ("average hours", ("scalar", "SELECT avg(hours) FROM attendance")),
    ("average base_salary by department",
     ("rows", "SELECT e.department, avg(p.base_salary) FROM employees e JOIN payroll p ON e.employee_id=p.staff_id GROUP BY 1")),
    ("count of employees by location", ("rows", "SELECT location, count(*) FROM employees GROUP BY 1")),
    ("unique department", ("scalar", "SELECT count(DISTINCT department) FROM employees")),
    # --- model / stub path ---
    ("total bonus in March 2025", ("scalar", "SELECT sum(bonus) FROM payroll WHERE pay_month >= '2025-03-01' AND pay_month < '2025-04-01'")),
    ("how many employees are in Engineering", ("scalar", "SELECT count(*) FROM employees WHERE department='Engineering'")),
    ("average hours worked per month",
     ("rows", "SELECT date_trunc('month', CAST(date AS DATE)), avg(hours) FROM attendance GROUP BY 1")),
    ("monthly total base salary", ("rows", "SELECT pay_month, sum(base_salary) FROM payroll GROUP BY 1")),
    ("which department has the highest average base salary",
     ("rows", "SELECT e.department, avg(p.base_salary) a FROM employees e JOIN payroll p ON e.employee_id=p.staff_id GROUP BY 1 ORDER BY a DESC LIMIT 1")),
    ("leave days by department in Q1 2025",
     ("rows", "SELECT e.department, count(*) FROM attendance a JOIN employees e ON a.emp_id=e.employee_id "
              "WHERE a.status='Leave' AND a.date >= '2025-01-01' AND a.date < '2025-04-01' GROUP BY 1")),
    ("compare average bonus between Sales and Engineering",
     ("rows", "SELECT e.department, avg(p.bonus) FROM employees e JOIN payroll p ON e.employee_id=p.staff_id "
              "WHERE e.department IN ('Sales','Engineering') GROUP BY 1")),
    ("list employees in Hyderabad", ("count_rows", "SELECT count(*) FROM employees WHERE location='Hyderabad'")),
    # --- model only (stub doesn't know these) ---
    ("how many employees joined in 2023", ("scalar", "SELECT count(*) FROM employees WHERE join_date >= '2023-01-01' AND join_date < '2024-01-01'")),
    ("which location has the most employees", ("rows", "SELECT location, count(*) c FROM employees GROUP BY 1 ORDER BY c DESC LIMIT 1")),
    ("weekly WFH count trend in Q2 2025",
     ("rows", "SELECT date_trunc('week', CAST(date AS DATE)), count(*) FROM attendance WHERE status='WFH' "
              "AND date >= '2025-04-01' AND date < '2025-07-01' GROUP BY 1")),
    ("total deductions for engineering vs sales",
     ("rows", "SELECT e.department, sum(p.deductions) FROM employees e JOIN payroll p ON e.employee_id=p.staff_id "
              "WHERE e.department IN ('Sales','Engineering') GROUP BY 1")),
    # --- expected refusals: abstain rather than approximate ---
    ("which employees have more than 75% attendance",
     ("count_rows", "SELECT count(*) FROM (SELECT emp_id, avg(CASE WHEN status='Present' THEN 100.0 ELSE 0 END) p "
                    "FROM attendance GROUP BY 1) WHERE p > 75")),
    ("what percentage of attendance records are on leave, by department",
     ("rows", "SELECT e.department, avg(CASE WHEN a.status='Leave' THEN 100.0 ELSE 0 END) FROM attendance a "
              "JOIN employees e ON a.emp_id=e.employee_id GROUP BY 1")),
    ("total bonus by attendance status", ("refuse", "")),  # fan-out
    ("show me the 5 highest paid employees", ("ask_or_disclose", 3)),  # arithmetic -> substitute ask; or one column -> disclosed
    # --- ratios: percentage of rows, rate per group, share of a measure, threshold on a rate ---
    ("what percentage of employees are in Sales",
     ("scalar", "SELECT avg(CASE WHEN department='Sales' THEN 100.0 ELSE 0 END) FROM employees")),
    ("attendance rate by department",
     ("rows", "SELECT e.department, avg(CASE WHEN a.status='Present' THEN 100.0 ELSE 0 END) FROM attendance a "
              "JOIN employees e ON a.emp_id=e.employee_id GROUP BY 1")),
    ("WFH rate per employee, top 5",
     ("rows_values", "SELECT emp_id, avg(CASE WHEN status='WFH' THEN 100.0 ELSE 0 END) r FROM attendance GROUP BY 1 ORDER BY r DESC, emp_id LIMIT 5")),
    ("what share of total bonus went to Engineering",
     ("share", ("Engineering", "SELECT 100.0 * sum(CASE WHEN e.department='Engineering' THEN p.bonus END) / sum(p.bonus) "
                               "FROM payroll p JOIN employees e ON e.employee_id=p.staff_id"))),
    ("how many employees have more than 75% attendance",
     ("headline", "SELECT count(*) FROM (SELECT emp_id, avg(CASE WHEN status='Present' THEN 100.0 ELSE 0 END) p "
                  "FROM attendance GROUP BY 1) WHERE p > 75")),
    ("percentage of leave days by department",
     ("rows", "SELECT e.department, avg(CASE WHEN a.status='Leave' THEN 100.0 ELSE 0 END) FROM attendance a "
              "JOIN employees e ON a.emp_id=e.employee_id GROUP BY 1")),
]


# Adversarial set: phrased to tempt the mistakes we've seen — a threshold applied as a row
# filter, counting rows when the question says "employees", vague words, dates in odd forms.
# Same rule: every expectation is independent SQL. Run with --hard.
HARD_QUESTIONS = [
    ("which locations have an average base salary above 150000",
     ("rows", "SELECT e.location, avg(p.base_salary) FROM employees e JOIN payroll p ON e.employee_id=p.staff_id "
              "GROUP BY 1 HAVING avg(p.base_salary) > 150000")),
    ("departments where total bonus exceeds 1.5 million",
     ("rows", "SELECT e.department, sum(p.bonus) FROM employees e JOIN payroll p ON e.employee_id=p.staff_id "
              "GROUP BY 1 HAVING sum(p.bonus) > 1500000")),
    ("how many employees earn more than 200000 base salary",
     ("scalar", "SELECT count(DISTINCT staff_id) FROM payroll WHERE base_salary > 200000")),
    ("average hours on WFH days by department",
     ("rows", "SELECT e.department, avg(a.hours) FROM attendance a JOIN employees e ON a.emp_id=e.employee_id "
              "WHERE a.status='WFH' GROUP BY 1")),
    ("bonus paid in Q2 2025 by department",
     ("rows", "SELECT e.department, sum(p.bonus) FROM employees e JOIN payroll p ON e.employee_id=p.staff_id "
              "WHERE p.pay_month >= '2025-04-01' AND p.pay_month < '2025-07-01' GROUP BY 1")),
    ("which grade has the most WFH days",
     ("rows", "SELECT e.grade, count(*) c FROM attendance a JOIN employees e ON a.emp_id=e.employee_id "
              "WHERE a.status='WFH' GROUP BY 1 ORDER BY c DESC LIMIT 1")),
    ("average base salary of employees who joined in 2024",
     ("scalar", "SELECT avg(p.base_salary) FROM employees e JOIN payroll p ON e.employee_id=p.staff_id "
                "WHERE e.join_date >= '2024-01-01' AND e.join_date < '2025-01-01'")),
    ("monthly leave count for engineering",
     ("rows", "SELECT date_trunc('month', CAST(a.date AS DATE)), count(*) FROM attendance a JOIN employees e ON a.emp_id=e.employee_id "
              "WHERE a.status='Leave' AND e.department='Engineering' GROUP BY 1")),
    ("percentage of attendance records that are leave in Sales",
     ("scalar", "SELECT avg(CASE WHEN a.status='Leave' THEN 100.0 ELSE 0 END) FROM attendance a JOIN employees e ON a.emp_id=e.employee_id "
                "WHERE e.department='Sales'")),
    ("top 3 locations by total deductions",
     ("rows", "SELECT e.location, sum(p.deductions) s FROM employees e JOIN payroll p ON e.employee_id=p.staff_id "
              "GROUP BY 1 ORDER BY s DESC LIMIT 3")),
    ("employees with attendance below 80%",
     ("count_rows", "SELECT count(*) FROM (SELECT emp_id, avg(CASE WHEN status='Present' THEN 100.0 ELSE 0 END) p "
                    "FROM attendance GROUP BY 1) WHERE p < 80")),
    ("total base salary in March vs April 2025",
     ("rows", "SELECT pay_month, sum(base_salary) FROM payroll WHERE pay_month IN ('2025-03-01','2025-04-01') GROUP BY 1")),
    ("how many distinct managers", ("scalar", "SELECT count(DISTINCT manager_id) FROM employees")),
    ("lowest average hours by location",
     ("rows", "SELECT e.location, avg(a.hours) h FROM attendance a JOIN employees e ON a.emp_id=e.employee_id "
              "GROUP BY 1 ORDER BY h ASC LIMIT 1")),
    ("count of employees per manager, top 5",
     ("rows", "SELECT manager_id, count(*) c FROM employees WHERE manager_id IS NOT NULL GROUP BY 1 ORDER BY c DESC LIMIT 5")),
    ("average tenure by department", ("refuse", "")),  # needs date arithmetic; a silent substitute here is a failure
]


def load():
    con = open_connection(None)
    tables = []
    for f in ["employees.csv", "payroll.csv", "attendance.xlsx"]:
        tables += load_file(con, DEMO / f, {t.name for t in tables}).tables
    return con, Catalog(tables, [confirm(c) for c in default_selection(detect_joins(con, tables))])


def _num(v):
    try:
        return round(float(v), 4)
    except (TypeError, ValueError):
        return str(v)


def check(con, a: Answer, expected) -> tuple[bool, str]:
    kind, arg = expected
    if kind == "refuse":
        if a.error is None:
            return False, f"expected a refusal, got an answer ({a.path})"
        return (arg.lower() in a.error.lower()), "refused"
    if kind in ("ask", "ask_or_disclose"):
        if a.path == "ask":
            return len(a.ask.options) == arg, f"asked: {' / '.join(a.ask.options)}"
        if kind == "ask_or_disclose" and a.error is None and any(" only — not including " in n for n in a.notes):
            return len(a.result.rows) == 5, "answered with disclosure: " + next(n for n in a.notes if " only — not including " in n)[:50]
        return False, f"expected a clarifying question or a disclosed reading, got {a.path} with no disclosure"
    if a.error is not None:
        return False, f"refused: {a.error[:80]}"
    r = a.result
    if kind == "scalar":
        exp = con.execute(arg).fetchone()[0]
        return _num(r.scalar) == _num(exp), f"{_num(r.scalar)} vs {_num(exp)}"
    if kind == "count_rows":
        exp = con.execute(arg).fetchone()[0]
        return len(r.rows) == exp, f"{len(r.rows)} rows vs {exp}"
    if kind == "headline":  # 'how many X over a threshold': the count is the answer, the rows the detail
        exp = con.execute(arg).fetchone()[0]
        return a.headline == exp, f"count {a.headline} vs {exp}" if a.headline is not None else f"no count shown ({len(r.rows)} rows)"
    if kind == "rows_values":  # the measure (last column) only: ties at a top-N cut make ids arbitrary, and
        exp = sorted(_num(row[-1]) for row in con.execute(arg).fetchall())  # the model may add name columns
        got = sorted(_num(row[-1]) for row in r.rows.itertuples(index=False, name=None))
        return got == exp, f"{len(got)} rows" if got == exp else f"mismatch: {got[:3]} vs {exp[:3]}"
    if kind == "share":  # (label, sql): either a scalar equal to sql, or a per-group table whose share_pct for label equals it
        label_, sql = arg
        exp = _num(con.execute(sql).fetchone()[0])
        if r.scalar is not None:
            return _num(r.scalar) == exp, f"{_num(r.scalar)} vs {exp}"
        if "share_pct" in r.rows.columns:
            row = r.rows[r.rows.iloc[:, 0].astype(str) == label_]
            got = _num(row["share_pct"].iloc[0]) if len(row) else None
            return got is not None and abs(got - exp) < 0.1, f"share table: {label_} = {got} vs {exp}"
        return False, f"no percentage: {list(r.rows.columns)}"
    if kind == "rows":
        exp = {str(row[0])[:10]: tuple(_num(x) for x in row[1:]) for row in con.execute(arg).fetchall()}
        got = {str(row[0])[:10]: tuple(_num(x) for x in row[1:]) for row in r.rows.itertuples(index=False, name=None)}
        return got == exp, f"{len(got)} rows" if got == exp else f"mismatch: {sorted(got.items())[:2]} vs {sorted(exp.items())[:2]}"
    raise ValueError(kind)


def provider_for(spec: str):
    """'groq' or 'groq:openai/gpt-oss-120b' (provider:model)."""
    name, _, model = spec.partition(":")
    return make_provider(name, model=model) if model else make_provider(name)


# Repairs the validator / intent layer made to the model's plan. These are the finding:
# a plan that needed one was structurally valid but did not say what the question asked.
_REPAIRS = [
    ("Ranked by ", "sort"),            # empty sort key on a ranking question
    ("Grouped by ", "bucket"),         # trend question with no time bucket
    ("Showing ", "limit"),             # limit 0 on a top-N question
    ("Sorted by '", "sort-key"),       # near-miss alias
    ("Took '", "table"),               # column on the wrong table
    ("Read '", "column"),              # misspelt column (the sum/avg disclosure is excluded below)
]


def repairs_for(a: Answer) -> list[str]:
    out = []
    for n in a.notes:
        if "as a total (sum)" in n or "as an average" in n or " only — not including " in n:
            continue  # disclosure of a reading, not a repair (pay->disclosed is tagged separately below)
        for prefix, tag in _REPAIRS:
            if n.startswith(prefix) and tag not in out:
                out.append(tag)
    if a.path.endswith(":retry"):
        out.append("retry")
    if a.path == "ask" and a.ask is not None and a.ask.substitute:
        out.append("arith->ask")
    if any(" only — not including " in n for n in a.notes):
        out.append("pay->disclosed")
    return out


GAP_SECONDS = 4.0  # between questions on hosted providers, so a 21-question run doesn't trip the limit

REPAIR_TAGS = ["sort", "limit", "bucket", "sort-key", "table", "column", "retry", "arith->ask", "pay->disclosed"]


def run(provider_name: str) -> list[dict]:
    con, cat = load()
    provider = provider_for(provider_name)
    hosted = provider.name in ("groq", "openrouter", "hosted")
    rows = []
    for i, (q, expected) in enumerate(QUESTIONS):
        t0 = time.perf_counter()
        a = answer(q, con, cat, provider=provider)
        total_ms = (time.perf_counter() - t0) * 1000
        model_used = a.path.startswith("model") or a.path == "ask" or (a.path == "refused" and a.attempts and a.attempts[0].source == "model")
        wait_ms = getattr(provider, "last_wait_ms", 0.0) if model_used else 0.0
        attempts = (getattr(provider, "last_attempts", 1) or 1) if model_used else 1
        row = {"q": q, "path": a.path, "ms": total_ms, "wait_ms": wait_ms, "infer_ms": max(total_ms - wait_ms, 0.0),
               "attempts": attempts, "repairs": repairs_for(a), "detail": "", "model_used": model_used}
        if a.error and "Without a model" in a.error:
            row.update(status="n/a", detail="not in the offline set", repairs=[])
            print(f"N/A   {total_ms:7.0f} ms  {'offline':14} {q!r}")
        elif a.error and ("rate-limited" in a.error or "HTTP 429" in a.error):
            row.update(status="RL", detail="rate-limited", repairs=[])
            print(f"RATE  {total_ms:7.0f} ms  {a.path:14} {q!r}")
        elif a.error and any(s in a.error for s in ("rejected the", "not found (HTTP", "couldn't reach", "couldn't connect",
                                                    "No model could answer", "not responding", "isn't installed", "try again in")):
            row.update(status="NA", detail=a.error[:60], repairs=[])
            print(f"UNAV  {total_ms:7.0f} ms  {a.path:14} {q!r}  - {a.error[:60]}")
        else:
            ok, detail = check(con, a, expected)
            row.update(status="OK" if ok else "FAIL", detail=detail)
            extra = (f"  [waited {wait_ms/1000:.0f}s, {attempts} attempts]" if attempts > 1 else "") + \
                    (f"  [repaired: {', '.join(row['repairs'])}]" if row["repairs"] else "")
            print(f"{'PASS' if ok else 'FAIL'}  {total_ms:7.0f} ms  {a.path:14} {q!r}  - {detail}{extra}")
        rows.append(row)
        if hosted and model_used and i < len(QUESTIONS) - 1:
            time.sleep(GAP_SECONDS)
    return rows


def label(spec: str) -> str:
    from dataqa.providers import short
    p = provider_for(spec)
    model = getattr(p, "model", "")
    return f"{short(p.name)} · {model}" if model else short(p.name)


ICON = {"OK": "✅", "FAIL": "❌", "RL": "⏳", "NA": "⛔", "n/a": "—"}


def _median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else None


def fmt_s(ms):
    return f"{ms/1000:.1f}s"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ollama", action="store_true", help="shorthand for --providers stub,ollama")
    ap.add_argument("--providers", default="", help="comma-separated; provider or provider:model, e.g. groq:openai/gpt-oss-120b")
    ap.add_argument("--hard", action="store_true", help="add the adversarial set (HARD_QUESTIONS)")
    args = ap.parse_args()
    global OUT, CMP
    if args.hard:  # the adversarial set gets its own artefacts so the base comparison isn't overwritten
        QUESTIONS.extend(HARD_QUESTIONS)
        OUT, CMP = OUT.with_name("adversarial_results.md"), CMP.with_name("adversarial.md")
    names = [n.strip() for n in args.providers.split(",") if n.strip()] or (["stub", "ollama"] if args.ollama else ["stub"])

    sections = [(label(n), run(n)) for n in names]

    OUT.parent.mkdir(exist_ok=True)
    md = ["# Eval results", "", "Demo data: `data/demo/` (employees 120 · payroll 1,411 · attendance 15,480). "
          "Expected values are computed by independent SQL in `scripts/eval.py`. Each provider runs alone — no fallback chain. "
          "Latency is split into model inference and time spent waiting on retries (429/5xx backoff).", ""]
    for title, rows in sections:
        scored = [r for r in rows if r["status"] in ("OK", "FAIL")]
        passed = sum(1 for r in scored if r["status"] == "OK")
        limited = sum(1 for r in rows if r["status"] in ("RL", "NA"))
        md += [f"## Provider: {title} — {passed}/{len(scored)} pass" + (f" · {limited} rate-limited/unavailable" if limited else ""), "",
               "| Question | Path | OK | Detail | Repaired | Inference | Waited on retries | Attempts |",
               "|---|---|---|---|---|---|---|---|"]
        for r in rows:
            md.append(f"| {r['q']} | {r['path']} | {ICON[r['status']]} | {r['detail']} | {', '.join(r['repairs']) or '—'} | "
                      f"{fmt_s(r['infer_ms'])} | {fmt_s(r['wait_ms']) if r['wait_ms'] else '—'} | {r['attempts']} |")
        md.append("")
    OUT.write_text("\n".join(md), encoding="utf-8")

    # ---- side-by-side comparison ----
    cmp = ["# Provider comparison" + (" — adversarial set" if args.hard else ""), "",
           "Same questions, same expected values, each provider alone (no fallback chain). "
           "Cell = result · path · **inference time**; `+Ns waiting` = time spent in 429/5xx backoff, shown separately so a "
           "rate-limited provider isn't reported as slow; **repaired: …** = the validator/intent layer had to fix the model's plan. "
           "An expected refusal counts as a pass; ⏳ rate-limited and ⛔ unavailable are provider outcomes, not model failures; "
           "a wrong number is the only real failure.", "",
           "| Question | " + " | ".join(t for t, _ in sections) + " |",
           "|---|" + "---|" * len(sections)]
    by_q = {}
    for title, rows in sections:
        for r in rows:
            by_q.setdefault(r["q"], {})[title] = r
    for q, _ in QUESTIONS:
        cells = []
        for title, _ in sections:
            r = by_q[q][title]
            if r["status"] == "n/a":
                cells.append("—"); continue
            cell = f"{ICON[r['status']]} {r['path']} · {fmt_s(r['infer_ms'])}"
            if r["wait_ms"]:
                cell += f" +{r['wait_ms']/1000:.0f}s waiting ({r['attempts']} attempts)"
            if r["status"] in ("FAIL", "NA"):
                cell += f" · {r['detail'][:60]}"
            if r["repairs"]:
                cell += f" · **repaired: {', '.join(r['repairs'])}**"
            cells.append(cell)
        cmp.append(f"| {q} | " + " | ".join(cells) + " |")

    cmp += ["", "## Summary", "",
            "| Provider | Pass | Wrong numbers | Rate-limited / unavailable | Plans needing repair | Median inference | Total time waiting on retries |",
            "|---|---|---|---|---|---|---|"]
    for title, rows in sections:
        scored = [r for r in rows if r["status"] in ("OK", "FAIL")]
        model_rows = [r for r in scored if r["model_used"]]
        wrong = sum(1 for r in scored if r["status"] == "FAIL" and not r["detail"].startswith(("refused", "expected")))
        limited = sum(1 for r in rows if r["status"] in ("RL", "NA"))
        repaired = sum(1 for r in model_rows if r["repairs"])
        med = _median([r["infer_ms"] for r in model_rows])
        waited = sum(r["wait_ms"] for r in rows)
        cmp.append(f"| {title} | {sum(1 for r in scored if r['status'] == 'OK')}/{len(scored)} | {wrong} | {limited} | "
                   f"{repaired}/{len(model_rows)} | {fmt_s(med) if med is not None else '—'} | "
                   f"{fmt_s(waited) if waited else '—'} |")

    cmp += ["", "## Repairs by type", "",
            "What the validation/intent layer had to fix in the model's plan, per provider. "
            "`sort` = ranking question with an empty sort key · `limit` = top-N with limit 0 · `bucket` = trend question with no time bucket · `sort-key` = near-miss alias · "
            "`table` = column on the wrong table · `column` = misspelt column · `retry` = plan fed back to the model once · "
            "`arith->ask` = arithmetic in a column field, turned into a substitute question · "
            "`pay->disclosed` = 'paid/pay' read as one money column; the result says which columns it excludes.", "",
            "| Provider | " + " | ".join(REPAIR_TAGS) + " | any |", "|---|" + "---|" * (len(REPAIR_TAGS) + 1)]
    for title, rows in sections:
        model_rows = [r for r in rows if r["status"] in ("OK", "FAIL") and r["model_used"]]
        counts = {t: sum(1 for r in model_rows if t in r["repairs"]) for t in REPAIR_TAGS}
        anyc = sum(1 for r in model_rows if r["repairs"])
        cmp.append(f"| {title} | " + " | ".join(str(counts[t]) for t in REPAIR_TAGS) + f" | {anyc}/{len(model_rows)} |")
    CMP.write_text("\n".join(cmp), encoding="utf-8")
    print(f"\nwrote {OUT}\nwrote {CMP}")


if __name__ == "__main__":
    main()
