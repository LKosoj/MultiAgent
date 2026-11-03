import { describe, expect, it } from "vitest";

import { createRetryOnce, mediaSuffix } from "../useShotMedia";

describe("createRetryOnce", () => {
  it("повтор один раз на каждый адрес", () => {
    const once = createRetryOnce();
    expect(once("a.png")).toBe(true);
    expect(once("a.png")).toBe(false);
    expect(once("b.png")).toBe(true);
  });
});

describe("mediaSuffix", () => {
  it("после bust ссылка меняется даже при неизменном времени изменения", () => {
    expect(mediaSuffix("12.5", 0)).toBe("&v=12.5");
    expect(mediaSuffix("12.5", 1)).not.toBe(mediaSuffix("12.5", 0));
    expect(mediaSuffix(null, 2)).toBe("&v=b2");
    expect(mediaSuffix(null, 0)).toBe("");
  });
});
