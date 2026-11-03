"use client";

import React, { useEffect, useRef, useState } from "react";

import type { BackgroundQueue } from "../../../lib/storybook/backgroundQueue";
import type { StorybookClient } from "../../../lib/storybook/client";
import type { ProjectCard as Card } from "../../../lib/storybook/projects";
import { cardModified, cardSubtitle } from "../../../lib/storybook/projectsView";
import { T } from "../../../lib/storybook/texts";
import { useCoverUrl } from "../../../lib/storybook/useCoverUrl";

type Props = {
  card: Card;
  sizeBytes: number | undefined;
  selected: boolean;
  client: StorybookClient;
  background: BackgroundQueue;
  onSelect: (id: string) => void;
  onOpen: (id: string) => void;
};

// Карточка проекта; обложку и размер грузим, только когда карточка попала на экран
export function ProjectCard({ card, sizeBytes, selected, client, background, onSelect, onOpen }: Props) {
  const ref = useRef<HTMLButtonElement>(null);
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof IntersectionObserver === "undefined") {
      setVisible(true);
      return;
    }
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) {
        setVisible(true);
        observer.disconnect();
      }
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  const cover = useCoverUrl(client, background, card.project_id, card.cover, visible);

  return (
    <button
      ref={ref}
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
      <div className="sb-cover">{cover ? (
          // eslint-disable-next-line @next/next/no-img-element -- адрес с токеном, оптимизатор Next не нужен
          <img src={cover} alt="" loading="lazy" />
        ) : (
          T.noCover
        )}</div>
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
