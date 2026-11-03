// Обёртки возможностей модуля media (инвентарь, таймлайн, субтитры) и чистые помощники для экрана «Фильм».
// Поля ответов остаются в snake_case, как их отдаёт studio.
import type { StorybookClient, ThumbSize } from "./client";

export type MediaKind = "image" | "video" | "audio" | "subtitles";

export type MediaItem = {
  path: string; // POSIX, относительно папки проекта
  name: string;
  kind: MediaKind;
  type: string;
  type_title: string;
  category: string;
  category_title: string;
  key: string | null;
  keys: string[];
  scene: number | null;
  shot: number | null;
  page: number | null;
  phase: string | null;
  music_id: string | null;
  size: number;
  modified: string;
  modified_ts: number;
  duration_s: number | null;
  servable: boolean; // false: браузер файл не откроет (bmp, avi, ...)
};

export type MediaSortBy = "scene_shot" | "name" | "modified" | "size" | "type" | "duration";

export type MediaInventoryParams = {
  project_id: string;
  types?: string[];
  categories?: string[];
  kinds?: MediaKind[];
  scene?: number;
  key?: string;
  search?: string;
  include_blockout_frames?: boolean;
  sort_by?: MediaSortBy;
  descending?: boolean;
  offset?: number;
  limit?: number;
  probe_limit?: number;
};

export type MediaLabels = { types: Record<string, string>; categories: Record<string, string> };

export type MediaInventoryResult = {
  project_id: string;
  total: number;
  matched: number;
  offset: number;
  limit: number;
  durations_pending: number;
  counts: { types: Record<string, number>; categories: Record<string, number>; kinds: Record<string, number> };
  labels: MediaLabels;
  items: MediaItem[];
};

export type TimelineWarning = { code: string; message: string; key?: string; path?: string; name?: string };

export type TimelineShot = {
  key: string;
  scene: number;
  shot: number;
  index: number | null;
  start_s: number | null;
  end_s: number | null;
  duration_s: number | null;
  planned_duration_s: number | null;
  actual_duration_s: number | null;
  in_cut: boolean;
  approximate: boolean;
  covered_by: string | null;
  unit_span: boolean;
  unit: { keys: string[]; path: string } | null;
  video: string | null;
  start_image: string | null;
  end_image: string | null;
  thumb_path: string | null;
  warnings: string[];
};

export type TimelineScene = {
  scene: number;
  start_s: number;
  end_s: number;
  duration_s: number;
  shot_keys: string[];
};

export type TimelineClip = {
  id: string;
  role: "narration" | "music" | "sfx";
  label: string;
  path: string;
  exists: boolean;
  scene: number | null;
  music_id: string | null;
  start_s: number;
  end_s: number | null;
  duration_s: number | null;
  source_duration_s: number | null;
  loop: boolean | null;
  placement: "manifest" | "scene" | "film";
};

export type TimelineTrack = {
  status: "present" | "missing" | "unavailable";
  note: string;
  source?: "plan" | "manifest" | "none";
  clips: TimelineClip[];
};

export type TimelineFinalVideo = {
  path: string;
  type: string;
  size: number;
  modified: string;
  duration_s: number | null;
  exists: boolean;
  older_than_shots: boolean;
};

export type TimelineResult = {
  project_id: string;
  source: "edit_decisions" | "shots" | "cue_sheet" | null;
  duration_s: number | null;
  shots: TimelineShot[];
  scenes: TimelineScene[];
  tracks: { narration: TimelineTrack; music: TimelineTrack; sfx: TimelineTrack };
  subtitles: { source: string | null; path: string | null; count: number; duration_s: number | null };
  final: {
    video: TimelineFinalVideo | null;
    photo_video: TimelineFinalVideo | null;
    render_report_path: string | null;
    final_review_path: string | null;
    review_passed: boolean | null;
    stale_hint: boolean;
  };
  missing: string[];
  warnings: TimelineWarning[];
  durations_pending: number;
  labels: MediaLabels;
};

export type SubtitleCue = {
  index: number;
  source_index: number | null;
  start_s: number;
  end_s: number;
  start_tc: string;
  end_tc: string;
  text: string;
  shot_key: string | null;
};

export type SubtitlesResult = {
  project_id: string;
  source: "final" | "planned" | null;
  path: string | null;
  exists: boolean;
  count: number;
  skipped: number;
  duration_s: number | null;
  truncated: boolean;
  cues: SubtitleCue[];
};

