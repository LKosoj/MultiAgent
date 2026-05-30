"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { KeyValueList } from "../shared/KeyValueList";
import { WorkflowResultView } from "../shared/WorkflowResultView";

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

const extractFinalOutput = (payload: unknown) => {
  if (!payload) return null;
  if (typeof payload !== "object") return payload;
  const typed = payload as any;
  if (typed?.artifacts?.final_output !== undefined) return typed.artifacts.final_output;
  if (typed?.final_output !== undefined) return typed.final_output;
  if (typed?.result?.final_output !== undefined) return typed.result.final_output;
  return payload;
};

type WorkflowInfo = {
  name: string;
  version?: string;
  description?: string;
  steps_count?: number;
  estimated_duration?: string;
  complexity?: string;
  category?: string;
  agents_used?: string[];
};

type WorkflowRunInfo = {
  run_id?: string;
  workflow_name?: string;
  status?: string;
  current_step?: string;
  progress_percentage?: number;
  duration_seconds?: number;
  error_message?: string;
};

type WorkflowRunHistoryEntry = {
  run_id: string;
  workflow_name?: string;
  status?: string;
  start_time?: string;
};

type Props = {
  isBusy: boolean;
  runServiceAction: (action: string, payload: Record<string, unknown>) => Promise<unknown>;
  workflowTab: "list" | "run" | "monitor" | "builder";
  setWorkflowTab: (tab: Props["workflowTab"]) => void;
  workflows: WorkflowInfo[];
  workflowsLoading: boolean;
  workflowsError: string | null;
  workflowCategoryFilter: string;
  workflowComplexityFilter: string;
  workflowSearch: string;
  workflowCategories: string[];
  workflowComplexities: string[];
  filteredWorkflows: WorkflowInfo[];
  selectedWorkflow: WorkflowInfo | null;
  workflowInputs: Record<string, unknown>;
  workflowParams: Record<string, string>;
  workflowOptions: { useEnhanced: boolean; enableTelemetry: boolean };
  workflowRuns: WorkflowRunInfo[];
  workflowRunsLoading: boolean;
  workflowRunsError: string | null;
  workflowAutoRefresh: boolean;
  currentWorkflowRunId: string | null;
  currentWorkflowRunLogs: unknown[];
  workflowResults: Record<string, unknown>;
  workflowRunHistory: WorkflowRunHistoryEntry[];
  workflowArtifacts: Record<string, unknown>;
  builderInfo: {
    name: string;
    version: string;
    type: string;
    category: string;
    description: string;
    estimated_duration: string;
    complexity: string;
  };
  builderInputs: { key: string; value: string }[];
  builderSteps: string;
  builderYaml: string;
  builderSaveName: string;
  builderError: string | null;
  workflowRunError: string | null;
  setWorkflowCategoryFilter: (v: string) => void;
  setWorkflowComplexityFilter: (v: string) => void;
  setWorkflowSearch: (v: string) => void;
  loadWorkflows: () => void;
  handleSelectWorkflow: (wf: WorkflowInfo) => void;
  handleViewWorkflowYaml: (wf: WorkflowInfo) => void;
  loadWorkflowInputs: (name: string) => void;
  setWorkflowOptions: React.Dispatch<React.SetStateAction<{ useEnhanced: boolean; enableTelemetry: boolean }>>;
  setWorkflowParams: React.Dispatch<React.SetStateAction<Record<string, string>>>;
  handleRunWorkflow: () => void;
  handleFetchWorkflowStatus: (id: string) => void;
  handleFetchWorkflowArtifacts: (id: string) => void;
  handleFetchWorkflowResult: (id: string) => void;
  handleFetchWorkflowRunLogs: (id: string) => void;
  handleCancelWorkflow: (id: string) => void;
  handleClearWorkflowHistory: () => void;
  refreshWorkflowRuns: () => void;
  setWorkflowAutoRefresh: (v: boolean) => void;
  setBuilderInfo: React.Dispatch<
    React.SetStateAction<{
      name: string;
      version: string;
      type: string;
      category: string;
      description: string;
      estimated_duration: string;
      complexity: string;
    }>
  >;
  setBuilderInputs: React.Dispatch<React.SetStateAction<{ key: string; value: string }[]>>;
  setBuilderSteps: React.Dispatch<React.SetStateAction<string>>;
  setBuilderYaml: React.Dispatch<React.SetStateAction<string>>;
  setBuilderSaveName: React.Dispatch<React.SetStateAction<string>>;
  handleGenerateYaml: () => void;
  handleSaveYaml: () => void;
};

