"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { artifactsList, type ArtifactItem } from "./artifacts";
import type { StorybookClient } from "./client";
import { classifyStorybookError } from "./uiErrors";

export type ArtifactsApi = {
  phase: "loading" | "ready" | "error";
  items: ArtifactItem[];
  error: string | null;
  reload: () => void;
  // тихое обновление списка (после сохранения): не показывает «Загрузка…»
  refreshQuiet: () => void;
};

// Список файлов проекта (реестр artifacts_list)
export const useArtifacts = (client: StorybookClient, projectId: string, active: boolean): ArtifactsApi => {
  const [phase, setPhase] = useState<ArtifactsApi["phase"]>("loading");
  const [items, setItems] = useState<ArtifactItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);
  const quietSeq = useRef(0);

  const fetchList = useCallback(
    async (quiet: boolean) => {
      // тихое обновление не отменяет чужую загрузку: у него свой счётчик
      const n = quiet ? seq.current : ++seq.current;
      const q = quiet ? ++quietSeq.current : quietSeq.current;
      if (!quiet) setPhase("loading");
      try {
        const res = await artifactsList(client, { project_id: projectId });
        if (n !== seq.current || (quiet && q !== quietSeq.current)) return;
        setItems(res.items);
        setError(null);
        setPhase("ready");
      } catch (err) {
        if (n !== seq.current || quiet) return;
        setError(classifyStorybookError(err).text);
        setPhase("error");
      }
    },
    [client, projectId],
  );

  useEffect(() => {
    if (active) void fetchList(false);
    return () => {
      seq.current += 1;
    };
  }, [active, fetchList]);

  const reload = useCallback(() => void fetchList(false), [fetchList]);
  const refreshQuiet = useCallback(() => void fetchList(true), [fetchList]);
  return { phase, items, error, reload, refreshQuiet };
};
