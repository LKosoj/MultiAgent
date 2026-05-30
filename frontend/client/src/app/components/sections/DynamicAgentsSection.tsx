"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { KeyValueList } from "../shared/KeyValueList";

type DynamicProfile = {
  name?: string;
  description?: string;
  type?: string;
  model?: string;
  tools?: string[];
  max_steps?: number;
  planning_interval?: number | null;
  memory_policy?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
};

type AgentProfile = {
  name?: string;
  type?: string;
  description?: string;
};

type TeamMember = {
  name: string;
  type: "standard" | "dynamic" | "template";
  description: string;
  role: string;
};

type TeamRunForm = {
  managerType: string;
  task: string;
  sessionId: string;
  enableTelemetry: boolean;
  maxParallel: number;
};

type ActiveRun = {
  run_id?: string;
  status?: string;
  profile_name?: string;
  manager_profile?: string;
  team_profiles?: string[];
  task?: string;
  session_id?: string;
  start_time?: string;
  is_dynamic?: boolean;
};

type Props = {
  runServiceAction: (action: string, payload: Record<string, unknown>) => Promise<unknown>;
  isBusy: boolean;
};

type DefinitionState = {
  name: string;
  type: string;
  description: string;
  model: string;
  max_steps: number;
  planning_interval: number | null;
  instructions: string;
  tools: string[];
  memory_policy: Record<string, unknown>;
  metadata: Record<string, unknown>;
  max_tool_threads: number;
};

const DEFAULT_DEFINITION: DefinitionState = {
  name: "",
  type: "code",
  description: "",
  model: "model_hard",
  max_steps: 20,
  planning_interval: 0,
  instructions: "",
  tools: [] as string[],
  memory_policy: { enable_memory: true, provide_run_summary: false },
  metadata: { created_by: "ag-ui" },
  max_tool_threads: 1,
};

const managerOptions = ["manager", "project_manager", "custom"];

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

