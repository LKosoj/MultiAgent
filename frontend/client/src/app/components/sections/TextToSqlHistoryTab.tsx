"use client";

import { useMemo } from "react";
import { textToSqlHistoryRowCount, type TextToSqlHistorySummary } from "../../lib/textToSqlContracts";

const HISTORY_DISPLAY_LIMIT = 20;

type Props = {
  history: TextToSqlHistorySummary[];
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  analytics: any | null;
  loadHistory: () => Promise<void>;
  clearHistory: () => Promise<void>;
  isBusy: boolean;
  historyFilterStatus: string;
  setHistoryFilterStatus: (value: string) => void;
  historyFilterDialect: string;
  setHistoryFilterDialect: (value: string) => void;
  historySearch: string;
  setHistorySearch: (value: string) => void;
};

export function TextToSqlHistoryTab({
  history,
  analytics,
  loadHistory,
  clearHistory,
  isBusy,
  historyFilterStatus,
  setHistoryFilterStatus,
  historyFilterDialect,
  setHistoryFilterDialect,
  historySearch,
  setHistorySearch,
}: Props) {
  const historyDialects = useMemo(() => {
    const values = new Set<string>();
    history.forEach((entry) => {
      const dialect = entry.dialect;
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
        const dialect = entry.dialect;
        if (dialect !== historyFilterDialect) return false;
      }
      if (historySearch.trim()) {
        const text = `${entry.sql_query} ${entry.natural_query ?? ""}`.toLowerCase();
        if (!text.includes(historySearch.toLowerCase())) return false;
      }
      return true;
    });
  }, [history, historyFilterStatus, historyFilterDialect, historySearch]);

  return (
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
          {filteredHistory.slice(-HISTORY_DISPLAY_LIMIT).reverse().map((item, idx) => (
            <article key={idx} className="card">
              <div className="card-title">{item.sql_query || "SQL не сохранён"}</div>
              <div className="card-description">{item.natural_query ?? ""}</div>
              <div className="profile-meta">
                <div>
                  <span className="label">Подключение</span>
                  <div className="meta-value">{item.connection_ref ?? "—"}</div>
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
                  <span className="label">Строк в результате</span>
                  <div className="meta-value">{textToSqlHistoryRowCount(item) ?? "—"}</div>
                </div>
                <div>
                  <span className="label">Лимит строк</span>
                  <div className="meta-value">{item.max_rows ?? "—"}</div>
                </div>
              </div>
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
                  // eslint-disable-next-line @typescript-eslint/no-explicit-any
                  ? analytics.dialects.map((d: any) => `${d.dialect}:${d.count}`).join(", ")
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
  );
}
