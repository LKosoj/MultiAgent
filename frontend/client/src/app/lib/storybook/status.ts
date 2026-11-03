// Обёртки возможностей модуля status (состояние проекта) и типы ответа.
// Подписи и правила «следующего действия» приходят готовыми из Python: своих копий здесь нет.
import type { StorybookClient } from "./client";

export type Surface = "tk" | "react";

export type StepState = "not_run" | "running" | "done" | "failed" | "skipped";
export type StepDisplayState = StepState | "outdated" | "waiting";
export type StageState = "not_started" | "partial" | "running" | "done" | "problems";
export type RunState =
  | "idle"
  | "running"
  | "pause_requested"
  | "paused"
  | "stopping"
  | "interrupted"
  | "failed"
  | "completed";
export type NextActionCode =
  | "run_active"
  | "resume_interrupted"
  | "empty_project"
  | "fix_failed_step"
  | "regenerate_stale"
  | "fix_shot_errors"
  | "generate_images"
  | "generate_videos"
  | "redo_shots"
  | "review_shots"
  | "run_next_step"
  | "build_film"
  | "look_at_warnings"
  | "done";
export type StepSource = "db" | "run_file" | "artifacts" | "flag" | "downstream" | "none";
export type StepNoteCode =
  | "recovered_after_failure"
  | "run_file_conflict"
  | "bypassed"
  | "flag_off"
  | "interrupted"
  | "artifacts_only"
  | "condition_unresolved";

export type StepArtifact = { rel: string; state: "present" | "missing"; mtime: number | null };

export type StepStatus = {
  id: string;
  order: number;
  title: string;
  stage: string | null;
  optional: boolean;
  state: StepState;
  display_state: StepDisplayState;
  state_title: string;
  source: StepSource;
  confidence: "high" | "medium" | "low";
  started_at: string | null; // ISO-8601 UTC
  finished_at: string | null;
  duration_seconds: number | null; // длительность последнего запуска
  error: string | null;
  note_code: StepNoteCode | null;
  outdated_hint: { since_step: string; reliability: "low" } | null;
  depends_on: string[];
  artifacts: StepArtifact[];
};

export type StageItem = {
  id: string;
  title: string;
  state: StageState;
  state_title: string;
  done: number;
  total: number;
};

export type ArtifactRow = {
  id: string;
  title: string;
  rel: string;
  state: "present" | "missing" | "invalid";
  mtime: number | null;
  size_bytes: number | null;
};

// null вместо числа - «неизвестно» (нет файла или нет данных от другой задачи)
export type ShotCounters = {
  source: "shots.json" | "missing" | "invalid";
  partial: boolean;
  total: number | null;
  with_images: number | null;
  with_videos: number | null;
  video_expected: number | null;
  ready: number | null;
  errors: number | null;
  stale: number | null;
  approved: number | null;
  review_pending: number | null;
  redo: number | null;
  ready_basis: "files" | "files+stale";
  items: null;
};

export type FinalVideo = {
  exists: boolean;
  path: string;
  size_bytes: number | null;
  mtime: number | null;
  duration_seconds: number | null;
  width: number | null;
  height: number | null;
  review_passed: boolean | null;
  review_errors: number | null;
  review_warnings: number | null;
  photo_video_exists: boolean;
  older_than_shots: boolean;
};

export type Spend = {
  scope: "video_jobs";
  currency: "RUB";
  total: number;
  by_status: Record<string, number>;
  wasted: number;
  by_provider: Record<string, number>;
  other_currencies: Record<string, number>;
  jobs_total: number;
  jobs_failed: number;
  jobs_unknown_cost: number;
  assumed_rub: number; // часть total по задачам без записанной валюты
  budget_rub: number | null;
  budget_left_rub: number | null;
};

export type ActiveRun = {
  state: RunState;
  locked: boolean;
  run_id: string | null;
  workflow_id: string | null;
  surface: string | null;
  current_step: string | null;
  step_index: number | null;
  step_total: number;
  percent: number | null;
  pause_requested: boolean;
  cancel_requested: boolean;
  stale_control: boolean;
  text: string;
};

