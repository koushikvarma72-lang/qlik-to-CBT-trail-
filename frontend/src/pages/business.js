import { api, apiDownloadUrl } from '../api.js';
import { store } from '../store.js';
import { LineageGraph } from '../components/LineageGraph.js';
import { renderQvdStatusChecklist } from '../components/qvdStatusChecklist.js';
import { escapeHtml } from '../utils.js';

let lineageGraph = null;
let activeBusinessSection = 'overview';
const lineageUiState = {
  selectedKpi: '',
  showFull: false,
  enabledTypes: new Set(['source', 'bronze', 'silver', 'gold', 'dimension', 'kpi', 'hierarchy']),
  selectedNode: null,
};

const KPI_THEMES = [
  ['Sales / revenue metrics', ['sales', 'revenue', 'amount', 'price', 'value']],
  ['Budget / planning metrics', ['budget', 'plan', 'business plan', 'busplan']],
  ['Forecast metrics', ['forecast', 'latest forecast']],
  ['Growth / variance metrics', ['growth', 'variance', 'difference', 'margin', 'yoy']],
  ['Operational metrics', ['ops', 'units', 'quantity', 'count', 'cost']],
  ['Flag / indicator metrics', ['flag', 'indicator', 'active', 'enabled']],
];

const LINEAGE_TYPES = [
  ['Source/Data layers', ['source', 'bronze', 'silver', 'gold']],
  ['Dimensions', ['dimension']],
  ['Measures', ['measure']],
  ['KPIs', ['kpi']],
  ['Hierarchies', ['hierarchy']],
];

const BUSINESS_SECTIONS = [
  ['overview', 'Overview', 'Understand what this QVD contains and how migration-ready it is.'],
  ['entities', 'Business Entities', 'See detected dimensions, measures, dates, flags, and hierarchies.'],
  ['metrics', 'Metrics / KPIs', 'Review business metrics and recommended calculations.'],
  ['lineage', 'Lineage Graph', 'Trace where each metric comes from and how it flows into Databricks.'],
  ['validation', 'Validation Plan', 'See how Qlik and Databricks results will be compared after migration.'],
  ['glossary', 'Business Glossary', 'Business-friendly definitions for detected fields and KPIs.'],
  ['ai', 'AI Explanation', 'Optional AI-generated plain-English explanation for business users.'],
  ['artifacts', 'Artifacts', 'Download generated JSON, CSV, Markdown, and lineage files.'],
];

const LEGACY_AI_NARRATIVE_KEY = ['de', 'mo_narrative'].join('');
const LEGACY_AI_NARRATIVE_ARTIFACT_KEY = ['ai_de', 'mo_narrative_md'].join('');

function artifactHref(path) {
  if (!path) return '#';
  if (typeof path === 'object' && path.download_url) {
    return apiDownloadUrl(path.download_url);
  }
  const rawPath = String(path);
  if (rawPath.startsWith('/api')) {
    return apiDownloadUrl(rawPath);
  }
  if (rawPath.startsWith('qvd_outputs/') || rawPath.startsWith('uploads/') || rawPath.startsWith('migration_packages/') || rawPath.startsWith('generated_artifacts/')) {
    return apiDownloadUrl(`/api/files/${rawPath.split('/').map(encodeURIComponent).join('/')}`);
  }
  const marker = '/qvd_outputs/';
  const index = rawPath.indexOf(marker);
  const relative = index >= 0 ? rawPath.slice(index + marker.length) : rawPath;
  const sessionId = store.get().qvdInspection?.session_id || store.get().sessionId || '';
  return apiDownloadUrl(`/api/qvd/download-artifact/${encodeURIComponent(sessionId)}/${relative.split('/').map(encodeURIComponent).join('/')}`);
}

function artifactLink(label, path) {
  if (!path) return '';
  return `<a class="qvd-artifact-link" href="${artifactHref(path)}">${escapeHtml(label)}</a>`;
}

export function renderBusinessPage(container) {
  const state = store.get();
  if (state.uploadMode !== 'qvd') {
    store.navigate('upload');
    return;
  }

  if (!state.qvdInspection) {
    container.innerHTML = `
      <div class="page">
        <div class="empty-state" style="margin:auto">
          <div class="empty-state-title">QVD Upload Required</div>
          <div class="empty-state-text">Upload and inspect QVD metadata before running business analysis.</div>
          <button class="btn btn-primary" id="business-upload-btn">Go To QVD Upload</button>
        </div>
      </div>
    `;
    document.getElementById('business-upload-btn')?.addEventListener('click', () => store.navigate('upload'));
    return;
  }

  const analysis = state.qvdBusinessAnalysis;
  const kpiCatalog = state.qvdKpiCatalog;
  const lineageReconciliation = state.qvdLineageReconciliation;
  const aiExplanation = state.qvdAiExplanation;
  const firstTable = state.qvdInspection.tables?.[0]?.summary || {};
  container.innerHTML = `
    <div class="page qvd-review-page">
      <main class="qvd-review-main">
        ${renderQvdStatusChecklist(state)}
        <section class="qvd-review-card">
          <div class="qvd-review-toolbar">
            <div>
              <h2 style="margin:0 0 4px;color:var(--text-primary);font-size:20px">Business Analysis Accelerator</h2>
              <div style="font-size:12px;color:var(--text-dim)">
                Session: <code>${escapeHtml(state.qvdInspection.session_id || state.sessionId || '')}</code>
                ${firstTable.file_name ? ` · ${escapeHtml(firstTable.table_name || firstTable.file_name)}` : ''}
              </div>
            </div>
          </div>
          <div style="display:flex;gap:8px;flex-wrap:wrap">
            <span class="badge badge-info">${state.qvdInspection.tables?.length || 0} QVD table${(state.qvdInspection.tables?.length || 0) === 1 ? '' : 's'}</span>
            <span class="badge badge-primary">${state.qvdInspection.uploaded_files?.length || 0} uploaded file${(state.qvdInspection.uploaded_files?.length || 0) === 1 ? '' : 's'}</span>
            ${analysis?.artifacts?.business_entities_json ? `<span class="badge badge-success">business_entities.json created</span>` : ''}
          </div>
        </section>

        ${renderBusinessAnalysisResults(analysis, kpiCatalog, lineageReconciliation, aiExplanation, state)}
      </main>
    </div>
  `;
  setupBusinessHandlers();
}

