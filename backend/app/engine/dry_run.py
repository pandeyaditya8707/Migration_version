from __future__ import annotations

import re
import time
import uuid
from typing import Any

from ..models.schemas import (
    DryRunSummary,
    FieldErrorEvidence,
    MigrationPlan,
    QuarantineRecord,
)
from .profiler import load_sample_records, load_target_schema
from .transforms import apply_transformation


class DryRunEngine:
    """Simulates plan transformations deterministically without writing to the target store,
    capturing granular field-level error evidence for quarantined records."""

    def __init__(self, target_schema: dict[str, Any] | None = None):
        self.target_schema = target_schema or load_target_schema()
        self.target_field_map = {f["name"]: f for f in self.target_schema.get("fields", [])}

    @staticmethod
    def _validate_and_coerce_target_type(val: Any, target_type: str) -> tuple[Any, str | None]:
        """Validates that a transformed value conforms to the target schema type,
        coercing where safe or returning a descriptive validation error message."""
        if val is None:
            return None, None
        ttype = target_type.lower().strip()

        # Integer types
        if ttype in ("int", "integer", "bigint", "smallint"):
            if isinstance(val, bool):
                return int(val), None
            if isinstance(val, (int, float)):
                if isinstance(val, float) and not val.is_integer():
                    return val, f"Expected integer but got float with fraction: {val}"
                return int(val), None
            val_str = str(val).strip()
            if re.match(r"^-?\d+$", val_str):
                return int(val_str), None
            return val, f"Cannot coerce value '{val}' to INTEGER"

        # Float / Real / Decimal
        elif ttype in ("float", "real", "double", "numeric", "decimal", "currency"):
            if isinstance(val, (int, float)):
                return float(val), None
            val_str = str(val).replace("$", "").replace("€", "").replace("£", "").replace(",", "").strip()
            try:
                parsed = float(val_str)
                import math

                if math.isnan(parsed) or math.isinf(parsed):
                    return val, f"Invalid numeric float value: '{val}'"
                return parsed, None
            except ValueError:
                return val, f"Cannot coerce value '{val}' to REAL/FLOAT"

        # Boolean
        elif ttype in ("bool", "boolean"):
            if isinstance(val, bool):
                return val, None
            val_str = str(val).lower().strip()
            if val_str in ("true", "1", "yes", "y", "t"):
                return True, None
            elif val_str in ("false", "0", "no", "n", "f"):
                return False, None
            return val, f"Cannot coerce value '{val}' to BOOLEAN"

        # Date (ISO-8601 calendar validity: YYYY-MM-DD)
        elif ttype == "date":
            val_str = str(val).strip()
            if len(val_str) >= 10:
                val_date_part = val_str[:10]
                if re.match(r"^\d{4}-\d{2}-\d{2}$", val_date_part):
                    try:
                        from datetime import datetime

                        datetime.strptime(val_date_part, "%Y-%m-%d")
                        return val_date_part, None
                    except ValueError as e:
                        return val, f"Invalid calendar date '{val_date_part}': {e}"
            return val, f"Expected ISO-8601 date (YYYY-MM-DD), but got: '{val}'"

        # DateTime / Timestamp
        elif ttype in ("datetime", "timestamp"):
            val_str = str(val).strip()
            if re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", val_str):
                return val_str, None
            return val, f"Expected ISO-8601 timestamp, but got: '{val}'"

        # Default string or text
        return str(val) if not isinstance(val, (dict, list)) else val, None

    def execute_dry_run(
        self,
        plan: MigrationPlan,
        records: list[dict[str, Any]] | None = None,
        target_schema: dict[str, Any] | None = None,
    ) -> tuple[DryRunSummary, list[dict[str, Any]], list[QuarantineRecord]]:
        """Executes a complete deterministic simulation across all bounded records.
        Returns (summary, valid_target_records, quarantined_records)."""
        start_time = time.perf_counter()
        records_to_process = records if records is not None else load_sample_records()
        run_id = f"dry_run_{uuid.uuid4().hex[:8]}"

        active_schema = target_schema or self.target_schema
        active_field_map = (
            {f["name"]: f for f in active_schema.get("fields", [])} if active_schema else self.target_field_map
        )

        # Check for unmapped required target fields
        mapped_target_fields = {m.target_field for m in plan.field_mappings}
        unmapped_required_fields = [
            f["name"]
            for f in (active_schema.get("fields", []) if active_schema else [])
            if (not f.get("nullable", True)) and f["name"] not in mapped_target_fields and f.get("default") is None
        ]

        # Dynamic natural key resolution from active target schema or plan
        schema_nk = None
        if active_schema:
            schema_nk = active_schema.get("natural_key") or active_schema.get("primary_key")

        nk_target_name = (
            "natural_key" if "natural_key" in active_field_map else (schema_nk if isinstance(schema_nk, str) else "id")
        )
        nk_source_fields: list[str] = []
        for m in plan.field_mappings:
            if m.target_field == nk_target_name:
                nk_source_fields = m.source_fields
                break

        valid_records: list[dict[str, Any]] = []
        quarantined_records: list[QuarantineRecord] = []
        field_error_breakdown: dict[str, int] = {}

        def _get_val(rec: dict[str, Any], col: str) -> Any:
            if col in rec:
                return rec[col]
            norm = col.lower().replace("-", "_").strip()
            for k, v in rec.items():
                if k.lower().replace("-", "_").strip() == norm:
                    return v
            return None

        for row_idx, src_rec in enumerate(records_to_process):
            row_errors: list[FieldErrorEvidence] = []
            target_row: dict[str, Any] = {}

            # Dynamic natural key extraction
            if nk_source_fields:
                nk_parts = [
                    str(_get_val(src_rec, sf) or "").strip()
                    for sf in nk_source_fields
                    if _get_val(src_rec, sf) is not None
                ]
                natural_key = "_".join(nk_parts) if nk_parts else None
            elif schema_nk:
                if isinstance(schema_nk, list):
                    nk_parts = [str(_get_val(src_rec, sf) or "").strip() for sf in schema_nk]
                    natural_key = "_".join(nk_parts)
                else:
                    natural_key = str(_get_val(src_rec, schema_nk) or "").strip()
            else:
                natural_key = None

            if not natural_key:
                for c_cand in [
                    "legacy_account_id",
                    "id",
                    "account_id",
                    "inv_num",
                    "order_num",
                    "encounter_id",
                    "sku_code",
                    "user_id",
                ]:
                    v_cand = _get_val(src_rec, c_cand)
                    if v_cand is not None and str(v_cand).strip():
                        natural_key = str(v_cand).strip()
                        break
            if not natural_key:
                natural_key = f"rec_{row_idx}"

            # Pre-check: If required target fields are unmapped in plan, flag error immediately
            for unmapped_col in unmapped_required_fields:
                sugg = f"Add a mapping in MigrationPlan for required target field '{unmapped_col}'."
                row_errors.append(
                    FieldErrorEvidence(
                        field=unmapped_col,
                        rule="REQUIRED_FIELD_UNMAPPED",
                        severity="CRITICAL",
                        error_message=f"Required target field '{unmapped_col}' (NOT NULL) has no mapping in MigrationPlan.",
                        raw_value=None,
                        ai_suggestion=sugg,
                    )
                )
                field_error_breakdown[unmapped_col] = field_error_breakdown.get(unmapped_col, 0) + 1

            for mapping in plan.field_mappings:
                tgt_field = mapping.target_field
                tgt_def = active_field_map.get(tgt_field, {})
                tgt_nullable = tgt_def.get("nullable", True)
                tgt_constraints = tgt_def.get("constraints") or {}
                tgt_type = tgt_def.get("data_type") or tgt_def.get("type", "string")

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
                    mapping.transformation, src_val, mapping.parameters
                )

                if transform_err:
                    src_label = ", ".join(mapping.source_fields) if mapping.source_fields else "input"
                    sugg = f"Configure '{mapping.transformation}' with fallback parameter or cleanse source field '{src_label}'."
                    row_errors.append(
                        FieldErrorEvidence(
                            field=tgt_field,
                            rule=mapping.transformation,
                            severity="CRITICAL",
                            error_message=transform_err,
                            raw_value=src_val,
                            ai_suggestion=sugg,
                        )
                    )
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
                        row_errors.append(
                            FieldErrorEvidence(
                                field=tgt_field,
                                rule="NOT_NULL_CONSTRAINT",
                                severity="CRITICAL",
                                error_message=f"Target field '{tgt_field}' is NOT NULL, but evaluated to null/empty",
                                raw_value=src_val,
                                ai_suggestion=sugg,
                            )
                        )
                        field_error_breakdown[tgt_field] = field_error_breakdown.get(tgt_field, 0) + 1
                        continue

                # 2. Enum constraint check
                allowed_enums = tgt_constraints.get("enum") or tgt_constraints.get("allowed_values")
                if allowed_enums and transformed_val is not None:
                    if transformed_val not in allowed_enums:
                        enum_fb = mapping.parameters.get("fallback") or mapping.parameters.get("fallback_enum")
                        if enum_fb and enum_fb in allowed_enums:
                            transformed_val = enum_fb
                        else:
                            default_fallback = allowed_enums[0] if allowed_enums else "UNKNOWN"
                            sugg = f"Value '{transformed_val}' is not in target enum list. Update ENUM_LOOKUP mapping dictionary or set fallback='{default_fallback}'."
                            row_errors.append(
                                FieldErrorEvidence(
                                    field=tgt_field,
                                    rule="ENUM_CONSTRAINT",
                                    severity="CRITICAL",
                                    error_message=f"Value '{transformed_val}' is not in allowed target enum list: {allowed_enums}",
                                    raw_value=src_val,
                                    ai_suggestion=sugg,
                                )
                            )
                            field_error_breakdown[tgt_field] = field_error_breakdown.get(tgt_field, 0) + 1
                            continue

                # 3. Regex constraint check
                regex_pattern = tgt_constraints.get("regex") or tgt_constraints.get("pattern")
                if regex_pattern and transformed_val is not None:
                    try:
                        matches = bool(re.search(regex_pattern, str(transformed_val)))
                    except Exception:
                        matches = False
                    if not matches:
                        if (
                            mapping.parameters.get("on_invalid") == "null" or mapping.parameters.get("null_on_invalid")
                        ) and tgt_nullable:
                            transformed_val = None
                        else:
                            sugg = f"Value '{transformed_val}' fails regex '{regex_pattern}'. Apply pre-formatting transform (e.g. phone E.164 normalization) or null-substitution if optional."
                            row_errors.append(
                                FieldErrorEvidence(
                                    field=tgt_field,
                                    rule="REGEX_CONSTRAINT",
                                    severity="CRITICAL",
                                    error_message=f"Value '{transformed_val}' violates target regex pattern: '{regex_pattern}'",
                                    raw_value=src_val,
                                    ai_suggestion=sugg,
                                )
                            )
                            field_error_breakdown[tgt_field] = field_error_breakdown.get(tgt_field, 0) + 1
                            continue

                # 4. Target Data-Type Validation & Safe Coercion
                if transformed_val is not None:
                    coerced_val, type_err = self._validate_and_coerce_target_type(transformed_val, tgt_type)
                    if type_err:
                        sugg = f"Configure a transformation rule to convert source value to '{tgt_type}'."
                        row_errors.append(
                            FieldErrorEvidence(
                                field=tgt_field,
                                rule="TYPE_INCOMPATIBILITY",
                                severity="CRITICAL",
                                error_message=type_err,
                                raw_value=src_val,
                                ai_suggestion=sugg,
                            )
                        )
                        field_error_breakdown[tgt_field] = field_error_breakdown.get(tgt_field, 0) + 1
                        continue
                    transformed_val = coerced_val

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
                    ai_remediation_summary=row_errors[0].ai_suggestion if row_errors else "Review source record schema",
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
            quarantine_sample=quarantined_records[:200],  # Comprehensive preview up to 200
        )

        return summary, valid_records, quarantined_records
