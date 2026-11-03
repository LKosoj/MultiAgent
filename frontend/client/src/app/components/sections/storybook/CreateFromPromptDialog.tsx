"use client";

import React, { useState } from "react";

import type { Outcome } from "../../../lib/storybook/useProjectActions";
import type { ProjectsCreateFromPromptParams } from "../../../lib/storybook/projects";
import { T } from "../../../lib/storybook/texts";
import { Banner } from "./Banner";
import { StudioModal } from "./StudioModal";

type Props = {
  progress: string | null; // не null - задание идёт
  onSubmit: (p: ProjectsCreateFromPromptParams) => Promise<Outcome<string>>;
  onClose: () => void;
};

const MIN_PROMPT = 10;

export function CreateFromPromptDialog({ progress, onSubmit, onClose }: Props) {
  const [prompt, setPrompt] = useState("");
  const [language, setLanguage] = useState<"ru" | "en">("ru");
  const [pagesMin, setPagesMin] = useState("9");
  const [pagesMax, setPagesMax] = useState("12");
  const [wordsMin, setWordsMin] = useState("400");
  const [wordsMax, setWordsMax] = useState("450");
  const [error, setError] = useState<string | null>(null);
  const busy = progress !== null;
  const tooShort = prompt.trim().length < MIN_PROMPT;

  const submit = async () => {
    setError(null);
    const out = await onSubmit({
      prompt: prompt.trim(),
      language,
      pages_min: Number(pagesMin),
      pages_max: Number(pagesMax),
      words_per_page_min: Number(wordsMin),
      words_per_page_max: Number(wordsMax),
    });
    if (out.ok) onClose();
    else setError(out.error.text);
  };

  return (
    <StudioModal title={T.promptTitle} onClose={onClose} closable={!busy}>
      <form
        className="sb-form"
        onSubmit={(ev) => {
          ev.preventDefault();
          if (!tooShort && !busy) void submit();
        }}
      >
        <label className="field">
          <span className="label">{T.promptField}</span>
          <textarea
            data-autofocus="true"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder={T.promptHint}
            rows={6}
            maxLength={8000}
            disabled={busy}
          />
        </label>
        <p className="sb-muted">{T.promptLlmWarning}</p>
        <details>
          <summary>{T.textParams}</summary>
          <div className="sb-form-grid" style={{ marginTop: 10 }}>
            <label className="field">
              <span className="label">{T.language}</span>
              <select value={language} onChange={(e) => setLanguage(e.target.value === "en" ? "en" : "ru")} disabled={busy}>
                <option value="ru">Русский</option>
                <option value="en">English</option>
              </select>
            </label>
            <span />
            <label className="field">
              <span className="label">{T.pagesMin}</span>
              <input type="number" min={1} value={pagesMin} onChange={(e) => setPagesMin(e.target.value)} disabled={busy} />
            </label>
            <label className="field">
              <span className="label">{T.pagesMax}</span>
              <input type="number" min={1} value={pagesMax} onChange={(e) => setPagesMax(e.target.value)} disabled={busy} />
            </label>
            <label className="field">
              <span className="label">{T.wordsMin}</span>
              <input type="number" min={1} value={wordsMin} onChange={(e) => setWordsMin(e.target.value)} disabled={busy} />
            </label>
            <label className="field">
              <span className="label">{T.wordsMax}</span>
              <input type="number" min={1} value={wordsMax} onChange={(e) => setWordsMax(e.target.value)} disabled={busy} />
            </label>
          </div>
        </details>
        {busy ? <Banner tone="info">{progress || "Создаём проект…"}</Banner> : null}
        {error ? <Banner tone="error">{error}</Banner> : null}
        <div className="button-row">
          <button type="button" className="button ghost" onClick={onClose} disabled={busy}>
            {T.cancel}
          </button>
          <button type="submit" className="button" disabled={tooShort || busy}>
            {T.createAndOpen}
          </button>
        </div>
      </form>
    </StudioModal>
  );
}
