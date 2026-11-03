"use client";

import React, { useRef } from "react";

import { versionLabel, type CompareSide, type ShotPart } from "../../../../lib/storybook/review";
import { SHOT_TEXTS, formatRubExact } from "../../../../lib/storybook/shotsTexts";
import { formatCreatedAt, partTitle, signedBytes } from "../../../../lib/storybook/shotsView";
import type { ShotMediaApi } from "../../../../lib/storybook/useShotMedia";
import type { VersionsApi } from "../../../../lib/storybook/useVersions";
import { StudioModal } from "../StudioModal";

type Props = {
  api: VersionsApi;
  media: ShotMediaApi;
  disabled: boolean;
  // вернуть версию (окно подтверждения открывает родитель)
  onRestore: (versionId: string) => void;
};

const PARTS: ShotPart[] = ["start", "end", "video"];

const sideTitle = (side: CompareSide): string => (side.id === "current" ? SHOT_TEXTS.compare_current : versionLabel(side));

function SideMeta({ side }: { side: CompareSide }) {
  if (!("number" in side)) return null;
  const line = [side.provider, side.model].filter(Boolean).join(" / ");
  return (
    <div className="sb-muted">
      {[formatCreatedAt(side.created_at), side.reason_title, line || null, side.cost_rub !== null ? formatRubExact(side.cost_rub) : null]
        .filter(Boolean)
        .join(" · ")}
    </div>
  );
}

function PartMedia({ side, part, media }: { side: CompareSide; part: "start" | "end"; media: ShotMediaApi }) {
  const p = side.parts[part];
  const src = p?.exists ? media.thumb(p.path, 512, null) : null;
  // eslint-disable-next-line @next/next/no-img-element
  return src ? <img className="sb-shots-compare-img" src={src} alt={`${sideTitle(side)}: ${partTitle(part)}`} onError={media.onImageError} /> : <div className="sb-shots-frame sb-muted">{SHOT_TEXTS.part_missing}</div>;
}

// Два ролика играют и перематываются вместе
function VideoPair({ left, right, media }: { left: CompareSide; right: CompareSide; media: ShotMediaApi }) {
  const refs = useRef<(HTMLVideoElement | null)[]>([null, null]);
  const sides = [left, right];
  const link = (i: number) => {
    const other = () => refs.current[1 - i];
    return {
      onPlay: () => void other()?.play().catch(() => undefined),
      onPause: () => other()?.pause(),
      onSeeked: () => {
        const me = refs.current[i];
        const o = other();
        if (me && o && Math.abs(o.currentTime - me.currentTime) > 0.3) o.currentTime = me.currentTime;
      },
    };
  };
  return (
    <div className="sb-shots-compare">
      {sides.map((side, i) => {
        const p = side.parts.video;
        const src = p?.exists ? media.media(p.path) : null;
        return src ? (
          <video
            key={`${side.id}:${src}`}
            ref={(el) => {
              refs.current[i] = el;
            }}
            className="sb-shots-video"
            src={src}
            poster={media.thumb(p?.path, 512, null) ?? undefined}
            controls
            muted
            preload="metadata"
            {...link(i)}
          />
        ) : (
          <div key={side.id} className="sb-shots-frame sb-muted">
            {SHOT_TEXTS.part_missing}
          </div>
        );
      })}
    </div>
  );
}

function PromptRow({ left, right, part, changed }: { left: CompareSide; right: CompareSide; part: ShotPart; changed: boolean | undefined }) {
  const text = (side: CompareSide) => (side.prompts_trusted ? side.prompts[part] ?? SHOT_TEXTS.compare_none : SHOT_TEXTS.compare_none);
  return (
    <div>
      <div className="sb-muted">
        {partTitle(part)}: {changed ? SHOT_TEXTS.compare_prompt_changed : SHOT_TEXTS.compare_prompt_same}
      </div>
      <div className="sb-shots-compare">
        <pre className="sb-shots-prompt">{text(left)}</pre>
        <pre className="sb-shots-prompt">{text(right)}</pre>
      </div>
    </div>
  );
}

// Две версии рядом: картинки, ролики, промпты, данные запуска
export function VersionCompareDialog({ api, media, disabled, onRestore }: Props) {
  const res = api.compareResult;
  return (
    <StudioModal title={SHOT_TEXTS.compare_title} onClose={api.closeCompare} wide>
      {api.compareError ? (
        <p role="alert" className="sb-tone-danger">
          {api.compareError}
        </p>
      ) : null}
      {res ? (
        <>
          <div className="sb-shots-compare">
            {[res.left, res.right].map((side) => (
              <div key={side.id}>
                <h3 className="sb-title">{sideTitle(side)}</h3>
                <SideMeta side={side} />
              </div>
            ))}
          </div>
          {(["start", "end"] as const).map((part) => (
            <div key={part} className="sb-shots-compare">
              <PartMedia side={res.left} part={part} media={media} />
              <PartMedia side={res.right} part={part} media={media} />
            </div>
          ))}
          <VideoPair left={res.left} right={res.right} media={media} />
          {PARTS.map((part) => (
            <PromptRow key={part} left={res.left} right={res.right} part={part} changed={res.diff.prompt_changed[part]} />
          ))}
          <div className="sb-muted">
            {PARTS.filter((p) => res.diff.size_delta[p] !== undefined)
              .map((p) => `${partTitle(p)}: ${signedBytes(res.diff.size_delta[p] ?? 0)}`)
              .join(" · ")}
          </div>
          <div className="button-row">
            {res.left.id !== "current" ? (
              <button type="button" className="button secondary" disabled={disabled || api.restoring} onClick={() => onRestore(res.left.id)}>
                {SHOT_TEXTS.use_left}
              </button>
            ) : null}
            {res.right.id !== "current" ? (
              <button type="button" className="button secondary" disabled={disabled || api.restoring} onClick={() => onRestore(res.right.id)}>
                {SHOT_TEXTS.use_right}
              </button>
            ) : null}
            <button type="button" className="button ghost" data-autofocus="true" onClick={api.closeCompare}>
              {SHOT_TEXTS.close}
            </button>
          </div>
        </>
      ) : (
        <div className="button-row">
          <button type="button" className="button ghost" data-autofocus="true" onClick={api.closeCompare}>
            {SHOT_TEXTS.close}
          </button>
        </div>
      )}
    </StudioModal>
  );
}
