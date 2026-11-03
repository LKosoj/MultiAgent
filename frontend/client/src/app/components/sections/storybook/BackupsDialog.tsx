"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";

import { formatBytes, formatDateTimeAny } from "../../../lib/storybook/format";
import { createLatestGate } from "../../../lib/storybook/latestGate";
import type { ProjectBackup } from "../../../lib/storybook/projects";
import { T, restoreTitle } from "../../../lib/storybook/texts";
import type { Outcome } from "../../../lib/storybook/useProjectActions";
import { Banner } from "./Banner";
import { ConfirmDialog } from "./ConfirmDialog";
import { StudioModal } from "./StudioModal";

type Props = {
  // проект, с которого открыли окно; null - «все проекты» (удалённый проект иначе не восстановить)
  projectId: string | null;
  loadBackups: (forProjectId?: string) => Promise<Outcome<ProjectBackup[]>>;
  createBackup: (projectId: string, note: string) => Promise<Outcome<{ consistent: boolean }>>;
  restoreBackup: (projectId: string, backupId: string) => Promise<Outcome<void>>;
  busyKinds: { create: boolean; restore: boolean };
  onClose: () => void;
};

export function BackupsDialog({ projectId, loadBackups, createBackup, restoreBackup, busyKinds, onClose }: Props) {
  const [all, setAll] = useState(projectId === null);
  const [items, setItems] = useState<ProjectBackup[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [warn, setWarn] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [confirm, setConfirm] = useState<ProjectBackup | null>(null);
  const [restoreError, setRestoreError] = useState<string | null>(null);

  const gate = useRef(createLatestGate());

  const reload = useCallback(async () => {
    const token = gate.current.next();
    setItems(null);
    const out = await loadBackups(all ? undefined : (projectId ?? undefined));
    if (!gate.current.isCurrent(token)) return; // пришёл устаревший ответ
    if (out.ok) {
      setItems(out.value);
      setError(null);
    } else {
      setItems([]);
      setError(out.error.text);
    }
  }, [all, loadBackups, projectId]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const doCreate = async () => {
    if (!projectId) return;
    setWarn(null);
    const out = await createBackup(projectId, note);
    if (!out.ok) return setError(out.error.text);
    if (!out.value.consistent) setWarn(T.backupInconsistent);
    setNote("");
    void reload();
  };

  const doRestore = async () => {
    if (!confirm) return;
    setRestoreError(null);
    const out = await restoreBackup(confirm.project_id, confirm.backup_id);
    if (out.ok) {
      setConfirm(null);
      onClose();
    } else setRestoreError(out.error.text);
  };

  if (confirm) {
    return (
      <ConfirmDialog
        title={restoreTitle(formatDateTimeAny(confirm.created_at))}
        body={T.restoreBody}
        confirmLabel={T.restoreConfirm}
        busy={busyKinds.restore}
        error={restoreError}
        onConfirm={() => void doRestore()}
        onCancel={() => setConfirm(null)}
      />
    );
  }

  return (
    <StudioModal title={T.backupsTitle} onClose={onClose} wide>
      {projectId ? (
        <label className="toggle">
          <input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} data-autofocus="true" />
          <span>{T.backupsAllProjects}</span>
        </label>
      ) : null}
      {error ? <Banner tone="error">{error}</Banner> : null}
      {warn ? <Banner tone="warn">{warn}</Banner> : null}
      {items === null ? (
        <p className="sb-muted">{T.loadingProjects}</p>
      ) : items.length === 0 ? (
        <p className="sb-muted">{T.backupsEmpty}</p>
      ) : (
        <table className="sb-table">
          <thead>
            <tr>
              <th>{T.backupDate}</th>
              {all ? <th>{T.backupProject}</th> : null}
              <th>{T.backupSize}</th>
              <th>{T.backupReason}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {items.map((b) => (
              <tr key={`${b.project_id}/${b.backup_id}`}>
                <td>{formatDateTimeAny(b.created_at)}</td>
                {all ? <td>{b.project_id}</td> : null}
                <td>{formatBytes(b.size_bytes)}</td>
                <td>
                  {b.reason_title}
                  {b.note ? ` · ${b.note}` : ""}
                </td>
                <td>
                  <button type="button" className="button secondary" style={{ minWidth: 0, height: 30 }} onClick={() => setConfirm(b)}>
                    {T.backupRestore}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {projectId ? (
        <div className="sb-row">
          <input
            style={{ flex: "1 1 200px" }}
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder={T.backupNote}
            aria-label={T.backupNote}
          />
          <button type="button" className="button" onClick={() => void doCreate()} disabled={busyKinds.create}>
            {T.backupCreate}
          </button>
        </div>
      ) : null}
    </StudioModal>
  );
}
