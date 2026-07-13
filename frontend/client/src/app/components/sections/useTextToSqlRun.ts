"use client";

import { useEffect, useRef, useState } from "react";
import {
  prepareTextToSqlStart,
  type PendingTextToSqlStart,
} from "../../utils/textToSqlStart";
import {
  buildTextToSqlStartPayload,
  createTextToSqlClient,
  type TextToSqlStartPayloadInput,
} from "../../lib/textToSqlClient";
import { isRecord, textToSqlRunTerminalStatus } from "../../lib/textToSqlContracts";
import {
  beginTextToSqlRunSelection,
  cancelTextToSqlResultRetries,
  commitTextToSqlRunSelection,
  createTextToSqlResultLoadState,
  invalidateTextToSqlRunSelection,
  isConfirmedTextToSqlResult,
  isConfirmedTextToSqlResultForRun,
  loadTextToSqlResultSingleFlight,
  loadTextToSqlResultThenArtifacts,
  loadTextToSqlStatusAfterCurrent,
  loadTextToSqlStatusSingleFlight,
  requestTextToSqlCancellation,
  scheduleTextToSqlResultRetry,
  selectTextToSqlRun,
  startTextToSqlStatusPolling,
  TEXT_TO_SQL_RESULT_RETRY_DELAY_MS,
  textToSqlRunCanCancel,
  textToSqlStatusForRun,
  type TextToSqlResultLoadState,
  type TextToSqlRunSelection,
  type TextToSqlStatusLoads,
} from "../../lib/textToSqlRunState";

export const extractFinalOutput = (payload: unknown) => {
  if (!payload) return null;
  if (typeof payload !== "object") return payload;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const typed = payload as any;
  if (typed?.artifacts?.final_output !== undefined) return typed.artifacts.final_output;
  if (typed?.final_output !== undefined) return typed.final_output;
  if (typed?.result?.final_output !== undefined) return typed.result.final_output;
  return payload;
};

