import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { StorybookTopBar } from "../storybook/StorybookTopBar";

const props = {
  brief: null,
  offline: false,
  onOpenPalette: () => undefined,
  onOpenProjects: () => undefined,
  onOpenGeneration: () => undefined,
  onRerun: () => undefined,
  onPauseToggle: async () => undefined,
  onStop: async () => null,
};

describe("StorybookTopBar", () => {
  it("без проекта полосы нет", () => {
    expect(renderToStaticMarkup(React.createElement(StorybookTopBar, { ...props, projectId: null }))).toBe("");
  });
  it("с проектом, но без статуса: Пауза и Стоп отключены", () => {
    const html = renderToStaticMarkup(React.createElement(StorybookTopBar, { ...props, projectId: "p1" }));
    expect(html).toContain("Проект: p1");
    expect(html).toMatch(/disabled=""[^>]*>Пауза</);
    expect(html).toMatch(/disabled=""[^>]*>Стоп</);
  });
  it("показывает выбранную серию", () => {
    const html = renderToStaticMarkup(React.createElement(StorybookTopBar, { ...props, projectId: "p1", seriesId: "s1" }));
    expect(html).toContain("Серия: s1");
  });
  it("«Запустить заново» отключена, пока прогона нет", () => {
    const html = renderToStaticMarkup(React.createElement(StorybookTopBar, { ...props, projectId: "p1" }));
    expect(html).toMatch(/disabled=""[^>]*title="Прогон ещё не запускался"[^>]*>Запустить заново</);
  });
});
