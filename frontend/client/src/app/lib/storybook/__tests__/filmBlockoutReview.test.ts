// Правки по ревью У9: сбои видео, повторы загрузки, конфликты, права, порядок выбора.
import { describe, expect, it } from "vitest";

import { StudioError, type StorybookClient } from "../client";
import { createBlockoutFlow } from "../blockoutFlow";
import { applySelection, detailKey, durationFailure, durationReasonText, pruneSelection } from "../blockoutView";
import { BLOCKOUT_TEXTS } from "../blockoutTexts";
import { createFilmLoader } from "../filmLoader";
import { FILM_TEXTS } from "../filmTexts";
import { musicDialogView, newestFirst, type AreaVersion } from "../filmRegen";
import { buildLayout, forbiddenAfter } from "../filmView";
import type { TimelineResult } from "../media";
import { isTypingElement, nextOnError } from "../playerModel";
import type { RegeneratePreviewResult } from "../review";

const fake = (handlers: Record<string, (p: Record<string, unknown>) => unknown>) => {
  const calls: string[] = [];
  const client = {
    call: async (id: string, payload: Record<string, unknown> = {}) => {
      calls.push(id);
      return handlers[id](payload);
    },
  } as unknown as StorybookClient;
  return { client, calls };
};

describe("плеер: ошибка загрузки", () => {
  it("первая ошибка - повтор, вторая - показать ошибку", () => {
    expect(nextOnError(0)).toBe("retry");
    expect(nextOnError(1)).toBe("fail");
    expect(nextOnError(5)).toBe("fail");
  });
  it("горячие клавиши не перехватываются в полях и кнопках, но работают на корне", () => {
    expect(isTypingElement({ tagName: "INPUT" })).toBe(true);
    expect(isTypingElement({ tagName: "BUTTON" })).toBe(true);
    expect(isTypingElement({ tagName: "DIV", isContentEditable: true })).toBe(true);
    expect(isTypingElement({ tagName: "DIV" })).toBe(false);
    expect(isTypingElement(null)).toBe(false);
  });
});

describe("загрузка фильма: сбой повтора", () => {
  it("последние удачные данные остаются, ошибка уходит в предупреждение", async () => {
    let n = 0;
    const { client } = fake({
      media_timeline: () => {
        n += 1;
        if (n === 2) throw new StudioError("unavailable", "нет связи");
        return { durations_pending: 3 };
      },
      media_subtitles: () => ({ cues: [] }),
    });
    const seen: boolean[] = [];
    const data = await createFilmLoader({ client, projectId: "p", sleep: async () => {}, onUpdate: (d) => seen.push(!!d.refreshFailed) }).load();
    expect(data.refreshFailed).toBe(true);
    expect(data.timeline.durations_pending).toBe(3);
    expect(seen).toEqual([false, true]);
  });
  it("сбой первого запроса остаётся ошибкой экрана", async () => {
    const { client } = fake({
      media_timeline: () => {
        throw new StudioError("unavailable", "нет связи");
      },
      media_subtitles: () => ({ cues: [] }),
    });
    await expect(createFilmLoader({ client, projectId: "p" }).load()).rejects.toBeInstanceOf(StudioError);
  });
});

describe("права", () => {
  it("успешная загрузка сбрасывает флаг, отказ ставит", () => {
    expect(forbiddenAfter(false, "forbidden")).toBe(true);
    expect(forbiddenAfter(true, "load_ok")).toBe(false);
  });
});

const plan = (over: Record<string, unknown> = {}) => ({
  action: "redraw_chain", plan: {}, plan_digest: "d1", can_start: true, problems: [], notes: [],
  confirm: { required: true, title: "t", text: "", cost_rub: null }, ...over,
});

describe("конфликт запуска болванки", () => {
  const run = async (reason: string) => {
    const { client, calls } = fake({
      blockout_plan: () => plan(),
      blockout_run: () => {
        throw new StudioError("conflict", "Нет Blender", { reason });
      },
    });
    const flow = createBlockoutFlow({ client, projectId: "p" });
    const step = await flow.start("redraw_chain", { chain_id: "c1" });
    if (step.kind !== "confirm") throw new Error("нужно окно");
    return { out: await flow.confirm("redraw_chain", { chain_id: "c1" }, step.plan), calls };
  };
  it("plan_blocked: текст блокера, без нового плана", async () => {
    const { out, calls } = await run("plan_blocked");
    expect(out).toEqual({ kind: "blocked", text: "Нет Blender" });
    expect(calls).toEqual(["blockout_plan", "blockout_run"]);
  });
  it("plan_changed: новый план", async () => {
    const { out, calls } = await run("plan_changed");
    expect(out.kind).toBe("replan");
    expect(calls).toEqual(["blockout_plan", "blockout_run", "blockout_plan"]);
  });
});

