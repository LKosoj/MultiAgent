"use client";

import React, { useEffect, useMemo, useRef, useState } from "react";
import { TextToSqlResultModal } from "./TextToSqlResultModal";
import { useTextToSqlRun } from "./useTextToSqlRun";
import { useRunLogs } from "./useRunLogs";
import { useTextToSqlHistory } from "./useTextToSqlHistory";
import { TextToSqlConnectionsTab } from "./TextToSqlConnectionsTab";
import { TextToSqlSchemaTab } from "./TextToSqlSchemaTab";
import { TextToSqlHistoryTab } from "./TextToSqlHistoryTab";
import { TextToSqlGenerateTab } from "./TextToSqlGenerateTab";
import { TextToSqlMetadataTab } from "./TextToSqlMetadataTab";
import {
  type TextToSqlStartResponse,
} from "../../utils/textToSqlStart";
import {
  createTextToSqlClient,
  type TextToSqlConnection,
} from "../../lib/textToSqlClient";
import {
  textToSqlRunTerminalStatus,
} from "../../lib/textToSqlContracts";
import { type TextToSqlMetadataView } from "../../lib/textToSqlMetadataContracts";

export {
  buildTextToSqlSchemaPayload,
  buildTextToSqlStartPayload,
  isTextToSqlConnectionReference,
  textToSqlConnectionEntries,
} from "../../lib/textToSqlClient";
export type { TextToSqlConnection } from "../../lib/textToSqlClient";
export {
  isTextToSqlHistorySummary,
  textToSqlHistoryEntries,
  textToSqlHistoryRowCount,
  textToSqlHistoryTerminalState,
  textToSqlRunTerminalStatus,
} from "../../lib/textToSqlContracts";
export type { TextToSqlHistorySummary } from "../../lib/textToSqlContracts";
export type { TextToSqlMetadataView } from "../../lib/textToSqlMetadataContracts";
export {
  beginTextToSqlRunSelection,
  cancelTextToSqlResultRetries,
  commitTextToSqlRunSelection,
  createTextToSqlResultLoadState,
  invalidateTextToSqlRunSelection,
  loadTextToSqlResultSingleFlight,
  loadTextToSqlResultThenArtifacts,
  loadTextToSqlStatusAfterCurrent,
  loadTextToSqlStatusSingleFlight,
  requestTextToSqlCancellation,
  scheduleTextToSqlResultRetry,
  selectTextToSqlRun,
  startTextToSqlStatusPolling,
  textToSqlRunCanCancel,
  textToSqlStatusForRun,
} from "../../lib/textToSqlRunState";

type Props = {
  runServiceAction: (action: string, payload: Record<string, unknown>) => Promise<unknown>;
  startTextToSqlRun: (
    payload: Record<string, unknown>,
  ) => Promise<TextToSqlStartResponse>;
  isBusy: boolean;
  active: boolean;
  isAdmin: boolean;
  notify?: (msg: string, type: "error" | "success" | "info") => void;
};

