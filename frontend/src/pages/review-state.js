export function hasReviewChangesSnapshot(baseline = null, current = {}) {
  if (!baseline) return true;
  return (
    (current.sourceSql || '') !== (baseline.sourceSql || '') ||
    (current.regenSql || '') !== (baseline.regenSql || '') ||
    (current.regenText || '') !== (baseline.regenText || '')
  );
}

export function normalizeRegenerationRecord(record = null) {
  if (!record) return null;
  const payload = record.regeneration || record;
  return {
    id: record.id || null,
    sessionId: record.sessionId || null,
    fileId: record.fileId || null,
    promptVersion: record.promptVersion || payload.promptVersion || '',
    model: record.model || payload.model || '',
    status: record.status || payload.status || 'complete',
    triggerMigration: !!record.triggerMigration,
    sql: payload.sql || '',
    description: payload.description || '',
    warnings: payload.warnings || [],
    errorText: record.errorText || '',
    createdAt: record.createdAt || '',
    completedAt: record.completedAt || '',
  };
}
