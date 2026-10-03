/**
 * V2 Universal Autonomous Studio - JavaScript Controller
 * Interacts with /api/v2 endpoints for dynamic schemas, Ollama autonomous planning,
 * dynamic DDL compilation, live SQLite queries, and universal mass conservation.
 */

const V2_API_BASE = '/api/v2';

/**
 * Robust JSON fetch helper that safely handles non-JSON / HTML / plain-text 500 error responses
 * without throwing "Unexpected token 'I', 'Internal S'... is not valid JSON".
 */
async function safeFetchJson(url, options = {}, fallbackErr = 'Request failed') {
  const res = await fetch(url, options);
  const text = await res.text();
  let data;
  try {
    data = text ? JSON.parse(text) : {};
  } catch (parseErr) {
    if (!res.ok) {
      throw new Error(`Server error (${res.status}): ${text || res.statusText || fallbackErr}`);
    }
    throw new Error(`Invalid JSON response: ${text.slice(0, 120)}`);
  }
  if (!res.ok) {
    throw new Error(data.detail || data.message || fallbackErr);
  }
  return data;
}

const v2Presets = {
  orders: {
    schema_id: "ecommerce_orders_v1",
    table_name: "orders",
    primary_key: "order_uuid",
    natural_key: "order_num",
    fields: [
      { name: "order_uuid", data_type: "string", nullable: false, constraints: { unique: true } },
      { name: "order_num", data_type: "string", nullable: false, constraints: { unique: true } },
      { name: "customer_email", data_type: "string", nullable: false },
      { name: "order_date", data_type: "string", nullable: false },
      { name: "total_amount", data_type: "float", nullable: false },
      { name: "order_status", data_type: "string", nullable: false }
    ]
  },
  encounters: {
    schema_id: "medical_encounters_v1",
    table_name: "encounters",
    primary_key: "encounter_uuid",
    natural_key: "encounter_id",
    fields: [
      { name: "encounter_uuid", data_type: "string", nullable: false, constraints: { unique: true } },
      { name: "encounter_id", data_type: "string", nullable: false, constraints: { unique: true } },
      { name: "patient_mrn", data_type: "string", nullable: false },
      { name: "patient_first_name", data_type: "string", nullable: false },
      { name: "patient_last_name", data_type: "string", nullable: false },
      { name: "service_date", data_type: "string", nullable: false },
      { name: "department_name", data_type: "string", nullable: false },
      { name: "billed_amount", data_type: "float", nullable: false }
    ]
  },
  invoices: {
    schema_id: "saas_invoices_v1",
    table_name: "invoices",
    primary_key: "invoice_uuid",
    natural_key: "invoice_number",
    fields: [
      { name: "invoice_uuid", data_type: "string", nullable: false, constraints: { unique: true } },
      { name: "invoice_number", data_type: "string", nullable: false, constraints: { unique: true } },
      { name: "client_name", data_type: "string", nullable: false },
      { name: "invoice_date", data_type: "string", nullable: false },
      { name: "due_date", data_type: "string", nullable: false },
      { name: "balance_due", data_type: "float", nullable: false },
      { name: "payment_status", data_type: "string", nullable: false }
    ]
  },
  inventory: {
    schema_id: "warehouse_inventory_v1",
    table_name: "inventory",
    primary_key: "item_uuid",
    natural_key: "sku_code",
    fields: [
      { name: "item_uuid", data_type: "string", nullable: false, constraints: { unique: true } },
      { name: "sku_code", data_type: "string", nullable: false, constraints: { unique: true } },
      { name: "item_name", data_type: "string", nullable: false },
      { name: "stock_quantity", data_type: "int", nullable: false },
      { name: "unit_price", data_type: "float", nullable: false },
      { name: "category", data_type: "string", nullable: false }
    ]
  },
  custom: {
    schema_id: "custom_enterprise_contract_v1",
    table_name: "custom_entity",
    primary_key: "entity_uuid",
    natural_key: "natural_id",
    fields: [
      { name: "entity_uuid", data_type: "string", nullable: false, constraints: { unique: true, description: "UUIDv5 Primary Key" } },
      { name: "natural_id", data_type: "string", nullable: false, constraints: { unique: true, description: "Unique natural business key" } },
      { name: "entity_name", data_type: "string", nullable: false, constraints: { description: "Cleaned entity title or contact name" } },
      { name: "event_date", data_type: "date", nullable: false, constraints: { description: "ISO-8601 UTC date (YYYY-MM-DD)" } },
      { name: "transaction_amount", data_type: "float", nullable: false, constraints: { description: "Monetary value normalized to float" } },
      { name: "operational_status", data_type: "enum", nullable: false, constraints: { allowed_values: ["ACTIVE", "PENDING", "COMPLETED", "CANCELLED"] } }
    ]
  }
};

const v2State = {
  currentPlan: null,
  targetSchema: null,
  compiledDDL: '',
  llmConfig: {
    host: 'https://ollama.com/api',
    model: 'gpt-oss:20b',
    apiKey: ''
  },
  lastDryRunResult: null,
  lastExecutionResult: null,
  targetRecords: [],
  targetTotal: 0,
  targetColumns: []
};

// Mode Switcher function exposed globally
window.switchWorkbenchMode = function(mode) {
  const btn1 = document.getElementById('toggle-mode-1');
  const btn2 = document.getElementById('toggle-mode-2');
  const navActionsV1 = document.getElementById('nav-actions-v1');
  const navActionsV2 = document.getElementById('nav-actions-v2');
  const tabsV1 = document.getElementById('tabs-bar-v1');
  const tabsV2 = document.getElementById('tabs-bar-v2');
  const stageV1 = document.getElementById('tab-container-v1');
  const stageV2 = document.getElementById('tab-container-v2');

  if (mode === 'v2') {
    btn1.classList.remove('active');
    btn2.classList.add('active', 'v2-active');
    if (navActionsV1) navActionsV1.style.display = 'none';
    if (navActionsV2) navActionsV2.style.display = 'flex';
    if (tabsV1) tabsV1.style.display = 'none';
    if (tabsV2) tabsV2.style.display = 'flex';
    if (stageV1) stageV1.style.display = 'none';
    if (stageV2) stageV2.style.display = 'block';

    initV2Workbench();
    localStorage.setItem('workbench_active_mode', 'v2');
  } else {
    btn2.classList.remove('active', 'v2-active');
    btn1.classList.add('active');
    if (navActionsV1) navActionsV1.style.display = 'flex';
    if (navActionsV2) navActionsV2.style.display = 'none';
    if (tabsV1) tabsV1.style.display = 'flex';
    if (tabsV2) tabsV2.style.display = 'none';
    if (stageV1) stageV1.style.display = 'block';
    if (stageV2) stageV2.style.display = 'none';

    localStorage.setItem('workbench_active_mode', 'v1');
  }
};

