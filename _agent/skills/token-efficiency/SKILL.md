---
name: token-efficiency
description: Use this skill whenever the user mentions token efficiency, reducing costs, compressing prompts, saving tokens, optimizing context windows, or trimming AI responses. Also trigger when users complain about verbose outputs, bloated system prompts, or expensive API usage. Apply proactively in any long-running agentic workflow or multi-turn conversation where context accumulation is a concern.
---

# Token Efficiency Skill

## Core Principle
Fewer tokens = lower cost + faster responses + more context headroom.
Token savings must never sacrifice accuracy, completeness, or usability. The goal is **density, not truncation**.

---

## Input Optimization (Prompts & Context)

### 1. Compress System Prompts
- Remove filler: "Please", "I want you to", "Make sure to", "Your job is to"
- Replace prose instructions with imperative bullets
- Merge redundant rules into one authoritative statement

**Before:** "Please make sure that when you respond to the user, you always try to be as concise as possible and avoid unnecessary verbosity."
**After:** "Be concise. No filler."

### 2. Trim Conversation History
- Summarize resolved turns instead of keeping full transcripts
- Drop intermediate reasoning steps once conclusions are confirmed
- Keep only: current task context + unresolved decisions + key constraints
- Prune user messages that are now irrelevant (e.g., "thanks", "ok got it")

### 3. Reference, Don't Repeat
- If a document is already in context, cite it — don't re-paste it
- Use a short label: "See Schema A above" instead of re-including the schema
- For structured data, send only relevant fields, not full objects

### 4. Compress Few-Shot Examples
- Use 1–2 tight examples, not 5 verbose ones
- Strip all commentary from examples — just input → output pairs
- If the pattern is simple, skip examples entirely; describe the rule instead

### 5. Chunk Long Documents
- Don't inject an entire document if only a section is relevant
- Pre-filter before sending: extract the relevant rows, paragraphs, or fields
- For RAG pipelines: retrieve narrowly, summarize before injecting

---

## Output Optimization (Responses)

### 6. Match Length to Task Complexity

| Task Type | Target Length |
|---|---|
| Yes/no, status check | 1 sentence |
| Simple factual | 1–3 sentences |
| Analysis / recommendation | 3–10 sentences or tight bullets |
| Multi-part deliverable | Use structure, not padding |
| Code | No prose wrapper unless asked |

### 7. Eliminate Structural Bloat
Cut these patterns:
- **Preamble:** "Great question! Let me explain..." → just answer
- **Restatement:** "You asked about X. X is..." → just explain X
- **Postamble:** "I hope this helps! Let me know if you need more." → omit
- **Hedge stacking:** "It's worth noting that, generally speaking, in most cases..." → pick one hedge or none

### 8. Use Dense Formats
- Prefer tables over prose for comparisons
- Prefer bullets over paragraphs for lists
- Prefer code over pseudocode
- Prefer inline citations over footnotes for short docs

### 9. Avoid Echoing
- Don't repeat user input back before answering
- Don't restate context that was just provided
- Exception: confirm only when ambiguity is high and a wrong action is costly

### 10. Compress Code Outputs
- Omit boilerplate unless the user is a beginner
- Skip comments that explain obvious logic
- Don't add example usage unless asked
- Don't wrap snippets in full files if a function suffices

---

## Agentic / Multi-Turn Workflows

### 11. Summarize, Don't Accumulate
At natural checkpoints (end of a phase, after completing a subtask):
- Replace the detailed exchange with a 2–4 line summary
- Store decisions, not deliberations
- Format: `[CHECKPOINT] Task: X. Decision: Y. Status: Done.`

### 12. Compress Tool Results Before Re-Injecting
- API/search results: extract only what's needed for the next step
- File reads: quote the relevant section, not the full file
- DB queries: summarize result set if > ~20 rows

### 13. Avoid Redundant Tool Calls
- Cache results within a session when the same data is needed multiple times
- Before calling a tool, check if the answer is already in context

---

## Measuring Efficiency
Track these signals:
- **Token count per turn** (use API usage metadata)
- **Context fill rate** (tokens used / context window size)
- **Output quality unchanged** — use a small eval set to verify no degradation

A **20–40% token reduction** is achievable on most prompts without quality loss.
If quality drops, roll back the most aggressive compression step first.

---

## Quick Reference: Red Flags (High Token Waste)

| Pattern | Fix |
|---|---|
| Full document injected every turn | Inject once; reference by label after |
| Raw API response in context | Extract relevant fields only |
| 5+ few-shot examples | Reduce to 1–2 |
| Verbose system prompt (>500 tokens) | Rewrite as bullets, cut filler |
| Model echoes user input | Instruct: "No restatements" |
| Long chain-of-thought in output | Use thinking internally; return conclusions only |
| Entire conversation history every call | Summarize resolved turns |
