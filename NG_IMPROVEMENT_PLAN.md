# NATURALGAS Improvement Plan — Developer Specification

**Version:** 1.2 (Phase 5 Weather Intelligence; §1.2 per-leg data sources — Shoonya primary)
**Scope:** MCX NATURALGAS only. No behavior change for any other symbol.
**Mode:** Paper first. Live only after §10 gates pass.
**Instrument:** FUT only. Options paths disabled for NG in all new modules.

---

## 0. Architecture Summary

NG trades in two regimes per day, switched by clock:

| Regime | IST Window | Driver | Strategy |
|---|---|---|---|
| `PARITY` | 09:00–17:30 | NYMEX closed/thin; MCX drifts around fair value | Mean-reversion to parity |
| `MOMENTUM` | 18:00–23:00 | Live NYMEX price discovery | Trend-follow live NYMEX |
| `EVENT` | Thu 19:45–21:30 | EIA storage report 20:00 | Surprise-direction play |
| `BLOCKED` | All other times + CME holidays + weekends + expiry week | — | No entries; force-flat rules |

New pipeline flow for NG:

```
scan → parity_engine (FV, dev_pct) → ng_session_router (regime)
     → regime-specific decision module → existing trade_decision gates
     → paper/live execution (FUT only)
```

Existing OI verdict for NG: **weight ~0 in PARITY regime, advisory-only in MOMENTUM** (thin OI, no local price discovery — verdicts follow noise).

---

## 1. Phase 1 — Foundations (build first; de-risks everything else)

### 1.1 `config/cme_holidays.py` — NEW

Sibling of `config/holidays.py`.

```python
# 2026 CME/NYMEX full-closure + early-close dates (energy complex)
CME_HOLIDAYS_2026: set[str] = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03",
    "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
    "2026-11-26", "2026-12-25",
}
CME_EARLY_CLOSE_2026: set[str] = {
    "2026-11-27", "2026-12-24",  # energy floor early close
}

def is_cme_closed(d: date) -> bool: ...
def is_cme_early_close(d: date) -> bool: ...
```

**Wire into `time_guards.is_trading_allowed_now()`:**
- `symbol in ("NATURALGAS","NATGAS","CRUDEOIL")` AND `is_cme_closed(today)` → `(False, "CME holiday — no NYMEX price discovery, MCX zombie session")`.
- Early close → block entries after 17:30 IST.

> Fixes a live bug today: bot generates NG signals into dead tape on US holidays.

### 1.2 `src/engine/parity_engine.py` — NEW

Fair value + deviation. **Per-leg data sources** (v1.2 amendment — Shoonya is primary for both Indian legs; yfinance only for the one leg no Indian broker can supply):

| Leg | Primary | Fallback | Latency |
|---|---|---|---|
| `mcx_last` | Shoonya MCX quote (existing auth, real-time) | — (no fallback; stale MCX = invalid) | RT |
| `usdinr` | Shoonya CDS USDINR near-month FUT | yfinance `USDINR=X` (delayed) | RT / delayed |
| `nymex_last` | yfinance `NG=F` — **only free CME source**; reuses `chart_fetcher` plumbing | — | ~1–2 min delayed |

**Hard rule:** calibration (§5) and live trading MUST use identical leg sources. Never mix mid-stream — the CDS future's forward-premium basis is a constant offset that washes out of the deviation distribution only if legs stay consistent.

**Pre-build check (day 1):** confirm CDS segment is enabled on the Shoonya account and USDINR quote call works (separate segment activation with some brokers). If unavailable → yfinance FX fallback is acceptable (intraday FX drift barely widens deviation noise), but the source must be logged per row.

```python
@dataclass
class ParityState:
    nymex_last: float          # NG=F last (USD/mmBtu) — yfinance, delayed
    usdinr: float              # Shoonya CDS FUT primary; yfinance fallback
    fair_value: float          # nymex_last * usdinr  (MCX quotes ₹/mmBtu, 1:1)
    mcx_last: float            # Shoonya MCX real-time tick
    dev_pct: float             # (mcx_last - fair_value) / fair_value * 100
    nymex_age_sec: int         # staleness — the binding constraint (delayed leg)
    fx_age_sec: int
    mcx_age_sec: int
    fx_src: str                # "shoonya_cds" | "yfinance"
    valid: bool                # False if any leg stale > PARITY_MAX_STALENESS_SEC

def get_parity_state(mcx_last: float) -> ParityState: ...
```

