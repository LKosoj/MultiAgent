import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import { StorybookSection, resolveSectionMode } from "../StorybookSection";

describe("resolveSectionMode", () => {
  it("не админ - всегда «нет доступа»", () => {
    expect(resolveSectionMode({ isAdmin: false, active: true, everActive: true })).toBe("no_access");
  });
  it("до первого открытия раздел спит", () => {
    expect(resolveSectionMode({ isAdmin: true, active: false, everActive: false })).toBe("dormant");
  });
  it("после открытия остаётся смонтированным", () => {
    expect(resolveSectionMode({ isAdmin: true, active: true, everActive: false })).toBe("live");
    expect(resolveSectionMode({ isAdmin: true, active: false, everActive: true })).toBe("live");
  });
});

const render = (isAdmin: boolean, active: boolean) => {
  const run = vi.fn();
  const html = renderToStaticMarkup(
    React.createElement(StorybookSection, {
      runServiceAction: run,
      backendUrl: "http://localhost:8000/agent",
      isAdmin,
      active,
      notify: () => undefined,
      openRequest: null,
    }),
  );
  return { html, run };
};

describe("StorybookSection", () => {
  it("не админ: текст про права и ни одного вызова storybook.*", () => {
    const { html, run } = render(false, true);
    expect(html).toContain("Недостаточно прав");
    expect(run).not.toHaveBeenCalled();
  });
  it("спящий раздел ничего не рисует и не ходит в сеть", () => {
    const { html, run } = render(true, false);
    expect(html).toBe("");
    expect(run).not.toHaveBeenCalled();
  });
  it("активный раздел рисует навигацию из 8 пунктов", () => {
    const { html } = render(true, true);
    expect(html).toContain("sb-nav");
    expect((html.match(/class="sb-nav-item"/g) ?? []).length).toBe(8);
    expect(html).not.toContain("sb-top"); // без проекта верхней полосы нет
  });
});
