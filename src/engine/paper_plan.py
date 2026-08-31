"""Shared paper-trade plan builder for Telegram text and auto execution.

P3 fix (#13): MAX_LEVEL_DISTANCE_STEPS moved to config/settings.py.
  Imported from there so the value is tunable per-deployment without a
  code change. Local module constant removed.

L5 fix: MCX commodities (NATURALGAS, CRUDEOIL) now use options when ATM
  liquidity is sufficient (volume >= threshold AND OI >= threshold).
  Falls back to FUT when options are illiquid.
"""
from __future__ import annotations

import logging
import re

from config.symbol_classes import get_strike_step
from config.settings import MAX_LEVEL_DISTANCE_STEPS
from src.engine.confidence_threshold import get_effective_min_confidence
from src.engine.verdict_sets import BULLISH_VERDICTS, BEARISH_VERDICTS, is_bullish, is_bearish

log = logging.getLogger(__name__)

MIN_PAPER_CONFIDENCE = 65

# L5: Liquidity thresholds for MCX commodity options.
# If ATM option rows meet BOTH thresholds, use options instead of forced FUT.
_MCX_OPTION_MIN_VOLUME = 500    # minimum total volume (CE + PE) at ATM
_MCX_OPTION_MIN_OI = 2000       # minimum total open interest (CE + PE) at ATM

LONG_CE_VERDICTS = BULLISH_VERDICTS   # backward-compat alias
LONG_PE_VERDICTS = BEARISH_VERDICTS   # backward-compat alias
WRITING_VERDICTS = {"Put Writing", "Call Writing"}


def is_bullish_verdict(verdict: str) -> bool:
    return is_bullish(verdict)


def is_bearish_verdict(verdict: str) -> bool:
    return is_bearish(verdict)


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _round_to_step(value: float, step: float) -> float:
    if step <= 0:
        return round(value, 2)
    return round(round(value / step) * step, 2)


def _near_level(level: float, underlying: float, step: float, direction: str) -> float | None:
    if level <= 0 or underlying <= 0:
        return None
    distance = abs(level - underlying)
    if distance > step * MAX_LEVEL_DISTANCE_STEPS:
        return None
    if direction == "above" and level > underlying:
        return level
    if direction == "below" and level < underlying:
        return level
    return None


def mcx_option_liquidity_ok(symbol: str, atm_strike: float, ctx: dict) -> bool:
    """
    L5: Check if ATM option liquidity is sufficient for MCX commodities.
    Returns True if BOTH CE and PE options at the ATM strike meet the required thresholds.
    Falls back to FUT if either threshold fails or data is unavailable.
    """
    if atm_strike <= 0:
        return False

    option_rows = ctx.get("option_rows") or []
    ce_vol = 0
    ce_oi = 0
    pe_vol = 0
    pe_oi = 0
    found_ce = False
    found_pe = False

    for row in option_rows:
        try:
            row_strike = float(row.get("strike") or 0)
            if abs(row_strike - atm_strike) < 0.01:
                opt_type = str(row.get("option_type") or "").upper()
                vol = int(row.get("volume") or 0)
                oi = int(row.get("oi") or 0)
                if opt_type == "CE":
                    ce_vol = vol
                    ce_oi = oi
                    found_ce = True
                elif opt_type == "PE":
                    pe_vol = vol
                    pe_oi = oi
                    found_pe = True
        except (ValueError, TypeError):
            continue

    if not found_ce or not found_pe:
        log.debug("%s: MCX liquidity check — ATM CE or PE row missing, falling back to FUT", symbol)
        return False

    min_vol = _MCX_OPTION_MIN_VOLUME // 2
    min_oi = _MCX_OPTION_MIN_OI // 2

    ce_ok = ce_vol >= min_vol and ce_oi >= min_oi
    pe_ok = pe_vol >= min_vol and pe_oi >= min_oi

    if ce_ok and pe_ok:
        log.debug(
            "%s: MCX liquidity OK — CE vol=%d, PE vol=%d (min=%d); CE OI=%d, PE OI=%d (min=%d). Using options.",
            symbol, ce_vol, pe_vol, min_vol, ce_oi, pe_oi, min_oi,
        )
        return True

    log.debug(
        "%s: MCX liquidity INSUFFICIENT — CE ok=%s (vol=%d, oi=%d), PE ok=%s (vol=%d, oi=%d). "
        "Falling back to FUT.",
        symbol, ce_ok, ce_vol, ce_oi, pe_ok, pe_vol, pe_oi,
    )
    return False


