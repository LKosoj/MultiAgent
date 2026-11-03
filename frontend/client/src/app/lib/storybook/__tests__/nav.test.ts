import { describe, expect, it } from "vitest";

import {
  INITIAL_NAV,
  NAV_STORAGE_KEY,
  STORYBOOK_SCREENS,
  dropMissingProject,
  isScreenEnabled,
  loadNav,
  navReducer,
  sanitizeNav,
  saveNav,
  screenForTarget,
  type NavState,
  type StorageLike,
} from "../nav";

const memory = (): StorageLike & { data: Map<string, string> } => {
  const data = new Map<string, string>();
  return { data, getItem: (k) => data.get(k) ?? null, setItem: (k, v) => void data.set(k, v) };
};
const broken: StorageLike = {
  getItem: () => {
    throw new Error("denied");
  },
  setItem: () => {
    throw new Error("denied");
  },
};

describe("nav", () => {
  it("порядок экранов как в спеке 2.1", () => {
    expect([...STORYBOOK_SCREENS]).toEqual([
      "projects", "overview", "shots", "generation", "film", "blockout", "files", "settings",
    ]);
  });

  it("начальное состояние и открытие проекта ведёт на Обзор", () => {
    expect(INITIAL_NAV).toEqual({ projectId: null, screen: "projects", intent: null });
    const s = navReducer(INITIAL_NAV, { type: "open_project", projectId: "fox_1" });
    expect(s).toEqual({ projectId: "fox_1", screen: "overview", intent: null });
  });

  it("экраны 2-7 без проекта недоступны", () => {
    for (const screen of ["overview", "shots", "generation", "film", "blockout", "files"] as const) {
      expect(isScreenEnabled(screen, null)).toBe(false);
      expect(navReducer(INITIAL_NAV, { type: "go", screen })).toBe(INITIAL_NAV);
    }
    expect(isScreenEnabled("projects", null)).toBe(true);
    expect(isScreenEnabled("settings", null)).toBe(true);
    expect(navReducer(INITIAL_NAV, { type: "go", screen: "settings" }).screen).toBe("settings");
  });

  it("небезопасный id проекта не открывается", () => {
    expect(navReducer(INITIAL_NAV, { type: "open_project", projectId: "../x" })).toBe(INITIAL_NAV);
  });

  it("applyTarget для всех экранов цели, movie -> film", () => {
    const base: NavState = { projectId: "p", screen: "overview", intent: null };
    expect(screenForTarget("movie")).toBe("film");
    expect(navReducer(base, { type: "apply_target", target: { screen: "movie" } }).screen).toBe("film");
    const shots = navReducer(base, { type: "apply_target", target: { screen: "shots", filter: "stale" } });
    expect(shots.screen).toBe("shots");
    expect(shots.intent).toMatchObject({ filter: "stale" });
    const gen = navReducer(base, {
      type: "apply_target",
      target: { screen: "generation", step_id: "video_generator", mode: "resume" },
    });
    expect(gen.intent).toMatchObject({ stepId: "video_generator", mode: "resume" });
    expect(navReducer(base, { type: "apply_target", target: { screen: "overview" } }).screen).toBe("overview");
    expect(navReducer(base, { type: "apply_target", target: { screen: "что-то" } }).screen).toBe("overview");
    expect(navReducer(INITIAL_NAV, { type: "apply_target", target: { screen: "shots" } })).toBe(INITIAL_NAV);
  });

  it("переход в «Генерацию» с отмеченными шотами", () => {
    const base: NavState = { projectId: "p", screen: "shots", intent: null };
    const s = navReducer(base, {
      type: "apply_target",
      target: { screen: "generation", mode: "regenerate_shots", shot_keys: ["1-1", "2-3"] },
    });
    expect(s.screen).toBe("generation");
    expect(s.intent).toMatchObject({ mode: "regenerate_shots", shotKeys: ["1-1", "2-3"] });
    const plain = navReducer(base, { type: "apply_target", target: { screen: "generation" } });
    expect(plain.intent?.shotKeys).toBeNull();
  });

  it("переход к шоту", () => {
    const s = navReducer({ projectId: "p", screen: "overview", intent: null }, { type: "open_shot", shotKey: "3-2" });
    expect(s).toMatchObject({ screen: "shots", intent: { shotKey: "3-2" } });
  });

  it("sanitizeNav на мусоре", () => {
    for (const bad of ["строка", 12, null, [], { screen: "nope", projectId: null }, { screen: "overview", projectId: null }]) {
      expect(sanitizeNav(bad)).toEqual(INITIAL_NAV);
    }
    expect(sanitizeNav({ screen: "shots", projectId: "../x" })).toEqual(INITIAL_NAV);
    expect(sanitizeNav({ screen: "shots", projectId: "p1" })).toEqual({ projectId: "p1", screen: "shots", intent: null });
  });

  it("сохранение и чтение, в том числе с падающим хранилищем", () => {
    const st = memory();
    saveNav(st, { projectId: "p1", screen: "film", intent: { screen: "film" } });
    expect(JSON.parse(st.data.get(NAV_STORAGE_KEY)!)).toEqual({ projectId: "p1", screen: "film" });
    expect(loadNav(st)).toEqual({ projectId: "p1", screen: "film", intent: null });
    st.data.set(NAV_STORAGE_KEY, "{не json");
    expect(loadNav(st)).toEqual(INITIAL_NAV);
    expect(loadNav(broken)).toEqual(INITIAL_NAV);
    expect(() => saveNav(broken, INITIAL_NAV)).not.toThrow();
    expect(loadNav(null)).toEqual(INITIAL_NAV);
  });

  it("проект исчез из списка: возврат к «Проектам»", () => {
    const nav: NavState = { projectId: "gone", screen: "overview", intent: null };
    expect(dropMissingProject(nav, ["a", "b"])).toEqual(INITIAL_NAV);
    expect(dropMissingProject(nav, ["gone"])).toBe(nav);
    expect(dropMissingProject(INITIAL_NAV, [])).toBe(INITIAL_NAV);
  });
});
