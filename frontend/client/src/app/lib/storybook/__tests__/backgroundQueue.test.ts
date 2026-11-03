import { describe, expect, it } from "vitest";

import { createBackgroundQueue } from "../backgroundQueue";

const tick = async () => {
  for (let i = 0; i < 10; i++) await Promise.resolve();
};

describe("createBackgroundQueue", () => {
  it("выполняет задачи по одному в порядке постановки", async () => {
    const q = createBackgroundQueue({ gapMs: 0 });
    const log: string[] = [];
    const slow = q.run(async () => {
      log.push("a-start");
      await tick();
      log.push("a-end");
      return 1;
    });
    const fast = q.run(async () => {
      log.push("b");
      return 2;
    });
    expect(await Promise.all([slow, fast])).toEqual([1, 2]);
    expect(log).toEqual(["a-start", "a-end", "b"]);
  });

  it("ошибка одной задачи не ломает очередь", async () => {
    const q = createBackgroundQueue({ gapMs: 0 });
    const bad = q.run(async () => {
      throw new Error("x");
    });
    await expect(bad).rejects.toThrow("x");
    await expect(q.run(async () => "ok")).resolves.toBe("ok");
  });

  it("ждёт, пока идут действия пользователя", async () => {
    const q = createBackgroundQueue({ gapMs: 0 });
    q.userStart();
    let ran = false;
    const p = q.run(async () => {
      ran = true;
    });
    await tick();
    expect(ran).toBe(false);
    expect(q.userCount()).toBe(1);
    q.userEnd();
    await p;
    expect(ran).toBe(true);
  });

  it("делает паузу между задачами", async () => {
    const pauses: number[] = [];
    const q = createBackgroundQueue({ gapMs: 150, sleep: async (ms) => void pauses.push(ms) });
    await q.run(async () => 1);
    await q.run(async () => 2);
    expect(pauses).toEqual([150, 150]);
  });
});
