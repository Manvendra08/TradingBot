"""
Tests for ShoonyaFetcher._bulk_get_quotes — the parallel MCX quote batch.

Regression: the MCX option-chain path used one _get_quotes()/_api_call() per
strike (~42 contracts at ATM ± 10), each holding _api_lock for the whole HTTP
round-trip → strictly sequential ~20s+ → blew the router's 12s per-source
deadline → NATURALGAS/CRUDEOIL/GOLD/SILVER fell back to dhan_commodity on every
scan, emitting "shoonya fetch failed/timed out: .".

The bulk path bypasses _api_lock (which exists to serialise the relogin/expiry
race) because a batch of read-only GetQuotes uses one fixed token and treats a
Session Expired as a dropped strike, not a concurrent relogin.
"""

import time
import threading
from unittest.mock import MagicMock, patch

from src.fetchers.shoonya_fetcher import ShoonyaFetcher


def _make_fetcher(access_token: str = "tok-123"):
    """Build a ShoonyaFetcher without running __init__ (which reads env/disk)."""
    f = ShoonyaFetcher.__new__(ShoonyaFetcher)
    f.access_token = access_token
    f.user_id = "TESTU1"
    f.actid = "TESTU1"
    f._token_created_at = time.time()
    f._save_token = MagicMock()
    f._load_cached_token = MagicMock()
    # No-op throttle: keeps the test deterministic and instant. The 8 req/s
    # limiter is orthogonal to the bug under test (parallelism without the lock).
    f._throttle_rate_limit = MagicMock()
    return f


def _contracts(n: int = 42) -> list[dict]:
    return [{"Token": str(i + 1), "strike_val": 100 + i} for i in range(n)]


def _ok_post(url, payload, token=None):
    return {"stat": "Ok", "token": payload["token"], "lp": "10.5", "oi": "1200"}


def test_fetches_all_contracts_in_parallel():
    f = _make_fetcher()
    with patch(
        "src.fetchers.shoonya_fetcher._post_jdata", side_effect=_ok_post
    ) as mock_post:
        res = f._bulk_get_quotes("MCX", _contracts(42))

    assert len(res) == 42
    assert mock_post.call_count == 42
    # Every payload addressed to MCX + one specific token.
    for call in mock_post.call_args_list:
        payload = call.args[1]
        assert payload["exch"] == "MCX"
    # Keys are the contract tokens (str).
    assert set(res.keys()) == {str(i) for i in range(1, 43)}


def test_does_not_use_api_lock_or_login():
    f = _make_fetcher()
    f.login = MagicMock()
    f._api_call = MagicMock()
    with patch("src.fetchers.shoonya_fetcher._post_jdata", side_effect=_ok_post):
        f._bulk_get_quotes("MCX", _contracts(5))

    # Token was read once up front; no per-call relogin / no serialised api_call.
    f.login.assert_not_called()
    f._api_call.assert_not_called()


def test_session_expired_degrades_to_dropped_strikes():
    f = _make_fetcher()
    f.login = MagicMock()

    real = {"stat": "Ok", "lp": "10.0", "oi": "1"}
    expired = {"stat": "Not_Ok", "emsg": "Session Expired"}
    # Alternate: odd tokens OK, even tokens expired.
    with patch(
        "src.fetchers.shoonya_fetcher._post_jdata",
        side_effect=lambda url, payload, token=None: (
            real if int(payload["token"]) % 2 == 1 else expired
        ),
    ) as mock_post:
        res = f._bulk_get_quotes("MCX", _contracts(6))

    # 3 OK + 3 expired → only the OK ones survive, no relogin attempted.
    assert len(res) == 3
    assert set(res.keys()) == {"1", "3", "5"}
    f.login.assert_not_called()


def test_no_token_aborts_fail_closed():
    f = _make_fetcher(access_token=None)
    with patch("src.fetchers.shoonya_fetcher._post_jdata") as mock_post:
        res = f._bulk_get_quotes("MCX", _contracts(5))
    assert res == {}
    mock_post.assert_not_called()


def test_rotates_and_persists_fresh_token():
    f = _make_fetcher(access_token="old-token")
    # Response rotates to a new session token.
    def rotated(url, payload, token=None):
        return {"stat": "Ok", "token": payload["token"], "lp": "9.0", "susertoken": "new-token"}

    with patch("src.fetchers.shoonya_fetcher._post_jdata", side_effect=rotated):
        f._bulk_get_quotes("MCX", _contracts(3))

    assert f.access_token == "new-token"
    f._save_token.assert_called_once()


def test_no_token_loaded_still_uses_passed_token():
    """access_token present in memory but disk reload returns None is safe."""
    f = _make_fetcher(access_token="mem-token")
    f._load_cached_token = MagicMock(return_value=None)  # reload yields nothing
    with patch("src.fetchers.shoonya_fetcher._post_jdata", side_effect=_ok_post) as m:
        res = f._bulk_get_quotes("MCX", _contracts(3))
    assert len(res) == 3
    # It still used the fixed token even though the reload found nothing.
    for c in m.call_args_list:
        assert c.args[2] == "mem-token"