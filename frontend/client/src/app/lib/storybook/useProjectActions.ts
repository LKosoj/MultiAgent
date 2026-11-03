"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import type { StorybookClient } from "./client";
import { formatBytes } from "./format";
import {
  projectsBackupCreate,
  projectsBackupRestore,
  projectsBackupsList,
  projectsCreate,
  projectsCreateFromPrompt,
  projectsDelete,
  projectsDuplicate,
  projectsExport,
  projectsRename,
  projectsSuggestId,
  waitForProjectJob,
  type BackupJobResult,
  type CreateFromPromptJobResult,
  type DeleteJobResult,
  type DuplicateJobResult,
  type ExportJobResult,
  type JobProgress,
  type ProjectBackup,
  type ProjectsCreateFromPromptParams,
  type ProjectsCreateParams,
  type ProjectsSuggestIdResult,
  type RestoreJobResult,
} from "./projects";
import { toasts } from "./texts";
import { classifyStorybookError, type ClassifiedError } from "./uiErrors";

export type JobKind = "create_from_prompt" | "duplicate" | "delete" | "backup_create" | "backup_restore" | "export";

export type Outcome<T> = { ok: true; value: T } | { ok: false; error: ClassifiedError };

export type ExportInfo = { projectId: string; filename: string; path: string; sizeBytes: number };

type StoredJob = { jobId: string; kind: JobKind; projectId: string | null };

export const JOBS_STORAGE_KEY = "storybook-studio:jobs:v1";

const readJobs = (): StoredJob[] => {
  try {
    const raw = window.sessionStorage.getItem(JOBS_STORAGE_KEY);
    const list = raw ? JSON.parse(raw) : [];
    return Array.isArray(list)
      ? list.filter((j) => j && typeof j.jobId === "string" && typeof j.kind === "string")
      : [];
  } catch {
    return [];
  }
};

const writeJobs = (jobs: StoredJob[]) => {
  try {
    window.sessionStorage.setItem(JOBS_STORAGE_KEY, JSON.stringify(jobs));
  } catch {
    // хранилище недоступно: потеряем нить задания после перезагрузки, не более
  }
};

const mapOk = <A, B>(out: Outcome<A>, f: (v: A) => B): Outcome<B> =>
  out.ok ? { ok: true, value: f(out.value) } : out;

type Args = {
  client: StorybookClient;
  runUser: <R>(task: () => Promise<R>) => Promise<R>;
  enabled: boolean;
  notifySuccess: (message: string) => void;
  onProjectsChanged: () => void | Promise<void>;
  onOpenProject: (projectId: string) => void;
  onSelectProject: (projectId: string) => void;
  onProjectRemoved: (projectId: string) => void;
};

export type ProjectActionsApi = {
  active: Partial<Record<JobKind, string>>; // kind -> строка прогресса
  bannerError: ClassifiedError | null;
  clearBanner: () => void;
  reportError: (error: ClassifiedError) => void;
  lastExport: ExportInfo | null;
  createByForm: (p: ProjectsCreateParams) => Promise<Outcome<string>>;
  createFromPrompt: (p: ProjectsCreateFromPromptParams) => Promise<Outcome<string>>;
  duplicate: (projectId: string) => Promise<Outcome<string>>;
  rename: (projectId: string, title: string) => Promise<Outcome<void>>;
  remove: (projectId: string) => Promise<Outcome<void>>;
  loadBackups: (forProjectId?: string) => Promise<Outcome<ProjectBackup[]>>;
  createBackup: (projectId: string, note: string) => Promise<Outcome<{ consistent: boolean }>>;
  restoreBackup: (projectId: string, backupId: string) => Promise<Outcome<void>>;
  exportZip: (projectId: string) => Promise<Outcome<ExportInfo>>;
  suggestId: (hint: string) => Promise<Outcome<ProjectsSuggestIdResult>>;
};

