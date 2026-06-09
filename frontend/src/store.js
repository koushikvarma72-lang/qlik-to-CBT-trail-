/**
 * QVF Decoder â€” Global State Store
 * Simple reactive state management
 */

const state = {
  currentPage: 'upload',      // 'upload' | 'business' | 'inspect' | 'review' | 'output' | 'deploy' | 'agent'
  sessionId: null,
  currentFileId: null,
  fileId: null,
  filename: null,

  // Data from upload
  graph: { nodes: [], edges: [] },
  tables: [],
  associations: [],
  metadata: null,
  script: '',
  sqlSections: [],
  description: '',
  generationPlan: [],
  generationPlanText: '',

  // Edited data (Page 2)
  editedSql: '',
  editedText: '',
  editMode: false,
  rightEditMode: false,
  activeRightTab: 'sql',

  // Regenerated output (Page 3)
  regeneratedSql: '',
  regeneratedText: '',
  regeneratedLineage: '',
  regeneration: null,
  regenerationHistory: [],

  // Review state, keyed by fileId
  reviewStateByFile: {},

  // UI State
  isUploading: false,
  isProcessing: false,
  uploadingFilename: null,
  isGenerating: false,
  uploadProgress: 0,
  dialect: 'dbt',
  uploadMode: 'qvf',
  qvdInspection: null,
  qvdSchemaSuggestion: null,
  qvdBusinessAnalysis: null,
  qvdKpiCatalog: null,
  qvdLineageReconciliation: null,
  qvdAiExplanation: null,
  qvdEditableMapping: [],
  qvdApprovedMapping: null,
  qvdDdlGeneration: null,
  qvdRowPreviews: {},
  qvdColumnProfiles: {},
  qvdParquetConversions: {},
  qvdParquetValidations: {},
  qvdDatabricksLoadScripts: {},
  qvdMigrationPackages: {},
  qvdDatabricksConfig: {
    workspace_url: '',
    personal_access_token: '',
    sql_warehouse_id: '',
    catalog: 'main',
    schema: 'qvd_raw',
    volume: '',
    volume_path: '',
    cloud_storage_path: '',
  },
  qvdDatabricksWarehouses: [],
  qvdDatabricksCatalogs: [],
  qvdDatabricksSchemas: [],
  qvdDatabricksVolumes: [],
  qvdDatabricksUpload: null,
  qvdDatabricksConnection: null,
  qvdDatabricksPrecheck: null,
  qvdDatabricksExecution: null,
  qvdExecutionMode: 'generate_sql_only',
  qvdMappingValidationErrors: [],
  qvdSelectedFiles: [],
  isSuggestingQvdSchema: false,
  isDiscoveringQvdBusinessEntities: false,
  isGeneratingQvdKpiCatalog: false,
  isGeneratingQvdLineageReconciliation: false,
  isGeneratingQvdAiExplanation: false,
  isSavingQvdMapping: false,
  isGeneratingQvdDdl: false,
  qvdPreviewLoadingByFile: {},
  qvdProfileLoadingByFile: {},
  qvdParquetLoadingByFile: {},
  qvdParquetValidationLoadingByFile: {},
  qvdDatabricksLoadLoadingByFile: {},
  qvdMigrationPackageLoadingByFile: {},
  isSavingDatabricksConfig: false,
  isTestingDatabricksConnection: false,
  isDiscoveringDatabricksWarehouses: false,
  isDiscoveringDatabricksCatalogs: false,
  isDiscoveringDatabricksSchemas: false,
  isDiscoveringDatabricksVolumes: false,
  isPreparingDatabricksTarget: false,
  isUploadingDatabricksParquet: false,
  isRunningDatabricksPrecheck: false,
  isExecutingDatabricksMigration: false,

  // Listeners
  _listeners: [],
};

