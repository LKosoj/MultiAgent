// Цепочка «план -> решение -> запуск» для действий болванки.
// 1) blockout_plan отдаёт отпечаток плана; 2) если запуск невозможен - только список проблем;
// 3) если нужно подтверждение - окно; иначе запуск сразу; 4) blockout_run получает expected_plan_digest;
// 5) conflict при запуске - план изменился, строим его заново и просим подтвердить ещё раз.
import {
  blockoutPlan,
  blockoutRun,
  needsConfirm,
  type BlockoutAction,
  type BlockoutPlanResult,
  type BlockoutRunResult,
} from "./blockout";
import type { PlanTarget } from "./blockoutView";
import { BLOCKOUT_TEXTS } from "./blockoutTexts";
import { StudioError, type StorybookClient } from "./client";
import { isConflict } from "./review";

// Для этих действий отпечаток плана обязателен: без него сервер запуск не примет
export const DIGEST_REQUIRED: readonly BlockoutAction[] = ["redraw_chain", "rebuild_scene", "regenerate_shots"];

export type FlowStep =
  | { kind: "blocked"; plan: BlockoutPlanResult }
  | { kind: "confirm"; plan: BlockoutPlanResult }
  | { kind: "started"; run: BlockoutRunResult };

export type RunOutcome =
  | { kind: "started"; run: BlockoutRunResult }
  // план изменился с момента показа: вот новый, пользователь подтверждает снова
  | { kind: "replan"; text: string; step: FlowStep }
  // запуск закрыт блокером: перепланировать бесполезно, показываем причину
  | { kind: "blocked"; text: string };

export type BlockoutFlowOptions = {
  client: StorybookClient;
  projectId: string;
  launcher?: "backend" | "local";
};

export const createBlockoutFlow = (o: BlockoutFlowOptions) => {
  const launcher = o.launcher ?? "backend";

  const runWith = (action: BlockoutAction, target: PlanTarget, plan: BlockoutPlanResult): Promise<BlockoutRunResult> =>
    blockoutRun(o.client, {
      project_id: o.projectId,
      action,
      launcher,
      ...target,
      ...(plan.plan_digest ? { expected_plan_digest: plan.plan_digest } : {}),
    });

  // Шаг после плана: проблемы, окно или сразу запуск
  const decide = async (action: BlockoutAction, target: PlanTarget, plan: BlockoutPlanResult): Promise<FlowStep> => {
    if (!plan.can_start) return { kind: "blocked", plan };
    if (DIGEST_REQUIRED.includes(action) && !plan.plan_digest) {
      return {
        kind: "blocked",
        plan: { ...plan, can_start: false, problems: [...plan.problems, { level: "error", code: "no_digest", message: BLOCKOUT_TEXTS.no_digest, step_id: null, artifact: null }] },
      };
    }
    if (needsConfirm(plan)) return { kind: "confirm", plan };
    return { kind: "started", run: await runWith(action, target, plan) };
  };

  const plan = (action: BlockoutAction, target: PlanTarget) =>
    blockoutPlan(o.client, { project_id: o.projectId, action, ...target });

  const start = async (action: BlockoutAction, target: PlanTarget): Promise<FlowStep> => decide(action, target, await plan(action, target));

  // Пользователь подтвердил показанный план
  const confirm = async (action: BlockoutAction, target: PlanTarget, shown: BlockoutPlanResult): Promise<RunOutcome> => {
    try {
      return { kind: "started", run: await runWith(action, target, shown) };
    } catch (err) {
      if (!isConflict(err)) throw err;
      if (isConflict(err, "plan_blocked")) return { kind: "blocked", text: (err instanceof StudioError && err.message.trim()) || BLOCKOUT_TEXTS.plan_blocked };
      const fresh = await plan(action, target);
      // после конфликта запуска без окна не бывает: просим подтвердить снова
      const step: FlowStep = fresh.can_start ? { kind: "confirm", plan: fresh } : { kind: "blocked", plan: fresh };
      return { kind: "replan", text: BLOCKOUT_TEXTS.plan_changed, step };
    }
  };

  return { start, confirm };
};

export type BlockoutFlow = ReturnType<typeof createBlockoutFlow>;
