import { describe, expect, it } from "vitest";

import {
  formatBytes,
  formatClock,
  formatDateRu,
  formatDateTimeAny,
  formatDateTimeRu,
  formatDuration,
  formatMaybe,
  formatRub,
} from "../format";

const NB = " ";

describe("format", () => {
  it("formatBytes", () => {
    expect(formatBytes(512)).toBe(`512${NB}Б`);
    expect(formatBytes(8 * 1024 * 1024)).toBe(`8.0${NB}МБ`);
    expect(formatBytes(6.4 * 1024 ** 3)).toBe(`6.4${NB}ГБ`);
    expect(formatBytes(148 * 1024 * 1024)).toBe(`148${NB}МБ`);
    expect(formatBytes(null)).toBe("—");
    expect(formatBytes(-1)).toBe("—");
  });

  it("formatRub: неразрывный пробел между тысячами и перед знаком", () => {
    expect(formatRub(1204)).toBe(`1${NB}204${NB}₽`);
    expect(formatRub(0)).toBe(`0${NB}₽`);
    expect(formatRub(1500000.4)).toBe(`1${NB}500${NB}000${NB}₽`);
    expect(formatRub(null)).toBe("—");
  });

  it("formatDuration", () => {
    expect(formatDuration(42 * 60)).toBe("42 мин");
    expect(formatDuration(2 * 3600 + 5 * 60)).toBe("2 ч 5 мин");
    expect(formatDuration(3600)).toBe("1 ч");
    expect(formatDuration(20)).toBe("20 с");
    expect(formatDuration(null)).toBe("—");
  });

  it("formatClock", () => {
    expect(formatClock(161)).toBe("02:41");
    expect(formatClock(3761)).toBe("1:02:41");
    expect(formatClock(undefined)).toBe("—");
  });

  it("даты", () => {
    expect(formatDateRu("2026-10-27T14:05:09")).toBe("27.10.2026");
    expect(formatDateTimeRu("2026-10-27T14:05:09")).toBe("27.10.2026 14:05");
    expect(formatDateRu("мусор")).toBe("—");
    expect(formatDateRu(null)).toBe("—");
    expect(formatDateTimeAny("2026-10-27T14:05:09")).toBe("27.10.2026 14:05");
    expect(formatDateTimeAny("2026-10-27T14:05:09Z")).toMatch(/^\d{2}\.\d{2}\.2026 \d{2}:\d{2}$/);
  });

  it("formatMaybe: null это тире, ноль остаётся нулём", () => {
    expect(formatMaybe(null)).toBe("—");
    expect(formatMaybe(undefined)).toBe("—");
    expect(formatMaybe(0)).toBe("0");
    expect(formatMaybe(5)).toBe("5");
  });
});
