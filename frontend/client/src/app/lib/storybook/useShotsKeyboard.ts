"use client";

import { useEffect, useRef } from "react";

import { matchShotKey, type ShotKeyAction, type ShotKeyContext } from "./shotsKeys";

type Args = {
  enabled: boolean; // экран «Кадры» открыт в активном разделе
  getContext: () => Omit<ShotKeyContext, "active">;
  onAction: (action: ShotKeyAction) => void;
};

const tagOf = (target: EventTarget | null): string => {
  const el = target as HTMLElement | null;
  return el && typeof el.tagName === "string" ? el.tagName.toLowerCase() : "";
};

const isEditable = (target: EventTarget | null): boolean => {
  const tag = tagOf(target);
  return tag === "input" || tag === "textarea" || tag === "select" || (target as HTMLElement | null)?.isContentEditable === true;
};

const isControl = (target: EventTarget | null): boolean => {
  const tag = tagOf(target);
  return tag === "button" || tag === "a" || tag === "summary" || (target as HTMLElement | null)?.getAttribute?.("role") === "button";
};

// Отдельный слушатель экрана: shortcuts.ts оболочки не трогаем
export const useShotsKeyboard = ({ enabled, getContext, onAction }: Args): void => {
  const contextRef = useRef(getContext);
  const actionRef = useRef(onAction);
  useEffect(() => {
    contextRef.current = getContext;
    actionRef.current = onAction;
  }, [getContext, onAction]);

  useEffect(() => {
    if (!enabled) return;
    const handler = (ev: KeyboardEvent) => {
      const action = matchShotKey(
        {
          key: ev.key,
          code: ev.code,
          ctrlKey: ev.ctrlKey,
          metaKey: ev.metaKey,
          altKey: ev.altKey,
          shiftKey: ev.shiftKey,
          inEditable: isEditable(ev.target),
          inControl: isControl(ev.target),
          repeat: ev.repeat,
        },
        { ...contextRef.current(), active: true },
      );
      if (!action) return;
      ev.preventDefault();
      actionRef.current(action);
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [enabled]);
};
