import { readFileSync } from "node:fs";
import { join } from "node:path";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { ProjectStatus } from "../../../lib/storybook/status";
import type { ProjectStatusApi } from "../../../lib/storybook/useProjectStatus";
import { StorybookOverviewTab } from "../storybook/StorybookOverviewTab";

const sample = (): ProjectStatus =>
  JSON.parse(
    readFileSync(join(__dirname, "../../../lib/storybook/__tests__/fixtures/project_status.sample.json"), "utf-8"),
  ) as ProjectStatus;

const api = (patch: Partial<ProjectStatusApi>): ProjectStatusApi => ({
  status: null,
  brief: null,
  state: "loading",
  error: null,
  offline: false,
  refresh: () => undefined,
  ...patch,
});

const render = (a: ProjectStatusApi) =>
  renderToStaticMarkup(
    React.createElement(StorybookOverviewTab, {
      api: a,
      onTarget: () => undefined,
      onWatchFinal: () => undefined,
      onGoGeneration: () => undefined,
      onBackToProjects: () => undefined,
    }),
  );

describe("StorybookOverviewTab", () => {
  it("загрузка: скелет", () => {
    expect(render(api({}))).toContain("Загрузка состояния проекта");
  });
  it("ошибка «не найден»: предлагает вернуться к списку", () => {
    const html = render(api({ state: "error", error: { kind: "not_found", text: "x" } }));
    expect(html).toContain("Проект не найден");
    expect(html).toContain("К списку проектов");
  });
  it("нет прав: единый текст", () => {
    const html = render(api({ state: "error", error: { kind: "forbidden", text: "x" } }));
    expect(html).toContain("Недостаточно прав");
  });
  it("данные: этапы, шаги, счётчики и блоки", () => {
    const status = sample();
    const html = render(api({ status, state: "ready" }));
    expect(html).toContain("Обзор серии");
    expect(html).toContain("Следующее действие");
    expect(html).toContain("Шаги пайплайна");
    expect(html).toContain("Последний прогон");
    expect(html).toContain("Готово"); // клиентская ячейка этапа
    expect((html.match(/class="sb-stage"/g) ?? []).length).toBe(status.stage.items.length + 1);
  });
  it("кнопки «Запустить заново» ведут в «Генерацию», пока прогона нет", () => {
    const html = render(api({ status: sample(), state: "ready" }));
    expect(html).not.toContain('aria-disabled="true"');
    expect(html).toContain("Открыть «Генерацию» с этим шагом");
  });
  it("кнопки «Запустить заново» отключены, пока идёт прогон", () => {
    const status = sample();
    status.active_run = { ...status.active_run, state: "running" };
    const html = render(api({ status, state: "ready" }));
    expect(html).toContain('aria-disabled="true"');
    expect(html).toContain("Недоступно, пока идёт прогон");
  });
});
