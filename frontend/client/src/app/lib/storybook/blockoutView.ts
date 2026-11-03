// Чистая логика экрана «Болванка»: выбор шотов, строки дерева, кнопки действий, окно подтверждения.
// Правила доступности действий лежат в blockout.ts (actionEnabled, actionBlockerText), здесь они только собираются в экран.
import {
  actionBlockerText,
  actionEnabled,
  groupByScene,
  type BlockoutAction,
  type BlockoutActionKey,
  type BlockoutDetailResult,
  type BlockoutPlanResult,
  type BlockoutProblem,
  type BlockoutSceneGroup,
  type BlockoutShotDetail,
  type BlockoutShotRow,
  type BlockoutStateResult,
} from "./blockout";
import { BLOCKOUT_TEXTS, blockoutFmt, type BlockoutTextId } from "./blockoutTexts";
import { isConflict } from "./review";
import { formatShotMoney } from "./shotsView";
import { NO_RIGHTS_TEXT } from "./texts";

// ---- выбор ----

// focus - последний кликнутый шот: его показывает сравнение, keys идут в порядке дерева
export type Selection = { keys: string[]; anchor: string | null; focus: string | null };
export const EMPTY_SELECTION: Selection = { keys: [], anchor: null, focus: null };

export const orderedShotKeys = (groups: readonly BlockoutSceneGroup[]): string[] =>
  groups.flatMap((g) => [...g.chains.flatMap((c) => c.shots.map((s) => s.key)), ...g.unchained.map((s) => s.key)]);

// Клик: обычный выбирает один шот, Ctrl добавляет или снимает, Shift выделяет диапазон от последнего выбранного
export const applySelection = (
  sel: Selection,
  ordered: readonly string[],
  key: string,
  mods: { ctrl?: boolean; shift?: boolean } = {},
): Selection => {
  if (mods.shift && sel.anchor && ordered.includes(sel.anchor) && ordered.includes(key)) {
    const a = ordered.indexOf(sel.anchor);
    const b = ordered.indexOf(key);
    const range = ordered.slice(Math.min(a, b), Math.max(a, b) + 1);
    const base = mods.ctrl ? sel.keys.filter((k) => !range.includes(k)) : [];
    // ключи отдаём в порядке дерева, как бы ни тянули диапазон
    const keys = [...base, ...range].sort((x, y) => ordered.indexOf(x) - ordered.indexOf(y));
    return { keys, anchor: sel.anchor, focus: key };
  }
  if (mods.ctrl) {
    return sel.keys.includes(key)
      ? (() => {
          const keys = sel.keys.filter((k) => k !== key);
          return { keys, anchor: key, focus: keys[keys.length - 1] ?? null };
        })()
      : { keys: [...sel.keys, key], anchor: key, focus: key };
  }
  return { keys: [key], anchor: key, focus: key };
};

// После перезагрузки состояния выбор не должен ссылаться на пропавшие шоты
export const pruneSelection = (sel: Selection, state: Pick<BlockoutStateResult, "shots"> | null): Selection => {
  if (!state) return sel;
  const known = new Set(state.shots.map((s) => s.key));
  const keys = sel.keys.filter((k) => known.has(k));
  if (keys.length === sel.keys.length) return sel;
  return {
    keys,
    anchor: sel.anchor && known.has(sel.anchor) ? sel.anchor : keys[keys.length - 1] ?? null,
    focus: sel.focus && known.has(sel.focus) ? sel.focus : keys[keys.length - 1] ?? null,
  };
};

// Сравнение показывает последний выбранный шот
export const detailKey = (sel: Selection): string | null => sel.focus;

// ---- дерево ----

export type TreeRow =
  | { kind: "scene"; id: string; depth: 0; label: string; sceneNumber: number }
  | { kind: "chain"; id: string; depth: 1; label: string; chainId: string; blendExists: boolean }
  | { kind: "unchained"; id: string; depth: 1; label: string }
  | { kind: "shot"; id: string; depth: 2; label: string; key: string; state: BlockoutShotRow["state"]; stateTitle: string; marks: string[]; chainId: string | null };

export const shotMarks = (row: Pick<BlockoutShotRow, "p10" | "duration_mismatch">): string[] => [
  ...(row.p10 ? [BLOCKOUT_TEXTS.mark_p10] : []),
  ...(row.duration_mismatch ? [BLOCKOUT_TEXTS.mark_mismatch] : []),
];

const shotRow = (s: BlockoutShotRow): TreeRow => ({
  kind: "shot",
  id: `shot:${s.key}`,
  depth: 2,
  label: blockoutFmt("shot_n", { key: s.key }),
  key: s.key,
  state: s.state,
  stateTitle: s.state_title,
  marks: shotMarks(s),
  chainId: s.chain_id,
});

