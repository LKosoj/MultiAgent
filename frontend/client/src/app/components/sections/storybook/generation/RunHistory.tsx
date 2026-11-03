import React from "react";

import { GEN_TEXTS } from "../../../../lib/storybook/generationTexts";
import type { HistoryViewRow } from "../../../../lib/storybook/historyView";

export function RunHistory({ rows, onOpenLog }: { rows: HistoryViewRow[]; onOpenLog: (runId: string) => void }) {
  return (
    <details className="sb-gen-card">
      <summary>{GEN_TEXTS.section_history}</summary>
      {rows.length === 0 ? (
        <p className="sb-muted">{GEN_TEXTS.history_empty}</p>
      ) : (
        <table className="sb-table">
          <thead>
            <tr>
              <th>{GEN_TEXTS.history_col_start}</th>
              <th>{GEN_TEXTS.history_col_mode}</th>
              <th>{GEN_TEXTS.history_col_where}</th>
              <th>{GEN_TEXTS.history_col_duration}</th>
              <th>{GEN_TEXTS.history_col_result}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.runId}>
                <td>{r.started}</td>
                <td>{r.mode}</td>
                <td>{r.where}</td>
                <td>{r.duration}</td>
                <td>
                  {r.result}
                  {r.note ? <span className="sb-muted"> · {r.note}</span> : null}
                </td>
                <td>
                  <button type="button" className="button ghost" onClick={() => onOpenLog(r.runId)}>
                    {GEN_TEXTS.history_open_log}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </details>
  );
}
