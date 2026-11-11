"use client";

import React, { useEffect, useState } from "react";
import { soundEventUpdate, soundEventsGet, soundPreview } from "../../../lib/storybook/series";
import type { StorybookClient } from "../../../lib/storybook/client";

type Event = { id: string; role?: string; editable_start?: boolean; offset_seconds?: number; level?: number; enabled?: boolean; path?: string };
export function SeriesSounds({ client, projectId }: { client: StorybookClient; projectId: string }) {
  const [events, setEvents] = useState<Event[]>([]);
  const [error, setError] = useState("");
  const [preview, setPreview] = useState<string | null>(null);
  useEffect(() => {
    let current = true;
    soundEventsGet<{ events: Event[] }>(client, { project_id: projectId }).then((r) => { if (current) setEvents(r.events); }).catch((e: Error) => { if (current) setError(e.message); });
    return () => { current = false; };
  }, [client, projectId]);
  const change = (i: number, patch: Partial<Event>) => setEvents((old) => old.map((event, n) => n === i ? { ...event, ...patch } : event));
  const save = async (event: Event) => {
    try { await soundEventUpdate(client, { project_id: projectId, event_id: event.id, start: event.offset_seconds ?? 0, level: event.level ?? 1, enabled: event.enabled !== false }); setError(""); }
    catch (e) { setError((e as Error).message); }
  };
  const play = async (event: Event) => {
    try {
      const path = event.role === "music" ? event.path : (await soundPreview<{ preview_path: string }>(client, { project_id: projectId, event_id: event.id })).preview_path;
      await client.ensureMediaToken(projectId);
      setPreview(path ? client.mediaUrl(projectId, `98_audio/${path}`) : null); setError("");
    } catch (e) { setError((e as Error).message); }
  };
  return <section><h3>Звуковые события</h3>{error && <p role="alert">{error}</p>}
    {events.map((event, i) => <div key={event.id}>
      <p>{event.path || event.id}</p>
      <label>Начало, с <input type="number" disabled={event.editable_start === false} min="0" step="0.1" value={event.offset_seconds ?? 0} onChange={(e) => change(i, { offset_seconds: Number(e.target.value) })} /></label>
      <label>Громкость <input type="number" min="0" max="1" step="0.1" value={event.level ?? 1} onChange={(e) => change(i, { level: Number(e.target.value) })} /></label>
      <label><input type="checkbox" checked={event.enabled !== false} onChange={(e) => change(i, { enabled: e.target.checked })} /> Включён</label>
      <button type="button" onClick={() => void save(event)}>Сохранить</button>
      <button type="button" onClick={() => void play(event)}>{event.role === "music" ? "Прослушать музыку" : "Прослушать с речью"}</button>
    </div>)}
    {preview && <audio controls src={preview} />}
  </section>;
}
