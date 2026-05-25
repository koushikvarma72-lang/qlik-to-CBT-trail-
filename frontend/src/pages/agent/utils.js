/**
 * Agent-page utilities.
 * escapeHtml is re-exported from the shared utils module so all pages use
 * a single implementation.
 */
export { escapeHtml } from '../../utils.js';

export function setBusy(btn, busy, text) {
  if (!btn) return;
  btn.disabled = busy;
  btn.textContent = text;
}