VERDICT_ACTION_MAP = {
    # Bullish — OI labels
    "Long Buildup":    ("SELL", "PE"), # Handled by TFSS v4
    "Put Writing":     ("SELL", "PE"), # Handled by TFSS v4
    "Short Covering":  ("SELL", "PE"), # Handled by TFSS v4
    "OI Bias Bullish": ("SELL", "PE"), # Handled by TFSS v4
    # Bearish — OI labels
    "Short Buildup":   ("SELL", "CE"), # Handled by TFSS v4
    "Call Writing":    ("SELL", "CE"), # Handled by TFSS v4
    "Long Unwinding":  ("SELL", "CE"), # Handled by TFSS v4
    "OI Bias Bearish": ("SELL", "CE"), # Handled by TFSS v4
    # Neutral — ideal for option selling (strangle/straddle)
    "Sideways":              ("SELL", "STRANGLE"), # Sell both CE + PE
    "Volatility Expansion":  ("SELL", "STRANGLE"), # Sell premium into high IV
    "Volatility Contraction": ("SELL", "STRANGLE"), # Sell before IV expansion
    # LLM action labels — map to canonical option actions
    "GO_LONG":         ("SELL", "PE"), # Handled by TFSS v4
    "GO_SHORT":        ("SELL", "CE"), # Handled by TFSS v4
}


