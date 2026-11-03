import { describe, expect, it } from "vitest";

import type { ProjectCard } from "../projects";
import {
  canDelete,
  canRename,
  cardModified,
  cardSubtitle,
  filterProjects,
  pendingSizeCount,
  sortProjects,
} from "../projectsView";

const card = (id: string, over: Partial<ProjectCard> = {}): ProjectCard => ({
  project_id: id,
  title: id,
  description: "",
  language: "ru",
  state: "ready",
  stage: { id: "story", title: "Сюжет", index: 1, source: "artifacts" },
  busy: false,
  pages_count: 4,
  characters_count: 2,
  has_pdf: false,
  has_final_video: false,
  cover: null,
  created: "2026-10-01T10:00:00",
  modified: "2026-10-27T10:00:00",
  ...over,
});

describe("filterProjects", () => {
  const list = [card("fox_1", { title: "Лиса и Заяц" }), card("snow", { title: "Снег валит" })];
  it("ищет без регистра по названию и id", () => {
    expect(filterProjects(list, "ЛИСА").map((p) => p.project_id)).toEqual(["fox_1"]);
    expect(filterProjects(list, "SNOW").map((p) => p.project_id)).toEqual(["snow"]);
    expect(filterProjects(list, "  ").length).toBe(2);
    expect(filterProjects(list, "нет такого")).toEqual([]);
  });
});

describe("sortProjects", () => {
  const a = card("a", { title: "Бобёр", modified: "2026-10-01T00:00:00", stage: { id: "x", title: "Книга", index: 2, source: "" } });
  const b = card("b", { title: "Ёж", modified: "2026-10-20T00:00:00", stage: { id: "x", title: "Видео", index: 6, source: "" } });
  const c = card("c", { title: "Аист", modified: "2026-10-10T00:00:00", stage: { id: "x", title: "Книга", index: 2, source: "" } });
  const list = [a, b, c];

  it("по изменению: новые первыми", () => {
    expect(sortProjects(list, "modified").map((p) => p.project_id)).toEqual(["b", "c", "a"]);
  });
  it("по названию", () => {
    expect(sortProjects(list, "name").map((p) => p.project_id)).toEqual(["c", "a", "b"]);
  });
  it("по этапу: дальше всех первыми, при равенстве по названию", () => {
    expect(sortProjects(list, "stage").map((p) => p.project_id)).toEqual(["b", "c", "a"]);
  });
  it("по размеру: неизвестный в конце, порядок стабилен", () => {
    const sizes = new Map([["a", 10], ["b", 500]]);
    expect(sortProjects(list, "size", sizes).map((p) => p.project_id)).toEqual(["b", "a", "c"]);
    expect(sortProjects([a, c, b], "size", {}).map((p) => p.project_id)).toEqual(["a", "c", "b"]);
  });
  it("не меняет исходный массив", () => {
    sortProjects(list, "name");
    expect(list.map((p) => p.project_id)).toEqual(["a", "b", "c"]);
  });
});

describe("доступность действий", () => {
  it("идущий прогон и создание запрещают удалить и переименовать", () => {
    expect(canDelete(card("x", { busy: true }))).toEqual({ allowed: false, reason: "Недоступно, пока идёт прогон." });
    expect(canRename(card("x", { busy: true })).allowed).toBe(false);
    expect(canDelete(card("x", { state: "creating" })).allowed).toBe(false);
    expect(canDelete(card("x"))).toEqual({ allowed: true, reason: null });
  });
});

describe("подписи карточки", () => {
  it("размер: тире, пока не посчитан", () => {
    expect(cardSubtitle(card("x"))).toBe("Этап: Сюжет · 4 стр. · —");
    expect(cardSubtitle(card("x"), 8 * 1024 * 1024)).toContain("8.0");
    expect(cardModified(card("x"))).toBe("Изменён 27.10.2026");
  });
  it("прогресс подсчёта размеров", () => {
    expect(pendingSizeCount([card("a"), card("b")], new Map([["a", 1]]))).toEqual({ done: 1, total: 2 });
  });
});