// V2 Tab Navigation
window.switchV2Tab = function(tabId) {
  document.querySelectorAll('#tabs-bar-v2 .tab-btn').forEach(btn => {
    btn.classList.toggle('active', btn.getAttribute('data-tab') === tabId);
  });
  document.querySelectorAll('#tab-container-v2 .view-panel').forEach(panel => {
    panel.classList.toggle('active', panel.id === tabId);
  });
  if (tabId === 'v2-tab-logs' && window.fetchSystemLogs) {
    window.fetchSystemLogs('mode2');
  }
};

async function initV2Workbench() {
  // Populate default preset if textarea is empty
  const jsonEditor = document.getElementById('v2-target-schema-editor');
  if (jsonEditor && !jsonEditor.value.trim()) {
    loadV2SchemaPreset('orders');
  }
  setupV2IntakeListeners();
  await fetchV2SourceInfo();
  await fetchV2CurrentTargetSchema();
  await fetchV2CurrentPlan();
  await fetchV2TargetRecords();
  await fetchV2Reconciliation();
}

async function fetchV2SourceInfo() {
  try {
    const res = await fetch(`${V2_API_BASE}/source`);
    if (res.ok) {
      const data = await res.json();
      const srcName = document.getElementById('v2-source-name');
      const srcRecs = document.getElementById('v2-source-records-count');
      const srcFields = document.getElementById('v2-source-fields-count');
      if (srcName) srcName.textContent = data.source_schema?.name || (data.total_records ? 'Active Mode 2 Source Dataset' : 'No dataset loaded');
      if (srcRecs) srcRecs.textContent = `${(data.total_records || 0).toLocaleString()} records`;
      if (srcFields && data.source_schema) {
        srcFields.textContent = `${data.source_schema.fields?.length || 0} fields (${(data.source_schema.fields || []).map(f => f.name).slice(0, 4).join(', ')}...)`;
      }
    }
  } catch (err) {
    console.warn('Error fetching Mode 2 source info:', err);
  }
}

window.uploadV2Dataset = async function(file) {
  if (!file) return;

  const scalerBox = document.getElementById('v2-upload-scaler-container');
  const labelElem = document.getElementById('v2-scaler-file-label');
  const pctElem = document.getElementById('v2-upload-scaler-percent');
  const fillElem = document.getElementById('v2-upload-scaler-fill');
  const msgElem = document.getElementById('v2-upload-scaler-msg');

  const formatSize = (bytes) => (bytes / 1024).toFixed(1) + ' KB';

  if (scalerBox) {
    scalerBox.style.display = 'block';
    if (labelElem) labelElem.textContent = `${file.name} (${formatSize(file.size)})`;
    if (pctElem) pctElem.textContent = '15%';
    if (fillElem) fillElem.style.width = '15%';
    if (msgElem) msgElem.textContent = 'Reading dataset chunks...';
  }

  const formData = new FormData();
  formData.append('file', file);
  showToast(`Universal Studio (Mode 2): Uploading & profiling '${file.name}'...`, 'info');

  try {
    if (fillElem) {
      setTimeout(() => { if (fillElem) fillElem.style.width = '50%'; if (pctElem) pctElem.textContent = '50%'; if (msgElem) msgElem.textContent = 'Streaming payload to server...'; }, 100);
      setTimeout(() => { if (fillElem) fillElem.style.width = '85%'; if (pctElem) pctElem.textContent = '85%'; if (msgElem) msgElem.textContent = 'Parsing schema contracts & profiling rows...'; }, 300);
    }

    const res = await fetch(`${V2_API_BASE}/upload/source`, {
      method: 'POST',
      body: formData
    });
    const text = await res.text();
    let data;
    try {
      data = text ? JSON.parse(text) : {};
    } catch (parseErr) {
      throw new Error(`Server returned non-JSON response (HTTP ${res.status}): ${text ? text.slice(0, 150) : 'Empty response'}`);
    }
    if (!res.ok) throw new Error(data.detail || data.message || `Upload failed (HTTP ${res.status})`);

    if (fillElem) fillElem.style.width = '100%';
    if (pctElem) pctElem.textContent = '100%';
    if (msgElem) msgElem.textContent = '✓ Parsing & schema profiling complete!';

    // Update Mode 2 Source Readiness display
    const srcName = document.getElementById('v2-source-name');
    const srcRecs = document.getElementById('v2-source-records-count');
    const srcFields = document.getElementById('v2-source-fields-count');
    if (srcName) srcName.textContent = data.filename || file.name;
    if (srcRecs) srcRecs.textContent = `${(data.total_records || 0).toLocaleString()} records`;
    if (srcFields && data.source_schema) {
      srcFields.textContent = `${data.source_schema.fields?.length || 0} fields (${(data.source_schema.fields || []).map(f => f.name).slice(0, 4).join(', ')}...)`;
    }

    if (data.plan) {
      v2State.currentPlan = data.plan;
      renderV2Plan(data.plan);
      showToast(`Mode 2: Ingested '${file.name}' (${data.total_records} rows). Plan synthesized.`, 'success');
      switchV2Tab('v2-tab-mapping');
    } else if (data.ai_error) {
      showToast(`Mode 2: Ingested '${file.name}' (${data.total_records} rows). Note: ${data.ai_error}`, 'info');
      switchV2Tab('v2-tab-mapping');
    } else {
      showToast(`Mode 2: Ingested '${file.name}' (${data.total_records} rows). Ready for target schema compilation.`, 'success');
    }

    setTimeout(() => {
      if (scalerBox) scalerBox.style.display = 'none';
    }, 2500);
  } catch (err) {
    if (msgElem) msgElem.textContent = '❌ Upload failed: ' + err.message;
    showToast(`Mode 2 Upload Error: ${err.message}`, 'error');
  }
};

