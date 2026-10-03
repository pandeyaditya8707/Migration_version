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
        records: Optional[List[Dict[str, Any]]] = None,
        target_schema: Optional[Dict[str, Any]] = None
    ) -> Tuple[DryRunSummary, List[Dict[str, Any]], List[QuarantineRecord]]:
        """Executes a complete deterministic simulation across all bounded records.
        Returns (summary, valid_target_records, quarantined_records)."""
        start_time = time.perf_counter()
        records_to_process = records if records is not None else load_sample_records()
        run_id = f"dry_run_{uuid.uuid4().hex[:8]}"

        active_schema = target_schema or self.target_schema
        active_field_map = {f["name"]: f for f in active_schema.get("fields", [])} if active_schema else self.target_field_map

        valid_records: List[Dict[str, Any]] = []
        quarantined_records: List[QuarantineRecord] = []
        field_error_breakdown: Dict[str, int] = {}

        def _get_val(rec: Dict[str, Any], col: str) -> Any:
            if col in rec:
                return rec[col]
            norm = col.lower().replace("-", "_").strip()
            for k, v in rec.items():
                if k.lower().replace("-", "_").strip() == norm:
                    return v
            return None

        for row_idx, src_rec in enumerate(records_to_process):
            row_errors: List[FieldErrorEvidence] = []
            target_row: Dict[str, Any] = {}
            natural_key = (
                src_rec.get("legacy_account_id") or
                src_rec.get("id") or
                src_rec.get("account_id") or
                src_rec.get("inv_num") or
                src_rec.get("order_num") or
                src_rec.get("encounter_id") or
                src_rec.get("sku_code") or
                src_rec.get("user_id") or
                f"rec_{row_idx}"
            )

            for mapping in plan.field_mappings:
                tgt_field = mapping.target_field
                tgt_def = active_field_map.get(tgt_field, {})
                tgt_nullable = tgt_def.get("nullable", True)
                tgt_constraints = tgt_def.get("constraints") or {}

                # Determine source value(s)
                if len(mapping.source_fields) == 1:
                    src_val = _get_val(src_rec, mapping.source_fields[0])
                elif len(mapping.source_fields) > 1:
                    src_val = [_get_val(src_rec, f) for f in mapping.source_fields]
                else:
                    src_val = None

                # For UUID generation, if source field was null/unmapped, fall back to natural key
                if mapping.transformation == "UUID_V5_FROM_KEY" and (src_val is None or not str(src_val).strip()):
                    src_val = natural_key

                # Apply transformation
                transformed_val, transform_err = apply_transformation(
                    mapping.transformation,
                    src_val,
                    mapping.parameters
                )

                if transform_err:
                    src_label = ', '.join(mapping.source_fields) if mapping.source_fields else 'input'
                    sugg = f"Configure '{mapping.transformation}' with fallback parameter or cleanse source field '{src_label}'."
                    row_errors.append(FieldErrorEvidence(
                        field=tgt_field,
                        rule=mapping.transformation,
                        severity="CRITICAL",
                        error_message=transform_err,
                        raw_value=src_val,
                        ai_suggestion=sugg
                    ))
                    field_error_breakdown[tgt_field] = field_error_breakdown.get(tgt_field, 0) + 1
                    continue


                # Target Constraint Checks
                # 1. Nullability check
                if not tgt_nullable and (transformed_val is None or str(transformed_val).strip() == ""):
                    fb = mapping.parameters.get("default") or mapping.parameters.get("fallback")
                    if fb is not None and str(fb).strip() != "":
                        transformed_val = fb
                    else:
                        sugg = f"Target field '{tgt_field}' is NOT NULL. Configure fallback substitution (e.g. 'UNKNOWN' or 'VALUED_CUSTOMER') or substitute default value."
                        row_errors.append(FieldErrorEvidence(
                            field=tgt_field,
                            rule="NOT_NULL_CONSTRAINT",
                            severity="CRITICAL",
                            error_message=f"Target field '{tgt_field}' is NOT NULL, but evaluated to null/empty",
                            raw_value=src_val,
                            ai_suggestion=sugg
                        ))
                        field_error_breakdown[tgt_field] = field_error_breakdown.get(tgt_field, 0) + 1
                        continue

                # 2. Enum constraint check
                allowed_enums = tgt_constraints.get("enum")
                if allowed_enums and transformed_val is not None:
                    if transformed_val not in allowed_enums:
                        enum_fb = mapping.parameters.get("fallback") or mapping.parameters.get("fallback_enum")
                        if enum_fb and enum_fb in allowed_enums:
                            transformed_val = enum_fb
                        else:
                            default_fallback = allowed_enums[0] if allowed_enums else "UNKNOWN"
                            sugg = f"Value '{transformed_val}' is not in target enum list. Update ENUM_LOOKUP mapping dictionary or set fallback='{default_fallback}'."
                            row_errors.append(FieldErrorEvidence(
                                field=tgt_field,
                                rule="ENUM_CONSTRAINT",
                                severity="CRITICAL",
                                error_message=f"Value '{transformed_val}' is not in allowed target enum list: {allowed_enums}",
                                raw_value=src_val,
                                ai_suggestion=sugg
                            ))
                            field_error_breakdown[tgt_field] = field_error_breakdown.get(tgt_field, 0) + 1
                            continue

                # 3. Regex constraint check
                regex_pattern = tgt_constraints.get("regex")
                if regex_pattern and transformed_val is not None:
                    if not re.search(regex_pattern, str(transformed_val)):
                        if (mapping.parameters.get("on_invalid") == "null" or mapping.parameters.get("null_on_invalid")) and tgt_nullable:
                            transformed_val = None
                        else:
                            sugg = f"Value '{transformed_val}' fails regex '{regex_pattern}'. Apply pre-formatting transform (e.g. phone E.164 normalization) or null-substitution if optional."
                            row_errors.append(FieldErrorEvidence(
                                field=tgt_field,
                                rule="REGEX_CONSTRAINT",
                                severity="CRITICAL",
                                error_message=f"Value '{transformed_val}' violates target regex pattern: '{regex_pattern}'",
                                raw_value=src_val,
                                ai_suggestion=sugg
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
                    errors=row_errors,
                    ai_remediation_summary=row_errors[0].ai_suggestion if row_errors else "Review source record schema"
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
