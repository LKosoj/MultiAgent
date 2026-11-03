"use client";

import React, { useState } from "react";

import { versionLabel, type VersionView } from "../../../../lib/storybook/review";
import { SHOT_TEXTS, fmt, formatRubExact } from "../../../../lib/storybook/shotsTexts";
import { formatCreatedAt } from "../../../../lib/storybook/shotsView";
import type { ShotMediaApi } from "../../../../lib/storybook/useShotMedia";
import type { VersionsApi } from "../../../../lib/storybook/useVersions";
import { ConfirmDialog } from "../ConfirmDialog";
import { Skeleton } from "../Skeleton";
import { VersionCompareDialog } from "./VersionCompareDialog";

type Props = {
  api: VersionsApi;
  media: ShotMediaApi;
  // шот в работе или идёт прогон: возврат версии запрещён
  disabled: boolean;
  disabledHint: string | null;
};

// Список версий шота: сравнить с текущей, вернуть
export function ShotVersions({ api, media, disabled, disabledHint }: Props) {
  const [pending, setPending] = useState<VersionView | null>(null);

  const confirmRestore = async () => {
    if (!pending) return;
    const id = pending.id;
    const ok = await api.restore(id);
    if (ok) setPending(null);
  };

  return (
    <section className="sb-shots-versions">
      <h3 className="sb-title">{fmt("versions_title", { n: api.versions.length })}</h3>
      {api.status === "loading" && api.versions.length === 0 ? <Skeleton lines={2} label={SHOT_TEXTS.loading} /> : null}
      {api.status === "error" ? (
        <p role="alert" className="sb-tone-danger">
          {api.error}
        </p>
      ) : null}
      {api.status === "ready" && api.versions.length === 0 ? <p className="sb-muted">{SHOT_TEXTS.versions_none}</p> : null}
      {disabled && disabledHint ? <p className="sb-muted">{disabledHint}</p> : null}
      <ul className="sb-shots-version-list">
        {api.versions.map((v) => (
          <li key={v.id} className="sb-shots-version">
            <span className="sb-shots-version-info">
              {v.parts.start?.exists && media.thumb(v.parts.start.path, 128, null) ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img className="sb-shots-version-thumb" src={media.thumb(v.parts.start.path, 128, null) ?? ""} alt="" loading="lazy" onError={media.onImageError} />
              ) : null}
              <span>
                <strong>{versionLabel(v)}</strong>
                {v.is_selected ? <span className="sb-shots-chip">{SHOT_TEXTS.version_selected}</span> : null}
                <span className="sb-muted">
                  {" "}
                  {[formatCreatedAt(v.created_at), v.reason_title, [v.provider, v.model].filter(Boolean).join(" / ") || null, v.cost_rub !== null ? formatRubExact(v.cost_rub) : null]
                    .filter(Boolean)
                    .join(" · ")}
                </span>
              </span>
            </span>
            <span className="button-row">
              <button type="button" className="button secondary" disabled={api.comparing} onClick={() => void api.compare(v.id, "current")}>
                {SHOT_TEXTS.compare}
              </button>
              <button type="button" className="button secondary" disabled={disabled || api.restoring} onClick={() => setPending(v)}>
                {SHOT_TEXTS.restore}
              </button>
            </span>
          </li>
        ))}
      </ul>
      {api.compareResult || api.compareError ? (
        <VersionCompareDialog
          api={api}
          media={media}
          disabled={disabled}
          onRestore={(id) => {
            const v = api.versions.find((x) => x.id === id);
            if (v) setPending(v);
          }}
        />
      ) : null}
      {pending ? (
        <ConfirmDialog
          title={fmt("restore_title", { n: pending.number })}
          body={SHOT_TEXTS.restore_body}
          confirmLabel={SHOT_TEXTS.restore_confirm}
          cancelLabel={SHOT_TEXTS.regen_cancel}
          busy={api.restoring}
          onConfirm={() => void confirmRestore()}
          onCancel={() => setPending(null)}
        />
      ) : null}
    </section>
  );
}
