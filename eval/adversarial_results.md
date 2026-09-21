# Eval results

Demo data: `data/demo/` (employees 120 · payroll 1,411 · attendance 15,480). Expected values are computed by independent SQL in `scripts/eval.py`. Each provider runs alone — no fallback chain. Latency is split into model inference and time spent waiting on retries (429/5xx backoff).

## Provider: offline — 15/15 pass

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
| which locations have an average base salary above 150000 | refused | — | not in the offline set | — | 0.0s | — | 1 |
| departments where total bonus exceeds 1.5 million | refused | — | not in the offline set | — | 0.0s | — | 1 |
| how many employees earn more than 200000 base salary | refused | — | not in the offline set | — | 0.0s | — | 1 |
| average hours on WFH days by department | refused | — | not in the offline set | — | 0.0s | — | 1 |
| bonus paid in Q2 2025 by department | refused | — | not in the offline set | — | 0.0s | — | 1 |
| which grade has the most WFH days | refused | — | not in the offline set | — | 0.0s | — | 1 |
| average base salary of employees who joined in 2024 | refused | — | not in the offline set | — | 0.0s | — | 1 |
| monthly leave count for engineering | refused | — | not in the offline set | — | 0.0s | — | 1 |
| percentage of attendance records that are leave in Sales | refused | — | not in the offline set | — | 0.0s | — | 1 |
| top 3 locations by total deductions | refused | — | not in the offline set | — | 0.0s | — | 1 |
| employees with attendance below 80% | refused | — | not in the offline set | — | 0.0s | — | 1 |
| total base salary in March vs April 2025 | refused | — | not in the offline set | — | 0.0s | — | 1 |
| how many distinct managers | refused | — | not in the offline set | — | 0.0s | — | 1 |
| lowest average hours by location | refused | — | not in the offline set | — | 0.0s | — | 1 |
| count of employees per manager, top 5 | refused | — | not in the offline set | — | 0.0s | — | 1 |
| average tenure by department | refused | ✅ | refused | — | 0.0s | — | 1 |

## Provider: ollama · qwen3.5:4b — 42/44 pass

