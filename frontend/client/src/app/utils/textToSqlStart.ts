export type PendingTextToSqlStart = {
  signature: string;
  idempotencyKey: string;
};

export function selectServiceActionAgent<T extends { clone: () => T }>(
  agent: T,
  isolatedFollow: boolean,
): T {
  return isolatedFollow ? agent.clone() : agent;
}

export function resolvePendingRunFailureRequestId(
  pending: ReadonlyMap<string, { action: string }>,
  input: unknown,
): string | undefined {
  if (!input || typeof input !== "object") {
    return undefined;
  }
  const forwardedProps = (input as { forwardedProps?: unknown }).forwardedProps;
  if (!forwardedProps || typeof forwardedProps !== "object") {
    return undefined;
  }
  const action = (forwardedProps as { service_action?: unknown }).service_action;
  const servicePayload = (forwardedProps as { service_payload?: unknown }).service_payload;
  if (!servicePayload || typeof servicePayload !== "object") {
    return undefined;
  }
  const requestId = (servicePayload as { __request_id?: unknown }).__request_id;
  return typeof action === "string"
    && typeof requestId === "string"
    && pending.get(requestId)?.action === action
    ? requestId
    : undefined;
}

export function resolvePendingServiceResultRequestId(
  pending: ReadonlyMap<string, { action: string }>,
  result: { action?: string; requestId?: string },
): string | undefined {
  return result.requestId
    && pending.get(result.requestId)?.action === result.action
    ? result.requestId
    : undefined;
}

export function prepareTextToSqlStart(
  payload: Record<string, unknown>,
  pending: PendingTextToSqlStart | null,
  createKey: () => string,
) {
  const signature = JSON.stringify(payload);
  const attempt = pending?.signature === signature
    ? pending
    : { signature, idempotencyKey: createKey() };
  return {
    attempt,
    payload: {
      ...payload,
      idempotency_key: attempt.idempotencyKey,
    },
  };
}

type TextToSqlFollowAgent = {
  abortRun: () => void;
  detachActiveRun?: () => Promise<void>;
};

const TEXT_TO_SQL_FOLLOW_STOP_TIMEOUT_MS = 1000;

export type TextToSqlStartResponse = Record<string, unknown> & {
  run_id: string;
};

export type TextToSqlFollowHandle = {
  settled: Promise<void>;
  stop: () => Promise<void>;
};

export type TextToSqlFollowRegistry = {
  closed: boolean;
  follows: Map<string, TextToSqlFollowHandle>;
};

export class TextToSqlStartResponseError extends Error {
  constructor() {
    super("Text-to-SQL start response must contain an unpadded, non-empty string run_id");
    this.name = "TextToSqlStartResponseError";
  }
}

export function takeTextToSqlPendingAction<T extends { timeoutId?: number }>(
  pending: Map<string, T>,
  requestId: string | undefined,
  clearTimer: (timeoutId: number) => void,
): T | undefined {
  if (!requestId) return undefined;
  const action = pending.get(requestId);
  if (!action) return undefined;
  pending.delete(requestId);
  if (action.timeoutId !== undefined) clearTimer(action.timeoutId);
  return action;
}

export function closeTextToSqlPendingActions<
  T extends { reject: (error: Error) => void; timeoutId?: number },
>(
  registry: TextToSqlFollowRegistry,
  pending: Map<string, T>,
  error: Error,
  clearTimer: (timeoutId: number) => void,
): void {
  registry.closed = true;
  for (const requestId of [...pending.keys()]) {
    takeTextToSqlPendingAction(pending, requestId, clearTimer)?.reject(error);
  }
}

function waitForTextToSqlFollowStop(
  pending: Promise<unknown>[],
): Promise<void> {
  return new Promise((resolve) => {
    let finished = false;
    const finish = () => {
      if (finished) return;
      finished = true;
      clearTimeout(timeoutId);
      resolve();
    };
    const timeoutId = setTimeout(finish, TEXT_TO_SQL_FOLLOW_STOP_TIMEOUT_MS);
    void Promise.allSettled(pending).then(finish);
  });
}

export function createTextToSqlFollowRegistry(): TextToSqlFollowRegistry {
  return { closed: false, follows: new Map() };
}

export function ensureTextToSqlFollowRegistryOpen(
  registry: TextToSqlFollowRegistry,
): void {
  if (registry.closed) throw new Error("Text-to-SQL follow registry is closed");
}

export function trackTextToSqlFollow(
  registry: TextToSqlFollowRegistry,
  requestId: string,
  agent: TextToSqlFollowAgent,
  follow: Promise<void>,
): TextToSqlFollowHandle {
  let stopPromise: Promise<void> | undefined;
  const remove = () => {
    for (const [key, current] of registry.follows) {
      if (current === handle) registry.follows.delete(key);
    }
  };
  const handle: TextToSqlFollowHandle = {
    settled: follow
      .catch(() => undefined)
      .finally(remove),
    stop: () => {
      if (!stopPromise) {
        stopPromise = Promise.resolve();
        remove();
        const pending: Promise<unknown>[] = [handle.settled];
        try {
          agent.abortRun();
        } catch {
          if (agent.detachActiveRun) {
            pending.push(Promise.resolve().then(() => agent.detachActiveRun?.()));
          }
        }
        stopPromise = waitForTextToSqlFollowStop(pending);
      }
      return stopPromise;
    },
  };
  if (registry.closed) {
    void handle.stop();
  } else {
    registry.follows.set(requestId, handle);
  }
  return handle;
}

export async function adoptTextToSqlFollow(
  registry: TextToSqlFollowRegistry,
  requestId: string,
  runId: string,
): Promise<void> {
  const follow = registry.follows.get(requestId);
  if (!follow) {
    return;
  }
  const existing = registry.follows.get(runId);
  registry.follows.delete(requestId);
  if (registry.closed) {
    await follow.stop();
    return;
  }
  if (existing && existing !== follow) {
    await follow.stop();
    return;
  }
  registry.follows.set(runId, follow);
}

export async function resolveTextToSqlStart(
  started: Promise<unknown>,
  registry: TextToSqlFollowRegistry,
  requestId: string,
  follow: TextToSqlFollowHandle,
): Promise<TextToSqlStartResponse> {
  let value: unknown;
  try {
    value = await started;
  } catch (error) {
    await follow.stop();
    throw error;
  }
  const runId = value && typeof value === "object"
    ? (value as { run_id?: unknown }).run_id
    : undefined;
  if (
    typeof runId !== "string"
    || !runId.trim()
    || runId !== runId.trim()
  ) {
    await follow.stop();
    throw new TextToSqlStartResponseError();
  }
  await adoptTextToSqlFollow(registry, requestId, runId);
  return value as TextToSqlStartResponse;
}

export async function stopTextToSqlFollows(
  registry: TextToSqlFollowRegistry,
): Promise<void> {
  registry.closed = true;
  const active = [...new Set(registry.follows.values())];
  registry.follows.clear();
  await Promise.allSettled(active.map((follow) => follow.stop()));
}
