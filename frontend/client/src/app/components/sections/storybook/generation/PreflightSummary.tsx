import React from "react";

import { budgetLine, costRows, estimateHeadline } from "../../../../lib/storybook/costView";
import { FINDING_LEVEL_WORDS, gfmt, GEN_TEXTS } from "../../../../lib/storybook/generationTexts";
import type { Note } from "../../../../lib/storybook/generationModel";
import type { RunPlan } from "../../../../lib/storybook/runs";
import type { CostEstimateResult } from "../../../../lib/storybook/runSettings";

type Props = {
  plan: RunPlan | null;
  estimate: CostEstimateResult | null;
  estimateError: string | null;
  dirty: boolean;
  keyLine: { found: boolean; name: string } | null;
  notes: Note[];
  titleOf: (id: string) => string;
  loading: boolean;
};

// Сводка перед запуском: шаги, оценка с разбивкой, бюджет, ключ провайдера и замечания
export function PreflightSummary({ plan, estimate, estimateError, dirty, keyLine, notes, titleOf, loading }: Props) {
  return (
    <section className="sb-gen-card" aria-label={GEN_TEXTS.section_summary} aria-busy={loading}>
      <h3 className="sb-h3">{GEN_TEXTS.section_summary}</h3>
      {plan && plan.steps_to_run.length > 0 ? <p>{gfmt("steps_to_run", { steps: plan.steps_to_run.map(titleOf).join(", ") })}</p> : null}
      {plan && plan.skip_steps.length > 0 ? <p className="sb-muted">{gfmt("steps_skipped", { steps: plan.skip_steps.map(titleOf).join(", ") })}</p> : null}
      {estimate ? (
        <div>
          <div className="sb-gen-estimate">
            {estimateHeadline(estimate)}
            {dirty ? <span className="sb-muted"> · {GEN_TEXTS.summary_dirty}</span> : null}
          </div>
          <table className="sb-table">
            <tbody>
              {costRows(estimate).map((r) => (
                <tr key={r.id}>
                  <td>{r.title}</td>
                  <td>
                    {r.amount}
                    {r.rough ? <span className="sb-muted"> · {GEN_TEXTS.estimate_rough}</span> : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className={`sb-tone-${budgetLine(estimate).tone === "muted" ? "info" : budgetLine(estimate).tone}`}>{budgetLine(estimate).text}</div>
        </div>
      ) : estimateError ? (
        <p role="alert" className="sb-tone-warn">
          {gfmt("estimate_failed", { reason: estimateError })}
        </p>
      ) : (
        <p className="sb-muted">{GEN_TEXTS.loading}</p>
      )}
      {keyLine ? (
        <div className={keyLine.found ? "sb-tone-ok" : "sb-tone-danger"}>
          {keyLine.found ? GEN_TEXTS.key_found : gfmt("key_missing", { name: keyLine.name })}
        </div>
      ) : null}
      {notes.length > 0 ? (
        <ul className="sb-gen-notes">
          {notes.map((n, i) => (
            <li key={i} data-level={n.level}>
              <strong>{FINDING_LEVEL_WORDS[n.level]}:</strong> {n.text}
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}