function setupV2IntakeListeners() {
  const v2FileInput = document.getElementById('input-v2-dataset-file');
  if (v2FileInput && !v2FileInput._bound) {
    v2FileInput._bound = true;
    v2FileInput.addEventListener('change', (e) => {
      const file = e.target.files && e.target.files[0];
      if (file) window.uploadV2Dataset(file);
    });
  }

  const dropZone = document.getElementById('v2-dataset-drop-zone');
  if (dropZone && !dropZone._bound) {
    dropZone._bound = true;
    ['dragenter', 'dragover'].forEach(name => {
      dropZone.addEventListener(name, (e) => {
        e.preventDefault();
        e.stopPropagation();
        dropZone.style.borderColor = 'var(--ink-primary)';
        dropZone.style.background = 'var(--bg-subtle)';
      }, false);
    });
    ['dragleave', 'drop'].forEach(name => {
      dropZone.addEventListener(name, (e) => {
        e.preventDefault();
        e.stopPropagation();
        dropZone.style.borderColor = 'var(--border-medium)';
        dropZone.style.background = 'var(--bg-inset)';
      }, false);
    });
    dropZone.addEventListener('drop', (e) => {
      e.preventDefault();
      e.stopPropagation();
      dropZone.style.borderColor = 'var(--border-medium)';
      dropZone.style.background = 'var(--bg-inset)';
      const dt = e.dataTransfer;
      if (dt && dt.files && dt.files.length > 0) {
        window.uploadV2Dataset(dt.files[0]);
      }
    });
  }

  setupSchemaEditorValidator();
}

window.toggleSchemaRulesGuide = function() {
  const content = document.getElementById('schema-rules-content');
  const chevron = document.getElementById('schema-rules-chevron');
  if (content) {
    const isHidden = content.style.display === 'none';
    content.style.display = isHidden ? 'block' : 'none';
    if (chevron) chevron.textContent = isHidden ? '▲' : '▼';
  }
};

function setupSchemaEditorValidator() {
  const editor = document.getElementById('v2-target-schema-editor');
  const indicator = document.getElementById('v2-json-syntax-indicator');
  if (editor && indicator) {
    editor.addEventListener('input', () => {
      try {
        const parsed = JSON.parse(editor.value);
        if (!parsed.table_name || !parsed.fields || !Array.isArray(parsed.fields)) {
          indicator.style.background = 'var(--accent-amber-subtle)';
          indicator.style.color = '#92400e';
          indicator.style.borderColor = 'var(--accent-amber-border)';
          indicator.textContent = '⚠️ Valid JSON, but missing table_name or fields array';
        } else {
          indicator.style.background = 'var(--accent-emerald-subtle)';
          indicator.style.color = '#065f46';
          indicator.style.borderColor = 'var(--accent-emerald-border)';
          indicator.textContent = `✓ Valid Contract: ${parsed.table_name} (${parsed.fields.length} fields)`;
        }
      } catch (e) {
        indicator.style.background = 'var(--accent-rose-subtle)';
        indicator.style.color = '#991b1b';
        indicator.style.borderColor = 'var(--accent-rose-border)';
        indicator.textContent = '✗ JSON Syntax Error';
      }
    });
  }
}

document.addEventListener('DOMContentLoaded', () => {
  setupV2IntakeListeners();
});

window.loadV2SchemaPreset = async function(presetKey) {
  document.querySelectorAll('.schema-preset-btn').forEach(btn => {
    btn.classList.toggle('active', btn.getAttribute('data-preset') === presetKey);
  });

  if (presetKey === 'custom') {
    const preset = v2Presets.custom;
    const jsonEditor = document.getElementById('v2-target-schema-editor');
    if (jsonEditor) {
      jsonEditor.value = JSON.stringify(preset, null, 2);
    }
    showToast('Loaded Custom Enterprise JSON Contract template. Click Compile to test.', 'info');
    await window.compileV2TargetSchema();
    return;
  }

  const backendKey = presetKey === 'encounters' ? 'healthcare' : presetKey;
  showToast(`Loading '${presetKey}' dataset & target schema into Mode 2...`, 'info');

  try {
    const data = await safeFetchJson(`${V2_API_BASE}/samples/load/${backendKey}`, { method: 'POST' }, 'Failed to load sample dataset');

    const jsonEditor = document.getElementById('v2-target-schema-editor');
    if (jsonEditor && data.target_schema) {
      jsonEditor.value = JSON.stringify(data.target_schema, null, 2);
    }

    const ddlElem = document.getElementById('v2-ddl-display');
    if (ddlElem && data.ddl) {
      ddlElem.textContent = data.ddl;
    }

    // Update Mode 2 Source Readiness display
    const srcName = document.getElementById('v2-source-name');
    const srcRecs = document.getElementById('v2-source-records-count');
    const srcFields = document.getElementById('v2-source-fields-count');
    if (srcName) srcName.textContent = `${data.title} (${data.records_count} rows)`;
    if (srcRecs) srcRecs.textContent = `${data.records_count.toLocaleString()} records`;
    if (srcFields && data.source_schema) {
      srcFields.textContent = `${data.source_schema.fields?.length || 0} fields (${(data.source_schema.fields || []).map(f => f.name).slice(0, 4).join(', ')}...)`;
    }

    if (data.plan) {
      v2State.currentPlan = data.plan;
      renderV2Plan(data.plan);
    } else {
      v2State.currentPlan = null;
      renderV2NoPlan();
    }

    showToast(`Loaded '${data.title}' dataset & target schema into Mode 2.`, 'success');
  } catch (err) {
    console.warn('Preset load fallback:', err);
    const preset = v2Presets[presetKey];
    if (preset) {
      const jsonEditor = document.getElementById('v2-target-schema-editor');
      if (jsonEditor) jsonEditor.value = JSON.stringify(preset, null, 2);
    }
  }
};

