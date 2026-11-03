import { describe, expect, it } from "vitest";

import {
  buildStartPayload,
  editButton,
  editCanSubmit,
  editCostLine,
  editErrorMessage,
  editErrorText,
  normalizeReferences,
  referenceCandidates,
  referenceLabel,
  toggleReference,
  type EditTarget,
  type ImageEditPreview,
} from "../imageEditFlow";
import { IMAGE_TEXTS } from "../imageToolsTexts";
import { PROJECT_LOCKED_TEXT } from "../texts";

const preview = (estimate: Partial<ImageEditPreview["estimate"]> = {}): ImageEditPreview => ({
  target: { kind: "shot", path: "97_shots/1-1/start.png", name: "start.png", shot_key: "1-1", part: "start", size: 1, modified_ts: 1 },
  references: [],
  limits: { prompt_min: 5, prompt_max: 2000, references_max: 3, extensions: [".png"] },
  estimate: { cost_rub: 0, unknown_cost: true, price_per_image_rub: null, ...estimate },
  problems: [],
  can_start: true,
  plan_digest: "d1",
});
const shot: EditTarget = { kind: "shot", shotKey: "1-1", part: "start", path: "97_shots/1-1/start.png", name: "start.png" };
const illustration: EditTarget = { kind: "illustration", path: "50_images/p1.png", name: "p1.png" };

describe("кнопка и отправка", () => {
  it("кнопка включается от 5 символов после обрезки пробелов, когда цена известна", () => {
    expect(editCanSubmit("  abcd  ", 0, true)).toBe(false);
    expect(editCanSubmit("abcde", 0, true)).toBe(true);
    expect(editCanSubmit("abcde", 0, false)).toBe(false);
    expect(editCanSubmit("abcde", 4, true)).toBe(false);
  });
  it("подпись: с суммой, без оценки, бесплатно", () => {
    expect(editButton(5, false).label).toBe("Править за 5\u00a0₽");
    expect(editButton(0, true)).toEqual({ label: "Править без оценки", note: "edit_cost_unknown" });
    expect(editButton(0, false)).toEqual({ label: "Править", note: null });
  });
  it("тексты ошибок", () => {
    expect(editErrorText("conflict", "plan_changed")).toBe("edit_plan_changed");
    expect(editErrorText("conflict", "cost_changed")).toBe("edit_cost_changed");
    expect(editErrorText("conflict", "edit_running")).toBe("edit_busy");
    expect(editErrorText("conflict", "cost_confirmation_required", true)).toBe("edit_confirm_unknown");
    expect(editErrorText("conflict", "cost_confirmation_required", false)).toBe("edit_cost_changed");
    expect(editErrorText("conflict", "cost_changed", true)).toBe("edit_cost_changed");
    expect(editErrorText("locked", null)).toBe("edit_busy");
    expect(editErrorText("internal", null)).toBe("edit_failed");
    expect(editErrorMessage("locked", null)).toBe(IMAGE_TEXTS.edit_busy);
    expect(editErrorMessage("locked", null)).not.toBe(PROJECT_LOCKED_TEXT);
  });
  it("неизвестная цена: confirm_unknown_cost, суммы нет", () => {
    const p = buildStartPayload("proj", shot, "  сделай небо розовым ", [], preview());
    expect(p).toMatchObject({ project_id: "proj", shot_key: "1-1", part: "start", prompt: "сделай небо розовым", expected_plan_digest: "d1", confirm_unknown_cost: true, surface: "react" });
    expect(p).not.toHaveProperty("confirm_cost_rub");
    expect(p).not.toHaveProperty("path");
  });
  it("известная цена: confirm_cost_rub из показанного предпросмотра, без confirm_unknown_cost", () => {
    const p = buildStartPayload("proj", illustration, "abcde", ["20_bible/a.png"], preview({ cost_rub: 5, unknown_cost: false, price_per_image_rub: 5 }));
    expect(p).toMatchObject({ path: "50_images/p1.png", confirm_cost_rub: 5, reference_paths: ["20_bible/a.png"] });
    expect(p).not.toHaveProperty("confirm_unknown_cost");
    expect(p).not.toHaveProperty("shot_key");
  });
  it("бесплатно и известно: подтверждений нет", () => {
    const p = buildStartPayload("proj", shot, "abcde", [], preview({ cost_rub: 0, unknown_cost: false, price_per_image_rub: 0 }));
    expect(p).not.toHaveProperty("confirm_cost_rub");
    expect(p).not.toHaveProperty("confirm_unknown_cost");
  });
});

describe("образцы", () => {
  const items = [
    { path: "50_images/p1.png", name: "p1.png", servable: true },
    { path: "20_bible/hero.png", name: "hero.png", servable: true },
    { path: "20_bible/anim.gif", name: "anim.gif", servable: true },
    { path: "20_bible/raw.bmp", name: "raw.bmp", servable: false },
  ];
  it("текущая картинка и неподходящие форматы в выбор не попадают", () => {
    const list = referenceCandidates(items, "50_images/p1.png");
    expect(list.map((c) => c.path)).toEqual(["20_bible/hero.png"]);
    expect(list[0].label).toBe("20_bible/hero.png");
  });
  it("подпись «папка/имя»", () => {
    expect(referenceLabel("a/b/c.png")).toBe("b/c.png");
    expect(referenceLabel("c.png")).toBe("c.png");
  });
  it("не больше трёх, повторный выбор снимает", () => {
    expect(toggleReference(["a", "b", "c"], "d")).toEqual(["a", "b", "c"]);
    expect(toggleReference(["a", "b"], "a")).toEqual(["b"]);
    expect(toggleReference(["a"], "b")).toEqual(["a", "b"]);
  });
  it("normalizeReferences убирает текущую, повторы и лишнее", () => {
    expect(normalizeReferences(["x", "cur", "x", "a", "b", "c"], "cur")).toEqual(["x", "a", "b"]);
  });
});

describe("строка цены", () => {
  it("известная цена - «Цена правки: …», неизвестная - пояснение", () => {
    expect(editCostLine(5, false)).toMatch(/^Цена правки: .*5/);
    expect(editCostLine(0, false)).toMatch(/^Цена правки: /);
    expect(editCostLine(0, true)).toBe(IMAGE_TEXTS.edit_cost_unknown);
  });
});