export const treeRows = (state: Pick<BlockoutStateResult, "scenes" | "shots">): TreeRow[] => {
  const out: TreeRow[] = [];
  for (const g of groupByScene(state)) {
    out.push({ kind: "scene", id: `scene:${g.scene_number}`, depth: 0, label: blockoutFmt("scene_n", { n: g.scene_number }), sceneNumber: g.scene_number });
    for (const c of g.chains) {
      out.push({ kind: "chain", id: `chain:${c.chain_id}`, depth: 1, label: blockoutFmt("chain_n", { id: c.chain_id }), chainId: c.chain_id, blendExists: c.blend_exists });
      out.push(...c.shots.map(shotRow));
    }
    if (g.unchained.length > 0) {
      out.push({ kind: "unchained", id: `unchained:${g.scene_number}`, depth: 1, label: BLOCKOUT_TEXTS.unchained });
      out.push(...g.unchained.map(shotRow));
    }
  }
  return out;
};

// ---- итоги и инструменты ----

const SUMMARY_ORDER: { id: "rendered" | "stale" | "missing" | "junction_error" | "p10"; text: BlockoutTextId }[] = [
  { id: "rendered", text: "summary_rendered" },
  { id: "stale", text: "summary_stale" },
  { id: "missing", text: "summary_missing" },
  { id: "junction_error", text: "summary_junction_error" },
  { id: "p10", text: "summary_p10" },
];

export const summaryParts = (summary: BlockoutStateResult["summary"]): { id: string; label: string; count: number }[] =>
  SUMMARY_ORDER.map((p) => ({ id: p.id, label: BLOCKOUT_TEXTS[p.text], count: summary[p.id] }));

export const summaryLine = (summary: BlockoutStateResult["summary"]): string =>
  summaryParts(summary)
    .map((p) => `${p.label}: ${p.count}`)
    .join(" · ");

export const toolsLine = (tools: BlockoutStateResult["tools"]): string =>
  [
    `${BLOCKOUT_TEXTS.tool_blender}: ${tools.blender.message}`,
    tools.ffmpeg.available ? BLOCKOUT_TEXTS.tool_ffmpeg_ok : BLOCKOUT_TEXTS.tool_ffmpeg_missing,
  ].join(" · ");

// ---- состояние экрана ----

export type BlockoutScreenState = "loading" | "error" | "not_built" | "no_shots" | "ready";

export const resolveBlockoutScreen = (a: { state: BlockoutStateResult | null; error: unknown }): BlockoutScreenState => {
  if (!a.state) return a.error ? "error" : "loading";
  if (a.state.empty_reason === "not_built") return "not_built";
  if (a.state.empty_reason === "no_shots") return "no_shots";
  return "ready";
};

// ---- действия ----

export const ACTION_ORDER: readonly BlockoutActionKey[] = [
  "redraw_shot",
  "redraw_chain",
  "rebuild_scene",
  "build_preview",
  "leave_as_is",
  "regenerate_shots",
];

const ACTION_TEXT: Record<BlockoutActionKey, BlockoutTextId> = {
  redraw_shot: "act_redraw_shot",
  redraw_chain: "act_redraw_chain",
  rebuild_scene: "act_rebuild_scene",
  build_preview: "act_build_preview",
  leave_as_is: "act_leave_as_is",
  regenerate_shots: "act_regenerate_shots",
  set_duration: "duration_apply",
};

export const actionLabel = (action: BlockoutActionKey): string => BLOCKOUT_TEXTS[ACTION_TEXT[action]];

// Эти действия рисует Blender: без него они недоступны
const BLENDER_ACTIONS: readonly BlockoutActionKey[] = ["redraw_shot", "redraw_chain", "rebuild_scene"];

export type ActionContext = { runBusy: boolean; forbidden: boolean };
export type ActionButton = { action: BlockoutActionKey; label: string; enabled: boolean; hint: string | null };

export const actionButtons = (state: BlockoutStateResult, selected: readonly string[], ctx: ActionContext): ActionButton[] =>
  ACTION_ORDER.map((action) => {
    const label = actionLabel(action);
    if (ctx.forbidden) return { action, label, enabled: false, hint: NO_RIGHTS_TEXT };
    if (ctx.runBusy || state.run.active) return { action, label, enabled: false, hint: BLOCKOUT_TEXTS.run_busy };
    if (!state.tools.blender.available && BLENDER_ACTIONS.includes(action)) {
      return { action, label, enabled: false, hint: BLOCKOUT_TEXTS.no_blender };
    }
    const enabled = actionEnabled(state, action, [...selected]);
    return { action, label, enabled, hint: enabled ? null : actionBlockerText(state, action, [...selected]) };
  });

export type PlanTarget = { shot_keys?: string[]; chain_id?: string };

// Что передать в blockout_plan / blockout_run: шот, цепочка шота или набор шотов
export const planTarget = (action: BlockoutAction, state: Pick<BlockoutStateResult, "shots">, selected: readonly string[]): PlanTarget => {
  switch (action) {
    case "redraw_shot":
      return { shot_keys: selected.slice(0, 1) };
    case "redraw_chain": {
      const row = state.shots.find((s) => s.key === selected[0]);
      return row?.chain_id ? { chain_id: row.chain_id } : { shot_keys: selected.slice(0, 1) };
    }
    case "regenerate_shots":
      return { shot_keys: [...selected] };
    default:
      return {};
  }
};

