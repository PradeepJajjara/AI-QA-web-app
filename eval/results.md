# Eval results

Demo data: `data/demo/` (employees 120 · payroll 1,411 · attendance 15,480). Expected values are computed by independent SQL in `scripts/eval.py`. Each provider runs alone — no fallback chain. Latency is split into model inference and time spent waiting on retries (429/5xx backoff).

## Provider: offline — 14/14 pass

| Question | Path | OK | Detail | Repaired | Inference | Waited on retries | Attempts |
|---|---|---|---|---|---|---|---|
| how many employees | template | ✅ | 120.0 vs 120.0 | — | 0.0s | — | 1 |
| total base_salary | template | ✅ | 206963503.0 vs 206963503.0 | — | 0.0s | — | 1 |
| average hours | template | ✅ | 7.6859 vs 7.6859 | — | 0.0s | — | 1 |
| average base_salary by department | template | ✅ | 5 rows | — | 0.0s | — | 1 |
| count of employees by location | template | ✅ | 5 rows | — | 0.0s | — | 1 |
| unique department | template | ✅ | 5.0 vs 5.0 | — | 0.0s | — | 1 |
| total bonus in March 2025 | model:offline | ✅ | 600813.0 vs 600813.0 | — | 0.0s | — | 1 |
| how many employees are in Engineering | model:offline | ✅ | 16.0 vs 16.0 | — | 0.0s | — | 1 |
| average hours worked per month | model:offline | ✅ | 6 rows | — | 0.0s | — | 1 |
| monthly total base salary | model:offline | ✅ | 12 rows | — | 0.0s | — | 1 |
| which department has the highest average base salary | model:offline | ✅ | 1 rows | — | 0.0s | — | 1 |
| leave days by department in Q1 2025 | model:offline | ✅ | 5 rows | — | 0.0s | — | 1 |
| compare average bonus between Sales and Engineering | model:offline | ✅ | 2 rows | — | 0.0s | — | 1 |
| list employees in Hyderabad | model:offline | ✅ | 26 rows vs 26 | — | 0.0s | — | 1 |
| how many employees joined in 2023 | refused | — | not in the offline set | — | 0.0s | — | 1 |
| which location has the most employees | refused | — | not in the offline set | — | 0.0s | — | 1 |
| weekly WFH count trend in Q2 2025 | refused | — | not in the offline set | — | 0.0s | — | 1 |
| total deductions for engineering vs sales | refused | — | not in the offline set | — | 0.0s | — | 1 |
| which employees have more than 75% attendance | refused | — | not in the offline set | — | 0.0s | — | 1 |
| what percentage of attendance records are on leave, by department | refused | — | not in the offline set | — | 0.0s | — | 1 |
| total bonus by attendance status | refused | — | not in the offline set | — | 0.0s | — | 1 |
| show me the 5 highest paid employees | refused | — | not in the offline set | — | 0.0s | — | 1 |
| what percentage of employees are in Sales | refused | — | not in the offline set | — | 0.0s | — | 1 |
| attendance rate by department | refused | — | not in the offline set | — | 0.0s | — | 1 |
| WFH rate per employee, top 5 | refused | — | not in the offline set | — | 0.0s | — | 1 |
| what share of total bonus went to Engineering | refused | — | not in the offline set | — | 0.0s | — | 1 |
| how many employees have more than 75% attendance | refused | — | not in the offline set | — | 0.0s | — | 1 |
| percentage of leave days by department | refused | — | not in the offline set | — | 0.0s | — | 1 |

## Provider: ollama · qwen3.5:4b — 28/28 pass

