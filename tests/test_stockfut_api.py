"""Unit and Integration tests for Stock Futures Dashboard & API endpoints.
Validates:
- All 9 HTML templates contain the '/futures' navigation link
- Swiss Modern Flat architecture constraints in futures.html
- FastAPI test client responses for all /api/futures/* endpoints
"""

from __future__ import annotations

import pathlib
from typing import Any
import pytest
from fastapi.testclient import TestClient

from dashboard_server import app


def test_nav_links_across_templates():
    dashboard_dir = pathlib.Path("src/dashboard")
    templates = [
        "index.html",
        "paper.html",
        "broker.html",
        "ai.html",
        "contributors.html",
        "report.html",
        "settings.html",
        "ops.html",
        "futures.html",
    ]
    for t in templates:
        p = dashboard_dir / t
        assert p.exists(), f"Template missing: {t}"
        content = p.read_text(encoding="utf-8")
        assert 'href="/futures"' in content, f"Template {t} missing href='/futures'"
        assert "Stock Futures" in content, f"Template {t} missing 'Stock Futures' label"


def test_settings_stockfut_controls():
    p = pathlib.Path("src/dashboard/settings.html")
    assert p.exists()
    content = p.read_text(encoding="utf-8")
    assert "STOCKFUT" in content
    assert "STOCK FUTURES" in content
    assert "strategy-card-STOCKFUT" in content


def test_futures_html_architecture():
    p = pathlib.Path("src/dashboard/futures.html")
    assert p.exists()
    content = p.read_text(encoding="utf-8")
    assert "data-theme" in content
    assert "tabular-nums" in content
    assert "theme-toggle-btn" in content
    assert "Chart.js" in content


def test_api_futures_routes():
    from src.models.schema import init_db
    init_db()

    client = TestClient(app)

    # 1. HTML route
    res = client.get("/futures")
    assert res.status_code == 200
    assert "STOCK FUTURES COCKPIT" in res.text

    # 2. Overview
    res = client.get("/api/futures/overview")
    assert res.status_code == 200
    data = res.json()
    assert "mode" in data
    assert "capital_reference" in data
    assert "margin_used" in data
    assert "active_positions_count" in data
    assert "gross_notional" in data
    assert "net_direction" in data

    # 3. Watchlist
    res = client.get("/api/futures/watchlist")
    assert res.status_code == 200
    data = res.json()
    assert "watchlist" in data
    assert "count" in data

    # 4. Positions
    res = client.get("/api/futures/positions")
    assert res.status_code == 200
    data = res.json()
    assert "positions" in data
    assert "count" in data

    # 5. Trades
    res = client.get("/api/futures/trades")
    assert res.status_code == 200
    data = res.json()
    assert "trades" in data
    assert "total" in data

    # 6. Risk
    res = client.get("/api/futures/risk")
    assert res.status_code == 200
    data = res.json()
    assert "gross_notional" in data
    assert "net_directional_pct" in data
    assert "gap_stress_loss" in data

    # 7. Performance
    res = client.get("/api/futures/performance")
    assert res.status_code == 200
    data = res.json()
    assert "curve" in data
    assert "starting_capital" in data
