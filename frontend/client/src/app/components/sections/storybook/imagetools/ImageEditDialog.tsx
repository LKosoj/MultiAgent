"use client";

import React from "react";

import { IMAGE_TEXTS, imageFmt } from "../../../../lib/storybook/imageToolsTexts";
import type { ImageEditApi } from "../../../../lib/storybook/useImageEdit";
import { editCostLine, REFERENCES_MAX } from "../../../../lib/storybook/imageEditFlow";
import { Banner } from "../Banner";
import { Skeleton } from "../Skeleton";
import { StudioModal } from "../StudioModal";

type Props = {
  api: ImageEditApi;
  // ссылка на правимую картинку (уже с версией); нет ссылки - предпросмотра не будет
  imageUrl: string | null;
};

// Окно «Править ИИ»: что изменить, образцы, цена. По умолчанию в фокусе «Отмена», Enter ничего не запускает
export function ImageEditDialog({ api, imageUrl }: Props) {
  const { phase, preview, button, target } = api;
  const busy = phase === "loading" || phase === "starting";
  const max = preview?.limits.references_max ?? REFERENCES_MAX;
  const full = api.references.length >= max;
  return (
    <StudioModal title={imageFmt("edit_title", { name: target?.name ?? "" })} onClose={api.close} closable={phase !== "starting"}>
      <div className="sb-imagetools-edit">
        {imageUrl ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img className="sb-imagetools-edit-preview" src={imageUrl} alt={target?.name ?? ""} />
        ) : null}
        <p className="sb-muted">{IMAGE_TEXTS.edit_backup_note}</p>
        {!api.supported ? <Banner tone="error">{IMAGE_TEXTS.edit_not_supported}</Banner> : null}
        <label className="sb-imagetools-field">
          <span>{IMAGE_TEXTS.edit_prompt_label}</span>
          <textarea
            rows={4}
            value={api.prompt}
            disabled={phase === "starting"}
            placeholder={IMAGE_TEXTS.edit_prompt_hint}
            onChange={(e) => api.setPrompt(e.target.value)}
          />
        </label>
        <fieldset className="sb-imagetools-refs" disabled={busy}>
          <legend>{IMAGE_TEXTS.edit_refs_label}</legend>
          {api.candidates.length === 0 ? (
            <p className="sb-muted">{IMAGE_TEXTS.edit_refs_none}</p>
          ) : (
            <ul>
              {api.candidates.map((c) => {
                const checked = api.references.includes(c.path);
                return (
                  <li key={c.path}>
                    <label className="toggle">
                      <input type="checkbox" checked={checked} disabled={!checked && full} onChange={() => api.toggleRef(c.path)} />
                      <span>{c.label}</span>
                    </label>
                  </li>
                );
              })}
            </ul>
          )}
          <p className="sb-muted">{imageFmt("edit_refs_count", { n: api.references.length })}</p>
        </fieldset>
        {phase === "loading" && !preview ? (
          <>
            <p className="sb-muted">{IMAGE_TEXTS.edit_loading}</p>
            <Skeleton lines={2} label={IMAGE_TEXTS.edit_loading} />
          </>
        ) : null}
        {api.error ? <Banner tone="error">{api.error}</Banner> : null}
        {preview ? (
          <div className="sb-imagetools-plan">
            {preview.problems.map((p, i) => (
              <Banner key={`${p.code}-${i}`} tone={p.level === "error" ? "error" : p.level === "warning" ? "warn" : "info"}>
                {p.message}
              </Banner>
            ))}
            <p className="sb-muted">{editCostLine(preview.estimate.cost_rub, preview.estimate.unknown_cost)}</p>
            {button?.note && button.note !== "edit_cost_unknown" ? <p className="sb-muted">{IMAGE_TEXTS[button.note]}</p> : null}
          </div>
        ) : null}
      </div>
      <div className="button-row">
        <button type="button" className="button ghost" data-autofocus="true" disabled={phase === "starting"} onClick={api.close}>
          {IMAGE_TEXTS.edit_cancel}
        </button>
        <button type="button" className="button" disabled={!api.canSubmit || busy} onClick={() => void api.start()}>
          {button ? button.label : IMAGE_TEXTS.edit_go_free}
        </button>
      </div>
    </StudioModal>
  );
}
