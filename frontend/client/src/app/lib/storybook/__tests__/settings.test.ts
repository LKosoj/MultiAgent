import { describe, expect, it, vi } from "vitest";

import type { StorybookClient } from "../client";
import { appSettingsCheckPath, appSettingsGet, appSettingsReset, appSettingsUpdate } from "../settings";

const fakeClient = () => {
  const call = vi.fn().mockResolvedValue({ status: "success" });
  return { client: { call } as unknown as StorybookClient, call };
};

describe("обёртки app_settings", () => {
  it("вызывают нужные id", async () => {
    const { client, call } = fakeClient();
    await appSettingsGet(client);
    await appSettingsReset(client, { confirm: true });
    await appSettingsCheckPath(client, { kind: "backups", path: "/tmp/b" });
    expect(call.mock.calls).toEqual([
      ["app_settings_get"],
      ["app_settings_reset", { confirm: true }],
      ["app_settings_check_path", { kind: "backups", path: "/tmp/b" }],
    ]);
  });

  it("update не отправляет undefined, но оставляет null и digest", async () => {
    const { client, call } = fakeClient();
    await appSettingsUpdate(client, {
      log_level: "DEBUG",
      max_backup_files: undefined,
      backup_directory: null,
      expected_digest: "abc",
    });
    expect(call).toHaveBeenCalledWith("app_settings_update", {
      log_level: "DEBUG",
      backup_directory: null,
      expected_digest: "abc",
    });
  });
});
