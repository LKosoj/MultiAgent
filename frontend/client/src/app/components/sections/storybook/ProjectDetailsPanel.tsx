"use client";

import React from "react";

import { formatBytes, formatDateTimeAny } from "../../../lib/storybook/format";
import type { StorybookClient } from "../../../lib/storybook/client";
import type { ProjectCard } from "../../../lib/storybook/projects";
import { canDelete, canRename } from "../../../lib/storybook/projectsView";
import { T } from "../../../lib/storybook/texts";
import type { ProjectExtra } from "../../../lib/storybook/useProjects";
import type { ExportInfo, JobKind } from "../../../lib/storybook/useProjectActions";

type Props = {
  card: ProjectCard | null;
  extra: ProjectExtra | undefined;
  sizeBytes: number | undefined;
  client: StorybookClient;
  active: Partial<Record<JobKind, string>>;
  lastExport: ExportInfo | null;
  onOpen: (id: string) => void;
  onDuplicate: (id: string) => void;
  onRename: (id: string) => void;
  onBackups: (id: string) => void;
  onExport: (id: string) => void;
  onRemove: (id: string) => void;
};

export function ProjectDetailsPanel(p: Props) {
  const { card, extra } = p;
  if (!card) {
    return (
      <aside className="sb-side" aria-label={T.selectedProject}>
        <h3 className="sb-h3">{T.selectedProject}</h3>
        <p className="sb-muted">{T.selectedNone}</p>
      </aside>
    );
  }
  const gate = canDelete(card);
  const d = extra?.details;
  const exportLink =
    p.lastExport && p.lastExport.projectId === card.project_id ? p.client.mediaUrl(card.project_id, p.lastExport.path) : null;
  const gated = (allowed: boolean, reason: string | null, action: () => void) => ({
    "aria-disabled": allowed ? undefined : true,
    title: allowed ? undefined : (reason ?? undefined),
    onClick: allowed ? action : undefined,
  });

  return (
    <aside className="sb-side" aria-label={T.selectedProject}>
      <h3 className="sb-h3">{card.title}</h3>
      <dl className="sb-kv">
        <dt>{T.fId}</dt>
        <dd>{card.project_id}</dd>
        <dt>{T.fDescription}</dt>
        <dd>{d?.description || card.description || "—"}</dd>
        <dt>Серий</dt>
        <dd>{card.series_count}</dd>
        <dt>Общие герои</dt>
        <dd>{Object.keys(d?.resources?.characters ?? {}).join(", ") || "—"}</dd>
        <dt>Общие места</dt>
        <dd>{Object.keys(d?.resources?.locations ?? {}).join(", ") || "—"}</dd>
        <dt>{T.fModified}</dt>
        <dd>{formatDateTimeAny(card.modified)}</dd>
        <dt>{T.backupSize}</dt>
        <dd>{p.sizeBytes === undefined ? "—" : formatBytes(p.sizeBytes)}</dd>
      </dl>
      <div className="sb-actions">
        <button type="button" className="button" onClick={() => p.onOpen(card.project_id)}>
          {T.open}
        </button>
        <button type="button" className="button secondary" onClick={() => p.onDuplicate(card.project_id)} disabled={"duplicate" in p.active}>
          {"duplicate" in p.active ? p.active.duplicate || "…" : T.duplicate}
        </button>
        <button
          type="button"
          className={`button secondary${gate.allowed ? "" : " sb-button-disabled"}`}
          {...gated(canRename(card).allowed, canRename(card).reason, () => p.onRename(card.project_id))}
        >
          {T.rename}
        </button>
        <button type="button" className="button secondary" onClick={() => p.onBackups(card.project_id)}>
          {T.backups}
        </button>
        <button type="button" className="button secondary" onClick={() => p.onExport(card.project_id)} disabled={"export" in p.active}>
          {"export" in p.active ? p.active.export || "…" : T.exportZip}
        </button>
        {p.lastExport && p.lastExport.projectId === card.project_id ? (
          exportLink ? (
            <a className="button ghost" href={exportLink} download={p.lastExport.filename}>
              {T.download}: {p.lastExport.filename}
            </a>
          ) : (
            <p className="sb-muted">Файл на сервере: {p.lastExport.path}</p>
          )
        ) : null}
        <button
          type="button"
          className={`button ghost${gate.allowed ? "" : " sb-button-disabled"}`}
          {...gated(gate.allowed, gate.reason, () => p.onRemove(card.project_id))}
        >
          {"delete" in p.active ? p.active.delete || "…" : T.remove}
        </button>
      </div>
    </aside>
  );
}
