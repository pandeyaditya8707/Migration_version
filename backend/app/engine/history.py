from __future__ import annotations
import copy
import json
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional
from ..models.schemas import MigrationPlan, AuditLogEvent
from .target_store import TargetDatabaseStore

def current_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

class PlanManager:
    """Manages versioned migration plans, SQLite persistence, and approval state transitions."""

    def __init__(self, target_store: Optional[TargetDatabaseStore] = None):
        self.store = target_store or TargetDatabaseStore()
        self._plans: Dict[int, MigrationPlan] = {}
        self._load_from_db()

    def _load_from_db(self) -> None:
        """Loads all saved plans from persistent SQLite database."""
        try:
            raw_plans = self.store.load_all_plans_from_db()
            for rp in raw_plans:
                plan = MigrationPlan.model_validate(rp)
                self._plans[plan.version] = plan
        except Exception:
            pass

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
                updated_at=plan.updated_at
            )
        except Exception:
            pass

        evt_suffix = uuid.uuid4().hex[:8]
        self.store.log_audit_event(
            event_id=f"evt_plan_{plan.version}_{evt_suffix}",
            event_type="PLAN_CREATED" if plan.version == 1 else "PLAN_UPDATED",
            actor=actor,
            details={
                "plan_id": plan.plan_id,
                "version": plan.version,
                "mappings_count": len(plan.field_mappings),
                "status": plan.status
            }
        )
        return plan

    def get_plan(self, version: int) -> Optional[MigrationPlan]:
        if version in self._plans:
            return self._plans[version]

        # Try loading from DB
        raw = self.store.load_plan_from_db(version)
        if raw:
            p = MigrationPlan.model_validate(raw)
            self._plans[p.version] = p
            return p
        return None

    def list_plans(self) -> List[MigrationPlan]:
        # Refresh from DB to pick up any external changes
        self._load_from_db()
        return sorted(list(self._plans.values()), key=lambda p: p.version)

    def get_latest_plan(self) -> Optional[MigrationPlan]:
        plans = self.list_plans()
        return plans[-1] if plans else None

    def approve_plan(self, version: int, approved_by: str = "Lead Data Engineer") -> MigrationPlan:
        plan = self.get_plan(version)
        if not plan:
            raise ValueError(f"Plan version {version} not found")

        now_iso = current_utc_iso()
        plan.status = "APPROVED"
        plan.approved_by = approved_by
        plan.approved_at = now_iso
        plan.updated_at = now_iso

        self.save_plan(plan, actor=approved_by)

        evt_suffix = uuid.uuid4().hex[:8]
        self.store.log_audit_event(
            event_id=f"evt_appr_{version}_{evt_suffix}",
            event_type="PLAN_APPROVED",
            actor=approved_by,
            details={
                "plan_id": plan.plan_id,
                "version": plan.version,
                "approved_at": now_iso
            }
        )
        return plan

    def create_next_version(self, base_version: int, updated_mappings: list, actor: str = "Lead Data Engineer") -> MigrationPlan:
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
            updated_at=now_iso
        )
        return self.save_plan(new_plan, actor=actor)

    def reset(self, initial_plan: MigrationPlan) -> None:
        """Clears cached plans and resets to initial plan."""
        self._plans.clear()
        self.save_plan(initial_plan, actor="System Reset")

