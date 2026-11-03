// Модель экрана «Генерация»: режимы, кнопка запуска, баннеры, уведомления. Чистые функции без React.
import { formatDuration } from "./format";
import { estimateHeadline } from "./costView";
import { gfmt, type GenTextId } from "./generationTexts";
import { isActiveState, type PlanProblem, type RunMode, type RunPlan, type RunStatusResult } from "./runs";
import type { CostEstimateResult, ProviderInfo, SettingsIssue } from "./runSettings";
import type { RunState } from "./status";
import { formatShotMoney } from "./shotsView";

export const RUN_MODES: readonly RunMode[] = ["full", "from_step", "single_step", "resume", "regenerate_shots"];

const MODE_TEXT: Record<RunMode, GenTextId> = {
  full: "mode_full",
  from_step: "mode_from_step",
  single_step: "mode_single_step",
  resume: "mode_resume",
  regenerate_shots: "mode_regenerate_shots",
};

export const modeLabel = (mode: RunMode): string => gfmt(MODE_TEXT[mode]);

export const isRunMode = (value: unknown): value is RunMode =>
  typeof value === "string" && (RUN_MODES as readonly string[]).includes(value);

// Прогон «занят»: идёт, на паузе или останавливается (одно определение на весь клиент)
export const isBusyState: (state: RunState | undefined) => boolean = isActiveState;

export type ModeOption = { mode: RunMode; label: string; enabled: boolean; hint: string | null };

export type ModeContext = { resumable: boolean; shotCount: number };

export const modeOptions = (ctx: ModeContext): ModeOption[] =>
  RUN_MODES.map((mode) => {
    if (mode === "resume" && !ctx.resumable) return { mode, label: modeLabel(mode), enabled: false, hint: gfmt("mode_resume_none") };
    if (mode === "regenerate_shots" && ctx.shotCount === 0) {
      return { mode, label: modeLabel(mode), enabled: false, hint: gfmt("mode_shots_none") };
    }
    return { mode, label: modeLabel(mode), enabled: true, hint: null };
  });

// «Продолжать нечего» - переключаемся на полный запуск, а не оставляем пользователя с пустой кнопкой
export const resolveMode = (mode: RunMode, ctx: ModeContext): RunMode => {
  const option = modeOptions(ctx).find((o) => o.mode === mode);
  return option && option.enabled ? mode : mode === "regenerate_shots" || mode === "resume" ? "full" : mode;
};

export type Selection = { mode: RunMode; stepId: string | null; shotKeys: string[] };

export type IntentLike = { mode?: string | null; stepId?: string | null; shotKeys?: string[] | null } | null | undefined;

// Что выбрать на экране по переходу из других экранов
export const selectionFromIntent = (intent: IntentLike, fallback: Selection, ctx: ModeContext): Selection => {
  if (!intent) return fallback;
  const keys = Array.isArray(intent.shotKeys) ? intent.shotKeys.filter((k) => typeof k === "string" && k !== "") : [];
  let mode: RunMode = isRunMode(intent.mode) ? intent.mode : fallback.mode;
  if (keys.length > 0) mode = "regenerate_shots";
  else if (intent.stepId && mode === "full") mode = "single_step";
  const stepId = intent.stepId ?? fallback.stepId;
  const shotKeys = keys.length > 0 ? keys : fallback.shotKeys;
  return { mode: resolveMode(mode, { resumable: ctx.resumable, shotCount: shotKeys.length }), stepId, shotKeys };
};

// Применять ли переход из другого экрана: один раз на объект и только после первого ответа run_status,
// иначе «продолжить» превратится в «весь пайплайн» (пока неизвестно, есть ли что продолжать)
export const shouldApplyIntent = (intent: IntentLike, applied: IntentLike, statusKnown: boolean): boolean =>
  !!intent && intent !== applied && statusKnown;

// ---- параметры плана

export type PlanParams = {
  mode: RunMode;
  start_step?: string;
  step_id?: string;
  shot_keys?: string[];
  inputs?: Record<string, unknown>;
  task?: string;
};

export const planParams = (sel: Selection, inputs: Record<string, unknown>, task: string): PlanParams => {
  const out: PlanParams = { mode: sel.mode };
  if (sel.mode === "from_step" && sel.stepId) out.start_step = sel.stepId;
  if (sel.mode === "single_step" && sel.stepId) out.step_id = sel.stepId;
  if (sel.mode === "regenerate_shots") out.shot_keys = sel.shotKeys;
  if (Object.keys(inputs).length > 0) out.inputs = inputs;
  if (task.trim() !== "") out.task = task.trim();
  return out;
};

