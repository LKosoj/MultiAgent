import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { ConfirmDialog } from "../storybook/ConfirmDialog";
import { StudioModal } from "../storybook/StudioModal";

describe("StudioModal", () => {
  it("диалог с заголовком и кнопкой закрытия", () => {
    const html = renderToStaticMarkup(
      React.createElement(StudioModal, { title: "Заголовок", onClose: () => undefined }, React.createElement("p", null, "текст")),
    );
    expect(html).toContain('role="dialog"');
    expect(html).toContain('aria-modal="true"');
    expect(html).toContain("Заголовок");
    expect(html).toContain('aria-label="Закрыть"');
  });
  it("во время операции кнопки закрытия нет", () => {
    const html = renderToStaticMarkup(
      React.createElement(StudioModal, { title: "T", onClose: () => undefined, closable: false }),
    );
    expect(html).not.toContain('aria-label="Закрыть"');
  });
});

describe("ConfirmDialog", () => {
  it("автофокус на «Отмена», не на опасной кнопке", () => {
    const html = renderToStaticMarkup(
      React.createElement(ConfirmDialog, {
        title: "Удалить?",
        body: "Тело",
        confirmLabel: "Удалить",
        onConfirm: () => undefined,
        onCancel: () => undefined,
      }),
    );
    const cancel = html.indexOf("Отмена");
    const marker = html.indexOf('data-autofocus="true"');
    expect(marker).toBeGreaterThan(-1);
    expect(marker).toBeLessThan(cancel);
    expect(html.split('data-autofocus="true"').length).toBe(2);
  });
});
