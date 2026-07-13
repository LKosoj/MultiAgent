import { HttpAgent } from "@ag-ui/client";
import { describe, expect, it } from "vitest";

import {
  adoptTextToSqlFollow,
  closeTextToSqlPendingActions,
  createTextToSqlFollowRegistry,
  ensureTextToSqlFollowRegistryOpen,
  resolveTextToSqlStart,
  prepareTextToSqlStart,
  resolvePendingRunFailureRequestId,
  resolvePendingServiceResultRequestId,
  selectServiceActionAgent,
  stopTextToSqlFollows,
  TextToSqlStartResponseError,
  takeTextToSqlPendingAction,
  trackTextToSqlFollow,
} from "../textToSqlStart";

describe("Text-to-SQL start and follow", () => {
  it("reuses one idempotency key for the same pending transport retry", () => {
    let generated = 0;
    const createKey = () => `key-${++generated}`;
    const first = prepareTextToSqlStart({ query: "count orders" }, null, createKey);
    const retry = prepareTextToSqlStart(
      { query: "count orders" },
      first.attempt,
      createKey,
    );
    const changed = prepareTextToSqlStart(
      { query: "count customers" },
      retry.attempt,
      createKey,
    );

    expect(retry.payload.idempotency_key).toBe(first.payload.idempotency_key);
    expect(changed.payload.idempotency_key).not.toBe(first.payload.idempotency_key);
  });

  it("never correlates an old late result to a newer Text-to-SQL request", () => {
    const pending = new Map([
      ["timed-out-request", { action: "presets.text_to_sql.generate" }],
    ]);
    pending.delete("timed-out-request");
    pending.set("new-request", { action: "presets.text_to_sql.generate" });

    expect(resolvePendingServiceResultRequestId(pending, {
      action: "presets.text_to_sql.generate",
      requestId: "timed-out-request",
    })).toBeUndefined();
    expect(resolvePendingServiceResultRequestId(pending, {
      action: "presets.text_to_sql.generate",
      requestId: "new-request",
    })).toBe("new-request");
  });

  it("does not correlate an ambiguous replay or relax generic correlation", () => {
    const ambiguous = new Map([
      ["request-a", { action: "presets.text_to_sql.generate" }],
      ["request-b", { action: "presets.text_to_sql.generate" }],
    ]);
    const generic = new Map([
      ["new-request", { action: "agents.list" }],
    ]);

    expect(resolvePendingServiceResultRequestId(ambiguous, {
      action: "presets.text_to_sql.generate",
      requestId: "old-request",
    })).toBeUndefined();
    expect(resolvePendingServiceResultRequestId(generic, {
      action: "agents.list",
      requestId: "old-request",
    })).toBeUndefined();
    expect(resolvePendingServiceResultRequestId(generic, {
      action: "agents.list",
      requestId: "new-request",
    })).toBe("new-request");
  });

  it("does not correlate an exact request id with a mismatched action", () => {
    const pending = new Map([
      ["request-1", { action: "presets.text_to_sql.generate" }],
    ]);

    expect(resolvePendingServiceResultRequestId(pending, {
      action: "agents.list",
      requestId: "request-1",
    })).toBeUndefined();
  });

  it("uses the original agent for ordinary service actions", () => {
    let cloneCount = 0;
    const agent = {
      clone: () => {
        cloneCount += 1;
        return { clone: agent.clone };
      },
    };

    expect(selectServiceActionAgent(agent, false)).toBe(agent);
    expect(cloneCount).toBe(0);
  });

  it("creates a fresh agent clone for every background follow", () => {
    let cloneCount = 0;
    const agent = {
      clone: () => {
        cloneCount += 1;
        return { clone: agent.clone, cloneNumber: cloneCount };
      },
    };

    const first = selectServiceActionAgent(agent, true);
    const second = selectServiceActionAgent(agent, true);

    expect(first).not.toBe(agent);
    expect(second).not.toBe(agent);
    expect(second).not.toBe(first);
    expect(cloneCount).toBe(2);
  });

  it("keeps existing event subscribers on an isolated HTTP agent clone", () => {
    const agent = new HttpAgent({ url: "http://localhost/agent" });
    const subscriber = { onRunFailed: () => undefined };
    agent.subscribe(subscriber);

    const isolated = selectServiceActionAgent(agent, true);
    isolated.subscribe({});

    expect(isolated.subscribers).toEqual([subscriber, {}]);
    expect(agent.subscribers).toEqual([subscriber]);
  });

  it("stops only the isolated HTTP agent transport", async () => {
    const agent = new HttpAgent({ url: "http://localhost/agent" });
    const isolated = selectServiceActionAgent(agent, true);
    const follow = new Promise<void>((resolve) => {
      isolated.abortController.signal.addEventListener("abort", () => resolve());
    });
    const follows = createTextToSqlFollowRegistry();
    const handle = trackTextToSqlFollow(
      follows,
      "request-1",
      isolated,
      follow,
    );

    await handle.stop();

    expect(isolated.abortController.signal.aborted).toBe(true);
    expect(agent.abortController.signal.aborted).toBe(false);
    expect(follows.follows.size).toBe(0);
  });

  it("registers the isolated follow before the start request settles", async () => {
    let finishFollow: (() => void) | undefined;
    const follow = new Promise<void>((resolve) => {
      finishFollow = resolve;
    });
    const follows = createTextToSqlFollowRegistry();
    const handle = trackTextToSqlFollow(
      follows,
      "request-1",
      { abortRun: () => undefined },
      follow,
    );

    expect(follows.follows.get("request-1")).toBe(handle);
    await expect(resolveTextToSqlStart(
      Promise.resolve({ run_id: "run-1" }),
      follows,
      "request-1",
      handle,
    )).resolves.toEqual({ run_id: "run-1" });
    expect(follows.follows.get("run-1")).toBe(handle);

    finishFollow?.();
    await handle.settled;
    expect(follows.follows.size).toBe(0);
  });

  it("deduplicates retries that resolve to the same durable run", async () => {
    let finishFirst: (() => void) | undefined;
    const firstFollow = new Promise<void>((resolve) => {
      finishFirst = resolve;
    });
    let rejectSecond: ((error: Error) => void) | undefined;
    const secondFollow = new Promise<void>((_resolve, reject) => {
      rejectSecond = reject;
    });
    let secondAbortCount = 0;
    const follows = createTextToSqlFollowRegistry();
    const first = trackTextToSqlFollow(
      follows,
      "request-1",
      { abortRun: () => undefined },
      firstFollow,
    );
    trackTextToSqlFollow(
      follows,
      "request-2",
      {
        abortRun: () => {
          secondAbortCount += 1;
          rejectSecond?.(new Error("duplicate follow"));
        },
      },
      secondFollow,
    );

    await adoptTextToSqlFollow(follows, "request-1", "run-1");
    await adoptTextToSqlFollow(follows, "request-2", "run-1");

    expect(follows.follows.size).toBe(1);
    expect(follows.follows.get("run-1")).toBe(first);
    expect(secondAbortCount).toBe(1);

    finishFirst?.();
    await first.settled;
    expect(follows.follows.size).toBe(0);
  });

  it.each([
    new Error("Таймаут запроса: presets.text_to_sql.generate"),
    new Error("start rejected"),
  ])("stops the isolated follow when start fails: %s", async (startError) => {
    let rejectFollow: ((error: Error) => void) | undefined;
    const follow = new Promise<void>((_resolve, reject) => {
      rejectFollow = reject;
    });
    let abortCount = 0;
    const follows = createTextToSqlFollowRegistry();
    const handle = trackTextToSqlFollow(
      follows,
      "request-1",
      {
        abortRun: () => {
          abortCount += 1;
          rejectFollow?.(new Error("local stream stopped"));
        },
      },
      follow,
    );

    await expect(resolveTextToSqlStart(
      Promise.reject(startError),
      follows,
      "request-1",
      handle,
    )).rejects.toBe(startError);
    expect(abortCount).toBe(1);
    expect(follows.follows.size).toBe(0);
  });

  it("consumes an early follow rejection without an unhandled promise", async () => {
    const follows = createTextToSqlFollowRegistry();
    const handle = trackTextToSqlFollow(
      follows,
      "request-1",
      { abortRun: () => undefined },
      Promise.reject(new Error("transport closed")),
    );

    await expect(handle.settled).resolves.toBeUndefined();
    expect(follows.follows.size).toBe(0);
  });

  it("stops every isolated local follow during page cleanup", async () => {
    const follows = createTextToSqlFollowRegistry();
    const aborted: string[] = [];
    for (const requestId of ["request-1", "request-2"]) {
      let rejectFollow: ((error: Error) => void) | undefined;
      const follow = new Promise<void>((_resolve, reject) => {
        rejectFollow = reject;
      });
      trackTextToSqlFollow(
        follows,
        requestId,
        {
          abortRun: () => {
            aborted.push(requestId);
            rejectFollow?.(new Error("unmounted"));
          },
        },
        follow,
      );
    }

    await stopTextToSqlFollows(follows);

    expect(aborted).toEqual(["request-1", "request-2"]);
    expect(follows.follows.size).toBe(0);
  });

  it("settles a late exact service result after close without UI writes", async () => {
    const follows = createTextToSqlFollowRegistry();
    follows.closed = true;
    const cleared: number[] = [];
    let uiWrites = 0;
    let resolveResult: ((value: unknown) => void) | undefined;
    const result = new Promise<unknown>((resolve) => {
      resolveResult = resolve;
    });
    const pending = new Map([
      ["request-1", {
        action: "presets.text_to_sql.generate",
        resolve: (value: unknown) => resolveResult?.(value),
        reject: () => undefined,
        timeoutId: 31,
      }],
    ]);
    const requestId = resolvePendingServiceResultRequestId(pending, {
      action: "presets.text_to_sql.generate",
      requestId: "request-1",
    });
    const action = takeTextToSqlPendingAction(
      pending,
      requestId,
      (timeoutId) => { cleared.push(timeoutId); },
    );
    action?.resolve({ run_id: "run-1" });
    if (!follows.closed) uiWrites += 1;

    await expect(result).resolves.toEqual({ run_id: "run-1" });
    expect(cleared).toEqual([31]);
    expect(pending.size).toBe(0);
    expect(uiWrites).toBe(0);
  });

  it("drains a pending start before abort callbacks after close", async () => {
    const follows = createTextToSqlFollowRegistry();
    const cleared: number[] = [];
    let uiWrites = 0;
    let rejectStart: ((error: Error) => void) | undefined;
    const started = new Promise<unknown>((_resolve, reject) => {
      rejectStart = reject;
    });
    const rejected = started.catch((error) => error);
    const pending = new Map([
      ["request-1", {
        action: "presets.text_to_sql.generate",
        resolve: () => undefined,
        reject: (error: Error) => rejectStart?.(error),
        timeoutId: 32,
      }],
    ]);
    let rejectFollow: ((error: Error) => void) | undefined;
    let aborts = 0;
    trackTextToSqlFollow(
      follows,
      "request-1",
      {
        abortRun: () => {
          aborts += 1;
          const requestId = resolvePendingRunFailureRequestId(pending, {
            forwardedProps: {
              service_action: "presets.text_to_sql.generate",
              service_payload: { __request_id: "request-1" },
            },
          });
          const action = takeTextToSqlPendingAction(
            pending,
            requestId,
            (timeoutId) => { cleared.push(timeoutId); },
          );
          action?.reject(new Error("transport aborted"));
          if (!follows.closed) uiWrites += 1;
          rejectFollow?.(new Error("follow aborted"));
        },
      },
      new Promise<void>((_resolve, reject) => { rejectFollow = reject; }),
    );

    const closeError = new Error("page disposed");
    closeTextToSqlPendingActions(
      follows,
      pending,
      closeError,
      (timeoutId) => { cleared.push(timeoutId); },
    );
    await stopTextToSqlFollows(follows);

    await expect(rejected).resolves.toBe(closeError);
    expect(cleared).toEqual([32]);
    expect(pending.size).toBe(0);
    expect(aborts).toBe(1);
    expect(uiWrites).toBe(0);
  });

  it.each([
    {},
    { run_id: "" },
    { run_id: "   " },
    { run_id: " run-1 " },
    { run_id: 7 },
    { run_id: { value: "run-1" } },
  ])("rejects and stops a start response with invalid run_id %#", async (response) => {
    const follows = createTextToSqlFollowRegistry();
    let rejectFollow: ((error: Error) => void) | undefined;
    const follow = new Promise<void>((_resolve, reject) => {
      rejectFollow = reject;
    });
    let abortCount = 0;
    const handle = trackTextToSqlFollow(
      follows,
      "request-1",
      {
        abortRun: () => {
          abortCount += 1;
          rejectFollow?.(new Error("invalid start stopped"));
        },
      },
      follow,
    );

    await expect(resolveTextToSqlStart(
      Promise.resolve(response),
      follows,
      "request-1",
      handle,
    )).rejects.toBeInstanceOf(TextToSqlStartResponseError);
    expect(abortCount).toBe(1);
    expect(follows.follows.size).toBe(0);
  });

  it("preserves the original start error when follow cleanup throws", async () => {
    const follows = createTextToSqlFollowRegistry();
    const startError = new Error("start timed out");
    let abortCount = 0;
    const handle = trackTextToSqlFollow(
      follows,
      "request-1",
      {
        abortRun: () => {
          abortCount += 1;
          throw new Error("abort failed");
        },
      },
      Promise.resolve(),
    );

    await expect(resolveTextToSqlStart(
      Promise.reject(startError),
      follows,
      "request-1",
      handle,
    )).rejects.toBe(startError);
    await expect(handle.stop()).resolves.toBeUndefined();
    expect(abortCount).toBe(1);
    expect(follows.follows.size).toBe(0);
  });

  it("detaches and settles a pending follow when abort throws", async () => {
    const follows = createTextToSqlFollowRegistry();
    let finishFollow: (() => void) | undefined;
    const follow = new Promise<void>((resolve) => {
      finishFollow = resolve;
    });
    let abortCount = 0;
    let detachCount = 0;
    const handle = trackTextToSqlFollow(
      follows,
      "request-1",
      {
        abortRun: () => {
          abortCount += 1;
          throw new Error("abort failed");
        },
        detachActiveRun: async () => {
          detachCount += 1;
          finishFollow?.();
        },
      },
      follow,
    );

    await expect(handle.stop()).resolves.toBeUndefined();
    await handle.settled;
    expect(abortCount).toBe(1);
    expect(detachCount).toBe(1);
    expect(follows.follows.size).toBe(0);
  });

  it("keeps a successful start when duplicate follow cleanup throws", async () => {
    const follows = createTextToSqlFollowRegistry();
    let finishFirst: (() => void) | undefined;
    const first = trackTextToSqlFollow(
      follows,
      "request-1",
      { abortRun: () => undefined },
      new Promise<void>((resolve) => { finishFirst = resolve; }),
    );
    await adoptTextToSqlFollow(follows, "request-1", "run-1");
    let duplicateAbortCount = 0;
    const duplicate = trackTextToSqlFollow(
      follows,
      "request-2",
      {
        abortRun: () => {
          duplicateAbortCount += 1;
          throw new Error("duplicate abort failed");
        },
      },
      Promise.resolve(),
    );

    await expect(resolveTextToSqlStart(
      Promise.resolve({ run_id: "run-1" }),
      follows,
      "request-2",
      duplicate,
    )).resolves.toEqual({ run_id: "run-1" });
    expect(follows.follows.get("run-1")).toBe(first);
    expect(duplicateAbortCount).toBe(1);
    finishFirst?.();
    await first.settled;
  });

  it("closes atomically and aborts a late registration exactly once", async () => {
    const follows = createTextToSqlFollowRegistry();
    await stopTextToSqlFollows(follows);
    let rejectFollow: ((error: Error) => void) | undefined;
    let abortCount = 0;
    const handle = trackTextToSqlFollow(
      follows,
      "late-request",
      {
        abortRun: () => {
          abortCount += 1;
          rejectFollow?.(new Error("disposed"));
        },
      },
      new Promise<void>((_resolve, reject) => { rejectFollow = reject; }),
    );

    await handle.settled;
    await handle.stop();
    expect(abortCount).toBe(1);
    expect(follows.follows.size).toBe(0);
  });

  it("attempts every cleanup when the first abort throws", async () => {
    const follows = createTextToSqlFollowRegistry();
    const attempts: string[] = [];
    for (const requestId of ["first", "second"]) {
      trackTextToSqlFollow(
        follows,
        requestId,
        {
          abortRun: () => {
            attempts.push(requestId);
            if (requestId === "first") throw new Error("abort failed");
          },
        },
        Promise.resolve(),
      );
    }

    await expect(stopTextToSqlFollows(follows)).resolves.toBeUndefined();
    expect(attempts).toEqual(["first", "second"]);
    expect(follows.follows.size).toBe(0);
  });

  it("rejects a queued action after disposal before side effects", async () => {
    const follows = createTextToSqlFollowRegistry();
    let pendingCreated = 0;
    let networkCalls = 0;
    const queued = Promise.resolve().then(() => {
      ensureTextToSqlFollowRegistryOpen(follows);
      pendingCreated += 1;
      networkCalls += 1;
    });
    await stopTextToSqlFollows(follows);

    await expect(queued).rejects.toThrow("Text-to-SQL follow registry is closed");
    expect(pendingCreated).toBe(0);
    expect(networkCalls).toBe(0);
  });

  it("correlates a run failure only to its matching request id", () => {
    const pending = new Map([
      ["request-a", { action: "presets.text_to_sql.generate" }],
      ["request-b", { action: "presets.text_to_sql.generate" }],
    ]);

    expect(resolvePendingRunFailureRequestId(pending, {
      forwardedProps: {
        service_action: "presets.text_to_sql.generate",
        service_payload: { __request_id: "request-b" },
      },
    })).toBe("request-b");
  });

  it("ignores a stale run failure request id", () => {
    const pending = new Map([
      ["current-request", { action: "presets.text_to_sql.generate" }],
    ]);

    expect(resolvePendingRunFailureRequestId(pending, {
      forwardedProps: {
        service_action: "presets.text_to_sql.generate",
        service_payload: { __request_id: "old-request" },
      },
    })).toBeUndefined();
  });

  it("rejects the same request id for a different run action", () => {
    const pending = new Map([
      ["request-1", { action: "presets.text_to_sql.generate" }],
    ]);

    expect(resolvePendingRunFailureRequestId(pending, {
      forwardedProps: {
        service_action: "agents.list",
        service_payload: { __request_id: "request-1" },
      },
    })).toBeUndefined();
  });

  it.each([
    undefined,
    {},
    { forwardedProps: null },
    { forwardedProps: { service_payload: null } },
    { forwardedProps: { service_payload: {} } },
    { forwardedProps: { service_payload: { __request_id: 123 } } },
    {
      forwardedProps: {
        service_action: 7,
        service_payload: { __request_id: "request-a" },
      },
    },
    {
      forwardedProps: {
        service_payload: { __request_id: "request-a" },
      },
    },
  ])("does not guess a pending action for malformed run input %#", (input) => {
    const pending = new Map([
      ["request-a", { action: "presets.text_to_sql.generate" }],
      ["request-b", { action: "presets.text_to_sql.generate" }],
    ]);

    expect(resolvePendingRunFailureRequestId(pending, input)).toBeUndefined();
  });
});
