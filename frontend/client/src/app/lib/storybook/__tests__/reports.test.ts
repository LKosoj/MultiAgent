import { describe, expect, it, vi } from "vitest";

import type { StorybookClient } from "../client";
import {
  compareShotKeys,
  groupFindingsByShot,
  hasIssues,
  makeShotKey,
  reportsForShot,
  sortFindings,
  worstLevel,
  type ReportFinding,
} from "../reports";

const fakeClient = () => {
  const call = vi.fn().mockResolvedValue({ status: "success" });
  return { client: { call } as unknown as StorybookClient, call };
};

const finding = (over: Partial<ReportFinding>): ReportFinding => ({
  id: "x",
  level: "warning",
  level_title: "",
  source: "provider_jobs",
  source_title: "",
  code: "c",
  message: "m",
  shot_key: null,
  related_shot_keys: [],
  phase: null,
  scope: "project",
  file: "f.json",
  details: {},
  ...over,
});

describe("обёртки reports", () => {
  it("зовут нужные id с нагрузкой", async () => {
    const { client, call } = fakeClient();
    await reportsForShot(client, { project_id: "p", shot_key: "2-3" });
    expect(call).toHaveBeenNthCalledWith(1, "reports_for_shot", { project_id: "p", shot_key: "2-3" });
  });
});

describe("помощники", () => {
  it("makeShotKey принимает числа и строки, отбрасывает мусор", () => {
    expect(makeShotKey(10, "1")).toBe("10-1");
    expect(makeShotKey("02", 3)).toBe("2-3");
    expect(makeShotKey(null, 1)).toBeNull();
    expect(makeShotKey(-1, 1)).toBeNull();
    expect(makeShotKey("x", 1)).toBeNull();
    expect(makeShotKey(1.5, 1)).toBeNull();
  });

  it("compareShotKeys сортирует числом, а не строкой", () => {
    expect(["10-1", "9-6", "2-10", "2-2"].sort(compareShotKeys)).toEqual(["2-2", "2-10", "9-6", "10-1"]);
    expect(["?", "1-1"].sort(compareShotKeys)).toEqual(["1-1", "?"]);
  });

  it("sortFindings: уровень, потом шот, порядок стабилен", () => {
    const a = finding({ id: "a", level: "warning", shot_key: "1-1" });
    const b = finding({ id: "b", level: "error", shot_key: "10-1" });
    const c = finding({ id: "c", level: "error", shot_key: "9-6" });
    const d = finding({ id: "d", level: "error", shot_key: "9-6" });
    expect(sortFindings([a, b, c, d]).map((f) => f.id)).toEqual(["c", "d", "b", "a"]);
  });

  it("worstLevel и hasIssues", () => {
    expect(worstLevel({ error: 0, warning: 2, info: 5 })).toBe("warning");
    expect(worstLevel({ error: 0, warning: 0, info: 1 })).toBe("info");
    expect(worstLevel({ error: 0, warning: 0, info: 0 })).toBeNull();
    expect(worstLevel(undefined)).toBeNull();
    expect(hasIssues({ error: 0, warning: 0, info: 9 })).toBe(false);
    expect(hasIssues({ error: 1, warning: 0, info: 0 })).toBe(true);
  });

  it("groupFindingsByShot раскладывает группу по шотам", () => {
    const own = finding({ id: "own", shot_key: "2-3", scope: "shot" });
    const grp = finding({ id: "grp", related_shot_keys: ["2-2", "2-3"], scope: "group" });
    const result = groupFindingsByShot([own, grp]);
    expect(result["2-3"].map((f) => f.id)).toEqual(["own", "grp"]);
    expect(result["2-2"].map((f) => f.id)).toEqual(["grp"]);
  });
});
