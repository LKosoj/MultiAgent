"use client";

import { useEffect, useRef } from "react";

import { matchShortcut, type ShortcutAction, type ShortcutContext } from "./shortcuts";

type Args = {
  enabled: boolean; // раздел активен и пользователь админ
  getContext: () => Omit<ShortcutContext, "active">;
  onAction: (action: ShortcutAction) => void;
};

const isEditable = (target: EventTarget | null): boolean => {
  const el = target as HTMLElement | null;
  if (!el || typeof el.tagName !== "string") return false;
  const tag = el.tagName.toLowerCase();
  return tag === "input" || tag === "textarea" || tag === "select" || el.isContentEditable === true;
};

export const useStorybookShortcuts = ({ enabled, getContext, onAction }: Args): void => {
  const contextRef = useRef(getContext);
  const actionRef = useRef(onAction);
  useEffect(() => {
    contextRef.current = getContext;
    actionRef.current = onAction;
  }, [getContext, onAction]);

  useEffect(() => {
    if (!enabled) return;
    const handler = (ev: KeyboardEvent) => {
      const action = matchShortcut(
        {
          key: ev.key,
          code: ev.code,
          ctrlKey: ev.ctrlKey,
          metaKey: ev.metaKey,
          altKey: ev.altKey,
          shiftKey: ev.shiftKey,
          inEditable: isEditable(ev.target),
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