export const extractSqlCandidate = (payload: unknown, depth = 0): string | null => {
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

type UseTextToSqlRunParams = {
  active: boolean;
  runServiceAction: (action: string, payload: Record<string, unknown>) => Promise<unknown>;
  textToSqlClient: ReturnType<typeof createTextToSqlClient>;
  notify?: (msg: string, type: "error" | "success" | "info") => void;
  setError: (msg: string | null) => void;
  resetRunLogs: () => void;
};

export function useTextToSqlRun({
  active,
  runServiceAction,
  textToSqlClient,
  notify,
  setError,
  resetRunLogs,
}: UseTextToSqlRunParams) {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const [result, setResult] = useState<any | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const [runStatus, setRunStatus] = useState<any | null>(null);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const [runArtifacts, setRunArtifacts] = useState<any | null>(null);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const [workflowResult, setWorkflowResult] = useState<any | null>(null);
  const [isCancelling, setIsCancelling] = useState(false);
  const [autoRefreshRun, setAutoRefreshRun] = useState(true);
  const [resultModal, setResultModal] = useState<{ open: boolean; runId: string | null }>({ open: false, runId: null });
  const [reportError, setReportError] = useState<string | null>(null);
  const selectedRunRef = useRef<TextToSqlRunSelection>({
    generation: 0,
    runId: null,
  });
  const resultLoadsRef = useRef<TextToSqlResultLoadState>(
    createTextToSqlResultLoadState(),
  );
  const resultRetryCancelsRef = useRef<Map<string, () => void>>(new Map());
  const runStatusInFlightRef = useRef<TextToSqlStatusLoads>(new Map());
  const pendingStartRef = useRef<PendingTextToSqlStart | null>(null);
  const cancelInFlightRef = useRef(false);

  useEffect(() => () => {
    invalidateTextToSqlRunSelection(selectedRunRef.current);
    cancelTextToSqlResultRetries(resultRetryCancelsRef.current);
  }, []);

  const commitSelectedRun = (targetRunId: string, commit: () => void) => (
    commitTextToSqlRunSelection(selectedRunRef.current, targetRunId, commit)
  );

  const isRunSelected = (targetRunId: string) => (
    selectedRunRef.current.runId === targetRunId
  );

  const handleGenerateReport = async () => {
    if (!runId) return;
    const targetRunId = runId;
    if (!commitSelectedRun(targetRunId, () => setReportError(null))) return;
    try {
      const resp = await runServiceAction("workflows.generate_report", {
        run_id: targetRunId,
      });
      if (selectedRunRef.current.runId !== targetRunId) return;
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const report = (resp as any)?.report ?? resp;
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      setWorkflowResult((current: any) => ({
        ...(isRecord(current) ? current : {}),
        report,
      }));
      setResultModal({ open: true, runId: targetRunId });
    } catch (err) {
      commitSelectedRun(targetRunId, () => {
        setReportError(
          err instanceof Error ? err.message : "Не удалось открыть отчёт",
        );
      });
    }
  };

  const handleGenerate = async (inputs: TextToSqlStartPayloadInput) => {
    let startGeneration: number | null = null;
    try {
      const startPayload = buildTextToSqlStartPayload(inputs);
      const prepared = prepareTextToSqlStart(
        startPayload,
        pendingStartRef.current,
        () => crypto.randomUUID(),
      );
      pendingStartRef.current = prepared.attempt;
      cancelTextToSqlResultRetries(resultRetryCancelsRef.current);
      // Fix C: очищаем состояние загрузок результата перед новым запуском.
      resultLoadsRef.current.confirmed.clear();
      resultLoadsRef.current.inFlight.clear();
      startGeneration = beginTextToSqlRunSelection(selectedRunRef.current);
      setError(null);
      setResult(null);
      setRunId(null);
      setRunStatus(null);
      setRunArtifacts(null);
      setWorkflowResult(null);
      resetRunLogs();
      setResultModal({ open: false, runId: null });
      setReportError(null);
      setIsCancelling(false);
      const resp = await textToSqlClient.start(prepared.payload);
      if (!selectTextToSqlRun(
        selectedRunRef.current,
        startGeneration,
        resp.run_id,
      )) return;
      setResult(resp);
      const nextRunId = resp.run_id;
      setRunId(nextRunId);
      pendingStartRef.current = null;
      void loadRunStatus(nextRunId);
    } catch (err) {
      if (
        startGeneration !== null
        && selectedRunRef.current.generation !== startGeneration
      ) return;
      const msg = err instanceof Error ? err.message : "Ошибка генерации SQL";
      setError(msg);
      notify?.(msg, "error");
    }
  };

  const loadRunStatus = async (runIdValue?: string | null) => {
    const targetRunId = runIdValue || runId;
    if (!targetRunId || selectedRunRef.current.runId !== targetRunId) return;
    return loadTextToSqlStatusSingleFlight(
      runStatusInFlightRef.current,
      targetRunId,
      async () => {
        try {
          const statusResp = await textToSqlClient.getStatus(targetRunId);
          const statusData = textToSqlStatusForRun(statusResp, targetRunId);
          if (!statusData) return null;
          if (!commitSelectedRun(targetRunId, () => {
            setError(null);
            setRunStatus(statusData);
          })) return null;

          const loadArtifacts = async () => {
            if (selectedRunRef.current.runId !== targetRunId) return null;
            const artifactsResp = await textToSqlClient.getArtifacts(targetRunId);
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            const artifactsData = (artifactsResp as any)?.artifacts
              ?? artifactsResp;
            commitSelectedRun(targetRunId, () => {
              setRunArtifacts(artifactsData);
            });
            return artifactsData;
          };

          void (async () => {
            if (!textToSqlRunTerminalStatus(statusData)) {
              await loadArtifacts().catch(() => null);
              return;
            }
            await loadTextToSqlResultThenArtifacts(
              () => loadRunResult(targetRunId),
              loadArtifacts,
              async () => undefined,
            );
          })().catch(() => undefined);
          return statusData;
        } catch (err) {
          commitSelectedRun(targetRunId, () => {
            setError(
              err instanceof Error
                ? err.message
                : "Не удалось получить статус запуска",
            );
          });
        }
      },
    );
  };

  const loadRunResult = async (
    runIdValue?: string | null,
    allowRetry = true,
  ): Promise<unknown | null | undefined> => {
    const targetRunId = runIdValue || runId;
    if (!targetRunId || selectedRunRef.current.runId !== targetRunId) return;
    let resp: unknown = null;
    try {
      resp = await loadTextToSqlResultSingleFlight(
        resultLoadsRef.current,
        targetRunId,
        () => textToSqlClient.getResult(targetRunId),
      );
    } catch {
      resp = null;
    }
    if (selectedRunRef.current.runId !== targetRunId) return null;
    const confirmed = isConfirmedTextToSqlResultForRun(resp, targetRunId);
    if (confirmed) {
      resultRetryCancelsRef.current.get(targetRunId)?.();
    } else if (allowRetry) {
      scheduleTextToSqlResultRetry(
        resultRetryCancelsRef.current,
        targetRunId,
        () => selectedRunRef.current.runId === targetRunId,
        async () => {
          const retried = await loadRunResult(targetRunId, false);
          if (
            isConfirmedTextToSqlResult(retried)
            && selectedRunRef.current.runId === targetRunId
          ) {
            void loadRunStatus(targetRunId);
          }
        },
        (retry) => {
          const timeoutId = window.setTimeout(
            retry,
            TEXT_TO_SQL_RESULT_RETRY_DELAY_MS,
          );
          return () => window.clearTimeout(timeoutId);
        },
      );
    }
    commitSelectedRun(targetRunId, () => {
      setWorkflowResult(confirmed ? resp : null);
    });
    return resp;
  };

  const handleCancelRun = async () => {
    if (!runId || !textToSqlRunCanCancel(runId, runStatus, cancelInFlightRef.current)) return;
    const targetRunId = runId;
    const targetStatus = runStatus;
    if (!commitSelectedRun(targetRunId, () => {
      setIsCancelling(true);
      setError(null);
    })) return;
    try {
      const outcome = await requestTextToSqlCancellation(
        (_action, payload) => textToSqlClient.cancel(String(payload.run_id)),
        (targetRunId) => loadTextToSqlStatusAfterCurrent(
          runStatusInFlightRef.current,
          targetRunId,
          () => loadRunStatus(targetRunId),
        ),
        targetRunId,
        targetStatus,
        cancelInFlightRef,
      );
      if (selectedRunRef.current.runId !== targetRunId) return;
      if (outcome.kind === "cancelled") {
        setRunStatus(outcome.status);
        setError(null);
        notify?.("Запуск отменён", "success");
      } else if (outcome.kind === "terminal") {
        notify?.(`Запуск уже завершён: ${outcome.terminalStatus}`, "info");
      } else if (outcome.kind === "not_confirmed") {
        const message = "Отмена не подтверждена";
        setError(message);
        notify?.(message, "error");
      }
    } catch (err) {
      if (selectedRunRef.current.runId !== targetRunId) return;
      const message = err instanceof Error ? err.message : "Не удалось отменить запуск";
      setError(message);
      notify?.(message, "error");
    } finally {
      commitSelectedRun(targetRunId, () => setIsCancelling(false));
    }
  };

  useEffect(() => {
    if (
      !active
      || !autoRefreshRun
      || !runId
      || textToSqlRunTerminalStatus(runStatus)
    ) return;
    return startTextToSqlStatusPolling(
      () => loadRunStatus(runId),
      (tick) => {
        const id = window.setInterval(() => void tick(), 3000);
        return () => window.clearInterval(id);
      },
    );
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, autoRefreshRun, runId, runStatus]);

  useEffect(() => {
    if (!runId || !textToSqlRunTerminalStatus(runStatus)) return;
    void loadRunResult(runId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId, runStatus]);

  return {
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
    handleGenerate,
    handleGenerateReport,
    handleCancelRun,
    loadRunStatus,
    loadRunResult,
  };
}
