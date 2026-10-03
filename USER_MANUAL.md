# Agentic Data Migration & Reconciliation Workbench — User Manual

An enterprise-grade platform for planning, validating, executing, and reconciling complex data migrations with **zero silent data loss** and autonomous AI assistance.

---

## 1. Executive Summary & Core Philosophy

Traditional data migrations rely on fragile custom scripts that fail silently, drop anomalous rows without warning, or corrupt target databases. 

The **Agentic Data Migration Workbench** solves this with a **mathematically verified, human-in-the-loop migration lifecycle**:

$$\text{Source Records Ingested} = \text{Target Database Records} + \text{Quarantined Records}$$

* **Zero Silent Drop:** Every single input record is either safely written to the target database or captured in the immutable Quarantine Ledger with the exact reason for failure.
* **Deterministic Verification:** Migrations cannot be executed without prior human approval and a sandboxed simulation (Dry-Run).
* **AI-Assisted Remediation:** Quarantined rows can be diagnosed, auto-corrected, and re-processed into the target store using autonomous AI forensics.

---

## 2. Operating Modes: Mode 1 vs. Mode 2

Switch between operating modes at any time using the toggle pill in the top navigation bar:

| Dimension | **Mode 1: Benchmark CRM** | **Mode 2: Universal Studio (V2)** |
| :--- | :--- | :--- |
| **Primary Goal** | Audited benchmark verification | Any custom dataset & arbitrary target schema |
| **Source Data** | 1,000 legacy CRM records with known anomalies | Any uploaded CSV, TSV, JSON, or NDJSON file |
| **Target Schema** | Modern Customer & Account Schema (11 fields) | Dynamic SQLite table compiled on the fly |
| **AI Planner** | Deterministic Rule-Engine Copilot | Ollama AI (with guaranteed offline fallback) |
| **Dry-Run** | Sandboxed simulation of 1,000 CRM rows | Sandboxed simulation of arbitrary schema rows |
| **Remediation** | Live AI forensic diagnosis & auto-fix | Live AI forensic diagnosis & auto-fix |
| **Rollback** | Idempotent ledger & database reset | Atomic snapshot-based dynamic table rollback |

---

## 3. Data Intake & Prefilled Datasets

The platform provides three ingestion options to start working immediately:

### Option A: Standard 1,000 Benchmark (`Load 1,000 Benchmark`)
* **What it is:** A pre-packaged, bounded dataset representing realistic legacy CRM customer accounts.
* **Record Count:** Exactly **1,000 records**.
* **Source Columns (9):** `legacy_id`, `full_name`, `email_address`, `phone_raw`, `signup_date`, `account_balance`, `plan_type`, `country_code`, `notes`.
* **Built-in Anomalies (~5.8%):**
  * Malformed/unparseable dates (e.g. `99/99/9999`, `invalid_date`).
  * Short or corrupted phone numbers (< 10 digits).
  * Out-of-bounds legacy plan categories (e.g. `legacy_v0`).
* **Purpose:** Demonstrates that the dry-run, quarantine ledger, and reconciliation engine catch anomalous records without crashing or silently dropping data.

### Option B: Quick Test Sample (`Load Test Sample (100 Recs)`)
* **What it is:** A 100-record subset designed for rapid verification, smoke testing, and live client demos.

### Option C: Upload Custom Dataset (`Upload Custom CSV / JSON`)
Upload your own data file from your local machine. Supported formats include:
* **CSV / TSV:** Auto-detects delimiters (`,`, `;`, `\t`, `|`) and handles UTF-8 BOM encoding.
* **Standard JSON Array:** `[{"id": 1, "name": "Acme"}, ...]`
* **Wrapped JSON Objects:** `{ "records": [...] }`, `{ "data": [...] }`, `{ "invoices": [...] }`, `{ "orders": [...] }`
* **NDJSON (JSON Lines):** Newline-delimited JSON objects.

---

## 4. End-to-End Migration Walkthrough

The platform guides users through 5 structured stages:

```
[1. Schemas & Profiler] ➔ [2. AI Mapping Studio] ➔ [3. Deterministic Dry-Run] ➔ [4. Target Store Write] ➔ [5. Reconciliation & Audit]
```

### Stage 1: Schemas & Profiler
1. View structural contracts for both the **Source Schema** and **Target Schema**.
2. Inspect profile metrics: total records, column null rates, unique value distributions, and detected anomaly rates.
3. Review target invariants (e.g., `NOT NULL` constraints, allowed enum values, strict ISO-8601 date requirements).

