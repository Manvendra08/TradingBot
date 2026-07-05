**Functional Requirement Specification**

*Trend-Following Delta-First Short Strangle Strategy | v1.1 — Amended Baseline*

# 0. Changelog from v1.0

| # | Section | Change | Reason |
|---|---|---|---|
| 1 | §10, FR-021 | Confirmed scale sequence **50% → 30% → 20%** (v1.0 had drifted from original 60/40/20 ask without explanation) | First tranche = weakest evidence (single trend signal). Front-load less risk on least-confirmed entry; back-load more onto confirmed persistence. |
| 2 | §11 | Added explicit precedence: **reduce/close original tested side → re-evaluate combined exposure → then decide open/block on reversal side** | v1.0 left simultaneous "original tested" + "combined cap breached" conditions unordered. Opening before reducing creates a window of dual over-exposure — exactly the risk this section exists to prevent. |
| 3 | §5 | Defined "persisted trend": **same verdict_label in ≥3 of last 5 scans, with the most recent scan agreeing** | v1.0 used "persisted"/"confirmed" throughout without definition. Reuses existing `_format_historical_oi` Counter-based majority logic already in the codebase — no new mechanism needed. |
| 4 | §9 | ATR trailing average window fixed: **10-session trailing average vs current 5-session ATR** | v1.0 didn't specify a window. 10-session catches weekly regime shift (matches weekly EIA/expiry cadence) without firing on single-session noise. |
| 5 | §8/§9 | Resolved DTE-band vs ATR-regime conflict: **ATR regime shift may only tighten by shifting to the next stricter DTE-bucket's delta band — never loosens, never overrides the DTE table in the permissive direction, never skips more than one bucket** | v1.0 had two independent delta-adjustment paths (DTE bucket, ATR regime) with no stated resolution when they'd disagree. |
| 6 | §12 | Added exit-trigger priority order (new — v1.0 had no ordering for simultaneous triggers) | AC-010 requires one action+reason per decision; without priority, competing same-cycle triggers were undefined. |

---

# 1. Document Control

| Attribute | Value |
|---|---|
| Document Name | Functional Requirement Specification — Trend-Following Delta-First Short Strangle |
| Strategy Mode | TREND_FOLLOWING_SHORT_STRANGLE |
| Document Version | 1.1 — Amended Baseline (supersedes 1.0 Consolidated) |
| Intended Use | Paper trading first; live trading only after separate audit and validation |
| Primary Instruments | Indian index options: NIFTY, BANKNIFTY, FINNIFTY, SENSEX; extensible to other liquid symbols |
| Core Position Type | Short option legs only: SELL PE in bullish trend, SELL CE in bearish trend |

# 2. Executive Summary

Trend-following short strangle: sell premium in the direction supported by the underlying move, controlling short-gamma risk through written-strike delta (primary), expiry-aware thresholds, ATR-based spacing/regime adjustment (secondary), and combined book exposure limits. v1.1 resolves five specification gaps and one unexplained parameter drift identified in v1.0 review — see §0.

# 3. Strategy Objective

- Maximize probability of profit by collecting premium on OTM strikes aligned with persisted directional trend.
- Avoid averaging into dangerous short-gamma exposure when a written strike becomes tested.
- Use delta as the primary indicator of breach probability and strike safety.
- Use ATR as a regime, spacing, and emergency adverse-move control — never as primary strike selector, never as a looser override of the DTE-delta table.
- Allow dynamic strangle construction by opening the opposite side on confirmed reversal, subject to combined risk limits and strict reduce-before-open sequencing.
- Preserve paper/live separation and require paper validation before any live execution.

# 4. Scope

## 4.1 In Scope
- Paper-trading implementation of delta-first trend-following short strangle logic.
- SELL PE and SELL CE option-writing decisions only.
- Separate CE-side and PE-side books for each symbol.
- Delta-based strike selection, delta-based stop, premium filter, ATR spacing, DTE tightening, combined risk checks.
- Tranche scaling with exposure caps and anti-overtrading controls.
- Functional monitoring requirements for leg-level and portfolio-level risk.

## 4.2 Out of Scope
- Direct live order placement without separate live-trading audit.
- Broker-specific production order management, slippage control, margin reconciliation beyond specification-level requirements.
- Guaranteed profitability or market prediction.
- Non-options instruments unless explicitly enabled in future versions.

# 5. Definitions

