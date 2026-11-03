// Навигация внутри раздела «Storybook»: восемь экранов, выбранный проект, переходы по цели.
import type { NextActionTarget } from "./status";

export const STORYBOOK_SCREENS = [
  "projects",
  "overview",
  "shots",
  "generation",
  "film",
  "blockout",
  "files",
  "settings",
] as const;

export type StorybookScreen = (typeof STORYBOOK_SCREENS)[number];

// Куда и с каким фильтром перейти; экраны Кадры/Генерация прочтут это позже
export type NavIntent = {
  screen: StorybookScreen;
  filter?: string | null;
  stepId?: string | null;
  mode?: string | null;
  shotKey?: string | null;
  // отмеченные на экране «Кадры» шоты: «Генерация» откроет перегенерацию именно их
  shotKeys?: string[] | null;
};

export type NavState = {
  projectId: string | null;
  screen: StorybookScreen;
  intent: NavIntent | null;
};

export type NavAction =
  | { type: "open_project"; projectId: string }
  | { type: "go"; screen: StorybookScreen }
  | { type: "clear_project" }
  | { type: "apply_target"; target: TargetLike }
  | { type: "open_shot"; shotKey: string };

export type TargetLike = Partial<Pick<NextActionTarget, "step_id">> & {
  mode?: string | null;
  screen: string;
  filter?: string | null;
  shot_keys?: string[] | null;
};

export type StorageLike = {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
};

export const NAV_STORAGE_KEY = "storybook-studio:nav:v1";
export const INITIAL_NAV: NavState = { projectId: null, screen: "projects", intent: null };

// Без выбранного проекта доступны только «Проекты» и «Настройки»
export const isScreenEnabled = (screen: StorybookScreen, projectId: string | null): boolean =>
  screen === "projects" || screen === "settings" || projectId !== null;

export const isScreen = (value: unknown): value is StorybookScreen =>
  typeof value === "string" && (STORYBOOK_SCREENS as readonly string[]).includes(value);

const SAFE_ID = /^[A-Za-z0-9_-]{1,128}$/;
export const isSafeProjectId = (value: unknown): value is string =>
  typeof value === "string" && SAFE_ID.test(value);

// Имена экранов в цели «следующего действия» (Python) отличаются: movie -> film
const TARGET_SCREENS: Record<string, StorybookScreen> = {
  overview: "overview",
  shots: "shots",
  generation: "generation",
  movie: "film",
  film: "film",
};

export const screenForTarget = (name: string): StorybookScreen => TARGET_SCREENS[name] ?? "overview";

export const navReducer = (state: NavState, action: NavAction): NavState => {
  switch (action.type) {
    case "open_project":
      if (!isSafeProjectId(action.projectId)) return state;
      return { projectId: action.projectId, screen: "overview", intent: null };
    case "go":
      if (!isScreenEnabled(action.screen, state.projectId)) return state;
      return { ...state, screen: action.screen, intent: null };
    case "clear_project":
      return { projectId: null, screen: "projects", intent: null };
    case "apply_target": {
      if (state.projectId === null) return state;
      const screen = screenForTarget(action.target.screen);
      return {
        ...state,
        screen,
        intent: {
          screen,
          filter: action.target.filter ?? null,
          stepId: action.target.step_id ?? null,
          mode: action.target.mode ?? null,
          shotKeys: action.target.shot_keys ?? null,
        },
      };
    }
    case "open_shot":
      if (state.projectId === null) return state;
      return { ...state, screen: "shots", intent: { screen: "shots", shotKey: action.shotKey } };
    default:
      return state;
  }
};

// Битое значение из хранилища превращается в начальное состояние
export const sanitizeNav = (raw: unknown): NavState => {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) return INITIAL_NAV;
  const { projectId, screen } = raw as Record<string, unknown>;
  const id = isSafeProjectId(projectId) ? projectId : null;
  if (!isScreen(screen)) return INITIAL_NAV;
  if (!isScreenEnabled(screen, id)) return INITIAL_NAV;
  return { projectId: id, screen, intent: null };
};

export const loadNav = (storage: StorageLike | null): NavState => {
  if (!storage) return INITIAL_NAV;
  try {
    const text = storage.getItem(NAV_STORAGE_KEY);
    return text ? sanitizeNav(JSON.parse(text)) : INITIAL_NAV;
  } catch {
    return INITIAL_NAV;
  }
};

export const saveNav = (storage: StorageLike | null, nav: NavState): void => {
  if (!storage) return;
  try {
    storage.setItem(NAV_STORAGE_KEY, JSON.stringify({ projectId: nav.projectId, screen: nav.screen }));
  } catch {
    // хранилище недоступно (приватный режим): работаем без сохранения
  }
};

// Выбранного проекта нет в списке (удалили в другом окне): тихо возвращаемся к «Проектам»
export const dropMissingProject = (nav: NavState, knownIds: readonly string[]): NavState =>
  nav.projectId !== null && !knownIds.includes(nav.projectId) ? INITIAL_NAV : nav;
