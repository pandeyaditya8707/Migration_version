"""Regression suite for Mode 1 reconciliation accounting.

Guards the defect where the 'latest' reconciliation path summed the whole
quarantine ledger (dry-run entries + execution entries) and so reported
1000 = 902 + 196 - 98. Every scenario asserts conservation per run.
"""

from __future__ import annotations

import pytest
from app.main import app, target_store
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    return TestClient(app)


def _recon(client, run_id):
    res = client.get(f"/api/reconciliation/{run_id}?plan_version=1")
    assert res.status_code == 200, res.text
    return res.json()


def _approve_and_execute(client):
    assert client.post("/api/plans/1/approve", json="Reviewer").status_code == 200
    res = client.post("/api/plans/1/execute")
    assert res.status_code == 200, res.text
    return res.json()


def _assert_conserved(report, source=1000):
    a = report["accounting"]
    assert a["total_source_records"] == source
    assert a["target_accepted_records"] + a["quarantined_records"] == source, a
    assert a["unaccounted_records"] == 0, a
    assert report["invariants_passed"] is True, report["details"]


def test_latest_after_dry_run_and_execution_is_not_double_counted(client):
    for _ in range(2):  # repeated dry runs must not accumulate either
        assert client.post("/api/plans/1/dry-run").status_code == 200
    ex = _approve_and_execute(client)
    assert ex["quarantined_count"] == 98

    for alias in ("latest_dry", "latest", "latest_run"):
        report = _recon(client, alias)
        _assert_conserved(report)
        assert report["accounting"]["target_accepted_records"] == 902
        assert report["accounting"]["quarantined_records"] == 98
        assert report["verdict"] == "PASSED_WITH_QUARANTINE"
        assert report["run_id"] == ex["run_id"]  # alias resolved to a concrete run


def test_latest_before_execution_is_pending_and_uses_only_latest_dry_run(client):
    for _ in range(3):
        client.post("/api/plans/1/dry-run")
    report = _recon(client, "latest_dry")
    assert report["verdict"] == "PENDING_EXECUTION"
    assert report["accounting"]["quarantined_records"] == 98  # not 294
    assert report["accounting"]["unaccounted_records"] == 0


def test_retry_with_updates_inserts_and_skips_is_fully_accounted(client):
    _approve_and_execute(client)
    with target_store.get_connection() as conn:
        keys = [r[0] for r in conn.execute("SELECT natural_key FROM customers ORDER BY natural_key LIMIT 8")]
        conn.executemany("UPDATE customers SET status='INACTIVE', risk_tier='ZZ' WHERE natural_key=?", [(k,) for k in keys[:5]])
        conn.executemany("DELETE FROM customers WHERE natural_key=?", [(k,) for k in keys[5:]])
        conn.commit()

    retry = client.post("/api/plans/1/execute").json()
    assert retry["updated_count"] >= 1 and retry["inserted_count"] == 3
    assert retry["skipped_duplicates_count"] > 0

    report = _recon(client, retry["run_id"])
    _assert_conserved(report)
    assert report["accounting"]["target_accepted_records"] == 902
    # the 2nd run owns only part of the target rows but must still be fully accounted for
    assert report["verdict"] == "PASSED_WITH_QUARANTINE"


def test_fully_idempotent_rerun_reports_exact_accounting(client):
    _approve_and_execute(client)
    rerun = client.post("/api/plans/1/execute").json()
    assert rerun["inserted_count"] == 0 and rerun["updated_count"] == 0
    report = _recon(client, rerun["run_id"])
    _assert_conserved(report)
    assert report["accounting"]["target_accepted_records"] == 902


def test_reconciliation_after_rollback_is_not_a_false_discrepancy(client):
    ex = _approve_and_execute(client)
    rb = client.post("/api/rollback", json={"snapshot_id": ex["snapshot_id"], "run_id": ex["run_id"]})
    assert rb.json()["status"] == "SUCCESS"
    report = _recon(client, ex["run_id"])
    assert report["verdict"] == "ROLLED_BACK"
    assert report["accounting"]["target_accepted_records"] == 0
    assert report["invariants_passed"] is True
    assert report["accounting"]["unaccounted_records"] == 0


def test_tampered_quarantine_ledger_is_detected(client):
    ex = _approve_and_execute(client)
    with target_store.get_connection() as conn:
        conn.execute("DELETE FROM quarantine_ledger WHERE run_id=? AND source_row_index IN (SELECT source_row_index FROM quarantine_ledger WHERE run_id=? LIMIT 3)", (ex["run_id"], ex["run_id"]))
        conn.commit()
    report = _recon(client, ex["run_id"])
    assert report["verdict"] == "DISCREPANCY_DETECTED"
    assert report["accounting"]["unaccounted_records"] == 3
    assert report["invariants_passed"] is False


def test_unknown_run_id_is_pending_not_a_fabricated_total(client):
    _approve_and_execute(client)
    report = _recon(client, "exec_run_doesnotexist")
    assert report["verdict"] == "PENDING_EXECUTION"
    assert report["accounting"]["target_accepted_records"] == 0


def test_quarantine_export_is_scoped_to_current_run_not_whole_ledger(client):
    client.post("/api/plans/1/dry-run")
    _approve_and_execute(client)
    res = client.get("/api/export/quarantine")
    assert res.status_code == 200
    data_lines = [ln for ln in res.text.strip().splitlines()[1:] if ln.strip()]
    assert len(data_lines) >= 98  # one row per quarantined record ...
    ids = client.get("/api/quarantine/records?limit=200").json()
    assert {r["run_id"] for r in ids} != set() and len({r["run_id"] for r in ids}) == 1  # ... from a single run


# ---------------------------------------------------------------- Mode 2 ----
def _seed_v2_execution(**overrides):
    from app.main import dynamic_store, v2_state

    base = {"run_id": "v2_exec_test", "inserted_count": 70, "updated_count": 10, "skipped_duplicates_count": 5,
            "quarantined_count": 15, "total_source_records": 100}
    base.update(overrides)
    v2_state["last_execution_result"] = None
    dynamic_store.save_state("last_execution_result", base)


def test_v2_reconciliation_counts_skipped_rows_and_balances(client):
    _seed_v2_execution()
    r = client.get("/api/v2/reconciliation").json()
    assert r["source_records"] == 100 and r["accepted_records"] == 85
    assert r["unaccounted_records"] == 0 and r["verdict"] == "PASSED_WITH_QUARANTINE"


def test_v2_reconciliation_detects_over_counting_instead_of_clamping_to_zero(client):
    _seed_v2_execution(quarantined_count=30)  # 85 + 30 = 115 > 100 : double counting
    r = client.get("/api/v2/reconciliation").json()
    assert r["unaccounted_records"] == -15
    assert r["verdict"] == "DISCREPANCY_DETECTED" and r["invariants_passed"] is False


def test_v2_reconciliation_detects_under_counting(client):
    _seed_v2_execution(inserted_count=60)
    r = client.get("/api/v2/reconciliation").json()
    assert r["unaccounted_records"] == 10 and r["verdict"] == "DISCREPANCY_DETECTED"
