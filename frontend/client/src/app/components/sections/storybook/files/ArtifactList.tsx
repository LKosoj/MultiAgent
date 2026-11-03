"use client";

import React from "react";

import type { ArtifactItem } from "../../../../lib/storybook/artifacts";
import { FILES_TEXTS } from "../../../../lib/storybook/filesTexts";
import { listRows } from "../../../../lib/storybook/filesView";

type Props = {
  items: readonly ArtifactItem[];
  selected: string | null;
  onPick: (key: string) => void;
};

// Список файлов: слева колонкой; на узком экране вместо неё выпадающий список
export function ArtifactList({ items, selected, onPick }: Props) {
  const rows = listRows(items);
  return (
    <nav className="sb-files-list" aria-label={FILES_TEXTS.title}>
      <ul className="sb-files-list-items">
        {rows.map((r) => (
          <li key={r.key}>
            <button
              type="button"
              className="sb-files-row"
              aria-current={r.key === selected ? "true" : undefined}
              data-status={r.status}
              title={r.tooltip}
              onClick={() => onPick(r.key)}
            >
              <span className="sb-files-row-title">{r.title}</span>
              <span className="sb-files-row-meta">
                <span className="sb-files-status" data-status={r.status}>{r.statusText}</span>
                {r.badges.map((b) => (
                  <span key={b} className="sb-files-badge">{b}</span>
                ))}
              </span>
            </button>
          </li>
        ))}
      </ul>
      <label className="sb-files-select">
        <span className="label">{FILES_TEXTS.list_label}</span>
        <select value={selected ?? ""} onChange={(e) => e.target.value && onPick(e.target.value)}>
          <option value="" disabled>
            {FILES_TEXTS.empty}
          </option>
          {rows.map((r) => (
            <option key={r.key} value={r.key}>
              {r.title} ({r.statusText})
            </option>
          ))}
        </select>
      </label>
    </nav>
  );
}
