"use client";

import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import { IMAGE_TEXTS } from "../../../../lib/storybook/imageToolsTexts";
import {
  ZOOM_FIT,
  actualSize,
  clampPan,
  effectiveScale,
  panAllowed,
  stepZoom,
  zoomKeyAction,
  zoomPercent,
  type Size,
  type ZoomKeyAction,
  type ZoomState,
} from "../../../../lib/storybook/imageZoom";

type Props = {
  src: string;
  alt: string;
  onError?: (ev: { currentTarget: { src: string } }) => void;
  // картинка загрузилась: настоящие размеры в пикселях (для свойств файла)
  onSize?: (size: Size) => void;
  // клавиши +, -, 0, 1 ловим на всей странице (окно просмотра); иначе только когда фокус внутри
  globalKeys?: boolean;
};

const isTyping = (el: EventTarget | null): boolean => el instanceof HTMLElement && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));

// Картинка с зумом: кнопки, колесо мыши, клавиши +, -, 0, 1 и перетаскивание
export function ZoomImage({ src, alt, onError, onSize, globalKeys = false }: Props) {
  const stageRef = useRef<HTMLDivElement>(null);
  const [zoom, setZoom] = useState<ZoomState>(ZOOM_FIT);
  const [view, setView] = useState<Size>({ w: 0, h: 0 });
  const [img, setImg] = useState<Size>({ w: 0, h: 0 });
  const [dragging, setDragging] = useState(false);
  const drag = useRef<{ px: number; py: number; x: number; y: number } | null>(null);

  // новая картинка: снова «по размеру окна»
  useEffect(() => {
    setZoom(ZOOM_FIT);
    setImg({ w: 0, h: 0 });
  }, [src]);

  // размер окна: при режиме «по размеру окна» масштаб пересчитывается сам, ручной остаётся
  useLayoutEffect(() => {
    const el = stageRef.current;
    if (!el) return undefined;
    const measure = () => setView({ w: el.clientWidth, h: el.clientHeight });
    measure();
    if (typeof ResizeObserver === "undefined") return undefined;
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const scale = effectiveScale(zoom, view, img);
  const state = useRef({ zoom, view, img });
  state.current = { zoom, view, img };

  const apply = useCallback((action: ZoomKeyAction) => {
    const cur = state.current;
    if (action === "fit") setZoom(ZOOM_FIT);
    else if (action === "actual") setZoom(actualSize());
    else {
      const next = stepZoom(cur.zoom, action, cur.view, cur.img);
      setZoom({ ...next, ...clampPan(next.x, next.y, next.manual, cur.view, cur.img) });
    }
  }, []);

  // колесо: React вешает его пассивным, а страницу прокручивать не нужно
  useEffect(() => {
    const el = stageRef.current;
    if (!el) return undefined;
    const onWheel = (ev: WheelEvent) => {
      ev.preventDefault();
      apply(ev.deltaY < 0 ? "in" : "out");
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [apply]);

  useEffect(() => {
    if (!globalKeys) return undefined;
    const onKey = (ev: KeyboardEvent) => {
      if (ev.ctrlKey || ev.metaKey || ev.altKey || isTyping(ev.target)) return;
      const action = zoomKeyAction(ev.key);
      if (!action) return;
      ev.preventDefault();
      apply(action);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [globalKeys, apply]);

  const onKeyDown = (ev: React.KeyboardEvent) => {
    if (globalKeys || ev.ctrlKey || ev.metaKey || ev.altKey || isTyping(ev.target)) return;
    const action = zoomKeyAction(ev.key);
    if (!action) return;
    ev.preventDefault();
    apply(action);
  };

  const movable = panAllowed(scale, [view.w, view.h], [img.w, img.h]);
  const pan = movable ? clampPan(zoom.x, zoom.y, scale, view, img) : { x: 0, y: 0 };

  const onPointerDown = (ev: React.PointerEvent) => {
    if (!movable || ev.button !== 0) return;
    drag.current = { px: ev.clientX, py: ev.clientY, x: pan.x, y: pan.y };
    ev.currentTarget.setPointerCapture?.(ev.pointerId);
    setDragging(true);
  };
  const onPointerMove = (ev: React.PointerEvent) => {
    const d = drag.current;
    if (!d) return;
    const next = clampPan(d.x + ev.clientX - d.px, d.y + ev.clientY - d.py, scale, view, img);
    setZoom((z) => ({ ...z, mode: z.mode === "fit" ? "manual" : z.mode, manual: z.mode === "fit" ? scale : z.manual, ...next }));
  };
  const endDrag = () => {
    drag.current = null;
    setDragging(false);
  };

  const ready = img.w > 0 && img.h > 0;
  return (
    <div className="sb-imagetools-zoom">
      <div className="sb-imagetools-toolbar" role="group" aria-label={IMAGE_TEXTS.zoom_hint}>
        <button type="button" className="button ghost" onClick={() => apply("in")}>
          {IMAGE_TEXTS.zoom_in}
        </button>
        <button type="button" className="button ghost" onClick={() => apply("out")}>
          {IMAGE_TEXTS.zoom_out}
        </button>
        <button type="button" className="button ghost" aria-pressed={zoom.mode === "fit"} onClick={() => apply("fit")}>
          {IMAGE_TEXTS.zoom_fit}
        </button>
        <button type="button" className="button ghost" onClick={() => apply("actual")}>
          {IMAGE_TEXTS.zoom_actual}
        </button>
        <span className="sb-imagetools-level" aria-live="polite">
          {zoomPercent(scale)}
        </span>
      </div>
      <div
        ref={stageRef}
        className="sb-imagetools-stage"
        data-movable={movable ? "true" : "false"}
        data-dragging={dragging ? "true" : "false"}
        tabIndex={0}
        onKeyDown={onKeyDown}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
      >
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src={src}
          alt={alt}
          draggable={false}
          onError={onError}
          onLoad={(ev) => {
            const size = { w: ev.currentTarget.naturalWidth, h: ev.currentTarget.naturalHeight };
            setImg(size);
            onSize?.(size);
          }}
          style={ready ? { width: img.w * scale, height: img.h * scale, transform: `translate(${pan.x}px, ${pan.y}px)` } : undefined}
        />
      </div>
      <p className="sb-muted sb-imagetools-hint">{IMAGE_TEXTS.zoom_hint}</p>
    </div>
  );
}
