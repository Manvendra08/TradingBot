# Trading Engine Safety & Determinism Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate un-gated broker orders, ghost LLM execution, repaired JSON trade authorization, and multi-leg DB/broker state divergence by building a centralized authorization gate, immutable scan snapshotting, strict execution parsing, and a broker-confirmed state machine.

**Architecture:** A fail-closed `ExecutionAuthorization` gate acts as the sole permission barrier for all broker interactions (entries, exits, GTTs, reconciliations). Market inputs are frozen into immutable `ScanSnapshot` objects tagged with unique IDs that travel through LLM enrichment, strategy dispatch, and persistence. Execution parsing is strictly separated from diagnostic parsing, rejecting repaired or default-filled JSON, and multi-leg exit state machines require positive broker fill confirmation before database trade records can be marked closed.

**Tech Stack:** Python 3.10+, FastAPI, SQLite (WAL mode), Pydantic v2, Zerodha Kite Connect API, Pytest.

**Spec:** [OVERALL_VERDICT.md](OVERALL_VERDICT.md)

## Global Constraints

- **Fail-closed default:** Any error, unhandled exception, missing quote, or missing configuration key must default to `live_broker_disabled=True`, `trading_paused=True`, and `live_shadow_mode=True`.
- **Zero synthetic pricing:** Option premiums must come directly from authoritative market snapshots; never substitute entry premiums, intrinsic estimates, or zero values for missing quotes.
- **Single authorization gate:** No strategy runner or order function may check individual booleans (`live_broker_disabled`, `shadow_mode`) directly — all must call `authorize_broker_execution()`.
- **Targeted testing:** Run targeted test files per task; do not run full pytest suites across the entire repository.

---

## File Structure

```
src/
  engine/
    broker_gate.py          # Centralized broker authorization & safety check
    execution_parser.py     # Strict LLM execution JSON parser & validator
    multileg_validator.py   # Multi-leg alignment & pre-order leg quote validator
    live_trading.py         # Modified: Refactored to use broker_gate
    multileg_live_trading.py# Modified: Refactored to use broker_gate & leg state machine
    capital_allocator.py   # Modified: Refactored to use broker_gate
    ng_parity_strategy.py  # Modified: Refactored to use broker_gate
    pipeline.py            # Modified: Binds ScanSnapshot to context
  models/
    scan_snapshot.py        # Immutable frozen dataclass for market scan context
    schema.py               # Modified: Leg status tracking columns
config/
  runtime_config.py         # Modified: Fail-closed configuration schema & loader
tests/
  test_broker_gate.py       # Unit & edge case tests for ExecutionAuthorization
  test_runtime_config_failclosed.py # Unit tests for fail-closed config loading
  test_scan_snapshot.py     # Unit tests for ScanSnapshot creation & immutability
  test_execution_parser.py  # Tests for strict execution parsing vs diagnostic parsing
  test_multileg_validator.py# Tests for multi-leg pre-order validation & engine alignment
  test_multileg_exit_reconciliation.py # Tests for multi-leg leg state machine & failure handling
```

---

### Task 1: Centralized Broker Authorization Gate

**Files:**
- Create: `src/engine/broker_gate.py`
- Modify: `src/engine/live_trading.py:910-927`, `src/engine/multileg_live_trading.py:189-202`, `src/engine/multileg_live_trading.py:629-650`, `src/engine/capital_allocator.py:30-38`, `src/engine/ng_parity_strategy.py:100-110`
- Test: `tests/test_broker_gate.py`

**Interfaces:**
- Consumes: `config.runtime_config.load_runtime_config()`, `src.engine.time_guards.is_trading_allowed_now()`
- Produces: `authorize_broker_execution(symbol: str, operation: str, scan_context: dict | None) -> ExecutionAuthorization`

- [ ] **Step 1: Write the failing test for broker authorization gate**

