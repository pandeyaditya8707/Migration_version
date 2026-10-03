from __future__ import annotations

import copy
import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from ..models.schemas import MigrationPlan
from .target_store import TargetDatabaseStore

logger = logging.getLogger(__name__)


def current_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def compute_plan_fingerprint(plan: MigrationPlan, target_schema: dict[str, Any] | None = None) -> str:
    """Computes a deterministic cryptographic SHA-256 fingerprint binding the plan version,
    its sorted field mappings, and target schema identity."""
    canonical_mappings = []
    for m in sorted(
        plan.field_mappings, key=lambda x: (x.target_field, tuple(sorted(getattr(x, "source_fields", []))))
    ):
        canonical_mappings.append(
            {
                "target_field": m.target_field,
                "source_fields": sorted(getattr(m, "source_fields", [])),
                "transformation": m.transformation,
                "parameters": getattr(m, "parameters", {}),
            }
        )
    canonical_payload = {
        "plan_id": plan.plan_id,
        "version": plan.version,
        "source_schema_id": plan.source_schema_id,
        "target_schema_id": plan.target_schema_id,
        "mappings": canonical_mappings,
        "target_schema_hash": hashlib.sha256(json.dumps(target_schema, sort_keys=True).encode("utf-8")).hexdigest()
        if target_schema
        else None,
    }
    dumped = json.dumps(canonical_payload, sort_keys=True)
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()


class PlanManager:
    """Manages versioned migration plans, SQLite persistence, and approval state transitions."""

    def __init__(self, target_store: TargetDatabaseStore | None = None):
        self.store = target_store or TargetDatabaseStore()
        self._plans: dict[int, MigrationPlan] = {}
        self._load_from_db()

    def _load_from_db(self) -> None:
        """Loads all saved plans from persistent SQLite database."""
        try:
            raw_plans = self.store.load_all_plans_from_db()
            for rp in raw_plans:
                plan = MigrationPlan.model_validate(rp)
                self._plans[plan.version] = plan
        except Exception as e:
            logger.error(f"Failed to load plans from database: {e}", exc_info=True)
            raise RuntimeError(f"Database persistence failure while loading plans: {e}") from e

    def save_plan(self, plan: MigrationPlan, actor: str = "AI Agent") -> MigrationPlan:
        """Saves a plan version in-memory and persistently in SQLite."""
        now_iso = current_utc_iso()
        plan.updated_at = now_iso
        self._plans[plan.version] = plan

        # Persist to SQLite
        try:
            self.store.save_plan_to_db(
                version=plan.version,
                plan_id=plan.plan_id,
                title=plan.title,
                status=plan.status,
                serialized_plan=json.dumps(plan.model_dump()),
                created_at=plan.created_at,
                updated_at=plan.updated_at,
            )
        except Exception as e:
            logger.error(f"Failed to persist plan v{plan.version} to database: {e}", exc_info=True)
            raise RuntimeError(f"Database persistence failure while saving plan v{plan.version}: {e}") from e

        evt_suffix = uuid.uuid4().hex[:8]
        self.store.log_audit_event(
            event_id=f"evt_plan_{plan.version}_{evt_suffix}",
            event_type="PLAN_CREATED" if plan.version == 1 else "PLAN_UPDATED",
            actor=actor,
            details={
                "plan_id": plan.plan_id,
                "version": plan.version,
                "mappings_count": len(plan.field_mappings),
                "status": plan.status,
            },
        )
        return plan

    def get_plan(self, version: int) -> MigrationPlan | None:
        if version in self._plans:
            return self._plans[version]

        # Try loading from DB
        raw = self.store.load_plan_from_db(version)
        if raw:
            p = MigrationPlan.model_validate(raw)
            self._plans[p.version] = p
            return p
        return None

    def list_plans(self) -> list[MigrationPlan]:
        # Refresh from DB to pick up any external changes
        self._load_from_db()
        return sorted(list(self._plans.values()), key=lambda p: p.version)

    def get_latest_plan(self) -> MigrationPlan | None:
        plans = self.list_plans()
        return plans[-1] if plans else None

    def approve_plan(
        self, version: int, approved_by: str = "Lead Data Engineer", target_schema: dict[str, Any] | None = None
    ) -> MigrationPlan:
        plan = self.get_plan(version)
        if not plan:
            raise ValueError(f"Plan version {version} not found")

        now_iso = current_utc_iso()
        plan.status = "APPROVED"
        plan.approved_by = approved_by
        plan.approved_at = now_iso
        plan.updated_at = now_iso
        plan.approval_fingerprint = compute_plan_fingerprint(plan, target_schema)

        self.save_plan(plan, actor=approved_by)

        evt_suffix = uuid.uuid4().hex[:8]
        self.store.log_audit_event(
            event_id=f"evt_appr_{version}_{evt_suffix}",
            event_type="PLAN_APPROVED",
            actor=approved_by,
            details={
                "plan_id": plan.plan_id,
                "version": plan.version,
                "approval_fingerprint": plan.approval_fingerprint,
                "approved_at": now_iso,
            },
        )
        return plan

    def create_next_version(
        self, base_version: int, updated_mappings: list, actor: str = "Lead Data Engineer"
    ) -> MigrationPlan:
        base_plan = self.get_plan(base_version)
        if not base_plan:
            raise ValueError(f"Base plan version {base_version} not found")

        existing_versions = list(self._plans.keys())
        next_version = max(existing_versions) + 1 if existing_versions else 1
        now_iso = current_utc_iso()

        new_plan = MigrationPlan(
            plan_id=f"plan_v{next_version}",
            version=next_version,
            title=f"Customer Migration Plan (v{next_version})",
            description=f"Revision derived from v{base_version} with customized mapping rules.",
            source_schema_id=base_plan.source_schema_id,
            target_schema_id=base_plan.target_schema_id,
            field_mappings=updated_mappings,
            clarifications=copy.deepcopy(base_plan.clarifications),
            status="DRAFT",
            approved_by=None,
            approved_at=None,
            created_at=now_iso,
            updated_at=now_iso,
        )
        return self.save_plan(new_plan, actor=actor)

    def reset(self, initial_plan: MigrationPlan) -> None:
        """Clears cached plans and resets to initial plan."""
        self._plans.clear()
        self.save_plan(initial_plan, actor="System Reset")
