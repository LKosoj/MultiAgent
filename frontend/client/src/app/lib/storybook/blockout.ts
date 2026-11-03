// Обёртки возможностей модуля blockout (болванка) и чистые помощники для экрана.
import type { StorybookClient, ThumbSize } from "./client";
import { mediaThumbUrl } from "./media";

export type BlockoutShotState = "rendered" | "stale" | "missing" | "junction_error";
// Действия, которые запускают прогон (blockout_plan / blockout_run).
export type BlockoutAction = "redraw_shot" | "redraw_chain" | "rebuild_scene" | "build_preview" | "regenerate_shots";
// Ключи словаря actions: запуски плюс две прямые записи.
export type BlockoutActionKey = BlockoutAction | "set_duration" | "leave_as_is";

export type BlockoutBlocker = { code: string; message: string };
export type BlockoutActionState = { enabled: boolean; blockers: BlockoutBlocker[] };

export type BlockoutShotRow = {
  key: string;
  scene_number: number;
  shot_number: number;
  chain_id: string | null;
  state: BlockoutShotState;
  state_title: string;
  duration_s: number | null;
  scene_spec_duration_s: number | null;
  duration_mismatch: boolean;
  p10: boolean;
  allowed: BlockoutActionKey[];
};

export type BlockoutChain = { chain_id: string; shot_keys: string[]; blend_path: string; blend_exists: boolean };
export type BlockoutScene = { scene_number: number; chains: BlockoutChain[]; unchained_shot_keys: string[] };

export type BlockoutSettings = {
  fps: number;
  resolution: string;
  jobs: number;
  burnin: boolean;
  use_as_image_reference: boolean;
  use_as_video_reference: boolean;
  scene_spec_fps: number | null;
  source: "brief" | "default";
};

export type BlockoutStateResult = {
  project_id: string;
  built: boolean;
  empty_reason: "not_built" | "no_shots" | null;
  enabled: boolean;
  settings: BlockoutSettings;
  durations: { allowed: number[]; source: "run_settings" | "brief" | "video_model_caps" | "none" };
  tools: { blender: { available: boolean; version: string | null; message: string }; ffmpeg: { available: boolean } };
  scenes: BlockoutScene[];
  shots: BlockoutShotRow[];
  summary: { total: number; rendered: number; stale: number; missing: number; junction_error: number; p10: number };
  preview: { path: string | null; kind: "burnin" | "plain" | null; servable: boolean; contact_sheets: string[] };
  run: { active: boolean; current_steps: string[] | null; known: boolean };
  checkpoint: { known: boolean; present: boolean | null };
  warnings: { code: string; message: string; file?: string }[];
  actions: Record<BlockoutActionKey, BlockoutActionState>;
  digest: string;
};

export type BlockoutFileRef = { path: string; servable: boolean; mtime: number };
export type BlockoutNote = { level: string | null; code: string | null; message: string | null; shot_key?: string | null; source: string };

export type BlockoutShotDetail = {
  key: string;
  state: BlockoutShotState;
  state_title: string;
  chain_id: string | null;
  notes: BlockoutNote[];
  manifest: {
    rendered_at: string | null;
    fps: number | null;
    resolution: number[] | null;
    duration_s: number | null;
    blender_version: string | null;
    warnings: string[] | null;
    junction_with_prev: unknown;
    junction_with_next: unknown;
    p10_acknowledged: string | null;
  } | null;
  images: {
    blockout_start: BlockoutFileRef | null;
    image_start: BlockoutFileRef | null;
    image_end: BlockoutFileRef | null;
    blockout_end: BlockoutFileRef | null;
  };
  videos: { blockout_ref: BlockoutFileRef | null; final: BlockoutFileRef | null };
  duration: {
    current_s: number | null;
    requested_s: number | null;
    source: string | null;
    allowed: number[];
    editable: boolean;
    reason?: string;
  };
  p10: boolean;
  digest: string | null;
};
export type BlockoutChainDetail = {
  chain_id: string;
  shot_keys: string[];
  notes: BlockoutNote[];
  blend_exists: boolean;
  blend_path: string;
};
export type BlockoutDetailResult = BlockoutShotDetail | BlockoutChainDetail;

export type BlockoutProblem = {
  level: "error" | "warning" | "info";
  code: string;
  message: string;
  step_id: string | null;
  artifact: string | null;
  source?: "blockout";
};
export type BlockoutConfirm = { required: boolean; title: string | null; text: string | null; cost_rub: number | null };
export type BlockoutPlanResult = {
  action: BlockoutAction;
  plan: Record<string, unknown>;
  plan_digest: string | null;
  can_start: boolean;
  problems: BlockoutProblem[];
  confirm: BlockoutConfirm;
  notes: string[];
};
export type BlockoutRunResult = { run_id: string; workflow_id: string; plan: Record<string, unknown> };
export type BlockoutSetDurationResult = {
  shot_key: string;
  duration_s: number;
  digest: string | null;
  rebuild_needed: boolean;
  state: BlockoutShotState;
  shot: BlockoutShotRow;
};
export type BlockoutLeaveAsIsResult = { changed: string[]; skipped: { key: string; reason: string }[] };

type ActionTarget = { shot_keys?: string[]; chain_id?: string };

export const blockoutState = (c: StorybookClient, p: { project_id: string }) =>
  c.call<BlockoutStateResult>("blockout_state", p);

export const blockoutDetail = (c: StorybookClient, p: { project_id: string; shot_key?: string; chain_id?: string }) =>
  c.call<BlockoutDetailResult>("blockout_detail", p);

