"use client";

import { resolveScreenKey } from "../../../../lib/storybook/screenKeys";
import React, { useEffect, useRef, useState } from "react";

import { formatDateTimeRu } from "../../../../lib/storybook/format";
import { FILES_TEXTS, filesFmt } from "../../../../lib/storybook/filesTexts";
import {
  ancestorPointers,
  canSaveAnyway,
  cursorForFinding,
  editorAccess,
  editorBanners,
  errorCount,
  resolveEditorScreen,
} from "../../../../lib/storybook/filesView";
import type { Finding } from "../../../../lib/storybook/artifacts";
import type { ArtifactsApi } from "../../../../lib/storybook/useArtifacts";
import type { ArtifactEditorApi } from "../../../../lib/storybook/useArtifactEditor";
import { Banner } from "../Banner";
import { ConfirmDialog } from "../ConfirmDialog";
import { ArtifactForm } from "./ArtifactForm";
import { ArtifactList } from "./ArtifactList";
import { ConflictDialog, MyEditsPanel, UnsavedDialog, ValidateAllDialog, type ValidateAllState } from "./FilesDialogs";
import { FindingsPanel } from "./FindingsPanel";
import { HistoryPanel } from "./HistoryPanel";
import { JsonEditor } from "./JsonEditor";

type Props = {
  list: ArtifactsApi;
  ed: ArtifactEditorApi;
  validateAll: { state: ValidateAllState; run: () => void; close: () => void };
  say: (text: string, type?: "error" | "success" | "info") => void;
};

type Confirm =
  | { kind: "anyway" }
  | { kind: "restore"; id: string; date: string }
  | { kind: "restore_invalid"; id: string }
  | null;

