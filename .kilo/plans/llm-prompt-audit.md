# LLM Prompt Audit — NSEBOT Financial Analysis Prompts
**Auditor lens:** 20+ year Indian equity/commodity/options trader  
**Scope:** `src/engine/llm_enrichment.py`, `src/engine/multileg_llm_prompt.py`, `src/engine/scan_sentinel.py`  
**Deliverable:** Critique + refined prompt text per prompt, ready to apply.

---

## 1. Entry Advisor — `_build_deep_prompt` (`llm_enrichment.py:851`)

### What's good
- Hard R:R arithmetic guard with explicit 1:1.2 floor.
- 3H breakout-only entry discipline with 1H explicitly excluded.
- DTE ≤1 no-trade rule (theta crush).
- DATA-LEGITIMACY section prevents blind hallucination.

### Indian-context gaps
- **No India VIX / regime calibration.** A 40% IV on NATURALGAS is cheap; 40% on BANKNIFTY expiry day is normal. Confidence rubric should weight IV rank vs. historical range, not just raw levels.
- **No lot-size / margin context.** LLM recommends a strike without knowing if margin is available or if the trade size is meaningful relative to lot size (NIFTY=50, BANKNIFTY=15, NATURALGAS=1250). Risk/Reward computed on per-premium basis ignores lot-multiplied P&L.
- **News is flat-scored.** A +3 article about US rig counts matters less than a +3 about RBI MPC. The prompt should weight news by source relevance + recency (EIA 8PM IST on Thursday should dominate NATURALGAS).
- **No event-day gamma awareness.** EIA Thursday, OPEC+ Wednesday, US CPI Friday — the prompt mentions these as catalysts but does not penalize naked short options into these events.
- **Chart "sentiment" is OHLC-only.** No RSI/MACD/ATR context, no VWAP, no India open-range (09:15–09:30 ORB). The LLM gets binary BULLISH/BEARISH/NEUTRAL tags that may mislead on chop.

### Recommended refinements
Add after `DATA:` block:

```
INDIA REGIME CONTEXT:
• India VIX: {india_vix:.2f} (regime: LOW<12 | NORMAL 12-18 | ELEVATED 18-25 | FEAR>25)
• {symbol} lot size: {lot_size} units/lot | Margin per lot ≈ ₹{margin_per_lot:,.0f}
• Session micro: {session_phase} (Pre-open 09:00-09:15 | Opening 09:15-10:00 | Midday 10:00-13:30 | Closing 13:30-15:30 | Post-close 15:30-16:00)
• FII/DII trend: {fii_net} (last 5 sessions)
• Event risk today: {event_risk_today} (EIA Thu8PM | OPEC+ Wed | US CPI Fri | None)

NEWS WEIGHTING:
• Headlines from economic-policy sources (RBI, MoPNG, EIA) weight 3x vs. analyst commentary.
• Recency: same-day news overrides stale directional bias. If last 2h news is neutral, treat as NO_NEWS not BULLISH/BEARISH.
```

Tighten TRADE DISCIPLINE block:

```
• NAKED SHORT BAN into events: DTE≤2 AND event_day={event_day} → IRON_CONDOR (defined-risk) or NO_TRADE. No naked strangle/straddle.
• Lot check: max_risk_per_lot = (entry − stop_loss) × lot_size. Max single-trade loss ≤ 1.5% of portfolio. NO_TRADE if math violates.
• IV RANK gate: {atm_iv:.1f}% vs 90d range {iv_90d_low:.1f}%–{iv_90d_high:.1f}%. Below 25th percentile → NO_TRADE (no edge for sellers).
```

---

## 2. Exit Advisor — `_build_exit_prompt` (`llm_enrichment.py:965`)

### What's good
- Age tracking ("opened 47m ago") disambiguates holding vs new signal.
- Expiry-hour countdown (<60 min → forced exit).
- Clear action enum: HOLD / TRAIL_SL / CLOSE_EARLY / EXTEND_TARGET.

