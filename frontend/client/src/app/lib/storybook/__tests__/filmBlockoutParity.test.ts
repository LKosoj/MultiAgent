// Золотые файлы У9 (tests/fixtures/storybook_screens/): те же случаи проверяет Tk-версия.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { groupByScene } from "../blockout";
import { BLOCKOUT_TEXTS } from "../blockoutTexts";
import {
  actionButtons, applySelection, canApplyDuration, durationView, orderedShotKeys, planDialogView, planTarget,
  pruneSelection, resolveBlockoutScreen, summaryLine, toolsLine, treeRows,
} from "../blockoutView";
import { FILM_TEXTS } from "../filmTexts";
import {
  blockAt, buildLayout, clampScale, cueAt, fitScale, rulerTicks, runTransition, tickStep, zoomIn, zoomOut, resolveFilmState,
} from "../filmView";
import type { SubtitlesResult, TimelineResult } from "../media";
import { clampTime, formatPosition, keyAction, timeFromX } from "../playerModel";

type Case = { in: unknown[]; out: unknown };
const load = (name: string) =>
  JSON.parse(readFileSync(fileURLToPath(new URL(`../../../../../../../tests/fixtures/storybook_screens/${name}`, import.meta.url)), "utf-8"));

describe("паритет «Фильма»", () => {
  it("тексты совпадают с золотым файлом", () => {
    expect({ ...FILM_TEXTS }).toEqual(load("golden_film_texts.json"));
  });
  it("раскладка дорожек совпадает", () => {
    const fx = load("golden_film_layout.json") as { cases: { timeline: TimelineResult; cues: SubtitlesResult["cues"]; layout: unknown }[] };
    expect(fx.cases.length).toBeGreaterThan(1);
    for (const c of fx.cases) expect(JSON.parse(JSON.stringify(buildLayout(c.timeline, c.cues)))).toEqual(c.layout);
  });
  it("простые векторы совпадают", () => {
    const fx = load("golden_film_vectors.json") as { cues: SubtitlesResult["cues"]; vectors: Record<string, Case[]> };
    const v = fx.vectors;
    const run = (name: string, f: (...a: never[]) => unknown) => {
      for (const c of v[name]) expect(JSON.parse(JSON.stringify(f(...(c.in as never[]))))).toEqual(c.out);
    };
    run("tick_step", tickStep as never);
    run("fit_scale", fitScale as never);
    run("clamp_scale", clampScale as never);
    run("zoom_in", zoomIn as never);
    run("zoom_out", zoomOut as never);
    run("ruler_ticks", ((a: number, b: number) => rulerTicks(a, b).map((t) => [t.t, t.label])) as never);
    run("cue_at", ((t: number) => cueAt(fx.cues as never, t)?.index ?? null) as never);
    run("format_position", formatPosition as never);
    run("clamp_time", clampTime as never);
    run("time_from_x", timeFromX as never);
    run("run_transition", runTransition as never);
    void blockAt;
    void resolveFilmState;
  });
  it("клавиши плеера", () => {
    const fx = load("golden_film_vectors.json") as { vectors: Record<string, Case[]> };
    for (const c of fx.vectors.key_action) {
      const [key, shift, inField] = c.in as [string, boolean, boolean];
      expect(JSON.parse(JSON.stringify(keyAction({ key, shiftKey: shift }, inField)))).toEqual(c.out);
    }
  });
});

describe("паритет «Болванки»", () => {
  it("тексты совпадают с золотым файлом", () => {
    expect({ ...BLOCKOUT_TEXTS }).toEqual(load("golden_blockout_texts.json"));
  });
  it("векторы совпадают", () => {
    const fx = load("golden_blockout_vectors.json") as { states: Record<string, never>; vectors: Record<string, Case[]> };
    const v = fx.vectors;
    const same = (got: unknown, want: unknown) => expect(JSON.parse(JSON.stringify(got))).toEqual(want);
    const base = fx.states.base as never;
    const ordered = orderedShotKeys(groupByScene(base));
    for (const c of v.tree_rows) same(treeRows((c.in[0]) as never), c.out);
    for (const c of v.ordered_keys) same(orderedShotKeys(groupByScene(c.in[0] as never)), c.out);
    for (const c of v.select) {
      const [sel, key, mods] = c.in as [never, string, never];
      same(applySelection(sel, ordered, key, mods), c.out);
    }
    for (const c of v.prune_selection) {
      const [sel, keys] = c.in as [never, string[]];
      same(pruneSelection(sel, { shots: keys.map((key) => ({ key })) } as never), c.out);
    }
    for (const c of v.summary_line) same(summaryLine(c.in[0] as never), c.out);
    for (const c of v.tools_line) same(toolsLine(c.in[0] as never), c.out);
    for (const c of v.action_buttons) {
      const [name, keys, ctx] = c.in as [string, string[], never];
      same(actionButtons(fx.states[name], keys, ctx).map((b) => ({ action: b.action, enabled: b.enabled, hint: b.hint })), c.out);
    }
    for (const c of v.plan_target) {
      const [action, keys] = c.in as [never, string[]];
      same(planTarget(action, base, keys), c.out);
    }
    for (const c of v.plan_dialog) same(planDialogView(c.in[0] as never, c.in[1] as never, (c.in[2] as number | undefined) ?? 0), c.out);
    for (const c of v.duration_view) {
      const [d, chosen] = c.in as [Record<string, unknown>, number];
      const view = durationView({ duration: { ...d, requested_s: null, source: null } } as never);
      same({ view, can_apply: canApplyDuration(view, chosen) }, c.out);
    }
    void resolveBlockoutScreen;
  });
});
