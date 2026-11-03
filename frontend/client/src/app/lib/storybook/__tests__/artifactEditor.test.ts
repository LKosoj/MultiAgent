import { describe, expect, it, vi } from "vitest";

import { createArtifactEditor, createDraftStore, validateAllFiles } from "../artifactEditor";
import { StudioError, type StorybookClient } from "../client";

type Handler = (payload: Record<string, unknown>) => unknown;

// Клиент с заранее заданными ответами по id возможности; ошибки бросаются как есть
const fakeClient = (handlers: Record<string, Handler>) => {
  const calls: { id: string; payload: Record<string, unknown> }[] = [];
  const client = {
    call: vi.fn(async (id: string, payload: Record<string, unknown> = {}) => {
      calls.push({ id, payload });
      const h = handlers[id];
      if (!h) throw new Error(`нет обработчика ${id}`);
      return h(payload);
    }),
  } as unknown as StorybookClient;
  return { client, calls };
};

const readOf = (content: unknown, digest = "j1:a", over: Record<string, unknown> = {}) => ({
  artifact_key: "brief", title: "Бриф", path: "brief.json", exists: true, digest, size: 10, modified: null, editable: true,
  read_only_reason: "", content, text: JSON.stringify(content, null, 2), parse_error: null, is_version: false, version_id: null,
  run_state: "free", run_state_message: "", ...over,
});
const saved = (over: Record<string, unknown> = {}) => ({
  saved: true, digest: "j1:b", previous_digest: "j1:a", version_id: "v1", merged: false, external_changes: false,
  validation: { counts: { errors: 0, warnings: 0 }, findings: [] }, warnings: [], ...over,
});
const BRIEF = { title: "Лиса", pages_min: 3 };
const setup = (handlers: Record<string, Handler>, drafts = createDraftStore()) => {
  const f = fakeClient({ artifact_read: () => readOf(BRIEF), ...handlers });
  const say = vi.fn();
  const onSaved = vi.fn();
  const editor = createArtifactEditor({ client: f.client, projectId: "p", say, onSaved, drafts });
  return { ...f, editor, say, onSaved, drafts };
};
const titleField = { kind: "string" as const, path: ["title"], label: "Название" };

