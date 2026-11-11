"use client";

import React, { useEffect, useMemo, useRef, useState } from "react";
import { filmReviewGet, filmReviewSet } from "../../../../lib/storybook/series";
import type { StorybookClient } from "../../../../lib/storybook/client";
import type { RunState } from "../../../../lib/storybook/status";

type Review = { status: "pending" | "approved" | "redo"; note: string; input_digest: string; stale: boolean };
type Result = { review: Review; technical_passed: boolean; final_available: boolean };

export function FilmReview({ client, projectId, seriesId, refreshTick, runState }: { client: StorybookClient; projectId: string; seriesId: string; refreshTick: number; runState: RunState | undefined }) {
  const [result, setResult] = useState<Result | null>(null);
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  const [reload, setReload] = useState(0);
  const [saving, setSaving] = useState(false);
  const scope = useMemo(() => ({}), [client, projectId, seriesId]);
  const currentScope = useRef<object | null>(scope);
  currentScope.current = scope;
  useEffect(() => {
    setSaving(false);
    return () => { if (currentScope.current === scope) currentScope.current = null; };
  }, [scope]);
  const generation = useRef(0);
  useEffect(() => {
    const current = ++generation.current;
    setResult(null);
    setError("");
    filmReviewGet<Result>(client, { project_id: projectId, series_id: seriesId }).then((r) => {
      if (current === generation.current) { setResult(r); setNote(r.review.note); }
    }).catch((e: Error) => { if (current === generation.current) setError(e.message); });
    return () => { generation.current++; };
  }, [client, projectId, seriesId, refreshTick, runState, reload]);
  const save = async (status: Review["status"]) => {
    if (!result || saving) return;
    const current = ++generation.current;
    setSaving(true);
    try {
      const r = await filmReviewSet<Result>(client, { project_id: projectId, series_id: seriesId, status, note, expected_digest: result.review.input_digest });
      if (currentScope.current === scope) {
        if (current === generation.current) { setResult(r); setError(""); }
        setReload((value) => value + 1);
      }
    } catch (e) { if (currentScope.current === scope) setError((e as Error).message); }
    finally { if (currentScope.current === scope) setSaving(false); }
  };
  return <section>
    <h3>Принятие фильма</h3>
    {error && <p role="alert">{error}</p>}
    {result && <>
      <p>Техническая проверка: {result.technical_passed ? "пройдена" : "не пройдена"}. Файл: {result.final_available ? "готов" : "отсутствует"}.</p>
      <p>Художественное решение: {result.review.status === "approved" ? "принято" : result.review.status === "redo" ? "на доработку" : "ожидает просмотра"}{result.review.stale ? " (финал изменился)" : ""}.</p>
      <label>Замечания <textarea value={note} onChange={(e) => setNote(e.target.value)} /></label>
      <button type="button" disabled={saving || !result.final_available || !result.technical_passed} onClick={() => void save("approved")}>Принять художественно</button>
      <button type="button" disabled={saving} onClick={() => void save("redo")}>На доработку</button>
    </>}
  </section>;
}
