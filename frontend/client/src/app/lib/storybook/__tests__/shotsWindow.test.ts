import { describe, expect, it } from "vitest";

import { columnsForWidth, layoutRows, rowIndexOfKey, shouldScrollToKey, visibleRange } from "../shotsWindow";

const item = (k: string) => ({ shot_key: k, scene: Number(k.split("-")[0]) });

describe("layoutRows", () => {
  it("пустой список", () => {
    expect(layoutRows([], "list", 1)).toEqual({ rows: [], total: 0 });
  });
  it("сетка закрывает неполную строку при смене сцены", () => {
    const { rows, total } = layoutRows(["1-1", "2-1", "2-2", "2-3"].map(item), "grid", 2);
    expect(rows.map((r) => (r.kind === "header" ? `h${r.scene}` : r.keys.join("+")))).toEqual(["h1", "1-1", "h2", "2-1+2-2", "2-3"]);
    expect(total).toBe(32 + 120 + 32 + 120 + 120);
  });
  it("колонки в режиме списка игнорируются; нулевые колонки считаются одной", () => {
    expect(layoutRows(["1-1", "1-2"].map(item), "list", 5).rows).toHaveLength(3);
    expect(layoutRows(["1-1", "1-2"].map(item), "grid", 0).rows).toHaveLength(3);
  });
});

describe("visibleRange", () => {
  const rows = layoutRows(["1-1", "1-2", "2-1"].map(item), "list", 1).rows;
  it("граница строки не попадает в окно", () => {
    expect(visibleRange(rows, 32, 64, 0)).toEqual([1, 1]);
  });
  it("отрицательная прокрутка равна нулю", () => {
    expect(visibleRange(rows, -50, 10, 0)).toEqual([0, 0]);
  });
});

describe("rowIndexOfKey и columnsForWidth", () => {
  it("находит строку по ключу", () => {
    const { rows } = layoutRows(["1-1", "1-2", "1-3"].map(item), "grid", 2);
    expect(rowIndexOfKey(rows, "1-3")).toBe(2);
    expect(rowIndexOfKey(rows, "9-9")).toBe(-1);
  });
  it("не меньше одной колонки", () => {
    expect(columnsForWidth(0)).toBe(1);
    expect(columnsForWidth(-10)).toBe(1);
  });
});

describe("shouldScrollToKey", () => {
  it("один раз на ключ, когда строка появилась", () => {
    expect(shouldScrollToKey("3-1", null, false)).toBe(false);
    expect(shouldScrollToKey("3-1", null, true)).toBe(true);
    expect(shouldScrollToKey("3-1", "3-1", true)).toBe(false);
    expect(shouldScrollToKey("3-2", "3-1", true)).toBe(true);
    expect(shouldScrollToKey(null, "3-1", true)).toBe(false);
  });
});
