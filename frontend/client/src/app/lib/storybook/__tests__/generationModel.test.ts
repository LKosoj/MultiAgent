import { describe, expect, it } from "vitest";

import {
  bannersFor,
  collectNotes,
  confirmKind,
  isBusyState,
  modeOptions,
  planParams,
  problemText,
  resolveMode,
  restartDialog,
  selectionFromIntent,
  settingsLocked,
  shouldApplyIntent,
  shouldDiscard,
  stepLine,
  transitionToast,
} from "../generationModel";
import type { RunPlan, RunStatusResult } from "../runs";
import type { CostEstimateResult } from "../runSettings";

const titleOf = (id: string) => ({ a: "Сценарий", b: "Раскадровка" })[id as "a" | "b"] ?? id;
const sel = { mode: "full" as const, stepId: null, shotKeys: [] };

describe("режимы", () => {
  it("«Продолжить» и «Перегенерировать» отключены без повода", () => {
    const o = modeOptions({ resumable: false, shotCount: 0 });
    expect(o.find((x) => x.mode === "resume")).toMatchObject({ enabled: false, hint: "Продолжать нечего: прошлый прогон завершён" });
    expect(o.find((x) => x.mode === "regenerate_shots")?.enabled).toBe(false);
    expect(o.find((x) => x.mode === "full")?.enabled).toBe(true);
  });
  it("нечего продолжать: переключаемся на полный запуск", () => {
    expect(resolveMode("resume", { resumable: false, shotCount: 0 })).toBe("full");
    expect(resolveMode("resume", { resumable: true, shotCount: 0 })).toBe("resume");
    expect(resolveMode("regenerate_shots", { resumable: true, shotCount: 2 })).toBe("regenerate_shots");
  });
  it("переход с шотами открывает перегенерацию", () => {
    const s = selectionFromIntent({ shotKeys: ["1-1", "1-2"] }, sel, { resumable: false, shotCount: 0 });
    expect(s).toEqual({ mode: "regenerate_shots", stepId: null, shotKeys: ["1-1", "1-2"] });
  });
  it("переход с шагом: один шаг, а сбойный шаг - продолжение", () => {
    expect(selectionFromIntent({ stepId: "a" }, sel, { resumable: true, shotCount: 0 })).toMatchObject({ mode: "single_step", stepId: "a" });
    expect(selectionFromIntent({ stepId: "a", mode: "resume" }, sel, { resumable: true, shotCount: 0 })).toMatchObject({ mode: "resume", stepId: "a" });
    expect(selectionFromIntent({ mode: "resume" }, sel, { resumable: false, shotCount: 0 }).mode).toBe("full");
    expect(selectionFromIntent(null, sel, { resumable: false, shotCount: 0 })).toBe(sel);
  });
  it("параметры плана по режиму", () => {
    expect(planParams({ mode: "from_step", stepId: "a", shotKeys: [] }, {}, "")).toEqual({ mode: "from_step", start_step: "a" });
    expect(planParams({ mode: "single_step", stepId: "a", shotKeys: [] }, {}, "")).toEqual({ mode: "single_step", step_id: "a" });
    expect(planParams(sel, { pages_min: 3 }, "  сказка ")).toEqual({ mode: "full", inputs: { pages_min: 3 }, task: "сказка" });
  });
});

describe("замечания", () => {
  const plan = {
    problems: [
      { level: "info", code: "brief_exists", message: "Бриф уже есть", step_id: null, artifact: null },
      { level: "error", code: "missing_artifact", message: "Для запуска нужны результаты шагов: a, b; запустите их раньше", step_id: "b", artifact: null },
      { level: "warning", code: "video_jobs_inflight", message: "Идут платные задачи", step_id: null, artifact: null },
    ],
  } as unknown as RunPlan;
  it("id шагов в тексте блокировки заменяются русскими названиями", () => {
    expect(problemText(plan.problems[1], titleOf)).toBe("Для шага «Раскадровка» нужны результаты шагов: Сценарий, Раскадровка; запустите их раньше");
  });
  it("порядок: ошибки, предупреждения, сведения; readiness только предупреждает; повторы убираются", () => {
    const notes = collectNotes({
      plan,
      issues: [{ code: "x", level: "warning", field: "f", message: "Идут платные задачи", scope: "run" }],
      readinessBlocking: ["Нет аудио"],
      readinessWarnings: [],
      titleOf,
    });
    expect(notes.map((n) => n.level)).toEqual(["error", "warning", "warning", "info"]);
    expect(notes.filter((n) => n.text === "Идут платные задачи")).toHaveLength(1);
    expect(notes.find((n) => n.text === "Нет аудио")?.level).toBe("warning");
  });
});

