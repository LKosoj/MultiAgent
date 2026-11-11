"use client";

import React, { useEffect, useRef, useState } from "react";
import { projectsUpdate, sharedResourceGet, sharedResourceSave } from "../../../lib/storybook/series";
import type { StorybookClient } from "../../../lib/storybook/client";

type Resources = Record<string, Record<string, string>>;
type Data = Record<string, unknown>;
export function updateStyleField(data: Data, group: "style_images" | "style_text", name: string, value: string): Data {
  const original = data[group];
  const fields = typeof original === "object" && original !== null && !Array.isArray(original) ? original as Data : {};
  return { ...data, [group]: { ...fields, [name]: value } };
}
const labels: Record<string, string> = { characters: "Герои", narrators: "Рассказчики", locations: "Места", items: "Предметы", style: "Стиль" };
export function SharedResources({ client, projectId, onChanged }: { client: StorybookClient; projectId: string; onChanged: () => void }) {
  const [project, setProject] = useState<{ title: string; description: string; language: string; defaults: Data } | null>(null);
  const [resources, setResources] = useState<Resources>({});
  const [kind, setKind] = useState("characters");
  const [id, setId] = useState("");
  const [data, setData] = useState<Data>({});
  const [referenceUrl, setReferenceUrl] = useState<string | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [error, setError] = useState("");
  const [resourceState, setResourceState] = useState<"new" | "loading" | "ready" | "error">("new");
  const selection = useRef(0);
  const fileInput = useRef<HTMLInputElement>(null);
  useEffect(() => () => { selection.current += 1; }, []);
  const clearResource = () => {
    selection.current += 1;
    setData({}); setFile(null); setReferenceUrl(null); setError(""); setResourceState("new");
    if (fileInput.current) fileInput.current.value = "";
  };
  const load = () => client.call<{ details: { resources?: Resources } }>("projects_get", { project_id: projectId }).then((r) => setResources(r.details.resources ?? {}));
  useEffect(() => { let current = true; client.call<{ details: { resources?: Resources; title: string; description: string; language: string; defaults: Data } }>("projects_get", { project_id: projectId }).then((r) => { if (current) { setResources(r.details.resources ?? {}); setProject(r.details); } }).catch((e: Error) => { if (current) setError(e.message); }); return () => { current = false; }; }, [client, projectId]);
  const select = async (nextKind: string, nextId: string, version: string) => {
    clearResource();
    const current = selection.current;
    setKind(nextKind); setId(nextId); setResourceState("loading");
    try {
      const r = await sharedResourceGet<{ data: Data; reference_path: string | null }>(client, { project_id: projectId, kind: nextKind, entity_id: nextId, version_id: version });
      if (current !== selection.current) return;
      setData(r.data);
      if (r.reference_path) {
        await client.ensureMediaToken(projectId);
        if (current !== selection.current) return;
        setReferenceUrl(client.mediaUrl(projectId, r.reference_path));
      }
      if (current === selection.current) setResourceState("ready");
    } catch (e) { if (current === selection.current) { setResourceState("error"); setError((e as Error).message); } }
  };
  const existingUnloaded = resourceState === "new" && !!resources[kind]?.[id];
  const resourceBlocked = resourceState === "loading" || resourceState === "error" || existingUnloaded;
  const save = async () => {
    if (!id.trim() || resourceBlocked) return;
    try {
      const reference = file ? await new Promise<string>((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result).split(",")[1]); reader.onerror = reject; reader.readAsDataURL(file); }) : "";
      await sharedResourceSave(client, { project_id: projectId, kind, entity_id: id, data, reference_base64: reference, reference_filename: file?.name ?? "" });
      await load(); onChanged(); setError("");
    } catch (e) { setError((e as Error).message); }
  };
  const saveProject = async () => {
    if (!project) return;
    try { await projectsUpdate(client, { project_id: projectId, title: project.title, description: project.description, language: project.language, defaults: project.defaults }); setError(""); }
    catch (e) { setError((e as Error).message); }
  };
  const field = (name: string, label: string) => <label>{label} <input value={typeof data[name] === "string" ? data[name] as string : ""} onChange={(e) => setData({ ...data, [name]: e.target.value })} /></label>;
  const styleField = (group: "style_images" | "style_text", name: string, label: string) => {
    const raw = data[group];
    const fields = typeof raw === "object" && raw !== null && !Array.isArray(raw) ? raw as Data : {};
    return <label>{label} <input value={typeof fields[name] === "string" ? fields[name] as string : ""} onChange={(e) => setData((old) => updateStyleField(old, group, name, e.target.value))} /></label>;
  };
  return <section><h3>Проект</h3>
    {project && <>
      <label>Название <input value={project.title} onChange={(e) => setProject({ ...project, title: e.target.value })} /></label>
      <label>Описание мира <textarea value={project.description} onChange={(e) => setProject({ ...project, description: e.target.value })} /></label>
      <label>Язык <input value={project.language} onChange={(e) => setProject({ ...project, language: e.target.value })} /></label>
      <button type="button" onClick={() => void saveProject()}>Сохранить проект</button>
    </>}
    <h3>Общие ресурсы проекта</h3>{error && <p role="alert">{error}</p>}
    {Object.entries(labels).map(([k, label]) => <div key={k}><strong>{label}</strong> {Object.entries(resources[k] ?? {}).map(([entity, version]) => <button type="button" key={entity} onClick={() => void select(k, entity, version)}>{entity}</button>)}</div>)}
    <label>Тип <select value={kind} onChange={(e) => { clearResource(); setKind(e.target.value); setId(""); }}>{Object.entries(labels).map(([k, label]) => <option value={k} key={k}>{label}</option>)}</select></label>
    <label>Идентификатор <input value={id} onChange={(e) => { clearResource(); setId(e.target.value); }} /></label>
    {resourceState === "loading" && <p role="status">Загрузка ресурса…</p>}
    {existingUnloaded && <p>Выберите существующий ресурс из списка, чтобы его изменить.</p>}
    <fieldset disabled={resourceBlocked}>
    {field("name", "Название")}{field("description", "Описание")}
    {(kind === "characters" || kind === "narrators") && <>{field("voice", "Голос")}{field("language", "Язык")}{field("speech_style", "Манера речи")}</>}
    {kind === "style" && <>
      {styleField("style_images", "art_style", "Стиль изображения")}
      {styleField("style_images", "material_signature", "Материал")}
      {styleField("style_images", "texture", "Фактура")}
      {styleField("style_images", "lighting", "Освещение")}
      {styleField("style_text", "narrative_voice", "Стиль повествования")}
      {styleField("style_text", "sentence_length", "Длина предложений")}
      {styleField("style_text", "vocabulary_bounds", "Словарь")}
      {field("negative_list", "Что исключить")}
    </>}
    {referenceUrl && <img src={referenceUrl} alt="Образец ресурса" style={{ maxWidth: 240 }} />}
    <label>Образец <input ref={fileInput} type="file" accept="image/png,image/jpeg,image/webp" onChange={(e) => setFile(e.target.files?.[0] ?? null)} /></label>
    <button type="button" disabled={!id.trim() || resourceBlocked} onClick={() => void save()}>Сохранить новую версию</button>
    </fieldset>
  </section>;
}
