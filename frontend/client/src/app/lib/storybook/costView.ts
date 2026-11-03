// Оценка стоимости для экрана «Генерация»: заголовок, разбивка, вердикт бюджета. Деньги - как на экране «Кадры».
import { gfmt } from "./generationTexts";
import type { CostComponent, CostEstimateResult } from "./runSettings";
import { formatShotMoney } from "./shotsView";

const COMPONENT_ORDER = ["video", "images", "music", "sfx", "text_llm"] as const;
const COMPONENT_WORDS: Record<string, string> = {
  video: "видео",
  images: "картинок",
  music: "музыки",
  sfx: "эффектов",
  text_llm: "текстов",
};
const COMPONENT_ROW: Record<string, "comp_video" | "comp_images" | "comp_music" | "comp_sfx" | "comp_text_llm"> = {
  video: "comp_video",
  images: "comp_images",
  music: "comp_music",
  sfx: "comp_sfx",
  text_llm: "comp_text_llm",
};

export const estimateHeadline = (e: Pick<CostEstimateResult, "complete" | "total_known_rub" | "unknown_components">): string => {
  if (e.complete) return gfmt("estimate_label", { cost: formatShotMoney(e.total_known_rub) });
  if (e.total_known_rub > 0) {
    const parts = e.unknown_components.map((id) => COMPONENT_WORDS[id] ?? id).join(", ");
    return gfmt("estimate_not_less", { cost: formatShotMoney(e.total_known_rub), parts });
  }
  return gfmt("estimate_unknown");
};

export type CostRow = { id: string; title: string; amount: string; rough: boolean };

export const costRows = (e: Pick<CostEstimateResult, "components">): CostRow[] => {
  const byId = new Map<string, CostComponent>(e.components.map((c) => [c.id, c]));
  const rows: CostRow[] = [];
  for (const id of COMPONENT_ORDER) {
    const c = byId.get(id);
    if (!c) continue;
    const known = c.basis !== "unknown" && typeof c.amount_rub === "number";
    rows.push({
      id,
      title: gfmt(COMPONENT_ROW[id]),
      amount: known ? formatShotMoney(c.amount_rub as number) : "—",
      rough: c.basis === "rough",
    });
  }
  return rows;
};

// Строка вердикта бюджета; tone нужен для цвета (но смысл несёт текст)
export const budgetLine = (
  e: Pick<CostEstimateResult, "budget_verdict" | "short_by_rub">,
): { text: string; tone: "ok" | "warn" | "muted" } => {
  switch (e.budget_verdict) {
    case "enough":
      return { text: gfmt("budget_enough"), tone: "ok" };
    case "short":
      return { text: gfmt("budget_short", { sum: formatShotMoney(e.short_by_rub ?? 0) }), tone: "warn" };
    case "cannot_check":
      return { text: gfmt("budget_cannot"), tone: "muted" };
    default:
      return { text: gfmt("budget_none"), tone: "muted" };
  }
};

export const isCostEstimate = (value: unknown): value is CostEstimateResult =>
  typeof value === "object" && value !== null && typeof (value as CostEstimateResult).total_known_rub === "number" && Array.isArray((value as CostEstimateResult).components);
