"use client";

import React, { useMemo, useState } from "react";

import type { ProjectCard } from "../../../lib/storybook/projects";
import { filterProjects } from "../../../lib/storybook/projectsView";
import { T, nothingFound } from "../../../lib/storybook/texts";
import { StudioModal } from "./StudioModal";

type Props = { projects: readonly ProjectCard[]; onPick: (projectId: string) => void; onClose: () => void };

// Быстрый выбор проекта по Ctrl/Cmd+K
export function ProjectPalette({ projects, onPick, onClose }: Props) {
  const [query, setQuery] = useState("");
  const [index, setIndex] = useState(0);
  const list = useMemo(() => filterProjects(projects, query).slice(0, 50), [projects, query]);
  const current = Math.min(index, Math.max(0, list.length - 1));

  const onKeyDown = (ev: React.KeyboardEvent) => {
    if (ev.key === "ArrowDown") {
      ev.preventDefault();
      setIndex(Math.min(current + 1, list.length - 1));
    } else if (ev.key === "ArrowUp") {
      ev.preventDefault();
      setIndex(Math.max(current - 1, 0));
    } else if (ev.key === "Enter" && list[current]) {
      ev.preventDefault();
      onPick(list[current].project_id);
    }
  };

  return (
    <StudioModal title={T.topProject.replace(":", "")} onClose={onClose}>
      <input
        data-autofocus="true"
        type="search"
        value={query}
        onChange={(e) => {
          setQuery(e.target.value);
          setIndex(0);
        }}
        onKeyDown={onKeyDown}
        placeholder={T.searchHint}
        aria-label={T.search}
      />
      {list.length === 0 ? (
        <p className="sb-muted">{query ? nothingFound(query) : T.emptyProjects}</p>
      ) : (
        <ul className="sb-palette-list" role="listbox" aria-label={T.projectsTitle}>
          {list.map((p, i) => (
            <li key={p.project_id} role="presentation">
              <button
                type="button"
                role="option"
                aria-selected={i === current}
                className="sb-palette-item"
                onClick={() => onPick(p.project_id)}
              >
                {p.title} <span className="sb-muted">· {p.project_id}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </StudioModal>
  );
}