export type LastRun = {
  workflow_id: string;
  status: string;
  mode: string | null;
  started_at: string | null;
  finished_at: string | null;
  duration_seconds: number | null;
  steps_done: number;
  steps_failed: number;
  error: string | null;
};

export type NextActionTarget = {
  screen: "generation" | "shots" | "movie" | "overview";
  filter: "stale" | "missing_images" | "missing_videos" | "errors" | "pending" | "redo" | null;
  step_id: string | null;
  mode: "resume" | "from_step" | "single_step" | "full" | null;
};

export type NextAction = {
  code: NextActionCode;
  title: string;
  detail: string;
  severity: "info" | "warning" | "error" | "success";
  target: NextActionTarget;
};

export type ProjectStatus = {
  version: number;
  generated_at: number;
  cache: { static_hit: boolean };
  project: { id: string; title: string | null; language: string | null };
  stage: { current: string; title: string; items: StageItem[] };
  steps: StepStatus[];
  artifacts: ArtifactRow[];
  shots: ShotCounters;
  final_video: FinalVideo;
  spend: Spend;
  warnings: { count: number | null; source: "reports" | null };
  active_run: ActiveRun;
  last_run: LastRun | null;
  incomplete_runs: string[];
  next_action: NextAction;
  flags: Record<string, { value: boolean | null; source: string }>;
  sources: {
    runs_db: string;
    run_files: number;
    run_files_unparsed: number;
    shots: string;
    provider_jobs: string;
    step_runs: string;
  };
  hook_errors: string[];
};

export type ProjectStatusBrief = {
  generated_at: number;
  project: { id: string; title: string | null };
  stage: { id: string; title: string };
  run: {
    state: RunState;
    text: string;
    step: { id: string; title: string | null; index: number | null; total: number } | null;
    percent: number | null;
    pause_requested: boolean;
    cancel_requested: boolean;
    surface: string | null;
  };
  spend: { total: number; currency: "RUB"; budget_rub: number | null };
  failed_step: { id: string; title: string } | null;
  final_video_exists: boolean;
};

export type ProjectStatusResult = ProjectStatus;
export type ProjectStatusBriefResult = ProjectStatusBrief;

export const projectStatus = (
  c: StorybookClient,
  p: { project_id: string; surface?: Surface; refresh?: boolean },
) => c.call<ProjectStatusResult>("project_status", p);

export const projectStatusBrief = (
  c: StorybookClient,
  p: { project_id: string; surface?: Surface },
) => c.call<ProjectStatusBriefResult>("project_status_brief", p);

export const STATUS_POLL_ACTIVE_MS = 2000;
export const STATUS_POLL_IDLE_MS = 10000;

// пока прогон жив (идёт, пауза, остановка), опрашиваем чаще
export const statusPollIntervalMs = (runState: RunState | undefined): number =>
  runState === "running" ||
  runState === "pause_requested" ||
  runState === "paused" ||
  runState === "stopping"
    ? STATUS_POLL_ACTIVE_MS
    : STATUS_POLL_IDLE_MS;

export type StatusCounter = "total" | "ready" | "stale" | "errors" | "approved";

// клик по счётчику на Обзоре ведёт в «Кадры» с фильтром («Предупреждений» - в блок на Обзоре)
const COUNTER_TARGETS: Record<StatusCounter, { screen: string; filter: string | null }> = {
  total: { screen: "shots", filter: null },
  ready: { screen: "shots", filter: "ready" },
  stale: { screen: "shots", filter: "stale" },
  errors: { screen: "shots", filter: "errors" },
  approved: { screen: "shots", filter: "approved" },
};

export const counterTarget = (counter: StatusCounter): { screen: string; filter: string | null } => ({
  ...COUNTER_TARGETS[counter],
});
