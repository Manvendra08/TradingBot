"""
Anomaly Detection Engine v2.8
Pure functions — no DB/Telegram side effects.
Returns alert dicts with 'severity' field (HIGH | MEDIUM | LOW).

Performance fix (v2.7), Anomaly Logic Overhaul (v2.8):
  - All detection functions now receive `prev_snap` dict pre-fetched by
    detect_anomalies() via a single get_prev_snapshots_bulk() call.
  - Eliminates 300+ SQLite round trips per pipeline run.
"""
import json
import logging
from datetime import datetime, timezone

from config.settings import (
    OI_SPIKE_THRESHOLD_PCT,
    PRICE_SPIKE_THRESHOLD_PCT,
    PCR_EXTREME_LOW, PCR_EXTREME_HIGH, PCR_SHIFT_THRESHOLD, PCR_EXTREME_SEVERITY_BAND,
    IV_SPIKE_ATM_THRESHOLD,
    MAX_PAIN_SHIFT_THRESHOLD,
    STRIKES_AROUND_ATM,
    SEVERITY_HIGH_MULT, SEVERITY_MED_MULT,
    BUILDUP_OI_MIN_PCT, BUILDUP_LTP_MIN_PCT,
    OTM_STRIKE_RANGE, OTM_OI_SPIKE_PCT,
    VOLUME_AGGRESSION_HIGH, VOLUME_AGGRESSION_LOW,
    IV_CRUSH_THRESHOLD,
    STRADDLE_DELTA_PCT,
    ATM_LEG_MOVE_PCT,
    PCR_VELOCITY_WINDOW,
)
from src.models.schema import (
    get_prev_snapshots_bulk,
    get_previous_underlying,
    get_previous_underlying_before,
    get_previous_snapshot,
    get_latest_snapshots_for_symbol,
    get_latest_n_snapshots,
)

log = logging.getLogger(__name__)

# Type alias for the bulk prev-snapshot dict
PrevByKey = dict[tuple, dict]


class _PrevSnapshotLookup(dict):
    def __init__(self, data: PrevByKey, symbol: str, expiry: str):
        super().__init__(data)
        self._symbol = symbol
        self._expiry = expiry

    def get(self, key, default=None):
        value = super().get(key)
        if value is not None:
            return value
        strike, option_type = key
        value = get_previous_snapshot(self._symbol, self._expiry, strike, option_type)
        return default if value is None else value


# ── Helpers ────────────────────────────────────────────────────────────────

def _pct_change(old: float, new: float) -> float | None:
    if old and old != 0:
        return round((new - old) / abs(old) * 100, 2)
    return None


def _score_severity(ratio: float) -> str:
    if ratio >= SEVERITY_HIGH_MULT:
        return "HIGH"
    if ratio >= SEVERITY_MED_MULT:
        return "MEDIUM"
    return "LOW"


def _make_alert(alert_type: str, symbol: str, expiry: str, strike: float | None,
                option_type: str | None, detail: dict,
                severity: str = "LOW") -> dict:
    return {
        "fired_at":    datetime.now(timezone.utc).isoformat(),
        "symbol":      symbol,
        "alert_type":  alert_type,
        "strike":      strike,
        "option_type": option_type,
        "expiry":      expiry,
        "detail_json": json.dumps(detail),
        "telegram_sent": 0,
        "severity":    severity,
        "digest_id":   None,
    }


# ── ATM detection ────────────────────────────────────────────────────────────

def _atm_strike(strikes_data: list[dict], underlying: float) -> float:
    all_strikes = sorted({r["strike"] for r in strikes_data})
    return min(all_strikes, key=lambda s: abs(s - underlying))


# ── Max Pain ───────────────────────────────────────────────────────────────

def _compute_max_pain(strikes_data: list[dict]) -> float | None:
    ce_map = {r["strike"]: r["oi"] for r in strikes_data if r["option_type"] == "CE"}
    pe_map = {r["strike"]: r["oi"] for r in strikes_data if r["option_type"] == "PE"}
    all_strikes = sorted(set(ce_map) | set(pe_map))
    if not all_strikes:
        return None
    min_pain = None
    max_pain_strike = None
    for candidate in all_strikes:
        pain = sum((candidate - s) * oi for s, oi in ce_map.items() if candidate > s)
        pain += sum((s - candidate) * oi for s, oi in pe_map.items() if candidate < s)
        if min_pain is None or pain < min_pain:
            min_pain = pain
            max_pain_strike = candidate
    return max_pain_strike


