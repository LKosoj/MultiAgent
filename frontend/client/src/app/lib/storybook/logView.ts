// Журнал прогона: разбор записей, догрузка по смещению, текст для копирования.
import { StudioError } from "./client";
import { GEN_TEXTS, LOG_LEVEL_WORDS } from "./generationTexts";
import { appendLogTail, type RunLogTailResult } from "./runs";

export type LogEntry = {
  raw: string;
  time: string | null;
  level: string | null;
  word: string | null;
  text: string;
};

const LINE = /^\d{4}-\d{2}-\d{2}T(\d{2}:\d{2}:\d{2})\s+([A-Z]+)\s+([\s\S]*)$/;

export const parseLine = (raw: string): LogEntry => {
  const m = LINE.exec(raw);
  if (!m) return { raw, time: null, level: null, word: null, text: raw };
  const level = m[2];
  return { raw, time: m[1], level, word: LOG_LEVEL_WORDS[level] ?? level, text: m[3] };
};

export const logText = (lines: readonly string[]): string => lines.join("\n");

export type LogState = {
  runId: string | null;
  lines: string[];
  offset: number;
  truncated: boolean;
  error: string | null;
};

export const EMPTY_LOG: LogState = { runId: null, lines: [], offset: 0, truncated: false, error: null };

export type LogLoaderOptions = {
  fetch: (runId: string, afterOffset: number) => Promise<RunLogTailResult>;
  onChange: (state: LogState) => void;
};

export type LogLoader = {
  // докачать новое; одновременно идёт один запрос
  poll(runId: string | null): Promise<void>;
  // «Очистить вид»: скрыть показанное, дальше только новое
  clear(): void;
  state(): LogState;
};

export const createLogLoader = (o: LogLoaderOptions): LogLoader => {
  let st: LogState = EMPTY_LOG;
  let inflight: Promise<void> | null = null;
  // растёт при каждом сбросе: ответ запроса из прошлого «поколения» отбрасывается, даже если runId снова тот же (A → B → A)
  let gen = 0;
  const set = (next: LogState) => {
    st = next;
    o.onChange(st);
  };
  const run = async (runId: string): Promise<void> => {
    const mineGen = gen;
    try {
      const res = await o.fetch(runId, st.offset);
      if (gen !== mineGen) return; // пока ждали, выбрали другой прогон
      set({
        runId,
        lines: appendLogTail(st.lines, res.lines),
        offset: res.offset,
        truncated: res.truncated || st.truncated,
        error: null,
      });
    } catch (err) {
      if (gen !== mineGen) return;
      const text = err instanceof StudioError && err.code === "not_found" ? GEN_TEXTS.log_deleted : err instanceof Error ? err.message : "";
      set({ ...st, error: text || GEN_TEXTS.log_deleted });
    }
  };
  return {
    async poll(runId) {
      if (runId === null) {
        if (st.runId !== null) {
          gen += 1;
          set(EMPTY_LOG);
        }
        return;
      }
      if (st.runId !== runId) {
        // другой прогон: старый запрос не ждём, его ответ отбросится сам
        gen += 1;
        set({ ...EMPTY_LOG, runId });
        inflight = null;
      }
      if (inflight) return inflight;
      const mine: Promise<void> = run(runId).finally(() => {
        if (inflight === mine) inflight = null;
      });
      inflight = mine;
      return mine;
    },
    clear() {
      set({ ...st, lines: [] });
    },
    state: () => st,
  };
};
