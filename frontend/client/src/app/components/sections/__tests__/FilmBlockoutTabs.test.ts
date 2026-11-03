import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { BlockoutTab } from "../storybook/blockout/BlockoutTab";
import { FilmTab } from "../storybook/film/FilmTab";

const h = (c: unknown, p: object) => renderToStaticMarkup(React.createElement(c as React.ComponentType<object>, p));
const noop = () => undefined;

const filmApi = (over: object) => ({
  projectId: "p", state: "loading", timeline: null, subtitles: null, subtitlesFailed: false, error: null,
  reload: noop, media: {}, source: "video", setSource: noop, banners: [], actionBlock: null, rebuilding: false,
  rebuild: async () => undefined, music: { open: false, phase: "idle", view: null, error: null, begin: noop, start: noop, close: noop },
  versions: { state: "idle", rows: [] }, loadVersions: noop, resolution: null, setResolution: noop, ...over,
});
const filmProps = { projectName: "Книга", onGoGeneration: noop, onOpenShot: noop, onOpenReport: noop };

describe("экран «Фильм»", () => {
  it("загрузка: скелетон без плеера", () => {
    const html = h(FilmTab, { ...filmProps, api: filmApi({ state: "loading" }) });
    expect(html).not.toContain("<video");
    expect(html).not.toMatch(/[\u{1F300}-\u{1FAFF}]/u);
  });
  it("корень принимает фокус, чтобы работали горячие клавиши", () => {
    expect(h(FilmTab, { ...filmProps, api: filmApi({ state: "loading" }) })).toContain('tabindex="-1"');
  });
  it("ошибка: текст и кнопка повтора", () => {
    const html = h(FilmTab, { ...filmProps, api: filmApi({ state: "error", error: "Нет связи" }) });
    expect(html).toContain("Нет связи");
    expect(html).toContain("<button");
  });
  it("пусто: ведёт в «Генерацию»", () => {
    const html = h(FilmTab, { ...filmProps, api: filmApi({ state: "empty" }) });
    expect(html).toContain("<button");
    expect(html).not.toContain("<video");
  });
});

const blockoutApi = (over: object) => ({
  screen: "loading", state: null, error: null, selection: { keys: [], anchor: null, focus: null }, detail: null, buttons: [],
  dialog: null, reload: noop, ...over,
});

describe("экран «Болванка»", () => {
  it("загрузка и ошибка рисуются без падений", () => {
    const base = { onGoGeneration: noop, onOpenShot: noop };
    expect(h(BlockoutTab, { ...base, api: blockoutApi({}) })).toContain("sb-blockout");
    const err = h(BlockoutTab, { ...base, api: blockoutApi({ screen: "error", error: "Сбой" }) });
    expect(err).toContain("Сбой");
    expect(err).not.toContain("Открыть .blend");
  });
});
