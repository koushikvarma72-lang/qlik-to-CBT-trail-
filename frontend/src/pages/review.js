/**
 * QVF Decoder — Page 2: Data Model + SQL + Text Editor
 */
import { store } from '../store.js';
import { api } from '../api.js';
import { GraphComponent } from '../components/graph.js';
import { SQLEditor } from '../components/editor.js';
import { hasReviewChangesSnapshot, normalizeRegenerationRecord } from './review-state.js';
import { escapeHtml, markdownToHtml } from '../utils.js';

let graphComponent = null;
let sqlEditor = null;
let rightSqlEditor = null;

function getCurrentReviewValues(currentFile) {
  const state = store.get();
  const fileId = state.currentFileId || state.fileId || null;
  const fileState = store.getFileReviewState(fileId) || {};
  const currentSourceSql = sqlEditor ? sqlEditor.getValue() : fileState.editedSql || state.editedSql || currentFile?.script || state.script || '';
  const currentRegenSql = rightSqlEditor ? rightSqlEditor.getValue() : fileState.regeneratedSql || state.regeneratedSql || '';
  const rightTextarea = document.getElementById('right-description-textarea');
  const currentRegenText = rightTextarea ? rightTextarea.value : fileState.regeneratedText || state.regeneratedText || '';

  return {
    fileId,
    sourceSql: currentSourceSql,
    regenSql: currentRegenSql,
    regenText: currentRegenText,
  };
}

function hasReviewChanges(currentFile) {
  const current = getCurrentReviewValues(currentFile);
  const fileId = current.fileId;
  const fileState = store.getFileReviewState(fileId);
  return hasReviewChangesSnapshot(fileState?.baseline || null, current);
}

function persistCurrentReviewState(currentFile) {
  const current = getCurrentReviewValues(currentFile);
  if (!current.fileId) return current;
  store.setFileReviewState(current.fileId, {
    editedSql: current.sourceSql,
    regeneratedSql: current.regenSql,
    regeneratedText: current.regenText,
  });
  return current;
}

function getFileHistory(state, fileId) {
  return (state.regenerationHistory || []).filter(item => item.fileId === fileId);
}

function getSessionTotals(state) {
  const sessionStats = state.sessionStats || {};

  // Prefer server-provided totals, then count from sessionStats files, then fall back to graph
  const totalTables = Number.isFinite(sessionStats.totalTables) && sessionStats.totalTables > 0
    ? sessionStats.totalTables
    : (sessionStats.files || []).reduce((sum, file) => sum + (file.tables || []).length, 0)
      || (state.graph?.nodes || []).filter(n => n.type !== 'external_file').length;

  const totalRelationships = Number.isFinite(sessionStats.totalRelationships) && sessionStats.totalRelationships > 0
    ? sessionStats.totalRelationships
    : (state.associations || []).length
      || (state.graph?.edges || []).filter(e => e.type !== 'dependency').length;

  return { totalTables, totalRelationships };
}

function applyRegenerationResult(fileId, result) {
  console.log('[applyRegenerationResult] raw result keys:', Object.keys(result || {}));
  console.log('[applyRegenerationResult] result.regeneration:', result?.regeneration);
  console.log('[applyRegenerationResult] result.result:', result?.result);
  const input = result?.regeneration || result || null;
  console.log('[applyRegenerationResult] input to normalizeRegenerationRecord:', input);
  const structured = normalizeRegenerationRecord(input);
  console.log('[applyRegenerationResult] structured after normalize:', structured);
  console.log('[applyRegenerationResult] structured.sql length:', structured?.sql?.length ?? 'NO SQL');
  if (!fileId || !structured) {
    console.warn('[applyRegenerationResult] BAILING — fileId:', fileId, 'structured:', structured);
    return structured;
  }

  const resolvedJobId =
    result?.jobId ||
    result?.job_id ||
    result?.id ||
    result?.result?.jobId ||
    result?.result?.job_id ||
    result?.regeneration?.jobId ||
    result?.regeneration?.job_id ||
    store.get().currentRegenerationJobId ||
    null;

  store.setFileReviewState(fileId, {
    regeneratedSql: structured.sql || '',
    regeneratedText: structured.description || '',
    regeneration: structured,
    regenerationHistory: result.regenerationHistory || store.get().regenerationHistory || [],
    activeRightTab: structured.sql ? 'sql' : 'desc',
    currentRegenerationJobId: resolvedJobId,
  });

  store.set({
    regeneration: structured,
    regenerationHistory: result.regenerationHistory || store.get().regenerationHistory || [],
    currentRegenerationJobId: resolvedJobId,
  });
  return structured;
}

