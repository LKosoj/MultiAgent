"use client";

import React from "react";

import { BLOCKOUT_TEXTS, blockoutFmt } from "../../../../lib/storybook/blockoutTexts";
import { detailKey, summaryLine, toolsLine } from "../../../../lib/storybook/blockoutView";
import type { BlockoutApi } from "../../../../lib/storybook/useBlockout";
import { Banner } from "../Banner";
import { Skeleton } from "../Skeleton";
import { BlockoutActions } from "./BlockoutActions";
import { BlockoutCompare } from "./BlockoutCompare";
import { BlockoutPlanDialog } from "./BlockoutPlanDialog";
import { BlockoutTree } from "./BlockoutTree";

type Props = {
  api: BlockoutApi;
  onGoGeneration: () => void;
  onOpenShot: (key: string) => void;
};

export function BlockoutTab({ api, onGoGeneration, onOpenShot }: Props) {
  const { screen, state } = api;
  const key = detailKey(api.selection);

  const onKeyDown = (ev: React.KeyboardEvent) => {
    if (ev.key === "Enter" && key && (ev.target as HTMLElement).getAttribute?.("role") === "option") {
      ev.preventDefault();
      onOpenShot(key);
    }
  };

  return (
    <div className="sb-screen sb-blockout" data-state={screen} onKeyDown={onKeyDown}>
      <header className="sb-blockout-head">
        <h2 className="sb-title">{BLOCKOUT_TEXTS.title}</h2>
      </header>
      {screen === "loading" ? <Skeleton lines={4} label={BLOCKOUT_TEXTS.loading} /> : null}
      {screen === "error" ? (
        <Banner
          tone="error"
          actions={
            <button type="button" className="button" onClick={api.reload}>
              {BLOCKOUT_TEXTS.retry}
            </button>
          }
        >
          {blockoutFmt("load_error", { reason: (api.error ?? "").replace(/[.]$/, "") })}
        </Banner>
      ) : null}
      {screen === "not_built" ? (
        <div className="sb-empty" data-empty="not_built">
          <p>{BLOCKOUT_TEXTS.not_built}</p>
          <button type="button" className="button" onClick={onGoGeneration}>
            {BLOCKOUT_TEXTS.goto_generation}
          </button>
        </div>
      ) : null}
      {screen === "no_shots" ? (
        <div className="sb-empty" data-empty="no_shots">
          <p>{BLOCKOUT_TEXTS.no_shots}</p>
        </div>
      ) : null}
      {screen === "ready" && state ? (
        <>
          {!state.tools.blender.available ? <Banner tone="warn">{BLOCKOUT_TEXTS.no_blender}</Banner> : null}
          <p className="sb-muted sb-blockout-summary">{summaryLine(state.summary)}</p>
          <p className="sb-muted sb-blockout-tools">{toolsLine(state.tools)}</p>
          <BlockoutActions
            buttons={api.buttons}
            busy={api.busy}
            canOpenShot={key !== null}
            onAction={(a) => void api.runAction(a)}
            onOpenShot={() => key && onOpenShot(key)}
          />
          <div className="sb-blockout-body">
            <BlockoutTree state={state} selection={api.selection} onSelect={api.select} />
            <BlockoutCompare
              detail={api.detail}
              detailError={api.detailError}
              hasSelection={key !== null}
              images={api.images}
              fileUrl={api.videoUrl}
              durationChoice={api.durationChoice}
              onDurationChoice={api.setDurationChoice}
              onApplyDuration={() => void api.applyDuration()}
              durationBusy={api.busy}
              rebuildHint={api.rebuildHint}
              onImageError={api.media.onImageError}
            />
          </div>
          <section className="sb-blockout-preview" aria-label={BLOCKOUT_TEXTS.preview_title}>
            <h3 className="sb-h3">{BLOCKOUT_TEXTS.preview_title}</h3>
            {api.previewUrl ? (
              <video controls preload="none" src={api.previewUrl} />
            ) : (
              <p className="sb-muted">{BLOCKOUT_TEXTS.preview_none}</p>
            )}
          </section>
          {state.warnings.length > 0 ? (
            <details className="sb-blockout-warnings">
              <summary>{BLOCKOUT_TEXTS.warnings_title}</summary>
              <ul>
                {state.warnings.map((w, i) => (
                  <li key={i}>{w.message}</li>
                ))}
              </ul>
            </details>
          ) : null}
        </>
      ) : null}
      {api.dialog ? <BlockoutPlanDialog dialog={api.dialog} onConfirm={() => void api.confirmDialog()} onClose={api.closeDialog} /> : null}
    </div>
  );
}