Rules:
- Cache yfinance NYMEX quote 60 s (rate-limit safety); Shoonya legs fetched fresh per computation.
- `valid=False` if any leg older than `PARITY_MAX_STALENESS_SEC` (default 300). Invalid parity = PARITY regime entries blocked, never a silent default. NYMEX will be the leg that trips this — expected and by design.
- **Latency ceiling:** the ~1–2 min delayed NYMEX anchor supports 30–90 min mean-reversion holds only. Sub-5-minute reversion plays are structurally unsupported — do not tighten the strategy below the anchor leg's resolution.
- Persist every computation (§1.4) — this is the calibration dataset for §5 threshold.

### 1.3 `src/engine/ng_session_router.py` — NEW

```python
def get_ng_regime(now_ist: datetime) -> tuple[str, str]:
    """Returns (regime, reason). Precedence:
    1. CME holiday / MCX holiday / weekend         → BLOCKED
    2. Front-contract expiry week (§7.3)           → BLOCKED
    3. Thu 19:45–21:30                             → EVENT
    4. 09:00–17:30                                 → PARITY
    5. 18:00–23:00                                 → MOMENTUM
    6. else (09:00-, 17:30–18:00 handoff, 23:00+)  → BLOCKED
    """
```

- Friday: MOMENTUM entries blocked after 21:00 (weekend-flat rule §7.1).
- Called at top of NG branch in `trade_decision`; regime + reason logged on every NG decision (`decision_audit` reason-code pattern).

### 1.4 Schema — `src/models/schema.py`

```sql
CREATE TABLE IF NOT EXISTS ng_parity_log (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,             -- IST ISO
    nymex_last REAL, usdinr REAL, fair_value REAL,
    mcx_last REAL, dev_pct REAL,
    nymex_age_sec INTEGER, fx_age_sec INTEGER, mcx_age_sec INTEGER,
    mcx_src TEXT, fx_src TEXT, nymex_src TEXT,   -- v1.2: per-leg source audit
    regime TEXT,                  -- PARITY/MOMENTUM/EVENT/BLOCKED
    valid INTEGER
);
CREATE TABLE IF NOT EXISTS eia_consensus (
    report_date TEXT PRIMARY KEY, -- Thursday date
    consensus_bcf REAL,           -- expected build(+)/draw(-)
    actual_bcf REAL,              -- filled post-release
    surprise_bcf REAL,
    fetched_at TEXT, source TEXT
);
```

### 1.5 `config/settings.py` additions

```python
# ── NATURALGAS strategy ──
NG_STRATEGY_ENABLED = env_bool("NG_STRATEGY_ENABLED", False)   # master switch, default OFF
NG_FUT_ONLY = True                                             # hard, not configurable
PARITY_MAX_STALENESS_SEC = 300
PARITY_DEV_ENTRY_PCT = 0.45        # placeholder; overwritten by §5 calibration
PARITY_DEV_STOP_MULT = 2.0         # stop = entry deviation × this
PARITY_FORCE_FLAT_IST = "17:30"
MOMENTUM_ENTRY_START_IST = "18:00"
MOMENTUM_NO_ENTRY_AFTER_IST = "23:00"
NG_WEEKEND_FLAT = True             # no NG position past Fri 23:00
EIA_MIN_SURPRISE_BCF = 15
EIA_NO_TRADE_BAND_BCF = 8
EIA_TIME_STOP_IST = "21:30"
NG_MAX_POSITIONS = 1               # one NG position at a time, all regimes
NG_RISK_PCT_PER_TRADE = 0.5        # % of capital
```

