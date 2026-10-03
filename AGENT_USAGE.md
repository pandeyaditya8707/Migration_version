# Agent Usage, Governance, and Development Log

This document details how the AI agent operates within the **Agentic Data Migration Planner and Reconciliation Workbench**, including tool specifications, representative prompt patterns, boundaries of autonomy vs. human delegation, documented agent mistakes and their resolutions, and verification methodologies.

---

## 1. Tool Specifications & APIs

The AI agent does not execute arbitrary code or write unvetted SQL directly to the database. Instead, it interacts through a standardized, sandboxed toolbelt:

### A. Inspection & Profiling Tools (`profiler.py`)
- `inspect_source_schema()`: Returns source field names, inferred data types, and nullability flags.
- `inspect_target_schema()`: Returns target schema constraints, mandatory `NOT NULL` fields, enum arrays, and regex patterns.
- `sample_records(n=5)`: Returns a bounded slice of raw source records for inspection.
- `profile_source_column(column_name)`: Computes statistical metrics: total count, non-null count, null percentage, distinct values, and pattern indicators (e.g. `CONTAINS_COMMAS`, `DATE_SEPARATORS_DETECTED`, `CURRENCY_SYMBOLS`).
- `infer_schema_from_records(records, dataset_name)`: Analyzes arbitrary user-uploaded CSV/Excel rows to dynamically generate a valid schema specification.

### B. Pure Transformation Tool Registry (`transforms.py`)
Enforces **zero arbitrary code execution** (`eval`/`exec`). The agent can only select from 12 pre-compiled, deterministic functions:
1. `DIRECT_COPY`: Identity pass-through with null checks.
2. `TRIM_CLEAN`: Strips extraneous whitespace, control characters, and tabs.
3. `SPLIT_NAME`: Splits composite names into `first` or `last` parts with multi-token handling.
4. `CONCAT_WS`: Concatenates multiple fields with a configurable separator.
5. `DATE_TO_ISO8601`: Parses heterogeneous date formats (`YYYY-MM-DD`, `MM/DD/YYYY`, timestamps) into standard UTC ISO-8601 strings.
6. `PHONE_TO_E164`: Sanitizes and reformats phone numbers to international standard `+1XXXXXXXXXX`.
7. `EMAIL_NORMALIZE`: Lowercases and validates against RFC 5322 regex.
8. `CLEAN_CURRENCY_TO_FLOAT`: Cleans currency symbols (`$`), commas, and accounting parentheses `(50.00)` into signed floats.
9. `ENUM_LOOKUP`: Maps legacy codes to target enum values with a safe fallback default.
10. `COUNTRY_TO_ISO2`: Standardizes country names and abbreviations to 2-letter ISO 3166-1 alpha-2 codes.
11. `COALESCE_VAL`: Applies fallback values if the source evaluates to null or empty.
12. `UUID_V5_FROM_KEY`: Generates deterministic, idempotent UUIDv5 identifiers from natural keys using a fixed namespace.

### C. Plan & Simulation Tools (`dry_run.py`, `history.py`)
- `DryRunEngine.execute_dry_run(plan, records)`: Executes an in-memory simulation without writing to the database, returning accepted records, quarantined records, and field-level error evidence.
- `PlanManager.save_plan(plan, actor)`: Persists versioned plans into the SQLite store.

---

## 2. Representative Prompts & Agent Interactions

### Prompt 1: Propose Migration Plan
```text
USER:
"I have ingested a legacy CRM dataset with 1,000 records. Target requires a modern customer schema with strict NOT NULL constraints on first_name and email. Propose an initial migration plan."

AGENT BEHAVIOR:
1. Calls inspect_source_schema() and inspect_target_schema().
2. Profiles high-risk columns (full_name_raw, email_address, balance_due_str).
3. Produces a MigrationPlan with 11 field mappings.
4. Identifies that ~1.4% of records have empty names and ~2.5% have malformed emails.
5. Flags these mappings as HIGH risk and generates clarification questions.
```

### Prompt 2: Handle Edge-Case Ambiguities
```text
USER:
"What should we do with account_status_code 'X' and truncated emergency phone numbers like '911'?"

AGENT BEHAVIOR:
Presents interactive clarification choices:
- For status 'X': Fallback to 'SUSPENDED' (Recommended) vs. Quarantine record.
- For '911': Quarantine entire record for strict data hygiene (Recommended) vs. set phone to null and import remainder of record.
```

### Prompt 3: Execute Migration
```text
USER:
"Execute Plan v1 into the target store."

AGENT BEHAVIOR:
Checks plan.status. If status == 'PROPOSED', halts execution with:
"Execution blocked: Plan v1 has status 'PROPOSED'. A human engineer must explicitly review and approve the plan before execution."
```

---

## 3. Delegated Work vs. Human Authority

To maintain zero-loss data governance, strict separation of concerns is maintained:

```
┌──────────────────────────────────────────────┐
│        DELEGATED TO AI AGENT                 │
│  - Statistical column profiling              │
│  - Dynamic schema detection from uploads     │
│  - Semantic column name matching             │
│  - Suggesting deterministic transforms       │
│  - Calculating risk levels (LOW/MEDIUM/HIGH) │
│  - Generating targeted clarification questions│
└──────────────────────┬───────────────────────┘
                       │ Human-in-the-Loop Barrier
                       ▼
┌──────────────────────────────────────────────┐
│        RESERVED FOR HUMAN OPERATOR           │
│  - Deciding ambiguous business policies      │
│  - Plan approval & cryptographic sign-off    │
│  - Authorizing execution into target DB      │
│  - Triggering snapshot rollback              │
│  - Inspecting & releasing quarantined data   │
└──────────────────────────────────────────────┘
```

