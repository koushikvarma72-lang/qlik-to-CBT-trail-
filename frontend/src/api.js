/**
 * QVF Decoder â€” API Client
 * Handles all communication with the Flask backend
 */

const API_BASE = '/api';

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

    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.error || 'Upload failed');
    }

    return res.json();
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
  async regenerate(sessionId, editedSql, editedText, triggerMigration = false, regeneratedSql = '', regeneratedText = '', dialect = 'dbt') {
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
        dialect
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

