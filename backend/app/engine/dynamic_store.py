from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "target_store.db")

IDENTIFIER_REGEX = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")

RESERVED_TABLE_NAMES = {
    "dynamic_schemas",
    "dynamic_snapshots",
    "v2_quarantine_ledger",
    "v2_audit_ledger",
    "sqlite_master",
    "sqlite_sequence",
    "sqlite_stat1",
    "sqlite_temp_master",
    "customers",
    "quarantine_ledger",
    "target_snapshots",
    "audit_ledger",
    "migration_plans",
}


def validate_sql_identifier(name: str, label: str = "identifier") -> str:
    """Strictly validates SQL table and column identifiers against injection and reserved system names."""
    clean = str(name).strip()
    if not IDENTIFIER_REGEX.match(clean):
        raise ValueError(
            f"Invalid SQL {label} '{name}'. Identifiers must start with a letter or underscore, "
            f"contain only alphanumeric characters or underscores, and be at most 64 characters."
        )
    if "table" in label.lower() and (clean.lower() in RESERVED_TABLE_NAMES or clean.lower().startswith("sqlite_")):
        raise ValueError(
            f"Security violation: Table name '{clean}' is a reserved internal system/ledger table and cannot be modified."
        )
    return clean


class DynamicDatabaseStore:
    """Dynamic SQLite storage engine capable of compiling arbitrary target schemas
    into tables with type affinities, constraints, atomic upserts, snapshots, and rollbacks."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or os.environ.get("DATABASE_PATH") or DB_PATH
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
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
            conn.execute("""
            CREATE TABLE IF NOT EXISTS v2_quarantine_ledger (
                quarantine_id TEXT PRIMARY KEY,
                table_name TEXT NOT NULL,
                run_id TEXT NOT NULL,
                source_row_index INT NOT NULL,
                source_natural_key TEXT,
                source_payload TEXT NOT NULL,
                errors TEXT NOT NULL,
                ai_suggestion TEXT,
                created_at TEXT NOT NULL
            );
            """)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS v2_audit_ledger (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                actor TEXT NOT NULL,
                table_name TEXT NOT NULL,
                run_id TEXT NOT NULL,
                details TEXT NOT NULL,
                timestamp TEXT NOT NULL
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

    def compile_and_create_table(self, schema_dict: dict[str, Any]) -> str:
        """Compiles arbitrary target schema JSON to SQLite DDL and creates/evolves the table safely."""
        raw_table_name = schema_dict.get("table_name", "target_records")
        table_name = validate_sql_identifier(raw_table_name, "table name")

        raw_nk = schema_dict.get("natural_key", schema_dict.get("primary_key", "id"))
        raw_pk = schema_dict.get("primary_key", raw_nk)
        natural_key_str = raw_nk[0] if isinstance(raw_nk, list) and raw_nk else str(raw_nk)
        primary_key_str = raw_pk[0] if isinstance(raw_pk, list) and raw_pk else str(raw_pk)

        natural_key = validate_sql_identifier(natural_key_str, "natural key")
        primary_key = validate_sql_identifier(primary_key_str, "primary key")
        fields = schema_dict.get("fields", [])

        if not fields:
            raise ValueError(f"Target schema for table '{table_name}' must define at least one field.")

        col_defs: list[str] = []
        field_names = set()

        for f in fields:
            name = validate_sql_identifier(f["name"], "column name")
            field_names.add(name)
            col_type = self.map_to_sqlite_type(f.get("data_type", "string"))
            nullable = f.get("nullable", True)
            constraints = f.get("constraints", {})

            parts = [f'"{name}"', col_type]
            if name == primary_key:
                parts.append("PRIMARY KEY")
            elif not nullable:
                parts.append("NOT NULL")

            if constraints.get("unique") and name != primary_key:
                parts.append("UNIQUE")

            enum_vals = constraints.get("allowed_values") or constraints.get("enum")
            if enum_vals and isinstance(enum_vals, list) and enum_vals:
                escaped_vals = [f"'{str(v).replace(chr(39), chr(39) + chr(39))}'" for v in enum_vals]
                allowed_str = ", ".join(escaped_vals)
                if nullable:
                    parts.append(f'CHECK ("{name}" IS NULL OR "{name}" IN ({allowed_str}))')
                else:
                    parts.append(f'CHECK ("{name}" IN ({allowed_str}))')

            col_defs.append(" ".join(parts))

        # Injected audit columns
        col_defs.append('"migration_run_id" TEXT NOT NULL')
        col_defs.append('"migrated_at" TEXT NOT NULL')

        table_constraints: list[str] = []
        if natural_key not in field_names:
            col_defs.append(f'"{natural_key}" TEXT UNIQUE NOT NULL')
        elif natural_key != primary_key and f'"{natural_key}" TEXT UNIQUE' not in " ".join(col_defs):
            table_constraints.append(f'UNIQUE("{natural_key}")')

        all_items = col_defs + table_constraints
        ddl = f'CREATE TABLE IF NOT EXISTS "{table_name}" (\n  ' + ",\n  ".join(all_items) + "\n);"

        with self.get_connection() as conn:
            # Safe non-destructive evolution: inspect existing columns
            cur = conn.execute(f'PRAGMA table_info("{table_name}");')
            existing_col_info = cur.fetchall()
            if existing_col_info:
                existing_cols = {row[1] for row in existing_col_info}
                missing_cols = field_names - existing_cols
                if missing_cols:
                    for f in fields:
                        fn = f["name"].strip()
                        if fn in missing_cols:
                            c_type = self.map_to_sqlite_type(f.get("data_type", "string"))
                            conn.execute(f'ALTER TABLE "{table_name}" ADD COLUMN "{fn}" {c_type};')
                    logger.info(f"Safely evolved existing table '{table_name}' with {len(missing_cols)} added columns.")
            else:
                conn.execute(ddl)

            # Register schema metadata
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            schema_id = schema_dict.get("schema_id", f"schema_{table_name}")
            conn.execute(
                """
                INSERT OR REPLACE INTO dynamic_schemas (schema_id, table_name, serialized_schema, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (schema_id, table_name, json.dumps(schema_dict), now_iso),
            )
            conn.commit()

        return ddl

    def execute_upsert_batch(
        self, table_name: str, natural_key_field: str, rows: list[dict[str, Any]], run_id: str
    ) -> tuple[int, int, int]:
        """Atomically inserts or updates records with idempotency and duplicate prevention.
        Returns: (inserted_count, updated_count, skipped_duplicates)."""
        if not rows:
            return 0, 0, 0

        clean_table_name = validate_sql_identifier(table_name, "table name")

        if isinstance(natural_key_field, list):
            natural_key_field = natural_key_field[0] if natural_key_field else "id"
        clean_nk = validate_sql_identifier(natural_key_field or "id", "natural key field")

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        inserted = 0
        updated = 0
        skipped = 0

        with self.get_connection() as conn:
            # 1. Inspect table columns and constraints
            cur = conn.execute(f'PRAGMA table_info("{clean_table_name}");')
            col_info = cur.fetchall()
            if not col_info:
                raise RuntimeError(f"Target table '{clean_table_name}' does not exist in SQLite database.")

            existing_col_names = {row[1] for row in col_info}
            ordered_col_names = [row[1] for row in col_info]
            col_types_map = {row[1]: (row[2] or "TEXT") for row in col_info}
            not_null_cols = {row[1]: (row[2] or "TEXT") for row in col_info if row[3] == 1 and row[4] is None}

            # 2. Check if any incoming keys in rows are missing from the table; dynamically ADD COLUMN if needed
            all_incoming_keys = set().union(*(r.keys() for r in rows))
            all_incoming_keys.add("migration_run_id")
            all_incoming_keys.add("migrated_at")
            for missing_col in all_incoming_keys - existing_col_names:
                try:
                    clean_missing = validate_sql_identifier(missing_col, "column name")
                    conn.execute(f'ALTER TABLE "{clean_table_name}" ADD COLUMN "{clean_missing}" TEXT;')
                    existing_col_names.add(clean_missing)
                    ordered_col_names.append(clean_missing)
                    col_types_map[clean_missing] = "TEXT"
                    logger.info(f"Dynamically added missing column '{clean_missing}' to table '{clean_table_name}'.")
                except Exception as alter_err:
                    logger.warning(f"Could not add column '{missing_col}' to '{clean_table_name}': {alter_err}")

            # 3. Ensure natural_key_field exists in existing_col_names deterministically
            pk_cols = [row[1] for row in col_info if row[5] > 0]
            pk_name = pk_cols[0] if pk_cols else None
            if clean_nk not in existing_col_names:
                clean_nk = pk_name if pk_name else (ordered_col_names[0] if ordered_col_names else "id")

            # 4. Ensure a UNIQUE index exists on natural_key_field for ON CONFLICT
            try:
                conn.execute(
                    f'CREATE UNIQUE INDEX IF NOT EXISTS "idx_{clean_table_name}_{clean_nk}" ON "{clean_table_name}"("{clean_nk}");'
                )
                conn.commit()
            except Exception as idx_err:
                logger.debug(f"Unique index creation note on {clean_table_name}.{clean_nk}: {idx_err}")

            # 5. Query existing keys and pre-fetch existing row snapshots for exact change detection
            existing_rows = conn.execute(f'SELECT "{clean_nk}" FROM "{clean_table_name}"').fetchall()
            existing_keys = {str(r[clean_nk]).strip() for r in existing_rows if r[clean_nk] is not None}
            existing_row_data = {}
            if clean_nk:
                try:
                    cur_rows = conn.execute(f'SELECT * FROM "{clean_table_name}"').fetchall()
                    for cr in cur_rows:
                        nk = str(cr[clean_nk]).strip() if cr[clean_nk] is not None else ""
                        if nk:
                            existing_row_data[nk] = dict(cr)
                except Exception as e:
                    logger.debug(f"Could not pre-fetch existing rows: {e}")

            existing_pks = set()
            if pk_name:
                pk_db_rows = conn.execute(f'SELECT "{pk_name}" FROM "{clean_table_name}"').fetchall()
                existing_pks = {str(r[pk_name]).strip() for r in pk_db_rows if r[pk_name] is not None}

            conn.execute("BEGIN TRANSACTION")
            try:
                import uuid as _uuid

                seen_batch_pks = set()

                for r in rows:
                    rec = dict(r)
                    rec["migration_run_id"] = run_id
                    rec["migrated_at"] = now_iso

                    # Strict NOT NULL validation: NEVER fabricate fake defaults (e.g. 0, 0.0, "")
                    for col_name, _col_type in not_null_cols.items():
                        is_missing = col_name not in rec or rec[col_name] is None
                        if is_missing:
                            if col_name in pk_cols or col_name.endswith("_uuid"):
                                rec[col_name] = str(_uuid.uuid4())
                            else:
                                raise ValueError(
                                    f"NOT NULL constraint failed on '{clean_table_name}.{col_name}': "
                                    f"Cannot fabricate missing required data for natural key '{rec.get(clean_nk)}'."
                                )

                    # Filter to only columns that actually exist in the table
                    valid_cols = [c for c in rec if c in existing_col_names]
                    if not valid_cols:
                        continue

                    # Strict type enforcement: Coerce/validate numbers before database insertion
                    for c in valid_cols:
                        c_type = col_types_map.get(c, "TEXT").upper()
                        raw_v = rec[c]
                        if raw_v is not None and not (isinstance(raw_v, (dict, list))):
                            str_v = str(raw_v).strip()
                            if ("REAL" in c_type or "FLOAT" in c_type or "NUMERIC" in c_type) and str_v != "":
                                try:
                                    rec[c] = float(str_v.replace("$", "").replace(",", ""))
                                except (ValueError, TypeError):
                                    raise ValueError(
                                        f"Data type constraint violation on '{c}': expected {c_type}, "
                                        f"got invalid value '{raw_v}'"
                                    )
                            elif ("INT" in c_type) and str_v != "":
                                try:
                                    rec[c] = int(str_v)
                                except (ValueError, TypeError):
                                    raise ValueError(
                                        f"Data type constraint violation on '{c}': expected {c_type}, "
                                        f"got invalid value '{raw_v}'"
                                    )

                    # Ensure primary key uniqueness across the batch and against existing table records
                    if pk_name and pk_name in rec:
                        cur_pk = str(rec[pk_name]).strip()
                        key_val_check = str(rec.get(clean_nk, "")).strip()
                        if (cur_pk in seen_batch_pks) or (
                            cur_pk in existing_pks and key_val_check not in existing_keys
                        ):
                            rec[pk_name] = str(_uuid.uuid4())
                            cur_pk = rec[pk_name]
                        seen_batch_pks.add(cur_pk)

                    key_val = str(rec.get(clean_nk, "")).strip()
                    placeholders = ", ".join(["?"] * len(valid_cols))
                    col_str = ", ".join(f'"{c}"' for c in valid_cols)

                    # On conflict, never update natural key or primary key
                    update_cols = [c for c in valid_cols if c != clean_nk and c not in pk_cols]
                    if update_cols:
                        update_assignments = ", ".join([f'"{c}" = excluded."{c}"' for c in update_cols])
                        upsert_sql = f"""
                        INSERT INTO "{clean_table_name}" ({col_str})
                        VALUES ({placeholders})
                        ON CONFLICT("{clean_nk}") DO UPDATE SET
                        {update_assignments};
                        """
                    else:
                        upsert_sql = f"""
                        INSERT INTO "{clean_table_name}" ({col_str})
                        VALUES ({placeholders})
                        ON CONFLICT("{clean_nk}") DO NOTHING;
                        """

                    val_list = [rec[c] for c in valid_cols]

                    if key_val and key_val in existing_keys:
                        conn.execute(upsert_sql, val_list)
                        if update_cols:
                            updated += 1
                        else:
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

    def query_dynamic_records(
        self, table_name: str, limit: int = 50, offset: int = 0
    ) -> tuple[int, list[dict[str, Any]]]:
        """Queries records from any dynamic table. Returns (total_count, records)."""
        clean_table_name = validate_sql_identifier(table_name, "table name")
        with self.get_connection() as conn:
            exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (clean_table_name,)
            ).fetchone()
            if not exists:
                return 0, []

            count_row = conn.execute(f'SELECT COUNT(*) as c FROM "{clean_table_name}"').fetchone()
            total = count_row["c"] if count_row else 0

            rows = conn.execute(f'SELECT * FROM "{clean_table_name}" LIMIT ? OFFSET ?', (limit, offset)).fetchall()
            return total, [dict(r) for r in rows]

    def create_snapshot(self, table_name: str, run_id: str) -> str:
        """Creates an execution snapshot of the specified table for rollback."""
        clean_table_name = validate_sql_identifier(table_name, "table name")
        snap_token = uuid.uuid4().hex[:12]
        snapshot_id = f"snap_{clean_table_name}_{run_id}_{snap_token}"
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        with self.get_connection() as conn:
            rows = conn.execute(f'SELECT * FROM "{clean_table_name}"').fetchall()
            row_dicts = [dict(r) for r in rows]
            conn.execute(
                """
                INSERT INTO dynamic_snapshots (snapshot_id, table_name, run_id, created_at, row_count, serialized_rows)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (snapshot_id, clean_table_name, run_id, now_iso, len(row_dicts), json.dumps(row_dicts)),
            )
            conn.commit()
        return snapshot_id

    def get_latest_snapshot(self, table_name: str | None = None) -> str | None:
        """Returns the most recent snapshot ID for the table (or globally if table_name is None)."""
        with self.get_connection() as conn:
            if table_name:
                clean_table_name = validate_sql_identifier(table_name, "table name")
                row = conn.execute(
                    "SELECT snapshot_id FROM dynamic_snapshots WHERE table_name = ? ORDER BY created_at DESC LIMIT 1",
                    (clean_table_name,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT snapshot_id FROM dynamic_snapshots ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
            return row["snapshot_id"] if row else None

    def rollback_snapshot(self, snapshot_id: str) -> tuple[bool, int, int]:
        """Rolls back the dynamic table to the saved snapshot state.
        Returns: (success: bool, removed_records: int, remaining_records: int)"""
        with self.get_connection() as conn:
            snap = conn.execute("SELECT * FROM dynamic_snapshots WHERE snapshot_id = ?", (snapshot_id,)).fetchone()
            if not snap:
                return False, 0, 0

            clean_table_name = validate_sql_identifier(snap["table_name"], "table name")
            saved_rows = json.loads(snap["serialized_rows"])

            # Check if table exists
            exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (clean_table_name,)
            ).fetchone()
            if not exists:
                return False, 0, 0

            count_row = conn.execute(f'SELECT COUNT(*) as c FROM "{clean_table_name}"').fetchone()
            existing_count = count_row["c"] if count_row else 0

            conn.execute("BEGIN TRANSACTION")
            try:
                conn.execute(f'DELETE FROM "{clean_table_name}";')
                if saved_rows:
                    cols = list(saved_rows[0].keys())
                    placeholders = ", ".join(["?"] * len(cols))
                    col_str = ", ".join(f'"{c}"' for c in cols)
                    for r in saved_rows:
                        conn.execute(
                            f'INSERT INTO "{clean_table_name}" ({col_str}) VALUES ({placeholders})',
                            [r[c] for c in cols],
                        )
                conn.commit()
                removed_count = max(0, existing_count - len(saved_rows))
                return True, removed_count, len(saved_rows)
            except Exception as e:
                conn.rollback()
                logger.error(f"rollback_snapshot failed for {snapshot_id}: {e}", exc_info=True)
                raise e

    def save_v2_quarantine_records(
        self, table_name_or_run_id: str, run_id_or_records: Any, records: list[Any] | None = None
    ) -> None:
        """Persists Mode 2 quarantined records to SQLite for durability.
        Supports both (table_name, run_id, records) and (run_id, records)."""
        if records is None:
            table_name = "target_records"
            run_id = str(table_name_or_run_id)
            actual_records = run_id_or_records or []
        else:
            table_name = str(table_name_or_run_id)
            run_id = str(run_id_or_records)
            actual_records = records or []

        if not actual_records:
            return
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.get_connection() as conn:
            for q in actual_records:
                q_id = getattr(q, "quarantine_id", None) or f"q_{run_id}_{getattr(q, 'source_row_index', 0)}"
                s_idx = getattr(q, "source_row_index", 0)
                nat_key = getattr(q, "source_natural_key", None)
                payload = getattr(q, "source_payload", {})
                errs = getattr(q, "errors", [])
                err_data = [e.model_dump() if hasattr(e, "model_dump") else e for e in errs]
                sugg = getattr(q, "ai_remediation_summary", "")
                conn.execute(
                    """
                    INSERT OR REPLACE INTO v2_quarantine_ledger (
                        quarantine_id, table_name, run_id, source_row_index, source_natural_key,
                        source_payload, errors, ai_suggestion, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        q_id,
                        table_name,
                        run_id,
                        s_idx,
                        str(nat_key) if nat_key else None,
                        json.dumps(payload),
                        json.dumps(err_data),
                        sugg,
                        now_iso,
                    ),
                )
            conn.commit()

    def load_v2_quarantine_records(
        self, table_name: str | None = None, run_id: str | None = None, limit: int = 50, offset: int = 0
    ) -> tuple[int, list[dict[str, Any]]]:
        """Loads Mode 2 quarantined records from SQLite."""
        with self.get_connection() as conn:
            where_clauses = []
            params = []
            if table_name:
                clean_table = validate_sql_identifier(table_name, "table name")
                where_clauses.append("table_name = ?")
                params.append(clean_table)
            if run_id:
                where_clauses.append("run_id = ?")
                params.append(run_id)
            where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

            count_row = conn.execute(f"SELECT COUNT(*) as c FROM v2_quarantine_ledger {where_sql}", params).fetchone()
            total = count_row["c"] if count_row else 0

            query_sql = f"SELECT * FROM v2_quarantine_ledger {where_sql} ORDER BY source_row_index ASC LIMIT ? OFFSET ?"
            rows = conn.execute(query_sql, params + [limit, offset]).fetchall()
            results = []
            for r in rows:
                item = dict(r)
                try:
                    item["source_payload"] = json.loads(item["source_payload"])
                except Exception:
                    pass
                try:
                    item["errors"] = json.loads(item["errors"])
                except Exception:
                    pass
                results.append(item)
            return total, results

    def log_v2_audit_event(
        self,
        event_id: str,
        event_type: str,
        actor: str = "System",
        details: Any = None,
        table_name: str = "v2_target",
        run_id: str = "",
    ) -> None:
        """Persists Mode 2 audit events to SQLite."""
        if details is None:
            details = {}
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        det_str = json.dumps(details) if not isinstance(details, str) else details
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO v2_audit_ledger (event_id, event_type, actor, table_name, run_id, details, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (event_id, event_type, actor, table_name, run_id, det_str, now_iso),
            )
            conn.commit()

    def load_v2_audit_events(self, limit: int = 50) -> list[dict[str, Any]]:
        """Loads Mode 2 audit events from SQLite."""
        with self.get_connection() as conn:
            rows = conn.execute("SELECT * FROM v2_audit_ledger ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()
            results = []
            for r in rows:
                item = dict(r)
                try:
                    item["details"] = json.loads(item["details"])
                except Exception:
                    pass
                results.append(item)
            return results

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
                (key, val_str, now_iso),
            )
            conn.commit()

    def load_state(self, key: str) -> Any | None:
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