def build_paper_trade_plan(verdict: str, confidence: int, ctx: dict) -> dict | None:
    """Return the executable paper plan, or None when no clean auto entry exists."""
    from config.settings import PAPER_RESEARCH_MODE

    min_conf = get_effective_min_confidence()
    if int(confidence or 0) < min_conf:
        # In research mode, allow lower-confidence trades through for observation
        if not PAPER_RESEARCH_MODE:
            return None
        # Research mode: require at least 40% confidence (down from 65%)
        if int(confidence or 0) < 40:
            return None

    symbol = str(ctx.get("symbol") or "").upper()
    underlying = _safe_float(ctx.get("underlying"))
    if underlying <= 0:
        return None

    verdict_str = str(verdict or "")
    if verdict_str not in VERDICT_ACTION_MAP:
        return None

    side, option_type = VERDICT_ACTION_MAP[verdict_str]
    bullish = is_bullish(verdict_str)

    # Merge CORE to TFSS only if TFSS strategy is explicitly enabled in settings.
    from config.runtime_config import load_runtime_config
    rconf = load_runtime_config()
    tfss_enabled = bool(rconf.get("strategies", {}).get("TFSS", {}).get("enabled", False))

    setup_type = "CORE"
    if tfss_enabled:
        from src.engine.trend_following_short_strangle import normalize_core_verdict_to_tfss_intent
        tfss_intent = normalize_core_verdict_to_tfss_intent(verdict_str)

        tfss_side = ctx.get("_tfss_execution_side") or (
            "SELL_PE" if tfss_intent and tfss_intent.bias == "BULLISH" else
            "SELL_CE" if tfss_intent and tfss_intent.bias == "BEARISH" else None
        )

        if tfss_side in ("SELL_PE", "SELL_CE"):
            side = "SELL"
            option_type = "PE" if tfss_side == "SELL_PE" else "CE"
            setup_type = "TFSS"

            # Support LLM override of option_type for GO_LONG/GO_SHORT if available
            if verdict_str in ("GO_LONG", "GO_SHORT") or ctx.get("instrument"):
                llm_instr = str(ctx.get("instrument") or "")
                if re.search(r"\bCE\b", llm_instr, re.IGNORECASE):
                    option_type = "CE"
                elif re.search(r"\bPE\b", llm_instr, re.IGNORECASE):
                    option_type = "PE"
    elif verdict_str in ("GO_LONG", "GO_SHORT"):
        llm_instr = str(ctx.get("instrument") or "")
        if re.search(r"\bCE\b", llm_instr, re.IGNORECASE):
            option_type = "CE"
        elif re.search(r"\bPE\b", llm_instr, re.IGNORECASE):
            option_type = "PE"

    step = float(get_strike_step(symbol) or 1)
    atm = _safe_float(ctx.get("atm_strike")) or _round_to_step(underlying, step)

    # L5: MCX commodities — use options when ATM liquidity is sufficient,
    # otherwise fall back to FUT. Previously forced FUT unconditionally.
    is_mcx_commodity = "NATURALGAS" in symbol or "CRUDEOIL" in symbol
    if is_mcx_commodity:
        use_options = mcx_option_liquidity_ok(symbol, atm, ctx)
        if not use_options:
            option_type = "FUT"
            # TFSS v4: FUT fallback still uses SELL direction for short-premium strategy.
            # Bullish → SELL FUT (short futures), Bearish → still SELL (no change needed for direction).
            side = "SELL"
        # else: keep the original option_type (CE/PE) and side from VERDICT_ACTION_MAP
    support = _safe_float(ctx.get("support"))
    resistance = _safe_float(ctx.get("resistance"))

    # ── STRANGLE: select both CE and PE legs ───────────────────────────
    if option_type == "STRANGLE":
        from src.engine.trade_plan import select_candidate, get_option_premium, get_atr

        symbol_str = str(ctx.get("symbol") or symbol)
        expiry_str = str(ctx.get("expiry") or "")
        option_rows = ctx.get("option_rows") or []
        if not option_rows:
            return None
        dte_val = int(ctx.get("dte") or 0)
        min_premium_threshold = 0.001 * underlying  # 0.1% of underlying

        legs = []
        for leg_type in ("CE", "PE"):
            effective_threshold = min_premium_threshold * 0.90
            leg_strike = None
            leg_premium = None

            # 1. Try candidate selection, but enforce OTM
            tfss_side = "SELL_PE" if leg_type == "PE" else "SELL_CE"
            cand = select_candidate(
                side=tfss_side,
                persisted_label="BULLISH" if leg_type == "PE" else "BEARISH",
                dte=dte_val,
                atr_state={"underlying": underlying},
                option_chain=option_rows,
            )
            if cand and cand.get("strike"):
                cand_strike = float(cand["strike"])
                # Enforce OTM: CE strike > ATM, PE strike < ATM
                is_otm = (leg_type == "CE" and cand_strike > underlying) or \
                         (leg_type == "PE" and cand_strike < underlying)
                if is_otm:
                    cand_prem = get_option_premium(symbol_str, expiry_str, cand_strike, leg_type, option_rows)
                    if cand_prem is not None and cand_prem >= effective_threshold:
                        leg_strike = cand_strike
                        leg_premium = cand_prem

            # 2. Step outward from ATM to find OTM strike with sufficient premium
            if leg_strike is None:
                cur = _round_to_step(underlying + step if leg_type == "CE" else underlying - step, step)
                for _ in range(10):
                    prem = get_option_premium(symbol_str, expiry_str, cur, leg_type, option_rows)
                    if prem is not None and prem >= effective_threshold:
                        leg_strike = cur
                        leg_premium = prem
                        break
                    cur = _round_to_step(cur + step if leg_type == "CE" else cur - step, step)

            # 3. Final fallback: best available OTM strike
            if leg_strike is None:
                cur = _round_to_step(underlying + step if leg_type == "CE" else underlying - step, step)
                prem = get_option_premium(symbol_str, expiry_str, cur, leg_type, option_rows)
                if prem is not None and prem > 0:
                    leg_strike = cur
                    leg_premium = prem

            if leg_strike is None:
                log.debug("[paper_plan] %s STRANGLE: failed to find %s leg — blocking", symbol, leg_type)
                return None

            legs.append({
                "option_type": leg_type,
                "strike": leg_strike,
                "premium": leg_premium,
            })

        ce_leg = legs[0]
        pe_leg = legs[1]
        net_premium = (ce_leg["premium"] or 0) + (pe_leg["premium"] or 0)

        return {
            "symbol": symbol,
            "verdict_label": verdict,
            "side": "SELL",
            "option_type": "STRANGLE",
            "strike": ce_leg["strike"],  # primary reference
            "premium": net_premium,
            "sl": None,  # managed at book level, not per-leg
            "target": None,
            "sl_underlying": round(underlying * 0.98, 4),
            "target_underlying": round(underlying * 1.02, 4),
            "atr": get_atr(ctx),
            "setup_type": "CORE",
            "legs": [
                {"option_type": "CE", "strike": ce_leg["strike"], "premium": ce_leg["premium"]},
                {"option_type": "PE", "strike": pe_leg["strike"], "premium": pe_leg["premium"]},
            ],
        }

    # Strike selection: OTM for SELL, ATM for BUY
    if option_type in ("CE", "PE"):
        if side == "SELL":
            from src.engine.trade_plan import select_candidate, get_option_premium
            min_premium_threshold = 0.001 * underlying  # 0.1% of underlying index spot price
            symbol_str = str(ctx.get("symbol") or symbol)
            expiry_str = str(ctx.get("expiry") or "")
            option_rows = ctx.get("option_rows") or []
            if not option_rows:
                strike = _round_to_step(underlying + step if option_type == "CE" else underlying - step, step)
                selected_strike = strike
                selected_premium = round(0.01 * underlying, 2)
            else:
                dte_val = int(ctx.get("dte") or 0)
                
                selected_strike = None
                selected_premium = None

                # 1. Try DTE-Delta Band candidate selection (Requirement 1)
                tfss_side = "SELL_PE" if option_type == "PE" else "SELL_CE"
                cand = select_candidate(
                    side=tfss_side,
                    persisted_label="BULLISH" if option_type == "PE" else "BEARISH",
                    dte=dte_val,
                    atr_state={"underlying": underlying},
                    option_chain=option_rows
                )
                
                effective_threshold = min_premium_threshold * 0.90  # 10% tolerance buffer (e.g. 21.98 instead of 24.42)

                if cand and cand.get("strike"):
                    cand_strike = float(cand["strike"])
                    cand_prem = get_option_premium(symbol_str, expiry_str, cand_strike, option_type, option_rows)
                    if cand_prem is not None and cand_prem >= effective_threshold:
                        selected_strike = cand_strike
                        selected_premium = cand_prem
                        log.info(
                            "[paper_plan] %s %s DTE-Delta Band strike %.1f selected with premium %.2f (>= threshold %.2f, delta %.2f)",
                            symbol_str, setup_type, cand_strike, cand_prem, effective_threshold, cand.get("delta", 0.0)
                        )

                # 2. If DTE-Delta candidate is unavailable or < threshold, escalate to Multi-Wall OI check (Requirement 2)
                if selected_strike is None:
                    if option_type == "CE":
                        raw_walls = ctx.get("resistance_walls") or ([resistance] if resistance else [])
                    else:
                        raw_walls = ctx.get("support_walls") or ([support] if support else [])
                    
                    walls = [w for w in raw_walls if w is not None]
                    if not walls:
                        fallback_w = underlying + (step * MAX_LEVEL_DISTANCE_STEPS if option_type == "CE" else -step * MAX_LEVEL_DISTANCE_STEPS)
                        walls = [fallback_w]

                    for idx, wall_strike in enumerate(walls, start=1):
                        cand_strike = _round_to_step(wall_strike, step)
                        if option_type == "CE" and cand_strike < underlying:
                            cand_strike = _round_to_step(underlying + step, step)
                        elif option_type == "PE" and cand_strike > underlying:
                            cand_strike = _round_to_step(underlying - step, step)
                            
                        prem = get_option_premium(symbol_str, expiry_str, cand_strike, option_type, option_rows)
                        
                        if prem is not None and prem >= effective_threshold:
                            selected_strike = cand_strike
                            selected_premium = prem
                            log.info(
                                "[paper_plan] %s %s Wall %d strike %.1f selected with premium %.2f (>= threshold %.2f)",
                                symbol_str, option_type, idx, cand_strike, prem, effective_threshold
                            )
                            break
                        else:
                            log.info(
                                "[paper_plan] %s %s Wall %d strike %.1f premium %s < threshold (%.2f), checking next wall...",
                                symbol_str, option_type, idx, cand_strike, f"{prem:.2f}" if prem is not None else "None", effective_threshold
                            )

                # 3. If OI walls still failed, step inward towards ATM to find nearest OTM strike with premium >= effective_threshold
                if selected_strike is None:
                    log.info("[paper_plan] %s %s: OI walls below threshold; stepping inward towards ATM...", symbol_str, option_type)
                    cur_strike = _round_to_step(underlying + step if option_type == "CE" else underlying - step, step)
                    max_steps = 10
                    for _ in range(max_steps):
                        prem = get_option_premium(symbol_str, expiry_str, cur_strike, option_type, option_rows)
                        if prem is not None and prem >= effective_threshold:
                            selected_strike = cur_strike
                            selected_premium = prem
                            log.info(
                                "[paper_plan] %s %s fallback OTM strike %.1f selected with premium %.2f (>= threshold %.2f)",
                                symbol_str, option_type, cur_strike, prem, effective_threshold
                            )
                            break
                        if option_type == "CE":
                            cur_strike = _round_to_step(cur_strike - step, step)
                            if cur_strike < underlying:
                                break
                        else:
                            cur_strike = _round_to_step(cur_strike + step, step)
                            if cur_strike > underlying:
                                break

                # 4. Final safety fallback: use the first wall or ATM+1 step if premium > 0
                if selected_strike is None:
                    first_wall = walls[0] if 'walls' in locals() and walls else (underlying + step if option_type == "CE" else underlying - step)
                    cand_strike = _round_to_step(first_wall, step)
                    if option_type == "CE" and cand_strike <= underlying:
                        cand_strike = _round_to_step(underlying + step, step)
                    elif option_type == "PE" and cand_strike >= underlying:
                        cand_strike = _round_to_step(underlying - step, step)
                    prem = get_option_premium(symbol_str, expiry_str, cand_strike, option_type, option_rows)
                    if prem is not None and prem > 0:
                        selected_strike = cand_strike
                        selected_premium = prem
                        log.warning(
                            "[paper_plan] %s %s FINAL FALLBACK to strike %.1f with premium %.2f (< threshold %.2f)",
                            symbol_str, option_type, cand_strike, prem, effective_threshold
                        )

                if selected_strike is not None:
                    strike = selected_strike
                else:
                    log.debug(
                        "[paper_plan] %s %s: All DTE-Delta candidates & OI walls failed premium checks. Trade blocked.",
                        symbol_str, option_type
                    )
                    return None
        else:
            strike = atm
    else:
        # FUT
        strike = atm

    # ATR for SL/Target calculation — fall back to step-based ATR when candle ATR unavailable.
    from src.engine.trade_plan import get_atr
    atr = get_atr(ctx)
    if not atr or atr <= 0:
        step = float(get_strike_step(symbol) or 1.0)
        atr = round(step * 1.5, 4)
        log.info("%s: Missing candle ATR data — using step-based ATR fallback (%.2f)", symbol, atr)

    if bullish:
        sl = underlying - 1.5 * atr
        target = underlying + 2.0 * atr
    else:
        sl = underlying + 1.5 * atr
        target = underlying - 2.0 * atr

    return {
        "symbol": symbol,
        "verdict_label": verdict,
        "side": side,
        "option_type": option_type,
        "strike": strike,
        "entry_underlying": underlying,
        "sl_underlying": round(sl, 4),
        "target_underlying": round(target, 4),
        "confidence": int(confidence or 0),
        "setup_type": setup_type,
    }


