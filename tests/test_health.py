from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import API_HEADERS

client = TestClient(app)


def test_health_ok():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_ping_rejects_missing_key():
    r = client.get("/v1/ping")
    assert r.status_code == 401
    assert r.json()["error"] == "http_error"


def test_ping_rejects_wrong_key():
    assert client.get("/v1/ping", headers={"X-API-Key": "wrong"}).status_code == 401


def test_ping_accepts_valid_key():
    r = client.get("/v1/ping", headers=API_HEADERS)
    assert r.status_code == 200
    assert r.json()["service"] == "lead-to-cash-api"


def test_request_id_is_echoed_or_generated():
    r = client.get("/health", headers={"X-Request-ID": "n8n-exec-123"})
    assert r.headers["X-Request-ID"] == "n8n-exec-123"
    generated = client.get("/health", headers={"X-Request-ID": "bad id <script>"})
    assert generated.headers["X-Request-ID"] != "bad id <script>"
