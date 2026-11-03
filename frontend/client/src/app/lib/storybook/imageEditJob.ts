// Фоновая правка картинки ИИ на уровне раздела «Студия»: ожидание задания и флаг «идёт правка»
// переживают переключение экранов. Один объект на проект. Чистая логика, без React.
import { StudioError, type StorybookClient } from "./client";
import type { ImageEditResult } from "./imageEdit";
import { editErrorMessage } from "./imageEditFlow";
import { IMAGE_TEXTS } from "./imageToolsTexts";
import { waitForProjectJob } from "./projects";
import { errorReason } from "./shotsView";

// Как в Tk (POLL_ERRORS_LIMIT): терпим до трёх ошибок опроса подряд
export const POLL_ERRORS_LIMIT = 3;

export type Say = (text: string, type?: "error" | "success" | "info") => void;

export type EditJobsSnapshot = {
  // имя картинки, которую правят сейчас
  running: string | null;
  // время изменения правленых файлов (путь -> modified_ts): по нему ссылки обходят браузерный кэш
  edited: Readonly<Record<string, number>>;
};

// result null - правка не удалась (список мог измениться: копия версии шота)
export type EditSettled = { result: ImageEditResult | null };

export type TrackArgs = { client: StorybookClient; jobId: string; name: string; say: Say; intervalMs?: number };

export type EditJobs = {
  getSnapshot: () => EditJobsSnapshot;
  subscribe: (listener: () => void) => () => void;
  // подписка экрана на итог правки; возвращает отписку
  onSettled: (listener: (e: EditSettled) => void) => () => void;
  track: (args: TrackArgs) => void;
  dispose: () => void;
};

// Текст ошибки запуска и задания: только из общей таблицы по code, details.reason и details.unknown_cost
export const editFailureText = (err: unknown): string =>
  err instanceof StudioError ? editErrorMessage(err.code, errorReason(err), err.details?.unknown_cost) : IMAGE_TEXTS.edit_failed;

export const createEditJobs = (): EditJobs => {
  let snapshot: EditJobsSnapshot = { running: null, edited: {} };
  let disposed = false;
  let controller: AbortController | null = null;
  const listeners = new Set<() => void>();
  const settled = new Set<(e: EditSettled) => void>();

  const set = (next: EditJobsSnapshot) => {
    snapshot = next;
    listeners.forEach((l) => l());
  };

  const track = ({ client, jobId, name, say, intervalMs }: TrackArgs): void => {
    if (disposed || snapshot.running !== null) return;
    const mine = new AbortController();
    controller = mine;
    set({ ...snapshot, running: name });
    const finish = (result: ImageEditResult | null, text: string, type: "success" | "error") => {
      if (disposed || mine.signal.aborted) return;
      set({ running: null, edited: result ? { ...snapshot.edited, [result.path]: result.modified_ts } : snapshot.edited });
      say(text, type);
      settled.forEach((l) => l({ result }));
    };
    waitForProjectJob<ImageEditResult>(client, jobId, { signal: mine.signal, intervalMs, errorsLimit: POLL_ERRORS_LIMIT }).then(
      (result) => finish(result, IMAGE_TEXTS.edit_done, "success"),
      (err: unknown) => finish(null, editFailureText(err), "error"),
    );
  };

  return {
    getSnapshot: () => snapshot,
    subscribe: (l) => {
      listeners.add(l);
      return () => void listeners.delete(l);
    },
    onSettled: (l) => {
      settled.add(l);
      return () => void settled.delete(l);
    },
    track,
    dispose: () => {
      disposed = true;
      controller?.abort();
      listeners.clear();
      settled.clear();
    },
  };
};
