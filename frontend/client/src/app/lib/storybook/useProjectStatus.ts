"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import type { StorybookClient } from "./client";
import { briefFromStatus } from "./overviewView";
import { createStatusPoller, type StatusPoller } from "./statusPoll";
import {
  projectStatus,
  projectStatusBrief,
  statusPollIntervalMs,
  type ProjectStatus,
  type ProjectStatusBrief,
} from "./status";
import { classifyStorybookError, type ClassifiedError } from "./uiErrors";

export type StatusMode = "full" | "brief";

type Data = { scopeId: string; projectId: string; status: ProjectStatus | null; brief: ProjectStatusBrief | null };

export type ProjectStatusApi = {
  status: ProjectStatus | null;
  brief: ProjectStatusBrief | null;
  state: "idle" | "loading" | "ready" | "error";
  error: ClassifiedError | null;
  // есть старые данные, но последний запрос не удался
  offline: boolean;
  refresh: () => void;
};

type Args = { scopeId?: string; client: StorybookClient; projectId: string | null; mode: StatusMode; enabled: boolean };

export const useProjectStatus = ({ client, projectId, mode, enabled, scopeId = "" }: Args): ProjectStatusApi => {
  const [data, setData] = useState<Data | null>(null);
  const [error, setError] = useState<{ scopeId: string; projectId: string; value: ClassifiedError } | null>(null);
  const pollerRef = useRef<StatusPoller | null>(null);

  useEffect(() => {
    if (!enabled || !projectId) return;
    const id = projectId;
    const poller = createStatusPoller<ProjectStatus | ProjectStatusBrief>({
      fetch: ({ manual }) =>
        mode === "full"
          ? projectStatus(client, { project_id: id, surface: "react", ...(manual ? { refresh: true } : {}) })
          : projectStatusBrief(client, { project_id: id, surface: "react" }),
      intervalFor: (d) =>
        statusPollIntervalMs(d ? ("active_run" in d ? d.active_run.state : d.run.state) : undefined),
      onData: (d) => {
        setError(null);
        if ("active_run" in d) setData({ scopeId, projectId: id, status: d, brief: briefFromStatus(d) });
        else setData((prev) => ({ scopeId, projectId: id, status: prev?.projectId === id && prev.scopeId === scopeId ? prev.status : null, brief: d }));
      },
      onError: (err) => setError({ scopeId, projectId: id, value: classifyStorybookError(err) }),
      isHidden: () => typeof document !== "undefined" && document.hidden,
    });
    pollerRef.current = poller;
    const onVisible = () => {
      if (!document.hidden) poller.notifyVisible();
    };
    document.addEventListener("visibilitychange", onVisible);
    poller.start();
    return () => {
      document.removeEventListener("visibilitychange", onVisible);
      poller.stop();
      if (pollerRef.current === poller) pollerRef.current = null;
    };
  }, [client, projectId, mode, enabled, scopeId]);

  const refresh = useCallback(() => pollerRef.current?.refreshNow(), []);

  const mine = data && data.projectId === projectId && data.scopeId === scopeId ? data : null;
  const myError = error && error.projectId === projectId && error.scopeId === scopeId ? error.value : null;
  const hasData = mode === "full" ? !!mine?.status : !!mine?.brief;
  const state: ProjectStatusApi["state"] =
    !enabled || !projectId ? "idle" : hasData ? "ready" : myError ? "error" : "loading";

  return {
    status: mine?.status ?? null,
    brief: mine?.brief ?? null,
    state,
    error: myError,
    offline: hasData && myError !== null,
    refresh,
  };
};
