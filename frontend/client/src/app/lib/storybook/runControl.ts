// Что показывать на кнопках «Пауза/Продолжить» и «Стоп» по состоянию прогона (паритет с Tk).
import type { RunState } from "./status";
import { GEN_TEXTS } from "./generationTexts";
import { T } from "./texts";

export type RunControlView = {
  pauseLabel: string;
  pauseAction: "pause" | "resume";
  pauseEnabled: boolean;
  stopEnabled: boolean;
};

export const runControlView = (state: RunState | undefined): RunControlView => {
  const resume = state === "paused" || state === "pause_requested";
  return {
    pauseLabel: resume ? T.topResume : T.topPause,
    pauseAction: resume ? "resume" : "pause",
    pauseEnabled: state === "running" || resume,
    stopEnabled: state === "running" || resume,
  };
};

export type RerunView = {
  enabled: boolean;
  hint: string;
  // режим, с которым открывается «Генерация»
  mode: "resume" | "full" | null;
};

// Кнопка «Запустить заново»: ведёт на «Генерацию» с нужным режимом, сам запуск делает экран
export const rerunView = (state: RunState | undefined, resumable = true): RerunView => {
  if (state === "running" || state === "pause_requested" || state === "paused" || state === "stopping") {
    return { enabled: false, hint: GEN_TEXTS.rerun_busy, mode: null };
  }
  if (state === "failed" || state === "interrupted") {
    return resumable
      ? { enabled: true, hint: GEN_TEXTS.rerun_resume, mode: "resume" }
      : { enabled: true, hint: GEN_TEXTS.rerun_full, mode: "full" };
  }
  if (state === "completed") return { enabled: true, hint: GEN_TEXTS.rerun_full, mode: "full" };
  return { enabled: false, hint: GEN_TEXTS.rerun_idle, mode: null };
};
