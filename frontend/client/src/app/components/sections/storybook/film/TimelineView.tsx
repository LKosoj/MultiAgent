"use client";

import React, { useEffect, useMemo, useRef, useState } from "react";

import { FILM_TEXTS } from "../../../../lib/storybook/filmTexts";
import {
  TRACK_ORDER,
  TRACK_TITLES,
  clampScale,
  fitScale,
  labelVisible,
  mergeNarrow,
  outOfCutText,
  rulerTicks,
  zoomIn,
  zoomOut,
  type FilmLayout,
  type LayoutBlock,
  type TrackId,
} from "../../../../lib/storybook/filmView";
import { scrollForCursor, timeFromX } from "../../../../lib/storybook/playerModel";

export type ZoomCommand = { direction: "in" | "out" | "fit"; n: number } | null;

type Props = {
  layout: FilmLayout;
  time: number;
  // подписи дорожек без блоков (озвучка не подключена и т.п.)
  notes: Partial<Record<TrackId, string>>;
  zoomCommand: ZoomCommand;
  onSeek: (t: number) => void;
  onOpenShot: (key: string) => void;
};

const LABEL_COL_PX = 150;
const INITIAL_VIEW_PX = 800;

export function TimelineView({ layout, time, notes, zoomCommand, onSeek, onOpenShot }: Props) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [viewWidth, setViewWidth] = useState(INITIAL_VIEW_PX);
  const [custom, setCustom] = useState<number | null>(null);
  const duration = layout.duration_s;
  const fit = fitScale(viewWidth, duration);
  const pxPerS = clampScale(custom ?? fit, fit, duration);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const update = () => setViewWidth(Math.max(200, el.clientWidth));
    update();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const lastCommand = useRef<number>(0);
  useEffect(() => {
    if (!zoomCommand || zoomCommand.n === lastCommand.current) return;
    lastCommand.current = zoomCommand.n;
    setCustom((cur) => {
      const now = clampScale(cur ?? fit, fit, duration);
      if (zoomCommand.direction === "fit") return null;
      return zoomCommand.direction === "in" ? zoomIn(now, fit, duration) : zoomOut(now, fit, duration);
    });
  }, [zoomCommand, fit, duration]);

  // курсор не уходит из видимой области
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const next = scrollForCursor(time * pxPerS, el.scrollLeft, el.clientWidth);
    if (next !== el.scrollLeft) el.scrollLeft = next;
  }, [time, pxPerS]);

  const byTrack = useMemo(() => {
    const out: Record<TrackId, LayoutBlock[]> = { shots: [], narration: [], music: [], sfx: [], subtitles: [] };
    for (const b of layout.blocks) out[b.track].push(b);
    out.subtitles = mergeNarrow(out.subtitles, pxPerS);
    return out;
  }, [layout, pxPerS]);

  const width = Math.max(1, duration * pxPerS);
  const ticks = rulerTicks(duration, pxPerS);
  const outText = outOfCutText(layout.out_of_cut);

  // Ctrl+колесо - масштаб; слушатель не пассивный, иначе страница прокрутится вместе с масштабом
  const scaleRef = useRef({ pxPerS, fit, duration });
  scaleRef.current = { pxPerS, fit, duration };
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const onWheel = (ev: WheelEvent) => {
      if (!ev.ctrlKey || ev.deltaY === 0) return;
      ev.preventDefault();
      const { pxPerS: now, fit: f, duration: d } = scaleRef.current;
      setCustom(ev.deltaY < 0 ? zoomIn(now, f, d) : zoomOut(now, f, d));
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);

  const seekFromClick = (ev: React.MouseEvent<HTMLDivElement>) => {
    // клик по кнопке-блоку не перемотка: у блока свои действия
    if ((ev.target as HTMLElement).closest?.("button")) return;
    const rect = ev.currentTarget.getBoundingClientRect();
    onSeek(timeFromX(ev.clientX - rect.left, 0, pxPerS, duration));
  };

  return (
    <div className="sb-film-timeline" role="group" aria-label={FILM_TEXTS.title}>
      <div className="sb-film-zoom">
        <button type="button" className="button ghost" onClick={() => setCustom(zoomIn(pxPerS, fit, duration))}>
          {FILM_TEXTS.zoom_in}
        </button>
        <button type="button" className="button ghost" onClick={() => setCustom(zoomOut(pxPerS, fit, duration))}>
          {FILM_TEXTS.zoom_out}
        </button>
        <button type="button" className="button ghost" onClick={() => setCustom(null)}>
          {FILM_TEXTS.zoom_fit}
        </button>
      </div>
      <div className="sb-film-tl-grid" style={{ gridTemplateColumns: `${LABEL_COL_PX}px minmax(0, 1fr)` }}>
        <div className="sb-film-labels">
          <div className="sb-film-label sb-film-label-ruler" aria-hidden="true" />
          {TRACK_ORDER.map((id) => (
            <div key={id} className="sb-film-label" data-track={id}>
              {TRACK_TITLES[id]}
            </div>
          ))}
        </div>
        <div className="sb-film-scroll" ref={scrollRef}>
          <div className="sb-film-content" style={{ width }}>
            <div className="sb-film-ruler" onClick={seekFromClick}>
              {ticks.map((tick) => (
                <span key={tick.t} className="sb-film-tick" style={{ left: tick.t * pxPerS }}>
                  {tick.label}
                </span>
              ))}
            </div>
            {TRACK_ORDER.map((id) => (
              <div key={id} className="sb-film-track" data-track={id} onClick={seekFromClick}>
                {byTrack[id].length === 0 && notes[id] ? <span className="sb-film-track-note">{notes[id]}</span> : null}
                {byTrack[id].map((b, i) => {
                  const w = Math.max(2, (b.end_s - b.start_s) * pxPerS);
                  return (
                    <button
                      key={`${b.key ?? "m"}-${i}`}
                      type="button"
                      className="sb-film-block"
                      data-tone={b.tone}
                      data-nested={b.nested ? "true" : undefined}
                      data-approximate={b.approximate ? "true" : undefined}
                      data-warning={b.warning ? "true" : undefined}
                      style={{ left: b.start_s * pxPerS, width: w }}
                      title={b.tooltip}
                      onDoubleClick={id === "shots" && b.key ? () => onOpenShot(b.key as string) : undefined}
                      onKeyDown={
                        id === "shots" && b.key
                          ? (e) => {
                              if (e.key === "Enter" || e.key === " ") {
                                e.preventDefault();
                                e.stopPropagation();
                                onOpenShot(b.key as string);
                              }
                            }
                          : undefined
                      }
                    >
                      {labelVisible(w) ? b.label : null}
                    </button>
                  );
                })}
              </div>
            ))}
            <div className="sb-film-cursor" style={{ left: time * pxPerS }} aria-hidden="true" />
          </div>
        </div>
      </div>
      {outText ? <p className="sb-muted">{outText}</p> : null}
    </div>
  );
}
