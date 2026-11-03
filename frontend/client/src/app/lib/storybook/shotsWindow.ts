// Раскладка виртуального списка шотов: считает позиции строк и видимый диапазон.
// Алгоритм тот же, что в Tk (gui/shots/layout.py); оба сверяются общими векторами.
import { fmt } from "./shotsTexts";

export const HEADER_H = 32;
export const LIST_ROW_H = 64;
export const GRID_ROW_H = 120;
export const GRID_CELL_W = 132;
// поля колонки списка (полоса прокрутки и отступы), вычитаются из ширины перед делением на ячейки
export const GRID_PAD_W = 16;

export type ListMode = "list" | "grid";

export type LayoutRow =
  | { kind: "header"; scene: number; top: number; height: number }
  | { kind: "row" | "grid"; keys: string[]; top: number; height: number };

export type LayoutItem = { shot_key: string; scene: number };

// items идут в порядке показа; новая сцена начинается заголовком
export const layoutRows = (
  items: readonly LayoutItem[],
  mode: ListMode,
  columns: number,
): { rows: LayoutRow[]; total: number } => {
  const rows: LayoutRow[] = [];
  const cols = mode === "grid" ? Math.max(1, Math.floor(columns)) : 1;
  const rowHeight = mode === "grid" ? GRID_ROW_H : LIST_ROW_H;
  let top = 0;
  let scene: number | null = null;
  let pending: string[] = [];
  const flush = () => {
    if (pending.length === 0) return;
    rows.push({ kind: mode === "grid" ? "grid" : "row", keys: pending, top, height: rowHeight });
    top += rowHeight;
    pending = [];
  };
  for (const item of items) {
    if (scene !== item.scene) {
      flush();
      scene = item.scene;
      rows.push({ kind: "header", scene: item.scene, top, height: HEADER_H });
      top += HEADER_H;
    }
    pending.push(item.shot_key);
    if (pending.length === cols) flush();
  }
  flush();
  return { rows, total: top };
};

// Индексы первой и последней строки в окне прокрутки с запасом overscan (px). Пусто: [0, -1].
// Прокрутка за конец даёт последнюю строку.
export const visibleRange = (
  rows: readonly { top: number; height: number }[],
  scrollTop: number,
  viewportH: number,
  overscan = 0,
): [number, number] => {
  const n = rows.length;
  if (n === 0) return [0, -1];
  const lo = Math.max(0, scrollTop - overscan);
  const hi = scrollTop + viewportH + overscan;
  let first = 0;
  while (first < n && rows[first].top + rows[first].height <= lo) first += 1;
  if (first >= n) return [n - 1, n - 1];
  let last = first;
  while (last + 1 < n && rows[last + 1].top < hi) last += 1;
  return [first, last];
};

export const rowIndexOfKey = (rows: readonly LayoutRow[], key: string): number =>
  rows.findIndex((r) => r.kind !== "header" && r.keys.includes(key));

// Сколько ячеек сетки помещается в колонку данной ширины (не меньше одной)
export const columnsForWidth = (width: number): number =>
  Math.max(1, Math.floor((width - GRID_PAD_W) / GRID_CELL_W));

export const sceneHeaderText = (scene: number): string => fmt("scene_header", { n: scene });

// Прокручивать к выбранному шоту нужно один раз на ключ, как только его строка появилась в списке
export const shouldScrollToKey = (selectedKey: string | null, scrolledKey: string | null, rowFound: boolean): boolean =>
  selectedKey !== null && rowFound && selectedKey !== scrolledKey;
