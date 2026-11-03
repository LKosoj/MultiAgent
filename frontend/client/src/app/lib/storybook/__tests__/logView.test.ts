import { describe, expect, it } from "vitest";

import { StudioError } from "../client";
import { createLogLoader, logText, type LogState } from "../logView";

const make = (fetch: (runId: string, after: number) => Promise<{ status: "success"; lines: string[]; offset: number; truncated: boolean }>) => {
  const seen: LogState[] = [];
  const loader = createLogLoader({ fetch, onChange: (s) => seen.push(s) });
  return { loader, seen };
};

describe("загрузка журнала", () => {
  it("догружает с последнего смещения", async () => {
    const calls: number[] = [];
    const { loader } = make(async (_r, after) => {
      calls.push(after);
      return { status: "success", lines: [`l${after}`], offset: after + 10, truncated: false };
    });
    await loader.poll("r1");
    await loader.poll("r1");
    expect(calls).toEqual([0, 10]);
    expect(loader.state().lines).toEqual(["l0", "l10"]);
    expect(logText(loader.state().lines)).toBe("l0\nl10");
  });
  it("новый прогон сбрасывает журнал", async () => {
    const { loader } = make(async (r) => ({ status: "success", lines: [r], offset: 5, truncated: false }));
    await loader.poll("r1");
    await loader.poll("r2");
    expect(loader.state()).toMatchObject({ runId: "r2", lines: ["r2"], offset: 5 });
    await loader.poll(null);
    expect(loader.state().lines).toEqual([]);
  });
  it("один запрос за раз", async () => {
    let n = 0;
    let release: () => void = () => undefined;
    const gate = new Promise<void>((r) => (release = r));
    const { loader } = make(async () => {
      n += 1;
      await gate;
      return { status: "success", lines: [], offset: 0, truncated: false };
    });
    const a = loader.poll("r1");
    const b = loader.poll("r1");
    release();
    await Promise.all([a, b]);
    expect(n).toBe(1);
  });
  it("журнал удалён: понятный текст", async () => {
    const { loader } = make(async () => {
      throw new StudioError("not_found", "нет файла");
    });
    await loader.poll("r1");
    expect(loader.state().error).toBe("Журнал этого прогона уже удалён");
  });
  it("«Очистить вид» скрывает показанное, но не сбрасывает смещение", async () => {
    const { loader } = make(async () => ({ status: "success", lines: ["x"], offset: 7, truncated: false }));
    await loader.poll("r1");
    loader.clear();
    expect(loader.state()).toMatchObject({ lines: [], offset: 7 });
  });
  it("усечение запоминается", async () => {
    const { loader } = make(async () => ({ status: "success", lines: [], offset: 1, truncated: true }));
    await loader.poll("r1");
    expect(loader.state().truncated).toBe(true);
  });
});

describe("смена прогона во время запроса", () => {
  it("новый прогон не ждёт старый запрос", async () => {
    let release: () => void = () => undefined;
    const calls: string[] = [];
    const { loader } = make(async (runId) => {
      calls.push(runId);
      if (runId === "old") await new Promise<void>((r) => (release = r));
      return { status: "success", lines: [`из ${runId}`], offset: 1, truncated: false };
    });
    const slow = loader.poll("old");
    await loader.poll("new");
    expect(calls).toEqual(["old", "new"]);
    expect(loader.state()).toMatchObject({ runId: "new", lines: ["из new"] });
    release();
    await slow;
    expect(loader.state()).toMatchObject({ runId: "new", lines: ["из new"] });
  });
  it("A → B → A: поздний ответ первого запроса A не дублирует строки", async () => {
    const gates: Array<() => void> = [];
    const { loader } = make(async (runId, after) => {
      if (runId === "a" && gates.length === 0) await new Promise<void>((r) => gates.push(r));
      return { status: "success", lines: [`${runId}@${after}`], offset: after + 1, truncated: false };
    });
    const slow = loader.poll("a");
    await loader.poll("b");
    await loader.poll("a");
    expect(loader.state()).toMatchObject({ runId: "a", lines: ["a@0"], offset: 1 });
    gates[0]();
    await slow;
    expect(loader.state()).toMatchObject({ runId: "a", lines: ["a@0"], offset: 1 });
  });
});
