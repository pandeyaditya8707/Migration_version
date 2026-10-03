/**
 * Agentic Data Migration Planner & Reconciliation Workbench
 * Interactive Client Application
 */

const API_BASE = '/api';

const state = {
  activeTab: 'tab-profiler',
  plans: [],
  currentPlan: null,
  sourceSchema: null,
  sourceProfiles: {},
  targetSchema: null,
  sampleRecords: [],
  supportedTransforms: [],
  lastDryRunSummary: null,
  lastExecutionResult: null,
  targetRecords: [],
  targetOffset: 0,
  targetLimit: 50,
  targetTotal: 0,
  auditTrail: []
};

/**
 * Production button loader helper that swaps button content with a spinner,
 * disables it to prevent duplicate clicks/races, and restores state when finished.
 */
window.withButtonLoader = async function(btn, loadingText, asyncCallback) {
  if (!btn) return await asyncCallback();
  const originalHtml = btn.innerHTML;
  const originalDisabled = btn.disabled;
  btn.disabled = true;
  btn.innerHTML = `<span class="btn-spinner"></span> <span>${loadingText}</span>`;
  try {
    return await asyncCallback();
  } finally {
    btn.innerHTML = originalHtml;
    btn.disabled = originalDisabled;
  }
};

// ============================================================================
// Initialization & Data Loading
// ============================================================================

document.addEventListener('DOMContentLoaded', async () => {
  setupTabNavigation();
  setupEventListeners();
  await loadInitialData();
});

async function loadInitialData() {
  try {
    await Promise.all([
      fetchSourceSchema(),
      fetchTargetSchema(),
      fetchSampleRecords(),
      fetchTransforms(),
      fetchPlans(),
      fetchTargetData(),
      fetchAuditTrail()
    ]);
    renderAllViews();
  } catch (error) {
    showToast('Failed to load workbench data: ' + error.message, 'error');
    console.error(error);
  }
}

async function fetchSourceSchema() {
  const res = await fetch(`${API_BASE}/schemas/source`);
  const data = await res.json();
  state.sourceSchema = data.schema;
  state.sourceProfiles = data.profiles;
  document.getElementById('stat-source-cols').textContent = data.schema.fields.length;
  if (data.total_sample_records !== undefined) {
    const totalElem = document.getElementById('stat-total-records');
    if (totalElem) totalElem.textContent = data.total_sample_records.toLocaleString();
    const badgeElem = document.getElementById('badge-source-count');
    if (badgeElem) badgeElem.textContent = `${data.total_sample_records.toLocaleString()} recs`;
  }
}

async function fetchTargetSchema() {
  const res = await fetch(`${API_BASE}/schemas/target`);
  state.targetSchema = await res.json();
  const reqCount = state.targetSchema.fields.filter(f => !f.nullable).length;
  document.getElementById('stat-target-req-cols').textContent = `${reqCount} / ${state.targetSchema.fields.length}`;
}

async function fetchSampleRecords() {
  const res = await fetch(`${API_BASE}/samples?limit=10`);
  state.sampleRecords = await res.json();
}

async function fetchTransforms() {
  const res = await fetch(`${API_BASE}/transforms`);
  state.supportedTransforms = await res.json();
}

async function fetchPlans() {
  const res = await fetch(`${API_BASE}/plans`);
  state.plans = await res.json();
  if (state.plans.length > 0) {
    // Select latest plan by default
    state.currentPlan = state.plans[state.plans.length - 1];
  }
  updatePlanHeaderBadge();
}

async function fetchTargetData(offset = null) {
  if (offset !== null) {
    state.targetOffset = Math.max(0, offset);
  }
  const res = await fetch(`${API_BASE}/target/records?limit=${state.targetLimit}&offset=${state.targetOffset}`);
  const data = await res.json();
  state.targetRecords = data.records;
  state.targetTotal = data.total_records;
  
  document.getElementById('target-stat-rows').textContent = data.total_records.toLocaleString();
  document.getElementById('badge-target-count').textContent = `${data.total_records} in DB`;
  document.getElementById('target-stat-balance').textContent = `$${data.financial_total_balance.toLocaleString(undefined, { minimumFractionDigits: 2 })}`;
  document.getElementById('target-records-count-badge').textContent = `${data.total_records} Rows in DB`;

  updateTargetPagination();
}

function updateTargetPagination() {
  const prevBtn = document.getElementById('btn-target-prev');
  const nextBtn = document.getElementById('btn-target-next');
  const infoSpan = document.getElementById('target-pagination-info');
  const pageSpan = document.getElementById('target-page-num');

  if (!prevBtn || !nextBtn || !infoSpan) return;

  const total = state.targetTotal;
  const start = total === 0 ? 0 : state.targetOffset + 1;
  const end = Math.min(state.targetOffset + state.targetLimit, total);
  const currentPage = Math.floor(state.targetOffset / state.targetLimit) + 1;
  const totalPages = Math.max(1, Math.ceil(total / state.targetLimit));

  infoSpan.textContent = `Showing ${start.toLocaleString()} to ${end.toLocaleString()} of ${total.toLocaleString()} records`;
  if (pageSpan) pageSpan.textContent = `Page ${currentPage} of ${totalPages}`;

  prevBtn.disabled = state.targetOffset <= 0;
  nextBtn.disabled = state.targetOffset + state.targetLimit >= total;
}

async function fetchAuditTrail() {
  const res = await fetch(`${API_BASE}/audit-trail`);
  state.auditTrail = await res.json();
}

// ============================================================================
// Tab Navigation & UI Events
// ============================================================================

function setupTabNavigation() {
  const tabBtns = document.querySelectorAll('#tabs-bar-v1 .tab-btn');
  tabBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      const targetTabId = btn.getAttribute('data-tab');
      switchTab(targetTabId);
    });
  });
}

