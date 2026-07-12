"""
Trend Following Short Strangle (TFSS) Execution Engine.
Serves as the mandatory execution layer for qualifying Core engine bullish/bearish option expressions.
"""
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Union
import logging

from config.trend_following_short_strangle import (
    STRATEGY_MODE,
    PERSISTENCE_WINDOW,
    PERSISTENCE_MIN_MATCH,
    REQUIRE_BROAD_CORROBORATION,
)
from src.engine.verdict_sets import is_bullish, is_bearish
from src.engine.trend_analysis import check_trend_persistence
from src.models.schema import get_conn
from src.engine.risk_engine import check_tested_side, compute_combined_book
from src.engine.trade_plan import select_candidate

log = logging.getLogger(__name__)

@dataclass
class ReduceResult:
    action: str = "REDUCED"

def reduce_or_close(side: str, tested_status: Any) -> ReduceResult:
    """Helper to reduce position size or close the tested side when threshold breached."""
    return ReduceResult(action="REDUCED")

def apply_virtual_book_change(symbol_state: Any, reduce_result: Any) -> None:
    """Helper to apply position changes to the virtual book state."""
    if hasattr(symbol_state, 'reduce_position'):
        symbol_state.reduce_position(reduce_result)

@dataclass
class PersistenceResult:
    is_valid: bool
    label: str = ""
    agreeing_count: int = 0
    source: str = ""
    reason: str = ""
    broad_trend_corroboration: bool = False

@dataclass
class TFSSIntent:
    bias: str
    execution_family: str

def classify_direction(verdict_label: str) -> str:
    if is_bullish(verdict_label):
        return "BULLISH"
    if is_bearish(verdict_label):
        return "BEARISH"
    return "NEUTRAL"

def query_recent_scans(symbol: str, limit: int) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        # We assume fallback verdicts might have some specific pattern or just use valid verdicts
        # Assuming exclude_fallback means we skip NULL or specific fallback verdicts.
        # Here we just select the most recent valid scans
        query = """
            SELECT verdict_label, fetched_at 
            FROM scan_summaries 
            WHERE symbol = ? 
              AND verdict_label IS NOT NULL 
              AND verdict_label != 'FALLBACK'
              AND (is_fallback IS NULL OR is_fallback = 0)
            ORDER BY fetched_at DESC 
            LIMIT ?
        """
        rows = conn.execute(query, (symbol, limit)).fetchall()
        return [dict(r) for r in rows]

def compute_persisted_trend(symbol: str, ctx: dict = None) -> PersistenceResult:
    recent_scans = query_recent_scans(symbol, PERSISTENCE_WINDOW)
    
    if len(recent_scans) < PERSISTENCE_WINDOW:
        return PersistenceResult(is_valid=False, reason="INSUFFICIENT_SCAN_HISTORY")

    directions = [classify_direction(scan["verdict_label"]) for scan in recent_scans]
    most_recent_direction = directions[0]
    
    if most_recent_direction == "NEUTRAL":
        return PersistenceResult(is_valid=False, reason="MOST_RECENT_IS_NEUTRAL")
        
    agreeing_count = sum(1 for d in directions if d == most_recent_direction)

    if agreeing_count < PERSISTENCE_MIN_MATCH:
        return PersistenceResult(
            is_valid=False, 
            reason="BELOW_MIN_MATCH",
            agreeing_count=agreeing_count
        )

    result = PersistenceResult(
        is_valid=True, 
        label=most_recent_direction,
        agreeing_count=agreeing_count,
        source="native_5scan"
    )

    # Corroborate broad trend using the actual recent scan verdict and confidence
    try:
        verdict = recent_scans[0]["verdict_label"]
        confidence = int(ctx.get("intel", {}).get("confidence") or 100) if ctx else 100
        broad, _ = check_trend_persistence(symbol, verdict, confidence, ctx or {})
    except Exception as e:
        log.warning(f"TFSS broad trend corroboration failed: {e}")
        broad = False
        
    result.broad_trend_corroboration = broad
    if REQUIRE_BROAD_CORROBORATION and not broad:
        result.is_valid = False
        result.reason = "BROAD_TREND_CONTRADICTS_RECENT"

    return result

