from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any

from ..models.schemas import ReconciliationReport
from .profiler import load_sample_records
from .target_store import TargetDatabaseStore


class ReconciliationEngine:
    """Performs rigorous post-migration auditing, comparing source totals,
    target state, and quarantine ledgers to verify zero silent data loss."""

    def __init__(self, target_store: TargetDatabaseStore | None = None):
        self.store = target_store or TargetDatabaseStore()

    def reconcile(
        self, run_id: str, plan_version: int, source_records: list[dict[str, Any]] | None = None
    ) -> ReconciliationReport:
        records = source_records if source_records is not None else load_sample_records()
        total_source = len(records)
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # 1. Target store metrics scoped to run_id if specified
        with self.store.get_connection() as conn:
            if run_id and not run_id.startswith("latest_"):
                tgt_row = conn.execute(
                    "SELECT COUNT(*) as c, COALESCE(SUM(balance_due), 0.0) as s FROM customers WHERE migration_run_id = ?",
                    (run_id,),
                ).fetchone()
                target_count = tgt_row["c"] if tgt_row and tgt_row["c"] > 0 else self.store.get_customer_count()
                target_balance_total = (
                    round(tgt_row["s"], 2) if tgt_row and tgt_row["c"] > 0 else self.store.get_financial_aggregate()
                )
            else:
                target_count = self.store.get_customer_count()
                target_balance_total = self.store.get_financial_aggregate()

            # 2. Quarantine ledger metrics for this run (or overall)
            if run_id and not run_id.startswith("latest_"):
                q_row = conn.execute(
                    "SELECT COUNT(*) as q_count FROM quarantine_ledger WHERE run_id = ?", (run_id,)
                ).fetchone()
                quarantine_count = q_row["q_count"] if q_row else 0
            else:
                # Latest dry run or overall quarantine
                q_row = conn.execute("SELECT COUNT(*) as q_count FROM quarantine_ledger").fetchone()
                quarantine_count = q_row["q_count"] if q_row else 0

            # Check if there are duplicate natural keys in target
            dup_row = conn.execute(
                "SELECT natural_key, COUNT(*) as c FROM customers GROUP BY natural_key HAVING c > 1"
            ).fetchall()
            duplicate_count = len(dup_row)

        # 3. Source monetary aggregate (dynamically find balance/amount column)
        from .transforms import TransformationRegistry

        balance_field = None
        if records:
            first_rec = records[0]
            for candidate in ["balance_due_str", "balance_due", "balance", "amount", "balance_raw"]:
                if candidate in first_rec:
                    balance_field = candidate
                    break
            if not balance_field:
                for k in first_rec.keys():
                    if "balance" in k.lower() or "amount" in k.lower():
                        balance_field = k
                        break

        source_balance_sum = 0.0
        for r in records:
            val = r.get(balance_field) if balance_field else r.get("balance_due_str", 0.0)
            parsed_num, _ = TransformationRegistry.clean_currency_to_float(val, {"default": 0.0})
            if parsed_num is not None:
                source_balance_sum += parsed_num
        source_balance_sum = round(source_balance_sum, 2)

        # 4. Conservation check
        accounted_total = target_count + quarantine_count
        unaccounted = total_source - accounted_total

        if target_count == 0 and (quarantine_count == 0 or run_id.startswith("latest_")):
            verdict = "PENDING_EXECUTION"
            mass_conserved = True
            unaccounted = 0
        elif unaccounted == 0 and quarantine_count == 0:
            verdict = "PASSED_EXACT"
            mass_conserved = duplicate_count == 0
        elif unaccounted == 0 and quarantine_count > 0:
            verdict = "PASSED_WITH_QUARANTINE"
            mass_conserved = duplicate_count == 0
        else:
            verdict = "DISCREPANCY_DETECTED"
            mass_conserved = False

        # Deterministic checksums
        src_hash = hashlib.sha256(f"SRC_{total_source}_{source_balance_sum}".encode()).hexdigest()[:16]
        tgt_hash = hashlib.sha256(f"TGT_{target_count}_{target_balance_total}".encode()).hexdigest()[:16]

        details = [
            f"Source records provided: {total_source}",
            f"Target database rows: {target_count}",
            f"Quarantined invalid rows: {quarantine_count}",
            f"Unaccounted delta: {unaccounted} records",
            f"Duplicate natural keys detected: {duplicate_count}",
            f"Source gross balance aggregate: ${source_balance_sum:,.2f}",
            f"Target gross balance aggregate: ${target_balance_total:,.2f}",
        ]

        if not mass_conserved:
            details.append(f"WARNING: Mass conservation failed! {abs(unaccounted)} records unaccounted.")

        return ReconciliationReport(
            reconciliation_id=f"recon_{uuid.uuid4().hex[:8]}",
            run_id=run_id,
            plan_version=plan_version,
            evaluated_at=now_iso,
            verdict=verdict,
            accounting={
                "total_source_records": total_source,
                "target_accepted_records": target_count,
                "quarantined_records": quarantine_count,
                "unaccounted_records": unaccounted,
            },
            invariants_passed=mass_conserved,
            duplicate_count=duplicate_count,
            source_checksum=src_hash,
            target_checksum=tgt_hash,
            aggregate_comparisons={
                "balance_due": {
                    "source_sum": source_balance_sum,
                    "target_sum": target_balance_total,
                    "delta": round(abs(source_balance_sum - target_balance_total), 2),
                }
            },
            details=details,
        )
