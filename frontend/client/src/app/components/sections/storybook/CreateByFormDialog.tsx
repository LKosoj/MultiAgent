"use client";

import React, { useEffect, useRef, useState } from "react";

import type { ProjectsCreateParams, ProjectsSuggestIdResult } from "../../../lib/storybook/projects";
import { T } from "../../../lib/storybook/texts";
import type { Outcome } from "../../../lib/storybook/useProjectActions";
import { Banner } from "./Banner";
import { StudioModal } from "./StudioModal";

type Props = {
  onSubmit: (p: ProjectsCreateParams) => Promise<Outcome<string>>;
  suggestId: (hint: string) => Promise<Outcome<ProjectsSuggestIdResult>>;
  onClose: () => void;
};

export function CreateByFormDialog({ onSubmit, suggestId, onClose }: Props) {
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [genre, setGenre] = useState("");
  const [age, setAge] = useState("");
  const [suggested, setSuggested] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);

  // id подсказываем по названию с паузой, чтобы не слать запрос на каждую букву
  useEffect(() => {
    const hint = title.trim();
    const mine = ++seq.current;
    if (!hint) return;
    const timer = window.setTimeout(() => {
      void suggestId(hint).then((out) => {
        if (mine === seq.current) setSuggested(out.ok ? out.value.project_id : null);
      });
    }, 300);
    return () => window.clearTimeout(timer);
  }, [title, suggestId]);

  const ready = title.trim() && description.trim() && genre.trim() && age.trim();

  const submit = async () => {
    setBusy(true);
    setError(null);
    const out = await onSubmit({
      title: title.trim(),
      description: description.trim(),
      genre: genre.trim(),
      target_age: age.trim(),
      id_hint: title.trim(),
    });
    setBusy(false);
    if (out.ok) onClose();
    else setError(out.error.text);
  };

  return (
    <StudioModal title={T.formTitle} onClose={onClose} closable={!busy}>
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
        <p className="sb-muted">
          {T.idField}: {title.trim() && suggested ? suggested : "—"}. {T.idHint}.
        </p>
        <label className="field">
          <span className="label">{T.fieldDescription}</span>
          <textarea value={description} onChange={(e) => setDescription(e.target.value)} rows={4} disabled={busy} />
        </label>
        <div className="sb-form-grid">
          <label className="field">
            <span className="label">{T.fieldGenre}</span>
            <input value={genre} onChange={(e) => setGenre(e.target.value)} disabled={busy} />
          </label>
          <label className="field">
            <span className="label">{T.fieldAge}</span>
            <input value={age} onChange={(e) => setAge(e.target.value)} disabled={busy} />
          </label>
        </div>
        {error ? <Banner tone="error">{error}</Banner> : null}
        <div className="button-row">
          <button type="button" className="button ghost" onClick={onClose} disabled={busy}>
            {T.cancel}
          </button>
          <button type="submit" className="button" disabled={!ready || busy}>
            {T.create}
          </button>
        </div>
      </form>
    </StudioModal>
  );
}
