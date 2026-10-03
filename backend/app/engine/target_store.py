from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "target_store.db")


class TargetDatabaseStore:
    """Mock Target Store using SQLite with WAL mode, ACID transactions,
    table snapshots for rollback, and strict unique constraints."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or os.environ.get("DATABASE_PATH") or DB_PATH
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self.init_database()

    def get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init_database(self) -> None:
        """Initializes tables for target records, snapshots, quarantine, and audit ledger."""
        with self.get_connection() as conn:
            # 1. Main target table
            conn.execute("""
            CREATE TABLE IF NOT EXISTS customers (
                customer_uuid TEXT PRIMARY KEY,
                natural_key TEXT UNIQUE NOT NULL,
                first_name TEXT NOT NULL,
                last_name TEXT,
                email TEXT NOT NULL,
                phone_e164 TEXT,
                joined_at TEXT NOT NULL,
                status TEXT NOT NULL,
                balance_due REAL NOT NULL DEFAULT 0.0,
                risk_tier TEXT NOT NULL,
                country_iso2 TEXT NOT NULL,
                migration_run_id TEXT NOT NULL,
                migrated_at TEXT NOT NULL
            );
            """)

            # 2. Table for execution snapshots (for 1-click rollback)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS target_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                snapshot_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                customer_count INT NOT NULL,
                serialized_rows TEXT NOT NULL
            );
            """)

            # 3. Persistent quarantine storage
            conn.execute("""
            CREATE TABLE IF NOT EXISTS quarantine_ledger (
                quarantine_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                source_row_index INT NOT NULL,
                source_natural_key TEXT,
                source_payload TEXT NOT NULL,
                errors TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """)

            # 4. Audit ledger table
            conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_ledger (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                actor TEXT NOT NULL,
                details TEXT NOT NULL,
                timestamp TEXT NOT NULL
            );
            """)

            # 5. Persistent Migration Plans table
            conn.execute("""
            CREATE TABLE IF NOT EXISTS migration_plans (
                version INT PRIMARY KEY,
                plan_id TEXT NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL,
                serialized_plan TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """)
            conn.commit()

    def reset_database(self) -> None:
        """Clears all customer and snapshot records (for clean test runs)."""
        with self.get_connection() as conn:
            conn.execute("DELETE FROM customers;")
            conn.execute("DELETE FROM target_snapshots;")
            conn.execute("DELETE FROM quarantine_ledger;")
            conn.execute("DELETE FROM audit_ledger;")
            conn.execute("DELETE FROM migration_plans;")
            conn.commit()

    def create_snapshot(self, run_id: str) -> str:
        """Takes a full serialized snapshot of the customers table prior to execution."""
        ts = int(datetime.now(timezone.utc).timestamp())
        snapshot_id = f"snap_{run_id}_{ts}"
        with self.get_connection() as conn:
            rows = conn.execute("SELECT * FROM customers").fetchall()
            row_dicts = [dict(r) for r in rows]
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.execute(
                """
                INSERT INTO target_snapshots (snapshot_id, run_id, snapshot_name, created_at, customer_count, serialized_rows)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    run_id,
                    f"Pre-migration snapshot for {run_id}",
                    now_iso,
                    len(row_dicts),
                    json.dumps(row_dicts),
                ),
            )
            conn.commit()
        return snapshot_id

    def rollback_to_snapshot(self, snapshot_id: str) -> tuple[bool, int, int]:
        """Rolls back the target customers table to the exact state saved in snapshot.
        Returns (success, rows_restored, current_count)."""
        with self.get_connection() as conn:
            snap = conn.execute("SELECT * FROM target_snapshots WHERE snapshot_id = ?", (snapshot_id,)).fetchone()

            if not snap:
                return False, 0, 0

            saved_rows = json.loads(snap["serialized_rows"])

            conn.execute("BEGIN TRANSACTION")
            try:
                conn.execute("DELETE FROM customers;")
                for r in saved_rows:
                    cols = list(r.keys())
                    placeholders = ", ".join(["?"] * len(cols))
                    col_str = ", ".join(cols)
                    conn.execute(f"INSERT INTO customers ({col_str}) VALUES ({placeholders})", [r[c] for c in cols])
                conn.commit()
                return True, len(saved_rows), len(saved_rows)
            except Exception as e:
                conn.rollback()
                raise e

    def get_customer_count(self) -> int:
        with self.get_connection() as conn:
            row = conn.execute("SELECT COUNT(*) as count FROM customers").fetchone()
            return row["count"] if row else 0

    def get_financial_aggregate(self) -> float:
        with self.get_connection() as conn:
            row = conn.execute("SELECT COALESCE(SUM(balance_due), 0.0) as total FROM customers").fetchone()
            return round(row["total"], 2) if row else 0.0

    def query_customers(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM customers ORDER BY natural_key ASC LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
            return [dict(r) for r in rows]

    def log_audit_event(self, event_id: str, event_type: str, actor: str, details: dict[str, Any]) -> None:
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.get_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO audit_ledger (event_id, event_type, actor, details, timestamp) VALUES (?, ?, ?, ?, ?)",
                (event_id, event_type, actor, json.dumps(details), now_iso),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Run-scoped ledger queries (single source of truth for reconciliation)
    # ------------------------------------------------------------------
    _EXECUTION_EVENTS = ("MIGRATION_EXECUTED", "MIGRATION_RETRIED")

    def _events_for(self, event_types: tuple[str, ...]) -> list[dict[str, Any]]:
        """Audit events of the given types, newest first (rowid breaks same-second ties)."""
        marks = ",".join("?" for _ in event_types)
        with self.get_connection() as conn:
            rows = conn.execute(
                f"SELECT event_id, event_type, details FROM audit_ledger "  # noqa: S608 (placeholders only)
                f"WHERE event_type IN ({marks}) ORDER BY rowid DESC",
                event_types,
            ).fetchall()
        out = []
        for r in rows:
            try:
                details = json.loads(r["details"])
            except (TypeError, ValueError):
                details = {}
            out.append({"event_id": r["event_id"], "event_type": r["event_type"], "details": details})
        return out

    def get_latest_execution_run_id(self) -> str | None:
        events = self._events_for(self._EXECUTION_EVENTS)
        return events[0]["details"].get("run_id") if events else None

    def get_latest_dry_run_id(self) -> str | None:
        events = self._events_for(("DRY_RUN_EXECUTED",))
        return events[0]["details"].get("run_id") if events else None

    def get_execution_outcome(self, run_id: str) -> dict[str, Any] | None:
        """The recorded outcome (inserted/updated/skipped/quarantined) of one execution run."""
        for ev in self._events_for(self._EXECUTION_EVENTS):
            if ev["details"].get("run_id") == run_id:
                return ev["details"]
        return None

    def is_run_rolled_back(self, run_id: str) -> bool:
        return any(ev["details"].get("run_id") == run_id for ev in self._events_for(("MIGRATION_ROLLED_BACK",)))

    def count_quarantined(self, run_id: str) -> int:
        with self.get_connection() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM quarantine_ledger WHERE run_id = ?", (run_id,)).fetchone()
            return int(row["c"]) if row else 0

    def get_active_quarantine_run_id(self) -> str | None:
        """The run whose quarantine entries represent the current state: the latest execution if any, else latest dry run."""
        return self.get_latest_execution_run_id() or self.get_latest_dry_run_id()

    def get_audit_trail(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.get_connection() as conn:
            rows = conn.execute("SELECT * FROM audit_ledger ORDER BY timestamp DESC, rowid DESC LIMIT ?", (limit,)).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                d["details"] = json.loads(d["details"])
                result.append(d)
            return result

    def save_plan_to_db(
        self,
        version: int,
        plan_id: str,
        title: str,
        status: str,
        serialized_plan: str,
        created_at: str,
        updated_at: str,
    ) -> None:
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO migration_plans (version, plan_id, title, status, serialized_plan, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (version, plan_id, title, status, serialized_plan, created_at, updated_at),
            )
            conn.commit()

    def load_all_plans_from_db(self) -> list[dict[str, Any]]:
        with self.get_connection() as conn:
            rows = conn.execute("SELECT * FROM migration_plans ORDER BY version ASC").fetchall()
            return [json.loads(r["serialized_plan"]) for r in rows]

    def load_plan_from_db(self, version: int) -> dict[str, Any] | None:
        with self.get_connection() as conn:
            row = conn.execute("SELECT serialized_plan FROM migration_plans WHERE version = ?", (version,)).fetchone()
            if row:
                return json.loads(row["serialized_plan"])
            return None
