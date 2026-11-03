// Обёртки возможностей модуля artifacts: реестр JSON-файлов проекта, чтение, проверка, сохранение, история.
import { StudioError, type StorybookClient } from "./client";

export type ArtifactStatus = "present" | "missing" | "invalid";
export type ArtifactRunState = "free" | "warn" | "busy";
export type ParseError = { line: number | null; column: number | null; message: string };

export type ArtifactItem = {
  artifact_key: string; // не "key": бэкенд закрашивает такие поля в ответах
  title: string;
  path: string; // относительный путь внутри проекта
  exists: boolean;
  status: ArtifactStatus;
  size: number;
  modified: string | null;
  editable: boolean;
  read_only_reason: string;
  careful: boolean;
  root: "object" | "array";
  writers: { step_id: string; title: string }[];
  produced_by: string;
  run_state: ArtifactRunState;
  run_state_message: string;
  history_count: number;
  error: ParseError | null;
};

export type ArtifactsListResult = {
  project_id: string;
  run: { active: boolean; current_steps: string[] | null };
  items: ArtifactItem[];
};

export type Finding = {
  severity: "error" | "warning";
  code: string;
  path: (string | number)[];
  pointer: string;
  label: string;
  message: string;
  line?: number | null;
  column?: number | null;
};

export type ArtifactReadResult = {
  artifact_key: string;
  title: string;
  path: string;
  exists: boolean;
  digest: string | null; // отпечаток: его надо вернуть в artifactSave как base_digest
  size: number;
  modified: string | null;
  editable: boolean;
  read_only_reason: string;
  content: unknown;
  text: string | null;
  parse_error: ParseError | null;
  is_version: boolean;
  version_id: string | null;
  run_state: ArtifactRunState;
  run_state_message: string;
};

export type FindingCounts = { errors: number; warnings: number };

export type ArtifactValidateResult = {
  artifact_key: string;
  valid: boolean;
  counts: FindingCounts;
  findings: Finding[];
  truncated: boolean;
};

export type ArtifactSaveResult = {
  saved: true;
  digest: string;
  previous_digest: string | null;
  version_id: string | null;
  merged: boolean;
  external_changes: boolean;
  validation: { counts: FindingCounts; findings: Finding[] };
  warnings: { code: string; message: string }[];
  restored_from?: string; // только у отката
};

export type ArtifactReason = "save" | "restore" | "external";

export type ArtifactVersion = {
  version_id: string;
  created_at: string;
  size: number;
  digest8: string;
  reason: ArtifactReason;
  reason_title: string;
};

export type ArtifactHistoryResult = { artifact_key: string; items: ArtifactVersion[] };

export type ArtifactDiffChange = {
  path: (string | number)[];
  pointer: string;
  label: string;
  op: "added" | "removed" | "changed";
  before: unknown;
  after: unknown;
};

export type ArtifactDiffResult = {
  artifact_key: string;
  version_id: string;
  identical: boolean;
  changes: ArtifactDiffChange[];
  truncated: boolean;
};

// Сохранение shots со 100 шотами и сравнение версий дольше обычного вызова.
const LONG_TIMEOUT_MS = 60_000;

export const artifactsList = (c: StorybookClient, p: { project_id: string }) =>
  c.call<ArtifactsListResult>("artifacts_list", p);

export const artifactRead = (c: StorybookClient, p: { project_id: string; key: string; version_id?: string }) =>
  c.call<ArtifactReadResult>("artifact_read", p);

// Ровно одно из content / text; без обоих проверяется файл на диске.
export const artifactValidate = (
  c: StorybookClient,
  p: { project_id: string; key: string; content?: unknown; text?: string },
) => c.call<ArtifactValidateResult>("artifact_validate", p);

// Ровно одно из content / text. base_digest - отпечаток из artifactRead (null, если файла не было).
export const artifactSave = (
  c: StorybookClient,
  p: {
    project_id: string;
    key: string;
    base_digest: string | null;
    content?: unknown;
    text?: string;
    allow_invalid?: boolean;
  },
  options?: { timeoutMs?: number },
) => c.call<ArtifactSaveResult>("artifact_save", p, { timeoutMs: LONG_TIMEOUT_MS, ...options });

export const artifactHistory = (c: StorybookClient, p: { project_id: string; key: string }) =>
  c.call<ArtifactHistoryResult>("artifact_history", p);

export const artifactDiff = (
  c: StorybookClient,
  p: { project_id: string; key: string; version_id: string },
  options?: { timeoutMs?: number },
) => c.call<ArtifactDiffResult>("artifact_diff", p, { timeoutMs: LONG_TIMEOUT_MS, ...options });

export const artifactRestore = (
  c: StorybookClient,
  // allow_invalid: откат версии, не проходящей схему, - только с явным подтверждением (как в artifactSave)
  p: { project_id: string; key: string; version_id: string; base_digest: string | null; allow_invalid?: boolean },
  options?: { timeoutMs?: number },
) => c.call<ArtifactSaveResult>("artifact_restore", p, { timeoutMs: LONG_TIMEOUT_MS, ...options });

// Файл изменился после открытия: клиент должен перечитать его (artifactRead) и повторить правку.
export const isConflict = (e: unknown): e is StudioError => e instanceof StudioError && e.code === "conflict";

// Находки по JSON Pointer - для подсветки полей формы.
export const findingsByPointer = (findings: Finding[]): Map<string, Finding[]> => {
  const grouped = new Map<string, Finding[]>();
  for (const finding of findings) {
    const bucket = grouped.get(finding.pointer);
    if (bucket) bucket.push(finding);
    else grouped.set(finding.pointer, [finding]);
  }
  return grouped;
};