# ── PCR ──────────────────────────────────────────────────────────────────

def _compute_pcr(strikes_data: list[dict]) -> float | None:
    total_ce = sum(r["oi"] for r in strikes_data if r["option_type"] == "CE")
    total_pe = sum(r["oi"] for r in strikes_data if r["option_type"] == "PE")
    if total_ce == 0:
        return None
    return round(total_pe / total_ce, 4)


# ── OI wall helpers ───────────────────────────────────────────────────────────

def _oi_wall(strikes_data: list[dict]) -> dict:
    """Return strike with max CE OI (resistance) and max PE OI (support).

    If both sides resolve to the same strike, attempt to pick the next best
    distinct level to avoid zero-width ranges.
    """
    ce_rows = [r for r in strikes_data if r["option_type"] == "CE" and r.get("oi")]
    pe_rows = [r for r in strikes_data if r["option_type"] == "PE" and r.get("oi")]
    if not ce_rows or not pe_rows:
        return {"resistance": None, "support": None}

    # Absolute max
    res_row = max(ce_rows, key=lambda r: r["oi"])
    sup_row = max(pe_rows, key=lambda r: r["oi"])
    resistance = res_row["strike"]
    support    = sup_row["strike"]

    if support == resistance:
        # Sort both sides by OI
        ce_sorted = sorted(ce_rows, key=lambda r: r["oi"], reverse=True)
        pe_sorted = sorted(pe_rows, key=lambda r: r["oi"], reverse=True)
        
        # Try finding next best resistance that is not the support
        alt_res = next((r["strike"] for r in ce_sorted if r["strike"] != support), None)
        # Try finding next best support that is not the resistance
        alt_sup = next((r["strike"] for r in pe_sorted if r["strike"] != resistance), None)
        
        if alt_res is not None and alt_sup is not None:
            # Pick the one with higher relative OI significance
            # For now, just shift the resistance up if it was the same as support
            if alt_res > support:
                resistance = alt_res
            elif alt_sup < resistance:
                support = alt_sup
            else:
                # Fallback: just pick one to shift
                resistance = alt_res
        elif alt_res is not None:
            resistance = alt_res
        elif alt_sup is not None:
            support = alt_sup
        else:
            # Complete overlap on single strike - can't find distinct
            support = None
            resistance = None

    return {"resistance": resistance, "support": support}


def _key_levels(strikes_data: list[dict], underlying: float) -> dict:
    """
    Derive trader-usable levels:
      - Support: max PE OI at/below underlying (fallback global PE max)
      - Resistance: max CE OI at/above underlying (fallback global CE max)
      - Max pain: computed from full strike set
    """
    ce_rows = [r for r in strikes_data if r.get("option_type") == "CE" and (r.get("oi") or 0) > 0]
    pe_rows = [r for r in strikes_data if r.get("option_type") == "PE" and (r.get("oi") or 0) > 0]
    support = None
    resistance = None

    if pe_rows:
        pe_below = [r for r in pe_rows if r.get("strike") is not None and r["strike"] <= underlying]
        support = (max(pe_below, key=lambda r: r["oi"]) if pe_below else max(pe_rows, key=lambda r: r["oi"]))["strike"]
    if ce_rows:
        ce_above = [r for r in ce_rows if r.get("strike") is not None and r["strike"] >= underlying]
        resistance = (max(ce_above, key=lambda r: r["oi"]) if ce_above else max(ce_rows, key=lambda r: r["oi"]))["strike"]

    if support is not None and resistance is not None and support >= resistance:
        all_strikes = sorted({r["strike"] for r in strikes_data if r.get("strike") is not None})
        lower = [s for s in all_strikes if s < underlying]
        upper = [s for s in all_strikes if s > underlying]
        if lower:
            support = lower[-1]
        if upper:
            resistance = upper[0]

    return {
        "support": support,
        "resistance": resistance,
        "max_pain": _compute_max_pain(strikes_data),
    }



