"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import type { StorybookClient } from "./client";
import { createLatestGate } from "./latestGate";
import {
  versionsCompare,
  versionsList,
  versionsRestore,
  type CurrentView,
  type VersionView,
  type VersionsCompareResult,
} from "./review";
import { SHOT_TEXTS } from "./shotsTexts";
import { shotErrorFrom } from "./shotsView";

type Say = (text: string, type?: "error" | "success" | "info") => void;
export type VersionsStatus = "idle" | "loading" | "ready" | "error";

export type VersionsApi = {
  status: VersionsStatus;
  error: string | null;
  versions: VersionView[];
  current: CurrentView | null;
  selected: string | null;
  reload: () => Promise<void>;
  // сравнение двух сторон; "current" - текущие файлы
  compare: (left: string, right: string) => Promise<void>;
  compareResult: VersionsCompareResult | null;
  compareError: string | null;
  comparing: boolean;
  closeCompare: () => void;
  restore: (versionId: string) => Promise<boolean>;
  restoring: boolean;
};

type Args = {
  client: StorybookClient;
  runUser: <T>(task: () => Promise<T>) => Promise<T>;
  background: { run: <T>(task: () => Promise<T>) => Promise<T> };
  projectId: string;
  shotKey: string | null;
  enabled: boolean;
  say: Say;
  // версия возвращена: карточка и список должны перечитаться
  onRestored: () => void;
};

export const useVersions = ({ client, runUser, background, projectId, shotKey, enabled, say, onRestored }: Args): VersionsApi => {
  const [status, setStatus] = useState<VersionsStatus>("idle");
  const [error, setError] = useState<string | null>(null);
  const [data, setData] = useState<{ key: string; versions: VersionView[]; current: CurrentView | null; selected: string | null } | null>(null);
  const [compareResult, setCompareResult] = useState<VersionsCompareResult | null>(null);
  const [compareError, setCompareError] = useState<string | null>(null);
  const [comparing, setComparing] = useState(false);
  const [restoring, setRestoring] = useState(false);
  const gate = useRef(createLatestGate()).current;
  const compareGate = useRef(createLatestGate()).current;
  // восстановление после сбоя показываем один раз на шот
  const recovered = useRef(new Set<string>()).current;
  const alive = useRef(true);
  const sayRef = useRef(say);
  const onRestoredRef = useRef(onRestored);
  useEffect(() => {
    sayRef.current = say;
    onRestoredRef.current = onRestored;
  });
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const load = useCallback(
    async (key: string, user: boolean) => {
      const token = gate.next();
      setStatus("loading");
      try {
        const task = () => versionsList(client, { project_id: projectId, key, kind: "shot" });
        const res = await (user ? runUser(task) : background.run(task));
        if (!alive.current || !gate.isCurrent(token)) return;
        if (res.recovery && Object.keys(res.recovery).length > 0 && !recovered.has(key)) {
          recovered.add(key);
          sayRef.current(SHOT_TEXTS.recovery_toast, "info");
        }
        setData({ key, versions: res.versions ?? [], current: res.current ?? null, selected: res.selected ?? null });
        setError(null);
        setStatus("ready");
      } catch (err) {
        if (!alive.current || !gate.isCurrent(token)) return;
        setError(shotErrorFrom(err));
        setStatus("error");
      }
    },
    [client, runUser, background, projectId, gate, recovered],
  );

  useEffect(() => {
    compareGate.next();
    setCompareResult(null);
    setCompareError(null);
    setComparing(false);
    if (!enabled || !shotKey) {
      gate.next();
      setStatus("idle");
      return;
    }
    const timer = setTimeout(() => void load(shotKey, false), 300);
    return () => clearTimeout(timer);
  }, [enabled, shotKey, load, gate, compareGate]);

  const reload = useCallback(async () => {
    if (shotKey) await load(shotKey, true);
  }, [shotKey, load]);

  const compare = useCallback(
    async (left: string, right: string) => {
      if (!shotKey) return;
      const token = compareGate.next();
      setComparing(true);
      setCompareError(null);
      try {
        const res = await runUser(() => versionsCompare(client, { project_id: projectId, key: shotKey, left, right }));
        if (alive.current && compareGate.isCurrent(token)) setCompareResult(res);
      } catch (err) {
        if (alive.current && compareGate.isCurrent(token)) setCompareError(shotErrorFrom(err));
      } finally {
        if (alive.current && compareGate.isCurrent(token)) setComparing(false);
      }
    },
    [client, runUser, projectId, shotKey, compareGate],
  );

  const closeCompare = useCallback(() => {
    compareGate.next();
    setCompareResult(null);
    setCompareError(null);
    setComparing(false);
  }, [compareGate]);

  // Возврат: expected_token берётся из текущих файлов, загруженных вместе со списком
  const restore = useCallback(
    async (versionId: string): Promise<boolean> => {
      if (!shotKey || restoring) return false;
      const tokens = data && data.key === shotKey ? data.current?.tokens : undefined;
      setRestoring(true);
      try {
        const res = await runUser(() =>
          versionsRestore(client, {
            project_id: projectId,
            key: shotKey,
            version_id: versionId,
            ...(tokens ? { expected_token: tokens } : {}),
            surface: "react",
          }),
        );
        if (!alive.current) return true;
        for (const w of res.warnings) sayRef.current(w.message, "info");
        sayRef.current(SHOT_TEXTS.restore_done, "success");
        setCompareResult(null);
        onRestoredRef.current();
        await load(shotKey, true);
        return true;
      } catch (err) {
        if (alive.current) {
          sayRef.current(shotErrorFrom(err), "error");
          // текущие файлы могли измениться: перечитываем список
          void load(shotKey, true);
        }
        return false;
      } finally {
        if (alive.current) setRestoring(false);
      }
    },
    [shotKey, restoring, data, runUser, client, projectId, load],
  );

  const mine = data && data.key === shotKey ? data : null;
  return {
    status,
    error,
    versions: mine?.versions ?? [],
    current: mine?.current ?? null,
    selected: mine?.selected ?? null,
    reload,
    compare,
    compareResult,
    compareError,
    comparing,
    closeCompare,
    restore,
    restoring,
  };
};