| Question | Path | OK | Detail | Repaired | Inference | Waited on retries | Attempts |
|---|---|---|---|---|---|---|---|
| how many employees | template | ✅ | 120.0 vs 120.0 | — | 0.0s | — | 1 |
| total base_salary | template | ✅ | 206963503.0 vs 206963503.0 | — | 0.0s | — | 1 |
| average hours | template | ✅ | 7.6859 vs 7.6859 | — | 0.0s | — | 1 |
| average base_salary by department | template | ✅ | 5 rows | — | 0.0s | — | 1 |
| count of employees by location | template | ✅ | 5 rows | — | 0.0s | — | 1 |
| unique department | template | ✅ | 5.0 vs 5.0 | — | 0.0s | — | 1 |
| total bonus in March 2025 | model:ollama | ✅ | 600813.0 vs 600813.0 | — | 8.7s | — | 1 |
| how many employees are in Engineering | model:ollama | ✅ | 16.0 vs 16.0 | — | 2.6s | — | 1 |
| average hours worked per month | model:ollama | ✅ | 6 rows | — | 3.3s | — | 1 |
| monthly total base salary | model:ollama | ✅ | 12 rows | — | 3.3s | — | 1 |
| which department has the highest average base salary | model:ollama | ✅ | 1 rows | — | 3.2s | — | 1 |
| leave days by department in Q1 2025 | model:ollama | ✅ | 5 rows | — | 3.5s | — | 1 |
| compare average bonus between Sales and Engineering | model:ollama | ✅ | 2 rows | — | 3.2s | — | 1 |
| list employees in Hyderabad | model:ollama | ✅ | 26 rows vs 26 | — | 2.4s | — | 1 |
| how many employees joined in 2023 | model:ollama | ✅ | 15.0 vs 15.0 | — | 2.8s | — | 1 |
| which location has the most employees | model:ollama | ✅ | 1 rows | — | 2.8s | — | 1 |
| weekly WFH count trend in Q2 2025 | model:ollama | ✅ | 14 rows | — | 3.7s | — | 1 |
| total deductions for engineering vs sales | model:ollama | ✅ | 2 rows | — | 3.1s | — | 1 |
| which employees have more than 75% attendance | model:ollama | ✅ | 120 rows vs 120 | — | 3.0s | — | 1 |
| what percentage of attendance records are on leave, by department | model:ollama | ✅ | 5 rows | — | 2.9s | — | 1 |
| total bonus by attendance status | refused | ✅ | refused | — | 2.8s | — | 1 |
| show me the 5 highest paid employees | ask | ✅ | asked: base_salary / bonus / deductions | arith->ask | 3.3s | — | 1 |
| what percentage of employees are in Sales | model:ollama:retry | ✅ | 22.5 vs 22.5 | retry | 5.3s | — | 1 |
| attendance rate by department | model:ollama | ✅ | 5 rows | — | 2.8s | — | 1 |
| WFH rate per employee, top 5 | model:ollama | ✅ | 5 rows | — | 3.2s | — | 1 |
| what share of total bonus went to Engineering | model:ollama | ✅ | share table: Engineering = 14.4 vs 14.3972 | — | 2.9s | — | 1 |
| how many employees have more than 75% attendance | model:ollama | ✅ | count 120 vs 120 | — | 3.1s | — | 1 |
| percentage of leave days by department | model:ollama | ✅ | 5 rows | — | 2.8s | — | 1 |
| which locations have an average base salary above 150000 | model:ollama | ✅ | 1 rows | — | 3.3s | — | 1 |
| departments where total bonus exceeds 1.5 million | refused | ❌ | refused: I couldn't tell what to compare department against. Give a value — e.g. 'departm | — | 9.0s | — | 1 |
| how many employees earn more than 200000 base salary | model:ollama | ✅ | 28.0 vs 28.0 | — | 3.7s | — | 1 |
| average hours on WFH days by department | model:ollama | ✅ | 5 rows | — | 3.8s | — | 1 |
| bonus paid in Q2 2025 by department | model:ollama | ✅ | 5 rows | pay->disclosed | 4.1s | — | 1 |
| which grade has the most WFH days | model:ollama | ✅ | 1 rows | limit | 3.6s | — | 1 |
| average base salary of employees who joined in 2024 | model:ollama | ✅ | 149241.1955 vs 149241.1955 | — | 3.5s | — | 1 |
| monthly leave count for engineering | model:ollama | ✅ | 6 rows | — | 4.3s | — | 1 |
| percentage of attendance records that are leave in Sales | model:ollama | ✅ | 9.532 vs 9.532 | — | 4.0s | — | 1 |
| top 3 locations by total deductions | model:ollama | ✅ | 3 rows | — | 3.7s | — | 1 |
| employees with attendance below 80% | model:ollama | ✅ | 9 rows vs 9 | — | 4.0s | — | 1 |
| total base salary in March vs April 2025 | model:ollama | ✅ | 2 rows | column | 4.4s | — | 1 |
| how many distinct managers | model:ollama | ✅ | 10.0 vs 10.0 | — | 3.2s | — | 1 |
| lowest average hours by location | model:ollama | ❌ | mismatch: [('Bengaluru', (7.7444,)), ('Dubai', (7.6048,))] vs [('Dubai', (7.6048,))] | — | 3.7s | — | 1 |
| count of employees per manager, top 5 | model:ollama | ✅ | 5 rows | — | 3.7s | — | 1 |
| average tenure by department | refused | ✅ | refused | — | 0.0s | — | 1 |

## Provider: groq · qwen/qwen3.8-27b — 7/7 pass · 37 rate-limited/unavailable