// ---- окно подтверждения ----

export type PlanDialogView =
  | { kind: "problems"; title: string; problems: DialogProblem[]; close: string }
  | { kind: "confirm"; title: string; text: string; problems: DialogProblem[]; notes: string[]; go: string; cancel: string };

export type DialogProblem = { level: BlockoutProblem["level"]; label: string; message: string };

const LEVEL_TEXT: Record<BlockoutProblem["level"], BlockoutTextId> = { error: "level_error", warning: "level_warning", info: "level_info" };

const dialogProblems = (problems: readonly BlockoutProblem[]): DialogProblem[] =>
  problems.map((p) => ({ level: p.level, label: BLOCKOUT_TEXTS[LEVEL_TEXT[p.level]], message: p.message }));

export const confirmLabel = (action: BlockoutAction, costRub: number | null): string => {
  const regen = action === "regenerate_shots";
  if (costRub !== null && costRub > 0) {
    return blockoutFmt(regen ? "dialog_regen_go_paid" : "dialog_go_paid", { cost: formatShotMoney(costRub) });
  }
  return regen ? BLOCKOUT_TEXTS.dialog_regen_go : BLOCKOUT_TEXTS.dialog_go;
};

// Сумму собираем сами из числа: серверный текст даёт копейки ("12.50 ₽"), а нужны целые рубли
export const regenText = (plan: BlockoutPlanResult, shotCount: number): string => {
  const cost = plan.confirm.cost_rub;
  const estimate = plan.plan.estimate as { complete?: boolean } | null | undefined;
  const money = cost === null ? "" : formatShotMoney(cost);
  const price =
    cost === null
      ? BLOCKOUT_TEXTS.dialog_price_unknown
      : estimate && estimate.complete === false
        ? blockoutFmt("dialog_price_partial", { cost: money })
        : blockoutFmt("dialog_price", { cost: money });
  return blockoutFmt("dialog_regen_text", { n: shotCount, price });
};

export const planDialogView = (action: BlockoutAction, plan: BlockoutPlanResult, shotCount = 0): PlanDialogView => {
  const problems = dialogProblems(plan.problems);
  if (!plan.can_start) {
    return { kind: "problems", title: BLOCKOUT_TEXTS.dialog_problems_title, problems, close: BLOCKOUT_TEXTS.dialog_close };
  }
  return {
    kind: "confirm",
    title: plan.confirm.title ?? actionLabel(action),
    text: action === "regenerate_shots" && shotCount > 0 ? regenText(plan, shotCount) : plan.confirm.text ?? "",
    problems,
    notes: plan.notes,
    go: confirmLabel(action, plan.confirm.cost_rub),
    cancel: BLOCKOUT_TEXTS.dialog_cancel,
  };
};

export const successToast = (action: BlockoutAction): string =>
  BLOCKOUT_TEXTS[`toast_${action}` as "toast_redraw_shot"];

// ---- длительность ----

export const isShotDetail = (d: BlockoutDetailResult | null): d is BlockoutShotDetail => !!d && "state" in d;

export type DurationView = { allowed: number[]; current: number | null; editable: boolean; reason: string | null };

// Сервер кладёт в duration.reason машинный код блокера; для неизвестного кода - общий текст
const DURATION_REASONS: Record<string, BlockoutTextId> = {
  no_durations: "duration_reason_no_durations",
  screenplay_running: "duration_reason_screenplay_running",
  checkpoint_unknown: "duration_reason_checkpoint_unknown",
  no_checkpoint: "duration_reason_no_checkpoint",
  no_start_item: "duration_reason_no_start_item",
};

export const durationReasonText = (code: string | null | undefined): string => {
  const id = code ? DURATION_REASONS[code] : undefined;
  return id ? BLOCKOUT_TEXTS[id] : BLOCKOUT_TEXTS.duration_locked;
};

export const durationView = (detail: BlockoutShotDetail): DurationView => ({
  allowed: detail.duration.allowed,
  current: detail.duration.current_s,
  editable: detail.duration.editable && detail.duration.allowed.length > 0,
  reason: detail.duration.editable ? null : durationReasonText(detail.duration.reason),
});

export const canApplyDuration = (view: DurationView, chosen: number | null): boolean =>
  view.editable && chosen !== null && chosen !== view.current && view.allowed.includes(chosen);

export const leaveAsIsText = (r: { changed: string[]; skipped: { key: string }[] }): string =>
  [blockoutFmt("leave_done", { n: r.changed.length }), r.skipped.length > 0 ? blockoutFmt("leave_skipped", { n: r.skipped.length }) : ""]
    .filter(Boolean)
    .join(". ");

// Ошибка смены длительности: conflict значит «отпечаток устарел» - данные надо перечитать
export const durationFailure = (err: unknown, fallback: (e: unknown) => string): { text: string; reload: boolean } =>
  isConflict(err) ? { text: BLOCKOUT_TEXTS.duration_stale, reload: true } : { text: fallback(err), reload: false };
