"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { createBackgroundQueue, type BackgroundQueue } from "./backgroundQueue";
import { apiBaseFromAgentUrl, createStorybookClient, type StorybookClient } from "./client";

export type RunServiceAction = (
  action: string,
  payload: Record<string, unknown>,
  options?: { timeoutMs?: number; trackPending?: boolean },
) => Promise<unknown>;

export type StorybookClientApi = {
  client: StorybookClient;
  // пересоздаёт клиент: сбрасывает токены медиа (после перезапуска сервера)
  resetClient: () => void;
  background: BackgroundQueue;
  // обёртка для действий пользователя: фоновые запросы ждут её окончания
  runUser: <T>(task: () => Promise<T>) => Promise<T>;
};

export const useStorybookClient = (runServiceAction: RunServiceAction, backendUrl: string, seriesId?: string): StorybookClientApi => {
  const [epoch, setEpoch] = useState(0);
  const actionRef = useRef(runServiceAction);
  useEffect(() => {
    actionRef.current = runServiceAction;
  }, [runServiceAction]);
  const background = useMemo(() => createBackgroundQueue(), []);

  const client = useMemo(
    () => {
      void epoch; // новый номер = новый клиент с пустым кэшем токенов
      return createStorybookClient(
        // trackPending:false - опрос не должен делать всё приложение «занятым»
        (action, payload, opts) => actionRef.current(action, payload, { ...opts, trackPending: false }),
        { apiBase: apiBaseFromAgentUrl(backendUrl), seriesId },
      );
    },
    [backendUrl, epoch, seriesId],
  );

  const resetClient = useCallback(() => setEpoch((n) => n + 1), []);

  const runUser = useCallback(
    async <T,>(task: () => Promise<T>): Promise<T> => {
      background.userStart();
      try {
        return await task();
      } finally {
        background.userEnd();
      }
    },
    [background],
  );

  return useMemo(() => ({ client, resetClient, background, runUser }), [client, resetClient, background, runUser]);
};
