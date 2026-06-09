/**
 * QVF Decoder — Page 1: Graph-Based Dependency Upload
 */
import { store } from '../store.js';
import { api, apiDownloadUrl } from '../api.js';
import { GraphComponent } from '../components/graph.js';
import { renderQvdStatusChecklist } from '../components/qvdStatusChecklist.js';
import { escapeHtml } from '../utils.js';

let graphComponent = null;

function getSessionTotals(state) {
  const stats = state.sessionStats || {};
  const totalTables = Number.isFinite(stats.totalTables)
    ? stats.totalTables
    : (stats.files || []).reduce((sum, file) => sum + (file.tables || []).length, 0);
  const totalRelationships = Number.isFinite(stats.totalRelationships)
    ? stats.totalRelationships
    : (state.associations || []).length;
  return { totalTables, totalRelationships };
}

export function renderUploadPage(container) {
  const state = store.get();
  const sessionTotals = getSessionTotals(state);
  const isQvdMode = state.uploadMode === 'qvd';

  container.innerHTML = `
    <div class="page" id="upload-page">
      <!-- Sidebar -->
      <div class="sidebar animate-slide-left">
        <div class="sidebar-header" style="padding-bottom: 0;">
          <!-- Header removed to avoid duplication with Navbar -->
        </div>

        <div class="sidebar-section">
          <div class="sidebar-section-title">Migration Mode</div>
          <div class="toggle-group" style="width:100%;display:grid;grid-template-columns:1fr;gap:6px;background:transparent;padding:0">
            <button class="toggle-btn ${!isQvdMode ? 'active' : ''}" id="mode-qvf" style="justify-content:center;padding:8px 10px;border:1px solid var(--border)">
              QVF / Qlik App Migration
            </button>
            <button class="toggle-btn ${isQvdMode ? 'active' : ''}" id="mode-qvd" style="justify-content:center;padding:8px 10px;border:1px solid var(--border)">
              QVD → Databricks Migration
            </button>
          </div>
        </div>

        ${isQvdMode ? renderQvdUploadControls(state) : renderQvfUploadControls()}

        <div class="sidebar-content" id="sidebar-files">
          ${isQvdMode ? renderQvdSidebar(state) : (state.filename ? renderFileInfo(state) : renderEmptyFiles())}
        </div>

        <div class="sidebar-section" style="border-top:1px solid var(--border);border-bottom:none;margin-top:auto;padding-bottom:16px">
          <button class="btn btn-outline btn-block" id="reset-session-btn" style="color:var(--text-dim);border-color:var(--border);width:100%;font-size:12px">
            🔄 Reset All Data
          </button>
        </div>
      </div>

      <!-- Main Graph Area -->
      <div class="${isQvdMode ? 'upload-content upload-content-qvd' : 'upload-content'}">
        <div class="${isQvdMode ? 'upload-main-area qvd-inspection-area' : 'upload-main-area'}" id="graph-area">
          ${isQvdMode ? renderQvdInspectionResults(state) : (state.graph.nodes.length === 0 ? renderEmptyGraph() : '')}
        </div>

        <!-- Bottom Bar -->
        <div class="review-footer">
          <div style="display:flex;align-items:center;gap:12px">
            ${!isQvdMode && state.filename ? `
              <span class="badge badge-success">✅ ${sessionTotals.totalTables} ${ sessionTotals.totalTables === 1 ? 'table' : 'tables' }</span>
              <span class="badge badge-info">🔗 ${sessionTotals.totalRelationships} ${ sessionTotals.totalRelationships === 1 ? 'relationship' : 'relationships' }</span>
            ` : ''}
            ${isQvdMode && state.qvdInspection ? `
              <span class="badge badge-success">${state.qvdInspection.tables.length} ${state.qvdInspection.tables.length === 1 ? 'table' : 'tables'} inspected</span>
              <span class="badge badge-info">${state.qvdInspection.uploaded_files.length} ${state.qvdInspection.uploaded_files.length === 1 ? 'file' : 'files'}</span>
            ` : ''}
          </div>
          <div style="display:flex;gap:8px">
            <button class="btn btn-secondary btn-lg" id="inspect-btn" ${isQvdMode ? (!state.qvdInspection ? 'disabled' : '') : (!state.filename ? 'disabled' : '')}>
              ${isQvdMode ? 'Inspect Mapping' : 'Inspect Data'}
            </button>
            <button class="btn btn-primary btn-lg" id="review-btn" ${isQvdMode || !state.filename ? 'disabled' : ''}>
            Review Data Model →
            </button>
          </div>
        </div>
      </div>
    </div>
  `;

  // Setup event listeners
  setupModeSelector();
  setupUploadHandlers();
  if (!isQvdMode) setupGraphIfData();
  setupUploadOverlay();
  setupInspectButton();
  setupReviewButton();
  setupResetButton();
  setupQvdHandlers();
}

function renderQvfUploadControls() {
  return `
    <div class="sidebar-section">
      <div class="sidebar-section-title">Upload QVF File</div>
      <div class="upload-zone" id="upload-zone">
        <div class="upload-zone-icon">📁</div>
        <div class="upload-zone-text">Drop .qvf or .zip file here</div>
        <div class="upload-zone-hint">or click to browse</div>
        <input type="file" id="file-input" accept=".qvf,.zip" />
      </div>
    </div>
  `;
}

function renderQvdUploadControls(state) {
  const selected = state.qvdSelectedFiles || [];
  return `
    <div class="sidebar-section">
      <div class="sidebar-section-title">Upload QVD Files</div>
      <div class="upload-zone" id="qvd-upload-zone">
        <div class="upload-zone-icon">📁</div>
        <div class="upload-zone-text">Drop .qvd files here</div>
        <div class="upload-zone-hint">or click to browse multiple files</div>
        <input type="file" id="qvd-file-input" accept=".qvd" multiple />
      </div>
      <button class="btn btn-primary btn-block" id="qvd-inspect-btn" style="width:100%;margin-top:12px" ${selected.length ? '' : 'disabled'}>
        Upload & Inspect QVD Metadata
      </button>
      ${selected.length ? `
        <div style="margin-top:10px;font-size:11px;color:var(--text-dim)">
          ${selected.length} ${selected.length === 1 ? 'file selected' : 'files selected'}
        </div>
      ` : ''}
    </div>
  `;
}

