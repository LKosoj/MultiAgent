import { describe, expect, it } from "vitest";

import { rerunView, runControlView } from "../runControl";

describe("runControlView", () => {
  it("идёт прогон: Пауза и Стоп доступны", () => {
    expect(runControlView("running")).toEqual({
      pauseLabel: "Пауза",
      pauseAction: "pause",
      pauseEnabled: true,
      stopEnabled: true,
    });
  });
  it("пауза запрошена или включена: Продолжить", () => {
    for (const s of ["pause_requested", "paused"] as const) {
      const v = runControlView(s);
      expect(v.pauseLabel).toBe("Продолжить");
      expect(v.pauseAction).toBe("resume");
      expect(v.pauseEnabled && v.stopEnabled).toBe(true);
    }
  });
  it("остановка, простой и итоговые состояния: всё выключено", () => {
    for (const s of ["stopping", "idle", "completed", "failed", "interrupted", undefined] as const) {
      const v = runControlView(s);
      expect(v.pauseEnabled || v.stopEnabled).toBe(false);
    }
  });
});

describe("rerunView", () => {
  it("нет прогона или идёт прогон: кнопка отключена, подсказка объясняет почему", () => {
    expect(rerunView("idle")).toEqual({ enabled: false, hint: "Прогон ещё не запускался", mode: null });
    expect(rerunView(undefined).enabled).toBe(false);
    for (const s of ["running", "pause_requested", "paused", "stopping"] as const) {
      expect(rerunView(s)).toEqual({ enabled: false, hint: "Недоступно, пока идёт прогон", mode: null });
    }
  });
  it("после сбоя продолжаем, после завершения запускаем заново", () => {
    expect(rerunView("failed").mode).toBe("resume");
    expect(rerunView("interrupted", true).mode).toBe("resume");
    expect(rerunView("interrupted", false).mode).toBe("full");
    expect(rerunView("completed")).toMatchObject({ enabled: true, mode: "full" });
  });
});
