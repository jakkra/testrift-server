"""
ASGI contract tests for FastAPI migration (Phase 0/1).

These hit the real app over HTTP/WebSocket so framework swaps are caught
without relying only on mocked handler unit tests.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import msgpack
import pytest
from starlette.testclient import TestClient

from testrift_server import database
from testrift_server.config import default_auth_config
from testrift_server.protocol import (
    F_TYPE,
    F_RUN_ID,
    F_RETENTION_DAYS,
    F_LOCAL_RUN,
    F_USER_METADATA,
    F_TARGET_KEY,
    F_PURPOSE,
    F_SOURCES,
    F_SOURCE_BRANCH,
    F_SOURCE_REVISION,
    F_SOURCE_REPOSITORY_URL,
    F_ERROR,
    MSG_RUN_STARTED,
    MSG_RUN_STARTED_RESPONSE,
)
from testrift_server.tr_server import create_app
from testrift_server.websocket import WebSocketServer


@pytest.fixture
def asgi_client(monkeypatch):
    """Create a TestClient with an isolated data directory."""
    temp_dir = tempfile.mkdtemp()
    data_dir = Path(temp_dir)

    monkeypatch.setattr("testrift_server.config.DATA_DIR", data_dir)
    monkeypatch.setattr("testrift_server.tr_server.DATA_DIR", data_dir)
    monkeypatch.setattr("testrift_server.utils.DATA_DIR", data_dir, raising=False)
    monkeypatch.setattr("testrift_server.config.AUTH_CONFIG", {**default_auth_config(), "enabled": False})

    # utils and others import DATA_DIR from config at use time via get_run_path
    import testrift_server.utils as utils_mod
    if hasattr(utils_mod, "DATA_DIR"):
        monkeypatch.setattr(utils_mod, "DATA_DIR", data_dir)

    database.initialize_database(data_dir)

    ws_server = WebSocketServer()
    app = create_app(ws_server=ws_server)

    with TestClient(app) as client:
        client.ws_server = ws_server
        client.data_dir = data_dir
        yield client

    shutil.rmtree(temp_dir, ignore_errors=True)


class TestHTTPContracts:
    def test_health(self, asgi_client):
        response = asgi_client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_index_html(self, asgi_client):
        response = asgi_client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers.get("content-type", "")

    def test_server_info(self, asgi_client):
        response = asgi_client.get("/api/server-info")
        assert response.status_code == 200
        body = response.json()
        assert body.get("service") == "testrift-server"
        assert "config_hash" in body

    def test_test_runs_list(self, asgi_client):
        response = asgi_client.get("/api/test-runs")
        assert response.status_code == 200
        body = response.json()
        assert body.get("success") is True

    def test_targets_list(self, asgi_client):
        response = asgi_client.get("/api/targets")
        assert response.status_code == 200
        assert response.json().get("success") is True

    def test_collections_list(self, asgi_client):
        response = asgi_client.get("/api/collections")
        assert response.status_code == 200
        assert response.json().get("success") is True

    def test_static_js_served(self, asgi_client):
        # Pick a known static file if present
        from testrift_server.config import STATIC_DIR
        js_files = list(Path(STATIC_DIR).glob("*.js"))
        assert js_files, "expected static JS files"
        name = js_files[0].name
        response = asgi_client.get(f"/static/{name}")
        assert response.status_code == 200

    def test_settings_page(self, asgi_client):
        response = asgi_client.get("/settings")
        assert response.status_code == 200
        assert "text/html" in response.headers.get("content-type", "")
        assert "tr-sidebar" in response.text

    def test_targets_list_page(self, asgi_client):
        response = asgi_client.get("/targets")
        assert response.status_code == 200
        assert "Targets" in response.text
        assert "tr-sidebar" in response.text

    def test_target_kpis_page(self, asgi_client):
        response = asgi_client.get("/targets/demo/kpis")
        assert response.status_code == 200
        assert "KPI history" in response.text
        assert "kpi-metric" in response.text
        assert 'id="kpi-plots"' in response.text
        assert 'id="kpi-group-jump"' in response.text
        assert '<details id="kpi-filter-disclosure" class="kpi-filter-disclosure" hidden>' in response.text
        assert "<summary>Filters</summary>" in response.text

    def test_collections_list_page(self, asgi_client):
        response = asgi_client.get("/collections")
        assert response.status_code == 200
        assert "Collections" in response.text
        assert "tr-sidebar" in response.text

    def test_tool_redirect(self, asgi_client):
        response = asgi_client.get("/targets/demo/analyzer", follow_redirects=False)
        assert response.status_code in (302, 307)
        assert "analyzer" in response.headers.get("location", "")


class TestWebSocketContracts:
    def test_nunit_run_started_roundtrip(self, asgi_client):
        with asgi_client.websocket_connect("/ws/nunit") as ws:
            payload = {
                F_TYPE: MSG_RUN_STARTED,
                F_RETENTION_DAYS: 7,
                F_LOCAL_RUN: False,
                F_USER_METADATA: {},
                F_TARGET_KEY: "contract-target",
                F_PURPOSE: "manual",
                F_SOURCES: {
                    "test-system": {
                        F_SOURCE_BRANCH: "main",
                        F_SOURCE_REVISION: "abc123",
                        F_SOURCE_REPOSITORY_URL: "https://example.invalid/repo",
                    }
                },
            }
            ws.send_bytes(msgpack.packb(payload, use_bin_type=True))
            raw = ws.receive_bytes()
            data = msgpack.unpackb(raw, raw=False)
            assert data.get(F_TYPE) == MSG_RUN_STARTED_RESPONSE
            assert F_ERROR not in data
            assert data.get(F_RUN_ID)

    def test_attachment_upload_list_download(self, asgi_client, monkeypatch):
        monkeypatch.setattr("testrift_server.handlers.ATTACHMENTS_ENABLED", True)
        monkeypatch.setattr("testrift_server.config.ATTACHMENTS_ENABLED", True)

        with asgi_client.websocket_connect("/ws/nunit") as ws:
            payload = {
                F_TYPE: MSG_RUN_STARTED,
                F_RETENTION_DAYS: 7,
                F_LOCAL_RUN: False,
                F_USER_METADATA: {},
                F_TARGET_KEY: "attach-target",
                F_PURPOSE: "manual",
                F_SOURCES: {
                    "test-system": {
                        F_SOURCE_BRANCH: "main",
                        F_SOURCE_REVISION: "abc",
                        F_SOURCE_REPOSITORY_URL: "https://example.invalid/repo",
                    }
                },
            }
            ws.send_bytes(msgpack.packb(payload, use_bin_type=True))
            data = msgpack.unpackb(ws.receive_bytes(), raw=False)
            run_id = data[F_RUN_ID]

            from testrift_server.models import TestCaseData
            from testrift_server.utils import TC_ID_FIELD

            run = asgi_client.ws_server.test_runs[run_id]
            tc = TestCaseData(run, "Attach.Test", {TC_ID_FIELD: "tcattach01", "status": "passed"})
            run.test_cases[tc.id] = tc
            run.test_cases_by_tc_id[tc.tc_id] = tc

            files = {"attachment": ("note.txt", b"hello-contract", "text/plain")}
            upload = asgi_client.post(
                f"/api/attachments/{run_id}/{tc.tc_id}/upload",
                files=files,
            )
            assert upload.status_code == 200, upload.text
            assert upload.json().get("success") is True

            listed = asgi_client.get(f"/api/attachments/{run_id}/{tc.tc_id}/list")
            assert listed.status_code == 200
            names = [a["filename"] for a in listed.json().get("attachments", [])]
            assert "note.txt" in names

            downloaded = asgi_client.get(
                f"/api/attachments/{run_id}/{tc.tc_id}/download/note.txt"
            )
            assert downloaded.status_code == 200
            assert downloaded.content == b"hello-contract"
