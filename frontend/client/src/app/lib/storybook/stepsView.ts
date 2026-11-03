// Шаги прогона: названия из каталога шагов, статусы из run_status.
import { formatDuration } from "./format";
import { stepStatusWord } from "./generationTexts";
import type { PipelineStepInfo, RunStepRow } from "./runs";

export type StepsTitles = (id: string) => string;

export const makeTitleOf = (catalog: readonly PipelineStepInfo[] | null | undefined): StepsTitles => {
  const map = new Map<string, string>();
  for (const s of catalog ?? []) if (typeof s.title === "string" && s.title) map.set(s.id, s.title);
  return (id) => map.get(id) ?? id;
};

export type StepViewRow = {
  id: string;
  index: number;
  title: string;
  status: string;
  word: string;
  duration: string | null;
  error: string | null;
  current: boolean;
};

const ERROR_LIMIT = 300;

const elapsedSeconds = (row: RunStepRow, nowMs: number): number | null => {
  if (typeof row.duration_s === "number") return row.duration_s;
  if (row.status !== "running" || !row.started_at) return null;
  const t = Date.parse(row.started_at);
  return Number.isFinite(t) && nowMs >= t ? Math.round((nowMs - t) / 1000) : null;
};

// Шаги в порядке плана; текущим считается идущий, а если его нет - первый ожидающий после готовых
export const stepRows = (steps: readonly RunStepRow[], titleOf: StepsTitles, nowMs: number): StepViewRow[] => {
  let current = steps.findIndex((s) => s.status === "running");
  if (current < 0) current = steps.findIndex((s) => s.status === "failed");
  return steps.map((s, i) => {
    const secs = elapsedSeconds(s, nowMs);
    return {
      id: s.id,
      index: i + 1,
      title: titleOf(s.id),
      status: s.status,
      word: stepStatusWord(s.status),
      duration: secs === null ? null : formatDuration(secs),
      error: s.error ? s.error.slice(0, ERROR_LIMIT) : null,
      current: i === current,
    };
  });
};

// Автопрокрутка к текущему шагу: выключается, когда пользователь прокрутил список сам, до смены текущего шага
export type FollowState = { follow: boolean; lastCurrent: string | null };

export const nextFollow = (state: FollowState, currentId: string | null, userScrolled: boolean): FollowState => {
  if (currentId !== state.lastCurrent) return { follow: true, lastCurrent: currentId };
  return userScrolled ? { follow: false, lastCurrent: currentId } : state;
};

// Шаги каталога вместе со статусами прогона: список виден и до первого запуска
export const mergeStepRows = (catalog: readonly PipelineStepInfo[], steps: readonly RunStepRow[]): RunStepRow[] => {
  const byId = new Map(steps.map((s) => [s.id, s]));
  const blank = (id: string): RunStepRow => ({ id, status: "pending", started_at: null, finished_at: null, duration_s: null, error: null });
  const out = catalog.map((c) => byId.get(c.id) ?? blank(c.id));
  const known = new Set(catalog.map((c) => c.id));
  for (const s of steps) if (!known.has(s.id)) out.push(s);
  return out;
};
