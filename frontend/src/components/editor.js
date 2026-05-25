/**
 * QVF Decoder — CodeMirror SQL Editor Component
 */
import { EditorView, keymap, lineNumbers, highlightActiveLine, highlightSpecialChars } from '@codemirror/view';
import { EditorState, Compartment } from '@codemirror/state';
import { sql } from '@codemirror/lang-sql';
import { defaultKeymap, history, historyKeymap } from '@codemirror/commands';
import { syntaxHighlighting, defaultHighlightStyle, bracketMatching } from '@codemirror/language';

export class SQLEditor {
  constructor(container, options = {}) {
    this.container = container;
    this.options = {
      readOnly: true,
      onChange: null,
      value: '',
      ...options,
    };
    this.view = null;
    // Compartment lets us reconfigure readOnly without rebuilding the editor
    this._readOnlyCompartment = new Compartment();
    this._init();
  }

  _init() {
    this.container.innerHTML = '';

    const extensions = [
      lineNumbers(),
      highlightActiveLine(),
      highlightSpecialChars(),
      history(),
      bracketMatching(),
      sql(),
      syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
      keymap.of([...defaultKeymap, ...historyKeymap]),
      // Wrap readOnly in a Compartment so it can be swapped at runtime
      this._readOnlyCompartment.of(EditorState.readOnly.of(!!this.options.readOnly)),
      EditorView.theme({
        '&': {
          height: '100%',
          fontSize: '13px',
          backgroundColor: 'var(--bg-primary)',
          color: 'var(--text-primary)',
        },
        '.cm-focused': {
          outline: 'none',
        },
        '.cm-scroller, .cm-gutters': {
          backgroundColor: 'transparent',
        },
        '.cm-scroller': {
          fontFamily: "'JetBrains Mono', 'Fira Code', monospace",
          overflow: 'auto',
        },
        '.cm-content': {
          caretColor: 'var(--primary)',
        },
        '.cm-gutters': {
          color: 'var(--text-dim)',
          borderRight: '1px solid var(--border)',
        },
        '.cm-activeLine, .cm-activeLineGutter': {
          backgroundColor: 'rgba(37, 99, 235, 0.06)',
        },
        '.cm-selectionBackground, ::selection': {
          backgroundColor: 'rgba(37, 99, 235, 0.18) !important',
        },
      }),
    ];

    if (this.options.onChange) {
      extensions.push(EditorView.updateListener.of((update) => {
        if (update.docChanged) {
          this.options.onChange(update.state.doc.toString());
        }
      }));
    }

    const state = EditorState.create({
      doc: this.options.value,
      extensions,
    });

    this.view = new EditorView({
      state,
      parent: this.container,
    });
  }

  getValue() {
    return this.view ? this.view.state.doc.toString() : '';
  }

  getSelection() {
    if (!this.view) return '';
    const state = this.view.state;
    return state.sliceDoc(state.selection.main.from, state.selection.main.to);
  }

  setValue(text) {
    if (!this.view) return;
    const transaction = this.view.state.update({
      changes: {
        from: 0,
        to: this.view.state.doc.length,
        insert: text,
      },
    });
    this.view.dispatch(transaction);
  }

  setReadOnly(readOnly) {
    if (!this.view) return;
    this.options.readOnly = readOnly;
    // Reconfigure via Compartment — preserves cursor, scroll, and undo history
    this.view.dispatch({
      effects: this._readOnlyCompartment.reconfigure(EditorState.readOnly.of(!!readOnly)),
    });
  }

  focus() {
    if (this.view) this.view.focus();
  }

  scrollToText(text) {
    if (!this.view || !text) return false;
    const doc = this.view.state.doc.toString();
    const target = text.trim();
    if (!target) return false;

    let index = doc.toLowerCase().indexOf(target.toLowerCase());
    if (index < 0) {
      const compact = target.replace(/\s+/g, ' ').trim();
      index = doc.toLowerCase().indexOf(compact.toLowerCase());
      if (index < 0) return false;
      text = compact;
    }

    const from = index;
    const to = index + text.length;
    this.view.dispatch({
      selection: { anchor: from, head: to },
      scrollIntoView: true,
    });
    this.view.focus();
    return true;
  }

  destroy() {
    if (this.view) {
      this.view.destroy();
      this.view = null;
    }
  }
}

/**
 * Syntax-highlight SQL for static display (no CodeMirror needed).
 *
 * Strategy: tokenise in a single pass using a combined regex so that
 * comments, strings, keywords, and numbers are each matched exactly once
 * and never double-highlighted.
 */
export function highlightSQL(sql) {
  if (!sql) return '';

  // Combined pattern — order matters: comments and strings must come first
  // so keywords inside them are not highlighted.
  const TOKEN_RE = /(\/\/[^\n]*)|((?:\[[^\]]*\])|(?:'[^']*'))|\b(SELECT|FROM|WHERE|JOIN|LEFT|RIGHT|INNER|OUTER|ON|AND|OR|NOT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|TABLE|INDEX|VIEW|LOAD|INTO|VALUES|SET|GROUP|BY|ORDER|HAVING|UNION|ALL|AS|IN|IS|NULL|BETWEEN|LIKE|EXISTS|CASE|WHEN|THEN|ELSE|END|DISTINCT|TOP|LIMIT|OFFSET|ASC|DESC|PRIMARY|KEY|FOREIGN|REFERENCES|CONSTRAINT|DEFAULT|CHECK|UNIQUE|RESIDENT|QVD|MAPPING|CONCATENATE|NOCONCATENATE|LET|IF|ELSEIF|ENDIF|SUB|ENDSUB|CALL|DO|WHILE|LOOP|NEXT|FOR|EACH|EXIT|STORE)\b|(&amp;|&lt;|&gt;)|(\b\d+(?:\.\d+)?\b)/gi;

  // First escape HTML entities
  const escaped = sql
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');

  return escaped.replace(TOKEN_RE, (match, comment, str, keyword, entity, number) => {
    if (comment)  return `<span class="sql-comment">${match}</span>`;
    if (str)      return `<span class="sql-bracket">${match}</span>`;
    if (keyword)  return `<span class="sql-keyword">${match}</span>`;
    if (entity)   return match; // already-escaped HTML entity — leave as-is
    if (number)   return `<span class="sql-number">${match}</span>`;
    return match;
  });
}
