export type WorkflowInputValue = string | number | boolean | null | undefined;
export type WorkflowInputs = Record<string, unknown>;
export type WorkflowParams = Record<string, WorkflowInputValue>;

export type WorkflowParameterBuildResult = {
  parameters: Record<string, unknown>;
  missing: string[];
  errors: string[];
};

const OPTIONAL_BLANK_INPUTS: Record<string, Set<string>> = {
  storybook_pipeline: new Set(["sample_shot_key"]),
};

const REQUIRED_USER_INPUTS: Record<string, Set<string>> = {
  storybook_pipeline: new Set(["task"]),
};

const POSITIVE_INT_INPUTS: Record<string, Set<string>> = {
  storybook_pipeline: new Set([
    "pages_min",
    "pages_max",
    "words_per_page_min",
    "words_per_page_max",
    "screenplay_time",
  ]),
};

export const inferWorkflowInputKind = (defaultValue: unknown): "boolean" | "number" | "text" => {
  if (typeof defaultValue === "boolean") return "boolean";
  if (typeof defaultValue === "number") return "number";
  return "text";
};

export const stringifyWorkflowValue = (value: unknown) => {
  if (value === null || value === undefined) return "";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  try {
    return JSON.stringify(value);
  } catch {
    return "";
  }
};

const isBlank = (value: unknown) => {
  return value === null || value === undefined || (typeof value === "string" && value.trim() === "");
};

const isPositiveIntPair = (value: string) => {
  return /^[1-9]\d*-[1-9]\d*$/.test(value);
};

export const isWorkflowInputOptionalBlank = (workflowName: string, key: string) => {
  return OPTIONAL_BLANK_INPUTS[workflowName]?.has(key) ?? false;
};

export const requiresWorkflowUserInput = (workflowName: string, key: string, defaultValue: unknown) => {
  if (REQUIRED_USER_INPUTS[workflowName]?.has(key)) return true;
  if (isWorkflowInputOptionalBlank(workflowName, key)) return false;
  return isBlank(defaultValue);
};

export const workflowNumberMin = (workflowName: string, key: string) => {
  return POSITIVE_INT_INPUTS[workflowName]?.has(key) ? 1 : undefined;
};

export const initialWorkflowParams = (workflowName: string, inputs: WorkflowInputs): WorkflowParams => {
  const defaults: WorkflowParams = {};
  Object.entries(inputs).forEach(([key, value]) => {
    defaults[key] = requiresWorkflowUserInput(workflowName, key, value) ? "" : (value as WorkflowInputValue);
  });
  return defaults;
};

const normalizeInputValue = (
  workflowName: string,
  key: string,
  defaultValue: unknown,
  rawValue: unknown,
  errors: string[],
) => {
  const kind = inferWorkflowInputKind(defaultValue);
  if (kind === "boolean") {
    if (typeof rawValue === "string") {
      return ["1", "true", "yes", "y", "on"].includes(rawValue.trim().toLowerCase());
    }
    return Boolean(rawValue);
  }
  if (kind === "number") {
    const numericValue = typeof rawValue === "number" ? rawValue : Number(String(rawValue).trim());
    if (!Number.isFinite(numericValue)) {
      errors.push(`${key} должен быть числом`);
      return null;
    }
    if (!Number.isInteger(numericValue)) {
      errors.push(`${key} должен быть целым числом`);
      return null;
    }
    const min = workflowNumberMin(workflowName, key);
    if (min !== undefined && numericValue < min) {
      errors.push(`${key} должен быть не меньше ${min}`);
      return null;
    }
    return numericValue;
  }
  if (typeof rawValue === "string") {
    return rawValue.trim();
  }
  return rawValue;
};

export const buildWorkflowParameters = (
  workflowName: string,
  inputs: WorkflowInputs,
  values: WorkflowParams,
): WorkflowParameterBuildResult => {
  const parameters: Record<string, unknown> = {};
  const missing: string[] = [];
  const errors: string[] = [];

  Object.entries(inputs).forEach(([key, defaultValue]) => {
    const explicitValue = values[key];
    const rawValue = isBlank(explicitValue) ? defaultValue : explicitValue;

    if (requiresWorkflowUserInput(workflowName, key, defaultValue) && isBlank(explicitValue)) {
      missing.push(key);
      return;
    }
    if (isWorkflowInputOptionalBlank(workflowName, key) && isBlank(explicitValue)) {
      return;
    }
    if (isBlank(rawValue)) {
      return;
    }

    parameters[key] = normalizeInputValue(workflowName, key, defaultValue, rawValue, errors);
  });

  if (workflowName === "storybook_pipeline") {
    const pagesMin = parameters.pages_min;
    const pagesMax = parameters.pages_max;
    if (typeof pagesMin === "number" && typeof pagesMax === "number" && pagesMax < pagesMin) {
      errors.push("pages_max должен быть не меньше pages_min");
    }
    const wordsMin = parameters.words_per_page_min;
    const wordsMax = parameters.words_per_page_max;
    if (typeof wordsMin === "number" && typeof wordsMax === "number" && wordsMax < wordsMin) {
      errors.push("words_per_page_max должен быть не меньше words_per_page_min");
    }
    const sampleShotKey = parameters.sample_shot_key;
    if (sampleShotKey !== undefined) {
      if (typeof sampleShotKey !== "string" || !isPositiveIntPair(sampleShotKey)) {
        errors.push("sample_shot_key должен быть в формате positive-int-positive-int");
      }
    }
  }

  return { parameters, missing, errors };
};
