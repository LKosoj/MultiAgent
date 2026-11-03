import { describe, expect, it } from "vitest";

import { StudioError, type StorybookClient } from "../client";
import { inflightWarning, loadPlanBundle, needsReload, outcomeText, planTotal, reconcileAfterSave, shownOf, startRegen, startRun } from "../generationFlow";
import { shouldDiscard } from "../generationModel";

type Handler = (payload: Record<string, unknown>) => unknown;

const fake = (handlers: Record<string, Handler>) => {
  const calls: { id: string; payload: Record<string, unknown> }[] = [];
  const client = {
    call: async (id: string, payload: Record<string, unknown> = {}) => {
      calls.push({ id, payload });
      const h = handlers[id];
      if (!h) throw new Error(`нет обработчика ${id}`);
      return h(payload);
    },
  } as unknown as StorybookClient;
  return { client, calls };
};

const est = (total: number) => ({ currency: "RUB", components: [], total_known_rub: total, complete: true, unknown_components: [], budget_verdict: "no_budget", warnings: [] });
const plan = (over: Record<string, unknown> = {}) => ({
  plan: { can_start: true, plan_digest: "d1", steps_to_run: ["a"], skip_steps: [], problems: [], estimate: est(100), inputs: {}, ...over },
});

describe("план и оценка", () => {
  it("оценка берётся из плана; сбой сводки не мешает плану", async () => {
    const { client } = fake({
      run_plan: () => plan(),
      run_preflight_summary: () => {
        throw new StudioError("unavailable", "нет связи");
      },
    });
    const b = await loadPlanBundle(client, "p", { mode: "full" });
    expect(b.estimate?.total_known_rub).toBe(100);
    expect(b.estimateError).toBeTruthy();
    expect(planTotal(b.plan)).toBe(100);
  });
  it("без оценки в плане берём сводку", async () => {
    const { client, calls } = fake({
      run_plan: () => plan({ estimate: null }),
      run_preflight_summary: () => ({ estimate: est(7), readiness: {} }),
    });
    const b = await loadPlanBundle(client, "p", { mode: "full" });
    expect(b.estimate?.total_known_rub).toBe(7);
    expect(calls[1].payload.step_ids).toEqual(["a"]);
  });
});

describe("запуск", () => {
  const shown = { digest: "d1", total: 100 };
  it("план заново, отпечаток совпал: запуск с expected_plan_digest и launcher=backend", async () => {
    const { client, calls } = fake({ run_plan: () => plan(), run_start: () => ({ run_id: "r1" }) });
    const o = await startRun(client, "p", { mode: "full" }, shown, { discardUnfinished: shouldDiscard("full", true) });
    expect(o).toEqual({ kind: "started", runId: "r1" });
    expect(calls[1].payload).toMatchObject({ launcher: "backend", expected_plan_digest: "d1", discard_unfinished: true });
  });
  it("отпечаток или сумма изменились: запуска нет", async () => {
    const a = fake({ run_plan: () => plan({ plan_digest: "d2" }), run_start: () => ({}) });
    expect((await startRun(a.client, "p", { mode: "full" }, shown)).kind).toBe("plan_changed");
    expect(a.calls.map((c) => c.id)).toEqual(["run_plan"]);
    const b = fake({ run_plan: () => plan({ estimate: est(150) }), run_start: () => ({}) });
    expect((await startRun(b.client, "p", { mode: "full" }, shown)).kind).toBe("cost_changed");
  });
  it("план перестал разрешать запуск", async () => {
    const { client } = fake({ run_plan: () => plan({ can_start: false, problems: [{ level: "error", code: "x", message: "Нельзя", step_id: null, artifact: null }] }) });
    expect(await startRun(client, "p", { mode: "full" }, shown)).toEqual({ kind: "blocked", text: "Нельзя" });
  });
  it("отказы сервера: замок, конфликт плана", async () => {
    const locked = fake({ run_plan: () => plan(), run_start: () => { throw new StudioError("locked", "занят"); } });
    expect((await startRun(locked.client, "p", { mode: "full" }, shown)).kind).toBe("locked");
    const conflict = fake({ run_plan: () => plan(), run_start: () => { throw new StudioError("conflict", "x", { reason: "plan_changed" }); } });
    expect((await startRun(conflict.client, "p", { mode: "full" }, shown)).kind).toBe("plan_changed");
  });
  it("тексты исходов", () => {
    expect(outcomeText({ kind: "locked" })).toContain("другое приложение");
    expect(outcomeText({ kind: "started", runId: null })).toBeNull();
    expect(outcomeText({ kind: "paid_pending", keys: ["1-1", "1-2"] })).toContain("1-1, 1-2");
    expect(shownOf(plan().plan as never)).toEqual({ digest: "d1", total: 100 });
  });
  it("предупреждение об оплаченных задачах видео", () => {
    expect(inflightWarning({ problems: [{ code: "video_jobs_inflight", message: "Идут задачи" }] } as never)).toBe("Идут задачи");
    expect(inflightWarning(null)).toBeNull();
  });
});

