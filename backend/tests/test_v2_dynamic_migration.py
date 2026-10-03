import pytest
from app.engine.dynamic_store import DynamicDatabaseStore
from app.main import app, ollama_agent
from app.models import FieldMapping, MigrationPlan
from app.models.schemas import current_utc_iso
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    return TestClient(app)


def mock_ai_plan(*args, **kwargs):
    now_iso = current_utc_iso()
    return MigrationPlan(
        plan_id="plan_v1",
        version=1,
        title="Ollama Autonomous Plan v1",
        description="Generated using Ollama AI inference (llama3.2)",
        source_schema_id="source_custom",
        target_schema_id="medical_encounters_v1",
        field_mappings=[
            FieldMapping(
                target_field="encounter_id",
                source_fields=["encounter_id"],
                transformation="DIRECT_COPY",
                parameters={},
                risk_level="LOW",
                risk_rationale="Direct map of clinical encounter ID",
                notes="Ollama AI Mapped",
            ),
            FieldMapping(
                target_field="patient_mrn",
                source_fields=["patient_mrn"],
                transformation="DIRECT_COPY",
                parameters={},
                risk_level="LOW",
                risk_rationale="Mapped patient natural key MRN",
                notes="Ollama AI Mapped",
            ),
            FieldMapping(
                target_field="patient_name",
                source_fields=["pt_full_name"],
                transformation="COALESCE_VAL",
                parameters={"default": "Unknown Patient"},
                risk_level="LOW",
                risk_rationale="Mapped patient name with fallback default",
                notes="Ollama AI Mapped",
            ),
            FieldMapping(
                target_field="fee_amount",
                source_fields=["charge_str"],
                transformation="CLEAN_CURRENCY_TO_FLOAT",
                parameters={},
                risk_level="LOW",
                risk_rationale="Transformed currency charge to numeric fee_amount",
                notes="Ollama AI Mapped",
            ),
        ],
        clarifications=[],
        status="PROPOSED",
        approved_by=None,
        approved_at=None,
        created_at=now_iso,
        updated_at=now_iso,
    )


def test_dynamic_database_store_direct():
    store = DynamicDatabaseStore()
    with store.get_connection() as conn:
        conn.execute("DROP TABLE IF EXISTS test_inventory;")
        conn.commit()

    custom_schema = {
        "schema_id": "test_inventory_v1",
        "table_name": "test_inventory",
        "primary_key": "sku_uuid",
        "natural_key": "sku_code",
        "fields": [
            {"name": "sku_uuid", "data_type": "string", "nullable": False, "constraints": {"unique": True}},
            {"name": "sku_code", "data_type": "string", "nullable": False, "constraints": {"unique": True}},
            {"name": "item_name", "data_type": "string", "nullable": False},
            {"name": "quantity", "data_type": "int", "nullable": False},
            {"name": "unit_price", "data_type": "float", "nullable": False},
        ],
    }

    # 1. Compile and create table
    ddl = store.compile_and_create_table(custom_schema)
    assert "test_inventory" in ddl
    assert "sku_uuid" in ddl
    assert "quantity" in ddl
    assert "unit_price" in ddl

    # 2. Upsert batch
    rows = [
        {"sku_uuid": "u1", "sku_code": "SKU-001", "item_name": "Widget A", "quantity": 100, "unit_price": 9.99},
        {"sku_uuid": "u2", "sku_code": "SKU-002", "item_name": "Widget B", "quantity": 50, "unit_price": 19.50},
    ]
    inserted, updated, skipped = store.execute_upsert_batch("test_inventory", "sku_code", rows, run_id="run_1")
    assert inserted == 2
    assert updated == 0
    assert skipped == 0

    total, recs = store.query_dynamic_records("test_inventory")
    assert total == 2
    assert len(recs) == 2

    # 3. Idempotent Retry - Should update existing, zero new inserts, zero double counting
    inserted2, updated2, skipped2 = store.execute_upsert_batch("test_inventory", "sku_code", rows, run_id="run_2")
    assert inserted2 == 0
    assert updated2 == 2
    assert skipped2 == 0
    assert inserted2 + updated2 + skipped2 == len(rows)

    # 4. Snapshot & Rollback (True Rollback Test: insert 3rd row, then roll back to 2 rows)
    snap_before_add = store.create_snapshot("test_inventory", run_id="snap_2_rows")
    row_3 = [{"sku_uuid": "u3", "sku_code": "SKU-003", "item_name": "Widget C", "quantity": 10, "unit_price": 5.0}]
    ins3, _, _ = store.execute_upsert_batch("test_inventory", "sku_code", row_3, run_id="run_3")
    assert ins3 == 1
    total_with_3, _ = store.query_dynamic_records("test_inventory")
    assert total_with_3 == 3

    # Roll back to snapshot: must remove 1 record and leave exactly 2
    success, removed, remaining = store.rollback_snapshot(snap_before_add)
    assert success is True
    assert removed == 1
    assert remaining == 2
    total_after_rollback, _ = store.query_dynamic_records("test_inventory")
    assert total_after_rollback == 2


