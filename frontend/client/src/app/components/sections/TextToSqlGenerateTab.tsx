"use client";

import { KeyValueList } from "../shared/KeyValueList";
import { WorkflowResultView } from "../shared/WorkflowResultView";
import { extractFinalOutput, extractSqlCandidate } from "./useTextToSqlRun";
import { textToSqlRunTerminalStatus } from "../../lib/textToSqlContracts";
import { textToSqlRunCanCancel } from "../../lib/textToSqlRunState";
import {
  type TextToSqlConnection,
  type TextToSqlStartPayloadInput,
} from "../../lib/textToSqlClient";

type Props = {
  connectionRef: string;
  setConnectionRef: (value: string) => void;
  connections: TextToSqlConnection[];
  isBusy: boolean;
  error: string | null;
  setError: (msg: string | null) => void;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  result: any;
  runId: string | null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  runStatus: any;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  runArtifacts: any;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  workflowResult: any;
  runStatusValue: string;
  autoRefreshRun: boolean;
  setAutoRefreshRun: (value: boolean) => void;
  isCancelling: boolean;
  handleCancelRun: () => Promise<void>;
  loadRunStatus: (runIdValue?: string | null) => Promise<unknown>;
  loadRunResult: (runIdValue?: string | null, allowRetry?: boolean) => Promise<unknown>;
  handleGenerateReport: () => Promise<void>;
  reportError: string | null;
  setResultModal: (value: { open: boolean; runId: string | null }) => void;
  runLogsAutoRefresh: boolean;
  setRunLogsAutoRefresh: (value: boolean) => void;
  loadRunLogs: (runIdValue?: string | null) => Promise<void>;
  formattedRunLogs: string;
  loadConnections: () => Promise<void>;
  onGenerate: (inputs: TextToSqlStartPayloadInput) => Promise<void>;
  prompt: string;
  setPrompt: (value: string) => void;
  naturalQuery: string;
  setNaturalQuery: (value: string) => void;
  sessionId: string;
  setSessionId: (value: string) => void;
  maxRows: string;
  setMaxRows: (value: string) => void;
  enableTelemetry: boolean;
  setEnableTelemetry: (value: boolean) => void;
  safetyLevel: string;
  setSafetyLevel: (value: string) => void;
  includeExplanation: boolean;
  setIncludeExplanation: (value: boolean) => void;
  validateSchema: boolean;
  setValidateSchema: (value: boolean) => void;
  dryRunOnly: boolean;
  setDryRunOnly: (value: boolean) => void;
  isSubmitting: boolean;
  setIsSubmitting: (value: boolean) => void;
};

