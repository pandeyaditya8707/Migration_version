"""
Audit Verification Test Suite addressing all reviewer findings:
1. No Fabricated Data: missing NOT NULL fields reject or quarantine, never silently fabricate 0 or empty string.
2. Strict Type Enforcement: strings into numeric/real columns are rejected, not silently stored as text.
3. Non-colliding Snapshot IDs: rapid snapshots produce unique IDs without IntegrityError.
4. SQL Reserved Table Name Protection: internal/reserved table names cannot be modified or dropped.
5. Exact 1-to-1 Mass Conservation: no double-counting on re-run, source = inserted + updated + skipped + quarantined.
"""

import pytest
from app.engine.dynamic_store import DynamicDatabaseStore, validate_sql_identifier


def test_no_fabricated_data_on_missing_not_null():
    """Verify that missing NOT NULL columns are NOT silently fabricated into 0, 0.0 or empty strings."""
    store = DynamicDatabaseStore()
    schema = {
        "schema_id": "audit_strict_schema_v1",
        "table_name": "strict_audit_records",
        "primary_key": "record_id",
        "natural_key": "record_code",
        "fields": [
            {"name": "record_id", "data_type": "string", "nullable": False},
            {"name": "record_code", "data_type": "string", "nullable": False},
            {"name": "amount", "data_type": "float", "nullable": False},
        ],
    }
    store.compile_and_create_table(schema)

    # A row without the required NOT NULL 'amount' column
    rows_missing_amount = [{"record_id": "r1", "record_code": "CODE-001"}]
    with pytest.raises(ValueError, match="NOT NULL constraint failed"):
        store.execute_upsert_batch("strict_audit_records", "record_code", rows_missing_amount, run_id="run_strict")


def test_type_enforcement_rejects_strings_in_numeric_columns():
    """Verify that 'not-a-number' in REAL/FLOAT column raises a type violation instead of inserting as text."""
    store = DynamicDatabaseStore()
    schema = {
        "schema_id": "audit_type_check_v1",
        "table_name": "strict_type_records",
        "primary_key": "row_id",
        "natural_key": "row_code",
        "fields": [
            {"name": "row_id", "data_type": "string", "nullable": False},
            {"name": "row_code", "data_type": "string", "nullable": False},
            {"name": "amount", "data_type": "float", "nullable": False},
        ],
    }
    store.compile_and_create_table(schema)

    rows_bad_type = [{"row_id": "r1", "row_code": "CODE-001", "amount": "not-a-number"}]
    with pytest.raises(ValueError, match="Data type constraint violation"):
        store.execute_upsert_batch("strict_type_records", "row_code", rows_bad_type, run_id="run_bad_type")


def test_snapshot_id_no_collision_rapid_calls():
    """Verify that two snapshots created in immediate succession have unique IDs and don't collide."""
    store = DynamicDatabaseStore()
    schema = {
        "schema_id": "audit_snap_test_v1",
        "table_name": "audit_snap_table",
        "primary_key": "id",
        "natural_key": "id",
        "fields": [
            {"name": "id", "data_type": "string", "nullable": False},
            {"name": "name", "data_type": "string", "nullable": True},
        ],
    }
    store.compile_and_create_table(schema)
    store.execute_upsert_batch("audit_snap_table", "id", [{"id": "s1", "name": "Test"}], run_id="run_snap")

    snap1 = store.create_snapshot("audit_snap_table", run_id="rapid_run")
    snap2 = store.create_snapshot("audit_snap_table", run_id="rapid_run")

    assert snap1 != snap2
    assert "rapid_run" in snap1
    assert "rapid_run" in snap2


def test_reserved_table_names_blocked():
    """Verify that internal table names cannot be modified or compiled."""
    store = DynamicDatabaseStore()
    for reserved in ["dynamic_snapshots", "dynamic_schemas", "sqlite_master", "quarantine_ledger", "customers"]:
        with pytest.raises(ValueError, match="reserved internal system/ledger table"):
            validate_sql_identifier(reserved, label="table name")

        with pytest.raises(ValueError, match="reserved internal system/ledger table"):
            store.compile_and_create_table({"table_name": reserved, "fields": [{"name": "id", "data_type": "string"}]})


def test_exact_reconciliation_mass_conservation_no_double_counting():
    """Verify that re-running rows never double counts: inserted + updated + skipped == total."""
    store = DynamicDatabaseStore()
    schema = {
        "schema_id": "audit_recon_test_v1",
        "table_name": "audit_recon_table",
        "primary_key": "sku_id",
        "natural_key": "sku_code",
        "fields": [
            {"name": "sku_id", "data_type": "string", "nullable": False},
            {"name": "sku_code", "data_type": "string", "nullable": False},
            {"name": "price", "data_type": "float", "nullable": False},
        ],
    }
    store.compile_and_create_table(schema)

    rows = [
        {"sku_id": "s1", "sku_code": "SKU-001", "price": 10.0},
        {"sku_id": "s2", "sku_code": "SKU-002", "price": 20.0},
        {"sku_id": "s3", "sku_code": "SKU-003", "price": 30.0},
    ]

    # Run 1: 3 inserts
    ins1, upd1, skp1 = store.execute_upsert_batch("audit_recon_table", "sku_code", rows, run_id="run_1")
    assert ins1 == 3
    assert upd1 == 0
    assert skp1 == 0
    assert ins1 + upd1 + skp1 == len(rows)

    # Run 2: Re-run exact same 3 rows
    ins2, upd2, skp2 = store.execute_upsert_batch("audit_recon_table", "sku_code", rows, run_id="run_2")
    # All 3 rows land in updated, exactly 0 are double counted in skipped
    assert ins2 == 0
    assert ins2 + upd2 + skp2 == len(rows)
