"use client";

import { useEffect, useMemo, useRef, useState } from "react";

type UseRunLogsParams = {
  active: boolean;
  runId: string | null;
  runStatus: unknown;
  runServiceAction: (action: string, payload: Record<string, unknown>) => Promise<unknown>;
  isRunSelected: (runId: string) => boolean;
  commitSelectedRun: (runId: string, commit: () => void) => boolean;
};

export function useRunLogs({
  active,
  runId,
  runStatus,
  runServiceAction,
  isRunSelected,
  commitSelectedRun,
}: UseRunLogsParams) {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const [runLogs, setRunLogs] = useState<any[]>([]);
  const [runLogsAutoRefresh, setRunLogsAutoRefresh] = useState(true);
  const runLogsInFlightRef = useRef<Set<string>>(new Set());

  const loadRunLogs = async (runIdValue?: string | null) => {
    const targetRunId = runIdValue || runId;
    if (!targetRunId || !isRunSelected(targetRunId)) return;
    if (runLogsInFlightRef.current.has(targetRunId)) return;
    runLogsInFlightRef.current.add(targetRunId);
    try {
      const resp = await runServiceAction("logs.run_logs", { run_id: targetRunId, limit: 20000 });
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const items = (resp as any)?.logs ?? resp;
      commitSelectedRun(targetRunId, () => {
        setRunLogs(Array.isArray(items) ? items : []);
      });
    } catch {
      commitSelectedRun(targetRunId, () => setRunLogs([]));
    } finally {
      runLogsInFlightRef.current.delete(targetRunId);
    }
  };

  useEffect(() => {
    if (!active || !runId) return;
    void loadRunLogs(runId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, runId]);

  useEffect(() => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const statusValue = (runStatus as any)?.status ?? (runStatus as any)?.state ?? "";
    if (!active || !runLogsAutoRefresh || !runId || statusValue !== "running") return;
    const id = window.setInterval(() => void loadRunLogs(runId), 3000);
    return () => window.clearInterval(id);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, runId, runLogsAutoRefresh, runStatus]);

  const formattedRunLogs = useMemo(() => {
    return runLogs
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
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

  return {
    runLogs,
    runLogsAutoRefresh,
    setRunLogsAutoRefresh,
    loadRunLogs,
    formattedRunLogs,
    resetRunLogs: () => setRunLogs([]),
  };
}
