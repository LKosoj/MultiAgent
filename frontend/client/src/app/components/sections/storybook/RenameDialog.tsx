"use client";

import React, { useState } from "react";

import { T } from "../../../lib/storybook/texts";
import type { Outcome } from "../../../lib/storybook/useProjectActions";
import { Banner } from "./Banner";
import { StudioModal } from "./StudioModal";

type Props = {
  currentTitle: string;
  onSubmit: (title: string) => Promise<Outcome<void>>;
  onClose: () => void;
};

export function RenameDialog({ currentTitle, onSubmit, onClose }: Props) {
  const [title, setTitle] = useState(currentTitle);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ready = title.trim().length > 0 && title.trim() !== currentTitle;

  const submit = async () => {
    setBusy(true);
    setError(null);
    const out = await onSubmit(title.trim());
    setBusy(false);
    if (out.ok) onClose();
    else setError(out.error.text);
  };

  return (
    <StudioModal title={T.renameTitle} onClose={onClose} closable={!busy}>
      <form
        className="sb-form"
        onSubmit={(ev) => {
          ev.preventDefault();
          if (ready && !busy) void submit();
        }}
      >
        <label className="field">
          <span className="label">{T.fieldTitle}</span>
          <input data-autofocus="true" value={title} onChange={(e) => setTitle(e.target.value)} disabled={busy} />
        </label>
        {error ? <Banner tone="error">{error}</Banner> : null}
        <div className="button-row">
          <button type="button" className="button ghost" onClick={onClose} disabled={busy}>
            {T.cancel}
          </button>
          <button type="submit" className="button" disabled={!ready || busy}>
            {T.save}
          </button>
        </div>
      </form>
    </StudioModal>
  );
}
