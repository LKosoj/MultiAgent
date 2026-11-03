import { describe, expect, it, vi } from "vitest";

import { createStorybookClient } from "../client";
import {
  cueAt,
  formatTimecode,
  groupByScene,
  loadAllMedia,
  mediaInventory,
  mediaItemUrl,
  mediaSubtitles,
  mediaThumbUrl,
  mediaTimeline,
  shotAt,
  type MediaItem,
  type SubtitleCue,
  type TimelineShot,
} from "../media";

const item = (over: Partial<MediaItem> = {}): MediaItem => ({
  path: "97_shots/a.png", name: "a.png", kind: "image", type: "shot", type_title: "Кадр шота",
  category: "start_frame", category_title: "Кадр начала", key: null, keys: [], scene: null, shot: null,
  page: null, phase: null, music_id: null, size: 1, modified: "", modified_ts: 0, duration_s: null,
  servable: true, ...over,
});

const page = (items: MediaItem[], over: Record<string, unknown> = {}) => ({
  status: "success", project_id: "p", total: 100, matched: 100, offset: 0, limit: 500,
  durations_pending: 0, counts: { types: {}, categories: {}, kinds: {} }, labels: { types: {}, categories: {} },
  items, ...over,
});

const tokenReply = { status: "success", t: "tok", expires_at: Date.now() / 1000 + 3600, ttl_seconds: 3600 };

describe("обёртки media", () => {
  it("уходят с префиксом storybook. и без изменения параметров", async () => {
    const transport = vi.fn().mockResolvedValue(page([]));
    const c = createStorybookClient(transport, { apiBase: "http://h" });
    const params = { project_id: "p", types: ["shot"], limit: 10, sort_by: "name" as const };
    await mediaInventory(c, params);
    await mediaTimeline(c, { project_id: "p", probe_limit: 5 });
    await mediaSubtitles(c, { project_id: "p", source: "final" });
    expect(transport.mock.calls.map((x) => x[0])).toEqual([
      "storybook.media_inventory", "storybook.media_timeline", "storybook.media_subtitles",
    ]);
    expect(transport.mock.calls[0][1]).toEqual(params);
    expect(transport.mock.calls[1][1]).toEqual({ project_id: "p", probe_limit: 5 });
  });
});

describe("loadAllMedia", () => {
  it("дочитывает, пока durations_pending > 0", async () => {
    const pending = [3, 1, 0];
    const transport = vi.fn().mockImplementation(async () => page([item()], { durations_pending: pending.shift() ?? 0, matched: 1 }));
    const c = createStorybookClient(transport, { apiBase: "http://h" });
    const result = await loadAllMedia(c, { project_id: "p" });
    expect(transport).toHaveBeenCalledTimes(3);
    expect(result.durations_pending).toBe(0);
    expect(result.items).toHaveLength(1);
  });

  it("останавливается на лимите повторов", async () => {
    const transport = vi.fn().mockResolvedValue(page([item()], { durations_pending: 2, matched: 1 }));
    const c = createStorybookClient(transport, { apiBase: "http://h" });
    const result = await loadAllMedia(c, { project_id: "p" }, { maxRepeats: 3 });
    expect(transport).toHaveBeenCalledTimes(4);
    expect(result.durations_pending).toBe(2);
  });

  it("склеивает страницы по offset/limit", async () => {
    const transport = vi.fn().mockImplementation(async (_a: string, payload: { offset: number }) =>
      page([item({ name: `n${payload.offset}` })], { matched: 3, limit: 1, offset: payload.offset }));
    const c = createStorybookClient(transport, { apiBase: "http://h" });
    const result = await loadAllMedia(c, { project_id: "p", limit: 1 });
    expect(result.items.map((x) => x.name)).toEqual(["n0", "n1", "n2"]);
    expect(transport.mock.calls.map((x) => x[1].offset)).toEqual([0, 1, 2]);
  });

  it("прерывается по AbortSignal", async () => {
    const controller = new AbortController();
    const transport = vi.fn().mockImplementation(async () => {
      controller.abort();
      return page([item()], { durations_pending: 5, matched: 1 });
    });
    const c = createStorybookClient(transport, { apiBase: "http://h" });
    const result = await loadAllMedia(c, { project_id: "p" }, { signal: controller.signal });
    expect(transport).toHaveBeenCalledTimes(1);
    expect(result.durations_pending).toBe(5);
  });
});

