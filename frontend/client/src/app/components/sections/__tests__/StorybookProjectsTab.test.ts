import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { ProjectCard } from "../../../lib/storybook/projects";
import type { ProjectActionsApi } from "../../../lib/storybook/useProjectActions";
import type { ProjectsApi } from "../../../lib/storybook/useProjects";
import { createBackgroundQueue } from "../../../lib/storybook/backgroundQueue";
import { ProjectDetailsPanel } from "../storybook/ProjectDetailsPanel";
import { StorybookProjectsTab } from "../storybook/StorybookProjectsTab";

const card = (id: string, extra: Partial<ProjectCard> = {}): ProjectCard => ({
  project_id: id,
  title: `Сказка ${id}`,
  description: "",
  language: "ru",
  state: "ready",
  busy: false,
  series_count: 4,
  modified: "2026-11-02T10:00:00",
  ...extra,
});

const projects = (patch: Partial<ProjectsApi>): ProjectsApi => ({
  state: "ready",
  error: null,
  projects: [],
  projectsDir: "/data/projects",
  sizes: new Map(),
  extras: new Map(),
  load: async () => undefined,
  loadSize: () => undefined,
  loadAllSizes: () => undefined,
  loadExtra: () => undefined,
  ...patch,
});

const actions = {
  active: {},
  bannerError: null,
  lastExport: null,
} as unknown as ProjectActionsApi;

const client = {
  call: async () => ({}),
  ensureMediaToken: async () => undefined,
  mediaUrl: () => null,
  thumbUrl: () => null,
} as never;

const render = (p: ProjectsApi, selectedId: string | null = null) =>
  renderToStaticMarkup(
    React.createElement(StorybookProjectsTab, {
      projects: p,
      actions,
      client,
      background: createBackgroundQueue(),
      selectedId,
      onSelect: () => undefined,
      onOpen: () => undefined,
      command: null,
      onOpenSettings: () => undefined,
    }),
  );

describe("StorybookProjectsTab", () => {
  it("загрузка: скелет", () => {
    expect(render(projects({ state: "loading" }))).toContain("Загрузка проектов");
  });
  it("пустой список: подсказка и кнопка создания", () => {
    const html = render(projects({}));
    expect(html).toContain("Проектов пока нет");
    expect(html).toContain("Создать проект");
    expect(html).toContain("добавьте в него серию");
    expect(html).not.toContain("Создать из промпта");
  });
  it("папка не найдена: текст с путём и кнопка настроек", () => {
    const html = render(projects({ state: "error", error: { kind: "not_found", text: "x" } }));
    expect(html).toContain("Папка проектов не найдена: /data/projects.");
    expect(html).toContain("Открыть настройки");
  });
  it("нет прав: единый текст", () => {
    const html = render(projects({ state: "error", error: { kind: "forbidden", text: "x" } }));
    expect(html).toContain("Недостаточно прав");
  });
  it("отображает ответ API постоянного проекта и его общие ресурсы", () => {
    const html = renderToStaticMarkup(React.createElement(ProjectDetailsPanel, {
      card: card("p", { series_count: 2 }),
      extra: { details: { title: "Мир", description: "Описание мира", language: "ru", resources: { characters: { fox: "v1" }, locations: { forest: "v1" } } } },
      sizeBytes: undefined, client, active: {}, lastExport: null,
      onOpen: () => {}, onDuplicate: () => {}, onRename: () => {}, onBackups: () => {}, onExport: () => {}, onRemove: () => {},
    }));
    expect(html).toContain("Серий");
    expect(html).toContain("fox");
    expect(html).toContain("forest");
    expect(html).not.toContain("undefined");
    expect(html).not.toContain("Финальное видео");
  });
  it("карточки и выбранный проект в правой панели", () => {
    const html = render(projects({ projects: [card("a"), card("b", { busy: true })] }), "b");
    expect(html).toContain("Сказка a");
    expect(html).toContain("Идёт прогон");
    expect(html).toContain('aria-pressed="true"');
    expect(html).toContain("Выбранный проект");
    // удалять нельзя, пока идёт прогон: кнопка отключена с подсказкой
    expect(html).toContain("Недоступно, пока идёт прогон.");
  });
});
