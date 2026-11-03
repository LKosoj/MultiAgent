"use client";

import React from "react";

import { SHOT_TEXTS, fmt } from "../../../../lib/storybook/shotsTexts";
import { StudioModal } from "../StudioModal";

type Props = {
  // название шота для текста «Изменения в «…»»
  what: string;
  busy?: boolean;
  onSave: () => void;
  onDiscard: () => void;
  onStay: () => void;
};

// Несохранённые правки при смене шота
export function UnsavedDialog({ what, busy = false, onSave, onDiscard, onStay }: Props) {
  return (
    <StudioModal title={SHOT_TEXTS.unsaved_title} onClose={onStay} closable={!busy}>
      <p>{fmt("unsaved_body", { what })}</p>
      <div className="button-row">
        <button type="button" className="button" data-autofocus="true" disabled={busy} onClick={onSave}>
          {SHOT_TEXTS.unsaved_save}
        </button>
        <button type="button" className="button secondary" disabled={busy} onClick={onDiscard}>
          {SHOT_TEXTS.unsaved_discard}
        </button>
        <button type="button" className="button ghost" disabled={busy} onClick={onStay}>
          {SHOT_TEXTS.unsaved_stay}
        </button>
      </div>
    </StudioModal>
  );
}
