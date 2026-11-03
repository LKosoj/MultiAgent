import { describe, expect, it } from "vitest";

import { stepStatusWord } from "../generationTexts";
import { makeTitleOf, mergeStepRows, nextFollow, stepRows } from "../stepsView";
import type { RunStepRow } from "../runs";

const row = (id: string, status: RunStepRow["status"], extra: Partial<RunStepRow> = {}): RunStepRow => ({
  id,
  status,
  started_at: null,
  finished_at: null,
  duration_s: null,
  error: null,
  ...extra,
});

describe("шаги", () => {
  it("названия из каталога, иначе id", () => {
    const t = makeTitleOf([{ id: "a", depends_on: [], title: "Сценарий" }]);
    expect(t("a")).toBe("Сценарий");
    expect(t("zzz")).toBe("zzz");
  });
  it("текущий шаг - идущий; слова статусов", () => {
    const rows = stepRows([row("a", "completed", { duration_s: 90 }), row("b", "running", { started_at: "2026-11-03T10:00:00" }), row("c", "pending")], (i) => i.toUpperCase(), Date.parse("2026-11-03T10:00:30"));
    expect(rows.map((r) => r.current)).toEqual([false, true, false]);
    expect(rows.map((r) => r.word)).toEqual(["Готово", "Выполняется", "Ожидает"]);
    expect(rows[0].duration).toBe("2 мин");
    expect(rows[1].duration).toBe("30 с");
  });
  it("без идущего текущим считается сбойный", () => {
    const rows = stepRows([row("a", "completed"), row("b", "failed", { error: "x".repeat(400) })], (i) => i, 0);
    expect(rows[1].current).toBe(true);
    expect(rows[1].error).toHaveLength(300);
    expect(stepStatusWord("weird")).toBe("weird");
  });
  it("автопрокрутка выключается ручной прокруткой и включается со сменой шага", () => {
    let f = nextFollow({ follow: true, lastCurrent: null }, "a", false);
    f = nextFollow(f, "a", true);
    expect(f.follow).toBe(false);
    expect(nextFollow(f, "a", false).follow).toBe(false);
    expect(nextFollow(f, "b", false).follow).toBe(true);
  });
});

describe("шаги каталога со статусами", () => {
  it("до первого прогона видны все шаги каталога как ожидающие", () => {
    const merged = mergeStepRows([{ id: "a", depends_on: [] }, { id: "b", depends_on: ["a"] }], []);
    expect(merged.map((r) => [r.id, r.status])).toEqual([["a", "pending"], ["b", "pending"]]);
  });
  it("статусы прогона накладываются на каталог, лишние шаги добавляются в конец", () => {
    const merged = mergeStepRows([{ id: "a", depends_on: [] }, { id: "b", depends_on: [] }], [row("b", "completed"), row("x", "running")]);
    expect(merged.map((r) => [r.id, r.status])).toEqual([["a", "pending"], ["b", "completed"], ["x", "running"]]);
  });
});