**Deliverable P1:** parity + regime + CME guard live in logs for every NG scan; zero trading behavior change (`NG_STRATEGY_ENABLED=False`). Ship this and let `ng_parity_log` accumulate ≥ 10 sessions before Phase 2.

---

## 2. Phase 2 — PARITY strategy (Session A)

### 2.1 `src/engine/ng_parity_strategy.py` — NEW

Entry (all must hold):
1. Regime == PARITY, `ParityState.valid`, `NG_STRATEGY_ENABLED`
2. `abs(dev_pct) >= PARITY_DEV_ENTRY_PCT` (calibrated, §5)
3. Deviation **shrinking or stable** vs previous scan (don't catch an expanding dislocation — that's information, not noise)
4. Existing `time_guards` pass; no open NG position
5. Direction: `dev_pct > 0` → SELL FUT (MCX rich); `dev_pct < 0` → BUY FUT

Exits (priority order, one action per cycle — reuse §12-FRS pattern):
1. **Parity touch:** `abs(dev_pct) <= 0.10` → close (target)
2. **Deviation stop:** `abs(dev_pct) >= entry_dev × PARITY_DEV_STOP_MULT` → close (dislocation is real — news/FX shock)
3. **Parity invalid** (stale feeds) → close, reason `PARITY_FEED_LOST`
4. **Force-flat 17:30 IST** — unconditional; never carry mean-reversion into MOMENTUM
5. Regular premium/underlying SL from `trade_plan.py` as backstop

### 2.2 Monitoring cadence
Parity exits evaluated every **2 min** via the existing `_check_live_exits` timer pattern in `job_runner` (scan cadence is user-selectable and too slow). Paper NG trades join the same 2-min loop.

### 2.3 OI verdict handling
In PARITY regime: NG OI verdict logged, **excluded from decision** (`reason: "NG PARITY mode — OI advisory only"`). Do not let `boost_only` AI mode resurrect an NG trade the parity engine didn't sign.

---

## 3. Phase 3 — EVENT strategy (EIA Thursday)

### 3.1 `src/fetchers/eia_consensus_fetcher.py` — NEW
- Runs Wednesday 20:00 IST (scheduler job): web-fetch consensus storage estimate; write `eia_consensus` row.
- ≥ 2 source fallbacks; on total failure write row with `consensus_bcf=NULL`.

