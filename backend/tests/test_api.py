import pytest
from app.main import app
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    return TestClient(app)


def test_get_schemas(client):
    res = client.get("/api/schemas/source")
    assert res.status_code == 200
    data = res.json()
    assert "schema" in data
    assert "profiles" in data
    assert data["total_sample_records"] == 1000

    res_tgt = client.get("/api/schemas/target")
    assert res_tgt.status_code == 200
    tgt_data = res_tgt.json()
    assert tgt_data["schema_id"] == "modern_core_v2"


def test_transforms_catalog(client):
    res = client.get("/api/transforms")
    assert res.status_code == 200
    rules = res.json()
    rule_ids = [r["rule_id"] for r in rules]
    assert "DATE_TO_ISO8601" in rule_ids
    assert "PHONE_TO_E164" in rule_ids
    assert "UUID_V5_FROM_KEY" in rule_ids


def test_plans_lifecycle_api(client):
    # Reset
    client.post("/api/reset")

    # 1. List plans (initial plan v1 should exist)
    res = client.get("/api/plans")
    assert res.status_code == 200
    plans = res.json()
    assert len(plans) >= 1

    # 2. Dry run without approval
    res_dry = client.post("/api/plans/1/dry-run")
    assert res_dry.status_code == 200
    dry_data = res_dry.json()
    assert dry_data["total_source_records"] == 1000
    assert dry_data["accepted_count"] + dry_data["rejected_count"] == 1000

    # 3. Attempt execution without approval -> MUST FAIL (400)
    res_exec_unapproved = client.post("/api/plans/1/execute", json={"plan_version": 1, "executed_by": "Aditya"})
    assert res_exec_unapproved.status_code == 400
    assert "must approve the plan before execution" in res_exec_unapproved.json()["detail"]

    # 4. Approve plan
    res_appr = client.post("/api/plans/1/approve", json="Staff Engineer")
    assert res_appr.status_code == 200
    assert res_appr.json()["status"] == "APPROVED"

    # 5. Execute approved plan
    res_exec = client.post("/api/plans/1/execute", json={"plan_version": 1, "executed_by": "Aditya"})
    assert res_exec.status_code == 200
    exec_data = res_exec.json()
    assert exec_data["status"] == "SUCCESS"
    assert exec_data["inserted_count"] > 0
    run_id = exec_data["run_id"]
    snap_id = exec_data["snapshot_id"]

    # 6. Reconciliation report
    res_recon = client.get(f"/api/reconciliation/{run_id}?plan_version=1")
    assert res_recon.status_code == 200
    recon_data = res_recon.json()
    assert recon_data["invariants_passed"] is True
    assert recon_data["accounting"]["unaccounted_records"] == 0

    # 7. Rollback
    res_rollback = client.post("/api/rollback", json={"snapshot_id": snap_id, "run_id": run_id, "actor": "Aditya"})
    assert res_rollback.status_code == 200
    assert res_rollback.json()["status"] == "SUCCESS"

    # Verify target store is empty after rollback
    res_tgt_recs = client.get("/api/target/records")
    assert res_tgt_recs.json()["total_records"] == 0


def test_clarifications_and_mapping_customization(client):
    client.post("/api/reset")

    # 1. Update clarification question policy
    res_clarif = client.post(
        "/api/plans/1/clarifications",
        json={
            "question_id": "clarify_bad_phones",
            "user_answer": "Set phone_e164 to null and import remainder of record",
        },
    )
    assert res_clarif.status_code == 200
    plan_data = res_clarif.json()
    phone_map = next(m for m in plan_data["field_mappings"] if m["target_field"] == "phone_e164")
    assert phone_map["parameters"].get("on_invalid") == "null"

    # 2. Update a field mapping directly
    res_map = client.post(
        "/api/plans/1/mappings",
        json={
            "target_field": "last_name",
            "source_fields": ["full_name_raw"],
            "transformation": "TRIM_CLEAN",
            "parameters": {},
            "risk_level": "LOW",
            "risk_rationale": "Directly trimmed",
            "notes": "Customized by user",
        },
    )
    assert res_map.status_code == 200
    updated_plan = res_map.json()
    last_name_map = next(m for m in updated_plan["field_mappings"] if m["target_field"] == "last_name")
    assert last_name_map["transformation"] == "TRIM_CLEAN"


def test_load_test_records_endpoint(client):
    res = client.post("/api/load-test-records")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "SUCCESS"
    assert data["total_records"] == 100
    assert "user_test_records.csv" in data["message"]
    assert "plan" in data