describe("подтверждение запуска", () => {
  const est = (p: Partial<CostEstimateResult>) => ({ complete: true, total_known_rub: 10, budget_verdict: "enough", components: [], ...p }) as CostEstimateResult;
  it("какое окно показать", () => {
    expect(confirmKind(est({}), null)).toBe("start");
    expect(confirmKind(est({ complete: false }), null)).toBe("no_estimate");
    expect(confirmKind(null, "сбой")).toBe("no_estimate");
    expect(confirmKind(est({ budget_verdict: "short", short_by_rub: 5 }), null)).toBe("over_budget");
  });
});

describe("прогон", () => {
  it("настройки блокируются на время прогона", () => {
    expect(settingsLocked("running", false)).toBe(true);
    expect(settingsLocked("idle", true)).toBe(true);
    expect(settingsLocked("idle", false)).toBe(false);
  });
  it("строка шага", () => {
    const s = { progress: { step_id: "a", step_index: 2, step_total: 5 } } as unknown as RunStatusResult;
    expect(stepLine(s, titleOf)).toBe("Шаг 2 из 5: Сценарий");
    expect(stepLine({ progress: null } as unknown as RunStatusResult, titleOf)).toBe("Прогон начинается…");
  });
  it("тост при завершении и сбое", () => {
    const done = { state: "completed", run: { started_at: "2026-11-03T10:00:00", finished_at: "2026-11-03T10:05:00" } } as unknown as RunStatusResult;
    expect(transitionToast("running", done, titleOf)).toEqual({ text: "Прогон завершён за 5 мин", type: "success" });
    const bad = { state: "failed", failure: { step_id: "a", message: "нет ключа" }, steps: [] } as unknown as RunStatusResult;
    expect(transitionToast("running", bad, titleOf)?.text).toBe("Прогон остановился на шаге «Сценарий»: нет ключа. Готовые шаги сохранены.");
    expect(transitionToast("idle", done, titleOf)).toBeNull();
  });
});

describe("перезапуск и переход из других экранов", () => {
  it("переход применяется один раз и только после первого ответа run_status", () => {
    const intent = { mode: "resume" };
    expect(shouldApplyIntent(intent, null, false)).toBe(false);
    expect(shouldApplyIntent(intent, null, true)).toBe(true);
    expect(shouldApplyIntent(intent, intent, true)).toBe(false);
    expect(shouldApplyIntent(null, null, true)).toBe(false);
  });
  it("«продолжить» после ответа сервера остаётся «продолжить», до ответа превратилось бы в «весь пайплайн»", () => {
    expect(resolveMode("resume", { resumable: false, shotCount: 0 })).toBe("full");
    expect(resolveMode("resume", { resumable: true, shotCount: 0 })).toBe("resume");
  });
  it("прерванный прогон стирается только при запуске сначала", () => {
    expect(shouldDiscard("full", true)).toBe(true);
    expect(shouldDiscard("resume", true)).toBe(false);
    expect(shouldDiscard("from_step", true)).toBe(false);
    expect(shouldDiscard("full", false)).toBe(false);
  });
  it("какое окно перед запуском сначала", () => {
    expect(restartDialog("full", "completed", false)).toBe("restart");
    expect(restartDialog("full", "failed", true)).toBe("discard");
    expect(restartDialog("full", "interrupted", true)).toBe("discard");
    expect(restartDialog("full", "idle", false)).toBeNull();
    expect(restartDialog("resume", "failed", true)).toBeNull();
  });
  it("занятое состояние - одно на весь клиент", () => {
    expect(["running", "paused", "pause_requested", "stopping"].every((s) => isBusyState(s as never))).toBe(true);
    expect(isBusyState("failed")).toBe(false);
  });
  it("«давно не отвечает» выше сообщения о прогоне из другого приложения, его текст сохранён", () => {
    const s = { state: "running", unresponsive: true, engine: { available: true }, other_surface: { surface: "tk", text: "Прогон идёт в приложении" }, steps: [] } as unknown as RunStatusResult;
    const list = bannersFor(s, titleOf);
    expect(list.map((b) => b.kind)).toEqual(["unresponsive", "other_surface"]);
    expect(list[0].action).toBe("mark_interrupted");
    expect(list[1].text).toBe("Прогон идёт в приложении");
    expect(bannersFor({ ...s, other_surface: null } as unknown as RunStatusResult, titleOf).map((b) => b.kind)).toEqual(["unresponsive"]);
    expect(bannersFor(null, titleOf)).toEqual([]);
  });
});