def test_v2_api_lifecycle(client, monkeypatch):
    # Patch Ollama inference to return strict AI plan
    monkeypatch.setattr(ollama_agent, "_call_ollama_llm", mock_ai_plan)

    # 1. Test LLM Config endpoint
    res_cfg = client.post("/api/v2/llm/config", json={"host": "https://ollama.com/api", "model": "llama3.2"})
    assert res_cfg.status_code == 200
    assert "LLM configured" in res_cfg.json()["message"]

    # 1.5 Load healthcare sample space (source encounters CSV + target schema)
    res_load = client.post("/api/v2/samples/load/healthcare")
    assert res_load.status_code == 200

    # 2. Submit dynamic target schema
    store = DynamicDatabaseStore()
    with store.get_connection() as conn:
        conn.execute("DROP TABLE IF EXISTS encounters;")
        conn.commit()

    custom_target = {
        "schema_id": "medical_encounters_v1",
        "table_name": "encounters",
        "primary_key": "encounter_id",
        "natural_key": "patient_mrn",
        "fields": [
            {"name": "encounter_id", "data_type": "string", "nullable": False, "constraints": {"unique": True}},
            {"name": "patient_mrn", "data_type": "string", "nullable": False, "constraints": {"unique": True}},
            {"name": "patient_name", "data_type": "string", "nullable": False},
            {"name": "fee_amount", "data_type": "float", "nullable": False},
        ],
    }
    res_schema = client.post("/api/v2/schema/target", json=custom_target)
    assert res_schema.status_code == 200
    assert "Compiled and created target table 'encounters'" in res_schema.json()["message"]

    # 3. Get proposed plan
    res_plan = client.get("/api/v2/plans/current")
    assert res_plan.status_code == 200
    plan = res_plan.json()
    assert plan["status"] == "PROPOSED"
    assert len(plan["field_mappings"]) == 4

    # 4. Dry run
    res_dry = client.post("/api/v2/plans/dry-run")
    assert res_dry.status_code == 200
    dry_data = res_dry.json()
    assert dry_data["total_source_records"] > 0

    # 5. Approve plan
    res_appr = client.post("/api/v2/plans/approve", json="Staff Engineer")
    assert res_appr.status_code == 200
    assert res_appr.json()["status"] == "APPROVED"

    # 6. Execute migration into dynamic table
    res_exec = client.post("/api/v2/plans/execute")
    assert res_exec.status_code == 200
    exec_data = res_exec.json()
    assert exec_data["status"] == "SUCCESS"
    assert (exec_data["inserted_count"] + exec_data["updated_count"]) > 0

    # 7. Query dynamic records
    res_recs = client.get("/api/v2/target/records?limit=10")
    assert res_recs.status_code == 200
    data_recs = res_recs.json()
    assert data_recs["table_name"] == "encounters"
    assert data_recs["total_records"] > 0
    assert len(data_recs["records"]) > 0

    # 8. Dynamic reconciliation
    res_recon = client.get("/api/v2/reconciliation")
    assert res_recon.status_code == 200
    recon = res_recon.json()
    assert recon["table_name"] == "encounters"
    assert recon["target_records"] > 0


def test_v2_strict_ai_error_handling(client):
    """Verifies that when AI service is unavailable, an explicit error is returned with ZERO offline heuristic fallback."""
    orig_host = ollama_agent.host
    orig_model = ollama_agent.model
    orig_key = ollama_agent.api_key
    try:
        # Point to an unreachable host with no mock
        ollama_agent.set_config(host="http://127.0.0.1:54321", model="unreachable-ai")

        # 1. Direct call to generate_plan must raise RuntimeError (not silently return heuristic plan)
        with pytest.raises(RuntimeError) as exc_info:
            ollama_agent.generate_plan(plan_version=1)
        assert "Ollama AI" in str(exc_info.value)
        assert "Communication Error" in str(exc_info.value) or "Error" in str(exc_info.value)

        # 2. REST API /api/v2/plans/propose must return HTTP 502 with AI error details
        res = client.post("/api/v2/plans/propose")
        assert res.status_code == 502
        assert "AI Agent Error" in res.json()["detail"]
    finally:
        ollama_agent.set_config(host=orig_host, model=orig_model, api_key=orig_key)


def test_v2_dataset_upload_complete_isolation_from_mode_1(client, monkeypatch):
    """Verifies that uploading a dataset in Mode 2 NEVER creates plans or alters state in Mode 1 (Benchmark CRM)."""
    monkeypatch.setattr(ollama_agent, "_call_ollama_llm", mock_ai_plan)

    # 1. Snapshot Mode 1 state
    mode1_plans_before = client.get("/api/plans").json()
    mode1_plan_count_before = len(mode1_plans_before)
    mode1_schema_before = client.get("/api/schemas/source").json()

    # 2. Upload custom dataset to Mode 2 intake endpoint
    csv_payload = "sku,product_title,price\nSKU-100,Super Gadget,49.99\nSKU-200,Mega Tool,29.95\n"
    res_upload = client.post(
        "/api/v2/upload/source", files={"file": ("warehouse_inventory.csv", csv_payload.encode("utf-8"), "text/csv")}
    )
    assert res_upload.status_code == 200
    data = res_upload.json()
    assert data["total_records"] == 2
    assert data["filename"] == "warehouse_inventory.csv"

    # 3. Verify Mode 2 source dataset is updated
    res_v2_src = client.get("/api/v2/source")
    assert res_v2_src.status_code == 200
    assert res_v2_src.json()["total_records"] == 2

    # 4. CRITICAL: Verify Mode 1 plans are UNTOUCHED (zero version 3 created in Mode 1)
    mode1_plans_after = client.get("/api/plans").json()
    assert len(mode1_plans_after) == mode1_plan_count_before, "Mode 1 plans list was modified by Mode 2 upload!"
    for p in mode1_plans_after:
        assert "warehouse_inventory" not in p.get("title", "").lower()
        assert "warehouse_inventory" not in (p.get("notes") or "").lower()

    # 5. Verify Mode 1 source schema is UNTOUCHED
    mode1_schema_after = client.get("/api/schemas/source").json()
    assert mode1_schema_after["schema"]["schema_id"] == mode1_schema_before["schema"]["schema_id"]
