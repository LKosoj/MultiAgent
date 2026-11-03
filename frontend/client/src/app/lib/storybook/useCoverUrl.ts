"use client";

import { useEffect, useState } from "react";

import type { BackgroundQueue } from "./backgroundQueue";
import type { StorybookClient } from "./client";

// Адрес обложки. Токен проекта берём только когда карточка попала на экран и в фоновой очереди.
export const useCoverUrl = (
  client: StorybookClient,
  background: BackgroundQueue,
  projectId: string,
  relPath: string | null,
  visible: boolean,
): string | null => {
  const [url, setUrl] = useState<string | null>(null);

  useEffect(() => {
    if (!visible || !relPath) return;
    let cancelled = false;
    const existing = client.thumbUrl(projectId, relPath, 256);
    if (existing) {
      setUrl(existing);
      return;
    }
    background
      .run(() => client.ensureMediaToken(projectId))
      .then(() => {
        if (!cancelled) setUrl(client.thumbUrl(projectId, relPath, 256));
      })
      .catch(() => undefined); // без токена остаётся заглушка «Нет обложки»
    return () => {
      cancelled = true;
    };
  }, [client, background, projectId, relPath, visible]);

  return visible && relPath ? url : null;
};
