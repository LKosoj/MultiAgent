// Разбор ошибок клиента в понятные пользователю тексты (спека 5.2). Stack trace не показываем.
import { StudioError } from "./client";
import { CONFLICT_TEXT, NO_CONNECTION_TEXT, NO_RIGHTS_TEXT, PROJECT_LOCKED_TEXT, T } from "./texts";

export type ErrorKind =
  | "forbidden"
  | "timeout"
  | "network"
  | "locked"
  | "conflict"
  | "not_found"
  | "invalid"
  | "unavailable"
  | "aborted"
  | "interrupted"
  | "other";

export type ClassifiedError = { kind: ErrorKind; text: string; code?: string; details?: Record<string, unknown> };

const TIMEOUT_TEXT = "Сервер не ответил вовремя. Повторите позже.";
const OTHER_TEXT = "Что-то пошло не так. Повторите действие.";

export const isNoRights = (err: unknown): boolean =>
  err instanceof Error && !(err instanceof StudioError) && /requires role/i.test(err.message);

export const classifyStorybookError = (err: unknown): ClassifiedError => {
  if (err instanceof StudioError) {
    const base = { code: err.code, details: err.details };
    switch (err.code) {
      case "locked":
        return { kind: "locked", text: PROJECT_LOCKED_TEXT, ...base };
      case "conflict":
        return { kind: "conflict", text: err.message || CONFLICT_TEXT, ...base };
      case "not_found":
        return { kind: "not_found", text: err.message, ...base };
      case "invalid_argument":
      case "forbidden_path":
        return { kind: "invalid", text: err.message, ...base };
      case "unavailable":
        return { kind: "unavailable", text: err.message, ...base };
      case "aborted":
        return { kind: "aborted", text: err.message, ...base };
      case "interrupted":
        return { kind: "interrupted", text: T.interruptedJob, ...base };
      default:
        return { kind: "other", text: err.message || OTHER_TEXT, ...base };
    }
  }
  const message = err instanceof Error ? err.message : "";
  if (isNoRights(err)) return { kind: "forbidden", text: NO_RIGHTS_TEXT };
  if (/Таймаут запроса|timed? ?out/i.test(message)) return { kind: "timeout", text: TIMEOUT_TEXT };
  if (err instanceof TypeError || /Failed to fetch|NetworkError|Load failed|network/i.test(message)) {
    return { kind: "network", text: NO_CONNECTION_TEXT };
  }
  return { kind: "other", text: OTHER_TEXT };
};
