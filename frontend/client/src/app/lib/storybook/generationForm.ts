// Форма «Генерации»: правка настроек прогона и полей брифа. Чистые функции без React.
import { gfmt } from "./generationTexts";
import type {
  EffectiveValue,
  ProviderCatalogResult,
  ProviderInfo,
  ProviderModel,
  RunSettings,
  StyleCatalogResult,
} from "./runSettings";

export const cloneSettings = (s: RunSettings): RunSettings => JSON.parse(JSON.stringify(s)) as RunSettings;

export const isDirty = (saved: RunSettings | null, draft: RunSettings | null): boolean =>
  !!saved && !!draft && JSON.stringify(saved) !== JSON.stringify(draft);

// ---- правка настроек (без изменения исходного объекта)

export const patchVideo = <K extends keyof RunSettings["video"]>(s: RunSettings, key: K, value: RunSettings["video"][K]): RunSettings => ({
  ...s,
  video: { ...s.video, [key]: value },
});

export const patchAudio = <K extends keyof RunSettings["audio"]>(s: RunSettings, key: K, value: RunSettings["audio"][K]): RunSettings => ({
  ...s,
  audio: { ...s.audio, [key]: value },
});

export const patchAdvanced = <K extends keyof RunSettings["advanced"]>(
  s: RunSettings,
  key: K,
  value: RunSettings["advanced"][K],
): RunSettings => ({ ...s, advanced: { ...s.advanced, [key]: value } });

export const patchBudget = (s: RunSettings, budget: number | null): RunSettings => ({ ...s, budget_rub: budget });

// Смена провайдера сбрасывает модель и разрешение: у другого провайдера они могут не подойти
export const changeProvider = (s: RunSettings, provider: string | null): RunSettings => ({
  ...s,
  video: { ...s.video, provider, model: null, fallback_model: null, resolution: null, allowed_durations: null },
});

// Значения по умолчанию подставляются в форму, но не сохраняются
export const resetToDefaults = (defaults: RunSettings): RunSettings => cloneSettings(defaults);

// ---- бюджет

export type BudgetParse = { value: number | null; error: string | null };

export const parseBudget = (text: string): BudgetParse => {
  const t = text.trim().replace(",", ".");
  if (t === "") return { value: null, error: null };
  if (!/^\d+(\.\d+)?$/.test(t)) return { value: null, error: gfmt("budget_invalid") };
  const v = Number(t);
  return Number.isFinite(v) ? { value: v, error: null } : { value: null, error: gfmt("budget_invalid") };
};

export const budgetText = (value: number | null): string => (value === null ? "" : String(value).replace(".", ","));

// ---- списки выбора

export type Option = { value: string; label: string; disabled?: boolean };

export const providerOptions = (catalog: ProviderCatalogResult | null): Option[] =>
  (catalog?.providers ?? []).map((p) => ({
    value: p.id,
    label: p.pipeline_support === "limited" ? `${p.title} (ограничено)` : p.title,
    disabled: !p.selectable,
  }));

// Провайдер, который реально будет использован: выбранный в настройках, иначе заданный сервером
export const activeProvider = (catalog: ProviderCatalogResult | null, s: RunSettings | null): ProviderInfo | null => {
  if (!catalog) return null;
  const id = s?.video.provider ?? catalog.selected_provider;
  return catalog.providers.find((p) => p.id === id) ?? null;
};

export const modelOptions = (p: ProviderInfo | null): Option[] => (p?.models ?? []).map((m) => ({ value: m.id, label: m.id }));

export const activeModel = (p: ProviderInfo | null, s: RunSettings | null): ProviderModel | null => {
  if (!p) return null;
  const id = s?.video.model ?? p.configured_model;
  return p.models.find((m) => m.id === id) ?? p.models[0] ?? null;
};

export const resolutionOptions = (m: ProviderModel | null): Option[] =>
  (m?.supported_resolutions ?? []).map((r) => ({ value: r, label: r }));

export const durationChoices = (m: ProviderModel | null): number[] => [...(m?.supported_durations ?? [])].sort((a, b) => a - b);

// null в allowed_durations значит «любые». Снятие галочки превращает список в явный.
export const toggleDuration = (current: number[] | null, all: number[], value: number): number[] | null => {
  const base = current ?? all;
  const next = base.includes(value) ? base.filter((d) => d !== value) : [...base, value].sort((a, b) => a - b);
  return next.length === all.length && all.every((d) => next.includes(d)) ? null : next;
};