async function waitForRegenerationJob(jobId) {
  /**
   * SSE-based streaming with automatic polling fallback.
   * Try to open an EventSource connection. If it fails or fails to connect within 5s,
   * fall back to the old robust polling loop.
   */
  return new Promise((resolve, reject) => {
    let sqlBuffer = '';
    const startTime = Date.now();
    let evtSource = null;
    let sseConnected = false;
    let sseTimeoutId = null;

    // Show streaming container, hide spinner
    const loadingEl = document.getElementById('ai-loading');
    if (loadingEl) loadingEl.style.display = 'none';

    // Get or prepare the right SQL editor for streaming
    const rightContainer = document.getElementById('right-sql-editor-container');
    let streamTarget = null;
    if (rightSqlEditor) {
      rightSqlEditor.setValue('');
      streamTarget = rightSqlEditor;
    } else if (rightContainer) {
      // Create a temporary streaming display
      rightContainer.innerHTML = '<pre id="stream-preview" style="padding:16px;margin:0;font-family:\'JetBrains Mono\',\'Fira Code\',monospace;font-size:13px;line-height:1.6;color:var(--text-primary);white-space:pre-wrap;word-break:break-word;overflow-y:auto;height:100%"></pre>';
    }

    // Function to run the polling fallback loop
    async function runPollingFallback(reason) {
      console.warn(`[waitForRegenerationJob] SSE failed (${reason}) — falling back to polling...`);
      // Restore loading overlay if it was hidden
      if (loadingEl) loadingEl.style.display = 'flex';
      // Remove floating status and stream preview if present
      document.getElementById('stream-status')?.remove();
      const previewEl = document.getElementById('stream-preview');
      if (previewEl) previewEl.remove();

      const BASE_DELAY_MS = 1000;
      const BACKOFF_FACTOR = 1.5;
      const MAX_DELAY_MS = 5000;
      const TIMEOUT_MS = 300_000; // Allow up to 5 minutes

      let attempt = 0;
      let delayMs = BASE_DELAY_MS;
      let lastProgressMessage = null;
      let lastHeartbeat = null;
      let lastChangeTime = Date.now();

      try {
        while (Date.now() - startTime < TIMEOUT_MS) {
          const status = await api.getRegenerationStatus(jobId);
          console.log(`[Polling Fallback] job=${jobId} status=${status.status} progress=${status.progress_message}`);

          if (status.status === 'complete' || status.status === 'failed') {
            resolve(status);
            return;
          }

          if (status.progress_message !== lastProgressMessage || status.last_heartbeat !== lastHeartbeat) {
            lastProgressMessage = status.progress_message;
            lastHeartbeat = status.last_heartbeat;
            lastChangeTime = Date.now();
          }

          const elapsed = Math.round((Date.now() - startTime) / 1000);
          const loadingCard = document.querySelector('#ai-loading .loading-card');
          if (loadingCard) {
            let elapsedEl = loadingCard.querySelector('.elapsed-time');
            if (!elapsedEl) {
              elapsedEl = document.createElement('p');
              elapsedEl.className = 'elapsed-time';
              elapsedEl.style.cssText = 'font-size:11px;color:var(--text-dim);margin-top:4px';
              loadingCard.appendChild(elapsedEl);
            }
            elapsedEl.textContent = `${elapsed}s elapsed…`;

            if (status.progress_message) {
              let progressEl = loadingCard.querySelector('.progress-message');
              if (!progressEl) {
                progressEl = document.createElement('p');
                progressEl.className = 'progress-message';
                progressEl.style.cssText = 'font-size:13px;color:var(--primary);font-weight:600;margin-top:8px';
                loadingCard.appendChild(progressEl);
              }
              progressEl.textContent = status.progress_message;
            }

            const timeSinceChange = Date.now() - lastChangeTime;
            let stallEl = loadingCard.querySelector('.stall-warning');
            if (timeSinceChange > 90_000) {
              if (!stallEl) {
                stallEl = document.createElement('p');
                stallEl.className = 'stall-warning';
                stallEl.style.cssText = 'font-size:11px;color:var(--warning,#f59e0b);margin-top:8px';
                loadingCard.appendChild(stallEl);
              }
              stallEl.textContent = '⚠️ AI model is taking longer than usual — still working, please wait…';
            } else if (stallEl) {
              stallEl.remove();
            }
          }

          const serverHint = status.retryAfter ? status.retryAfter * 1000 : null;
          await new Promise(res => setTimeout(res, serverHint ?? delayMs));
          delayMs = Math.min(delayMs * BACKOFF_FACTOR, MAX_DELAY_MS);
          attempt += 1;
        }
        reject(new Error('Timed out waiting for regeneration to finish (5 min).'));
      } catch (err) {
        reject(err);
      }
    }

    // Set a 5-second connection watchdog timer
    sseTimeoutId = setTimeout(() => {
      if (!sseConnected) {
        if (evtSource) evtSource.close();
        runPollingFallback('connection timeout');
      }
    }, 5000);

    try {
      evtSource = api.streamJob(jobId, {
        onToken(content) {
          sseConnected = true;
          if (sseTimeoutId) { clearTimeout(sseTimeoutId); sseTimeoutId = null; }
          sqlBuffer += content;
          if (streamTarget) {
            streamTarget.setValue(sqlBuffer);
          } else {
            const pre = document.getElementById('stream-preview');
            if (pre) {
              pre.textContent = sqlBuffer;
              pre.scrollTop = pre.scrollHeight;
            }
          }
        },

        onProgress(message) {
          sseConnected = true;
          if (sseTimeoutId) { clearTimeout(sseTimeoutId); sseTimeoutId = null; }
          // Show progress messages in a subtle status bar
          const loadingCard = document.querySelector('#ai-loading .loading-card');
          if (loadingCard) {
            let progressEl = loadingCard.querySelector('.progress-message');
            if (!progressEl) {
              progressEl = document.createElement('p');
              progressEl.className = 'progress-message';
              progressEl.style.cssText = 'font-size:13px;color:var(--primary);font-weight:600;margin-top:8px';
              loadingCard.appendChild(progressEl);
            }
            progressEl.textContent = message;
          }
          // Also show as a floating status if loading overlay is hidden
          let floatingStatus = document.getElementById('stream-status');
          if (!floatingStatus) {
            floatingStatus = document.createElement('div');
            floatingStatus.id = 'stream-status';
            floatingStatus.style.cssText = 'position:absolute;top:8px;right:8px;background:var(--bg-elevated);color:var(--primary);padding:4px 10px;border-radius:6px;font-size:11px;font-weight:600;z-index:10;box-shadow:0 2px 8px rgba(0,0,0,0.1)';
            const rightOutput = document.getElementById('right-output-area');
            if (rightOutput) rightOutput.appendChild(floatingStatus);
          }
          floatingStatus.textContent = `⚡ ${message}`;
        },

        onDone(data) {
          sseConnected = true;
          if (sseTimeoutId) { clearTimeout(sseTimeoutId); sseTimeoutId = null; }
          document.getElementById('stream-status')?.remove();
          const finalSql = data?.sql || data?.final_sql || '';
          const finalDesc = data?.description || '';
          const finalWarnings = data?.warnings || [];
          const finalStatus = data?.status || 'complete';
          const repairAttempted = Boolean(data?.repairAttempted);
          const loopNeeded = Boolean(data?.loopNeeded);
          const oneShotQualityScore = Number(data?.oneShotQualityScore || 0);
          const blockingIssues = data?.blockingIssues || [];
          const resolvedJobId = data?.jobId || data?.job_id || data?.id || jobId;
          console.log('SSE done final SQL chars', finalSql.length);
          if (finalSql.trim()) {
            sqlBuffer = finalSql;
            if (streamTarget) {
              streamTarget.setValue(finalSql);
            } else {
              const pre = document.getElementById('stream-preview');
              if (pre) {
                pre.textContent = finalSql;
                pre.scrollTop = pre.scrollHeight;
              }
            }
            const state = store.get();
            const fileId = state.currentFileId || state.fileId;
            if (fileId) {
              store.setFileReviewState(fileId, {
                regeneratedSql: finalSql,
                regeneration: {
                  ...(store.getFileReviewState(fileId)?.regeneration || {}),
                  sql: finalSql,
                  description: finalDesc || (store.getFileReviewState(fileId)?.regeneratedText || ''),
                  warnings: finalWarnings,
                  status: finalStatus,
                  repairAttempted,
                  loopNeeded,
                  oneShotQualityScore,
                  blockingIssues,
                  jobId: resolvedJobId,
                },
              });
            }
          }
          resolve({
            status: finalStatus,
            result: {
              sql: finalSql,
              description: finalDesc,
              warnings: finalWarnings,
              repairAttempted,
              loopNeeded,
              oneShotQualityScore,
              blockingIssues,
              jobId: resolvedJobId,
            },
          });
        },

        onError(err) {
          if (sseTimeoutId) { clearTimeout(sseTimeoutId); sseTimeoutId = null; }
          if (!sseConnected) {
            // Fails immediately before getting any data: fallback
            runPollingFallback(err.message || 'connection error');
          } else {
            // Already received data but got an error mid-stream
            document.getElementById('stream-status')?.remove();
            if (loadingEl) loadingEl.style.display = 'none';
            reject(err);
          }
        },
      });
    } catch (e) {
      if (sseTimeoutId) { clearTimeout(sseTimeoutId); sseTimeoutId = null; }
      runPollingFallback(e.message);
    }
  });
}

