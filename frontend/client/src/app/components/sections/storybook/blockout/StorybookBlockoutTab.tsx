"use client";

import React from "react";

import type { BackgroundQueue } from "../../../../lib/storybook/backgroundQueue";
import type { StorybookClient } from "../../../../lib/storybook/client";
import type { RunState } from "../../../../lib/storybook/status";
import { useRefreshTick } from "../../../../lib/storybook/useRefreshTick";
import { useBlockout } from "../../../../lib/storybook/useBlockout";
import { BlockoutTab } from "./BlockoutTab";

type Props = {
  client: StorybookClient;
  background: BackgroundQueue;
  runUser: <T>(task: () => Promise<T>) => Promise<T>;
  projectId: string;
  active: boolean;
  refreshTick: number;
  runState: RunState | undefined;
  say: (text: string, type?: "error" | "success" | "info") => void;
  onGoGeneration: () => void;
  onOpenShot: (key: string) => void;
  onRunStarted: () => void;
};

// Контейнер экрана «Болванка»: хук ходит в сеть, компонент только рисует
export function StorybookBlockoutTab(p: Props) {
  const api = useBlockout({
    client: p.client,
    background: p.background,
    runUser: p.runUser,
    projectId: p.projectId,
    active: p.active,
    runState: p.runState,
    say: p.say,
    onRunStarted: p.onRunStarted,
  });
  useRefreshTick(p.refreshTick, api.reload);
  return <BlockoutTab api={api} onGoGeneration={p.onGoGeneration} onOpenShot={p.onOpenShot} />;
}