# ── Strike filter ─────────────────────────────────────────────────────────────

def _filter_atm_range(strikes_data: list[dict], underlying: float) -> list[dict]:
    if STRIKES_AROUND_ATM <= 0:
        return strikes_data
    atm = _atm_strike(strikes_data, underlying)
    sorted_strikes = sorted({r["strike"] for r in strikes_data})
    try:
        idx = sorted_strikes.index(atm)
    except ValueError:
        return strikes_data
    lo = sorted_strikes[max(0, idx - STRIKES_AROUND_ATM)]
    hi = sorted_strikes[min(len(sorted_strikes) - 1, idx + STRIKES_AROUND_ATM)]
    return [r for r in strikes_data if lo <= r["strike"] <= hi]


def _filter_otm_only(strikes_data: list[dict], underlying: float) -> list[dict]:
    """
    Return strikes strictly beyond ATM \u00b1 OTM_STRIKE_RANGE.
    """
    atm = _atm_strike(strikes_data, underlying)
    sorted_strikes = sorted({r["strike"] for r in strikes_data})
    try:
        idx = sorted_strikes.index(atm)
    except ValueError:
        return []
    # Boundary: last strike that is still within the ATM range
    lo_idx = max(0, idx - OTM_STRIKE_RANGE)
    hi_idx = min(len(sorted_strikes) - 1, idx + OTM_STRIKE_RANGE)
    lo_bound = sorted_strikes[lo_idx]
    hi_bound = sorted_strikes[hi_idx]
    return [r for r in strikes_data if r["strike"] < lo_bound or r["strike"] > hi_bound]


# \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
# DETECTION RULES
# \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550

def _detect_oi_spike_unwind(filtered: list[dict], symbol: str, expiry: str,
                             underlying: float, prev_by_key: PrevByKey,
                             oi_thresh: float = OI_SPIKE_THRESHOLD_PCT) -> list[dict]:
    alerts = []
    for row in filtered:
        strike = row["strike"]
        ot     = row["option_type"]
        prev   = prev_by_key.get((strike, ot))
        if not prev:
            continue
        prev_oi = prev.get("oi") or 0
        curr_oi = row.get("oi") or 0
        pct     = _pct_change(prev_oi, curr_oi)
        if pct is None or abs(pct) < oi_thresh:
            continue

        sev = _score_severity(abs(pct) / oi_thresh)
        atype = "OI_SPIKE" if pct > 0 else "OI_UNWIND"
        detail = {
            "strike": strike, "option_type": ot,
            "prev_oi": prev_oi, "curr_oi": curr_oi, "pct_change": pct,
            "prev_ltp": prev.get("ltp"), "curr_ltp": row.get("ltp"),
            "underlying": underlying,
        }
        log.info("[engine] %s | %s %.0f %s: OI %d\u2192%d (%.1f%%) [%s]",
                 symbol, atype, strike, ot, prev_oi, curr_oi, pct, sev)
        alerts.append(_make_alert(atype, symbol, expiry, strike, ot, detail, sev))
    return alerts