```python
# tests/test_broker_gate.py
import pytest
from src.engine.broker_gate import authorize_broker_execution, ExecutionAuthorization

def test_authorize_broker_execution_blocked_when_shadow_mode_true(monkeypatch):
    config = {
        "live_shadow_mode": True,
        "live_broker_disabled": False,
        "trading_paused": False,
        "live_enabled_broker_symbols": ["NIFTY"]
    }
    monkeypatch.setattr("config.runtime_config.load_runtime_config", lambda: config)
    
    auth = authorize_broker_execution("NIFTY", operation="ENTRY")
    assert isinstance(auth, ExecutionAuthorization)
    assert auth.is_authorized is False
    assert auth.is_shadow is True
    assert "shadow mode" in auth.reason.lower()

def test_authorize_broker_execution_blocked_when_trading_paused(monkeypatch):
    config = {
        "live_shadow_mode": False,
        "live_broker_disabled": False,
        "trading_paused": True,
        "live_enabled_broker_symbols": ["NIFTY"]
    }
    monkeypatch.setattr("config.runtime_config.load_runtime_config", lambda: config)
    
    auth = authorize_broker_execution("NIFTY", operation="EXIT")
    assert auth.is_authorized is False
    assert "paused" in auth.reason.lower()

def test_authorize_broker_execution_success(monkeypatch):
    config = {
        "live_shadow_mode": False,
        "live_broker_disabled": False,
        "trading_paused": False,
        "live_enabled_broker_symbols": ["NIFTY"]
    }
    monkeypatch.setattr("config.runtime_config.load_runtime_config", lambda: config)
    monkeypatch.setattr("src.engine.broker_gate._is_market_open", lambda sym: True)
    
    auth = authorize_broker_execution("NIFTY", operation="ENTRY")
    assert auth.is_authorized is True
    assert auth.is_shadow is False
    assert auth.reason == "AUTHORIZED"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_broker_gate.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.engine.broker_gate'`

- [ ] **Step 3: Implement `src/engine/broker_gate.py`**

```python
# src/engine/broker_gate.py
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from config.runtime_config import load_runtime_config

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExecutionAuthorization:
    is_authorized: bool
    is_shadow: bool
    symbol: str
    operation: str  # ENTRY, EXIT, ADJUSTMENT, GTT, RECONCILE
    reason: str
    config_snapshot: dict[str, Any]


def _is_market_open(symbol: str) -> bool:
    from src.engine.paper_trading import _is_market_open as market_open_check
    return market_open_check(symbol)


def authorize_broker_execution(
    symbol: str,
    operation: str = "ENTRY",
    scan_context: dict | None = None,
) -> ExecutionAuthorization:
    """Centralized, fail-closed broker execution authorization gate.
    
    Mandatory check before ANY order, GTT placement, or exit squareoff.
    """
    sym_base = str(symbol).upper().strip().split()[0]
    
    try:
        cfg = load_runtime_config()
    except Exception as exc:
        log.error("Broker gate failed to load runtime config: %s", exc)
        return ExecutionAuthorization(
            is_authorized=False,
            is_shadow=True,
            symbol=sym_base,
            operation=operation,
            reason=f"FAIL_CLOSED: Runtime config load exception ({exc})",
            config_snapshot={},
        )

    shadow_mode = bool(cfg.get("live_shadow_mode", True))
    broker_disabled = bool(cfg.get("live_broker_disabled", False))
    trading_paused = bool(cfg.get("trading_paused", False))
    enabled_symbols = cfg.get("live_enabled_broker_symbols")

    # 1. Trading Paused Emergency Switch
    if trading_paused:
        return ExecutionAuthorization(
            is_authorized=False,
            is_shadow=shadow_mode,
            symbol=sym_base,
            operation=operation,
            reason="BLOCKED: Trading paused via emergency safety switch",
            config_snapshot=cfg,
        )

    # 2. Broker Disabled Cockpit Switch
    if broker_disabled:
        return ExecutionAuthorization(
            is_authorized=False,
            is_shadow=shadow_mode,
            symbol=sym_base,
            operation=operation,
            reason="BLOCKED: Live broker order placement disabled in Cockpit",
            config_snapshot=cfg,
        )

    # 3. Enabled Symbol List Check
    if enabled_symbols is not None and sym_base not in enabled_symbols:
        return ExecutionAuthorization(
            is_authorized=False,
            is_shadow=shadow_mode,
            symbol=sym_base,
            operation=operation,
            reason=f"BLOCKED: Symbol {sym_base} is not enabled for live trading",
            config_snapshot=cfg,
        )

    # 4. Market Hours Check
    if not _is_market_open(sym_base):
        return ExecutionAuthorization(
            is_authorized=False,
            is_shadow=shadow_mode,
            symbol=sym_base,
            operation=operation,
            reason="BLOCKED: Market is currently closed",
            config_snapshot=cfg,
        )

    # 5. Shadow Mode Gate (If in shadow mode, real execution is NOT authorized)
    if shadow_mode:
        return ExecutionAuthorization(
            is_authorized=False,
            is_shadow=True,
            symbol=sym_base,
            operation=operation,
            reason="BLOCKED: System is running in Shadow Mode (real broker orders prohibited)",
            config_snapshot=cfg,
        )

    # All checks passed — Authorized for real broker trade
    return ExecutionAuthorization(
        is_authorized=True,
        is_shadow=False,
        symbol=sym_base,
        operation=operation,
        reason="AUTHORIZED",
        config_snapshot=cfg,
    )
```

