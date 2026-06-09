import { api } from '../api.js';
import { store } from '../store.js';
import { renderQvdStatusChecklist } from '../components/qvdStatusChecklist.js';
import { escapeHtml } from '../utils.js';

const EXECUTION_MODES = [
  ['generate_sql_only', 'Generate SQL Only'],
  ['execute_ddl_only', 'Execute DDL Only'],
  ['execute_ddl_load', 'Execute DDL + Load Data'],
  ['full_migration', 'Full Migration Execution'],
];

let activeDeploySection = 'connection';

export function renderDeployPage(container) {
  const state = store.get();
  if (state.uploadMode !== 'qvd') {
    store.navigate('upload');
    return;
  }

  const packages = state.qvdMigrationPackages || {};
  const packageEntries = Object.entries(packages).filter(([, result]) => result?.generated);
  if (!packageEntries.length) {
    container.innerHTML = `
      <div class="page">
        <div class="empty-state" style="margin:auto">
          <div class="empty-state-title">Migration Package Required</div>
          <div class="empty-state-text">Generate the QVD migration package before deploying to Databricks.</div>
          <button class="btn btn-primary" id="deploy-output-btn">Go To Output</button>
        </div>
      </div>
    `;
    document.getElementById('deploy-output-btn')?.addEventListener('click', () => store.navigate('output'));
    return;
  }

  const config = state.qvdDatabricksConfig || {};
  const connection = state.qvdDatabricksConnection;
  const precheck = state.qvdDatabricksPrecheck;
  const execution = state.qvdDatabricksExecution;
  const selectedMode = state.qvdExecutionMode || 'generate_sql_only';
  const sections = [
    ['connection', 'Connection', 'Configure and test Databricks workspace access.'],
    ['target', 'Target Location', 'Choose catalog, schema, volume, or cloud path.'],
    ['upload', 'Data Upload', 'Upload validated Parquet to a Unity Catalog volume.'],
    ['precheck', 'Precheck', 'Verify package, paths, and validation readiness.'],
    ['execution', 'Execution', 'Optionally execute the migration in Databricks.'],
    ['results', 'Results', 'Review execution report, logs, and artifacts.'],
  ];
  const active = sections.some(([id]) => id === activeDeploySection) ? activeDeploySection : 'connection';

  container.innerHTML = `
    <div class="page qvd-review-page">
      <main class="qvd-review-main">
        ${renderQvdStatusChecklist(state)}
        <header class="inspect-header">
          <div>
            <div class="inspect-title">Databricks Deployment</div>
            <div class="inspect-subtitle">Configure Databricks access, test permissions, and optionally execute the validated QVD migration.</div>
          </div>
          <div class="inspect-metrics">
            <div class="inspect-metric"><span>Connection</span><strong>${connection?.success ? 'PASS' : connection ? 'FAIL' : 'NOT TESTED'}</strong></div>
            <div class="inspect-metric"><span>Execution</span><strong>${execution?.success ? 'DONE' : execution ? 'FAILED' : 'READY'}</strong></div>
          </div>
        </header>

        <section class="qvd-review-card">
          <div class="qvd-business-status-strip">
            <div class="qvd-business-status ${connection?.success ? 'generated' : connection ? 'failed' : 'pending'}"><span>Connection</span><strong>${connection?.success ? 'Passed' : connection ? 'Failed' : 'Not tested'}</strong></div>
            <div class="qvd-business-status ${precheck?.passed ? 'generated' : precheck ? 'failed' : 'pending'}"><span>Precheck</span><strong>${precheck?.passed ? 'Passed' : precheck ? 'Needs attention' : 'Not run'}</strong></div>
            <div class="qvd-business-status ${execution?.success ? 'generated' : execution ? 'failed' : 'pending'}"><span>Execution</span><strong>${execution?.success ? 'Done' : execution ? 'Failed' : 'Ready'}</strong></div>
          </div>
          <div class="qvd-business-shell">
            <nav class="qvd-business-section-nav" aria-label="Databricks Deployment sections">
              ${sections.map(([id, label, helper]) => `
                <button type="button" class="qvd-business-section-link ${active === id ? 'active' : ''}" data-deploy-section="${id}">
                  <span>${escapeHtml(label)}</span>
                  <small>${escapeHtml(helper)}</small>
                </button>
              `).join('')}
            </nav>
            <div class="qvd-business-section-panel">
              ${renderDeploySection(active, config, connection, precheck, execution, packageEntries, selectedMode, state)}
            </div>
          </div>
        </section>
      </main>
    </div>
  `;

  setupDeployHandlers();
}

