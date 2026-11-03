"use client";

import React, { useState } from "react";

import type { BlockoutCompare as Images, BlockoutDetailResult, BlockoutFileRef, BlockoutShotDetail } from "../../../../lib/storybook/blockout";
import { BLOCKOUT_TEXTS } from "../../../../lib/storybook/blockoutTexts";
import { canApplyDuration, durationView } from "../../../../lib/storybook/blockoutView";
import { Skeleton } from "../Skeleton";
import { StudioModal } from "../StudioModal";

type Props = {
  detail: BlockoutDetailResult | null;
  detailError: string | null;
  hasSelection: boolean;
  images: Images | null;
  fileUrl: (ref: BlockoutFileRef | null) => string | null;
  durationChoice: number | null;
  onDurationChoice: (n: number | null) => void;
  onApplyDuration: () => void;
  durationBusy: boolean;
  rebuildHint: boolean;
  onImageError: (ev: { currentTarget: { src: string } }) => void;
};

const SLOTS: { id: keyof Images; text: "img_blockout_start" | "img_image_start" | "img_image_end" | "img_blockout_end" }[] = [
  { id: "blockout_start", text: "img_blockout_start" },
  { id: "image_start", text: "img_image_start" },
  { id: "image_end", text: "img_image_end" },
  { id: "blockout_end", text: "img_blockout_end" },
];

const isShot = (d: BlockoutDetailResult | null): d is BlockoutShotDetail => !!d && "state" in d;

const noteLevel = (level: string | null): string =>
  level === "error" ? BLOCKOUT_TEXTS.level_error : level === "warning" ? BLOCKOUT_TEXTS.level_warning : level === "info" ? BLOCKOUT_TEXTS.level_info : level ?? "—";

// Правая колонка: четыре картинки, два ролика, замечания и длительность выбранного шота
export function BlockoutCompare(p: Props) {
  const [zoomed, setZoomed] = useState<{ title: string; url: string } | null>(null);
  if (!p.hasSelection) return <p className="sb-muted">{BLOCKOUT_TEXTS.compare_hint}</p>;
  if (p.detailError) return <p role="alert" className="sb-tone-danger">{p.detailError}</p>;
  if (!isShot(p.detail)) return <Skeleton lines={3} label={BLOCKOUT_TEXTS.loading} />;
  const d = p.detail;
  const dur = durationView(d);
  const refUrl = p.fileUrl(d.videos.blockout_ref);
  const finalUrl = p.fileUrl(d.videos.final);

  return (
    <div className="sb-blockout-compare">
      <h3 className="sb-h3">{BLOCKOUT_TEXTS.compare_title}</h3>
      <div className="sb-blockout-images">
        {SLOTS.map((slot) => {
          const thumb = p.images?.[slot.id] ?? null;
          const full = p.fileUrl(d.images[slot.id]);
          const title = BLOCKOUT_TEXTS[slot.text];
          return (
            <figure key={slot.id} className="sb-blockout-figure" data-slot={slot.id}>
              {thumb ? (
                <button type="button" className="sb-blockout-thumb" onClick={() => full && setZoomed({ title, url: full })} aria-label={title}>
                  {/*  */}
                  {/* eslint-disable-next-line @next/next/no-img-element -- адрес с токеном, оптимизатор Next не нужен */}
                  <img src={thumb} alt={title} loading="lazy" onError={p.onImageError} />
                </button>
              ) : (
                <div className="sb-blockout-thumb sb-blockout-thumb-empty">{BLOCKOUT_TEXTS.img_none}</div>
              )}
              <figcaption>{title}</figcaption>
            </figure>
          );
        })}
      </div>
      <div className="sb-blockout-videos">
        <figure>
          {refUrl ? <video controls preload="none" src={refUrl} /> : <div className="sb-blockout-thumb sb-blockout-thumb-empty">{BLOCKOUT_TEXTS.img_none}</div>}
          <figcaption>{BLOCKOUT_TEXTS.video_ref}</figcaption>
        </figure>
        <figure>
          {finalUrl ? <video controls preload="none" src={finalUrl} /> : <div className="sb-blockout-thumb sb-blockout-thumb-empty">{BLOCKOUT_TEXTS.img_none}</div>}
          <figcaption>{BLOCKOUT_TEXTS.video_final}</figcaption>
        </figure>
      </div>
      <section aria-label={BLOCKOUT_TEXTS.notes_title}>
        <h4 className="sb-h3">{BLOCKOUT_TEXTS.notes_title}</h4>
        {d.notes.length === 0 ? (
          <p className="sb-muted">{BLOCKOUT_TEXTS.notes_none}</p>
        ) : (
          <table className="sb-film-table">
            <thead>
              <tr>
                <th scope="col">{BLOCKOUT_TEXTS.note_level}</th>
                <th scope="col">{BLOCKOUT_TEXTS.note_text}</th>
              </tr>
            </thead>
            <tbody>
              {d.notes.map((n, i) => (
                <tr key={i}>
                  <td>{noteLevel(n.level)}</td>
                  <td>{n.message ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
      <div className="sb-blockout-duration">
        <label>
          <span>{BLOCKOUT_TEXTS.duration_label}</span>
          <select
            value={p.durationChoice ?? dur.current ?? ""}
            disabled={!dur.editable || p.durationBusy}
            onChange={(e) => p.onDurationChoice(e.target.value === "" ? null : Number(e.target.value))}
          >
            {dur.current !== null && !dur.allowed.includes(dur.current) ? <option value={dur.current}>{dur.current}</option> : null}
            {dur.allowed.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          className="button"
          aria-disabled={canApplyDuration(dur, p.durationChoice) && !p.durationBusy ? undefined : "true"}
          onClick={() => (canApplyDuration(dur, p.durationChoice) && !p.durationBusy ? p.onApplyDuration() : undefined)}
        >
          {BLOCKOUT_TEXTS.duration_apply}
        </button>
        {dur.reason ? <p className="sb-muted">{dur.reason}</p> : null}
        {p.rebuildHint ? <p className="sb-tone-warn">{BLOCKOUT_TEXTS.duration_rebuild}</p> : null}
      </div>
      {zoomed ? (
        <StudioModal title={zoomed.title} onClose={() => setZoomed(null)} wide>
          {/* eslint-disable-next-line @next/next/no-img-element -- адрес с токеном, оптимизатор Next не нужен */}
          <img className="sb-blockout-zoom" src={zoomed.url} alt={zoomed.title} />
          <div className="button-row">
            <button type="button" className="button ghost" data-autofocus="true" onClick={() => setZoomed(null)}>
              {BLOCKOUT_TEXTS.viewer_close}
            </button>
          </div>
        </StudioModal>
      ) : null}
    </div>
  );
}
