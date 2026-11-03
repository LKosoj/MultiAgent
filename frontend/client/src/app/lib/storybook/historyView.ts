// Таблица прошлых прогонов.
import { formatDateTimeRu, formatDuration } from "./format";
import { GEN_TEXTS } from "./generationTexts";
import { modeLabel, isRunMode, secondsBetween } from "./generationModel";
import type { RunHistoryRow } from "./runs";
import { RUN_RESULT_LABELS } from "./texts";

export type HistoryViewRow = {
  runId: string;
  started: string;
  mode: string;
  where: string;
  result: string;
  duration: string;
  note: string | null;
};

export const historyRows = (rows: readonly RunHistoryRow[]): HistoryViewRow[] =>
  rows.map((r) => ({
    runId: r.run_id,
    started: formatDateTimeRu(r.started_at),
    mode: r.mode && isRunMode(r.mode) ? modeLabel(r.mode) : "—",
    where: r.surface === "tk" ? GEN_TEXTS.history_where_tk : r.surface === "react" ? GEN_TEXTS.history_where_react : "—",
    duration: (() => {
      const secs = secondsBetween(r.started_at, r.finished_at);
      return secs === null ? "—" : formatDuration(secs);
    })(),
    result: r.outcome ? (RUN_RESULT_LABELS[r.outcome] ?? r.outcome) : r.unfinished ? GEN_TEXTS.history_unfinished : "—",
    note: r.resumable ? GEN_TEXTS.history_resumable : null,
  }));
