// Чистая логика галереи иллюстраций (папка 50_images): группы по страницам, поиск, строки свойств.
// Те же правила, что в Tk (tests/fixtures/storybook_screens/golden_imagetools_vectors.json).
import { IMAGE_TEXTS, imageFmt } from "./imageToolsTexts";

export type GalleryItem = { path: string; name: string; page: number | null };
export type GalleryGroup<T extends GalleryItem = GalleryItem> = { title: string; page: number | null; items: T[] };

// Естественный порядок имён: c9 раньше c10, регистр не важен
const chunks = (name: string): [number, number, string][] =>
  name
    .toLowerCase()
    .split(/(\d+)/)
    .filter((c) => c !== "")
    .map((c): [number, number, string] => (/^\d+$/.test(c) ? [0, Number(c), ""] : [1, 0, c]));

export const naturalCompare = (a: string, b: string): number => {
  const ca = chunks(a);
  const cb = chunks(b);
  for (let i = 0; i < Math.min(ca.length, cb.length); i += 1) {
    for (let k = 0; k < 3; k += 1) {
      if (ca[i][k] < cb[i][k]) return -1;
      if (ca[i][k] > cb[i][k]) return 1;
    }
  }
  return ca.length - cb.length;
};

const pageOf = (item: GalleryItem): number | null => (Number.isInteger(item.page) ? item.page : null);

// Группы по номеру страницы по возрастанию, без номера в конце; внутри естественный порядок имён
export const galleryGroupItems = <T extends GalleryItem>(items: readonly T[]): GalleryGroup<T>[] => {
  const pages = new Map<number | null, T[]>();
  for (const item of items) {
    const page = pageOf(item);
    const list = pages.get(page);
    if (list) list.push(item);
    else pages.set(page, [item]);
  }
  const numbered = [...pages.keys()].filter((p): p is number => p !== null).sort((a, b) => a - b);
  const order: (number | null)[] = pages.has(null) ? [...numbered, null] : numbered;
  return order.map((page) => ({
    title: page === null ? IMAGE_TEXTS.gallery_page_other : imageFmt("gallery_page", { n: page }),
    page,
    items: [...(pages.get(page) as T[])].sort((a, b) => naturalCompare(a.name, b.name)),
  }));
};

// Для общих векторов: в группах только имена
export const galleryGroups = (items: readonly GalleryItem[]) =>
  galleryGroupItems(items).map((g) => ({ title: g.title, page: g.page, items: g.items.map((i) => i.name) }));

export const galleryFilterItems = <T extends GalleryItem>(items: readonly T[], query: unknown): T[] => {
  const needle = typeof query === "string" ? query.trim().toLowerCase() : "";
  return items.filter((i) => i.name.toLowerCase().includes(needle));
};

export const galleryFilter = (items: readonly GalleryItem[], query: unknown): string[] => galleryFilterItems(items, query).map((i) => i.name);

// Строки свойств; размер и дату вызывающий форматирует своими форматтерами, размеры в пикселях - если известны
export const fileInfoLines = (name: string, sizeText: string, modifiedText: string, w: unknown, h: unknown): string[] => {
  const lines = [imageFmt("info_name", { v: name }), imageFmt("info_size", { v: sizeText }), imageFmt("info_modified", { v: modifiedText })];
  if (typeof w === "number" && typeof h === "number" && Number.isFinite(w) && Number.isFinite(h) && w > 0 && h > 0) {
    lines.push(imageFmt("info_dimensions", { w: Math.trunc(w), h: Math.trunc(h) }));
  }
  return lines;
};
