// Горячие клавиши раздела: чистая функция по полям события, без привязки к DOM.
import { STORYBOOK_SCREENS, type StorybookScreen } from "./nav";

export type ShortcutAction =
  | { type: "screen"; screen: StorybookScreen }
  | { type: "palette" }
  | { type: "refresh" }
  | { type: "create_prompt" }
  | { type: "focus_search" };

export type ShortcutEvent = {
  key: string;
  code?: string;
  ctrlKey: boolean;
  metaKey: boolean;
  altKey: boolean;
  shiftKey: boolean;
  // true, если фокус в input/textarea/select/contenteditable
  inEditable: boolean;
};

export type ShortcutContext = {
  active: boolean;
  modalOpen: boolean;
  focusInside: boolean; // фокус внутри раздела (нужно для F5)
  screen: StorybookScreen;
  hasProject: boolean;
};

const digitIndex = (ev: ShortcutEvent): number | null => {
  const fromCode = /^(?:Digit|Numpad)([1-8])$/.exec(ev.code ?? "");
  if (fromCode) return Number(fromCode[1]) - 1;
  return /^[1-8]$/.test(ev.key) ? Number(ev.key) - 1 : null;
};

const isKey = (ev: ShortcutEvent, letter: string, code: string): boolean =>
  ev.code === code || ev.key.toLowerCase() === letter;

export const matchShortcut = (ev: ShortcutEvent, ctx: ShortcutContext): ShortcutAction | null => {
  if (!ctx.active || ctx.modalOpen) return null;
  const ctrl = ev.ctrlKey || ev.metaKey;
  const anyMod = ctrl || ev.altKey;

  if (ev.key === "F5" && !anyMod && !ev.shiftKey) {
    return ctx.focusInside ? { type: "refresh" } : null;
  }
  if (ev.shiftKey && ev.key !== "/") return null;

  // Alt+1..8 (и Ctrl/Cmd+1..8, если браузер отдал событие)
  if (anyMod && !(ev.altKey && ctrl)) {
    // Alt+Numpad в поле ввода на Windows набирает символ по коду
    if (ev.altKey && ev.inEditable && /^Numpad/.test(ev.code ?? "")) return null;
    const idx = digitIndex(ev);
    if (idx !== null) {
      const screen = STORYBOOK_SCREENS[idx];
      const enabled = screen === "projects" || screen === "settings" || ctx.hasProject;
      return enabled ? { type: "screen", screen } : null;
    }
    if (ctrl && !ev.altKey && isKey(ev, "k", "KeyK")) return { type: "palette" };
    if (isKey(ev, "n", "KeyN") && ctx.screen === "projects") return { type: "create_prompt" };
    return null;
  }

  if (!anyMod && !ev.inEditable && ev.key === "/" && ctx.screen === "projects") {
    return { type: "focus_search" };
  }
  return null;
};
