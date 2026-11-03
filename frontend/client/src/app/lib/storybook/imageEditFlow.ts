// Чистая логика правки картинки ИИ: кнопка, ошибки, тело запуска, образцы.
// Те же правила, что в Tk (tests/fixtures/storybook_screens/golden_imagetools_vectors.json).
import type { MediaItem } from "./media";
import { IMAGE_TEXTS, imageFmt, type ImageTextId } from "./imageToolsTexts";
import { formatShotMoney } from "./shotsView";

// Числа живут на сервере (limits в предпросмотре); это запасные значения до его ответа
export const PROMPT_MIN = 5;
export const REFERENCES_MAX = 3;
export const EDIT_EXTENSIONS = [".png", ".jpg", ".jpeg", ".webp"] as const;

export type EditTarget =
  | { kind: "shot"; shotKey: string; part: "start" | "end"; path: string; name: string }
  | { kind: "illustration"; path: string; name: string };

export type EditProblem = { level: "error" | "warning" | "info"; code: string; message: string };

export type ImageEditPreview = {
  target: { kind: "shot" | "illustration"; path: string; name: string; shot_key: string; part: string; size: number; modified_ts: number };
  references: { path: string; name: string }[];
  limits: { prompt_min: number; prompt_max: number; references_max: number; extensions: string[] };
  estimate: { cost_rub: number; unknown_cost: boolean; price_per_image_rub: number | null };
  problems: EditProblem[];
  can_start: boolean;
  plan_digest: string;
};

const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

export const editCanSubmit = (prompt: unknown, refsN: unknown, estimateReady: unknown): boolean => {
  const text = typeof prompt === "string" ? prompt.trim() : "";
  return text.length >= PROMPT_MIN && Number.isInteger(refsN) && (refsN as number) >= 0 && (refsN as number) <= REFERENCES_MAX && estimateReady === true;
};

export type EditButton = { label: string; note: ImageTextId | null };

// Подпись кнопки запуска и пояснение под ней. Деньги только через formatShotMoney
export const editButton = (costRub: unknown, unknownCost: unknown): EditButton => {
  if (isNum(costRub) && costRub > 0) return { label: imageFmt("edit_go_paid", { cost: formatShotMoney(costRub) }), note: null };
  if (unknownCost) return { label: IMAGE_TEXTS.edit_go_unknown, note: "edit_cost_unknown" };
  return { label: IMAGE_TEXTS.edit_go_free, note: null };
};

// id текста для ошибки image_edit_start (code и details.reason)
export const editErrorText = (code: unknown, reason: unknown, unknownCost: unknown = false): ImageTextId => {
  if (reason === "plan_changed") return "edit_plan_changed";
  if (reason === "cost_confirmation_required" && unknownCost === true) return "edit_confirm_unknown";
  if (reason === "cost_changed" || reason === "cost_confirmation_required") return "edit_cost_changed";
  if (code === "locked" || reason === "regeneration_pending" || reason === "edit_running") return "edit_busy";
  return "edit_failed";
};

// Текст ошибки для человека: всегда из таблицы текстов (как в Tk)
export const editErrorMessage = (code: unknown, reason: unknown, unknownCost: unknown = false): string =>
  IMAGE_TEXTS[editErrorText(code, reason, unknownCost)];

// Строка цены в окне правки: при известной цене - «Цена правки: …», при неизвестной - пояснение
export const editCostLine = (costRub: unknown, unknownCost: unknown): string =>
  unknownCost ? IMAGE_TEXTS.edit_cost_unknown : imageFmt("edit_cost", { cost: formatShotMoney(isNum(costRub) ? costRub : 0) });

export const editableExtension = (name: unknown): boolean => {
  if (typeof name !== "string") return false;
  const dot = name.lastIndexOf(".");
  return dot > 0 && (EDIT_EXTENSIONS as readonly string[]).includes(name.slice(dot).toLowerCase());
};

// «папка/имя»: так образцы выглядели в старой панели
export const referenceLabel = (path: string): string => {
  const parts = path.split("/").filter(Boolean);
  return parts.length >= 2 ? `${parts[parts.length - 2]}/${parts[parts.length - 1]}` : (parts[0] ?? path);
};

// Образцы для выбора: картинки проекта, которые можно показать и отдать на правку, без самой правимой
export const referenceCandidates = (items: readonly Pick<MediaItem, "path" | "name" | "servable">[], currentPath: string) =>
  items
    .filter((i) => i.path !== currentPath && i.servable && editableExtension(i.name))
    .map((i) => ({ path: i.path, name: i.name, label: referenceLabel(i.path) }))
    .sort((a, b) => a.label.localeCompare(b.label, "ru", { numeric: true }));

// Цель в полях запроса: шот - ключ и часть, иллюстрация - путь
export const targetArgs = (target: EditTarget): Record<string, unknown> =>
  target.kind === "shot" ? { shot_key: target.shotKey, part: target.part } : { path: target.path };

export type StartPayload = Record<string, unknown>;

// Тело запуска. Сумму подтверждения берём из показанного предпросмотра, а не из ответа сервера «на лету»:
// пользователь согласился именно с этой суммой. confirm_unknown_cost ставим только при неизвестной цене
export const buildStartPayload = (projectId: string, target: EditTarget, prompt: string, references: readonly string[], preview: ImageEditPreview): StartPayload => {
  const { estimate } = preview;
  return {
    project_id: projectId,
    ...targetArgs(target),
    prompt: prompt.trim(),
    reference_paths: [...references],
    expected_plan_digest: preview.plan_digest,
    ...(estimate.unknown_cost ? { confirm_unknown_cost: true } : estimate.cost_rub > 0 ? { confirm_cost_rub: estimate.cost_rub } : {}),
    surface: "react",
  };
};

// Образцы после смены правимой картинки: текущая в образцы не попадает, лишние сверх лимита отрезаются
export const normalizeReferences = (selected: readonly string[], currentPath: string, max = REFERENCES_MAX): string[] =>
  [...new Set(selected)].filter((p) => p !== currentPath).slice(0, max);

export const toggleReference = (selected: readonly string[], path: string, max = REFERENCES_MAX): string[] => {
  if (selected.includes(path)) return selected.filter((p) => p !== path);
  return selected.length >= max ? [...selected] : [...selected, path];
};
