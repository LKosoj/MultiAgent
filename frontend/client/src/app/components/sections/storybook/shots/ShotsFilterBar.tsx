"use client";

import React, { useEffect, useState } from "react";

import type { ShotsUiFilters } from "../../../../lib/storybook/shotsPrefs";
import { SHOT_TEXTS, fmt } from "../../../../lib/storybook/shotsTexts";
import { hasActiveFilters, type ListData } from "../../../../lib/storybook/shotsView";
import type { ListMode } from "../../../../lib/storybook/shotsWindow";

type Props = {
  filters: ShotsUiFilters;
  data: ListData;
  found: number;
  mode: ListMode;
  onFilters: (patch: Partial<ShotsUiFilters>) => void;
  onReset: () => void;
  onMode: (mode: ListMode) => void;
};

const SEARCH_DELAY_MS = 300;

// Полоса фильтров над списком; поиск уходит на сервер с небольшой задержкой
export function ShotsFilterBar({ filters, data, found, mode, onFilters, onReset, onMode }: Props) {
  const [query, setQuery] = useState(filters.query);
  useEffect(() => setQuery(filters.query), [filters.query]);
  useEffect(() => {
    if (query === filters.query) return;
    const timer = setTimeout(() => onFilters({ query }), SEARCH_DELAY_MS);
    return () => clearTimeout(timer);
  }, [query, filters.query, onFilters]);

  const scenes = data.facets?.scenes ?? [];
  const reviewIds = Object.keys(data.reviewTitles);
  const stateIds = Object.keys(data.stateTitles);

  return (
    <div className="sb-shots-filters">
      <label className="sb-shots-field">
        <span className="sb-muted">{SHOT_TEXTS.filter_scene}</span>
        <select
          value={filters.scene === null ? "" : String(filters.scene)}
          onChange={(e) => onFilters({ scene: e.target.value === "" ? null : Number(e.target.value) })}
        >
          <option value="">{SHOT_TEXTS.filter_scene_all}</option>
          {scenes.map((s) => (
            <option key={s.scene} value={s.scene}>
              {fmt("filter_scene_n", { n: s.scene })}
            </option>
          ))}
        </select>
      </label>
      <label className="sb-shots-field">
        <span className="sb-muted">{SHOT_TEXTS.filter_review}</span>
        <select value={filters.review} onChange={(e) => onFilters({ review: e.target.value })}>
          <option value="">{SHOT_TEXTS.filter_all}</option>
          {reviewIds.map((id) => (
            <option key={id} value={id}>
              {data.reviewTitles[id]}
            </option>
          ))}
        </select>
      </label>
      <label className="sb-shots-field">
        <span className="sb-muted">{SHOT_TEXTS.filter_state}</span>
        <select value={filters.state} onChange={(e) => onFilters({ state: e.target.value as ShotsUiFilters["state"] })}>
          <option value="">{SHOT_TEXTS.filter_all}</option>
          {stateIds.map((id) => (
            <option key={id} value={id}>
              {data.stateTitles[id]}
            </option>
          ))}
        </select>
      </label>
      <label className="toggle">
        <input type="checkbox" checked={filters.staleOnly} onChange={(e) => onFilters({ staleOnly: e.target.checked })} />
        <span>{SHOT_TEXTS.filter_stale_only}</span>
      </label>
      <label className="toggle">
        <input type="checkbox" checked={filters.withFindings} onChange={(e) => onFilters({ withFindings: e.target.checked })} />
        <span>{SHOT_TEXTS.filter_with_findings}</span>
      </label>
      <input
        type="search"
        className="sb-shots-search"
        value={query}
        placeholder={SHOT_TEXTS.filter_search}
        aria-label={SHOT_TEXTS.filter_search}
        onChange={(e) => setQuery(e.target.value)}
      />
      {hasActiveFilters(filters) ? (
        <button type="button" className="button ghost" onClick={onReset}>
          {SHOT_TEXTS.reset_filters}
        </button>
      ) : null}
      <span className="sb-muted sb-shots-found" role="status">
        {fmt("found", { n: found })}
      </span>
      <div className="sb-shots-modes" role="group">
        <button type="button" className="button secondary" aria-pressed={mode === "grid"} onClick={() => onMode("grid")}>
          {SHOT_TEXTS.view_grid}
        </button>
        <button type="button" className="button secondary" aria-pressed={mode === "list"} onClick={() => onMode("list")}>
          {SHOT_TEXTS.view_list}
        </button>
      </div>
    </div>
  );
}
