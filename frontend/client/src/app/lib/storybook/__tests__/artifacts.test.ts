import { describe, expect, it, vi } from "vitest";

import { createStorybookClient, StudioError } from "../client";
import {
  artifactDiff,
  artifactHistory,
  artifactRead,
  artifactRestore,
  artifactSave,
  artifactValidate,
  artifactsList,
  findingsByPointer,
  isConflict,
  type Finding,
} from "../artifacts";

const make = (reply: unknown = { status: "success" }) => {
  const transport = vi.fn().mockResolvedValue(reply);
  return { transport, client: createStorybookClient(transport, { apiBase: "http://h" }) };
};

describe("storybook artifacts", () => {
  it("каждая функция вызывает свою возможность с аргументами", async () => {
    const { transport, client } = make();
    await artifactsList(client, { project_id: "p" });
    await artifactRead(client, { project_id: "p", key: "brief", version_id: "v" });
    await artifactValidate(client, { project_id: "p", key: "brief", content: { a: 1 } });
    await artifactSave(client, { project_id: "p", key: "brief", base_digest: null, text: "{}" });
    await artifactHistory(client, { project_id: "p", key: "brief" });
    await artifactDiff(client, { project_id: "p", key: "brief", version_id: "v" });
    await artifactRestore(client, { project_id: "p", key: "brief", version_id: "v", base_digest: "j1:x" });
    const actions = transport.mock.calls.map((call) => call[0]);
    expect(actions).toEqual([
      "storybook.artifacts_list",
      "storybook.artifact_read",
      "storybook.artifact_validate",
      "storybook.artifact_save",
      "storybook.artifact_history",
      "storybook.artifact_diff",
      "storybook.artifact_restore",
    ]);
    expect(transport.mock.calls[1][1]).toEqual({ project_id: "p", key: "brief", version_id: "v" });
    expect(transport.mock.calls[3][1]).toEqual({ project_id: "p", key: "brief", base_digest: null, text: "{}" });
  });

  it("для save, diff и restore передаётся длинный таймаут, его можно переопределить", async () => {
    const { transport, client } = make();
    await artifactSave(client, { project_id: "p", key: "shots", base_digest: "j1:x", content: {} });
    await artifactDiff(client, { project_id: "p", key: "shots", version_id: "v" });
    await artifactRestore(client, { project_id: "p", key: "shots", version_id: "v", base_digest: null });
    await artifactSave(client, { project_id: "p", key: "shots", base_digest: null, content: {} }, { timeoutMs: 5 });
    await artifactRead(client, { project_id: "p", key: "shots" });
    expect(transport.mock.calls.map((call) => call[2])).toEqual([
      { timeoutMs: 60000 },
      { timeoutMs: 60000 },
      { timeoutMs: 60000 },
      { timeoutMs: 5 },
      { timeoutMs: 20000 },
    ]);
  });

  it("конфликт распознаётся по коду", async () => {
    const { client } = make({
      status: "error",
      error: { code: "conflict", message: "Файл изменён", details: { current_digest: "j1:new" } },
    });
    const error = await artifactSave(client, { project_id: "p", key: "brief", base_digest: "j1:old", text: "{}" }).catch(
      (e) => e,
    );
    expect(isConflict(error)).toBe(true);
    expect((error as StudioError).details).toEqual({ current_digest: "j1:new" });
    expect(isConflict(new StudioError("locked", "x"))).toBe(false);
    expect(isConflict(new Error("conflict"))).toBe(false);
  });

  it("findingsByPointer группирует находки по пути поля", () => {
    const f = (pointer: string, message: string): Finding => ({
      severity: "error",
      code: "required",
      path: [],
      pointer,
      label: "",
      message,
    });
    const grouped = findingsByPointer([f("/a", "1"), f("/b", "2"), f("/a", "3")]);
    expect([...grouped.keys()]).toEqual(["/a", "/b"]);
    expect(grouped.get("/a")?.map((x) => x.message)).toEqual(["1", "3"]);
    expect(findingsByPointer([]).size).toBe(0);
  });
});
