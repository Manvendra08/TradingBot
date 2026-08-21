"""
Strategy Registry.
Manages dynamic loading and routing of strategies.
The LLM Multi-Leg Engine (MULTILEG) acts as the primary trade decision and execution layer.
All quantitative engines (CORE, TIMEFRAME, NG_PARITY, NG_MOMENTUM, NG_EVENT, TFSS) serve
strictly as quantitative analytical intelligence inputs for LLM reasoning.
"""

from typing import Callable, Optional
import logging
from config.runtime_config import load_runtime_config

log = logging.getLogger(__name__)

# Default startup strategies configuration
DEFAULT_STRATEGIES = {
    "MULTILEG": { "enabled": True, "ai_mode": "full", "symbols": {} },
    "CORE": { "enabled": True, "ai_mode": "full", "symbols": {} },
}

def active_strategies_for(symbol: str) -> list[str]:
    """
    Returns active strategy IDs for a symbol.
    All asset classes (NSE indices & MCX commodities) route trade execution strictly through MULTILEG.
    """
    symbol = str(symbol).upper().strip().split()[0]
    config = load_runtime_config()
    strategies = config.get("strategies", DEFAULT_STRATEGIES)
    
    multileg_conf = strategies.get("MULTILEG", {})
    if multileg_conf.get("enabled", True):
        sym_map = multileg_conf.get("symbols", {})
        if sym_map.get(symbol, True):
            return ["MULTILEG"]
            
    return []

def get_runner(sid: str) -> Optional[Callable]:
    """
    Returns the strategy runner function for the strategy ID.
    
    Architecture:
    - MULTILEG is the sole trade execution strategy (LLM primary decision-making agent).
    - CORE, TIMEFRAME, TFSS, NG_PARITY, NG_MOMENTUM, NG_EVENT are quantitative analysis engines
      whose analytical output is fed into the LLM prompt. They do NOT execute standalone single-leg trades.
    """
    if sid == "MULTILEG":
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

