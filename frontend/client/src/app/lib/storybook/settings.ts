// Обёртки возможностей модуля app_settings (настройки приложения).
import type { StorybookClient } from "./client";

export type SettingSource = "env" | "file" | "default";

export type SettingEntry<T> = { value: T; default: T; source: SettingSource };

export type AppSettingsValues = {
  projects_directory: SettingEntry<string>;
  backup_directory: SettingEntry<string>;
  logs_directory: SettingEntry<string>;
  max_backup_files: SettingEntry<number>;
  log_level: SettingEntry<string>;
};

export type PathState = "ok" | "missing" | "not_dir" | "not_writable";

export type PathCheck = { state: PathState; message: string };

export type AppSettingsResult = {
  settings: AppSettingsValues;
  checks: { projects_directory: PathCheck; backup_directory: PathCheck; logs_directory: PathCheck };
  warnings: string[];
  digest: string; // отправляется обратно как expected_digest
  projects_directory_locked_reason: string | null;
  changed?: string[]; // только в ответах update и reset
};

export type AppSettingsUpdateParams = {
  projects_directory?: string | null;
  backup_directory?: string | null;
  logs_directory?: string | null;
  max_backup_files?: number | null;
  log_level?: string | null;
  expected_digest?: string;
};

export type CheckPathResult = { state: PathState; message: string; resolved: string };

export const appSettingsGet = (c: StorybookClient) => c.call<AppSettingsResult>("app_settings_get");

// Значения undefined не отправляем: ключ отсутствует = "не менять"
export const appSettingsUpdate = (c: StorybookClient, p: AppSettingsUpdateParams) =>
  c.call<AppSettingsResult>(
    "app_settings_update",
    Object.fromEntries(Object.entries(p).filter(([, value]) => value !== undefined)),
  );

export const appSettingsReset = (c: StorybookClient, p: { confirm: boolean }) =>
  c.call<AppSettingsResult>("app_settings_reset", p);

export const appSettingsCheckPath = (
  c: StorybookClient,
  p: { kind: "projects" | "backups" | "logs"; path: string },
) => c.call<CheckPathResult>("app_settings_check_path", p);
