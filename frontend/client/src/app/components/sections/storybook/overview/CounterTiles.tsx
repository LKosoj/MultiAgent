import React from "react";

import type { CounterTile } from "../../../../lib/storybook/overviewView";
import { T } from "../../../../lib/storybook/texts";

type Props = {
  tiles: CounterTile[];
  onTarget: (target: NonNullable<CounterTile["target"]>) => void;
  onWarnings: () => void;
};

export function CounterTiles({ tiles, onTarget, onWarnings }: Props) {
  return (
    <div className="sb-tiles">
      {tiles.map((t) => {
        const body = (
          <>
            <span className="sb-muted">{t.label}</span>
            <span className="sb-tile-value">{t.known ? t.value : T.noData}</span>
            {t.sub ? <span className="sb-muted">{t.sub}</span> : null}
            {t.partial ? <span className="sb-muted">{T.updating}</span> : null}
            {t.notes.map((n) => (
              <span key={n} className="sb-muted">
                {n}
              </span>
            ))}
          </>
        );
        const target = t.target;
        if (target) {
          return (
            <button key={t.key} type="button" className="sb-tile" onClick={() => onTarget(target)}>
              {body}
            </button>
          );
        }
        if (t.toWarnings) {
          return (
            <button key={t.key} type="button" className="sb-tile" onClick={onWarnings}>
              {body}
            </button>
          );
        }
        return (
          <div key={t.key} className="sb-tile">
            {body}
          </div>
        );
      })}
    </div>
  );
}