function renderFileInfo(state) {
  const stats = state.sessionStats || { totalTables: (state.tables || []).length, totalRelationships: (state.associations || []).length, files: [] };
  const files = stats.files || [];
  
  const pluralize = (count, singular, plural) => count === 1 ? singular : plural;

  return `
    <div class="sidebar-section-title">Session Summary</div>
    <div class="file-info-card" style="margin-bottom: 24px; border-left: 3px solid var(--primary)">
      <div class="file-info-icon" style="color:var(--primary)">📊</div>
      <div class="file-info-details">
        <div class="file-info-name">Total Model Size</div>
        <div class="file-info-meta">
          ${stats.totalTables} ${pluralize(stats.totalTables, 'table', 'tables')} · 
          ${stats.totalRelationships} ${pluralize(stats.totalRelationships, 'relationship', 'relationships')}
        </div>
      </div>
    </div>

    <div class="sidebar-section-title">Uploaded Files (${files.length})</div>
    <div style="display:flex;flex-direction:column;gap:8px">
      ${files.length > 0 ? files.map(f => `
        <div class="file-info-card" style="padding: 10px; background: var(--bg-surface); border: 1px solid var(--border)">
          <div class="file-info-icon" style="font-size:14px">📄</div>
          <div class="file-info-details">
            <div class="file-info-name" style="font-size:12px">${f.filename}</div>
            <div class="file-info-meta" style="font-size:10px">
              ${(f.tables || []).length} ${pluralize((f.tables || []).length, 'table', 'tables')}
            </div>
          </div>
        </div>
      `).join('') : `
        <div class="file-info-card" style="padding: 10px; background: var(--bg-surface); border: 1px solid var(--border)">
          <div class="file-info-icon" style="font-size:14px">📄</div>
          <div class="file-info-details">
            <div class="file-info-name" style="font-size:12px">${state.filename}</div>
            <div class="file-info-meta" style="font-size:10px">
              ${(state.tables || []).length} ${pluralize((state.tables || []).length, 'table', 'tables')}
            </div>
          </div>
        </div>
      `}
    </div>
  `;
}

function renderEmptyFiles() {
  return `
    <div class="sidebar-section-title">Files</div>
    <div style="text-align:center;padding:24px 0;color:var(--text-dim);font-size:12px">
      No files uploaded yet
    </div>
  `;
}

