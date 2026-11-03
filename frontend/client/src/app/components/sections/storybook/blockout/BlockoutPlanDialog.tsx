"use client";

import React from "react";

import type { PlanDialogState } from "../../../../lib/storybook/useBlockout";
import { Banner } from "../Banner";
import { StudioModal } from "../StudioModal";

type Props = { dialog: PlanDialogState; onConfirm: () => void; onClose: () => void };

const tone = (level: string) => (level === "error" ? "error" : level === "warning" ? "warn" : "info");

// Окно плана: либо только список проблем, либо подтверждение запуска (фокус на «Отмена»)
export function BlockoutPlanDialog({ dialog, onConfirm, onClose }: Props) {
  const v = dialog.view;
  const problems = v.problems.map((p, i) => (
    <Banner key={i} tone={tone(p.level)}>
      {p.label}: {p.message}
    </Banner>
  ));
  if (v.kind === "problems") {
    return (
      <StudioModal title={v.title} onClose={onClose}>
        {problems}
        <div className="button-row">
          <button type="button" className="button" data-autofocus="true" onClick={onClose}>
            {v.close}
          </button>
        </div>
      </StudioModal>
    );
  }
  return (
    <StudioModal title={v.title} onClose={onClose} closable={!dialog.busy}>
      {dialog.error ? <Banner tone="error">{dialog.error}</Banner> : null}
      {v.text ? <p>{v.text}</p> : null}
      {problems}
      {v.notes.map((n, i) => (
        <p key={i} className="sb-muted">
          {n}
        </p>
      ))}
      <div className="button-row">
        <button type="button" className="button ghost" data-autofocus="true" disabled={dialog.busy} onClick={onClose}>
          {v.cancel}
        </button>
        <button type="button" className="button" disabled={dialog.busy} onClick={onConfirm}>
          {dialog.busy ? "…" : v.go}
        </button>
      </div>
    </StudioModal>
  );
}

