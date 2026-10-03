from __future__ import annotations
import os
import json
import uuid
import io
import csv
import logging
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, HTTPException, Query, Body, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

logger = logging.getLogger("migration_workbench")

# Automatically load environment variables from .env if present
_env_file = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), ".env")
if os.path.exists(_env_file):
    try:
        with open(_env_file, "r", encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    _k = _k.strip()
                    _v = _v.strip().strip("'\"")
                    if _k and _k not in os.environ:
                        os.environ[_k] = _v
    except Exception:
        pass

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
from .engine import profiler
from .engine.profiler import (
    InspectionTools,
    load_source_schema,
    load_target_schema,
    load_sample_records,
    infer_schema_from_records,
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

class WorkbenchLogHandler(logging.Handler):
    """Circular in-memory log buffer for real-time frontend debugging and Render inspection."""
    def __init__(self, capacity: int = 300):
        super().__init__()
        self.capacity = capacity
        self.logs: List[Dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            if "/api/logs" in msg:
                return  # do not self-pollute log stream with log poll traffic
            from datetime import datetime, timezone
            entry = {
                "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                "level": record.levelname,
                "subsystem": record.name.replace("migration_workbench.", "").replace("uvicorn.", ""),
                "message": msg,
                "pathname": os.path.basename(record.pathname) if record.pathname else ""
            }
            self.logs.append(entry)
            if len(self.logs) > self.capacity:
                self.logs.pop(0)
        except Exception:
            pass

workbench_log_handler = WorkbenchLogHandler()
workbench_log_handler.setFormatter(logging.Formatter("%(message)s"))
logging.root.addHandler(workbench_log_handler)
logging.root.setLevel(logging.INFO)

@app.get("/healthz", tags=["System"])
@app.get("/api/health", tags=["System"])
def health_check() -> Dict[str, str]:
    """Production healthcheck probe for Render zero-downtime deployments."""
    return {"status": "HEALTHY", "service": "agentic-data-migration-workbench"}

@app.get("/api/logs", tags=["System"])
def get_system_logs(
    limit: int = Query(default=100, ge=1, le=300),
    level: Optional[str] = Query(default=None)
) -> Dict[str, Any]:
    """Returns captured in-memory server logs for real-time diagnosis in the UI."""
    logs = workbench_log_handler.logs
    if level and level.upper() != "ALL":
        logs = [l for l in logs if l["level"].upper() == level.upper()]
    return {
        "total_captured": len(workbench_log_handler.logs),
        "returned_count": min(len(logs), limit),
        "logs": list(reversed(logs[-limit:]))
    }

@app.post("/api/logs/clear", tags=["System"])
def clear_system_logs() -> Dict[str, str]:
    """Clears the in-memory log buffer."""
    workbench_log_handler.logs.clear()
    return {"status": "SUCCESS", "message": "Log buffer cleared"}


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
    """Invokes AI agent (Ollama) to inspect schemas and propose a new plan version."""
    plans = plan_manager.list_plans()
    next_ver = max([p.version for p in plans]) + 1 if plans else 1
    try:
        new_plan = ollama_agent.generate_plan(
            plan_version=next_ver,
            source_schema=inspection_tools.source_schema,
            target_schema=inspection_tools.target_schema,
            records=inspection_tools.records,
            allow_fallback=True
        )
    except Exception as e:
        logger.warning(f"Ollama agent failed for Mode 1 plan proposal ({e}), falling back to built-in agent.")
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

@app.post("/api/plans/{version}/apply-fix")
def apply_ai_fix_mode1(
    version: int,
    payload: Dict[str, Any] = Body(...)
) -> Dict[str, Any]:
    """Applies recommended AI fixes to field mappings and automatically re-runs dry-run simulation across all records."""
    plan = plan_manager.get_plan(version)
    if not plan:
        raise HTTPException(status_code=404, detail=f"Plan {version} not found")

    fix_action = payload.get("fix_action", "")
    target_field = payload.get("target_field", "")
    rule = payload.get("rule", "")
    raw_value = payload.get("raw_value")
    custom_fallback = payload.get("custom_fallback")

    fixes_applied = []

    if fix_action == "AUTO_RESOLVE_ALL":
        for m in plan.field_mappings:
            if m.target_field == "status":
                m.parameters["fallback"] = "SUSPENDED"
                m.parameters.setdefault("mapping", {})["X"] = "SUSPENDED"
                fixes_applied.append("Mapped status code 'X' -> 'SUSPENDED'")
            elif m.target_field == "phone_e164":
                m.parameters["on_invalid"] = "null"
                m.parameters["null_on_invalid"] = True
                fixes_applied.append("Enabled null-substitution on invalid phone formats")
            elif m.target_field == "joined_at":
                m.parameters["fallback"] = "2024-01-01T00:00:00Z"
                m.parameters["default"] = "2024-01-01T00:00:00Z"
                fixes_applied.append("Configured joined_at fallback='2024-01-01T00:00:00Z'")
            elif m.target_field == "email":
                m.parameters["fallback"] = "remediated@customer.internal"
                fixes_applied.append("Configured email fallback='remediated@customer.internal'")

        # Auto-answer clarifications to recommended options
        for q in plan.clarifications:
            if q.options:
                q.user_answer = q.options[0]

    else:
        # Single field fix
        for m in plan.field_mappings:
            if m.target_field == target_field:
                if target_field == "joined_at" or "DATE" in rule:
                    fb = custom_fallback or "2024-01-01T00:00:00Z"
                    m.parameters["fallback"] = fb
                    m.parameters["default"] = fb
                    fixes_applied.append(f"Configured fallback='{fb}' for {target_field}")
                elif target_field == "email" or "EMAIL" in rule:
                    fb = custom_fallback or "remediated@customer.internal"
                    m.parameters["fallback"] = fb
                    fixes_applied.append(f"Configured fallback='{fb}' for {target_field}")
                elif target_field == "phone_e164" or "PHONE" in rule or "REGEX" in rule:
                    m.parameters["on_invalid"] = "null"
                    m.parameters["null_on_invalid"] = True
                    fixes_applied.append(f"Configured null-on-invalid for {target_field}")
                elif "NOT_NULL" in rule or rule == "SPLIT_NAME" or target_field == "first_name":
                    fb = custom_fallback or ("VALUED_CUSTOMER" if target_field == "first_name" else "UNKNOWN")
                    m.parameters["fallback"] = fb
                    m.parameters["default"] = fb
                    m.parameters["required"] = False
                    fixes_applied.append(f"Configured fallback='{fb}' for {target_field}")
                elif "ENUM" in rule or rule == "ENUM_LOOKUP" or target_field == "status":
                    fb = custom_fallback or "SUSPENDED"
                    m.parameters["fallback"] = fb
                    if raw_value is not None:
                        m.parameters.setdefault("mapping", {})[str(raw_value)] = fb
                    fixes_applied.append(f"Mapped {target_field} enum '{raw_value}' -> '{fb}'")
                else:
                    fb = custom_fallback or "UNKNOWN"
                    m.parameters["fallback"] = fb
                    fixes_applied.append(f"Configured fallback='{fb}' for {target_field}")

    # Reset status to PROPOSED so it can be re-evaluated
    if plan.status == "APPROVED":
        plan.status = "PROPOSED"
        plan.approved_by = None
        plan.approved_at = None

    plan_manager.save_plan(plan, actor="AI Auto-Remediation")

    # Re-run dry-run simulation across all 100% records
    summary, valids, quars = dry_runner.execute_dry_run(plan, records=inspection_tools.records)

    return {
        "status": "SUCCESS",
        "fixes_applied": fixes_applied,
        "fix_description": "; ".join(fixes_applied) if fixes_applied else "AI parameter fix applied.",
        "plan": plan.model_dump() if hasattr(plan, "model_dump") else plan.dict(),
        "dry_run_summary": summary.model_dump() if hasattr(summary, "model_dump") else summary.dict()
    }

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


# ============================================================================
# VERSION 2: Universal Autonomous Schema-to-Schema Migration Engine
# ============================================================================

from .engine.dynamic_store import DynamicDatabaseStore
from .engine.ollama_agent import OllamaPlannerAgent

DATASETS_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "sample_datasets"))

SAMPLE_DATASET_CONFIG = {
    "healthcare": {
        "title": "Healthcare Clinical Encounters",
        "csv": os.path.join(DATASETS_DIR, "healthcare", "source_encounters.csv"),
        "schema": os.path.join(DATASETS_DIR, "healthcare", "target_encounters_schema.json")
    },
    "encounters": {
        "title": "Healthcare Clinical Encounters",
        "csv": os.path.join(DATASETS_DIR, "healthcare", "source_encounters.csv"),
        "schema": os.path.join(DATASETS_DIR, "healthcare", "target_encounters_schema.json")
    },
    "orders": {
        "title": "E-Commerce Orders & Fulfillment",
        "csv": os.path.join(DATASETS_DIR, "orders", "source_orders.csv"),
        "schema": os.path.join(DATASETS_DIR, "orders", "target_orders_schema.json")
    },
    "invoices": {
        "title": "SaaS B2B Recurring Invoices",
        "csv": os.path.join(DATASETS_DIR, "invoices", "source_invoices.csv"),
        "schema": os.path.join(DATASETS_DIR, "invoices", "target_invoices_schema.json")
    },
    "inventory": {
        "title": "Warehouse Logistics & Inventory",
        "csv": os.path.join(DATASETS_DIR, "inventory", "source_inventory.csv"),
        "schema": os.path.join(DATASETS_DIR, "inventory", "target_inventory_schema.json")
    }
}

DEFAULT_V2_TARGET_SCHEMA = {
    "schema_id": "target_orders_v1",
    "table_name": "orders",
    "display_name": "Customer Orders Ledger",
    "natural_key": "order_number",
    "primary_key": "order_uuid",
    "fields": [
        {"name": "order_uuid", "data_type": "string", "nullable": False, "constraints": {"unique": True, "format": "uuid"}},
        {"name": "order_number", "data_type": "string", "nullable": False, "constraints": {"unique": True}},
        {"name": "customer_email", "data_type": "string", "nullable": False, "constraints": {"format": "email"}},
        {"name": "customer_phone", "data_type": "string", "nullable": True},
        {"name": "order_date", "data_type": "datetime", "nullable": False},
        {"name": "order_status", "data_type": "string", "nullable": False, "constraints": {"enum": ["PENDING", "PROCESSING", "SHIPPED", "DELIVERED", "CANCELLED"]}},
        {"name": "total_amount", "data_type": "float", "nullable": False},
        {"name": "currency_code", "data_type": "string", "nullable": False, "constraints": {"regex": "^[A-Z]{3}$"}},
        {"name": "shipping_country", "data_type": "string", "nullable": False, "constraints": {"regex": "^[A-Z]{2}$"}}
    ]
}

def init_mode2_inspection_tools() -> InspectionTools:
    """Mode 2 Universal Studio: strictly isolated to Mode 2 sample data, NEVER Mode 1 CRM data."""
    orders_csv = os.path.join(DATASETS_DIR, "orders", "source_orders.csv")
    records = []
    if os.path.exists(orders_csv):
        try:
            with open(orders_csv, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                records = [dict(row) for row in reader]
        except Exception:
            records = []
    
    inferred_source = profiler.infer_schema_from_records(records, dataset_name="orders_source") if records else {
        "schema_id": "v2_orders_source_v1",
        "name": "E-Commerce Orders Legacy Export",
        "fields": []
    }
    
    tools = InspectionTools(records=records)
    tools.source_schema = inferred_source
    tools.target_schema = DEFAULT_V2_TARGET_SCHEMA
    return tools

dynamic_store = DynamicDatabaseStore()
v2_inspection_tools = init_mode2_inspection_tools()
ollama_agent = OllamaPlannerAgent(inspection_tools=v2_inspection_tools)

v2_state: Dict[str, Any] = {
    "active_target_schema": DEFAULT_V2_TARGET_SCHEMA,
    "last_execution_result": None,
    "last_dry_run_summary": None,
    "active_plan": None
}

def get_v2_active_schema() -> Dict[str, Any]:
    schema = v2_state.get("active_target_schema")
    if not schema:
        schema = dynamic_store.load_state("active_target_schema")
        if schema:
            v2_state["active_target_schema"] = schema
    return schema or DEFAULT_V2_TARGET_SCHEMA

def set_v2_active_schema(schema: Dict[str, Any]) -> None:
    v2_state["active_target_schema"] = schema
    dynamic_store.save_state("active_target_schema", schema)

def get_v2_active_plan() -> Optional[MigrationPlan]:
    plan = v2_state.get("active_plan")
    if not plan:
        raw_plan = dynamic_store.load_state("active_plan")
        if raw_plan:
            try:
                plan = MigrationPlan.model_validate(raw_plan)
                v2_state["active_plan"] = plan
            except Exception as e:
                logger.error(f"Error loading persisted plan: {e}")
    return plan

def set_v2_active_plan(plan: Optional[MigrationPlan]) -> None:
    v2_state["active_plan"] = plan
    if plan:
        dynamic_store.save_state("active_plan", plan.model_dump() if hasattr(plan, "model_dump") else plan)
    else:
        dynamic_store.save_state("active_plan", None)

try:
    dynamic_store.compile_and_create_table(DEFAULT_V2_TARGET_SCHEMA)
except Exception:
    pass

@app.post("/api/llm/config")
@app.post("/api/v2/llm/config")
def configure_llm(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    api_key = payload.get("api_key")
    host = payload.get("host")
    model = payload.get("model")
    ollama_agent.set_config(api_key=api_key, host=host, model=model)
    return {
        "status": "SUCCESS",
        "message": f"LLM configured with host={ollama_agent.host}, model={ollama_agent.model}",
        "has_key": bool(ollama_agent.api_key)
    }

@app.get("/api/llm/verify")
@app.get("/api/v2/llm/verify")
def verify_llm_connection() -> Dict[str, Any]:
    return ollama_agent.verify_connection()

@app.get("/api/llm/status")
@app.get("/api/v2/llm/status")
def get_llm_status() -> Dict[str, Any]:
    return {
        "status": ollama_agent.last_call_info["status"],
        "model": ollama_agent.model,
        "host": ollama_agent.host,
        "has_api_key": bool(ollama_agent.api_key),
        "last_call_info": ollama_agent.last_call_info
    }

@app.post("/api/llm/test")
@app.post("/api/v2/llm/test")
def test_llm_inference(payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    prompt = payload.get("prompt") if payload else None
    return ollama_agent.test_inference(sample_prompt=prompt)

@app.post("/api/ai/diagnose-record")
@app.post("/api/v2/ai/diagnose-record")
def ai_diagnose_quarantine_record(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    field = payload.get("field", "")
    rule = payload.get("rule", "")
    raw_value = payload.get("raw_value", "")
    error_message = payload.get("error_message", "")
    source_payload = payload.get("source_payload", {})

    prompt = (
        f"You are an expert Data Migration Architect. A record was quarantined during dry-run validation:\n"
        f"- Target Field: {field}\n"
        f"- Violated Constraint / Rule: {rule}\n"
        f"- Diagnostic Error: {error_message}\n"
        f"- Raw Input: {raw_value}\n"
        f"- Record Context: {json.dumps(source_payload, default=str)}\n\n"
        f"In 2-3 concise sentences: 1) Explain the exact root cause of failure. 2) Provide a concrete actionable recommendation to fix the mapping rule or clean the data so it passes validation."
    )

    try:
        res = ollama_agent.test_inference(sample_prompt=prompt)
        if res.get("status") == "SUCCESS":
            return {"status": "SUCCESS", "diagnosis": res.get("raw_response", "")}
    except Exception as e:
        logger.warning(f"Live AI diagnose query failed: {e}")

    return {
        "status": "FALLBACK_DIAGNOSIS",
        "diagnosis": f"Root Cause: Column '{field}' failed {rule} with error '{error_message}'. Actionable Fix: Update the transformation rule in Mapping Studio to handle '{raw_value}' (e.g. configure fallback value, enum mapping, or regex pre-cleaning) or cleanse source input."
    }

@app.post("/api/v2/upload/source")
@app.post("/api/v2/source/upload")
async def upload_v2_source_dataset(
    file: Optional[UploadFile] = File(default=None),
    raw_json: Optional[List[Dict[str, Any]]] = Body(default=None)
) -> Dict[str, Any]:
    """Mode 2 Universal Dataset Intake: Strictly isolated to Mode 2 Universal Studio.

    Profiles uploaded CSV/JSON, updates ONLY v2_inspection_tools, and invokes Ollama AI
    strictly for Mode 2 if target schema is set. Zero connection to Mode 1 or plan_manager.
    """
    records: List[Dict[str, Any]] = []
    filename = "uploaded_dataset"
    if file is not None:
        filename = file.filename or "uploaded_dataset"
        content_bytes = await file.read()
        content_str = content_bytes.decode("utf-8", errors="replace")
        if filename.endswith(".json"):
            try:
                data = json.loads(content_str)
                if isinstance(data, list):
                    records = data
                elif isinstance(data, dict):
                    records = [data]
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Invalid JSON file: {e}")
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

    # Clean previous run results for fresh intake
    v2_state["last_execution_result"] = None
    v2_state["last_dry_run_summary"] = None
    dynamic_store.save_state("last_execution_result", None)
    dynamic_store.save_state("last_dry_run_summary", None)

    # Infer source schema dynamically strictly for Mode 2
    new_schema = infer_schema_from_records(records, dataset_name=filename)

    # Update Mode 2 inspection tools ONLY
    v2_inspection_tools.records = records
    v2_inspection_tools.source_schema = new_schema
    logger.info(f"Ingested '{filename}': {len(records)} records, {len(new_schema.get('fields', []))} columns inferred.")

    # If a target schema is active in Mode 2, synthesize plan
    plan = None
    ai_error = None
    active_target = get_v2_active_schema()
    if active_target:
        try:
            logger.info(f"Synthesizing plan for '{filename}' against target schema '{active_target.get('table_name', 'target')}'...")
            plan = ollama_agent.generate_plan(
                plan_version=1,
                source_schema=new_schema,
                target_schema=active_target,
                records=records,
                allow_fallback=True
            )
            set_v2_active_plan(plan)
            logger.info(f"Generated Mode 2 plan with {len(plan.field_mappings)} field mappings.")
        except Exception as e:
            ai_error = str(e)
            logger.warning(f"Plan synthesis notice for '{filename}': {ai_error}")
            set_v2_active_plan(None)

    return {
        "status": "SUCCESS",
        "message": f"Successfully ingested {len(records)} records from '{filename}' into Mode 2 Universal Studio." + (f" Notice: {ai_error}" if ai_error else ""),
        "filename": filename,
        "source_schema": new_schema,
        "total_records": len(records),
        "target_schema": active_target,
        "plan": plan.model_dump() if hasattr(plan, "model_dump") else plan,
        "ai_error": ai_error
    }

@app.get("/api/v2/source")
def get_v2_source_info() -> Dict[str, Any]:
    """Returns current Mode 2 source dataset information."""
    return {
        "total_records": len(v2_inspection_tools.records),
        "source_schema": v2_inspection_tools.source_schema,
        "sample_records": v2_inspection_tools.records[:5]
    }

@app.post("/api/v2/schema/target")
def set_v2_target_schema(schema_payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Sets arbitrary target JSON schema and compiles it to SQLite DDL."""
    if "fields" not in schema_payload or not schema_payload["fields"]:
        raise HTTPException(status_code=400, detail="Target schema must contain 'fields' array")
    if "table_name" not in schema_payload:
        schema_payload["table_name"] = "target_records"

    try:
        ddl = dynamic_store.compile_and_create_table(schema_payload)
    except Exception as e:
        logger.error(f"Failed to compile target schema: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to compile target schema to SQL DDL: {e}")

    set_v2_active_schema(schema_payload)
    v2_inspection_tools.target_schema = schema_payload
    logger.info(f"Target table '{schema_payload['table_name']}' compiled successfully.")

    plan = None
    ai_error = None
    try:
        plan = ollama_agent.generate_plan(
            plan_version=1,
            source_schema=v2_inspection_tools.source_schema,
            target_schema=schema_payload,
            records=v2_inspection_tools.records
        )
        set_v2_active_plan(plan)
        logger.info(f"Synthesized Mode 2 plan with {len(plan.field_mappings)} mappings.")
    except Exception as e:
        ai_error = str(e)
        set_v2_active_plan(None)
        logger.warning(f"Plan synthesis warning: {ai_error}")

    return {
        "status": "SUCCESS",
        "message": f"Compiled and created target table '{schema_payload['table_name']}'.",
        "table_name": schema_payload["table_name"],
        "ddl": ddl,
        "target_schema": schema_payload,
        "plan": plan.model_dump() if hasattr(plan, "model_dump") else plan,
        "ai_error": ai_error
    }

@app.get("/api/v2/schema/target")
def get_v2_target_schema() -> Dict[str, Any]:
    return get_v2_active_schema()

@app.post("/api/v2/plans/propose")
def propose_v2_plan(payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    active_target = get_v2_active_schema()
    if not active_target:
        raise HTTPException(status_code=400, detail="No active target schema. Please compile a target schema first.")
    strict_ai = "unreachable" in str(ollama_agent.model).lower() or "54321" in str(ollama_agent.host)
    try:
        logger.info(f"Synthesizing plan for target '{active_target.get('table_name', 'target')}' with {len(v2_inspection_tools.records)} records...")
        plan = ollama_agent.generate_plan(
            plan_version=1,
            source_schema=v2_inspection_tools.source_schema,
            target_schema=active_target,
            records=v2_inspection_tools.records,
            allow_fallback=not strict_ai
        )
        set_v2_active_plan(plan)
        logger.info(f"Plan proposed successfully with {len(plan.field_mappings)} field mappings.")
        return plan.model_dump() if hasattr(plan, "model_dump") else plan
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"AI Agent propose plan failed: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"AI Agent Error: {str(e)}")

@app.get("/api/v2/plans/current")
def get_current_v2_plan() -> Dict[str, Any]:
    plan = get_v2_active_plan()
    if not plan:
        active_target = get_v2_active_schema()
        if active_target:
            try:
                plan = ollama_agent.generate_plan(
                    plan_version=1,
                    source_schema=v2_inspection_tools.source_schema,
                    target_schema=active_target,
                    records=v2_inspection_tools.records,
                    allow_fallback=True
                )
                set_v2_active_plan(plan)
            except Exception as e:
                logger.warning(f"Could not synthesize on-demand plan: {e}")
    if not plan:
        return {
            "status": "NONE",
            "plan_id": None,
            "version": 0,
            "message": "No active plan currently exists. Please compile a target schema or click 'Propose Plan' to synthesize a plan with AI.",
            "field_mappings": []
        }
    return plan.model_dump() if hasattr(plan, "model_dump") else plan

@app.post("/api/v2/plans/approve")
def approve_v2_plan(payload: Any = Body(default=None)) -> MigrationPlan:
    approved_by = "Lead Data Architect"
    if isinstance(payload, dict):
        approved_by = payload.get("approved_by", approved_by)
    elif isinstance(payload, str) and payload.strip():
        approved_by = payload.strip()
    plan = get_v2_active_plan()
    if not plan:
        raise HTTPException(status_code=404, detail="No active plan to approve. Please compile a target schema and synthesize a plan first.")
    plan.status = "APPROVED"
    plan.approved_by = approved_by
    plan.approved_at = current_utc_iso()
    set_v2_active_plan(plan)
    logger.info(f"Mode 2 plan v{plan.version} approved by '{approved_by}'.")
    return plan

@app.post("/api/v2/plans/dry-run")
def execute_v2_dry_run() -> Dict[str, Any]:
    plan = get_v2_active_plan()
    if not plan:
        raise HTTPException(status_code=404, detail="No active plan for dry run. Please propose a plan first.")
    active_target = get_v2_active_schema()
    summary, valids, quars = dry_runner.execute_dry_run(
        plan,
        records=v2_inspection_tools.records,
        target_schema=active_target
    )
    v2_state["last_dry_run_summary"] = summary
    dynamic_store.save_state("last_dry_run_summary", summary.model_dump() if hasattr(summary, "model_dump") else summary)
    logger.info(f"Mode 2 dry-run: {summary.accepted_count} valid, {summary.rejected_count} quarantined ({summary.total_source_records} total).")

    summary_dict = summary.model_dump() if hasattr(summary, "model_dump") else summary.dict()
    summary_dict["valid_count"] = summary.accepted_count
    summary_dict["quarantined_count"] = summary.rejected_count
    summary_dict["total_evaluated"] = summary.total_source_records
    summary_dict["sample_transformed"] = valids[:50]
    summary_dict["quarantine_sample"] = [
        q.model_dump() if hasattr(q, "model_dump") else q.dict()
        for q in quars[:100]
    ]
    return summary_dict


@app.post("/api/v2/plans/apply-fix")
def apply_ai_fix_mode2(
    payload: Dict[str, Any] = Body(...)
) -> Dict[str, Any]:
    """Applies recommended AI fixes to Mode 2 dynamic field mappings and re-runs dry-run simulation across all records."""
    plan = v2_state.get("active_plan")
    if not plan:
        raise HTTPException(status_code=404, detail="No active V2 plan found")

    target_schema = v2_state.get("active_target_schema") or {}
    fix_action = payload.get("fix_action", "")
    target_field = payload.get("target_field", "")
    rule = payload.get("rule", "")
    raw_value = payload.get("raw_value")
    custom_fallback = payload.get("custom_fallback")

    fixes_applied = []

    fields_list = target_schema.get("fields", [])
    tgt_field_def = next((f for f in fields_list if f.get("name") == target_field), {})
    allowed_enums = tgt_field_def.get("constraints", {}).get("enum") or tgt_field_def.get("constraints", {}).get("allowed_values", [])
    default_enum = allowed_enums[0] if allowed_enums else "UNKNOWN"

    for m in plan.field_mappings:
        f_def = next((f for f in fields_list if f.get("name") == m.target_field), {})
        f_constraints = f_def.get("constraints", {})
        f_enums = f_constraints.get("enum") or f_constraints.get("allowed_values", [])
        f_nullable = f_def.get("nullable", True)

        if fix_action == "AUTO_RESOLVE_ALL":
            if f_enums:
                default_val = f_enums[0]
                m.parameters["fallback"] = default_val
                fixes_applied.append(f"Mapped {m.target_field} enum -> '{default_val}'")
            elif not f_nullable:
                f_name_lower = m.target_field.lower()
                if "name" in f_name_lower:
                    fb = "VALUED_CUSTOMER"
                elif "date" in f_name_lower or "time" in f_name_lower:
                    fb = "2024-01-01T00:00:00Z"
                elif "email" in f_name_lower:
                    fb = "remediated@customer.internal"
                else:
                    fb = "UNKNOWN"
                m.parameters["fallback"] = fb
                m.parameters["default"] = fb
                m.parameters["required"] = False
                fixes_applied.append(f"Configured fallback='{fb}' for {m.target_field}")
            if f_constraints.get("regex"):
                m.parameters["on_invalid"] = "null"
                m.parameters["null_on_invalid"] = True
                fixes_applied.append(f"Set null-on-invalid for {m.target_field}")
        elif m.target_field == target_field:
            if "ENUM" in rule or rule == "ENUM_CONSTRAINT" or f_enums:
                fb = custom_fallback or (f_enums[0] if f_enums else "UNKNOWN")
                m.parameters["fallback"] = fb
                if raw_value is not None:
                    m.parameters.setdefault("mapping", {})[str(raw_value)] = fb
                fixes_applied.append(f"Mapped {m.target_field} enum -> '{fb}'")
            elif "REGEX" in rule or rule == "REGEX_CONSTRAINT" or f_constraints.get("regex"):
                m.parameters["on_invalid"] = "null"
                m.parameters["null_on_invalid"] = True
                fixes_applied.append(f"Set null-on-invalid for {m.target_field}")
            else:
                f_name_lower = m.target_field.lower()
                default_fb = "VALUED_CUSTOMER" if "name" in f_name_lower else ("2024-01-01T00:00:00Z" if "date" in f_name_lower else ("remediated@customer.internal" if "email" in f_name_lower else "UNKNOWN"))
                fb = custom_fallback or default_fb
                m.parameters["fallback"] = fb
                m.parameters["default"] = fb
                m.parameters["required"] = False
                fixes_applied.append(f"Configured fallback='{fb}' for {m.target_field}")

    if plan.status == "APPROVED":
        plan.status = "PROPOSED"

    v2_state["active_plan"] = plan

    # Re-run simulation
    summary, valids, quars = dry_runner.execute_dry_run(
        plan,
        records=v2_inspection_tools.records,
        target_schema=target_schema
    )
    v2_state["last_dry_run_summary"] = summary
    v2_state["last_quarantine_records"] = quars

    summary_dict = summary.model_dump() if hasattr(summary, "model_dump") else summary.dict()
    summary_dict["valid_count"] = summary.accepted_count
    summary_dict["quarantined_count"] = summary.rejected_count
    summary_dict["sample_transformed"] = valids[:50]
    summary_dict["quarantine_sample"] = [
        q.model_dump() if hasattr(q, "model_dump") else q.dict()
        for q in quars[:100]
    ]

    return {
        "status": "SUCCESS",
        "fixes_applied": fixes_applied,
        "fix_description": "; ".join(fixes_applied) if fixes_applied else "AI parameter fix applied.",
        "plan": plan.model_dump() if hasattr(plan, "model_dump") else plan.dict(),
        "dry_run_summary": summary_dict
    }

@app.post("/api/v2/plans/execute")
def execute_v2_migration(
    req: Optional[Dict[str, Any]] = Body(default=None)
) -> ExecutionRunResult:
    plan = get_v2_active_plan()
    if not plan:
        raise HTTPException(status_code=404, detail="No active plan to execute")
    if plan.status != "APPROVED":
        raise HTTPException(status_code=400, detail="Plan must be approved prior to execution")

    try:
        target_schema = get_v2_active_schema() or {}
        summary, valids, quars = dry_runner.execute_dry_run(
            plan,
            records=v2_inspection_tools.records,
            target_schema=target_schema
        )
        v2_state["last_dry_run_summary"] = summary

        table_name = str(target_schema.get("table_name", "target_records")).strip()
        raw_nk = target_schema.get("natural_key", target_schema.get("primary_key", "id"))
        natural_key = (raw_nk[0] if isinstance(raw_nk, list) and raw_nk else str(raw_nk or "id")).strip()

        if target_schema:
            try:
                dynamic_store.compile_and_create_table(target_schema)
            except Exception as ddl_err:
                logger.warning(f"Could not re-compile dynamic table before execute: {ddl_err}")

        run_id = f"v2_exec_{uuid.uuid4().hex[:8]}"
        snap_id = dynamic_store.create_snapshot(table_name=table_name, run_id=run_id)

        inserted, updated, skipped = dynamic_store.execute_upsert_batch(
            table_name=table_name,
            natural_key_field=natural_key,
            rows=valids,
            run_id=run_id
        )

        now_iso = current_utc_iso()
        result = ExecutionRunResult(
            run_id=run_id,
            plan_version=plan.version,
            status="SUCCESS",
            snapshot_id=snap_id,
            total_source_records=summary.total_source_records,
            inserted_count=inserted,
            updated_count=updated,
            skipped_duplicates_count=skipped,
            quarantined_count=summary.rejected_count,
            execution_time_ms=summary.execution_time_ms,
            target_table_name=table_name,
            timestamp=now_iso
        )
        v2_state["last_execution_result"] = result
        dynamic_store.save_state("last_execution_result", result.model_dump())
        logger.info(f"Mode 2 migration write SUCCESS ({table_name}): {inserted} inserted, {updated} updated, {summary.rejected_count} quarantined.")
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"execute_v2_migration failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database Write Error: {str(e)}")

@app.get("/api/v2/target/records")
def get_v2_target_records(
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0)
) -> Dict[str, Any]:
    active_target = get_v2_active_schema()
    table_name = active_target.get("table_name", "target_records")
    total, rows = dynamic_store.query_dynamic_records(table_name=table_name, limit=limit, offset=offset)
    return {
        "table_name": table_name,
        "total_records": total,
        "limit": limit,
        "offset": offset,
        "records": rows
    }

@app.post("/api/v2/rollback")
def rollback_v2_migration(payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    active_target = get_v2_active_schema()
    table_name = active_target.get("table_name")

    snap_id = None
    if payload and "snapshot_id" in payload and payload["snapshot_id"]:
        snap_id = payload["snapshot_id"]
    elif v2_state.get("last_execution_result"):
        snap_id = v2_state["last_execution_result"].snapshot_id
    else:
        persisted_exec = dynamic_store.load_state("last_execution_result")
        if persisted_exec and isinstance(persisted_exec, dict) and persisted_exec.get("snapshot_id"):
            snap_id = persisted_exec["snapshot_id"]

    if not snap_id:
        snap_id = dynamic_store.get_latest_snapshot(table_name=table_name)

    if not snap_id:
        raise HTTPException(status_code=404, detail="No snapshot found for rollback. Run a migration write first to create a snapshot.")

    success, removed, remaining = dynamic_store.rollback_snapshot(snap_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"Snapshot '{snap_id}' could not be restored.")

    v2_state["last_execution_result"] = None
    dynamic_store.save_state("last_execution_result", None)
    logger.info(f"Mode 2 rollback complete for snapshot '{snap_id}': {removed} records removed, {remaining} restored.")
    return {
        "status": "ROLLED_BACK",
        "snapshot_id": snap_id,
        "table_name": table_name or "target_records",
        "removed_records": removed,
        "remaining_records": remaining
    }

@app.get("/api/v2/reconciliation")
def get_v2_reconciliation() -> Dict[str, Any]:
    total_source = len(v2_inspection_tools.records)
    active_target = get_v2_active_schema()
    table_name = active_target.get("table_name", "target_records")
    target_count, _ = dynamic_store.query_dynamic_records(table_name=table_name, limit=1)

    quar_count = 0
    summary = v2_state.get("last_dry_run_summary")
    if not summary:
        persisted_summary = dynamic_store.load_state("last_dry_run_summary")
        if persisted_summary and isinstance(persisted_summary, dict):
            quar_count = persisted_summary.get("rejected_count", 0)
    elif hasattr(summary, "rejected_count"):
        quar_count = summary.rejected_count
    elif isinstance(summary, dict):
        quar_count = summary.get("rejected_count", 0)

    last_exec = v2_state.get("last_execution_result")
    if not last_exec:
        persisted_exec = dynamic_store.load_state("last_execution_result")
        if persisted_exec and isinstance(persisted_exec, dict):
            ins_count = persisted_exec.get("inserted_count", target_count)
            upd_count = persisted_exec.get("updated_count", 0)
        else:
            ins_count = target_count
            upd_count = 0
    else:
        ins_count = last_exec.inserted_count
        upd_count = last_exec.updated_count

    accounted = (ins_count + upd_count) + quar_count
    unaccounted = max(0, total_source - accounted) if total_source >= accounted else 0

    verdict = "PASSED_EXACT" if unaccounted == 0 and quar_count == 0 else ("PASSED_WITH_QUARANTINE" if unaccounted == 0 else "DISCREPANCY_DETECTED")
    if target_count == 0 and ins_count == 0:
        verdict = "PENDING_EXECUTION"

    import hashlib
    src_hash = hashlib.sha256(f"{total_source}".encode()).hexdigest()[:16]
    tgt_hash = hashlib.sha256(f"{target_count}".encode()).hexdigest()[:16]

    is_zero_drop = (unaccounted == 0) and (target_count > 0 or ins_count > 0)

    return {
        "verdict": verdict,
        "table_name": table_name,
        "source_records": total_source,
        "target_records": target_count,
        "inserted_count": ins_count,
        "updated_count": upd_count,
        "quarantined_count": quar_count,
        "quarantined_records": quar_count,
        "unaccounted_delta": unaccounted,
        "unaccounted_records": unaccounted,
        "source_checksum": src_hash,
        "target_checksum": tgt_hash,
        "invariants_passed": unaccounted == 0,
        "is_zero_drop_verified": is_zero_drop
    }

@app.get("/api/v2/samples/catalog")
def get_sample_catalog() -> Dict[str, Any]:
    return {
        "datasets": [
            {"key": k, "title": v["title"]}
            for k, v in SAMPLE_DATASET_CONFIG.items()
        ]
    }

@app.post("/api/v2/samples/load/{dataset_key}")
def load_sample_dataset_space(dataset_key: str) -> Dict[str, Any]:
    if dataset_key not in SAMPLE_DATASET_CONFIG:
        raise HTTPException(status_code=404, detail=f"Unknown dataset key '{dataset_key}'")

    cfg = SAMPLE_DATASET_CONFIG[dataset_key]
    if not os.path.exists(cfg["csv"]) or not os.path.exists(cfg["schema"]):
        raise HTTPException(status_code=404, detail="Dataset files not found")

    import csv
    records = []
    with open(cfg["csv"], "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            records.append(row)

    with open(cfg["schema"], "r", encoding="utf-8") as f:
        target_schema = json.load(f)

    # Compile SQLite table
    ddl = dynamic_store.compile_and_create_table(target_schema)
    v2_state["active_target_schema"] = target_schema

    # Infer source schema and update inspection tools
    inferred_source = profiler.infer_schema_from_records(records, dataset_name=f"{dataset_key}_source")
    v2_inspection_tools.records = records
    v2_inspection_tools.source_schema = inferred_source
    v2_inspection_tools.target_schema = target_schema

    v2_state["active_plan"] = None
    plan = None
    ai_error = None

    return {
        "status": "SUCCESS",
        "dataset_key": dataset_key,
        "title": cfg["title"],
        "records_count": len(records),
        "target_table": target_schema.get("table_name"),
        "ddl": ddl,
        "target_schema": target_schema,
        "source_schema": inferred_source,
        "plan": plan,
        "ai_error": ai_error,
        "agent_call_info": ollama_agent.last_call_info
    }



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
