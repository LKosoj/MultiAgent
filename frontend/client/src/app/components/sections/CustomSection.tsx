"use client";

import React, { useState } from "react";
import { KeyValueList } from "../shared/KeyValueList";

type Props = {
  runServiceAction: (action: string, payload: Record<string, unknown>) => Promise<unknown>;
  isBusy: boolean;
  results: { id: string; action: string; status: string; data: unknown; timestamp: string }[];
  clearResults: () => void;
};

function CustomActionCard({ onRun, busy }: { onRun: Props["runServiceAction"]; busy: boolean }) {
  const [action, setAction] = useState("system.init_status");
  const [payload, setPayload] = useState("{}");
  const [error, setError] = useState<string | null>(null);

  const handleRun = async () => {
    setError(null);
    try {
      const parsed = payload.trim() ? JSON.parse(payload) : {};
      await onRun(action.trim(), parsed);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Ошибка выполнения");
    }
  };

  return (
    <div className="card">
      <div className="card-title">Произвольный service action</div>
      <div className="card-description">Для редких или новых действий без отдельного UI.</div>
      <div className="card-hint">Сверяйтесь с `doc/AG_UI_SERVICE_ACTIONS.md` для актуальных параметров.</div>
      <div className="form-grid">
        <label className="field">
          <span className="label">Action</span>
          <input value={action} onChange={(event) => setAction(event.target.value)} />
        </label>
        <label className="field">
          <span className="label">Payload (JSON)</span>
          <textarea value={payload} onChange={(event) => setPayload(event.target.value)} />
        </label>
      </div>
      {error ? <div className="card-description">Ошибка: {error}</div> : null}
      <div className="button-row">
        <button className="button" onClick={handleRun} disabled={busy}>
          {busy ? "Выполняется..." : "Запустить"}
        </button>
      </div>
    </div>
  );
}

export function CustomSection({ runServiceAction, isBusy, results, clearResults }: Props) {
  return (
    <div className="section" id="custom">
      <div className="section-header">
        <div className="section-title">Произвольные действия</div>
        <div className="section-hint">Выполнение сервисных action напрямую</div>
      </div>
      <CustomActionCard onRun={runServiceAction} busy={isBusy} />
      <div className="section">
        <div className="section-header">
          <div className="section-title">Результаты</div>
          <button className="button ghost" onClick={clearResults}>
            Очистить
          </button>
        </div>
        <div className="result-list">
          {results.length === 0 ? (
            <div className="card-description">Пока нет результатов.</div>
          ) : (
            results.map((entry) => {
              const data = entry.data as { base64?: string; mime_type?: string } | undefined;
              const imageSrc = data?.base64 && data?.mime_type?.startsWith("image/") ? `data:${data.mime_type};base64,${data.base64}` : null;
              return (
                <div key={entry.id} className="result-card">
                  <div className="inline">
                    <div className="result-title">{entry.action}</div>
                    <span className="tag">{entry.status}</span>
                    <span className="app-subtitle">{entry.timestamp}</span>
                  </div>
                  <KeyValueList data={entry.data} />
                  {imageSrc ? <img className="image-preview" src={imageSrc} alt={entry.action} /> : null}
                </div>
              );
            })
          )}
        </div>
      </div>
    </div>
  );
}
