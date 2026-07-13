"use client";

import type { TextToSqlConnection } from "../../lib/textToSqlClient";

type Props = {
  connections: TextToSqlConnection[];
  connectionRef: string;
  setConnectionRef: (value: string) => void;
  loadConnections: () => Promise<void>;
  isBusy: boolean;
};

export function TextToSqlConnectionsTab({
  connections,
  connectionRef,
  setConnectionRef,
  loadConnections,
  isBusy,
}: Props) {
  return (
    <div className="stack">
      <div className="card">
        <div className="section-header">
          <div className="card-title">Подключения</div>
          <div className="card-description">Доступные подключения для Text-to-SQL</div>
        </div>
        <div className="button-row">
          <button className="button secondary" type="button" onClick={loadConnections} disabled={isBusy}>
            Обновить список
          </button>
        </div>
      </div>
      <div className="cards">
        {connections.map((connection) => (
          <article key={connection.connection_ref} className="card">
            <div className="inline">
              <div className="card-title">{connection.display_name}</div>
              <span className="app-subtitle">{connection.created_at ?? ""}</span>
            </div>
            <div className="card-description">{connection.target_description ?? "Описание цели отсутствует."}</div>
            <div className="label">Ссылка</div>
            <div className="meta-value">{connection.connection_ref}</div>
            <div className="button-row">
              <button
                className="button ghost"
                type="button"
                onClick={() => setConnectionRef(connection.connection_ref)}
                disabled={connection.connection_ref === connectionRef}
              >
                {connection.connection_ref === connectionRef ? "Выбрано" : "Выбрать"}
              </button>
            </div>
          </article>
        ))}
        {connections.length === 0 ? <div className="card-description">Доступных подключений нет.</div> : null}
      </div>
    </div>
  );
}
