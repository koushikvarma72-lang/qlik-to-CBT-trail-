import { escapeHtml } from '../utils.js';

function hasGenerated(values) {
  return Object.values(values || {}).some(value => value?.generated || value?.success || value?.passed);
}

export function qvdStatusItems(state) {
  const business = state.qvdBusinessAnalysis;
  const kpis = state.qvdKpiCatalog;
  const lineage = state.qvdLineageReconciliation;
  return [
    ['Metadata inspected', !!state.qvdInspection],
    ['Business entities generated', !!business?.success],
    ['KPI catalog generated', !!kpis?.success || Number(kpis?.kpi_count || 0) > 0],
    ['Lineage generated', Number(lineage?.lineage_nodes || 0) > 0],
    ['Reconciliation rules generated', Number(lineage?.reconciliation_rule_count || 0) > 0],
    ['Mapping approved', !!state.qvdApprovedMapping?.saved || !!state.qvdApprovedMapping?.approved_mapping_csv],
    ['DDL generated', !!state.qvdDdlGeneration?.generated],
    ['Parquet generated', hasGenerated(state.qvdParquetConversions)],
    ['Parquet validated', hasGenerated(state.qvdParquetValidations)],
    ['Load scripts generated', hasGenerated(state.qvdDatabricksLoadScripts)],
    ['Migration package generated', hasGenerated(state.qvdMigrationPackages)],
  ];
}

export function renderQvdStatusChecklist(state, title = 'QVD Progress') {
  const items = qvdStatusItems(state);
  const complete = items.filter(([, done]) => done).length;
  return `
    <section class="qvd-review-card qvd-status-checklist">
      <div class="qvd-review-toolbar">
        <div>
          <h3 style="margin:0;color:var(--text-primary);font-size:15px">${escapeHtml(title)}</h3>
          <div style="font-size:12px;color:var(--text-dim);margin-top:3px">${complete} of ${items.length} steps complete</div>
        </div>
        <span class="badge ${complete === items.length ? 'badge-success' : 'badge-info'}">${complete}/${items.length}</span>
      </div>
      <div class="qvd-status-checklist-grid">
        ${items.map(([label, done]) => `
          <div class="qvd-status-checklist-item ${done ? 'done' : ''}">
            <span>${done ? '✓' : '○'}</span>
            <strong>${escapeHtml(label)}</strong>
          </div>
        `).join('')}
      </div>
    </section>
  `;
}