| Term | Definition |
|---|---|
| Trend-supported side | The option side favoured by the underlying trend: PE on bullish trend, CE on bearish trend. |
| **Persisted trend** *(v1.1 — added)* | Same `verdict_label` present in ≥3 of the last 5 scans, AND the most recent scan agrees with that label. Reuses the existing scan-history majority-count mechanism (`_format_historical_oi`); no separate tracking required. |
| **Confirmed reversal** *(v1.1 — added)* | A persisted trend (per above) whose direction is opposite to the direction that justified the currently open side. |
| Tested side | A short option leg whose strike is moving closer to spot and whose delta is rising toward unsafe levels. |
| Untested side | A short option leg whose strike is moving farther from spot or whose premium/delta has decayed favourably. |
| Written-strike delta | Absolute delta of the short option strike; primary measure for strike safety and breach-risk control. |
| Tranche | A controlled partial addition to an existing side book. |
| Side book | Open short PE or short CE legs tracked separately for a symbol. |
| Combined book | The full CE+PE short-premium exposure for a symbol. |
| Re-centering | Rolling or adding a new strike to keep the short-premium structure aligned with current spot, delta, and risk budget. |

# 6. Core Functional Requirements

| ID | Requirement | Priority |
|---|---|---|
| FR-001 | The strategy shall operate in a dedicated mode named TREND_FOLLOWING_SHORT_STRANGLE. | Must |
| FR-002 | The strategy shall create only short option positions: SELL PE or SELL CE. | Must |
| FR-003 | Bullish persisted trend (§5 definition) shall map to SELL PE candidate evaluation. | Must |
| FR-004 | Bearish persisted trend (§5 definition) shall map to SELL CE candidate evaluation. | Must |
| FR-005 | The system shall maintain separate PE-side and CE-side books for each symbol. | Must |
| FR-006 | The system shall maintain a combined strangle book for symbol-level exposure assessment. | Must |
| FR-007 | The system shall use written-strike delta as the primary strike-selection variable. | Must |
| FR-008 | The system shall use ATR only as secondary regime, spacing, and emergency-stop input, subject to the one-way tightening constraint in §9. | Must |
| FR-009 | The system shall block same-strike averaging unless explicitly enabled for research. | Must |
| FR-010 | The system shall prevent additions where candidate delta is worse than the existing side risk profile. | Must |
| FR-011 | The system shall tighten delta thresholds as DTE reduces, per the table in §8. | Must |
| FR-012 | The system shall apply premium, liquidity, and OTM-distance filters after delta qualification. | Must |
| FR-013 | The system shall allow reversal-side entries only if combined book risk remains within configured limits, evaluated per the sequencing in §11. | Must |
| FR-014 | The system shall reduce or block expansion when the original tested side exceeds risk thresholds, per the precedence in §11. | Must |
| FR-015 | The system shall provide auditable reasons for every OPEN, ADD, ROLL, BLOCK, EXIT, or REDUCE decision, including which trigger/rule fired when multiple were eligible. | Must |

# 7. Signal-to-Action Mapping

| Signal / Verdict Class | Trend Interpretation | Candidate Action | Notes |
|---|---|---|---|
| Long Buildup | Bullish | Evaluate SELL PE | Proceed only if delta, premium, ATR spacing, and combined risk filters pass. |
| Put Writing | Bullish | Evaluate SELL PE | High-quality signal if PE OI supports the move and strike delta remains safe. |
| Short Covering | Weak/fast bullish | Evaluate SELL PE cautiously | Require stronger premium and delta safety — move may be exhaustion-led. |
| OI Bias Bullish | Bullish | Evaluate SELL PE | Treat as directional support; still delta-first. |
| Short Buildup | Bearish | Evaluate SELL CE | Proceed only if delta, premium, ATR spacing, and combined risk filters pass. |
| Call Writing | Bearish | Evaluate SELL CE | High-quality signal if CE OI supports the move and strike delta remains safe. |
| Long Unwinding | Weak/fast bearish | Evaluate SELL CE cautiously | Require stronger premium and delta safety — move may be exhaustion-led. |
| OI Bias Bearish | Bearish | Evaluate SELL CE | Treat as directional support; still delta-first. |

All "persisted"/"confirmed" language in this table refers to the §5 definition — ≥3 of last 5 scans agreeing, most recent included.

# 8. Delta-First Strike Selection Requirements