export const store = {
  get() {
    return state;
  },

  set(updates) {
    Object.assign(state, updates);
    // Persist sessionId so a page refresh can restore the session
    if (updates.sessionId !== undefined) {
      if (updates.sessionId) {
        localStorage.setItem('qvf_session_id', updates.sessionId);
      } else {
        localStorage.removeItem('qvf_session_id');
      }
    }
    state._listeners.forEach(fn => fn(state));
  },

  ensureFileReviewState(fileId, fallback = {}) {
    if (!fileId) return null;

    if (!state.reviewStateByFile[fileId]) {
      state.reviewStateByFile[fileId] = {
        editMode: false,
        rightEditMode: false,
        activeRightTab: 'sql',
        editedSql: fallback.editedSql || fallback.script || '',
        editedText: fallback.editedText || fallback.description || '',
        regeneratedSql: fallback.regeneratedSql || '',
        regeneratedText: fallback.regeneratedText || '',
        regeneratedLineage: fallback.regeneratedLineage || '',
        regeneration: fallback.regeneration || null,
        regenerationHistory: fallback.regenerationHistory || [],
        generationPlan: fallback.generationPlan || [],
        generationPlanText: fallback.generationPlanText || '',
        baseline: fallback.baseline || null,
      };
    } else if (fallback && Object.keys(fallback).length > 0) {
      // Merge carefully: don't overwrite a populated generationPlan with an empty one
      const safeUpdate = { ...fallback };
      if (
        (!safeUpdate.generationPlan || safeUpdate.generationPlan.length === 0) &&
        state.reviewStateByFile[fileId].generationPlan?.length > 0
      ) {
        delete safeUpdate.generationPlan;
        delete safeUpdate.generationPlanText;
      }
      Object.assign(state.reviewStateByFile[fileId], safeUpdate);
    }

    return state.reviewStateByFile[fileId];
  },

  getFileReviewState(fileId) {
    if (!fileId) return null;
    return state.reviewStateByFile[fileId] || null;
  },

  setFileReviewState(fileId, updates) {
    if (!fileId) return null;
    const current = this.ensureFileReviewState(fileId);
    Object.assign(current, updates);
    this._syncCurrentFileMirror(fileId);
    return current;
  },

  setFileReviewBaseline(fileId, baseline) {
    if (!fileId) return null;
    const current = this.ensureFileReviewState(fileId);
    current.baseline = { ...baseline };
    this._syncCurrentFileMirror(fileId);
    return current;
  },

  isFileReviewDirty(fileId, snapshot) {
    const current = state.reviewStateByFile[fileId];
    if (!current || !current.baseline) return true;

    const live = snapshot || {
      sourceSql: current.editedSql || '',
      regenSql: current.regeneratedSql || '',
      regenText: current.regeneratedText || '',
    };

    return (
      live.sourceSql !== (current.baseline.sourceSql || '') ||
      live.regenSql !== (current.baseline.regenSql || '') ||
      live.regenText !== (current.baseline.regenText || '')
    );
  },

  setCurrentFile(fileId, fallback = {}) {
    const current = this.ensureFileReviewState(fileId, fallback);
    state.currentFileId = fileId;
    state.fileId = fileId;
    if (current) this._syncCurrentFileMirror(fileId);
    return current;
  },

  _syncCurrentFileMirror(fileId) {
    const current = state.reviewStateByFile[fileId];
    if (!current) return;
    state.editMode = !!current.editMode;
    state.rightEditMode = !!current.rightEditMode;
    state.activeRightTab = current.activeRightTab || 'sql';
    state.editedSql = current.editedSql || '';
    state.editedText = current.editedText || '';
    state.regeneratedSql = current.regeneratedSql || '';
    state.regeneratedText = current.regeneratedText || '';
    state.regeneratedLineage = current.regeneratedLineage || '';
    state.regeneration = current.regeneration || null;
    state.regenerationHistory = current.regenerationHistory || [];
    state.generationPlan = current.generationPlan || [];
    state.generationPlanText = current.generationPlanText || '';
  },

  subscribe(fn) {
    state._listeners.push(fn);
    return () => {
      state._listeners = state._listeners.filter(l => l !== fn);
    };
  },

  navigate(page) {
    state.currentPage = page;
    window.location.hash = page;
    state._listeners.forEach(fn => fn(state));
  },

  reset() {
    Object.assign(state, {
      sessionId: null,
      currentFileId: null,
      fileId: null,
      filename: null,
      graph: { nodes: [], edges: [] },
      tables: [],
      associations: [],
      metadata: null,
      script: '',
      sqlSections: [],
      description: '',
      generationPlan: [],
      generationPlanText: '',
      editedSql: '',
      editedText: '',
      editMode: false,
      rightEditMode: false,
      activeRightTab: 'sql',
      regeneratedSql: '',
      regeneratedText: '',
      regeneratedLineage: '',
      regeneration: null,
      regenerationHistory: [],
      reviewStateByFile: {},
      isUploading: false,
      isProcessing: false,
      uploadingFilename: null,
      isGenerating: false,
      dialect: 'dbt',
      uploadMode: 'qvf',
      qvdInspection: null,
      qvdSchemaSuggestion: null,
      qvdBusinessAnalysis: null,
      qvdKpiCatalog: null,
      qvdLineageReconciliation: null,
      qvdAiExplanation: null,
      qvdEditableMapping: [],
      qvdApprovedMapping: null,
      qvdDdlGeneration: null,
      qvdRowPreviews: {},
      qvdColumnProfiles: {},
      qvdParquetConversions: {},
      qvdParquetValidations: {},
      qvdDatabricksLoadScripts: {},
      qvdMigrationPackages: {},
      qvdDatabricksConfig: {
        workspace_url: '',
        personal_access_token: '',
        sql_warehouse_id: '',
        catalog: 'main',
        schema: 'qvd_raw',
        volume: '',
        volume_path: '',
        cloud_storage_path: '',
      },
      qvdDatabricksWarehouses: [],
      qvdDatabricksCatalogs: [],
      qvdDatabricksSchemas: [],
      qvdDatabricksVolumes: [],
      qvdDatabricksUpload: null,
      qvdDatabricksConnection: null,
      qvdDatabricksPrecheck: null,
      qvdDatabricksExecution: null,
      qvdExecutionMode: 'generate_sql_only',
      qvdMappingValidationErrors: [],
      qvdSelectedFiles: [],
      isSuggestingQvdSchema: false,
      isDiscoveringQvdBusinessEntities: false,
      isGeneratingQvdKpiCatalog: false,
      isGeneratingQvdLineageReconciliation: false,
      isGeneratingQvdAiExplanation: false,
      isGeneratingQvdDdl: false,
      isSavingQvdMapping: false,
      qvdPreviewLoadingByFile: {},
      qvdProfileLoadingByFile: {},
      qvdParquetLoadingByFile: {},
      qvdParquetValidationLoadingByFile: {},
      qvdDatabricksLoadLoadingByFile: {},
      qvdMigrationPackageLoadingByFile: {},
      isSavingDatabricksConfig: false,
      isTestingDatabricksConnection: false,
      isDiscoveringDatabricksWarehouses: false,
      isDiscoveringDatabricksCatalogs: false,
      isDiscoveringDatabricksSchemas: false,
      isDiscoveringDatabricksVolumes: false,
      isPreparingDatabricksTarget: false,
      isUploadingDatabricksParquet: false,
      isRunningDatabricksPrecheck: false,
      isExecutingDatabricksMigration: false,
    });
    localStorage.removeItem('qvf_session_id');
    state._listeners.forEach(fn => fn(state));
  },
};

// Initialize from URL hash
const hash = window.location.hash.slice(1);
if (['upload', 'business', 'inspect', 'review', 'output', 'deploy', 'agent'].includes(hash)) {
  state.currentPage = hash;
}

// Restore sessionId from localStorage so a page refresh reconnects to the active session.
// The app should call api.getModel(sessionId) after this to rehydrate full state.
const _storedSessionId = localStorage.getItem('qvf_session_id');
if (_storedSessionId) {
  state.sessionId = _storedSessionId;
}