// ---- тексты находок

const NEEDED = /:\s*([^;]+);/;

// missing_artifact: «нужны результаты шагов: a, b; запустите…» превращаем в русские названия
export const problemText = (p: Pick<PlanProblem, "code" | "message" | "step_id">, titleOf: (id: string) => string): string => {
  if (p.code === "missing_artifact" && p.step_id) {
    const m = NEEDED.exec(p.message);
    if (m) {
      const needed = m[1]
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean)
        .map(titleOf)
        .join(", ");
      return gfmt("step_block", { step: titleOf(p.step_id), needed });
    }
  }
  return p.message;
};

export type Note = { level: "error" | "warning" | "info"; text: string };

const LEVEL_ORDER = { error: 0, warning: 1, info: 2 } as const;

export type NotesInput = {
  plan: RunPlan | null;
  issues: SettingsIssue[];
  readinessBlocking: string[];
  readinessWarnings: string[];
  titleOf: (id: string) => string;
};

// Все замечания одним списком: сначала ошибки, затем предупреждения и сведения; одинаковый текст - один раз
export const collectNotes = (input: NotesInput): Note[] => {
  const notes: Note[] = [];
  for (const p of input.plan?.problems ?? []) notes.push({ level: p.level, text: problemText(p, input.titleOf) });
  for (const i of input.issues) notes.push({ level: i.level, text: i.message });
  // readiness только предупреждает: запуск он не блокирует
  for (const text of [...input.readinessBlocking, ...input.readinessWarnings]) notes.push({ level: "warning", text });
  const seen = new Set<string>();
  const out: Note[] = [];
  for (const n of notes) {
    if (!n.text || seen.has(n.text)) continue;
    seen.add(n.text);
    out.push(n);
  }
  return out.sort((a, b) => LEVEL_ORDER[a.level] - LEVEL_ORDER[b.level]);
};

// ---- кнопка запуска

export type RegenPreviewLike = { can_start: boolean; cost_rub: number; unknown_cost: boolean; problem: string | null };

export type StartInput = {
  loading: boolean;
  engineError: string | null; // engine.available === false
  isAdmin: boolean;
  runState: RunState | undefined;
  locked: boolean;
  selection: Selection;
  hasBrief: boolean;
  task: string;
  plan: RunPlan | null;
  planError: string | null;
  issues: SettingsIssue[];
  provider: Pick<ProviderInfo, "title" | "credentials_found"> | null;
  estimate: CostEstimateResult | null;
  estimateError: string | null;
  regen: RegenPreviewLike | null;
  titleOf: (id: string) => string;
};

export type StartButton = {
  label: string;
  enabled: boolean;
  reason: string | null;
  // true: перед запуском нужно подтверждение «без оценки»
  noEstimate: boolean;
  tone: "go" | "off";
};

const off = (label: string, reason: string | null): StartButton => ({ label, enabled: false, reason, noEstimate: false, tone: "off" });

export const blockingIssues = (issues: SettingsIssue[]): SettingsIssue[] => issues.filter((i) => i.level === "error");

