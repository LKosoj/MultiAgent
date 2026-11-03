import React from "react";

import { FILM_TEXTS } from "../../../../lib/storybook/filmTexts";
import { finalFileRows, sourceVideo, type FilmSource } from "../../../../lib/storybook/filmView";
import type { TimelineResult } from "../../../../lib/storybook/media";

type Props = {
  timeline: TimelineResult;
  source: FilmSource;
  resolution: { width: number; height: number } | null;
  onOpenReport: () => void;
};

export function FinalFileInfo({ timeline, source, resolution, onOpenReport }: Props) {
  const video = sourceVideo(timeline, source);
  const rows = finalFileRows(video, resolution);
  return (
    <section className="sb-film-panel" aria-label={FILM_TEXTS.tab_file}>
      <h3 className="sb-h3">{FILM_TEXTS.tab_file}</h3>
      <dl className="sb-film-facts">
        {rows.map((r) => (
          <div key={r.id} data-fact={r.id}>
            <dt>{r.label}</dt>
            <dd>{r.value}</dd>
          </div>
        ))}
      </dl>
      {timeline.final.render_report_path ? (
        <button type="button" className="button ghost" onClick={onOpenReport}>
          {FILM_TEXTS.report_link}
        </button>
      ) : null}
    </section>
  );
}
