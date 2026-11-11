"use client";

import React, { useEffect, useState } from "react";
import { speechGet, speechLineUpdate } from "../../../lib/storybook/series";
import type { StorybookClient } from "../../../lib/storybook/client";

type Line = { id: string; text: string; delivery: string; pause_before: number; pause_after: number };
type Entry = Record<string, unknown>;
const number = (v: unknown) => typeof v === "number" ? v : 0;
const text = (v: unknown) => typeof v === "string" ? v : "";
const array = (v: unknown): Entry[] => Array.isArray(v) ? v as Entry[] : [];
export function screenplayLines(screenplay: Entry): Line[] {
  const result: Line[] = [];
  for (const scene of array(screenplay.screenplay)) {
    for (const item of array(scene.dialogue)) if (item.id) result.push({ id: text(item.id), text: text(item.line), delivery: text(item.direction), pause_before: number(item.pause_before), pause_after: number(item.pause_after) });
    const shots = array(scene.storyboard);
    for (const shot of shots) if (shot.voiceover_id && shot.voiceover) result.push({ id: text(shot.voiceover_id), text: text(shot.voiceover), delivery: text(shot.voiceover_delivery), pause_before: number(shot.pause_before), pause_after: number(shot.pause_after) });
    if (scene.voiceover_id && !shots.some((shot) => shot.voiceover)) result.push({ id: text(scene.voiceover_id), text: text(scene.voiceover), delivery: text(scene.voiceover_delivery), pause_before: number(scene.pause_before), pause_after: number(scene.pause_after) });
  }
  return result;
}
export function SeriesSpeech({ client, projectId }: { client: StorybookClient; projectId: string }) {
  const [lines, setLines] = useState<Line[]>([]);
  const [speech, setSpeech] = useState<Entry | null>(null);
  const [audioUrl, setAudioUrl] = useState<string | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let current = true;
    speechGet<{ screenplay: Entry; speech: Entry | null }>(client, { project_id: projectId }).then((r) => {
      if (current) { setLines(screenplayLines(r.screenplay)); setSpeech(r.speech); }
      const audioPath = r.speech?.audio_path;
      if (typeof audioPath === "string") void client.ensureMediaToken(projectId).then(() => { if (current) setAudioUrl(client.mediaUrl(projectId, `98_audio/${audioPath}`)); });
    }).catch((e: Error) => { if (current) setError(e.message); });
    return () => { current = false; };
  }, [client, projectId]);
  const change = (index: number, patch: Partial<Line>) => setLines((old) => old.map((line, i) => i === index ? { ...line, ...patch } : line));
  const save = async (line: Line) => {
    try { await speechLineUpdate(client, { project_id: projectId, line_id: line.id, text: line.text, delivery: line.delivery, pause_before: line.pause_before, pause_after: line.pause_after }); setSpeech((old) => old ? { ...old, status: "stale" } : old); setError(""); }
    catch (e) { setError((e as Error).message); }
  };
  return <section><h3>Речь серии</h3>
    {error && <p role="alert">{error}</p>}
    <p>{speech ? `Озвучка: ${text(speech.status) || "создана"}` : "Озвучка ещё не создана"}</p>
    {audioUrl && <audio controls src={audioUrl} />}
    {lines.map((line, i) => <div key={line.id}>
      {array(speech?.lines).filter((item) => item.id === line.id).map((item) => <div key={line.id}>
        <p>Время: {number(item.start)}–{number(item.end)} с; говорящий: {text(item.speaker_id)}</p>
        {audioUrl && <audio controls src={`${audioUrl}#t=${number(item.start)},${number(item.end)}`} />}
      </div>)}
      <label>Реплика <textarea value={line.text} onChange={(e) => change(i, { text: e.target.value })} /></label>
      <label>Подача <input value={line.delivery} onChange={(e) => change(i, { delivery: e.target.value })} /></label>
      <label>Пауза до, с <input type="number" min="0" step="0.1" value={line.pause_before} onChange={(e) => change(i, { pause_before: Number(e.target.value) })} /></label>
      <label>Пауза после, с <input type="number" min="0" step="0.1" value={line.pause_after} onChange={(e) => change(i, { pause_after: Number(e.target.value) })} /></label>
      <button type="button" onClick={() => void save(line)}>Сохранить реплику</button>
    </div>)}
  </section>;
}
