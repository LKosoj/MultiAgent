"use client";

import React from "react";

import type { BackgroundQueue } from "../../../lib/storybook/backgroundQueue";
import type { StorybookClient } from "../../../lib/storybook/client";
import type { ProjectCard as Card } from "../../../lib/storybook/projects";
import { cardModified, cardSubtitle } from "../../../lib/storybook/projectsView";
import { T } from "../../../lib/storybook/texts";

type Props = {
  card: Card;
  sizeBytes: number | undefined;
  selected: boolean;
  client: StorybookClient;
  background: BackgroundQueue;
  onSelect: (id: string) => void;
  onOpen: (id: string) => void;
};

// Карточка постоянного проекта и число его серий.
export function ProjectCard({ card, sizeBytes, selected, onSelect, onOpen }: Props) {

  return (
    <button
      type="button"
      className="sb-card"
      aria-pressed={selected}
      data-project-id={card.project_id}
      onClick={() => onSelect(card.project_id)}
      onDoubleClick={() => onOpen(card.project_id)}
      onKeyDown={(ev) => {
        if (ev.key === "Enter") {
          ev.preventDefault();
          onOpen(card.project_id);
        }
      }}
    >
      <div className="sb-card-title">{card.title}</div>
      <div className="sb-muted">{cardSubtitle(card, sizeBytes)}</div>
      <div className="sb-muted">{cardModified(card)}</div>
      <div className="sb-row" style={{ gap: 6 }}>
        {card.busy ? <span className="status-tag" data-status="running">{T.runBadge}</span> : null}
        {card.state === "creating" ? <span className="status-tag">{T.creatingBadge}</span> : null}
        {card.state === "empty" ? <span className="status-tag">{T.emptyBadge}</span> : null}
      </div>
    </button>
  );
}
