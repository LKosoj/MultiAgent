"use client";

import { useState } from "react";
import { isRecord } from "../../lib/textToSqlContracts";
import {
  parseTextToSqlGlossaryEntries,
  type TextToSqlColumnMetadata,
  type TextToSqlGlossaryEntry,
  type TextToSqlMetadataView,
  type TextToSqlSemanticFact,
  parseTextToSqlMetadataView,
} from "../../lib/textToSqlMetadataContracts";
import {
  createTextToSqlClient,
  type TextToSqlConnection,
} from "../../lib/textToSqlClient";

type Props = {
  connectionRef: string;
  setConnectionRef: (value: string) => void;
  connections: TextToSqlConnection[];
  textToSqlClient: ReturnType<typeof createTextToSqlClient>;
  isBusy: boolean;
  isAdmin: boolean;
  setError: (msg: string | null) => void;
  metadataView: TextToSqlMetadataView | null;
  setMetadataView: (value: TextToSqlMetadataView | null) => void;
};

// Only present when the field was actually touched by the operator, so a
// partial-update request (§1.3 of the design doc) can tell "not changed"
// apart from "changed to empty string".
type TableEdit = {
  description?: string;
  columns: Record<string, { description?: string; examples?: string }>;
};
type TableEditMap = Record<string, TableEdit>;

type GlossaryDraftEntry = {
  // Stable per-row key: rows can be removed from the middle of the list, so
  // the array index would make React reuse the wrong DOM inputs.
  id: number;
  term: string;
  synonymsText: string;
  table: string;
  column: string;
  kind: "" | "dimension" | "measure" | "filter_value" | "entity";
  note: string;
};

function parseExamplesInput(text: string): string[] {
  return text.split(",").map((item) => item.trim()).filter((item) => item.length > 0);
}

function formatExamples(examples: unknown[]): string {
  return examples.map((item) => String(item)).join(", ");
}

let nextGlossaryRowId = 1;

function newGlossaryRowId(): number {
  return nextGlossaryRowId++;
}

function toDraftEntry(entry: TextToSqlGlossaryEntry): GlossaryDraftEntry {
  return {
    id: newGlossaryRowId(),
    term: entry.term,
    synonymsText: entry.synonyms.join(", "),
    table: entry.table,
    column: entry.column ?? "",
    kind: entry.kind ?? "",
    note: entry.note ?? "",
  };
}

function fromDraftEntry(draft: GlossaryDraftEntry): TextToSqlGlossaryEntry {
  return {
    term: draft.term.trim(),
    synonyms: parseExamplesInput(draft.synonymsText),
    table: draft.table.trim(),
    column: draft.column.trim() ? draft.column.trim() : null,
    kind: draft.kind || null,
    note: draft.note.trim() ? draft.note.trim() : null,
  };
}

// Applies a successful save_descriptions response to the in-memory view so
// the form keeps showing what was just persisted, without a full reload
// round-trip (which would also re-run the live-schema introspection, see
// §1.2/§7.2 of the design doc).
function applySavedTableEdits(
  view: TextToSqlMetadataView,
  edits: TableEditMap,
  schemaDigest: string,
): TextToSqlMetadataView {
  const tables = { ...view.tables };
  for (const [tableFqn, edit] of Object.entries(edits)) {
    const table = tables[tableFqn];
    if (!table) continue;
    const description = "description" in edit ? (edit.description ?? "") : table.description;
    const columns = { ...table.columns };
    for (const [column, columnEdit] of Object.entries(edit.columns)) {
      const existingColumn = columns[column];
      if (!existingColumn) continue;
      const columnDescription = "description" in columnEdit
        ? (columnEdit.description ?? "")
        : existingColumn.description;
      const examples = "examples" in columnEdit
        ? parseExamplesInput(columnEdit.examples ?? "")
        : existingColumn.examples;
      const merged: TextToSqlColumnMetadata = {
        ...existingColumn,
        description: columnDescription,
        description_source: columnDescription ? "file" : "none",
        examples,
        examples_source: "examples" in columnEdit
          ? (examples.length > 0 ? "file" : "none")
          : existingColumn.examples_source,
      };
      columns[column] = merged;
    }
    tables[tableFqn] = {
      ...table,
      description,
      description_source: description ? "file" : "none",
      columns,
    };
  }
  return { ...view, schema_digest: schemaDigest, tables };
}

function isVersionConflictMessage(message: string): boolean {
  return message.toLowerCase().includes("version conflict");
}

