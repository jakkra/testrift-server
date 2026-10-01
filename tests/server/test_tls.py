"""TLS certificate generation, ingest HTTPS gate, and server-info fields."""

from __future__ import annotations

import asyncio
import logging
import shutil
import socket
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from starlette.responses import Response
from starlette.testclient import TestClient

from testrift_server import auth, config, database
from testrift_server.tls_certs import (
    current_material,
    fingerprint_der,
    format_fingerprint,
    ingest_base_url,
    parse_ingest_port,
    parse_tls_config,
    reset_material_for_tests,
    setup_tls,
    ui_tls_enabled,
)
from testrift_server.tr_server import create_app, _ssl_kwargs
from testrift_server.websocket import WebSocketServer


def _tls_config(**overrides):
    cfg = {
        "ui": "off",
        "ingest": "off",
        "cert_file": "",
        "key_file": "",
    }
    cfg.update(overrides)
    return cfg


@pytest.fixture
def tls_data_dir():
    temp_dir = tempfile.mkdtemp()
    reset_material_for_tests()
    yield Path(temp_dir)
    reset_material_for_tests()
    shutil.rmtree(temp_dir, ignore_errors=True)


def test_parse_tls_rejects_unknown_mode():
    with pytest.raises(ValueError, match="tls.ingest"):
        parse_tls_config({"ingest": "maybe"})


def test_parse_tls_rejects_mixed_auto_and_files():
    with pytest.raises(ValueError, match="cannot mix"):
        parse_tls_config({"ingest": "auto", "ui": "files", "cert_file": "a", "key_file": "b"})


def test_parse_tls_files_requires_paths():
    with pytest.raises(ValueError, match="cert_file"):
        parse_tls_config({"ingest": "files"})


def test_ingest_port_defaults_when_ui_is_http():
    tls = _tls_config(ingest="auto")
    assert parse_ingest_port({}, tls, 8080) == 8443


def test_ingest_port_must_differ_from_ui_when_ui_is_http():
    tls = _tls_config(ingest="auto")
    with pytest.raises(ValueError, match="differ"):
        parse_ingest_port({"ingest_port": 8080}, tls, 8080)


def test_ingest_port_unused_when_tls_off():
    assert parse_ingest_port({"ingest_port": 8443}, _tls_config(), 8080) is None


def test_ingest_port_shared_when_ui_also_tls():
    tls = _tls_config(ingest="auto", ui="auto")
    assert parse_ingest_port({}, tls, 8080) is None


def test_auto_certs_generate_reuse_and_fingerprint(tls_data_dir, caplog):
    cfg = {
        "data_dir": tls_data_dir,
        "tls": _tls_config(ingest="auto"),
        "port": 8080,
    }
    caplog.set_level(logging.INFO)
    first = setup_tls(cfg)
    assert first is not None
    assert first.ca_cert_path.is_file()
    assert first.chain_path.is_file()
    assert "TLS CA fingerprint (SHA-256):" in caplog.text
    assert first.ca_fingerprint in caplog.text
    assert first.ca_fingerprint.count(":") == 31

    ca_pem = first.ca_cert_path.read_bytes()
    server_cert = x509.load_pem_x509_certificate(first.cert_path.read_bytes())
    sans = server_cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    dns = set(sans.get_values_for_type(x509.DNSName))
    ips = {str(name) for name in sans.get_values_for_type(x509.IPAddress)}
    assert "localhost" in dns
    assert "127.0.0.1" in ips
    assert "::1" in ips

    second = setup_tls(cfg)
    assert second.ca_fingerprint == first.ca_fingerprint
    assert second.ca_cert_path.read_bytes() == ca_pem
    der = x509.load_pem_x509_certificate(ca_pem).public_bytes(serialization.Encoding.DER)
    assert fingerprint_der(der) == first.ca_fingerprint
    assert format_fingerprint(first.ca_fingerprint.replace(":", "")) == first.ca_fingerprint


def test_ingest_base_url_uses_ingest_port():
    cfg = {
        "port": 8080,
        "ingest_port": 8443,
        "tls": _tls_config(ingest="auto"),
    }
    assert ingest_base_url(cfg, "127.0.0.1:8080") == "https://127.0.0.1:8443"
    assert ingest_base_url(cfg, "localhost:8080") == "https://localhost:8443"
    assert ingest_base_url(cfg, "[::1]:8080") == "https://[::1]:8443"


