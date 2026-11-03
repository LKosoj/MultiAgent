import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { createArtifactEditor, createDraftStore, type EditorState } from "../../../lib/storybook/artifactEditor";
import { buildFields } from "../../../lib/storybook/artifactForm";
import type { StorybookClient } from "../../../lib/storybook/client";
import { formFromSettings } from "../../../lib/storybook/settingsView";
import { FilesTab } from "../storybook/files/FilesTab";
import { SettingsTab } from "../storybook/settings/SettingsTab";

const h = (c: unknown, p: object) => renderToStaticMarkup(React.createElement(c as React.ComponentType<object>, p));
const noop = () => undefined;
const noEmoji = (html: string) => expect(html).not.toMatch(/[\u{1F300}-\u{1FAFF}]/u);

const ITEMS = [
  { artifact_key: "brief", title: "Бриф", path: "brief.json", exists: true, status: "present", size: 1, modified: null, editable: true, read_only_reason: "", careful: false, root: "object", writers: [], produced_by: "", run_state: "free", run_state_message: "", history_count: 0, error: null },
  { artifact_key: "shots", title: "Шоты", path: "shots.json", exists: false, status: "missing", size: 0, modified: null, editable: true, read_only_reason: "", careful: true, root: "object", writers: [], produced_by: "Шоты", run_state: "free", run_state_message: "", history_count: 0, error: null },
];
const list = (over: object = {}) => ({ phase: "ready", items: ITEMS, error: null, reload: noop, refreshQuiet: noop, ...over });
const validateAll = { state: { phase: "idle" }, run: noop, close: noop };

// редактор с готовым состоянием: вызывать сеть не нужно, компонент только читает getState
const editorWith = (patch: Partial<Record<keyof EditorState, unknown>>) => {
  const editor = createArtifactEditor({ client: {} as StorybookClient, projectId: "p", drafts: createDraftStore() });
  return { editor, state: { ...editor.getState(), ...patch } as EditorState };
};
const read = (over: object = {}): never => ({
  artifact_key: "brief", title: "Бриф", path: "brief.json", exists: true, digest: "j1:a", size: 10, modified: null, editable: true,
  read_only_reason: "", content: { title: "Лиса", pages_min: 3 }, text: "", parse_error: null, is_version: false, version_id: null,
  run_state: "free", run_state_message: "", ...over,
}) as never;

