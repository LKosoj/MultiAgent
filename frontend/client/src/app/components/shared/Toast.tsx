"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

export type ToastItem = {
  id: string;
  msg: string;
  type: "error" | "success" | "info";
};

type ToastProps = {
  toasts: ToastItem[];
  dismiss: (id: string) => void;
};

function ToastList({ toasts, dismiss }: ToastProps) {
  return (
    <div className="toast-container">
      {toasts.map((t) => (
        <div
          key={t.id}
          className={`toast toast--${t.type}`}
          role="alert"
          onClick={() => dismiss(t.id)}
        >
          {t.msg}
        </div>
      ))}
    </div>
  );
}

export function Toast({ toasts, dismiss }: ToastProps) {
  const [mounted, setMounted] = useState(false);
  useEffect(() => { setMounted(true); }, []);
  if (!mounted || toasts.length === 0) return null;
  return createPortal(<ToastList toasts={toasts} dismiss={dismiss} />, document.body);
}

export function useToast() {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const timers = useRef<Map<string, number>>(new Map());

  const dismiss = useCallback((id: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
    const timer = timers.current.get(id);
    if (timer !== undefined) {
      window.clearTimeout(timer);
      timers.current.delete(id);
    }
  }, []);

  const notify = useCallback((msg: string, type: ToastItem["type"] = "info") => {
    const id = crypto.randomUUID();
    setToasts((prev) => [...prev.slice(-4), { id, msg, type }]);
    const timer = window.setTimeout(() => dismiss(id), 5000) as unknown as number;
    timers.current.set(id, timer);
  }, [dismiss]);

  useEffect(() => {
    return () => {
      timers.current.forEach((t) => window.clearTimeout(t));
    };
  }, []);

  return { toasts, dismiss, notify };
}
