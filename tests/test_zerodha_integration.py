import os
import tempfile
import pytest
import hashlib
import json
from pathlib import Path
from unittest.mock import patch, MagicMock
import httpx
from src.services.zerodha_auth import encrypt_secret, decrypt_secret, get_current_totp
from config.runtime_config import load_runtime_config, save_runtime_config
from dashboard_server import app


def _kite_checksum(payload: dict, api_secret: str) -> str:
    return hashlib.sha256(
        f"{payload['order_id']}{payload['order_timestamp']}{api_secret}".encode("utf-8")
    ).hexdigest()

def test_fernet_encryption_decryption():
    secret = "my_super_secret_totp_key"
    enc = encrypt_secret(secret)
    assert enc != secret
    dec = decrypt_secret(enc)
    assert dec == secret

def test_get_current_totp_invalid_config():
    with patch("src.services.zerodha_auth.get_broker_config", return_value=None):
        assert get_current_totp() == ""

def test_get_current_totp_with_secret():
    # JBSWY3DPEHPK3PXP is a valid base32 key for pyotp
    dummy_secret = "JBSWY3DPEHPK3PXP"
    enc_secret = encrypt_secret(dummy_secret)
    
    with patch("src.services.zerodha_auth.get_broker_config", return_value={"totp_secret": enc_secret}):
        code = get_current_totp()
        assert len(code) == 6
        assert code.isdigit()

def test_runtime_config_io():
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp_config_path = f.name
        
    try:
        # Patch BOTH paths in settings and runtime_config if referenced
        with patch("config.runtime_config.RUNTIME_CONFIG_PATH", Path(tmp_config_path)):
            # Load default config
            conf = load_runtime_config()
            assert conf["live_shadow_mode"] is True
            assert conf["live_capital_per_trade_inr"] == 20000
            
            # Modify and save
            conf["live_shadow_mode"] = False
            conf["live_capital_per_trade_inr"] = 50000
            conf["live_symbol_lots"]["NIFTY"] = 2
            
            save_runtime_config(conf)
            
            # Reload
            conf2 = load_runtime_config()
            assert conf2["live_shadow_mode"] is False
            assert conf2["live_capital_per_trade_inr"] == 50000
            assert conf2["live_symbol_lots"]["NIFTY"] == 2
    finally:
        try:
            os.unlink(tmp_config_path)
        except Exception:
            pass

def test_zerodha_postback_signature_validation():
    from starlette.testclient import TestClient
    client = TestClient(app)
    
    # Test case 1: No api_secret configured
    with patch("src.models.schema.get_broker_config", return_value=None):
        response = client.post("/api/zerodha/postback", content="{}")
        assert response.status_code == 400
        assert "Broker config missing" in response.json()["error"]
        
    # Test case 2: Missing checksum must fail closed
    with patch("src.models.schema.get_broker_config", return_value={"api_secret": "my_secret"}):
        response = client.post(
            "/api/zerodha/postback",
            content=json.dumps({
                "order_id": "12345",
                "status": "COMPLETE",
                "order_timestamp": "2026-06-15 09:15:00",
            }),
        )
        assert response.status_code == 400
        assert "Missing required postback fields" in response.json()["error"]

    # Test case 3: Invalid checksum
    with patch("src.models.schema.get_broker_config", return_value={"api_secret": "my_secret"}):
        response = client.post(
            "/api/zerodha/postback",
            content=json.dumps({
                "order_id": "12345",
                "status": "COMPLETE",
                "order_timestamp": "2026-06-15 09:15:00",
                "checksum": "invalid_checksum",
            }),
        )
        assert response.status_code == 401
        assert "Invalid checksum" in response.json()["error"]
        
    # Test case 4: Valid checksum
    secret = "my_secret"
    payload = {
        "order_id": "12345",
        "status": "UPDATE",
        "order_timestamp": "2026-06-15 09:15:00",
        "tradingsymbol": "NIFTY24JUN22000CE",
    }
    payload["checksum"] = _kite_checksum(payload, secret)
    body = json.dumps(payload).encode("utf-8")
    
    with patch("src.models.schema.get_broker_config", return_value={"api_secret": secret}), \
         patch("src.models.schema.get_conn") as mock_conn:
        
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None  # No matching open trade in DB
        mock_conn.return_value.__enter__.return_value.execute.return_value = mock_cursor
        
        response = client.post(
            "/api/zerodha/postback",
            content=body,
        )
        assert response.status_code == 200
        assert response.json() == {"status": "processed"}


