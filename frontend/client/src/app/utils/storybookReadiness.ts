import type { WorkflowInputs, WorkflowParams } from "./workflowParams";

export const STORYBOOK_WORKFLOW_NAME = "storybook_pipeline";

export type StorybookReadinessPayload = {
  project_id: string;
  language: string;
  enable: boolean;
  generate_music: boolean;
};

export type StorybookReadinessRowLevel = "success" | "warning" | "error" | "info";

export type StorybookReadinessRow = {
  key: string;
  label: string;
  value: string;
  level: StorybookReadinessRowLevel;
};

export type StorybookReadinessSummary = {
  status: string;
  projectId: string;
  generationEnabled: boolean | null;
  ready: boolean;
  severity: StorybookReadinessRowLevel;
  blockers: string[];
  warnings: string[];
  errors: string[];
  rows: StorybookReadinessRow[];
};

export type StorybookWorkflowAction = {
  id: string;
  label: string;
  category: string;
  status: string;
  serviceAction: string | null;
  surfaces: Record<string, string>;
  reason?: string;
};

export type StorybookWorkflowActionsSummary = {
  workflowName: string;
  projectId: string;
  actions: StorybookWorkflowAction[];
  rows: StorybookReadinessRow[];
};

type ReadinessRecord = Record<string, unknown>;

const isRecord = (value: unknown): value is ReadinessRecord => {
  return typeof value === "object" && value !== null && !Array.isArray(value);
};

const isBlank = (value: unknown) => {
  return value === null || value === undefined || (typeof value === "string" && value.trim() === "");
};

const valueFromInputs = (inputs: WorkflowInputs, params: WorkflowParams, key: string) => {
  return isBlank(params[key]) ? inputs[key] : params[key];
};

