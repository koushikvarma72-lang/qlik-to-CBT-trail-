/**
 * QVF Decoder â€” API Client
 * Handles all communication with the Flask backend
 */

export const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL || 'http://localhost:5000').replace(/\/+$/, '');
const API_BASE = `${API_BASE_URL}/api`;

export function apiDownloadUrl(downloadUrl) {
  if (!downloadUrl) return '#';
  if (/^https?:\/\//i.test(downloadUrl)) return downloadUrl;
  if (downloadUrl.startsWith('/api')) return `${API_BASE_URL}${downloadUrl}`;
  return downloadUrl;
}

function friendlyMessage(payload, fallbackMessage, statusMessage) {
  const raw = String(payload.error || (payload.errors || [])[0] || '').trim();
  if (raw.includes('<!doctype html>') || raw.includes('Method Not Allowed')) {
    return `${fallbackMessage}: this action is not available from the current backend route. Restart the backend and try again.`;
  }
  if (/approved mapping artifact not found/i.test(raw)) {
    return 'Save the approved mapping before running this step.';
  }
  if (/KPI catalog artifact not found/i.test(raw)) {
    return 'Generate the KPI Catalog & Documentation before running this step.';
  }
  if (/Parquet validation report not found/i.test(raw)) {
    return 'Validate the Parquet output before running Databricks load or deployment steps.';
  }
  if (/Parquet path is local/i.test(raw)) {
    return 'The Parquet output is on your local machine. Configure a Databricks Volume Path or cloud storage path before loading data.';
  }
  if (/SQL Warehouse ID is required/i.test(raw)) {
    return 'Enter a SQL Warehouse ID before testing or executing Databricks deployment.';
  }
  if (/valid Databricks Workspace URL|Workspace URL is required/i.test(raw)) {
    return 'Enter a valid Databricks Workspace URL, for example https://dbc-xxxx.cloud.databricks.com.';
  }
  return raw || `${fallbackMessage}: ${statusMessage}`;
}

async function parseApiResponse(res, fallbackMessage) {
  const text = await res.text();
  let payload = {};

  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = { error: text };
    }
  }

  if (!res.ok) {
    const statusMessage = `${res.status}${res.statusText ? ` ${res.statusText}` : ''}`;
    const error = new Error(friendlyMessage(payload, fallbackMessage, statusMessage));
    Object.assign(error, payload);
    throw error;
  }

  return payload;
}

