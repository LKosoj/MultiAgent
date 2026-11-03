import { describe, expect, it } from "vitest";

import { fileInfoLines, galleryFilterItems, galleryGroupItems, naturalCompare } from "../galleryView";

const item = (name: string, page: number | null) => ({ path: `50_images/${name}`, name, page });

describe("галерея иллюстраций", () => {
  it("группы по страницам, без номера в конце, имена по естественному порядку", () => {
    const groups = galleryGroupItems([item("c10.png", 3), item("c9.png", 3), item("z.png", null), item("a.png", 1)]);
    expect(groups.map((g) => g.title)).toEqual(["Страница 1", "Страница 3", "Без номера страницы"]);
    expect(groups[1].items.map((i) => i.name)).toEqual(["c9.png", "c10.png"]);
  });
  it("пустой список - нет групп", () => {
    expect(galleryGroupItems([])).toEqual([]);
  });
  it("естественное сравнение не зависит от регистра", () => {
    expect(["B2.png", "a10.png", "a9.png"].sort(naturalCompare)).toEqual(["a9.png", "a10.png", "B2.png"]);
  });
  it("поиск без учёта регистра и пробелов по краям", () => {
    const all = [item("Cat.png", 1), item("dog.png", 1)];
    expect(galleryFilterItems(all, "  CAT ").map((i) => i.name)).toEqual(["Cat.png"]);
    expect(galleryFilterItems(all, "").length).toBe(2);
    expect(galleryFilterItems(all, null).length).toBe(2);
  });
  it("свойства: размеры в пикселях только если известны", () => {
    expect(fileInfoLines("a.png", "1 КБ", "—", 10, 20)).toHaveLength(4);
    expect(fileInfoLines("a.png", "1 КБ", "—", null, null)).toHaveLength(3);
    expect(fileInfoLines("a.png", "1 КБ", "—", 0, 5)).toHaveLength(3);
  });
});
