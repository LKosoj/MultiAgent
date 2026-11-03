// Чистая логика экрана «Кадры»: фильтры, выбор соседа, перегенерация, слияние страниц.
// Хуки только вызывают эти функции, поэтому всё проверяется vitest без DOM.
import { StudioError } from "./client";
import { compareShotKeys } from "./reports";
import {
  type ShotCard,
  type ShotChanges,
  type ShotForm,
  type ShotPartName,
  type ShotRow,
  type ShotsFacets,
  type ShotsFilters,
  type ShotsListResult,
} from "./shots";
import type { RegeneratePreviewResult, RegenWhat, ReviewStatus } from "./review";
import { formatRubExact, formatRubInt, SHOT_TEXTS, shotErrorText, type ShotTextId } from "./shotsTexts";
import { EMPTY_FILTERS, type ClientPredicate, type ShotsUiFilters } from "./shotsPrefs";
import { classifyStorybookError } from "./uiErrors";

// ---- фильтры ----

// Фильтр «Обзора» превращается в параметры сервера; клиентские (missing_*) дают пустой набор
export const filterFromIntent = (name: string | null | undefined): ShotsFilters => {
  switch (name) {
    case "stale":
      return { stale_only: true };
    case "errors":
      return { state: "error" };
    case "ready":
      return { state: "ready" };
    case "approved":
    case "pending":
    case "redo":
      return { review: name };
    default:
      return {};
  }
};

export const clientPredicate = (name: string | null | undefined): ClientPredicate | null =>
  name === "missing_images" || name === "missing_videos" ? name : null;

export const uiFiltersFromIntent = (name: string | null | undefined): ShotsUiFilters => {
  const p = filterFromIntent(name);
  return {
    ...EMPTY_FILTERS,
    staleOnly: p.stale_only === true,
    state: p.state ?? "",
    review: p.review ?? "",
    predicate: clientPredicate(name),
  };
};

// Параметры shots_list: только непустые
export const listParams = (f: ShotsUiFilters): ShotsFilters => {
  const out: ShotsFilters = {};
  if (f.scene !== null) out.scene = f.scene;
  if (f.review) out.review = f.review;
  if (f.state) out.state = f.state;
  if (f.staleOnly) out.stale_only = true;
  if (f.withFindings) out.with_findings = true;
  const q = f.query.trim();
  if (q) out.query = q;
  return out;
};

export const hasActiveFilters = (f: ShotsUiFilters): boolean =>
  Object.keys(listParams(f)).length > 0 || f.predicate !== null;

export const matchesPredicate = (row: ShotRow, predicate: ClientPredicate | null): boolean => {
  if (!predicate) return true;
  const { start, end, video } = row.parts;
  if (predicate === "missing_images") {
    return !start.exists || (end.state !== "not_applicable" && !end.exists);
  }
  return video.state !== "not_applicable" && !video.exists && row.state !== "generating";
};

export const applyPredicate = (rows: readonly ShotRow[], predicate: ClientPredicate | null): ShotRow[] =>
  predicate ? rows.filter((r) => matchesPredicate(r, predicate)) : [...rows];

// ---- страницы списка ----

export const PAGE_SIZE = 100;

export type ListData = {
  items: ShotRow[];
  total: number;
  facets: ShotsFacets | null;
  baseline: { missing: number; outdated: number };
  stateTitles: Record<string, string>;
  reviewTitles: Record<string, string>;
  reason: string | null;
  revision: string | null;
};

export const EMPTY_LIST: ListData = {
  items: [],
  total: 0,
  facets: null,
  baseline: { missing: 0, outdated: 0 },
  stateTitles: {},
  reviewTitles: {},
  reason: null,
  revision: null,
};

// Страница встаёт на своё место (offset); повторная загрузка той же страницы заменяет её строки
export const mergePages = (items: readonly ShotRow[], incoming: readonly ShotRow[], offset: number, total: number): ShotRow[] => {
  const at = Math.min(Math.max(0, offset), items.length);
  return [...items.slice(0, at), ...incoming, ...items.slice(at + incoming.length)].slice(0, Math.max(0, total));
};