def _detect_buildup(filtered: list[dict], symbol: str, expiry: str,
                    underlying: float, prev_by_key: PrevByKey,
                    oi_min_pct: float = BUILDUP_OI_MIN_PCT,
                    ltp_min_pct: float = BUILDUP_LTP_MIN_PCT) -> list[dict]:
    alerts = []
    for row in filtered:
        strike = row["strike"]
        ot     = row["option_type"]
        prev   = prev_by_key.get((strike, ot))
        if not prev:
            continue
        prev_oi  = prev.get("oi") or 0
        curr_oi  = row.get("oi") or 0
        prev_ltp = prev.get("ltp") or 0
        curr_ltp = row.get("ltp") or 0
        oi_pct  = _pct_change(prev_oi, curr_oi)
        ltp_pct = _pct_change(prev_ltp, curr_ltp)
        if oi_pct is None or ltp_pct is None:
            continue
        if abs(oi_pct) < oi_min_pct or abs(ltp_pct) < ltp_min_pct:
            continue

        oi_up  = oi_pct  > 0
        ltp_up = ltp_pct > 0

        if oi_up and ltp_up:
            label = "Long Buildup"
        elif oi_up and not ltp_up:
            label = "Short Buildup"
        elif not oi_up and not ltp_up:
            label = "Long Unwinding"
        else:
            label = "Short Covering"

        sev = _score_severity(abs(oi_pct) / oi_min_pct)
        detail = {
            "strike": strike, "option_type": ot, "buildup_type": label,
            "oi_pct": oi_pct, "ltp_pct": ltp_pct,
            "prev_oi": prev_oi, "curr_oi": curr_oi,
            "prev_ltp": prev_ltp, "curr_ltp": curr_ltp,
            "underlying": underlying,
        }
        log.info("[engine] BUILDUP | %s %.0f %s: %s oi=%.1f%% ltp=%.1f%% [%s]",
                 symbol, strike, ot, label, oi_pct, ltp_pct, sev)
        alerts.append(_make_alert("BUILDUP_CLASSIFY", symbol, expiry, strike, ot, detail, sev))
    return alerts


def _detect_price_spike(symbol: str, expiry: str, underlying: float) -> list[dict]:
    alerts = []
    prev_row = get_previous_underlying(symbol)
    if not prev_row:
        return alerts
    prev_price = prev_row["price"]
    pct = _pct_change(prev_price, underlying)
    if pct is None or abs(pct) < PRICE_SPIKE_THRESHOLD_PCT:
        return alerts
    sev = _score_severity(abs(pct) / PRICE_SPIKE_THRESHOLD_PCT)
    detail = {
        "prev_price": prev_price, "curr_price": underlying,
        "pct_change": pct, "direction": "UP" if pct > 0 else "DOWN",
    }
    log.info("[engine] PRICE_SPIKE | %s: %.2f\u2192%.2f (%.2f%%) [%s]",
             symbol, prev_price, underlying, pct, sev)
    alerts.append(_make_alert("PRICE_SPIKE", symbol, expiry, None, None, detail, sev))
    return alerts


def _detect_pcr_velocity(symbol: str, expiry: str, underlying: float,
                          curr_pcr: float | None) -> list[dict]:
    if curr_pcr is None:
        return []
    snapshots = get_latest_n_snapshots(symbol, expiry, PCR_VELOCITY_WINDOW)
    if len(snapshots) < 2:
        return []
    pcr_series = []
    for snap in snapshots:
        p = _compute_pcr(snap)
        if p is not None:
            pcr_series.append(p)
    pcr_series = [curr_pcr] + pcr_series
    if len(pcr_series) < 3:
        return []
    diffs = [pcr_series[i] - pcr_series[i + 1] for i in range(len(pcr_series) - 1)]
    if all(d > 0 for d in diffs):
        direction, label = "rising", "Bulls gaining control"
    elif all(d < 0 for d in diffs):
        direction, label = "falling", "Bears gaining control"
    else:
        return []
    slope = round(sum(diffs) / len(diffs), 4)
    detail = {
        "pcr_series": pcr_series[:PCR_VELOCITY_WINDOW],
        "slope": slope, "direction": direction, "label": label,
    }
    return [_make_alert("PCR_VELOCITY", symbol, expiry, None, None, detail, "MEDIUM")]


