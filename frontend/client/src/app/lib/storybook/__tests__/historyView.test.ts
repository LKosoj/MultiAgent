import { describe, expect, it } from "vitest";

import { historyRows } from "../historyView";

describe("история прогонов", () => {
  it("режим, место и итог словами", () => {
    const [a, b] = historyRows([
      { run_id: "1", mode: "full", surface: "tk", started_at: "2026-11-03T10:05:00", finished_at: null, outcome: "failed", unfinished: false, resumable: true },
      { run_id: "2", mode: null, surface: null, started_at: null, finished_at: null, outcome: null, unfinished: true, resumable: false },
    ]);
    expect(a).toMatchObject({ mode: "Весь пайплайн", where: "Приложение", result: "Сбой", note: "можно продолжить" });
    expect(a.duration).toBe("—");
    expect(b).toMatchObject({ mode: "—", where: "—", result: "не завершён", note: null });
  });
  it("длительность считается по началу и концу", () => {
    const [r] = historyRows([
      { run_id: "3", mode: "full", surface: "react", started_at: "2026-11-03T10:00:00", finished_at: "2026-11-03T10:05:00", outcome: "completed", unfinished: false, resumable: false },
    ]);
    expect(r.duration).toBe("5 мин");
  });
});
