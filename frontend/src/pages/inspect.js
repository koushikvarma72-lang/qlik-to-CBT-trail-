import { store } from '../store.js';
import { renderQvdMappingReview, setupQvdMappingReview } from '../components/qvdMappingReview.js';
import { escapeHtml } from '../utils.js';

let activeFileId = null;
let activeTab = 'tables';

const tabs = [
  ['tables', 'Tables'],
  ['relationships', 'Relationships'],
  ['blocks', 'SQL Blocks'],
  ['script', 'Raw Script'],
  ['metadata', 'Metadata'],
  ['dependencies', 'Dependencies'],
];

export function renderInspectPage(container) {
  const state = store.get();
  if (state.uploadMode === 'qvd') {
    container.innerHTML = renderQvdMappingReview(state);
    setupQvdMappingReview();
    return;
  }

  const files = state.sessionStats?.files || [];

  if (!state.sessionId || files.length === 0) {
    container.innerHTML = `
      <div class="page">
        <div class="empty-state" style="margin:auto">
          <div class="empty-state-title">No QVF Data Loaded</div>
          <div class="empty-state-text">Upload a QVF file first to inspect its extracted data.</div>
          <button class="btn btn-primary" id="inspect-upload-btn">Upload QVF</button>
        </div>
      </div>
    `;
    document.getElementById('inspect-upload-btn')?.addEventListener('click', () => store.navigate('upload'));
    return;
  }

  if (!activeFileId || !files.some(file => file.fileId === activeFileId)) {
    activeFileId = state.currentFileId || state.fileId || files[files.length - 1].fileId;
  }

  const currentFile = files.find(file => file.fileId === activeFileId) || files[0];
  const tableCount = (currentFile.tables || []).length;
  const relationshipCount = (currentFile.associations || []).length;
  const blockCount = (currentFile.sqlSections || []).length;
  const fieldCount = (currentFile.tables || []).reduce((sum, table) => sum + (table.fields || []).length, 0);

  container.innerHTML = `
    <div class="page inspect-page">
      <aside class="inspect-sidebar">
        <div class="sidebar-section-title">QVF Files</div>
        <div class="inspect-file-list">
          ${files.map(file => `
            <button class="inspect-file ${file.fileId === currentFile.fileId ? 'active' : ''}" data-file-id="${escapeAttr(file.fileId)}">
              <span class="inspect-file-name">${escapeHtml(file.filename || 'Unknown file')}</span>
              <span class="inspect-file-meta">${(file.tables || []).length} tables</span>
            </button>
          `).join('')}
        </div>
      </aside>

      <main class="inspect-main">
        <header class="inspect-header">
          <div>
            <div class="inspect-title">${escapeHtml(currentFile.filename || 'QVF file')}</div>
            <div class="inspect-subtitle">Extracted script, tables, associations, metadata, and dependencies</div>
          </div>
          <div class="inspect-metrics">
            ${metric('Tables', tableCount)}
            ${metric('Fields', fieldCount)}
            ${metric('Relationships', relationshipCount)}
            ${metric('Blocks', blockCount)}
          </div>
        </header>

        <div class="inspect-tabs">
          ${tabs.map(([id, label]) => `
            <button class="inspect-tab ${activeTab === id ? 'active' : ''}" data-tab="${id}">${label}</button>
          `).join('')}
        </div>

        <section class="inspect-content">
          ${renderActiveTab(currentFile, state)}
        </section>
      </main>
    </div>
  `;

  document.querySelectorAll('[data-file-id]').forEach(btn => {
    btn.addEventListener('click', () => {
      activeFileId = btn.getAttribute('data-file-id');
      const page = document.getElementById('page-content');
      if (page) renderInspectPage(page);
    });
  });

  document.querySelectorAll('[data-tab]').forEach(btn => {
    btn.addEventListener('click', () => {
      activeTab = btn.getAttribute('data-tab') || 'tables';
      const page = document.getElementById('page-content');
      if (page) renderInspectPage(page);
    });
  });
}

function renderActiveTab(file, state) {
  if (activeTab === 'relationships') return renderRelationships(file);
  if (activeTab === 'blocks') return renderBlocks(file);
  if (activeTab === 'script') return renderScript(file);
  if (activeTab === 'metadata') return renderMetadata(file);
  if (activeTab === 'dependencies') return renderDependencies(file, state);
  return renderTables(file);
}

function renderTables(file) {
  const tables = file.tables || [];
  if (tables.length === 0) return emptyPanel('No tables were extracted from this file.');

  return `
    <div class="inspect-grid">
      ${tables.map(table => `
        <article class="inspect-panel">
          <div class="inspect-panel-header">
            <div>
              <h3>${escapeHtml(table.name || table.id || 'Unnamed table')}</h3>
              <p>${Number(table.rows || 0).toLocaleString()} rows</p>
            </div>
            <span class="inspect-chip">${(table.fields || []).length} fields</span>
          </div>
          ${renderFields(table.fields || [])}
          ${renderTableRows(table)}
        </article>
      `).join('')}
    </div>
  `;
}

