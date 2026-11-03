import { describe, expect, it, vi } from "vitest";

import { createStatusPoller } from "../statusPoll";

type Timer = { fn: () => void; ms: number; id: number };

const setup = (opts: { hidden?: () => boolean } = {}) => {
  let nextId = 1;
  let timers: Timer[] = [];
  const resolvers: { resolve: (v: string) => void; reject: (e: unknown) => void; manual: boolean }[] = [];
  const fetch = vi.fn((info: { manual: boolean }) =>
    new Promise<string>((resolve, reject) => resolvers.push({ resolve, reject, manual: info.manual })),
  );
  const onData = vi.fn();
  const onError = vi.fn();
  let interval = 2000;
  const poller = createStatusPoller<string>({
    fetch,
    onData,
    onError,
    intervalFor: () => interval,
    setTimer: (fn, ms) => {
      const t = { fn, ms, id: nextId++ };
      timers.push(t);
      return t.id;
    },
    clearTimer: (h) => {
      timers = timers.filter((t) => t.id !== h);
    },
    isHidden: opts.hidden,
  });
  const flush = async () => {
    for (let i = 0; i < 5; i++) await Promise.resolve();
  };
  return {
    poller, fetch, onData, onError, resolvers, flush,
    timers: () => timers,
    setInterval: (n: number) => (interval = n),
    fire: () => {
      const t = timers.shift();
      t?.fn();
    },
  };
};

describe("createStatusPoller", () => {
  it("single-flight: пока нет ответа, второй запрос не уходит", async () => {
    const h = setup();
    h.poller.start();
    h.poller.notifyVisible();
    await h.flush();
    expect(h.fetch).toHaveBeenCalledTimes(1);
    h.resolvers[0].resolve("a");
    await h.flush();
    expect(h.onData).toHaveBeenCalledWith("a");
    expect(h.timers()).toHaveLength(1);
    expect(h.timers()[0].ms).toBe(2000);
  });

  it("интервал берётся из данных (2 с при прогоне, 10 с в покое)", async () => {
    const h = setup();
    h.setInterval(10000);
    h.poller.start();
    h.resolvers[0].resolve("a");
    await h.flush();
    expect(h.timers()[0].ms).toBe(10000);
  });

  it("не опрашивает скрытую вкладку, по возвращению делает один запрос", async () => {
    let hidden = true;
    const h = setup({ hidden: () => hidden });
    h.poller.start();
    await h.flush();
    expect(h.fetch).not.toHaveBeenCalled();
    hidden = false;
    h.poller.notifyVisible();
    await h.flush();
    expect(h.fetch).toHaveBeenCalledTimes(1);
  });

  it("при ошибках интервал растёт и ограничен 30 с, после успеха сбрасывается", async () => {
    const h = setup();
    h.poller.start();
    h.resolvers[0].reject(new Error("net"));
    await h.flush();
    expect(h.onError).toHaveBeenCalledTimes(1);
    expect(h.timers()[0].ms).toBe(4000);
    h.fire();
    h.resolvers[1].reject(new Error("net"));
    await h.flush();
    expect(h.timers()[0].ms).toBe(8000);
    for (let i = 2; i < 8; i++) {
      h.fire();
      h.resolvers[i].reject(new Error("net"));
      await h.flush();
    }
    expect(h.timers()[0].ms).toBe(30000);
    h.fire();
    h.resolvers[8].resolve("ok");
    await h.flush();
    expect(h.timers()[0].ms).toBe(2000);
  });

  it("refreshNow делает ручной запрос; во время запроса запоминается и идёт следом", async () => {
    const h = setup();
    h.poller.start();
    await h.flush();
    h.poller.refreshNow();
    await h.flush();
    expect(h.fetch).toHaveBeenCalledTimes(1);
    h.resolvers[0].resolve("a");
    await h.flush();
    expect(h.fetch).toHaveBeenCalledTimes(2);
    expect(h.resolvers[1].manual).toBe(true);
    expect(h.resolvers[0].manual).toBe(false);
  });

  it("stop гасит таймер и отбрасывает позднее решение запроса", async () => {
    const h = setup();
    h.poller.start();
    await h.flush();
    h.poller.stop();
    h.resolvers[0].resolve("поздний");
    await h.flush();
    expect(h.onData).not.toHaveBeenCalled();
    expect(h.timers()).toHaveLength(0);
    h.poller.refreshNow();
    h.poller.notifyVisible();
    await h.flush();
    expect(h.fetch).toHaveBeenCalledTimes(1);
  });

  it("ответ прошлого запуска не попадает в данные нового", async () => {
    const h = setup();
    h.poller.start();
    await h.flush();
    h.poller.stop();
    h.poller.start();
    await h.flush();
    expect(h.fetch).toHaveBeenCalledTimes(2);
    h.resolvers[0].resolve("чужой");
    await h.flush();
    expect(h.onData).not.toHaveBeenCalled();
    h.resolvers[1].resolve("свой");
    await h.flush();
    expect(h.onData).toHaveBeenCalledTimes(1);
    expect(h.onData).toHaveBeenCalledWith("свой");
  });
});
