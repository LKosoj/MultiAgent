"use client";

import { CopilotKitProvider, defineToolCallRenderer, useAgent, useCopilotKit } from "@copilotkitnext/react";
import { HttpAgent } from "@ag-ui/client";
import { EMPTY } from "rxjs";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AgentsSection } from "./components/sections/AgentsSection";
import { WorkflowsSection } from "./components/sections/WorkflowsSection";
import { DbSection } from "./components/sections/DbSection";
import { MemorySection } from "./components/sections/MemorySection";
import { DashboardSection } from "./components/sections/DashboardSection";
import { DynamicAgentsSection } from "./components/sections/DynamicAgentsSection";
import { TextToSqlSection } from "./components/sections/TextToSqlSection";
import {
  ConfigSection,
  TelemetrySection,
  ToolsSection,
  SystemSection,
} from "./components/sections/ActionCardSections";
import { KeyValueList } from "./components/shared/KeyValueList";

const DEFAULT_BACKEND_URL = "http://localhost:8000/agent";

type ServiceResult = {
  id: string;
  action: string;
  status: "ok" | "error" | "event";
  data: unknown;
  timestamp: string;
};

type AgentProfile = {
  name: string;
  type?: string;
  description?: string;
  model?: string;
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
  start_time?: string;
  end_time?: string;
  session_id?: string;
  step_count?: number;
  current_step?: string;
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

type DbPlugin = {
  name?: string;
  scheme?: string;
  description?: string;
  dialect_label?: string;
  supported_features?: string[];
  dsn_examples?: string[];
};

type DbTestConfig = {
  name: string;
  dsn: string;
  description?: string;
  created_at?: string;
};

type PendingAction = {
  action: string;
  resolve: (value: unknown) => void;
  reject: (error: Error) => void;
  silent?: boolean;
  timeoutId?: number;
};

class HttpConnectAgent extends HttpAgent {
  connect() {
    return EMPTY;
  }
}

const wildcardRenderer = defineToolCallRenderer({
  name: "*",
  render: ({ name, args, status }) => (
    <div className="card">
      <div className="card-title">Неизвестный инструмент: {name}</div>
      <div className="card-description">Статус: {status}</div>
      {args ? <KeyValueList data={args} /> : null}
    </div>
  ),
});

const nowStamp = () => new Date().toLocaleTimeString("ru-RU");

const stringifyValue = (value: unknown) => {
  if (value === null || value === undefined) return "";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  try {
    return JSON.stringify(value);
  } catch {
    return "";
  }
};

const getModelLabel = (model: unknown) => {
  if (typeof model === "string") return model;
  if (model && typeof model === "object") {
    const typed = model as { model_id?: unknown; model?: unknown; name?: unknown };
    if (typeof typed.model_id === "string") return typed.model_id;
    if (typeof typed.model === "string") return typed.model;
    if (typeof typed.name === "string") return typed.name;
  }
  return "custom";
};

const getModelKey = (model: unknown) => {
  return stringifyValue(model);
};

function AguiStudio() {
  const { agent } = useAgent({ agentId: "default" });
  const { copilotkit } = useCopilotKit();

  const [results, setResults] = useState<ServiceResult[]>([]);
  const [, setPendingTick] = useState(0);
  const [activeSection, setActiveSection] = useState("dashboard");
  const [serviceReady, setServiceReady] = useState(false);

  // Agents state
  const [agentTab, setAgentTab] = useState<"profiles" | "run" | "monitor">("profiles");
  const [agentProfiles, setAgentProfiles] = useState<AgentProfile[]>([]);
  const [profilesLoaded, setProfilesLoaded] = useState(false);
  const [profilesLoading, setProfilesLoading] = useState(false);
  const [profilesError, setProfilesError] = useState<string | null>(null);
  const [profileTypeFilter, setProfileTypeFilter] = useState("Все");
  const [profileModelFilter, setProfileModelFilter] = useState("Все");
  const [profileSearch, setProfileSearch] = useState("");
  const [selectedProfile, setSelectedProfile] = useState<AgentProfile | null>(null);
  const [createdAgents, setCreatedAgents] = useState<Record<string, { profile_name: string; session_id: string; created_at: string }>>(
    {},
  );
  const [agentRunHistory, setAgentRunHistory] = useState<
    { run_id: string; profile_name: string; status?: string; task?: string; start_time?: string; end_time?: string }[]
  >([]);
  const [agentRunForm, setAgentRunForm] = useState({
    sessionId: "",
    task: "",
    useExistingAgent: false,
    selectedAgentId: "",
    enableTelemetry: true,
    enableMemory: true,
  });
  const [activeAgentRuns, setActiveAgentRuns] = useState<AgentRunInfo[]>([]);
  const [runsLoading, setRunsLoading] = useState(false);
  const [runsError, setRunsError] = useState<string | null>(null);
  const [autoRefreshRuns, setAutoRefreshRuns] = useState(true);
  const [runResults, setRunResults] = useState<Record<string, unknown>>({});
  const [currentAgentRunId, setCurrentAgentRunId] = useState<string | null>(null);
  const [currentAgentRunLogs, setCurrentAgentRunLogs] = useState<unknown[]>([]);

  // Workflows state
  const [workflowTab, setWorkflowTab] = useState<"list" | "run" | "monitor" | "builder">("list");
  const [workflows, setWorkflows] = useState<WorkflowInfo[]>([]);
  const [workflowsLoading, setWorkflowsLoading] = useState(false);
  const [workflowsError, setWorkflowsError] = useState<string | null>(null);
  const [workflowCategoryFilter, setWorkflowCategoryFilter] = useState("Все");
  const [workflowComplexityFilter, setWorkflowComplexityFilter] = useState("Все");
  const [workflowSearch, setWorkflowSearch] = useState("");
  const [selectedWorkflow, setSelectedWorkflow] = useState<WorkflowInfo | null>(null);
  const [workflowInputs, setWorkflowInputs] = useState<Record<string, unknown>>({});
  const [workflowParams, setWorkflowParams] = useState<Record<string, string>>({});
  const [workflowOptions, setWorkflowOptions] = useState({ useEnhanced: true, enableTelemetry: true });
  const [workflowRuns, setWorkflowRuns] = useState<WorkflowRunInfo[]>([]);
  const [workflowRunsLoading, setWorkflowRunsLoading] = useState(false);
  const [workflowRunsError, setWorkflowRunsError] = useState<string | null>(null);
  const [workflowAutoRefresh, setWorkflowAutoRefresh] = useState(false);
  const [currentWorkflowRunId, setCurrentWorkflowRunId] = useState<string | null>(null);
  const currentWorkflowRunIdRef = useRef<string | null>(null);
  const [currentWorkflowRunLogs, setCurrentWorkflowRunLogs] = useState<unknown[]>([]);
  const [workflowResults, setWorkflowResults] = useState<Record<string, unknown>>({});
  const requestedWorkflowResultsRef = useRef<Set<string>>(new Set());
  const [workflowRunHistory, setWorkflowRunHistory] = useState<
    { run_id: string; workflow_name?: string; status?: string; start_time?: string }[]
  >([]);
  const [workflowArtifacts, setWorkflowArtifacts] = useState<Record<string, unknown>>({});
  const [builderInfo, setBuilderInfo] = useState({
    name: "my_pipeline",
    version: "1.0",
    type: "simple",
    category: "general",
    description: "",
    estimated_duration: "5 minutes",
    complexity: "simple",
  });
  const [builderInputs, setBuilderInputs] = useState<{ key: string; value: string }[]>([{ key: "topic", value: "" }]);
  const [builderSteps, setBuilderSteps] = useState("[]");
  const [builderYaml, setBuilderYaml] = useState("");
  const [builderSaveName, setBuilderSaveName] = useState("");
  const [workflowRunError, setWorkflowRunError] = useState<string | null>(null);
  const [builderError, setBuilderError] = useState<string | null>(null);
  const [workflowYamlModal, setWorkflowYamlModal] = useState<{
    open: boolean;
    name?: string;
    yaml?: string;
    steps?: any[];
    meta?: WorkflowInfo;
    pipeline?: Record<string, unknown>;
    inputs?: Record<string, unknown>;
    metadata?: Record<string, unknown>;
  }>({
    open: false,
  });

  const workflowGraph = useMemo(() => {
    if (!workflowYamlModal.steps || !workflowYamlModal.steps.length) return { nodes: [], edges: [] };
    type Node = { id: string; level: number; row: number; data: any };
    const nodes: Node[] = [];
    const edges: Array<{ from: string; to: string }> = [];
    const levelMap: Record<string, number> = {};
    const children: Record<string, string[]> = {};

    (workflowYamlModal.steps || []).forEach((step: any) => {
      const id = step.id || step.step_id || `step_${nodes.length + 1}`;
      const deps = Array.isArray(step.depends_on) ? step.depends_on : step.depends_on ? [step.depends_on] : [];
      deps.forEach((dep: string) => {
        children[dep] = children[dep] || [];
        children[dep].push(id);
      });
      levelMap[id] = 0;
    });

    const visit = (id: string, stack: Set<string>) => {
      if (stack.has(id)) return 0;
      stack.add(id);
      const deps = (workflowYamlModal.steps || []).find((s: any) => s.id === id || s.step_id === id)?.depends_on;
      const depsArr = Array.isArray(deps) ? deps : deps ? [deps] : [];
      const level = depsArr.reduce((max: number, dep: string) => Math.max(max, visit(dep, stack)), 0);
      levelMap[id] = Math.max(levelMap[id] || 0, level + (depsArr.length ? 1 : 0));
      stack.delete(id);
      return levelMap[id];
    };

    workflowYamlModal.steps.forEach((step: any) => {
      const id = step.id || step.step_id || `step_${nodes.length + 1}`;
      visit(id, new Set());
    });

    const rowsPerLevel: Record<number, number> = {};
    (workflowYamlModal.steps || []).forEach((step: any) => {
      const id = step.id || step.step_id || `step_${nodes.length + 1}`;
      const level = levelMap[id] ?? 0;
      const row = rowsPerLevel[level] ?? 0;
      rowsPerLevel[level] = row + 1;
      nodes.push({ id, level, row, data: step });
      const deps = Array.isArray(step.depends_on) ? step.depends_on : step.depends_on ? [step.depends_on] : [];
      deps.forEach((dep: string) => edges.push({ from: dep, to: id }));
    });

    const maxLevel = Math.max(...nodes.map((n) => n.level), 0);
    const positions: Record<string, { x: number; y: number }> = {};
    const colHeight: Record<number, number> = {};
    for (let l = 0; l <= maxLevel; l += 1) colHeight[l] = 0;
    const verticalSpacing = 260;
    const horizontalSpacing = 300;
    nodes
      .sort((a, b) => a.level - b.level || a.row - b.row)
      .forEach((node) => {
        const y = colHeight[node.level] * verticalSpacing;
        positions[node.id] = { x: node.level * horizontalSpacing, y };
        colHeight[node.level] += 1;
      });

    return { nodes, edges, positions, size: { width: (maxLevel + 1) * horizontalSpacing + 180, height: Math.max(...Object.values(colHeight)) * verticalSpacing + 180 } };
  }, [workflowYamlModal.steps]);

  useEffect(() => {
    if (!workflowYamlModal.open) return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previous;
    };
  }, [workflowYamlModal.open]);

  const yamlMetadataSnippet = useMemo(() => {
    if (!workflowYamlModal.metadata && workflowYamlModal.yaml) {
      const match = workflowYamlModal.yaml.match(/metadata:\n((?:\s{2}.*\n?)*)/);
      return match ? match[0] : null;
    }
    return null;
  }, [workflowYamlModal.metadata, workflowYamlModal.yaml]);

  const yamlInputsSnippet = useMemo(() => {
    if ((!workflowYamlModal.inputs || Object.keys(workflowYamlModal.inputs).length === 0) && workflowYamlModal.yaml) {
      const match = workflowYamlModal.yaml.match(/inputs:\n((?:\s{2}.*\n?)*)/);
      return match ? match[0] : null;
    }
    return null;
  }, [workflowYamlModal.inputs, workflowYamlModal.yaml]);

  // DB plugins state
  const [dbTab, setDbTab] = useState<"plugins" | "test" | "configs" | "diagnostics">("plugins");
  const [dbPlugins, setDbPlugins] = useState<DbPlugin[]>([]);
  const [dbPluginsLoading, setDbPluginsLoading] = useState(false);
  const [dbPluginsError, setDbPluginsError] = useState<string | null>(null);
  const [dbTestForm, setDbTestForm] = useState({
    dsn: "",
    timeout: 10,
    testBasic: true,
    testSchema: true,
    testSecurity: true,
    verbose: false,
  });
  const [dbTestResult, setDbTestResult] = useState<unknown>(null);
  const [dbConfigs, setDbConfigs] = useState<DbTestConfig[]>([]);
  const [dbConfigsLoading, setDbConfigsLoading] = useState(false);
  const [dbConfigsError, setDbConfigsError] = useState<string | null>(null);
  const [dbConfigForm, setDbConfigForm] = useState({ name: "", dsn: "", description: "" });
  const [dbDiagnostics, setDbDiagnostics] = useState<Record<string, unknown> | null>(null);
  const [dbBenchmark, setDbBenchmark] = useState<Record<string, unknown> | null>(null);

  // Memory state
  const [memoryTab, setMemoryTab] = useState<"status" | "search" | "analytics" | "manage">("status");
  const [memoryStatus, setMemoryStatus] = useState<unknown>(null);
  const [memoryStatusLoading, setMemoryStatusLoading] = useState(false);
  const [memoryStatusError, setMemoryStatusError] = useState<string | null>(null);
  const [memorySearchForm, setMemorySearchForm] = useState({
    query: "",
    memoryType: "tactical",
    limit: 5,
    sessionId: "",
    agentName: "",
  });
  const [memorySearchResults, setMemorySearchResults] = useState<unknown>(null);
  const [memorySearchError, setMemorySearchError] = useState<string | null>(null);
  const [memoryAnalytics, setMemoryAnalytics] = useState<Record<string, unknown>>({});
  const [memoryAnalyticsDays, setMemoryAnalyticsDays] = useState(7);
  const [memoryManageResult, setMemoryManageResult] = useState<unknown>(null);
  const [memoryClearForm, setMemoryClearForm] = useState({ agentName: "", sessionId: "" });
  const [memoryTestResult, setMemoryTestResult] = useState<unknown>(null);

  const pendingMapRef = useRef<Map<string, PendingAction>>(new Map());
  const serviceActionQueueRef = useRef<Promise<void>>(Promise.resolve());
  const requestedRunResultsRef = useRef<Set<string>>(new Set());
  const workflowRunLogsInFlightRef = useRef<Set<string>>(new Set());

  const sections = [
    { id: "dashboard", label: "Дашборд" },
    { id: "agents", label: "Агенты" },
    { id: "dynamic-agents", label: "Динамические агенты" },
    { id: "workflows", label: "Workflow" },
    { id: "text-to-sql", label: "Text-to-SQL" },
    { id: "db", label: "DB плагины" },
    { id: "memory", label: "Память/RAG" },
    { id: "tools", label: "Инструменты" },
    { id: "config", label: "Конфигурация" },
    { id: "telemetry", label: "Телеметрия" },
    { id: "system", label: "Система" },
  ];

  const appendResult = useCallback((entry: ServiceResult) => {
    setResults((prev) => [entry, ...prev].slice(0, 80));
  }, []);

  useEffect(() => {
    if (!agent) {
      return () => undefined;
    }
    const subscription = agent.subscribe({
      onCustomEvent: ({ event }) => {
        if (!event || typeof event !== "object") {
          return;
        }
        const name = (event as { name?: string }).name;
        const value = (event as { value?: unknown }).value;
        if (name === "service.result" && value && typeof value === "object") {
          const payload = value as { action?: string; ok?: boolean; data?: unknown; __request_id?: string };
          const requestId = payload.__request_id;
          appendResult({
            id: crypto.randomUUID(),
            action: payload.action ?? "service.result",
            status: payload.ok ? "ok" : "error",
            data: payload.data ?? value,
            timestamp: nowStamp(),
          });
          if (requestId && pendingMapRef.current.has(requestId)) {
            const pending = pendingMapRef.current.get(requestId);
            if (pending) {
              if (payload.ok) {
                pending.resolve(payload.data);
              } else {
                const errorData = payload.data as { error?: unknown } | undefined;
                pending.reject(new Error(String(errorData?.error ?? `Service action failed: ${payload.action ?? "unknown"}`)));
              }
              if (pending.timeoutId) window.clearTimeout(pending.timeoutId);
            }
            pendingMapRef.current.delete(requestId);
            setPendingTick((tick) => tick + 1);
          }
          return;
        }
        if (name === "service.log" || name === "service.progress") {
          appendResult({
            id: crypto.randomUUID(),
            action: name ?? "service.event",
            status: "event",
            data: value ?? event,
            timestamp: nowStamp(),
          });
        }
        // T3.3: workflow.result envelope содержит итоговый результат workflow,
        // запущенного через forwardedProps (без service_action). Кладём в
        // workflowResults по workflow_run_id, чтобы UI не дёргал workflows.result.
        if (name === "workflow.result" && value && typeof value === "object") {
          const envelope = value as { workflow_run_id?: string };
          if (envelope.workflow_run_id) {
            setWorkflowResults((prev) => ({
              ...prev,
              [envelope.workflow_run_id as string]: envelope,
            }));
          }
        }
      },
      onRunFailed: ({ error }) => {
        if (pendingMapRef.current.size) {
          pendingMapRef.current.forEach((pending) => {
            pending.reject(error);
            if (pending.timeoutId) window.clearTimeout(pending.timeoutId);
          });
          pendingMapRef.current.clear();
          setPendingTick((tick) => tick + 1);
        }
        appendResult({
          id: crypto.randomUUID(),
          action: "run.error",
          status: "error",
          data: { message: error.message },
          timestamp: nowStamp(),
        });
      },
    });

    setServiceReady(true);
    return () => {
      setServiceReady(false);
      subscription.unsubscribe();
    };
  }, [agent, appendResult]);

  type ServiceActionOptions = {
    trackPending?: boolean;
    timeoutMs?: number;
  };

  const runServiceAction = useCallback(
    async (action: string, payload: Record<string, unknown>, options?: ServiceActionOptions) => {
      if (!agent) {
        throw new Error("Агент не инициализирован");
      }
      const actionTimeouts: Record<string, number> = {
        "memory.rebuild": 600000,
      };
      const allowNoResult = action === "logs.stream" || action === "progress.stream";
      const requestId = crypto.randomUUID();
      const enrichedPayload = { ...payload, __request_id: requestId };
      const trackPending = options?.trackPending !== false;
      const isStreamAction = action === "logs.stream" || action === "progress.stream";
      const timeoutMs = options?.timeoutMs ?? actionTimeouts[action] ?? (isStreamAction ? 30000 : 20000);
      const runQueuedAction = async () => {
        const previousProps = { ...copilotkit.properties };
        const promise = new Promise<unknown>((resolve, reject) => {
          pendingMapRef.current.set(requestId, {
            action,
            resolve,
            reject,
            silent: !trackPending,
            timeoutId:
              timeoutMs > 0
                ? window.setTimeout(() => {
                  const pending = pendingMapRef.current.get(requestId);
                  if (!pending) return;
                    if (allowNoResult) {
                      pending.resolve(null);
                    } else {
                      pending.reject(new Error(`Таймаут запроса: ${action}`));
                    }
                    pendingMapRef.current.delete(requestId);
                    setPendingTick((tick) => tick + 1);
                  }, timeoutMs)
                : undefined,
          });
          setPendingTick((tick) => tick + 1);
        });
        promise.catch(() => undefined);

        copilotkit.setProperties({
          ...previousProps,
          service_action: action,
          service_payload: enrichedPayload,
        });

        const runAgentPromise = copilotkit.runAgent({ agent });
        runAgentPromise.catch(() => undefined);

        try {
          if (allowNoResult) {
            const outcome = await Promise.race([
              promise.then((value) => ({ kind: "result" as const, value })),
              runAgentPromise.then(() => ({ kind: "agent" as const })),
            ]);
            if (outcome.kind === "result") {
              await runAgentPromise;
              return outcome.value;
            }
            const pending = pendingMapRef.current.get(requestId);
            if (pending) {
              pending.resolve(null);
              if (pending.timeoutId) window.clearTimeout(pending.timeoutId);
              pendingMapRef.current.delete(requestId);
              setPendingTick((tick) => tick + 1);
            }
            await runAgentPromise;
            return await promise;
          }
          const value = await promise;
          await runAgentPromise;
          return value;
        } catch (err) {
          // Важно: если транспорт (fetch/SSE) упал до получения ответа,
          // нужно снять pending, иначе UI "зависнет" с незавершённым запросом.
          const pending = pendingMapRef.current.get(requestId);
          if (pending) {
            pending.reject(err instanceof Error ? err : new Error(String(err)));
            if (pending.timeoutId) window.clearTimeout(pending.timeoutId);
            pendingMapRef.current.delete(requestId);
            setPendingTick((tick) => tick + 1);
          }

          const message = err instanceof Error ? err.message : String(err);
          appendResult({
            id: crypto.randomUUID(),
            action: "run.error",
            status: "error",
            data: {
              message,
              action,
              request_id: requestId,
              // Иногда это чисто сетевой сбой/blocked by CORS; URL помогает быстро диагностировать.
              agent_url: (agent as any)?.url ?? null,
            },
            timestamp: nowStamp(),
          });

          throw err;
        } finally {
          copilotkit.setProperties(previousProps);
        }
      };

      const queued = serviceActionQueueRef.current.then(runQueuedAction, runQueuedAction);
      serviceActionQueueRef.current = queued.then(
        () => undefined,
        () => undefined,
      );
      return queued;
    },
    [copilotkit, agent, appendResult],
  );

  useEffect(() => {
    currentWorkflowRunIdRef.current = currentWorkflowRunId;
  }, [currentWorkflowRunId]);

  const clearProgressResults = () => {
    setResults((prev) => prev.filter((entry) => entry.action !== "service.progress"));
  };
  const pendingNonSilent = Array.from(pendingMapRef.current.values()).filter((pending) => !pending.silent);
  const isBusy = pendingNonSilent.length > 0;

  // Agents logic
  const loadAgentProfiles = useCallback(async () => {
    setProfilesLoading(true);
    setProfilesError(null);
    try {
      const result = (await runServiceAction("agents.list", {})) as { agents?: AgentProfile[] };
      const profiles = Array.isArray(result?.agents) ? result.agents : [];
      setAgentProfiles(profiles);
      setProfilesLoaded(true);
    } catch (err) {
      setProfilesError(err instanceof Error ? err.message : "Не удалось загрузить профили");
    } finally {
      setProfilesLoading(false);
    }
  }, [runServiceAction]);

  const refreshActiveRuns = useCallback(async () => {
    setRunsLoading(true);
    setRunsError(null);
    try {
      const result = (await runServiceAction("system.active_runs", {})) as { agents?: AgentRunInfo[] };
      const runs = Array.isArray(result?.agents) ? result.agents : [];
      setActiveAgentRuns(runs);
    } catch (err) {
      setRunsError(err instanceof Error ? err.message : "Не удалось обновить запуски");
    } finally {
      setRunsLoading(false);
    }
  }, [runServiceAction]);

  useEffect(() => {
    if (activeSection !== "agents") return;
    if (!profilesLoaded) void loadAgentProfiles();
  }, [activeSection, profilesLoaded, loadAgentProfiles]);

  useEffect(() => {
    if (activeSection !== "agents" || agentTab !== "monitor") return;
    void refreshActiveRuns();
  }, [activeSection, agentTab, refreshActiveRuns]);

  useEffect(() => {
    if (!activeAgentRuns.length) return;
    setAgentRunHistory((prev) =>
      prev.map((entry) => {
        const match = activeAgentRuns.find((run) => run.run_id === entry.run_id);
        if (!match) return entry;
        const next = { ...entry };
        if (match.status && match.status !== entry.status) {
          next.status = match.status;
        }
        if ((match as any).end_time && next.end_time !== (match as any).end_time) {
          next.end_time = (match as any).end_time;
        }
        return next;
      }),
    );
  }, [activeAgentRuns]);

  useEffect(() => {
    if (!autoRefreshRuns || activeSection !== "agents" || agentTab !== "monitor") return;
    const interval = window.setInterval(() => void refreshActiveRuns(), 4000);
    return () => window.clearInterval(interval);
  }, [autoRefreshRuns, activeSection, agentTab, refreshActiveRuns]);

  const availableProfileTypes = useMemo(() => {
    const types = Array.from(new Set(agentProfiles.map((profile) => profile.type).filter((type): type is string => Boolean(type))));
    return ["Все", ...types];
  }, [agentProfiles]);

  const availableProfileModels = useMemo(() => {
    const seen = new Set<string>();
    const items = [{ key: "Все", label: "Все" }];
    agentProfiles.forEach((profile) => {
      const key = getModelKey(profile.model);
      if (!key || seen.has(key)) return;
      seen.add(key);
      items.push({ key, label: getModelLabel(profile.model) });
    });
    return items;
  }, [agentProfiles]);

  const filteredProfiles = useMemo(() => {
    return agentProfiles.filter((profile) => {
      if (profileTypeFilter !== "Все" && profile.type !== profileTypeFilter) return false;
      if (profileModelFilter !== "Все" && getModelKey(profile.model) !== profileModelFilter) return false;
      if (profileSearch.trim()) {
        const term = profileSearch.trim().toLowerCase();
        return (
          profile.name.toLowerCase().includes(term) ||
          (profile.description ?? "").toLowerCase().includes(term)
        );
      }
      return true;
    });
  }, [agentProfiles, profileModelFilter, profileSearch, profileTypeFilter]);

  const createdAgentsForProfile = useMemo(() => {
    if (!selectedProfile) return [];
    return Object.entries(createdAgents)
      .filter(([, info]) => info.profile_name === selectedProfile.name)
      .map(([agentId, info]) => ({ agentId, ...info }));
  }, [createdAgents, selectedProfile]);

  const createdAgentsList = useMemo(() => {
    return Object.entries(createdAgents).map(([agentId, info]) => ({ agentId, ...info }));
  }, [createdAgents]);

  const handleSelectProfile = useCallback((profile: AgentProfile) => {
    setSelectedProfile(profile);
    setAgentTab("run");
  }, []);

  const handleRunAgent = useCallback(async () => {
    if (!selectedProfile || !agentRunForm.task.trim()) return;
    const agentIdOrProfile =
      agentRunForm.useExistingAgent && agentRunForm.selectedAgentId
        ? agentRunForm.selectedAgentId
        : selectedProfile.name;
    const payload: Record<string, unknown> = {
      agent_id_or_profile: agentIdOrProfile,
      task: agentRunForm.task.trim(),
      enable_telemetry: agentRunForm.enableTelemetry,
      enable_memory: agentRunForm.enableMemory,
    };
    if (agentRunForm.sessionId.trim()) {
      payload.session_id = agentRunForm.sessionId.trim();
    }
    const response = (await runServiceAction("agents.run", payload)) as { run_id?: string };
    const runId = response?.run_id ?? (agentRunForm.sessionId.trim() || "unknown");
    setCurrentAgentRunId(runId);
    setCurrentAgentRunLogs([]);
    setAgentRunHistory((prev) => [
      {
        run_id: runId,
        profile_name: selectedProfile.name,
        status: "running",
        task: agentRunForm.task.trim(),
        start_time: new Date().toLocaleTimeString("ru-RU"),
      },
      ...prev,
    ]);
    setAgentTab("monitor");
    void refreshActiveRuns();
  }, [agentRunForm, refreshActiveRuns, runServiceAction, selectedProfile]);

  const handleCancelRun = useCallback(
    async (runId: string) => {
      await runServiceAction("agents.cancel", { run_id: runId });
      void refreshActiveRuns();
    },
    [refreshActiveRuns, runServiceAction],
  );

  const handleFetchRunResult = useCallback(
    async (runId: string) => {
      const result = await runServiceAction("agents.result", { run_id: runId });
      setRunResults((prev) => ({ ...prev, [runId]: result }));
    },
    [runServiceAction],
  );

  const handleFetchRunLogs = useCallback(
    async (runId: string) => {
      const resp = await runServiceAction("logs.run_logs", { run_id: runId, limit: 500 });
      const items = (resp as any)?.logs ?? resp;
      setCurrentAgentRunLogs(Array.isArray(items) ? items : []);
    },
    [runServiceAction],
  );

  useEffect(() => {
    const candidateIndex = activeAgentRuns.findIndex((run, index) => {
      const runId = run.run_id ?? `run-${index + 1}`;
      if (run.status !== "completed") return false;
      if (runResults[runId]) return false;
      if (requestedRunResultsRef.current.has(runId)) return false;
      return true;
    });
    if (candidateIndex === -1) return;
    const candidate = activeAgentRuns[candidateIndex];
    const candidateId = candidate.run_id ?? `run-${candidateIndex + 1}`;
    requestedRunResultsRef.current.add(candidateId);
    void handleFetchRunResult(candidateId);
  }, [activeAgentRuns, handleFetchRunResult, runResults]);

  const handleFetchRunStatus = useCallback(
    async (runId: string) => {
      const result = await runServiceAction("agents.status", { run_id: runId });
      setRunResults((prev) => ({ ...prev, [runId]: result }));
    },
    [runServiceAction],
  );

  const handleRemoveCreatedAgent = useCallback((agentId: string) => {
    setCreatedAgents((prev) => {
      const next = { ...prev };
      delete next[agentId];
      return next;
    });
  }, []);

  const handleClearAgentHistory = useCallback(() => {
    setAgentRunHistory([]);
  }, []);

  const handleCleanupRuns = useCallback(async () => {
    await runServiceAction("agents.cleanup", {});
    void refreshActiveRuns();
  }, [refreshActiveRuns, runServiceAction]);

  // Workflows logic
  const loadWorkflows = useCallback(async () => {
    setWorkflowsLoading(true);
    setWorkflowsError(null);
    try {
      const result = (await runServiceAction("workflows.list", {})) as { workflows?: WorkflowInfo[] };
      setWorkflows(Array.isArray(result?.workflows) ? result.workflows : []);
    } catch (err) {
      setWorkflowsError(err instanceof Error ? err.message : "Не удалось загрузить пайплайны");
    } finally {
      setWorkflowsLoading(false);
    }
  }, [runServiceAction]);

  const loadWorkflowInputs = useCallback(
    async (workflowName: string) => {
      const result = (await runServiceAction("workflows.parse_yaml", { workflow_name: workflowName })) as {
        pipeline_info?: { inputs?: Record<string, unknown> };
      };
      const inputs = result?.pipeline_info?.inputs ?? {};
      setWorkflowInputs(inputs);
      const defaults: Record<string, string> = {};
      Object.entries(inputs).forEach(([key, value]) => {
        defaults[key] = value === null || value === undefined ? "" : String(value);
      });
      setWorkflowParams(defaults);
    },
    [runServiceAction],
  );

  const handleSelectWorkflow = useCallback(
    async (workflow: WorkflowInfo) => {
      setSelectedWorkflow(workflow);
      setWorkflowTab("run");
      await loadWorkflowInputs(workflow.name);
    },
    [loadWorkflowInputs],
  );

  const handleViewWorkflowYaml = useCallback(
    async (workflow: WorkflowInfo) => {
      try {
        const parsed = (await runServiceAction("workflows.parse_yaml", { workflow_name: workflow.name })) as {
          steps?: any[];
          pipeline_info?: Record<string, unknown>;
          inputs?: Record<string, unknown>;
          metadata?: Record<string, unknown>;
        };
        const yamlResp = (await runServiceAction("workflows.get_yaml", { workflow_name: workflow.name })) as { yaml?: string };
        const pipelineInfo = parsed?.pipeline_info ?? {};
        const metadata = parsed?.metadata ?? (pipelineInfo?.metadata as Record<string, unknown> | undefined);
        setWorkflowYamlModal({
          open: true,
          name: workflow.name,
          yaml: yamlResp?.yaml ?? "",
          steps: parsed?.steps ?? [],
          meta: workflow,
          pipeline: pipelineInfo,
          inputs: parsed?.inputs,
          metadata,
        });
      } catch (err) {
        setWorkflowYamlModal({
          open: true,
          name: workflow.name,
          yaml: err instanceof Error ? err.message : "Ошибка загрузки",
          steps: [],
          meta: workflow,
          pipeline: undefined,
          inputs: undefined,
          metadata: undefined,
        });
      }
    },
    [runServiceAction],
  );

  const refreshWorkflowRuns = useCallback(async () => {
    setWorkflowRunsLoading(true);
    setWorkflowRunsError(null);
    try {
      const result = (await runServiceAction("system.active_runs", {})) as { workflows?: WorkflowRunInfo[] };
      const runs = Array.isArray(result?.workflows) ? result.workflows : [];
      setWorkflowRuns(runs);
    } catch (err) {
      setWorkflowRunsError(err instanceof Error ? err.message : "Не удалось обновить запуски workflow");
    } finally {
      setWorkflowRunsLoading(false);
    }
  }, [runServiceAction]);

  const handleRunWorkflow = useCallback(async () => {
    if (!selectedWorkflow) return;
    setWorkflowRunError(null);
    const parameters: Record<string, string> = {};
    Object.entries(workflowInputs).forEach(([key, defaultValue]) => {
      const raw = (workflowParams[key] ?? "").trim();
      if (raw) {
        parameters[key] = raw;
      } else if (defaultValue !== null && defaultValue !== undefined && String(defaultValue).trim()) {
        parameters[key] = String(defaultValue).trim();
      }
    });
    const missing = Object.entries(workflowInputs)
      .filter(([, value]) => value === null || value === undefined || String(value).trim() === "")
      .map(([key]) => key)
      .filter((key) => !parameters[key]);
    if (missing.length) {
      setWorkflowRunError(`Заполните обязательные параметры: ${missing.join(", ")}`);
      return;
    }

    try {
      const response = (await runServiceAction("workflows.start", {
        workflow_name: selectedWorkflow.name,
        parameters,
        use_enhanced: workflowOptions.useEnhanced,
        enable_telemetry: workflowOptions.enableTelemetry,
      })) as { run_id?: string };
      if (response?.run_id) {
        setCurrentWorkflowRunId(response.run_id);
        setCurrentWorkflowRunLogs([]);
      }
      if (response?.run_id) {
        setWorkflowRunHistory((prev) => [
          {
            run_id: response.run_id as string,
            workflow_name: selectedWorkflow.name,
            status: "running",
            start_time: new Date().toLocaleTimeString("ru-RU"),
          },
          ...prev,
        ]);
      }
      setWorkflowTab("monitor");
      await refreshWorkflowRuns();
    } catch (err) {
      setWorkflowRunError(err instanceof Error ? err.message : "Не удалось запустить workflow");
    }
  }, [refreshWorkflowRuns, runServiceAction, selectedWorkflow, workflowInputs, workflowOptions, workflowParams]);

  const handleCancelWorkflow = useCallback(
    async (runId: string) => {
      await runServiceAction("workflows.cancel", { run_id: runId });
      setWorkflowRunHistory((prev) =>
        prev.map((entry) => (entry.run_id === runId ? { ...entry, status: "cancelled" } : entry)),
      );
      await refreshWorkflowRuns();
    },
    [refreshWorkflowRuns, runServiceAction],
  );

  const handleFetchWorkflowStatus = useCallback(
    async (runId: string) => {
      const result = await runServiceAction("workflows.status", { run_id: runId });
      setWorkflowArtifacts((prev) => ({ ...prev, [`status:${runId}`]: result }));
      const statusValue = (result as any)?.status?.status;
      if (statusValue) {
        setWorkflowRunHistory((prev) =>
          prev.map((entry) => (entry.run_id === runId ? { ...entry, status: statusValue } : entry)),
        );
      }
    },
    [runServiceAction],
  );

  const handleFetchWorkflowArtifacts = useCallback(
    async (runId: string) => {
      const result = await runServiceAction("workflows.artifacts", { run_id: runId });
      setWorkflowArtifacts((prev) => ({ ...prev, [runId]: result }));
    },
    [runServiceAction],
  );

  const handleFetchWorkflowResult = useCallback(
    async (runId: string) => {
      const result = await runServiceAction("workflows.result", { run_id: runId });
      setWorkflowResults((prev) => ({ ...prev, [runId]: result }));
    },
    [runServiceAction],
  );

  useEffect(() => {
    const candidateIndex = workflowRuns.findIndex((run, index) => {
      const runId = run.run_id ?? `workflow-${index + 1}`;
      const status = run.status ?? "unknown";
      if (!["completed", "failed", "cancelled"].includes(status)) return false;
      if (workflowResults[runId]) return false;
      if (requestedWorkflowResultsRef.current.has(runId)) return false;
      return true;
    });
    if (candidateIndex === -1) return;
    const candidate = workflowRuns[candidateIndex];
    const candidateId = candidate.run_id ?? `workflow-${candidateIndex + 1}`;
    requestedWorkflowResultsRef.current.add(candidateId);
    void handleFetchWorkflowResult(candidateId);
  }, [handleFetchWorkflowResult, workflowResults, workflowRuns]);

  const handleFetchWorkflowRunLogs = useCallback(
    async (runId: string) => {
      if (workflowRunLogsInFlightRef.current.has(runId)) return;
      workflowRunLogsInFlightRef.current.add(runId);
      try {
        const resp = await runServiceAction("logs.run_logs", { run_id: runId, limit: 500 });
        const items = (resp as any)?.logs ?? resp;
        if (currentWorkflowRunIdRef.current !== runId) return;
        setCurrentWorkflowRunLogs(Array.isArray(items) ? items : []);
      } finally {
        workflowRunLogsInFlightRef.current.delete(runId);
      }
    },
    [runServiceAction],
  );

  const handleClearWorkflowHistory = useCallback(() => {
    setWorkflowRunHistory([]);
  }, []);

  const handleGenerateYaml = useCallback(async () => {
    setBuilderError(null);
    try {
      const inputs: Record<string, string> = {};
      builderInputs.forEach(({ key, value }) => {
        const trimmed = key.trim();
        if (!trimmed) return;
        inputs[trimmed] = value;
      });
      const pipelineInfo = { ...builderInfo, inputs };
      const steps = JSON.parse(builderSteps || "[]");
      const result = (await runServiceAction("workflows.generate_yaml", {
        pipeline_info: pipelineInfo,
        steps,
      })) as { yaml?: string };
      setBuilderYaml(result?.yaml ?? "");
    } catch (err) {
      setBuilderError(err instanceof Error ? err.message : "Не удалось сгенерировать YAML");
    }
  }, [builderInfo, builderInputs, builderSteps, runServiceAction]);

  const handleSaveYaml = useCallback(async () => {
    if (!builderSaveName.trim() || !builderYaml.trim()) {
      setBuilderError("Укажите имя и сгенерируйте YAML перед сохранением");
      return;
    }
    try {
      await runServiceAction("workflows.save_yaml", {
        workflow_name: builderSaveName.trim(),
        yaml: builderYaml,
      });
      await loadWorkflows();
    } catch (err) {
      setBuilderError(err instanceof Error ? err.message : "Не удалось сохранить YAML");
    }
  }, [builderSaveName, builderYaml, loadWorkflows, runServiceAction]);

  useEffect(() => {
    if (activeSection !== "workflows") return;
    void loadWorkflows();
  }, [activeSection, loadWorkflows]);

  // DB plugins logic
  const loadDbPlugins = useCallback(async () => {
    setDbPluginsLoading(true);
    setDbPluginsError(null);
    try {
      const result = (await runServiceAction("db.list", {})) as { plugins?: DbPlugin[] };
      setDbPlugins(Array.isArray(result?.plugins) ? result.plugins : []);
    } catch (err) {
      setDbPluginsError(err instanceof Error ? err.message : "Не удалось загрузить плагины");
    } finally {
      setDbPluginsLoading(false);
    }
  }, [runServiceAction]);

  const handleQuickTestPlugin = async (scheme: string) => {
    try {
      const result = await runServiceAction("db.quick_test", { scheme });
      setDbTestResult(result);
    } catch (err) {
      setDbTestResult(err instanceof Error ? err.message : err);
    }
  };

  const handleRunDbTest = async () => {
    const payload = {
      dsn: dbTestForm.dsn,
      timeout_seconds: dbTestForm.timeout,
      test_basic_query: dbTestForm.testBasic,
      test_schema_introspection: dbTestForm.testSchema,
      test_security_validation: dbTestForm.testSecurity,
      verbose_output: dbTestForm.verbose,
    };
    try {
      const result = await runServiceAction("db.comprehensive_test", payload);
      setDbTestResult(result);
    } catch (err) {
      setDbTestResult(err instanceof Error ? err.message : err);
    }
  };

  const loadDbConfigs = useCallback(async () => {
    setDbConfigsLoading(true);
    setDbConfigsError(null);
    try {
      const result = (await runServiceAction("db.test_configs.list", {})) as { configs?: DbTestConfig[] };
      const configs = Array.isArray(result?.configs) ? result.configs : [];
      setDbConfigs(configs);
    } catch (err) {
      setDbConfigsError(err instanceof Error ? err.message : "Не удалось загрузить конфиги");
    } finally {
      setDbConfigsLoading(false);
    }
  }, [runServiceAction]);

  const handleSaveDbConfig = async () => {
    if (!dbConfigForm.name.trim() || !dbConfigForm.dsn.trim()) {
      setDbConfigsError("Имя и DSN обязательны");
      return;
    }
    try {
      await runServiceAction("db.test_configs.save", dbConfigForm);
      setDbConfigForm({ name: "", dsn: "", description: "" });
      await loadDbConfigs();
    } catch (err) {
      setDbConfigsError(err instanceof Error ? err.message : "Не удалось сохранить");
    }
  };

  const handleDeleteDbConfig = async (name: string) => {
    try {
      await runServiceAction("db.test_configs.delete", { name });
      await loadDbConfigs();
    } catch (err) {
      setDbConfigsError(err instanceof Error ? err.message : "Не удалось удалить конфиг");
    }
  };

  const loadDbDiagnostics = useCallback(async () => {
    try {
      const result = await runServiceAction("db.diagnostics", {});
      setDbDiagnostics(result as Record<string, unknown>);
    } catch (err) {
      setDbDiagnostics({ error: err instanceof Error ? err.message : "Не удалось получить диагностику" });
    }
  }, [runServiceAction]);

  const loadDbBenchmark = useCallback(async () => {
    try {
      const result = await runServiceAction("db.benchmark", {});
      setDbBenchmark(result as Record<string, unknown>);
    } catch (err) {
      setDbBenchmark({ error: err instanceof Error ? err.message : "Не удалось запустить бенчмарк" });
    }
  }, [runServiceAction]);

  useEffect(() => {
    if (activeSection !== "workflows" || workflowTab !== "monitor") return;
    void refreshWorkflowRuns();
  }, [activeSection, workflowTab, refreshWorkflowRuns]);

  useEffect(() => {
    if (!workflowAutoRefresh || activeSection !== "workflows" || workflowTab !== "monitor") return;
    const interval = window.setInterval(() => void refreshWorkflowRuns(), 3000);
    return () => window.clearInterval(interval);
  }, [workflowAutoRefresh, activeSection, workflowTab, refreshWorkflowRuns]);

  const workflowCategories = useMemo(() => {
    const categories = Array.from(new Set(workflows.map((wf) => wf.category).filter((cat): cat is string => Boolean(cat))));
    return ["Все", ...categories];
  }, [workflows]);
  const workflowComplexities = useMemo(() => {
    const values = Array.from(new Set(workflows.map((wf) => wf.complexity).filter((value): value is string => Boolean(value))));
    return ["Все", ...values];
  }, [workflows]);
  const filteredWorkflows = useMemo(() => {
    return workflows.filter((wf) => {
      if (workflowCategoryFilter !== "Все" && wf.category !== workflowCategoryFilter) return false;
      if (workflowComplexityFilter !== "Все" && wf.complexity !== workflowComplexityFilter) return false;
      if (workflowSearch.trim()) {
        const term = workflowSearch.trim().toLowerCase();
        return wf.name.toLowerCase().includes(term) || (wf.description ?? "").toLowerCase().includes(term);
      }
      return true;
    });
  }, [workflowCategoryFilter, workflowComplexityFilter, workflowSearch, workflows]);

  useEffect(() => {
    if (activeSection !== "db") return;
    if (dbTab === "plugins" || dbTab === "test") void loadDbPlugins();
    if (dbTab === "configs") void loadDbConfigs();
    if (dbTab === "diagnostics") void loadDbDiagnostics();
  }, [activeSection, dbTab, loadDbConfigs, loadDbPlugins, loadDbDiagnostics]);

  // Memory logic
  const loadMemoryStatus = useCallback(async () => {
    setMemoryStatusLoading(true);
    setMemoryStatusError(null);
    try {
      const result = await runServiceAction("memory.status", {});
      // backend отдаёт { status: {...} }, но UI ожидает поля на верхнем уровне
      const unwrapped =
        result && typeof result === "object" && "status" in (result as any) ? (result as any).status : result;
      setMemoryStatus(unwrapped);
    } catch (err) {
      setMemoryStatusError(err instanceof Error ? err.message : "Не удалось получить статус памяти");
    } finally {
      setMemoryStatusLoading(false);
    }
  }, [runServiceAction]);

  const handleMemorySearch = useCallback(async () => {
    setMemorySearchError(null);
    try {
      const result = await runServiceAction("memory.search", {
        query: memorySearchForm.query,
        memory_type: memorySearchForm.memoryType,
        limit: memorySearchForm.limit,
        session_id: memorySearchForm.sessionId || undefined,
        agent_name: memorySearchForm.agentName || undefined,
      });
      setMemorySearchResults(result);
    } catch (err) {
      setMemorySearchError(err instanceof Error ? err.message : "Не удалось выполнить поиск");
    }
  }, [memorySearchForm, runServiceAction]);

  const handleMemoryAnalytics = useCallback(async () => {
    try {
      const summary = await runServiceAction("memory.analytics.summary", { days: memoryAnalyticsDays });
      const timeseries = await runServiceAction("memory.analytics.timeseries", { days: memoryAnalyticsDays });
      const keywords = await runServiceAction("memory.analytics.keywords", { limit: 20, min_len: 4 });
      const unwrapResult = (value: unknown) => {
        if (!value || typeof value !== "object") return value;
        if ("result" in (value as any)) return (value as any).result;
        return value;
      };
      setMemoryAnalytics({
        summary: unwrapResult(summary),
        timeseries: unwrapResult(timeseries),
        keywords: unwrapResult(keywords),
      });
    } catch (err) {
      setMemoryAnalytics({ error: err instanceof Error ? err.message : "Ошибка аналитики" });
    }
  }, [memoryAnalyticsDays, runServiceAction]);

  const handleMemoryAction = useCallback(
    async (action: string, payload: Record<string, unknown> = {}) => {
      setMemoryManageResult(null);
      const result = await runServiceAction(action, payload);
      setMemoryManageResult({ action, result });
    },
    [runServiceAction],
  );

  const handleMemoryTestEmbeddings = useCallback(async () => {
    try {
      const result = await runServiceAction("memory.embeddings.test", {});
      setMemoryTestResult(result);
    } catch (err) {
      setMemoryTestResult({ error: err instanceof Error ? err.message : "Ошибка теста" });
    }
  }, [runServiceAction]);

  useEffect(() => {
    if (activeSection !== "memory") return;
    void loadMemoryStatus();
  }, [activeSection, loadMemoryStatus]);

  return (
    <div className="app-shell">
      <header className="app-header">
        <div>
          <div className="app-title">MultiAgent Studio</div>
          <div className="app-subtitle">Центр управления запусками, сервисными функциями и мониторинга</div>
        </div>
      </header>

      <main className="app-grid">
        <aside className="nav-panel">
          <nav className="nav-list">
            {sections.map((section) => (
              <button
                key={section.id}
                type="button"
                className={`nav-link${activeSection === section.id ? " active" : ""}`}
                onClick={() => setActiveSection(section.id)}
              >
                {section.label}
              </button>
            ))}
          </nav>
        </aside>

        <section className="panel content-panel">
          {activeSection === "dashboard" ? (
            <DashboardSection
              runServiceAction={runServiceAction}
              isBusy={isBusy}
              onNavigate={setActiveSection}
              serviceReady={serviceReady}
            />
          ) : null}


          {activeSection === "agents" ? (
            <AgentsSection
              isBusy={isBusy}
              runServiceAction={runServiceAction}
              agentTab={agentTab}
              setAgentTab={setAgentTab}
              loadAgentProfiles={loadAgentProfiles}
              profilesLoading={profilesLoading}
              profilesError={profilesError}
              agentProfiles={agentProfiles}
              availableProfileTypes={availableProfileTypes}
              availableProfileModels={availableProfileModels}
              profileTypeFilter={profileTypeFilter}
              setProfileTypeFilter={setProfileTypeFilter}
              profileModelFilter={profileModelFilter}
              setProfileModelFilter={setProfileModelFilter}
              profileSearch={profileSearch}
              setProfileSearch={setProfileSearch}
              filteredProfiles={filteredProfiles}
              handleSelectProfile={handleSelectProfile}
              getModelLabel={getModelLabel}
              selectedProfile={selectedProfile}
              agentRunForm={agentRunForm}
              setAgentRunForm={setAgentRunForm}
              createdAgentsForProfile={createdAgentsForProfile}
              createdAgents={createdAgentsList}
              handleRemoveCreatedAgent={handleRemoveCreatedAgent}
              agentRunHistory={agentRunHistory}
              handleClearAgentHistory={handleClearAgentHistory}
              handleCleanupRuns={handleCleanupRuns}
              handleRunAgent={handleRunAgent}
              refreshActiveRuns={refreshActiveRuns}
              runsLoading={runsLoading}
              runsError={runsError}
              activeAgentRuns={activeAgentRuns}
              runResults={runResults}
              currentAgentRunId={currentAgentRunId}
              currentAgentRunLogs={currentAgentRunLogs}
              handleFetchRunStatus={handleFetchRunStatus}
              handleFetchRunResult={handleFetchRunResult}
              handleCancelRun={handleCancelRun}
              handleFetchRunLogs={handleFetchRunLogs}
              autoRefreshRuns={autoRefreshRuns}
              setAutoRefreshRuns={setAutoRefreshRuns}
            />
          ) : null}

          {activeSection === "dynamic-agents" ? <DynamicAgentsSection runServiceAction={runServiceAction} isBusy={isBusy} /> : null}

          {activeSection === "workflows" ? (
            <WorkflowsSection
              isBusy={isBusy}
              runServiceAction={runServiceAction}
              workflowTab={workflowTab}
              setWorkflowTab={setWorkflowTab}
              workflows={workflows}
              workflowsLoading={workflowsLoading}
              workflowsError={workflowsError}
              workflowCategoryFilter={workflowCategoryFilter}
              workflowComplexityFilter={workflowComplexityFilter}
              workflowSearch={workflowSearch}
              workflowCategories={workflowCategories}
              workflowComplexities={workflowComplexities}
              filteredWorkflows={filteredWorkflows}
              selectedWorkflow={selectedWorkflow}
              workflowInputs={workflowInputs}
              workflowParams={workflowParams}
              workflowOptions={workflowOptions}
              workflowRuns={workflowRuns}
              workflowRunsLoading={workflowRunsLoading}
              workflowRunsError={workflowRunsError}
              workflowAutoRefresh={workflowAutoRefresh}
              currentWorkflowRunId={currentWorkflowRunId}
              currentWorkflowRunLogs={currentWorkflowRunLogs}
              workflowResults={workflowResults}
              workflowRunHistory={workflowRunHistory}
              workflowArtifacts={workflowArtifacts}
              builderInfo={builderInfo}
              builderInputs={builderInputs}
              builderSteps={builderSteps}
              builderYaml={builderYaml}
              builderSaveName={builderSaveName}
              builderError={builderError}
              workflowRunError={workflowRunError}
              setWorkflowCategoryFilter={setWorkflowCategoryFilter}
              setWorkflowComplexityFilter={setWorkflowComplexityFilter}
              setWorkflowSearch={setWorkflowSearch}
              loadWorkflows={loadWorkflows}
              handleSelectWorkflow={handleSelectWorkflow}
              handleViewWorkflowYaml={handleViewWorkflowYaml}
              loadWorkflowInputs={loadWorkflowInputs}
              setWorkflowOptions={setWorkflowOptions}
              setWorkflowParams={setWorkflowParams}
              handleRunWorkflow={handleRunWorkflow}
              handleFetchWorkflowStatus={handleFetchWorkflowStatus}
              handleFetchWorkflowArtifacts={handleFetchWorkflowArtifacts}
              handleFetchWorkflowResult={handleFetchWorkflowResult}
              handleFetchWorkflowRunLogs={handleFetchWorkflowRunLogs}
              handleCancelWorkflow={handleCancelWorkflow}
              handleClearWorkflowHistory={handleClearWorkflowHistory}
              refreshWorkflowRuns={refreshWorkflowRuns}
              setWorkflowAutoRefresh={setWorkflowAutoRefresh}
              setBuilderInfo={setBuilderInfo}
              setBuilderInputs={setBuilderInputs}
              setBuilderSteps={setBuilderSteps}
              setBuilderYaml={setBuilderYaml}
              setBuilderSaveName={setBuilderSaveName}
              handleGenerateYaml={handleGenerateYaml}
              handleSaveYaml={handleSaveYaml}
            />
          ) : null}

          <div hidden={activeSection !== "text-to-sql"}>
            <TextToSqlSection
              runServiceAction={runServiceAction}
              isBusy={isBusy}
              active={activeSection === "text-to-sql"}
            />
          </div>

          {activeSection === "db" ? (
            <DbSection
              isBusy={isBusy}
              dbTab={dbTab}
              setDbTab={setDbTab}
              dbPlugins={dbPlugins}
              dbPluginsLoading={dbPluginsLoading}
              dbPluginsError={dbPluginsError}
              dbTestForm={dbTestForm}
              dbTestResult={dbTestResult}
              dbConfigs={dbConfigs}
              dbConfigsLoading={dbConfigsLoading}
              dbConfigsError={dbConfigsError}
              dbConfigForm={dbConfigForm}
              dbDiagnostics={dbDiagnostics as any}
              dbBenchmark={dbBenchmark as any}
              loadDbPlugins={loadDbPlugins}
              handleQuickTestPlugin={handleQuickTestPlugin}
              setDbTestForm={setDbTestForm}
              handleRunDbTest={handleRunDbTest}
              loadDbConfigs={loadDbConfigs}
              setDbConfigForm={setDbConfigForm}
              handleSaveDbConfig={handleSaveDbConfig}
              handleDeleteDbConfig={handleDeleteDbConfig}
              setDbTestResult={setDbTestResult}
              loadDbDiagnostics={loadDbDiagnostics}
              loadDbBenchmark={loadDbBenchmark}
            />
          ) : null}

          {activeSection === "memory" ? (
            <MemorySection
              isBusy={isBusy}
              memoryTab={memoryTab}
              setMemoryTab={setMemoryTab}
              loadMemoryStatus={loadMemoryStatus}
              memoryStatusError={memoryStatusError}
              memoryStatusLoading={memoryStatusLoading}
              memoryStatus={memoryStatus}
              memorySearchForm={memorySearchForm}
              setMemorySearchForm={setMemorySearchForm}
              handleMemorySearch={handleMemorySearch}
              memorySearchError={memorySearchError}
              memorySearchResults={memorySearchResults}
              memoryAnalytics={memoryAnalytics}
              memoryAnalyticsDays={memoryAnalyticsDays}
              setMemoryAnalyticsDays={setMemoryAnalyticsDays}
              handleMemoryAnalytics={handleMemoryAnalytics}
              memoryManageResult={memoryManageResult}
              handleMemoryAction={handleMemoryAction}
              memoryTestResult={memoryTestResult}
              handleMemoryTestEmbeddings={handleMemoryTestEmbeddings}
              memoryClearForm={memoryClearForm}
              setMemoryClearForm={setMemoryClearForm}
            />
          ) : null}

          {activeSection === "config" ? <ConfigSection runServiceAction={runServiceAction} isBusy={isBusy} /> : null}

          {activeSection === "telemetry" ? <TelemetrySection runServiceAction={runServiceAction} isBusy={isBusy} /> : null}


          {activeSection === "tools" ? <ToolsSection runServiceAction={runServiceAction} isBusy={isBusy} /> : null}

          {activeSection === "system" ? (
            <SystemSection runServiceAction={runServiceAction} isBusy={isBusy} progressResults={results} clearProgressResults={clearProgressResults} />
          ) : null}

        </section>
      </main>
            {workflowYamlModal.open ? (
              <div className="modal-overlay" onClick={() => setWorkflowYamlModal({ open: false })}>
                <div className="modal" onClick={(event) => event.stopPropagation()}>
                  <div className="section-header">
                    <div>
                      <div className="card-title">Граф workflow</div>
                      <div className="card-description">{workflowYamlModal.name}</div>
                    </div>
                    <button className="modal-close" aria-label="Закрыть" onClick={() => setWorkflowYamlModal({ open: false })}>
                      ×
                    </button>
                  </div>
                  {workflowYamlModal.meta ? (
                    <div className="profile-meta meta-grid">
                      <div>
                        <span className="label">Имя</span>
                        <div className="meta-value">{workflowYamlModal.meta.name || "—"}</div>
                      </div>
                      <div>
                        <span className="label">Версия</span>
                        <div className="meta-value">{workflowYamlModal.meta.version || "—"}</div>
                      </div>
                      <div>
                        <span className="label">Категория</span>
                        <div className="meta-value">{workflowYamlModal.meta.category || "general"}</div>
                      </div>
                      <div>
                        <span className="label">Сложность</span>
                        <div className="meta-value">{workflowYamlModal.meta.complexity || "—"}</div>
                      </div>
                      <div>
                        <span className="label">Шагов</span>
                        <div className="meta-value">{workflowYamlModal.meta.steps_count ?? "—"}</div>
                      </div>
                      <div>
                        <span className="label">Оценка времени</span>
                        <div className="meta-value">{workflowYamlModal.meta.estimated_duration || "—"}</div>
                      </div>
                      {typeof workflowYamlModal.pipeline?.parallel_execution !== "undefined" ? (
                        <div>
                          <span className="label">Параллельное выполнение</span>
                          <div className="meta-value">
                            {workflowYamlModal.pipeline?.parallel_execution ? "Вкл" : "Выкл"}
                          </div>
                        </div>
                      ) : null}
                      {workflowYamlModal.pipeline?.max_parallel_steps ? (
                        <div>
                          <span className="label">Max parallel</span>
                          <div className="meta-value">{workflowYamlModal.pipeline?.max_parallel_steps as string}</div>
                        </div>
                      ) : null}
                    </div>
                  ) : null}
                  {workflowYamlModal.pipeline?.description ? (
                    <div className="card-description">{String(workflowYamlModal.pipeline.description)}</div>
                  ) : null}
                  {workflowYamlModal.metadata || workflowYamlModal.pipeline?.metadata || yamlMetadataSnippet ? (
                    <details className="details">
                      <summary>Metadata</summary>
                      <div className="graph-inputs">
                        {workflowYamlModal.metadata
                          ? Object.entries(workflowYamlModal.metadata as Record<string, unknown>).map(([key, value]) => (
                              <div key={key} className="graph-input">
                                <div className="label">{key}</div>
                                {Array.isArray(value) ? (
                                  <div className="badge-row">
                                    {value.map((tag) => (
                                      <span key={String(tag)} className="badge">
                                        {String(tag)}
                                      </span>
                                    ))}
                                  </div>
                                ) : (
                                  <div className="meta-value">{String(value)}</div>
                                )}
                              </div>
                            ))
                          : null}
                        {!workflowYamlModal.metadata && workflowYamlModal.pipeline?.metadata
                          ? Object.entries(workflowYamlModal.pipeline.metadata as Record<string, unknown>).map(([key, value]) => (
                              <div key={key} className="graph-input">
                                <div className="label">{key}</div>
                                <div className="meta-value">{Array.isArray(value) ? value.join(", ") : String(value)}</div>
                              </div>
                            ))
                          : null}
                        {!workflowYamlModal.metadata && !workflowYamlModal.pipeline?.metadata && yamlMetadataSnippet ? (
                          <div className="graph-input">
                            <textarea className="code" readOnly value={yamlMetadataSnippet} style={{ minHeight: 120 }} />
                          </div>
                        ) : null}
                      </div>
                    </details>
                  ) : null}
                  {workflowYamlModal.inputs && Object.keys(workflowYamlModal.inputs).length ? (
                    <details className="details">
                      <summary>Inputs</summary>
                      <div className="graph-inputs">
                        {Object.entries(workflowYamlModal.inputs).map(([key, value]) => (
                          <div key={key} className="graph-input">
                            <div className="label">{key}</div>
                            <div className="meta-value" style={{ whiteSpace: "pre-wrap" }}>
                              {String(value)}
                            </div>
                          </div>
                        ))}
                      </div>
                    </details>
                  ) : null}
                  {(!workflowYamlModal.inputs || Object.keys(workflowYamlModal.inputs).length === 0) && yamlInputsSnippet ? (
                    <details className="details">
                      <summary>Входные данные</summary>
                      <div className="graph-inputs">
                        <div className="graph-input">
                          <textarea className="code" readOnly value={yamlInputsSnippet} style={{ minHeight: 120 }} />
                        </div>
                      </div>
                    </details>
                  ) : null}
                  {workflowYamlModal.pipeline?.global_retry_policy || workflowYamlModal.pipeline?.global_resource_limits ? (
                    <details className="details" open>
                      <summary>Политики</summary>
                      <div className="graph-inputs">
                        {workflowYamlModal.pipeline?.global_retry_policy ? (
                          <div className="graph-input">
                            <div className="label">Retry policy</div>
                            <KeyValueList data={workflowYamlModal.pipeline.global_retry_policy} />
                          </div>
                        ) : null}
                        {workflowYamlModal.pipeline?.global_resource_limits ? (
                          <div className="graph-input">
                            <div className="label">Resource limits</div>
                            <KeyValueList data={workflowYamlModal.pipeline.global_resource_limits} />
                          </div>
                        ) : null}
                      </div>
                    </details>
                  ) : null}
                  {workflowYamlModal.meta?.agents_used?.length ? (
                    <details className="details">
                      <summary>Агенты / инструменты</summary>
                      <div className="card-description">{workflowYamlModal.meta.agents_used.join(", ")}</div>
                    </details>
                  ) : null}
                  {workflowYamlModal.steps && workflowYamlModal.steps.length && workflowGraph ? (
              <div className="graph-wrapper">
                <div className="graph-legend">
                  <span className="badge agent">Agent</span>
                  <span className="badge tool">Tool</span>
                  <span className="badge muted">Стрелки показывают зависимость</span>
                </div>
                <div className="graph-scroll" style={{ width: "100%", height: "60vh" }}>
                  <svg
                    className="graph-svg"
                    width={workflowGraph.size?.width ?? 800}
                    height={workflowGraph.size?.height ?? 600}
                    viewBox={`0 0 ${workflowGraph.size?.width ?? 800} ${workflowGraph.size?.height ?? 600}`}
                  >
                    {workflowGraph.edges.map((edge, idx) => {
                      const from = workflowGraph.positions?.[edge.from];
                      const to = workflowGraph.positions?.[edge.to];
                      if (!from || !to) return null;
                      const x1 = from.x + 120;
                      const y1 = from.y + 50;
                      const x2 = to.x + 20;
                      const y2 = to.y + 50;
                      const mx = (x1 + x2) / 2;
                      return (
                        <g key={`${edge.from}-${edge.to}-${idx}`} className="graph-edge">
                          <path d={`M ${x1} ${y1} C ${mx} ${y1} ${mx} ${y2} ${x2} ${y2}`} />
                          <polygon points={`${x2 - 8},${y2 - 4} ${x2},${y2} ${x2 - 8},${y2 + 4}`} />
                        </g>
                      );
                    })}
                  </svg>
                  <div className="graph-nodes">
                    {workflowGraph.nodes.map((node) => {
                      const step = node.data;
                      const executor = step.agent || step.tool || step.executor || "—";
                      const pos = workflowGraph.positions?.[node.id] || { x: 0, y: 0 };
                      const typeClass = (step.step_type || step.type || "agent").toString().toLowerCase();
                      return (
                        <div
                          key={node.id}
                          className={`graph-node ${typeClass}`}
                          style={{ left: pos.x, top: pos.y, width: 260, position: "absolute" }}
                        >
                        <div className="card-title">{node.id}</div>
                        <div className="card-description">Тип: {step.step_type || step.type || "agent"}</div>
                        <div className="card-description">Исполнитель: {executor}</div>
                          {step.task ? <div className="card-description">Задача: {String(step.task).slice(0, 120)}</div> : null}
                          <div className="badge-row">
                            <span className="badge muted">Таймаут: {step.timeout || "—"}</span>
                            {step.condition ? <span className="badge">Условие</span> : null}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </div>
                <div className="card-description">Тяните горизонтальный скролл, чтобы увидеть длинные цепочки.</div>
              </div>
            ) : (
              <div className="card-description">Шаги не найдены, показываем YAML.</div>
            )}
            {workflowYamlModal.yaml ? (
              <details className="details">
                <summary>YAML</summary>
                <textarea className="code" readOnly value={workflowYamlModal.yaml} style={{ minHeight: 220 }} />
              </details>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}

export default function Page() {
  const backendUrl = normalizeAgentUrl(process.env.NEXT_PUBLIC_AG_UI_URL || DEFAULT_BACKEND_URL);
  const agent = useMemo(() => new HttpConnectAgent({ url: backendUrl }), [backendUrl]);

  return (
    <CopilotKitProvider agents__unsafe_dev_only={{ default: agent as any }} renderToolCalls={[wildcardRenderer]} showDevConsole="auto">
      <AguiStudio />
    </CopilotKitProvider>
  );
}

const normalizeAgentUrl = (value: string) => {
  const trimmed = value.trim().replace(/\/+$/, "");
  if (!trimmed) return DEFAULT_BACKEND_URL;
  return trimmed.endsWith("/agent") ? trimmed : `${trimmed}/agent`;
};
