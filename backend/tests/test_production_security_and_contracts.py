"""
Production Security, SQL Injection Prevention, Plan Integrity, and Contract Validation Test Suite.
Validates:
1. SQL Identifier Whitelisting & Injection Prevention (table/column names).
2. Unmapped Required (NOT NULL) Target Field quarantine enforcement.
3. Target Type Validation & Coercion (int, float, bool, date calendar checks).
4. Plan-Bound Cryptographic Approval Fingerprinting & Anti-Tamper Execution Blocks.
5. Mode 2 SQLite Quarantine & Audit Ledger Durability.
6. Explicit Persistence Error Propagation (no swallowed SQLite exceptions).
"""

import os
import json
import pytest
from fastapi.testclient import TestClient

from app.main import app, plan_manager, dry_runner, dynamic_store, executor, get_v2_active_plan, set_v2_active_plan, ollama_agent
from app.models.schemas import MigrationPlan, FieldMapping, current_utc_iso
from app.engine.dynamic_store import validate_sql_identifier, DynamicDatabaseStore
from app.engine.history import PlanManager, compute_plan_fingerprint
from app.engine.target_store import TargetDatabaseStore

def mock_ai_plan(prompt: str) -> str:
    return json.dumps({
        "thought_process": "Deterministic mock plan for security testing",
        "field_mappings": [
            {"target_field": "encounter_id", "source_fields": ["id"], "transformation": "DIRECT_COPY", "parameters": {}},
            {"target_field": "patient_mrn", "source_fields": ["mrn"], "transformation": "DIRECT_COPY", "parameters": {}},
            {"target_field": "patient_name", "source_fields": ["name"], "transformation": "TRIM_CLEAN", "parameters": {}},
            {"target_field": "fee_amount", "source_fields": ["cost"], "transformation": "CURRENCY_TO_FLOAT", "parameters": {}}
        ]
    })

@pytest.fixture(autouse=True)
def speed_up_ai(monkeypatch):
    monkeypatch.setattr(ollama_agent, "_call_ollama_llm", mock_ai_plan)

@pytest.fixture
def client():
    return TestClient(app)

# ============================================================================
# 1. SQL Injection & Identifier Defense Tests
# ============================================================================

def test_sql_identifier_validation_blocks_injection():
    """Ensures SQL injection attempts in table and column names raise ValueError."""
    malicious_identifiers = [
        "customers; DROP TABLE users; --",
        "orders' OR '1'='1",
        "tbl`name",
        "table name with spaces",
        "123_starts_with_number",
        "col$name",
        "drop--table",
        "a" * 65,  # Exceeds max 64 chars
        "",
    ]
    for bad in malicious_identifiers:
        with pytest.raises(ValueError) as exc:
            validate_sql_identifier(bad, "table_name")
        assert "Invalid SQL" in str(exc.value)

def test_sql_identifier_validation_allows_safe_names():
    """Ensures valid SQL identifiers pass without error."""
    valid_identifiers = [
        "customers",
        "_internal_id",
        "PatientEncounter_v2",
        "order_item_quantity_2024",
    ]
    for good in valid_identifiers:
        assert validate_sql_identifier(good, "column") == good

def test_dynamic_store_table_creation_rejects_sql_injection():
    """Ensures compiling a target schema with an injected table name is blocked safely."""
    store = DynamicDatabaseStore()
    evil_schema = {
        "schema_id": "evil_schema",
        "table_name": "target; DROP TABLE customers; --",
        "primary_key": "id",
        "fields": [{"name": "id", "data_type": "string"}]
    }
    with pytest.raises(ValueError):
        store.compile_and_create_table(evil_schema)

# ============================================================================
# 2. Unmapped Required Target Fields Enforcement
# ============================================================================

