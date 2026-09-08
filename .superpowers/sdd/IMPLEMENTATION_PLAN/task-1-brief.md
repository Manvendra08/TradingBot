# Task 1: Centralized Broker Authorization Gate

**Files:**
- Create: `src/engine/broker_gate.py`
- Modify: `src/engine/live_trading.py:910-927`, `src/engine/multileg_live_trading.py:189-202`, `src/engine/multileg_live_trading.py:629-650`, `src/engine/capital_allocator.py:30-38`, `src/engine/ng_parity_strategy.py:100-110`
- Test: `tests/test_broker_gate.py`

**Interfaces:**
- Consumes: `config.runtime_config.load_runtime_config()`, `src.engine.time_guards.is_trading_allowed_now()`
- Produces: `authorize_broker_execution(symbol: str, operation: str, scan_context: dict | None) -> ExecutionAuthorization`

## Global Constraints
- **Fail-closed default:** Any error, unhandled exception, missing quote, or missing configuration key must default to `live_broker_disabled=True`, `trading_paused=True`, and `live_shadow_mode=True`.
- **Zero synthetic pricing:** Option premiums must come directly from authoritative market snapshots; never substitute entry premiums, intrinsic estimates, or zero values for missing quotes.
- **Single authorization gate:** No strategy runner or order function may check individual booleans (`live_broker_disabled`, `shadow_mode`) directly — all must call `authorize_broker_execution()`.
- **Targeted testing:** Run targeted test files per task; do not run full pytest suites across the entire repository.

## Requirements & Specification

1. Create `src/engine/broker_gate.py` implementing `ExecutionAuthorization` (dataclass(frozen=True)) and `authorize_broker_execution(symbol: str, operation: str = "ENTRY", scan_context: dict | None = None) -> ExecutionAuthorization`.
2. Logic of `authorize_broker_execution`:
   - Load `runtime_config` via `load_runtime_config()`.
   - If loading fails or config missing, return unauthorized with reason "Config load failure — fail-closed".
   - Check `live_shadow_mode` (if True -> unauthorized, shadow=True, reason="Shadow mode enabled").
   - Check `live_broker_disabled` (if True -> unauthorized, shadow=False, reason="Live broker disabled in runtime config").
   - Check `trading_paused` (if True -> unauthorized, shadow=False, reason="Trading is currently paused").
   - Check `live_enabled_broker_symbols` (if symbol not in list -> unauthorized, shadow=False, reason=f"Symbol {symbol} not in live_enabled_broker_symbols").
   - Check market hours via `_is_market_open(symbol)` (if False -> unauthorized, shadow=False, reason=f"Market closed for {symbol}").
   - If all pass -> return `ExecutionAuthorization(is_authorized=True, is_shadow=False, symbol=symbol, operation=operation, reason="AUTHORIZED", config_snapshot=config)`.
3. Refactor all direct boolean safety checks in `live_trading.py`, `multileg_live_trading.py` (entry gate & exit gate), `capital_allocator.py`, and `ng_parity_strategy.py` to route through `authorize_broker_execution()`.
4. Create tests in `tests/test_broker_gate.py` covering all authorization branches (shadow mode, trading paused, broker disabled, symbol not enabled, market closed, authorized success).
5. Run targeted tests: `pytest tests/test_broker_gate.py -v`.
