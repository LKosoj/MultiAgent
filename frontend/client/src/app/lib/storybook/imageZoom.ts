// Чистая модель зума картинки (без окон). Правила те же, что в Tk:
// tests/fixtures/storybook_screens/golden_imagetools_vectors.json.
import { imageFmt } from "./imageToolsTexts";

export const ZOOM_STEP = 1.25;
export const ZOOM_MIN = 0.1;
export const ZOOM_MAX = 8;
export const FIT_MARGIN = 0.95;

export type Size = { w: number; h: number };

const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
const clamp = (scale: number): number => Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, scale));
// четыре знака после запятой: те же значения, что в эталоне
const round4 = (v: number): number => Math.floor(v * 10000 + 0.5) / 10000;

// Шаг 1,25 с пределами 0,1…8; не число или не больше нуля считается масштабом 1
export const zoomStep = (scale: unknown, direction: "in" | "out"): number => {
  const base = isNum(scale) && scale > 0 ? scale : 1;
  return round4(clamp(direction === "in" ? base * ZOOM_STEP : base / ZOOM_STEP));
};

// По размеру окна с запасом 0,95; нулевые или неизвестные размеры дают 1
export const fitScale = (viewW: unknown, viewH: unknown, imgW: unknown, imgH: unknown): number => {
  if (![viewW, viewH, imgW, imgH].every((v) => isNum(v) && v > 0)) return 1;
  const [vw, vh, iw, ih] = [viewW, viewH, imgW, imgH] as number[];
  return round4(clamp(Math.min(vw / iw, vh / ih) * FIT_MARGIN));
};

export const zoomPercent = (scale: unknown): string =>
  imageFmt("zoom_level", { pct: Math.floor((isNum(scale) ? scale : 1) * 100 + 0.5) });

// Двигать картинку можно, только когда она больше окна хотя бы по одной стороне
export const panAllowed = (scale: unknown, view: readonly [number, number], img: readonly [number, number]): boolean => {
  if (!(isNum(scale) && scale > 0)) return false;
  const [vw, vh] = view;
  const [iw, ih] = img;
  if (![vw, vh, iw, ih].every((v) => isNum(v) && v > 0)) return false;
  return iw * scale > vw + 1e-9 || ih * scale > vh + 1e-9;
};

// ---- состояние зума в окне ----

// fit: масштаб считается по окну; manual: пользователь менял масштаб сам
export type ZoomState = { mode: "fit" | "manual"; manual: number; x: number; y: number };

export const ZOOM_FIT: ZoomState = { mode: "fit", manual: 1, x: 0, y: 0 };

export const effectiveScale = (state: ZoomState, view: Size, img: Size): number =>
  state.mode === "fit" ? fitScale(view.w, view.h, img.w, img.h) : state.manual;

// Приблизить или отдалить от текущего (в том числе вычисленного «по окну») масштаба
export const stepZoom = (state: ZoomState, direction: "in" | "out", view: Size, img: Size): ZoomState => {
  const next = zoomStep(effectiveScale(state, view, img), direction);
  return { mode: "manual", manual: next, x: state.x, y: state.y };
};

export const actualSize = (): ZoomState => ({ mode: "manual", manual: 1, x: 0, y: 0 });

// Сдвиг не выходит за край: картинка не уезжает из окна целиком
export const clampPan = (x: number, y: number, scale: number, view: Size, img: Size): { x: number; y: number } => {
  if (!panAllowed(scale, [view.w, view.h], [img.w, img.h])) return { x: 0, y: 0 };
  const limit = (imgSide: number, viewSide: number) => Math.max(0, (imgSide * scale - viewSide) / 2);
  const lx = limit(img.w, view.w);
  const ly = limit(img.h, view.h);
  return { x: Math.min(lx, Math.max(-lx, x)), y: Math.min(ly, Math.max(-ly, y)) };
};

// Клавиши зума; null - клавиша не наша
export type ZoomKeyAction = "in" | "out" | "fit" | "actual";
export const zoomKeyAction = (key: string): ZoomKeyAction | null => {
  if (key === "+" || key === "=") return "in";
  if (key === "-" || key === "_") return "out";
  if (key === "0") return "fit";
  if (key === "1") return "actual";
  return null;
};