describe("редактор файла", () => {
  it("открывает файл в форме, без правок", async () => {
    const { editor } = setup({});
    await editor.open("brief");
    const s = editor.getState();
    expect(s.phase).toBe("ready");
    expect(s.mode).toBe("form");
    expect(s.dirty).toBe(false);
    expect(s.fields?.map((f) => f.pointer)).toEqual(["/title", "/pages_min"]);
  });

  it("повреждённый файл открывается только в режиме JSON", async () => {
    const { editor } = setup({
      artifact_read: () => readOf(null, "j1:x", { text: "{oops", parse_error: { line: 1, column: 2, message: "Ожидалось имя" } }),
    });
    await editor.open("brief");
    expect(editor.getState().mode).toBe("json");
    expect(editor.getState().fields).toBeNull();
  });

  it("сохранение шлёт digest из чтения и content, потом сбрасывает правки", async () => {
    const { editor, calls, onSaved, say } = setup({ artifact_save: () => saved() });
    await editor.open("brief");
    editor.editField(titleField, "Заяц");
    expect(editor.getState().dirty).toBe(true);
    expect(await editor.save()).toBe(true);
    const call = calls.find((c) => c.id === "artifact_save")!;
    expect(call.payload).toMatchObject({ project_id: "p", key: "brief", base_digest: "j1:a", content: { title: "Заяц", pages_min: 3 } });
    expect(call.payload).not.toHaveProperty("allow_invalid");
    expect(editor.getState().dirty).toBe(false);
    expect(editor.getState().read?.digest).toBe("j1:b");
    expect(onSaved).toHaveBeenCalled();
    expect(say).toHaveBeenCalledWith("Файл сохранён", "success");
  });

  it("число с ошибкой не применяется", async () => {
    const { editor } = setup({});
    await editor.open("brief");
    const err = editor.editField({ kind: "number", path: ["pages_min"], label: "Страниц" }, "abc");
    expect(err).toBe("Поле «Страниц» должно быть числом.");
    expect(editor.getState().dirty).toBe(false);
  });

  it("конфликт: правки уходят в «Мои правки», файл перечитывается", async () => {
    let reads = 0;
    const { editor } = setup({
      artifact_read: () => readOf(reads++ === 0 ? BRIEF : { title: "Чужое", pages_min: 3 }, reads === 1 ? "j1:a" : "j1:z"),
      artifact_save: () => {
        throw new StudioError("conflict", "Файл изменён", { current_digest: "j1:z" });
      },
    });
    await editor.open("brief");
    editor.editField(titleField, "Моё");
    expect(await editor.save()).toBe(false);
    expect(editor.getState().conflict).toBe(true);
    await editor.refreshAfterConflict();
    const s = editor.getState();
    expect(s.conflict).toBe(false);
    expect(s.dirty).toBe(false);
    expect(s.read?.digest).toBe("j1:z");
    expect(s.myEdits).toContain("Моё");
    editor.closeMyEdits();
    expect(editor.getState().myEdits).toBeNull();
  });

  it("ошибки проверки: «сохранить всё равно» шлёт allow_invalid", async () => {
    const errors = [{ severity: "error", code: "type", path: ["pages_min"], pointer: "/pages_min", label: "x", message: "Ожидается целое число" }];
    let n = 0;
    const { editor, calls } = setup({
      artifact_save: () => {
        if (n++ === 0) throw new StudioError("invalid_argument", "Файл не прошёл проверку", { errors });
        return saved();
      },
    });
    await editor.open("brief");
    editor.editField(titleField, "Заяц");
    expect(await editor.save()).toBe(false);
    expect(editor.getState().saveProblem?.errors).toEqual(errors);
    expect(editor.getState().findings).toEqual(errors);
    expect(await editor.save({ allowInvalid: true })).toBe(true);
    const saves = calls.filter((c) => c.id === "artifact_save");
    expect(saves[0].payload).not.toHaveProperty("allow_invalid");
    expect(saves[1].payload.allow_invalid).toBe(true);
  });

  it("нет прав: режим только чтение, подсказка", async () => {
    const { editor } = setup({
      artifact_save: () => {
        throw new Error("Action requires role admin");
      },
    });
    await editor.open("brief");
    editor.editField(titleField, "Заяц");
    await editor.save();
    expect(editor.getState().forbidden).toBe(true);
    expect(editor.getState().saveProblem?.text).toBeTruthy();
  });

  it("слияние с чужими правками: файл перечитывается и показывается пометка", async () => {
    let reads = 0;
    const { editor } = setup({
      artifact_read: () => {
        reads += 1;
        return readOf(BRIEF, reads === 1 ? "j1:a" : "j1:m");
      },
      artifact_save: () => saved({ merged: true, digest: "j1:m" }),
    });
    await editor.open("brief");
    editor.editField(titleField, "Заяц");
    await editor.save();
    expect(reads).toBe(2);
    expect(editor.getState().mergedNote).toBe(true);
    expect(editor.getState().saving).toBe(false);
  });

  it("переход к другому файлу при правках спрашивает; «Отбросить» открывает новый", async () => {
    const { editor } = setup({});
    await editor.open("brief");
    editor.editField(titleField, "Заяц");
    await editor.requestOpen("synopsis");
    expect(editor.getState().pendingKey).toBe("synopsis");
    expect(editor.getState().key).toBe("brief");
    await editor.resolvePending("stay");
    expect(editor.getState().pendingKey).toBeNull();
    expect(editor.getState().dirty).toBe(true);
    await editor.requestOpen("synopsis");
    await editor.resolvePending("discard");
    expect(editor.getState().key).toBe("synopsis");
    expect(editor.getState().dirty).toBe(false);
  });

  it("черновик: тот же digest возвращает правки, другой - кладёт в «Мои правки»", async () => {
    const drafts = createDraftStore();
    const a = setup({}, drafts);
    await a.editor.open("brief");
    a.editor.editField(titleField, "Заяц");
    a.editor.dispose();
    const b = setup({}, drafts);
    await b.editor.open("brief");
    expect(b.editor.getState().draftRestored).toBe(true);
    expect(b.editor.getState().dirty).toBe(true);
    b.editor.dispose();
    const c = setup({ artifact_read: () => readOf(BRIEF, "j1:other") }, drafts);
    await c.editor.open("brief");
    expect(c.editor.getState().draftRestored).toBe(false);
    expect(c.editor.getState().dirty).toBe(false);
    expect(c.editor.getState().myEdits).toContain("Заяц");
  });

  it("JSON -> форма с ошибкой в тексте: блокировка с позицией от сервера", async () => {
    const { editor } = setup({
      artifact_validate: () => ({
        artifact_key: "brief", valid: false, counts: { errors: 1, warnings: 0 }, truncated: false,
        findings: [{ severity: "error", code: "syntax", path: [], pointer: "", label: "", message: "Ожидалась запятая", line: 2, column: 5 }],
      }),
    });
    await editor.open("brief");
    await editor.setMode("json");
    editor.setText("{ oops");
    await editor.setMode("form");
    const s = editor.getState();
    expect(s.mode).toBe("json");
    expect(s.modeBlock).toContain("строка 2, столбец 5");
  });

  it("форма -> JSON -> форма не теряет правку", async () => {
    const { editor } = setup({});
    await editor.open("brief");
    editor.editField(titleField, "Заяц");
    await editor.setMode("json");
    expect(JSON.parse(editor.getState().text)).toEqual({ title: "Заяц", pages_min: 3 });
    await editor.setMode("form");
    expect(editor.getState().content).toEqual({ title: "Заяц", pages_min: 3 });
    expect(editor.getState().dirty).toBe(true);
  });

  it("просмотр версии не затирает рабочую копию", async () => {
    const { editor } = setup({
      artifact_history: () => ({ artifact_key: "brief", items: [{ version_id: "v1", created_at: "2026-01-02T10:05:00", size: 5, digest8: "abcd", reason: "save", reason_title: "Сохранение" }] }),
      artifact_read: (p) => (p.version_id ? readOf({ title: "Старое" }, "j1:old", { is_version: true, version_id: "v1", editable: false }) : readOf(BRIEF)),
    });
    await editor.open("brief");
    editor.editField(titleField, "Заяц");
    await editor.openHistory();
    await editor.viewVersion("v1");
    const s = editor.getState();
    expect(s.versionRead?.content).toEqual({ title: "Старое" });
    expect(s.versionDate).toBe("2026-01-02T10:05:00");
    expect(s.content).toEqual({ title: "Заяц", pages_min: 3 });
    editor.leaveVersion();
    expect(editor.getState().versionRead).toBeNull();
    expect(editor.getState().dirty).toBe(true);
  });

  it("откат версии, не проходящей проверку: сначала invalid, потом с allow_invalid", async () => {
    const errors = [{ severity: "error", code: "required", path: ["a"], pointer: "/a", label: "x", message: "Не заполнено" }];
    const attempts: unknown[] = [];
    const { editor } = setup({
      artifact_restore: (p) => {
        attempts.push(p.allow_invalid);
        if (!p.allow_invalid) throw new StudioError("invalid_argument", "Версия не проходит проверку", { errors });
        return saved({ restored_from: "v1" });
      },
    });
    await editor.open("brief");
    expect(await editor.restore("v1")).toBe("invalid");
    expect(await editor.restore("v1", { allowInvalid: true })).toBe("done");
    expect(attempts).toEqual([undefined, true]);
  });

  it("откат при активном прогоне: сообщение о блокировке, правки целы", async () => {
    const { editor, say } = setup({
      artifact_restore: () => {
        throw new StudioError("locked", "busy");
      },
    });
    await editor.open("brief");
    expect(await editor.restore("v1")).toBe("failed");
    expect(say).toHaveBeenCalledWith(expect.any(String), "error");
  });
});

