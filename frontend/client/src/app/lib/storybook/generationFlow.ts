// Запуск прогона на клиенте: план, оценка, повторный план перед стартом, обработка отказов сервера.
// Серверного подтверждения суммы нет, поэтому перед стартом план строится заново и сверяются отпечаток и сумма.
import { StudioError, type StorybookClient } from "./client";
import { isCostEstimate } from "./costView";
import { gfmt } from "./generationTexts";
import type { PlanParams } from "./generationModel";
import {
  regeneratePreview,
  regenerateStart,
  startPayloadFromPreview,
  type RegenWhat,
  type RegeneratePreviewResult,
} from "./review";
import { runPlan, runStart, type RunPlan } from "./runs";
import { runPreflightSummary, type CostEstimateResult, type PreflightSummaryResult, type RunSettings } from "./runSettings";
import { errorReason, pendingKeysFromError, shotErrorFrom } from "./shotsView";
import { classifyStorybookError } from "./uiErrors";
import { PROJECT_LOCKED_TEXT } from "./texts";

export type PlanBundle = {
  plan: RunPlan;
  summary: PreflightSummaryResult | null;
  estimate: CostEstimateResult | null;
  estimateError: string | null;
};

// Сумма из плана; null - плана с оценкой нет
export const planTotal = (plan: RunPlan): number | null =>
  isCostEstimate(plan.estimate) ? plan.estimate.total_known_rub : null;

// План и сводка. Сбой оценки не мешает показать план: запуск тогда возможен только «без оценки»
export const loadPlanBundle = async (
  client: StorybookClient,
  projectId: string,
  params: PlanParams,
  settings?: RunSettings,
): Promise<PlanBundle> => {
  const { plan } = await runPlan(client, { project_id: projectId, ...params });
  let summary: PreflightSummaryResult | null = null;
  let estimateError: string | null = null;
  try {
    summary = await runPreflightSummary(client, {
      project_id: projectId,
      step_ids: plan.steps_to_run,
      ...(settings ? { settings } : {}),
    });
  } catch (err) {
    estimateError = classifyStorybookError(err).text;
  }
  // с несохранёнными настройками оценка из плана считает по сохранённым: показываем оценку сводки по форме
  const estimate = settings ? (summary?.estimate ?? null) : isCostEstimate(plan.estimate) ? plan.estimate : (summary?.estimate ?? null);
  if (!estimate && estimateError === null) estimateError = gfmt("estimate_unknown");
  return { plan, summary, estimate, estimateError };
};

export type FlowOutcome =
  | { kind: "started"; runId: string | null }
  | { kind: "plan_changed"; plan?: RunPlan }
  | { kind: "cost_changed" }
  | { kind: "blocked"; text: string }
  | { kind: "locked" }
  | { kind: "paid_pending"; keys: string[] }
  | { kind: "error"; text: string };

// После этих исходов показанный план устарел: его нужно перечитать, иначе следующий старт снова упрётся в то же
export const needsReload = (o: FlowOutcome): boolean => o.kind === "plan_changed" || o.kind === "cost_changed";

// «Сохранить -> перестроить -> сверить»: что показывали до сохранения настроек и что получилось после.
// Любое расхождение суммы или отпечатка плана требует нового подтверждения.
export const reconcileAfterSave = (before: PlanBundle | null, fresh: PlanBundle): FlowOutcome | null => {
  if (!before) return { kind: "plan_changed" };
  if (before.plan.plan_digest !== fresh.plan.plan_digest) return { kind: "plan_changed" };
  if ((before.estimate?.total_known_rub ?? null) !== (fresh.estimate?.total_known_rub ?? null)) return { kind: "cost_changed" };
  return null;
};

export type ShownPlan = { digest: string; total: number | null };

export const shownOf = (plan: RunPlan): ShownPlan => ({ digest: plan.plan_digest, total: planTotal(plan) });

