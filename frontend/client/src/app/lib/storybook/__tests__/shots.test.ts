import { describe, expect, it, vi } from "vitest";

import { StudioError, type StorybookClient } from "../client";
import {
  canEditShot,
  conflictDraft,
  dirtyChanges,
  groupByScene,
  isConflict,
  shotGet,
  shotMediaUrl,
  shotThumbUrl,
  shotUpdate,
  shotsAdoptBaseline,
  shotsKeys,
  shotsList,
  type ShotCard,
  type ShotRow,
} from "../shots";

const fake = () => {
  const call = vi.fn().mockResolvedValue({ status: "success" });
  const thumbUrl = vi.fn((p: string, rel: string, size?: number) => `http://x/${p}/thumb?path=${encodeURIComponent(rel)}&size=${size}&t=tok`);
  const mediaUrl = vi.fn((p: string, rel: string) => `http://x/${p}/media?path=${encodeURIComponent(rel)}`);
  return { client: { call, thumbUrl, mediaUrl } as unknown as StorybookClient, call };
};

const row = (shot_key: string, over: Partial<ShotRow> = {}): ShotRow => {
  const [scene, shot] = shot_key.split("-").map(Number);
  return { shot_key, scene, shot, thumb_path: null, thumb_rev: null, ...over } as ShotRow;
};

const card = (over: Partial<ShotCard> = {}): ShotCard =>
  ({
    ...row("1-1"),
    start: { prompt: "a", negative_prompt: "n" },
    end: { prompt: "b", negative_prompt: "" },
    video: { prompt: "v", duration_s: 5 },
    editable: { fields: ["start_prompt", "end_prompt", "video_prompt", "start_negative_prompt", "end_negative_prompt", "duration_s"], locked: false },
    ...over,
  }) as unknown as ShotCard;

describe("обёртки shots", () => {
  it("зовут нужные id с нагрузкой", async () => {
    const { client, call } = fake();
    await shotsList(client, { project_id: "p", state: "stale", limit: 10 });
    await shotsKeys(client, { project_id: "p", stale_only: true });
    await shotGet(client, { project_id: "p", shot_key: "2-3" });
    await shotUpdate(client, { project_id: "p", shot_key: "2-3", changes: { duration_s: 6 }, base_digest: "j1:x" });
    await shotsAdoptBaseline(client, { project_id: "p", parts: ["start"] });
    expect(call.mock.calls.map((x) => x[0])).toEqual(["shots_list", "shots_keys", "shot_get", "shot_update", "shots_adopt_baseline"]);
    expect(call.mock.calls[0][1]).toEqual({ project_id: "p", state: "stale", limit: 10 });
    expect(call.mock.calls[3][1]).toEqual({ project_id: "p", shot_key: "2-3", changes: { duration_s: 6 }, base_digest: "j1:x" });
  });
});

describe("ссылки", () => {
  it("shotThumbUrl добавляет v и кодирует символы", () => {
    const { client } = fake();
    const url = shotThumbUrl(client, "p", { thumb_path: "97_shots/сцена #1/a b.png", thumb_rev: "t1:5:ab#c" }, 256);
    expect(url).toContain("size=256");
    expect(url).toContain(encodeURIComponent("97_shots/сцена #1/a b.png"));
    expect(url!.endsWith("&v=" + encodeURIComponent("t1:5:ab#c"))).toBe(true);
  });
  it("null без пути и без токена доступа", () => {
    const { client } = fake();
    expect(shotThumbUrl(client, "p", { thumb_path: null, thumb_rev: null })).toBeNull();
    expect(shotMediaUrl(client, "p", null)).toBeNull();
    (client.thumbUrl as unknown as ReturnType<typeof vi.fn>).mockReturnValue(null);
    expect(shotThumbUrl(client, "p", { thumb_path: "a.png", thumb_rev: "t" })).toBeNull();
  });
});

describe("конфликт", () => {
  it("isConflict и conflictDraft достают current", () => {
    const e = new StudioError("conflict", "x", { current_digest: "j1:new", current: { start_prompt: "чужое" } });
    expect(isConflict(e)).toBe(true);
    expect(conflictDraft(e)).toEqual({ current_digest: "j1:new", current: { start_prompt: "чужое" } });
    expect(isConflict(new StudioError("not_found", "x"))).toBe(false);
    expect(conflictDraft(new Error("x"))).toBeNull();
  });
});

describe("помощники формы", () => {
  it("dirtyChanges: ничего не менялось -> {}", () => {
    expect(dirtyChanges(card(), { start_prompt: "a", duration_s: 5 })).toEqual({});
    expect(dirtyChanges(card(), {})).toEqual({});
  });
  it("dirtyChanges берёт только изменённые и разрешённые поля", () => {
    expect(dirtyChanges(card(), { start_prompt: "новый", end_prompt: "b", duration_s: 7 })).toEqual({ start_prompt: "новый", duration_s: 7 });
    const noEnd = card({ editable: { fields: ["start_prompt"], locked: false, locked_reason: null, hint: "" } });
    expect(dirtyChanges(noEnd, { end_prompt: "x" })).toEqual({});
  });
  it("canEditShot", () => {
    expect(canEditShot(card())).toBe(true);
    expect(canEditShot(card({ editable: { fields: [], locked: true, locked_reason: "r", hint: "" } }))).toBe(false);
  });
  it("groupByScene: числовой порядок", () => {
    const groups = groupByScene([row("10-1"), row("9-6"), row("9-2")]);
    expect(groups.map((g) => g.scene)).toEqual([9, 10]);
    expect(groups[0].rows.map((r) => r.shot_key)).toEqual(["9-2", "9-6"]);
  });
});
