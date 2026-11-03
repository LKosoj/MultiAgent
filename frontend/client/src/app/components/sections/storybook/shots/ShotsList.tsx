"use client";

import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import type { ShotMediaApi } from "../../../../lib/storybook/useShotMedia";
import type { ShotRow } from "../../../../lib/storybook/shots";
import { SHOT_TEXTS } from "../../../../lib/storybook/shotsTexts";
import {
  columnsForWidth,
  layoutRows,
  rowIndexOfKey,
  shouldScrollToKey,
  sceneHeaderText,
  visibleRange,
  type ListMode,
} from "../../../../lib/storybook/shotsWindow";

type Props = {
  items: readonly ShotRow[];
  mode: ListMode;
  selectedKey: string | null;
  marked: ReadonlySet<string>;
  media: ShotMediaApi;
  hasMore: boolean;
  loadingMore: boolean;
  onLoadMore: () => void;
  onSelect: (key: string) => void;
  onMark: (key: string) => void;
};

const OVERSCAN_PX = 240;

function Thumb({ row, media }: { row: ShotRow; media: ShotMediaApi }) {
  const src = media.thumb(row.thumb_path, 128, row.thumb_rev);
  if (row.state === "generating") return <div className="sb-shots-thumb sb-muted">{SHOT_TEXTS.generating}</div>;
  if (!src) return <div className="sb-shots-thumb sb-muted">{SHOT_TEXTS.no_image}</div>;
  // eslint-disable-next-line @next/next/no-img-element
  return <img className="sb-shots-thumb" src={src} alt="" loading="lazy" onError={media.onImageError} />;
}

const Cell = React.memo(function Cell({
  row,
  mode,
  selected,
  marked,
  media,
  onSelect,
  onMark,
}: {
  row: ShotRow;
  mode: ListMode;
  selected: boolean;
  marked: boolean;
  media: ShotMediaApi;
  onSelect: (key: string) => void;
  onMark: (key: string) => void;
}) {
  const findings = row.findings ? row.findings.error + row.findings.warning : 0;
  return (
    <div className="sb-shots-item" data-mode={mode} data-selected={selected ? "true" : "false"} data-state={row.state}>
      <input
        type="checkbox"
        className="sb-shots-mark"
        checked={marked}
        aria-label={row.shot_key}
        onChange={() => onMark(row.shot_key)}
      />
      <button type="button" className="sb-shots-open" onClick={() => onSelect(row.shot_key)} aria-current={selected ? "true" : undefined}>
        <Thumb row={row} media={media} />
        <span className="sb-shots-meta">
          <strong>{row.shot_key}</strong>
          <span className="sb-muted">{row.state_title}</span>
          {mode === "list" ? <span className="sb-shots-preview sb-muted">{row.prompt_preview}</span> : null}
          <span className="sb-shots-badges">
            {row.review ? <span data-review={row.review.status}>{row.review.status_title}</span> : null}
            {findings > 0 ? (
              <span data-findings="true" aria-label={`${SHOT_TEXTS.findings_short}: ${findings}`}>
                {SHOT_TEXTS.findings_short}: {findings}
              </span>
            ) : null}
          </span>
        </span>
      </button>
    </div>
  );
});

// Виртуальный список: в DOM только строки в окне прокрутки
export function ShotsList({ items, mode, selectedKey, marked, media, hasMore, loadingMore, onLoadMore, onSelect, onMark }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const [box, setBox] = useState({ w: 400, h: 600 });
  const [scrollTop, setScrollTop] = useState(0);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => setBox({ w: el.clientWidth, h: el.clientHeight });
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const columns = mode === "grid" ? columnsForWidth(box.w) : 1;
  const { rows, total } = useMemo(
    () => layoutRows(items.map((r) => ({ shot_key: r.shot_key, scene: r.scene })), mode, columns),
    [items, mode, columns],
  );
  const byKey = useMemo(() => new Map(items.map((r) => [r.shot_key, r])), [items]);
  const [first, last] = visibleRange(rows, scrollTop, box.h, OVERSCAN_PX);

  // выбранный шот должен быть виден (стрелки, переход из другого экрана); только при смене выбора,
  // иначе подгрузка страниц возвращала бы список назад
  const rowsRef = useRef(rows);
  useEffect(() => {
    rowsRef.current = rows;
  });
  const scrolledKey = useRef<string | null>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const idx = selectedKey ? rowIndexOfKey(rows, selectedKey) : -1;
    if (!shouldScrollToKey(selectedKey, scrolledKey.current, idx >= 0)) return;
    scrolledKey.current = selectedKey;
    const r = rows[idx];
    if (r.top < el.scrollTop) el.scrollTop = r.top;
    else if (r.top + r.height > el.scrollTop + el.clientHeight) el.scrollTop = r.top + r.height - el.clientHeight;
  }, [selectedKey, rows]);

  // дошли до конца загруженного: просим следующую страницу
  useEffect(() => {
    if (hasMore && !loadingMore && last >= rows.length - 1) onLoadMore();
  }, [hasMore, loadingMore, last, rows.length, onLoadMore]);

  return (
    <div
      ref={ref}
      className="sb-shots-scroll"
      onScroll={(e) => setScrollTop(e.currentTarget.scrollTop)}
      role="region"
      aria-label={SHOT_TEXTS.title}
    >
      <div style={{ height: total, position: "relative" }}>
        {rows.slice(first, last + 1).map((r, i) =>
          r.kind === "header" ? (
            <div key={`h${r.scene}`} className="sb-shots-scene" role="heading" aria-level={3} style={{ top: r.top, height: r.height }}>
              {sceneHeaderText(r.scene)}
            </div>
          ) : (
            <div key={`r${first + i}`} className="sb-shots-row" data-mode={mode} style={{ top: r.top, height: r.height }}>
              {r.keys.map((k) => {
                const row = byKey.get(k);
                return row ? (
                  <Cell
                    key={k}
                    row={row}
                    mode={mode}
                    selected={k === selectedKey}
                    marked={marked.has(k)}
                    media={media}
                    onSelect={onSelect}
                    onMark={onMark}
                  />
                ) : null;
              })}
            </div>
          ),
        )}
      </div>
    </div>
  );
}
