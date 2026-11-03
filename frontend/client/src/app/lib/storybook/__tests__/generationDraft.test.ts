import { describe, expect, it } from "vitest";

import { formKey, loadForm, loadSettingsDraft, saveForm, saveSettingsDraft, settingsKey } from "../generationDraft";
import type { RunSettings } from "../runSettings";

const memory = () => {
  const m = new Map<string, string>();
  return {
    m,
    getItem: (k: string) => m.get(k) ?? null,
    setItem: (k: string, v: string) => void m.set(k, v),
    removeItem: (k: string) => void m.delete(k),
  };
};

describe("черновик экрана «Генерация»", () => {
  it("сказка и бриф переживают уход с экрана", () => {
    const st = memory();
    saveForm(st, "p", { task: "Сказка про лису", brief: { pages_min: "5", generate_end_shots: true }, regenWhat: "video" });
    expect(loadForm(st, "p")).toEqual({ task: "Сказка про лису", brief: { pages_min: "5", generate_end_shots: true }, regenWhat: "video" });
    expect(loadForm(st, "q")).toBeNull();
  });
  it("пустая форма не хранится", () => {
    const st = memory();
    saveForm(st, "p", { task: "x", brief: {}, regenWhat: "both" });
    saveForm(st, "p", { task: "", brief: {}, regenWhat: "both" });
    expect(st.m.has(formKey("p"))).toBe(false);
  });
  it("испорченное хранилище не ломает экран", () => {
    const st = memory();
    st.m.set(formKey("p"), "{не json");
    expect(loadForm(st, "p")).toBeNull();
    expect(loadForm(null, "p")).toBeNull();
    expect(() => saveForm(null, "p", { task: "x", brief: {}, regenWhat: "both" })).not.toThrow();
  });
  it("настройки подставляются только к той же сохранённой версии", () => {
    const st = memory();
    const s = { budget_rub: 5 } as unknown as RunSettings;
    saveSettingsDraft(st, "p", "d1", s);
    expect(loadSettingsDraft(st, "p", "d1")).toEqual(s);
    expect(loadSettingsDraft(st, "p", "d2")).toBeNull();
    saveSettingsDraft(st, "p", "d1", null);
    expect(st.m.has(settingsKey("p"))).toBe(false);
  });
});
