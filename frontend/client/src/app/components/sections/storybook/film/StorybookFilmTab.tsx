"use client";

import React from "react";

import type { BackgroundQueue } from "../../../../lib/storybook/backgroundQueue";
import type { StorybookClient } from "../../../../lib/storybook/client";
import type { RunState } from "../../../../lib/storybook/status";
import { useRefreshTick } from "../../../../lib/storybook/useRefreshTick";
import { useFilm } from "../../../../lib/storybook/useFilm";
import { FilmTab } from "./FilmTab";

type Props = {
  client: StorybookClient;
  background: BackgroundQueue;
  runUser: <T>(task: () => Promise<T>) => Promise<T>;
  projectId: string;
  projectName: string;
  active: boolean;
  refreshTick: number;
  runState: RunState | undefined;
  runSteps: readonly string[] | null | undefined;
  say: (text: string, type?: "error" | "success" | "info") => void;
  onGoGeneration: () => void;
  onOpenShot: (key: string) => void;
  onOpenFiles: () => void;
  // после запуска перегенерации или монтажа оболочка перечитывает состояние прогона
  onRunStarted: () => void;
};

// Контейнер экрана «Фильм»: хук ходит в сеть, компонент только рисует
export function StorybookFilmTab(p: Props) {
  const api = useFilm({
    client: p.client,
    background: p.background,
    runUser: p.runUser,
    projectId: p.projectId,
    active: p.active,
    runState: p.runState,
    runSteps: p.runSteps,
    say: p.say,
    onRunStarted: p.onRunStarted,
  });
  useRefreshTick(p.refreshTick, api.reload);
  return (
    <FilmTab api={api} projectName={p.projectName} onGoGeneration={p.onGoGeneration} onOpenShot={p.onOpenShot} onOpenReport={p.onOpenFiles} />
  );
}
