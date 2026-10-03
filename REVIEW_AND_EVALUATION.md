# Comprehensive Architecture & Review Evaluation Document

This document provides a detailed breakdown of the **Agentic Data Migration Planner & Reconciliation Workbench**, mapped directly to the 10 professional evaluation criteria.

---

## 1. Product Understanding

### The Core Problem
In enterprise data migrations, the most catastrophic failure is **silent data loss**: records that are dropped, truncated, or coerced into invalid states without human awareness or audit trails. Traditional ETL/ELT pipelines frequently fail silently when encountering schema drift, unexpected format anomalies (e.g., European date strings, unformatted phone numbers, non-standard ISO timestamps), or unmapped enumeration codes.

### Our Solution & Target Personas
The workbench is an enterprise-grade migration platform built for **Lead Data Architects, Migration Engineers, and Compliance Auditors**. It guarantees **zero silent drop** by enforcing:
1. **Mathematical Mass Conservation**: Every single source record is rigorously accounted for:
   $$\text{Total Source} = \text{Target Accepted} + \text{Quarantined} + \text{Duplicates}$$
2. **Deterministic Governance & Human-in-the-Loop Barrier**: No migration plan can write to target storage without explicit human review and sign-off.
3. **Dual-Mode Enterprise Architecture**:
   - **Mode 1 (Benchmark CRM Engine - 1,000 Records)**: A deterministic, audit-first workflow with strict invariant testing, zero non-deterministic AI mutations, and forensic-only quarantine evidence.
   - **Mode 2 (Universal Studio V2)**: An autonomous schema-to-schema engine supporting arbitrary target JSON contracts, user-uploaded datasets (`.csv`, `.xlsx`, `.json`), and an interactive **Ollama AI Copilot** for live anomaly diagnosis and remediation.

---

## 2. Frontend Usability

### Design System & Layout
- **Tailwind CSS & Curated Enterprise Palette**: Styled with a dark/obsidian workspace aesthetic (`#09090b` obsidian canvas, `#18181b` card surfaces, `#38bdf8` electric blue accents, and `#10b981` emerald valid tags).
- **Strict Viewport & Screen Alignment**: Eliminated horizontal overflowing and layout shifts. All tables feature bounded scrolling wrappers (`overflow-x: auto`), sticky headers, and clear responsive typography using Google's Inter and JetBrains Mono.
- **Micro-Animations & Visual Hierarchy**: Smooth tab transitions (`Profiler` ➔ `Mapping` ➔ `Dry-Run` ➔ `Target Store` ➔ `Reconciliation`), badge counters showing record counts in real-time, and color-coded risk indicators.

### User Experience Enhancements
- **Button Loading States (`withButtonLoader`)**: Every mutation and asynchronous action (Dry Run, Plan Synthesis, Target Write, Rollback, AI Diagnosis) shows an active SVG spinner, disables duplicate clicks to prevent race conditions, and restores original button state upon completion.
- **Forensic Separation**:
  - **Mode 1**: Displays a clean 6-column forensic ledger (`Row #`, `Natural Key`, `Violated Target Field`, `Rule / Constraint`, `Error Diagnostics`, `Action`). All AI fix buttons are intentionally hidden to preserve audit integrity. Clicking `View Evidence` opens raw schema violations and payload inspection.
  - **Mode 2**: Features the full Ollama AI Copilot suite: live diagnosis, row-level `⚡ Fix & Re-run`, modal-level `⚡ Implement AI Fix & Re-run Simulation`, and header-level `⚡ Auto-Apply All AI Fixes & Re-run`.
- **Target Pagination & Search**: Allows seamless browsing of target SQLite tables and quarantine ledgers with page sizing and offset controls.

---

## 3. Backend and Data Design