- [ ] **Step 4: Refactor existing strategy modules to use `authorize_broker_execution`**

In `src/engine/live_trading.py`:
Replace line 910-927 with:
```python
    from src.engine.broker_gate import authorize_broker_execution
    auth = authorize_broker_execution(symbol, operation="ENTRY", scan_context=scan_context)
    if not auth.is_authorized:
        log.debug("%s: live_trading entry blocked — %s", symbol, auth.reason)
        return {"action": "BLOCKED_BROKER_GATE", "reason": auth.reason}
    shadow_mode = auth.is_shadow
```

In `src/engine/multileg_live_trading.py`:
Replace line 189-202 with:
```python
    from src.engine.broker_gate import authorize_broker_execution
    auth = authorize_broker_execution(symbol, operation="ENTRY", scan_context=scan_context)
    if not auth.is_authorized:
        log.debug("[multileg-live] %s: strategy entry blocked — %s", symbol, auth.reason)
        return {"action": "BLOCKED_BROKER_GATE", "reason": auth.reason}
```

In `src/engine/multileg_live_trading.py`:
In `_close_live_book` (around line 629):
```python
    from src.engine.broker_gate import authorize_broker_execution
    auth = authorize_broker_execution(symbol, operation="EXIT")
    if not auth.is_authorized:
        log.info("[multileg-live] %s: real broker close not authorized (%s) — performing DB close only", symbol, auth.reason)
        kite = None
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_broker_gate.py -v`
Expected: PASS

- [ ] **Step 6: Commit changes**

```bash
git add src/engine/broker_gate.py src/engine/live_trading.py src/engine/multileg_live_trading.py tests/test_broker_gate.py
git commit -m "feat(engine): enforce centralized fail-closed broker authorization gate"
```

---

### Task 2: Fail-Closed Runtime Configuration Loader

**Files:**
- Modify: `config/runtime_config.py:46-122`, `config/runtime_config.py:130-155`
- Test: `tests/test_runtime_config_failclosed.py`

**Interfaces:**
- Consumes: `data/runtime_config.json`
- Produces: `load_runtime_config() -> dict`, `save_runtime_config(config: dict) -> None` with strict validation.

- [ ] **Step 1: Write the failing test for fail-closed config parsing**

```python
# tests/test_runtime_config_failclosed.py
import json
import pytest
from config.runtime_config import load_runtime_config, save_runtime_config, RUNTIME_CONFIG_PATH

def test_load_runtime_config_corrupt_file_returns_fail_closed_defaults(tmp_path, monkeypatch):
    bad_config_file = tmp_path / "runtime_config.json"
    bad_config_file.write_text("{ invalid json ...", encoding="utf-8")
    monkeypatch.setattr("config.runtime_config.RUNTIME_CONFIG_PATH", bad_config_file)
    
    cfg = load_runtime_config()
    assert cfg["live_shadow_mode"] is True
    assert cfg["live_broker_disabled"] is True
    assert cfg["trading_paused"] is True
    assert cfg["live_ai_exit_advisor_enabled"] is False

def test_save_runtime_config_validates_types(tmp_path, monkeypatch):
    config_file = tmp_path / "runtime_config.json"
    monkeypatch.setattr("config.runtime_config.RUNTIME_CONFIG_PATH", config_file)
    
    with pytest.raises(ValueError, match="live_ai_decision_mode"):
        save_runtime_config({"live_ai_decision_mode": "invalid_mode"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_runtime_config_failclosed.py -v`
Expected: FAIL with `AssertionError: assert cfg["live_broker_disabled"] is True` (currently defaults to False)

- [ ] **Step 3: Implement strict fail-closed loading and validation in `config/runtime_config.py`**

Update `load_runtime_config()` in `config/runtime_config.py`:

