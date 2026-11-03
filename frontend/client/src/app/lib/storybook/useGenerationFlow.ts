"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { StorybookClient } from "./client";
import { loadForm, saveForm, sessionStorageOrNull } from "./generationDraft";
import {
  activeProvider,
  collectInputs,
  isDirty,
  patchBudget,
  setBrief,
  type BriefDraft,
  type BriefKey,
} from "./generationForm";
import {
  confirmKind,
  planParams,
  resolveMode,
  restartDialog,
  selectionFromIntent,
  shouldApplyIntent,
  shouldDiscard,
  startButton,
  transitionToast,
  type ConfirmKind,
  type IntentLike,
  type RegenPreviewLike,
  type Selection,
  type StartButton,
} from "./generationModel";
import { gfmt } from "./generationTexts";
import {
  loadPlanBundle,
  needsReload,
  reconcileAfterSave,
  outcomeText,
  previewRegen,
  shownOf,
  startRegen,
  startRun,
  type FlowOutcome,
  type PlanBundle,
} from "./generationFlow";
import type { RegenWhat, RegeneratePreviewResult } from "./review";
import { runCancel, runMarkInterrupted, runSetPause, type RunMode } from "./runs";
import type { RunSettingsApi } from "./useRunSettings";
import { runIdOf, type RunStatusApi } from "./useRunStatus";
import { StudioError } from "./client";
import { classifyStorybookError } from "./uiErrors";
import { shotErrorFrom } from "./shotsView";
import { toasts } from "./texts";

type Say = (text: string, type?: "error" | "success" | "info") => void;

export type Dialog = null | { type: ConfirmKind | "restart" | "discard" | "mark" | "stop"; error?: string | null };

export type GenerationFlowApi = {
  selection: Selection;
  setMode: (mode: RunMode) => void;
  setStepId: (id: string | null) => void;
  // «С этого шага» / «Только этот шаг» из списка шагов
  pickStep: (mode: "from_step" | "single_step", id: string) => void;
  task: string;
  setTask: (value: string) => void;
  brief: BriefDraft;
  setBriefField: (key: BriefKey, value: string | boolean) => void;
  inputError: string | null;
  regenWhat: RegenWhat;
  setRegenWhat: (what: RegenWhat) => void;
  bundle: PlanBundle | null;
  bundleError: string | null;
  planLoading: boolean;
  regen: RegeneratePreviewResult | null;
  button: StartButton;
  dialog: Dialog;
  busy: boolean;
  pauseBusy: boolean;
  message: string | null;
  paidPending: string[];
  requestStart: () => void;
  confirmDialog: (choice?: "raise") => Promise<void>;
  closeDialog: () => void;
  askStop: () => void;
  askMark: () => void;
  pauseToggle: () => Promise<void>;
  abandonAndStart: () => Promise<void>;
  dismissPending: () => void;
};

type Args = {
  client: StorybookClient;
  runUser: <T>(task: () => Promise<T>) => Promise<T>;
  projectId: string;
  enabled: boolean;
  isAdmin: boolean;
  say: Say;
  intent: IntentLike;
  titleOf: (id: string) => string;
  runApi: RunStatusApi;
  settings: RunSettingsApi;
};

const PLAN_DELAY_MS = 500;

