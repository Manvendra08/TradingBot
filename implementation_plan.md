# Revised P1 Plan — Algo Template Builder (CRUD only)

**Scope:** templates CRUD + validation + UI + tests. No runner, no execution. Ships safe.
**Deferred to P2+:** strike resolution, multi-leg booking, premium monitoring, live path, NRML/EOD override.

## Decisions resolved

- Table named **`algo_templates`** — avoids collision with strategy-registry `strategies` concept.
- `paper_only` default **1**; `product_type` default **MIS**. NRML/override deferred.
- No Wait & Trade in P1.
- Writes via `get_conn()` from `schema.py` — **never** dashboard `_db()` (read-only RO-URI).
- All routes behind `authenticate` (same as settings/broker).
- Nav class is **`nav-tab`** (not `nav-item`/`nav-link`).

---

## 1. Schema — `src/models/schema.py`

### 1a. DDL (append to the `DDL` block, not `_MIGRATIONS` — new table, no column-add)

```sql
CREATE TABLE IF NOT EXISTS algo_templates (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL UNIQUE,
    description         TEXT,
    is_active           INTEGER DEFAULT 1,

    -- Scope
    symbol              TEXT NOT NULL,              -- NIFTY / BANKNIFTY / ...
    expiry_mode         TEXT DEFAULT 'CURRENT',     -- CURRENT | NEXT | SPECIFIC
    expiry              TEXT,                       -- YYYY-MM-DD when expiry_mode='SPECIFIC'

    -- Trigger (direction family + confidence gate, NOT exact verdict string)
    trigger_direction   TEXT NOT NULL DEFAULT 'ALL',-- LONG | SHORT | ALL
    min_confidence      INTEGER DEFAULT 0,          -- engine confidence 0-100

    -- Book-level risk guards
    max_profit          REAL,                       -- book max profit (₹)
    max_loss            REAL,                       -- book max loss (₹)
    max_margin          REAL,                       -- margin cap (₹), e.g. 600000
    max_net_delta       REAL,                       -- combined net delta cap, e.g. 0.60
    paper_only          INTEGER DEFAULT 1,          -- 1 = paper only (P1 safety gate)

    -- Trailing SL
    move_sl_to_cost     INTEGER DEFAULT 0,
    trail_sl_points     REAL,
    trail_profit_points REAL,

    -- Legs (validated JSON, contract below)
    legs_json           TEXT NOT NULL,

    product_type        TEXT DEFAULT 'MIS',         -- MIS | NRML
    created_at          TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at          TEXT DEFAULT CURRENT_TIMESTAMP
);
```

Notes:
- `created_at`/`updated_at` use `DEFAULT CURRENT_TIMESTAMP` (matches `scan_summaries`); `update_algo_template` sets `updated_at = CURRENT_TIMESTAMP` explicitly.
- `trigger_direction` replaces the plan's single `trigger_verdict` string — verdict families live in `verdict_sets.py` and can't be a 1:1 match. Confidence gate added.
- Risk guards (`max_margin`, `max_net_delta`) stored now, enforced in P3 executor. `paper_only` is the live-trading gate in P4.

### 1b. Helpers (same file, same idioms)