describe("экран «Файлы»", () => {
  it("список: файлы со статусом и значком, без выбранного файла - подсказка", () => {
    const html = h(FilesTab, { list: list(), ed: editorWith({}), validateAll, say: noop });
    expect(html).toContain("Бриф");
    expect(html).toContain("Нет");
    expect(html).toContain("Осторожно");
    expect(html).toContain("Выберите файл слева.");
    expect(html).toContain("Проверить все файлы");
    noEmoji(html);
  });
  it("ошибка списка: текст и повтор", () => {
    const html = h(FilesTab, { list: list({ phase: "error", error: "Нет связи" }), ed: editorWith({}), validateAll, say: noop });
    expect(html).toContain("Нет связи");
    expect(html).toContain("Повторить");
  });
  it("файл ещё не создан: называет шаг", () => {
    const html = h(FilesTab, { list: list(), ed: editorWith({ key: "shots", phase: "ready", read: read({ exists: false }) }), validateAll, say: noop });
    expect(html).toContain("Файл ещё не создан. Он появится после шага «Шоты».");
  });
  it("форма: подписи из таблицы схем, подсказка про списки, pointer только в title", () => {
    const content = { title: "Лиса", pages_min: 3 };
    const html = h(FilesTab, {
      list: list(),
      ed: editorWith({ key: "brief", phase: "ready", read: read(), content, baseContent: content, mode: "form", fields: buildFields("brief", content) }),
      validateAll,
      say: noop,
    });
    expect(html).toContain("Название");
    expect(html).toContain("Страниц не меньше");
    expect(html).toContain("Добавлять, удалять и переставлять элементы списков можно в режиме JSON.");
    expect(html).toContain('title="/pages_min"');
    expect(html).not.toContain(">/pages_min<");
    noEmoji(html);
  });
  it("повреждённый файл: баннер с позицией, форма недоступна", () => {
    const html = h(FilesTab, {
      list: list(),
      ed: editorWith({
        key: "brief", phase: "ready", mode: "json", text: "{oops",
        read: read({ content: null, text: "{oops", parse_error: { line: 1, column: 2, message: "Ожидалось имя" } }),
      }),
      validateAll,
      say: noop,
    });
    expect(html).toContain("строка 1, столбец 2: Ожидалось имя");
    expect(html).toContain("Строка 1, столбец 1");
  });
  it("только чтение: кнопка «Сохранить» заблокирована", () => {
    const html = h(FilesTab, {
      list: list(),
      ed: editorWith({ key: "brief", phase: "ready", mode: "json", text: "{}", read: read({ editable: false, read_only_reason: "Создаётся пайплайном" }) }),
      validateAll,
      say: noop,
    });
    expect(html).toContain("Только чтение: Создаётся пайплайном");
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>Сохранить<\/button>/);
  });
  it("конфликт и несохранённые правки показывают диалоги", () => {
    const html = h(FilesTab, {
      list: list(),
      ed: editorWith({ key: "brief", phase: "ready", mode: "json", text: "{}", read: read(), conflict: true, pendingKey: "shots", myEdits: "{\"a\": 1}" }),
      validateAll,
      say: noop,
    });
    expect(html).toContain("Обновить файл");
    expect(html).toContain("Сохранить правки?");
    expect(html).toContain("Мои правки");
  });
  it("«Сохранить всё равно…» только при ошибках без корневых и синтаксических", () => {
    const err = (code: string) => ({ severity: "error", code, path: [], pointer: "", label: "", message: "m" }) as never;
    const mk = (code: string) =>
      h(FilesTab, {
        list: list(),
        ed: editorWith({ key: "brief", phase: "ready", mode: "json", text: "{}", read: read(), saveProblem: { text: "Не прошёл проверку", errors: [err(code)] }, findings: [err(code)] }),
        validateAll,
        say: noop,
      });
    expect(mk("type")).toContain("Сохранить всё равно…");
    expect(mk("root")).not.toContain("Сохранить всё равно…");
    expect(mk("syntax")).not.toContain("Сохранить всё равно…");
  });
  it("два редактора JSON на экране различаются подписью", () => {
    const content = { title: "Лиса" };
    const html = h(FilesTab, {
      list: list(),
      ed: editorWith({ key: "brief", phase: "ready", read: read(), content, baseContent: content, mode: "form", fields: buildFields("brief", content), myEdits: "{}" }),
      validateAll,
      say: noop,
    });
    const labels = [...html.matchAll(/<textarea[^>]*aria-label="([^"]+)"/g)].map((m) => m[1]);
    expect(new Set(labels).size).toBe(labels.length);
    expect(labels.length).toBeGreaterThanOrEqual(2);
  });
  it("итог проверки всех файлов", () => {
    const entries = [{ key: "brief", title: "Бриф", counts: { errors: 2, warnings: 1 }, error: null }];
    const html = h(FilesTab, {
      list: list(),
      ed: editorWith({}),
      validateAll: { ...validateAll, state: { phase: "done", entries, summary: { checked: 1, errors: 2, warnings: 1, passed: false, problems: entries } } },
      say: noop,
    });
    expect(html).toContain("Проверено файлов: 1. Ошибок: 2, замечаний: 1.");
  });
});

const SNAP = {
  settings: {
    projects_directory: { value: "/data/p", default: "/data/p", source: "env" },
    backup_directory: { value: "/data/b", default: "/x", source: "file" },
    logs_directory: { value: "/data/l", default: "/data/l", source: "default" },
    max_backup_files: { value: 5, default: 5, source: "file" },
    log_level: { value: "info", default: "INFO", source: "file" },
  },
  checks: {}, warnings: ["Прежние бэкапы остались в /old"], digest: "s1:x", projects_directory_locked_reason: null,
};
const settingsApi = (over: object = {}) => ({
  phase: "ready", error: null, snapshot: SNAP, form: formFromSettings(SNAP as never), dirty: false, fieldErrors: {},
  locks: { projects_directory: { disabled: true, note: "Папка задана переменной окружения сервера" }, backup_directory: { disabled: false, note: null }, logs_directory: { disabled: false, note: null }, max_backup_files: { disabled: false, note: null }, log_level: { disabled: false, note: null } },
  checks: { projects_directory: { tone: "ok", text: "Папка доступна", resolved: "/data/p" }, backup_directory: null, logs_directory: { tone: "warn", text: "Папки не существует", resolved: null } },
  saving: false, conflict: false, forbidden: false, resetOpen: false, resetError: null,
  setField: noop, blurField: noop, save: noop, reload: noop, refreshAfterConflict: noop, openReset: noop, closeReset: noop, confirmReset: noop, ...over,
});

