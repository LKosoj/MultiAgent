import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { StorybookClient } from "../client";
import { StudioError } from "../client";
import { imageEditPreview, imageEditStart, previewArgs } from "../imageEdit";
import { waitForProjectJob } from "../projects";

const fakeClient = (handler: (id: string, payload: Record<string, unknown>) => unknown) => {
  const calls: { id: string; payload: Record<string, unknown> }[] = [];
  const client = {
    call: async (id: string, payload: Record<string, unknown> = {}) => {
      calls.push({ id, payload });
      const out = handler(id, payload);
      if (out instanceof Error) throw out;
      return { status: "success", ...(out as object) };
    },
  } as unknown as StorybookClient;
  return { client, calls };
};

describe("обёртки возможностей", () => {
  it("предпросмотр и запуск зовут image_edit_preview и image_edit_start с теми же полями", async () => {
    const { client, calls } = fakeClient((id) => (id === "image_edit_start" ? { job_id: "j1" } : { plan_digest: "d" }));
    await imageEditPreview(client, previewArgs("p", { kind: "shot", shotKey: "1-1", part: "end", path: "x", name: "x" }, ["r.png"]));
    await imageEditStart(client, { project_id: "p", prompt: "abcde" });
    expect(calls[0]).toEqual({ id: "image_edit_preview", payload: { project_id: "p", shot_key: "1-1", part: "end", reference_paths: ["r.png"] } });
    expect(calls[1].id).toBe("image_edit_start");
  });
  it("иллюстрация передаётся путём", () => {
    expect(previewArgs("p", { kind: "illustration", path: "50_images/a.png", name: "a.png" }, [])).toEqual({ project_id: "p", path: "50_images/a.png", reference_paths: [] });
  });
});

describe("ожидание задания правки", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("опрос раз в секунду доходит до done и отдаёт результат", async () => {
    let n = 0;
    const { client, calls } = fakeClient(() => {
      n += 1;
      return n < 3
        ? { state: "running", progress: { done: n, total: 3, message: "" } }
        : { state: "done", progress: { done: 3, total: 3, message: "" }, result: { path: "50_images/a.png", modified_ts: 12.5 } };
    });
    const promise = waitForProjectJob(client, "j1", { intervalMs: 1000 });
    await vi.advanceTimersByTimeAsync(2000);
    await expect(promise).resolves.toMatchObject({ path: "50_images/a.png", modified_ts: 12.5 });
    expect(calls.map((c) => c.id)).toEqual(["projects_job_status", "projects_job_status", "projects_job_status"]);
  });
  it("state error становится ошибкой с кодом", async () => {
    const { client } = fakeClient(() => ({ state: "error", progress: {}, error: { code: "unavailable", message: "нет" } }));
    const promise = waitForProjectJob(client, "j1", { intervalMs: 1000 });
    const caught = promise.catch((e: unknown) => e);
    await vi.advanceTimersByTimeAsync(0);
    const err = await caught;
    expect(err).toBeInstanceOf(StudioError);
    expect((err as StudioError).code).toBe("unavailable");
  });
});