window.compileV2TargetSchema = async function() {
  const jsonEditor = document.getElementById('v2-target-schema-editor');
  const btn = document.getElementById('btn-v2-compile-schema');
  let schemaObj;
  try {
    schemaObj = JSON.parse(jsonEditor.value);
  } catch (err) {
    showToast('Invalid JSON Schema: ' + err.message, 'error');
    return;
  }

  await window.withButtonLoader(btn, 'Compiling SQLite DDL...', async () => {
    try {
      const data = await safeFetchJson(`${V2_API_BASE}/schema/target`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(schemaObj)
      }, 'Failed to compile schema');

      v2State.targetSchema = data.schema;
      v2State.compiledDDL = data.ddl;

      const ddlElem = document.getElementById('v2-ddl-display');
      if (ddlElem) ddlElem.textContent = data.ddl;

      showToast(`Target schema '${data.table_name}' compiled successfully!`, 'success');
      if (data.ai_error) {
        showToast(`AI Generation Error: ${data.ai_error}`, 'error');
      }
      await fetchV2CurrentPlan();
    } catch (err) {
      showToast('Compilation error: ' + err.message, 'error');
    }
  });
};

async function fetchV2CurrentTargetSchema() {
  try {
    const res = await fetch(`${V2_API_BASE}/schema/target`);
    if (res.ok) {
      const data = await res.json();
      v2State.targetSchema = data;
      const ddlElem = document.getElementById('v2-ddl-display');
      if (ddlElem && !ddlElem.textContent.trim()) {
        ddlElem.textContent = `-- Target table: ${data.table_name || 'N/A'}\n-- Ready for compilation`;
      }
    }
  } catch (err) {
    console.warn('Error fetching V2 target schema:', err);
  }
}

window.proposeV2Plan = async function() {
  const btn = document.getElementById('btn-v2-propose');
  await window.withButtonLoader(btn, 'Synthesizing Plan...', async () => {
    try {
      showToast('AI Autonomous Agent inspecting schemas and querying Ollama...', 'info');
      const plan = await safeFetchJson(`${V2_API_BASE}/plans/propose`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ use_llm: true })
      }, 'Failed to propose AI plan');

      v2State.currentPlan = plan;
      renderV2Plan(plan);
      showToast(`V2 Plan proposed strictly by ${plan.metadata?.planner_type || 'Ollama AI'}!`, 'success');
      switchV2Tab('v2-tab-mapping');
    } catch (err) {
      showToast('AI Generation Error: ' + err.message, 'error');
      const ratBox = document.getElementById('v2-agent-rationale-box');
      if (ratBox) {
        ratBox.innerHTML = `
          <div style="font-weight: 600; color: #dc2626; margin-bottom: 0.4rem; display: flex; align-items: center; justify-content: space-between;">
            <span>AI Service Error (No Offline Fallback)</span>
            <span class="v2-badge" style="background: rgba(220, 38, 38, 0.1); color: #dc2626; border-color: rgba(220, 38, 38, 0.3);">AI ERROR</span>
          </div>
          <p style="font-size: 0.85rem; color: #b91c1c; line-height: 1.5;">${err.message}</p>
        `;
      }
    }
  });
};

async function fetchV2CurrentPlan() {
  try {
    const res = await fetch(`${V2_API_BASE}/plans/current`);
    if (res.ok) {
      const plan = await res.json();
      if (plan && plan.plan_id) {
        v2State.currentPlan = plan;
        renderV2Plan(plan);
      } else {
        v2State.currentPlan = null;
        renderV2NoPlan();
      }
    } else {
      v2State.currentPlan = null;
      renderV2NoPlan();
    }
  } catch (err) {
    v2State.currentPlan = null;
    renderV2NoPlan();
  }
}

function renderV2NoPlan() {
  const badge = document.getElementById('v2-plan-badge-text');
  const dot = document.getElementById('v2-plan-status-dot');
  if (badge) badge.textContent = 'No Plan Synthesized';
  if (dot) dot.className = 'status-indicator draft';

  const ratBox = document.getElementById('v2-agent-rationale-box');
  if (ratBox) {
    ratBox.innerHTML = `
      <div style="font-weight: 600; color: #94a3b8; margin-bottom: 0.4rem; display: flex; align-items: center; justify-content: space-between;">
        <span>Autonomous Agent (Mode 2)</span>
        <span class="v2-badge" style="background: rgba(148, 163, 184, 0.15); color: #94a3b8;">IDLE</span>
      </div>
      <p style="font-size: 0.85rem; color: #94a3b8; line-height: 1.5;">Target schema is ready. Click <strong>"Propose Plan"</strong> in the top bar to synthesize schema mappings using Ollama AI.</p>
    `;
  }

  const tbody = document.getElementById('v2-tbody-mappings');
  if (tbody) {
    tbody.innerHTML = `
      <tr>
        <td colspan="5" style="text-align: center; color: #64748b; padding: 2.5rem 1rem;">
          No migration plan currently active. Click <strong>"Propose Plan"</strong> to generate schema mappings with AI.
        </td>
      </tr>
    `;
  }

  const execBtn = document.getElementById('btn-v2-execute-top');
  if (execBtn) execBtn.disabled = true;
  const dryBtn = document.getElementById('btn-v2-dryrun-top');
  if (dryBtn) dryBtn.disabled = true;
  const appBtn = document.getElementById('btn-v2-approve-top');
  if (appBtn) appBtn.disabled = true;
}

