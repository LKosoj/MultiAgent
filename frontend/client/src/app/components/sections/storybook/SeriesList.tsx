"use client";

import React, { useEffect, useRef, useState } from "react";
import { seriesCreate, seriesDelete, seriesList } from "../../../lib/storybook/series";
import { SeriesSettingsFields } from "./SeriesSettingsFields";
import { ConfirmDialog } from "./ConfirmDialog";
import { SharedResources } from "./SharedResources";
import type { StorybookClient } from "../../../lib/storybook/client";

type Series = { series_id: string; title: string; source_material: string; participant_ids: string[]; settings: Record<string, unknown>; stale_results?: string[] };

type Props = { client: StorybookClient; projectId: string; onOpen: (id: string) => void };

export function SeriesList({ client, projectId, onOpen }: Props) {
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const [items, setItems] = useState<Series[]>([]);
  const [characters, setCharacters] = useState<string[]>([]);
  const [defaults, setDefaults] = useState<Record<string, unknown>>({});
  const [settings, setSettings] = useState<Record<string, unknown>>({});
  const [deleting, setDeleting] = useState<Series | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [title, setTitle] = useState("");
  const [source, setSource] = useState("");
  const [error, setError] = useState("");
  const [reload, setReload] = useState(0);
  useEffect(() => {
    let current = true;
    client.call<{ details: { defaults?: Record<string, unknown>; resources?: { characters?: Record<string, string> } } }>("projects_get", { project_id: projectId }).then((r) => { if (current) { setCharacters(Object.keys(r.details.resources?.characters ?? {})); setDefaults(r.details.defaults ?? {}); } }).catch((e: Error) => { if (current) setError(e.message); });
    seriesList<{ series: Series[] }>(client, { project_id: projectId }).then((r) => {
      if (current) setItems(r.series);
    }).catch((e: Error) => { if (current) setError(e.message); });
    return () => { current = false; };
  }, [client, projectId, reload]);
  const create = async () => {
    try {
      const result = await seriesCreate<{ series_id: string }>(client, {
        project_id: projectId, title, source_material: source, participant_ids: selected, settings,
      });
      if (mounted.current) onOpen(result.series_id);
    } catch (e) { setError((e as Error).message); }
  };
  const remove = async () => {
    if (!deleting || deleteBusy) return;
    setDeleteBusy(true); setDeleteError("");
    try {
      await seriesDelete(client, { project_id: projectId, series_id: deleting.series_id, confirm: true });
      if (mounted.current) { setDeleting(null); setReload((n) => n + 1); }
    } catch (e) { if (mounted.current) setDeleteError((e as Error).message); }
    finally { if (mounted.current) setDeleteBusy(false); }
  };
  return <section>
    <SharedResources client={client} projectId={projectId} onChanged={() => setReload((n) => n + 1)} />
    <h2>Серии проекта</h2>
    {error && <p role="alert">{error}</p>}
    {items.map((item) => <div key={item.series_id}>
      <button type="button" onClick={() => onOpen(item.series_id)}>{item.title}</button>
      <button type="button" onClick={() => { setDeleteError(""); setDeleting(item); }}>Удалить серию</button>
      {item.stale_results?.length ? <p>Требуют обновления: {item.stale_results.join(", ")}</p> : null}
    </div>)}
    <h3>Новая серия</h3>
    <label>Название <input value={title} onChange={(e) => setTitle(e.target.value)} /></label>
    <label>Исходный материал <textarea value={source} onChange={(e) => setSource(e.target.value)} /></label>
    <fieldset><legend>Участники</legend>{characters.map((id) => <label key={id}><input type="checkbox" checked={selected.includes(id)} onChange={(e) => setSelected((old) => e.target.checked ? [...old, id] : old.filter((x) => x !== id))} /> {id}</label>)}</fieldset>
    <SeriesSettingsFields defaults={defaults} settings={settings} onChange={setSettings} />
    <button type="button" disabled={!title.trim()} onClick={() => void create()}>Создать серию</button>
    <button type="button" onClick={() => setReload((n) => n + 1)}>Обновить</button>
    {deleting && <ConfirmDialog title="Удалить серию?" body={`Серия «${deleting.title}» и её материалы будут удалены.`} confirmLabel="Удалить" busy={deleteBusy} error={deleteError} onConfirm={() => void remove()} onCancel={() => setDeleting(null)} />}
  </section>;
}
