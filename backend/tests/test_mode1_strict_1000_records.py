import pytest
import os
from fastapi.testclient import TestClient
from app.main import app, plan_manager, inspection_tools, target_store
from app.engine.agent import MigrationPlannerAgent

@pytest.fixture
def client():
    # Guarantee clean state for Mode 1
    c = TestClient(app)
    c.post("/api/reset")
    return c

def test_mode1_source_dataset_strictly_1000_records(client):
    """Verify Mode 1 source inspection tool has exactly 1,000 records with full field profiling."""
    assert len(inspection_tools.records) == 1000, f"Expected 1000 source records, got {len(inspection_tools.records)}"
    
    # Verify mandatory fields present in all 1000 records
    expected_fields = {
        "legacy_account_id",
        "full_name_raw",
        "email_address",
        "phone_raw",
        "signup_date_str",
        "account_status_code",
        "balance_due_str",
        "risk_flag",
        "country_code_raw"
    }
    for idx, r in enumerate(inspection_tools.records):
        assert expected_fields.issubset(r.keys()), f"Row #{idx} missing expected keys"

def test_mode1_plan_proposing_and_invariants(client):
    """Verify Mode 1 benchmark plan contains 11 target fields and active risk invariants."""
    res = client.get("/api/plans/1")
    assert res.status_code == 200
    plan = res.json()
    assert plan["version"] == 1
    assert plan["status"] == "PROPOSED"
    assert plan["approved_by"] is None
    assert plan["approved_at"] is None
    assert len(plan["field_mappings"]) == 11

    # Check risk level coverage
    risks = {m["risk_level"] for m in plan["field_mappings"]}
    assert "HIGH" in risks or "MEDIUM" in risks
    assert "LOW" in risks

    # Target fields match target schema contract
    target_fields = [m["target_field"] for m in plan["field_mappings"]]
    assert "customer_uuid" in target_fields
    assert "natural_key" in target_fields
    assert "first_name" in target_fields
    assert "last_name" in target_fields
    assert "email" in target_fields
    assert "phone_e164" in target_fields
    assert "joined_at" in target_fields
    assert "status" in target_fields
    assert "balance_due" in target_fields

def test_mode1_unapproved_execution_blocked(client):
    """Strict security invariant: unapproved plan CANNOT execute writes to target database."""
    # Attempt to execute unapproved plan 1
    exec_res = client.post("/api/plans/1/execute")
    assert exec_res.status_code == 400
    err_msg = exec_res.json()["detail"].lower()
    assert "approve" in err_msg

def test_mode1_deterministic_dry_run_1000_records_mass_conservation(client):
    """Verify deterministic dry run across exactly 1,000 records satisfies pure conservation laws."""
    dry_res = client.post("/api/plans/1/dry-run")
    assert dry_res.status_code == 200
    summary = dry_res.json()

    total = summary["total_source_records"]
    accepted = summary["accepted_count"]
    rejected = summary["rejected_count"]

    assert total == 1000, f"Expected 1000 total source records evaluated, got {total}"
    assert accepted + rejected == total, f"Mass conservation violated: {accepted} + {rejected} != {total}"
    assert accepted == 902, f"Expected deterministic 902 accepted records, got {accepted}"
    assert rejected == 98, f"Expected deterministic 98 quarantined records, got {rejected}"

    # Verify quarantine forensics evidence integrity
    quar_samples = summary["quarantine_sample"]
    assert len(quar_samples) == 98
    for q in quar_samples:
        assert "source_row_index" in q
        assert "source_natural_key" in q
        assert "errors" in q
        assert len(q["errors"]) > 0
        first_err = q["errors"][0]
        assert first_err["field"] != ""
        assert first_err["error_message"] != ""