export const useGenerationFlow = (a: Args): GenerationFlowApi => {
  const { client, runUser, projectId, enabled, isAdmin, intent, titleOf, runApi, settings } = a;
  const status = runApi.status;
  const [selection, setSelection] = useState<Selection>({ mode: "full", stepId: null, shotKeys: [] });
  const [task, setTask] = useState("");
  const [brief, setBriefState] = useState<BriefDraft>({});
  const [regenWhat, setRegenWhat] = useState<RegenWhat>("both");
  const [bundle, setBundle] = useState<PlanBundle | null>(null);
  const [bundleError, setBundleError] = useState<string | null>(null);
  const [planLoading, setPlanLoading] = useState(false);
  const [regen, setRegen] = useState<RegeneratePreviewResult | null>(null);
  const [dialog, setDialog] = useState<Dialog>(null);
  const [busy, setBusy] = useState(false);
  const [pauseBusy, setPauseBusy] = useState(false);
  const pauseGuard = useRef(false);
  const [reloadNonce, setReloadNonce] = useState(0);
  const [hydrated, setHydrated] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [paidPending, setPaidPending] = useState<string[]>([]);
  const gate = useRef(0);
  const sayRef = useRef(a.say);
  useEffect(() => {
    sayRef.current = a.say;
  });

  const resumable = status?.resumable ?? false;
  const shotCount = selection.shotKeys.length;
  const ctx = useMemo(() => ({ resumable, shotCount }), [resumable, shotCount]);

  // черновик сказки и брифа возвращается при возвращении на экран
  useEffect(() => {
    const d = loadForm(sessionStorageOrNull(), projectId);
    if (d) {
      setTask(d.task);
      setBriefState(d.brief);
      setRegenWhat(d.regenWhat);
    }
    setHydrated(true);
  }, [projectId]);
  useEffect(() => {
    if (hydrated) saveForm(sessionStorageOrNull(), projectId, { task, brief, regenWhat });
  }, [hydrated, projectId, task, brief, regenWhat]);

  // переход из других экранов применяется один раз на объект перехода, когда уже известно, есть ли что продолжать
  const lastIntent = useRef<IntentLike>(null);
  const statusKnown = !!status || !!runApi.error;
  useEffect(() => {
    if (!shouldApplyIntent(intent, lastIntent.current, statusKnown)) return;
    lastIntent.current = intent;
    setSelection((s) => selectionFromIntent(intent, s, ctx));
  }, [intent, ctx, statusKnown]);

  // после сбоя по умолчанию предлагаем продолжить; один раз на проект
  const defaulted = useRef<string | null>(null);
  useEffect(() => {
    if (!status || defaulted.current === projectId) return;
    defaulted.current = projectId;
    if (!intent && status.resumable && (status.state === "failed" || status.state === "interrupted")) {
      setSelection((s) => ({ ...s, mode: "resume" }));
    }
  }, [status, projectId, intent]);

  const inputs = useMemo(() => collectInputs(brief, bundle?.plan.inputs), [brief, bundle?.plan.inputs]);
  const params = useMemo(
    () => planParams(selection, inputs.error ? {} : inputs.inputs, task),
    [selection, inputs, task],
  );
  const paramsSig = JSON.stringify(params);
  const runState = status?.state;
  const dirtyDraft = isDirty(settings.saved, settings.draft) && settings.draft ? settings.draft : undefined;
  const draftSig = dirtyDraft ? JSON.stringify(dirtyDraft) : "";

  useEffect(() => {
    if (!enabled || settings.state !== "ready" || inputs.error) {
      setPlanLoading(false);
      return;
    }
    if (selection.mode === "regenerate_shots" && selection.shotKeys.length === 0) {
      setRegen(null);
      setBundle(null);
      setPlanLoading(false);
      return;
    }
    const token = ++gate.current;
    setPlanLoading(true);
    const timer = setTimeout(async () => {
      try {
        if (selection.mode === "regenerate_shots") {
          const preview = await previewRegen(client, projectId, regenWhat, selection.shotKeys);
          if (token !== gate.current) return;
          setRegen(preview);
          setBundleError(null);
        } else {
          const next = await loadPlanBundle(client, projectId, params, dirtyDraft);
          if (token !== gate.current) return;
          setBundle(next);
          setBundleError(null);
        }
      } catch (err) {
        if (token !== gate.current) return;
        setBundleError(shotErrorFrom(err));
      } finally {
        if (token === gate.current) setPlanLoading(false);
      }
    }, PLAN_DELAY_MS);
    return () => {
      clearTimeout(timer);
      gate.current += 1;
    };
    // dirtyDraft входит через draftSig: объект меняется при каждом рендере
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, settings.state, client, projectId, paramsSig, draftSig, regenWhat, runState, inputs.error, selection.mode, selection.shotKeys, reloadNonce]);

  // тосты о смене состояния прогона
  const prevState = useRef<typeof runState>(undefined);
  useEffect(() => {
    const toast = transitionToast(prevState.current, status, titleOf);
    prevState.current = status?.state;
    if (toast) sayRef.current(toast.text, toast.type);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status]);

  const regenLike: RegenPreviewLike | null = regen
    ? {
        can_start: regen.can_start,
        cost_rub: regen.confirm_required.cost_rub,
        unknown_cost: regen.confirm_required.unknown_cost,
        problem: regen.problems.find((p) => p.level === "error")?.message ?? null,
      }
    : null;

  const engine = (status?.engine ?? {}) as { available?: boolean; error?: string | null };
  const button = startButton({
    loading: settings.state !== "ready" || (!status && !runApi.error),
    engineError: engine.available === false ? (engine.error ?? "причина неизвестна") : null,
    isAdmin,
    runState,
    locked: status?.locked ?? false,
    selection,
    hasBrief: bundle ? !bundle.plan.problems.some((p) => p.code === "invalid_task") : true,
    task,
    plan: bundle?.plan ?? null,
    planError: bundleError,
    issues: settings.issues,
    provider: activeProvider(settings.catalog, settings.draft),
    estimate: bundle?.estimate ?? null,
    estimateError: bundle?.estimateError ?? null,
    regen: regenLike,
    titleOf,
  });

  const setMode = useCallback((mode: RunMode) => setSelection((s) => ({ ...s, mode: resolveMode(mode, { resumable, shotCount: s.shotKeys.length }) })), [resumable]);
  const setStepId = useCallback((stepId: string | null) => setSelection((s) => ({ ...s, stepId })), []);
  const pickStep = useCallback((mode: "from_step" | "single_step", id: string) => setSelection((s) => ({ ...s, mode, stepId: id })), []);
  const setBriefField = useCallback((key: BriefKey, value: string | boolean) => setBriefState((d) => setBrief(d, key, value)), []);

  const requestStart = useCallback(() => {
    if (!button.enabled || planLoading || busy) return;
    setMessage(null);
    const first = restartDialog(selection.mode, status?.state, resumable);
    if (first) return setDialog({ type: first });
    if (selection.mode === "regenerate_shots") return setDialog({ type: regen?.confirm_required.unknown_cost ? "no_estimate" : "start" });
    setDialog({ type: confirmKind(bundle?.estimate ?? null, bundle?.estimateError ?? null) });
  }, [button.enabled, planLoading, busy, selection.mode, status?.state, resumable, regen, bundle]);

  const closeDialog = useCallback(() => setDialog(null), []);

  const afterOutcome = useCallback(
    (o: FlowOutcome) => {
      if (o.kind === "started") {
        setDialog(null);
        setPaidPending([]);
        // запущенный текст сказки и бриф не должны уйти повторно
        setTask("");
        setBriefState({});
        setRegenWhat("both");
        sayRef.current(gfmt("started"), "success");
        runApi.refresh();
        void settings.reloadHistory();
        return;
      }
      setDialog(null);
      if (o.kind === "paid_pending") setPaidPending(o.keys);
      setMessage(outcomeText(o));
      // при смене плана показываем новый: перечитываем
      if (needsReload(o)) setReloadNonce((n) => n + 1);
    },
    [runApi, settings],
  );

  const doStart = useCallback(
    async (planBundle: PlanBundle | null, opts: { abandon?: boolean; saved?: boolean } = {}): Promise<void> => {
      setBusy(true);
      try {
        let shown = planBundle;
        // форма с несохранёнными правками: сначала сохраняем, затем строим план заново, чтобы запуск шёл по тому, что видно
        if (dirtyDraft && !opts.saved) {
          const err = await settings.save();
          if (err) {
            setDialog((d) => (d ? { ...d, error: err } : d));
            setMessage(err);
            return;
          }
          if (selection.mode !== "regenerate_shots") {
            const fresh = await loadPlanBundle(client, projectId, params);
            setBundle(fresh);
            const changed = reconcileAfterSave(planBundle, fresh);
            if (changed) {
              afterOutcome(changed);
              return;
            }
            shown = fresh;
          }
        }
        const outcome: FlowOutcome = await runUser(async () => {
          if (selection.mode === "regenerate_shots") {
            if (!regen) return { kind: "plan_changed" } as FlowOutcome;
            return startRegen(client, projectId, regenWhat, selection.shotKeys, regen, !!opts.abandon);
          }
          if (!shown) return { kind: "plan_changed" } as FlowOutcome;
          return startRun(client, projectId, params, shownOf(shown.plan), { discardUnfinished: shouldDiscard(selection.mode, resumable) });
        });
        afterOutcome(outcome);
      } catch (err) {
        setDialog((d) => (d ? { ...d, error: classifyStorybookError(err).text } : d));
      } finally {
        setBusy(false);
      }
    },
    [dirtyDraft, runUser, client, projectId, params, selection, regen, regenWhat, resumable, settings, afterOutcome],
  );

  const confirmDialog = useCallback(
    async (choice?: "raise") => {
      const d = dialog;
      if (!d) return;
      if (d.type === "restart" || d.type === "discard") {
        setDialog({ type: confirmKind(bundle?.estimate ?? null, bundle?.estimateError ?? null) });
        return;
      }
      if (d.type === "stop") {
        setBusy(true);
        try {
          await runUser(() => runCancel(client, { project_id: projectId }));
          sayRef.current(toasts.stopRequested, "success");
          setDialog(null);
          runApi.refresh();
        } catch (err) {
          setDialog({ type: "stop", error: classifyStorybookError(err).text });
        } finally {
          setBusy(false);
        }
        return;
      }
      if (d.type === "mark") {
        const id = runIdOf(status);
        if (!id) return;
        setBusy(true);
        try {
          await runUser(() => runMarkInterrupted(client, { project_id: projectId, run_id: id }));
          sayRef.current(gfmt("marked"), "success");
          setDialog(null);
          runApi.refresh();
        } catch (err) {
          const locked = err instanceof StudioError && err.code === "locked";
          setDialog({ type: "mark", error: locked ? gfmt("mark_locked") : classifyStorybookError(err).text });
        } finally {
          setBusy(false);
        }
        return;
      }
      if (choice === "raise" && bundle?.estimate && settings.draft) {
        setBusy(true);
        const total = Math.ceil(bundle.estimate.total_known_rub);
        // поднимаем бюджет в текущей форме: остальные правки не теряются
        const err = await settings.save(patchBudget(settings.draft, total));
        if (err) {
          setBusy(false);
          setDialog({ ...d, error: err });
          return;
        }
        try {
          const fresh = await loadPlanBundle(client, projectId, params);
          setBundle(fresh);
          setBusy(false);
          await doStart(fresh, { saved: true });
        } catch (e) {
          setBusy(false);
          setDialog({ ...d, error: classifyStorybookError(e).text });
        }
        return;
      }
      await doStart(bundle);
    },
    [dialog, bundle, runUser, client, projectId, runApi, status, settings, params, doStart],
  );

  const abandonAndStart = useCallback(() => doStart(null, { abandon: true }), [doStart]);
  const dismissPending = useCallback(() => setPaidPending([]), []);

  const askStop = useCallback(() => setDialog({ type: "stop" }), []);
  const askMark = useCallback(() => setDialog({ type: "mark" }), []);

  const pauseToggle = useCallback(async () => {
    if (pauseGuard.current) return;
    pauseGuard.current = true;
    setPauseBusy(true);
    const pausing = !(status?.state === "paused" || status?.state === "pause_requested");
    try {
      await runUser(() => runSetPause(client, { project_id: projectId, paused: pausing }));
      sayRef.current(pausing ? toasts.pauseRequested : toasts.resumed, "success");
    } catch (err) {
      sayRef.current(classifyStorybookError(err).text, "error");
    } finally {
      pauseGuard.current = false;
      setPauseBusy(false);
    }
    runApi.refresh();
  }, [status?.state, runUser, client, projectId, runApi]);

  return {
    selection,
    setMode,
    setStepId,
    pickStep,
    task,
    setTask,
    brief,
    setBriefField,
    inputError: inputs.error,
    regenWhat,
    setRegenWhat,
    bundle,
    bundleError,
    planLoading,
    regen,
    button,
    dialog,
    busy,
    pauseBusy,
    message,
    paidPending,
    requestStart,
    confirmDialog,
    closeDialog,
    askStop,
    askMark,
    pauseToggle,
    abandonAndStart,
    dismissPending,
  };
};
