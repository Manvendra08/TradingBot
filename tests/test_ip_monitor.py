"""
Unit tests for ISP IP Monitor (src/utils/ip_monitor.py).
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.utils.ip_monitor import (
    _extract_ipv4_from_response,
    _fetch_public_ip,
    _is_valid_public_ipv4,
    _load_stored_ip,
    _save_ip,
    check_ip_changed,
)


def test_is_valid_public_ipv4():
    # Valid public IPv4 addresses
    assert _is_valid_public_ipv4("103.197.74.109") is True
    assert _is_valid_public_ipv4("8.8.8.8") is True
    assert _is_valid_public_ipv4("1.1.1.1") is True

    # Invalid / Loopback / Private / IPv6
    assert _is_valid_public_ipv4("127.0.0.1") is False
    assert _is_valid_public_ipv4("10.0.0.1") is False
    assert _is_valid_public_ipv4("192.168.1.1") is False
    assert _is_valid_public_ipv4("172.16.0.1") is False
    assert _is_valid_public_ipv4("2402:e280:3e38:193::1") is False
    assert _is_valid_public_ipv4("invalid_string") is False
    assert _is_valid_public_ipv4("") is False
    assert _is_valid_public_ipv4(None) is False


def test_extract_ipv4_from_response():
    # JSON with ip key
    assert _extract_ipv4_from_response('{"ip": "103.197.74.109"}') == "103.197.74.109"

    # JSON with query key
    assert _extract_ipv4_from_response('{"query": "49.36.216.5"}') == "49.36.216.5"

    # Plain text IPv4
    assert _extract_ipv4_from_response("103.197.74.109\n") == "103.197.74.109"

    # HTML / Text containing IPv4
    html = "<html><body>Your IP address is 103.197.74.109 in India</body></html>"
    assert _extract_ipv4_from_response(html) == "103.197.74.109"

    # IPv6 response returning None
    assert _extract_ipv4_from_response('{"ip": "2402:e280:3e38:193::1"}') is None


@patch("src.utils.ip_monitor.urllib.request.urlopen")
def test_fetch_public_ip_success(mock_urlopen):
    mock_resp = MagicMock()
    mock_resp.read.return_value = b'{"ip": "103.197.74.109"}'
    mock_resp.__enter__.return_value = mock_resp
    mock_urlopen.return_value = mock_resp

    ip = _fetch_public_ip()
    assert ip == "103.197.74.109"


@patch("src.utils.ip_monitor._load_stored_ip")
@patch("src.utils.ip_monitor._save_ip")
@patch("src.utils.ip_monitor._fetch_public_ip")
def test_check_ip_changed_detected(mock_fetch, mock_save, mock_load):
    mock_fetch.return_value = "103.197.74.110"
    mock_load.return_value = "103.197.74.109"

    res = check_ip_changed()
    assert res == ("103.197.74.109", "103.197.74.110")
    mock_save.assert_called_once_with("103.197.74.110")


@patch("src.utils.ip_monitor._load_stored_ip")
@patch("src.utils.ip_monitor._save_ip")
@patch("src.utils.ip_monitor._fetch_public_ip")
def test_check_ip_changed_unchanged(mock_fetch, mock_save, mock_load):
    mock_fetch.return_value = "103.197.74.109"
    mock_load.return_value = "103.197.74.109"

    res = check_ip_changed()
    assert res is None
    mock_save.assert_not_called()
