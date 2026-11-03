"use client";

import React, { useState } from "react";

import {
  activeModel,
  activeProvider,
  budgetText,
  changeProvider,
  durationChoices,
  isDirty,
  isDurationOn,
  modelOptions,
  parseBudget,
  patchAdvanced,
  patchAudio,
  patchBudget,
  patchVideo,
  providerOptions,
  resetToDefaults,
  resolutionOptions,
  sourceHint,
  styleGroups,
  styleValue,
  toggleDuration,
  withStyle,
} from "../../../../lib/storybook/generationForm";
import { GEN_TEXTS } from "../../../../lib/storybook/generationTexts";
import type { RunSettingsApi } from "../../../../lib/storybook/useRunSettings";

type Props = {
  api: RunSettingsApi;
  // идёт прогон или проект занят: всё, кроме бюджета, заблокировано
  locked: boolean;
  isAdmin: boolean;
  saving: boolean;
  onSave: () => void;
};

const hint = (text: string | null) => (text ? <span className="sb-muted">{text}</span> : null);

// Настройки прогона: провайдер, модель, разрешение, стиль, звук, бюджет и дополнительные флаги
export function RunSettingsForm({ api, locked, isAdmin, saving, onSave }: Props) {
  const { draft, saved, catalog, styles, effective, defaults } = api;
  const [budgetInput, setBudgetInput] = useState<string | null>(null);
  if (!draft) return null;
  const provider = activeProvider(catalog, draft);
  const model = activeModel(provider, draft);
  const off = locked || !isAdmin;
  const durations = durationChoices(model);
  const budgetShown = budgetInput ?? budgetText(draft.budget_rub);
  const budgetErr = budgetInput !== null ? parseBudget(budgetInput).error : null;
  const dirty = isDirty(saved, draft);

  return (
    <section className="sb-gen-card" aria-label={GEN_TEXTS.section_settings}>
      <h3 className="sb-h3">{GEN_TEXTS.section_settings}</h3>
      {locked ? <p className="sb-muted">{GEN_TEXTS.settings_locked}</p> : null}
      <div className="sb-gen-fields">
        <label className="field">
          <span className="label">{GEN_TEXTS.video_provider}</span>
          <select value={draft.video.provider ?? catalog?.selected_provider ?? ""} disabled={off} onChange={(e) => api.setDraft(changeProvider(draft, e.target.value || null))}>
            {providerOptions(catalog).map((o) => (
              <option key={o.value} value={o.value} disabled={o.disabled}>
                {o.label}
              </option>
            ))}
          </select>
          {hint(sourceHint(effective, "video.provider"))}
        </label>
        <label className="field">
          <span className="label">{GEN_TEXTS.video_model}</span>
          <select value={draft.video.model ?? model?.id ?? ""} disabled={off} onChange={(e) => api.setDraft(patchVideo(draft, "model", e.target.value || null))}>
            {modelOptions(provider).map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
          {hint(sourceHint(effective, "video.model"))}
        </label>
        {provider?.settings_honored.fallback_model ? (
          <label className="field">
            <span className="label">{GEN_TEXTS.video_fallback_model}</span>
            <select value={draft.video.fallback_model ?? ""} disabled={off} onChange={(e) => api.setDraft(patchVideo(draft, "fallback_model", e.target.value || null))}>
              <option value="">—</option>
              {modelOptions(provider).map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        {provider?.settings_honored.resolution ? (
          <label className="field">
            <span className="label">{GEN_TEXTS.video_resolution}</span>
            <select value={draft.video.resolution ?? ""} disabled={off} onChange={(e) => api.setDraft(patchVideo(draft, "resolution", e.target.value || null))}>
              <option value="">—</option>
              {resolutionOptions(model).map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        {provider?.settings_honored.retry_cycles ? (
          <label className="field">
            <span className="label">{GEN_TEXTS.video_retry_cycles}</span>
            <input
              type="number"
              min={0}
              value={draft.video.retry_cycles ?? ""}
              disabled={off}
              onChange={(e) => api.setDraft(patchVideo(draft, "retry_cycles", e.target.value === "" ? null : Math.max(0, Math.floor(Number(e.target.value)))))}
            />
          </label>
        ) : null}
      </div>

      {durations.length > 0 ? (
        <fieldset className="field">
          <legend className="label">{GEN_TEXTS.video_durations}</legend>
          <div className="toggle-grid">
            {durations.map((d) => (
              <label key={d} className="toggle">
                <input
                  type="checkbox"
                  checked={isDurationOn(draft.video.allowed_durations, d)}
                  disabled={off}
                  onChange={() => api.setDraft(patchVideo(draft, "allowed_durations", toggleDuration(draft.video.allowed_durations, durations, d)))}
                />
                {d}
              </label>
            ))}
          </div>
        </fieldset>
      ) : null}

      <label className="field">
        <span className="label">{GEN_TEXTS.style}</span>
        <select value={styleValue(draft)} disabled={off} onChange={(e) => api.setDraft(withStyle(draft, e.target.value))}>
          <option value="">—</option>
          {styleGroups(styles).map((g) => (
            <optgroup key={g.profile} label={g.profile}>
              {g.options.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </optgroup>
          ))}
        </select>
      </label>

      <div className="toggle-grid">
        <label className="toggle">
          <input type="checkbox" checked={draft.audio.music === true} disabled={off} onChange={(e) => api.setDraft(patchAudio(draft, "music", e.target.checked))} />
          {GEN_TEXTS.audio_music}
        </label>
        <label className="toggle">
          <input type="checkbox" checked={draft.audio.sfx === true} disabled={off} onChange={(e) => api.setDraft(patchAudio(draft, "sfx", e.target.checked))} />
          {GEN_TEXTS.audio_sfx}
        </label>
        <label className="toggle">
          <input type="checkbox" checked={draft.audio.subtitles === true} disabled={off} onChange={(e) => api.setDraft(patchAudio(draft, "subtitles", e.target.checked))} />
          {GEN_TEXTS.audio_subtitles}
        </label>
      </div>

      <label className="field">
        <span className="label">{GEN_TEXTS.budget}</span>
        <input
          type="text"
          inputMode="decimal"
          value={budgetShown}
          disabled={!isAdmin}
          aria-invalid={budgetErr ? true : undefined}
          onChange={(e) => {
            setBudgetInput(e.target.value);
            const parsed = parseBudget(e.target.value);
            if (!parsed.error) api.setDraft(patchBudget(draft, parsed.value));
          }}
          onBlur={() => setBudgetInput(null)}
        />
        <span className="sb-muted">{budgetErr ?? GEN_TEXTS.budget_hint}</span>
      </label>

      <details>
        <summary>{GEN_TEXTS.advanced}</summary>
        <div className="toggle-grid">
          <label className="toggle">
            <input type="checkbox" checked={draft.advanced.force_update_prompts === true} disabled={off} onChange={(e) => api.setDraft(patchAdvanced(draft, "force_update_prompts", e.target.checked))} />
            {GEN_TEXTS.adv_force_prompts}
          </label>
          <label className="toggle">
            <input type="checkbox" checked={draft.advanced.skip_prompt_enhancement === true} disabled={off} onChange={(e) => api.setDraft(patchAdvanced(draft, "skip_prompt_enhancement", e.target.checked))} />
            {GEN_TEXTS.adv_skip_enhance}
          </label>
          <label className="toggle">
            <input type="checkbox" checked={draft.advanced.sample_before_batch === true} disabled={off} onChange={(e) => api.setDraft(patchAdvanced(draft, "sample_before_batch", e.target.checked))} />
            {GEN_TEXTS.adv_sample_first}
          </label>
          <label className="toggle">
            <input type="checkbox" checked={draft.advanced.final_allow_missing_audio === true} disabled={off} onChange={(e) => api.setDraft(patchAdvanced(draft, "final_allow_missing_audio", e.target.checked))} />
            {GEN_TEXTS.adv_final_no_audio}
          </label>
        </div>
      </details>

      <div className="button-row">
        <button type="button" className="button" disabled={!dirty || saving || !isAdmin || budgetErr !== null} onClick={onSave} title="Ctrl/Cmd+S">
          {GEN_TEXTS.save}
        </button>
        <button type="button" className="button ghost" disabled={off || !defaults} onClick={() => defaults && api.setDraft(resetToDefaults(defaults))}>
          {GEN_TEXTS.reset_defaults}
        </button>
      </div>
    </section>
  );
}
