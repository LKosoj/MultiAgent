import { describe, expect, it } from "vitest";

import * as texts from "../texts";

const collect = (value: unknown, out: string[] = []): string[] => {
  if (typeof value === "string") out.push(value);
  else if (Array.isArray(value)) value.forEach((v) => collect(v, out));
  else if (value && typeof value === "object") Object.values(value).forEach((v) => collect(v, out));
  return out;
};

describe("тексты раздела", () => {
  it("ключевые фразы совпадают со спекой", () => {
    expect(texts.NO_RIGHTS_TEXT).toBe("Недостаточно прав: это действие доступно администратору.");
    expect(texts.T.emptyProjects).toBe("Проектов пока нет. Создайте проект, затем добавьте в него серию и её материал.");
    expect(texts.T.emptyOverview).toBe("Проект ещё не запускался. Настройте запуск и оцените стоимость.");
    expect(texts.T.loadingStatus).toBe("Загрузка состояния проекта…");
    expect(texts.T.noRuns).toBe("Прогон ещё не запускался.");
    expect(texts.BUSY_HINT).toBe("Недоступно, пока идёт прогон.");
    expect(texts.SELECT_PROJECT_HINT).toBe("Сначала выберите проект");
    expect(texts.nothingFound("лис")).toBe("Ничего не найдено по запросу «лис».");
    expect(texts.notFoundFolderText("/srv/p")).toBe("Папка проектов не найдена: /srv/p.");
    expect(texts.loadProjectsFailed("нет связи")).toBe("Не удалось загрузить список проектов: нет связи.");
    expect(texts.loadStatusFailed("сбой")).toBe("Не удалось прочитать состояние проекта: сбой.");
    expect(texts.deleteTitle("Лиса")).toBe("Удалить проект «Лиса»?");
    expect(texts.deleteBody("8.0 МБ")).toContain("Перед удалением будет создан бэкап (8.0 МБ).");
    expect(texts.restoreTitle("27.10.2026")).toBe("Восстановить бэкап от 27.10.2026?");
    expect(texts.toasts.removed("8.0 МБ")).toBe("Проект удалён. Бэкап создан (8.0 МБ)");
    expect(texts.toasts.exported("a.zip")).toBe("Экспорт готов: a.zip");
  });

  it("в строках нет эмодзи и восклицательных знаков", () => {
    const all = collect(texts);
    expect(all.length).toBeGreaterThan(100);
    for (const s of all) {
      expect(s, s).not.toMatch(/!/);
      expect(s, s).not.toMatch(/\p{Extended_Pictographic}/u);
    }
  });

  it("неизвестный код заметки выводится как есть", () => {
    expect(texts.stepNoteLabel("bypassed")).toBe("Обойдён другими шагами");
    expect(texts.stepNoteLabel("новый_код")).toBe("новый_код");
  });
});
