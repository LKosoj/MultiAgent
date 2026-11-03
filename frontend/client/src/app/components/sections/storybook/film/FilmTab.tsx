"use client";

import React, { useEffect, useMemo, useRef, useState } from "react";

import { FILM_TEXTS, filmFmt } from "../../../../lib/storybook/filmTexts";
import { buildLayout, cueAt, type FilmLayout, type TrackId } from "../../../../lib/storybook/filmView";
import type { SubtitleCue } from "../../../../lib/storybook/media";
import { isTypingElement, keyAction } from "../../../../lib/storybook/playerModel";
import type { FilmApi } from "../../../../lib/storybook/useFilm";
import { Banner } from "../Banner";
import { Skeleton } from "../Skeleton";
import { FilmPlayer, type PlayerHandle } from "./FilmPlayer";
import { FilmVersions } from "./FilmVersions";
import { FinalFileInfo } from "./FinalFileInfo";
import { MusicPanel } from "./MusicPanel";
import { SubtitlesPanel } from "./SubtitlesPanel";
import { TimelineView, type ZoomCommand } from "./TimelineView";

type Props = {
  api: FilmApi;
  projectName: string;
  onGoGeneration: () => void;
  onOpenShot: (key: string) => void;
  onOpenReport: () => void;
};

type Tab = "player" | "subtitles" | "music" | "file" | "versions";
const TABS: { id: Tab; text: "tab_player" | "tab_subtitles" | "tab_music" | "tab_file" | "tab_versions" }[] = [
  { id: "player", text: "tab_player" },
  { id: "subtitles", text: "tab_subtitles" },
  { id: "music", text: "tab_music" },
  { id: "file", text: "tab_file" },
  { id: "versions", text: "tab_versions" },
];

const EMPTY_CUES: SubtitleCue[] = [];