def format_paper_plan(plan: dict | None) -> str:
    if not plan:
        return "No auto paper trade: wait for cleaner alignment"
    symbol = plan.get("symbol", "")
    strike = plan.get("strike")
    opt = plan.get("option_type")
    sl = plan.get("sl_underlying")
    target = plan.get("target_underlying")
    side = str(plan.get("side", "BUY")).title()

    if opt == "STRANGLE":
        legs = plan.get("legs") or []
        ce_stk = legs[0].get("strike") if len(legs) > 0 else "N/A"
        pe_stk = legs[1].get("strike") if len(legs) > 1 else "N/A"
        net_p = plan.get("premium", 0.0)
        return f"Sell Short Strangle {symbol} | CE {ce_stk} + PE {pe_stk} @ Net Prem ₹{net_p:.2f} (Book SL/Target)"

    # BUG FIX: Add None checks for sl and target before formatting
    if sl is None or target is None:
        log.warning("format_paper_plan: sl or target is None for %s — returning fallback", symbol or "N/A")
        return "No auto paper trade: invalid SL/Target levels"

    is_commodity = symbol.upper() in {"NATURALGAS", "CRUDEOIL", "GOLD", "SILVER"}
    if is_commodity and opt == "FUT":
        return (f"{side} {opt} (Commodity) at current scan "
                f"| SL spot {sl:g} | Target spot {target:g}")
    if opt == "FUT":
        return (
            f"{side} {opt} at current scan "
            f"| SL spot {sl:g} | Target spot {target:g}"
        )
    return (
        f"{side} {strike:g} {opt} at current scan "
        f"| SL spot {sl:g} | Target spot {target:g}"
    )
