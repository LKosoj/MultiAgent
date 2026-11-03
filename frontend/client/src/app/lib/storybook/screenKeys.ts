// Горячие клавиши экранов «Файлы» и «Настройки»: Ctrl+S сохраняет, F5 перечитывает.
export type ScreenKey = { key: string; ctrlKey: boolean; metaKey: boolean };
export type ScreenKeyResult = { swallow: boolean; action: "save" | "reload" | null };

// Нажатие всегда глушим (иначе браузер сохранит страницу или перезагрузит её),
// а действие выполняем, только если поверх экрана нет модального окна.
export function resolveScreenKey(e: ScreenKey, modalOpen: boolean): ScreenKeyResult {
  const save = (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s";
  const reload = e.key === "F5" && !e.ctrlKey && !e.metaKey;
  if (!save && !reload) return { swallow: false, action: null };
  if (modalOpen) return { swallow: true, action: null };
  return { swallow: true, action: save ? "save" : "reload" };
}

// Что обновляет F5 на экране: список проектов, статус проекта, данные самого экрана (через счётчик) или ничего
export type RefreshKind = "projects" | "overview" | "tick" | "none";
export const refreshKindOf = (screen: string): RefreshKind => {
  switch (screen) {
    case "projects":
      return "projects";
    case "overview":
      return "overview";
    case "shots":
    case "generation":
    case "film":
    case "blockout":
      return "tick";
    default:
      return "none";
  }
};

// F5 в «Кадрах»: список перечитываем всегда, карточку - только если в ней нет несохранённой правки
export const shotsRefreshPlan = (cardDirty: boolean): { list: boolean; card: boolean } => ({ list: true, card: !cardDirty });
