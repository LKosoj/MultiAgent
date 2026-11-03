import { describe, expect, it } from "vitest";

import { matchShortcut, type ShortcutContext, type ShortcutEvent } from "../shortcuts";

const ev = (over: Partial<ShortcutEvent>): ShortcutEvent => ({
  key: "",
  ctrlKey: false,
  metaKey: false,
  altKey: false,
  shiftKey: false,
  inEditable: false,
  ...over,
});
const ctx = (over: Partial<ShortcutContext> = {}): ShortcutContext => ({
  active: true,
  modalOpen: false,
  focusInside: true,
  screen: "projects",
  hasProject: true,
  ...over,
});

describe("matchShortcut", () => {
  it("Alt+1..8 переключают разделы", () => {
    expect(matchShortcut(ev({ altKey: true, key: "1", code: "Digit1" }), ctx())).toEqual({ type: "screen", screen: "projects" });
    expect(matchShortcut(ev({ altKey: true, key: "¡", code: "Digit8" }), ctx())).toEqual({ type: "screen", screen: "settings" });
    expect(matchShortcut(ev({ altKey: true, key: "9", code: "Digit9" }), ctx())).toBeNull();
  });

  it("без проекта разделы 2-7 не включаются", () => {
    expect(matchShortcut(ev({ altKey: true, key: "3", code: "Digit3" }), ctx({ hasProject: false }))).toBeNull();
    expect(matchShortcut(ev({ altKey: true, key: "8", code: "Digit8" }), ctx({ hasProject: false }))).toEqual({
      type: "screen",
      screen: "settings",
    });
  });

  it("Ctrl+K и Cmd+K открывают быстрый выбор, в том числе из поля ввода", () => {
    expect(matchShortcut(ev({ ctrlKey: true, key: "k", code: "KeyK" }), ctx())).toEqual({ type: "palette" });
    expect(matchShortcut(ev({ metaKey: true, key: "k" }), ctx({ screen: "overview" }))).toEqual({ type: "palette" });
    expect(matchShortcut(ev({ ctrlKey: true, key: "к", code: "KeyK", inEditable: true }), ctx())).toEqual({ type: "palette" });
  });

  it("F5 только когда фокус внутри раздела", () => {
    expect(matchShortcut(ev({ key: "F5" }), ctx())).toEqual({ type: "refresh" });
    expect(matchShortcut(ev({ key: "F5" }), ctx({ focusInside: false }))).toBeNull();
    expect(matchShortcut(ev({ key: "F5", ctrlKey: true }), ctx())).toBeNull();
  });

  it("Alt+N только на «Проектах», работает и на русской раскладке", () => {
    expect(matchShortcut(ev({ altKey: true, key: "n", code: "KeyN" }), ctx())).toEqual({ type: "create_prompt" });
    expect(matchShortcut(ev({ altKey: true, key: "т", code: "KeyN" }), ctx())).toEqual({ type: "create_prompt" });
    expect(matchShortcut(ev({ altKey: true, key: "n", code: "KeyN" }), ctx({ screen: "overview" }))).toBeNull();
  });

  it("«/» фокусирует поиск, но не в поле ввода", () => {
    expect(matchShortcut(ev({ key: "/" }), ctx())).toEqual({ type: "focus_search" });
    expect(matchShortcut(ev({ key: "/", inEditable: true }), ctx())).toBeNull();
    expect(matchShortcut(ev({ key: "/" }), ctx({ screen: "overview" }))).toBeNull();
  });

  it("ничего не делает при открытом окне и в неактивном разделе", () => {
    expect(matchShortcut(ev({ key: "F5" }), ctx({ modalOpen: true }))).toBeNull();
    expect(matchShortcut(ev({ ctrlKey: true, key: "k" }), ctx({ active: false }))).toBeNull();
  });

  it("чужие сочетания не распознаются", () => {
    expect(matchShortcut(ev({ ctrlKey: true, key: "c" }), ctx())).toBeNull();
    expect(matchShortcut(ev({ key: "a" }), ctx())).toBeNull();
    expect(matchShortcut(ev({ ctrlKey: true, shiftKey: true, key: "K" }), ctx())).toBeNull();
    expect(matchShortcut(ev({ altKey: true, ctrlKey: true, key: "1", code: "Digit1" }), ctx())).toBeNull();
  });
});
