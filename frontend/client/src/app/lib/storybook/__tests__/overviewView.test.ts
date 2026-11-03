import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import {
  briefFromStatus,
  counterTiles,
  finalVideoView,
  isEmptyProject,
  lastRunView,
  nextActionView,
  runTone,
  stageStrip,
  stepGroups,
  stepRows,
} from "../overviewView";
import type { ProjectStatus } from "../status";

const sample = (): ProjectStatus =>
  JSON.parse(readFileSync(join(__dirname, "fixtures", "project_status.sample.json"), "utf-8")) as ProjectStatus;

describe("stageStrip", () => {
  it("8 этапов из данных и ячейка «Готово», светится при current=done", () => {
    const strip = stageStrip(sample());
    expect(strip).toHaveLength(9);
    const last = strip[strip.length - 1];
    expect(last).toMatchObject({ id: "done", title: "Готово", state: "done", current: true, synthetic: true });
    expect(strip[0]).toMatchObject({ id: "story", title: "Сюжет", progress: "8/8", current: false });
  });

  it("«Готово» не горит на другом этапе, текущий выделен", () => {
    const s = sample();
    s.stage.current = "video";
    const strip = stageStrip(s);
    expect(strip.at(-1)).toMatchObject({ state: "not_started", current: false });
    expect(strip.filter((c) => c.current).map((c) => c.id)).toEqual(["video"]);
  });
});

describe("counterTiles", () => {
  it("null это тире, ноль остаётся нулём", () => {
    const tiles = counterTiles(sample());
    const by = Object.fromEntries(tiles.map((t) => [t.key, t]));
    expect(tiles.map((t) => t.key)).toEqual(["total", "ready", "stale", "errors", "approved", "spend", "warnings"]);
    expect(by.total.value).toBe("2");
    expect(by.errors.value).toBe("0");
    expect(by.errors.known).toBe(true);
    expect(by.stale.value).toBe("—");
    expect(by.stale.known).toBe(false);
    expect(by.stale.notes).toContain("нет данных об устаревании");
    expect(by.warnings.value).toBe("—");
    expect(by.warnings.toWarnings).toBe(true);
    expect(by.spend.target).toBeNull();
    expect(by.stale.target).toEqual({ screen: "shots", filter: "stale" });
  });

  it("partial, бюджет и допущения в тратах", () => {
    const s = sample();
    s.shots.partial = true;
    s.spend.budget_rub = 1500;
    s.spend.assumed_rub = 2;
    s.spend.other_currencies = { USD: 1 };
    s.warnings.count = 5;
    const by = Object.fromEntries(counterTiles(s).map((t) => [t.key, t]));
    expect(by.total.partial).toBe(true);
    expect(by.spend.sub).toContain("1 500");
    expect(by.spend.notes).toHaveLength(2);
    expect(by.warnings.value).toBe("5");
  });
});

describe("nextActionView", () => {
  it("берёт текст и цель из данных", () => {
    const v = nextActionView(sample());
    expect(v).toMatchObject({ buttonLabel: "Перейти", severity: "success", code: "done" });
    expect(v.target.screen).toBe("movie");
  });
});

describe("finalVideoView", () => {
  it("есть видео: мета в формате «мм:сс · 720p · размер»", () => {
    const v = finalVideoView(sample());
    expect(v.exists).toBe(true);
    expect(v.meta).toBe("00:12 · 720p · 5 Б");
    expect(v.olderThanShots).toBe(false);
    expect(v.review).toBeNull();
  });
  it("устарело и ошибки проверки", () => {
    const s = sample();
    s.final_video.older_than_shots = true;
    s.final_video.review_passed = false;
    s.final_video.review_errors = 2;
    s.final_video.review_warnings = 1;
    const v = finalVideoView(s);
    expect(v.olderThanShots).toBe(true);
    expect(v.review).toBe("Проверка фильма: ошибок 2, замечаний 1");
  });
  it("нет видео", () => {
    const s = sample();
    s.final_video.exists = false;
    s.final_video.older_than_shots = true;
    expect(finalVideoView(s)).toMatchObject({ exists: false, olderThanShots: false, review: null });
  });
});

describe("шаги", () => {
  it("порядок по order, низкая уверенность и заметки", () => {
    const s = sample();
    s.steps.reverse();
    const rows = stepRows(s);
    expect(rows.map((r) => r.order)).toEqual([...rows.map((r) => r.order)].sort((a, b) => a - b));
    const first = rows[0];
    expect(first.lowConfidence).toBe(true);
    expect(first.note).toBe("Обойдён другими шагами");
  });
  it("длинная ошибка обрезается", () => {
    const s = sample();
    s.steps[0].error = "x".repeat(500);
    expect(stepRows(s).find((r) => r.id === s.steps[0].id)!.error!.length).toBe(301);
  });
  it("группы по этапам с русскими названиями", () => {
    const groups = stepGroups(sample());
    expect(groups[0]).toMatchObject({ stageId: "story", title: "Сюжет" });
    expect(groups.reduce((n, g) => n + g.rows.length, 0)).toBe(sample().steps.length);
  });
});

describe("прочее", () => {
  it("isEmptyProject", () => {
    const s = sample();
    expect(isEmptyProject(s)).toBe(false);
    s.next_action.code = "empty_project";
    expect(isEmptyProject(s)).toBe(true);
    const t = sample();
    t.steps.forEach((x) => (x.state = "not_run"));
    t.last_run = null;
    expect(isEmptyProject(t)).toBe(true);
  });

  it("runTone", () => {
    expect(runTone("idle")).toBe("muted");
    expect(runTone("running")).toBe("running");
    expect(runTone("interrupted")).toBe("warn");
    expect(runTone("failed")).toBe("danger");
    expect(runTone("completed")).toBe("ok");
    expect(runTone(undefined)).toBe("muted");
  });

  it("lastRunView", () => {
    const s = sample();
    expect(lastRunView(s)).toBeNull();
    s.last_run = {
      workflow_id: "w", status: "failed", mode: "resume", started_at: "2026-10-27T10:00:00",
      finished_at: null, duration_seconds: 125, steps_done: 10, steps_failed: 1, error: "сбой",
    };
    const v = lastRunView(s)!;
    expect(v.rows.find((r) => r.label === "Итог")!.value).toBe("Сбой");
    expect(v.rows.find((r) => r.label === "Режим")!.value).toBe("Продолжение");
    expect(v.rows.find((r) => r.label === "Длительность")!.value).toBe("2 мин");
    expect(v.error).toBe("сбой");
  });

  it("briefFromStatus совпадает по форме с кратким статусом", () => {
    const s = sample();
    s.active_run = { ...s.active_run, state: "running", current_step: s.steps[2].id, step_index: 3, percent: 8, text: "Идёт" };
    const b = briefFromStatus(s);
    expect(b.project.id).toBe("demo");
    expect(b.run).toMatchObject({ state: "running", text: "Идёт", percent: 8 });
    expect(b.run.step).toEqual({ id: s.steps[2].id, title: s.steps[2].title, index: 3, total: 36 });
    expect(b.final_video_exists).toBe(true);
    expect(b.spend.total).toBe(4);
    expect(b.failed_step).toBeNull();
    s.steps[4].state = "failed";
    expect(briefFromStatus(s).failed_step).toEqual({ id: s.steps[4].id, title: s.steps[4].title });
  });
});