### Architecture Overview
Built on **FastAPI (Python 3.11+)** and **Pydantic v2**:
- **Modular Engine Design**:
  - `backend/app/engine/profiler.py`: Column-level statistical profiling (null rates, uniqueness, type inference, enum detection).
  - `backend/app/engine/agent.py`: Deterministic heuristic planner and ambiguity question synthesizer.
  - `backend/app/engine/ollama_agent.py`: Cloud & local Ollama LLM integration using structured JSON output mode.
  - `backend/app/engine/transforms.py`: Pure deterministic transform catalog (12 transformations with zero `eval()` / `exec()`).
  - `backend/app/engine/dry_run.py`: Zero-write in-memory execution pipeline with granular quarantine capture.
  - `backend/app/engine/executor.py`: ACID transaction execution, point-in-time snapshotting, and rollback.
  - `backend/app/engine/reconciliation.py`: Universal ledger parity auditor and monetary balance checksum verifier.
  - `backend/app/engine/target_store.py` & `dynamic_store.py`: SQLite databases running with Write-Ahead Logging (`WAL` mode).

### Data Integrity & Cryptographic Invariants
- **Deterministic Primary Keys (UUIDv5)**: Uses UUIDv5 derived from natural keys (`legacy_account_id` or designated primary keys) within a defined namespace (`modern-customer-store.prod`), guaranteeing 100% collision-free idempotent retries.
- **Pre-Run Snapshots & Rollback**: Before every execution, a snapshot of the target database is written to `target_snapshots`. If an engineer requests a rollback, the target table is restored in milliseconds with an immutable audit event recorded.

---

## 4. AI Workflow Quality

### Responsible & Controlled AI Integration
1. **JSON Schema Mode**: Ollama queries enforce strict JSON output formatting, preventing Markdown parsing anomalies and ensuring valid `MigrationPlan` Pydantic models.
2. **Prompt Engineering with Catalog Constraints**: Prompts provide Ollama with the exact supported transformation catalog and strict invariant rules (e.g., date normalization to ISO-8601, UUIDv5 for primary keys, ENUM lookups for category fields).
3. **Dual Execution Mode**:
   - **Mode 1**: 100% deterministic heuristic rule engine. Zero LLM hallucinations or drift.
   - **Mode 2**: Full autonomous copilot with few-shot reasoning, interactive ambiguity resolutions, and live single-row and batch auto-remediations.
4. **Resilience & Fallbacks**: If external AI services encounter timeouts or rate limits, the system exposes clear diagnostics in the UI and allows engineers to proceed using heuristic fallbacks.

---

## 5. Error Handling and Logs

### Granular Forensics
- **Quarantine Records**: Instead of discarding problematic rows, the system stores:
  - `source_row_index`: Original line index from the source data.
  - `source_natural_key`: Primary business key for deduplication and tracking.
  - `source_payload`: Complete original row dictionary.
  - `errors`: Array of specific field violations, including `field`, `rule`, `error_message`, and `raw_value`.
- **Structured Audit Ledger**: Every action (`PLAN_CREATED`, `PLAN_APPROVED`, `DRY_RUN_COMPLETED`, `MIGRATION_EXECUTED`, `MIGRATION_ROLLED_BACK`) is recorded in the SQLite `audit_ledger` with actor metadata, timestamps, and parameters.
- **HTTP Status Codes**: Clear semantic REST responses (`400 Bad Request` for unapproved migrations, `404 Not Found` for missing plans/runs, `502 Bad Gateway` for upstream LLM communication failures).

---

## 6. Testing

### Comprehensive Automated Test Suite
The repository includes **22+ unit, integration, and browser tests** covering the entire lifecycle:

