// Чистая логика экрана «Фильм»: раскладка таймлайна, линейка, масштаб, состояние экрана, переход состояния прогона.
// Здесь только секунды и текст: пиксели считает компонент по масштабу. Те же правила держит Tk (golden_film_layout.json).
import {
  cueAt,
  formatTimecode,
  type SubtitleCue,
  type TimelineClip,
  type TimelineFinalVideo,
  type TimelineResult,
  type TimelineTrack,
} from "./media";
import { FILM_TEXTS, filmFmt, type FilmTextId } from "./filmTexts";
import { isRunBusy, formatBytes, formatCreatedAt } from "./shotsView";
import type { RunState } from "./status";

export { cueAt };

export type TrackId = "shots" | "narration" | "music" | "sfx" | "subtitles";
export const TRACK_ORDER: readonly TrackId[] = ["shots", "narration", "music", "sfx", "subtitles"];
export const TRACK_TITLES: Record<TrackId, string> = {
  shots: FILM_TEXTS.track_shots,
  narration: FILM_TEXTS.track_narration,
  music: FILM_TEXTS.track_music,
  sfx: FILM_TEXTS.track_sfx,
  subtitles: FILM_TEXTS.track_subtitles,
};

export type BlockTone = "shot" | "unit" | "covered" | "clip" | "missing" | "cue";

export type LayoutBlock = {
  track: TrackId;
  start_s: number;
  end_s: number;
  label: string;
  tone: BlockTone;
  key: string | null;
  tooltip: string;
  approximate: boolean;
  warning: boolean;
  nested: boolean;
};

export type FilmLayout = { blocks: LayoutBlock[]; duration_s: number; out_of_cut: number };

const round3 = (n: number): number => Math.round(n * 1000) / 1000;

const tc = (a: number, b: number): string => `${formatTimecode(a)} – ${formatTimecode(b)}`;

export const filmDuration = (timeline: Pick<TimelineResult, "duration_s" | "final">, cues: readonly SubtitleCue[] = []): number => {
  const fromCut = timeline.duration_s;
  if (fromCut !== null && fromCut > 0) return fromCut;
  const fromFile = timeline.final.video?.duration_s ?? null;
  if (fromFile !== null && fromFile > 0) return fromFile;
  return cues.reduce((m, c) => Math.max(m, c.end_s), 0);
};

const shotBlocks = (timeline: TimelineResult): { blocks: LayoutBlock[]; outOfCut: number } => {
  const warningsByKey = new Map<string, string[]>();
  for (const w of timeline.warnings) {
    if (!w.key) continue;
    const list = warningsByKey.get(w.key) ?? [];
    list.push(w.message);
    warningsByKey.set(w.key, list);
  }
  const blocks: LayoutBlock[] = [];
  let outOfCut = 0;
  for (const s of timeline.shots) {
    if (!s.in_cut || s.start_s === null || s.end_s === null) {
      outOfCut += 1;
      continue;
    }
    const label = `Шот ${s.scene}-${s.shot}`;
    const nested = !!s.covered_by || s.unit_span;
    const warning = s.warnings.length > 0;
    const lines = [`${label} · ${tc(s.start_s, s.end_s)}`];
    if (s.approximate) lines.push(FILM_TEXTS.approximate);
    // сырые коды предупреждений не показываем: только сообщения сервера
    const texts = warningsByKey.get(s.key) ?? [];
    if (texts.length > 0) lines.push(`${FILM_TEXTS.warning_word}: ${texts.join("; ")}`);
    blocks.push({
      track: "shots",
      start_s: round3(s.start_s),
      end_s: round3(s.end_s),
      label,
      tone: s.unit_span ? "unit" : s.covered_by ? "covered" : "shot",
      key: s.key,
      tooltip: lines.join("\n"),
      approximate: s.approximate,
      warning,
      nested,
    });
  }
  return { blocks, outOfCut };
};

const PLACEMENT_TEXT: Record<TimelineClip["placement"], FilmTextId> = {
  manifest: "src_manifest",
  scene: "src_scene",
  film: "src_film",
};

export const placementText = (placement: TimelineClip["placement"]): string => FILM_TEXTS[PLACEMENT_TEXT[placement]];

const clipBlocks = (track: TrackId, data: TimelineTrack, filmEnd: number): LayoutBlock[] =>
  data.clips.map((c) => {
    const end = c.end_s ?? filmEnd;
    const missing = !c.exists;
    const extras = [c.loop ? FILM_TEXTS.loop_word : "", missing ? FILM_TEXTS.no_file : ""].filter(Boolean);
    const label = extras.length ? `${c.label} (${extras.join(", ")})` : c.label;
    return {
      track,
      start_s: round3(c.start_s),
      end_s: round3(Math.max(end, c.start_s)),
      label,
      tone: missing ? "missing" : "clip",
      key: c.id,
      tooltip: `${label} · ${tc(c.start_s, end)} · ${placementText(c.placement)}`,
      approximate: false,
      warning: false,
      nested: false,
    };
  });

