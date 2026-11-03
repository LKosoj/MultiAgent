"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  blockoutDetail,
  blockoutLeaveAsIs,
  blockoutSetDuration,
  blockoutState,
  compareImages,
  groupByScene,
  type BlockoutAction,
  type BlockoutCompare,
  type BlockoutDetailResult,
  type BlockoutFileRef,
  type BlockoutPlanResult,
  type BlockoutStateResult,
} from "./blockout";
import { createBlockoutFlow, type FlowStep } from "./blockoutFlow";
import { BLOCKOUT_TEXTS } from "./blockoutTexts";
import {
  EMPTY_SELECTION,
  actionButtons,
  applySelection,
  detailKey,
  leaveAsIsText,
  orderedShotKeys,
  durationFailure,
  planDialogView,
  planTarget,
  pruneSelection,
  resolveBlockoutScreen,
  successToast,
  type ActionButton,
  type BlockoutScreenState,
  type PlanDialogView,
  type PlanTarget,
  type Selection,
} from "./blockoutView";
import type { BackgroundQueue } from "./backgroundQueue";
import type { StorybookClient } from "./client";
import { createLatestGate } from "./latestGate";
import { isConflict } from "./review";
import { isRunBusy, shotErrorFrom } from "./shotsView";
import type { RunState } from "./status";
import { classifyStorybookError } from "./uiErrors";
import { useShotMedia, type ShotMediaApi } from "./useShotMedia";

type Say = (text: string, type?: "error" | "success" | "info") => void;

export type PlanDialogState = {
  action: BlockoutAction;
  target: PlanTarget;
  plan: BlockoutPlanResult;
  view: PlanDialogView;
  busy: boolean;
  error: string | null;
};

export type BlockoutApi = {
  projectId: string;
  screen: BlockoutScreenState;
  state: BlockoutStateResult | null;
  error: string | null;
  reload: () => void;
  media: ShotMediaApi;
  selection: Selection;
  select: (key: string, mods?: { ctrl?: boolean; shift?: boolean }) => void;
  detail: BlockoutDetailResult | null;
  detailError: string | null;
  images: BlockoutCompare | null;
  videoUrl: (ref: BlockoutFileRef | null) => string | null;
  previewUrl: string | null;
  buttons: ActionButton[];
  // идёт запрос (план, запуск, запись): кнопки отключены
  busy: boolean;
  runAction: (action: BlockoutAction | "leave_as_is") => Promise<void>;
  dialog: PlanDialogState | null;
  confirmDialog: () => Promise<void>;
  closeDialog: () => void;
  durationChoice: number | null;
  setDurationChoice: (n: number | null) => void;
  applyDuration: () => Promise<void>;
  rebuildHint: boolean;
};

type Args = {
  client: StorybookClient;
  background: BackgroundQueue;
  runUser: <T>(task: () => Promise<T>) => Promise<T>;
  projectId: string;
  active: boolean;
  runState: RunState | undefined;
  say: Say;
  onRunStarted: () => void;
};

const withRev = (url: string | null, mtime: number | undefined): string | null =>
  url && mtime ? `${url}&v=${encodeURIComponent(String(mtime))}` : url;

