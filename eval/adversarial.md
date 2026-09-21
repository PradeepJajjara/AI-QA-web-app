# Provider comparison — adversarial set

Same questions, same expected values, each provider alone (no fallback chain). Cell = result · path · **inference time**; `+Ns waiting` = time spent in 429/5xx backoff, shown separately so a rate-limited provider isn't reported as slow; **repaired: …** = the validator/intent layer had to fix the model's plan. An expected refusal counts as a pass; ⏳ rate-limited and ⛔ unavailable are provider outcomes, not model failures; a wrong number is the only real failure.

| Question | offline | ollama · qwen3.5:4b | groq · qwen/qwen3.8-27b |
|---|---|---|---|
| how many employees | ✅ template · 0.0s | ✅ template · 0.0s | ✅ template · 0.0s |
| total base_salary | ✅ template · 0.0s | ✅ template · 0.0s | ✅ template · 0.0s |
| average hours | ✅ template · 0.0s | ✅ template · 0.0s | ✅ template · 0.0s |
| average base_salary by department | ✅ template · 0.0s | ✅ template · 0.0s | ✅ template · 0.0s |
| count of employees by location | ✅ template · 0.0s | ✅ template · 0.0s | ✅ template · 0.0s |
| unique department | ✅ template · 0.0s | ✅ template · 0.0s | ✅ template · 0.0s |
| total bonus in March 2025 | ✅ model:offline · 0.0s | ✅ model:ollama · 8.7s | ⏳ refused · 1.1s |
| how many employees are in Engineering | ✅ model:offline · 0.0s | ✅ model:ollama · 2.6s | ⏳ refused · 1.0s |
| average hours worked per month | ✅ model:offline · 0.0s | ✅ model:ollama · 3.3s | ⏳ refused · 0.9s |
| monthly total base salary | ✅ model:offline · 0.0s | ✅ model:ollama · 3.3s | ⏳ refused · 1.2s |
| which department has the highest average base salary | ✅ model:offline · 0.0s | ✅ model:ollama · 3.2s | ⏳ refused · 1.1s |
| leave days by department in Q1 2025 | ✅ model:offline · 0.0s | ✅ model:ollama · 3.5s | ⏳ refused · 0.9s |
| compare average bonus between Sales and Engineering | ✅ model:offline · 0.0s | ✅ model:ollama · 3.2s | ⏳ refused · 0.9s |
| list employees in Hyderabad | ✅ model:offline · 0.0s | ✅ model:ollama · 2.4s | ⏳ refused · 0.9s |
| how many employees joined in 2023 | — | ✅ model:ollama · 2.8s | ⏳ refused · 1.0s |
| which location has the most employees | — | ✅ model:ollama · 2.8s | ⏳ refused · 0.9s |
| weekly WFH count trend in Q2 2025 | — | ✅ model:ollama · 3.7s | ⏳ refused · 0.9s |
| total deductions for engineering vs sales | — | ✅ model:ollama · 3.1s | ⏳ refused · 0.9s |
| which employees have more than 75% attendance | — | ✅ model:ollama · 3.0s | ⏳ refused · 0.9s |
| what percentage of attendance records are on leave, by department | — | ✅ model:ollama · 2.9s | ⏳ refused · 0.9s |
| total bonus by attendance status | — | ✅ refused · 2.8s | ⏳ refused · 0.9s |
| show me the 5 highest paid employees | — | ✅ ask · 3.3s · **repaired: arith->ask** | ⏳ refused · 0.9s |
| what percentage of employees are in Sales | — | ✅ model:ollama:retry · 5.3s · **repaired: retry** | ⏳ refused · 0.9s |
| attendance rate by department | — | ✅ model:ollama · 2.8s | ⏳ refused · 1.1s |
| WFH rate per employee, top 5 | — | ✅ model:ollama · 3.2s | ⏳ refused · 0.9s |
| what share of total bonus went to Engineering | — | ✅ model:ollama · 2.9s | ⏳ refused · 1.1s |
| how many employees have more than 75% attendance | — | ✅ model:ollama · 3.1s | ⏳ refused · 0.9s |
| percentage of leave days by department | — | ✅ model:ollama · 2.8s | ⏳ refused · 0.9s |
| which locations have an average base salary above 150000 | — | ✅ model:ollama · 3.3s | ⏳ refused · 0.9s |
| departments where total bonus exceeds 1.5 million | — | ❌ refused · 9.0s · refused: I couldn't tell what to compare department against. | ⏳ refused · 0.9s |
| how many employees earn more than 200000 base salary | — | ✅ model:ollama · 3.7s | ⏳ refused · 0.9s |
| average hours on WFH days by department | — | ✅ model:ollama · 3.8s | ⏳ refused · 0.9s |
| bonus paid in Q2 2025 by department | — | ✅ model:ollama · 4.1s · **repaired: pay->disclosed** | ⏳ refused · 0.9s |
| which grade has the most WFH days | — | ✅ model:ollama · 3.6s · **repaired: limit** | ⏳ refused · 0.9s |
| average base salary of employees who joined in 2024 | — | ✅ model:ollama · 3.5s | ⏳ refused · 0.9s |
| monthly leave count for engineering | — | ✅ model:ollama · 4.3s | ⏳ refused · 1.0s |
| percentage of attendance records that are leave in Sales | — | ✅ model:ollama · 4.0s | ⏳ refused · 0.9s |
| top 3 locations by total deductions | — | ✅ model:ollama · 3.7s | ⏳ refused · 2.7s +4s waiting (3 attempts) |
| employees with attendance below 80% | — | ✅ model:ollama · 4.0s | ⏳ refused · 0.9s |
| total base salary in March vs April 2025 | — | ✅ model:ollama · 4.4s · **repaired: column** | ⏳ refused · 1.8s +1s waiting (2 attempts) |
| how many distinct managers | — | ✅ model:ollama · 3.2s | ⏳ refused · 0.9s |
| lowest average hours by location | — | ❌ model:ollama · 3.7s · mismatch: [('Bengaluru', (7.7444,)), ('Dubai', (7.6048,))] v | ⏳ refused · 0.9s |
| count of employees per manager, top 5 | — | ✅ model:ollama · 3.7s | ⏳ refused · 0.9s |
| average tenure by department | ✅ refused · 0.0s | ✅ refused · 0.0s | ✅ refused · 0.0s |