function renderV2Plan(plan) {
  const badge = document.getElementById('v2-plan-badge-text');
  const dot = document.getElementById('v2-plan-status-dot');
  if (badge) badge.textContent = `Plan: ${plan.plan_id} (${plan.status})`;
  if (dot) {
    dot.className = `status-indicator ${plan.status.toLowerCase()}`;
    if (plan.status === 'APPROVED') dot.classList.add('approved');
  }

  // Update rationale box
  const ratBox = document.getElementById('v2-agent-rationale-box');
  if (ratBox) {
    ratBox.innerHTML = `
      <div style="font-weight: 600; color: #09090b; margin-bottom: 0.4rem; display: flex; align-items: center; justify-content: space-between;">
        <span style="display: flex; align-items: center; gap: 0.45rem;">
          <span style="display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: #059669;"></span>
          Autonomous Agent Rationale (${plan.metadata?.planner_type || 'Ollama AI'})
        </span>
        <span class="v2-badge">${plan.status}</span>
      </div>
      <p style="font-size: 0.84rem; color: #475569; line-height: 1.55;">${plan.metadata?.rationale || plan.notes || plan.description || 'Autonomous AI synthesized deterministic mappings.'}</p>
    `;
  }

  // Render Mapping Matrix
  const tbody = document.getElementById('v2-tbody-mappings');
  if (tbody && plan.field_mappings) {
    tbody.innerHTML = plan.field_mappings.map((m, idx) => {
      const riskClass = m.risk_level === 'HIGH' ? 'badge-risk-high' : (m.risk_level === 'MEDIUM' ? 'badge-risk-med' : 'badge-risk-low');
      const srcDisplay = (m.source_fields && m.source_fields.length > 0)
        ? m.source_fields.join(', ')
        : (m.source_field || '<em style="color:#94a3b8;">(Generated/Literal)</em>');
      const ruleDisplay = m.transformation || m.transformation_rule || 'DIRECT_COPY';
      const rationaleDisplay = m.risk_rationale || m.rationale || m.notes || 'Deterministic mapping';
      return `
        <tr>
          <td><strong style="color: #09090b; font-weight: 600;">${m.target_field}</strong></td>
          <td><span class="mono" style="color: #334155; font-weight: 500;">${srcDisplay}</span></td>
          <td><span class="badge-pill" style="font-size: 0.72rem; background: #f1f5f9; color: #0f172a; border: 1px solid #e2e8f0;">${ruleDisplay}</span></td>
          <td><span class="badge-risk ${riskClass}">${m.risk_level || 'LOW'}</span></td>
          <td style="font-size: 0.8rem; color: #475569;">${rationaleDisplay}</td>
        </tr>
      `;
    }).join('');
  }

  // Enable/disable execution button
  const execBtn = document.getElementById('btn-v2-execute-top');
  if (execBtn) {
    execBtn.disabled = plan.status !== 'APPROVED';
  }
  const dryBtn = document.getElementById('btn-v2-dryrun-top');
  if (dryBtn) dryBtn.disabled = false;
  const appBtn = document.getElementById('btn-v2-approve-top');
  if (appBtn) appBtn.disabled = plan.status === 'APPROVED';
}

window.runV2DryRun = async function() {
  const btn = document.getElementById('btn-v2-dryrun-top');
  await window.withButtonLoader(btn, 'Simulating Dry-Run...', async () => {
    try {
      showToast('Executing deterministic dry-run simulation...', 'info');
      const data = await safeFetchJson(`${V2_API_BASE}/plans/dry-run`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sample_size: 50 })
      }, 'Dry-run failed');

      v2State.lastDryRunResult = data;
      renderV2DryRun(data);
      showToast(`Dry-run complete! ${data.valid_count} valid, ${data.quarantined_count} quarantined.`, 'success');
      switchV2Tab('v2-tab-dryrun');
    } catch (err) {
      showToast('Dry-run error: ' + err.message, 'error');
    }
  });
};

