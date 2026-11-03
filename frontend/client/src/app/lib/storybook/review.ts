// Обёртки возможностей ревью, версий и перегенерации шотов (Ф7) и чистые помощники.
import { StudioError, type StorybookClient } from "./client";

export type ReviewStatus = "pending" | "approved" | "redo" | "rejected";
export type ReviewCounts = Record<ReviewStatus, number>;
export type ShotPart = "start" | "end" | "video";

export type ReviewEntry = {
  status: ReviewStatus;
  status_title: string;
  note: string;
  selected_take: string | null;
  status_by: string | null;
  status_changed_at: string | null;
  entry_digest: string;
  history?: Record<string, unknown>[];
};

export type ReviewGetResult = {
  digest: string;
  counts: ReviewCounts;
  titles: Record<ReviewStatus, string>;
  corrupt: boolean;
  shots: Record<string, ReviewEntry>;
  key?: string;
};

// Часть шота в версии (version_view.parts) и в «текущем» (current_view.parts: без sha256).
export type VersionPartView = {
  path: string | null;
  size: number | null;
  exists: boolean;
  unit_keys: string[] | null;
  sha256?: string | null;
};
export type PromptTexts = Partial<Record<"start" | "end" | "video" | "negative_start" | "negative_end", string>>;
export type VersionView = {
  id: string;
  number: number;
  created_at: string | null;
  reason: string | null;
  reason_title: string | null;
  mode: string | null;
  surface: string | null;
  parts: Partial<Record<ShotPart, VersionPartView>>;
  prompts: PromptTexts;
  prompts_trusted: boolean;
  matches_current: Record<ShotPart, boolean | null>;
  cost_rub: number | null;
  provider: string | null;
  model: string | null;
  is_selected: boolean;
  restored_from: string | null;
};
// Текущие файлы шота (current_view). tokens нужны для expected_token при возврате версии.
export type CurrentView = {
  parts: Record<ShotPart, VersionPartView>;
  tokens: Record<ShotPart, string | null>;
  prompts: PromptTexts;
};
// Сторона сравнения: версия или «текущая».
export type CompareSide =
  | VersionView
  | { id: "current"; parts: CurrentView["parts"]; tokens: CurrentView["tokens"]; prompts: PromptTexts; prompts_trusted: true };

export type VersionsListResult = {
  key?: string;
  versions?: VersionView[];
  current?: CurrentView;
  selected?: string | null;
  usage: Record<string, unknown>;
  shots?: unknown;
  recovery?: Record<string, unknown>;
};

export type VersionsCompareResult = {
  key: string;
  left: CompareSide;
  right: CompareSide;
  diff: {
    prompt_changed: Record<ShotPart, boolean>;
    size_delta: Partial<Record<ShotPart, number>>;
    parts_only_left: ShotPart[];
    parts_only_right: ShotPart[];
  };
};

export type RestoreWarning = { code: string; message: string };
export type VersionsRestoreResult = {
  restored: ShotPart[];
  snapshot_version_id: string | null;
  review_reset: string[];
  freshness: Record<string, unknown>;
  warnings: RestoreWarning[];
};

export type RegenWhat = "images" | "video" | "both" | "music" | "film";
export type PlanProblem = { level: "error" | "warning" | "info"; code: string; message: string };

export type RegeneratePreviewResult = {
  plan_digest: string;
  can_start: boolean;
  problems: PlanProblem[];
  expanded_keys: string[];
  linked_keys: string[];
  steps: string[];
  will_snapshot: unknown;
  disk_needed_bytes: number;
  pending_paid_jobs: Record<string, unknown>[];
  estimate: Record<string, unknown> | null;
  confirm_required: { cost_rub: number; unknown_cost: boolean };
  run_plan: Record<string, unknown>;
};

type ProjectArg = { project_id: string };

export const reviewGet = (c: StorybookClient, p: ProjectArg & { key?: string }) =>
  c.call<ReviewGetResult>("review_get", p);

export type ReviewSetStatusResult = {
  updated: string[];
  unchanged: string[];
  counts: ReviewCounts;
  digest: string;
  entries: Record<string, string>;
};

