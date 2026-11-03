import { describe, expect, it } from "vitest";

import type { StorybookClient } from "../client";
import { FilmAborted, createFilmLoader } from "../filmLoader";

const fake = (pending: number[], subsFail = false) => {
  let i = 0;
  const calls: string[] = [];
  const client = {
    call: async (id: string) => {
      calls.push(id);
      if (id === "media_subtitles") {
        if (subsFail) throw new Error("нет");
        return { cues: [] };
      }
      return { durations_pending: pending[Math.min(i++, pending.length - 1)] };
    },
  } as unknown as StorybookClient;
  return { client, calls };
};
const noSleep = async () => {};

describe("загрузка фильма", () => {
  it("повторяет, пока студия досчитывает длительности", async () => {
    const { client, calls } = fake([2, 1, 0]);
    const data = await createFilmLoader({ client, projectId: "p", sleep: noSleep }).load();
    expect(calls.filter((c) => c === "media_timeline")).toHaveLength(3);
    expect(data.timeline.durations_pending).toBe(0);
  });
  it("останавливается по лимиту повторов", async () => {
    const { client, calls } = fake([5]);
    await createFilmLoader({ client, projectId: "p", sleep: noSleep, maxRepeats: 2 }).load();
    expect(calls.filter((c) => c === "media_timeline")).toHaveLength(3);
  });
  it("сбой субтитров не фатален", async () => {
    const { client } = fake([0], true);
    const data = await createFilmLoader({ client, projectId: "p", sleep: noSleep }).load();
    expect(data.subtitlesFailed).toBe(true);
    expect(data.subtitles).toBeNull();
  });
  it("отмена до старта бросает FilmAborted", async () => {
    const { client } = fake([0]);
    const ac = new AbortController();
    ac.abort();
    await expect(createFilmLoader({ client, projectId: "p", signal: ac.signal }).load()).rejects.toBeInstanceOf(FilmAborted);
  });
});