describe("перегенерация шотов", () => {
  const preview = (digest: string, cost: number) => ({ plan_digest: digest, can_start: true, problems: [], confirm_required: { cost_rub: cost, unknown_cost: false } });
  const shown = { plan_digest: "p1", confirm_required: { cost_rub: 50, unknown_cost: false } };
  it("отпечаток и сумма сверяются; запуск с подтверждением суммы и без бросания заданий по умолчанию", async () => {
    const ok = fake({ regenerate_preview: () => preview("p1", 50), regenerate_start: () => ({ run_id: "r9" }) });
    expect(await startRegen(ok.client, "p", "both", ["1-1"], shown, false)).toEqual({ kind: "started", runId: "r9" });
    expect(ok.calls[1].payload).toMatchObject({ confirm_cost_rub: 50, expected_plan_digest: "p1", launcher: "backend" });
    expect(ok.calls[1].payload.abandon_pending_jobs).toBeUndefined();
    const dig = fake({ regenerate_preview: () => preview("p2", 50) });
    expect((await startRegen(dig.client, "p", "both", ["1-1"], shown, false)).kind).toBe("plan_changed");
    const cost = fake({ regenerate_preview: () => preview("p1", 80) });
    expect((await startRegen(cost.client, "p", "both", ["1-1"], shown, false)).kind).toBe("cost_changed");
  });
  it("висящие платные задания: ключи в ответе, а «бросить» передаётся отдельным флагом", async () => {
    const pend = fake({
      regenerate_preview: () => preview("p1", 50),
      regenerate_start: () => { throw new StudioError("conflict", "x", { reason: "paid_job_pending", keys: ["2-1", "1-3"] }); },
    });
    expect(await startRegen(pend.client, "p", "video", ["1-3"], shown, false)).toEqual({ kind: "paid_pending", keys: ["1-3", "2-1"] });
    const abandon = fake({ regenerate_preview: () => preview("p1", 50), regenerate_start: () => ({ run_id: null }) });
    await startRegen(abandon.client, "p", "video", ["1-3"], shown, true);
    expect(abandon.calls[1].payload.abandon_pending_jobs).toBe(true);
  });
});

describe("незавершённый прогон и перечитывание плана", () => {
  const shown = { digest: "d1", total: 100 };
  it("discard_unfinished уходит только при запуске сначала", async () => {
    for (const [mode, resumable, expected] of [["full", true, true], ["resume", true, false], ["full", false, false]] as const) {
      const { client, calls } = fake({ run_plan: () => plan(), run_start: () => ({ run_id: "r" }) });
      await startRun(client, "p", { mode }, shown, { discardUnfinished: shouldDiscard(mode, resumable) });
      expect("discard_unfinished" in calls[1].payload).toBe(expected);
    }
  });
  it("изменился -> перечитал -> старт проходит", async () => {
    let digest = "d2";
    const { client, calls } = fake({ run_plan: () => plan({ plan_digest: digest }), run_start: () => ({ run_id: "r1" }) });
    const first = await startRun(client, "p", { mode: "full" }, shown);
    expect(first.kind).toBe("plan_changed");
    expect(needsReload(first)).toBe(true);
    const fresh = await loadPlanBundle(client, "p", { mode: "full" });
    const second = await startRun(client, "p", { mode: "full" }, shownOf(fresh.plan));
    expect(second).toEqual({ kind: "started", runId: "r1" });
    expect(calls.filter((c) => c.id === "run_start")).toHaveLength(1);
    digest = "d3";
  });
  it("перечитывание нужно только после смены плана или суммы", () => {
    expect(needsReload({ kind: "plan_changed" })).toBe(true);
    expect(needsReload({ kind: "cost_changed" })).toBe(true);
    expect(needsReload({ kind: "locked" })).toBe(false);
    expect(needsReload({ kind: "started", runId: null })).toBe(false);
  });
  it("с несохранёнными настройками оценка берётся из сводки по форме, а не из плана", async () => {
    const { client } = fake({ run_plan: () => plan(), run_preflight_summary: () => ({ estimate: est(7), readiness: {} }) });
    const b = await loadPlanBundle(client, "p", { mode: "full" }, {} as never);
    expect(b.estimate?.total_known_rub).toBe(7);
  });
});

describe("сверка после сохранения настроек", () => {
  const bundle = (digest: string, total: number) => ({ plan: plan({ plan_digest: digest }).plan, summary: null, estimate: est(total), estimateError: null }) as never;
  it("тот же отпечаток и сумма - можно запускать", () => {
    expect(reconcileAfterSave(bundle("d1", 5), bundle("d1", 5))).toBeNull();
  });
  it("изменилась сумма или отпечаток - нужно новое подтверждение", () => {
    expect(reconcileAfterSave(bundle("d1", 5), bundle("d1", 6))).toEqual({ kind: "cost_changed" });
    expect(reconcileAfterSave(bundle("d1", 5), bundle("d2", 5))).toEqual({ kind: "plan_changed" });
    expect(reconcileAfterSave(null, bundle("d2", 5))).toEqual({ kind: "plan_changed" });
  });
});
