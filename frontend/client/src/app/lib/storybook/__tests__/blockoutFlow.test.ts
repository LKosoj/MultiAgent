import { describe, expect, it } from "vitest";

import { StudioError, type StorybookClient } from "../client";
import { createBlockoutFlow } from "../blockoutFlow";

type Handler = (payload: Record<string, unknown>) => unknown;
const fake = (handlers: Record<string, Handler>) => {
  const calls: { id: string; payload: Record<string, unknown> }[] = [];
  const client = {
    call: async (id: string, payload: Record<string, unknown> = {}) => {
      calls.push({ id, payload });
      return handlers[id](payload);
    },
  } as unknown as StorybookClient;
  return { client, calls };
};
const plan = (over: Record<string, unknown> = {}) => ({
  action: "x", plan: {}, plan_digest: "d1", can_start: true, problems: [], notes: [],
  confirm: { required: false, title: "", text: "", cost_rub: null }, ...over,
});
const needConfirm = { required: true, title: "t", text: "", cost_rub: null };

describe("цепочка план -> запуск", () => {
  it("redraw_shot без окна запускается сразу", async () => {
    const { client, calls } = fake({ blockout_plan: () => plan(), blockout_run: () => ({ started: true }) });
    const step = await createBlockoutFlow({ client, projectId: "p" }).start("redraw_shot", { shot_keys: ["1-1"] });
    expect(step.kind).toBe("started");
    expect(calls.map((c) => c.id)).toEqual(["blockout_plan", "blockout_run"]);
    expect(calls[1].payload.expected_plan_digest).toBe("d1");
  });
  it("redraw_chain с окном ждёт подтверждения", async () => {
    const { client, calls } = fake({ blockout_plan: () => plan({ confirm: needConfirm }), blockout_run: () => ({}) });
    const step = await createBlockoutFlow({ client, projectId: "p" }).start("redraw_chain", { chain_id: "c1" });
    expect(step.kind).toBe("confirm");
    expect(calls).toHaveLength(1);
  });
  it("can_start=false даёт список проблем без запуска", async () => {
    const { client, calls } = fake({ blockout_plan: () => plan({ can_start: false }), blockout_run: () => ({}) });
    const step = await createBlockoutFlow({ client, projectId: "p" }).start("redraw_shot", { shot_keys: ["1-1"] });
    expect(step.kind).toBe("blocked");
    expect(calls).toHaveLength(1);
  });
  it("без digest у redraw_chain запуск закрыт", async () => {
    const { client, calls } = fake({ blockout_plan: () => plan({ plan_digest: null }), blockout_run: () => ({}) });
    const step = await createBlockoutFlow({ client, projectId: "p" }).start("redraw_chain", { chain_id: "c1" });
    expect(step.kind).toBe("blocked");
    if (step.kind === "blocked") expect(step.plan.problems.map((p) => p.code)).toContain("no_digest");
    expect(calls).toHaveLength(1);
  });
  it("конфликт при запуске строит план заново и просит подтвердить", async () => {
    let n = 0;
    const { client, calls } = fake({
      blockout_plan: () => plan({ plan_digest: `d${++n}`, confirm: needConfirm }),
      blockout_run: () => {
        throw new StudioError("conflict", "план изменился");
      },
    });
    const flow = createBlockoutFlow({ client, projectId: "p" });
    const step = await flow.start("rebuild_scene", { shot_keys: ["1-1"] });
    if (step.kind !== "confirm") throw new Error("ожидалось окно");
    const out = await flow.confirm("rebuild_scene", { shot_keys: ["1-1"] }, step.plan);
    expect(out.kind).toBe("replan");
    expect(calls.map((c) => c.id)).toEqual(["blockout_plan", "blockout_run", "blockout_plan"]);
    if (out.kind === "replan" && out.step.kind === "confirm") expect(out.step.plan.plan_digest).toBe("d2");
  });
});
