// Общие векторы экрана «Генерация» (tests/fixtures/storybook_generation_parity.json): те же случаи проверяет Tk-версия.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { estimateHeadline } from "../costView";
import { bannerFor, startButton, type StartInput } from "../generationModel";
import { GEN_TEXTS, STEP_STATUS_LABELS } from "../generationTexts";
import { parseLine } from "../logView";
import { rerunView } from "../runControl";
import type { RunState } from "../status";
import type { RunStatusResult } from "../runs";

type Case = { in: unknown[]; out: unknown };
type Fixture = {
  texts: Record<string, string>;
  step_status_labels: Record<string, string>;
  titles: Record<string, string>;
  start_button_base: Record<string, unknown>;
  vectors: Record<string, Case[]>;
};
const path = fileURLToPath(new URL("../../../../../../../tests/fixtures/storybook_generation_parity.json", import.meta.url));
const fx = JSON.parse(readFileSync(path, "utf-8")) as Fixture;
const titleOf = (id: string) => fx.titles[id] ?? id;

describe("паритет «Генерации»", () => {
  it("тексты совпадают с фикстурой", () => {
    expect({ ...GEN_TEXTS }).toEqual(fx.texts);
    expect(STEP_STATUS_LABELS).toEqual(fx.step_status_labels);
  });

  it("start_button", () => {
    for (const c of fx.vectors.start_button) {
      const input = { ...fx.start_button_base, ...(c.in[0] as object), titleOf } as unknown as StartInput;
      const v = startButton(input);
      expect({ label: v.label, enabled: v.enabled, reason: v.reason }).toEqual(c.out);
    }
  });

  it("rerun_view", () => {
    for (const c of fx.vectors.rerun_view) {
      expect(rerunView(c.in[0] as RunState, c.in[1] as boolean)).toEqual(c.out);
    }
  });

  it("estimate_headline", () => {
    for (const c of fx.vectors.estimate_headline) {
      expect(estimateHeadline(c.in[0] as Parameters<typeof estimateHeadline>[0])).toBe(c.out);
    }
  });

  it("parse_log_line", () => {
    for (const c of fx.vectors.parse_log_line) {
      const e = parseLine(c.in[0] as string);
      expect({ time: e.time, level: e.level, word: e.word, text: e.text }).toEqual(c.out);
    }
  });

  it("banner_for", () => {
    for (const c of fx.vectors.banner_for) {
      const base = { steps: [], locked: false, other_surface: null, engine: { available: true }, stale_control: false, unresponsive: false, failure: null, run: null };
      const b = bannerFor({ ...base, ...(c.in[0] as object) } as unknown as RunStatusResult, titleOf);
      expect(b ? { kind: b.kind, text: b.text } : null).toEqual(c.out);
    }
  });
});
