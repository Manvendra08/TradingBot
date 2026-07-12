"""
Strategy Registry.
Manages dynamic loading and routing of strategies (CORE, TIMEFRAME, TFSS).
"""

from typing import Callable, Optional
import logging
from config.runtime_config import load_runtime_config

log = logging.getLogger(__name__)

# Default startup strategies configuration if not present in DB
DEFAULT_STRATEGIES = {
    "CORE": { "enabled": True, "ai_mode": "boost_only", "symbols": {} },
    "TIMEFRAME": { "enabled": True, "ai_mode": "boost_only", "symbols": {} }
}

def active_strategies_for(symbol: str) -> list[str]:
    """
    Returns active strategy IDs for a symbol.
    A strategy is active if:
    1. Strategy is globally enabled.
    2. Symbol is enabled for this strategy (defaults to True if not explicitly set).
    """
    symbol = str(symbol).upper()
    
    # Custom routing for NATURALGAS based on session regimes
    if symbol.startswith("NATURALGAS"):
        from config.settings import NG_STRATEGY_ENABLED
        if not NG_STRATEGY_ENABLED:
            return []
            
        from src.engine.ng_session_router import get_ng_regime
        from datetime import datetime
        import pytz
        
        now_ist = datetime.now(pytz.timezone("Asia/Kolkata"))
        regime, _ = get_ng_regime(now_ist)
        
        if regime == "PARITY":
            return ["NG_PARITY"]
        elif regime == "EVENT":
            return ["NG_EVENT"]
        elif regime == "MOMENTUM":
            return ["NG_MOMENTUM"]
        else:
            return []

    config = load_runtime_config()
    strategies = config.get("strategies", DEFAULT_STRATEGIES)
    
    active = []
    
    for sid in ["CORE", "TIMEFRAME"]:
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
            verdict = intel_dict.get("verdict_label", "")
            side = "BUY" if verdict in ("LONG", "BULLISH") else "SELL" if verdict in ("SHORT", "BEARISH") else None
            if side:
                ok, reason = check_ng_momentum_entry(side)
                if not ok:
                    log.info("NG Momentum Entry Blocked: %s", reason)
                    return {"action": "BLOCKED_DECISION", "reason": reason}
            from src.engine.paper_trading import run_paper_trading
            return run_paper_trading(sym, scan_ctx, dig_id, intel_dict, ai_verdict)
        return run_ng_momentum_strategy
    return None

def get_ai_mode(sid: str) -> str:
    """
    Returns the AI decision mode for the strategy.
    Defaults to the global decision mode if not specified.
    """
    if sid in ("NG_PARITY", "NG_EVENT", "NG_MOMENTUM"):
        return "boost_only"
    config = load_runtime_config()
    global_mode = config.get("live_ai_decision_mode", "boost_only")
    strategies = config.get("strategies", DEFAULT_STRATEGIES)
    return strategies.get(sid, {}).get("ai_mode", global_mode)