async function executeRegenerationFlow(triggerMigration, finalize = false) {
  const state = store.get();
  const fileId = state.currentFileId || state.fileId;
  const files = state.sessionStats?.files || [];
  const currentFile = files.find(f => f.fileId === fileId) || {
    filename: state.filename,
    script: state.script,
    description: state.description,
    tables: state.tables
  };

  const currentValues = getCurrentReviewValues(currentFile);
  const currentSourceSql = currentValues.sourceSql;
  const currentRegenSql = currentValues.regenSql;
  const currentRegenText = currentValues.regenText;
  const editedText = store.getFileReviewState(fileId)?.editedText || state.editedText || state.description || '';

  store.setFileReviewState(fileId, {
    editedSql: currentSourceSql,
    regeneratedSql: currentRegenSql,
    regeneratedText: currentRegenText
  });
  store.set({ isGenerating: true });

  const page = document.getElementById('review-page')?.parentElement;
  if (page) renderReviewPage(page);

  try {
    const initial = await api.regenerate(
      state.sessionId,
      currentSourceSql,
      editedText,
      triggerMigration,
      currentRegenSql,
      currentRegenText,
      state.dialect || 'snowflake'
    );
    console.log('executeRegenerationFlow: initial regenerate response', initial);

    let finalPayload = initial;
    if (initial.queued && initial.jobId) {
      store.set({ currentRegenerationJobId: initial.jobId });

      if (fileId) {
        store.setFileReviewState(fileId, {
          currentRegenerationJobId: initial.jobId,
          validationExportResult: null,
          validationExportError: '',
        });
      }

      finalPayload = await waitForRegenerationJob(initial.jobId);
    }

    console.log('[executeRegenerationFlow] finalPayload keys:', Object.keys(finalPayload || {}));
    console.log('[executeRegenerationFlow] finalPayload.result:', finalPayload?.result);
    console.log('[executeRegenerationFlow] finalPayload.result?.sql length:', finalPayload?.result?.sql?.length ?? 'MISSING');
    console.log('[executeRegenerationFlow] finalPayload.regeneration:', finalPayload?.regeneration);
    console.log('[executeRegenerationFlow] initial.regeneration:', initial?.regeneration);

    const normalizedResult = finalPayload.result || initial.regeneration || finalPayload.regeneration || null;
    console.log('[executeRegenerationFlow] normalizedResult:', normalizedResult);
    console.log('[executeRegenerationFlow] normalizedResult?.sql length:', normalizedResult?.sql?.length ?? 'MISSING');
    const mergedHistory = Array.isArray(finalPayload.history)
      ? finalPayload.history
      : (Array.isArray(initial.regenerationHistory) ? initial.regenerationHistory : []);
    const mergedResult = {
      ...initial,
      ...finalPayload,
      regeneration: normalizedResult,
      regenerationHistory: mergedHistory,
    };
    const structured = applyRegenerationResult(fileId, mergedResult);
    if (finalize) {
      store.setFileReviewBaseline(fileId, {
        sourceSql: currentSourceSql,
        regenSql: structured?.sql || currentRegenSql || '',
        regenText: structured?.description || currentRegenText || '',
      });
    } else {
      // After a successful "Migrate to DBT" (non-finalize), update the baseline
      // so that clicking "Finalize" immediately after does NOT re-run the AI —
      // it only re-runs if the user actually edits the source or generated SQL.
      if (structured?.sql) {
        store.setFileReviewBaseline(fileId, {
          sourceSql: currentSourceSql,
          regenSql: structured.sql,
          regenText: structured.description || currentRegenText || '',
        });
      }
    }
    store.set({
      isGenerating: false,
      regeneration: structured,
      regenerationHistory: mergedResult.regenerationHistory || store.get().regenerationHistory || [],
    });

    const rerenderTarget = document.getElementById('review-page')?.parentElement;
    if (rerenderTarget) renderReviewPage(rerenderTarget);

    if (finalize) {
      store.navigate('output');
    }
  } catch (err) {
    console.error(finalize ? 'Finalization failed:' : 'Migration failed:', err);
    store.set({ isGenerating: false });
    const rerenderTarget = document.getElementById('review-page')?.parentElement;
    if (rerenderTarget) renderReviewPage(rerenderTarget);
    throw err;
  }
}

