import { describe, expect, it } from "vitest";

import {
  ZOOM_FIT,
  ZOOM_MAX,
  ZOOM_MIN,
  actualSize,
  clampPan,
  effectiveScale,
  fitScale,
  panAllowed,
  stepZoom,
  zoomKeyAction,
  zoomPercent,
  zoomStep,
} from "../imageZoom";

describe("шаг зума", () => {
  it("шаг 1,25 в обе стороны", () => {
    expect(zoomStep(1, "in")).toBe(1.25);
    expect(zoomStep(1, "out")).toBe(0.8);
  });
  it("пределы 0,1 и 8", () => {
    expect(zoomStep(ZOOM_MAX, "in")).toBe(ZOOM_MAX);
    expect(zoomStep(7, "in")).toBe(ZOOM_MAX);
    expect(zoomStep(ZOOM_MIN, "out")).toBe(ZOOM_MIN);
    expect(zoomStep(0.11, "out")).toBe(ZOOM_MIN);
  });
  it("не число и не больше нуля считается масштабом 1", () => {
    expect(zoomStep(Number.NaN, "in")).toBe(1.25);
    expect(zoomStep(0, "out")).toBe(0.8);
    expect(zoomStep(-3, "in")).toBe(1.25);
    expect(zoomStep("2", "in")).toBe(1.25);
  });
});

describe("по размеру окна", () => {
  it("запас 0,95 и пределы", () => {
    expect(fitScale(800, 600, 1600, 1200)).toBe(0.475);
    expect(fitScale(800, 600, 400, 300)).toBe(1.9);
    expect(fitScale(10000, 10000, 10, 10)).toBe(ZOOM_MAX);
    expect(fitScale(10, 10, 100000, 100000)).toBe(ZOOM_MIN);
  });
  it("нулевые и неизвестные размеры дают 1", () => {
    expect(fitScale(0, 600, 10, 10)).toBe(1);
    expect(fitScale(800, 600, 0, 0)).toBe(1);
    expect(fitScale(Number.NaN, 600, 10, 10)).toBe(1);
  });
});

describe("подпись и сдвиг", () => {
  it("процент округляется", () => {
    expect(zoomPercent(1.25)).toBe("125%");
    expect(zoomPercent(0.1)).toBe("10%");
    expect(zoomPercent(Number.NaN)).toBe("100%");
  });
  it("двигать можно, только если картинка больше окна", () => {
    expect(panAllowed(1, [800, 600], [400, 300])).toBe(false);
    expect(panAllowed(2, [800, 600], [500, 300])).toBe(true);
    expect(panAllowed(0, [800, 600], [500, 300])).toBe(false);
    expect(panAllowed(2, [0, 600], [500, 300])).toBe(false);
  });
  it("сдвиг не уводит картинку за край, у маленькой картинки сдвига нет", () => {
    const view = { w: 800, h: 600 };
    expect(clampPan(9999, -9999, 2, view, { w: 500, h: 400 })).toEqual({ x: 100, y: -100 });
    expect(clampPan(50, 50, 1, view, { w: 500, h: 400 })).toEqual({ x: 0, y: 0 });
  });
});

describe("состояние зума", () => {
  const view = { w: 800, h: 600 };
  const img = { w: 1600, h: 1200 };
  it("по умолчанию масштаб считается по окну и следует за его размером", () => {
    expect(effectiveScale(ZOOM_FIT, view, img)).toBe(0.475);
    expect(effectiveScale(ZOOM_FIT, { w: 400, h: 300 }, img)).toBe(0.2375);
  });
  it("ручной масштаб при смене размера окна остаётся", () => {
    const manual = stepZoom(ZOOM_FIT, "in", view, img);
    expect(manual.mode).toBe("manual");
    expect(effectiveScale(manual, { w: 100, h: 100 }, img)).toBe(manual.manual);
  });
  it("шаг идёт от вычисленного масштаба «по окну»", () => {
    expect(stepZoom(ZOOM_FIT, "in", view, img).manual).toBe(0.5938);
  });
  it("реальный размер - 1 и сдвиг 0", () => {
    expect(actualSize()).toEqual({ mode: "manual", manual: 1, x: 0, y: 0 });
  });
  it("клавиши", () => {
    expect(zoomKeyAction("+")).toBe("in");
    expect(zoomKeyAction("=")).toBe("in");
    expect(zoomKeyAction("-")).toBe("out");
    expect(zoomKeyAction("0")).toBe("fit");
    expect(zoomKeyAction("1")).toBe("actual");
    expect(zoomKeyAction("a")).toBeNull();
  });
});
