"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import type { StorybookClient } from "./client";
import { mediaInventory, type MediaItem } from "./media";
import { shotErrorFrom } from "./shotsView";

export type IllustrationsApi = {
  status: "idle" | "loading" | "ready" | "error";
  items: MediaItem[];
  error: string | null;
  reload: () => void;
};

type Args = { client: StorybookClient; runUser: <T>(task: () => Promise<T>) => Promise<T>; projectId: string; enabled: boolean };

// Иллюстрации книги (папка 50_images) из инвентаря медиа; размеры в пикселях сервер не читает (probe_limit: 0)
export const useIllustrations = ({ client, runUser, projectId, enabled }: Args): IllustrationsApi => {
  const [status, setStatus] = useState<IllustrationsApi["status"]>("idle");
  const [items, setItems] = useState<MediaItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const reload = useCallback(() => {
    const my = ++seq.current;
    setStatus((s) => (s === "ready" ? s : "loading"));
    setError(null);
    runUser(() => mediaInventory(client, { project_id: projectId, types: ["illustration"], limit: 2000, probe_limit: 0 }))
      .then((res) => {
        if (!alive.current || my !== seq.current) return;
        setItems(res.items);
        setStatus("ready");
      })
      .catch((err: unknown) => {
        if (!alive.current || my !== seq.current) return;
        setError(shotErrorFrom(err));
        setStatus("error");
      });
  }, [client, runUser, projectId]);

  useEffect(() => {
    if (enabled) reload();
  }, [enabled, reload]);

  return { status, items, error, reload };
};
