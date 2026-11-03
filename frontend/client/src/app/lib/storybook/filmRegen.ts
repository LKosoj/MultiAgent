// Перегенерация музыки и пересборка монтажа: предпросмотр, повторная проверка плана перед стартом, версии.
import type { StorybookClient } from "./client";
import { FILM_TEXTS, filmFmt } from "./filmTexts";
import {
  isConflict,
  needsCostConfirm,
  regeneratePreview,
  regenerateStart,
  startPayloadFromPreview,
  versionsList,
  type RegeneratePreviewResult,
} from "./review";
import { formatShotMoney, shotErrorFrom } from "./shotsView";

export type FilmRegenWhat = "music" | "film";

export type RegenOutcome =
  | { kind: "started" }
  | { kind: "blocked"; preview: RegeneratePreviewResult }
  | { kind: "needs_confirm"; preview: RegeneratePreviewResult }
  | { kind: "changed"; reason: "plan_changed" | "cost_changed"; preview: RegeneratePreviewResult };

export const previewRegen = (client: StorybookClient, projectId: string, what: FilmRegenWhat) =>
  regeneratePreview(client, { project_id: projectId, what });

const startFrom = async (
  client: StorybookClient,
  projectId: string,
  what: FilmRegenWhat,
  preview: RegeneratePreviewResult,
  confirmed: boolean,
): Promise<void> => {
  await regenerateStart(client, startPayloadFromPreview({ project_id: projectId, what, launcher: "backend" }, preview, confirmed));
};

// Музыка: после подтверждения план строится заново; если отпечаток или сумма изменились, запуска нет
export const startAfterConfirm = async (
  client: StorybookClient,
  projectId: string,
  what: FilmRegenWhat,
  shown: RegeneratePreviewResult,
): Promise<RegenOutcome> => {
  const fresh = await previewRegen(client, projectId, what);
  if (!fresh.can_start) return { kind: "blocked", preview: fresh };
  if (fresh.plan_digest !== shown.plan_digest) return { kind: "changed", reason: "plan_changed", preview: fresh };
  if (fresh.confirm_required.cost_rub !== shown.confirm_required.cost_rub) {
    return { kind: "changed", reason: "cost_changed", preview: fresh };
  }
  try {
    await startFrom(client, projectId, what, fresh, true);
  } catch (err) {
    if (isConflict(err, "plan_changed") || isConflict(err, "cost_changed")) {
      const reason = isConflict(err, "cost_changed") ? "cost_changed" : "plan_changed";
      return { kind: "changed", reason, preview: await previewRegen(client, projectId, what) };
    }
    throw err;
  }
  return { kind: "started" };
};

// Монтаж: платного нет, окна нет. Если вдруг понадобилось подтверждение суммы, молча не запускаем
export const rebuildMontage = async (client: StorybookClient, projectId: string): Promise<RegenOutcome> => {
  const preview = await previewRegen(client, projectId, "film");
  if (!preview.can_start) return { kind: "blocked", preview };
  if (needsCostConfirm(preview)) return { kind: "needs_confirm", preview };
  try {
    await startFrom(client, projectId, "film", preview, false);
  } catch (err) {
    if (isConflict(err, "plan_changed")) return { kind: "changed", reason: "plan_changed", preview: await previewRegen(client, projectId, "film") };
    throw err;
  }
  return { kind: "started" };
};

export const filmErrorText = (err: unknown): string => shotErrorFrom(err);

export const changedText = (reason: "plan_changed" | "cost_changed"): string =>
  reason === "plan_changed" ? FILM_TEXTS.plan_changed : FILM_TEXTS.cost_changed;

// Окно «Перегенерировать музыку?»
export type MusicDialogView = {
  title: string;
  body: string;
  goLabel: string;
  canStart: boolean;
  problems: { level: string; message: string }[];
};

export const musicDialogView = (preview: RegeneratePreviewResult): MusicDialogView => {
  const cost = preview.confirm_required.cost_rub;
  const unknown = preview.confirm_required.unknown_cost;
  const money = formatShotMoney(cost);
  const body = cost > 0 ? filmFmt(unknown ? "music_body_partial" : "music_body", { cost: money }) : unknown ? FILM_TEXTS.music_body_unknown : FILM_TEXTS.music_body_free;
  const goLabel = cost > 0 ? filmFmt("music_go_paid", { cost: money }) : unknown ? FILM_TEXTS.music_go_unknown : FILM_TEXTS.music_go_free;
  return {
    title: FILM_TEXTS.music_title,
    body,
    goLabel,
    canStart: preview.can_start,
    problems: preview.problems.map((p) => ({ level: p.level, message: p.message })),
  };
};

// ---- прежние версии музыки и фильма (только список) ----

export type AreaVersionFile = { name: string; size: number | null; path: string };
export type AreaVersion = {
  id: string;
  number: number;
  created_at: string | null;
  reason: string | null;
  reason_title: string | null;
  surface: string | null;
  files: AreaVersionFile[];
};

// свежие версии сверху
export const newestFirst = (list: readonly AreaVersion[]): AreaVersion[] => [...list].sort((a, b) => b.number - a.number);

const isAreaVersion = (v: unknown): v is AreaVersion => {
  if (typeof v !== "object" || v === null) return false;
  const r = v as Record<string, unknown>;
  return typeof r.id === "string" && typeof r.number === "number" && Array.isArray(r.files);
};

export const loadAreaVersions = async (client: StorybookClient, projectId: string, kind: "music" | "film"): Promise<AreaVersion[]> => {
  const res = await versionsList(client, { project_id: projectId, kind });
  // ответ общий для шотов и областей: форму проверяем, а не приводим
  const raw: readonly unknown[] = Array.isArray(res.versions) ? res.versions : [];
  return newestFirst(raw.filter(isAreaVersion));
};
