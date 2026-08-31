# NSEBOT Code Audit — Implementation Plan

## Goal
Produce a comprehensive, line-by-line audit of every `.py` file in `C:\Users\manve\VibeProjects\NSEBOT`, focused exclusively on **functional logic errors, technical bugs, edge cases, and potential runtime failures**. Deliver the findings as a single structured Markdown report.

## Resolved Decisions (do not re-litigate)
1. **Codebase:** `C:\Users\manve\VibeProjects\NSEBOT` (the live, actively-edited copy in this workspace).
2. **File scope:** Python (`.py`) files only. Config (`.json`, `.toml`), SQL migrations embedded in `src/models/schema.py`, HTML/CSS/JS, shell wrappers, and `.md` docs are out of scope.
3. **Depth:** Read every line of every in-scope file. Report only real defects (logic errors, bugs, edge cases, runtime failures). Skip blank lines, imports-only sections, and pure style/cosmetic items.
4. **Report destination:** `C:\Users\manve\VibeProjects\NSEBOT\NSEBOT_AUDIT_REPORT.md` (repo root).
5. **Plan file (this file):** `C:\Users\manve\VibeProjects\NSEBOT\.kilo\plans\1787292634580-nsebot-code-audit-plan.md`.

## Severity Taxonomy (use these exactly in the report)
- **Critical** — Crash, data loss/corruption, or financial misbehavior in normal operation.
- **High** — Failure under realistic conditions (concurrency, empty inputs, partial network failure, market hours, expiry rollover, near-zero prices).
- **Medium** — Edge case / race / silent fallback that can produce incorrect results or expensive retries.
- **Low** — Minor logic quirk, defensive-coding gap, or subtle behavior that could trip a future change.
- **Info** — Observation worth noting but not a defect (dead code, suspicious pattern, TODO adjacent to risk).

## Required Report Structure
The report at `NSEBOT_AUDIT_REPORT.md` MUST contain these sections, in this order:

1. **Header**
   - Generated date (UTC ISO 8601)
   - Audited path: `C:\Users\manve\VibeProjects\NSEBOT`
   - Scope: `*.py` (N files, M total lines — count both)
   - Severity legend (the 5 levels above)
2. **Executive Summary**
   - Counts: total findings, count per severity.
   - Top 5 most impactful findings (one bullet each, file:line + one-sentence impact).
3. **Findings Summary Table** — sorted by severity (Critical first), then by file path, then by line number. Columns: `#` | `Severity` | `File` | `Line` | `Short Title` | `Category`.
4. **Per-File Detailed Findings** — one subsection per `.py` file, ordered by relative path. For each finding:
   - Header: `### <File>:<line>` with severity badge
   - **Observed:** exact code excerpt (5–15 lines) showing the issue
   - **Defect:** plain-language description of what is wrong
   - **Impact:** what runtime behavior this produces (be concrete: e.g. "raises `KeyError` when `intel` lacks `confidence`")
   - **Repro / Edge case:** minimal trigger (input shape, threading, time-of-day, etc.)
   - **Suggested fix:** concrete code change (do not implement it; just describe)
5. **Cross-Cutting Concerns** — synthesis of themes that recur across files:
   - Concurrency / shared state
   - Error handling / exception swallowing
   - Numeric / timestamp / time-of-day handling
   - Configuration / runtime-config coupling
   - Logging / observability
   - Database / migration safety
6. **Out of Scope** — explicit list of what was *not* audited (configs, SQL, frontend, tests, docs, performance, security beyond functional correctness).

## Category Labels (assign one per finding)
`Concurrency` · `Error-Handling` · `Numeric/Time` · `State` · `API-Network` · `DB/SQL` · `Logic` · `Edge-Case` · `Resource-Leak` · `Config` · `Observability` · `Type-Safety` · `Other`.

## Execution Order (ordered task list for the implementation agent)