export function FilmTab({ api, projectName, onGoGeneration, onOpenShot, onOpenReport }: Props) {
  const { state, timeline } = api;
  const [tab, setTab] = useState<Tab>("player");
  const [time, setTime] = useState(0);
  const [zoom, setZoom] = useState<ZoomCommand>(null);
  const handle = useRef<PlayerHandle | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const zoomSeq = useRef(0);
  const cues = api.subtitles?.cues ?? EMPTY_CUES;

  const layout: FilmLayout | null = useMemo(() => (timeline ? buildLayout(timeline, cues) : null), [timeline, cues]);
  const notes = useMemo(() => {
    const out: Partial<Record<TrackId, string>> = {};
    if (!timeline) return out;
    for (const id of ["narration", "music", "sfx"] as const) {
      const t = timeline.tracks[id];
      if (t.clips.length === 0 && t.status !== "present" && t.note) out[id] = t.note;
    }
    return out;
  }, [timeline]);

  // клавиши слушает корень: без фокуса внутри они бы не работали
  useEffect(() => {
    rootRef.current?.focus({ preventScroll: true });
  }, []);

  const onMouseDown = (ev: React.MouseEvent) => {
    if (!isTypingElement(ev.target as HTMLElement | null)) rootRef.current?.focus({ preventScroll: true });
  };

  const onKeyDown = (ev: React.KeyboardEvent) => {
    const action = keyAction(ev, isTypingElement(ev.target as HTMLElement | null));
    if (!action) return;
    if (action.type === "zoom") {
      zoomSeq.current += 1;
      setZoom({ direction: action.direction, n: zoomSeq.current });
    } else if (action.type === "toggle") handle.current?.toggle();
    else if (action.type === "seek_by") handle.current?.seekBy(action.delta);
    else if (action.type === "mute") handle.current?.mute();
    else handle.current?.fullscreen();
    ev.preventDefault();
  };

  const blocked = api.actionBlock;
  const rebuildButton = (
    <button
      type="button"
      className="button"
      aria-disabled={blocked || api.rebuilding ? "true" : undefined}
      title={blocked ?? undefined}
      onClick={() => (blocked || api.rebuilding ? undefined : void api.rebuild())}
    >
      {FILM_TEXTS.rebuild}
    </button>
  );

  const seek = (t: number) => (handle.current ? handle.current.seek(t) : setTime(t));

  return (
    <div className="sb-screen sb-film" data-tab={tab} data-state={state} tabIndex={-1} ref={rootRef} onKeyDown={onKeyDown} onMouseDown={onMouseDown}>
      <header className="sb-film-head">
        <h2 className="sb-title">{filmFmt("title_project", { name: projectName })}</h2>
        {rebuildButton}
      </header>
      {blocked ? <p className="sb-muted">{blocked}</p> : null}
      {api.refreshFailed ? <Banner tone="warn">{FILM_TEXTS.refresh_failed}</Banner> : null}
      {api.banners.map((b) => (
        <Banner key={b.id} tone={b.tone}>
          {b.text}
        </Banner>
      ))}
      {state === "loading" ? <Skeleton lines={4} label={FILM_TEXTS.loading} /> : null}
      {state === "error" ? (
        <Banner
          tone="error"
          actions={
            <button type="button" className="button" onClick={api.reload}>
              {FILM_TEXTS.retry}
            </button>
          }
        >
          {filmFmt("load_error", { reason: (api.error ?? "").replace(/[.]$/, "") })}
        </Banner>
      ) : null}
      {timeline && layout && (state === "ready" || state === "partial" || state === "empty") ? (
        <>
          {state !== "ready" ? (
            <div className="sb-empty" data-empty="film">
              <p>{FILM_TEXTS.empty}</p>
              <button type="button" className="button" onClick={onGoGeneration}>
                {FILM_TEXTS.empty_goto}
              </button>
            </div>
          ) : null}
          {state === "empty" && layout.blocks.length === 0 ? null : (
            <>
              <div className="sb-film-tabs" role="tablist" aria-label={FILM_TEXTS.title}>
                {TABS.map((t) => (
                  <button
                    key={t.id}
                    type="button"
                    role="tab"
                    id={`sb-film-tab-${t.id}`}
                    aria-selected={tab === t.id}
                    aria-controls={`sb-film-pane-${t.id}`}
                    className="button ghost"
                    onClick={() => setTab(t.id)}
                  >
                    {FILM_TEXTS[t.text]}
                  </button>
                ))}
              </div>
              <div className="sb-film-grid">
                <div className="sb-film-main sb-film-pane" data-pane="player" id="sb-film-pane-player" role="tabpanel" aria-labelledby="sb-film-tab-player">
                  {state === "ready" ? (
                    <FilmPlayer
                      timeline={timeline}
                      cues={cues}
                      media={api.media}
                      source={api.source}
                      onSource={api.setSource}
                      time={time}
                      onTime={setTime}
                      onResolution={api.setResolution}
                      handleRef={handle}
                    />
                  ) : null}
                  <TimelineView layout={layout} time={time} notes={notes} zoomCommand={zoom} onSeek={seek} onOpenShot={onOpenShot} />
                </div>
                <div className="sb-film-side">
                  <div className="sb-film-pane" data-pane="subtitles" id="sb-film-pane-subtitles" role="tabpanel" aria-labelledby="sb-film-tab-subtitles">
                    <SubtitlesPanel subtitles={api.subtitles} current={cueAt(cues, time)} onSeek={seek} />
                  </div>
                  <div className="sb-film-pane" data-pane="music" id="sb-film-pane-music" role="tabpanel" aria-labelledby="sb-film-tab-music">
                    <MusicPanel api={api} timeline={timeline} />
                  </div>
                  <div className="sb-film-pane" data-pane="file" id="sb-film-pane-file" role="tabpanel" aria-labelledby="sb-film-tab-file">
                    <FinalFileInfo timeline={timeline} source={api.source} resolution={api.resolution} onOpenReport={onOpenReport} />
                  </div>
                  <div className="sb-film-pane" data-pane="versions" id="sb-film-pane-versions" role="tabpanel" aria-labelledby="sb-film-tab-versions">
                    <FilmVersions versions={api.versions} media={api.media} />
                  </div>
                </div>
              </div>
            </>
          )}
        </>
      ) : null}
    </div>
  );
}
