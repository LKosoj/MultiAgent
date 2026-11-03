// Обёртки возможностей модуля runs (управление прогоном), типы ответа и чистые помощники.
// Тексты о чужой поверхности и прогнозе приходят готовыми из Python: своих копий здесь нет.
import type { StorybookClient } from "./client";
import { createStatusPoller, type StatusPoller } from "./statusPoll";
import type { RunState } from "./status";

export type RunMode = "full" | "from_step" | "single_step" | "resume" | "regenerate_shots";
export type RunLauncher = "backend" | "local";
export type RunOutcome = "completed" | "failed" | "cancelled" | "interrupted" | null;
export type StepRunStatus = "pending" | "running" | "completed" | "skipped" | "failed";

export type PlanProblem = {
  level: "error" | "warning" | "info";
  code: string;
  message: string;
  step_id: string | null;
  artifact: string | null;
};

export type RunPlan = {
  version: number;
  mode: RunMode;
  project_id: string;
  pipeline_hash: string;
  steps_to_run: string[];
  skip_steps: string[];
  gated_off: string[];
  start_step: string | null;
  source_workflow_id: string | null;
  workflow_id: string;
  inputs: Record<string, unknown>;
  input_sources: Record<string, string>;
  step_params: Record<string, Record<string, unknown>>;
  scope: { shot_keys: string[]; kinds: string[] };
  problems: PlanProblem[];
  can_start: boolean;
  plan_digest: string;
  estimate: Record<string, unknown> | null;
  rebuild_film: boolean;
};

export type RunPlanParams = {
  project_id: string;
  mode: RunMode;
  start_step?: string;
  step_id?: string;
  shot_keys?: string[];
  kinds?: string[];
  rebuild_film?: boolean;
  inputs?: Record<string, unknown>;
  task?: string;
};

export type RunStartParams = RunPlanParams & {
  launcher?: RunLauncher;
  expected_plan_digest?: string;
  discard_unfinished?: boolean;
  abandon_pending_jobs?: boolean;
};

export type RunPlanResult = { status: "success"; plan: RunPlan };
export type RunStartResult = { status: "success"; run_id: string; workflow_id: string; plan: RunPlan };

export type RunProgress = {
  run_id: string | null;
  mode: RunMode | null;
  surface: "tk" | "react" | null;
  step_id: string | null;
  step_index: number | null;
  step_total: number | null;
  percent: number | null;
  message: string | null;
  state: RunState;
  paused: boolean;
  pause_requested: boolean;
  cancel_requested: boolean;
};

export type RunStepRow = {
  id: string;
  status: StepRunStatus;
  started_at: string | null;
  finished_at: string | null;
  duration_s: number | null;
  error: string | null;
};

export type RunStatusResult = {
  status: "success";
  state: RunState;
  locked: boolean;
  run: Record<string, unknown> | null;
  other_surface: { surface: "tk" | "react"; text: string } | null;
  progress: RunProgress | null;
  steps: RunStepRow[];
  video: { done: number; total: number; failed: number };
  cost: { spent_rub: number; budget_rub: number | null; scope: "video_jobs" };
  eta: { seconds: number | null; basis: "history" | "no_history" } | null;
  control: Record<string, unknown>;
  unresponsive: boolean;
  heartbeat_age_s: number | null;
  failure: { step_id: string | null; message: string | null } | null;
  outcome: RunOutcome;
  resumable: boolean;
  engine: Record<string, unknown>;
  stale_control: boolean;
};

export type RunControlResult = { status: "success"; state: RunState; message?: string };
export type RunLogTailResult = { status: "success"; lines: string[]; offset: number; truncated: boolean };

export type RunHistoryRow = {
  run_id: string;
  mode: RunMode | null;
  surface: "tk" | "react" | null;
  started_at: string | null;
  finished_at: string | null;
  outcome: RunOutcome;
  unfinished: boolean;
  resumable: boolean;
};
export type RunHistoryResult = { status: "success"; runs: RunHistoryRow[] };

