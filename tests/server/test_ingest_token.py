"""Tests for the optional test-client ingest token."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from testrift_server import auth, config, database
from testrift_server.tr_server import create_app
from testrift_server.websocket import WebSocketServer

TOKEN = "ingest-secret"


def _config(**overrides):
    cfg = config.default_auth_config()
    cfg.update(overrides)
    return cfg


@pytest.fixture
def ingest_client(monkeypatch):
    temp_dir = tempfile.mkdtemp()
    data_dir = Path(temp_dir)
    monkeypatch.setattr("testrift_server.config.DATA_DIR", data_dir)
    monkeypatch.setattr("testrift_server.tr_server.DATA_DIR", data_dir)
    monkeypatch.setattr("testrift_server.config.AUTH_CONFIG", _config(ingest_token=TOKEN))
    database.initialize_database(data_dir)
    app = create_app(ws_server=WebSocketServer())
    with TestClient(app) as client:
        yield client
    shutil.rmtree(temp_dir, ignore_errors=True)


def test_nunit_rejected_without_token(ingest_client):
    with pytest.raises(Exception):
        with ingest_client.websocket_connect("/ws/nunit"):
            pass


def test_nunit_rejected_with_wrong_token(ingest_client):
    with pytest.raises(Exception):
        with ingest_client.websocket_connect(
            "/ws/nunit",
            headers={auth.INGEST_TOKEN_HEADER: "nope"},
        ):
            pass


def test_nunit_accepted_with_header(ingest_client):
    with ingest_client.websocket_connect(
        "/ws/nunit",
        headers={auth.INGEST_TOKEN_HEADER: TOKEN},
    ) as ws:
        assert ws is not None


def test_nunit_accepted_with_bearer(ingest_client):
    with ingest_client.websocket_connect(
        "/ws/nunit",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as ws:
        assert ws is not None


def test_attachment_upload_requires_token(ingest_client):
    response = ingest_client.post("/api/attachments/run1/tc1/upload")
    assert response.status_code == 401
    assert response.json()["error"] == auth.INGEST_TOKEN_ERROR_MESSAGE


def test_commit_upload_requires_token(ingest_client):
    response = ingest_client.post("/api/runs/run1/commits", json={})
    assert response.status_code == 401
    assert response.json()["error"] == auth.INGEST_TOKEN_ERROR_MESSAGE


def test_kpi_upload_requires_token(ingest_client):
    response = ingest_client.post("/api/runs/run1/kpis", json={})
    assert response.status_code == 401
    assert response.json()["error"] == auth.INGEST_TOKEN_ERROR_MESSAGE


def test_kpi_upload_with_token_passes_ingest_gate(ingest_client):
    response = ingest_client.post(
        "/api/runs/run1/kpis",
        headers={auth.INGEST_TOKEN_HEADER: TOKEN},
        json={},
    )
    assert response.status_code == 404
    assert response.json()["error"] == "Run not found"


def test_health_stays_open(ingest_client):
    assert ingest_client.get("/health").status_code == 200


def test_ui_stays_open_when_auth_off(ingest_client):
        assert ingest_client.get("/").status_code == 200


def test_attachment_with_token_passes_auth_gate(ingest_client):
    response = ingest_client.post(
        "/api/attachments/run1/tc1/upload",
        headers={auth.INGEST_TOKEN_HEADER: TOKEN},
    )
    assert response.status_code != 401


def test_session_is_not_a_substitute(monkeypatch):
    temp_dir = tempfile.mkdtemp()
    data_dir = Path(temp_dir)
    cfg = _config(enabled=True, ingest_token=TOKEN)
    cfg["bootstrap_admin"] = {"username": "admin", "password": "bootstrap-pass"}
    monkeypatch.setattr("testrift_server.config.DATA_DIR", data_dir)
    monkeypatch.setattr("testrift_server.tr_server.DATA_DIR", data_dir)
    monkeypatch.setattr("testrift_server.config.AUTH_CONFIG", cfg)
    monkeypatch.setattr("testrift_server.auth.LOCKOUT_DELAY_RANGE", (0, 0))
    auth.reset_session_secret_cache()
    database.initialize_database(data_dir)
    app = create_app(ws_server=WebSocketServer())
    try:
        with TestClient(app) as client:
            login = client.post("/api/auth/login", json={"username": "admin", "password": "bootstrap-pass"})
            assert login.status_code == 200
            denied = client.post("/api/attachments/run1/tc1/upload")
            assert denied.status_code == 401
            with pytest.raises(Exception):
                with client.websocket_connect("/ws/nunit"):
                    pass
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_parse_ingest_token_from_env_placeholder():
    parsed = config.parse_auth_config({"ingest_token": "  secret-value  "})
    assert parsed["ingest_token"] == "secret-value"
    parsed_empty = config.parse_auth_config({})
    assert parsed_empty["ingest_token"] == ""
