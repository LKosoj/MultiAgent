// Обёртки возможностей модуля projects (проекты storybook).
import { StudioError, type StorybookClient } from "./client";

export type ProjectStage = {
  id: string; // brief ... film, либо empty
  title: string;
  index: number; // 0 - пусто, 1..11 - этапы по порядку
  source: string; // "artifacts": грубая оценка по файлам
};

export type ProjectCard = {
  project_id: string;
  title: string;
  description: string;
  language: string;
  state: "ready" | "creating" | "empty";
  stage: ProjectStage;
  busy: boolean; // идёт прогон
  pages_count: number;
  characters_count: number;
  has_pdf: boolean;
  has_final_video: boolean;
  cover: string | null; // путь внутри проекта для c.thumbUrl()
  created: string; // ISO, локальное время
  modified: string;
};

export type ProjectsListResult = {
  projects_dir: string;
  total: number;
  count: number;
  projects: ProjectCard[];
};

export type ProjectDetails = {
  genre: string;
  target_age: string;
  description_full: string;
  main_characters: string[];
  pages_min: number | null;
  pages_max: number | null;
  shots_total: number | null;
};

export type ProjectsGetResult = { project: ProjectCard; details: ProjectDetails };
export type ProjectsSizeResult = { size_bytes: number; files_count: number; cached: boolean };
export type ProjectsSuggestIdResult = { project_id: string; exists: boolean; changed: boolean };
export type ProjectsCreateResult = { project_id: string; project: ProjectCard };
export type ProjectsCreateFromPromptResult = { project_id: string; job_id: string };
export type ProjectsDuplicateResult = { job_id: string; new_project_id: string };
export type ProjectsRenameResult = { project: ProjectCard; previous_title: string };
export type ProjectJobStart = { job_id: string };

export type BackupReason = "manual" | "before_delete" | "before_restore" | "unknown";

export type ProjectBackup = {
  backup_id: string;
  project_id: string;
  created_at: string;
  size_bytes: number;
  reason: BackupReason;
  reason_title: string;
  note: string;
  project_exists: boolean;
};

export type ProjectsBackupsListResult = { backup_dir: string; backups: ProjectBackup[] };
export type ProjectsBackupCreateStart = { job_id: string; consistent: boolean };

// Результаты фоновых заданий (поле result записи задания)
export type DeleteJobResult = { project_id: string; backup_id: string | null; backup_size_bytes: number };
export type BackupJobResult = { backup_id: string; size_bytes: number; consistent: boolean };
export type RestoreJobResult = {
  project_id: string;
  restored_backup_id: string;
  safety_backup_id: string | null;
  project: ProjectCard;
};
export type ExportJobResult = { path: string; filename: string; size_bytes: number; files_count: number };
export type CreateFromPromptJobResult = { project_id: string; title: string; project: ProjectCard };
export type DuplicateJobResult = { new_project_id: string; source_busy: boolean };

export type JobState = "running" | "done" | "error" | "interrupted";
export type JobProgress = { current: number; total: number; message: string };

export type ProjectJob<T = Record<string, unknown>> = {
  job_id: string;
  kind: string;
  project_id: string | null;
  state: JobState;
  progress: JobProgress;
  result?: T;
  error?: { code: string; message: string };
  pid: number;
  created_at: number;
  updated_at: number;
};

export type ProjectsListParams = { search?: string; sort?: "modified" | "name" | "stage"; include_busy?: boolean };

export type ProjectsCreateParams = {
  title: string;
  description: string;
  genre: string;
  target_age: string;
  language?: "ru" | "en";
  pages_min?: number;
  pages_max?: number;
  words_per_page_min?: number;
  words_per_page_max?: number;
  id_hint?: string;
};

export type ProjectsCreateFromPromptParams = {
  prompt: string;
  id_hint?: string;
  language?: "ru" | "en";
  pages_min?: number;
  pages_max?: number;
  words_per_page_min?: number;
  words_per_page_max?: number;
};

export const projectsList = (c: StorybookClient, p?: ProjectsListParams) =>
  c.call<ProjectsListResult>("projects_list", p);

export const projectsGet = (c: StorybookClient, p: { project_id: string }) =>
  c.call<ProjectsGetResult>("projects_get", p);

