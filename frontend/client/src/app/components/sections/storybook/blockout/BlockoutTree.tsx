"use client";

import React, { useRef } from "react";

import { groupByScene, type BlockoutStateResult } from "../../../../lib/storybook/blockout";
import { BLOCKOUT_TEXTS } from "../../../../lib/storybook/blockoutTexts";
import { orderedShotKeys, treeRows, type Selection } from "../../../../lib/storybook/blockoutView";

type Props = {
  state: Pick<BlockoutStateResult, "scenes" | "shots">;
  selection: Selection;
  onSelect: (key: string, mods: { ctrl?: boolean; shift?: boolean }) => void;
};

// Дерево «сцена - цепочка - шот». Состояние пишется словом, цвет только помогает
export function BlockoutTree({ state, selection, onSelect }: Props) {
  const rows = treeRows(state);
  const ordered = orderedShotKeys(groupByScene(state));
  const current = selection.focus;

  const listRef = useRef<HTMLUListElement>(null);
  // активный пункт клавиатуры: при Shift-выборе он не совпадает с последним ключом выбора
  const cursor = useRef<string | null>(null);

  const focusShot = (key: string) => {
    const el = Array.from(listRef.current?.querySelectorAll<HTMLElement>('[role="option"]') ?? []).find((n) => n.dataset.shotKey === key);
    el?.focus();
  };

  const onKeyDown = (ev: React.KeyboardEvent) => {
    if (ev.key !== "ArrowDown" && ev.key !== "ArrowUp") return;
    if (ordered.length === 0) return;
    ev.preventDefault();
    const from = cursor.current && ordered.includes(cursor.current) ? cursor.current : current;
    const at = from ? ordered.indexOf(from) : -1;
    const next = ev.key === "ArrowDown" ? Math.min(ordered.length - 1, at + 1) : Math.max(0, at <= 0 ? 0 : at - 1);
    cursor.current = ordered[next];
    onSelect(ordered[next], { shift: ev.shiftKey });
    // фокус идёт за выбором: так пункт озвучит скринридер
    requestAnimationFrame(() => focusShot(ordered[next]));
  };

  return (
    <nav className="sb-blockout-tree" aria-label={BLOCKOUT_TEXTS.tree_title}>
      <ul ref={listRef} role="listbox" aria-multiselectable="true" aria-label={BLOCKOUT_TEXTS.tree_title} onKeyDown={onKeyDown}>
        {rows.map((row) => {
          if (row.kind !== "shot") {
            return (
              <li key={row.id} role="presentation" className="sb-blockout-group" data-depth={row.depth} data-kind={row.kind}>
                {row.label}
              </li>
            );
          }
          const selected = selection.keys.includes(row.key);
          return (
            <li key={row.id} role="presentation" data-depth={row.depth}>
              <button
                type="button"
                role="option"
                aria-selected={selected}
                tabIndex={current === row.key || (!current && row.key === ordered[0]) ? 0 : -1}
                className="sb-blockout-shot"
                data-state={row.state}
                data-shot-key={row.key}
                onClick={(ev) => {
                  cursor.current = row.key;
                  onSelect(row.key, { ctrl: ev.ctrlKey || ev.metaKey, shift: ev.shiftKey });
                }}
              >
                <span>{row.label}</span>
                <span className="sb-blockout-state">{row.stateTitle}</span>
                {row.marks.map((m) => (
                  <span key={m} className="sb-blockout-mark">
                    {m}
                  </span>
                ))}
              </button>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