```python
VALID_AI_DECISION_MODES = {"advisory", "boost_only", "full", "empirical"}

def get_fail_closed_defaults(default_freq: int = 15) -> dict:
    """Safe, fail-closed defaults returned on missing file or parse corruption."""
    return {
        "scan_frequency_minutes": default_freq,
        "scan_frequency_nse": default_freq,
        "scan_frequency_mcx": default_freq,
        "live_shadow_mode": True,               # SAFE: Shadow mode ON
        "live_broker_disabled": True,           # SAFE: Broker orders DISABLED
        "trading_paused": True,                 # SAFE: Trading PAUSED
        "live_capital_per_trade_inr": 20000,
        "live_max_capital_utilisation_pct": 80,
        "live_max_concurrent_positions": 2,
        "live_max_daily_loss_rupees": 200000,
        "live_symbol_lots": default_symbol_lots(1),
        "paper_symbol_lots": default_symbol_lots(10),
        "paper_lots": 10,
        "live_enabled_broker_symbols": ["NIFTY", "BANKNIFTY", "NATURALGAS", "CRUDEOIL"],
        "paper_enabled_symbols": ["NIFTY", "BANKNIFTY", "NATURALGAS", "CRUDEOIL"],
        "oi_spike_threshold_pct": 10.0,
        "price_spike_threshold_pct": 2.0,
        "dashboard_auth_enabled": False,
        "live_ai_decision_mode": "advisory",
        "live_ai_min_confidence_boost": 80,
        "live_ai_min_confidence_veto": 85,
        "live_ai_exit_advisor_enabled": False, # SAFE: AI Exit Advisor DISABLED by default
        "emp_boost_min_trades": 20,
        "emp_boost_min_winrate": 0.60,
        "ml_predictor_mode": "shadow",
        "derive_min_confidence": False,
        "llm_enrichment_async": True,
        "llm_enrich_timeout_s": 120,
        "autopsy_enabled": True,
        "autopsy_time_ist": "23:45",
        "manage_direct_kite_positions": False,
        "direct_kite_initialization_mode": "fixed_pct",
        "direct_kite_default_sl_pct": 75.0,
        "direct_kite_default_tgt_pct": 60.0,
        "enable_tfss_trade_blocked_rules": False,
        "enable_ng_parity_trades": True,
        "sentinel_report_mode": "anomalies",
        "ops_agent_mode": "observe",
        "tiered_gates_enabled": True,
    }

def validate_config_dict(config: dict) -> None:
    """Validate runtime configuration keys and types before saving."""
    if "live_ai_decision_mode" in config:
        mode = str(config["live_ai_decision_mode"]).lower()
        if mode not in VALID_AI_DECISION_MODES:
            raise ValueError(f"Invalid live_ai_decision_mode '{mode}'. Allowed: {VALID_AI_DECISION_MODES}")
            
    for bool_key in ("live_shadow_mode", "live_broker_disabled", "trading_paused", "live_ai_exit_advisor_enabled"):
        if bool_key in config and not isinstance(config[bool_key], bool):
            raise ValueError(f"Configuration key '{bool_key}' must be a boolean.")
```

Modify `load_runtime_config()` exception handler:
```python
    except Exception as exc:
        log.error("Failed to load or parse %s, returning fail-closed defaults: %s", RUNTIME_CONFIG_PATH, exc)
        return get_fail_closed_defaults()
```

Modify `save_runtime_config()`:
```python
def save_runtime_config(config: dict) -> None:
    global _CACHED_CONFIG, _CACHED_MTIME, _CACHED_PATH
    validate_config_dict(config)
    # ... rest of atomic file save logic ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_runtime_config_failclosed.py -v`
Expected: PASS

- [ ] **Step 5: Commit changes**

```bash
git add config/runtime_config.py tests/test_runtime_config_failclosed.py
git commit -m "fix(config): enforce fail-closed runtime configuration loading and schema validation"
```

---

### Task 3: Immutable Scan Snapshots

**Files:**
- Create: `src/models/scan_snapshot.py`
- Modify: `src/engine/pipeline.py:880-920`, `src/engine/pipeline.py:1126-1175`
- Test: `tests/test_scan_snapshot.py`

**Interfaces:**
- Consumes: `scan_context: dict`
- Produces: `ScanSnapshot` frozen dataclass with `snapshot_id: str`, `to_dict() -> dict`

- [ ] **Step 1: Write the failing test for ScanSnapshot**

```python
# tests/test_scan_snapshot.py
import pytest
from src.models.scan_snapshot import ScanSnapshot, create_scan_snapshot

def test_scan_snapshot_immutability():
    snap = create_scan_snapshot(
        symbol="NIFTY",
        underlying=24500.0,
        expiry="2026-09-10",
        option_rows=[{"strike": 24500, "option_type": "CE", "ltp": 120.0}],
        engine_verdict="BULLISH_BUILDUP",
        engine_confidence=82
    )
    assert snap.snapshot_id.startswith("snap_NIFTY_")
    assert snap.underlying == 24500.0
    
    with pytest.raises(AttributeError):
        snap.underlying = 25000.0  # Frozen dataclass check

def test_scan_snapshot_option_rows_hash():
    snap1 = create_scan_snapshot("NIFTY", 24500.0, "2026-09-10", [{"strike": 24500, "ltp": 120.0}])
    snap2 = create_scan_snapshot("NIFTY", 24500.0, "2026-09-10", [{"strike": 24500, "ltp": 120.0}])
    assert snap1.option_rows_hash == snap2.option_rows_hash
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_scan_snapshot.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.models.scan_snapshot'`