```python
# legs_json <-> list conversion is a single source of truth here so the API
# layer and the (P3+) executor see the same shape. We deliberately do NOT reuse
# multi_leg_trades/multi_leg_legs — those model *executed* trades (currently
# disabled) and lack strike-selection fields; a template is config, not a booking.

def _serialize_legs(legs: list) -> str:
    return json.dumps(legs, separators=(",", ":"))

def _parse_legs(raw: str | None) -> list:
    try:
        return json.loads(raw) if raw else []
    except (ValueError, TypeError):
        return []

def list_algo_templates() -> list[dict]:
    sql = "SELECT * FROM algo_templates ORDER BY is_active DESC, name"
    with get_conn(read_only=True) as conn:
        rows = conn.execute(sql).fetchall()
    out = [dict(r) for r in rows]
    for t in out:
        t["legs_json"] = _parse_legs(t.get("legs_json"))
    return out

def get_algo_template(tid: int) -> dict | None:
    sql = "SELECT * FROM algo_templates WHERE id=?"
    with get_conn(read_only=True) as conn:
        row = conn.execute(sql, (tid,)).fetchone()
        if not row:
            return None
        t = dict(row)
        t["legs_json"] = _parse_legs(t.get("legs_json"))
        return t

def insert_algo_template(data: dict) -> int:
    # data["legs_json"] is a list here — serialize once at the boundary
    payload = {**data, "legs_json": _serialize_legs(data["legs_json"])}
    sql = """INSERT INTO algo_templates (name, description, symbol, expiry_mode, expiry, trigger_direction,
             min_confidence, max_profit, max_loss, max_margin, max_net_delta,
             paper_only, move_sl_to_cost, trail_sl_points, trail_profit_points,
             legs_json, product_type)
             VALUES (:name, :description, :symbol, :expiry_mode, :expiry, :trigger_direction,
             :min_confidence, :max_profit, :max_loss, :max_margin, :max_net_delta,
             :paper_only, :move_sl_to_cost, :trail_sl_points, :trail_profit_points,
             :legs_json, :product_type) RETURNING id"""
    with get_conn() as conn:
        row = conn.execute(sql, payload).fetchone()
        return int(row["id"])

def update_algo_template(tid: int, data: dict) -> None:
    # Full-field overwrite, same field set as insert. PUT handlers must pass a
    # complete template (validate_algo_template enforces it) — a missing key is
    # a hard 400, never a silent NULL. updated_at bumped explicitly.
    payload = {**data, "id": tid, "legs_json": _serialize_legs(data["legs_json"])}
    sql = """UPDATE algo_templates SET
             name=:name, description=:description, symbol=:symbol,
             expiry_mode=:expiry_mode, expiry=:expiry, trigger_direction=:trigger_direction,
             min_confidence=:min_confidence, max_profit=:max_profit, max_loss=:max_loss,
             max_margin=:max_margin, max_net_delta=:max_net_delta,
             paper_only=:paper_only, move_sl_to_cost=:move_sl_to_cost,
             trail_sl_points=:trail_sl_points, trail_profit_points=:trail_profit_points,
             legs_json=:legs_json, product_type=:product_type,
             updated_at=CURRENT_TIMESTAMP
             WHERE id=:id"""
    with get_conn() as conn:
        conn.execute(sql, payload)

def toggle_algo_template(tid: int, is_active: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE algo_templates SET is_active=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (is_active, tid),
        )

def delete_algo_template(tid: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM algo_templates WHERE id=?", (tid,))
```

### 1c. `legs_json` contract (validated + documented)

```json
{
  "side": "BUY",
  "option_type": "CE",
  "strike_selection": "ATM",
  "offset": 0,
  "premium": 0,
  "width": 0,
  "strike": 0,
  "lots": 1,
  "target_pct": 20,
  "sl_pct": 10
}
```

- `side` ∈ {BUY, SELL}
- `option_type` ∈ {CE, PE, FUT}
- `strike_selection` ∈ {ATM, ATM_OFFSET, PREMIUM, STRADDLE_WIDTH, SPECIFIC}
- `ATM_OFFSET` → signed `offset` required; `PREMIUM` → `premium` > 0; `STRADDLE_WIDTH` → `width` > 0; `SPECIFIC` → `strike` > 0
- `lots` integer ≥ 1
- `target_pct`/`sl_pct` > 0 — semantics (premium % for SELL, underlying % for BUY) resolved in P2 via `trade_plan.py`, **never** a parallel implementation (CLAUDE.md hard constraint)
- At least **1 leg** per template; duplicate `(side, option_type, strike-selection family)` legs are rejected.
- Templates store legs as a JSON blob rather than reusing `multi_leg_trades`/`multi_leg_legs` (`schema.py`): those tables model *executed* trades (and `list_multi_leg_trades()` is currently disabled) and lack strike-selection/offset columns. A template is a config, not a booking.

### 1d. Validation + normalization

Returns `(errors, normalized)` — errors empty means valid, and `normalized` is
what callers pass to `insert`/`update` (types coerced, `legs_json` a parsed list,
defaults filled). Keeps the named-param insert path from ever seeing a stray type.

```python
def validate_algo_template(data: dict) -> tuple[list[str], dict]:
    # name, symbol required, non-empty
    # symbol in CANONICAL_SYMBOLS ∪ WATCH_SYMBOLS (config/symbols.py) — unknown → error
    # trigger_direction in {LONG, SHORT, ALL}
    # min_confidence int 0-100 (coerced)
    # expiry_mode in {CURRENT, NEXT, SPECIFIC}; SPECIFIC requires expiry parsing
    #   via datetime.strptime(expiry, "%Y-%m-%d") → bad format is an error
    # product_type in {MIS, NRML}
    # legs: parsed list, at least 1 leg, each leg per 1c rules, and NO duplicate
    #   (side, option_type, strike-selection family) legs
    # paper_only / move_sl_to_cost coerced to 0|1
    # max_profit/max_loss/max_margin/max_net_delta coerced to float, >= 0 if provided
```

Dup-name handling: catch `sqlite3.IntegrityError` on name UNIQUE → 409.

---

## 2. API — `dashboard_server.py`

Page route (mirrors `/ops` pattern at `dashboard_server.py:4257`):

```python
@app.get("/algo", response_class=HTMLResponse)
async def algo_page(username: str = Depends(authenticate)):
    html_path = ROOT / "src" / "dashboard" / "algo.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>algo.html not found</h1>", status_code=404)
```

