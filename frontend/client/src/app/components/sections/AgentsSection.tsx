"use client";

import React from "react";
import { createPortal } from "react-dom";
import { KeyValueList } from "../shared/KeyValueList";

type AgentProfile = {
  name: string;
  type?: string;
  description?: string;
  model?: string;
  model_key?: string;
  model_real_id?: string;
  tools?: string[];
  max_steps?: number;
  planning_interval?: number | string | null;
  memory_policy?: Record<string, unknown>;
};

type AgentRunInfo = {
  run_id?: string;
  profile_name?: string;
  status?: string;
  task?: string;
  session_id?: string;
  step_count?: number;
  current_step?: string;
};

type AgentHistoryEntry = {
  run_id: string;
  profile_name: string;
  status?: string;
  task?: string;
  start_time?: string;
  end_time?: string;
};

type AgentRunForm = {
  sessionId: string;
  task: string;
  useExistingAgent: boolean;
  selectedAgentId: string;
  enableTelemetry: boolean;
  enableMemory: boolean;
};

const decodeGzipBase64 = async (b64: string) => {
  const binary = atob(b64);
  const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
  if (!("DecompressionStream" in window)) {
    throw new Error("Браузер не поддерживает распаковку gzip");
  }
  const stream = new DecompressionStream("gzip");
  const body = new Response(bytes).body;
  if (!body) {
    throw new Error("Не удалось распаковать отчёт");
  }
  const decompressed = body.pipeThrough(stream);
  return new Response(decompressed).text();
};

type Props = {
  isBusy: boolean;
  runServiceAction: (action: string, payload: Record<string, unknown>) => Promise<unknown>;
  agentTab: "profiles" | "run" | "monitor";
  setAgentTab: (tab: Props["agentTab"]) => void;
  loadAgentProfiles: () => void;
  profilesLoading: boolean;
  profilesError: string | null;
  agentProfiles: AgentProfile[];
  availableProfileTypes: string[];
  availableProfileModels: { key: string; label: string }[];
  profileTypeFilter: string;
  setProfileTypeFilter: (value: string) => void;
  profileModelFilter: string;
  setProfileModelFilter: (value: string) => void;
  profileSearch: string;
  setProfileSearch: (value: string) => void;
  filteredProfiles: AgentProfile[];
  handleSelectProfile: (profile: AgentProfile) => void;
  getModelLabel: (model: unknown) => string;
  selectedProfile: AgentProfile | null;
  agentRunForm: AgentRunForm;
  setAgentRunForm: React.Dispatch<React.SetStateAction<AgentRunForm>>;
  createdAgentsForProfile: { agentId: string; profile_name: string; session_id: string; created_at: string }[];
  createdAgents: { agentId: string; profile_name: string; session_id: string; created_at: string }[];
  handleRemoveCreatedAgent: (agentId: string) => void;
  agentRunHistory: AgentHistoryEntry[];
  handleClearAgentHistory: () => void;
  handleCleanupRuns: () => void;
  handleRunAgent: () => void;
  refreshActiveRuns: () => void;
  runsLoading: boolean;
  runsError: string | null;
  activeAgentRuns: AgentRunInfo[];
  runResults: Record<string, unknown>;
  currentAgentRunId: string | null;
  currentAgentRunLogs: unknown[];
  handleFetchRunStatus: (runId: string) => void;
  handleFetchRunResult: (runId: string) => void;
  handleCancelRun: (runId: string) => void;
  handleFetchRunLogs: (runId: string) => void;
  autoRefreshRuns: boolean;
  setAutoRefreshRuns: (value: boolean) => void;
};

