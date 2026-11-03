"use client";

import React from "react";

import type { RegenWhat } from "../../../../lib/storybook/review";
import { SHOT_TEXTS, fmt } from "../../../../lib/storybook/shotsTexts";
import { formatBytes, formatShotMoney } from "../../../../lib/storybook/shotsView";
import type { RegenerationApi } from "../../../../lib/storybook/useRegeneration";
import { Banner } from "../Banner";
import { Skeleton } from "../Skeleton";
import { StudioModal } from "../StudioModal";

type Props = { api: RegenerationApi };

const OPTIONS: { value: RegenWhat; label: string }[] = [
  { value: "images", label: SHOT_TEXTS.regen_images },
  { value: "video", label: SHOT_TEXTS.regen_video },
  { value: "both", label: SHOT_TEXTS.regen_both },
];

// Предпросмотр перегенерации: что будет пересоздано и сколько это стоит
export function RegenerateDialog({ api }: Props) {
  const { view, phase } = api;
  const many = api.keys.length > 1;
  const busy = phase === "previewing" || phase === "starting";
  const title = many ? fmt("regen_title_many", { k: api.keys.length }) : SHOT_TEXTS.regen_title;
  const blocked = !view || !view.canStart || (view.needsAbandon && !api.abandon);
  return (
    <StudioModal title={title} onClose={api.close} closable={phase !== "starting"}>
      <fieldset className="sb-shots-what" disabled={busy}>
        <legend>{SHOT_TEXTS.regen_what}</legend>
        {OPTIONS.map((o) => (
          <label key={o.value} className="toggle">
            <input type="radio" name="regen-what" checked={api.what === o.value} onChange={() => api.setWhat(o.value)} />
            <span>{o.label}</span>
          </label>
        ))}
      </fieldset>
      {phase === "previewing" && !view ? (
        <>
          <p className="sb-muted">{SHOT_TEXTS.regen_loading}</p>
          <Skeleton lines={2} label={SHOT_TEXTS.regen_loading} />
        </>
      ) : null}
      {api.error ? <Banner tone="error">{api.error}</Banner> : null}
      {view ? (
        <div className="sb-shots-plan">
          {view.costRub > 0 || !view.unknownCost ? <p>{fmt("regen_cost", { cost: formatShotMoney(view.costRub), k: view.shotCount })}</p> : null}
          {view.linkedKeys.length > 0 ? <p>{fmt("regen_linked", { keys: view.linkedKeys.join(", ") })}</p> : null}
          {view.diskNeeded > 0 ? <p className="sb-muted">{fmt("regen_disk", { size: formatBytes(view.diskNeeded) })}</p> : null}
          {view.pendingKeys.length > 0 ? <p>{fmt("regen_pending_keys", { keys: view.pendingKeys.join(", ") })}</p> : null}
          {view.problems.map((p, i) => (
            <Banner key={i} tone={p.level === "error" ? "error" : p.level === "warning" ? "warn" : "info"}>
              {p.level === "error" ? `${SHOT_TEXTS.regen_error}: ` : p.level === "warning" ? `${SHOT_TEXTS.regen_warning}: ` : ""}
              {p.message}
            </Banner>
          ))}
          {!view.canStart && view.problems.length === 0 ? <Banner tone="error">{SHOT_TEXTS.regen_blocked}</Banner> : null}
          {view.needsAbandon ? (
            <label className="toggle">
              <input type="checkbox" checked={api.abandon} onChange={(e) => api.setAbandon(e.target.checked)} />
              <span>{SHOT_TEXTS.regen_abandon}</span>
            </label>
          ) : null}
          <p className="sb-muted">{SHOT_TEXTS.regen_versions_kept}</p>
          {view.button.note ? <p className="sb-muted">{SHOT_TEXTS[view.button.note]}</p> : null}
        </div>
      ) : null}
      <div className="button-row">
        <button type="button" className="button ghost" data-autofocus="true" disabled={phase === "starting"} onClick={api.close}>
          {SHOT_TEXTS.regen_cancel}
        </button>
        <button type="button" className="button" disabled={busy || blocked} onClick={() => void api.start()}>
          {view ? view.button.label : SHOT_TEXTS.regen_go_free}
        </button>
      </div>
    </StudioModal>
  );
}
