from __future__ import annotations
import sqlite3
import os
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "target_store.db")

class TargetDatabaseStore:
    """Mock Target Store using SQLite with WAL mode, ACID transactions, 
    table snapshots for rollback, and strict unique constraints."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
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
                (snapshot_id, run_id, f"Pre-migration snapshot for {run_id}", now_iso, len(row_dicts), json.dumps(row_dicts))
            )
            conn.commit()
        return snapshot_id

    def rollback_to_snapshot(self, snapshot_id: str) -> Tuple[bool, int, int]:
        """Rolls back the target customers table to the exact state saved in snapshot.
        Returns (success, rows_restored, current_count)."""
        with self.get_connection() as conn:
            snap = conn.execute(
                "SELECT * FROM target_snapshots WHERE snapshot_id = ?",
                (snapshot_id,)
            ).fetchone()

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
                    conn.execute(
                        f"INSERT INTO customers ({col_str}) VALUES ({placeholders})",
                        [r[c] for c in cols]
                    )
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

    def query_customers(self, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM customers ORDER BY natural_key ASC LIMIT ? OFFSET ?",
                (limit, offset)
            ).fetchall()
            return [dict(r) for r in rows]

    def log_audit_event(self, event_id: str, event_type: str, actor: str, details: Dict[str, Any]) -> None:
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.get_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO audit_ledger (event_id, event_type, actor, details, timestamp) VALUES (?, ?, ?, ?, ?)",
                (event_id, event_type, actor, json.dumps(details), now_iso)
            )
            conn.commit()

    def get_audit_trail(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_ledger ORDER BY timestamp DESC LIMIT ?",
                (limit,)
            ).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                d["details"] = json.loads(d["details"])
                result.append(d)
            return result

    def save_plan_to_db(self, version: int, plan_id: str, title: str, status: str, serialized_plan: str, created_at: str, updated_at: str) -> None:
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO migration_plans (version, plan_id, title, status, serialized_plan, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (version, plan_id, title, status, serialized_plan, created_at, updated_at)
            )
            conn.commit()

    def load_all_plans_from_db(self) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            rows = conn.execute("SELECT * FROM migration_plans ORDER BY version ASC").fetchall()
            return [json.loads(r["serialized_plan"]) for r in rows]

    def load_plan_from_db(self, version: int) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            row = conn.execute("SELECT serialized_plan FROM migration_plans WHERE version = ?", (version,)).fetchone()
            if row:
                return json.loads(row["serialized_plan"])
            return None
