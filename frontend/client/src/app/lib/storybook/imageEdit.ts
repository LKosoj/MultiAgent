// Обёртки возможностей правки картинки ИИ (studio/image_edit.py) и ожидание фонового задания.
// Поля ответов остаются в snake_case, как их отдаёт studio.
import type { StorybookClient } from "./client";
import type { EditTarget, ImageEditPreview, StartPayload } from "./imageEditFlow";
import { targetArgs } from "./imageEditFlow";

export type ImageEditJobStart = { job_id: string };

export type ImageEditResult = {
  path: string;
  name: string;
  modified_ts: number;
  size: number;
  backup: string | null;
  version_id: string | null;
  shot_key: string | null;
};

export const imageEditPreview = (
  c: StorybookClient,
  p: { project_id: string; shot_key?: string; part?: string; path?: string; reference_paths?: string[] },
) => c.call<ImageEditPreview>("image_edit_preview", p);

export const imageEditStart = (c: StorybookClient, p: StartPayload) => c.call<ImageEditJobStart>("image_edit_start", p);

export const previewArgs = (projectId: string, target: EditTarget, references: readonly string[]) => ({
  project_id: projectId,
  ...targetArgs(target),
  reference_paths: [...references],
});