describe("ссылки", () => {
  it("servable:false даёт null, кириллица и пробелы кодируются", async () => {
    const c = createStorybookClient(vi.fn().mockResolvedValue(tokenReply), { apiBase: "http://h" });
    await c.ensureMediaToken("p");
    expect(mediaItemUrl(c, "p", item({ servable: false, path: "x.avi" }))).toBeNull();
    expect(mediaThumbUrl(c, "p", item({ servable: false }))).toBeNull();
    const url = mediaItemUrl(c, "p", item({ path: "97_shots/кадр 1.png" }));
    expect(url).toContain("path=97_shots%2F%D0%BA%D0%B0%D0%B4%D1%80%201.png");
    expect(mediaThumbUrl(c, "p", item(), 128)).toContain("size=128");
  });

  it("до токена ссылки нет", () => {
    const c = createStorybookClient(vi.fn(), { apiBase: "http://h" });
    expect(mediaItemUrl(c, "p", item())).toBeNull();
  });
});

describe("помощники", () => {
  it("formatTimecode", () => {
    expect(formatTimecode(0)).toBe("0:00");
    expect(formatTimecode(59.9)).toBe("0:59");
    expect(formatTimecode(61)).toBe("1:01");
    expect(formatTimecode(161)).toBe("2:41");
    expect(formatTimecode(3661)).toBe("1:01:01");
    expect(formatTimecode(-3)).toBe("0:00");
  });

  const shot = (key: string, start: number, end: number, over: Partial<TimelineShot> = {}): TimelineShot => ({
    key, scene: 1, shot: 1, index: 1, start_s: start, end_s: end, duration_s: end - start,
    planned_duration_s: null, actual_duration_s: null, in_cut: true, approximate: false, covered_by: null,
    unit_span: false, unit: null, video: null, start_image: null, end_image: null, thumb_path: null,
    warnings: [], ...over,
  });

  it("shotAt: границы, промежуток и блок", () => {
    const timeline = {
      shots: [
        shot("1-1", 0, 5),
        shot("1-2", 6, 20, { unit_span: true }),
        shot("1-3", 13, 20, { covered_by: "1-2", approximate: true }),
        shot("1-4", 25, 30, { in_cut: false, start_s: null, end_s: null }),
      ],
    };
    expect(shotAt(timeline, 0)?.key).toBe("1-1");
    expect(shotAt(timeline, 4.99)?.key).toBe("1-1");
    expect(shotAt(timeline, 5)).toBeNull(); // промежуток между шотами
    expect(shotAt(timeline, 6)?.key).toBe("1-2");
    expect(shotAt(timeline, 14)?.key).toBe("1-3"); // точнее, чем весь блок
    expect(shotAt(timeline, 12.9)?.key).toBe("1-2");
    expect(shotAt(timeline, 20)).toBeNull();
    expect(shotAt({ shots: [] }, 1)).toBeNull();
  });

  it("cueAt: до первой и после последней реплики", () => {
    const cue = (i: number, s: number, e: number): SubtitleCue => ({
      index: i, source_index: i, start_s: s, end_s: e, start_tc: "", end_tc: "", text: "", shot_key: null,
    });
    const cues = [cue(1, 1, 3), cue(2, 4, 6)];
    expect(cueAt(cues, 0.5)).toBeNull();
    expect(cueAt(cues, 1)?.index).toBe(1);
    expect(cueAt(cues, 3)).toBeNull();
    expect(cueAt(cues, 5.9)?.index).toBe(2);
    expect(cueAt(cues, 6)).toBeNull();
    expect(cueAt([], 1)).toBeNull();
  });

  it("groupByScene: сцены по возрастанию, без сцены - в конце", () => {
    const groups = groupByScene([item({ scene: 2 }), item({ scene: null }), item({ scene: 1 }), item({ scene: 2 })]);
    expect(groups.map((g) => g.scene)).toEqual([1, 2, null]);
    expect(groups[1].items).toHaveLength(2);
  });
});
