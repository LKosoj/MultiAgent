// Обёртки возможностей модуля shots (шоты: список, карточка, правка) и чистые помощники.
import { StudioError, type StorybookClient } from "./client";
import { compareShotKeys } from "./reports";

export type ShotState = "ready" | "stale" | "error" | "generating" | "missing" | "partial";
export type PartState = "missing" | "ready" | "generating" | "error" | "not_started" | "not_applicable";
export type Freshness = "fresh" | "stale" | "unknown";
export type ShotPartName = "start" | "end" | "video";

export type ShotPart = {
  state: PartState;
  path: string | null;
  exists: boolean;
  freshness: Freshness | null;
  unit?: { keys: string[]; path: string } | null;
  approximate?: boolean;
};

export type ShotJobBrief = {
  status: string;
  status_title: string;
  provider: string | null;
  model: string | null;
  attempts: number;
  cost: number | null;
  currency: string | null;
  spent_rub: number;
  recent: { status: string; status_title: string; at: string | null; cost: number | null; currency: string | null; error: string | null }[];
  unknown_currency_attempts: number;
};

// Идентификатор называется shot_key: поле "key" бэкенд маскирует как секрет.
export type ShotRow = {
  shot_key: string;
  scene: number;
  shot: number;
  index: number;
  duration_s: number | null;
  duration_source: string | null;
  prompt_preview: string;
  thumb_path: string | null;
  thumb_rev: string | null;
  parts: Record<ShotPartName, ShotPart>;
  state: ShotState;
  state_title: string;
  stale: boolean;
  stale_parts: ShotPartName[];
  freshness: Freshness | null;
  review: { status: string; status_title: string; note?: string | null } | null;
  findings: { error: number; warning: number; info: number } | null;
  job: ShotJobBrief | null;
  blockout: { start: boolean; end: boolean; video: boolean };
};

export type ShotsFilters = {
  scene?: number;
  state?: ShotState | "";
  review?: string;
  freshness?: Freshness | "";
  stale_only?: boolean;
  with_findings?: boolean;
  query?: string;
  sort?: "scene_shot" | "state" | "duration" | "cost";
};
export type ShotsListFilters = ShotsFilters;

export type ShotsFacets = {
  scenes: { scene: number; count: number }[];
  state: Record<ShotState, number>;
  review: Record<string, number>;
  freshness: Record<Freshness, number>;
  with_findings: number;
  stale: number;
};

export type ShotsListResult = {
  project_id: string;
  revision: string;
  items: ShotRow[];
  total: number;
  offset: number;
  limit: number;
  facets: ShotsFacets;
  baseline: { missing: number; outdated: number };
  state_titles: Record<ShotState, string>;
  freshness_titles: Record<Freshness, string>;
  review_titles: Record<string, string>;
  reason: string | null;
  hook_errors: string[];
};

export type ShotChanges = Partial<{
  start_prompt: string;
  end_prompt: string;
  video_prompt: string;
  start_negative_prompt: string;
  end_negative_prompt: string;
  duration_s: number;
}>;

export type ShotImageSide = {
  prompt: string;
  negative_prompt: string;
  shot_type: string;
  output_path: string;
  width: number | null;
  height: number | null;
  seed: number | null;
  true_cfg_scale: number | null;
  num_inference_steps: number | null;
  references: { path: string | null; exists: boolean }[];
};

export type ShotEditable = { fields: (keyof ShotChanges)[]; locked: boolean; locked_reason: string | null; hint: string };
export type ShotEdit = { at: string; shot_key: string; fields: Record<string, { old: unknown; new: unknown }> };
export type ShotCascade = { stale_now: ShotPartName[]; stale_after_regeneration: ShotPartName[] };

export type ShotCard = ShotRow & {
  start: ShotImageSide | null;
  end: ShotImageSide | null;
  video: {
    prompt: string;
    duration_s: number | null;
    duration_source: string | null;
    timing: string | null;
    unit: { keys: string[]; path: string } | null;
    size: number | null;
    mtime_ns: number | null;
  };
  digest: string;
  editable: ShotEditable;
  stale_reasons: Partial<Record<ShotPartName, string[]>>;
  unknown_reasons: Partial<Record<ShotPartName, string[]>>;
  cascade: ShotCascade;
  upstream_stale_keys: string[];
  edits: ShotEdit[];
  warnings: string[];
  blockout_state: unknown;
};

