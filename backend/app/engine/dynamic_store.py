from __future__ import annotations
import sqlite3
import os
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "target_store.db")

class DynamicDatabaseStore:
    """Dynamic SQLite storage engine capable of compiling arbitrary target schemas 
    into tables with type affinities, constraints, atomic upserts, snapshots, and rollbacks."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_metadata_tables()

    def get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_metadata_tables(self) -> None:
        """Initializes tables for tracking registered schemas, dynamic snapshots, and ledgers."""
        with self.get_connection() as conn:
            conn.execute("""
            CREATE TABLE IF NOT EXISTS dynamic_schemas (
                schema_id TEXT PRIMARY KEY,
                table_name TEXT NOT NULL UNIQUE,
                serialized_schema TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS dynamic_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                table_name TEXT NOT NULL,
                run_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                row_count INT NOT NULL,
                serialized_rows TEXT NOT NULL
            );
            """)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS v2_persistent_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """)
            conn.commit()

    @staticmethod
    def map_to_sqlite_type(data_type: str) -> str:
        dt = data_type.lower().strip()
        if dt in ("int", "integer", "boolean", "bool", "bigint", "smallint"):
            return "INTEGER"
        elif dt in ("float", "double", "real", "numeric", "decimal", "currency", "money"):
            return "REAL"
        elif dt in ("blob", "binary", "bytes"):
            return "BLOB"
        else:
            return "TEXT"

    def compile_and_create_table(self, schema_dict: Dict[str, Any]) -> str:
        """Compiles arbitrary target schema JSON to SQLite DDL and creates the table."""
        table_name = str(schema_dict.get("table_name", "target_records")).strip()
        raw_nk = schema_dict.get("natural_key", schema_dict.get("primary_key", "id"))
        raw_pk = schema_dict.get("primary_key", raw_nk)
        natural_key = (raw_nk[0] if isinstance(raw_nk, list) and raw_nk else str(raw_nk)).strip()
        primary_key = (raw_pk[0] if isinstance(raw_pk, list) and raw_pk else str(raw_pk)).strip()
        fields = schema_dict.get("fields", [])

        if not fields:
            raise ValueError(f"Target schema for table '{table_name}' must define at least one field.")

        col_defs: List[str] = []
        field_names = set()

        for f in fields:
            name = f["name"].strip()
            field_names.add(name)
            col_type = self.map_to_sqlite_type(f.get("data_type", "string"))
            nullable = f.get("nullable", True)
            constraints = f.get("constraints", {})

            parts = [name, col_type]
            if name == primary_key:
                parts.append("PRIMARY KEY")
            elif not nullable:
                parts.append("NOT NULL")

            if constraints.get("unique") and name != primary_key:
                parts.append("UNIQUE")

            if constraints.get("allowed_values") and isinstance(constraints["allowed_values"], list) and constraints["allowed_values"]:
                allowed_str = ", ".join(f"'{v}'" for v in constraints["allowed_values"])
                if nullable:
                    parts.append(f"CHECK ({name} IS NULL OR {name} IN ({allowed_str}))")
                else:
                    parts.append(f"CHECK ({name} IN ({allowed_str}))")

            col_defs.append(" ".join(parts))

        # Injected audit columns
        col_defs.append("migration_run_id TEXT NOT NULL")
        col_defs.append("migrated_at TEXT NOT NULL")

        table_constraints: List[str] = []
        if natural_key not in field_names:
            col_defs.append(f"{natural_key} TEXT UNIQUE NOT NULL")
        elif natural_key != primary_key and f"{natural_key} TEXT UNIQUE" not in " ".join(col_defs):
            table_constraints.append(f"UNIQUE({natural_key})")

        all_items = col_defs + table_constraints
        ddl = f"CREATE TABLE IF NOT EXISTS {table_name} (\n  " + ",\n  ".join(all_items) + "\n);"

        with self.get_connection() as conn:
            # Check existing columns; if table exists with mismatched schema, recreate it
            cur = conn.execute(f"PRAGMA table_info({table_name});")
            existing_cols = {row[1] for row in cur.fetchall()}
            if existing_cols and not field_names.issubset(existing_cols):
                conn.execute(f"DROP TABLE IF EXISTS {table_name};")
            conn.execute(ddl)
            # Register schema metadata
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            schema_id = schema_dict.get("schema_id", f"schema_{table_name}")
            conn.execute(
                """
                INSERT OR REPLACE INTO dynamic_schemas (schema_id, table_name, serialized_schema, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (schema_id, table_name, json.dumps(schema_dict), now_iso)
            )
            conn.commit()

        return ddl

    def execute_upsert_batch(
        self,
        table_name: str,
        natural_key_field: str,
        rows: List[Dict[str, Any]],
        run_id: str
    ) -> Tuple[int, int, int]:
        """Atomically inserts or updates records with idempotency and duplicate prevention.
        Returns: (inserted_count, updated_count, skipped_duplicates)."""
        if not rows:
            return 0, 0, 0

        if isinstance(natural_key_field, list):
            natural_key_field = natural_key_field[0] if natural_key_field else "id"
        natural_key_field = str(natural_key_field or "id").strip()

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        inserted = 0
        updated = 0
        skipped = 0

        with self.get_connection() as conn:
            # 1. Inspect table columns and constraints
            cur = conn.execute(f"PRAGMA table_info({table_name});")
            col_info = cur.fetchall()
            if not col_info:
                raise RuntimeError(f"Target table '{table_name}' does not exist in SQLite database.")

            existing_col_names = {row[1] for row in col_info}
            not_null_cols = {row[1]: (row[2] or "TEXT") for row in col_info if row[3] == 1 and row[4] is None}

            # 2. Check if any incoming keys in rows are missing from the table; dynamically ADD COLUMN if needed
            all_incoming_keys = set().union(*(r.keys() for r in rows))
            all_incoming_keys.add("migration_run_id")
            all_incoming_keys.add("migrated_at")
            for missing_col in (all_incoming_keys - existing_col_names):
                try:
                    conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {missing_col} TEXT;")
                    existing_col_names.add(missing_col)
                    logger.info(f"Dynamically added missing column '{missing_col}' to table '{table_name}'.")
                except Exception as alter_err:
                    logger.warning(f"Could not add column '{missing_col}' to '{table_name}': {alter_err}")

            # 3. Ensure natural_key_field exists in existing_col_names
            pk_cols = [row[1] for row in col_info if row[5] > 0]
            pk_name = pk_cols[0] if pk_cols else None
            if natural_key_field not in existing_col_names:
                natural_key_field = pk_name if pk_name else list(existing_col_names)[0]

            # 4. Ensure a UNIQUE index exists on natural_key_field for ON CONFLICT
            try:
                conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{table_name}_{natural_key_field} ON {table_name}({natural_key_field});")
                conn.commit()
            except Exception as idx_err:
                logger.debug(f"Unique index creation note on {table_name}.{natural_key_field}: {idx_err}")

            # 5. Query existing keys
            existing_rows = conn.execute(f"SELECT {natural_key_field} FROM {table_name}").fetchall()
            existing_keys = {str(r[natural_key_field]).strip() for r in existing_rows if r[natural_key_field] is not None}

            existing_pks = set()
            if pk_name:
                pk_db_rows = conn.execute(f"SELECT {pk_name} FROM {table_name}").fetchall()
                existing_pks = {str(r[pk_name]).strip() for r in pk_db_rows if r[pk_name] is not None}

            conn.execute("BEGIN TRANSACTION")
            try:
                import uuid as _uuid
                seen_batch_pks = set()

                for r in rows:
                    rec = dict(r)
                    rec["migration_run_id"] = run_id
                    rec["migrated_at"] = now_iso

                    # Supply safe defaults for any NOT NULL columns missing from rec
                    for col_name, col_type in not_null_cols.items():
                        is_missing_or_blank = col_name not in rec or rec[col_name] is None or (isinstance(rec[col_name], str) and not rec[col_name].strip())
                        if is_missing_or_blank:
                            c_upper = col_type.upper()
                            if col_name in pk_cols or col_name.endswith("_uuid") or col_name.endswith("_id"):
                                rec[col_name] = str(_uuid.uuid4())
                            elif col_name == natural_key_field:
                                rec[col_name] = f"auto_{run_id}_{_uuid.uuid4().hex[:6]}"
                            elif "INT" in c_upper:
                                rec[col_name] = 0
                            elif "REAL" in c_upper or "FLOAT" in c_upper:
                                rec[col_name] = 0.0
                            else:
                                rec[col_name] = ""

                    # Ensure primary key uniqueness across the batch and against existing table records
                    if pk_name and pk_name in rec:
                        cur_pk = str(rec[pk_name]).strip()
                        key_val_check = str(rec.get(natural_key_field, "")).strip()
                        # If duplicate within this batch, or colliding with an existing PK from another natural key
                        if (cur_pk in seen_batch_pks) or (cur_pk in existing_pks and key_val_check not in existing_keys):
                            rec[pk_name] = str(_uuid.uuid4())
                            cur_pk = rec[pk_name]
                        seen_batch_pks.add(cur_pk)

                    # Filter to only columns that actually exist in the table
                    valid_cols = [c for c in rec.keys() if c in existing_col_names]
                    if not valid_cols:
                        continue

                    key_val = str(rec.get(natural_key_field, "")).strip()
                    placeholders = ", ".join(["?"] * len(valid_cols))
                    col_str = ", ".join(valid_cols)

                    # On conflict, never update natural key or primary key
                    update_cols = [c for c in valid_cols if c != natural_key_field and c not in pk_cols]
                    if update_cols:
                        update_assignments = ", ".join([f"{c} = excluded.{c}" for c in update_cols])
                        upsert_sql = f"""
                        INSERT INTO {table_name} ({col_str})
                        VALUES ({placeholders})
                        ON CONFLICT({natural_key_field}) DO UPDATE SET
                        {update_assignments};
                        """
                    else:
                        upsert_sql = f"""
                        INSERT INTO {table_name} ({col_str})
                        VALUES ({placeholders})
                        ON CONFLICT({natural_key_field}) DO NOTHING;
                        """

                    val_list = [rec[c] for c in valid_cols]
                    if key_val in existing_keys:
                        conn.execute(upsert_sql, val_list)
                        updated += 1
                        skipped += 1
                    else:
                        conn.execute(upsert_sql, val_list)
                        if key_val:
                            existing_keys.add(key_val)
                        if pk_name and rec.get(pk_name):
                            existing_pks.add(str(rec[pk_name]).strip())
                        inserted += 1

                conn.commit()

            except Exception as e:
                conn.rollback()
                logger.error(f"execute_upsert_batch failed: {e}", exc_info=True)
                raise e

        return inserted, updated, skipped


    def query_dynamic_records(self, table_name: str, limit: int = 50, offset: int = 0) -> Tuple[int, List[Dict[str, Any]]]:
        """Queries records from any dynamic table. Returns (total_count, records)."""
        with self.get_connection() as conn:
            # Check table exists
            exists = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,)).fetchone()
            if not exists:
                return 0, []

            count_row = conn.execute(f"SELECT COUNT(*) as c FROM {table_name}").fetchone()
            total = count_row["c"] if count_row else 0

            rows = conn.execute(f"SELECT * FROM {table_name} LIMIT ? OFFSET ?", (limit, offset)).fetchall()
            return total, [dict(r) for r in rows]

    def create_snapshot(self, table_name: str, run_id: str) -> str:
        """Creates an execution snapshot of the specified table for rollback."""
        ts = int(datetime.now(timezone.utc).timestamp())
        snapshot_id = f"snap_{table_name}_{run_id}_{ts}"
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        with self.get_connection() as conn:
            rows = conn.execute(f"SELECT * FROM {table_name}").fetchall()
            row_dicts = [dict(r) for r in rows]
            conn.execute(
                """
                INSERT INTO dynamic_snapshots (snapshot_id, table_name, run_id, created_at, row_count, serialized_rows)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (snapshot_id, table_name, run_id, now_iso, len(row_dicts), json.dumps(row_dicts))
            )
            conn.commit()
        return snapshot_id

    def get_latest_snapshot(self, table_name: Optional[str] = None) -> Optional[str]:
        """Returns the most recent snapshot ID for the table (or globally if table_name is None)."""
        with self.get_connection() as conn:
            if table_name:
                row = conn.execute(
                    "SELECT snapshot_id FROM dynamic_snapshots WHERE table_name = ? ORDER BY created_at DESC LIMIT 1",
                    (table_name,)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT snapshot_id FROM dynamic_snapshots ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
            return row["snapshot_id"] if row else None

    def rollback_snapshot(self, snapshot_id: str) -> Tuple[bool, int, int]:
        """Rolls back the dynamic table to the saved snapshot state.
        Returns: (success: bool, removed_records: int, remaining_records: int)"""
        with self.get_connection() as conn:
            snap = conn.execute("SELECT * FROM dynamic_snapshots WHERE snapshot_id = ?", (snapshot_id,)).fetchone()
            if not snap:
                return False, 0, 0

            table_name = snap["table_name"]
            saved_rows = json.loads(snap["serialized_rows"])

            # Check if table exists
            exists = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,)).fetchone()
            if not exists:
                return False, 0, 0

            count_row = conn.execute(f"SELECT COUNT(*) as c FROM {table_name}").fetchone()
            existing_count = count_row["c"] if count_row else 0

            conn.execute("BEGIN TRANSACTION")
            try:
                conn.execute(f"DELETE FROM {table_name};")
                if saved_rows:
                    cols = list(saved_rows[0].keys())
                    placeholders = ", ".join(["?"] * len(cols))
                    col_str = ", ".join(cols)
                    for r in saved_rows:
                        conn.execute(
                            f"INSERT INTO {table_name} ({col_str}) VALUES ({placeholders})",
                            [r[c] for c in cols]
                        )
                conn.commit()
                removed_count = max(0, existing_count - len(saved_rows))
                return True, removed_count, len(saved_rows)
            except Exception as e:
                conn.rollback()
                logger.error(f"rollback_snapshot failed for {snapshot_id}: {e}", exc_info=True)
                raise e

    def save_state(self, key: str, value: Any) -> None:
        """Persists a key-value pair to SQLite so workers never lose state."""
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        val_str = json.dumps(value) if not isinstance(value, str) else value
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO v2_persistent_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at;
                """,
                (key, val_str, now_iso)
            )
            conn.commit()

    def load_state(self, key: str) -> Optional[Any]:
        """Loads a persisted value from SQLite."""
        with self.get_connection() as conn:
            row = conn.execute("SELECT value FROM v2_persistent_state WHERE key = ?", (key,)).fetchone()
            if not row:
                return None
            val_str = row["value"]
            try:
                return json.loads(val_str)
            except Exception:
                return val_str
