import { describe, expect, it } from "vitest";
import { resolveScreenKey } from "../screenKeys";

const k = (key: string, o: { ctrlKey?: boolean; metaKey?: boolean } = {}) => ({ key, ctrlKey: false, metaKey: false, ...o });

describe("resolveScreenKey", () => {
  it("Ctrl+S и Cmd+S сохраняют", () => {
    expect(resolveScreenKey(k("s", { ctrlKey: true }), false)).toEqual({ swallow: true, action: "save" });
    expect(resolveScreenKey(k("S", { metaKey: true }), false)).toEqual({ swallow: true, action: "save" });
  });
  it("F5 перечитывает, Ctrl+F5 не трогаем", () => {
    expect(resolveScreenKey(k("F5"), false)).toEqual({ swallow: true, action: "reload" });
    expect(resolveScreenKey(k("F5", { ctrlKey: true }), false)).toEqual({ swallow: false, action: null });
  });
  it("при открытом окне клавиши глушатся, но действия нет", () => {
    expect(resolveScreenKey(k("s", { ctrlKey: true }), true)).toEqual({ swallow: true, action: null });
    expect(resolveScreenKey(k("F5"), true)).toEqual({ swallow: true, action: null });
  });
  it("прочие клавиши не трогаем", () => {
    expect(resolveScreenKey(k("a"), true)).toEqual({ swallow: false, action: null });
  });
});

describe("refreshKindOf", () => {
  it("F5 по экранам", async () => {
    const { refreshKindOf } = await import("../screenKeys");
    expect(refreshKindOf("projects")).toBe("projects");
    expect(refreshKindOf("overview")).toBe("overview");
    for (const s of ["shots", "generation", "film", "blockout"]) expect(refreshKindOf(s)).toBe("tick");
    for (const s of ["files", "settings"]) expect(refreshKindOf(s)).toBe("none");
  });
});

describe("shotsRefreshPlan", () => {
  it("карточка с несохранённой правкой не перечитывается, список - всегда", async () => {
    const { shotsRefreshPlan } = await import("../screenKeys");
    expect(shotsRefreshPlan(true)).toEqual({ list: true, card: false });
    expect(shotsRefreshPlan(false)).toEqual({ list: true, card: true });
  });
});
