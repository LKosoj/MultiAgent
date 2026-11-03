"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import type { StorybookClient } from "./client";
import {
  regeneratePreview,
  regenerateStart,
  startPayloadFromPreview,
  type RegenWhat,
  type RegeneratePreviewResult,
} from "./review";
import { SHOT_TEXTS, fmt } from "./shotsTexts";
import { errorReason, pendingKeysFromError, regenView, shotErrorFrom, type RegenView } from "./shotsView";

type Say = (text: string, type?: "error" | "success" | "info") => void;
export type RegenPhase = "idle" | "previewing" | "ready" | "starting";

export type RegenerationApi = {
  open: boolean;
  phase: RegenPhase;
  keys: string[];
  what: RegenWhat;
  preview: RegeneratePreviewResult | null;
  view: RegenView | null;
  error: string | null;
  abandon: boolean;
  setAbandon: (value: boolean) => void;
  begin: (keys: string[], what: RegenWhat) => void;
  setWhat: (what: RegenWhat) => void;
  start: () => Promise<boolean>;
  close: () => void;
};

type Args = {
  client: StorybookClient;
  runUser: <T>(task: () => Promise<T>) => Promise<T>;
  projectId: string;
  say: Say;
  // запуск принят: список и карточка перечитываются, экран следит за прогоном
  onStarted: () => void;
};

export const useRegeneration = ({ client, runUser, projectId, say, onStarted }: Args): RegenerationApi => {
  const [open, setOpen] = useState(false);
  const [phase, setPhase] = useState<RegenPhase>("idle");
  const [keys, setKeys] = useState<string[]>([]);
  const [what, setWhatState] = useState<RegenWhat>("both");
  const [preview, setPreview] = useState<RegeneratePreviewResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [abandon, setAbandon] = useState(false);
  const seq = useRef(0);
  const alive = useRef(true);
  const sayRef = useRef(say);
  const onStartedRef = useRef(onStarted);
  useEffect(() => {
    sayRef.current = say;
    onStartedRef.current = onStarted;
  });
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const fetchPreview = useCallback(
    async (k: string[], w: RegenWhat): Promise<RegeneratePreviewResult | null> => {
      const my = ++seq.current;
      setPhase("previewing");
      setError(null);
      try {
        const res = await runUser(() => regeneratePreview(client, { project_id: projectId, what: w, shot_keys: k }));
        if (!alive.current || my !== seq.current) return null;
        setPreview(res);
        setPhase("ready");
        return res;
      } catch (err) {
        if (!alive.current || my !== seq.current) return null;
        setPreview(null);
        setError(shotErrorFrom(err));
        setPhase("ready");
        return null;
      }
    },
    [client, runUser, projectId],
  );

  const begin = useCallback(
    (k: string[], w: RegenWhat) => {
      setKeys(k);
      setWhatState(w);
      setAbandon(false);
      setPreview(null);
      setOpen(true);
      void fetchPreview(k, w);
    },
    [fetchPreview],
  );

  const setWhat = useCallback(
    (w: RegenWhat) => {
      setWhatState(w);
      void fetchPreview(keys, w);
    },
    [keys, fetchPreview],
  );

  const close = useCallback(() => {
    seq.current += 1;
    setOpen(false);
    setPhase("idle");
    setPreview(null);
    setError(null);
  }, []);

  // Перед стартом план строится заново: если отпечаток или сумма изменились, запуска нет, пользователь видит новый план
  const start = useCallback(async (): Promise<boolean> => {
    if (!preview || phase !== "ready") return false;
    const shown = preview;
    setPhase("starting");
    setError(null);
    try {
      const ok = await runUser(async () => {
        const fresh = await regeneratePreview(client, { project_id: projectId, what, shot_keys: keys });
        if (fresh.plan_digest !== shown.plan_digest) return { stop: "plan_changed" as const, fresh };
        if (fresh.confirm_required.cost_rub !== shown.confirm_required.cost_rub) return { stop: "cost_changed" as const, fresh };
        const payload = startPayloadFromPreview(
          { project_id: projectId, what, launcher: "backend", shot_keys: keys, ...(abandon ? { abandon_pending_jobs: true } : {}) },
          fresh,
          true,
        );
        await regenerateStart(client, payload);
        return { stop: null, fresh };
      });
      if (!alive.current) return !ok.stop;
      if (ok.stop) {
        setPreview(ok.fresh);
        setError(ok.stop === "plan_changed" ? SHOT_TEXTS.regen_plan_changed : SHOT_TEXTS.regen_cost_changed);
        setPhase("ready");
        return false;
      }
      sayRef.current(fmt("regen_started", { n: shown.expanded_keys.length }), "success");
      close();
      onStartedRef.current();
      return true;
    } catch (err) {
      if (!alive.current) return false;
      const reason = errorReason(err);
      if (reason === "plan_changed" || reason === "cost_changed" || reason === "paid_job_pending") {
        // сервер видит другой план: показываем свежий предпросмотр и причину
        const pending = pendingKeysFromError(err);
        const text = reason === "paid_job_pending" && pending.length > 0 ? fmt("regen_pending_keys", { keys: pending.join(", ") }) : shotErrorFrom(err);
        await fetchPreview(keys, what);
        if (alive.current) setError(text);
        return false;
      }
      setError(shotErrorFrom(err));
      setPhase("ready");
      return false;
    }
  }, [preview, phase, runUser, client, projectId, what, keys, abandon, close, fetchPreview]);

  return {
    open,
    phase,
    keys,
    what,
    preview,
    view: preview ? regenView(preview) : null,
    error,
    abandon,
    setAbandon,
    begin,
    setWhat,
    start,
    close,
  };
};