export type ShotWarning = { code: string; message: string };
export type ShotUpdateResult = {
  shot: ShotCard;
  digest: string;
  changed: (keyof ShotChanges)[];
  effects: ShotCascade & { note: string | null };
  warnings: ShotWarning[];
};

export const shotsList = (c: StorybookClient, p: { project_id: string; limit?: number; offset?: number } & ShotsFilters) =>
  c.call<ShotsListResult>("shots_list", p);

export const shotsKeys = (c: StorybookClient, p: { project_id: string; limit?: number } & ShotsFilters) =>
  c.call<{ keys: string[]; total: number; truncated: boolean }>("shots_keys", p);

export const shotGet = (c: StorybookClient, p: { project_id: string; shot_key: string }) =>
  c.call<{ shot: ShotCard; hook_errors: string[] }>("shot_get", p);

export const shotUpdate = (
  c: StorybookClient,
  p: { project_id: string; shot_key: string; changes: ShotChanges; base_digest: string },
) => c.call<ShotUpdateResult>("shot_update", p);

export const shotsAdoptBaseline = (
  c: StorybookClient,
  p: { project_id: string; shot_keys?: string[]; parts?: ShotPartName[] },
) => c.call<{ adopted: number; parts: number; skipped_stale: number }>("shots_adopt_baseline", p);

/** Миниатюра шота: добавляет токен файла, чтобы браузер не держал старую картинку после перегенерации. */
export const shotThumbUrl = (
  c: StorybookClient,
  projectId: string,
  row: Pick<ShotRow, "thumb_path" | "thumb_rev">,
  size: 128 | 256 | 512 = 128,
): string | null => {
  if (!row.thumb_path) return null;
  const url = c.thumbUrl(projectId, row.thumb_path, size);
  if (!url) return null;
  return row.thumb_rev ? `${url}&v=${encodeURIComponent(row.thumb_rev)}` : url;
};

export const shotMediaUrl = (c: StorybookClient, projectId: string, relPath: string | null): string | null =>
  relPath ? c.mediaUrl(projectId, relPath) : null;

export const isConflict = (e: unknown): e is StudioError => e instanceof StudioError && e.code === "conflict";

/** Текущие значения правимых полей из ошибки conflict (для «Оставить мою правку как черновик»). */
export const conflictDraft = (e: unknown): { current_digest: string | null; current: ShotChanges } | null => {
  if (!isConflict(e) || !e.details) return null;
  const d = e.details as { current_digest?: unknown; current?: unknown };
  return {
    current_digest: typeof d.current_digest === "string" ? d.current_digest : null,
    current: (d.current && typeof d.current === "object" ? d.current : {}) as ShotChanges,
  };
};

export type ShotForm = {
  start_prompt: string;
  end_prompt: string;
  video_prompt: string;
  start_negative_prompt: string;
  end_negative_prompt: string;
  duration_s: number | null;
};

/** Только реально изменённые поля формы; пустой объект = сохранять нечего. */
export const dirtyChanges = (card: ShotCard, form: Partial<ShotForm>): ShotChanges => {
  const original: Partial<ShotForm> = {
    start_prompt: card.start?.prompt ?? "",
    end_prompt: card.end?.prompt ?? "",
    video_prompt: card.video.prompt,
    start_negative_prompt: card.start?.negative_prompt ?? "",
    end_negative_prompt: card.end?.negative_prompt ?? "",
    duration_s: card.video.duration_s,
  };
  const allowed = new Set<string>(card.editable.fields);
  const out: Record<string, string | number> = {};
  for (const name of Object.keys(original) as (keyof ShotForm)[]) {
    const value = form[name];
    if (value === undefined || value === null || !allowed.has(name)) continue;
    if (value !== original[name]) out[name] = value;
  }
  return out as ShotChanges;
};

export const canEditShot = (card: Pick<ShotCard, "editable">): boolean =>
  !card.editable.locked && card.editable.fields.length > 0;

/** Группы «Сцена N» в числовом порядке ключей ("9-6" раньше "10-1"). */
export const groupByScene = (rows: ShotRow[]): { scene: number; rows: ShotRow[] }[] => {
  const sorted = [...rows].sort((a, b) => compareShotKeys(a.shot_key, b.shot_key));
  const groups: { scene: number; rows: ShotRow[] }[] = [];
  for (const row of sorted) {
    const last = groups[groups.length - 1];
    if (last && last.scene === row.scene) last.rows.push(row);
    else groups.push({ scene: row.scene, rows: [row] });
  }
  return groups;
};
