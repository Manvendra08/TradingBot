"""
Strategy Registry.
Manages dynamic loading and routing of strategies.
The LLM Multi-Leg Engine (MULTILEG) acts as the primary trade decision and execution layer.
All quantitative engines (CORE, TIMEFRAME, NG_PARITY, NG_MOMENTUM, NG_EVENT, TFSS) serve
strictly as quantitative analytical intelligence inputs for LLM reasoning.
"""

from typing import Callable, Optional
import logging
from datetime import datetime
import pytz
from config.runtime_config import load_runtime_config

log = logging.getLogger(__name__)

# Default startup strategies configuration
DEFAULT_STRATEGIES = {
    "MULTILEG": { "enabled": True, "ai_mode": "full", "symbols": {} },
    "CORE": { "enabled": True, "ai_mode": "boost_only", "symbols": {} },
    "TIMEFRAME": { "enabled": False, "ai_mode": "boost_only", "symbols": {} },
    "TFSS": { "enabled": False, "ai_mode": "advisory", "symbols": {} },
    "NG_PARITY": { "enabled": True, "ai_mode": "boost_only", "symbols": {} },
}

def active_strategies_for(symbol: str) -> list[str]:
    """
    Returns active strategy IDs for a symbol based on runtime config and symbol regimes.
    """
    symbol = str(symbol).upper().strip().split()[0]
    config = load_runtime_config()
    strategies = config.get("strategies", DEFAULT_STRATEGIES)

    if symbol == "NATURALGAS":
        from src.engine.ng_session_router import get_ng_regime
        now_ist = datetime.now(pytz.timezone("Asia/Kolkata"))
        regime, _ = get_ng_regime(now_ist)

        ng_parity_enabled = bool(
            config.get("enable_ng_parity_trades", True)
            and strategies.get("NG_PARITY", {}).get("enabled", True)
        )

        active_ng = []
        if regime == "PARITY" and ng_parity_enabled:
            active_ng.append("NG_PARITY")
        elif regime == "EVENT":
            active_ng.append("NG_EVENT")
        elif regime == "MOMENTUM":
            active_ng.append("NG_MOMENTUM")

        if strategies.get("TIMEFRAME", {}).get("enabled", False):
            active_ng.append("TIMEFRAME")

        multileg_conf = strategies.get("MULTILEG", {})
        if multileg_conf.get("enabled", False) and multileg_conf.get("symbols", {}).get(symbol, True):
            active_ng.append("MULTILEG")

        core_conf = strategies.get("CORE", {})
        if core_conf.get("enabled", False) and core_conf.get("symbols", {}).get(symbol, True):
            if "CORE" not in active_ng and not active_ng:
                active_ng.append("CORE")

        return active_ng

    active = []
    for sid in ["CORE", "TIMEFRAME", "TFSS", "MULTILEG"]:
        strat_conf = strategies.get(sid, {})
        if not strat_conf.get("enabled", False):
            continue

        sym_map = strat_conf.get("symbols", {})
        # Symbol is active if not explicitly set to False
        if sym_map.get(symbol, True):
            active.append(sid)

    return active

def get_runner(sid: str) -> Optional[Callable]:
    """
    Returns the strategy runner function for the strategy ID.
    """
    if sid == "CORE":
        from src.engine.paper_trading import run_paper_trading
        return run_paper_trading
    elif sid == "TFSS":
        config = load_runtime_config()
        tfss_enabled = bool(config.get("strategies", {}).get("TFSS", {}).get("enabled", False))
        if tfss_enabled:
            from src.engine.paper_trading import run_paper_trading
            return run_paper_trading
        return None
    elif sid == "TIMEFRAME":
        from src.engine.paper_trading import run_timeframe_strategy
        return run_timeframe_strategy
    elif sid == "NG_PARITY":
        from src.engine.ng_parity_strategy import run_ng_parity_strategy
        return run_ng_parity_strategy
    elif sid == "NG_EVENT":
        from src.engine.ng_eia_strategy import run_ng_eia_strategy
        return run_ng_eia_strategy
    elif sid == "NG_MOMENTUM":
        def run_ng_momentum_strategy(sym, scan_ctx, dig_id, intel_dict, ai_verdict=None):
            from src.engine.ng_momentum_strategy import check_ng_momentum_entry
            verdict = str(intel_dict.get("verdict_label", "")).upper()
            if any(k in verdict for k in ("LONG", "BULLISH", "PUT WRITING")):
                side = "BUY"
            elif any(k in verdict for k in ("SHORT", "BEARISH", "CALL WRITING")):
                side = "SELL"
            else:
                side = None
            if side:
                ok, reason = check_ng_momentum_entry(side)
                if not ok:
                    log.info("NG Momentum Entry Blocked: %s", reason)
                    return {"action": "BLOCKED_DECISION", "reason": reason}
            from src.engine.paper_trading import run_paper_trading
            return run_paper_trading(sym, scan_ctx, dig_id, intel_dict, ai_verdict)
        return run_ng_momentum_strategy
    elif sid == "MULTILEG":
        from src.engine.multileg_paper_trading import run_multileg_paper_strategy
        return run_multileg_paper_strategy
    return None

def get_ai_mode(sid: str) -> str:
    """
    Returns the AI decision mode for the strategy.
    Defaults to 'full' for LLM-driven execution.
    """
    config = load_runtime_config()
    global_mode = config.get("live_ai_decision_mode", "full")
    strategies = config.get("strategies", DEFAULT_STRATEGIES)
    return strategies.get(sid, {}).get("ai_mode", global_mode)


