"""Multi-Leg Strategy Pre-Flight & Engine Alignment Validator.

Prevents LLM direction flips and enforces multi-leg risk rules (margin cap, net delta cap, leg limits).
"""
from dataclasses import dataclass
from src.engine.execution_parser import ParsedExecution

@dataclass
class ValidationResult:
    is_valid: bool
    rejection_reason: str = ""
    warnings: list[str] = None

    def __post_init__(self):
        if self.warnings is None:
            self.warnings = []


def _estimate_margin(legs: list[dict], underlying: float, symbol: str = "NIFTY") -> float:
    """Accurate estimation of broker margin requirement based on structure and lot size."""
    try:
        from src.engine.multileg_strategy import calculate_combined_margin
        norm_legs = []
        for leg in legs:
            l_copy = dict(leg)
            if "side" not in l_copy and "action" in l_copy:
                l_copy["side"] = l_copy["action"]
            if "lots" not in l_copy and "ratio" in l_copy:
                l_copy["lots"] = l_copy["ratio"]
            if "premium" not in l_copy and "entry_premium" in l_copy:
                l_copy["premium"] = l_copy["entry_premium"]
            norm_legs.append(l_copy)
        margin = calculate_combined_margin(norm_legs, symbol, underlying=underlying)
        if margin > 0:
            return float(margin)
    except Exception:
        pass

    from config.settings import LOT_SIZES
    base = symbol.upper().split()[0] if symbol else "NIFTY"
    lot_size = LOT_SIZES.get(base, LOT_SIZES.get(symbol, 75))
    margin = 0.0
    for leg in legs:
        if str(leg.get("action") or leg.get("side") or "").upper() == "SELL":
            ratio = float(leg.get("ratio") or leg.get("lots") or 1)
            margin += (underlying * ratio * lot_size * 0.15)
    return margin


from src.engine.verdict_sets import is_bullish as _is_bullish_canonical, is_bearish as _is_bearish_canonical


def _check_engine_direction(engine_verdict: str) -> tuple[bool, bool]:
    """Return (is_bullish, is_bearish) tuple respecting canonical verdict sets."""
    v = str(engine_verdict or "").strip()
    if _is_bullish_canonical(v):
        return True, False
    if _is_bearish_canonical(v):
        return False, True
    v_upper = v.upper()
    if "SHORT COVERING" in v_upper:
        return True, False
    if "LONG UNWINDING" in v_upper:
        return False, True
    is_bull = "BULLISH" in v_upper or "LONG" in v_upper
    is_bear = "BEARISH" in v_upper or "SHORT" in v_upper
    return is_bull, is_bear


