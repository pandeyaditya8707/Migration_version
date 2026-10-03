import pytest
from fastapi.testclient import TestClient
from app.main import app, workbench_log_handler, set_v2_active_schema, set_v2_active_plan, dynamic_store, ollama_agent
from app.models import MigrationPlan, FieldMapping
from app.models.schemas import current_utc_iso
import json

@pytest.fixture
def client():
    return TestClient(app)

def mock_dynamic_plan(*args, **kwargs):
    source_schema = kwargs.get("source_schema") or {}
    target_schema = kwargs.get("target_schema") or {}
    tgt_fields = target_schema.get("fields", [])
    mappings = []
    for tf in tgt_fields:
        mappings.append(FieldMapping(
            target_field=tf["name"],
            source_fields=[tf["name"]],
            transformation="DIRECT_COPY",
            parameters={},
            risk_level="LOW",
            risk_rationale="Direct copy",
            notes="Mocked AI Plan"
        ))
    return MigrationPlan(
        plan_id="plan_v1",
        version=kwargs.get("plan_version", 1),
        title="Mock AI Plan",
        description="Fast mock AI plan for testing",
        source_schema_id="source_test",
        target_schema_id="target_test",
        field_mappings=mappings
    )

def test_repeated_uploads_mode2_resilience(client, monkeypatch):
    """Verifies that repeatedly uploading the same (or changing) dataset never crashes, desyncs, or corrupts state."""
    monkeypatch.setattr(ollama_agent, "_call_ollama_llm", mock_dynamic_plan)

    csv_content = """invoice_id,customer_name,amount_due,created_at,status
INV-001,Acme Corp,1500.50,2024-01-10T10:00:00Z,PAID
INV-002,Global Tech,3400.00,2024-01-12T14:30:00Z,PENDING
INV-003,Starlight LLC,250.75,2024-01-15T09:15:00Z,DUE
INV-004,Beta Labs,980.00,2024-01-18T16:00:00Z,PAID
INV-005,Omega Corp,4200.20,2024-01-20T11:45:00Z,PENDING
"""
    # 1. Set a valid active target schema for invoices
    schema_payload = {
        "schema_id": "schema_invoices_test",
        "name": "Target Invoices",
        "table_name": "target_invoices_test",
        "primary_key": ["invoice_id"],
        "fields": [
            {"name": "invoice_id", "type": "STRING", "nullable": False},
            {"name": "customer_name", "type": "STRING", "nullable": False},
            {"name": "amount_due", "type": "FLOAT", "nullable": False},
            {"name": "created_at", "type": "TIMESTAMP", "nullable": True},
            {"name": "status", "type": "STRING", "nullable": False}
        ]
    }
    schema_res = client.post("/api/v2/schema/target", json=schema_payload)
    assert schema_res.status_code == 200

    # 2. Upload the exact same file 5 times sequentially (simulating user repeatedly uploading/reloading)
    for i in range(5):
        upload_res = client.post(
            "/api/v2/upload/source",
            files={"file": (f"legacy_invoices_v1_dummy.csv", csv_content.encode("utf-8"), "text/csv")}
        )
        assert upload_res.status_code == 200, f"Upload iteration {i} failed: {upload_res.text}"
        data = upload_res.json()
        assert data["status"] == "SUCCESS"
        assert data["total_records"] == 5
        assert data["filename"] == "legacy_invoices_v1_dummy.csv"
        assert data["plan"] is not None
        assert len(data["plan"]["field_mappings"]) > 0

    # 3. Verify current plan is accessible and persisted in SQLite
    plan_res = client.get("/api/v2/plans/current")
    assert plan_res.status_code == 200
    plan_data = plan_res.json()
    assert plan_data["status"] in ["PROPOSED", "DRAFT"]

