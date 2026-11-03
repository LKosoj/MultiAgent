import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { StudioError, type StorybookClient } from "../client";
import { createEditJobs, editFailureText } from "../imageEditJob";
import { IMAGE_TEXTS } from "../imageToolsTexts";
import { waitForProjectJob } from "../projects";

const okJob = (state: string, extra: Record<string, unknown> = {}) => ({ status: "success", job_id: "j", kind: "image_edit", state, progress: {}, ...extra });
const net = () => new StudioError("network", "нет связи");

// ответы по очереди: объект - успех, Error - ошибка вызова
const fakeClient = (...replies: unknown[]) => {
  const call = vi.fn();
  for (const r of replies) {
    if (r instanceof Error) call.mockRejectedValueOnce(r);
    else call.mockResolvedValueOnce(r);
  }
  return { client: { call } as unknown as StorybookClient, call };
};

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe("терпимость опроса к ошибкам", () => {
  it("две ошибки подряд терпим, успешный ответ сбрасывает счётчик", async () => {
    const { client, call } = fakeClient(net(), net(), okJob("running"), net(), net(), okJob("done", { result: { path: "a.png" } }));
    const p = waitForProjectJob(client, "j", { intervalMs: 100, errorsLimit: 3 });
    await vi.advanceTimersByTimeAsync(1000);
    await expect(p).resolves.toEqual({ path: "a.png" });
    expect(call).toHaveBeenCalledTimes(6);
  });
  it("третья ошибка подряд обрывает ожидание", async () => {
    const { client, call } = fakeClient(net(), net(), net(), okJob("done"));
    const p = waitForProjectJob(client, "j", { intervalMs: 100, errorsLimit: 3 });
    const caught = p.catch((e: unknown) => e);
    await vi.advanceTimersByTimeAsync(1000);
    expect(await caught).toMatchObject({ code: "network" });
    expect(call).toHaveBeenCalledTimes(3);
  });
  it("not_found и interrupted - итог сразу", async () => {
    for (const code of ["not_found", "interrupted"]) {
      const { client, call } = fakeClient(new StudioError(code, "x"), okJob("done"));
      await expect(waitForProjectJob(client, "j", { intervalMs: 100, errorsLimit: 3 })).rejects.toMatchObject({ code });
      expect(call).toHaveBeenCalledTimes(1);
    }
  });
  it("без errorsLimit первая ошибка обрывает ожидание", async () => {
    const { client, call } = fakeClient(net(), okJob("done"));
    await expect(waitForProjectJob(client, "j")).rejects.toMatchObject({ code: "network" });
    expect(call).toHaveBeenCalledTimes(1);
  });
});

describe("ожидание правки на уровне раздела", () => {
  it("флаг идёт до итога, правленый файл запоминается, тост и подписчики получают итог", async () => {
    const { client } = fakeClient(okJob("running"), okJob("done", { result: { path: "50_images/a.png", modified_ts: 7 } }));
    const jobs = createEditJobs();
    const say = vi.fn();
    const settled = vi.fn();
    jobs.onSettled(settled);
    jobs.track({ client, jobId: "j", name: "a.png", say, intervalMs: 100 });
    expect(jobs.getSnapshot().running).toBe("a.png");
    await vi.advanceTimersByTimeAsync(0);
    expect(jobs.getSnapshot().running).toBe("a.png");
    await vi.advanceTimersByTimeAsync(100);
    expect(jobs.getSnapshot()).toEqual({ running: null, edited: { "50_images/a.png": 7 } });
    expect(say).toHaveBeenCalledWith(IMAGE_TEXTS.edit_done, "success");
    expect(settled).toHaveBeenCalledWith({ result: expect.objectContaining({ path: "50_images/a.png" }) });
  });
  it("итог приходит и без подписчиков (экран ушёл), отписка работает", async () => {
    const { client } = fakeClient(okJob("done", { result: { path: "p.png", modified_ts: 1 } }));
    const jobs = createEditJobs();
    const say = vi.fn();
    const settled = vi.fn();
    jobs.onSettled(settled)();
    jobs.track({ client, jobId: "j", name: "p.png", say, intervalMs: 100 });
    await vi.advanceTimersByTimeAsync(0);
    expect(say).toHaveBeenCalledOnce();
    expect(settled).not.toHaveBeenCalled();
    expect(jobs.getSnapshot().running).toBeNull();
  });
  it("ошибка задания: текст из общей таблицы, edited не меняется, result null", async () => {
    const { client } = fakeClient(okJob("error", { error: { code: "internal", message: "секрет инструмента" } }));
    const jobs = createEditJobs();
    const say = vi.fn();
    const settled = vi.fn();
    jobs.onSettled(settled);
    jobs.track({ client, jobId: "j", name: "p.png", say, intervalMs: 100 });
    await vi.advanceTimersByTimeAsync(0);
    expect(say).toHaveBeenCalledWith(IMAGE_TEXTS.edit_failed, "error");
    expect(settled).toHaveBeenCalledWith({ result: null });
    expect(jobs.getSnapshot().edited).toEqual({});
  });
  it("пока правка идёт, второй запуск игнорируется; dispose обрывает ожидание без тоста", async () => {
    const { client, call } = fakeClient(okJob("running"), okJob("running"), okJob("running"));
    const jobs = createEditJobs();
    const say = vi.fn();
    jobs.track({ client, jobId: "j", name: "a.png", say, intervalMs: 100 });
    jobs.track({ client, jobId: "j2", name: "b.png", say, intervalMs: 100 });
    expect(jobs.getSnapshot().running).toBe("a.png");
    jobs.dispose();
    await vi.advanceTimersByTimeAsync(1000);
    expect(say).not.toHaveBeenCalled();
    expect(call.mock.calls.length).toBeLessThanOrEqual(1);
  });
  it("подписчики состояния получают уведомления", async () => {
    const { client } = fakeClient(okJob("done", { result: { path: "p.png", modified_ts: 1 } }));
    const jobs = createEditJobs();
    const changed = vi.fn();
    jobs.subscribe(changed);
    jobs.track({ client, jobId: "j", name: "p.png", say: vi.fn(), intervalMs: 100 });
    await vi.advanceTimersByTimeAsync(0);
    expect(changed).toHaveBeenCalledTimes(2);
  });
});

describe("текст ошибки правки", () => {
  it("locked - edit_busy, не серверный текст", () => {
    expect(editFailureText(new StudioError("locked", "серверный текст"))).toBe(IMAGE_TEXTS.edit_busy);
    expect(editFailureText(new StudioError("conflict", "x", { reason: "plan_changed" }))).toBe(IMAGE_TEXTS.edit_plan_changed);
    expect(editFailureText(new StudioError("conflict", "x", { reason: "cost_confirmation_required", unknown_cost: true }))).toBe(IMAGE_TEXTS.edit_confirm_unknown);
    expect(editFailureText(new StudioError("not_found", "x"))).toBe(IMAGE_TEXTS.edit_failed);
    expect(editFailureText(new StudioError("interrupted", "x"))).toBe(IMAGE_TEXTS.edit_failed);
    expect(editFailureText(new Error("boom"))).toBe(IMAGE_TEXTS.edit_failed);
  });
});
