/**
 * QVF Decoder — Page 3: Regenerated Output
 */
import { store } from '../store.js';
import { highlightSQL } from '../components/editor.js';
import { escapeHtml, markdownToHtml } from '../utils.js';

export function renderOutputPage(container) {
  const state = store.get();
  const structured = state.regeneration || {};
  const sqlOutput = structured.sql || state.regeneratedSql || '';
  const descriptionOutput = structured.description || state.regeneratedText || '';
  const warnings = structured.warnings || [];

  // If no regenerated data, redirect
  if (!sqlOutput && !descriptionOutput) {
    if (state.filename) {
      store.navigate('review');
    } else {
      store.navigate('upload');
    }
    return;
  }

  const sqlHtml = highlightSQL(sqlOutput);
  const descHtml = markdownToHtml(descriptionOutput);

  container.innerHTML = `
    <div class="page" id="output-page" style="flex-direction:column">
      <!-- Main Content: Side by Side -->
      <div style="flex:1;display:flex;overflow:hidden;min-height:0">
        <!-- Left: SQL Output -->
        <div class="output-panel" style="border-right:1px solid var(--border)">
          <div class="output-panel-header">
            <div class="panel-title">
              <span class="panel-title-icon">📄</span>
              Regenerated SQL
            </div>
            <div style="display:flex;align-items:center;gap:8px">
              <span style="font-size:11px;color:var(--text-dim);font-family:var(--font-mono)">
                ${sqlOutput.split('\n').length} lines
              </span>
              ${structured.promptVersion ? `<span class="badge badge-info">${escapeHtml(structured.promptVersion)}</span>` : ''}
              ${structured.model ? `<span class="badge badge-primary">${escapeHtml(structured.model)}</span>` : ''}
              <button class="copy-btn" id="copy-sql-btn">
                📋 Copy
              </button>
            </div>
          </div>
          <div class="output-panel-body">
            ${warnings.length ? `
              <div style="margin:12px 12px 0;padding:12px;border:1px solid var(--warning);border-radius:6px;color:var(--warning);background:rgba(245,158,11,0.08);font-size:12px">
                <strong>Validation notes</strong>
                <ul style="margin:8px 0 0 16px;padding:0">
                  ${warnings.map(w => `<li>${escapeHtml(w)}</li>`).join('')}
                </ul>
              </div>` : ''}
            <pre class="output-sql-content animate-fade-in">${sqlHtml}</pre>
          </div>
        </div>

        <!-- Right: Description Output -->
        <div class="output-panel">
          <div class="output-panel-header">
            <div class="panel-title">
              <span class="panel-title-icon">📝</span>
              Regenerated Description
            </div>
            <button class="copy-btn" id="copy-desc-btn">
              📋 Copy
            </button>
          </div>
          <div class="output-panel-body">
            <div class="description-content animate-fade-in">${descHtml}</div>
          </div>
        </div>
      </div>

      <!-- Footer -->
      <div class="review-footer">
        <button class="btn btn-secondary" id="back-to-review">← Back to Editor</button>
        <div style="display:flex;gap:8px">
          <button class="btn btn-secondary" id="download-package-btn">
            📦 Download Full DBT Package
          </button>
          <button class="btn btn-success" id="open-agent-btn">
            ⚡ Open dbt Agent
          </button>
          <button class="btn btn-primary" id="new-upload-btn">
            🔄 New Upload
          </button>
        </div>
      </div>
    </div>
  `;

  setupOutputButtons(state, sqlOutput, descriptionOutput);
}

function setupOutputButtons(state, sqlOutput, descriptionOutput) {
  // Back to review
  document.getElementById('back-to-review')?.addEventListener('click', () => {
    store.navigate('review');
  });

  // Copy SQL
  document.getElementById('copy-sql-btn')?.addEventListener('click', async () => {
    const btn = document.getElementById('copy-sql-btn');
    try {
      await navigator.clipboard.writeText(sqlOutput);
      btn.classList.add('copied');
      btn.innerHTML = '✅ Copied!';
      setTimeout(() => {
        btn.classList.remove('copied');
        btn.innerHTML = '📋 Copy';
      }, 2000);
    } catch (e) {
      console.error('Copy failed:', e);
    }
  });

  // Copy Description
  document.getElementById('copy-desc-btn')?.addEventListener('click', async () => {
    const btn = document.getElementById('copy-desc-btn');
    try {
      await navigator.clipboard.writeText(descriptionOutput);
      btn.classList.add('copied');
      btn.innerHTML = '✅ Copied!';
      setTimeout(() => {
        btn.classList.remove('copied');
        btn.innerHTML = '📋 Copy';
      }, 2000);
    } catch (e) {
      console.error('Copy failed:', e);
    }
  });

  // Download Package — use a relative URL so it works in any deployment
  document.getElementById('download-package-btn')?.addEventListener('click', () => {
    window.location.href = `/api/download/${state.sessionId}`;
  });

  document.getElementById('open-agent-btn')?.addEventListener('click', () => {
    store.navigate('agent');
  });

  // New upload
  document.getElementById('new-upload-btn')?.addEventListener('click', () => {
    store.reset();
    store.navigate('upload');
  });
}

export function destroyOutputPage() {
  // No components to clean up
}
