import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { ProjectCard } from "../../../lib/storybook/projects";
import type { ProjectActionsApi } from "../../../lib/storybook/useProjectActions";
import type { ProjectsApi } from "../../../lib/storybook/useProjects";
import { createBackgroundQueue } from "../../../lib/storybook/backgroundQueue";
import { StorybookProjectsTab } from "../storybook/StorybookProjectsTab";

const card = (id: string, extra: Partial<ProjectCard> = {}): ProjectCard => ({
  project_id: id,
  title: `Сказка ${id}`,
  description: "",
  language: "ru",
  state: "ready",
  stage: { id: "brief", title: "Бриф", index: 1, source: "db" },
  busy: false,
  pages_count: 9,
  characters_count: 2,
  has_pdf: false,
  has_final_video: false,
  cover: null,
  created: "2026-11-01T10:00:00",
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