def test_ingest_base_url_http_when_tls_off():
    cfg = {"port": 8080, "ingest_port": None, "tls": _tls_config()}
    assert ingest_base_url(cfg, "127.0.0.1:8080") == "http://127.0.0.1:8080"


@pytest.fixture
def tls_ingest_app(monkeypatch, tls_data_dir):
    cfg = dict(config.CONFIG)
    cfg["data_dir"] = tls_data_dir
    cfg["tls"] = _tls_config(ingest="auto")
    cfg["ingest_port"] = 8443
    monkeypatch.setattr(config, "CONFIG", cfg)
    monkeypatch.setattr("testrift_server.tr_server.DATA_DIR", tls_data_dir)
    monkeypatch.setattr("testrift_server.config.DATA_DIR", tls_data_dir)
    database.initialize_database(tls_data_dir)
    setup_tls(cfg)
    app = create_app(ws_server=WebSocketServer())
    return app


def test_http_ingest_rejected_when_tls_on(tls_ingest_app):
    with TestClient(tls_ingest_app) as client:
        response = client.post("/api/runs/run1/commits", json={})
        assert response.status_code == 400
        assert response.json()["error"] == auth.INGEST_TLS_REQUIRED_MESSAGE
        kpi_response = client.post("/api/runs/run1/kpis", json={})
        assert kpi_response.status_code == 400
        assert kpi_response.json()["error"] == auth.INGEST_TLS_REQUIRED_MESSAGE
        with pytest.raises(Exception):
            with client.websocket_connect("/ws/nunit"):
                pass


def test_https_ingest_allowed_when_tls_on(tls_ingest_app):
    with TestClient(tls_ingest_app, base_url="https://testserver") as client:
        response = client.post("/api/runs/run1/commits", json={"diffs": []})
        assert response.json().get("error") != auth.INGEST_TLS_REQUIRED_MESSAGE


def test_nunit_websocket_requires_tls_scheme(monkeypatch):
    cfg = dict(config.CONFIG)
    cfg["tls"] = _tls_config(ingest="auto")
    monkeypatch.setattr(config, "CONFIG", cfg)
    monkeypatch.setattr(auth, "ingest_token_configured", lambda: "")

    from types import SimpleNamespace
    from testrift_server.tls_certs import ingest_tls_enabled
    assert ingest_tls_enabled() is True

    def _ws(scheme):
        return SimpleNamespace(url=SimpleNamespace(scheme=scheme), headers={}, cookies={})

    assert asyncio.run(auth.enforce_websocket(_ws("wss"), "/ws/nunit")) is True
    assert asyncio.run(auth.enforce_websocket(_ws("https"), "/ws/nunit")) is True
    assert asyncio.run(auth.enforce_websocket(_ws("ws"), "/ws/nunit")) is False
    assert asyncio.run(auth.enforce_websocket(_ws("http"), "/ws/nunit")) is False


def test_server_info_includes_ingest_tls_fields(tls_ingest_app):
    material = current_material()
    with TestClient(tls_ingest_app) as client:
        body = client.get("/api/server-info").json()
        assert body["ingest_url"] == "https://testserver:8443"
        assert body["tls_ca_fingerprint"] == material.ca_fingerprint
        ca = client.get("/ca.crt")
        assert ca.status_code == 200
        assert b"BEGIN CERTIFICATE" in ca.content


def test_ca_crt_missing_when_tls_off(monkeypatch, tls_data_dir):
    monkeypatch.setattr("testrift_server.config.DATA_DIR", tls_data_dir)
    monkeypatch.setattr("testrift_server.tr_server.DATA_DIR", tls_data_dir)
    database.initialize_database(tls_data_dir)
    app = create_app(ws_server=WebSocketServer())
    with TestClient(app) as client:
        assert client.get("/ca.crt").status_code == 404
        body = client.get("/api/server-info").json()
        assert body["ingest_url"].startswith("http://")
        assert body["tls_ca_fingerprint"] is None


def _write_user_cert(directory: Path) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "testrift-test")]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "testrift-test")]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=2))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / "user.crt"
    key_path = directory / "user.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def test_parse_ingest_port_shared_when_both_use_files():
    tls = _tls_config(ui="files", ingest="files", cert_file="c", key_file="k")
    assert parse_ingest_port({}, tls, 8080) is None