export function renderReviewPage(container, options = {}) {
  const state = store.get();
  const preserveGraph = !!options.preserveGraph;
  const existingGraphArea = preserveGraph ? document.getElementById('review-graph-area') : null;

  // If no data, redirect to upload
  if (!state.filename) {
    if (state.sessionId) {
      loadSessionData(state.sessionId, container);
      container.innerHTML = `<div class="page"><div class="empty-state"><div class="spinner spinner-lg"></div><div class="empty-state-title" style="margin-top:16px">Loading session...</div></div></div>`;
      return;
    }
    store.navigate('upload');
    return;
  }

  // If sessionStats is missing (e.g. navigated back from output after a page
  // refresh), reload from the server so the script and tables are available.
  if (!state.sessionStats && state.sessionId) {
    loadSessionData(state.sessionId, container);
    container.innerHTML = `<div class="page"><div class="empty-state"><div class="spinner spinner-lg"></div><div class="empty-state-title" style="margin-top:16px">Loading session...</div></div></div>`;
    return;
  }

  const files = state.sessionStats?.files || [];
  const currentFileId = state.currentFileId || state.fileId;

  // Build currentFile — prefer sessionStats (has the original script) over
  // the top-level state fields which may be stale after finalization.
  const currentFile = files.find(f => f.fileId === currentFileId) || {
    filename: state.filename,
    script: state.script || '',
    description: state.description || '',
    tables: state.tables || [],
  };

  // Ensure the script is always available — if sessionStats lost it, fall back
  // to the store's top-level script field.
  if (!currentFile.script && state.script) {
    currentFile.script = state.script;
  }
  const currentFileState = currentFileId ? store.setCurrentFile(currentFileId, {
    editedSql: state.editedSql || currentFile.script || '',
    editedText: state.editedText || currentFile.description || '',
    regeneratedSql: state.regeneratedSql || '',
    regeneratedText: state.regeneratedText || '',
    editMode: state.editMode || false,
    rightEditMode: state.rightEditMode || false,
    activeRightTab: state.activeRightTab || 'sql',
  }) : {
    editedSql: currentFile.script || '',
    editedText: currentFile.description || '',
    regeneratedSql: state.regeneratedSql || '',
    regeneratedText: state.regeneratedText || '',
    editMode: state.editMode || false,
    rightEditMode: state.rightEditMode || false,
    activeRightTab: state.activeRightTab || 'sql',
    baseline: null,
  };
  if (!currentFileState.baseline) {
    store.setFileReviewBaseline(currentFileId, {
      sourceSql: currentFileState.editedSql || currentFile.script || '',
      regenSql: currentFileState.regeneratedSql || '',
      regenText: currentFileState.regeneratedText || '',
    });
  }
  const isEdit = currentFileState.editMode;
  const activeRightTab = currentFileState.activeRightTab || 'sql';
  const rightEditMode = currentFileState.rightEditMode || false;
  const generationWarnings = currentFileState.regeneration?.warnings || state.regeneration?.warnings || [];
  const currentJobId =
    currentFileState.currentRegenerationJobId ||
    state.currentRegenerationJobId ||
    currentFileState.regeneration?.jobId ||
    currentFileState.regeneration?.job_id ||
    state.regeneration?.jobId ||
    state.regeneration?.job_id ||
    null;
  const exportResult = currentFileState.validationExportResult || null;
  const exportError = currentFileState.validationExportError || '';
  const isExporting = Boolean(currentFileState.isExportingValidationZip);
  const fileHistory = getFileHistory(state, currentFileId);
  const sessionTotals = getSessionTotals(state);

  container.innerHTML = `
    <div class="page" id="review-page" style="flex-direction:column">
      <!-- Top Section -->
      <div style="flex:1;display:flex;overflow:hidden;min-height:0">
        <!-- Left Panel: Tree View -->
        <div class="panel" style="width:280px;min-width:280px;border-right:1px solid var(--border)">
          <div class="panel-header">
            <div class="panel-title"><span class="panel-title-icon">📂</span>Project Tables</div>
          </div>
          <div class="panel-body" id="tree-container" style="padding:8px"></div>
        </div>

        <!-- Right Panel: Graph -->
        <div class="panel" style="flex:1">
            <div class="panel-header">
              <div class="panel-title"><span class="panel-title-icon">🔗</span>Dependency Map</div>
              <div style="display:flex;align-items:center;gap:8px">
              <span class="badge badge-info">${sessionTotals.totalTables} tables</span>
              <span class="badge badge-primary">${sessionTotals.totalRelationships} links</span>
              </div>
            </div>
          <div class="panel-body no-pad" id="review-graph-area"></div>
        </div>
      </div>

      <!-- Vertical Resizer (between Top and Bottom sections) -->
      <div class="resizer-v" id="resizer-v-main"></div>

      <!-- Bottom Section: SQL + Description -->
      <div style="flex:1;display:flex;flex-direction:column;min-height:100px" id="bottom-section">
        <!-- File Selector Tabs -->
        <div style="display:flex;background:rgba(255,255,255,0.82);border-bottom:1px solid var(--border);padding:0 12px;overflow-x:auto;align-items:center;justify-content:space-between;backdrop-filter:blur(10px)">
          <div style="display:flex">
            ${files.map(f => `
              <div class="nav-step ${f.fileId === currentFileId ? 'active' : ''}" 
                   style="padding: 10px 16px; border-bottom: 2px solid ${f.fileId === currentFileId ? 'var(--primary)' : 'transparent'}; border-radius: 0; cursor: pointer; white-space: nowrap; font-size: 13px"
                   onclick="window.switchReviewFile('${f.fileId}')">
                📄 ${f.filename}
              </div>
            `).join('')}
          </div>
          
          <button class="btn btn-primary" id="migrate-dbt-btn" style="margin: 4px 8px; padding: 6px 12px; font-size: 12px; font-weight: 700; background: linear-gradient(135deg, var(--primary), var(--secondary)); border: none; box-shadow: 0 10px 22px rgba(47, 125, 91, 0.22)">
             ${state.dialect === 'powerbi' ? '📊 Convert to Power BI' : '✨ Migrate to DBT'}
          </button>
        </div>

        <!-- Editor Content -->
        <div style="flex:1;display:flex;overflow:hidden;min-height:0;position:relative">
          <!-- Left: Source QVS -->
          <div style="flex:1;display:flex;flex-direction:column;border-right:1px solid var(--border);overflow:hidden">
            <div class="panel-header" style="background:var(--bg-surface); min-height: 28px; padding: 2px 12px; border-bottom:1px solid var(--border)">
              <div class="panel-title" style="font-size: 10px; color: var(--text-dim)">SOURCE QLIK SCRIPT</div>
              <div class="toggle-group" style="transform: scale(0.75)">
                <button class="toggle-btn ${!isEdit ? 'active' : ''}" id="toggle-view">👁 View</button>
                <button class="toggle-btn ${isEdit ? 'active' : ''}" id="toggle-edit">✏️ Edit</button>
              </div>
            </div>
            <div id="sql-editor-container" style="flex:1;overflow:hidden"></div>
          </div>

          <!-- Horizontal Resizer (between Source and Target) -->
          <div class="resizer-h" id="resizer-h-editor"></div>

          <!-- Right: DBT SQL / Description -->
          <div style="flex:1;display:flex;flex-direction:column;overflow:hidden;background:var(--bg-primary)" id="target-pane">
             <div class="panel-header" style="background:rgba(255,255,255,0.78); min-height: 28px; padding: 0 4px; border-bottom:1px solid var(--border)">
                <div style="display:flex; gap: 2px">
                  <button class="tab-btn ${activeRightTab === 'sql' ? 'active' : ''}" id="tab-sql">💎 DBT SQL</button>
                  <button class="tab-btn ${activeRightTab === 'desc' ? 'active' : ''}" id="tab-desc">📝 Description</button>
                  <button class="tab-btn ${activeRightTab === 'chat' ? 'active' : ''}" id="tab-chat">💬 Refine</button>
                </div>
                <div style="display:flex; align-items:center; gap:8px; margin-left:12px">
                  <span style="font-size:10px; color:var(--text-secondary); text-transform:uppercase; letter-spacing:1px">Framework:</span>
                  <select id="dialect-selector" style="background:var(--bg-primary); color:var(--text-primary); border:1px solid var(--border); border-radius:4px; font-size:11px; padding:2px 4px; outline:none">
                    <option value="dbt" ${state.dialect === 'dbt' ? 'selected' : ''}>✨ Generic DBT</option>
                    <option value="snowflake" ${state.dialect === 'snowflake' ? 'selected' : ''}>❄️ Snowflake (DBT)</option>
                    <option value="bigquery" ${state.dialect === 'bigquery' ? 'selected' : ''}>☁️ BigQuery (DBT)</option>
                    <option value="databricks" ${state.dialect === 'databricks' ? 'selected' : ''}>🧱 Databricks (DBT)</option>
                    <option value="postgres" ${state.dialect === 'postgres' ? 'selected' : ''}>🐘 Postgres (DBT)</option>
                    <option value="powerbi" ${state.dialect === 'powerbi' ? 'selected' : ''}>📊 Power BI (M + DAX)</option>
                  </select>
                </div>
                <div class="toggle-group" style="transform: scale(0.75)">
                  <button class="btn btn-secondary" id="explain-code-btn" style="padding: 2px 8px; border-radius: 4px; font-weight: 600; background: var(--bg-surface); border: 1px solid var(--border); margin-right: 8px" title="Highlight code to explain">💡 Explain</button>
                  <button class="toggle-btn ${!rightEditMode ? 'active' : ''}" id="toggle-right-view">👁 View</button>
                  <button class="toggle-btn ${rightEditMode ? 'active' : ''}" id="toggle-right-edit">✏️ Edit</button>
                </div>
             </div>
             <div style="flex:1;display:flex;flex-direction:column;overflow:hidden;position:relative" id="right-output-area">
                ${(function() {
                  if (activeRightTab === 'desc') {
                    return rightEditMode
                      ? `<textarea class="description-textarea" id="right-description-textarea" style="flex:1;border:none;background:transparent;color:var(--text-primary);padding:20px;font-family:inherit;resize:none;outline:none">${escapeHtml(currentFileState.regeneratedText || currentFile.description)}</textarea>`
                      : `<div class="description-content" style="padding:20px">${markdownToHtml(currentFileState.regeneratedText || currentFile.description)}</div>`;
                  }
                  if (activeRightTab === 'chat') {
                    return renderChatPanel(currentFileState);
                  }
                  return `<div id="right-sql-editor-container" style="flex:1;overflow:hidden"></div>`;
                })()}
             </div>
          </div>
          
          <!-- AI Explanation Modal -->
          <div id="explainer-modal" class="modal-overlay" style="display:none">
            <div class="modal-content" style="max-width: 600px; width: 90%">
              <div class="modal-header">
                <h3>💡 Code Deep-Dive</h3>
                <button class="close-btn" id="close-explainer">×</button>
              </div>
              <div class="modal-body" id="explainer-body" style="font-size: 14px; line-height: 1.6; color: var(--text-primary)">
                <div class="spinner"></div> Thinking...
              </div>
            </div>
          </div>

          <!-- AI Loading Overlay -->
          <div id="ai-loading" class="loading-overlay" style="display:${state.isGenerating ? 'flex' : 'none'}">
            <div class="loading-card">
              <div class="spinner spinner-lg"></div>
              <h3>${state.dialect === 'powerbi' ? 'Converting to Power BI...' : 'Claude 3.5 is Migrating to DBT...'}</h3>
              <p>${state.dialect === 'powerbi' ? 'Generating Power Query M + DAX from Qlik logic' : 'Converting Qlik logic to modular CTEs'}</p>
            </div>
          </div>
        </div>
      </div>

      <!-- Footer -->
      <div class="review-footer" style="align-items:center;gap:10px;flex-wrap:wrap">
        <button class="btn btn-secondary" id="back-to-upload">← Back to Upload</button>
        <div style="flex:1"></div>

        <div id="validation-export-status" style="font-size:12px;color:var(--text-secondary);max-width:420px;text-align:right">
          ${!currentJobId ? 'Generate SQL first before exporting validation ZIP.' : ''}
          ${exportError ? `<div style="color:var(--danger,#dc2626);font-weight:700">Export failed: ${escapeHtml(exportError)}</div>` : ''}
          ${exportResult ? `
            <div style="line-height:1.5">
              <strong>ZIP:</strong> ${escapeHtml(exportResult.zipFileName || 'created')}
              · <strong>Dry run:</strong> ${escapeHtml(exportResult.dryRunResult?.status || 'not run')}
              ${exportResult.zipFileName ? `
                · <a
                    href="/api/download-validation-artifacts/${encodeURIComponent(exportResult.zipFileName)}"
                    target="_blank"
                    rel="noopener noreferrer"
                    style="color:var(--primary);font-weight:800"
                  >Download</a>
              ` : ''}
            </div>
          ` : ''}
        </div>

        <button
          class="btn btn-secondary btn-lg"
          id="export-validation-zip-btn"
          ${!currentJobId || isExporting ? 'disabled' : ''}
        >
          ${isExporting ? 'Exporting...' : '📦 Export validation ZIP'}
        </button>

        <button class="btn btn-success btn-lg" id="generate-btn">⚡ Finalize Migration →</button>
      </div>
    </div>
  `;

  // Initialize components
  initTreeView(state);
  const newGraphArea = document.getElementById('review-graph-area');
  if (existingGraphArea && newGraphArea && graphComponent) {
    newGraphArea.replaceWith(existingGraphArea);
  } else {
    initGraph(state);
  }
  initSQLEditor(currentFile, isEdit, rightEditMode, activeRightTab, currentFileId);
  setupToggle();
  setupResizers();
  setupButtons();
  // Wire chat panel if that tab is currently active
  if (activeRightTab === 'chat') setupChatPanel();
}

