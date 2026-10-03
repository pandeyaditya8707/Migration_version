from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


def current_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ============================================================================
# Schema & Field Definitions
# ============================================================================


class FieldDefinition(BaseModel):
    name: str
    data_type: str
    nullable: bool = True
    description: str | None = None
    constraints: dict[str, Any] | None = None  # e.g. {"regex": "^\\+...", "enum": ["ACTIVE", ...], "unique": True}


class DatasetSchema(BaseModel):
    schema_id: str
    name: str
    description: str
    fields: list[FieldDefinition]


# ============================================================================
# Transformation Rules
# ============================================================================


class TransformationRuleSpec(BaseModel):
    rule_id: str
    name: str
    description: str
    required_params: list[str] = Field(default_factory=list)
    optional_params: list[str] = Field(default_factory=list)
    example_input: str
    example_output: str


# ============================================================================
# Mapping & Plan Models
# ============================================================================


class FieldMapping(BaseModel):
    target_field: str
    source_fields: list[str] = Field(default_factory=list)
    transformation: str = "DIRECT_COPY"
    parameters: dict[str, Any] = Field(default_factory=dict)
    risk_level: Literal["LOW", "MEDIUM", "HIGH"] = "LOW"
    risk_rationale: str | None = None
    notes: str | None = None


class ClarificationQuestion(BaseModel):
    question_id: str
    field: str
    question: str
    options: list[str] = Field(default_factory=list)
    user_answer: str | None = None


class MigrationPlan(BaseModel):
    plan_id: str = "plan_v1"
    version: int = 1
    title: str = "Customer Data Migration Plan"
    description: str = "Generated migration plan from legacy customer CRM to modern platform"
    source_schema_id: str = "legacy_customers"
    target_schema_id: str = "modern_customers"
    field_mappings: list[FieldMapping]
    clarifications: list[ClarificationQuestion] = Field(default_factory=list)
    status: Literal["DRAFT", "PROPOSED", "APPROVED", "SUPERSEDED"] = "PROPOSED"
    approved_by: str | None = None
    approved_at: str | None = None
    approval_fingerprint: str | None = None
    created_at: str = Field(default_factory=current_utc_iso)
    updated_at: str = Field(default_factory=current_utc_iso)


# ============================================================================
# Dry-Run & Quarantine Models
# ============================================================================


class FieldErrorEvidence(BaseModel):
    field: str
    rule: str
    severity: Literal["WARNING", "CRITICAL"] = "CRITICAL"
    error_message: str
    raw_value: Any = None
    ai_suggestion: str | None = None


class QuarantineRecord(BaseModel):
    quarantine_id: str
    run_id: str
    source_row_index: int
    source_natural_key: str | None = None
    source_payload: dict[str, Any]
    errors: list[FieldErrorEvidence]
    ai_remediation_summary: str | None = None
    timestamp: str = Field(default_factory=current_utc_iso)


class DryRunSummary(BaseModel):
    run_id: str
    plan_version: int
    total_source_records: int
    transformed_count: int
    accepted_count: int
    rejected_count: int
    execution_time_ms: float
    field_error_breakdown: dict[str, int]
    quarantine_sample: list[QuarantineRecord] = Field(default_factory=list)
    timestamp: str = Field(default_factory=current_utc_iso)


# ============================================================================
# Execution & Rollback Models
# ============================================================================


class ExecutionRunRequest(BaseModel):
    plan_version: int
    executed_by: str = "Lead Data Engineer"
    allow_quarantine: bool = True


class ExecutionRunResult(BaseModel):
    run_id: str
    plan_version: int
    status: Literal["SUCCESS", "FAILED", "ROLLED_BACK"]
    total_source_records: int
    inserted_count: int
    updated_count: int
    skipped_duplicates_count: int
    quarantined_count: int
    execution_time_ms: float
    target_table_name: str
    snapshot_id: str
    timestamp: str = Field(default_factory=current_utc_iso)


class RollbackResult(BaseModel):
    run_id: str
    status: Literal["SUCCESS", "FAILED"]
    message: str
    records_removed: int
    target_records_remaining: int
    timestamp: str = Field(default_factory=current_utc_iso)


# ============================================================================
# Reconciliation Models
# ============================================================================


class ReconciliationReport(BaseModel):
    reconciliation_id: str
    run_id: str
    plan_version: int
    evaluated_at: str = Field(default_factory=current_utc_iso)
    verdict: Literal["PENDING_EXECUTION", "PASSED_EXACT", "PASSED_WITH_QUARANTINE", "DISCREPANCY_DETECTED"]
    accounting: dict[str, int]  # source_total, accepted, quarantined, unaccounted
    invariants_passed: bool
    duplicate_count: int
    source_checksum: str
    target_checksum: str
    aggregate_comparisons: dict[str, dict[str, Any]]
    details: list[str] = Field(default_factory=list)


# ============================================================================
# Audit Log Event
# ============================================================================


class AuditLogEvent(BaseModel):
    event_id: str
    event_type: Literal[
        "PLAN_CREATED",
        "PLAN_UPDATED",
        "PLAN_APPROVED",
        "DRY_RUN_EXECUTED",
        "MIGRATION_EXECUTED",
        "MIGRATION_RETRIED",
        "MIGRATION_ROLLED_BACK",
    ]
    actor: str
    details: dict[str, Any]
    timestamp: str = Field(default_factory=current_utc_iso)