1. **Enumerate scope.**
   - Glob `C:\Users\manve\VibeProjects\NSEBOT/**/*.py`.
   - Record total file count and total line count.
   - Build a stable relative-path list (use forward slashes; sort alphabetically for determinism).
   - Save the inventory to a working note (not a permanent file) for the report header.

2. **Per-file line-by-line review.** For each `.py` file in the inventory, in order:
   a. Read the file end-to-end.
   b. While reading, flag every site that matches the report criteria (real defect, not style).
   c. For each flag, capture: file:line, severity (use the taxonomy), category, observed excerpt, defect description, impact, repro, suggested fix.
   d. Pay special attention to the high-blast-radius modules (these are where the prior session's bugs lived and where audits historically pay off most):
      - `src/engine/llm_enrichment.py`
      - `src/engine/decision_pipeline.py`
      - `src/engine/trade_decision.py`
      - `src/engine/paper_trading.py`
      - `src/engine/live_trading.py`
      - `src/engine/risk_engine.py`
      - `src/engine/main.py`
      - `src/engine/pipeline.py`
      - `src/engine/pipeline_concurrency.py`
      - `src/models/schema.py` (migrations only — Python correctness, not SQL semantics)
      - `src/fetchers/*.py` (network I/O, retries, timeouts, JSON parsing)
      - `src/scheduler/job_runner.py`
      - `src/ops_agent.py`
      - `dashboard_server.py`
      - `config/settings.py`
      - `config/runtime_config.py`
   e. Do not modify any source file.

3. **Cross-cutting synthesis.** After all files are reviewed, group findings into the cross-cutting themes (Concurrency, Error-Handling, Numeric/Time, State, API-Network, DB/SQL, Config, Observability, Type-Safety). Identify themes that appear in 2+ files.

4. **Write the report.** Write to `C:\Users\manve\VibeProjects\NSEBOT\NSEBOT_AUDIT_REPORT.md` (UTF-8). Follow the required structure exactly. Use stable markdown anchors (e.g. `## Per-File Detailed Findings` and per-file `### <path>` headers) so they are linkable.

5. **Self-check before delivery.** Verify:
   - Every row in the summary table has a matching detail section.
   - Every detail section's `File:Line` actually exists in the audited file.
   - Severity counts in the executive summary match the table.
   - The "Out of Scope" section explicitly mentions configs, SQL semantics, frontend, tests, docs, performance, security (beyond functional).
   - The report is plain Markdown (no HTML beyond what GitHub renders, no binary content).

## Output Constraints
- **One file only:** `C:\Users\manve\VibeProjects\NSEBOT\NSEBOT_AUDIT_REPORT.md`.
- Do not create any other files (no per-file notes, no patch files).
- Do not modify any source file.
- Do not run the application, do not invoke the broker or LLM providers, do not run tests.
- If a finding cannot be reproduced deterministically, mark its severity at most **Medium** and note "static analysis only — no live repro."

## Definition of Done
- `NSEBOT_AUDIT_REPORT.md` exists at the repo root.
- All six required sections are present and non-empty.
- Summary-table row count equals detail-section count.
- Every finding cites a real `file:line` that resolves in the audited tree.
- Executive summary severity counts match the table.

## Risks & Caveats
- This is a **static** audit. It will not catch bugs that only manifest under live market data, broker API quirks, or specific timing windows. Where a finding depends on runtime state, the report must say so.
- The VibeProjects tree was edited earlier in this session (LLM denylist, thread-local counters, symbol-tagged log adapter). Findings must reflect the **current on-disk state** at audit time, not the pre-edit state.
- The "line-by-line" mandate is a reading posture, not a finding quota. A small, correct module is allowed to produce zero findings.
- The implementing agent is an implementation-capable agent. This plan does not require source edits, but it does require file write to the report path (which is allowed under the plan's deliverable).

## Open Questions
None. All material decisions resolved.