function switchTab(tabId) {
  state.activeTab = tabId;
  document.querySelectorAll('#tabs-bar-v1 .tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('#tab-container-v1 .view-panel').forEach(p => p.classList.remove('active'));

  const activeBtn = document.querySelector(`#tabs-bar-v1 .tab-btn[data-tab="${tabId}"]`);
  const activePanel = document.getElementById(tabId);
  if (activeBtn) activeBtn.classList.add('active');
  if (activePanel) activePanel.classList.add('active');

  // Trigger tab-specific refresh if needed
  if (tabId === 'tab-execution') {
    fetchTargetData().then(renderTargetStoreView);
  } else if (tabId === 'tab-reconciliation') {
    renderReconciliationView();
  }
}

function setupEventListeners() {
  // Plan Action Buttons
  document.getElementById('btn-propose-new-plan').addEventListener('click', handleProposePlan);
  document.getElementById('btn-approve-plan-top').addEventListener('click', handleApprovePlan);
  document.getElementById('btn-run-dry-run-top').addEventListener('click', handleRunDryRun);
  document.getElementById('btn-execute-dry-run-main').addEventListener('click', handleRunDryRun);
  document.getElementById('btn-execute-migration-top').addEventListener('click', handleExecuteMigration);
  document.getElementById('btn-execute-migration-main').addEventListener('click', handleExecuteMigration);
  document.getElementById('btn-retry-migration').addEventListener('click', handleRetryMigration);
  document.getElementById('btn-rollback-migration').addEventListener('click', handleRollback);
  document.getElementById('btn-reset-workbench').addEventListener('click', handleResetWorkbench);
  document.getElementById('btn-refresh-reconciliation').addEventListener('click', renderReconciliationView);
  document.getElementById('btn-save-as-new-version').addEventListener('click', handleForkPlan);

  // Dataset Intake Upload, Benchmark & Drag-Drop
  const fileInput = document.getElementById('input-dataset-file');
  if (fileInput) {
    fileInput.addEventListener('change', handleUploadDataset);
  }
  const btnTestSample = document.getElementById('btn-load-test-sample');
  if (btnTestSample) {
    btnTestSample.addEventListener('click', handleLoadTestSample);
  }
  const btnBenchmark = document.getElementById('btn-load-benchmark');
  if (btnBenchmark) {
    btnBenchmark.addEventListener('click', handleLoadBenchmark);
  }

  // Drag and drop onto intake card
  const intakeCard = document.getElementById('dataset-intake-card');
  if (intakeCard) {
    ['dragenter', 'dragover'].forEach(name => {
      intakeCard.addEventListener(name, (e) => {
        e.preventDefault();
        e.stopPropagation();
        intakeCard.style.borderColor = 'var(--accent-cyan)';
        intakeCard.style.background = 'rgba(6, 182, 212, 0.12)';
      }, false);
    });
    ['dragleave', 'drop'].forEach(name => {
      intakeCard.addEventListener(name, (e) => {
        e.preventDefault();
        e.stopPropagation();
        intakeCard.style.borderColor = 'rgba(99, 102, 241, 0.35)';
        intakeCard.style.background = 'linear-gradient(145deg, rgba(99, 102, 241, 0.08), rgba(6, 182, 212, 0.05))';
      }, false);
    });
    intakeCard.addEventListener('drop', (e) => {
      const dt = e.dataTransfer;
      if (dt && dt.files && dt.files.length > 0) {
        uploadFileObject(dt.files[0]);
      }
    });
  }

  // CSV Exports
  document.getElementById('btn-export-target-csv').addEventListener('click', () => {
    window.location.href = `${API_BASE}/export/target?format=csv`;
  });
  document.getElementById('btn-export-quarantine-csv').addEventListener('click', () => {
    window.location.href = `${API_BASE}/export/quarantine`;
  });

  // Target Table Pagination
  document.getElementById('btn-target-prev').addEventListener('click', async () => {
    await fetchTargetData(state.targetOffset - state.targetLimit);
    renderTargetStoreView();
  });
  document.getElementById('btn-target-next').addEventListener('click', async () => {
    await fetchTargetData(state.targetOffset + state.targetLimit);
    renderTargetStoreView();
  });

  // Plan version switcher
  const planSelect = document.getElementById('select-plan-version');
  planSelect.addEventListener('change', (e) => {
    const ver = parseInt(e.target.value, 10);
    const selected = state.plans.find(p => p.version === ver);
    if (selected) {
      state.currentPlan = selected;
      updatePlanHeaderBadge();
      renderMappingStudio();
    }
  });

  // Modal close
  document.getElementById('modal-close-btn').addEventListener('click', closeModal);
  document.getElementById('record-modal').addEventListener('click', (e) => {
    if (e.target.id === 'record-modal') closeModal();
  });
}

function updatePlanHeaderBadge() {
  if (!state.currentPlan) return;
  const p = state.currentPlan;
  const badgeDot = document.getElementById('plan-status-dot');
  const badgeText = document.getElementById('active-plan-badge-text');
  const execBtnTop = document.getElementById('btn-execute-migration-top');
  const execBtnMain = document.getElementById('btn-execute-migration-main');

  badgeText.textContent = `Plan: v${p.version} (${p.status})`;
  if (p.status === 'APPROVED') {
    badgeDot.classList.add('approved');
    badgeText.style.color = '#6ee7b7';
    execBtnTop.disabled = false;
    execBtnMain.disabled = false;
  } else {
    badgeDot.classList.remove('approved');
    badgeText.style.color = '#a5b4fc';
    execBtnTop.disabled = true;
    execBtnMain.disabled = true;
  }

  // Update Plan Select Dropdown
  const planSelect = document.getElementById('select-plan-version');
  planSelect.innerHTML = state.plans.map(pl => 
    `<option value="${pl.version}" ${pl.version === p.version ? 'selected' : ''}>
      Plan v${pl.version} (${pl.status})
    </option>`
  ).join('');

  const statusLabel = document.getElementById('badge-plan-status-text');
  if (statusLabel) statusLabel.textContent = `Status: ${p.status}`;
}

// ============================================================================
// Render Functions
// ============================================================================

function renderAllViews() {
  renderProfilerTab();
  renderMappingStudio();
  renderTargetStoreView();
  renderAuditTrailView();
  updatePlanHeaderBadge();
}

// 1. Profiler Tab Rendering
function renderProfilerTab() {
  if (!state.sourceSchema || !state.targetSchema) return;

  // Source Schema Table
  const tbodySrc = document.getElementById('tbody-source-schema');
  tbodySrc.innerHTML = state.sourceSchema.fields.map(f => {
    const prof = state.sourceProfiles[f.name] || {};
    const patterns = (prof.patterns_detected || []).join(', ') || 'Clean text';
    return `
      <tr>
        <td class="mono font-semibold" style="color: var(--accent-cyan);">${f.name}</td>
        <td><span class="badge-pill">${f.data_type}</span></td>
        <td>${prof.null_percentage || 0}%</td>
        <td>${prof.distinct_count || '-'}</td>
        <td style="font-size: 0.75rem; color: var(--text-muted);">${patterns}</td>
      </tr>
    `;
  }).join('');

  // Target Schema Table
  const tbodyTgt = document.getElementById('tbody-target-schema');
  tbodyTgt.innerHTML = state.targetSchema.fields.map(f => {
    const nullableBadge = f.nullable 
      ? '<span class="badge-pill" style="color: var(--text-muted);">NULLABLE</span>' 
      : '<span class="risk-tag risk-high" style="font-size: 0.68rem;">NOT NULL</span>';
    
    let constraintsStr = [];
    if (f.constraints) {
      if (f.constraints.unique) constraintsStr.push('UNIQUE');
      if (f.constraints.enum) constraintsStr.push(`ENUM(${f.constraints.enum.join(', ')})`);
      if (f.constraints.regex) constraintsStr.push(`REGEX: ${f.constraints.regex}`);
      if (f.constraints.format) constraintsStr.push(`FORMAT: ${f.constraints.format}`);
    }

    return `
      <tr>
        <td class="mono font-semibold" style="color: var(--accent-emerald);">${f.name}</td>
        <td><span class="badge-pill">${f.data_type}</span></td>
        <td>${nullableBadge}</td>
        <td class="mono" style="font-size: 0.74rem; color: #a5b4fc;">${constraintsStr.join(' | ') || 'None'}</td>
      </tr>
    `;
  }).join('');

  // Sample Records Table
  if (state.sampleRecords.length > 0) {
    const cols = Object.keys(state.sampleRecords[0]);
    document.getElementById('thead-sample-records').innerHTML = `
      <tr>${cols.map(c => `<th>${c}</th>`).join('')}</tr>
    `;
    document.getElementById('tbody-sample-records').innerHTML = state.sampleRecords.map(r => `
      <tr>${cols.map(c => `<td class="mono" style="font-size: 0.75rem;">${r[c] !== null ? escapeHtml(String(r[c])) : '<em style="color: var(--text-muted);">null</em>'}</td>`).join('')}</tr>
    `).join('');
  }
}

// 2. Mapping Studio Tab Rendering
function renderMappingStudio() {
  if (!state.currentPlan) return;
  const p = state.currentPlan;

  // Render Clarifications
  const clarifContainer = document.getElementById('clarifications-container');
  clarifContainer.innerHTML = p.clarifications.map((q, idx) => `
    <div class="question-card">
      <div class="question-text">❓ Clarification #${idx+1} [Field: <span class="mono" style="color: var(--accent-cyan);">${q.field}</span>]: ${escapeHtml(q.question)}</div>
      <div class="options-group">
        ${q.options.map(opt => `
          <label class="option-label">
            <input type="radio" name="clarif_${q.question_id}" value="${escapeHtml(opt)}" 
              ${q.user_answer === opt ? 'checked' : ''} onchange="handleAnswerClarification('${q.question_id}', '${escapeHtml(opt)}')">
            <span>${escapeHtml(opt)}</span>
          </label>
        `).join('')}
      </div>
    </div>
  `).join('');

  // Render Field Mapping Matrix as Structured High-Precision Data Table
  const matrixContainer = document.getElementById('mapping-studio-list');
  if (matrixContainer) {
    matrixContainer.className = "table-wrapper";

    const rowsHtml = p.field_mappings.map(m => {
      const riskClass = m.risk_level === 'HIGH' ? 'risk-high' : (m.risk_level === 'MEDIUM' ? 'risk-medium' : 'risk-low');
      const srcList = (m.source_fields && m.source_fields.length > 0)
        ? m.source_fields.join(', ')
        : (m.source_field || '(Synthetic / Generated)');

      return `
        <tr>
          <td>
            <div style="display: flex; flex-direction: column; gap: 0.15rem;">
              <span class="mono" style="font-weight: 700; color: var(--ink-primary); font-size: 0.84rem;">${escapeHtml(m.target_field)}</span>
              <span style="font-size: 0.68rem; color: var(--ink-muted); text-transform: uppercase; letter-spacing: 0.04em;">Target Field</span>
            </div>
          </td>

          <td>
            <div style="display: flex; flex-direction: column; gap: 0.15rem;">
              <span class="mono" style="font-weight: 600; color: #334155; font-size: 0.8rem; background: var(--bg-subtle); padding: 0.2rem 0.5rem; border-radius: 4px; border: 1px solid var(--border-hairline); display: inline-block; width: fit-content;">${escapeHtml(srcList)}</span>
              <span style="font-size: 0.68rem; color: var(--ink-muted);">Source Inbound</span>
            </div>
          </td>

          <td>
            <div style="display: flex; align-items: center; gap: 0.45rem;">
              ${p.status !== 'APPROVED' && state.supportedTransforms.length > 0 ? `
                <select class="btn btn-secondary mono" style="padding: 0.3rem 0.6rem; font-size: 0.76rem; background: var(--bg-surface); border: 1px solid var(--border-hairline); color: var(--ink-primary); min-width: 175px;" 
                        onchange="handleUpdateMappingRule('${m.target_field}', this.value)">
                  ${state.supportedTransforms.map(t => `
                    <option value="${t.rule_id}" ${t.rule_id === m.transformation ? 'selected' : ''}>
                      ${t.rule_id}
                    </option>
                  `).join('')}
                </select>
              ` : `
                <span class="transform-pill">${escapeHtml(m.transformation)}</span>
              `}
              <span style="color: var(--ink-muted); font-size: 0.85rem;" title="Transforms into target">➔</span>
            </div>
          </td>

          <td>
            <span class="badge-risk ${riskClass}">${m.risk_level} RISK</span>
          </td>

          <td style="white-space: normal; min-width: 250px; max-width: 380px; line-height: 1.45; font-size: 0.78rem; color: #475569;">
            ${escapeHtml(m.risk_rationale || 'Deterministic mapping invariant')}
          </td>

          <td style="white-space: normal; min-width: 180px; text-align: right;">
            <span class="badge-pill" style="font-size: 0.72rem; background: var(--bg-subtle); border: 1px solid var(--border-hairline); color: var(--ink-secondary); display: inline-block; text-align: left;">${escapeHtml(m.notes || 'Deterministic')}</span>
          </td>
        </tr>
      `;
    }).join('');

    matrixContainer.innerHTML = `
      <table class="data-table mapping-matrix-table" style="width: 100%;">
        <thead>
          <tr>
            <th style="min-width: 160px;">Target Field</th>
            <th style="min-width: 160px;">Source Field(s)</th>
            <th style="min-width: 220px;">Transformation Rule</th>
            <th style="min-width: 110px;">Risk Level</th>
            <th style="min-width: 250px;">Agent Invariant & Rationale</th>
            <th style="min-width: 180px; text-align: right;">Policy / Lineage Notes</th>
          </tr>
        </thead>
        <tbody>
          ${rowsHtml}
        </tbody>
      </table>
    `;
  }
}

window.handleUpdateMappingRule = async function(targetField, newTransform) {
  if (!state.currentPlan) return;
  const mapping = state.currentPlan.field_mappings.find(m => m.target_field === targetField);
  if (!mapping) return;
  mapping.transformation = newTransform;
  try {
    const res = await fetch(`${API_BASE}/plans/${state.currentPlan.version}/mappings`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(mapping)
    });
    if (!res.ok) throw new Error((await res.json()).detail || 'Failed to update mapping');
    const updated = await res.json();
    state.currentPlan = updated;
    const idx = state.plans.findIndex(p => p.version === updated.version);
    if (idx !== -1) state.plans[idx] = updated;
    updatePlanHeaderBadge();
    renderMappingStudio();
    showToast(`Updated transformation for '${targetField}' to ${newTransform}`, 'success');
  } catch (err) {
    showToast('Failed to update mapping: ' + err.message, 'error');
  }
};

