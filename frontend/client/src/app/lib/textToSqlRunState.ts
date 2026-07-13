import {
  hasOwnField,
  isRecord,
  isTextToSqlTerminalOutcome,
  textToSqlRunTerminalStatus,
} from "./textToSqlContracts";

export type TextToSqlRunSelection = {
  generation: number;
  runId: string | null;
};

export function invalidateTextToSqlRunSelection(
  selection: TextToSqlRunSelection,
): number {
  selection.generation += 1;
  selection.runId = null;
  return selection.generation;
}

export function beginTextToSqlRunSelection(
  selection: TextToSqlRunSelection,
): number {
  return invalidateTextToSqlRunSelection(selection);
}

export function selectTextToSqlRun(
  selection: TextToSqlRunSelection,
  generation: number,
  runId: string,
): boolean {
  if (selection.generation !== generation) return false;
  selection.runId = runId;
  return true;
}

export function commitTextToSqlRunSelection(
  selection: TextToSqlRunSelection,
  runId: string,
  commit: () => void,
): boolean {
  if (selection.runId !== runId) return false;
  commit();
  return true;
}

export type TextToSqlResultLoadState = {
  inFlight: Map<string, Promise<unknown>>;
  confirmed: Map<string, unknown>;
};

export function createTextToSqlResultLoadState(): TextToSqlResultLoadState {
  return { inFlight: new Map(), confirmed: new Map() };
}

export function isConfirmedTextToSqlResult(value: unknown): boolean {
  return isRecord(value) && isTextToSqlTerminalOutcome(value.terminal_outcome);
}

export function isConfirmedTextToSqlResultForRun(
  value: unknown,
  runId: string,
): boolean {
  return isRecord(value)
    && isTextToSqlTerminalOutcome(value.terminal_outcome)
    && value.terminal_outcome.run_id === runId;
}

export function loadTextToSqlResultSingleFlight(
  state: TextToSqlResultLoadState,
  runId: string,
  load: () => Promise<unknown>,
): Promise<unknown> {
  if (state.confirmed.has(runId)) {
    return Promise.resolve(state.confirmed.get(runId));
  }
  const existing = state.inFlight.get(runId);
  if (existing) return existing;

  const pending = Promise.resolve()
    .then(load)
    .then((result) => {
      const confirmed = isConfirmedTextToSqlResultForRun(result, runId);
      if (confirmed) {
        state.confirmed.set(runId, result);
      } else {
        state.confirmed.delete(runId);
      }
      if (isConfirmedTextToSqlResult(result) && !confirmed) return null;
      return result;
    })
    .catch((error) => {
      state.confirmed.delete(runId);
      throw error;
    })
    .finally(() => {
      if (state.inFlight.get(runId) === pending) state.inFlight.delete(runId);
    });
  state.inFlight.set(runId, pending);
  return pending;
}

type TextToSqlResultRetrySchedule = (
  retry: () => void,
) => () => void;
export const TEXT_TO_SQL_RESULT_RETRY_DELAY_MS = 500;

export function scheduleTextToSqlResultRetry(
  retries: Map<string, () => void>,
  runId: string,
  isSelected: () => boolean,
  retry: () => Promise<unknown>,
  schedule: TextToSqlResultRetrySchedule,
): void {
  if (retries.has(runId) || !isSelected()) return;

  let active = true;
  let cancelScheduled: () => void = () => undefined;
  const cancel = () => {
    if (!active) return;
    active = false;
    cancelScheduled();
    if (retries.get(runId) === cancel) retries.delete(runId);
  };
  retries.set(runId, cancel);
  cancelScheduled = schedule(() => {
    if (!active || retries.get(runId) !== cancel) return;
    active = false;
    retries.delete(runId);
    if (!isSelected()) return;
    void retry().catch(() => undefined);
  });
}

export function cancelTextToSqlResultRetries(
  retries: Map<string, () => void>,
): void {
  for (const cancel of [...retries.values()]) cancel();
  retries.clear();
}

