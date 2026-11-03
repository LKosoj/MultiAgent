import React from "react";

import { FILM_TEXTS } from "../../../../lib/storybook/filmTexts";
import { subtitlesTruncatedText } from "../../../../lib/storybook/filmView";
import type { SubtitleCue, SubtitlesResult } from "../../../../lib/storybook/media";
import { formatTimecode } from "../../../../lib/storybook/media";

type Props = {
  subtitles: SubtitlesResult | null;
  current: SubtitleCue | null;
  onSeek: (t: number) => void;
};

export function SubtitlesPanel({ subtitles, current, onSeek }: Props) {
  const cues = subtitles?.cues ?? [];
  const truncated = subtitlesTruncatedText(!!subtitles?.truncated);
  return (
    <section className="sb-film-panel" aria-label={FILM_TEXTS.track_subtitles}>
      <h3 className="sb-h3">{FILM_TEXTS.track_subtitles}</h3>
      {cues.length === 0 ? (
        <p className="sb-muted">{FILM_TEXTS.subs_none}</p>
      ) : (
        <table className="sb-film-table">
          <thead>
            <tr>
              <th scope="col">{FILM_TEXTS.col_time}</th>
              <th scope="col">{FILM_TEXTS.col_text}</th>
            </tr>
          </thead>
          <tbody>
            {cues.map((c) => (
              <tr key={c.index} aria-current={current?.index === c.index ? "true" : undefined}>
                <td>
                  <button type="button" className="sb-film-link" onClick={() => onSeek(c.start_s)}>
                    {formatTimecode(c.start_s)}
                  </button>
                </td>
                <td>{c.text}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {truncated ? <p className="sb-muted">{truncated}</p> : null}
    </section>
  );
}
