"use client";

import { useEffect, useState } from "react";
import { createTextToSqlClient } from "../../lib/textToSqlClient";
import {
  textToSqlHistoryEntries,
  type TextToSqlHistorySummary,
} from "../../lib/textToSqlContracts";

type UseTextToSqlHistoryParams = {
  tab: string;
  textToSqlClient: ReturnType<typeof createTextToSqlClient>;
  setError: (msg: string | null) => void;
};

export function useTextToSqlHistory({
  tab,
  textToSqlClient,
  setError,
}: UseTextToSqlHistoryParams) {
  const [history, setHistory] = useState<TextToSqlHistorySummary[]>([]);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const [analytics, setAnalytics] = useState<any | null>(null);
  const [loadedHistory, setLoadedHistory] = useState(false);

  const loadHistory = async () => {
    setError(null);
    try {
      const resp = await textToSqlClient.listHistory(100, 0);
      setHistory(textToSqlHistoryEntries(resp));
      const analyticsResp = await textToSqlClient.historyAnalytics();
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      setAnalytics((analyticsResp as any)?.result ?? analyticsResp);
      setLoadedHistory(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось получить историю");
    }
  };

  const clearHistory = async () => {
    setError(null);
    try {
      await textToSqlClient.clearHistory();
      setHistory([]);
      setAnalytics(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось очистить историю");
    }
  };

  useEffect(() => {
    if (tab === "history" && !loadedHistory) {
      void loadHistory();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, loadedHistory]);

  return { history, analytics, loadHistory, clearHistory };
}
