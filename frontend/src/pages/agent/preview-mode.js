import { escapeHtml } from '../../utils.js';

export function renderMigrationPreview({ lineCount, modelName, sqlOutput, sourceName }) {
  return `
    <div class="output-panel">
      <div class="output-panel-header">
        <div class="panel-title">
          <span class="panel-title-icon">SQL</span>
          Migration Setup Preview
        </div>
        <div style="display:flex;gap:8px;align-items:center">
          <span class="badge badge-info">${lineCount} SQL lines</span>
          <button class="copy-btn" id="copy-agent-sql">Copy SQL</button>
        </div>
      </div>
      <div class="output-panel-body" style="display:grid;grid-template-rows:auto 1fr;min-height:0">
        <div style="padding:14px 18px;border-bottom:1px solid var(--border);display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px">
          <div class="agent-mini-card">
            <strong>Model</strong>
            <span>models/${modelName}.sql</span>
          </div>
          <div class="agent-mini-card">
            <strong>Schema</strong>
            <span>models/schema.yml</span>
          </div>
          <div class="agent-mini-card">
            <strong>Source</strong>
            <span>${escapeHtml(sourceName)}</span>
          </div>
        </div>
        <pre class="output-sql-content animate-fade-in" style="margin:0">${escapeHtml(sqlOutput)}</pre>
      </div>
    </div>
  `;
}

export function bindPreviewActions(sqlOutput) {
  document.getElementById('copy-agent-sql')?.addEventListener('click', async () => {
    await navigator.clipboard.writeText(sqlOutput);
  });
}