- [ ] **Step 3: Implement `src/models/scan_snapshot.py`**

```python
# src/models/scan_snapshot.py
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class ScanSnapshot:
    snapshot_id: str
    created_at_iso: str
    symbol: str
    underlying: float
    expiry: str
    atm_strike: float
    engine_verdict: str
    engine_confidence: int
    data_legitimacy_score: int
    option_rows_hash: str
    option_rows: tuple[dict[str, Any], ...]
    intel_snapshot: tuple[tuple[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "created_at_iso": self.created_at_iso,
            "symbol": self.symbol,
            "underlying": self.underlying,
            "expiry": self.expiry,
            "atm_strike": self.atm_strike,
            "engine_verdict": self.engine_verdict,
            "engine_confidence": self.engine_confidence,
            "data_legitimacy_score": self.data_legitimacy_score,
            "option_rows_hash": self.option_rows_hash,
            "option_rows": list(self.option_rows),
            "intel": dict(self.intel_snapshot),
        }


def _hash_option_rows(rows: list[dict[str, Any]]) -> str:
    simplified = [
        {
            "s": r.get("strike"),
            "t": r.get("option_type"),
            "p": r.get("ltp"),
            "oi": r.get("oi"),
        }
        for r in rows
        if isinstance(r, dict)
    ]
    raw = json.dumps(simplified, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def create_scan_snapshot(
    symbol: str,
    underlying: float,
    expiry: str,
    option_rows: list[dict[str, Any]],
    engine_verdict: str = "NEUTRAL",
    engine_confidence: int = 0,
    atm_strike: float = 0.0,
    data_legitimacy_score: int = 100,
    intel: dict[str, Any] | None = None,
) -> ScanSnapshot:
    now_iso = datetime.now(timezone.utc).isoformat()
    unique_suffix = uuid.uuid4().hex[:8]
    snap_id = f"snap_{symbol}_{now_iso[:10]}_{unique_suffix}"

    rows_tuple = tuple(dict(r) for r in option_rows if isinstance(r, dict))
    intel_dict = intel or {}
    intel_tuple = tuple((k, v) for k, v in intel_dict.items() if isinstance(k, str))

    return ScanSnapshot(
        snapshot_id=snap_id,
        created_at_iso=now_iso,
        symbol=symbol,
        underlying=float(underlying),
        expiry=str(expiry),
        atm_strike=float(atm_strike),
        engine_verdict=str(engine_verdict),
        engine_confidence=int(engine_confidence),
        data_legitimacy_score=int(data_legitimacy_score),
        option_rows_hash=_hash_option_rows(option_rows),
        option_rows=rows_tuple,
        intel_snapshot=intel_tuple,
    )
```

- [ ] **Step 4: Bind `ScanSnapshot` into `src/engine/pipeline.py`**

In `src/engine/pipeline.py`, inside `_process_prefetched_symbol`:
```python
    from src.models.scan_snapshot import create_scan_snapshot
    snapshot = create_scan_snapshot(
        symbol=symbol,
        underlying=underlying,
        expiry=expiry,
        option_rows=scan_context.get("option_rows") or [],
        engine_verdict=intel.get("verdict_label", ""),
        engine_confidence=int(intel.get("confidence") or 0),
        atm_strike=float(scan_context.get("atm_strike") or 0.0),
        data_legitimacy_score=legit.score if hasattr(legit, 'score') else 100,
        intel=intel,
    )
    scan_context["snapshot_id"] = snapshot.snapshot_id
    scan_context["_snapshot"] = snapshot
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_scan_snapshot.py -v`
Expected: PASS

- [ ] **Step 6: Commit changes**

```bash
git add src/models/scan_snapshot.py src/engine/pipeline.py tests/test_scan_snapshot.py
git commit -m "feat(engine): bind immutable ScanSnapshot context to execution pipeline"
```

---

### Task 4: Strict LLM Execution Parser

**Files:**
- Create: `src/engine/execution_parser.py`
- Modify: `src/engine/llm_enrichment.py:1590-1630`, `src/engine/llm_enrichment.py:3930-3960`
- Test: `tests/test_execution_parser.py`

**Interfaces:**
- Consumes: `raw_text: str`, `snapshot: ScanSnapshot | None`
- Produces: `parse_strict_execution_json(raw_text: str) -> dict` (Raises `ValueError` on repaired, partial, or ambiguous output).