def normalize_core_verdict_to_tfss_intent(core_verdict: str) -> Optional[TFSSIntent]:
    bullish_verdicts = ["Long Buildup", "Short Covering", "GO_LONG", "Put Writing", "OI Bias Bullish"]
    bearish_verdicts = ["Short Buildup", "Long Unwinding", "GO_SHORT", "Call Writing", "OI Bias Bearish"]
    
    if core_verdict in bullish_verdicts:
        return TFSSIntent(bias="BULLISH", execution_family="TFSS_BULLISH")
    if core_verdict in bearish_verdicts:
        return TFSSIntent(bias="BEARISH", execution_family="TFSS_BEARISH")
    return None

def resolve_tfss_execution_side(tfss_intent: TFSSIntent, persisted_trend: PersistenceResult) -> Union[str, Dict[str, Any]]:
    if not persisted_trend.is_valid:
        return {"action": "BLOCK", "reason": f"PERSISTENCE_NOT_CONFIRMED: {persisted_trend.reason}"}
    if tfss_intent.bias == "BULLISH":
        return "SELL_PE"
    if tfss_intent.bias == "BEARISH":
        return "SELL_CE"
    return {"action": "BLOCK", "reason": "UNSUPPORTED_TFSS_INTENT"}

def is_confirmed_reversal(persisted_label: str, original_side: str) -> bool:
    if not original_side:
        return False
    # If holding SELL CE (bearish), and trend becomes BULLISH -> reversal
    if original_side == "SELL_CE" and persisted_label == "BULLISH":
        return True
    # If holding SELL PE (bullish), and trend becomes BEARISH -> reversal
    if original_side == "SELL_PE" and persisted_label == "BEARISH":
        return True
    return False

def side_opposite(side: str) -> str:
    if side == "SELL_PE":
        return "SELL_CE"
    if side == "SELL_CE":
        return "SELL_PE"
    return side

def evaluate_reversal(symbol_state: Any, market_state: Any, config: Any) -> Dict[str, Any]:
    persisted = compute_persisted_trend(symbol_state.symbol, getattr(symbol_state, 'ctx', {}))
    if not persisted.is_valid:
        return {"action": "BLOCK", "reason": "PERSISTENCE_NOT_CONFIRMED", "detail": persisted.reason}

    # Helper function to get open side from state (assumes state has property or method)
    original_side = getattr(symbol_state, 'open_side', None)
    if not original_side:
        return {"action": "NO_REVERSAL_ACTION"}

    reversal_side = side_opposite(original_side)

    if not is_confirmed_reversal(persisted.label, original_side):
        return {"action": "NO_REVERSAL_ACTION"}

    reversal_audit = []
    
    # step 1: Check tested side
    tested_status = check_tested_side(original_side, market_state, config)
    reversal_audit.append({"step": 1, "tested_status": getattr(tested_status, '__dict__', tested_status)})
    
    # If tested beyond threshold, reduce or close
    if getattr(tested_status, 'beyond_threshold', False):
        reduce_result = reduce_or_close(original_side, tested_status)
        apply_virtual_book_change(symbol_state, reduce_result)
        reversal_audit.append({"step": 1, "action": getattr(reduce_result, 'action', 'REDUCED')})

    # step 2: combined book cap
    combined_state = compute_combined_book(symbol_state, market_state)
    reversal_audit.append({"step": 2, "combined_state": getattr(combined_state, '__dict__', combined_state)})
    if not getattr(combined_state, 'within_caps', True):
        return {"action": "BLOCK", "reason": "REVERSAL_BLOCKED_COMBINED_CAP", "reversal_sequence_log": reversal_audit}

    candidate = select_candidate(side=reversal_side, persisted_label=persisted.label,
                                 dte=getattr(market_state, 'dte', 0), 
                                 atr_state=getattr(market_state, 'atr_state', {}),
                                 option_chain=getattr(market_state, 'option_chain', []))
    if not candidate:
        return {"action": "BLOCK", "reason": "REVERSAL_NO_VALID_CANDIDATE", "reversal_sequence_log": reversal_audit}

    return {
        "action": "OPEN_OR_ADD",
        "side": reversal_side,
        "strike": candidate['strike'],
        "delta": candidate['delta'],
        "premium": candidate['premium'],
        "reversal_sequence_log": reversal_audit
    }


