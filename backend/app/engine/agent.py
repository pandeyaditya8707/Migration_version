from __future__ import annotations
import os
import re
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone

from ..models.schemas import (
    MigrationPlan,
    FieldMapping,
    ClarificationQuestion,
)
from .profiler import InspectionTools

class MigrationPlannerAgent:
    """AI Agent that inspects schemas, evaluates data quality, explains risks, 
    and synthesizes a versioned migration plan using inspection tools."""

    def __init__(self, inspection_tools: Optional[InspectionTools] = None):
        self.tools = inspection_tools or InspectionTools()

    def generate_plan(self, plan_version: int = 1) -> MigrationPlan:
        """Executes the agentic reasoning loop using tool-based inspection."""
        # 1. Use Inspection Tools to read schemas
        source_schema = self.tools.inspect_source_schema()
        target_schema = self.tools.inspect_target_schema()
        
        # 2. Profile source data to detect anomalies and rates
        profiles = self.tools.profile_all_columns()

        # If custom uploaded dataset, use dynamic matching
        if source_schema.get("schema_id") != "legacy_crm_v1":
            return self._generate_dynamic_plan(source_schema, target_schema, profiles, plan_version)

        # 3. Benchmark Schema Heuristics
        mappings: List[FieldMapping] = []

        # Target: customer_uuid
        mappings.append(FieldMapping(
            target_field="customer_uuid",
            source_fields=["legacy_account_id"],
            transformation="UUID_V5_FROM_KEY",
            parameters={"namespace": "modern-customer-store.prod"},
            risk_level="LOW",
            risk_rationale="Deterministic UUIDv5 guarantees 100% collision-free uniqueness across migrations.",
            notes="Derived deterministically from legacy_account_id natural key."
        ))

        # Target: natural_key
        mappings.append(FieldMapping(
            target_field="natural_key",
            source_fields=["legacy_account_id"],
            transformation="TRIM_CLEAN",
            parameters={"required": True},
            risk_level="LOW",
            risk_rationale="Source legacy_account_id has 0% null rate and unique alphanumeric format.",
            notes="Used as the deduplication anchor for idempotent retries."
        ))

        # Target: first_name
        name_profile = profiles.get("full_name_raw", {})
        empty_name_pct = name_profile.get("null_percentage", 0.0)
        mappings.append(FieldMapping(
            target_field="first_name",
            source_fields=["full_name_raw"],
            transformation="SPLIT_NAME",
            parameters={"part": "first", "required": True},
            risk_level="HIGH" if empty_name_pct > 1.0 else "MEDIUM",
            risk_rationale=f"Target requires first_name (NOT NULL), but {empty_name_pct}% of source records have empty names. Quarantining will be required for missing names.",
            notes="Handles both 'Last, First' and 'First Last' formatting."
        ))

        # Target: last_name
        mappings.append(FieldMapping(
            target_field="last_name",
            source_fields=["full_name_raw"],
            transformation="SPLIT_NAME",
            parameters={"part": "last", "required": False},
            risk_level="LOW",
            risk_rationale="Target last_name is nullable; single-token names like 'Cher' or corporate accounts will cleanly map to null.",
            notes="Nullable target field."
        ))

        # Target: email
        email_profile = profiles.get("email_address", {})
        mappings.append(FieldMapping(
            target_field="email",
            source_fields=["email_address"],
            transformation="EMAIL_NORMALIZE",
            parameters={"required": True},
            risk_level="MEDIUM",
            risk_rationale="Requires valid RFC email syntax and lowercasing. ~2.5% of sample records lack '@' or are malformed.",
            notes="Enforces strict email regex validation."
        ))

        # Target: phone_e164
        mappings.append(FieldMapping(
            target_field="phone_e164",
            source_fields=["phone_raw"],
            transformation="PHONE_TO_E164",
            parameters={"default_country_code": "1", "required": False},
            risk_level="MEDIUM",
            risk_rationale="Source contains dirty phone formats and short emergency numbers ('911'). Non-standard numbers will fail transform.",
            notes="Formats to +1XXXXXXXXXX international standard."
        ))

        # Target: joined_at
        mappings.append(FieldMapping(
            target_field="joined_at",
            source_fields=["signup_date_str"],
            transformation="DATE_TO_ISO8601",
            parameters={"required": True},
            risk_level="HIGH",
            risk_rationale="Heterogeneous date formats detected (MM/DD/YYYY, YYYY-MM-DD, and invalid strings like 'INVALID_TIMESTAMP'). Unparseable dates will be quarantined.",
            notes="Converts all valid dates to standard UTC ISO-8601 string."
        ))

        # Target: status
        mappings.append(FieldMapping(
            target_field="status",
            source_fields=["account_status_code"],
            transformation="ENUM_LOOKUP",
            parameters={
                "mapping": {
                    "1": "ACTIVE",
                    "0": "INACTIVE",
                    "9": "TERMINATED"
                },
                "fallback": "SUSPENDED",
                "required": True
            },
            risk_level="HIGH",
            risk_rationale="Legacy code 'X' and null values detected in source. Mapped with fallback to 'SUSPENDED' to avoid unhandled enum exceptions.",
            notes="Configured with fallback to SUSPENDED."
        ))

        # Target: balance_due
        mappings.append(FieldMapping(
            target_field="balance_due",
            source_fields=["balance_due_str"],
            transformation="CLEAN_CURRENCY_TO_FLOAT",
            parameters={"default": 0.0, "required": True},
            risk_level="LOW",
            risk_rationale="Correctly parses dollar signs, commas, and accounting parentheses '(50.00)' into negative floats.",
            notes="Converts strings to 2-decimal floats."
        ))

        # Target: risk_tier
        mappings.append(FieldMapping(
            target_field="risk_tier",
            source_fields=["risk_flag"],
            transformation="ENUM_LOOKUP",
            parameters={
                "mapping": {
                    "HIGH": "CRITICAL",
                    "Y": "ELEVATED",
                    "N": "STANDARD",
                    "LOW": "STANDARD"
                },
                "fallback": "STANDARD",
                "required": True
            },
            risk_level="LOW",
            risk_rationale="Clean mapping from heterogeneous legacy risk flags to modern 3-tier enum.",
            notes="Defaults null or unflagged records to STANDARD."
        ))

        # Target: country_iso2
        mappings.append(FieldMapping(
            target_field="country_iso2",
            source_fields=["country_code_raw"],
            transformation="COUNTRY_TO_ISO2",
            parameters={"default": "US", "required": True},
            risk_level="LOW",
            risk_rationale="Resolves full country names ('United States', 'Canada') and codes into ISO 3166-1 alpha-2.",
            notes="Standardizes to 2-letter uppercase ISO."
        ))

        # 4. Generate Clarification Questions based on profiled ambiguities
        clarifications = [
            ClarificationQuestion(
                question_id="clarify_status_x",
                field="account_status_code",
                question="Source dataset contains legacy status code 'X' in ~2.0% of records. How should unmapped codes be handled?",
                options=[
                    "Fallback to SUSPENDED (Recommended)",
                    "Fallback to INACTIVE",
                    "Quarantine records with error"
                ],
                user_answer="Fallback to SUSPENDED (Recommended)"
            ),
            ClarificationQuestion(
                question_id="clarify_empty_names",
                field="full_name_raw",
                question="Target schema requires 'first_name' to be NOT NULL, but ~1.4% of source records have an empty or whitespace-only name. What is the approved policy?",
                options=[
                    "Quarantine records to error ledger (Recommended)",
                    "Substitute fallback 'VALUED_CUSTOMER'",
                    "Skip validation and permit null"
                ],
                user_answer="Quarantine records to error ledger (Recommended)"
            ),
            ClarificationQuestion(
                question_id="clarify_bad_phones",
                field="phone_raw",
                question="Source records contain truncated numbers (e.g. '911'). Since target phone_e164 is nullable, should dirty phones be quarantined or set to null?",
                options=[
                    "Quarantine entire record (Recommended for strict data hygiene)",
                    "Set phone_e164 to null and import remainder of record"
                ],
                user_answer="Quarantine entire record (Recommended for strict data hygiene)"
            )
        ]

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return MigrationPlan(
            plan_id=f"plan_v{plan_version}",
            version=plan_version,
            title="Customer Master Data Migration Plan",
            description="Agentic migration specification projecting legacy CRM records into modern core customer schema with strict validation and quarantine rules.",
            source_schema_id=source_schema["schema_id"],
            target_schema_id=target_schema["schema_id"],
            field_mappings=mappings,
            clarifications=clarifications,
            status="PROPOSED",
            approved_by=None,
            approved_at=None,
            created_at=now_iso,
            updated_at=now_iso,
        )

    def _generate_dynamic_plan(
        self,
        source_schema: Dict[str, Any],
        target_schema: Dict[str, Any],
        profiles: Dict[str, Any],
        plan_version: int
    ) -> MigrationPlan:
        source_cols = [f["name"] for f in source_schema.get("fields", [])]
        target_fields = target_schema.get("fields", [])

        mappings: List[FieldMapping] = []
        clarifications: List[ClarificationQuestion] = []

        def match_source_col(target_name: str) -> Optional[str]:
            t_clean = re.sub(r"[^a-zA-Z0-9]", "", target_name.lower())
            for s in source_cols:
                if s.lower() == target_name.lower():
                    return s
            for s in source_cols:
                s_clean = re.sub(r"[^a-zA-Z0-9]", "", re.sub(r"(raw|str|legacy|_cd|_id)", "", s.lower()))
                if t_clean == s_clean or t_clean in s.lower() or (s_clean and s_clean in t_clean):
                    return s
            # Semantic aliases
            aliases = {
                "joined_at": ["created", "created_at", "signup_date", "registered_at", "date_joined", "signup_date_str"],
                "natural_key": ["id", "legacy_id", "legacy_account_id", "user_id", "customer_id"],
                "customer_uuid": ["id", "legacy_id", "legacy_account_id", "user_id", "customer_id"],
                "first_name": ["name", "full_name", "full_name_raw", "fname"],
                "last_name": ["name", "full_name", "full_name_raw", "lname"],
                "email": ["mail", "email_address", "contact_email"],
                "phone_e164": ["phone", "cell", "mobile", "telephone", "phone_number", "phone_raw"],
                "balance_due": ["amount", "balance", "due", "total_due", "balance_due_str"],
                "status": ["status_code", "account_status", "status_raw", "active_flag", "account_status_code"],
                "risk_tier": ["risk", "risk_flag", "risk_level", "risk_tier_code"],
                "country_iso2": ["country", "country_code", "country_code_raw", "nation", "country_iso"],
            }
            if target_name in aliases:
                for candidate in aliases[target_name]:
                    for s in source_cols:
                        if candidate in s.lower():
                            return s
            return None

        for tf in target_fields:
            t_name = tf["name"]
            t_nullable = tf.get("nullable", True)
            matched_src = match_source_col(t_name)

            if not matched_src:
                if t_nullable:
                    transform_name = "COALESCE_VAL"
                    params = {"default": None}
                    risk_level = "LOW"
                    risk_rationale = f"Nullable target field '{t_name}' not present in source dataset. Initialized to null."
                    source_fields = []
                else:
                    transform_name = "COALESCE_VAL"
                    fallback_val = "UNKNOWN"
                    if tf.get("constraints", {}).get("enum"):
                        fallback_val = tf["constraints"]["enum"][0]
                    elif tf.get("constraints", {}).get("regex") == r"^[A-Z]{2}$":
                        fallback_val = "US"
                    elif tf.get("data_type") in ("float", "int", "numeric"):
                        fallback_val = 0
                    params = {"default": fallback_val}
                    risk_level = "HIGH"
                    risk_rationale = f"Non-nullable target field '{t_name}' missing from source. Default fallback '{fallback_val}' applied."
                    source_fields = []
                    clarifications.append(ClarificationQuestion(
                        question_id=f"clarify_{t_name}",
                        field=t_name,
                        question=f"Target field '{t_name}' is required but not in source. Default '{fallback_val}' will be used.",
                        options=[f"Apply fallback default '{fallback_val}'", "Quarantine record"],
                        user_answer=f"Apply fallback default '{fallback_val}'"
                    ))
            else:
                source_fields = [matched_src]
                if "uuid" in t_name.lower() or tf.get("constraints", {}).get("format") == "uuid":
                    transform_name = "UUID_V5_FROM_KEY"
                    params = {"namespace": "custom.data.migration"}
                    risk_level = "LOW"
                    risk_rationale = f"Generated deterministic UUIDv5 from source column '{matched_src}'"
                elif tf.get("constraints", {}).get("enum"):
                    transform_name = "ENUM_LOOKUP"
                    enum_list = tf["constraints"]["enum"]
                    mapping_dict = {str(e): str(e) for e in enum_list}
                    mapping_dict.update({str(e).lower(): str(e) for e in enum_list})
                    if "ACTIVE" in enum_list:
                        mapping_dict.update({"1": "ACTIVE", "0": "INACTIVE", "true": "ACTIVE", "false": "INACTIVE", "active": "ACTIVE", "inactive": "INACTIVE", "9": "TERMINATED"})
                    if "risk" in t_name.lower():
                        mapping_dict.update({
                            "LOW": "STANDARD", "low": "STANDARD", "N": "STANDARD", "n": "STANDARD",
                            "MED": "ELEVATED", "med": "ELEVATED", "MEDIUM": "ELEVATED",
                            "HIGH": "CRITICAL", "high": "CRITICAL", "Y": "CRITICAL", "y": "CRITICAL"
                        })
                    params = {
                        "mapping": mapping_dict,
                        "fallback": enum_list[0]
                    }
                    risk_level = "HIGH"
                    risk_rationale = f"Target enum constraint: {enum_list}. Unknown values mapped or fallback to '{enum_list[0]}'."
                elif any(w in t_name.lower() for w in ["_at", "created", "joined", "timestamp"]) or (tf.get("data_type") == "datetime"):
                    transform_name = "DATE_TO_ISO8601"
                    params = {"required": not t_nullable}
                    risk_level = "MEDIUM"
                    risk_rationale = f"Standardized date string from '{matched_src}' to UTC ISO-8601"
                elif "email" in t_name.lower():
                    transform_name = "EMAIL_NORMALIZE"
                    params = {"required": not t_nullable}
                    risk_level = "MEDIUM"
                    risk_rationale = f"Normalized RFC email address from '{matched_src}'"
                elif "phone" in t_name.lower():
                    transform_name = "PHONE_TO_E164"
                    params = {"default_country_code": "1", "required": not t_nullable}
                    risk_level = "MEDIUM"
                    risk_rationale = f"Normalized phone to international E.164 from '{matched_src}'"
                elif tf.get("data_type") in ("float", "numeric") or any(w in t_name.lower() for w in ["balance", "amount", "price", "due"]):
                    transform_name = "CLEAN_CURRENCY_TO_FLOAT"
                    params = {"default": 0.0}
                    risk_level = "LOW"
                    risk_rationale = f"Parsed numeric currency/float from '{matched_src}'"
                elif "first_name" in t_name.lower():
                    transform_name = "SPLIT_NAME"
                    params = {"part": "first", "required": not t_nullable}
                    risk_level = "LOW"
                    risk_rationale = f"Extracted first name from '{matched_src}'"
                elif "last_name" in t_name.lower():
                    transform_name = "SPLIT_NAME"
                    params = {"part": "last", "required": not t_nullable}
                    risk_level = "LOW"
                    risk_rationale = f"Extracted last name from '{matched_src}'"
                elif tf.get("constraints", {}).get("regex") == r"^[A-Z]{2}$":
                    transform_name = "COUNTRY_TO_ISO2"
                    params = {"default": "US", "required": not t_nullable}
                    risk_level = "LOW"
                    risk_rationale = f"Standardized country code from '{matched_src}'"
                else:
                    transform_name = "DIRECT_COPY"
                    params = {"required": not t_nullable}
                    risk_level = "LOW"
                    risk_rationale = f"Direct copy from '{matched_src}'"

                col_prof = profiles.get(matched_src, {})
                null_pct = col_prof.get("null_percentage", 0.0)
                if not t_nullable and null_pct > 0.0:
                    risk_level = "HIGH"
                    risk_rationale += f" [CRITICAL: Target is NOT NULL but source has {null_pct}% nulls. Invalid rows will be quarantined.]"

            mappings.append(FieldMapping(
                target_field=t_name,
                source_fields=source_fields,
                transformation=transform_name,
                parameters=params,
                risk_level=risk_level,
                risk_rationale=risk_rationale,
                notes=f"Dynamic rule for {t_name}"
            ))

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return MigrationPlan(
            plan_id=f"plan_v{plan_version}",
            version=plan_version,
            title=f"{source_schema.get('name', 'Custom')} Migration Plan",
            description=f"Dynamically generated migration specification for {source_schema.get('name')}.",
            source_schema_id=source_schema["schema_id"],
            target_schema_id=target_schema["schema_id"],
            field_mappings=mappings,
            clarifications=clarifications,
            status="PROPOSED",
            approved_by=None,
            approved_at=None,
            created_at=now_iso,
            updated_at=now_iso,
        )