export function WorkflowsSection({
  isBusy,
  runServiceAction,
  workflowTab,
  setWorkflowTab,
  workflows,
  workflowsLoading,
  workflowsError,
  workflowCategoryFilter,
  workflowComplexityFilter,
  workflowSearch,
  workflowCategories,
  workflowComplexities,
  filteredWorkflows,
  selectedWorkflow,
  workflowInputs,
  workflowParams,
  workflowOptions,
  workflowRuns,
  workflowRunsLoading,
  workflowRunsError,
  workflowAutoRefresh,
  currentWorkflowRunId,
  currentWorkflowRunLogs,
  workflowResults,
  workflowRunHistory,
  workflowArtifacts,
  builderInfo,
  builderInputs,
  builderSteps,
  builderYaml,
  builderSaveName,
  builderError,
  workflowRunError,
  setWorkflowCategoryFilter,
  setWorkflowComplexityFilter,
  setWorkflowSearch,
  loadWorkflows,
  handleSelectWorkflow,
  handleViewWorkflowYaml,
  loadWorkflowInputs,
  setWorkflowOptions,
  setWorkflowParams,
  handleRunWorkflow,
  handleFetchWorkflowStatus,
  handleFetchWorkflowArtifacts,
  handleFetchWorkflowResult,
  handleFetchWorkflowRunLogs,
  handleCancelWorkflow,
  handleClearWorkflowHistory,
  refreshWorkflowRuns,
  setWorkflowAutoRefresh,
  setBuilderInfo,
  setBuilderInputs,
  setBuilderSteps,
  setBuilderYaml,
  setBuilderSaveName,
  handleGenerateYaml,
  handleSaveYaml,
}: Props) {
  const [reportError, setReportError] = useState<string | null>(null);
  const [runLogs, setRunLogs] = useState<any[]>([]);
  const [logsModal, setLogsModal] = useState<{ open: boolean; runId: string | null }>({ open: false, runId: null });
  const [resultModal, setResultModal] = useState<{ open: boolean; runId: string | null }>({ open: false, runId: null });
  const [reportPreviewUrl, setReportPreviewUrl] = useState<string | null>(null);
  const [reportPreviewError, setReportPreviewError] = useState<string | null>(null);
  const reportPreviewUrlRef = React.useRef<string | null>(null);
  const reportPreviewRunIdRef = React.useRef<string | null>(null);
  const [resultModalOutput, setResultModalOutput] = useState<unknown>(null);
  const resultModalOutputRunIdRef = React.useRef<string | null>(null);
  const resultModalRunIdRef = React.useRef<string | null>(null);
  const [isMounted, setIsMounted] = useState(false);
  const [logsModalAutoRefresh, setLogsModalAutoRefresh] = useState(true);
  const [runLogsAutoRefresh, setRunLogsAutoRefresh] = useState(true);
  const runLogsInFlightRef = useRef<Set<string>>(new Set());

  const openReport = async (runId: string, report: any) => {
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
  };

  const handleGenerateReport = async (runId: string) => {
    setReportError(null);
    try {
      let resp: unknown;
      try {
        resp = await runServiceAction("workflows.generate_report", { run_id: runId });
      } catch {
        resp = await runServiceAction("telemetry.generate_report", { run_id: runId, persist: false });
      }
      const report = (resp as any)?.report ?? resp;
      await openReport(runId, report);
    } catch (err) {
      setReportError(err instanceof Error ? err.message : "Не удалось открыть отчёт");
    }
  };

  const handleRunLogs = useCallback(async (runId: string) => {
    if (runLogsInFlightRef.current.has(runId)) return;
    runLogsInFlightRef.current.add(runId);
    try {
      const resp = await runServiceAction("logs.run_logs", { run_id: runId, limit: 20000 });
      const items = (resp as any)?.logs ?? resp;
      setRunLogs(Array.isArray(items) ? items : []);
    } finally {
      runLogsInFlightRef.current.delete(runId);
    }
  }, [runServiceAction]);

  useEffect(() => {
    setIsMounted(true);
  }, []);

  useEffect(() => {
    if (!isMounted) return;
    if (!logsModal.open && !resultModal.open) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [isMounted, logsModal.open, resultModal.open]);

  useEffect(() => {
    if (!logsModal.open || !logsModal.runId) return;
    if (!logsModalAutoRefresh) return;
    void handleRunLogs(logsModal.runId);
    const intervalId = window.setInterval(() => {
      void handleRunLogs(logsModal.runId!);
    }, 4000);
    return () => window.clearInterval(intervalId);
  }, [logsModal.open, logsModal.runId, logsModalAutoRefresh, handleRunLogs]);

  useEffect(() => {
    if (!resultModal.open || !resultModal.runId) {
      resultModalRunIdRef.current = null;
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
    resultModalRunIdRef.current = resultModal.runId;
    const requestedRunId = resultModal.runId;
    const resultPayload = workflowResults[resultModal.runId] as any;
    const report = resultPayload?.report;
    let payload =
      report?.base64_gzip ??
      report?.content_b64_gzip ??
      report?.report_b64_gzip ??
      report?.base64;
    let revoked = false;
    const createPreview = async () => {
      try {
        setReportPreviewError(null);
        if (!payload) {
          const reportResp = await runServiceAction("telemetry.generate_report", {
            run_id: requestedRunId,
            persist: false,
          });
          if (revoked || resultModalRunIdRef.current !== requestedRunId) return;
          const generated = (reportResp as any)?.report ?? reportResp;
          payload =
            generated?.base64_gzip ??
            generated?.content_b64_gzip ??
            generated?.report_b64_gzip ??
            generated?.base64 ??
            null;
          if (!payload) {
            setReportPreviewUrl(null);
            return;
          }
        }
        const html = await decodeGzipBase64(payload);
        if (revoked || resultModalRunIdRef.current !== requestedRunId) return;
        const mimeType = report?.mime_type ?? "text/html";
        const blob = new Blob([html], { type: mimeType });
        const url = URL.createObjectURL(blob);
        if (reportPreviewUrlRef.current) {
          URL.revokeObjectURL(reportPreviewUrlRef.current);
        }
        reportPreviewUrlRef.current = url;
        setReportPreviewUrl(url);
      } catch (err) {
        if (revoked || resultModalRunIdRef.current !== requestedRunId) return;
        if (reportPreviewUrlRef.current) {
          URL.revokeObjectURL(reportPreviewUrlRef.current);
          reportPreviewUrlRef.current = null;
        }
        setReportPreviewUrl(null);
        setReportPreviewError(err instanceof Error ? err.message : "Не удалось открыть отчёт");
      }
    };
    if (reportPreviewRunIdRef.current === requestedRunId && reportPreviewUrlRef.current) {
      return;
    }
    reportPreviewRunIdRef.current = requestedRunId;
    void createPreview();
    return () => {
      revoked = true;
      if (reportPreviewUrlRef.current) {
        URL.revokeObjectURL(reportPreviewUrlRef.current);
        reportPreviewUrlRef.current = null;
      }
    };
    }, [resultModal.open, resultModal.runId, workflowResults, runServiceAction]);

  const formattedRunLogs = useMemo(() => {
    return runLogs
      .map((entry: any) => {
        const timestamp = entry?.timestamp ?? "";
        const level = entry?.level ?? "INFO";
        const message = entry?.message ?? "";
        const loggerName = entry?.logger_name;
        const loggerSuffix =
          loggerName &&
          loggerName !== "agent_stdout" &&
          loggerName !== "agent_stderr" &&
          loggerName !== "workflow_stdout" &&
          loggerName !== "workflow_stderr"
            ? ` (${loggerName})`
            : "";
        return `${timestamp} [${level}] ${message}${loggerSuffix}`.trim();
      })
      .join("\n");
  }, [runLogs]);

  const formattedCurrentRunLogs = useMemo(() => {
    return currentWorkflowRunLogs
      .map((entry: any) => {
        const timestamp = entry?.timestamp ?? "";
        const level = entry?.level ?? "INFO";
        const message = entry?.message ?? "";
        const loggerName = entry?.logger_name;
        const loggerSuffix =
          loggerName &&
          loggerName !== "agent_stdout" &&
          loggerName !== "agent_stderr" &&
          loggerName !== "workflow_stdout" &&
          loggerName !== "workflow_stderr"
            ? ` (${loggerName})`
            : "";
        return `${timestamp} [${level}] ${message}${loggerSuffix}`.trim();
      })
      .join("\n");
  }, [currentWorkflowRunLogs]);

  const getRunStatus = useCallback(
    (runId: string | null) => {
      if (!runId) return null;
      const active = workflowRuns.find((run) => run.run_id === runId);
      if (active?.status) return active.status;
      const history = workflowRunHistory.find((entry) => entry.run_id === runId);
      return history?.status ?? null;
    },
    [workflowRunHistory, workflowRuns],
  );

  useEffect(() => {
    if (!resultModal.open || !resultModal.runId) return;
    resultModalRunIdRef.current = resultModal.runId;
    const requestedRunId = resultModal.runId;
    let cancelled = false;
    const resultPayload = workflowResults[resultModal.runId] as any;
    const extracted = resultPayload?.result ?? null;
    if (extracted !== null && extracted !== undefined) {
      setResultModalOutput(extracted);
      resultModalOutputRunIdRef.current = requestedRunId;
      return;
    }
    const statusValue = String(resultPayload?.status ?? getRunStatus(resultModal.runId) ?? "").toLowerCase();
    if (statusValue && statusValue !== "completed" && statusValue !== "failed" && statusValue !== "cancelled") {
      setResultModalOutput(null);
      return;
    }
    if (resultModalOutputRunIdRef.current === resultModal.runId && resultModalOutput) return;
    const loadFromTrace = async () => {
      try {
        const resp = await runServiceAction("telemetry.trace_file", { run_id: requestedRunId });
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
            const parsed = JSON.parse(value);
            value = parsed;
          } catch {
            // keep as string
          }
        }
        if (cancelled || resultModalRunIdRef.current !== requestedRunId) return;
        setResultModalOutput(value);
        resultModalOutputRunIdRef.current = requestedRunId;
      } catch {
        if (cancelled || resultModalRunIdRef.current !== requestedRunId) return;
        setResultModalOutput(null);
      }
    };
    void loadFromTrace();
    return () => {
      cancelled = true;
    };
  }, [resultModal.open, resultModal.runId, runServiceAction, workflowResults, resultModalOutput, getRunStatus]);

  const currentRunStatus = useMemo(() => getRunStatus(currentWorkflowRunId), [currentWorkflowRunId, getRunStatus]);

  useEffect(() => {
    if (!currentWorkflowRunId) return;
    void handleFetchWorkflowRunLogs(currentWorkflowRunId);
  }, [currentWorkflowRunId, handleFetchWorkflowRunLogs]);

  useEffect(() => {
    if (!runLogsAutoRefresh || !currentWorkflowRunId || currentRunStatus !== "running") return;
    const intervalId = window.setInterval(() => {
      void handleFetchWorkflowRunLogs(currentWorkflowRunId);
    }, 4000);
    return () => window.clearInterval(intervalId);
  }, [currentWorkflowRunId, currentRunStatus, handleFetchWorkflowRunLogs, runLogsAutoRefresh]);
  return (
    <div className="section" id="workflows">
      <div className="section-header">
        <div className="section-title">Workflow</div>
        <div className="section-hint">Пайплайны, запуск и мониторинг</div>
      </div>
      <div className="segment-row">
        <button className={`segment-button${workflowTab === "list" ? " active" : ""}`} onClick={() => setWorkflowTab("list")}>
          Пайплайны
        </button>
        <button className={`segment-button${workflowTab === "run" ? " active" : ""}`} onClick={() => setWorkflowTab("run")}>
          Запуск
        </button>
        <button className={`segment-button${workflowTab === "monitor" ? " active" : ""}`} onClick={() => setWorkflowTab("monitor")}>
          Мониторинг
        </button>
        <button className={`segment-button${workflowTab === "builder" ? " active" : ""}`} onClick={() => setWorkflowTab("builder")}>
          Конструктор
        </button>
      </div>

      {workflowTab === "list" ? (
        <div className="stack">
          <div className="toolbar-row">
            <div className="inline">
              <button className="button secondary" type="button" onClick={loadWorkflows} disabled={isBusy}>
                Обновить список
              </button>
              <span className="status-chip">{workflowsLoading ? "Загрузка..." : `Найдено: ${workflows.length}`}</span>
            </div>
            {workflowsError ? <div className="card-description">Ошибка: {workflowsError}</div> : null}
          </div>
          <div className="filters-grid">
            <label className="field">
              <span className="label">Категория</span>
              <select value={workflowCategoryFilter} onChange={(event) => setWorkflowCategoryFilter(event.target.value)}>
                {workflowCategories.map((category) => (
                  <option key={category} value={category}>
                    {category}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span className="label">Сложность</span>
              <select value={workflowComplexityFilter} onChange={(event) => setWorkflowComplexityFilter(event.target.value)}>
                {workflowComplexities.map((complexity) => (
                  <option key={complexity} value={complexity}>
                    {complexity}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span className="label">Поиск</span>
              <input value={workflowSearch} placeholder="Имя или описание" onChange={(event) => setWorkflowSearch(event.target.value)} />
            </label>
          </div>
          {workflowsLoading ? <div className="card-description">Загрузка пайплайнов...</div> : null}
          {!workflowsLoading && filteredWorkflows.length === 0 ? (
            <div className="card-description">Пайплайны не найдены.</div>
          ) : null}
          <div className="cards profile-grid">
            {filteredWorkflows.map((workflow) => (
              <article key={workflow.name} className="card profile-card">
                <div className="inline">
                  <div className="card-title">{workflow.name}</div>
                  <span className="status-tag" data-status={workflow.complexity ?? "workflow"}>
                    {workflow.complexity ?? "workflow"}
                  </span>
                </div>
                <div className="card-description">{workflow.description || "Описание отсутствует."}</div>
                <div className="button-row">
                  <button className="button" type="button" onClick={() => handleSelectWorkflow(workflow)} disabled={isBusy}>
                    Выбрать
                  </button>
                  <button className="button secondary" type="button" onClick={() => handleViewWorkflowYaml(workflow)} disabled={isBusy}>
                    Детали
                  </button>
                </div>
              </article>
            ))}
          </div>
        </div>
      ) : null}

      {workflowTab === "run" ? (
        <div className="stack">
          {!selectedWorkflow ? (
            <div className="empty-state">
              <div className="card-title">Сначала выберите пайплайн</div>
              <div className="card-description">Перейдите во вкладку “Пайплайны” и выберите сценарий запуска.</div>
              <button className="button secondary" type="button" onClick={() => setWorkflowTab("list")}>
                К пайплайнам
              </button>
            </div>
          ) : (
            <div className="card">
              <div className="section-header">
                <div>
                  <div className="card-title">{selectedWorkflow.name}</div>
                  <div className="card-description">{selectedWorkflow.description || "Описание отсутствует."}</div>
                </div>
                <button className="button ghost" type="button" onClick={() => setWorkflowTab("list")}>
                  Сменить пайплайн
                </button>
              </div>
              <div className="profile-meta">
                <div>
                  <span className="label">Версия</span>
                  <div className="meta-value">{selectedWorkflow.version || "—"}</div>
                </div>
                <div>
                  <span className="label">Шагов</span>
                  <div className="meta-value">{selectedWorkflow.steps_count ?? 0}</div>
                </div>
                <div>
                  <span className="label">Время</span>
                  <div className="meta-value">{selectedWorkflow.estimated_duration || "—"}</div>
                </div>
                <div>
                  <span className="label">Категория</span>
                  <div className="meta-value">{selectedWorkflow.category || "general"}</div>
                </div>
                <div>
                  <span className="label">Сложность</span>
                  <div className="meta-value">{selectedWorkflow.complexity || "—"}</div>
                </div>
              </div>
              <div className="toggle-grid">
                <label className="toggle">
                  <input
                    type="checkbox"
                    checked={workflowOptions.useEnhanced}
                    onChange={(event) => setWorkflowOptions((prev) => ({ ...prev, useEnhanced: event.target.checked }))}
                  />
                  <span>Enhanced engine</span>
                </label>
                <label className="toggle">
                  <input
                    type="checkbox"
                    checked={workflowOptions.enableTelemetry}
                    onChange={(event) => setWorkflowOptions((prev) => ({ ...prev, enableTelemetry: event.target.checked }))}
                  />
                  <span>Телеметрия</span>
                </label>
              </div>
              <div className="form-grid">
                {Object.keys(workflowInputs).length === 0 ? (
                  <div className="card-description">Этот workflow не содержит inputs — проверьте YAML.</div>
                ) : (
                  Object.entries(workflowInputs).map(([key, defaultValue]) => (
                    <label className="field" key={key}>
                      <span className="label">{key}</span>
                      <input
                        value={workflowParams[key] ?? ""}
                        placeholder={defaultValue ? String(defaultValue) : "обязательный"}
                        onChange={(event) => setWorkflowParams((prev) => ({ ...prev, [key]: event.target.value }))}
                      />
                    </label>
                  ))
                )}
              </div>
              <div className="button-row">
                <button className="button" type="button" onClick={handleRunWorkflow} disabled={isBusy}>
                  Запустить
                </button>
                <button className="button secondary" type="button" onClick={() => loadWorkflowInputs(selectedWorkflow.name)} disabled={isBusy}>
                  Обновить inputs
                </button>
              </div>
              {workflowRunError ? <div className="card-description">Ошибка: {workflowRunError}</div> : null}
              <div className="run-result">
                <div className="section-header">
                  <div className="label">Логи запуска</div>
                  <div className="button-row">
                    <label className="toggle">
                      <input
                        type="checkbox"
                        checked={runLogsAutoRefresh}
                        onChange={(event) => setRunLogsAutoRefresh(event.target.checked)}
                      />
                      <span>Автообновление</span>
                    </label>
                    <button
                      className="button ghost"
                      type="button"
                      onClick={() => currentWorkflowRunId && handleFetchWorkflowRunLogs(currentWorkflowRunId)}
                      disabled={isBusy || !currentWorkflowRunId}
                    >
                      Обновить логи
                    </button>
                  </div>
                </div>
                {!currentWorkflowRunId ? (
                  <div className="card-description">Сначала запустите workflow.</div>
                ) : formattedCurrentRunLogs ? (
                  <pre className="code log-output">{formattedCurrentRunLogs}</pre>
                ) : (
                  <div className="card-description">Логов пока нет.</div>
                )}
              </div>
            </div>
          )}
        </div>
      ) : null}

      {workflowTab === "monitor" ? (
        <div className="stack">
          <div className="toolbar-row">
            <div className="inline">
              <button className="button secondary" type="button" onClick={refreshWorkflowRuns} disabled={isBusy}>
                Обновить
              </button>
              <label className="toggle">
                <input type="checkbox" checked={workflowAutoRefresh} onChange={(event) => setWorkflowAutoRefresh(event.target.checked)} />
                <span>Автообновление</span>
              </label>
            </div>
            <span className="status-chip">{workflowRunsLoading ? "Обновление..." : `Активных: ${workflowRuns.length}`}</span>
          </div>
          {workflowRunsError ? <div className="card-description">Ошибка: {workflowRunsError}</div> : null}
          {workflowRuns.length === 0 && !workflowRunsLoading ? (
            <div className="card-description">Нет активных workflow.</div>
          ) : null}
          <div className="cards">
            {workflowRuns.map((run, index) => {
              const runId = run.run_id ?? `workflow-${index + 1}`;
              const status = run.status ?? "unknown";
              const statusInfo = workflowArtifacts[`status:${runId}`];
              const resultPayload = workflowResults[runId] as any;
              const finalOutput = extractFinalOutput(resultPayload?.result);
              const report = resultPayload?.report;
              const transientReport = Boolean(resultPayload?.report_transient);
              const hasReport =
                report?.base64_gzip ?? report?.content_b64_gzip ?? report?.report_b64_gzip ?? report?.base64;
              return (
                <article key={runId} className="card run-card">
                  <div className="inline">
                    <div className="card-title">{run.workflow_name || "Workflow"}</div>
                    <span className="status-tag" data-status={status}>
                      {status}
                    </span>
                    <span className="app-subtitle">{runId}</span>
                  </div>
                  <div className="profile-meta">
                    <div>
                      <span className="label">Прогресс</span>
                      <div className="meta-value">{run.progress_percentage ? `${run.progress_percentage}%` : "—"}</div>
                    </div>
                    <div>
                      <span className="label">Шаг</span>
                      <div className="meta-value">{run.current_step || "—"}</div>
                    </div>
                    <div>
                      <span className="label">Длительность</span>
                      <div className="meta-value">{run.duration_seconds ? `${run.duration_seconds}s` : "—"}</div>
                    </div>
                  </div>
                  {run.progress_percentage ? (
                    <div className="progress-bar">
                      <div className="progress-fill" style={{ width: `${Math.min(run.progress_percentage, 100)}%` }} />
                    </div>
                  ) : null}
                  <div className="button-row">
                    <button className="button ghost" type="button" onClick={() => handleFetchWorkflowStatus(runId)} disabled={isBusy}>
                      Статус
                    </button>
                    <button
                      className="button secondary"
                      type="button"
                      onClick={() => handleFetchWorkflowArtifacts(runId)}
                      disabled={isBusy}
                    >
                      Артефакты
                    </button>
                    <button
                      className="button secondary"
                      type="button"
                      onClick={async () => {
                        setResultModal({ open: true, runId });
                        await handleFetchWorkflowResult(runId);
                      }}
                      disabled={isBusy}
                    >
                      Результат
                    </button>
                    <button
                      className="button secondary"
                      type="button"
                      onClick={async () => {
                        setLogsModal({ open: true, runId });
                        await handleRunLogs(runId);
                      }}
                      disabled={isBusy}
                    >
                      Логи
                    </button>
                    {status === "running" ? (
                      <button className="button" type="button" onClick={() => handleCancelWorkflow(runId)} disabled={isBusy}>
                        Остановить
                      </button>
                    ) : null}
                  </div>
                  {statusInfo ? (
                    <div className="run-result">
                      <div className="label">Статус</div>
                      <KeyValueList data={statusInfo} />
                    </div>
                  ) : null}
                  {resultPayload ? (
                    <div className="run-result">
                      <div className="label">Результат</div>
                      <WorkflowResultView finalOutput={finalOutput} />
                      {finalOutput ? (
                        <div className="button-row">
                          {hasReport ? (
                            <>
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
                            </>
                          ) : (
                            <button className="button secondary" type="button" onClick={() => handleGenerateReport(runId)} disabled={isBusy}>
                              Сохранить отчёт
                            </button>
                          )}
                        </div>
                      ) : null}
                    </div>
                  ) : null}
                </article>
              );
            })}
          </div>
          {reportError ? <div className="card-description">Ошибка отчёта: {reportError}</div> : null}
          <div className="card">
            <div className="section-header">
              <div className="card-title">История запусков</div>
              <div className="button-row">
                <button className="button ghost" type="button" onClick={handleClearWorkflowHistory} disabled={isBusy}>
                  Очистить историю
                </button>
              </div>
            </div>
            {workflowRunHistory.length ? (
              <div className="cards profile-grid">
                {workflowRunHistory.slice(0, 20).map((entry) => {
                  const runId = entry.run_id;
                  const statusInfo = workflowArtifacts[`status:${runId}`];
                  const resultPayload = workflowResults[runId] as any;
                  const finalOutput = extractFinalOutput(resultPayload?.result);
                  const report = resultPayload?.report;
                  const transientReport = Boolean(resultPayload?.report_transient);
                  const hasReport =
                    report?.base64_gzip ?? report?.content_b64_gzip ?? report?.report_b64_gzip ?? report?.base64;
                  return (
                    <article key={runId} className="card profile-card">
                      <div className="inline">
                        <div className="card-title">{entry.workflow_name ?? "Workflow"}</div>
                        <span className="status-tag" data-status={entry.status ?? "unknown"}>
                          {entry.status ?? "unknown"}
                        </span>
                      </div>
                      <div className="profile-meta">
                        <div>
                          <span className="label">Run ID</span>
                          <div className="meta-value">{runId}</div>
                        </div>
                        <div>
                          <span className="label">Старт</span>
                          <div className="meta-value">{entry.start_time ?? "—"}</div>
                        </div>
                      </div>
                      <div className="button-row">
                        <button className="button ghost" type="button" onClick={() => handleFetchWorkflowStatus(runId)} disabled={isBusy}>
                          Статус
                        </button>
                        <button className="button secondary" type="button" onClick={() => handleFetchWorkflowArtifacts(runId)} disabled={isBusy}>
                          Артефакты
                        </button>
                        <button
                          className="button secondary"
                          type="button"
                          onClick={async () => {
                            setResultModal({ open: true, runId });
                            await handleFetchWorkflowResult(runId);
                          }}
                          disabled={isBusy}
                        >
                          Результат
                        </button>
                        <button
                          className="button secondary"
                          type="button"
                          onClick={async () => {
                            setLogsModal({ open: true, runId });
                            await handleRunLogs(runId);
                          }}
                          disabled={isBusy}
                        >
                          Логи
                        </button>
                      </div>
                      {statusInfo ? (
                        <div className="run-result">
                          <div className="label">Статус</div>
                          <KeyValueList data={statusInfo} />
                        </div>
                      ) : null}
                      {resultPayload ? (
                        <div className="run-result">
                          <div className="label">Результат</div>
                          <WorkflowResultView finalOutput={finalOutput} />
                          {finalOutput ? (
                            <div className="button-row">
                              {hasReport ? (
                                <>
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
                                </>
                              ) : (
                                <button className="button secondary" type="button" onClick={() => handleGenerateReport(runId)} disabled={isBusy}>
                                  Сохранить отчёт
                                </button>
                              )}
                            </div>
                          ) : null}
                        </div>
                      ) : null}
                    </article>
                  );
                })}
              </div>
            ) : (
              <div className="card-description">История пока пуста.</div>
            )}
          </div>
        </div>
      ) : null}

      {workflowTab === "builder" ? (
        <div className="stack">
          <div className="card">
            <div className="section-header">
              <div className="card-title">Конструктор YAML</div>
              <div className="card-description">Упрощенный режим генерации и сохранения пайплайна.</div>
            </div>
            <div className="form-grid">
              <label className="field">
                <span className="label">Имя</span>
                <input value={builderInfo.name} onChange={(event) => setBuilderInfo((prev) => ({ ...prev, name: event.target.value }))} />
              </label>
              <label className="field">
                <span className="label">Версия</span>
                <input value={builderInfo.version} onChange={(event) => setBuilderInfo((prev) => ({ ...prev, version: event.target.value }))} />
              </label>
              <label className="field">
                <span className="label">Тип</span>
                <select value={builderInfo.type} onChange={(event) => setBuilderInfo((prev) => ({ ...prev, type: event.target.value }))}>
                  <option value="simple">simple</option>
                  <option value="enhanced">enhanced</option>
                </select>
              </label>
              <label className="field">
                <span className="label">Категория</span>
                <input value={builderInfo.category} onChange={(event) => setBuilderInfo((prev) => ({ ...prev, category: event.target.value }))} />
              </label>
              <label className="field">
                <span className="label">Время</span>
                <input
                  value={builderInfo.estimated_duration}
                  onChange={(event) => setBuilderInfo((prev) => ({ ...prev, estimated_duration: event.target.value }))}
                />
              </label>
              <label className="field">
                <span className="label">Сложность</span>
                <select
                  value={builderInfo.complexity}
                  onChange={(event) => setBuilderInfo((prev) => ({ ...prev, complexity: event.target.value }))}
                >
                  <option value="simple">simple</option>
                  <option value="medium">medium</option>
                  <option value="complex">complex</option>
                </select>
              </label>
            </div>
            <label className="field">
              <span className="label">Описание</span>
              <textarea
                value={builderInfo.description}
                onChange={(event) => setBuilderInfo((prev) => ({ ...prev, description: event.target.value }))}
                style={{ minHeight: 80 }}
              />
            </label>
            <div className="card">
              <div className="card-title">Входные параметры</div>
              <div className="card-description">Пустое значение делает параметр обязательным.</div>
              {builderInputs.map((item, index) => (
                <div className="form-grid" key={`input-${index}`}>
                  <label className="field">
                    <span className="label">Имя</span>
                    <input
                      value={item.key}
                      onChange={(event) =>
                        setBuilderInputs((prev) => {
                          const next = [...prev];
                          next[index] = { ...next[index], key: event.target.value };
                          return next;
                        })
                      }
                    />
                  </label>
                  <label className="field">
                    <span className="label">Значение по умолчанию</span>
                    <input
                      value={item.value}
                      onChange={(event) =>
                        setBuilderInputs((prev) => {
                          const next = [...prev];
                          next[index] = { ...next[index], value: event.target.value };
                          return next;
                        })
                      }
                    />
                  </label>
                  <div className="button-row">
                    <button
                      className="button ghost"
                      type="button"
                      onClick={() => setBuilderInputs((prev) => prev.filter((_, idx) => idx !== index))}
                    >
                      Удалить
                    </button>
                  </div>
                </div>
              ))}
              <div className="button-row">
                <button className="button secondary" type="button" onClick={() => setBuilderInputs((prev) => [...prev, { key: "", value: "" }])}>
                  Добавить параметр
                </button>
              </div>
            </div>
            <div className="card">
              <div className="card-title">Шаги (JSON массив)</div>
              <textarea
                className="code"
                value={builderSteps}
                onChange={(event) => setBuilderSteps(event.target.value)}
                placeholder='[{"id":"step1","type":"agent","task":"..."}]'
                style={{ minHeight: 140 }}
              />
            </div>
            <div className="button-row">
              <button className="button" type="button" onClick={handleGenerateYaml} disabled={isBusy}>
                Сгенерировать YAML
              </button>
              <button className="button secondary" type="button" onClick={() => setBuilderYaml("")} disabled={isBusy}>
                Очистить YAML
              </button>
            </div>
            {builderYaml ? (
              <div className="run-result">
                <div className="label">YAML</div>
                <textarea className="code" readOnly value={builderYaml} style={{ minHeight: 160 }} />
              </div>
            ) : null}
            <div className="divider" />
            <div className="form-grid">
              <label className="field">
                <span className="label">Имя файла</span>
                <input value={builderSaveName} onChange={(event) => setBuilderSaveName(event.target.value)} placeholder="my_pipeline" />
              </label>
            </div>
            <div className="button-row">
              <button className="button" type="button" onClick={handleSaveYaml} disabled={isBusy || !builderSaveName.trim() || !builderYaml.trim()}>
                Сохранить
              </button>
            </div>
            {builderError ? <div className="card-description">Ошибка: {builderError}</div> : null}
          </div>
        </div>
      ) : null}

      {isMounted && logsModal.open
        ? createPortal(
            <div className="modal-overlay" onClick={() => setLogsModal({ open: false, runId: null })}>
              <div className="modal" onClick={(event) => event.stopPropagation()}>
                <div className="section-header">
                  <div>
                    <div className="card-title">Логи запуска</div>
                    <div className="card-description">{logsModal.runId}</div>
                  </div>
                  <label className="toggle">
                    <input
                      type="checkbox"
                      checked={logsModalAutoRefresh}
                      onChange={(event) => setLogsModalAutoRefresh(event.target.checked)}
                    />
                    <span>Автообновление</span>
                  </label>
                  <button className="modal-close" type="button" aria-label="Закрыть" onClick={() => setLogsModal({ open: false, runId: null })}>
                    ×
                  </button>
                </div>
                <div className="run-result">
                  {formattedRunLogs ? <pre className="code log-output">{formattedRunLogs}</pre> : <div className="card-description">Логов нет.</div>}
                </div>
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
                  <div className="button-row">
                    {getRunStatus(resultModal.runId) && getRunStatus(resultModal.runId) !== "running" ? (
                      <button
                        className="button secondary"
                        type="button"
                        onClick={async () => {
                          const runId = resultModal.runId;
                          if (!runId) return;
                          const report = (workflowResults[runId] as any)?.report;
                          if (report) {
                            setReportError(null);
                            try {
                              await openReport(runId, report);
                            } catch (err) {
                              setReportError(err instanceof Error ? err.message : "Не удалось открыть отчёт");
                            }
                          } else {
                            await handleGenerateReport(runId);
                          }
                        }}
                        disabled={isBusy}
                      >
                        Сохранить отчёт
                      </button>
                    ) : null}
                    <button
                      className="modal-close"
                      type="button"
                      aria-label="Закрыть"
                      onClick={() => setResultModal({ open: false, runId: null })}
                    >
                      ×
                    </button>
                  </div>
                </div>

                {reportPreviewError ? <div className="card-description">Ошибка отчёта: {reportPreviewError}</div> : null}
                {!reportPreviewUrl ? <div className="card-description">Отчёт пока не сформирован.</div> : null}
                {reportPreviewUrl ? (
                  <div className="run-result">
                    <div className="label">Отчет</div>
                    <iframe
                      title={`report-${resultModal.runId}`}
                      src={reportPreviewUrl}
                      style={{ width: "100%", height: 520, border: "1px solid var(--line-muted)", borderRadius: 12 }}
                    />
                  </div>
                ) : null}

                <div className="run-result">
                  <div className="label">Результат</div>
                  <WorkflowResultView finalOutput={resultModalOutput} />
                </div>
              </div>
            </div>,
            document.body,
          )
        : null}
    </div>
  );
}