const cueBlocks = (cues: readonly SubtitleCue[]): LayoutBlock[] =>
  cues.map((c) => ({
    track: "subtitles" as const,
    start_s: round3(c.start_s),
    end_s: round3(c.end_s),
    label: c.text,
    tone: "cue" as const,
    key: String(c.index),
    tooltip: `${tc(c.start_s, c.end_s)}\n${c.text}`,
    approximate: false,
    warning: false,
    nested: false,
  }));

export const buildLayout = (timeline: TimelineResult, cues: readonly SubtitleCue[] = []): FilmLayout => {
  const duration = filmDuration(timeline, cues);
  const shots = shotBlocks(timeline);
  const blocks = [
    ...shots.blocks,
    ...clipBlocks("narration", timeline.tracks.narration, duration),
    ...clipBlocks("music", timeline.tracks.music, duration),
    ...clipBlocks("sfx", timeline.tracks.sfx, duration),
    ...cueBlocks(cues),
  ];
  const end = blocks.reduce((m, b) => Math.max(m, b.end_s), 0);
  return { blocks, duration_s: round3(Math.max(duration, end)), out_of_cut: shots.outOfCut };
};

// Подпись в блоке прячем, если блок уже порога (остаётся подсказка)
export const LABEL_MIN_PX = 28;
export const labelVisible = (widthPx: number): boolean => widthPx >= LABEL_MIN_PX;

// Мелкие блоки без места под текст сливаются в одну полосу, пока они ближе порога друг к другу
export const mergeNarrow = (blocks: readonly LayoutBlock[], pxPerS: number, minPx = LABEL_MIN_PX): LayoutBlock[] => {
  const out: LayoutBlock[] = [];
  for (const b of [...blocks].sort((x, y) => x.start_s - y.start_s)) {
    const narrow = (b.end_s - b.start_s) * pxPerS < minPx;
    const prev = out[out.length - 1];
    const prevNarrow = prev && (prev.end_s - prev.start_s) * pxPerS < minPx;
    if (narrow && prev && prevNarrow && (b.start_s - prev.end_s) * pxPerS < minPx) {
      prev.end_s = Math.max(prev.end_s, b.end_s);
      prev.label = "";
      prev.key = null;
      prev.tooltip = tc(prev.start_s, prev.end_s);
    } else {
      out.push({ ...b });
    }
  }
  return out;
};

// Блок под курсором времени t; вложенный (шот внутри объединённого видео) точнее вмещающего
export const blockAt = (blocks: readonly LayoutBlock[], track: TrackId, t: number): LayoutBlock | null => {
  const hit = blocks.filter((b) => b.track === track && b.start_s <= t && t < b.end_s);
  if (hit.length === 0) return null;
  return hit.sort((a, b) => Number(b.nested) - Number(a.nested) || b.start_s - a.start_s)[0];
};

// ---- линейка и масштаб ----

export const TICK_STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300] as const;
export const TICK_MIN_PX = 60;
export const MAX_SCALE_PX_PER_S = 64;
export const MAX_TOTAL_WIDTH_PX = 30000;
export const ZOOM_FACTOR = 1.5;

export const tickStep = (pxPerS: number): number =>
  TICK_STEPS.find((step) => step * pxPerS >= TICK_MIN_PX) ?? TICK_STEPS[TICK_STEPS.length - 1];

export const fitScale = (viewWidthPx: number, durationS: number): number =>
  durationS > 0 && viewWidthPx > 0 ? viewWidthPx / durationS : 1;

export const clampScale = (pxPerS: number, fit: number, durationS: number): number => {
  const hi = Math.max(fit, Math.min(MAX_SCALE_PX_PER_S, durationS > 0 ? MAX_TOTAL_WIDTH_PX / durationS : MAX_SCALE_PX_PER_S));
  return Math.min(hi, Math.max(fit, pxPerS));
};

export const zoomIn = (pxPerS: number, fit: number, durationS: number): number => clampScale(pxPerS * ZOOM_FACTOR, fit, durationS);
export const zoomOut = (pxPerS: number, fit: number, durationS: number): number => clampScale(pxPerS / ZOOM_FACTOR, fit, durationS);

export const rulerTicks = (durationS: number, pxPerS: number): { t: number; label: string }[] => {
  const step = tickStep(pxPerS);
  const out: { t: number; label: string }[] = [];
  for (let t = 0; t <= durationS; t += step) out.push({ t, label: formatTimecode(t) });
  return out;
};

// ---- состояние экрана ----

export type FilmScreenState = "loading" | "error" | "empty" | "partial" | "ready";

export const hasFilmFile = (timeline: Pick<TimelineResult, "final"> | null): boolean =>
  !!timeline && (!!timeline.final.video?.exists || !!timeline.final.photo_video?.exists);

