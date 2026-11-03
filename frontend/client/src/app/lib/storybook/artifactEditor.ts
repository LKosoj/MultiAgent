// Редактор одного файла проекта: открыть, править (форма или JSON), проверить, сохранить, история, откат.
// Фабрика без React: клиент внедряется, состояние читается через getState/subscribe (в тестах - фейковый клиент).
// Правила: сохранение идёт с digest из чтения; при конфликте правки не теряются (буфер «Мои правки»);
// «сохранить всё равно» и откат невалидной версии - только по явному подтверждению (allow_invalid).
import {
  artifactDiff,
  artifactHistory,
  artifactRead,
  artifactRestore,
  artifactSave,
  artifactValidate,
  type ArtifactDiffResult,
  type ArtifactItem,
  type ArtifactReadResult,
  type ArtifactSaveResult,
  type ArtifactVersion,
  type Finding,
  type FindingCounts,
} from "./artifacts";
import {
  applyInput,
  buildFields,
  contentToText,
  textToContent,
  type FormField,
} from "./artifactForm";
import type { StorybookClient } from "./client";
import { FILES_TEXTS, filesFmt } from "./filesTexts";
import { filesToValidate, type ValidateAllEntry } from "./filesView";
import { NO_RIGHTS_TEXT } from "./texts";
import { classifyStorybookError } from "./uiErrors";

export type EditorMode = "form" | "json";
export type EditorPhase = "idle" | "loading" | "ready" | "error";

export type SaveProblem = {
  text: string;
  // ошибки проверки от сервера (для «Сохранить всё равно…» и подсветки полей)
  errors: Finding[];
};

export type HistoryState = {
  open: boolean;
  phase: "idle" | "loading" | "ready" | "error";
  items: ArtifactVersion[];
  error: string | null;
};

export type DiffState = {
  versionId: string;
  phase: "loading" | "ready" | "error";
  result: ArtifactDiffResult | null;
  error: string | null;
};

export type EditorState = {
  key: string | null;
  phase: EditorPhase;
  error: string | null;
  // последнее чтение текущего файла: из него берутся digest и права
  read: ArtifactReadResult | null;
  // открыта старая версия (только чтение)
  versionRead: ArtifactReadResult | null;
  // дата открытой версии (в чтении версии её нет, берётся из истории)
  versionDate: string | null;
  mode: EditorMode;
  // рабочий документ формы и рабочий текст JSON; в режиме «Форма» главный content, в режиме «JSON» - text
  content: unknown;
  text: string;
  baseContent: unknown;
  baseText: string;
  // структура формы (строится при открытии и переключении; значения живут в content)
  fields: FormField[] | null;
  dirty: boolean;
  findings: Finding[];
  counts: FindingCounts | null;
  truncated: boolean;
  validating: boolean;
  saving: boolean;
  saveProblem: SaveProblem | null;
  conflict: boolean;
  forbidden: boolean;
  myEdits: string | null;
  draftRestored: boolean;
  mergedNote: boolean;
  modeBlock: string | null;
  // файл, на который хотели перейти, пока есть несохранённые правки
  pendingKey: string | null;
  history: HistoryState;
  diff: DiffState | null;
};

export type Draft = {
  baseDigest: string | null;
  mode: EditorMode;
  content: unknown;
  text: string;
};

export type DraftStore = {
  get: (id: string) => Draft | undefined;
  set: (id: string, draft: Draft) => void;
  delete: (id: string) => void;
};

export const createDraftStore = (): DraftStore => {
  const map = new Map<string, Draft>();
  return { get: (id) => map.get(id), set: (id, d) => void map.set(id, d), delete: (id) => void map.delete(id) };
};

// Черновики переживают смену раздела и проекта внутри страницы
export const sharedDrafts: DraftStore = createDraftStore();

export type EditorOptions = {
  client: StorybookClient;
  projectId: string;
  say?: (text: string, type?: "error" | "success" | "info") => void;
  // после записи на диск: список файлов надо перечитать
  onSaved?: () => void;
  drafts?: DraftStore;
};

const emptyHistory = (): HistoryState => ({ open: false, phase: "idle", items: [], error: null });

