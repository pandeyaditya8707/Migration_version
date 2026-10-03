import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.models import MigrationPlan, FieldMapping
from app.models.schemas import current_utc_iso
from app.main import plan_manager, v2_state, v2_inspection_tools

@pytest.fixture
def client():
    return TestClient(app)

def test_mode1_apply_ai_fix_single_and_all(client):
    # Propose initial plan
    init_res = client.post("/api/plans/propose")
    assert init_res.status_code == 200
    plan_data = init_res.json()
    version = plan_data["version"]

    # Initial dry run
    dry_res = client.post(f"/api/plans/{version}/dry-run")
    assert dry_res.status_code == 200
    dry_data = dry_res.json()
    initial_quarantine = dry_data["rejected_count"]
    assert initial_quarantine > 0

    # 1. Apply single field fix for joined_at
    fix1_res = client.post(f"/api/plans/{version}/apply-fix", json={
        "target_field": "joined_at",
        "rule": "DATE_TO_ISO8601",
        "raw_value": "INVALID_TIMESTAMP"
    })
    assert fix1_res.status_code == 200
    fix1_data = fix1_res.json()
    assert fix1_data["status"] == "SUCCESS"
    quar_after_fix1 = fix1_data["dry_run_summary"]["rejected_count"]
    assert quar_after_fix1 < initial_quarantine

    # 2. Apply AUTO_RESOLVE_ALL to fix remaining fields (phone, email, first_name)
    fix_all_res = client.post(f"/api/plans/{version}/apply-fix", json={
        "fix_action": "AUTO_RESOLVE_ALL"
    })
    assert fix_all_res.status_code == 200
    fix_all_data = fix_all_res.json()
    assert fix_all_data["status"] == "SUCCESS"
    # AUTO_RESOLVE_ALL resolves date, phone, email anomalies; leaves 9 unfixable fatal schema violations
    assert fix_all_data["dry_run_summary"]["rejected_count"] == 9
    assert fix_all_data["dry_run_summary"]["accepted_count"] == 991

def test_mode2_apply_ai_fix_lifecycle(client):
    # Set up mode 2 active target schema & plan
    target_schema = {
        "schema_id": "test_schema_v2",
        "name": "Dynamic Test Schema",
        "fields": [
            {
                "name": "invoice_id",
                "type": "string",
                "nullable": False,
                "constraints": {"required": True}
            },
            {
                "name": "status",
                "type": "string",
                "nullable": False,
                "constraints": {"enum": ["PAID", "PENDING", "CANCELLED"]}
            }
        ]
    }
    v2_state["active_target_schema"] = target_schema
    now_iso = current_utc_iso()
    plan = MigrationPlan(
        plan_id="v2_plan_test",
        version=1,
        title="Test V2 Plan",
        description="Testing V2 remediation",
        source_schema_id="source_custom",
        target_schema_id="test_schema_v2",
        field_mappings=[
            FieldMapping(
                target_field="invoice_id",
                source_fields=["invoice_id"],
                transformation="COALESCE_VAL",
                parameters={},
                risk_level="LOW"
            ),
            FieldMapping(
                target_field="status",
                source_fields=["status_code"],
                transformation="DIRECT_COPY",
                parameters={},
                risk_level="LOW"
            )
        ],
        clarifications=[],
        status="PROPOSED",
        created_at=now_iso,
        updated_at=now_iso
    )
    v2_state["active_plan"] = plan
    v2_inspection_tools.records = [
        {"invoice_id": "INV-101", "status_code": "PAID"},
        {"invoice_id": "", "status_code": "UNKNOWN_VAL"}
    ]

    # Apply Auto-resolve all
    fix_res = client.post("/api/v2/plans/apply-fix", json={
        "fix_action": "AUTO_RESOLVE_ALL"
    })
    assert fix_res.status_code == 200
    res_data = fix_res.json()
    assert res_data["status"] == "SUCCESS"
    assert res_data["dry_run_summary"]["quarantined_count"] == 0
    assert res_data["dry_run_summary"]["valid_count"] == 2

def test_ai_diagnosis_endpoints(client):
    res = client.post("/api/ai/diagnose-record", json={
        "field": "joined_at",
        "rule": "DATE_TO_ISO8601",
        "raw_value": "INVALID_TIMESTAMP",
        "error_message": "Cannot parse as valid date",
        "source_payload": {"signup_date_str": "INVALID_TIMESTAMP"}
    })
    assert res.status_code == 200
    data = res.json()
    assert "diagnosis" in data
    assert data["status"] in ["SUCCESS", "FALLBACK_DIAGNOSIS"]
