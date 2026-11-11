"use client";

import React, { useEffect, useMemo, useRef, useState } from "react";

import type { BackgroundQueue } from "../../../lib/storybook/backgroundQueue";
import type { StorybookClient } from "../../../lib/storybook/client";
import { formatBytes } from "../../../lib/storybook/format";
import {
  PROJECT_SORTS,
  canDelete,
  filterProjects,
  isProjectSort,
  pendingSizeCount,
  sortProjects,
  type ProjectSort,
} from "../../../lib/storybook/projectsView";
import { T, deleteBody, deleteTitle, loadProjectsFailed, nothingFound, notFoundFolderText } from "../../../lib/storybook/texts";
import type { ProjectActionsApi } from "../../../lib/storybook/useProjectActions";
import type { ProjectsApi } from "../../../lib/storybook/useProjects";
import { Banner } from "./Banner";
import { BackupsDialog } from "./BackupsDialog";
import { ConfirmDialog } from "./ConfirmDialog";
import { CreateByFormDialog } from "./CreateByFormDialog";
import { NoAccess } from "./NoAccess";
import { ProjectCard } from "./ProjectCard";
import { focusedCardId, gridBaseId } from "../../../lib/storybook/gridFocus";
import { ProjectDetailsPanel } from "./ProjectDetailsPanel";
import { RenameDialog } from "./RenameDialog";
import { Skeleton } from "./Skeleton";

// Команда от горячих клавиш: номер нужен, чтобы повторная команда срабатывала снова
export type ProjectsCommand = { type: "create_project" | "focus_search"; n: number } | null;

type Dialog =
  | { kind: "form" }
  | { kind: "rename"; id: string }
  | { kind: "backups"; id: string | null }
  | { kind: "remove"; id: string };

type Props = {
  projects: ProjectsApi;
  actions: ProjectActionsApi;
  client: StorybookClient;
  background: BackgroundQueue;
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  onOpen: (id: string) => void;
  command: ProjectsCommand;
  onOpenSettings: () => void;
  // «Обновить»/«Повторить» как F5 (при сбое сети пересоздаёт клиент)
  onRefresh?: () => void;
};