| ID | Requirement |
|---|---|
| FR-016 | The system shall reject candidate strikes outside the configured DTE-aware entry delta band. |
| FR-017 | The system shall prefer candidates closest to target delta midpoint after passing safety filters. |
| FR-018 | The system shall reject candidate strikes where delta is unavailable unless a research fallback is explicitly enabled. |
| FR-019 | The system shall not select strikes using fixed point-distance as the primary method. |
| FR-020 | The system may use fixed distance or ATR buffer only as a secondary safety filter. |

| DTE Bucket | Entry Delta Band | Add/Recenter Delta Band | Hard Stop Delta |
|---|---|---|---|
| 4–7 DTE | 0.16–0.20 | 0.12–0.18 | 0.35–0.40 |
| 2–3 DTE | 0.12–0.16 | 0.10–0.14 | 0.30–0.35 |
| 1 DTE | 0.08–0.12 | 0.07–0.10 | 0.25–0.30 |
| 0 DTE | No new entry by default | No new add by default | Very tight / forced risk-off |

**v1.1 addition:** this table is the *only* source of the permissive (looser) bound on delta band selection. §9's ATR regime rule may shift selection to a **tighter** band (i.e., toward the next row down) but may never select a band looser than what the current DTE bucket allows, and may not skip more than one bucket-width tighter in a single adjustment.

# 9. ATR Usage Requirements

| ATR Use Case | Requirement |
|---|---|
| Regime adjustment | **(v1.1 — window specified)** If current 5-session ATR exceeds its own 10-session trailing average, the system shall shift to the next stricter DTE-bucket's delta band (§8), one bucket-width only, never looser, never bypassing the DTE table. |
| Tranche spacing | The system shall require spot movement since last placement to exceed a configured ATR multiple before re-centering or adding. |
| Emergency adverse stop | The system shall exit or reduce when spot moves adversely by a configured ATR multiple against the short leg. |
| Noise control | The system shall block rolls/additions that are within normal ATR noise (i.e., movement below the tranche-spacing ATR multiple). |
| Not primary selector | ATR shall not replace delta as the primary strike-selection variable, and shall not independently loosen a delta band set by the DTE table. |

# 10. Scaling and Re-centering Requirements

| ID | Requirement |
|---|---|
| FR-021 | **(v1.1 — confirmed, see §0 changelog)** The default scale sequence shall be **50% → 30% → 20%** of preset side size, in that order, one tranche per qualifying persisted-trend confirmation (not per scan). |
| FR-022 | Total side exposure shall not exceed 100% of preset side size unless research override is enabled. |
| FR-023 | The system shall enforce minimum time gap between tranche decisions. |
| FR-024 | The system shall enforce minimum ATR/spot movement gap between tranche decisions (per §9 tranche spacing). |
| FR-025 | The system shall prohibit adding to the same strike by default. |
| FR-026 | The system shall prohibit adding to a higher-delta/worsening-risk strike. |
| FR-027 | The system shall allow rolling or moving the untested side closer only when combined risk remains acceptable. |
| FR-028 | The system shall require existing tested-side MTM to be non-negative or within configured drawdown tolerance before additional tested-side exposure. |

# 11. Reversal Handling Requirements

A reversal does not automatically close the original side. Opening the opposite side is conditional, not automatic, and follows the **mandatory sequencing** below.

**v1.1 — added mandatory decision sequence** (resolves v1.0 ambiguity when multiple conditions are true simultaneously):

1. **Check original tested side first.** If the original side is tested beyond its risk threshold, reduce or close it. Do this before any evaluation of the reversal-side entry.
2. **Re-evaluate combined exposure** using the post-reduction state of the original side.
3. **Then, and only then, decide** whether to open/scale the reversal side, based on the re-evaluated combined exposure against configured caps.

This ordering is mandatory: the system must never open or scale the reversal side first and reduce the original side afterward, as that sequence creates a window of simultaneous full exposure on both legs.

| Scenario | Expected Behaviour |
|---|---|
| PE short exists; bearish reversal confirmed (§5 definition) | Apply the 3-step sequence above; evaluate SELL CE only after step 2, subject to combined delta, margin, DTE, and tested-side risk remaining within budget. |
| CE short exists; bullish reversal confirmed (§5 definition) | Apply the 3-step sequence above; evaluate SELL PE only after step 2. |
| Original side already tested beyond threshold | Reduce or close original side — this is step 1 above, and executes regardless of reversal-side outcome. |
| Combined exposure would exceed cap (post step-2 re-evaluation) | Block reversal-side addition and log reason. |
| Binary event / high-risk regime | Block or reduce-size reversal-side addition depending on configuration. |