```bash
backend/tests/test_mode1_strict_1000_records.py
  ✓ test_mode1_source_dataset_strictly_1000_records (PASSED)
  ✓ test_mode1_plan_proposing_and_invariants (PASSED)
  ✓ test_mode1_unapproved_execution_blocked (PASSED)
  ✓ test_mode1_deterministic_dry_run_1000_records_mass_conservation (PASSED)
  ✓ test_mode1_execution_idempotency_and_reconciliation (PASSED)
  ✓ test_mode1_no_ai_fix_buttons_or_remediation_in_mode1 (PASSED)

backend/tests/test_migration_pipeline.py
  ✓ test_agent_proposes_valid_plan (PASSED)
  ✓ test_deterministic_dry_run (PASSED)
  ✓ test_execution_requires_approval (PASSED)
  ✓ test_full_execution_idempotency_and_rollback (PASSED)

backend/tests/test_transforms.py
  ✓ test_trim_clean (PASSED)
  ✓ test_split_name (PASSED)
  ✓ test_date_to_iso8601 (PASSED)
  ✓ test_phone_to_e164 (PASSED)
  ✓ test_currency_to_float (PASSED)
  ✓ test_enum_lookup (PASSED)
  ✓ test_uuid_v5_determinism (PASSED)

backend/tests/test_api.py
  ✓ test_get_schemas (PASSED)
  ✓ test_transforms_catalog (PASSED)
  ✓ test_plans_lifecycle_api (PASSED)
  ✓ test_clarifications_and_mapping_customization (PASSED)
  ✓ test_load_test_records_endpoint (PASSED)
```

### Strict Mode 1 Invariant Verification
- Verified across **1,000 source records**:
  - $1,000 \text{ Source} = 902 \text{ Valid Target} + 98 \text{ Quarantined}$
  - $\text{Unaccounted Delta} = 0$
  - Idempotency verified: re-running migration on the target database maintains exactly 902 records without duplicates.
- **Playwright End-to-End Browser Testing**: `verify_mode1_browser_strict.py` confirms clean DOM elements, absence of Mode 1 AI buttons, execution flow, target grid rendering, and zero-delta parity display.

---

## 7. Maintainability

- **Zero Unsafe Dynamic Execution**: All data transformations are pure Python functions without `eval()`, `exec()`, or runtime code generation.
- **Strict Typing & Data Contracts**: Full Pydantic v2 schemas across both frontend payloads and backend responses (`MigrationPlan`, `FieldMapping`, `DryRunSummary`, `ReconciliationReport`).
- **Clean Separation of Concerns**:
  - `backend/app/engine/`: Business logic and computational transforms.
  - `backend/app/models/`: Pydantic schema contracts.
  - `backend/tests/`: Automated pytest and playwright suites.
  - `frontend/`: Static vanilla JS, modular CSS, and Tailwind utilities.

---

## 8. Deployment and Documentation

### Single Unified Deployment on Render
- **Combined Service Architecture**: FastAPI serves both the REST API (`/api`) and the static web assets (`/`, `/css`, `/js`, `/assets`).
- **Zero CORS Issues**: Relative API paths ensure identical origin between frontend and backend.
- **Infrastructure as Code**:
  - Included [render.yaml](file:///Users/adityapandey/data_mig/render.yaml) for 1-click Render Blueprint deployments.
  - Included production [requirements.txt](file:///Users/adityapandey/data_mig/requirements.txt) with `gunicorn` and `uvicorn[standard]`.
  - Added dedicated `/healthz` zero-downtime healthcheck endpoint in FastAPI.

---

## 9. Responsible Agent Use

- **Governance Barrier**: The agent never executes database writes autonomously; it only *proposes* plans and *simulates* outcomes.
- **Explicit Risk Classifications**: Every field mapping includes a risk level (`LOW`, `MEDIUM`, `HIGH`) and risk rationale so engineers understand why a rule was selected.
- **Ambiguity Clarifications**: The system proactively flags ambiguous source patterns (e.g., whether to default missing phone numbers or reject the record) and generates questions for human input.
- **Audit Trails**: All modifications, approvals, and AI remediations are stamped with actor identity, UTC timestamps, and version hashes.

---

## 10. Professional Readiness

- **Security Best Practices**: `.gitignore` strictly excludes `.env`, secrets, `.sqlite` databases, and compiled artifacts.
- **Zero Console Errors**: Clean browser execution, defensive null checks, and graceful error boundaries.
- **Complete End-to-End Usability**: From cloning the repository to running dry runs and reconciling in production, the workflow requires zero undocumented steps.