### Indian-context gaps
- **No IV-crush detection.** On event days (EIA Thursday, expiry Thursday NIFTY), IV collapses post-event. The LLM should check whether it's holding into an event-window where IV-crush will accelerate premium decay and tighten the SL distance unfairly.
- **No bid-ask spread awareness.** On illiquid strikes, the "current premium" may be last-traded, not tradeable. Exit at bid, not mid. This matters especially for MCX commodities where spreads widen after 22:00 IST.
- **No time-of-day exit filter.** Last 30 min before NSE cash close (15:00 IST) or MCX close (23:30 IST) has adverse selection. The prompt should explicitly warn against new exits in this window unless forced by SL breach.

### Recommended refinements
Add after `MARKET:` block:

```
EVENT WINDOW RISK:
• {symbol} next catalyst: {next_catalyst} in {hours_to_event:.1f}h
• IV status: {iv_current:.1f}% vs {iv_7d_avg:.1f}% 7d avg ({iv_regime})
• Post-event behavior: IV typically {'collapses 20-40%' if event_day else 'stable'}
• If holding into event with profit already locked: prefer CLOSE_EARLY over HOLD to avoid gamma/IV whipsaw.

LIQUIDITY CHECK:
• Current quote may be stale. If Bid/Ask spread > 15% of LTP or OI<100, assume exit at BID (not mid).
• Do not propose TRAIL_SL if new SL premium is closer than Bid.

TIME-OF-DAY RULE:
• NSE expiry window 15:00-15:30 IST: only CLOSE or CLOSE_EARLY allowed.
• MCX window 23:00-23:30 IST: only CLOSE or CLOSE_EARLY allowed.
• Outside window: all actions permitted.
```

---

## 3. Multi-Leg Entry — `build_multileg_prompt` (`multileg_llm_prompt.py:253`)

### What's good
- Full chain at ATM±10 strikes.
- IV summary + skew breakdown.
- Explicit anti-hallucination: leg LTP must exist in CHAIN.
- MCX parity & EIA-day discipline.
- Net delta, max loss ≤3× net premium, 30-50% profit target.

### Indian-context gaps
- **No IV surface / term structure.** Weekly expiry has steeper IV smile than monthly. The LLM should compare current ATM IV vs same-day previous-week expiry IV to judge whether to sell weekly vs monthly.
- **No margin optimization hint.** The LLM picks strikes but doesn't know SPAN margin per leg. A strangle might max out margin at ₹600k cap even if delta looks fine. Prompt should include current margin usage.
- **No SEBI surveillance / ban-list awareness.** If symbol is under F&O ban (no new positions), LLM must not emit ENTER. This is checked downstream but should be a prompt-level no-trade trigger.
- **Open books section (`Open:`) is verbose.** If 20 books are open, the LLM gets 20× multi-line blocks — token waste + context pollution. The prompt should summarize, not enumerate.

### Recommended refinements
Replace `Open: {_format_open_books(open_books)}` block with:

```
OPEN BOOK SUMMARY: {book_count} books | combined_net_delta={combined_delta:+.2f} | combined_margin_used=₹{combined_margin:,.0f} / ₹{margin_cap:,.0f} cap
Margin headroom: ₹{margin_headroom:,.0f} | Delta headroom: {delta_headroom:.2f}
{'MARGIN WARNING: new position requires full margin check — do not propose legs if estimated margin > headroom' if margin_headroom < 50000 else ''}
```

And add before `TASK:`:

```
CONSTRAINTS (non-negotiable):
• {symbol} F&O ban status: {fo_ban_status} → if BANNED, strategy_type="NO_TRACE", legs=[].
• Weekly vs Monthly expiry: {expiry_type}. Weekly → max 2 legs, tighter 15-20% profit target. Monthly → up to 4 legs, 30-50% target.
• SEBI STT on sell side: ~0.05% on premium for equity options, ~0.125% for commodity. Factor into max_profit estimate if not already netted.
• SPAN margin is dynamic. If estimated margin + existing ₹{combined_margin:,.0f} > ₹600,000 → NO_TRADE.
```

