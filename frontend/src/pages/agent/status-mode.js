import { agentState } from './state.js';
import { escapeHtml } from '../../utils.js';

export function renderAgentStatus() {
  if (agentState.error) return escapeHtml(agentState.error);
  if (agentState.runId) {
    const detail = agentState.statusDetail ? ` - ${escapeHtml(agentState.statusDetail)}` : '';
    return `Run ${escapeHtml(String(agentState.runId))}: ${escapeHtml(agentState.status || 'queued')}${detail}`;
  }
  if (agentState.connected) {
    const projectCount = agentState.projects.length;
    const jobCount = agentState.jobs.length;
    return `Connection verified. Found ${projectCount} project${projectCount === 1 ? '' : 's'} and ${jobCount} job${jobCount === 1 ? '' : 's'}.`;
  }
  return 'Enter a dbt Cloud token, account ID, and job ID. The token is only sent for this request.';
}

export function updateStatus() {
  const status = document.getElementById('agent-status');
  if (!status) return;
  status.classList.toggle('error', !!agentState.error);
  status.innerHTML = renderAgentStatus();
}
