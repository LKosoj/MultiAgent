// Чистая модель плеера: клавиши, перемотка, позиция. Видео и звук рисует компонент.
import { formatTimecode } from "./media";

export type PlayerAction =
  | { type: "toggle" }
  | { type: "seek_by"; delta: number }
  | { type: "mute" }
  | { type: "fullscreen" }
  | { type: "zoom"; direction: "in" | "out" };

export type KeyInfo = { key: string; shiftKey?: boolean; ctrlKey?: boolean; metaKey?: boolean; altKey?: boolean };

export const SEEK_STEP_S = 5;
export const SEEK_FINE_STEP_S = 1;

// Клавиши работают, когда фокус не в поле ввода и не нажаты Ctrl/Alt/Meta
export const keyAction = (e: KeyInfo, inField: boolean): PlayerAction | null => {
  if (inField || e.ctrlKey || e.metaKey || e.altKey) return null;
  const step = e.shiftKey ? SEEK_FINE_STEP_S : SEEK_STEP_S;
  switch (e.key) {
    case " ":
    case "Spacebar":
      return { type: "toggle" };
    case "ArrowLeft":
      return { type: "seek_by", delta: -step };
    case "ArrowRight":
      return { type: "seek_by", delta: step };
    case "m":
    case "M":
    case "ь":
    case "Ь":
      return { type: "mute" };
    case "f":
    case "F":
    case "а":
    case "А":
      return { type: "fullscreen" };
    case "+":
    case "=":
      return { type: "zoom", direction: "in" };
    case "-":
    case "_":
      return { type: "zoom", direction: "out" };
    default:
      return null;
  }
};

export const clampTime = (t: number, duration: number): number => {
  const max = duration > 0 ? duration : 0;
  return Math.min(max, Math.max(0, Number.isFinite(t) ? t : 0));
};

export const seekTarget = (current: number, delta: number, duration: number): number => clampTime(current + delta, duration);

// Клик по дорожке: x внутри видимой области, scrollLeft - сдвиг прокрутки
export const timeFromX = (x: number, scrollLeft: number, pxPerS: number, duration: number): number =>
  pxPerS > 0 ? clampTime((x + scrollLeft) / pxPerS, duration) : 0;

export const formatPosition = (current: number, duration: number): string =>
  `${formatTimecode(current)} / ${formatTimecode(duration)}`;

// Видимая область после перемотки: курсор остаётся в поле зрения
export const scrollForCursor = (cursorPx: number, scrollLeft: number, viewWidth: number): number => {
  if (cursorPx < scrollLeft) return Math.max(0, cursorPx - viewWidth * 0.1);
  if (cursorPx > scrollLeft + viewWidth) return Math.max(0, cursorPx - viewWidth * 0.9);
  return scrollLeft;
};

// Ошибка загрузки видео: первая - один повтор после обновления токена, дальше - показать ошибку
export const nextOnError = (attempt: number): "retry" | "fail" => (attempt < 1 ? "retry" : "fail");

// Элемент, в котором клавиши нужны самому элементу (поля, кнопки, ссылки): горячие клавиши там не перехватываем
export const isTypingElement = (el: { tagName?: string; isContentEditable?: boolean } | null): boolean => {
  if (!el || !el.tagName) return false;
  return ["INPUT", "TEXTAREA", "SELECT", "BUTTON", "A"].includes(el.tagName) || !!el.isContentEditable;
};

// Что показывать вместо видео: ссылка пропала из-за истёкшего токена - это ошибка, а не «пусто»
export const playerStatus = (a: { hasVideo: boolean; src: string | null; failed: boolean; mediaReady: boolean }): "ok" | "empty" | "loading" | "failed" => {
  if (!a.hasVideo) return "empty";
  if (a.failed) return "failed";
  if (a.src) return "ok";
  return a.mediaReady ? "failed" : "loading";
};

// «Повторить»: сначала показываем загрузку, ошибку и счётчик попыток сбрасываем только после токена
export const runRetry = async (
  refresh: () => Promise<void>,
  on: { start: () => void; done: () => void; fail: () => void },
): Promise<void> => {
  on.start();
  try {
    await refresh();
    on.done();
  } catch {
    on.fail();
  }
};
