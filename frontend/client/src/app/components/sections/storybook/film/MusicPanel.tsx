import React from "react";

import { FILM_TEXTS } from "../../../../lib/storybook/filmTexts";
import { musicRows } from "../../../../lib/storybook/filmView";
import type { TimelineResult } from "../../../../lib/storybook/media";
import type { FilmApi } from "../../../../lib/storybook/useFilm";
import { Banner } from "../Banner";
import { StudioModal } from "../StudioModal";

type Props = { api: FilmApi; timeline: TimelineResult };

function MusicDialog({ api }: { api: FilmApi }) {
  const { music } = api;
  const view = music.view;
  const busy = music.phase === "previewing" || music.phase === "starting";
  return (
    <StudioModal title={FILM_TEXTS.music_title} onClose={music.close} closable={music.phase !== "starting"}>
      {music.phase === "previewing" && !view ? <p className="sb-muted">{FILM_TEXTS.music_loading}</p> : null}
      {music.error ? <Banner tone="error">{music.error}</Banner> : null}
      {view ? (
        <div className="sb-film-music-plan">
          <p>{view.body}</p>
          {view.problems.map((p, i) => (
            <Banner key={i} tone={p.level === "error" ? "error" : p.level === "warning" ? "warn" : "info"}>
              {p.message}
            </Banner>
          ))}
          {!view.canStart && view.problems.length === 0 ? <Banner tone="error">{FILM_TEXTS.music_blocked}</Banner> : null}
        </div>
      ) : null}
      <div className="button-row">
        <button type="button" className="button ghost" data-autofocus="true" disabled={music.phase === "starting"} onClick={music.close}>
          {FILM_TEXTS.music_cancel}
        </button>
        <button type="button" className="button" disabled={busy || !view || !view.canStart} onClick={() => void music.start()}>
          {view ? view.goLabel : FILM_TEXTS.music_go_free}
        </button>
      </div>
    </StudioModal>
  );
}

export function MusicPanel({ api, timeline }: Props) {
  const track = timeline.tracks.music;
  const rows = musicRows(track.clips);
  const blocked = api.actionBlock;
  return (
    <section className="sb-film-panel" aria-label={FILM_TEXTS.track_music}>
      <h3 className="sb-h3">{FILM_TEXTS.track_music}</h3>
      {rows.length === 0 ? (
        <p className="sb-muted">{track.note || FILM_TEXTS.music_none}</p>
      ) : (
        <table className="sb-film-table">
          <thead>
            <tr>
              <th scope="col">{FILM_TEXTS.col_name}</th>
              <th scope="col">{FILM_TEXTS.col_scenes}</th>
              <th scope="col">{FILM_TEXTS.col_source}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const url = r.exists ? api.media.media(r.path) : null;
              return (
                <tr key={r.id}>
                  <td>
                    <div>{r.name}</div>
                    {url ? <audio controls preload="none" src={url} aria-label={r.name} /> : <span className="sb-muted">{FILM_TEXTS.no_file}</span>}
                  </td>
                  <td>{r.scenes}</td>
                  <td>{r.source}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      <button
        type="button"
        className="button"
        aria-disabled={blocked ? "true" : undefined}
        title={blocked ?? undefined}
        onClick={() => (blocked ? undefined : api.music.begin())}
      >
        {FILM_TEXTS.regen_music}
      </button>
      {blocked ? <p className="sb-muted">{blocked}</p> : null}
      {api.music.open ? <MusicDialog api={api} /> : null}
    </section>
  );
}
