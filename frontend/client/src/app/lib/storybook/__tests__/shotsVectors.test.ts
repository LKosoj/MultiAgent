// Общие векторы (tests/fixtures/storybook_shots_vectors.json): те же случаи проверяет Tk-версия на Python.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { nextStatusOnKey } from "../review";
import { dirtyChanges, type ShotCard } from "../shots";
import { bulkTargetKeys, defaultWhat, filterFromIntent, leftColumnWidth, neighborKey, regenButton } from "../shotsView";
import { formatRubExact, pluralShots, shotErrorText } from "../shotsTexts";
import { columnsForWidth, layoutRows, visibleRange } from "../shotsWindow";

type Case = { in: unknown[]; out: unknown };
const path = fileURLToPath(new URL("../../../../../../../tests/fixtures/storybook_shots_vectors.json", import.meta.url));
const vectors = JSON.parse(readFileSync(path, "utf-8")) as Record<string, Case[]>;

// Имена функций Python -> вызов TS-версии
const impl: Record<string, (...a: any[]) => unknown> = {
  filter_from_intent: (name) => filterFromIntent(name),
  next_status_on_key: (status, key) => nextStatusOnKey(status, key),
  plural_shots: (n) => pluralShots(n),
  default_what: (stale, missing) => defaultWhat(stale, missing),
  regen_button: (cost, unknown) => regenButton(cost, unknown),
  format_rub_exact: (v) => formatRubExact(v),
  neighbor_key: (keys, current, delta) => neighborKey(keys, current, delta),
  bulk_target_keys: (order, marked, current) => bulkTargetKeys(order, marked, current),
  error_text: (code, reason, message) => shotErrorText(code, reason, message),
  dirty_changes: (card, form) => dirtyChanges(card as ShotCard, form),
  layout_rows: (keys: string[], mode, columns) =>
    layoutRows(
      keys.map((k) => ({ shot_key: k, scene: Number(k.split("-")[0]) })),
      mode,
      columns,
    ),
  visible_range: (rows, top, height, overscan) => visibleRange(rows, top, height, overscan),
  left_column_width: (w) => leftColumnWidth(w),
  columns_for_width: (w) => columnsForWidth(w),
};

describe("общие векторы", () => {
  it("в файле ровно 14 функций приложения Б", () => {
    expect(Object.keys(vectors).sort()).toEqual(Object.keys(impl).sort());
  });

  for (const [name, cases] of Object.entries(vectors)) {
    it(name, () => {
      const fn = impl[name];
      expect(fn, `нет реализации ${name}`).toBeTypeOf("function");
      cases.forEach((c, i) => {
        expect(fn(...c.in), `${name}[${i}] вход ${JSON.stringify(c.in)}`).toEqual(c.out);
      });
    });
  }
});