const initialState = (): EditorState => ({
  key: null,
  phase: "idle",
  error: null,
  read: null,
  versionRead: null,
  versionDate: null,
  mode: "form",
  content: null,
  text: "",
  baseContent: null,
  baseText: "",
  fields: null,
  dirty: false,
  findings: [],
  counts: null,
  truncated: false,
  validating: false,
  saving: false,
  saveProblem: null,
  conflict: false,
  forbidden: false,
  myEdits: null,
  draftRestored: false,
  mergedNote: false,
  modeBlock: null,
  pendingKey: null,
  history: emptyHistory(),
  diff: null,
});

const same = (a: unknown, b: unknown): boolean => contentToText(a) === contentToText(b);

// Есть ли несохранённые правки в рабочей копии
export const computeDirty = (s: Pick<EditorState, "mode" | "content" | "text" | "baseContent" | "baseText" | "read">): boolean => {
  if (!s.read) return false;
  if (s.mode === "form") return !same(s.content, s.baseContent);
  if (s.text === s.baseText) return false;
  const parsed = textToContent(s.text);
  return !(parsed.ok && s.read.parse_error === null && same(parsed.value, s.baseContent));
};

const findingsFromDetails = (details: Record<string, unknown> | undefined): Finding[] =>
  Array.isArray(details?.errors) ? (details.errors as Finding[]) : [];

