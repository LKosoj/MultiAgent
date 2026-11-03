import { describe, expect, it } from "vitest";

import { matchShotKey, type ShotKeyContext, type ShotKeyEvent } from "../shotsKeys";

const ev = (over: Partial<ShotKeyEvent>): ShotKeyEvent => ({
  key: "",
  ctrlKey: false,
  metaKey: false,
  altKey: false,
  shiftKey: false,
  inEditable: false,
  ...over,
});
const ctx = (over: Partial<ShotKeyContext> = {}): ShotKeyContext => ({ active: true, modalOpen: false, focusInside: true, ...over });

describe("matchShotKey", () => {
  it("стрелки, статусы, пробел и Enter", () => {
    expect(matchShotKey(ev({ key: "ArrowDown", code: "ArrowDown" }), ctx())).toEqual({ type: "move", delta: 1 });
    expect(matchShotKey(ev({ key: "ArrowUp", code: "ArrowUp" }), ctx())).toEqual({ type: "move", delta: -1 });
    expect(matchShotKey(ev({ key: "a", code: "KeyA" }), ctx())).toEqual({ type: "status", letter: "a", status: "approved" });
    expect(matchShotKey(ev({ key: "r", code: "KeyR" }), ctx())).toEqual({ type: "status", letter: "r", status: "redo" });
    expect(matchShotKey(ev({ key: "x", code: "KeyX" }), ctx())).toEqual({ type: "status", letter: "x", status: "rejected" });
    expect(matchShotKey(ev({ key: " ", code: "Space" }), ctx())).toEqual({ type: "mark" });
    expect(matchShotKey(ev({ key: "Enter", code: "Enter" }), ctx())).toEqual({ type: "open" });
  });

  it("автоповтор при удержании клавиши молчит", () => {
    expect(matchShotKey(ev({ key: "a", code: "KeyA", repeat: true }), ctx())).toBeNull();
    expect(matchShotKey(ev({ key: "ArrowDown", code: "ArrowDown", repeat: true }), ctx())).toEqual({ type: "move", delta: 1 });
    expect(matchShotKey(ev({ key: " ", code: "Space", repeat: true }), ctx())).toBeNull();
    expect(matchShotKey(ev({ key: "a", code: "KeyA", ctrlKey: true, repeat: true }), ctx())).toBeNull();
    expect(matchShotKey(ev({ key: "s", code: "KeyS", ctrlKey: true, repeat: true }), ctx())).toBeNull();
    expect(matchShotKey(ev({ key: "a", code: "KeyA", repeat: false }), ctx())).toMatchObject({ type: "status" });
  });

  it("русская раскладка: решает code, а не символ", () => {
    expect(matchShotKey(ev({ key: "ф", code: "KeyA" }), ctx())).toMatchObject({ type: "status", status: "approved" });
    expect(matchShotKey(ev({ key: "ч", code: "KeyX" }), ctx())).toMatchObject({ status: "rejected" });
    expect(matchShotKey(ev({ key: "ы", code: "KeyS", ctrlKey: true }), ctx())).toEqual({ type: "save" });
  });

  it("без code работает по латинской букве", () => {
    expect(matchShotKey(ev({ key: "A" }), ctx())).toMatchObject({ status: "approved" });
    expect(matchShotKey(ev({ key: " " }), ctx())).toEqual({ type: "mark" });
  });

  it("в поле ввода одиночные клавиши молчат, Ctrl+S работает", () => {
    expect(matchShotKey(ev({ key: "a", code: "KeyA", inEditable: true }), ctx())).toBeNull();
    expect(matchShotKey(ev({ key: "ArrowDown", code: "ArrowDown", inEditable: true }), ctx())).toBeNull();
    expect(matchShotKey(ev({ key: "s", code: "KeyS", ctrlKey: true, inEditable: true }), ctx())).toEqual({ type: "save" });
    expect(matchShotKey(ev({ key: "a", code: "KeyA", ctrlKey: true, inEditable: true }), ctx())).toBeNull();
  });

  it("Ctrl/Cmd+S и Ctrl+A", () => {
    expect(matchShotKey(ev({ key: "s", code: "KeyS", metaKey: true }), ctx())).toEqual({ type: "save" });
    expect(matchShotKey(ev({ key: "a", code: "KeyA", ctrlKey: true }), ctx())).toEqual({ type: "mark_all" });
    expect(matchShotKey(ev({ key: "s", code: "KeyS", ctrlKey: true, shiftKey: true }), ctx())).toBeNull();
  });

  it("с модальным окном, на чужом экране и вне раздела молчит", () => {
    const e = ev({ key: "a", code: "KeyA" });
    expect(matchShotKey(e, ctx({ modalOpen: true }))).toBeNull();
    expect(matchShotKey(e, ctx({ active: false }))).toBeNull();
    expect(matchShotKey(e, ctx({ focusInside: false }))).toBeNull();
  });

  it("на кнопке Enter и Пробел остаются кнопке", () => {
    expect(matchShotKey(ev({ key: "Enter", code: "Enter", inControl: true }), ctx())).toBeNull();
    expect(matchShotKey(ev({ key: " ", code: "Space", inControl: true }), ctx())).toBeNull();
    expect(matchShotKey(ev({ key: "ArrowDown", code: "ArrowDown", inControl: true }), ctx())).toEqual({ type: "move", delta: 1 });
  });

  it("Alt и Shift отключают клавиши", () => {
    expect(matchShotKey(ev({ key: "a", code: "KeyA", altKey: true }), ctx())).toBeNull();
    expect(matchShotKey(ev({ key: "A", code: "KeyA", shiftKey: true }), ctx())).toBeNull();
  });
});
