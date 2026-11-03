// Горячие клавиши экрана «Кадры»: чистая функция по полям события.
// Работает по event.code, поэтому русская раскладка не мешает. shortcuts.ts (оболочка) не трогаем.
import type { ReviewStatus } from "./review";

export type ShotKeyAction =
  | { type: "move"; delta: 1 | -1 }
  | { type: "status"; letter: "a" | "r" | "x"; status: ReviewStatus }
  | { type: "mark" }
  | { type: "mark_all" }
  | { type: "open" }
  | { type: "save" };

export type ShotKeyEvent = {
  key: string;
  code?: string;
  ctrlKey: boolean;
  metaKey: boolean;
  altKey: boolean;
  shiftKey: boolean;
  // фокус в input/textarea/select/contenteditable
  inEditable: boolean;
  // фокус на кнопке или ссылке: Enter и Пробел должны нажать её
  inControl?: boolean;
  // автоповтор при удержании клавиши
  repeat?: boolean;
};

export type ShotKeyContext = {
  active: boolean; // экран «Кадры» открыт
  modalOpen: boolean;
  focusInside: boolean; // фокус внутри раздела (или на странице без фокуса)
};

const LETTERS: Record<string, { letter: "a" | "r" | "x"; status: ReviewStatus }> = {
  KeyA: { letter: "a", status: "approved" },
  KeyR: { letter: "r", status: "redo" },
  KeyX: { letter: "x", status: "rejected" },
};

const codeOf = (ev: ShotKeyEvent): string => {
  if (ev.code) return ev.code;
  const k = ev.key;
  if (/^[a-z]$/i.test(k)) return `Key${k.toUpperCase()}`;
  if (k === " ") return "Space";
  return k;
};

// Удержание клавиши повторяет только стрелки; остальные действия срабатывают один раз
export const matchShotKey = (ev: ShotKeyEvent, ctx: ShotKeyContext): ShotKeyAction | null => {
  const action = matchShotKeyOnce(ev, ctx);
  if (action && ev.repeat && action.type !== "move") return null;
  return action;
};

const matchShotKeyOnce = (ev: ShotKeyEvent, ctx: ShotKeyContext): ShotKeyAction | null => {
  if (!ctx.active || ctx.modalOpen || !ctx.focusInside) return null;
  const code = codeOf(ev);
  const ctrl = ev.ctrlKey || ev.metaKey;
  if (ev.altKey) return null;

  // Ctrl/Cmd+S работает и в полях ввода
  if (ctrl) {
    if (ev.shiftKey) return null;
    if (code === "KeyS") return { type: "save" };
    if (code === "KeyA" && !ev.inEditable) return { type: "mark_all" };
    return null;
  }
  if (ev.shiftKey || ev.inEditable) return null;

  if (code === "ArrowDown") return { type: "move", delta: 1 };
  if (code === "ArrowUp") return { type: "move", delta: -1 };
  const status = LETTERS[code];
  if (status) return { type: "status", ...status };
  if (ev.inControl) return null;
  if (code === "Space") return { type: "mark" };
  if (code === "Enter") return { type: "open" };
  return null;
};
