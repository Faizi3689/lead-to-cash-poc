from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app

client = TestClient(app)


def test_health_ok():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_ping_rejects_missing_key():
    assert client.get("/v1/ping").status_code == 401


def test_ping_rejects_wrong_key():
    assert client.get("/v1/ping", headers={"X-API-Key": "wrong"}).status_code == 401


def test_ping_accepts_valid_key():
    r = client.get("/v1/ping", headers={"X-API-Key": get_settings().api_key})
    assert r.status_code == 200
    assert r.json()["service"] == "lead-to-cash-api"