export const applyListPage = (prev: ListData, res: ShotsListResult): ListData => ({
  items: mergePages(prev.items, res.items, res.offset, res.total),
  total: res.total,
  facets: res.facets,
  baseline: res.baseline,
  stateTitles: res.state_titles,
  reviewTitles: res.review_titles,
  reason: res.reason,
  revision: res.revision,
});

export const hasMorePages = (data: Pick<ListData, "items" | "total">): boolean => data.items.length < data.total;

// ---- ревью ----

export const applyReviewStatus = (
  rows: readonly ShotRow[],
  keys: readonly string[],
  status: ReviewStatus,
  titles: Record<string, string>,
): ShotRow[] => {
  const set = new Set(keys);
  return rows.map((r) =>
    set.has(r.shot_key)
      ? { ...r, review: { ...(r.review ?? {}), status, status_title: titles[status] ?? status } }
      : r,
  );
};

// Сосед в списке: в конце остаётся последний, без текущего - первый; пустой список даёт null
export const neighborKey = (keys: readonly string[], current: string | null, delta: number): string | null => {
  if (keys.length === 0) return null;
  const idx = current === null ? -1 : keys.indexOf(current);
  if (idx === -1) return keys[0];
  return keys[Math.min(keys.length - 1, Math.max(0, idx + delta))];
};

// Над кем работают A/R/X и массовые кнопки: отмеченные (в порядке списка), иначе текущий
export const bulkTargetKeys = (order: readonly string[], marked: Iterable<string>, current: string | null): string[] => {
  const set = new Set(marked);
  if (set.size === 0) return current ? [current] : [];
  const inOrder = order.filter((k) => set.has(k));
  const known = new Set(order);
  const rest = [...set].filter((k) => !known.has(k)).sort(compareShotKeys);
  return [...inOrder, ...rest];
};

// ---- карточка ----

export const formFromCard = (card: ShotCard): ShotForm => ({
  start_prompt: card.start?.prompt ?? "",
  end_prompt: card.end?.prompt ?? "",
  video_prompt: card.video.prompt,
  start_negative_prompt: card.start?.negative_prompt ?? "",
  end_negative_prompt: card.end?.negative_prompt ?? "",
  duration_s: card.video.duration_s,
});

// «Оставить мою правку»: мои изменённые поля поверх свежей карточки
export const reapplyDraft = (fresh: ShotCard, mine: ShotChanges): ShotForm => ({ ...formFromCard(fresh), ...mine });

// Файлы болванки лежат по соглашению об именах (сервер отдаёт только признаки наличия)
export const blockoutRel = (scene: number, shot: number, part: ShotPartName): string => {
  const pad = (n: number) => String(Math.trunc(n)).padStart(2, "0");
  const file = part === "start" ? "ref_start.png" : part === "end" ? "ref_end.png" : "blockout_ref.mp4";
  return `93_blockout/scene_${pad(scene)}_shot_${pad(shot)}/${file}`;
};

export const hasAnyBlockout = (row: Pick<ShotRow, "blockout">): boolean =>
  row.blockout.start || row.blockout.end || row.blockout.video;

export const isGenerating = (row: Pick<ShotRow, "state" | "parts">): boolean =>
  row.state === "generating" || Object.values(row.parts).some((p) => p.state === "generating");

// ---- прогон ----

const BUSY_RUN_STATES = ["running", "pause_requested", "paused", "stopping"];
export const isRunBusy = (state: string | null | undefined): boolean => !!state && BUSY_RUN_STATES.includes(state);

// ---- перегенерация ----

// Что пересоздавать по умолчанию: картинки нужны, если часть начала или конца устарела или пуста
export const defaultWhat = (stale: readonly string[], missing: readonly string[]): RegenWhat => {
  const bad = new Set([...stale, ...missing]);
  if (bad.has("start") || bad.has("end")) return "both";
  if (bad.has("video")) return "video";
  return "both";
};

