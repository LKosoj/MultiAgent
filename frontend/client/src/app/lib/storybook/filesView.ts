// Чистая логика экрана «Файлы»: строки списка, состояние редактора, баннеры, находки, история.
// Правила одинаковы с Tk-версией: векторы лежат в golden_files_vectors.json.
import type {
  ArtifactDiffChange,
  ArtifactItem,
  ArtifactReadResult,
  ArtifactStatus,
  ArtifactVersion,
  Finding,
  FindingCounts,
  ParseError,
} from "./artifacts";
import { jsonOffsetOf, offsetAt, type JsonPath } from "./artifactForm";
import { FILES_TEXTS, filesFmt } from "./filesTexts";
import { formatBytes, formatDateTimeRu } from "./format";

export const STATUS_TEXT: Record<ArtifactStatus, string> = {
  present: FILES_TEXTS.status_present,
  missing: FILES_TEXTS.status_missing,
  invalid: FILES_TEXTS.status_invalid,
};

export type ListRow = {
  key: string;
  title: string;
  status: ArtifactStatus;
  statusText: string;
  // значки текстом: цвет не единственный признак
  badges: string[];
  // подсказка: путь файла (+ причина «только чтение»)
  tooltip: string;
};

export const listRows = (items: readonly ArtifactItem[]): ListRow[] =>
  items.map((item) => {
    const badges: string[] = [];
    if (!item.editable) badges.push(FILES_TEXTS.badge_readonly);
    if (item.careful) badges.push(FILES_TEXTS.badge_careful);
    const tooltipParts = [item.path];
    if (!item.editable && item.read_only_reason) tooltipParts.push(item.read_only_reason);
    if (item.careful) tooltipParts.push(FILES_TEXTS.careful_hint);
    return {
      key: item.artifact_key,
      title: item.title,
      status: item.status,
      statusText: STATUS_TEXT[item.status] ?? item.status,
      badges,
      tooltip: tooltipParts.join("\n"),
    };
  });

// Подпись ошибки разбора: «строка N, столбец M: причина» (без позиции - только причина)
export const parseErrorText = (error: ParseError): string =>
  error.line ? `строка ${error.line}, столбец ${error.column ?? 1}: ${error.message}` : error.message;

export type EditorScreen = "empty" | "loading" | "error" | "missing" | "ready";

export const resolveEditorScreen = (a: {
  key: string | null;
  phase: "idle" | "loading" | "ready" | "error";
  read: Pick<ArtifactReadResult, "exists"> | null;
}): EditorScreen => {
  if (!a.key) return "empty";
  if (a.phase === "loading" || a.phase === "idle") return "loading";
  if (a.phase === "error") return "error";
  return a.read && !a.read.exists ? "missing" : "ready";
};

export type Banner = { tone: "error" | "warn" | "info"; text: string };

// Баннеры над редактором в порядке важности
export const editorBanners = (a: {
  read: ArtifactReadResult;
  forbidden: boolean;
  versionDate: string | null;
}): Banner[] => {
  const { read } = a;
  const out: Banner[] = [];
  if (read.parse_error) {
    out.push({
      tone: "error",
      text: filesFmt("corrupt", { name: read.title, reason: parseErrorText(read.parse_error) }),
    });
  }
  if (a.versionDate) out.push({ tone: "info", text: filesFmt("version_note", { date: a.versionDate }) });
  else if (a.forbidden) out.push({ tone: "warn", text: FILES_TEXTS.no_rights });
  else if (!read.editable && read.read_only_reason) {
    out.push({ tone: "info", text: filesFmt("readonly_note", { reason: read.read_only_reason }) });
  }
  if (!a.versionDate) {
    if (read.run_state === "busy") {
      out.push({ tone: "warn", text: [FILES_TEXTS.busy, read.run_state_message].filter(Boolean).join(". ") });
    } else if (read.run_state === "warn" && read.run_state_message) {
      out.push({ tone: "warn", text: read.run_state_message });
    }
  }
  return out;
};

export type Access = { readOnly: boolean; canSave: boolean };

// Можно ли править: старая версия, «только чтение», занятый файл и нехватка прав закрывают правку
export const editorAccess = (a: {
  read: Pick<ArtifactReadResult, "editable" | "run_state">;
  viewingVersion: boolean;
  forbidden: boolean;
  dirty: boolean;
  saving: boolean;
}): Access => {
  const readOnly = a.viewingVersion || !a.read.editable || a.read.run_state === "busy" || a.forbidden;
  return { readOnly, canSave: !readOnly && a.dirty && !a.saving };
};