### 3.2 `src/engine/ng_eia_strategy.py` — NEW
- 19:45–20:00: existing EIA guard already blocks entries ✅. **Add:** force-close any open NG position at 19:40 (guard currently blocks entries only — open-position exposure through the print is today's gap).
- 20:00–20:10: fetch actual; `surprise = actual − consensus`.
  - `consensus IS NULL` → no trade, reason `EIA_NO_CONSENSUS`
  - `|surprise| < EIA_NO_TRADE_BAND_BCF` → no trade
  - `|surprise| >= EIA_MIN_SURPRISE_BCF` → enter **with** first move: build > consensus → SELL; draw > consensus → BUY. Entry only if price already moved in surprise direction (confirmation, not anticipation).
- Stop: `0.6 × ATR(5m,14)`; needs 5-min candles from `chart_fetcher` during event window only.
- Exits: +1R → trail to breakeven; hard time-stop flat at `EIA_TIME_STOP_IST`.
- Never fade the print. `SELL`/`BUY` opposite to surprise direction is structurally blocked in this module.

---

## 4. Phase 4 — MOMENTUM strategy (Session B)

Smallest new code — reuse existing machinery, gated:

- Regime == MOMENTUM required for any NG trend entry. Existing 3H/1H chart logic + OI verdict apply **only here** (this is the only window where they mean anything for NG).
- Additional filter: NYMEX (`NG=F`) 1H direction must agree with MCX signal direction — parity engine data reused; disagreement → block, reason `NYMEX_DIVERGENCE`.
- No new entries after 23:00. Monday: first entries from 18:00 only (gap-day rule — Indian morning session post-weekend is gap marking, not information).
- Weekend flat: any NG position open Friday 23:00 → force close (add to 2-min exit loop).

---

## 5. Calibration task (before enabling Phase 2)

Script `scripts/calibrate_parity.py`:
1. Read ≥ 10 sessions of `ng_parity_log` (Phase 1 output).
2. Distribution of `dev_pct` in PARITY hours: report p50/p80/p90/p95 of `|dev|`, mean-reversion half-life, hit-rate of reversion-to-±0.10% within 90 min for entry thresholds ∈ {0.30, 0.40, 0.50, 0.60}.
3. Pick threshold ≈ p85–p90 with reversion hit-rate ≥ 70%; write chosen value + evidence into the script output committed as `docs/parity_calibration_YYYYMMDD.md`.
4. If no threshold clears 70% → **PARITY strategy does not ship.** (Possible outcome; the plan survives on EVENT + MOMENTUM.)

---

## 6. Risk & sizing (all regimes)

- `NG_MAX_POSITIONS = 1`. Hard.
- Size: `floor(capital × NG_RISK_PCT_PER_TRADE% / (stop_distance_₹ × lot_size))` lots; `LOT_SIZES["NATURALGAS"]=1250` already in settings. If broker offers Mini (250), add `NATURALGAS_MINI` lot entry and prefer it while calibrating.
- Daily NG loss cap: 2 consecutive NG stops in one session → NG disabled until next session (`runtime_config` flag, reason logged).
- Existing MCX confidence floor (72) applies only to MOMENTUM (the only OI-informed path).

---

## 7. Hard blocks (enforced in `ng_session_router` / `time_guards`)

1. **Weekend:** no NG position past Friday 23:00; no entries Sat MCX session.
2. **CME holidays:** full NG block (§1.1).
3. **Expiry week:** no entries in front-contract expiry week; existing FIX #15 contract-ID rollover note applies — roll signal generation to next month when front DTE ≤ 5.
4. **Options:** any NG options order in this strategy mode → hard exception, not a fallback. (Existing MCX liquidity check becomes unconditional FUT for NG.)
5. **17:30–18:00 handoff:** flat, no entries.

---

## 8. Files touched — summary

| File | Change |
|---|---|
| `config/cme_holidays.py` | NEW |
| `config/settings.py` | +NG block (§1.5) |
| `src/engine/parity_engine.py` | NEW |
| `src/engine/ng_session_router.py` | NEW |
| `src/engine/ng_parity_strategy.py` | NEW (Phase 2) |
| `src/engine/ng_eia_strategy.py` | NEW (Phase 3) |
| `src/fetchers/eia_consensus_fetcher.py` | NEW (Phase 3) |
| `src/fetchers/weather_fetcher.py` | NEW (Phase 5) |
| `src/engine/time_guards.py` | +CME guard; +19:40 force-close hook |
| `src/engine/trade_decision.py` | NG branch → regime router; OI weight rules |
| `src/scheduler/job_runner.py` | 2-min NG exit loop; Wed consensus job |
| `src/models/schema.py` | +2 tables (§1.4) |
| `src/engine/pipeline.py` | attach `ParityState` to NG scan context |
| `src/alerts/digest.py` | NG digest: regime, dev_pct, FV line |
| `scripts/calibrate_parity.py` | NEW |

---

## 9. Tests

`tests/test_ng_strategy.py`:
- Regime router: every IST hour × weekday × CME-holiday matrix → expected regime (table-driven).
- Parity: stale feed → `valid=False` → entry blocked; dev sign → trade direction; force-flat 17:30 fires.
- EIA: NULL consensus → no trade; surprise 10 Bcf → no trade; +20 Bcf build → SELL only; fade direction structurally impossible; 19:40 force-close of open position.
- Momentum: NYMEX divergence blocks; Friday 21:00+ entry blocked; weekend force-flat.
- FUT-only: options order attempt raises.
- Sizing: lot math vs stop distance.

---

## 10. Go-live gates (paper → live), per phase independently

| Gate | Threshold |
|---|---|
| Sessions in paper | ≥ 40 (PARITY), ≥ 8 events (EIA), ≥ 30 (MOMENTUM) |
| Profit factor, cost-adjusted | ≥ 1.3 |
| Max single-session loss | ≤ 2 × avg session P&L stdev |
| Force-flat rules | 0 violations in logs |
| Gate parity | Validation run with `PAPER_RESEARCH_MODE=false`, live-equivalent thresholds |
| Feed reliability | Parity `valid` uptime ≥ 95% during PARITY hours |

Phases go live independently — EVENT can ship while PARITY is still calibrating.

---

## 11. Phase 5 — Weather Intelligence (US demand forecast revisions)

NG reprices on **run-to-run revisions** of US weather models (GFS/ECMWF), not forecast levels. This phase converts free model data into a population-weighted degree-day revision signal that gates/boosts MOMENTUM entries and guards PARITY fades.

**Source policy:** raw model data, not X handles. Weather-trader accounts summarize public model runs with latency; X API is paid/write-only on free tier and scraping violates ToS — breaches the zero-paid-API constraint. Go upstream.

### 11.1 Sources (all free, zero-auth)

| Priority | Source | Endpoint | Role |
|---|---|---|---|
| 1 | Open-Meteo | `api.open-meteo.com/v1/forecast` (GFS + ECMWF wrapped, 16-day daily, JSON, no key) | Primary trigger data |
| 2 | NOAA NWS | `api.weather.gov` | Fallback (same fallback-chain pattern as LLM stack) |
| 3 | NOAA CPC 6-10/8-14d outlooks | `cpc.ncep.noaa.gov` (daily ~01:30 IST) | Slow regime label, not a trigger |
| 4 | NHC | `nhc.noaa.gov` JSON/RSS | Jun–Nov Gulf storm flag only |

**NHC polarity note:** modern Gulf hurricanes threaten LNG export terminals (Freeport, Sabine Pass) → demand destruction → typically **bearish** NG. Encode as `gulf_storm_active` flag feeding LLM context; never auto-direction.

### 11.2 `src/fetchers/weather_fetcher.py` — NEW

One batched Open-Meteo call per run, ~10 demand-weighted cities:

```python
CITIES = {  # (lat, lon, weight ≈ US gas heating-demand share)
    "Chicago":      (41.88, -87.63, 0.14),
    "NewYork":      (40.71, -74.01, 0.13),
    "Boston":       (42.36, -71.06, 0.07),
    "Philadelphia": (39.95, -75.17, 0.07),
    "Detroit":      (42.33, -83.05, 0.06),
    "Minneapolis":  (44.98, -93.27, 0.06),
    "Columbus":     (39.96, -83.00, 0.05),
    "DC":           (38.90, -77.04, 0.05),
    "Dallas":       (32.78, -96.80, 0.05),
    "Atlanta":      (33.75, -84.39, 0.04),
}

# Per city/day (°F): HDD = max(0, 65 − (tmax+tmin)/2); CDD = max(0, (tmax+tmin)/2 − 65)
# run_hdd_15d = Σ days1–15 Σ cities (HDD × weight); same for CDD
```

- Timeout 10 s, retry ×2, then fallback source; total failure → write row with `valid=0`, no signal (honest null, never a stale default).
- Cache last good run in `runtime_config` for fetch-gap tolerance.

### 11.3 Fetch schedule (IST) — `job_runner` jobs

| IST | Model run captured | Relevance |
|---|---|---|
| ~10:00 | GFS 00z | Sets Session A tone |
| ~16:00 | GFS 06z + ECMWF 00z | Pre-Session-B positioning |
| ~22:00 | GFS 12z | Lands mid-MOMENTUM — the live one |

### 11.4 Schema

```sql
CREATE TABLE IF NOT EXISTS ng_weather_runs (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,            -- IST ISO
    source TEXT,                 -- open-meteo-gfs / open-meteo-ecmwf / nws
    hdd_15d REAL, cdd_15d REAL,
    delta_hdd REAL, delta_cdd REAL,   -- vs previous valid run, same source
    zscore REAL,                 -- revision z vs trailing 30 runs (seasonal-aware)
    gulf_storm_active INTEGER DEFAULT 0,
    valid INTEGER
);
```

### 11.5 Signal definition

Raw DD deltas are meaningless without seasonal context (3 HDD is huge in April, noise in January) → z-score revisions against the trailing 30-run distribution:

- **Winter (Nov–Mar):** HDD revision; `z ≥ +1.5` → bullish bias, `z ≤ −1.5` → bearish
- **Summer (Jun–Sep):** CDD revision (power-burn demand), same thresholds
- **Shoulder (Apr–May, Oct):** weight → 0; no weather signal
- `|z| < 1.5` → no signal — expected on most days

### 11.6 Integration

| Consumer | Rule |
|---|---|
| MOMENTUM (§4) | Entry **against** a fresh `|z| ≥ 1.5` revision (< 4 h old) → blocked, reason `WEATHER_DIVERGENCE`. Entry **with** it → confidence +5 (capped by existing floors). |
| PARITY (§2) | Fresh `|z| ≥ 2.0` within last 30 min → parity entries disabled 60 min, reason `WEATHER_REPRICING` (real information arriving; don't fade it). Otherwise no role. |
| EVENT (§3) | No role. Storage day stays isolated. |
| LLM context (NG news-only routing) | Inject one line: `15d pop-wtd HDD 142 | rev +6.2 | z +1.8 | GFS 12z | storm: none`. |
| Digest | Same line in NG Telegram block when `|z| ≥ 1.5`. |

### 11.7 Settings

```python
WEATHER_SIGNAL_ENABLED = env_bool("WEATHER_SIGNAL_ENABLED", False)
WEATHER_Z_SIGNAL = 1.5
WEATHER_Z_PARITY_GUARD = 2.0
WEATHER_SIGNAL_MAX_AGE_H = 4
WEATHER_PARITY_LOCKOUT_MIN = 60
```

### 11.8 Tests — extend `tests/test_ng_strategy.py`

- DD math: known tmax/tmin fixtures → exact HDD/CDD; weight sum = 1.0 assertion.
- z-score: seasonal fixtures — identical raw delta → different z in Jan vs Apr.
- Fallback chain: primary timeout → NWS; both fail → `valid=0`, no signal, MOMENTUM unaffected.
- Gates: z=+1.8 + SHORT momentum entry → blocked; z=+1.8 + LONG → +5 confidence; stale run (>4 h) → no effect.
- Parity lockout: z=2.3 revision → parity blocked exactly 60 min.

### 11.9 Calibration gate (same pattern as §5)

Run ≥ 30 days with `WEATHER_SIGNAL_ENABLED=False` (log-only). Then measure: NG price direction in the 4 h following `|z| ≥ 1.5` revisions during MOMENTUM hours. Directional agreement ≥ 60% → enable; else re-tune thresholds or keep as LLM-context-only. Findings → `docs/weather_calibration_YYYYMMDD.md`.

---

## 12. Build order & effort

| # | Item | Est. |
|---|---|---|
| 1 | CME holidays + time-guard wire (bug fix today) | 0.5 d |
| 2 | Parity engine + schema + logging (no trading) | 1 d |
| 3 | Session router + trade_decision gating | 1 d |
| 4 | 2-min NG exit loop + force-flat rules | 0.5 d |
| 5 | 10 sessions data accumulation | wait |
| 6 | Calibration script + threshold decision | 0.5 d |
| 7 | PARITY strategy + tests | 1.5 d |
| 8 | EIA consensus fetcher + EVENT strategy + tests | 1.5 d |
| 9 | MOMENTUM gating + NYMEX-divergence filter | 1 d |
| 10 | Digest/dashboard surfacing | 0.5 d |
| 11 | Weather fetcher + DD/z math + schema (log-only) | 1 d |
| 12 | Weather scheduler jobs + MOMENTUM/PARITY gates + tests | 1 d |
| 13 | 30-day weather log accumulation | wait |
| 14 | Weather calibration + enable decision | 0.5 d |

~10.5 dev-days + calibration waits. Items 1–4 are pure risk reduction and ship with `NG_STRATEGY_ENABLED=False`; items 11–12 ship log-only with `WEATHER_SIGNAL_ENABLED=False`.