def test_zerodha_postback_valid_checksum_does_not_fallback_close_wrong_trade():
    from src.models.schema import get_conn, insert_live_trade, update_broker_config

    with get_conn() as conn:
        conn.execute("DELETE FROM live_trades")
        conn.execute("DELETE FROM broker_configs")

    secret = "my_secret"
    update_broker_config(api_key="k", api_secret=secret)
    trade_id = insert_live_trade({
        "opened_at": "2026-06-15T09:15:00+00:00",
        "symbol": "NIFTY",
        "expiry": "2026-06-25",
        "verdict_label": "Long Buildup",
        "side": "BUY",
        "option_type": "CE",
        "strike": 22000.0,
        "entry_underlying": 22000.0,
        "entry_premium": 100.0,
        "sl_underlying": 21900.0,
        "sl_premium": 70.0,
        "target_underlying": 22200.0,
        "target_premium": 150.0,
        "lots": 1,
        "status": "OPEN",
        "reason": "test",
        "digest_id": "digest",
        "signal_key": "postback-fallback-test",
        "broker_order_id": "entry-order",
        "broker_status": "COMPLETE",
        "exit_mode": "GTT",
    })
    payload = {
        "order_id": "unrelated-exit-order",
        "status": "COMPLETE",
        "order_timestamp": "2026-06-15 09:16:00",
        "tradingsymbol": "OTHER26JUN22000CE",
        "transaction_type": "SELL",
        "average_price": 120.0,
    }
    payload["checksum"] = _kite_checksum(payload, secret)

    from starlette.testclient import TestClient
    response = TestClient(app).post("/api/zerodha/postback", content=json.dumps(payload).encode("utf-8"))

    assert response.status_code == 200
    with get_conn() as conn:
        row = conn.execute("SELECT status FROM live_trades WHERE id=?", (trade_id,)).fetchone()
    assert row["status"] == "OPEN"


def test_fetch_real_kite_positions_side_aware_matching():
    from src.models.schema import get_conn, insert_live_trade
    from dashboard_server import _fetch_real_kite_positions, get_live_trades
    from config.runtime_config import load_runtime_config, save_runtime_config
    
    # Clean database
    with get_conn() as conn:
        conn.execute("DELETE FROM live_trades")
        
    # Insert a SELL FUT trade in live_trades
    trade_id = insert_live_trade({
        "opened_at": "2026-06-17T14:30:29.808812+00:00",
        "symbol": "NATURALGAS",
        "expiry": "2026-06-23",
        "option_type": "FUT",
        "strike": 295.0,
        "side": "SELL",
        "lots": 1,
        "status": "OPEN",
        "entry_underlying": 296.8,
        "entry_premium": 296.8,
        "sl_underlying": 305.72,
        "target_underlying": 284.91,
        "sl_premium": 305.72,
        "target_premium": 284.91,
        "trade_status": "TRIGGERED_EXPERIMENTAL",
        "exit_mode": "POLL"
    })
    
    # Mock Kite client and its positions response
    mock_kite = MagicMock()
    mock_kite.positions.return_value = {
        "net": [
            {
                "tradingsymbol": "NATURALGAS26JUNFUT",
                "exchange": "MCX",
                "quantity": -1,  # SELL 1 lot
                "average_price": 296.8,
                "last_price": 298.3,
                "pnl": -1750.0
            },
            {
                "tradingsymbol": "NATURALGAS26JUNFUT",
                "exchange": "MCX",
                "quantity": 3,   # BUY 3 lots
                "average_price": 296.8,
                "last_price": 298.3,
                "pnl": 58000.0
            }
        ]
    }
    mock_kite.get_gtts.return_value = []
    
    # Run _fetch_real_kite_positions
    with patch("src.engine.symbol_resolver._INSTRUMENT_CACHE", {
        ("NATURALGAS", "2026-06-23", 0.0, "FUT"): {
            "tradingsymbol": "NATURALGAS26JUNFUT",
            "instrument_token": 12345,
            "lot_size": 1,
            "tick_size": 0.05
        }
    }), patch("src.engine.live_trading.get_kite_client", return_value=mock_kite):
        parsed = _fetch_real_kite_positions(mock_kite)
        
        # Verify two positions parsed
        assert len(parsed) == 2
        
        # Find SELL FUT parsed position (should match bot trade)
        sell_pos = next(p for p in parsed if p["side"] == "SELL")
        assert sell_pos["lots"] == 1
        assert sell_pos["exit_mode"] == "BOT"
        assert sell_pos["sl_premium"] == 305.72
        assert sell_pos["target_premium"] == 284.91
        assert sell_pos["pnl_rupees"] == -1750.0
        
        # Find BUY FUT parsed position (should be KITE manual trade)
        buy_pos = next(p for p in parsed if p["side"] == "BUY")
        assert buy_pos["lots"] == 3
        assert buy_pos["exit_mode"] == "KITE"
        assert buy_pos["sl_premium"] == "—"
        assert buy_pos["target_premium"] == "—"
        assert buy_pos["pnl_rupees"] == 58000.0
        
        # Now check get_live_trades duplicate checking
        # Save shadow mode settings to True
        conf = load_runtime_config()
        original_shadow = conf.get("live_shadow_mode", True)
        conf["live_shadow_mode"] = True
        save_runtime_config(conf)
        
        try:
            live_positions = get_live_trades(symbol="", status="OPEN")
            assert len(live_positions) == 2
            
            sides = {p["side"] for p in live_positions}
            assert sides == {"BUY", "SELL"}
        finally:
            conf["live_shadow_mode"] = original_shadow
            save_runtime_config(conf)