export const isDurationOn = (current: number[] | null, value: number): boolean => current === null || current.includes(value);

export type StyleGroup = { profile: string; options: Option[] };

export const styleGroups = (catalog: StyleCatalogResult | null): StyleGroup[] =>
  (catalog?.profiles ?? []).map((p) => ({
    profile: p.id,
    options: p.presets.map((preset) => ({ value: `${p.id}|${preset.id}`, label: preset.title })),
  }));

export const styleValue = (s: RunSettings): string => (s.style.profile && s.style.preset ? `${s.style.profile}|${s.style.preset}` : "");

export const withStyle = (s: RunSettings, value: string): RunSettings => {
  if (value === "") return { ...s, style: { profile: null, preset: null } };
  const [profile, preset] = value.split("|");
  return { ...s, style: { profile: profile || null, preset: preset || null } };
};

// Откуда взято значение: из настроек, окружения сервера или по умолчанию
export const sourceHint = (effective: Record<string, EffectiveValue> | null, key: string): string | null => {
  const e = effective?.[key];
  if (!e) return null;
  return e.source === "setting" ? gfmt("source_setting") : e.source === "env" ? gfmt("source_env") : gfmt("source_default");
};

// ---- поля брифа

export const BRIEF_FIELDS = [
  { key: "pages_min", label: "brief_pages_min", kind: "int" },
  { key: "pages_max", label: "brief_pages_max", kind: "int" },
  { key: "words_per_page_min", label: "brief_words_min", kind: "int" },
  { key: "words_per_page_max", label: "brief_words_max", kind: "int" },
  { key: "screenplay_time", label: "brief_screenplay_time", kind: "int" },
  { key: "language", label: "brief_language", kind: "text" },
  { key: "generate_end_shots", label: "brief_end_shots", kind: "bool" },
] as const;

export type BriefKey = (typeof BRIEF_FIELDS)[number]["key"];
// Черновик хранит только то, что пользователь тронул
export type BriefDraft = Partial<Record<BriefKey, string | boolean>>;

export const briefValue = (draft: BriefDraft, planInputs: Record<string, unknown> | null | undefined, key: BriefKey): string | boolean => {
  if (key in draft) return draft[key] as string | boolean;
  const v = planInputs?.[key];
  if (typeof v === "boolean") return v;
  return v === undefined || v === null ? "" : String(v);
};

const POSITIVE_INT = /^[1-9]\d*$/;

export type InputsResult = { inputs: Record<string, unknown>; error: string | null };

// В запрос уходят только затронутые поля; ошибка - первая найденная
export const collectInputs = (draft: BriefDraft, planInputs: Record<string, unknown> | null | undefined): InputsResult => {
  const inputs: Record<string, unknown> = {};
  for (const f of BRIEF_FIELDS) {
    if (!(f.key in draft)) continue;
    const raw = draft[f.key];
    if (f.kind === "bool") {
      inputs[f.key] = raw === true;
    } else if (f.kind === "int") {
      const text = String(raw ?? "").trim();
      if (!POSITIVE_INT.test(text)) return { inputs: {}, error: gfmt("field_int_positive", { name: gfmt(f.label) }) };
      inputs[f.key] = Number(text);
    } else {
      inputs[f.key] = String(raw ?? "").trim();
    }
  }
  const pair = (lo: BriefKey, hi: BriefKey, label: Parameters<typeof gfmt>[0]) => {
    const a = Number(inputs[lo] ?? planInputs?.[lo]);
    const b = Number(inputs[hi] ?? planInputs?.[hi]);
    return (lo in inputs || hi in inputs) && Number.isFinite(a) && Number.isFinite(b) && a > b ? gfmt("field_min_max", { name: gfmt(label) }) : null;
  };
  const err = pair("pages_min", "pages_max", "brief_pages_min") ?? pair("words_per_page_min", "words_per_page_max", "brief_words_min");
  return err ? { inputs: {}, error: err } : { inputs, error: null };
};

export const setBrief = (draft: BriefDraft, key: BriefKey, value: string | boolean): BriefDraft => ({ ...draft, [key]: value });

export const blockoutOn = (planInputs: Record<string, unknown> | null | undefined): boolean => planInputs?.generate_blockout === true;
