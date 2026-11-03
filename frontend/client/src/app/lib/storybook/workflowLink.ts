// Проект, на который указывает workflow; null, если свой project_id не задан.
import { isSafeProjectId } from "./nav";

const pick = (v: unknown): string | null => (typeof v === "string" && v.trim() ? v.trim() : null);

export const workflowProjectId = (inputs: Record<string, unknown>, params: Record<string, unknown>): string | null => {
  const id = pick(params.project_id) ?? pick(inputs.project_id);
  return id !== null && isSafeProjectId(id) ? id : null;
};
