# What I'd build next

Deliberate cuts, each with the trigger that would justify building it.

- **Schema retrieval (vector store over column metadata).** Trigger: combined schema across uploaded files exceeds ~200 columns and no longer fits the prompt comfortably. Today the full schema goes in the prompt; under the file caps it's ~100-150 lines.
- **MCP server exposing the plan executor.** Trigger: another agent (e.g. Darwinbox's own) needs to query uploaded data. Darwinbox ships an MCP server; the plan/executor split here is already the right shape for a tool.
- **Composite join keys.** Trigger: real customer exports where a single column isn't unique (e.g. `emp_id` + `pay_period`).
- **Free-form code path behind an explicit opt-in.** Trigger: users hit the refusal message often enough on legitimate questions that the plan schema's coverage is the bottleneck. Would run sandboxed, clearly labelled, never silently.
- **Query history / saved questions.** Trigger: multi-session use.
- **Eval harness as a CI step.** The README results table is run by hand today; trigger: any change to the prompt or plan schema.
- **Derived metrics in the plan** (`net pay = base + bonus − deductions`). Ratios and thresholds are done; per-row arithmetic between columns isn't. Trigger: users hit the refusal on these often. Needs a small expression grammar in the plan, validated the same way — not free code.
- **Fan-out handling beyond refusal.** Today a sum that a join would inflate is refused with an explanation. Next: rewrite the plan to pre-aggregate the many-side (sum per employee first, then join) — the correct answer, computed safely.
- **More intent checks.** Today: a ranking question must yield a ranked, cut list. Next: a "trend / over time" question must yield a time-bucketed series; a "compare A and B" must yield both groups; a "per X" must group by X. Each is a few lines after structural validation and catches a silent shape mismatch.
- **Date-like strings at load.** DuckDB's CSV sniffer leaves some date columns as VARCHAR (mixed formats, `dd/mm/yyyy`, text months). Those can't be bucketed, so "training cost by month" refuses because of an ingestion detail the user can't see. Detect date-like strings during schema extraction (sample parse rate > 90%) and offer to parse the column — a one-click, disclosed cast, not a silent one.
