"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import type { StorybookClient } from "./client";
import { createLogLoader, EMPTY_LOG, type LogLoader, type LogState } from "./logView";
import { createRunPoller, LOG_LINES_CAP, runLogTail, type RunStatusResult } from "./runs";
import type { StatusPoller } from "./statusPoll";
import { classifyStorybookError, type ClassifiedError } from "./uiErrors";

export type RunStatusApi = {
  status: RunStatusResult | null;
  error: ClassifiedError | null;
  log: LogState;
  // журнал выбранного из истории прогона; null - вернуться к текущему
  viewRunId: string | null;
  viewLog: (runId: string | null) => void;
  refresh: () => void;
  clearLog: () => void;
};

type Args = { client: StorybookClient; projectId: string; enabled: boolean };

export const runIdOf = (s: RunStatusResult | null): string | null => {
  const fromRun = s?.run && typeof (s.run as { run_id?: unknown }).run_id === "string" ? ((s.run as { run_id: string }).run_id) : null;
  return s?.progress?.run_id ?? fromRun;
};

// Опрос состояния прогона и дочитывание журнала. Каждому проекту - свой опросчик.
export const useRunStatus = ({ client, projectId, enabled }: Args): RunStatusApi => {
  const [status, setStatus] = useState<{ projectId: string; value: RunStatusResult } | null>(null);
  const [error, setError] = useState<ClassifiedError | null>(null);
  const [log, setLog] = useState<LogState>(EMPTY_LOG);
  const pollerRef = useRef<StatusPoller | null>(null);
  const loaderRef = useRef<LogLoader | null>(null);
  const [viewRunId, setViewRunId] = useState<string | null>(null);
  const viewRef = useRef<string | null>(null);
  const lastRunId = useRef<string | null>(null);

  useEffect(() => {
    if (!enabled) return;
    const id = projectId;
    let alive = true;
    const loader = createLogLoader({
      fetch: (runId, after) => runLogTail(client, { project_id: id, run_id: runId, lines: LOG_LINES_CAP, after_offset: after }),
      onChange: (s) => {
        if (alive) setLog(s);
      },
    });
    loaderRef.current = loader;
    const poller = createRunPoller({
      client,
      project_id: id,
      onStatus: (s) => {
        if (!alive) return;
        setError(null);
        setStatus({ projectId: id, value: s });
        lastRunId.current = runIdOf(s);
        void loader.poll(viewRef.current ?? lastRunId.current);
      },
      onError: (err) => {
        if (alive) setError(classifyStorybookError(err));
      },
      isHidden: () => typeof document !== "undefined" && document.hidden,
    });
    pollerRef.current = poller;
    const onVisible = () => {
      if (!document.hidden) poller.notifyVisible();
    };
    document.addEventListener("visibilitychange", onVisible);
    poller.start();
    return () => {
      alive = false;
      document.removeEventListener("visibilitychange", onVisible);
      poller.stop();
      if (pollerRef.current === poller) pollerRef.current = null;
      if (loaderRef.current === loader) loaderRef.current = null;
    };
  }, [client, projectId, enabled]);

  const refresh = useCallback(() => pollerRef.current?.refreshNow(), []);
  const viewLog = useCallback((runId: string | null) => {
    viewRef.current = runId;
    setViewRunId(runId);
    void loaderRef.current?.poll(runId ?? lastRunId.current);
  }, []);
  const clearLog = useCallback(() => loaderRef.current?.clear(), []);
  const mine = status && status.projectId === projectId ? status.value : null;
  return { status: mine, error, log, viewRunId, viewLog, refresh, clearLog };
};
