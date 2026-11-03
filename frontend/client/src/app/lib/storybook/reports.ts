// Обёртки возможностей модуля reports (сводка отчётов проверок) и чистые помощники.
import type { StorybookClient } from "./client";

export type ReportLevel = "error" | "warning" | "info";
export type ReportSourceId =
  | "preflight"
  | "delivery_promise"
  | "shots_prompt_qa"
  | "blockout"
  | "continuity_images"
  | "provider_jobs"
  | "continuity_videos"
  | "decision_log"
  | "render_report"
  | "final_review"
  | "video_plan"
  | "quality_review";
export type ReportStatus = "ok" | "warning" | "error" | "missing" | "invalid";
export type LevelCounts = Record<ReportLevel, number>;

export type ReportFinding = {
  id: string;
  level: ReportLevel;
  level_title: string;
  source: ReportSourceId;
  source_title: string;
  code: string;
  message: string;
  shot_key: string | null;
  related_shot_keys: string[];
  phase: "start" | "end" | null;
  scope: "shot" | "group" | "project";
  file: string;
  details: Record<string, unknown>;
};

export type ReportEntry = {
  id: ReportSourceId;
  title: string;
  step_ids: string[];
  file: string;
  status: ReportStatus;
  status_title: string;
  counts: LevelCounts;
  updated_at: string | null;
  meta: Record<string, unknown>;
  error: string | null;
};

export type ReportsForShotResult = {
  shot_key: string;
  counts: LevelCounts;
  groups: { source: ReportSourceId; title: string; status: ReportStatus; findings: ReportFinding[] }[];
};

export const reportsForShot = (
  c: StorybookClient,
  p: { project_id: string; shot_key: string; verify_hashes?: boolean },
) => c.call<ReportsForShotResult>("reports_for_shot", p);

// ---- чистые помощники (без обращений к клиенту) ----

export const LEVEL_ORDER: ReportLevel[] = ["error", "warning", "info"];
export const LEVEL_RANK: Record<ReportLevel, number> = { error: 0, warning: 1, info: 2 };

const toCount = (value: unknown): number => (typeof value === "number" && Number.isFinite(value) ? value : 0);

const parseKey = (key: unknown): [number, number] | null => {
  if (typeof key !== "string") return null;
  const m = /^(\d+)-(\d+)$/.exec(key.trim());
  return m ? [Number(m[1]), Number(m[2])] : null;
};

/** Ключ шота "сцена-шот" без нулей; всё нечисловое и отрицательное даёт null. */
export const makeShotKey = (scene: unknown, shot: unknown): string | null => {
  const num = (v: unknown): number | null => {
    if (typeof v === "number") return Number.isInteger(v) && v >= 0 ? v : null;
    if (typeof v === "string" && /^\s*\d+\s*$/.test(v)) return Number(v);
    return null;
  };
  const a = num(scene);
  const b = num(shot);
  return a === null || b === null ? null : `${a}-${b}`;
};

/** Числовое сравнение ключей: "9-6" раньше "10-1"; непонятные ключи в конец. */
export const compareShotKeys = (a: string, b: string): number => {
  const pa = parseKey(a);
  const pb = parseKey(b);
  if (pa && pb) return pa[0] - pb[0] || pa[1] - pb[1];
  if (pa) return -1;
  if (pb) return 1;
  return a.localeCompare(b);
};

/** Стабильная сортировка: уровень, затем шот (числом); исходный порядок сохраняется. */
export const sortFindings = (list: ReportFinding[]): ReportFinding[] =>
  list
    .map((f, i) => ({ f, i }))
    .sort(
      (x, y) =>
        LEVEL_RANK[x.f.level] - LEVEL_RANK[y.f.level] ||
        compareShotKeys(x.f.shot_key ?? x.f.related_shot_keys[0] ?? "", y.f.shot_key ?? y.f.related_shot_keys[0] ?? "") ||
        x.i - y.i,
    )
    .map((x) => x.f);

/** Самый тяжёлый уровень среди ненулевых счётчиков (для цвета значка); null, если замечаний нет. */
export const worstLevel = (counts: Partial<LevelCounts> | null | undefined): ReportLevel | null => {
  for (const level of LEVEL_ORDER) if (toCount(counts?.[level]) > 0) return level;
  return null;
};

/** Есть ли ошибки или предупреждения (сведения не считаются). */
export const hasIssues = (counts: Partial<LevelCounts> | null | undefined): boolean =>
  toCount(counts?.error) + toCount(counts?.warning) > 0;

/** Раскладка по шотам: находка группы попадает в каждый шот группы. */
export const groupFindingsByShot = (list: ReportFinding[]): Record<string, ReportFinding[]> => {
  const result: Record<string, ReportFinding[]> = {};
  for (const f of list) {
    const keys = f.shot_key ? [f.shot_key] : [];
    for (const k of f.related_shot_keys) if (!keys.includes(k)) keys.push(k);
    for (const k of keys) (result[k] ??= []).push(f);
  }
  return result;
};
