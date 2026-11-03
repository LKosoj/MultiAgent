"use client";

import React from "react";

import type { Finding, FindingCounts } from "../../../../lib/storybook/artifacts";
import { FILES_TEXTS } from "../../../../lib/storybook/filesTexts";
import { countsText, findingRows } from "../../../../lib/storybook/filesView";

type Props = {
  findings: readonly Finding[];
  counts: FindingCounts | null;
  truncated: boolean;
  onPick: (finding: Finding) => void;
};

// Результат проверки: счётчик и список находок; клик ведёт к полю или строке JSON
export function FindingsPanel({ findings, counts, truncated, onPick }: Props) {
  const rows = findingRows(findings);
  return (
    <section className="sb-files-findings" aria-label={FILES_TEXTS.findings_title}>
      <h3 className="sb-h3">{FILES_TEXTS.findings_title}</h3>
      {counts === null ? (
        <p className="sb-muted">{FILES_TEXTS.not_validated}</p>
      ) : (
        <p role="status" className={counts.errors > 0 ? "sb-tone-danger" : undefined}>
          {countsText(counts)}
        </p>
      )}
      {rows.length > 0 ? (
        <ul>
          {rows.map((r, i) => (
            <li key={`${r.pointer}-${i}`}>
              <button type="button" className="sb-files-finding" data-severity={r.severity} onClick={() => onPick(findings[i])}>
                <strong>{r.where}</strong>
                <span>{r.message}</span>
                {r.line ? <span className="sb-muted">{r.line}:{r.column ?? 1}</span> : null}
              </button>
            </li>
          ))}
        </ul>
      ) : null}
      {truncated ? <p className="sb-muted">{FILES_TEXTS.findings_truncated}</p> : null}
    </section>
  );
}
