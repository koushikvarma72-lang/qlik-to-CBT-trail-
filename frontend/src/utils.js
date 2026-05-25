/**
 * QVF Decoder — Shared Utilities
 * Single source of truth for helpers used across multiple pages/components.
 */

/**
 * Escape a value for safe HTML insertion.
 * Handles null / undefined gracefully.
 */
export function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

/**
 * Convert Markdown to HTML.
 * Handles headers, bold, italic, inline code, fenced code blocks,
 * unordered lists, ordered lists, horizontal rules, and simple tables.
 * Operates on already-escaped HTML so it is XSS-safe.
 */
export function markdownToHtml(md) {
  if (!md) return '<div style="color:var(--text-dim);padding:24px">No description available</div>';

  let html = escapeHtml(md);

  // Fenced code blocks (``` ... ```) — must come before inline code
  html = html.replace(/```[\w]*\n?([\s\S]*?)```/g, (_, code) =>
    `<pre><code>${code.trim()}</code></pre>`
  );

  // Headers
  html = html.replace(/^### (.+)$/gm, '<h3>$1</h3>');
  html = html.replace(/^## (.+)$/gm, '<h2>$1</h2>');
  html = html.replace(/^# (.+)$/gm, '<h1>$1</h1>');

  // Horizontal rules
  html = html.replace(/^---+$/gm, '<hr>');

  // Bold (must come before italic)
  html = html.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');

  // Italic
  html = html.replace(/\*([^*\n]+)\*/g, '<em>$1</em>');

  // Inline code (after fenced blocks so we don't double-process)
  html = html.replace(/`([^`\n]+)`/g, '<code>$1</code>');

  // Simple Markdown tables (| col | col | rows)
  html = html.replace(/((?:^\|.+\|\n?)+)/gm, (tableBlock) => {
    const rows = tableBlock.trim().split('\n').filter(r => r.trim());
    const dataRows = rows.filter(r => !/^\|[\s|:-]+\|$/.test(r.trim()));
    if (dataRows.length < 1) return tableBlock;
    const parseRow = (row, tag) =>
      '<tr>' + row.split('|').slice(1, -1)
        .map(cell => `<${tag}>${cell.trim()}</${tag}>`).join('') + '</tr>';
    const [header, ...body] = dataRows;
    return (
      `<table><thead>${parseRow(header, 'th')}</thead>` +
      `<tbody>${body.map(r => parseRow(r, 'td')).join('')}</tbody></table>`
    );
  });

  // Indented list items (must come before top-level list items)
  html = html.replace(/^  - (.+)$/gm, '<li class="li-indent">$1</li>');

  // Top-level unordered list items
  html = html.replace(/^- (.+)$/gm, '<li>$1</li>');

  // Ordered list items (1. 2. 3.)
  html = html.replace(/^\d+\. (.+)$/gm, '<li class="li-ordered">$1</li>');

  // Wrap consecutive <li> runs — ordered vs unordered
  html = html.replace(/((?:<li(?:\s[^>]*)?>[\s\S]*?<\/li>\n?)+)/g, (block) => {
    if (block.includes('li-ordered')) {
      return `<ol>${block.replace(/\s*class="li-ordered"/g, '')}</ol>`;
    }
    return `<ul>${block}</ul>`;
  });

  // Paragraphs — lines not already wrapped in a block tag
  html = html.replace(/^(?!<[hupot]|<li|<hr|<pre|<table)(.*\S.*)$/gm, '<p>$1</p>');

  // Remove empty paragraphs
  html = html.replace(/<p>\s*<\/p>/g, '');

  return html;
}
