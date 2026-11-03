// Чистая логика экрана «Настройки»: форма, изменённые поля, запрос на сохранение, блокировки, подписи.
// Правила одинаковы с Tk-версией: векторы лежат в golden_settings_vectors.json.
import type {
  AppSettingsResult,
  AppSettingsUpdateParams,
  PathCheck,
  PathState,
  SettingEntry,
} from "./settings";
import { SETTINGS_TEXTS, settingsFmt, type SettingsTextId } from "./settingsTexts";

export const FORM_FIELDS = ["projects_directory", "backup_directory", "logs_directory", "max_backup_files", "log_level"] as const;
export type FormField = (typeof FORM_FIELDS)[number];
export type SettingsForm = Record<FormField, string>;

export const PATH_FIELDS = ["projects_directory", "backup_directory", "logs_directory"] as const;
export type PathField = (typeof PATH_FIELDS)[number];
export type PathKind = "projects" | "backups" | "logs";

export const PATH_KIND: Record<PathField, PathKind> = {
  projects_directory: "projects",
  backup_directory: "backups",
  logs_directory: "logs",
};

export const FIELD_TITLE: Record<FormField, SettingsTextId> = {
  projects_directory: "field_projects",
  backup_directory: "field_backups",
  logs_directory: "field_logs",
  max_backup_files: "field_max_backups",
  log_level: "field_log_level",
};

// Пять значений журнала, которые принимает сервер
export const LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] as const;
export type LogLevel = (typeof LOG_LEVELS)[number];

export const LOG_LEVEL_LABELS: Record<LogLevel, string> = {
  DEBUG: SETTINGS_TEXTS.level_debug,
  INFO: SETTINGS_TEXTS.level_info,
  WARNING: SETTINGS_TEXTS.level_warning,
  ERROR: SETTINGS_TEXTS.level_error,
  CRITICAL: SETTINGS_TEXTS.level_critical,
};

export const logLevelLabel = (level: string): string => LOG_LEVEL_LABELS[level.toUpperCase() as LogLevel] ?? level;

export const PATH_STATE_TEXT: Record<PathState, string> = {
  ok: SETTINGS_TEXTS.path_ok,
  missing: SETTINGS_TEXTS.path_missing,
  not_dir: SETTINGS_TEXTS.path_not_dir,
  not_writable: SETTINGS_TEXTS.path_not_writable,
};

export const MAX_BACKUPS_MIN = 1;
export const MAX_BACKUPS_MAX = 100;

export const formFromSettings = (r: Pick<AppSettingsResult, "settings">): SettingsForm => ({
  projects_directory: String(r.settings.projects_directory.value ?? ""),
  backup_directory: String(r.settings.backup_directory.value ?? ""),
  logs_directory: String(r.settings.logs_directory.value ?? ""),
  max_backup_files: String(r.settings.max_backup_files.value ?? ""),
  log_level: String(r.settings.log_level.value ?? "").toUpperCase(),
});

const norm = (field: FormField, value: string): string =>
  field === "log_level" ? value.trim().toUpperCase() : value.trim();

// Какие поля отличаются от сохранённых
export const changedFields = (form: SettingsForm, base: SettingsForm): FormField[] =>
  FORM_FIELDS.filter((f) => norm(f, form[f]) !== norm(f, base[f]));

export const isDirty = (form: SettingsForm, base: SettingsForm): boolean => changedFields(form, base).length > 0;

export type FieldLock = { disabled: boolean; note: string | null };

// Поле «Папка проектов» закрыто окружением сервера или идущим прогоном; остальные поля открыты
export const fieldLock = (r: Pick<AppSettingsResult, "settings" | "projects_directory_locked_reason">, field: FormField): FieldLock => {
  if (field !== "projects_directory") return { disabled: false, note: null };
  if (r.settings.projects_directory.source === "env") return { disabled: true, note: SETTINGS_TEXTS.source_env };
  if (r.projects_directory_locked_reason) return { disabled: true, note: r.projects_directory_locked_reason };
  return { disabled: false, note: null };
};

// Слабая отметка «по умолчанию»: только когда значение не взято из файла или окружения
export const defaultMark = (entry: Pick<SettingEntry<unknown>, "source">): string | null =>
  entry.source === "default" ? SETTINGS_TEXTS.source_default : null;

export type BuiltUpdate =
  | { ok: true; payload: AppSettingsUpdateParams }
  | { ok: false; errors: Partial<Record<FormField, string>> };

// Запрос на сохранение: только изменённые поля и expected_digest
export const buildUpdate = (
  form: SettingsForm,
  base: SettingsForm,
  snapshot: Pick<AppSettingsResult, "settings" | "projects_directory_locked_reason" | "digest">,
): BuiltUpdate => {
  const errors: Partial<Record<FormField, string>> = {};
  const payload: AppSettingsUpdateParams = {};
  for (const field of changedFields(form, base)) {
    if (fieldLock(snapshot, field).disabled) continue;
    const value = norm(field, form[field]);
    if (field === "max_backup_files") {
      const n = /^\d+$/.test(value) ? Number(value) : NaN;
      if (!Number.isInteger(n) || n < MAX_BACKUPS_MIN || n > MAX_BACKUPS_MAX) errors[field] = SETTINGS_TEXTS.max_backups_invalid;
      else payload.max_backup_files = n;
    } else if (field === "log_level") {
      payload.log_level = value;
    } else if (value === "") {
      errors[field] = settingsFmt("path_empty", { name: SETTINGS_TEXTS[FIELD_TITLE[field]] });
    } else {
      payload[field] = value;
    }
  }
  if (Object.keys(errors).length > 0) return { ok: false, errors };
  return { ok: true, payload: { ...payload, expected_digest: snapshot.digest } };
};

// Куда повесить ошибку сервера: по началу сообщения (тексты сервера); пересечение папок и прочее - на группу папок
export const fieldForServerError = (message: string): FormField | "paths" => {
  if (message.startsWith("Папка проектов")) return "projects_directory";
  if (message.startsWith("Папка бэкапов")) return "backup_directory";
  if (message.startsWith("Папка логов")) return "logs_directory";
  if (message.includes("Число бэкапов")) return "max_backup_files";
  if (message.includes("Уровень логов")) return "log_level";
  return "paths";
};

export type PathCheckView = { tone: "ok" | "warn" | "pending"; text: string; resolved: string | null };

// Что показать рядом с полем папки; до первой проверки - ответ сервера из снимка настроек
export const pathCheckView = (
  check: (Pick<PathCheck, "state"> & { message?: string; resolved?: string }) | null,
  checking: boolean,
): PathCheckView | null => {
  if (checking) return { tone: "pending", text: SETTINGS_TEXTS.path_checking, resolved: null };
  if (!check) return null;
  return {
    tone: check.state === "ok" ? "ok" : "warn",
    text: PATH_STATE_TEXT[check.state] ?? check.message ?? "",
    resolved: check.resolved ?? null,
  };
};

// Проверять путь на сервере нужно, только если поле не пустое и значение изменилось
export const shouldCheckPath = (value: string): boolean => value.trim() !== "";
