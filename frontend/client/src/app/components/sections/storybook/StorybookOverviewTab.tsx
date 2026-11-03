"use client";

import React, { useRef } from "react";

import type { NextActionTarget } from "../../../lib/storybook/status";
import {
  counterTiles,
  finalVideoView,
  isEmptyProject,
  lastRunView,
  nextActionView,
  runTone,
  stageStrip,
  stepGroups,
} from "../../../lib/storybook/overviewView";
import { isActiveState } from "../../../lib/storybook/runs";
import { T, loadStatusFailed } from "../../../lib/storybook/texts";
import type { ProjectStatusApi } from "../../../lib/storybook/useProjectStatus";
import { Banner } from "./Banner";
import { NoAccess } from "./NoAccess";
import { Skeleton } from "./Skeleton";
import { CounterTiles } from "./overview/CounterTiles";
import { FinalVideoBlock } from "./overview/FinalVideoBlock";
import { LastRunBlock } from "./overview/LastRunBlock";
import { NextActionBlock } from "./overview/NextActionBlock";
import { StageStrip } from "./overview/StageStrip";
import { StepsList } from "./overview/StepsList";
import { WarningsBlock } from "./overview/WarningsBlock";

type Props = {
  api: ProjectStatusApi;
  onTarget: (target: Pick<NextActionTarget, "screen"> & Partial<NextActionTarget>) => void;
  onWatchFinal: () => void;
  onGoGeneration: () => void;
  onBackToProjects: () => void;
  onRefresh?: () => void;
};

export function StorybookOverviewTab({ api, onTarget, onWatchFinal, onGoGeneration, onBackToProjects, onRefresh }: Props) {
  const warningsRef = useRef<HTMLElement>(null);
  const { status, state, error, offline } = api;
  const refresh = onRefresh ?? api.refresh;

  if (state === "error" && error) {
    if (error.kind === "forbidden") return <NoAccess />;
    return (
      <div className="sb-screen">
        <h2 className="sb-title">{T.overviewTitle}</h2>
        {error.kind === "not_found" ? (
          <Banner
            tone="error"
            actions={
              <button type="button" className="button secondary" onClick={onBackToProjects}>
                {T.toProjects}
              </button>
            }
          >
            {T.projectGone}
          </Banner>
        ) : (
          <Banner
            tone="error"
            actions={
              <button type="button" className="button secondary" onClick={refresh}>
                {T.retry}
              </button>
            }
          >
            {loadStatusFailed(error.text)}
          </Banner>
        )}
      </div>
    );
  }
  if (!status) {
    return (
      <div className="sb-screen">
        <h2 className="sb-title">{T.overviewTitle}</h2>
        <Skeleton label={T.loadingStatus} lines={5} height={24} />
      </div>
    );
  }

  const run = status.active_run;
  const tone = runTone(run.state);
  const empty = isEmptyProject(status);

  return (
    <div className="sb-screen">
      <div className="sb-row" style={{ justifyContent: "space-between" }}>
        <h2 className="sb-title">{T.overviewTitle}</h2>
        <button type="button" className="button ghost" onClick={refresh} title="F5">
          {T.refresh}
        </button>
      </div>
      {offline && error ? <Banner tone="warn">{loadStatusFailed(error.text)}</Banner> : null}
      <p className={`sb-tone-${tone}`} data-run-state={run.state}>
        {run.text}
      </p>
      <NextActionBlock view={nextActionView(status)} onGo={(target) => onTarget(target)} />
      {empty ? (
        <div className="sb-empty">
          <p>{T.emptyOverview}</p>
          <button type="button" className="button" onClick={onGoGeneration}>
            {T.goGeneration}
          </button>
        </div>
      ) : null}
      <StageStrip cells={stageStrip(status)} />
      <CounterTiles
        tiles={counterTiles(status)}
        onTarget={(t) => onTarget({ screen: t.screen as NextActionTarget["screen"], filter: t.filter as NextActionTarget["filter"] })}
        onWarnings={() => warningsRef.current?.focus()}
      />
      <FinalVideoBlock view={finalVideoView(status)} onWatch={onWatchFinal} />
      <StepsList
        groups={stepGroups(status)}
        busy={isActiveState(status.active_run.state)}
        onRerun={(stepId, failed) => onTarget({ screen: "generation", step_id: stepId, mode: failed ? "resume" : "single_step" })}
      />
      <WarningsBlock count={status.warnings.count} blockRef={warningsRef} />
      <LastRunBlock view={lastRunView(status)} incomplete={status.incomplete_runs.length} />
    </div>
  );
}