def test_mode2_end_to_end_lifecycle_and_rollback(client, monkeypatch):
    """Verifies Mode 2 Plan Approval, Dry-Run, Execution, and Rollback work end-to-end without bugs."""
    monkeypatch.setattr(ollama_agent, "_call_ollama_llm", mock_dynamic_plan)

    csv_content = """order_ref,client_title,total_val,order_date
ORD-101,Acme Supply,550.00,2024-02-01
ORD-102,Beta Logistics,1200.50,2024-02-02
ORD-103,Gamma Systems,90.25,2024-02-03
"""
    # Ensure clean target table state
    with dynamic_store.get_connection() as conn:
        conn.execute("DROP TABLE IF EXISTS target_orders_e2e")
        conn.commit()

    # 1. Compile target schema
    schema_payload = {
        "schema_id": "schema_orders_e2e",
        "name": "Target Orders E2E",
        "table_name": "target_orders_e2e",
        "primary_key": ["order_ref"],
        "fields": [
            {"name": "order_ref", "type": "STRING", "nullable": False},
            {"name": "client_title", "type": "STRING", "nullable": False},
            {"name": "total_val", "type": "FLOAT", "nullable": False},
            {"name": "order_date", "type": "STRING", "nullable": True}
        ]
    }
    schema_res = client.post("/api/v2/schema/target", json=schema_payload)
    assert schema_res.status_code == 200

    # 2. Upload dataset
    upload_res = client.post(
        "/api/v2/upload/source",
        files={"file": ("orders.csv", csv_content.encode("utf-8"), "text/csv")}
    )
    assert upload_res.status_code == 200

    # 3. Approve Plan
    approve_res = client.post("/api/v2/plans/approve", json={"approved_by": "Data Platform Architect"})
    assert approve_res.status_code == 200
    approved_plan = approve_res.json()
    assert approved_plan["status"] == "APPROVED"

    # 4. Dry Run
    dry_res = client.post("/api/v2/plans/dry-run")
    assert dry_res.status_code == 200
    dry_data = dry_res.json()
    assert dry_data["total_evaluated"] == 3
    assert dry_data["accepted_count"] == 3
    assert dry_data["rejected_count"] == 0

    # 5. Execute Migration
    exec_res = client.post("/api/v2/plans/execute", json={"executed_by": "Production Lead"})
    assert exec_res.status_code == 200
    exec_data = exec_res.json()
    assert exec_data["status"] in ["SUCCESS", "COMPLETED"]
    assert exec_data["inserted_count"] + exec_data["updated_count"] == 3
    assert "snapshot_id" in exec_data

    # 6. Verify records exist in target store
    target_res = client.get("/api/v2/target/records?limit=50&offset=0")
    assert target_res.status_code == 200
    assert target_res.json()["total_records"] == 3

    # 7. Rollback Migration
    rollback_res = client.post("/api/v2/rollback", json={"reason": "Customer migration rehearsal verification"})
    assert rollback_res.status_code == 200
    rb_data = rollback_res.json()
    assert rb_data["status"] == "ROLLED_BACK"
    assert rb_data["removed_records"] == 3

    # 8. Verify target records are rolled back to 0
    target_after = client.get("/api/v2/target/records?limit=50&offset=0")
    assert target_after.status_code == 200
    assert target_after.json()["total_records"] == 0

def test_system_logs_subsystem_api(client):
    """Verifies that system logs capture events, support filtering, and clear properly."""
    # 1. Fetch system logs
    res = client.get("/api/logs?limit=50&level=ALL")
    assert res.status_code == 200
    data = res.json()
    assert "logs" in data
    assert "total_captured" in data
    assert isinstance(data["logs"], list)

    # 2. Filter by level
    res_info = client.get("/api/logs?limit=50&level=INFO")
    assert res_info.status_code == 200
    for l in res_info.json()["logs"]:
        assert l["level"] == "INFO"

    # 3. Clear logs
    res_clear = client.post("/api/logs/clear")
    assert res_clear.status_code == 200
    assert res_clear.json()["status"] == "SUCCESS"

    # 4. Verify cleared (buffer has 0 logs, excluding /api/logs polling endpoints)
    res_after = client.get("/api/logs?limit=50&level=ALL")
    assert res_after.status_code == 200
    assert len(res_after.json()["logs"]) == 0