def test_unmapped_required_target_field_quarantines_record():
    """Ensures that if a NOT NULL target field with no default is unmapped in the plan,
    records are quarantined with REQUIRED_FIELD_UNMAPPED rather than dropped or failing silently."""
    target_schema = {
        "schema_id": "strict_orders_v1",
        "table_name": "orders_strict",
        "primary_key": "order_id",
        "fields": [
            {"name": "order_id", "data_type": "string", "nullable": False},
            {"name": "customer_id", "data_type": "string", "nullable": False},
            {"name": "order_total", "data_type": "float", "nullable": False},
            # This mandatory field is omitted from field_mappings:
            {"name": "mandatory_tax_code", "data_type": "string", "nullable": False}
        ]
    }

    # Plan maps order_id, customer_id, order_total, but NOT mandatory_tax_code
    plan = MigrationPlan(
        plan_id="plan_missing_required",
        version=1,
        source_schema_id="raw_orders",
        target_schema_id="strict_orders_v1",
        field_mappings=[
            FieldMapping(target_field="order_id", source_fields=["raw_id"], transformation="DIRECT_COPY"),
            FieldMapping(target_field="customer_id", source_fields=["cust_code"], transformation="DIRECT_COPY"),
            FieldMapping(target_field="order_total", source_fields=["amount"], transformation="DIRECT_COPY"),
        ]
    )

    records = [
        {"raw_id": "ORD-1", "cust_code": "CUST-99", "amount": "150.00"},
        {"raw_id": "ORD-2", "cust_code": "CUST-100", "amount": "250.00"}
    ]

    summary, valids, quars = dry_runner.execute_dry_run(plan, records=records, target_schema=target_schema)

    assert summary.accepted_count == 0
    assert summary.rejected_count == 2
    assert len(quars) == 2
    assert "mandatory_tax_code" in summary.field_error_breakdown

    for q in quars:
        error_rules = [e.rule for e in q.errors]
        assert "REQUIRED_FIELD_UNMAPPED" in error_rules
        unmapped_err = next(e for e in q.errors if e.rule == "REQUIRED_FIELD_UNMAPPED")
        assert unmapped_err.field == "mandatory_tax_code"

# ============================================================================
# 3. Target Data-Type Validation & Coercion
# ============================================================================

def test_target_data_type_coercion_and_calendar_validation():
    """Tests type conversion for int, float, bool, date, and calendar validity (e.g. Feb 31 invalid)."""
    target_schema = {
        "schema_id": "datatypes_schema",
        "table_name": "data_types_test",
        "primary_key": "id",
        "fields": [
            {"name": "id", "data_type": "string", "nullable": False},
            {"name": "item_count", "data_type": "int", "nullable": False},
            {"name": "unit_price", "data_type": "float", "nullable": False},
            {"name": "is_active", "data_type": "bool", "nullable": False},
            {"name": "created_date", "data_type": "date", "nullable": False},
        ]
    }

    plan = MigrationPlan(
        plan_id="plan_types",
        version=1,
        source_schema_id="raw_source",
        target_schema_id="datatypes_schema",
        field_mappings=[
            FieldMapping(target_field="id", source_fields=["src_id"], transformation="DIRECT_COPY"),
            FieldMapping(target_field="item_count", source_fields=["count_str"], transformation="DIRECT_COPY"),
            FieldMapping(target_field="unit_price", source_fields=["price_str"], transformation="DIRECT_COPY"),
            FieldMapping(target_field="is_active", source_fields=["active_str"], transformation="DIRECT_COPY"),
            FieldMapping(target_field="created_date", source_fields=["date_str"], transformation="DIRECT_COPY"),
        ]
    )

    records = [
        # 1. Perfectly coercible record
        {"src_id": "REC-1", "count_str": "42", "price_str": "19.99", "active_str": "true", "date_str": "2024-05-15"},
        # 2. String in integer field
        {"src_id": "REC-2", "count_str": "forty-two", "price_str": "19.99", "active_str": "1", "date_str": "2024-05-15"},
        # 3. Nonexistent calendar date (Feb 31)
        {"src_id": "REC-3", "count_str": "10", "price_str": "5.00", "active_str": "false", "date_str": "2024-02-31"},
    ]

    summary, valids, quars = dry_runner.execute_dry_run(plan, records=records, target_schema=target_schema)

    assert summary.accepted_count == 1
    assert summary.rejected_count == 2
    assert valids[0]["item_count"] == 42
    assert valids[0]["unit_price"] == 19.99
    assert valids[0]["is_active"] is True
    assert valids[0]["created_date"] == "2024-05-15"

    # Verify quarantine reasons
    quar_ids = {q.source_payload["src_id"]: [e.rule for e in q.errors] for q in quars}
    assert "TYPE_INCOMPATIBILITY" in quar_ids["REC-2"]
    assert "TYPE_INCOMPATIBILITY" in quar_ids["REC-3"]

# ============================================================================
# 4. Plan-Bound Cryptographic Fingerprint & Anti-Tampering Security
# ============================================================================

def test_plan_bound_approval_fingerprint_generation():
    """Ensures approving a plan generates a deterministic SHA-256 fingerprint."""
    plan = MigrationPlan(
        plan_id="plan_fp_test",
        version=1,
        source_schema_id="src",
        target_schema_id="tgt",
        field_mappings=[
            FieldMapping(target_field="target_a", source_fields=["src_a"], transformation="DIRECT_COPY"),
            FieldMapping(target_field="target_b", source_fields=["src_b"], transformation="UPPERCASE"),
        ]
    )
    plan_manager.save_plan(plan)
    approved = plan_manager.approve_plan(version=1, approved_by="SecOps Lead")

    assert approved.status == "APPROVED"
    assert approved.approval_fingerprint is not None
    assert len(approved.approval_fingerprint) == 64  # SHA-256 hex string

    expected_fp = compute_plan_fingerprint(approved)
    assert approved.approval_fingerprint == expected_fp

