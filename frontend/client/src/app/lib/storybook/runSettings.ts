// Обёртки возможностей модуля run_settings (настройки прогона, провайдеры, стоимость).
import type { StorybookClient } from "./client";

export type RunSettings = {
  version: 1;
  video: {
    provider: string | null;
    model: string | null;
    fallback_model: string | null;
    retry_cycles: number | null;
    resolution: string | null;
    allowed_durations: number[] | null;
  };
  style: { profile: string | null; preset: string | null };
  audio: {
    music: boolean | null;
    sfx: boolean | null;
    subtitles: boolean | null;
    narration_path: string | null;
  };
  budget_rub: number | null;
  advanced: {
    force_update_prompts: boolean | null;
    skip_prompt_enhancement: boolean | null;
    sample_before_batch: boolean | null;
    sample_shot_key: string | null;
    final_allow_missing_audio: boolean | null;
  };
};

export type SettingsIssue = {
  code: string;
  level: "error" | "warning";
  field: string;
  message: string;
  // save - мешает сохранению; run - только запуску (зависит от окружения сервера)
  scope: "save" | "run";
  details?: Record<string, unknown>;
};

export type EffectiveValue = { value: unknown; source: "setting" | "env" | "default" };

export type RunSettingsGetResult = {
  settings: RunSettings;
  effective: Record<string, EffectiveValue>;
  defaults: RunSettings;
  digest: string;
  run_active: boolean;
  issues: SettingsIssue[];
};

export type RunSettingsSaveResult = {
  settings: RunSettings;
  digest: string;
  issues: SettingsIssue[];
};

export type ProviderModel = {
  id: string;
  supported_durations: number[];
  supported_resolutions: string[];
  supported_aspect_ratios: string[];
  supported_frame_images: string[];
  max_image_references: number | null;
  source: "static" | "network" | "env" | "registry";
  price: { unit: "per_second" | "per_clip" | null; rub: number | null; source: string };
};

export type ProviderInfo = {
  id: string;
  title: string;
  tool_name: string;
  selectable: boolean;
  pipeline_support: "full" | "limited";
  limitations: string[];
  credentials_found: boolean; // только да/нет: значения ключей наружу не отдаются
  missing_env: string[];
  required_env: string[];
  budget_supported: boolean;
  settings_honored: { resolution: boolean; fallback_model: boolean; retry_cycles: boolean };
  models: ProviderModel[];
  configured_model: string | null;
  catalog_status: "network" | "static_fallback";
  import_error?: string;
};

export type CatalogWarning = { code: string; message: string };

export type ProviderCatalogResult = {
  providers: ProviderInfo[];
  selected_provider: string | null;
  warnings: CatalogWarning[];
};

export type StylePresetInfo = { id: string; title: string; summary: string };

export type StyleCatalogResult = {
  profiles: { id: string; fallback_preset_id: string; presets: StylePresetInfo[] }[];
  active_profile: string;
};

export type CostComponent = {
  id: string;
  title?: string;
  units: number | null;
  unit?: string;
  seconds?: number | null;
  unit_price_rub?: number | null;
  price_unit?: string | null;
  amount_rub: number | null;
  price_source: string;
  basis: "exact" | "rough" | "unknown";
  already_done?: number;
  note?: string;
};

export type BudgetVerdict = "no_budget" | "enough" | "short" | "cannot_check";

export type CostEstimateResult = {
  currency: "RUB";
  provider: string;
  model: string | null;
  components: CostComponent[];
  total_known_rub: number;
  complete: boolean;
  unknown_components: string[];
  budget_rub: number | null;
  budget_verdict: BudgetVerdict;
  short_by_rub?: number;
  warnings: CatalogWarning[];
};

export type PreflightSummaryResult = {
  ready: boolean;
  blocking: string[];
  warnings: string[];
  estimate: CostEstimateResult;
  providers: ProviderInfo[];
  readiness: Record<string, unknown>;
  issues: SettingsIssue[];
};

export const runSettingsGet = (c: StorybookClient, p: { project_id: string }) =>
  c.call<RunSettingsGetResult>("run_settings_get", p);

export const runSettingsSave = (
  c: StorybookClient,
  p: { project_id: string; settings: RunSettings; expected_digest?: string | null },
) => c.call<RunSettingsSaveResult>("run_settings_save", p);

// Имя for_project_id (не project_id): сервер всегда проверяет project_id как существующий проект.
// Обновление списка идёт в сеть и может занять до 15 секунд, поэтому таймаут больше общего.
export const providerCatalog = (c: StorybookClient, p?: { refresh_remote?: boolean; for_project_id?: string }) =>
  c.call<ProviderCatalogResult>("provider_catalog", p, p?.refresh_remote ? { timeoutMs: 30_000 } : undefined);

export const styleCatalog = (c: StorybookClient) => c.call<StyleCatalogResult>("style_catalog");

export const runPreflightSummary = (
  c: StorybookClient,
  p: { project_id: string; settings?: RunSettings; step_ids?: string[] },
) => c.call<PreflightSummaryResult>("run_preflight_summary", p);
