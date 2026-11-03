// Золотые файлы R3 (tests/fixtures/storybook_screens/): те же случаи проверяет Tk-версия.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { editButton, editCanSubmit, editErrorText, editableExtension } from "../imageEditFlow";
import { fileInfoLines, galleryFilter, galleryGroups } from "../galleryView";
import { IMAGE_TEXTS } from "../imageToolsTexts";
import { fitScale, panAllowed, zoomPercent, zoomStep } from "../imageZoom";

type Case = { in: unknown[]; out: unknown };
const load = (name: string) =>
  JSON.parse(readFileSync(fileURLToPath(new URL(`../../../../../../../tests/fixtures/storybook_screens/${name}`, import.meta.url)), "utf-8"));
const J = (v: unknown) => JSON.parse(JSON.stringify(v));
const vectors = load("golden_imagetools_vectors.json").vectors as Record<string, Case[]>;
// все случаи прогоняются через функцию; результат сравнивается с золотым
const check = (name: string, f: (...a: never[]) => unknown, norm: (v: unknown) => unknown = (v) => v) => {
  const cases = vectors[name];
  expect(cases.length).toBeGreaterThan(0);
  for (const c of cases) expect(norm(J(f(...(c.in as never[]))))).toEqual(norm(c.out));
};

describe("паритет зума, правки и галереи: тексты", () => {
  it("тексты совпадают с золотым (golden_imagetools_texts.json)", () => {
    expect({ ...IMAGE_TEXTS }).toEqual(load("golden_imagetools_texts.json"));
  });
  it("в тексте нет эмодзи", () => {
    for (const t of Object.values(IMAGE_TEXTS)) expect(t).not.toMatch(/[\u{1F300}-\u{1FAFF}]/u);
  });
});

describe("паритет: общие векторы (golden_imagetools_vectors.json)", () => {
  it("zoom_step", () => check("zoom_step", zoomStep as never));
  it("fit_scale", () => check("fit_scale", fitScale as never));
  it("zoom_percent", () => check("zoom_percent", zoomPercent as never));
  it("pan_allowed", () => check("pan_allowed", panAllowed as never));
  it("edit_can_submit", () => check("edit_can_submit", editCanSubmit as never));
  it("edit_button", () => check("edit_button", editButton as never));
  it("edit_error_text", () => check("edit_error_text", editErrorText as never));
  it("editable_extension", () => check("editable_extension", editableExtension as never));
  it("gallery_groups", () => check("gallery_groups", galleryGroups as never));
  it("gallery_filter", () => check("gallery_filter", galleryFilter as never));
  it("file_info_lines", () => check("file_info_lines", fileInfoLines as never));
});