// Запуск блокируют только: plan.can_start=false, ошибки настроек и нет ключа выбранного провайдера
export const startButton = (s: StartInput): StartButton => {
  const regen = s.selection.mode === "regenerate_shots";
  const idle = regen ? gfmt("go_regen") : gfmt("go");
  if (s.loading) return off(idle, gfmt("why_loading"));
  if (s.engineError !== null) return off(idle, gfmt("why_engine", { reason: s.engineError }));
  if (!s.isAdmin) return off(idle, gfmt("why_no_rights"));
  if (isBusyState(s.runState)) return off(gfmt("go_running"), null);
  if (s.locked) return off(idle, gfmt("why_busy"));
  const { mode, stepId, shotKeys } = s.selection;
  if ((mode === "from_step" || mode === "single_step") && !stepId) return off(idle, gfmt("why_pick_step"));
  if (regen && shotKeys.length === 0) return off(idle, gfmt("why_no_shots"));
  if (mode === "full" && !s.hasBrief && s.task.trim() === "") return off(idle, gfmt("why_no_brief"));

  const errors = blockingIssues(s.issues);
  if (errors.length > 0) return off(idle, gfmt("why_issues", { reason: errors[0].message }));
  if (s.provider && !s.provider.credentials_found) return off(idle, gfmt("why_no_key", { name: s.provider.title }));

  if (regen) {
    if (!s.regen) return off(idle, gfmt("why_loading"));
    if (!s.regen.can_start) return off(idle, gfmt("why_plan", { reason: s.regen.problem ?? "" }));
    if (s.regen.cost_rub > 0) {
      return { label: gfmt("go_regen_cost", { cost: formatShotMoney(s.regen.cost_rub) }), enabled: true, reason: null, noEstimate: false, tone: "go" };
    }
    if (s.regen.unknown_cost) return { label: gfmt("go_no_estimate"), enabled: true, reason: null, noEstimate: true, tone: "go" };
    return { label: gfmt("go_regen"), enabled: true, reason: null, noEstimate: false, tone: "go" };
  }

  if (s.planError !== null) return off(idle, gfmt("why_plan", { reason: s.planError }));
  if (!s.plan) return off(idle, gfmt("why_loading"));
  if (!s.plan.can_start) {
    const first = s.plan.problems.find((p) => p.level === "error");
    return off(idle, first ? problemText(first, s.titleOf) : gfmt("why_plan", { reason: "" }));
  }
  if (s.estimateError !== null || !s.estimate) {
    return { label: gfmt("go_no_estimate"), enabled: true, reason: s.estimateError, noEstimate: true, tone: "go" };
  }
  const e = s.estimate;
  if (e.total_known_rub > 0) {
    return { label: gfmt("go_cost", { cost: formatShotMoney(e.total_known_rub) }), enabled: true, reason: null, noEstimate: !e.complete, tone: "go" };
  }
  if (!e.complete) return { label: gfmt("go_no_estimate"), enabled: true, reason: null, noEstimate: true, tone: "go" };
  return { label: gfmt("go"), enabled: true, reason: null, noEstimate: false, tone: "go" };
};

// Нужны ли окна подтверждения: запуск, «без оценки», «оценка выше бюджета»
export type ConfirmKind = "start" | "no_estimate" | "over_budget";

export const confirmKind = (estimate: CostEstimateResult | null, estimateError: string | null): ConfirmKind => {
  if (estimateError !== null || !estimate) return "no_estimate";
  if (estimate.budget_verdict === "short") return "over_budget";
  if (!estimate.complete) return "no_estimate";
  return "start";
};

// Окно перед запуском с режима «весь пайплайн»: готовый прогон - «выполнятся заново», незавершённый - «начать сначала»
export const restartDialog = (mode: RunMode, state: RunState | undefined, resumable: boolean): "restart" | "discard" | null => {
  if (mode !== "full") return null;
  if (state === "completed") return "restart";
  return resumable ? "discard" : null;
};

// Незавершённый прогон стирается только при запуске «сначала»; при продолжении его нельзя трогать
export const shouldDiscard = (mode: RunMode, resumable: boolean): boolean => mode === "full" && resumable;

export const confirmBody = (kind: ConfirmKind, mode: RunMode, estimate: CostEstimateResult | null): string => {
  if (kind === "no_estimate") return gfmt("confirm_none_body");
  if (kind === "over_budget" && estimate) {
    return gfmt("confirm_over_body", { cost: formatShotMoney(estimate.total_known_rub), sum: formatShotMoney(estimate.short_by_rub ?? 0) });
  }
  return gfmt("confirm_start_body", { mode: modeLabel(mode), cost: estimate ? estimateHeadline(estimate).replace(/^Оценка: /, "") : "—" });
};

// ---- баннеры

export type BannerAction = "mark_interrupted" | "resume" | "restart" | "refresh" | null;
export type RunBanner = {
  kind: "engine" | "other_surface" | "unresponsive" | "interrupted" | "failed" | "stale_control" | "completed";
  tone: "error" | "warn" | "ok" | "info";
  text: string;
  action: BannerAction;
};

const REASON_LIMIT = 500;

export const secondsBetween = (from: unknown, to: unknown): number | null => {
  if (typeof from !== "string" || typeof to !== "string") return null;
  const a = Date.parse(from);
  const b = Date.parse(to);
  return Number.isFinite(a) && Number.isFinite(b) && b >= a ? Math.round((b - a) / 1000) : null;
};