// Global switcher
window.switchReviewFile = (fileId) => {
  const state = store.get();
  const files = state.sessionStats?.files || [];
  const currentFileId = state.currentFileId || state.fileId;
  const currentFile = files.find(f => f.fileId === currentFileId) || {
    filename: state.filename,
    script: state.script,
    description: state.description,
    tables: state.tables
  };
  persistCurrentReviewState(currentFile);
  store.setCurrentFile(fileId, store.getFileReviewState(fileId) || {});
  const container = document.getElementById('review-page')?.parentElement;
  if (container) renderReviewPage(container);
};

function rerenderReviewControls() {
  const page = document.getElementById('review-page')?.parentElement;
  if (page) renderReviewPage(page, { preserveGraph: true });
}

function initTreeView(state) {
  const container = document.getElementById('tree-container');
  if (!container) return;

  const files = state.sessionStats?.files || [];
  
  // Custom Tree Rendering for Multi-File
  container.innerHTML = files.map(f => `
    <div class="tree-node">
      <div class="tree-node-header" style="background:var(--bg-elevated); margin-bottom: 4px; border-radius: 10px" onclick="window.switchReviewFile('${f.fileId}')">
        <span class="tree-node-icon">📄</span>
        <span class="tree-node-label" style="font-weight:700">${f.filename}</span>
      </div>
      <div class="tree-node-children" style="padding-left: 12px; margin-bottom: 12px">
        ${(f.tables || []).map(t => {
          const fieldCount = (t.fields || []).length;
          const rowCount = t.rows || 0;
          const metaLabel = rowCount > 0
            ? rowCount.toLocaleString() + ' rows'
            : fieldCount > 0
              ? fieldCount + ' field' + (fieldCount !== 1 ? 's' : '')
              : '';
          const isFactTable = (t.fields || []).filter(f => f.isKey).length > 1;
          return `
          <div class="tree-node-header" style="font-size: 11px">
            <span class="tree-node-icon">${isFactTable ? '📊' : '📋'}</span>
            <span class="tree-node-label">${t.name}</span>
            <span class="tree-node-meta">${metaLabel}</span>
          </div>
        `;
        }).join('')}
      </div>
    </div>
  `).join('');
}

function initGraph(state) {
  const graphArea = document.getElementById('review-graph-area');
  if (!graphArea) return;

  if (graphComponent) graphComponent.destroy();

  graphComponent = new GraphComponent(graphArea, {
    showUploadButtons: false,
  });

  graphComponent.update(state.graph);
}

