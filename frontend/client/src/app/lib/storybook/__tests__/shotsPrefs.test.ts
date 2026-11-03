import { describe, expect, it } from "vitest";

import {
  DEFAULT_PREFS,
  clearDraft,
  draftKey,
  loadDraft,
  loadPrefs,
  prefsKey,
  sanitizeDraft,
  sanitizePrefs,
  saveDraft,
  savePrefs,
  type StorageLike,
} from "../shotsPrefs";

const memory = (): StorageLike & { data: Map<string, string> } => {
  const data = new Map<string, string>();
  return {
    data,
    getItem: (k) => data.get(k) ?? null,
    setItem: (k, v) => void data.set(k, v),
    removeItem: (k) => void data.delete(k),
  };
};
const broken: StorageLike = {
  getItem: () => {
    throw new Error("denied");
  },
  setItem: () => {
    throw new Error("denied");
  },
};

describe("настройки экрана", () => {
  it("мусор превращается в значения по умолчанию", () => {
    expect(sanitizePrefs(null)).toEqual(DEFAULT_PREFS);
    expect(sanitizePrefs([1, 2])).toEqual(DEFAULT_PREFS);
    expect(sanitizePrefs({ filters: { scene: -1, state: "nope", review: "bad id!", predicate: "x" }, mode: "tiles", selected: "../x" })).toEqual(
      DEFAULT_PREFS,
    );
  });
  it("круг сохранить и загрузить", () => {
    const s = memory();
    const prefs = {
      filters: { scene: 2, review: "redo", state: "stale" as const, staleOnly: true, withFindings: true, query: "кот", predicate: "missing_videos" as const },
      mode: "grid" as const,
      selected: "2-3",
    };
    savePrefs(s, "p1", prefs);
    expect(s.data.has(prefsKey("p1"))).toBe(true);
    expect(loadPrefs(s, "p1")).toEqual(prefs);
    expect(loadPrefs(s, "p2")).toEqual(DEFAULT_PREFS);
  });
  it("битый JSON и недоступное хранилище не роняют", () => {
    const s = memory();
    s.setItem(prefsKey("p"), "{oops");
    expect(loadPrefs(s, "p")).toEqual(DEFAULT_PREFS);
    expect(loadPrefs(broken, "p")).toEqual(DEFAULT_PREFS);
    expect(() => savePrefs(broken, "p", DEFAULT_PREFS)).not.toThrow();
    expect(loadPrefs(null, "p")).toEqual(DEFAULT_PREFS);
  });
});

describe("черновики", () => {
  it("круг и очистка", () => {
    const s = memory();
    saveDraft(s, "p", "1-1", { changes: { video_prompt: "новый", duration_s: 7 }, note: "заметка", baseDigest: "j1:x" });
    expect(loadDraft(s, "p", "1-1")).toEqual({ changes: { video_prompt: "новый", duration_s: 7 }, note: "заметка", baseDigest: "j1:x" });
    clearDraft(s, "p", "1-1");
    expect(s.data.has(draftKey("p", "1-1"))).toBe(false);
    expect(loadDraft(s, "p", "1-1")).toBeNull();
  });
  it("лишние и неверные поля отбрасываются", () => {
    expect(sanitizeDraft({ changes: { video_prompt: 5, evil: "x", duration_s: -3 } })).toBeNull();
    expect(sanitizeDraft({ changes: { start_prompt: "a", evil: "x" } })).toEqual({ changes: { start_prompt: "a" }, note: null, baseDigest: null });
    expect(sanitizeDraft("text")).toBeNull();
  });
  it("сбой хранилища даёт null", () => {
    expect(loadDraft(broken, "p", "1-1")).toBeNull();
    expect(() => saveDraft(broken, "p", "1-1", { changes: {}, note: "x", baseDigest: null })).not.toThrow();
    expect(() => clearDraft(broken, "p", "1-1")).not.toThrow();
  });
});
