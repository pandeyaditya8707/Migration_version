from __future__ import annotations
import time
import uuid
import re
from typing import Any, Dict, List, Optional, Tuple
from ..models.schemas import (
    MigrationPlan,
    DryRunSummary,
    QuarantineRecord,
    FieldErrorEvidence,
    DatasetSchema
)
from .transforms import apply_transformation
from .profiler import load_target_schema, load_sample_records

class DryRunEngine:
    """Simulates plan transformations deterministically without writing to the target store,
    capturing granular field-level error evidence for quarantined records."""

    def __init__(self, target_schema: Optional[Dict[str, Any]] = None):
        self.target_schema = target_schema or load_target_schema()
        self.target_field_map = {f["name"]: f for f in self.target_schema.get("fields", [])}

    def execute_dry_run(
        self,
        plan: MigrationPlan,
        records: Optional[List[Dict[str, Any]]] = None
    ) -> Tuple[DryRunSummary, List[Dict[str, Any]], List[QuarantineRecord]]:
        """Executes a complete deterministic simulation across all bounded records.
        Returns (summary, valid_target_records, quarantined_records)."""
        start_time = time.perf_counter()
        records_to_process = records if records is not None else load_sample_records()
        run_id = f"dry_run_{uuid.uuid4().hex[:8]}"

        valid_records: List[Dict[str, Any]] = []
        quarantined_records: List[QuarantineRecord] = []
        field_error_breakdown: Dict[str, int] = {}

        for row_idx, src_rec in enumerate(records_to_process):
            row_errors: List[FieldErrorEvidence] = []
            target_row: Dict[str, Any] = {}
            natural_key = src_rec.get("legacy_account_id") or src_rec.get("id") or src_rec.get("account_id") or src_rec.get("user_id") or f"rec_{row_idx}"

            for mapping in plan.field_mappings:
                tgt_field = mapping.target_field
                tgt_def = self.target_field_map.get(tgt_field, {})
                tgt_nullable = tgt_def.get("nullable", True)
                tgt_constraints = tgt_def.get("constraints") or {}

                # Determine source value(s)
                if len(mapping.source_fields) == 1:
                    src_val = src_rec.get(mapping.source_fields[0])
                elif len(mapping.source_fields) > 1:
                    src_val = [src_rec.get(f) for f in mapping.source_fields]
                else:
                    src_val = None

                # Apply transformation
                transformed_val, transform_err = apply_transformation(
                    mapping.transformation,
                    src_val,
                    mapping.parameters
                )

                if transform_err:
                    row_errors.append(FieldErrorEvidence(
                        field=tgt_field,
                        rule=mapping.transformation,
                        severity="CRITICAL",
                        error_message=transform_err,
                        raw_value=src_val
                    ))
                    field_error_breakdown[tgt_field] = field_error_breakdown.get(tgt_field, 0) + 1
                    continue

                # Target Constraint Checks
                # 1. Nullability check
                if not tgt_nullable and (transformed_val is None or str(transformed_val).strip() == ""):
                    row_errors.append(FieldErrorEvidence(
                        field=tgt_field,
                        rule="NOT_NULL_CONSTRAINT",
                        severity="CRITICAL",
                        error_message=f"Target field '{tgt_field}' is NOT NULL, but evaluated to null/empty",
                        raw_value=src_val
                    ))
                    field_error_breakdown[tgt_field] = field_error_breakdown.get(tgt_field, 0) + 1
                    continue

                # 2. Enum constraint check
                allowed_enums = tgt_constraints.get("enum")
                if allowed_enums and transformed_val is not None:
                    if transformed_val not in allowed_enums:
                        row_errors.append(FieldErrorEvidence(
                            field=tgt_field,
                            rule="ENUM_CONSTRAINT",
                            severity="CRITICAL",
                            error_message=f"Value '{transformed_val}' is not in allowed target enum list: {allowed_enums}",
                            raw_value=src_val
                        ))
                        field_error_breakdown[tgt_field] = field_error_breakdown.get(tgt_field, 0) + 1
                        continue

                # 3. Regex constraint check
                regex_pattern = tgt_constraints.get("regex")
                if regex_pattern and transformed_val is not None:
                    if not re.search(regex_pattern, str(transformed_val)):
                        row_errors.append(FieldErrorEvidence(
                            field=tgt_field,
                            rule="REGEX_CONSTRAINT",
                            severity="CRITICAL",
                            error_message=f"Value '{transformed_val}' violates target regex pattern: '{regex_pattern}'",
                            raw_value=src_val
                        ))
                        field_error_breakdown[tgt_field] = field_error_breakdown.get(tgt_field, 0) + 1
                        continue

                # Valid field evaluation
                target_row[tgt_field] = transformed_val

            # Record partitioning
            if row_errors:
                q_rec = QuarantineRecord(
                    quarantine_id=f"q_{run_id}_{row_idx:04d}",
                    run_id=run_id,
                    source_row_index=row_idx,
                    source_natural_key=str(natural_key) if natural_key else None,
                    source_payload=src_rec,
                    errors=row_errors
                )
                quarantined_records.append(q_rec)
            else:
                valid_records.append(target_row)

        elapsed_ms = (time.perf_counter() - start_time) * 1000.0

        summary = DryRunSummary(
            run_id=run_id,
            plan_version=plan.version,
            total_source_records=len(records_to_process),
            transformed_count=len(records_to_process),
            accepted_count=len(valid_records),
            rejected_count=len(quarantined_records),
            execution_time_ms=round(elapsed_ms, 2),
            field_error_breakdown=field_error_breakdown,
            quarantine_sample=quarantined_records[:200]  # Comprehensive preview up to 200
        )

        return summary, valid_records, quarantined_records