function deploySectionHeader(title, helper, action = '') {
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

function renderDeploySection(active, config, connection, precheck, execution, packageEntries, selectedMode, state) {
  if (active === 'precheck') {
    return `
      ${deploySectionHeader('Deployment Precheck', 'Verify the migration package, Parquet validation, load scripts, and Databricks-readable path.', `
        <button class="btn btn-secondary" id="dbx-precheck-btn" ${state.isRunningDatabricksPrecheck ? 'disabled' : ''}>
          ${state.isRunningDatabricksPrecheck ? 'Checking...' : 'Run Deployment Precheck'}
        </button>
      `)}
      ${renderDeploymentSelectors(packageEntries, selectedMode)}
      ${renderPrecheckResult(precheck)}
    `;
  }
  if (active === 'target') {
    return `
      ${deploySectionHeader('Target Location', 'Discover or enter the Unity Catalog target for this migration.', `
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <button class="btn btn-secondary" id="dbx-discover-catalogs-btn" ${state.isDiscoveringDatabricksCatalogs ? 'disabled' : ''}>${state.isDiscoveringDatabricksCatalogs ? 'Discovering...' : 'Discover Catalogs'}</button>
          <button class="btn btn-secondary" id="dbx-discover-schemas-btn" ${state.isDiscoveringDatabricksSchemas ? 'disabled' : ''}>${state.isDiscoveringDatabricksSchemas ? 'Discovering...' : 'Discover Schemas'}</button>
          <button class="btn btn-secondary" id="dbx-discover-volumes-btn" ${state.isDiscoveringDatabricksVolumes ? 'disabled' : ''}>${state.isDiscoveringDatabricksVolumes ? 'Discovering...' : 'Discover Volumes'}</button>
        </div>
      `)}
      ${renderTargetForm(config, state)}
      ${renderPrepareTargetCard(state)}
    `;
  }
  if (active === 'upload') {
    return `
      ${deploySectionHeader('Data Upload', 'Upload generated Parquet files to the selected Unity Catalog volume.', `
        <button class="btn btn-primary" id="dbx-upload-parquet-btn" ${state.isUploadingDatabricksParquet ? 'disabled' : ''}>
          ${state.isUploadingDatabricksParquet ? 'Uploading...' : 'Upload Parquet To Databricks Volume'}
        </button>
      `)}
      ${renderDeploymentSelectors(packageEntries, selectedMode)}
      ${renderUploadPanel(config, state)}
    `;
  }
  if (active === 'execution') {
    return `
      ${deploySectionHeader('Execution', 'Execution is optional and starts only when you click the button.', `
        <button class="btn btn-success" id="dbx-execute-btn" ${state.isExecutingDatabricksMigration ? 'disabled' : ''}>
          ${state.isExecutingDatabricksMigration ? 'Executing...' : 'Execute Migration'}
        </button>
      `)}
      ${renderDeploymentSelectors(packageEntries, selectedMode)}
      <div class="inspect-empty">Use Generate SQL Only for a dry run, or choose an execution mode when Databricks access and paths are ready.</div>
    `;
  }
  if (active === 'results') {
    return `
      ${deploySectionHeader('Results', 'Review execution report, logs, errors, and generated artifacts.')}
      ${execution ? renderExecutionResult(execution) : '<div class="inspect-empty">No execution result yet.</div>'}
    `;
  }
  return `
    ${deploySectionHeader('Connection Configuration', 'Stored only for this local migration session.', `
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <button class="btn btn-secondary" id="dbx-save-config-btn" ${state.isSavingDatabricksConfig ? 'disabled' : ''}>
          ${state.isSavingDatabricksConfig ? 'Saving...' : 'Save Session Configuration'}
        </button>
        <button class="btn btn-primary" id="dbx-test-btn" ${state.isTestingDatabricksConnection ? 'disabled' : ''}>
          ${state.isTestingDatabricksConnection ? 'Testing...' : 'Test Connection'}
        </button>
        <button class="btn btn-secondary" id="dbx-discover-warehouses-btn" ${state.isDiscoveringDatabricksWarehouses ? 'disabled' : ''}>
          ${state.isDiscoveringDatabricksWarehouses ? 'Discovering...' : 'Discover Warehouses'}
        </button>
      </div>
    `)}
    ${renderConnectionForm(config)}
    ${renderConnectionStatus(connection)}
  `;
}

function renderDeploymentSelectors(packageEntries, selectedMode) {
  return `
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin-bottom:12px">
      <label class="qvd-deploy-field">
        <span>Target Table</span>
        <select class="qvd-mapping-select" id="dbx-target-table">
          ${packageEntries.map(([fileName, result]) => `
            <option value="${escapeHtml(result.summary?.target_table || result.target_table || '')}">${escapeHtml(result.summary?.target_table || result.target_table || fileName)}</option>
          `).join('')}
        </select>
      </label>
      <label class="qvd-deploy-field">
        <span>Execution Mode</span>
        <select class="qvd-mapping-select" id="dbx-execution-mode">
          ${EXECUTION_MODES.map(([value, label]) => `
            <option value="${value}" ${value === selectedMode ? 'selected' : ''}>${label}</option>
          `).join('')}
        </select>
      </label>
    </div>
  `;
}

function renderConnectionForm(config) {
  const state = store.get();
  const warehouses = state.qvdDatabricksWarehouses || [];
  const field = (label, id, value, type = 'text', placeholder = '') => `
    <label class="qvd-deploy-field">
      <span>${label}</span>
      <input class="qvd-mapping-input" id="${id}" type="${type}" value="${escapeHtml(value || '')}" placeholder="${escapeHtml(placeholder)}" />
    </label>
  `;
  return `
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px">
      ${field('Databricks Workspace URL', 'dbx-workspace-url', config.workspace_url, 'text', 'https://dbc-...cloud.databricks.com')}
      ${field('Personal Access Token', 'dbx-token', config.personal_access_token, 'password', config.personal_access_token_present ? 'Saved token present' : '')}
      <label class="qvd-deploy-field">
        <span>SQL Warehouse ID</span>
        <input class="qvd-mapping-input" id="dbx-warehouse-id" list="dbx-warehouse-options" value="${escapeHtml(config.sql_warehouse_id || '')}" />
        <datalist id="dbx-warehouse-options">
          ${warehouses.map(row => `<option value="${escapeHtml(row.id || '')}">${escapeHtml(row.name || row.id || '')}</option>`).join('')}
        </datalist>
      </label>
    </div>
  `;
}

function renderTargetForm(config, state) {
  const catalogs = state.qvdDatabricksCatalogs || [];
  const schemas = state.qvdDatabricksSchemas || [];
  const volumes = state.qvdDatabricksVolumes || [];
  const volumePath = config.cloud_storage_path
    ? `${String(config.cloud_storage_path).replace(/\/$/, '')}/${currentTargetTable() || '<target_table>'}/`
    : config.volume
      ? `/Volumes/${config.catalog || 'catalog'}/${config.schema || 'schema'}/${config.volume}/${store.get().sessionId || '<session>'}/${currentTargetTable() || '<target_table>'}/`
      : config.volume_path
        ? `${String(config.volume_path).replace(/\/$/, '')}/${store.get().sessionId || '<session>'}/${currentTargetTable() || '<target_table>'}/`
        : 'Select a volume or cloud path to make Parquet readable by Databricks.';
  return `
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px">
      <label class="qvd-deploy-field">
        <span>Catalog</span>
        <input class="qvd-mapping-input" id="dbx-catalog" list="dbx-catalog-options" value="${escapeHtml(config.catalog || 'main')}" />
        <datalist id="dbx-catalog-options">${catalogs.map(row => `<option value="${escapeHtml(row.name || '')}"></option>`).join('')}</datalist>
      </label>
      <label class="qvd-deploy-field">
        <span>Schema</span>
        <input class="qvd-mapping-input" id="dbx-schema" list="dbx-schema-options" value="${escapeHtml(config.schema || 'qvd_raw')}" />
        <datalist id="dbx-schema-options">${schemas.map(row => `<option value="${escapeHtml(row.name || '')}"></option>`).join('')}</datalist>
      </label>
      <label class="qvd-deploy-field">
        <span>Volume</span>
        <input class="qvd-mapping-input" id="dbx-volume" list="dbx-volume-options" value="${escapeHtml(config.volume || '')}" placeholder="qvd_uploads" />
        <datalist id="dbx-volume-options">${volumes.map(row => `<option value="${escapeHtml(row.name || '')}"></option>`).join('')}</datalist>
      </label>
      <label class="qvd-deploy-field">
        <span>Optional Cloud Storage Path</span>
        <input class="qvd-mapping-input" id="dbx-cloud-path" value="${escapeHtml(config.cloud_storage_path || '')}" placeholder="s3://bucket/path or abfss://..." />
      </label>
      <label class="qvd-deploy-field">
        <span>Optional Volume Path Override</span>
        <input class="qvd-mapping-input" id="dbx-volume-path" value="${escapeHtml(config.volume_path || '')}" placeholder="/Volumes/catalog/schema/volume" />
      </label>
    </div>
    <div class="qvd-parquet-messages" style="margin-top:12px">
      <strong>Databricks-readable path preview</strong>
      <div>${escapeHtml(volumePath)}</div>
    </div>
  `;
}

function renderPrepareTargetCard(state) {
  return `
    <div class="qvd-ddl-result" style="margin-top:12px">
      <div class="qvd-ddl-header">
        <div>
          <h3>Prepare Databricks target</h3>
          <p>Create the schema or volume if your workspace does not have them yet.</p>
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <button class="btn btn-secondary" id="dbx-create-schema-btn" ${state.isPreparingDatabricksTarget ? 'disabled' : ''}>Create Schema</button>
          <button class="btn btn-secondary" id="dbx-create-volume-btn" ${state.isPreparingDatabricksTarget ? 'disabled' : ''}>Create Volume</button>
        </div>
      </div>
    </div>
  `;
}

function renderUploadPanel(config, state) {
  const upload = state.qvdDatabricksUpload;
  const targetTable = currentTargetTable();
  const localPath = Object.values(state.qvdDatabricksLoadScripts || {}).find(item => item?.target_table === targetTable)?.parquet_path || 'Generated local Parquet path from load_config.json';
  const targetPath = config.cloud_storage_path
    ? `${String(config.cloud_storage_path).replace(/\/$/, '')}/${targetTable || '<target_table>'}/`
    : (upload?.volume_path || (config.volume ? `/Volumes/${config.catalog}/${config.schema}/${config.volume}/${state.sessionId}/${targetTable || '<target_table>'}/` : 'Select a Unity Catalog volume first.'));
  return `
    <div class="qvd-profile-summary">
      <div class="qvd-profile-card"><span>Local Parquet</span><strong>${escapeHtml(localPath)}</strong></div>
      <div class="qvd-profile-card"><span>Target Path</span><strong>${escapeHtml(targetPath)}</strong></div>
      <div class="qvd-profile-card"><span>Uploaded Files</span><strong>${escapeHtml(upload?.uploaded_file_count ?? 0)}</strong></div>
      <div class="qvd-profile-card"><span>Status</span><strong>${upload?.success ? 'READY' : upload ? 'FAILED' : 'NOT UPLOADED'}</strong></div>
    </div>
    ${(upload?.errors || []).length ? `<div class="qvd-parquet-messages qvd-row-preview-error">${upload.errors.map(error => `<div>${escapeHtml(error)}</div>`).join('')}</div>` : ''}
    ${(upload?.warnings || []).length ? `<div class="qvd-parquet-messages">${upload.warnings.map(warning => `<div>${escapeHtml(warning)}</div>`).join('')}</div>` : ''}
  `;
}

function renderConnectionStatus(connection) {
  if (!connection) return '';
  const checks = connection.checks || {};
  return `
    <div class="qvd-profile-summary" style="margin-top:12px">
      ${['connection', 'warehouse', 'catalog', 'schema'].map(name => `
        <div class="qvd-profile-card">
          <span>${name}</span>
          <strong>${checks[name] ? 'PASS' : 'FAIL'}</strong>
        </div>
      `).join('')}
    </div>
    ${(connection.errors || []).length ? `
      <div class="qvd-parquet-messages qvd-row-preview-error">
        <strong>Connection Errors</strong>
        ${connection.errors.map(error => `<div>${escapeHtml(error)}</div>`).join('')}
      </div>
    ` : ''}
  `;
}

function renderPrecheckResult(precheck) {
  if (!precheck) {
    return `
      <div class="inspect-empty" style="margin-bottom:12px">
        Run precheck to verify the migration package, Parquet validation, load scripts, and Databricks-readable path before execution.
      </div>
    `;
  }
  return `
    <div class="qvd-parquet-messages ${precheck.passed ? '' : 'qvd-row-preview-error'}" style="margin-bottom:12px">
      <strong>Deployment Precheck: ${precheck.passed ? 'PASS' : 'NEEDS ATTENTION'}</strong>
      ${(precheck.errors || []).length ? precheck.errors.map(error => `<div>${escapeHtml(error)}</div>`).join('') : '<div>Required package and validation artifacts are present.</div>'}
      ${(precheck.warnings || []).map(warning => `<div>${escapeHtml(warning)}</div>`).join('')}
    </div>
  `;
}

function renderExecutionResult(execution) {
  if (!execution) return '';
  const report = execution.report || {};
  return `
    <div class="qvd-profile-summary">
      <div class="qvd-profile-card"><span>Status</span><strong>${escapeHtml(report.execution_status || '')}</strong></div>
      <div class="qvd-profile-card"><span>Source Rows</span><strong>${escapeHtml(report.source_row_count ?? '')}</strong></div>
      <div class="qvd-profile-card"><span>Loaded Rows</span><strong>${escapeHtml(report.loaded_row_count ?? '')}</strong></div>
      <div class="qvd-profile-card"><span>Duration</span><strong>${escapeHtml(report.duration_seconds ?? '')}s</strong></div>
    </div>
    ${(execution.logs || []).length ? `
      <div class="qvd-load-preview">
        <strong>Execution Logs</strong>
        <pre class="inspect-code">${escapeHtml((execution.logs || []).join('\n'))}</pre>
      </div>
    ` : ''}
    ${execution.artifacts ? `
      <div class="qvd-parquet-messages">
        <strong>Execution Artifacts</strong>
        ${Object.entries(execution.artifacts).map(([name, value]) => `<div>${escapeHtml(name)}: ${escapeHtml(value)}</div>`).join('')}
      </div>
    ` : ''}
    ${(report.errors || execution.errors || []).length ? `
      <div class="qvd-parquet-messages qvd-row-preview-error">
        <strong>Errors</strong>
        ${(report.errors || execution.errors || []).map(error => `<div>${escapeHtml(error)}</div>`).join('')}
      </div>
    ` : ''}
  `;
}

function collectConfig() {
  const existing = store.get().qvdDatabricksConfig || {};
  const tokenInput = document.getElementById('dbx-token');
  return {
    ...existing,
    workspace_url: document.getElementById('dbx-workspace-url')?.value ?? existing.workspace_url ?? '',
    personal_access_token: tokenInput ? (tokenInput.value || existing.personal_access_token || '') : (existing.personal_access_token || ''),
    sql_warehouse_id: document.getElementById('dbx-warehouse-id')?.value ?? existing.sql_warehouse_id ?? '',
    catalog: document.getElementById('dbx-catalog')?.value ?? existing.catalog ?? 'main',
    schema: document.getElementById('dbx-schema')?.value ?? existing.schema ?? 'qvd_raw',
    volume: document.getElementById('dbx-volume')?.value ?? existing.volume ?? '',
    volume_path: document.getElementById('dbx-volume-path')?.value ?? existing.volume_path ?? '',
    cloud_storage_path: document.getElementById('dbx-cloud-path')?.value ?? existing.cloud_storage_path ?? '',
  };
}

function currentTargetTable() {
  const selected = document.getElementById('dbx-target-table')?.value;
  if (selected) return selected;
  const packages = store.get().qvdMigrationPackages || {};
  const first = Object.values(packages).find(result => result?.generated);
  return first?.summary?.target_table || first?.target_table || '';
}

function setupDeployHandlers() {
  document.querySelectorAll('[data-deploy-section]').forEach(button => {
    button.addEventListener('click', () => {
      const nextConfig = collectConfig();
      activeDeploySection = button.dataset.deploySection || 'connection';
      store.set({ qvdDatabricksConfig: nextConfig });
    });
  });
  document.querySelectorAll('#dbx-workspace-url, #dbx-token, #dbx-warehouse-id, #dbx-catalog, #dbx-schema, #dbx-volume, #dbx-volume-path, #dbx-cloud-path').forEach(input => {
    input.addEventListener('change', () => {
      store.set({ qvdDatabricksConfig: collectConfig() });
    });
  });
  document.getElementById('dbx-execution-mode')?.addEventListener('change', event => {
    store.set({ qvdExecutionMode: event.target.value });
  });
  document.getElementById('dbx-save-config-btn')?.addEventListener('click', handleSaveConfig);
  document.getElementById('dbx-test-btn')?.addEventListener('click', handleTestConnection);
  document.getElementById('dbx-discover-warehouses-btn')?.addEventListener('click', handleDiscoverWarehouses);
  document.getElementById('dbx-discover-catalogs-btn')?.addEventListener('click', handleDiscoverCatalogs);
  document.getElementById('dbx-discover-schemas-btn')?.addEventListener('click', handleDiscoverSchemas);
  document.getElementById('dbx-discover-volumes-btn')?.addEventListener('click', handleDiscoverVolumes);
  document.getElementById('dbx-create-schema-btn')?.addEventListener('click', handleCreateSchema);
  document.getElementById('dbx-create-volume-btn')?.addEventListener('click', handleCreateVolume);
  document.getElementById('dbx-upload-parquet-btn')?.addEventListener('click', handleUploadParquet);
  document.getElementById('dbx-precheck-btn')?.addEventListener('click', handlePrecheck);
  document.getElementById('dbx-execute-btn')?.addEventListener('click', handleExecute);
}

async function handleSaveConfig() {
  const state = store.get();
  const config = collectConfig();
  store.set({ isSavingDatabricksConfig: true, qvdDatabricksConfig: config });
  try {
    const result = await api.saveQvdDatabricksConfig(state.sessionId, config);
    store.set({ isSavingDatabricksConfig: false, qvdDatabricksConfig: config, qvdDatabricksConnection: result });
  } catch (err) {
    store.set({ isSavingDatabricksConfig: false, qvdDatabricksConnection: { success: false, errors: err.errors || [err.message] } });
  }
}

async function handleTestConnection() {
  const state = store.get();
  const config = collectConfig();
  store.set({ isTestingDatabricksConnection: true, qvdDatabricksConfig: config });
  try {
    const result = await api.testQvdDatabricksConnection(state.sessionId, config);
    store.set({ isTestingDatabricksConnection: false, qvdDatabricksConnection: result });
    if (result.success) {
      handleDiscoverWarehouses();
      handleDiscoverCatalogs();
    }
  } catch (err) {
    store.set({ isTestingDatabricksConnection: false, qvdDatabricksConnection: { success: false, checks: err.checks || {}, errors: err.errors || [err.message] } });
  }
}

async function handleDiscoverWarehouses() {
  const state = store.get();
  const config = collectConfig();
  store.set({ isDiscoveringDatabricksWarehouses: true, qvdDatabricksConfig: config });
  try {
    const result = await api.discoverQvdDatabricksWarehouses(state.sessionId, config);
    store.set({ isDiscoveringDatabricksWarehouses: false, qvdDatabricksWarehouses: result.warehouses || [] });
  } catch (err) {
    store.set({ isDiscoveringDatabricksWarehouses: false, qvdDatabricksConnection: { success: false, errors: err.errors || [err.message || 'Databricks warehouse discovery failed. Check the workspace URL and token, then retry.'] } });
  }
}

async function handleDiscoverCatalogs() {
  const state = store.get();
  const config = collectConfig();
  store.set({ isDiscoveringDatabricksCatalogs: true, qvdDatabricksConfig: config });
  try {
    const result = await api.discoverQvdDatabricksCatalogs(state.sessionId, config);
    store.set({ isDiscoveringDatabricksCatalogs: false, qvdDatabricksCatalogs: result.catalogs || [] });
  } catch (err) {
    store.set({ isDiscoveringDatabricksCatalogs: false, qvdDatabricksConnection: { success: false, errors: err.errors || [err.message || 'Databricks catalog discovery failed. The saved token may be missing or the catalog API may be unavailable.'] } });
  }
}

async function handleDiscoverSchemas() {
  const state = store.get();
  const config = collectConfig();
  store.set({ isDiscoveringDatabricksSchemas: true, qvdDatabricksConfig: config });
  try {
    const result = await api.discoverQvdDatabricksSchemas(state.sessionId, config);
    store.set({ isDiscoveringDatabricksSchemas: false, qvdDatabricksSchemas: result.schemas || [] });
  } catch (err) {
    store.set({ isDiscoveringDatabricksSchemas: false, qvdDatabricksConnection: { success: false, errors: err.errors || [err.message || 'Databricks schema discovery failed. Check catalog access, then retry.'] } });
  }
}

async function handleDiscoverVolumes() {
  const state = store.get();
  const config = collectConfig();
  store.set({ isDiscoveringDatabricksVolumes: true, qvdDatabricksConfig: config });
  try {
    const result = await api.discoverQvdDatabricksVolumes(state.sessionId, config);
    store.set({ isDiscoveringDatabricksVolumes: false, qvdDatabricksVolumes: result.volumes || [] });
  } catch (err) {
    store.set({ isDiscoveringDatabricksVolumes: false, qvdDatabricksConnection: { success: false, errors: err.errors || [err.message || 'Databricks volume discovery failed. Check schema and volume permissions, then retry.'] } });
  }
}

async function handleCreateSchema() {
  const state = store.get();
  const config = collectConfig();
  store.set({ isPreparingDatabricksTarget: true, qvdDatabricksConfig: config });
  try {
    await api.createQvdDatabricksSchema(state.sessionId, config);
    store.set({ isPreparingDatabricksTarget: false });
    handleDiscoverSchemas();
  } catch (err) {
    store.set({ isPreparingDatabricksTarget: false, qvdDatabricksConnection: { success: false, errors: err.errors || [err.message] } });
  }
}

async function handleCreateVolume() {
  const state = store.get();
  const config = collectConfig();
  store.set({ isPreparingDatabricksTarget: true, qvdDatabricksConfig: config });
  try {
    await api.createQvdDatabricksVolume(state.sessionId, config);
    store.set({ isPreparingDatabricksTarget: false });
    handleDiscoverVolumes();
  } catch (err) {
    store.set({ isPreparingDatabricksTarget: false, qvdDatabricksConnection: { success: false, errors: err.errors || [err.message] } });
  }
}

async function handleUploadParquet() {
  const state = store.get();
  const config = collectConfig();
  const targetTable = currentTargetTable();
  store.set({ isUploadingDatabricksParquet: true, qvdDatabricksConfig: config });
  try {
    const result = await api.uploadQvdParquetToDatabricksVolume(state.sessionId, targetTable, config);
    store.set({ isUploadingDatabricksParquet: false, qvdDatabricksUpload: result });
  } catch (err) {
    store.set({ isUploadingDatabricksParquet: false, qvdDatabricksUpload: { success: false, errors: err.errors || [err.message], warnings: err.warnings || [] } });
  }
}

async function handlePrecheck() {
  const state = store.get();
  const config = collectConfig();
  const targetTable = document.getElementById('dbx-target-table')?.value || '';
  const executionMode = document.getElementById('dbx-execution-mode')?.value || state.qvdExecutionMode || 'generate_sql_only';
  store.set({ isRunningDatabricksPrecheck: true, qvdDatabricksConfig: config, qvdExecutionMode: executionMode });
  try {
    const result = await api.precheckQvdDatabricksDeployment(state.sessionId, targetTable, executionMode, config);
    store.set({ isRunningDatabricksPrecheck: false, qvdDatabricksPrecheck: result });
  } catch (err) {
    store.set({
      isRunningDatabricksPrecheck: false,
      qvdDatabricksPrecheck: {
        passed: false,
        errors: err.errors || [err.message],
        warnings: err.warnings || [],
      },
    });
  }
}

async function handleExecute() {
  const state = store.get();
  const config = collectConfig();
  const targetTable = document.getElementById('dbx-target-table')?.value || '';
  const executionMode = document.getElementById('dbx-execution-mode')?.value || state.qvdExecutionMode || 'generate_sql_only';
  store.set({ isExecutingDatabricksMigration: true, qvdDatabricksConfig: config, qvdExecutionMode: executionMode });
  try {
    const result = await api.executeQvdDatabricksMigration(state.sessionId, targetTable, executionMode, config);
    store.set({ isExecutingDatabricksMigration: false, qvdDatabricksExecution: result });
  } catch (err) {
    store.set({
      isExecutingDatabricksMigration: false,
      qvdDatabricksExecution: {
        success: false,
        errors: err.errors || [err.message],
        report: err.report || { execution_status: 'failed', errors: err.errors || [err.message] },
        logs: err.logs || [],
      },
    });
  }
}

export function destroyDeployPage() {}
