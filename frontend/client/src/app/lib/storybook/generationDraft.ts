// Что экран «Генерация» помнит при уходе на другой экран: текст сказки, поля брифа, настройки формы (sessionStorage).
// Любой сбой хранилища - работаем без него.
import type { StorageLike } from "./shotsPrefs";
import type { BriefDraft } from "./generationForm";
import type { RegenWhat } from "./review";
import type { RunSettings } from "./runSettings";

export type FormDraft = { task: string; brief: BriefDraft; regenWhat: RegenWhat };

export const formKey = (projectId: string): string => `storybook-studio:generation:v1:form:${projectId}`;
export const settingsKey = (projectId: string): string => `storybook-studio:generation:v1:settings:${projectId}`;

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const WHAT: readonly string[] = ["images", "video", "both", "music", "film"];

export const sanitizeForm = (raw: unknown): FormDraft | null => {
  if (!isRecord(raw)) return null;
  const brief: Record<string, string | boolean> = {};
  if (isRecord(raw.brief)) {
    for (const [k, v] of Object.entries(raw.brief)) {
      if (typeof v === "string" || typeof v === "boolean") brief[k] = typeof v === "string" ? v.slice(0, 5000) : v;
    }
  }
  return {
    task: typeof raw.task === "string" ? raw.task.slice(0, 20000) : "",
    brief: brief as BriefDraft,
    regenWhat: typeof raw.regenWhat === "string" && WHAT.includes(raw.regenWhat) ? (raw.regenWhat as RegenWhat) : "both",
  };
};

export const loadForm = (storage: StorageLike | null, projectId: string): FormDraft | null => {
  if (!storage) return null;
  try {
    const text = storage.getItem(formKey(projectId));
    return text ? sanitizeForm(JSON.parse(text)) : null;
  } catch {
    return null;
  }
};

const isEmptyForm = (d: FormDraft): boolean => d.task === "" && Object.keys(d.brief).length === 0 && d.regenWhat === "both";

// Пустую форму не храним: ключ убирается
export const saveForm = (storage: StorageLike | null, projectId: string, draft: FormDraft): void => {
  if (!storage) return;
  try {
    if (isEmptyForm(draft)) storage.removeItem?.(formKey(projectId));
    else storage.setItem(formKey(projectId), JSON.stringify(draft));
  } catch {
    // хранилище недоступно или переполнено
  }
};

// Настройки привязаны к отпечатку сохранённой версии: если их успели изменить в другом окне, черновик не подставляем
export const loadSettingsDraft = (storage: StorageLike | null, projectId: string, digest: string | null): RunSettings | null => {
  if (!storage || !digest) return null;
  try {
    const text = storage.getItem(settingsKey(projectId));
    const raw: unknown = text ? JSON.parse(text) : null;
    if (!isRecord(raw) || raw.digest !== digest || !isRecord(raw.settings)) return null;
    return raw.settings as unknown as RunSettings;
  } catch {
    return null;
  }
};

export const saveSettingsDraft = (storage: StorageLike | null, projectId: string, digest: string | null, draft: RunSettings | null): void => {
  if (!storage) return;
  try {
    if (draft && digest) storage.setItem(settingsKey(projectId), JSON.stringify({ digest, settings: draft }));
    else storage.removeItem?.(settingsKey(projectId));
  } catch {
    // хранилище недоступно или переполнено
  }
};

export const sessionStorageOrNull = (): StorageLike | null => {
  try {
    return typeof window !== "undefined" ? window.sessionStorage : null;
  } catch {
    return null;
  }
};
