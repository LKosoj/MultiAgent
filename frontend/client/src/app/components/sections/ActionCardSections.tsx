"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ActionCard } from "../shared/ActionCard";
import { KeyValueList } from "../shared/KeyValueList";
import { openReportFromPayload } from "../../utils/report";

type ActionCardProps = {
  runServiceAction: (action: string, payload: Record<string, unknown>) => Promise<unknown>;
  isBusy: boolean;
};

type ProgressResult = {
  id: string;
  action: string;
  status: "ok" | "error" | "event";
  data: unknown;
  timestamp: string;
};

type ProgressSectionProps = ActionCardProps & {
  results: ProgressResult[];
  clearResults: () => void;
};

const MERMAID_TEMPLATES: Record<string, string> = {
  Граф: `graph TD
  A[Начало] --> B[Процесс]
  B --> C{Решение?}
  C -->|Да| D[Действие 1]
  C -->|Нет| E[Действие 2]
  D --> F[Конец]
  E --> F`,
  Последовательность: `sequenceDiagram
  participant A as Пользователь
  participant B as Агент
  participant C as БД

  A->>B: Запрос
  B->>C: Запрос данных
  C-->>B: Данные
  B-->>A: Ответ`,
  "Диаграмма классов": `classDiagram
  class Agent {
    +String name
    +String type
    +execute(task)
    +getStatus()
  }
  class Workflow {
    +String name
    +List steps
    +run()
  }
  Agent --|> Workflow : uses`,
};

const PLANTUML_TEMPLATES: Record<string, string> = {
  "Диаграмма классов": `@startuml
class Agent {
  - name: String
  - type: String
  + execute(task: String): Result
  + getStatus(): Status
}

class Workflow {
  - steps: List<Step>
  + run(): Result
}

Agent --> Workflow
@enduml`,
  "Диаграмма последовательности": `@startuml
actor User
participant Agent
participant Database

User -> Agent: Запрос
Agent -> Database: Запрос данных
Database --> Agent: Данные
Agent --> User: Результат
@enduml`,
};

