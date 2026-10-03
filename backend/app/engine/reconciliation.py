from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any

from ..models.schemas import ReconciliationReport
from .profiler import load_sample_records
from .target_store import TargetDatabaseStore


class ReconciliationEngine:
    """Post-migration audit. Accounting is scoped to ONE execution run:
    source = accepted (inserted + updated + unchanged) + quarantined."""

    def __init__(self, target_store: TargetDatabaseStore | None = None):
        self.store = target_store or TargetDatabaseStore()

    def reconcile(
        self, run_id: str, plan_version: int, source_records: list[dict[str, Any]] | None = None
    ) -> ReconciliationReport:
        records = source_records if source_records is not None else load_sample_records()
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # 1. Resolve aliases ("latest*", empty) to one concrete run. Every figure below is
        #    scoped to that single run, so ledger entries of other runs (e.g. the dry run
        #    whose rejections the execution re-records) can never be counted twice.
        requested = run_id or ""
        if not requested or requested.startswith("latest"):
            resolved = self.store.get_latest_execution_run_id() or self.store.get_latest_dry_run_id() or requested
        else:
            resolved = requested
        run_id = resolved

        outcome = self.store.get_execution_outcome(run_id)
        rolled_back = bool(outcome) and self.store.is_run_rolled_back(run_id)
        quarantine_count = self.store.count_quarantined(run_id)
        target_count_live = self.store.get_customer_count()
        target_balance_total = self.store.get_financial_aggregate()

        with self.store.get_connection() as conn:
            dup_row = conn.execute(
                "SELECT natural_key, COUNT(*) as c FROM customers GROUP BY natural_key HAVING c > 1"
            ).fetchall()
            duplicate_count = len(dup_row)

        if outcome:
            # Source size at the time of the run (the live source may have been replaced since).
            total_source = int(outcome.get("total_source_records", len(records)))
            # Every valid row is exactly one of inserted / updated / skipped-unchanged, whichever run
            # originally wrote it, so a retry that skips rows still accounts for all of them.
            target_count = (
                int(outcome.get("inserted_count", 0))
                + int(outcome.get("updated_count", 0))
                + int(outcome.get("skipped_duplicates_count", 0))
            )
        else:
            total_source = len(records)
            target_count = 0

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
        if not outcome:
            # Nothing executed for this run: dry-run rejections are reported, nothing is "missing".
            verdict = "PENDING_EXECUTION"
            mass_conserved = True
            unaccounted = 0
        elif rolled_back:
            target_count = 0  # target was restored to its pre-run snapshot
            verdict = "ROLLED_BACK"
            mass_conserved = True
            unaccounted = 0
        else:
            unaccounted = total_source - (target_count + quarantine_count)
            if unaccounted != 0:
                verdict = "DISCREPANCY_DETECTED"
                mass_conserved = False
            elif quarantine_count == 0:
                verdict = "PASSED_EXACT"
                mass_conserved = duplicate_count == 0
            else:
                verdict = "PASSED_WITH_QUARANTINE"
                mass_conserved = duplicate_count == 0
            if verdict.startswith("PASSED") and not mass_conserved:
                verdict = "DISCREPANCY_DETECTED"

        # Deterministic checksums
        src_hash = hashlib.sha256(f"SRC_{total_source}_{source_balance_sum}".encode()).hexdigest()[:16]
        tgt_hash = hashlib.sha256(f"TGT_{target_count_live}_{target_balance_total}".encode()).hexdigest()[:16]

        details = [
            f"Run evaluated: {run_id or 'none'}" + (" (rolled back)" if rolled_back else ""),
            f"Source records provided: {total_source}",
            f"Accepted by this run (inserted + updated + unchanged): {target_count}",
            f"Live target table rows: {target_count_live}",
            f"Quarantined invalid rows (this run only): {quarantine_count}",
            f"Unaccounted delta: {unaccounted} records",
            f"Duplicate natural keys detected: {duplicate_count}",
            f"Source gross balance aggregate: ${source_balance_sum:,.2f}",
            f"Target gross balance aggregate: ${target_balance_total:,.2f}",
        ]

        if verdict == "DISCREPANCY_DETECTED":
            details.append(
                f"WARNING: Mass conservation failed! {abs(unaccounted)} records unaccounted"
                + (f"; {duplicate_count} duplicate natural keys in target." if duplicate_count else ".")
            )

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