// Шаг для баннера: из failure.step_id, иначе последний выполнявшийся
export const bannerStepId = (s: Pick<RunStatusResult, "failure" | "steps">): string | null => {
  if (s.failure?.step_id) return s.failure.step_id;
  const running = [...s.steps].reverse().find((r) => r.status === "running" || r.status === "failed");
  return running ? running.id : null;
};

export const bannerFor = (s: RunStatusResult | null, titleOf: (id: string) => string): RunBanner | null => {
  if (!s) return null;
  const engine = s.engine as { available?: boolean; error?: string | null };
  if (engine && engine.available === false) {
    return { kind: "engine", tone: "error", text: gfmt("banner_engine", { reason: engine.error ?? "причина неизвестна" }), action: null };
  }
  if (s.other_surface) return { kind: "other_surface", tone: "info", text: s.other_surface.text, action: null };
  const stepId = bannerStepId(s);
  const step = stepId ? titleOf(stepId) : "—";
  if (s.unresponsive && isBusyState(s.state)) {
    return { kind: "unresponsive", tone: "warn", text: gfmt("banner_unresponsive"), action: "mark_interrupted" };
  }
  if (s.state === "interrupted") return { kind: "interrupted", tone: "warn", text: gfmt("banner_interrupted", { step }), action: "resume" };
  if (s.state === "failed") {
    const reason = (s.failure?.message ?? "").slice(0, REASON_LIMIT) || "причина неизвестна";
    return { kind: "failed", tone: "error", text: gfmt("banner_failed", { step, reason }), action: "resume" };
  }
  if (s.stale_control) return { kind: "stale_control", tone: "warn", text: gfmt("banner_stale"), action: "refresh" };
  if (s.state === "completed") {
    const run = s.run as { started_at?: unknown; finished_at?: unknown } | null;
    const secs = secondsBetween(run?.started_at, run?.finished_at);
    return { kind: "completed", tone: "ok", text: gfmt("banner_completed", { time: secs === null ? "—" : formatDuration(secs) }), action: "restart" };
  }
  return null;
};

// Баннеры по порядку показа: «давно не отвечает» выше сообщения о прогоне из другого приложения
export const bannersFor = (s: RunStatusResult | null, titleOf: (id: string) => string): RunBanner[] => {
  const main = bannerFor(s, titleOf);
  if (!main) return [];
  if (s && main.kind === "other_surface" && s.unresponsive && isBusyState(s.state)) {
    return [{ kind: "unresponsive", tone: "warn", text: gfmt("banner_unresponsive"), action: "mark_interrupted" }, main];
  }
  return [main];
};

// Поля настроек блокируются на время прогона, кроме бюджета
export const settingsLocked = (state: RunState | undefined, locked: boolean): boolean => isBusyState(state) || locked;

// Строка «Шаг N из M: Имя» над полосой прогресса
export const stepLine = (s: RunStatusResult, titleOf: (id: string) => string): string => {
  const p = s.progress;
  if (!p || !p.step_id) return gfmt("progress_starting");
  return gfmt("step_n_of", { n: Math.max(p.step_index ?? 1, 1), total: p.step_total ?? 0, name: titleOf(p.step_id) });
};

// ---- уведомления о смене состояния прогона

export type RunToast = { text: string; type: "success" | "error" | "info" };

// Тост показывается один раз при переходе из «идёт» в итоговое состояние
export const transitionToast = (
  prev: RunState | undefined,
  next: RunStatusResult | null,
  titleOf: (id: string) => string,
): RunToast | null => {
  if (!next || !isBusyState(prev) || isBusyState(next.state)) return null;
  if (next.state === "completed") {
    const run = next.run as { started_at?: unknown; finished_at?: unknown } | null;
    const secs = secondsBetween(run?.started_at, run?.finished_at);
    return { text: gfmt("finished", { time: secs === null ? "—" : formatDuration(secs) }), type: "success" };
  }
  if (next.state === "failed") {
    const id = bannerStepId(next);
    const reason = (next.failure?.message ?? "").slice(0, REASON_LIMIT) || "причина неизвестна";
    return { text: gfmt("failed_toast", { step: id ? titleOf(id) : "—", reason }), type: "error" };
  }
  if (next.outcome === "cancelled" || next.state === "idle") return { text: gfmt("cancelled_toast"), type: "info" };
  return null;
};
