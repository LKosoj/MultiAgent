import { describe, expect, it, vi } from "vitest";

import type { StorybookClient } from "../client";
import {
  actionBlockerText,
  actionEnabled,
  blockoutDetail,
  blockoutLeaveAsIs,
  blockoutPlan,
  blockoutRun,
  blockoutSetDuration,
  blockoutState,
  compareImages,
  groupByScene,
  isEmptyBlockout,
  needsConfirm,
  shotStateTitle,
  type BlockoutActionKey,
  type BlockoutShotRow,
  type BlockoutStateResult,
} from "../blockout";

const fake = () => {
  const call = vi.fn().mockResolvedValue({ status: "success" });
  const thumbUrl = vi.fn((p: string, rel: string, size?: number) => `http://x/${p}/thumb?path=${encodeURIComponent(rel)}&size=${size}`);
  return { client: { call, thumbUrl } as unknown as StorybookClient, call, thumbUrl };
};

const shot = (key: string, allowed: BlockoutActionKey[] = [], over: Partial<BlockoutShotRow> = {}): BlockoutShotRow => {
  const [scene, n] = key.split("-").map(Number);
  return {
    key, scene_number: scene, shot_number: n, chain_id: null, state: "rendered", state_title: "готова",
    duration_s: 5, scene_spec_duration_s: 5, duration_mismatch: false, p10: false, allowed, ...over,
  };
};

const open = { enabled: true, blockers: [] };
const state = (over: Partial<BlockoutStateResult> = {}): BlockoutStateResult =>
  ({
    scenes: [],
    shots: [],
    empty_reason: null,
    actions: {
      redraw_shot: open, redraw_chain: open, rebuild_scene: open,
      build_preview: open, regenerate_shots: open, set_duration: open, leave_as_is: open,
    },
    ...over,
  }) as unknown as BlockoutStateResult;

describe("обёртки", () => {
  it("шлют нужные id и поля", async () => {
    const { client, call } = fake();
    await blockoutState(client, { project_id: "p" });
    await blockoutDetail(client, { project_id: "p", shot_key: "1-1" });
    await blockoutPlan(client, { project_id: "p", action: "redraw_shot", shot_keys: ["1-1"] });
    await blockoutRun(client, { project_id: "p", action: "rebuild_scene", launcher: "local", expected_plan_digest: "d" });
    await blockoutSetDuration(client, { project_id: "p", shot_key: "1-1", seconds: 10, base_digest: "b" });
    await blockoutLeaveAsIs(client, { project_id: "p", shot_keys: ["1-1"] });
    expect(call.mock.calls).toEqual([
      ["blockout_state", { project_id: "p" }],
      ["blockout_detail", { project_id: "p", shot_key: "1-1" }],
      ["blockout_plan", { project_id: "p", action: "redraw_shot", shot_keys: ["1-1"] }],
      ["blockout_run", { project_id: "p", action: "rebuild_scene", launcher: "local", expected_plan_digest: "d" }],
      ["blockout_set_duration", { project_id: "p", shot_key: "1-1", seconds: 10, base_digest: "b" }],
      ["blockout_leave_as_is", { project_id: "p", shot_keys: ["1-1"] }],
    ]);
  });
});