- [ ] **Step 1: Write failing test for strict execution parsing**

```python
# tests/test_execution_parser.py
import pytest
from src.engine.execution_parser import parse_strict_execution_json, StrictExecutionParseError

def test_strict_parser_accepts_clean_json():
    raw = '{"action": "GO_LONG", "confidence": 85, "instrument": "NIFTY 24500 CE", "stop_loss": "24400"}'
    res = parse_strict_execution_json(raw)
    assert res["action"] == "GO_LONG"
    assert res["confidence"] == 85

def test_strict_parser_rejects_repaired_truncated_json():
    raw = '{"action": "GO_LONG", "confidence": 85, "instrument": "NIFTY 24500' # Truncated
    with pytest.raises(StrictExecutionParseError, match="Truncated or invalid JSON"):
        parse_strict_execution_json(raw)

def test_strict_parser_rejects_multiple_json_objects():
    raw = 'Example: {"action": "NO_TRADE"}\nFinal Answer: {"action": "GO_LONG", "confidence": 85}'
    with pytest.raises(StrictExecutionParseError, match="Multiple JSON candidates"):
        parse_strict_execution_json(raw)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_execution_parser.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.engine.execution_parser'`

- [ ] **Step 3: Implement `src/engine/execution_parser.py`**

```python
# src/engine/execution_parser.py
from __future__ import annotations

import json
import re
from typing import Any


class StrictExecutionParseError(ValueError):
    """Raised when raw LLM output fails strict, un-repaired execution validation."""
    pass


def parse_strict_execution_json(raw_text: str) -> dict[str, Any]:
    """Strict parser for execution-authorizing LLM outputs.
    
    Unlike diagnostic parser (_extract_json), this parser:
    1. Rejects repaired/truncated JSON strings.
    2. Rejects outputs containing multiple distinct JSON blocks (ambiguity).
    3. Rejects default-filled placeholders.
    """
    if not raw_text or not isinstance(raw_text, str):
        raise StrictExecutionParseError("Empty or non-string LLM response")

    # Strip thinking blocks if present
    cleaned = re.sub(r"<thinking>.*?</thinking>", "", raw_text, flags=re.DOTALL).strip()

    # Find all JSON block candidates
    matches = re.findall(r"\{[^{}]*\}", cleaned, flags=re.DOTALL)
    if not matches:
        # Try finding outer braces
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            matches = [cleaned[start : end + 1]]

    if not matches:
        raise StrictExecutionParseError("No JSON object found in response")

    if len(matches) > 1:
        # Check if they are distinct objects or nested
        valid_objs = []
        for m in matches:
            try:
                valid_objs.append(json.loads(m))
            except Exception:
                continue
        if len(valid_objs) > 1:
            raise StrictExecutionParseError(f"Multiple JSON candidates detected ({len(valid_objs)}). Ambiguous output rejected.")

    raw_json_str = matches[0]

    # Strict JSON parse without repair
    try:
        data = json.loads(raw_json_str)
    except json.JSONDecodeError as exc:
        raise StrictExecutionParseError(f"Truncated or invalid JSON string: {exc}") from exc

    if not isinstance(data, dict):
        raise StrictExecutionParseError(f"JSON response must be a dict object, got {type(data).__name__}")

    # Required field verification
    if "action" not in data and "strategy_type" not in data:
        raise StrictExecutionParseError("Missing required execution key ('action' or 'strategy_type')")

    return data
```

- [ ] **Step 4: Integrate strict parser into execution path in `src/engine/llm_enrichment.py`**

In `src/engine/llm_enrichment.py`, update `get_llm_verdict` and `get_multileg_verdict` when called for live execution:
```python
    from src.engine.execution_parser import parse_strict_execution_json, StrictExecutionParseError
    # Use strict parser when evaluating execution eligibility
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_execution_parser.py -v`
Expected: PASS

- [ ] **Step 6: Commit changes**

```bash
git add src/engine/execution_parser.py src/engine/llm_enrichment.py tests/test_execution_parser.py
git commit -m "feat(engine): add strict execution parser for un-repaired trade authorization"
```

---

### Task 5: Multi-Leg Strategy Pre-Flight & Engine Alignment

**Files:**
- Create: `src/engine/multileg_validator.py`
- Modify: `src/engine/multileg_live_trading.py:860-910`
- Test: `tests/test_multileg_validator.py`

**Interfaces:**
- Consumes: `verdict: Any`, `scan_context: dict`
- Produces: `validate_multileg_preflight(verdict, scan_context) -> tuple[bool, str, list[dict]]`

