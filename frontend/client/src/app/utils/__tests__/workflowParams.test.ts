import { describe, expect, it } from "vitest";
import { buildWorkflowParameters, initialWorkflowParams } from "../workflowParams";

describe("workflow parameter helpers", () => {
  it("requires an explicit storybook task and allows blank sample_shot_key", () => {
    const inputs = {
      task: "demo prompt from yaml",
      sample_before_batch: false,
      sample_shot_key: "",
      generate_music: true,
      final_allow_missing_audio: false,
      screenplay_time: 120,
    };
    const params = initialWorkflowParams("storybook_pipeline", inputs);

    expect(params.task).toBe("");
    expect(params.sample_shot_key).toBe("");

    const empty = buildWorkflowParameters("storybook_pipeline", inputs, params);
    expect(empty.missing).toEqual(["task"]);
    expect(empty.errors).toEqual([]);

    const ready = buildWorkflowParameters("storybook_pipeline", inputs, {
      ...params,
      task: "custom story",
    });
    expect(ready.missing).toEqual([]);
    expect(ready.parameters).toEqual({
      task: "custom story",
      sample_before_batch: false,
      generate_music: true,
      final_allow_missing_audio: false,
      screenplay_time: 120,
    });
  });

  it("keeps workflow bools and numbers typed", () => {
    const result = buildWorkflowParameters(
      "storybook_pipeline",
      {
        task: "demo",
        sample_before_batch: false,
        generate_music: true,
        final_allow_missing_audio: false,
        screenplay_time: 120,
      },
      {
        task: "custom",
        sample_before_batch: "true",
        generate_music: "false",
        final_allow_missing_audio: "false",
        screenplay_time: "240",
      },
    );

    expect(result.errors).toEqual([]);
    expect(result.parameters.sample_before_batch).toBe(true);
    expect(result.parameters.generate_music).toBe(false);
    expect(result.parameters.final_allow_missing_audio).toBe(false);
    expect(result.parameters.screenplay_time).toBe(240);
  });

  it("rejects invalid storybook positive integers and ranges before submit", () => {
    const result = buildWorkflowParameters(
      "storybook_pipeline",
      {
        task: "demo",
        pages_min: 1,
        pages_max: 2,
        words_per_page_min: 200,
        words_per_page_max: 300,
        screenplay_time: 120,
      },
      {
        task: "custom",
        pages_min: "3",
        pages_max: "2",
        words_per_page_min: "200",
        words_per_page_max: "100",
        screenplay_time: "0",
      },
    );

    expect(result.errors).toContain("screenplay_time должен быть не меньше 1");
    expect(result.errors).toContain("pages_max должен быть не меньше pages_min");
    expect(result.errors).toContain("words_per_page_max должен быть не меньше words_per_page_min");
  });

  it("rejects malformed nonblank storybook sample_shot_key", () => {
    const invalid = buildWorkflowParameters(
      "storybook_pipeline",
      {
        task: "demo",
        sample_shot_key: "",
      },
      {
        task: "custom",
        sample_shot_key: "1-0",
      },
    );

    expect(invalid.errors).toContain("sample_shot_key должен быть в формате positive-int-positive-int");

    const valid = buildWorkflowParameters(
      "storybook_pipeline",
      {
        task: "demo",
        sample_shot_key: "",
      },
      {
        task: "custom",
        sample_shot_key: "12-3",
      },
    );

    expect(valid.errors).toEqual([]);
    expect(valid.parameters.sample_shot_key).toBe("12-3");
  });
});