// Экран «Файлы»: список слева, редактор (форма или JSON) справа; при ширине от 1600 форма и JSON рядом
export function FilesTab({ list, ed, validateAll, say }: Props) {
  const { editor, state } = ed;
  const rootRef = useRef<HTMLDivElement>(null);
  const [confirm, setConfirm] = useState<Confirm>(null);
  const [jump, setJump] = useState<{ offset: number; n: number } | null>(null);
  const [focusPointer, setFocusPointer] = useState<string | null>(null);
  const [forceOpen, setForceOpen] = useState<ReadonlySet<string>>(new Set());
  const [busyConfirm, setBusyConfirm] = useState(false);

  const read = state.read;
  const screen = resolveEditorScreen({ key: state.key, phase: state.phase, read });
  const viewing = state.versionRead !== null;
  const access = read
    ? editorAccess({ read, viewingVersion: viewing, forbidden: state.forbidden, dirty: state.dirty, saving: state.saving })
    : { readOnly: true, canSave: false };

  const edRef = useRef({ editor, state, list });
  edRef.current = { editor, state, list };
  useEffect(() => {
    const root = rootRef.current;
    if (!root) return undefined;
    const onKey = (e: KeyboardEvent) => {
      const r = resolveScreenKey(e, root.querySelector("[data-sb-modal]") !== null);
      if (!r.swallow) return;
      e.preventDefault();
      e.stopPropagation();
      const cur = edRef.current;
      if (r.action === "save") {
        if (cur.state.dirty && !cur.state.saving && !cur.state.versionRead) void cur.editor.save();
      } else if (r.action === "reload" && !cur.state.dirty) {
        cur.list.reload();
        void cur.editor.reload();
      }
    };
    root.addEventListener("keydown", onKey);
    return () => root.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    if (focusPointer === null) return undefined;
    const t = setTimeout(() => setFocusPointer(null), 60);
    return () => clearTimeout(t);
  }, [focusPointer]);

  const titleOf = (key: string | null) => list.items.find((i) => i.artifact_key === key)?.title ?? key ?? "";
  const item = list.items.find((i) => i.artifact_key === state.key);
  const banners = read
    ? editorBanners({
        read: viewing && state.versionRead ? { ...read, parse_error: null } : read,
        forbidden: state.forbidden,
        versionDate: state.versionRead ? formatDateTimeRu(state.versionDate) : null,
      })
    : [];

  const pick = (f: Finding) => {
    if (state.mode === "form" && state.fields) {
      setForceOpen(new Set(ancestorPointers(f.pointer)));
      setFocusPointer(f.pointer);
    } else {
      const offset = cursorForFinding(f, state.text);
      if (offset !== null) setJump({ offset, n: (jump?.n ?? 0) + 1 });
    }
  };

  const problem = state.saveProblem;
  const anywayAllowed = problem ? canSaveAnyway(problem.errors) : false;

  const runConfirm = async () => {
    if (!confirm) return;
    setBusyConfirm(true);
    try {
      if (confirm.kind === "anyway") {
        await editor.save({ allowInvalid: true });
        setConfirm(null);
      } else if (confirm.kind === "restore") {
        const res = await editor.restore(confirm.id);
        setConfirm(res === "invalid" ? { kind: "restore_invalid", id: confirm.id } : null);
      } else {
        await editor.restore(confirm.id, { allowInvalid: true });
        setConfirm(null);
      }
    } finally {
      setBusyConfirm(false);
    }
  };

  const copyMyEdits = async () => {
    try {
      await navigator.clipboard.writeText(state.myEdits ?? "");
      say(FILES_TEXTS.toast_copied, "success");
    } catch {
      // буфер обмена недоступен: текст можно выделить и скопировать вручную
    }
  };

  const jsonText = viewing ? (state.versionRead?.text ?? "") : state.text;
  const formAvailable = state.fields !== null;
  const wideBoth = state.mode === "form" && formAvailable && !viewing;
  const errorsTotal = errorCount(state.findings);


  return (
    <div className="sb-files" ref={rootRef}>
      <div className="sb-row">
        <h2 className="sb-title">{FILES_TEXTS.title}</h2>
        <button type="button" className="button secondary" onClick={validateAll.run} disabled={list.phase !== "ready"}>
          {FILES_TEXTS.validate_all}
        </button>
      </div>
      {list.phase === "loading" ? <p className="sb-muted" role="status">{FILES_TEXTS.loading}</p> : null}
      {list.phase === "error" ? (
        <Banner
          tone="error"
          actions={<button type="button" className="button secondary" onClick={list.reload}>{FILES_TEXTS.retry}</button>}
        >
          {filesFmt("list_error", { reason: (list.error ?? "").replace(/\.$/, "") })}
        </Banner>
      ) : null}
      {list.phase === "ready" ? (
        <div className="sb-files-body">
          <ArtifactList items={list.items} selected={state.key} onPick={(k) => void editor.requestOpen(k)} />
          <div className="sb-files-editor">
            {screen === "empty" ? <p className="sb-empty">{FILES_TEXTS.empty}</p> : null}
            {screen === "loading" ? <p className="sb-muted" role="status">{FILES_TEXTS.loading_file}</p> : null}
            {screen === "error" ? (
              <Banner
                tone="error"
                actions={<button type="button" className="button secondary" onClick={() => void editor.reload()}>{FILES_TEXTS.retry}</button>}
              >
                {filesFmt("read_error", { reason: (state.error ?? "").replace(/\.$/, "") })}
              </Banner>
            ) : null}
            {screen === "missing" ? (
              <p className="sb-empty">
                {filesFmt("missing_file", { name: item?.produced_by || item?.writers[0]?.title || titleOf(state.key) })}
              </p>
            ) : null}
            {screen === "ready" && read ? (
              <>
                <div className="sb-files-head">
                  <h3 className="sb-h3">
                    {filesFmt("title_file", { title: read.title, path: read.path })}
                    {state.dirty ? <span className="sb-muted"> ({FILES_TEXTS.dirty})</span> : null}
                  </h3>
                  <div className="button-row">
                    <div className="sb-files-mode" role="group" aria-label={FILES_TEXTS.mode_form + " / " + FILES_TEXTS.mode_json}>
                      <button
                        type="button"
                        className="button secondary"
                        aria-pressed={state.mode === "form"}
                        disabled={viewing}
                        onClick={() => void editor.setMode("form")}
                      >
                        {FILES_TEXTS.mode_form}
                      </button>
                      <button
                        type="button"
                        className="button secondary"
                        aria-pressed={state.mode === "json"}
                        disabled={viewing}
                        onClick={() => void editor.setMode("json")}
                      >
                        {FILES_TEXTS.mode_json}
                      </button>
                    </div>
                    <button type="button" className="button secondary" onClick={() => void editor.validate()} disabled={state.validating || viewing}>
                      {FILES_TEXTS.validate}
                    </button>
                    <button type="button" className="button" onClick={() => void editor.save()} disabled={!access.canSave}>
                      {state.saving ? FILES_TEXTS.saving : FILES_TEXTS.save}
                    </button>
                    <button type="button" className="button ghost" onClick={() => void editor.reload()} disabled={state.saving}>
                      {FILES_TEXTS.reload}
                    </button>
                    <button type="button" className="button ghost" onClick={() => void editor.openHistory()}>
                      {FILES_TEXTS.history}
                    </button>
                  </div>
                </div>
                {banners.map((b, i) => (
                  <Banner
                    key={i}
                    tone={b.tone === "error" ? "error" : b.tone === "warn" ? "warn" : "info"}
                    actions={
                      viewing && b.tone === "info" ? (
                        <button type="button" className="button secondary" onClick={editor.leaveVersion}>
                          {FILES_TEXTS.back_to_current}
                        </button>
                      ) : undefined
                    }
                  >
                    {b.text}
                  </Banner>
                ))}
                {state.draftRestored ? <Banner tone="info">{FILES_TEXTS.draft_restored}</Banner> : null}
                {state.mergedNote ? <Banner tone="info">{FILES_TEXTS.merged_note}</Banner> : null}
                {state.modeBlock ? <Banner tone="warn">{state.modeBlock}</Banner> : null}
                {problem ? (
                  <Banner
                    tone="error"
                    actions={
                      anywayAllowed && errorsTotal > 0 ? (
                        <button type="button" className="button secondary" onClick={() => setConfirm({ kind: "anyway" })}>
                          {FILES_TEXTS.save_anyway}
                        </button>
                      ) : undefined
                    }
                  >
                    {problem.text}
                  </Banner>
                ) : null}
                {state.myEdits !== null ? (
                  <MyEditsPanel text={state.myEdits} onCopy={() => void copyMyEdits()} onClose={editor.closeMyEdits} />
                ) : null}
                <div className="sb-files-work" data-both={wideBoth ? "true" : undefined}>
                  {state.mode === "form" && state.fields && !viewing ? (
                    <div className="sb-files-pane">
                      <p className="sb-muted">{FILES_TEXTS.form_array_hint}</p>
                      <ArtifactForm
                        fields={state.fields}
                        content={state.content}
                        readOnly={access.readOnly}
                        findings={state.findings}
                        forceOpen={forceOpen}
                        focusPointer={focusPointer}
                        onEdit={(field, input) => editor.editField(field, input)}
                      />
                    </div>
                  ) : null}
                  {state.mode === "json" || viewing ? (
                    <div className="sb-files-pane">
                      <JsonEditor text={jsonText} readOnly={access.readOnly} onChange={editor.setText} jump={jump} />
                    </div>
                  ) : null}
                  {wideBoth ? (
                    <div className="sb-files-pane sb-files-pane-json">
                      <JsonEditor text={JSON.stringify(state.content, null, 2)} readOnly jump={null} label={FILES_TEXTS.json_preview} onChange={() => undefined} />
                    </div>
                  ) : null}
                </div>
                <FindingsPanel findings={state.findings} counts={state.counts} truncated={state.truncated} onPick={pick} />
                {state.history.open ? (
                  <HistoryPanel
                    history={state.history}
                    diff={state.diff}
                    canRestore={!state.forbidden && read.editable && read.run_state !== "busy"}
                    onClose={editor.closeHistory}
                    onView={(id) => void editor.viewVersion(id)}
                    onCompare={(id) => void editor.compareWith(id)}
                    onRestore={(id, date) => setConfirm({ kind: "restore", id, date })}
                  />
                ) : null}
              </>
            ) : null}
          </div>
        </div>
      ) : null}
      {state.pendingKey ? (
        <UnsavedDialog
          what={titleOf(state.key)}
          busy={state.saving}
          onSave={() => void editor.resolvePending("save")}
          onDiscard={() => void editor.resolvePending("discard")}
          onStay={() => void editor.resolvePending("stay")}
        />
      ) : null}
      {state.conflict ? <ConflictDialog onRefresh={() => void editor.refreshAfterConflict()} /> : null}
      {confirm?.kind === "anyway" ? (
        <ConfirmDialog
          title={FILES_TEXTS.save_anyway_title}
          body={filesFmt("save_anyway_body", { errors: errorsTotal })}
          confirmLabel={FILES_TEXTS.save_anyway_go}
          cancelLabel={FILES_TEXTS.cancel}
          busy={busyConfirm}
          onConfirm={() => void runConfirm()}
          onCancel={() => setConfirm(null)}
        />
      ) : null}
      {confirm?.kind === "restore" ? (
        <ConfirmDialog
          title={filesFmt("restore_title", { date: confirm.date })}
          body={FILES_TEXTS.restore_body}
          confirmLabel={FILES_TEXTS.restore_go}
          cancelLabel={FILES_TEXTS.cancel}
          busy={busyConfirm}
          onConfirm={() => void runConfirm()}
          onCancel={() => setConfirm(null)}
        />
      ) : null}
      {confirm?.kind === "restore_invalid" ? (
        <ConfirmDialog
          title={FILES_TEXTS.restore_invalid_title}
          body={FILES_TEXTS.restore_invalid_body}
          confirmLabel={FILES_TEXTS.restore_invalid_go}
          cancelLabel={FILES_TEXTS.cancel}
          busy={busyConfirm}
          onConfirm={() => void runConfirm()}
          onCancel={() => setConfirm(null)}
        />
      ) : null}
      {validateAll.state.phase !== "idle" ? (
        <ValidateAllDialog
          state={validateAll.state}
          onOpen={(key) => {
            validateAll.close();
            void editor.requestOpen(key);
          }}
          onClose={validateAll.close}
        />
      ) : null}
    </div>
  );
}
