/**
 * QVF Decoder — Page 3: Regenerated Output
 */
import { store } from '../store.js';
import { highlightSQL } from '../components/editor.js';
import {
  renderQvdDatabricksLoadScripts,
  renderQvdMigrationPackage,
  setupQvdHandlers,
} from './upload.js';
import { renderQvdStatusChecklist } from '../components/qvdStatusChecklist.js';
import { escapeHtml, markdownToHtml } from '../utils.js';

let activeQvdOutputSection = 'overview';

export function renderOutputPage(container) {
  const state = store.get();
  if (state.uploadMode === 'qvd') {
    renderQvdOutputPage(container, state);
    return;
  }

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

function renderQvdOutputPage(container, state) {
  const tables = state.qvdInspection?.tables || [];
  const hasLoadScripts = Object.values(state.qvdDatabricksLoadScripts || {}).some(result => result?.generated);
  if (!hasLoadScripts) {
    container.innerHTML = `
      <div class="page">
        <div class="empty-state" style="margin:auto">
          <div class="empty-state-title">Load Scripts Required</div>
          <div class="empty-state-text">Generate Databricks load scripts in the Next Step page before packaging output artifacts.</div>
          <button class="btn btn-primary" id="qvd-output-next-btn">Go To Next Step</button>
        </div>
      </div>
    `;
    document.getElementById('qvd-output-next-btn')?.addEventListener('click', () => store.navigate('review'));
    return;
  }
  if (activeQvdOutputSection !== 'overview' && !tables.some(table => (table.summary?.file_name || '') === activeQvdOutputSection)) {
    activeQvdOutputSection = 'overview';
  }

  container.innerHTML = `
    <div class="page qvd-review-page">
      <main class="qvd-review-main">
        ${renderQvdStatusChecklist(state)}
        <header class="inspect-header">
          <div>
            <div class="inspect-title">QVD Output</div>
            <div class="inspect-subtitle">Review generated Databricks load scripts and create the downloadable migration package.</div>
          </div>
          <div class="inspect-metrics">
            <div class="inspect-metric"><span>Load Scripts</span><strong>${Object.values(state.qvdDatabricksLoadScripts || {}).filter(result => result?.generated).length}</strong></div>
            <div class="inspect-metric"><span>Packages</span><strong>${Object.values(state.qvdMigrationPackages || {}).filter(result => result?.generated).length}</strong></div>
          </div>
        </header>

        <section class="qvd-review-card">
          <div class="qvd-business-shell">
            <nav class="qvd-business-section-nav" aria-label="QVD Output sections">
              <button type="button" class="qvd-business-section-link ${activeQvdOutputSection === 'overview' ? 'active' : ''}" data-qvd-output-section="overview">
                <span>Overview</span>
                <small>Review generated load scripts and package readiness.</small>
              </button>
              ${tables.map(table => {
                const summary = table.summary || {};
                const fileName = summary.file_name || '';
                return `
                  <button type="button" class="qvd-business-section-link ${activeQvdOutputSection === fileName ? 'active' : ''}" data-qvd-output-section="${escapeHtml(fileName)}">
                    <span>${escapeHtml(summary.table_name || fileName || 'QVD Table')}</span>
                    <small>${state.qvdMigrationPackages?.[fileName]?.generated ? 'Package ready' : 'Ready to package'}</small>
                  </button>
                `;
              }).join('')}
            </nav>
            <div class="qvd-business-section-panel">
              ${renderQvdOutputSection(activeQvdOutputSection, tables, state)}
            </div>
          </div>
        </section>
      </main>
    </div>
  `;
  document.querySelectorAll('[data-qvd-output-section]').forEach(button => {
    button.addEventListener('click', () => {
      activeQvdOutputSection = button.dataset.qvdOutputSection || 'overview';
      renderQvdOutputPage(container, store.get());
    });
  });
  setupQvdHandlers();
}

function renderQvdOutputSection(active, tables, state) {
  if (active === 'overview') {
    const loadCount = Object.values(state.qvdDatabricksLoadScripts || {}).filter(result => result?.generated).length;
    const packageCount = Object.values(state.qvdMigrationPackages || {}).filter(result => result?.generated).length;
    return `
      <div class="qvd-business-section-header">
        <div>
          <h3>Overview</h3>
          <p>Review generated load scripts and package readiness.</p>
        </div>
      </div>
      <div class="qvd-profile-summary">
        <div class="qvd-profile-card"><span>QVD Tables</span><strong>${tables.length}</strong></div>
        <div class="qvd-profile-card"><span>Load Scripts</span><strong>${loadCount}</strong></div>
        <div class="qvd-profile-card"><span>Packages</span><strong>${packageCount}</strong></div>
      </div>
      <div class="inspect-empty">Select a QVD table from the left to review load scripts and generate or download its migration package.</div>
    `;
  }
  const table = tables.find(item => (item.summary?.file_name || '') === active) || tables[0];
  if (!table) return '<div class="inspect-empty">No QVD table selected.</div>';
  const summary = table.summary || {};
  const fileName = summary.file_name || '';
  return `
    <section class="qvd-metadata-card" style="border:1px solid var(--border);background:rgba(255,255,255,0.76);border-radius:8px">
      <div style="padding:14px 16px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;gap:12px;align-items:flex-start">
        <div>
          <h3 style="margin:0 0 4px;color:var(--text-primary);font-size:16px">${escapeHtml(summary.table_name || fileName)}</h3>
          <div style="font-size:12px;color:var(--text-dim)">${escapeHtml(fileName)}</div>
        </div>
        <span class="badge ${state.qvdMigrationPackages?.[fileName]?.generated ? 'badge-success' : 'badge-info'}">
          ${state.qvdMigrationPackages?.[fileName]?.generated ? 'Package ready' : 'Ready to package'}
        </span>
      </div>
      <div style="padding:14px 16px">
        ${renderQvdDatabricksLoadScripts(fileName, state)}
        ${renderQvdMigrationPackage(fileName, state)}
      </div>
    </section>
  `;
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
