"use client";

import React, { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { KeyValueList } from "../shared/KeyValueList";

type MemoryTab = "status" | "search" | "analytics" | "manage";

type MemorySearchForm = {
  query: string;
  memoryType: string;
  limit: number;
  sessionId: string;
  agentName: string;
};

type MemoryClearForm = { agentName: string; sessionId: string };

type Props = {
  isBusy: boolean;
  memoryTab: MemoryTab;
  setMemoryTab: (tab: MemoryTab) => void;
  loadMemoryStatus: () => void;
  memoryStatusError: string | null;
  memoryStatusLoading: boolean;
  memoryStatus: unknown;
  memorySearchForm: MemorySearchForm;
  setMemorySearchForm: React.Dispatch<React.SetStateAction<MemorySearchForm>>;
  handleMemorySearch: () => void;
  memorySearchError: string | null;
  memorySearchResults: unknown;
  memoryAnalytics: Record<string, unknown>;
  memoryAnalyticsDays: number;
  setMemoryAnalyticsDays: React.Dispatch<React.SetStateAction<number>>;
  handleMemoryAnalytics: () => void;
  memoryManageResult: unknown;
  handleMemoryAction: (action: string, payload?: Record<string, unknown>) => Promise<void>;
  memoryTestResult: unknown;
  handleMemoryTestEmbeddings: () => void;
  memoryClearForm: MemoryClearForm;
  setMemoryClearForm: React.Dispatch<React.SetStateAction<MemoryClearForm>>;
};

export function MemorySection({
  isBusy,
  memoryTab,
  setMemoryTab,
  loadMemoryStatus,
  memoryStatusError,
  memoryStatusLoading,
  memoryStatus,
  memorySearchForm,
  setMemorySearchForm,
  handleMemorySearch,
  memorySearchError,
  memorySearchResults,
  memoryAnalytics,
  memoryAnalyticsDays,
  setMemoryAnalyticsDays,
  handleMemoryAnalytics,
  memoryManageResult,
  handleMemoryAction,
  memoryTestResult,
  handleMemoryTestEmbeddings,
  memoryClearForm,
  setMemoryClearForm,
}: Props) {
  const [autoRefresh, setAutoRefresh] = useState(false);
  const [activeAgents, setActiveAgents] = useState<Array<{ agent_name?: string; total_count?: number }>>([]);
  const [isMounted, setIsMounted] = useState(false);
  const [agentsModalOpen, setAgentsModalOpen] = useState(false);
  const [agentSearch, setAgentSearch] = useState("");
  const [agentMinCount, setAgentMinCount] = useState(0);
  const [exportFormat, setExportFormat] = useState("json");
  const [exportSession, setExportSession] = useState("");
  const [exportAgent, setExportAgent] = useState("");
  const [importText, setImportText] = useState("");
  const [cleanupDays, setCleanupDays] = useState(30);
  const [confirmFullCleanup, setConfirmFullCleanup] = useState(false);

  useEffect(() => {
    setIsMounted(true);
  }, []);

  useEffect(() => {
    if (!autoRefresh) return;
    const id = window.setInterval(() => {
      loadMemoryStatus();
    }, 30000);
    return () => window.clearInterval(id);
  }, [autoRefresh, loadMemoryStatus]);

  useEffect(() => {
    if (!memoryManageResult || typeof memoryManageResult !== "object") return;
    const action = (memoryManageResult as any).action;
    const result = (memoryManageResult as any).result;
    if (action === "memory.active_agents") {
      const items = (result as any)?.agents ?? result;
      if (Array.isArray(items)) {
        setActiveAgents(items);
      }
    }
  }, [memoryManageResult]);

  useEffect(() => {
    if (!agentsModalOpen) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [agentsModalOpen]);
  const normalizeKeywords = (raw: unknown): Array<{ label: string; key: string }> => {
    if (!raw) return [];
    if (Array.isArray(raw)) {
      return raw
        .map((item, idx) => {
          if (item && typeof item === "object") {
            const kw = (item as any).keyword ?? (item as any).key ?? (item as any).word;
            const count = (item as any).count ?? (item as any).value;
            if (kw != null) {
              const countNum = typeof count === "number" ? count : Number(count);
              const suffix = Number.isFinite(countNum) ? ` (${countNum})` : "";
              const label = `${String(kw)}${suffix}`;
              return { label, key: `arr-${idx}-${String(kw)}` };
            }
          }
          return { label: String(item), key: `arr-${idx}-${String(item)}` };
        })
        .filter((x) => x.label.trim().length > 0);
    }
    if (typeof raw === "string") {
      return raw
        .split(/[,\n]/g)
        .map((s) => s.trim())
        .filter(Boolean)
        .map((s, idx) => ({ label: s, key: `str-${idx}-${s}` }));
    }
    if (typeof raw === "object") {
      const entries = Object.entries(raw as Record<string, unknown>);
      // Частый формат: { "keyword": 12, "another": 3 }
      return entries
        .map(([k, v]) => {
          const vNum = typeof v === "number" ? v : Number(v);
          const suffix = Number.isFinite(vNum) ? ` (${vNum})` : "";
          return { label: `${k}${suffix}`, key: `obj-${k}` };
        })
        .filter((x) => x.label.trim().length > 0);
    }
    return [{ label: String(raw), key: `other-${String(raw)}` }];
  };

  const pickArray = <T,>(raw: unknown, field: string): T[] | null => {
    if (!raw) return null;
    if (Array.isArray(raw)) return raw as T[];
    if (typeof raw !== "object") return null;
    const value = (raw as any)[field];
    return Array.isArray(value) ? (value as T[]) : null;
  };

  return (
    <div className="section" id="memory">
      <div className="section-header">
        <div className="section-title">Память и RAG</div>
        <div className="section-hint">Статус, поиск, аналитика, управление</div>
      </div>
      <div className="segment-row">
        <button className={`segment-button${memoryTab === "status" ? " active" : ""}`} onClick={() => setMemoryTab("status")}>
          Статус
        </button>
        <button className={`segment-button${memoryTab === "search" ? " active" : ""}`} onClick={() => setMemoryTab("search")}>
          Поиск
        </button>
        <button className={`segment-button${memoryTab === "analytics" ? " active" : ""}`} onClick={() => setMemoryTab("analytics")}>
          Аналитика
        </button>
        <button className={`segment-button${memoryTab === "manage" ? " active" : ""}`} onClick={() => setMemoryTab("manage")}>
          Управление
        </button>
      </div>

      {memoryTab === "status" ? (
        <div className="stack">
          <div className="toolbar-row">
            <div className="inline">
              <button className="button secondary" type="button" onClick={loadMemoryStatus} disabled={isBusy}>
                Обновить статус
              </button>
              <label className="toggle">
                <input type="checkbox" checked={autoRefresh} onChange={(event) => setAutoRefresh(event.target.checked)} />
                <span>Автообновление (30с)</span>
              </label>
            </div>
            {memoryStatusError ? <div className="card-description">Ошибка: {memoryStatusError}</div> : null}
          </div>
          <div className="cards">
            <article className="card">
              <div className="card-title">Статус памяти</div>
              {memoryStatusLoading ? <div className="card-description">Загрузка...</div> : null}
              {memoryStatus ? (
                <div className="profile-meta">
                  <div>
                    <span className="label">SQLite</span>
                    <div className="meta-value">{(memoryStatus as any).sqlite_available ? "OK" : "—"}</div>
                  </div>
                  <div>
                    <span className="label">ChromaDB</span>
                    <div className="meta-value">{(memoryStatus as any).chromadb_available ? "OK" : "—"}</div>
                  </div>
                  <div>
                    <span className="label">Embeddings</span>
                    <div className="meta-value">{(memoryStatus as any).embedding_model_name ?? "—"}</div>
                  </div>
                  <div>
                    <span className="label">Тактическая память</span>
                    <div className="meta-value">{(memoryStatus as any).tactical_memories_count ?? "—"}</div>
                  </div>
                  <div>
                    <span className="label">Стратегическая память</span>
                    <div className="meta-value">{(memoryStatus as any).strategic_memories_count ?? "—"}</div>
                  </div>
                  <div>
                    <span className="label">Размер БД (МБ)</span>
                    <div className="meta-value">{(memoryStatus as any).database_size_mb ?? "—"}</div>
                  </div>
                  <div>
                    <span className="label">Путь SQLite</span>
                    <div className="meta-value">{(memoryStatus as any).sqlite_path ?? "—"}</div>
                  </div>
                  <div>
                    <span className="label">Путь ChromaDB</span>
                    <div className="meta-value">{(memoryStatus as any).chromadb_path ?? "—"}</div>
                  </div>
                </div>
              ) : null}
              {memoryStatus && (memoryStatus as any).collections_info ? (
                <details className="details">
                  <summary>Коллекции</summary>
                  <KeyValueList data={(memoryStatus as any).collections_info} />
                </details>
              ) : null}
              <div className="button-row">
                <button className="button secondary" type="button" onClick={() => handleMemoryAction("memory.rebuild")} disabled={isBusy}>
                  Перестроить ChromaDB
                </button>
                <button className="button ghost" type="button" onClick={handleMemoryTestEmbeddings} disabled={isBusy}>
                  Тест embeddings
                </button>
              </div>
              {memoryTestResult ? (
                <details className="details">
                  <summary>Результат теста</summary>
                  <KeyValueList data={memoryTestResult} />
                </details>
              ) : null}
            </article>
          </div>
        </div>
      ) : null}

      {memoryTab === "search" ? (
        <div className="stack">
          <div className="card">
            <div className="section-header">
              <div className="card-title">Поиск</div>
              <div className="card-description">Запрос в память с фильтрами.</div>
            </div>
            <div className="form-grid">
              <label className="field">
                <span className="label">Запрос</span>
                <input
                  value={memorySearchForm.query}
                  onChange={(event) => setMemorySearchForm((prev) => ({ ...prev, query: event.target.value }))}
                  placeholder="Что искать?"
                />
              </label>
              <label className="field">
                <span className="label">Тип памяти</span>
                <select
                  value={memorySearchForm.memoryType}
                  onChange={(event) => setMemorySearchForm((prev) => ({ ...prev, memoryType: event.target.value }))}
                >
                  <option value="tactical">tactical</option>
                  <option value="strategic">strategic</option>
                  <option value="">—</option>
                </select>
              </label>
              <label className="field">
                <span className="label">Лимит</span>
                <input
                  type="number"
                  value={memorySearchForm.limit}
                  onChange={(event) => setMemorySearchForm((prev) => ({ ...prev, limit: Number(event.target.value) || 0 }))}
                />
              </label>
              <label className="field">
                <span className="label">Session ID</span>
                <input
                  value={memorySearchForm.sessionId}
                  onChange={(event) => setMemorySearchForm((prev) => ({ ...prev, sessionId: event.target.value }))}
                  placeholder="session-123"
                />
              </label>
              <label className="field">
                <span className="label">Agent name</span>
                <input
                  value={memorySearchForm.agentName}
                  onChange={(event) => setMemorySearchForm((prev) => ({ ...prev, agentName: event.target.value }))}
                  placeholder="researcher"
                />
              </label>
            </div>
            <div className="button-row">
              <button className="button" type="button" onClick={handleMemorySearch} disabled={isBusy || !memorySearchForm.query.trim()}>
                Искать
              </button>
            </div>
            {memorySearchError ? <div className="card-description">Ошибка: {memorySearchError}</div> : null}
            {memorySearchResults ? (
              Array.isArray(memorySearchResults) ? (
                <div className="cards">
                  {(memorySearchResults as any[]).map((item, idx) => (
                    <article key={idx} className="card">
                      <KeyValueList data={item} maxDepth={4} />
                    </article>
                  ))}
                </div>
              ) : (
                <div className="cards">
                  <article className="card">
                    <KeyValueList data={memorySearchResults} maxDepth={4} />
                  </article>
                </div>
              )
            ) : null}
          </div>
        </div>
      ) : null}

      {memoryTab === "analytics" ? (
        <div className="stack">
          <div className="card">
            <div className="section-header">
              <div className="card-title">Аналитика памяти</div>
              <div className="card-description">Сводка, таймсерии и ключевые слова.</div>
            </div>
            <div className="form-grid">
              <label className="field">
                <span className="label">Дней</span>
                <input type="number" value={memoryAnalyticsDays} onChange={(event) => setMemoryAnalyticsDays(Number(event.target.value) || 0)} />
              </label>
            </div>
            <div className="button-row">
              <button className="button" type="button" onClick={handleMemoryAnalytics} disabled={isBusy}>
                Обновить аналитику
              </button>
            </div>
            {memoryAnalytics.summary ? (
              <details className="details">
                <summary>Сводка</summary>
                {(() => {
                  const agents = pickArray<{ agent_name?: unknown; total?: unknown; sessions?: unknown; last_activity?: unknown }>(
                    memoryAnalytics.summary,
                    "agents",
                  );
                  if (!agents) return <KeyValueList data={memoryAnalytics.summary} />;
                  return (
                    <div className="cards">
                      {agents.map((a, idx) => (
                        <article key={idx} className="card">
                          <div className="card-title">{String(a.agent_name ?? "—")}</div>
                          <div className="profile-meta">
                            <div>
                              <span className="label">Записей</span>
                              <div className="meta-value">{String(a.total ?? "—")}</div>
                            </div>
                            <div>
                              <span className="label">Сессий</span>
                              <div className="meta-value">{String(a.sessions ?? "—")}</div>
                            </div>
                            <div>
                              <span className="label">Последняя активность</span>
                              <div className="meta-value">{String(a.last_activity ?? "—")}</div>
                            </div>
                          </div>
                        </article>
                      ))}
                    </div>
                  );
                })()}
              </details>
            ) : null}
            {memoryAnalytics.timeseries ? (
              <details className="details">
                <summary>Таймсерии</summary>
                {(() => {
                  const series = pickArray<{ day?: unknown; count?: unknown }>(memoryAnalytics.timeseries, "series");
                  if (!series) return <KeyValueList data={memoryAnalytics.timeseries} />;
                  return (
                    <div className="cards">
                      {series.map((p, idx) => (
                        <article key={idx} className="card">
                          <div className="card-title">{String(p.day ?? "—")}</div>
                          <div className="card-description">Записей: {String(p.count ?? "—")}</div>
                        </article>
                      ))}
                    </div>
                  );
                })()}
              </details>
            ) : null}
            {memoryAnalytics.keywords ? (
              <details className="details">
                <summary>Ключевые слова</summary>
                <div className="badge-row">
                  {normalizeKeywords(pickArray<unknown>(memoryAnalytics.keywords, "keywords") ?? memoryAnalytics.keywords).map((kw) => (
                    <span key={kw.key} className="badge">
                      {kw.label}
                    </span>
                  ))}
                </div>
              </details>
            ) : null}
            {memoryAnalytics.error ? <div className="card-description">Ошибка: {String(memoryAnalytics.error)}</div> : null}
          </div>
        </div>
      ) : null}

      {memoryTab === "manage" ? (
        <div className="stack">
          <div className="cards">
            <article className="card">
              <div className="card-title">Перестройка</div>
              <div className="card-description">Полное пересоздание индексов (Chroma/SQLite).</div>
              <div className="button-row">
                <button className="button" type="button" onClick={() => handleMemoryAction("memory.rebuild")} disabled={isBusy}>
                  Перестроить ChromaDB
                </button>
                <button
                  className="button ghost"
                  type="button"
                  onClick={() => handleMemoryAction("memory.optimize_indexes", { confirm: true })}
                  disabled={isBusy}
                >
                  Оптимизировать индексы
                </button>
                <button
                  className="button ghost"
                  type="button"
                  onClick={() => handleMemoryAction("memory.compress_database", { confirm: true })}
                  disabled={isBusy}
                >
                  Сжать базу
                </button>
              </div>
            </article>
            <article className="card">
              <div className="card-title">Активные агенты</div>
              <div className="card-description">Список агентов с памятью.</div>
              <div className="button-row">
                <button
                  className="button secondary"
                  type="button"
                  onClick={async () => {
                    if (!activeAgents.length) {
                      await handleMemoryAction("memory.active_agents");
                    }
                    setAgentsModalOpen(true);
                  }}
                  disabled={isBusy}
                >
                  Открыть список
                </button>
              </div>
              <div className="status-chip">Найдено: {activeAgents.length}</div>
            </article>
            <article className="card">
              <div className="card-title">Очистка агента</div>
              <div className="card-description">Удалить память агента/сессии.</div>
              <div className="form-grid">
                <label className="field">
                  <span className="label">Agent</span>
                  <input
                    placeholder="researcher"
                    value={memoryClearForm.agentName}
                    onChange={(event) => setMemoryClearForm((prev) => ({ ...prev, agentName: event.target.value }))}
                  />
                </label>
                <label className="field">
                  <span className="label">Session ID</span>
                  <input
                    placeholder="session-001"
                    value={memoryClearForm.sessionId}
                    onChange={(event) => setMemoryClearForm((prev) => ({ ...prev, sessionId: event.target.value }))}
                  />
                </label>
              </div>
              <div className="button-row">
                <button
                  className="button ghost"
                  type="button"
                  onClick={() =>
                    handleMemoryAction("memory.clear_agent", {
                      agent_name: memoryClearForm.agentName,
                      session_id: memoryClearForm.sessionId,
                      confirm: true,
                    })
                  }
                  disabled={isBusy}
                >
                  Очистить
                </button>
              </div>
            </article>
            <article className="card">
              <div className="card-title">Экспорт / Импорт</div>
              <div className="card-description">Выгрузка или загрузка памяти.</div>
              <div className="form-grid">
                <label className="field">
                  <span className="label">Формат</span>
                  <select value={exportFormat} onChange={(e) => setExportFormat(e.target.value)}>
                    <option value="json">JSON</option>
                    <option value="csv">CSV</option>
                  </select>
                </label>
                <label className="field">
                  <span className="label">Session ID (опционально)</span>
                  <input value={exportSession} onChange={(e) => setExportSession(e.target.value)} />
                </label>
                <label className="field">
                  <span className="label">Агент (опционально)</span>
                  <input value={exportAgent} onChange={(e) => setExportAgent(e.target.value)} />
                </label>
              </div>
              <div className="button-row">
                <button
                  className="button secondary"
                  type="button"
                  onClick={() =>
                    handleMemoryAction("memory.export", {
                      format: exportFormat,
                      session_id: exportSession || undefined,
                      agent_name: exportAgent || undefined,
                    })
                  }
                  disabled={isBusy}
                >
                  Экспорт
                </button>
              </div>
              <label className="field">
                <span className="label">Импорт (JSON массив)</span>
                <textarea value={importText} onChange={(e) => setImportText(e.target.value)} />
              </label>
              <label className="field">
                <span className="label">Загрузить JSON</span>
                <input
                  type="file"
                  accept=".json,application/json"
                  onChange={(event) => {
                    const file = event.target.files?.[0];
                    if (!file) return;
                    const reader = new FileReader();
                    reader.onload = () => setImportText(String(reader.result || ""));
                    reader.readAsText(file);
                  }}
                />
              </label>
              <div className="button-row">
                <button
                  className="button ghost"
                  type="button"
                  onClick={() => {
                    try {
                      const records = JSON.parse(importText || "[]");
                      handleMemoryAction("memory.import", { format: "json", records, allow_overwrite: false });
                    } catch {
                      handleMemoryAction("memory.import", { format: "json", records: [] });
                    }
                  }}
                  disabled={isBusy || !importText.trim()}
                >
                  Импорт
                </button>
              </div>
            </article>
            <article className="card">
              <div className="card-title">Очистка старых записей</div>
              <div className="card-description">Удалить старые записи из памяти.</div>
              <div className="form-grid">
                <label className="field">
                  <span className="label">Сохранить дней</span>
                  <input type="number" value={cleanupDays} onChange={(e) => setCleanupDays(Number(e.target.value) || 0)} />
                </label>
              </div>
              <div className="button-row">
                <button
                  className="button"
                  type="button"
                  onClick={() => handleMemoryAction("memory.cleanup_old", { days: cleanupDays, confirm: true })}
                  disabled={isBusy}
                >
                  Очистить
                </button>
              </div>
            </article>
            <article className="card">
              <div className="card-title">Обслуживание</div>
              <div className="button-row">
                <button className="button secondary" type="button" onClick={() => handleMemoryAction("memory.vacuum", { confirm: true })} disabled={isBusy}>
                  Vacuum
                </button>
                <button className="button ghost" type="button" onClick={() => handleMemoryAction("memory.chroma.cleanup_empty")} disabled={isBusy}>
                  Очистить пустые коллекции
                </button>
              </div>
            </article>
            <article className="card">
              <div className="card-title">Полная очистка</div>
              <div className="card-description">Удалит все записи памяти. Опасная операция.</div>
              <div className="toggle-grid">
                <label className="toggle">
                  <input type="checkbox" checked={confirmFullCleanup} onChange={(e) => setConfirmFullCleanup(e.target.checked)} />
                  <span>Подтверждаю очистку</span>
                </label>
              </div>
              <div className="button-row">
                <button
                  className="button ghost"
                  type="button"
                  onClick={() => handleMemoryAction("memory.full_cleanup", { confirm: true })}
                  disabled={isBusy || !confirmFullCleanup}
                >
                  Очистить всю память
                </button>
              </div>
            </article>
            <article className="card">
              <div className="card-title">Статистика хранилища</div>
              <div className="profile-meta">
                <div>
                  <span className="label">SQLite размер</span>
                  <div className="meta-value">{(memoryStatus as any)?.database_size_mb ?? "—"} MB</div>
                </div>
                <div>
                  <span className="label">Всего записей</span>
                  <div className="meta-value">
                    {Number((memoryStatus as any)?.tactical_memories_count ?? 0) + Number((memoryStatus as any)?.strategic_memories_count ?? 0)}
                  </div>
                </div>
              </div>
              {(memoryStatus as any)?.collections_info ? (
                <details className="details">
                  <summary>Коллекции</summary>
                  <KeyValueList data={(memoryStatus as any).collections_info} />
                </details>
              ) : null}
            </article>
          </div>
          {memoryManageResult ? (
            <details className="details">
              <summary>Результат действия</summary>
              <KeyValueList data={memoryManageResult} />
            </details>
          ) : null}
        </div>
      ) : null}
      {isMounted && agentsModalOpen
        ? createPortal(
            <div className="modal-overlay" onClick={() => setAgentsModalOpen(false)}>
              <div className="modal" onClick={(event) => event.stopPropagation()}>
                <div className="section-header">
                  <div>
                    <div className="card-title">Список агентов памяти</div>
                    <div className="card-description">Поиск и фильтрация активных агентов.</div>
                  </div>
                  <button className="modal-close" aria-label="Закрыть" onClick={() => setAgentsModalOpen(false)}>
                    ×
                  </button>
                </div>
                <div className="form-grid">
                  <label className="field">
                    <span className="label">Поиск</span>
                    <input value={agentSearch} onChange={(event) => setAgentSearch(event.target.value)} placeholder="Имя агента" />
                  </label>
                  <label className="field">
                    <span className="label">Минимум записей</span>
                    <select value={agentMinCount} onChange={(event) => setAgentMinCount(Number(event.target.value))}>
                      <option value={0}>Любое</option>
                      <option value={10}>10+</option>
                      <option value={50}>50+</option>
                      <option value={100}>100+</option>
                      <option value={500}>500+</option>
                    </select>
                  </label>
                </div>
                <div className="toolbar-row">
                  <span className="status-chip">
                    Найдено:{" "}
                    {
                      activeAgents.filter((agent) => {
                        const name = (agent.agent_name ?? "").toLowerCase();
                        const count = agent.total_count ?? 0;
                        const matchesName = !agentSearch.trim() || name.includes(agentSearch.trim().toLowerCase());
                        const matchesCount = count >= agentMinCount;
                        return matchesName && matchesCount;
                      }).length
                    }
                  </span>
                  <button className="button ghost" type="button" onClick={() => handleMemoryAction("memory.active_agents")} disabled={isBusy}>
                    Обновить
                  </button>
                </div>
                {activeAgents.length ? (
                  <div className="cards profile-grid">
                    {activeAgents
                      .filter((agent) => {
                        const name = (agent.agent_name ?? "").toLowerCase();
                        const count = agent.total_count ?? 0;
                        const matchesName = !agentSearch.trim() || name.includes(agentSearch.trim().toLowerCase());
                        const matchesCount = count >= agentMinCount;
                        return matchesName && matchesCount;
                      })
                      .map((agent, idx) => (
                        <article key={`${agent.agent_name ?? idx}`} className="card">
                          <div className="card-title">{agent.agent_name ?? "—"}</div>
                          <div className="card-description">Записей: {agent.total_count ?? "—"}</div>
                          <div className="button-row">
                            <button
                              className="button ghost"
                              type="button"
                              onClick={() => {
                                setMemoryClearForm((prev) => ({ ...prev, agentName: agent.agent_name ?? "" }));
                                setAgentsModalOpen(false);
                              }}
                            >
                              Выбрать
                            </button>
                          </div>
                        </article>
                      ))}
                  </div>
                ) : (
                  <div className="card-description">Список пуст — обновите данные.</div>
                )}
              </div>
            </div>,
            document.body
          )
        : null}
    </div>
  );
}
