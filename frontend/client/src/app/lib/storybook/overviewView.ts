// Модели для экрана «Обзор»: всё считается из ProjectStatus, подписи берём из данных.
import { formatBytes, formatClock, formatDateTimeAny, formatDuration, formatMaybe, formatRub } from "./format";
import type {
  NextActionTarget,
  ProjectStatus,
  ProjectStatusBrief,
  RunState,
  StatusCounter,
  StepStatus,
} from "./status";
import { counterTarget } from "./status";
import { RUN_MODE_LABELS, RUN_RESULT_LABELS, T, counterLabels, lastRunLabels, stepNoteLabel } from "./texts";

export type Tone = "muted" | "running" | "warn" | "danger" | "ok" | "info";

export const runTone = (state: RunState | undefined): Tone => {
  switch (state) {
    case "running":
      return "running";
    case "pause_requested":
    case "paused":
    case "stopping":
    case "interrupted":
      return "warn";
    case "failed":
      return "danger";
    case "completed":
      return "ok";
    default:
      return "muted";
  }
};

export type StageCell = {
  id: string;
  title: string;
  state: string;
  stateTitle: string;
  progress: string | null;
  current: boolean;
  synthetic: boolean;
};

// Этапы как отдаёт сервер, плюс клиентская ячейка «Готово»
export const stageStrip = (status: ProjectStatus): StageCell[] => {
  const cells: StageCell[] = status.stage.items.map((item) => ({
    id: item.id,
    title: item.title,
    state: item.state,
    stateTitle: item.state_title,
    progress: item.total > 0 ? `${item.done}/${item.total}` : null,
    current: status.stage.current === item.id,
    synthetic: false,
  }));
  const finished = status.stage.current === "done";
  cells.push({
    id: "done",
    title: T.doneStage,
    state: finished ? "done" : "not_started",
    stateTitle: finished ? "Готов" : "Не начат",
    progress: null,
    current: finished,
    synthetic: true,
  });
  return cells;
};

export type CounterTile = {
  key: StatusCounter | "spend" | "warnings";
  label: string;
  value: string;
  known: boolean;
  sub: string | null;
  notes: string[];
  partial: boolean;
  target: { screen: string; filter: string | null } | null;
  toWarnings: boolean;
};

const COUNTER_FIELDS: { key: StatusCounter; field: keyof ProjectStatus["shots"] }[] = [
  { key: "total", field: "total" },
  { key: "ready", field: "ready" },
  { key: "stale", field: "stale" },
  { key: "errors", field: "errors" },
  { key: "approved", field: "approved" },
];

export const counterTiles = (status: ProjectStatus): CounterTile[] => {
  const shots = status.shots;
  const tiles: CounterTile[] = COUNTER_FIELDS.map(({ key, field }) => {
    const raw = shots[field] as number | null;
    const notes: string[] = [];
    if (key === "stale" && shots.ready_basis === "files") notes.push(T.noStaleData);
    return {
      key,
      label: counterLabels[key],
      value: formatMaybe(raw),
      known: raw !== null,
      sub: null,
      notes,
      partial: shots.partial,
      target: counterTarget(key),
      toWarnings: false,
    };
  });
  const spend = status.spend;
  const spendNotes: string[] = [];
  if (spend.assumed_rub > 0) spendNotes.push(T.assumedSpend);
  if (Object.keys(spend.other_currencies ?? {}).length > 0) spendNotes.push(T.otherCurrency);
  tiles.push({
    key: "spend",
    label: counterLabels.spend,
    value: formatRub(spend.total),
    known: true,
    sub: spend.budget_rub !== null ? `${T.topOf} ${formatRub(spend.budget_rub)}` : null,
    notes: spendNotes,
    partial: false,
    target: null,
    toWarnings: false,
  });
  tiles.push({
    key: "warnings",
    label: counterLabels.warnings,
    value: formatMaybe(status.warnings.count),
    known: status.warnings.count !== null,
    sub: null,
    notes: [],
    partial: false,
    target: null,
    toWarnings: true,
  });
  return tiles;
};

export type StepRow = {
  id: string;
  order: number;
  title: string;
  stage: string | null;
  displayState: string;
  stateTitle: string;
  duration: string | null;
  error: string | null;
  lowConfidence: boolean;
  note: string | null;
  running: boolean;
};

const ERROR_LIMIT = 300;

