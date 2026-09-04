from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
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