export function TextToSqlSection({ runServiceAction, startTextToSqlRun, isBusy, active, isAdmin, notify }: Props) {
  const textToSqlClient = useMemo(
    () => createTextToSqlClient(runServiceAction, startTextToSqlRun),
    [runServiceAction, startTextToSqlRun],
  );
  const [tab, setTab] = useState<"generate" | "connections" | "schema" | "metadata" | "history">("generate");
  const [prompt, setPrompt] = useState("");
  const [naturalQuery, setNaturalQuery] = useState("");
  const [connectionRef, setConnectionRef] = useState("");
  const [sessionId, setSessionId] = useState("");
  const [maxRows, setMaxRows] = useState("100");
  const [enableTelemetry, setEnableTelemetry] = useState(true);
  const [safetyLevel, setSafetyLevel] = useState("strict");
  const [includeExplanation, setIncludeExplanation] = useState(true);
  const [validateSchema, setValidateSchema] = useState(true);
  const [dryRunOnly, setDryRunOnly] = useState(false);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [connections, setConnections] = useState<TextToSqlConnection[]>([]);
  const [schemaFilter, setSchemaFilter] = useState("");
  const [tableFilter, setTableFilter] = useState("");
  const [allowDbSchemaFallback, setAllowDbSchemaFallback] = useState(false);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const [schemaResult, setSchemaResult] = useState<any | null>(null);
  const [metadataView, setMetadataView] = useState<TextToSqlMetadataView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadedConnections, setLoadedConnections] = useState(false);
  const [historyFilterStatus, setHistoryFilterStatus] = useState("all");
  const [historyFilterDialect, setHistoryFilterDialect] = useState("all");
  const [historySearch, setHistorySearch] = useState("");
  const resetRunLogsRef = useRef<() => void>(() => undefined);

  const {
    result,
    runId,
    runStatus,
    runArtifacts,
    workflowResult,
    isCancelling,
    autoRefreshRun,
    setAutoRefreshRun,
    resultModal,
    setResultModal,
    reportError,
    commitSelectedRun,
    isRunSelected,
    handleGenerate: runGenerate,
    handleGenerateReport,
    handleCancelRun,
    loadRunStatus,
    loadRunResult,
  } = useTextToSqlRun({
    active,
    runServiceAction,
    textToSqlClient,
    notify,
    setError,
    resetRunLogs: () => resetRunLogsRef.current(),
  });

  const {
    runLogsAutoRefresh,
    setRunLogsAutoRefresh,
    loadRunLogs,
    formattedRunLogs,
    resetRunLogs,
  } = useRunLogs({
    active,
    runId,
    runStatus,
    runServiceAction,
    isRunSelected,
    commitSelectedRun,
  });
  resetRunLogsRef.current = resetRunLogs;

  const { history, analytics, loadHistory, clearHistory } = useTextToSqlHistory({
    tab,
    textToSqlClient,
    setError,
  });

  const loadConnections = async () => {
    setError(null);
    try {
      const items = await textToSqlClient.listConnections();
      setConnections(items);
      setConnectionRef((current) => (
        items.some((item) => item.connection_ref === current)
          ? current
          : items[0]?.connection_ref ?? ""
      ));
      setLoadedConnections(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось получить подключения");
    }
  };

  useEffect(() => {
    if (active && !loadedConnections) {
      void loadConnections();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, loadedConnections]);

  const runStatusValue = textToSqlRunTerminalStatus(runStatus)
    ?? runStatus?.status
    ?? runStatus?.state
    ?? "";

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
        <button className={`segment-button${tab === "metadata" ? " active" : ""}`} onClick={() => setTab("metadata")}>
          Метаданные
        </button>
        <button className={`segment-button${tab === "history" ? " active" : ""}`} onClick={() => setTab("history")}>
          История
        </button>
      </div>

      {tab === "generate" ? (
        <TextToSqlGenerateTab
          connectionRef={connectionRef}
          setConnectionRef={setConnectionRef}
          connections={connections}
          isBusy={isBusy}
          error={error}
          setError={setError}
          result={result}
          runId={runId}
          runStatus={runStatus}
          runArtifacts={runArtifacts}
          workflowResult={workflowResult}
          runStatusValue={runStatusValue}
          autoRefreshRun={autoRefreshRun}
          setAutoRefreshRun={setAutoRefreshRun}
          isCancelling={isCancelling}
          handleCancelRun={handleCancelRun}
          loadRunStatus={loadRunStatus}
          loadRunResult={loadRunResult}
          handleGenerateReport={handleGenerateReport}
          reportError={reportError}
          setResultModal={setResultModal}
          runLogsAutoRefresh={runLogsAutoRefresh}
          setRunLogsAutoRefresh={setRunLogsAutoRefresh}
          loadRunLogs={loadRunLogs}
          formattedRunLogs={formattedRunLogs}
          loadConnections={loadConnections}
          onGenerate={runGenerate}
          prompt={prompt}
          setPrompt={setPrompt}
          naturalQuery={naturalQuery}
          setNaturalQuery={setNaturalQuery}
          sessionId={sessionId}
          setSessionId={setSessionId}
          maxRows={maxRows}
          setMaxRows={setMaxRows}
          enableTelemetry={enableTelemetry}
          setEnableTelemetry={setEnableTelemetry}
          safetyLevel={safetyLevel}
          setSafetyLevel={setSafetyLevel}
          includeExplanation={includeExplanation}
          setIncludeExplanation={setIncludeExplanation}
          validateSchema={validateSchema}
          setValidateSchema={setValidateSchema}
          dryRunOnly={dryRunOnly}
          setDryRunOnly={setDryRunOnly}
          isSubmitting={isSubmitting}
          setIsSubmitting={setIsSubmitting}
        />
      ) : null}

      {tab === "connections" ? (
        <TextToSqlConnectionsTab
          connections={connections}
          connectionRef={connectionRef}
          setConnectionRef={setConnectionRef}
          loadConnections={loadConnections}
          isBusy={isBusy}
        />
      ) : null}

      {tab === "schema" ? (
        <TextToSqlSchemaTab
          connectionRef={connectionRef}
          setConnectionRef={setConnectionRef}
          connections={connections}
          textToSqlClient={textToSqlClient}
          setError={setError}
          isBusy={isBusy}
          schemaFilter={schemaFilter}
          setSchemaFilter={setSchemaFilter}
          tableFilter={tableFilter}
          setTableFilter={setTableFilter}
          allowDbSchemaFallback={allowDbSchemaFallback}
          setAllowDbSchemaFallback={setAllowDbSchemaFallback}
          schemaResult={schemaResult}
          setSchemaResult={setSchemaResult}
        />
      ) : null}

      {tab === "metadata" ? (
        <TextToSqlMetadataTab
          connectionRef={connectionRef}
          setConnectionRef={setConnectionRef}
          connections={connections}
          textToSqlClient={textToSqlClient}
          isBusy={isBusy}
          isAdmin={isAdmin}
          setError={setError}
          metadataView={metadataView}
          setMetadataView={setMetadataView}
        />
      ) : null}

      {tab === "history" ? (
        <TextToSqlHistoryTab
          history={history}
          analytics={analytics}
          loadHistory={loadHistory}
          clearHistory={clearHistory}
          isBusy={isBusy}
          historyFilterStatus={historyFilterStatus}
          setHistoryFilterStatus={setHistoryFilterStatus}
          historyFilterDialect={historyFilterDialect}
          setHistoryFilterDialect={setHistoryFilterDialect}
          historySearch={historySearch}
          setHistorySearch={setHistorySearch}
        />
      ) : null}

      <TextToSqlResultModal
        open={resultModal.open}
        runId={resultModal.runId}
        active={active}
        isBusy={isBusy}
        runStatusValue={runStatusValue}
        result={result}
        workflowResult={workflowResult}
        onClose={() => setResultModal({ open: false, runId: null })}
        onGenerateReport={handleGenerateReport}
      />
    </div>
  );
}
