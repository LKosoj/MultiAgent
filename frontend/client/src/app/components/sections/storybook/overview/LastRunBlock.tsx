import React from "react";

import type { LastRunView } from "../../../../lib/storybook/overviewView";
import { T, lastRunLabels } from "../../../../lib/storybook/texts";

type Props = { view: LastRunView; incomplete: number };

export function LastRunBlock({ view, incomplete }: Props) {
  return (
    <section aria-label={T.lastRun}>
      <h3 className="sb-h3">{T.lastRun}</h3>
      {view ? (
        <dl className="sb-kv">
          {view.rows.map((r) => (
            <React.Fragment key={r.label}>
              <dt>{r.label}</dt>
              <dd>{r.value}</dd>
            </React.Fragment>
          ))}
          {view.error ? (
            <>
              <dt>{lastRunLabels.error}</dt>
              <dd className="sb-tone-danger">{view.error}</dd>
            </>
          ) : null}
        </dl>
      ) : (
        <p className="sb-muted">{T.noRuns}</p>
      )}
      {incomplete > 0 ? <p className="sb-tone-warn">{lastRunLabels.incomplete(incomplete)}</p> : null}
    </section>
  );
}