function formatBytes(bytes) {
  const value = Number(bytes || 0);
  if (!value) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function renderQvdSidebar(state) {
  const inspection = state.qvdInspection;
  if (!inspection) {
    return `
      <div class="sidebar-section-title">QVD Files</div>
      <div style="text-align:center;padding:24px 0;color:var(--text-dim);font-size:12px">
        No QVD metadata inspected yet
      </div>
    `;
  }

  return `
    <div class="sidebar-section-title">Session</div>
    <div class="file-info-card" style="padding:10px;background:var(--bg-surface);border:1px solid var(--border)">
      <div class="file-info-icon" style="font-size:14px">🧾</div>
      <div class="file-info-details">
        <div class="file-info-name" style="font-size:12px">Session ID</div>
        <div class="file-info-meta" style="font-size:10px;word-break:break-all">${escapeHtml(inspection.session_id)}</div>
      </div>
    </div>
    <div class="sidebar-section-title">Uploaded QVDs (${inspection.uploaded_files.length})</div>
    <div style="display:flex;flex-direction:column;gap:8px">
      ${inspection.uploaded_files.map(file => `
        <div class="file-info-card" style="padding:10px;background:var(--bg-surface);border:1px solid var(--border)">
          <div class="file-info-icon" style="font-size:14px">📄</div>
          <div class="file-info-details">
            <div class="file-info-name" style="font-size:12px">${escapeHtml(file.file_name)}</div>
            <div class="file-info-meta" style="font-size:10px">${formatBytes(file.file_size_bytes)}</div>
          </div>
        </div>
      `).join('')}
    </div>
  `;
}

function renderAnalysisGroup(label, values) {
  const items = values || [];
  return `
    <div style="border:1px solid var(--border);background:var(--bg-surface);padding:8px;border-radius:6px;min-height:58px">
      <div style="font-size:10px;text-transform:uppercase;color:var(--text-dim);font-weight:700;margin-bottom:6px">${label}</div>
      <div style="display:flex;flex-wrap:wrap;gap:4px">
        ${items.length
          ? items.slice(0, 12).map(value => `<span class="badge badge-info" style="font-size:10px">${escapeHtml(value)}</span>`).join('')
          : `<span style="font-size:11px;color:var(--text-dim)">None detected</span>`}
      </div>
    </div>
  `;
}

function renderQvdFieldTable(fields) {
  return `
    <div class="qvd-fields-table-wrapper">
      <table class="qvd-fields-table" style="width:100%;border-collapse:collapse;font-size:12px;background:var(--bg-primary)">
        <thead>
          <tr style="background:var(--bg-surface);color:var(--text-primary)">
            <th style="text-align:left;padding:8px;border-bottom:1px solid var(--border)">#</th>
            <th style="text-align:left;padding:8px;border-bottom:1px solid var(--border)">Field Name</th>
            <th style="text-align:left;padding:8px;border-bottom:1px solid var(--border)">Tags</th>
            <th style="text-align:left;padding:8px;border-bottom:1px solid var(--border)">Number Format</th>
            <th style="text-align:left;padding:8px;border-bottom:1px solid var(--border)">No. Symbols</th>
            <th style="text-align:left;padding:8px;border-bottom:1px solid var(--border)">Bit Offset</th>
            <th style="text-align:left;padding:8px;border-bottom:1px solid var(--border)">Bit Width</th>
          </tr>
        </thead>
        <tbody>
          ${(fields || []).map(field => `
            <tr>
              <td style="padding:8px;border-bottom:1px solid var(--border);color:var(--text-dim)">${field.position}</td>
              <td style="padding:8px;border-bottom:1px solid var(--border);font-weight:600;color:var(--text-primary)">${escapeHtml(field.field_name)}</td>
              <td style="padding:8px;border-bottom:1px solid var(--border)">${(field.tags || []).map(tag => `<code>${escapeHtml(tag)}</code>`).join(' ')}</td>
              <td style="padding:8px;border-bottom:1px solid var(--border)"><code>${escapeHtml(JSON.stringify(field.number_format || {}))}</code></td>
              <td style="padding:8px;border-bottom:1px solid var(--border)">${escapeHtml(field.no_of_symbols || '')}</td>
              <td style="padding:8px;border-bottom:1px solid var(--border)">${escapeHtml(field.bit_offset || '')}</td>
              <td style="padding:8px;border-bottom:1px solid var(--border)">${escapeHtml(field.bit_width || '')}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    </div>
  `;
}

export function renderQvdRowPreview(fileName, state) {
  const preview = state.qvdRowPreviews?.[fileName];
  const loading = state.qvdPreviewLoadingByFile?.[fileName];
  if (loading) {
    return `
      <div class="qvd-row-preview">
        <div class="spinner"></div>
        <span style="font-size:12px;color:var(--text-dim)">Reading first 100 rows...</span>
      </div>
    `;
  }
  if (!preview) return '';
  if (!preview.success) {
    return `
      <div class="qvd-row-preview qvd-row-preview-error">
        <strong>Row preview unavailable</strong>
        <div>${escapeHtml(preview.error || 'No compatible QVD row reader is installed.')}</div>
      </div>
    `;
  }

  const columns = preview.columns || [];
  const rows = preview.rows || [];
  return `
    <div class="qvd-row-preview">
      <div class="qvd-row-preview-header">
        <div>
          <strong>Row Preview</strong>
          <span>${preview.row_count_returned || 0} of ${preview.limit || 100} rows returned</span>
        </div>
        <span class="badge badge-info">Reader: ${escapeHtml(preview.reader_used || 'unknown')}</span>
      </div>
      <div class="qvd-row-preview-table-wrapper">
        <table class="qvd-row-preview-table">
          <thead>
            <tr>
              <th>#</th>
              ${columns.map(column => `<th>${escapeHtml(column)}</th>`).join('')}
            </tr>
          </thead>
          <tbody>
            ${rows.map((row, index) => `
              <tr>
                <td>${index + 1}</td>
                ${columns.map(column => `<td>${escapeHtml(row?.[column] ?? '')}</td>`).join('')}
              </tr>
            `).join('')}
          </tbody>
        </table>
      </div>
    </div>
  `;
}

export function renderQvdColumnProfile(fileName, state) {
  const profile = state.qvdColumnProfiles?.[fileName];
  const loading = state.qvdProfileLoadingByFile?.[fileName];
  if (loading) {
    return `
      <div class="qvd-column-profile">
        <div class="spinner"></div>
        <span style="font-size:12px;color:var(--text-dim)">Profiling columns...</span>
      </div>
    `;
  }
  if (!profile) return '';
  if (!profile.success) {
    return `
      <div class="qvd-column-profile qvd-row-preview-error">
        <strong>Column profiling unavailable</strong>
        <div>${escapeHtml(profile.error || 'QVD column profiling failed.')}</div>
      </div>
    `;
  }

  return `
    <div class="qvd-column-profile">
      <div class="qvd-profile-summary">
        <div class="qvd-profile-card"><span>Rows Checked</span><strong>${profile.rows_checked || 0}</strong></div>
        <div class="qvd-profile-card"><span>Total Columns</span><strong>${profile.total_columns || 0}</strong></div>
        <div class="qvd-profile-card"><span>Matches</span><strong>${profile.match_count || 0}</strong></div>
        <div class="qvd-profile-card"><span>Needs Review</span><strong>${profile.needs_review_count || 0}</strong></div>
        <div class="qvd-profile-card"><span>Mismatches</span><strong>${profile.mismatch_count || 0}</strong></div>
      </div>
      <div class="qvd-profile-table-wrapper">
        <table class="qvd-profile-table">
          <thead>
            <tr>
              <th>Column Name</th>
              <th>Detected Type</th>
              <th>Approved Type</th>
              <th>Nulls</th>
              <th>Distinct</th>
              <th>Samples</th>
              <th>Min</th>
              <th>Max</th>
              <th>Status</th>
              <th>Warning</th>
            </tr>
          </thead>
          <tbody>
            ${(profile.profile_rows || []).map(row => `
              <tr class="${row.type_match_status === 'MISMATCH' ? 'qvd-profile-mismatch' : row.type_match_status === 'NEEDS_REVIEW' ? 'qvd-profile-review' : ''}">
                <td>${escapeHtml(row.column_name || '')}</td>
                <td><code>${escapeHtml(row.detected_runtime_type || '')}</code></td>
                <td><code>${escapeHtml(row.approved_target_type || '')}</code></td>
                <td>${escapeHtml(row.null_count ?? '')}</td>
                <td>${escapeHtml(row.distinct_count ?? '')}</td>
                <td>${escapeHtml((row.sample_values || []).join(', '))}</td>
                <td>${escapeHtml(row.min_value ?? '')}</td>
                <td>${escapeHtml(row.max_value ?? '')}</td>
                <td><span class="badge ${row.type_match_status === 'MISMATCH' ? 'badge-error' : row.type_match_status === 'NEEDS_REVIEW' ? 'badge-warning' : 'badge-success'}">${escapeHtml(row.type_match_status || '')}</span></td>
                <td>${escapeHtml(row.warning_reason || '')}</td>
              </tr>
            `).join('')}
          </tbody>
        </table>
      </div>
    </div>
  `;
}

export function renderQvdParquetConversion(fileName, state) {
  const conversion = state.qvdParquetConversions?.[fileName];
  const loading = state.qvdParquetLoadingByFile?.[fileName];
  if (loading) {
    return `
      <div class="qvd-parquet-result">
        <div class="spinner"></div>
        <span style="font-size:12px;color:var(--text-dim)">Converting QVD to Parquet...</span>
      </div>
    `;
  }
  if (!conversion) return '';
  const warnings = conversion.conversion_warnings || [];
  const errors = conversion.errors || [];
  return `
    <div class="qvd-parquet-result ${conversion.success ? '' : 'qvd-row-preview-error'}">
      <div class="qvd-parquet-header">
        <div>
          <strong>${conversion.success ? 'Parquet Conversion Complete' : 'Parquet Conversion Failed'}</strong>
          <span>${escapeHtml(conversion.target_table || '')}</span>
        </div>
        <span class="badge ${conversion.success ? 'badge-success' : 'badge-error'}">${conversion.success ? 'Success' : 'Failed'}</span>
      </div>
      <div class="qvd-profile-summary">
        <div class="qvd-profile-card"><span>Rows</span><strong>${conversion.row_count || 0}</strong></div>
        <div class="qvd-profile-card"><span>Columns</span><strong>${conversion.column_count || 0}</strong></div>
      </div>
      ${conversion.parquet_path ? `<div class="qvd-parquet-path">${escapeHtml(conversion.parquet_path)}</div>` : ''}
      ${warnings.length ? `
        <div class="qvd-parquet-messages">
          <strong>Warnings</strong>
          ${warnings.map(warning => `<div>${escapeHtml(warning)}</div>`).join('')}
        </div>
      ` : ''}
      ${errors.length ? `
        <div class="qvd-parquet-messages">
          <strong>Errors</strong>
          ${errors.map(error => `<div>${escapeHtml(error)}</div>`).join('')}
        </div>
      ` : ''}
      ${conversion.success ? `
        <div style="display:flex;justify-content:flex-end;margin-top:10px">
          <button class="btn btn-secondary qvd-preview-btn" data-qvd-validate-parquet-file="${escapeHtml(fileName)}" ${state.qvdParquetValidationLoadingByFile?.[fileName] ? 'disabled' : ''}>
            ${state.qvdParquetValidationLoadingByFile?.[fileName] ? 'Validating...' : 'Validate Parquet'}
          </button>
        </div>
      ` : ''}
      ${renderQvdParquetValidation(fileName, state)}
    </div>
  `;
}

function checkByName(validation, name) {
  return (validation?.checks || []).find(check => check.name === name) || {};
}

function renderQvdParquetValidation(fileName, state) {
  const loading = state.qvdParquetValidationLoadingByFile?.[fileName];
  const validation = state.qvdParquetValidations?.[fileName];
  if (loading) {
    return `
      <div class="qvd-parquet-validation">
        <div class="spinner"></div>
        <span style="font-size:12px;color:var(--text-dim)">Validating Parquet output...</span>
      </div>
    `;
  }
  if (!validation) return '';

  const rowCheck = checkByName(validation, 'row_count');
  const columnCheck = checkByName(validation, 'column_count');
  const missingColumns = checkByName(validation, 'approved_target_columns_exist')?.details?.missing_columns || [];
  const dateCheck = checkByName(validation, 'date_conversion');
  const booleanCheck = checkByName(validation, 'boolean_values');
  const numericCheck = checkByName(validation, 'numeric_values');
  const nullWarnings = (validation.null_percentages || []).filter(row => row.warning);

  return `
    <div class="qvd-parquet-validation ${validation.success ? '' : 'qvd-row-preview-error'}">
      <div class="qvd-parquet-header">
        <div>
          <strong>Parquet Validation</strong>
          <span>${validation.success ? 'All validation checks passed' : 'One or more validation checks failed'}</span>
        </div>
        <span class="badge ${validation.success ? 'badge-success' : 'badge-error'}">${validation.success ? 'PASS' : 'FAIL'}</span>
      </div>
      <div class="qvd-profile-summary">
        <div class="qvd-profile-card"><span>Row Count</span><strong>${rowCheck.passed ? 'OK' : 'FAIL'}</strong></div>
        <div class="qvd-profile-card"><span>Column Count</span><strong>${columnCheck.passed ? 'OK' : 'FAIL'}</strong></div>
        <div class="qvd-profile-card"><span>Date Check</span><strong>${dateCheck.passed ? 'OK' : 'FAIL'}</strong></div>
        <div class="qvd-profile-card"><span>Boolean Check</span><strong>${booleanCheck.passed ? 'OK' : 'FAIL'}</strong></div>
        <div class="qvd-profile-card"><span>Numeric Check</span><strong>${numericCheck.passed ? 'OK' : 'FAIL'}</strong></div>
      </div>
      ${missingColumns.length ? `
        <div class="qvd-parquet-messages">
          <strong>Missing Columns</strong>
          ${missingColumns.map(column => `<div>${escapeHtml(column)}</div>`).join('')}
        </div>
      ` : ''}
      ${nullWarnings.length ? `
        <div class="qvd-parquet-messages">
          <strong>Null Percentage Warnings</strong>
          ${nullWarnings.map(row => `<div>${escapeHtml(row.column_name)}: ${escapeHtml(row.null_percentage)}%</div>`).join('')}
        </div>
      ` : ''}
      ${(validation.errors || []).length ? `
        <div class="qvd-parquet-messages">
          <strong>Errors</strong>
          ${validation.errors.map(error => `<div>${escapeHtml(error)}</div>`).join('')}
        </div>
      ` : ''}
      ${validation.success ? `
        <div style="display:flex;justify-content:flex-end;margin-top:10px">
          <button class="btn btn-primary qvd-preview-btn" data-qvd-load-scripts-file="${escapeHtml(fileName)}" ${state.qvdDatabricksLoadLoadingByFile?.[fileName] ? 'disabled' : ''}>
            ${state.qvdDatabricksLoadLoadingByFile?.[fileName] ? 'Generating...' : 'Generate Databricks Load Scripts'}
          </button>
        </div>
      ` : ''}
      ${renderQvdDatabricksLoadScripts(fileName, state)}
    </div>
  `;
}

export function renderQvdDatabricksLoadScripts(fileName, state) {
  const loading = state.qvdDatabricksLoadLoadingByFile?.[fileName];
  const result = state.qvdDatabricksLoadScripts?.[fileName];
  if (loading) {
    return `
      <div class="qvd-load-result">
        <div class="spinner"></div>
        <span style="font-size:12px;color:var(--text-dim)">Generating Databricks load scripts...</span>
      </div>
    `;
  }
  if (!result) return '';

  const artifacts = result.artifacts || result.artifact_downloads || {};
  const artifactRows = Object.entries(artifacts).filter(([, value]) => value);
  const artifactLabel = value => {
    if (value && typeof value === 'object') return value.relative_path || value.file_name || '';
    return value || '';
  };
  return `
    <div class="qvd-load-result ${result.generated ? '' : 'qvd-row-preview-error'}">
      <div class="qvd-parquet-header">
        <div>
          <strong>Databricks Load Scripts</strong>
          <span>${escapeHtml(result.qualified_table || result.target_table || '')}</span>
        </div>
        <span class="badge ${result.generated ? 'badge-success' : 'badge-error'}">${result.generated ? 'Generated' : 'Failed'}</span>
      </div>
      ${result.local_path_warning ? `
        <div class="qvd-parquet-messages qvd-load-warning">
          <strong>Path Warning</strong>
          <div>${escapeHtml(result.local_path_warning)}</div>
        </div>
      ` : ''}
      ${result.create_table_sql ? `
        <div class="qvd-load-preview">
          <strong>Create Table SQL</strong>
          <pre class="inspect-code">${escapeHtml(result.create_table_sql)}</pre>
        </div>
      ` : ''}
      ${result.copy_into_sql ? `
        <div class="qvd-load-preview">
          <strong>COPY INTO SQL</strong>
          <pre class="inspect-code">${escapeHtml(result.copy_into_sql)}</pre>
        </div>
      ` : ''}
      ${result.pyspark_snippet ? `
        <div class="qvd-load-preview">
          <strong>PySpark Load Snippet</strong>
          <pre class="inspect-code">${escapeHtml(result.pyspark_snippet)}</pre>
        </div>
      ` : ''}
      ${artifactRows.length ? `
        <div class="qvd-parquet-messages">
          <strong>Artifacts</strong>
          ${artifactRows.map(([name, value]) => `<div>${escapeHtml(name)}: ${escapeHtml(artifactLabel(value))}</div>`).join('')}
        </div>
      ` : ''}
      ${(result.errors || []).length ? `
        <div class="qvd-parquet-messages">
          <strong>Errors</strong>
          ${result.errors.map(error => `<div>${escapeHtml(error)}</div>`).join('')}
        </div>
      ` : ''}
      ${result.generated && state.currentPage === 'output' ? `
        <div style="display:flex;justify-content:flex-end;margin-top:10px">
          <button class="btn btn-primary qvd-preview-btn" data-qvd-package-file="${escapeHtml(fileName)}" ${(state.qvdMigrationPackageLoadingByFile?.[fileName] || state.qvdMigrationPackages?.[fileName]?.generated) ? 'disabled' : ''}>
            ${state.qvdMigrationPackageLoadingByFile?.[fileName] ? 'Packaging...' : state.qvdMigrationPackages?.[fileName]?.generated ? 'Migration Package Generated' : 'Generate Migration Package'}
          </button>
        </div>
      ` : ''}
      ${renderQvdMigrationPackage(fileName, state)}
    </div>
  `;
}

export function renderQvdMigrationPackage(fileName, state) {
  const loading = state.qvdMigrationPackageLoadingByFile?.[fileName];
  const result = state.qvdMigrationPackages?.[fileName];
  if (loading) {
    return `
      <div class="qvd-load-result">
        <div class="spinner"></div>
        <span style="font-size:12px;color:var(--text-dim)">Building migration package...</span>
      </div>
    `;
  }
  if (!result) return '';

  const summary = result.summary || {};
  const packageDownloadUrl = result.download_url || result.migration_package?.download_url || `/api/qvd/download-migration-package/${encodeURIComponent(state.qvdInspection?.session_id || state.sessionId || '')}`;
  return `
    <div class="qvd-load-result ${result.generated ? '' : 'qvd-row-preview-error'}">
      <div class="qvd-parquet-header">
        <div>
          <strong>Migration Package</strong>
          <span>${escapeHtml(result.migration_package_zip || result.migration_package?.relative_path || '')}</span>
        </div>
        <span class="badge ${result.generated ? 'badge-success' : 'badge-error'}">${result.generated ? 'Ready' : 'Failed'}</span>
      </div>
      ${result.generated ? `
        <div class="qvd-profile-summary">
          <div class="qvd-profile-card"><span>Records</span><strong>${summary.records || 0}</strong></div>
          <div class="qvd-profile-card"><span>Columns</span><strong>${summary.columns || 0}</strong></div>
          <div class="qvd-profile-card"><span>Validation</span><strong>${summary.validation_passed ? 'PASS' : 'FAIL'}</strong></div>
          <div class="qvd-profile-card"><span>Load Scripts</span><strong>${summary.load_scripts_generated ? 'YES' : 'NO'}</strong></div>
        </div>
        <div class="qvd-parquet-path">${escapeHtml(result.migration_package_zip || result.migration_package?.relative_path || '')}</div>
        <a class="btn btn-secondary qvd-preview-btn" style="display:inline-flex;margin-top:10px" href="${apiDownloadUrl(packageDownloadUrl)}">
          Download migration_package.zip
        </a>
      ` : ''}
      ${(result.errors || []).length ? `
        <div class="qvd-parquet-messages">
          <strong>Errors</strong>
          ${result.errors.map(error => `<div>${escapeHtml(error)}</div>`).join('')}
        </div>
      ` : ''}
    </div>
  `;
}

function renderQvdInspectionResults(state) {
  const inspection = state.qvdInspection;
  if (state.isUploading) {
    return `
      <div class="empty-state">
        <div class="spinner spinner-lg"></div>
        <div class="empty-state-title" style="margin-top:16px">Inspecting QVD metadata...</div>
      </div>
    `;
  }
  if (!inspection) {
    return `
      <div class="empty-state" id="empty-graph-state">
        <div class="empty-state-icon">🧱</div>
        <div class="empty-state-title">Upload QVD Files</div>
        <div class="empty-state-text">
          Select one or more QVD files to inspect source table structure before the Databricks migration steps.
        </div>
      </div>
    `;
  }

  return `
    <div class="qvd-inspection-results">
      ${renderQvdStatusChecklist(state)}
      <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:16px">
        <div>
          <h2 style="margin:0 0 6px;color:var(--text-primary);font-size:20px">QVD Metadata Inspector</h2>
          <div style="font-size:12px;color:var(--text-dim)">Session: <code>${escapeHtml(inspection.session_id)}</code></div>
        </div>
        <div style="display:flex;gap:8px;align-items:center;justify-content:flex-end;flex-wrap:wrap">
          ${inspection.artifacts?.source_structure_csv ? `<span class="badge badge-primary">source_structure.csv created</span>` : ''}
          <button class="btn btn-primary" id="qvd-business-btn">
            Continue To Business Analysis
          </button>
        </div>
      </div>

      ${inspection.errors?.length ? `
        <div style="border:1px solid var(--error);background:rgba(239,68,68,0.08);padding:12px;border-radius:6px;margin-bottom:16px;color:var(--text-primary)">
          ${inspection.errors.map(err => `<div><strong>${escapeHtml(err.file_name || 'File')}:</strong> ${escapeHtml(err.error)}</div>`).join('')}
        </div>
      ` : ''}

      <div style="display:flex;flex-direction:column;gap:16px">
        ${inspection.tables.map(table => {
          const summary = table.summary || {};
          const qa = table.quick_analysis || {};
          return `
            <section class="qvd-metadata-card" style="border:1px solid var(--border);background:rgba(255,255,255,0.76);border-radius:8px">
              <div style="padding:14px 16px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;gap:12px;align-items:flex-start">
                <div>
                  <h3 style="margin:0 0 4px;color:var(--text-primary);font-size:16px">${escapeHtml(summary.table_name || summary.file_name)}</h3>
                  <div style="font-size:12px;color:var(--text-dim)">${escapeHtml(summary.file_name || '')}</div>
                </div>
                <div style="display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end">
                  <span class="badge badge-info">${escapeHtml(summary.no_of_records || '0')} records</span>
                  <span class="badge badge-primary">${summary.field_count || 0} fields</span>
                  <span class="badge badge-info">${formatBytes(summary.file_size_bytes)}</span>
                </div>
              </div>
              <div style="padding:14px 16px">
                <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px;margin-bottom:14px">
                  ${renderAnalysisGroup('Date-like', qa.date_like_fields)}
                  ${renderAnalysisGroup('Numeric-like', qa.numeric_like_fields)}
                  ${renderAnalysisGroup('Key-like', qa.key_like_fields)}
                  ${renderAnalysisGroup('Text-like', qa.text_like_fields)}
                  ${renderAnalysisGroup('Flag-like', qa.flag_like_fields)}
                </div>
                ${renderQvdFieldTable(table.fields || [])}
              </div>
            </section>
          `;
        }).join('')}
      </div>
    </div>
  `;
}

function renderEmptyGraph() {
  const state = store.get();
  if (state.filename) {
    return `
      <div class="empty-state" id="empty-graph-state">
        <div class="empty-state-icon">📄</div>
        <div class="empty-state-title">No Tables Extracted</div>
        <div class="empty-state-text">
          The file <strong>${state.filename}</strong> was uploaded, but no table definitions (LOAD/SELECT) were found in the script.
          Please ensure your Qlik script uses standard <code>[TableName]: LOAD</code> or <code>TableName: LOAD</code> syntax.
        </div>
        <button class="btn btn-outline" onclick="document.getElementById('file-input').click()" style="margin-top:16px">
          Try Another File
        </button>
      </div>
    `;
  }

  return `
    <div class="empty-state" id="empty-graph-state">
      <div class="empty-state-icon">🔮</div>
      <div class="empty-state-title">Upload a QVF or ZIP File</div>
      <div class="empty-state-text">
        Upload a Qlik application file (.qvf) or a ZIP containing multiple files to visualize the data model as an interactive graph.
        Tables, fields, and relationships will be automatically detected.
      </div>
    </div>
  `;
}


function setupUploadHandlers() {
  const uploadZone = document.getElementById('upload-zone');
  const fileInput = document.getElementById('file-input');

  if (!uploadZone || !fileInput) return;

  // Drag & drop
  uploadZone.addEventListener('dragover', (e) => {
    e.preventDefault();
    uploadZone.classList.add('dragging');
  });

  uploadZone.addEventListener('dragleave', () => {
    uploadZone.classList.remove('dragging');
  });

  uploadZone.addEventListener('drop', (e) => {
    e.preventDefault();
    uploadZone.classList.remove('dragging');
    const files = e.dataTransfer.files;
    if (files.length > 0) handleFileUpload(files[0]);
  });

  // Click
  fileInput.addEventListener('change', (e) => {
    if (e.target.files.length > 0) handleFileUpload(e.target.files[0]);
  });
}

function setupModeSelector() {
  document.getElementById('mode-qvf')?.addEventListener('click', () => {
    store.set({ uploadMode: 'qvf' });
  });
  document.getElementById('mode-qvd')?.addEventListener('click', () => {
    store.set({ uploadMode: 'qvd' });
  });
}

export function setupQvdHandlers() {
  if (store.get().uploadMode !== 'qvd') return;
  const uploadZone = document.getElementById('qvd-upload-zone');
  const fileInput = document.getElementById('qvd-file-input');
  const inspectBtn = document.getElementById('qvd-inspect-btn');

  const setFiles = (fileList) => {
    const files = Array.from(fileList || []).filter(file => file.name.toLowerCase().endsWith('.qvd'));
    store.set({ qvdSelectedFiles: files });
  };

  uploadZone?.addEventListener('dragover', (e) => {
    e.preventDefault();
    uploadZone.classList.add('dragging');
  });

  uploadZone?.addEventListener('dragleave', () => {
    uploadZone.classList.remove('dragging');
  });

  uploadZone?.addEventListener('drop', (e) => {
    e.preventDefault();
    uploadZone.classList.remove('dragging');
    setFiles(e.dataTransfer.files);
  });

  fileInput?.addEventListener('change', (e) => {
    setFiles(e.target.files);
  });

  inspectBtn?.addEventListener('click', handleQvdInspect);
  document.getElementById('qvd-business-btn')?.addEventListener('click', () => store.navigate('business'));
  document.querySelectorAll('[data-qvd-preview-file]').forEach(button => {
    button.addEventListener('click', () => handleQvdPreviewRows(button.dataset.qvdPreviewFile));
  });
  document.querySelectorAll('[data-qvd-profile-file]').forEach(button => {
    button.addEventListener('click', () => handleQvdProfileColumns(button.dataset.qvdProfileFile));
  });
  document.querySelectorAll('[data-qvd-parquet-file]').forEach(button => {
    button.addEventListener('click', () => handleQvdConvertParquet(button.dataset.qvdParquetFile));
  });
  document.querySelectorAll('[data-qvd-validate-parquet-file]').forEach(button => {
    button.addEventListener('click', () => handleQvdValidateParquet(button.dataset.qvdValidateParquetFile));
  });
  document.querySelectorAll('[data-qvd-load-scripts-file]').forEach(button => {
    button.addEventListener('click', () => handleQvdGenerateDatabricksLoad(button.dataset.qvdLoadScriptsFile));
  });
  document.querySelectorAll('[data-qvd-package-file]').forEach(button => {
    button.addEventListener('click', () => handleQvdGenerateMigrationPackage(button.dataset.qvdPackageFile));
  });
}

async function handleQvdInspect() {
  const files = store.get().qvdSelectedFiles || [];
  if (!files.length) {
    alert('Please select one or more .qvd files');
    return;
  }

  store.set({
    sessionType: 'qvd',
    uploadMode: 'qvd',
    isUploading: true,
    isProcessing: true,
    uploadingFilename: files.length === 1 ? files[0].name : `${files.length} QVD files`,
    qvdSchemaSuggestion: null,
    qvdBusinessAnalysis: null,
    qvdKpiCatalog: null,
    qvdLineageReconciliation: null,
    qvdAiExplanation: null,
    qvdEditableMapping: [],
    qvdApprovedMapping: null,
    qvdRowPreviews: {},
    qvdColumnProfiles: {},
    qvdParquetConversions: {},
    qvdParquetValidations: {},
    qvdDatabricksLoadScripts: {},
    qvdMigrationPackages: {},
    qvdPreviewLoadingByFile: {},
    qvdProfileLoadingByFile: {},
    qvdParquetLoadingByFile: {},
    qvdParquetValidationLoadingByFile: {},
    qvdDatabricksLoadLoadingByFile: {},
    qvdMigrationPackageLoadingByFile: {},
    qvdMappingValidationErrors: [],
  });
  try {
    const result = await api.uploadInspectQvd(files, store.get().sessionId);
    store.set({
      sessionId: result.session_id,
      sessionType: 'qvd',
      uploadMode: 'qvd',
      qvdInspection: result,
      isUploading: false,
      isProcessing: false,
      uploadingFilename: null,
    });
  } catch (err) {
    store.set({ isUploading: false, isProcessing: false, uploadingFilename: null });
    alert(err.message || 'QVD inspection failed');
  }
}

async function handleQvdSuggestSchema() {
  const sessionId = store.get().qvdInspection?.session_id || store.get().sessionId;
  if (!sessionId) {
    alert('Inspect QVD metadata before suggesting a Databricks structure.');
    return;
  }

  store.set({ isSuggestingQvdSchema: true });
  try {
    const result = await api.suggestQvdSchema(sessionId);
    store.set({
      qvdSchemaSuggestion: result,
      qvdEditableMapping: cloneQvdMapping(result.mapping || []),
      qvdApprovedMapping: null,
      qvdMappingValidationErrors: [],
      isSuggestingQvdSchema: false,
    });
    store.navigate('inspect');
  } catch (err) {
    store.set({ isSuggestingQvdSchema: false });
    alert(err.message || 'QVD schema suggestion failed');
  }
}

function cloneQvdMapping(rows) {
  return JSON.parse(JSON.stringify(rows || []));
}

function qvdRowsFromApprovedMapping(state) {
  if (Array.isArray(state.qvdApprovedMapping?.mapping_rows)) return state.qvdApprovedMapping.mapping_rows;
  if (Array.isArray(state.qvdApprovedMapping?.mapping)) return state.qvdApprovedMapping.mapping;
  const rowsByFile = state.approved_mapping?.rows_by_file || state.approvedMapping?.rows_by_file || {};
  return Object.values(rowsByFile).flat();
}

function qvdTargetTableForFile(fileName, state) {
  const conversion = state.qvdParquetConversions?.[fileName];
  if (conversion?.target_table) return conversion.target_table;
  const validation = state.qvdParquetValidations?.[fileName];
  if (validation?.target_table) return validation.target_table;
  const load = state.qvdDatabricksLoadScripts?.[fileName];
  if (load?.target_table) return load.target_table;
  const pkg = state.qvdMigrationPackages?.[fileName];
  if (pkg?.target_table) return pkg.target_table;

  const mappingRow = qvdRowsFromApprovedMapping(state).find(row => row.qvd_file === fileName);
  if (mappingRow?.target_table) return mappingRow.target_table;

  const table = (state.qvdInspection?.tables || []).find(item => item.summary?.file_name === fileName);
  const fallback = table?.summary?.table_name || fileName.replace(/\.qvd$/i, '');
  return fallback
    .trim()
    .replace(/[^0-9A-Za-z_]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .toLowerCase();
}

async function handleQvdPreviewRows(fileName) {
  const state = store.get();
  const sessionId = state.qvdInspection?.session_id || state.sessionId;
  if (!sessionId || !fileName) return;

  store.set({
    qvdPreviewLoadingByFile: {
      ...(state.qvdPreviewLoadingByFile || {}),
      [fileName]: true,
    },
  });

  try {
    const result = await api.previewQvdRows(sessionId, fileName, 100);
    const latest = store.get();
    store.set({
      qvdRowPreviews: {
        ...(latest.qvdRowPreviews || {}),
        [fileName]: result,
      },
      qvdPreviewLoadingByFile: {
        ...(latest.qvdPreviewLoadingByFile || {}),
        [fileName]: false,
      },
    });
  } catch (err) {
    const latest = store.get();
    store.set({
      qvdRowPreviews: {
        ...(latest.qvdRowPreviews || {}),
        [fileName]: {
          success: false,
          columns: [],
          rows: [],
          row_count_returned: 0,
          limit: 100,
          reader_used: null,
          error: err.message || 'QVD row preview failed',
        },
      },
      qvdPreviewLoadingByFile: {
        ...(latest.qvdPreviewLoadingByFile || {}),
        [fileName]: false,
      },
    });
  }
}

async function handleQvdProfileColumns(fileName) {
  const state = store.get();
  const sessionId = state.qvdInspection?.session_id || state.sessionId;
  if (!sessionId || !fileName) return;

  store.set({
    qvdProfileLoadingByFile: {
      ...(state.qvdProfileLoadingByFile || {}),
      [fileName]: true,
    },
  });

  try {
    const result = await api.profileQvdColumns(sessionId, fileName, 10000);
    const latest = store.get();
    store.set({
      qvdColumnProfiles: {
        ...(latest.qvdColumnProfiles || {}),
        [fileName]: result,
      },
      qvdProfileLoadingByFile: {
        ...(latest.qvdProfileLoadingByFile || {}),
        [fileName]: false,
      },
    });
  } catch (err) {
    const latest = store.get();
    store.set({
      qvdColumnProfiles: {
        ...(latest.qvdColumnProfiles || {}),
        [fileName]: {
          success: false,
          rows_checked: 0,
          total_columns: 0,
          match_count: 0,
          needs_review_count: 0,
          mismatch_count: 0,
          profile_rows: [],
          error: err.message || 'QVD column profiling failed',
        },
      },
      qvdProfileLoadingByFile: {
        ...(latest.qvdProfileLoadingByFile || {}),
        [fileName]: false,
      },
    });
  }
}

async function handleQvdConvertParquet(fileName) {
  const state = store.get();
  const sessionId = state.qvdInspection?.session_id || state.sessionId;
  if (!sessionId || !fileName) return;

  store.set({
    qvdParquetLoadingByFile: {
      ...(state.qvdParquetLoadingByFile || {}),
      [fileName]: true,
    },
  });

  try {
    const result = await api.convertQvdToParquet(sessionId, fileName);
    const latest = store.get();
    store.set({
      qvdParquetConversions: {
        ...(latest.qvdParquetConversions || {}),
        [fileName]: result,
      },
      qvdParquetValidations: {
        ...(latest.qvdParquetValidations || {}),
        [fileName]: null,
      },
      qvdDatabricksLoadScripts: {
        ...(latest.qvdDatabricksLoadScripts || {}),
        [fileName]: null,
      },
      qvdMigrationPackages: {
        ...(latest.qvdMigrationPackages || {}),
        [fileName]: null,
      },
      qvdParquetLoadingByFile: {
        ...(latest.qvdParquetLoadingByFile || {}),
        [fileName]: false,
      },
    });
  } catch (err) {
    const latest = store.get();
    store.set({
      qvdParquetConversions: {
        ...(latest.qvdParquetConversions || {}),
        [fileName]: {
          success: false,
          target_table: '',
          row_count: 0,
          column_count: 0,
          parquet_path: '',
          conversion_warnings: [],
          errors: err.errors || [err.message || 'QVD to Parquet conversion failed'],
        },
      },
      qvdParquetLoadingByFile: {
        ...(latest.qvdParquetLoadingByFile || {}),
        [fileName]: false,
      },
    });
  }
}

async function handleQvdValidateParquet(fileName) {
  const state = store.get();
  const sessionId = state.qvdInspection?.session_id || state.sessionId;
  const targetTable = qvdTargetTableForFile(fileName, state);
  if (!sessionId || !fileName || !targetTable) return;

  store.set({
    qvdParquetValidationLoadingByFile: {
      ...(state.qvdParquetValidationLoadingByFile || {}),
      [fileName]: true,
    },
  });

  try {
    const result = await api.validateQvdParquet(sessionId, targetTable);
    const latest = store.get();
    store.set({
      qvdParquetValidations: {
        ...(latest.qvdParquetValidations || {}),
        [fileName]: result,
      },
      qvdDatabricksLoadScripts: {
        ...(latest.qvdDatabricksLoadScripts || {}),
        [fileName]: null,
      },
      qvdMigrationPackages: {
        ...(latest.qvdMigrationPackages || {}),
        [fileName]: null,
      },
      qvdParquetValidationLoadingByFile: {
        ...(latest.qvdParquetValidationLoadingByFile || {}),
        [fileName]: false,
      },
    });
  } catch (err) {
    const latest = store.get();
    store.set({
      qvdParquetValidations: {
        ...(latest.qvdParquetValidations || {}),
        [fileName]: {
          success: false,
          checks: err.checks || [],
          errors: err.errors || [err.message || 'QVD Parquet validation failed'],
          null_percentages: err.null_percentages || [],
        },
      },
      qvdParquetValidationLoadingByFile: {
        ...(latest.qvdParquetValidationLoadingByFile || {}),
        [fileName]: false,
      },
    });
  }
}

async function handleQvdGenerateDatabricksLoad(fileName) {
  const state = store.get();
  const sessionId = state.qvdInspection?.session_id || state.sessionId;
  const targetTable = qvdTargetTableForFile(fileName, state);
  if (!sessionId || !fileName || !targetTable) return;

  store.set({
    qvdDatabricksLoadLoadingByFile: {
      ...(state.qvdDatabricksLoadLoadingByFile || {}),
      [fileName]: true,
    },
  });

  try {
    const result = await api.generateQvdDatabricksLoad(sessionId, targetTable);
    const latest = store.get();
    store.set({
      qvdDatabricksLoadScripts: {
        ...(latest.qvdDatabricksLoadScripts || {}),
        [fileName]: result,
      },
      qvdMigrationPackages: {
        ...(latest.qvdMigrationPackages || {}),
        [fileName]: null,
      },
      qvdDatabricksLoadLoadingByFile: {
        ...(latest.qvdDatabricksLoadLoadingByFile || {}),
        [fileName]: false,
      },
    });
  } catch (err) {
    const latest = store.get();
    store.set({
      qvdDatabricksLoadScripts: {
        ...(latest.qvdDatabricksLoadScripts || {}),
        [fileName]: {
          generated: false,
          target_table: targetTable,
          errors: err.errors || [err.message || 'Databricks load script generation failed'],
          artifacts: {},
        },
      },
      qvdDatabricksLoadLoadingByFile: {
        ...(latest.qvdDatabricksLoadLoadingByFile || {}),
        [fileName]: false,
      },
    });
  }
}

async function handleQvdGenerateMigrationPackage(fileName) {
  const state = store.get();
  const sessionId = state.qvdInspection?.session_id || state.sessionId;
  const conversion = state.qvdParquetConversions?.[fileName];
  const targetTable = conversion?.target_table;
  if (!sessionId || !fileName || !targetTable) return;

  store.set({
    qvdMigrationPackageLoadingByFile: {
      ...(state.qvdMigrationPackageLoadingByFile || {}),
      [fileName]: true,
    },
  });

  try {
    const result = await api.generateQvdMigrationPackage(sessionId, targetTable, fileName);
    const latest = store.get();
    store.set({
      qvdMigrationPackages: {
        ...(latest.qvdMigrationPackages || {}),
        [fileName]: result,
      },
      qvdMigrationPackageLoadingByFile: {
        ...(latest.qvdMigrationPackageLoadingByFile || {}),
        [fileName]: false,
      },
    });
  } catch (err) {
    const latest = store.get();
    store.set({
      qvdMigrationPackages: {
        ...(latest.qvdMigrationPackages || {}),
        [fileName]: {
          generated: false,
          errors: err.errors || [err.message || 'QVD migration package generation failed'],
        },
      },
      qvdMigrationPackageLoadingByFile: {
        ...(latest.qvdMigrationPackageLoadingByFile || {}),
        [fileName]: false,
      },
    });
  }
}

async function handleFileUpload(file) {
  const ext = file.name.split('.').pop().toLowerCase();
  if (ext !== 'qvf' && ext !== 'zip') {
    alert('Please upload a .qvf or .zip file');
    return;
  }

  store.set({ isUploading: true, isProcessing: true, uploadingFilename: file.name });

  try {
    const uploadResult = await api.uploadFile(file, store.get().sessionId);
    const sessionId = uploadResult.sessionId;

    // Fetch the updated session model (handles both single and bulk uploads)
    const result = await api.getModel(sessionId);

    // Enrich nodes with type info (safety check for graph)
    const previousGraph = store.get().graph || { nodes: [], edges: [] };
    const graphData = (result.graph && (result.graph.nodes || []).length > 0)
      ? result.graph
      : previousGraph;
    const enrichedGraph = {
      ...graphData,
      nodes: (graphData.nodes || []).map(n => ({
        ...n,
        type: n.type || ((n.keyFields || []).length > 1 ? 'fact' : 'dimension'),
      })),
    };

    store.set({
      sessionId: sessionId,
      sessionType: 'qvf',
      uploadMode: 'qvf',
      fileId: result.fileId || uploadResult.fileId,
      filename: result.filename || uploadResult.filename,
      graph: enrichedGraph,
      tables: result.tables || [],
      associations: result.associations || [],
      metadata: result.metadata,
      script: result.script || '',
      sqlSections: result.sqlSections || [],
      description: result.description || '',
      editedSql: result.script || '',
      editedText: result.description || '',
      generationPlan: result.generationPlan || [],
      generationPlanText: result.generationPlanText || '',
      sessionStats: result.sessionStats,
      isUploading: false,
      isProcessing: false,
      uploadingFilename: null,
    });

    // Seed per-file review state with the plan so the Review page
    // can show it immediately without waiting for migration.
    const files = result.sessionStats?.files || [];
    files.forEach(file => {
      store.ensureFileReviewState(file.fileId, {
        editedSql: file.script || '',
        editedText: file.description || '',
        generationPlan: file.generationPlan || result.generationPlan || [],
        generationPlanText: file.generationPlanText || result.generationPlanText || '',
      });
    });
    // Also seed the current file
    const currentFileId = result.fileId || uploadResult.fileId;
    if (currentFileId) {
      store.setFileReviewState(currentFileId, {
        generationPlan: result.generationPlan || [],
        generationPlanText: result.generationPlanText || '',
      });
    }

  } catch (err) {
    console.error('Upload error:', err);
    store.set({ isUploading: false, isProcessing: false, uploadingFilename: null });

    // Re-render the upload zone with an error state so the user can retry
    const uploadZone = document.getElementById('upload-zone');
    if (uploadZone) {
      uploadZone.innerHTML = `
        <div class="upload-zone-icon">❌</div>
        <div class="upload-zone-text" style="color:var(--error)">${err.message || 'Upload failed'}</div>
        <div class="upload-zone-hint">Click to try again</div>
        <input type="file" id="file-input" accept=".qvf,.zip" />
      `;
      setupUploadHandlers();
    } else {
      alert(err.message || 'Upload failed');
    }
    return;
  }
}

function setupUploadOverlay() {
  const state = store.get();
  if (state.uploadMode === 'qvd') return;
  if (!state.isUploading) return;

  const graphArea = document.getElementById('graph-area');
  if (!graphArea) return;

  const overlay = document.createElement('div');
  overlay.className = 'loading-overlay';
  overlay.innerHTML = `
    <div class="loading-card">
      <div class="spinner spinner-lg"></div>
      <h3>Processing ${state.uploadingFilename || 'file'}...</h3>
      <p>Extracting tables, relationships, and dependency links.</p>
    </div>
  `;
  graphArea.appendChild(overlay);
}

function setupGraphIfData() {
  const state = store.get();
  if (state.graph.nodes.length === 0) return;

  const graphArea = document.getElementById('graph-area');
  if (!graphArea) return;

  // Remove empty state
  const emptyState = document.getElementById('empty-graph-state');
  if (emptyState) emptyState.remove();

  if (graphComponent) graphComponent.destroy();

  graphComponent = new GraphComponent(graphArea, {
    showUploadButtons: true,
    onNodeClick: () => {},
    onUploadClick: (node) => {
      // Create a temporary file input to bypass browser security restrictions
      const tempInput = document.createElement('input');
      tempInput.type = 'file';
      tempInput.accept = '.qvf,.zip';
      tempInput.style.display = 'none';
      
      tempInput.onchange = (e) => {
        if (e.target.files.length > 0) {
          handleFileUpload(e.target.files[0]);
        }
        document.body.removeChild(tempInput);
      };
      
      document.body.appendChild(tempInput);
      tempInput.click();
    }
  });

  graphComponent.update(state.graph);
}

function setupReviewButton() {
  const btn = document.getElementById('review-btn');
  if (!btn) return;

  btn.addEventListener('click', () => {
    const state = store.get();
    if (state.filename) {
      store.navigate('review');
    }
  });
}

function setupInspectButton() {
  const btn = document.getElementById('inspect-btn');
  if (!btn) return;

  btn.addEventListener('click', () => {
    const state = store.get();
    if (state.filename || (state.uploadMode === 'qvd' && state.qvdInspection)) {
      store.navigate('inspect');
    }
  });
}

// Expose handleFileUpload for the graph to call
window.handleGraphUpload = handleFileUpload;

function setupResetButton() {
  const btn = document.getElementById('reset-session-btn');
  if (!btn) return;

  btn.addEventListener('click', async () => {
    if (confirm('Are you sure you want to clear all uploaded files and start over?')) {
      try {
        await api.reset();
      } catch (err) {
        console.warn('Server reset failed (continuing anyway):', err.message);
      }
      store.reset(); // also clears localStorage
      window.location.reload();
    }
  });
}

export function destroyUploadPage() {
  if (graphComponent) {
    graphComponent.destroy();
    graphComponent = null;
  }
}
