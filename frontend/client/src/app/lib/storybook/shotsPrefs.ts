// Что экран «Кадры» помнит в браузере: фильтры, режим, выбранный шот (localStorage)
// и несохранённые правки карточки (sessionStorage). Любой сбой хранилища - работаем без него.
import type { ShotChanges, ShotState } from "./shots";
import type { ListMode } from "./shotsWindow";

export type StorageLike = {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem?(key: string): void;
};

export type ClientPredicate = "missing_images" | "missing_videos";

export type ShotsUiFilters = {
  scene: number | null;
  review: string;
  state: ShotState | "";
  staleOnly: boolean;
  withFindings: boolean;
  query: string;
  predicate: ClientPredicate | null;
};

export const EMPTY_FILTERS: ShotsUiFilters = {
  scene: null,
  review: "",
  state: "",
  staleOnly: false,
  withFindings: false,
  query: "",
  predicate: null,
};

export type ShotsPrefs = { filters: ShotsUiFilters; mode: ListMode; selected: string | null };

export const DEFAULT_PREFS: ShotsPrefs = { filters: EMPTY_FILTERS, mode: "list", selected: null };

const STATES: readonly string[] = ["ready", "stale", "error", "generating", "missing", "partial"];
const SAFE_ID = /^[A-Za-z0-9_-]{1,128}$/;
const SHOT_KEY = /^\d{1,6}-\d{1,6}$/;

export const prefsKey = (projectId: string): string => `storybook-studio:shots:v1:${projectId}`;
export const draftKey = (projectId: string, shotKey: string): string =>
  `storybook-studio:shots:v1:draft:${projectId}:${shotKey}`;

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);

export const sanitizePrefs = (raw: unknown): ShotsPrefs => {
  if (!isRecord(raw)) return DEFAULT_PREFS;
  const f = isRecord(raw.filters) ? raw.filters : {};
  const filters: ShotsUiFilters = {
    scene: typeof f.scene === "number" && Number.isInteger(f.scene) && f.scene >= 0 ? f.scene : null,
    review: typeof f.review === "string" && SAFE_ID.test(f.review) ? f.review : "",
    state: typeof f.state === "string" && STATES.includes(f.state) ? (f.state as ShotState) : "",
    staleOnly: f.staleOnly === true,
    withFindings: f.withFindings === true,
    query: typeof f.query === "string" ? f.query.slice(0, 200) : "",
    predicate: f.predicate === "missing_images" || f.predicate === "missing_videos" ? f.predicate : null,
  };
  return {
    filters,
    mode: raw.mode === "grid" ? "grid" : "list",
    selected: typeof raw.selected === "string" && SHOT_KEY.test(raw.selected) ? raw.selected : null,
  };
};

export const loadPrefs = (storage: StorageLike | null, projectId: string): ShotsPrefs => {
  if (!storage) return DEFAULT_PREFS;
  try {
    const text = storage.getItem(prefsKey(projectId));
    return text ? sanitizePrefs(JSON.parse(text)) : DEFAULT_PREFS;
  } catch {
    return DEFAULT_PREFS;
  }
};

export const savePrefs = (storage: StorageLike | null, projectId: string, prefs: ShotsPrefs): void => {
  if (!storage) return;
  try {
    storage.setItem(prefsKey(projectId), JSON.stringify(prefs));
  } catch {
    // хранилище недоступно: работаем без сохранения
  }
};

// ---- черновики карточки ----

export type ShotDraft = { changes: ShotChanges; note: string | null; baseDigest: string | null };

const TEXT_FIELDS = ["start_prompt", "end_prompt", "video_prompt", "start_negative_prompt", "end_negative_prompt"] as const;

export const sanitizeDraft = (raw: unknown): ShotDraft | null => {
  if (!isRecord(raw) || !isRecord(raw.changes)) return null;
  const changes: Record<string, string | number> = {};
  for (const name of TEXT_FIELDS) {
    const v = raw.changes[name];
    if (typeof v === "string") changes[name] = v;
  }
  const d = raw.changes.duration_s;
  if (typeof d === "number" && Number.isFinite(d) && d > 0) changes.duration_s = d;
  const note = typeof raw.note === "string" ? raw.note : null;
  if (Object.keys(changes).length === 0 && note === null) return null;
  return {
    changes: changes as ShotChanges,
    note,
    baseDigest: typeof raw.baseDigest === "string" ? raw.baseDigest : null,
  };
};

export const loadDraft = (storage: StorageLike | null, projectId: string, shotKey: string): ShotDraft | null => {
  if (!storage) return null;
  try {
    const text = storage.getItem(draftKey(projectId, shotKey));
    return text ? sanitizeDraft(JSON.parse(text)) : null;
  } catch {
    return null;
  }
};

export const saveDraft = (storage: StorageLike | null, projectId: string, shotKey: string, draft: ShotDraft): void => {
  if (!storage) return;
  try {
    storage.setItem(draftKey(projectId, shotKey), JSON.stringify(draft));
  } catch {
    // нет места или приватный режим
  }
};

export const clearDraft = (storage: StorageLike | null, projectId: string, shotKey: string): void => {
  if (!storage) return;
  try {
    if (storage.removeItem) storage.removeItem(draftKey(projectId, shotKey));
    else storage.setItem(draftKey(projectId, shotKey), "");
  } catch {
    // ничего: черновик останется до закрытия вкладки
  }
};