describe("actionEnabled", () => {
  const s = state({
    shots: [
      shot("1-1", ["redraw_shot", "redraw_chain", "regenerate_shots", "set_duration"]),
      shot("1-2", ["redraw_shot", "regenerate_shots", "leave_as_is"]),
      shot("2-1", []),
    ],
  });

  it.each([
    ["redraw_shot", [], false],
    ["redraw_shot", ["1-1"], true],
    ["redraw_shot", ["1-1", "1-2"], false],
    ["redraw_shot", ["2-1"], false],
    ["redraw_chain", ["1-1"], true],
    ["redraw_chain", ["1-2"], false],
    ["set_duration", ["1-1"], true],
    ["set_duration", ["1-1", "1-2"], false],
    ["regenerate_shots", [], false],
    ["regenerate_shots", ["1-1", "1-2"], true],
    ["regenerate_shots", ["1-1", "2-1"], false],
    ["leave_as_is", [], false],
    ["leave_as_is", ["1-1", "1-2"], true],
    ["leave_as_is", ["1-1"], false],
    ["rebuild_scene", [], true],
    ["build_preview", ["2-1"], true],
  ] as [BlockoutActionKey, string[], boolean][])("%s %j -> %s", (action, keys, want) => {
    expect(actionEnabled(s, action, keys)).toBe(want);
  });

  it("глобальный блокер выключает действие и даёт текст", () => {
    const blocked = state({
      ...s,
      actions: { ...s.actions, rebuild_scene: { enabled: false, blockers: [{ code: "blender_unavailable", message: "нет Blender" }] } },
    });
    expect(actionEnabled(blocked, "rebuild_scene", [])).toBe(false);
    expect(actionBlockerText(blocked, "rebuild_scene", [])).toBe("нет Blender");
  });

  it("текст про выбор шотов", () => {
    expect(actionBlockerText(s, "redraw_shot", [])).toBe("Выберите один шот");
    expect(actionBlockerText(s, "regenerate_shots", [])).toBe("Выберите шоты");
    expect(actionBlockerText(s, "redraw_shot", ["1-1"])).toBeNull();
    expect(actionBlockerText(s, "rebuild_scene", [])).toBeNull();
  });
});

describe("groupByScene", () => {
  it("собирает сцена -> цепочка -> шоты и не теряет шоты без цепочки", () => {
    const shots = [shot("1-1"), shot("1-2"), shot("2-1")];
    const groups = groupByScene({
      shots,
      scenes: [
        { scene_number: 1, chains: [{ chain_id: "c1", shot_keys: ["1-1", "1-2", "9-9"], blend_path: "a", blend_exists: true }], unchained_shot_keys: [] },
        { scene_number: 2, chains: [], unchained_shot_keys: ["2-1"] },
      ],
    });
    expect(groups.map((g) => g.scene_number)).toEqual([1, 2]);
    expect(groups[0].chains[0].shots.map((x) => x.key)).toEqual(["1-1", "1-2"]);
    expect(groups[1].chains).toEqual([]);
    expect(groups[1].unchained.map((x) => x.key)).toEqual(["2-1"]);
  });
});

describe("compareImages", () => {
  it("даёт адреса только для отдаваемых файлов", () => {
    const { client, thumbUrl } = fake();
    const ref = (path: string, servable = true) => ({ path, servable, mtime: 1 });
    const r = compareImages(client, "p", {
      images: { blockout_start: ref("a.png"), image_start: ref("b.png", false), image_end: null, blockout_end: ref("d.png") },
    }, 512);
    expect(r.blockout_start).toContain("a.png");
    expect(r.blockout_end).toContain("d.png");
    expect(r.image_start).toBeNull();
    expect(r.image_end).toBeNull();
    expect(thumbUrl).toHaveBeenCalledTimes(2);
    expect(thumbUrl).toHaveBeenCalledWith("p", "a.png", 512);
  });
});

describe("мелочи", () => {
  it("needsConfirm берёт флаг из плана", () => {
    const confirm = { title: null, text: null, cost_rub: null };
    expect(needsConfirm({ confirm: { required: true, ...confirm } })).toBe(true);
    expect(needsConfirm({ confirm: { required: false, ...confirm } })).toBe(false);
  });

  it("пустая болванка и названия состояний", () => {
    expect(isEmptyBlockout({ empty_reason: "not_built" })).toBe(true);
    expect(isEmptyBlockout({ empty_reason: null })).toBe(false);
    expect(shotStateTitle("stale")).toBe("устарела");
    expect(shotStateTitle("junction_error")).toBe("ошибка");
  });
});
