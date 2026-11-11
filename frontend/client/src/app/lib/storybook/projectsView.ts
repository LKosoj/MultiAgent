// Представление списка проектов: поиск, сортировка, доступность действий, подписи карточки.
import type { ProjectCard } from "./projects";
import { BUSY_HINT, T } from "./texts";
import { formatBytes, formatDateRu } from "./format";

export type ProjectSort = "modified" | "name" | "size" | "series";
export const PROJECT_SORTS: { value: ProjectSort; label: string }[] = [
  { value: "modified", label: T.sortModified },
  { value: "name", label: T.sortName },
  { value: "size", label: T.sortSize },
  { value: "series", label: "Число серий" },
];

export const isProjectSort = (value: unknown): value is ProjectSort =>
  PROJECT_SORTS.some((s) => s.value === value);

// Поиск без учёта регистра по названию и id, как на сервере
export const filterProjects = (list: readonly ProjectCard[], query: string): ProjectCard[] => {
  const q = query.trim().toLowerCase();
  if (!q) return [...list];
  return list.filter((p) => p.title.toLowerCase().includes(q) || p.project_id.toLowerCase().includes(q));
};

const byTitle = (a: ProjectCard, b: ProjectCard) => a.title.localeCompare(b.title, "ru", { sensitivity: "base" });

// Сортировка стабильная; «Размер»: неизвестный размер в конец
export const sortProjects = (
  list: readonly ProjectCard[],
  sort: ProjectSort,
  sizes: ReadonlyMap<string, number> | Readonly<Record<string, number | undefined>> = {},
): ProjectCard[] => {
  const sizeOf = (id: string): number | undefined =>
    sizes instanceof Map ? sizes.get(id) : (sizes as Record<string, number | undefined>)[id];
  const indexed = list.map((p, i) => ({ p, i }));
  indexed.sort((x, y) => {
    let diff = 0;
    if (sort === "name") diff = byTitle(x.p, y.p);
    else if (sort === "series") diff = y.p.series_count - x.p.series_count || byTitle(x.p, y.p);
    else if (sort === "size") {
      const sx = sizeOf(x.p.project_id);
      const sy = sizeOf(y.p.project_id);
      if (sx === undefined && sy === undefined) diff = 0;
      else if (sx === undefined) diff = 1;
      else if (sy === undefined) diff = -1;
      else diff = sy - sx;
    } else diff = x.p.modified < y.p.modified ? 1 : x.p.modified > y.p.modified ? -1 : 0;
    return diff || x.i - y.i;
  });
  return indexed.map((x) => x.p);
};

export type ActionGate = { allowed: boolean; reason: string | null };

// Удалять и переименовывать нельзя, пока идёт прогон или проект ещё создаётся
export const canDelete = (card: ProjectCard): ActionGate =>
  card.busy
    ? { allowed: false, reason: BUSY_HINT }
    : card.state === "creating"
      ? { allowed: false, reason: "Проект ещё создаётся." }
      : { allowed: true, reason: null };

export const canRename = canDelete;

export const cardSubtitle = (card: ProjectCard, sizeBytes?: number): string => {
  const parts = [`Серий: ${card.series_count}`];
  parts.push(sizeBytes === undefined ? "—" : formatBytes(sizeBytes));
  return parts.join(" · ");
};

export const cardModified = (card: ProjectCard): string => `${T.modified} ${formatDateRu(card.modified)}`;

export const pendingSizeCount = (
  list: readonly ProjectCard[],
  sizes: ReadonlyMap<string, number>,
): { done: number; total: number } => ({
  done: list.filter((p) => sizes.has(p.project_id)).length,
  total: list.length,
});