- [ ] **Step 1: Write failing test for multi-leg validator**

```python
# tests/test_multileg_validator.py
import pytest
from src.engine.multileg_validator import validate_multileg_preflight

def test_validate_multileg_preflight_blocks_directional_flip():
    class DummyVerdict:
        strategy_type = "BULL_PUT_SPREAD"
        legs = [{"strike": 24500, "option_type": "PE", "side": "SELL", "entry_premium": 100.0}]
        
    scan_context = {
        "underlying": 24500.0,
        "intel": {"verdict_label": "BEARISH_BREAKOUT"}, # Engine is bearish, verdict is bullish
        "option_rows": [{"strike": 24500, "option_type": "PE", "ltp": 100.0, "oi": 5000, "volume": 1000}]
    }
    
    ok, reason, validated_legs = validate_multileg_preflight(DummyVerdict(), scan_context)
    assert ok is False
    assert "DIRECTION_FLIP" in reason
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_multileg_validator.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.engine.multileg_validator'`

- [ ] **Step 3: Implement `src/engine/multileg_validator.py`**

```python
# src/engine/multileg_validator.py
from __future__ import annotations

import logging
from typing import Any

from src.engine.data_validator import validate_trade_leg_data
from src.engine.verdict_sets import is_bearish, is_bullish

log = logging.getLogger(__name__)


def validate_multileg_preflight(
    verdict: Any,
    scan_context: dict[str, Any],
) -> tuple[bool, str, list[dict[str, Any]]]:
    """Validate multi-leg decision against engine direction and authoritative chain quotes."""
    if not verdict:
        return False, "NO_VERDICT: Verdict object is None", []

    st_upper = str(getattr(verdict, "strategy_type", "")).upper().strip()
    if st_upper in ("NONE", "NO_TRADE", "SKIP", ""):
        return False, f"NO_TRADE: Verdict selected {st_upper}", []

    legs = getattr(verdict, "legs", None)
    if not legs or not isinstance(legs, list):
        return False, "INVALID_LEGS: Verdict contains no legs", []

    intel = scan_context.get("intel") or {}
    engine_verdict = intel.get("verdict_label", "")

    # 1. Directional Alignment Guard
    # Bullish strategy types: BULL_PUT_SPREAD, BULL_CALL_SPREAD
    # Bearish strategy types: BEAR_CALL_SPREAD, BEAR_PUT_SPREAD
    is_multileg_bullish = st_upper in ("BULL_PUT_SPREAD", "BULL_CALL_SPREAD")
    is_multileg_bearish = st_upper in ("BEAR_CALL_SPREAD", "BEAR_PUT_SPREAD")

    if is_bullish(engine_verdict) and is_multileg_bearish:
        return False, f"DIRECTION_FLIP_BLOCKED: Engine is Bullish ({engine_verdict}) but MULTILEG proposed Bearish strategy ({st_upper})", []

    if is_bearish(engine_verdict) and is_multileg_bullish:
        return False, f"DIRECTION_FLIP_BLOCKED: Engine is Bearish ({engine_verdict}) but MULTILEG proposed Bullish strategy ({st_upper})", []

    # 2. Strict Binary Leg Quote Validation
    underlying = float(scan_context.get("underlying") or 0.0)
    oc_data = {
        "strikes": scan_context.get("option_rows") or []
    }

    leg_dicts = []
    for leg in legs:
        if isinstance(leg, dict):
            leg_dicts.append(leg)
        else:
            leg_dicts.append(getattr(leg, "__dict__", {}))

    is_valid, issues = validate_trade_leg_data(leg_dicts, oc_data, underlying)
    if not is_valid:
        return False, f"DATA_INTEGRITY_FAILURE: {'; '.join(issues)}", []

    return True, "PREFLIGHT_OK", leg_dicts
```

- [ ] **Step 4: Integrate `validate_multileg_preflight` into `src/engine/multileg_live_trading.py`**

In `_attempt_new_live_entry` in `src/engine/multileg_live_trading.py`:
```python
    from src.engine.multileg_validator import validate_multileg_preflight
    ok, reason, validated_legs = validate_multileg_preflight(verdict, scan_context)
    if not ok:
        log.warning("[multileg-live] %s: Pre-flight validation failed — %s", symbol, reason)
        return {"action": "BLOCKED_PREFLIGHT", "reason": reason}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_multileg_validator.py -v`
Expected: PASS

- [ ] **Step 6: Commit changes**