export const partsMissing = (row: Pick<ShotRow, "parts">): ShotPartName[] =>
  (Object.keys(row.parts) as ShotPartName[]).filter((name) => row.parts[name].state === "missing");

export const defaultWhatForRows = (rows: readonly ShotRow[]): RegenWhat =>
  defaultWhat(
    rows.flatMap((r) => r.stale_parts),
    rows.flatMap(partsMissing),
  );

// Сумма в кнопках и окнах: целые рубли, но от 0 до 0,5 показываем точно, чтобы не было «0 ₽»
export const formatShotMoney = (rub: number): string => (rub > 0 && rub < 0.5 ? formatRubExact(rub) : formatRubInt(rub));

export type RegenButton = { label: string; note: ShotTextId | null };

// Подпись кнопки запуска; note - пояснение под ней
export const regenButton = (costRub: number, unknownCost: boolean): RegenButton => {
  if (costRub > 0) {
    return {
      label: SHOT_TEXTS.regen_go_paid.replace("{cost}", formatShotMoney(costRub)),
      note: unknownCost ? "regen_cost_partial" : null,
    };
  }
  if (unknownCost) return { label: SHOT_TEXTS.regen_go_unknown, note: "regen_cost_unknown" };
  return { label: SHOT_TEXTS.regen_go_free, note: null };
};

export type RegenView = {
  button: RegenButton;
  costRub: number;
  unknownCost: boolean;
  canStart: boolean;
  needsAbandon: boolean;
  pendingKeys: string[];
  shotCount: number;
  linkedKeys: string[];
  diskNeeded: number;
  problems: { level: string; message: string }[];
};

export const regenView = (preview: RegeneratePreviewResult): RegenView => {
  const cost = preview.confirm_required.cost_rub;
  const unknown = preview.confirm_required.unknown_cost;
  const pendingKeys = [...new Set(preview.pending_paid_jobs.map((p) => String(p.shot_key ?? "")).filter(Boolean))].sort(compareShotKeys);
  return {
    button: regenButton(cost, unknown),
    costRub: cost,
    unknownCost: unknown,
    canStart: preview.can_start,
    needsAbandon: preview.pending_paid_jobs.length > 0,
    pendingKeys,
    shotCount: preview.expanded_keys.length,
    linkedKeys: preview.linked_keys,
    diskNeeded: preview.disk_needed_bytes > 0 ? preview.disk_needed_bytes : 0,
    problems: preview.problems.map((p) => ({ level: p.level, message: p.message })),
  };
};

// ---- левая колонка и ошибки ----

export const leftColumnWidth = (windowWidth: number): number => (windowWidth < 1100 ? 280 : windowWidth < 1500 ? 360 : 440);

export const errorReason = (err: unknown): string | null =>
  err instanceof StudioError && typeof err.details?.reason === "string" ? err.details.reason : null;

// Понятный текст любой ошибки вызова
export const shotErrorFrom = (err: unknown): string =>
  err instanceof StudioError ? shotErrorText(err.code, errorReason(err), err.message) : classifyStorybookError(err).text;

// ---- отпечатки записей ревью ----

// Отпечаток нужен только при смене статуса одного открытого шота: массовое действие идёт без проверки
export const pickExpectedDigests = (
  keys: readonly string[],
  selected: string | null,
  known: Readonly<Record<string, string>>,
): Record<string, string> | undefined => {
  if (keys.length !== 1 || keys[0] !== selected) return undefined;
  const digest = known[keys[0]];
  return digest ? { [keys[0]]: digest } : undefined;
};

// Какой тост показать после смены статуса: один шот или несколько
export const reviewToastId = (status: ReviewStatus, many: boolean): ShotTextId => {
  const one = { pending: "toast_pending", approved: "toast_approved", redo: "toast_redo", rejected: "toast_rejected" } as const;
  const bulk = { pending: "toast_bulk_pending", approved: "toast_bulk_approved", redo: "toast_bulk_redo", rejected: "toast_bulk_rejected" } as const;
  return many ? bulk[status] : one[status];
};

