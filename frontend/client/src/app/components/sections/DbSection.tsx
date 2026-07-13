"use client";

import React from "react";
import { KeyValueList } from "../shared/KeyValueList";

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
  connection_ref?: string;
  description?: string;
  created_at?: string;
};

export type DbConnectionRecord = {
  connection_ref: string;
  display_name: string;
  owner_subject: string | null;
  tenant_id: string;
  target_kind?: string;
  dialect?: string;
  target_description?: string;
  created_at?: string;
  enabled_for_user?: boolean;
};

type DbConnectionRegistrationInput = {
  displayName: string;
  dsn: string;
  ownerSubject: string;
  tenantId: string;
};

type Props = {
  isBusy: boolean;
  runServiceAction?: (action: string, payload: Record<string, unknown>) => Promise<unknown>;
  dbTab: "plugins" | "test" | "configs" | "diagnostics";
  setDbTab: (tab: Props["dbTab"]) => void;
  dbPlugins: DbPlugin[];
  dbPluginsLoading: boolean;
  dbPluginsError: string | null;
  dbTestForm: {
    dsn: string;
    timeout: number;
    testBasic: boolean;
    testSchema: boolean;
    testSecurity: boolean;
    verbose: boolean;
  };
  dbTestResult: unknown;
  dbConfigs: DbTestConfig[];
  dbConfigsLoading: boolean;
  dbConfigsError: string | null;
  dbConfigForm: { name: string; dsn: string; description: string };
  dbDiagnostics: { system_info?: Record<string, unknown>; plugin_status?: any[]; dependency_status?: any[] } | null;
  dbBenchmark: { results?: any[]; summary?: Record<string, unknown> } | null;
  loadDbPlugins: () => void;
  handleQuickTestPlugin: (scheme: string) => void;
  setDbTestForm: React.Dispatch<
    React.SetStateAction<{ dsn: string; timeout: number; testBasic: boolean; testSchema: boolean; testSecurity: boolean; verbose: boolean }>
  >;
  handleRunDbTest: () => void;
  loadDbConfigs: () => void;
  setDbConfigForm: React.Dispatch<React.SetStateAction<{ name: string; dsn: string; description: string }>>;
  handleSaveDbConfig: () => void;
  handleDeleteDbConfig: (name: string) => void;
  setDbTestResult: React.Dispatch<React.SetStateAction<unknown>>;
  loadDbDiagnostics: () => void;
  loadDbBenchmark: () => void;
};

const isRecord = (value: unknown): value is Record<string, unknown> => (
  value !== null && typeof value === "object" && !Array.isArray(value)
);

export function dbConnectionEntries(payload: unknown): DbConnectionRecord[] {
  const result = isRecord(payload) && isRecord(payload.result)
    ? payload.result
    : payload;
  const values = Array.isArray(result)
    ? result
    : isRecord(result) && Array.isArray(result.connections)
      ? result.connections
      : [];

  return values.flatMap((value): DbConnectionRecord[] => {
    if (
      !isRecord(value)
      || typeof value.connection_ref !== "string"
      || typeof value.display_name !== "string"
      || !(typeof value.owner_subject === "string" || value.owner_subject === null)
      || typeof value.tenant_id !== "string"
    ) return [];
    return [{
      connection_ref: value.connection_ref,
      display_name: value.display_name,
      owner_subject: value.owner_subject,
      tenant_id: value.tenant_id,
      ...(typeof value.target_kind === "string" ? { target_kind: value.target_kind } : {}),
      ...(typeof value.dialect === "string" ? { dialect: value.dialect } : {}),
      ...(typeof value.target_description === "string"
        ? { target_description: value.target_description }
        : {}),
      ...(typeof value.created_at === "string" ? { created_at: value.created_at } : {}),
      ...(typeof value.enabled_for_user === "boolean"
        ? { enabled_for_user: value.enabled_for_user }
        : {}),
    }];
  });
}

export function buildDbConnectionRegistrationPayload(
  input: DbConnectionRegistrationInput,
): Record<string, unknown> {
  return {
    display_name: input.displayName.trim(),
    dsn: input.dsn,
    owner_subject: input.ownerSubject.trim() || null,
    tenant_id: input.tenantId.trim(),
  };
}

const toStringList = (value: unknown): string[] => {
  if (Array.isArray(value)) {
    return value.map((item) => String(item)).filter(Boolean);
  }
  if (typeof value === "string") {
    return value ? [value] : [];
  }
  if (value && typeof value === "object") {
    return Object.values(value).map((item) => String(item)).filter(Boolean);
  }
  return [];
};