export const blockoutPlan = (c: StorybookClient, p: { project_id: string; action: BlockoutAction } & ActionTarget) =>
  c.call<BlockoutPlanResult>("blockout_plan", p);

export const blockoutRun = (
  c: StorybookClient,
  p: {
    project_id: string;
    action: BlockoutAction;
    launcher?: "backend" | "local";
    // обязателен для redraw_chain, rebuild_scene и regenerate_shots (plan_digest из blockout_plan)
    expected_plan_digest?: string;
    discard_unfinished?: boolean;
  } & ActionTarget,
) => c.call<BlockoutRunResult>("blockout_run", p);

export const blockoutSetDuration = (
  c: StorybookClient,
  p: { project_id: string; shot_key: string; seconds: number; base_digest: string },
) => c.call<BlockoutSetDurationResult>("blockout_set_duration", p);

export const blockoutLeaveAsIs = (c: StorybookClient, p: { project_id: string; shot_keys: string[] }) =>
  c.call<BlockoutLeaveAsIsResult>("blockout_leave_as_is", p);

// --- чистые помощники (без React) ---

export const SHOT_STATE_TITLES: Record<BlockoutShotState, string> = {
  rendered: "готова",
  stale: "устарела",
  missing: "отсутствует",
  junction_error: "ошибка",
};

export const shotStateTitle = (state: BlockoutShotState): string => SHOT_STATE_TITLES[state] ?? state;

export const isEmptyBlockout = (state: Pick<BlockoutStateResult, "empty_reason">): boolean =>
  state.empty_reason !== null;

export type BlockoutSceneGroup = {
  scene_number: number;
  chains: { chain_id: string; blend_exists: boolean; blend_path: string; shots: BlockoutShotRow[] }[];
  unchained: BlockoutShotRow[];
};

// Дерево «сцена -> цепочка -> шот»; шоты без цепочки лежат отдельно, а сцены без цепочек не теряются.
export const groupByScene = (state: Pick<BlockoutStateResult, "scenes" | "shots">): BlockoutSceneGroup[] => {
  const byKey = new Map(state.shots.map((s) => [s.key, s] as const));
  const pick = (keys: string[]) => keys.map((k) => byKey.get(k)).filter((s): s is BlockoutShotRow => !!s);
  return state.scenes.map((scene) => ({
    scene_number: scene.scene_number,
    chains: scene.chains.map((ch) => ({
      chain_id: ch.chain_id,
      blend_exists: ch.blend_exists,
      blend_path: ch.blend_path,
      shots: pick(ch.shot_keys),
    })),
    unchained: pick(scene.unchained_shot_keys),
  }));
};

// Действие берёт ровно один шот, остальные работают с набором или с проектом целиком.
const NEEDS_ONE: BlockoutActionKey[] = ["redraw_shot", "redraw_chain", "set_duration"];
const NEEDS_SELECTION: BlockoutActionKey[] = ["regenerate_shots", "leave_as_is"];

const selectedRows = (state: Pick<BlockoutStateResult, "shots">, keys: string[]): BlockoutShotRow[] => {
  const wanted = new Set(keys);
  return state.shots.filter((s) => wanted.has(s.key));
};

const selectionProblem = (state: Pick<BlockoutStateResult, "shots">, action: BlockoutActionKey, keys: string[]): string | null => {
  const rows = selectedRows(state, keys);
  if (NEEDS_ONE.includes(action) && rows.length !== 1) return "Выберите один шот";
  if (NEEDS_SELECTION.includes(action) && rows.length === 0) return "Выберите шоты";
  if (rows.length === 0) return null;
  const ok = action === "leave_as_is" ? rows.some((r) => r.allowed.includes(action)) : rows.every((r) => r.allowed.includes(action));
  if (NEEDS_ONE.includes(action) || NEEDS_SELECTION.includes(action)) {
    if (!ok) return action === "leave_as_is" ? "У выбранных шотов нет расхождений с картинками" : "Для выбранного шота действие недоступно";
  }
  return null;
};

export const actionBlockerText = (
  state: Pick<BlockoutStateResult, "actions" | "shots">,
  action: BlockoutActionKey,
  selectedKeys: string[],
): string | null => {
  const blocker = state.actions[action]?.blockers[0];
  if (blocker) return blocker.message;
  return selectionProblem(state, action, selectedKeys);
};

export const actionEnabled = (
  state: Pick<BlockoutStateResult, "actions" | "shots">,
  action: BlockoutActionKey,
  selectedKeys: string[],
): boolean => !!state.actions[action]?.enabled && selectionProblem(state, action, selectedKeys) === null;

export const needsConfirm = (plan: Pick<BlockoutPlanResult, "confirm">): boolean => plan.confirm.required;

export type BlockoutCompare = Record<keyof BlockoutShotDetail["images"], string | null>;

// Четыре адреса миниатюр для сравнения; нет файла или он не отдаётся -> null.
export const compareImages = (
  c: StorybookClient,
  projectId: string,
  detail: Pick<BlockoutShotDetail, "images">,
  size?: ThumbSize,
): BlockoutCompare => {
  const one = (ref: BlockoutFileRef | null) => (ref ? mediaThumbUrl(c, projectId, ref, size) : null);
  return {
    blockout_start: one(detail.images.blockout_start),
    image_start: one(detail.images.image_start),
    image_end: one(detail.images.image_end),
    blockout_end: one(detail.images.blockout_end),
  };
};