```bash
git add src/engine/multileg_validator.py src/engine/multileg_live_trading.py tests/test_multileg_validator.py
git commit -m "feat(engine): enforce pre-flight engine alignment and leg validation for multi-leg orders"
```

---

### Task 6: Leg State Machine & Broker Exit Confirmation

**Files:**
- Modify: `src/models/schema.py:110-149`, `src/engine/multileg_live_trading.py:710-805`
- Test: `tests/test_multileg_exit_reconciliation.py`

**Interfaces:**
- Consumes: `book_id: str`, `legs: list[dict]`, `closed_at: str`
- Produces: `close_book_with_reconciliation(book_id, closed_at, exit_results)` (Leaves book open if any leg exit fails).

- [ ] **Step 1: Write failing test for exit reconciliation**

```python
# tests/test_multileg_exit_reconciliation.py
import pytest
from src.engine.multileg_live_trading import _reconcile_book_exit_status

def test_reconcile_book_exit_status_keeps_book_open_on_failed_leg():
    exit_results = [
        {"leg_id": 1, "status": "ORDER_FILLED"},
        {"leg_id": 2, "status": "FAILED", "error": "Insufficient margin"}
    ]
    book_status, reason = _reconcile_book_exit_status(exit_results)
    assert book_status == "OPEN"
    assert "RECONCILIATION_REQUIRED" in reason
    assert "leg_id 2 failed" in reason

def test_reconcile_book_exit_status_closes_book_when_all_filled():
    exit_results = [
        {"leg_id": 1, "status": "ORDER_FILLED"},
        {"leg_id": 2, "status": "ORDER_FILLED"}
    ]
    book_status, reason = _reconcile_book_exit_status(exit_results)
    assert book_status == "CLOSED"
    assert reason == "ALL_LEGS_CONFIRMED"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_multileg_exit_reconciliation.py -v`
Expected: FAIL with `ImportError: cannot import name '_reconcile_book_exit_status'`

- [ ] **Step 3: Implement `_reconcile_book_exit_status` and update `_close_live_book` in `src/engine/multileg_live_trading.py`**

Add helper function `_reconcile_book_exit_status` to `src/engine/multileg_live_trading.py`:

```python
def _reconcile_book_exit_status(exit_results: list[dict[str, Any]]) -> tuple[str, str]:
    """Determine final book status based on per-leg broker exit outcomes.
    
    If any leg failed to close at the broker, the book MUST remain OPEN
    with status RECONCILIATION_REQUIRED to prevent orphan positions.
    """
    failed_legs = [r for r in exit_results if r.get("status") in ("FAILED", "UNRESOLVED", "NO_BROKER")]
    if failed_legs:
        failed_ids = [str(r.get("leg_id")) for r in failed_legs]
        return "OPEN", f"RECONCILIATION_REQUIRED: Exit failed for leg_id(s) {', '.join(failed_ids)}"
    return "CLOSED", "ALL_LEGS_CONFIRMED"
```

Update `_close_live_book` in `src/engine/multileg_live_trading.py`:

```python
    # Reconcile exit results before updating DB book status
    final_book_status, reconciliation_reason = _reconcile_book_exit_status(exit_results)
    
    if final_book_status == "OPEN":
        log.error(
            "[multileg-live] %s: CANNOT close book %s — %s. Book remains OPEN for manual/ops intervention.",
            symbol, book_id, reconciliation_reason
        )
        # Update book record with alert status, but DO NOT mark closed
        from src.models.schema import update_book_reconciliation_alert
        update_book_reconciliation_alert(book_id, reconciliation_reason)
    else:
        close_book(book_id, closed_at, status, reason, total_pnl)
        log.info("[multileg-live] %s: book %s successfully closed — %s", symbol, book_id, reason)
```

In `src/models/schema.py`:
Add helper `update_book_reconciliation_alert`:

```python
def update_book_reconciliation_alert(book_id: str, reason: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE multi_leg_trades
            SET exit_reason = ?,
                trade_status = 'RECONCILIATION_REQUIRED'
            WHERE book_id = ?
            """,
            (reason, book_id),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_multileg_exit_reconciliation.py -v`
Expected: PASS

- [ ] **Step 5: Commit changes**

```bash
git add src/models/schema.py src/engine/multileg_live_trading.py tests/test_multileg_exit_reconciliation.py
git commit -m "fix(engine): prevent premature DB book closure on failed multi-leg broker exit legs"
```

---

## Execution Handoff

Plan complete and saved to `IMPLEMENTATION_PLAN.md`. Two execution options:

1. **Subagent-Driven (recommended)** - Dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** - Execute tasks in this session using `executing-plans`, batch execution with checkpoints.

Which approach would you like to take?