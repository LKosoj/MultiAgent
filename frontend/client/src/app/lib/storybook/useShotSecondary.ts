"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import type { StorybookClient } from "./client";
import { createLatestGate } from "./latestGate";
import { reportsForShot, type ReportsForShotResult } from "./reports";
import { shotErrorFrom } from "./shotsView";

export type SecondaryStatus = "idle" | "loading" | "ready" | "error";

export type ShotSecondaryApi = {
  status: SecondaryStatus;
  error: string | null;
  reports: ReportsForShotResult | null;
  reload: () => Promise<void>;
};

type Args = {
  client: StorybookClient;
  background: { run: <T>(task: () => Promise<T>) => Promise<T> };
  projectId: string;
  shotKey: string | null;
  enabled: boolean;
};

// Замечания по открытому шоту. Это второстепенные данные: грузятся в фоне, после карточки
export const useShotSecondary = ({ client, background, projectId, shotKey, enabled }: Args): ShotSecondaryApi => {
  const [status, setStatus] = useState<SecondaryStatus>("idle");
  const [error, setError] = useState<string | null>(null);
  const [reports, setReports] = useState<{ key: string; data: ReportsForShotResult } | null>(null);
  const gate = useRef(createLatestGate()).current;
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const load = useCallback(
    async (key: string) => {
      const token = gate.next();
      setStatus("loading");
      try {
        const data = await background.run(() => reportsForShot(client, { project_id: projectId, shot_key: key }));
        if (!alive.current || !gate.isCurrent(token)) return;
        setReports({ key, data });
        setError(null);
        setStatus("ready");
      } catch (err) {
        if (!alive.current || !gate.isCurrent(token)) return;
        setError(shotErrorFrom(err));
        setStatus("error");
      }
    },
    [client, background, projectId, gate],
  );

  useEffect(() => {
    if (!enabled || !shotKey) {
      gate.next();
      setStatus("idle");
      return;
    }
    const timer = setTimeout(() => void load(shotKey), 300);
    return () => clearTimeout(timer);
  }, [enabled, shotKey, load, gate]);

  const reload = useCallback(async () => {
    if (shotKey) await load(shotKey);
  }, [shotKey, load]);

  return { status, error, reports: reports && reports.key === shotKey ? reports.data : null, reload };
};
