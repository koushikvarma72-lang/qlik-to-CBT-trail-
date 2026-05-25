const FORM_CACHE_KEY = 'qvf_dbt_agent_form';
const FORM_FIELD_IDS = [
  'dbt-base-url',
  'dbt-account-id',
  'dbt-project-id',
  'dbt-job-id',
  'dbt-commands',
];

export function readConfig() {
  cacheForm();
  return {
    baseUrl: document.getElementById('dbt-base-url')?.value.trim(),
    token: document.getElementById('dbt-token')?.value.trim(),
    accountId: document.getElementById('dbt-account-id')?.value.trim(),
    projectId: document.getElementById('dbt-project-id')?.value.trim(),
    jobId: document.getElementById('dbt-job-id')?.value.trim(),
    commands: (document.getElementById('dbt-commands')?.value || '')
      .split('\n')
      .map(line => line.trim())
      .filter(Boolean),
  };
}

export function cacheForm() {
  const form = {};
  FORM_FIELD_IDS.forEach(id => {
    const el = document.getElementById(id);
    if (el) form[id] = el.value;
  });
  sessionStorage.setItem(FORM_CACHE_KEY, JSON.stringify(form));
}

export function restoreCachedForm() {
  const cached = JSON.parse(sessionStorage.getItem(FORM_CACHE_KEY) || '{}');
  Object.entries(cached).forEach(([id, value]) => {
    const el = document.getElementById(id);
    if (el) el.value = value;
  });
}

export function bindFormCache() {
  document.querySelectorAll('#agent-page input, #agent-page textarea').forEach(input => {
    input.addEventListener('input', cacheForm);
  });
}
