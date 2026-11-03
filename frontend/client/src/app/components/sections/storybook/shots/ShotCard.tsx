"use client";

import React from "react";

import type { ReviewStatus } from "../../../../lib/storybook/review";
import { SHOT_TEXTS, fmt, shotCardTitle } from "../../../../lib/storybook/shotsTexts";
import { partTitle, staleView } from "../../../../lib/storybook/shotsView";
import type { ShotCardApi } from "../../../../lib/storybook/useShotCard";
import type { ShotMediaApi } from "../../../../lib/storybook/useShotMedia";
import type { ShotReviewApi } from "../../../../lib/storybook/useShotReview";
import type { ShotSecondaryApi } from "../../../../lib/storybook/useShotSecondary";
import type { VersionsApi } from "../../../../lib/storybook/useVersions";
import { Banner } from "../Banner";
import { Skeleton } from "../Skeleton";
import { ShotMediaRow } from "./ShotMediaRow";
import { ShotPromptsForm } from "./ShotPromptsForm";
import { ShotReportsTable } from "./ShotReportsTable";
import { ShotVersions } from "./ShotVersions";

type Props = {
  api: ShotCardApi;
  review: ShotReviewApi;
  secondary: ShotSecondaryApi;
  versions: VersionsApi;
  media: ShotMediaApi;
  // причина, по которой правки и перегенерация закрыты (идёт прогон, шот в работе); null - можно
  lockedText: string | null;
  onSetStatus: (status: ReviewStatus) => void;
  onRegenerate: () => void;
  onView: (rel: string) => void;
  // править ИИ начало или конец шота
  onEditImage: (part: "start" | "end") => void;
  // время изменения файлов, правленых ИИ (по пути)
  edited: Readonly<Record<string, number>>;
  onRetry: () => void;
};

const BUTTONS: { status: ReviewStatus; label: string }[] = [
  { status: "approved", label: SHOT_TEXTS.approve },
  { status: "redo", label: SHOT_TEXTS.redo },
  { status: "rejected", label: SHOT_TEXTS.reject },
];

// Правая часть экрана: открытый шот целиком
export function ShotCard({ api, review, secondary, versions, media, lockedText, onSetStatus, onRegenerate, onView, onEditImage, edited, onRetry }: Props) {
  const { card } = api;
  if (api.status === "error" && !card) {
    return (
      <div className="sb-shots-card">
        <Banner
          tone="error"
          actions={
            <button type="button" className="button secondary" onClick={onRetry}>
              {SHOT_TEXTS.retry}
            </button>
          }
        >
          {fmt("card_error", { reason: api.error ?? "" })}
        </Banner>
      </div>
    );
  }
  if (!card) {
    return (
      <div className="sb-shots-card">
        <Skeleton lines={6} label={SHOT_TEXTS.loading} />
      </div>
    );
  }
  const current = api.entry?.status ?? "pending";
  const stale = staleView(card);
  return (
    <div className="sb-shots-card" data-loading={api.status === "loading" ? "true" : "false"}>
      <div className="sb-shots-card-head">
        <h2 className="sb-title">{shotCardTitle(card.shot_key, card.scene, card.video.duration_s)}</h2>
        <span className="sb-muted">{card.state_title}</span>
        {api.entry ? <span className="sb-shots-status" data-review={current}>{api.entry.status_title}</span> : null}
      </div>
      {stale ? (
        <section className="sb-shots-stale">
          <h3 className="sb-title">{SHOT_TEXTS.stale_title}</h3>
          {stale.parts.map((p) => (
            <p key={p.part}>
              <strong>{partTitle(p.part)}</strong>
              {p.reasons.length > 0 ? `: ${p.reasons.join(", ")}` : ""}
            </p>
          ))}
          {stale.after.length > 0 ? <p className="sb-muted">{fmt("stale_after", { parts: stale.after.map(partTitle).join(", ") })}</p> : null}
          {stale.upstream.length > 0 ? <p className="sb-muted">{fmt("upstream_stale", { keys: stale.upstream.join(", ") })}</p> : null}
        </section>
      ) : null}
      <div className="button-row" role="group">
        {BUTTONS.map((b) => (
          <button
            key={b.status}
            type="button"
            className="button secondary"
            aria-pressed={current === b.status}
            disabled={review.busy}
            onClick={() => onSetStatus(b.status)}
          >
            {b.label}
          </button>
        ))}
        <button type="button" className="button" disabled={lockedText !== null} onClick={onRegenerate}>
          {SHOT_TEXTS.regen_button}
        </button>
      </div>
      {lockedText ? <p className="sb-muted">{lockedText}</p> : null}
      <ShotMediaRow card={card} media={media} onView={onView} onEdit={onEditImage} editBlocked={lockedText} edited={edited} />
      <ShotPromptsForm card={card} api={api} lockedText={lockedText} />
      <ShotReportsTable api={secondary} />
      <ShotVersions api={versions} media={media} disabled={lockedText !== null} disabledHint={lockedText} />
    </div>
  );
}
