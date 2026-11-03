import React from "react";

import type { FinalVideoView } from "../../../../lib/storybook/overviewView";
import { T } from "../../../../lib/storybook/texts";

type Props = { view: FinalVideoView; onWatch: () => void };

export function FinalVideoBlock({ view, onWatch }: Props) {
  return (
    <section aria-label={T.finalVideo}>
      <h3 className="sb-h3">{T.finalVideo}</h3>
      {view.exists ? (
        <div className="sb-row">
          <span>{view.meta || view.path}</span>
          <button type="button" className="button secondary" onClick={onWatch}>
            {T.watch}
          </button>
          {view.olderThanShots ? <span className="sb-tone-warn">{T.finalOlder}</span> : null}
          {view.review ? <span className="sb-tone-warn">{view.review}</span> : null}
        </div>
      ) : (
        <p className="sb-muted">{T.noFinal}</p>
      )}
    </section>
  );
}