## Summary

| Provider | Pass | Wrong numbers | Rate-limited / unavailable | Plans needing repair | Median inference | Total time waiting on retries |
|---|---|---|---|---|---|---|
| offline | 15/15 | 0 | 0 | 0/8 | 0.0s | — |
| ollama · qwen3.5:4b | 42/44 | 1 | 0 | 5/37 | 3.3s | — |
| groq · qwen/qwen3.8-27b | 7/7 | 0 | 37 | 0/0 | — | 4.5s |

## Repairs by type

What the validation/intent layer had to fix in the model's plan, per provider. `sort` = ranking question with an empty sort key · `limit` = top-N with limit 0 · `bucket` = trend question with no time bucket · `sort-key` = near-miss alias · `table` = column on the wrong table · `column` = misspelt column · `retry` = plan fed back to the model once · `arith->ask` = arithmetic in a column field, turned into a substitute question · `pay->disclosed` = 'paid/pay' read as one money column; the result says which columns it excludes.

| Provider | sort | limit | bucket | sort-key | table | column | retry | arith->ask | pay->disclosed | any |
|---|---|---|---|---|---|---|---|---|---|---|
| offline | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0/8 |
| ollama · qwen3.5:4b | 0 | 1 | 0 | 0 | 0 | 1 | 1 | 1 | 1 | 5/37 |
| groq · qwen/qwen3.8-27b | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0/0 |