export const useBlockout = ({ client, background, runUser, projectId, active, runState, say, onRunStarted }: Args): BlockoutApi => {
  const [state, setState] = useState<BlockoutStateResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [selection, setSelection] = useState<Selection>(EMPTY_SELECTION);
  const [detail, setDetail] = useState<BlockoutDetailResult | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [forbidden, setForbidden] = useState(false);
  const [dialog, setDialog] = useState<PlanDialogState | null>(null);
  const [durationChoice, setDurationChoice] = useState<number | null>(null);
  const [rebuildHint, setRebuildHint] = useState(false);
  const alive = useRef(true);
  const stateGate = useRef(createLatestGate());
  const detailGate = useRef(createLatestGate());
  const sayRef = useRef(say);
  const startedRef = useRef(onRunStarted);
  useEffect(() => {
    sayRef.current = say;
    startedRef.current = onRunStarted;
  });
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const media = useShotMedia({ client, background, projectId, enabled: active });
  const flow = useMemo(() => createBlockoutFlow({ client, projectId, launcher: "backend" }), [client, projectId]);

  const reload = useCallback(() => setReloadKey((n) => n + 1), []);

  // состояние болванки: последний ответ побеждает
  useEffect(() => {
    if (!active) return;
    const token = stateGate.current.next();
    runUser(() => blockoutState(client, { project_id: projectId }))
      .then((res) => {
        if (!alive.current || !stateGate.current.isCurrent(token)) return;
        setState(res);
        setError(null);
        setSelection((sel) => pruneSelection(sel, res));
      })
      .catch((err: unknown) => {
        if (!alive.current || !stateGate.current.isCurrent(token)) return;
        setError(classifyStorybookError(err).text);
      });
  }, [client, runUser, projectId, active, reloadKey]);

  // другой проект: старое не показываем
  useEffect(() => {
    setState(null);
    setError(null);
    setSelection(EMPTY_SELECTION);
    setDetail(null);
    setDialog(null);
  }, [projectId]);

  // прогон закончился: перечитываем
  const prevRun = useRef<RunState | undefined>(runState);
  useEffect(() => {
    const was = isRunBusy(prevRun.current);
    prevRun.current = runState;
    if (was && !isRunBusy(runState)) {
      setReloadKey((n) => n + 1);
      media.bust();
    }
  }, [runState, media]);

  // детали выбранного шота; устаревший ответ отбрасывается
  const key = detailKey(selection);
  useEffect(() => {
    const token = detailGate.current.next();
    setDetailError(null);
    setRebuildHint(false);
    if (!key || !active) {
      setDetail(null);
      return;
    }
    runUser(() => blockoutDetail(client, { project_id: projectId, shot_key: key }))
      .then((res) => {
        if (!alive.current || !detailGate.current.isCurrent(token)) return;
        setDetail(res);
        setDurationChoice(null);
      })
      .catch((err: unknown) => {
        if (!alive.current || !detailGate.current.isCurrent(token)) return;
        setDetail(null);
        setDetailError(classifyStorybookError(err).text);
      });
  }, [client, runUser, projectId, active, key, reloadKey]);

  const select = useCallback(
    (shotKey: string, mods: { ctrl?: boolean; shift?: boolean } = {}) => {
      if (!state) return;
      const ordered = orderedShotKeys(groupByScene(state));
      setSelection((sel) => applySelection(sel, ordered, shotKey, mods));
    },
    [state],
  );

  const runBusy = isRunBusy(runState);
  const buttons = useMemo(
    () => (state ? actionButtons(state, selection.keys, { runBusy, forbidden }) : []),
    [state, selection.keys, runBusy, forbidden],
  );

  const noteError = useCallback((err: unknown): string => {
    const c = classifyStorybookError(err);
    if (c.kind === "forbidden") setForbidden(true);
    if (c.kind === "locked" || c.kind === "forbidden") return c.text;
    if (isConflict(err)) return BLOCKOUT_TEXTS.plan_changed;
    return shotErrorFrom(err);
  }, []);

  const afterStarted = useCallback(
    (action: BlockoutAction) => {
      sayRef.current(successToast(action), "success");
      startedRef.current();
      setReloadKey((n) => n + 1);
    },
    [],
  );

  const openStep = useCallback((action: BlockoutAction, target: PlanTarget, step: FlowStep) => {
    if (step.kind === "started") return afterStarted(action);
    setDialog({ action, target, plan: step.plan, view: planDialogView(action, step.plan, target.shot_keys?.length ?? 0), busy: false, error: null });
  }, [afterStarted]);

  const runAction = useCallback(
    async (action: BlockoutAction | "leave_as_is") => {
      if (!state || busy) return;
      const btn = buttons.find((b) => b.action === action);
      if (!btn?.enabled) return;
      setBusy(true);
      try {
        if (action === "leave_as_is") {
          const res = await runUser(() => blockoutLeaveAsIs(client, { project_id: projectId, shot_keys: selection.keys }));
          if (!alive.current) return;
          sayRef.current(leaveAsIsText(res), "success");
          setReloadKey((n) => n + 1);
          return;
        }
        const target = planTarget(action, state, selection.keys);
        const step = await runUser(() => flow.start(action, target));
        if (alive.current) openStep(action, target, step);
      } catch (err) {
        if (alive.current) sayRef.current(noteError(err), "error");
      } finally {
        if (alive.current) setBusy(false);
      }
    },
    [state, busy, buttons, runUser, client, projectId, selection.keys, flow, openStep, noteError],
  );

  const confirmDialog = useCallback(async () => {
    if (!dialog || dialog.view.kind !== "confirm" || dialog.busy) return;
    const { action, target, plan } = dialog;
    setDialog({ ...dialog, busy: true, error: null });
    try {
      const out = await runUser(() => flow.confirm(action, target, plan));
      if (!alive.current) return;
      if (out.kind === "started") {
        setDialog(null);
        afterStarted(action);
        return;
      }
      if (out.kind === "blocked") {
        setDialog((d) => (d ? { ...d, busy: false, error: out.text } : d));
        return;
      }
      if (out.step.kind === "started") return;
      setDialog({ action, target, plan: out.step.plan, view: planDialogView(action, out.step.plan, target.shot_keys?.length ?? 0), busy: false, error: out.text });
    } catch (err) {
      if (alive.current) setDialog((d) => (d ? { ...d, busy: false, error: noteError(err) } : d));
    }
  }, [dialog, runUser, flow, afterStarted, noteError]);

  const closeDialog = useCallback(() => setDialog((d) => (d?.busy ? d : null)), []);

  const applyDuration = useCallback(async () => {
    if (!detail || !("state" in detail) || durationChoice === null || busy) return;
    const base = detail.digest;
    if (!base) return;
    setBusy(true);
    try {
      const res = await runUser(() =>
        blockoutSetDuration(client, { project_id: projectId, shot_key: detail.key, seconds: durationChoice, base_digest: base }),
      );
      if (!alive.current) return;
      sayRef.current(BLOCKOUT_TEXTS.duration_changed, "success");
      setRebuildHint(res.rebuild_needed);
      setReloadKey((n) => n + 1);
    } catch (err) {
      if (!alive.current) return;
      const f = durationFailure(err, noteError);
      sayRef.current(f.text, "error");
      // отпечаток устарел: перечитываем шот, иначе повтор даст тот же отказ
      if (f.reload) setReloadKey((n) => n + 1);
    } finally {
      if (alive.current) setBusy(false);
    }
  }, [detail, durationChoice, busy, runUser, client, projectId, noteError]);

  const images = useMemo<BlockoutCompare | null>(() => {
    if (!detail || !("state" in detail) || !media.ready) return null;
    const base = compareImages(client, projectId, detail, 256);
    const mt = (ref: BlockoutFileRef | null) => ref?.mtime;
    return {
      blockout_start: withRev(base.blockout_start, mt(detail.images.blockout_start)),
      image_start: withRev(base.image_start, mt(detail.images.image_start)),
      image_end: withRev(base.image_end, mt(detail.images.image_end)),
      blockout_end: withRev(base.blockout_end, mt(detail.images.blockout_end)),
    };
    // media меняется вместе с токеном: ссылки пересобираются
  }, [detail, media, client, projectId]);

  const videoUrl = useCallback(
    (ref: BlockoutFileRef | null) => (ref && ref.servable && media.ready ? media.media(ref.path, String(ref.mtime)) : null),
    [media],
  );
  const previewUrl = state?.preview.path && state.preview.servable && media.ready ? media.media(state.preview.path) : null;

  return {
    projectId,
    screen: resolveBlockoutScreen({ state, error }),
    state,
    error,
    reload,
    media,
    selection,
    select,
    detail,
    detailError,
    images,
    videoUrl,
    previewUrl,
    buttons,
    busy,
    runAction,
    dialog,
    confirmDialog,
    closeDialog,
    durationChoice,
    setDurationChoice,
    applyDuration,
    rebuildHint,
  };
};
