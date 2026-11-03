"use client";

import React from "react";

import type { Dialog } from "../../../../lib/storybook/useGenerationFlow";
import { GEN_TEXTS } from "../../../../lib/storybook/generationTexts";
import { T } from "../../../../lib/storybook/texts";
import { StudioModal } from "../StudioModal";

type Props = {
  dialog: Dialog;
  body: string;
  busy: boolean;
  onConfirm: (choice?: "raise") => void;
  onClose: () => void;
};

const TITLES = {
  start: "confirm_start_title",
  no_estimate: "confirm_none_title",
  over_budget: "confirm_over_title",
  restart: "confirm_restart_title",
  discard: "confirm_restart_title",
  mark: "confirm_mark_title",
} as const;

// Все окна подтверждения экрана: запуск, «без оценки», «оценка выше бюджета», «сначала», «пометить», «стоп»
export function StartConfirmDialogs({ dialog, body, busy, onConfirm, onClose }: Props) {
  if (!dialog) return null;
  const title = dialog.type === "stop" ? T.stopTitle : GEN_TEXTS[TITLES[dialog.type]];
  const text =
    dialog.type === "stop" ? T.stopBody : dialog.type === "restart" ? GEN_TEXTS.confirm_restart_body : dialog.type === "discard" ? GEN_TEXTS.confirm_discard_body : dialog.type === "mark" ? GEN_TEXTS.confirm_mark_body : body;
  const okLabel =
    dialog.type === "stop"
      ? T.stopConfirm
      : dialog.type === "restart" || dialog.type === "discard"
        ? GEN_TEXTS.confirm_restart_ok
        : dialog.type === "mark"
          ? GEN_TEXTS.confirm_mark_ok
          : dialog.type === "no_estimate"
            ? GEN_TEXTS.confirm_none_ok
            : dialog.type === "over_budget"
              ? GEN_TEXTS.confirm_over_go
              : GEN_TEXTS.confirm_start_ok;
  return (
    <StudioModal title={title} onClose={onClose} closable={!busy}>
      <p>{text}</p>
      {dialog.error ? (
        <p role="alert" className="sb-tone-danger">
          {dialog.error}
        </p>
      ) : null}
      <div className="button-row">
        <button type="button" className="button ghost" data-autofocus="true" disabled={busy} onClick={onClose}>
          {dialog.type === "stop" ? T.stopCancel : GEN_TEXTS.confirm_cancel}
        </button>
        {dialog.type === "over_budget" ? (
          <button type="button" className="button secondary" disabled={busy} onClick={() => onConfirm("raise")}>
            {GEN_TEXTS.confirm_over_raise}
          </button>
        ) : null}
        <button type="button" className="button" disabled={busy} onClick={() => onConfirm()}>
          {busy ? "…" : okLabel}
        </button>
      </div>
    </StudioModal>
  );
}