### Stage 2: AI Mapping Studio
1. The AI engine inspects source columns and generates a field-by-field migration plan.
2. **Standard Transformation Rules Catalog:**
   * `TRIM_CLEAN`: Strips extraneous whitespace, normalizes text case.
   * `SPLIT_NAME`: Splits raw names into `first_name` and `last_name`.
   * `DATE_TO_ISO8601`: Standardizes messy date strings into `YYYY-MM-DD` or `YYYY-MM-DDTHH:MM:SSZ`.
   * `PHONE_E164`: Cleans and formats phone numbers to international `+E.164` standards.
   * `CURRENCY_TO_FLOAT`: Parses currency symbols (`$`, `€`, commas) into clean numeric floats.
   * `ENUM_LOOKUP`: Maps legacy strings to valid target enum choices with safe fallback defaults.
   * `UUID_V5_FROM_KEY`: Deterministically generates modern UUIDs from legacy primary keys.
3. Review risk assessments (Low, Medium, High) for each mapped field.
4. **Approval Step:** Click **Approve Plan** in the top navigation header. Writes to the target database remain locked until a human approves the plan.

### Stage 3: Deterministic Dry-Run
1. Click **Run Dry-Run** in the top navigation header.
2. The simulation runs against 100% of source records in an isolated sandbox.
3. Review the breakdown:
   * **Accepted Records:** Rows that strictly satisfy all target constraints and types.
   * **Rejected Records:** Rows flagged for quarantine, showing the exact failing field and validation error.

### Stage 4: Mock Target Store & Quarantine Ledger
1. Click **Execute Target Write** (unlocked after approval).
2. The engine writes accepted records to the SQLite database (using WAL mode for high concurrency).
3. **Quarantine Ledger:** Every rejected row is recorded with its original source payload, rejection timestamp, and error category.
4. **Live AI Remediation:**
   * Click **Ask AI** next to any quarantined row to view an autonomous forensic diagnosis.
   * Click **Apply Fix & Re-process** to correct the record and write it to the target database.
   * Click **Auto-Remediate All with AI** to batch-remediate quarantined rows.
5. **Rollback (Mode 2):** Click the **Rollback** button ($\circlearrowleft$) at any time to instantly revert the database to the prior snapshot.

### Stage 5: Reconciliation & Audit
1. View the verified **Parity Equation**:
   $$\text{Source Records } (1,000) = \text{Target DB } (942) + \text{Quarantined } (58)$$
2. Confirm that **Net Variance is 0** (Zero Silent Drop).
3. Inspect the cryptographic **SHA-256 Audit Run Hash**, providing an immutable signature for enterprise compliance and data governance.

---

## 5. Mode 2: Universal Studio Industry Presets

Mode 2 allows you to ingest any dataset and map it into any custom target table. Pre-configured industry templates include:

1. **E-Commerce Orders:** Schema for order tracking (`order_id`, `customer_email`, `order_total`, `status`, `ordered_at`).
2. **SaaS Invoices:** Financial contract schema (`invoice_num`, `client_name`, `amount_due`, `due_date`, `is_paid`).
3. **Healthcare Encounters:** Clinical data schema (`encounter_id`, `patient_mrn`, `department`, `admitted_at`).
4. **Warehouse Inventory:** Supply chain schema (`sku_code`, `product_title`, `quantity_on_hand`, `unit_cost`).
5. **Custom Schema Editor:** Edit JSON directly in the live browser editor. The engine automatically compiles SQLite DDL and provisions tables dynamically.

---

## 6. Real-Time Server Diagnostics (`/logs`)

A standalone server diagnostics and AI telemetry stream is available directly in your browser:
* **URL:** `https://<your-host>/logs` (or `http://localhost:8000/logs`)
* **Features:**
  * 2-second live streaming log updates with Pause/Resume toggle.
  * Level filtering (`ALL`, `ERROR`, `WARNING`, `INFO`).
  * Live keyword search filter (e.g. search `upload`, `502`, `ollama`, `rollback`).
  * One-click `.log` export and memory buffer clear.

---

## 7. Frequently Asked Questions (FAQ)

**Q: Can a migration run without approving the plan?**  
*No.* The platform strictly blocks execution until a user clicks **Approve Plan**.

**Q: What happens if an external AI model is offline or slow?**  
The platform features an autonomous fallback engine. If an external LLM is unreachable or times out, it automatically falls back to the deterministic semantic rule engine. The upload and planning pipeline never crashes or returns 502.

**Q: Where are target records stored?**  
Target records are persisted in a production-configured SQLite database running in WAL mode with snapshot versioning.

**Q: Is the interface mobile-friendly?**  
*Yes.* The entire web interface is fully responsive, featuring touch-optimized tab swiping, horizontally scrollable data tables, collapsible grids, and mobile-adapted modals.
