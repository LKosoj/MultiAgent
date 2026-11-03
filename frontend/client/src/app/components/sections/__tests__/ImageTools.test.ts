import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { ImageEditApi } from "../../../lib/storybook/useImageEdit";
import type { IllustrationsApi } from "../../../lib/storybook/useIllustrations";
import type { ShotMediaApi } from "../../../lib/storybook/useShotMedia";
import { ImageEditDialog } from "../storybook/imagetools/ImageEditDialog";
import { IllustrationsGallery } from "../storybook/imagetools/IllustrationsGallery";
import { ZoomImage } from "../storybook/imagetools/ZoomImage";

const h = (c: unknown, p: object) => renderToStaticMarkup(React.createElement(c as React.ComponentType<object>, p));
const noop = () => undefined;
const noEmoji = (html: string) => expect(html).not.toMatch(/[\u{1F300}-\u{1FAFF}]/u);

const media = { ready: true, thumb: () => null, media: (rel: string | null | undefined, rev?: string | null) => (rel ? `http://h/media?path=${rel}&t=tok${rev ? `&v=${rev}` : ""}` : null), onImageError: noop, bust: noop, refresh: async () => undefined } as unknown as ShotMediaApi;
const item = (name: string, page: number | null, extra: object = {}) => ({ path: `50_images/${name}`, name, page, size: 2048, modified: "2026-11-03T10:15:00Z", modified_ts: 5.5, servable: true, ...extra });
const list = (over: Partial<IllustrationsApi> = {}): IllustrationsApi => ({ status: "ready", items: [item("b.png", 2), item("a.png", 1)], error: null, reload: noop, ...over }) as IllustrationsApi;
const edit = (over: Partial<ImageEditApi> = {}): ImageEditApi =>
  ({
    open: true,
    phase: "ready",
    target: { kind: "illustration", path: "50_images/a.png", name: "a.png" },
    prompt: "",
    references: [],
    candidates: [{ path: "20_bible/hero.png", name: "hero.png", label: "20_bible/hero.png" }],
    preview: {
      target: { kind: "illustration", path: "50_images/a.png", name: "a.png", shot_key: "", part: "", size: 1, modified_ts: 1 },
      references: [],
      limits: { prompt_min: 5, prompt_max: 2000, references_max: 3, extensions: [".png"] },
      estimate: { cost_rub: 0, unknown_cost: true, price_per_image_rub: null },
      problems: [{ level: "warning", code: "linked_part", message: "Копии правка не затронет" }],
      can_start: true,
      plan_digest: "d",
    },
    button: { label: "Править без оценки", note: "edit_cost_unknown" },
    canSubmit: false,
    supported: true,
    error: null,
    running: null,
    begin: noop,
    setPrompt: noop,
    toggleRef: noop,
    start: async () => true,
    close: noop,
    ...over,
  }) as ImageEditApi;

describe("зум картинки", () => {
  it("кнопки зума, подпись масштаба и подсказка", () => {
    const html = h(ZoomImage, { src: "http://h/a.png", alt: "a" });
    for (const t of ["Приблизить", "Отдалить", "По размеру окна", "Реальный размер", "100%", "перетащите картинку мышью"]) expect(html).toContain(t);
    noEmoji(html);
  });
});

describe("окно правки", () => {
  it("цена неизвестна: кнопка «Править без оценки» и пояснение, по умолчанию в фокусе «Отмена»", () => {
    const html = h(ImageEditDialog, { api: edit(), imageUrl: "http://h/a.png" });
    expect(html).toContain("Правка картинки: a.png");
    expect(html).toContain("Править без оценки");
    expect(html).toContain("Цену оценить не удалось");
    expect(html).toContain("Копии правка не затронет");
    expect(html).toContain("Выбрано: 0 из 3");
    expect(html).toMatch(/data-autofocus="true"[^>]*>Отмена/);
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>Править без оценки/);
    noEmoji(html);
  });
  it("готово к запуску: кнопка доступна; цена с суммой", () => {
    const html = h(ImageEditDialog, { api: edit({ canSubmit: true, button: { label: "Править за 5 ₽", note: null } }), imageUrl: null });
    expect(html).not.toMatch(/<button[^>]*disabled=""[^>]*>Править за/);
    expect(html).toContain("Править за 5");
  });
  it("формат не поддерживается - предупреждение", () => {
    expect(h(ImageEditDialog, { api: edit({ supported: false }), imageUrl: null })).toContain("Править можно только PNG, JPG и WEBP.");
  });
});

describe("галерея иллюстраций", () => {
  it("счётчик, страницы по порядку, свойства и кнопка правки", () => {
    const html = h(IllustrationsGallery, { list: list(), edit: edit({ open: false }), media, runBusy: false });
    expect(html).toContain("Иллюстраций: 2");
    expect(html.indexOf("Страница 1")).toBeLessThan(html.indexOf("Страница 2"));
    expect(html).toContain("Файл: a.png");
    expect(html).toContain("Размер: 2,0");
    expect(html).toContain("&amp;v=5.5");
    expect(html).toContain("Править ИИ…");
    noEmoji(html);
  });
  it("идёт прогон: правка закрыта с причиной", () => {
    const html = h(IllustrationsGallery, { list: list(), edit: edit({ open: false }), media, runBusy: true });
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>Править ИИ…/);
    expect(html).toContain("уже работает другое приложение или прогон");
  });
  it("идёт правка: строка прогресса", () => {
    expect(h(IllustrationsGallery, { list: list(), edit: edit({ open: false, running: "a.png" }), media, runBusy: false })).toContain("Идёт правка картинки…");
  });
  it("пустая папка", () => {
    expect(h(IllustrationsGallery, { list: list({ items: [] }), edit: edit({ open: false }), media, runBusy: false })).toContain("В папке 50_images пока нет иллюстраций");
  });
  it("ошибка списка", () => {
    const html = h(IllustrationsGallery, { list: list({ status: "error", items: [], error: "нет связи" }), edit: edit({ open: false }), media, runBusy: false });
    expect(html).toContain("Не удалось получить список иллюстраций");
    expect(html).toContain("нет связи");
  });
});
