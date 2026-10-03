"""Opt-in write protection and CORS behaviour."""

from __future__ import annotations

import pytest
from app.main import app
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    return TestClient(app)


def test_open_by_default_for_demo_mode(client, monkeypatch):
    monkeypatch.delenv("WORKBENCH_API_KEY", raising=False)
    assert client.post("/api/plans/1/dry-run").status_code == 200


def test_mutations_require_key_when_configured(client, monkeypatch):
    monkeypatch.setenv("WORKBENCH_API_KEY", "s3cret")
    assert client.post("/api/plans/1/approve", json="X").status_code == 401
    assert client.post("/api/reset").status_code == 401
    assert client.post("/api/reset", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.post("/api/reset", headers={"X-API-Key": "s3cret"}).status_code == 200


def test_reads_and_health_stay_open_when_key_configured(client, monkeypatch):
    monkeypatch.setenv("WORKBENCH_API_KEY", "s3cret")
    assert client.get("/api/audit-trail").status_code == 200
    assert client.get("/healthz").status_code == 200


def test_approval_and_execute_blocked_without_key(client, monkeypatch):
    monkeypatch.setenv("WORKBENCH_API_KEY", "s3cret")
    assert client.post("/api/plans/1/execute").status_code == 401
    assert client.post("/api/rollback", json={"snapshot_id": "x", "run_id": "y"}).status_code == 401