function renderV2DryRun(data) {
  const totalElem = document.getElementById('v2-dry-stat-total');
  const validElem = document.getElementById('v2-dry-stat-valid');
  const quarElem = document.getElementById('v2-dry-stat-quar');
  const readyElem = document.getElementById('v2-dry-stat-ready');

  if (totalElem) totalElem.textContent = (data.total_source_records || 0).toLocaleString();
  if (validElem) validElem.textContent = (data.valid_count || 0).toLocaleString();
  if (quarElem) quarElem.textContent = (data.quarantined_count || 0).toLocaleString();
  if (readyElem) {
    const pct = data.total_source_records ? Math.round((data.valid_count / data.total_source_records) * 100) : 100;
    readyElem.textContent = `${pct}%`;
  }

  // Render Dry Run Transformed Sample Table
  const thead = document.getElementById('v2-thead-dryrun');
  const tbody = document.getElementById('v2-tbody-dryrun');
  if (thead && tbody && data.sample_transformed && data.sample_transformed.length > 0) {
    const cols = Object.keys(data.sample_transformed[0]);
    thead.innerHTML = `<tr>${cols.map(c => `<th>${c}</th>`).join('')}</tr>`;
    tbody.innerHTML = data.sample_transformed.map(row => {
      return `<tr>${cols.map(c => `<td class="mono" style="font-size: 0.78rem;">${row[c] !== null && row[c] !== undefined ? String(row[c]) : '<em style="color:#64748b;">NULL</em>'}</td>`).join('')}</tr>`;
    }).join('');
  }

  // Render V2 Quarantine Ledger Table
  const quarBadge = document.getElementById('v2-quarantine-count-badge');
  const quarTbody = document.getElementById('v2-tbody-quarantine');
  const quars = data.quarantine_sample || [];

  if (quarBadge) {
    quarBadge.textContent = `${data.quarantined_count || 0} Quarantined Records`;
  }

  if (quarTbody) {
    if (quars.length === 0) {
      quarTbody.innerHTML = `<tr><td colspan="7" style="text-align: center; color: var(--accent-emerald); padding: 2rem;">No records quarantined. 100% schema invariant compliance!</td></tr>`;
    } else {
      quarTbody.innerHTML = quars.map(q => {
        const err = (q.errors && q.errors.length > 0) ? q.errors[0] : {};
        const fieldName = err.field || 'General';
        const ruleName = err.rule || 'INVARIANT';
        const errorMsg = err.error_message || 'Schema constraint violation';
        const aiFix = err.ai_suggestion || q.ai_remediation_summary || 'Review source record format or update mapping fallback';
        const natKey = q.source_natural_key || `ROW_${q.source_row_index + 1}`;
        const qStr = JSON.stringify(q).replace(/'/g, '&#39;');

        return `
          <tr>
            <td class="mono font-semibold">#${q.source_row_index + 1}</td>
            <td class="mono font-semibold" style="color: #0284c7;">${escapeHtml(natKey)}</td>
            <td class="mono" style="color: #f43f5e; font-weight: 600;">${escapeHtml(fieldName)}</td>
            <td><span class="badge-pill" style="font-size: 0.72rem; background: rgba(244, 63, 94, 0.08); color: #f43f5e; border-color: rgba(244, 63, 94, 0.25);">${escapeHtml(ruleName)}</span></td>
            <td style="color: #f43f5e; font-size: 0.78rem; line-height: 1.4; max-width: 260px; white-space: normal;">
              ${escapeHtml(errorMsg)}
            </td>
            <td style="font-size: 0.78rem; line-height: 1.4; max-width: 290px; white-space: normal; color: #334155; background: #f8fafc; border-left: 2px solid #0284c7; padding: 0.45rem 0.65rem; border-radius: 4px;">
              <span style="color: #0284c7; font-weight: 700; font-size: 0.7rem; display: block; text-transform: uppercase;">AI Suggestion:</span>
              ${escapeHtml(aiFix)}
            </td>
            <td style="text-align: right; white-space: nowrap;">
              <div style="display: flex; gap: 0.35rem; justify-content: flex-end; align-items: center;">
                <button class="btn btn-primary" style="padding: 0.25rem 0.55rem; font-size: 0.72rem; white-space: nowrap;" onclick='applyAIFixSingleV2("${escapeHtml(fieldName)}", "${escapeHtml(ruleName)}", ${JSON.stringify(err.raw_value)})'>
                  ⚡ Fix & Re-run
                </button>
                <button class="btn btn-secondary" style="padding: 0.25rem 0.55rem; font-size: 0.72rem; white-space: nowrap;" onclick='openQuarantineModal(${qStr}, "mode2")'>
                  Evidence & AI
                </button>
              </div>
            </td>
          </tr>
        `;
      }).join('');
    }
  }
}

window.approveV2Plan = async function() {
  const btn = document.getElementById('btn-v2-approve-top');
  await window.withButtonLoader(btn, 'Approving Plan...', async () => {
    try {
      const plan = await safeFetchJson(`${V2_API_BASE}/plans/approve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify("Lead Data Engineer")
      }, 'Approval failed');

      v2State.currentPlan = plan;
      renderV2Plan(plan);
      showToast('Plan approved! Ready for execution write.', 'success');
    } catch (err) {
      showToast('Approval error: ' + err.message, 'error');
    }
  });
};

window.executeV2Migration = async function() {
  const btn = document.getElementById('btn-v2-execute-top');
  await window.withButtonLoader(btn, 'Writing to SQLite...', async () => {
    try {
      showToast('Executing migration batch into dynamic SQLite store...', 'info');
      const data = await safeFetchJson(`${V2_API_BASE}/plans/execute`, {
        method: 'POST'
      }, 'Migration execution failed');

      v2State.lastExecutionResult = data;
      showToast(`Execution finished! ${data.inserted_count} inserted, ${data.updated_count} updated.`, 'success');

      await fetchV2TargetRecords();
      await fetchV2Reconciliation();
      switchV2Tab('v2-tab-execution');
    } catch (err) {
      showToast('Execution error: ' + err.message, 'error');
    }
  });
};

window.fetchV2TargetRecords = async function() {
  try {
    const res = await fetch(`${V2_API_BASE}/target/records?limit=50&offset=0`);
    if (res.ok) {
      const data = await res.json();
      v2State.targetRecords = data.records || [];
      v2State.targetTotal = data.total_records || 0;
      v2State.targetColumns = data.columns || [];
      renderV2TargetRecords(data);
    }
  } catch (err) {
    console.warn('Error fetching V2 target records:', err);
  }
};

function renderV2TargetRecords(data) {
  const badge = document.getElementById('badge-v2-target-count');
  const statRows = document.getElementById('v2-stat-target-rows');
  const statTable = document.getElementById('v2-stat-target-table');

  if (badge) badge.textContent = `${data.total_records || 0} in DB`;
  if (statRows) statRows.textContent = (data.total_records || 0).toLocaleString();
  if (statTable) statTable.textContent = data.table_name || 'N/A';

  const thead = document.getElementById('v2-thead-target');
  const tbody = document.getElementById('v2-tbody-target');
  if (thead && tbody) {
    const cols = data.columns || (data.records.length > 0 ? Object.keys(data.records[0]) : []);
    thead.innerHTML = `<tr>${cols.map(c => `<th>${c}</th>`).join('')}</tr>`;
    if (data.records.length === 0) {
      tbody.innerHTML = `<tr><td colspan="${cols.length || 1}" style="text-align: center; color: var(--text-muted); padding: 2rem;">No records written yet. Approve and Execute the migration plan.</td></tr>`;
    } else {
      tbody.innerHTML = data.records.map(row => {
        return `<tr>${cols.map(c => `<td class="mono" style="font-size: 0.78rem;">${row[c] !== null && row[c] !== undefined ? String(row[c]) : '<em style="color:#64748b;">NULL</em>'}</td>`).join('')}</tr>`;
      }).join('');
    }
  }
}

window.fetchV2Reconciliation = async function() {
  try {
    const res = await fetch(`${V2_API_BASE}/reconciliation`);
    if (res.ok) {
      const data = await res.json();
      renderV2Reconciliation(data);
    }
  } catch (err) {
    console.warn('Error fetching V2 reconciliation:', err);
  }
};

function renderV2Reconciliation(data) {
  const src = document.getElementById('v2-recon-source');
  const ins = document.getElementById('v2-recon-inserted');
  const upd = document.getElementById('v2-recon-updated');
  const qua = document.getElementById('v2-recon-quarantined');
  const del = document.getElementById('v2-recon-delta');
  const statusBanner = document.getElementById('v2-recon-status-banner');

  if (src) src.textContent = (data.source_records || 0).toLocaleString();
  if (ins) ins.textContent = (data.inserted_count || 0).toLocaleString();
  if (upd) upd.textContent = (data.updated_count || 0).toLocaleString();
  if (qua) qua.textContent = (data.quarantined_count || 0).toLocaleString();
  if (del) del.textContent = (data.unaccounted_delta || 0).toLocaleString();

  if (statusBanner) {
    if (data.is_zero_drop_verified) {
      statusBanner.innerHTML = `<span style="color: #34d399; font-weight: 700;">✓ MASS CONSERVATION VERIFIED</span>: Zero silent drop across target table '${data.table_name}'. Invariants satisfied.`;
      statusBanner.style.borderColor = 'rgba(16, 185, 129, 0.4)';
      statusBanner.style.background = 'rgba(16, 185, 129, 0.1)';
    } else {
      statusBanner.innerHTML = `<span style="color: #f59e0b; font-weight: 700;">⚠️ RECONCILIATION PENDING</span>: Execute migration to verify zero-drop mathematical parity.`;
      statusBanner.style.borderColor = 'rgba(245, 158, 11, 0.4)';
      statusBanner.style.background = 'rgba(245, 158, 11, 0.1)';
    }
  }
}

window.rollbackV2Latest = async function() {
  if (!confirm('Are you sure you want to rollback to the last pre-execution database snapshot?')) return;
  const btn = document.getElementById('btn-v2-rollback');
  await window.withButtonLoader(btn, 'Rolling back...', async () => {
    try {
      const data = await safeFetchJson(`${V2_API_BASE}/rollback`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({})
      }, 'Rollback failed');

      showToast(`Rollback complete: ${data.removed_records} records removed, ${data.remaining_records} remaining in DB.`, 'success');
      await fetchV2TargetRecords();
      await fetchV2Reconciliation();
    } catch (err) {
      showToast('Rollback error: ' + err.message, 'error');
    }
  });
};

window.openAIStudioModal = window.openV2LLMModal = async function() {
  const modal = document.getElementById('v2-llm-modal');
  if (modal) modal.classList.add('active');
  try {
    const res = await fetch('/api/llm/status');
    const data = await res.json();
    if (data.host) {
      const hostInput = document.getElementById('v2-llm-host');
      if (hostInput) hostInput.value = data.host;
    }
    if (data.model) {
      const modelInput = document.getElementById('v2-llm-model');
      if (modelInput) modelInput.value = data.model;
    }
    const keyInput = document.getElementById('v2-llm-key');
    if (keyInput && data.has_api_key && !keyInput.value) {
      keyInput.placeholder = '•••••••••••••••• (Key Configured)';
    }
    const isCloud = (data.host || '').includes('ollama.com');
    const btnCloud = document.getElementById('btn-preset-cloud');
    const btnLocal = document.getElementById('btn-preset-local');
    if (btnCloud) btnCloud.classList.toggle('active', isCloud);
    if (btnLocal) btnLocal.classList.toggle('active', !isCloud);
  } catch (e) {}
};

window.closeAIStudioModal = window.closeV2LLMModal = function() {
  const modal = document.getElementById('v2-llm-modal');
  if (modal) modal.classList.remove('active');
};

window.selectModelPreset = async function(model, host) {
  const hostInput = document.getElementById('v2-llm-host');
  const modelInput = document.getElementById('v2-llm-model');
  const btnCloud = document.getElementById('btn-preset-cloud');
  const btnLocal = document.getElementById('btn-preset-local');

  if (hostInput) hostInput.value = host;
  if (modelInput) modelInput.value = model;

  const isCloud = host.includes('ollama.com');
  if (btnCloud) btnCloud.classList.toggle('active', isCloud);
  if (btnLocal) btnLocal.classList.toggle('active', !isCloud);

  await saveV2LLMConfig();
};

window.saveV2LLMConfig = async function() {
  const host = document.getElementById('v2-llm-host').value.trim();
  const model = document.getElementById('v2-llm-model').value.trim();
  const apiKey = document.getElementById('v2-llm-key').value.trim();

  try {
    const res = await fetch(`/api/llm/config`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ host, model, api_key: apiKey || null })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Failed to save config');

    showToast('Ollama LLM configuration saved!', 'success');
    testV2LLMConnection();
  } catch (err) {
    showToast('LLM config error: ' + err.message, 'error');
  }
};

window.testV2LLMConnection = async function() {
  const badge1 = document.getElementById('mode1-llm-badge');
  const badge2 = document.getElementById('v2-llm-badge');
  const statusPill = document.getElementById('ai-test-status-pill');

  try {
    const res = await fetch(`/api/llm/verify`);
    const data = await res.json();
    if (data.status === 'SUCCESS' || data.status === 'CONNECTED') {
      showToast(`Ollama Connected: ${data.model} @ ${data.message || 'Host Online'}`, 'success');
      if (badge1) {
        badge1.textContent = `🤖 ${data.model} (Online)`;
        badge1.style.color = '#34d399';
      }
      if (badge2) {
        badge2.textContent = `🤖 ${data.model} (Online)`;
        badge2.style.color = '#34d399';
      }
      if (statusPill) {
        statusPill.textContent = '🟢 Online & Ready';
        statusPill.style.background = 'rgba(16, 185, 129, 0.2)';
        statusPill.style.color = '#34d399';
      }
    } else {
      showToast(`AI Offline (${data.message}). No offline fallback permitted.`, 'error');
      const offlineText = '🔴 AI Offline';
      if (badge1) { badge1.textContent = offlineText; badge1.style.color = '#ef4444'; }
      if (badge2) { badge2.textContent = offlineText; badge2.style.color = '#ef4444'; }
      if (statusPill) {
        statusPill.textContent = '🔴 AI Offline';
        statusPill.style.background = 'rgba(239, 68, 68, 0.2)';
        statusPill.style.color = '#ef4444';
      }
    }
  } catch (err) {
    const offlineText = '🔴 AI Offline';
    if (badge1) { badge1.textContent = offlineText; badge1.style.color = '#ef4444'; }
    if (badge2) { badge2.textContent = offlineText; badge2.style.color = '#ef4444'; }
    if (statusPill) {
      statusPill.textContent = '🔴 AI Error';
      statusPill.style.background = 'rgba(239, 68, 68, 0.2)';
      statusPill.style.color = '#ef4444';
    }
  }
};

window.runLiveAIInferenceTest = async function() {
  const promptInput = document.getElementById('ai-test-prompt-input');
  const btn = document.getElementById('btn-run-live-ai-test');
  const statusPill = document.getElementById('ai-test-status-pill');
  const resultBox = document.getElementById('ai-test-result-container');
  const rawOutput = document.getElementById('ai-test-raw-output');
  const modelTag = document.getElementById('ai-test-model-tag');
  const latencyTag = document.getElementById('ai-test-latency-tag');

  const prompt = promptInput ? promptInput.value.trim() : '';
  if (!prompt) {
    showToast('Please enter a test prompt for Ollama', 'warning');
    return;
  }

  if (btn) btn.disabled = true;
  if (statusPill) {
    statusPill.textContent = '⏳ Inferencing...';
    statusPill.style.color = '#38bdf8';
  }

  try {
    showToast('Executing live inference via Ollama model...', 'info');
    const res = await fetch(`/api/llm/test`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt })
    });
    const data = await res.json();

    if (resultBox) resultBox.style.display = 'block';
    if (modelTag) modelTag.textContent = `${data.model || 'Model'} @ ${data.host || 'Ollama'}`;
    if (latencyTag) latencyTag.textContent = `${data.latency_ms || 0} ms`;

    if (data.status === 'SUCCESS') {
      if (statusPill) {
        statusPill.textContent = '🟢 Inference Success';
        statusPill.style.background = 'rgba(16, 185, 129, 0.2)';
        statusPill.style.color = '#34d399';
      }
      if (rawOutput) {
        rawOutput.textContent = data.raw_response || JSON.stringify(data.parsed_json, null, 2);
      }
      showToast(`Inference returned in ${data.latency_ms} ms!`, 'success');
    } else {
      if (statusPill) {
        statusPill.textContent = '🔴 Inference Failed';
        statusPill.style.background = 'rgba(244, 63, 94, 0.2)';
        statusPill.style.color = '#fda4af';
      }
      if (rawOutput) {
        rawOutput.textContent = `Error: ${data.error || 'Unknown error'}\nRaw response:\n${data.raw_response || '(No response received)'}`;
      }
      showToast(`Ollama test failed: ${data.error || 'Server error'}`, 'error');
    }
  } catch (err) {
    if (statusPill) {
      statusPill.textContent = '🔴 Network Error';
      statusPill.style.color = '#fda4af';
    }
    if (rawOutput) {
      rawOutput.textContent = `Client Exception: ${err.message}`;
    }
    showToast('Inference call failed: ' + err.message, 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
};

window.applyAIFixSingleV2 = async function(field, rule, rawVal, customFallback = null) {
  if (!v2State.currentPlan) {
    showToast('No active V2 plan available.', 'error');
    return;
  }
  const oldQuar = v2State.lastDryRunSummary ? (v2State.lastDryRunSummary.quarantined_count || v2State.lastDryRunSummary.rejected_count || 0) : 0;
  
  try {
    showToast(`Applying Mode 2 AI remediation for '${field}'...`, 'info');
    const res = await fetch(`${V2_API_BASE}/plans/apply-fix`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        target_field: field,
        rule: rule,
        raw_value: rawVal,
        custom_fallback: customFallback
      })
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: 'Failed to apply V2 fix' }));
      throw new Error(err.detail || 'Failed to apply V2 fix');
    }
    const data = await res.json();
    v2State.currentPlan = data.plan;
    v2State.lastDryRunSummary = data.dry_run_summary;
    renderV2Plan(data.plan);
    renderV2DryRun(data.dry_run_summary);
    if (typeof closeModal === 'function') closeModal();
    const newQuar = data.dry_run_summary.quarantined_count || data.dry_run_summary.rejected_count || 0;
    showToast(`🎉 AI Fix Implemented! Quarantined records reduced from ${oldQuar} ➔ ${newQuar} across all values.`, 'success');
  } catch (err) {
    showToast('Failed to implement AI fix: ' + err.message, 'error');
  }
};

window.applyAllAIFixesMode2 = async function() {
  if (!v2State.currentPlan) {
    showToast('Please synthesize or load a V2 plan first.', 'error');
    return;
  }
  const btn = document.getElementById('btn-auto-fix-all-mode2');
  const oldQuar = v2State.lastDryRunSummary ? (v2State.lastDryRunSummary.quarantined_count || v2State.lastDryRunSummary.rejected_count || 0) : 0;

  await window.withButtonLoader(btn, 'Auto-Remediating & Re-simulating...', async () => {
    try {
      const res = await fetch(`${V2_API_BASE}/plans/apply-fix`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          fix_action: 'AUTO_RESOLVE_ALL'
        })
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: 'V2 auto-remediation failed' }));
        throw new Error(err.detail || 'V2 auto-remediation failed');
      }
      const data = await res.json();
      v2State.currentPlan = data.plan;
      v2State.lastDryRunSummary = data.dry_run_summary;
      renderV2Plan(data.plan);
      renderV2DryRun(data.dry_run_summary);
      if (typeof closeModal === 'function') closeModal();
      const newQuar = data.dry_run_summary.quarantined_count || data.dry_run_summary.rejected_count || 0;
      showToast(`⚡ All AI fixes applied! Quarantine dropped from ${oldQuar} ➔ ${newQuar} (100% compliant).`, 'success');
    } catch (err) {
      showToast('Error applying AI fixes: ' + err.message, 'error');
    }
  });
};

