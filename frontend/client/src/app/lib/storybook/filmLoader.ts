// Загрузка данных экрана «Фильм»: таймлайн (с повтором, пока студия досчитывает длительности) и субтитры параллельно.
import type { StorybookClient } from "./client";
import { mediaSubtitles, mediaTimeline, type SubtitlesResult, type TimelineResult } from "./media";

export type FilmData = {
  timeline: TimelineResult;
  subtitles: SubtitlesResult | null;
  subtitlesFailed: boolean;
  // повторное чтение не удалось: показываем последние удачные данные и предупреждение
  refreshFailed?: boolean;
};

export type FilmLoaderOptions = {
  client: StorybookClient;
  projectId: string;
  signal?: AbortSignal;
  pauseMs?: number; // пауза между повторами, по умолчанию 1500
  maxRepeats?: number; // сколько раз повторять, пока durations_pending > 0, по умолчанию 10
  probeLimit?: number;
  sleep?: (ms: number) => Promise<void>;
  // первый и каждый следующий ответ: экран показывает данные, не дожидаясь конца повторов
  onUpdate?: (data: FilmData) => void;
};

export const FILM_PROBE_LIMIT = 20;
export const FILM_REPEAT_PAUSE_MS = 1500;
export const FILM_MAX_REPEATS = 10;

const defaultSleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

export class FilmAborted extends Error {
  constructor() {
    super("Загрузка фильма прервана");
    this.name = "FilmAborted";
  }
}

export const createFilmLoader = (o: FilmLoaderOptions) => {
  const pauseMs = o.pauseMs ?? FILM_REPEAT_PAUSE_MS;
  const maxRepeats = o.maxRepeats ?? FILM_MAX_REPEATS;
  const sleep = o.sleep ?? defaultSleep;
  const aborted = () => !!o.signal?.aborted;

  const load = async (): Promise<FilmData> => {
    if (aborted()) throw new FilmAborted();
    // субтитры не должны ронять экран: без них фильм смотреть можно
    const subsPromise = mediaSubtitles(o.client, { project_id: o.projectId, source: "auto" }).then(
      (value) => ({ value, failed: false }),
      () => ({ value: null as SubtitlesResult | null, failed: true }),
    );
    let data: FilmData | null = null;
    for (let attempt = 0; attempt <= maxRepeats; attempt += 1) {
      let timeline: TimelineResult;
      try {
        timeline = await mediaTimeline(o.client, { project_id: o.projectId, probe_limit: o.probeLimit ?? FILM_PROBE_LIMIT });
      } catch (err) {
        // первый запрос упал - это ошибка экрана; упавший повтор оставляет прежние данные
        if (!data || aborted()) throw err;
        data = { ...data, refreshFailed: true };
        o.onUpdate?.(data);
        return data;
      }
      if (aborted()) {
        if (data) return data;
        throw new FilmAborted();
      }
      const subs = await subsPromise;
      data = { timeline, subtitles: subs.value, subtitlesFailed: subs.failed };
      o.onUpdate?.(data);
      if (timeline.durations_pending === 0 || attempt === maxRepeats) break;
      await sleep(pauseMs);
      if (aborted()) return data;
    }
    return data as FilmData;
  };

  return { load };
};

export type FilmLoader = ReturnType<typeof createFilmLoader>;
