"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import type { StorybookClient } from "./client";
import {
  providerCatalog,
  runSettingsGet,
  runSettingsSave,
  styleCatalog,
  type ProviderCatalogResult,
  type RunSettings,
  type RunSettingsGetResult,
  type SettingsIssue,
  type StyleCatalogResult,
} from "./runSettings";
import { runHistory, runStepsGet, type PipelineStepInfo, type RunHistoryRow } from "./runs";
import { loadSettingsDraft, saveSettingsDraft, sessionStorageOrNull } from "./generationDraft";
import { cloneSettings, isDirty } from "./generationForm";
import { classifyStorybookError, type ClassifiedError } from "./uiErrors";

export type SettingsLoad = {
  state: "loading" | "ready" | "error";
  error: ClassifiedError | null;
  saved: RunSettings | null;
  draft: RunSettings | null;
  digest: string | null;
  effective: RunSettingsGetResult["effective"] | null;
  defaults: RunSettings | null;
  issues: SettingsIssue[];
  catalog: ProviderCatalogResult | null;
  styles: StyleCatalogResult | null;
  steps: PipelineStepInfo[];
  history: RunHistoryRow[];
  // форма подставлена из браузера, а не с сервера
  restored: boolean;
};

export type RunSettingsApi = SettingsLoad & {
  setDraft: (next: RunSettings) => void;
  reload: () => Promise<void>;
  reloadHistory: () => Promise<void>;
  discardRestored: () => void;
  // вернуть текст ошибки или null, если сохранено
  save: (override?: RunSettings) => Promise<string | null>;
};

type Args = {
  client: StorybookClient;
  runUser: <T>(task: () => Promise<T>) => Promise<T>;
  projectId: string;
  storageId?: string;
  enabled: boolean;
};

const EMPTY: SettingsLoad = {
  state: "loading",
  error: null,
  saved: null,
  draft: null,
  digest: null,
  effective: null,
  defaults: null,
  issues: [],
  catalog: null,
  styles: null,
  steps: [],
  history: [],
  restored: false,
};

export const useRunSettings = ({ client, runUser, projectId, storageId = projectId, enabled }: Args): RunSettingsApi => {
  const [data, setData] = useState<SettingsLoad>(EMPTY);
  const seq = useRef(0);
  const dataRef = useRef(data);
  useEffect(() => {
    dataRef.current = data;
  });

  const reload = useCallback(async () => {
    const my = ++seq.current;
    setData((d) => ({ ...d, state: d.saved ? d.state : "loading", error: null }));
    try {
      const [settings, catalog, styles, steps, history] = await Promise.all([
        runSettingsGet(client, { project_id: projectId }),
        providerCatalog(client, { for_project_id: projectId }),
        styleCatalog(client),
        runStepsGet(client),
        runHistory(client, { project_id: projectId, limit: 20 }),
      ]);
      if (my !== seq.current) return;
      const restored = loadSettingsDraft(sessionStorageOrNull(), storageId, settings.digest);
      setData({
        state: "ready",
        error: null,
        saved: settings.settings,
        draft: restored ?? cloneSettings(settings.settings),
        restored: restored !== null && isDirty(settings.settings, restored),
        digest: settings.digest,
        effective: settings.effective,
        defaults: settings.defaults,
        issues: settings.issues,
        catalog,
        styles,
        steps: steps.steps,
        history: history.runs,
      });
    } catch (err) {
      if (my !== seq.current) return;
      setData((d) => ({ ...d, state: "error", error: classifyStorybookError(err) }));
    }
  }, [client, projectId, storageId]);

  useEffect(() => {
    if (!enabled) return;
    void reload();
    return () => {
      seq.current += 1;
    };
  }, [enabled, reload]);

  // несохранённая форма переживает уход с экрана
  const { saved, draft, digest } = data;
  useEffect(() => {
    if (data.state !== "ready") return;
    saveSettingsDraft(sessionStorageOrNull(), storageId, digest, isDirty(saved, draft) ? draft : null);
  }, [data.state, storageId, saved, draft, digest]);

  const reloadHistory = useCallback(async () => {
    try {
      const history = await runHistory(client, { project_id: projectId, limit: 20 });
      setData((d) => ({ ...d, history: history.runs }));
    } catch {
      // история необязательна: старый список остаётся
    }
  }, [client, projectId]);

  const setDraft = useCallback((next: RunSettings) => setData((d) => ({ ...d, draft: next })), []);

  const discardRestored = useCallback(() => setData((d) => ({ ...d, draft: d.saved ? cloneSettings(d.saved) : d.draft, restored: false })), []);

  const save = useCallback(async (override?: RunSettings): Promise<string | null> => {
    const cur = dataRef.current;
    const draft = override ?? cur.draft;
    if (!draft) return null;
    try {
      const res = await runUser(() => runSettingsSave(client, { project_id: projectId, settings: draft, expected_digest: cur.digest }));
      setData((d) => ({ ...d, saved: res.settings, draft: cloneSettings(res.settings), digest: res.digest, issues: res.issues, restored: false }));
      return null;
    } catch (err) {
      return classifyStorybookError(err).text;
    }
  }, [client, runUser, projectId]);

  return { ...data, setDraft, reload, reloadHistory, discardRestored, save };
};
