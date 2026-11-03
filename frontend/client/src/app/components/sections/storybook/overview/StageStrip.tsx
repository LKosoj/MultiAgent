import React from "react";

import type { StageCell } from "../../../../lib/storybook/overviewView";
import { T } from "../../../../lib/storybook/texts";

export function StageStrip({ cells }: { cells: StageCell[] }) {
  return (
    <section aria-label={T.stages}>
      <h3 className="sb-h3">{T.stages}</h3>
      <ol className="sb-stages">
        {cells.map((c) => (
          <li
            key={c.id}
            className="sb-stage"
            data-state={c.state}
            data-current={c.current ? "true" : "false"}
            aria-current={c.current ? "step" : undefined}
          >
            <strong>{c.title}</strong>
            <span className="sb-muted">
              {c.stateTitle}
              {c.progress ? ` · ${c.progress}` : ""}
            </span>
          </li>
        ))}
      </ol>
    </section>
  );
}
