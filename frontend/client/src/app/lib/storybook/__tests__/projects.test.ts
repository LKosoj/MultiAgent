import { afterEach, describe, expect, it, vi } from "vitest";

import { StudioError, type StorybookClient } from "../client";
import {
  projectsBackupCreate,
  projectsBackupRestore,
  projectsBackupsList,
  projectsCreate,
  projectsCreateFromPrompt,
  projectsDelete,
  projectsDuplicate,
  projectsExport,
  projectsGet,
  projectsJobStatus,
  projectsList,
  projectsRename,
  projectsSize,
  projectsSuggestId,
  waitForProjectJob,
} from "../projects";

const fakeClient = (...replies: unknown[]) => {
  const call = vi.fn();
  for (const reply of replies) call.mockResolvedValueOnce(reply);
  return { client: { call } as unknown as StorybookClient, call };
};

const job = (state: string, extra: Record<string, unknown> = {}) => ({
  status: "success",
  job_id: "a".repeat(32),
  kind: "backup",
  project_id: "p",
  state,
  progress: { current: 1, total: 4, message: "Копирование" },
  ...extra,
});

afterEach(() => vi.useRealTimers());

describe("обёртки projects", () => {
  it("вызывают нужный id с теми же ключами", async () => {
    const { client, call } = fakeClient(...Array(14).fill({ status: "success" }));
    await projectsList(client, { search: "лис", sort: "name" });
    await projectsGet(client, { project_id: "p" });
    await projectsSize(client, { project_id: "p" });
    await projectsSuggestId(client, { id_hint: "Мой проект" });
    await projectsCreate(client, { title: "Т", description: "Описание", genre: "сказка", target_age: "3-5" });
    await projectsCreateFromPrompt(client, { prompt: "Про лисёнка", id_hint: "fox" });
    await projectsDuplicate(client, { project_id: "p", id_hint: "p2" });
    await projectsRename(client, { project_id: "p", title: "Новое" });
    await projectsDelete(client, { project_id: "p", confirm: true });
    await projectsBackupsList(client, { for_project_id: "p" });
    await projectsBackupCreate(client, { project_id: "p", note: "перед правкой" });
    await projectsBackupRestore(client, { project_id: "p", backup_id: "p_backup_20260101_000000", confirm: true });
    await projectsExport(client, { project_id: "p" });
    await projectsJobStatus(client, { job_id: "x" });
    expect(call.mock.calls.map((c) => c[0])).toEqual([
      "projects_list", "projects_get", "projects_size", "projects_suggest_id", "projects_create",
      "projects_create_from_prompt", "projects_duplicate", "projects_rename", "projects_delete",
      "projects_backups_list", "projects_backup_create", "projects_backup_restore", "projects_export",
      "projects_job_status",
    ]);
    expect(call.mock.calls[0][1]).toEqual({ search: "лис", sort: "name" });
    expect(call.mock.calls[8][1]).toEqual({ project_id: "p", confirm: true });
    expect(call.mock.calls[11][1]).toEqual({
      project_id: "p", backup_id: "p_backup_20260101_000000", confirm: true,
    });
  });

  it("не подставляет лишние параметры по умолчанию", async () => {
    const { client, call } = fakeClient({ status: "success" }, { status: "success" });
    await projectsList(client);
    await projectsBackupsList(client);
    expect(call).toHaveBeenNthCalledWith(1, "projects_list", undefined);
    expect(call).toHaveBeenNthCalledWith(2, "projects_backups_list", undefined);
  });
});

describe("waitForProjectJob", () => {
  it("опрашивает с интервалом и возвращает результат", async () => {
    vi.useFakeTimers();
    const { client, call } = fakeClient(job("running"), job("running"), job("done", { result: { backup_id: "b" } }));
    const seen: number[] = [];
    const promise = waitForProjectJob<{ backup_id: string }>(client, "j", {
      intervalMs: 500,
      onProgress: (p) => seen.push(p.current),
    });
    await vi.advanceTimersByTimeAsync(0);
    expect(call).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(499);
    expect(call).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(call).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(500);
    await expect(promise).resolves.toEqual({ backup_id: "b" });
    expect(seen).toEqual([1, 1, 1]);
    expect(call).toHaveBeenCalledWith("projects_job_status", { job_id: "j" });
  });

  it("бросает StudioError с кодом из error", async () => {
    const { client } = fakeClient(job("error", { error: { code: "locked", message: "Занято" } }));
    await expect(waitForProjectJob(client, "j")).rejects.toMatchObject({ code: "locked", message: "Занято" });
  });

  it("прерванное задание даёт код interrupted", async () => {
    const { client } = fakeClient(job("interrupted"));
    await expect(waitForProjectJob(client, "j")).rejects.toBeInstanceOf(StudioError);
  });

  it("отмена через AbortSignal останавливает опрос", async () => {
    vi.useFakeTimers();
    const { client, call } = fakeClient(job("running"), job("running"));
    const controller = new AbortController();
    const promise = waitForProjectJob(client, "j", { intervalMs: 1000, signal: controller.signal });
    const assertion = expect(promise).rejects.toMatchObject({ code: "aborted" });
    await vi.advanceTimersByTimeAsync(0);
    controller.abort();
    await assertion;
    await vi.advanceTimersByTimeAsync(5000);
    expect(call).toHaveBeenCalledTimes(1);
  });

  it("уже отменённый сигнал не делает запросов", async () => {
    const { client, call } = fakeClient();
    const controller = new AbortController();
    controller.abort();
    await expect(waitForProjectJob(client, "j", { signal: controller.signal })).rejects.toMatchObject({
      code: "aborted",
    });
    expect(call).not.toHaveBeenCalled();
  });
});
