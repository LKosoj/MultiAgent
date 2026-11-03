"use client";

import React from "react";

import { BRIEF_FIELDS, blockoutOn, briefValue, type BriefDraft, type BriefKey } from "../../../../lib/storybook/generationForm";
import { gfmt, GEN_TEXTS } from "../../../../lib/storybook/generationTexts";
import { modeOptions, type Selection } from "../../../../lib/storybook/generationModel";
import type { RegenWhat } from "../../../../lib/storybook/review";
import type { PipelineStepInfo, RunMode } from "../../../../lib/storybook/runs";

type Props = {
  selection: Selection;
  steps: PipelineStepInfo[];
  resumable: boolean;
  disabled: boolean;
  needsTask: boolean;
  task: string;
  brief: BriefDraft;
  planInputs: Record<string, unknown> | null;
  inputError: string | null;
  regenWhat: RegenWhat;
  onMode: (mode: RunMode) => void;
  onStep: (id: string | null) => void;
  onTask: (value: string) => void;
  onBrief: (key: BriefKey, value: string | boolean) => void;
  onRegenWhat: (what: RegenWhat) => void;
  onOpenBlockout: () => void;
};

const REGEN_OPTIONS: { value: RegenWhat; text: "regen_images" | "regen_video" | "regen_both" | "regen_music" | "regen_film" }[] = [
  { value: "images", text: "regen_images" },
  { value: "video", text: "regen_video" },
  { value: "both", text: "regen_both" },
  { value: "music", text: "regen_music" },
  { value: "film", text: "regen_film" },
];

// Режим запуска, шаг, описание сказки и поля брифа
export function RunModeForm(p: Props) {
  const options = modeOptions({ resumable: p.resumable, shotCount: p.selection.shotKeys.length });
  const mode = p.selection.mode;
  return (
    <section className="sb-gen-card" aria-label={GEN_TEXTS.section_mode}>
      <h3 className="sb-h3">{GEN_TEXTS.section_mode}</h3>
      <div role="radiogroup" aria-label={GEN_TEXTS.section_mode} className="sb-gen-modes">
        {options.map((o) => (
          <label key={o.mode} className="toggle" title={o.hint ?? undefined}>
            <input
              type="radio"
              name="sb-gen-mode"
              checked={mode === o.mode}
              disabled={p.disabled || !o.enabled}
              onChange={() => p.onMode(o.mode)}
            />
            <span>
              {o.label}
              {o.hint ? <span className="sb-muted"> · {o.hint}</span> : null}
            </span>
          </label>
        ))}
      </div>

      {mode === "from_step" || mode === "single_step" ? (
        <label className="field">
          <span className="label">{GEN_TEXTS.mode_step}</span>
          <select value={p.selection.stepId ?? ""} disabled={p.disabled} onChange={(e) => p.onStep(e.target.value || null)}>
            <option value="">{GEN_TEXTS.why_pick_step}</option>
            {p.steps.map((s) => (
              <option key={s.id} value={s.id}>
                {s.title ?? s.id}
              </option>
            ))}
          </select>
        </label>
      ) : null}

      {mode === "regenerate_shots" ? (
        <div className="sb-gen-regen">
          <div className="sb-muted">
            {p.selection.shotKeys.length > 0 ? gfmt("mode_shots_count", { n: p.selection.shotKeys.length }) : GEN_TEXTS.mode_shots_none}
          </div>
          <label className="field">
            <span className="label">{GEN_TEXTS.regen_what}</span>
            <select value={p.regenWhat} disabled={p.disabled} onChange={(e) => p.onRegenWhat(e.target.value as RegenWhat)}>
              {REGEN_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {GEN_TEXTS[o.text]}
                </option>
              ))}
            </select>
          </label>
        </div>
      ) : null}

      {mode === "full" ? (
        <>
          <label className="field">
            <span className="label">{GEN_TEXTS.brief_task}</span>
            <textarea value={p.task} disabled={p.disabled} onChange={(e) => p.onTask(e.target.value)} aria-describedby="sb-gen-task-hint" />
            <span id="sb-gen-task-hint" className="sb-muted">
              {p.needsTask ? GEN_TEXTS.brief_task_hint : GEN_TEXTS.brief_exists}
            </span>
          </label>
          <details>
            <summary>{GEN_TEXTS.advanced}</summary>
            <div className="sb-gen-brief">
              {BRIEF_FIELDS.map((f) => {
                const value = briefValue(p.brief, p.planInputs, f.key);
                return f.kind === "bool" ? (
                  <label key={f.key} className="toggle">
                    <input type="checkbox" checked={value === true} disabled={p.disabled} onChange={(e) => p.onBrief(f.key, e.target.checked)} />
                    {GEN_TEXTS[f.label]}
                  </label>
                ) : (
                  <label key={f.key} className="field">
                    <span className="label">{GEN_TEXTS[f.label]}</span>
                    <input
                      type="text"
                      inputMode={f.kind === "int" ? "numeric" : undefined}
                      value={String(value)}
                      disabled={p.disabled}
                      onChange={(e) => p.onBrief(f.key, e.target.value)}
                    />
                  </label>
                );
              })}
              <div className="sb-muted">
                {blockoutOn(p.planInputs) ? GEN_TEXTS.brief_blockout_on : GEN_TEXTS.brief_blockout_off}{" "}
                <button type="button" className="button ghost" onClick={p.onOpenBlockout}>
                  {GEN_TEXTS.brief_blockout_link}
                </button>
              </div>
            </div>
          </details>
          {p.inputError ? (
            <p role="alert" className="sb-tone-danger">
              {p.inputError}
            </p>
          ) : null}
        </>
      ) : null}
    </section>
  );
}