def test_files_mode_requires_existing_paths(tls_data_dir):
    cfg = {
        "data_dir": tls_data_dir,
        "tls": _tls_config(ui="files", ingest="files", cert_file=str(tls_data_dir / "missing.crt"), key_file=str(tls_data_dir / "missing.key")),
        "port": 8080,
    }
    with pytest.raises(ValueError, match="cert_file not found"):
        setup_tls(cfg)


def test_files_mode_loads_user_certificate(tls_data_dir, caplog):
    cert_path, key_path = _write_user_cert(tls_data_dir)
    cfg = {
        "data_dir": tls_data_dir,
        "tls": _tls_config(ui="files", ingest="files", cert_file=str(cert_path), key_file=str(key_path)),
        "port": 8080,
    }
    caplog.set_level(logging.INFO)
    material = setup_tls(cfg)
    assert material is not None
    assert material.cert_path == cert_path.resolve()
    assert material.key_path == key_path.resolve()
    assert material.ca_cert_path is None
    assert material.ca_fingerprint.count(":") == 31
    assert "TLS CA fingerprint (SHA-256):" in caplog.text
    assert material.ca_fingerprint in caplog.text


def test_ingest_base_url_https_when_ui_uses_files():
    cfg = {
        "port": 8080,
        "ingest_port": None,
        "tls": _tls_config(ui="files", ingest="files", cert_file="c", key_file="k"),
    }
    assert ingest_base_url(cfg, "127.0.0.1:8080") == "https://127.0.0.1:8080"


def test_session_cookie_secure_only_when_ui_tls(monkeypatch):
    cfg = dict(config.CONFIG)
    cfg["tls"] = _tls_config()
    monkeypatch.setattr(config, "CONFIG", cfg)
    assert ui_tls_enabled() is False
    response = Response()
    auth.apply_session_cookie(response, "token-value")
    assert "secure" not in response.headers.get("set-cookie", "").lower()

    cfg["tls"] = _tls_config(ui="files", cert_file="c", key_file="k")
    monkeypatch.setattr(config, "CONFIG", cfg)
    assert ui_tls_enabled() is True
    response = Response()
    auth.apply_session_cookie(response, "token-value")
    assert "secure" in response.headers.get("set-cookie", "").lower()


def test_ui_https_with_user_cert_serves_health(monkeypatch, tls_data_dir):
    cert_path, key_path = _write_user_cert(tls_data_dir)
    cfg = dict(config.CONFIG)
    cfg["data_dir"] = tls_data_dir
    cfg["port"] = 8080
    cfg["ingest_port"] = None
    cfg["tls"] = _tls_config(ui="files", ingest="files", cert_file=str(cert_path), key_file=str(key_path))
    monkeypatch.setattr(config, "CONFIG", cfg)
    monkeypatch.setattr("testrift_server.tr_server.DATA_DIR", tls_data_dir)
    monkeypatch.setattr("testrift_server.config.DATA_DIR", tls_data_dir)
    database.initialize_database(tls_data_dir)
    setup_tls(cfg)
    ssl_kwargs = _ssl_kwargs()
    assert Path(ssl_kwargs["ssl_certfile"]) == cert_path.resolve()
    assert Path(ssl_kwargs["ssl_keyfile"]) == key_path.resolve()

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    app = create_app(ws_server=WebSocketServer())
    import uvicorn
    uv_cfg = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
        timeout_graceful_shutdown=2,
        lifespan="on",
        **ssl_kwargs,
    )
    server = uvicorn.Server(uv_cfg)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 8
    try:
        while not getattr(server, "started", False):
            if time.time() > deadline:
                raise TimeoutError("UI HTTPS test server did not start")
            time.sleep(0.05)
        with httpx.Client(verify=False, timeout=2.0) as client:
            health = client.get(f"https://127.0.0.1:{port}/health")
            assert health.status_code == 200
            assert health.json() == {"status": "ok"}
            info = client.get(f"https://127.0.0.1:{port}/api/server-info")
            assert info.status_code == 200
            body = info.json()
            assert body["ingest_url"].startswith("https://")
            assert client.get(f"https://127.0.0.1:{port}/ca.crt").status_code == 404
        probed = config.get_running_server_info(port)
        assert probed is not None
        assert probed.get("service") == "testrift-server"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
