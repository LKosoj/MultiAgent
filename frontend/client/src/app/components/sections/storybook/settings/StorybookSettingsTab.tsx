"use client";

import React from "react";

import type { StorybookClient } from "../../../../lib/storybook/client";
import { useAppSettings } from "../../../../lib/storybook/useAppSettings";
import { SettingsTab } from "./SettingsTab";

type Props = {
  client: StorybookClient;
  runUser: <T>(task: () => Promise<T>) => Promise<T>;
  active: boolean;
  say: (text: string, type?: "error" | "success" | "info") => void;
  onSettingsChanged: (changed: string[]) => void;
};

// Контейнер экрана «Настройки»: хук ходит в сеть, компонент только рисует
export function StorybookSettingsTab(p: Props) {
  const api = useAppSettings({ client: p.client, runUser: p.runUser, active: p.active, say: p.say, onChanged: p.onSettingsChanged });
  return <SettingsTab api={api} />;
}