export const countsText = (counts: FindingCounts): string =>
  filesFmt("counts", { errors: counts.errors, warnings: counts.warnings });

export type FindingRow = {
  pointer: string;
  path: JsonPath;
  where: string;
  message: string;
  severity: "error" | "warning";
  line: number | null;
  column: number | null;
};

export const findingRows = (findings: readonly Finding[]): FindingRow[] =>
  findings.map((f) => ({
    pointer: f.pointer,
    path: f.path,
    where: f.label || f.pointer || "/",
    message: f.message,
    severity: f.severity,
    line: f.line ?? null,
    column: f.column ?? null,
  }));

// Смещение курсора в тексте JSON для находки: синтаксическая - по строке и столбцу, прочие - по пути
export const cursorForFinding = (finding: Pick<Finding, "path" | "line" | "column">, text: string): number | null => {
  if (finding.line) return offsetAt(text, finding.line, finding.column ?? 1);
  return jsonOffsetOf(text, finding.path);
};

// «Сохранить всё равно» возможно, только если есть ошибки и среди них нет корневой и синтаксической:
// их сервер не пропускает и с allow_invalid
export const canSaveAnyway = (findings: readonly Pick<Finding, "severity" | "code">[]): boolean => {
  const errors = findings.filter((f) => f.severity === "error");
  return errors.length > 0 && errors.every((f) => f.code !== "root" && f.code !== "syntax");
};

export const errorCount = (findings: readonly Pick<Finding, "severity">[]): number =>
  findings.filter((f) => f.severity === "error").length;

// Указатели вложенных блоков, которые надо раскрыть, чтобы увидеть поле: "/items/3/timing" -> "/items", "/items/3"
export const ancestorPointers = (pointer: string): string[] => {
  const parts = pointer.split("/").slice(1);
  const out: string[] = [];
  for (let i = 1; i < parts.length; i += 1) out.push("/" + parts.slice(0, i).join("/"));
  return out;
};

// --- История ---

export type VersionRow = { id: string; date: string; size: string; reason: string };

export const versionRows = (items: readonly ArtifactVersion[]): VersionRow[] =>
  [...items]
    .sort((a, b) => (a.created_at < b.created_at ? 1 : a.created_at > b.created_at ? -1 : 0))
    .map((v) => ({ id: v.version_id, date: formatDateTimeRu(v.created_at), size: formatBytes(v.size), reason: v.reason_title }));

const CLIP = 80;

export const formatDiffValue = (value: unknown, present: boolean): string => {
  if (!present || value === undefined) return FILES_TEXTS.diff_none;
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return text.length > CLIP ? `${text.slice(0, CLIP)}…` : text;
};

export type DiffRow = { where: string; before: string; after: string };

export const diffRows = (changes: readonly ArtifactDiffChange[]): DiffRow[] =>
  changes.map((c) => ({
    where: c.label || c.pointer || "/",
    before: formatDiffValue(c.before, c.op !== "added"),
    after: formatDiffValue(c.after, c.op !== "removed"),
  }));

// --- Проверка всех файлов ---

export type ValidateAllEntry = {
  key: string;
  title: string;
  counts: FindingCounts | null;
  // проверить не удалось
  error: string | null;
};

export type ValidateAllSummary = {
  checked: number;
  errors: number;
  warnings: number;
  // true, если ошибок нет нигде и каждый файл проверился
  passed: boolean;
  // файлы с замечаниями, ошибками или сбоем проверки
  problems: ValidateAllEntry[];
};

export const summarizeValidateAll = (entries: readonly ValidateAllEntry[]): ValidateAllSummary => {
  let errors = 0;
  let warnings = 0;
  const problems: ValidateAllEntry[] = [];
  for (const e of entries) {
    if (e.counts) {
      errors += e.counts.errors;
      warnings += e.counts.warnings;
    }
    if (e.error || (e.counts && (e.counts.errors > 0 || e.counts.warnings > 0))) problems.push(e);
  }
  return {
    checked: entries.length,
    errors,
    warnings,
    passed: errors === 0 && entries.every((e) => !e.error),
    problems,
  };
};

export const validateAllText = (s: ValidateAllSummary): string =>
  filesFmt("validate_all_done", { n: s.checked, errors: s.errors, warnings: s.warnings });

// Файлы, которые есть на диске, - только их имеет смысл проверять
export const filesToValidate = (items: readonly ArtifactItem[]): ArtifactItem[] => items.filter((i) => i.exists);
