# Provider comparison

Same questions, same expected values, each provider alone (no fallback chain). Cell = result · path · **inference time**; `+Ns waiting` = time spent in 429/5xx backoff, shown separately so a rate-limited provider isn't reported as slow; **repaired: …** = the validator/intent layer had to fix the model's plan. An expected refusal counts as a pass; ⏳ rate-limited and ⛔ unavailable are provider outcomes, not model failures; a wrong number is the only real failure.

| Question | offline | ollama · qwen3.5:4b | groq · qwen/qwen3.8-27b | groq · openai/gpt-oss-120b |
|---|---|---|---|---|
| how many employees | ✅ template · 0.0s | ✅ template · 0.0s | ✅ template · 0.0s | ✅ template · 0.0s |
| total base_salary | ✅ template · 0.0s | ✅ template · 0.0s | ✅ template · 0.0s | ✅ template · 0.0s |
| average hours | ✅ template · 0.0s | ✅ template · 0.0s | ✅ template · 0.0s | ✅ template · 0.0s |
| average base_salary by department | ✅ template · 0.0s | ✅ template · 0.0s | ✅ template · 0.0s | ✅ template · 0.0s |
| count of employees by location | ✅ template · 0.0s | ✅ template · 0.0s | ✅ template · 0.0s | ✅ template · 0.0s |
| unique department | ✅ template · 0.0s | ✅ template · 0.0s | ✅ template · 0.0s | ✅ template · 0.0s |
| total bonus in March 2025 | ✅ model:offline · 0.0s | ✅ model:ollama · 12.2s | ✅ model:groq · 1.8s | ✅ model:groq · 2.3s |
| how many employees are in Engineering | ✅ model:offline · 0.0s | ✅ model:ollama · 3.5s | ⏳ refused · 0.6s | ✅ model:groq · 2.6s +1s waiting (2 attempts) |
| average hours worked per month | ✅ model:offline · 0.0s | ✅ model:ollama · 3.5s | ⏳ refused · 0.8s | ✅ model:groq · 4.4s +24s waiting (3 attempts) |
| monthly total base salary | ✅ model:offline · 0.0s | ✅ model:ollama · 3.6s | ⏳ refused · 1.7s | ✅ model:groq · 2.6s +25s waiting (2 attempts) |
| which department has the highest average base salary | ✅ model:offline · 0.0s | ✅ model:ollama · 3.5s | ⏳ refused · 0.8s | ✅ model:groq · 2.7s +27s waiting (2 attempts) |
| leave days by department in Q1 2025 | ✅ model:offline · 0.0s | ✅ model:ollama · 4.3s | ⏳ refused · 1.2s | ✅ model:groq · 3.3s +26s waiting (2 attempts) |
| compare average bonus between Sales and Engineering | ✅ model:offline · 0.0s | ✅ model:ollama · 5.0s | ⏳ refused · 0.8s | ✅ model:groq · 3.6s +28s waiting (2 attempts) |
| list employees in Hyderabad | ✅ model:offline · 0.0s | ✅ model:ollama · 3.2s | ⏳ refused · 0.7s | ✅ model:groq · 3.4s +25s waiting (2 attempts) |
| how many employees joined in 2023 | — | ✅ model:ollama · 3.5s | ⏳ refused · 0.7s | ✅ model:groq · 3.1s +24s waiting (2 attempts) |
| which location has the most employees | — | ✅ model:ollama · 3.4s | ⏳ refused · 0.6s | ✅ model:groq · 2.0s |
| weekly WFH count trend in Q2 2025 | — | ✅ model:ollama · 4.7s · **repaired: bucket** | ⏳ refused · 0.6s | ✅ model:groq · 3.4s +23s waiting (2 attempts) |
| total deductions for engineering vs sales | — | ✅ model:ollama · 4.0s | ⏳ refused · 0.6s | ✅ model:groq · 2.3s |
| which employees have more than 75% attendance | — | ✅ model:ollama · 3.7s | ⏳ refused · 0.5s | ✅ model:groq · 3.4s +26s waiting (2 attempts) |
| what percentage of attendance records are on leave, by department | — | ✅ model:ollama · 3.6s | ⏳ refused · 0.8s | ✅ model:groq · 2.3s |
| total bonus by attendance status | — | ✅ refused · 3.5s | ⏳ refused · 0.6s | ✅ refused · 2.0s |
| show me the 5 highest paid employees | — | ✅ ask · 3.9s · **repaired: arith->ask** | ⏳ refused · 0.6s | ✅ model:groq · 4.1s +22s waiting (2 attempts) · **repaired: pay->disclosed** |
| what percentage of employees are in Sales | — | ✅ model:ollama:retry · 6.4s · **repaired: retry** | ⏳ refused · 0.7s | ✅ model:groq · 2.9s +30s waiting (2 attempts) |
| attendance rate by department | — | ✅ model:ollama · 3.6s | ⏳ refused · 0.9s | ✅ model:groq · 3.1s +26s waiting (2 attempts) |
| WFH rate per employee, top 5 | — | ✅ model:ollama · 3.8s | ⏳ refused · 0.6s | ✅ model:groq · 3.5s +24s waiting (2 attempts) |
| what share of total bonus went to Engineering | — | ✅ model:ollama · 3.5s | ⏳ refused · 0.8s | ❌ refused · 2.5s · refused: 'payroll' has no column 'department'. Did you mean  |
| how many employees have more than 75% attendance | — | ✅ model:ollama · 4.0s | ⏳ refused · 0.6s | ✅ model:groq · 3.5s +28s waiting (2 attempts) |
| percentage of leave days by department | — | ✅ model:ollama · 3.6s | ⏳ refused · 0.5s | ✅ model:groq · 2.5s +27s waiting (2 attempts) |

## Summary

| Provider | Pass | Wrong numbers | Rate-limited / unavailable | Plans needing repair | Median inference | Total time waiting on retries |
|---|---|---|---|---|---|---|
| offline | 14/14 | 0 | 0 | 0/8 | 0.0s | — |
| ollama · qwen3.5:4b | 28/28 | 0 | 0 | 3/22 | 3.6s | — |
| groq · qwen/qwen3.8-27b | 7/7 | 0 | 21 | 0/1 | 1.8s | — |
| groq · openai/gpt-oss-120b | 27/28 | 0 | 0 | 1/22 | 3.1s | 386.0s |

## Repairs by type

What the validation/intent layer had to fix in the model's plan, per provider. `sort` = ranking question with an empty sort key · `limit` = top-N with limit 0 · `bucket` = trend question with no time bucket · `sort-key` = near-miss alias · `table` = column on the wrong table · `column` = misspelt column · `retry` = plan fed back to the model once · `arith->ask` = arithmetic in a column field, turned into a substitute question · `pay->disclosed` = 'paid/pay' read as one money column; the result says which columns it excludes.

| Provider | sort | limit | bucket | sort-key | table | column | retry | arith->ask | pay->disclosed | any |
|---|---|---|---|---|---|---|---|---|---|---|
| offline | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0/8 |
| ollama · qwen3.5:4b | 0 | 0 | 1 | 0 | 0 | 0 | 1 | 1 | 0 | 3/22 |
| groq · qwen/qwen3.8-27b | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0/1 |
| groq · openai/gpt-oss-120b | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1/22 |