| Question | Path | OK | Detail | Repaired | Inference | Waited on retries | Attempts |
|---|---|---|---|---|---|---|---|
| how many employees | template | ✅ | 120.0 vs 120.0 | — | 0.0s | — | 1 |
| total base_salary | template | ✅ | 206963503.0 vs 206963503.0 | — | 0.0s | — | 1 |
| average hours | template | ✅ | 7.6859 vs 7.6859 | — | 0.0s | — | 1 |
| average base_salary by department | template | ✅ | 5 rows | — | 0.0s | — | 1 |
| count of employees by location | template | ✅ | 5 rows | — | 0.0s | — | 1 |
| unique department | template | ✅ | 5.0 vs 5.0 | — | 0.0s | — | 1 |
| total bonus in March 2025 | model:ollama | ✅ | 600813.0 vs 600813.0 | — | 12.2s | — | 1 |
| how many employees are in Engineering | model:ollama | ✅ | 16.0 vs 16.0 | — | 3.5s | — | 1 |
| average hours worked per month | model:ollama | ✅ | 6 rows | — | 3.5s | — | 1 |
| monthly total base salary | model:ollama | ✅ | 12 rows | — | 3.6s | — | 1 |
| which department has the highest average base salary | model:ollama | ✅ | 1 rows | — | 3.5s | — | 1 |
| leave days by department in Q1 2025 | model:ollama | ✅ | 5 rows | — | 4.3s | — | 1 |
| compare average bonus between Sales and Engineering | model:ollama | ✅ | 2 rows | — | 5.0s | — | 1 |
| list employees in Hyderabad | model:ollama | ✅ | 26 rows vs 26 | — | 3.2s | — | 1 |
| how many employees joined in 2023 | model:ollama | ✅ | 15.0 vs 15.0 | — | 3.5s | — | 1 |
| which location has the most employees | model:ollama | ✅ | 1 rows | — | 3.4s | — | 1 |
| weekly WFH count trend in Q2 2025 | model:ollama | ✅ | 14 rows | bucket | 4.7s | — | 1 |
| total deductions for engineering vs sales | model:ollama | ✅ | 2 rows | — | 4.0s | — | 1 |
| which employees have more than 75% attendance | model:ollama | ✅ | 120 rows vs 120 | — | 3.7s | — | 1 |
| what percentage of attendance records are on leave, by department | model:ollama | ✅ | 5 rows | — | 3.6s | — | 1 |
| total bonus by attendance status | refused | ✅ | refused | — | 3.5s | — | 1 |
| show me the 5 highest paid employees | ask | ✅ | asked: base_salary / bonus / deductions | arith->ask | 3.9s | — | 1 |
| what percentage of employees are in Sales | model:ollama:retry | ✅ | 22.5 vs 22.5 | retry | 6.4s | — | 1 |
| attendance rate by department | model:ollama | ✅ | 5 rows | — | 3.6s | — | 1 |
| WFH rate per employee, top 5 | model:ollama | ✅ | 5 rows | — | 3.8s | — | 1 |
| what share of total bonus went to Engineering | model:ollama | ✅ | share table: Engineering = 14.4 vs 14.3972 | — | 3.5s | — | 1 |
| how many employees have more than 75% attendance | model:ollama | ✅ | count 120 vs 120 | — | 4.0s | — | 1 |
| percentage of leave days by department | model:ollama | ✅ | 5 rows | — | 3.6s | — | 1 |

## Provider: groq · qwen/qwen3.8-27b — 7/7 pass · 21 rate-limited/unavailable

| Question | Path | OK | Detail | Repaired | Inference | Waited on retries | Attempts |
|---|---|---|---|---|---|---|---|
| how many employees | template | ✅ | 120.0 vs 120.0 | — | 0.0s | — | 1 |
| total base_salary | template | ✅ | 206963503.0 vs 206963503.0 | — | 0.0s | — | 1 |
| average hours | template | ✅ | 7.6859 vs 7.6859 | — | 0.0s | — | 1 |
| average base_salary by department | template | ✅ | 5 rows | — | 0.0s | — | 1 |
| count of employees by location | template | ✅ | 5 rows | — | 0.0s | — | 1 |
| unique department | template | ✅ | 5.0 vs 5.0 | — | 0.0s | — | 1 |
| total bonus in March 2025 | model:groq | ✅ | 600813.0 vs 600813.0 | — | 1.8s | — | 1 |
| how many employees are in Engineering | refused | ⏳ | rate-limited | — | 0.6s | — | 1 |
| average hours worked per month | refused | ⏳ | rate-limited | — | 0.8s | — | 1 |
| monthly total base salary | refused | ⏳ | rate-limited | — | 1.7s | — | 1 |
| which department has the highest average base salary | refused | ⏳ | rate-limited | — | 0.8s | — | 1 |
| leave days by department in Q1 2025 | refused | ⏳ | rate-limited | — | 1.2s | — | 1 |
| compare average bonus between Sales and Engineering | refused | ⏳ | rate-limited | — | 0.8s | — | 1 |
| list employees in Hyderabad | refused | ⏳ | rate-limited | — | 0.7s | — | 1 |
| how many employees joined in 2023 | refused | ⏳ | rate-limited | — | 0.7s | — | 1 |
| which location has the most employees | refused | ⏳ | rate-limited | — | 0.6s | — | 1 |
| weekly WFH count trend in Q2 2025 | refused | ⏳ | rate-limited | — | 0.6s | — | 1 |
| total deductions for engineering vs sales | refused | ⏳ | rate-limited | — | 0.6s | — | 1 |
| which employees have more than 75% attendance | refused | ⏳ | rate-limited | — | 0.5s | — | 1 |
| what percentage of attendance records are on leave, by department | refused | ⏳ | rate-limited | — | 0.8s | — | 1 |
| total bonus by attendance status | refused | ⏳ | rate-limited | — | 0.6s | — | 1 |
| show me the 5 highest paid employees | refused | ⏳ | rate-limited | — | 0.6s | — | 1 |
| what percentage of employees are in Sales | refused | ⏳ | rate-limited | — | 0.7s | — | 1 |
| attendance rate by department | refused | ⏳ | rate-limited | — | 0.9s | — | 1 |
| WFH rate per employee, top 5 | refused | ⏳ | rate-limited | — | 0.6s | — | 1 |
| what share of total bonus went to Engineering | refused | ⏳ | rate-limited | — | 0.8s | — | 1 |
| how many employees have more than 75% attendance | refused | ⏳ | rate-limited | — | 0.6s | — | 1 |
| percentage of leave days by department | refused | ⏳ | rate-limited | — | 0.5s | — | 1 |

