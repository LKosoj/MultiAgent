"use client";

import React from "react";

import { T } from "../../../lib/storybook/texts";
import { StudioModal } from "./StudioModal";

type Props = {
  title: string;
  body: string;
  confirmLabel: string;
  cancelLabel?: string;
  busy?: boolean;
  error?: string | null;
  onConfirm: () => void;
  onCancel: () => void;
};

// Подтверждение опасного действия: фокус на «Отмена», Enter не подтверждает
export function ConfirmDialog({ title, body, confirmLabel, cancelLabel, busy = false, error, onConfirm, onCancel }: Props) {
  return (
    <StudioModal title={title} onClose={onCancel} closable={!busy}>
      <p>{body}</p>
      {error ? (
        <p role="alert" className="sb-tone-danger">
          {error}
        </p>
      ) : null}
      <div className="button-row">
        <button type="button" className="button ghost" data-autofocus="true" onClick={onCancel} disabled={busy}>
          {cancelLabel ?? T.cancel}
        </button>
        <button type="button" className="button" onClick={onConfirm} disabled={busy}>
          {busy ? "…" : confirmLabel}
        </button>
      </div>
    </StudioModal>
  );
}