export const projectsSize = (c: StorybookClient, p: { project_id: string }) =>
  c.call<ProjectsSizeResult>("projects_size", p);

export const projectsSuggestId = (c: StorybookClient, p: { id_hint: string }) =>
  c.call<ProjectsSuggestIdResult>("projects_suggest_id", p);

export const projectsCreate = (c: StorybookClient, p: ProjectsCreateParams) =>
  c.call<ProjectsCreateResult>("projects_create", p);

export const projectsCreateFromPrompt = (c: StorybookClient, p: ProjectsCreateFromPromptParams) =>
  c.call<ProjectsCreateFromPromptResult>("projects_create_from_prompt", p);

export const projectsDuplicate = (c: StorybookClient, p: { project_id: string; id_hint?: string }) =>
  c.call<ProjectsDuplicateResult>("projects_duplicate", p);

export const projectsRename = (c: StorybookClient, p: { project_id: string; title: string }) =>
  c.call<ProjectsRenameResult>("projects_rename", p);

export const projectsDelete = (c: StorybookClient, p: { project_id: string; confirm: boolean }) =>
  c.call<ProjectJobStart>("projects_delete", p);

export const projectsBackupsList = (c: StorybookClient, p?: { for_project_id?: string }) =>
  c.call<ProjectsBackupsListResult>("projects_backups_list", p);

export const projectsBackupCreate = (c: StorybookClient, p: { project_id: string; note?: string }) =>
  c.call<ProjectsBackupCreateStart>("projects_backup_create", p);

export const projectsBackupRestore = (
  c: StorybookClient,
  p: { project_id: string; backup_id: string; confirm: boolean },
) => c.call<ProjectJobStart>("projects_backup_restore", p);

export const projectsExport = (c: StorybookClient, p: { project_id: string }) =>
  c.call<ProjectJobStart>("projects_export", p);

export const projectsJobStatus = (c: StorybookClient, p: { job_id: string }) =>
  c.call<ProjectJob>("projects_job_status", p);

export type WaitForJobOptions = {
  intervalMs?: number;
  onProgress?: (progress: JobProgress) => void;
  signal?: AbortSignal;
  // сколько ошибок опроса подряд терпим (успешный ответ сбрасывает счётчик); по умолчанию первая обрывает ожидание
  errorsLimit?: number;
};

const abortedError = () => new StudioError("aborted", "Ожидание отменено");

const sleep = (ms: number, signal?: AbortSignal) =>
  new Promise<void>((resolve, reject) => {
    if (signal?.aborted) return reject(abortedError());
    const onAbort = () => {
      clearTimeout(timer);
      reject(abortedError());
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    signal?.addEventListener("abort", onAbort, { once: true });
  });

// Опрашивает задание, пока оно не завершится; возвращает его результат.
export const waitForProjectJob = async <T = Record<string, unknown>>(
  c: StorybookClient,
  jobId: string,
  { intervalMs = 1000, onProgress, signal, errorsLimit = 1 }: WaitForJobOptions = {},
): Promise<T> => {
  let failures = 0;
  for (;;) {
    if (signal?.aborted) throw abortedError();
    let job: Awaited<ReturnType<typeof projectsJobStatus>>;
    try {
      job = await projectsJobStatus(c, { job_id: jobId });
      failures = 0;
    } catch (err) {
      if (signal?.aborted) throw abortedError();
      // задания нет или оно прервано - итог сразу; остальное (сеть, таймаут) терпим до лимита
      const final = err instanceof StudioError && (err.code === "not_found" || err.code === "interrupted");
      failures += 1;
      if (final || failures >= errorsLimit) throw err;
      await sleep(intervalMs, signal);
      continue;
    }
    if (signal?.aborted) throw abortedError();
    onProgress?.(job.progress);
    if (job.state === "done") return job.result as unknown as T;
    if (job.state === "error") {
      throw new StudioError(job.error?.code ?? "internal", job.error?.message ?? "Ошибка задания");
    }
    if (job.state === "interrupted") {
      throw new StudioError("interrupted", "Задание прервано: сервер был перезапущен");
    }
    await sleep(intervalMs, signal);
  }
};
