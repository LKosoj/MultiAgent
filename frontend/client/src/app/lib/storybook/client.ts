// Общий слой клиента storybook studio: вызов возможностей и ссылки на медиа.
// Про отдельные возможности он ничего не знает: их обёртки лежат в соседних файлах.

export type StudioErrorBody = {
  code: string;
  message: string;
  details?: Record<string, unknown>;
};

export type StudioSuccess<T> = { status: "success" } & T;

export class StudioError extends Error {
  code: string;
  details?: Record<string, unknown>;

  constructor(code: string, message: string, details?: Record<string, unknown>) {
    super(message);
    this.name = "StudioError";
    this.code = code;
    this.details = details;
  }
}

export type StorybookTransport = (
  action: string,
  payload: Record<string, unknown>,
  options?: { timeoutMs?: number },
) => Promise<unknown>;

export type StorybookClientOptions = {
  apiBase: string; // например "http://localhost:8000" (без /agent)
  timeoutMs?: number;
  now?: () => number;
};

export type ThumbSize = 128 | 256 | 512;

export type StorybookClient = {
  call<T extends object = Record<string, unknown>>(
    id: string,
    payload?: Record<string, unknown>,
    options?: { timeoutMs?: number },
  ): Promise<StudioSuccess<T>>;
  // force: кэш токена игнорируется, запрашивается новый (повтор после ошибки загрузки файла)
  ensureMediaToken(projectId: string, opts?: { force?: boolean }): Promise<void>;
  mediaUrl(projectId: string, relPath: string): string | null;
  thumbUrl(projectId: string, relPath: string, size?: ThumbSize): string | null;
};

const DEFAULT_TIMEOUT_MS = 20_000;
const TOKEN_REFRESH_MARGIN_MS = 5 * 60 * 1000;

export const apiBaseFromAgentUrl = (agentUrl: string): string =>
  agentUrl.replace(/\/+$/, "").replace(/\/agent$/, "");

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

export const createStorybookClient = (
  runServiceAction: StorybookTransport,
  options: StorybookClientOptions,
): StorybookClient => {
  const apiBase = options.apiBase.replace(/\/+$/, "");
  const now = options.now ?? Date.now;
  const tokens = new Map<string, { token: string; expMs: number }>();
  const pending = new Map<string, Promise<void>>();

  const call: StorybookClient["call"] = async <T extends object>(
    id: string,
    payload: Record<string, unknown> = {},
    callOptions?: { timeoutMs?: number },
  ) => {
    const raw = await runServiceAction(`storybook.${id}`, payload, {
      timeoutMs: callOptions?.timeoutMs ?? options.timeoutMs ?? DEFAULT_TIMEOUT_MS,
    });
    if (!isRecord(raw) || typeof raw.status !== "string") {
      throw new StudioError("invalid_result", `Ответ storybook.${id} без поля status`);
    }
    if (raw.status === "error") {
      const body = isRecord(raw.error) ? raw.error : {};
      throw new StudioError(
        typeof body.code === "string" ? body.code : "internal",
        typeof body.message === "string" ? body.message : "Ошибка",
        isRecord(body.details) ? body.details : undefined,
      );
    }
    return raw as StudioSuccess<T>;
  };

  const tokenIsFresh = (projectId: string): boolean => {
    const entry = tokens.get(projectId);
    return !!entry && entry.expMs - now() > TOKEN_REFRESH_MARGIN_MS;
  };

  const ensureMediaToken = (projectId: string, opts?: { force?: boolean }): Promise<void> => {
    if (!opts?.force && tokenIsFresh(projectId)) return Promise.resolve();
    const running = pending.get(projectId);
    if (running) return running;
    const request = call<{ t: string; expires_at: number }>("media_token", { project_id: projectId })
      .then((result) => {
        tokens.set(projectId, { token: result.t, expMs: result.expires_at * 1000 });
      })
      .finally(() => pending.delete(projectId));
    pending.set(projectId, request);
    return request;
  };

  const buildUrl = (projectId: string, route: string, relPath: string, extra = ""): string | null => {
    const entry = tokens.get(projectId);
    if (!entry || entry.expMs <= now()) return null;
    const base = `${apiBase}/v1/storybook/projects/${encodeURIComponent(projectId)}/${route}`;
    return `${base}?path=${encodeURIComponent(relPath)}${extra}&t=${encodeURIComponent(entry.token)}`;
  };

  return {
    call,
    ensureMediaToken,
    mediaUrl: (projectId, relPath) => buildUrl(projectId, "media", relPath),
    thumbUrl: (projectId, relPath, size = 256) => buildUrl(projectId, "thumb", relPath, `&size=${size}`),
  };
};
