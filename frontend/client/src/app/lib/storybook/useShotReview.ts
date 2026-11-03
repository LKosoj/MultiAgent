"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import type { StorybookClient } from "./client";
import { reviewSetStatus, type ReviewCounts, type ReviewSetStatusResult, type ReviewStatus } from "./review";
import type { ShotRow } from "./shots";
import { createSingleFlight } from "./singleFlight";
import { fmt } from "./shotsTexts";
import { applyReviewStatus, reviewToastId, shotErrorFrom } from "./shotsView";

type Say = (text: string, type?: "error" | "success" | "info") => void;

export type ShotReviewApi = {
  busy: boolean;
  // keys - шоты, digests - отпечатки записей ревью, если они известны (одиночная смена).
  // null: действие не выполнено (ошибка или предыдущее ещё идёт)
  setStatus: (keys: string[], status: ReviewStatus, digests?: Record<string, string>) => Promise<ReviewSetStatusResult | null>;
  counts: ReviewCounts | null;
};

type Args = {
  client: StorybookClient;
  runUser: <T>(task: () => Promise<T>) => Promise<T>;
  projectId: string;
  say: Say;
  // подписи статусов от сервера (из списка шотов)
  getTitles: () => Record<string, string>;
  patchRows: (fn: (rows: ShotRow[]) => ShotRow[]) => void;
  // после успеха или конфликта: перечитать ревью открытой карточки и список
  onChanged: () => void;
};

// Статус сразу меняется в списке; при ошибке строки возвращаются, как были
export const useShotReview = ({ client, runUser, projectId, say, getTitles, patchRows, onChanged }: Args): ShotReviewApi => {
  const [busy, setBusy] = useState(false);
  const [counts, setCounts] = useState<ReviewCounts | null>(null);
  const alive = useRef(true);
  const flight = useRef(createSingleFlight()).current;
  const titlesRef = useRef(getTitles);
  const changedRef = useRef(onChanged);
  useEffect(() => {
    titlesRef.current = getTitles;
    changedRef.current = onChanged;
  });
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const setStatus = useCallback(
    async (keys: string[], status: ReviewStatus, digests?: Record<string, string>): Promise<ReviewSetStatusResult | null> => {
      if (keys.length === 0) return null;
      const out = await flight.run(async () => {
        setBusy(true);
        // прежние статусы нужны для отката
        const before: Record<string, ShotRow["review"]> = {};
        patchRows((rows) => {
          for (const r of rows) if (keys.includes(r.shot_key)) before[r.shot_key] = r.review;
          return applyReviewStatus(rows, keys, status, titlesRef.current());
        });
        try {
          const res = await runUser(() =>
            reviewSetStatus(client, {
              project_id: projectId,
              keys,
              status,
              ...(digests ? { expected_digests: digests } : {}),
              surface: "react",
            }),
          );
          if (alive.current) {
            setCounts(res.counts);
            if (keys.length > 1) say(fmt(reviewToastId(status, true), { n: res.updated.length }), "success");
          }
          return res;
        } catch (err) {
          patchRows((rows) => rows.map((r) => (r.shot_key in before ? { ...r, review: before[r.shot_key] } : r)));
          if (alive.current) say(shotErrorFrom(err), "error");
          return null;
        } finally {
          if (alive.current) setBusy(false);
        }
      });
      if (!out.ran) return null;
      if (alive.current) changedRef.current();
      return out.value;
    },
    [client, runUser, projectId, say, patchRows, flight],
  );

  return { busy, setStatus, counts };
};