def test_execution_engine_blocks_tampered_plan():
    """Ensures that if plan mappings are mutated post-approval, ExecutionEngine aborts execution."""
    plan = MigrationPlan(
        plan_id="plan_tamper",
        version=1,
        source_schema_id="legacy_customers",
        target_schema_id="modern_customers",
        field_mappings=[
            FieldMapping(target_field="first_name", source_fields=["first_name"], transformation="DIRECT_COPY"),
        ]
    )
    plan_manager.save_plan(plan)
    approved_plan = plan_manager.approve_plan(version=1, approved_by="Data Admin")

    # Adversary stealthily alters mapping without re-approval
    approved_plan.field_mappings[0].transformation = "REDACT_ALL"

    with pytest.raises(ValueError) as exc:
        executor.execute_migration(approved_plan)
    assert "Security violation" in str(exc.value)
    assert "approval fingerprint" in str(exc.value).lower()

def test_mode2_api_blocks_execution_on_tampered_plan(client):
    """Ensures Mode 2 /api/v2/plans/execute blocks execution with HTTP 400 when fingerprint mismatches."""
    # Set up Mode 2 target and synthesize plan
    client.post("/api/v2/samples/load/healthcare")
    res_plan = client.get("/api/v2/plans/current")
    assert res_plan.status_code == 200
    plan = get_v2_active_plan()
    assert plan is not None

    # Approve Mode 2 plan
    res_appr = client.post("/api/v2/plans/approve", json={"approved_by": "Architect"})
    assert res_appr.status_code == 200

    # Tamper with the active plan mappings directly
    tampered_plan = get_v2_active_plan()
    tampered_plan.field_mappings.append(
        FieldMapping(target_field="unauthorized_field", source_fields=["dummy"], transformation="DIRECT_COPY")
    )
    set_v2_active_plan(tampered_plan)

    # Attempt execution -> MUST be rejected with HTTP 400
    res_exec = client.post("/api/v2/plans/execute")
    assert res_exec.status_code == 400
    assert "Security violation" in res_exec.json()["detail"]

# ============================================================================
# 5. Mode 2 Durable Quarantine & Audit Ledger
# ============================================================================

def test_mode2_quarantine_and_audit_ledger_durability(client):
    """Verifies that Mode 2 quarantine records and audit events are durably persisted in SQLite."""
    # 1. Load healthcare dataset & ensure active plan
    client.post("/api/v2/samples/load/healthcare")
    client.get("/api/v2/plans/current")

    # 2. Run dry-run to produce quarantine records
    res_dry = client.post("/api/v2/plans/dry-run")
    assert res_dry.status_code == 200
    dry_data = res_dry.json()

    # 3. Query durable quarantine ledger endpoint
    res_quar = client.get("/api/v2/quarantine/records")
    assert res_quar.status_code == 200
    quar_ledger = res_quar.json()
    assert "total" in quar_ledger
    assert "records" in quar_ledger

    # 4. Approve and execute
    client.post("/api/v2/plans/approve")
    res_exec = client.post("/api/v2/plans/execute")
    assert res_exec.status_code == 200

    # 5. Query durable audit ledger endpoint
    res_audit = client.get("/api/v2/audit/events")
    assert res_audit.status_code == 200
    audit_events = res_audit.json()
    assert len(audit_events) > 0
    event_types = [ev["event_type"] for ev in audit_events]
    assert "PLAN_APPROVED" in event_types
    assert "MIGRATION_EXECUTED" in event_types

# ============================================================================
# 6. Explicit Persistence Error Propagation
# ============================================================================

def test_history_raises_explicit_runtime_error_on_persistence_failure(tmp_path):
    """Verifies that plan_manager does NOT swallow SQLite persistence errors on save or load."""
    db_file = tmp_path / "corrupt_store.db"
    store = TargetDatabaseStore(db_path=str(db_file))
    store.init_database()

    # Instantiate manager while table exists
    mgr = PlanManager(target_store=store)

    # Drop the table to cause SQLite exception during save
    with store.get_connection() as conn:
        conn.execute("DROP TABLE migration_plans;")
        conn.commit()

    plan = MigrationPlan(
        plan_id="plan_fail",
        version=1,
        source_schema_id="s",
        target_schema_id="t",
        field_mappings=[]
    )

    with pytest.raises(RuntimeError) as exc_save:
        mgr.save_plan(plan)
    assert "Database persistence failure while saving plan" in str(exc_save.value)

    # Test that loading plans without table also raises explicit RuntimeError
    with pytest.raises(RuntimeError) as exc_load:
        PlanManager(target_store=store)
    assert "Database persistence failure while loading plans" in str(exc_load.value)