| Question | Path | OK | Detail | Repaired | Inference | Waited on retries | Attempts |
|---|---|---|---|---|---|---|---|
| how many employees | template | ✅ | 120.0 vs 120.0 | — | 0.0s | — | 1 |
| total base_salary | template | ✅ | 206963503.0 vs 206963503.0 | — | 0.0s | — | 1 |
| average hours | template | ✅ | 7.6859 vs 7.6859 | — | 0.0s | — | 1 |
| average base_salary by department | template | ✅ | 5 rows | — | 0.0s | — | 1 |
| count of employees by location | template | ✅ | 5 rows | — | 0.0s | — | 1 |
| unique department | template | ✅ | 5.0 vs 5.0 | — | 0.0s | — | 1 |
| total bonus in March 2025 | refused | ⏳ | rate-limited | — | 1.1s | — | 1 |
| how many employees are in Engineering | refused | ⏳ | rate-limited | — | 1.0s | — | 1 |
| average hours worked per month | refused | ⏳ | rate-limited | — | 0.9s | — | 1 |
| monthly total base salary | refused | ⏳ | rate-limited | — | 1.2s | — | 1 |
| which department has the highest average base salary | refused | ⏳ | rate-limited | — | 1.1s | — | 1 |
| leave days by department in Q1 2025 | refused | ⏳ | rate-limited | — | 0.9s | — | 1 |
| compare average bonus between Sales and Engineering | refused | ⏳ | rate-limited | — | 0.9s | — | 1 |
| list employees in Hyderabad | refused | ⏳ | rate-limited | — | 0.9s | — | 1 |
| how many employees joined in 2023 | refused | ⏳ | rate-limited | — | 1.0s | — | 1 |
| which location has the most employees | refused | ⏳ | rate-limited | — | 0.9s | — | 1 |
| weekly WFH count trend in Q2 2025 | refused | ⏳ | rate-limited | — | 0.9s | — | 1 |
| total deductions for engineering vs sales | refused | ⏳ | rate-limited | — | 0.9s | — | 1 |
| which employees have more than 75% attendance | refused | ⏳ | rate-limited | — | 0.9s | — | 1 |
| what percentage of attendance records are on leave, by department | refused | ⏳ | rate-limited | — | 0.9s | — | 1 |
| total bonus by attendance status | refused | ⏳ | rate-limited | — | 0.9s | — | 1 |
| show me the 5 highest paid employees | refused | ⏳ | rate-limited | — | 0.9s | — | 1 |
| what percentage of employees are in Sales | refused | ⏳ | rate-limited | — | 0.9s | — | 1 |
| attendance rate by department | refused | ⏳ | rate-limited | — | 1.1s | — | 1 |
| WFH rate per employee, top 5 | refused | ⏳ | rate-limited | — | 0.9s | — | 1 |
| what share of total bonus went to Engineering | refused | ⏳ | rate-limited | — | 1.1s | — | 1 |
| how many employees have more than 75% attendance | refused | ⏳ | rate-limited | — | 0.9s | — | 1 |
| percentage of leave days by department | refused | ⏳ | rate-limited | — | 0.9s | — | 1 |
| which locations have an average base salary above 150000 | refused | ⏳ | rate-limited | — | 0.9s | — | 1 |
| departments where total bonus exceeds 1.5 million | refused | ⏳ | rate-limited | — | 0.9s | — | 1 |
| how many employees earn more than 200000 base salary | refused | ⏳ | rate-limited | — | 0.9s | — | 1 |
| average hours on WFH days by department | refused | ⏳ | rate-limited | — | 0.9s | — | 1 |
| bonus paid in Q2 2025 by department | refused | ⏳ | rate-limited | — | 0.9s | — | 1 |
| which grade has the most WFH days | refused | ⏳ | rate-limited | — | 0.9s | — | 1 |
| average base salary of employees who joined in 2024 | refused | ⏳ | rate-limited | — | 0.9s | — | 1 |
| monthly leave count for engineering | refused | ⏳ | rate-limited | — | 1.0s | — | 1 |
| percentage of attendance records that are leave in Sales | refused | ⏳ | rate-limited | — | 0.9s | — | 1 |
| top 3 locations by total deductions | refused | ⏳ | rate-limited | — | 2.7s | 3.5s | 3 |
| employees with attendance below 80% | refused | ⏳ | rate-limited | — | 0.9s | — | 1 |
| total base salary in March vs April 2025 | refused | ⏳ | rate-limited | — | 1.8s | 1.0s | 2 |
| how many distinct managers | refused | ⏳ | rate-limited | — | 0.9s | — | 1 |
| lowest average hours by location | refused | ⏳ | rate-limited | — | 0.9s | — | 1 |
| count of employees per manager, top 5 | refused | ⏳ | rate-limited | — | 0.9s | — | 1 |
| average tenure by department | refused | ✅ | refused | — | 0.0s | — | 1 |