---

## 4. Documented Agent Mistakes & Resolutions

During the design and implementation of the workbench, several edge-case errors were intercepted and corrected:

### Mistake 1: Substring Collision on Date Detection (`"at"` in `"status"`)
- **The Issue**: In dynamic plan generation, the agent used the heuristic:
  ```python
  elif "date" in t_name.lower() or "time" in t_name.lower() or "at" in t_name.lower():
      transform_name = "DATE_TO_ISO8601"
  ```
  Because the word `"status"` contains `"at"` (`st-at-us`), the target column `status` was classified as a date! When the dry run executed, status codes were parsed as timestamps (`1970-01-01T00:00:01Z`), which then violated the target enum list `['ACTIVE', 'INACTIVE', 'SUSPENDED', 'TERMINATED']`.
- **Resolution**: Refined the keyword check to exact tokens: `any(w in t_name.lower() for w in ["_at", "date", "time", "timestamp"])` and placed enum constraint checks higher in precedence.

### Mistake 2: Generic `"UNKNOWN"` Fallback Violating Regex Constraints
- **The Issue**: When target field `country_iso2` was not found in the source dataset, the agent defaulted it to `"UNKNOWN"`. However, `country_iso2` had a strict regex constraint: `^[A-Z]{2}$`. Because `"UNKNOWN"` has 7 letters, all valid records failed validation and were quarantined.
- **Resolution**: Added constraint-aware fallback resolution: if a target field enforces a 2-letter ISO regex, the default fallback is set to `"US"`; if numeric, `0.0`; if enum, the first enum value.

### Mistake 3: Volatile In-Memory Plan Storage
- **The Issue**: Plans were initially held in an in-memory Python dictionary (`_plans = {}`). When the server reloaded during file uploads, `_plans` reset, causing `404 Not Found` when requesting `Plan v2`.
- **Resolution**: Created a persistent SQLite table `migration_plans` and backed `PlanManager` with database transactions. Added auto-healing fallback if an older plan version is requested.

### Mistake 4: Type Mismatch in Duplicate Detection Causing `UNIQUE constraint failed`
- **The Issue**: When checking if a record was already migrated, the code performed `if nat_key in existing_keys:`. For uploaded datasets, numeric keys were parsed as integers (`101`), whereas SQLite returned strings (`'101'`). In Python, `101 in {'101': ...}` evaluates to `False`. As a result, the executor attempted to insert duplicate keys, causing SQLite to throw `UNIQUE constraint failed: customers.natural_key`. Furthermore, newly inserted keys within the same batch were not cached in `existing_keys`.
- **Resolution**:
  1. Normalized all natural keys with `str(key).strip()`.
  2. Updated `existing_keys[nat_key] = customer_uuid` immediately after insert.
  3. Added database-level atomic UPSERT (`ON CONFLICT(natural_key) DO UPDATE SET ...`).

### Mistake 5: Binary Parsing of Excel Spreadsheets
- **The Issue**: When users uploaded Microsoft Excel spreadsheets (`.xlsx`), the file reader attempted to decode raw bytes as UTF-8 CSV text, producing corrupted rows or decode errors.
- **Resolution**: Integrated `openpyxl` to inspect file extensions and parse binary `.xlsx` sheets into structured record dictionaries.

### Mistake 6: SQLite DDL Constraint Placement Order in Dynamic Store
- **The Issue**: During dynamic SQLite table generation in V2, column-level definitions and table-level constraints (`UNIQUE(natural_key)`) were generated in arbitrary order, placing `UNIQUE(...)` in between column declarations. SQLite raised `OperationalError: near "...": syntax error`.
- **Resolution**: Segregated column definitions from table-level constraints in `DynamicDatabaseStore.compile_and_create_table()`. All column definitions are assembled first, followed by trailing `UNIQUE(...)` table constraints.

### Mistake 7: V2 Ollama Structured Output Mode & Fallback
- **The Issue**: LLM model output from Ollama Cloud could occasionally include surrounding conversational markdown (e.g. ````json ... ````) or experience latency timeouts.
- **Resolution**: Implemented `format="json"` in `OllamaPlannerAgent`, added a robust JSON block extractor regex, and built an automated heuristic fallback so the studio functions reliably even if offline or without internet access.

---

## 5. Verification Methodology

Every agent output and pipeline component is verified through five levels of testing:

1. **Deterministic Unit Tests**:
   - Tested 7 core transforms in isolation ([test_transforms.py](file:///Users/adityapandey/data_mig/backend/tests/test_transforms.py)) verifying phone formatting, currency cleaning, name splitting, and UUIDv5 determinism.
2. **Pipeline Integration Tests**:
   - Verified plan generation, dry-run simulation, approval gating, and execution rollback in [test_migration_pipeline.py](file:///Users/adityapandey/data_mig/backend/tests/test_migration_pipeline.py).
3. **Dynamic DDL & Arbitrary Schema Tests (V2)**:
   - Verified DDL compilation, dynamic upserts, and V2 API lifecycle in [test_v2_dynamic_migration.py](file:///Users/adityapandey/data_mig/backend/tests/test_v2_dynamic_migration.py).
4. **End-to-End Live HTTP Tests**:
   - Verified live server responses for both Mode 1 and Mode 2 using [verify_v2_e2e_live.py](file:///Users/adityapandey/data_mig/backend/tests/verify_v2_e2e_live.py) across all operational steps.
5. **Mathematical Reconciliation Invariant**:
   - Asserted that $\Delta = \text{Source} - (\text{Target} + \text{Quarantine} + \text{Duplicates}) \equiv 0$ on every execution run.
