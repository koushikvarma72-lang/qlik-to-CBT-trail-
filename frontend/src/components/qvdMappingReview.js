import { api } from '../api.js';
import { store } from '../store.js';
import { renderQvdStatusChecklist } from './qvdStatusChecklist.js';
import { escapeHtml } from '../utils.js';

const QVD_TARGET_TYPES = [
  'STRING',
  'BOOLEAN',
  'INT',
  'BIGINT',
  'DOUBLE',
  'DECIMAL(18,2)',
  'DATE',
  'TIMESTAMP',
];

const QVD_REVIEW_STATUSES = [
  'AUTO_APPROVED',
  'NEEDS_REVIEW',
  'MANUALLY_APPROVED',
];

let activeQvdMappingSection = 'overview';

function cloneQvdMapping(rows) {
  return JSON.parse(JSON.stringify(rows || []));
}

function qvdReadonlyValue(value) {
  if (Array.isArray(value)) return value.join(' ');
  if (value && typeof value === 'object') return JSON.stringify(value);
  return value || '';
}

function qvdSelectOptions(values, selected) {
  return values.map(value => `
    <option value="${escapeHtml(value)}" ${value === selected ? 'selected' : ''}>${escapeHtml(value)}</option>
  `).join('');
}

function renderQvdMappingErrors(errors) {
  const items = errors || [];
  if (!items.length) return '';
  return `
    <div class="qvd-validation-errors">
      ${items.slice(0, 8).map(err => `
        <div>${err.row !== undefined && err.row !== null ? `Row ${Number(err.row) + 1}: ` : ''}${escapeHtml(err.error || err)}</div>
      `).join('')}
      ${items.length > 8 ? `<div>${items.length - 8} more validation errors...</div>` : ''}
    </div>
  `;
}

