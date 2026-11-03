import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import {
  SHOT_TEXTS,
  fmt,
  formatRubExact,
  formatRubInt,
  formatShotDuration,
  pluralShots,
  shotCardTitle,
  shotErrorText,
} from "../shotsTexts";
import { PROJECT_LOCKED_TEXT } from "../texts";

const NBSP = " ";

describe("SHOT_TEXTS", () => {
  it("совпадает с общей фикстурой", () => {
    const path = fileURLToPath(new URL("../../../../../../../tests/fixtures/storybook_shots_texts.json", import.meta.url));
    expect(SHOT_TEXTS).toEqual(JSON.parse(readFileSync(path, "utf-8")));
  });
});

describe("fmt", () => {
  it("подставляет значения и оставляет неизвестные имена", () => {
    expect(fmt("found", { n: 7 })).toBe("Найдено: 7");
    expect(fmt("load_error", {})).toBe("Не удалось загрузить кадры: {reason}.");
    expect(fmt("toast_approved", { key: "2-3" })).toBe("Шот 2-3 одобрен");
  });
});

describe("числа и деньги", () => {
  it("pluralShots", () => {
    expect([0, 1, 3, 12, 14, 101, 111].map(pluralShots)).toEqual([
      "0 шотов", "1 шот", "3 шота", "12 шотов", "14 шотов", "101 шот", "111 шотов",
    ]);
  });
  it("formatRubInt", () => {
    expect(formatRubInt(1180.4)).toBe(`1${NBSP}180${NBSP}₽`);
    expect(formatRubInt(null)).toBe("—");
  });
  it("formatRubExact", () => {
    expect(formatRubExact(6.36)).toBe(`6,36${NBSP}₽`);
    expect(formatRubExact(0.05)).toBe(`0,05${NBSP}₽`);
    expect(formatRubExact(-2.5)).toBe(`-2,50${NBSP}₽`);
    expect(formatRubExact(Number.NaN)).toBe("—");
  });
  it("длительность и заголовок карточки", () => {
    expect(formatShotDuration(6)).toBe("6 с");
    expect(formatShotDuration(5.5)).toBe("5,5 с");
    expect(formatShotDuration(null)).toBe("—");
    expect(shotCardTitle("2-3", 2, 6)).toBe("Шот 2-3 · Сцена 2 · 6 с");
  });
});

describe("shotErrorText", () => {
  it("известные причины и запасной текст", () => {
    expect(shotErrorText("conflict", "current_changed", "x")).toBe(SHOT_TEXTS.restore_current_changed);
    expect(shotErrorText("locked", null, "x")).toBe(PROJECT_LOCKED_TEXT);
    expect(shotErrorText("conflict", "other", "сообщение")).toBe("сообщение");
    expect(shotErrorText("not_found", undefined, "нет шота")).toBe("нет шота");
  });
});
