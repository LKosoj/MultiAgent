// «Повторить» и первая повторная попытка просят новый токен принудительно.
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import type { BackgroundQueue } from "../backgroundQueue";
import type { StorybookClient } from "../client";
import { playerStatus, runRetry } from "../playerModel";
import { stripToken, tokenOf, useShotMedia, type ShotMediaApi } from "../useShotMedia";
import { FilmPlayer } from "../../../components/sections/storybook/film/FilmPlayer";
import type { TimelineResult } from "../media";

// клиент, у которого каждый force-запрос меняет токен
const harness = (initialToken: string | null = null) => {
  let n = 0;
  let token = initialToken;
  const ensure = vi.fn().mockImplementation(async () => {
    n += 1;
    token = `tok${n}`;
  });
  const client = {
    ensureMediaToken: ensure,
    mediaUrl: (_p: string, rel: string) => (token ? `http://h/media?path=${rel}&t=${token}` : null),
    thumbUrl: () => null,
  } as unknown as StorybookClient;
  const background = { run: (task: () => Promise<unknown>) => task() } as unknown as BackgroundQueue;
  let api: ShotMediaApi | null = null;
  const Probe = () => {
    api = useShotMedia({ client, background, projectId: "p", enabled: false });
    return null;
  };
  renderToStaticMarkup(React.createElement(Probe));
  return { ensure, api: () => api as unknown as ShotMediaApi };
};

describe("повторное получение токена медиа", () => {
  it("refresh («Повторить») просит токен с force", () => {
    const h = harness();
    h.api().refresh();
    expect(h.ensure).toHaveBeenCalledWith("p", { force: true });
  });
  it("первая ошибка загрузки файла тоже просит токен с force", () => {
    const h = harness();
    h.api().onImageError({ currentTarget: { src: "http://h/a?t=old" } });
    expect(h.ensure).toHaveBeenCalledWith("p", { force: true });
  });
});

describe("защита от цикла force-токена", () => {
  it("адрес сравнивается без параметра t", () => {
    expect(stripToken("http://h/m?path=a.png&t=abc&v=1")).toBe("http://h/m?path=a.png&v=1");
    expect(stripToken("http://h/m?path=a.png&t=abc")).toBe("http://h/m?path=a.png");
    expect(stripToken("http://h/m?t=abc&path=a")).toBe("http://h/m?path=a");
    expect(tokenOf("http://h/m?path=a&t=abc")).toBe("abc");
    expect(tokenOf(null)).toBeNull();
  });
  it("20 битых картинок, токен меняется после каждого запроса: force не больше двух раз", async () => {
    const h = harness("tok0");
    // первая волна ошибок: все с текущим токеном
    for (let i = 0; i < 20; i += 1) h.api().onImageError({ currentTarget: { src: `http://h/media?path=img${i}.png&t=tok0` } });
    await new Promise((r) => setTimeout(r, 0));
    // после пересборки те же 20 адресов падают с новым токеном
    for (let round = 0; round < 3; round += 1) {
      for (let i = 0; i < 20; i += 1) h.api().onImageError({ currentTarget: { src: `http://h/media?path=img${i}.png&t=tok${round + 1}` } });
      await new Promise((r) => setTimeout(r, 0));
    }
    expect(h.ensure.mock.calls.length).toBeLessThanOrEqual(2);
    expect(h.ensure.mock.calls.length).toBeGreaterThanOrEqual(1);
  });
  it("токен в упавшем адресе устарел: force не нужен", () => {
    const h = harness("tok5");
    h.api().onImageError({ currentTarget: { src: "http://h/media?path=a.png&t=old" } });
    expect(h.ensure).not.toHaveBeenCalled();
  });
  it("параллельные refresh склеиваются в один запрос", async () => {
    const h = harness("tok0");
    await Promise.all([h.api().refresh(), h.api().refresh(), h.api().refresh()]);
    expect(h.ensure).toHaveBeenCalledTimes(1);
  });
});

describe("«Повторить»: порядок событий", () => {
  it("загрузка, потом токен, и только потом сброс ошибки", async () => {
    const log: string[] = [];
    await runRetry(async () => { log.push("refresh"); }, {
      start: () => log.push("start"), done: () => log.push("done"), fail: () => log.push("fail"),
    });
    expect(log).toEqual(["start", "refresh", "done"]);
  });
  it("сбой обновления токена оставляет ошибку", async () => {
    const log: string[] = [];
    await runRetry(async () => { throw new Error("нет"); }, {
      start: () => log.push("start"), done: () => log.push("done"), fail: () => log.push("fail"),
    });
    expect(log).toEqual(["start", "fail"]);
  });
});

describe("видео без ссылки из-за истёкшего токена", () => {
  it("статус: токен готов, а ссылки нет - это ошибка, не «пусто»", () => {
    expect(playerStatus({ hasVideo: true, src: null, failed: false, mediaReady: true })).toBe("failed");
    expect(playerStatus({ hasVideo: true, src: null, failed: false, mediaReady: false })).toBe("loading");
    expect(playerStatus({ hasVideo: false, src: null, failed: false, mediaReady: true })).toBe("empty");
    expect(playerStatus({ hasVideo: true, src: "u", failed: false, mediaReady: true })).toBe("ok");
  });
  it("плеер показывает video_failed и кнопку «Повторить»", () => {
    const timeline = { final: { video: { exists: true, path: "final.mp4", modified: 1, duration_s: 10 }, photo_video: null } } as unknown as TimelineResult;
    const media = { ready: true, media: () => null, thumb: () => null, onImageError: () => undefined, bust: () => undefined, refresh: async () => undefined } as ShotMediaApi;
    const html = renderToStaticMarkup(
      React.createElement(FilmPlayer, {
        timeline, cues: [], media, source: "film", onSource: () => undefined, time: 0, onTime: () => undefined,
        onResolution: () => undefined, handleRef: { current: null },
      }),
    );
    expect(html).toContain('role="alert"');
    expect(html).toContain("Повторить");
    expect(html).not.toContain("<video");
  });
});
