# OPS_AGENT_SPEC — NSEBOT Operations Agent v2.0

**Status:** Proposed
**Principle:** Agency over infrastructure, never over trade selection. Every action is from a pre-approved playbook, bounded, reversible or fail-safe, and logged. The agent keeps the system **alive, flat when it should be flat, and loud when it can't fix something.**
**Companion docs:** ADR-007 (AI role boundaries), NG_IMPROVEMENT_PLAN v1.2 (force-flat rules the agent enforces)

---

## 1. Problem

Remote hosting (Oracle VM migration) introduces the failure mode a laptop never had: **silent death with open positions.** Concrete scenarios this agent exists for:

| Scenario | Today's outcome |
|---|---|
| Shoonya session token expires mid-day | All fetches fail silently until someone reads logs; scans produce fallback data |
| Scheduler process crashes at 19:35 Thu | NG position rides through EIA print — the exact 19:40 force-flat gap, unenforced |
| Parity feed goes stale (yfinance outage) | PARITY entries blocked correctly, but nobody knows the strategy is dark |
| SQLite disk full / DB locked | Scans fail, trades unmonitored, zero notification |
| Telegram API down | Bot healthy but user blind — indistinguishable from bot death |
| VM reclaimed/rebooted by Oracle | Everything down; user finds out at next manual check |

## 2. Architecture

Separate process, separate failure domain. The agent must survive the bot dying — so it never runs inside the bot.

```text
┌────────────────────────── VM ──────────────────────────┐
│  nsebot.service (main.py)      dashboard.service       │
│        │ writes                      │                 │
│        ▼                             ▼                 │
│  health_state table + /health endpoint + heartbeat file│
│        ▲ reads                                         │
│  opsagent.service (ops_agent.py)  ← independent systemd│
│        │                                               │
│        ├─ Tier 0/1: acts directly (playbook)           │
│        ├─ Tier 2: acts + requires ack                  │
│        └─ Escalation: Telegram (primary) → SMS-gw (fb) │
└────────────────────────────────────────────────────────┘
External watchdog: healthchecks.io free ping (agent pings
every 5 min; missed ping = email/push) — watches the watcher.
```

**LLM role inside the agent:** none in the act path. Playbook matching is deterministic (state machine). The LLM may optionally *summarize* incident context in the escalation message (async, after the action) — same language-plane rule as ADR-007. An agent that needs an LLM to decide whether to restart a service will one day hallucinate a reason not to.

## 3. Health signal contract (bot-side changes)

`src/models/schema.py` — new table, written by the bot:

```sql
CREATE TABLE IF NOT EXISTS health_state (
    key TEXT PRIMARY KEY,          -- component name
    status TEXT,                   -- OK | DEGRADED | DOWN
    detail TEXT,
    updated_at TEXT                -- IST ISO
);
```

Components the bot must stamp (each at its natural cadence):

| key | Written by | Stale threshold |
|---|---|---|
| `scheduler_loop` | `job_runner` every loop tick | 3 min |
| `shoonya_session` | router on every fetch (OK/auth-fail) | 10 min in market hours |
| `parity_feed` | `parity_engine` per computation | 10 min in NG hours |
| `telegram_send` | dispatcher per send | on failure only |
| `db_write` | schema helpers | 5 min |
| `last_scan_<SYMBOL>` | pipeline per symbol | 2× scan interval |
| `open_positions` | paper/live monitors (count + oldest unmonitored age) | 5 min when >0 |

Plus: `GET /health` on dashboard (JSON dump of table) and a plain heartbeat file `/tmp/nsebot.heartbeat` touched each loop — three independent read paths so the agent can distinguish "bot dead" from "DB locked" from "dashboard dead."

## 4. Playbook (the whole point — exhaustive, closed list)

Authority tiers:
- **T0 — observe & notify:** no action beyond alerting.
- **T1 — auto-act:** bounded, reversible/fail-safe actions. Act first, notify immediately after.
- **T2 — act toward safety + require ack:** actions that touch positions. Always in the *flattening* direction only — the agent may close/protect, **never open, never widen risk.**

| # | Trigger (deterministic) | Tier | Action | Bound |
|---|---|---|---|---|
| P01 | `scheduler_loop` stale > 3 min AND heartbeat file stale AND `open_positions == 0` | T1 | `systemctl restart nsebot` → verify heartbeat within 90 s | Max 2 restarts / 30 min, then P02 |
| P02 | P01 bound exceeded (crash-loop) OR (bot dead AND `open_positions > 0`) | T2 | Stop restarts; if open positions exist → attempt direct exit via P10 path; escalate CRITICAL | Bot restarts skipped if open pos exist to avoid unprotected market exposure |
| P03 | `shoonya_session` = auth-fail (HTTP 401/403 Invalid Token specifically, NOT 502/Timeout) | T1 | Trigger bot's re-auth hook (`POST /internal/reauth` or restart with reauth flag) → verify one quote fetch | Max 3 / hour; TOTP secrets stay in bot env — agent never holds broker creds |
| P04 | Re-auth failed ×3 OR repeated 502/Timeout (Broker down) | T2 | Set `runtime_config.trading_paused=true` (new entries stop; monitors continue via cached session if alive); escalate CRITICAL | Pause is one-way; only human unpauses |
| P05 | `parity_feed` DOWN in NG hours | T0→T1 | Notify; if bot alive, confirm PARITY entries are blocked — if a PARITY position is open with feed dead > 15 min → P10 it | — |
| P06 | `db_write` failing / disk > 90% | T1 | Rotate logs, vacuum WAL, prune `/tmp`; re-check; if still failing → T2 pause + escalate | Never deletes DB files |
| P07 | `telegram_send` failing > 10 min | T1 | Switch own escalations to fallback channel (plain SMTP or ntfy.sh); notify there that user is blind to bot alerts | — |
| P08 | VM reboot detected (uptime < 10 min) | T1 | Verify both services came up (systemd should have); stamp incident; notify | — |
| P09 | **Force-flat sentinel:** now > any configured force-flat time AND time verified via NTP/external ping AND `open_positions` shows a matching position still open | T2 | Call bot's close endpoint; if bot unresponsive → P10 | External time validation prevents clock-drift induced force-flats |
| P10 | Direct-exit path: bot dead/unresponsive with live position open | T2 | Standalone `emergency_flat.py`: **1) Cancel ALL open/pending orders** to clear zombies. **2) Close all open live positions** at market. Confirm, escalate CRITICAL. | Uses strict isolated API wrapper restricting BUY actions. |
| P11 | `last_scan_<SYM>` stale > 2× interval, everything else OK | T0 | Notify (fetcher-source degradation pattern) | — |
| P12 | Anything not matching P01–P11 | T0 | Notify with raw state dump. **No improvised action, ever.** | — |