function renderFields(fields) {
  if (fields.length === 0) return '<div class="inspect-muted">No field metadata found for this table.</div>';
  return `
    <div class="inspect-table-wrap">
      <table class="inspect-table">
        <thead><tr><th>Field</th><th>Type</th><th>Key</th></tr></thead>
        <tbody>
          ${fields.map(field => `
            <tr>
              <td>${escapeHtml(field.name || '')}</td>
              <td>${escapeHtml(field.type || field.dataType || '-')}</td>
              <td>${field.isKey ? 'Yes' : 'No'}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    </div>
  `;
}

function renderTableRows(table) {
  const rows = table.dataRows || table.previewRows || table.rowsData || [];
  const columns = table.dataColumns || deriveColumns(rows);
  const declaredRowCount = Number(table.dataRowCount || table.rows || rows.length || 0);

  if (!rows.length || !columns.length) {
    return `
      <div class="inspect-data-preview">
        <div class="inspect-preview-title">
          <span>Data Preview</span>
          <strong>${declaredRowCount.toLocaleString()} rows</strong>
        </div>
        <div class="inspect-muted">
          No embedded row data was found for this table. QVF metadata exposes the row count, but the actual values are usually stored in referenced QVD/source files unless the script uses an INLINE load.
        </div>
      </div>
    `;
  }

  return `
    <div class="inspect-data-preview">
      <div class="inspect-preview-title">
        <span>Data Preview</span>
        <strong>
          Showing ${rows.length.toLocaleString()} of ${declaredRowCount.toLocaleString()} rows
          ${table.dataTruncated ? ' (truncated)' : ''}
        </strong>
      </div>
      <div class="inspect-table-wrap inspect-data-wrap">
        <table class="inspect-table inspect-data-table">
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
                ${columns.map(column => `<td>${escapeHtml(row[column] ?? '')}</td>`).join('')}
              </tr>
            `).join('')}
          </tbody>
        </table>
      </div>
    </div>
  `;
}

function deriveColumns(rows) {
  const seen = new Set();
  rows.forEach(row => {
    Object.keys(row || {}).forEach(key => seen.add(key));
  });
  return Array.from(seen);
}

function renderRelationships(file) {
  const relationships = file.associations || [];
  if (relationships.length === 0) return emptyPanel('No relationships were extracted from this file.');

  return `
    <div class="inspect-table-wrap">
      <table class="inspect-table">
        <thead><tr><th>From</th><th>Field</th><th>To</th><th>Field</th><th>Relationship</th></tr></thead>
        <tbody>
          ${relationships.map(rel => `
            <tr>
              <td>${escapeHtml(rel.fromTable || rel.fromTableId || '')}</td>
              <td>${escapeHtml(rel.fromFieldName || rel.sourceField || '')}</td>
              <td>${escapeHtml(rel.toTable || rel.toTableId || '')}</td>
              <td>${escapeHtml(rel.toFieldName || rel.targetField || '')}</td>
              <td>${escapeHtml(rel.relationship || rel.type || '')}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    </div>
  `;
}

function renderBlocks(file) {
  const blocks = file.sqlSections || [];
  if (blocks.length === 0) return emptyPanel('No LOAD or SELECT blocks were detected in this script.');

  return `
    <div class="inspect-stack">
      ${blocks.map(block => `
        <article class="inspect-panel">
          <div class="inspect-panel-header">
            <div>
              <h3>${escapeHtml(block.tableName || 'SQL block')}</h3>
              <p>${escapeHtml(detectBlockOperation(block.sql))}</p>
            </div>
          </div>
          <pre class="inspect-code">${escapeHtml(block.fullBlock || block.sql || '')}</pre>
        </article>
      `).join('')}
    </div>
  `;
}

function renderScript(file) {
  return `<pre class="inspect-code inspect-code-full">${escapeHtml(file.script || 'No script text available.')}</pre>`;
}

function renderMetadata(file) {
  const metadata = file.metadata || {};
  const hasMetadata = Object.keys(metadata).length > 0;
  return `<pre class="inspect-code inspect-code-full">${escapeHtml(hasMetadata ? JSON.stringify(metadata, null, 2) : 'No metadata found.')}</pre>`;
}

function renderDependencies(file, state) {
  const nodes = state.graph?.nodes || [];
  const edges = state.graph?.edges || [];
  const fileNodes = nodes.filter(node => node.fileId === file.fileId);
  const fileNodeIds = new Set(fileNodes.map(node => node.id));
  const dependencies = edges
    .filter(edge => edge.type === 'dependency' && fileNodeIds.has(edge.target))
    .map(edge => {
      const source = nodes.find(node => node.id === edge.source);
      const target = nodes.find(node => node.id === edge.target);
      return { source, target, edge };
    });

  if (dependencies.length === 0) return emptyPanel('No QVF file dependencies were detected for this file.');

  return `
    <div class="inspect-table-wrap">
      <table class="inspect-table">
        <thead><tr><th>Dependency File</th><th>Status</th><th>Feeds Table</th></tr></thead>
        <tbody>
          ${dependencies.map(dep => `
            <tr>
              <td>${escapeHtml(dep.source?.name || dep.edge.source || '')}</td>
              <td>${escapeHtml(dep.source?.status || '')}</td>
              <td>${escapeHtml(dep.target?.name || dep.edge.target || '')}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    </div>
  `;
}

function metric(label, value) {
  return `
    <div class="inspect-metric">
      <span>${escapeHtml(label)}</span>
      <strong>${Number(value || 0).toLocaleString()}</strong>
    </div>
  `;
}

function emptyPanel(message) {
  return `<div class="inspect-empty">${escapeHtml(message)}</div>`;
}

function detectBlockOperation(sql) {
  if (/\bSELECT\b/i.test(sql || '')) return 'SELECT block';
  if (/\bLOAD\b/i.test(sql || '')) return 'LOAD block';
  return 'Script block';
}

function escapeAttr(value) {
  return escapeHtml(value);
}

export function destroyInspectPage() {}