export function TextToSqlMetadataTab({
  connectionRef,
  setConnectionRef,
  connections,
  textToSqlClient,
  isBusy,
  isAdmin,
  setError,
  metadataView,
  setMetadataView,
}: Props) {
  const [tableEdits, setTableEdits] = useState<TableEditMap>({});
  const [glossaryDraft, setGlossaryDraft] = useState<GlossaryDraftEntry[]>(
    () => metadataView?.glossary.entries.map(toDraftEntry) ?? [],
  );
  const [factsFilter, setFactsFilter] = useState("");
  const [conflict, setConflict] = useState(false);

  const handleSaveError = (err: unknown) => {
    const message = err instanceof Error ? err.message : "Не удалось сохранить метаданные";
    if (isVersionConflictMessage(message)) {
      setConflict(true);
      return;
    }
    setError(message);
  };

  const loadMetadata = async () => {
    if (!connectionRef) return;
    setError(null);
    setConflict(false);
    try {
      const view = parseTextToSqlMetadataView(await textToSqlClient.loadMetadata(connectionRef));
      setMetadataView(view);
      setTableEdits({});
      setGlossaryDraft(view.glossary.entries.map(toDraftEntry));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось получить метаданные");
    }
  };

  const updateTableDescription = (tableFqn: string, value: string) => {
    setTableEdits((prev) => ({
      ...prev,
      [tableFqn]: { ...(prev[tableFqn] ?? { columns: {} }), description: value },
    }));
  };

  const updateColumnField = (
    tableFqn: string,
    column: string,
    field: "description" | "examples",
    value: string,
  ) => {
    setTableEdits((prev) => {
      const table = prev[tableFqn] ?? { columns: {} };
      const columnEdit = table.columns[column] ?? {};
      return {
        ...prev,
        [tableFqn]: {
          ...table,
          columns: { ...table.columns, [column]: { ...columnEdit, [field]: value } },
        },
      };
    });
  };

  const saveDescriptions = async () => {
    if (!metadataView || Object.keys(tableEdits).length === 0) return;
    setError(null);
    const tables = Object.entries(tableEdits).map(([tableFqn, edit]) => {
      const columns = Object.entries(edit.columns).map(([column, columnEdit]) => {
        const entry: Record<string, unknown> = { column };
        if ("description" in columnEdit) entry.description = columnEdit.description;
        if ("examples" in columnEdit) entry.examples = parseExamplesInput(columnEdit.examples ?? "");
        return entry;
      });
      const entry: Record<string, unknown> = { table_fqn: tableFqn };
      if ("description" in edit) entry.description = edit.description;
      if (columns.length > 0) entry.columns = columns;
      return entry;
    });

    try {
      const response = await textToSqlClient.saveMetadataDescriptions({
        connection_ref: metadataView.connection_ref,
        expected_schema_digest: metadataView.schema_digest,
        tables,
      });
      const schemaDigest = isRecord(response) && typeof response.schema_digest === "string"
        ? response.schema_digest
        : metadataView.schema_digest ?? "";
      setMetadataView(applySavedTableEdits(metadataView, tableEdits, schemaDigest));
      setTableEdits({});
    } catch (err) {
      handleSaveError(err);
    }
  };

  const addGlossaryRow = () => {
    setGlossaryDraft((prev) => [
      ...prev,
      { id: newGlossaryRowId(), term: "", synonymsText: "", table: "", column: "", kind: "", note: "" },
    ]);
  };

  const removeGlossaryRow = (index: number) => {
    setGlossaryDraft((prev) => prev.filter((_, rowIndex) => rowIndex !== index));
  };

  const updateGlossaryRow = (index: number, patch: Partial<GlossaryDraftEntry>) => {
    setGlossaryDraft((prev) => prev.map((row, rowIndex) => (rowIndex === index ? { ...row, ...patch } : row)));
  };

  const saveGlossary = async () => {
    if (!metadataView) return;
    setError(null);
    const entries = glossaryDraft.map(fromDraftEntry);
    try {
      const response = await textToSqlClient.saveMetadataGlossary({
        connection_ref: metadataView.connection_ref,
        expected_glossary_digest: metadataView.glossary.digest,
        entries,
      });
      const digest = isRecord(response) && typeof response.glossary_digest === "string"
        ? response.glossary_digest
        : metadataView.glossary.digest;
      // The server trims and deduplicates synonyms; show what was actually
      // stored rather than the raw draft.
      const savedEntries = isRecord(response) && Array.isArray(response.entries)
        ? parseTextToSqlGlossaryEntries(response.entries)
        : entries;
      setMetadataView({
        ...metadataView,
        glossary: { digest, profile_exists: true, entries: savedEntries },
      });
      setGlossaryDraft(savedEntries.map(toDraftEntry));
    } catch (err) {
      handleSaveError(err);
    }
  };

  const toggleFactStatus = async (fact: TextToSqlSemanticFact, nextStatus: "approved" | "rejected") => {
    if (!metadataView) return;
    setError(null);
    try {
      const response = await textToSqlClient.setMetadataFactStatus({
        connection_ref: metadataView.connection_ref,
        fact_key: fact.fact_key,
        status: nextStatus,
      });
      const status = isRecord(response) && (response.status === "approved" || response.status === "rejected")
        ? response.status
        : nextStatus;
      setMetadataView({
        ...metadataView,
        facts: metadataView.facts.map((item) => (item.fact_key === fact.fact_key ? { ...item, status } : item)),
      });
    } catch (err) {
      handleSaveError(err);
    }
  };

  const writeDisabled = isBusy || !isAdmin;
  const hasTableEdits = Object.keys(tableEdits).length > 0;
  const filteredFacts = metadataView?.facts.filter((fact) => {
    const filterText = factsFilter.trim().toLowerCase();
    if (!filterText) return true;
    return `${fact.table_fqn} ${fact.column ?? ""} ${fact.fact_kind}`.toLowerCase().includes(filterText);
  }) ?? [];

  return (
    <div className="stack">
      <div className="card">
        <div className="section-header">
          <div className="card-title">Метаданные</div>
          <div className="card-description">Описания таблиц/колонок, глоссарий и семантические факты</div>
        </div>
        <div className="form-grid">
          <label className="field">
            <span className="label">Подключение</span>
            <select value={connectionRef} onChange={(e) => setConnectionRef(e.target.value)}>
              <option value="">-- выберите подключение --</option>
              {connections.map((connection) => (
                <option key={connection.connection_ref} value={connection.connection_ref}>
                  {connection.display_name}
                </option>
              ))}
            </select>
          </label>
        </div>
        <div className="button-row">
          <button className="button" type="button" onClick={loadMetadata} disabled={isBusy || !connectionRef}>
            Загрузить метаданные
          </button>
        </div>
        {!isAdmin ? (
          <div className="card-hint">
            Изменение метаданных доступно только пользователям с ролью admin. Просмотр доступен всем.
          </div>
        ) : null}
      </div>

      {conflict ? (
        <div className="card-hint" role="alert">
          Метаданные изменились на сервере (version conflict). Перезагрузите их перед повторным сохранением.
          <div className="button-row">
            <button className="button secondary" type="button" onClick={loadMetadata} disabled={isBusy || !connectionRef}>
              Перезагрузить
            </button>
          </div>
        </div>
      ) : null}

      {metadataView ? (
        <>
          <div className="card">
            <div className="section-header">
              <div className="card-title">Описания таблиц и колонок</div>
            </div>
            {metadataView.editable_file_enabled === false ? (
              <p className="card-hint">
                Файл описаний для этого подключения отключён (enable: false): сохранённые
                описания не попадут в пайплайн, пока файл не будет включён.
              </p>
            ) : null}
            <div className="stack">
              {Object.entries(metadataView.tables).map(([tableFqn, table]) => {
                const tableEdit = tableEdits[tableFqn];
                const descriptionValue = tableEdit && "description" in tableEdit
                  ? (tableEdit.description ?? "")
                  : table.description;
                return (
                  <details key={tableFqn} className="details">
                    <summary>{tableFqn}</summary>
                    <div className="stack">
                      <label className="field">
                        <span className="label">Описание таблицы</span>
                        <textarea
                          value={descriptionValue}
                          onChange={(e) => updateTableDescription(tableFqn, e.target.value)}
                          disabled={writeDisabled}
                        />
                      </label>
                      <div className="graph-inputs">
                        {Object.entries(table.columns).map(([column, columnMeta]) => {
                          const columnEdit = tableEdit?.columns[column];
                          const columnDescription = columnEdit && "description" in columnEdit
                            ? (columnEdit.description ?? "")
                            : columnMeta.description;
                          const columnExamples = columnEdit && "examples" in columnEdit
                            ? (columnEdit.examples ?? "")
                            : formatExamples(columnMeta.examples);
                          return (
                            <div key={column} className="graph-input">
                              <div className="label">{column}{columnMeta.type ? ` (${columnMeta.type})` : ""}</div>
                              <div className="form-grid">
                                <label className="field">
                                  <span className="label">Описание колонки</span>
                                  <input
                                    value={columnDescription}
                                    onChange={(e) => updateColumnField(tableFqn, column, "description", e.target.value)}
                                    disabled={writeDisabled}
                                  />
                                </label>
                                <label className="field">
                                  <span className="label">Примеры (через запятую)</span>
                                  <input
                                    value={columnExamples}
                                    onChange={(e) => updateColumnField(tableFqn, column, "examples", e.target.value)}
                                    disabled={writeDisabled}
                                  />
                                </label>
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  </details>
                );
              })}
              {Object.keys(metadataView.tables).length === 0 ? (
                <div className="card-description">Таблиц в схеме не найдено.</div>
              ) : null}
            </div>
            <div className="button-row">
              <button
                className="button"
                type="button"
                onClick={saveDescriptions}
                disabled={writeDisabled || !hasTableEdits}
              >
                Сохранить описания
              </button>
            </div>
          </div>

          <div className="card">
            <div className="section-header">
              <div className="card-title">Глоссарий</div>
            </div>
            <div className="stack">
              {glossaryDraft.map((row, index) => (
                <div key={row.id} className="graph-input">
                  <div className="form-grid">
                    <label className="field">
                      <span className="label">Термин</span>
                      <input
                        value={row.term}
                        onChange={(e) => updateGlossaryRow(index, { term: e.target.value })}
                        disabled={writeDisabled}
                      />
                    </label>
                    <label className="field">
                      <span className="label">Синонимы (через запятую)</span>
                      <input
                        value={row.synonymsText}
                        onChange={(e) => updateGlossaryRow(index, { synonymsText: e.target.value })}
                        disabled={writeDisabled}
                      />
                    </label>
                    <label className="field">
                      <span className="label">Таблица</span>
                      <input
                        value={row.table}
                        onChange={(e) => updateGlossaryRow(index, { table: e.target.value })}
                        disabled={writeDisabled}
                      />
                    </label>
                    <label className="field">
                      <span className="label">Колонка (опционально)</span>
                      <input
                        value={row.column}
                        onChange={(e) => updateGlossaryRow(index, { column: e.target.value })}
                        disabled={writeDisabled}
                      />
                    </label>
                    <label className="field">
                      <span className="label">Тип</span>
                      <select
                        value={row.kind}
                        onChange={(e) => updateGlossaryRow(index, { kind: e.target.value as GlossaryDraftEntry["kind"] })}
                        disabled={writeDisabled}
                      >
                        <option value="">—</option>
                        <option value="dimension">dimension</option>
                        <option value="measure">measure</option>
                        <option value="filter_value">filter_value</option>
                        <option value="entity">entity</option>
                      </select>
                    </label>
                    <label className="field">
                      <span className="label">Заметка</span>
                      <input
                        value={row.note}
                        onChange={(e) => updateGlossaryRow(index, { note: e.target.value })}
                        disabled={writeDisabled}
                      />
                    </label>
                  </div>
                  <div className="button-row">
                    <button
                      className="button ghost"
                      type="button"
                      onClick={() => removeGlossaryRow(index)}
                      disabled={writeDisabled}
                    >
                      Удалить
                    </button>
                  </div>
                </div>
              ))}
              {glossaryDraft.length === 0 ? (
                <div className="card-description">Термины глоссария не заданы.</div>
              ) : null}
            </div>
            <div className="button-row">
              <button className="button secondary" type="button" onClick={addGlossaryRow} disabled={writeDisabled}>
                Добавить термин
              </button>
              <button className="button" type="button" onClick={saveGlossary} disabled={writeDisabled}>
                Сохранить глоссарий
              </button>
            </div>
          </div>

          <div className="card">
            <div className="section-header">
              <div className="card-title">Семантические факты</div>
              <div className="card-description">Только факты, накопленные автоматически (typed_probe)</div>
            </div>
            <div className="form-grid">
              <label className="field">
                <span className="label">Фильтр (таблица/колонка/тип факта)</span>
                <input value={factsFilter} onChange={(e) => setFactsFilter(e.target.value)} />
              </label>
            </div>
            <div className="cards">
              {filteredFacts.map((fact) => (
                <article key={fact.fact_key} className="card">
                  <div className="inline">
                    <div className="card-title">{fact.table_fqn}{fact.column ? `.${fact.column}` : ""}</div>
                    <span className="tag">{fact.fact_kind}</span>
                    <span className="status-tag" data-status={fact.status === "approved" ? "success" : "failed"}>
                      {fact.status === "approved" ? "Одобрено" : "Отклонено"}
                    </span>
                  </div>
                  <div className="card-description">{String(fact.value)}</div>
                  <div className="button-row">
                    <button
                      className="button secondary"
                      type="button"
                      onClick={() => toggleFactStatus(fact, "approved")}
                      disabled={writeDisabled || fact.status === "approved"}
                    >
                      Одобрить
                    </button>
                    <button
                      className="button ghost"
                      type="button"
                      onClick={() => toggleFactStatus(fact, "rejected")}
                      disabled={writeDisabled || fact.status === "rejected"}
                    >
                      Отклонить
                    </button>
                  </div>
                </article>
              ))}
              {filteredFacts.length === 0 ? <div className="card-description">Фактов нет.</div> : null}
            </div>
          </div>
        </>
      ) : (
        <div className="card-description">Метаданные не загружены. Выберите подключение и нажмите «Загрузить метаданные».</div>
      )}
    </div>
  );
}
