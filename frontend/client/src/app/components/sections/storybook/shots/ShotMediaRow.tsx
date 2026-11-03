"use client";

import React from "react";

import type { ShotCard, ShotPartName } from "../../../../lib/storybook/shots";
import { IMAGE_TEXTS } from "../../../../lib/storybook/imageToolsTexts";
import { SHOT_TEXTS, fmt } from "../../../../lib/storybook/shotsTexts";
import { blockoutRel, hasAnyBlockout, partTitle } from "../../../../lib/storybook/shotsView";
import type { ShotMediaApi } from "../../../../lib/storybook/useShotMedia";

type Props = {
  card: ShotCard;
  media: ShotMediaApi;
  // открыть крупный просмотр картинки
  onView: (rel: string) => void;
  // править ИИ начало или конец; без этого свойства кнопок нет
  onEdit?: (part: "start" | "end") => void;
  // причина, по которой правка закрыта; null - можно
  editBlocked?: string | null;
  // время изменения файлов, правленых ИИ (по пути): ссылка получает &v=
  edited?: Readonly<Record<string, number>>;
};

// Подпись-ссылка: открывает файл в новой вкладке
function Caption({ title, href }: { title: string; href: string | null }) {
  return href ? (
    <figcaption>
      <a href={href} target="_blank" rel="noreferrer" className="sb-muted">
        {title}
      </a>
    </figcaption>
  ) : (
    <figcaption className="sb-muted">{title}</figcaption>
  );
}

function ImageSlot({ title, rel, rev, media, busy, missingText, full = false, onView, onEdit, editBlocked = null, editedTs }: {
  title: string;
  rel: string | null;
  rev: string | null;
  media: ShotMediaApi;
  busy: boolean;
  missingText: string;
  // файл болванки: миниатюры для него нет, берём сам файл
  full?: boolean;
  onView: Props["onView"];
  onEdit?: () => void;
  editBlocked?: string | null;
  editedTs?: number;
}) {
  const edited = editedTs === undefined ? rev : `${editedTs}${rev ?? ""}`;
  const src = full ? media.media(rel, edited) : media.thumb(rel, 512, edited);
  return (
    <figure className="sb-shots-slot">
      <Caption title={title} href={media.media(rel, edited)} />
      {busy ? (
        <div className="sb-shots-frame sb-muted">{SHOT_TEXTS.generating}</div>
      ) : src && rel ? (
        <button type="button" className="sb-shots-frame" onClick={() => onView(rel)}>
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src={src} alt={title} onError={media.onImageError} />
        </button>
      ) : (
        <div className="sb-shots-frame sb-muted">{missingText}</div>
      )}
      {onEdit && rel && !busy ? (
        <button type="button" className="button ghost" disabled={editBlocked !== null} title={editBlocked ?? undefined} onClick={onEdit}>
          {IMAGE_TEXTS.edit_open}
        </button>
      ) : null}
    </figure>
  );
}

// key={src}: при новой ссылке (возврат версии, новый токен) ошибка прошлой загрузки забывается
function VideoPlayer({ src, poster }: { src: string; poster: string | null }) {
  const [sound, setSound] = React.useState(false);
  const [failed, setFailed] = React.useState(false);
  if (failed) return <div className="sb-shots-frame sb-muted">{SHOT_TEXTS.video_failed}</div>;
  return (
    <>
      <video className="sb-shots-video" src={src} poster={poster ?? undefined} controls muted={!sound} preload="metadata" onError={() => setFailed(true)} />
      <button type="button" className="button ghost" onClick={() => setSound((v) => !v)}>
        {sound ? SHOT_TEXTS.no_sound : SHOT_TEXTS.watch_with_sound}
      </button>
    </>
  );
}

function VideoSlot({ title, rel, media, shared }: { title: string; rel: string | null; media: ShotMediaApi; shared: string[] }) {
  const src = media.media(rel);
  return (
    <figure className="sb-shots-slot">
      <Caption title={title} href={src} />
      {src && rel ? <VideoPlayer key={src} src={src} poster={media.thumb(rel, 512, null)} /> : <div className="sb-shots-frame sb-muted">{SHOT_TEXTS.part_missing}</div>}
      {shared.length > 1 ? <span className="sb-muted">{fmt("video_shared", { keys: shared.join(", ") })}</span> : null}
    </figure>
  );
}

// Начало, конец, видео и (если есть) болванка
export function ShotMediaRow({ card, media, onView, onEdit, editBlocked, edited }: Props) {
  const part = (name: ShotPartName) => (card.parts[name].exists ? card.parts[name].path : null);
  const busy = (name: ShotPartName) => card.parts[name].state === "generating";
  const missing = (name: ShotPartName) => (card.parts[name].state === "missing" ? SHOT_TEXTS.part_missing : SHOT_TEXTS.no_image);
  const blockout = hasAnyBlockout(card);
  return (
    <div className="sb-shots-media">
      <ImageSlot title={partTitle("start")} rel={part("start")} rev={card.thumb_rev} media={media} busy={busy("start")} missingText={missing("start")} onView={onView} onEdit={onEdit && (() => onEdit("start"))} editBlocked={editBlocked} editedTs={part("start") ? edited?.[part("start") as string] : undefined} />
      {card.parts.end.state === "not_applicable" ? null : (
        <ImageSlot title={partTitle("end")} rel={part("end")} rev={null} media={media} busy={busy("end")} missingText={missing("end")} onView={onView} onEdit={onEdit && (() => onEdit("end"))} editBlocked={editBlocked} editedTs={part("end") ? edited?.[part("end") as string] : undefined} />
      )}
      {card.parts.video.state === "not_applicable" ? null : <VideoSlot title={partTitle("video")} rel={part("video")} media={media} shared={card.parts.video.unit?.keys ?? []} />}
      {blockout ? (
        <>
          {card.blockout.start ? (
            <ImageSlot
              title={`${SHOT_TEXTS.part_blockout}: ${partTitle("start")}`}
              rel={blockoutRel(card.scene, card.shot, "start")}
              rev={null}
              media={media}
              busy={false}
              missingText={SHOT_TEXTS.no_image}
              full
              onView={onView}
            />
          ) : null}
          {card.blockout.end ? (
            <ImageSlot
              title={`${SHOT_TEXTS.part_blockout}: ${partTitle("end")}`}
              rel={blockoutRel(card.scene, card.shot, "end")}
              rev={null}
              media={media}
              busy={false}
              missingText={SHOT_TEXTS.no_image}
              full
              onView={onView}
            />
          ) : null}
          {card.blockout.video ? (
            <VideoSlot title={`${SHOT_TEXTS.part_blockout}: ${partTitle("video")}`} rel={blockoutRel(card.scene, card.shot, "video")} media={media} shared={[]} />
          ) : null}
        </>
      ) : (
        <span className="sb-muted">{SHOT_TEXTS.blockout_missing}</span>
      )}
    </div>
  );
}