def validate_multileg_trade(
    proposal: ParsedExecution,
    engine_verdict: str,
    underlying: float,
    max_margin_inr: float = 500000.0,
    max_net_delta: float = 0.60,
    symbol: str = "NIFTY",
) -> ValidationResult:
    """Validate multileg proposal against engine verdict and risk limits."""
    if not proposal.is_valid:
        return ValidationResult(is_valid=False, rejection_reason=proposal.rejection_reason)

    is_bullish_engine, is_bearish_engine = _check_engine_direction(engine_verdict)

    # Check direction conflict
    if is_bearish_engine and proposal.action == "GO_LONG":
        return ValidationResult(
            is_valid=False,
            rejection_reason=f"Direction conflict: Engine is {engine_verdict}, but proposal is GO_LONG."
        )
    if is_bullish_engine and proposal.action == "GO_SHORT":
        return ValidationResult(
            is_valid=False,
            rejection_reason=f"Direction conflict: Engine is {engine_verdict}, but proposal is GO_SHORT."
        )

    # Heavyweights Confluence Veto (for NSE/BSE equity indices)
    base_sym = symbol.upper().split()[0] if symbol else "NIFTY"
    if base_sym in ("NIFTY", "BANKNIFTY", "SENSEX"):
        try:
            from src.alerts.heavyweight_digest import get_heavyweights_confluence
            hw_conf = get_heavyweights_confluence() or {}
            if hw_conf:
                strat_upper = str(getattr(proposal, "strategy_type", None) or getattr(proposal, "strategy", None) or getattr(proposal, "structure", None) or "").upper()
                if hw_conf.get("veto_bear_calls"):
                    if strat_upper in ("BEAR_CALL_SPREAD", "BEAR_CALL") or (proposal.action == "GO_SHORT" and any(str(l.get("option_type") or "").upper() == "CE" and str(l.get("action") or l.get("side") or "").upper() == "SELL" for l in proposal.legs)):
                        return ValidationResult(
                            is_valid=False,
                            rejection_reason=f"Heavyweights Veto: Breadth is {hw_conf.get('breadth')} with upward velocity. Bear Call Spreads / short CE vetoed against institutional cash buying."
                        )
                if hw_conf.get("veto_bull_puts"):
                    if strat_upper in ("BULL_PUT_SPREAD", "BULL_PUT") or (proposal.action == "GO_LONG" and any(str(l.get("option_type") or "").upper() == "PE" and str(l.get("action") or l.get("side") or "").upper() == "SELL" for l in proposal.legs)):
                        return ValidationResult(
                            is_valid=False,
                            rejection_reason=f"Heavyweights Veto: Breadth is {hw_conf.get('breadth')} with downward velocity. Bull Put Spreads / short PE vetoed against institutional selling pressure."
                        )
        except Exception as hw_val_err:
            log.debug("[multileg_validator] Heavyweights confluence veto check failed gracefully: %s", hw_val_err)

    # Check max legs limit
    if len(proposal.legs) > 6:
        return ValidationResult(
            is_valid=False,
            rejection_reason=f"Risk limit exceeded: Proposal has {len(proposal.legs)} legs (max 6)."
        )

    # C12 FIX: Block CUSTOM structures from execution
    strategy_type = getattr(proposal, "strategy_type", None) or getattr(proposal, "strategy", None) or getattr(proposal, "structure", None) or ""
    if str(strategy_type).upper() == "CUSTOM":
        return ValidationResult(
            is_valid=False,
            rejection_reason="CUSTOM structures are not permitted for execution (undefined risk profile)."
        )

    # C11 FIX: Neutral structures (Iron Condors, Strangles, Straddles) require neutral/sideways engine regime
    if str(strategy_type).upper() in ("IRON_CONDOR", "SHORT_STRANGLE", "SHORT_STRADDLE"):
        if is_bullish_engine or is_bearish_engine:
            # Check if engine verdict is strongly directional
            from src.engine.verdict_sets import is_bullish, is_bearish
            if is_bullish(engine_verdict) or is_bearish(engine_verdict):
                return ValidationResult(
                    is_valid=False,
                    rejection_reason=f"Direction conflict: Neutral structure '{strategy_type}' cannot be entered against trending engine verdict '{engine_verdict}'."
                )

    # Approximate margin check
    estimated_margin = _estimate_margin(proposal.legs, underlying, symbol=symbol)
    if estimated_margin > max_margin_inr:
        return ValidationResult(
            is_valid=False,
            rejection_reason=f"Margin requirement exceeded: Estimated margin ₹{estimated_margin:.2f} > max ₹{max_margin_inr:.2f}."
        )

    # Net delta cap check (evaluated on the normalized strategy structure)
    ratios = [float(l.get("ratio") or 1.0) for l in proposal.legs if float(l.get("ratio") or 1.0) > 0]
    min_ratio = min(ratios) if ratios else 1.0

    net_delta = 0.0
    has_delta = False
    for leg in proposal.legs:
        if "delta" in leg and leg["delta"] is not None:
            has_delta = True
            raw_delta = abs(float(leg["delta"]))
            action = str(leg.get("action") or leg.get("side") or "SELL").upper()
            opt_type = str(leg.get("option_type") or "").upper()
            ratio = float(leg.get("ratio") or 1.0)
            norm_ratio = ratio / min_ratio if min_ratio > 0 else 1.0

            # CE delta is positive (+), PE delta is negative (-)
            und_delta = -raw_delta if opt_type == "PE" else raw_delta
            # BUY takes underlying delta (+), SELL negates it (-)
            pos_delta = und_delta if action == "BUY" else -und_delta
            net_delta += pos_delta * norm_ratio

    if has_delta and abs(net_delta) > max_net_delta:
        return ValidationResult(
            is_valid=False,
            rejection_reason=f"Risk limit exceeded: Proposal net delta {net_delta:+.2f} exceeds cap ±{max_net_delta:.2f}."
        )

    # Defined-risk wing width check
    strategy_type = getattr(proposal, "strategy_type", None) or getattr(proposal, "strategy", None) or getattr(proposal, "structure", None) or ""
    if str(strategy_type).upper() in ("IRON_CONDOR", "BEAR_CALL_SPREAD", "BULL_PUT_SPREAD"):
        from config.multileg_strategies import MIN_WING_WIDTH_PCT, MIN_WING_WIDTH_POINTS
        sym_key = symbol.upper().split()[0] if symbol else "DEFAULT"
        min_width_pts = MIN_WING_WIDTH_POINTS.get(sym_key, MIN_WING_WIDTH_POINTS.get("DEFAULT", 50.0))
        min_width_pct = MIN_WING_WIDTH_PCT.get(sym_key, MIN_WING_WIDTH_PCT.get("DEFAULT", 0.005))
        effective_min_width = max(min_width_pts, underlying * min_width_pct) if underlying > 0 else min_width_pts

        ce_sell = [float(l["strike"]) for l in proposal.legs if str(l.get("option_type") or "").upper() == "CE" and str(l.get("action") or l.get("side") or "").upper() == "SELL" and "strike" in l]
        ce_buy = [float(l["strike"]) for l in proposal.legs if str(l.get("option_type") or "").upper() == "CE" and str(l.get("action") or l.get("side") or "").upper() == "BUY" and "strike" in l]
        pe_sell = [float(l["strike"]) for l in proposal.legs if str(l.get("option_type") or "").upper() == "PE" and str(l.get("action") or l.get("side") or "").upper() == "SELL" and "strike" in l]
        pe_buy = [float(l["strike"]) for l in proposal.legs if str(l.get("option_type") or "").upper() == "PE" and str(l.get("action") or l.get("side") or "").upper() == "BUY" and "strike" in l]

        if ce_sell and ce_buy:
            call_width = max(ce_buy) - min(ce_sell)
            if call_width < effective_min_width:
                return ValidationResult(
                    is_valid=False,
                    rejection_reason=f"Call wing width {call_width:.0f} pts is too narrow for {symbol} (min required: {effective_min_width:.0f} pts). Buy leg is too close."
                )

        if pe_sell and pe_buy:
            put_width = max(pe_sell) - min(pe_buy)
            if put_width < effective_min_width:
                return ValidationResult(
                    is_valid=False,
                    rejection_reason=f"Put wing width {put_width:.0f} pts is too narrow for {symbol} (min required: {effective_min_width:.0f} pts). Buy leg is too close."
                )

    # ── Phase 2: Decision Matrix Rules & Strike Danger Ceilings ─────────────────
    from config.decision_matrix_loader import load_decision_matrix, get_matrix_mode
    matrix_cfg = load_decision_matrix()
    matrix_mode = get_matrix_mode(matrix_cfg)

    # 1. MCX Naked Ban (Always strictly enforced for capital safety)
    from src.engine.decision_matrix import MCX_SYMBOLS, delta_band
    is_mcx_sym = symbol.upper().split()[0] in MCX_SYMBOLS
    if is_mcx_sym and str(strategy_type).upper() in ("SHORT_STRANGLE", "SHORT_STRADDLE", "CUSTOM"):
        return ValidationResult(
            is_valid=False,
            rejection_reason=f"Naked options structure '{strategy_type}' is prohibited on MCX (defined-risk only)."
        )

    # 2. Strike ITM and Delta Danger Ceiling Checks
    for leg in proposal.legs:
        action = str(leg.get("action") or leg.get("side") or "").upper()
        opt_type = str(leg.get("option_type") or "").upper()
        strike = float(leg.get("strike") or 0.0)

        # Rejection: Sold leg must NOT be ITM
        if action == "SELL" and underlying > 0:
            if opt_type == "CE" and strike < underlying:
                return ValidationResult(
                    is_valid=False,
                    rejection_reason=f"ITM short leg rejected: CE strike {strike} is below spot {underlying}."
                )
            elif opt_type == "PE" and strike > underlying:
                return ValidationResult(
                    is_valid=False,
                    rejection_reason=f"ITM short leg rejected: PE strike {strike} is above spot {underlying}."
                )

        # Danger ceiling check
        if "delta" in leg and leg["delta"] is not None and strategy_type:
            abs_delta = abs(float(leg["delta"]))
            leg_role = "SHORT" if action == "SELL" else "LONG"
            _, _, danger_ceiling = delta_band(str(strategy_type), leg_role, symbol=symbol, cfg=matrix_cfg)
            if abs_delta > danger_ceiling:
                return ValidationResult(
                    is_valid=False,
                    rejection_reason=f"Delta danger ceiling breached: {action} {strike} {opt_type} delta {abs_delta:.2f} > max allowed {danger_ceiling:.2f}."
                )

    # 3. Credit-to-width rule (When in enforce mode)
    if matrix_mode == "enforce" and str(strategy_type).upper() in ("BULL_PUT_SPREAD", "BEAR_CALL_SPREAD", "IRON_CONDOR"):
        net_credit = float(getattr(proposal, "net_credit", 0.0) or 0.0)
        max_loss = float(getattr(proposal, "max_loss", 0.0) or 0.0)
        total_width = net_credit + max_loss
        if total_width > 0:
            credit_ratio = net_credit / total_width
            reject_below = float(matrix_cfg.get("credit_rules", {}).get("reject_below_ratio", 0.25))
            if credit_ratio < reject_below:
                return ValidationResult(
                    is_valid=False,
                    rejection_reason=f"Credit ratio {credit_ratio*100:.1f}% below minimum {reject_below*100:.0f}% of wing width."
                )

    # 4. Regime-cell structure check (enforce mode only)
    # Rejects LLM proposals whose structure is outside the allowed set for current
    # direction × vol regime. select_structure returns (recommended, allowed_list, reason).
    if matrix_mode == "enforce":
        try:
            from src.engine.decision_matrix import select_structure, vol_bucket
            from datetime import datetime
            import pytz as _pytz
            _IST = _pytz.timezone("Asia/Kolkata")
            _now_ist = datetime.now(_IST)
            # Resolve direction from engine verdict first, or infer from directional strategy types
            if is_bullish_engine:
                _direction = "BULLISH"
            elif is_bearish_engine:
                _direction = "BEARISH"
            elif str(strategy_type).upper() == "BULL_PUT_SPREAD":
                _direction = "BULLISH"
            elif str(strategy_type).upper() == "BEAR_CALL_SPREAD":
                _direction = "BEARISH"
            else:
                _direction = str(getattr(proposal, "direction", None) or "NEUTRAL").upper()
            _dte_val = getattr(proposal, "days_to_expiry", None)
            if _dte_val is None and proposal.legs:
                # Try to derive DTE from the first leg's expiry
                try:
                    from src.engine.multileg_paper_trading import _dte_from_expiry
                    _expiry_str = str(proposal.legs[0].get("expiry", "") or "")
                    _dte_val = _dte_from_expiry(_expiry_str) if _expiry_str else None
                except Exception:
                    _dte_val = None
            _is_event = bool(matrix_cfg.get("overrides", {}).get("event_window_active", False))
            _vol = vol_bucket(symbol, cfg=matrix_cfg)
            _recommended, _allowed, _reason = select_structure(
                direction=_direction,
                vol=_vol,
                dte=_dte_val,
                is_event=_is_event,
                symbol=symbol,
                now_ist=_now_ist,
                cfg=matrix_cfg,
            )
            if _recommended is None:
                return ValidationResult(
                    is_valid=False,
                    rejection_reason=f"Structure '{strategy_type}' rejected: regime matrix NO_TRADE ({_reason})."
                )
            _proposed_upper = str(strategy_type).upper()
            if _proposed_upper not in [s.upper() for s in _allowed]:
                return ValidationResult(
                    is_valid=False,
                    rejection_reason=(
                        f"Structure '{strategy_type}' is above regime cell — allowed for {_direction}/{_vol}: "
                        f"{_allowed}. ({_reason})"
                    )
                )
        except Exception as _sel_exc:
            import logging as _log
            _log.getLogger(__name__).warning(
                "[validator] select_structure check failed (non-blocking, skipping): %s", _sel_exc
            )

    return ValidationResult(is_valid=True)
