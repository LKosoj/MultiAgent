import { expect, it, vi } from "vitest";
import { createStorybookClient } from "../client";
import { seriesResourceUpdatePreview, speechLineUpdate } from "../series";

it("передаёт операции серии и аргументы в общий клиент", async () => {
  const transport = vi.fn().mockResolvedValue({ status: "success" });
  const client = createStorybookClient(transport, { apiBase: "http://h", seriesId: "episode" });
  await speechLineUpdate(client, { project_id: "p", line_id: "l", text: "Текст", delivery: "тихо", pause_before: 1, pause_after: 2 });
  expect(transport).toHaveBeenCalledWith("storybook.speech_line_update", { project_id: "p", series_id: "episode", line_id: "l", text: "Текст", delivery: "тихо", pause_before: 1, pause_after: 2 }, { timeoutMs: 20000 });
  await seriesResourceUpdatePreview(client, { project_id: "p", series_id: "episode", kind: "characters", entity_id: "hero", version_id: "v2" });
  expect(transport).toHaveBeenCalledWith("storybook.series_resource_update_preview", { project_id: "p", series_id: "episode", kind: "characters", entity_id: "hero", version_id: "v2" }, { timeoutMs: 20000 });
});