export const reviewSetStatus = (
  c: StorybookClient,
  p: ProjectArg & {
    keys: string[];
    status: ReviewStatus;
    expected_digests?: Record<string, string>;
    note?: string;
    surface?: string;
  },
) => c.call<ReviewSetStatusResult>("review_set_status", p);

export const reviewSetNote = (
  c: StorybookClient,
  p: ProjectArg & { key: string; note: string; expected_digest: string; surface?: string },
) => c.call<{ entry: ReviewEntry; entry_digest: string; digest: string }>("review_set_note", p);

export const versionsList = (c: StorybookClient, p: ProjectArg & { key?: string; kind?: "shot" | "music" | "film" }) =>
  c.call<VersionsListResult>("versions_list", p);

export const versionsCompare = (c: StorybookClient, p: ProjectArg & { key: string; left: string; right: string }) =>
  c.call<VersionsCompareResult>("versions_compare", p);

export const versionsRestore = (
  c: StorybookClient,
  p: ProjectArg & { key: string; version_id: string; parts?: ShotPart[]; expected_token?: unknown; surface?: string },
) => c.call<VersionsRestoreResult>("versions_restore", p);

export const regeneratePreview = (
  c: StorybookClient,
  p: ProjectArg & { what: RegenWhat; shot_keys?: string[]; rebuild_film?: boolean; image_parts?: ("start" | "end")[] },
) => c.call<RegeneratePreviewResult>("regenerate_preview", p);

export type RegenerateStartPayload = ProjectArg & {
  what: RegenWhat;
  launcher: "backend" | "local";
  expected_plan_digest: string;
  shot_keys?: string[];
  rebuild_film?: boolean;
  image_parts?: ("start" | "end")[];
  confirm_cost_rub?: number;
  confirm_unknown_cost?: boolean;
  abandon_pending_jobs?: boolean;
};

export const regenerateStart = (c: StorybookClient, p: RegenerateStartPayload) =>
  c.call<{ run_id: string | null; workflow_id: string | null; regen_pending: boolean }>("regenerate_start", p);

// --- чистые помощники ---

const ZERO: ReviewCounts = { pending: 0, approved: 0, redo: 0, rejected: 0 };

export const reviewCountsFromEntries = (
  keys: string[],
  entries: Record<string, { status?: string } | undefined>,
): ReviewCounts => {
  const out = { ...ZERO };
  for (const key of keys) {
    const s = entries[key]?.status;
    out[s === "approved" || s === "redo" || s === "rejected" ? s : "pending"] += 1;
  }
  return out;
};

// Клавиши быстрого ревью: A одобрить, R переделать, X отклонить; повтор той же клавиши возвращает «Ожидает».
const KEY_STATUS: Record<string, ReviewStatus> = { a: "approved", r: "redo", x: "rejected" };

export const nextStatusOnKey = (current: ReviewStatus, key: string): ReviewStatus | null => {
  const target = KEY_STATUS[key.toLowerCase()];
  if (!target) return null;
  return current === target ? "pending" : target;
};

export const needsCostConfirm = (preview: Pick<RegeneratePreviewResult, "confirm_required">): boolean =>
  preview.confirm_required.cost_rub > 0 || preview.confirm_required.unknown_cost;

export const startPayloadFromPreview = (
  base: Omit<RegenerateStartPayload, "expected_plan_digest" | "confirm_cost_rub" | "confirm_unknown_cost">,
  preview: RegeneratePreviewResult,
  confirmed: boolean,
): RegenerateStartPayload => {
  const payload: RegenerateStartPayload = { ...base, expected_plan_digest: preview.plan_digest };
  if (confirmed && preview.confirm_required.cost_rub > 0) payload.confirm_cost_rub = preview.confirm_required.cost_rub;
  if (confirmed && preview.confirm_required.unknown_cost) payload.confirm_unknown_cost = true;
  return payload;
};

export const versionLabel = (v: { id: string; number?: number }): string =>
  v.id === "current" ? "Текущая" : `Версия ${v.number ?? ""}`.trim();

export const isConflict = (err: unknown, reason?: string): boolean =>
  err instanceof StudioError && err.code === "conflict" && (reason === undefined || err.details?.reason === reason);
