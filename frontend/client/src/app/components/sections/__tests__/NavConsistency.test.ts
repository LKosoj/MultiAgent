import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { STORYBOOK_SCREENS } from "../../../lib/storybook/nav";
import { SCREEN_TITLES } from "../../../lib/storybook/texts";

describe("согласованность навигации Storybook", () => {
  it("у каждого экрана есть подпись, подписей лишних нет", () => {
    expect(Object.keys(SCREEN_TITLES).sort()).toEqual([...STORYBOOK_SCREENS].sort());
  });
  it("раздел подключён в page.tsx после Workflow", () => {
    const page = readFileSync(join(__dirname, "..", "..", "..", "page.tsx"), "utf-8");
    const wf = page.indexOf('{ id: "workflows"');
    const sb = page.indexOf('{ id: "storybook"');
    const tsql = page.indexOf('{ id: "text-to-sql"');
    expect(wf).toBeGreaterThan(-1);
    expect(sb).toBeGreaterThan(wf);
    expect(sb).toBeLessThan(tsql);
    expect(page).toContain('hidden={activeSection !== "storybook"}');
  });
});