export const useProjectActions = (args: Args): ProjectActionsApi => {
  const { client, runUser, enabled } = args;
  const argsRef = useRef(args);
  useEffect(() => {
    argsRef.current = args;
  });

  const [active, setActive] = useState<Partial<Record<JobKind, string>>>({});
  const [bannerError, setBannerError] = useState<ClassifiedError | null>(null);
  const [lastExport, setLastExport] = useState<ExportInfo | null>(null);
  const controller = useRef<AbortController | null>(null);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    controller.current = new AbortController();
    return () => {
      alive.current = false;
      controller.current?.abort(); // нить задания остаётся в sessionStorage
    };
  }, []);

  const attempt = useCallback(
    async <R,>(task: () => Promise<R>): Promise<Outcome<R>> => {
      try {
        return { ok: true, value: await runUser(task) };
      } catch (err) {
        return { ok: false, error: classifyStorybookError(err) };
      }
    },
    [runUser],
  );

  // Общий конец заданий: тосты, обновление списка, переходы. Один путь и для живых, и для «подобранных» заданий.
  const finish = useCallback(async (kind: JobKind, result: unknown, projectId: string | null) => {
    const a = argsRef.current;
    switch (kind) {
      case "create_from_prompt": {
        const r = result as CreateFromPromptJobResult;
        await a.onProjectsChanged();
        a.notifySuccess(toasts.created);
        a.onOpenProject(r.project_id);
        break;
      }
      case "duplicate": {
        const r = result as DuplicateJobResult;
        await a.onProjectsChanged();
        a.notifySuccess(toasts.duplicated);
        a.onSelectProject(r.new_project_id);
        break;
      }
      case "delete": {
        const r = result as DeleteJobResult;
        a.notifySuccess(toasts.removed(formatBytes(r.backup_size_bytes)));
        if (projectId) a.onProjectRemoved(projectId);
        await a.onProjectsChanged();
        break;
      }
      case "backup_create":
        a.notifySuccess(toasts.backupCreated);
        break;
      case "backup_restore": {
        const r = result as RestoreJobResult;
        await a.onProjectsChanged();
        a.notifySuccess(toasts.backupRestored);
        a.onSelectProject(r.project_id);
        break;
      }
      case "export": {
        const r = result as ExportJobResult;
        if (projectId) {
          try {
            await client.ensureMediaToken(projectId);
          } catch {
            // ссылка не построится, покажем путь на сервере
          }
          setLastExport({ projectId, filename: r.filename, path: r.path, sizeBytes: r.size_bytes });
        }
        a.notifySuccess(toasts.exported(r.filename));
        break;
      }
    }
  }, [client]);

  const track = useCallback(
    async <R,>(kind: JobKind, jobId: string, projectId: string | null): Promise<Outcome<R>> => {
      writeJobs([...readJobs().filter((j) => j.jobId !== jobId), { jobId, kind, projectId }]);
      setActive((prev) => ({ ...prev, [kind]: "" }));
      const onProgress = (p: JobProgress) => {
        if (alive.current) setActive((prev) => (kind in prev ? { ...prev, [kind]: p.message } : prev));
      };
      try {
        const result = await waitForProjectJob<R>(client, jobId, {
          onProgress,
          signal: controller.current?.signal,
        });
        writeJobs(readJobs().filter((j) => j.jobId !== jobId));
        await finish(kind, result, projectId);
        return { ok: true, value: result };
      } catch (err) {
        const classified = classifyStorybookError(err);
        // отмену из-за размонтирования не считаем завершением: задание подберётся при возвращении
        if (classified.kind !== "aborted") writeJobs(readJobs().filter((j) => j.jobId !== jobId));
        return { ok: false, error: classified };
      } finally {
        if (alive.current) {
          setActive((prev) => {
            const next = { ...prev };
            delete next[kind];
            return next;
          });
        }
      }
    },
    [client, finish],
  );

  // Страницу перезагрузили, пока задание шло: подбираем его заново
  const resumed = useRef(false);
  useEffect(() => {
    if (!enabled || resumed.current) return;
    resumed.current = true;
    for (const job of readJobs()) {
      void track(job.kind, job.jobId, job.projectId).then((out) => {
        if (!out.ok && out.error.kind !== "aborted" && alive.current) setBannerError(out.error);
      });
    }
  }, [enabled, track]);

  const asJob = useCallback(
    async <S extends { job_id: string }, R>(
      kind: JobKind,
      projectId: string | null,
      start: () => Promise<S>,
    ): Promise<Outcome<{ started: S; result: R }>> => {
      const started = await attempt(start);
      if (!started.ok) return started;
      const done = await track<R>(kind, started.value.job_id, projectId);
      return done.ok ? { ok: true, value: { started: started.value, result: done.value } } : done;
    },
    [attempt, track],
  );

  return {
    active,
    bannerError,
    clearBanner: useCallback(() => setBannerError(null), []),
    reportError: useCallback((error) => setBannerError(error), []),
    lastExport,

    createByForm: useCallback(
      async (p) => {
        const out = await attempt(() => projectsCreate(client, p));
        if (out.ok) {
          await argsRef.current.onProjectsChanged();
          argsRef.current.notifySuccess(toasts.created);
          argsRef.current.onOpenProject(out.value.project_id);
        }
        return mapOk(out, (v) => v.project_id);
      },
      [attempt, client],
    ),

    createFromPrompt: useCallback(
      async (p) => {
        const out = await asJob<{ job_id: string; project_id: string }, CreateFromPromptJobResult>(
          "create_from_prompt",
          null,
          () => projectsCreateFromPrompt(client, p),
        );
        return mapOk(out, (v) => v.result.project_id);
      },
      [asJob, client],
    ),

    duplicate: useCallback(
      async (projectId) => {
        const out = await asJob<{ job_id: string }, DuplicateJobResult>("duplicate", projectId, () =>
          projectsDuplicate(client, { project_id: projectId }),
        );
        return mapOk(out, (v) => v.result.new_project_id);
      },
      [asJob, client],
    ),

    rename: useCallback(
      async (projectId, title) => {
        const out = await attempt(() => projectsRename(client, { project_id: projectId, title }));
        if (out.ok) {
          await argsRef.current.onProjectsChanged();
          argsRef.current.notifySuccess(toasts.renamed);
        }
        return mapOk(out, () => undefined);
      },
      [attempt, client],
    ),

    remove: useCallback(
      async (projectId) => {
        const out = await asJob<{ job_id: string }, DeleteJobResult>("delete", projectId, () =>
          projectsDelete(client, { project_id: projectId, confirm: true }),
        );
        return mapOk(out, () => undefined);
      },
      [asJob, client],
    ),

    loadBackups: useCallback(
      async (forProjectId) => {
        const out = await attempt(() =>
          projectsBackupsList(client, forProjectId ? { for_project_id: forProjectId } : undefined),
        );
        return mapOk(out, (v) => v.backups);
      },
      [attempt, client],
    ),

    createBackup: useCallback(
      async (projectId, note) => {
        const out = await asJob<{ job_id: string; consistent: boolean }, BackupJobResult>(
          "backup_create",
          projectId,
          () => projectsBackupCreate(client, { project_id: projectId, ...(note.trim() ? { note: note.trim() } : {}) }),
        );
        return mapOk(out, (v) => ({ consistent: v.result.consistent ?? v.started.consistent }));
      },
      [asJob, client],
    ),

    restoreBackup: useCallback(
      async (projectId, backupId) => {
        const out = await asJob<{ job_id: string }, RestoreJobResult>("backup_restore", projectId, () =>
          projectsBackupRestore(client, { project_id: projectId, backup_id: backupId, confirm: true }),
        );
        return mapOk(out, () => undefined);
      },
      [asJob, client],
    ),

    exportZip: useCallback(
      async (projectId) => {
        const out = await asJob<{ job_id: string }, ExportJobResult>("export", projectId, () =>
          projectsExport(client, { project_id: projectId }),
        );
        return mapOk(out, (v) => ({
          projectId,
          filename: v.result.filename,
          path: v.result.path,
          sizeBytes: v.result.size_bytes,
        }));
      },
      [asJob, client],
    ),

    suggestId: useCallback(
      (hint) => attempt(() => projectsSuggestId(client, { id_hint: hint })),
      [attempt, client],
    ),
  };
};
