/**
 * QVF Decoder — Main Application Entry Point
 * Hash-based SPA router with navigation bar
 */
import './styles/main.css';
import { api } from './api.js';
import { store } from './store.js';
import { renderUploadPage, destroyUploadPage } from './pages/upload.js';
import { renderBusinessPage, destroyBusinessPage } from './pages/business.js';
import { renderInspectPage, destroyInspectPage } from './pages/inspect.js';
import { renderReviewPage, destroyReviewPage } from './pages/review.js';
import { renderOutputPage, destroyOutputPage } from './pages/output.js';
import { renderDeployPage, destroyDeployPage } from './pages/deploy.js';
import { renderAgentPage, destroyAgentPage } from './pages/agent.js';

const app = document.getElementById('app');

// ─── Render Application Shell ────────────────────────────────────────────────

function renderApp() {
  const state = store.get();
  const isQvdMode = state.uploadMode === 'qvd';
  const canBusiness = isQvdMode ? !!state.qvdInspection : false;
  const canInspect = isQvdMode ? !!state.qvdSchemaSuggestion : !!state.filename;
  const qvdHasDdl = !!state.qvdDdlGeneration?.generated;
  const qvdHasLoadScripts = Object.values(state.qvdDatabricksLoadScripts || {}).some(result => result?.generated);
  const qvdHasPackage = Object.values(state.qvdMigrationPackages || {}).some(result => result?.generated);
  const canReview = isQvdMode ? qvdHasDdl : !!state.filename;
  const canOutput = isQvdMode ? qvdHasLoadScripts : !!state.regeneratedSql;
  const activeName = isQvdMode
    ? `${state.qvdInspection?.uploaded_files?.length || 0} QVD file${(state.qvdInspection?.uploaded_files?.length || 0) === 1 ? '' : 's'}`
    : state.filename;

  app.innerHTML = `
    <!-- Navigation Bar -->
    <nav class="navbar">
      <div class="navbar-brand">
        <div class="navbar-logo">Q</div>
        <div>
          <div class="navbar-title">QVF Decoder</div>
          <div class="navbar-subtitle">Qlik Migration Tool</div>
        </div>
      </div>

      <div class="navbar-nav">
        <a class="nav-step ${state.currentPage === 'upload' ? 'active' : ''}" data-page="upload" id="nav-upload">
          <span class="nav-step-number">1</span>
          ${isQvdMode ? 'QVD Upload' : 'Upload & Analyze'}
        </a>
        <div class="nav-divider"></div>
        ${isQvdMode ? `
          <a class="nav-step ${state.currentPage === 'business' ? 'active' : ''} ${!canBusiness ? 'disabled' : ''}" data-page="business" id="nav-business">
            <span class="nav-step-number">2</span>
            Business Analysis
          </a>
          <div class="nav-divider"></div>
        ` : ''}
        <a class="nav-step ${state.currentPage === 'inspect' ? 'active' : ''} ${!canInspect ? 'disabled' : ''}" data-page="inspect" id="nav-inspect">
          <span class="nav-step-number">${isQvdMode ? '3' : '2'}</span>
          ${isQvdMode ? 'Inspect Mapping' : 'Inspect Data'}
        </a>
        <div class="nav-divider"></div>
        <a class="nav-step ${state.currentPage === 'review' ? 'active' : ''} ${!canReview ? 'disabled' : ''}" data-page="review" id="nav-review">
          <span class="nav-step-number">${isQvdMode ? '4' : '3'}</span>
          ${isQvdMode ? 'Next Step' : 'Review & Edit'}
        </a>
        <div class="nav-divider"></div>
        <a class="nav-step ${state.currentPage === 'output' ? 'active' : ''} ${!canOutput ? 'disabled' : ''}" data-page="output" id="nav-output">
          <span class="nav-step-number">${isQvdMode ? '5' : '4'}</span>
          Output
        </a>
        ${isQvdMode ? `
          <div class="nav-divider"></div>
          <a class="nav-step ${state.currentPage === 'deploy' ? 'active' : ''} ${!qvdHasPackage ? 'disabled' : ''}" data-page="deploy" id="nav-deploy">
            <span class="nav-step-number">6</span>
            Databricks Deployment
          </a>
        ` : `
          <div class="nav-divider"></div>
          <a class="nav-step ${state.currentPage === 'agent' ? 'active' : ''} ${!canOutput ? 'disabled' : ''}" data-page="agent" id="nav-agent">
            <span class="nav-step-number">5</span>
            dbt Agent
          </a>
        `}
      </div>

      <div class="navbar-actions">
        ${activeName ? `
          <span style="font-size:11px;color:var(--text-muted);display:flex;align-items:center;gap:6px">
            <span style="color:var(--success)">●</span>
            ${activeName}
          </span>
        ` : ''}
      </div>
    </nav>

    <!-- Page Content -->
    <div id="page-content" style="flex:1;display:flex;overflow:hidden"></div>

    <!-- Status Bar -->
    <div class="status-bar">
      <div class="status-bar-left">
        <div class="status-indicator">
          <div class="status-dot ${activeName ? '' : 'warning'}"></div>
          <span>${activeName ? 'Connected' : 'Ready'}</span>
        </div>
        ${state.sessionId ? `<span>Session: ${state.sessionId.substring(0, 8)}…</span>` : ''}
      </div>
      <div class="status-bar-right">
        ${(() => {
          if (isQvdMode) {
            const qvdTables = state.qvdInspection?.tables?.length || 0;
            const qvdFiles = state.qvdInspection?.uploaded_files?.length || 0;
            return `
              <span>${qvdTables} QVD table${qvdTables !== 1 ? 's' : ''}</span>
              <span>${qvdFiles} file${qvdFiles !== 1 ? 's' : ''}</span>
            `;
          }
          const tableCount = state.graph && state.graph.nodes
            ? state.graph.nodes.filter(n => n.type !== 'external_file').length
            : (state.tables ? state.tables.length : 0);
          const relCount = state.graph && state.graph.edges
            ? state.graph.edges.filter(e => e.type !== 'dependency').length
            : (state.associations ? state.associations.length : 0);
          return `
            <span>${tableCount} table${tableCount !== 1 ? 's' : ''}</span>
            <span>${relCount} relationship${relCount !== 1 ? 's' : ''}</span>
          `;
        })()}
        <span>QVF Decoder v2.0</span>
      </div>
    </div>
  `;

  // Render current page
  const pageContent = document.getElementById('page-content');
  renderCurrentPage(pageContent, state.currentPage);

  // Setup navigation click handlers
  setupNavigation();
}