function renderBusinessEmptyState() {
  return `
    <div class="empty-state" style="padding:36px 12px">
      <div class="empty-state-title">No Business Entities Discovered Yet</div>
      <div class="empty-state-text">Run discovery from the Business Entities section to classify dimensions, measures, dates, flags, identifiers, and candidate hierarchies.</div>
    </div>
  `;
}

function renderBusinessAnalysisResults(analysis, kpiCatalog, lineageReconciliation, aiExplanation, state) {
  const active = BUSINESS_SECTIONS.some(([id]) => id === activeBusinessSection) ? activeBusinessSection : 'overview';
  return `
    <section class="qvd-review-card" style="margin-top:16px">
      ${renderBusinessAnalysisStatusStrip(analysis, kpiCatalog, lineageReconciliation, aiExplanation)}
      <div class="qvd-business-shell">
        ${renderBusinessAnalysisSectionNav(active)}
        <div class="qvd-business-section-panel">
          ${renderBusinessSection(active, analysis, kpiCatalog, lineageReconciliation, aiExplanation, state)}
        </div>
      </div>
      <div style="display:flex;justify-content:flex-end;margin-top:14px">
        <button class="btn btn-primary" id="continue-inspect-mapping-btn">Continue To Inspect Mapping</button>
      </div>
    </section>
  `;
}

function renderBusinessAnalysisSectionNav(active) {
  return `
    <nav class="qvd-business-section-nav" aria-label="Business Analysis sections">
      ${BUSINESS_SECTIONS.map(([id, label, helper]) => `
        <button
          type="button"
          class="qvd-business-section-link ${id === active ? 'active' : ''}"
          data-business-section="${escapeHtml(id)}"
          aria-current="${id === active ? 'page' : 'false'}"
        >
          <span>${escapeHtml(label)}</span>
          <small>${escapeHtml(helper)}</small>
        </button>
      `).join('')}
    </nav>
  `;
}

function renderBusinessAnalysisStatusStrip(analysis, kpiCatalog, lineageReconciliation, aiExplanation) {
  const reconciliation = lineageReconciliation?.reconciliation || {};
  const glossary = lineageReconciliation?.business_glossary || {};
  const statuses = [
    ['Entities', statusFor(analysis, !!analysis?.artifacts?.business_entities_json)],
    ['KPIs', statusFor(kpiCatalog, !!kpiCatalog?.kpi_count || !!kpiCatalog?.artifacts?.kpi_catalog_json)],
    ['Lineage', statusFor(lineageReconciliation, !!lineageReconciliation?.lineage?.nodes?.length)],
    ['Reconciliation', statusFor(lineageReconciliation, !!reconciliation.rule_count || !!lineageReconciliation?.reconciliation_rule_count)],
    ['Glossary', statusFor(lineageReconciliation, !!glossary.glossary_count || !!glossary.terms?.length)],
    ['AI', statusFor(aiExplanation, !!aiExplanation?.summary_markdown)],
  ];
  return `
    <div class="qvd-business-status-strip">
      ${statuses.map(([label, status]) => `
        <div class="qvd-business-status ${status.kind}">
          <span>${escapeHtml(label)}</span>
          <strong>${escapeHtml(status.label)}</strong>
        </div>
      `).join('')}
    </div>
  `;
}

function statusFor(payload, generated) {
  if (payload?.success === false || (payload?.errors || []).length) return { kind: 'failed', label: 'Failed' };
  if (generated || payload?.success === true) return { kind: 'generated', label: 'Generated' };
  return { kind: 'pending', label: 'Not generated' };
}

function renderBusinessSection(active, analysis, kpiCatalog, lineageReconciliation, aiExplanation, state) {
  switch (active) {
    case 'entities':
      return renderBusinessEntitiesSection(analysis, state);
    case 'metrics':
      return renderMetricsSection(kpiCatalog, state, !!analysis);
    case 'lineage':
      return renderLineageSection(lineageReconciliation, state, !!kpiCatalog);
    case 'validation':
      return renderValidationSection(lineageReconciliation, state, !!kpiCatalog);
    case 'glossary':
      return renderGlossarySection(lineageReconciliation);
    case 'ai':
      return renderAiExplanationSection(aiExplanation, state, !!kpiCatalog);
    case 'artifacts':
      return renderArtifactsSection(analysis, kpiCatalog, lineageReconciliation, aiExplanation);
    case 'overview':
    default:
      return renderOverviewSection(analysis, kpiCatalog, lineageReconciliation);
  }
}

