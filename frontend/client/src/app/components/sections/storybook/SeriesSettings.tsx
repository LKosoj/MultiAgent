"use client";

import React, { useEffect, useRef, useState } from "react";
import { seriesGet, seriesResourceUpdateApply, seriesResourceUpdatePreview, seriesUpdate, sharedResourceVersions } from "../../../lib/storybook/series";
import { SeriesSettingsFields } from "./SeriesSettingsFields";
import type { StorybookClient } from "../../../lib/storybook/client";

type Snapshot = Record<string, Record<string, string>>;
type Series = { title: string; source_material: string; participant_ids: string[]; settings: Record<string, unknown>; snapshot: Snapshot; stale_results?: string[] };
type Preview = { kind: string; entity: string; version: string; affected: string[] };
export function SeriesSettings({ client, projectId, seriesId }: { client: StorybookClient; projectId: string; seriesId: string }) {
  const [series, setSeries] = useState<Series | null>(null);
  const [defaults, setDefaults] = useState<Record<string, unknown>>({});
  const [available, setAvailable] = useState<Snapshot>({});
  const [versions, setVersions] = useState<{ kind: string; entity: string; items: string[] } | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [error, setError] = useState("");
  const selection = useRef(0);
  useEffect(() => () => { selection.current += 1; }, []);
  useEffect(() => {
    let current = true;
    Promise.all([seriesGet<{ series: Series }>(client, { project_id: projectId, series_id: seriesId }), client.call<{ details: { resources?: Snapshot; defaults?: Record<string, unknown> } }>("projects_get", { project_id: projectId })]).then(([a, b]) => { if (current) { setSeries(a.series); setAvailable(b.details.resources ?? {}); setDefaults(b.details.defaults ?? {}); } }).catch((e: Error) => { if (current) setError(e.message); });
    return () => { current = false; };
  }, [client, projectId, seriesId]);
  const save = async () => {
    if (!series) return;
    try { const r = await seriesUpdate<{ series: Series }>(client, { project_id: projectId, series_id: seriesId, title: series.title, source_material: series.source_material, participant_ids: series.participant_ids, settings: series.settings }); setSeries(r.series); setError(""); } catch (e) { setError((e as Error).message); }
  };
  const listVersions = async (kind: string, entity: string) => {
    const current = ++selection.current;
    setVersions(null); setPreview(null); setError("");
    try {
      const r = await sharedResourceVersions<{ versions: string[] }>(client, { project_id: projectId, kind, entity_id: entity });
      if (current === selection.current) setVersions({ kind, entity, items: r.versions });
    } catch (e) { if (current === selection.current) setError((e as Error).message); }
  };
  const show = async (kind: string, entity: string, version: string) => {
    const current = ++selection.current;
    setPreview(null); setError("");
    try {
      const r = await seriesResourceUpdatePreview<{ affected_results: string[] }>(client, { project_id: projectId, series_id: seriesId, kind, entity_id: entity, version_id: version });
      if (current === selection.current) setPreview({ kind, entity, version, affected: r.affected_results });
    } catch (e) { if (current === selection.current) setError((e as Error).message); }
  };
  const apply = async () => {
    if (!preview) return;
    try { await seriesResourceUpdateApply(client, { project_id: projectId, series_id: seriesId, kind: preview.kind, entity_id: preview.entity, version_id: preview.version }); const r = await seriesGet<{ series: Series }>(client, { project_id: projectId, series_id: seriesId }); setSeries(r.series); setPreview(null); setError(""); } catch (e) { setError((e as Error).message); }
  };
  return <section><h3>Серия и снимок ресурсов</h3>{error && <p role="alert">{error}</p>}{series && <>
    <label>Название <input value={series.title} onChange={(e) => setSeries({ ...series, title: e.target.value })} /></label>
    <label>Материал <textarea value={series.source_material} onChange={(e) => setSeries({ ...series, source_material: e.target.value })} /></label>
    <fieldset><legend>Участники</legend>{Object.keys(available.characters ?? {}).map((id) => <label key={id}><input type="checkbox" checked={series.participant_ids.includes(id)} onChange={(e) => setSeries({ ...series, participant_ids: e.target.checked ? [...series.participant_ids, id] : series.participant_ids.filter((x) => x !== id) })} />{id}</label>)}</fieldset>
    <SeriesSettingsFields defaults={defaults} settings={series.settings} onChange={(settings) => setSeries({ ...series, settings })} />
    <button type="button" onClick={() => void save()}>Сохранить серию</button>
    {series.stale_results?.length ? <p>Требуют обновления: {series.stale_results.join(", ")}</p> : null}
    {Object.entries(series.snapshot ?? {}).flatMap(([kind, values]) => Object.entries(values).map(([entity, version]) => <div key={`${kind}:${entity}`}>{kind} / {entity}: {version} <button type="button" onClick={() => void listVersions(kind, entity)}>Выбрать версию</button></div>))}
    {versions && <div><p>Версии {versions.entity}</p>{versions.items.map((version) => <button type="button" key={version} onClick={() => void show(versions.kind, versions.entity, version)}>{version}</button>)}</div>}
    {preview && <div><p>Затронутые результаты: {preview.affected.join(", ") || "нет"}</p><button type="button" onClick={() => void apply()}>Применить версию</button><button type="button" onClick={() => { selection.current += 1; setPreview(null); }}>Отмена</button></div>}
  </>}</section>;
}