---

## 4. Multi-Leg Exit — `build_multileg_exit_prompt` (`multileg_llm_prompt.py:397`)

### What's good
- P&L as % of max profit clearly displayed.
- Adjustment counter with hard max-3 cap.
- Roll candidates provided for ADJUST path.
- Decision enum enforced.

### Indian-context gaps
- **No max-profit lock discipline.** Indian retail traders tend to let a 40% winner become a loser waiting for 80%. The prompt should force a CLOSE at 50% unless both legs are far OTM with DTE≥7.
- **No event-aware ADJUST gating.** EIA Thursday at 20:00 IST: if book is open through EIA, ADJUST becomes dangerous (gamma + IV crush). Prompt should downgrade ADJUST → HOLD or CLOSE on event days.
- **No NIFTY/BANKNIFTY weekly pin risk.** If underlying is within 0.5% of short strike on expiry Thursday, pin risk is high — close not adjust.

### Recommended refinements
After `EXIT PLAN` block:

```
EXIT DISCIPLINE (Indian context):
• If profit_pct ≥ 50%: CLOSE is preferred. Only ADJUST if both legs delta < 0.10 AND DTE ≥ 5 AND no event in next 24h.
• {symbol} pin risk: {'HIGH — expiry today, underlying near short strike' if is_expiry_today else 'normal'}.
• EIA/Event window: {'Do NOT ADJUST within 4h of catalyst — close or hold only.' if event_within_4h else ''}
• Weekly index (NIFTY/BANKNIFTY/SENSEX): if underlying within ±0.5% of ANY short strike on expiry day → CLOSE (pin risk).
```

---

## 5. Scan Sentinel — `_run_ai_diagnostic` (`scan_sentinel.py:777`)

### What's good
- Knowledge-base citation enforcement (F2, F131, etc.).
- Anti-fabrication: no error invented without verbatim log evidence.
- Severity proportional to evidence.
- Action taxonomy: SKIP_TRADE / FORCE_RESCAN / PAUSE_SYMBOL / CLEAR_CACHE / ALERT_ONLY.

### Indian-context gaps
- **No market-hours awareness.** A "fetcher failure" at 03:00 IST is expected (NSE closed); same failure at 09:20 IST is critical. Severity should scale by timestamp.
- **No symbol-specific tolerances.** NATURALGAS trades 23.5h/day; a 30s gap is noise. NIFTY 6.5h/day; a 2-min gap at open is meaningful. The sentinel should weight flags by symbol session length.
- **No back-to-back scan failure rule.** One missed scan → retry. Three consecutive misses → PAUSE_SYMBOL. Currently the LLM decides without an arithmetic rule.

### Recommended refinements
Add to DIAGNOSTIC CRITERIA:

```
SEVERITY CALIBRATION:
• {symbol} session: {session_hours}h/day. A {gap_minutes:.0f}-minute gap at {timestamp_ist} is {'NORMAL (off-hours)' if off_hours else 'CRITICAL (live session)'}.
• Consecutive scan failures: {consecutive_failures}/3 → {'PAUSE_SYMBOL' if consecutive_failures >= 3 else 'FORCE_RESCAN' if consecutive_failures >= 1 else 'monitor'}.
• F&O ban-list check: {fo_ban_status} → if BANNED, this scan's NO_TRADE is expected behavior, not a fault.
• Underlying price stale > {stale_threshold_min} min → CLEAR_CACHE (stale data poisoning).
```

---

## 6. Strategy Optimization (EOD Review) — `get_strategy_optimization_advice` (`llm_enrichment.py:4091`)

### What's good
- Minimum sample guard (≥5 trades).
- Confidence threshold logic.
- Decision-mode upgrade/downgrade logic.

### Indian-context gaps
- **No expectancy / Sharpe / drawdown.** Optimizing on raw PnL can increase risk without realizing it. Should suggest position-size changes based on max-drawdown streak.
- **No symbol-specific seasonality.** NIFTY tends to underperform on expiry Thursdays; NATURALGAS has weekly EIA drift. Strategy optimizer should consider day-of-week + symbol interaction.
- **Config bounds too loose.** "max_concurrent_positions 1-5" — 5 concurrent commodity strangles can exceed ₹600k margin. Should suggest config-aware bounds.

