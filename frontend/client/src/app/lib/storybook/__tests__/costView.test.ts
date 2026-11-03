import { describe, expect, it } from "vitest";

import { budgetLine, costRows, estimateHeadline } from "../costView";
import type { CostEstimateResult } from "../runSettings";

const comp = (id: string, amount: number | null, basis: "exact" | "rough" | "unknown") =>
  ({ id, units: 1, amount_rub: amount, price_source: "x", basis }) as CostEstimateResult["components"][number];

describe("оценка", () => {
  it("разбивка: порядок, неизвестное чёрточкой, ориентировочно", () => {
    const rows = costRows({ components: [comp("images", 10.2, "exact"), comp("video", 1180, "rough"), comp("music", null, "unknown")] });
    expect(rows.map((r) => r.id)).toEqual(["video", "images", "music"]);
    expect(rows[0]).toMatchObject({ title: "Видео", rough: true });
    expect(rows[2].amount).toBe("—");
  });
  it("вердикты бюджета", () => {
    expect(budgetLine({ budget_verdict: "no_budget" }).text).toBe("Бюджет не задан");
    expect(budgetLine({ budget_verdict: "enough" })).toEqual({ text: "Бюджет: хватает", tone: "ok" });
    expect(budgetLine({ budget_verdict: "short", short_by_rub: 40 }).text).toContain("Бюджет меньше оценки на 40");
    expect(budgetLine({ budget_verdict: "cannot_check" }).text).toBe("Бюджет проверить нельзя");
  });
  it("малая сумма показывается точно", () => {
    expect(estimateHeadline({ complete: true, total_known_rub: 0.3, unknown_components: [] })).toContain("0,30");
  });
});
