import type { StorybookClient } from "./client";

export const projectsUpdate = <T extends object = Record<string, unknown>>(client: StorybookClient, payload: { project_id: string; title: string; description: string; language: string; defaults: Record<string, unknown> }) =>
  client.call<T>("projects_update", payload);

export const seriesCreate = <T extends object = Record<string, unknown>>(client: StorybookClient, payload: { project_id: string; title: string; source_material: string; participant_ids: string[]; settings: Record<string, unknown> }) =>
  client.call<T>("series_create", payload);

export const seriesDelete = <T extends object = Record<string, unknown>>(client: StorybookClient, payload: { project_id: string; series_id: string; confirm: boolean }) =>
  client.call<T>("series_delete", payload);

export const seriesGet = <T extends object = Record<string, unknown>>(client: StorybookClient, payload: { project_id: string; series_id: string }) =>
  client.call<T>("series_get", payload);

export const seriesList = <T extends object = Record<string, unknown>>(client: StorybookClient, payload: { project_id: string }) =>
  client.call<T>("series_list", payload);

export const seriesResourceUpdateApply = <T extends object = Record<string, unknown>>(client: StorybookClient, payload: { project_id: string; series_id: string; kind: string; entity_id: string; version_id: string }) =>
  client.call<T>("series_resource_update_apply", payload);

export const seriesResourceUpdatePreview = <T extends object = Record<string, unknown>>(client: StorybookClient, payload: { project_id: string; series_id: string; kind: string; entity_id: string; version_id: string }) =>
  client.call<T>("series_resource_update_preview", payload);

export const seriesUpdate = <T extends object = Record<string, unknown>>(client: StorybookClient, payload: { project_id: string; series_id: string; title: string; source_material: string; settings: Record<string, unknown>; participant_ids?: string[] }) =>
  client.call<T>("series_update", payload);

export const sharedResourceGet = <T extends object = Record<string, unknown>>(client: StorybookClient, payload: { project_id: string; kind: string; entity_id: string; version_id: string }) =>
  client.call<T>("shared_resource_get", payload);

export const sharedResourceSave = <T extends object = Record<string, unknown>>(client: StorybookClient, payload: { project_id: string; kind: string; entity_id: string; data: Record<string, unknown>; reference_base64?: string; reference_filename?: string }) =>
  client.call<T>("shared_resource_save", payload);

export const sharedResourceVersions = <T extends object = Record<string, unknown>>(client: StorybookClient, payload: { project_id: string; kind: string; entity_id: string }) =>
  client.call<T>("shared_resource_versions", payload);

export const soundEventUpdate = <T extends object = Record<string, unknown>>(client: StorybookClient, payload: { project_id: string; series_id?: string; event_id: string; start: number; level: number; enabled: boolean }) =>
  client.call<T>("sound_event_update", payload);

export const soundEventsGet = <T extends object = Record<string, unknown>>(client: StorybookClient, payload: { project_id: string; series_id?: string }) =>
  client.call<T>("sound_events_get", payload);

export const soundPreview = <T extends object = Record<string, unknown>>(client: StorybookClient, payload: { project_id: string; series_id?: string; event_id: string }) =>
  client.call<T>("sound_preview", payload);

export const speechGet = <T extends object = Record<string, unknown>>(client: StorybookClient, payload: { project_id: string; series_id?: string }) =>
  client.call<T>("speech_get", payload);

export const speechLineUpdate = <T extends object = Record<string, unknown>>(client: StorybookClient, payload: { project_id: string; series_id?: string; line_id: string; text: string; delivery: string; pause_before: number; pause_after: number }) =>
  client.call<T>("speech_line_update", payload);

export const filmReviewGet = <T extends object = Record<string, unknown>>(client: StorybookClient, payload: { project_id: string; series_id: string }) =>
  client.call<T>("film_review_get", payload);

export const filmReviewSet = <T extends object = Record<string, unknown>>(client: StorybookClient, payload: { project_id: string; series_id: string; status: "pending" | "approved" | "redo"; note?: string; expected_digest: string }) =>
  client.call<T>("film_review_set", payload);