export function ConfigSection({ runServiceAction, isBusy }: ActionCardProps) {
  const [tab, setTab] = useState<"llm" | "telemetry" | "security" | "memory" | "system">("llm");
  const [config, setConfig] = useState<any | null>(null);
  const [providers, setProviders] = useState<Record<string, any>>({});
  const [llmTest, setLlmTest] = useState<any | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [llmForm, setLlmForm] = useState<any>({});
  const [telemetryForm, setTelemetryForm] = useState<any>({});
  const [securityForm, setSecurityForm] = useState<any>({});
  const [memoryForm, setMemoryForm] = useState<any>({});
  const [systemForm, setSystemForm] = useState<any>({});
  const [loggingForm, setLoggingForm] = useState<any>({});
  const [resourceLimitsForm, setResourceLimitsForm] = useState<any>({});
  const [performanceForm, setPerformanceForm] = useState<any>({});
  const [networkForm, setNetworkForm] = useState<any>({});

  const loadConfig = useCallback(async () => {
    try {
      const resp = await runServiceAction("config.get", {});
      setConfig((resp as any)?.config ?? resp);
    } catch (err) {
      setStatus(err instanceof Error ? err.message : "Не удалось загрузить конфигурацию");
    }
  }, [runServiceAction]);

  const loadProviders = useCallback(async () => {
    try {
      const resp = await runServiceAction("config.llm_providers", {});
      setProviders((resp as any)?.providers ?? {});
    } catch (err) {
      setStatus(err instanceof Error ? err.message : "Не удалось загрузить провайдеры");
    }
  }, [runServiceAction]);

  useEffect(() => {
    void loadConfig();
    void loadProviders();
  }, [loadConfig, loadProviders]);

  useEffect(() => {
    if (!config) return;
    setLlmForm(config.llm ?? {});
    setTelemetryForm(config.telemetry ?? {});
    setSecurityForm(config.security ?? {});
    setMemoryForm(config.memory ?? {});
    setSystemForm(config.system ?? {});
    setLoggingForm(config.logging ?? {});
    setResourceLimitsForm(config.resource_limits ?? {});
    setPerformanceForm(config.performance ?? {});
    setNetworkForm(config.network ?? {});
  }, [config]);

  const updateSection = async (section: string, payload: Record<string, unknown>) => {
    setStatus(null);
    try {
      await runServiceAction("config.update_section", { section, config: payload });
      setStatus("Сохранено");
      await loadConfig();
    } catch (err) {
      setStatus(err instanceof Error ? err.message : "Не удалось обновить");
    }
  };

  const handleTelemetrySave = async () => {
    setStatus(null);
    try {
      await runServiceAction("config.update_section", { section: "telemetry", config: telemetryForm });
      if (telemetryForm.enabled) {
        await runServiceAction("telemetry.enable", {});
      } else {
        await runServiceAction("telemetry.disable", {});
      }
      setStatus("Сохранено");
      await loadConfig();
    } catch (err) {
      setStatus(err instanceof Error ? err.message : "Не удалось обновить");
    }
  };

  const handleLlmTest = async () => {
    setLlmTest(null);
    try {
      const resp = await runServiceAction("config.test_llm", { provider: llmForm.provider, model: llmForm.model });
      setLlmTest((resp as any)?.result ?? resp);
    } catch (err) {
      setStatus(err instanceof Error ? err.message : "Не удалось протестировать LLM");
    }
  };

  const providerInfo = providers?.[llmForm.provider] ?? {};
  const providerModels = providerInfo?.models ?? [];
  const modelDetails = providerInfo?.model_details ?? {};
  const usesSystemConnections = !!providerInfo?.uses_system_connections;
  const allowedOperations = Array.isArray(securityForm.allowed_sql_operations) ? securityForm.allowed_sql_operations : [];

  const updateSecurityList = (field: string, text: string) => {
    const values = text
      .split("\n")
      .map((item) => item.trim())
      .filter(Boolean);
    setSecurityForm((prev: any) => ({ ...prev, [field]: values }));
  };

  return (
    <div className="section" id="config">
      <div className="section-header">
        <div className="section-title">Конфигурация</div>
        <div className="section-hint">Системные настройки и окружение</div>
      </div>
      <div className="segment-row">
        <button className={`segment-button${tab === "llm" ? " active" : ""}`} onClick={() => setTab("llm")}>
          LLM
        </button>
        <button className={`segment-button${tab === "telemetry" ? " active" : ""}`} onClick={() => setTab("telemetry")}>
          Телеметрия
        </button>
        <button className={`segment-button${tab === "security" ? " active" : ""}`} onClick={() => setTab("security")}>
          Безопасность
        </button>
        <button className={`segment-button${tab === "memory" ? " active" : ""}`} onClick={() => setTab("memory")}>
          Память
        </button>
        <button className={`segment-button${tab === "system" ? " active" : ""}`} onClick={() => setTab("system")}>
          Система
        </button>
      </div>

      {tab === "llm" ? (
        <div className="card">
          <div className="card-title">LLM конфигурация</div>
          <div className="form-grid">
            <label className="field">
              <span className="label">Провайдер</span>
              <select value={llmForm.provider ?? ""} onChange={(e) => setLlmForm((p: any) => ({ ...p, provider: e.target.value }))}>
                <option value="">--</option>
                {Object.keys(providers || {}).map((p) => (
                  <option key={p} value={p}>
                    {p}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span className="label">Модель</span>
              <select value={llmForm.model ?? ""} onChange={(e) => setLlmForm((p: any) => ({ ...p, model: e.target.value }))}>
                <option value="">--</option>
                {providerModels.map((m: string) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
            </label>
            {usesSystemConnections ? (
              <div className="field">
                <span className="label">Системные подключения</span>
                <div className="card-description">Используются переменные окружения OPENAI_API_BASE_DB и OPENAI_API_KEY_DB.</div>
              </div>
            ) : (
              <>
                <label className="field">
                  <span className="label">API key</span>
                  <input value={llmForm.api_key ?? ""} onChange={(e) => setLlmForm((p: any) => ({ ...p, api_key: e.target.value }))} />
                </label>
                <label className="field">
                  <span className="label">Base URL</span>
                  <input value={llmForm.base_url ?? ""} onChange={(e) => setLlmForm((p: any) => ({ ...p, base_url: e.target.value }))} />
                </label>
              </>
            )}
            <label className="field">
              <span className="label">Temperature</span>
              <input type="number" value={llmForm.temperature ?? 0.7} onChange={(e) => setLlmForm((p: any) => ({ ...p, temperature: Number(e.target.value) }))} />
            </label>
            <label className="field">
              <span className="label">Max tokens</span>
              <input type="number" value={llmForm.max_tokens ?? 4000} onChange={(e) => setLlmForm((p: any) => ({ ...p, max_tokens: Number(e.target.value) }))} />
            </label>
            <label className="field">
              <span className="label">Top P</span>
              <input type="number" value={llmForm.top_p ?? 1} onChange={(e) => setLlmForm((p: any) => ({ ...p, top_p: Number(e.target.value) }))} />
            </label>
            <label className="field">
              <span className="label">Frequency penalty</span>
              <input
                type="number"
                value={llmForm.frequency_penalty ?? 0}
                onChange={(e) => setLlmForm((p: any) => ({ ...p, frequency_penalty: Number(e.target.value) }))}
              />
            </label>
            <label className="field">
              <span className="label">Presence penalty</span>
              <input
                type="number"
                value={llmForm.presence_penalty ?? 0}
                onChange={(e) => setLlmForm((p: any) => ({ ...p, presence_penalty: Number(e.target.value) }))}
              />
            </label>
            <label className="field">
              <span className="label">Timeout (сек)</span>
              <input
                type="number"
                value={llmForm.timeout_seconds ?? 30}
                onChange={(e) => setLlmForm((p: any) => ({ ...p, timeout_seconds: Number(e.target.value) }))}
              />
            </label>
          </div>
          <div className="button-row">
            <button className="button" type="button" onClick={() => updateSection("llm", llmForm)} disabled={isBusy}>
              Сохранить
            </button>
            <button className="button secondary" type="button" onClick={handleLlmTest} disabled={isBusy}>
              Тест соединения
            </button>
          </div>
          {llmForm.model && modelDetails[llmForm.model] ? (
            <details className="details">
              <summary>Информация о модели</summary>
              <KeyValueList data={modelDetails[llmForm.model]} />
            </details>
          ) : null}
          {llmTest ? <KeyValueList data={llmTest} /> : null}
        </div>
      ) : null}

      {tab === "telemetry" ? (
        <div className="card">
          <div className="card-title">Телеметрия</div>
          <div className="form-grid">
            <label className="toggle">
              <input type="checkbox" checked={!!telemetryForm.enabled} onChange={(e) => setTelemetryForm((p: any) => ({ ...p, enabled: e.target.checked }))} />
              <span>Включено</span>
            </label>
            <label className="field">
              <span className="label">Trace retention (days)</span>
              <input type="number" value={telemetryForm.trace_retention_days ?? 7} onChange={(e) => setTelemetryForm((p: any) => ({ ...p, trace_retention_days: Number(e.target.value) }))} />
            </label>
            <label className="field">
              <span className="label">Detail level</span>
              <input value={telemetryForm.detail_level ?? ""} onChange={(e) => setTelemetryForm((p: any) => ({ ...p, detail_level: e.target.value }))} />
            </label>
            <label className="field">
              <span className="label">Traces dir</span>
              <input value={telemetryForm.traces_dir ?? ""} onChange={(e) => setTelemetryForm((p: any) => ({ ...p, traces_dir: e.target.value }))} />
            </label>
            <label className="field">
              <span className="label">Макс. размер файла (MB)</span>
              <input
                type="number"
                value={telemetryForm.max_trace_file_size_mb ?? 25}
                onChange={(e) => setTelemetryForm((p: any) => ({ ...p, max_trace_file_size_mb: Number(e.target.value) }))}
              />
            </label>
            <label className="field">
              <span className="label">Формат экспорта</span>
              <select value={telemetryForm.export_format ?? "jsonl"} onChange={(e) => setTelemetryForm((p: any) => ({ ...p, export_format: e.target.value }))}>
                <option value="jsonl">jsonl</option>
                <option value="json">json</option>
                <option value="otlp">otlp</option>
              </select>
            </label>
            <label className="field">
              <span className="label">Batch size</span>
              <input type="number" value={telemetryForm.batch_size ?? 100} onChange={(e) => setTelemetryForm((p: any) => ({ ...p, batch_size: Number(e.target.value) }))} />
            </label>
            <label className="field">
              <span className="label">Flush interval (сек)</span>
              <input
                type="number"
                value={telemetryForm.flush_interval_seconds ?? 5}
                onChange={(e) => setTelemetryForm((p: any) => ({ ...p, flush_interval_seconds: Number(e.target.value) }))}
              />
            </label>
            <label className="toggle">
              <input
                type="checkbox"
                checked={!!telemetryForm.compression_enabled}
                onChange={(e) => setTelemetryForm((p: any) => ({ ...p, compression_enabled: e.target.checked }))}
              />
              <span>Сжатие файлов</span>
            </label>
            <label className="toggle">
              <input
                type="checkbox"
                checked={!!telemetryForm.collect_detailed_spans}
                onChange={(e) => setTelemetryForm((p: any) => ({ ...p, collect_detailed_spans: e.target.checked }))}
              />
              <span>Детальные спаны</span>
            </label>
            <label className="toggle">
              <input
                type="checkbox"
                checked={!!telemetryForm.collect_system_metrics}
                onChange={(e) => setTelemetryForm((p: any) => ({ ...p, collect_system_metrics: e.target.checked }))}
              />
              <span>Системные метрики</span>
            </label>
            <label className="toggle">
              <input
                type="checkbox"
                checked={!!telemetryForm.collect_memory_metrics}
                onChange={(e) => setTelemetryForm((p: any) => ({ ...p, collect_memory_metrics: e.target.checked }))}
              />
              <span>Метрики памяти</span>
            </label>
            <label className="toggle">
              <input
                type="checkbox"
                checked={!!telemetryForm.collect_performance_metrics}
                onChange={(e) => setTelemetryForm((p: any) => ({ ...p, collect_performance_metrics: e.target.checked }))}
              />
              <span>Метрики производительности</span>
            </label>
            <label className="toggle">
              <input
                type="checkbox"
                checked={!!telemetryForm.collect_error_details}
                onChange={(e) => setTelemetryForm((p: any) => ({ ...p, collect_error_details: e.target.checked }))}
              />
              <span>Детали ошибок</span>
            </label>
            <label className="toggle">
              <input
                type="checkbox"
                checked={!!telemetryForm.collect_user_interactions}
                onChange={(e) => setTelemetryForm((p: any) => ({ ...p, collect_user_interactions: e.target.checked }))}
              />
              <span>Взаимодействия пользователя</span>
            </label>
          </div>
          <div className="button-row">
            <button className="button" type="button" onClick={handleTelemetrySave} disabled={isBusy}>
              Сохранить
            </button>
          </div>
        </div>
      ) : null}

      {tab === "security" ? (
        <div className="stack">
          <div className="card">
            <div className="card-title">Основные настройки</div>
            <div className="form-grid">
              <label className="toggle">
                <input
                  type="checkbox"
                  checked={!!securityForm.sql_execution_enabled}
                  onChange={(e) => setSecurityForm((p: any) => ({ ...p, sql_execution_enabled: e.target.checked }))}
                />
                <span>Разрешить SQL</span>
              </label>
              <label className="field">
                <span className="label">Уровень безопасности</span>
                <select value={securityForm.safety_level ?? "strict"} onChange={(e) => setSecurityForm((p: any) => ({ ...p, safety_level: e.target.value }))}>
                  <option value="strict">strict</option>
                  <option value="moderate">moderate</option>
                  <option value="permissive">permissive</option>
                </select>
              </label>
              <label className="field">
                <span className="label">Макс. строк</span>
                <input type="number" value={securityForm.max_sql_rows ?? 1000} onChange={(e) => setSecurityForm((p: any) => ({ ...p, max_sql_rows: Number(e.target.value) }))} />
              </label>
              <label className="field">
                <span className="label">Таймаут (сек)</span>
                <input
                  type="number"
                  value={securityForm.query_timeout_seconds ?? 30}
                  onChange={(e) => setSecurityForm((p: any) => ({ ...p, query_timeout_seconds: Number(e.target.value) }))}
                />
              </label>
            </div>
          </div>

          <div className="card">
            <div className="card-title">Разрешенные операции</div>
            <div className="toggle-grid">
              {["SELECT", "INSERT", "UPDATE", "DELETE"].map((op) => (
                <label key={op} className="toggle">
                  <input
                    type="checkbox"
                    checked={allowedOperations.includes(op)}
                    onChange={(e) =>
                      setSecurityForm((prev: any) => ({
                        ...prev,
                        allowed_sql_operations: e.target.checked
                          ? Array.from(new Set([...(prev.allowed_sql_operations ?? []), op]))
                          : (prev.allowed_sql_operations ?? []).filter((value: string) => value !== op),
                      }))
                    }
                  />
                  <span>{op}</span>
                </label>
              ))}
            </div>
          </div>

          <div className="card">
            <div className="card-title">Ограничения</div>
            <div className="form-grid">
              <label className="field">
                <span className="label">Blocked keywords</span>
                <textarea
                  value={Array.isArray(securityForm.blocked_sql_keywords) ? securityForm.blocked_sql_keywords.join("\n") : ""}
                  onChange={(e) => updateSecurityList("blocked_sql_keywords", e.target.value)}
                />
              </label>
              <label className="field">
                <span className="label">Allowed schemas</span>
                <textarea
                  value={Array.isArray(securityForm.allowed_schemas) ? securityForm.allowed_schemas.join("\n") : ""}
                  onChange={(e) => updateSecurityList("allowed_schemas", e.target.value)}
                />
              </label>
              <label className="field">
                <span className="label">Whitelist таблиц</span>
                <textarea
                  value={Array.isArray(securityForm.table_whitelist) ? securityForm.table_whitelist.join("\n") : ""}
                  onChange={(e) => updateSecurityList("table_whitelist", e.target.value)}
                />
              </label>
              <label className="field">
                <span className="label">Blacklist таблиц</span>
                <textarea
                  value={Array.isArray(securityForm.table_blacklist) ? securityForm.table_blacklist.join("\n") : ""}
                  onChange={(e) => updateSecurityList("table_blacklist", e.target.value)}
                />
              </label>
            </div>
          </div>

          <div className="card">
            <div className="card-title">PII и аудит</div>
            <div className="form-grid">
              <label className="toggle">
                <input
                  type="checkbox"
                  checked={!!securityForm.enable_pii_detection}
                  onChange={(e) => setSecurityForm((p: any) => ({ ...p, enable_pii_detection: e.target.checked }))}
                />
                <span>PII detection</span>
              </label>
              <label className="field">
                <span className="label">PII action</span>
                <select value={securityForm.pii_action ?? "block"} onChange={(e) => setSecurityForm((p: any) => ({ ...p, pii_action: e.target.value }))}>
                  <option value="block">block</option>
                  <option value="mask">mask</option>
                  <option value="warn">warn</option>
                </select>
              </label>
              <label className="toggle">
                <input
                  type="checkbox"
                  checked={!!securityForm.log_security_events}
                  onChange={(e) => setSecurityForm((p: any) => ({ ...p, log_security_events: e.target.checked }))}
                />
                <span>Логировать события</span>
              </label>
              <label className="toggle">
                <input
                  type="checkbox"
                  checked={!!securityForm.audit_all_queries}
                  onChange={(e) => setSecurityForm((p: any) => ({ ...p, audit_all_queries: e.target.checked }))}
                />
                <span>Аудит всех запросов</span>
              </label>
            </div>
            <div className="button-row">
              <button className="button" type="button" onClick={() => updateSection("security", securityForm)} disabled={isBusy}>
                Сохранить
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {tab === "memory" ? (
        <div className="stack">
          <div className="card">
            <div className="card-title">Основные настройки памяти</div>
            <div className="form-grid">
              <label className="toggle">
                <input type="checkbox" checked={!!memoryForm.enabled} onChange={(e) => setMemoryForm((p: any) => ({ ...p, enabled: e.target.checked }))} />
                <span>Включено</span>
              </label>
              <label className="field">
                <span className="label">Тип</span>
                <select value={memoryForm.memory_type ?? "chromadb"} onChange={(e) => setMemoryForm((p: any) => ({ ...p, memory_type: e.target.value }))}>
                  <option value="chromadb">chromadb</option>
                  <option value="sqlite">sqlite</option>
                </select>
              </label>
              <label className="field">
                <span className="label">Max tactical</span>
                <input type="number" value={memoryForm.max_tactical_memories ?? 1000} onChange={(e) => setMemoryForm((p: any) => ({ ...p, max_tactical_memories: Number(e.target.value) }))} />
              </label>
              <label className="field">
                <span className="label">Max strategic</span>
                <input type="number" value={memoryForm.max_strategic_memories ?? 500} onChange={(e) => setMemoryForm((p: any) => ({ ...p, max_strategic_memories: Number(e.target.value) }))} />
              </label>
              <label className="field">
                <span className="label">TTL тактических (ч)</span>
                <input
                  type="number"
                  value={memoryForm.tactical_memory_ttl_hours ?? 24}
                  onChange={(e) => setMemoryForm((p: any) => ({ ...p, tactical_memory_ttl_hours: Number(e.target.value) }))}
                />
              </label>
              <label className="field">
                <span className="label">TTL стратегических (д)</span>
                <input
                  type="number"
                  value={memoryForm.strategic_memory_ttl_days ?? 30}
                  onChange={(e) => setMemoryForm((p: any) => ({ ...p, strategic_memory_ttl_days: Number(e.target.value) }))}
                />
              </label>
            </div>
          </div>

          <div className="card">
            <div className="card-title">Embeddings</div>
            <div className="form-grid">
              <label className="field">
                <span className="label">Модель</span>
                <select value={memoryForm.embedding_model ?? "all-MiniLM-L6-v2"} onChange={(e) => setMemoryForm((p: any) => ({ ...p, embedding_model: e.target.value }))}>
                  <option value="intfloat/multilingual-e5-base">intfloat/multilingual-e5-base</option>
                  <option value="sentence-transformers/all-MiniLM-L6-v2">all-MiniLM-L6-v2</option>
                  <option value="sentence-transformers/all-mpnet-base-v2">all-mpnet-base-v2</option>
                  <option value="text-embedding-ada-002">text-embedding-ada-002</option>
                  <option value="custom">custom</option>
                </select>
              </label>
              {memoryForm.embedding_model === "custom" ? (
                <label className="field">
                  <span className="label">Кастомная модель</span>
                  <input
                    value={memoryForm.custom_embedding_model ?? ""}
                    onChange={(e) => setMemoryForm((p: any) => ({ ...p, custom_embedding_model: e.target.value }))}
                  />
                </label>
              ) : null}
              <label className="field">
                <span className="label">Размерность</span>
                <input
                  type="number"
                  value={memoryForm.embedding_dimensions ?? 384}
                  onChange={(e) => setMemoryForm((p: any) => ({ ...p, embedding_dimensions: Number(e.target.value) }))}
                />
              </label>
              <label className="field">
                <span className="label">K по умолчанию</span>
                <input
                  type="number"
                  value={memoryForm.default_search_k ?? 10}
                  onChange={(e) => setMemoryForm((p: any) => ({ ...p, default_search_k: Number(e.target.value) }))}
                />
              </label>
              <label className="field">
                <span className="label">Порог схожести</span>
                <input
                  type="number"
                  value={memoryForm.similarity_threshold ?? 0.7}
                  onChange={(e) => setMemoryForm((p: any) => ({ ...p, similarity_threshold: Number(e.target.value) }))}
                />
              </label>
              <label className="field">
                <span className="label">Интервал переиндексации (ч)</span>
                <input
                  type="number"
                  value={memoryForm.reindex_interval_hours ?? 24}
                  onChange={(e) => setMemoryForm((p: any) => ({ ...p, reindex_interval_hours: Number(e.target.value) }))}
                />
              </label>
            </div>
          </div>

          <div className="card">
            <div className="card-title">ChromaDB</div>
            <div className="form-grid">
              <label className="field">
                <span className="label">Путь</span>
                <input value={memoryForm.chromadb_path ?? ""} onChange={(e) => setMemoryForm((p: any) => ({ ...p, chromadb_path: e.target.value }))} />
              </label>
              <label className="field">
                <span className="label">Collection prefix</span>
                <input
                  value={memoryForm.collection_prefix ?? ""}
                  onChange={(e) => setMemoryForm((p: any) => ({ ...p, collection_prefix: e.target.value }))}
                />
              </label>
              <label className="field">
                <span className="label">Batch size</span>
                <input
                  type="number"
                  value={memoryForm.batch_size ?? 100}
                  onChange={(e) => setMemoryForm((p: any) => ({ ...p, batch_size: Number(e.target.value) }))}
                />
              </label>
              <label className="toggle">
                <input
                  type="checkbox"
                  checked={!!memoryForm.enable_compression}
                  onChange={(e) => setMemoryForm((p: any) => ({ ...p, enable_compression: e.target.checked }))}
                />
                <span>Сжатие</span>
              </label>
            </div>
            <div className="button-row">
              <button className="button" type="button" onClick={() => updateSection("memory", memoryForm)} disabled={isBusy}>
                Сохранить
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {tab === "system" ? (
        <div className="stack">
          <div className="card">
            <div className="card-title">Логирование</div>
            <div className="form-grid">
              <label className="field">
                <span className="label">Уровень</span>
                <select value={loggingForm.level ?? "INFO"} onChange={(e) => setLoggingForm((p: any) => ({ ...p, level: e.target.value }))}>
                  <option value="DEBUG">DEBUG</option>
                  <option value="INFO">INFO</option>
                  <option value="WARNING">WARNING</option>
                  <option value="ERROR">ERROR</option>
                </select>
              </label>
              <label className="field">
                <span className="label">Формат</span>
                <select value={loggingForm.format ?? "detailed"} onChange={(e) => setLoggingForm((p: any) => ({ ...p, format: e.target.value }))}>
                  <option value="detailed">detailed</option>
                  <option value="simple">simple</option>
                  <option value="json">json</option>
                </select>
              </label>
              <label className="field">
                <span className="label">Логи (путь)</span>
                <input value={loggingForm.logs_dir ?? ""} onChange={(e) => setLoggingForm((p: any) => ({ ...p, logs_dir: e.target.value }))} />
              </label>
              <label className="field">
                <span className="label">Ротация (MB)</span>
                <input
                  type="number"
                  value={loggingForm.rotation_size_mb ?? 50}
                  onChange={(e) => setLoggingForm((p: any) => ({ ...p, rotation_size_mb: Number(e.target.value) }))}
                />
              </label>
              <label className="field">
                <span className="label">Хранение (дни)</span>
                <input
                  type="number"
                  value={loggingForm.max_age_days ?? 7}
                  onChange={(e) => setLoggingForm((p: any) => ({ ...p, max_age_days: Number(e.target.value) }))}
                />
              </label>
              <label className="toggle">
                <input
                  type="checkbox"
                  checked={!!loggingForm.console_output}
                  onChange={(e) => setLoggingForm((p: any) => ({ ...p, console_output: e.target.checked }))}
                />
                <span>Консоль</span>
              </label>
              <label className="toggle">
                <input
                  type="checkbox"
                  checked={!!loggingForm.file_output}
                  onChange={(e) => setLoggingForm((p: any) => ({ ...p, file_output: e.target.checked }))}
                />
                <span>Файл</span>
              </label>
              <label className="toggle">
                <input
                  type="checkbox"
                  checked={!!loggingForm.unified_logging_enabled}
                  onChange={(e) => setLoggingForm((p: any) => ({ ...p, unified_logging_enabled: e.target.checked }))}
                />
                <span>Unified logging</span>
              </label>
            </div>
            <div className="button-row">
              <button className="button" type="button" onClick={() => updateSection("logging", loggingForm)} disabled={isBusy}>
                Сохранить логирование
              </button>
            </div>
          </div>

          <div className="card">
            <div className="card-title">Система и ресурсы</div>
            <div className="form-grid">
              <label className="field">
                <span className="label">Work dir</span>
                <input value={systemForm.work_directory ?? ""} onChange={(e) => setSystemForm((p: any) => ({ ...p, work_directory: e.target.value }))} />
              </label>
              <label className="field">
                <span className="label">Temp dir</span>
                <input value={systemForm.temp_directory ?? ""} onChange={(e) => setSystemForm((p: any) => ({ ...p, temp_directory: e.target.value }))} />
              </label>
              <label className="field">
                <span className="label">Language</span>
                <select value={systemForm.language ?? "ru"} onChange={(e) => setSystemForm((p: any) => ({ ...p, language: e.target.value }))}>
                  <option value="ru">ru</option>
                  <option value="en">en</option>
                  <option value="auto">auto</option>
                </select>
              </label>
              <label className="field">
                <span className="label">Очистка (ч)</span>
                <input
                  type="number"
                  value={systemForm.cleanup_interval_hours ?? 24}
                  onChange={(e) => setSystemForm((p: any) => ({ ...p, cleanup_interval_hours: Number(e.target.value) }))}
                />
              </label>
            </div>
            <div className="button-row">
              <button className="button" type="button" onClick={() => updateSection("system", systemForm)} disabled={isBusy}>
                Сохранить системные
              </button>
            </div>
          </div>

          <div className="card">
            <div className="card-title">Лимиты ресурсов</div>
            <div className="form-grid">
              <label className="field">
                <span className="label">Max workflows</span>
                <input
                  type="number"
                  value={resourceLimitsForm.max_concurrent_workflows ?? 5}
                  onChange={(e) => setResourceLimitsForm((p: any) => ({ ...p, max_concurrent_workflows: Number(e.target.value) }))}
                />
              </label>
              <label className="field">
                <span className="label">Max agents</span>
                <input
                  type="number"
                  value={resourceLimitsForm.max_concurrent_agents ?? 10}
                  onChange={(e) => setResourceLimitsForm((p: any) => ({ ...p, max_concurrent_agents: Number(e.target.value) }))}
                />
              </label>
              <label className="field">
                <span className="label">Memory (MB)</span>
                <input
                  type="number"
                  value={resourceLimitsForm.memory_limit_mb ?? 2048}
                  onChange={(e) => setResourceLimitsForm((p: any) => ({ ...p, memory_limit_mb: Number(e.target.value) }))}
                />
              </label>
              <label className="field">
                <span className="label">Disk (GB)</span>
                <input
                  type="number"
                  value={resourceLimitsForm.disk_space_limit_gb ?? 50}
                  onChange={(e) => setResourceLimitsForm((p: any) => ({ ...p, disk_space_limit_gb: Number(e.target.value) }))}
                />
              </label>
              <label className="field">
                <span className="label">Timeout (мин)</span>
                <input
                  type="number"
                  value={resourceLimitsForm.execution_timeout_minutes ?? 30}
                  onChange={(e) => setResourceLimitsForm((p: any) => ({ ...p, execution_timeout_minutes: Number(e.target.value) }))}
                />
              </label>
              <label className="field">
                <span className="label">API calls/min</span>
                <input
                  type="number"
                  value={resourceLimitsForm.api_calls_per_minute ?? 60}
                  onChange={(e) => setResourceLimitsForm((p: any) => ({ ...p, api_calls_per_minute: Number(e.target.value) }))}
                />
              </label>
            </div>
            <div className="button-row">
              <button className="button" type="button" onClick={() => updateSection("resource_limits", resourceLimitsForm)} disabled={isBusy}>
                Сохранить лимиты
              </button>
            </div>
          </div>

          <div className="card">
            <div className="card-title">Производительность</div>
            <div className="form-grid">
              <label className="field">
                <span className="label">Worker threads</span>
                <input
                  type="number"
                  value={performanceForm.worker_threads ?? 4}
                  onChange={(e) => setPerformanceForm((p: any) => ({ ...p, worker_threads: Number(e.target.value) }))}
                />
              </label>
              <label className="field">
                <span className="label">Очередь задач</span>
                <input
                  type="number"
                  value={performanceForm.task_queue_size ?? 1000}
                  onChange={(e) => setPerformanceForm((p: any) => ({ ...p, task_queue_size: Number(e.target.value) }))}
                />
              </label>
              <label className="toggle">
                <input
                  type="checkbox"
                  checked={!!performanceForm.enable_caching}
                  onChange={(e) => setPerformanceForm((p: any) => ({ ...p, enable_caching: e.target.checked }))}
                />
                <span>Кэширование</span>
              </label>
              <label className="field">
                <span className="label">Cache size (MB)</span>
                <input
                  type="number"
                  value={performanceForm.cache_size_mb ?? 256}
                  onChange={(e) => setPerformanceForm((p: any) => ({ ...p, cache_size_mb: Number(e.target.value) }))}
                />
              </label>
            </div>
            <div className="button-row">
              <button className="button" type="button" onClick={() => updateSection("performance", performanceForm)} disabled={isBusy}>
                Сохранить производительность
              </button>
            </div>
          </div>

          <div className="card">
            <div className="card-title">Сеть</div>
            <div className="form-grid">
              <label className="field">
                <span className="label">HTTP timeout (сек)</span>
                <input
                  type="number"
                  value={networkForm.http_timeout_seconds ?? 30}
                  onChange={(e) => setNetworkForm((p: any) => ({ ...p, http_timeout_seconds: Number(e.target.value) }))}
                />
              </label>
              <label className="field">
                <span className="label">Max retries</span>
                <input type="number" value={networkForm.max_retries ?? 3} onChange={(e) => setNetworkForm((p: any) => ({ ...p, max_retries: Number(e.target.value) }))} />
              </label>
              <label className="field">
                <span className="label">User agent</span>
                <input value={networkForm.user_agent ?? ""} onChange={(e) => setNetworkForm((p: any) => ({ ...p, user_agent: e.target.value }))} />
              </label>
              <label className="field">
                <span className="label">Proxy URL</span>
                <input value={networkForm.proxy_url ?? ""} onChange={(e) => setNetworkForm((p: any) => ({ ...p, proxy_url: e.target.value }))} />
              </label>
            </div>
            <div className="button-row">
              <button className="button" type="button" onClick={() => updateSection("network", networkForm)} disabled={isBusy}>
                Сохранить сеть
              </button>
            </div>
          </div>
        </div>
      ) : null}
      {status ? <div className="card-description">Статус: {status}</div> : null}
    </div>
  );
}

export function TelemetrySection({ runServiceAction, isBusy }: ActionCardProps) {
  const [tab, setTab] = useState<"list" | "filter" | "analytics" | "settings" | "logs">("list");
  const [traces, setTraces] = useState<any[]>([]);
  const [selected, setSelected] = useState<any | null>(null);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [traceEvents, setTraceEvents] = useState<any | null>(null);
  const [spanLogs, setSpanLogs] = useState<any | null>(null);
  const [runLogs, setRunLogs] = useState<any[]>([]);
  const [logsModal, setLogsModal] = useState<{ open: boolean; runId: string | null }>({ open: false, runId: null });
  const [logsModalAutoRefresh, setLogsModalAutoRefresh] = useState(true);
  const [filter, setFilter] = useState<any>({});
  const [analytics, setAnalytics] = useState<any | null>(null);
  const [analyticsDetail, setAnalyticsDetail] = useState<any | null>(null);
  const [analyticsPeriod, setAnalyticsPeriod] = useState("7d");
  const [autoRefresh, setAutoRefresh] = useState(false);
  const [telemetryEnabled, setTelemetryEnabled] = useState<boolean | null>(null);
  const [telemetryConfig, setTelemetryConfig] = useState<any | null>(null);
  const [reportError, setReportError] = useState<string | null>(null);
  const [telemetryError, setTelemetryError] = useState<string | null>(null);
  const [reportCache, setReportCache] = useState<Record<string, any>>({});
  const [isMounted, setIsMounted] = useState(false);
  const mountedRef = useRef(false);
  const logsModalRunIdRef = useRef<string | null>(null);
  const tracesInFlightRef = useRef(false);
  const runLogsInFlightRef = useRef<Set<string>>(new Set());

  const loadTraces = useCallback(async () => {
    if (tracesInFlightRef.current) return;
    tracesInFlightRef.current = true;
    setTelemetryError(null);
    try {
      const resp = await runServiceAction("telemetry.list_traces", {});
      const items = (resp as any)?.traces ?? [];
      if (!mountedRef.current) return;
      setTraces(Array.isArray(items) ? items.filter((trace) => trace?.run_id !== "unknown") : []);
    } catch (err) {
      setTelemetryError(err instanceof Error ? err.message : "Не удалось загрузить трассы");
    } finally {
      tracesInFlightRef.current = false;
    }
  }, [runServiceAction]);

  const applyFilter = async () => {
    setTelemetryError(null);
    try {
      const resp = await runServiceAction("telemetry.filter_traces", filter);
      setTraces((resp as any)?.traces ?? []);
    } catch (err) {
      setTelemetryError(err instanceof Error ? err.message : "Не удалось применить фильтр");
    }
  };

  const loadTraceDetails = async (runId: string) => {
    setTelemetryError(null);
    try {
      const resp = await runServiceAction("telemetry.trace_file", { run_id: runId });
      setSelected((resp as any)?.trace ?? resp);
      setSelectedRunId(runId);
      setSpanLogs(null);
    } catch (err) {
      setTelemetryError(err instanceof Error ? err.message : "Не удалось загрузить детали трассы");
    }
  };
  const findReportEvent = (events: any[]) => {
    for (const ev of events) {
      const name = (ev?.name ?? "").toLowerCase();
      if (name === "report_generated") {
        const attrs = ev?.attributes ?? {};
        const b64 = attrs["report.content_b64_gzip"] ?? attrs["report_b64_gzip"];
        if (b64) {
          return { attrs, base64_gzip: b64 };
        }
      }
    }
    return null;
  };

  const loadTraceEvents = async (runId: string) => {
    setTelemetryError(null);
    try {
      const resp = await runServiceAction("telemetry.trace_events", { run_id: runId });
      const events = (resp as any)?.events ?? resp;
      setTraceEvents(events);
      if (Array.isArray(events)) {
        const report = findReportEvent(events);
        if (report) {
          setReportCache((prev) => ({ ...prev, [runId]: report }));
        }
      }
    } catch (err) {
      setTelemetryError(err instanceof Error ? err.message : "Не удалось загрузить события трассы");
    }
  };
  const loadSpanLogs = async (runId: string, spanId: string) => {
    setTelemetryError(null);
    try {
      const resp = await runServiceAction("logs.span_logs", { run_id: runId, span_id: spanId });
      setSpanLogs((resp as any)?.logs ?? resp);
    } catch (err) {
      setTelemetryError(err instanceof Error ? err.message : "Не удалось загрузить логи спана");
    }
  };
  const loadRunLogs = useCallback(async (runId: string) => {
    if (runLogsInFlightRef.current.has(runId)) return;
    runLogsInFlightRef.current.add(runId);
    try {
      const resp = await runServiceAction("logs.run_logs", { run_id: runId, limit: 20000 });
      const items = (resp as any)?.logs ?? resp;
      if (!mountedRef.current || logsModalRunIdRef.current !== runId) return;
      setRunLogs(Array.isArray(items) ? items : []);
    } finally {
      runLogsInFlightRef.current.delete(runId);
    }
  }, [runServiceAction]);
  const handleGenerateReport = async (runId: string) => {
    setReportError(null);
    try {
      if (reportCache[runId]) {
        await openReportFromPayload(runId, reportCache[runId]);
        return;
      }
      const traceResp = await runServiceAction("telemetry.trace_events", { run_id: runId });
      const events = (traceResp as any)?.events ?? traceResp;
      if (Array.isArray(events)) {
        const cached = findReportEvent(events);
        if (cached) {
          setReportCache((prev) => ({ ...prev, [runId]: cached }));
          await openReportFromPayload(runId, cached);
          return;
        }
      }
      const resp = await runServiceAction("telemetry.generate_report", { run_id: runId });
      const report = (resp as any)?.report ?? resp;
      setReportCache((prev) => ({ ...prev, [runId]: report }));
      await openReportFromPayload(runId, report);
    } catch (err) {
      setReportError(err instanceof Error ? err.message : "Не удалось сформировать отчёт");
    }
  };

  const loadTelemetrySettings = useCallback(async () => {
    const resp = await runServiceAction("config.get", {});
    const cfg = (resp as any)?.config ?? resp;
    setTelemetryConfig(cfg?.telemetry ?? null);
    setTelemetryEnabled(!!cfg?.telemetry?.enabled);
  }, [runServiceAction]);

  const handleTelemetryToggle = async () => {
    if (telemetryEnabled) {
      await runServiceAction("telemetry.disable", {});
      setTelemetryEnabled(false);
    } else {
      await runServiceAction("telemetry.enable", {});
      setTelemetryEnabled(true);
    }
  };

  useEffect(() => {
    void loadTraces();
  }, [loadTraces]);

  useEffect(() => {
    if (!autoRefresh) return;
    const id = window.setInterval(() => void loadTraces(), 5000);
    return () => window.clearInterval(id);
  }, [autoRefresh, loadTraces]);

  useEffect(() => {
    if (tab === "settings") {
      void loadTelemetrySettings();
    }
  }, [tab, loadTelemetrySettings]);

  useEffect(() => {
    mountedRef.current = true;
    setIsMounted(true);
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    logsModalRunIdRef.current = logsModal.open ? logsModal.runId : null;
  }, [logsModal.open, logsModal.runId]);

  useEffect(() => {
    if (!isMounted) return;
    if (!logsModal.open) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [isMounted, logsModal.open]);

  useEffect(() => {
    if (!logsModal.open || !logsModal.runId) return;
    if (!logsModalAutoRefresh) return;
    void loadRunLogs(logsModal.runId);
    const intervalId = window.setInterval(() => {
      void loadRunLogs(logsModal.runId!);
    }, 4000);
    return () => window.clearInterval(intervalId);
  }, [logsModal.open, logsModal.runId, logsModalAutoRefresh, loadRunLogs]);

  const formattedRunLogs = runLogs
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

  return (
    <div className="section" id="telemetry">
      <div className="section-header">
        <div className="section-title">Телеметрия</div>
        <div className="section-hint">Трассы и события</div>
      </div>
      <div className="segment-row">
        <button className={`segment-button${tab === "list" ? " active" : ""}`} onClick={() => setTab("list")}>
          Трассы
        </button>
        <button className={`segment-button${tab === "filter" ? " active" : ""}`} onClick={() => setTab("filter")}>
          Фильтры
        </button>
        <button className={`segment-button${tab === "analytics" ? " active" : ""}`} onClick={() => setTab("analytics")}>
          Аналитика
        </button>
        <button className={`segment-button${tab === "logs" ? " active" : ""}`} onClick={() => setTab("logs")}>
          Логи
        </button>
        <button className={`segment-button${tab === "settings" ? " active" : ""}`} onClick={() => setTab("settings")}>
          Настройки
        </button>
      </div>

      {tab === "list" ? (
        <div className="stack">
          <div className="button-row">
            <button className="button secondary" type="button" onClick={loadTraces} disabled={isBusy}>
              Обновить
            </button>
            <label className="toggle">
              <input type="checkbox" checked={autoRefresh} onChange={(e) => setAutoRefresh(e.target.checked)} />
              <span>Авто (5s)</span>
            </label>
          </div>
          <div className="cards profile-grid">
            {traces.map((trace, idx) => (
              <article key={trace.run_id ?? idx} className="card">
                <div className="inline">
                  <div className="card-title">{trace.run_id ?? "trace"}</div>
                  <span className="status-tag" data-status={trace.status ?? "unknown"}>
                    {trace.status ?? "unknown"}
                  </span>
                </div>
                <div className="card-description">Событий: {trace.events_count ?? "—"}</div>
                <div className="profile-meta">
                  <div>
                    <span className="label">Время</span>
                    <div className="meta-value">{trace.modified_time ?? "—"}</div>
                  </div>
                  <div>
                    <span className="label">Длительность</span>
                    <div className="meta-value">{trace.duration_ms ?? "—"} ms</div>
                  </div>
                </div>
                <div className="button-row">
                  <button className="button ghost" type="button" onClick={() => loadTraceDetails(trace.run_id)} disabled={isBusy}>
                    Детали
                  </button>
                  <button className="button secondary" type="button" onClick={() => loadTraceEvents(trace.run_id)} disabled={isBusy}>
                    События
                  </button>
                  <button
                    className="button secondary"
                    type="button"
                    onClick={async () => {
                      setLogsModal({ open: true, runId: trace.run_id });
                      await loadRunLogs(trace.run_id);
                    }}
                    disabled={isBusy}
                  >
                    Логи
                  </button>
                  <button className="button secondary" type="button" onClick={() => handleGenerateReport(trace.run_id)} disabled={isBusy}>
                    Отчёт
                  </button>
                </div>
              </article>
            ))}
            {traces.length === 0 ? <div className="card-description">Нет трасс.</div> : null}
          </div>
          {reportError ? <div className="card-description">Ошибка отчёта: {reportError}</div> : null}
          {telemetryError ? <div className="card-description">Ошибка: {telemetryError}</div> : null}
          {selected ? (
            <div className="card">
              <div className="card-title">Детали трассы</div>
              {Array.isArray(selected.spans) ? (
                <div className="cards profile-grid">
                  {selected.spans.map((span: any, idx: number) => (
                    <article key={span.span_id ?? idx} className="card">
                      <div className="inline">
                        <div className="card-title">{span.name ?? span.span_name ?? "span"}</div>
                        <span className="status-tag" data-status={span.status?.status_code ?? span.status ?? "unknown"}>
                          {span.status?.status_code ?? span.status ?? "unknown"}
                        </span>
                      </div>
                      <div className="profile-meta">
                        <div>
                          <span className="label">Span ID</span>
                          <div className="meta-value">{span.span_id ?? "—"}</div>
                        </div>
                        <div>
                          <span className="label">Длительность</span>
                          <div className="meta-value">{span.duration_ms ?? span.duration_seconds ?? "—"}</div>
                        </div>
                      </div>
                      {span.attributes ? (
                        <details className="details">
                          <summary>Атрибуты</summary>
                          <KeyValueList data={span.attributes} />
                        </details>
                      ) : null}
                      {selectedRunId && span.span_id ? (
                        <div className="button-row">
                          <button className="button ghost" type="button" onClick={() => loadSpanLogs(selectedRunId, span.span_id)} disabled={isBusy}>
                            Логи спана
                          </button>
                        </div>
                      ) : null}
                    </article>
                  ))}
                </div>
              ) : (
                <KeyValueList data={selected} />
              )}
            </div>
          ) : null}
          {spanLogs ? (
            <div className="card">
              <div className="card-title">Логи выбранного спана</div>
              <KeyValueList data={spanLogs} />
            </div>
          ) : null}
          {traceEvents ? (
            <div className="card">
              <div className="card-title">События</div>
              <KeyValueList data={traceEvents} />
            </div>
          ) : null}
        </div>
      ) : null}

      {tab === "filter" ? (
        <div className="card">
          <div className="card-title">Фильтр трасс</div>
          <div className="form-grid">
            <label className="field">
              <span className="label">Дата от</span>
              <input value={filter.date_from ?? ""} onChange={(e) => setFilter((p: any) => ({ ...p, date_from: e.target.value }))} placeholder="2024-01-01" />
            </label>
            <label className="field">
              <span className="label">Дата до</span>
              <input value={filter.date_to ?? ""} onChange={(e) => setFilter((p: any) => ({ ...p, date_to: e.target.value }))} placeholder="2024-01-07" />
            </label>
            <label className="field">
              <span className="label">Run ID</span>
              <input value={filter.run_id_filter ?? ""} onChange={(e) => setFilter((p: any) => ({ ...p, run_id_filter: e.target.value }))} />
            </label>
            <label className="field">
              <span className="label">Agent</span>
              <input value={filter.agent_filter ?? ""} onChange={(e) => setFilter((p: any) => ({ ...p, agent_filter: e.target.value }))} />
            </label>
            <label className="field">
              <span className="label">Status</span>
              <input value={filter.status_filter ?? ""} onChange={(e) => setFilter((p: any) => ({ ...p, status_filter: e.target.value }))} />
            </label>
            <label className="field">
              <span className="label">Мин. спанов</span>
              <input type="number" value={filter.min_spans ?? 0} onChange={(e) => setFilter((p: any) => ({ ...p, min_spans: Number(e.target.value) }))} />
            </label>
            <label className="field">
              <span className="label">Макс. спанов</span>
              <input type="number" value={filter.max_spans ?? 10000} onChange={(e) => setFilter((p: any) => ({ ...p, max_spans: Number(e.target.value) }))} />
            </label>
            <label className="field">
              <span className="label">Мин. длительность (мс)</span>
              <input type="number" value={filter.min_duration_ms ?? 0} onChange={(e) => setFilter((p: any) => ({ ...p, min_duration_ms: Number(e.target.value) }))} />
            </label>
            <label className="field">
              <span className="label">Макс. длительность (мс)</span>
              <input type="number" value={filter.max_duration_ms ?? 604800000} onChange={(e) => setFilter((p: any) => ({ ...p, max_duration_ms: Number(e.target.value) }))} />
            </label>
            <label className="field">
              <span className="label">Имя спана</span>
              <input value={filter.span_name_filter ?? ""} onChange={(e) => setFilter((p: any) => ({ ...p, span_name_filter: e.target.value }))} />
            </label>
            <label className="field">
              <span className="label">Фильтр атрибутов</span>
              <input value={filter.attribute_filter ?? ""} onChange={(e) => setFilter((p: any) => ({ ...p, attribute_filter: e.target.value }))} />
            </label>
            <label className="field">
              <span className="label">Операция</span>
              <input value={filter.operation_filter ?? ""} onChange={(e) => setFilter((p: any) => ({ ...p, operation_filter: e.target.value }))} />
            </label>
            <label className="field">
              <span className="label">Текст ошибки</span>
              <input value={filter.error_text_filter ?? ""} onChange={(e) => setFilter((p: any) => ({ ...p, error_text_filter: e.target.value }))} />
            </label>
          </div>
          <div className="toggle-grid">
            <label className="toggle">
              <input type="checkbox" checked={!!filter.use_regex} onChange={(e) => setFilter((p: any) => ({ ...p, use_regex: e.target.checked }))} />
              <span>Regex</span>
            </label>
            <label className="toggle">
              <input
                type="checkbox"
                checked={filter.show_only_root_spans ?? false}
                onChange={(e) => setFilter((p: any) => ({ ...p, show_only_root_spans: e.target.checked }))}
              />
              <span>Только root spans</span>
            </label>
            <label className="toggle">
              <input
                type="checkbox"
                checked={filter.include_nested_spans ?? true}
                onChange={(e) => setFilter((p: any) => ({ ...p, include_nested_spans: e.target.checked }))}
              />
              <span>Включать nested</span>
            </label>
            <label className="toggle">
              <input
                type="checkbox"
                checked={filter.sort_by_duration ?? false}
                onChange={(e) => setFilter((p: any) => ({ ...p, sort_by_duration: e.target.checked }))}
              />
              <span>Сортировать по длительности</span>
            </label>
          </div>
          <div className="button-row">
            <button className="button ghost" type="button" onClick={() => setFilter({ status_filter: "С ошибками" })} disabled={isBusy}>
              Только ошибки
            </button>
            <button
              className="button ghost"
              type="button"
              onClick={() => setFilter({ min_duration_ms: 30000, sort_by_duration: true })}
              disabled={isBusy}
            >
              Долгие &gt; 30с
            </button>
            <button
              className="button ghost"
              type="button"
              onClick={() => setFilter({ span_name_filter: "agent", use_regex: true })}
              disabled={isBusy}
            >
              Агентские
            </button>
            <button className="button ghost" type="button" onClick={() => setFilter({})} disabled={isBusy}>
              Сбросить фильтры
            </button>
          </div>
          <div className="button-row">
            <button className="button" type="button" onClick={applyFilter} disabled={isBusy}>
              Применить фильтр
            </button>
            <button className="button secondary" type="button" onClick={() => runServiceAction("telemetry.mark_incomplete", {})} disabled={isBusy}>
              Пометить incomplete
            </button>
            <button className="button ghost" type="button" onClick={() => runServiceAction("telemetry.cleanup", { max_age_days: 7 })} disabled={isBusy}>
              Очистка 7 дней
            </button>
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

      {tab === "analytics" ? (
        <div className="card">
          <div className="section-header">
            <div className="card-title">Аналитика</div>
          </div>
          <div className="form-grid">
            <label className="field">
              <span className="label">Период</span>
              <select value={analyticsPeriod} onChange={(e) => setAnalyticsPeriod(e.target.value)}>
                <option value="1d">Последние 24 часа</option>
                <option value="7d">Последние 7 дней</option>
                <option value="30d">Последние 30 дней</option>
                <option value="all">Все время</option>
              </select>
            </label>
          </div>
          <div className="button-row">
            <button
              className="button secondary"
              type="button"
              onClick={async () => {
                const days = analyticsPeriod === "1d" ? 1 : analyticsPeriod === "30d" ? 30 : analyticsPeriod === "all" ? 3650 : 7;
                const resp = await runServiceAction("telemetry.analytics", { days });
                setAnalytics((resp as any)?.result ?? resp);
                const listResp = await runServiceAction("telemetry.list_traces", {});
                const items = (listResp as any)?.traces ?? [];
                const cutoff = analyticsPeriod === "all" ? 0 : Date.now() - days * 24 * 60 * 60 * 1000;
                const filtered = items.filter((item: any) => {
                  const ts = item.modified_time ? Date.parse(item.modified_time) : 0;
                  return analyticsPeriod === "all" ? true : ts >= cutoff;
                });
                setAnalyticsDetail({ filteredCount: filtered.length, recent: filtered.slice(0, 10) });
              }}
              disabled={isBusy}
            >
              Построить аналитику
            </button>
            <button className="button ghost" type="button" onClick={() => runServiceAction("telemetry.export", { format: "json" })} disabled={isBusy}>
              Экспорт
            </button>
          </div>
          {analytics ? (
            <div className="stack">
              <div className="profile-meta">
                <div>
                  <span className="label">Трасс</span>
                  <div className="meta-value">{analytics.trace_count ?? "—"}</div>
                </div>
                <div>
                  <span className="label">Ошибок</span>
                  <div className="meta-value">{analytics.error_count ?? "—"}</div>
                </div>
                <div>
                  <span className="label">Средняя длительность</span>
                  <div className="meta-value">{analytics.avg_duration_ms ?? "—"} ms</div>
                </div>
              </div>
              {analytics.operations?.length ? (
                <details className="details">
                  <summary>Операции</summary>
                  <div className="graph-inputs">
                    {analytics.operations.map((op: any) => (
                      <div key={op.name} className="graph-input">
                        <div className="label">{op.name}</div>
                        <div className="meta-value">{op.count}</div>
                      </div>
                    ))}
                  </div>
                </details>
              ) : null}
            </div>
          ) : null}
          {analyticsDetail?.recent?.length ? (
            <details className="details">
              <summary>Последние трассы</summary>
              <div className="cards profile-grid">
                {analyticsDetail.recent.map((item: any) => (
                  <article key={item.run_id ?? item.modified_time} className="card">
                    <div className="card-title">{item.run_id ?? "trace"}</div>
                    <div className="card-description">Событий: {item.events_count ?? "—"}</div>
                  </article>
                ))}
              </div>
            </details>
          ) : null}
        </div>
      ) : null}

      {tab === "settings" ? (
        <div className="card">
          <div className="card-title">Настройки телеметрии</div>
          <div className="profile-meta">
            <div>
              <span className="label">Статус</span>
              <div className="meta-value">{telemetryEnabled === null ? "—" : telemetryEnabled ? "Включена" : "Отключена"}</div>
            </div>
            <div>
              <span className="label">Retention</span>
              <div className="meta-value">{telemetryConfig?.trace_retention_days ?? "—"} дн.</div>
            </div>
          </div>
          <div className="button-row">
            <button className="button secondary" type="button" onClick={handleTelemetryToggle} disabled={isBusy}>
              {telemetryEnabled ? "Отключить" : "Включить"}
            </button>
            <button className="button ghost" type="button" onClick={() => runServiceAction("telemetry.cleanup", { max_age_days: 7 })} disabled={isBusy}>
              Очистить (7 дней)
            </button>
            <button className="button ghost" type="button" onClick={() => runServiceAction("telemetry.mark_incomplete", {})} disabled={isBusy}>
              Пометить незавершенные
            </button>
          </div>
        </div>
      ) : null}

      {tab === "logs" ? <LogsSection runServiceAction={runServiceAction} isBusy={isBusy} /> : null}
    </div>
  );
}

export function LogsSection({ runServiceAction, isBusy }: ActionCardProps) {
  const [tab, setTab] = useState<"search" | "files" | "run" | "analytics">("search");
  const [search, setSearch] = useState({
    query: "",
    level: "",
    limit: 200,
    use_regex: false,
    case_sensitive: false,
    start_time: "",
    end_time: "",
    invert_search: false,
    logger_name: "",
    run_id: "",
    span_id: "",
  });
  const [files, setFiles] = useState<any[]>([]);
  const [fileName, setFileName] = useState("");
  const [fileContent, setFileContent] = useState<any | null>(null);
  const [fileSearch, setFileSearch] = useState({
    query: "",
    level: "",
    limit: 500,
    use_regex: false,
    case_sensitive: false,
    start_time: "",
    end_time: "",
    invert_search: false,
    context_lines: 0,
  });
  const [searchResults, setSearchResults] = useState<any | null>(null);
  const [runId, setRunId] = useState("");
  const [spanId, setSpanId] = useState("");
  const [runLogs, setRunLogs] = useState<any | null>(null);
  const [spanLogs, setSpanLogs] = useState<any | null>(null);
  const [analytics, setAnalytics] = useState<any | null>(null);

  const loadFiles = useCallback(async () => {
    const resp = await runServiceAction("logs.files", {});
    setFiles((resp as any)?.files ?? []);
  }, [runServiceAction]);

  const searchLogs = async () => {
    const resp = await runServiceAction("logs.search", search);
    setSearchResults((resp as any)?.logs ?? resp);
  };

  const loadFileContent = async () => {
    const resp = await runServiceAction("logs.file_search", { filename: fileName, ...fileSearch });
    setFileContent((resp as any)?.logs ?? resp);
  };

  const loadRunLogs = async () => {
    const resp = await runServiceAction("logs.run_logs", { run_id: runId, limit: 500 });
    setRunLogs((resp as any)?.logs ?? resp);
  };

  const loadSpanLogs = async () => {
    const resp = await runServiceAction("logs.span_logs", { run_id: runId, span_id: spanId });
    setSpanLogs((resp as any)?.logs ?? resp);
  };

  const loadAnalytics = async () => {
    const resp = await runServiceAction("logs.analytics", { max_files: 20 });
    setAnalytics((resp as any)?.result ?? resp);
  };

  const normalizeLogs = (raw: any) => {
    if (!raw) return [];
    if (Array.isArray(raw)) return raw;
    if (Array.isArray(raw.logs)) return raw.logs;
    return [];
  };

  const renderLogs = (raw: any) => {
    const items = normalizeLogs(raw);
    if (!items.length) {
      return <div className="card-description">Нет логов.</div>;
    }
    return (
      <div className="cards profile-grid">
        {items.map((entry: any, idx: number) => (
          <article key={`${entry.timestamp ?? idx}-${entry.message ?? idx}`} className="card">
            <div className="inline">
              <div className="card-title">{entry.level ?? "INFO"}</div>
              {entry.__matched ? <span className="status-tag" data-status="matched">match</span> : null}
            </div>
            <div className="card-description">{entry.message ?? "—"}</div>
            <div className="profile-meta">
              <div>
                <span className="label">Время</span>
                <div className="meta-value">{entry.timestamp ?? "—"}</div>
              </div>
              <div>
                <span className="label">Logger</span>
                <div className="meta-value">{entry.logger_name ?? "—"}</div>
              </div>
              <div>
                <span className="label">Run ID</span>
                <div className="meta-value">{entry.run_id ?? "—"}</div>
              </div>
              <div>
                <span className="label">Span ID</span>
                <div className="meta-value">{entry.span_id ?? "—"}</div>
              </div>
            </div>
            {entry.extra_data ? (
              <details className="details">
                <summary>Данные</summary>
                <KeyValueList data={entry.extra_data} />
              </details>
            ) : null}
          </article>
        ))}
      </div>
    );
  };

  useEffect(() => {
    void loadFiles();
  }, [loadFiles]);

  return (
    <div className="section" id="logs">
      <div className="section-header">
        <div className="section-title">Логи</div>
        <div className="section-hint">Поиск и файлы логов</div>
      </div>
      <div className="segment-row">
        <button className={`segment-button${tab === "search" ? " active" : ""}`} onClick={() => setTab("search")}>
          Поиск
        </button>
        <button className={`segment-button${tab === "files" ? " active" : ""}`} onClick={() => setTab("files")}>
          Файлы
        </button>
        <button className={`segment-button${tab === "run" ? " active" : ""}`} onClick={() => setTab("run")}>
          По запуску
        </button>
        <button className={`segment-button${tab === "analytics" ? " active" : ""}`} onClick={() => setTab("analytics")}>
          Аналитика
        </button>
      </div>

      {tab === "search" ? (
        <div className="card">
          <div className="card-title">Поиск по логам</div>
          <div className="form-grid">
            <label className="field">
              <span className="label">Запрос</span>
              <input value={search.query} onChange={(e) => setSearch((p) => ({ ...p, query: e.target.value }))} />
            </label>
            <label className="field">
              <span className="label">Уровень</span>
              <input value={search.level} onChange={(e) => setSearch((p) => ({ ...p, level: e.target.value }))} />
            </label>
            <label className="field">
              <span className="label">Лимит</span>
              <input type="number" value={search.limit} onChange={(e) => setSearch((p) => ({ ...p, limit: Number(e.target.value) }))} />
            </label>
            <label className="field">
              <span className="label">Логгер</span>
              <input value={search.logger_name} onChange={(e) => setSearch((p) => ({ ...p, logger_name: e.target.value }))} />
            </label>
            <label className="field">
              <span className="label">Run ID</span>
              <input value={search.run_id} onChange={(e) => setSearch((p) => ({ ...p, run_id: e.target.value }))} />
            </label>
            <label className="field">
              <span className="label">Span ID</span>
              <input value={search.span_id} onChange={(e) => setSearch((p) => ({ ...p, span_id: e.target.value }))} />
            </label>
            <label className="field">
              <span className="label">Начало (ISO)</span>
              <input value={search.start_time ?? ""} onChange={(e) => setSearch((p: any) => ({ ...p, start_time: e.target.value }))} />
            </label>
            <label className="field">
              <span className="label">Конец (ISO)</span>
              <input value={search.end_time ?? ""} onChange={(e) => setSearch((p: any) => ({ ...p, end_time: e.target.value }))} />
            </label>
          </div>
          <div className="toggle-grid">
            <label className="toggle">
              <input type="checkbox" checked={search.use_regex} onChange={(e) => setSearch((p) => ({ ...p, use_regex: e.target.checked }))} />
              <span>Regex</span>
            </label>
            <label className="toggle">
              <input type="checkbox" checked={search.case_sensitive} onChange={(e) => setSearch((p) => ({ ...p, case_sensitive: e.target.checked }))} />
              <span>Регистр</span>
            </label>
            <label className="toggle">
              <input type="checkbox" checked={search.invert_search} onChange={(e) => setSearch((p) => ({ ...p, invert_search: e.target.checked }))} />
              <span>Инвертировать</span>
            </label>
          </div>
          <div className="button-row">
            <button className="button" type="button" onClick={searchLogs} disabled={isBusy}>
              Искать
            </button>
            <button className="button ghost" type="button" onClick={() => runServiceAction("logs.cleanup", { max_age_days: 7 })} disabled={isBusy}>
              Очистка 7 дней
            </button>
            <button
              className="button ghost"
              type="button"
              onClick={() => setSearch((prev) => ({ ...prev, level: "ERROR" }))}
              disabled={isBusy}
            >
              Только ошибки
            </button>
          </div>
          {searchResults ? renderLogs(searchResults) : null}
        </div>
      ) : null}

      {tab === "files" ? (
        <div className="stack">
          <div className="button-row">
            <button className="button secondary" type="button" onClick={loadFiles} disabled={isBusy}>
              Обновить список
            </button>
          </div>
          <div className="cards profile-grid">
            {files.map((file, idx) => (
              <article key={file.name ?? idx} className="card">
                <div className="card-title">{file.name}</div>
                <div className="card-description">{file.modified_time ?? ""}</div>
                <div className="button-row">
                  <button className="button ghost" type="button" onClick={() => setFileName(file.name)} disabled={isBusy}>
                    Выбрать
                  </button>
                </div>
              </article>
            ))}
          </div>
          {fileName ? (
            <div className="card">
              <div className="card-title">Файл: {fileName}</div>
              <div className="form-grid">
                <label className="field">
                  <span className="label">Запрос</span>
                  <input value={fileSearch.query} onChange={(e) => setFileSearch((p) => ({ ...p, query: e.target.value }))} />
                </label>
                <label className="field">
                  <span className="label">Уровень</span>
                  <input value={fileSearch.level} onChange={(e) => setFileSearch((p) => ({ ...p, level: e.target.value }))} />
                </label>
                <label className="field">
                  <span className="label">Лимит</span>
                  <input type="number" value={fileSearch.limit} onChange={(e) => setFileSearch((p) => ({ ...p, limit: Number(e.target.value) }))} />
                </label>
                <label className="field">
                  <span className="label">Начало (ISO)</span>
                  <input value={fileSearch.start_time ?? ""} onChange={(e) => setFileSearch((p: any) => ({ ...p, start_time: e.target.value }))} />
                </label>
                <label className="field">
                  <span className="label">Конец (ISO)</span>
                  <input value={fileSearch.end_time ?? ""} onChange={(e) => setFileSearch((p: any) => ({ ...p, end_time: e.target.value }))} />
                </label>
                <label className="field">
                  <span className="label">Контекст (строк)</span>
                  <input
                    type="number"
                    value={fileSearch.context_lines}
                    onChange={(e) => setFileSearch((p) => ({ ...p, context_lines: Number(e.target.value) }))}
                  />
                </label>
              </div>
              <div className="toggle-grid">
                <label className="toggle">
                  <input type="checkbox" checked={fileSearch.use_regex} onChange={(e) => setFileSearch((p) => ({ ...p, use_regex: e.target.checked }))} />
                  <span>Regex</span>
                </label>
                <label className="toggle">
                  <input
                    type="checkbox"
                    checked={fileSearch.case_sensitive}
                    onChange={(e) => setFileSearch((p) => ({ ...p, case_sensitive: e.target.checked }))}
                  />
                  <span>Регистр</span>
                </label>
                <label className="toggle">
                  <input
                    type="checkbox"
                    checked={fileSearch.invert_search}
                    onChange={(e) => setFileSearch((p) => ({ ...p, invert_search: e.target.checked }))}
                  />
                  <span>Инвертировать</span>
                </label>
              </div>
              <div className="button-row">
                <button className="button secondary" type="button" onClick={loadFileContent} disabled={isBusy}>
                  Поиск в файле
                </button>
              </div>
              {fileContent ? renderLogs(fileContent) : null}
            </div>
          ) : null}
        </div>
      ) : null}

      {tab === "run" ? (
        <div className="card">
          <div className="card-title">Логи по запуску</div>
          <div className="form-grid">
            <label className="field">
              <span className="label">Run ID</span>
              <input value={runId} onChange={(e) => setRunId(e.target.value)} />
            </label>
            <label className="field">
              <span className="label">Span ID</span>
              <input value={spanId} onChange={(e) => setSpanId(e.target.value)} />
            </label>
          </div>
          <div className="button-row">
            <button className="button secondary" type="button" onClick={loadRunLogs} disabled={isBusy || !runId.trim()}>
              Логи запуска
            </button>
            <button className="button ghost" type="button" onClick={loadSpanLogs} disabled={isBusy || !runId.trim() || !spanId.trim()}>
              Логи спана
            </button>
          </div>
          {runLogs ? renderLogs(runLogs) : null}
          {spanLogs ? renderLogs(spanLogs) : null}
        </div>
      ) : null}

      {tab === "analytics" ? (
        <div className="card">
          <div className="card-title">Аналитика логов</div>
          <div className="button-row">
            <button className="button secondary" type="button" onClick={loadAnalytics} disabled={isBusy}>
              Обновить
            </button>
          </div>
          {analytics ? (
            <div className="stack">
              <div className="profile-meta">
                <div>
                  <span className="label">Всего логов</span>
                  <div className="meta-value">{analytics.total ?? "—"}</div>
                </div>
                <div>
                  <span className="label">Период</span>
                  <div className="meta-value">
                    {analytics.time_range?.start ?? "—"} → {analytics.time_range?.end ?? "—"}
                  </div>
                </div>
              </div>
              {analytics.by_level?.length ? (
                <details className="details">
                  <summary>Уровни</summary>
                  <div className="graph-inputs">
                    {analytics.by_level.map((row: any) => (
                      <div key={row.level} className="graph-input">
                        <div className="label">{row.level}</div>
                        <div className="meta-value">{row.count}</div>
                      </div>
                    ))}
                  </div>
                </details>
              ) : null}
              {analytics.by_logger?.length ? (
                <details className="details">
                  <summary>Топ логгеров</summary>
                  <div className="graph-inputs">
                    {analytics.by_logger.map((row: any) => (
                      <div key={row.logger} className="graph-input">
                        <div className="label">{row.logger}</div>
                        <div className="meta-value">{row.count}</div>
                      </div>
                    ))}
                  </div>
                </details>
              ) : null}
            </div>
          ) : (
            <div className="card-description">Нет данных аналитики.</div>
          )}
        </div>
      ) : null}
    </div>
  );
}

export function ProgressSection({ runServiceAction, isBusy, results, clearResults }: ProgressSectionProps) {
  const progressEvents = results.filter((entry) => entry.action === "service.progress");
  return (
    <div className="section" id="progress">
      <div className="section-header">
        <div className="section-title">Прогресс</div>
        <div className="section-hint">Мониторинг фоновых задач</div>
      </div>
      <div className="cards">
        <ActionCard
          title="Stream progress"
          action="progress.stream"
          onRun={runServiceAction}
          busy={isBusy}
          fields={[
            { name: "duration_seconds", label: "Длительность (сек.)", type: "number", defaultValue: 30 },
          ]}
        />
      </div>
      <div className="section">
        <div className="section-header">
          <div className="section-title">События прогресса</div>
          <button className="button ghost" onClick={clearResults}>
            Очистить
          </button>
        </div>
        <div className="result-list">
          {progressEvents.length === 0 ? (
            <div className="card-description">Пока нет событий прогресса.</div>
          ) : (
            progressEvents.map((entry) => (
              <div key={entry.id} className="result-card">
                <div className="inline">
                  <div className="result-title">{entry.action}</div>
                  <span className="tag">{entry.status}</span>
                  <span className="app-subtitle">{entry.timestamp}</span>
                </div>
                <KeyValueList data={entry.data} />
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}

export function ToolsSection({ runServiceAction, isBusy }: ActionCardProps) {
  const [isMounted, setIsMounted] = useState(false);
  const [tab, setTab] = useState<"diagram" | "images" | "utilities" | "tools" | "agent">("diagram");
  const [diagramTab, setDiagramTab] = useState<"ai" | "editor">("ai");
  const [imageTab, setImageTab] = useState<"generate" | "edit" | "analyze">("generate");
  const [diagramPrompt, setDiagramPrompt] = useState("");
  const [diagramCode, setDiagramCode] = useState("");
  const [diagramType, setDiagramType] = useState("auto");
  const [diagramEditorType, setDiagramEditorType] = useState<"mermaid" | "plantuml">("mermaid");
  const [diagramDetail, setDiagramDetail] = useState("Средний");
  const [diagramExamples, setDiagramExamples] = useState(true);
  const [diagramRun, setDiagramRun] = useState<any | null>(null);
  const [diagramStatus, setDiagramStatus] = useState<string | null>(null);
  const [diagramResult, setDiagramResult] = useState<any | null>(null);
  const [diagramPreview, setDiagramPreview] = useState<any | null>(null);
  const [mermaidCode, setMermaidCode] = useState("");
  const [plantumlCode, setPlantumlCode] = useState("");
  const [diagramTemplate, setDiagramTemplate] = useState("Граф");
  const [imagePrompt, setImagePrompt] = useState("");
  const [imageStyle, setImageStyle] = useState("Реалистичный");
  const [imageSize, setImageSize] = useState("1024x1024");
  const [imageQuality, setImageQuality] = useState("standard");
  const [imageNegativePrompt, setImageNegativePrompt] = useState("");
  const [imageSeed, setImageSeed] = useState("");
  const [imageCount, setImageCount] = useState(1);
  const [imageResult, setImageResult] = useState<any | null>(null);
  const [imageEditPath, setImageEditPath] = useState("");
  const [imageEditPrompt, setImageEditPrompt] = useState("");
  const [imageEditPaths, setImageEditPaths] = useState("");
  const [imageEditNegative, setImageEditNegative] = useState("");
  const [imageEditWidth, setImageEditWidth] = useState(1024);
  const [imageEditHeight, setImageEditHeight] = useState(1024);
  const [imageEditSeed, setImageEditSeed] = useState("");
  const [imageEditInputType, setImageEditInputType] = useState("path");
  const [imageEditBatch, setImageEditBatch] = useState(false);
  const [imageAnalyzePath, setImageAnalyzePath] = useState("");
  const [imageAnalyzePrompt, setImageAnalyzePrompt] = useState("");
  const [imageAnalyzeTypes, setImageAnalyzeTypes] = useState<string[]>([]);
  const [imageAnalyzeAvailableTypes, setImageAnalyzeAvailableTypes] = useState<string[]>([]);
  const [imageAnalyzeInputType, setImageAnalyzeInputType] = useState("path");
  const [imageFiles, setImageFiles] = useState<string[]>([]);
  const [previewImage, setPreviewImage] = useState<any | null>(null);
  const [toolsList, setToolsList] = useState<any[]>([]);
  const [toolsMcp, setToolsMcp] = useState<any[]>([]);
  const [agentDesc, setAgentDesc] = useState("");
  const [agentTools, setAgentTools] = useState("");
  const [agentResult, setAgentResult] = useState<any | null>(null);
  const [utilityTab, setUtilityTab] = useState<"data" | "text" | "time" | "convert" | "system">("data");
  const [jsonInput, setJsonInput] = useState("");
  const [jsonResult, setJsonResult] = useState<any | null>(null);
  const [csvInput, setCsvInput] = useState("");
  const [csvDelimiter, setCsvDelimiter] = useState(",");
  const [csvResult, setCsvResult] = useState<any | null>(null);
  const [textInput, setTextInput] = useState("");
  const [textResult, setTextResult] = useState<any | null>(null);
  const [hashInput, setHashInput] = useState("");
  const [hashResult, setHashResult] = useState<any | null>(null);
  const [timeNow, setTimeNow] = useState<any | null>(null);
  const [timeStart, setTimeStart] = useState("");
  const [timeEnd, setTimeEnd] = useState("");
  const [timeDiff, setTimeDiff] = useState<any | null>(null);
  const [base64EncodeInput, setBase64EncodeInput] = useState("");
  const [base64DecodeInput, setBase64DecodeInput] = useState("");
  const [base64EncodeResult, setBase64EncodeResult] = useState("");
  const [base64DecodeResult, setBase64DecodeResult] = useState("");
  const [urlEncodeInput, setUrlEncodeInput] = useState("");
  const [urlDecodeInput, setUrlDecodeInput] = useState("");
  const [urlEncodeResult, setUrlEncodeResult] = useState("");
  const [urlDecodeResult, setUrlDecodeResult] = useState("");
  const [colorHex, setColorHex] = useState("#FF5733");
  const [colorRgb, setColorRgb] = useState<any | null>(null);
  const [toolDetailsModal, setToolDetailsModal] = useState<{ open: boolean; title: string; data: any }>({
    open: false,
    title: "",
    data: null,
  });

  const loadTools = useCallback(async () => {
    const defs = await runServiceAction("tools.list_definitions", {});
    const mcp = await runServiceAction("tools.list_mcp", {});
    setToolsList((defs as any)?.tools ?? []);
    setToolsMcp((mcp as any)?.tools ?? []);
  }, [runServiceAction]);

  const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

  const extractDiagramPayload = (payload: any) => {
    if (!payload) return null;
    const result = payload.result ?? payload;
    const finalOutput = result?.final_output ?? result?.finalOutput ?? result;
    if (!finalOutput) return null;
    if (typeof finalOutput === "object") return finalOutput;
    if (typeof finalOutput !== "string") return null;

    const marker = "Final answer:";
    const raw = finalOutput.includes(marker) ? finalOutput.split(marker).slice(1).join(marker).trim() : finalOutput.trim();
    try {
      return JSON.parse(raw);
    } catch {
      const parsed: Record<string, string> = {};
      const codeMatch = raw.match(/diagram_code['"]?\s*:\s*'([\s\S]*?)'(?:,\s*['"]explanation|$)/);
      if (codeMatch?.[1]) {
        parsed.diagram_code = codeMatch[1].replace(/\\n/g, "\n").replace(/\\\"/g, "\"").replace(/\\\\/g, "\\");
      }
      const explanationMatch = raw.match(/explanation['"]?\s*:\s*'([\s\S]*?)'(?:,\s*['"]recommendations|$)/);
      if (explanationMatch?.[1]) {
        parsed.explanation = explanationMatch[1].replace(/\\n/g, "\n");
      }
      const recommendationsMatch = raw.match(/recommendations['"]?\s*:\s*'([\s\S]*?)'(?:,\s*['"]file_saved|$)/);
      if (recommendationsMatch?.[1]) {
        parsed.recommendations = recommendationsMatch[1].replace(/\\n/g, "\n");
      }
      const fileMatch = raw.match(/file_saved['"]?\s*:\s*'([^']*)'/);
      if (fileMatch?.[1]) {
        parsed.file_saved = fileMatch[1];
      }
      if (Object.keys(parsed).length) return parsed;
      return { raw: finalOutput };
    }
  };

  const pickDiagramKind = (kind: string, code: string) => {
    if (kind === "plantuml" || kind === "mermaid") return kind;
    if (code.trim().toLowerCase().startsWith("@startuml")) return "plantuml";
    return "mermaid";
  };

  const generateDiagram = async () => {
    const resp = await runServiceAction("presets.diagram.generate", {
      prompt: diagramPrompt,
      diagram_type: diagramType,
      detail_level: diagramDetail,
      include_examples: diagramExamples,
    });
    setDiagramRun(resp);
    setDiagramStatus("running");
    setDiagramResult(null);
    setDiagramPreview(null);

    const runId = (resp as any)?.run_id;
    if (!runId) return;

    let statusValue = "running";
    const startTime = Date.now();
    while (Date.now() - startTime < 180000) {
      const statusResp = await runServiceAction("agents.status", { run_id: runId });
      statusValue =
        (statusResp as any)?.status?.status ??
        (statusResp as any)?.status ??
        (statusResp as any)?.state ??
        statusValue;
      if (statusValue === "completed" || statusValue === "failed" || statusValue === "cancelled") {
        break;
      }
      await sleep(1000);
    }
    setDiagramStatus(statusValue);

    const resultResp = await runServiceAction("agents.result", { run_id: runId });
    const parsed = extractDiagramPayload(resultResp);
    setDiagramResult(parsed);

    if (parsed?.diagram_code) {
      setDiagramCode(parsed.diagram_code);
      const kind = pickDiagramKind(diagramType, parsed.diagram_code);
      await previewDiagramRaw(parsed.diagram_code, kind);
    }
  };

  const previewDiagramRaw = async (code: string, kind: string) => {
    if (!code.trim()) return;
    const resp = await runServiceAction("presets.diagram.preview", {
      code,
      diagram_type: kind,
      format: "svg",
    });
    setDiagramPreview(resp);
  };

  const previewDiagram = async () => {
    await previewDiagramRaw(diagramCode, diagramType === "plantuml" ? "plantuml" : "mermaid");
  };

  const generateImage = async () => {
    const resp = await runServiceAction("presets.image.generate", {
      prompt: imagePrompt,
      style: imageStyle,
      size: imageSize,
      quality: imageQuality,
      n_images: imageCount,
      negative_prompt: imageNegativePrompt,
      seed: imageSeed ? Number(imageSeed) : undefined,
    });
    setImageResult(resp);
    const patterns = (resp as any)?.expected_files ?? [];
    if (patterns.length) {
      const list = await runServiceAction("files.list", { base_dir: ".", pattern: patterns[0] });
      setImageFiles((list as any)?.files ?? []);
    }
  };

  const editImage = async () => {
    const action = imageEditBatch ? "presets.image.edit_batch" : "presets.image.edit";
    const payload: Record<string, unknown> = {
      prompt: imageEditPrompt,
      negative_prompt: imageEditNegative,
      width: imageEditWidth,
      height: imageEditHeight,
      seed: imageEditSeed ? Number(imageEditSeed) : undefined,
    };
    if (imageEditBatch) {
      payload.image_inputs = imageEditPaths
        .split("\n")
        .map((item) => item.trim())
        .filter(Boolean);
      payload.input_type = imageEditInputType;
    } else {
      payload.image_input = imageEditPath;
      payload.input_type = imageEditInputType;
    }
    const resp = await runServiceAction(action, payload);
    setImageResult(resp);
  };

  const analyzeImage = async () => {
    const resp = await runServiceAction("presets.image.analyze", {
      image_input: imageAnalyzePath,
      input_type: imageAnalyzeInputType,
      analysis_prompt: imageAnalyzePrompt,
      analysis_types: imageAnalyzeTypes,
    });
    setImageResult(resp);
  };

  const generateAgent = async () => {
    const resp = await runServiceAction("presets.agent_constructor.generate", {
      description: agentDesc,
      tools_requested: agentTools.split(",").map((t) => t.trim()).filter(Boolean),
    });
    setAgentResult(resp);
  };

  const parseAnalysisPayload = (payload: any) => {
    if (!payload) return null;
    const raw = payload.result ?? payload;
    if (typeof raw === "string") {
      try {
        return JSON.parse(raw);
      } catch {
        return null;
      }
    }
    if (typeof raw === "object") return raw;
    return null;
  };

  const renderImageResult = () => {
    if (!imageResult) return null;
    const parsed = parseAnalysisPayload(imageResult);
    if (!parsed || (!parsed.general_description && !parsed.objects_detected)) {
      return <KeyValueList data={imageResult} />;
    }

    const objects = Array.isArray(parsed.objects_detected) ? parsed.objects_detected : [];
    const analysisFields = [
      { key: "composition_analysis", label: "Композиция" },
      { key: "perspective_analysis", label: "Перспектива" },
      { key: "background_analysis", label: "Фон" },
      { key: "color_analysis", label: "Цвет" },
      { key: "quality_assessment", label: "Качество" },
      { key: "content_analysis", label: "Контент" },
      { key: "face_analysis", label: "Лица" },
      { key: "mood_analysis", label: "Настроение" },
    ];

    return (
      <div className="stack">
        {parsed.general_description ? (
          <div className="card">
            <div className="card-title">Общее описание</div>
            <div className="card-description">{String(parsed.general_description)}</div>
          </div>
        ) : null}
        {objects.length ? (
          <div className="card">
            <div className="card-title">Объекты</div>
            <div className="cards profile-grid">
              {objects.map((item: any, idx: number) => (
                <article key={`${item.object ?? "obj"}-${idx}`} className="card">
                  <div className="card-title">{String(item.object ?? "Объект")}</div>
                  <div className="profile-meta">
                    <div>
                      <span className="label">Координаты</span>
                      <div className="meta-value">{String(item.coordinates ?? "—")}</div>
                    </div>
                    <div>
                      <span className="label">Уверенность</span>
                      <div className="meta-value">{String(item.confidence ?? "—")}</div>
                    </div>
                  </div>
                </article>
              ))}
            </div>
          </div>
        ) : null}
        {analysisFields.map((field) =>
          parsed[field.key] ? (
            <div key={field.key} className="card">
              <div className="card-title">{field.label}</div>
              <div className="card-description">{String(parsed[field.key])}</div>
            </div>
          ) : null
        )}
      </div>
    );
  };

  useEffect(() => {
    setIsMounted(true);
    void loadTools();
    setMermaidCode((current) => current || MERMAID_TEMPLATES["Граф"]);
    setPlantumlCode((current) => current || PLANTUML_TEMPLATES["Диаграмма классов"]);
  }, [loadTools]);

  useEffect(() => {
    if (!toolDetailsModal.open) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [toolDetailsModal.open]);

  useEffect(() => {
    if (tab !== "images") return;
    runServiceAction("presets.image.analysis_types", {})
      .then((resp) => {
        const items = (resp as any)?.types ?? [];
        setImageAnalyzeAvailableTypes(Array.isArray(items) ? items : []);
      })
      .catch(() => {
        setImageAnalyzeAvailableTypes([]);
      });
  }, [tab, runServiceAction]);

  useEffect(() => {
    const options = diagramEditorType === "mermaid" ? Object.keys(MERMAID_TEMPLATES) : Object.keys(PLANTUML_TEMPLATES);
    if (options.length && !options.includes(diagramTemplate)) {
      setDiagramTemplate(options[0]);
    }
  }, [diagramEditorType, diagramTemplate]);

  const getToolLabel = (tool: any) => {
    if (!tool) return "Без названия";
    if (typeof tool === "string") return tool;
    return tool.name || tool.tool_name || tool.id || tool.title || "Без названия";
  };

  const getToolDescription = (tool: any) => {
    if (!tool || typeof tool === "string") return "";
    return tool.description || tool.summary || tool.purpose || "";
  };

  return (
    <div className="section" id="tools">
      <div className="section-header">
        <div className="section-title">Инструменты</div>
        <div className="section-hint">Диаграммы, изображения и утилиты</div>
      </div>
      <div className="segment-row">
        <button className={`segment-button${tab === "diagram" ? " active" : ""}`} onClick={() => setTab("diagram")}>
          Диаграммы
        </button>
        <button className={`segment-button${tab === "images" ? " active" : ""}`} onClick={() => setTab("images")}>
          Изображения
        </button>
        <button className={`segment-button${tab === "utilities" ? " active" : ""}`} onClick={() => setTab("utilities")}>
          Утилиты
        </button>
        <button className={`segment-button${tab === "tools" ? " active" : ""}`} onClick={() => setTab("tools")}>
          Список
        </button>
        <button className={`segment-button${tab === "agent" ? " active" : ""}`} onClick={() => setTab("agent")}>
          Создать агента
        </button>
      </div>

      {tab === "diagram" ? (
        <div className="stack">
          <div className="segment-row">
            <button className={`segment-button${diagramTab === "ai" ? " active" : ""}`} onClick={() => setDiagramTab("ai")}>
              ИИ генерация
            </button>
            <button className={`segment-button${diagramTab === "editor" ? " active" : ""}`} onClick={() => setDiagramTab("editor")}>
              Редактор
            </button>
          </div>

          {diagramTab === "ai" ? (
            <div className="card">
              <div className="card-title">Создание диаграмм</div>
              <div className="form-grid">
                <label className="field">
                  <span className="label">Описание</span>
                  <textarea value={diagramPrompt} onChange={(e) => setDiagramPrompt(e.target.value)} />
                </label>
                <label className="field">
                  <span className="label">Код диаграммы (для предпросмотра)</span>
                  <textarea value={diagramCode} onChange={(e) => setDiagramCode(e.target.value)} />
                </label>
                <label className="field">
                  <span className="label">Тип</span>
                  <select value={diagramType} onChange={(e) => setDiagramType(e.target.value)}>
                    <option value="auto">Авто</option>
                    <option value="mermaid">Mermaid</option>
                    <option value="plantuml">PlantUML</option>
                  </select>
                </label>
                <label className="field">
                  <span className="label">Детализация</span>
                  <select value={diagramDetail} onChange={(e) => setDiagramDetail(e.target.value)}>
                    <option value="Высокий">Высокий</option>
                    <option value="Средний">Средний</option>
                    <option value="Базовый">Базовый</option>
                  </select>
                </label>
                <label className="toggle">
                  <input type="checkbox" checked={diagramExamples} onChange={(e) => setDiagramExamples(e.target.checked)} />
                  <span>Включить примеры</span>
                </label>
              </div>
              <div className="button-row">
                <button className="button" type="button" onClick={generateDiagram} disabled={isBusy || !diagramPrompt.trim()}>
                  Сгенерировать
                </button>
                <button className="button secondary" type="button" onClick={previewDiagram} disabled={isBusy || !diagramCode.trim()}>
                  Предпросмотр
                </button>
              </div>
              {diagramStatus ? <div className="card-description">Статус: {diagramStatus}</div> : null}
              {diagramRun ? <KeyValueList data={diagramRun} /> : null}
              {diagramResult ? (
                <div className="stack">
                  {diagramResult.diagram_code ? (
                    <div className="card">
                      <div className="card-title">Код диаграммы</div>
                      <textarea className="code" readOnly value={String(diagramResult.diagram_code)} />
                    </div>
                  ) : null}
                  {diagramResult.explanation ? (
                    <div className="card">
                      <div className="card-title">Пояснение</div>
                      <div className="card-description">{String(diagramResult.explanation)}</div>
                    </div>
                  ) : null}
                  {diagramResult.recommendations ? (
                    <div className="card">
                      <div className="card-title">Рекомендации</div>
                      <div className="card-description">{String(diagramResult.recommendations)}</div>
                    </div>
                  ) : null}
                  {!diagramResult.diagram_code && !diagramResult.explanation && !diagramResult.recommendations ? (
                    <KeyValueList data={diagramResult} />
                  ) : null}
                </div>
              ) : null}
              {diagramPreview?.base64 ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img className="image-preview" src={`data:${diagramPreview.mime_type};base64,${diagramPreview.base64}`} alt="diagram preview" />
              ) : null}
            </div>
          ) : null}

          {diagramTab === "editor" ? (
            <div className="card">
              <div className="card-title">Редактор диаграмм</div>
              <div className="form-grid">
                <label className="field">
                  <span className="label">Тип диаграммы</span>
                  <select value={diagramEditorType} onChange={(e) => setDiagramEditorType(e.target.value as "mermaid" | "plantuml")}>
                    <option value="mermaid">Mermaid</option>
                    <option value="plantuml">PlantUML</option>
                  </select>
                </label>
                <label className="field">
                  <span className="label">Шаблон</span>
                  <select value={diagramTemplate} onChange={(e) => setDiagramTemplate(e.target.value)}>
                    {(diagramEditorType === "mermaid" ? Object.keys(MERMAID_TEMPLATES) : Object.keys(PLANTUML_TEMPLATES)).map((name) => (
                      <option key={name} value={name}>
                        {name}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
              <div className="button-row">
                <button
                  className="button secondary"
                  type="button"
                  onClick={() => {
                    if (diagramEditorType === "mermaid") {
                      setMermaidCode(MERMAID_TEMPLATES[diagramTemplate] ?? "");
                    } else {
                      setPlantumlCode(PLANTUML_TEMPLATES[diagramTemplate] ?? "");
                    }
                  }}
                  disabled={isBusy}
                >
                  Загрузить шаблон
                </button>
                <button
                  className="button"
                  type="button"
                  onClick={() => {
                    const code = diagramEditorType === "mermaid" ? mermaidCode : plantumlCode;
                    void previewDiagramRaw(code, diagramEditorType);
                  }}
                  disabled={isBusy}
                >
                  Предпросмотр
                </button>
              </div>
              <label className="field">
                <span className="label">Код</span>
                <textarea
                  value={diagramEditorType === "mermaid" ? mermaidCode : plantumlCode}
                  onChange={(e) => {
                    if (diagramEditorType === "mermaid") {
                      setMermaidCode(e.target.value);
                    } else {
                      setPlantumlCode(e.target.value);
                    }
                  }}
                />
              </label>
              {diagramPreview?.base64 ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img className="image-preview" src={`data:${diagramPreview.mime_type};base64,${diagramPreview.base64}`} alt="diagram preview" />
              ) : null}
            </div>
          ) : null}
        </div>
      ) : null}

      {tab === "images" ? (
        <div className="stack">
          <div className="segment-row">
            <button className={`segment-button${imageTab === "generate" ? " active" : ""}`} onClick={() => setImageTab("generate")}>
              Генерация
            </button>
            <button className={`segment-button${imageTab === "edit" ? " active" : ""}`} onClick={() => setImageTab("edit")}>
              Редактирование
            </button>
            <button className={`segment-button${imageTab === "analyze" ? " active" : ""}`} onClick={() => setImageTab("analyze")}>
              Анализ
            </button>
          </div>

          {imageTab === "generate" ? (
            <div className="card">
              <div className="card-title">Генерация изображений</div>
              <div className="form-grid">
                <label className="field">
                  <span className="label">Промпт</span>
                  <textarea value={imagePrompt} onChange={(e) => setImagePrompt(e.target.value)} />
                </label>
                <label className="field">
                  <span className="label">Стиль</span>
                  <input value={imageStyle} onChange={(e) => setImageStyle(e.target.value)} />
                </label>
                <label className="field">
                  <span className="label">Размер</span>
                  <input value={imageSize} onChange={(e) => setImageSize(e.target.value)} />
                </label>
                <label className="field">
                  <span className="label">Качество</span>
                  <select value={imageQuality} onChange={(e) => setImageQuality(e.target.value)}>
                    <option value="standard">standard</option>
                    <option value="hd">hd</option>
                    <option value="ultra">ultra</option>
                  </select>
                </label>
                <label className="field">
                  <span className="label">Количество</span>
                  <input type="number" value={imageCount} onChange={(e) => setImageCount(Number(e.target.value) || 1)} />
                </label>
                <label className="field">
                  <span className="label">Негативный промпт</span>
                  <input value={imageNegativePrompt} onChange={(e) => setImageNegativePrompt(e.target.value)} />
                </label>
                <label className="field">
                  <span className="label">Seed</span>
                  <input value={imageSeed} onChange={(e) => setImageSeed(e.target.value)} />
                </label>
              </div>
              <div className="button-row">
                <button className="button" type="button" onClick={generateImage} disabled={isBusy || !imagePrompt.trim()}>
                  Сгенерировать
                </button>
              </div>
              {imageFiles.length ? (
                <details className="details">
                  <summary>Файлы изображений</summary>
                  <div className="graph-inputs">
                    {imageFiles.map((file) => (
                      <div key={file} className="graph-input">
                        <div className="meta-value" style={{ wordBreak: "break-all" }}>
                          {file}
                        </div>
                        <button
                          className="button ghost"
                          type="button"
                          onClick={async () => {
                            const resp = await runServiceAction("files.read_base64", { path: file });
                            const filename = (resp as any)?.filename ?? file;
                            const ext = String(filename).split(".").pop()?.toLowerCase();
                            const mime = ext === "svg" ? "image/svg+xml" : "image/png";
                            setPreviewImage({ ...(resp as any), mime_type: mime });
                          }}
                        >
                          Открыть
                        </button>
                      </div>
                    ))}
                  </div>
                </details>
              ) : null}
              {previewImage?.base64 ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img className="image-preview" src={`data:${previewImage.mime_type};base64,${previewImage.base64}`} alt="image preview" />
              ) : null}
            </div>
          ) : null}

          {imageTab === "edit" ? (
            <div className="card">
              <div className="card-title">Редактирование</div>
              <div className="form-grid">
                <label className="toggle">
                  <input type="checkbox" checked={imageEditBatch} onChange={(e) => setImageEditBatch(e.target.checked)} />
                  <span>Пакетный режим</span>
                </label>
                {imageEditBatch ? (
                  <label className="field">
                    <span className="label">Пути (каждый с новой строки)</span>
                    <textarea value={imageEditPaths} onChange={(e) => setImageEditPaths(e.target.value)} />
                  </label>
                ) : (
                  <label className="field">
                    <span className="label">Путь к файлу</span>
                    <input value={imageEditPath} onChange={(e) => setImageEditPath(e.target.value)} />
                  </label>
                )}
                {imageEditBatch ? (
                  <label className="field">
                    <span className="label">Загрузить файлы</span>
                    <input
                      type="file"
                      multiple
                      accept="image/*"
                      onChange={(event) => {
                        const files = Array.from(event.target.files ?? []);
                        if (!files.length) return;
                        Promise.all(
                          files.map(
                            (file) =>
                              new Promise<string>((resolve, reject) => {
                                const reader = new FileReader();
                                reader.onload = () => resolve(String(reader.result || ""));
                                reader.onerror = () => reject(new Error("read error"));
                                reader.readAsDataURL(file);
                              })
                          )
                        ).then((items) => {
                          const encoded = items.map((item) => item.split(",")[1] ?? "");
                          setImageEditPaths(encoded.join("\n"));
                          setImageEditInputType("base64");
                        });
                      }}
                    />
                  </label>
                ) : (
                  <label className="field">
                    <span className="label">Загрузить файл</span>
                    <input
                      type="file"
                      accept="image/*"
                      onChange={(event) => {
                        const file = event.target.files?.[0];
                        if (!file) return;
                        const reader = new FileReader();
                        reader.onload = () => {
                          const result = String(reader.result || "");
                          const base64 = result.split(",")[1] ?? "";
                          setImageEditPath(base64);
                          setImageEditInputType("base64");
                        };
                        reader.readAsDataURL(file);
                      }}
                    />
                  </label>
                )}
                <label className="field">
                  <span className="label">Тип ввода</span>
                  <select value={imageEditInputType} onChange={(e) => setImageEditInputType(e.target.value)}>
                    <option value="path">path</option>
                    <option value="paths">paths</option>
                    <option value="url">url</option>
                    <option value="base64">base64</option>
                  </select>
                </label>
                <label className="field">
                  <span className="label">Промпт</span>
                  <input value={imageEditPrompt} onChange={(e) => setImageEditPrompt(e.target.value)} />
                </label>
                <label className="field">
                  <span className="label">Негативный промпт</span>
                  <input value={imageEditNegative} onChange={(e) => setImageEditNegative(e.target.value)} />
                </label>
                <label className="field">
                  <span className="label">Ширина</span>
                  <input type="number" value={imageEditWidth} onChange={(e) => setImageEditWidth(Number(e.target.value) || 0)} />
                </label>
                <label className="field">
                  <span className="label">Высота</span>
                  <input type="number" value={imageEditHeight} onChange={(e) => setImageEditHeight(Number(e.target.value) || 0)} />
                </label>
                <label className="field">
                  <span className="label">Seed</span>
                  <input value={imageEditSeed} onChange={(e) => setImageEditSeed(e.target.value)} />
                </label>
              </div>
              <div className="button-row">
                <button
                  className="button secondary"
                  type="button"
                  onClick={editImage}
                  disabled={
                    isBusy ||
                    !imageEditPrompt.trim() ||
                    (imageEditBatch ? !imageEditPaths.trim() : !imageEditPath.trim())
                  }
                >
                  Редактировать
                </button>
              </div>
            </div>
          ) : null}

          {imageTab === "analyze" ? (
            <div className="card">
              <div className="card-title">Анализ</div>
              <div className="form-grid">
                <label className="field">
                  <span className="label">Путь к файлу</span>
                  <input value={imageAnalyzePath} onChange={(e) => setImageAnalyzePath(e.target.value)} />
                </label>
                <label className="field">
                  <span className="label">Загрузить файл</span>
                  <input
                    type="file"
                    accept="image/*"
                    onChange={(event) => {
                      const file = event.target.files?.[0];
                      if (!file) return;
                      const reader = new FileReader();
                      reader.onload = () => {
                        const result = String(reader.result || "");
                        const base64 = result.split(",")[1] ?? "";
                        setImageAnalyzePath(base64);
                        setImageAnalyzeInputType("base64");
                      };
                      reader.readAsDataURL(file);
                    }}
                  />
                </label>
                <label className="field">
                  <span className="label">Тип ввода</span>
                  <select value={imageAnalyzeInputType} onChange={(e) => setImageAnalyzeInputType(e.target.value)}>
                    <option value="path">path</option>
                    <option value="url">url</option>
                    <option value="base64">base64</option>
                    <option value="auto">auto</option>
                  </select>
                </label>
                <label className="field">
                  <span className="label">Запрос</span>
                  <input value={imageAnalyzePrompt} onChange={(e) => setImageAnalyzePrompt(e.target.value)} />
                </label>
                <div className="field">
                  <span className="label">Типы анализа</span>
                  <div className="toggle-grid">
                    {imageAnalyzeAvailableTypes.length ? (
                      imageAnalyzeAvailableTypes.map((item) => (
                        <label key={item} className="toggle">
                          <input
                            type="checkbox"
                            checked={imageAnalyzeTypes.includes(item)}
                            onChange={(event) => {
                              setImageAnalyzeTypes((prev) =>
                                event.target.checked ? [...prev, item] : prev.filter((value) => value !== item)
                              );
                            }}
                          />
                          <span>{item}</span>
                        </label>
                      ))
                    ) : (
                      <div className="card-description">Список типов не загрузился</div>
                    )}
                  </div>
                </div>
              </div>
              <div className="button-row">
                <button className="button ghost" type="button" onClick={analyzeImage} disabled={isBusy || !imageAnalyzePath.trim()}>
                  Анализировать
                </button>
              </div>
              {renderImageResult()}
            </div>
          ) : null}
        </div>
      ) : null}

      {tab === "utilities" ? (
        <div className="stack">
          <div className="segment-row">
            <button className={`segment-button${utilityTab === "data" ? " active" : ""}`} onClick={() => setUtilityTab("data")}>
              Данные
            </button>
            <button className={`segment-button${utilityTab === "text" ? " active" : ""}`} onClick={() => setUtilityTab("text")}>
              Текст
            </button>
            <button className={`segment-button${utilityTab === "time" ? " active" : ""}`} onClick={() => setUtilityTab("time")}>
              Время
            </button>
            <button className={`segment-button${utilityTab === "convert" ? " active" : ""}`} onClick={() => setUtilityTab("convert")}>
              Конвертеры
            </button>
            <button className={`segment-button${utilityTab === "system" ? " active" : ""}`} onClick={() => setUtilityTab("system")}>
              Система
            </button>
          </div>

          {utilityTab === "data" ? (
            <div className="card">
              <div className="card-title">JSON форматтер</div>
              <label className="field">
                <span className="label">JSON</span>
                <textarea value={jsonInput} onChange={(e) => setJsonInput(e.target.value)} />
              </label>
              <div className="button-row">
                <button
                  className="button"
                  type="button"
                  onClick={async () => {
                    const resp = await runServiceAction("utils.json.format", { text: jsonInput, mode: "pretty" });
                    setJsonResult((resp as any)?.result ?? resp);
                  }}
                  disabled={isBusy || !jsonInput.trim()}
                >
                  Форматировать
                </button>
                <button
                  className="button secondary"
                  type="button"
                  onClick={async () => {
                    const resp = await runServiceAction("utils.json.format", { text: jsonInput, mode: "minify" });
                    setJsonResult((resp as any)?.result ?? resp);
                  }}
                  disabled={isBusy || !jsonInput.trim()}
                >
                  Минимизировать
                </button>
                <button
                  className="button ghost"
                  type="button"
                  onClick={async () => {
                    const resp = await runServiceAction("utils.json.format", { text: jsonInput, mode: "validate" });
                    setJsonResult((resp as any)?.result ?? resp);
                  }}
                  disabled={isBusy || !jsonInput.trim()}
                >
                  Валидировать
                </button>
              </div>
              {jsonResult ? <KeyValueList data={jsonResult} /> : null}

              <div className="divider" />
              <div className="card-title">CSV анализатор</div>
              <label className="field">
                <span className="label">CSV данные</span>
                <textarea value={csvInput} onChange={(e) => setCsvInput(e.target.value)} />
              </label>
              <div className="form-grid">
                <label className="field">
                  <span className="label">Разделитель</span>
                  <input value={csvDelimiter} onChange={(e) => setCsvDelimiter(e.target.value)} />
                </label>
                <label className="field">
                  <span className="label">Загрузить CSV</span>
                  <input
                    type="file"
                    accept=".csv,text/csv"
                    onChange={(e) => {
                      const file = e.target.files?.[0];
                      if (!file) return;
                      const reader = new FileReader();
                      reader.onload = () => setCsvInput(String(reader.result || ""));
                      reader.readAsText(file);
                    }}
                  />
                </label>
              </div>
              <div className="button-row">
                <button
                  className="button"
                  type="button"
                  onClick={async () => {
                    const resp = await runServiceAction("utils.csv.analyze", { text: csvInput, delimiter: csvDelimiter, sample_rows: 5 });
                    setCsvResult((resp as any)?.result ?? resp);
                  }}
                  disabled={isBusy || !csvInput.trim()}
                >
                  Анализировать
                </button>
              </div>
              {csvResult ? <KeyValueList data={csvResult} /> : null}
            </div>
          ) : null}

          {utilityTab === "text" ? (
            <div className="card">
              <div className="card-title">Анализ текста</div>
              <label className="field">
                <span className="label">Текст</span>
                <textarea value={textInput} onChange={(e) => setTextInput(e.target.value)} />
              </label>
              <div className="button-row">
                <button
                  className="button"
                  type="button"
                  onClick={async () => {
                    const resp = await runServiceAction("utils.text.analyze", { text: textInput, top_n: 10 });
                    setTextResult((resp as any)?.result ?? resp);
                  }}
                  disabled={isBusy || !textInput.trim()}
                >
                  Анализировать
                </button>
              </div>
              {textResult ? <KeyValueList data={textResult} /> : null}

              <div className="divider" />
              <div className="card-title">Хеши</div>
              <label className="field">
                <span className="label">Текст</span>
                <input value={hashInput} onChange={(e) => setHashInput(e.target.value)} />
              </label>
              <div className="button-row">
                <button
                  className="button"
                  type="button"
                  onClick={async () => {
                    const resp = await runServiceAction("utils.hash.generate", { text: hashInput });
                    setHashResult((resp as any)?.result ?? resp);
                  }}
                  disabled={isBusy || !hashInput.trim()}
                >
                  Сгенерировать
                </button>
              </div>
              {hashResult ? <KeyValueList data={hashResult} /> : null}
            </div>
          ) : null}

          {utilityTab === "time" ? (
            <div className="card">
              <div className="card-title">Время</div>
              <div className="button-row">
                <button
                  className="button"
                  type="button"
                  onClick={async () => {
                    const resp = await runServiceAction("utils.time.now", {});
                    setTimeNow((resp as any)?.result ?? resp);
                  }}
                  disabled={isBusy}
                >
                  Текущее время
                </button>
              </div>
              {timeNow ? <KeyValueList data={timeNow} /> : null}
              <div className="divider" />
              <div className="card-title">Разность времени</div>
              <div className="form-grid">
                <label className="field">
                  <span className="label">Start (ISO)</span>
                  <input value={timeStart} onChange={(e) => setTimeStart(e.target.value)} />
                </label>
                <label className="field">
                  <span className="label">End (ISO)</span>
                  <input value={timeEnd} onChange={(e) => setTimeEnd(e.target.value)} />
                </label>
              </div>
              <div className="button-row">
                <button
                  className="button"
                  type="button"
                  onClick={async () => {
                    const resp = await runServiceAction("utils.time.diff", { start: timeStart, end: timeEnd });
                    setTimeDiff((resp as any)?.result ?? resp);
                  }}
                  disabled={isBusy || !timeStart || !timeEnd}
                >
                  Рассчитать
                </button>
              </div>
              {timeDiff ? <KeyValueList data={timeDiff} /> : null}
            </div>
          ) : null}

          {utilityTab === "convert" ? (
            <div className="card">
              <div className="card-title">Base64</div>
              <div className="form-grid">
                <label className="field">
                  <span className="label">Кодировать</span>
                  <textarea value={base64EncodeInput} onChange={(e) => setBase64EncodeInput(e.target.value)} />
                </label>
                <label className="field">
                  <span className="label">Декодировать</span>
                  <textarea value={base64DecodeInput} onChange={(e) => setBase64DecodeInput(e.target.value)} />
                </label>
              </div>
              <div className="button-row">
                <button
                  className="button"
                  type="button"
                  onClick={async () => {
                    const resp = await runServiceAction("utils.base64.encode", { text: base64EncodeInput });
                    setBase64EncodeResult((resp as any)?.result ?? resp);
                  }}
                  disabled={isBusy || !base64EncodeInput.trim()}
                >
                  Кодировать
                </button>
                <button
                  className="button secondary"
                  type="button"
                  onClick={async () => {
                    const resp = await runServiceAction("utils.base64.decode", { text: base64DecodeInput });
                    setBase64DecodeResult((resp as any)?.result ?? resp);
                  }}
                  disabled={isBusy || !base64DecodeInput.trim()}
                >
                  Декодировать
                </button>
              </div>
              {base64EncodeResult ? <div className="kv-value">{base64EncodeResult}</div> : null}
              {base64DecodeResult ? <div className="kv-value">{base64DecodeResult}</div> : null}

              <div className="divider" />
              <div className="card-title">URL</div>
              <div className="form-grid">
                <label className="field">
                  <span className="label">Кодировать URL</span>
                  <input value={urlEncodeInput} onChange={(e) => setUrlEncodeInput(e.target.value)} />
                </label>
                <label className="field">
                  <span className="label">Декодировать URL</span>
                  <input value={urlDecodeInput} onChange={(e) => setUrlDecodeInput(e.target.value)} />
                </label>
              </div>
              <div className="button-row">
                <button
                  className="button"
                  type="button"
                  onClick={async () => {
                    const resp = await runServiceAction("utils.url.encode", { text: urlEncodeInput });
                    setUrlEncodeResult((resp as any)?.result ?? resp);
                  }}
                  disabled={isBusy || !urlEncodeInput.trim()}
                >
                  Кодировать
                </button>
                <button
                  className="button secondary"
                  type="button"
                  onClick={async () => {
                    const resp = await runServiceAction("utils.url.decode", { text: urlDecodeInput });
                    setUrlDecodeResult((resp as any)?.result ?? resp);
                  }}
                  disabled={isBusy || !urlDecodeInput.trim()}
                >
                  Декодировать
                </button>
              </div>
              {urlEncodeResult ? <div className="kv-value">{urlEncodeResult}</div> : null}
              {urlDecodeResult ? <div className="kv-value">{urlDecodeResult}</div> : null}

              <div className="divider" />
              <div className="card-title">Цвет</div>
              <div className="form-grid">
                <label className="field">
                  <span className="label">HEX</span>
                  <input value={colorHex} onChange={(e) => setColorHex(e.target.value)} />
                </label>
              </div>
              <div className="button-row">
                <button
                  className="button"
                  type="button"
                  onClick={async () => {
                    const resp = await runServiceAction("utils.color.convert", { mode: "hex", value: colorHex });
                    setColorRgb((resp as any)?.result ?? resp);
                  }}
                  disabled={isBusy || !colorHex.trim()}
                >
                  Конвертировать
                </button>
              </div>
              {colorRgb ? <KeyValueList data={colorRgb} /> : null}
            </div>
          ) : null}

          {utilityTab === "system" ? (
            <div className="cards">
              <ActionCard title="Диагностика" action="system.diagnostics" onRun={runServiceAction} busy={isBusy} />
              <ActionCard title="Оптимизатор промптов" action="system.prompt_optimizer.run" onRun={runServiceAction} busy={isBusy} />
              <ActionCard title="Монитор зависаний: старт" action="system.stale_monitor.start" onRun={runServiceAction} busy={isBusy} />
              <ActionCard title="Монитор зависаний: стоп" action="system.stale_monitor.stop" onRun={runServiceAction} busy={isBusy} />
              <ActionCard title="Монитор зависаний: статус" action="system.stale_monitor.status" onRun={runServiceAction} busy={isBusy} />
            </div>
          ) : null}
        </div>
      ) : null}

      {tab === "tools" ? (
        <div className="stack">
          <div className="card">
            <div className="card-title">Инструменты</div>
            {toolsList.length ? (
              <div className="cards profile-grid">
                {toolsList.map((tool, index) => {
                  const label = getToolLabel(tool);
                  const description = getToolDescription(tool);
                  return (
                    <article key={`${label}-${index}`} className="card">
                      <div className="card-title">{label}</div>
                      <div className="card-description">{description || "Описание не задано."}</div>
                      <div className="profile-meta">
                        <div>
                          <span className="label">Категория</span>
                          <div className="meta-value">{tool?.category ?? "—"}</div>
                        </div>
                        <div>
                          <span className="label">Источник</span>
                          <div className="meta-value">{tool?.source_type ?? tool?.implementation_source ?? "—"}</div>
                        </div>
                      </div>
                      <div className="button-row">
                        <button
                          className="button ghost"
                          type="button"
                          onClick={() => setToolDetailsModal({ open: true, title: label, data: tool })}
                        >
                          Подробнее
                        </button>
                      </div>
                    </article>
                  );
                })}
              </div>
            ) : (
              <div className="card-description">Список инструментов пуст.</div>
            )}
          </div>
          <div className="card">
            <div className="card-title">MCP инструменты</div>
            {toolsMcp.length ? (
              <div className="cards profile-grid">
                {toolsMcp.map((tool, index) => {
                  const label = getToolLabel(tool);
                  const data = typeof tool === "string" ? { name: tool } : tool;
                  return (
                    <article key={`${label}-${index}`} className="card">
                      <div className="card-title">{label}</div>
                      <div className="card-description">MCP инструмент</div>
                      <div className="button-row">
                        <button
                          className="button ghost"
                          type="button"
                          onClick={() => setToolDetailsModal({ open: true, title: label, data })}
                        >
                          Подробнее
                        </button>
                      </div>
                    </article>
                  );
                })}
              </div>
            ) : (
              <div className="card-description">MCP инструменты не найдены.</div>
            )}
          </div>
        </div>
      ) : null}

      {tab === "agent" ? (
        <div className="card">
          <div className="card-title">Создать агента</div>
          <div className="form-grid">
            <label className="field">
              <span className="label">Описание</span>
              <textarea value={agentDesc} onChange={(e) => setAgentDesc(e.target.value)} />
            </label>
            <label className="field">
              <span className="label">Инструменты (через запятую)</span>
              <input value={agentTools} onChange={(e) => setAgentTools(e.target.value)} placeholder="search_tool, webpage_content" />
            </label>
          </div>
          <div className="button-row">
            <button className="button" type="button" onClick={generateAgent} disabled={isBusy || !agentDesc.trim()}>
              Сгенерировать профиль
            </button>
          </div>
          {agentResult ? <KeyValueList data={agentResult} /> : null}
        </div>
      ) : null}
      {isMounted && toolDetailsModal.open
        ? createPortal(
            <div className="modal-overlay" onClick={() => setToolDetailsModal({ open: false, title: "", data: null })}>
              <div className="modal" onClick={(event) => event.stopPropagation()}>
                <div className="section-header">
                  <div>
                    <div className="card-title">{toolDetailsModal.title}</div>
                    <div className="card-description">Детали инструмента</div>
                  </div>
                  <button
                    className="modal-close"
                    type="button"
                    aria-label="Закрыть"
                    onClick={() => setToolDetailsModal({ open: false, title: "", data: null })}
                  >
                    ×
                  </button>
                </div>
                {toolDetailsModal.data ? <KeyValueList data={toolDetailsModal.data} /> : <div className="card-description">Нет данных.</div>}
              </div>
            </div>,
            document.body
          )
        : null}
    </div>
  );
}

export function PresetsSection({ runServiceAction, isBusy }: ActionCardProps) {
  return (
    <div className="section" id="presets">
      <div className="section-header">
        <div className="section-title">Пресеты</div>
        <div className="section-hint">Быстрые сценарии</div>
      </div>
      <div className="cards">
        <ActionCard
          title="Генерация SQL (preset)"
          action="presets.text_to_sql.generate"
          onRun={runServiceAction}
          busy={isBusy}
          fields={[
            { name: "query", label: "Query", type: "textarea", required: true },
            { name: "dsn", label: "DSN", type: "text", required: true },
            { name: "max_rows", label: "Max rows", type: "number", defaultValue: 100 },
          ]}
        />
        <ActionCard
          title="Диаграмма (preset)"
          action="presets.diagram.generate"
          onRun={runServiceAction}
          busy={isBusy}
          fields={[
            { name: "prompt", label: "Prompt", type: "textarea", required: true },
            { name: "diagram_type", label: "Тип", type: "text", defaultValue: "auto" },
          ]}
        />
        <ActionCard
          title="Генерация изображения (preset)"
          action="presets.image.generate"
          onRun={runServiceAction}
          busy={isBusy}
          fields={[
            { name: "prompt", label: "Prompt", type: "textarea", required: true },
            { name: "size", label: "Size", type: "text", defaultValue: "1024x1024" },
          ]}
        />
      </div>
    </div>
  );
}

type SystemTab = "overview" | "progress";

type SystemSectionProps = ActionCardProps & {
  progressResults: ProgressResult[];
  clearProgressResults: () => void;
};

export function SystemSection({ runServiceAction, isBusy, progressResults, clearProgressResults }: SystemSectionProps) {
  const [tab, setTab] = useState<SystemTab>("overview");
  const [diagnosticsModalOpen, setDiagnosticsModalOpen] = useState(false);
  const [diagnosticsResult, setDiagnosticsResult] = useState<unknown>(null);
  const [activeRunsModalOpen, setActiveRunsModalOpen] = useState(false);
  const [activeRunsResult, setActiveRunsResult] = useState<unknown>(null);
  const [checksModalOpen, setChecksModalOpen] = useState(false);
  const [checksResult, setChecksResult] = useState<unknown>(null);
  const [isMounted, setIsMounted] = useState(false);

  useEffect(() => {
    setIsMounted(true);
  }, []);

  useEffect(() => {
    if (!diagnosticsModalOpen) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [diagnosticsModalOpen]);

  useEffect(() => {
    if (!activeRunsModalOpen) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [activeRunsModalOpen]);

  useEffect(() => {
    if (!checksModalOpen) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [checksModalOpen]);
  return (
    <div className="section" id="system">
      <div className="section-header">
        <div className="section-title">Система</div>
        <div className="section-hint">Здоровье, версия, перезапуск</div>
      </div>
      <div className="segment-row">
        <button className={`segment-button${tab === "overview" ? " active" : ""}`} onClick={() => setTab("overview")}>
          Система
        </button>
        <button className={`segment-button${tab === "progress" ? " active" : ""}`} onClick={() => setTab("progress")}>
          Прогресс
        </button>
      </div>

      {tab === "overview" ? (
        <div className="cards">
          <ActionCard
            title="Active runs"
            action="system.active_runs"
            onRun={async (action, payload) => {
              const resp = await runServiceAction(action, payload);
              setActiveRunsResult(resp);
              setActiveRunsModalOpen(true);
            }}
            busy={isBusy}
          />
          <ActionCard
            title="Checks"
            action="system.checks"
            onRun={async (action, payload) => {
              const resp = await runServiceAction(action, payload);
              setChecksResult(resp);
              setChecksModalOpen(true);
            }}
            busy={isBusy}
          />
          <ActionCard
            title="Diagnostics"
            action="system.diagnostics"
            onRun={async (action, payload) => {
              const resp = await runServiceAction(action, payload);
              setDiagnosticsResult(resp);
              setDiagnosticsModalOpen(true);
            }}
            busy={isBusy}
          />
        </div>
      ) : null}

      {tab === "progress" ? (
        <ProgressSection
          runServiceAction={runServiceAction}
          isBusy={isBusy}
          results={progressResults}
          clearResults={clearProgressResults}
        />
      ) : null}
      {isMounted && diagnosticsModalOpen ? (
        <div className="modal-overlay" onClick={() => setDiagnosticsModalOpen(false)}>
          <div className="modal" onClick={(event) => event.stopPropagation()}>
            <div className="section-header">
              <div>
                <div className="card-title">Diagnostics</div>
                <div className="card-description">Системная диагностика</div>
              </div>
              <button className="modal-close" aria-label="Закрыть" onClick={() => setDiagnosticsModalOpen(false)}>
                ×
              </button>
            </div>
            {diagnosticsResult ? <KeyValueList data={diagnosticsResult} /> : <div className="card-description">Нет данных.</div>}
          </div>
        </div>
      ) : null}
      {isMounted && activeRunsModalOpen ? (
        <div className="modal-overlay" onClick={() => setActiveRunsModalOpen(false)}>
          <div className="modal" onClick={(event) => event.stopPropagation()}>
            <div className="section-header">
              <div>
                <div className="card-title">Active runs</div>
                <div className="card-description">Текущие активные запуски</div>
              </div>
              <button className="modal-close" aria-label="Закрыть" onClick={() => setActiveRunsModalOpen(false)}>
                ×
              </button>
            </div>
            {activeRunsResult ? <KeyValueList data={activeRunsResult} /> : <div className="card-description">Нет данных.</div>}
          </div>
        </div>
      ) : null}
      {isMounted && checksModalOpen ? (
        <div className="modal-overlay" onClick={() => setChecksModalOpen(false)}>
          <div className="modal" onClick={(event) => event.stopPropagation()}>
            <div className="section-header">
              <div>
                <div className="card-title">Checks</div>
                <div className="card-description">Системные проверки</div>
              </div>
              <button className="modal-close" aria-label="Закрыть" onClick={() => setChecksModalOpen(false)}>
                ×
              </button>
            </div>
            {checksResult ? <KeyValueList data={checksResult} /> : <div className="card-description">Нет данных.</div>}
          </div>
        </div>
      ) : null}
    </div>
  );
}