export function TextToSqlGenerateTab({
  connectionRef,
  setConnectionRef,
  connections,
  isBusy,
  error,
  setError,
  result,
  runId,
  runStatus,
  runArtifacts,
  workflowResult,
  runStatusValue,
  autoRefreshRun,
  setAutoRefreshRun,
  isCancelling,
  handleCancelRun,
  loadRunStatus,
  loadRunResult,
  handleGenerateReport,
  reportError,
  setResultModal,
  runLogsAutoRefresh,
  setRunLogsAutoRefresh,
  loadRunLogs,
  formattedRunLogs,
  loadConnections,
  onGenerate,
  prompt,
  setPrompt,
  naturalQuery,
  setNaturalQuery,
  sessionId,
  setSessionId,
  maxRows,
  setMaxRows,
  enableTelemetry,
  setEnableTelemetry,
  safetyLevel,
  setSafetyLevel,
  includeExplanation,
  setIncludeExplanation,
  validateSchema,
  setValidateSchema,
  dryRunOnly,
  setDryRunOnly,
  isSubmitting,
  setIsSubmitting,
}: Props) {
  const effectiveQuery = naturalQuery.trim() || prompt.trim();
  const selectedConnection = connections.find(
    (connection) => connection.connection_ref === connectionRef,
  );
  const resultSql = extractSqlCandidate(workflowResult) ?? extractSqlCandidate(result) ?? "";
  const resultSchema = result?.schema ?? result?.parameters?.schema ?? "";
  const resultStatus = textToSqlRunTerminalStatus(workflowResult)
    ?? (runStatusValue || result?.status || result?.state);

  const handleGenerate = async () => {
    const maxRowsText = maxRows.trim();
    const normalizedMaxRows = Number(maxRowsText);
    if (!/^\d+$/.test(maxRowsText)) {
      setError("max_rows должен быть целым числом от 1 до 10000");
      return;
    }
    if (!Number.isInteger(normalizedMaxRows) || normalizedMaxRows < 1 || normalizedMaxRows > 10000) {
      setError("max_rows должен быть целым числом от 1 до 10000");
      return;
    }
    await onGenerate({
      query: effectiveQuery,
      connectionRef,
      maxRows: normalizedMaxRows,
      sessionId: sessionId || undefined,
      enableTelemetry,
      safetyLevel,
      includeExplanation,
      validateSchema,
      dryRunOnly,
    });
  };

  return (
    <div className="card">
      <div className="section-header">
        <div className="card-title">Генерация запроса</div>
        <div className="card-description">Опишите запрос на естественном языке</div>
      </div>
      <div className="form-grid">
        <label className="field">
          <span className="label">Prompt</span>
          <textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} placeholder="Опишите аналитический вопрос" />
        </label>
        <label className="field">
          <span className="label">Natural Query (опционально)</span>
          <textarea value={naturalQuery} onChange={(e) => setNaturalQuery(e.target.value)} placeholder="Natural language query" />
        </label>
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
          <span className="label">Session ID</span>
          <input value={sessionId} onChange={(e) => setSessionId(e.target.value)} placeholder="опционально" />
        </label>
        <label className="field">
          <span className="label">Макс. строк</span>
          <input type="text" inputMode="numeric" pattern="[0-9]*" value={maxRows} onChange={(e) => setMaxRows(e.target.value)} />
        </label>
        <label className="field">
          <span className="label">Уровень безопасности</span>
          <select value={safetyLevel} onChange={(e) => setSafetyLevel(e.target.value)}>
            <option value="strict">strict</option>
          </select>
        </label>
      </div>
      <div className="toggle-grid">
        <label className="toggle">
          <input type="checkbox" checked={enableTelemetry} onChange={(e) => setEnableTelemetry(e.target.checked)} />
          <span>Телеметрия</span>
        </label>
        <label className="toggle">
          <input type="checkbox" checked={includeExplanation} onChange={(e) => setIncludeExplanation(e.target.checked)} />
          <span>Объяснение</span>
        </label>
        <label className="toggle">
          <input type="checkbox" checked={validateSchema} onChange={(e) => setValidateSchema(e.target.checked)} />
          <span>Валидация схемы</span>
        </label>
        <label className="toggle">
          <input type="checkbox" checked={dryRunOnly} onChange={(e) => setDryRunOnly(e.target.checked)} />
          <span>Dry run</span>
        </label>
      </div>
      <div className="button-row">
        <button
          className="button"
          type="button"
          disabled={isBusy || isSubmitting || !effectiveQuery || !connectionRef}
          onClick={async () => {
            if (isSubmitting) return;
            setIsSubmitting(true);
            try { await handleGenerate(); } finally { setIsSubmitting(false); }
          }}
        >
          {isSubmitting ? <span className="spinner" /> : null}
          Сгенерировать
        </button>
        <button className="button secondary" type="button" onClick={loadConnections} disabled={isBusy}>
          Обновить подключения
        </button>
      </div>
      {error ? <div className="card-description">Ошибка: {error}</div> : null}
      {selectedConnection ? (
        <div className="card" style={{ background: "var(--panel-strong)" }}>
          <div className="card-title">Информация о подключении</div>
          <div className="profile-meta">
            <div>
              <span className="label">Цель</span>
              <div className="meta-value">{selectedConnection.target_description ?? "—"}</div>
            </div>
            <div>
              <span className="label">Тип БД</span>
              <div className="meta-value">{selectedConnection.target_kind ?? "—"}</div>
            </div>
            <div>
              <span className="label">Диалект</span>
              <div className="meta-value">{selectedConnection.dialect ?? "—"}</div>
            </div>
          </div>
        </div>
      ) : null}
      {result ? (
        <div className="card">
          <div className="card-title">Результат</div>
          <div className="profile-meta">
            <div>
              <span className="label">SQL</span>
              <div className="meta-value">{resultSql || "—"}</div>
            </div>
            <div>
              <span className="label">Схема</span>
              <div className="meta-value">{resultSchema || "—"}</div>
            </div>
            <div>
              <span className="label">Статус</span>
              <div className="meta-value">{resultStatus || "—"}</div>
            </div>
          </div>
          {result.columns ? (
            <details className="details">
              <summary>Колонки</summary>
              <div className="badge-row">
                {/* eslint-disable-next-line @typescript-eslint/no-explicit-any */}
                {(result.columns as any[]).map((col, idx) => (
                  <span key={idx} className="badge">
                    {String(col)}
                  </span>
                ))}
              </div>
            </details>
          ) : null}
          {result.explanation ? (
            <details className="details">
              <summary>Объяснение</summary>
              <div className="card-description">{String(result.explanation)}</div>
            </details>
          ) : null}
          {result.result_preview ? (
            <details className="details">
              <summary>Превью результата</summary>
              <div className="card-description" style={{ whiteSpace: "pre-wrap" }}>
                {String(result.result_preview)}
              </div>
            </details>
          ) : null}
        </div>
      ) : null}
      {runId ? (
        <div className="card">
          <div className="section-header">
            <div className="card-title">Статус запуска</div>
            <div className="button-row">
              <button className="button secondary" type="button" onClick={() => loadRunStatus(runId)} disabled={isBusy}>
                Обновить статус
              </button>
              {textToSqlRunCanCancel(runId, runStatus, false) ? (
                <button
                  className="button ghost"
                  type="button"
                  onClick={handleCancelRun}
                  disabled={isBusy || isCancelling}
                  aria-busy={isCancelling || undefined}
                >
                  {isCancelling ? "Отмена…" : "Отменить запуск"}
                </button>
              ) : null}
              <button
                className="button secondary"
                type="button"
                onClick={async () => {
                  setResultModal({ open: true, runId });
                  await loadRunStatus(runId);
                  await loadRunResult(runId);
                }}
                disabled={isBusy}
              >
                Результат
              </button>
              <label className="toggle">
                <input type="checkbox" checked={autoRefreshRun} onChange={(e) => setAutoRefreshRun(e.target.checked)} />
                <span>Автообновление</span>
              </label>
            </div>
          </div>
          <div className="profile-meta">
            <div>
              <span className="label">Run ID</span>
              <div className="meta-value">{runId}</div>
            </div>
            <div>
              <span className="label">Статус</span>
              <div className="meta-value">{runStatus?.status ?? runStatus?.state ?? "—"}</div>
            </div>
            <div>
              <span className="label">Прогресс</span>
              <div className="meta-value">
                {typeof runStatus?.progress_percentage === "number" ? `${runStatus.progress_percentage.toFixed(1)}%` : "—"}
              </div>
            </div>
          </div>
          {runStatus ? (
            <details className="details">
              <summary>Подробности статуса</summary>
              <KeyValueList data={runStatus} />
            </details>
          ) : null}
          <div className="run-result">
            <div className="section-header">
              <div className="label">Логи запуска</div>
              <div className="button-row">
                <button className="button ghost" type="button" onClick={() => loadRunLogs(runId)} disabled={isBusy}>
                  Обновить логи
                </button>
                <label className="toggle">
                  <input
                    type="checkbox"
                    checked={runLogsAutoRefresh}
                    onChange={(e) => setRunLogsAutoRefresh(e.target.checked)}
                  />
                  <span>Автообновление</span>
                </label>
              </div>
            </div>
            {formattedRunLogs ? <pre className="code log-output">{formattedRunLogs}</pre> : <div className="card-description">Логов пока нет.</div>}
          </div>
          {workflowResult ? (
            <details className="details">
              <summary>Финальный результат</summary>
              {/* eslint-disable-next-line @typescript-eslint/no-explicit-any */}
              <WorkflowResultView finalOutput={extractFinalOutput((workflowResult as any)?.result)} />
              {/* eslint-disable-next-line @typescript-eslint/no-explicit-any */}
              {extractFinalOutput((workflowResult as any)?.result) ? (
                <div className="button-row">
                  {/* eslint-disable-next-line @typescript-eslint/no-explicit-any */}
                  {(workflowResult as any)?.report ? (
                    <button
                      className="button secondary"
                      type="button"
                      onClick={() => setResultModal({ open: true, runId })}
                      disabled={isBusy}
                    >
                      Сохранить отчёт
                    </button>
                  ) : (
                    <button className="button secondary" type="button" onClick={handleGenerateReport} disabled={isBusy}>
                      Сохранить отчёт
                    </button>
                  )}
                </div>
              ) : null}
            </details>
          ) : null}
          {runArtifacts?.step_outputs ? (
            <details className="details">
              <summary>Шаги workflow</summary>
              <div className="cards">
                {/* eslint-disable-next-line @typescript-eslint/no-explicit-any */}
                {Object.entries(runArtifacts.step_outputs as Record<string, any>).map(([stepId, output]) => (
                  <article key={stepId} className="card">
                    <div className="card-title">{stepId}</div>
                    {typeof output === "string" ? (
                      <div className="card-description" style={{ whiteSpace: "pre-wrap" }}>
                        {output}
                      </div>
                    ) : (
                      <KeyValueList data={output} />
                    )}
                  </article>
                ))}
              </div>
            </details>
          ) : null}
        </div>
      ) : null}
      {reportError ? <div className="card-description">Ошибка отчёта: {reportError}</div> : null}
    </div>
  );
}
