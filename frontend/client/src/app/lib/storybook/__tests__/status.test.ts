import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it, vi } from "vitest";

import type { StorybookClient } from "../client";
import {
  counterTarget,
  projectStatus,
  projectStatusBrief,
  statusPollIntervalMs,
  STATUS_POLL_ACTIVE_MS,
  STATUS_POLL_IDLE_MS,
  type ProjectStatus,
  type RunState,
} from "../status";

const fakeClient = () => {
  const call = vi.fn().mockResolvedValue({ status: "success" });
  return { client: { call } as unknown as StorybookClient, call };
};

describe("обёртки status", () => {
  it("зовут нужные id с нагрузкой", async () => {
    const { client, call } = fakeClient();
    await projectStatus(client, { project_id: "p", surface: "react", refresh: true });
    await projectStatusBrief(client, { project_id: "p", surface: "tk" });
    expect(call).toHaveBeenNthCalledWith(1, "project_status", { project_id: "p", surface: "react", refresh: true });
    expect(call).toHaveBeenNthCalledWith(2, "project_status_brief", { project_id: "p", surface: "tk" });
  });

  it("ошибка клиента доходит наверх без изменений", async () => {
    const call = vi.fn().mockRejectedValue(new Error("нет проекта"));
    const client = { call } as unknown as StorybookClient;
    await expect(projectStatus(client, { project_id: "x" })).rejects.toThrow("нет проекта");
  });
});

describe("statusPollIntervalMs", () => {
  const cases: Array<[RunState | undefined, number]> = [
    ["running", STATUS_POLL_ACTIVE_MS],
    ["pause_requested", STATUS_POLL_ACTIVE_MS],
    ["paused", STATUS_POLL_ACTIVE_MS],
    ["stopping", STATUS_POLL_ACTIVE_MS],
    ["idle", STATUS_POLL_IDLE_MS],
    ["interrupted", STATUS_POLL_IDLE_MS],
    ["failed", STATUS_POLL_IDLE_MS],
    ["completed", STATUS_POLL_IDLE_MS],
    [undefined, STATUS_POLL_IDLE_MS],
  ];
  it.each(cases)("%s -> %i мс", (state, ms) => {
    expect(statusPollIntervalMs(state)).toBe(ms);
  });
});

describe("counterTarget", () => {
  it("ведёт в кадры с фильтром и отдаёт копию", () => {
    expect(counterTarget("total")).toEqual({ screen: "shots", filter: null });
    expect(counterTarget("stale")).toEqual({ screen: "shots", filter: "stale" });
    expect(counterTarget("errors")).toEqual({ screen: "shots", filter: "errors" });
    expect(counterTarget("approved")).toEqual({ screen: "shots", filter: "approved" });
    const first = counterTarget("ready");
    first.filter = "x";
    expect(counterTarget("ready").filter).toBe("ready");
  });
});

describe("образец ответа из Python", () => {
  const sample = JSON.parse(
    readFileSync(join(__dirname, "fixtures", "project_status.sample.json"), "utf-8"),
  ) as ProjectStatus;

  it("содержит ключи верхнего уровня из типа", () => {
    const keys = [
      "version", "generated_at", "cache", "project", "stage", "steps", "artifacts", "shots",
      "final_video", "spend", "warnings", "active_run", "last_run", "incomplete_runs",
      "next_action", "flags", "sources", "hook_errors",
    ];
    expect(Object.keys(sample).filter((k) => k !== "status").sort()).toEqual([...keys].sort());
  });

  it("шаги и этапы имеют ожидаемую форму", () => {
    expect(sample.steps).toHaveLength(36);
    expect(Object.keys(sample.steps[0]).sort()).toEqual(
      ["artifacts", "confidence", "depends_on", "display_state", "duration_seconds", "error", "finished_at",
       "id", "note_code", "optional", "order", "outdated_hint", "source", "stage", "started_at", "state",
       "state_title", "title"].sort(),
    );
    expect(sample.stage.items.length).toBeGreaterThan(0);
    expect(Object.keys(sample.stage.items[0]).sort()).toEqual(
      ["done", "id", "state", "state_title", "title", "total"],
    );
  });

  it("счётчики, расходы и действие на месте", () => {
    expect(sample.shots.total).toBe(2);
    expect(sample.spend.currency).toBe("RUB");
    expect(sample.next_action.target).toHaveProperty("screen");
    expect(statusPollIntervalMs(sample.active_run.state)).toBe(STATUS_POLL_IDLE_MS);
  });
});