# 12. Exit and Risk Requirements

**v1.1 — added priority order** for same-cycle competing triggers (highest first). When multiple triggers are eligible in the same scan, the highest-priority trigger determines the logged action and reason; lower-priority triggers are recorded as "also eligible" in the audit trail but do not independently execute.

1. **Delta stop** — Primary stop. Exit/reduce if written-leg absolute delta crosses configured DTE-aware threshold.
2. **Margin cap** — Block new entries or reduce exposure when margin usage exceeds budget.
3. **ATR adverse move** — Emergency backstop. Exit/reduce if adverse spot movement exceeds configured ATR multiple.
4. **Combined book drawdown** — Reduce exposure when combined MTM drawdown exceeds configured budget.
5. **Premium SL** — Backup stop. Exit if short option premium reaches configured multiple of entry premium.
6. **DTE cut-off** — Restrict new entries and force risk-off actions near expiry as configured.
7. **Profit decay target** — Book/partial-book when premium decays by configured target percentage. Lowest priority: never allowed to suppress a higher-priority live risk stop firing in the same cycle.

Rationale for ordering: risk-of-ruin triggers (1–4) outrank capital-efficiency triggers (5–7). Profit-taking must never override an active risk stop.

# 13. Non-Functional Requirements

| Category | Requirement |
|---|---|
| Auditability | Every strategy decision shall include reason codes, input metrics, and (v1.1) any lower-priority triggers that were also eligible that cycle. |
| Safety | Paper mode shall be the default deployment state. |
| Configurability | Delta bands, ATR multiples, premium bands, DTE rules, exposure caps, persistence thresholds (§5), and ATR trailing-average window (§9) shall be configurable. |
| Extensibility | The strategy shall be modular and not hard-coded into generic paper trading logic. |
| Observability | Telegram/dashboard output should display side book, candidate delta, premium, risk reason, and action. |
| Data quality | The system shall reject stale or missing delta unless explicit fallback is enabled. |

# 14. Acceptance Criteria

| ID | Acceptance Criteria |
|---|---|
| AC-001 | Bullish signals never generate BUY CE/PE under this strategy mode. |
| AC-002 | Bearish signals never generate BUY CE/PE under this strategy mode. |
| AC-003 | A candidate strike outside configured delta band is rejected. |
| AC-004 | Same-strike add is blocked by default. |
| AC-005 | A worsening-delta add is blocked. |
| AC-006 | A reversal-side entry is blocked if combined exposure exceeds configured cap, evaluated after §11's mandatory reduce-then-evaluate sequence. |
| AC-007 | A short leg exits or reduces when hard delta stop is breached. |
| AC-008 | DTE ≤ 2 applies tighter entry/add/stop delta thresholds. |
| AC-009 | ATR expansion (5-session vs 10-session trailing, §9) results in a one-bucket-tighter delta target, never looser. |
| AC-010 | Every decision returns action, reason, symbol, option side, delta, premium, strike, risk metrics, and any lower-priority triggers also eligible that cycle. |
| **AC-011** *(v1.1 — added)* | A persisted-trend evaluation with fewer than 3 of the last 5 scans agreeing does not qualify as persisted, and does not trigger OPEN/ADD/reversal evaluation. |
| **AC-012** *(v1.1 — added)* | When both "original tested beyond threshold" and "combined exposure would exceed cap" are true in the same cycle, the system executes the reduce-then-evaluate sequence (§11) and never opens the reversal side before reducing the original. |

# 15. Implementation Notes for Developers

- Treat this as the consolidated baseline; do not implement separate V1/V2/v1.1 branches.
- Existing generic option-buying mappings should remain available outside this strategy mode, but must not execute inside TREND_FOLLOWING_SHORT_STRANGLE.
- This v1.1 document supersedes v1.0 where they conflict — specifically §10 scale sequence, §11 reversal sequencing, §9 ATR window/precedence, and §5 persistence definition.
- Persistence logic (§5) should call the existing scan-history majority mechanism already present in the codebase (`_format_historical_oi`'s verdict Counter) rather than a new implementation — avoids duplicate logic and the formatting-drift class of bug already seen elsewhere in this codebase.
- Paper-trading validation should measure: net premium captured, max MTM drawdown, tested-side events, delta-stop frequency, whipsaw loss, cost-adjusted P&L, and (v1.1) frequency of each exit-trigger priority tier firing, to validate the §12 ordering is actually the binding constraint in practice and not just delta stop firing exclusively.
