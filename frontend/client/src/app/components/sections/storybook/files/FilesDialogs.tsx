"use client";

import React from "react";

import { FILES_TEXTS, filesFmt } from "../../../../lib/storybook/filesTexts";
import type { ValidateAllEntry, ValidateAllSummary } from "../../../../lib/storybook/filesView";
import { validateAllText } from "../../../../lib/storybook/filesView";
import { StudioModal } from "../StudioModal";

// Есть несохранённые правки, а пользователь уходит к другому файлу
export function UnsavedDialog({ what, busy, onSave, onDiscard, onStay }: {
  what: string;
  busy: boolean;
  onSave: () => void;
  onDiscard: () => void;
  onStay: () => void;
}) {
  return (
    <StudioModal title={FILES_TEXTS.unsaved_title} onClose={onStay} closable={!busy}>
      <p>{filesFmt("unsaved_body", { what })}</p>
      <div className="button-row">
        <button type="button" className="button ghost" data-autofocus="true" onClick={onStay} disabled={busy}>
          {FILES_TEXTS.unsaved_stay}
        </button>
        <button type="button" className="button secondary" onClick={onDiscard} disabled={busy}>
          {FILES_TEXTS.unsaved_discard}
        </button>
        <button type="button" className="button" onClick={onSave} disabled={busy}>
          {FILES_TEXTS.unsaved_save}
        </button>
      </div>
    </StudioModal>
  );
}

// Файл изменили, пока его правили: единственный выход - перечитать (правки уходят в «Мои правки»)
export function ConflictDialog({ onRefresh }: { onRefresh: () => void }) {
  return (
    <StudioModal title={FILES_TEXTS.conflict_title} onClose={onRefresh} closable={false}>
      <p>{FILES_TEXTS.conflict_body}</p>
      <div className="button-row">
        <button type="button" className="button" data-autofocus="true" onClick={onRefresh}>
          {FILES_TEXTS.conflict_refresh}
        </button>
      </div>
    </StudioModal>
  );
}

// Буфер «Мои правки»: только просмотр и копирование, автослияния нет
export function MyEditsPanel({ text, onCopy, onClose }: { text: string; onCopy: () => void; onClose: () => void }) {
  return (
    <section className="sb-files-myedits" aria-label={FILES_TEXTS.my_edits_title}>
      <h3 className="sb-h3">{FILES_TEXTS.my_edits_title}</h3>
      <p className="sb-muted">{FILES_TEXTS.my_edits_hint}</p>
      <textarea className="sb-files-json-text" readOnly value={text} aria-label={`${FILES_TEXTS.my_edits_title}: JSON`} spellCheck={false} />
      <div className="button-row">
        <button type="button" className="button secondary" onClick={onCopy}>
          {FILES_TEXTS.my_edits_copy}
        </button>
        <button type="button" className="button ghost" onClick={onClose}>
          {FILES_TEXTS.my_edits_close}
        </button>
      </div>
    </section>
  );
}

export type ValidateAllState =
  | { phase: "idle" }
  | { phase: "running" }
  | { phase: "done"; entries: ValidateAllEntry[]; summary: ValidateAllSummary };

// Итог «Проверить все файлы»: сводка и файлы с замечаниями (можно открыть)
export function ValidateAllDialog({ state, onOpen, onClose }: {
  state: ValidateAllState;
  onOpen: (key: string) => void;
  onClose: () => void;
}) {
  return (
    <StudioModal title={FILES_TEXTS.validate_all_title} onClose={onClose} closable={state.phase !== "running"} wide>
      {state.phase === "running" ? <p role="status">{FILES_TEXTS.validate_all_running}</p> : null}
      {state.phase === "done" ? (
        state.summary.checked === 0 ? (
          <p>{FILES_TEXTS.validate_all_none}</p>
        ) : (
          <>
            <p role="status">{validateAllText(state.summary)}</p>
            {state.summary.problems.length > 0 ? (
              <ul className="sb-files-problems">
                {state.summary.problems.map((e) => (
                  <li key={e.key}>
                    <strong>{e.title}</strong>{" "}
                    {e.error ? (
                      <span className="sb-tone-danger">{filesFmt("validate_all_failed", { reason: e.error.replace(/\.$/, "") })}</span>
                    ) : e.counts ? (
                      <span className={e.counts.errors > 0 ? "sb-tone-danger" : "sb-muted"}>
                        {filesFmt("counts", { errors: e.counts.errors, warnings: e.counts.warnings })}
                      </span>
                    ) : null}{" "}
                    <button type="button" className="button ghost" onClick={() => onOpen(e.key)}>
                      {FILES_TEXTS.validate_all_open}
                    </button>
                  </li>
                ))}
              </ul>
            ) : null}
          </>
        )
      ) : null}
      <div className="button-row">
        <button type="button" className="button ghost" data-autofocus="true" onClick={onClose} disabled={state.phase === "running"}>
          {FILES_TEXTS.close}
        </button>
      </div>
    </StudioModal>
  );
}