function initSQLEditor(currentFile, isEdit, rightEditMode, activeRightTab, currentFileId) {
  const state = store.get();
  
  // Left Source Editor
  // Priority: user's edited version → original script from sessionStats → top-level state.script
  const leftContainer = document.getElementById('sql-editor-container');
  if (leftContainer) {
    if (sqlEditor) sqlEditor.destroy();
    const fileState = store.getFileReviewState(currentFileId) || {};
    const sourceValue = fileState.editedSql || currentFile.script || state.script || '';
    sqlEditor = new SQLEditor(leftContainer, {
      value: sourceValue,
      readOnly: !isEdit,
      onChange: (value) => { store.setFileReviewState(currentFileId, { editedSql: value }); }
    });
  }

  // Right Output Editor (if active)
  const rightContainer = document.getElementById('right-sql-editor-container');
  if (rightContainer && !['desc', 'chat'].includes(activeRightTab)) {
    if (rightSqlEditor) rightSqlEditor.destroy();

    // Use the file-level regenerated SQL, not the raw source script.
    // If nothing has been generated yet, show a dialect-appropriate placeholder.
    const fileState = store.getFileReviewState(currentFileId) || {};
    const regeneratedValue = fileState.regeneratedSql || state.regeneratedSql || '';
    const placeholder = state.dialect === 'powerbi'
      ? '// No output yet.\n// Select "📊 Convert to Power BI" and click the button above.'
      : '// No SQL generated yet.\n// Click "✨ Migrate to DBT" to run the AI migration.';

    rightSqlEditor = new SQLEditor(rightContainer, {
      value: regeneratedValue || placeholder,
      readOnly: !rightEditMode,
      onChange: (value) => { store.setFileReviewState(currentFileId, { regeneratedSql: value }); }
    });
  }
}

function setupToggle() {
  const viewBtn = document.getElementById('toggle-view');
  const editBtn = document.getElementById('toggle-edit');

  viewBtn?.addEventListener('click', () => {
    const fileId = store.get().currentFileId || store.get().fileId;
    store.setFileReviewState(fileId, { editMode: false });
    rerenderReviewControls();
  });

  editBtn?.addEventListener('click', () => {
    const fileId = store.get().currentFileId || store.get().fileId;
    store.setFileReviewState(fileId, { editMode: true });
    rerenderReviewControls();
  });

  // Right Side Toggles
  document.getElementById('toggle-right-view')?.addEventListener('click', () => {
    // Save current right description if it was being edited
    const textarea = document.getElementById('right-description-textarea');
    const fileId = store.get().currentFileId || store.get().fileId;
    if (textarea) store.setFileReviewState(fileId, { regeneratedText: textarea.value });

    store.setFileReviewState(fileId, { rightEditMode: false });
    rerenderReviewControls();
  });

  document.getElementById('toggle-right-edit')?.addEventListener('click', () => {
    const fileId = store.get().currentFileId || store.get().fileId;
    store.setFileReviewState(fileId, { rightEditMode: true });
    rerenderReviewControls();
  });

  document.getElementById('dialect-selector')?.addEventListener('change', (e) => {
    const newDialect = e.target.value;
    const prevDialect = store.get().dialect;
    store.set({ dialect: newDialect });

    // When switching to/from Power BI, clear stale generated output so the
    // previous dialect's SQL/M code doesn't show in the wrong editor.
    if (newDialect !== prevDialect) {
      const fileId = store.get().currentFileId || store.get().fileId;
      if (fileId) {
        store.setFileReviewState(fileId, {
          regeneratedSql: '',
          regeneratedText: '',
          regeneration: null,
          activeRightTab: 'sql',
        });
      }
      store.set({
        regeneratedSql: '',
        regeneratedText: '',
        regeneration: null,
      });
    }

    // Re-render the button label, loading overlay, and right editor placeholder
    rerenderReviewControls();
  });

  document.getElementById('explain-code-btn')?.addEventListener('click', async () => {
    const state = store.get();
    const selection = rightSqlEditor ? rightSqlEditor.getSelection() : '';
    
    if (!selection) {
      alert('Please highlight a snippet of code in the editor first!');
      return;
    }

    const modal = document.getElementById('explainer-modal');
    const body = document.getElementById('explainer-body');
    if (modal) modal.style.display = 'flex';
    if (body) body.innerHTML = '<div style="display:flex; flex-direction:column; align-items:center; gap:12px; padding:20px"><div class="spinner"></div>Analyzing logic mapping...</div>';

    try {
      const result = await api.explain(state.sessionId, selection);
      if (body) body.innerHTML = `<div class="animate-fade-in">${markdownToHtml(result.explanation)}</div>`;
    } catch (err) {
      if (body) body.innerHTML = `<div style="color:var(--danger)">Failed to explain code: ${err.message}</div>`;
    }
  });

  document.getElementById('close-explainer')?.addEventListener('click', () => {
    const modal = document.getElementById('explainer-modal');
    if (modal) modal.style.display = 'none';
  });
}

function setupResizers() {
  const vResizer = document.getElementById('resizer-v-main');
  const hResizer = document.getElementById('resizer-h-editor');
  const bottomSection = document.getElementById('bottom-section');
  const targetPane = document.getElementById('target-pane');
  const reviewPage = document.getElementById('review-page');

  if (vResizer) {
    vResizer.addEventListener('mousedown', (e) => {
      vResizer.classList.add('active');
      const startY = e.clientY;
      const startHeight = bottomSection.getBoundingClientRect().height;

      const onMouseMove = (moveEvent) => {
        const deltaY = startY - moveEvent.clientY;
        const newHeight = startHeight + deltaY;
        if (newHeight > 100) {
          bottomSection.style.flex = 'none';
          bottomSection.style.height = `${newHeight}px`;
        }
      };

      const onMouseUp = () => {
        vResizer.classList.remove('active');
        document.removeEventListener('mousemove', onMouseMove);
        document.removeEventListener('mouseup', onMouseUp);
      };

      document.addEventListener('mousemove', onMouseMove);
      document.addEventListener('mouseup', onMouseUp);
    });
  }

  if (hResizer) {
    hResizer.addEventListener('mousedown', (e) => {
      hResizer.classList.add('active');
      const startX = e.clientX;
      const startWidth = targetPane.getBoundingClientRect().width;

      const onMouseMove = (moveEvent) => {
        const deltaX = startX - moveEvent.clientX;
        const newWidth = startWidth + deltaX;
        if (newWidth > 150) {
          targetPane.style.flex = 'none';
          targetPane.style.width = `${newWidth}px`;
        }
      };

      const onMouseUp = () => {
        hResizer.classList.remove('active');
        document.removeEventListener('mousemove', onMouseMove);
        document.removeEventListener('mouseup', onMouseUp);
      };

      document.addEventListener('mousemove', onMouseMove);
      document.addEventListener('mouseup', onMouseUp);
    });
  }
}

