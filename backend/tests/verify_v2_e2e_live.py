"""
End-to-End Live Verification Script for Migration Workbench V2
Tests all frontend assets, Mode 1 and Mode 2 REST endpoints,
dynamic DDL compilation, Ollama/heuristic mapping, execution,
and universal mass conservation against the live running server.
"""

import json
import urllib.request
import urllib.error

BASE_URL = "http://127.0.0.1:8000"

def get(path):
    req = urllib.request.Request(f"{BASE_URL}{path}", headers={"Accept": "application/json"})
    with urllib.request.urlopen(req) as response:
        return response.status, json.loads(response.read().decode())

def get_text(path):
    req = urllib.request.Request(f"{BASE_URL}{path}")
    with urllib.request.urlopen(req) as response:
        return response.status, response.read().decode()

def post(path, data=None):
    payload = json.dumps(data).encode("utf-8") if data is not None else b"{}"
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"}
    )
    with urllib.request.urlopen(req) as response:
        return response.status, json.loads(response.read().decode())

def run_e2e():
    print("🚀 Starting End-to-End Live Verification...")
    
    # 1. Verify Frontend Assets
    print("1. Verifying HTML & JS Assets...")
    status, html = get_text("/")
    assert status == 200, f"Expected 200, got {status}"
    assert "MIGRATION WORKBENCH" in html
    assert "toggle-mode-1" in html
    assert "toggle-mode-2" in html
    assert "v2-tab-schemas" in html
    assert "/js/v2_app.js" in html
    print("   ✓ index.html properly includes Mode 1 & Mode 2 components")

    status, v2_js = get_text("/js/v2_app.js")
    assert status == 200
    assert "switchWorkbenchMode" in v2_js
    assert "compileV2TargetSchema" in v2_js
    print("   ✓ /js/v2_app.js loaded successfully")

    # 2. Verify Mode 1 Endpoints (Regression test)
    print("2. Verifying Mode 1 Benchmark CRM Endpoints...")
    status, src_schema = get("/api/schemas/source")
    assert status == 200 and src_schema["schema"]["fields"]
    print(f"   ✓ Mode 1 Source Schema loaded: {len(src_schema['schema']['fields'])} fields")

    status, v1_plans = get("/api/plans")
    assert status == 200 and len(v1_plans) > 0
    print(f"   ✓ Mode 1 Active Plan: {v1_plans[0]['plan_id']} ({v1_plans[0]['status']})")

    # 3. Verify Mode 2 LLM Config & Connection
    print("3. Verifying Mode 2 LLM Endpoint & Configuration...")
    status, llm_cfg = post("/api/v2/llm/config", {"host": "https://ollama.com/api", "model": "gpt-oss:20b"})
    assert status == 200
    print(f"   ✓ LLM Config updated: {llm_cfg['message']}")

    status, llm_ver = get("/api/v2/llm/verify")
    assert status == 200
    print(f"   ✓ LLM Verification status: {llm_ver['status']} ({llm_ver['model']})")

    # 4. Ingest & Compile Arbitrary Target Schema (Healthcare Encounters)
    print("4. Testing Dynamic Schema Ingestion & DDL Compilation...")
    encounters_schema = {
        "schema_id": "medical_encounters_v1",
        "table_name": "encounters_live",
        "primary_key": "encounter_id",
        "natural_key": "patient_mrn",
        "fields": [
            {"name": "encounter_id", "data_type": "string", "nullable": False, "constraints": {"unique": True}},
            {"name": "patient_mrn", "data_type": "string", "nullable": False, "constraints": {"unique": True}},
            {"name": "patient_name", "data_type": "string", "nullable": False},
            {"name": "admission_date", "data_type": "string", "nullable": False},
            {"name": "fee_amount", "data_type": "float", "nullable": False},
            {"name": "department", "data_type": "string", "nullable": False}
        ]
    }
    status, ddl_res = post("/api/v2/schema/target", encounters_schema)
    assert status == 200
    assert "encounters_live" in ddl_res["table_name"]
    assert "CREATE TABLE IF NOT EXISTS encounters_live" in ddl_res["ddl"]
    print(f"   ✓ Compiled SQLite Table '{ddl_res['table_name']}' successfully.")

    # 5. Propose V2 Plan via Autonomous Agent
    print("5. Proposing Autonomous Migration Plan for Dynamic Table...")
    status, plan = post("/api/v2/plans/propose", {"use_llm": True})
    assert status == 200
    assert plan["status"] == "PROPOSED"
    assert len(plan["field_mappings"]) == len(encounters_schema["fields"])
    print(f"   ✓ Autonomous Plan Proposed: {plan['plan_id']} with {len(plan['field_mappings'])} field mappings")
    print(f"     Status: {plan.get('status')} - Notes: {plan.get('notes', 'Generated plan')}")

    # 6. Execute Deterministic Dry-Run
    print("6. Executing In-Memory Deterministic Dry-Run...")
    status, dry_res = post("/api/v2/plans/dry-run", {"sample_size": 25})
    assert status == 200
    assert dry_res["total_source_records"] > 0
    assert len(dry_res["sample_transformed"]) > 0
    print(f"   ✓ Dry Run simulated {dry_res['total_source_records']} records: {dry_res['valid_count']} valid, {dry_res['quarantined_count']} quarantined")

    # 7. Approve Plan
    print("7. Approving Plan with Lead Governance...")
    status, approved_plan = post("/api/v2/plans/approve", "Lead Data Engineer")
    assert status == 200
    assert approved_plan["status"] == "APPROVED"
    assert approved_plan["approved_by"] == "Lead Data Engineer"
    print(f"   ✓ Plan {approved_plan['plan_id']} successfully approved by {approved_plan['approved_by']}")

    # 8. Execute Target Write into Dynamic SQLite Store
    print("8. Executing Atomic Migration Write into Dynamic SQLite Table...")
    status, exec_res = post("/api/v2/plans/execute")
    assert status == 200
    assert exec_res["status"] == "SUCCESS"
    assert (exec_res["inserted_count"] + exec_res["updated_count"]) > 0
    print(f"   ✓ Migration executed into '{exec_res['target_table_name']}': {exec_res['inserted_count']} inserted, {exec_res['updated_count']} updated in {exec_res['execution_time_ms']}ms")

    # 9. Query Live Dynamic Target Records
    print("9. Querying Live Target Store...")
    status, recs_res = get("/api/v2/target/records?limit=10&offset=0")
    assert status == 200
    assert recs_res["table_name"] == "encounters_live"
    assert recs_res["total_records"] > 0
    assert len(recs_res["records"]) > 0
    assert "patient_mrn" in recs_res["records"][0]
    print(f"   ✓ Retrieved {len(recs_res['records'])} live records from dynamic table '{recs_res['table_name']}'")

    # 10. Universal Mass Conservation Reconciliation
    print("10. Checking Universal Mass Conservation Invariant...")
    status, recon = get("/api/v2/reconciliation")
    assert status == 200
    assert recon["is_zero_drop_verified"] is True
    assert recon["unaccounted_delta"] == 0
    print(f"   ✓ Mass Conservation Verified: Source({recon['source_records']}) = Inserted({recon['inserted_count']}) + Updated({recon['updated_count']}) + Quarantined({recon['quarantined_count']}) + Delta({recon['unaccounted_delta']})")

    # 11. Test Snapshot Rollback
    print("11. Testing Table Snapshot Rollback...")
    status, roll_res = post("/api/v2/rollback", {})
    assert status == 200
    assert roll_res["status"] == "ROLLED_BACK"
    print(f"   ✓ Rollback verified: {roll_res['removed_records']} records removed")

    print("\n🎉 ALL LIVE END-TO-END VERIFICATIONS PASSED SUCCESSFULLY WITH ZERO REGRESSIONS! 🎉")

if __name__ == "__main__":
    run_e2e()