P12 is the most important row: the agent's response to novelty is escalation, not creativity.

## 5. Notification contract

- Every T1/T2 action → Telegram within 30 s: `⚙️ OPS | P03 | Shoonya re-auth succeeded (attempt 1) | 14:22 IST`
- CRITICAL (P02/P04/P10) → Telegram + fallback channel simultaneously, repeated every 10 min until acked (`/ack` bot command).
- Daily 08:45 IST digest: overnight incidents, current health table, disk/mem, positions carried.
- Silence discipline: healthy day = one morning digest and nothing else. An ops agent that chats is an ops agent that gets muted.

## 6. Agent-side implementation

`ops_agent.py` — single file, stdlib + `requests` only (minimal dependency surface; it must run when everything else is broken):

- Loop every 60 s: read heartbeat file → `/health` → direct SQLite read-only fallback (`file:...?mode=ro`).
- **Time Validation:** Check NTP sync or HTTP Date header from `healthchecks.io` ping to prevent clock-drift induced force-flats.
- State machine per component: OK → DEGRADED → DOWN with debounce (2 consecutive bad reads) to avoid flapping restarts.
- `incidents` table (own SQLite file, not the bot's): `id, ts, playbook_id, trigger_state, action, result, acked`.
- `healthchecks.io` ping each loop (dead-man for the agent itself).
- systemd unit: `Restart=always`, `MemoryMax=200M`, runs as same user, **read-only mount of bot DB**, write access only to its own state dir + systemctl for the two bot units (sudoers line scoped to exactly `systemctl restart nsebot dashboard`).

## 7. What the agent can NEVER do (enforced by construction, not policy)

- Open or size any position (no order-entry code path exists in it except `emergency_flat.py`, which is restricted to `cancel_all_orders` and `exit_positions`).
- Modify `settings.py`, strategy thresholds, or any gate (no write access to config; `trading_paused` is the sole runtime flag it may set, and only to `true`).
- Delete data, rotate the bot's DB, touch calibration tables.
- Take an action absent from the playbook table (P12 catch-all).
- Unpause trading (human-only).

## 8. Tests — `tests/test_ops_agent.py`

- Stale heartbeat fixture → P01 restart command issued once; still stale after 2 → P02 state, no third restart.
- Open position with dead bot → Skips P01 restart, directly invokes P10 emergency exit.
- Auth-fail health row (401) → re-auth hook called; 3 failures → `trading_paused=true` written, CRITICAL emitted.
- Broker down health row (502) → NO re-auth attempted, limits looping.
- Force-flat sentinel: fake open NG position + clock 19:41 Thu + synced NTP → close endpoint called; bot 404 → emergency_flat invoked (mocked broker checking order cancellation first).
- Flapping guard: OK/DOWN alternating reads → no action until 2 consecutive DOWN.
- P12: unknown bad state → notification only, assert zero side-effect calls.

## 9. Rollout

| Step | Gate |
|---|---|
| 1. Bot-side health stamps + `/health` (no agent yet) | 3 days of clean health_state data |
| 2. Agent in **observe-only** (all playbooks forced T0) | 1 week; verify zero false DOWN detections |
| 3. Enable T1 (restarts, re-auth, disk) | 1 week; at least one induced-failure drill (kill -9 the bot at random hour) |
| 4. Enable T2 sentinels on **paper** positions | 1 week incl. one Thursday EIA drill |
| 5. Enable P10 emergency-flat for live | Only after live trading itself is approved; test against broker sandbox/1-lot drill first |

**Effort:** bot-side stamps ~1 d, agent + playbook ~2 d, emergency_flat ~1 d, tests ~1 d ≈ **5 dev-days**.

## 10. Consequences

**Gains:** the remote-hosting silent-death failure mode is closed; force-flat rules gain a second enforcer in a separate failure domain; every incident becomes a logged, auditable record; the user learns of problems in seconds, not at the next manual check.

**Costs:** one more service to keep honest (mitigated by external dead-man ping); a scoped sudoers entry; a second broker API key for `emergency_flat`.
*Note on API Keys:* Since Indian brokers typically lack "exit-only" API keys, `emergency_flat.py` introduces slight risk if the Ops Agent VM is compromised. This is mitigated by strictly custom-building the Shoonya wrapper inside the agent to mathematically restrict the script's capability to only cancellation and closing actions.
