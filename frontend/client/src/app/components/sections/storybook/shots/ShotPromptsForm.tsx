"use client";

import React from "react";

import type { ShotCard, ShotChanges, ShotForm } from "../../../../lib/storybook/shots";
import { SHOT_TEXTS, type ShotTextId } from "../../../../lib/storybook/shotsTexts";
import type { ShotCardApi } from "../../../../lib/storybook/useShotCard";
import { Banner } from "../Banner";

type Props = {
  card: ShotCard;
  api: ShotCardApi;
  // шот в работе или идёт прогон: правки запрещены
  lockedText: string | null;
};

type TextField = Exclude<keyof ShotForm, "duration_s">;
const FIELDS: { name: TextField; label: ShotTextId }[] = [
  { name: "start_prompt", label: "prompt_start" },
  { name: "end_prompt", label: "prompt_end" },
  { name: "video_prompt", label: "prompt_video" },
  { name: "start_negative_prompt", label: "prompt_neg_start" },
  { name: "end_negative_prompt", label: "prompt_neg_end" },
];

const NEGATIVE = new Set<TextField>(["start_negative_prompt", "end_negative_prompt"]);

// Форма правки промптов, длительности и заметки ревью
export function ShotPromptsForm({ card, api, lockedText }: Props) {
  const { form, note, dirtyFields, dirty, saving } = api;
  if (!form) return null;
  const allowed = new Set<string>(card.editable.fields);
  const locked = card.editable.locked || lockedText !== null;
  const hint = card.editable.hint || null;
  const lockNote = locked ? (lockedText ?? card.editable.locked_reason ?? null) : null;
  const changed = (name: keyof ShotChanges) => name in dirtyFields;
  const renderField = ({ name, label }: { name: TextField; label: ShotTextId }) => (
    <label key={name} className="sb-shots-field-block" data-dirty={changed(name) ? "true" : "false"}>
      <span>{SHOT_TEXTS[label]}</span>
      <textarea
        rows={NEGATIVE.has(name) ? 2 : 4}
        value={form[name]}
        disabled={locked || saving || !allowed.has(name)}
        onChange={(e) => api.setField(name, e.target.value)}
      />
    </label>
  );
  return (
    <form
      className="sb-shots-form"
      onSubmit={(e) => {
        e.preventDefault();
        void api.save();
      }}
    >
      {api.draftRestored ? <Banner tone="info">{SHOT_TEXTS.draft_restored}</Banner> : null}
      {hint ? <p className="sb-muted">{hint}</p> : null}
      {lockNote ? <p className="sb-muted">{lockNote}</p> : null}
      {FIELDS.filter((f) => !NEGATIVE.has(f.name)).map(renderField)}
      <label className="sb-shots-field-block" data-dirty={changed("duration_s") ? "true" : "false"}>
        <span>{SHOT_TEXTS.duration_label}</span>
        <input
          type="number"
          min={0}
          step={0.5}
          value={form.duration_s ?? ""}
          disabled={locked || saving || !allowed.has("duration_s")}
          onChange={(e) => api.setField("duration_s", e.target.value === "" ? null : Number(e.target.value))}
        />
      </label>
      <details className="sb-shots-advanced" open={FIELDS.some((f) => NEGATIVE.has(f.name) && changed(f.name))}>
        <summary>{SHOT_TEXTS.advanced}</summary>
        {FIELDS.filter((f) => NEGATIVE.has(f.name)).map(renderField)}
      </details>
      <label className="sb-shots-field-block" data-dirty={api.noteDirty ? "true" : "false"}>
        <span>{SHOT_TEXTS.note_label}</span>
        <textarea rows={2} value={note} disabled={saving} onChange={(e) => api.setNote(e.target.value)} />
      </label>
      {api.effectsNote ? <Banner tone="info">{api.effectsNote}</Banner> : null}
      <div className="button-row">
        <button type="submit" className="button" disabled={!dirty || saving}>
          {SHOT_TEXTS.save}
        </button>
        <button type="button" className="button ghost" disabled={!dirty || saving} onClick={api.discard}>
          {SHOT_TEXTS.discard_edit}
        </button>
      </div>
    </form>
  );
}