const hasAudioClips = (timeline: Pick<TimelineResult, "tracks">): boolean =>
  timeline.tracks.narration.clips.length + timeline.tracks.music.clips.length + timeline.tracks.sfx.clips.length > 0;

export const resolveFilmState = (a: { timeline: TimelineResult | null; error: unknown }): FilmScreenState => {
  if (!a.timeline) return a.error ? "error" : "loading";
  if (hasFilmFile(a.timeline)) return "ready";
  return hasAudioClips(a.timeline) ? "partial" : "empty";
};

// Переход состояния прогона: перечитать экран, когда прогон закончился; тост - только после успешного конца
export type RunTransition = { reload: boolean; toast: "done" | null };

export const runTransition = (prev: RunState | null | undefined, next: RunState | null | undefined): RunTransition => {
  const wasBusy = isRunBusy(prev);
  const nowBusy = isRunBusy(next);
  if (!wasBusy || nowBusy) return { reload: false, toast: null };
  return { reload: true, toast: next === "completed" ? "done" : null };
};

export const MONTAGE_STEP_ID = "montage_assembler";
export const isMontageRunning = (runState: RunState | null | undefined, steps: readonly string[] | null | undefined): boolean =>
  isRunBusy(runState) && !!steps && steps.includes(MONTAGE_STEP_ID);

export type FilmBanner = { id: "building" | "stale" | "approximate"; tone: "warn" | "info"; text: string };

export const isFilmStale = (timeline: Pick<TimelineResult, "final">): boolean =>
  timeline.final.stale_hint || !!timeline.final.video?.older_than_shots;

export const filmBanners = (timeline: TimelineResult | null, montageRunning: boolean): FilmBanner[] => {
  const out: FilmBanner[] = [];
  if (montageRunning) out.push({ id: "building", tone: "warn", text: FILM_TEXTS.building });
  if (timeline && isFilmStale(timeline)) out.push({ id: "stale", tone: "warn", text: FILM_TEXTS.stale });
  if (timeline && timeline.source !== null && timeline.source !== "edit_decisions") {
    out.push({ id: "approximate", tone: "info", text: FILM_TEXTS.approximate });
  }
  return out;
};

// ---- правая колонка ----

export type MusicRow = { id: string; name: string; scenes: string; source: string; path: string; exists: boolean };

// Одна строка на композицию: сцены собираются вместе
export const musicRows = (clips: readonly TimelineClip[]): MusicRow[] => {
  const groups = new Map<string, TimelineClip[]>();
  for (const c of clips) {
    const id = c.music_id ?? c.id;
    groups.set(id, [...(groups.get(id) ?? []), c]);
  }
  return [...groups.entries()].map(([id, list]) => {
    const whole = list.some((c) => c.placement === "film" || c.scene === null);
    const scenes = [...new Set(list.map((c) => c.scene).filter((s): s is number => s !== null))].sort((a, b) => a - b);
    const first = list[0];
    return {
      id,
      name: first.label,
      scenes: whole ? FILM_TEXTS.music_all_scenes : scenes.join(", "),
      source: placementText(first.placement),
      path: first.path,
      exists: list.some((c) => c.exists),
    };
  });
};

export type FileInfoRow = { id: "size" | "duration" | "resolution" | "created"; label: string; value: string };

export const finalFileRows = (video: TimelineFinalVideo | null, resolution: { width: number; height: number } | null): FileInfoRow[] => [
  { id: "size", label: FILM_TEXTS.file_size, value: video ? formatBytes(video.size) : "—" },
  { id: "duration", label: FILM_TEXTS.file_duration, value: video?.duration_s != null ? formatTimecode(video.duration_s) : "—" },
  { id: "resolution", label: FILM_TEXTS.file_resolution, value: resolution && resolution.width > 0 ? `${resolution.width}×${resolution.height}` : "—" },
  { id: "created", label: FILM_TEXTS.file_created, value: video ? formatCreatedAt(video.modified) : "—" },
];

export type FilmSource = "film" | "photo";

export const sourceVideo = (timeline: Pick<TimelineResult, "final">, source: FilmSource): TimelineFinalVideo | null => {
  const v = source === "film" ? timeline.final.video : timeline.final.photo_video;
  return v && v.exists ? v : null;
};

// Какой источник показывать первым: фильм, а если его нет - фотомонтаж
export const defaultSource = (timeline: Pick<TimelineResult, "final">): FilmSource =>
  sourceVideo(timeline, "film") ? "film" : sourceVideo(timeline, "photo") ? "photo" : "film";

export const subtitlesTruncatedText = (truncated: boolean): string | null => (truncated ? FILM_TEXTS.subs_truncated : null);

export const outOfCutText = (n: number): string | null => (n > 0 ? filmFmt("out_of_cut", { n }) : null);

// Флаг «нет прав»: отказ его ставит, любая успешная загрузка экрана сбрасывает
export const forbiddenAfter = (prev: boolean, event: "forbidden" | "load_ok"): boolean => (event === "forbidden" ? true : event === "load_ok" ? false : prev);