describe("экран «Настройки»", () => {
  it("поля, подписи уровней журнала, предупреждения сервера и пометка Tk", () => {
    const html = h(SettingsTab, { api: settingsApi() });
    for (const t of ["Папка проектов", "Папка бэкапов", "Папка журналов", "Число бэкапов", "Уровень журнала", "Подробный", "Обычный", "Предупреждения", "Только ошибки", "Критические", "Прежние бэкапы остались в /old", "Параметры окна Tk"]) {
      expect(html).toContain(t);
    }
    expect(html).toContain("Папка доступна");
    expect(html).toContain("Папки не существует");
    expect(html).toContain("по умолчанию");
    noEmoji(html);
  });
  it("папка проектов из окружения заблокирована с пояснением", () => {
    const html = h(SettingsTab, { api: settingsApi() });
    expect(html).toContain("Папка задана переменной окружения сервера");
    expect(html).toMatch(/<input[^>]*value="\/data\/p"[^>]*disabled=""|<input[^>]*disabled=""[^>]*value="\/data\/p"/);
  });
  it("несохранённые изменения: пометка в заголовке и кнопка «Сохранить» активна", () => {
    const html = h(SettingsTab, { api: settingsApi({ dirty: true }) });
    expect(html).toContain("не сохранено");
    expect(html).toContain("Есть несохранённые изменения.");
    expect(html).not.toMatch(/<button[^>]*disabled=""[^>]*>Сохранить<\/button>/);
  });
  it("без изменений «Сохранить» заблокирована", () => {
    expect(h(SettingsTab, { api: settingsApi() })).toMatch(/<button[^>]*disabled=""[^>]*>Сохранить<\/button>/);
  });
  it("ошибка поля и конфликт", () => {
    const html = h(SettingsTab, { api: settingsApi({ conflict: true, fieldErrors: { max_backup_files: "Поле «Число бэкапов» должно быть целым от 1 до 100." } }) });
    expect(html).toContain("Данные изменили после того, как вы их открыли.");
    expect(html).toContain('role="alert"');
    expect(html).toContain("должно быть целым от 1 до 100");
  });
  it("ошибка сохранения: баннер с причиной", () => {
    const html = h(SettingsTab, { api: settingsApi({ saveError: "Нет связи." }) });
    expect(html).toContain("Не удалось сохранить настройки: Нет связи.");
    expect(h(SettingsTab, { api: settingsApi() })).not.toContain("Не удалось сохранить");
  });
  it("во время сохранения поля закрыты", () => {
    const html = h(SettingsTab, { api: settingsApi({ saving: true }) });
    expect(html).toMatch(/<input[^>]*value="\/data\/b"[^>]*disabled=""|<input[^>]*disabled=""[^>]*value="\/data\/b"/);
    expect(html).toMatch(/<select[^>]*disabled=""/);
    expect(html).toMatch(/<input[^>]*type="number"[^>]*disabled=""|<input[^>]*disabled=""[^>]*type="number"/);
  });
  it("нет прав: только чтение", () => {
    const html = h(SettingsTab, { api: settingsApi({ forbidden: true }) });
    expect(html).toContain("Настройки может менять администратор");
    expect(html).toMatch(/<select[^>]*disabled=""/);
  });
  it("загрузка и ошибка загрузки", () => {
    expect(h(SettingsTab, { api: settingsApi({ phase: "loading", snapshot: null }) })).toContain("Загрузка настроек…");
    const err = h(SettingsTab, { api: settingsApi({ phase: "error", snapshot: null, error: "Нет связи" }) });
    expect(err).toContain("Нет связи");
    expect(err).toContain("Повторить");
  });
});