export const createArtifactEditor = (o: EditorOptions) => {
  const drafts = o.drafts ?? sharedDrafts;
  const say = o.say ?? (() => undefined);
  let state: EditorState = initialState();
  const listeners = new Set<() => void>();
  let openSeq = 0;
  let disposed = false;
  // «Мои правки» по файлам: живут, пока жив редактор
  const myEditsByKey = new Map<string, string>();

  const draftId = (key: string) => `${o.projectId}:${key}`;
  const emit = () => listeners.forEach((l) => l());
  const set = (patch: Partial<EditorState>) => {
    state = { ...state, ...patch };
    state.dirty = computeDirty(state);
    emit();
  };

  const workingText = (s: EditorState = state): string => (s.mode === "form" ? contentToText(s.content) : s.text);

  const stash = () => {
    if (!state.key || !state.read || !state.dirty) return;
    drafts.set(draftId(state.key), { baseDigest: state.read.digest, mode: state.mode, content: state.content, text: workingText() });
  };

  // Загрузка файла в редактор (рабочая копия заменяется)
  const open = async (key: string): Promise<void> => {
    const seq = ++openSeq;
    set({
      key,
      phase: "loading",
      error: null,
      saving: false,
      validating: false,
      read: null,
      versionRead: null,
      versionDate: null,
      findings: [],
      counts: null,
      truncated: false,
      saveProblem: null,
      conflict: false,
      draftRestored: false,
      mergedNote: false,
      modeBlock: null,
      pendingKey: null,
      history: emptyHistory(),
      diff: null,
      myEdits: myEditsByKey.get(key) ?? null,
    });
    let read: ArtifactReadResult;
    try {
      read = await artifactRead(o.client, { project_id: o.projectId, key });
    } catch (err) {
      if (seq !== openSeq || disposed) return;
      const c = classifyStorybookError(err);
      set({ phase: "error", error: c.text, forbidden: c.kind === "forbidden" ? true : state.forbidden });
      return;
    }
    if (seq !== openSeq || disposed) return;
    const content = read.content;
    const text = read.text ?? "";
    // повреждённый файл открывается только в режиме JSON
    const mode: EditorMode = read.parse_error || read.content === null || buildFields(key, content) === null ? "json" : "form";
    let patch: Partial<EditorState> = {
      phase: "ready",
      read,
      baseContent: content,
      baseText: text,
      content,
      text,
      mode,
      fields: mode === "form" ? buildFields(key, content) : null,
    };
    const draft = drafts.get(draftId(key));
    if (draft) {
      drafts.delete(draftId(key));
      if (draft.baseDigest === read.digest) {
        // файл тот же: возвращаем правки на место
        const asForm = draft.mode === "form" && mode === "form";
        patch = {
          ...patch,
          mode: asForm ? "form" : "json",
          content: asForm ? draft.content : content,
          text: asForm ? text : draft.text,
          fields: asForm ? buildFields(key, draft.content) : null,
          draftRestored: true,
        };
      } else {
        // файл успел измениться: правки уходят в буфер «Мои правки»
        myEditsByKey.set(key, draft.text);
        patch = { ...patch, myEdits: draft.text };
      }
    }
    set(patch);
  };

  const reload = () => (state.key ? open(state.key) : Promise.resolve());

  // Перейти к файлу; при несохранённых правках сначала спросить
  const requestOpen = async (key: string): Promise<void> => {
    if (key === state.key && state.phase === "ready" && !state.versionRead) return;
    if (state.dirty) {
      set({ pendingKey: key });
      return;
    }
    await open(key);
  };

  const resolvePending = async (decision: "save" | "discard" | "stay"): Promise<void> => {
    const target = state.pendingKey;
    if (!target) return;
    if (decision === "stay") {
      set({ pendingKey: null });
      return;
    }
    if (decision === "save") {
      await save();
      // не вышло (ошибки проверки, конфликт): остаёмся с правками
      if (state.dirty || state.conflict || state.saveProblem) {
        set({ pendingKey: null });
        return;
      }
    }
    await open(target);
  };

  const setMode = async (mode: EditorMode): Promise<void> => {
    if (mode === state.mode || !state.read || state.versionRead) return;
    if (mode === "json") {
      const text = state.dirty ? contentToText(state.content) : state.baseText;
      set({ mode, text, modeBlock: null, fields: null });
      return;
    }
    const parsed = textToContent(state.text);
    if (!parsed.ok) {
      // строку и причину ошибки даёт сервер: у Tk и React она одинаковая
      let reason: string = FILES_TEXTS.form_unavailable;
      let line: number | null = null;
      let column: number | null = null;
      const seq = openSeq;
      try {
        const check = await artifactValidate(o.client, { project_id: o.projectId, key: state.key ?? "", text: state.text });
        if (seq !== openSeq || disposed) return;
        const syntax = check.findings.find((f) => f.code === "syntax");
        if (syntax) {
          reason = syntax.message;
          line = syntax.line ?? null;
          column = syntax.column ?? null;
        }
      } catch {
        // без ответа сервера остаётся общая причина
      }
      set({
        modeBlock: line
          ? filesFmt("json_blocked", { line, column: column ?? 1, reason })
          : filesFmt("json_blocked_nopos", { reason }),
      });
      return;
    }
    const fields = buildFields(state.key ?? "", parsed.value);
    if (!fields) {
      set({ modeBlock: FILES_TEXTS.form_unavailable });
      return;
    }
    set({ mode, content: parsed.value, fields, modeBlock: null });
  };

  const editField = (field: Pick<FormField, "kind" | "path" | "label">, input: string | boolean): string | null => {
    if (state.mode !== "form" || state.saving) return null;
    const result = applyInput(state.content, field, input);
    if (result.content !== state.content) set({ content: result.content, saveProblem: null });
    return result.error;
  };

  const setText = (text: string) => {
    if (state.mode !== "json" || state.saving) return;
    set({ text, saveProblem: null, modeBlock: null });
  };

  const validate = async (): Promise<FindingCounts | null> => {
    if (!state.key || !state.read) return null;
    set({ validating: true });
    const seq = openSeq;
    try {
      const payload = state.mode === "form" ? { content: state.content } : { text: state.text };
      const res = await artifactValidate(o.client, { project_id: o.projectId, key: state.key, ...payload });
      // за время запроса открыли другой файл: ответ чужой
      if (seq !== openSeq || disposed) return null;
      set({ validating: false, findings: res.findings, counts: res.counts, truncated: res.truncated });
      if (res.counts.errors === 0) say(FILES_TEXTS.toast_valid, "success");
      return res.counts;
    } catch (err) {
      if (seq !== openSeq || disposed) return null;
      const c = classifyStorybookError(err);
      set({ validating: false });
      say(c.text, "error");
      return null;
    }
  };

  // Правки уходят в буфер «Мои правки» перед перечитыванием файла
  const stashMyEdits = () => {
    if (!state.key || !state.dirty) return;
    const text = workingText();
    myEditsByKey.set(state.key, text);
  };

  const refreshAfterConflict = async (): Promise<void> => {
    stashMyEdits();
    set({ conflict: false });
    await reload();
  };

  const closeMyEdits = () => {
    if (state.key) myEditsByKey.delete(state.key);
    set({ myEdits: null });
  };

  const handleWriteError = (err: unknown): void => {
    const c = classifyStorybookError(err);
    if (c.kind === "conflict") {
      set({ saving: false, conflict: true });
    } else if (c.kind === "forbidden") {
      set({ saving: false, forbidden: true, saveProblem: { text: NO_RIGHTS_TEXT, errors: [] } });
    } else {
      const errors = c.kind === "invalid" ? findingsFromDetails(c.details) : [];
      set({
        saving: false,
        saveProblem: { text: c.text, errors },
        ...(errors.length ? { findings: errors, counts: { errors: errors.filter((f) => f.severity === "error").length, warnings: errors.filter((f) => f.severity === "warning").length }, truncated: false } : {}),
      });
    }
  };

  // Успешная запись на диск: новый digest, перечитать при слиянии, обновить список
  const afterWrite = async (result: ArtifactSaveResult, savedText: string, toast: string): Promise<void> => {
    say(toast, "success");
    if (state.key) drafts.delete(draftId(state.key));
    for (const w of result.warnings ?? []) if (w.message) say(w.message, "info");
    o.onSaved?.();
    if (result.merged || result.external_changes) {
      await reload();
      set({ mergedNote: true, findings: result.validation.findings, counts: result.validation.counts, truncated: false });
      return;
    }
    const parsed = textToContent(savedText);
    const content = parsed.ok ? parsed.value : state.content;
    set({
      saving: false,
      saveProblem: null,
      read: state.read ? { ...state.read, digest: result.digest, text: savedText, content, parse_error: null } : state.read,
      baseContent: content,
      baseText: savedText,
      content,
      text: savedText,
      fields: state.mode === "form" ? buildFields(state.key ?? "", content) : null,
      findings: result.validation.findings,
      counts: result.validation.counts,
      truncated: false,
      mergedNote: false,
      draftRestored: false,
    });
  };

  const save = async (opts: { allowInvalid?: boolean } = {}): Promise<boolean> => {
    const s = state;
    if (!s.key || !s.read || s.saving || s.versionRead || !s.dirty) return false;
    set({ saving: true, saveProblem: null });
    const savedText = workingText(s);
    try {
      const result = await artifactSave(o.client, {
        project_id: o.projectId,
        key: s.key,
        base_digest: s.read.digest,
        ...(s.mode === "form" ? { content: s.content } : { text: s.text }),
        ...(opts.allowInvalid ? { allow_invalid: true } : {}),
      });
      if (disposed) return true;
      await afterWrite(result, savedText, FILES_TEXTS.toast_saved);
      return true;
    } catch (err) {
      if (!disposed) handleWriteError(err);
      return false;
    }
  };

  // --- Старые версии ---

  const openHistory = async (): Promise<void> => {
    if (!state.key) return;
    const key = state.key;
    set({ history: { open: true, phase: "loading", items: [], error: null }, diff: null });
    try {
      const res = await artifactHistory(o.client, { project_id: o.projectId, key });
      if (state.key !== key) return;
      set({ history: { open: true, phase: "ready", items: res.items, error: null } });
    } catch (err) {
      if (state.key !== key) return;
      set({ history: { open: true, phase: "error", items: [], error: classifyStorybookError(err).text } });
    }
  };

  const closeHistory = () => set({ history: emptyHistory(), diff: null });

  const compareWith = async (versionId: string): Promise<void> => {
    if (!state.key) return;
    const key = state.key;
    set({ diff: { versionId, phase: "loading", result: null, error: null } });
    try {
      const result = await artifactDiff(o.client, { project_id: o.projectId, key, version_id: versionId });
      if (state.key !== key || state.diff?.versionId !== versionId) return;
      set({ diff: { versionId, phase: "ready", result, error: null } });
    } catch (err) {
      if (state.key !== key || state.diff?.versionId !== versionId) return;
      set({ diff: { versionId, phase: "error", result: null, error: classifyStorybookError(err).text } });
    }
  };

  const viewVersion = async (versionId: string): Promise<void> => {
    if (!state.key) return;
    const key = state.key;
    const seq = openSeq;
    try {
      const versionRead = await artifactRead(o.client, { project_id: o.projectId, key, version_id: versionId });
      if (seq !== openSeq || disposed) return;
      const created = state.history.items.find((v) => v.version_id === versionId)?.created_at;
      set({ versionRead, versionDate: created ?? null });
    } catch (err) {
      if (seq !== openSeq || disposed) return;
      say(classifyStorybookError(err).text, "error");
    }
  };

  const leaveVersion = () => {
    if (state.versionRead) set({ versionRead: null, versionDate: null });
  };

  // Откат. "invalid" - версия не проходит проверку: спросить и повторить с allowInvalid
  const restore = async (versionId: string, opts: { allowInvalid?: boolean } = {}): Promise<"done" | "invalid" | "failed"> => {
    if (!state.key || !state.read || state.saving) return "failed";
    const key = state.key;
    const seq = openSeq;
    set({ saving: true, saveProblem: null });
    try {
      const result = await artifactRestore(o.client, {
        project_id: o.projectId,
        key,
        version_id: versionId,
        base_digest: state.read.digest,
        ...(opts.allowInvalid ? { allow_invalid: true } : {}),
      });
      say(FILES_TEXTS.toast_restored, "success");
      for (const w of result.warnings ?? []) if (w.message) say(w.message, "info");
      o.onSaved?.();
      // пока шёл откат, пользователь ушёл к другому файлу: его экран не трогаем
      if (seq !== openSeq || disposed) return "done";
      stashMyEdits();
      drafts.delete(draftId(key));
      set({ saving: false, history: emptyHistory(), diff: null, versionRead: null, versionDate: null });
      await open(key);
      return "done";
    } catch (err) {
      if (seq !== openSeq || disposed) return "failed";
      const c = classifyStorybookError(err);
      if (c.kind === "invalid" && !opts.allowInvalid) {
        const errors = findingsFromDetails(c.details);
        set({ saving: false });
        // корневые и синтаксические ошибки сервер не пропускает и с allow_invalid
        if (errors.length > 0 && errors.every((f) => f.code !== "root" && f.code !== "syntax")) return "invalid";
        say(c.text, "error");
        return "failed";
      }
      if (c.kind === "conflict") set({ saving: false, conflict: true });
      else {
        set({ saving: false });
        say(c.kind === "forbidden" ? NO_RIGHTS_TEXT : c.text, "error");
        if (c.kind === "forbidden") set({ forbidden: true });
      }
      return "failed";
    }
  };

  const dispose = () => {
    stash();
    disposed = true;
  };

  // StrictMode в dev гасит и снова включает эффекты: редактор после dispose должен уметь ожить
  const activate = () => {
    disposed = false;
    // правки на месте и редактор жив: черновик от dispose устарел и стал бы «чужими правками»
    if (state.key) drafts.delete(draftId(state.key));
  };

  return {
    getState: () => state,
    subscribe: (l: () => void) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    open,
    reload,
    requestOpen,
    resolvePending,
    setMode,
    editField,
    setText,
    validate,
    save,
    refreshAfterConflict,
    closeMyEdits,
    openHistory,
    closeHistory,
    compareWith,
    viewVersion,
    leaveVersion,
    restore,
    dispose,
    activate,
  };
};

export type ArtifactEditor = ReturnType<typeof createArtifactEditor>;

// Проверка всех файлов проекта (по диску, не по правкам): до трёх запросов одновременно
export const validateAllFiles = async (
  client: StorybookClient,
  projectId: string,
  items: readonly ArtifactItem[],
  isCancelled: () => boolean = () => false,
): Promise<ValidateAllEntry[]> => {
  const todo = filesToValidate(items);
  const out: (ValidateAllEntry | undefined)[] = new Array(todo.length);
  let next = 0;
  const worker = async () => {
    for (;;) {
      const i = next++;
      if (i >= todo.length || isCancelled()) return;
      const item = todo[i];
      try {
        const res = await artifactValidate(client, { project_id: projectId, key: item.artifact_key });
        out[i] = { key: item.artifact_key, title: item.title, counts: res.counts, error: null };
      } catch (err) {
        out[i] = { key: item.artifact_key, title: item.title, counts: null, error: classifyStorybookError(err).text };
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(3, todo.length) }, worker));
  return out.filter((e): e is ValidateAllEntry => e !== undefined);
};
