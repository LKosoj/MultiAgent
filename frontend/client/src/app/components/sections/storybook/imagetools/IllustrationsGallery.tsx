"use client";

import React, { useMemo, useState } from "react";

import { editableExtension } from "../../../../lib/storybook/imageEditFlow";
import { fileInfoLines, galleryFilterItems, galleryGroupItems } from "../../../../lib/storybook/galleryView";
import { IMAGE_TEXTS, imageFmt } from "../../../../lib/storybook/imageToolsTexts";
import { formatDateTimeRu } from "../../../../lib/storybook/format";
import { SHOT_TEXTS } from "../../../../lib/storybook/shotsTexts";
import { formatBytes } from "../../../../lib/storybook/shotsView";
import { PROJECT_LOCKED_TEXT } from "../../../../lib/storybook/texts";
import type { ImageEditApi } from "../../../../lib/storybook/useImageEdit";
import type { IllustrationsApi } from "../../../../lib/storybook/useIllustrations";
import type { ShotMediaApi } from "../../../../lib/storybook/useShotMedia";
import { Banner } from "../Banner";
import { Skeleton } from "../Skeleton";
import { ZoomImage } from "./ZoomImage";

type Props = {
  list: IllustrationsApi;
  edit: ImageEditApi;
  media: ShotMediaApi;
  // прогон идёт: правка закрыта
  runBusy: boolean;
};

// Режим «Иллюстрации» на экране «Файлы»: страницы слева, картинка с зумом и свойства справа
export function IllustrationsGallery({ list, edit, media, runBusy }: Props) {
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [dims, setDims] = useState<{ path: string; w: number; h: number } | null>(null);

  const groups = useMemo(() => galleryGroupItems(galleryFilterItems(list.items, query)), [list.items, query]);
  const current = list.items.find((i) => i.path === selected) ?? groups[0]?.items[0] ?? null;

  if (list.status === "error" && list.items.length === 0) {
    return (
      <Banner
        tone="error"
        actions={
          <button type="button" className="button secondary" onClick={list.reload}>
            {SHOT_TEXTS.retry}
          </button>
        }
      >
        {IMAGE_TEXTS.gallery_load_error}: {list.error}
      </Banner>
    );
  }
  if (list.status !== "ready" && list.items.length === 0) return <Skeleton lines={5} label={IMAGE_TEXTS.gallery_count.replace("{n}", "…")} />;
  if (list.items.length === 0) return <p className="sb-muted">{IMAGE_TEXTS.gallery_empty}</p>;

  const src = current ? media.media(current.path, String(current.modified_ts)) : null;
  const size = dims && current && dims.path === current.path ? dims : null;
  const lines = current ? fileInfoLines(current.name, formatBytes(current.size), formatDateTimeRu(current.modified), size?.w, size?.h) : [];
  const blocked = runBusy ? PROJECT_LOCKED_TEXT : edit.running ? IMAGE_TEXTS.edit_running : null;
  return (
    <div className="sb-imagetools-gallery">
      <div className="sb-imagetools-list">
        <p className="sb-muted">{imageFmt("gallery_count", { n: list.items.length })}</p>
        <input type="search" value={query} placeholder={IMAGE_TEXTS.gallery_search} aria-label={IMAGE_TEXTS.gallery_search} onChange={(e) => setQuery(e.target.value)} />
        {groups.map((g) => (
          <section key={g.page ?? "other"} className="sb-imagetools-group">
            <h3 className="sb-title">{g.title}</h3>
            <ul>
              {g.items.map((i) => (
                <li key={i.path}>
                  <button type="button" className="sb-imagetools-item" aria-pressed={current?.path === i.path} onClick={() => setSelected(i.path)}>
                    {i.name}
                  </button>
                </li>
              ))}
            </ul>
          </section>
        ))}
      </div>
      <div className="sb-imagetools-view">
        {edit.running ? <Banner tone="info">{IMAGE_TEXTS.edit_running}</Banner> : null}
        {current && src ? (
          <>
            <ZoomImage src={src} alt={current.name} onError={media.onImageError} onSize={(s) => setDims({ path: current.path, ...s })} />
            <ul className="sb-imagetools-info">
              {lines.map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
            <div className="button-row">
              <button
                type="button"
                className="button"
                disabled={blocked !== null || !editableExtension(current.name)}
                onClick={() => edit.begin({ kind: "illustration", path: current.path, name: current.name })}
              >
                {IMAGE_TEXTS.edit_open}
              </button>
            </div>
            {blocked ? <p className="sb-muted">{blocked}</p> : null}
            {!editableExtension(current.name) ? <p className="sb-muted">{IMAGE_TEXTS.edit_not_supported}</p> : null}
          </>
        ) : null}
      </div>
    </div>
  );
}
