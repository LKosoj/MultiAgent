"use client";

import React, { useEffect, useId, useRef } from "react";

import { trapTarget } from "../../../lib/storybook/focusTrap";
import { T } from "../../../lib/storybook/texts";

type Props = {
  title: string;
  onClose: () => void;
  children?: React.ReactNode;
  wide?: boolean;
  // false: закрыть нельзя (идёт операция)
  closable?: boolean;
};

const FOCUSABLE =
  'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

// Окно: фокус внутри, Esc закрывает, фокус возвращается на кнопку, открывшую окно
export function StudioModal({ title, onClose, children, wide = false, closable = true }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const closeRef = useRef(onClose);
  const closableRef = useRef(closable);
  useEffect(() => {
    closeRef.current = onClose;
    closableRef.current = closable;
  });

  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    const root = ref.current;
    const first = root?.querySelector<HTMLElement>("[data-autofocus]") ?? root?.querySelector<HTMLElement>(FOCUSABLE);
    first?.focus();
    return () => {
      if (opener && document.contains(opener)) opener.focus();
    };
  }, []);

  // слушаем document на время жизни окна: после клика по тексту фокус уходит с кнопок
  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => {
      const root = ref.current;
      if (!root || root.getClientRects().length === 0) return; // раздел скрыт
      const all = document.querySelectorAll("[data-sb-modal]");
      if (all[all.length - 1] !== root.parentElement) return; // реагирует только верхнее окно
      if (ev.key === "Escape") {
        ev.stopPropagation();
        if (closableRef.current) closeRef.current();
        return;
      }
      if (ev.key !== "Tab") return;
      const items = Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE));
      const active = document.activeElement as HTMLElement | null;
      const target = trapTarget(items, active, ev.shiftKey);
      if (target) {
        ev.preventDefault();
        target.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  return (
    <div className="sb-modal-overlay" data-sb-modal="true">
      <div
        ref={ref}
        className="sb-modal"
        data-wide={wide ? "true" : "false"}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
      >
        <div className="sb-modal-head">
          <h2 id={titleId} className="sb-title">
            {title}
          </h2>
          {closable ? (
            <button type="button" className="button ghost" style={{ minWidth: 0, height: 32 }} onClick={onClose} aria-label={T.close}>
              ×
            </button>
          ) : null}
        </div>
        {children}
      </div>
    </div>
  );
}