export const stepRows = (status: ProjectStatus): StepRow[] =>
  [...status.steps]
    .sort((a, b) => a.order - b.order)
    .map((s: StepStatus) => ({
      id: s.id,
      order: s.order,
      title: s.title,
      stage: s.stage,
      displayState: s.display_state,
      stateTitle: s.state_title,
      duration: s.duration_seconds === null ? null : formatDuration(s.duration_seconds),
      error: s.error ? (s.error.length > ERROR_LIMIT ? `${s.error.slice(0, ERROR_LIMIT)}…` : s.error) : null,
      lowConfidence: s.confidence === "low",
      note: s.note_code ? stepNoteLabel(s.note_code) : null,
      running: s.display_state === "running",
    }));

export type StepGroup = { stageId: string | null; title: string; rows: StepRow[] };

// Шаги по этапам в порядке этапов; шаги без этапа - в группе «Прочее»
export const stepGroups = (status: ProjectStatus): StepGroup[] => {
  const titles = new Map(status.stage.items.map((i) => [i.id, i.title]));
  const groups: StepGroup[] = [];
  for (const row of stepRows(status)) {
    let group = groups.find((g) => g.stageId === row.stage);
    if (!group) {
      group = { stageId: row.stage, title: (row.stage && titles.get(row.stage)) || "Прочее", rows: [] };
      groups.push(group);
    }
    group.rows.push(row);
  }
  return groups;
};

export type NextActionView = {
  code: string;
  title: string;
  detail: string;
  severity: "info" | "warning" | "error" | "success";
  buttonLabel: string;
  target: NextActionTarget;
};

export const nextActionView = (status: ProjectStatus): NextActionView => ({
  code: status.next_action.code,
  title: status.next_action.title,
  detail: status.next_action.detail,
  severity: status.next_action.severity,
  buttonLabel: T.go,
  target: status.next_action.target,
});

export type FinalVideoView = {
  exists: boolean;
  path: string;
  meta: string;
  olderThanShots: boolean;
  review: string | null;
};

export const finalVideoView = (status: ProjectStatus): FinalVideoView => {
  const v = status.final_video;
  const parts: string[] = [];
  if (v.duration_seconds !== null) parts.push(formatClock(v.duration_seconds));
  if (v.height !== null) parts.push(`${v.height}p`);
  if (v.size_bytes !== null) parts.push(formatBytes(v.size_bytes));
  return {
    exists: v.exists,
    path: v.path,
    meta: parts.join(" · "),
    olderThanShots: v.exists && v.older_than_shots,
    review:
      v.exists && v.review_passed === false
        ? lastRunLabels.reviewErrors(v.review_errors ?? 0, v.review_warnings ?? 0)
        : null,
  };
};

export type LastRunView = { rows: { label: string; value: string }[]; error: string | null } | null;

export const lastRunView = (status: ProjectStatus): LastRunView => {
  const r = status.last_run;
  if (!r) return null;
  const rows = [
    { label: lastRunLabels.started, value: formatDateTimeAny(r.started_at) },
    { label: lastRunLabels.mode, value: r.mode ? (RUN_MODE_LABELS[r.mode] ?? r.mode) : "—" },
    { label: lastRunLabels.duration, value: formatDuration(r.duration_seconds) },
    { label: lastRunLabels.result, value: RUN_RESULT_LABELS[r.status] ?? r.status },
    {
      label: lastRunLabels.steps,
      value: r.steps_failed > 0 ? `${r.steps_done}, ${lastRunLabels.failedSteps} ${r.steps_failed}` : String(r.steps_done),
    },
  ];
  return { rows, error: r.error };
};

export const isEmptyProject = (status: ProjectStatus): boolean =>
  status.next_action.code === "empty_project" ||
  (status.last_run === null && status.steps.every((s) => s.state === "not_run"));

// Краткий статус из полного: верхняя полоса не опрашивает сервер второй раз
export const briefFromStatus = (status: ProjectStatus): ProjectStatusBrief => {
  const run = status.active_run;
  const current = run.current_step ? status.steps.find((s) => s.id === run.current_step) : undefined;
  const failed = [...status.steps].reverse().find((s) => s.state === "failed");
  return {
    generated_at: status.generated_at,
    project: { id: status.project.id, title: status.project.title },
    stage: { id: status.stage.current, title: status.stage.title },
    run: {
      state: run.state,
      text: run.text,
      step: run.current_step
        ? { id: run.current_step, title: current?.title ?? null, index: run.step_index, total: run.step_total }
        : null,
      percent: run.percent,
      pause_requested: run.pause_requested,
      cancel_requested: run.cancel_requested,
      surface: run.surface,
    },
    spend: { total: status.spend.total, currency: status.spend.currency, budget_rub: status.spend.budget_rub },
    failed_step: failed ? { id: failed.id, title: failed.title } : null,
    final_video_exists: status.final_video.exists,
  };
};
