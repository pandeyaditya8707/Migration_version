import urllib.request
import json

def req(url, data=None):
    if data is not None:
        data_bytes = json.dumps(data).encode("utf-8")
        r = urllib.request.Request(url, data=data_bytes, headers={"Content-Type": "application/json"})
    else:
        r = urllib.request.Request(url)
    with urllib.request.urlopen(r) as resp:
        return json.loads(resp.read().decode("utf-8"))

def main():
    print("0. Resetting Target Store to clean state...")
    req("http://127.0.0.1:8000/api/reset", {})

    print("1. Testing Source Schema & Profiling...")
    src = req("http://127.0.0.1:8000/api/schemas/source")
    print(f"   Source Fields: {len(src['schema']['fields'])}, Records: {src['total_sample_records']}")

    print("2. Testing Target Schema...")
    tgt = req("http://127.0.0.1:8000/api/schemas/target")
    print(f"   Target Fields: {len(tgt['fields'])}")

    print("3. Testing Initial Plan v1 Dry-Run...")
    dry1 = req("http://127.0.0.1:8000/api/plans/1/dry-run", {})
    print(f"   Plan v1 Dry-Run: {dry1['accepted_count']} Valid, {dry1['rejected_count']} Quarantined ({dry1['execution_time_ms']}ms)")
    assert dry1['total_source_records'] == 1000

    print("4. Testing AI Propose Plan (Creating Plan v2)...")
    plan2 = req("http://127.0.0.1:8000/api/plans/propose", {})
    v2 = plan2["version"]
    print(f"   AI Created Plan v{v2} ({plan2['status']}) with {len(plan2['field_mappings'])} mappings")
    assert v2 >= 2

    print(f"5. Testing Dry-Run on Plan v{v2}...")
    dry2 = req(f"http://127.0.0.1:8000/api/plans/{v2}/dry-run", {})
    print(f"   Plan v{v2} Dry-Run: {dry2['accepted_count']} Valid, {dry2['rejected_count']} Quarantined")

    print(f"6. Testing Plan v{v2} Approval Guard...")
    appr2 = req(f"http://127.0.0.1:8000/api/plans/{v2}/approve", "Lead Architect")
    print(f"   Status: {appr2['status']}, Sign-off: {appr2['approved_by']}")
    assert appr2['status'] == "APPROVED"

    print(f"7. Testing Target Execution for Plan v{v2}...")
    exec2 = req(f"http://127.0.0.1:8000/api/plans/{v2}/execute", {"plan_version": v2, "executed_by": "Lead Architect"})
    run_id = exec2["run_id"]
    snap_id = exec2["snapshot_id"]
    print(f"   Execution SUCCESS: {exec2['inserted_count']} rows written into SQLite target table")
    assert exec2['inserted_count'] > 0

    print("8. Testing Target Database State...")
    db_rows = req("http://127.0.0.1:8000/api/target/records?limit=10")
    print(f"   Live Target Rows: {db_rows['total_records']}, Total Balance: ${db_rows['financial_total_balance']:,.2f}")
    assert db_rows['total_records'] == exec2['inserted_count']

    print("9. Testing Reconciliation...")
    recon = req(f"http://127.0.0.1:8000/api/reconciliation/{run_id}?plan_version={v2}")
    print(f"   Verdict: {recon['verdict']}, Parity Invariant Passed: {recon['invariants_passed']}")
    assert recon['invariants_passed'] is True
    assert recon['accounting']['unaccounted_records'] == 0

    print("10. Testing Idempotency on Retry...")
    retry2 = req(f"http://127.0.0.1:8000/api/plans/{v2}/execute", {"plan_version": v2, "executed_by": "Lead Architect"})
    print(f"    Inserted on Retry: {retry2['inserted_count']}, Duplicates Skipped: {retry2['skipped_duplicates_count']}")
    assert retry2['inserted_count'] == 0, "Idempotency failed!"

    print("11. Testing Rollback...")
    rb = req("http://127.0.0.1:8000/api/rollback", {"snapshot_id": snap_id, "run_id": run_id, "actor": "Lead Architect"})
    print(f"    Rollback Status: {rb['status']}, Records Removed: {rb['records_removed']}, Remaining in DB: {rb['target_records_remaining']}")
    assert rb['target_records_remaining'] == 0

    print("12. Testing Audit Trail...")
    audit = req("http://127.0.0.1:8000/api/audit-trail")
    event_types = [e["event_type"] for e in audit]
    print(f"    Events in Ledger ({len(audit)}): {event_types[:6]}")
    assert "MIGRATION_EXECUTED" in event_types
    assert "MIGRATION_RETRIED" in event_types
    assert "MIGRATION_ROLLED_BACK" in event_types

    print("\n=======================================================")
    print(">>> ALL 12 END-TO-END WORKBENCH TEST CASES PASSED! <<<")
    print("=======================================================\n")

if __name__ == "__main__":
    main()
