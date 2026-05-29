"use client";

/* eslint-disable @typescript-eslint/no-explicit-any, react-hooks/exhaustive-deps */

import React, { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { KeyValueList } from "../shared/KeyValueList";
import { WorkflowResultView } from "../shared/WorkflowResultView";

type Props = {
  runServiceAction: (action: string, payload: Record<string, unknown>) => Promise<unknown>;
  isBusy: boolean;
  active: boolean;
};

type RunMeta = {
  sessionId?: string;
  naturalQuery: string;
  dsn: string;
  workflowName: string;
  maxRows: string;
  flags: {
    safety_level: string;
    include_explanation: boolean;
    validate_schema: boolean;
    dry_run_only: boolean;
    use_schema_suggestions: boolean;
    allow_enhanced_fallback: boolean;
  };
  dialect?: string;
};

export function TextToSqlSection({ runServiceAction, isBusy, active }: Props) {
  const [tab, setTab] = useState<"generate" | "connections" | "schema" | "history">("generate");
  const [prompt, setPrompt] = useState("");
  const [naturalQuery, setNaturalQuery] = useState("");
  const [dsn, setDsn] = useState("");
  const [sessionId, setSessionId] = useState("");
  const [workflowName, setWorkflowName] = useState("text_to_sql_pipeline");
  const [maxRows, setMaxRows] = useState("100");
  const [useEnhanced, setUseEnhanced] = useState(true);
  const [allowEnhancedFallback, setAllowEnhancedFallback] = useState(false);
  const [enableTelemetry, setEnableTelemetry] = useState(true);
  const [safetyLevel, setSafetyLevel] = useState("strict");
  const [includeExplanation, setIncludeExplanation] = useState(true);
  const [validateSchema, setValidateSchema] = useState(true);
  const [dryRunOnly, setDryRunOnly] = useState(false);
  const [useSchemaSuggestions, setUseSchemaSuggestions] = useState(true);
  const [result, setResult] = useState<any | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [runStatus, setRunStatus] = useState<any | null>(null);
  const [runArtifacts, setRunArtifacts] = useState<any | null>(null);
  const [workflowResult, setWorkflowResult] = useState<any | null>(null);
  const [autoRefreshRun, setAutoRefreshRun] = useState(true);
  const [runLogs, setRunLogs] = useState<any[]>([]);
  const [runLogsAutoRefresh, setRunLogsAutoRefresh] = useState(true);
  const [resultModal, setResultModal] = useState<{ open: boolean; runId: string | null }>({ open: false, runId: null });
  const [reportPreviewUrl, setReportPreviewUrl] = useState<string | null>(null);
  const [reportPreviewError, setReportPreviewError] = useState<string | null>(null);
  const reportPreviewUrlRef = useRef<string | null>(null);
  const resultFetchedRef = useRef<Set<string>>(new Set());
  const [isMounted, setIsMounted] = useState(false);
  const [history, setHistory] = useState<any[]>([]);
  const [analytics, setAnalytics] = useState<any | null>(null);
  const [connections, setConnections] = useState<any[]>([]);
  const [connectionName, setConnectionName] = useState("");
  const [connectionDescription, setConnectionDescription] = useState("");
  const [dsnInfo, setDsnInfo] = useState<any | null>(null);
  const [schemaFilter, setSchemaFilter] = useState("");
  const [tableFilter, setTableFilter] = useState("");
  const [allowDbSchemaFallback, setAllowDbSchemaFallback] = useState(false);
  const [schemaResult, setSchemaResult] = useState<any | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reportError, setReportError] = useState<string | null>(null);
  const [loadedConnections, setLoadedConnections] = useState(false);
  const [loadedHistory, setLoadedHistory] = useState(false);
  const [historyFilterStatus, setHistoryFilterStatus] = useState("all");
  const [historyFilterDialect, setHistoryFilterDialect] = useState("all");
  const [historySearch, setHistorySearch] = useState("");
  const savedRunsRef = useRef<Set<string>>(new Set());
  const savingRunsRef = useRef<Set<string>>(new Set());
  const runStatusInFlightRef = useRef<Set<string>>(new Set());
  const runLogsInFlightRef = useRef<Set<string>>(new Set());
  const localConnectionDsnsRef = useRef<Map<string, string>>(new Map());
  const runMetadataRef = useRef<Map<string, RunMeta>>(new Map());
  const effectiveQuery = naturalQuery.trim() || prompt.trim();

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

  const extractSqlCandidate = (payload: unknown, depth = 0): string | null => {
    if (!payload || depth > 4) return null;
    if (typeof payload !== "object") return null;
    const typed = payload as Record<string, unknown>;
    for (const key of ["sql_query", "sqlQuery", "sql", "generated_sql"]) {
      const value = typed[key];
      if (typeof value === "string" && value.trim()) return value.trim();
    }
    for (const key of ["step_outputs", "stepOutputs", "final_output", "finalOutput", "result", "artifacts", "output"]) {
      const nested = extractSqlCandidate(typed[key], depth + 1);
      if (nested) return nested;
    }
    for (const value of Object.values(typed)) {
      const nested = extractSqlCandidate(value, depth + 1);
      if (nested) return nested;
    }
    return null;
  };

  const openReport = async (runIdValue: string, report: any) => {
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
    const filename = report?.filename ?? `report_${runIdValue}.html`;
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

  const handleGenerateReport = async () => {
    if (!runId) return;
    setReportError(null);
    try {
      const resp = await runServiceAction("workflows.generate_report", { run_id: runId });
      const report = (resp as any)?.report ?? resp;
      await openReport(runId, report);
    } catch (err) {
      setReportError(err instanceof Error ? err.message : "Не удалось открыть отчёт");
    }
  };

  const handleGenerate = async () => {
    setError(null);
    setResult(null);
    setRunStatus(null);
    setRunArtifacts(null);
    setWorkflowResult(null);
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
    const runMeta: RunMeta = {
      sessionId: sessionId || undefined,
      naturalQuery: effectiveQuery,
      dsn,
      workflowName,
      maxRows: maxRowsText,
      flags: {
        safety_level: safetyLevel,
        include_explanation: includeExplanation,
        validate_schema: validateSchema,
        dry_run_only: dryRunOnly,
        use_schema_suggestions: useSchemaSuggestions,
        allow_enhanced_fallback: allowEnhancedFallback,
      },
      dialect: dsnInfo?.validation?.result?.detected_scheme ?? undefined,
    };
    try {
      const resp = await runServiceAction("presets.text_to_sql.generate", {
        query: effectiveQuery,
        natural_query: effectiveQuery,
        dsn,
        max_rows: normalizedMaxRows,
        workflow_name: workflowName || undefined,
        session_id: sessionId || undefined,
        use_enhanced: useEnhanced,
        allow_enhanced_fallback: allowEnhancedFallback,
        enable_telemetry: enableTelemetry,
        safety_level: safetyLevel,
        include_explanation: includeExplanation,
        validate_schema: validateSchema,
        dry_run_only: dryRunOnly,
        use_schema_suggestions: useSchemaSuggestions,
      });
      setResult(resp);
      const nextRunId = (resp as any)?.run_id ?? null;
      setRunId(nextRunId);
      if (nextRunId) {
        runMetadataRef.current.set(nextRunId, {
          ...runMeta,
          sessionId: (resp as any)?.session_id ?? runMeta.sessionId,
        });
        void loadRunStatus(nextRunId);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Ошибка генерации SQL");
    }
  };

  const loadHistory = async () => {
    setError(null);
    try {
      const resp = await runServiceAction("text_to_sql.history.list", { limit: 100 });
      const items = Array.isArray((resp as any)?.entries) ? (resp as any).entries : Array.isArray(resp) ? resp : [];
      setHistory(items);
      const analyticsResp = await runServiceAction("text_to_sql.history.analytics", { limit: 100 });
      setAnalytics((analyticsResp as any)?.result ?? analyticsResp);
      setLoadedHistory(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось получить историю");
    }
  };

  const clearHistory = async () => {
    setError(null);
    try {
      await runServiceAction("text_to_sql.history.clear", { confirm: true });
      setHistory([]);
      setAnalytics(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось очистить историю");
    }
  };

  const loadConnections = async () => {
    setError(null);
    try {
      const resp = await runServiceAction("db.test_configs.list", {});
      const items = Array.isArray((resp as any)?.configs) ? (resp as any).configs : [];
      setConnections(
        items.map((item: any) => {
          const localDsn = typeof item?.name === "string" ? localConnectionDsnsRef.current.get(item.name) : undefined;
          return localDsn ? { ...item, dsn: localDsn, masked_dsn: item.dsn } : item;
        }),
      );
      setLoadedConnections(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось получить подключения");
    }
  };

  useEffect(() => {
    if (tab === "connections" && !loadedConnections) {
      void loadConnections();
    }
    if (tab === "history" && !loadedHistory) {
      void loadHistory();
    }
  }, [tab, loadedConnections, loadedHistory]);

  useEffect(() => {
    setIsMounted(true);
  }, []);

  useEffect(() => {
    if (!active && resultModal.open) {
      setResultModal({ open: false, runId: null });
    }
  }, [active, resultModal.open]);

  useEffect(() => {
    if (!active) return;
    if (!isMounted) return;
    if (!resultModal.open) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [active, isMounted, resultModal.open]);

  const saveConnection = async () => {
    setError(null);
    try {
      await runServiceAction("db.test_configs.save", {
        name: connectionName,
        dsn,
        description: connectionDescription,
      });
      localConnectionDsnsRef.current.set(connectionName, dsn);
      setConnectionName("");
      setConnectionDescription("");
      await loadConnections();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось сохранить подключение");
    }
  };

  const deleteConnection = async (name: string) => {
    setError(null);
    try {
      await runServiceAction("db.test_configs.delete", { name });
      localConnectionDsnsRef.current.delete(name);
      await loadConnections();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось удалить подключение");
    }
  };

  const analyzeDsn = async () => {
    if (!dsn.trim()) return;
    setError(null);
    try {
      const validation = await runServiceAction("db.validate_dsn", { dsn });
      const validationResult = (validation as any)?.result;
      if (validationResult?.is_valid === false) {
        setDsnInfo({ validation, dialect: null });
        const validationErrors = Array.isArray(validationResult.errors) ? validationResult.errors.filter(Boolean).join("; ") : "";
        setError(validationErrors || "DSN не прошёл валидацию");
        return;
      }
      const detectedScheme = validationResult?.detected_scheme;
      const dialect = detectedScheme ? await runServiceAction("db.dialect_info", { scheme: detectedScheme }) : null;
      setDsnInfo({ validation, dialect });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось проверить DSN");
    }
  };

  const loadSchema = async () => {
    if (!dsn.trim()) return;
    setError(null);
    try {
      const resp = await runServiceAction("text_to_sql.schema.load", {
        dsn,
        schema: schemaFilter || undefined,
        table_name: tableFilter || undefined,
        allow_db_schema_fallback: allowDbSchemaFallback,
      });
      setSchemaResult(resp);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось получить схему");
    }
  };

  const loadRunStatus = async (runIdValue?: string | null) => {
    const targetRunId = runIdValue || runId;
    if (!targetRunId) return;
    if (runStatusInFlightRef.current.has(targetRunId)) return;
    runStatusInFlightRef.current.add(targetRunId);
    setError(null);
    try {
      const [statusResp, artifactsResp] = await Promise.all([
        runServiceAction("workflows.status", { run_id: targetRunId }),
        runServiceAction("workflows.artifacts", { run_id: targetRunId }),
      ]);
      const statusData = (statusResp as any)?.status ?? statusResp;
      const artifactsData = (artifactsResp as any)?.artifacts ?? artifactsResp;
      setRunStatus(statusData);
      setRunArtifacts(artifactsData);
      if (
        (statusData?.status === "completed" || statusData?.status === "failed") &&
        !savedRunsRef.current.has(targetRunId) &&
        !savingRunsRef.current.has(targetRunId)
      ) {
        savingRunsRef.current.add(targetRunId);
        const runMeta = runMetadataRef.current.get(targetRunId);
        let resultResp: unknown = null;
        try {
          resultResp = await runServiceAction("workflows.result", { run_id: targetRunId });
          setWorkflowResult(resultResp);
        } catch {
          resultResp = null;
        }
        const resultRecord = resultResp as Record<string, unknown> | null;
        const finalOutput = extractFinalOutput(resultRecord?.result);
        const sqlQuery = extractSqlCandidate(resultResp) ?? extractSqlCandidate(artifactsData) ?? undefined;
        try {
          await runServiceAction("text_to_sql.history.append", {
            entry: {
              run_id: targetRunId,
              session_id: runMeta?.sessionId ?? result?.session_id,
              natural_query: runMeta?.naturalQuery ?? effectiveQuery,
              query: runMeta?.naturalQuery ?? effectiveQuery,
              sql_query: sqlQuery,
              final_output: finalOutput ?? undefined,
              dsn: runMeta?.dsn ?? dsn,
              workflow_name: runMeta?.workflowName ?? workflowName,
              max_rows: runMeta?.maxRows ?? maxRows,
              flags: runMeta?.flags ?? {
                safety_level: safetyLevel,
                include_explanation: includeExplanation,
                validate_schema: validateSchema,
                dry_run_only: dryRunOnly,
                use_schema_suggestions: useSchemaSuggestions,
                allow_enhanced_fallback: allowEnhancedFallback,
              },
              status: statusData?.status,
              success: statusData?.status === "completed",
              error: statusData?.error ?? resultRecord?.error,
              timestamp: new Date().toISOString(),
              dialect: runMeta?.dialect ?? dsnInfo?.validation?.result?.detected_scheme ?? undefined,
            },
          });
          savedRunsRef.current.add(targetRunId);
          runMetadataRef.current.delete(targetRunId);
        } finally {
          savingRunsRef.current.delete(targetRunId);
        }
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось получить статус запуска");
    } finally {
      runStatusInFlightRef.current.delete(targetRunId);
    }
  };

  const loadRunResult = async (runIdValue?: string | null) => {
    const targetRunId = runIdValue || runId;
    if (!targetRunId) return;
    try {
      const resp = await runServiceAction("workflows.result", { run_id: targetRunId });
      setWorkflowResult(resp as any);
    } catch {
      setWorkflowResult(null);
    }
  };

  useEffect(() => {
    if (!active || !resultModal.open || !resultModal.runId) {
      if (reportPreviewUrlRef.current) {
        URL.revokeObjectURL(reportPreviewUrlRef.current);
        reportPreviewUrlRef.current = null;
      }
      setReportPreviewUrl(null);
      setReportPreviewError(null);
      return;
    }
    const report = (workflowResult as any)?.report;
    const payload =
      report?.base64_gzip ??
      report?.content_b64_gzip ??
      report?.report_b64_gzip ??
      report?.base64;
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
  }, [active, resultModal.open, resultModal.runId, workflowResult]);

  const loadRunLogs = async (runIdValue?: string | null) => {
    const targetRunId = runIdValue || runId;
    if (!targetRunId) return;
    if (runLogsInFlightRef.current.has(targetRunId)) return;
    runLogsInFlightRef.current.add(targetRunId);
    try {
      const resp = await runServiceAction("logs.run_logs", { run_id: targetRunId, limit: 20000 });
      const items = (resp as any)?.logs ?? resp;
      setRunLogs(Array.isArray(items) ? items : []);
    } catch {
      setRunLogs([]);
    } finally {
      runLogsInFlightRef.current.delete(targetRunId);
    }
  };

  useEffect(() => {
    if (!active || !autoRefreshRun || !runId) return;
    const id = window.setInterval(() => void loadRunStatus(runId), 3000);
    return () => window.clearInterval(id);
  }, [active, autoRefreshRun, runId]);

  useEffect(() => {
    const statusValue = runStatus?.status ?? runStatus?.state ?? "";
    if (!runId || !["completed", "failed", "cancelled"].includes(statusValue)) return;
    if (resultFetchedRef.current.has(runId)) return;
    resultFetchedRef.current.add(runId);
    void loadRunResult(runId);
  }, [runId, runStatus]);

  useEffect(() => {
    if (!active || !runId) return;
    void loadRunLogs(runId);
  }, [active, runId]);

  useEffect(() => {
    const statusValue = runStatus?.status ?? runStatus?.state ?? "";
    if (!active || !runLogsAutoRefresh || !runId || statusValue !== "running") return;
    const id = window.setInterval(() => void loadRunLogs(runId), 3000);
    return () => window.clearInterval(id);
  }, [active, runId, runLogsAutoRefresh, runStatus]);

  const historyDialects = useMemo(() => {
    const values = new Set<string>();
    history.forEach((entry) => {
      const dialect = entry?.dialect ?? entry?.db_type ?? entry?.scheme;
      if (typeof dialect === "string" && dialect) values.add(dialect);
    });
    return Array.from(values);
  }, [history]);

  const filteredHistory = useMemo(() => {
    return history.filter((entry) => {
      if (historyFilterStatus !== "all") {
        const success = entry?.success;
        if (historyFilterStatus === "success" && success !== true) return false;
        if (historyFilterStatus === "failed" && success !== false) return false;
        if (historyFilterStatus === "unknown" && success != null) return false;
      }
      if (historyFilterDialect !== "all") {
        const dialect = entry?.dialect ?? entry?.db_type ?? entry?.scheme;
        if (dialect !== historyFilterDialect) return false;
      }
      if (historySearch.trim()) {
        const text = `${entry?.sql_query ?? ""} ${entry?.sql ?? ""} ${entry?.final_output ?? ""} ${entry?.natural_query ?? ""} ${entry?.query ?? ""} ${entry?.prompt ?? ""} ${entry?.question ?? ""}`.toLowerCase();
        if (!text.includes(historySearch.toLowerCase())) return false;
      }
      return true;
    });
  }, [history, historyFilterStatus, historyFilterDialect, historySearch]);

  const resultSql = extractSqlCandidate(workflowResult) ?? extractSqlCandidate(result) ?? "";
  const resultSchema = result?.schema ?? result?.parameters?.schema ?? "";
  const runStatusValue = runStatus?.status ?? runStatus?.state ?? "";
  const resultStatus = result?.status ?? result?.state ?? runStatusValue;
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

  return (
    <div className="section" id="text-to-sql">
      <div className="section-header">
        <div className="section-title">Text-to-SQL</div>
        <div className="section-hint">Формирование SQL запросов по описанию</div>
      </div>

      <div className="segment-row">
        <button className={`segment-button${tab === "generate" ? " active" : ""}`} onClick={() => setTab("generate")}>
          Генерация
        </button>
        <button className={`segment-button${tab === "connections" ? " active" : ""}`} onClick={() => setTab("connections")}>
          Подключения
        </button>
        <button className={`segment-button${tab === "schema" ? " active" : ""}`} onClick={() => setTab("schema")}>
          Схема БД
        </button>
        <button className={`segment-button${tab === "history" ? " active" : ""}`} onClick={() => setTab("history")}>
          История
        </button>
      </div>

      {tab === "generate" ? (
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
              <span className="label">DSN</span>
              <input value={dsn} onChange={(e) => setDsn(e.target.value)} placeholder="scheme://user:pass@host/db" />
            </label>
            <label className="field">
              <span className="label">Workflow</span>
              <input value={workflowName} onChange={(e) => setWorkflowName(e.target.value)} />
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
              <input type="checkbox" checked={useEnhanced} onChange={(e) => setUseEnhanced(e.target.checked)} />
              <span>Enhanced engine</span>
            </label>
            <label className="toggle">
              <input type="checkbox" checked={allowEnhancedFallback} onChange={(e) => setAllowEnhancedFallback(e.target.checked)} />
              <span>Enhanced fallback</span>
            </label>
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
            <label className="toggle">
              <input type="checkbox" checked={useSchemaSuggestions} onChange={(e) => setUseSchemaSuggestions(e.target.checked)} />
              <span>Подсказки схемы</span>
            </label>
          </div>
          <div className="button-row">
            <button className="button" type="button" onClick={handleGenerate} disabled={isBusy || !effectiveQuery || !dsn.trim()}>
              Сгенерировать
            </button>
            <button className="button secondary" type="button" onClick={analyzeDsn} disabled={isBusy || !dsn.trim()}>
              Проверить подключение
            </button>
          </div>
          {error ? <div className="card-description">Ошибка: {error}</div> : null}
          {dsnInfo ? (
            <div className="card" style={{ background: "var(--panel-strong)" }}>
              <div className="card-title">Информация о подключении</div>
              <div className="profile-meta">
                <div>
                  <span className="label">Схема</span>
                  <div className="meta-value">{(dsnInfo.validation as any)?.result?.detected_schema ?? "—"}</div>
                </div>
                <div>
                  <span className="label">Тип БД</span>
                  <div className="meta-value">{(dsnInfo.validation as any)?.result?.detected_scheme ?? "—"}</div>
                </div>
                <div>
                  <span className="label">Диалект</span>
                  <div className="meta-value">{(dsnInfo.dialect as any)?.result?.dialect_label ?? "—"}</div>
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
                  <span className="label">Workflow</span>
                  <div className="meta-value">{workflowName}</div>
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
                  <WorkflowResultView finalOutput={extractFinalOutput((workflowResult as any)?.result)} />
                  {extractFinalOutput((workflowResult as any)?.result) ? (
                    <div className="button-row">
                      {(workflowResult as any)?.report ? (
                        <button
                          className="button secondary"
                          type="button"
                          onClick={async () => {
                            setReportError(null);
                            try {
                              await openReport(runId, (workflowResult as any)?.report);
                            } catch (err) {
                              setReportError(err instanceof Error ? err.message : "Не удалось открыть отчёт");
                            }
                          }}
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
      ) : null}

      {tab === "connections" ? (
        <div className="stack">
          <div className="card">
            <div className="section-header">
              <div className="card-title">Подключения</div>
              <div className="card-description">Сохранённые DSN для Text-to-SQL</div>
            </div>
            <div className="form-grid">
              <label className="field">
                <span className="label">Имя</span>
                <input value={connectionName} onChange={(e) => setConnectionName(e.target.value)} placeholder="prod_db" />
              </label>
              <label className="field">
                <span className="label">DSN</span>
                <input value={dsn} onChange={(e) => setDsn(e.target.value)} placeholder="scheme://user:pass@host/db" />
              </label>
              <label className="field">
                <span className="label">Описание</span>
                <input value={connectionDescription} onChange={(e) => setConnectionDescription(e.target.value)} placeholder="Основная БД" />
              </label>
            </div>
            <div className="button-row">
              <button className="button" type="button" onClick={saveConnection} disabled={isBusy || !connectionName.trim() || !dsn.trim()}>
                Сохранить
              </button>
              <button className="button secondary" type="button" onClick={loadConnections} disabled={isBusy}>
                Обновить список
              </button>
            </div>
          </div>
          <div className="cards">
            {connections.map((conn, idx) => (
              <article key={conn.name ?? `conn-${idx}`} className="card">
                <div className="inline">
                  <div className="card-title">{conn.name ?? "Подключение"}</div>
                  <span className="app-subtitle">{conn.created_at ?? ""}</span>
                </div>
                <div className="card-description">{conn.description ?? "Описание отсутствует."}</div>
                <div className="label">DSN</div>
                <div className="meta-value" style={{ wordBreak: "break-all" }}>
                  {conn.masked_dsn ?? conn.dsn}
                </div>
                <div className="button-row">
                  <button
                    className="button ghost"
                    type="button"
                    onClick={() => setDsn(conn.connection_ref ?? conn.dsn)}
                    disabled={
                      typeof (conn.connection_ref ?? conn.dsn) !== "string"
                      || ((conn.connection_ref ?? conn.dsn).includes("***") || (conn.connection_ref ?? conn.dsn).includes("<redacted>"))
                    }
                    title={
                      typeof (conn.connection_ref ?? conn.dsn) === "string"
                        && ((conn.connection_ref ?? conn.dsn).includes("***") || (conn.connection_ref ?? conn.dsn).includes("<redacted>"))
                        ? "DSN скрыт; введите подключение заново"
                        : undefined
                    }
                  >
                    Выбрать
                  </button>
                  <button className="button secondary" type="button" onClick={() => deleteConnection(conn.name)} disabled={isBusy}>
                    Удалить
                  </button>
                </div>
              </article>
            ))}
            {connections.length === 0 ? <div className="card-description">Сохранённых подключений нет.</div> : null}
          </div>
        </div>
      ) : null}

      {tab === "schema" ? (
        <div className="stack">
          <div className="card">
            <div className="section-header">
              <div className="card-title">Схема БД</div>
              <div className="card-description">Интроспекция таблиц и колонок</div>
            </div>
            <div className="form-grid">
              <label className="field">
                <span className="label">DSN</span>
                <input value={dsn} onChange={(e) => setDsn(e.target.value)} placeholder="scheme://user:pass@host/db" />
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
              <button className="button" type="button" onClick={loadSchema} disabled={isBusy || !dsn.trim()}>
                Загрузить схему
              </button>
            </div>
          </div>
          {schemaResult ? (
            <div className="cards">
              <article className="card">
                <div className="card-title">Схема</div>
                <div className="card-description">Источник: {(schemaResult as any)?.source ?? "—"}</div>
                {Array.isArray((schemaResult as any)?.warnings) && (schemaResult as any).warnings.length > 0 ? (
                  <div className="card-description">{(schemaResult as any).warnings.join("; ")}</div>
                ) : null}
                <div className="card-description">Количество таблиц: {Object.keys((schemaResult as any)?.schema ?? {}).length}</div>
                <details className="details">
                  <summary>Таблицы</summary>
                  <div className="graph-inputs">
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
      ) : null}

      {tab === "history" ? (
        <div className="stack">
          <div className="card">
            <div className="section-header">
              <div className="card-title">Фильтры истории</div>
            </div>
            <div className="form-grid">
              <label className="field">
                <span className="label">Статус</span>
                <select value={historyFilterStatus} onChange={(e) => setHistoryFilterStatus(e.target.value)}>
                  <option value="all">Все</option>
                  <option value="success">Успешные</option>
                  <option value="failed">Ошибки</option>
                  <option value="unknown">Неизвестно</option>
                </select>
              </label>
              <label className="field">
                <span className="label">Диалект</span>
                <select value={historyFilterDialect} onChange={(e) => setHistoryFilterDialect(e.target.value)}>
                  <option value="all">Все</option>
                  {historyDialects.map((dialect) => (
                    <option key={dialect} value={dialect}>
                      {dialect}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span className="label">Поиск</span>
                <input value={historySearch} onChange={(e) => setHistorySearch(e.target.value)} placeholder="SQL или запрос" />
              </label>
            </div>
          </div>
          <div className="button-row">
            <button className="button secondary" type="button" onClick={loadHistory} disabled={isBusy}>
              Обновить историю
            </button>
            <button className="button ghost" type="button" onClick={clearHistory} disabled={isBusy}>
              Очистить историю
            </button>
          </div>
          {filteredHistory && filteredHistory.length ? (
            <div className="cards">
              {filteredHistory.slice(-20).reverse().map((item, idx) => (
                <article key={idx} className="card">
                  <div className="card-title">{item.sql_query ?? item.sql ?? "SQL не сохранён"}</div>
                  <div className="card-description">{item.natural_query ?? item.query ?? item.prompt ?? item.question ?? ""}</div>
                  <div className="profile-meta">
                    <div>
                      <span className="label">DSN</span>
                      <div className="meta-value">{item.dsn ?? "—"}</div>
                    </div>
                    <div>
                      <span className="label">Время</span>
                      <div className="meta-value">{item.timestamp ?? "—"}</div>
                    </div>
                    <div>
                      <span className="label">Статус</span>
                      <div className="meta-value">{item.success === true ? "OK" : item.success === false ? "Ошибка" : "—"}</div>
                    </div>
                    <div>
                      <span className="label">Строк</span>
                      <div className="meta-value">{item.max_rows ?? "—"}</div>
                    </div>
                  </div>
                  {item.final_output ? (
                    <details className="details">
                      <summary>Итоговый вывод</summary>
                      <div className="card-description" style={{ whiteSpace: "pre-wrap" }}>
                        {String(item.final_output)}
                      </div>
                    </details>
                  ) : null}
                </article>
              ))}
            </div>
          ) : (
            <div className="card-description">История пуста.</div>
          )}
          {analytics ? (
            <div className="card" style={{ background: "var(--panel-strong)" }}>
              <div className="card-title">Аналитика истории</div>
              <div className="profile-meta">
                <div>
                  <span className="label">Запросов</span>
                  <div className="meta-value">{analytics.total ?? analytics.total_queries ?? "—"}</div>
                </div>
                <div>
                  <span className="label">Диалекты</span>
                  <div className="meta-value">
                    {Array.isArray(analytics.dialects)
                      ? analytics.dialects.map((d: any) => `${d.dialect}:${d.count}`).join(", ")
                      : Array.isArray(analytics.top_dsns)
                      ? analytics.top_dsns.join(", ")
                      : "—"}
                  </div>
                </div>
                <div>
                  <span className="label">Успех</span>
                  <div className="meta-value">{analytics.success ? `${analytics.success.success ?? 0}/${analytics.total ?? 0}` : "—"}</div>
                </div>
              </div>
            </div>
          ) : null}
        </div>
      ) : null}

      {active && isMounted && resultModal.open && resultModal.runId
        ? createPortal(
            <div className="modal-overlay" onClick={() => setResultModal({ open: false, runId: null })}>
              <div className="modal" onClick={(event) => event.stopPropagation()}>
                <div className="section-header">
                  <div>
                    <div className="card-title">Результат запуска</div>
                    <div className="card-description">{resultModal.runId}</div>
                  </div>
                  <div className="button-row">
                    {runStatusValue && runStatusValue !== "running" ? (
                      <button
                        className="button secondary"
                        type="button"
                        onClick={async () => {
                          if ((workflowResult as any)?.report) {
                            setReportError(null);
                            try {
                              await openReport(resultModal.runId!, (workflowResult as any)?.report);
                            } catch (err) {
                              setReportError(err instanceof Error ? err.message : "Не удалось открыть отчёт");
                            }
                          } else {
                            await handleGenerateReport();
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
                  <WorkflowResultView finalOutput={extractFinalOutput((workflowResult as any)?.result) ?? result} />
                </div>
              </div>
            </div>,
            document.body,
          )
        : null}
    </div>
  );
}