function renderQvdMappingTable(rows) {
  return `
    <div class="qvd-mapping-table-wrapper">
      <table class="qvd-mapping-table" style="width:100%;border-collapse:collapse;font-size:12px;background:var(--bg-primary)">
        <thead>
          <tr style="background:var(--bg-surface);color:var(--text-primary)">
            <th style="text-align:left;padding:8px;border-bottom:1px solid var(--border)">Source Column</th>
            <th style="text-align:left;padding:8px;border-bottom:1px solid var(--border)">Category</th>
            <th style="text-align:left;padding:8px;border-bottom:1px solid var(--border)">Source Tags</th>
            <th style="text-align:left;padding:8px;border-bottom:1px solid var(--border)">Number Format</th>
            <th style="text-align:left;padding:8px;border-bottom:1px solid var(--border)">Target Table</th>
            <th style="text-align:left;padding:8px;border-bottom:1px solid var(--border)">Target Column</th>
            <th style="text-align:left;padding:8px;border-bottom:1px solid var(--border)">Target Type</th>
            <th style="text-align:left;padding:8px;border-bottom:1px solid var(--border)">Conversion Rule</th>
            <th style="text-align:left;padding:8px;border-bottom:1px solid var(--border)">Confidence</th>
            <th style="text-align:left;padding:8px;border-bottom:1px solid var(--border)">Review Status</th>
            <th style="text-align:left;padding:8px;border-bottom:1px solid var(--border)">Reason</th>
          </tr>
        </thead>
        <tbody>
          ${(rows || []).map((row, index) => `
            <tr class="${row.review_status === 'NEEDS_REVIEW' ? 'qvd-needs-review-row' : ''}">
              <td style="padding:8px;border-bottom:1px solid var(--border);font-weight:600;color:var(--text-primary)">${escapeHtml(row.source_column || '')}</td>
              <td style="padding:8px;border-bottom:1px solid var(--border)"><code>${escapeHtml(row.inferred_category || '')}</code></td>
              <td style="padding:8px;border-bottom:1px solid var(--border)"><code>${escapeHtml(qvdReadonlyValue(row.source_tags))}</code></td>
              <td style="padding:8px;border-bottom:1px solid var(--border)"><code>${escapeHtml(qvdReadonlyValue(row.source_number_format))}</code></td>
              <td style="padding:8px;border-bottom:1px solid var(--border)">
                <input class="qvd-mapping-input" data-qvd-map-index="${index}" data-qvd-map-field="target_table" value="${escapeHtml(row.target_table || '')}" />
              </td>
              <td style="padding:8px;border-bottom:1px solid var(--border)">
                <input class="qvd-mapping-input" data-qvd-map-index="${index}" data-qvd-map-field="target_column" value="${escapeHtml(row.target_column || '')}" />
              </td>
              <td style="padding:8px;border-bottom:1px solid var(--border)">
                <select class="qvd-mapping-select" data-qvd-map-index="${index}" data-qvd-map-field="target_type">
                  ${qvdSelectOptions(QVD_TARGET_TYPES, row.target_type)}
                </select>
              </td>
              <td style="padding:8px;border-bottom:1px solid var(--border)">
                <input class="qvd-mapping-input" data-qvd-map-index="${index}" data-qvd-map-field="conversion_rule" value="${escapeHtml(row.conversion_rule || '')}" />
              </td>
              <td style="padding:8px;border-bottom:1px solid var(--border)">${Number(row.confidence || 0).toFixed(2)}</td>
              <td style="padding:8px;border-bottom:1px solid var(--border)">
                <select class="qvd-mapping-select" data-qvd-map-index="${index}" data-qvd-map-field="review_status">
                  ${qvdSelectOptions(QVD_REVIEW_STATUSES, row.review_status)}
                </select>
              </td>
              <td style="padding:8px;border-bottom:1px solid var(--border);color:var(--text-dim)">${escapeHtml(row.reason || '')}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    </div>
  `;
}

function renderQvdDdlResult(result) {
  if (!result) return '';
  const preview = result.sql_preview || {};
  return `
    <section class="qvd-ddl-result">
      <div class="qvd-ddl-header">
        <div>
          <h3>Generated Databricks DDL</h3>
          <p>${result.table_count || 0} Delta table${(result.table_count || 0) === 1 ? '' : 's'} generated</p>
        </div>
        <span class="badge badge-success">DDL created</span>
      </div>
      <div class="qvd-ddl-files">
        ${(result.ddl_files || []).map(file => `
          <div class="qvd-ddl-file">
            <span>${escapeHtml(file)}</span>
          </div>
        `).join('')}
      </div>
      ${Object.entries(preview).map(([file, sql]) => `
        <div class="qvd-ddl-preview">
          <div class="qvd-ddl-preview-title">${escapeHtml(file)}</div>
          <pre class="inspect-code">${escapeHtml(sql)}</pre>
        </div>
      `).join('')}
    </section>
  `;
}

export function renderQvdMappingReview(state) {
  const suggestion = state.qvdSchemaSuggestion;
  if (!suggestion) {
    return `
      <div class="page">
        <div class="empty-state" style="margin:auto">
          <div class="empty-state-title">No Suggested Mapping Yet</div>
          <div class="empty-state-text">Upload and inspect QVD metadata, then generate the suggested Databricks structure.</div>
          <button class="btn btn-primary" id="qvd-review-upload-btn">Back To QVD Upload</button>
        </div>
      </div>
    `;
  }

  const rows = state.qvdEditableMapping?.length ? state.qvdEditableMapping : (suggestion.mapping || []);
  const approved = state.qvdApprovedMapping;
  const ddlResult = state.qvdDdlGeneration;
  const validationErrors = state.qvdMappingValidationErrors || [];
  const sections = [
    ['overview', 'Overview', 'Review suggested target tables and readiness.'],
    ['mapping', 'Mapping Table', 'Edit target table, column, type, and review status.'],
    ['ddl', 'Databricks DDL', 'Generate and inspect Delta table DDL.'],
  ];
  const active = sections.some(([id]) => id === activeQvdMappingSection) ? activeQvdMappingSection : 'overview';

  return `
    <div class="page qvd-review-page">
      <main class="qvd-review-main">
        ${renderQvdStatusChecklist(state)}
        <header class="inspect-header">
          <div>
            <div class="inspect-title">Manual Mapping Review</div>
            <div class="inspect-subtitle">Review and approve the Databricks target structure for this QVD flow.</div>
          </div>
          <div class="inspect-metrics">
            <div class="inspect-metric"><span>Total Columns</span><strong>${suggestion.total_columns || 0}</strong></div>
            <div class="inspect-metric"><span>Auto Approved</span><strong>${suggestion.auto_approved_count || 0}</strong></div>
            <div class="inspect-metric"><span>Needs Review</span><strong>${suggestion.needs_review_count || 0}</strong></div>
            <div class="inspect-metric"><span>Target Tables</span><strong>${(suggestion.target_tables || []).length}</strong></div>
          </div>
        </header>

        <section class="qvd-review-card">
          <div class="qvd-business-status-strip">
            <div class="qvd-business-status generated"><span>Suggested</span><strong>${suggestion.mapping?.length || rows.length} rows</strong></div>
            <div class="qvd-business-status ${approved ? 'generated' : 'pending'}"><span>Approved</span><strong>${approved ? 'Generated' : 'Not generated'}</strong></div>
            <div class="qvd-business-status ${ddlResult?.generated ? 'generated' : ddlResult ? 'failed' : 'pending'}"><span>DDL</span><strong>${ddlResult?.generated ? 'Generated' : ddlResult ? 'Failed' : 'Not generated'}</strong></div>
          </div>
          <div class="qvd-business-shell">
            <nav class="qvd-business-section-nav" aria-label="Inspect Mapping sections">
              ${sections.map(([id, label, helper]) => `
                <button type="button" class="qvd-business-section-link ${active === id ? 'active' : ''}" data-qvd-mapping-section="${id}">
                  <span>${escapeHtml(label)}</span>
                  <small>${escapeHtml(helper)}</small>
                </button>
              `).join('')}
            </nav>
            <div class="qvd-business-section-panel">
              ${renderQvdMappingSection(active, rows, suggestion, approved, ddlResult, validationErrors, state)}
            </div>
          </div>
        </section>
      </main>
    </div>
  `;
}

function renderQvdSectionHeader(title, helper, action = '') {
  return `
    <div class="qvd-business-section-header">
      <div>
        <h3>${escapeHtml(title)}</h3>
        <p>${escapeHtml(helper)}</p>
      </div>
      ${action}
    </div>
  `;
}

function renderQvdMappingSection(active, rows, suggestion, approved, ddlResult, validationErrors, state) {
  if (active === 'mapping') {
    return `
      ${renderQvdSectionHeader('Mapping Table', 'Edit and approve the Databricks target structure.', `
        <div style="display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap">
          <button class="btn btn-outline" id="qvd-reset-mapping-btn">Reset To Suggested Mapping</button>
          <button class="btn btn-primary" id="qvd-save-mapping-btn" ${state.isSavingQvdMapping ? 'disabled' : ''}>
            ${state.isSavingQvdMapping ? 'Saving...' : 'Save Approved Mapping'}
          </button>
        </div>
      `)}
      ${renderQvdMappingErrors(validationErrors)}
      ${renderQvdMappingTable(rows)}
    `;
  }
  if (active === 'ddl') {
    return `
      ${renderQvdSectionHeader('Databricks DDL', 'Generate and inspect Delta table DDL after approving the mapping.', `
        <button class="btn btn-secondary" id="qvd-generate-ddl-btn" ${!approved || state.isGeneratingQvdDdl ? 'disabled' : ''}>
          ${state.isGeneratingQvdDdl ? 'Generating...' : 'Generate Databricks DDL'}
        </button>
      `)}
      ${ddlResult ? renderQvdDdlResult(ddlResult) : `<div class="inspect-empty">Save the approved mapping, then generate Databricks DDL.</div>`}
      ${ddlResult?.generated ? `
        <div style="display:flex;justify-content:flex-end;margin-top:14px">
          <button class="btn btn-primary" id="qvd-continue-next-btn">Continue To Next Step</button>
        </div>
      ` : ''}
    `;
  }
  return `
    ${renderQvdSectionHeader('Overview', 'Review suggested target tables and mapping readiness.')}
    <div class="qvd-profile-summary">
      <div class="qvd-profile-card"><span>Total Columns</span><strong>${suggestion.total_columns || 0}</strong></div>
      <div class="qvd-profile-card"><span>Auto Approved</span><strong>${suggestion.auto_approved_count || 0}</strong></div>
      <div class="qvd-profile-card"><span>Needs Review</span><strong>${suggestion.needs_review_count || 0}</strong></div>
      <div class="qvd-profile-card"><span>Target Tables</span><strong>${(suggestion.target_tables || []).length}</strong></div>
    </div>
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
      ${suggestion.artifacts?.suggested_mapping_csv ? `<span class="badge badge-primary">suggested_databricks_mapping.csv created</span>` : ''}
      ${approved?.approved_mapping_csv ? `<span class="badge badge-success">approved_databricks_mapping.csv created</span>` : ''}
    </div>
    <div class="qvd-parquet-messages" style="margin-top:10px">
      <strong>Recommended next action</strong>
      <div>${approved ? 'Generate Databricks DDL from the approved mapping.' : 'Open Mapping Table and save the approved mapping.'}</div>
    </div>
  `;
}

export function setupQvdMappingReview() {
  document.querySelectorAll('[data-qvd-mapping-section]').forEach(button => {
    button.addEventListener('click', () => {
      activeQvdMappingSection = button.dataset.qvdMappingSection || 'overview';
      const page = document.getElementById('page-content');
      if (page) page.innerHTML = renderQvdMappingReview(store.get());
      setupQvdMappingReview();
    });
  });
  document.getElementById('qvd-review-upload-btn')?.addEventListener('click', () => store.navigate('upload'));
  document.getElementById('qvd-save-mapping-btn')?.addEventListener('click', handleQvdSaveApprovedMapping);
  document.getElementById('qvd-generate-ddl-btn')?.addEventListener('click', handleQvdGenerateDdl);
  document.getElementById('qvd-reset-mapping-btn')?.addEventListener('click', handleQvdResetMapping);
  document.getElementById('qvd-continue-next-btn')?.addEventListener('click', () => store.navigate('output'));
  document.querySelectorAll('[data-qvd-map-index][data-qvd-map-field]').forEach(input => {
    input.addEventListener('change', handleQvdMappingChange);
  });
}

function handleQvdMappingChange(event) {
  const index = Number(event.target.dataset.qvdMapIndex);
  const field = event.target.dataset.qvdMapField;
  if (!Number.isInteger(index) || !field) return;

  const rows = cloneQvdMapping(store.get().qvdEditableMapping || []);
  if (!rows[index]) return;
  rows[index][field] = event.target.value;
  store.set({
    qvdEditableMapping: rows,
    qvdMappingValidationErrors: validateQvdMappingRows(rows, false),
  });
}

function validateQvdMappingRows(rows, requireApproved = true) {
  const errors = [];
  const seen = new Map();

  (rows || []).forEach((row, index) => {
    const targetTable = String(row.target_table || '').trim();
    const targetColumn = String(row.target_column || '').trim();
    const targetType = String(row.target_type || '').trim();
    const reviewStatus = String(row.review_status || '').trim();

    if (!targetTable) errors.push({ row: index, field: 'target_table', error: 'target_table cannot be empty.' });
    if (!targetColumn) errors.push({ row: index, field: 'target_column', error: 'target_column cannot be empty.' });
    if (!QVD_TARGET_TYPES.includes(targetType)) {
      errors.push({ row: index, field: 'target_type', error: `${targetType || '(empty)'} is not an allowed target type.` });
    }
    if (requireApproved && !['AUTO_APPROVED', 'MANUALLY_APPROVED'].includes(reviewStatus)) {
      errors.push({ row: index, field: 'review_status', error: 'Row must be AUTO_APPROVED or MANUALLY_APPROVED before saving.' });
    }

    if (targetTable && targetColumn) {
      const key = `${targetTable.toLowerCase()}::${targetColumn.toLowerCase()}`;
      if (seen.has(key)) {
        errors.push({ row: index, field: 'target_column', error: `Duplicate target column '${targetColumn}' in target table '${targetTable}'.` });
      } else {
        seen.set(key, index);
      }
    }
  });

  return errors;
}

async function handleQvdSaveApprovedMapping() {
  const state = store.get();
  const sessionId = state.qvdInspection?.session_id || state.sessionId;
  const rows = state.qvdEditableMapping || [];
  const errors = validateQvdMappingRows(rows, true);
  if (errors.length) {
    store.set({ qvdMappingValidationErrors: errors });
    return;
  }

  store.set({ isSavingQvdMapping: true, qvdMappingValidationErrors: [] });
  try {
    const result = await api.saveApprovedQvdMapping(sessionId, rows);
    store.set({
      qvdApprovedMapping: result,
      qvdDdlGeneration: null,
      isSavingQvdMapping: false,
      qvdMappingValidationErrors: [],
    });
  } catch (err) {
    store.set({
      isSavingQvdMapping: false,
      qvdMappingValidationErrors: err.errors || [{ error: err.message || 'Approved mapping save failed' }],
    });
    alert(err.message || 'Approved mapping save failed');
  }
}

async function handleQvdGenerateDdl() {
  const state = store.get();
  const sessionId = state.qvdInspection?.session_id || state.sessionId;
  if (!state.qvdApprovedMapping) return;

  store.set({ isGeneratingQvdDdl: true, qvdMappingValidationErrors: [] });
  try {
    const result = await api.generateQvdDdl(sessionId);
    store.set({
      qvdDdlGeneration: result,
      isGeneratingQvdDdl: false,
    });
  } catch (err) {
    store.set({
      isGeneratingQvdDdl: false,
      qvdMappingValidationErrors: err.errors || [{ error: err.message || 'QVD DDL generation failed' }],
    });
    alert(err.message || 'QVD DDL generation failed');
  }
}

function handleQvdResetMapping() {
  const suggestion = store.get().qvdSchemaSuggestion;
  store.set({
    qvdEditableMapping: cloneQvdMapping(suggestion?.mapping || []),
    qvdApprovedMapping: null,
    qvdDdlGeneration: null,
    qvdMappingValidationErrors: [],
  });
}
