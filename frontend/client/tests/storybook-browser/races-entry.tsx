import React from "react";
import { createRoot } from "react-dom/client";
import { SharedResources } from "../../src/app/components/sections/storybook/SharedResources";
import { SeriesSettings } from "../../src/app/components/sections/storybook/SeriesSettings";

type Pending = { action: string; id: string; resolve: (value: unknown) => void; reject: (error: Error) => void };
const pending: Pending[] = [];
const calls: { action: string; payload: Record<string, unknown> }[] = [];
const deferred = (action: string, id: string) => new Promise((resolve, reject) => pending.push({ action, id, resolve, reject }));
const client = {
  call: async (action: string, payload: Record<string, unknown>) => {
    calls.push({ action, payload });
    if (action === "projects_get") return { status: "success", details: { title: "Проект", description: "Мир", language: "ru", defaults: {}, resources: { characters: { A: "v1", B: "v1" } } } };
    if (action === "series_get") return { status: "success", series: { title: "Серия", source_material: "", participant_ids: [], settings: {}, snapshot: { characters: { A: "v1", B: "v1" } } } };
    if (action === "shared_resource_get" || action === "shared_resource_versions" || action === "series_resource_update_preview") return deferred(action, String(payload.entity_id));
    return { status: "success" };
  },
  ensureMediaToken: async () => { await deferred("token", "token"); },
  mediaUrl: (_: string, path: string) => `/media/${path}`,
  thumbUrl: () => null,
};
Object.assign(window, { resolveRequest: (action: string, id: string, value: unknown) => {
  const index = pending.findIndex((p) => p.action === action && p.id === id);
  if (index < 0) throw new Error(`No request ${action} ${id}`);
  pending.splice(index, 1)[0].resolve(value);
}, rejectRequest: (action: string, id: string) => {
  const index = pending.findIndex((p) => p.action === action && p.id === id);
  if (index < 0) throw new Error(`No request ${action} ${id}`);
  pending.splice(index, 1)[0].reject(new Error("Ошибка загрузки"));
}, requestCalls: () => calls });
const c = client as never;
createRoot(document.getElementById("root")!).render(<>
  <div id="resources"><SharedResources client={c} projectId="p" onChanged={() => {}} /></div>
  <div id="series"><SeriesSettings client={c} projectId="p" seriesId="s" /></div>
</>);
