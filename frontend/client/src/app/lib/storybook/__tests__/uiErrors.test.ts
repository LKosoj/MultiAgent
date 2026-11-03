import { describe, expect, it } from "vitest";

import { StudioError } from "../client";
import { classifyStorybookError, isNoRights } from "../uiErrors";
import { NO_CONNECTION_TEXT, NO_RIGHTS_TEXT, PROJECT_LOCKED_TEXT } from "../texts";

describe("classifyStorybookError", () => {
  it("нет прав", () => {
    const err = new Error("service action 'storybook.projects_list' requires role 'admin'");
    expect(isNoRights(err)).toBe(true);
    expect(classifyStorybookError(err)).toMatchObject({ kind: "forbidden", text: NO_RIGHTS_TEXT });
    expect(isNoRights(new StudioError("x", "requires role"))).toBe(false);
  });

  it("таймаут и сеть", () => {
    expect(classifyStorybookError(new Error("Таймаут запроса: storybook.x")).kind).toBe("timeout");
    const net = classifyStorybookError(new TypeError("Failed to fetch"));
    expect(net).toMatchObject({ kind: "network", text: NO_CONNECTION_TEXT });
  });

  it("коды studio", () => {
    expect(classifyStorybookError(new StudioError("locked", "что-то"))).toMatchObject({
      kind: "locked",
      text: PROJECT_LOCKED_TEXT,
    });
    expect(classifyStorybookError(new StudioError("conflict", "Данные изменились")).kind).toBe("conflict");
    expect(classifyStorybookError(new StudioError("not_found", "Проект не найден: p")).text).toBe("Проект не найден: p");
    const bad = classifyStorybookError(new StudioError("invalid_argument", "Поле «Название» должно быть длиннее"));
    expect(bad).toMatchObject({ kind: "invalid", text: "Поле «Название» должно быть длиннее" });
    expect(classifyStorybookError(new StudioError("unavailable", "Нет места")).kind).toBe("unavailable");
    expect(classifyStorybookError(new StudioError("aborted", "Отменено")).kind).toBe("aborted");
    expect(classifyStorybookError(new StudioError("interrupted", "Прервано"))).toMatchObject({ kind: "interrupted", text: expect.stringContaining("Проверьте проект и повторите") });
  });

  it("not_found сохраняет details (путь папки)", () => {
    const e = classifyStorybookError(new StudioError("not_found", "Папка", { path: "/x" }));
    expect(e.details).toEqual({ path: "/x" });
  });

  it("неизвестное не показывает stack", () => {
    const e = new Error("boom\n    at secret.ts:1");
    const c = classifyStorybookError(e);
    expect(c.kind).toBe("other");
    expect(c.text).not.toContain("secret");
    expect(classifyStorybookError("строка").kind).toBe("other");
  });
});
