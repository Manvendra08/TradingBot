import copy
import json
import pytest
from fastapi.testclient import TestClient
from dashboard_server import app, authenticate
import config.decision_matrix_loader as dml


@pytest.fixture
def auth_client():
    """Client with authentication bypassed."""
    app.dependency_overrides[authenticate] = lambda: "admin"
    client = TestClient(app)
    yield client
    app.dependency_overrides.pop(authenticate, None)


def test_get_decision_matrix(auth_client):
    response = auth_client.get("/api/decision_matrix")
    assert response.status_code == 200
    data = response.json()
    assert "version" in data
    assert "mode" in data
    assert "regime_table" in data
    assert "vol_buckets" in data
    assert "delta_bands" in data
    assert "exits" in data
    assert "priority_order" in data["exits"]


def test_post_decision_matrix_invalid(auth_client):
    # Invalid mode and invalid numbers
    invalid_payload = {
        "version": "1.0.0",
        "mode": "invalid_mode_here",
    }
    response = auth_client.post("/api/decision_matrix", json=invalid_payload)
    assert response.status_code == 400
    res_json = response.json()
    assert "detail" in res_json
    detail = res_json["detail"]
    assert detail["status"] == "ERROR"
    assert "errors" in detail
    assert any("must be 'off', 'shadow', or 'enforce'" in e for e in detail["errors"])


def test_post_decision_matrix_success(auth_client, tmp_path, monkeypatch):
    # Setup temporary file
    temp_file = tmp_path / "decision_matrix.json"
    valid_cfg = copy.deepcopy(dml.load_decision_matrix())
    temp_file.write_text(json.dumps(valid_cfg, indent=2), encoding="utf-8")

    monkeypatch.setattr(dml, "DECISION_MATRIX_PATH", temp_file)

    # Change delta_profile and mode
    valid_cfg["mode"] = "enforce"
    valid_cfg["delta_profile"] = "defensive"

    response = auth_client.post("/api/decision_matrix", json=valid_cfg)
    assert response.status_code == 200
    res_json = response.json()
    assert res_json["status"] == "SUCCESS"
    assert "Decision matrix configuration updated" in res_json["message"]

    # Verify persisted content on disk
    saved_disk = json.loads(temp_file.read_text(encoding="utf-8"))
    assert saved_disk["mode"] == "enforce"
    assert saved_disk["delta_profile"] == "defensive"
