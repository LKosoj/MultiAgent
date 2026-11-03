import { describe, expect, it, vi } from "vitest";

import { StudioError, type StorybookClient } from "../client";
import {
  isConflict,
  needsCostConfirm,
  nextStatusOnKey,
  regeneratePreview,
  reviewCountsFromEntries,
  reviewSetStatus,
  startPayloadFromPreview,
  versionLabel,
  versionsRestore,
  type RegeneratePreviewResult,
} from "../review";

const fakeClient = () => {
  const call = vi.fn().mockResolvedValue({ status: "success" });
  return { client: { call } as unknown as StorybookClient, call };
};

const preview = (cost: number, unknown = false): RegeneratePreviewResult =>
  ({ plan_digest: "d", confirm_required: { cost_rub: cost, unknown_cost: unknown } }) as RegeneratePreviewResult;

describe("обёртки", () => {
  it("вызывают возможности по id", async () => {
    const { client, call } = fakeClient();
    await reviewSetStatus(client, { project_id: "p", keys: ["1-1"], status: "approved" });
    await versionsRestore(client, { project_id: "p", key: "1-1", version_id: "v" });
    await regeneratePreview(client, { project_id: "p", what: "images" });
    expect(call.mock.calls.map((c) => c[0])).toEqual(["review_set_status", "versions_restore", "regenerate_preview"]);
  });
});

describe("помощники", () => {
  it("считают статусы, неизвестное = ожидает", () => {
    expect(reviewCountsFromEntries(["1-1", "1-2", "2-1"], { "1-1": { status: "approved" }, "1-2": { status: "zzz" } })).toEqual({
      pending: 2,
      approved: 1,
      redo: 0,
      rejected: 0,
    });
  });

  it("клавиши ревью переключают статус", () => {
    expect(nextStatusOnKey("pending", "A")).toBe("approved");
    expect(nextStatusOnKey("approved", "a")).toBe("pending");
    expect(nextStatusOnKey("pending", "q")).toBeNull();
  });

  it("подтверждение суммы и payload старта", () => {
    expect(needsCostConfirm(preview(0))).toBe(false);
    expect(needsCostConfirm(preview(5))).toBe(true);
    const base = { project_id: "p", what: "images" as const, launcher: "backend" as const };
    expect(startPayloadFromPreview(base, preview(5, true), true)).toMatchObject({
      expected_plan_digest: "d",
      confirm_cost_rub: 5,
      confirm_unknown_cost: true,
    });
    expect(startPayloadFromPreview(base, preview(5), false).confirm_cost_rub).toBeUndefined();
  });

  it("подпись версии и признак конфликта", () => {
    expect(versionLabel({ id: "current", number: 0 })).toBe("Текущая");
    expect(versionLabel({ id: "x", number: 3 })).toBe("Версия 3");
    const err = new StudioError("conflict", "m", { reason: "plan_changed" });
    expect(isConflict(err, "plan_changed")).toBe(true);
    expect(isConflict(err, "cost_changed")).toBe(false);
    expect(isConflict(new Error("x"))).toBe(false);
  });
});
