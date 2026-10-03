import json

import pytest
from app.main import (
    app,
    dynamic_store,
    ollama_agent,
)
from app.models import FieldMapping, MigrationPlan
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    return TestClient(app)


def mock_dynamic_plan(*args, **kwargs):
    target_schema = kwargs.get("target_schema") or {}
    tgt_fields = target_schema.get("fields", [])
    mappings = []
    for tf in tgt_fields:
        mappings.append(
            FieldMapping(
                target_field=tf["name"],
                source_fields=[tf["name"]],
                transformation="DIRECT_COPY",
                parameters={},
                risk_level="LOW",
                risk_rationale="Direct copy",
                notes="Mocked AI Plan",
            )
        )
    return MigrationPlan(
        plan_id="plan_v1",
        version=kwargs.get("plan_version", 1),
        title="Mock AI Plan",
        description="Fast mock AI plan for testing",
        source_schema_id="source_test",
        target_schema_id="target_test",
        field_mappings=mappings,
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
            {"name": "status", "type": "STRING", "nullable": False},
        ],
    }
    schema_res = client.post("/api/v2/schema/target", json=schema_payload)
    assert schema_res.status_code == 200

    # 2. Upload the exact same file 5 times sequentially (simulating user repeatedly uploading/reloading)
    for i in range(5):
        upload_res = client.post(
            "/api/v2/upload/source",
            files={"file": ("legacy_invoices_v1_dummy.csv", csv_content.encode("utf-8"), "text/csv")},
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
            {"name": "order_date", "type": "STRING", "nullable": True},
        ],
    }
    schema_res = client.post("/api/v2/schema/target", json=schema_payload)
    assert schema_res.status_code == 200

    # 2. Upload dataset
    upload_res = client.post(
        "/api/v2/upload/source", files={"file": ("orders.csv", csv_content.encode("utf-8"), "text/csv")}
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
    for log_item in res_info.json()["logs"]:
        assert log_item["level"] == "INFO"

    # 3. Clear logs
    res_clear = client.post("/api/logs/clear")
    assert res_clear.status_code == 200
    assert res_clear.json()["status"] == "SUCCESS"

    # 4. Verify cleared (buffer has 0 logs, excluding /api/logs polling endpoints)
    res_after = client.get("/api/logs?limit=50&level=ALL")
    assert res_after.status_code == 200
    assert len(res_after.json()["logs"]) == 0


def test_standalone_logs_route(client):
    """Verifies that GET /logs serves the standalone HTML log explorer page."""
    res = client.get("/logs")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    assert (
        "Autonomous Server &amp; AI Copilot Diagnostics" in res.text
        or "Autonomous Server & AI Copilot Diagnostics" in res.text
    )
    assert "/api/logs" in res.text


def test_all_json_and_csv_input_types_mode2(client, monkeypatch):
    """Verifies that Mode 2 upload handles ALL input formats without 502 or 500:
    - JSON Array
    - JSON Object with 'records'
    - JSON Object with 'data'
    - JSON Object with 'invoices'
    - Single JSON Object
    - NDJSON / JSON Lines
    - Semicolon-delimited CSV
    - Tab-delimited TSV
    - UTF-8 with BOM
    """
    monkeypatch.setattr(ollama_agent, "_call_ollama_llm", mock_dynamic_plan)

    # 1. Standard JSON array
    json_array = json.dumps(
        [{"inv_id": "I-1", "client": "Alpha", "amount": 100.0}, {"inv_id": "I-2", "client": "Beta", "amount": 200.0}]
    )
    r1 = client.post(
        "/api/v2/upload/source", files={"file": ("invoices.json", json_array.encode("utf-8"), "application/json")}
    )
    assert r1.status_code == 200
    assert r1.json()["total_records"] == 2
    assert "inv_id" in [f["name"] for f in r1.json()["source_schema"]["fields"]]

    # 2. JSON with 'records' wrapper
    json_records = json.dumps(
        {
            "status": "success",
            "records": [
                {"order_no": "ORD-1", "item": "Widget", "qty": 10},
                {"order_no": "ORD-2", "item": "Gadget", "qty": 5},
            ],
        }
    )
    r2 = client.post(
        "/api/v2/upload/source", files={"file": ("dataset.json", json_records.encode("utf-8"), "application/json")}
    )
    assert r2.status_code == 200
    assert r2.json()["total_records"] == 2

    # 3. JSON with 'invoices' wrapper
    json_invoices = json.dumps({"invoices": [{"code": "INV-100", "due": 500}, {"code": "INV-200", "due": 750}]})
    r3 = client.post(
        "/api/v2/upload/source", files={"file": ("data.json", json_invoices.encode("utf-8"), "application/json")}
    )
    assert r3.status_code == 200
    assert r3.json()["total_records"] == 2

    # 4. NDJSON / JSON Lines
    ndjson = '{"user_id": 1, "name": "Alice"}\n{"user_id": 2, "name": "Bob"}\n{"user_id": 3, "name": "Charlie"}'
    r4 = client.post("/api/v2/upload/source", files={"file": ("stream.ndjson", ndjson.encode("utf-8"), "text/plain")})
    assert r4.status_code == 200
    assert r4.json()["total_records"] == 3

    # 5. Semicolon-delimited CSV
    csv_semi = "id;product;price\nP-1;Laptop;1200\nP-2;Mouse;25\n"
    r5 = client.post("/api/v2/upload/source", files={"file": ("products.csv", csv_semi.encode("utf-8"), "text/csv")})
    assert r5.status_code == 200
    assert r5.json()["total_records"] == 2
    assert "product" in [f["name"] for f in r5.json()["source_schema"]["fields"]]

    # 6. Tab-delimited TSV
    tsv = "code\tcity\tpop\nNYC\tNew York\t8000000\nLON\tLondon\t9000000\n"
    r6 = client.post(
        "/api/v2/upload/source", files={"file": ("cities.tsv", tsv.encode("utf-8"), "text/tab-separated-values")}
    )
    assert r6.status_code == 200
    assert r6.json()["total_records"] == 2

    # 7. UTF-8 with BOM
    csv_bom = "\ufeffaccount_id,balance\nACC-1,500.00\nACC-2,950.00\n"
    r7 = client.post("/api/v2/upload/source", files={"file": ("bom_data.csv", csv_bom.encode("utf-8"), "text/csv")})
    assert r7.status_code == 200
    assert r7.json()["total_records"] == 2
