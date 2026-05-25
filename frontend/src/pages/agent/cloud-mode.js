import { api } from '../../api.js';
import { store } from '../../store.js';
import { readConfig } from './form-mode.js';
import { agentState } from './state.js';
import { updateStatus } from './status-mode.js';
import { setBusy } from './utils.js';

let pollTimer = null;

export async function testConnection() {
  const btn = document.getElementById('test-dbt-btn');
  setBusy(btn, true, 'Testing...');
  agentState.error = '';
  try {
    const result = await api.testDbtCloudConnection(readConfig());
    agentState.connected = true;
    agentState.projects = result.projects || [];
    agentState.jobs = result.jobs || [];
  } catch (err) {
    agentState.connected = false;
    agentState.error = err.message;
  } finally {
    setBusy(btn, false, 'Test Login');
    updateStatus();
  }
}

export async function runAgent() {
  const btn = document.getElementById('run-dbt-btn');
  setBusy(btn, true, 'Starting...');
  agentState.error = '';
  try {
    const result = await api.runDbtCloudJob({
      ...readConfig(),
      sessionId: store.get().sessionId,
    });
    agentState.connected = true;
    agentState.runId = result.runId;
    agentState.status = result.statusHumanized || result.status || 'queued';
    agentState.statusDetail = result.href || '';
    startPolling();
  } catch (err) {
    agentState.error = err.message;
  } finally {
    setBusy(btn, false, 'Run Agent');
    updateStatus();
  }
}

function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    if (!agentState.runId) return;
    try {
      const result = await api.getDbtCloudRunStatus({
        ...readConfig(),
        runId: agentState.runId,
      });
      agentState.status = result.statusHumanized || result.status || '';
      agentState.statusDetail = result.finishedAt ? `finished ${result.finishedAt}` : (result.href || '');
      updateStatus();
      if (['Success', 'Error', 'Cancelled'].includes(agentState.status)) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
    } catch (err) {
      agentState.error = err.message;
      updateStatus();
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }, 5000);
}

export function destroyAgentPage() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
}
