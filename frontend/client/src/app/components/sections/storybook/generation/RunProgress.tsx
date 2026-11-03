import React from "react";

import { formatDuration } from "../../../../lib/storybook/format";
import { gfmt, GEN_TEXTS } from "../../../../lib/storybook/generationTexts";
import { isBusyState, stepLine } from "../../../../lib/storybook/generationModel";
import type { RunStatusResult } from "../../../../lib/storybook/runs";
import { formatShotMoney } from "../../../../lib/storybook/shotsView";

type Props = {
  status: RunStatusResult | null;
  titleOf: (id: string) => string;
  isAdmin: boolean;
  busy: boolean;
  onPause: () => void;
  onStop: () => void;
};

// Блок «Прогон»: строка шага, полоса прогресса, видео, траты, оставшееся время, Пауза/Стоп
export function RunProgress({ status, titleOf, isAdmin, busy, onPause, onStop }: Props) {
  if (!status || !isBusyState(status.state)) {
    return (
      <section className="sb-gen-card" aria-label={GEN_TEXTS.section_run}>
        <h3 className="sb-h3">{GEN_TEXTS.section_run}</h3>
        <p className="sb-muted">{GEN_TEXTS.progress_idle}</p>
        {status && status.cost.spent_rub > 0 ? <p className="sb-muted">{gfmt("spent", { sum: formatShotMoney(status.cost.spent_rub) })}</p> : null}
      </section>
    );
  }
  const percent = Math.max(0, Math.min(100, Math.round(status.progress?.percent ?? 0)));
  const paused = status.state === "paused" || status.state === "pause_requested";
  const { video, cost, eta } = status;
  const off = busy || status.state === "stopping";
  return (
    <section className="sb-gen-card" aria-label={GEN_TEXTS.section_run}>
      <h3 className="sb-h3">{GEN_TEXTS.section_run}</h3>
      <div>{stepLine(status, titleOf)}</div>
      <div className="progress-bar" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent} aria-label={GEN_TEXTS.section_run}>
        <div className="progress-fill" style={{ width: `${percent}%` }} />
      </div>
      <div className="sb-muted">{percent}%</div>
      <div className="sb-gen-facts">
        {video.total > 0 ? (
          <span>
            {gfmt("video_progress", { done: video.done, total: video.total })}
            {video.failed > 0 ? `, ${gfmt("video_failed", { n: video.failed })}` : ""}
          </span>
        ) : null}
        <span>
          {cost.budget_rub !== null
            ? gfmt("spent_of", { sum: formatShotMoney(cost.spent_rub), budget: formatShotMoney(cost.budget_rub) })
            : gfmt("spent", { sum: formatShotMoney(cost.spent_rub) })}
        </span>
        <span>{eta && eta.seconds !== null ? gfmt("eta", { time: formatDuration(eta.seconds) }) : GEN_TEXTS.eta_none}</span>
      </div>
      {isAdmin ? (
        <div className="button-row">
          <button type="button" className="button secondary" aria-disabled={off} onClick={() => !off && onPause()} title="Alt+P">
            {paused ? GEN_TEXTS.resume : GEN_TEXTS.pause}
          </button>
          <button type="button" className="button secondary" aria-disabled={off} onClick={() => !off && onStop()} title="Ctrl+.">
            {GEN_TEXTS.stop}
          </button>
        </div>
      ) : null}
    </section>
  );
}