export function AgentsSection({
  isBusy,
  runServiceAction,
  agentTab,
  setAgentTab,
  loadAgentProfiles,
  profilesLoading,
  profilesError,
  agentProfiles,
  availableProfileTypes,
  availableProfileModels,
  profileTypeFilter,
  setProfileTypeFilter,
  profileModelFilter,
  setProfileModelFilter,
  profileSearch,
  setProfileSearch,
  filteredProfiles,
  handleSelectProfile,
  getModelLabel,
  selectedProfile,
  agentRunForm,
  setAgentRunForm,
  createdAgentsForProfile,
  createdAgents,
  handleRemoveCreatedAgent,
  agentRunHistory,
  handleClearAgentHistory,
  handleCleanupRuns,
  handleRunAgent,
  refreshActiveRuns,
  runsLoading,
  runsError,
  activeAgentRuns,
  runResults,
  currentAgentRunId,
  currentAgentRunLogs,
  handleFetchRunStatus,
  handleFetchRunResult,
  handleCancelRun,
  handleFetchRunLogs,
  autoRefreshRuns,
  setAutoRefreshRuns,
}: Props) {
  const [profileDetailsModal, setProfileDetailsModal] = React.useState<{ open: boolean; profile: AgentProfile | null }>({
    open: false,
    profile: null,
  });
  const [isMounted, setIsMounted] = React.useState(false);
  const [reportError, setReportError] = React.useState<string | null>(null);
  const [resultModal, setResultModal] = React.useState<{ open: boolean; runId: string | null }>({
    open: false,
    runId: null,
  });
  const [reportPreviewUrl, setReportPreviewUrl] = React.useState<string | null>(null);
  const [reportPreviewError, setReportPreviewError] = React.useState<string | null>(null);
  const reportPreviewUrlRef = React.useRef<string | null>(null);
  const [resultModalOutput, setResultModalOutput] = React.useState<unknown>(null);
  const resultModalOutputRunIdRef = React.useRef<string | null>(null);

  const getAgentRunStatus = React.useCallback(
    (runId: string | null) => {
      if (!runId) return null;
      const active = activeAgentRuns.find((run) => run.run_id === runId);
      if (active?.status) return active.status;
      const history = agentRunHistory.find((entry) => entry.run_id === runId);
      return history?.status ?? null;
    },
    [activeAgentRuns, agentRunHistory],
  );

  const currentRunStatus = React.useMemo(() => {
    if (!currentAgentRunId) return null;
    return activeAgentRuns.find((run) => run.run_id === currentAgentRunId)?.status ?? null;
  }, [activeAgentRuns, currentAgentRunId]);

  const formattedRunLogs = React.useMemo(() => {
    if (!Array.isArray(currentAgentRunLogs) || currentAgentRunLogs.length === 0) return "";
    return currentAgentRunLogs
      .map((entry: any) => {
        const timestamp = entry?.timestamp ?? "";
        const level = entry?.level ?? "INFO";
        const message = entry?.message ?? "";
        const loggerName = entry?.logger_name;
        const loggerSuffix =
          loggerName && loggerName !== "agent_stdout" && loggerName !== "agent_stderr" ? ` (${loggerName})` : "";
        return `${timestamp} [${level}] ${message}${loggerSuffix}`.trim();
      })
      .join("\n");
  }, [currentAgentRunLogs]);

  const openReport = React.useCallback(async (runId: string, report: any) => {
    const payload =
      report?.base64_gzip ??
      report?.content_b64_gzip ??
      report?.report_b64_gzip ??
      report?.base64;
    if (!payload) {
      throw new Error("Пустой отчёт");
    }
    const html = await decodeGzipBase64(payload);
    const mimeType = report?.mime_type ?? "text/html";
    const filename = report?.filename ?? `report_${runId}.html`;
    const blob = new Blob([html], { type: mimeType });
    const url = URL.createObjectURL(blob);
    const opened = window.open(url, "_blank", "noopener,noreferrer");
    if (!opened) {
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      link.click();
    }
    window.setTimeout(() => URL.revokeObjectURL(url), 10000);
  }, []);

  // Автооткрытие отключено — отчёт открывается только по клику.

  React.useEffect(() => {
    setIsMounted(true);
  }, []);

  React.useEffect(() => {
    if (!isMounted) return;
    if (!profileDetailsModal.open && !resultModal.open) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [isMounted, profileDetailsModal.open, resultModal.open]);

  React.useEffect(() => {
    if (!resultModal.open || !resultModal.runId) {
      if (reportPreviewUrlRef.current) {
        URL.revokeObjectURL(reportPreviewUrlRef.current);
        reportPreviewUrlRef.current = null;
      }
      setReportPreviewUrl(null);
      setReportPreviewError(null);
      setResultModalOutput(null);
      resultModalOutputRunIdRef.current = null;
      return;
    }
    const result = runResults[resultModal.runId];
    const report = (result as any)?.report;
    const payload =
      report?.base64_gzip ?? report?.content_b64_gzip ?? report?.report_b64_gzip ?? report?.base64;
    if (!payload) {
      setReportPreviewUrl(null);
      setReportPreviewError(null);
      return;
    }
    let revoked = false;
    const createPreview = async () => {
      try {
        setReportPreviewError(null);
        const html = await decodeGzipBase64(payload);
        if (revoked) return;
        const mimeType = report?.mime_type ?? "text/html";
        const blob = new Blob([html], { type: mimeType });
        const url = URL.createObjectURL(blob);
        if (reportPreviewUrlRef.current) {
          URL.revokeObjectURL(reportPreviewUrlRef.current);
        }
        reportPreviewUrlRef.current = url;
        setReportPreviewUrl(url);
      } catch (err) {
        if (reportPreviewUrlRef.current) {
          URL.revokeObjectURL(reportPreviewUrlRef.current);
          reportPreviewUrlRef.current = null;
        }
        setReportPreviewUrl(null);
        setReportPreviewError(err instanceof Error ? err.message : "Не удалось открыть отчёт");
      }
    };
    void createPreview();
    return () => {
      revoked = true;
      if (reportPreviewUrlRef.current) {
        URL.revokeObjectURL(reportPreviewUrlRef.current);
        reportPreviewUrlRef.current = null;
      }
    };
  }, [resultModal.open, resultModal.runId, runResults]);

  React.useEffect(() => {
    if (!resultModal.open || !resultModal.runId) return;
    const result = runResults[resultModal.runId] as any;
    const directOutput = result?.result ?? result?.output ?? null;
    if (directOutput !== null && directOutput !== undefined) {
      setResultModalOutput(directOutput);
      resultModalOutputRunIdRef.current = resultModal.runId;
      return;
    }
    const statusValue = String(getAgentRunStatus(resultModal.runId) ?? "").toLowerCase();
    if (statusValue && statusValue !== "completed" && statusValue !== "failed" && statusValue !== "cancelled") {
      setResultModalOutput(null);
      return;
    }
    if (resultModalOutputRunIdRef.current === resultModal.runId && resultModalOutput) return;
    const loadFromTrace = async () => {
      try {
        const resp = await runServiceAction("telemetry.trace_file", { run_id: resultModal.runId });
        const trace = (resp as any)?.trace ?? resp;
        const spans = Array.isArray(trace?.spans) ? trace.spans : [];
        let value: unknown = null;
        for (const span of spans) {
          const attrs = (span as any)?.attributes ?? {};
          if (attrs && attrs["output.value"] != null) {
            value = attrs["output.value"];
            break;
          }
        }
        if (typeof value === "string") {
          try {
            value = JSON.parse(value);
          } catch {
            // keep as string
          }
        }
        setResultModalOutput(value);
        resultModalOutputRunIdRef.current = resultModal.runId;
      } catch {
        setResultModalOutput(null);
      }
    };
    void loadFromTrace();
  }, [resultModal.open, resultModal.runId, runResults, runServiceAction, resultModalOutput, getAgentRunStatus]);

  const renderResultValue = (value: unknown) => {
    if (value === null || value === undefined) {
      return <div className="card-description">Результаты пока недоступны.</div>;
    }
    if (typeof value === "string") {
      return <pre className="whitespace-pre-wrap text-sm">{value}</pre>;
    }
    if (typeof value === "number" || typeof value === "boolean") {
      return <div className="card-description">{String(value)}</div>;
    }
    return <KeyValueList data={value} />;
  };

  React.useEffect(() => {
    if (!currentAgentRunId) return;
    void handleFetchRunLogs(currentAgentRunId);
  }, [currentAgentRunId, handleFetchRunLogs]);

  React.useEffect(() => {
    if (!currentAgentRunId || currentRunStatus !== "running") return;
    const timer = window.setInterval(() => {
      void handleFetchRunLogs(currentAgentRunId);
    }, 3000);
    return () => window.clearInterval(timer);
  }, [currentAgentRunId, currentRunStatus, handleFetchRunLogs]);

  return (
    <div className="section" id="agents">
      <div className="section-header">
        <div className="section-title">Агенты</div>
        <div className="section-hint">Профили, запуск и мониторинг</div>
      </div>
      <div className="segment-row">
        <button className={`segment-button${agentTab === "profiles" ? " active" : ""}`} onClick={() => setAgentTab("profiles")}>
          Профили
        </button>
        <button className={`segment-button${agentTab === "run" ? " active" : ""}`} onClick={() => setAgentTab("run")}>
          Запуск
        </button>
        <button className={`segment-button${agentTab === "monitor" ? " active" : ""}`} onClick={() => setAgentTab("monitor")}>
          Мониторинг
        </button>
      </div>

      {agentTab === "profiles" ? (
        <div className="stack">
          <div className="toolbar-row">
            <div className="inline">
              <button className="button secondary" type="button" onClick={loadAgentProfiles} disabled={isBusy}>
                Обновить список
              </button>
              <span className="status-chip">{profilesLoading ? "Загрузка..." : `Найдено: ${agentProfiles.length}`}</span>
            </div>
            {profilesError ? <div className="card-description">Ошибка: {profilesError}</div> : null}
          </div>
          <div className="filters-grid">
            <label className="field">
              <span className="label">Тип агента</span>
              <select value={profileTypeFilter} onChange={(event) => setProfileTypeFilter(event.target.value)}>
                {availableProfileTypes.map((type) => (
                  <option key={type} value={type}>
                    {type}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span className="label">Модель</span>
              <select value={profileModelFilter} onChange={(event) => setProfileModelFilter(event.target.value)}>
                {availableProfileModels.map((model) => (
                  <option key={model.key} value={model.key}>
                    {model.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span className="label">Поиск</span>
              <input value={profileSearch} placeholder="Имя или описание" onChange={(event) => setProfileSearch(event.target.value)} />
            </label>
          </div>
          {profilesLoading ? <div className="card-description">Загрузка профилей...</div> : null}
          {!profilesLoading && filteredProfiles.length === 0 ? <div className="card-description">Нет профилей с заданными фильтрами.</div> : null}
          <div className="cards profile-grid">
            {filteredProfiles.map((profile) => {
              const openProfileModal = () => setProfileDetailsModal({ open: true, profile });
              return (
                <article
                  key={profile.name}
                  className="card profile-card"
                  role="button"
                  tabIndex={0}
                  onClick={openProfileModal}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") openProfileModal();
                  }}
                  style={{ cursor: "pointer" }}
                >
                  <div className="card-title">{profile.name}</div>
                  <div className="card-description">{profile.description || "Описание не задано."}</div>
                  <div className="button-row">
                  <button
                      className="button ghost"
                      type="button"
                      onClick={(event) => {
                        event.stopPropagation();
                        openProfileModal();
                      }}
                      disabled={isBusy}
                    >
                      Подробнее
                    </button>
                    <button
                      className="button"
                      type="button"
                      onClick={(event) => {
                        event.stopPropagation();
                        handleSelectProfile(profile);
                      }}
                      disabled={isBusy}
                    >
                      Выбрать
                    </button>
                  </div>
                </article>
              );
            })}
          </div>
        </div>
      ) : null}

      {agentTab === "run" ? (
        <div className="stack">
          {!selectedProfile ? (
            <div className="empty-state">
              <div className="card-title">Сначала выберите профиль</div>
              <div className="card-description">Перейдите во вкладку “Профили” и выберите агента для запуска.</div>
              <button className="button secondary" type="button" onClick={() => setAgentTab("profiles")}>
                К профилям
              </button>
            </div>
          ) : (
            <div className="card">
              <div className="section-header">
                <div>
                  <div className="card-title">Запуск {selectedProfile.name}</div>
                  <div className="card-description">{selectedProfile.description || "Описание не задано."}</div>
                </div>
                <button className="button ghost" type="button" onClick={() => setAgentTab("profiles")}>
                  Сменить профиль
                </button>
              </div>
              <div className="form-grid">
                <label className="field">
                  <span className="label">Session ID (необязательно)</span>
                  <input
                    value={agentRunForm.sessionId}
                    placeholder="Оставьте пустым для автогенерации"
                    onChange={(event) => setAgentRunForm((prev) => ({ ...prev, sessionId: event.target.value }))}
                  />
                </label>
                <label className="field">
                  <span className="label">Задача</span>
                  <textarea
                    value={agentRunForm.task}
                    placeholder="Опишите задачу для агента..."
                    onChange={(event) => setAgentRunForm((prev) => ({ ...prev, task: event.target.value }))}
                  />
                </label>
              </div>
              <div className="toggle-grid">
                <label className="toggle">
                  <input
                    type="checkbox"
                    checked={agentRunForm.enableTelemetry}
                    onChange={(event) => setAgentRunForm((prev) => ({ ...prev, enableTelemetry: event.target.checked }))}
                  />
                  <span>Телеметрия</span>
                </label>
                <label className="toggle">
                  <input
                    type="checkbox"
                    checked={agentRunForm.enableMemory}
                    onChange={(event) => setAgentRunForm((prev) => ({ ...prev, enableMemory: event.target.checked }))}
                  />
                  <span>Память</span>
                </label>
                <label className="toggle">
                  <input
                    type="checkbox"
                    checked={agentRunForm.useExistingAgent}
                    disabled={createdAgentsForProfile.length === 0}
                    onChange={(event) =>
                      setAgentRunForm((prev) => ({
                        ...prev,
                        useExistingAgent: event.target.checked,
                        selectedAgentId: event.target.checked ? prev.selectedAgentId : "",
                      }))
                    }
                  />
                  <span>Использовать созданный экземпляр</span>
                </label>
              </div>
              {agentRunForm.useExistingAgent ? (
                <label className="field">
                  <span className="label">Экземпляр агента</span>
                  <select
                    value={agentRunForm.selectedAgentId}
                    onChange={(event) => setAgentRunForm((prev) => ({ ...prev, selectedAgentId: event.target.value }))}
                  >
                    <option value="">-- выберите --</option>
                    {createdAgentsForProfile.map((entry) => (
                      <option key={entry.agentId} value={entry.agentId}>
                        {entry.agentId} · {entry.session_id}
                      </option>
                    ))}
                  </select>
                </label>
              ) : null}
              <div className="button-row">
                <button className="button" type="button" onClick={handleRunAgent} disabled={isBusy || !agentRunForm.task.trim()}>
                  Запустить
                </button>
                <button className="button secondary" type="button" onClick={() => setAgentRunForm((prev) => ({ ...prev, task: "" }))} disabled={isBusy}>
                  Очистить задачу
                </button>
              </div>
              <div className="run-result">
                <div className="section-header">
                  <div className="label">Логи запуска</div>
                  <div className="button-row">
                    <button
                      className="button ghost"
                      type="button"
                      onClick={() => currentAgentRunId && handleFetchRunLogs(currentAgentRunId)}
                      disabled={isBusy || !currentAgentRunId}
                    >
                      Обновить логи
                    </button>
                  </div>
                </div>
                {!currentAgentRunId ? (
                  <div className="card-description">Сначала запустите агента.</div>
                ) : formattedRunLogs ? (
                  <pre className="code log-output">{formattedRunLogs}</pre>
                ) : (
                  <div className="card-description">Логов пока нет.</div>
                )}
              </div>
            </div>
          )}
        </div>
      ) : null}

      {agentTab === "monitor" ? (
        <div className="stack">
          <div className="toolbar-row">
            <div className="inline">
              <button className="button secondary" type="button" onClick={refreshActiveRuns} disabled={isBusy}>
                Обновить
              </button>
              <button className="button ghost" type="button" onClick={handleCleanupRuns} disabled={isBusy}>
                Очистить завершенные
              </button>
              <label className="toggle">
                <input type="checkbox" checked={autoRefreshRuns} onChange={(event) => setAutoRefreshRuns(event.target.checked)} />
                <span>Автообновление</span>
              </label>
            </div>
            <span className="status-chip">{runsLoading ? "Обновление..." : `Активных запусков: ${activeAgentRuns.length}`}</span>
          </div>
          {runsError ? <div className="card-description">Ошибка: {runsError}</div> : null}
          {activeAgentRuns.length === 0 && !runsLoading ? <div className="card-description">Активных запусков пока нет.</div> : null}
          <div className="cards">
            {activeAgentRuns.map((run, index) => {
              const runId = run.run_id ?? `run-${index + 1}`;
              const status = run.status ?? "unknown";
              const result = runResults[runId];
              const transientReport = Boolean((result as any)?.report_transient);
              const report = (result as any)?.report;
              const hasReport =
                report?.base64_gzip ?? report?.content_b64_gzip ?? report?.report_b64_gzip ?? report?.base64;
              return (
                <article key={runId} className="card run-card">
                  <div className="inline">
                    <div className="card-title">{run.profile_name || "Агент"}</div>
                    <span className="status-tag" data-status={status}>
                      {status}
                    </span>
                    <span className="app-subtitle">{runId}</span>
                  </div>
                  {run.task ? <div className="card-description">{run.task}</div> : null}
                  <div className="profile-meta">
                    <div>
                      <span className="label">Session</span>
                      <div className="meta-value">{run.session_id || "—"}</div>
                    </div>
                    <div>
                      <span className="label">Шагов</span>
                      <div className="meta-value">{run.step_count ?? 0}</div>
                    </div>
                    <div>
                      <span className="label">Текущий шаг</span>
                      <div className="meta-value">{run.current_step || "—"}</div>
                    </div>
                  </div>
                  <div className="button-row">
                    <button
                      className="button ghost"
                      type="button"
                      onClick={async () => {
                        setResultModal({ open: true, runId });
                        await handleFetchRunStatus(runId);
                      }}
                      disabled={isBusy}
                    >
                      Статус
                    </button>
                    <button
                      className="button secondary"
                      type="button"
                      onClick={async () => {
                        setResultModal({ open: true, runId });
                        await handleFetchRunResult(runId);
                      }}
                      disabled={isBusy}
                    >
                      Результат
                    </button>
                    {status === "running" ? (
                      <button className="button" type="button" onClick={() => handleCancelRun(runId)} disabled={isBusy}>
                        Остановить
                      </button>
                    ) : null}
                  </div>
                  {hasReport && status !== "running" ? (
                    <div className="button-row">
                      <span className={`status-chip${transientReport ? " transient" : ""}`}>
                        {transientReport ? "Промежуточный отчет" : "Отчет готов"}
                      </span>
                      <button
                        className="button secondary"
                        type="button"
                        onClick={async () => {
                          setReportError(null);
                          try {
                            await openReport(runId, report);
                          } catch (err) {
                            setReportError(err instanceof Error ? err.message : "Не удалось открыть отчёт");
                          }
                        }}
                        disabled={isBusy}
                      >
                        Сохранить отчёт
                      </button>
                    </div>
                  ) : null}
                </article>
              );
            })}
          </div>
          {reportError ? <div className="card-description">Ошибка отчёта: {reportError}</div> : null}
          {createdAgents.length ? (
            <div className="card">
              <div className="card-title">Созданные экземпляры</div>
              <div className="cards profile-grid">
                {createdAgents.map((entry) => (
                  <article key={entry.agentId} className="card profile-card">
                    <div className="card-title">{entry.agentId}</div>
                    <div className="card-description">{entry.profile_name}</div>
                    <div className="profile-meta">
                      <div>
                        <span className="label">Session</span>
                        <div className="meta-value">{entry.session_id}</div>
                      </div>
                      <div>
                        <span className="label">Создан</span>
                        <div className="meta-value">{entry.created_at}</div>
                      </div>
                    </div>
                    <div className="button-row">
                      <button className="button ghost" type="button" onClick={() => handleRemoveCreatedAgent(entry.agentId)}>
                        Удалить
                      </button>
                    </div>
                  </article>
                ))}
              </div>
            </div>
          ) : null}
          <div className="card">
            <div className="section-header">
              <div className="card-title">История запусков</div>
              <div className="button-row">
                <button className="button ghost" type="button" onClick={handleClearAgentHistory} disabled={isBusy}>
                  Очистить историю
                </button>
              </div>
            </div>
            {agentRunHistory.length ? (
              <div className="cards profile-grid">
                {agentRunHistory.slice(0, 20).map((entry, idx) => (
                  <article key={`${entry.run_id}-${idx}`} className="card profile-card">
                    <div className="inline">
                      <div className="card-title">{entry.profile_name}</div>
                      <span className="status-tag" data-status={entry.status ?? "unknown"}>
                        {entry.status ?? "unknown"}
                      </span>
                    </div>
                    {entry.task ? <div className="card-description">{entry.task}</div> : null}
                    <div className="profile-meta">
                      <div>
                        <span className="label">Run ID</span>
                        <div className="meta-value">{entry.run_id}</div>
                      </div>
                      <div>
                        <span className="label">Старт</span>
                        <div className="meta-value">{entry.start_time ?? "—"}</div>
                      </div>
                      <div>
                        <span className="label">Финиш</span>
                        <div className="meta-value">{entry.end_time ?? "—"}</div>
                      </div>
                    </div>
                    <div className="button-row">
                      <button
                        className="button secondary"
                        type="button"
                        onClick={async () => {
                          setResultModal({ open: true, runId: entry.run_id });
                          await handleFetchRunResult(entry.run_id);
                        }}
                        disabled={isBusy}
                      >
                        Результат
                      </button>
                      <button
                        className="button ghost"
                        type="button"
                        onClick={async () => {
                          setResultModal({ open: true, runId: entry.run_id });
                          await handleFetchRunStatus(entry.run_id);
                        }}
                        disabled={isBusy}
                      >
                        Статус
                      </button>
                    </div>
                    {runResults[entry.run_id]
                      ? (() => {
                          const report = (runResults[entry.run_id] as any)?.report;
                          const transientReport = Boolean((runResults[entry.run_id] as any)?.report_transient);
                          const hasReport =
                            report?.base64_gzip ??
                            report?.content_b64_gzip ??
                            report?.report_b64_gzip ??
                            report?.base64;
                          if (!hasReport || entry.status === "running") return null;
                          return (
                            <div className="button-row">
                              <span className={`status-chip${transientReport ? " transient" : ""}`}>
                                {transientReport ? "Промежуточный отчет" : "Отчет готов"}
                              </span>
                              <button
                                className="button secondary"
                                type="button"
                                onClick={async () => {
                                  setReportError(null);
                                  try {
                                    await openReport(entry.run_id, report);
                                  } catch (err) {
                                    setReportError(err instanceof Error ? err.message : "Не удалось открыть отчёт");
                                  }
                                }}
                                disabled={isBusy}
                              >
                                Сохранить отчёт
                              </button>
                            </div>
                          );
                        })()
                      : null}
                  </article>
                ))}
              </div>
            ) : (
              <div className="card-description">История пока пуста.</div>
            )}
          </div>
        </div>
      ) : null}

      {isMounted && profileDetailsModal.open && profileDetailsModal.profile
        ? createPortal(
            <div className="modal-overlay" onClick={() => setProfileDetailsModal({ open: false, profile: null })}>
              <div className="modal" onClick={(event) => event.stopPropagation()}>
                <div className="section-header">
                  <div>
                    <div className="card-title">{profileDetailsModal.profile.name}</div>
                    <div className="card-description">{profileDetailsModal.profile.description || "Описание не задано."}</div>
                  </div>
                  <button
                    className="modal-close"
                    type="button"
                    aria-label="Закрыть"
                    onClick={() => setProfileDetailsModal({ open: false, profile: null })}
                  >
                    ×
                  </button>
                </div>

                <div className="profile-meta meta-grid">
                  <div>
                    <span className="label">Тип</span>
                    <div className="meta-value">{profileDetailsModal.profile.type ?? "agent"}</div>
                  </div>
                  <div>
                    <span className="label">Модель</span>
                    <div className="meta-value">
                      {profileDetailsModal.profile.model_key || getModelLabel(profileDetailsModal.profile.model) || "По умолчанию"}
                    </div>
                  </div>
                  <div>
                    <span className="label">Шагов</span>
                    <div className="meta-value">{profileDetailsModal.profile.max_steps ?? 0}</div>
                  </div>
                  <div>
                    <span className="label">Интервал планирования</span>
                    <div className="meta-value">{profileDetailsModal.profile.planning_interval ?? "—"}</div>
                  </div>
                </div>

                <details className="details">
                  <summary>Инструменты</summary>
                  {profileDetailsModal.profile.tools?.length ? (
                    <div className="badge-row">
                      {profileDetailsModal.profile.tools.map((tool) => (
                        <span key={tool} className="badge muted">
                          {tool}
                        </span>
                      ))}
                    </div>
                  ) : (
                    <div className="card-description">Нет инструментов.</div>
                  )}
                </details>

                {profileDetailsModal.profile.memory_policy && Object.keys(profileDetailsModal.profile.memory_policy).length ? (
                  <details className="details">
                    <summary>Политика памяти</summary>
                    <KeyValueList data={profileDetailsModal.profile.memory_policy} />
                  </details>
                ) : (
                  <div className="card-description">Политика памяти не задана.</div>
                )}
              </div>
            </div>,
            document.body,
          )
        : null}

      {isMounted && resultModal.open && resultModal.runId
        ? createPortal(
            <div className="modal-overlay" onClick={() => setResultModal({ open: false, runId: null })}>
              <div className="modal" onClick={(event) => event.stopPropagation()}>
                <div className="section-header">
                  <div>
                    <div className="card-title">Результат запуска</div>
                    <div className="card-description">{resultModal.runId}</div>
                  </div>
                  <button
                    className="modal-close"
                    type="button"
                    aria-label="Закрыть"
                    onClick={() => setResultModal({ open: false, runId: null })}
                  >
                    ×
                  </button>
                </div>

                {!reportPreviewUrl ? <div className="card-description">Отчёт пока не сформирован.</div> : null}

                {reportPreviewError ? <div className="card-description">Ошибка отчёта: {reportPreviewError}</div> : null}
                {reportPreviewUrl ? (
                  <div className="run-result">
                    <div className="label">
                      {(runResults[resultModal.runId] as any)?.report_transient ? (
                        <span className="status-chip transient">Промежуточный отчет</span>
                      ) : (
                        "Отчет"
                      )}
                    </div>
                    <iframe
                      title={`report-${resultModal.runId}`}
                      src={reportPreviewUrl}
                      style={{ width: "100%", height: 520, border: "1px solid var(--line-muted)", borderRadius: 12 }}
                    />
                  </div>
                ) : null}

                <div className="run-result">
                  <div className="label">Результат</div>
                  {renderResultValue(resultModalOutput)}
                </div>
              </div>
            </div>,
            document.body,
          )
        : null}
    </div>
  );
}
