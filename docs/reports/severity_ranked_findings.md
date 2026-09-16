# Severity-Ranked Findings — Live Trading Safety Audit

## Fixed / Closed

| # | Finding | Severity | Status | Fix |
|---|---------|----------|--------|-----|
| 1 | LLM async default enabled unsafe parallel LLM enrichment in live paths | High | Closed | `data/runtime_config.json` → `llm_enrichment_async: false`; live broker mode forces synchronous LLM verdict in `src/engine/pipeline.py` |
| 2 | Multi-leg undefined-risk SL lacked broker margin awareness and hard rupee ceiling | High | Closed | `src/engine/multileg_live_trading.py` SL logic now prefers broker margin API with ₹25,000/lot hard ceiling |
| 3 | Broker gate shadow-mode exit behavior was ambiguous | High | Closed | `src/engine/broker_gate.py` clarified invariant: exits blocked in shadow mode |
| 4 | JSON repair truncation could silently alter trade execution payloads | Medium | Closed | `src/engine/llm_enrichment.py` adds strict JSON parse mode; truncated-JSON auto-close disabled for trade execution; strict mode flag (`NSEBOT_STRICT_JSON`) added |
| 5 | Provider ladder success path lacked deterministic ordering and audit trail | Medium | Closed | `src/engine/llm_enrichment.py` adds `_PROVIDER_LADDER_ORDER`, deterministic sort key, and `_log_provider_audit` on success |
| 6 | Watchdog used daemon thread with no kill/timeout enforcement | Medium | Closed | `src/scheduler/job_runner.py` upgrades watchdog to `multiprocessing.Process` with `terminate()`/`kill()`; falls back to daemon thread when target function is unpicklable |
| 7 | Missing edge-validation gate allowed live strategy execution without positive expectancy check | Medium | Closed | `src/engine/pipeline.py` adds `verify_positive_expectancy` gate that blocks live strategy execution when broker mode is on |
| 8 | Context divergence risk: option row fetcher could cross-expiry fallback silently | Low | Closed | `_get_option_rows_for_expiry` fails-closed instead of cross-expiry fallback |

## Open

None currently.
