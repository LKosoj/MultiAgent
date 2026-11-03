"use client";

import React, { useEffect, useState } from "react";

import { IMAGE_TEXTS } from "../../../../lib/storybook/imageToolsTexts";
import { SHOT_TEXTS, fmt } from "../../../../lib/storybook/shotsTexts";
import { stepIndex, type ViewerImage } from "../../../../lib/storybook/shotsView";
import type { ShotMediaApi } from "../../../../lib/storybook/useShotMedia";
import { ZoomImage } from "../imagetools/ZoomImage";
import { StudioModal } from "../StudioModal";

type Props = {
  images: ViewerImage[];
  // с какой картинки открыть (по пути файла)
  startRel: string;
  media: ShotMediaApi;
  // время изменения файлов, правленых ИИ: ссылка получает &v=, браузер не берёт старую картинку из кэша
  edited?: Readonly<Record<string, number>>;
  // кнопка «Править ИИ…» для картинки (начало, конец); null - для этой картинки её нет
  onEdit?: (rel: string) => void;
  canEdit?: (rel: string) => boolean;
  // причина, по которой правка закрыта; null - можно
  editBlocked?: string | null;
  onClose: () => void;
};

// Крупный просмотр картинок шота; ←/→ листают
export function ShotViewer({ images, startRel, media, edited, onEdit, canEdit, editBlocked = null, onClose }: Props) {
  const [index, setIndex] = useState(() => Math.max(0, images.findIndex((i) => i.rel === startRel)));
  const current = images[index];

  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key !== "ArrowLeft" && ev.key !== "ArrowRight") return;
      ev.preventDefault();
      setIndex((i) => stepIndex(i, images.length, ev.key === "ArrowRight" ? 1 : -1));
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [images.length]);

  const src = current ? media.media(current.rel, edited?.[current.rel] != null ? String(edited[current.rel]) : null) : null;
  return (
    <StudioModal title={fmt("viewer_title", { title: current?.title ?? "" })} onClose={onClose} wide>
      <div className="sb-shots-viewer">
        {src && current ? (
          <ZoomImage src={src} alt={current.title} onError={media.onImageError} globalKeys />
        ) : (
          <p className="sb-muted">{SHOT_TEXTS.no_file}</p>
        )}
      </div>
      <p className="sb-muted">{SHOT_TEXTS.viewer_hint}</p>
      <div className="button-row">
        <button type="button" className="button ghost" data-autofocus="true" onClick={onClose}>
          {SHOT_TEXTS.close}
        </button>
        {onEdit && current && canEdit?.(current.rel) ? (
          <button type="button" className="button" disabled={editBlocked !== null} onClick={() => onEdit(current.rel)}>
            {IMAGE_TEXTS.edit_open}
          </button>
        ) : null}
      </div>
    </StudioModal>
  );
}