### Recommended refinements
Add to TARGET PARAMETERS:

```
RISK METRICS (add before tuning):
• Max drawdown in period: ₹{max_dd:,.0f} | Avg win: ₹{avg_win:,.0f} | Avg loss: ₹{avg_loss:,.0f} | Profit factor: {profit_factor:.2f}
• Win rate by day-of-week: {dow_win_rates}
• Win rate by symbol: {symbol_win_rates}

CONSTRAINTS:
• live_max_concurrent_positions effective cap: ₹600,000 combined margin.
• For MCX (NATURALGAS/CRUDEOIL/GOLD/SILVER): max concurrent positions ≤ {mcx_cap} due to higher per-lot margin.
• If profit_factor < 1.0: recommend reducing max_concurrent_positions by 1 before changing confidence thresholds.
```

---

## 7. Shared System Prompt — `llm_enrichment.py:1570`

### What's good
- JSON-only output enforcement.
- Data-legitimacy first-line guard.
- Schema-anchored arithmetic.

### Indian-context gaps
- **No timezone anchoring.** IST is implicit; prompts reference "{datetime.now(_IST)}" but the LLM doesn't know IST = UTC+5:30. When the model reasons about "today's EIA at 8PM", it may default to US Eastern.
- **No currency discipline.** All amounts are ₹ but the LLM occasionally emits $ or USD in reasoning. Hard rule should be explicit.

### Recommended refinements
Append to system prompt:

```
CONTEXT RULES:
• All times are IST (UTC+5:30). All amounts are INR (₹). All prices are per-unit INR. No $, no USD, no other timezone.
• {symbol} is an Indian exchange instrument (NSE/MCX). Expiry cycles: NIFTY/BANKNIFTY weekly Thursday; NATURALGAS/GOLD/SILVER daily/MCX-standard.
• When reasoning about catalysts: EIA = Thursday 20:00 IST; OPEC+ = Wednesday ~16:00 IST; US CPI/NFP = Friday 20:30 IST.
```

---

## Validation Checklist

| Prompt | Validation Method | Pass Criteria |
|---|---|---|
| Entry advisor | Trigger 20 synthetic scans with known setups; inspect JSON for R:R arithmetic, NO_TRADE logic, and instrument matching | ≥90% pass on R:R math; 0 fabricated strikes |
| Exit advisor | Replay 10 closed trades; verify HOLD/CLOSE/TRAIL_SL decisions respect event-day rule | Event-day ADJUST → downgraded to HOLD/CLOSE |
| Multi-leg entry | Run on 5 liquid + 5 illiquid chains; check strategy_type, leg count, LTP match | Illiquid → NO_TRADE; leg LTP matches chain ±0.1 |
| Multi-leg exit | Open 3 test books at +30%/+50%/−20% P&L; verify CLOSE triggers | +50% → CLOSE; −20% → CLOSE; adj=3/3 → no ADJUST |
| Sentinel | Inject 3 synthetic scan reports with known faults; inspect diagnosis | R5 → ALERT_ONLY not SKIP; no invented tracebacks |
| Strategy optimizer | Feed 30 historical trades with known losing patterns; check suggestions | Pattern-based only; no guess-fill for unsupported params |

---

## Implementation Order

1. **System prompt** — 10 min, highest-leverage (all prompts inherit it).
2. **Entry advisor** — 30 min, highest P&L impact.
3. **Multi-leg entry** — 25 min, token + margin correctness.
4. **Exit advisor** — 20 min, prevents giving back gains.
5. **Multi-leg exit** — 20 min, discipline on event days.
6. **Sentinel** — 15 min, reduces false alarms.
7. **Strategy optimizer** — 15 min, safer config drift.

**Total estimate:** ~2.5 hours prompt-text work; no schema changes required.