// ответ, который приходит только когда тест его отпустит
const deferred = <T,>() => {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((r) => (resolve = r));
  return { promise, resolve };
};

describe("поздние ответы и устаревшие данные", () => {
  it("проверка файла A не пишется в файл B, открытый за время запроса", async () => {
    const gate = deferred<unknown>();
    const { editor } = setup({
      artifact_validate: () => gate.promise,
      artifact_read: (p) => readOf(p.key === "brief" ? BRIEF : { x: 1 }),
    });
    await editor.open("brief");
    const pending = editor.validate();
    await editor.open("synopsis");
    gate.resolve({ artifact_key: "brief", valid: false, counts: { errors: 1, warnings: 0 }, truncated: false, findings: [{ severity: "error", code: "type", path: [], pointer: "", label: "", message: "чужое" }] });
    expect(await pending).toBeNull();
    expect(editor.getState().key).toBe("synopsis");
    expect(editor.getState().findings).toEqual([]);
    expect(editor.getState().counts).toBeNull();
  });

  it("поздний просмотр версии не перебивает другой файл", async () => {
    const gate = deferred<unknown>();
    const { editor } = setup({ artifact_read: (p) => (p.version_id ? gate.promise : readOf(BRIEF)) });
    await editor.open("brief");
    const pending = editor.viewVersion("v1");
    await editor.open("synopsis");
    gate.resolve(readOf({ title: "Старое" }, "j1:old", { is_version: true, version_id: "v1" }));
    await pending;
    expect(editor.getState().versionRead).toBeNull();
  });

  it("поздний ответ перехода JSON -> форма не меняет режим другого файла", async () => {
    const gate = deferred<unknown>();
    const { editor } = setup({ artifact_validate: () => gate.promise });
    await editor.open("brief");
    await editor.setMode("json");
    editor.setText("{ oops");
    const pending = editor.setMode("form");
    await editor.open("synopsis");
    gate.resolve({ artifact_key: "brief", valid: false, counts: { errors: 1, warnings: 0 }, truncated: false, findings: [] });
    await pending;
    expect(editor.getState().modeBlock).toBeNull();
    expect(editor.getState().mode).toBe("form");
  });

  it("поздний откат не трогает экран другого файла", async () => {
    const gate = deferred<unknown>();
    const { editor, onSaved } = setup({ artifact_restore: () => gate.promise });
    await editor.open("brief");
    const pending = editor.restore("v1");
    await editor.open("synopsis");
    gate.resolve(saved());
    expect(await pending).toBe("done");
    expect(onSaved).toHaveBeenCalled();
    expect(editor.getState().key).toBe("synopsis");
    expect(editor.getState().history.open).toBe(false);
  });

  it("сравнение: ответ по старой версии не перебивает выбранную позже", async () => {
    const first = deferred<unknown>();
    const { editor } = setup({
      artifact_diff: (p) => (p.version_id === "v1" ? first.promise : { artifact_key: "brief", version_id: "v2", identical: true, changes: [], truncated: false }),
    });
    await editor.open("brief");
    const a = editor.compareWith("v1");
    await editor.compareWith("v2");
    first.resolve({ artifact_key: "brief", version_id: "v1", identical: false, changes: [], truncated: false });
    await a;
    expect(editor.getState().diff?.versionId).toBe("v2");
    expect(editor.getState().diff?.result?.identical).toBe(true);
  });

  it("после сохранения повреждённого файла баннер «повреждён» уходит", async () => {
    const { editor } = setup({
      artifact_read: () => readOf(null, "j1:x", { text: "{oops", parse_error: { line: 1, column: 2, message: "Ожидалось имя" } }),
      artifact_save: () => saved({ digest: "j1:fixed" }),
    });
    await editor.open("brief");
    editor.setText('{"title": "Лиса"}');
    expect(await editor.save()).toBe(true);
    expect(editor.getState().read?.parse_error).toBeNull();
    expect(editor.getState().read?.digest).toBe("j1:fixed");
  });

  it("StrictMode: dispose + activate, потом сохранение - старый черновик не всплывает «чужими правками»", async () => {
    const drafts = createDraftStore();
    let digest = "j1:a";
    const handlers = { artifact_read: () => readOf(BRIEF, digest), artifact_save: () => { digest = "j1:b"; return saved(); } };
    const a = setup(handlers, drafts);
    await a.editor.open("brief");
    a.editor.editField(titleField, "Заяц");
    a.editor.dispose();
    a.editor.activate();
    await a.editor.save();
    a.editor.dispose();
    const b = setup(handlers, drafts);
    await b.editor.open("brief");
    expect(b.editor.getState().myEdits).toBeNull();
    expect(b.editor.getState().draftRestored).toBe(false);
  });
});

describe("проверка всех файлов", () => {
  it("проверяет только существующие файлы, сбой одного не ломает остальные", async () => {
    const { client, calls } = fakeClient({
      artifact_validate: (p) => {
        if (p.key === "b") throw new StudioError("not_found", "Нет такого файла");
        return { artifact_key: p.key, valid: true, counts: { errors: 0, warnings: 1 }, findings: [], truncated: false };
      },
    });
    const items = [
      { artifact_key: "a", title: "A", exists: true },
      { artifact_key: "b", title: "B", exists: true },
      { artifact_key: "c", title: "C", exists: false },
    ] as never;
    const out = await validateAllFiles(client, "p", items);
    expect(calls.map((c) => c.payload.key).sort()).toEqual(["a", "b"]);
    expect(out.map((e) => [e.key, e.counts?.warnings ?? null, e.error !== null])).toEqual([["a", 1, false], ["b", null, true]]);
  });
});