export function StorybookProjectsTab({
  projects,
  actions,
  client,
  background,
  selectedId,
  onSelect,
  onOpen,
  command,
  onOpenSettings,
  onRefresh,
}: Props) {
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<ProjectSort>("modified");
  const [dialog, setDialog] = useState<Dialog | null>(null);
  const [removeError, setRemoveError] = useState<string | null>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const gridRef = useRef<HTMLDivElement>(null);
  const handled = useRef(command?.n ?? 0);

  const { state, error, load, loadAllSizes, loadExtra, sizes, extras } = projects;

  // горячие клавиши
  useEffect(() => {
    if (!command || command.n === handled.current) return;
    handled.current = command.n;
    if (command.type === "create_project") setDialog({ kind: "form" });
    else searchRef.current?.focus();
  }, [command]);

  // размеры считаем в фоне, когда список готов
  useEffect(() => {
    if (state === "ready") loadAllSizes();
  }, [state, projects.projects, loadAllSizes]);

  const visible = useMemo(
    () => sortProjects(filterProjects(projects.projects, query), sort, sizes),
    [projects.projects, query, sort, sizes],
  );
  const selected = projects.projects.find((p) => p.project_id === selectedId) ?? null;

  useEffect(() => {
    if (selected) loadExtra(selected.project_id);
  }, [selected, loadExtra]);

  const refreshAll = () => (onRefresh ? onRefresh() : void load());

  const removeTarget = dialog?.kind === "remove" ? projects.projects.find((p) => p.project_id === dialog.id) : null;
  const renameTarget = dialog?.kind === "rename" ? projects.projects.find((p) => p.project_id === dialog.id) : null;

  const moveSelection = (delta: number, baseId: string | null) => {
    if (visible.length === 0) return;
    const idx = Math.max(0, visible.findIndex((p) => p.project_id === baseId));
    const next = visible[Math.min(visible.length - 1, Math.max(0, idx + delta))];
    onSelect(next.project_id);
    gridRef.current?.querySelector<HTMLElement>(`[data-project-id="${CSS.escape(next.project_id)}"]`)?.focus();
  };

  const onGridKeyDown = (ev: React.KeyboardEvent) => {
    const baseId = gridBaseId(focusedCardId(ev.target), selectedId);
    const current = projects.projects.find((p) => p.project_id === baseId) ?? null;
    if (ev.key === "ArrowRight" || ev.key === "ArrowDown") {
      ev.preventDefault();
      moveSelection(1, baseId);
    } else if (ev.key === "ArrowLeft" || ev.key === "ArrowUp") {
      ev.preventDefault();
      moveSelection(-1, baseId);
    } else if (ev.key === "Delete" && current && canDelete(current).allowed) {
      ev.preventDefault();
      onSelect(current.project_id);
      setRemoveError(null);
      setDialog({ kind: "remove", id: current.project_id });
    }
  };

  const doRemove = async () => {
    if (!removeTarget) return;
    setRemoveError(null);
    const out = await actions.remove(removeTarget.project_id);
    if (out.ok) setDialog(null);
    else setRemoveError(out.error.text);
  };

  const doExport = async (id: string) => {
    const out = await actions.exportZip(id);
    if (!out.ok) actions.reportError(out.error);
  };
  const doDuplicate = async (id: string) => {
    const out = await actions.duplicate(id);
    if (!out.ok) actions.reportError(out.error);
  };

  if (state === "error" && error?.kind === "forbidden") return <NoAccess />;

  const { done, total } = pendingSizeCount(projects.projects, sizes);

  return (
    <div className="sb-screen">
      <div className="sb-row" style={{ justifyContent: "space-between" }}>
        <h2 className="sb-title">{T.projectsTitle}</h2>
        <div className="sb-row">
          <button type="button" className="button" onClick={() => setDialog({ kind: "form" })} title="Alt+N">
            {T.createByForm}
          </button>
          <button type="button" className="button ghost" onClick={refreshAll} title="F5">
            {T.refresh}
          </button>
          <button type="button" className="button ghost" onClick={() => setDialog({ kind: "backups", id: null })}>
            {T.allBackups}
          </button>
        </div>
      </div>

      {actions.bannerError ? (
        <Banner
          tone="error"
          actions={
            <button type="button" className="button ghost" style={{ minWidth: 0, height: 30 }} onClick={actions.clearBanner}>
              {T.close}
            </button>
          }
        >
          {actions.bannerError.text}
        </Banner>
      ) : null}
      {state === "ready" && projects.projects.length > 0 && done < total ? (
        <p className="sb-muted" role="status">
          {T.countingSizes}: {done} / {total}
        </p>
      ) : null}

      {state === "loading" || state === "idle" ? (
        <Skeleton label={T.loadingProjects} lines={4} height={28} />
      ) : state === "error" && error ? (
        <Banner
          tone="error"
          actions={
            <>
              <button type="button" className="button secondary" style={{ minWidth: 0, height: 30 }} onClick={refreshAll}>
                {T.retry}
              </button>
              {error.kind === "not_found" ? (
                <button type="button" className="button ghost" style={{ minWidth: 0, height: 30 }} onClick={onOpenSettings}>
                  {T.openSettings}
                </button>
              ) : null}
            </>
          }
        >
          {error.kind === "not_found" ? notFoundFolderText(projects.projectsDir) : loadProjectsFailed(error.text)}
        </Banner>
      ) : (
        <div className="sb-projects">
          <div style={{ display: "grid", gap: 12, minWidth: 0 }}>
            <div className="sb-toolbar">
              <input
                ref={searchRef}
                type="search"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder={T.searchHint}
                aria-label={T.search}
              />
              <label className="sb-row">
                <span className="sb-muted">{T.sort}</span>
                <select value={sort} onChange={(e) => isProjectSort(e.target.value) && setSort(e.target.value)}>
                  {PROJECT_SORTS.map((s) => (
                    <option key={s.value} value={s.value}>
                      {s.label}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            {projects.projects.length === 0 ? (
              <div className="sb-empty">
                <p>{T.emptyProjects}</p>
                <button type="button" className="button" onClick={() => setDialog({ kind: "form" })}>
                  {T.createByForm}
                </button>
              </div>
            ) : visible.length === 0 ? (
              <div className="sb-empty">
                <p>{nothingFound(query)}</p>
                <button type="button" className="button secondary" onClick={() => setQuery("")}>
                  {T.resetSearch}
                </button>
              </div>
            ) : (
              <div ref={gridRef} className="sb-grid" onKeyDown={onGridKeyDown}>
                {visible.map((card) => (
                  <ProjectCard
                    key={card.project_id}
                    card={card}
                    sizeBytes={sizes.get(card.project_id)}
                    selected={card.project_id === selectedId}
                    client={client}
                    background={background}
                    onSelect={onSelect}
                    onOpen={onOpen}
                  />
                ))}
              </div>
            )}
          </div>
          <ProjectDetailsPanel
            card={selected}
            extra={selected ? extras.get(selected.project_id) : undefined}
            sizeBytes={selected ? sizes.get(selected.project_id) : undefined}
            client={client}
            active={actions.active}
            lastExport={actions.lastExport}
            onOpen={onOpen}
            onDuplicate={(id) => void doDuplicate(id)}
            onRename={(id) => setDialog({ kind: "rename", id })}
            onBackups={(id) => setDialog({ kind: "backups", id })}
            onExport={(id) => void doExport(id)}
            onRemove={(id) => {
              setRemoveError(null);
              setDialog({ kind: "remove", id });
            }}
          />
        </div>
      )}

      {dialog?.kind === "form" ? (
        <CreateByFormDialog onSubmit={actions.createByForm} suggestId={actions.suggestId} onClose={() => setDialog(null)} />
      ) : null}
      {renameTarget ? (
        <RenameDialog
          currentTitle={renameTarget.title}
          onSubmit={(title) => actions.rename(renameTarget.project_id, title)}
          onClose={() => setDialog(null)}
        />
      ) : null}
      {dialog?.kind === "backups" ? (
        <BackupsDialog
          projectId={dialog.id}
          loadBackups={actions.loadBackups}
          createBackup={actions.createBackup}
          restoreBackup={actions.restoreBackup}
          busyKinds={{ create: "backup_create" in actions.active, restore: "backup_restore" in actions.active }}
          onClose={() => setDialog(null)}
        />
      ) : null}
      {removeTarget ? (
        <ConfirmDialog
          title={deleteTitle(removeTarget.title)}
          body={deleteBody(sizes.has(removeTarget.project_id) ? formatBytes(sizes.get(removeTarget.project_id)) : "размер уточняется")}
          confirmLabel={T.removeConfirm}
          busy={"delete" in actions.active}
          error={removeError}
          onConfirm={() => void doRemove()}
          onCancel={() => setDialog(null)}
        />
      ) : null}
    </div>
  );
}