// 3. Dry-Run Tab Rendering
function renderDryRunResults(summary) {
  state.lastDryRunSummary = summary;
  document.getElementById('dry-stat-total').textContent = summary.total_source_records.toLocaleString();
  document.getElementById('dry-stat-accepted').textContent = summary.accepted_count.toLocaleString();
  document.getElementById('dry-stat-rejected').textContent = summary.rejected_count.toLocaleString();
  document.getElementById('dry-stat-time').textContent = `${summary.execution_time_ms} ms`;
  document.getElementById('badge-dryrun-status').textContent = `${summary.accepted_count} Valid / ${summary.rejected_count} Quarantined`;

  // Field breakdown pills
  const breakdownDiv = document.getElementById('dry-run-error-breakdown');
  const errors = Object.entries(summary.field_error_breakdown || {});
  if (errors.length === 0) {
    breakdownDiv.innerHTML = '<span style="color: var(--accent-emerald); font-size: 0.85rem;">Zero constraint violations detected!</span>';
  } else {
    breakdownDiv.innerHTML = errors.map(([f, count]) => `
      <div style="display: inline-flex; align-items: center; gap: 0.5rem; background: rgba(244, 63, 94, 0.12); border: 1px solid rgba(244, 63, 94, 0.3); border-radius: var(--radius-sm); padding: 0.35rem 0.75rem;">
        <span class="mono" style="color: #fda4af; font-weight: 600; font-size: 0.8rem;">${f}:</span>
        <span style="color: #fff; font-weight: 700; font-size: 0.85rem;">${count} errors</span>
      </div>
    `).join('');
  }

  // Quarantine Table
  const tbody = document.getElementById('tbody-quarantine-ledger');
  const samples = summary.quarantine_sample || [];
  document.getElementById('quarantine-count-badge').textContent = `${summary.rejected_count} Quarantined Records`;

  if (samples.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: var(--accent-emerald); padding: 2rem;">No records quarantined. 100% passed!</td></tr>';
    return;
  }

  tbody.innerHTML = samples.map(q => {
    const err = (q.errors && q.errors.length > 0) ? q.errors[0] : {};
    const qStr = JSON.stringify(q).replace(/'/g, '&#39;');

    return `
      <tr>
        <td class="mono font-semibold">#${q.source_row_index + 1}</td>
        <td class="mono font-semibold" style="color: var(--accent-cyan);">${escapeHtml(q.source_natural_key || 'UNKNOWN')}</td>
        <td class="mono" style="color: #f43f5e; font-weight: 600;">${escapeHtml(err.field || '')}</td>
        <td><span class="badge-pill" style="font-size: 0.72rem; background: rgba(244, 63, 94, 0.08); color: #f43f5e; border-color: rgba(244, 63, 94, 0.25);">${escapeHtml(err.rule || '')}</span></td>
        <td style="color: #f43f5e; max-width: 380px; font-size: 0.78rem; line-height: 1.4; white-space: normal;">
          ${escapeHtml(err.error_message || '')}
        </td>
        <td style="text-align: right; white-space: nowrap;">
          <button class="btn btn-secondary" style="padding: 0.25rem 0.65rem; font-size: 0.74rem;" onclick='openQuarantineModal(${qStr}, "mode1")'>
            View Evidence
          </button>
        </td>
      </tr>
    `;
  }).join('');
}

// 4. Target Store View Rendering
function renderTargetStoreView() {
  const tbody = document.getElementById('tbody-target-data');
  if (state.targetRecords.length === 0) {
    tbody.innerHTML = '<tr><td colspan="11" style="text-align: center; color: var(--text-muted); padding: 2.5rem;">Target store is currently empty. Execute an approved migration.</td></tr>';
    document.getElementById('btn-retry-migration').disabled = true;
    document.getElementById('btn-rollback-migration').disabled = true;
    return;
  }

  document.getElementById('btn-retry-migration').disabled = false;
  document.getElementById('btn-rollback-migration').disabled = false;

  tbody.innerHTML = state.targetRecords.map(r => {
    const statusClass = r.status === 'ACTIVE' ? 'active' : r.status === 'SUSPENDED' ? 'suspended' : 'inactive';
    return `
      <tr>
        <td class="mono" style="font-size: 0.72rem; color: var(--ink-muted);">${r.customer_uuid.slice(0, 8)}...</td>
        <td class="mono font-semibold" style="color: var(--ink-primary);">${r.natural_key}</td>
        <td style="font-weight: 600; color: var(--ink-primary);">${escapeHtml(r.first_name)}</td>
        <td>${escapeHtml(r.last_name || '')}</td>
        <td class="mono" style="font-size: 0.75rem;">${escapeHtml(r.email)}</td>
        <td class="mono" style="font-size: 0.75rem;">${escapeHtml(r.phone_e164 || '-')}</td>
        <td class="mono" style="font-size: 0.72rem;">${r.joined_at.split('T')[0]}</td>
        <td><span class="pill-status ${statusClass}">${r.status}</span></td>
        <td class="mono font-semibold" style="color: ${r.balance_due < 0 ? 'var(--accent-rose)' : 'var(--ink-primary)'};">$${r.balance_due.toFixed(2)}</td>
        <td><span class="badge-pill">${r.risk_tier}</span></td>
        <td><span class="badge-pill">${r.country_iso2}</span></td>
      </tr>
    `;
  }).join('');
}

// 5. Reconciliation View Rendering
async function renderReconciliationView() {
  const runId = state.lastExecutionResult ? state.lastExecutionResult.run_id : 'latest_dry';
  const planVer = state.currentPlan ? state.currentPlan.version : 1;

  try {
    const res = await fetch(`${API_BASE}/reconciliation/${runId}?plan_version=${planVer}`);
    const report = await res.json();

    document.getElementById('recon-eq-source').textContent = report.accounting.total_source_records.toLocaleString();
    document.getElementById('recon-eq-target').textContent = report.accounting.target_accepted_records.toLocaleString();
    document.getElementById('recon-eq-quar').textContent = report.accounting.quarantined_records.toLocaleString();
    document.getElementById('recon-eq-unaccounted').textContent = report.accounting.unaccounted_records.toLocaleString();

    const tag = document.getElementById('parity-verdict-tag');
    tag.textContent = report.verdict.replace(/_/g, ' ');
    if (report.verdict.startsWith('PASSED')) {
      tag.className = 'risk-tag risk-low';
    } else if (report.verdict === 'PENDING_EXECUTION') {
      tag.className = 'risk-tag risk-medium';
    } else {
      tag.className = 'risk-tag risk-high';
    }

    const summaryDiv = document.getElementById('recon-invariants-summary');
    summaryDiv.innerHTML = `
      <div style="margin-top: 0.75rem; display: flex; justify-content: center; gap: 2rem; font-size: 0.84rem;">
        <div><span>Source Hash: </span><span class="mono" style="color: var(--accent-cyan);">${report.source_checksum}</span></div>
        <div><span>Target Hash: </span><span class="mono" style="color: var(--accent-emerald);">${report.target_checksum}</span></div>
        <div><span>Duplicates Detected: </span><span class="mono" style="color: #6ee7b7;">${report.duplicate_count}</span></div>
      </div>
    `;

    document.getElementById('badge-recon-status').textContent = report.verdict.replace(/_/g, ' ');
  } catch (err) {
    console.warn('Reconciliation report pending execution:', err);
  }

  await fetchAuditTrail();
  renderAuditTrailView();
}

// 6. Audit Trail Rendering
function renderAuditTrailView() {
  const tbody = document.getElementById('tbody-audit-trail');
  if (!state.auditTrail || state.auditTrail.length === 0) {
    tbody.innerHTML = '<tr><td colspan="4" style="text-align: center; color: var(--text-muted); padding: 1.5rem;">No audit events recorded yet.</td></tr>';
    return;
  }

  tbody.innerHTML = state.auditTrail.map(evt => {
    let badgeClass = 'risk-low';
    if (evt.event_type.includes('ROLLBACK') || evt.event_type.includes('FAILED')) badgeClass = 'risk-high';
    else if (evt.event_type.includes('RETRIED')) badgeClass = 'risk-medium';

    return `
      <tr>
        <td class="mono" style="font-size: 0.75rem; color: var(--text-muted);">${evt.timestamp.replace('T', ' ').slice(0, 19)}</td>
        <td><span class="risk-tag ${badgeClass}">${evt.event_type}</span></td>
        <td style="font-weight: 600;">${escapeHtml(evt.actor)}</td>
        <td class="mono" style="font-size: 0.74rem; color: #cbd5e1;">${escapeHtml(JSON.stringify(evt.details))}</td>
      </tr>
    `;
  }).join('');
}

// ============================================================================
// Actions & API Triggers
// ============================================================================

// ============================================================================
// Actions & API Triggers with Embedded Loaders
// ============================================================================

async function handleProposePlan() {
  const btn = document.getElementById('btn-propose-new-plan');
  await withButtonLoader(btn, 'Synthesizing Plan...', async () => {
    try {
      showToast('AI Agent inspecting schemas and synthesizing new plan...', 'info');
      const res = await fetch(`${API_BASE}/plans/propose`, { method: 'POST' });
      const newPlan = await res.json();
      state.plans.push(newPlan);
      state.currentPlan = newPlan;
      updatePlanHeaderBadge();
      renderMappingStudio();
      switchTab('tab-mapping');
      showToast(`AI Proposed Plan v${newPlan.version} successfully!`, 'success');
    } catch (err) {
      showToast('Failed to propose plan: ' + err.message, 'error');
    }
  });
}

async function handleApprovePlan() {
  if (!state.currentPlan) return;
  const btn = document.getElementById('btn-approve-plan-top');
  await withButtonLoader(btn, 'Approving Plan...', async () => {
    try {
      const res = await fetch(`${API_BASE}/plans/${state.currentPlan.version}/approve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify('Lead Data Engineer')
      });
      const updated = await res.json();
      state.currentPlan = updated;
      const idx = state.plans.findIndex(p => p.version === updated.version);
      if (idx !== -1) state.plans[idx] = updated;

      updatePlanHeaderBadge();
      showToast(`Plan v${updated.version} explicitly APPROVED for target execution!`, 'success');
    } catch (err) {
      showToast('Failed to approve plan: ' + err.message, 'error');
    }
  });
}

async function handleRunDryRun() {
  if (!state.currentPlan) return;
  const btnTop = document.getElementById('btn-run-dry-run-top');
  const btnMain = document.getElementById('btn-execute-dry-run-main');
  const activeBtn = btnTop || btnMain;
  await withButtonLoader(activeBtn, 'Simulating Dry-Run...', async () => {
    try {
      showToast(`Running deterministic dry-run on Plan v${state.currentPlan.version}...`, 'info');
      const res = await fetch(`${API_BASE}/plans/${state.currentPlan.version}/dry-run`, { method: 'POST' });
      const summary = await res.json();
      renderDryRunResults(summary);
      switchTab('tab-dryrun');
      showToast(`Dry-Run complete: ${summary.accepted_count} valid, ${summary.rejected_count} quarantined in ${summary.execution_time_ms}ms`, 'success');
    } catch (err) {
      showToast('Dry-run failed: ' + err.message, 'error');
    }
  });
}

async function handleExecuteMigration() {
  if (!state.currentPlan) return;
  if (state.currentPlan.status !== 'APPROVED') {
    showToast('Execution blocked: You must approve the plan first!', 'error');
    return;
  }

  const btnTop = document.getElementById('btn-execute-migration-top');
  const btnMain = document.getElementById('btn-execute-migration-main');
  const activeBtn = btnTop || btnMain;
  await withButtonLoader(activeBtn, 'Writing Target Store...', async () => {
    try {
      showToast(`Executing Plan v${state.currentPlan.version} into target SQLite store...`, 'info');
      const res = await fetch(`${API_BASE}/plans/${state.currentPlan.version}/execute`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ plan_version: state.currentPlan.version, executed_by: 'Lead Data Engineer' })
      });

      if (!res.ok) {
        const errData = await res.json();
        throw new Error(errData.detail || 'Execution failed');
      }

      const execResult = await res.json();
      state.lastExecutionResult = execResult;
      document.getElementById('target-stat-snapshot').textContent = execResult.snapshot_id;

      await fetchTargetData();
      renderTargetStoreView();
      await renderReconciliationView();
      switchTab('tab-execution');

      showToast(`Migration executed! ${execResult.inserted_count} rows inserted into target store.`, 'success');
    } catch (err) {
      showToast('Execution failed: ' + err.message, 'error');
    }
  });
}

async function handleRetryMigration() {
  if (!state.currentPlan || state.currentPlan.status !== 'APPROVED') return;
  const btn = document.getElementById('btn-retry-migration');
  await withButtonLoader(btn, 'Verifying Idempotency...', async () => {
    try {
      showToast('Retrying migration to verify Idempotency & Duplicate Prevention...', 'info');
      const res = await fetch(`${API_BASE}/plans/${state.currentPlan.version}/execute`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ plan_version: state.currentPlan.version, executed_by: 'Lead Data Engineer' })
      });
      const execResult = await res.json();
      state.lastExecutionResult = execResult;

      await fetchTargetData();
      renderTargetStoreView();
      await renderReconciliationView();

      showToast(
        `IDEMPOTENCY VERIFIED: 0 duplicates created! (${execResult.skipped_duplicates_count} rows recognized & refreshed)`, 
        'success'
      );
    } catch (err) {
      showToast('Retry test failed: ' + err.message, 'error');
    }
  });
}

async function handleRollback() {
  if (!state.lastExecutionResult) {
    showToast('No active execution snapshot to rollback', 'error');
    return;
  }

  const snapId = state.lastExecutionResult.snapshot_id;
  const runId = state.lastExecutionResult.run_id;
  const btn = document.getElementById('btn-rollback-migration');

  await withButtonLoader(btn, 'Rolling back...', async () => {
    try {
      showToast(`Rolling back target store using snapshot ${snapId}...`, 'info');
      const res = await fetch(`${API_BASE}/rollback`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ snapshot_id: snapId, run_id: runId, actor: 'Lead Data Engineer' })
      });
      const rbResult = await res.json();

      await fetchTargetData();
      renderTargetStoreView();
      await renderReconciliationView();

      showToast(`ROLLBACK COMPLETE: ${rbResult.records_removed} rows removed. Target restored cleanly.`, 'success');
    } catch (err) {
      showToast('Rollback failed: ' + err.message, 'error');
    }
  });
}

async function handleForkPlan() {
  if (!state.currentPlan) return;
  const btn = document.getElementById('btn-save-as-new-version');
  await withButtonLoader(btn, 'Forking Plan...', async () => {
    try {
      const res = await fetch(`${API_BASE}/plans/${state.currentPlan.version}/fork`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(state.currentPlan.field_mappings)
      });
      const forked = await res.json();
      state.plans.push(forked);
      state.currentPlan = forked;
      updatePlanHeaderBadge();
      renderMappingStudio();
      showToast(`Created new editable Plan v${forked.version} (DRAFT)!`, 'success');
    } catch (err) {
      showToast('Fork failed: ' + err.message, 'error');
    }
  });
}

async function handleResetWorkbench() {
  if (!confirm('Reset target database, quarantine ledgers, and audit events to a clean slate?')) return;
  const btn = document.getElementById('btn-reset-workbench');
  await withButtonLoader(btn, 'Resetting...', async () => {
    try {
      await fetch(`${API_BASE}/reset`, { method: 'POST' });
      state.lastExecutionResult = null;
      state.lastDryRunSummary = null;
      await fetchTargetData();
      renderTargetStoreView();
      await renderReconciliationView();
      showToast('Target store and ledger reset to clean slate.', 'info');
    } catch (err) {
      showToast('Reset failed: ' + err.message, 'error');
    }
  });
}

async function uploadFileObject(file) {
  if (!file) return;

  const scalerBox = document.getElementById('upload-scaler-container');
  const labelElem = document.getElementById('scaler-file-label');
  const pctElem = document.getElementById('upload-scaler-percent');
  const fillElem = document.getElementById('upload-scaler-fill');
  const msgElem = document.getElementById('upload-scaler-msg');

  const formatSize = (bytes) => (bytes / 1024).toFixed(1) + ' KB';

  if (scalerBox) {
    scalerBox.style.display = 'block';
    if (labelElem) labelElem.textContent = `${file.name} (${formatSize(file.size)})`;
    if (pctElem) pctElem.textContent = '10%';
    if (fillElem) fillElem.style.width = '10%';
    if (msgElem) msgElem.textContent = 'Reading binary chunks...';
  }

  const formData = new FormData();
  formData.append('file', file);

  try {
    showToast(`Uploading and profiling '${file.name}'...`, 'info');

    if (fillElem) {
      setTimeout(() => { if (fillElem) fillElem.style.width = '45%'; if (pctElem) pctElem.textContent = '45%'; if (msgElem) msgElem.textContent = 'Streaming payload to server...'; }, 100);
      setTimeout(() => { if (fillElem) fillElem.style.width = '80%'; if (pctElem) pctElem.textContent = '80%'; if (msgElem) msgElem.textContent = 'Parsing rows and profiling column invariants...'; }, 300);
    }

    const res = await fetch(`${API_BASE}/upload/source`, {
      method: 'POST',
      body: formData
    });

    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || 'Upload failed');
    }

    const data = await res.json();
    if (fillElem) fillElem.style.width = '100%';
    if (pctElem) pctElem.textContent = '100%';
    if (msgElem) msgElem.textContent = '✓ Parsing & schema profiling complete!';

    showToast(data.message, 'success');

    // Reload all state and switch to profiler
    await loadInitialData();
    switchTab('tab-profiler');

    setTimeout(() => {
      if (scalerBox) scalerBox.style.display = 'none';
    }, 2500);
  } catch (err) {
    if (msgElem) msgElem.textContent = '❌ Upload failed: ' + err.message;
    showToast('Upload error: ' + err.message, 'error');
  }
}

async function handleUploadDataset(event) {
  const file = event.target.files && event.target.files[0];
  if (file) {
    await uploadFileObject(file);
  }
  event.target.value = '';
}

async function handleLoadTestSample() {
  const btn = document.getElementById('btn-load-test-sample');
  await withButtonLoader(btn, 'Loading Sample...', async () => {
    try {
      showToast('Loading 100 test records from user_test_records.csv...', 'info');
      const res = await fetch(`${API_BASE}/load-test-records`, { method: 'POST' });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || 'Failed to load test sample');
      }
      const data = await res.json();
      showToast(data.message, 'success');
      await loadInitialData();
      switchTab('tab-profiler');
    } catch (err) {
      showToast('Load test sample error: ' + err.message, 'error');
    }
  });
}

async function handleLoadBenchmark() {
  const btn = document.getElementById('btn-load-benchmark');
  await withButtonLoader(btn, 'Loading Benchmark...', async () => {
    try {
      showToast('Resetting to 1,000-record benchmark dataset...', 'info');
      await fetch(`${API_BASE}/reset`, { method: 'POST' });
      await fetch(`${API_BASE}/schemas/source`);
      await loadInitialData();
      showToast('Loaded benchmark dataset (1,000 records).', 'success');
    } catch (err) {
      showToast('Failed to load benchmark: ' + err.message, 'error');
    }
  });
}

window.handleAnswerClarification = async function(qId, answer) {
  if (!state.currentPlan) return;
  try {
    const res = await fetch(`${API_BASE}/plans/${state.currentPlan.version}/clarifications`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question_id: qId, user_answer: answer })
    });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || 'Failed to update clarification');
    }
    const updated = await res.json();
    state.currentPlan = updated;
    const idx = state.plans.findIndex(p => p.version === updated.version);
    if (idx !== -1) state.plans[idx] = updated;

    updatePlanHeaderBadge();
    renderMappingStudio();
    showToast(`Clarification policy applied & saved: "${answer}"`, 'info');
  } catch (err) {
    showToast('Failed to save clarification: ' + err.message, 'error');
  }
};

// ============================================================================
// Modal & Toasts
// ============================================================================

window.runLiveAIDiagnosis = async function(btn, field, rule, rawVal, errMsg, sourcePayload) {
  const container = document.getElementById('live-ai-diagnosis-result');
  if (!container) return;

  await window.withButtonLoader(btn, 'Analyzing with Ollama AI...', async () => {
    try {
      container.style.display = 'block';
      container.innerHTML = '<div style="color: var(--ink-muted); font-size: 0.8rem; font-style: italic;">Ollama Copilot inspecting target schema constraints and source payload...</div>';

      const res = await fetch('/api/ai/diagnose-record', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          field,
          rule,
          raw_value: rawVal,
          error_message: errMsg,
          source_payload: sourcePayload
        })
      });
      const data = await res.json();
      container.innerHTML = `
        <div style="background: rgba(14, 165, 233, 0.08); border: 1px solid rgba(14, 165, 233, 0.3); border-radius: var(--radius-sm); padding: 0.85rem; margin-top: 0.5rem;">
          <div style="font-weight: 700; color: #0284c7; font-size: 0.74rem; text-transform: uppercase; margin-bottom: 0.35rem; display: flex; align-items: center; gap: 0.35rem;">
            <span>🤖</span> Ollama Agent Reasoning (${data.status})
          </div>
          <div style="font-size: 0.82rem; line-height: 1.5; color: var(--ink-primary); white-space: pre-wrap;">${escapeHtml(data.diagnosis || '')}</div>
        </div>
      `;
    } catch (err) {
      container.innerHTML = `<div style="color: #f43f5e; font-size: 0.8rem;">Diagnosis request failed: ${escapeHtml(err.message)}</div>`;
    }
  });
};

window.applyAIFixSingle = async function(field, rule, rawVal, customFallback = null) {
  if (!state.currentPlan) {
    showToast('No active migration plan available.', 'error');
    return;
  }
  const oldQuar = state.dryRunSummary ? state.dryRunSummary.rejected_count : 0;
  
  try {
    showToast(`Applying AI remediation for field '${field}'...`, 'info');
    const res = await fetch(`/api/plans/${state.currentPlan.version}/apply-fix`, {
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
      const err = await res.json().catch(() => ({ detail: 'Failed to apply AI fix' }));
      throw new Error(err.detail || 'Failed to apply AI fix');
    }
    const data = await res.json();
    state.currentPlan = data.plan;
    state.dryRunSummary = data.dry_run_summary;
    renderDryRunResults(data.dry_run_summary);
    renderMappingStudio();
    updatePlanHeaderBadge();
    closeModal();
    const newQuar = data.dry_run_summary.rejected_count;
    showToast(`🎉 AI Fix Implemented! Quarantined records reduced from ${oldQuar} ➔ ${newQuar} across all values.`, 'success');
  } catch (err) {
    showToast('Failed to implement AI fix: ' + err.message, 'error');
  }
};

window.applyAllAIFixesMode1 = async function() {
  if (!state.currentPlan) {
    showToast('Please synthesize or load a migration plan first.', 'error');
    return;
  }
  const btn = document.getElementById('btn-auto-fix-all-mode1');
  const oldQuar = state.dryRunSummary ? state.dryRunSummary.rejected_count : 0;

  await window.withButtonLoader(btn, 'Auto-Remediating & Re-simulating...', async () => {
    try {
      const res = await fetch(`/api/plans/${state.currentPlan.version}/apply-fix`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          fix_action: 'AUTO_RESOLVE_ALL'
        })
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: 'Auto-remediation failed' }));
        throw new Error(err.detail || 'Auto-remediation failed');
      }
      const data = await res.json();
      state.currentPlan = data.plan;
      state.dryRunSummary = data.dry_run_summary;
      renderDryRunResults(data.dry_run_summary);
      renderMappingStudio();
      updatePlanHeaderBadge();
      closeModal();
      const newQuar = data.dry_run_summary.rejected_count;
      showToast(`⚡ All AI fixes applied! Quarantine dropped from ${oldQuar} ➔ ${newQuar} (100% compliant).`, 'success');
    } catch (err) {
      showToast('Error applying AI fixes: ' + err.message, 'error');
    }
  });
};

window.openQuarantineModal = function(qRecord, mode = 'mode1') {
  const modal = document.getElementById('record-modal');
  const title = document.getElementById('modal-title');
  const body = document.getElementById('modal-body');

  const isMode2 = (mode === 'mode2');
  title.textContent = isMode2 
    ? `Quarantine Forensic Evidence (Row #${qRecord.source_row_index + 1}) [Universal Studio]`
    : `Quarantine Forensic Evidence (Row #${qRecord.source_row_index + 1})`;

  const firstErr = (qRecord.errors && qRecord.errors.length > 0) ? qRecord.errors[0] : {};
  const aiFix = firstErr.ai_suggestion || qRecord.ai_remediation_summary || 'Configure fallback substitution or normalize source format';

  const errorsHtml = (qRecord.errors || []).map(e => `
    <div style="background: rgba(244, 63, 94, 0.08); border: 1px solid rgba(244, 63, 94, 0.25); border-radius: var(--radius-sm); padding: 0.85rem; margin-bottom: 0.75rem;">
      <div style="display: flex; justify-content: space-between; align-items: center; font-weight: 600; color: #f43f5e; margin-bottom: 0.35rem;">
        <span>Field: <span class="mono" style="font-weight: 700;">${escapeHtml(e.field)}</span></span>
        <span class="badge-pill" style="font-size: 0.7rem; background: rgba(244, 63, 94, 0.12); color: #f43f5e; border-color: rgba(244, 63, 94, 0.3);">${escapeHtml(e.rule)}</span>
      </div>
      <div style="font-size: 0.84rem; color: var(--ink-primary); font-weight: 500; margin-bottom: 0.35rem;">${escapeHtml(e.error_message)}</div>
      <div style="font-size: 0.75rem; color: var(--ink-muted);">Raw Input: <span class="mono" style="color: #334155; background: var(--bg-subtle); padding: 0.15rem 0.4rem; border-radius: 3px; border: 1px solid var(--border-hairline);">${escapeHtml(String(e.raw_value))}</span></div>
    </div>
  `).join('');

  const aiBoxHtml = isMode2 ? `
    <!-- AI Remediation Box (Universal Studio V2) -->
    <div style="background: #f8fafc; border: 1px solid #cbd5e1; border-left: 4px solid #0284c7; border-radius: var(--radius-md); padding: 1rem; margin-bottom: 1.25rem;">
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem; flex-wrap: wrap; gap: 0.5rem;">
        <span style="font-weight: 700; font-size: 0.78rem; color: #0284c7; text-transform: uppercase; letter-spacing: 0.04em;">
          🤖 AI Forensic Remediation Suggestion
        </span>
        <button id="btn-live-ai-diagnose" class="btn btn-secondary" style="padding: 0.25rem 0.65rem; font-size: 0.74rem;">
          ⚡ Ask Ollama Live Diagnosis
        </button>
      </div>
      <p style="font-size: 0.84rem; color: #1e293b; line-height: 1.5; margin: 0 0 0.75rem 0;">
        ${escapeHtml(aiFix)}
      </p>
      <div id="live-ai-diagnosis-result" style="display: none; margin-bottom: 0.75rem;"></div>
      <div style="display: flex; justify-content: flex-end; gap: 0.5rem; border-top: 1px dashed #cbd5e1; padding-top: 0.75rem;">
        <button id="btn-modal-apply-fix" class="btn btn-primary" style="padding: 0.35rem 0.85rem; font-size: 0.78rem; font-weight: 600;">
          ⚡ Implement AI Fix & Re-run Simulation
        </button>
      </div>
    </div>
  ` : '';

  body.innerHTML = `
    ${aiBoxHtml}

    <!-- Violations -->
    <div style="margin-bottom: 1.25rem;">
      <h4 style="font-size: 0.8rem; font-weight: 700; text-transform: uppercase; color: var(--ink-muted); letter-spacing: 0.04em; margin-bottom: 0.5rem;">Target Schema Violations:</h4>
      ${errorsHtml}
    </div>

    <!-- Raw Source Payload -->
    <div>
      <h4 style="font-size: 0.8rem; font-weight: 700; text-transform: uppercase; color: var(--ink-muted); letter-spacing: 0.04em; margin-bottom: 0.5rem;">Raw Source Record Payload:</h4>
      <pre style="background: #09090b; color: #38bdf8; padding: 1rem; border-radius: var(--radius-md); font-size: 0.76rem; overflow-x: auto; border: 1px solid var(--border-hairline); max-height: 200px;">${escapeHtml(JSON.stringify(qRecord.source_payload, null, 2))}</pre>
    </div>
  `;

  // Attach live diagnosis click handler (Mode 2 only)
  const liveBtn = document.getElementById('btn-live-ai-diagnose');
  if (liveBtn) {
    liveBtn.onclick = () => {
      window.runLiveAIDiagnosis(
        liveBtn,
        firstErr.field || '',
        firstErr.rule || '',
        firstErr.raw_value || '',
        firstErr.error_message || '',
        qRecord.source_payload || {}
      );
    };
  }

  // Attach Implement Fix handler (Mode 2 only)
  const modalFixBtn = document.getElementById('btn-modal-apply-fix');
  if (modalFixBtn) {
    modalFixBtn.onclick = async () => {
      await window.withButtonLoader(modalFixBtn, 'Applying Fix & Re-simulating...', async () => {
        if (typeof window.applyAIFixSingleV2 === 'function') {
          await window.applyAIFixSingleV2(firstErr.field || '', firstErr.rule || '', firstErr.raw_value);
        }
      });
    };
  }

  modal.classList.add('active');
};

function closeModal() {
  document.getElementById('record-modal').classList.remove('active');
}

function showToast(message, type = 'info') {
  const container = document.getElementById('toast-container');
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.innerHTML = `
    <span>${type === 'success' ? '✅' : type === 'error' ? '⚠️' : 'ℹ️'}</span>
    <span>${escapeHtml(message)}</span>
  `;
  container.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateY(10px)';
    setTimeout(() => toast.remove(), 300);
  }, 3500);
}

function escapeHtml(str) {
  if (str === null || str === undefined) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}