// ---- блок «устарел» ----

export const partTitle = (part: ShotPartName): string =>
  part === "start" ? SHOT_TEXTS.part_start : part === "end" ? SHOT_TEXTS.part_end : SHOT_TEXTS.part_video;

// Подпись причины; неизвестный код показывается как есть
export const staleReasonText = (code: string): string => {
  const id = `stale_reason_${code}`;
  return id in SHOT_TEXTS ? SHOT_TEXTS[id as ShotTextId] : code;
};

export type StaleView = {
  parts: { part: ShotPartName; reasons: string[] }[];
  // что устареет после перегенерации
  after: ShotPartName[];
  upstream: string[];
};

const PART_ORDER: ShotPartName[] = ["start", "end", "video"];

export const staleView = (
  card: Pick<ShotCard, "stale" | "stale_parts" | "stale_reasons" | "unknown_reasons" | "cascade" | "upstream_stale_keys">,
): StaleView | null => {
  const parts = PART_ORDER.filter((p) => card.stale_parts.includes(p) || (card.stale_reasons[p]?.length ?? 0) > 0).map((part) => ({
    part,
    reasons: [...new Set([...(card.stale_reasons[part] ?? []), ...(card.unknown_reasons[part] ?? [])])].map(staleReasonText),
  }));
  const after = PART_ORDER.filter((p) => card.cascade.stale_after_regeneration.includes(p) && !card.stale_parts.includes(p));
  const upstream = card.upstream_stale_keys;
  if (parts.length === 0 && after.length === 0 && upstream.length === 0) return null;
  return { parts, after, upstream };
};

// ---- числа и даты ----

const MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"];

// «2026-11-03T10:15:30» -> «3 ноября 2026, 10:15»; непонятное остаётся как есть, пустое - тире
export const formatCreatedAt = (iso: string | null | undefined): string => {
  if (!iso) return "—";
  const m = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/.exec(iso);
  if (!m) return iso;
  const month = MONTHS[Number(m[2]) - 1];
  return month ? `${Number(m[3])} ${month} ${m[1]}, ${m[4]}:${m[5]}` : iso;
};

const NBSP_ = " ";

export const formatBytes = (bytes: number): string => {
  const n = Math.abs(bytes);
  if (n < 1024) return `${n}${NBSP_}Б`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1).replace(".", ",")}${NBSP_}КБ`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1).replace(".", ",")}${NBSP_}МБ`;
  return `${(n / 1024 / 1024 / 1024).toFixed(1).replace(".", ",")}${NBSP_}ГБ`;
};

export const signedBytes = (bytes: number): string => (bytes === 0 ? formatBytes(0) : `${bytes > 0 ? "+" : "-"}${formatBytes(bytes)}`);

// Ключи шотов с отправленными оплаченными задачами из ошибки paid_job_pending
export const pendingKeysFromError = (err: unknown): string[] => {
  const keys = err instanceof StudioError ? err.details?.keys : null;
  return Array.isArray(keys) ? keys.filter((k): k is string => typeof k === "string").sort(compareShotKeys) : [];
};

// ---- просмотр картинок ----

export type ViewerImage = { rel: string; title: string };

// Картинки шота для просмотра ←/→: начало, конец, затем болванка
export const viewerImages = (card: Pick<ShotCard, "scene" | "shot" | "parts" | "blockout">): ViewerImage[] => {
  const out: ViewerImage[] = [];
  for (const part of ["start", "end"] as const) {
    const p = card.parts[part];
    if (p.exists && p.path) out.push({ rel: p.path, title: partTitle(part) });
  }
  for (const part of ["start", "end"] as const) {
    if (card.blockout[part]) out.push({ rel: blockoutRel(card.scene, card.shot, part), title: `${SHOT_TEXTS.part_blockout}: ${partTitle(part)}` });
  }
  return out;
};

// Соседняя картинка; на краях остаётся на месте
export const stepIndex = (index: number, length: number, delta: number): number =>
  length === 0 ? 0 : Math.min(length - 1, Math.max(0, index + delta));