function renderSectionHeader(title, helper, action = '') {
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

function renderOverviewSection(analysis, kpiCatalog, lineageReconciliation) {
  if (!analysis) {
    return `
      ${renderSectionHeader('Overview', 'Understand what this QVD contains and how migration-ready it is.')}
      ${renderBusinessEmptyState()}
    `;
  }
  const executiveSummary = lineageReconciliation?.executive_summary || {};
  return `
    ${renderSectionHeader('Overview', 'Understand what this QVD contains and how migration-ready it is.')}
    ${renderLearnedSummary(analysis, kpiCatalog, lineageReconciliation, executiveSummary)}
    ${renderExecutiveSummary(executiveSummary, lineageReconciliation || {}) || `<div class="inspect-empty">Generate lineage and reconciliation to see the recommended Databricks migration design.</div>`}
  `;
}

function renderBusinessEntitiesSection(analysis, state) {
  const action = `
    <button class="btn btn-primary" id="run-business-analysis-btn" ${state.isDiscoveringQvdBusinessEntities ? 'disabled' : ''}>
      ${state.isDiscoveringQvdBusinessEntities ? 'Analyzing...' : 'Run Business Entity Discovery'}
    </button>
  `;
  if (!analysis) {
    return `
      ${renderSectionHeader('Business Entities', 'See detected dimensions, measures, dates, flags, and hierarchies.', action)}
      <div class="inspect-empty">Run discovery to classify dimensions, measures, dates, flags, identifiers, and candidate hierarchies.</div>
    `;
  }
  return `
    ${renderSectionHeader('Business Entities', 'See detected dimensions, measures, dates, flags, and hierarchies.', action)}
    ${renderAnalyzeBy(analysis)}
    ${renderEntitySection('Dimensions', analysis.dimensions || [])}
    ${renderEntitySection('Measures', analysis.measures || [])}
    ${renderEntitySection('Dates', analysis.dates || [])}
    ${renderEntitySection('Flags', analysis.flags || [])}
    ${renderEntitySection('Identifiers', analysis.identifiers || [])}
    ${renderEntitySection('Hierarchies', analysis.hierarchies || [], true)}
  `;
}

function renderMetricsSection(kpiCatalog, state, hasEntities) {
  const action = `
    <button class="btn btn-secondary" id="generate-kpi-catalog-btn" ${!hasEntities || state.isGeneratingQvdKpiCatalog ? 'disabled' : ''}>
      ${state.isGeneratingQvdKpiCatalog ? 'Generating...' : 'Generate KPI Catalog & Documentation'}
    </button>
  `;
  return `
    ${renderSectionHeader('Metrics / KPIs', 'Review business metrics and recommended calculations.', action)}
    ${kpiCatalog ? renderKpiCatalog(kpiCatalog) : `<div class="inspect-empty">KPI recommendations are not generated yet. Run this step to see what the QVD can measure.</div>`}
  `;
}

function renderLineageSection(lineageReconciliation, state, hasKpis) {
  const action = renderLineageAction(state, hasKpis);
  return `
    ${renderSectionHeader('Lineage Graph', 'Trace where each metric comes from and how it flows into Databricks.', action)}
    ${lineageReconciliation ? renderLineageGraphSection(lineageReconciliation) : `<div class="inspect-empty">Generate lineage and reconciliation rules to view the metric flow graph.</div>`}
  `;
}

function renderValidationSection(lineageReconciliation, state, hasKpis) {
  const action = renderLineageAction(state, hasKpis);
  return `
    ${renderSectionHeader('Validation Plan', 'See how Qlik and Databricks results will be compared after migration.', action)}
    ${lineageReconciliation ? renderValidationPlan(lineageReconciliation) : `<div class="inspect-empty">Generate reconciliation rules to see the migration validation plan.</div>`}
  `;
}

function renderLineageAction(state, hasKpis) {
  return `
    <button class="btn btn-secondary" id="generate-lineage-reconciliation-btn" ${!hasKpis || state.isGeneratingQvdLineageReconciliation ? 'disabled' : ''}>
      ${state.isGeneratingQvdLineageReconciliation ? 'Generating...' : 'Generate Lineage & Reconciliation Rules'}
    </button>
  `;
}

function renderGlossarySection(lineageReconciliation) {
  const terms = lineageReconciliation?.business_glossary?.terms || [];
  return `
    ${renderSectionHeader('Business Glossary', 'Business-friendly definitions for detected fields and KPIs.')}
    ${terms.length ? renderBusinessGlossary(terms, { expanded: true }) : `<div class="inspect-empty">Generate lineage and reconciliation outputs to create the business glossary.</div>`}
  `;
}

function renderArtifactsSection(analysis, kpiCatalog, lineageReconciliation, aiExplanation) {
  return `
    ${renderSectionHeader('Artifacts', 'Download generated JSON, CSV, Markdown, and lineage files.')}
    ${renderAdvancedArtifacts(analysis, kpiCatalog, lineageReconciliation, aiExplanation, { expanded: true })}
  `;
}

function renderLearnedSummary(analysis, kpiCatalog, lineageReconciliation, executiveSummary) {
  const summary = analysis.summary || {};
  const overview = executiveSummary.source_system_overview || {};
  const businessAreas = [...new Set([
    ...(analysis.dimensions || []).slice(0, 6).map(row => row.business_name || row.source_column),
    ...(analysis.hierarchies || []).slice(0, 3).map(row => row.business_name || row.source_column),
  ].filter(Boolean))];
  const purpose = inferPurpose(kpiCatalog, businessAreas);
  return `
    <div class="qvd-ddl-result">
      <div class="qvd-ddl-header">
        <div>
          <h3>What did we learn from this QVD?</h3>
          <p>${escapeHtml(purpose)}</p>
        </div>
        <span class="badge ${Number(executiveSummary.migration_readiness_score || 0) >= 80 ? 'badge-success' : 'badge-info'}">
          ${Number(executiveSummary.migration_readiness_score || 0)}% migration readiness
        </span>
      </div>
      <div class="qvd-profile-summary">
        ${summaryCard('Records', overview.record_count || 0)}
        ${summaryCard('Fields', overview.field_count || 0)}
        ${summaryCard('Metrics', kpiCatalog?.kpi_count || summary.measures_count)}
        ${summaryCard('Validation checks', lineageReconciliation?.reconciliation_rule_count || 0)}
      </div>
      <div class="qvd-parquet-messages" style="margin-top:10px">
        <strong>Business areas</strong>
        <div>${businessAreas.length ? businessAreas.join(', ') : 'Generate KPI and lineage outputs to summarize business areas.'}</div>
      </div>
    </div>
  `;
}

function inferPurpose(kpiCatalog, businessAreas) {
  const text = (kpiCatalog?.kpis || []).map(kpi => `${kpi.kpi_name} ${kpi.recommended_formula}`).join(' ').toLowerCase();
  const themes = [];
  if (/sales|revenue|amount|price|value/.test(text)) themes.push('commercial performance');
  if (/budget|forecast|plan/.test(text)) themes.push('planning and forecast tracking');
  if (/ops|units|quantity|count/.test(text)) themes.push('operational performance');
  const areaText = businessAreas.length ? ` across ${businessAreas.slice(0, 5).join(', ')}` : '';
  return `This QVD appears to support ${themes.join(', ') || 'business performance'} reporting${areaText}.`;
}

function formatHierarchy(row) {
  const levels = (row.levels || []).map(level => level.business_name || level.source_column).filter(Boolean);
  if (levels.length) return levels.join(' > ');
  return row.business_name || row.source_column || '';
}

function renderAnalyzeBy(analysis) {
  const dimensions = analysis.dimensions || [];
  const hierarchies = analysis.hierarchies || [];
  return `
    <div class="qvd-ddl-result">
      <div class="qvd-ddl-header">
        <div>
          <h3>What can we analyze by?</h3>
          <p>Business dimensions and hierarchy paths inferred from the QVD metadata.</p>
        </div>
      </div>
      <div class="qvd-business-chip-grid">
        ${dimensions.slice(0, 16).map(row => `<span class="qvd-business-chip">${escapeHtml(row.business_name || row.source_column || '')}</span>`).join('')}
        ${hierarchies.map(row => `<span class="qvd-business-chip hierarchy">${escapeHtml(formatHierarchy(row))}</span>`).join('')}
      </div>
      ${!dimensions.length && !hierarchies.length ? `<div class="inspect-empty">No dimensions or hierarchy candidates generated yet.</div>` : ''}
    </div>
  `;
}

function renderExecutiveSummary(summary, payload) {
  if (!summary || !Object.keys(summary).length) return '';
  const overview = summary.source_system_overview || {};
  return `
    <div class="qvd-ddl-result">
      <div class="qvd-ddl-header">
        <div>
          <h3>Recommended Databricks migration design</h3>
          <p>${escapeHtml(overview.source_system || 'QVD')} overview and migration readiness.</p>
        </div>
        <span class="badge ${Number(summary.migration_readiness_score || 0) >= 80 ? 'badge-success' : 'badge-warning'}">
          ${Number(summary.migration_readiness_score || payload.migration_readiness_score || 0)}% ready
        </span>
      </div>
      <div class="qvd-profile-summary">
        ${summaryCard('KPI Count', summary.kpi_count)}
        ${summaryCard('Dimensions', summary.dimension_count)}
        ${summaryCard('Hierarchies', summary.hierarchy_count)}
        ${summaryCard('Reconciliations', summary.reconciliation_count)}
      </div>
      <div class="qvd-parquet-messages" style="margin-top:10px">
        <strong>Source System Overview</strong>
        <div>Source file: ${escapeHtml(overview.source_file || '')}</div>
        <div>Source table: ${escapeHtml(overview.source_table || '')}</div>
        <div>Records: ${escapeHtml(overview.record_count || '')}</div>
        <div>Fields: ${escapeHtml(String(overview.field_count || 0))}</div>
        ${(summary.readiness_reasons || []).map(reason => `<div>${escapeHtml(reason)}</div>`).join('')}
      </div>
    </div>
  `;
}

function renderBusinessGlossary(terms, options = {}) {
  if (!terms.length) return '';
  return `
    <div class="qvd-ddl-result">
      <div class="qvd-ddl-header">
        <div>
          <h3>Business Glossary</h3>
          <p>${terms.length} terms generated from KPI and entity metadata.</p>
        </div>
      </div>
      <details class="qvd-details" ${options.expanded ? 'open' : ''}>
        <summary>Show business terms</summary>
        <div class="qvd-profile-table-wrapper">
        <table class="qvd-profile-table">
          <thead>
            <tr>
              <th>Type</th>
              <th>Name</th>
              <th>Business Definition</th>
              <th>Dimensions Used</th>
              <th>Date Grain</th>
              <th>Owner</th>
            </tr>
          </thead>
          <tbody>
            ${terms.map(term => `
              <tr>
                <td><code>${escapeHtml(term.term_type || '')}</code></td>
                <td>${escapeHtml(term.name || '')}</td>
                <td>${escapeHtml(term.business_definition || '')}</td>
                <td>${escapeHtml((term.dimensions_used || []).join(', '))}</td>
                <td>${escapeHtml(term.date_grain || '')}</td>
                <td>${escapeHtml(term.owner || '')}</td>
              </tr>
            `).join('')}
          </tbody>
        </table>
        </div>
      </details>
    </div>
  `;
}

function renderKpiCatalog(catalog) {
  const kpis = catalog.kpis || [];
  const groups = groupKpisByTheme(kpis);
  return `
    <div class="qvd-profile-summary">
      ${summaryCard('Measurable metrics', catalog.kpi_count || kpis.length)}
      ${summaryCard('Additive metrics', kpis.filter(kpi => kpi.aggregation_type === 'SUM').length)}
      ${summaryCard('Average metrics', kpis.filter(kpi => kpi.aggregation_type === 'AVG').length)}
      ${summaryCard('Flag metrics', kpis.filter(kpi => kpi.aggregation_type === 'COUNT').length)}
    </div>
    ${kpis.length ? groups.map(group => renderKpiThemeGroup(group)).join('') : `<div class="inspect-empty">No metric candidates generated.</div>`}
  `;
}

function renderKpiThemeGroup(group) {
  return `
    <div class="qvd-metric-theme">
      <div class="qvd-metric-theme-title">
        <h4>${escapeHtml(group.theme)}</h4>
        <span class="badge badge-info">${group.kpis.length}</span>
      </div>
      <div class="qvd-metric-grid">
        ${group.kpis.map(kpi => `
          <article class="qvd-metric-card">
            <div class="qvd-metric-name">${escapeHtml(kpi.kpi_name || 'Metric')}</div>
            <div class="qvd-metric-description">${escapeHtml(kpi.business_description || '')}</div>
            <dl>
              <div><dt>Source</dt><dd>${escapeHtml((kpi.source_columns || []).join(', ') || 'Source column pending review')}</dd></div>
              <div><dt>Calculation</dt><dd><code>${escapeHtml(kpi.recommended_formula || '')}</code></dd></div>
              <div><dt>Analyze by</dt><dd>${escapeHtml((kpi.dimensions || []).slice(0, 6).join(', ') || 'Available dimensions')}</dd></div>
              <div><dt>Date</dt><dd>${escapeHtml(kpi.date_column || kpi.grain || 'No date field detected')}</dd></div>
            </dl>
          </article>
        `).join('')}
      </div>
    </div>
  `;
}

function groupKpisByTheme(kpis) {
  const groups = KPI_THEMES.map(([theme]) => ({ theme, kpis: [] }));
  const other = { theme: 'Other metrics', kpis: [] };
  (kpis || []).forEach(kpi => {
    const text = `${kpi.kpi_name || ''} ${kpi.business_description || ''} ${kpi.recommended_formula || ''}`.toLowerCase();
    const index = KPI_THEMES.findIndex(([, tokens]) => tokens.some(token => text.includes(token)));
    (index >= 0 ? groups[index] : other).kpis.push(kpi);
  });
  return [...groups, other].filter(group => group.kpis.length);
}

function renderLineageGraphSection(payload) {
  const nodes = payload.lineage?.nodes || [];
  const edges = payload.lineage?.edges || [];
  const kpiNodes = nodes.filter(node => node.type === 'kpi');
  const selectedNode = lineageUiState.selectedNode;
  return `
    <div class="qvd-ddl-result">
      <div class="qvd-ddl-header">
        <div>
          <h3>Where does each metric come from?</h3>
          <p>Default view keeps the graph readable; use the controls to trace a metric or reveal full technical lineage.</p>
        </div>
        <span class="badge badge-info">${nodes.length} nodes / ${edges.length} relationships</span>
      </div>
      <div class="qvd-lineage-controls">
        <label>
          Select metric to trace
          <select id="qvd-lineage-kpi-select" class="qvd-mapping-select">
            <option value="" ${lineageUiState.selectedKpi ? '' : 'selected'}>Top metrics overview</option>
            ${kpiNodes.map(node => `<option value="${escapeHtml(node.id)}" ${node.id === lineageUiState.selectedKpi ? 'selected' : ''}>${escapeHtml(node.label || node.id)}</option>`).join('')}
          </select>
        </label>
        ${LINEAGE_TYPES.map(([label, types], index) => `
          <label class="qvd-toggle">
            <input type="checkbox" class="qvd-lineage-type-toggle" data-types="${escapeHtml(types.join(','))}" ${types.every(type => lineageUiState.enabledTypes.has(type)) ? 'checked' : ''}>
            ${escapeHtml(label)}
          </label>
        `).join('')}
        <button class="btn btn-secondary" id="qvd-lineage-fit-btn">Fit View</button>
        <button class="btn btn-secondary" id="qvd-lineage-full-btn" data-full="${lineageUiState.showFull ? 'true' : 'false'}">${lineageUiState.showFull ? 'Show Summary Lineage' : 'Show Full Technical Lineage'}</button>
      </div>
      <div class="qvd-lineage-layout">
        <div id="qvd-lineage-graph" class="qvd-lineage-graph"></div>
        <aside id="qvd-lineage-detail" class="qvd-lineage-detail">
          ${selectedNode ? renderLineageNodeDetail(selectedNode) : `
            <strong>Metric lineage detail</strong>
            <p>Click a node to see source, layer, formula, or field details.</p>
          `}
        </aside>
      </div>
    </div>
  `;
}

function renderValidationPlan(payload) {
  const reconciliation = payload.reconciliation || {};
  const groups = reconciliation.groups || groupRulesByMetric(reconciliation.rules || []);
  const rules = reconciliation.rules || [];
  const markdownPath = payload.artifacts?.reconciliation_rules_md;
  const tolerance = rules.find(rule => rule.tolerance)?.tolerance || '0.01%';
  return `
    <div class="qvd-ddl-result">
      <div class="qvd-ddl-header">
        <div>
          <h3>How do we verify migration success?</h3>
          <p>Migration validation plan generated from the metric catalog and approved Databricks mappings.</p>
        </div>
        ${artifactLink('Download reconciliation_rules.md', markdownPath)}
      </div>
      <div class="qvd-profile-summary">
        ${summaryCard('KPI checks', reconciliation.kpi_check_count || groups.length)}
        ${summaryCard('Dimension checks', reconciliation.dimension_check_count || 0)}
        ${summaryCard('Date/month checks', reconciliation.date_check_count || 0)}
        ${summaryCard('Generated SQL', reconciliation.rule_count || rules.length)}
      </div>
      <div class="qvd-validation-tolerance">Tolerance: ${escapeHtml(tolerance)}</div>
      ${groups.length ? `
        <div class="qvd-validation-grid">
          ${groups.map(group => renderValidationMetricGroup(group)).join('')}
        </div>
      ` : `<div class="inspect-empty">No validation checks generated.</div>`}
    </div>
  `;
}

function renderValidationMetricGroup(group) {
  const rules = group.rules || [];
  const firstRule = rules[0] || {};
  const levels = [
    group.has_total_check ? 'total' : '',
    group.date_check_count ? 'by month' : '',
    group.dimension_check_count ? 'by dimension' : '',
  ].filter(Boolean);
  return `
    <article class="qvd-validation-card">
      <div class="qvd-validation-card-header">
        <h4>${escapeHtml(group.metric_name || 'Metric')}</h4>
        <span class="badge badge-primary">${group.total_checks || rules.length} checks</span>
      </div>
      <dl>
        <div><dt>Qlik</dt><dd><code>${escapeHtml(firstRule.qlik_metric || firstRule.qlik_expression_placeholder || '')}</code></dd></div>
        <div><dt>Databricks</dt><dd><code>${escapeHtml(firstRule.databricks_sql || '')}</code></dd></div>
        <div><dt>Expected</dt><dd>${escapeHtml(firstRule.expected_result || 'Values must match within tolerance.')}</dd></div>
        <div><dt>Check levels</dt><dd>${escapeHtml(levels.join(', ') || 'total')}</dd></div>
        <div><dt>Tolerance</dt><dd>${escapeHtml(group.tolerance || firstRule.tolerance || '')}</dd></div>
      </dl>
      <details class="qvd-details compact">
        <summary>Show SQL</summary>
        ${(rules || []).map(rule => `
          <div class="qvd-sql-block">
            <strong>${escapeHtml(rule.rule_name || '')}</strong>
            <pre class="inspect-code">${escapeHtml(rule.databricks_sql || '')}</pre>
          </div>
        `).join('')}
      </details>
    </article>
  `;
}

function groupRulesByMetric(rules) {
  const byMetric = new Map();
  (rules || []).forEach(rule => {
    const name = rule.metric_name || rule.rule_name || 'Metric';
    if (!byMetric.has(name)) {
      byMetric.set(name, {
        metric_name: name,
        rules: [],
        total_checks: 0,
        has_total_check: false,
        date_check_count: 0,
        dimension_check_count: 0,
        tolerance: rule.tolerance || '',
      });
    }
    const group = byMetric.get(name);
    group.rules.push(rule);
    group.total_checks += 1;
    if (!(rule.dimensions || []).length) group.has_total_check = true;
    else if (rule.date_grain === 'month') group.date_check_count += 1;
    else group.dimension_check_count += 1;
  });
  return [...byMetric.values()];
}

function renderAiExplanationSection(aiExplanation, state, canRun) {
  const displayExplanation = normalizeAiExplanationForDisplay(aiExplanation);
  return `
    <div class="qvd-ddl-result">
      <div class="qvd-ddl-header">
        <div>
          <h3>Optional business explanation</h3>
          <p>Uses OpenRouter when configured; otherwise it writes deterministic plain-English artifacts.</p>
        </div>
        <button class="btn btn-secondary" id="generate-ai-explanation-btn" ${!canRun || state.isGeneratingQvdAiExplanation ? 'disabled' : ''}>
          ${state.isGeneratingQvdAiExplanation ? 'Explaining...' : 'Generate Business Explanation'}
        </button>
      </div>
      ${displayExplanation ? `
        <div class="qvd-ai-summary">
          <div>
            <span class="badge ${displayExplanation.used_llm ? 'badge-success' : 'badge-info'}">${displayExplanation.used_llm ? 'LLM enhanced' : 'Deterministic fallback'}</span>
          </div>
          ${(displayExplanation.warnings || []).map(warning => `<div class="qvd-warning-line">${escapeHtml(warning)}</div>`).join('')}
          ${renderAiMarkdown(displayExplanation.summary_markdown || '')}
          ${renderAiMetricExplanations(displayExplanation.metric_explanations || [])}
          ${(displayExplanation.migration_narrative || '').trim() ? `
            <details class="qvd-details">
              <summary>Show migration narrative</summary>
              ${renderAiMarkdown(displayExplanation.migration_narrative || '')}
            </details>
          ` : ''}
        </div>
      ` : `<div class="inspect-empty">Generate an optional plain-English summary after the metric catalog is available.</div>`}
    </div>
  `;
}

function normalizeAiExplanationForDisplay(aiExplanation) {
  if (!aiExplanation) return null;
  const parsed = parseJsonFromAiText(aiExplanation.summary_markdown || '');
  if (!parsed) return aiExplanation;
  const legacyParseWarning = 'AI response was not JSON; using it as summary text with deterministic metric details.';
  return {
    ...aiExplanation,
    summary_markdown: parsed.summary_markdown || aiExplanation.summary_markdown || '',
    metric_explanations: parsed.metric_explanations || aiExplanation.metric_explanations || [],
    migration_narrative: parsed.migration_narrative || parsed[LEGACY_AI_NARRATIVE_KEY] || aiExplanation.migration_narrative || aiExplanation[LEGACY_AI_NARRATIVE_KEY] || '',
    warnings: (aiExplanation.warnings || []).filter(warning => warning !== legacyParseWarning),
  };
}

function parseJsonFromAiText(text) {
  const raw = String(text || '').trim();
  if (!raw) return null;
  const fenced = raw.match(/```(?:json)?\s*([\s\S]*?)\s*```/i);
  const candidate = fenced ? fenced[1].trim() : raw.slice(raw.indexOf('{'), raw.lastIndexOf('}') + 1).trim();
  if (!candidate || !candidate.startsWith('{') || !candidate.endsWith('}')) return null;
  try {
    return JSON.parse(candidate);
  } catch {
    return null;
  }
}

function renderAiMarkdown(text) {
  const lines = String(text || '').split(/\r?\n/);
  const blocks = [];
  let bullets = [];
  const flushBullets = () => {
    if (bullets.length) {
      blocks.push(`<ul>${bullets.map(item => `<li>${formatAiInline(item)}</li>`).join('')}</ul>`);
      bullets = [];
    }
  };
  lines.forEach(line => {
    const trimmed = line.trim();
    if (!trimmed) {
      flushBullets();
      return;
    }
    const heading = trimmed.match(/^#{1,3}\s+(.+)$/);
    const bullet = trimmed.match(/^[-*]\s+(.+)$/);
    if (heading) {
      flushBullets();
      blocks.push(`<h4>${formatAiInline(heading[1])}</h4>`);
    } else if (bullet) {
      bullets.push(bullet[1]);
    } else {
      flushBullets();
      blocks.push(`<p>${formatAiInline(trimmed)}</p>`);
    }
  });
  flushBullets();
  return `<div class="qvd-ai-content">${blocks.join('')}</div>`;
}

function formatAiInline(text) {
  return escapeHtml(text)
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code>$1</code>');
}

function renderAiMetricExplanations(explanations) {
  const rows = normalizeAiMetricExplanations(explanations).slice(0, 8);
  if (!rows.length) return '';
  return `
    <div class="qvd-metric-theme">
      <div class="qvd-metric-theme-title">
        <h4>Metric explanations</h4>
        <span class="badge badge-info">${rows.length}</span>
      </div>
      <div class="qvd-metric-grid">
        ${rows.map(row => `
          <article class="qvd-metric-card">
            <div class="qvd-metric-name">${escapeHtml(row.metric_name || 'Metric')}</div>
            <div class="qvd-metric-description">${escapeHtml(row.plain_english || '')}</div>
            ${(row.source_columns || []).length || (row.analyze_by || []).length || row.date_column ? `
              <dl>
                ${(row.source_columns || []).length ? `<div><dt>Source</dt><dd>${escapeHtml(row.source_columns.join(', '))}</dd></div>` : ''}
                ${(row.analyze_by || []).length ? `<div><dt>Analyze by</dt><dd>${escapeHtml(row.analyze_by.slice(0, 6).join(', '))}</dd></div>` : ''}
                ${row.date_column ? `<div><dt>Date</dt><dd>${escapeHtml(row.date_column)}</dd></div>` : ''}
              </dl>
            ` : ''}
          </article>
        `).join('')}
      </div>
    </div>
  `;
}

function normalizeAiMetricExplanations(explanations) {
  if (Array.isArray(explanations)) return explanations;
  if (!explanations || typeof explanations !== 'object') return [];
  return Object.entries(explanations).map(([metricName, explanation]) => ({
    metric_name: metricName,
    plain_english: typeof explanation === 'string' ? explanation : JSON.stringify(explanation),
    source_columns: [],
    analyze_by: [],
    date_column: '',
  }));
}

function renderAdvancedArtifacts(analysis, kpiCatalog, lineageReconciliation, aiExplanation, options = {}) {
  const artifacts = {
    'business_entities.json': analysis?.artifacts?.business_entities_json,
    'kpi_catalog.csv': kpiCatalog?.artifacts?.kpi_catalog_csv,
    'business_analysis.md': kpiCatalog?.artifacts?.business_analysis_md || kpiCatalog?.documentation_path,
    'lineage.json': lineageReconciliation?.artifacts?.lineage_json,
    'reconciliation_rules.md': lineageReconciliation?.artifacts?.reconciliation_rules_md,
    'business_glossary.csv': lineageReconciliation?.artifacts?.business_glossary_csv,
    'executive_summary.json': lineageReconciliation?.artifacts?.executive_summary_json,
    'ai_business_summary.md': aiExplanation?.artifacts?.ai_business_summary_md,
    'ai_migration_narrative.md': aiExplanation?.artifacts?.ai_migration_narrative_md || aiExplanation?.artifacts?.[LEGACY_AI_NARRATIVE_ARTIFACT_KEY],
  };
  return `
    <details class="qvd-details qvd-advanced-artifacts" ${options.expanded ? 'open' : ''}>
      <summary>Advanced artifacts</summary>
      <div class="qvd-artifact-grid">
        ${Object.entries(artifacts).map(([label, path]) => `
          <div class="qvd-artifact-item">
            <span>${escapeHtml(label)}</span>
            ${path ? artifactLink('Download', path) : '<em>Not generated yet</em>'}
          </div>
        `).join('')}
      </div>
    </details>
  `;
}

function summaryCard(label, value) {
  return `<div class="qvd-profile-card"><span>${escapeHtml(label)}</span><strong>${Number(value || 0)}</strong></div>`;
}

function renderEntitySection(title, rows, isHierarchy = false) {
  return `
    <div class="qvd-ddl-result">
      <div class="qvd-ddl-header">
        <div>
          <h3>${escapeHtml(title)}</h3>
          <p>${rows.length} detected</p>
        </div>
      </div>
      ${rows.length ? `
        <div class="qvd-profile-table-wrapper">
          <table class="qvd-profile-table">
            <thead>
              <tr>
                <th>Source Column</th>
                <th>Target Column</th>
                <th>Entity Type</th>
                <th>Business Name</th>
                <th>Confidence</th>
                <th>Reason</th>
                <th>${isHierarchy ? 'Levels' : 'Sample Values'}</th>
              </tr>
            </thead>
            <tbody>
              ${rows.map(row => `
                <tr>
                  <td>${escapeHtml(row.source_column || '')}</td>
                  <td>${escapeHtml(row.target_column || '')}</td>
                  <td><code>${escapeHtml(row.entity_type || '')}</code></td>
                  <td>${escapeHtml(row.business_name || '')}</td>
                  <td>${Number(row.confidence || 0).toFixed(2)}</td>
                  <td>${escapeHtml(row.reason || '')}</td>
                  <td>${escapeHtml(isHierarchy ? (row.levels || []).map(level => level.source_column).join(' > ') : (row.sample_values || []).join(', '))}</td>
                </tr>
              `).join('')}
            </tbody>
          </table>
        </div>
      ` : `<div class="inspect-empty">No ${escapeHtml(title.toLowerCase())} detected.</div>`}
    </div>
  `;
}

function setupBusinessHandlers() {
  document.querySelectorAll('[data-business-section]').forEach(button => {
    button.addEventListener('click', () => {
      activeBusinessSection = button.dataset.businessSection || 'overview';
      store.set({});
    });
  });
  document.getElementById('run-business-analysis-btn')?.addEventListener('click', handleRunBusinessAnalysis);
  document.getElementById('generate-kpi-catalog-btn')?.addEventListener('click', handleGenerateKpiCatalog);
  document.getElementById('generate-lineage-reconciliation-btn')?.addEventListener('click', handleGenerateLineageReconciliation);
  document.getElementById('generate-ai-explanation-btn')?.addEventListener('click', handleGenerateAiExplanation);
  document.getElementById('continue-inspect-mapping-btn')?.addEventListener('click', handleContinueInspectMapping);
  setupLineageGraph();
}

function setupLineageGraph() {
  const graphContainer = document.getElementById('qvd-lineage-graph');
  const payload = store.get().qvdLineageReconciliation;
  if (!graphContainer || !payload?.lineage) {
    if (lineageGraph) {
      lineageGraph.destroy();
      lineageGraph = null;
    }
    return;
  }
  if (lineageGraph) lineageGraph.destroy();

  const detail = document.getElementById('qvd-lineage-detail');
  const getEnabledTypes = () => {
    const enabled = new Set();
    document.querySelectorAll('.qvd-lineage-type-toggle').forEach(input => {
      if (input.checked) {
        String(input.dataset.types || '').split(',').filter(Boolean).forEach(type => enabled.add(type));
      }
    });
    return enabled;
  };
  const renderNodeDetail = node => {
    if (!detail || !node) return;
    lineageUiState.selectedNode = node;
    detail.innerHTML = renderLineageNodeDetail(node);
  };
  const updateGraph = () => {
    const selectedKpi = document.getElementById('qvd-lineage-kpi-select')?.value || '';
    const fullButton = document.getElementById('qvd-lineage-full-btn');
    lineageUiState.selectedKpi = selectedKpi;
    lineageUiState.showFull = fullButton?.dataset.full === 'true';
    lineageUiState.enabledTypes = getEnabledTypes();
    lineageGraph.update({
      selectedKpi: lineageUiState.selectedKpi,
      showFull: lineageUiState.showFull,
      enabledTypes: lineageUiState.enabledTypes,
    });
  };

  lineageGraph = new LineageGraph(graphContainer, payload.lineage, {
    selectedKpi: lineageUiState.selectedKpi,
    showFull: lineageUiState.showFull,
    enabledTypes: lineageUiState.enabledTypes,
    onNodeClick: renderNodeDetail,
  });
  document.getElementById('qvd-lineage-kpi-select')?.addEventListener('change', updateGraph);
  document.querySelectorAll('.qvd-lineage-type-toggle').forEach(input => input.addEventListener('change', updateGraph));
  document.getElementById('qvd-lineage-fit-btn')?.addEventListener('click', () => lineageGraph.fitView());
  document.getElementById('qvd-lineage-full-btn')?.addEventListener('click', event => {
    const button = event.currentTarget;
    const next = button.dataset.full !== 'true';
    button.dataset.full = String(next);
    lineageUiState.showFull = next;
    button.textContent = next ? 'Show Summary Lineage' : 'Show Full Technical Lineage';
    updateGraph();
  });
}

function renderLineageNodeDetail(node) {
  return `
    <strong>${escapeHtml(node.label || node.id || 'Lineage node')}</strong>
    <dl>
      <div><dt>Type</dt><dd>${escapeHtml(node.type || '')}</dd></div>
      ${node.source_column ? `<div><dt>QVD source column</dt><dd>${escapeHtml(node.source_column)}</dd></div>` : ''}
      ${node.target_column ? `<div><dt>Databricks field</dt><dd>${escapeHtml(node.target_column)}</dd></div>` : ''}
      ${node.formula ? `<div><dt>Metric formula</dt><dd><code>${escapeHtml(node.formula)}</code></dd></div>` : ''}
    </dl>
  `;
}

async function handleRunBusinessAnalysis() {
  const sessionId = store.get().qvdInspection?.session_id || store.get().sessionId;
  if (!sessionId) return;
  store.set({ isDiscoveringQvdBusinessEntities: true });
  try {
    const result = await api.discoverQvdBusinessEntities(sessionId);
    store.set({
      qvdBusinessAnalysis: result,
      qvdKpiCatalog: null,
      qvdLineageReconciliation: null,
      qvdAiExplanation: null,
      isDiscoveringQvdBusinessEntities: false,
    });
  } catch (err) {
    store.set({
      isDiscoveringQvdBusinessEntities: false,
      qvdBusinessAnalysis: {
        success: false,
        summary: {},
        dimensions: [],
        measures: [],
        dates: [],
        flags: [],
        identifiers: [],
        hierarchies: [],
        errors: err.errors || [err.message || 'Business entity discovery failed'],
      },
    });
    alert(err.message || 'Business entity discovery failed');
  }
}

async function handleGenerateKpiCatalog() {
  const sessionId = store.get().qvdInspection?.session_id || store.get().sessionId;
  if (!sessionId) return;
  store.set({ isGeneratingQvdKpiCatalog: true });
  try {
    const result = await api.generateQvdKpiCatalog(sessionId);
    store.set({ qvdKpiCatalog: result, qvdLineageReconciliation: null, qvdAiExplanation: null, isGeneratingQvdKpiCatalog: false });
  } catch (err) {
    store.set({
      isGeneratingQvdKpiCatalog: false,
      qvdKpiCatalog: {
        success: false,
        kpi_count: 0,
        kpis: [],
        errors: err.errors || [err.message || 'KPI catalog generation failed'],
      },
    });
    alert(err.message || 'KPI catalog generation failed');
  }
}

async function handleGenerateLineageReconciliation() {
  const sessionId = store.get().qvdInspection?.session_id || store.get().sessionId;
  if (!sessionId) return;
  store.set({ isGeneratingQvdLineageReconciliation: true });
  try {
    const result = await api.generateQvdLineageReconciliation(sessionId);
    store.set({ qvdLineageReconciliation: result, isGeneratingQvdLineageReconciliation: false });
  } catch (err) {
    store.set({
      isGeneratingQvdLineageReconciliation: false,
      qvdLineageReconciliation: {
        success: false,
        lineage_nodes: 0,
        lineage_edges: 0,
        reconciliation_rule_count: 0,
        lineage: { nodes: [], edges: [] },
        reconciliation: { rules: [] },
        errors: err.errors || [err.message || 'Lineage and reconciliation generation failed'],
      },
    });
    alert(err.message || 'Lineage and reconciliation generation failed');
  }
}

async function handleGenerateAiExplanation() {
  const sessionId = store.get().qvdInspection?.session_id || store.get().sessionId;
  if (!sessionId) return;
  store.set({ isGeneratingQvdAiExplanation: true });
  try {
    const result = await api.generateQvdAiExplanation(sessionId);
    store.set({ qvdAiExplanation: result, isGeneratingQvdAiExplanation: false });
  } catch (err) {
    store.set({
      isGeneratingQvdAiExplanation: false,
      qvdAiExplanation: {
        success: false,
        used_llm: false,
        summary_markdown: '',
        metric_explanations: [],
        migration_narrative: '',
        warnings: err.warnings || [],
        errors: err.errors || [err.message || 'Business explanation failed'],
      },
    });
    alert(err.message || 'Business explanation failed');
  }
}

async function handleContinueInspectMapping() {
  const state = store.get();
  const sessionId = state.qvdInspection?.session_id || state.sessionId;
  if (!sessionId) return;
  if (state.qvdSchemaSuggestion) {
    store.navigate('inspect');
    return;
  }
  store.set({ isSuggestingQvdSchema: true });
  try {
    const result = await api.suggestQvdSchema(sessionId);
    store.set({
      qvdSchemaSuggestion: result,
      qvdEditableMapping: JSON.parse(JSON.stringify(result.mapping || [])),
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

export function destroyBusinessPage() {
  if (lineageGraph) {
    lineageGraph.destroy();
    lineageGraph = null;
  }
}
