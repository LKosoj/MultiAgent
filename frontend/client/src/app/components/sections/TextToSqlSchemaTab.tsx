"use client";

import {
  buildTextToSqlSchemaPayload,
  createTextToSqlClient,
  type TextToSqlConnection,
} from "../../lib/textToSqlClient";

type Props = {
  connectionRef: string;
  setConnectionRef: (value: string) => void;
  connections: TextToSqlConnection[];
  textToSqlClient: ReturnType<typeof createTextToSqlClient>;
  setError: (msg: string | null) => void;
  isBusy: boolean;
  schemaFilter: string;
  setSchemaFilter: (value: string) => void;
  tableFilter: string;
  setTableFilter: (value: string) => void;
  allowDbSchemaFallback: boolean;
  setAllowDbSchemaFallback: (value: boolean) => void;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  schemaResult: any | null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  setSchemaResult: (value: any | null) => void;
};

export function TextToSqlSchemaTab({
  connectionRef,
  setConnectionRef,
  connections,
  textToSqlClient,
  setError,
  isBusy,
  schemaFilter,
  setSchemaFilter,
  tableFilter,
  setTableFilter,
  allowDbSchemaFallback,
  setAllowDbSchemaFallback,
  schemaResult,
  setSchemaResult,
}: Props) {
  const loadSchema = async () => {
    if (!connectionRef) return;
    setError(null);
    try {
      const resp = await textToSqlClient.loadSchema(
        buildTextToSqlSchemaPayload(
          connectionRef,
          schemaFilter,
          tableFilter,
          allowDbSchemaFallback,
        ),
      );
      setSchemaResult(resp);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось получить схему");
    }
  };

  return (
    <div className="stack">
      <div className="card">
        <div className="section-header">
          <div className="card-title">Схема БД</div>
          <div className="card-description">Интроспекция таблиц и колонок</div>
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
          <label className="field">
            <span className="label">Schema (опционально)</span>
            <input value={schemaFilter} onChange={(e) => setSchemaFilter(e.target.value)} placeholder="public" />
          </label>
          <label className="field">
            <span className="label">Table (опционально)</span>
            <input value={tableFilter} onChange={(e) => setTableFilter(e.target.value)} placeholder="users" />
          </label>
        </div>
        <div className="button-row">
          <label className="toggle">
            <input
              type="checkbox"
              checked={allowDbSchemaFallback}
              onChange={(e) => setAllowDbSchemaFallback(e.target.checked)}
            />
            <span>Разрешить загрузку из БД</span>
          </label>
          <button className="button" type="button" onClick={loadSchema} disabled={isBusy || !connectionRef}>
            Загрузить схему
          </button>
        </div>
      </div>
      {schemaResult ? (
        <div className="cards">
          <article className="card">
            <div className="card-title">Схема</div>
            {/* eslint-disable-next-line @typescript-eslint/no-explicit-any */}
            <div className="card-description">Источник: {(schemaResult as any)?.source ?? "—"}</div>
            {/* eslint-disable-next-line @typescript-eslint/no-explicit-any */}
            {Array.isArray((schemaResult as any)?.warnings) && (schemaResult as any).warnings.length > 0 ? (
              // eslint-disable-next-line @typescript-eslint/no-explicit-any
              <div className="card-description">{(schemaResult as any).warnings.join("; ")}</div>
            ) : null}
            {/* eslint-disable-next-line @typescript-eslint/no-explicit-any */}
            <div className="card-description">Количество таблиц: {Object.keys((schemaResult as any)?.schema ?? {}).length}</div>
            <details className="details">
              <summary>Таблицы</summary>
              <div className="graph-inputs">
                {/* eslint-disable-next-line @typescript-eslint/no-explicit-any */}
                {Object.entries((schemaResult as any)?.schema ?? {}).map(([tableName, tableInfo]: any) => (
                  <div key={tableName} className="graph-input">
                    <div className="label">{tableName}</div>
                    <div className="meta-value">
                      {tableInfo?.columns ? Object.keys(tableInfo.columns).join(", ") : "—"}
                    </div>
                  </div>
                ))}
              </div>
            </details>
          </article>
        </div>
      ) : null}
    </div>
  );
}
