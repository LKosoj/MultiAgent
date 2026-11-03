"use client";

import React from "react";

import type { HistoryState, DiffState } from "../../../../lib/storybook/artifactEditor";
import { FILES_TEXTS } from "../../../../lib/storybook/filesTexts";
import { diffRows, versionRows } from "../../../../lib/storybook/filesView";

type Props = {
  history: HistoryState;
  diff: DiffState | null;
  canRestore: boolean;
  onClose: () => void;
  onView: (versionId: string) => void;
  onCompare: (versionId: string) => void;
  onRestore: (versionId: string, date: string) => void;
};

// История: только список версий (просмотр, сравнение, откат)
export function HistoryPanel({ history, diff, canRestore, onClose, onView, onCompare, onRestore }: Props) {
  const rows = versionRows(history.items);
  return (
    <section className="sb-files-history" aria-label={FILES_TEXTS.history_title}>
      <div className="sb-row">
        <h3 className="sb-h3">{FILES_TEXTS.history_title}</h3>
        <button type="button" className="button ghost" onClick={onClose}>
          {FILES_TEXTS.close}
        </button>
      </div>
      {history.phase === "loading" ? <p className="sb-muted" role="status">{FILES_TEXTS.loading}</p> : null}
      {history.phase === "error" ? (
        <p role="alert" className="sb-tone-danger">
          {FILES_TEXTS.history_error.replace("{reason}", (history.error ?? "").replace(/\.$/, ""))}
        </p>
      ) : null}
      {history.phase === "ready" && rows.length === 0 ? <p className="sb-muted">{FILES_TEXTS.history_empty}</p> : null}
      {rows.length > 0 ? (
        <table className="sb-files-versions">
          <thead>
            <tr>
              <th scope="col">{FILES_TEXTS.col_date}</th>
              <th scope="col">{FILES_TEXTS.col_size}</th>
              <th scope="col">{FILES_TEXTS.col_reason}</th>
              <th scope="col"><span className="sb-visually-hidden">{FILES_TEXTS.history_title}</span></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id} data-selected={diff?.versionId === r.id ? "true" : undefined}>
                <td>{r.date}</td>
                <td>{r.size}</td>
                <td>{r.reason}</td>
                <td className="sb-files-version-actions">
                  <button type="button" className="button ghost" onClick={() => onView(r.id)}>{FILES_TEXTS.view_version}</button>
                  <button type="button" className="button ghost" onClick={() => onCompare(r.id)}>{FILES_TEXTS.compare}</button>
                  {canRestore ? (
                    <button type="button" className="button ghost" onClick={() => onRestore(r.id, r.date)}>{FILES_TEXTS.restore}</button>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
      {diff ? (
        <div className="sb-files-diff">
          <h4 className="sb-h3">{FILES_TEXTS.diff_title}</h4>
          {diff.phase === "loading" ? <p className="sb-muted" role="status">{FILES_TEXTS.loading}</p> : null}
          {diff.phase === "error" ? (
            <p role="alert" className="sb-tone-danger">
              {FILES_TEXTS.diff_error.replace("{reason}", (diff.error ?? "").replace(/\.$/, ""))}
            </p>
          ) : null}
          {diff.result?.identical ? <p className="sb-muted">{FILES_TEXTS.diff_same}</p> : null}
          {diff.result && !diff.result.identical ? (
            <ul>
              {diffRows(diff.result.changes).map((r, i) => (
                <li key={i}>
                  <strong>{r.where}</strong>: {r.before} → {r.after}
                </li>
              ))}
            </ul>
          ) : null}
          {diff.result?.truncated ? <p className="sb-muted">{FILES_TEXTS.diff_truncated}</p> : null}
        </div>
      ) : null}
    </section>
  );
}
