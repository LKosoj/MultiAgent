// Золотые файлы У10 (tests/fixtures/storybook_screens/): те же случаи проверяет Tk-версия.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { ARTIFACT_LABELS } from "../artifactLabels";
import * as form from "../artifactForm";
import { FILES_TEXTS } from "../filesTexts";
import * as fv from "../filesView";
import { SETTINGS_TEXTS } from "../settingsTexts";
import * as sv from "../settingsView";

type Case = { in: unknown[]; out: unknown };
const load = (name: string) =>
  JSON.parse(readFileSync(fileURLToPath(new URL(`../../../../../../../tests/fixtures/storybook_screens/${name}`, import.meta.url)), "utf-8"));
const J = (v: unknown) => JSON.parse(JSON.stringify(v));
// все случаи прогоняются через функцию; результат сравнивается с золотым
const check = (cases: Case[], f: (...a: never[]) => unknown) => {
  expect(cases.length).toBeGreaterThan(0);
  for (const c of cases) expect(J(f(...(c.in as never[])))).toEqual(c.out);
};

describe("паритет «Файлов» и «Настроек»: тексты", () => {
  it("тексты «Файлов» совпадают с золотым", () => {
    expect({ ...FILES_TEXTS }).toEqual(load("golden_files_texts.json"));
  });
  it("тексты «Настроек» совпадают с золотым", () => {
    expect({ ...SETTINGS_TEXTS }).toEqual(load("golden_settings_texts.json"));
  });
  it("в тексте нет эмодзи", () => {
    for (const t of [...Object.values(FILES_TEXTS), ...Object.values(SETTINGS_TEXTS)]) expect(t).not.toMatch(/[\u{1F300}-\u{1FAFF}]/u);
  });
});

describe("паритет: алгоритм формы", () => {
  const fx = load("golden_files_form_vectors.json") as { contents: Record<string, { key: string; content: unknown }>; vectors: Record<string, Case[]> };
  const v = fx.vectors;
  it("состав полей формы", () => check(v.build_fields, form.buildFields as never));
  it("правка поля", () => {
    expect(v.apply_input.length).toBeGreaterThan(10);
    for (const c of v.apply_input) {
      const [key, content, pointer, input] = c.in as [string, unknown, string, string | boolean];
      const field = form.buildFields(key, content)!.find((f) => f.pointer === pointer)!;
      expect(J(form.applyInput(content, field, input))).toEqual(c.out);
    }
  });
  it("позиции в тексте JSON", () => {
    check(v.json_offset_of, form.jsonOffsetOf as never);
    check(v.line_col_at, form.lineColAt as never);
    check(v.offset_at, form.offsetAt as never);
  });
  it("числа, указатели, подписи", () => {
    check(v.parse_number, form.parseNumber as never);
    check(v.pointer_of, form.pointerOf as never);
    check(v.template_of, form.templateOf as never);
    check(v.label_for, form.labelFor as never);
    check(v.is_long_string, form.isLongString as never);
    check(v.content_to_text, form.contentToText as never);
  });
  it("правка не трогает остальное и порядок ключей", () => {
    const { key, content } = fx.contents.brief_sample;
    const field = form.buildFields(key, content)!.find((f) => f.pointer === "/pages_min")!;
    const next = form.applyInput(content, field, "9").content as Record<string, unknown>;
    expect(Object.keys(next)).toEqual(Object.keys(content as object));
    expect(next.title).toBe((content as Record<string, unknown>).title);
  });
  it("таблица подписей не пуста для каждого файла со схемой", () => {
    for (const [k, t] of Object.entries(ARTIFACT_LABELS)) expect(t[""], k).toBeTruthy();
  });
});

describe("паритет: логика экрана «Файлы»", () => {
  const fx = load("golden_files_vectors.json") as { server_findings: Record<string, { findings: unknown[] }>; vectors: Record<string, Case[]> };
  const v = fx.vectors;
  it("простые функции", () => {
    check(v.list_rows, fv.listRows as never);
    check(v.parse_error_text, fv.parseErrorText as never);
    check(v.resolve_editor_screen, fv.resolveEditorScreen as never);
    check(v.editor_banners, fv.editorBanners as never);
    check(v.editor_access, fv.editorAccess as never);
    check(v.counts_text, fv.countsText as never);
    check(v.ancestor_pointers, fv.ancestorPointers as never);
    check(v.cursor_for_finding, fv.cursorForFinding as never);
    check(v.version_rows, fv.versionRows as never);
    check(v.format_diff_value, fv.formatDiffValue as never);
    check(v.diff_rows, fv.diffRows as never);
    check(v.validate_all_text, fv.validateAllText as never);
  });
  it("находки сервера: строки, разрешение «Сохранить всё равно», счётчик", () => {
    check(v.finding_rows, fv.findingRows as never);
    check(v.can_save_anyway, fv.canSaveAnyway as never);
    check(v.error_count, fv.errorCount as never);
  });
  it("корневая ошибка закрывает «Сохранить всё равно»", () => {
    expect(fv.canSaveAnyway(fx.server_findings.brief_root.findings as never)).toBe(false);
    expect(fv.canSaveAnyway(fx.server_findings.brief_bad.findings as never)).toBe(true);
  });
  it("проверка всех файлов и список для проверки", () => {
    check(v.summarize_validate_all, fv.summarizeValidateAll as never);
    check(v.files_to_validate, ((i: never) => fv.filesToValidate(i).map((x) => x.artifact_key)) as never);
  });
});

describe("паритет: логика экрана «Настройки»", () => {
  const v = (load("golden_settings_vectors.json") as { vectors: Record<string, Case[]> }).vectors;
  it("форма и изменения", () => {
    check(v.form_from_settings, sv.formFromSettings as never);
    check(v.changed_fields, sv.changedFields as never);
    check(v.field_lock, sv.fieldLock as never);
    check(v.default_mark, sv.defaultMark as never);
  });
  it("запрос на сохранение", () => check(v.build_update, sv.buildUpdate as never));
  it("ошибки сервера и проверка пути", () => {
    check(v.field_for_server_error, sv.fieldForServerError as never);
    check(v.path_check_view, sv.pathCheckView as never);
    check(v.log_level_label, sv.logLevelLabel as never);
    check(v.should_check_path, sv.shouldCheckPath as never);
  });
});
