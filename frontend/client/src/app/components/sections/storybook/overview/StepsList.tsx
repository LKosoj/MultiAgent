import React from "react";

import type { StepGroup } from "../../../../lib/storybook/overviewView";
import { BUSY_HINT, RERUN_HINT, T } from "../../../../lib/storybook/texts";

const tag = (state: string): string =>
  state === "done" ? "completed" : state === "failed" ? "failed" : state === "running" ? "running" : "pending";

type Props = {
  groups: StepGroup[];
  // прогон идёт: повторный запуск шага недоступен
  busy?: boolean;
  onRerun?: (stepId: string, failed: boolean) => void;
};

export function StepsList({ groups, busy = false, onRerun }: Props) {
  return (
    <section aria-label={T.steps}>
      <h3 className="sb-h3">{T.steps}</h3>
      {groups.map((g) => (
        <div key={g.stageId ?? "other"} style={{ marginBottom: 12 }}>
          <div className="sb-muted">{g.title}</div>
          <ul className="sb-steps">
            {g.rows.map((r) => (
              <li key={r.id} className="sb-step" data-step-id={r.id}>
                <span>
                  {r.order}. {r.title}
                  {r.note ? <span className="sb-muted"> · {r.note}</span> : null}
                  {r.lowConfidence ? <span className="sb-muted"> · {T.lowConfidence}</span> : null}
                </span>
                <span className="status-tag" data-status={tag(r.displayState)}>
                  {r.stateTitle}
                  {r.duration ? ` · ${r.duration}` : ""}
                </span>
                <button
                  type="button"
                  className={busy || !onRerun ? "button ghost sb-button-disabled" : "button ghost"}
                  style={{ minWidth: 0, height: 28, fontSize: 12 }}
                  aria-disabled={busy || !onRerun ? "true" : undefined}
                  title={busy ? BUSY_HINT : RERUN_HINT}
                  onClick={() => !busy && onRerun?.(r.id, r.displayState === "failed")}
                >
                  {T.rerun}
                </button>
                {r.error ? <div className="sb-step-error">{r.error}</div> : null}
              </li>
            ))}
          </ul>
        </div>
      ))}
    </section>
  );
}
