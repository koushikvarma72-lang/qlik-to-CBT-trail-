/**
 * QVF Decoder — Page 1: Graph-Based Dependency Upload
 */
import { store } from '../store.js';
import { api } from '../api.js';
import { GraphComponent } from '../components/graph.js';

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

  container.innerHTML = `
    <div class="page" id="upload-page">
      <!-- Sidebar -->
      <div class="sidebar animate-slide-left">
        <div class="sidebar-header" style="padding-bottom: 0;">
          <!-- Header removed to avoid duplication with Navbar -->
        </div>

        <div class="sidebar-section">
          <div class="sidebar-section-title">Upload QVF File</div>
          <div class="upload-zone" id="upload-zone">
            <div class="upload-zone-icon">📁</div>
            <div class="upload-zone-text">Drop .qvf or .zip file here</div>
            <div class="upload-zone-hint">or click to browse</div>
            <input type="file" id="file-input" accept=".qvf,.zip" />
          </div>
        </div>

        <div class="sidebar-content" id="sidebar-files">
          ${state.filename ? renderFileInfo(state) : renderEmptyFiles()}
        </div>

        <div class="sidebar-section" style="border-top:1px solid var(--border);border-bottom:none;margin-top:auto;padding-bottom:16px">
          <button class="btn btn-outline btn-block" id="reset-session-btn" style="color:var(--text-dim);border-color:var(--border);width:100%;font-size:12px">
            🔄 Reset All Data
          </button>
        </div>
      </div>

      <!-- Main Graph Area -->
      <div style="flex:1;display:flex;flex-direction:column;overflow:hidden">
        <div style="flex:1;position:relative" id="graph-area">
          ${state.graph.nodes.length === 0 ? renderEmptyGraph() : ''}
        </div>

        <!-- Bottom Bar -->
        <div class="review-footer">
          <div style="display:flex;align-items:center;gap:12px">
            ${state.filename ? `
              <span class="badge badge-success">✅ ${sessionTotals.totalTables} ${ sessionTotals.totalTables === 1 ? 'table' : 'tables' }</span>
              <span class="badge badge-info">🔗 ${sessionTotals.totalRelationships} ${ sessionTotals.totalRelationships === 1 ? 'relationship' : 'relationships' }</span>
            ` : ''}
          </div>
          <div style="display:flex;gap:8px">
            <button class="btn btn-secondary btn-lg" id="inspect-btn" ${!state.filename ? 'disabled' : ''}>
              Inspect Data
            </button>
            <button class="btn btn-primary btn-lg" id="review-btn" ${!state.filename ? 'disabled' : ''}>
            Review Data Model →
            </button>
          </div>
        </div>
      </div>
    </div>
  `;

  // Setup event listeners
  setupUploadHandlers();
  setupGraphIfData();
  setupUploadOverlay();
  setupInspectButton();
  setupReviewButton();
  setupResetButton();
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
    onNodeClick: (node) => {
      console.log('Node clicked:', node);
    },
    onUploadClick: (node) => {
      console.log('🚀 Graph Upload Triggered for:', node.name);
      
      // Create a temporary file input to bypass browser security restrictions
      const tempInput = document.createElement('input');
      tempInput.type = 'file';
      tempInput.accept = '.qvf,.zip';
      tempInput.style.display = 'none';
      
      tempInput.onchange = (e) => {
        if (e.target.files.length > 0) {
          console.log('📁 File selected from graph button:', e.target.files[0].name);
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
    if (state.filename) {
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
