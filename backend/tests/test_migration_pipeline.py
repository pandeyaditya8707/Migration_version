import pytest
import os
from app.engine.profiler import InspectionTools
from app.engine.agent import MigrationPlannerAgent
from app.engine.dry_run import DryRunEngine
from app.engine.target_store import TargetDatabaseStore
from app.engine.executor import ExecutionEngine
from app.engine.reconciliation import ReconciliationEngine
from app.engine.history import PlanManager

@pytest.fixture
def test_env(tmp_path):
    db_file = str(tmp_path / "test_target.db")
    store = TargetDatabaseStore(db_path=db_file)
    tools = InspectionTools()
    agent = MigrationPlannerAgent(inspection_tools=tools)
    dry_runner = DryRunEngine()
    executor = ExecutionEngine(target_store=store)
    recon = ReconciliationEngine(target_store=store)
    plan_mgr = PlanManager(target_store=store)
    return {
        "store": store,
        "tools": tools,
        "agent": agent,
        "dry_runner": dry_runner,
        "executor": executor,
        "recon": recon,
        "plan_mgr": plan_mgr
    }

def test_agent_proposes_valid_plan(test_env):
    agent = test_env["agent"]
    plan = agent.generate_plan(plan_version=1)

    assert plan.version == 1
    assert plan.status == "PROPOSED"
    assert len(plan.field_mappings) == 11
    assert len(plan.clarifications) >= 2
    # Verify risk levels are assigned
    risk_levels = [m.risk_level for m in plan.field_mappings]
    assert "HIGH" in risk_levels
    assert "LOW" in risk_levels

def test_deterministic_dry_run(test_env):
    agent = test_env["agent"]
    dry_runner = test_env["dry_runner"]
    plan = agent.generate_plan(plan_version=1)

    summary, valids, quars = dry_runner.execute_dry_run(plan)

    # Invariant: Total == Accepted + Quarantined
    assert summary.total_source_records == 1000
    assert summary.accepted_count + summary.rejected_count == summary.total_source_records
    assert len(valids) == summary.accepted_count
    assert len(quars) == summary.rejected_count

    # Check field level error evidence in quarantine
    assert len(quars) > 0
    first_q = quars[0]
    assert first_q.source_row_index is not None
    assert len(first_q.errors) > 0
    assert first_q.errors[0].field != ""
    assert first_q.errors[0].error_message != ""

def test_execution_requires_approval(test_env):
    agent = test_env["agent"]
    executor = test_env["executor"]
    plan = agent.generate_plan(plan_version=1)

    assert plan.status == "PROPOSED"
    with pytest.raises(ValueError, match="User approval is mandatory"):
        executor.execute_migration(plan)

def test_full_execution_idempotency_and_rollback(test_env):
    agent = test_env["agent"]
    executor = test_env["executor"]
    store = test_env["store"]
    recon = test_env["recon"]
    plan_mgr = test_env["plan_mgr"]

    # 1. Generate and Approve Plan
    plan = agent.generate_plan(plan_version=1)
    plan_mgr.save_plan(plan)
    approved_plan = plan_mgr.approve_plan(version=1, approved_by="Principal Architect")
    assert approved_plan.status == "APPROVED"

    # 2. First Execution
    run_res_1 = executor.execute_migration(approved_plan)
    assert run_res_1.status == "SUCCESS"
    assert run_res_1.inserted_count > 0
    assert run_res_1.skipped_duplicates_count == 0
    first_count = store.get_customer_count()
    assert first_count == run_res_1.inserted_count

    # 3. Reconciliation after first run
    report_1 = recon.reconcile(run_id=run_res_1.run_id, plan_version=1)
    assert report_1.accounting["unaccounted_records"] == 0
    assert report_1.invariants_passed is True

    # 4. Idempotent Retry: Re-execute the exact same migration
    run_res_2 = executor.execute_migration(approved_plan, is_retry=True)
    assert run_res_2.status == "SUCCESS"
    # All records should be recognized as duplicates, 0 new inserts!
    assert run_res_2.inserted_count == 0
    assert run_res_2.skipped_duplicates_count == first_count
    # Database count must remain unchanged!
    assert store.get_customer_count() == first_count

    # 5. Rollback to the pre-migration snapshot
    rollback_res = executor.rollback_migration(snapshot_id=run_res_1.snapshot_id, run_id=run_res_1.run_id)
    assert rollback_res.status == "SUCCESS"
    assert rollback_res.records_removed == first_count
    # Target store is restored to 0 records!
    assert store.get_customer_count() == 0

    # 6. Audit Trail verification
    trail = store.get_audit_trail()
    event_types = [e["event_type"] for e in trail]
    assert "MIGRATION_EXECUTED" in event_types
    assert "MIGRATION_RETRIED" in event_types
    assert "MIGRATION_ROLLED_BACK" in event_types
