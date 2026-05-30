"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";

type Props = {
  runServiceAction: (
    action: string,
    payload: Record<string, unknown>,
    options?: { trackPending?: boolean; timeoutMs?: number }
  ) => Promise<unknown>;
  isBusy: boolean;
  onNavigate?: (section: string) => void;
  serviceReady: boolean;
};

type Metric = { label: string; value: string | number; hint?: string };
type DashboardActionResult = PromiseSettledResult<unknown>;

type ActiveRun = {
  run_id?: string;
  status?: string;
  task?: string;
  profile_name?: string;
  workflow_name?: string;
  current_step?: string;
  duration_seconds?: number;
  start_time?: string;
};

type TraceInfo = {
  run_id?: string;
  modified_time?: string;
  status?: string;
  agent?: string;
  pipeline?: string;
};

const DEFAULT_METRICS: Metric[] = [
  { label: "Профили агентов", value: "—" },
  { label: "Доступные пайплайны", value: "—" },
  { label: "Активные агенты", value: "—" },
  { label: "Активные workflow", value: "—" },
  { label: "DB плагины", value: "—" },
];

export function DashboardSection({ runServiceAction, isBusy, onNavigate, serviceReady }: Props) {
  const [metrics, setMetrics] = useState<Metric[]>(DEFAULT_METRICS);
  const [errors, setErrors] = useState<string | null>(null);
  const [autoTick, setAutoTick] = useState(0);
  const [activeAgentRuns, setActiveAgentRuns] = useState<ActiveRun[]>([]);
  const [activeWorkflowRuns, setActiveWorkflowRuns] = useState<ActiveRun[]>([]);
  const [systemStatus, setSystemStatus] = useState<Record<string, unknown> | null>(null);
  const [configInfo, setConfigInfo] = useState<Record<string, unknown> | null>(null);
  const [traces, setTraces] = useState<TraceInfo[]>([]);
  const [systemTab, setSystemTab] = useState<"config" | "memory" | "telemetry">("config");
  const [llmTest, setLlmTest] = useState<Record<string, unknown> | null>(null);
  const [actionStatus, setActionStatus] = useState<string | null>(null);
  const mountedRef = useRef(true);
  const loadMetricsInFlightRef = useRef(false);
  const metricsRef = useRef(metrics);

  useEffect(() => {
    metricsRef.current = metrics;
  }, [metrics]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    try {
      const cached = localStorage.getItem("agui-dashboard-metrics");
      if (cached) {
        const parsed = JSON.parse(cached) as Metric[];
        setMetrics(parsed);
      }
    } catch {
      /* ignore */
    }
  }, []);

  const loadMetrics = useCallback(async () => {
    if (!mountedRef.current) return;
    if (loadMetricsInFlightRef.current) return;
    loadMetricsInFlightRef.current = true;
    setErrors(null);
    try {
      const actionLabels = [
        "agents.list",
        "workflows.list",
        "system.active_runs",
        "db.list",
        "system.init_status",
        "system.checks",
        "config.get",
        "telemetry.list_traces",
      ];
      const actionResults: DashboardActionResult[] = await Promise.allSettled([
        runServiceAction("agents.list", {}, { trackPending: false, timeoutMs: 8000 }),
        runServiceAction("workflows.list", {}, { trackPending: false, timeoutMs: 8000 }),
        runServiceAction("system.active_runs", {}, { trackPending: false, timeoutMs: 8000 }),
        runServiceAction("db.list", {}, { trackPending: false, timeoutMs: 8000 }),
        runServiceAction("system.init_status", {}, { trackPending: false, timeoutMs: 8000 }),
        runServiceAction("system.checks", {}, { trackPending: false, timeoutMs: 8000 }),
        runServiceAction("config.get", {}, { trackPending: false, timeoutMs: 8000 }),
        runServiceAction("telemetry.list_traces", {}, { trackPending: false, timeoutMs: 8000 }),
      ]);
      if (!mountedRef.current) return;
      const getValue = (index: number) => {
        const result = actionResults[index];
        return result?.status === "fulfilled" ? result.value : null;
      };
      const failedActions = actionResults
        .map((result, index) => (result.status === "rejected" ? actionLabels[index] : null))
        .filter((label): label is string => Boolean(label));
      if (failedActions.length > 0) {
        setErrors(`Не удалось загрузить: ${failedActions.join(", ")}`);
      }
      const agentsResp = getValue(0);
      const workflowsResp = getValue(1);
      const runsResp = getValue(2);
      const dbResp = getValue(3);
      const initResp = getValue(4);
      const configResp = getValue(6);
      const tracesResp = getValue(7);
      const previousMetrics = metricsRef.current;
      const agentsCount = Array.isArray((agentsResp as any)?.agents) ? (agentsResp as any).agents.length : (previousMetrics[0]?.value ?? "—");
      const workflowsCount = Array.isArray((workflowsResp as any)?.workflows) ? (workflowsResp as any).workflows.length : (previousMetrics[1]?.value ?? "—");
      const agentRuns = Array.isArray((runsResp as any)?.agents) ? (runsResp as any).agents.length : (previousMetrics[2]?.value ?? "—");
      const workflowRuns = Array.isArray((runsResp as any)?.workflows) ? (runsResp as any).workflows.length : (previousMetrics[3]?.value ?? "—");
      const dbPlugins = Array.isArray((dbResp as any)?.plugins) ? (dbResp as any).plugins.length : (previousMetrics[4]?.value ?? "—");
      if (runsResp !== null) {
        setActiveAgentRuns(Array.isArray((runsResp as any)?.agents) ? ((runsResp as any).agents as ActiveRun[]) : []);
        setActiveWorkflowRuns(Array.isArray((runsResp as any)?.workflows) ? ((runsResp as any).workflows as ActiveRun[]) : []);
      }
      if (initResp !== null) setSystemStatus((initResp as Record<string, unknown>) ?? null);
      if (configResp !== null) setConfigInfo((configResp as Record<string, unknown>) ?? null);
      if (tracesResp && Array.isArray((tracesResp as any).traces)) {
        const items = ((tracesResp as any).traces as any[]).slice(0, 10).map((t) => ({
          run_id: t.run_id ?? t.id ?? "",
          modified_time: t.modified_time ?? t.timestamp ?? t.time,
          status: t.status ?? t.trace_status ?? "unknown",
          agent: t.agent_name ?? t.agent,
          pipeline: t.pipeline_name ?? t.workflow_name ?? t.pipeline,
        }));
        setTraces(items);
      } else if (Array.isArray(tracesResp)) {
        const items = (tracesResp as any[]).slice(0, 10).map((t) => ({
          run_id: (t as any).run_id ?? (t as any).id ?? "",
          modified_time: (t as any).modified_time ?? (t as any).timestamp ?? (t as any).time,
          status: (t as any).status ?? (t as any).trace_status ?? "unknown",
          agent: (t as any).agent_name ?? (t as any).agent,
          pipeline: (t as any).pipeline_name ?? (t as any).workflow_name ?? (t as any).pipeline,
        }));
        setTraces(items);
      }
      const next = [
        { label: "Профили агентов", value: agentsCount },
        { label: "Доступные пайплайны", value: workflowsCount },
        { label: "Активные агенты", value: agentRuns },
        { label: "Активные workflow", value: workflowRuns },
        { label: "DB плагины", value: dbPlugins },
      ];
      setMetrics(next);
      try {
        localStorage.setItem("agui-dashboard-metrics", JSON.stringify(next));
      } catch {
        /* ignore */
      }
    } catch (err) {
      if (!mountedRef.current) return;
      setErrors(err instanceof Error ? err.message : "Не удалось загрузить метрики");
    } finally {
      loadMetricsInFlightRef.current = false;
    }
  }, [runServiceAction]);

  useEffect(() => {
    if (!serviceReady) return;
    void loadMetrics();
  }, [autoTick, serviceReady, loadMetrics]);

  useEffect(() => {
    const id = window.setInterval(() => setAutoTick((t) => t + 1), 5000);
    return () => window.clearInterval(id);
  }, []);

  const memoryStatus = systemStatus?.memory_status as Record<string, unknown> | undefined;
  const tacticalCount = typeof memoryStatus?.tactical_memories_count === "number" ? memoryStatus.tactical_memories_count : null;
  const strategicCount = typeof memoryStatus?.strategic_memories_count === "number" ? memoryStatus.strategic_memories_count : null;
  const dbSize = typeof memoryStatus?.database_size_mb === "number" ? memoryStatus.database_size_mb : null;
  const embeddingModel = typeof memoryStatus?.embedding_model_name === "string" ? (memoryStatus.embedding_model_name as string) : null;
  const llmConfig = configInfo?.llm as Record<string, unknown> | undefined;
  const security = configInfo?.security as Record<string, unknown> | undefined;
  const limits = configInfo?.resource_limits as Record<string, unknown> | undefined;
  const telemetryCount = Array.isArray(traces) ? traces.length : 0;
  const telemetryEnabled = (configInfo?.telemetry as Record<string, unknown> | undefined)?.enabled;

  const metricsGrid = useMemo(() => {
    const items: Metric[] = [
      { label: "Доступные пайплайны", value: metrics[1]?.value ?? "—" },
      { label: "Активные пайплайны", value: activeWorkflowRuns.length },
      { label: "Профили агентов", value: metrics[0]?.value ?? "—" },
      { label: "Запущенные агенты", value: activeAgentRuns.length },
      { label: "Тактическая память", value: tacticalCount ?? "—" },
      { label: "Стратегическая память", value: strategicCount ?? "—" },
      { label: "Файлы трасс", value: telemetryCount },
      { label: "Размер БД (МБ)", value: dbSize ?? "—" },
    ];
    return items;
  }, [metrics, activeWorkflowRuns.length, activeAgentRuns.length, tacticalCount, strategicCount, telemetryCount, dbSize]);

  const activities = useMemo(() => {
    const items: {
      id: string;
      type: "agent" | "workflow";
      title: string;
      status: string;
      time?: string;
    }[] = [];
    activeWorkflowRuns.forEach((run) => {
      if (!run.run_id) return;
      items.push({
        id: run.run_id,
        type: "workflow",
        title: run.workflow_name ?? "Workflow",
        status: run.status ?? "unknown",
        time: run.start_time,
      });
    });
    activeAgentRuns.forEach((run) => {
      if (!run.run_id) return;
      items.push({
        id: run.run_id,
        type: "agent",
        title: run.profile_name ?? "Агент",
        status: run.status ?? "unknown",
        time: run.start_time,
      });
    });
    return items.slice(0, 10);
  }, [activeWorkflowRuns, activeAgentRuns]);

  const handleLlmTest = async () => {
    setActionStatus(null);
    setLlmTest(null);
    try {
      const provider = (configInfo?.llm as any)?.provider;
      const model = (configInfo?.llm as any)?.model;
      const resp = await runServiceAction("config.test_llm", { provider, model });
      setLlmTest((resp as any)?.result ?? resp);
    } catch (err) {
      setActionStatus(err instanceof Error ? err.message : "Не удалось протестировать LLM");
    }
  };

  const handleRebuildMemory = async () => {
    setActionStatus(null);
    try {
      await runServiceAction("memory.rebuild", {});
      setActionStatus("Память пересобрана");
      void loadMetrics();
    } catch (err) {
      setActionStatus(err instanceof Error ? err.message : "Не удалось пересобрать память");
    }
  };

  const handleTelemetryToggle = async () => {
    setActionStatus(null);
    try {
      if (telemetryEnabled) {
        await runServiceAction("telemetry.disable", {});
      } else {
        await runServiceAction("telemetry.enable", {});
      }
      void loadMetrics();
    } catch (err) {
      setActionStatus(err instanceof Error ? err.message : "Не удалось переключить телеметрию");
    }
  };

  const handleTelemetryCleanup = async () => {
    setActionStatus(null);
    try {
      await runServiceAction("telemetry.cleanup", { max_age_days: 7 });
      setActionStatus("Старые трассы очищены");
      void loadMetrics();
    } catch (err) {
      setActionStatus(err instanceof Error ? err.message : "Не удалось очистить трассы");
    }
  };

  return (
    <div className="section" id="dashboard">
      <div className="section-header">
        <div className="section-title">Дашборд</div>
        <div className="section-hint">Быстрые метрики системы</div>
      </div>
      {errors ? <div className="card-description">Ошибка: {errors}</div> : null}
      <div className="cards metric-grid">
        {metricsGrid.map((item) => (
          <article key={item.label} className="card">
            <div className="card-title">{item.label}</div>
            <div className="hero-number">{item.value}</div>
            {item.hint ? <div className="card-description">{item.hint}</div> : null}
          </article>
        ))}
      </div>

      <div className="card">
        <div className="section-header">
          <div className="card-title">Быстрые действия</div>
        </div>
        <div className="button-row">
          <button className="button" type="button" onClick={() => onNavigate?.("workflows")}>
            Запустить пайплайн
          </button>
          <button className="button secondary" type="button" onClick={() => onNavigate?.("workflows")}>
            Просмотр пайплайнов
          </button>
          <button className="button" type="button" onClick={() => onNavigate?.("agents")}>
            Создать агента
          </button>
          <button className="button ghost" type="button" onClick={() => onNavigate?.("dynamic-agents")}>
            Динамический агент
          </button>
          <button className="button ghost" type="button" onClick={() => onNavigate?.("text-to-sql")}>
            Text-to-SQL
          </button>
          <button className="button ghost" type="button" onClick={() => onNavigate?.("db")}>
            DB плагины
          </button>
          <button className="button ghost" type="button" onClick={() => onNavigate?.("memory")}>
            Память/RAG
          </button>
          <button className="button ghost" type="button" onClick={() => onNavigate?.("config")}>
            Настройки
          </button>
        </div>
      </div>

      <div className="card">
        <div className="section-header">
          <div className="card-title">Активные запуски</div>
        </div>
        <div className="profile-meta">
          <div>
            <span className="label">Агентов</span>
            <div className="meta-value">{activeAgentRuns.length}</div>
          </div>
          <div>
            <span className="label">Workflow</span>
            <div className="meta-value">{activeWorkflowRuns.length}</div>
          </div>
        </div>
        <div className="cards profile-grid">
          {activeAgentRuns.slice(0, 4).map((run, idx) => (
            <article key={run.run_id ?? `agent-${idx}`} className="card">
              <div className="inline">
                <div className="card-title">{run.profile_name ?? "Агент"}</div>
                <span className="status-tag" data-status={run.status ?? "unknown"}>
                  {run.status ?? "unknown"}
                </span>
              </div>
              {run.task ? <div className="card-description">{String(run.task).slice(0, 160)}</div> : null}
              <div className="profile-meta">
                <div>
                  <span className="label">Run ID</span>
                  <div className="meta-value">{run.run_id ?? "—"}</div>
                </div>
                <div>
                  <span className="label">Шаг</span>
                  <div className="meta-value">{run.current_step ?? "—"}</div>
                </div>
              </div>
            </article>
          ))}
          {activeWorkflowRuns.slice(0, 4).map((run, idx) => (
            <article key={run.run_id ?? `wf-${idx}`} className="card">
              <div className="inline">
                <div className="card-title">{run.workflow_name ?? "Workflow"}</div>
                <span className="status-tag" data-status={run.status ?? "unknown"}>
                  {run.status ?? "unknown"}
                </span>
              </div>
              {run.task ? <div className="card-description">{String(run.task).slice(0, 160)}</div> : null}
              <div className="profile-meta">
                <div>
                  <span className="label">Run ID</span>
                  <div className="meta-value">{run.run_id ?? "—"}</div>
                </div>
                <div>
                  <span className="label">Шаг</span>
                  <div className="meta-value">{run.current_step ?? "—"}</div>
                </div>
              </div>
            </article>
          ))}
          {activeAgentRuns.length === 0 && activeWorkflowRuns.length === 0 ? (
            <div className="card-description">Нет активных запусков.</div>
          ) : null}
        </div>
      </div>

      <div className="card">
        <div className="section-header">
          <div className="card-title">Обзор системы</div>
        </div>
        <div className="segment-row">
          <button className={`segment-button${systemTab === "config" ? " active" : ""}`} onClick={() => setSystemTab("config")}>
            Конфигурация
          </button>
          <button className={`segment-button${systemTab === "memory" ? " active" : ""}`} onClick={() => setSystemTab("memory")}>
            Память
          </button>
          <button className={`segment-button${systemTab === "telemetry" ? " active" : ""}`} onClick={() => setSystemTab("telemetry")}>
            Телеметрия
          </button>
        </div>
        {systemTab === "config" ? (
          <div className="stack">
            <div className="profile-meta">
              <div>
                <span className="label">LLM провайдер</span>
                <div className="meta-value">{(llmConfig?.provider as string) || "—"}</div>
              </div>
              <div>
                <span className="label">Модель</span>
                <div className="meta-value">{(llmConfig?.model as string) || "—"}</div>
              </div>
              <div>
                <span className="label">SQL</span>
                <div className="meta-value">{security?.sql_execution_enabled ? "вкл" : "выкл"}</div>
              </div>
              <div>
                <span className="label">Уровень</span>
                <div className="meta-value">{(security?.safety_level as string) || "—"}</div>
              </div>
            </div>
            <div className="button-row">
              <button className="button secondary" type="button" onClick={handleLlmTest} disabled={isBusy}>
                Тест соединения LLM
              </button>
            </div>
            {llmTest ? (
              <details className="details">
                <summary>Результат теста</summary>
                <div className="profile-meta">
                  <div>
                    <span className="label">Успех</span>
                    <div className="meta-value">{(llmTest as any)?.success ? "OK" : "Ошибка"}</div>
                  </div>
                  <div>
                    <span className="label">Время</span>
                    <div className="meta-value">{(llmTest as any)?.response_time_ms ?? "—"} ms</div>
                  </div>
                </div>
              </details>
            ) : null}
            {limits ? (
              <details className="details">
                <summary>Лимиты ресурсов</summary>
                <div className="graph-inputs">
                  <div className="graph-input">
                    <div className="label">Workflows</div>
                    <div className="meta-value">{(limits.max_concurrent_workflows as number) ?? "—"}</div>
                  </div>
                  <div className="graph-input">
                    <div className="label">Agents</div>
                    <div className="meta-value">{(limits.max_concurrent_agents as number) ?? "—"}</div>
                  </div>
                  <div className="graph-input">
                    <div className="label">Memory</div>
                    <div className="meta-value">{(limits.memory_limit_mb as number) ?? "—"} MB</div>
                  </div>
                </div>
              </details>
            ) : null}
          </div>
        ) : null}
        {systemTab === "memory" ? (
          <div className="stack">
            <div className="profile-meta">
              <div>
                <span className="label">SQLite</span>
                <div className="meta-value">{memoryStatus?.sqlite_available ? "OK" : "—"}</div>
              </div>
              <div>
                <span className="label">ChromaDB</span>
                <div className="meta-value">{memoryStatus?.chromadb_available ? "OK" : "—"}</div>
              </div>
              <div>
                <span className="label">Embedding</span>
                <div className="meta-value">{embeddingModel || "—"}</div>
              </div>
              <div>
                <span className="label">Размер БД</span>
                <div className="meta-value">{dbSize ?? "—"} MB</div>
              </div>
            </div>
            <div className="button-row">
              <button className="button secondary" type="button" onClick={handleRebuildMemory} disabled={isBusy}>
                Перестроить память
              </button>
            </div>
            {memoryStatus?.collections_info ? (
              <details className="details">
                <summary>Коллекции</summary>
                <div className="graph-inputs">
                  {Object.entries(memoryStatus.collections_info as Record<string, any>).map(([name, info]) => (
                    <div key={name} className="graph-input">
                      <div className="label">{name}</div>
                      <div className="meta-value">{info?.count ?? 0}</div>
                    </div>
                  ))}
                </div>
              </details>
            ) : null}
          </div>
        ) : null}
        {systemTab === "telemetry" ? (
          <div className="stack">
            <div className="profile-meta">
              <div>
                <span className="label">Статус</span>
                <div className="meta-value">{telemetryEnabled ? "Включена" : "Отключена"}</div>
              </div>
              <div>
                <span className="label">Файлов трасс</span>
                <div className="meta-value">{telemetryCount}</div>
              </div>
            </div>
            <div className="button-row">
              <button className="button secondary" type="button" onClick={handleTelemetryToggle} disabled={isBusy}>
                {telemetryEnabled ? "Отключить" : "Включить"} телеметрию
              </button>
              <button className="button ghost" type="button" onClick={handleTelemetryCleanup} disabled={isBusy}>
                Очистить трассы (7д)
              </button>
            </div>
            {traces.length ? (
              <details className="details">
                <summary>Последние трассы</summary>
                <div className="cards profile-grid">
                  {traces.slice(0, 5).map((trace) => (
                    <article key={trace.run_id ?? trace.modified_time} className="card">
                      <div className="card-title">{trace.run_id ?? "trace"}</div>
                      <div className="card-description">Событий: {trace.status ?? "—"}</div>
                    </article>
                  ))}
                </div>
              </details>
            ) : null}
          </div>
        ) : null}
        {actionStatus ? <div className="card-description">{actionStatus}</div> : null}
      </div>

      <div className="card">
        <div className="section-header">
          <div className="card-title">Последние активности</div>
          <div className="card-description">Активные запуски и последние трассы</div>
        </div>
        {activities.length ? (
          <div className="cards profile-grid">
            {activities.map((item) => (
              <article key={item.id} className="card">
                <div className="inline">
                  <div className="card-title">{item.title}</div>
                  <span className="status-tag" data-status={item.status}>
                    {item.status}
                  </span>
                </div>
                <div className="card-description">Run ID: {item.id}</div>
                <div className="profile-meta">
                  <div>
                    <span className="label">Тип</span>
                    <div className="meta-value">{item.type}</div>
                  </div>
                  <div>
                    <span className="label">Время</span>
                    <div className="meta-value">{item.time ?? "—"}</div>
                  </div>
                </div>
                <div className="button-row">
                  {item.status === "running" ? (
                    <button
                      className="button"
                      type="button"
                      onClick={() =>
                        runServiceAction(item.type === "workflow" ? "workflows.cancel" : "agents.cancel", { run_id: item.id })
                      }
                      disabled={isBusy}
                    >
                      Остановить
                    </button>
                  ) : null}
                </div>
              </article>
            ))}
          </div>
        ) : traces.length ? (
          <div className="cards profile-grid">
            {traces.map((trace, idx) => (
              <article key={trace.run_id ?? `trace-${idx}`} className="card">
                <div className="inline">
                  <div className="card-title">{trace.pipeline || trace.agent || trace.run_id || "Запуск"}</div>
                  <span className="status-tag" data-status={trace.status ?? "unknown"}>
                    {trace.status ?? "unknown"}
                  </span>
                </div>
                <div className="card-description">Run ID: {trace.run_id}</div>
                <div className="profile-meta">
                  <div>
                    <span className="label">Агент</span>
                    <div className="meta-value">{trace.agent || "—"}</div>
                  </div>
                  <div>
                    <span className="label">Пайплайн</span>
                    <div className="meta-value">{trace.pipeline || "—"}</div>
                  </div>
                  <div>
                    <span className="label">Время</span>
                    <div className="meta-value">{trace.modified_time || "—"}</div>
                  </div>
                </div>
              </article>
            ))}
          </div>
        ) : (
          <div className="card-description">Активности не найдены.</div>
        )}
      </div>
    </div>
  );
}