export const mediaInventory = (c: StorybookClient, p: MediaInventoryParams) =>
  c.call<MediaInventoryResult>("media_inventory", p);

export const mediaTimeline = (c: StorybookClient, p: { project_id: string; probe_limit?: number }) =>
  c.call<TimelineResult>("media_timeline", p);

export const mediaSubtitles = (c: StorybookClient, p: { project_id: string; source?: "auto" | "final" | "planned" }) =>
  c.call<SubtitlesResult>("media_subtitles", p);

// --- ссылки ---

export const mediaItemUrl = (c: StorybookClient, projectId: string, item: Pick<MediaItem, "path" | "servable">) =>
  item.servable ? c.mediaUrl(projectId, item.path) : null;

export const mediaThumbUrl = (
  c: StorybookClient,
  projectId: string,
  item: Pick<MediaItem, "path" | "servable">,
  size?: ThumbSize,
) => (item.servable ? c.thumbUrl(projectId, item.path, size) : null);

// --- загрузка всего списка ---

export type LoadAllOptions = {
  signal?: AbortSignal;
  maxRepeats?: number; // сколько раз повторять, пока durations_pending > 0 (по умолчанию 10)
  pauseMs?: number;
};

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

/** Тянет весь список страницами и повторяет, пока студия досчитывает длительности. */
export const loadAllMedia = async (
  c: StorybookClient,
  params: MediaInventoryParams,
  options: LoadAllOptions = {},
): Promise<MediaInventoryResult> => {
  const maxRepeats = options.maxRepeats ?? 10;
  const pauseMs = options.pauseMs ?? 0;
  let last: MediaInventoryResult | null = null;
  for (let attempt = 0; attempt <= maxRepeats; attempt += 1) {
    const items: MediaItem[] = [];
    let pending = 0;
    let offset = params.offset ?? 0;
    let page: MediaInventoryResult | null = null;
    for (;;) {
      if (options.signal?.aborted) {
        if (last) return last;
        throw new Error("Загрузка медиа прервана");
      }
      page = await mediaInventory(c, { ...params, offset });
      items.push(...page.items);
      pending += page.durations_pending;
      offset += page.limit;
      if (page.items.length === 0 || offset >= page.matched) break;
    }
    last = { ...page, items, durations_pending: pending };
    if (pending === 0) break;
    if (attempt < maxRepeats && pauseMs > 0) await sleep(pauseMs);
  }
  return last as MediaInventoryResult;
};

// --- чистые помощники ---

export const formatTimecode = (seconds: number): string => {
  const total = Math.max(0, Math.floor(Number.isFinite(seconds) ? seconds : 0));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const ss = String(s).padStart(2, "0");
  return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
};

/** Шот в момент t. Шоты, накрытые видео-блоком, точнее самого блока. */
export const shotAt = (timeline: Pick<TimelineResult, "shots">, t: number): TimelineShot | null => {
  const shots = timeline.shots
    .filter((s) => s.in_cut && s.start_s !== null && s.end_s !== null)
    .sort((a, b) => (a.start_s as number) - (b.start_s as number) || Number(!!a.covered_by) - Number(!!b.covered_by));
  let lo = 0;
  let hi = shots.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if ((shots[mid].start_s as number) <= t) lo = mid + 1;
    else hi = mid;
  }
  for (let i = lo - 1; i >= 0; i -= 1) {
    const s = shots[i];
    if (t < (s.end_s as number)) return s;
    if (!s.covered_by && !s.unit_span) break;
  }
  return null;
};

export const cueAt = (cues: SubtitleCue[], t: number): SubtitleCue | null => {
  let lo = 0;
  let hi = cues.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (cues[mid].start_s <= t) lo = mid + 1;
    else hi = mid;
  }
  const cue = lo > 0 ? cues[lo - 1] : undefined;
  return cue && t < cue.end_s ? cue : null;
};

export const groupByScene = (items: MediaItem[]): { scene: number | null; items: MediaItem[] }[] => {
  const groups = new Map<number | null, MediaItem[]>();
  for (const item of items) {
    const list = groups.get(item.scene);
    if (list) list.push(item);
    else groups.set(item.scene, [item]);
  }
  return [...groups.entries()]
    .sort(([a], [b]) => (a === null ? 1 : b === null ? -1 : a - b))
    .map(([scene, list]) => ({ scene, items: list }));
};