def _detect_iv_spike_crush(filtered: list[dict], symbol: str, expiry: str,
                            underlying: float, prev_by_key: PrevByKey) -> list[dict]:
    alerts = []
    atm = _atm_strike(filtered, underlying)
    for ot in ("CE", "PE"):
        row = next((r for r in filtered if r["strike"] == atm and r["option_type"] == ot), None)
        if not row:
            continue
        curr_iv = row.get("iv") or 0
        prev = prev_by_key.get((atm, ot))
        if not prev:
            continue
        prev_iv = prev.get("iv") or 0
        if not prev_iv:
            continue
        delta = curr_iv - prev_iv
        if delta >= IV_SPIKE_ATM_THRESHOLD:
            sev = _score_severity(delta / IV_SPIKE_ATM_THRESHOLD)
            detail = {
                "strike": atm, "option_type": ot,
                "prev_iv": prev_iv, "curr_iv": curr_iv, "iv_delta": round(delta, 2),
                "underlying": underlying,
            }
            log.info("[engine] IV_SPIKE | %s ATM %.0f %s: %.1f\u2192%.1f [%s]",
                     symbol, atm, ot, prev_iv, curr_iv, sev)
            alerts.append(_make_alert("IV_SPIKE", symbol, expiry, atm, ot, detail, sev))
        elif delta <= IV_CRUSH_THRESHOLD:
            detail = {
                "strike": atm, "option_type": ot,
                "prev_iv": prev_iv, "curr_iv": curr_iv, "iv_delta": round(delta, 2),
                "underlying": underlying,
            }
            log.info("[engine] IV_CRUSH | %s ATM %.0f %s: %.1f\u2192%.1f", symbol, atm, ot, prev_iv, curr_iv)
            alerts.append(_make_alert("IV_CRUSH", symbol, expiry, atm, ot, detail, "MEDIUM"))
    return alerts


def _detect_straddle_premium(filtered: list[dict], symbol: str, expiry: str,
                               underlying: float, prev_by_key: PrevByKey) -> list[dict]:
    atm = _atm_strike(filtered, underlying)
    ce_row = next((r for r in filtered if r["strike"] == atm and r["option_type"] == "CE"), None)
    pe_row = next((r for r in filtered if r["strike"] == atm and r["option_type"] == "PE"), None)
    if not ce_row or not pe_row:
        return []
    curr_premium = (ce_row.get("ltp") or 0) + (pe_row.get("ltp") or 0)
    prev_ce = prev_by_key.get((atm, "CE"))
    prev_pe = prev_by_key.get((atm, "PE"))
    if not prev_ce or not prev_pe:
        return []
    prev_premium = (prev_ce.get("ltp") or 0) + (prev_pe.get("ltp") or 0)
    pct = _pct_change(prev_premium, curr_premium)
    if pct is None or abs(pct) < STRADDLE_DELTA_PCT:
        return []
    direction = "expansion" if pct > 0 else "contraction"
    label = "Event/Uncertainty Pricing" if pct > 0 else "Premium Decay / Vol Crush"
    sev = _score_severity(abs(pct) / STRADDLE_DELTA_PCT)
    detail = {
        "atm_strike": atm, "underlying": underlying,
        "prev_premium": prev_premium, "curr_premium": curr_premium,
        "pct_change": pct, "direction": direction, "label": label,
    }
    return [_make_alert("STRADDLE_PREMIUM", symbol, expiry, atm, None, detail, sev)]


def _detect_max_pain_shift(filtered: list[dict], symbol: str, expiry: str,
                            underlying: float, prev_snaps: list[dict]) -> list[dict]:
    max_pain = _compute_max_pain(filtered)
    if max_pain is None or not prev_snaps:
        return []
    prev_mp = _compute_max_pain(prev_snaps)
    if prev_mp and abs(max_pain - prev_mp) >= MAX_PAIN_SHIFT_THRESHOLD:
        sev = _score_severity(abs(max_pain - prev_mp) / MAX_PAIN_SHIFT_THRESHOLD)
        detail = {
            "prev_max_pain": prev_mp, "curr_max_pain": max_pain,
            "shift": max_pain - prev_mp, "underlying": underlying,
        }
        log.info("[engine] MAX_PAIN_SHIFT | %s: %.0f\u2192%.0f [%s]", symbol, prev_mp, max_pain, sev)
        return [_make_alert("MAX_PAIN_SHIFT", symbol, expiry, max_pain, None, detail, sev)]
    return []


