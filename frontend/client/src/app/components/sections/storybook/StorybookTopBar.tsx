"use client";

import React, { useState } from "react";

import { formatRub } from "../../../lib/storybook/format";
import { rerunView, runControlView } from "../../../lib/storybook/runControl";
import type { ProjectStatusBrief } from "../../../lib/storybook/status";
import { T } from "../../../lib/storybook/texts";
import { ConfirmDialog } from "./ConfirmDialog";
import { RunIndicator } from "./RunIndicator";

type Props = {
  projectId: string | null;
  seriesId?: string;
  brief: ProjectStatusBrief | null;
  offline: boolean;
  onOpenPalette: () => void;
  onOpenProjects: () => void;
  onOpenGeneration: () => void;
  // «Запустить заново»: открыть «Генерацию» с нужным режимом
  onRerun: (mode: "resume" | "full") => void;
  onPauseToggle: (action: "pause" | "resume") => Promise<void>;
  // вернуть текст ошибки или null, если остановка запрошена
  onStop: () => Promise<string | null>;
};

// Верхняя полоса: проект, этап, прогон, траты, Пауза/Стоп. Без проекта не показывается.
export function StorybookTopBar({
  projectId,
  seriesId,
  brief,
  offline,
  onOpenPalette,
  onOpenProjects,
  onOpenGeneration,
  onRerun,
  onPauseToggle,
  onStop,
}: Props) {
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [stopError, setStopError] = useState<string | null>(null);
  if (!projectId) return null;
  const title = brief?.project.title || seriesId || projectId;
  const view = runControlView(brief?.run.state);
  // в кратком статусе нет флага resumable: продолжать есть что, если прогон прерван или известен упавший шаг
  const resumable = brief?.run.state === "interrupted" || !!brief?.failed_step;
  const rerun = rerunView(brief?.run.state, resumable);

  const pause = async () => {
    setBusy(true);
    try {
      await onPauseToggle(view.pauseAction);
    } finally {
      setBusy(false);
    }
  };
  const stop = async () => {
    setBusy(true);
    const err = await onStop();
    setBusy(false);
    setStopError(err);
    if (!err) setConfirm(false);
  };

  return (
    <div className="sb-top" role="region" aria-label="Состояние проекта">
      <button type="button" className="button ghost" onClick={onOpenProjects} title={T.linkProjects}>
        {seriesId ? "Серия:" : T.topProject} {title}
      </button>
      <button type="button" className="button ghost" onClick={onOpenPalette} title={T.paletteButton} aria-label={T.paletteButton}>
        ⌕
      </button>
      {brief ? (
        <>
          <span>
            {T.topStage} {brief.stage.title}
          </span>
          <button type="button" className="button ghost" onClick={onOpenGeneration} title={T.linkGeneration}>
            <RunIndicator brief={brief} />
          </button>
          <span>
            {T.topSpend}: {formatRub(brief.spend.total)}
            {brief.spend.budget_rub !== null ? ` ${T.topOf} ${formatRub(brief.spend.budget_rub)}` : ""}
          </span>
        </>
      ) : null}
      {offline ? <span className="sb-top-warn" role="status">{T.topOffline}</span> : null}
      <span className="sb-top-spacer" />
      <button type="button" className="button secondary" disabled={!view.pauseEnabled || busy} onClick={pause}>
        {view.pauseLabel}
      </button>
      <button
        type="button"
        className="button secondary"
        disabled={!view.stopEnabled || busy}
        onClick={() => {
          setStopError(null);
          setConfirm(true);
        }}
      >
        {T.topStop}
      </button>
      <button
        type="button"
        className="button secondary"
        disabled={!rerun.enabled || busy}
        title={rerun.hint}
        onClick={() => rerun.mode && onRerun(rerun.mode)}
      >
        {T.rerun}
      </button>
      {confirm ? (
        <ConfirmDialog
          title={T.stopTitle}
          body={T.stopBody}
          confirmLabel={T.stopConfirm}
          cancelLabel={T.stopCancel}
          busy={busy}
          error={stopError}
          onConfirm={stop}
          onCancel={() => setConfirm(false)}
        />
      ) : null}
    </div>
  );
}