export const api = {
  /**
   * Upload a QVF file
   * @param {File} file - The QVF file to upload
   * @param {string} sessionId - Optional session ID
   * @returns {Promise<Object>} Upload result with graph data
   */
  async uploadFile(file, sessionId = null) {
    const formData = new FormData();
    formData.append('file', file);
    if (sessionId) {
      formData.append('session_id', sessionId);
    }

    const res = await fetch(`${API_BASE}/upload`, {
      method: 'POST',
      body: formData,
    });

    return parseApiResponse(res, 'Upload failed');
  },

  async uploadInspectQvd(files, sessionId = null) {
    const formData = new FormData();
    Array.from(files || []).forEach(file => {
      formData.append('files', file);
    });
    if (sessionId) {
      formData.append('session_id', sessionId);
    }

    const res = await fetch(`${API_BASE}/qvd/upload-inspect`, {
      method: 'POST',
      body: formData,
    });

    return parseApiResponse(res, 'QVD inspection failed');
  },

  async suggestQvdSchema(sessionId) {
    const res = await fetch(`${API_BASE}/qvd/suggest-schema/${encodeURIComponent(sessionId)}`, {
      method: 'POST',
    });

    return parseApiResponse(res, 'QVD schema suggestion failed');
  },

  async discoverQvdBusinessEntities(sessionId) {
    const res = await fetch(`${API_BASE}/qvd/business-analysis/entities/${encodeURIComponent(sessionId)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });

    return parseApiResponse(res, 'QVD business entity discovery failed');
  },

  async generateQvdKpiCatalog(sessionId) {
    const res = await fetch(`${API_BASE}/qvd/business-analysis/kpis/${encodeURIComponent(sessionId)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });

    return parseApiResponse(res, 'QVD KPI catalog generation failed');
  },

  async generateQvdLineageReconciliation(sessionId) {
    const res = await fetch(`${API_BASE}/qvd/business-analysis/lineage-reconciliation/${encodeURIComponent(sessionId)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });

    return parseApiResponse(res, 'QVD lineage and reconciliation generation failed');
  },

  async generateQvdAiExplanation(sessionId) {
    const res = await fetch(`${API_BASE}/qvd/business-analysis/ai-explain/${encodeURIComponent(sessionId)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });

    return parseApiResponse(res, 'QVD AI business explanation failed');
  },

  async saveApprovedQvdMapping(sessionId, mappingRows) {
    const res = await fetch(`${API_BASE}/qvd/save-approved-mapping/${encodeURIComponent(sessionId)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mapping_rows: mappingRows }),
    });

    return parseApiResponse(res, 'Approved QVD mapping save failed');
  },

  async generateQvdDdl(sessionId) {
    const res = await fetch(`${API_BASE}/qvd/generate-ddl/${encodeURIComponent(sessionId)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });

    return parseApiResponse(res, 'QVD DDL generation failed');
  },

  async getQvdSession(sessionId) {
    const res = await fetch(`${API_BASE}/qvd/session/${encodeURIComponent(sessionId)}`);
    return parseApiResponse(res, 'Failed to load QVD session');
  },

  async previewQvdRows(sessionId, fileName, limit = 100) {
    const res = await fetch(`${API_BASE}/qvd/preview-rows/${encodeURIComponent(sessionId)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file_name: fileName, limit }),
    });

    return parseApiResponse(res, 'QVD row preview failed');
  },

  async profileQvdColumns(sessionId, fileName, limit = 10000) {
    const res = await fetch(`${API_BASE}/qvd/profile-columns/${encodeURIComponent(sessionId)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file_name: fileName, limit }),
    });

    return parseApiResponse(res, 'QVD column profiling failed');
  },

  async convertQvdToParquet(sessionId, fileName, batchId = null) {
    const body = { file_name: fileName };
    if (batchId) body.batch_id = batchId;
    const res = await fetch(`${API_BASE}/qvd/convert-parquet/${encodeURIComponent(sessionId)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });

    return parseApiResponse(res, 'QVD to Parquet conversion failed');
  },

  async validateQvdParquet(sessionId, targetTable) {
    const res = await fetch(`${API_BASE}/qvd/validate-parquet/${encodeURIComponent(sessionId)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target_table: targetTable }),
    });

    return parseApiResponse(res, 'QVD Parquet validation failed');
  },

  async generateQvdDatabricksLoad(sessionId, targetTable, parquetPath = null) {
    const body = { target_table: targetTable };
    if (parquetPath) body.parquet_path = parquetPath;
    const res = await fetch(`${API_BASE}/qvd/generate-databricks-load/${encodeURIComponent(sessionId)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });

    return parseApiResponse(res, 'QVD Databricks load script generation failed');
  },

  async generateQvdMigrationPackage(sessionId, targetTable, fileName = null) {
    const body = { target_table: targetTable };
    if (fileName) body.file_name = fileName;
    const res = await fetch(`${API_BASE}/qvd/generate-migration-package/${encodeURIComponent(sessionId)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });

    return parseApiResponse(res, 'QVD migration package generation failed');
  },

  async saveQvdDatabricksConfig(sessionId, config) {
    const res = await fetch(`${API_BASE}/qvd/databricks/save-config/${encodeURIComponent(sessionId)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(config || {}),
    });

    return parseApiResponse(res, 'Databricks configuration save failed');
  },

  async testQvdDatabricksConnection(sessionId, config) {
    const res = await fetch(`${API_BASE}/qvd/databricks/test-connection/${encodeURIComponent(sessionId)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(config || {}),
    });

    return parseApiResponse(res, 'Databricks connection test failed');
  },

  async discoverQvdDatabricksWarehouses(sessionId, config) {
    const res = await fetch(`${API_BASE}/qvd/databricks/warehouses/${encodeURIComponent(sessionId)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(config || {}),
    });
    return parseApiResponse(res, 'Databricks warehouse discovery failed');
  },

  async discoverQvdDatabricksCatalogs(sessionId, config) {
    const res = await fetch(`${API_BASE}/qvd/databricks/catalogs/${encodeURIComponent(sessionId)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(config || {}),
    });
    return parseApiResponse(res, 'Databricks catalog discovery failed');
  },

  async discoverQvdDatabricksSchemas(sessionId, config) {
    const res = await fetch(`${API_BASE}/qvd/databricks/schemas/${encodeURIComponent(sessionId)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(config || {}),
    });
    return parseApiResponse(res, 'Databricks schema discovery failed');
  },

  async discoverQvdDatabricksVolumes(sessionId, config) {
    const res = await fetch(`${API_BASE}/qvd/databricks/volumes/${encodeURIComponent(sessionId)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(config || {}),
    });
    return parseApiResponse(res, 'Databricks volume discovery failed');
  },

  async createQvdDatabricksSchema(sessionId, config) {
    const res = await fetch(`${API_BASE}/qvd/databricks/create-schema/${encodeURIComponent(sessionId)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(config || {}),
    });
    return parseApiResponse(res, 'Databricks schema creation failed');
  },

  async createQvdDatabricksVolume(sessionId, config) {
    const res = await fetch(`${API_BASE}/qvd/databricks/create-volume/${encodeURIComponent(sessionId)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(config || {}),
    });
    return parseApiResponse(res, 'Databricks volume creation failed');
  },

  async uploadQvdParquetToDatabricksVolume(sessionId, targetTable, config) {
    const res = await fetch(`${API_BASE}/qvd/databricks/upload-parquet/${encodeURIComponent(sessionId)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...(config || {}), target_table: targetTable }),
    });
    return parseApiResponse(res, 'Databricks volume upload failed');
  },

  async executeQvdDatabricksMigration(sessionId, targetTable, executionMode, config) {
    const res = await fetch(`${API_BASE}/qvd/databricks/execute/${encodeURIComponent(sessionId)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ...(config || {}),
        target_table: targetTable,
        execution_mode: executionMode,
      }),
    });

    return parseApiResponse(res, 'Databricks migration execution failed');
  },

  async precheckQvdDatabricksDeployment(sessionId, targetTable, executionMode, config) {
    const res = await fetch(`${API_BASE}/qvd/databricks/precheck/${encodeURIComponent(sessionId)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ...(config || {}),
        target_table: targetTable,
        execution_mode: executionMode,
      }),
    });

    return parseApiResponse(res, 'Databricks deployment precheck failed');
  },

  /**
   * Get full data model for a session
   * @param {string} sessionId
   * @returns {Promise<Object>} Full model data
   */
  async getModel(sessionId) {
    const res = await fetch(`${API_BASE}/model/${sessionId}`);
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.error || 'Failed to load model');
    }
    return res.json();
  },

  /**
   * Regenerate SQL and description from edits
   * @param {string} sessionId
   * @param {string} editedSql
   * @param {string} editedText
   * @returns {Promise<Object>} Regenerated output
   */
  async regenerate(sessionId, editedSql, editedText, triggerMigration = false, regeneratedSql = '', regeneratedText = '', dialect = 'dbt', generationMode = 'auto') {
    const res = await fetch(`${API_BASE}/regenerate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ 
        sessionId, 
        editedSql, 
        editedText,
        triggerMigration,
        regeneratedSql,
        regeneratedText,
        dialect,
        generationMode
      }),
    });

    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.error || 'Regeneration failed');
    }

    return res.json();
  },

  async getRegenerationStatus(jobId) {
    const res = await fetch(`${API_BASE}/regenerate/status/${jobId}`);
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.error || 'Failed to load regeneration status');
    }
    return res.json();
  },

  async explain(sessionId, code) {
    const res = await fetch(`${API_BASE}/explain`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sessionId, code }),
    });
    if (!res.ok) throw new Error('Failed to explain code');
    return res.json();
  },

  /**
   * Send a natural-language refinement instruction for the current SQL draft.
   * @param {string} sessionId
   * @param {string} message  - e.g. "add a filter for active customers only"
   * @param {string} currentSql
   * @param {string} currentDesc
   * @param {string} dialect
   */
  async chat(sessionId, message, currentSql = '', currentDesc = '', dialect = 'dbt') {
    const res = await fetch(`${API_BASE}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sessionId, message, currentSql, currentDesc, dialect }),
    });
    const payload = await res.json();
    if (!res.ok) throw new Error(payload.error || 'Chat refinement failed');
    return payload;
  },

  /**
   * Open an SSE stream for a regeneration job.
   * @param {string} jobId
   * @param {Object} callbacks - { onToken(text), onProgress(msg), onDone(data), onError(err) }
   * @returns {EventSource} — caller can call .close() to abort
   */
  streamJob(jobId, callbacks = {}) {
    const evtSource = new EventSource(`${API_BASE}/stream/${jobId}`);
    evtSource.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'token' && callbacks.onToken) callbacks.onToken(data.content);
        else if (data.type === 'progress' && callbacks.onProgress) callbacks.onProgress(data.message);
        else if (data.type === 'done') { evtSource.close(); if (callbacks.onDone) callbacks.onDone(data); }
        else if (data.type === 'error') { evtSource.close(); if (callbacks.onError) callbacks.onError(new Error(data.message)); }
        // heartbeat — ignore
      } catch (e) { /* ignore parse errors */ }
    };
    evtSource.onerror = () => {
      evtSource.close();
      if (callbacks.onError) callbacks.onError(new Error('Stream connection failed'));
    };
    return evtSource;
  },

  /**
   * Streaming chat refinement via fetch + ReadableStream.
   * POST is not supported by EventSource, so we use fetch.
   * @param {string} sessionId
   * @param {string} message
   * @param {string} currentSql
   * @param {string} currentDesc
   * @param {string} dialect
   * @param {Object} callbacks - { onToken(text), onDone(data), onError(err) }
   * @returns {Promise<void>}
   */
  async chatStream(sessionId, message, currentSql = '', currentDesc = '', dialect = 'dbt', callbacks = {}) {
    const res = await fetch(`${API_BASE}/chat/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sessionId, message, currentSql, currentDesc, dialect }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ error: 'Chat stream failed' }));
      throw new Error(err.error || 'Chat stream failed');
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop(); // keep incomplete line in buffer
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        try {
          const data = JSON.parse(line.slice(6));
          if (data.type === 'token' && callbacks.onToken) callbacks.onToken(data.content);
          else if (data.type === 'done' && callbacks.onDone) callbacks.onDone(data);
          else if (data.type === 'error') {
            if (callbacks.onError) callbacks.onError(new Error(data.message));
            return;
          }
        } catch (e) { /* ignore parse errors */ }
      }
    }
  },

  async testDbtCloudConnection(config) {
    const res = await fetch(`${API_BASE}/dbt-cloud/test`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(config),
    });
    const payload = await res.json();
    if (!res.ok) throw new Error(payload.error || 'Failed to connect to dbt Cloud');
    return payload;
  },

  async runDbtCloudJob(config) {
    const res = await fetch(`${API_BASE}/dbt-cloud/run`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(config),
    });
    const payload = await res.json();
    if (!res.ok) throw new Error(payload.error || 'Failed to trigger dbt Cloud job');
    return payload;
  },

  async getDbtCloudRunStatus(config) {
    const res = await fetch(`${API_BASE}/dbt-cloud/status`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(config),
    });
    const payload = await res.json();
    if (!res.ok) throw new Error(payload.error || 'Failed to load dbt Cloud run status');
    return payload;
  },

  /**
   * Clear all data for a fresh start
   */
  async reset() {
    const res = await fetch(`${API_BASE}/reset`, { method: 'POST' });
    if (!res.ok) throw new Error('Failed to reset session');
    return res.json();
  },
};