export type PipelineStepInfo = { id: string; depends_on: string[]; title?: string } & Record<string, unknown>;
export type RunStepsResult = { status: "success"; steps: PipelineStepInfo[] };

export const runPlan = (c: StorybookClient, p: RunPlanParams) => c.call<RunPlanResult>("run_plan", p);

export const runStart = (c: StorybookClient, p: RunStartParams) => c.call<RunStartResult>("run_start", p);

export const runStatus = (c: StorybookClient, p: { project_id: string; run_id?: string }) =>
  c.call<RunStatusResult>("run_status", p);

export const runSetPause = (c: StorybookClient, p: { project_id: string; paused: boolean; run_id?: string }) =>
  c.call<RunControlResult>("run_set_pause", p);

export const runCancel = (c: StorybookClient, p: { project_id: string; run_id?: string; force?: boolean }) =>
  c.call<RunControlResult>("run_cancel", p);

export const runMarkInterrupted = (c: StorybookClient, p: { project_id: string; run_id: string }) =>
  c.call<RunControlResult>("run_mark_interrupted", p);

export const runLogTail = (
  c: StorybookClient,
  p: { project_id: string; run_id?: string; lines?: number; after_offset?: number },
) => c.call<RunLogTailResult>("run_log_tail", p);

export const runHistory = (c: StorybookClient, p: { project_id: string; limit?: number }) =>
  c.call<RunHistoryResult>("run_history", p);

export const runStepsGet = (c: StorybookClient) => c.call<RunStepsResult>("run_steps_get");

// ---- чистые помощники

const ACTIVE_STATES: readonly RunState[] = ["running", "pause_requested", "paused", "stopping"];

export const isActiveState = (state: RunState | undefined): boolean =>
  state !== undefined && ACTIVE_STATES.includes(state);

export const isTerminalState = (state: RunState | undefined): boolean =>
  state === "failed" || state === "completed" || state === "interrupted";

// Новый прогон можно просить, когда активного нет: текущий проект не занят.
export const canStart = (s: Pick<RunStatusResult, "state" | "locked"> | null | undefined): boolean =>
  !!s && !s.locked && !isActiveState(s.state);

export const otherSurfaceText = (s: Pick<RunStatusResult, "other_surface"> | null | undefined): string | null =>
  s?.other_surface?.text ?? null;

// Во время прогона опрашиваем чаще, в остальных состояниях редко; при зависании показываем без спешки.
export const nextPollDelayMs = (state: RunState | undefined): number => {
  switch (state) {
    case "running":
    case "pause_requested":
    case "stopping":
      return 2_000;
    case "paused":
      return 5_000;
    default:
      return 15_000;
  }
};

export const LOG_LINES_CAP = 2_000;

// Дописывает новые строки к уже показанным, оставляя не более LOG_LINES_CAP последних.
export const appendLogTail = (current: string[], next: string[], cap: number = LOG_LINES_CAP): string[] => {
  if (next.length === 0) return current.length > cap ? current.slice(-cap) : current;
  const merged = current.concat(next);
  return merged.length > cap ? merged.slice(merged.length - cap) : merged;
};

export type RunPollerOptions = {
  client: StorybookClient;
  project_id: string;
  run_id?: string;
  onStatus: (s: RunStatusResult) => void;
  onError: (err: unknown) => void;
  setTimer?: (fn: () => void, ms: number) => unknown;
  clearTimer?: (handle: unknown) => void;
  isHidden?: () => boolean;
};

// Опрос состояния прогона: интервал зависит от состояния, остальное (single-flight, скрытая вкладка,
// рост интервала при сбоях) делает общий опросчик.
export const createRunPoller = (o: RunPollerOptions): StatusPoller =>
  createStatusPoller<RunStatusResult>({
    fetch: () => runStatus(o.client, o.run_id ? { project_id: o.project_id, run_id: o.run_id } : { project_id: o.project_id }),
    intervalFor: (data) => nextPollDelayMs(data?.state),
    onData: o.onStatus,
    onError: o.onError,
    setTimer: o.setTimer,
    clearTimer: o.clearTimer,
    isHidden: o.isHidden,
  });