def test_mode1_execution_idempotency_and_reconciliation(client):
    """Strict execution write test: approved plan writes valid records, duplicate run is idempotent, reconciliation delta is 0."""
    # Reset target store table cleanly
    target_store.reset_database()

    # Approve plan 1
    appr_res = client.post("/api/plans/1/approve", json="Senior Data Architect")
    assert appr_res.status_code == 200
    approved_plan = appr_res.json()
    assert approved_plan["status"] == "APPROVED"
    assert approved_plan["approved_by"] == "Senior Data Architect"

    # Execute migration
    exec_res = client.post("/api/plans/1/execute")
    assert exec_res.status_code == 200
    exec_data = exec_res.json()
    assert exec_data["status"] == "SUCCESS"
    assert exec_data["inserted_count"] == 902
    assert exec_data["quarantined_count"] == 98
    assert exec_data["total_source_records"] == 1000
    run_id = exec_data["run_id"]
    snapshot_id = exec_data["snapshot_id"]

    # Query target SQLite table directly to verify actual database state
    with target_store.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT count(*) FROM customers;")
        db_count = cursor.fetchone()[0]
        assert db_count == 902, f"Target database customer count expected 902, got {db_count}"

        # Verify deterministic UUIDv5 primary keys and natural key references
        cursor.execute("SELECT customer_uuid, natural_key, first_name, email, status FROM customers LIMIT 5;")
        rows = cursor.fetchall()
        assert len(rows) == 5
        for uuid_val, nat_key, fname, email, status in rows:
            assert uuid_val.count("-") == 4  # Valid UUID format
            assert nat_key.startswith("LEGACY-CUST-")
            assert len(fname) > 0
            assert "@" in email
            assert status in ["ACTIVE", "SUSPENDED", "INACTIVE"]

    # Idempotency test: re-executing must NOT duplicate records
    exec_res2 = client.post("/api/plans/1/execute")
    assert exec_res2.status_code == 200
    with target_store.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT count(*) FROM customers;")
        db_count_after_retry = cursor.fetchone()[0]
        assert db_count_after_retry == 902, "Idempotency failed: database count changed upon retry!"

    # Universal Reconciliation check
    recon_res = client.get(f"/api/reconciliation/{run_id}?plan_version=1")
    assert recon_res.status_code == 200
    recon = recon_res.json()

    assert recon["invariants_passed"] is True
    assert recon["accounting"]["total_source_records"] == 1000
    assert recon["accounting"]["target_accepted_records"] == 902
    assert recon["accounting"]["quarantined_records"] == 98
    assert recon["accounting"]["unaccounted_records"] == 0, f"Unaccounted delta must be 0! Got {recon['accounting']}"

    # Rollback test: clean target store restoration
    rollback_res = client.post("/api/rollback", json={
        "snapshot_id": snapshot_id,
        "run_id": run_id,
        "actor": "Senior Data Architect"
    })
    assert rollback_res.status_code == 200
    rb_data = rollback_res.json()
    assert rb_data["status"] == "SUCCESS"
    assert rb_data["records_removed"] == 902
    assert target_store.get_customer_count() == 0

def test_mode1_no_ai_fix_buttons_or_remediation_in_mode1():
    """Verify Mode 1 UI template and JavaScript strictly hide all AI fix buttons and auto-apply actions."""
    html_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "frontend", "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        html_content = f.read()

    # Verify no auto-fix button in Mode 1 quarantine ledger
    assert "btn-auto-fix-all-mode1" not in html_content
    # Mode 2 retains its button
    assert "btn-auto-fix-all-mode2" in html_content

    # In Mode 1 Quarantine table, AI suggested fix column header is removed
    # Locate tab-dryrun section in Mode 1
    q_section = html_content[html_content.find('id="tab-dryrun"'):html_content.find('id="tab-execution"')]
    assert "🤖 AI Suggested Fix" not in q_section
    assert "Violated Target Field" in q_section

    # Verify app.js isolates AI remediation box strictly to Mode 2
    js_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "frontend", "js", "app.js")
    with open(js_path, "r", encoding="utf-8") as f:
        js_content = f.read()

    # Verify Mode 1 rendering does NOT render Fix & Re-run buttons
    assert "tbody-quarantine-ledger" in js_content
    assert "openQuarantineModal" in js_content
    assert "isMode2 = (mode === 'mode2')" in js_content
