import { describe, expect, it, vi } from "vitest";

import type { StorybookClient } from "../client";
import {
  appendLogTail,
  canStart,
  createRunPoller,
  isActiveState,
  isTerminalState,
  LOG_LINES_CAP,
  nextPollDelayMs,
  otherSurfaceText,
  runCancel,
  runHistory,
  runLogTail,
  runMarkInterrupted,
  runPlan,
  runSetPause,
  runStart,
  runStatus,
  runStepsGet,
  type RunStatusResult,
} from "../runs";

const fakeClient = (impl?: () => Promise<unknown>) => {
  const call = vi.fn<(...args: unknown[]) => Promise<unknown>>(impl ?? (() => Promise.resolve({ status: "success" })));
  return { client: { call } as unknown as StorybookClient, call };
};

describe("обёртки runs", () => {
  it("вызывают нужные id с нужной нагрузкой", async () => {
    const { client, call } = fakeClient();
    await runPlan(client, { project_id: "p", mode: "full" });
    await runStart(client, { project_id: "p", mode: "from_step", start_step: "story_writer", launcher: "local" });
    await runStatus(client, { project_id: "p" });
    await runSetPause(client, { project_id: "p", paused: true });
    await runCancel(client, { project_id: "p", force: true });
    await runMarkInterrupted(client, { project_id: "p", run_id: "r1" });
    await runLogTail(client, { project_id: "p", after_offset: 10 });
    await runHistory(client, { project_id: "p", limit: 5 });
    await runStepsGet(client);
    expect(call.mock.calls.map((c) => c[0])).toEqual([
      "run_plan", "run_start", "run_status", "run_set_pause", "run_cancel",
      "run_mark_interrupted", "run_log_tail", "run_history", "run_steps_get",
    ]);
    expect(call.mock.calls[1][1]).toEqual({
      project_id: "p", mode: "from_step", start_step: "story_writer", launcher: "local",
    });
  });
});

describe("состояния", () => {
  it("активные и конечные не пересекаются", () => {
    for (const s of ["running", "pause_requested", "paused", "stopping"] as const) {
      expect(isActiveState(s)).toBe(true);
      expect(isTerminalState(s)).toBe(false);
    }
    for (const s of ["failed", "completed", "interrupted"] as const) {
      expect(isTerminalState(s)).toBe(true);
      expect(isActiveState(s)).toBe(false);
    }
    expect(isActiveState("idle")).toBe(false);
    expect(isTerminalState(undefined)).toBe(false);
  });

  it("canStart: нельзя при замке и при активном прогоне", () => {
    expect(canStart({ state: "idle", locked: false })).toBe(true);
    expect(canStart({ state: "completed", locked: false })).toBe(true);
    expect(canStart({ state: "running", locked: true })).toBe(false);
    expect(canStart({ state: "idle", locked: true })).toBe(false);
    expect(canStart(null)).toBe(false);
  });

  it("текст чужой поверхности берётся из ответа", () => {
    expect(otherSurfaceText({ other_surface: { surface: "tk", text: "Идёт в Tk" } })).toBe("Идёт в Tk");
    expect(otherSurfaceText({ other_surface: null })).toBeNull();
    expect(otherSurfaceText(undefined)).toBeNull();
  });

  it("интервал опроса зависит от состояния", () => {
    expect(nextPollDelayMs("running")).toBeLessThan(nextPollDelayMs("paused"));
    expect(nextPollDelayMs("paused")).toBeLessThan(nextPollDelayMs("idle"));
    expect(nextPollDelayMs(undefined)).toBe(nextPollDelayMs("idle"));
  });
});

describe("appendLogTail", () => {
  it("дописывает и держит предел", () => {
    expect(appendLogTail(["a"], ["b", "c"])).toEqual(["a", "b", "c"]);
    expect(appendLogTail(["a", "b", "c"], ["d"], 3)).toEqual(["b", "c", "d"]);
    const big = Array.from({ length: LOG_LINES_CAP + 5 }, (_, i) => String(i));
    const out = appendLogTail([], big);
    expect(out).toHaveLength(LOG_LINES_CAP);
    expect(out[out.length - 1]).toBe(String(LOG_LINES_CAP + 4));
  });

  it("пустое продолжение не меняет список", () => {
    const cur = ["a"];
    expect(appendLogTail(cur, [])).toBe(cur);
  });
});

describe("createRunPoller", () => {
  it("опрашивает с интервалом по состоянию и отдаёт данные", async () => {
    const answer = { status: "success", state: "running" } as unknown as RunStatusResult;
    const { client, call } = fakeClient(() => Promise.resolve(answer));
    const timers: { fn: () => void; ms: number }[] = [];
    const onStatus = vi.fn();
    const poller = createRunPoller({
      client, project_id: "p", run_id: "r1", onStatus, onError: vi.fn(),
      setTimer: (fn, ms) => {
        timers.push({ fn, ms });
        return timers.length;
      },
      clearTimer: () => undefined,
    });
    poller.start();
    for (let i = 0; i < 10; i++) await Promise.resolve();
    expect(call).toHaveBeenCalledWith("run_status", { project_id: "p", run_id: "r1" });
    expect(onStatus).toHaveBeenCalledWith(answer);
    expect(timers[timers.length - 1].ms).toBe(nextPollDelayMs("running"));
    poller.stop();
  });
});
