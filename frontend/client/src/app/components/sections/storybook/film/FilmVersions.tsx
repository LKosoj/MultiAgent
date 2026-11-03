import React from "react";

import { FILM_TEXTS } from "../../../../lib/storybook/filmTexts";
import type { AreaVersion } from "../../../../lib/storybook/filmRegen";
import { formatBytes, formatCreatedAt } from "../../../../lib/storybook/shotsView";
import type { VersionsState } from "../../../../lib/storybook/useFilm";
import type { ShotMediaApi } from "../../../../lib/storybook/useShotMedia";

type Props = { versions: VersionsState; media: ShotMediaApi };

function List({ title, items, media }: { title: string; items: AreaVersion[] | null; media: ShotMediaApi }) {
  return (
    <div className="sb-film-versions-group">
      <h4 className="sb-h3">{title}</h4>
      {items === null ? null : items.length === 0 ? (
        <p className="sb-muted">{FILM_TEXTS.versions_none}</p>
      ) : (
        <ul className="sb-film-versions">
          {items.map((v) => (
            <li key={v.id}>
              <div>
                {v.number}. {formatCreatedAt(v.created_at)}
                {v.reason_title ? ` · ${v.reason_title}` : ""}
              </div>
              <ul>
                {v.files.map((f) => {
                  const url = media.media(f.path);
                  return (
                    <li key={f.path}>
                      {f.name}
                      {f.size !== null ? ` (${formatBytes(f.size)})` : ""}{" "}
                      {url ? (
                        <a href={url} target="_blank" rel="noreferrer">
                          {FILM_TEXTS.versions_open}
                        </a>
                      ) : null}
                    </li>
                  );
                })}
              </ul>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// Только просмотр списка: вернуть версию музыки или фильма на сервере пока нельзя
export function FilmVersions({ versions, media }: Props) {
  return (
    <section className="sb-film-panel" aria-label={FILM_TEXTS.versions_title}>
      <h3 className="sb-h3">{FILM_TEXTS.versions_title}</h3>
      {versions.error ? <p className="sb-muted">{versions.error}</p> : null}
      <List title={FILM_TEXTS.versions_music} items={versions.music} media={media} />
      <List title={FILM_TEXTS.versions_film} items={versions.film} media={media} />
    </section>
  );
}