Data routes (all `dependencies=[Depends(authenticate)]`):

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/algo_templates` | — | `list[dict]` |
| GET | `/api/algo_templates/{id}` | — | `dict`; 404 if missing |
| POST | `/api/algo_templates` | template | `{"ok": true, "id": N}`; 400 `{"ok": false, "errors": [...]}`; 409 on dup name |
| PUT | `/api/algo_templates/{id}` | full template | `{"ok": true}` (full-field overwrite; `updated_at` bumped) |
| PATCH | `/api/algo_templates/{id}/toggle` | `{"is_active": 0\|1}` | `{"ok": true}` |
| DELETE | `/api/algo_templates/{id}` | — | `{"ok": true}` |

Writes call the schema helpers — dashboard `_db()` is read-only (RO-URI). `legs_json` flows as a real list in request/response; serialization/parsing happens inside the schema helpers (`insert`/`update` serialize, `list`/`get` parse), so routes never touch `json`.

Endpoint behavior:
- **PUT = full replace, enforced.** `PUT /{id}` runs the same `validate_algo_template` as POST; a payload missing any required key → 400 `{"ok": false, "errors": [...]}`. A dropped field is a hard error, never a silent `NULL`. `PATCH /{id}/toggle` is the only partial-write endpoint.
- **Symbol whitelist.** `validate_algo_template` rejects symbols outside `CANONICAL_SYMBOLS ∪ WATCH_SYMBOLS` → 400. Server-side validation agrees with the `/api/symbols` dropdown, so a typo'd symbol can't be saved for P3 to later trade on.
- **404s:** GET/PUT/PATCH/DELETE on a missing `{id}` → 404 `{"ok": false}`.

---

## 3. UI — `src/dashboard/algo.html` + nav edits

- New page with inline `<style>` block + `theme.css` classes (same pattern as `ops.html`).
- **Left panel:** saved templates, name + symbol, Active toggle (PATCH), Delete button.
- **Right (builder form):**
  - Basic: name, description, symbol (dropdown from `/api/symbols`), expiry_mode
  - Trigger: direction (LONG/SHORT/ALL), min_confidence slider
  - Risk guards: max_profit, max_loss, max_margin, max_net_delta, paper_only checkbox
  - Leg builder: dynamic rows per 1c fields
  - Trailing: move_sl_to_cost, trail_sl_points, trail_profit_points
  - product_type (default MIS)
- Nav link added with **`nav-tab`** class to `algo.html` **and** the 6 existing pages (`index/paper/broker/ops/settings/ai`), matching the inline `.header-nav` markup.
- algo.html must copy the per-page inline `.header-nav`/`.nav-tab` style block from `ops.html` (lines ~43-50) plus `theme.css`, and mark the `/algo` link `.nav-tab.active` on that page — each dashboard page owns its own inline nav CSS.

---

## 4. Tests — `tests/test_algo_templates.py`

| Case | Expect |
|---|---|
| POST valid template → GET roundtrip, fields preserved | 200, legs_json a parsed list, types normalized |
| POST missing `name` / `symbol` | 400 + error list |
| POST unknown `symbol` (not in CANONICAL ∪ WATCH) | 400 |
| POST invalid `side` / `option_type` / `strike_selection` | 400 |
| POST `lots` = 0 or negative | 400 |
| POST single-leg template / duplicate-leg template | 400 |
| POST `SPECIFIC` without `expiry` | 400 |
| POST `SPECIFIC` with valid `expiry` → GET roundtrip | 200, expiry preserved |
| POST string ints/floats coerced | 200, read-back types correct |
| POST duplicate `name` | 409 |
| GET `/api/algo_templates/{id}` | 200 dict; missing id → 404 |
| PATCH toggle inactive → active | `is_active` flips |
| PATCH / DELETE on missing id | 404 |
| PUT full update roundtrip | fields updated, `updated_at` bumped |
| PUT missing required key | 400 (no silent partial overwrite) |
| DELETE then GET `/api/algo_templates/{id}` | 404 |
| Defaults: `paper_only=1`, `product_type=MIS` when omitted | confirmed on insert |
| `validate_algo_template` unit tests (pure) | error lists correct + normalized output |

Run: `pytest tests/test_algo_templates.py -v`

---

## 5. Housekeeping

- Update `data/sentinel/KNOWLEDGE_BASE.md` (CLAUDE.md mandate for new features).
- Append a "Changes in this session" bullet to `CLAUDE.md` for the algo template builder.
- Manual verify: `python dashboard_server.py` → open `/algo` → create/save/edit/toggle/delete; nav link present on all pages.
- `schema.py` `init_db()` auto-creates table on startup — no manual migration.

## Explicitly out of scope (P2+)

Single-leg runner → multi-leg (`leg_group_id`) → strike resolution against `scan_context` → book-level SL/target via `trade_plan.py` → live path gated by `paper_only` + risk guards.
