from __future__ import annotations
import os
import json
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, HTTPException, Query, Body, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from .models.schemas import (
    MigrationPlan,
    DryRunSummary,
    ExecutionRunResult,
    RollbackResult,
    ReconciliationReport,
    ExecutionRunRequest,
    DatasetSchema,
    TransformationRuleSpec,
    current_utc_iso,
)
from .engine.profiler import (
    InspectionTools,
    load_source_schema,
    load_target_schema,
    load_sample_records
)
from .engine.agent import MigrationPlannerAgent
from .engine.transforms import SUPPORTED_RULES
from .engine.dry_run import DryRunEngine
from .engine.target_store import TargetDatabaseStore
from .engine.executor import ExecutionEngine
from .engine.reconciliation import ReconciliationEngine
from .engine.history import PlanManager

app = FastAPI(
    title="Agentic Data Migration Planner & Reconciliation Workbench",
    description="Production-grade API for planning, validating, executing, and reconciling bounded data migrations.",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Shared singletons for workbench state
target_store = TargetDatabaseStore()
inspection_tools = InspectionTools()
agent = MigrationPlannerAgent(inspection_tools=inspection_tools)
dry_runner = DryRunEngine()
executor = ExecutionEngine(target_store=target_store)
reconciler = ReconciliationEngine(target_store=target_store)
plan_manager = PlanManager(target_store=target_store)

# Initialize with initial proposed plan v1
initial_plan = agent.generate_plan(plan_version=1)
plan_manager.save_plan(initial_plan, actor="AI Migration Planner Agent")


# ============================================================================
# Schema & Data Endpoints
# ============================================================================

@app.get("/api/schemas/source")
def get_source_schema() -> Dict[str, Any]:
    schema = inspection_tools.source_schema
    profiles = inspection_tools.profile_all_columns()
    return {
        "schema": schema,
        "profiles": profiles,
        "total_sample_records": len(inspection_tools.records)
    }

@app.get("/api/schemas/target")
def get_target_schema() -> Dict[str, Any]:
    return inspection_tools.target_schema

@app.post("/api/upload/source")
async def upload_source_dataset(
    file: Optional[UploadFile] = File(None),
    raw_json: Optional[List[Dict[str, Any]]] = Body(None)
) -> Dict[str, Any]:
    """Upload custom CSV or JSON dataset from the user."""
    import csv, io
    from .engine.profiler import infer_schema_from_records

    records: List[Dict[str, Any]] = []
    filename = "Custom Dataset"

    if file:
        filename = file.filename or "uploaded_data"
        content_bytes = await file.read()
        content_str = content_bytes.decode("utf-8", errors="replace")

        lower_name = filename.lower()
        if lower_name.endswith(".json"):
            try:
                parsed = json.loads(content_str)
                records = parsed if isinstance(parsed, list) else parsed.get("records", [])
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Invalid JSON file: {e}")
        elif lower_name.endswith(".xlsx") or lower_name.endswith(".xls"):
            try:
                import openpyxl
                wb = openpyxl.load_workbook(io.BytesIO(content_bytes), data_only=True)
                sheet = wb.active
                rows = list(sheet.iter_rows(values_only=True))
                if rows:
                    raw_headers = [str(h).strip() if h is not None else f"col_{i}" for i, h in enumerate(rows[0])]
                    for r in rows[1:]:
                        if any(v is not None and str(v).strip() != "" for v in r):
                            row_dict = {}
                            for h, val in zip(raw_headers, r):
                                row_dict[h] = str(val).strip() if val is not None else ""
                            records.append(row_dict)
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Invalid Excel spreadsheet: {e}")
        else:
            # Parse as CSV
            try:
                reader = csv.DictReader(io.StringIO(content_str))
                records = [dict(row) for row in reader]
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Invalid CSV file: {e}")
    elif raw_json is not None:
        records = raw_json
    else:
        raise HTTPException(status_code=400, detail="No file or JSON payload provided")

    if not records:
        raise HTTPException(status_code=400, detail="The uploaded dataset contains 0 records.")

    # Infer source schema dynamically
    new_schema = infer_schema_from_records(records, dataset_name=filename)
    
    # Update inspection tools with new dataset
    inspection_tools.records = records
    inspection_tools.source_schema = new_schema

    # Dynamically generate next incremental Plan for the new dataset
    existing_plans = plan_manager.list_plans()
    next_ver = max([p.version for p in existing_plans]) + 1 if existing_plans else 1
    new_plan = agent.generate_plan(plan_version=next_ver)
    plan_manager.save_plan(new_plan, actor=f"User Upload ({filename})")

    return {
        "status": "SUCCESS",
        "message": f"Successfully ingested {len(records)} records from '{filename}'. AI proposed Plan v{next_ver}.",
        "schema": new_schema,
        "total_records": len(records),
        "plan": new_plan
    }

@app.post("/api/load-test-records")
def load_test_records() -> Dict[str, Any]:
    """Instantly loads user_test_records.csv (100 records) into the workbench."""
    import csv
    from .engine.profiler import infer_schema_from_records

    test_csv_path = os.path.join(os.path.dirname(__file__), "data", "user_test_records.csv")
    if not os.path.exists(test_csv_path):
        raise HTTPException(status_code=404, detail="user_test_records.csv not found in data directory")

    with open(test_csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        records = [dict(r) for r in reader]

    new_schema = infer_schema_from_records(records, dataset_name="user_test_records.csv")
    inspection_tools.records = records
    inspection_tools.source_schema = new_schema

    existing_plans = plan_manager.list_plans()
    next_ver = max([p.version for p in existing_plans]) + 1 if existing_plans else 1
    new_plan = agent.generate_plan(plan_version=next_ver)
    plan_manager.save_plan(new_plan, actor="Quick Load (user_test_records.csv)")

    return {
        "status": "SUCCESS",
        "message": f"Loaded 100 test records from 'user_test_records.csv'. AI proposed Plan v{next_ver}.",
        "schema": new_schema,
        "total_records": len(records),
        "plan": new_plan
    }

@app.post("/api/schema/target")
def update_target_schema(schema_payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Updates target schema definition and regenerates plan."""
    if "fields" not in schema_payload or not schema_payload["fields"]:
        raise HTTPException(status_code=400, detail="Invalid target schema: 'fields' list required")
    
    inspection_tools.target_schema = schema_payload
    # Regenerate plan
    plans = plan_manager.list_plans()
    next_ver = max([p.version for p in plans]) + 1 if plans else 1
    new_plan = agent.generate_plan(plan_version=next_ver)
    plan_manager.save_plan(new_plan, actor="Target Schema Update")

    return {
        "status": "SUCCESS",
        "message": "Target schema updated and new plan generated.",
        "target_schema": schema_payload,
        "plan": new_plan
    }

@app.get("/api/export/target")
def export_target_records(format: str = Query(default="csv")):
    """Exports all target database records as CSV or JSON."""
    import csv, io
    from fastapi.responses import Response

    rows = target_store.query_customers(limit=100000, offset=0)
    if not rows:
        raise HTTPException(status_code=400, detail="Target store has 0 records. Execute migration first.")

    if format == "json":
        return Response(
            content=json.dumps(rows, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=target_customers_migrated.json"}
        )
    
    # Export CSV
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=target_customers_migrated.csv"}
    )

@app.get("/api/export/quarantine")
def export_quarantine_records():
    """Exports all quarantined records with error evidence as CSV."""
    import csv, io
    from fastapi.responses import Response

    with target_store.get_connection() as conn:
        rows = conn.execute("SELECT * FROM quarantine_ledger ORDER BY source_row_index ASC").fetchall()
        if not rows:
            raise HTTPException(status_code=400, detail="Quarantine ledger has 0 records.")
        
        flat_rows = []
        for r in rows:
            d = dict(r)
            errors = json.loads(d["errors"])
            payload = json.loads(d["source_payload"])
            err_summary = "; ".join([f"[{e['field']}]: {e['error_message']}" for e in errors])
            flat_rows.append({
                "source_row_index": d["source_row_index"] + 1,
                "natural_key": d["source_natural_key"],
                "run_id": d["run_id"],
                "errors_detected": err_summary,
                "raw_payload": json.dumps(payload),
                "created_at": d["created_at"]
            })

        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=list(flat_rows[0].keys()))
        writer.writeheader()
        writer.writerows(flat_rows)
        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=quarantine_error_ledger.csv"}
        )

@app.get("/api/samples")
def get_sample_records(limit: int = Query(default=10, ge=1, le=100)) -> List[Dict[str, Any]]:
    return inspection_tools.sample_records(n=limit)

@app.get("/api/transforms")
def get_supported_transforms() -> List[TransformationRuleSpec]:
    return SUPPORTED_RULES


# ============================================================================
# Plan Management & Agent Endpoints
# ============================================================================

@app.post("/api/plans/propose")
def propose_new_plan() -> MigrationPlan:
    """Invokes AI agent to inspect schemas and propose a new plan version."""
    plans = plan_manager.list_plans()
    next_ver = max([p.version for p in plans]) + 1 if plans else 1
    new_plan = agent.generate_plan(plan_version=next_ver)
    return plan_manager.save_plan(new_plan, actor="AI Migration Agent")

@app.get("/api/plans")
def list_plans() -> List[MigrationPlan]:
    return plan_manager.list_plans()

@app.get("/api/plans/{version}")
def get_plan(version: int) -> MigrationPlan:
    p = plan_manager.get_plan(version)
    if not p:
        p = agent.generate_plan(plan_version=version)
        plan_manager.save_plan(p, actor="AI Migration Agent")
    return p

@app.post("/api/plans/{version}/approve")
def approve_plan(version: int, approved_by: str = Body(default="Lead Data Engineer")) -> MigrationPlan:
    try:
        p = plan_manager.get_plan(version)
        if not p:
            p = agent.generate_plan(plan_version=version)
            plan_manager.save_plan(p, actor="AI Migration Agent")
        return plan_manager.approve_plan(version, approved_by=approved_by)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

@app.post("/api/plans/{version}/clarifications")
def update_plan_clarifications(
    version: int,
    payload: Dict[str, str] = Body(...)
) -> MigrationPlan:
    """Updates user answers to clarification questions and synchronizes transformation parameters."""
    question_id = payload.get("question_id")
    user_answer = payload.get("user_answer")

    plan = plan_manager.get_plan(version)
    if not plan:
        raise HTTPException(status_code=404, detail=f"Plan version {version} not found")

    target_q = None
    for q in plan.clarifications:
        if q.question_id == question_id:
            q.user_answer = user_answer
            target_q = q
            break

    if not target_q:
        raise HTTPException(status_code=404, detail=f"Clarification question '{question_id}' not found")

    # Propagate policy changes to field_mappings
    import re
    for m in plan.field_mappings:
        # Status fallback policy
        if question_id == "clarify_status_x" and m.target_field == "status":
            if "SUSPENDED" in user_answer:
                m.parameters["fallback"] = "SUSPENDED"
            elif "INACTIVE" in user_answer:
                m.parameters["fallback"] = "INACTIVE"
            elif "Quarantine" in user_answer:
                m.parameters.pop("fallback", None)

        # Name fallback policy
        elif question_id == "clarify_empty_names" and m.target_field == "first_name":
            if "VALUED_CUSTOMER" in user_answer:
                m.parameters["default"] = "VALUED_CUSTOMER"
                m.parameters["required"] = False
            elif "Quarantine" in user_answer:
                m.parameters["required"] = True
                m.parameters.pop("default", None)

        # Phone invalid policy
        elif question_id == "clarify_bad_phones" and m.target_field == "phone_e164":
            if "null" in user_answer.lower():
                m.parameters["on_invalid"] = "null"
            elif "Quarantine" in user_answer:
                m.parameters.pop("on_invalid", None)

        # Dynamic fallback policy (e.g. clarify_risk_tier, clarify_country_iso2)
        elif question_id.startswith("clarify_") and m.target_field == target_q.field:
            if "Apply fallback default" in user_answer:
                match = re.search(r"'(.*?)'", user_answer)
                if match:
                    m.parameters["default"] = match.group(1)
            elif "Quarantine" in user_answer:
                m.parameters["required"] = True
                m.parameters["default"] = None

    # If the plan was approved, reset status to PROPOSED upon policy change
    if plan.status == "APPROVED":
        plan.status = "PROPOSED"
        plan.approved_by = None
        plan.approved_at = None

    return plan_manager.save_plan(plan, actor="User Clarification Decision")

@app.post("/api/plans/{version}/mappings")
def update_plan_mapping(
    version: int,
    mapping_payload: Dict[str, Any] = Body(...)
) -> MigrationPlan:
    """Updates a specific field mapping rule in the plan."""
    plan = plan_manager.get_plan(version)
    if not plan:
        raise HTTPException(status_code=404, detail=f"Plan version {version} not found")

    target_field = mapping_payload.get("target_field")
    from .models.schemas import FieldMapping
    found = False
    for i, m in enumerate(plan.field_mappings):
        if m.target_field == target_field:
            plan.field_mappings[i] = FieldMapping(**mapping_payload)
            found = True
            break

    if not found:
        plan.field_mappings.append(FieldMapping(**mapping_payload))

    if plan.status == "APPROVED":
        plan.status = "PROPOSED"
        plan.approved_by = None
        plan.approved_at = None

    return plan_manager.save_plan(plan, actor="User Mapping Edit")

@app.post("/api/plans/{version}/fork")
def fork_plan(version: int, updated_mappings: List[Dict[str, Any]] = Body(...)) -> MigrationPlan:
    try:
        return plan_manager.create_next_version(version, updated_mappings, actor="User Customization")
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ============================================================================
# Dry-Run Simulation Endpoints
# ============================================================================

@app.post("/api/plans/{version}/dry-run")
def execute_dry_run(version: int) -> DryRunSummary:
    p = plan_manager.get_plan(version)
    if not p:
        p = agent.generate_plan(plan_version=version)
        plan_manager.save_plan(p, actor="AI Migration Agent")

    summary, valids, quars = dry_runner.execute_dry_run(p, records=inspection_tools.records)

    # Save quarantine records from dry run into ledger
    import json
    now_iso = current_utc_iso()
    with target_store.get_connection() as conn:
        for q in quars:
            conn.execute(
                """
                INSERT OR REPLACE INTO quarantine_ledger (
                    quarantine_id, run_id, source_row_index, source_natural_key,
                    source_payload, errors, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    q.quarantine_id, summary.run_id, q.source_row_index, q.source_natural_key,
                    json.dumps(q.source_payload), json.dumps([e.model_dump() for e in q.errors]), now_iso
                )
            )
        conn.commit()

    target_store.log_audit_event(
        event_id=f"evt_dry_{summary.run_id}",
        event_type="DRY_RUN_EXECUTED",
        actor="Data Workbench Simulator",
        details={
            "plan_version": version,
            "run_id": summary.run_id,
            "total_records": summary.total_source_records,
            "accepted_count": summary.accepted_count,
            "rejected_count": summary.rejected_count,
            "field_errors": summary.field_error_breakdown
        }
    )
    return summary


# ============================================================================
# Execution, Retry & Rollback Endpoints
# ============================================================================

@app.post("/api/plans/{version}/execute")
def execute_migration(
    version: int,
    req: ExecutionRunRequest = Body(default_factory=lambda: ExecutionRunRequest(plan_version=1))
) -> ExecutionRunResult:
    p = plan_manager.get_plan(version)
    if not p:
        p = agent.generate_plan(plan_version=version)
        plan_manager.save_plan(p, actor="AI Migration Agent")

    if p.status != "APPROVED":
        raise HTTPException(
            status_code=400,
            detail=f"Execution blocked: Plan v{version} is in status '{p.status}'. You must approve the plan before execution."
        )

    try:
        return executor.execute_migration(p, actor=req.executed_by, records=inspection_tools.records)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/rollback")
def rollback_migration(
    snapshot_id: str = Body(..., embed=True),
    run_id: str = Body(..., embed=True),
    actor: str = Body(default="Lead Data Engineer", embed=True)
) -> RollbackResult:
    return executor.rollback_migration(snapshot_id=snapshot_id, run_id=run_id, actor=actor)


# ============================================================================
# Target Store & Reconciliation Endpoints
# ============================================================================

@app.get("/api/target/records")
def get_target_records(
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0)
) -> Dict[str, Any]:
    total = target_store.get_customer_count()
    rows = target_store.query_customers(limit=limit, offset=offset)
    financial_total = target_store.get_financial_aggregate()
    return {
        "total_records": total,
        "financial_total_balance": financial_total,
        "limit": limit,
        "offset": offset,
        "records": rows
    }

@app.get("/api/quarantine/records")
def get_quarantine_records(
    run_id: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=200)
) -> List[Dict[str, Any]]:
    import json
    with target_store.get_connection() as conn:
        if run_id:
            rows = conn.execute(
                "SELECT * FROM quarantine_ledger WHERE run_id = ? ORDER BY source_row_index ASC LIMIT ?",
                (run_id, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM quarantine_ledger ORDER BY created_at DESC, source_row_index ASC LIMIT ?",
                (limit,)
            ).fetchall()

        result = []
        for r in rows:
            d = dict(r)
            d["source_payload"] = json.loads(d["source_payload"])
            d["errors"] = json.loads(d["errors"])
            result.append(d)
        return result

@app.get("/api/reconciliation/{run_id}")
def get_reconciliation_report(run_id: str, plan_version: int = 1) -> ReconciliationReport:
    return reconciler.reconcile(run_id=run_id, plan_version=plan_version, source_records=inspection_tools.records)

@app.get("/api/audit-trail")
def get_audit_trail() -> List[Dict[str, Any]]:
    return target_store.get_audit_trail(limit=50)

@app.post("/api/reset")
def reset_workbench() -> Dict[str, str]:
    """Resets the mock target store and audit trail for clean demo runs."""
    target_store.reset_database()
    inspection_tools.records = load_sample_records()
    inspection_tools.source_schema = load_source_schema()
    inspection_tools.target_schema = load_target_schema()
    init_p = agent.generate_plan(plan_version=1)
    plan_manager.reset(init_p)
    return {"status": "SUCCESS", "message": "Target database, plans, and audit ledger have been reset to clean state."}


# Mount frontend static directories
FRONTEND_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "frontend"))

if os.path.exists(FRONTEND_DIR):
    css_dir = os.path.join(FRONTEND_DIR, "css")
    js_dir = os.path.join(FRONTEND_DIR, "js")
    assets_dir = os.path.join(FRONTEND_DIR, "assets")

    if os.path.exists(css_dir):
        app.mount("/css", StaticFiles(directory=css_dir), name="css")
    if os.path.exists(js_dir):
        app.mount("/js", StaticFiles(directory=js_dir), name="js")
    if os.path.exists(assets_dir):
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/")
    def serve_index():
        return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))

    @app.get("/{full_path:path}")
    def serve_spa_fallback(full_path: str):
        # Don't intercept API routes
        if full_path.startswith("api"):
            raise HTTPException(status_code=404, detail="API endpoint not found")
        index_file = os.path.join(FRONTEND_DIR, "index.html")
        if os.path.exists(index_file):
            return FileResponse(index_file)
        raise HTTPException(status_code=404, detail="Frontend index.html not found")