def _detect_oi_wall_shift(all_strikes: list[dict], symbol: str, expiry: str,
                           underlying: float, prev_snaps: list[dict]) -> list[dict]:
    curr_wall = _oi_wall(all_strikes)
    if not prev_snaps:
        return []
    prev_wall = _oi_wall(prev_snaps)
    changes = {}
    if curr_wall["resistance"] and prev_wall["resistance"] and \
            curr_wall["resistance"] != prev_wall["resistance"]:
        changes["resistance"] = {"prev": prev_wall["resistance"],
                                  "curr": curr_wall["resistance"]}
    if curr_wall["support"] and prev_wall["support"] and \
            curr_wall["support"] != prev_wall["support"]:
        changes["support"] = {"prev": prev_wall["support"],
                               "curr": curr_wall["support"]}
    if not changes:
        return []
    detail = {"underlying": underlying, "changes": changes, **curr_wall}
    return [_make_alert("OI_WALL_SHIFT", symbol, expiry, None, None, detail, "MEDIUM")]


def _detect_volume_aggression(filtered: list[dict], symbol: str, expiry: str,
                               underlying: float, prev_by_key: PrevByKey) -> list[dict]:
    alerts = []
    # Determine minimum volume and OI delta based on symbol class to avoid signal spam
    is_mcx = symbol.upper() in ["NATURALGAS", "CRUDEOIL", "GOLD", "SILVER"]
    min_vol_aggressive = 500 if is_mcx else 5000
    min_oi_delta = 50 if is_mcx else 500

    for row in filtered:
        vol  = row.get("volume") or 0
        oi   = row.get("oi") or 0
        prev = prev_by_key.get((row["strike"], row["option_type"]))
        if not prev or not vol:
            continue
        oi_delta = abs((oi - (prev.get("oi") or 0)))
        if oi_delta == 0:
            continue
        ratio = vol / oi_delta
        if ratio > VOLUME_AGGRESSION_HIGH:
            # Filter out insignificant volumes and OI shifts to avoid signal spam
            if vol < min_vol_aggressive or oi_delta < min_oi_delta:
                continue
            label = "Aggressive Flow (high vol vs OI delta)"
            sev = "HIGH" if ratio > VOLUME_AGGRESSION_HIGH * 2 else "MEDIUM"
        elif ratio < VOLUME_AGGRESSION_LOW and vol > (100 if is_mcx else 1000):
            # Passive Positioning requires a substantial OI buildup to be meaningful
            if oi_delta < min_oi_delta:
                continue
            label = "Passive Positioning (low vol, quiet OI build)"
            sev = "LOW"
        else:
            continue
        detail = {
            "strike": row["strike"], "option_type": row["option_type"],
            "volume": vol, "oi_delta": oi_delta, "ratio": round(ratio, 2),
            "label": label, "underlying": underlying,
        }
        alerts.append(_make_alert("VOLUME_AGGRESSION", symbol, expiry,
                                   row["strike"], row["option_type"], detail, sev))
    return alerts


def _detect_otm_unusual(all_strikes: list[dict], symbol: str, expiry: str,
                         underlying: float, prev_by_key: PrevByKey) -> list[dict]:
    otm = _filter_otm_only(all_strikes, underlying)
    alerts = []
    for row in otm:
        strike = row["strike"]
        ot     = row["option_type"]
        prev   = prev_by_key.get((strike, ot))
        if not prev:
            continue
        prev_oi = prev.get("oi") or 0
        curr_oi = row.get("oi") or 0
        pct = _pct_change(prev_oi, curr_oi)
        if pct is None or pct < OTM_OI_SPIKE_PCT:
            continue
        detail = {
            "strike": strike, "option_type": ot,
            "prev_oi": prev_oi, "curr_oi": curr_oi, "pct_change": pct,
            "underlying": underlying, "note": "Far-OTM unusual activity",
        }
        sev = _score_severity(pct / OTM_OI_SPIKE_PCT)
        log.info("[engine] OTM_UNUSUAL | %s %.0f %s: +%.1f%% [%s]",
                 symbol, strike, ot, pct, sev)
        alerts.append(_make_alert("OTM_UNUSUAL", symbol, expiry, strike, ot, detail, sev))
    return alerts


# \u2500\u2500 Main entry \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