const outcomeFromError = (err: unknown): FlowOutcome => {
  if (err instanceof StudioError) {
    if (err.code === "locked") return { kind: "locked" };
    if (err.code === "conflict") {
      const reason = errorReason(err);
      if (reason === "plan_changed") return { kind: "plan_changed" };
      if (reason === "cost_changed") return { kind: "cost_changed" };
      if (reason === "paid_job_pending") return { kind: "paid_pending", keys: pendingKeysFromError(err) };
      if (reason === "plan_blocked") {
        const problems = Array.isArray(err.details?.problems) ? (err.details?.problems as { message?: unknown }[]) : [];
        const first = problems.find((p) => typeof p?.message === "string");
        return { kind: "blocked", text: first ? String(first.message) : err.message };
      }
    }
  }
  return { kind: "error", text: classifyStorybookError(err).text };
};

export const startRun = async (
  client: StorybookClient,
  projectId: string,
  params: PlanParams,
  shown: ShownPlan,
  options: { discardUnfinished?: boolean } = {},
): Promise<FlowOutcome> => {
  try {
    const { plan: fresh } = await runPlan(client, { project_id: projectId, ...params });
    if (!fresh.can_start) {
      const first = fresh.problems.find((p) => p.level === "error");
      return { kind: "blocked", text: first?.message ?? "" };
    }
    if (fresh.plan_digest !== shown.digest) return { kind: "plan_changed", plan: fresh };
    const total = planTotal(fresh);
    if (total !== null && shown.total !== null && total !== shown.total) return { kind: "cost_changed" };
    const res = await runStart(client, {
      project_id: projectId,
      ...params,
      launcher: "backend",
      expected_plan_digest: fresh.plan_digest,
      ...(options.discardUnfinished ? { discard_unfinished: true } : {}),
    });
    return { kind: "started", runId: res.run_id };
  } catch (err) {
    return outcomeFromError(err);
  }
};

// ---- перегенерация выбранных шотов (через regenerate_*)

export const previewRegen = (client: StorybookClient, projectId: string, what: RegenWhat, keys: string[]) =>
  regeneratePreview(client, { project_id: projectId, what, shot_keys: keys });

export const startRegen = async (
  client: StorybookClient,
  projectId: string,
  what: RegenWhat,
  keys: string[],
  shown: Pick<RegeneratePreviewResult, "plan_digest" | "confirm_required">,
  abandonPending: boolean,
): Promise<FlowOutcome> => {
  try {
    const fresh = await previewRegen(client, projectId, what, keys);
    if (fresh.plan_digest !== shown.plan_digest) return { kind: "plan_changed" };
    if (fresh.confirm_required.cost_rub !== shown.confirm_required.cost_rub) return { kind: "cost_changed" };
    const payload = startPayloadFromPreview(
      { project_id: projectId, what, launcher: "backend", shot_keys: keys, ...(abandonPending ? { abandon_pending_jobs: true } : {}) },
      fresh,
      true,
    );
    const res = await regenerateStart(client, payload);
    return { kind: "started", runId: res.run_id };
  } catch (err) {
    const outcome = outcomeFromError(err);
    return outcome.kind === "error" ? { kind: "error", text: shotErrorFrom(err) } : outcome;
  }
};

// Предупреждение «video_jobs_inflight» из плана - это текст, кнопки «бросить» у обычных режимов нет
export const inflightWarning = (plan: RunPlan | null): string | null =>
  plan?.problems.find((p) => p.code === "video_jobs_inflight")?.message ?? null;

export const outcomeText = (o: FlowOutcome): string | null => {
  switch (o.kind) {
    case "plan_changed":
      return gfmt("changed_plan");
    case "cost_changed":
      return gfmt("changed_cost");
    case "locked":
      return PROJECT_LOCKED_TEXT;
    case "paid_pending":
      return gfmt("paid_pending", { keys: o.keys.join(", ") });
    case "blocked":
      return o.text || gfmt("changed_plan");
    case "error":
      return o.text;
    default:
      return null;
  }
};