function renderCurrentPage(container, page) {
  // Destroy previous page components
  destroyUploadPage();
  destroyBusinessPage();
  destroyInspectPage();
  destroyReviewPage();
  destroyOutputPage();
  destroyDeployPage();
  destroyAgentPage();

  switch (page) {
    case 'upload':
      renderUploadPage(container);
      break;
    case 'business':
      renderBusinessPage(container);
      break;
    case 'inspect':
      renderInspectPage(container);
      break;
    case 'review':
      renderReviewPage(container);
      break;
    case 'output':
      renderOutputPage(container);
      break;
    case 'deploy':
      renderDeployPage(container);
      break;
    case 'agent':
      renderAgentPage(container);
      break;
    default:
      renderUploadPage(container);
  }
}

function setupNavigation() {
  document.querySelectorAll('.nav-step:not(.disabled)').forEach(step => {
    step.addEventListener('click', (e) => {
      e.preventDefault();
      const page = step.dataset.page;
      if (page) {
        store.navigate(page);
      }
    });
  });
}

// ─── Subscribe to State Changes ──────────────────────────────────────────────

store.subscribe((state) => {
  renderApp();
});

// ─── Handle Browser Navigation ──────────────────────────────────────────────

window.addEventListener('hashchange', () => {
  const hash = window.location.hash.slice(1);
  if (['upload', 'business', 'inspect', 'review', 'output', 'deploy', 'agent'].includes(hash)) {
    const state = store.get();
    if (state.currentPage !== hash) {
      store.set({ currentPage: hash });
      renderApp();
    }
  }
});

// ─── Initial Render ──────────────────────────────────────────────────────────
(async () => {
  // Restore a previous session from localStorage so a page refresh doesn't
  // wipe the user's work.  If no saved session exists we start fresh.
  const savedSessionId = localStorage.getItem('qvf_session_id');

  if (savedSessionId) {
    try {
      const data = await api.getModel(savedSessionId);

      const enrichedGraph = {
        ...data.graph,
        nodes: (data.graph?.nodes || []).map(n => ({
          ...n,
          type: n.type || ((n.keyFields || []).length > 1 ? 'fact' : 'dimension'),
        })),
      };

      store.set({
        sessionId: savedSessionId,
        fileId: data.fileId,
        currentFileId: data.fileId,
        filename: data.filename,
        graph: enrichedGraph,
        tables: data.tables || [],
        associations: data.associations || [],
        metadata: data.metadata,
        script: data.script || '',
        sqlSections: data.sqlSections || [],
        description: data.description || '',
        editedSql: data.editedSql || data.script || '',
        editedText: data.editedText || data.description || '',
        generationPlan: data.generationPlan || [],
        generationPlanText: data.generationPlanText || '',
        regeneration: data.regeneration || null,
        regeneratedSql: data.regeneratedSql || data.regeneration?.sql || '',
        regeneratedText: data.regeneratedText || data.regeneration?.description || '',
        regeneratedLineage: data.regeneratedLineage || data.regeneration?.lineage || '',
        regenerationHistory: data.regenerationHistory || [],
        sessionStats: data.sessionStats,
      });

      // Restore per-file review state
      const files = data.sessionStats?.files || [];
      files.forEach(file => {
        store.ensureFileReviewState(file.fileId, {
          editedSql: file.script || '',
          editedText: file.description || '',
          regeneratedSql: '',
          regeneratedText: '',
          regeneratedLineage: '',
          generationPlan: data.generationPlan || [],
          generationPlanText: data.generationPlanText || '',
          regeneration: data.regeneration || null,
          regenerationHistory: data.regenerationHistory || [],
          editMode: false,
          rightEditMode: false,
          activeRightTab: 'plan',
        });
        store.setFileReviewBaseline(file.fileId, {
          sourceSql: file.script || '',
          regenSql: '',
          regenText: '',
        });
      });

      if (data.fileId) {
        store.setCurrentFile(data.fileId, store.getFileReviewState(data.fileId) || {});
      }
    } catch (err) {
      // Session no longer valid on the server — start clean
      console.warn('Could not restore session, starting fresh:', err.message);
      localStorage.removeItem('qvf_session_id');
      store.reset();
    }
  }

  renderApp();
})();