def _empty_scan_context(symbol: str, expiry: str, underlying: float,
                        chart_indicators: dict | None = None,
                        reason: str = "empty_strikes") -> dict:
    return {
        "symbol": symbol,
        "expiry": expiry,
        "underlying": underlying,
        "prev_underlying": underlying,
        "price_change_points": 0.0,
        "price_change_pct": None,
        "total_ce_oi": 0,
        "total_pe_oi": 0,
        "ce_oi_change": 0,
        "pe_oi_change": 0,
        "pcr": None,
        "max_pain": None,
        "support": None,
        "resistance": None,
        "atm_strike": None,
        "straddle_premium": 0.0,
        "chart_indicators": chart_indicators,
        "diagnostics": {"reason": reason},
    }


def _sum_oi_by_type(rows: list[dict]) -> tuple[int, int]:
    ce = sum(r.get("oi", 0) or 0 for r in rows if r.get("option_type") == "CE")
    pe = sum(r.get("oi", 0) or 0 for r in rows if r.get("option_type") == "PE")
    return ce, pe


def detect_anomalies(oc_data: dict, fetched_at: str, chart_indicators: dict | None = None, override_thresholds: dict | None = None) -> tuple[list[dict], dict]:
    symbol     = oc_data["symbol"]
    expiry     = oc_data["expiry"]
    strikes    = oc_data.get("strikes", []) or []
    underlying = oc_data.get("underlying_price", 0) or 0
    if chart_indicators is None:
        chart_indicators = oc_data.get("chart_indicators")

    t = override_thresholds or {}
    oi_thresh = t.get("oi_threshold", OI_SPIKE_THRESHOLD_PCT)
    ltp_thresh = t.get("ltp_threshold", ATM_LEG_MOVE_PCT)
    pcr_shift_thresh = t.get("pcr_shift_threshold", PCR_SHIFT_THRESHOLD)

    if not strikes:
        log.warning("[engine] empty strikes | %s | expiry=%s", symbol, expiry)
        return [], _empty_scan_context(symbol, expiry, underlying, chart_indicators, "empty_strikes")

    filtered = _filter_atm_range(strikes, underlying)
    if not filtered:
        log.warning("[engine] empty filtered strikes | %s | expiry=%s", symbol, expiry)
        return [], _empty_scan_context(symbol, expiry, underlying, chart_indicators, "empty_filtered_strikes")

    prev_by_key: PrevByKey = _PrevSnapshotLookup(get_prev_snapshots_bulk(symbol, expiry), symbol, expiry)
    prev_snaps: list[dict] = list(prev_by_key.values())

    alerts: list[dict] = []
    
    alerts += _detect_oi_spike_unwind(filtered, symbol, expiry, underlying, prev_by_key, oi_thresh=oi_thresh)
    alerts += _detect_buildup(filtered, symbol, expiry, underlying, prev_by_key,
                             oi_min_pct=t.get("buildup_oi_min_pct", BUILDUP_OI_MIN_PCT),
                             ltp_min_pct=t.get("buildup_ltp_min_pct", BUILDUP_LTP_MIN_PCT))
    alerts += _detect_price_spike(symbol, expiry, underlying)

    pcr = _compute_pcr(filtered)
    if pcr is not None:
        if pcr <= PCR_EXTREME_LOW or pcr >= PCR_EXTREME_HIGH:
            boundary = PCR_EXTREME_LOW if pcr < 1 else PCR_EXTREME_HIGH
            sev = _score_severity(abs(pcr - boundary) / PCR_EXTREME_SEVERITY_BAND)
            alerts.append(_make_alert("PCR_EXTREME", symbol, expiry, None, None, {"pcr": pcr, "underlying": underlying}, sev))
        if prev_snaps:
            prev_pcr = _compute_pcr(prev_snaps)
            if prev_pcr and abs(pcr - prev_pcr) >= pcr_shift_thresh:
                sev = _score_severity(abs(pcr - prev_pcr) / pcr_shift_thresh)
                alerts.append(_make_alert("PCR_SHIFT", symbol, expiry, None, None, {"pcr": pcr, "prev_pcr": prev_pcr, "pcr_delta": round(pcr - prev_pcr, 4)}, sev))

    alerts += _detect_pcr_velocity(symbol, expiry, underlying, pcr)
    alerts += _detect_iv_spike_crush(filtered, symbol, expiry, underlying, prev_by_key)
    
    atm = _atm_strike(filtered, underlying)
    moves = {}
    for ot in ("CE", "PE"):
        row = next((r for r in filtered if r["strike"] == atm and r["option_type"] == ot), None)
        prev = prev_by_key.get((atm, ot))
        if row and prev:
            pct = _pct_change(prev.get("ltp", 0), row.get("ltp", 0))
            if pct is not None: moves[ot] = {"pct": pct}
    if moves:
        ce_p = moves.get("CE", {}).get("pct", 0); pe_p = moves.get("PE", {}).get("pct", 0)
        if abs(ce_p) >= ltp_thresh or abs(pe_p) >= ltp_thresh:
            bias = "Bullish Flow" if ce_p > 0 and pe_p < 0 else ("Bearish Flow" if ce_p < 0 and pe_p > 0 else ("Vol Expansion" if ce_p > 0 and pe_p > 0 else "Vol Crush"))
            sev = _score_severity(max(abs(ce_p), abs(pe_p)) / ltp_thresh)
            alerts.append(_make_alert("ATM_LEG_MOVE", symbol, expiry, atm, None, {"ce_pct": ce_p, "pe_pct": pe_p, "bias": bias}, sev))

    alerts += _detect_straddle_premium(filtered, symbol, expiry, underlying, prev_by_key)
    alerts += _detect_max_pain_shift(filtered, symbol, expiry, underlying, prev_snaps)
    alerts += _detect_oi_wall_shift(strikes, symbol, expiry, underlying, prev_snaps)
    alerts += _detect_volume_aggression(filtered, symbol, expiry, underlying, prev_by_key)
    alerts += _detect_otm_unusual(strikes, symbol, expiry, underlying, prev_by_key)

    log.info("[engine] %s | %d anomalies detected", symbol, len(alerts))

    # \u2500\u2500 Build scan context \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    total_ce_oi, total_pe_oi = _sum_oi_by_type(strikes)
    prev_ce_oi, prev_pe_oi = _sum_oi_by_type(prev_snaps) if prev_snaps else (0, 0)
    
    max_oi_delta_pct = 0.0
    top_oi_delta = None
    for row in filtered:
        prev_row = prev_by_key.get((row.get("strike"), row.get("option_type")))
        if not prev_row: continue
        pct = _pct_change(prev_row.get("oi", 0), row.get("oi", 0))
        if pct is not None and abs(pct) > max_oi_delta_pct:
            max_oi_delta_pct = abs(pct)
            top_oi_delta = {"strike": row["strike"], "option_type": row["option_type"], "pct": pct}

    prev_pcr = _compute_pcr(prev_snaps) if prev_snaps else None
    prev_und = get_previous_underlying_before(symbol, fetched_at)
    prev_price = prev_und["price"] if prev_und else None
    price_change_points = round(float(underlying or 0) - float(prev_price or 0), 4) if prev_price is not None else 0.0
    levels = _key_levels(strikes, underlying)

    scan_context = {
        "symbol": symbol,
        "expiry": expiry,
        "underlying": underlying,
        "prev_underlying": prev_price if prev_price is not None else underlying,
        "price_change_points": price_change_points,
        "price_change_pct": _pct_change(prev_price, underlying) if prev_price is not None else None,
        "total_ce_oi": total_ce_oi,
        "total_pe_oi": total_pe_oi,
        "ce_oi_change": total_ce_oi - prev_ce_oi,
        "pe_oi_change": total_pe_oi - prev_pe_oi,
        "pcr": pcr,
        "max_pain": levels.get("max_pain"),
        "support": levels.get("support"),
        "resistance": levels.get("resistance"),
        "atm_strike": atm,
        "chart_indicators": chart_indicators,
        "diagnostics": {
            "max_oi_delta_pct": max_oi_delta_pct,
            "top_oi_delta": top_oi_delta,
            "oi_threshold": oi_thresh,
            "ltp_threshold": ltp_thresh,
        },
    }
    return alerts, scan_context
