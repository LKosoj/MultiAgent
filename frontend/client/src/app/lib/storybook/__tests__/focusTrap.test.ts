import { describe, expect, it } from "vitest";

import { trapTarget } from "../focusTrap";

describe("trapTarget", () => {
  const items = ["a", "b", "c"];
  it("по кругу вперёд и назад", () => {
    expect(trapTarget(items, "c", false)).toBe("a");
    expect(trapTarget(items, "a", true)).toBe("c");
  });
  it("внутри списка не вмешивается", () => {
    expect(trapTarget(items, "b", false)).toBeNull();
    expect(trapTarget(items, "b", true)).toBeNull();
  });
  it("фокус вне окна (после клика по тексту) возвращается внутрь", () => {
    expect(trapTarget(items, null, false)).toBe("a");
    expect(trapTarget(items, "x", true)).toBe("c");
  });
  it("пустой список", () => {
    expect(trapTarget([], null, false)).toBeNull();
  });
});