function setupButtons() {
  // Back button
  document.getElementById('back-to-upload')?.addEventListener('click', () => {
    store.navigate('upload');
  });

  // Tab Switching
  document.getElementById('tab-sql')?.addEventListener('click', () => {
    const fileId = store.get().currentFileId || store.get().fileId;
    store.setFileReviewState(fileId, { activeRightTab: 'sql' });
    rerenderReviewControls();
  });

  document.getElementById('tab-desc')?.addEventListener('click', () => {
    const fileId = store.get().currentFileId || store.get().fileId;
    store.setFileReviewState(fileId, { activeRightTab: 'desc' });
    rerenderReviewControls();
  });

  document.getElementById('tab-chat')?.addEventListener('click', () => {
    const fileId = store.get().currentFileId || store.get().fileId;
    store.setFileReviewState(fileId, { activeRightTab: 'chat' });
    rerenderReviewControls();
    // Focus the chat input after render
    setTimeout(() => {
      document.getElementById('chat-input')?.focus();
      setupChatPanel();
    }, 50);
  });

  document.querySelectorAll('[data-history-id]').forEach(btn => {
    btn.addEventListener('click', () => {
      const fileId = store.get().currentFileId || store.get().fileId;
      const historyId = btn.getAttribute('data-history-id');
      const history = (store.get().regenerationHistory || []).find(item => item.id === historyId);
      if (!history) return;
      store.setFileReviewState(fileId, {
        regeneratedSql: history.regeneration?.sql || '',
        regeneratedText: history.regeneration?.description || '',
        regeneration: history.regeneration || null,
        activeRightTab: 'sql',
      });
      store.set({
        regeneration: history.regeneration || null,
      });
      const page = document.getElementById('review-page')?.parentElement;
      if (page) renderReviewPage(page);
    });
  });

  // Migrate to DBT button
  document.getElementById('migrate-dbt-btn')?.addEventListener('click', async () => {
    const btn = document.getElementById('migrate-dbt-btn');
    const isPowerBi = store.get().dialect === 'powerbi';
    if (btn) {
      btn.disabled = true;
      btn.innerHTML = `<span class="spinner"></span> ${isPowerBi ? 'Converting...' : 'Migrating...'}`;
    }
    try {
      await executeRegenerationFlow(true, false);
    } catch (err) {
      alert(`${isPowerBi ? 'Conversion' : 'Migration'} failed: ${err.message || 'Check the console for details.'}`);
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = isPowerBi ? '📊 Convert to Power BI' : '✨ Migrate to DBT';
      }
    }
  });

  // Export validation ZIP button
  document.getElementById('export-validation-zip-btn')?.addEventListener('click', async () => {
    const state = store.get();
    const fileId = state.currentFileId || state.fileId;
    const fileState = store.getFileReviewState(fileId) || {};

    const jobId =
      fileState.currentRegenerationJobId ||
      state.currentRegenerationJobId ||
      fileState.regeneration?.jobId ||
      fileState.regeneration?.job_id ||
      state.regeneration?.jobId ||
      state.regeneration?.job_id ||
      null;

    if (!jobId) {
      store.setFileReviewState(fileId, {
        validationExportError: 'No regeneration job ID found. Generate SQL first.',
      });
      rerenderReviewControls();
      return;
    }

    try {
      store.setFileReviewState(fileId, {
        isExportingValidationZip: true,
        validationExportError: '',
      });
      rerenderReviewControls();

      const res = await fetch('/api/export-validation-artifacts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          jobId,
          includeProjectScaffold: true,
          zip: true,
          dryRun: true,
        }),
      });

      const data = await res.json().catch(() => ({}));

      if (!res.ok || data.status === 'error') {
        throw new Error(data.message || data.error || `Export failed with HTTP ${res.status}`);
      }

      store.setFileReviewState(fileId, {
        isExportingValidationZip: false,
        validationExportResult: data,
        validationExportError: '',
      });
      rerenderReviewControls();
    } catch (err) {
      store.setFileReviewState(fileId, {
        isExportingValidationZip: false,
        validationExportError: err.message || String(err),
      });
      rerenderReviewControls();
    }
  });
  // Finalize button
  document.getElementById('generate-btn')?.addEventListener('click', async () => {
    const btn = document.getElementById('generate-btn');
    if (btn) {
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner"></span> Finalizing...';
    }

    try {
      const state = store.get();
      const files = state.sessionStats?.files || [];
      const currentFileId = state.currentFileId || state.fileId;
      const currentFile = files.find(f => f.fileId === currentFileId) || {
        filename: state.filename,
        script: state.script,
        description: state.description,
        tables: state.tables
      };
      if (!hasReviewChanges(currentFile)) {
        store.navigate('output');
        return;
      }
      await executeRegenerationFlow(true, true);
    } catch (err) {
      console.error('Finalization failed:', err);
      if (btn) {
          btn.disabled = false;
          btn.innerHTML = '🏁 Finalize Migration';
      }
      alert(`Finalization failed: ${err.message || 'Check the console for details.'}`);
    }
  });
}

async function loadSessionData(sessionId, container) {
  try {
    const data = await api.getModel(sessionId);

    // Enrich nodes
    const enrichedGraph = {
      ...data.graph,
      nodes: (data.graph?.nodes || []).map(n => ({
        ...n,
        type: (n.keyFields || []).length > 1 ? 'fact' : 'dimension',
      })),
    };

    store.set({
      filename: data.filename,
      fileId: data.fileId,
      currentFileId: data.fileId,
      tables: data.tables || [],
      associations: data.associations || [],
      graph: enrichedGraph,
      script: data.script || '',
      sqlSections: data.sqlSections || [],
      description: data.description || '',
      metadata: data.metadata,
      generationPlan: data.generationPlan || [],
      generationPlanText: data.generationPlanText || '',
      regeneration: data.regeneration || null,
      regenerationHistory: data.regenerationHistory || [],
      sessionStats: data.sessionStats
    });

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
        activeRightTab: 'sql',
      });
      store.setFileReviewBaseline(file.fileId, {
        sourceSql: file.script || '',
        regenSql: '',
        regenText: '',
      });
    });

    store.setCurrentFile(data.fileId, {
      editedSql: data.editedSql || data.script || '',
      editedText: data.editedText || data.description || '',
      regeneratedSql: data.regeneratedSql || '',
      regeneratedText: data.regeneratedText || '',
      regeneratedLineage: data.regeneratedLineage || '',
      generationPlan: data.generationPlan || [],
      generationPlanText: data.generationPlanText || '',
      regeneration: data.regeneration || null,
      regenerationHistory: data.regenerationHistory || [],
      activeRightTab: 'sql',
    });

    renderReviewPage(container);
  } catch (err) {
    console.error('Failed to load session:', err);
    store.navigate('upload');
  }
}

// ─── Chat / Iterative Refinement Panel ───────────────────────────────────────

/**
 * Render the chat panel HTML.
 * Chat history is stored in fileState.chatHistory as [{role, content}].
 */