export function DbSection({
  isBusy,
  runServiceAction,
  dbTab,
  setDbTab,
  dbPlugins,
  dbPluginsLoading,
  dbPluginsError,
  dbTestForm,
  dbTestResult,
  dbConfigForm,
  dbDiagnostics,
  dbBenchmark,
  loadDbPlugins,
  handleQuickTestPlugin,
  setDbTestForm,
  handleRunDbTest,
  setDbConfigForm,
  setDbTestResult,
  loadDbDiagnostics,
  loadDbBenchmark,
}: Props) {
  const [selectedPlugin, setSelectedPlugin] = React.useState("");
  const [connections, setConnections] = React.useState<DbConnectionRecord[]>([]);
  const [connectionsLoading, setConnectionsLoading] = React.useState(false);
  const [connectionsError, setConnectionsError] = React.useState<string | null>(null);
  const [ownerSubject, setOwnerSubject] = React.useState("");
  const [tenantId, setTenantId] = React.useState("");
  const pluginOptions = dbPlugins.map((plugin) => ({
    label: `${plugin.scheme ?? "db"} - ${plugin.name ?? "plugin"}`,
    value: plugin.scheme ?? "",
  }));
  const activePlugin = dbPlugins.find((plugin) => plugin.scheme === selectedPlugin);
  const activePluginDsnExamples = toStringList(activePlugin?.dsn_examples);

  const loadConnections = React.useCallback(async () => {
    if (!runServiceAction) return;
    setConnectionsLoading(true);
    setConnectionsError(null);
    try {
      const result = await runServiceAction("db.connections.list", {});
      setConnections(dbConnectionEntries(result));
    } catch (error) {
      setConnectionsError(
        error instanceof Error ? error.message : "Не удалось загрузить подключения",
      );
    } finally {
      setConnectionsLoading(false);
    }
  }, [runServiceAction]);

  React.useEffect(() => {
    if (dbTab === "configs" && runServiceAction) void loadConnections();
  }, [dbTab, loadConnections, runServiceAction]);

  const registerConnection = async () => {
    if (!runServiceAction) {
      setConnectionsError("Управление подключениями недоступно");
      return;
    }
    if (
      !dbConfigForm.name.trim()
      || !dbConfigForm.dsn.trim()
      || !tenantId.trim()
    ) {
      setConnectionsError("Имя, DSN и tenant обязательны");
      return;
    }
    setConnectionsError(null);
    try {
      await runServiceAction(
        "db.connections.register",
        buildDbConnectionRegistrationPayload({
          displayName: dbConfigForm.name,
          dsn: dbConfigForm.dsn,
          ownerSubject,
          tenantId,
        }),
      );
      setDbConfigForm({ name: "", dsn: "", description: "" });
      setOwnerSubject("");
      setTenantId("");
      await loadConnections();
    } catch (error) {
      setConnectionsError(
        error instanceof Error ? error.message : "Не удалось зарегистрировать подключение",
      );
    }
  };

  const deleteConnection = async (connectionRef: string) => {
    if (!runServiceAction) return;
    setConnectionsError(null);
    try {
      await runServiceAction("db.connections.delete", {
        connection_ref: connectionRef,
      });
      await loadConnections();
    } catch (error) {
      setConnectionsError(
        error instanceof Error ? error.message : "Не удалось удалить подключение",
      );
    }
  };

  return (
    <div className="section" id="db">
      <div className="section-header">
        <div className="section-title">DB плагины</div>
        <div className="section-hint">Тесты и диагностика подключений</div>
      </div>
      <div className="segment-row">
        <button className={`segment-button${dbTab === "plugins" ? " active" : ""}`} onClick={() => setDbTab("plugins")}>
          Плагины
        </button>
        <button className={`segment-button${dbTab === "test" ? " active" : ""}`} onClick={() => setDbTab("test")}>
          Тестирование
        </button>
        <button className={`segment-button${dbTab === "configs" ? " active" : ""}`} onClick={() => setDbTab("configs")}>
          Подключения
        </button>
        <button className={`segment-button${dbTab === "diagnostics" ? " active" : ""}`} onClick={() => setDbTab("diagnostics")}>
          Диагностика
        </button>
      </div>

      {dbTab === "plugins" ? (
        <div className="stack">
          <div className="toolbar-row">
            <div className="inline">
              <button className="button secondary" type="button" onClick={loadDbPlugins} disabled={isBusy}>
                Обновить список
              </button>
              <span className="status-chip">{dbPluginsLoading ? "Загрузка..." : `Найдено: ${dbPlugins.length}`}</span>
            </div>
            {dbPluginsError ? <div className="card-description">Ошибка: {dbPluginsError}</div> : null}
          </div>
          <div className="cards">
            <article className="card">
              <div className="card-title">Всего плагинов</div>
              <div className="hero-number">{dbPlugins.length}</div>
            </article>
            <article className="card">
              <div className="card-title">Типов БД</div>
              <div className="hero-number">{new Set(dbPlugins.map((p) => p.scheme).filter(Boolean)).size}</div>
            </article>
            <article className="card">
              <div className="card-title">Возможностей</div>
              <div className="hero-number">
                {dbPlugins.reduce((sum, plugin) => sum + toStringList(plugin.supported_features).length, 0)}
              </div>
            </article>
          </div>
          <div className="cards profile-grid">
            {dbPlugins.map((plugin, index) => {
              const name = plugin.name || `plugin-${index + 1}`;
              const supportedFeatures = toStringList(plugin.supported_features);
              const dsnExamples = toStringList(plugin.dsn_examples);
              return (
                <article key={`${name}-${plugin.scheme ?? index}`} className="card profile-card">
                  <div className="inline">
                    <div className="card-title">{name}</div>
                    <span className="status-tag" data-status={plugin.scheme ?? "db"}>
                      {plugin.scheme ?? "db"}
                    </span>
                  </div>
                  <div className="card-description">{plugin.description || "Описание отсутствует."}</div>
                  <div className="profile-meta">
                    <div>
                      <span className="label">Диалект</span>
                      <div className="meta-value">{plugin.dialect_label || "—"}</div>
                    </div>
                    <div>
                      <span className="label">Возможности</span>
                      <div className="meta-value">{supportedFeatures.length}</div>
                    </div>
                  </div>
                  {supportedFeatures.length ? (
                    <div className="card-hint">Фичи: {supportedFeatures.slice(0, 5).join(", ")}</div>
                  ) : null}
                  {dsnExamples.length ? (
                    <details className="details">
                      <summary>Примеры DSN</summary>
                      <div className="kv-list">
                        {dsnExamples.map((dsn) => (
                          <div key={dsn} className="kv-row">
                            <div className="kv-key">DSN</div>
                            <div className="kv-value" style={{ fontFamily: "var(--font-mono)", wordBreak: "break-all" }}>
                              {dsn}
                            </div>
                          </div>
                        ))}
                      </div>
                    </details>
                  ) : null}
                  <div className="button-row">
                    {plugin.scheme ? (
                      <button className="button" type="button" onClick={() => handleQuickTestPlugin(plugin.scheme!)} disabled={isBusy}>
                        Быстрый тест
                      </button>
                    ) : null}
                  </div>
                </article>
              );
            })}
          </div>
          {dbTestResult ? (
            <div className="card">
              <div className="card-title">Результат теста</div>
              <KeyValueList data={dbTestResult} />
              <button className="button ghost" type="button" onClick={() => setDbTestResult(null)}>
                Очистить результат
              </button>
            </div>
          ) : null}
          <details className="details">
            <summary>Информация о системе плагинов</summary>
            <div className="card-description">
              Плагины БД поддерживают подключение, интроспекцию схемы, безопасные SELECT‑запросы и валидацию SQL. Лимиты
              строк применяются через API плагина, а схемы указываются в DSN.
            </div>
          </details>
        </div>
      ) : null}

      {dbTab === "test" ? (
        <div className="card">
          <div className="section-header">
            <div className="card-title">Комплексное тестирование</div>
            <div className="card-description">Проверка подключения, схемы и безопасности</div>
          </div>
          <div className="form-grid">
            <label className="field">
              <span className="label">Плагин</span>
              <select
                value={selectedPlugin}
                onChange={(event) => {
                  setSelectedPlugin(event.target.value);
                }}
              >
                <option value="">--</option>
                {pluginOptions.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span className="label">DSN</span>
              <input
                value={dbTestForm.dsn}
                placeholder="scheme://user:pass@host:port/db.schema"
                onChange={(event) => setDbTestForm((prev) => ({ ...prev, dsn: event.target.value }))}
              />
            </label>
            <label className="field">
              <span className="label">Таймаут (сек)</span>
              <input
                value={dbTestForm.timeout}
                onChange={(event) => setDbTestForm((prev) => ({ ...prev, timeout: Number(event.target.value) || 0 }))}
              />
            </label>
          </div>
          {activePluginDsnExamples.length ? (
            <details className="details">
              <summary>Примеры DSN</summary>
              <div className="graph-inputs">
                {activePluginDsnExamples.map((dsn) => (
                  <div key={dsn} className="graph-input">
                    <div className="meta-value" style={{ wordBreak: "break-all" }}>
                      {dsn}
                    </div>
                    <button
                      className="button ghost"
                      type="button"
                      onClick={() => setDbTestForm((prev) => ({ ...prev, dsn }))}
                    >
                      Использовать
                    </button>
                  </div>
                ))}
              </div>
            </details>
          ) : null}
          <div className="toggle-grid">
            <label className="toggle">
              <input
                type="checkbox"
                checked={dbTestForm.testBasic}
                onChange={(event) => setDbTestForm((prev) => ({ ...prev, testBasic: event.target.checked }))}
              />
              <span>Тестовый SELECT</span>
            </label>
            <label className="toggle">
              <input
                type="checkbox"
                checked={dbTestForm.testSchema}
                onChange={(event) => setDbTestForm((prev) => ({ ...prev, testSchema: event.target.checked }))}
              />
              <span>Интроспекция схемы</span>
            </label>
            <label className="toggle">
              <input
                type="checkbox"
                checked={dbTestForm.testSecurity}
                onChange={(event) => setDbTestForm((prev) => ({ ...prev, testSecurity: event.target.checked }))}
              />
              <span>Проверка безопасности</span>
            </label>
            <label className="toggle">
              <input
                type="checkbox"
                checked={dbTestForm.verbose}
                onChange={(event) => setDbTestForm((prev) => ({ ...prev, verbose: event.target.checked }))}
              />
              <span>Подробный вывод</span>
            </label>
          </div>
          <div className="button-row">
            <button className="button" type="button" onClick={handleRunDbTest} disabled={isBusy}>
              Запустить тест
            </button>
          </div>
          {dbTestResult ? (
            <div className="card" style={{ background: "var(--panel-strong)" }}>
              <div className="card-title">Результат</div>
              <KeyValueList data={dbTestResult} />
            </div>
          ) : null}
        </div>
      ) : null}

      {dbTab === "configs" ? (
        <div className="stack">
          <div className="toolbar-row">
            <div className="inline">
              <button className="button secondary" type="button" onClick={loadConnections} disabled={isBusy || !runServiceAction}>
                Обновить
              </button>
            </div>
            {connectionsError ? <div className="card-description">Ошибка: {connectionsError}</div> : null}
          </div>
          <div className="card">
            <div className="card-title">Зарегистрировать подключение</div>
            <div className="form-grid">
              <label className="field">
                <span className="label">Отображаемое имя</span>
                <input value={dbConfigForm.name} onChange={(event) => setDbConfigForm((prev) => ({ ...prev, name: event.target.value }))} />
              </label>
              <label className="field">
                <span className="label">DSN</span>
                <input value={dbConfigForm.dsn} onChange={(event) => setDbConfigForm((prev) => ({ ...prev, dsn: event.target.value }))} />
              </label>
              <label className="field">
                <span className="label">Владелец (пусто — весь tenant)</span>
                <input
                  value={ownerSubject}
                  onChange={(event) => setOwnerSubject(event.target.value)}
                  placeholder="owner subject"
                />
              </label>
              <label className="field">
                <span className="label">Tenant</span>
                <input
                  value={tenantId}
                  onChange={(event) => setTenantId(event.target.value)}
                  placeholder="tenant id"
                />
              </label>
            </div>
            <div className="button-row">
              <button
                className="button"
                type="button"
                onClick={registerConnection}
                disabled={
                  isBusy
                  || !runServiceAction
                  || !dbConfigForm.name.trim()
                  || !dbConfigForm.dsn.trim()
                  || !tenantId.trim()
                }
              >
                Зарегистрировать
              </button>
            </div>
          </div>

          <div className="cards">
            {connectionsLoading ? <div className="card-description">Загрузка подключений...</div> : null}
            {connections.map((connection) => (
              <article key={connection.connection_ref} className="card">
                <div className="inline">
                  <div className="card-title">{connection.display_name}</div>
                  <span className="app-subtitle">{connection.created_at || ""}</span>
                </div>
                <div className="card-description">{connection.target_description || "Описание цели отсутствует."}</div>
                <div className="profile-meta">
                  <div>
                    <span className="label">Владелец</span>
                    <div className="meta-value">{connection.owner_subject ?? "tenant-wide"}</div>
                  </div>
                  <div>
                    <span className="label">Tenant</span>
                    <div className="meta-value">{connection.tenant_id}</div>
                  </div>
                  <div>
                    <span className="label">Ссылка</span>
                    <div className="meta-value">{connection.connection_ref}</div>
                  </div>
                </div>
                <div className="button-row">
                  <button className="button secondary" type="button" onClick={() => deleteConnection(connection.connection_ref)} disabled={isBusy}>
                    Удалить
                  </button>
                </div>
              </article>
            ))}
          </div>
        </div>
      ) : null}

      {dbTab === "diagnostics" ? (
        <div className="stack">
          <div className="toolbar-row">
            <div className="inline">
              <button className="button secondary" type="button" onClick={loadDbDiagnostics} disabled={isBusy}>
                Обновить диагностику
              </button>
              <button className="button ghost" type="button" onClick={loadDbBenchmark} disabled={isBusy}>
                Запустить бенчмарк
              </button>
            </div>
          </div>
          {(dbDiagnostics as any)?.error ? <div className="card-description">Ошибка: {(dbDiagnostics as any).error}</div> : null}
          {dbDiagnostics?.system_info ? (
            <div className="card">
              <div className="card-title">Системная информация</div>
              <div className="profile-meta">
                {Object.entries(dbDiagnostics.system_info).map(([key, value]) => (
                  <div key={key}>
                    <span className="label">{key}</span>
                    <div className="meta-value">{String(value)}</div>
                  </div>
                ))}
              </div>
            </div>
          ) : null}
          {dbDiagnostics?.plugin_status ? (
            <div className="card">
              <div className="card-title">Статус плагинов</div>
              <div className="cards profile-grid">
                {dbDiagnostics.plugin_status.map((item, idx) => (
                  <article key={idx} className="card">
                    <div className="card-title">{item.plugin ?? item.scheme}</div>
                    <div className="profile-meta">
                      <div>
                        <span className="label">Модуль</span>
                        <div className="meta-value">{item.module ?? "—"}</div>
                      </div>
                      <div>
                        <span className="label">Загружен</span>
                        <div className="meta-value">{item.loaded ? "OK" : "—"}</div>
                      </div>
                      <div>
                        <span className="label">Класс</span>
                        <div className="meta-value">{item.plugin_class ? "OK" : "—"}</div>
                      </div>
                    </div>
                    {item.error ? <div className="card-description">Ошибка: {String(item.error)}</div> : null}
                  </article>
                ))}
              </div>
            </div>
          ) : null}
          {dbDiagnostics?.dependency_status ? (
            <div className="card">
              <div className="card-title">Зависимости</div>
              <div className="cards profile-grid">
                {dbDiagnostics.dependency_status.map((item, idx) => (
                  <article key={idx} className="card">
                    <div className="card-title">{item.package}</div>
                    <div className="card-description">{item.description}</div>
                    <div className="profile-meta">
                      <div>
                        <span className="label">Статус</span>
                        <div className="meta-value">{String(item.status)}</div>
                      </div>
                      <div>
                        <span className="label">Версия</span>
                        <div className="meta-value">{item.version || "—"}</div>
                      </div>
                    </div>
                  </article>
                ))}
              </div>
            </div>
          ) : null}
          {dbBenchmark?.results ? (
            <div className="card">
              <div className="card-title">Бенчмарк</div>
              <div className="cards profile-grid">
                {dbBenchmark.results.map((item, idx) => (
                  <article key={idx} className="card">
                    <div className="card-title">{item.plugin}</div>
                    <div className="profile-meta">
                      <div>
                        <span className="label">Схема</span>
                        <div className="meta-value">{item.scheme}</div>
                      </div>
                      <div>
                        <span className="label">Загрузка</span>
                        <div className="meta-value">{item.load_time_ms ?? "—"} ms</div>
                      </div>
                      <div>
                        <span className="label">DSN</span>
                        <div className="meta-value">{item.dsn_validation_ms ?? "—"} ms</div>
                      </div>
                    </div>
                    <div className="card-description">Статус: {item.status}</div>
                  </article>
                ))}
              </div>
            </div>
          ) : null}
          {dbBenchmark?.summary ? (
            <div className="card" style={{ background: "var(--panel-strong)" }}>
              <div className="card-title">Сводка бенчмарка</div>
              <div className="profile-meta">
                {Object.entries(dbBenchmark.summary).map(([key, value]) => (
                  <div key={key}>
                    <span className="label">{key}</span>
                    <div className="meta-value">{String(value)}</div>
                  </div>
                ))}
              </div>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