describe("длительность шота", () => {
  it("конфликт значит устаревший отпечаток: перечитать и написать понятно", () => {
    const f = durationFailure(new StudioError("conflict", "x"), () => "другое");
    expect(f).toEqual({ text: BLOCKOUT_TEXTS.duration_stale, reload: true });
    expect(durationFailure(new Error("e"), () => "другое")).toEqual({ text: "другое", reload: false });
  });
  it("коды причин переводятся, неизвестный даёт общий текст", () => {
    expect(durationReasonText("screenplay_running")).toContain("Раскадровка");
    expect(durationReasonText("future_code")).toBe(BLOCKOUT_TEXTS.duration_locked);
    expect(durationReasonText(null)).toBe(BLOCKOUT_TEXTS.duration_locked);
  });
});

const ordered4 = ["1-1", "1-2", "1-3", "2-1"];
describe("выбор Shift", () => {
  it("ключи идут в порядке дерева при любом направлении", () => {
    const ordered = ["1-1", "1-2", "1-3", "2-1"];
    const sel = applySelection({ keys: ["2-1"], anchor: "2-1", focus: "2-1" }, ordered, "1-2", { shift: true });
    expect(sel.keys).toEqual(["1-2", "1-3", "2-1"]);
    // сравнение показывает шот, по которому кликнули
    expect(detailKey(sel)).toBe("1-2");
  });
  it("снятие через Ctrl: focus - последний оставшийся ключ или null", () => {
    const one = applySelection({ keys: ["1-1", "1-2"], anchor: "1-2", focus: "1-2" }, ordered4, "1-2", { ctrl: true });
    expect(one.focus).toBe("1-1");
    expect(applySelection(one, ordered4, "1-1", { ctrl: true }).focus).toBeNull();
  });
  it("pruneSelection сбрасывает пропавший focus", () => {
    const out = pruneSelection({ keys: ["1-1", "9-9"], anchor: "9-9", focus: "9-9" }, { shots: [{ key: "1-1" }] } as never);
    expect(out.focus).toBe("1-1");
  });
  it("plan_blocked с пустым сообщением даёт локальный текст", async () => {
    const { client } = fake({
      blockout_plan: () => plan(),
      blockout_run: () => {
        throw new StudioError("conflict", "  ", { reason: "plan_blocked" });
      },
    });
    const flow = createBlockoutFlow({ client, projectId: "p" });
    const step = await flow.start("redraw_chain", { chain_id: "c1" });
    if (step.kind !== "confirm") throw new Error("нужно окно");
    expect(await flow.confirm("redraw_chain", { chain_id: "c1" }, step.plan)).toEqual({ kind: "blocked", text: BLOCKOUT_TEXTS.plan_blocked });
  });
});

describe("музыка и версии", () => {
  const preview = (cost: number, unknown: boolean) =>
    ({ can_start: true, problems: [], confirm_required: { cost_rub: cost, unknown_cost: unknown } }) as unknown as RegeneratePreviewResult;
  it("частичная цена - «не менее»", () => {
    expect(musicDialogView(preview(12, true)).body).toContain("не менее");
    expect(musicDialogView(preview(12, false)).body).not.toContain("не менее");
    expect(FILM_TEXTS.music_body_partial).toContain("не менее");
  });
  it("версии: свежие сверху", () => {
    const v = (number: number) => ({ id: `v${number}`, number, files: [] }) as unknown as AreaVersion;
    expect(newestFirst([v(1), v(3), v(2)]).map((x) => x.number)).toEqual([3, 2, 1]);
  });
});

describe("предупреждения шотов", () => {
  it("код без сообщения сервера не показывается", () => {
    const tl = {
      source: "shots", duration_s: 10, warnings: [], tracks: { narration: { clips: [] }, music: { clips: [] }, sfx: { clips: [] } },
      shots: [{ key: "1-1", scene: 1, shot: 1, in_cut: true, start_s: 0, end_s: 5, approximate: false, unit_span: false, covered_by: null, warnings: ["future_code"] }],
    } as unknown as TimelineResult;
    const b = buildLayout(tl, []).blocks.find((x) => x.track === "shots");
    expect(b?.tooltip).not.toContain("future_code");
  });
});
