"""Test setup.

Database tests run against TEST_DATABASE_URL (any Postgres with the migrations applied - your
Supabase URL works). Each test runs inside a transaction that is ROLLED BACK afterwards, so no
test data is ever left behind. Without TEST_DATABASE_URL, database tests are skipped.
"""
import os

os.environ["API_KEY"] = "test-api-key"
os.environ["APP_ENV"] = "test"
os.environ["LLM_PROVIDER"] = "mock"
TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
if TEST_DATABASE_URL:
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db import engine, get_db  # noqa: E402
from app.main import app  # noqa: E402

API_HEADERS = {"X-API-Key": "test-api-key"}


@pytest.fixture
def db_session():
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    connection = engine.connect()
    outer = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint",
                      expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        outer.rollback()
        connection.close()


@pytest.fixture
def client(db_session):
    app.dependency_overrides[get_db] = lambda: db_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
