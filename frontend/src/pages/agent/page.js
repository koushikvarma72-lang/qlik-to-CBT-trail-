import { store } from '../../store.js';
import { agentState } from './state.js';
import { bindFormCache, restoreCachedForm } from './form-mode.js';
import { renderAgentStatus } from './status-mode.js';
import { runAgent, testConnection } from './cloud-mode.js';
import { bindPreviewActions, renderMigrationPreview } from './preview-mode.js';
import { escapeHtml } from '../../utils.js';

export function renderAgentPage(container) {
  const state = store.get();
  const structured = state.regeneration || {};
  const sqlOutput = structured.sql || state.regeneratedSql || '';

  if (!sqlOutput) {
    store.navigate(state.filename ? 'output' : 'upload');
    return;
  }

  const modelName = 'migration_output';
  const defaultCommands = `dbt seed --full-refresh\ndbt run --select ${modelName}\ndbt test --select ${modelName}`;
  const lineCount = sqlOutput.split('\n').length;

  container.innerHTML = `
    <div class="page" id="agent-page" style="flex-direction:column">
      <div style="flex:1;display:grid;grid-template-columns:minmax(340px,420px) minmax(0,1fr);min-height:0;overflow:hidden">
        ${renderCloudMode(defaultCommands)}
        ${renderMigrationPreview({
          lineCount,
          modelName,
          sqlOutput,
          sourceName: state.filename || 'Uploaded Qlik setup',
        })}
      </div>

      <div class="review-footer">
        <button class="btn btn-secondary" id="back-to-output">Back to Output</button>
        <div style="flex:1"></div>
        <button class="btn btn-secondary" id="download-agent-package">Download dbt Package</button>
        <button class="btn btn-primary" id="new-agent-upload">New Upload</button>
      </div>
    </div>
  `;

  restoreCachedForm();
  bindAgentActions(sqlOutput);
}

function renderCloudMode(defaultCommands) {
  return `
    <div class="output-panel" style="border-right:1px solid var(--border);background:var(--bg-primary)">
      <div class="output-panel-header">
        <div class="panel-title">
          <span class="panel-title-icon">A</span>
          dbt Cloud Agent
        </div>
        <span class="badge ${agentState.connected ? 'badge-success' : 'badge-warning'}">
          ${agentState.connected ? 'Connected' : 'Not connected'}
        </span>
      </div>
      <div class="output-panel-body" style="padding:18px;display:flex;flex-direction:column;gap:14px">
        <label class="agent-field">
          <span>dbt Cloud API URL</span>
          <input id="dbt-base-url" value="https://cloud.getdbt.com/api/v2" placeholder="https://cloud.getdbt.com/api/v2">
        </label>
        <label class="agent-field">
          <span>Service token</span>
          <input id="dbt-token" type="password" autocomplete="off" placeholder="Token from dbt Cloud account settings">
        </label>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
          <label class="agent-field">
            <span>Account ID</span>
            <input id="dbt-account-id" inputmode="numeric" placeholder="12345">
          </label>
          <label class="agent-field">
            <span>Project ID</span>
            <input id="dbt-project-id" inputmode="numeric" placeholder="Optional">
          </label>
        </div>
        <label class="agent-field">
          <span>Job ID</span>
          <input id="dbt-job-id" inputmode="numeric" placeholder="Deployment job to run">
        </label>
        <label class="agent-field">
          <span>Commands to run in dbt Cloud</span>
          <textarea id="dbt-commands" rows="5" spellcheck="false">${escapeHtml(defaultCommands)}</textarea>
        </label>
        <div style="display:flex;gap:8px">
          <button class="btn btn-secondary" id="test-dbt-btn" style="flex:1">Test Login</button>
          <button class="btn btn-success" id="run-dbt-btn" style="flex:1">Run Agent</button>
        </div>
        <div class="agent-status ${agentState.error ? 'error' : ''}" id="agent-status">
          ${renderAgentStatus()}
        </div>
      </div>
    </div>
  `;
}

function bindAgentActions(sqlOutput) {
  document.getElementById('back-to-output')?.addEventListener('click', () => store.navigate('output'));
  document.getElementById('new-agent-upload')?.addEventListener('click', () => {
    store.reset();
    store.navigate('upload');
  });
  document.getElementById('download-agent-package')?.addEventListener('click', () => {
    // Use a relative URL so it works in any deployment environment
    window.location.href = `/api/download/${store.get().sessionId}`;
  });
  bindPreviewActions(sqlOutput);
  bindFormCache();
  document.getElementById('test-dbt-btn')?.addEventListener('click', testConnection);
  document.getElementById('run-dbt-btn')?.addEventListener('click', runAgent);
}
