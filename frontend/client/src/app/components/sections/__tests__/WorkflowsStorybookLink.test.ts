import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const read = (rel: string) => readFileSync(join(__dirname, "..", "..", "..", rel), "utf-8");

describe("ссылка из Workflow на Storybook", () => {
  const workflows = read("components/sections/WorkflowsSection.tsx");
  const page = read("page.tsx");

  it("старая проверка readiness/validation/inventory убрана", () => {
    expect(workflows).not.toMatch(/\bstorybookReadiness(?!")|storybook_validate|storybook_project_inventory/);
    expect(page).not.toMatch(/\bstorybookReadiness(?!")|storybook_readiness/);
  });
  it("есть кнопка «Открыть в Storybook»", () => {
    expect(workflows).toContain("Открыть в Storybook");
    expect(workflows).toContain("onOpenStorybook");
    expect(page).toContain("onOpenStorybook={openStorybook}");
  });
});