const asString = (value: unknown, fallback: string) => {
  if (typeof value === "string") {
    const trimmed = value.trim();
    return trimmed || fallback;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return fallback;
};

const asBoolean = (value: unknown, fallback: boolean) => {
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return value !== 0;
  if (typeof value === "string") {
    const normalized = value.trim().toLowerCase();
    if (!normalized) return fallback;
    if (["1", "true", "yes", "y", "on"].includes(normalized)) return true;
    if (["0", "false", "no", "n", "off"].includes(normalized)) return false;
  }
  return fallback;
};

const asNullableBoolean = (value: unknown) => {
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return value !== 0;
  if (typeof value === "string") {
    const normalized = value.trim().toLowerCase();
    if (["1", "true", "yes", "y", "on"].includes(normalized)) return true;
    if (["0", "false", "no", "n", "off"].includes(normalized)) return false;
  }
  return null;
};

const asList = (value: unknown) => {
  if (!Array.isArray(value)) return [];
  return value
    .filter((item) => ["string", "number", "boolean"].includes(typeof item))
    .map((item) => String(item));
};

const boolLabel = (value: unknown) => {
  const bool = asNullableBoolean(value);
  if (bool === true) return "yes";
  if (bool === false) return "no";
  return "unknown";
};

const stringValue = (value: unknown, fallback = "unknown") => {
  if (value === null || value === undefined || value === "") return fallback;
  return String(value);
};

const numberValue = (value: unknown, fallback = 0) => {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
};

const artifactStatus = (artifacts: ReadinessRecord, key: string) => {
  const artifact = artifacts[key];
  if (!isRecord(artifact)) return null;
  return `${key}=${stringValue(artifact.status, "unknown")}`;
};

const formatCapabilities = (capabilities: ReadinessRecord) => {
  const keys = ["image", "video", "audio", "music", "render"];
  return keys.map((key) => `${key}=${boolLabel(capabilities[key])}`).join(", ");
};

export const isStorybookWorkflowName = (workflowName: string) => {
  return workflowName === STORYBOOK_WORKFLOW_NAME;
};

export const buildStorybookReadinessPayload = (
  inputs: WorkflowInputs,
  params: WorkflowParams,
): StorybookReadinessPayload => {
  return {
    project_id: asString(valueFromInputs(inputs, params, "project_id"), "storybook_project"),
    language: asString(valueFromInputs(inputs, params, "language"), "ru"),
    enable: asBoolean(valueFromInputs(inputs, params, "generate_screenplay"), true),
    generate_music: asBoolean(valueFromInputs(inputs, params, "generate_music"), true),
  };
};

export const buildStorybookProjectPayload = (inputs: WorkflowInputs, params: WorkflowParams) => {
  return {
    project_id: asString(valueFromInputs(inputs, params, "project_id"), "storybook_project"),
  };
};

export const extractStorybookReadiness = (response: unknown): ReadinessRecord | null => {
  if (!isRecord(response)) return null;
  if (isRecord(response.readiness)) return response.readiness;
  if (isRecord(response.result) && isRecord(response.result.readiness)) return response.result.readiness;
  return response;
};

const camelizeAction = (action: ReadinessRecord): StorybookWorkflowAction => {
  const serviceAction = action.service_action;
  const surfaces = isRecord(action.surfaces) ? action.surfaces : {};
  return {
    id: asString(action.id, "unknown"),
    label: asString(action.label, "Unknown action"),
    category: asString(action.category, "general"),
    status: asString(action.status, "unknown"),
    serviceAction: typeof serviceAction === "string" ? serviceAction : null,
    surfaces: Object.fromEntries(Object.entries(surfaces).map(([key, value]) => [key, stringValue(value)])),
    reason: typeof action.reason === "string" ? action.reason : undefined,
  };
};

const extractWorkflowActionsContract = (payload: unknown): ReadinessRecord | null => {
  if (!isRecord(payload)) return null;
  if (isRecord(payload.workflow_actions)) return payload.workflow_actions;
  if (isRecord(payload.actions) && Array.isArray(payload.actions.actions)) return payload.actions;
  if (Array.isArray(payload.actions)) return payload;
  return null;
};

const workflowActionLevel = (status: string): StorybookReadinessRowLevel => {
  if (status === "available") return "success";
  if (status === "not_implemented") return "error";
  if (status === "manager_only" || status === "web_only") return "warning";
  return "info";
};

export const summarizeStorybookWorkflowActions = (payload: unknown): StorybookWorkflowActionsSummary | null => {
  const contract = extractWorkflowActionsContract(payload);
  if (!contract) return null;
  const rawActions = Array.isArray(contract.actions) ? contract.actions : [];
  const actions = rawActions.filter(isRecord).map(camelizeAction);
  const rows = actions.map((action) => ({
    key: action.id,
    label: action.label,
    value: `${action.status}; manager=${action.surfaces.storybook_manager ?? "unknown"}, react=${
      action.surfaces.react ?? "unknown"
    }, streamlit=${action.surfaces.streamlit ?? "unknown"}${action.reason ? `; ${action.reason}` : ""}`,
    level: workflowActionLevel(action.status),
  }));
  return {
    workflowName: asString(contract.workflow_name, STORYBOOK_WORKFLOW_NAME),
    projectId: asString(contract.project_id, ""),
    actions,
    rows,
  };
};

export const summarizeStorybookReadiness = (readiness: unknown): StorybookReadinessSummary => {
  const data = isRecord(readiness) ? readiness : {};
  const capabilities = isRecord(data.capabilities) ? data.capabilities : {};
  const video = isRecord(data.video) ? data.video : {};
  const music = isRecord(data.music) ? data.music : {};
  const render = isRecord(data.render) ? data.render : {};
  const artifacts = isRecord(data.artifacts) ? data.artifacts : {};
  const finalReview = isRecord(data.final_review) ? data.final_review : {};
  const blockers = asList(data.blocking_reasons);
  const warnings = asList(data.warnings);
  const errors = asList(data.errors);
  const status = asString(data.status, "unknown");
  const generationEnabled = asNullableBoolean(data.generation_enabled);
  const readyValue = asNullableBoolean(data.ready);
  const musicStatus = stringValue(music.status, "").toLowerCase();
  const musicFailed = ["error", "failed"].includes(musicStatus) || errors.includes("music_generation_failed");
  const severity: StorybookReadinessRowLevel =
    blockers.length || errors.length ? "error" : warnings.length ? "warning" : readyValue === false ? "warning" : "success";

  const rows: StorybookReadinessRow[] = [
    {
      key: "provider",
      label: "Provider readiness",
      value: `capabilities=${formatCapabilities(capabilities)}, blockers=${
        blockers.length ? blockers.join(", ") : "none"
      }, warnings=${warnings.length ? warnings.join(", ") : "none"}, errors=${
        errors.length ? errors.join(", ") : "none"
      }`,
      level: blockers.length || errors.length ? "error" : warnings.length ? "warning" : "success",
    },
    {
      key: "video",
      label: "Video provider",
      value: `provider=${stringValue(video.provider)}, expected_clips=${numberValue(
        video.expected_clip_count,
      )}, shots_exists=${boolLabel(video.shots_exists)}`,
      level: asNullableBoolean(capabilities.video) ? "success" : "warning",
    },
    {
      key: "music",
      label: "Music provider",
      value: `provider=${stringValue(music.provider)}, enabled=${boolLabel(
        music.enabled,
      )}, configured=${boolLabel(music.configured)}, model=${stringValue(
        music.model,
      )}, callback=${boolLabel(music.callback_configured)}, status=${stringValue(
        music.status,
      )}, music_exists=${boolLabel(music.music_exists)}`,
      level:
        musicFailed
          ? "error"
          : asNullableBoolean(music.enabled) === false
          ? "info"
          : asNullableBoolean(music.configured) || asNullableBoolean(music.music_exists)
            ? "success"
            : "warning",
    },
    {
      key: "render",
      label: "Render runtime",
      value: `ffmpeg=${stringValue(render.ffmpeg_path, "missing")}, ffprobe=${stringValue(
        render.ffprobe_path,
        "missing",
      )}`,
      level: asNullableBoolean(render.configured) ? "success" : "warning",
    },
  ];

  const artifactBits = [
    "cue_sheet",
    "subtitles",
    "music_manifest",
    "music",
    "final_video",
    "timeline",
    "manifest",
    "asset_manifest",
    "edit_decisions",
    "render_report",
    "final_review",
  ]
    .map((key) => artifactStatus(artifacts, key))
    .filter((value): value is string => Boolean(value));
  if (artifactBits.length) {
    rows.push({
      key: "artifacts",
      label: "Artifacts",
      value: artifactBits.join(", "),
      level: "info",
    });
  }

  if (asNullableBoolean(finalReview.exists)) {
    const failedChecks = asList(finalReview.failed_checks);
    rows.push({
      key: "final_review",
      label: "Final review readiness",
      value: `passed=${boolLabel(finalReview.passed)}, failed_checks=${
        failedChecks.length ? failedChecks.join(", ") : "none"
      }`,
      level: asNullableBoolean(finalReview.passed) ? "success" : "error",
    });
  }

  return {
    status,
    projectId: asString(data.project_id, ""),
    generationEnabled,
    ready: readyValue ?? (status !== "unknown" && blockers.length === 0 && errors.length === 0),
    severity,
    blockers,
    warnings,
    errors,
    rows,
  };
};
