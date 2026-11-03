"use client";

import { useEffect, useRef } from "react";

// F5 в разделе: когда счётчик вырос, вызываем обновление данных экрана (первое значение не считается)
export const useRefreshTick = (tick: number, onTick: () => void): void => {
  const last = useRef(tick);
  const cb = useRef(onTick);
  cb.current = onTick;
  useEffect(() => {
    if (tick === last.current) return;
    last.current = tick;
    cb.current();
  }, [tick]);
};