export function DynamicAgentsSection({ runServiceAction, isBusy }: Props) {
  const [profiles, setProfiles] = useState<DynamicProfile[]>([]);
  const [availableTools, setAvailableTools] = useState<string[]>([]);
  const [agentProfiles, setAgentProfiles] = useState<AgentProfile[]>([]);
  const [availableModelKeys, setAvailableModelKeys] = useState<string[]>([]);
  const [activeTab, setActiveTab] = useState<"constructor" | "team" | "runs" | "manage" | "import">("constructor");
  const [definition, setDefinition] = useState<DefinitionState>({ ...DEFAULT_DEFINITION });
  const [sessionId, setSessionId] = useState("");
  const [preview, setPreview] = useState<Record<string, unknown> | null>(null);
  const [createResult, setCreateResult] = useState<Record<string, unknown> | null>(null);
  const [lastCreatedAgentId, setLastCreatedAgentId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [templates, setTemplates] = useState<Record<string, DynamicProfile>>(() => {
    try {
      const saved = localStorage.getItem("agui-dynamic-templates");
      return saved ? (JSON.parse(saved) as Record<string, DynamicProfile>) : {};
    } catch {
      return {};
    }
  });
  const [selectedTemplate, setSelectedTemplate] = useState("");
  const [team, setTeam] = useState<TeamMember[]>([]);
  const [teamForm, setTeamForm] = useState<TeamRunForm>({
    managerType: "manager",
    task: "",
    sessionId: "",
    enableTelemetry: true,
    maxParallel: 2,
  });
  const [activeRuns, setActiveRuns] = useState<ActiveRun[]>([]);
  const [runDetails, setRunDetails] = useState<Record<string, unknown>>({});
  const [runEvents, setRunEvents] = useState<Record<string, unknown>>({});
  const [runResults, setRunResults] = useState<Record<string, unknown>>({});
  const [runLogs, setRunLogs] = useState<any[]>([]);
  const [logsModal, setLogsModal] = useState<{ open: boolean; runId: string | null }>({ open: false, runId: null });
  const [logsModalAutoRefresh, setLogsModalAutoRefresh] = useState(true);
  const [resultModal, setResultModal] = useState<{ open: boolean; runId: string | null }>({ open: false, runId: null });
  const [reportPreviewUrl, setReportPreviewUrl] = useState<string | null>(null);
  const [reportPreviewError, setReportPreviewError] = useState<string | null>(null);
  const reportPreviewUrlRef = useRef<string | null>(null);
  const [autoRefreshRuns, setAutoRefreshRuns] = useState(false);
  const [reportError, setReportError] = useState<string | null>(null);
  const [yamlImport, setYamlImport] = useState<DynamicProfile | null>(null);
  const [isMounted, setIsMounted] = useState(false);

  const loadProfiles = useCallback(async () => {
    setError(null);
    try {
      const resp = (await runServiceAction("agents.dynamic.list", {})) as { profiles?: DynamicProfile[] };
      setProfiles(Array.isArray(resp?.profiles) ? resp.profiles : []);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось получить список динамических агентов");
    }
  }, [runServiceAction]);

  const loadTools = useCallback(async () => {
    try {
      const [defs, mcp] = await Promise.all([
        runServiceAction("tools.list_definitions", {}),
        runServiceAction("tools.list_mcp", {}),
      ]);
      const defTools = Array.isArray((defs as any)?.tools) ? (defs as any).tools.map((tool: any) => tool.name).filter(Boolean) : [];
      const mcpTools = Array.isArray((mcp as any)?.tools) ? (mcp as any).tools : [];
      const combined = Array.from(new Set([...defTools, ...mcpTools])).sort();
      setAvailableTools(combined);
    } catch {
      setAvailableTools([]);
    }
  }, [runServiceAction]);

  const loadAgentProfiles = useCallback(async () => {
    try {
      const resp = (await runServiceAction("agents.list", {})) as { agents?: AgentProfile[] };
      setAgentProfiles(Array.isArray(resp?.agents) ? resp.agents : []);
    } catch {
      setAgentProfiles([]);
    }
  }, [runServiceAction]);

  const loadModelKeys = useCallback(async () => {
    try {
      const resp = (await runServiceAction("config.llm_providers", {})) as { providers?: Record<string, any> };
      const providers = resp?.providers ?? {};
      const openai = providers?.openai ?? {};
      const modelDetails = openai?.model_details ?? {};
      const keys = Object.keys(modelDetails).filter((k) => k.startsWith("model_"));
      setAvailableModelKeys(keys.sort());
    } catch {
      setAvailableModelKeys([]);
    }
  }, [runServiceAction]);

  const loadActiveRuns = useCallback(async () => {
    try {
      const resp = (await runServiceAction("system.active_runs", {})) as { agents?: ActiveRun[] };
      const runs = Array.isArray(resp?.agents) ? resp.agents : [];
      setActiveRuns(runs);
    } catch {
      setActiveRuns([]);
    }
  }, [runServiceAction]);

  useEffect(() => {
    void loadProfiles();
    void loadTools();
    void loadAgentProfiles();
    void loadModelKeys();
  }, [loadProfiles, loadTools, loadAgentProfiles, loadModelKeys]);

  useEffect(() => {
    if (!autoRefreshRuns) return;
    const id = window.setInterval(() => void loadActiveRuns(), 3000);
    return () => window.clearInterval(id);
  }, [autoRefreshRuns, loadActiveRuns]);

  useEffect(() => {
    if (activeTab === "runs") {
      void loadActiveRuns();
    }
  }, [activeTab, loadActiveRuns]);

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
    if (!resultModal.open || !resultModal.runId) {
      if (reportPreviewUrlRef.current) {
        URL.revokeObjectURL(reportPreviewUrlRef.current);
        reportPreviewUrlRef.current = null;
      }
      setReportPreviewUrl(null);
      setReportPreviewError(null);
      return;
    }
    const result = runResults[resultModal.runId] as any;
    const report = result?.report;
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
  }, [resultModal.open, resultModal.runId, runResults]);

  const formattedRunLogs = useMemo(() => {
    return runLogs
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
  }, [runLogs]);

  const definitionPayload = useMemo(() => {
    return {
      name: definition.name.trim(),
      type: definition.type,
      description: definition.description,
      model: definition.model,
      tools: definition.tools,
      instructions: definition.instructions,
      max_steps: definition.max_steps,
      planning_interval: definition.planning_interval ? definition.planning_interval : null,
      memory_policy: definition.memory_policy,
      metadata: {
        ...definition.metadata,
        max_tool_threads: definition.max_tool_threads,
      },
    };
  }, [definition]);

  const updateDefinition = (field: keyof DefinitionState, value: any) => {
    setDefinition((prev) => ({ ...prev, [field]: value }));
  };

  const updateNumber = (field: keyof DefinitionState, value: number) => {
    setDefinition((prev) => ({ ...prev, [field]: value }));
  };

  const handlePreview = () => {
    setPreview(definitionPayload);
  };

  const handleRegister = async () => {
    setError(null);
    setStatus(null);
    try {
      await runServiceAction("agents.dynamic.register", { definition: definitionPayload });
      setStatus("Профиль зарегистрирован");
      await loadProfiles();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Ошибка регистрации профиля");
    }
  };

  const handleCreate = async () => {
    setError(null);
    setStatus(null);
    setCreateResult(null);
    try {
      await runServiceAction("agents.dynamic.register", { definition: definitionPayload });
      const resp = (await runServiceAction("agents.dynamic.create", {
        definition: definitionPayload,
        session_id: sessionId || undefined,
      })) as Record<string, unknown>;
      setCreateResult(resp);
      const agentId = typeof resp?.agent_id === "string" ? resp.agent_id : null;
      setLastCreatedAgentId(agentId);
      setStatus("Агент создан");
      await loadProfiles();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Ошибка создания агента");
    }
  };

  const handleTestRun = async () => {
    if (!lastCreatedAgentId && !definition.name) {
      setError("Сначала создайте агента");
      return;
    }
    setError(null);
    try {
      const target = lastCreatedAgentId || definition.name;
      await runServiceAction("agents.run", {
        agent_id_or_profile: target,
        task: "Представься и расскажи о своих возможностях",
        session_id: sessionId || undefined,
        enable_telemetry: true,
      });
      setStatus("Тестовая задача запущена");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось запустить тестовую задачу");
    }
  };

  const saveTemplate = () => {
    if (!definition.name.trim()) {
      setError("Укажите имя шаблона");
      return;
    }
    setError(null);
    const next = { ...templates, [definition.name.trim()]: definitionPayload };
    setTemplates(next);
    localStorage.setItem("agui-dynamic-templates", JSON.stringify(next));
    setStatus("Шаблон сохранен");
  };

  const loadTemplate = (templateName: string) => {
    const payload = templates[templateName];
    if (!payload) return;
    setDefinition({
      ...DEFAULT_DEFINITION,
      ...payload,
      tools: payload.tools ?? [],
      instructions: (payload as any).instructions ?? "",
      memory_policy: (payload as any).memory_policy ?? DEFAULT_DEFINITION.memory_policy,
    });
    setStatus(`Шаблон ${templateName} загружен`);
  };

  const deleteTemplate = (templateName: string) => {
    const next = { ...templates };
    delete next[templateName];
    setTemplates(next);
    localStorage.setItem("agui-dynamic-templates", JSON.stringify(next));
    if (selectedTemplate === templateName) setSelectedTemplate("");
  };

  const addTeamMember = (member: TeamMember) => {
    setTeam((prev) => (prev.some((m) => m.name === member.name) ? prev : [...prev, member]));
  };

  const removeTeamMember = (name: string) => {
    setTeam((prev) => prev.filter((m) => m.name !== name));
  };

  const handleRunTeam = async (allowEmpty: boolean) => {
    setError(null);
    setStatus(null);
    if (!teamForm.task.trim()) {
      setError("Введите задачу для команды");
      return;
    }
    const teamProfiles = allowEmpty ? [] : team.map((m) => m.name);
    try {
      const resp = (await runServiceAction("agents.team.run", {
        task: teamForm.task,
        manager_profile: teamForm.managerType,
        team_profiles: teamProfiles,
        session_id: teamForm.sessionId || undefined,
        enable_telemetry: teamForm.enableTelemetry,
        max_parallel: teamForm.maxParallel,
      })) as Record<string, unknown>;
      setStatus(`Запуск команды: ${String(resp.run_id ?? "—")}`);
      void loadActiveRuns();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось запустить команду");
    }
  };

  const handleRunStatus = async (runId: string) => {
    const resp = await runServiceAction("agents.status", { run_id: runId });
    setRunDetails((prev) => ({ ...prev, [runId]: (resp as any)?.status ?? resp }));
  };

  const handleRunEvents = async (runId: string) => {
    const resp = await runServiceAction("agents.events", { run_id: runId });
    setRunEvents((prev) => ({ ...prev, [runId]: (resp as any)?.events ?? resp }));
  };

  const handleRunLogs = useCallback(async (runId: string) => {
    const resp = await runServiceAction("logs.run_logs", { run_id: runId, limit: 20000 });
    const items = (resp as any)?.logs ?? resp;
    setRunLogs(Array.isArray(items) ? items : []);
  }, [runServiceAction]);

  useEffect(() => {
    if (!logsModal.open || !logsModal.runId) return;
    if (!logsModalAutoRefresh) return;
    void handleRunLogs(logsModal.runId);
    const intervalId = window.setInterval(() => {
      void handleRunLogs(logsModal.runId!);
    }, 4000);
    return () => window.clearInterval(intervalId);
  }, [logsModal.open, logsModal.runId, logsModalAutoRefresh, handleRunLogs]);

  const handleRunResult = async (runId: string) => {
    let resp = (await runServiceAction("agents.result", { run_id: runId })) as any;
    if (!resp || typeof resp !== "object") {
      resp = { result: resp };
    }
    const runStatus = activeRuns.find((run) => run.run_id === runId)?.status ?? "";
    const statusValue = String(runStatus || resp?.status || resp?.state || "").toLowerCase();
    const isTerminal = statusValue === "completed" || statusValue === "failed" || statusValue === "cancelled";
    if (resp?.result === null || resp?.result === undefined) {
      if (!isTerminal) {
        setRunResults((prev) => ({ ...prev, [runId]: resp }));
        return;
      }
      try {
        const traceResp = await runServiceAction("telemetry.trace_file", { run_id: runId });
        const trace = (traceResp as any)?.trace ?? traceResp;
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
        resp.result = value;
      } catch {
        // ignore
      }
    }
    setRunResults((prev) => ({ ...prev, [runId]: resp }));
  };

  const buildDisplayResult = (result: unknown) => {
    if (!result || typeof result !== "object") return result;
    const clone = { ...(result as any) };
    if (clone.report && typeof clone.report === "object") {
      const reportClone = { ...(clone.report as any) };
      const keys = ["base64_gzip", "content_b64_gzip", "report_b64_gzip", "base64"];
      keys.forEach((key) => {
        if (reportClone[key]) {
          reportClone[key] = "[base64 omitted]";
        }
      });
      clone.report = reportClone;
    }
    return clone;
  };

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
    if (typeof value === "object" && value !== null) {
      const typed = value as Record<string, any>;
      if (typed.final_output !== undefined && typed.final_output !== null && typed.final_output !== "") {
        return renderResultValue(typed.final_output);
      }
      if (typed.result !== undefined && typed.result !== null) {
        const nested = typed.result as any;
        if (nested && typeof nested === "object" && nested.final_output !== undefined && nested.final_output !== null && nested.final_output !== "") {
          return renderResultValue(nested.final_output);
        }
        return renderResultValue(typed.result);
      }
    }
    return <KeyValueList data={value} />;
  };

  const hasMeaningfulOutput = (value: unknown): boolean => {
    if (value === null || value === undefined) return false;
    if (typeof value === "string") return value.trim().length > 0;
    if (typeof value === "number" || typeof value === "boolean") return true;
    if (typeof value === "object") {
      const typed = value as Record<string, any>;
      if (typed.final_output !== undefined && typed.final_output !== null && typed.final_output !== "") return true;
      if (typed.result !== undefined && typed.result !== null) {
        return hasMeaningfulOutput(typed.result);
      }
      return false;
    }
    return false;
  };

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

  const handleCancelRun = async (runId: string) => {
    await runServiceAction("agents.cancel", { run_id: runId });
    void loadActiveRuns();
  };

  const handleDeleteDynamic = async (name: string) => {
    setError(null);
    try {
      await runServiceAction("agents.dynamic.delete", { profile_name: name });
      await loadProfiles();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось удалить профиль");
    }
  };

  const handleImportYaml = async (text: string) => {
    setError(null);
    try {
      const resp = (await runServiceAction("agents.dynamic.parse_yaml", { yaml_content: text })) as { template?: DynamicProfile };
      setYamlImport(resp?.template ?? null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось разобрать YAML");
    }
  };

  const exportJson = (filename: string, payload: unknown) => {
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  };

  const standardProfiles = agentProfiles;
  const dynamicProfiles = profiles;
  const templateEntries = Object.entries(templates);

  return (
    <div className="section" id="dynamic-agents">
      <div className="section-header">
        <div>
          <div className="section-title">Динамические агенты</div>
          <div className="section-hint">Конструктор, команды менеджера и управление</div>
        </div>
        <button className="button ghost" onClick={loadProfiles} disabled={isBusy}>
          Обновить список
        </button>
      </div>

      <div className="segment-row">
        <button className={`segment-button${activeTab === "constructor" ? " active" : ""}`} onClick={() => setActiveTab("constructor")}>
          Конструктор
        </button>
        <button className={`segment-button${activeTab === "team" ? " active" : ""}`} onClick={() => setActiveTab("team")}>
          Команда менеджера
        </button>
        <button className={`segment-button${activeTab === "runs" ? " active" : ""}`} onClick={() => setActiveTab("runs")}>
          Активные запуски
        </button>
        <button className={`segment-button${activeTab === "manage" ? " active" : ""}`} onClick={() => setActiveTab("manage")}>
          Управление
        </button>
        <button className={`segment-button${activeTab === "import" ? " active" : ""}`} onClick={() => setActiveTab("import")}>
          Импорт/экспорт
        </button>
      </div>

      {activeTab === "constructor" ? (
        <div className="stack">
          <div className="card">
            <div className="section-header">
              <div className="card-title">Параметры динамического агента</div>
              <div className="card-description">Заполните параметры и создайте агента без YAML.</div>
            </div>
            <div className="form-grid">
              <label className="field">
                <span className="label">Имя агента</span>
                <input value={definition.name} onChange={(e) => updateDefinition("name", e.target.value)} placeholder="CustomAnalyst" />
              </label>
              <label className="field">
                <span className="label">Тип агента</span>
                <select value={definition.type} onChange={(e) => updateDefinition("type", e.target.value)}>
                  <option value="code">code</option>
                  <option value="tool_calling">tool_calling</option>
                  <option value="multi_step">multi_step</option>
                </select>
              </label>
              <label className="field">
                <span className="label">Модель</span>
                <select value={definition.model} onChange={(e) => updateDefinition("model", e.target.value)} disabled={!availableModelKeys.length}>
                  {availableModelKeys.map((key) => (
                    <option key={key} value={key}>
                      {key}
                    </option>
                  ))}
                </select>
                {!availableModelKeys.length ? (
                  <div className="card-description">Нет доступных ключей моделей (ожидаем `openai.model_details` из `config.llm_providers`).</div>
                ) : null}
              </label>
              <label className="field">
                <span className="label">Описание</span>
                <input value={definition.description} onChange={(e) => updateDefinition("description", e.target.value)} placeholder="Аналитик по запросу" />
              </label>
              <label className="field">
                <span className="label">Макс. шагов</span>
                <input
                  type="number"
                  value={definition.max_steps}
                  onChange={(e) => updateNumber("max_steps", Number(e.target.value) || 0)}
                />
              </label>
              <label className="field">
                <span className="label">Интервал планирования</span>
                <input
                  type="number"
                  value={definition.planning_interval ?? ""}
                  onChange={(e) => updateNumber("planning_interval", Number(e.target.value) || 0)}
                />
              </label>
              <label className="field">
                <span className="label">Max tool threads</span>
                <input
                  type="number"
                  value={definition.max_tool_threads}
                  onChange={(e) => updateNumber("max_tool_threads", Number(e.target.value) || 1)}
                />
              </label>
              <label className="field">
                <span className="label">Session ID (опционально)</span>
                <input value={sessionId} onChange={(e) => setSessionId(e.target.value)} placeholder="run-xxx" />
              </label>
            </div>
            <label className="field">
              <span className="label">Инструкции</span>
              <textarea
                value={definition.instructions}
                onChange={(e) => updateDefinition("instructions", e.target.value)}
                placeholder="Опишите роль и правила для агента..."
              />
            </label>
            <label className="field">
              <span className="label">Инструменты</span>
              <select
                multiple
                value={definition.tools}
                onChange={(e) =>
                  updateDefinition(
                    "tools",
                    Array.from(e.target.selectedOptions).map((opt) => opt.value),
                  )
                }
                size={Math.min(8, Math.max(4, availableTools.length))}
              >
                {availableTools.map((tool) => (
                  <option key={tool} value={tool}>
                    {tool}
                  </option>
                ))}
              </select>
            </label>
            <div className="toggle-grid">
              <label className="toggle">
                <input
                  type="checkbox"
                  checked={!!definition.memory_policy?.enable_memory}
                  onChange={(e) =>
                    setDefinition((prev) => ({
                      ...prev,
                      memory_policy: { ...prev.memory_policy, enable_memory: e.target.checked },
                    }))
                  }
                />
                <span>Включить память</span>
              </label>
              <label className="toggle">
                <input
                  type="checkbox"
                  checked={!!definition.memory_policy?.provide_run_summary}
                  onChange={(e) =>
                    setDefinition((prev) => ({
                      ...prev,
                      memory_policy: { ...prev.memory_policy, provide_run_summary: e.target.checked },
                    }))
                  }
                />
                <span>Сводка по запуску</span>
              </label>
            </div>
            <div className="button-row">
              <button className="button secondary" type="button" onClick={handlePreview} disabled={isBusy}>
                Предпросмотр
              </button>
              <button className="button" type="button" onClick={handleRegister} disabled={isBusy || !definition.name.trim()}>
                Зарегистрировать профиль
              </button>
              <button className="button" type="button" onClick={handleCreate} disabled={isBusy || !definition.name.trim()}>
                Создать агента
              </button>
              <button className="button ghost" type="button" onClick={saveTemplate} disabled={isBusy || !definition.name.trim()}>
                Сохранить шаблон
              </button>
              <button className="button ghost" type="button" onClick={handleTestRun} disabled={isBusy}>
                Тестовый запуск
              </button>
            </div>
            {status ? <div className="card-description">{status}</div> : null}
            {error ? <div className="card-description">Ошибка: {error}</div> : null}
          </div>

          {preview ? (
            <div className="card">
              <div className="card-title">Предпросмотр</div>
              <KeyValueList data={preview} />
            </div>
          ) : null}

          {createResult ? (
            <div className="card" style={{ background: "var(--panel-strong)" }}>
              <div className="card-title">Созданный агент</div>
              <div className="profile-meta">
                <div>
                  <span className="label">Agent ID</span>
                  <div className="meta-value">{String(createResult.agent_id ?? "—")}</div>
                </div>
                <div>
                  <span className="label">Session</span>
                  <div className="meta-value">{sessionId || "—"}</div>
                </div>
              </div>
            </div>
          ) : null}
        </div>
      ) : null}

      {activeTab === "team" ? (
        <div className="stack">
          <div className="card">
            <div className="section-header">
              <div className="card-title">Доступные агенты</div>
              <div className="card-description">Добавьте агентов в команду менеджера.</div>
            </div>
            <div className="cards profile-grid">
              {standardProfiles.map((profile) => (
                <article key={profile.name ?? "standard"} className="card profile-card">
                  <div className="card-title">{profile.name}</div>
                  <div className="card-description">{profile.description || "Описание отсутствует."}</div>
                  <div className="button-row">
                    <button
                      className="button ghost"
                      type="button"
                      onClick={() =>
                        profile.name &&
                        addTeamMember({
                          name: profile.name,
                          type: "standard",
                          description: profile.description || "",
                          role: profile.type || "agent",
                        })
                      }
                    >
                      Добавить
                    </button>
                  </div>
                </article>
              ))}
              {dynamicProfiles.map((profile) => (
                <article key={profile.name ?? "dynamic"} className="card profile-card">
                  <div className="card-title">{profile.name}</div>
                  <div className="card-description">{profile.description || "Описание отсутствует."}</div>
                  <div className="button-row">
                    <button
                      className="button ghost"
                      type="button"
                      onClick={() =>
                        profile.name &&
                        addTeamMember({
                          name: profile.name,
                          type: "dynamic",
                          description: profile.description || "",
                          role: profile.type || "dynamic",
                        })
                      }
                    >
                      Добавить
                    </button>
                  </div>
                </article>
              ))}
              {templateEntries.map(([name, template]) => (
                <article key={name} className="card profile-card">
                  <div className="card-title">{name}</div>
                  <div className="card-description">{template.description || "Шаблон"}</div>
                  <div className="button-row">
                    <button
                      className="button ghost"
                      type="button"
                      onClick={() =>
                        addTeamMember({
                          name,
                          type: "template",
                          description: template.description || "",
                          role: template.type || "template",
                        })
                      }
                    >
                      Добавить
                    </button>
                  </div>
                </article>
              ))}
            </div>
          </div>

          <div className="card">
            <div className="section-header">
              <div className="card-title">Состав команды</div>
              <div className="card-description">Выбранные агенты для менеджера.</div>
            </div>
            {team.length ? (
              <div className="cards profile-grid">
                {team.map((member) => (
                  <article key={member.name} className="card profile-card">
                    <div className="card-title">{member.name}</div>
                    <div className="card-description">{member.description}</div>
                    <div className="profile-meta">
                      <div>
                        <span className="label">Тип</span>
                        <div className="meta-value">{member.type}</div>
                      </div>
                      <div>
                        <span className="label">Роль</span>
                        <div className="meta-value">{member.role}</div>
                      </div>
                    </div>
                    <button className="button ghost" type="button" onClick={() => removeTeamMember(member.name)}>
                      Удалить
                    </button>
                  </article>
                ))}
              </div>
            ) : (
              <div className="card-description">Команда пуста.</div>
            )}
            <div className="form-grid">
              <label className="field">
                <span className="label">Тип менеджера</span>
                <select value={teamForm.managerType} onChange={(e) => setTeamForm((prev) => ({ ...prev, managerType: e.target.value }))}>
                  {managerOptions.map((opt) => (
                    <option key={opt} value={opt}>
                      {opt}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span className="label">Session ID</span>
                <input value={teamForm.sessionId} onChange={(e) => setTeamForm((prev) => ({ ...prev, sessionId: e.target.value }))} placeholder="run-xxxx" />
              </label>
              <label className="field">
                <span className="label">Макс. параллельных задач</span>
                <input
                  type="number"
                  value={teamForm.maxParallel}
                  onChange={(e) => setTeamForm((prev) => ({ ...prev, maxParallel: Number(e.target.value) || 1 }))}
                />
              </label>
            </div>
            <label className="field">
              <span className="label">Задача для команды</span>
              <textarea
                value={teamForm.task}
                onChange={(e) => setTeamForm((prev) => ({ ...prev, task: e.target.value }))}
                placeholder="Опишите задачу для команды..."
              />
            </label>
            <label className="toggle">
              <input
                type="checkbox"
                checked={teamForm.enableTelemetry}
                onChange={(e) => setTeamForm((prev) => ({ ...prev, enableTelemetry: e.target.checked }))}
              />
              <span>Телеметрия</span>
            </label>
            <div className="button-row">
              <button className="button" type="button" onClick={() => handleRunTeam(false)} disabled={isBusy}>
                Запустить команду
              </button>
              <button className="button secondary" type="button" onClick={() => handleRunTeam(true)} disabled={isBusy}>
                Запустить без команды
              </button>
            </div>
            {status ? <div className="card-description">{status}</div> : null}
            {error ? <div className="card-description">Ошибка: {error}</div> : null}
          </div>
        </div>
      ) : null}

      {activeTab === "runs" ? (
        <div className="stack">
          <div className="toolbar-row">
            <div className="inline">
              <button className="button secondary" type="button" onClick={loadActiveRuns} disabled={isBusy}>
                Обновить
              </button>
              <label className="toggle">
                <input type="checkbox" checked={autoRefreshRuns} onChange={(e) => setAutoRefreshRuns(e.target.checked)} />
                <span>Автообновление</span>
              </label>
            </div>
            <span className="status-chip">Активных запусков: {activeRuns.length}</span>
          </div>
          <div className="cards profile-grid">
            {activeRuns.length === 0 ? <div className="card-description">Нет активных запусков.</div> : null}
            {activeRuns.map((run, idx) => {
              const runId = run.run_id ?? `run-${idx}`;
              const details = runDetails[runId];
              const events = runEvents[runId];
              const result = runResults[runId];
              const report = (result as any)?.report;
              const hasReport =
                report?.base64_gzip ?? report?.content_b64_gzip ?? report?.report_b64_gzip ?? report?.base64;
              return (
                <article key={runId} className="card profile-card">
                  <div className="inline">
                    <div className="card-title">{run.profile_name ?? run.manager_profile ?? "Запуск"}</div>
                    <span className="status-tag" data-status={run.status ?? "unknown"}>
                      {run.status ?? "unknown"}
                    </span>
                  </div>
                  {run.task ? <div className="card-description">{run.task}</div> : null}
                  <div className="profile-meta">
                    <div>
                      <span className="label">Run ID</span>
                      <div className="meta-value">{runId}</div>
                    </div>
                    <div>
                      <span className="label">Session</span>
                      <div className="meta-value">{run.session_id ?? "—"}</div>
                    </div>
                    <div>
                      <span className="label">Команда</span>
                      <div className="meta-value">{run.team_profiles?.join(", ") || "—"}</div>
                    </div>
                  </div>
                  <div className="button-row">
                    <button className="button secondary" type="button" onClick={() => handleRunStatus(runId)} disabled={isBusy}>
                      Статус
                    </button>
                    <button
                      className="button secondary"
                      type="button"
                      onClick={async () => {
                        setResultModal({ open: true, runId });
                        await handleRunStatus(runId);
                        const statusValue = String(run.status ?? "").toLowerCase();
                        if (["completed", "failed", "cancelled"].includes(statusValue)) {
                          await handleRunResult(runId);
                        }
                      }}
                      disabled={isBusy}
                    >
                      Результат
                    </button>
                    <button className="button ghost" type="button" onClick={() => handleRunEvents(runId)} disabled={isBusy}>
                      События
                    </button>
                    <button
                      className="button ghost"
                      type="button"
                      onClick={async () => {
                        setLogsModal({ open: true, runId });
                        await handleRunLogs(runId);
                      }}
                      disabled={isBusy}
                    >
                      Логи
                    </button>
                    {run.status === "running" ? (
                      <button className="button" type="button" onClick={() => handleCancelRun(runId)} disabled={isBusy}>
                        Остановить
                      </button>
                    ) : null}
                  </div>
                  {details ? (
                    <details className="details">
                      <summary>Детали</summary>
                      <KeyValueList data={details} />
                    </details>
                  ) : null}
                  {result ? (
                    <details className="details">
                      <summary>Результат</summary>
                      <KeyValueList data={buildDisplayResult(result)} />
                      {hasReport ? (
                        <div className="button-row">
                          <span className="status-chip">Отчёт готов</span>
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
                            Открыть отчёт
                          </button>
                        </div>
                      ) : null}
                    </details>
                  ) : null}
                  {events ? (
                    <details className="details">
                      <summary>События</summary>
                      <KeyValueList data={events} />
                    </details>
                  ) : null}
                </article>
              );
            })}
          </div>
          {reportError ? <div className="card-description">Ошибка отчёта: {reportError}</div> : null}
        </div>
      ) : null}

      {activeTab === "manage" ? (
        <div className="stack">
          <div className="cards profile-grid">
            {profiles.length === 0 ? <div className="card-description">Нет зарегистрированных динамических профилей.</div> : null}
            {profiles.map((profile) => (
              <article key={profile.name ?? "dynamic"} className="card profile-card">
                <div className="inline">
                  <div className="card-title">{profile.name}</div>
                  <span className="status-tag">{profile.type ?? "dynamic"}</span>
                </div>
                <div className="card-description">{profile.description || "Описание отсутствует."}</div>
                <div className="profile-meta">
                  <div>
                    <span className="label">Модель</span>
                    <div className="meta-value">{profile.model || "—"}</div>
                  </div>
                  <div>
                    <span className="label">Шагов</span>
                    <div className="meta-value">{profile.max_steps ?? "—"}</div>
                  </div>
                </div>
                {profile.tools?.length ? (
                  <details className="details">
                    <summary>Инструменты</summary>
                    <div className="badge-row">
                      {profile.tools.map((tool) => (
                        <span key={tool} className="badge">
                          {tool}
                        </span>
                      ))}
                    </div>
                  </details>
                ) : null}
                <div className="button-row">
                  <button className="button ghost" type="button" onClick={() => profile.name && handleDeleteDynamic(profile.name)} disabled={isBusy}>
                    Удалить профиль
                  </button>
                </div>
              </article>
            ))}
          </div>

          {templateEntries.length ? (
            <div className="card">
              <div className="card-title">Сохраненные шаблоны</div>
              <div className="cards profile-grid">
                {templateEntries.map(([name, template]) => (
                  <article key={name} className="card profile-card">
                    <div className="card-title">{name}</div>
                    <div className="card-description">{template.description || "Описание отсутствует."}</div>
                    <div className="button-row">
                      <button className="button" type="button" onClick={() => loadTemplate(name)}>
                        Открыть в конструкторе
                      </button>
                      <button className="button ghost" type="button" onClick={() => deleteTemplate(name)}>
                        Удалить
                      </button>
                    </div>
                  </article>
                ))}
              </div>
            </div>
          ) : null}
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
                    {(() => {
                      const result = runResults[resultModal.runId] as any;
                      const report = result?.report;
                      if (!report) return null;
                      return (
                        <button
                          className="button secondary"
                          type="button"
                          onClick={async () => {
                            setReportError(null);
                            try {
                              await openReport(resultModal.runId!, report);
                            } catch (err) {
                              setReportError(err instanceof Error ? err.message : "Не удалось открыть отчёт");
                            }
                          }}
                          disabled={isBusy}
                        >
                          Сохранить отчёт
                        </button>
                      );
                    })()}
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

                {hasMeaningfulOutput((runResults[resultModal.runId] as any)?.result) || !reportPreviewUrl ? (
                  <div className="run-result">
                    <div className="label">Результат</div>
                    {renderResultValue((runResults[resultModal.runId] as any)?.result)}
                  </div>
                ) : null}
              </div>
            </div>,
            document.body,
          )
        : null}

      {activeTab === "import" ? (
        <div className="stack">
          <div className="card">
            <div className="section-header">
              <div className="card-title">Экспорт</div>
              <div className="card-description">Скачайте шаблоны или состав команды.</div>
            </div>
            <div className="button-row">
              <button className="button" type="button" onClick={() => exportJson("dynamic_templates.json", templates)} disabled={isBusy}>
                Экспорт шаблонов
              </button>
              <button className="button secondary" type="button" onClick={() => exportJson("team_composition.json", { team })} disabled={isBusy}>
                Экспорт команды
              </button>
            </div>
          </div>

          <div className="card">
            <div className="section-header">
              <div className="card-title">Импорт JSON</div>
              <div className="card-description">Загрузите экспортированные шаблоны или команду.</div>
            </div>
            <input
              type="file"
              accept="application/json"
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (!file) return;
                const reader = new FileReader();
                reader.onload = () => {
                  try {
                    const data = JSON.parse(String(reader.result || "{}"));
                    if (data.templates) {
                      const next = { ...templates, ...data.templates };
                      setTemplates(next);
                      localStorage.setItem("agui-dynamic-templates", JSON.stringify(next));
                    } else if (data.team) {
                      setTeam(Array.isArray(data.team) ? data.team : []);
                    } else {
                      setTemplates((prev) => ({ ...prev, ...data }));
                      localStorage.setItem("agui-dynamic-templates", JSON.stringify({ ...templates, ...data }));
                    }
                  } catch (err) {
                    setError(err instanceof Error ? err.message : "Не удалось импортировать JSON");
                  }
                };
                reader.readAsText(file);
              }}
            />
          </div>

          <div className="card">
            <div className="section-header">
              <div className="card-title">Импорт YAML профиля</div>
              <div className="card-description">Загрузите YAML профиль агента для сохранения как шаблон.</div>
            </div>
            <input
              type="file"
              accept=".yaml,.yml"
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (!file) return;
                const reader = new FileReader();
                reader.onload = () => {
                  const content = String(reader.result || "");
                  void handleImportYaml(content);
                };
                reader.readAsText(file);
              }}
            />
            {yamlImport ? (
              <div className="stack" style={{ marginTop: 16 }}>
                <KeyValueList data={yamlImport} />
                <button
                  className="button"
                  type="button"
                  onClick={() => {
                    if (!yamlImport.name) return;
                    const next = { ...templates, [yamlImport.name]: yamlImport };
                    setTemplates(next);
                    localStorage.setItem("agui-dynamic-templates", JSON.stringify(next));
                    setStatus("YAML импортирован");
                  }}
                >
                  Сохранить как шаблон
                </button>
              </div>
            ) : null}
          </div>
          {status ? <div className="card-description">{status}</div> : null}
          {error ? <div className="card-description">Ошибка: {error}</div> : null}
        </div>
      ) : null}
    </div>
  );
}