function renderChatPanel(fileState) {
  const history = fileState.chatHistory || [];

  const messagesHtml = history.length
    ? history.map(msg => {
        const isUser = msg.role === 'user';
        return `
          <div style="display:flex;flex-direction:column;align-items:${isUser ? 'flex-end' : 'flex-start'};margin-bottom:10px">
            <div style="
              max-width:85%;
              padding:8px 12px;
              border-radius:${isUser ? '12px 12px 2px 12px' : '12px 12px 12px 2px'};
              background:${isUser ? 'var(--primary)' : 'var(--bg-elevated)'};
              color:${isUser ? '#fff' : 'var(--text-primary)'};
              font-size:13px;
              line-height:1.5;
              white-space:pre-wrap;
              word-break:break-word;
            ">${escapeHtml(msg.content)}</div>
            <span style="font-size:10px;color:var(--text-dim);margin-top:3px">${isUser ? 'You' : '🤖 AI'}</span>
          </div>`;
      }).join('')
    : `<div style="color:var(--text-dim);font-size:12px;text-align:center;padding:24px 16px;line-height:1.7">
         <p style="font-size:24px;margin:0 0 8px">💬</p>
         <p style="margin:0"><strong>Refine your migration</strong></p>
         <p style="margin:6px 0 0">Type an instruction and the AI will apply it to the current SQL draft.</p>
         <p style="margin:8px 0 0;font-size:11px;color:var(--text-dim)">
           Examples:<br>
           "Add a filter for active customers only"<br>
           "Rename OrderID to order_key"<br>
           "Add a CTE that calculates monthly totals"
         </p>
       </div>`;

  return `
    <div id="chat-panel" style="flex:1;display:flex;flex-direction:column;overflow:hidden;background:var(--bg-surface)">
      <!-- Message history -->
      <div id="chat-messages" style="flex:1;overflow-y:auto;padding:14px 12px;display:flex;flex-direction:column">
        ${messagesHtml}
      </div>

      <!-- Input row -->
      <div style="border-top:1px solid var(--border);padding:10px 12px;display:flex;gap:8px;align-items:flex-end;background:rgba(255,255,255,0.82);backdrop-filter:blur(10px)">
        <textarea
          id="chat-input"
          placeholder="Describe a change to make to the SQL…"
          rows="2"
          style="flex:1;background:var(--bg-surface);color:var(--text-primary);border:1px solid var(--border);border-radius:6px;padding:8px 10px;font-size:13px;font-family:inherit;resize:none;outline:none;line-height:1.5"
        ></textarea>
        <button
          id="chat-send-btn"
          class="btn btn-primary"
          style="padding:8px 14px;font-size:13px;align-self:flex-end;white-space:nowrap"
        >Send ↵</button>
      </div>
    </div>`;
}

/**
 * Wire up the chat panel's send button and Enter-key shortcut.
 * Called from setupButtons() whenever the chat tab is active.
 */
function setupChatPanel() {
  const sendBtn = document.getElementById('chat-send-btn');
  const input = document.getElementById('chat-input');
  if (!sendBtn || !input) return;

  // Scroll message list to bottom
  const messages = document.getElementById('chat-messages');
  if (messages) messages.scrollTop = messages.scrollHeight;

  async function sendMessage() {
    const text = input.value.trim();
    if (!text) return;

    const state = store.get();
    const fileId = state.currentFileId || state.fileId;
    const fileState = store.getFileReviewState(fileId) || {};

    // Append user message to history immediately
    const history = [...(fileState.chatHistory || []), { role: 'user', content: text }];
    store.setFileReviewState(fileId, { chatHistory: history });
    input.value = '';
    rerenderReviewControls();
    // Re-focus after rerender
    setTimeout(() => document.getElementById('chat-input')?.focus(), 30);

    // Show typing indicator
    const messagesEl = document.getElementById('chat-messages');
    if (messagesEl) {
      const typing = document.createElement('div');
      typing.id = 'chat-typing';
      typing.style.cssText = 'color:var(--text-dim);font-size:12px;padding:4px 8px';
      typing.textContent = '🤖 Thinking…';
      messagesEl.appendChild(typing);
      messagesEl.scrollTop = messagesEl.scrollHeight;
    }

    // Disable send while waiting
    sendBtn.disabled = true;

    try {
      const currentSql = rightSqlEditor ? rightSqlEditor.getValue() : fileState.regeneratedSql || '';
      const currentDesc = fileState.regeneratedText || '';

      // Create a streaming AI bubble immediately
      let streamBuffer = '';
      const tempHistory = [
        ...(store.getFileReviewState(fileId)?.chatHistory || []),
        { role: 'assistant', content: '' },
      ];
      store.setFileReviewState(fileId, { chatHistory: tempHistory });
      rerenderReviewControls();
      // Remove typing indicator since streaming starts
      document.getElementById('chat-typing')?.remove();
      
      // Re-setup chat panel after rerender so we can target the bubble
      setTimeout(() => {
        const messagesEl = document.getElementById('chat-messages');
        if (messagesEl) messagesEl.scrollTop = messagesEl.scrollHeight;
      }, 30);

      await api.chatStream(
        state.sessionId,
        text,
        currentSql,
        currentDesc,
        state.dialect || 'dbt',
        {
          onToken(content) {
            streamBuffer += content;
            // Update the last chat bubble in real-time
            const messagesEl = document.getElementById('chat-messages');
            if (messagesEl) {
              const bubbles = messagesEl.querySelectorAll('div[style*="flex-start"] > div');
              const lastBubble = bubbles[bubbles.length - 1];
              if (lastBubble) {
                lastBubble.textContent = streamBuffer;
                messagesEl.scrollTop = messagesEl.scrollHeight;
              }
            }
          },
          onDone(data) {
            const structured = data.regeneration || {};
            const aiReply = structured.sql
              ? streamBuffer || 'Updated SQL with your change. Switch to the 💎 DBT SQL tab to review.'
              : streamBuffer || 'No SQL changes were returned. Try rephrasing your instruction.';

            const updatedHistory = [
              ...(store.getFileReviewState(fileId)?.chatHistory || []).slice(0, -1),
              { role: 'assistant', content: aiReply },
            ];
            store.setFileReviewState(fileId, {
              chatHistory: updatedHistory,
              regeneratedSql: structured.sql || currentSql,
              regeneratedText: structured.description || currentDesc,
              regeneratedLineage: structured.lineage || fileState.regeneratedLineage || '',
              regeneration: structured,
            });
            store.set({
              regeneration: structured,
            });
          },
          onError(err) {
            throw err;
          },
        }
      );
    } catch (err) {
      const errHistory = [
        ...(store.getFileReviewState(fileId)?.chatHistory || []),
        { role: 'assistant', content: `Error: ${err.message}` },
      ];
      store.setFileReviewState(fileId, { chatHistory: errHistory });
    } finally {
      sendBtn.disabled = false;
      rerenderReviewControls();
      setTimeout(() => {
        const el = document.getElementById('chat-messages');
        if (el) el.scrollTop = el.scrollHeight;
        document.getElementById('chat-input')?.focus();
      }, 30);
    }
  }

  sendBtn.addEventListener('click', sendMessage);

  // Ctrl+Enter or Cmd+Enter to send
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      sendMessage();
    }
  });
}

export function destroyReviewPage() {
  if (graphComponent) { graphComponent.destroy(); graphComponent = null; }
  if (sqlEditor) { sqlEditor.destroy(); sqlEditor = null; }
}