export async function loadTextToSqlResultThenArtifacts(
  loadResult: () => Promise<unknown>,
  loadArtifacts: () => Promise<unknown>,
  handleResult: (result: unknown | null) => Promise<void>,
): Promise<unknown | null> {
  const result = await loadResult().catch(() => null);
  if (result === null) return null;
  await handleResult(result).catch(() => undefined);
  if (isConfirmedTextToSqlResult(result)) {
    void loadArtifacts().catch(() => undefined);
  }
  return result;
}

type TextToSqlCancellationInFlight = { current: boolean };
export type TextToSqlStatusLoads = Map<string, Promise<unknown>>;

export function textToSqlStatusForRun(
  payload: unknown,
  runId: string,
): Record<string, unknown> | null {
  if (!isRecord(payload)) return null;
  if (hasOwnField(payload, "run_id") && payload.run_id !== runId) return null;
  const status = isRecord(payload.status) ? payload.status : payload;
  if (status.run_id !== runId) return null;
  if (
    hasOwnField(status, "terminal_outcome")
    && (
      !isTextToSqlTerminalOutcome(status.terminal_outcome)
      || status.terminal_outcome.run_id !== runId
    )
  ) return null;
  return status;
}

export function loadTextToSqlStatusSingleFlight(
  inFlight: TextToSqlStatusLoads,
  runId: string,
  loadStatus: () => Promise<unknown>,
): Promise<unknown> {
  const existing = inFlight.get(runId);
  if (existing) return existing;

  const pending = Promise.resolve()
    .then(loadStatus)
    .finally(() => {
      if (inFlight.get(runId) === pending) inFlight.delete(runId);
    });
  inFlight.set(runId, pending);
  return pending;
}

export async function loadTextToSqlStatusAfterCurrent(
  inFlight: TextToSqlStatusLoads,
  runId: string,
  loadStatus: () => Promise<unknown>,
): Promise<unknown> {
  const current = inFlight.get(runId);
  if (current) {
    try {
      await current;
    } catch {
      // Ошибка старого опроса не отменяет чтение статуса после cancel.
    }
  }
  return loadStatus();
}

export function textToSqlRunCanCancel(
  runId: string | null,
  status: unknown,
  inFlight: boolean,
): boolean {
  return Boolean(runId) && !inFlight && textToSqlRunTerminalStatus(status) === null;
}

export async function requestTextToSqlCancellation(
  runServiceAction: (action: string, payload: Record<string, unknown>) => Promise<unknown>,
  loadStatus: (runId: string) => Promise<unknown>,
  runId: string,
  currentStatus: unknown,
  inFlight: TextToSqlCancellationInFlight,
) {
  if (!textToSqlRunCanCancel(runId, currentStatus, inFlight.current)) {
    return {
      kind: "skipped" as const,
      requested: false,
      terminalStatus: textToSqlRunTerminalStatus(currentStatus),
      status: currentStatus,
    };
  }

  inFlight.current = true;
  try {
    const response = await runServiceAction("workflows.cancel", { run_id: runId });
    const requested = isRecord(response) && response.cancelled === true;
    let observedStatus: unknown;
    try {
      observedStatus = await loadStatus(runId);
    } catch (error) {
      if (!requested) throw error;
    }
    const status = requested
      ? { ...(isRecord(observedStatus) ? observedStatus : {}), status: "cancelled" }
      : observedStatus;
    const terminalStatus = textToSqlRunTerminalStatus(status);
    return {
      kind: requested || terminalStatus === "cancelled"
        ? "cancelled" as const
        : terminalStatus
          ? "terminal" as const
          : "not_confirmed" as const,
      requested,
      terminalStatus,
      status,
    };
  } finally {
    inFlight.current = false;
  }
}

type TextToSqlPollScheduler = (
  tick: () => Promise<void>,
) => () => void;

export function startTextToSqlStatusPolling(
  loadStatus: () => Promise<unknown>,
  schedule: TextToSqlPollScheduler,
) {
  let stopped = false;
  let cancelScheduled = () => {};
  const stop = () => {
    if (stopped) return;
    stopped = true;
    cancelScheduled();
  };
  const tick = async () => {
    if (stopped) return;
    const status = await loadStatus();
    if (textToSqlRunTerminalStatus(status)) stop();
  };
  cancelScheduled = schedule(tick);
  return stop;
}
