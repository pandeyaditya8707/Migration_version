from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from ..models.schemas import (
    ExecutionRunResult,
    MigrationPlan,
    RollbackResult,
)
from .dry_run import DryRunEngine
from .history import compute_plan_fingerprint
from .target_store import TargetDatabaseStore


class ExecutionEngine:
    """Executes approved migration plans into the mock target store.
    Strictly requires plan.status == 'APPROVED' and plan.approval_fingerprint integrity."""

    def __init__(self, target_store: TargetDatabaseStore | None = None):
        self.store = target_store or TargetDatabaseStore()
        self.dry_runner = DryRunEngine()

    def execute_migration(
        self,
        plan: MigrationPlan,
        actor: str = "Lead Data Engineer",
        is_retry: bool = False,
        records: list[dict[str, Any]] | None = None,
    ) -> ExecutionRunResult:
        """Executes an approved migration plan into the mock target store.
        Strictly requires plan.status == 'APPROVED' and verified approval_fingerprint."""
        if plan.status != "APPROVED":
            raise ValueError(
                f"Execution rejected: Migration plan '{plan.plan_id}' (v{plan.version}) "
                f"has status '{plan.status}'. User approval is mandatory prior to execution."
            )

        expected_fingerprint = compute_plan_fingerprint(plan)
        if not plan.approval_fingerprint or plan.approval_fingerprint != expected_fingerprint:
            raise ValueError(
                f"Security violation: Migration plan '{plan.plan_id}' (v{plan.version}) has missing or invalid "
                f"approval fingerprint. Plan mappings were altered or approval was forged. Re-approval is mandatory."
            )

        start_time = time.perf_counter()
        run_id = f"exec_run_{uuid.uuid4().hex[:8]}"
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # 1. Execute deterministic dry run to partition valid vs quarantine
        dry_summary, valid_records, quarantine_records = self.dry_runner.execute_dry_run(plan, records=records)

        # 2. Create pre-execution snapshot for rollback safety
        snapshot_id = self.store.create_snapshot(run_id)

        # 3. Insert valid records with idempotency / duplicate prevention
        inserted_count = 0
        updated_count = 0
        skipped_duplicates = 0

        with self.store.get_connection() as conn:
            conn.execute("BEGIN TRANSACTION")
            try:
                # First, query existing natural keys in target store to detect duplicates and changes
                existing_rows = {
                    str(r["natural_key"]).strip(): dict(r) for r in conn.execute("SELECT * FROM customers").fetchall()
                }

                for rec in valid_records:
                    nat_key = str(rec.get("natural_key", "")).strip()
                    if nat_key in existing_rows:
                        old_r = existing_rows[nat_key]
                        is_diff = (
                            old_r.get("first_name") != rec["first_name"]
                            or old_r.get("last_name") != rec.get("last_name")
                            or old_r.get("email") != rec["email"]
                            or old_r.get("phone_e164") != rec.get("phone_e164")
                            or str(old_r.get("joined_at")) != str(rec.get("joined_at"))
                            or old_r.get("status") != rec.get("status")
                            or float(old_r.get("balance_due") or 0.0) != float(rec.get("balance_due") or 0.0)
                            or old_r.get("risk_tier") != rec.get("risk_tier")
                            or old_r.get("country_iso2") != rec.get("country_iso2")
                        )
                        if is_diff:
                            conn.execute(
                                """
                                UPDATE customers
                                SET first_name = ?, last_name = ?, email = ?, phone_e164 = ?,
                                    joined_at = ?, status = ?, balance_due = ?, risk_tier = ?,
                                    country_iso2 = ?, migration_run_id = ?, migrated_at = ?
                                WHERE natural_key = ?
                                """,
                                (
                                    rec["first_name"],
                                    rec.get("last_name"),
                                    rec["email"],
                                    rec.get("phone_e164"),
                                    rec["joined_at"],
                                    rec["status"],
                                    rec["balance_due"],
                                    rec["risk_tier"],
                                    rec["country_iso2"],
                                    run_id,
                                    now_iso,
                                    nat_key,
                                ),
                            )
                            updated_count += 1
                            existing_rows[nat_key] = {**old_r, **rec}
                        else:
                            skipped_duplicates += 1
                    else:
                        conn.execute(
                            """
                            INSERT INTO customers (
                                customer_uuid, natural_key, first_name, last_name, email,
                                phone_e164, joined_at, status, balance_due, risk_tier,
                                country_iso2, migration_run_id, migrated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(natural_key) DO UPDATE SET
                                first_name = excluded.first_name,
                                last_name = excluded.last_name,
                                email = excluded.email,
                                phone_e164 = excluded.phone_e164,
                                joined_at = excluded.joined_at,
                                status = excluded.status,
                                balance_due = excluded.balance_due,
                                risk_tier = excluded.risk_tier,
                                country_iso2 = excluded.country_iso2,
                                migration_run_id = excluded.migration_run_id,
                                migrated_at = excluded.migrated_at
                            """,
                            (
                                rec["customer_uuid"],
                                nat_key,
                                rec["first_name"],
                                rec.get("last_name"),
                                rec["email"],
                                rec.get("phone_e164"),
                                rec["joined_at"],
                                rec["status"],
                                rec["balance_due"],
                                rec["risk_tier"],
                                rec["country_iso2"],
                                run_id,
                                now_iso,
                            ),
                        )
                        existing_rows[nat_key] = dict(rec)
                        inserted_count += 1

                # 4. Save quarantine records to quarantine ledger
                for q in quarantine_records:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO quarantine_ledger (
                            quarantine_id, run_id, source_row_index, source_natural_key,
                            source_payload, errors, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            q.quarantine_id,
                            run_id,
                            q.source_row_index,
                            q.source_natural_key,
                            json.dumps(q.source_payload),
                            json.dumps([e.model_dump() for e in q.errors]),
                            now_iso,
                        ),
                    )

                conn.commit()
            except Exception as e:
                conn.rollback()
                raise RuntimeError(f"Database transaction failed during migration execution: {e}")

        elapsed_ms = (time.perf_counter() - start_time) * 1000.0

        # 5. Log audit event
        event_type = "MIGRATION_RETRIED" if is_retry or skipped_duplicates > 0 else "MIGRATION_EXECUTED"
        self.store.log_audit_event(
            event_id=f"evt_{uuid.uuid4().hex[:8]}",
            event_type=event_type,
            actor=actor,
            details={
                "run_id": run_id,
                "plan_id": plan.plan_id,
                "plan_version": plan.version,
                "snapshot_id": snapshot_id,
                "inserted_count": inserted_count,
                "updated_count": updated_count,
                "skipped_duplicates_count": skipped_duplicates,
                "quarantined_count": len(quarantine_records),
                "is_retry": is_retry,
            },
        )

        return ExecutionRunResult(
            run_id=run_id,
            plan_version=plan.version,
            status="SUCCESS",
            total_source_records=dry_summary.total_source_records,
            inserted_count=inserted_count,
            updated_count=updated_count,
            skipped_duplicates_count=skipped_duplicates,
            quarantined_count=len(quarantine_records),
            execution_time_ms=round(elapsed_ms, 2),
            target_table_name="customers",
            snapshot_id=snapshot_id,
            timestamp=now_iso,
        )

    def rollback_migration(self, snapshot_id: str, run_id: str, actor: str = "Lead Data Engineer") -> RollbackResult:
        """Rolls back the target customers table to the designated pre-migration snapshot."""
        count_before = self.store.get_customer_count()
        success, restored_count, remaining_count = self.store.rollback_to_snapshot(snapshot_id)

        if not success:
            return RollbackResult(
                run_id=run_id,
                status="FAILED",
                message=f"Snapshot '{snapshot_id}' not found or rollback transaction failed",
                records_removed=0,
                target_records_remaining=count_before,
            )

        records_removed = max(0, count_before - remaining_count)

        # Log audit event
        self.store.log_audit_event(
            event_id=f"evt_{uuid.uuid4().hex[:8]}",
            event_type="MIGRATION_ROLLED_BACK",
            actor=actor,
            details={
                "run_id": run_id,
                "snapshot_id": snapshot_id,
                "records_before_rollback": count_before,
                "records_after_rollback": remaining_count,
                "records_removed": records_removed,
            },
        )

        return RollbackResult(
            run_id=run_id,
            status="SUCCESS",
            message=f"Successfully restored target store to snapshot state ({remaining_count} records remaining).",
            records_removed=records_removed,
            target_records_remaining=remaining_count,
        )