## Provider: groq · openai/gpt-oss-120b — 27/28 pass

| Question | Path | OK | Detail | Repaired | Inference | Waited on retries | Attempts |
|---|---|---|---|---|---|---|---|
| how many employees | template | ✅ | 120.0 vs 120.0 | — | 0.0s | — | 1 |
| total base_salary | template | ✅ | 206963503.0 vs 206963503.0 | — | 0.0s | — | 1 |
| average hours | template | ✅ | 7.6859 vs 7.6859 | — | 0.0s | — | 1 |
| average base_salary by department | template | ✅ | 5 rows | — | 0.0s | — | 1 |
| count of employees by location | template | ✅ | 5 rows | — | 0.0s | — | 1 |
| unique department | template | ✅ | 5.0 vs 5.0 | — | 0.0s | — | 1 |
| total bonus in March 2025 | model:groq | ✅ | 600813.0 vs 600813.0 | — | 2.3s | — | 1 |
| how many employees are in Engineering | model:groq | ✅ | 16.0 vs 16.0 | — | 2.6s | 1.0s | 2 |
| average hours worked per month | model:groq | ✅ | 6 rows | — | 4.4s | 24.0s | 3 |
| monthly total base salary | model:groq | ✅ | 12 rows | — | 2.6s | 25.0s | 2 |
| which department has the highest average base salary | model:groq | ✅ | 1 rows | — | 2.7s | 27.0s | 2 |
| leave days by department in Q1 2025 | model:groq | ✅ | 5 rows | — | 3.3s | 26.0s | 2 |
| compare average bonus between Sales and Engineering | model:groq | ✅ | 2 rows | — | 3.6s | 28.0s | 2 |
| list employees in Hyderabad | model:groq | ✅ | 26 rows vs 26 | — | 3.4s | 25.0s | 2 |
| how many employees joined in 2023 | model:groq | ✅ | 15.0 vs 15.0 | — | 3.1s | 24.0s | 2 |
| which location has the most employees | model:groq | ✅ | 1 rows | — | 2.0s | — | 1 |
| weekly WFH count trend in Q2 2025 | model:groq | ✅ | 14 rows | — | 3.4s | 23.0s | 2 |
| total deductions for engineering vs sales | model:groq | ✅ | 2 rows | — | 2.3s | — | 1 |
| which employees have more than 75% attendance | model:groq | ✅ | 120 rows vs 120 | — | 3.4s | 26.0s | 2 |
| what percentage of attendance records are on leave, by department | model:groq | ✅ | 5 rows | — | 2.3s | — | 1 |
| total bonus by attendance status | refused | ✅ | refused | — | 2.0s | — | 1 |
| show me the 5 highest paid employees | model:groq | ✅ | answered with disclosure: Read 'paid' as base_salary only — not including bo | pay->disclosed | 4.1s | 22.0s | 2 |
| what percentage of employees are in Sales | model:groq | ✅ | 22.5 vs 22.5 | — | 2.9s | 30.0s | 2 |
| attendance rate by department | model:groq | ✅ | 5 rows | — | 3.1s | 26.0s | 2 |
| WFH rate per employee, top 5 | model:groq | ✅ | 5 rows | — | 3.5s | 24.0s | 2 |
| what share of total bonus went to Engineering | refused | ❌ | refused: 'payroll' has no column 'department'. Did you mean pay_month? | — | 2.5s | — | 1 |
| how many employees have more than 75% attendance | model:groq | ✅ | count 120 vs 120 | — | 3.5s | 28.0s | 2 |
| percentage of leave days by department | model:groq | ✅ | 5 rows | — | 2.5s | 27.0s | 2 |
