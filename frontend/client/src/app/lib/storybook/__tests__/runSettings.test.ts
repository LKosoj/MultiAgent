import { describe, expect, it, vi } from "vitest";

import { StudioError } from "../client";
import type { StorybookClient } from "../client";
import {
  providerCatalog,
  runPreflightSummary,
  runSettingsGet,
  runSettingsSave,
  styleCatalog,
  type RunSettings,
} from "../runSettings";

const fakeClient = (impl?: () => Promise<unknown>) => {
  const call = vi.fn<(...args: unknown[]) => Promise<unknown>>(impl ?? (() => Promise.resolve({ status: "success" })));
  return { client: { call } as unknown as StorybookClient, call };
};

const settings = {
  version: 1,
  video: { provider: "routerai" },
  budget_rub: 100,
} as unknown as RunSettings;

describe("обёртки run_settings", () => {
  it("вызывают нужные id с нужной нагрузкой", async () => {
    const { client, call } = fakeClient();
    await runSettingsGet(client, { project_id: "p" });
    await runSettingsSave(client, { project_id: "p", settings, expected_digest: "d" });
    await styleCatalog(client);
    await runPreflightSummary(client, { project_id: "p" });
    expect(call.mock.calls.map((c) => c[0])).toEqual([
      "run_settings_get",
      "run_settings_save",
      "style_catalog",
      "run_preflight_summary",
    ]);
    expect(call.mock.calls[1][1]).toEqual({ project_id: "p", settings, expected_digest: "d" });
  });

  it("каталог: обычный вызов без своего таймаута, обновление - с 30 секундами", async () => {
    const { client, call } = fakeClient();
    await providerCatalog(client);
    await providerCatalog(client, { refresh_remote: true, for_project_id: "p" });
    expect(call.mock.calls[0]).toEqual(["provider_catalog", undefined, undefined]);
    expect(call.mock.calls[1]).toEqual([
      "provider_catalog",
      { refresh_remote: true, for_project_id: "p" },
      { timeoutMs: 30_000 },
    ]);
  });

  it("ошибка conflict остаётся StudioError", async () => {
    const { client } = fakeClient(() => Promise.reject(new StudioError("conflict", "Настройки изменены")));
    await expect(runSettingsSave(client, { project_id: "p", settings })).rejects.toMatchObject({
      code: "conflict",
    });
  });
});
